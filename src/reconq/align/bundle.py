"""归因证据包构造（Task 27）。

``ai_role=core`` 的**输入侧**
----------------------------

模型要给出可信的单边账成因，必须先拿到可信的证据。把整张账单表丢给模型
不是证据，是噪声：六路各两千行、字段几十列，模型既找不到该看的那一行，也
分不清「渠道本来就没有 HIS 单号」与「导出方把单号列导空了」。本模块把一条
差异要看的全部东西收成一个**结构化证据包**，边界收在一处、可复现、可脱敏、
可量体。

证据包由五块组成
----------------

=================  ==========================================================
块                 为什么必须有它
=================  ==========================================================
``diff_row``       差异行本身：金额、退款、时间、科室、结算方式、源文件行号。
                   没有行号的差异在结论单上没法回查，等于「据财务科说」
``candidate_rows`` **全部**候选行 + 每条的打分因子。歧义之所以是歧义，正
                   因为有第二条、第三条；只给「最像的那条」等于替人归并，
                   而那正是对齐器拒绝做的事
``contract_slice`` 该渠道契约里与这条差异相关的口径（笔数 / 金额合计 / 退费
                   红冲 / 跨账期切分…）。成因解释必须落在**当期口径**上：
                   拿上周的口径解释这周的差异，结论单签了字也对不上账
``period_window``  账期半开时间窗。跨账期落点的判据就是「这条 HIS 记录的
                   收费时间在不在 [start, end) 里」，不给边界模型无从判断
``channel_meta``   渠道、账单类型（``bill_type=ALL`` 与 ``=SUCCESS`` 看到的
                   是两份不同的账单）、源文件与证据切片指纹
=================  ==========================================================

另外两块是**让模型不至于凭空编**的东西：

* **近 N 账期同因历史**（:func:`query_cause_history`）：同一渠道同一形态在
  过去几期被怎么处理过、各多少次。先验频次来自台账里的**真实处置结论**，
  不是猜的；台账还没建起来时明确返回「不可用 + 原因」，**不返回 0**——
  「查不到」与「确实没有」在归因上是两回事，把前者报成后者会让模型把
  「首次出现」当成「历史上从没出过」。
* **脱敏白名单**（:data:`BUNDLE_ROW_FIELDS` + 复用
  :func:`reconq.align.snapshot.assert_deidentified`）：证据包会随结论单流转
  很久，患者姓名 / 身份证 / 手机号 / 住院号一律不落。

三条不可省的性质
----------------

1. **确定性序列化**：同一份数据构造两次，字节逐字节相同（:attr:`EvidenceBundle.digest`
   是规范化 JSON 的 sha256）。归因草稿要能被复算复核，「同样的输入给出不同
   的证据包」会让复算这件事本身失去意义。刻意**不含构造时刻**：那会让每次
   重跑都换一个哈希。
2. **裁剪必留痕**：候选行超上限时按分数保留 top N，并显式带
   ``truncated`` 与真实总数。静默截断会让模型以为「就这些候选」——
   而被截掉的那条可能正是唯一说得通的一条。
3. **体量可量**：:attr:`EvidenceBundle.char_count` / :attr:`line_count`
   量的是**真正发给模型的那段文本**（:meth:`EvidenceBundle.render`），不是
   内部字典的规模。网关调用审计元数据直接取 :meth:`audit_metadata`。

与源产品的关系
--------------
参考实现（``soda-core``）的 ``contract verify`` 把逐 check 的结果打到终端，
进程退出即消失，**没有任何一处把「这次失败的全部上下文」收成一份可复现的
东西交给下游**。本模块是这条主路径上的新建环节：源产品的主路径止于
「打印证据行」，本产品要在此之后再走一步「把证据交给模型解释成因」，
而那一步的前提就是证据包。

模块依赖方向：本模块依赖 ``contracts`` / ``sources.timezone`` / ``store`` /
``align`` 内的既有类型，**不反向依赖 ``engine.period``**（台账表名在函数内
局部 import，与 :mod:`reconq.align.taxonomy` 的做法一致）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..contracts.model import ContractYaml
from ..errors import ReconqError
from ..sources.timezone import HOSPITAL_TZ, PeriodWindow, period_window
from ..store.schema import BILL_TABLE
from .ambiguity import AmbiguityCandidate
from .index import normalize_key
from .matcher import STATUS_ZH, AlignRecord, MatchOutcome
from .scoring import Candidate as ScoredCandidate
from .scoring import ScoringConfig, default_scoring_config, score_pair
from .snapshot import SnapshotRef, assert_deidentified
from .trace import GLOBAL_CHANNEL, TRACE_KIND_ZH, TraceConclusion, shift_period

__all__ = [
    "BUNDLE_VERSION",
    "BUNDLE_ROW_FIELDS",
    "DEFAULT_CANDIDATE_CAP",
    "DEFAULT_HISTORY_PERIODS",
    "CHANNEL_ZH",
    "CHANNEL_BILL_TYPE",
    "KIND_ZH",
    "KIND_HIS_ONLY_ZH",
    "HIS_ONLY_KIND",
    "KIND_CHECK_TYPES",
    "KIND_CHECK_PATH_PREFIX",
    "ROLE_TO_CONTRACT_COLUMN",
    "EvidenceBundle",
    "build_evidence_bundle",
    "query_cause_history",
    "contract_slice",
    "assert_bundle_deidentified",
    "bundle_from_payload",
]

logger = logging.getLogger("reconq.align.bundle")

#: 证据包结构版本。结构变了就 bump：网关的调用审计、结论单附录用它判断
#: 「这份证据包能不能与上一版直接比较」。
BUNDLE_VERSION = "evidence-bundle/1"

#: 候选行默认保留条数。超出的按分数截断并显式标 ``truncated``。
DEFAULT_CANDIDATE_CAP = 5

#: 近 N 账期同因历史的默认窗口半径（往前看几期，不含本期）。
DEFAULT_HISTORY_PERIODS = 4

#: 渠道中文名。面板、结论单、证据包三处说同一句话，不各写一份。
CHANNEL_ZH: dict[str, str] = dict(
    {
        "wechat": "微信支付",
        "alipay": "支付宝",
        "unionpay": "银联",
        "self_service": "院内自助机",
        "his": "HIS 收费流水",
        "medical_ins": "医保结算",
        GLOBAL_CHANNEL: "跨渠道/跨账期",
    }
)

#: 各渠道取件时用的账单类型。**微信这一项直接决定模型看到的证据**：
#: ``bill_type=ALL`` 的账单含退款与关闭交易，``=SUCCESS`` 只有成功交易 ——
#: 同一笔退费在两份账单里根本不是同一个现象。支付宝走
#: ``bill.downloadurl.query``（无 bill_type 概念），院内两路是文件导出。
CHANNEL_BILL_TYPE: dict[str, str] = dict(
    {
        "wechat": "ALL",
        "alipay": "alipay.data.dataservice.bill.downloadurl.query",
        "unionpay": "院内文件导出",
        "self_service": "院内文件导出",
        "his": "只读库查询",
        "medical_ins": "院内文件导出",
    }
)

#: 「HIS 有渠道无」这一形态的机器名与中文说法。
#:
#: **刻意不并进** :data:`reconq.align.matcher.STATUS_ZH`：那一张表是**渠道行**
#: 的判定结果，而反向单边账挂在
#: :attr:`reconq.align.matcher.AlignAllResult.his_only` 上 —— 压根没有渠道行
#: 可判。它同样不能摊到某一路渠道名下：那一笔不属于任何一路渠道，
#: 挂到微信头上等于暗示「是微信漏了」。
HIS_ONLY_KIND = "his_only"
KIND_HIS_ONLY_ZH = "HIS 有渠道无"

#: 形态 → 中文说法。**三处合成为一份**（对齐器 :data:`STATUS_ZH`、连坐追查
#: :data:`~reconq.align.trace.TRACE_KIND_ZH`、本模块自有的两个非枚举值），
#: 不另写第四份 —— 同一形态在面板、台账、证据包上出现两种中文说法时，
#: 经办人会以为是两条不同的差异。

KIND_ZH: dict[str, str] = {
    **STATUS_ZH,
    **{kind: zh for kind, (zh, _advice) in TRACE_KIND_ZH.items()},
    HIS_ONLY_KIND: KIND_HIS_ONLY_ZH,
    # 台账 check_path 前缀兜底不到具体枚举时（check_path 写错了、或来自
    # 更早的版本）给出的中性说法。**不给它编一个具体成因**：形态判错话术
    # 就错，编一个比留空更危险。
    "unknown": "形态未登记",
    "口径差异": "口径差异",
}

#: 对齐/连坐状态 → 与之相关的口径算子。**只挑与该状态判据直接相关的**：
#: 把一份契约的全部 check 塞进证据包，模型会把「单号唯一性」当成
#: 「渠道有 HIS 无」的解释 —— 两者毫无因果关系。
KIND_CHECK_TYPES: dict[str, frozenset[str]] = dict(
    {
        HIS_ONLY_KIND: frozenset({"not_empty", "decimal_sum", "row_count"}),
        "channel_only": frozenset({"not_empty", "decimal_sum", "row_count", "matches_regex"}),
        "mismatch": frozenset({"decimal_sum", "decimal_scale", "refund_offset", "between"}),
        "ambiguous": frozenset({"unique", "refund_offset", "cross_period", "decimal_sum"}),
        "weak_pending": frozenset({"not_empty", "unique", "matches_regex", "decimal_sum"}),
        "cross_period": frozenset({"cross_period", "decimal_sum", "refund_offset"}),
        "duplicate_claim": frozenset({"unique", "not_empty", "decimal_sum"}),
        "duplicate_across_period": frozenset({"cross_period", "decimal_sum", "refund_offset"}),
        "duplicate_across_channel": frozenset({"unique", "not_empty", "decimal_sum"}),
        "amount_inconsistent": frozenset({"refund_offset", "decimal_sum", "decimal_scale"}),
    }
)

#: 对齐/连坐状态 → 台账里该类差异的 ``check_path`` 前缀。历史频次按它查。
#: 前缀与 :mod:`reconq.engine.period` 写入台账时的 ``check_path`` 对齐；
#: 歧义类复用 :data:`reconq.align.taxonomy.TAXONOMY_PATH_PREFIX` 那一份，
#: 因为歧义结论本来就是以成因条目落台账的。
KIND_CHECK_PATH_PREFIX: dict[str, str] = dict(
    {
        HIS_ONLY_KIND: "align.his_only",
        "channel_only": "align.channel_only",
        "mismatch": "align.amount_mismatch",
        "ambiguous": "taxonomy.ambiguity",
        "weak_pending": "align.weak_pending",
        "cross_period": "align.cross_period",
        "duplicate_claim": "align.duplicate_claim",
        "duplicate_across_period": "trace.duplicate_across_period",
        "duplicate_across_channel": "trace.duplicate_across_channel",
        "amount_inconsistent": "trace.amount_inconsistent",
    }
)

#: :class:`~reconq.align.matcher.AlignRecord` 的角色 → 契约列名。取自
#: :data:`reconq.store.bill_store.CHANNEL_COLUMN_MAP` 的键（那是**统一落库
#: 列名**，本表要的是**契约列名**，按渠道查那张表即可，故只登记角色名）。
ROLE_TO_CONTRACT_COLUMN: tuple[str, ...] = (
    "order_no",
    "secondary_no",
    "his_order_no",
    "amount",
    "refund",
    "trade_time",
    "trade_type",
    "department",
    "settle_type",
)

#: 证据包行**允许**出现的字段。白名单而非黑名单：黑名单要靠人记得列全所有
#: 不该落的字段（姓名？床号？就诊卡号？），漏一个就跟着结论单流转出去了。
#: 逐键核对见 :func:`assert_bundle_deidentified`。
BUNDLE_ROW_FIELDS: frozenset[str] = frozenset(
    {
        "ref",
        "source_file",
        "source_row_no",
        "record_id",
        "side",
        "period",
        "order_no",
        "his_order_no",
        "amount",
        "refund",
        "net_amount",
        "trade_time",
        "trade_type",
        "department",
        "settle_type",
        "is_refund",
        "cross_period",
        "score",
        "tier",
        "tier_zh",
        "score_source",
        "factors",
        "hints",
    }
)

#: 契约口径摘要在证据包里允许出现的字段。同样走白名单（``name`` 刻意
#: **不**在白名单里：它与患者姓名同名，宁可写成 ``check_name``）。
_CONTRACT_SLICE_FIELDS: frozenset[str] = frozenset(
    {"check_path", "check_name", "operator", "relevance", "why", "column"}
)

#: 打分因子在证据包里允许出现的字段。因子原字典用 ``name``（与患者姓名
#: 同名），这里改叫 ``factor``。
_FACTOR_FIELDS: frozenset[str] = frozenset(
    {"factor", "factor_zh", "available", "value", "weight", "contribution", "detail"}
)


# ─────────────────────────── 数据结构 ───────────────────────────


@dataclass(frozen=True)
class EvidenceBundle:
    """一条差异的完整归因证据包。

    :attr:`payload` 是**规范化后可序列化**的部分（不含构造时刻、不含
    体量统计），:attr:`digest` 是它的 sha256。体量统计量的是
    :meth:`render` 真正要发出去的那段文本。
    """

    payload: dict[str, Any]
    text: str
    digest: str

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def line_count(self) -> int:
        return self.text.count("\n") + 1 if self.text else 0

    def render(self) -> str:
        return self.text

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)

    def audit_metadata(self) -> dict[str, Any]:
        """网关调用审计元数据（Task 28 的 ``ModelGateway`` 直接取这里）。

        **不含证据正文**：审计要能回答「这次调用喂了多大一坨、依据哪条差异、
        证据包有没有被裁剪」，但正文进日志库就等于把渠道账单细节抄了第二份。
        """
        diff = self.payload.get("discrepancy", {})
        slice_info = self.payload.get("channel_contract_slice", {})
        return {
            "bundle_version": self.payload.get("bundle_version", ""),
            "bundle_digest": self.digest,
            "conclusion_key": self.payload.get("conclusion_key", ""),
            "period": self.payload.get("period", ""),
            "channel": self.payload.get("channel", ""),
            "discrepancy_kind": diff.get("kind", ""),
            "char_count": self.char_count,
            "line_count": self.line_count,
            "candidate_shown": self.payload.get("candidate_shown", 0),
            "candidate_total": self.payload.get("candidate_total", 0),
            "candidate_truncated": self.payload.get("candidate_truncated", False),
            "contract_check_shown": slice_info.get("check_shown", 0),
            "contract_check_total": slice_info.get("check_total", 0),
            "contract_check_truncated": slice_info.get("truncated", False),
            "contract_slice_available": slice_info.get("available", False),
            "history_available": self.payload.get("history", {}).get("available", False),
        }


# ─────────────────────────── 脱敏 ───────────────────────────


def assert_bundle_deidentified(bundle: EvidenceBundle) -> None:
    """核对证据包：行字段走白名单，且不含患者可识别信息。

    两道关卡，缺一不可：

    1. **白名单**（:data:`BUNDLE_ROW_FIELDS` / :data:`_CONTRACT_SLICE_FIELDS` /
       :data:`_FACTOR_FIELDS`）—— 挡住「顺手加了一个字段」。构造新字段的人
       不会想到去更新脱敏检查，但白名单会当场拦下。
    2. **值形态**（复用 :func:`reconq.align.snapshot.assert_deidentified`）——
       挡住「键名 innocuous 但值是身份证/手机号」。导出方把患者姓名写进
       订单备注列是很常见的事。
    """
    for row in _iter_row_dicts(bundle.payload):
        for key, allowed in (
            ("diff_row", BUNDLE_ROW_FIELDS),
            ("candidate_rows", BUNDLE_ROW_FIELDS),
            ("same_order_no_rows", BUNDLE_ROW_FIELDS),
        ):
            value = row.get(key)
            items = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                extra = set(item) - allowed
                if extra:
                    raise ReconqError(
                        "BUNDLE_FIELD_NOT_ALLOWED",
                        f"证据包 {key} 出现未登记字段 {sorted(extra)}；"
                        f"允许字段清单见 BUNDLE_ROW_FIELDS。"
                        f"新增字段请同步改白名单与脱敏检查 —— "
                        f"患者信息不落证据包（它会跟着结论单流转很久）",
                    )
                for factor in item.get("factors", []) or []:
                    if not isinstance(factor, dict):
                        continue
                    extra_factor = set(factor) - _FACTOR_FIELDS
                    if extra_factor:
                        raise ReconqError(
                            "BUNDLE_FIELD_NOT_ALLOWED",
                            f"证据包因子出现未登记字段 {sorted(extra_factor)}；"
                            f"允许字段清单见 _FACTOR_FIELDS",
                        )
    for entry in bundle.payload.get("contract_slice", {}).get("checks", []) or []:
        if not isinstance(entry, dict):
            continue
        extra = set(entry) - _CONTRACT_SLICE_FIELDS
        if extra:
            raise ReconqError(
                "BUNDLE_FIELD_NOT_ALLOWED",
                f"证据包契约口径摘要出现未登记字段 {sorted(extra)}；"
                f"允许字段清单见 _CONTRACT_SLICE_FIELDS",
            )
    assert_deidentified(bundle.payload, where="证据包")


def _iter_row_dicts(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """把证据包里所有「行」定位出来（含历史里的同单号行）。"""
    yield payload
    for key in ("diff_row", "candidate_rows", "same_order_no_rows"):
        value = payload.get(key)
        if isinstance(value, list):
            yield from (v for v in value if isinstance(v, dict))
        elif isinstance(value, dict):
            yield value


# ─────────────────────── 行与候选的规范化 ───────────────────────


def _record_row(record: AlignRecord, *, side: str, cross_period: bool = False) -> dict[str, Any]:
    """:class:`AlignRecord` → 证据包行（定点金额字符串，绝不 float）。"""
    return {
        "ref": record.ref,
        "source_file": record.source_file,
        "source_row_no": record.source_row_no,
        "record_id": record.his_order_no or record.order_no,
        "side": side,
        "period": record.period,
        "order_no": record.order_no,
        "his_order_no": record.his_order_no,
        "amount": str(record.amount),
        "refund": str(record.refund) if record.refund is not None else None,
        "net_amount": str(record.net_amount),
        "trade_time": record.trade_time.isoformat() if record.trade_time else None,
        "trade_type": record.trade_type,
        "department": record.department,
        "settle_type": record.settle_type,
        "is_refund": record.is_refund,
        "cross_period": cross_period,
    }


def _factor_rows(candidate: ScoredCandidate) -> list[dict[str, Any]]:
    """打分因子 → 证据包形态。``name`` 改叫 ``factor``（见 :data:`_FACTOR_FIELDS`）。"""
    rows: list[dict[str, Any]] = []
    for factor in candidate.factors:
        rows.append(
            {
                "factor": factor.name,
                "factor_zh": factor.zh,
                "available": factor.available,
                "value": str(factor.value) if factor.value is not None else None,
                "weight": str(factor.weight),
                "contribution": str(factor.contribution),
                "detail": factor.detail,
            }
        )
    return rows


def _candidate_row(
    candidate: AmbiguityCandidate,
    *,
    score: Optional[ScoredCandidate],
    score_source: str,
) -> dict[str, Any]:
    """一条候选 → 证据包行。

    候选是 :class:`~reconq.align.ambiguity.AmbiguityCandidate`（对齐器拒绝
    归并时给出的那一组）。它只有净额、没有 HIS 单号列，因此**要打分就得先
    还原成对齐行**——复用 :meth:`SnapshotRef.from_candidate` +
    :meth:`SnapshotRef.to_record`，与 Task 26 复算用的是**同一套还原口径**
    （两处各写一份，退费标记就会在「打分时算红冲、复算时算正常收费」上
    漂移，候选排序与复算结论对不上，而两边都不报错）。
    """
    row = {
        "ref": candidate.ref,
        "source_file": candidate.source_file,
        "source_row_no": candidate.row_no,
        "record_id": candidate.record_id,
        "side": "his",
        "period": candidate.period,
        "order_no": candidate.record_id,
        "his_order_no": candidate.record_id,
        "amount": candidate.amount,
        "refund": None,
        "net_amount": candidate.amount,
        "trade_time": candidate.trade_time or None,
        "trade_type": "",
        "department": candidate.department,
        "settle_type": candidate.settle_type,
        "is_refund": candidate.is_refund,
        "cross_period": candidate.cross_period,
    }
    if score is None:
        row.update({"score": None, "tier": "", "tier_zh": "", "score_source": score_source,
                    "factors": [], "hints": []})
        return row
    row.update(
        {
            "score": str(score.score),
            "tier": score.tier,
            "tier_zh": score.tier_zh,
            "score_source": score_source,
            "factors": _factor_rows(score),
            "hints": list(score.hints),
        }
    )
    return row


def _scored_candidates(
    channel_row: Optional[AlignRecord],
    candidates: Sequence[AmbiguityCandidate],
    config: ScoringConfig,
) -> dict[str, ScoredCandidate]:
    """给候选打分，返回 ``record_id+行号`` → :class:`ScoredCandidate`。

    键用 ``(record_id, source_file, row_no)`` 而不是单号：同号多候选正是
    本模块要处理的核心情形，用单号当键会把后面那条直接挤掉。
    """
    scored: dict[tuple[str, str, int], ScoredCandidate] = {}
    for candidate in candidates:
        his = SnapshotRef.from_candidate(candidate).to_record()
        if channel_row is None:
            continue
        result = score_pair(channel_row, his, config=config)
        scored[(candidate.record_id, candidate.source_file, candidate.row_no)] = result
    return scored


# ─────────────────────── 契约口径摘要 ───────────────────────


def _relevant_roles(rows: Sequence[dict[str, Any]]) -> set[str]:
    """从证据行推出「哪些角色有值」，据此挑契约里相关的列。"""
    roles: set[str] = set()
    for row in rows:
        for role in ROLE_TO_CONTRACT_COLUMN:
            if role == "refund":
                if row.get("refund"):
                    roles.add(role)
                continue
            if role == "order_no" or role == "amount":
                # 单号与金额是每条差异的必备上下文，永远算「有值」
                roles.add(role)
                continue
            if row.get(role):
                roles.add(role)
    return roles


def contract_slice(
    contract: Optional[ContractYaml],
    *,
    kind: str,
    channel: str,
    rows: Sequence[dict[str, Any]],
    cap: int = 12,
) -> dict[str, Any]:
    """挑出与这条差异相关的契约口径摘要。

    两条入选规则（都刻意收窄，宁可少给也不给无关口径）：

    1. **列级 check**：挂在本次证据里**有值的列**上。列没值（文件通道的
       账单普遍没有科室列）就不选 —— 让模型去解释一条它根本看不到的列，
       只会得到「科室不一致」这种凭空话术。
    2. **表级 check**：算子类型落在 :data:`KIND_CHECK_TYPES[kind]` 里。
       单号唯一性与「渠道有 HIS 无」没有因果关系，不选。

    ``contract`` 为 ``None`` 时**明说没契约**（``available=False`` + 原因），
    不返回空列表冒充「这条差异没有相关口径」。
    """
    if contract is None:
        return {
            "available": False,
            "reason": "未提供该渠道契约：证据包不带口径摘要，模型无法判断这条差异"
                      "是否与当期口径冲突。请传入 contracts/<channel>.yml 加载结果",
            "contract_name": "",
            "contract_version": "",
            "check_total": 0,
            "check_shown": 0,
            "truncated": False,
            "checks": [],
        }
    from ..store.bill_store import CHANNEL_COLUMN_MAP  # 局部 import：避免顶层循环

    roles = _relevant_roles(rows)
    # ``GLOBAL_CHANNEL``（连坐结论）不在映射表里：这类差异横跨多路渠道，
    # 拿任一路的列名去挑口径都会挑错。此时 ``wanted_columns`` 为空，只按
    # 算子类型挑表级口径 —— 宁少不错。
    column_map = CHANNEL_COLUMN_MAP.get(channel) or {}
    wanted_columns = {
        column_map[role] for role in roles if column_map.get(role)
    }
    wanted_types = KIND_CHECK_TYPES.get(kind, frozenset())

    selected: list[dict[str, Any]] = []
    total = 0
    for check, column in contract.iter_checks():
        relevance, why = _relevance_of(check, column, wanted_columns, wanted_types)
        total += 1
        if not relevance:
            continue
        selected.append(
            {
                "check_path": check.path_str,
                "check_name": check.display_name,
                "operator": check.type,
                "column": column.name if column is not None else "",
                "relevance": relevance,
                "why": why,
            }
        )
    truncated = len(selected) > cap
    shown = selected[:cap] if cap and cap > 0 else selected
    return {
        "available": True,
        "reason": "",
        "contract_name": contract.name,
        "contract_version": contract.version,
        "check_total": total,
        "check_shown": len(shown),
        "truncated": truncated,
        "checks": shown,
    }


def _relevance_of(
    check: Any,
    column: Any,
    wanted_columns: set[str],
    wanted_types: frozenset[str],
) -> tuple[str, str]:
    """判断一条 check 与本差异的相关性，返回 ``(relevance, why)``。

    ``relevance`` 为空串即「不选」。为什么用「相关」而不是「适用」：口径
    本身并不解释成因，它只是**约束**成因必须落在哪个范围内（退费红冲口径
    说清「已退金额不该计入已收」，跨账期口径说清「周一零点归哪期」）。
    """
    if column is not None and column.name in wanted_columns:
        if check.type in wanted_types:
            return "直接相关", f"该口径既落在本差异涉及的列 {column.name} 上，其判据也直接对应本差异形态"
        return "涉及本列", f"该口径落在本差异涉及的列 {column.name} 上"
    if column is None and check.type in wanted_types:
        return "直接相关", f"表级口径 {check.type} 的判据直接对应本差异形态"
    return "", ""


# ─────────────────────── 近 N 账期同因历史 ───────────────────────


def query_cause_history(
    conn: Optional[sqlite3.Connection],
    *,
    period: str,
    channel: str,
    kind: str,
    window_periods: int = DEFAULT_HISTORY_PERIODS,
    order_no: str = "",
) -> dict[str, Any]:
    """近 N 账期同因处置结论频次（**先验频次，来自台账真实结论**）。

    两个数据源各自都可能不可用，**不可用时明说原因而不是返回 0**：

    * 台账（``period_diff_items``）：按该形态的 ``check_path`` 前缀统计
      各期 ``disposition`` 分布。表还没建（这几期压根没扫过账）→ 不可用。
    * 账单表（``bill_rows``）：同一渠道单号在近 N 期的出现账期与笔数，
      走 ``ix_bill_order_period`` 复合索引。表不存在 → 不可用。

    「查不到」与「确实没有」在归因上是两回事：前者是**证据不足**（模型该
    说「本期无先例可依」），后者是**真没有**（模型可以更有把握）。把前者
    报成后者，模型会把「首次出现」说成「历史上从未发生过」。
    """
    if window_periods < 0:
        raise ReconqError(
            "BUNDLE_WINDOW_INVALID",
            f"window_periods={window_periods} 非法：历史窗口以账期为单位，不能为负"
            f"（0 表示只看本期，不查历史）",
        )
    periods = [shift_period(period, -offset) for offset in range(window_periods, 0, -1)]
    result: dict[str, Any] = {
        "available": False,
        "window_periods": window_periods,
        "periods": periods,
        "kind": kind,
        "channel": channel,
        "reasons": [],
        "same_cause": {"total": 0, "by_disposition": {}, "by_period": {}},
        "same_order_no": {"available": False, "occurrences": 0, "by_period": {}, "reasons": []},
    }
    if not periods:
        result["available"] = True
        result["reasons"].append("历史窗口为 0：按调用方要求不查往期")
        return result
    if conn is None:
        result["reasons"].append("未提供账单库连接：查不到历史处置结论（不等于没有先例）")
        result["same_order_no"]["reasons"].append("未提供账单库连接")
        return result

    result["same_cause"] = _cause_history_from_ledger(
        conn, periods=periods, channel=channel, kind=kind
    )
    result["same_order_no"] = _order_no_history_from_bills(
        conn, periods=periods, channel=channel, order_no=order_no
    )
    reasons = [r for r in (result["same_cause"].get("reason"), result["same_order_no"].get("reason")) if r]
    result["available"] = bool(result["same_cause"]["total"] or result["same_order_no"]["occurrences"])
    result["reasons"] = reasons
    return result


def _cause_history_from_ledger(
    conn: sqlite3.Connection, *, periods: Sequence[str], channel: str, kind: str
) -> dict[str, Any]:
    from ..engine.period import DIFF_TABLE  # 局部 import：align 不反向依赖 engine

    prefix = KIND_CHECK_PATH_PREFIX.get(kind)
    if not prefix:
        return {
            "total": 0,
            "by_disposition": {},
            "by_period": {},
            "reason": f"形态 {kind!r} 未登记台账 check_path 前缀：查不到历史处置结论",
        }
    if not _table_exists(conn, DIFF_TABLE):
        return {
            "total": 0,
            "by_disposition": {},
            "by_period": {},
            "reason": f"差异台账（{DIFF_TABLE}）尚未建立：这几期没有扫描过账，"
                      f"查不到先验（不等于没有先例）",
        }
    placeholders = ",".join("?" for _ in periods)
    rows = conn.execute(
        f"SELECT period, disposition, COUNT(*) AS n FROM {DIFF_TABLE} "
        f"WHERE period IN ({placeholders}) AND check_path LIKE ? "
        "GROUP BY period, disposition ORDER BY period, disposition",
        [*periods, f"{prefix}%"],
    ).fetchall()
    by_period: dict[str, int] = {}
    by_disposition: dict[str, int] = {}
    for row in rows:
        by_period[row["period"]] = by_period.get(row["period"], 0) + int(row["n"])
        by_disposition[row["disposition"]] = by_disposition.get(row["disposition"], 0) + int(row["n"])
    total = sum(by_disposition.values())
    return {
        "total": total,
        "by_disposition": dict(sorted(by_disposition.items())),
        "by_period": dict(sorted(by_period.items())),
        "reason": "" if total else f"近 {len(periods)} 期台账里没有同类差异的处置结论（确实无先例）",
    }


def _order_no_history_from_bills(
    conn: sqlite3.Connection, *, periods: Sequence[str], channel: str, order_no: str
) -> dict[str, Any]:
    key = normalize_key(order_no)
    if not key:
        return {
            "available": False,
            "occurrences": 0,
            "by_period": {},
            "reason": "无单号可查（该差异靠金额 + 时间窗定位，历史按单号查不到）",
        }
    if not _table_exists(conn, BILL_TABLE):
        return {
            "available": False,
            "occurrences": 0,
            "by_period": {},
            "reason": f"账单表（{BILL_TABLE}）不存在：查不到往期出现情况",
        }
    placeholders = ",".join("?" for _ in periods)
    rows = conn.execute(
        f"SELECT period, COUNT(*) AS n FROM {BILL_TABLE} "
        f"WHERE channel = ? AND channel_order_no = ? COLLATE NOCASE "
        f"AND period IN ({placeholders}) GROUP BY period ORDER BY period",
        [channel, key, *periods],
    ).fetchall()
    by_period = {row["period"]: int(row["n"]) for row in rows}
    total = sum(by_period.values())
    return {
        "available": True,
        "occurrences": total,
        "by_period": dict(sorted(by_period.items())),
        "reason": "" if total else f"近 {len(periods)} 期该渠道未再出现此单号",
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# ─────────────────────────── 构造 ───────────────────────────


@dataclass(frozen=True)
class _Discrepancy:
    """把三类差异来源归一成同一形状。"""

    kind: str
    period: str
    channel: str
    order_no: str
    detail: str
    diff_amount: str
    channel_row: Optional[AlignRecord] = None
    his_row: Optional[AlignRecord] = None
    candidates: tuple[AmbiguityCandidate, ...] = ()
    source_files: tuple[str, ...] = ()


def _normalize(discrepancy: Any) -> _Discrepancy:
    """接受 :class:`MatchOutcome` / :class:`TraceConclusion` / 台账条目。"""
    if isinstance(discrepancy, MatchOutcome):
        files = tuple(
            sorted(
                {
                    r.source_file
                    for r in (discrepancy.channel_record, discrepancy.his_record)
                    if r is not None and r.source_file
                }
                | {c.source_file for c in discrepancy.candidates if c.source_file}
            )
        )
        return _Discrepancy(
            kind=discrepancy.status,
            period=discrepancy.key.period,
            channel=discrepancy.key.channel,
            order_no=discrepancy.key.order_no,
            detail=discrepancy.detail,
            diff_amount=discrepancy.diff_amount,
            channel_row=discrepancy.channel_record,
            his_row=discrepancy.his_record,
            candidates=tuple(discrepancy.candidates),
            source_files=files,
        )
    if isinstance(discrepancy, TraceConclusion):
        return _Discrepancy(
            kind=discrepancy.kind,
            period=discrepancy.period,
            channel=GLOBAL_CHANNEL,
            order_no=discrepancy.order_no,
            detail=discrepancy.detail,
            diff_amount=discrepancy.diff_amount,
            source_files=(),
        )
    # 台账条目（engine.period.DiffItem）：本地 import 避免顶层循环
    from ..engine.period import DiffItem

    if isinstance(discrepancy, DiffItem):
        return _Discrepancy(
            kind=discrepancy.ambiguity_kind or _kind_from_check_path(discrepancy.check_path),
            period=discrepancy.period,
            channel=discrepancy.channel,
            order_no=_order_no_from_check_path(discrepancy.check_path),
            detail=discrepancy.message,
            diff_amount=discrepancy.diff_amount or "",
            source_files=(discrepancy.source_file,) if discrepancy.source_file else (),
        )
    raise ReconqError(
        "BUNDLE_INPUT_INVALID",
        f"证据包只接受对齐结论（MatchOutcome）、连坐结论（TraceConclusion）"
        f"或差异台账条目（DiffItem），得到 {type(discrepancy).__name__}",
    )


def _kind_from_check_path(check_path: str) -> str:
    """台账 ``check_path`` → 形态名。取登记过的最长前缀。"""
    for kind, prefix in sorted(
        KIND_CHECK_PATH_PREFIX.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        if check_path.startswith(prefix):
            return kind
    return check_path.split(".", 1)[0] or "unknown"


def _order_no_from_check_path(check_path: str) -> str:
    """台账 ``check_path`` 尾部的单号（``taxonomy.ambiguity:<单号>``）。"""
    return check_path.rsplit(":", 1)[-1] if ":" in check_path else ""


def _slice_fingerprint(rows: Sequence[dict[str, Any]]) -> str:
    """证据切片的 sha256（**按行定位算，不读文件**）。

    刻意不用文件内容哈希：渠道账单文件会被下一次取件覆盖，同一份差异在
    两次构造之间读到的文件内容可能已经变了，而**当时比的是哪几行**这件事
    只由行号与金额决定。指纹钉住的是「看了哪几行」，这正是结论单要回答的
    问题；文件内容哈希回答的是另一个问题，且会让「同一份数据两次构造字节
    相同」这条不变量在渠道重发账单时失效。
    """
    parts = sorted(
        f"{row.get('source_file', '')}:{row.get('source_row_no', 0)}:"
        f"{row.get('record_id', '')}:{row.get('net_amount', '')}"
        for row in rows
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def build_evidence_bundle(
    discrepancy: Any,
    *,
    contract: Optional[ContractYaml] = None,
    window: Optional[PeriodWindow] = None,
    conn: Optional[sqlite3] = None,
    history_periods: int = DEFAULT_HISTORY_PERIODS,
    candidate_cap: int = DEFAULT_CANDIDATE_CAP,
    scoring_config: Optional[ScoringConfig] = None,
    bill_type: str = "",
) -> EvidenceBundle:
    """给一条差异构造归因证据包。

    ``discrepancy`` 接受三类来源（见 :func:`_normalize`）：对齐器给出的
    :class:`~reconq.align.matcher.MatchOutcome`、连坐追查的
    :class:`~reconq.align.trace.TraceConclusion`、以及台账里的一行
    :class:`~reconq.engine.period.DiffItem`。**台账条目那一路拿不到候选行**
    （台账只存结论不存证据），此时 ``candidate_rows`` 为空且
    ``candidate_source`` 明说是「台账条目未带候选」—— 不静默返回空列表
    冒充「没有候选」。
    """
    norm = _normalize(discrepancy)
    if candidate_cap < 0:
        raise ReconqError(
            "BUNDLE_WINDOW_INVALID",
            f"candidate_cap={candidate_cap} 非法：候选保留上限不能为负",
        )
    cfg = scoring_config or default_scoring_config()
    win = window or (period_window(norm.period) if norm.period else None)

    # —— 差异行 ——
    diff_row: dict[str, Any] = {}
    diff_side = ""
    if norm.channel_row is not None:
        diff_row = _record_row(
            norm.channel_row,
            side="channel",
            cross_period=bool(
                win is not None
                and norm.channel_row.trade_time is not None
                and win.is_cross_period(norm.channel_row.trade_time)
            ),
        )
        diff_side = "channel"
    elif norm.his_row is not None:
        diff_row = _record_row(
            norm.his_row,
            side="his",
            cross_period=bool(
                win is not None
                and norm.his_row.trade_time is not None
                and win.is_cross_period(norm.his_row.trade_time)
            ),
        )
        diff_side = "his"

    # —— 候选行：打分 → 排序 → 裁剪（留痕） ——
    scored = _scored_candidates(norm.channel_row, norm.candidates, cfg)
    rows: list[dict[str, Any]] = []
    for candidate in norm.candidates:
        hit = scored.get((candidate.record_id, candidate.source_file, candidate.row_no))
        rows.append(
            _candidate_row(
                candidate,
                score=hit,
                score_source="scoring.score_pair" if hit is not None else "input",
            )
        )
    if norm.his_row is not None and not norm.candidates:
        rows.append(
            _record_row(
                norm.his_row,
                side="his",
                cross_period=bool(
                    win is not None
                    and norm.his_row.trade_time is not None
                    and win.is_cross_period(norm.his_row.trade_time)
                ),
            )
        )
        rows[-1].update(
            {"score": None, "tier": "", "tier_zh": "", "score_source": "claimed_row",
             "factors": [], "hints": []}
        )
    candidate_total = len(rows)
    # 有分数才按分数排：分数全空时按输入顺序排，并明说排的是什么 ——
    # 无声地「按行号排」会被读成「按可信度排」。
    if any(row["score"] for row in rows):
        rows.sort(key=lambda r: Decimal(r["score"] or "0"), reverse=True)
        rank_basis = "score"
    else:
        rank_basis = "input_order"
    truncated = candidate_total > candidate_cap if candidate_cap else False
    shown = rows[:candidate_cap] if candidate_cap else rows

    history = query_cause_history(
        conn,
        period=norm.period,
        channel=norm.channel,
        kind=norm.kind,
        window_periods=history_periods,
        order_no=norm.order_no,
    )
    same_order_rows = (
        _same_order_no_rows(
            conn, history, channel=norm.channel, order_no=norm.order_no
        )
        if conn is not None
        else []
    )

    rows_for_slice = ([diff_row] if diff_row else []) + shown
    slice_info = contract_slice(
        contract,
        kind=norm.kind,
        channel=norm.channel,
        rows=rows_for_slice,
    )

    source_files = list(norm.source_files)
    for row in rows_for_slice:
        name = row.get("source_file") or ""
        if name and name not in source_files:
            source_files.append(name)

    payload: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "conclusion_key": f"{norm.channel}:{norm.period}:{normalize_key(norm.order_no)}",
        "period": norm.period,
        "channel": norm.channel,
        "discrepancy": {
            "kind": norm.kind,
            "kind_zh": KIND_ZH.get(norm.kind, norm.kind),
            "detail": norm.detail,
            "diff_amount": norm.diff_amount,
            "needs_attribution": True,
        },
        "diff_row": diff_row,
        "diff_side": diff_side,
        "candidate_source": _candidate_source(discrepancy, norm),
        "candidate_rows": shown,
        "candidate_total": candidate_total,
        "candidate_shown": len(shown),
        "candidate_truncated": truncated,
        "candidate_cap": candidate_cap,
        "candidate_rank_basis": rank_basis,
        "channel_contract_slice": slice_info,
        "period_window": _window_dict(win),
        "channel_meta": {
            "channel": norm.channel,
            "channel_zh": CHANNEL_ZH.get(norm.channel, norm.channel),
            "bill_type": bill_type or CHANNEL_BILL_TYPE.get(norm.channel, ""),
            "bill_type_source": "caller" if bill_type else "channel_default",
            "source_files": sorted(source_files),
            "source_file_count": len(source_files),
            "evidence_slice_sha256": _slice_fingerprint(rows_for_slice),
        },
        "same_order_no_rows": same_order_rows,
        "history": history,
        "scoring_config_fingerprint": cfg.fingerprint(),
    }

    text = _render_payload(payload)
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    bundle = EvidenceBundle(payload=payload, text=text, digest=digest)
    assert_bundle_deidentified(bundle)
    return bundle


def _candidate_source(discrepancy: Any, norm: _Discrepancy) -> str:
    """候选行**来自哪里**，以及为什么这里可能没有候选。

    三种「没有候选」在业务上完全不同，混作一种就会让模型误判系统的
    查证程度：

    * ``align.no_candidate_found`` —— 对齐器**查过**、确实没有第二条记录
      （单边账的常态）。这是结论。
    * ``ledger_row_without_candidates`` —— 台账条目压根不带证据行。
      这是**证据缺口**，系统没查过。
    * ``trace.conclusion_nodes_unavailable`` —— 连坐结论的链上节点由
      :mod:`reconq.align.trace` 的调用方掌握，不走本模块。
    """
    if norm.candidates:
        return "align.candidates"
    if isinstance(discrepancy, TraceConclusion):
        return "trace.conclusion_nodes_unavailable"
    if isinstance(discrepancy, MatchOutcome):
        return "align.no_candidate_found"
    return "ledger_row_without_candidates"


def _window_dict(win: Optional[PeriodWindow]) -> dict[str, Any]:
    if win is None:
        return {
            "available": False,
            "reason": "无账期或未给时间窗：跨账期落点的判据缺失，模型不得自行假定边界",
            "period": "",
            "start": "",
            "end": "",
            "tz": "",
            "half_open": True,
        }
    return {
        "available": True,
        "reason": "",
        "period": win.period,
        "start": win.start.isoformat(),
        "end": win.end.isoformat(),
        "tz": str(win.tz or HOSPITAL_TZ),
        "half_open": True,
    }


def _same_order_no_rows(
    conn: sqlite3.Connection, history: dict[str, Any], *, channel: str, order_no: str
) -> list[dict[str, Any]]:
    """历史里该单号出现的行（逐条带行号，可回查）。

    频次在 :func:`query_cause_history` 里已经数过一遍，这里把**行本身**取
    回来：模型判「这一笔是不是上周那笔的延续」需要看到那几行的金额与时间，
    只给一个「上周出现过 1 次」的计数等于让它猜。
    """
    by_period = history.get("same_order_no", {}).get("by_period", {})
    key = normalize_key(order_no)
    if not by_period or not key or not _table_exists(conn, BILL_TABLE):
        return []
    periods = list(by_period)
    placeholders = ",".join("?" for _ in periods)
    from .matcher import record_from_row_dict

    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        f"SELECT * FROM {BILL_TABLE} WHERE channel = ? AND channel_order_no = ? COLLATE NOCASE "
        f"AND period IN ({placeholders}) ORDER BY period, source_file, source_row_no",
        [channel, key, *periods],
    ):
        record = record_from_row_dict({k: row[k] for k in row.keys()})
        rows.append(_record_row(record, side="channel" if record.channel != "his" else "his"))
    return rows


# ─────────────────────────── 渲染 ───────────────────────────


def _render_payload(payload: dict[str, Any]) -> str:
    """把证据包渲染成给模型看的那段文本。

    体量统计量的就是这里的输出。刻意**不加时间戳、不加序号**：同一份数据
    渲染两次必须逐字节相同，否则「归因可复现」这条不变量在最后一步失效。
    """
    diff = payload.get("discrepancy", {})
    lines: list[str] = [
        f"差异：{diff.get('kind_zh', '')}（{diff.get('kind', '')}）"
        + (f"，差额 {diff['diff_amount']} 元" if diff.get("diff_amount") else ""),
        f"渠道：{payload['channel_meta']['channel_zh']}（{payload['channel']}）"
        f"，账期 {payload['period']}，账单类型 {payload['channel_meta']['bill_type']}",
    ]
    if diff.get("detail"):
        lines.append(f"系统判定：{diff['detail']}")

    win = payload.get("period_window", {})
    if win.get("available"):
        lines.append(f"账期时间窗：[{win['start']}, {win['end']}) 半开（周一 00:00 归新账期）")
    else:
        lines.append(f"账期时间窗：不可用（{win.get('reason', '')}）")

    row = payload.get("diff_row") or {}
    if row:
        lines.append(
            f"差异行：{row['ref']} 金额 {row['amount']} 元"
            + (f"（退款 {row['refund']} 元，净额 {row['net_amount']} 元）" if row.get("refund") else "")
            + (f"，时间 {row['trade_time']}" if row.get("trade_time") else "")
            + (f"，科室 {row['department']}" if row.get("department") else "")
            + (f"，结算方式 {row['settle_type']}" if row.get("settle_type") else "")
            + ("，该行收费时间落在账期窗口之外" if row.get("cross_period") else "")
        )
    else:
        lines.append("差异行：未随结论传来（台账条目不带证据行，见 candidate_source 说明）")

    candidates = payload.get("candidate_rows", [])
    if candidates:
        head = (
            f"候选行：共 {payload['candidate_total']} 条，按"
            f"{'打分' if payload['candidate_rank_basis'] == 'score' else '原有顺序'}"
            f"给出 {len(candidates)} 条"
        )
        if payload.get("candidate_truncated"):
            head += f"（已截断，另有 {payload['candidate_total'] - len(candidates)} 条未展示）"
        lines.append(head)
        for cand in candidates:
            bits = [f"{cand['ref']} 金额 {cand['amount']} 元"]
            if cand.get("trade_time"):
                bits.append(f"时间 {cand['trade_time']}")
            if cand.get("department"):
                bits.append(f"科室 {cand['department']}")
            if cand.get("settle_type"):
                bits.append(f"结算方式 {cand['settle_type']}")
            if cand.get("is_refund"):
                bits.append("红冲行")
            if cand.get("cross_period"):
                bits.append("落在账期窗口外")
            if cand.get("score"):
                bits.append(f"{cand['tier_zh']} {cand['score']} 分")
            lines.append("  · " + "，".join(bits))
            for factor in cand.get("factors", []) or []:
                detail = factor.get("detail") or ""
                if not factor.get("available"):
                    lines.append(
                        f"      · {factor['factor_zh']}：不可用（{detail or '数据不全'}，不计入分数）"
                    )
                else:
                    # detail 必须一起给：只给「金额差 0 × 0.45 = 0.00」而不说
                    # 「差 78.00 元，超容差 0.01 元」，模型看到的就只是一个
                    # 没有来由的 0 分 —— 它会把这理解成「金额对不上但原因未知」，
                    # 而真实成因（超容差 vs 字段缺失）恰恰在这里。
                    tail = f"（{detail}）" if detail else ""
                    lines.append(
                        f"      · {factor['factor_zh']}：{factor['value']} × {factor['weight']}"
                        f" = {factor['contribution']}{tail}"
                    )
            for hint in cand.get("hints", []) or []:
                lines.append(f"      · 线索：{hint}")
    else:
        source = payload["candidate_source"]
        why = {
            "align.no_candidate_found": "对齐器已查过，HIS 侧本账期确无第二条可候选记录",
            "ledger_row_without_candidates": "台账条目不带证据行，**系统未查证过是否存在候选**",
            "trace.conclusion_nodes_unavailable": "连坐结论的链上节点不在本证据包内",
        }.get(source, "来源未登记")
        lines.append(f"候选行：无（{source} —— {why}）")

    slice_info = payload.get("channel_contract_slice", {})
    if slice_info.get("available"):
        head = (
            f"本渠道相关口径：{slice_info['check_shown']}/{slice_info['check_total']} 条"
            f"（契约 {slice_info['contract_name']} {slice_info['contract_version']}）"
        )
        if slice_info.get("truncated"):
            head += "，已截断"
        lines.append(head)
        for entry in slice_info["checks"]:
            column = f"[{entry['column']}]" if entry.get("column") else "[表级]"
            lines.append(
                f"  · {column} {entry['check_path']}（{entry['check_name']}，"
                f"算子 {entry['operator']}）—— {entry['why']}"
            )
    else:
        lines.append(f"本渠道相关口径：不可用（{slice_info.get('reason', '')}）")

    history = payload.get("history", {})
    same_cause = history.get("same_cause", {})
    if history.get("available"):
        disposition = "、".join(
            f"{k} {v} 次" for k, v in (same_cause.get("by_disposition") or {}).items()
        )
        lines.append(
            f"同因历史：近 {history['window_periods']} 期（{history['periods'][0]}…"
            f"{history['periods'][-1]}）同形态差异 {same_cause.get('total', 0)} 条"
            + (f"（{disposition}）" if disposition else "")
        )
        by_period = history.get("same_order_no", {}).get("by_period", {})
        if by_period:
            lines.append(
                "同单号往期出现："
                + "、".join(f"{p} {n} 行" for p, n in by_period.items())
            )
    else:
        reasons = "；".join(history.get("reasons", [])) or "查不到历史处置结论"
        lines.append(f"同因历史：不可用（{reasons}）——**不可据此断言「历史上从未发生过」**")

    return "\n".join(lines)


def bundle_from_payload(payload: Mapping[str, Any]) -> EvidenceBundle:
    """从落盘的 ``payload`` 重建 :class:`EvidenceBundle`（Task 34 离线回放）。

    离线回放集把证据包**存成 JSON 文件**而不是每次重跑对齐器重建：重建
    依赖当期账单行与候选打分配置，而这两样都会随数据与配置漂移 ——
    回放集必须**逐字节固定**，否则「改了提示词之后是变好还是变坏」这个
    问题会混进「证据包本身也变了」这个说不清的变量里。

    正文按 :func:`_render_payload` **重渲染**而不是把 ``text`` 一起存下来：
    同一份 payload 渲染两次必须逐字节相同（该函数刻意不加时间戳与序号），
    所以重渲染是幂等的；而把正文存一份会引入「payload 与 text 对不上」
    这种只有比对时才会暴露的漂移。
    """
    data = dict(payload)
    text = _render_payload(data)
    digest = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    bundle = EvidenceBundle(payload=data, text=text, digest=digest)
    assert_bundle_deidentified(bundle)
    return bundle
