"""跨渠道实体对齐器（Task 21 · 本产品最核心的新建模块）。

三方对齐是什么
--------------
对账真正要回答的问题不是「这笔钱在不在」，而是「**渠道说的这一笔，等于
HIS 记的那几笔里的哪几笔**」。三方是：

* **渠道订单号** —— 微信/支付宝/银联/自助机账单里的商户订单号；
* **HIS 收费记录** —— 收费流水里的 ``his_order_no``（权威标识）；
* **账期时间窗** —— ``[周一 00:00, 下周一 00:00)``，决定一笔记录算哪期。

对不上的行分两个方向，**都是单边账**：

* ``渠道有 HIS 无``：渠道收了钱，HIS 没这笔收费 → 漏收费 / 挂账未记；
* ``HIS 有渠道无``：HIS 记了收费，渠道侧没这笔 → 漏收 / 线下收现未入渠道。

只报一个方向的工具是有偏的：只报「渠道有 HIS 无」会漏掉现金与医保代收，
只报另一方向会漏掉漏收费。两边都给、且给出各自的行与金额，才是能签字的结论。

**为什么不静默择一（本模块最重要的一条不变量）**
------------------------------------------------
一个订单号命中 2 条及以上 HIS 记录时，本产品**不选**。医院里能造成这种
一对多的真实业务有四种：一码付拆笔（住院预交金按费用项目拆）、合并支付
（家属一次刷卡付多个单）、跨账期落点（渠道已入账、HIS 落在周一零点之后）、
退费与原单并存。四种成因的**正确处理各不相同**（有的应合并、有的应分摊、
有的根本不该动），系统侧没有任何字段能区分它们。猜错的代价是整段金额被归
到错的费用项目上，且方向可能连错好几天——比留待人工确认危险得多。

弱匹配同理：单号缺失时靠「金额 + 账期时间窗」找候选，**即使唯一命中也不
认领**，只标「弱匹配待人工」。理由是唯一命中只说明「碰巧对得上」，而院内
一天里同金额的缴费有几十笔；把碰巧对上的当对上，等于把不确定性藏进结论单。

源产品对照
----------
参考实现（``soda-core``）**没有这一层**：它的 check 只在**单个 dataset 内**
检查列值，跨表只有 ``relationships`` 这种声明式的存在性约束，既不做实体
对齐、也不报单边账，更不解释成因。本模块是围绕医院三方对账**新建**的，
算法与判据均为本产品所有；沿用的只是「逐行流式 + 证据定位到源行」这条
主路径纪律（见 ``sources/his_db.py`` 与 ``engine/common.Evidence``）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

from ..engine.common import to_bool
from ..sources.timezone import PeriodWindow
from ..store.bill_store import to_amount_text
from .ambiguity import (
    AmbiguityCandidate,
    AmbiguityKind,
    AmbiguityVerdict,
    classify_ambiguity,
)

__all__ = [
    "AlignKey",
    "AlignRecord",
    "MatchStatus",
    "MatchIndex",
    "MatchOutcome",
    "AlignAllResult",
    "AlignResult",
    "build_index",
    "align_channel",
    "align_all",
    "record_from_bill",
    "records_from_bill",
    "record_from_row_dict",
    "STATUS_ZH",
]

logger = logging.getLogger("reconq.align.matcher")

#: 权威侧：只有 HIS 侧是「权威实体」，渠道侧一律是待核对方。
AUTHORITATIVE_CHANNEL = "his"

#: 交易类型列里**明确写着退费/红冲**的写法。六路的 ``trade_type`` 含义
#: 不统一（微信是 ``SUCCESS``/``REFUND``，银联是「对公/对私」，自助机直接
#: 放退费标志），所以这张表只收明确表意的词，不做模式推断。
_REFUND_TYPE_TOKENS = frozenset({"退费", "红冲", "退款", "退款成功", "refund"})


def _trade_type_is_refund(trade_type: str) -> bool:
    """交易类型列是否表示退费 / 红冲。

    两条判据，先布尔后词表：

    * ``is_refund`` 是 ``true``/``是``/``1`` 这类布尔写法时，**复用
      :func:`engine.common.to_bool`** 而不是自己认一份词表。两处判据
      漂移的后果很具体：退费红冲口径判「过」、对齐器把同一条当正常收费，
      同一个字段在两处得出相反结论，而台账上只会看到后者。
    * 剩下认不出的（返回 ``None``）再看词表 —— 渠道侧的 ``REFUND``
      这类枚举值落在这一支。
    * 都不命中就是没命中，按正常收费走。歧义分类会把它如实报成一对多，
      由人判断；在这里猜一次「看着像退费」只会把对的判成错的。
    """
    text = str(trade_type or "").strip()
    if not text:
        return False
    flag = to_bool(text)
    if flag is not None:
        return flag
    return text.lower() in _REFUND_TYPE_TOKENS


#: 金额比较容差（元）。弱匹配靠金额找候选，容差比口径 check 略宽一点
#: （院内导出常有分位抹零），但**不**大到能跨过「同一天几十笔同额缴费」。
WEAK_MATCH_TOLERANCE = Decimal("0.01")


class MatchStatus(str, Enum):
    """一行渠道账单的对齐结果。"""

    #: 单号精确命中唯一一条 HIS 记录
    MATCHED = "matched"
    #: 单号精确命中，但金额与 HIS 侧不一致（对上了但对不平）
    MISMATCH = "mismatch"
    #: 渠道有、HIS 无
    CHANNEL_ONLY = "channel_only"
    #: 单号命中多条 HIS 记录 —— 拒绝归并，交人工
    AMBIGUOUS = "ambiguous"
    #: 单号缺失/未命中，靠金额 + 时间窗唯一命中 —— 仍不认领
    WEAK_PENDING = "weak_pending"
    #: 单号命中唯一一条，但那条记录的收费时间落在本账期窗口之外
    CROSS_PERIOD = "cross_period"
    #: 单号命中的那条 HIS 记录**已被前面的渠道行认领**（通常是同一笔被两路
    #: 渠道各报一次，或导出方把一笔拆成两行同号）—— 报告而不二次认领
    DUPLICATE_CLAIM = "duplicate_claim"


#: 状态 → 中文。面板与终端共用一处说法。
STATUS_ZH: dict[str, str] = {
    MatchStatus.MATCHED.value: "已对齐",
    MatchStatus.MISMATCH.value: "已对齐但金额不符",
    MatchStatus.CHANNEL_ONLY.value: "渠道有 HIS 无",
    MatchStatus.AMBIGUOUS.value: "歧义待人工",
    MatchStatus.WEAK_PENDING.value: "弱匹配待人工",
    MatchStatus.CROSS_PERIOD.value: "跨账期落点待确认",
    MatchStatus.DUPLICATE_CLAIM.value: "重复认领待人工",
}


@dataclass(frozen=True)
class AlignKey:
    """实体标识：渠道 + 单号类型 + 单号 + 账期。

    **为什么键里必须有账期**：同一个商户订单号在自助机重试、或跨月退费时会
    重复出现。只用「渠道 + 单号」当键会把上周那笔一起拉进候选，对齐器就
    会在自己制造的歧义上打转。带账期后，跨账期的同一单号由
    ``cross_period`` 形态单独交代，而不是变成一对多的噪声。
    """

    channel: str
    order_no_type: str
    order_no: str
    period: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "order_no_type": self.order_no_type,
            "order_no": self.order_no,
            "period": self.period,
        }

    def render(self) -> str:
        return f"{self.channel}/{self.order_no_type}={self.order_no}@{self.period}"


@dataclass(frozen=True)
class AlignRecord:
    """参与对齐的一行。字段与 ``bill_rows`` 表列名一致，构造自
    :func:`record_from_row_dict`，使对齐器不依赖数据库层。
    """

    channel: str
    period: str
    order_no: str
    amount: Decimal = Decimal("0")
    refund: Optional[Decimal] = None
    trade_time: Optional[datetime] = None
    trade_type: str = ""
    department: str = ""
    settle_type: str = ""
    his_order_no: str = ""
    source_file: str = ""
    source_row_no: int = 0
    ch_id: int = 0
    bill_date: str = ""

    @property
    def is_refund(self) -> bool:
        """该行是否带退费。

        两条判据，缺一不可：

        1. ``refund`` 列非零 —— 微信/支付宝账单把退款记在**原单行**的退款列
           上，金额是**正数**（见 ``contracts/wechat.yml`` 的 refund 列定义）。
           **不做符号归一**：微信侧退款额是正的、HIS 侧红冲记录本身金额即为
           负，把负号在这里抹掉会让「退款 86 元」变成「退款 -86 元」，
           ``net_amount`` 随之加回原额，一笔退费凭空消失。
        2. ``trade_type`` 列写着退费/红冲 —— HIS 流水有独立的 ``is_refund``
           列（落库后进 ``trade_type``），那一行金额是**红冲后的净额**，
           退款列是空的。**只看第 1 条会把这种行判成正常收费**，于是
           「原单 128 + 红冲 42」这对候选被误判成普通的一码付拆多笔，
           建议话术把排查方向直接带偏。
        """
        if self.refund is not None and self.refund != Decimal("0"):
            return True
        return _trade_type_is_refund(self.trade_type)

    @property
    def net_amount(self) -> Decimal:
        """净收款：订单金额扣掉本行退款额。

        微信/支付宝账单把退款记在**原单行**的退款列上（一行同时有订单金额
        与退款金额），所以这里是减不是加。HIS 侧红冲记录本身金额即为负，
        两边净额因此同向，「渠道 128 - 退款 86 = 42」能与「HIS -42」对上。
        """
        return self.amount - (self.refund if self.refund else Decimal("0"))

    @property
    def ref(self) -> str:
        return f"{self.source_file}:{self.source_row_no}" if self.source_file else f"#{self.source_row_no}"

    def to_candidate(self, window: Optional[PeriodWindow] = None) -> AmbiguityCandidate:
        """转成候选行，供 :mod:`reconq.align.ambiguity` 渲染。"""
        cross = False
        if window is not None and self.trade_time is not None:
            cross = window.is_cross_period(self.trade_time)
        return AmbiguityCandidate(
            row_no=self.source_row_no,
            source_file=self.source_file,
            amount=str(self.net_amount),
            trade_time=self.trade_time.isoformat() if self.trade_time else "",
            department=self.department,
            settle_type=self.settle_type,
            record_id=self.his_order_no or self.order_no,
            is_refund=self.is_refund,
            cross_period=cross,
            period=self.period,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "period": self.period,
            "order_no": self.order_no,
            "amount": str(self.amount),
            "refund": str(self.refund) if self.refund is not None else None,
            "net_amount": str(self.net_amount),
            "trade_time": self.trade_time.isoformat() if self.trade_time else None,
            "trade_type": self.trade_type,
            "department": self.department,
            "settle_type": self.settle_type,
            "his_order_no": self.his_order_no,
            "source_file": self.source_file,
            "source_row_no": self.source_row_no,
        }


def record_from_bill(record: Any) -> AlignRecord:
    """把 :class:`~reconq.store.bill_store.BillRecord`（落库前）转成对齐行。

    这是**主路径上**的转换：扫描时渠道行与 HIS 行都先经过
    ``build_record`` 归一（Task 7），再交给对齐器。用鸭子类型取值而不是
    ``isinstance``，是为了不把 store 层 import 进 align —— 依赖方向保持
    ``store → align``，反过来的 import 会让对齐器绑死落库实现。
    """
    return AlignRecord(
        channel=record.channel,
        period=record.period,
        order_no=record.channel_order_no,
        amount=record.amount,
        refund=record.refund,
        trade_time=record.trade_time,
        trade_type=record.trade_type,
        department=record.department,
        settle_type=record.settle_type,
        his_order_no=record.his_order_no,
        source_file=record.source_file,
        source_row_no=record.source_row_no,
        bill_date=record.bill_date,
    )


def records_from_bill(records: Iterable[Any]) -> list[AlignRecord]:
    """:func:`record_from_bill` 的批量版。"""
    return [record_from_bill(r) for r in records]


def record_from_row_dict(row: dict[str, Any]) -> AlignRecord:
    """把 ``query_period()`` 的一行转成 :class:`AlignRecord`。

    金额经 :func:`to_decimal` 归一：库里是定点字面量字符串，这里统一在入口
    收口，避免金额解析口径散在各处。
    """
    trade_time: Optional[datetime] = None
    raw_time = row.get("trade_time")
    if raw_time:
        text = str(raw_time)
        try:
            trade_time = datetime.fromisoformat(text)
        except ValueError:
            # 落库时已归一为带偏移 ISO 串；这里解析不了就留空而不是抛错 ——
            # 少一个时间戳不该让整行对不上，弱匹配会因缺时间而更保守。
            logger.warning("对齐时无法解析 trade_time=%r（行 %s），该行按无时间处理", text, row.get("source_row_no"))
    refund_raw = row.get("refund_text")
    return AlignRecord(
        channel=str(row.get("channel", "")),
        period=str(row.get("period", "")),
        order_no=str(row.get("channel_order_no", "")),
        amount=to_amount_text(row.get("amount_text"), channel=str(row.get("channel", ""))),
        refund=to_amount_text(refund_raw, channel=str(row.get("channel", ""))) if refund_raw else None,
        trade_time=trade_time,
        trade_type=str(row.get("trade_type", "") or ""),
        department=str(row.get("department", "") or ""),
        settle_type=str(row.get("settle_type", "") or ""),
        his_order_no=str(row.get("his_order_no", "") or ""),
        source_file=str(row.get("source_file", "") or ""),
        source_row_no=int(row.get("source_row_no") or 0),
        ch_id=int(row.get("ch_id") or 0),
        bill_date=str(row.get("bill_date", "") or ""),
    )


class MatchIndex:
    """HIS 侧的倒排索引：单号 → 记录列表。

    刻意用 ``dict[str, list[AlignRecord]]`` 而不是 ``dict[str, AlignRecord]``：
    键冲突（一个单号对多条记录）正是本模块要处理的核心情形，用单值结构
    表达会逼着调用方在构造时就丢掉后面那条候选——**歧义恰恰是被丢掉的那条
    记录**。多值结构让「同键多记录」成为显式可见的事实。

    同时保留 :attr:`all`，弱匹配要在全集上按金额 + 时间窗找候选。
    """

    __slots__ = ("period", "_by_no", "_all")

    def __init__(self, period: str = "") -> None:
        self.period = period
        self._by_no: dict[str, list[AlignRecord]] = {}
        self._all: list[AlignRecord] = []

    @staticmethod
    def _keys(record: AlignRecord) -> list[str]:
        """该记录可被哪些单号命中。

        HIS 侧自己的 ``channel_order_no`` 恒等于 ``his_order_no``（见
        ``CHANNEL_COLUMN_MAP``），但院内导出偶尔只给其中一个；两个都收，
        归一后去重。
        """
        raw = [record.order_no, record.his_order_no]
        seen: list[str] = []
        for value in raw:
            key = (value or "").strip()
            if key and key not in seen:
                seen.append(key)
        return seen

    def add(self, record: AlignRecord) -> None:
        self._all.append(record)
        for key in self._keys(record):
            self._by_no.setdefault(key, []).append(record)

    def lookup(self, order_no: str) -> list[AlignRecord]:
        """按单号取候选；空单号返回空列表（不猜）。"""
        key = (order_no or "").strip()
        if not key:
            return []
        return list(self._by_no.get(key, ()))

    def candidates_by_amount(
        self,
        *,
        amount: Decimal,
        tolerance: Decimal = WEAK_MATCH_TOLERANCE,
        window: Optional[PeriodWindow] = None,
    ) -> list[AlignRecord]:
        """弱匹配候选：金额落在容差内 **且** 时间落在账期窗内。

        刻意**不做**「同科室 / 同结算方式」之类的收窄。理由：院内导出里
        渠道侧根本没有科室字段，一端有、一端没有的字段不能拿来当判据
        —— 那会让弱匹配看起来更准，实际是拿不存在的信息凑条件。
        """
        out: list[AlignRecord] = []
        for record in self._all:
            if abs(record.net_amount - amount) > tolerance:
                continue
            if window is not None:
                if record.trade_time is None or not window.contains(record.trade_time):
                    continue
            out.append(record)
        return out

    @property
    def all(self) -> list[AlignRecord]:
        return list(self._all)

    def __len__(self) -> int:
        return len(self._all)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"MatchIndex(period={self.period!r}, records={len(self._all)}, keys={len(self._by_no)})"


def build_index(
    his_records: Iterable[AlignRecord],
    *,
    period: str = "",
) -> MatchIndex:
    """把 HIS 侧记录建成索引。"""
    index = MatchIndex(period)
    for record in his_records:
        index.add(record)
    return index


@dataclass
class MatchOutcome:
    """一行渠道账单的对齐结论。"""

    key: AlignKey
    status: str
    channel_record: Optional[AlignRecord] = None
    his_record: Optional[AlignRecord] = None
    #: 歧义/弱匹配时为全部候选（拒绝归并，逐条列出）
    candidates: list[AmbiguityCandidate] = field(default_factory=list)
    ambiguity: Optional[AmbiguityVerdict] = None
    diff_amount: str = ""
    detail: str = ""

    @property
    def status_zh(self) -> str:
        return STATUS_ZH.get(self.status, self.status)

    @property
    def needs_attribution(self) -> bool:
        """是否需要进入差异台账并走归因（单边账 / 歧义 / 弱匹配）。"""
        return self.status in (
            MatchStatus.CHANNEL_ONLY.value,
            MatchStatus.AMBIGUOUS.value,
            MatchStatus.WEAK_PENDING.value,
            MatchStatus.CROSS_PERIOD.value,
            MatchStatus.DUPLICATE_CLAIM.value,
            MatchStatus.MISMATCH.value,
        )

    def render(self) -> str:
        head = f"{self.key.render()} → {self.status_zh}"
        if self.ambiguity is not None:
            return "\n".join([head, self.ambiguity.render()])
        if self.diff_amount:
            head += f"（差额 {self.diff_amount} 元）"
        if self.detail:
            head += f" · {self.detail}"
        return head

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.as_dict(),
            "status": self.status,
            "status_zh": self.status_zh,
            "channel_record": self.channel_record.to_dict() if self.channel_record else None,
            "his_record": self.his_record.to_dict() if self.his_record else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "ambiguity": self.ambiguity.to_dict() if self.ambiguity else None,
            "diff_amount": self.diff_amount,
            "detail": self.detail,
            "needs_attribution": self.needs_attribution,
        }


@dataclass
class AlignAllResult:
    """六路一起跑的对齐汇总。

    反向单边账（HIS 有渠道无）挂在**这一层**而不是某一路上：一笔 HIS 收费
    查不到对应渠道行，原因可能是压根没走这几个渠道（现金、医保代收），
    说成「微信漏了」会把排查方向直接带偏。
    """

    period: str
    channels: dict[str, AlignResult] = field(default_factory=dict)
    #: 全六路跑完后仍无任何渠道行认领的 HIS 记录
    his_only: list[AlignRecord] = field(default_factory=list)

    @property
    def pending_attribution(self) -> int:
        return sum(c.pending_attribution for c in self.channels.values()) + len(self.his_only)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channels": {k: v.to_dict() for k, v in self.channels.items()},
            "his_only": [r.to_dict() for r in self.his_only],
            "his_only_amount": str(sum((r.net_amount for r in self.his_only), Decimal("0"))),
            "pending_attribution": self.pending_attribution,
        }


@dataclass
class AlignResult:
    """一路渠道的对齐汇总。

    ``his_only`` 的归属刻意分两种（见 :func:`align_channel` 与
    :func:`align_all` 的 docstring）：单跑一路时它是「本渠道没认领的 HIS
    记录」；六路一起跑时**每路都为空**、反向单边账统一挂在
    :class:`AlignAllResult.his_only` 上 —— 因为一笔 HIS 收费没被认领这件事
    本身就是**全局**的（它不属于任何一路渠道），挂到某一路名下等于暗示
    「是这一路漏的」，那是错的。
    """

    channel: str
    period: str
    outcomes: list[MatchOutcome] = field(default_factory=list)
    #: 没有任何渠道行命中到的 HIS 记录（仅单跑一路时有值）
    his_only: list[AlignRecord] = field(default_factory=list)

    def by_status(self, status: str) -> list[MatchOutcome]:
        return [o for o in self.outcomes if o.status == status]

    def counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in MatchStatus}
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return counts

    def total_amount(self) -> str:
        total = Decimal("0")
        for outcome in self.outcomes:
            if outcome.channel_record is not None:
                total += outcome.channel_record.net_amount
        return str(total)

    def his_only_amount(self) -> str:
        return str(sum((r.net_amount for r in self.his_only), Decimal("0")))

    @property
    def pending_attribution(self) -> int:
        """需要进归因的条数（单边账 + 歧义 + 弱匹配 + 金额不符）。"""
        return sum(1 for o in self.outcomes if o.needs_attribution) + len(self.his_only)

    def render(self) -> str:
        counts = self.counts()
        lines = [
            f"渠道 {self.channel} 账期 {self.period}："
            f"共 {len(self.outcomes)} 行，"
            + "、".join(f"{STATUS_ZH.get(k, k)} {v}" for k, v in counts.items() if v)
            + (f"；HIS 有渠道无 {len(self.his_only)} 行" if self.his_only else "")
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "period": self.period,
            "counts": self.counts(),
            "total_amount": self.total_amount(),
            "his_only_amount": self.his_only_amount(),
            "his_only": [r.to_dict() for r in self.his_only],
            "pending_attribution": self.pending_attribution,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


def _matched_his_order(record: AlignRecord) -> str:
    """HIS 侧用于匹配的单号：优先 ``his_order_no``，退回订单号列。"""
    return (record.his_order_no or record.order_no or "").strip()


def _make_key(channel: str, record: AlignRecord, period: str) -> AlignKey:
    # 渠道行自身的 his_order_no 若有值（院内导出会填），优先用它对齐 ——
    # 商户订单号在聚合支付场景下可能一对多，HIS 侧给的单号更细。
    order_no = (record.his_order_no or record.order_no or "").strip()
    order_no_type = "his_order_no" if record.his_order_no else "out_trade_no"
    return AlignKey(
        channel=channel,
        order_no_type=order_no_type,
        order_no=order_no,
        period=period or record.period,
    )


def align_channel(
    channel: str,
    channel_records: Sequence[AlignRecord],
    index: MatchIndex,
    *,
    period: str = "",
    window: Optional[PeriodWindow] = None,
    tolerance: Decimal = WEAK_MATCH_TOLERANCE,
    claimed: Optional[dict[int, str]] = None,
    referenced: Optional[set[int]] = None,
) -> AlignResult:
    """把一路渠道账单与 HIS 索引做三方对齐。

    判定顺序（顺序本身是产品决策，不是性能取舍）：

    1. **单号精确命中唯一一条 HIS 记录** → ``matched``；金额不等则
       ``mismatch`` 并给差额（对上了但对不平，比对不上更需要解释）。
    2. **唯一命中的那条落在账期窗口之外** → ``cross_period``，不认领。
       半开区间把周一 00:00 划给新账期，跨账期落点的记录若在本期认领，
       本期合计就会凭空多出一笔、下期又要认一次，跨期重复计入。
    3. **单号命中 ≥2 条** → ``ambiguous``，**拒绝归并**，逐条列候选并按
       成因分类（拆笔 / 合并 / 跨账期 / 退费并存 / 金额相同）。
    4. **单号未命中**（含单号缺失）→ 弱匹配：按金额 + 账期时间窗找候选。
       唯一命中也只标 ``weak_pending``，**不认领**；不唯一则列全部候选。
    5. **完全找不到候选** → ``channel_only``（渠道有 HIS 无）。

    ``claimed`` 传入时复用调用方的认领集合（:func:`align_all` 六路共用一
    个），此时本路不再自行计算 ``his_only`` —— 反向单边账是全局事实。

    任何一条 HIS 记录都**最多被一条渠道行认领**；已被认领的记录不再作为
    后续行的弱匹配候选，否则同一笔 HIS 收费会被多个渠道行同时「弱匹配」
    上，看起来两路都对、实际是重复计入。
    """
    result = AlignResult(channel=channel, period=period or index.period)
    shared = claimed is not None
    # claimed: 行标识 → 首认领它的渠道名（六路共用时，重复认领要能点名是哪一路）
    local_claimed: dict[int, str] = claimed if claimed is not None else {}
    # referenced: 已作为「歧义 / 弱匹配候选」列给人看的行。这些行**不认领**，
    # 但也**不再单列为反向单边账** —— 同一笔钱既挂在歧义下面、又被开一条
    # 「HIS 有渠道无」，台账上就是同一笔报两次，经办人要判两次。
    local_referenced: set[int] = referenced if referenced is not None else set()

    for record in channel_records:
        key = _make_key(channel, record, result.period)
        result.outcomes.append(
            _align_one(
                key,
                record,
                index,
                window=window,
                tolerance=tolerance,
                claimed=local_claimed,
                referenced=local_referenced,
            )
        )

    if not shared:
        # 剩下没被任何渠道行认领、也没被列为候选的 HIS 记录 = HIS 有渠道无。
        for record in index.all:
            ident = _identity(record)
            if ident in local_claimed or ident in local_referenced:
                continue
            result.his_only.append(record)
    return result


def _identity(record: AlignRecord) -> int:
    """认领用的行标识。

    优先用数据库主键 ``ch_id``；测试里手工构造的行没有主键，退回行对象的
    自身 id。**刻意不用 ``ch_id or 0``**：那样所有无主键的行会共用标识 0，
    第一个被认领后其余全部被当成「已认领」，弱匹配与单边账判定会整体错位。
    """
    return record.ch_id if record.ch_id else id(record)


def _align_one(
    key: AlignKey,
    record: AlignRecord,
    index: MatchIndex,
    *,
    window: Optional[PeriodWindow],
    tolerance: Decimal,
    claimed: dict[int, str],
    referenced: set[int],
) -> MatchOutcome:
    # —— 1/2) 单号精确命中 ——
    candidates = index.lookup(key.order_no)
    if len(candidates) == 1:
        his = candidates[0]
        # 单号对上了，但那条记录的收费时间落在本账期窗口之外 —— 单号是同一
        # 笔，但**账期不是这一期**。此时不认领：认了本期合计就凭空多一笔，
        # 下期再认一次又变成两期重复计入。标跨账期落点交人确认。
        if window is not None and his.trade_time is not None and window.is_cross_period(his.trade_time):
            cand = his.to_candidate(window)
            verdict = AmbiguityVerdict(
                channel=record.channel,
                order_no=key.order_no,
                kind=AmbiguityKind.CROSS_PERIOD.value,
                candidates=[cand],
                period=result_period(record, key),
                channel_row_no=record.source_row_no,
                source_file=record.source_file,
                detail=(
                    f"渠道行 {record.ref} 金额 {record.net_amount} 元，"
                    f"单号 {key.order_no} 命中 HIS {his.ref}，"
                    f"但该记录收费时间 {cand.trade_time} 落在账期 "
                    f"{key.period} 窗口 [{window.start.isoformat()}, "
                    f"{window.end.isoformat()}) 之外；不认领，请确认应计入哪一期"
                ),
            )
            referenced.add(_identity(his))
            return MatchOutcome(
                key=key,
                status=MatchStatus.CROSS_PERIOD.value,
                channel_record=record,
                candidates=[cand],
                ambiguity=verdict,
                detail=verdict.headline(),
            )
        ident = _identity(his)
        holder = claimed.get(ident)
        if holder is not None:
            # 同一笔 HIS 收费被报了第二次：跨路（两路渠道各报一次）或同路
            # （导出方把一笔拆成两行同号）。若照旧认领，两行都显示已对齐、
            # 合计却只算一笔 —— 差额就在这里被吃掉了。报告，不二次认领。
            cand = his.to_candidate(window)
            verdict = AmbiguityVerdict(
                channel=record.channel,
                order_no=key.order_no,
                kind=AmbiguityKind.DUPLICATE_CLAIM.value,
                candidates=[cand],
                period=result_period(record, key),
                channel_row_no=record.source_row_no,
                source_file=record.source_file,
                detail=(
                    f"渠道行 {record.ref} 金额 {record.net_amount} 元，"
                    f"单号 {key.order_no} 命中的 HIS 记录 {his.ref} 已被 "
                    + (f"渠道 {holder} " if holder != record.channel else "")
                    + f"认领（本次渠道 {record.channel}）；同一笔被报了两次，"
                    f"本产品只认领一次，避免同一笔钱被算进合计两次"
                ),
            )
            return MatchOutcome(
                key=key,
                status=MatchStatus.DUPLICATE_CLAIM.value,
                channel_record=record,
                candidates=[cand],
                ambiguity=verdict,
                detail=verdict.headline(),
            )
        claimed[ident] = record.channel
        referenced.add(ident)
        diff = record.net_amount - his.net_amount
        if abs(diff) <= tolerance:
            return MatchOutcome(
                key=key,
                status=MatchStatus.MATCHED.value,
                channel_record=record,
                his_record=his,
                detail=f"HIS {his.ref}",
            )
        return MatchOutcome(
            key=key,
            status=MatchStatus.MISMATCH.value,
            channel_record=record,
            his_record=his,
            diff_amount=str(diff),
            detail=f"渠道 {record.net_amount} 元 vs HIS {his.net_amount} 元（{his.ref}）",
        )

    if len(candidates) > 1:
        # 命中多条 —— 拒绝归并。
        cand_rows = [c.to_candidate(window) for c in candidates]
        kind = classify_ambiguity(
            candidates=cand_rows,
            channel_amount=record.net_amount,
        )
        taken = [_identity(c) in claimed for c in candidates]
        referenced.update(_identity(c) for c in candidates)
        verdict = AmbiguityVerdict(
            channel=record.channel,
            order_no=key.order_no,
            kind=kind,
            candidates=cand_rows,
            period=result_period(record, key),
            channel_row_no=record.source_row_no,
            source_file=record.source_file,
            detail=(
                f"渠道行 {record.ref} 金额 {record.net_amount} 元，"
                f"单号 {key.order_no or '（空）'} 命中 {len(cand_rows)} 条 HIS 记录；"
                f"本产品不择一归并"
                + (
                    f"；其中 {sum(taken)} 条已被前面的渠道行认领，"
                    f"很可能是同一笔被重复导出"
                    if any(taken)
                    else ""
                )
            ),
        )
        return MatchOutcome(
            key=key,
            status=MatchStatus.AMBIGUOUS.value,
            channel_record=record,
            candidates=cand_rows,
            ambiguity=verdict,
            detail=verdict.headline(),
        )

    # —— 3) 弱匹配：单号未命中 ——
    weak = index.candidates_by_amount(
        amount=record.net_amount, tolerance=tolerance, window=window
    )
    # 已**认领**的记录不再作为弱匹配候选。**不做「全被占用就退回全集」的
    # 兜底**：那种兜底等于让第二行把别人已认领的记录再「弱匹配」一次，看着
    # 两行都有候选、实则重复计入。全被占用时就是没有候选 → 落单边账。
    #
    # 注意区分「认领」与「列为候选」：两行弱匹配到**同一条** HIS 记录是允许
    # 的 —— 弱匹配从不认领，两行都标待人工，结论仍是「这一条没对上」。把它
    # 当成冲突去拦，报出去的就是一条「候选冲突」，而现场事实是两笔渠道款
    # 都对不上同一条 HIS 收费，那本来就该各报一条单边账。
    weak = [w for w in weak if _identity(w) not in claimed]
    referenced.update(_identity(w) for w in weak)
    if len(weak) == 1:
        his = weak[0]
        cand = his.to_candidate(window)
        verdict = AmbiguityVerdict(
            channel=record.channel,
            order_no=key.order_no,
            kind=AmbiguityKind.WEAK_MULTIPLE.value,
            candidates=[cand],
            period=result_period(record, key),
            channel_row_no=record.source_row_no,
            source_file=record.source_file,
            error_code="WEAK_MATCH_PENDING",
            detail=(
                f"渠道行 {record.ref} 金额 {record.net_amount} 元、"
                f"时间 {record.trade_time or '空'}，按金额 + 账期时间窗唯一命中 "
                f"{his.ref}；单号 "
                + (f"{key.order_no} 未在 HIS 侧命中" if key.order_no else "缺失")
                + "，仅标弱匹配待人工，不认领"
            ),
        )
        return MatchOutcome(
            key=key,
            status=MatchStatus.WEAK_PENDING.value,
            channel_record=record,
            candidates=[cand],
            ambiguity=verdict,
            detail=verdict.headline(),
        )

    if len(weak) > 1:
        cand_rows = [w.to_candidate(window) for w in weak]
        verdict = AmbiguityVerdict(
            channel=record.channel,
            order_no=key.order_no,
            kind=AmbiguityKind.WEAK_MULTIPLE.value,
            candidates=cand_rows,
            period=result_period(record, key),
            channel_row_no=record.source_row_no,
            source_file=record.source_file,
            error_code="WEAK_MATCH_PENDING",
            detail=(
                f"渠道行 {record.ref} 金额 {record.net_amount} 元，"
                f"单号未命中，金额 + 时间窗找到 {len(cand_rows)} 条候选；本产品不认领"
            ),
        )
        return MatchOutcome(
            key=key,
            status=MatchStatus.WEAK_PENDING.value,
            channel_record=record,
            candidates=cand_rows,
            ambiguity=verdict,
            detail=verdict.headline(),
        )

    # —— 4) 完全找不到 ——
    return MatchOutcome(
        key=key,
        status=MatchStatus.CHANNEL_ONLY.value,
        channel_record=record,
        detail=(
            f"渠道 {record.ref} 金额 {record.net_amount} 元，"
            f"单号 {key.order_no or '（空）'} 在 HIS 侧本账期无任何记录；"
            f"请确认是漏收费、挂账未记还是跨账期落点"
        ),
    )


def result_period(record: AlignRecord, key: AlignKey) -> str:
    return key.period or record.period


def align_all(
    channel_records_by_channel: dict[str, Sequence[AlignRecord]],
    his_records: Sequence[AlignRecord],
    *,
    period: str = "",
    window: Optional[PeriodWindow] = None,
    tolerance: Decimal = WEAK_MATCH_TOLERANCE,
) -> AlignAllResult:
    """六路一起对齐：HIS 侧只建一次索引，六路共用**同一个认领集合**。

    共用认领集合是硬要求，不是优化：同一笔 HIS 收费若被微信和自助机各自
    「认领」一次，两路都会显示已对齐、合计却只算了一笔 —— 差额就在这里
    被吃掉了。六路跑完后仍无人认领的 HIS 记录，统一挂在
    :attr:`AlignAllResult.his_only`，不摊到任何一路名下。
    """
    index = build_index(his_records, period=period)
    claimed: dict[int, str] = {}
    referenced: set[int] = set()
    channels: dict[str, AlignResult] = {}
    for channel, records in channel_records_by_channel.items():
        channels[channel] = align_channel(
            channel,
            records,
            index,
            period=period or index.period,
            window=window,
            tolerance=tolerance,
            claimed=claimed,
            referenced=referenced,
        )
    leftover = [
        r for r in index.all
        if _identity(r) not in claimed and _identity(r) not in referenced
    ]
    return AlignAllResult(period=period or index.period, channels=channels, his_only=leftover)
