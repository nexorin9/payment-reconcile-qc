"""对齐结论的不可变证据快照与可复算回放（Task 26）。

财务科敢在周对账结论单上签字的前提是**每条差异都能追到源行**
------------------------------------------------------------

单看一条差异条目，它只有一句「微信渠道有、HIS 无，金额 128.00 元」。
这句话三个月后没人能复核：账单重拉过了、导出方换了模板、上周的库已经被
下一期的数据覆盖，**当时到底比的是哪几行**就再也说不清了。本模块给每条
对齐结论拍一份**不可变快照**：把参与方（渠道行 + HIS 行 + 全部候选行）、
金额、归一时区后的时间戳、源文件与行号、以及**当时的契约指纹与索引版本**
一并落下来，且**只追加、不更新**。

三条纪律（每条都对应一种真实的签字风险）
----------------------------------------

1. **不可变 = 只追加**。已落盘的快照永不 UPDATE：结论单上那条差异引用的
   是快照 ``id``，改写它等于让「上周的结论单」在数据库里悄悄换了内容。
   同 ``(period, conclusion_key)`` 二次生成且内容哈希不同时报
   ``SNAPSHOT_CONFLICT`` 并**把两份都留着** —— 冲突本身就是信息
   （这一条在两次扫描之间变了），偷偷覆盖掉就再也查不出「什么时候变的、
   为什么变」。
2. **复算走快照，不走库**。:func:`replay_snapshot` 只用快照里记的候选集
   重建对齐、重跑判定，与账单表当前状态**无关**。这正是它的用途：源数据
   已被重拉时，结论单上那条差异仍要能被独立复算一遍。复算与记录不一致时
   标 ``replay_mismatch`` 并逐条列出变化点 —— 它同时是**口径变更的报警
   信号**（契约指纹变了而结论跟着变了，或反之）。
3. **脱敏是结构性的，不是靠自觉**。快照只允许固定的十个字段
   （见 :data:`SNAPSHOT_ALLOWED_FIELDS`），写入前经
   :func:`assert_deidentified` 逐键核对；出现患者姓名 / 身份证 / 手机号 /
   住院号这类字段直接抛 ``SNAPSHOT_NOT_DEIDENTIFIED``。

源产品对照
----------
参考实现（``soda-core``）**没有这一层，也没有等价物**：``contract verify``
把逐 check 的结果打到终端，进程退出即消失；契约 publish/fetch 推的是
契约本身，不是某次执行的证据。源产品的结论靠「当场打印」与「重跑一次」
支撑 —— 重跑得出不同结果时，**没有办法区分是数据变了、口径变了还是代码
变了**。本模块把这一层显式化，让「上周那条差异当时依据哪几行」有据可查。

与主路径的关系
--------------
快照挂在 :class:`~reconq.align.matcher.MatchOutcome` 与
:class:`~reconq.align.trace.TraceConclusion` 这两个**已有结论类型**上，
不新开一条判定路径：快照是既有结论的投影，复算也只是把同一套判定器
（:func:`reconq.align.matcher.align_channel`）在重建出来的候选集上再跑一遍。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from ..errors import ReconqError
from .ambiguity import AmbiguityCandidate
from .index import normalize_key
from .matcher import AlignRecord, MatchOutcome, WEAK_MATCH_TOLERANCE, align_channel, build_index
from .trace import GLOBAL_CHANNEL, TraceConclusion, TraceNode

__all__ = [
    "ALIGN_INDEX_VERSION",
    "SNAPSHOT_ALLOWED_FIELDS",
    "FORBIDDEN_PATIENT_KEYS",
    "SnapshotRef",
    "EvidenceSnapshot",
    "ReplayChange",
    "ReplayResult",
    "SnapshotWriteOutcome",
    "conclusion_key_of",
    "conclusion_key_for",
    "snapshot_from_outcome",
    "snapshot_from_trace_conclusion",
    "replay_snapshot",
    "render_snapshot",
    "render_replay",
    "assert_deidentified",
    "init_align_snapshot_schema",
    "record_alignment_snapshot",
    "load_alignment_snapshots",
    "latest_alignment_snapshot",
    "count_alignment_snapshots",
]

logger = logging.getLogger("reconq.align.snapshot")

#: 索引/对齐器的版本号。快照要记住「当时是按哪一版对齐逻辑判的」——
#: 对齐逻辑改动会让同样的候选集得出不同结论，版本号变了而快照没变时，
#: :func:`replay_snapshot` 的比较结果就不该被当成「数据有问题」。
ALIGN_INDEX_VERSION = "sharded-inverted/1"

#: 快照**允许**出现的字段。刻意用白名单而不是黑名单：黑名单要靠人记得
#: 列出所有不该存的字段（姓名？床号？就诊卡号？），漏一个就落进库了。
SNAPSHOT_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "side",
        "channel",
        "period",
        "order_no",
        "his_order_no",
        "amount",
        "refund",
        "trade_time",
        "trade_type",
        "department",
        "settle_type",
        "is_refund",
        "source_file",
        "source_row_no",
        "record_id",
    }
)

#: 患者可识别字段。命中即拒写（见 :func:`assert_deidentified`）。
FORBIDDEN_PATIENT_KEYS: frozenset[str] = frozenset(
    {
        "patient_name",
        "patient_id",
        "patient_no",
        "inpatient_no",
        "outpatient_no",
        "name",
        "id_card",
        "idcard",
        "phone",
        "mobile",
        "tel",
        "bed_no",
        "card_no",
        "姓名",
        "患者姓名",
        "身份证",
        "身份证号",
        "手机号",
        "电话",
        "住院号",
        "门诊号",
        "病案号",
        "床号",
    }
)

#: 身份证号（18 位，末位可为 X）与手机号（11 位 1 开头）的值形态。
#: 键名不在黑名单里但**值**是身份证/手机号，同样要拦 ——
#: 导出方把姓名写成备注、把手机号写进订单备注列是很常见的事。
_ID_CARD_RE = re.compile(r"^\d{17}[\dXx]$")
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


# ─────────────────────────── 数据结构 ───────────────────────────


@dataclass(frozen=True)
class SnapshotRef:
    """快照里的一条**证据行引用**。

    只记「哪一行、多少钱、什么时候、哪个文件第几行」，不记整行原文：
    原文带渠道自定义列（可能含患者信息），而结论单与复算只需要这七样。
    ``side`` 区分 ``channel``（渠道行）与 ``his``（权威侧收费记录）——
    两者金额关系不同（渠道侧退款记在原单行的退款列上、HIS 侧红冲本身即负），
    混作一堆就没法复算了。
    """

    side: str
    channel: str
    period: str
    order_no: str
    his_order_no: str = ""
    amount: str = "0"
    refund: Optional[str] = None
    trade_time: Optional[str] = None
    trade_type: str = ""
    department: str = ""
    settle_type: str = ""
    is_refund: bool = False
    source_file: str = ""
    source_row_no: int = 0
    record_id: str = ""

    @property
    def ref(self) -> str:
        """可回查定位：``文件名:行号``。"""
        if self.source_file:
            return f"{self.source_file}:{self.source_row_no}"
        return f"#{self.source_row_no}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "channel": self.channel,
            "period": self.period,
            "order_no": self.order_no,
            "his_order_no": self.his_order_no,
            "amount": self.amount,
            "refund": self.refund,
            "trade_time": self.trade_time,
            "trade_type": self.trade_type,
            "department": self.department,
            "settle_type": self.settle_type,
            "is_refund": self.is_refund,
            "source_file": self.source_file,
            "source_row_no": self.source_row_no,
            "record_id": self.record_id,
            "ref": self.ref,
        }

    def render(self) -> str:
        flags = "（红冲）" if self.is_refund else ""
        when = f" {self.trade_time}" if self.trade_time else ""
        dept = f" {self.department}" if self.department else ""
        return (
            f"{self.side}/{self.channel} {self.amount} 元{when}{dept}{flags} · {self.ref}"
        )

    @classmethod
    def from_record(cls, record: AlignRecord, *, side: str, ch_id: int = 0) -> SnapshotRef:
        """从 :class:`AlignRecord` 造引用。"""
        return cls(
            side=side,
            channel=record.channel,
            period=record.period,
            order_no=record.order_no,
            his_order_no=record.his_order_no,
            amount=str(record.amount),
            refund=str(record.refund) if record.refund is not None else None,
            trade_time=record.trade_time.isoformat() if record.trade_time else None,
            trade_type=record.trade_type,
            department=record.department,
            settle_type=record.settle_type,
            is_refund=record.is_refund,
            source_file=record.source_file,
            source_row_no=record.source_row_no,
            record_id=str(record.ch_id or ch_id),
        )

    @classmethod
    def from_candidate(
        cls, candidate: AmbiguityCandidate, *, channel: str = "his"
    ) -> SnapshotRef:
        """从 :class:`AmbiguityCandidate` 造引用。

        候选只有净额（没有独立的退款列），因此 ``amount`` 记净额、
        ``refund`` 留空 —— 复算时按「无退款列」重建，净额语义与当初判定
        时一致。写成 ``amount = 净额 + 退款`` 会让复算凭空多出一笔退款。
        """
        return cls(
            side="his",
            channel=channel,
            period=candidate.period,
            order_no=candidate.record_id,
            his_order_no=candidate.record_id,
            amount=candidate.amount or "0",
            refund=None,
            trade_time=candidate.trade_time or None,
            trade_type="",
            department=candidate.department,
            settle_type=candidate.settle_type,
            is_refund=candidate.is_refund,
            source_file=candidate.source_file,
            source_row_no=candidate.row_no,
            record_id=candidate.record_id,
        )

    def to_record(self, *, ch_id: int = 0) -> AlignRecord:
        """把引用还原成 :class:`AlignRecord`，供 :func:`replay_snapshot` 复算。

        两处**必须**当心的还原口径：

        1. 金额不做二次净额计算。快照里记的 ``amount`` 在渠道行上是原单
           金额（退款另记在 ``refund``）、在 HIS 侧是净额，两边口径本就
           不同，统一在这里再扣一次会让退款变成两笔。
        2. **退费标记要还原回去**。:attr:`AlignRecord.is_refund` 是从
           「退款列非零」或「交易类型写着退费/红冲」推出来的，候选行两样
           都没有（候选只带净额）。不补回去的话一条红冲行复算时会变成
           普通收费，「退费与原单并存」被判成「一码付拆多笔」——
           状态还是 ambiguous，看着没差，建议话术却把经办人引去查拆分
           说明，而实际该查的是红冲是否生效。
        """
        trade_type = self.trade_type
        if self.is_refund and not trade_type:
            trade_type = "红冲"
        return AlignRecord(
            channel=self.channel,
            period=self.period,
            order_no=self.order_no,
            amount=Decimal(self.amount or "0"),
            refund=Decimal(self.refund) if self.refund else None,
            trade_time=_parse_time(self.trade_time),
            trade_type=trade_type,
            department=self.department,
            settle_type=self.settle_type,
            his_order_no=self.his_order_no,
            source_file=self.source_file,
            source_row_no=self.source_row_no,
            ch_id=ch_id,
        )


def _parse_time(text: Optional[str]):
    if not text:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # 快照里存的是归一后的带偏移 ISO 串，解析不了只可能是数据被手工改过。
        # 留空而不是抛错：让复算照跑并把差异报成 replay_mismatch，比在
        # 读快照这一步就崩掉更有用 —— 排障的人要看到的是「哪一条对不上」。
        logger.warning("快照中的 trade_time=%r 无法解析，该行按无时间处理", text)
        return None


@dataclass(frozen=True)
class EvidenceSnapshot:
    """一条对齐结论的**不可变**证据快照。"""

    conclusion_key: str
    period: str
    channel: str
    #: 结论类型：``MatchStatus`` 的取值或 ``TraceKind`` 的取值
    kind: str
    order_no: str
    detail: str = ""
    diff_amount: str = ""
    #: 歧义/弱匹配时的**成因形态**（``ambiguity.kind`` 的取值），无则空串。
    #:
    #: 刻意存成独立字段而**不**从 ``detail`` 中文文案里反解：形态决定建议
    #: 话术（「退费与原单并存」要查红冲，「一码付拆多笔」要查拆分说明），
    #: 从散文里抠字回来等于把一条业务判据绑在措辞上 —— 改一次话术就静默
    #: 改掉复算的判定基准。空串是正常值（matched / channel_only 没有形态）。
    ambiguity_kind: str = ""
    participants: tuple[SnapshotRef, ...] = ()
    contract_digest: str = ""
    contract_check_digest: str = ""
    index_version: str = ALIGN_INDEX_VERSION
    created_at: str = ""
    content_hash: str = ""

    @property
    def participant_total(self) -> int:
        return len(self.participants)

    @property
    def channel_refs(self) -> list[SnapshotRef]:
        return [p for p in self.participants if p.side == "channel"]

    @property
    def his_refs(self) -> list[SnapshotRef]:
        return [p for p in self.participants if p.side == "his"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "conclusion_key": self.conclusion_key,
            "period": self.period,
            "channel": self.channel,
            "kind": self.kind,
            "order_no": self.order_no,
            "detail": self.detail,
            "diff_amount": self.diff_amount,
            "ambiguity_kind": self.ambiguity_kind,
            "participants": [p.to_dict() for p in self.participants],
            "participant_total": self.participant_total,
            "contract_digest": self.contract_digest,
            "contract_check_digest": self.contract_check_digest,
            "index_version": self.index_version,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
        }

    def render(self) -> str:
        head = f"快照 {self.content_hash[:12] or '（未落盘）'} · {self.period} {self.channel} · {self.kind} {self.order_no}"
        if self.diff_amount:
            head += f"（差额 {self.diff_amount} 元）"
        lines = [head]
        if self.detail:
            lines.append(f"  说明：{self.detail}")
        lines.append(f"  参与方 {self.participant_total} 条（复算只用这些，不读账单表现状）：")
        lines.extend(f"    {p.render()}" for p in self.participants)
        lines.append(
            f"  口径指纹 {self.contract_digest[:12] or '（未记录）'}"
            f" · 口径内容指纹 {self.contract_check_digest[:12] or '（未记录）'}"
            f" · 索引版本 {self.index_version or '（未记录）'}"
        )
        if self.created_at:
            lines.append(f"  拍快照时刻：{self.created_at}")
        return "\n".join(lines)

    def compute_hash(self) -> str:
        """内容哈希。**刻意不含** ``created_at``：
        同一份数据拍两次快照必须得到同一个哈希，否则「重跑了一次但内容没变」
        会被误报成冲突，而那恰恰是最常见的一种重跑。"""
        payload = {
            "conclusion_key": self.conclusion_key,
            "period": self.period,
            "channel": self.channel,
            "kind": self.kind,
            "order_no": self.order_no,
            "detail": self.detail,
            "diff_amount": self.diff_amount,
            "ambiguity_kind": self.ambiguity_kind,
            "participants": [p.to_dict() for p in self.participants],
            "contract_digest": self.contract_digest,
            "contract_check_digest": self.contract_check_digest,
            "index_version": self.index_version,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def with_hash(self) -> "EvidenceSnapshot":
        return EvidenceSnapshot(
            conclusion_key=self.conclusion_key,
            period=self.period,
            channel=self.channel,
            kind=self.kind,
            order_no=self.order_no,
            detail=self.detail,
            diff_amount=self.diff_amount,
            ambiguity_kind=self.ambiguity_kind,
            participants=self.participants,
            contract_digest=self.contract_digest,
            contract_check_digest=self.contract_check_digest,
            index_version=self.index_version,
            created_at=self.created_at,
            content_hash=self.compute_hash(),
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "EvidenceSnapshot":
        payload = json.loads(row["payload_json"])
        return cls(
            conclusion_key=str(row["conclusion_key"]),
            period=str(row["period"]),
            channel=str(row["channel"]),
            kind=str(row["kind"]),
            order_no=str(payload.get("order_no", "")),
            detail=str(payload.get("detail", "")),
            diff_amount=str(payload.get("diff_amount", "")),
            ambiguity_kind=str(payload.get("ambiguity_kind", "")),
            participants=tuple(
                SnapshotRef(**{k: v for k, v in p.items() if k != "ref"})
                for p in payload.get("participants", [])
            ),
            contract_digest=str(row["contract_digest"]),
            contract_check_digest=str(row["contract_check_digest"]),
            index_version=str(row["index_version"]),
            created_at=str(row["created_at"]),
            content_hash=str(row["content_hash"]),
        )


# ─────────────────────────── 构造 ───────────────────────────


def conclusion_key_for(channel: str, period: str, order_no: str) -> str:
    """结论键：``渠道:账期:归一单号``。

    **刻意不含结论类型**。含了的话，同一实体在两次扫描里从 ``matched``
    变成 ``mismatch`` 会落到两个不同的键下，冲突检测就永远测不出来 ——
    而「上周对齐了这笔、这周没对齐」正是财务科最需要被告知的一种变化。
    """
    return f"{channel}:{period}:{normalize_key(order_no)}"


def conclusion_key_of(outcome: MatchOutcome) -> str:
    return conclusion_key_for(
        outcome.key.channel,
        outcome.key.period,
        outcome.key.order_no or (outcome.channel_record.his_order_no if outcome.channel_record else ""),
    )


def _participants_of(outcome: MatchOutcome) -> list[SnapshotRef]:
    """把一条对齐结论的参与方收齐：渠道行 + 认领的 HIS 行 + 全部候选。

    三者**都要**：认领行与候选行在形态上互斥（认领了就不会是候选），
    但漏掉候选会让「歧义」「弱匹配」「跨账期」这三类的快照只剩渠道行 ——
    复算时索引是空的，判定必然翻成单边账，``replay_mismatch`` 变成
    每次都报的噪声。
    """
    refs: list[SnapshotRef] = []
    if outcome.channel_record is not None:
        refs.append(SnapshotRef.from_record(outcome.channel_record, side="channel"))
    if outcome.his_record is not None:
        refs.append(SnapshotRef.from_record(outcome.his_record, side="his"))
    if outcome.his_record is None:
        for candidate in outcome.candidates:
            refs.append(SnapshotRef.from_candidate(candidate))
    return refs


def snapshot_from_outcome(
    outcome: MatchOutcome,
    *,
    contract_digest: str = "",
    contract_check_digest: str = "",
    index_version: str = ALIGN_INDEX_VERSION,
    created_at: str = "",
    now: Optional[Any] = None,
) -> EvidenceSnapshot:
    """给一条 :class:`MatchOutcome` 拍证据快照。"""
    snap = EvidenceSnapshot(
        conclusion_key=conclusion_key_of(outcome),
        period=outcome.key.period,
        channel=outcome.key.channel,
        kind=outcome.status,
        order_no=outcome.key.order_no,
        detail=outcome.detail or (outcome.ambiguity.headline() if outcome.ambiguity else ""),
        diff_amount=outcome.diff_amount,
        ambiguity_kind=outcome.ambiguity.kind if outcome.ambiguity else "",
        participants=tuple(_participants_of(outcome)),
        contract_digest=contract_digest,
        contract_check_digest=contract_check_digest,
        index_version=index_version,
        created_at=created_at or _now(now),
    )
    return snap.with_hash()


def snapshot_from_trace_conclusion(
    conclusion: TraceConclusion,
    *,
    period: str,
    order_no: str = "",
    nodes: Sequence[TraceNode] = (),
    contract_digest: str = "",
    contract_check_digest: str = "",
    index_version: str = ALIGN_INDEX_VERSION,
    created_at: str = "",
    now: Optional[Any] = None,
) -> EvidenceSnapshot:
    """给一条 :class:`TraceConclusion`（连坐结论）拍证据快照。

    连坐结论跨渠道 / 跨账期，``channel`` 恒为 :data:`~reconq.align.trace.GLOBAL_CHANNEL`
    —— 记在任一路渠道名下等于暗示「是这一路多记了一笔」。
    """
    refs = [
        SnapshotRef(
            side="his" if node.side == "his" else "channel",
            channel=node.channel,
            period=node.period,
            order_no=node.order_no,
            his_order_no=node.his_order_no,
            amount=str(node.net_amount),
            refund=None,
            trade_time=node.trade_time,
            trade_type="",
            department=node.department,
            settle_type=node.settle_type,
            is_refund=node.is_refund,
            source_file=node.source_file,
            source_row_no=node.source_row_no,
            record_id=node.order_no,
        )
        for node in nodes
    ]
    snap = EvidenceSnapshot(
        conclusion_key=conclusion_key_for(GLOBAL_CHANNEL, period, order_no or conclusion.order_no),
        period=period,
        channel=GLOBAL_CHANNEL,
        kind=conclusion.kind,
        order_no=order_no or conclusion.order_no,
        detail=conclusion.detail,
        diff_amount=conclusion.diff_amount,
        participants=tuple(refs),
        contract_digest=contract_digest,
        contract_check_digest=contract_check_digest,
        index_version=index_version,
        created_at=created_at or _now(now),
    )
    return snap.with_hash()


def _now(now: Optional[Any]) -> str:
    """当前时刻，归一到院内时区后取 ISO 串。

    归一是必须的：快照要跟着结论单流转很久，落库时刻本身就该是带偏移的
    ISO 串（与账单表 ``trade_time`` 同一口径），否则跨时区读库时「拍于
    何时」本身就对不上。
    """
    if now is not None:
        return str(now)
    from datetime import datetime

    from ..sources.timezone import HOSPITAL_TZ

    return datetime.now(HOSPITAL_TZ).isoformat()


# ─────────────────────────── 脱敏 ───────────────────────────


def assert_deidentified(payload: Any, *, where: str = "快照") -> None:
    """核对 ``payload`` 里没有患者可识别信息。

    两道检查：键名命中 :data:`FORBIDDEN_PATIENT_KEYS`，或**值**长得像
    身份证 / 手机号。都拒。检查失败抛异常而不是打日志 —— 快照一旦落库
    就要跟着结论单流转很久，事后清理的成本远高于当场拒写。
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).strip().lower()
            if lowered in FORBIDDEN_PATIENT_KEYS:
                raise ReconqError(
                    "SNAPSHOT_NOT_DEIDENTIFIED",
                    f"{where} 中出现患者可识别字段 {key!r}：本产品的快照只允许"
                    f"金额、订单号、凭证号、账期、科室与时间，"
                    f"患者信息不落快照（结论单附录用脱敏样例即可回查）",
                )
            assert_deidentified(value, where=f"{where}.{key}")
        return
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_deidentified(item, where=f"{where}[{index}]")
        return
    if isinstance(payload, str):
        text = payload.strip()
        if _ID_CARD_RE.match(text) or _PHONE_RE.match(text):
            raise ReconqError(
                "SNAPSHOT_NOT_DEIDENTIFIED",
                f"{where} 的值 {text[:3]}*** 形似身份证号或手机号；"
                f"快照不落患者可识别信息，请确认该字段是订单号而不是个人信息",
            )


