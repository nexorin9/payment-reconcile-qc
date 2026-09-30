"""对齐质量指标体系与基础方案同口径对比（Task 38 · 本产品最核心模块的验收尺）。

「对齐准不准」不能靠感觉
------------------------
Task 21–37 把对齐器、歧义拒绝归并、弱匹配、跨账期落点、重复认领一层层建起来
了，但建完之后还差**最要命的一步**：凭什么说它比「财务科今天在用的那套
VLOOKUP 对」更准？说得准是因为**两边在同一份人工标注的金标集上、用同一套
口径**量过，差异能落到具体 case 上。

本模块提供四样东西：

1. **金标集格式**（:class:`GoldenCase` / :func:`load_golden_set`）——
   逐条人工标注的对齐期望：给一组渠道行与 HIS 行，期望对齐器报什么状态。
2. **同一份 case 喂两套实现**（:func:`evaluate_product` /
   :func:`evaluate_baseline`）——本产品跑 :func:`reconq.align.matcher.align_all`，
   基线跑 :func:`reconq.align.vlookup_baseline.run_vlookup_baseline`（即
   Task 37 对拍用的那一份实现，不是另写一份）。两边输出都被压进**同一个标签
   空间**，否则「谁更准」没有可核对的对象。
3. **指标**（:class:`MetricSet`）——召回、精确率、F1、误报率，以及三条本产品
   专属的**硬门禁指标**：歧义漏判率、弱匹配误认领率、重复认领/跨账期漏判率。
4. **门禁**（:meth:`MetricSet.gate_failures`）——歧义漏判率 > 0 即硬失败；
   误报率不得比基线更差；金标集条数不足 :data:`MIN_GOLDEN_CASES` 直接拒评。

指标口径（逐条写清，避免「同一个词两种算法」）
-------------------------------------------
::

    recall / precision / f1
        正类 = 「这条需要人看一眼」（标签 ≠ matched）。
        召回来的是**该报而没报**（漏报，最危险：差额被吃掉、台账干净）。
        精确率管的是**不该报而报了**（误报，浪费经办人时间）。

    false_positive_rate
        FP / (FP + TN)。门禁要求**不高于基线** —— 本产品报得更全是可以接受的，
        报得更吵就不行（财务科不会因为工具太吵而继续用它）。

    ambiguity_miss_rate
        金标里期望「歧义待人工」的 case 中，被实现**报成别的**（尤其是静默
        择一报成 matched）的比例。**必须为 0**：一对多的处置方式各不相同，
        猜错会把整段金额归到错的费用项目上。

    weak_match_wrong_claim_rate
        金标里期望「弱匹配待人工」的 case 中，被实现**自动认领**（报成
        matched / mismatch）的比例。**必须为 0**：单号都没对上，凭金额碰巧
        相同就认领等于把不确定性藏进结论单。

    duplicate_cross_period_miss_rate
        金标里期望「重复认领」或「跨账期落点」的 case 中，被实现**报成已对齐**
        的比例。**必须为 0**：前者让同一笔钱进两次合计，后者让同一笔跨两期计入。

    disposition_accuracy
        标签完全一致的 case 占比。**这是唯一一条两边都能拿满分的指标**，
        也是「同口径」最直观的证据：基线没有歧义/弱匹配/跨期这些概念，
        只能落在 matched 与 channel_only 两个标签上。

    silent_pick_rate
        金标歧义 case 中，实现**挑了一个候选当结论**（命中多条仍报平）的比例。
        基线在这条上是 1.0 —— 它不是「算错」，是**按设计就这么做的**。

为什么「弱匹配误认领率」基线是 0 而不代表它更安全
----------------------------------------------
基线在这条门禁上得 0，是因为它**从不认领** —— 但它换来的是把「不确定」直接
说成「渠道有、HIS 无」。这不是更安全，是**把不确定性换个方向藏起来**：经办人
会拿着一条并不存在的单边账去查收费处。本模块不靠这条指标下结论，靠的是
:attr:`MetricSet.disposition_accuracy` 与 :attr:`MetricSet.silent_pick_rate`
两条同时看 —— 报告里也照实写明这一点，不拿一个好看的 0 去当结论。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from ..errors import ReconqError
from ..sources.timezone import period_window, to_session_tz
from .matcher import (
    AlignRecord,
    MatchStatus,
    align_all,
)
from .vlookup_baseline import run_vlookup_baseline

__all__ = [
    "GOLD_LABEL_ZH",
    "MIN_GOLDEN_CASES",
    "CaseVerdict",
    "GoldenCase",
    "GoldenSet",
    "MetricSet",
    "evaluate_baseline",
    "evaluate_case_product",
    "evaluate_golden_set",
    "load_golden_set",
    "render_comparison_table",
    "render_metric_table",
    "render_report",
    "write_metrics_report",
]


#: 金标集条数下限。**低于这个数的金标集没有否决力** —— 一条差异偶然对一次
#: 是常事，60 条里漏 1 条与漏 6 条才能看出趋势。低于下限时
#: :func:`load_golden_set` 直接报错，不「先跑跑看」。
MIN_GOLDEN_CASES = 60

#: 两侧共用的标签空间。本产品的 :class:`~reconq.align.matcher.MatchStatus`
#: 原样落进来；基线的 ``amount_diff`` 折成 ``mismatch``（两边指同一件事：
#: 对上了但金额对不平），其余原样。
BASELINE_TAG_TO_LABEL: dict[str, str] = {
    "matched": MatchStatus.MATCHED.value,
    "amount_diff": MatchStatus.MISMATCH.value,
    "channel_only": MatchStatus.CHANNEL_ONLY.value,
}

#: 金标里允许出现的标签（``his_only`` 表示反向单边账：HIS 有渠道无）
GOLD_LABELS: frozenset[str] = frozenset(
    {s.value for s in MatchStatus} | {"his_only"}
)

#: 标签 → 中文（终端与报告共用一处说法，与 ``STATUS_ZH`` 同源不同表：
#: 这里多了 ``his_only``，那是 :class:`~reconq.align.matcher.AlignAllResult`
#: 层面的结论，不在单行状态里）
GOLD_LABEL_ZH: dict[str, str] = {
    MatchStatus.MATCHED.value: "已对齐",
    MatchStatus.MISMATCH.value: "已对齐但金额不符",
    MatchStatus.CHANNEL_ONLY.value: "渠道有 HIS 无",
    MatchStatus.AMBIGUOUS.value: "歧义待人工",
    MatchStatus.WEAK_PENDING.value: "弱匹配待人工",
    MatchStatus.CROSS_PERIOD.value: "跨账期落点待确认",
    MatchStatus.DUPLICATE_CLAIM.value: "重复认领待人工",
    "his_only": "HIS 有渠道无",
}

#: 期望「必须交给人」的标签集合。门禁指标只在这三类上算分母 ——
#: 把正例（已对齐）也算进去会让指标被大量平凡 case 稀释，看着好看其实没信息。
_GATED_LABELS: dict[str, frozenset[str]] = {
    "ambiguity_miss_rate": frozenset({MatchStatus.AMBIGUOUS.value}),
    "weak_match_wrong_claim_rate": frozenset({MatchStatus.WEAK_PENDING.value}),
    "duplicate_cross_period_miss_rate": frozenset(
        {MatchStatus.DUPLICATE_CLAIM.value, MatchStatus.CROSS_PERIOD.value}
    ),
}

#: 「自动认领」= 报了 matched / mismatch。用于弱匹配误认领率的分子。
_CLAIMED_LABELS = frozenset({MatchStatus.MATCHED.value, MatchStatus.MISMATCH.value})


# ─────────────────────────── 金标集 ───────────────────────────


@dataclass(frozen=True)
class GoldenCase:
    """一条人工标注的对齐期望。

    一条 case 是**自足**的：给一组渠道行与一组 HIS 行，期望对齐器报什么。
    自足是刻意的 —— 指标要能逐条定位「哪一条错了、错在哪一步」，
    而不是一个必须整批重跑才能复现的黑盒数字。

    ``focus_index`` 指明拿哪一行渠道行的结论去比对。默认取最后一行：
    重复认领那类 case 必须有**两行同号**渠道行，第二行才是被拒的那一次认领。
    """

    case_id: str
    channel: str
    period: str
    layer: str
    expect_label: str
    channel_rows: tuple[AlignRecord, ...]
    his_rows: tuple[AlignRecord, ...]
    focus_index: int = -1
    expect_kind: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.expect_label not in GOLD_LABELS:
            raise ReconqError(
                "GOLDEN_LABEL_UNKNOWN",
                f"金标 {self.case_id} 的期望标签 {self.expect_label!r} 不在受控标签空间内："
                f"{'、'.join(sorted(GOLD_LABELS))}",
            )
        if not self.channel_rows:
            raise ReconqError(
                "GOLDEN_CASE_INVALID",
                f"金标 {self.case_id} 没有任何渠道行；反向单边账（his_only）也须给出行，"
                "否则无法判断是不是被别的行认领掉了",
            )
        if self.focus_index < 0:
            object.__setattr__(self, "focus_index", len(self.channel_rows) - 1)
        if self.focus_index >= len(self.channel_rows):
            raise ReconqError(
                "GOLDEN_CASE_INVALID",
                f"金标 {self.case_id} 的 focus_index={self.focus_index} 越界："
                f"该 case 只有 {len(self.channel_rows)} 行渠道行",
            )

    @property
    def focus_row(self) -> AlignRecord:
        return self.channel_rows[self.focus_index]

    @property
    def is_gated(self) -> bool:
        """该 case 是否落在某条硬门禁指标的分母里。"""
        return any(self.expect_label in labels for labels in _GATED_LABELS.values())

    def describe(self) -> str:
        kind = f"/{self.expect_kind}" if self.expect_kind else ""
        return f"{self.case_id}[{self.layer}] → {self.expect_label}{kind}"


@dataclass(frozen=True)
class GoldenSet:
    """金标集 + 标注元数据。

    元数据（标注来源、标注时间、口径约定）**不是装饰** —— 指标脱离标注
    时间就只是一串没有出处的数字，半年后没人敢拿它当验收依据。
    """

    path: str
    cases: tuple[GoldenCase, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.cases)

    def layer_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.layer] = counts.get(case.layer, 0) + 1
        return dict(sorted(counts.items()))

    def label_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.expect_label] = counts.get(case.expect_label, 0) + 1
        return dict(sorted(counts.items()))


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReconqError(
            "GOLDEN_CASE_INVALID",
            f"金标行的 trade_time={value!r} 不是 ISO 8601 时刻（须带 +08:00 偏移）：{exc}",
        ) from exc
    if moment.tzinfo is None:
        raise ReconqError(
            "GOLDEN_CASE_INVALID",
            f"金标行的 trade_time={value!r} 缺时区偏移；本产品按归一时区判跨账期落点，"
            "不带偏移的时间会让「跨期」这条判据失去可核对性",
        )
    return moment


def _record_from_gold(
    raw: dict[str, Any], *, channel: str, period: str, case_id: str, index: int,
    side: str, source_file: str,
) -> AlignRecord:
    where = f"金标 {case_id} 的 {side}[{index}]"
    # 单号**允许为空** —— 渠道导出行缺商户订单号是真实且常见的形态，
    # 而且正是弱匹配这一层要处理的场景（金标集里就有这类 case）。
    # 金额与时刻则缺一不可：缺金额判不了弱匹配，缺时刻判不了跨账期落点。
    for key in ("amount", "trade_time"):
        if not str(raw.get(key, "")).strip():
            raise ReconqError(
                "GOLDEN_CASE_INVALID",
                f"{where} 缺必填键 {key}；金额与时刻缺一不可 —— "
                "缺时刻就无法判跨账期落点，缺金额就无法判弱匹配"
                "（单号允许为空，那本身就是弱匹配场景）",
            )
    refund_raw = str(raw.get("refund", "") or "").strip()
    return AlignRecord(
        channel=channel,
        period=period,
        order_no=str(raw.get("order_no", "") or "").strip(),
        amount=Decimal(str(raw["amount"])),
        refund=Decimal(refund_raw) if refund_raw else None,
        trade_time=_parse_time(raw["trade_time"]),
        trade_type=str(raw.get("trade_type", "") or ""),
        department=str(raw.get("department", "") or ""),
        settle_type=str(raw.get("settle_type", "") or ""),
        his_order_no=str(raw.get("his_order_no", "") or ""),
        source_file=str(raw.get("source_file", "") or source_file),
        source_row_no=int(raw.get("source_row_no", 0) or index + 1),
        ch_id=int(raw.get("ch_id", 0) or 0),
    )


def parse_golden_case(raw: dict[str, Any], *, path: str) -> GoldenCase:
    case_id = str(raw.get("case_id", "")).strip()
    if not case_id:
        raise ReconqError("GOLDEN_CASE_INVALID", f"金标行缺 case_id：{path}")
    channel = str(raw.get("channel", "")).strip()
    period = str(raw.get("period", "")).strip()
    if not channel or not period:
        raise ReconqError(
            "GOLDEN_CASE_INVALID",
            f"金标 {case_id} 缺 channel 或 period；每条 case 都要能独立复现，"
            "账期写空就没法算跨账期落点",
        )
    ch_rows = tuple(
        _record_from_gold(
            item, channel=channel, period=period, case_id=case_id, index=i,
            side="channel_rows", source_file=f"golden_{case_id}_{channel}.csv",
        )
        for i, item in enumerate(raw.get("channel_rows") or [])
    )
    his_rows = tuple(
        _record_from_gold(
            item, channel="his", period=period, case_id=case_id, index=i,
            side="his_rows", source_file=f"golden_{case_id}_his.csv",
        )
        for i, item in enumerate(raw.get("his_rows") or [])
    )
    if not ch_rows:
        raise ReconqError(
            "GOLDEN_CASE_INVALID",
            f"金标 {case_id} 的 channel_rows 为空；每条 case 至少要有一行渠道行",
        )
    return GoldenCase(
        case_id=case_id,
        channel=channel,
        period=period,
        layer=str(raw.get("layer", "unlabeled") or "unlabeled"),
        expect_label=str(raw.get("expect_label", "")).strip(),
        channel_rows=ch_rows,
        his_rows=his_rows,
        focus_index=int(raw.get("focus_index", -1)),
        expect_kind=str(raw.get("expect_kind", "") or ""),
        note=str(raw.get("note", "") or ""),
    )


def load_golden_set(path: str, *, min_cases: int = MIN_GOLDEN_CASES) -> GoldenSet:
    """读金标集（JSONL，一行一条 case）。

    元数据读同目录的 ``golden_meta.json``（可选）：标注来源、标注时间、
    口径约定。**没有元数据的金标集照读**，但报告里会显式标出「标注来源
    未记录」—— 没有出处的指标不该被当成验收依据。
    """
    if not os.path.isfile(path):
        raise ReconqError(
            "GOLDEN_SET_MISSING",
            f"找不到金标集 {path}；指标没有金标集就只是一串凭感觉写的数字",
        )
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text or text.startswith("//"):
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ReconqError(
                    "GOLDEN_CASE_INVALID",
                    f"金标集 {path} 第 {line_no} 行不是合法 JSON：{exc}",
                ) from exc
            try:
                case = parse_golden_case(raw, path=f"{path}:{line_no}")
            except ReconqError as exc:
                raise ReconqError(
                    exc.code, f"{exc.detail}（{path} 第 {line_no} 行）"
                ) from exc
            if case.case_id in seen:
                raise ReconqError(
                    "GOLDEN_CASE_INVALID",
                    f"金标集 {path} 第 {line_no} 行的 case_id={case.case_id} 与前面的行重复；"
                    "case_id 是指标报告里定位「哪一条错了」的唯一钥匙，不能重复",
                )
            seen.add(case.case_id)
            cases.append(case)
    if not cases:
        raise ReconqError("GOLDEN_SET_EMPTY", f"金标集 {path} 一条 case 都没有")
    if len(cases) < min_cases:
        raise ReconqError(
            "GOLDEN_SET_TOO_SMALL",
            f"金标集 {path} 只有 {len(cases)} 条，低于下限 {min_cases} 条。"
            "一条差异偶然对一次是常事，条数不够就没有否决力；"
            "宁可先补标注，也不要用小样本报一个好看的数字",
        )
    meta_path = os.path.join(os.path.dirname(path), "golden_meta.json")
    meta: dict[str, Any] = {}
    if os.path.isfile(meta_path):
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    else:
        meta = {
            "annotated_source": "（未记录）",
            "annotated_at": "",
            "note": "同目录缺 golden_meta.json：标注来源与时间未记录，"
                    "该指标表不得作为验收依据",
        }
    return GoldenSet(path=path, cases=tuple(cases), meta=meta)


# ─────────────────────── 单条 case 的两侧结论 ───────────────────────


@dataclass(frozen=True)
class CaseVerdict:
    """一套实现在一条金标 case 上的结论。

    两侧都压成同一个结构，指标才有「同口径」的实体基础。
    """

    case_id: str
    layer: str
    channel: str
    #: 落在受控标签空间里的结论
    label: str
    #: 这条是否「需要人看一眼」（label ≠ matched）
    flagged: bool
    #: 命中多条候选却仍给了确定结论（基线按设计如此，本产品恒为 False）
    silently_picked: bool = False
    #: 期望标签
    expect_label: str = ""
    #: 期望歧义形态（金标有记且本产品报了歧义时用于算形态命中率）
    expect_kind: str = ""
    actual_kind: str = ""
    #: 结论回查定位（渠道行 文件:行号）
    ref: str = ""
    detail: str = ""
    note: str = ""

    @property
    def correct(self) -> bool:
        return self.label == self.expect_label

    @property
    def is_ambiguity_case(self) -> bool:
        return self.expect_label == MatchStatus.AMBIGUOUS.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "layer": self.layer,
            "channel": self.channel,
            "label": self.label,
            "label_zh": GOLD_LABEL_ZH.get(self.label, self.label),
            "expect_label": self.expect_label,
            "expect_kind": self.expect_kind,
            "actual_kind": self.actual_kind,
            "flagged": self.flagged,
            "silently_picked": self.silently_picked,
            "correct": self.correct,
            "ref": self.ref,
            "detail": self.detail,
            "note": self.note,
        }


def evaluate_case_product(case: GoldenCase, *, tz: Optional[Any] = None) -> CaseVerdict:
    """本产品（:func:`reconq.align.matcher.align_all`）在一条 case 上的结论。

    标签取法与基线侧**逐条对称**：

    * 焦点渠道行不是 ``matched`` → 取该行状态；
    * 焦点行已对齐但 :attr:`AlignAllResult.his_only` 非空 → ``his_only``；
    * 否则 ``matched``。

    两侧用同一套取法，「谁更准」才是同一把尺子量出来的。
    """
    window = period_window(case.period, tz or to_session_tz(None))
    result = align_all(
        {case.channel: list(case.channel_rows)},
        list(case.his_rows),
        period=case.period,
        window=window,
    )
    outcomes = result.channels[case.channel].outcomes
    focus = outcomes[case.focus_index]
    if focus.status != MatchStatus.MATCHED.value and result.his_only:
        # 焦点行本身有结论时以它为准；his_only 是全局事实，挂在这一条上会
        # 让一条「歧义待人工」被记成「HIS 有渠道无」，把排查方向带偏。
        label = focus.status
    elif focus.status != MatchStatus.MATCHED.value:
        label = focus.status
    elif result.his_only:
        label = "his_only"
    else:
        label = MatchStatus.MATCHED.value
    return CaseVerdict(
        case_id=case.case_id,
        layer=case.layer,
        channel=case.channel,
        label=label,
        flagged=label != MatchStatus.MATCHED.value,
        silently_picked=False,
        expect_label=case.expect_label,
        expect_kind=case.expect_kind,
        actual_kind=focus.ambiguity.kind if focus.ambiguity else "",
        ref=case.focus_row.ref,
        detail=focus.detail,
        note=case.note,
    )


def evaluate_case_baseline(case: GoldenCase) -> CaseVerdict:
    """基线（:func:`reconq.align.vlookup_baseline.run_vlookup_baseline`）的结论。

    用的是 Task 37 对拍时那**同一份**实现（不是另写一份）—— 指标体系最忌讳
    「基线有两份实现」，那样报告里的基线数字与门禁里的基线数字来自不同代码，
    「优于基线」这句话就没有可核对的对象。

    标签取法与产品侧对称：``amount_diff`` 折成 ``mismatch``（同一件事），
    ``matched`` 且存在没被引用的 HIS 记录 → ``his_only``。
    """
    verdicts, referenced = run_vlookup_baseline(
        list(case.channel_rows), list(case.his_rows)
    )
    verdict = verdicts[case.focus_index]
    label = BASELINE_TAG_TO_LABEL.get(verdict.tag, verdict.tag)
    leftover = [r for r in case.his_rows if r.order_no not in referenced]
    if label == MatchStatus.MATCHED.value and leftover:
        label = "his_only"
    return CaseVerdict(
        case_id=case.case_id,
        layer=case.layer,
        channel=case.channel,
        label=label,
        flagged=label != MatchStatus.MATCHED.value,
        # 命中多条仍按「取第一条」当结论 —— 这就是静默择一本身
        silently_picked=verdict.hit_count > 1,
        expect_label=case.expect_label,
        ref=case.focus_row.ref,
        detail=(
            f"基线命中 {verdict.hit_count} 条，取第一条"
            + (f"，丢掉 {len(verdict.dropped)} 条" if verdict.dropped else "")
        ),
        note=case.note,
    )


# ─────────────────────────── 指标 ───────────────────────────


def _ratio(numerator: int, denominator: int) -> float:
    """比例。**分母为 0 时返回 0.0 而不是 1.0** —— 「没这类 case」不等于
    「这类 case 全对」，把它算成 1.0 会让门禁在真出问题时看不出差别。"""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


@dataclass
class MetricSet:
    """一套实现在一份金标集上的全部指标。"""

    implementation: str
    total: int = 0
    #: 正类 = 需要人看一眼（金标标签 ≠ matched）
    positives: int = 0
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    correct: int = 0
    #: 歧义形态命中率（金标记了形态且本产品报了歧义的那些 case 里，形态对的）
    kind_total: int = 0
    kind_hits: int = 0
    #: 各门禁指标的分子/分母：{指标名: (错, 总)}
    gate_numerators: dict[str, int] = field(default_factory=dict)
    gate_denominators: dict[str, int] = field(default_factory=dict)
    gate_failure_detail: dict[str, list[str]] = field(default_factory=dict)
    silent_picks: int = 0
    verdicts: tuple[CaseVerdict, ...] = ()

    # —— 基础四率 ——

    @property
    def recall(self) -> float:
        return _ratio(self.true_positive, self.positives)

    @property
    def precision(self) -> float:
        return _ratio(self.true_positive, self.true_positive + self.false_positive)

    @property
    def f1(self) -> float:
        if self.precision + self.recall <= 0:
            return 0.0
        return 2 * self.precision * self.recall / (self.precision + self.recall)

    @property
    def false_positive_rate(self) -> float:
        return _ratio(self.false_positive, self.false_positive + self.true_negative)

    @property
    def disposition_accuracy(self) -> float:
        return _ratio(self.correct, self.total)

    @property
    def ambiguity_kind_accuracy(self) -> float:
        return _ratio(self.kind_hits, self.kind_total)

    @property
    def silent_pick_rate(self) -> float:
        amb = self.gate_denominators.get("ambiguity_miss_rate", 0)
        return _ratio(self.silent_picks, amb)

    # —— 三条硬门禁指标 ——

    def _gate_rate(self, name: str) -> float:
        return _ratio(self.gate_numerators.get(name, 0), self.gate_denominators.get(name, 0))

    @property
    def ambiguity_miss_rate(self) -> float:
        return self._gate_rate("ambiguity_miss_rate")

    @property
    def weak_match_wrong_claim_rate(self) -> float:
        return self._gate_rate("weak_match_wrong_claim_rate")

    @property
    def duplicate_cross_period_miss_rate(self) -> float:
        return self._gate_rate("duplicate_cross_period_miss_rate")

    def gate_failures(self, baseline: Optional["MetricSet"] = None) -> list[str]:
        """门禁未过的项。**空列表 = 通过**。

        三条硬门禁（歧义漏判 / 弱匹配误认领 / 重复认领·跨账期漏判）任一
        大于 0 即失败；另加一条相对门禁：**误报率不得高于基线**。
        """
        failures: list[str] = []
        for name, value in (
            ("歧义漏判率", self.ambiguity_miss_rate),
            ("弱匹配误认领率", self.weak_match_wrong_claim_rate),
            ("重复认领/跨账期漏判率", self.duplicate_cross_period_miss_rate),
        ):
            if value > 0:
                total = self.gate_denominators.get(
                    name if name in self.gate_denominators else _GATE_KEY_BY_ZH[name], 0
                )
                bad = self.gate_numerators.get(_GATE_KEY_BY_ZH[name], 0)
                cases = "、".join(self.gate_failure_detail.get(_GATE_KEY_BY_ZH[name], [])[:5])
                failures.append(
                    f"{name} {value:.1%}（{bad}/{total} 条）：{cases}"
                )
        if baseline is not None and self.false_positive_rate > baseline.false_positive_rate:
            failures.append(
                f"误报率 {self.false_positive_rate:.1%} 高于基线 "
                f"{baseline.false_positive_rate:.1%}：报得更吵不是更准"
            )
        return failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "implementation": self.implementation,
            "total": self.total,
            "positives": self.positives,
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "true_negative": self.true_negative,
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "f1": round(self.f1, 4),
            "false_positive_rate": round(self.false_positive_rate, 4),
            "disposition_accuracy": round(self.disposition_accuracy, 4),
            "ambiguity_kind_accuracy": round(self.ambiguity_kind_accuracy, 4),
            "ambiguity_miss_rate": round(self.ambiguity_miss_rate, 4),
            "weak_match_wrong_claim_rate": round(self.weak_match_wrong_claim_rate, 4),
            "duplicate_cross_period_miss_rate": round(
                self.duplicate_cross_period_miss_rate, 4
            ),
            "silent_pick_rate": round(self.silent_pick_rate, 4),
            "gate_denominators": dict(self.gate_denominators),
            "gate_numerators": dict(self.gate_numerators),
        }


#: 中文指标名 → 内部键（:meth:`MetricSet.gate_failures` 拼文案用）
_GATE_KEY_BY_ZH: dict[str, str] = {
    "歧义漏判率": "ambiguity_miss_rate",
    "弱匹配误认领率": "weak_match_wrong_claim_rate",
    "重复认领/跨账期漏判率": "duplicate_cross_period_miss_rate",
}


def _compute(implementation: str, verdicts: Sequence[CaseVerdict]) -> MetricSet:
    """把逐条结论汇成指标。

    分子口径逐条写在这里（而不是散在各处），是为了让「同一个词只有一种算法」：

    * ``ambiguity_miss_rate``       分母=期望歧义；分子=没报歧义（含静默择一）
    * ``weak_match_wrong_claim_rate`` 分母=期望弱匹配；分子=自动认领成
      matched/mismatch
    * ``duplicate_cross_period_miss_rate`` 分母=期望重复认领/跨账期；
      分子=报成已对齐
    """
    metrics = MetricSet(implementation=implementation, total=len(verdicts))
    for name, labels in _GATED_LABELS.items():
        metrics.gate_denominators[name] = 0
        metrics.gate_numerators[name] = 0
        metrics.gate_failure_detail[name] = []
    for verdict in verdicts:
        gold_positive = verdict.expect_label != MatchStatus.MATCHED.value
        pred_positive = verdict.flagged
        if gold_positive:
            metrics.positives += 1
        if gold_positive and pred_positive:
            metrics.true_positive += 1
        elif gold_positive and not pred_positive:
            metrics.false_negative += 1
        elif (not gold_positive) and pred_positive:
            metrics.false_positive += 1
        else:
            metrics.true_negative += 1
        if verdict.correct:
            metrics.correct += 1
        if verdict.silently_picked:
            metrics.silent_picks += 1
        if verdict.expect_kind and verdict.actual_kind:
            metrics.kind_total += 1
            if verdict.actual_kind == verdict.expect_kind:
                metrics.kind_hits += 1

        for name, labels in _GATED_LABELS.items():
            if verdict.expect_label not in labels:
                continue
            metrics.gate_denominators[name] += 1
            bad = False
            if name == "ambiguity_miss_rate":
                bad = verdict.label != MatchStatus.AMBIGUOUS.value
            elif name == "weak_match_wrong_claim_rate":
                bad = verdict.label in _CLAIMED_LABELS
            else:
                bad = verdict.label == MatchStatus.MATCHED.value
            if bad:
                metrics.gate_numerators[name] += 1
                metrics.gate_failure_detail[name].append(
                    f"{verdict.case_id}（期望 {verdict.expect_label}，实得 {verdict.label}）"
                )
    metrics.verdicts = tuple(verdicts)
    return metrics


def evaluate_golden_set(
    golden: GoldenSet, *, tz: Optional[Any] = None
) -> tuple[MetricSet, MetricSet]:
    """在**同一份**金标集上跑本产品与基线，返回 ``(本产品, 基线)``。"""
    product = [evaluate_case_product(case, tz=tz) for case in golden.cases]
    baseline = [evaluate_case_baseline(case) for case in golden.cases]
    return _compute("本产品（拒绝归并对齐器）", product), _compute(
        "基线（按单号逐笔 VLOOKUP 对）", baseline
    )


# ─────────────────────────── 渲染 ───────────────────────────


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _metric_rows(product: MetricSet, baseline: MetricSet) -> list[tuple[str, str, str]]:
    return [
        ("金标 case 总数", str(product.total), str(baseline.total)),
        ("正类（需人看一眼）", str(product.positives), str(baseline.positives)),
        ("召回 recall", _pct(product.recall), _pct(baseline.recall)),
        ("精确率 precision", _pct(product.precision), _pct(baseline.precision)),
        ("F1", f"{product.f1:.3f}", f"{baseline.f1:.3f}"),
        ("误报率 FP/(FP+TN)", _pct(product.false_positive_rate), _pct(baseline.false_positive_rate)),
        ("结论一致率", _pct(product.disposition_accuracy), _pct(baseline.disposition_accuracy)),
        ("形态判对率", _pct(product.ambiguity_kind_accuracy), "—（基线无形态概念）"),
        ("歧义漏判率", _pct(product.ambiguity_miss_rate), _pct(baseline.ambiguity_miss_rate)),
        ("弱匹配误认领率", _pct(product.weak_match_wrong_claim_rate), _pct(baseline.weak_match_wrong_claim_rate)),
        (
            "重复认领/跨账期漏判率",
            _pct(product.duplicate_cross_period_miss_rate),
            _pct(baseline.duplicate_cross_period_miss_rate),
        ),
        ("静默择一率", _pct(product.silent_pick_rate), _pct(baseline.silent_pick_rate)),
    ]


def render_comparison_table(product: MetricSet, baseline: MetricSet) -> str:
    """终端并排对比表。"""
    width = max(len(label) for label, _, _ in _metric_rows(product, baseline))
    head = f"{'指标':<{width}}  {'本产品':>14}  {'基线':>14}"
    lines = [head, "-" * (width + 34)]
    for label, left, right in _metric_rows(product, baseline):
        lines.append(f"{label:<{width}}  {left:>14}  {right:>14}")
    return "\n".join(lines)


def render_metric_table(metrics: MetricSet) -> str:
    """单侧指标表（``--json`` 之外的默认输出之一）。"""
    rows = _metric_rows(metrics, metrics)
    width = max(len(label) for label, _, _ in rows)
    lines = [f"{metrics.implementation}", "-" * (width + 20)]
    for label, left, _ in rows:
        lines.append(f"{label:<{width}}  {left:>14}")
    return "\n".join(lines)


def _meta_line(meta: dict[str, Any]) -> str:
    at = str(meta.get("annotated_at", "") or "（未记录）")
    return f"标注时间：{at}"


def _meta_source(meta: dict[str, Any]) -> str:
    return str(meta.get("annotated_source", "") or "（未记录）")


def render_report(
    golden: GoldenSet, product: MetricSet, baseline: MetricSet
) -> str:
    """终端完整报告：口径 + 对比 + 门禁 + 逐条差异。"""
    failures = product.gate_failures(baseline)
    lines = [
        "对齐质量指标（本产品 vs 基础方案 · 同口径）",
        f"金标集 {golden.path} · {golden.total} 条"
        + (
            f"（下限 {MIN_GOLDEN_CASES} 条）"
            if golden.total >= MIN_GOLDEN_CASES
            else ""
        ),
        _meta_line(golden.meta),
        "分层：" + "、".join(f"{k} {v}" for k, v in golden.layer_counts().items()),
        "金标标签：" + "、".join(
            f"{GOLD_LABEL_ZH.get(k, k)} {v}" for k, v in golden.label_counts().items()
        ),
        "",
        render_comparison_table(product, baseline),
        "",
        "门禁：" + ("全部通过" if not failures else f"{len(failures)} 项未过"),
    ]
    lines.extend(f"  ✗ {item}" for item in failures)
    if failures and product.gate_failure_detail:
        lines.append("未过门禁的 case 明细：")
        for cases in product.gate_failure_detail.values():
            if cases:
                lines.append("  " + "、".join(cases))
    wrong = [v for v in product.verdicts if not v.correct]
    if wrong:
        lines.append("")
        lines.append(f"本产品结论与金标不一致 {len(wrong)} 条（逐条可查）：")
        for verdict in wrong[:20]:
            lines.append(
                f"  · {verdict.case_id}[{verdict.layer}] {verdict.ref}："
                f"期望 {GOLD_LABEL_ZH.get(verdict.expect_label, verdict.expect_label)}，"
                f"实得 {GOLD_LABEL_ZH.get(verdict.label, verdict.label)}"
                + (f"（{verdict.detail}）" if verdict.detail else "")
            )
        if len(wrong) > 20:
            lines.append(f"  …… 另有 {len(wrong) - 20} 条，详见 out/metrics-report.md")
    return "\n".join(lines)


#: 指标表的 Markdown 表头（报告与 README 共用同一份渲染）
_MD_HEADER = (
    "| 指标 | 本产品（拒绝归并对齐器） | 基线（按单号逐笔 VLOOKUP 对） |\n"
    "|------|------|------|"
)


def write_metrics_report(
    out_path: str,
    golden: GoldenSet,
    product: MetricSet,
    baseline: MetricSet,
    *,
    written_at: Optional[str] = None,
) -> str:
    """写 ``out/metrics-report.md``。

    报告里**必须**同时给出：样例规模、计算口径、以及这些数字**不能**说明什么。
    只报一个好看的召回率而不写分母是 60 条还是 600 条，等于没报。
    """
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    stamp = written_at or datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    failures = product.gate_failures(baseline)
    rows = _metric_rows(product, baseline)
    width = max(len(label) for label, _, _ in rows)

    wrong = [v for v in product.verdicts if not v.correct]
    base_wrong = [v for v in baseline.verdicts if not v.correct]
    base_silent = [v for v in baseline.verdicts if v.silently_picked]

    lines: list[str] = [
        "# 对齐质量指标报告 · 本产品 vs 基础方案",
        "",
        f"- 生成时间：{stamp}",
        f"- 金标集：`{golden.path}`（{golden.total} 条，下限 {MIN_GOLDEN_CASES} 条）",
        f"- {_meta_line(golden.meta)}",
        f"- 标注来源：{_meta_source(golden.meta)}",
        f"- 账期：{sorted({c.period for c in golden.cases})}",
        f"- 门禁：{'全部通过' if not failures else f'**{len(failures)} 项未过**'}",
        "",
        "## 1 样例规模",
        "",
        "| 分层 | 条数 |",
        "|------|------|",
    ]
    for layer, count in golden.layer_counts().items():
        lines.append(f"| {layer} | {count} |")
    lines += ["", "| 金标标签 | 条数 |", "|------|------|"]
    for label, count in golden.label_counts().items():
        lines.append(f"| {GOLD_LABEL_ZH.get(label, label)}（{label}） | {count} |")
    lines += [
        "",
        f"每条 case 自足（给一组渠道行 + 一组 HIS 行，期望报什么），"
        f"可单条复现；三条硬门禁指标的分母分别是"
        f"期望歧义 {product.gate_denominators.get('ambiguity_miss_rate', 0)} 条、"
        f"期望弱匹配 {product.gate_denominators.get('weak_match_wrong_claim_rate', 0)} 条、"
        f"期望重复认领/跨账期 {product.gate_denominators.get('duplicate_cross_period_miss_rate', 0)} 条。",
        "",
        "### 1.1 标注口径（`golden_meta.json`）",
        "",
    ]
    caliber = golden.meta.get("caliber_notes")
    if caliber:
        lines.extend(f"- {note}" for note in caliber)
    else:
        lines.append("- （金标集未记录标注口径）")
    lines += [
        "",
        "## 2 计算口径",
        "",
        "| 指标 | 定义 |",
        "|------|------|",
        "| recall | 召回率 = TP / (TP + FN)。正类 =「需要人看一眼」（标签 ≠ matched）。管的是**该报而没报**。 |",
        "| precision | 精确率 = TP / (TP + FP)。管的是**不该报而报了**。 |",
        "| F1 | precision 与 recall 的调和平均。 |",
        "| 误报率 | FP / (FP + TN)。门禁要求不高于基线 —— 报得更吵不是更准。 |",
        "| 结论一致率 | 标签完全一致的 case 占比。**唯一一条两边都能拿分的指标**，也是「同口径」最直观的证据。 |",
        "| 形态判对率 | 金标记了期望形态、且实得报出了形态的 case 里，形态判对的比例（覆盖拆笔/合并支付/退费并存/金额相同/弱匹配/重复认领六种形态）。 |",
        "| 歧义漏判率 | 期望歧义的 case 中被报成别的（尤其静默择一报成已对齐）的比例。**门禁：必须 0**。 |",
        "| 弱匹配误认领率 | 期望弱匹配的 case 中被自动认领（报成已对齐/金额不符）的比例。**门禁：必须 0**。 |",
        "| 重复认领/跨账期漏判率 | 期望重复认领或跨账期落点的 case 中被报成已对齐的比例。**门禁：必须 0**。 |",
        "| 静默择一率 | 期望歧义的 case 中命中多条却仍给确定结论的比例。 |",
        "",
        "两侧的标签取法**逐条对称**：焦点渠道行不是已对齐就取该行状态；"
        "焦点行已对齐但存在无人认领的 HIS 记录则记 `his_only`；否则记 `matched`。"
        "基线的 `amount_diff` 折成 `mismatch`（同一件事）。"
        "基线实现是 `reconq.align.vlookup_baseline.run_vlookup_baseline` —— 与"
        " `scripts/baseline_compare.py` 对拍用的是**同一份**代码。",
        "",
        "## 3 指标对比",
        "",
        _MD_HEADER,
    ]
    for label, left, right in rows:
        lines.append(f"| {label.ljust(width)} | {left} | {right} |")

    lines += [
        "",
        "## 4 门禁",
        "",
        "| 门禁 | 阈值 | 实测 | 结论 |",
        "|------|------|------|------|",
        f"| 歧义漏判率 | = 0 | {_pct(product.ambiguity_miss_rate)} | "
        f"{'通过' if product.ambiguity_miss_rate == 0 else '**未过**'} |",
        f"| 弱匹配误认领率 | = 0 | {_pct(product.weak_match_wrong_claim_rate)} | "
        f"{'通过' if product.weak_match_wrong_claim_rate == 0 else '**未过**'} |",
        f"| 重复认领/跨账期漏判率 | = 0 | {_pct(product.duplicate_cross_period_miss_rate)} | "
        f"{'通过' if product.duplicate_cross_period_miss_rate == 0 else '**未过**'} |",
        f"| 误报率不高于基线 | ≤ {_pct(baseline.false_positive_rate)} | "
        f"{_pct(product.false_positive_rate)} | "
        f"{'通过' if product.false_positive_rate <= baseline.false_positive_rate else '**未过**'} |",
    ]
    if failures:
        lines += ["", "未过项："] + [f"- {item}" for item in failures]

    lines += [
        "",
        "## 5 逐条差异",
        "",
        f"本产品与金标不一致 **{len(wrong)}** 条；基线与金标不一致 **{len(base_wrong)}** 条，"
        f"其中 **{len(base_silent)}** 条是静默择一（命中多条仍按第一条当结论）。",
        "",
        "### 5.1 本产品不一致的 case",
        "",
    ]
    if wrong:
        lines += [
            "| case | 分层 | 定位 | 期望 | 实得 | 说明 |",
            "|------|------|------|------|------|------|",
        ]
        for verdict in wrong:
            lines.append(
                f"| {verdict.case_id} | {verdict.layer} | {verdict.ref} | "
                f"{GOLD_LABEL_ZH.get(verdict.expect_label, verdict.expect_label)} | "
                f"{GOLD_LABEL_ZH.get(verdict.label, verdict.label)} | {verdict.detail} |"
            )
    else:
        lines.append("（无：全部 case 结论与金标一致）")

    lines += ["", "### 5.2 基线静默择一的 case（前三类各取前 10 条）", ""]
    if base_silent:
        lines += ["| case | 分层 | 定位 | 金标期望 | 基线结论 | 基线做了什么 |", "|------|------|------|------|------|------|"]
        for verdict in base_silent[:30]:
            lines.append(
                f"| {verdict.case_id} | {verdict.layer} | {verdict.ref} | "
                f"{GOLD_LABEL_ZH.get(verdict.expect_label, verdict.expect_label)} | "
                f"{GOLD_LABEL_ZH.get(verdict.label, verdict.label)} | {verdict.detail} |"
            )
    else:
        lines.append("（无）")

    lines += [
        "",
        "## 6 这些数字不能说明什么",
        "",
        "1. **金标集是脱敏样例，不是院内真实账期。** 结论一致率高只说明"
        "「在这 60 余条构造样本上与人工标注一致」，不构成对真实账期的验收。",
        "2. **基线在「弱匹配误认领率」上得 0，不代表它更安全。** 它从不认领，"
        "但代价是把「不确定」说成「渠道有、HIS 无」—— 把不确定性换个方向藏起来，"
        "经办人会拿着一条并不存在的单边账去查收费处。要一起看「结论一致率」与"
        "「静默择一率」两行。",
        "3. **成对多的拆分与合并不可分。** 拆笔（1 渠道单 → N HIS 记录）与"
        "合并支付（N HIS 记录 → 1 渠道单）在字段层面同形，系统侧判据相同，"
        "形态判对率这一项不构成对成因判别的验收。",
        "4. **金额容差 0.01 元，两侧同值。** 金额不符这一类只在"
        "差额超过容差时才成立；容差本身是产品决策，不是从金标集学出来的。",
        "5. **未实连真实渠道端点与院内 HIS 库。** 本报告度量的是**对齐逻辑**，"
        "取数侧走的是文件通道与协议替身；真实取数口径的验收见数据源章节。",
        "",
        "## 7 复现",
        "",
        "```bash",
        "PYTHONPATH=src python3 -m reconq align-metrics \\",
        "  --golden tests/fixtures/align/golden.jsonl --out out/metrics-report.md",
        "```",
        "",
    ]
    lines += [
        "",
        "## 6.1 金标集自记的局限",
        "",
    ]
    limitations = golden.meta.get("known_limitations")
    if limitations:
        lines.extend(f"- {item}" for item in limitations)
    else:
        lines.append("- （金标集未记录局限）")

    text = "\n".join(lines)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return out_path
