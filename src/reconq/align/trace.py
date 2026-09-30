"""跨账期与跨渠道连坐追查（Task 24）。

对不上最常见的两种形态，六路各扫各的账期时**根本看不见**
--------------------------------------------------------
医院现场最典型的「对不平」不是某一行金额错了，而是**同一笔钱在两个地方
各记了一次**：

* **跨账期落点**：周一凌晨那笔，渠道按自己的结算日算进了上周，HIS 按
  收费时间记进了本周。上一期报「HIS 有渠道无」、这一期报「渠道有 HIS 无」，
  两张单子各自看都成立，**放在一起看是同一笔钱被记了两次**。
* **跨渠道重复**：家属在自助机上刷了一次，微信侧也收到一笔通知（自助机
  走的就是微信通道），两路各认领一次。任一路单看都「已对齐」。

这两类都不属于 Task 21 的七种状态里的任何一种：它们**单行看每一行都对**
，错在「这些行之间的关系」。把关系一层层拆开、只按行判定，得到的结论
必然是「两边都没错」——而结论单上恰恰只能这么写，因为系统拿不到更多上下文。

所以本模块做的是**连坐**：给一个实体（单号）在相邻若干账期、全部六路上的
所有出现串成一条链，然后在**链上**判三件事：

============================  =========================================
链上结论                       现场长什么样
============================  =========================================
``duplicate_across_period``   同一笔 HIS 收费被两个账期各认领一次
``duplicate_across_channel``  同一渠道单号被两路渠道各认领一次
``amount_inconsistent``       链上多节点的金额不满足渠道间关系
                              （退款应为负、合并支付应为分摊和）
============================  =========================================

三条结论的共同点：**不猜、不择一、不自动调账**。它们进差异台账的方式与
单边账并列（``needs_attribution=True``、``disposition=待确认``），由经办人
逐条确认、主管复核。

连坐结论的**归属是全局的**
--------------------------
跨渠道重复天生涉及两路渠道，跨账期重复天生涉及两个账期。把它记在微信名下，
等于暗示「是微信多记了一笔」；而实际可能两路导出方都有份。因此这类结论在
台账里的 ``channel`` 恒为 ``*``，两路渠道名写进 ``message`` —— 这与 Task 21
把反向单边账挂在 :class:`~reconq.align.matcher.AlignAllResult.his_only`
（而不是某一路）��同一个判断。

金额一致性的两条关系（不靠启发式，靠渠道间的业务恒等式）
--------------------------------------------------------
1. **退款为负**：跨期的红冲节点，净额必须等于被红冲那笔的**相反数**。
   渠道账单把退款记在原单行的退款列上（金额是正数），HIS 侧红冲记录本身
   金额即为负，两边净额必须同号相反 —— 不同就说明有一边的红冲没生效，
   或者退了两笔而 HIS 只记了一笔。
2. **合并支付为分摊和**：同一账期内，**一个渠道节点对多个 HIS 节点**时，
   渠道净额必须等于这些 HIS 节点净额之和。差出来的部分就是「渠道收了、
   HIS 少记（或多记）」的那一笔。
   **刻意只在「恰好一个渠道节点、且期内无退费节点」时判**：多渠道节点之间
   存在分摊关系，判据需要的是「谁分摊给谁」这种系统侧拿不到的上下文；
   期内有退费节点时净额之和的含义已经变了（一笔退费会把和拉向 0），照算
   报出来的差额是把退款重算一遍，方向可能连反。

源产品对照
----------
参考实现（``soda-core``）**没有这一层**，也没有任何等价的：
它的 check 只在**单个 dataset 内**检查列值，跨表只有 ``relationships``
这种声明式的存在性约束，连「同一行等于对面哪几行」都不回答（见
:mod:`reconq.align.matcher` 模块头）。本模块沿用的仍是主路径纪律：
**逐行流式 + 证据定位到源行 + 拒绝对不上时静默择一**，判据与算法均为本
产品所有。
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

from ..engine.period import DiffItem
from ..errors import ReconqError
from .index import normalize_key
from .matcher import AlignRecord, AlignResult, MatchStatus, WEAK_MATCH_TOLERANCE

__all__ = [
    "TraceKind",
    "TraceNode",
    "Claim",
    "TraceConclusion",
    "EntityChain",
    "EntityIndex",
    "TRACE_KIND_ZH",
    "GLOBAL_CHANNEL",
    "DEFAULT_WINDOW_PERIODS",
    "shift_period",
    "period_window_of",
    "node_from_record",
    "claims_from_align_result",
    "claims_from_nodes",
    "build_entity_index",
    "trace_entity",
    "trace_from_records",
    "chain_to_diff_items",
    "render_chain",
    "load_entity_nodes",
]

logger = logging.getLogger("reconq.align.trace")

#: 跨渠道 / 跨账期结论在差异台账里的渠道归属。
#: 理由见模块头「连坐结论的归属是全局的」：记在任一路渠道名下都是错的。
GLOBAL_CHANNEL = "*"

#: 追查窗口：以 focus 账期为中心，上下各展开多少期（默认 2 → 共 5 期）。
DEFAULT_WINDOW_PERIODS = 2


class TraceKind(str, Enum):
    """链上结论类型。值即落库字符串。"""

    #: 同一笔 HIS 收费被两个账期各认领一次
    DUPLICATE_ACROSS_PERIOD = "duplicate_across_period"
    #: 同一渠道单号被两路渠道各认领一次
    DUPLICATE_ACROSS_CHANNEL = "duplicate_across_channel"
    #: 链上多节点金额不满足渠道间关系（退款为负 / 合并支付为分摊和）
    AMOUNT_INCONSISTENT = "amount_inconsistent"


TRACE_KIND_ZH: dict[str, tuple[str, str]] = {
    TraceKind.DUPLICATE_ACROSS_PERIOD.value: (
        "跨账期重复计入",
        "同一笔 HIS 收费被两个账期各认领了一次。常见于周一零点前后的落点差异："
        "渠道按自己的结算日记账、HIS 按收费时间记账。请确认这一笔应计入哪一期，"
        "并在另一期做冲销 —— **不要**为了两期都对平而在两期都留",
    ),
    TraceKind.DUPLICATE_ACROSS_CHANNEL.value: (
        "跨渠道重复计入",
        "同一渠道单号被两路渠道各认领了一次。常见于自助机与微信/支付宝"
        "本就是同一条通道、或导出方把一笔拆成两路各发一次。"
        "请确认是导出重复还是真的收了两遍；本产品只认领一次，"
        "避免同一笔钱被算进合计两次",
    ),
    TraceKind.AMOUNT_INCONSISTENT.value: (
        "链上金额关系不成立",
        "链上多笔之间应满足渠道间的金额关系：退款为负（原单 128、红冲必须 -128），"
        "合并支付为分摊和（渠道一笔 = HIS 侧几笔之和）。请核对哪一边的红冲/分摊"
        "没有生效 —— 本产品给出差额，不替财务科决定改哪一边",
    ),
}


# ─────────────────────────── 账期算术 ───────────────────────────


def shift_period(period: str, delta: int) -> str:
    """账期平移：``shift_period("2026-W36", -1)`` → ``"2026-W35"``。

    走 :meth:`date.date.fromisocalendar` 而不是手工推周边界：跨年时
    ``2026-W01`` 的前一周属于 2025 年，而第 53 周是否存在取决于年份 ——
    手工算的周边界在跨年与 53 周那两处必然出错，而差一期就是一整批单边账。
    """
    from ..sources.timezone import parse_period

    year, week = parse_period(period)
    monday = date.fromisocalendar(year, week, 1)
    shifted = monday + timedelta(weeks=delta)
    iso = shifted.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def period_window_of(period: str, *, center: str, window_periods: int = DEFAULT_WINDOW_PERIODS) -> list[str]:
    """以 ``center`` 为中心展开的账期列表（升序，长度 ``2 * window_periods + 1``）。

    ``window_periods=0`` 时只返回 ``center`` 自己。
    """
    if window_periods < 0:
        raise ReconqError(
            "TRACE_WINDOW_INVALID",
            f"window_periods={window_periods} 非法：追查窗口以账期为单位，"
            f"不能为负（0 表示只看 focus 账期本身）",
        )
    return [shift_period(center, i) for i in range(-window_periods, window_periods + 1)]


# ─────────────────────────── 数据结构 ───────────────────────────


@dataclass(frozen=True)
class TraceNode:
    """链上的一个节点：某一账期、某一路渠道上该单号的一次出现。

    ``side`` 区分两侧：``his`` 是权威侧的收费记录，其余是待核对的渠道行。
    金额关系核对（合并支付为分摊和）正是靠这一位分「谁是谁的和」——
    两端混作一堆就没法判了。
    """

    period: str
    channel: str
    order_no: str
    amount: Decimal
    side: str = "channel"
    trade_time: Optional[str] = None
    settle_type: str = ""
    department: str = ""
    source_file: str = ""
    source_row_no: int = 0
    his_order_no: str = ""
    is_refund: bool = False

    @property
    def net_amount(self) -> Decimal:
        """净额。渠道侧退款记在原单行的退款列上，已由
        :func:`reconq.align.matcher.AlignRecord.net_amount` 扣减过；
        本节点收的就是净额，这里不再二次扣。"""
        return self.amount

    @property
    def ref(self) -> str:
        """可回查定位：``文件名:行号``。"""
        return f"{self.source_file}:{self.source_row_no}" if self.source_file else f"#{self.source_row_no}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "order_no": self.order_no,
            "amount": str(self.amount),
            "net_amount": str(self.net_amount),
            "side": self.side,
            "trade_time": self.trade_time,
            "settle_type": self.settle_type,
            "department": self.department,
            "ref": self.ref,
            "source_file": self.source_file,
            "source_row_no": self.source_row_no,
            "his_order_no": self.his_order_no,
            "is_refund": self.is_refund,
        }

    def render(self) -> str:
        flags = "（红冲）" if self.is_refund else ""
        settle = f" {self.settle_type}" if self.settle_type else ""
        when = f" {self.trade_time}" if self.trade_time else ""
        return (
            f"{self.period} {self.channel}/{self.side} 金额 {self.net_amount} 元{when}{settle}{flags} · {self.ref}"
        )


@dataclass(frozen=True)
class Claim:
    """一次「认领」：某账期某渠道的一行，把某个 HIS 收费记录认作了己方。

    连坐判定的输入。**必须显式收集而不是从库里现推**：认领是一次判定
    行为（对齐器说「这两行是同一笔」），从 ``bill_rows`` 反推会把「两行
    恰好同号」也当成认领，而那正是需要人来分辨的情形。
    """

    period: str
    channel: str
    order_no: str
    his_order_no: str
    source_file: str = ""
    source_row_no: int = 0
    amount: Decimal = Decimal("0")

    @property
    def ref(self) -> str:
        return f"{self.source_file}:{self.source_row_no}" if self.source_file else f"#{self.source_row_no}"

    @property
    def identity(self) -> str:
        """被认领的权威实体标识。

        优先 ``his_order_no``（收费记录主键口径），退回渠道单号。
        归一在这里做：院内导出同一个单号能有全角/大小写/空格的多种写法，
        不归一就会把同一笔认成两个实体、判成「没人重复计入」。
        """
        return normalize_key(self.his_order_no or self.order_no)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "order_no": self.order_no,
            "his_order_no": self.his_order_no,
            "amount": str(self.amount),
            "ref": self.ref,
        }


@dataclass
class TraceConclusion:
    """一条链上结论。**不是异常** —— 它是台账里的一行。"""

    kind: str
    order_no: str
    detail: str
    diff_amount: str = ""
    evidence: list[str] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    #: 取这条结论的账期（台账的 period 键）
    period: str = ""
    error_code: str = ""

    @property
    def kind_zh(self) -> str:
        return TRACE_KIND_ZH.get(self.kind, ("未登记形态", ""))[0]

    @property
    def suggestion(self) -> str:
        return TRACE_KIND_ZH.get(self.kind, ("", ""))[1]

    @property
    def evidence_total(self) -> int:
        return len(self.evidence)

    def headline(self) -> str:
        head = f"{self.kind_zh}：{self.order_no or '（无单号）'}"
        if self.diff_amount:
            head += f"（差额 {self.diff_amount} 元）"
        return head

    def render(self) -> str:
        lines = [self.headline(), f"  说明：{self.detail}"]
        if self.evidence:
            lines.append("  链上节点（逐条回查，本产品不择一）：")
            lines.extend(f"    {e}" for e in self.evidence)
        if self.suggestion:
            lines.append(f"  建议：{self.suggestion}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "kind_zh": self.kind_zh,
            "order_no": self.order_no,
            "detail": self.detail,
            "diff_amount": self.diff_amount,
            "evidence": list(self.evidence),
            "evidence_total": self.evidence_total,
            "periods": list(self.periods),
            "channels": list(self.channels),
            "period": self.period,
            "error_code": self.error_code or self.kind.upper(),
        }


@dataclass
class EntityChain:
    """一个实体（单号）在追查窗口内的完整出现链。"""

    order_no: str
    focus_period: str = ""
    window_periods: int = DEFAULT_WINDOW_PERIODS
    nodes: list[TraceNode] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    conclusions: list[TraceConclusion] = field(default_factory=list)
    #: 该单号在窗口**之外**还出现于哪些账期（只报不展开）
    outside_window: list[str] = field(default_factory=list)

    @property
    def periods(self) -> list[str]:
        """链上出现过的账期（升序）。"""
        return sorted({n.period for n in self.nodes})

    @property
    def channels(self) -> list[str]:
        return sorted({n.channel for n in self.nodes})

    @property
    def occurrences(self) -> dict[str, int]:
        """账期 → 该单号在期内出现次数。"""
        out: dict[str, int] = defaultdict(int)
        for node in self.nodes:
            out[node.period] += 1
        return dict(out)

    @property
    def adjacent_period_hits(self) -> int:
        """相邻账期成对出现的对数。

        「W35 与 W36 都有、W36 与 W37 都有」= 2 对。**连坐追查真正要找的
        形态就是相邻期同时有** —— 隔了三期还在同一个单号上，那是重名或
        单号复用，不是落点差异，面板不该拿它当线索。
        """
        periods = self.periods
        hits = 0
        for left, right in zip(periods, periods[1:]):
            if shift_period(left, 1) == right:
                hits += 1
        return hits

    @property
    def total_amount(self) -> Decimal:
        return sum((n.net_amount for n in self.nodes), Decimal("0"))

    @property
    def needs_attribution(self) -> bool:
        return bool(self.conclusions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_no": self.order_no,
            "focus_period": self.focus_period,
            "window_periods": self.window_periods,
            "periods": self.periods,
            "channels": self.channels,
            "occurrences": self.occurrences,
            "adjacent_period_hits": self.adjacent_period_hits,
            "outside_window": list(self.outside_window),
            "node_total": len(self.nodes),
            "claim_total": len(self.claims),
            "total_amount": str(self.total_amount),
            "nodes": [n.to_dict() for n in self.nodes],
            "claims": [c.to_dict() for c in self.claims],
            "conclusions": [c.to_dict() for c in self.conclusions],
            "needs_attribution": self.needs_attribution,
        }

    def render(self) -> str:
        head = f"单号 {self.order_no} · 追查窗口 {self.focus_period} ±{self.window_periods} 期"
        if not self.nodes:
            head += "：窗口内没有任何记录"
            if self.outside_window:
                head += f"（该单号在 {', '.join(self.outside_window)} 另有出现，已超出窗口）"
            return head
        head += (
            f"：{len(self.nodes)} 个节点，覆盖 {len(self.periods)} 个账期、"
            f"{len(self.channels)} 路渠道，相邻期成对 {self.adjacent_period_hits} 对，"
            f"净额合计 {self.total_amount} 元"
        )
        lines = [head, "  链上节点："]
        lines.extend(f"    {n.render()}" for n in self.nodes)
        if self.conclusions:
            lines.append(f"  连坐结论 {len(self.conclusions)} 条：")
            lines.extend(f"    {c.render()}" for c in self.conclusions)
        else:
            lines.append("  连坐结论：0 条（链上各节点未发现跨期/跨渠道重复，金额关系成立）")
        if self.outside_window:
            lines.append(f"  提示：该单号在窗口外的 {', '.join(self.outside_window)} 另有出现，未纳入本链")
        return "\n".join(lines)


# ─────────────────────────── 索引与构造 ───────────────────────────


class EntityIndex:
    """按 ``(单号, 账期)`` 复合键建的倒排索引 —— 连坐查询的存储支持。

    为什么要复合键而不是只用单号：单号跨期重复是**常态**（这正是本模块
    要查的东西），单值索引在构造时就必须丢掉后面的行，那会把「两个账期
    各记了一次」这条结论的直接证据丢掉 —— 而丢掉的恰恰是结论本身。

    键走 :func:`reconq.align.index.normalize_key`（去空白 / NFKC / casefold），
    与 Task 22 索引同一份归一口径：两处归一漂移会让同一个单号在倒排查询里
    查不到、在账期索引里查得到，连坐结论随机消失。
    """

    __slots__ = ("_by_key", "_outside", "records")

    def __init__(self) -> None:
        #: 归一单号 → 该单号的全部节点（按加入顺序）
        self._by_key: dict[str, list[TraceNode]] = defaultdict(list)
        #: 归一单号 → 出现过的全部账期（含窗口外，仅供报「窗口外还有」）
        self._outside: dict[str, set[str]] = defaultdict(set)
        self.records: list[TraceNode] = []

    def add(self, node: TraceNode) -> None:
        key = normalize_key(node.order_no) or normalize_key(node.his_order_no)
        if not key:
            # 空单号不进索引：它对任何查询都只会返回「全体」，
            # 而「全体」当成候选交出去比查不到更糟。
            return
        self.records.append(node)
        self._by_key[key].append(node)
        self._outside[key].add(node.period)

    def add_all(self, nodes: Iterable[TraceNode]) -> None:
        for node in nodes:
            self.add(node)

    def nodes_for(self, order_no: str) -> list[TraceNode]:
        key = normalize_key(order_no)
        if not key:
            return []
        return list(self._by_key.get(key, ()))

    def periods_for(self, order_no: str) -> list[str]:
        key = normalize_key(order_no)
        if not key:
            return []
        return sorted(self._outside.get(key, ()))

    def __len__(self) -> int:
        return len(self.records)

    def key_total(self) -> int:
        return len(self._by_key)


def node_from_record(record: AlignRecord) -> TraceNode:
    """把 :class:`~reconq.align.matcher.AlignRecord` 转成链节点。

    金额取 :attr:`AlignRecord.net_amount`（渠道侧退款已扣减、HIS 侧红冲本身
    即负），**不再二次处理退款列**：在两条腿上都扣一遍会让退款变成正数，
    金额关系核对会以「总额不成立」报出，而真因是退款被扣了两次。
    """
    return TraceNode(
        period=record.period,
        channel=record.channel,
        order_no=record.order_no or record.his_order_no,
        amount=record.net_amount,
        side="his" if record.channel == "his" else "channel",
        trade_time=record.trade_time.isoformat() if record.trade_time else None,
        settle_type=record.settle_type,
        department=record.department,
        source_file=record.source_file,
        source_row_no=record.source_row_no,
        his_order_no=record.his_order_no,
        is_refund=record.is_refund,
    )


def claims_from_align_result(result: AlignResult) -> list[Claim]:
    """从一路渠道的对齐结论里收集「认领」。

    **只认 ``matched``**：那是唯一一种「对齐器明确说这两行是同一笔」的结论。
    ``mismatch`` 单号对上了但金额对不平，``cross_period`` / ``ambiguous`` /
    ``weak_pending`` 本质是**拒绝归并** —— 把它们算成认领，等于让
    连坐追查在一条「系统都没认下的关系」上判重复计入，报出来的差额没人能
    解释清楚。
    """
    claims: list[Claim] = []
    for outcome in result.outcomes:
        if outcome.status != MatchStatus.MATCHED.value:
            continue
        his = outcome.his_record
        if his is None:
            continue
        claims.append(
            Claim(
                period=result.period,
                channel=result.channel,
                order_no=outcome.key.order_no,
                his_order_no=his.his_order_no or his.order_no,
                source_file=outcome.channel_record.source_file if outcome.channel_record else "",
                source_row_no=outcome.channel_record.source_row_no if outcome.channel_record else 0,
                amount=outcome.channel_record.net_amount if outcome.channel_record else Decimal("0"),
            )
        )
    return claims


def build_entity_index(nodes: Iterable[TraceNode]) -> EntityIndex:
    index = EntityIndex()
    index.add_all(nodes)
    return index


def claims_from_nodes(nodes: Sequence[TraceNode]) -> list[Claim]:
    """从**已落库的节点**反推认领（``reconq trace`` 这条排障腿用）。

    判据：某渠道行自己带着 ``his_order_no``，且链上存在一条 HIS 侧记录
    对应该单号 —— 这一对就是库里记着的一次认领。

    **比 :func:`claims_from_align_result` 弱一层**，原因是它看不到
    ``matched`` 之外的信息：账单表只存了单号，不存「对齐器当时判的是什么」。
    所以由它反推出的认领只用于排障展示，**不作为台账依据** ——
    :func:`chain_to_diff_items` 拿到的结论若来自这里，调用方需要自己
    确认（CLI 只读展示，不写台账，正因如此）。
    """
    his_by_period: dict[tuple[str, str], TraceNode] = {}
    for node in nodes:
        if node.side == "his":
            key = (node.period, normalize_key(node.his_order_no or node.order_no))
            his_by_period.setdefault(key, node)
    claims: list[Claim] = []
    for node in nodes:
        if node.side == "his":
            continue
        key = (node.period, normalize_key(node.his_order_no or node.order_no))
        his = his_by_period.get(key)
        if his is None:
            continue
        claims.append(
            Claim(
                period=node.period,
                channel=node.channel,
                order_no=node.order_no,
                his_order_no=his.his_order_no or his.order_no,
                source_file=node.source_file,
                source_row_no=node.source_row_no,
                amount=node.net_amount,
            )
        )
    return claims


# ─────────────────────────── 连坐判定 ───────────────────────────


def _conclusion(
    kind: TraceKind,
    order_no: str,
    detail: str,
    *,
    diff_amount: str = "",
    evidence: Optional[Sequence[str]] = None,
    periods: Optional[Sequence[str]] = None,
    channels: Optional[Sequence[str]] = None,
    period: str = "",
) -> TraceConclusion:
    return TraceConclusion(
        kind=kind.value,
        order_no=order_no,
        detail=detail,
        diff_amount=diff_amount,
        evidence=list(evidence or ()),
        periods=list(periods or ()),
        channels=list(channels or ()),
        period=period,
        error_code=kind.value.upper(),
    )


def _detect_duplicate_across_period(claims: Sequence[Claim], order_no: str) -> list[TraceConclusion]:
    """同一笔 HIS 收费被两个账期各认领一次。"""
    buckets: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        if claim.identity:
            buckets[claim.identity].append(claim)
    out: list[TraceConclusion] = []
    for identity, group in buckets.items():
        periods = sorted({c.period for c in group})
        if len(periods) < 2:
            continue
        # 每个账期只留一条证据：同一期内被两行重复认领是 duplicate_across_channel
        # 的事，混进来会让「跨了哪几期」这个问题被同期的行淹没。
        first_per_period: dict[str, Claim] = {}
        for claim in sorted(group, key=lambda c: (c.period, c.channel, c.source_row_no)):
            first_per_period.setdefault(claim.period, claim)
        picked = [first_per_period[p] for p in periods]
        out.append(
            _conclusion(
                TraceKind.DUPLICATE_ACROSS_PERIOD,
                order_no,
                f"同一笔 HIS 收费记录（{identity}）被 {len(periods)} 个账期各认领一次："
                f"{'、'.join(periods)}；每一期单看都「已对齐」，"
                f"放在一起是同一笔钱被记了 {len(periods)} 次",
                evidence=[f"{c.period} {c.channel} 金额 {c.amount} 元 · {c.ref}" for c in picked],
                periods=periods,
                channels=sorted({c.channel for c in picked}),
                # 结论落在**最早**那一期：财务科是按时间顺序处理的，
                # 记在最近一期会让「先撤哪一笔」找不到落点。
                period=periods[0],
            )
        )
    return out


def _detect_duplicate_across_channel(claims: Sequence[Claim], order_no: str) -> list[TraceConclusion]:
    """同一渠道单号被两路渠道各认领一次（限同一账期内）。"""
    buckets: dict[tuple[str, str], list[Claim]] = defaultdict(list)
    for claim in claims:
        key = normalize_key(claim.order_no) or claim.identity
        if key:
            buckets[(claim.period, key)].append(claim)
    out: list[TraceConclusion] = []
    for (period, key), group in sorted(buckets.items()):
        channels = sorted({c.channel for c in group})
        if len(channels) < 2:
            continue
        out.append(
            _conclusion(
                TraceKind.DUPLICATE_ACROSS_CHANNEL,
                order_no,
                f"账期 {period} 内同一渠道单号（{key}）被 {len(channels)} 路渠道各认领一次："
                f"{'、'.join(channels)}；本产品只认领一次，另一路改报本结论，"
                f"避免同一笔钱被算进合计两次",
                evidence=[f"{c.period} {c.channel} 金额 {c.amount} 元 · {c.ref}" for c in sorted(group, key=lambda x: x.channel)],
                periods=[period],
                channels=channels,
                period=period,
            )
        )
    return out


def _detect_amount_inconsistent(
    nodes: Sequence[TraceNode],
    order_no: str,
    *,
    tolerance: Decimal = WEAK_MATCH_TOLERANCE,
) -> list[TraceConclusion]:
    """链上金额关系核对：退款为负、合并支付为分摊和。"""
    out: list[TraceConclusion] = []

    # —— 关系一：跨期退款为负 ——
    # 红冲节点必须等于它所红冲那笔的相反数。按账期升序，每遇到一个红冲
    # 节点就找**最近一个更早的正常节点**比对：不是同期那笔（同期原单与
    # 红冲并存是正常形态，见 Task 21 的 refund_and_original）。
    ordered = sorted(nodes, key=lambda n: (n.period, n.trade_time or "", n.source_row_no))
    for position, node in enumerate(ordered):
        if not node.is_refund:
            continue
        base = next(
            (n for n in reversed(ordered[:position]) if not n.is_refund and n.period < node.period),
            None,
        )
        if base is None:
            # 追查窗口里没有更早的原单：这可能只是窗口太窄，不是一条错误。
            # 报成「金额不成立」会给台账塞一条查不出所以然的差异。
            logger.info(
                "单号 %s 的红冲节点 %s（%s）在本追查窗口内没有更早的原单可比对，"
                "不判金额关系不成立",
                order_no, node.ref, node.period,
            )
            continue
        expected = -base.net_amount
        diff = node.net_amount - expected
        if abs(diff) <= tolerance:
            continue
        out.append(
            _conclusion(
                TraceKind.AMOUNT_INCONSISTENT,
                order_no,
                f"跨账期退款关系不成立：{base.period} 原单 {base.net_amount} 元，"
                f"{node.period} 红冲 {node.net_amount} 元，"
                f"红冲应为原单的相反数 {expected} 元。"
                f"请核对渠道与 HIS 哪一边的红冲没有生效",
                diff_amount=str(diff),
                evidence=[base.render(), node.render()],
                periods=[base.period, node.period],
                channels=sorted({base.channel, node.channel}),
                period=node.period,
            )
        )

    # —— 关系二：合并支付为分摊和 ——
    by_period: dict[str, list[TraceNode]] = defaultdict(list)
    for node in nodes:
        by_period[node.period].append(node)
    for period in sorted(by_period):
        group = by_period[period]
        if any(n.is_refund for n in group):
            # 期内有红冲节点时，「和」已经不再等于本次收款额
            # （原单 + 红冲 ≈ 0），照算报出来的差额是把退款重算一遍。
            continue
        channel_side = [n for n in group if n.side != "his"]
        his_side = [n for n in group if n.side == "his"]
        if len(channel_side) != 1 or len(his_side) < 2:
            # 不是「一个渠道节点对多个 HIS 节点」就判不了：多渠道节点之间
            # 存在分摊关系，判据需要「谁分摊给谁」这种系统侧拿不到的上下文。
            continue
        head = channel_side[0]
        expected = sum((n.net_amount for n in his_side), Decimal("0"))
        diff = head.net_amount - expected
        if abs(diff) <= tolerance:
            continue
        out.append(
            _conclusion(
                TraceKind.AMOUNT_INCONSISTENT,
                order_no,
                f"账期 {period} 合并支付的分摊和不成立：渠道 {head.channel} "
                f"一笔 {head.net_amount} 元，HIS 侧 {len(his_side)} 条收费之和为 {expected} 元，"
                f"差 {diff} 元；请核对渠道与 HIS 哪一边少记或多记了这一笔",
                diff_amount=str(diff),
                evidence=[head.render()] + [n.render() for n in his_side],
                periods=[period],
                channels=sorted({n.channel for n in group}),
                period=period,
            )
        )
    return out


def trace_entity(
    order_no: str,
    *,
    nodes: Sequence[TraceNode],
    claims: Sequence[Claim] = (),
    focus_period: str = "",
    window_periods: int = DEFAULT_WINDOW_PERIODS,
    index: Optional[EntityIndex] = None,
    tolerance: Decimal = WEAK_MATCH_TOLERANCE,
) -> EntityChain:
    """把一个实体在追查窗口内的全部出现串成链，并给出链上结论。

    ``window_periods`` 以 ``focus_period`` 为中心上下各展开多少期。
    **窗口只约束结论、不删证据**：窗口外的出现记进
    :attr:`EntityChain.outside_window` 并在链尾提示 —— 悄悄丢掉窗口外的
    行，会让「这笔在 W33 也有一份」这条线索永远问不出来，而它常常正是
    跨账期重复的另一半。

    三条结论各自独立成条、互不吞并：同一笔既跨了期又跨了渠道，出两条
    台账记录（经办人判两遍）好过出一条把两种成因揉在一起的记录 ——
    后者没法分别确认，也没法分别冲销。
    """
    all_nodes = list(index.nodes_for(order_no) if index is not None else nodes)
    chain = EntityChain(
        order_no=order_no,
        focus_period=focus_period,
        window_periods=window_periods,
    )
    if index is None:
        index = build_entity_index(all_nodes)
        all_nodes = list(index.nodes_for(order_no))

    if focus_period and window_periods >= 0:
        window = set(period_window_of(focus_period, center=focus_period, window_periods=window_periods))
        chain.outside_window = [p for p in index.periods_for(order_no) if p not in window]
        chain.nodes = [n for n in all_nodes if n.period in window]
    else:
        window = None
        chain.nodes = list(all_nodes)
    # 链按时间序读 —— 结论单附录里经办人是顺着往下看的，节点乱序时
    # 「哪一笔在前、哪一笔在后」这件事得他自己从乱序里挑出来。
    # 排序键里带上 source_row_no：同一时刻同一账期的多行必须有稳定次序，
    # 否则同一份数据两次跑出的链长得不一样，截图与结论单就对不上了。
    chain.nodes.sort(key=lambda n: (n.period, n.trade_time or "", n.source_row_no))
    # 认领按**窗口**裁，不按「节点出现在哪几期」裁：认领是判定行为，它的
    # 证据行可能在某一侧没进链（对齐器认了、HIS 那一侧行没取到）。拿节点
    # 账期去裁认领，那条认领会连同它的重复计入结论一起静默消失 —— 而
    # 「被两期各认领一次」正是本模块存在的理由。
    chain.claims = [c for c in claims if window is None or c.period in window]

    chain.conclusions.extend(_detect_duplicate_across_period(chain.claims, order_no))
    chain.conclusions.extend(_detect_duplicate_across_channel(chain.claims, order_no))
    chain.conclusions.extend(
        _detect_amount_inconsistent(chain.nodes, order_no, tolerance=tolerance)
    )
    if chain.conclusions:
        logger.info(
            "单号 %s 窗口 %s ±%s：%d 节点，%d 条连坐结论（%s）",
            order_no,
            focus_period,
            window_periods,
            len(chain.nodes),
            len(chain.conclusions),
            "、".join(c.kind for c in chain.conclusions),
        )
    return chain


def trace_from_records(
    order_no: str,
    records: Sequence[AlignRecord],
    *,
    focus_period: str = "",
    window_periods: int = DEFAULT_WINDOW_PERIODS,
    tolerance: Decimal = WEAK_MATCH_TOLERANCE,
) -> EntityChain:
    """便利入口：从 :class:`AlignRecord` 直接追查（无认领信息时只判金额关系）。

    没有 ``claims`` 就**判不出跨期/跨渠道重复** —— 那两条结论的输入是
    认领行为本身。这不是缺陷而是边界：函数不拿「两行恰好同号」当认领
    去凑一条重复计入结论。
    """
    nodes = [node_from_record(r) for r in records]
    return trace_entity(
        order_no,
        nodes=nodes,
        claims=(),
        focus_period=focus_period,
        window_periods=window_periods,
        tolerance=tolerance,
    )


# ─────────────────────────── 入台账 ───────────────────────────


def chain_to_diff_items(
    chain: EntityChain,
    *,
    run_id: str = "",
) -> list[DiffItem]:
    """把链上结论转成差异台账条目。

    与单边账**并列**（同一张表、同样 ``needs_attribution=True``、
    ``disposition=待确认``），**不混为一谈**：``check_path`` 带
    ``trace.`` 前缀，财务科在台账上一眼能分出「这一条对不上」与
    「这一条被人认了两次」。

    ``channel`` 恒为 ``*``（见模块头）；``check_path`` 里带上单号，
    同一期出现多个连坐结论时键互不相同、不会互相覆盖。
    """
    items: list[DiffItem] = []
    for conclusion in chain.conclusions:
        items.append(
            DiffItem(
                period=conclusion.period or chain.focus_period,
                channel=GLOBAL_CHANNEL,
                check_path=f"trace.{conclusion.kind}:{chain.order_no}",
                check_name=conclusion.kind_zh,
                status="fail",
                message=conclusion.detail,
                diff_amount=conclusion.diff_amount or None,
                evidence_total=conclusion.evidence_total,
                source_file="",
                first_row_no=None,
                needs_attribution=True,
                run_id=run_id,
            )
        )
    return items


def render_chain(chain: EntityChain) -> str:
    """把链渲染成可读文本（终端与结论单附录共用）。"""
    return chain.render()


# ─────────────────────────── 存储支持 ───────────────────────────


def load_entity_nodes(
    conn: sqlite3.Connection,
    order_no: str,
    periods: Sequence[str],
) -> list[TraceNode]:
    """从账单库按单号 + 账期取节点，走 ``ix_bill_order_period`` 复合索引。

    **不接受空账期列表**（那是全表扫，而本模块的存在意义就是让连坐追查
    在几十万行下仍然是毫秒级）。调用方要么给出追查窗口，要么用
    :func:`EntityIndex` 在已取回的记录上做。
    """
    if not periods:
        raise ReconqError(
            "TRACE_PERIOD_REQUIRED",
            "追查账期列表为空：连坐追查按 (单号, 账期) 复合索引取数，"
            "空账期等于全表扫。请用 reconq trace --periods 2026-W35,2026-W36 "
            "或以 --period 为中心展开追查窗口",
        )
    key = normalize_key(order_no)
    if not key:
        return []
    placeholders = ",".join("?" for _ in periods)
    # 刻意拆成**两次索引查询**而不是一条 `channel_order_no = ? OR his_order_no = ?`。
    # SQLite 对 OR 两列的过滤只能整体扫（它得逐行判断两个条件），哪怕两列
    # 各有 (单号, 账期) 复合索引也用不上 —— 查询计划会退回按账期扫，
    # 六路几十万行下追查一条链从毫秒级掉到秒级。拆开后每条都是「单号等值
    # + 账期 IN」，正好命中复合索引的首列。
    out: list[TraceNode] = []
    seen: set[tuple] = set()
    for column in ("channel_order_no", "his_order_no"):
        sql = (
            f"SELECT * FROM bill_rows WHERE {column} = ? COLLATE NOCASE "
            f"AND period IN ({placeholders}) "
            "ORDER BY period, channel, source_file, source_row_no"
        )
        for row in conn.execute(sql, [key, *periods]):
            marker = (row["ch_id"],)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(_node_from_row(row))
    out.sort(key=lambda n: (n.period, n.channel, n.source_file, n.source_row_no))
    return out


def _node_from_row(row: sqlite3.Row) -> TraceNode:
    """``bill_rows`` 的一行 → :class:`TraceNode`。

    金额是**净额**：账单表里退款列存的是正数（渠道把退款记在原单行的退款列
    上），这里扣一次。两条腿都扣会让退款变成正数、金额关系核对报出一个
    方向可能连反的差额。
    """
    from ..store.bill_store import to_amount_text

    channel = str(row["channel"])
    refund_raw = row["refund_text"]
    amount = to_amount_text(row["amount_text"], channel=channel)
    refund = to_amount_text(refund_raw, channel=channel) if refund_raw else Decimal("0")
    if refund:
        amount = amount - refund
    trade_type = str(row["trade_type"] or "")
    is_refund = bool(refund) or trade_type.lower() in {"退费", "红冲", "退款", "refund"}
    return TraceNode(
        period=str(row["period"]),
        channel=channel,
        order_no=str(row["channel_order_no"] or row["his_order_no"] or ""),
        amount=amount,
        side="his" if channel == "his" else "channel",
        trade_time=str(row["trade_time"] or "") or None,
        settle_type=str(row["settle_type"] or ""),
        department=str(row["department"] or ""),
        source_file=str(row["source_file"] or ""),
        source_row_no=int(row["source_row_no"] or 0),
        his_order_no=str(row["his_order_no"] or ""),
        is_refund=is_refund,
    )