def _validate_refs(refs: Iterable[SnapshotRef]) -> None:
    for ref in refs:
        payload = {k: v for k, v in ref.to_dict().items() if k != "ref"}
        extra = set(payload) - SNAPSHOT_ALLOWED_FIELDS
        if extra:  # pragma: no cover - 结构改动时兜底
            raise ReconqError(
                "SNAPSHOT_FIELD_NOT_ALLOWED",
                f"快照行引用出现未登记字段 {sorted(extra)}；"
                f"允许字段清单见 SNAPSHOT_ALLOWED_FIELDS",
            )
        assert_deidentified(payload, where=f"快照参与方 {ref.ref}")


# ─────────────────────────── 复算回放 ───────────────────────────


@dataclass(frozen=True)
class ReplayChange:
    """复算与快照记录之间的一处变化点。"""

    field_name: str
    recorded: str
    replayed: str
    note: str = ""

    def render(self) -> str:
        line = f"{self.field_name}：记录 {self.recorded or '（空）'} → 复算 {self.replayed or '（空）'}"
        if self.note:
            line += f"（{self.note}）"
        return line

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "recorded": self.recorded,
            "replayed": self.replayed,
            "note": self.note,
        }


@dataclass(frozen=True)
class ReplayResult:
    """一次复算的结论。"""

    conclusion_key: str
    period: str
    channel: str
    order_no: str
    recorded_kind: str
    replayed_kind: str
    recorded_diff_amount: str = ""
    replayed_diff_amount: str = ""
    contract_digest: str = ""
    current_contract_digest: str = ""
    index_version: str = ""
    current_index_version: str = ""
    replayable: bool = True
    replay_mismatch: bool = False
    changes: tuple[ReplayChange, ...] = ()
    note: str = ""

    @property
    def change_total(self) -> int:
        return len(self.changes)

    @property
    def contract_changed(self) -> bool:
        return bool(
            self.contract_digest
            and self.current_contract_digest
            and self.contract_digest != self.current_contract_digest
        )

    @property
    def index_version_changed(self) -> bool:
        return bool(
            self.index_version
            and self.current_index_version
            and self.index_version != self.current_index_version
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conclusion_key": self.conclusion_key,
            "period": self.period,
            "channel": self.channel,
            "order_no": self.order_no,
            "recorded_kind": self.recorded_kind,
            "replayed_kind": self.replayed_kind,
            "recorded_diff_amount": self.recorded_diff_amount,
            "replayed_diff_amount": self.replayed_diff_amount,
            "contract_digest": self.contract_digest,
            "current_contract_digest": self.current_contract_digest,
            "contract_changed": self.contract_changed,
            "index_version": self.index_version,
            "current_index_version": self.current_index_version,
            "index_version_changed": self.index_version_changed,
            "replayable": self.replayable,
            "replay_mismatch": self.replay_mismatch,
            "change_total": self.change_total,
            "changes": [c.to_dict() for c in self.changes],
            "note": self.note,
        }

    def render(self) -> str:
        head = f"复算 {self.conclusion_key}"
        lines = [head]
        lines.append(
            f"  记录结论：{self.recorded_kind}"
            + (f"（差额 {self.recorded_diff_amount} 元）" if self.recorded_diff_amount else "")
        )
        if not self.replayable:
            lines.append(f"  复算：不可复算 —— {self.note}")
            return "\n".join(lines)
        lines.append(
            f"  复算结论：{self.replayed_kind}"
            + (f"（差额 {self.replayed_diff_amount} 元）" if self.replayed_diff_amount else "")
        )
        if self.contract_changed:
            lines.append(
                f"  ! 口径指纹已变：{self.contract_digest[:12]} → {self.current_contract_digest[:12]}；"
                f"结论翻转可能来自口径变更而非数据出错，请先确认口径版本"
            )
        if self.index_version_changed:
            lines.append(
                f"  ! 索引版本已变：{self.index_version} → {self.current_index_version}；"
                f"复算走的是新版本判定器"
            )
        if self.replay_mismatch:
            lines.append(f"  ✗ 复算不一致（{self.change_total} 处变化）：")
            lines.extend(f"    - {c.render()}" for c in self.changes)
        else:
            lines.append("  ✓ 复算与快照记录一致（候选集完全取自快照，未读账单表）")
        if self.note:
            lines.append(f"  提示：{self.note}")
        return "\n".join(lines)


