"""归因效果评测：模型档 vs 规则基线档（Task 39 · ``ai_role: core`` 的效果验收）。

为什么归因必须有一份可复现的效果评测
------------------------------------
本产品的成因归因是 ``ai_role: core`` —— 开放集合的成因靠模型给，而模型档
拿不到「说对了没有」这个反馈：单看一条草稿很通顺，落到台账上才发现十条里
有三条归错了桶，而归错桶的代价是让经办人拿着错误的排查方向去问收费处。
:mod:`reconq.align.replay`（Task 34）解决的是「改了提示词之后是变好还是
变坏」，**两版之间**可比；本模块解决的是「模型档相对规则档到底值不值」，
需要一份**人工标注的正确成因**当尺子。

四档指标，逐条写清分子分母
------------------------
::

    top1_rate
        Top-1 成因码落在期望集合内。分母 = **可评条数**（出了草稿且 Top-1
        落在受控字典内），不是总条数 —— 全被拒的那几条不参与平均，否则
        「让所有草稿都过不了 Schema」反而会让命中率好看一点。

    top3_rate
        Top-3 内任一成因码落在期望集合内。它比 Top-1 宽松，但**不能拿它
        替代 Top-1 汇报**：经办人签字时看的是第一条。门禁因此卡 Top-3 而
        不是 Top-1（见 :meth:`TierMetrics.gate_failures`）。

    unknown_refusal_rate（反幻觉拒绝率）
        期望 UNKNOWN（``expect_unknown=True``）的样本里，Top-1 真的是
        ``undetermined`` 的比例。**分母只取 UNKNOWN 样本** —— 混进全部
        样本会让这个数字被大量「本来该给具体成因」的平凡 case 稀释。

        「给了具体成因」在这里记为**失败**而不是「不算」：证据不足时
        硬猜一条最像的，比说「我判不出来」贵得多 —— 前者让人签字，
        后者让人去查证。

    mean_confidence
        Top-1 置信度的均值。**只在「Top-1 答对」的样本上取**，答错的
        置信度混进来会把一个毫无意义的数字抬起来：一个把成因判错却给
        0.95 的模型，均值越高越危险。

成本维度（供科室权衡）
--------------------
两档各记：平均耗时、单条 token、单条成本、总耗时。**规则档不发网络
请求，成本恒为 0 且 ``priced=True``**（真源是零网络、零 token，不是
「不知道单价」）；**模型档单价从环境变量读，缺位即 ``priced=False``**，
报告里显示「未配置单价」而不是一个看起来完整的 0 元 —— 那是
:mod:`reconq.audit.model_calls` 里 ``priced`` 字段的同一个口径。

门禁
----
**模型档 Top-3 命中率不得低于规则基线档。** 规则档是「不调模型时能做到
的下限」，模型档比它差就说明这一层的模型调用没有挣回成本。
（这条门禁**不**要求模型档 Top-1 更高：规则在「形态 → 成因」这类
确定性映射上本就强，模型的优势在弱匹配与 UNKNOWN 拒答上，两条指标各说
各的，混成一条会逼着人去做「让模型在规则本来就对的地方也赢」这种
既无业务价值又容易过拟合的优化。）

模型档数据从哪来
---------------
产品代码走的是**真实** OpenAI-compatible 协议：真实 URL 拼法、真实
``response_format``、真实 Schema 六层校验、真实证据引用越界作废、真实
降级路径。评测时默认把 HTTP 传输层换成
:class:`httpx.MockTransport` 回放 ``responses.json``（录制响应替身，
**不是任何真实模型的输出**，见该文件 ``note_zh``）。配好
``RECONQ_MODEL_*`` 后用 ``--live`` 走真实端点重录，评测口径一字不改。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Sequence

from ..audit.model_calls import PRICE_ENV_VARS, ModelUsage
from ..errors import ReconqError
from ..gateway.model_gateway import GatewayConfig, ModelGateway
from .attribution import AttributionResult, attribute, evidence_refs_of
from .baseline import BASELINE_VERSION, RULE_SOURCE, rank_causes_by_rules
from .bundle import EvidenceBundle, bundle_from_payload
from .causes import CAUSES, cause_vocabulary, screen_hits
from .prompts import PROMPT_VERSION, prompt_digest, template_of

__all__ = [
    "DEFAULT_EVAL_DIR",
    "MIN_EVAL_CASES",
    "MIN_UNKNOWN_CASES",
    "CaseVerdict",
    "EvalCase",
    "EvalSet",
    "TierMetrics",
    "build_live_gateway",
    "build_replay_gateway",
    "evaluate_eval_set",
    "load_eval_set",
    "render_report",
    "write_eval_report",
]


#: 评测集默认目录（与生成脚本 ``tests/fixtures/attrib/golden/`` 同源）
DEFAULT_EVAL_DIR = os.path.join("tests", "fixtures", "attrib", "golden")

#: 样本条数下限。低于这个数**直接拒评**（``ATTRIB_EVAL_SET_TOO_SMALL``）：
#: 一条差异偶然对一次是常事，43 条里漏 3 条与漏 13 条才是两种信息。
MIN_EVAL_CASES = 40

#: UNKNOWN 期望样本条数下限。低于这个数，反幻觉拒绝率就没有意义 ——
#: 2 条里对 1 条是 50%，那是抛硬币，不是拒答能力。
MIN_UNKNOWN_CASES = 5

#: 「判不出来」在受控字典里的码。评测集判 UNKNOWN 时期望的就是它。
UNKNOWN_CODE = "undetermined"

#: 模型档的两种跑法。报告里必须写明用了哪一种，否则「Top-3 命中率 0.9x」
#: 这句话在换一台机器后含义就变了。
RUN_MODES = {
    "replay": "回放录制响应替身（httpx.MockTransport，不发真实请求）",
    "live": "实连真实模型端点（RECONQ_MODEL_*）",
}


# ─────────────────────────── 评测集 ───────────────────────────


@dataclass(frozen=True)
class EvalCase:
    """一条自足的归因评测样本。

    自足是刻意的：命中与未命中都要能落到**具体某一条**上，而不是一个
    必须整批重跑才复现的数字。``bundle`` 是逐字节固定的 payload，
    重建走 :func:`reconq.align.bundle.bundle_from_payload`（重渲染、
    无时间戳与序号），同一份样本任何时候重建都得到同一个 digest。
    """

    case_id: str
    name_zh: str
    channel: str
    layer: str
    kind: str
    bundle: EvidenceBundle
    expect_codes: tuple[str, ...]
    expect_unknown: bool
    required_refs: tuple[str, ...]
    min_refs: int = 1
    note: str = ""
    #: 录制响应的声明行为（``correct`` / ``correct_at_2`` / ``miss`` /
    #: ``refuse`` / ``hallucinate``）。**只用于报告里点名某条**，不参与
    #: 打分 —— 打分只看实际输出与期望，不看「本来打算让它怎样」。
    behaviour: str = ""

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ReconqError("ATTRIB_EVAL_CASE_INVALID", "评测样本缺 case_id")
        if not self.expect_codes:
            raise ReconqError(
                "ATTRIB_EVAL_CASE_INVALID",
                f"评测样本 {self.case_id} 的 expect_codes 为空："
                "没有期望就没有尺子，评测它等于什么都没量",
            )
        unknown = set(self.expect_codes) == {UNKNOWN_CODE}
        if self.expect_unknown and not unknown:
            raise ReconqError(
                "ATTRIB_EVAL_CASE_INVALID",
                f"评测样本 {self.case_id} 标了 expect_unknown，"
                f"但 expect_codes={list(self.expect_codes)} 不是恰好 "
                f"['{UNKNOWN_CODE}']。这两者说的是同一件事，"
                "标错一个会让反幻觉拒绝率的分母算错",
            )
        if unknown != self.expect_unknown:
            raise ReconqError(
                "ATTRIB_EVAL_CASE_INVALID",
                f"评测样本 {self.case_id} 的 expect_codes={list(self.expect_codes)} "
                f"与 expect_unknown={self.expect_unknown} 不自洽："
                f"只有恰好 ['{UNKNOWN_CODE}'] 才算 UNKNOWN 期望",
            )
        if not evidence_refs_of(self.bundle):
            raise ReconqError(
                "ATTRIB_EVAL_CASE_INVALID",
                f"评测样本 {self.case_id} 的证据包没有任何可引用行标识，"
                "无法核对模型引用了什么",
            )

    @property
    def expected_set(self) -> frozenset[str]:
        return frozenset(self.expect_codes)

    def describe(self) -> str:
        return f"{self.case_id}[{self.layer}] → {'/'.join(self.expect_codes)}"


@dataclass(frozen=True)
class EvalSet:
    """评测集 + 录制响应 + 标注元数据。"""

    path: str
    cases: tuple[EvalCase, ...]
    responses: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def unknown_total(self) -> int:
        return sum(1 for c in self.cases if c.expect_unknown)

    @property
    def periods(self) -> tuple[str, ...]:
        return tuple(sorted({str(c.bundle.payload.get("period", "")) for c in self.cases}))

    def layer_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.layer] = counts.get(case.layer, 0) + 1
        return dict(sorted(counts.items()))

    def channel_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.channel] = counts.get(case.channel, 0) + 1
        return dict(sorted(counts.items()))

    def behaviour_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            if case.behaviour:
                counts[case.behaviour] = counts.get(case.behaviour, 0) + 1
        return dict(sorted(counts.items()))

    def cause_coverage(self) -> dict[str, int]:
        """受控字典 10 条各自出现在多少条样本的期望集合里。"""
        counts: dict[str, int] = {code: 0 for code in cause_vocabulary()}
        for case in self.cases:
            for code in case.expect_codes:
                counts[code] = counts.get(code, 0) + 1
        return counts

    def case_of(self, case_id: str) -> EvalCase:
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise ReconqError(
            "ATTRIB_EVAL_CASE_NOT_FOUND",
            f"评测集里没有样本 {case_id!r}；现有 {self.total} 条",
        )


def _read_json(path: str, *, required: bool = True) -> dict[str, Any]:
    if not os.path.exists(path):
        if required:
            raise ReconqError(
                "ATTRIB_EVAL_FIXTURE_MISSING",
                f"评测夹具不存在：{path}。跑 "
                "PYTHONPATH=src python3 tests/fixtures/attrib/golden/generate_eval_set.py 生成",
            )
        return {}
    with open(path, encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError as exc:
            raise ReconqError(
                "ATTRIB_EVAL_FIXTURE_INVALID",
                f"评测夹具 {path} 不是合法 JSON：{exc}",
            ) from exc


def load_eval_set(
    directory: str = DEFAULT_EVAL_DIR,
    *,
    min_cases: int = MIN_EVAL_CASES,
    min_unknown: int = MIN_UNKNOWN_CASES,
) -> EvalSet:
    """读 ``cases.jsonl`` / ``responses.json`` / ``golden_meta.json``。

    三道下限在**加载期**就拒（条数不足 / UNKNOWN 不足 / 期望不自洽）：
    放到评测跑完再报「样本量偏小」已经晚了 —— 报告已经打给人看了。
    """
    cases_path = os.path.join(directory, "cases.jsonl")
    if not os.path.exists(cases_path):
        raise ReconqError(
            "ATTRIB_EVAL_FIXTURE_MISSING",
            f"评测集 {cases_path} 不存在。跑 "
            "PYTHONPATH=src python3 tests/fixtures/attrib/golden/generate_eval_set.py 生成",
        )

    cases: list[EvalCase] = []
    seen: set[str] = set()
    with open(cases_path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ReconqError(
                    "ATTRIB_EVAL_FIXTURE_INVALID",
                    f"{cases_path} 第 {line_no} 行不是合法 JSON：{exc}",
                ) from exc
            cases.append(_parse_case(raw, where=f"{cases_path}:{line_no}"))
            if cases[-1].case_id in seen:
                raise ReconqError(
                    "ATTRIB_EVAL_FIXTURE_INVALID",
                    f"{cases_path} 第 {line_no} 行 case_id 重复：{cases[-1].case_id!r}。"
                    "重复会让同一条被计两次，两次还可能算出两个不同结论",
                )
            seen.add(cases[-1].case_id)

    unknown_total = sum(1 for c in cases if c.expect_unknown)
    if len(cases) < min_cases:
        raise ReconqError(
            "ATTRIB_EVAL_SET_TOO_SMALL",
            f"归因评测集只有 {len(cases)} 条，下限 {min_cases} 条。"
            "一条差异偶然对一次是常事，条数不够就分不出「真变好了」与"
            "「这次运气好」；宁可先补标注，也不要用小样本报一个好看的数字",
        )
    if unknown_total < min_unknown:
        raise ReconqError(
            "ATTRIB_EVAL_SET_TOO_SMALL",
            f"归因评测集里期望 UNKNOWN 的只有 {unknown_total} 条，下限 {min_unknown} 条。"
            "反幻觉拒绝率的分母就是它 —— 2 条里对 1 条是 50%，那是抛硬币，"
            "不是拒答能力",
        )

    responses_doc = _read_json(os.path.join(directory, "responses.json"))
    meta = _read_json(os.path.join(directory, "golden_meta.json"), required=False)
    responses = dict(responses_doc.get("responses") or {})

    return EvalSet(
        path=cases_path,
        cases=tuple(cases),
        responses=responses,
        model=str(responses_doc.get("model", "")),
        prompt_version=str(responses_doc.get("prompt_version") or PROMPT_VERSION),
        meta=meta,
    )


def _parse_case(raw: Mapping[str, Any], *, where: str) -> EvalCase:
    def _need(key: str) -> Any:
        if key not in raw:
            raise ReconqError(
                "ATTRIB_EVAL_CASE_INVALID",
                f"{where}：样本 {raw.get('case_id', '?')!r} 缺字段 {key}",
            )
        return raw[key]

    payload = _need("bundle")
    if not isinstance(payload, Mapping):
        raise ReconqError(
            "ATTRIB_EVAL_CASE_INVALID",
            f"{where}：bundle 不是对象（实际 {type(payload).__name__}）",
        )
    refs = tuple(str(r) for r in (_need("required_refs") or ()))
    try:
        bundle = bundle_from_payload(payload)
        case = EvalCase(
            case_id=str(_need("case_id")),
            name_zh=str(raw.get("name_zh") or ""),
            channel=str(_need("channel")),
            layer=str(raw.get("layer") or ""),
            kind=str(raw.get("kind") or ""),
            bundle=bundle,
            expect_codes=tuple(str(c) for c in _need("expect_codes")),
            expect_unknown=bool(raw.get("expect_unknown", False)),
            required_refs=refs,
            min_refs=int(raw.get("min_refs") or 1),
            note=str(raw.get("note") or ""),
            behaviour=str(raw.get("behaviour") or ""),
        )
    except ReconqError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise ReconqError(
            "ATTRIB_EVAL_CASE_INVALID", f"{where}：样本结构不合法：{exc}"
        ) from exc
    return case


# ─────────────────────────── 逐条结论 ───────────────────────────


@dataclass(frozen=True)
class CaseVerdict:
    """一条样本在一档上的结论。"""

    case_id: str
    tier: str
    top1_code: str = ""
    top3_codes: tuple[str, ...] = ()
    top1_hit: bool = False
    top3_hit: bool = False
    top1_confidence: float = 0.0
    cited_refs: tuple[str, ...] = ()
    ref_hit: bool = False
    #: 期望 UNKNOWN 的样本：Top-1 是不是 undetermined
    unknown_resolved: Optional[bool] = None
    #: 期望 UNKNOWN 的样本：Top-1 是不是给了个具体成因（= 硬猜）
    hallucinated: bool = False
    #: 这一条有没有可评的产出（出了草稿且 Top-1 落在受控字典内）
    usable: bool = False
    degraded: bool = False
    error_code: str = ""
    elapsed_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: Decimal = Decimal("0")
    priced: bool = True
    kept: int = 0
    rejected: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "tier": self.tier,
            "top1_code": self.top1_code,
            "top3_codes": list(self.top3_codes),
            "top1_hit": self.top1_hit,
            "top3_hit": self.top3_hit,
            "top1_confidence": self.top1_confidence,
            "cited_refs": list(self.cited_refs),
            "ref_hit": self.ref_hit,
            "unknown_resolved": self.unknown_resolved,
            "hallucinated": self.hallucinated,
            "usable": self.usable,
            "degraded": self.degraded,
            "error_code": self.error_code,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost": str(self.cost),
            "priced": self.priced,
            "kept": self.kept,
            "rejected": self.rejected,
        }


# ─────────────────────────── 两档指标 ───────────────────────────


@dataclass
class TierMetrics:
    """一档在整份评测集上的全部指标。"""

    tier: str
    run_mode: str = ""
    total: int = 0
    usable: int = 0
    top1_hits: int = 0
    top3_hits: int = 0
    ref_hits: int = 0
    #: UNKNOWN 样本数 / 其中 Top-1 真是 undetermined 的条数
    unknown_total: int = 0
    unknown_resolved: int = 0
    hallucinated: int = 0
    #: Top-1 答对的那些条的置信度之和与条数（答错的不参与）
    correct_confidence_sum: float = 0.0
    correct_confidence_count: int = 0
    degraded: int = 0
    elapsed_total_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: Decimal = Decimal("0")
    priced: bool = True
    verdicts: tuple[CaseVerdict, ...] = ()

    # —— 四档指标 ——

    @property
    def top1_rate(self) -> float:
        return _ratio(self.top1_hits, self.usable)

    @property
    def top3_rate(self) -> float:
        return _ratio(self.top3_hits, self.usable)

    @property
    def ref_rate(self) -> float:
        return _ratio(self.ref_hits, self.usable)

    @property
    def unknown_refusal_rate(self) -> float:
        """反幻觉拒绝率。**分母只取 UNKNOWN 样本**。"""
        return _ratio(self.unknown_resolved, self.unknown_total)

    @property
    def hallucination_rate(self) -> float:
        return _ratio(self.hallucinated, self.unknown_total)

    @property
    def mean_confidence(self) -> float:
        """Top-1 **答对**那些条的置信度均值。

        刻意不取全部：把答错的高置信度算进来，得到的数字越高越危险 ——
        一个把成因判错却给 0.95 的模型，均值不该被拿去汇报。
        """
        if not self.correct_confidence_count:
            return 0.0
        return self.correct_confidence_sum / self.correct_confidence_count

    @property
    def avg_elapsed_ms(self) -> float:
        return _ratio_f(self.elapsed_total_ms, self.total)

    @property
    def avg_cost(self) -> Decimal:
        if not self.total:
            return Decimal("0")
        return self.cost / Decimal(self.total)

    @property
    def avg_prompt_tokens(self) -> float:
        return _ratio_f(self.prompt_tokens, self.total)

    @property
    def avg_completion_tokens(self) -> float:
        return _ratio_f(self.completion_tokens, self.total)

    @property
    def cost_text(self) -> str:
        """单条平均成本。**单价缺位就说未配置**，不显示 0 元。"""
        if not self.priced:
            return "未配置单价"
        return f"{self.avg_cost.quantize(Decimal('0.000001'))} 元/条"

    @property
    def total_cost_text(self) -> str:
        if not self.priced:
            return "未配置单价"
        return f"{self.cost.quantize(Decimal('0.000001'))} 元"

    def gate_failures(self, baseline: Optional["TierMetrics"] = None) -> list[str]:
        """门禁未过的项。**空列表 = 通过**。

        唯一硬门禁：**Top-3 命中率不得低于规则基线档**。规则档是
        「完全不调模型时能做到的下限」，模型档比它差，这一层的模型调用
        就没有挣回成本 —— 付了钱、慢了、还没更准。

        刻意**不**要求 Top-1 更高、也不要求拒答率更高：规则在
        「形态 → 成因」这类确定性映射上本就强，模型的优势在弱匹配与
        UNKNOWN 拒答上。把这两条也写成硬门禁，会逼着人去做「让模型在
        规则本来就对的地方也赢」这种既无业务价值又容易过拟合的优化，
        最后变成对着评测集调提示词。
        """
        failures: list[str] = []
        if baseline is not None:
            if self.top3_rate < baseline.top3_rate:
                failures.append(
                    f"模型档 Top-3 命中率 {self.top3_rate:.1%} 低于规则基线档 "
                    f"{baseline.top3_rate:.1%}：付了模型的钱、慢了，结论还更差"
                )
        return failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "run_mode": self.run_mode,
            "total": self.total,
            "usable": self.usable,
            "top1_hits": self.top1_hits,
            "top3_hits": self.top3_hits,
            "ref_hits": self.ref_hits,
            "top1_rate": round(self.top1_rate, 4),
            "top3_rate": round(self.top3_rate, 4),
            "ref_rate": round(self.ref_rate, 4),
            "unknown_total": self.unknown_total,
            "unknown_resolved": self.unknown_resolved,
            "unknown_refusal_rate": round(self.unknown_refusal_rate, 4),
            "hallucinated": self.hallucinated,
            "hallucination_rate": round(self.hallucination_rate, 4),
            "mean_confidence": round(self.mean_confidence, 4),
            "degraded": self.degraded,
            "elapsed_total_ms": round(self.elapsed_total_ms, 3),
            "avg_elapsed_ms": round(self.avg_elapsed_ms, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "avg_prompt_tokens": round(self.avg_prompt_tokens, 2),
            "avg_completion_tokens": round(self.avg_completion_tokens, 2),
            "cost": str(self.cost),
            "avg_cost": str(self.avg_cost),
            "priced": self.priced,
            "cost_text": self.cost_text,
            "total_cost_text": self.total_cost_text,
        }


def _ratio(numerator: int, denominator: int) -> float:
    """比例。**分母为 0 返回 0.0 而不是 1.0** —— 一条 UNKNOWN 样本都没有时
    「拒答率 100%」是本产品最危险的一个假数字。"""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _ratio_f(numerator: float, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


# ─────────────────────────── 跑两档 ───────────────────────────


def _score(
    case: EvalCase,
    tier: str,
    codes: Sequence[str],
    confidences: Sequence[float],
    cited: Sequence[str],
    *,
    kept: int,
    rejected: int,
    usable: bool,
    degraded: bool,
    error_code: str,
    elapsed_ms: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: Decimal = Decimal("0"),
    priced: bool = True,
) -> CaseVerdict:
    top1 = codes[0] if codes else ""
    top3 = tuple(codes[:3])
    expected = case.expected_set
    top1_hit = bool(top1) and top1 in expected
    top3_hit = any(code in expected for code in top3)
    ref_hit = (
        bool(cited)
        and set(case.required_refs) <= set(cited)
        and len(cited) >= case.min_refs
    )
    unknown_resolved: Optional[bool] = None
    hallucinated = False
    if case.expect_unknown:
        # 只看 Top-1：模型在第 2 位也写了 undetermined 但 Top-1 给了具体
        # 成因，经办人签字时看的是第一条，仍算硬猜。
        unknown_resolved = top1 == UNKNOWN_CODE
        hallucinated = bool(top1) and top1 != UNKNOWN_CODE
    return CaseVerdict(
        case_id=case.case_id,
        tier=tier,
        top1_code=top1,
        top3_codes=top3,
        top1_hit=top1_hit,
        top3_hit=top3_hit,
        top1_confidence=confidences[0] if confidences else 0.0,
        cited_refs=tuple(cited),
        ref_hit=ref_hit,
        unknown_resolved=unknown_resolved,
        hallucinated=hallucinated,
        usable=usable,
        degraded=degraded,
        error_code=error_code,
        elapsed_ms=elapsed_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        priced=priced,
        kept=kept,
        rejected=rejected,
    )


def _accumulate(metrics: TierMetrics, verdict: CaseVerdict) -> None:
    metrics.total += 1
    metrics.elapsed_total_ms += verdict.elapsed_ms
    metrics.prompt_tokens += verdict.prompt_tokens
    metrics.completion_tokens += verdict.completion_tokens
    if verdict.priced:
        metrics.cost += verdict.cost
    else:
        metrics.priced = False
    if verdict.degraded:
        metrics.degraded += 1
    if verdict.usable:
        metrics.usable += 1
        if verdict.top1_hit:
            metrics.top1_hits += 1
            metrics.correct_confidence_sum += verdict.top1_confidence
            metrics.correct_confidence_count += 1
        if verdict.top3_hit:
            metrics.top3_hits += 1
        if verdict.ref_hit:
            metrics.ref_hits += 1
    if verdict.unknown_resolved is not None:
        metrics.unknown_total += 1
        if verdict.unknown_resolved:
            metrics.unknown_resolved += 1
        if verdict.hallucinated:
            metrics.hallucinated += 1


def run_rule_tier(eval_set: EvalSet) -> TierMetrics:
    """规则基线档：**零网络、零模型、零状态**。

    走的是 :func:`reconq.align.baseline.rank_causes_by_rules` 本体 ——
    与 Task 35 降级兜底、Task 28 对照结论用的是**同一份实现**，不是
    另写一份「评测专用规则」。评测专用的规则会漂：线上兜底改了、评测
    还按老规则算，报告就会说「规则档没问题」而线上早就变了。
    """
    metrics = TierMetrics(tier="规则基线档", run_mode="local")
    for case in eval_set.cases:
        started = time.perf_counter()
        try:
            candidates = rank_causes_by_rules(case.bundle)
        except Exception:  # noqa: BLE001 - 规则档崩了也要给出可读结论
            verdict = _score(
                case,
                metrics.tier,
                [],
                [],
                [],
                kept=0,
                rejected=0,
                usable=False,
                degraded=True,
                error_code="RULE_BASELINE_FAILED",
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
            _accumulate(metrics, verdict)
            metrics.verdicts = metrics.verdicts + (verdict,)
            continue
        elapsed = (time.perf_counter() - started) * 1000
        codes = [c.cause_code for c in candidates]
        # 规则候选已经过受控字典构造（构造期就拒自造码），这里再走一次
        # screen_hits 是为了让两档走**同一个归一出口** —— 否则「模型档
        # 的码被 screen_hits 改写过、规则档的没有」会成为两档唯一的差别。
        hits, _ = screen_hits(candidates, period=str(case.bundle.payload.get("period", "")))
        normalized = {h.cause_code: h for h in hits}
        codes = [code for code in codes if code in normalized] or codes
        confidences = [
            next((c.confidence for c in candidates if c.cause_code == code), 0.0)
            for code in codes
        ]
        cited: list[str] = []
        for cand in candidates:
            cited.extend(cand.evidence_refs)
        verdict = _score(
            case,
            metrics.tier,
            codes,
            confidences,
            list(dict.fromkeys(cited)),
            kept=len(candidates),
            rejected=0,
            usable=bool(codes),
            degraded=False,
            error_code="",
            elapsed_ms=elapsed,
            # 规则档真源是零网络零 token：priced=True 而不是「不知道单价」。
            # 把它标成未定价会让「模型档比规则档贵」这句话算不出倍数。
            cost=Decimal("0"),
            priced=True,
        )
        _accumulate(metrics, verdict)
        metrics.verdicts = metrics.verdicts + (verdict,)
    return metrics


def build_replay_gateway(
    record: Mapping[str, Any],
    *,
    model_name: str = "reconq-attribution-eval-standin",
    calls: Optional[list[Any]] = None,
) -> ModelGateway:
    """把一条录制响应装进**真实** :class:`ModelGateway`。

    替身只在 HTTP 传输层：URL 拼法、``Authorization`` 头、
    ``response_format`` 的 ``json_schema`` 结构、响应解析、六层 Schema
    校验、证据引用越界作废、失败分类与降级 —— 全部是产品代码本身。
    ``calls`` 收到每次请求的 body，供测试断言「请求形态没变」。
    """
    import httpx

    drafts = record.get("drafts")
    content = json.dumps({"drafts": list(drafts or [])}, ensure_ascii=False)
    usage = dict(record.get("usage") or {})

    def _handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            try:
                calls.append(json.loads(request.content.decode("utf-8")))
            except Exception:  # noqa: BLE001 - 记不到也不该让评测崩
                calls.append({"_unparsed": True})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-attrib-eval",
                "object": "chat.completion",
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            },
        )

    config = GatewayConfig(
        base_url="http://127.0.0.1:9/v1",
        api_key="FAKE_MODEL_KEY_PLACEHOLDER",
        model=model_name,
        timeout=5.0,
        connect_timeout=2.0,
        jitter_ratio=0.0,
    )
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    return ModelGateway(
        config, client=client, sleeper=lambda _s: None, clock=lambda: 0.0
    )


def build_live_gateway(
    *,
    env: Optional[Mapping[str, str]] = None,
    prices: Optional[tuple[Optional[Decimal], Optional[Decimal]]] = None,
) -> tuple[ModelGateway, Optional[Decimal], Optional[Decimal]]:
    """实连真实模型端点。配置位全从环境变量读，缺位即
    ``MODEL_CONFIG_INVALID``（一次列全），不给默认端点兜底。

    返回 ``(网关, 提示词单价, 生成单价)`` —— 单价与网关一起返回而不是
    只在内部用：成本要落进报告，而报告在网关之外渲染。
    """
    import os

    source = os.environ if env is None else env
    config = GatewayConfig.from_env(source)
    prompt_price, completion_price = _prices_of(source) if prices is None else prices
    return ModelGateway(config, period="", channel="", ledger=None), prompt_price, completion_price


def _prices_of(
    env: Mapping[str, str],
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """单价从环境变量读。缺位返回 ``None`` —— **不估、不用 0 顶替**。"""
    out: list[Optional[Decimal]] = []
    for key in ("prompt", "completion"):
        raw = (env.get(PRICE_ENV_VARS[key]) or "").strip()
        if not raw:
            out.append(None)
            continue
        try:
            value = Decimal(raw)
        except Exception:  # noqa: BLE001 - Decimal 对任何非数字都抛
            raise ReconqError(
                "MODEL_PRICE_INVALID",
                f"{PRICE_ENV_VARS[key]}={raw!r} 不是合法数字；"
                "单位是元/千 token，例如 0.012",
            ) from None
        if value < 0:
            raise ReconqError(
                "MODEL_PRICE_INVALID",
                f"{PRICE_ENV_VARS[key]}={raw!r} 为负数；单价为负会让成本合计"
                "出现「用得越多越便宜」的荒谬结果",
            )
        out.append(value)
    return out[0], out[1]


def run_model_tier(
    eval_set: EvalSet,
    *,
    live: bool = False,
    env: Optional[Mapping[str, str]] = None,
    gateway_factory: Optional[Callable[[EvalCase], ModelGateway]] = None,
) -> TierMetrics:
    """模型档：每条样本走一次真实 :func:`reconq.align.attribution.attribute`。

    ``live=False``（默认）用 :func:`build_replay_gateway` 回放录制响应；
    ``live=True`` 走真实端点。三条途径**只有传输层不同**，业务路径
    （提示词 → 网关 → Schema 六层校验 → 证据引用筛查 → 排序）完全一致。
    """
    metrics = TierMetrics(
        tier="模型档",
        run_mode="live" if live else "replay",
    )
    # 单价**两种跑法都要读**。回放模式下录制响应同样带 usage，成本照样是
    # 真实可比的维度；早先只在 ``live`` 分支里读，导致回放模式永远
    # ``priced=False``，报告里恒显「未配置单价」—— 明明配了单价。
    import os

    source = os.environ if env is None else env
    prompt_price, completion_price = _prices_of(source)

    for case in eval_set.cases:
        if gateway_factory is not None:
            gateway = gateway_factory(case)
        elif live:
            # 实连时以当次读到的配置位为准（可能在两次调用之间被改过）
            gateway, prompt_price, completion_price = build_live_gateway(env=source)
        else:
            record = eval_set.responses.get(case.case_id)
            if not isinstance(record, Mapping):
                raise ReconqError(
                    "ATTRIB_EVAL_RESPONSE_MISSING",
                    f"样本 {case.case_id} 没有录制响应 —— "
                    f"responses.json 里现有 {len(eval_set.responses)} 条。"
                    "缺一条会让模型档少算一条，而「少算的那条恰好是对的」"
                    "在报告上与「这一版没提上来」长得一模一样",
                )
            gateway = build_replay_gateway(record)

        started = time.perf_counter()
        result: AttributionResult = attribute(case.bundle, gateway)
        elapsed = (time.perf_counter() - started) * 1000

        hits, _rejections = screen_hits(
            result.causes, period=str(case.bundle.payload.get("period", ""))
        )
        codes = [h.cause_code for h in hits]
        if not codes:
            codes = [c.cause for c in result.causes]
        confidences = [c.confidence for c in result.causes]
        cited = [ref for c in result.causes for ref in c.evidence_refs]

        call = result.gateway_call or {}
        usage = ModelUsage.of_call(
            int(call.get("prompt_tokens") or 0),
            int(call.get("completion_tokens") or 0),
            prompt_price=prompt_price,
            completion_price=completion_price,
        )
        verdict = _score(
            case,
            metrics.tier,
            codes,
            confidences,
            list(dict.fromkeys(cited)),
            kept=len(result.causes),
            rejected=len(result.rejected),
            usable=bool(result.causes) and bool(codes) and not result.degraded,
            degraded=result.degraded,
            error_code=result.error_code,
            elapsed_ms=elapsed,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost=usage.cost,
            priced=usage.priced,
        )
        _accumulate(metrics, verdict)
        metrics.verdicts = metrics.verdicts + (verdict,)
    return metrics


def evaluate_eval_set(
    eval_set: EvalSet,
    *,
    live: bool = False,
    env: Optional[Mapping[str, str]] = None,
    gateway_factory: Optional[Callable[[EvalCase], ModelGateway]] = None,
) -> tuple[TierMetrics, TierMetrics]:
    """在**同一份**评测集上跑两档，返回 ``(模型档, 规则基线档)``。

    顺序刻意是「先跑模型档」：规则档零网络零状态，先跑它会把
    ``elapsed`` 混进网络与 Schema 校验的成本里，两档的耗时就没法比。
    """
    model = run_model_tier(
        eval_set, live=live, env=env, gateway_factory=gateway_factory
    )
    rules = run_rule_tier(eval_set)
    return model, rules


# ─────────────────────────── 报告 ───────────────────────────

REPORT_TITLE = "归因效果评测报告 · 模型档 vs 规则基线档"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _metric_rows(model: TierMetrics, rules: TierMetrics) -> list[tuple[str, str, str]]:
    return [
        ("样本条数", str(model.total), str(rules.total)),
        ("可评条数（出了草稿且 Top-1 落字典）", str(model.usable), str(rules.usable)),
        ("Top-1 命中率", _pct(model.top1_rate), _pct(rules.top1_rate)),
        ("Top-3 命中率", _pct(model.top3_rate), _pct(rules.top3_rate)),
        ("依据行引用正确率", _pct(model.ref_rate), _pct(rules.ref_rate)),
        ("反幻觉拒绝率（Top-1 判不出来）", _pct(model.unknown_refusal_rate), _pct(rules.unknown_refusal_rate)),
        ("硬猜率（该拒却给了具体成因）", _pct(model.hallucination_rate), _pct(rules.hallucination_rate)),
        ("平均置信度（仅 Top-1 答对的样本）", f"{model.mean_confidence:.3f}", f"{rules.mean_confidence:.3f}"),
        ("降级条数", str(model.degraded), str(rules.degraded)),
        ("平均耗时", f"{model.avg_elapsed_ms:.2f} ms", f"{rules.avg_elapsed_ms:.2f} ms"),
        ("平均提示词 token", f"{model.avg_prompt_tokens:.1f}", "0.0"),
        ("平均生成 token", f"{model.avg_completion_tokens:.1f}", "0.0"),
        ("单条平均成本", model.cost_text, rules.cost_text),
        ("总成本", model.total_cost_text, rules.total_cost_text),
    ]


def render_report(
    eval_set: EvalSet,
    model: TierMetrics,
    rules: TierMetrics,
    *,
    evaluated_at: Optional[str] = None,
) -> str:
    """渲染完整报告。**必须**同时给出：样例规模、计算口径、
    两档并排、以及「这些数字不能说明什么」。"""
    stamp = evaluated_at or datetime.now(timezone(timedelta(hours=8))).isoformat(
        timespec="seconds"
    )
    failures = model.gate_failures(rules)
    rows = _metric_rows(model, rules)
    width = max(len(label) for label, _, _ in rows)
    meta = eval_set.meta

    lines: list[str] = [
        f"# {REPORT_TITLE}",
        "",
        f"- 生成时间：{stamp}",
        f"- 评测集：`{eval_set.path}`（{eval_set.total} 条，下限 {MIN_EVAL_CASES} 条）",
        f"- 账期：{list(eval_set.periods)}",
        f"- 模型名与版本：{eval_set.model or '（未记录）'}",
        f"- 提示词版本：{eval_set.prompt_version}（模板指纹 "
        f"{prompt_digest(template_of(eval_set.prompt_version))}）",
        f"- 受控成因字典版本：{BASELINE_VERSION} / 规则来源标记 {RULE_SOURCE}",
        f"- 模型档跑法：{RUN_MODES.get(model.run_mode, model.run_mode)}",
        f"- 规则档跑法：本地纯规则，零网络、零模型、零状态推进",
        f"- 门禁：{'全部通过' if not failures else f'**{len(failures)} 项未过**'}",
        "",
        "## 1 样例规模",
        "",
        "| 分层 | 条数 |",
        "|------|------|",
    ]
    for layer, count in eval_set.layer_counts().items():
        lines.append(f"| {layer} | {count} |")
    lines += ["", "| 渠道 | 条数 |", "|------|------|"]
    for channel, count in eval_set.channel_counts().items():
        lines.append(f"| {channel} | {count} |")
    lines += [
        "",
        f"其中期望 UNKNOWN（证据不足、正确答案是「判不出来」）的样本 "
        f"**{eval_set.unknown_total} 条**（下限 {MIN_UNKNOWN_CASES} 条）—— "
        "反幻觉拒绝率的分母就是它。",
        "",
        "### 1.1 受控成因字典覆盖",
        "",
        "| 成因码 | 出现在多少条样本的期望集合里 |",
        "|--------|------------------------|",
    ]
    for code, count in eval_set.cause_coverage().items():
        entry = CAUSES.get(code)
        name = entry.zh_name if entry else code
        lines.append(f"| {code}（{name}） | {count} |")
    lines += [
        "",
        "### 1.2 标注口径（`golden_meta.json`）",
        "",
    ]
    caliber = meta.get("caliber_notes")
    if caliber:
        lines.extend(f"- {note}" for note in caliber)
    else:
        lines.append("- （评测集未记录标注口径）")
    annotated = meta.get("annotated_by") or "（未记录）"
    annotated_at = meta.get("annotated_at") or "（未记录）"
    lines += ["", f"- 标注来源：{annotated}", f"- 标注时间：{annotated_at}"]

    lines += [
        "",
        "## 2 计算口径",
        "",
        "| 指标 | 分子 | 分母 |",
        "|------|------|------|",
        "| Top-1 命中率 | Top-1 成因码落在期望集合内的条数 | **可评条数**"
        "（出了草稿且 Top-1 落在受控字典内），不是总条数 |",
        "| Top-3 命中率 | Top-3 内任一成因码落在期望集合内的条数 | 同上 |",
        "| 依据行引用正确率 | 引用行覆盖 required_refs 且不少于 min_refs 的条数 | 同上 |",
        "| 反幻觉拒绝率 | UNKNOWN 样本里 Top-1 为 undetermined 的条数 | "
        "**仅 UNKNOWN 样本** |",
        "| 硬猜率 | UNKNOWN 样本里 Top-1 给了具体成因的条数 | 仅 UNKNOWN 样本 |",
        "| 平均置信度 | Top-1 **答对**样本的置信度之和 | 仅 Top-1 答对的样本 |",
        "",
        "两处刻意：**分母不是总条数**（全被拒的那几条不算「答错」，"
        "那是链路拦下的）；**平均置信度只取答对的**"
        "（把答错的高置信度算进来，数字越高越危险）。",
        "",
        "## 3 两档并排",
        "",
        f"| 指标 | 模型档 | 规则基线档 |",
        "|------|--------|------------|",
    ]
    for label, model_text, rules_text in rows:
        lines.append(f"| {label} | {model_text} | {rules_text} |")

    lines += ["", "## 4 门禁", "", "| 门禁 | 结论 |", "|------|------|"]
    gate = model.gate_failures(rules)
    lines.append(
        f"| 模型档 Top-3 命中率 ≥ 规则基线档 | "
        f"{'通过' if not gate else '**未过**'} |"
    )
    if gate:
        for item in gate:
            lines.append(f"| └ {item} | |")
    lines += [
        "",
        "只卡 Top-3 不卡 Top-1，是产品决策：规则在「形态 → 成因」这类"
        "确定性映射上本就强，模型的优势在弱匹配与 UNKNOWN 拒答上。"
        "把 Top-1 也写成硬门禁会逼人对着评测集调提示词，"
        "换来的分数涨不涨得上业务。",
    ]
    # 门禁通过**不等于**两档全面占优。Top-1 落后于规则档时必须在这里
    # 点破：经办人签字时看的是第一条，Top-1 落后意味着多数差异上模型
    # 档给的第一个成因不如规则档的查表结果。只报「门禁通过」会把这句
    # 话藏在一个看起来四平八稳的绿勾底下。
    if model.top1_rate < rules.top1_rate:
        lines += [
            "",
            f"> **门禁通过，但 Top-1 落后**：模型档 Top-1 {_pct(model.top1_rate)} "
            f"低于规则基线档 {_pct(rules.top1_rate)}。多数差异上经办人看到的"
            "第一条成因，模型档不如规则档的形态查表。Top-3 持平只说明"
            "「正确答案还排在候选里」，而排序本身要花人工去翻。"
            "这批样本里模型档的优势集中在 UNKNOWN 拒答"
            f"（{_pct(model.unknown_refusal_rate)} vs {_pct(rules.unknown_refusal_rate)}）—— "
            "接手前请确认院内差异的构成比例：单边账为主时规则档够用，"
            "弱匹配与判不出来为主时才需要模型层。",
        ]
    lines += [
        "",
        "## 5 逐条结论（模型档未命中与 UNKNOWN 样本）",
        "",
        "| case_id | 期望 | 模型 Top-1 | Top-1 命中 | Top-3 命中 | 拒答正确 | 置信度 | 耗时 ms |",
        "|---------|------|-----------|-----------|-----------|----------|--------|---------|",
    ]
    by_case = {c.case_id: c for c in eval_set.cases}
    for verdict in model.verdicts:
        case = by_case[verdict.case_id]
        if verdict.top1_hit and not case.expect_unknown:
            continue
        lines.append(
            f"| {verdict.case_id} | {'/'.join(case.expect_codes)} | "
            f"{verdict.top1_code or '（无可用成因）'} | "
            f"{'是' if verdict.top1_hit else '否'} | "
            f"{'是' if verdict.top3_hit else '否'} | "
            f"{'—' if verdict.unknown_resolved is None else ('是' if verdict.unknown_resolved else '否')} | "
            f"{verdict.top1_confidence:.2f} | {verdict.elapsed_ms:.2f} |"
        )
    behaviour = eval_set.behaviour_counts()
    if behaviour:
        lines += [
            "",
            "录制响应替身的行为分布（**只用于报告，不参与打分**）："
            + "、".join(f"{k} {v} 条" for k, v in behaviour.items()),
        ]

    lines += [
        "",
        "## 6 这些数字不能说明什么",
        "",
        "1. **模型档数字不代表任何真实端点的判别能力。** 本轮未实连院内模型"
        "端点，模型档跑的是 `responses.json` 里的录制响应替身（见该文件 "
        "`note_zh`）。要回答「院内那个端点判得准不准」，得配好 "
        "`RECONQ_MODEL_*` 后用 `reconq attrib-eval --live` 实连重跑。",
        "2. **标注由本产品作者完成，不是第三方盲标。** 存在「按自己实现的"
        "判据标注」这一层循环；缓解办法是期望只写受控字典的桶、不写实现"
        "细节，但这不能完全消除。",
        "3. **样本全部来自单账期的构造数据。** 不代表真实六路账单的形态"
        "分布，命中率不能外推到别的账期。",
        "4. **成本维度在替身模式下是「按证据包体量估的 token × 环境变量单价」，"
        "不是真实账单。** 单价缺位时报告显示「未配置单价」而不是 0 元。",
        "",
        "## 7 复现",
        "",
        "```bash",
        "# 回放模式（本轮默认，不发真实请求）",
        "PYTHONPATH=src python3 -m reconq attrib-eval \\",
        "  --set tests/fixtures/attrib/golden --out out/attrib-eval.md",
        "",
        "# 实连真实端点（需 RECONQ_MODEL_* 与可选单价变量）",
        "PYTHONPATH=src python3 -m reconq attrib-eval --live --out out/attrib-eval.md",
        "```",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_eval_report(
    out_path: str,
    eval_set: EvalSet,
    model: TierMetrics,
    rules: TierMetrics,
    *,
    evaluated_at: Optional[str] = None,
) -> str:
    """写 ``out/attrib-eval.md``，返回路径。"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(
            render_report(eval_set, model, rules, evaluated_at=evaluated_at)
        )
    return out_path
