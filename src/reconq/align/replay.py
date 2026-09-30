"""归因离线回放与版本对比（Task 34）。

「改了提示词之后是变好还是变坏」必须有答案
------------------------------------------
提示词是本产品**唯一一处可以被凭感觉改**的地方，而模型输出本来就有随机性：
改完跑一期看着差不多就上去了，等到某天发现 Top-1 命中率从 0.7 掉到 0.4，
已经没人记得三周前动过哪句话。

本模块把这件事做成**离线、可复算、不花钱**的一步：

1. :class:`ReplayCase` —— 一条**逐字节固定**的证据包 + 期望性质
   （落在哪一桶成因、Top-1 必须引用哪几行）。期望**不是逐字答案**：
   现场没有标准答案，标准答案是「归因该落到受控字典的哪一桶」。
2. :func:`run_replay` —— 把某版提示词下**录好的模型响应**喂进**真实的
   归因后处理链**（Schema 校验 → 证据引用越界筛查 → 反幻觉核对 →
   受控字典筛查），出该版的指标。**不重新调模型**：重调测的是端点当天的
   状态，不是提示词版的差别。
3. :func:`compare_reports` —— 两版逐条对比 + 指标增减，**更差的一版给出
   告警**（:class:`ReplayComparison.regressed`）。

为什么回放走「后处理链」而不是直接比字符串
------------------------------------------
本产品对模型输出有四道闸：草稿 Schema（:mod:`reconq.gateway.schema`）、
证据引用越界（:func:`reconq.align.attribution.attribute`）、反幻觉核对
（:mod:`reconq.align.verify_draft`）、受控字典（:func:`reconq.align.causes.screen_hits`）。
**回放若绕过这四道闸，评出来的命中率是虚的** —— 一份引用了不存在行的草稿
在线上会被拒，回放里却算「说对了」。所以本模块的每一条 case 都真的走一遍
完整链路，被拒的计入 :attr:`CaseOutcome.rejected_*` 而不是悄悄消失。

指标口径
--------
``top1_hit``     Top-1 成因落在期望集合内 —— 经办人只看得动第一条
``top3_hit``     前三条里有一条落在期望集合内 —— 排序质量
``ref_hit``      Top-1 引用的行覆盖 ``required_refs`` —— 有没有据可依
``kept``         通过全部四道闸的草稿条数（>0 才算这条 case 有产出）
``rejected_*``   被哪道闸拒了多少条 —— 提示词改坏了会体现在这里

被拒的条数**单列不并入命中**：「模型说了十句，其中一句被反幻觉核对拒了、
另外九句都对」与「模型说了一句，被拒了」在命中率上一样，但前者其实是好结果。
把两者混成一个数，改提示词的人就永远学不到东西。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层，也不需要有**：``contract verify`` 的
判定是确定性的，重跑一次就是同一个结果，「这次改动有没有变好」根本不成问题。
本产品多出来的这一跳是「判据的一部分搬进了模型」，于是模型那一段话就成了
必须纳入版本管理与回归的东西。沿用的仍是主路径纪律：**判据收口在一处**、
**版本进缓存键与审计链**、**结论交人工**。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from ..errors import ReconqError
from . import prompts as _prompts
from .attribution import CauseDraft, evidence_refs_of
from .bundle import EvidenceBundle, bundle_from_payload
from .causes import screen_hits
from .schema_version import VersionPair, pair_of
from .verify_draft import screen_drafts

__all__ = [
    "CaseOutcome",
    "ReplayCase",
    "ReplayComparison",
    "ReplayReport",
    "compare_reports",
    "load_replay_set",
    "render_comparison",
    "render_report",
    "run_replay",
]


#: 回放集根目录（项目内默认位置）
DEFAULT_REPLAY_DIR = os.path.join("tests", "fixtures", "attrib")

#: 期望条数下限。**低于 12 条的回放集没有否决力** —— 一条差异的结论
#: 偶然对一次是常事，12 条里错 1 条与错 3 条才能看出趋势。
MIN_CASES = 12


# ─────────────────────────── 回放集 ───────────────────────────


@dataclass(frozen=True)
class ReplayCase:
    """一条固定证据包 + 期望性质。"""

    case_id: str
    name_zh: str
    channel: str
    kind: str
    bundle: EvidenceBundle
    #: 可接受的成因码集合（**集合不是单值**：一条差异常有两个都说得通的成因）
    cause_codes: tuple[str, ...]
    #: Top-1 必须引用的行号
    required_refs: tuple[str, ...] = ()
    #: 全部保留草稿合计至少引用几行
    min_refs: int = 1

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReplayCase":
        expect = dict(raw.get("expect") or {})
        case_id = str(raw.get("case_id", "")).strip()
        if not case_id:
            raise ReconqError(
                "REPLAY_CASE_INVALID",
                f"回放用例缺 case_id：{json.dumps(dict(raw), ensure_ascii=False)[:120]}…",
            )
        bundle_payload = raw.get("bundle")
        if not isinstance(bundle_payload, dict):
            raise ReconqError(
                "REPLAY_CASE_INVALID",
                f"回放用例 {case_id} 缺 bundle —— 离线回放要求证据包**逐字节固定**"
                "（存 JSON 而不是每次重跑对齐器重建：重建依赖当期账单行与"
                "候选打分配置，两者都会漂移，漂移会让「提示词变好还是变坏」"
                "这个变量里混进「证据包也变了」那个说不清的量）",
            )
        return cls(
            case_id=case_id,
            name_zh=str(raw.get("name_zh", case_id)),
            channel=str(raw.get("channel", "")),
            kind=str(raw.get("kind", "")),
            bundle=bundle_from_payload(bundle_payload),
            cause_codes=tuple(str(c) for c in (expect.get("cause_codes") or ())),
            required_refs=tuple(str(r) for r in (expect.get("required_refs") or ())),
            min_refs=int(expect.get("min_refs", 1) or 0),
        )


def load_replay_set(directory: str = DEFAULT_REPLAY_DIR) -> list[ReplayCase]:
    """读回放集（``<dir>/cases/*.json`` + ``<dir>/responses.json``）。

    :raises ReconqError: ``REPLAY_SET_EMPTY`` / ``REPLAY_SET_TOO_SMALL`` /
        ``REPLAY_RESPONSE_MISSING``
    """
    cases_dir = os.path.join(directory, "cases")
    if not os.path.isdir(cases_dir):
        raise ReconqError(
            "REPLAY_SET_EMPTY",
            f"回放集目录不存在：{cases_dir}。用 "
            "PYTHONPATH=src python3 tests/fixtures/attrib/generate_replay_set.py 生成",
        )
    cases: list[ReplayCase] = []
    for name in sorted(os.listdir(cases_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(cases_dir, name), encoding="utf-8") as handle:
            cases.append(ReplayCase.from_dict(json.load(handle)))
    if not cases:
        raise ReconqError(
            "REPLAY_SET_EMPTY",
            f"{cases_dir} 下没有 *.json 回放用例 —— 空回放集跑出来的「命中率 0%」"
            "不是模型不行，是压根没喂东西",
        )
    if len(cases) < MIN_CASES:
        raise ReconqError(
            "REPLAY_SET_TOO_SMALL",
            f"回放集只有 {len(cases)} 条，低于下限 {MIN_CASES} 条。"
            "一条差异的结论偶然对一次是常事，条数不够就分不出"
            "「真变好了」与「这次运气好」",
        )
    return cases


def load_responses(directory: str = DEFAULT_REPLAY_DIR) -> dict[str, Any]:
    """读录好的模型响应（``<dir>/responses.json``）。"""
    path = os.path.join(directory, "responses.json")
    if not os.path.isfile(path):
        raise ReconqError(
            "REPLAY_RESPONSE_MISSING",
            f"回放响应文件不存在：{path}。没有录好的响应就没有回放 —— "
            "重新调模型测的是端点当天的状态，不是提示词版的差别",
        )
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# ─────────────────────────── 逐条结论 ───────────────────────────


@dataclass(frozen=True)
class CaseOutcome:
    """一条回放用例在某版提示词下的结论。"""

    case_id: str
    name_zh: str
    channel: str
    kind: str
    prompt_version: str
    #: 通过全部四道闸的草稿（已按置信度排好序）
    kept: tuple[CauseDraft, ...] = ()
    #: Top-1 的成因码（经 :func:`reconq.align.causes.screen_hits` 归一后）
    top1_code: str = ""
    top3_codes: tuple[str, ...] = ()
    top1_hit: bool = False
    top3_hit: bool = False
    ref_hit: bool = False
    cited_refs: tuple[str, ...] = ()
    rejected_ref_invalid: int = 0
    rejected_evidence_mismatch: int = 0
    rejected_cause_unknown: int = 0
    rejected_schema: int = 0
    degraded: bool = False
    error_code: str = ""
    error_detail: str = ""

    @property
    def usable(self) -> bool:
        """这条用例有没有**可评的产出**：通过了引用筛查与反幻觉核对、
        且至少一条成因落在受控字典内。

        「全被拒掉」不算它「答错」—— 那是链路把它拦下了，不是模型说错���；
        但「草稿过了核对、却因为用了字典外的措辞而一条都没落进字典」
        **要算答错**：草稿本身被链路收下了，是它说的成因落不了地，
        经办人在台账上看到的仍然是「这条没有可用成因」。
        """
        return bool(self.kept) and bool(self.top1_code) and not self.degraded

    @property
    def answered_but_unusable(self) -> bool:
        """草稿过了前两道闸，却因成因不在受控字典内而一条都没留下。

        单独一个属性而不是并进拒因计数：它与「被引用越界筛查拒掉」是
        两回事 —— 前者说明模型**确实回答了**，后者说明它引用了不存在的
        证据。回放报告要能分开说这两句话，否则改提示词的人会把
        「措辞没对齐字典」误当成「模型在说胡话」。
        """
        return bool(self.kept) and not self.top1_code and not self.degraded

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "name_zh": self.name_zh,
            "channel": self.channel,
            "kind": self.kind,
            "prompt_version": self.prompt_version,
            "kept": [d.to_dict() for d in self.kept],
            "top1_code": self.top1_code,
            "top3_codes": list(self.top3_codes),
            "top1_hit": self.top1_hit,
            "top3_hit": self.top3_hit,
            "ref_hit": self.ref_hit,
            "cited_refs": list(self.cited_refs),
            "rejected_ref_invalid": self.rejected_ref_invalid,
            "rejected_evidence_mismatch": self.rejected_evidence_mismatch,
            "rejected_cause_unknown": self.rejected_cause_unknown,
            "rejected_schema": self.rejected_schema,
            "degraded": self.degraded,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "usable": self.usable,
        }


# ─────────────────────────── 报告 ───────────────────────────


@dataclass(frozen=True)
class ReplayReport:
    """一版提示词在整份回放集上的指标。"""

    prompt_version: str
    pair: VersionPair
    outcomes: tuple[CaseOutcome, ...] = ()
    #: 录响应时的模型名。**报告里必须带**：换个模型跑同一份响应，
    #: 指标的含义就变了，而「模型名」不在报告里就没人知道。
    model: str = ""

    @property
    def total(self) -> int:
        return len(self.outcomes)

    def _usable(self) -> tuple[CaseOutcome, ...]:
        return tuple(o for o in self.outcomes if o.usable)

    @property
    def usable(self) -> int:
        return len(self._usable())

    @property
    def top1_hits(self) -> int:
        return sum(1 for o in self._usable() if o.top1_hit)

    @property
    def top3_hits(self) -> int:
        return sum(1 for o in self._usable() if o.top3_hit)

    @property
    def ref_hits(self) -> int:
        return sum(1 for o in self._usable() if o.ref_hit)

    @property
    def top1_rate(self) -> Decimal:
        """Top-1 命中率。分母是**可用条数**而不是总条数 —— 全被拒的
        那几条不参与平均，否则改提示词时「让所有草稿都过不了 Schema」
        反而会让命中率好看一点。"""
        usable = self.usable
        return (
            Decimal(self.top1_hits) / Decimal(usable) if usable else Decimal("0")
        )

    @property
    def top3_rate(self) -> Decimal:
        usable = self.usable
        return (
            Decimal(self.top3_hits) / Decimal(usable) if usable else Decimal("0")
        )

    @property
    def ref_rate(self) -> Decimal:
        usable = self.usable
        return (
            Decimal(self.ref_hits) / Decimal(usable) if usable else Decimal("0")
        )

    @property
    def rejected_total(self) -> int:
        return sum(
            o.rejected_ref_invalid
            + o.rejected_evidence_mismatch
            + o.rejected_cause_unknown
            + o.rejected_schema
            for o in self.outcomes
        )

    @property
    def rejection_rate(self) -> Decimal:
        """被反幻觉核对拒掉的比例（分母 = 送进核对的草稿条数）。"""
        offered = self.kept_total + sum(
            o.rejected_evidence_mismatch for o in self.outcomes
        )
        return (
            Decimal(sum(o.rejected_evidence_mismatch for o in self.outcomes))
            / Decimal(offered)
            if offered
            else Decimal("0")
        )

    @property
    def kept_total(self) -> int:
        return sum(len(o.kept) for o in self.outcomes)

    @property
    def degraded(self) -> int:
        return sum(1 for o in self.outcomes if o.degraded)

    def outcome_of(self, case_id: str) -> CaseOutcome:
        for outcome in self.outcomes:
            if outcome.case_id == case_id:
                return outcome
        raise ReconqError(
            "REPLAY_CASE_NOT_FOUND",
            f"回放集里没有用例 {case_id!r}；现有 {len(self.outcomes)} 条",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_version": self.prompt_version,
            "pair": self.pair.to_dict(),
            "model": self.model,
            "total": self.total,
            "usable": self.usable,
            "degraded": self.degraded,
            "top1_hits": self.top1_hits,
            "top3_hits": self.top3_hits,
            "ref_hits": self.ref_hits,
            "top1_rate": f"{self.top1_rate:.4f}",
            "top3_rate": f"{self.top3_rate:.4f}",
            "ref_rate": f"{self.ref_rate:.4f}",
            "kept_total": self.kept_total,
            "rejected_total": self.rejected_total,
            "rejection_rate": f"{self.rejection_rate:.4f}",
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


# ─────────────────────────── 回放 ───────────────────────────


def _drafts_of(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把录好的响应还原成草稿列表。

    ``drafts``（结构化）与 ``content``（原样文本）**只给其一**：给
    ``content`` 是为了录「模型当时就是这么回的」这种带围栏 / 非 JSON 的
    真实响应，那条路径必须由 :mod:`reconq.gateway.schema` 自己解，
    回放器不代劳。
    """
    if "drafts" in record:
        return [dict(d) for d in (record.get("drafts") or [])]
    if "content" in record:
        raw = str(record.get("content") or "")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReconqError(
                "REPLAY_CASE_INVALID",
                f"回放响应里 drafts 与 content 都没给，或 content 不是 JSON："
                f"{exc}",
            ) from exc
        return [dict(d) for d in (parsed.get("drafts") or [])]
    raise ReconqError(
        "REPLAY_CASE_INVALID",
        "回放响应必须给 drafts（结构化）或 content（原样文本）之一",
    )


def _evaluate(
    case: ReplayCase,
    version: str,
    drafts: Sequence[Mapping[str, Any]],
) -> CaseOutcome:
    """把一份草稿走完**四道闸**，再与期望比对。

    四道闸的顺序就是线上顺序：Schema → 证据引用越界 → 反幻觉核对 →
    受控字典。顺序不能调 —— 字典筛查放在反幻觉核对之前会让「引用了不
    存在行且自造成因」的草稿在字典这一步就被拒，反幻觉核对的统计里
    就看不到它，而那恰恰是最该被看见的一类。
    """
    # 判据一律取归因层那一套，**不另写一遍**：草稿校验用
    # :func:`reconq.align.attribution.validate_draft`（它把网关侧六层校验
    # 收敛成一条 :class:`CauseDraft`），引用筛查与排名用 attribution 的
    # 内部两个函数。回放里重写一份的后果是「回放说过了、线上没过」，
    # 而那正是这套回放要抓的 bug —— 自己就带了一个。
    from .attribution import _rank, _screen_drafts, validate_draft as attrib_validate

    rejected_ref = 0
    rejected_schema = 0
    drafts_out: list[CauseDraft] = []
    for raw in drafts:
        try:
            drafts_out.append(attrib_validate(raw))
        except ReconqError:
            # ``ATTRIBUTION_DRAFT_INVALID`` 与网关侧六层校验判据同源，
            # 这里同样计入「被 Schema 拒掉」
            rejected_schema += 1

    allowed = evidence_refs_of(case.bundle)
    kept_ref, rejected = _screen_drafts(drafts_out, allowed)
    rejected_ref = len(rejected)

    kept_drafts, rejected_verify = screen_drafts(kept_ref, case.bundle)
    rejected_mismatch = len(rejected_verify)

    hits, cause_rejections = screen_hits(kept_drafts, period=case.bundle.payload.get("period", ""))
    rejected_cause = len(cause_rejections)

    ranked = _rank(tuple(kept_drafts))
    codes = [h.cause_code for h in hits]
    top1 = codes[0] if codes else ""
    top3 = tuple(codes[:3])
    cited = tuple(dict.fromkeys(ref for d in ranked for ref in d.evidence_refs))
    expected = set(case.cause_codes)

    return CaseOutcome(
        case_id=case.case_id,
        name_zh=case.name_zh,
        channel=case.channel,
        kind=case.kind,
        prompt_version=version,
        kept=ranked,
        top1_code=top1,
        top3_codes=top3,
        top1_hit=bool(top1) and top1 in expected,
        top3_hit=any(code in expected for code in top3),
        ref_hit=bool(cited)
        and set(case.required_refs) <= set(cited)
        and len(cited) >= case.min_refs,
        cited_refs=cited,
        rejected_ref_invalid=rejected_ref,
        rejected_evidence_mismatch=rejected_mismatch,
        rejected_cause_unknown=rejected_cause,
        rejected_schema=rejected_schema,
    )


def run_replay(
    version: Optional[str] = None,
    *,
    directory: str = DEFAULT_REPLAY_DIR,
    responses: Optional[Mapping[str, Any]] = None,
) -> ReplayReport:
    """跑一版提示词的回放。

    **不调模型**：响应来自 ``responses.json``（某版提示词下录好的实际输出）。
    重调模型测的是端点当天的状态，而不是提示词版的差别 —— 那件事该由
    ``reconq scan`` 去做，而不是回放集。
    """
    template = _prompts.template_of(version)
    cases = load_replay_set(directory)
    recorded = dict(responses if responses is not None else load_responses(directory))
    by_case = dict(recorded.get("responses") or {})

    outcomes: list[CaseOutcome] = []
    for case in cases:
        record = by_case.get(case.case_id)
        if not isinstance(record, dict) or template.version not in record:
            raise ReconqError(
                "REPLAY_RESPONSE_MISSING",
                f"用例 {case.case_id} 没有录 {template.version!r} 版的响应 —— "
                f"回放集里录了 {sorted(set(by_case.get(case.case_id, {}) or {}))}。"
                "缺一版的响应会让版本对比缺一条基线，而「基线缺失」在报告上"
                "看起来和「这一版没提上来」一模一样",
            )
        outcomes.append(_evaluate(case, template.version, _drafts_of(record[template.version])))

    return ReplayReport(
        prompt_version=template.version,
        pair=pair_of(template.version),
        outcomes=tuple(outcomes),
        model=str(recorded.get("model", "")),
    )


# ─────────────────────────── 版本对比 ───────────────────────────


@dataclass(frozen=True)
class CaseChange:
    """一条用例在两版之间的结论变化。"""

    case_id: str
    name_zh: str
    baseline_top1: str
    candidate_top1: str
    baseline_hit: bool
    candidate_hit: bool
    baseline_refs: tuple[str, ...] = ()
    candidate_refs: tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        """``better`` / ``worse`` / ``same`` / ``changed``。

        **命中与否优先于 Top-1 文本变化**：Top-1 从
        ``his_missing_order`` 变成 ``channel_late_posting`` 而两者都在期望
        集合里，这是「换了个同样对的说法」，不是变好也不是变坏 ——
        把它算成变化，回放报告里会凭空多出十几条「结论变了」，
        而真正该盯的那一条反而被淹了。
        """
        if self.candidate_hit and not self.baseline_hit:
            return "better"
        if self.baseline_hit and not self.candidate_hit:
            return "worse"
        if self.baseline_top1 != self.candidate_top1:
            return "changed"
        return "same"

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "name_zh": self.name_zh,
            "baseline_top1": self.baseline_top1,
            "candidate_top1": self.candidate_top1,
            "baseline_hit": self.baseline_hit,
            "candidate_hit": self.candidate_hit,
            "baseline_refs": list(self.baseline_refs),
            "candidate_refs": list(self.candidate_refs),
            "verdict": self.verdict,
        }


@dataclass(frozen=True)
class ReplayComparison:
    """两版提示词的逐条与汇总对比。"""

    baseline: ReplayReport
    candidate: ReplayReport
    changes: tuple[CaseChange, ...] = ()

    @property
    def worse_cases(self) -> tuple[CaseChange, ...]:
        return tuple(c for c in self.changes if c.verdict == "worse")

    @property
    def better_cases(self) -> tuple[CaseChange, ...]:
        return tuple(c for c in self.changes if c.verdict == "better")

    @property
    def regressed(self) -> bool:
        """新版本比旧版本差。

        判据是「**变差的条数 > 变好的条数**」而不是「Top-1 命中率下降」：
        命中率是 12 条上的一个比例，改一条就从 11/12 掉到 10/12，而
        11/12 与 10/12 之间隔着一条真实变差的用例 —— 逐条看得见，
        总分看不见。反过来，条数判据在「命中率没动但 Top-3 掉了」时也不会
        误报变差（那几条的 ``verdict`` 是 ``changed`` 不是 ``worse``）。
        """
        return len(self.worse_cases) > len(self.better_cases)

    @property
    def warning(self) -> str:
        """更差时的中文告警；不差时为空串。"""
        if not self.regressed:
            return ""
        worse = self.worse_cases
        detail = "、".join(
            f"{c.case_id}（{c.baseline_top1 or '（无）'} → {c.candidate_top1 or '（无）'}）"
            for c in worse[:3]
        )
        more = f"等 {len(worse)} 条" if len(worse) > 3 else ""
        return (
            f"新版本 {self.candidate.prompt_version} 相对基线 "
            f"{self.baseline.prompt_version} 变差：{len(worse)} 条由对变错"
            f"（{detail}{more}），变好 {len(self.better_cases)} 条。"
            "**不要静默采用更差版本** —— 请改回基线，或按上面列出的用例逐条看"
            "是提示词退化了还是录的那批响应本身有问题"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "changes": [c.to_dict() for c in self.changes],
            "better_cases": [c.case_id for c in self.better_cases],
            "worse_cases": [c.case_id for c in self.worse_cases],
            "regressed": self.regressed,
            "warning": self.warning,
        }


def compare_reports(
    baseline: ReplayReport, candidate: ReplayReport
) -> ReplayComparison:
    """逐条 + 汇总对比两版报告。"""
    changes: list[CaseChange] = []
    for base_outcome in baseline.outcomes:
        cand_outcome = candidate.outcome_of(base_outcome.case_id)
        changes.append(
            CaseChange(
                case_id=base_outcome.case_id,
                name_zh=base_outcome.name_zh,
                baseline_top1=base_outcome.top1_code,
                candidate_top1=cand_outcome.top1_code,
                baseline_hit=base_outcome.top1_hit,
                candidate_hit=cand_outcome.top1_hit,
                baseline_refs=base_outcome.cited_refs,
                candidate_refs=cand_outcome.cited_refs,
            )
        )
    return ReplayComparison(baseline=baseline, candidate=candidate, changes=tuple(changes))


def compare_versions(
    baseline_version: str,
    candidate_version: str,
    *,
    directory: str = DEFAULT_REPLAY_DIR,
    responses: Optional[Mapping[str, Any]] = None,
) -> ReplayComparison:
    """跑两版并对比（``reconq attrib-replay --compare v1,v2`` 的内核）。"""
    recorded = responses if responses is not None else load_responses(directory)
    baseline = run_replay(baseline_version, directory=directory, responses=recorded)
    candidate = run_replay(candidate_version, directory=directory, responses=recorded)
    return compare_reports(baseline, candidate)


# ─────────────────────────── 渲染 ───────────────────────────


def _pct(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def render_report(report: ReplayReport) -> str:
    """终端报告。"""
    answered_unusable = sum(1 for o in report.outcomes if o.answered_but_unusable)
    lines = [
        f"归因离线回放 · 提示词 {report.prompt_version}"
        f"（{_prompts.template_of(report.prompt_version).change_note or '（无变更说明）'}）",
        f"版本对：{report.pair.label()}",
        f"模型：{report.model or '（回放集未记录）'}",
        "",
        f"用例 {report.total} 条 · 可评 {report.usable} 条 · 降级 {report.degraded} 条"
        + (
            f" · 答了但措辞未对齐字典 {answered_unusable} 条"
            if answered_unusable
            else ""
        ),
        f"Top-1 命中 {report.top1_hits}/{report.usable}（{_pct(report.top1_rate)}）"
        f" · Top-3 命中 {report.top3_hits}/{report.usable}（{_pct(report.top3_rate)}）"
        f" · 引用行达标 {report.ref_hits}/{report.usable}（{_pct(report.ref_rate)}）",
        f"保留草稿 {report.kept_total} 条 · 被拒 {report.rejected_total} 条"
        f"（反幻觉核对拒 {_pct(report.rejection_rate)}）",
        "",
    ]
    for outcome in report.outcomes:
        mark = "√" if outcome.top1_hit else "×"
        flag = ""
        if outcome.rejected_ref_invalid:
            flag += f" 引用越界 {outcome.rejected_ref_invalid}"
        if outcome.rejected_evidence_mismatch:
            flag += f" 核对不过 {outcome.rejected_evidence_mismatch}"
        if outcome.rejected_cause_unknown:
            flag += f" 字典外 {outcome.rejected_cause_unknown}"
        if outcome.rejected_schema:
            flag += f" 结构不合 {outcome.rejected_schema}"
        top1 = outcome.top1_code or (
            "（措辞未对齐字典）" if outcome.answered_but_unusable else "（无）"
        )
        lines.append(
            f"  {mark} {outcome.case_id:<34} Top-1 {top1:<28}"
            f" 引用 {len(outcome.cited_refs)} 行{flag}"
        )
    return "\n".join(lines)


def render_comparison(comparison: ReplayComparison) -> str:
    """终端版本对比报告。"""
    base = comparison.baseline
    cand = comparison.candidate
    lines = [
        f"归因提示词版本对比：{base.prompt_version} → {cand.prompt_version}",
        f"基线版本对：{base.pair.label()}",
        f"新版本对：{cand.pair.label()}",
        "",
        f"{'指标':<18}{base.prompt_version:>10}{cand.prompt_version:>12}{'变化':>12}",
    ]
    for label, left, right in (
        ("Top-1 命中率", base.top1_rate, cand.top1_rate),
        ("Top-3 命中率", base.top3_rate, cand.top3_rate),
        ("引用行达标率", base.ref_rate, cand.ref_rate),
        ("反幻觉核对拒率", base.rejection_rate, cand.rejection_rate),
    ):
        delta = (right - left) * 100
        sign = "+" if delta > 0 else ""
        lines.append(f"{label:<18}{_pct(left):>10}{_pct(right):>12}{sign}{delta:.1f}pp")
    lines.append("")
    lines.append("逐条结论变化：")
    for change in comparison.changes:
        mark = {"better": "↑", "worse": "↓", "changed": "·", "same": " "}[change.verdict]
        lines.append(
            f"  {mark} {change.case_id:<34} {change.baseline_top1 or '（无）'}"
            f" → {change.candidate_top1 or '（无）'}"
        )
    lines.append("")
    if comparison.regressed:
        lines.append(f"⚠ {comparison.warning}")
    else:
        lines.append(
            f"新版本未出现「由对变错」的用例（变好 {len(comparison.better_cases)} 条 / "
            f"变差 {len(comparison.worse_cases)} 条）"
        )
    return "\n".join(lines)
