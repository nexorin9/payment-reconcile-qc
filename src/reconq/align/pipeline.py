"""对齐主路径编排（Task 42）：把核心模块串成一条能跑通、能看进度的流水线。

为什么要有这一层
----------------
前二十来个任务把对齐的每一段都做出来了 —— 索引、对齐、弱匹配打分、歧义
分型、跨账期连坐、证据包、归因、反幻觉核对、成因字典、同根因聚合 ——
但它们各自是**函数**，不是**流程**。经办人真正要的东西是「周一早上跑一次，
告诉我这一期有多少件事要我判、每件归谁」，而这件事此前没有任何一个函数
能回答。

本模块只做**编排**，不新增判据：每一段都调既有模块的既有函数。判据散在
八处、各自都有单测，本层把它们按顺序接起来，并回答三个此前无人回答的
问题：

1. **跑到哪了** —— :class:`ProgressReporter` 按阶段发事件（阶段名、已处理
   条数、耗时），面板据此显示进度条与「哪一段慢」。
2. **跑挂了怎么办** —— :class:`PipelineResult` 逐阶段记录成败。某一阶段
   抛错时**已完成阶段的结果保留**、该期标 ``partial``，而不是整期回滚成
   「无结果」。整期回滚看着干净，实际是把「已归因 180 条、聚合那一步崩了」
   说成「本期一条差异都没有」，经办人会照着这份假结论去签字。
3. **结果能不能复算** —— 同一账期重复跑，产出集合必须逐键相同（:meth:`AlignPipelineResult.fingerprint`
   钉的是**结论键 + 成因 + 处置状态**的规范 JSON 摘要，不含耗时、不含
   构造时刻）。这条不成立时，结论单上的「谁、何时、依据哪几条证据行」
   就无从追起。

依赖方向
--------
``pipeline`` 是本层里唯一 import 了 ``engine.period`` 的模块，且**只在
函数体内**局部 import（与 :mod:`reconq.align.taxonomy`、
:mod:`reconq.align.aggregate` 同一手法）。它落台账，但台账不反过来依赖它。

与源产品的关系
--------------
参考实现 ``soda-core`` 的主路径是「一条命令跑完一串阶段」——
``contract verify`` 里取契约、取数、逐 check 执行、打印失败、``exit_with_code``，
阶段是**顺序写死在一条调用栈里**的，没有进度、没有部分成功。本产品保留
「一条命令跑完」这个形状（经办人不会为了看进度去开八个终端），但把阶段
**显式化**：名字、受控顺序、逐段耗时、逐段成败、逐段事件。这一跳是新增的，
因为源产品要判的是「这一行的这一列不满足这条口径」，判完就完事；本产品
要回答的是「这一期有多少件事归谁办」，那件事在跑完之前就该能看见。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from ..errors import ReconqError
from .aggregate import (
    DEFAULT_AMOUNT_BUCKET,
    AggregationResult,
    DiscrepancyEntry,
    EvidenceRow,
    aggregate_discrepancies,
    diff_key,
    to_decimal,
)
from .attribution import AttributionResult, attribute
from .baseline import rank_causes_by_rules
from .bundle import (
    DEFAULT_HISTORY_PERIODS,
    HIS_ONLY_KIND,
    KIND_CHECK_PATH_PREFIX,
    EvidenceBundle,
    build_evidence_bundle,
)
from .causes import CAUSES
from .matcher import (
    AUTHORITATIVE_CHANNEL,
    AlignAllResult,
    AlignKey,
    AlignRecord,
    MatchOutcome,
    align_all,
    record_from_row_dict,
)
from .precedent import suggest_for
from .taxonomy import classify_taxonomy
from .trace import (
    DEFAULT_WINDOW_PERIODS,
    GLOBAL_CHANNEL,
    TraceConclusion,
    claims_from_align_result,
    period_window_of,
    trace_entity,
)
from .verify_draft import VerificationLedger, screen_drafts

logger = logging.getLogger("reconq.align.pipeline")

__all__ = [
    "STAGES",
    "STAGE_ZH",
    "PIPELINE_VERSION",
    "CAUSE_SOURCE_MODEL",
    "CAUSE_SOURCE_RULE",
    "CAUSE_SOURCE_NONE",
    "CAUSE_SOURCE_ZH",
    "PendingItem",
    "PipelineResult",
    "ProgressReporter",
    "StageReport",
    "align_fingerprint",
    "briefing_zh",
    "run_align_pipeline",
]

#: 主路径阶段表。**顺序即执行顺序**，且这张表是受控词表
#: （:data:`STAGE_ZH` 逐项登记中文名与一句话说明）—— 面板按阶段名画进度条，
#: 未登记的阶段名会走到 :func:`ProgressReporter` 时被显式拒掉，而不是变成
#: 一段没有名字的等待。
STAGES: tuple[str, ...] = (
    "index",          # 建 HIS 侧倒排索引
    "align",          # 六路 + HIS 三方对齐，产出待归因结论
    "taxonomy",       # 歧义分型（拆笔 / 合并 / 跨账期 / 退费并存）
    "trace",          # 跨账期连坐追查
    "bundle",         # 归因证据包
    "attribute",      # 归因（模型不可达时降级为规则候选 + 待人工判定）
    "verify",         # 反幻觉核对（草稿与它引的证据行逐项对账）
    "aggregate",      # 同根因聚合 → 处置单
)

#: 阶段 → (中文名, 这段在做什么)。
STAGE_ZH: dict[str, tuple[str, str]] = {
    "index": ("建索引", "把 HIS 侧收费流水按账期分片建倒排索引"),
    "align": ("三方对齐", "六路渠道账单与 HIS 逐单认领，单边账/歧义/弱匹配全部拒归并"),
    "taxonomy": ("歧义分型", "一对多的候选判成拆笔 / 合并 / 跨账期 / 退费并存 / 不可判定"),
    "trace": ("跨账期连坐", "同一单号在相邻账期与六路上的出现串成链，判重复计入"),
    "bundle": ("建证据包", "给每条待归因差异裁一份脱敏证据包，附渠道口径摘要与近 N 期先验"),
    "attribute": ("归因", "给出成因排序草稿；模型不可达时降级为规则候选，状态仍待人工判定"),
    "verify": ("反幻觉核对", "草稿引用的证据行、金额、时间、单号逐项与证据包对账"),
    "aggregate": ("同根因聚合", "按成因 + 渠道 + 形态 + 金额分桶收成处置单"),
}

#: 主路径编排版本。阶段集合或顺序变了就 bump：面板与结论单据此判断
#: 「这份汇总是不是同一套阶段跑出来的」。
PIPELINE_VERSION = "align-pipeline/1"

#: 未登记在 :data:`STAGE_ZH` 的形态默认归到这一项（对应受控字典里的
#: ``undetermined``）—— 归因没跑或草稿全被拒时，成因码不能空着，
#: 空着会让这一批差异在聚合层被拒收并**永远不成单**。
UNDETERMINED_CAUSE = "undetermined"

#: 成因出处三值（见 :attr:`PendingItem.cause_source`）。
CAUSE_SOURCE_MODEL = "model_verified"
CAUSE_SOURCE_RULE = "rule_inferred"
CAUSE_SOURCE_NONE = "none"

CAUSE_SOURCE_ZH: dict[str, str] = {
    CAUSE_SOURCE_MODEL: "模型草稿（已过反幻觉核对，仍待人工确认）",
    CAUSE_SOURCE_RULE: "规则推断（未经模型，置信度封顶，仅供人工判定起点）",
    CAUSE_SOURCE_NONE: "未归出成因（待人工判定）",
}


# ═══════════════════════════ 阶段进度 ═══════════════════════════


@dataclass(frozen=True)
class StageReport:
    """一个阶段的执行结论。**失败也留痕** —— 面板要能回答「这一段是没跑
    还是跑挂了」，两者在排障上是两回事。"""

    stage: str
    ok: bool
    #: 本阶段处理掉的条数（口径随阶段而变：索引是 HIS 行数、聚合是差异条数…）
    processed: int = 0
    elapsed_ms: float = 0.0
    error_code: str = ""
    detail: str = ""

    @property
    def stage_zh(self) -> str:
        return STAGE_ZH.get(self.stage, (self.stage, ""))[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "stage_zh": self.stage_zh,
            "ok": self.ok,
            "processed": self.processed,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "error_code": self.error_code,
            "detail": self.detail,
        }


class ProgressReporter:
    """阶段进度回调。

    **面板按阶段名显示进度，不按百分比猜**：八个阶段的耗时差着一到两个
    数量级（索引毫秒级、归因可能几十秒），按总时长线性折算的百分比会在
    最慢的那一段上长时间卡在 90% 附近，看的人不知道它是在跑还是在挂。

    回调本身抛错**不许**带崩主路径：面板那一侧的订阅出问题（连接断了、
    序列化失败）不该让财务科这一期账跑不出结论。回调异常被记成一条
    ``align.stage`` 事件后继续跑。
    """

    def __init__(self, sink: Optional[Callable[[dict[str, Any]], None]] = None) -> None:
        self._sink = sink
        self.events: list[dict[str, Any]] = []

    def __call__(self, report: StageReport, *, total: int, index: int) -> None:
        payload = {
            "event": "align.stage",
            "pipeline_version": PIPELINE_VERSION,
            "stage": report.stage,
            "stage_zh": report.stage_zh,
            "ok": report.ok,
            "processed": report.processed,
            "elapsed_ms": round(report.elapsed_ms, 3),
            "error_code": report.error_code,
            "detail": report.detail,
            "stage_index": index,
            "stage_total": total,
        }
        self.events.append(payload)
        if self._sink is None:
            return
        try:
            self._sink(payload)
        except Exception as exc:  # noqa: BLE001 - 订阅侧的问题不许带崩主路径
            logger.warning("阶段进度回调抛错（该段进度面板上可能不刷新）：%s", exc)

    def as_sink(self) -> Callable[[dict[str, Any]], None]:
        """给 web 层用的裸回调（:class:`ProgressReporter` 自身即回调）。"""
        return self


def _stage_start(
    reporter: Optional[ProgressReporter], stage: str, index: int
) -> float:
    if stage not in STAGE_ZH:
        raise ReconqError(
            "ALIGN_PIPELINE_STAGE_UNKNOWN",
            f"阶段 {stage!r} 不在受控阶段表内（{list(STAGES)}）。"
            "面板按阶段名画进度条，未登记的阶段会变成一段没有名字的等待",
        )
    if reporter is not None:
        reporter(
            StageReport(stage=stage, ok=True, processed=0, detail="开始"),
            total=len(STAGES),
            index=index,
        )
    return time.perf_counter()


def _stage_done(
    reporter: Optional[ProgressReporter],
    stage: str,
    index: int,
    started: float,
    processed: int,
    detail: str = "",
) -> StageReport:
    report = StageReport(
        stage=stage,
        ok=True,
        processed=processed,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        detail=detail,
    )
    if reporter is not None:
        reporter(report, total=len(STAGES), index=index)
    return report


def _stage_failed(
    reporter: Optional[ProgressReporter],
    stage: str,
    index: int,
    started: float,
    exc: BaseException,
) -> StageReport:
    """阶段失败。**降级为 partial 而不是整期回滚**。

    保留已完成阶段的结果并把这一期标成 ``partial``，理由写在模块头：
    整期回滚会把「归因跑完了、聚合崩了」显示成「本期零差异」。
    """
    code = exc.code if isinstance(exc, ReconqError) else "UNEXPECTED_INTERNAL"
    report = StageReport(
        stage=stage,
        ok=False,
        processed=0,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        error_code=code,
        detail=str(exc) if isinstance(exc, ReconqError) else f"{type(exc).__name__}: {exc}",
    )
    logger.warning("主路径阶段 %s 失败（该期标 partial，其余阶段结果保留）：%s", stage, report.detail)
    if reporter is not None:
        reporter(report, total=len(STAGES), index=index)
    return report


# ═══════════════════════════ 待办条目 ═══════════════════════════


@dataclass
class PendingItem:
    """一条进入归因链的差异。

    刻意**同时**挂住三样东西：对齐结论（判据来源）、证据包（归因输入）、
    连坐结论（有的话）。只留结论会让经办人问「凭哪几行判的」时无处可答；
    只留证据包则连「这条为什么进了待办」都说不清。
    """

    key: str
    period: str
    channel: str
    kind: str
    order_no: str
    check_path: str
    diff_amount: str = ""
    #: 对齐器给出的原始结论（有连坐结论时为 None）。
    outcome: Optional[MatchOutcome] = None
    #: 跨账期/跨渠道连坐结论。
    trace: Optional[TraceConclusion] = None
    bundle: Optional[EvidenceBundle] = None
    attribution: Optional[AttributionResult] = None
    #: 歧义分型结论的机器名（:class:`reconq.align.taxonomy.AmbiguityKind`）。
    ambiguity_kind: str = ""
    #: 该条经反幻觉核对后**被采用**的成因码（空串 = 待人工判定）。
    cause_code: str = ""
    cause_rank: int = 1
    cause_confidence: Optional[Decimal] = None
    #: 成因码的出处。**必填且受控** —— 面板与结论单必须能回答
    #: 「这个成因是谁说的」：
    #:
    #: * ``model_verified`` —— 模型草稿且过了反幻觉核对（仍只是草稿）
    #: * ``rule_inferred`` —— 规则基线 Top-1，**未经模型**（模型降级时接管）
    #: * ``none`` —— 压根没归出成因，只能按成因未定成单
    #:
    #: 之所以要这一位：规则候选与模型草稿若都塞进同一个 ``cause_code``，
    #: 面板按成因分组后读的人分不出哪一栏是「模型说的」、哪一栏是
    #: 「规则猜的」—— 而这两者的可信度差着一档。
    cause_source: str = CAUSE_SOURCE_NONE
    #: 逐条证据定位（交办单上「回查这一条」用的行号）。
    evidence: tuple[EvidenceRow, ...] = ()
    needs_manual: bool = True
    note: str = ""

    @property
    def conclusion_key(self) -> str:
        """对齐快照用的结论键：``渠道:账期:单号``。"""
        return f"{self.channel}:{self.period}:{self.order_no or self.key}"

    @property
    def kind_zh(self) -> str:
        from .bundle import KIND_ZH

        return KIND_ZH.get(self.kind, self.kind)

    @property
    def cause_zh(self) -> str:
        entry = CAUSES.get(self.cause_code)
        return entry.zh_name if entry else "待人工判定"

    def evidence_refs(self) -> tuple[str, ...]:
        seen: list[str] = []
        for row in self.evidence:
            if row.ref and row.ref not in seen:
                seen.append(row.ref)
        return tuple(seen)

    def to_entry(self) -> DiscrepancyEntry:
        """转成参与聚合的条目。

        ``disposition`` 恒为「待确认」：本模块**只产出草稿与待办**，处置
        状态是人工点的（见 :mod:`reconq.align.aggregate` 的状态机）。
        """
        return DiscrepancyEntry(
            key=self.key,
            period=self.period,
            channel=self.channel,
            check_path=self.check_path,
            cause_code=self.cause_code,
            diff_amount=to_decimal(self.diff_amount or "0", where=f"差异 {self.check_path} 的差额"),
            ambiguity_kind=self.ambiguity_kind,
            order_no=self.order_no,
            evidence=self.evidence,
            disposition="待确认",
            needs_attribution=self.needs_manual,
            amount_known=bool(self.diff_amount),
            source_file=self.evidence[0].source_file if self.evidence else "",
            first_row_no=self.evidence[0].row_no if self.evidence else None,
            cause_rank=self.cause_rank,
            cause_confidence=self.cause_confidence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "conclusion_key": self.conclusion_key,
            "period": self.period,
            "channel": self.channel,
            "kind": self.kind,
            "kind_zh": self.kind_zh,
            "order_no": self.order_no,
            "check_path": self.check_path,
            "diff_amount": self.diff_amount,
            "ambiguity_kind": self.ambiguity_kind,
            "cause_code": self.cause_code,
            "cause_zh": self.cause_zh,
            "cause_source": self.cause_source,
            "cause_source_zh": CAUSE_SOURCE_ZH.get(self.cause_source, self.cause_source),
            "cause_rank": self.cause_rank,
            "cause_confidence": str(self.cause_confidence) if self.cause_confidence else "",
            "evidence_refs": list(self.evidence_refs()),
            "needs_manual": self.needs_manual,
            "note": self.note,
            "bundle_digest": self.bundle.digest if self.bundle else "",
            "degraded": bool(self.attribution and self.attribution.degraded),
            "degraded_code": (
                self.attribution.error_code if self.attribution and self.attribution.degraded else ""
            ),
        }


# ═══════════════════════════ 主结果 ═══════════════════════════


@dataclass
class PipelineResult:
    """一次主路径运行的完整结论。"""

    period: str
    pipeline_version: str = PIPELINE_VERSION
    stages: tuple[StageReport, ...] = ()
    #: 待人工判定的逐条差异。
    items: tuple[PendingItem, ...] = ()
    #: 聚合后的处置单。
    groups: AggregationResult = field(default_factory=AggregationResult)
    #: 逐阶段证据包（键 = 结论键），供面板反查。
    bundles: dict[str, EvidenceBundle] = field(default_factory=dict)
    #: 归因结果（键 = 结论键）。
    attributions: dict[str, AttributionResult] = field(default_factory=dict)
    #: 反幻觉核对台账。
    verification: Optional[VerificationLedger] = None
    #: 先例建议（键 = 结论键）。**只给建议，不推进任何状态**。
    precedent_hints: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    #: 对齐结论（原始，供面板逐条下钻）。
    align: Optional[AlignAllResult] = None
    #: 六路 + HIS 的行数与合计（口径取账单库，不是本次跑的那几路）。
    row_counts: dict[str, int] = field(default_factory=dict)
    total_amount: str = "0.00"
    elapsed_ms: float = 0.0

    # —— 阶段视图 ———————————————————————————————————————————

    @property
    def stage_map(self) -> dict[str, StageReport]:
        return {r.stage: r for r in self.stages}

    @property
    def failed_stages(self) -> tuple[StageReport, ...]:
        return tuple(r for r in self.stages if not r.ok)

    @property
    def partial(self) -> bool:
        """本期结论**不完整**。

        只要有一阶段失败就是 partial。partial 的期次**不允许出结论单** ——
        「已归因 180 条、聚合那一步崩了」与「本期零差异」在签字页上
        长得一模一样，区别只在经办人心里有没有数。
        """
        return bool(self.failed_stages)

    @property
    def partial_reason(self) -> str:
        if not self.partial:
            return ""
        return "；".join(
            f"{r.stage_zh}({r.stage}) 失败 [{r.error_code}]" for r in self.failed_stages
        )

    def stage(self, name: str) -> Optional[StageReport]:
        return self.stage_map.get(name)

    def stage_timings(self) -> list[dict[str, Any]]:
        """逐段耗时（毫秒），给信息科判断「哪一段慢」。"""
        return [
            {"stage": r.stage, "stage_zh": r.stage_zh, "elapsed_ms": round(r.elapsed_ms, 3),
             "processed": r.processed, "ok": r.ok, "error_code": r.error_code}
            for r in self.stages
        ]

    # —— 汇总视图 ———————————————————————————————————————————

    @property
    def pending(self) -> int:
        return len(self.items)

    @property
    def needs_manual(self) -> int:
        return sum(1 for i in self.items if i.needs_manual)

    @property
    def total_diff_amount(self) -> Decimal:
        total = Decimal("0")
        for item in self.items:
            if item.diff_amount:
                total += to_decimal(item.diff_amount, where=f"差异 {item.key} 的差额")
        return total

    def items_by_cause(self) -> list[tuple[str, str, int, str, str]]:
        """按成因聚合的开工前清单：``(成因码, 中文名, 条数, 金额, 责任方)``。

        按条数降序、金额降序、编码升序 —— 后两级不是装饰：条数与金额都相同
        时顺序必须由编码自身决定，否则同一份数据两次跑面板排序会不同。
        """
        buckets: dict[str, list[PendingItem]] = {}
        for item in self.items:
            buckets.setdefault(item.cause_code or UNDETERMINED_CAUSE, []).append(item)
        rows: list[tuple[str, str, int, str, str]] = []
        for code, group in buckets.items():
            amount = Decimal("0")
            has_amount = False
            for item in group:
                if item.diff_amount:
                    amount += to_decimal(item.diff_amount, where="差异金额合计")
                    has_amount = True
            entry = CAUSES.get(code)
            rows.append(
                (
                    code,
                    entry.zh_name if entry else "待人工判定",
                    len(group),
                    f"{amount:.2f}" if has_amount else "",
                    entry.owner if entry else "",
                )
            )
        rows.sort(key=lambda r: (-r[2], -(Decimal(r[3] or "0")), r[0]))
        return rows

    def by_owner(self) -> dict[str, int]:
        """责任方 → 条数。**这张表就是交办分工表**。"""
        out: dict[str, int] = {}
        for item in self.items:
            entry = CAUSES.get(item.cause_code)
            owner = entry.owner if entry else "待人工判定"
            out[owner] = out.get(owner, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    def by_cause_source(self) -> dict[str, int]:
        """成因出处 → 条数。**模型说的与规则猜的必须能分开数**。

        把两者混进一个数字，面板上「30 条都有成因」会读成「模型归对了 30 条」，
        而模型不可达时那 30 条其实一条都不是它说的。
        """
        out = {CAUSE_SOURCE_MODEL: 0, CAUSE_SOURCE_RULE: 0, CAUSE_SOURCE_NONE: 0}
        for item in self.items:
            out[item.cause_source] = out.get(item.cause_source, 0) + 1
        return {k: v for k, v in out.items() if v}

    def brief_zh(self) -> str:
        """开工前汇总：经办人先看全局再逐条点。"""
        return briefing_zh(self)

    def fingerprint(self) -> str:
        """结论集合指纹。**不含耗时、不含构造时刻** —— 复算比的是结论，
        不是这一期跑了多久。"""
        return align_fingerprint(self.items, groups=self.groups)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "pipeline_version": self.pipeline_version,
            "partial": self.partial,
            "partial_reason": self.partial_reason,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "pending": self.pending,
            "needs_manual": self.needs_manual,
            "total_diff_amount": f"{self.total_diff_amount:.2f}",
            "row_counts": dict(self.row_counts),
            "total_amount": self.total_amount,
            "stages": [r.to_dict() for r in self.stages],
            "stage_timings": self.stage_timings(),
            "briefing": [
                {"cause_code": c, "cause_zh": z, "count": n, "amount": a, "owner": o}
                for c, z, n, a, o in self.items_by_cause()
            ],
            "by_owner": self.by_owner(),
            "by_cause_source": self.by_cause_source(),
            "groups": [g.to_dict() for g in self.groups.groups],
            "items": [i.to_dict() for i in self.items],
            "verification": self.verification.to_dict() if self.verification else {},
            "fingerprint": self.fingerprint(),
        }


# ═══════════════════════════ 指纹 ═══════════════════════════


def align_fingerprint(
    items: Sequence[PendingItem], *, groups: Optional[AggregationResult] = None
) -> str:
    """待办条目 + 处置单的规范摘要。

    规范化规则：``sort_keys`` + 无空格分隔符 + 定点金额字符串 + **不含**
    耗时 / 时刻 / 事件序号。少一条规则，同一份数据两次跑出来的指纹就会不同，
    而「同一账期重复跑结果一致」这条不变量恰恰是靠它测的。
    """
    payload = {
        "items": sorted(
            (
                {
                    "key": i.key,
                    "kind": i.kind,
                    "order_no": i.order_no,
                    "diff_amount": i.diff_amount,
                    "cause_code": i.cause_code,
                    "cause_source": i.cause_source,
                    "ambiguity_kind": i.ambiguity_kind,
                    "evidence": sorted(i.evidence_refs()),
                    "needs_manual": i.needs_manual,
                }
                for i in items
            ),
            key=lambda row: row["key"],
        ),
        "groups": sorted(
            (
                {
                    "group_key": g.group_key,
                    "cause_code": g.cause_code,
                    "channel": g.channel,
                    "size": g.size,
                    "members": sorted(g.member_keys),
                    "disposition": g.disposition,
                }
                for g in (groups.groups if groups else ())
            ),
            key=lambda row: row["group_key"],
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ═══════════════════════════ 开工前汇总 ═══════════════════════════


def briefing_zh(result: PipelineResult) -> str:
    """「本期需要你判的 N 件事」清单。

    刻意做成**一张能直接贴进科务会的表**而不是一串日志：条数、涉及金额、
    建议动作、责任方、证据条数五列齐了，经办人才能先分派再逐条点。
    """
    lines: list[str] = []
    head = f"账期 {result.period} · 对账主路径待办汇总"
    if result.partial:
        head += f"（**本期结论不完整**：{result.partial_reason}）"
    lines.append(head)
    lines.append(
        f"  待判 {result.needs_manual} 件 / 共 {result.pending} 条差异，"
        f"涉及金额 {result.total_diff_amount:.2f} 元 · "
        f"六路 + HIS 落库 {sum(result.row_counts.values())} 行 / "
        f"合计 {result.total_amount} 元 · 耗时 {result.elapsed_ms:.0f} ms"
    )
    if not result.items:
        lines.append("  本期没有需要人工判定的差异。")
        return "\n".join(lines)

    lines.append("")
    lines.append("  按成因（本期需要你判的事）：")
    lines.append("    成因 / 条数 / 金额 / 责任方 / 证据条数 / 建议动作")
    for code, zh, count, amount, owner in result.items_by_cause():
        entry = CAUSES.get(code)
        evidence = sum(
            len(i.evidence_refs()) for i in result.items if (i.cause_code or UNDETERMINED_CAUSE) == code
        )
        action = (entry.suggested_action if entry else "") or "由经办人人工判定：向导出方索取完整单号，系统不猜"
        lines.append(
            f"    {zh}（{code}） {count} 条 / {amount or '（无金额）'} 元 / "
            f"{owner or '待人工判定'} / 证据 {evidence} 条"
        )
        lines.append(f"      建议动作：{action}")

    owners = result.by_owner()
    if owners:
        lines.append("")
        lines.append("  分工：" + "、".join(f"{o} {n} 条" for o, n in owners.items()))
    groups = result.groups.groups
    if groups:
        lines.append(
            f"  已收成处置单 {len(groups)} 张"
            f"（{result.groups.entry_count} 条 → {len(groups)} 张，合并 {result.groups.collapsed} 条）"
        )
    degraded = sum(1 for i in result.items if i.attribution and i.attribution.degraded)
    if degraded:
        lines.append(
            f"  ~ {degraded} 条归因降级（模型不可达/超时/草稿被拒）：成因取自规则基线，"
            "**未经模型**，台账状态未推进，仍待人工判定"
        )
    sources = result.by_cause_source()
    if sources:
        lines.append(
            "  成因出处："
            + "、".join(
                f"{CAUSE_SOURCE_ZH.get(k, k).split('（')[0]} {v} 条"
                for k, v in sources.items()
            )
        )
    verification = result.verification
    if verification and verification.checked:
        lines.append(f"  ~ {verification.render_zh()}")
    return "\n".join(lines)


# ═══════════════════════════ 取数 ═══════════════════════════


def _table_exists(conn: Any, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _load_records(
    conn: Any, period: str
) -> tuple[dict[str, list[AlignRecord]], list[AlignRecord], dict[str, int], str]:
    """从账单库取本期落库行，转成对齐行。

    **读账单库而不是重读渠道文件**：账单文件会被下一次取件覆盖，而台账要
    回答的是「当时比的是哪几行」（见
    :func:`reconq.align.bundle._slice_fingerprint` 的理由）。重读文件会让
    同一期账两次跑出的证据指向不同内容。
    """
    from ..store.bill_store import query_period

    rows = query_period(conn, period)
    records = [record_from_row_dict(r) for r in rows]
    by_channel: dict[str, list[AlignRecord]] = {}
    his: list[AlignRecord] = []
    counts: dict[str, int] = {}
    total = Decimal("0")
    for record in records:
        counts[record.channel] = counts.get(record.channel, 0) + 1
        total += record.net_amount
        if record.channel == AUTHORITATIVE_CHANNEL:
            his.append(record)
        else:
            by_channel.setdefault(record.channel, []).append(record)
    return by_channel, his, dict(sorted(counts.items())), f"{total:.2f}"


def _involved_amount(outcome: MatchOutcome) -> str:
    """这条差异**涉及多少钱**（两侧都取不到时返回空串）。

    与 :attr:`MatchOutcome.diff_amount` 的区别：后者是「两侧差多少」，
    只在两侧都有行时才有值；这里是「这一笔涉及多少」。渠道有 HIS 无这类
    差异没有对面可差，但涉及的钱是明确的 —— 报出来，经办人才知道
    这条值不值得今晚就打电话问收费处。
    """
    for record in (outcome.channel_record, outcome.his_record):
        if record is not None:
            return str(record.net_amount)
    if outcome.candidates:
        return str(sum((c.amount for c in outcome.candidates), Decimal("0")))
    return ""


def _check_path_of(kind: str, order_no: str) -> str:
    """形态 → 台账 ``check_path``。

    形态没登记过前缀时**不编**：留一个 ``align.<kind>:<单号>`` 的形状，
    面板与结论单照样能定位到行，而凭空套一个别的形态前缀会让经办人按
    错误的成因去查。
    """
    prefix = KIND_CHECK_PATH_PREFIX.get(kind)
    if not prefix:
        return f"align.{kind}:{order_no}"
    return f"{prefix}:{order_no}"


def _evidence_of(bundle: EvidenceBundle) -> tuple[EvidenceRow, ...]:
    """从证据包取逐条证据定位（交办单上「回查这一条」用的行号）。

    取**差异行 + 全部候选行 + 同单号历史行**，去重且保序。不截断 ——
    截掉几条等于让信息科拿着单子回查不到底。
    """
    payload = bundle.payload
    rows: list[dict[str, Any]] = []
    diff_row = payload.get("diff_row") or {}
    if diff_row:
        rows.append(diff_row)
    for row in payload.get("candidate_rows") or []:
        rows.append(row)
    for row in payload.get("same_order_rows") or payload.get("same_order_no_rows") or []:
        rows.append(row)
    out: list[EvidenceRow] = []
    seen: set[str] = set()
    for row in rows:
        ref = EvidenceRow.parse_ref(str(row.get("ref") or ""))
        if not ref.ref or ref.ref in seen:
            continue
        seen.add(ref.ref)
        out.append(
            EvidenceRow(
                source_file=ref.source_file,
                row_no=ref.row_no,
                field_name="",
                value=str(row.get("record_id", "")),
            )
        )
    return tuple(out)


# ═══════════════════════════ 主路径 ═══════════════════════════


def run_align_pipeline(
    conn: Any,
    period: str,
    *,
    gateway: Optional[Any] = None,
    contracts_dir: str = "contracts",
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
    amount_bucket_width: Any = DEFAULT_AMOUNT_BUCKET,
    history_periods: int = DEFAULT_HISTORY_PERIODS,
    trace_window_periods: int = DEFAULT_WINDOW_PERIODS,
    instructions: str = "",
    window: Optional[Any] = None,
    run_id: str = "",
    variables: Optional[dict[str, Any]] = None,
) -> PipelineResult:
    """跑完对齐主路径，产出待办条目与处置单。

    :param conn: 账单库连接（只读即可；本函数**不写台账**）。
    :param period: 账期，如 ``2026-W36``。
    :param gateway: :class:`reconq.gateway.model_gateway.ModelGateway`。
        传 ``None`` 时归因走降级路径（规则候选 + 待人工判定）—— 这**不是**
        静默失败：结果里 ``degraded=True`` 且带原因码。
    :param on_progress: 阶段进度回调，入参是 :class:`ProgressReporter`
        构造的阶段事件字典。

    本函数**不推进任何处置状态**，也不写差异台账 —— 它是 Task 42 的
    「跑一遍看看」腿；台账写入与三角色确认流是 Task 43 的活。把两条路
    混在一起会让「我只想看看这一期有多少事」变成一次写操作。
    """
    if not period:
        raise ReconqError(
            "ALIGN_PIPELINE_PERIOD_REQUIRED",
            "跑主路径必须给账期（reconq align-pipeline --period 2026-W36）；"
            "账期是账期实例、台账键与处置单键的共同主键，不给账期就没有"
            "唯一的一份「本期」",
        )

    from ..sources.timezone import period_window as _period_window
    from ..store.schema import BILL_TABLE

    if not _table_exists(conn, BILL_TABLE):
        raise ReconqError(
            "ALIGN_PIPELINE_BILL_TABLE_MISSING",
            f"账单表 {BILL_TABLE} 尚未建表。先跑一次 reconq scan --period {period}"
            "（取数并幂等落库）；主路径读的是账单库里已落的那份行，"
            "不自己再去读渠道文件",
        )

    win = window if window is not None else _period_window(period)
    reporter = ProgressReporter(on_progress)
    stages: list[StageReport] = []
    result = PipelineResult(period=period)
    started_all = time.perf_counter()

    variables = dict(variables or {"period": period})

    def contract_of(channel: str) -> Optional[Any]:
        """按渠道读契约。读不到就返回 ``None``（证据包会明说「未提供契约」
        而不是假装没有相关口径），但**不让整个阶段崩掉** —— 少一段口径
        摘要不该让这期账一条差异都归不出来。"""
        path = os.path.join(contracts_dir, f"{channel}.yml")
        if channel == GLOBAL_CHANNEL or not os.path.isfile(path):
            return None
        try:
            from ..contracts.loader import load_contract

            return load_contract(path, variables)
        except ReconqError as exc:
            logger.warning("渠道 %s 契约读取失败，证据包将不带口径摘要：%s", channel, exc)
            return None

    # ── 1. 取数（不算阶段：它是入口条件，失败就是整期跑不了）──────────
    by_channel, his_records, row_counts, total_amount = _load_records(conn, period)
    result.row_counts = row_counts
    result.total_amount = total_amount

    # ── 2. 索引 ────────────────────────────────────────────────────────
    index_started = _stage_start(reporter, "index", 1)
    from .index import build_sharded_index

    try:
        sharded = build_sharded_index(his_records, period=period, window=win)
        stages.append(
            _stage_done(
                reporter, "index", 1, index_started, len(his_records),
                detail=(
                    f"{sharded.stats().shards} 片 / "
                    f"{sharded.stats().cross_period_entries} 条跨账期"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        stages.append(_stage_failed(reporter, "index", 1, index_started, exc))
        sharded = None

    # ── 3. 对齐 ────────────────────────────────────────────────────────
    align_started = _stage_start(reporter, "align", 2)
    align_result: Optional[AlignAllResult] = None
    try:
        align_result = align_all(by_channel, his_records, period=period, window=win)
        result.align = align_result
        stages.append(
            _stage_done(
                reporter, "align", 2, align_started, align_result.pending_attribution,
                detail=(
                    "、".join(
                        f"{name} {sum(1 for o in cres.outcomes if o.needs_attribution)} 条待归因"
                        for name, cres in sorted(align_result.channels.items())
                        if cres.pending_attribution
                    )
                    or "无待归因差异"
                )
                + (f"；反向单边账 {len(align_result.his_only)} 条" if align_result.his_only else ""),
            )
        )
    except Exception as exc:  # noqa: BLE001
        stages.append(_stage_failed(reporter, "align", 2, align_started, exc))

    items: list[PendingItem] = []

    # ── 4. 歧义分型 ────────────────────────────────────────────────────
    taxonomy_started = _stage_start(reporter, "taxonomy", 3)
    verdicts: dict[str, Any] = {}
    try:
        if align_result is not None:
            for name, cres in sorted(align_result.channels.items()):
                for outcome in cres.outcomes:
                    if outcome.ambiguity is None:
                        continue
                    case = _case_of(outcome, period)
                    if case is None:
                        continue
                    verdict = classify_taxonomy(case, channel=name, order_no=outcome.key.order_no)
                    verdicts[outcome.key.render()] = verdict
            stages.append(
                _stage_done(
                    reporter, "taxonomy", 3, taxonomy_started, len(verdicts),
                    detail="、".join(
                        f"{kind_zh} {sum(1 for v in verdicts.values() if v.kind == kind)}"
                        for kind, kind_zh in sorted(
                            {v.kind: v.kind_zh for v in verdicts.values()}.items()
                        )
                    ) or "无歧义候选",
                )
            )
    except Exception as exc:  # noqa: BLE001
        stages.append(_stage_failed(reporter, "taxonomy", 3, taxonomy_started, exc))

    # ── 5. 逐条建待办（对齐结论 → 台账键 → 形态）────────────────────────
    if align_result is not None:
        for name, cres in sorted(align_result.channels.items()):
            for outcome in cres.outcomes:
                if not outcome.needs_attribution:
                    continue
                order_no = outcome.key.order_no
                kind = outcome.status
                ambiguity_kind = verdicts.get(outcome.key.render()).kind if (
                    outcome.key.render() in verdicts
                ) else ""
                if not ambiguity_kind and outcome.ambiguity is not None:
                    ambiguity_kind = outcome.ambiguity.kind
                check_path = _check_path_of(kind, order_no)
                items.append(
                    PendingItem(
                        key=diff_key(period, name, check_path),
                        period=period,
                        channel=name,
                        kind=kind,
                        order_no=order_no,
                        check_path=check_path,
                        # 对齐器的 ``diff_amount`` 只在**两侧都有行**时才有值
                        # （对得上但对不平）。「渠道有 HIS 无」压根没有对面，
                        # 差额无从算起 —— 但这条涉及的**钱是明确的**（渠道行
                        # 净额），不报出来的话汇总里会出现一条
                        # 「HIS 未落单 /（无金额）」，读的人会以为这条不影响
                        # 账。取渠道行净额，语义是「这一笔涉及多少钱」而非
                        # 「两侧差多少」。
                        diff_amount=outcome.diff_amount or _involved_amount(outcome),
                        outcome=outcome,
                        ambiguity_kind=ambiguity_kind,
                        note=outcome.detail,
                    )
                )
        # 反向单边账：HIS 有、六路无人认领。**渠道记为 ``*``**，不摊到任何
        # 一路名下 —— 那笔压根不属于任何一路渠道，挂到微信头上等于暗示
        # 「是微信漏了」。
        for record in align_result.his_only:
            order_no = record.his_order_no or record.order_no
            check_path = _check_path_of(HIS_ONLY_KIND, order_no)
            items.append(
                PendingItem(
                    key=diff_key(period, GLOBAL_CHANNEL, check_path),
                    period=period,
                    channel=GLOBAL_CHANNEL,
                    kind=HIS_ONLY_KIND,
                    order_no=order_no,
                    check_path=check_path,
                    diff_amount=str(record.net_amount),
                    # 合成一条对齐结论，让反向单边账**同样有证据包可归因**。
                    # 不这么做它会因「没有 outcome 也没有 trace」而跳过归因，
                    # 成因永远空着、在汇总里单列成「未归出成因」—— 而它恰恰
                    # 是最该归因的一类：HIS 收了钱、六路账单里一分钱都没有。
                    outcome=MatchOutcome(
                        key=AlignKey(
                            channel=GLOBAL_CHANNEL,
                            order_no_type="his_order_no",
                            order_no=order_no,
                            period=period,
                        ),
                        status=HIS_ONLY_KIND,
                        his_record=record,
                        diff_amount=str(record.net_amount),
                        detail=(
                            f"HIS 收费 {record.ref} 金额 {record.net_amount} 元"
                            f"（结算方式 {record.settle_type or '未填'}），"
                            "六路渠道账单均无对应记录；该笔可能压根不走这几个渠道"
                            "（现金、医保代收），请确认后归口"
                        ),
                    ),
                    evidence=(
                        EvidenceRow(
                            source_file=record.source_file,
                            row_no=record.source_row_no,
                            field_name="",
                            value=order_no,
                        ),
                    )
                    if record.source_file
                    else (),
                    note=(
                        f"HIS 收费 {record.ref} 金额 {record.net_amount} 元"
                        f"（结算方式 {record.settle_type or '未填'}），"
                        "六路渠道账单均无对应记录；该笔可能压根不走这几个渠道，"
                        "请确认后归口"
                    ),
                )
            )

    # ── 6. 跨账期连坐 ──────────────────────────────────────────────────
    trace_started = _stage_start(reporter, "trace", 4)
    try:
        if align_result is not None:
            claims = [
                claim
                for name in sorted(align_result.channels)
                for claim in claims_from_align_result(align_result.channels[name])
            ]
            order_nos = sorted({i.order_no for i in items if i.order_no})
            periods = period_window_of(period, center=period, window_periods=trace_window_periods)
            if order_nos:
                from .trace import load_entity_nodes

                for order_no in order_nos:
                    nodes = load_entity_nodes(conn, order_no, periods)
                    if not nodes:
                        continue
                    chain = trace_entity(
                        order_no,
                        nodes=nodes,
                        claims=[c for c in claims if (c.order_no or c.his_order_no) == order_no],
                        focus_period=period,
                        window_periods=trace_window_periods,
                    )
                    for conclusion in chain.conclusions:
                        items.append(
                            _item_of_trace(period, conclusion)
                        )
            stages.append(
                _stage_done(
                    reporter, "trace", 4, trace_started,
                    sum(1 for i in items if i.trace is not None),
                    detail=f"追查窗口 {'、'.join(periods)}",
                )
            )
    except Exception as exc:  # noqa: BLE001
        stages.append(_stage_failed(reporter, "trace", 4, trace_started, exc))

    # ── 7. 证据包 ──────────────────────────────────────────────────────
    bundle_started = _stage_start(reporter, "bundle", 5)
    built = 0
    try:
        for item in items:
            source = item.outcome if item.outcome is not None else item.trace
            if source is None:
                continue
            contract = contract_of(item.channel)
            bundle = build_evidence_bundle(
                source,
                contract=contract,
                window=win,
                conn=conn,
                history_periods=history_periods,
            )
            item.bundle = bundle
            item.evidence = _evidence_of(bundle) or item.evidence
            result.bundles[item.conclusion_key] = bundle
            built += 1
        stages.append(
            _stage_done(reporter, "bundle", 5, bundle_started, built,
                        detail=f"证据包 {built} 份")
        )
    except Exception as exc:  # noqa: BLE001
        stages.append(_stage_failed(reporter, "bundle", 5, bundle_started, exc))

    # ── 8. 归因 ────────────────────────────────────────────────────────
    attr_started = _stage_start(reporter, "attribute", 6)
    degraded = 0
    try:
        for item in items:
            if item.bundle is None:
                continue
            outcome = attribute(item.bundle, gateway, instructions=instructions)
            item.attribution = outcome
            result.attributions[item.conclusion_key] = outcome
            if outcome.degraded:
                degraded += 1
            # 先例建议：**只查不算**。它给的是「历史上有人这么判过」，
            # 结论只能由人点（见 reconq.align.precedent 模块头）。
            try:
                signature = _signature_of(item)
                if signature is not None:
                    result.precedent_hints[item.conclusion_key] = tuple(suggest_for(conn, signature))
            except ReconqError as exc:
                logger.debug("先例检索跳过 %s：%s", item.conclusion_key, exc)
        stages.append(
            _stage_done(
                reporter, "attribute", 6, attr_started, len(result.attributions),
                detail=(f"{degraded} 条降级（模型不可达/超时/草稿被拒）" if degraded else "全部走模型"),
            )
        )
    except Exception as exc:  # noqa: BLE001
        stages.append(_stage_failed(reporter, "attribute", 6, attr_started, exc))

    # ── 9. 反幻觉核对 ──────────────────────────────────────────────────
    verify_started = _stage_start(reporter, "verify", 7)
    ledger = VerificationLedger(period=period)
    try:
        for item in items:
            outcome = item.attribution
            if outcome is None or item.bundle is None or not outcome.causes:
                continue
            kept, _rejected = screen_drafts(list(outcome.causes), item.bundle, ledger)
            if not kept:
                # 全被拒：**不取任何一条**当结论。留空 = 待人工判定，
                # 而不是「挑一条看起来最像的」。
                continue
            top = kept[0]
            item.cause_code = top.cause
            item.cause_rank = getattr(top, "rank", 1)
            item.cause_confidence = Decimal(str(top.confidence))
            item.cause_source = CAUSE_SOURCE_MODEL
            item.needs_manual = False
        # 没有模型草稿的条目：成因取规则基线 Top-1 **作为分组依据**，
        # 但仍标待人工判定。规则候选恒非空（字典里留了 undetermined），
        # 因此不会有一条差异因为「没有成因」而永远不成单。
        for item in items:
            if item.cause_code or item.bundle is None:
                continue
            candidates = rank_causes_by_rules(item.bundle)
            if not candidates:
                continue
            top_rule = candidates[0]
            item.cause_code = top_rule.cause_code
            item.cause_confidence = Decimal(str(top_rule.confidence))
            item.cause_source = CAUSE_SOURCE_RULE
        stages.append(
            _stage_done(reporter, "verify", 7, verify_started, ledger.checked,
                        detail=ledger.consistency_rate or "未核对任何草稿")
        )
    except Exception as exc:  # noqa: BLE001
        stages.append(_stage_failed(reporter, "verify", 7, verify_started, exc))

    # ── 10. 同根因聚合 ─────────────────────────────────────────────────
    agg_started = _stage_start(reporter, "aggregate", 8)
    try:
        result.groups = aggregate_discrepancies(
            [i.to_entry() for i in items], amount_bucket_width=amount_bucket_width
        )
        stages.append(
            _stage_done(
                reporter, "aggregate", 8, agg_started, result.groups.entry_count,
                detail=(
                    f"{result.groups.entry_count} 条 → {len(result.groups.groups)} 张处置单"
                    f"（合并 {result.groups.collapsed} 条）"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        stages.append(_stage_failed(reporter, "aggregate", 8, agg_started, exc))

    result.items = tuple(sorted(items, key=lambda i: i.key))
    result.stages = tuple(stages)
    result.verification = ledger
    result.elapsed_ms = (time.perf_counter() - started_all) * 1000.0

    logger.info(
        "账期 %s 主路径完成：待判 %d 件 / %d 条，处置单 %d 张，%s",
        period, result.needs_manual, result.pending, len(result.groups.groups),
        f"部分阶段失败（{result.partial_reason}）" if result.partial else "全部阶段通过",
    )
    return result


# ═══════════════════════════ 内部小工具 ═══════════════════════════


def _case_of(outcome: MatchOutcome, period: str) -> Optional[Any]:
    """对齐结论 → 歧义分型输入。取不到渠道侧金额时返回 ``None``。

    渠道侧金额取不到**不填 0 充数** —— 填 0 会让守恒判据永远成立，
    分型结果就成了假的。返回 None 让 :func:`classify_taxonomy` 落 UNKNOWN
    并在 ``excluded`` 里说清缺什么。
    """
    from .taxonomy import case_from_verdict

    if outcome.ambiguity is None:
        return None
    record = outcome.channel_record
    if record is None:
        return None
    return case_from_verdict(outcome.ambiguity, channel_amount=record.net_amount)


def _item_of_trace(period: str, conclusion: TraceConclusion) -> PendingItem:
    """连坐结论 → 待办条目。

    ``check_path`` 沿用 :func:`reconq.align.trace.chain_to_diff_items` 的
    ``trace.<形态>:<单号>`` 形状：台账上「这一条对不上」与「这一条被人
    认了两次」必须一眼能分开，混进同一类前缀就没法分别确认、分别冲销。
    """
    order_no = conclusion.order_no
    check_path = f"trace.{conclusion.kind}:{order_no}"
    return PendingItem(
        key=diff_key(period, GLOBAL_CHANNEL, check_path),
        period=period,
        channel=GLOBAL_CHANNEL,
        kind=conclusion.kind,
        order_no=order_no,
        check_path=check_path,
        diff_amount=conclusion.diff_amount,
        trace=conclusion,
        ambiguity_kind=conclusion.kind,
        evidence=tuple(EvidenceRow.parse_ref(ref) for ref in conclusion.evidence),
        note=conclusion.detail,
    )


def _signature_of(item: PendingItem) -> Optional[Any]:
    """从证据包算先例特征签名。

    **只对渠道条目查先例**：:func:`reconq.align.precedent.feature_signature`
    要求渠道在六路受控词表内，而连坐结论（渠道记为 ``*``）与反向单边账
    压根不属于任何一路渠道 —— 它们的历史按单号在
    :func:`reconq.align.bundle.query_cause_history` 里查，不走先例库。
    """
    from .precedent import signature_of_bundle

    if item.bundle is None or item.channel == GLOBAL_CHANNEL:
        return None
    return signature_of_bundle(item.bundle)