def _replayable_outcome(snapshot: EvidenceSnapshot):
    """按快照重建对齐并重跑判定。返回 ``(outcome, note)``。

    ``note`` 非空表示**不可复算**，原因逐条列明而不是静默跳过。
    """
    channel_refs = snapshot.channel_refs
    if len(channel_refs) != 1:
        return None, (
            f"快照里有 {len(channel_refs)} 条渠道行引用，判定器的输入是单行渠道账单，"
            f"无法复算；该结论多半是跨渠道/跨账期的连坐结论"
            f"（复算口径与 channel_only 那一类不同）"
        )
    from ..sources.timezone import period_window

    his_records = [
        ref.to_record(ch_id=index + 1)
        for index, ref in enumerate(snapshot.his_refs)
    ]
    channel_records = [channel_refs[0].to_record(ch_id=0)]
    index = build_index(his_records, period=snapshot.period)
    window = period_window(snapshot.period) if snapshot.period else None
    result = align_channel(
        snapshot.channel,
        channel_records,
        index,
        period=snapshot.period,
        window=window,
    )
    if not result.outcomes:  # pragma: no cover - 一条渠道行必出一条结论
        return None, "复算没有产出任何结论（渠道行为空）"
    return result.outcomes[0], ""


def replay_snapshot(
    snapshot: EvidenceSnapshot,
    *,
    current_contract_digest: str = "",
    current_index_version: str = "",
) -> ReplayResult:
    """用快照自带的候选集复算一遍结论。

    **只读快照**：不碰账单表、不碰文件系统。源数据已被重拉、上一期的库
    已被覆盖，本函数仍应得出与快照记录相同的结论 —— 这正是它存在的意义。

    不一致时标 :attr:`ReplayResult.replay_mismatch` 并逐条列出变化点。
    口径指纹与索引版本的变化**单独**报（不混进 ``changes``）：那属于
    「比较的前提变了」，与「结论本身翻���了」是两件事，混在一起会让
    排障的人先去查数据、而真因是上周改了口径。
    """
    outcome, note = _replayable_outcome(snapshot)
    if outcome is None:
        return ReplayResult(
            conclusion_key=snapshot.conclusion_key,
            period=snapshot.period,
            channel=snapshot.channel,
            order_no=snapshot.order_no,
            recorded_kind=snapshot.kind,
            replayed_kind="",
            recorded_diff_amount=snapshot.diff_amount,
            contract_digest=snapshot.contract_digest,
            current_contract_digest=current_contract_digest,
            index_version=snapshot.index_version,
            current_index_version=current_index_version,
            replayable=False,
            replay_mismatch=False,
            note=note,
        )

    replayed_kind = outcome.status
    replayed_diff = outcome.diff_amount
    replayed_ambiguity = outcome.ambiguity.kind if outcome.ambiguity else ""

    changes: list[ReplayChange] = []
    if replayed_kind != snapshot.kind:
        changes.append(
            ReplayChange(
                "判定状态",
                snapshot.kind,
                replayed_kind,
                "同一候选集在当前判定器下得出不同状态",
            )
        )
    if replayed_diff != snapshot.diff_amount:
        changes.append(
            ReplayChange(
                "差额",
                snapshot.diff_amount,
                replayed_diff,
                "差额对不上时先看参与方金额是否被改写过",
            )
        )
    if replayed_ambiguity and snapshot.kind in _AMBIGUITY_KINDS:
        # 歧义形态变了但状态没变（都是 ambiguous）时，仍要报出来：
        # 「拒绝归并」本身没变，成因从「退费与原单并存」变成「一码付拆多笔」
        # 会把经办人引去查错的方向 —— 形态决定建议话术，话术错比状态错更误导。
        if snapshot.ambiguity_kind and snapshot.ambiguity_kind != replayed_ambiguity:
            changes.append(
                ReplayChange(
                    "歧义形态",
                    snapshot.ambiguity_kind,
                    replayed_ambiguity,
                    "状态未变但成因变了，建议话术会跟着变",
                )
            )

    return ReplayResult(
        conclusion_key=snapshot.conclusion_key,
        period=snapshot.period,
        channel=snapshot.channel,
        order_no=snapshot.order_no,
        recorded_kind=snapshot.kind,
        replayed_kind=replayed_kind,
        recorded_diff_amount=snapshot.diff_amount,
        replayed_diff_amount=replayed_diff,
        contract_digest=snapshot.contract_digest,
        current_contract_digest=current_contract_digest,
        index_version=snapshot.index_version,
        current_index_version=current_index_version or ALIGN_INDEX_VERSION,
        replayable=True,
        replay_mismatch=bool(changes),
        changes=tuple(changes),
    )


