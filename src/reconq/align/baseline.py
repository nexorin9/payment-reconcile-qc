"""确定性成因兜底基线（Task 35）。

为什么要有这一层
----------------
本产品的成因归因是 ``ai_role: core``：开放集合的成因靠模型给。但**模型不是
唯一一条路**，也不是**永远在的一条路**。院内自建端点可能连不上、可能超时、
可能熔断、可能当天根本没配凭据位。这些时候如果系统只留下一句「待人工判定」，
经办人拿到的就是一个空结论单 —— 比规则给一个带线索的候选还差。

所以这里给一条**纯确定性、零模型、零网络**的成因候选排序基线：

- 模型不可达 / 超时 / 熔断 / 缺凭据 → 归因自动改用本基线（Task 35 第 6 步）
- 模型可用 → 本基线**同时**跑一遍，作为**对照**记下一致 / 分歧；分歧样本进
  回放集（Task 34）当提示词迭代素材

三条不可让的边界
----------------

1. **只映射到受控成因字典**（:mod:`reconq.align.causes` 的 ``CAUSES``）。
   规则里出现字典外的码一律在模块加载期就抛 —— 规则可以少、可以钝，
   但**不许自造码**：自造码落不进台账聚合，事后没人说得清那一桶是什么。
2. **置信度封顶 0.5**（:data:`MAX_CONFIDENCE`）。这是「规则」与「看过账单
   业务含义的判断」之间的诚实分界：规则能算出的只是**形态**，不是**成因**。
   让规则打 0.9 只会让人在结论单上签字时以为模型也确认过了。
3. **每条候选都必须带真实证据行**。一个引不出行的候选不给 —— 面板上
   「规则推断」四个字后面没有行号，等于让经办人自己再去翻一遍原始文件，
   那还不如直接说「判不出来」（字典里本来就留了这一项）。

与降级、状态的关系
------------------
基线产出的是**候选**，不是结论。:func:`reconq.align.attribution.attribute`
在 ``GatewayResult.status == DEGRADED`` 时把它挂到结果的 ``rule_baseline``
字段上，``causes`` 仍然为空、``degraded`` 仍为 True、状态仍停在「待人工判定」。
**规则不推进任何业务状态** —— 与模型草稿同一条纪律，只是理由更朴素：
规则根本没看过账单列的业务含义。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层，也不需要有**：它的 ``contract verify``
输出是逐 check 的确定性判定（「这一行的这一列不满足这条口径」），本来就是
确定性的，所以不存在「模型不可达时拿什么兜」这个问题。本模块沿用的仍是那套
主路径纪律 —— 判据收口在一处、结论交人工、证据定位到源行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from ..errors import ReconqError
from .bundle import EvidenceBundle
from .causes import CAUSES, cause_vocabulary, entry_of, render_explanation
from .taxonomy import AMOUNT_TOLERANCE

logger = logging.getLogger("reconq.align.baseline")

__all__ = [
    "BASELINE_VERSION",
    "CLUE_RULES",
    "MAX_CONFIDENCE",
    "NO_RANK",
    "RULE_SOURCE",
    "BaselineComparison",
    "CandidateDraft",
    "ClueHit",
    "compare_with_model",
    "divergence_to_replay_case",
    "rank_causes_by_rules",
    "rule_catalog",
    "shape_cause",
    "skipped_clues",
]


#: 基线判据版本。进结论单与台账的 ``rule_version`` 列：规则改了要能回答
#: 「上周那条「规则推断」是按哪一版规则给的」。
BASELINE_VERSION = "rule-baseline/1"

#: 基线结果的来源标记。**与模型草稿在类型与字段上分得清清楚楚** ——
#: 面板上「规则推断(未经模型)」这句话就是靠这个常量渲染的。
RULE_SOURCE = "rules_baseline"

#: 规则推断的置信度上限。**硬上限，不是均值**。
#:
#: 定 0.5 而不是「按得分折算」的理由：规则能读出的全部是**形态**
#: （候选几条、金额守不守恒、是不是退费行、账单类型含不含退款），
#: 成因需要知道**账单列的业务含义**（``bill_type=ALL`` 与 ``=SUCCESS``
#: 看到的是两份不同的账单、AES-GCM 解密后的字段布局、院内结算规程）。
#: 形态相同而成因不同的两类差异在字段层面无法区分 —— 这正是模型存在的
#: 理由。规则给的分数再高也不该越过这条线。
MAX_CONFIDENCE = Decimal("0.5")

#: 落点距账期边界多近算「压在边界上」（秒）。
#:
#: 与 :data:`reconq.align.verify_draft._WINDOW_EDGE_TOLERANCE_SECONDS` 同值：
#: 「压在周一零点」这条判据只该有一个定义处。两处各写一个「大概一分钟」，
#: 迟早会漂成两个数，而漂掉的正是最要命的跨日切分那一类。
WINDOW_EDGE_TOLERANCE_SECONDS = 60

#: 线索规则码 → 中文名。给面板与结论单用，也是 :func:`rule_catalog` 的内容。
CLUE_RULES: dict[str, str] = {
    "bill_type_scope": "账单类型是否覆盖退款",
    "trade_type_enum": "交易类型枚举",
    "refund_row_shape": "退费行形态",
    "period_boundary": "是否压在账期边界",
    "cross_period_history": "同单号在相邻账期是否已出现",
    "kind_mapping": "对齐形态对应成因",
    "amount_structure": "候选金额结构（守恒 / 不守恒 / 全同）",
    "precedent": "近几期同形态的处置先例",
}


# ─────────────────────────── 数据结构 ───────────────────────────


@dataclass(frozen=True)
class ClueHit:
    """一条线索命中。

    :attr:`evidence_refs` 里的每个 ref 必须落在证据包真实出现过的行标识
    集合内（与模型草稿同一条纪律，判据复用
    :func:`reconq.align.attribution.evidence_refs_of`）。
    """

    rule: str
    rule_zh: str
    cause_code: str
    weight: Decimal
    detail: str
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "rule_zh": self.rule_zh,
            "cause_code": self.cause_code,
            "weight": str(self.weight),
            "detail": self.detail,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class CandidateDraft:
    """一条规则候选。**不是** :class:`~reconq.align.attribution.CauseDraft`。

    两者字段刻意不共用地基：``CauseDraft`` 是模型草稿（有 ``rank``、有
    ``explanation_zh``、会被 :mod:`reconq.align.verify_draft` 的四道闸筛），
    本类是规则候选（带 ``clues`` 明细、置信度封顶、来源恒为
    :data:`RULE_SOURCE`）。合成一个类再靠 ``source`` 字段区分，等于把
    「模型说的」与「规则算的」放进同一个列表里排序 —— 面板上一旦按
    ``confidence`` 排，规则候选就会插到模型草稿中间，读的人分不出谁说的。
    """

    cause_code: str
    score: Decimal
    confidence: float
    evidence_refs: tuple[str, ...]
    explanation_zh: str
    clues: tuple[ClueHit, ...] = ()
    rank: int = 1
    source: str = RULE_SOURCE
    rule_version: str = BASELINE_VERSION
    #: 说明模板渲染失败时为真（见 :func:`_render_zh`）。**不静默**：
    #: 模板缺槽位说明这条成因的说明模板与本基线提供的 facts 不匹配，
    #: 该修的是模板而不是把说明吞掉。
    explanation_fallback: bool = False

    @property
    def zh_name(self) -> str:
        entry = CAUSES.get(self.cause_code)
        return entry.zh_name if entry else self.cause_code

    @property
    def label_zh(self) -> str:
        return f"规则推断(未经模型) · {self.zh_name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_code": self.cause_code,
            "zh_name": self.zh_name,
            "score": str(self.score),
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "explanation_zh": self.explanation_zh,
            "clues": [c.to_dict() for c in self.clues],
            "rank": self.rank,
            "source": self.source,
            "rule_version": self.rule_version,
            "label_zh": self.label_zh,
            "explanation_fallback": self.explanation_fallback,
        }

    def render(self) -> str:
        refs = "、".join(self.evidence_refs) or "（无）"
        head = (
            f"  {self.rank}. {self.label_zh}"
            f"（置信度 {Decimal(str(self.confidence)):.2f}，封顶 {MAX_CONFIDENCE}）"
            f" ← {refs}"
        )
        lines = [head, f"     {self.explanation_zh}"]
        for clue in self.clues:
            lines.append(f"     · 线索：{clue.rule_zh} —— {clue.detail}")
        return "\n".join(lines)


@dataclass(frozen=True)
class BaselineComparison:
    """规则基线与模型草稿的对照结果。

    分歧不是缺陷 —— **它是指示词迭代的方向**。规则能稳定抓住的形态类成因
    （拆笔、合并、跨账期、退费并存）如果模型 Top-1 落在别处，说明提示词
    没能把形态读成成因；反过来模型给出字典外的解释则是提示词注入不够。
    """

    baseline_top: str
    model_top: str
    #: ``agree`` / ``diverge`` / ``baseline_only`` / ``model_only``
    verdict: str
    baseline_codes: tuple[str, ...] = ()
    model_codes: tuple[str, ...] = ()
    bundle_digest: str = ""
    conclusion_key: str = ""
    channel: str = ""
    period: str = ""

    @property
    def diverged(self) -> bool:
        return self.verdict == "diverge"

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_top": self.baseline_top,
            "model_top": self.model_top,
            "verdict": self.verdict,
            "baseline_codes": list(self.baseline_codes),
            "model_codes": list(self.model_codes),
            "bundle_digest": self.bundle_digest,
            "conclusion_key": self.conclusion_key,
            "channel": self.channel,
            "period": self.period,
        }


# ─────────────────────────── 上下文 ───────────────────────────


class _Ctx:
    """把证据包里本模块要用的判据**一次性**摊平。

    摊平而不是每条线索各读一遍，是为了让「单条差异 <5ms」这条性能断言
    测的是真实工作量：8 条线索 × 5 次 ``payload.get`` 嵌套取值在 2000 行
    六路账上会变成可见的耗时，而这里只做一次。
    """

    __slots__ = (
        "allowed",
        "amount",
        "candidates",
        "channel",
        "clue_refs",
        "cross_period",
        "diff_ref",
        "diff_row",
        "discrepancy",
        "history",
        "is_refund",
        "kind",
        "notes",
        "order_no",
        "payload",
        "period",
        "period_window",
        "refund",
        "same_order_rows",
        "trade_type",
        "window_edges",
    )

    def __init__(self, bundle: EvidenceBundle) -> None:
        from .attribution import evidence_refs_of

        payload = bundle.payload
        self.payload = payload
        self.allowed = frozenset(evidence_refs_of(bundle))
        self.period = str(payload.get("period", "") or "")
        self.channel = str(payload.get("channel", "") or "")
        diff_row = payload.get("diff_row")
        self.diff_row = diff_row if isinstance(diff_row, dict) else {}
        self.discrepancy = payload.get("discrepancy") or {}
        self.kind = str(self.discrepancy.get("kind", "") or "")
        self.amount = _decimal_or_none(self.diff_row.get("amount"))
        self.is_refund = bool(self.diff_row.get("is_refund"))
        refund = _decimal_or_none(self.diff_row.get("refund"))
        self.refund = refund if refund and refund > 0 else None
        self.cross_period = bool(self.diff_row.get("cross_period"))
        self.trade_type = str(self.diff_row.get("trade_type", "") or "").strip()
        self.order_no = str(self.diff_row.get("order_no", "") or "").strip()
        self.diff_ref = str(self.diff_row.get("ref", "") or "").strip()
        self.candidates = [
            r for r in (payload.get("candidate_rows") or []) if isinstance(r, dict)
        ]
        self.same_order_rows = [
            r for r in (payload.get("same_order_no_rows") or []) if isinstance(r, dict)
        ]
        self.history = payload.get("history") or {}
        self.period_window = payload.get("period_window") or {}
        self.window_edges = _window_edges(self.period_window)
        #: 判据被跳过的原因。**不静默**：一条线索「没跑」与「跑了没命中」
        #: 在面板上长得一模一样，而前者意味着这条差异的成因判据少了一条。
        self.notes: list[str] = []
        # 候选里带证据行的 ref：线索尽量引**被指认的那几行**，而不是只引差异行。
        self.clue_refs = tuple(
            str(r.get("ref", "") or "").strip()
            for r in (*self.candidates, *self.same_order_rows)
            if str(r.get("ref", "") or "").strip()
        )

    def refs(self, *values: str) -> tuple[str, ...]:
        """过滤出真实存在的行标识（去重、保序）。"""
        out: list[str] = []
        for value in values:
            if value and value in self.allowed and value not in out:
                out.append(value)
        return tuple(out)


# ─────────────────────────── 线索规则 ───────────────────────────


def _hit(
    ctx: _Ctx, rule: str, cause_code: str, weight: str, detail: str, *refs: str
) -> ClueHit:
    """构造一条线索命中。**成因码当场校验**（字典外即抛）。"""
    entry_of(cause_code)  # 字典外立刻炸，不留到落库时才炸
    return ClueHit(
        rule=rule,
        rule_zh=CLUE_RULES[rule],
        cause_code=cause_code,
        weight=Decimal(weight),
        detail=detail,
        evidence_refs=ctx.refs(*refs),
    )


def _clue_bill_type(ctx: _Ctx) -> list[ClueHit]:
    """账单类型是否覆盖退款。

    这是**渠道取件参数**层面的判据（``bill_type`` 取 ``ALL`` 还是
    ``SUCCESS``），不是渠道名 —— 规则里刻意只认这三个词，换一家渠道只要
    它用同一套取件参数，规则不用改。
    """
    bill_type = str(
        (ctx.payload.get("channel_meta") or {}).get("bill_type", "") or ""
    ).upper()
    if not bill_type:
        return []
    refund_evidence = ctx.is_refund or ctx.refund is not None
    if not refund_evidence:
        refund_evidence = any(
            bool(r.get("is_refund")) or _negative_amount(r) for r in ctx.candidates
        )
    if not refund_evidence:
        return []
    if "REFUND" in bill_type and "ALL" not in bill_type:
        return [
            _hit(
                ctx,
                "bill_type_scope",
                "refund_offset_in_his",
                "0.35",
                f"本期账单类型为「{bill_type}」，只含退款交易；"
                f"同一单号在账单里看不到对应的原单行",
                ctx.diff_ref,
                *ctx.clue_refs[:2],
            )
        ]
    if "SUCCESS" in bill_type:
        return [
            _hit(
                ctx,
                "bill_type_scope",
                "bill_type_excludes_refund",
                # 0.55：高于退费行形态的 0.50。理由是**证据的确定性**——
                # 「本期取的是只含成功交易的账单」是取件参数写死在配置文件里
                # 的事实，而「这一行像退费行」是从金额与退款额字段**推**出来的。
                # 两者同时成立时，先信那个不用推的：账单里压根不该有退费行，
                # 所以问题出在取件口径，不在收费处的红冲。
                "0.55",
                f"本期账单类型为「{bill_type}」，只含成功交易；"
                f"账单里存在退费形态的行/字段但账单类型不覆盖退款",
                ctx.diff_ref,
                *ctx.clue_refs[:2],
            )
        ]
    return []


def _clue_trade_type(ctx: _Ctx) -> list[ClueHit]:
    """交易类型枚举。**权重高于形态映射**：交易类型是账单里写死的枚举值，
    看得见就是看得见；而形态（``channel_only`` / ``mismatch``）是对齐器
    判出来的，两侧都还隔着一次推断。"""
    value = ctx.trade_type.upper()
    if not value:
        return []
    refund_tokens = ("退款", "退费", "REFUND", "REVERSAL", "撤销入账")
    closed_tokens = ("关闭", "CLOSED", "CANCEL", "取消", "未支付", "UNPAID", " unpaid")
    if any(token in value for token in refund_tokens):
        return [
            _hit(
                ctx,
                "trade_type_enum",
                "refund_offset_in_his",
                "0.55",
                f"该行交易类型为「{ctx.trade_type}」，属退款/红冲类交易",
                ctx.diff_ref,
            )
        ]
    if any(token in value for token in closed_tokens):
        return [
            _hit(
                ctx,
                "trade_type_enum",
                "contract_config_error",
                "0.50",
                f"该行交易类型为「{ctx.trade_type}」，属未成交/已关闭交易；"
                f"它出现在账单里说明取件口径把未成交交易也纳进来了，"
                f"该口径取值需信息科复核",
                ctx.diff_ref,
            )
        ]
    return []


def _clue_refund_row_shape(ctx: _Ctx) -> list[ClueHit]:
    """退费行形态。"""
    if ctx.is_refund:
        return [
            _hit(
                ctx,
                "refund_row_shape",
                "refund_offset_in_his",
                "0.50",
                f"该行本身是退费（红冲）行：金额 {ctx.diff_row.get('amount')} 元、"
                f"退款额 {ctx.diff_row.get('refund') or '（空）'} 元、"
                f"净额 {ctx.diff_row.get('net_amount')} 元",
                ctx.diff_ref,
            )
        ]
    negative = [r for r in ctx.candidates if _negative_amount(r)]
    if negative:
        return [
            _hit(
                ctx,
                "refund_row_shape",
                "refund_offset_in_his",
                "0.40",
                f"候选里混有 {len(negative)} 条负额行（退费/红冲），"
                f"与原单并存需先确认红冲是否生效",
                ctx.diff_ref,
                *(str(r.get("ref", "")) for r in negative[:2]),
            )
        ]
    return []


def _clue_period_boundary(ctx: _Ctx) -> list[ClueHit]:
    """是否压在账期边界（周一 00:00 归新账期）。

    交易时间**必须带偏移**才能参与这条判据：账期边界是带偏移的时刻，
    拿一个 naive 时刻去和它比，要么直接抛（``can't subtract
    offset-naive and offset-aware``），要么在某个库/版本下悄悄按 UTC 算 ——
    后者会把整批行全判成「压在周一零点」。所以这里对 naive 时刻**不猜**，
    显式跳过并在 :attr:`_Ctx.notes` 里留一句原因。
    """
    hits: list[ClueHit] = []
    trade_time = _parse_dt(ctx.diff_row.get("trade_time"))
    if trade_time is None:
        if ctx.diff_row.get("trade_time"):
            ctx.notes.append(
                f"交易时间 {ctx.diff_row.get('trade_time')!r} 不可解析为时刻，"
                f"跳过账期边界判据"
            )
    elif trade_time.tzinfo is None:
        ctx.notes.append(
            f"交易时间 {ctx.diff_row.get('trade_time')!r} 不带时区偏移，"
            f"无法与账期边界比较 —— 跳过该判据（不按 UTC 猜，"
            f"猜错会让整批行都被判成边界行）"
        )
    elif ctx.window_edges:
        for label, edge in ctx.window_edges:
            if abs((trade_time - edge).total_seconds()) <= WINDOW_EDGE_TOLERANCE_SECONDS:
                hits.append(
                    _hit(
                        ctx,
                        "period_boundary",
                        "period_boundary_landing",
                        "0.45",
                        f"交易时间 {ctx.diff_row.get('trade_time')} 距账期 {label} "
                        f"边界（{edge.isoformat()}）不足 "
                        f"{WINDOW_EDGE_TOLERANCE_SECONDS} 秒",
                        ctx.diff_ref,
                    )
                )
                break
    else:
        ctx.notes.append("账期时间窗不可用，跳过账期边界判据")
    if ctx.cross_period:
        hits.append(
            _hit(
                ctx,
                "period_boundary",
                "period_boundary_landing",
                "0.50",
                "该行被标为跨账期落点（落在本账期时间窗之外）",
                ctx.diff_ref,
            )
        )
    if not hits:
        return []
    return hits


def _clue_cross_period_history(ctx: _Ctx) -> list[ClueHit]:
    """同单号在相邻账期是否已出现（渠道延迟入账 / 跨账期重复计入）。

    判据是**方向**：出现在**往后**的账期 = 这笔是渠道晚给了（本期查不到、
    下期能查到）；出现在**往前**的账期 = 这笔上期已经计过一次。两种方向
    指向完全相反的处置，把它们混成「历史出现过」是最容易出的错。
    """
    if not ctx.period or not ctx.order_no:
        return []
    by_period = (ctx.history.get("same_order_no") or {}).get("by_period") or {}
    if not isinstance(by_period, Mapping) or not by_period:
        return []
    from ..sources.timezone import parse_period

    try:
        cur_year, cur_week = parse_period(ctx.period)
    except ReconqError:
        return []
    later: list[tuple[str, int]] = []
    earlier: list[tuple[str, int]] = []
    for period in by_period:
        try:
            year, week = parse_period(str(period))
        except ReconqError:
            continue
        delta_weeks = (year * 53 + week) - (cur_year * 53 + cur_week)
        if delta_weeks > 0:
            later.append((str(period), delta_weeks * 7))
        elif delta_weeks < 0:
            earlier.append((str(period), abs(delta_weeks) * 7))
    if not later and not earlier:
        return []
    history_refs = tuple(str(r.get("ref", "") or "") for r in ctx.same_order_rows)
    hits: list[ClueHit] = []
    if later:
        period, days = min(later, key=lambda item: item[1])
        hits.append(
            _hit(
                ctx,
                "cross_period_history",
                "channel_late_posting",
                "0.55",
                f"该单号在更晚的账期 {period} 已出现过（晚 {days} 天），"
                f"而本期账单里 HIS 侧查不到 —— 指渠道结算入账延迟",
                ctx.diff_ref,
                *history_refs[:2],
            )
        )
    if earlier:
        period, days = min(earlier, key=lambda item: item[1])
        hits.append(
            _hit(
                ctx,
                "cross_period_history",
                "cross_period_duplicate",
                "0.50",
                f"该单号在更早的账期 {period} 已出现过（早 {days} 天），"
                f"本期又被认领一次 —— 指跨账期重复计入",
                ctx.diff_ref,
                *history_refs[:2],
            )
        )
    return hits


#: 对齐形态 → 成因码。**只列有据可依的**：形态到成因不是一对一，
#: 剩下的交给 :func:`_clue_amount_structure` 与 ``undetermined``。
_KIND_TO_CAUSE: dict[str, tuple[str, str]] = {
    "channel_only": ("his_missing_order", "0.45"),
    "cross_period": ("period_boundary_landing", "0.35"),
    "duplicate_across_period": ("cross_period_duplicate", "0.50"),
    "duplicate_claim": ("merged_payment_allocation", "0.35"),
    "duplicate_across_channel": ("undetermined", "0.30"),
    "mismatch": ("contract_config_error", "0.30"),
    "amount_inconsistent": ("contract_config_error", "0.30"),
    "weak_pending": ("undetermined", "0.25"),
}

#: 形态 → 中文说明。与 :func:`_clue_kind_mapping` 里那份**同值**但各自
#: 定义：那份在热路径上、随线索一起构造 :class:`ClueHit`；这份是给
#: :func:`shape_cause` 这条**只读**路径用的（面板按成因分组时要显示
#: 「为什么规则推了这一条」）。两处各写一份字面量是重复，但把热路径
#: 拖成一次 dict 查表、只为省下三行字不值得 —— 而让面板另抄一份文案
#: 则是真的会出现「规则说 A、面板说 B」。
_KIND_DETAIL_ZH: dict[str, str] = {
    "channel_only": "渠道有、HIS 侧本账期无任何记录（不是金额不符，是查不到行）",
    "cross_period": "该笔落在本账期时间窗之外，按跨日切分口径未归入本期",
    "duplicate_across_period": "同一笔 HIS 收费被两个账期各认领了一次",
    "duplicate_claim": "同一条 HIS 收费被两条渠道行各认领了一次",
    "duplicate_across_channel": "同一渠道单号被两路渠道各认领一次；"
    "是导出重复还是真收了两遍，规则判不出",
    "mismatch": "两侧同一笔金额不符（差在金额上，不是缺行）",
    "amount_inconsistent": "链上多笔之间的金额关系不成立",
    "weak_pending": "只靠金额 + 时间窗的弱匹配找到候选，缺 HIS 单号",
}


def shape_cause(kind: str) -> Optional[dict[str, Any]]:
    """形态 → 单条规则成因候选。**只读那一档映射，不跑整套线索。**

    面板「按成因分组」要显示规则侧的建议，而面板**手上没有证据包** ——
    证据包是 :mod:`reconq.align.bundle` 在跑主路径时现构造的，落库的是
    快照（不含逐条线索）。所以这里给的是「仅凭形态」这一档：它恰好是
    :func:`rank_causes_by_rules` 里权重最高、也最容易被人复核的那一档，
    而**不是**完整基线结果。

    面板因此必须把这条标成「规则推断(未经模型)」并且**不声称它是排序
    第一**（:attr:`rank` 是 :data:`NO_RANK`）。完整基线要证据包，
    面板不重跑 —— 重跑会把「打开网页」变成「再跑一次归因」，那不是
    面板该干的活。
    """
    mapped = _KIND_TO_CAUSE.get(kind or "")
    if not mapped:
        return None
    cause_code, weight = mapped
    return {
        "cause_code": cause_code,
        "zh_name": CAUSES[cause_code].zh_name if cause_code in CAUSES else cause_code,
        "score": weight,
        "kind": kind,
        "rule": "kind_mapping",
        "rule_zh": CLUE_RULES["kind_mapping"],
        "detail": _KIND_DETAIL_ZH.get(kind, ""),
        "source": RULE_SOURCE,
        "rule_version": BASELINE_VERSION,
        "label_zh": f"规则推断(未经模型) · {CLUE_RULES['kind_mapping']}",
        "rank": NO_RANK,
        "partial": True,
    }


#: 「未排名」哨兵。:func:`shape_cause` 给的候选没有排名（它不是完整基线
#: 的 Top-1），面板靠这个值拒绝把它排到模型草稿前面。
NO_RANK = 0


def _clue_kind_mapping(ctx: _Ctx) -> list[ClueHit]:
    """对齐形态对应成因。"""
    mapped = _KIND_TO_CAUSE.get(ctx.kind)
    if not mapped:
        return []
    cause_code, weight = mapped
    detail = _KIND_DETAIL_ZH[ctx.kind]
    return [
        _hit(ctx, "kind_mapping", cause_code, weight, detail, ctx.diff_ref, *ctx.clue_refs[:2])
    ]


def _clue_amount_structure(ctx: _Ctx) -> list[ClueHit]:
    """候选金额结构：守恒 / 不守恒 / 全同。

    这一条是本基线**最硬的判据** —— 它复用的是
    :mod:`reconq.align.taxonomy` 里那套已经被单测钉死的形态判别逻辑
    （金额守恒=拆笔、扇出且金额互不相同=合并、金额全同且不守恒=判不出），
    区别只在于这里把它翻译成**带证据行的成因候选**而不是分型结论。
    """
    candidates = ctx.candidates
    if len(candidates) < 2 or ctx.amount is None:
        return []
    amounts = [_decimal_or_none(r.get("amount")) for r in candidates]
    if any(a is None for a in amounts):
        return []
    total = sum((a for a in amounts if a is not None), Decimal("0"))
    diff = abs(total - ctx.amount)
    conserved = diff <= AMOUNT_TOLERANCE
    all_equal = len(set(amounts)) == 1
    refs = tuple(str(r.get("ref", "")) for r in candidates)
    if conserved:
        return [
            _hit(
                ctx,
                "amount_structure",
                "split_payment_unallocated",
                "0.50",
                f"{len(candidates)} 条候选金额加总 {total} 元与差异行 {ctx.amount} 元"
                f"守恒（差 {diff} 元，容差 {AMOUNT_TOLERANCE}）—— 指一次渠道支付在 "
                f"HIS 侧被按费用项目拆成多笔",
                ctx.diff_ref,
                *refs,
            )
        ]
    if all_equal:
        return [
            _hit(
                ctx,
                "amount_structure",
                "undetermined",
                "0.40",
                f"{len(candidates)} 条候选金额全相同（{amounts[0]} 元）且与差异行"
                f"{ctx.amount} 元不守恒（差 {diff} 元）—— 单号可能被导出方截断或复用，"
                f"规则判不出是不是同一笔",
                ctx.diff_ref,
                *refs,
            )
        ]
    return [
        _hit(
            ctx,
            "amount_structure",
            "merged_payment_allocation",
            "0.45",
            f"{len(candidates)} 条候选金额互不相同且加总 {total} 元与差异行 "
            f"{ctx.amount} 元不守恒（差 {diff} 元）—— 指一笔渠道款覆盖了多条 HIS 收费",
            ctx.diff_ref,
            *refs,
        )
    ]


def _clue_precedent(ctx: _Ctx, prior: Mapping[str, Decimal]) -> list[ClueHit]:
    """近几期同形态的处置先例。权重刻意压低（0.15）——

    先例是**佐证**不是判据：上期同一形态被判成「口径配置错」，不代表这期
    也是。让先例压过形态判据，规则基线就会退化成「照抄上期」。

    它加给**前面线索已经得出来的成因**，而不是自己再按形态表推一遍 ——
    「上期同样形态有人这么判过」这句话只对已经成立的候选有意义。
    对一个还没有任何线索支持的成因补一分，就是在凭空造依据。
    """
    by_disposition = (ctx.history.get("same_cause") or {}).get("by_disposition") or {}
    if not isinstance(by_disposition, Mapping) or not by_disposition:
        return []
    confirmed = by_disposition.get("confirmed") or by_disposition.get("已确认") or 0
    try:
        confirmed = int(confirmed)
    except (TypeError, ValueError):
        confirmed = 0
    if confirmed <= 0:
        return []
    if not prior:
        # 前面的线索一条没命中：此时没有「同形态已成立的候选」可佐证，
        # 静默不加 —— 先例不能凭空造出一个成因。
        return []
    lead = max(prior.items(), key=lambda item: (item[1], item[0]))[0]
    return [
        _hit(
            ctx,
            "precedent",
            lead,
            "0.15",
            f"近几期同形态差异有 {confirmed} 条已确认处置结论，可作为佐证",
            ctx.diff_ref,
        )
    ]


#: 全部线索规则（**不含** :func:`_clue_precedent`）。顺序即执行顺序，
#: 也决定同分时的线索排列。先例单列在最后跑 —— 它要读前面线索的累计得分。
_SHAPE_RULES = (
    _clue_bill_type,
    _clue_trade_type,
    _clue_refund_row_shape,
    _clue_period_boundary,
    _clue_cross_period_history,
    _clue_kind_mapping,
    _clue_amount_structure,
)


# ─────────────────────────── 主入口 ───────────────────────────


def rank_causes_by_rules(bundle: EvidenceBundle) -> list[CandidateDraft]:
    """纯规则成因候选排序。**不调模型、不发网络请求、不写任何状态。**

    :returns: 按得分降序、同分按成因码字典序（保证可复算）的候选列表。
        恒非空 —— 字典里既然留了 ``undetermined``，基线就不该在实在
        判不出来时交一张空表。
    """
    ctx = _Ctx(bundle)
    hits: list[ClueHit] = []
    running: dict[str, Decimal] = {}
    for rule in _SHAPE_RULES:
        for hit in rule(ctx):
            hits.append(hit)
            running[hit.cause_code] = running.get(hit.cause_code, Decimal("0")) + hit.weight
    hits.extend(_clue_precedent(ctx, running))

    scores: dict[str, Decimal] = {}
    clues: dict[str, list[ClueHit]] = {}
    for hit in hits:
        scores[hit.cause_code] = scores.get(hit.cause_code, Decimal("0")) + hit.weight
        clues.setdefault(hit.cause_code, []).append(hit)

    # 「判不出来」永远在候选表末尾，权重恒定最低：规则跑完了还是给不出
    # 结论时，人工判定必须是一个显式可选项，而不是「表里没有所以没人管」。
    if "undetermined" not in scores:
        scores["undetermined"] = Decimal("0.05")
        clues["undetermined"] = []

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    candidates: list[CandidateDraft] = []
    for rank, (cause_code, score) in enumerate(ordered, start=1):
        cause_hits = tuple(clues.get(cause_code, ()))
        refs = _collect_refs(ctx, cause_hits, cause_code)
        if not refs:
            # 引不出证据行的候选不给：面板上「规则推断」后面没有行号，
            # 等于让经办人自己再翻一遍原始文件。
            continue
        confidence = _confidence(score)
        explanation, fallback = _render_zh(cause_code, ctx, cause_hits, refs, score)
        candidates.append(
            CandidateDraft(
                cause_code=cause_code,
                score=score,
                confidence=confidence,
                evidence_refs=refs,
                explanation_zh=explanation,
                clues=cause_hits,
                rank=len(candidates) + 1,
                explanation_fallback=fallback,
            )
        )
    return candidates


def _confidence(score: Decimal) -> float:
    """得分 → 置信度。**恒 ≤** :data:`MAX_CONFIDENCE`。"""
    normalized = score if score < Decimal("1") else Decimal("1")
    value = normalized * MAX_CONFIDENCE
    if value > MAX_CONFIDENCE:  # 防御：万一有人把 MAX 调大了仍要封顶在 1
        value = Decimal("1")
    return float(value.quantize(Decimal("0.0001")))


def _collect_refs(
    ctx: _Ctx, hits: Sequence[ClueHit], cause_code: str
) -> tuple[str, ...]:
    """该成因候选引用的证据行。

    先取线索自己引的行；一条都没引到时退回差异行 —— 差异行总是真实存在
    的（证据包就是为这条差异造的），而「一条差异至少能被指认」是面板上
    点开一条候选就能回查的最低要求。
    """
    refs: list[str] = []
    for hit in hits:
        for ref in hit.evidence_refs:
            if ref not in refs:
                refs.append(ref)
    if not refs and ctx.diff_ref and ctx.diff_ref in ctx.allowed:
        refs.append(ctx.diff_ref)
    return tuple(refs)


def _render_zh(
    cause_code: str,
    ctx: _Ctx,
    hits: Sequence[ClueHit],
    refs: Sequence[str],
    score: Decimal,
) -> tuple[str, bool]:
    """按受控字典的中文模板出说明。

    模板渲染失败（缺槽位）时**不吞**：返回一条显式标注「说明模板缺槽位」
    的兜底句并把 ``explanation_fallback`` 置真。静默吞掉的话，面板上
    「模板要 ``{amount}`` 而本基线没给」这件事永远没人知道，直到某天
    结论单上少半句话被投诉。
    """
    facts: dict[str, Any] = {
        "count": len(refs) or 1,
        "amount": ctx.amount if ctx.amount is not None else "-",
        "diff_amount": str(ctx.discrepancy.get("diff_amount", "") or ""),
        "evidence": list(refs),
        "order_no": ctx.order_no or "-",
        "period": ctx.period or "-",
        "clues": [f"{h.rule_zh}：{h.detail}" for h in hits] or ["证据包内无可用规则线索"],
    }
    try:
        return render_explanation(cause_code, facts), False
    except ReconqError as exc:
        entry = entry_of(cause_code)
        lines = [h.detail for h in hits] or ["证据包内无可用规则线索"]
        return (
            f"【{entry.zh_name}】规则线索：{'；'.join(lines)}。"
            f"建议：{entry.suggested_action}。责任方：{entry.owner}。"
            f"（本条说明按兜底句渲染：{exc.code} —— "
            f"说明模板与本基线提供的槽位不匹配，请补模板）",
            True,
        )


# ─────────────────────────── 对照 ───────────────────────────


def compare_with_model(
    bundle: EvidenceBundle,
    baseline: Sequence[CandidateDraft],
    model_drafts: Sequence[Any],
) -> BaselineComparison:
    """规则基线与模型草稿的对照（同一份证据包下）。

    ``model_drafts`` 接受任何带 ``.cause`` 属性的对象（:class:`CauseDraft`
    或原始 dict 都行）—— 刻意不做类型收窄：这一层只是记一个 Top-1 对不对，
    收窄了类型反而让「回放集里那条录好的响应」也得先构造成草稿才能比。
    """
    base_codes = tuple(c.cause_code for c in baseline)
    model_codes = tuple(str(_get(d, "cause", "")) for d in model_drafts)
    base_top = base_codes[0] if base_codes else ""
    model_top = model_codes[0] if model_codes else ""
    if base_top and model_top:
        verdict = "agree" if base_top == model_top else "diverge"
    elif base_top:
        verdict = "baseline_only"
    else:
        verdict = "model_only"
    return BaselineComparison(
        baseline_top=base_top,
        model_top=model_top,
        verdict=verdict,
        baseline_codes=base_codes,
        model_codes=model_codes,
        bundle_digest=bundle.digest,
        conclusion_key=str(bundle.payload.get("conclusion_key", "") or ""),
        channel=str(bundle.payload.get("channel", "") or ""),
        period=str(bundle.payload.get("period", "") or ""),
    )


def divergence_to_replay_case(
    comparison: BaselineComparison,
    model_drafts: Sequence[Any],
    *,
    case_id: str = "",
) -> dict[str, Any]:
    """把一条**分歧**样本转成回放集用例的形状（Task 34 的 ``cases/*.json``）。

    只收分歧：一致样本进回放集没有增量（那条已经对了），而回放集贵在
    「每一条都在某个方向上给提示词出题」。字段与
    :class:`reconq.align.replay.ReplayCase` 一一对应，落盘即可被
    :func:`reconq.align.replay.load_replay_set` 读回来。

    ``cause_codes`` 给的是「模型答错的这一条 + 规则给的那一条」：写成
    **可接受集合**而不是单值，正是因为规则候选本身就可能是次优答案，
    回放集要评的是「落在这一族里」，不是「逐字等于规则 Top-1」。
    """
    if not comparison.diverged:
        return {}
    codes = [c for c in (comparison.model_top, comparison.baseline_top) if c]
    seen: list[str] = []
    for code in codes:
        if code not in seen:
            seen.append(code)
    return {
        "case_id": case_id
        or f"div-{comparison.conclusion_key or comparison.bundle_digest[:12]}",
        "name_zh": (
            f"规则与模型分歧：{comparison.baseline_top} vs {comparison.model_top}"
            f"（{comparison.period} / {comparison.channel}）"
        ),
        "channel": comparison.channel,
        "kind": "",
        "expect": {
            "cause_codes": seen,
            "required_refs": _top_refs(model_drafts),
            "min_refs": 1,
        },
    }


def _top_refs(model_drafts: Sequence[Any]) -> list[str]:
    """模型 Top-1 引用的行。回放集的 ``required_refs`` 要的是 Top-1 的
    那一组 —— 拿第 2 条的引用去要求 Top-1，会把「排序错了」误报成
    「证据引错了」。"""
    if not model_drafts:
        return []
    refs = _get(model_drafts[0], "evidence_refs", ()) or ()
    return [str(r) for r in refs][:1]


# ─────────────────────────── 小工具 ───────────────────────────


def rule_catalog() -> dict[str, Any]:
    """规则目录（面板/结论单上「规则推断」那行点开看到的说明）。"""
    return {
        "version": BASELINE_VERSION,
        "max_confidence": str(MAX_CONFIDENCE),
        "rules": [{"rule": code, "rule_zh": zh} for code, zh in CLUE_RULES.items()],
        "causes": list(cause_vocabulary()),
    }


def skipped_clues(bundle: EvidenceBundle) -> tuple[str, ...]:
    """本次判据被跳过的原因（去重、保序）。

    「一条线索**没跑**」与「跑了但没命中」在面板上长得一模一样，而前者
    意味着这条差异少了一条判据 —— 面板上要能回答「为什么这条只给了一条
    线索」。返回空元组表示八条线索全部跑过。

    单独一个出口而不是塞进 :func:`rank_causes_by_rules` 的返回值：调用方
    绝大多数只关心候选列表，让它去解第二个返回值等于逼所有人写
    ``_, skipped = rank(...)``。要查的时候调这个就行。
    """
    ctx = _Ctx(bundle)
    for rule in _SHAPE_RULES:
        rule(ctx)
    return tuple(dict.fromkeys(ctx.notes))


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - 脏值一律当「没有这个量」，不炸主路径
        return None


def _negative_amount(row: Mapping[str, Any]) -> bool:
    for key in ("amount", "net_amount", "refund"):
        value = _decimal_or_none(row.get(key))
        if value is not None and value < 0:
            return True
    return False


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _window_edges(window: Mapping[str, Any]) -> tuple[tuple[str, datetime], ...]:
    """账期时间窗的两条边界（带偏移的时刻）。

    **带偏移的 datetime 之间直接相减是错的**：一个带 +08:00、一个带 Z，
    结果会差 8 小时，「压在周一零点」这条判据会把整批行全判成边界行。
    所以这里先各自转成带偏移的时刻再相减 —— 两个都有偏移，Python 才会
    做正确的时区换算。
    """
    if not window.get("available"):
        return ()
    edges: list[tuple[str, datetime]] = []
    for label, key in (("起", "start"), ("止", "end")):
        moment = _parse_dt(window.get(key))
        if moment is not None and moment.tzinfo is not None:
            edges.append((label, moment))
    return tuple(edges)


def _get(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)
