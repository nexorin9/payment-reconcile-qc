"""「按渠道单表逐笔 VLOOKUP 对」的基础实现（Task 37 抽出，Task 38 复用）。

为什么它进产品包而不是留在脚本里
--------------------------------
它有两处消费者，两处**必须**是同一份实现：

* ``scripts/baseline_compare.py`` —— 2000+ 行真实规模上的对拍报告；
* :mod:`reconq.align.metrics` —— 金标集上的指标同口径对比（Task 38）。

指标体系最忌讳的就是「基线有两份实现」：报告里的基线数字与门禁里的基线数字
一旦来自不同代码，「本产品优于基线」这句话就没有可核对的对象了。所以
判据收口在这里，脚本与指标模块都从这里导入。

基线是什么（不是稻草人，是财务科今天真在做的事）
------------------------------------------------
把 HIS 收费导出与渠道账单各自导成一张表，用 Excel 的 ``VLOOKUP``（或
DataGrip 里一条 ``select * from his where his_order_no = ?``）**按单号逐笔
对**：查到就比金额，查不到就记「渠道有 HIS 无」。然后把 HIS 表里没被任何
渠道行引用到的那些行，人工扫一眼标「HIS 有渠道无」。

它有三条**结构性**缺陷，本产品逐条对应：

===========================  ==========================================
基线的做法                   本产品多做了什么
===========================  ==========================================
命中多条时**静默取第一条**   报 ``ambiguous`` 并列全候选，**不认领**
只判「有没有」，不判「哪一期」 跨账期落点单独成类，不在本期认领
单号没命中就是「没有」        按金额+时间窗弱匹配，仍**不自动归领**
===========================  ==========================================

第一条是所有对拍与指标的核心：``EX-AMBIG-*`` 这类拆笔与金额相同形态，
基线会**挑一个数对上就当平了**，多出来的那条 HIS 记录则因为「已经被引用过」
而不出现在任何单边账里 —— 差额被吃掉了，而台账上一条差异都没有。

刻意**不加**任何启发式：不看金额、不看时间窗、不看红冲方向。加了就不是
「财务科今天在用的那个 VLOOKUP」了，对拍出来的差异会变成「我的实现 vs
另一个我的实现」，证明不了任何事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

from .matcher import AlignRecord

__all__ = [
    "BASELINE_TOLERANCE",
    "BaselineVerdict",
    "ProductVerdict",
    "run_vlookup_baseline",
]


#: 基线的容差与本产品对齐器一致（0.01 元）。基线用**同一个**容差，才叫对拍 ——
#: 给基线一个更宽的容差可以让差异变少，但那样量出来的「本产品更准」是假的。
#: 口径真源仍在 :data:`reconq.align.matcher.WEAK_MATCH_TOLERANCE`；这里
#: 显式写成字面量而不是 import，是为了让「基线与产品各自独立取值」这件事
#: 在代码上看得见 —— 哪天两边漂了，对拍报告的差异数会先变。
BASELINE_TOLERANCE = Decimal("0.01")


@dataclass
class BaselineVerdict:
    """基线对一行渠道账单的结论。"""

    order_no: str
    #: ``matched`` / ``amount_diff`` / ``channel_only``
    tag: str
    #: 基线**选中**的那条 HIS 记录（命中多条时只留第一条 —— 这就是缺陷本身）
    picked: Optional[AlignRecord] = None
    #: 该单号在 HIS 侧实际命中的全部候选（>1 即基线会静默丢弃其余）
    hit_count: int = 0
    dropped: list[AlignRecord] = field(default_factory=list)
    diff: str = ""


@dataclass
class ProductVerdict:
    """本产品对同一行的结论。与 :class:`BaselineVerdict` 并排放进对比表。"""

    order_no: str
    status: str
    diff: str = ""
    ref: str = ""


def run_vlookup_baseline(
    channel_rows: Sequence[AlignRecord],
    his_rows: Sequence[AlignRecord],
    *,
    tolerance: Decimal = BASELINE_TOLERANCE,
) -> tuple[list[BaselineVerdict], set[str]]:
    """基线：按单号逐笔 VLOOKUP，命中多条时**静默取第一条**。

    返回 ``(逐行结论, 被引用过的 HIS 单号集合)``。第二个返回值用来算反向
    单边账 —— 基线里这一步是人工再扫一遍 HIS 表，这里用引用集合等价表达。

    逐行结论与 ``channel_rows`` **同序同长**：调用方按下标与产品侧的
    :class:`~reconq.align.matcher.MatchOutcome` 对齐，这是「同一行、两种
    方案」在指标模块里成立的前提。
    """
    his_by_no: dict[str, list[AlignRecord]] = {}
    for rec in his_rows:
        his_by_no.setdefault(rec.order_no, []).append(rec)
    referenced: set[str] = set()
    verdicts: list[BaselineVerdict] = []
    for rec in channel_rows:
        hits = his_by_no.get(rec.order_no) or []
        if not hits:
            verdicts.append(BaselineVerdict(order_no=rec.order_no, tag="channel_only"))
            continue
        first = hits[0]
        referenced.add(first.order_no)
        verdict = BaselineVerdict(
            order_no=rec.order_no,
            tag="matched",
            picked=first,
            hit_count=len(hits),
            dropped=list(hits[1:]),
        )
        delta = rec.net_amount - first.net_amount
        if abs(delta) > tolerance:
            verdict.tag = "amount_diff"
            verdict.diff = str(delta)
        verdicts.append(verdict)
    return verdicts, referenced
