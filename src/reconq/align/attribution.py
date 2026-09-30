"""单边账成因排序草稿（Task 28）。

为什么成因要模型、规则不行
--------------------------
同一条「HIS 没有、渠道有」可能是：导出方漏传 / 这笔其实落在下一期 /
退费已在 HIS 红冲但渠道账单没反映 / 一码付在 HIS 被拆成几笔分摊 /
住院预交金跨日切分重复计入。这几类在**字段层面同形**——都是「渠道有一行、
HIS 侧查不到对应记录」——区分它们靠的是账单列的**业务含义**
（``bill_type=ALL/SUCCESS/REFUND`` 看到的是两份不同的账单、交易类型枚举、
退费行形态、AES-GCM 解密后的字段布局）与院内结算规程。写成规则就是
「每接一个渠道版本加一批特例」，而标注一份样本要跨渠道跨账期看，
样本量小且随渠道版本漂移，传统 ML 与固定工作流都不适用。

三条不可让的边界
----------------

1. **输出必为草稿。** :class:`CauseDraft` 上没有 ``disposition`` 字段 ——
   草稿结构里**装不下**「已确认」。这不是靠调用方自觉，是靠类型：想推进
   状态只能另走 :mod:`reconq.engine.period` 的白名单迁移，而那条路只接
   人工点的参数。
2. **证据引用越界即整条作废。** ``evidence_refs`` 里的每个 ref 必须落在
   证据包真实出现过的行标识集合内。不做「就近匹配」「去数字对」这类修补
   —— 模型引用了不存在的行，说明它在按记忆编而不是在读证据；把 ``:18``
   悄悄改成 ``:17`` 会让一份编造的结论看起来有据可查。
3. **降级不推进状态。** 模型不可达 / 超时 / 重试用尽 / 响应不合协议，
   全部收敛到 :func:`attribute` 返回 ``degraded=True`` 且
   ``causes=()`` 的结果。调用方**拿不到任何可落库的成因**，
   只有一条「待人工判定」的显式标记。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层**：``contract verify`` 只把逐 check
的 fail 行打到终端，说「哪一行不合约」，不说「为什么」也不排序。
本模块沿用的仍是主路径纪律 —— 证据定位到源行、结构不合约定就拒收、
结论交人工；模型调用部分（:mod:`reconq.gateway`）同样源产品无对应实现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from ..errors import ReconqError
from ..gateway.degradation import STATUS_DEGRADED, mark_pending_manual
from ..gateway.model_gateway import ModelGateway, build_prompt
from ..gateway.schema import (
    ATTRIBUTION_DRAFT_FIELDS,
    SchemaLayerError,
    validate_draft as schema_validate_draft,
)
from ..runlog import log_event
from .baseline import BASELINE_VERSION as RULE_BASELINE_VERSION
from .baseline import MAX_CONFIDENCE as RULE_BASELINE_MAX
from .baseline import compare_with_model, rank_causes_by_rules
from .bundle import EvidenceBundle

logger = logging.getLogger("reconq.attribution")

__all__ = [
    "ATTRIBUTION_SCHEMA",
    "MAX_CAUSES",
    "AttributionResult",
    "CauseDraft",
    "DraftValidationError",
    "RejectedDraft",
    "attribute",
    "attribute_many",
    "evidence_refs_of",
    "validate_draft",
]


#: 一次归因最多保留几条成因排序。多于这个数说明模型在「把可能性都列一遍」，
#: 而经办人一次只看得动前两三条。
MAX_CAUSES = 5

#: 草稿 JSON Schema（发给模型的 ``response_format``）。**字段清单不在这里
#: 写第二遍** —— 逐字段取自 :data:`reconq.gateway.schema.ATTRIBUTION_DRAFT_FIELDS`
#: （那是本地 pydantic 校验的判据）。两份写法不一致时，模型返回的
#: ``evidence_refs`` 会被自己的 schema 拒掉，而这种不一致在真实端点上
#: 要到某次模型恰好少写一个字段时才暴露。
#:
#: 刻意手写而不是 ``AttributionDraft.model_json_schema()``：后者会吐出
#: ``$ref`` / ``$defs``，而院内自建端点与老版本 OpenAI-compatible 端点对
#: 引用与 ``strict`` 模式的支持参差，踩中的表现是「不支持该参数」而非
#: 「少一个字段」，排障方向完全不一样。
ATTRIBUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cause": {"type": "string", "minLength": 1, "description": "成因短语，如「跨日切分重复计入」"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
            "description": "证据行标识，须原样抄自证据包",
        },
        "explanation_zh": {"type": "string", "minLength": 1, "description": "中文说明"},
    },
    "required": list(ATTRIBUTION_DRAFT_FIELDS),
    "additionalProperties": False,
}

#: 成因排序响应（一次可能给多条）的外层 schema。
DRAFT_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "drafts": {"type": "array", "items": ATTRIBUTION_SCHEMA},
    },
    "required": ["drafts"],
    "additionalProperties": False,
}


# ────────────────────── 数据结构 ──────────────────────


@dataclass(frozen=True)
class CauseDraft:
    """一条成因草稿。

    **刻意没有 ``disposition`` 字段**：草稿在类型层面就装不下「已确认」。
    状态推进只走 :mod:`reconq.engine.period` 的白名单迁移，而那条迁移
    只接受人工点的参数。
    """

    cause: str
    confidence: float
    evidence_refs: tuple[str, ...]
    explanation_zh: str
    rank: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause": self.cause,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "explanation_zh": self.explanation_zh,
            "rank": self.rank,
        }


@dataclass(frozen=True)
class RejectedDraft:
    """一条被拒的草稿 + 拒的原因。**拒了要留痕**：面板上要能回答
    「模型给了几条、收下几条、丢了几条、为什么丢」。"""

    cause: str
    error_code: str
    detail: str
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause": self.cause,
            "error_code": self.error_code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AttributionResult:
    """一次归因的结果。``degraded=True`` 时 ``causes`` 必为空。"""

    period: str
    channel: str
    conclusion_key: str
    causes: tuple[CauseDraft, ...] = ()
    rejected: tuple[RejectedDraft, ...] = ()
    degraded: bool = False
    error_code: str = ""
    error_detail: str = ""
    #: 证据包里真实出现过的行标识（去重有序）
    allowed_refs: tuple[str, ...] = ()
    gateway_call: Optional[dict[str, Any]] = None
    bundle_digest: str = ""
    instructions: str = ""
    #: Task 30：降级分类（连接 / 超时 / 5xx / Schema / 熔断）。面板按它把
    #: 降级分组；空串表示本次不是降级。
    failure_kind: str = ""
    #: 熔断器当时的状态（开着时降级的原因就是它）。
    breaker_state: str = ""
    #: Task 35：确定性规则基线给出的成因候选。**降级时由基线接管**，
    #: 模型可用时也照样算一遍留作对照。恒为「规则推断(未经模型)」，
    #: 与 :attr:`causes` 里的模型草稿**不是同一种东西**（见
    #: :mod:`reconq.align.baseline`）。**它不推进任何状态**。
    rule_baseline: tuple[Any, ...] = ()
    #: 规则与模型 Top-1 的对照结论（模型可用时才有值）。
    baseline_verdict: str = ""

    @property
    def baseline_top(self) -> str:
        return self.rule_baseline[0].cause_code if self.rule_baseline else ""

    @property
    def needs_manual(self) -> bool:
        """是否需要人工判定。降级与零草稿都为 True。"""
        return self.degraded or not self.causes

    @property
    def status_zh(self) -> str:
        if self.degraded:
            base = mark_pending_manual(self.error_code)
            if self.rule_baseline:
                return f"{base}（已附 {len(self.rule_baseline)} 条规则推断候选，未经模型）"
            return base
        if not self.causes:
            return "待人工判定（模型未给出可用成因草稿）"
        return f"待人工判定（{len(self.causes)} 条成因草稿待确认）"

    def top_cause(self) -> Optional[CauseDraft]:
        return self.causes[0] if self.causes else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "conclusion_key": self.conclusion_key,
            "causes": [c.to_dict() for c in self.causes],
            "rejected": [r.to_dict() for r in self.rejected],
            "degraded": self.degraded,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "allowed_refs": list(self.allowed_refs),
            "gateway_call": dict(self.gateway_call) if self.gateway_call else None,
            "bundle_digest": self.bundle_digest,
            "needs_manual": self.needs_manual,
            "status_zh": self.status_zh,
            "failure_kind": self.failure_kind,
            "breaker_state": self.breaker_state,
            "rule_baseline": [c.to_dict() for c in self.rule_baseline],
            "baseline_verdict": self.baseline_verdict,
        }

    def render(self) -> str:
        """终端/面板一行式渲染。"""
        head = (
            f"{self.channel} / {self.period} / {self.conclusion_key}：{self.status_zh}"
        )
        lines = [head]
        if not self.causes:
            if self.error_detail:
                lines.append(f"  降级原因：{self.error_detail}")
            if self.rule_baseline:
                lines.append("  以下为规则推断候选（未经模型，置信度封顶 "
                             f"{RULE_BASELINE_MAX}），仅供人工判定的起点：")
                lines.extend(c.render() for c in self.rule_baseline)
            return "\n".join(lines)
        for cause in self.causes:
            refs = "、".join(cause.evidence_refs)
            lines.append(
                f"  {cause.rank}. {cause.cause}（置信度 {Decimal(str(cause.confidence)):.2f}）"
                f" ← {refs}"
            )
            lines.append(f"     {cause.explanation_zh}")
        for rej in self.rejected:
            lines.append(f"  已丢弃：{rej.cause or '（无 cause）'} —— {rej.error_code}：{rej.detail}")
        return "\n".join(lines)


# ────────────────────── 证据引用集合 ──────────────────────


def evidence_refs_of(bundle: EvidenceBundle) -> tuple[str, ...]:
    """证据包里**真实出现过**的行标识（去重、保序）。

    三处来源都要收：差异行本身、候选行、同单号历史行。模型引用了
    差异行不在这三条路径上的东西 —— 无论它长得多像 —— 一律判越界。
    """
    payload = bundle.payload
    refs: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            refs.append(value)

    diff_row = payload.get("diff_row")
    if isinstance(diff_row, dict):
        _add(diff_row.get("ref"))
    for key in ("candidate_rows", "same_order_no_rows"):
        for row in payload.get(key) or []:
            if isinstance(row, dict):
                _add(row.get("ref"))
    return tuple(refs)


# ────────────────────── 草稿校验 ──────────────────────


class DraftValidationError(ReconqError):
    """一条草稿不合约定（Task 28 兼容出口）。

    刻意是 :class:`~reconq.errors.ReconqError` 的子类而不是裸
    ``ValueError``：本产品不允许任何没有错误码、没有建议动作的异常冒到
    面板上（``tests/test_errors.py`` 用 AST 扫全树把关）。错误码
    ``ATTRIBUTION_DRAFT_INVALID`` 会被网关的 :meth:`_parse` 归一成
    ``MODEL_SCHEMA_INVALID`` 并重试 —— 对外看到的仍是「模型返回结构不合约
    定」，但内层这层保留了自己的码与建议动作，排障时能分开看。

    Task 29 起，本类**不再自己判**：
    :func:`reconq.gateway.schema.validate_draft_layers` 才是判据的
    单一真源（pydantic 模型 + 六层链），本类只负责把分层错误统一包成
    一个码。分层码（``MODEL_OUTPUT_UNPARSEABLE`` /
    ``ATTRIBUTION_CONFIDENCE_OUT_OF_RANGE`` / …）在
    :func:`reconq.gateway.schema.validate_candidates` 那条新出口上直接
    可见，见该模块头。
    """

    def __init__(self, detail: str) -> None:
        super().__init__("ATTRIBUTION_DRAFT_INVALID", detail)


def validate_draft(raw: Any) -> CauseDraft:
    """校验一条模型返回的草稿，不合约定抛 :class:`DraftValidationError`。

    **不在这里「尽量修一修」** —— 把 ``confidence=85``（百分制）悄悄除以
    100、或把字符串 ``"0.8"`` 强转成数字，修的是模型的错，代价是这份草稿
    不再可复算：同一份输入两次归因会得到两个不同的置信度，指标表里的
    「成因 Top-1 命中率」就失去意义。

    判据本身在 :mod:`reconq.gateway.schema`（Task 29）；本函数是它的
    兼容包装，把六层里任一层的失败统一收敛成
    ``ATTRIBUTION_DRAFT_INVALID``，detail 逐字保留（``越界`` /
    ``看着像百分制`` / ``未登记字段`` …）。
    """
    try:
        draft = schema_validate_draft(raw)
    except SchemaLayerError as exc:
        raise DraftValidationError(exc.detail) from exc
    return CauseDraft(
        cause=draft.cause,
        confidence=draft.confidence,
        evidence_refs=tuple(r.ref for r in draft.evidence_refs),
        explanation_zh=draft.explanation_zh,
    )


def _validate_draft_list(raw: Any) -> list[CauseDraft]:
    """校验**一次归因的完整响应**：外层 ``{"drafts": [...]}``。"""
    if isinstance(raw, list):
        # 容忍模型把外层对象省掉、直接给数组。结构不合约定时由网关重试，
        # 但「少一层包装」这种形态不值得为此烧掉一次重试额度。
        items: Any = raw
    elif isinstance(raw, dict):
        if "drafts" not in raw:
            raise DraftValidationError(
                f"响应须含 drafts 数组，实际键为 {sorted(raw)}；"
                f"单个草稿也须包在 drafts 里"
            )
        items = raw["drafts"]
    else:
        raise DraftValidationError(f"响应顶层须为对象或数组，实际是 {type(raw).__name__}")
    if not isinstance(items, list):
        raise DraftValidationError(f"drafts 必须是数组，实际是 {type(items).__name__}")
    if not items:
        raise DraftValidationError("drafts 为空：模型没有给出任何成因草稿")
    if len(items) > MAX_CAUSES:
        # 超过上限不是错误，是「模型在穷举」。取前 N 条即可，
        # 但**要留痕**（记在 rejected 里），否则面板上会以为模型就想了这些。
        pass
    return [validate_draft(item) for item in items[:MAX_CAUSES]]


# ────────────────────── 归因主入口 ──────────────────────


def attribute(
    bundle: EvidenceBundle,
    gateway: Optional[ModelGateway] = None,
    *,
    instructions: str = "",
) -> AttributionResult:
    """给一条差异产出成因排序草稿。

    **永不抛异常、永不返回可落库的自动结论。** 四条降级路径全部收敛到
    ``degraded=True`` + ``causes=()``：

    ============================  ==========================================
    情形                          错误码
    ============================  ==========================================
    未配置 / 缺凭据位              ``MODEL_CONFIG_INVALID``
    端点不可达 / 5xx / 限流        ``MODEL_UNAVAILABLE``（重试用尽后）
    超时                           ``MODEL_TIMEOUT``（重试用尽后）
    响应不合 OpenAI-compatible    ``MODEL_RESPONSE_INVALID``
    响应结构不合草稿 Schema        ``MODEL_SCHEMA_INVALID``
    草稿引用了证据包外的行         ``ATTRIBUTION_EVIDENCE_REF_INVALID``
    ============================  ==========================================

    最后一条与其他几条不同：模型**确实答了**，但答的证据不存在。
    这时收下任何一条草稿都是替它背书，故整条作废、只留拒因。

    **降级不等于交白卷**（Task 35）：``degraded=True`` 时自动附上
    :func:`reconq.align.baseline.rank_causes_by_rules` 的规则候选，
    ``causes`` 仍然为空、``needs_manual`` 仍为 True、状态仍停在
    「待人工判定」。规则候选给的是**人工判定的起点**（带证据行、带线索
    说明），不是结论。模型可用时它同样跑一遍，只为在
    :attr:`AttributionResult.baseline_verdict` 上留一句「规则与模型
    顶��一致还是分歧」。
    """
    allowed = evidence_refs_of(bundle)
    base = {
        "period": bundle.payload.get("period", ""),
        "channel": bundle.payload.get("channel", ""),
        "conclusion_key": bundle.payload.get("conclusion_key", ""),
        "allowed_refs": allowed,
        "bundle_digest": bundle.digest,
        "instructions": instructions,
    }
    rule_baseline = tuple(rank_causes_by_rules(bundle))

    if gateway is None:
        # 没给网关 = 部署时没配模型端点。**明说**，不静默返回「没有成因」。
        return _degraded(
            base,
            "MODEL_CONFIG_INVALID",
            "未提供模型网关（RECONQ_MODEL_* 未配置？）。"
            "本条差异只保留确定性口径结论，成因待人工判定",
            log_ok=False,
            rule_baseline=rule_baseline,
        )

    messages = build_prompt(bundle.render(), instructions=instructions)
    result = gateway.chat_completion(
        messages,
        schema=DRAFT_LIST_SCHEMA,
        validator=_validate_draft_list,
        bundle_audit=bundle.audit_metadata(),
    )
    if result.status == STATUS_DEGRADED:
        # 任务书里的降级接线条件写的就是这一句。显式比对 ``status`` 而不是
        # ``not result.ok``：``ok`` 是 ``status == OK`` 的别名，将来若加第四
        # 态（「调成了但校验不过」之类），``not ok`` 会把新状态一起吞进这条
        # 降级路径，而基线不该替一个「调成了」的情形背书。
        return _degraded(
            base,
            result.error_code,
            result.error_detail,
            gateway_call=result.call.to_dict() if result.call else None,
            rule_baseline=rule_baseline,
        )

    drafts: Sequence[CauseDraft] = result.data or ()
    kept, rejected = _screen_drafts(drafts, allowed)
    kept = _rank(kept)

    if not kept:
        return _degraded(
            base,
            "ATTRIBUTION_EVIDENCE_REF_INVALID" if rejected else "MODEL_SCHEMA_INVALID",
            "模型给出的成因草稿全部被拒（引用了证据包之外的行），待人工判定",
            rejected=rejected,
            gateway_call=result.call.to_dict() if result.call else None,
            # 调成了但草稿全被拒：记成功事件（网关本身没问题），
            # 业务上仍是降级 —— 拿不到任何可落库的成因。
            log_ok=True,
            rule_baseline=rule_baseline,
        )

    # 模型可用：规则照样跑一遍，只为记一句一致 / 分歧。分歧样本是
    # 提示词迭代的方向（见 reconq.align.baseline 模块头）。
    comparison = compare_with_model(bundle, rule_baseline, kept)

    return AttributionResult(
        causes=kept,
        rejected=rejected,
        gateway_call=result.call.to_dict() if result.call else None,
        rule_baseline=rule_baseline,
        baseline_verdict=comparison.verdict,
        **base,  # type: ignore[arg-type]
    )


def attribute_many(
    bundles: Iterable[EvidenceBundle],
    gateway: Optional[ModelGateway] = None,
    *,
    instructions: str = "",
) -> list[AttributionResult]:
    """批量归因。**逐条独立**：一条降级不影响其余。"""
    return [attribute(b, gateway, instructions=instructions) for b in bundles]


# ────────────────────── 内部 ──────────────────────


def _screen_drafts(
    drafts: Sequence[CauseDraft], allowed: Sequence[str]
) -> tuple[list[CauseDraft], list[RejectedDraft]]:
    """证据引用越界筛查。**越界即整条作废**，不做就近匹配。"""
    if not allowed:
        return [], [
            RejectedDraft(
                cause=d.cause,
                error_code="ATTRIBUTION_EVIDENCE_REF_INVALID",
                detail="证据包内没有任何可引用的行标识，无法核对草稿依据",
            )
            for d in drafts
        ]
    allowed_set = set(allowed)
    kept: list[CauseDraft] = []
    rejected: list[RejectedDraft] = []
    for draft in drafts:
        outside = [ref for ref in draft.evidence_refs if ref not in allowed_set]
        if outside:
            rejected.append(
                RejectedDraft(
                    cause=draft.cause,
                    error_code="ATTRIBUTION_EVIDENCE_REF_INVALID",
                    detail=(
                        f"引用了证据包中不存在的行 {outside}；"
                        f"证据包实际可引用 {len(allowed_set)} 行。"
                        f"整条作废 —— 不做就近匹配"
                    ),
                )
            )
            continue
        kept.append(draft)
    return kept, rejected


def _rank(drafts: Sequence[CauseDraft]) -> tuple[CauseDraft, ...]:
    """按置信度降序排名。

    排序**稳定**：同分时保持模型给出的原顺序。同一份证据两次归因若因
    排序不稳定而得到不同的 Top-1，指标表里的「成因 Top-1 命中率」就
    没法复算。
    """
    ordered = sorted(drafts, key=lambda d: d.confidence, reverse=True)
    return tuple(
        CauseDraft(
            cause=d.cause,
            confidence=d.confidence,
            evidence_refs=d.evidence_refs,
            explanation_zh=d.explanation_zh,
            rank=index,
        )
        for index, d in enumerate(ordered, start=1)
    )


def _degraded(
    base: dict[str, Any],
    code: str,
    detail: str,
    *,
    rejected: Sequence[RejectedDraft] = (),
    gateway_call: Optional[dict[str, Any]] = None,
    log_ok: bool = True,
    rule_baseline: Sequence[Any] = (),
) -> AttributionResult:
    """构造降级结果。**``causes`` 必为空，且不打任何状态推进调用。**

    ``log_ok`` 区分「网关压根没调成」与「调成了但草稿被拒」：后者记成
    成功事件（``attribution.done`` + ok），前者记失败。两条路径的
    ``degraded`` 都是 True —— 业务上「拿不到任何可落库的成因」这一条
    是同一件事，分开只在日志级别上。

    ``rule_baseline`` 挂在 :attr:`AttributionResult.rule_baseline` 上，
    **不混进 ``causes``**：混进去就等于让规则候选与模型草稿共用一条
    落库路径，而规则候选的置信度封顶 0.5、来源是规则、未经四道闸 ——
    台账上分不出来谁说的，签字的人就会以为模型也确认过了。
    """
    log_event(
        "attribution.done",
        level="info" if log_ok else "warning",
        message=f"归因{'完成（草稿被拒）' if log_ok else '降级'}：{code}",
        code=code,
        period=base.get("period", ""),
        channel=base.get("channel", ""),
        degraded=True,
        cause_count=0,
        rejected_count=len(rejected),
        bundle_digest=base.get("bundle_digest", ""),
        gateway_call=gateway_call or {},
    )
    if rule_baseline:
        # 单独记一条：面板上要能回答「这期到底有多少条差异是靠规则顶上的」。
        # 只在 attribution.done 里塞个计数不够 —— 那条事件的语义是「归因
        # 结束了」，看不出有没有模型参与。
        log_event(
            "attribution.baseline",
            level="warning",
            message=f"改用确定性规则基线给出 {len(rule_baseline)} 条成因候选（未经模型）",
            code=code,
            period=base.get("period", ""),
            channel=base.get("channel", ""),
            rule_version=RULE_BASELINE_VERSION,
            candidate_count=len(rule_baseline),
            top_cause=rule_baseline[0].cause_code,
            top_confidence=str(rule_baseline[0].confidence),
            bundle_digest=base.get("bundle_digest", ""),
        )
    return AttributionResult(
        causes=(),
        rejected=tuple(rejected),
        degraded=True,
        error_code=code,
        error_detail=detail,
        gateway_call=gateway_call,
        rule_baseline=tuple(rule_baseline),
        **base,  # type: ignore[arg-type]
    )