#: 判定为「拒绝归并」的状态集合。这些状态下歧义形态是结论的一部分。
_AMBIGUITY_KINDS = frozenset({"ambiguous", "weak_pending", "cross_period", "duplicate_claim"})

def render_snapshot(snapshot: EvidenceSnapshot) -> str:
    """把快照渲染成可读文本（结论单附录用）。"""
    return snapshot.render()


def render_replay(result: ReplayResult) -> str:
    return result.render()


# ─────────────────────────── 存储 ───────────────────────────

ALIGN_SNAPSHOT_TABLE = "align_snapshots"

_ALIGN_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS align_snapshots (
    snap_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    period                TEXT    NOT NULL,
    conclusion_key        TEXT    NOT NULL,
    channel               TEXT    NOT NULL DEFAULT '',
    kind                  TEXT    NOT NULL DEFAULT '',
    order_no              TEXT    NOT NULL DEFAULT '',
    content_hash          TEXT    NOT NULL,
    contract_digest       TEXT    NOT NULL DEFAULT '',
    contract_check_digest TEXT    NOT NULL DEFAULT '',
    index_version         TEXT    NOT NULL DEFAULT '',
    participant_total     INTEGER NOT NULL DEFAULT 0,
    payload_json          TEXT    NOT NULL,
    created_at            TEXT    NOT NULL DEFAULT ''
)
"""

#: 只建**普通**索引，不建唯一索引：同 (period, conclusion_key) 允许存多份
#: （内容不同即冲突，两份都要留着，见模块头纪律 1）。查最新一份走
#: ``snap_id`` 倒序，不需要唯一键。
_ALIGN_SNAPSHOT_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS ix_align_snapshot_key
        ON align_snapshots (period, conclusion_key, snap_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_align_snapshot_hash
        ON align_snapshots (content_hash)
    """,
)


