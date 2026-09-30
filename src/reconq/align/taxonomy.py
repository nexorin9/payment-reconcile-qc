"""歧义四形态分类与判别规则（Task 25）。

拒绝归并解决了「不静默择一」，但没解决「经办人该找谁」
--------------------------------------------------------
:mod:`reconq.align.ambiguity` 给出的形态（split / merged / cross_period /
refund_and_original / equal_amount …）是**对齐器视角**：它只描述「候选行长
什么样」。台账上写「一码付拆多笔」时，经办人真正要问的是另一组问题：

* 一码付拆笔 → 该去查**拆分明细与分摊**（住院预交金按费用项目拆开入账）
* 合并支付 → 该去查**合并支付的分摊规则**（家属一次刷卡付了几个单）
* 跨账期落点 → 该去查**这笔该计入哪一期**
* 退费与原单并存 → 该去查**红冲有没有生效**

同一个 ``AMBIGUOUS_ENTITY`` 错误码下藏着四种**处置动作完全不同**的情形，
而 ``equal_amount`` 那类「信息已耗尽」的候选集根本不该被塞进任何一种成因
—— 塞进去等于给出一个查不下去的假线索。本模块把这一层显式化：
:class:`AmbiguityKind` 只承认四种成因 + 一个显式的 ``UNKNOWN``，
:class:`TaxonomyVerdict` 带上**判别依据**、**建议动作**，以及
**已排除的规则清单**。

三条硬约束
----------
1. **UNKNOWN 必须显式，且必须说清排除了什么。** 把判不出来的候选集硬塞进
   某一类，结论单上就会出现一条「建议核对拆分明细」而现场根本没有拆分这回事
   —— 那是比「不知道」更贵的错误，因为它把排查方向带偏。``excluded``
   逐条写明「哪条规则不成立、为什么不成立」，经办人才知道该问导出方还是该问
   收费处。
2. **本模块不产生任何自动处置。** 它只出 ``suggestion``（提示文本）与
   ``kind``（分类），状态推进仍只由人工点。规则与模型都不得 transition。
3. **判别只在候选集上做，不改候选集。** 拒绝归并的产物是「候选全列」，
   本模块消费它、给它贴分类标签，不从中挑一条。

判别规则（按「判据硬度」从硬到软）
----------------------------------
==========  ==========================  =========================================
规则码      判据                       归入
==========  ==========================  =========================================
``refund``  候选里既有退费行又有原单行   ``REFUND_COEXIST``
``period``  任一候选的账期与本账期不同   ``CROSS_PERIOD``
``split``   多对一扇出且两侧金额守恒     ``SPLIT_PAYMENT``
``merged``  多对一扇出、金额不守恒、     ``MERGED_PAYMENT``
            且扇出侧金额**不全相同**
==========  ==========================  =========================================

顺序是刻意的：退费与跨账期是**可直接证伪**的硬判据（符号、时区），
必须先判；拆笔与合并支付是**金额关系推断**，只有在前面两条都不成立时
才有讨论余地。剩下的全部落 ``UNKNOWN``。

「扇出侧金额不全相同」这条限制不是修辞
------------------------------------
同一天同金额的两笔缴费（候选 66.00 + 66.00，渠道 66.00）满足「多对一扇出」
却不满足金额守恒，若不设这道限制就会被判成 ``MERGED_PAYMENT``，建议
「核对合并支付的分摊规则」—— 而现场根本没有合并支付这回事，是导出方截断了
单号。把它判成 UNKNOWN 并写明「候选金额全同，属信息耗尽」才是诚实的。

与 Task 21/23 的关系
-------------------
* :func:`case_from_verdict` 直接消费 :class:`~reconq.align.ambiguity.AmbiguityVerdict`；
* :func:`case_from_weak_candidate` 消费 :class:`~reconq.align.scoring.Candidate`
  （弱匹配二级候选），一律给 ``UNKNOWN`` 并附 ``factors`` —— 弱候选本来
  就只有金额 + 时间窗，**连「是不是同一笔」都没判据**，分型是替它编的；
* :func:`verdict_to_diff_items` 把分类结果落进差异台账（``ambiguity_kind``
  列），与单边账、连坐结论**并列**。

源产品对照
----------
参考实现（``soda-core``）**没有这一层**，连「一对多」这件事都不存在：
它的 check 只在**单个 dataset 内**逐行检查列值，跨表只有 ``relationships``
这种声明式存在性约束，既不报单边账、也不拒绝归并、更不解释成因（见
:mod:`reconq.align.matcher` 模块头）。本模块的算法、判据与成因字典均为
本产品所有；沿用的仍只是主路径纪律：**证据定位到源行、拒绝对不上时静默
择一、结论交人工**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Sequence

from .ambiguity import AmbiguityCandidate, AmbiguityVerdict

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from ..engine.period import DiffItem

__all__ = [
    "AMOUNT_TOLERANCE",
    "AmbiguityKind",
    "KIND_ZH",
    "RULE_NAMES",
    "ClusterRow",
    "TaxonomyCase",
    "ExcludedRule",
    "TaxonomyVerdict",
    "classify_taxonomy",
    "case_from_verdict",
    "case_from_weak_candidate",
    "verdict_to_diff_items",
    "verdicts_to_diff_items",
]

logger = logging.getLogger("reconq.align.taxonomy")


#: 金额守恒的容差（元）。与 :data:`reconq.align.matcher.WEAK_MATCH_TOLERANCE`
#: 同值（分位抹零），但**各写一份常量、由测试锁死相等** —— 跨模块 import
#: 一个常量会把两个本可独立演化的判据绑死（Task 9 对下载链接 TTL 做过同样
#: 的处理）。
AMOUNT_TOLERANCE = Decimal("0.01")


class AmbiguityKind(str, Enum):
    """歧义成因分类。值即落库字符串。"""

    #: 一码付拆笔：一次渠道支付，HIS 按费用项目拆成多笔
    SPLIT_PAYMENT = "split_payment"
    #: 合并支付：多笔渠道行共同覆盖一条（或数条）HIS 收费
    MERGED_PAYMENT = "merged_payment"
    #: 跨账期落点：候选记录的账期与本账期不同
    CROSS_PERIOD = "cross_period"
    #: 退费与原单并存：候选里既有退费（红冲）行又有原单行
    REFUND_COEXIST = "refund_coexist"
    #: 判不出来。**不是兜底**，是「信息已耗尽」的显式表述
    UNKNOWN = "unknown"


#: 成因 → (中文名, 建议动作)。建议动作只提示查什么，**不代替判断**。
KIND_ZH: dict[str, tuple[str, str]] = {
    AmbiguityKind.SPLIT_PAYMENT.value: (
        "一码付拆笔",
        "建议核对拆分明细与分摊：一次渠道支付在 HIS 侧被按费用项目拆成多笔"
        "（常见于住院预交金）。请收费处补充分摊说明，确认这几条是否同属本次支付",
    ),
    AmbiguityKind.MERGED_PAYMENT.value: (
        "合并支付",
        "建议核对合并支付的分摊规则：一笔（或数笔）渠道款覆盖了多条 HIS 收费，"
        "但两侧金额不守恒。请确认该笔应覆盖哪几条 HIS 收费、分摊比例由谁定",
    ),
    AmbiguityKind.CROSS_PERIOD.value: (
        "跨账期落点",
        "建议核对本笔应计入哪个账期：候选记录的收费时间落在本账期窗口之外"
        "（周一 00:00 边界或账单文件晚到）。不要为了对平把它挪到本期",
    ),
    AmbiguityKind.REFUND_COEXIST.value: (
        "退费与原单并存",
        "建议核对红冲是否已生效：同一单号同时存在原单与退费行，"
        "任一方向单独看都成立。请确认已退金额未再计入已收",
    ),
    AmbiguityKind.UNKNOWN.value: (
        "不可判定",
        "现有系统侧字段不足以判别成因：候选金额全同或形态不构成多对一扇出。"
        "请向导出方索取完整单号，或由收费员确认，系统不猜",
    ),
}

#: 规则码 → 中文名。``excluded`` 清单里用，说清「排除了哪条判据」。
RULE_NAMES: dict[str, str] = {
    "refund": "退费与原单并存",
    "period": "跨账期落点",
    "split": "一码付拆笔（金额守恒）",
    "merged": "合并支付（金额不守恒）",
}


@dataclass(frozen=True)
class ClusterRow:
    """参与判别的一行。``role`` 区分它来自渠道侧还是 HIS 侧。"""

    ref: str
    amount: Decimal
    role: str = "his"
    period: str = ""
    is_refund: bool = False
    cross_period: bool = False
    department: str = ""

    def render(self) -> str:
        side = "渠道" if self.role == "channel" else "HIS"
        flags = []
        if self.is_refund:
            flags.append("红冲")
        if self.cross_period:
            flags.append("跨账期")
        suffix = f"（{'、'.join(flags)}）" if flags else ""
        return f"{side} {self.ref} 金额 {self.amount} 元 {self.period or '（账期空）'}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "role": self.role,
            "amount": str(self.amount),
            "period": self.period,
            "is_refund": self.is_refund,
            "cross_period": self.cross_period,
            "department": self.department,
        }


@dataclass(frozen=True)
class TaxonomyCase:
    """一次歧义判别的输入：一组渠道行 + 一组 HIS 候选。

    两侧都用 :class:`ClusterRow`，因为四形态里既有「一条渠道行扇出成多条
    HIS 记录」（一码付拆笔），也有「多条渠道行认领同一条 HIS 记录」
    （合并支付）—— 只建模单侧会把后者判不出来。
    """

    period: str
    channel: str
    order_no: str = ""
    channel_rows: tuple[ClusterRow, ...] = ()
    his_rows: tuple[ClusterRow, ...] = ()
    #: 弱匹配二级候选：连「是不是同一笔」都靠金额+时间窗猜
    weak: bool = False
    #: 弱候选的因子说明（不可判定的原因写在这里）
    factors: tuple[str, ...] = ()
    tolerance: Decimal = AMOUNT_TOLERANCE

    @property
    def channel_total(self) -> Decimal:
        return sum((r.amount for r in self.channel_rows), Decimal("0"))

    @property
    def his_total(self) -> Decimal:
        return sum((r.amount for r in self.his_rows), Decimal("0"))

    @property
    def is_fanout(self) -> bool:
        """是否构成「多对一 / 一对多」扇出。

        1 对 1 不构成歧义扇出（那是金额不符，走 ``MISMATCH``），不参与
        四形态判别。
        """
        return len(self.channel_rows) >= 2 or len(self.his_rows) >= 2

    @property
    def fanout_side_rows(self) -> tuple[ClusterRow, ...]:
        """扇出侧（多出来的那一侧）的行。

        两侧都 >=2 行时取 HIS 侧：判据是「这些明细加总等于对侧那一笔」，
        权威侧出明细、渠道侧出单笔，方向上更贴近现场说法。
        """
        return self.channel_rows if len(self.channel_rows) >= 2 else self.his_rows

    @property
    def fanout_amounts(self) -> list[Decimal]:
        """扇出侧的金额集合。"""
        return [r.amount for r in self.fanout_side_rows]

    @property
    def fanout_total(self) -> Decimal:
        return sum(self.fanout_amounts, Decimal("0"))

    @property
    def fanout_all_equal(self) -> bool:
        amounts = self.fanout_amounts
        return bool(amounts) and len(set(amounts)) == 1

    @property
    def conserved(self) -> bool:
        """两侧金额是否守恒（容差内）。"""
        return abs(self.channel_total - self.his_total) <= self.tolerance

    def evidence_refs(self) -> list[str]:
        return [r.ref for r in (*self.channel_rows, *self.his_rows) if r.ref]

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "order_no": self.order_no,
            "channel_rows": [r.to_dict() for r in self.channel_rows],
            "his_rows": [r.to_dict() for r in self.his_rows],
            "weak": self.weak,
            "factors": list(self.factors),
            "channel_total": str(self.channel_total),
            "his_total": str(self.his_total),
            "conserved": self.conserved,
        }


@dataclass(frozen=True)
class ExcludedRule:
    """一条**没有**成立的判据 + 它为什么不成立。"""

    code: str
    reason: str

    @property
    def name_zh(self) -> str:
        return RULE_NAMES.get(self.code, self.code)

    def render(self) -> str:
        return f"{self.name_zh}：不成立（{self.reason}）"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name_zh": self.name_zh, "reason": self.reason}


@dataclass(frozen=True)
class TaxonomyVerdict:
    """一次分型结论。**不是异常** —— 它是台账里的一行。"""

    kind: str
    period: str
    channel: str
    order_no: str = ""
    detail: str = ""
    #: 判别依据（人可读），逐条写清是哪一行、金额多少
    factors: tuple[str, ...] = ()
    #: 未成立的判据，UNKNOWN 时**必非空**
    excluded: tuple[ExcludedRule, ...] = ()
    #: 参与判别的全部行，可回查
    evidence: tuple[str, ...] = ()
    #: 渠道侧合计 − HIS 侧合计（守恒时为 0；UNKNOWN 也照实给）
    diff_amount: str = ""
    weak: bool = False
    error_code: str = "AMBIGUOUS_ENTITY"

    @property
    def kind_zh(self) -> str:
        return KIND_ZH.get(self.kind, ("未登记成因", ""))[0]

    @property
    def suggestion(self) -> str:
        return KIND_ZH.get(self.kind, ("", ""))[1]

    @property
    def is_unknown(self) -> bool:
        return self.kind == AmbiguityKind.UNKNOWN.value

    @property
    def evidence_total(self) -> int:
        return len(self.evidence)

    def headline(self) -> str:
        head = f"{self.kind_zh}：{self.order_no or '（无单号）'}"
        if self.diff_amount:
            head += f"（渠道侧与 HIS 侧相差 {self.diff_amount} 元）"
        return head

    def render(self) -> str:
        lines = [self.headline()]
        if self.detail:
            lines.append(f"  说明：{self.detail}")
        if self.factors:
            lines.append("  判别依据：")
            lines.extend(f"    · {f}" for f in self.factors)
        if self.evidence:
            lines.append("  参与判别的行（逐条回查）：")
            lines.extend(f"    {e}" for e in self.evidence)
        if self.excluded:
            lines.append("  已排除的判据：")
            lines.extend(f"    · {e.render()}" for e in self.excluded)
        if self.suggestion:
            lines.append(f"  建议：{self.suggestion}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "kind_zh": self.kind_zh,
            "period": self.period,
            "channel": self.channel,
            "order_no": self.order_no,
            "detail": self.detail,
            "factors": list(self.factors),
            "excluded": [e.to_dict() for e in self.excluded],
            "evidence": list(self.evidence),
            "evidence_total": self.evidence_total,
            "diff_amount": self.diff_amount,
            "weak": self.weak,
            "is_unknown": self.is_unknown,
            "error_code": self.error_code,
            "suggestion": self.suggestion,
        }


# ────────────────────────── 判别规则 ──────────────────────────


def _row(row: Any) -> ClusterRow:
    """把 :class:`ClusterRow` / 鸭子类型行统一成 ClusterRow。

    用鸭子取值而不是 ``isinstance``，让调用方（matcher / 测试夹具）不必
    import 本模块的类型就能造行。
    """
    if isinstance(row, ClusterRow):
        return row
    amount = getattr(row, "amount", None)
    return ClusterRow(
        ref=str(getattr(row, "ref", "")),
        amount=amount if isinstance(amount, Decimal) else Decimal(str(amount or "0")),
        role=str(getattr(row, "role", "his")),
        period=str(getattr(row, "period", "") or ""),
        is_refund=bool(getattr(row, "is_refund", False)),
        cross_period=bool(getattr(row, "cross_period", False)),
        department=str(getattr(row, "department", "") or ""),
    )


def classify_taxonomy(
    case: TaxonomyCase | Any,
    *,
    channel: str = "",
    order_no: str = "",
) -> TaxonomyVerdict:
    """给一组歧义候选判一个成因分类。

    判据顺序见模块头。UNKNOWN 时 ``excluded`` **必非空** 且逐条给出
    原因；四种成因各自至少有一条 ``factors`` 说明判据成立在哪一行。
    """
    if not isinstance(case, TaxonomyCase):
        case = TaxonomyCase(
            period=str(getattr(case, "period", "") or ""),
            channel=channel or str(getattr(case, "channel", "") or ""),
            order_no=order_no or str(getattr(case, "order_no", "") or ""),
            channel_rows=tuple(_row(r) for r in getattr(case, "channel_rows", ()) or ()),
            his_rows=tuple(_row(r) for r in getattr(case, "his_rows", ()) or ()),
            weak=bool(getattr(case, "weak", False)),
            factors=tuple(getattr(case, "factors", ()) or ()),
        )
    channel_name = case.channel or channel
    order = case.order_no or order_no
    excluded: list[ExcludedRule] = []
    diff_amount = str(case.channel_total - case.his_total)

    def verdict(kind: str, detail: str, factors: Sequence[str]) -> TaxonomyVerdict:
        return TaxonomyVerdict(
            kind=kind,
            period=case.period,
            channel=channel_name,
            order_no=order,
            detail=detail,
            factors=tuple(factors),
            excluded=tuple(excluded),
            evidence=tuple(case.evidence_refs()),
            diff_amount=diff_amount,
            weak=case.weak,
        )

    # 弱候选一律 UNKNOWN：单号没命中，只能靠金额 + 时间窗，连「是不是同一笔」
    # 都没有判据，再往下分型就是替人编。
    if case.weak:
        excluded.append(
            ExcludedRule(
                "weak",
                "弱匹配候选没有可用的 HIS 单号，判据只有金额与时间窗，不足以分型",
            )
        )
        factors = list(case.factors) or ["无因子说明（调用方未提供打分依据）"]
        return verdict(
            AmbiguityKind.UNKNOWN.value,
            f"该行是弱匹配二级候选（渠道 {channel_name or '（渠道未登记）'}），"
            f"未认领；成因不可判定，附因子说明",
            factors,
        )

    if not case.channel_rows or not case.his_rows:
        excluded.append(
            ExcludedRule("fanout", "缺少渠道侧或 HIS 侧的行，不构成多对一判别")
        )
        return verdict(
            AmbiguityKind.UNKNOWN.value,
            "候选集不完整（渠道侧或 HIS 侧为空），无法判别成因",
            ["候选集不完整"],
        )

    all_rows = (*case.channel_rows, *case.his_rows)

    # R1 退费与原单并存 —— 判据最硬：方向直接可辨。
    refunds = [r for r in all_rows if r.is_refund or r.amount < 0]
    plains = [r for r in all_rows if r not in refunds]
    if refunds and plains:
        return verdict(
            AmbiguityKind.REFUND_COEXIST.value,
            f"候选里既有原单行也有退费（红冲）行 {len(refunds)} 条，"
            f"红冲是否已生效决定了这两条该不该同时计入本期",
            [
                f"退费行：{'；'.join(r.render() for r in refunds)}",
                f"原单行：{'；'.join(r.render() for r in plains[:3])}"
                + ("…" if len(plains) > 3 else ""),
            ],
        )
    excluded.append(
        ExcludedRule(
            "refund",
            "候选里没有退费行（无负额、无红冲标志）" if not refunds else "候选全部是退费行，没有原单行",
        )
    )

    # R2 跨账期落点 —— 时区归一后的时间窗能直接证伪。
    off_period = [r for r in all_rows if r.cross_period or (r.period and r.period != case.period)]
    if off_period:
        return verdict(
            AmbiguityKind.CROSS_PERIOD.value,
            f"{len(off_period)} 条候选的收费时间落在本账期 {case.period or '（空）'} 之外",
            [
                f"越界行：{'；'.join(r.render() for r in off_period[:3])}"
                + ("…" if len(off_period) > 3 else "")
            ],
        )
    excluded.append(
        ExcludedRule("period", "全部候选都落在本账期窗口内")
    )

    # R3 / R4 需要扇出。1 对 1 不是歧义扇出（那是金额不符）。
    if not case.is_fanout:
        excluded.append(
            ExcludedRule("fanout", "渠道侧与 HIS 侧各一行，不构成多对一扇出")
        )
        return verdict(
            AmbiguityKind.UNKNOWN.value,
            "渠道侧与 HIS 侧各一行，不构成歧义扇出；成因不属于四形态",
            ["渠道 1 行 ↔ HIS 1 行"],
        )

    if case.conserved:
        return verdict(
            AmbiguityKind.SPLIT_PAYMENT.value,
            f"构成多对一扇出（渠道 {len(case.channel_rows)} 行 / HIS "
            f"{len(case.his_rows)} 行），且两侧金额守恒："
            f"渠道 {case.channel_total} 元 = HIS {case.his_total} 元",
            [
                f"渠道侧合计 {case.channel_total} 元；HIS 侧合计 {case.his_total} 元；"
                f"差额 {case.channel_total - case.his_total} 元（容差 {case.tolerance}）",
                "拆分明细：" + " + ".join(
                    f"{r.amount}（{r.ref}）" for r in case.fanout_side_rows
                ) + f" = {case.fanout_total}",
            ],
        )
    excluded.append(
        ExcludedRule(
            "split",
            f"两侧金额不守恒：渠道 {case.channel_total} 元 ≠ HIS {case.his_total} 元"
            f"（差 {case.channel_total - case.his_total} 元）",
        )
    )

    if case.fanout_all_equal:
        excluded.append(
            ExcludedRule(
                "merged",
                f"扇出侧 {len(case.fanout_amounts)} 条金额全为 "
                f"{case.fanout_amounts[0]} 元：属信息耗尽（单号被截断或复用），"
                f"判不出分摊关系",
            )
        )
        return verdict(
            AmbiguityKind.UNKNOWN.value,
            f"扇出侧候选金额全同且与对侧不守恒，信息已耗尽，判不出是合并支付还是导出重复",
            [f"扇出侧金额集合：{sorted({str(a) for a in case.fanout_amounts})}"],
        )

    return verdict(
        AmbiguityKind.MERGED_PAYMENT.value,
        f"构成多对一扇出（渠道 {len(case.channel_rows)} 行 / HIS "
        f"{len(case.his_rows)} 行）但金额不守恒：渠道 {case.channel_total} 元 ≠ "
        f"HIS {case.his_total} 元，差 {case.channel_total - case.his_total} 元；"
        f"扇出侧金额不全相同，排除「信息耗尽」",
        [
            f"扇出侧金额：{'、'.join(str(a) for a in case.fanout_amounts)}",
            f"渠道侧合计 {case.channel_total} 元；HIS 侧合计 {case.his_total} 元",
            "扇出侧明细：" + " + ".join(
                f"{r.amount}（{r.ref}）" for r in case.fanout_side_rows
            ) + f" = {case.fanout_total}",
        ],
    )


# ────────────────────────── 从既有产物构造 ──────────────────────────


def _candidate_row(candidate: AmbiguityCandidate, period: str) -> ClusterRow:
    return ClusterRow(
        ref=candidate.ref,
        amount=Decimal(candidate.amount or "0"),
        role="his",
        period=candidate.period or period,
        is_refund=candidate.is_refund,
        cross_period=candidate.cross_period,
        department=candidate.department,
    )


def case_from_verdict(
    verdict: AmbiguityVerdict,
    *,
    channel_amount: Optional[Decimal] = None,
    tolerance: Decimal = AMOUNT_TOLERANCE,
) -> TaxonomyCase:
    """从 :class:`~reconq.align.ambiguity.AmbiguityVerdict` 造判别输入。

    渠道侧那一行由 ``verdict`` 自带的 ``source_file:channel_row_no`` 定位；
    ``channel_amount`` 拿不到时**不填 0 充数**（填 0 会让守恒判据永远成立），
    此时渠道侧为空、判别直接落 UNKNOWN 并在 ``excluded`` 里说明缺什么。
    """
    channel_rows: tuple[ClusterRow, ...] = ()
    if channel_amount is not None:
        ref = (
            f"{verdict.source_file}:{verdict.channel_row_no}"
            if verdict.source_file and verdict.channel_row_no
            else verdict.order_no
        )
        channel_rows = (
            ClusterRow(ref=ref, amount=channel_amount, role="channel", period=verdict.period),
        )
    weak = verdict.kind in ("weak_multiple",)
    factors: tuple[str, ...] = ()
    if weak:
        factors = (f"对齐形态：{verdict.kind_zh}（单号未命中，按金额 + 账期时间窗找候选）",)
    return TaxonomyCase(
        period=verdict.period,
        channel=verdict.channel,
        order_no=verdict.order_no,
        channel_rows=channel_rows,
        his_rows=tuple(_candidate_row(c, verdict.period) for c in verdict.candidates),
        weak=weak,
        factors=factors,
        tolerance=tolerance,
    )


def case_from_weak_candidate(
    candidate: Any,
    *,
    period: str = "",
    channel: str = "",
) -> TaxonomyCase:
    """从 :class:`~reconq.align.scoring.Candidate`（弱候选）造判别输入。

    一律 ``weak=True``：弱候选的分档只决定「先看哪一条」，不决定「这条
    就是它」，因此在成因层**必然**落 UNKNOWN。``factors`` 取打分因子，
    把「为什么不可判定」落到具体因子上（金额差多少、时间窗差多远、
    哪几个因子因导出侧无该列而不可用）。
    """
    channel_row = getattr(candidate, "channel_row", None)
    his_row = getattr(candidate, "his_row", None)
    factors: list[str] = []
    for factor in getattr(candidate, "factors", ()) or ():
        factors.append(getattr(factor, "render", lambda: str(factor))())
    for hint in getattr(candidate, "hints", ()) or ():
        factors.append(f"线索：{hint}")
    return TaxonomyCase(
        period=period or str(getattr(channel_row, "period", "") or ""),
        channel=channel or str(getattr(channel_row, "channel", "") or ""),
        order_no=str(getattr(channel_row, "order_no", "") or ""),
        channel_rows=(
            (
                ClusterRow(
                    ref=getattr(channel_row, "ref", ""),
                    amount=getattr(channel_row, "net_amount", Decimal("0")),
                    role="channel",
                    period=str(getattr(channel_row, "period", "") or ""),
                ),
            )
            if channel_row is not None
            else ()
        ),
        his_rows=(
            (
                ClusterRow(
                    ref=getattr(his_row, "ref", ""),
                    amount=getattr(his_row, "net_amount", Decimal("0")),
                    role="his",
                    period=str(getattr(his_row, "period", "") or ""),
                    is_refund=bool(getattr(his_row, "is_refund", False)),
                    department=str(getattr(his_row, "department", "") or ""),
                ),
            )
            if his_row is not None
            else ()
        ),
        weak=True,
        factors=tuple(factors) or ("弱候选未提供打分因子",),
    )


# ────────────────────────── 入台账 ──────────────────────────

#: 差异台账里歧义成因条目的 ``check_path`` 前缀。与 ``trace.`` 前缀并列，
#: 财务科在台账上一眼能分出「一对多」与「被人认了两次」。
TAXONOMY_PATH_PREFIX = "taxonomy"


def verdict_to_diff_items(
    verdict: TaxonomyVerdict,
    *,
    run_id: str = "",
) -> list["DiffItem"]:
    """把一条分型结论转成差异台账条目（1 进 1 出）。

    ``check_path`` 只带**单号、不带成因**（唯一键是
    ``(period, channel, check_path)``）。成因是判出来的、随时可能变：把
    ``kind`` 拼进键的话，重扫后成因从「合并支付」变成「一码付拆笔」就会
    **多出一行新条目、同时留着上一行旧条目**，台账上同一笔差异出现两次，
    而经办人已确认过的旧行还挂着「待确认」。成因变化应表现为**原行更新**
    （``ambiguity_kind`` 跟着覆盖），不是新增一行。

    ``disposition`` 恒为「待确认」、``needs_attribution=True``：成因分类
    **不推进状态**，那是人工确认的活。
    """
    from ..engine.period import DiffItem  # 局部 import：align 不该被 engine 反向依赖

    return [
        DiffItem(
            period=verdict.period,
            channel=verdict.channel or "*",
            check_path=f"{TAXONOMY_PATH_PREFIX}.ambiguity:{verdict.order_no}",
            check_name=verdict.kind_zh,
            status="fail",
            message=verdict.detail,
            diff_amount=verdict.diff_amount or None,
            evidence_total=verdict.evidence_total,
            source_file="",
            first_row_no=None,
            disposition="待确认",
            needs_attribution=True,
            run_id=run_id,
            ambiguity_kind=verdict.kind,
        )
    ]


def verdicts_to_diff_items(
    verdicts: Sequence[TaxonomyVerdict],
    *,
    run_id: str = "",
) -> list["DiffItem"]:
    """批量版 :func:`verdict_to_diff_items`。"""
    items: list[DiffItem] = []
    for verdict in verdicts:
        items.extend(verdict_to_diff_items(verdict, run_id=run_id))
    return items
