"""歧义拒绝归并（Task 21）。

为什么这一层单独成文件
----------------------
对齐器最危险的动作不是「对不上」，而是**对得上但对错了**。一个渠道订单号
在 HIS 侧命中 2 条以上收费记录时，任何「择一」的策略都是在替财务科做账务
判断，而猜错的后果是整段金额被归到错的费用项目上、错的方向可能连着错好几天。
因此本产品的口径是：**拒绝归并，把全部候选行列出来交给人**。

五种歧义形态（医院里都能当场对上号）
------------------------------------

======================  ====================================================
形态                    现场长什么样
======================  ====================================================
``split``               一码付一次结算拆多笔：住院预交金 / 门诊一次缴多笔，
                        HIS 按费用项目各记一条，渠道只给一个订单号
``merged``              合并支付：家属一次刷卡付了 3 个 HIS 收费单，
                        渠道侧合成一个订单号
``cross_period``        跨账期落点：渠道已入账、HIS 收费落在下周一 00:00 之后
                        （半开区间另一侧），或渠道文件晚到一天
``refund_and_original`` 同一单号的退费行与原单行同时存在：原单 128.00、
                        退费 -128.00，任一方向单独看都成立
``equal_amount``        金额相同的多候选：同一天同金额两笔，单号被导出方
                        截断或复用，肉眼与规则都无法判断
======================  ====================================================

``equal_amount`` 特别说明：它连「哪条更可能」都说不出来。金额与时间全同
的单号复用，判据必须来自导出方或收费员口述，系统侧再聪明的启发式也是编的。
这正是「规则解决不了、必须人工」的那一类，模型也不该在这里替人拍板。

与错误码的关系
--------------
歧义不是「错误」，是**一种必须留痕的判定结果**，因此不进
``ReconqError`` 的异常路径，而是作为 :class:`AmbiguityVerdict` 正常返回，
由调用方写进差异台账（``needs_attribution=True``、``disposition=待确认``）。
错误码 ``AMBIGUOUS_ENTITY`` / ``WEAK_MATCH_PENDING`` 仍被登记在
``errors.py``：面板拿结论里的码去反查「该不该重试 / 建议动作是什么」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Sequence

__all__ = [
    "AmbiguityKind",
    "AmbiguityCandidate",
    "AmbiguityVerdict",
    "classify_ambiguity",
    "render_candidates",
]


class AmbiguityKind(str, Enum):
    """歧义形态。值即落库字符串，中文说明在 :data:`KIND_ZH`。"""

    SPLIT = "split"
    MERGED = "merged"
    CROSS_PERIOD = "cross_period"
    REFUND_AND_ORIGINAL = "refund_and_original"
    EQUAL_AMOUNT = "equal_amount"
    #: 二级（弱匹配）候选不唯一时用：单号没命中，只能靠金额 + 时间窗找，
    #: 而这样的候选不止一条。与前五种的区别是**候选来源不同**而非成因不同。
    WEAK_MULTIPLE = "weak_multiple"
    #: 同一笔 HIS 收费被两路渠道各报一次（不是一对多，而是多对一）
    DUPLICATE_CLAIM = "duplicate_claim"


#: 形态 → 中文说明 + 给经办人的一句「该去查什么」。写在这里而不是散在
#: 各个 raise/return 里，是为了让面板、台账、结论单三处说同一句话。
KIND_ZH: dict[str, tuple[str, str]] = {
    AmbiguityKind.SPLIT.value: (
        "一码付拆多笔",
        "同一渠道订单号在 HIS 侧有多条收费记录，通常是一次结算按费用项目拆开入账；"
        "请确认哪几条属于本次支付，或请收费处补充拆分说明",
    ),
    AmbiguityKind.MERGED.value: (
        "合并支付",
        "一个渠道订单号对应 HIS 侧多条收费记录，常见于家属一次刷卡付多个单；"
        "请确认该订单号应覆盖的全部 HIS 收费记录",
    ),
    AmbiguityKind.CROSS_PERIOD.value: (
        "跨账期落点",
        "候选记录的收费时间落在本账期时间窗之外（周一 00:00 边界或账单文件晚到）；"
        "请确认该笔应计入哪个账期，不要为了对平挪到本期",
    ),
    AmbiguityKind.REFUND_AND_ORIGINAL.value: (
        "退费与原单并存",
        "同一单号同时存在原单与退费行；请确认红冲是否已生效，"
        "避免把已退的金额当成已收",
    ),
    AmbiguityKind.EQUAL_AMOUNT.value: (
        "金额相同的多候选",
        "候选记录的金额与时间完全相同，单号可能被导出方截断或复用，"
        "系统侧无法判别；请向导出方索取完整单号或由收费员确认",
    ),
    AmbiguityKind.WEAK_MULTIPLE.value: (
        "弱匹配待人工",
        "该渠道行没有可用的 HIS 单号，只能按金额 + 账期时间窗找候选。"
        "**唯一命中也不认领** —— 一天里同金额的缴费有几十笔，唯一只说明"
        "「碰巧对得上」；请经办人确认后由主管复核，系统不自动归并",
    ),
    AmbiguityKind.DUPLICATE_CLAIM.value: (
        "重复认领",
        "同一笔 HIS 收费被两路渠道各报了一次；请确认是导出重复还是真的收了两遍，"
        "本产品只认领一次，避免同一笔钱被算进合计两次",
    ),
}


@dataclass(frozen=True)
class AmbiguityCandidate:
    """一条候选 HIS 记录。字段刻意对齐财务科看收费记录的习惯：
    行号、金额、时间、科室、结算方式——四样就够人回查了。
    """

    row_no: int
    source_file: str = ""
    amount: str = ""
    trade_time: str = ""
    department: str = ""
    settle_type: str = ""
    record_id: str = ""
    is_refund: bool = False
    cross_period: bool = False
    #: 该候选记录所属账期（Task 25 加）。跨账期落点分型要按**账期**判，
    #: 而不只是按 ``cross_period`` 布尔位：账单文件晚到时导出方会把上一期
    #: 的记录标成本期账期，时间落在窗外但 ``period`` 字段写的是本期 ——
    #: 只看布尔位会把它判成「未跨期」，而那正是连坐追查要抓的那一类。
    period: str = ""

    @property
    def ref(self) -> str:
        """可回查定位：``文件名:行号``。"""
        return f"{self.source_file}:{self.row_no}" if self.source_file else f"#{self.row_no}"

    def render(self) -> str:
        flags = []
        if self.is_refund:
            flags.append("红冲")
        if self.cross_period:
            flags.append("跨账期")
        suffix = f"（{'、'.join(flags)}）" if flags else ""
        dept = self.department or "（科室空）"
        settle = self.settle_type or "（结算方式空）"
        return f"{self.ref} 金额 {self.amount or '空'} 时间 {self.trade_time or '空'} {dept} {settle}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_no": self.row_no,
            "source_file": self.source_file,
            "ref": self.ref,
            "amount": self.amount,
            "trade_time": self.trade_time,
            "department": self.department,
            "settle_type": self.settle_type,
            "record_id": self.record_id,
            "is_refund": self.is_refund,
            "cross_period": self.cross_period,
            "period": self.period,
        }


@dataclass(frozen=True)
class AmbiguityVerdict:
    """一次拒绝归并的判定结果。**不是异常**——它是台账里的一行。"""

    channel: str
    order_no: str
    kind: str
    candidates: list[AmbiguityCandidate] = field(default_factory=list)
    detail: str = ""
    period: str = ""
    channel_row_no: Optional[int] = None
    source_file: str = ""
    error_code: str = "AMBIGUOUS_ENTITY"

    @property
    def kind_zh(self) -> str:
        return KIND_ZH.get(self.kind, ("未登记形态", ""))[0]

    @property
    def suggestion(self) -> str:
        return KIND_ZH.get(self.kind, ("", ""))[1]

    @property
    def candidate_total(self) -> int:
        return len(self.candidates)

    def headline(self) -> str:
        """一句话结论，终端与面板都直接用它。

        候选数**总是**带出来，即使只有一条：弱匹配唯一命中时不写「命中 1
        条」会让人误以为那是确定结论，而它恰恰是最需要人确认的那一条。
        """
        return (
            f"{self.kind_zh}：{self.order_no or '（无单号）'} "
            f"命中 {self.candidate_total} 条候选，未归并"
        )

    def render(self) -> str:
        lines = [self.headline()]
        if self.detail:
            lines.append(f"  说明：{self.detail}")
        lines.append("  候选（逐条回查，系统不择一）：")
        lines.extend(f"    {c.render()}" for c in self.candidates)
        if self.suggestion:
            lines.append(f"  建议：{self.suggestion}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "period": self.period,
            "order_no": self.order_no,
            "kind": self.kind,
            "kind_zh": self.kind_zh,
            "candidate_total": self.candidate_total,
            "candidates": [c.to_dict() for c in self.candidates],
            "detail": self.detail,
            "error_code": self.error_code,
            "source_file": self.source_file,
            "channel_row_no": self.channel_row_no,
        }


def _refund_like(amount: Decimal, is_refund_flag: bool) -> bool:
    """退费行判定：显式红冲标志，或金额为负。"""
    return bool(is_refund_flag) or amount < 0


def classify_ambiguity(
    *,
    candidates: Sequence[AmbiguityCandidate],
    channel_amount: Optional[Decimal] = None,
    weak: bool = False,
) -> str:
    """给候选集判一个歧义形态。

    判据顺序是刻意的：从**成因最具体**的往回退。跨账期落点最具体
    （时间窗能直接证伪），退费与原单并存次之（方向能直接分辨），
    金额相同最难判（什么信息都没有了）。判不出来的落到
    ``equal_amount``——它不是「默认」而是「信息已耗尽」的诚实表述。

    ``weak=True`` 时（单号没命中、只能靠金额 + 时间窗找）即使只有一条
    候选也不认领，形态记 ``weak_multiple``：让台账能区分「单号冲突」与
    「根本没有单号」，两者的排查方向完全不同。
    """
    if not candidates:
        return AmbiguityKind.WEAK_MULTIPLE.value if weak else AmbiguityKind.EQUAL_AMOUNT.value

    amounts = {c.amount for c in candidates}

    # 1) 全部候选都落在本账期之外 —— 跨账期落点，判据最硬。
    if all(c.cross_period for c in candidates):
        return AmbiguityKind.CROSS_PERIOD.value

    # 2) 候选里既有红冲/负额行又有正常行 —— 退费与原单并存。
    flags = [_refund_like(Decimal(c.amount or "0"), c.is_refund) for c in candidates]
    if any(flags) and not all(flags):
        return AmbiguityKind.REFUND_AND_ORIGINAL.value

    # 3) 金额全同（相对渠道行而言也算同）—— 信息耗尽。
    if len(amounts) == 1 and (channel_amount is None or amounts == {str(channel_amount)}):
        if weak:
            return AmbiguityKind.WEAK_MULTIPLE.value
        return AmbiguityKind.EQUAL_AMOUNT.value

    # 4) 弱匹配且金额互不相同：金额能把候选区分开，判据其实是够的，
    #    但本产品仍不自动认领 —— 形态记 weak_multiple 以示区别。
    if weak:
        return AmbiguityKind.WEAK_MULTIPLE.value

    # 5) 其余情形：一码付拆多笔与合并支付在字段层面同形（都是 1→N），
    #    区分靠「N 条是否共享同一收费员/科室」这类系统侧拿不到的上下文，
    #    因此统一记 split，并在 detail 里把两种可能都写出来交人判断。
    return AmbiguityKind.SPLIT.value


def render_candidates(verdict: AmbiguityVerdict) -> list[str]:
    """把候选渲染成可打印行（终端与台账导出共用）。"""
    return [c.render() for c in verdict.candidates]