def init_align_snapshot_schema(conn: sqlite3.Connection) -> None:
    """建对齐证据快照表。幂等。"""
    conn.execute(_ALIGN_SNAPSHOT_DDL)
    for statement in _ALIGN_SNAPSHOT_INDEXES:
        conn.execute(statement)


@dataclass(frozen=True)
class SnapshotWriteOutcome:
    """一次快照写入的结果。"""

    snapshot: EvidenceSnapshot
    snap_id: int = 0
    #: 同一结论键下已存在**不同内容**的快照（真冲突）
    conflict_with: tuple[int, ...] = ()
    #: 同一结论键下已存在**相同内容**的快照（重复拍，等价于无操作）
    duplicate_of: Optional[int] = None

    @property
    def conflict(self) -> bool:
        return bool(self.conflict_with)

    @property
    def duplicate(self) -> bool:
        return self.duplicate_of is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "conclusion_key": self.snapshot.conclusion_key,
            "snap_id": self.snap_id,
            "content_hash": self.snapshot.content_hash,
            "conflict": self.conflict,
            "conflict_with": list(self.conflict_with),
            "duplicate": self.duplicate,
            "duplicate_of": self.duplicate_of,
        }


def record_alignment_snapshot(
    conn: sqlite3.Connection,
    snapshot: EvidenceSnapshot,
    *,
    raise_on_conflict: bool = True,
) -> SnapshotWriteOutcome:
    """**追加**一条快照。永不 UPDATE 已有行。

    同一 ``(period, conclusion_key)``：

    * 内容哈希相同 → 判为重复拍，**不再插入**（同一份数据重跑两次不该
      在库里堆两行），返回 ``duplicate_of`` 指向已有那条；
    * 内容哈希不同 → 真冲突：**两份都留**，并按 ``raise_on_conflict``
      抛 ``SNAPSHOT_CONFLICT``。抛在插入**之后**是有意的：冲突要能被
      查得到，抛异常前就把新行回滚掉的话，事后只剩一条「看起来正常」的
      旧快照，恰恰把变化本身藏起来了。
    """
    init_align_snapshot_schema(conn)
    snap = snapshot if snapshot.content_hash else snapshot.with_hash()
    existing = conn.execute(
        f"SELECT snap_id, content_hash FROM {ALIGN_SNAPSHOT_TABLE} "
        "WHERE period = ? AND conclusion_key = ? ORDER BY snap_id",
        (snap.period, snap.conclusion_key),
    ).fetchall()
    same = [int(r["snap_id"]) for r in existing if r["content_hash"] == snap.content_hash]
    others = [int(r["snap_id"]) for r in existing if r["content_hash"] != snap.content_hash]

    if same and not others:
        return SnapshotWriteOutcome(snapshot=snap, snap_id=same[0], duplicate_of=same[0])

    payload = json.dumps(snap.to_dict(), ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        f"INSERT INTO {ALIGN_SNAPSHOT_TABLE} ("
        "  period, conclusion_key, channel, kind, order_no, content_hash,"
        "  contract_digest, contract_check_digest, index_version,"
        "  participant_total, payload_json, created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snap.period,
            snap.conclusion_key,
            snap.channel,
            snap.kind,
            snap.order_no,
            snap.content_hash,
            snap.contract_digest,
            snap.contract_check_digest,
            snap.index_version,
            snap.participant_total,
            payload,
            snap.created_at,
        ),
    )
    snap_id = int(cursor.lastrowid or 0)
    if others and raise_on_conflict:
        raise ReconqError(
            "SNAPSHOT_CONFLICT",
            f"结论 {snap.conclusion_key}（账期 {snap.period}）已有内容不同的快照 "
            f"{snap.content_hash[:12]}，与既有的 {'、'.join(str(i) for i in others)} 冲突；"
            f"两份都已保留（新增 snap_id={snap_id}）。"
            f"这说明这条差异在两次扫描之间变了 —— 请用 reconq verify --replay "
            f"{snap.conclusion_key} 复算，看清是口径改了还是数据改了。"
            f"不要手工删掉其中一份：结论单上可能正引用着它",
            channel=snap.channel,
        )
    return SnapshotWriteOutcome(
        snapshot=snap, snap_id=snap_id, conflict_with=tuple(others)
    )


def load_alignment_snapshots(
    conn: sqlite3.Connection,
    *,
    period: Optional[str] = None,
    conclusion_key: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[EvidenceSnapshot]:
    """查快照。按 ``snap_id`` 升序（= 拍摄先后）。"""
    init_align_snapshot_schema(conn)
    sql = f"SELECT * FROM {ALIGN_SNAPSHOT_TABLE} WHERE 1=1"
    params: list[Any] = []
    if period:
        sql += " AND period = ?"
        params.append(period)
    if conclusion_key:
        sql += " AND conclusion_key = ?"
        params.append(conclusion_key)
    sql += " ORDER BY snap_id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [EvidenceSnapshot.from_row(row) for row in conn.execute(sql, params)]


def latest_alignment_snapshot(
    conn: sqlite3.Connection, period: str, conclusion_key: str
) -> Optional[EvidenceSnapshot]:
    """取某条结论的**最新**一份快照（冲突时看最新那份）。"""
    rows = load_alignment_snapshots(conn, period=period, conclusion_key=conclusion_key)
    return rows[-1] if rows else None


def count_alignment_snapshots(
    conn: sqlite3.Connection, *, period: Optional[str] = None
) -> int:
    init_align_snapshot_schema(conn)
    if period:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {ALIGN_SNAPSHOT_TABLE} WHERE period = ?", (period,)
        ).fetchone()
    else:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {ALIGN_SNAPSHOT_TABLE}").fetchone()
    return int(row["n"])
