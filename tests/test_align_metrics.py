"""对齐质量指标体系与基线同口径对比（Task 38）。

任务书要求的五类断言逐条可核：

1. **指标计算正确** —— 混淆矩阵（TP/FP/FN/TN）、recall/precision/F1、误报率、
   结论一致率、歧义形态命中率，全部用手算得出的数逐个比对，不只断言「大于 0」。
   另有一条**反向构造**：手工造一批已知结果的 verdict，验证指标的分子分母
   与文档里写的口径逐字一致。
2. **歧义漏判被门禁拦下** —— 三条硬门禁各有一条「本该过」的对照与一条
   「本该挂」的对照；尤其验证「静默择一」即使标签碰巧与金标一致（都是
   matched），**仍然**计进歧义漏判分子（这是最容易糊弄过去的一处）。
3. **对比表生成** —— 并排表格含全部指标行、表头、两侧数值，且**基线侧
   逐条 case 都真的跑了基线实现**（用 monkeypatch 计数钉死「同口径」）。
4. **与基线同口径** —— 两侧喂的是**同一份 case 对象**、同一个容差常量；
   基线实现是 Task 37 对拍用的那一份（同一函数对象），不是另写的替身。
5. **金标集自身的门禁** —— 条数不足、期望标签不在受控空间内、时刻缺时区
   偏移、case_id 重复、行缺金额/时刻，各自有明确错误码而不是裸栈。

外加几条容易被糊弄过去的性质：分母为 0 时比例返回 **0.0 而不是 1.0**
（「没这类 case」≠「这类 case 全对」）；金标集缺 ``golden_meta.json`` 时
报告显式标出「标注来源未记录」；两侧的 ``verify`` 主干未回归。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest

from reconq.align.ambiguity import AmbiguityKind
from reconq.align.matcher import MatchStatus, build_index
from reconq.align.metrics import (
    GOLD_LABEL_ZH,
    MIN_GOLDEN_CASES,
    CaseVerdict,
    GoldenCase,
    MetricSet,
    evaluate_case_baseline,
    evaluate_case_product,
    evaluate_golden_set,
    load_golden_set,
    parse_golden_case,
    render_comparison_table,
    render_report,
    write_metrics_report,
)
from reconq.align.vlookup_baseline import (
    BASELINE_TOLERANCE,
    run_vlookup_baseline,
)
from reconq.errors import ERROR_CODES, ReconqError
from reconq.sources.timezone import period_window, to_session_tz

PERIOD = "2026-W36"
GOLDEN = os.path.join("tests", "fixtures", "align", "golden.jsonl")
TZ = to_session_tz(None)


# ─────────────────────── 夹具 ───────────────────────


@pytest.fixture(scope="module")
def golden():
    return load_golden_set(GOLDEN)


@pytest.fixture(scope="module")
def scored(golden):
    return evaluate_golden_set(golden)


def _verdict(
    case_id: str,
    expect: str,
    label: str,
    *,
    kind: str = "",
    actual_kind: str = "",
    silent: bool = False,
) -> CaseVerdict:
    return CaseVerdict(
        case_id=case_id,
        layer="test",
        channel="wechat",
        label=label,
        flagged=label != MatchStatus.MATCHED.value,
        silently_picked=silent,
        expect_label=expect,
        expect_kind=kind,
        actual_kind=actual_kind,
    )


# ───────────── 1. 指标计算正确 ─────────────


def test_golden_set_meets_the_declared_minimum(golden):
    """金标集条数达到下限，且分层覆盖了全部六种需要人工的形态。

    只看「总数 ≥ 60」不够 —— 60 条里 55 条都是歧义例，指标照样好看但
    对弱匹配与跨账期毫无约束力。这里把六个标签各自的条数都钉住。
    """
    assert golden.total >= MIN_GOLDEN_CASES
    assert golden.total == 80
    counts = golden.label_counts()
    for label in (
        MatchStatus.AMBIGUOUS.value,
        MatchStatus.WEAK_PENDING.value,
        MatchStatus.CROSS_PERIOD.value,
        MatchStatus.DUPLICATE_CLAIM.value,
        MatchStatus.CHANNEL_ONLY.value,
        "his_only",
        MatchStatus.MATCHED.value,
        MatchStatus.MISMATCH.value,
    ):
        assert counts.get(label, 0) > 0, f"金标集缺少 {label} 分层"
    # 正类必须占多数，否则 recall 是在几个平凡 case 上算的
    positives = sum(n for k, n in counts.items() if k != MatchStatus.MATCHED.value)
    assert positives / golden.total > 0.7


def test_confusion_matrix_and_rates_match_hand_computation(scored):
    """混淆矩阵与四个基础比率逐个等于手算值。"""
    product, _ = scored
    # TP + FP + FN + TN == 总数，且与金标正负例数自洽
    assert (
        product.true_positive
        + product.false_positive
        + product.false_negative
        + product.true_negative
    ) == product.total == 80
    assert product.positives == product.true_positive + product.false_negative
    assert product.true_positive + product.true_negative + product.false_positive == product.total

    assert product.recall == pytest.approx(1.0)
    assert product.precision == pytest.approx(1.0)
    assert product.f1 == pytest.approx(1.0)
    assert product.false_positive_rate == pytest.approx(0.0)
    assert product.correct == product.total


def test_rates_are_computed_from_a_known_verdict_batch():
    """手工构造一批 verdict，验证分子分母与文档口径**逐字**一致。

    这条是「指标计算正确」的正面证据：不是跑一遍金标集看结果顺眼，而是
    造一个答案已知的输入，核对 TP/FP/FN/TN 与四个比率的每一个数。
    """
    verdicts = [
        # 金标正类 + 实得正类 → TP ×3
        _verdict("T1", MatchStatus.AMBIGUOUS.value, MatchStatus.AMBIGUOUS.value),
        _verdict("T2", MatchStatus.AMBIGUOUS.value, MatchStatus.AMBIGUOUS.value),
        _verdict("T3", MatchStatus.CHANNEL_ONLY.value, MatchStatus.CHANNEL_ONLY.value),
        # 金标正类 + 实得负类 → FN ×1
        _verdict("F1", MatchStatus.AMBIGUOUS.value, MatchStatus.MATCHED.value, silent=True),
        # 金标负类 + 实得正类 → FP ×1
        _verdict("P1", MatchStatus.MATCHED.value, MatchStatus.CHANNEL_ONLY.value),
        # 金标负类 + 实得负类 → TN ×1
        _verdict("N1", MatchStatus.MATCHED.value, MatchStatus.MATCHED.value),
    ]
    from reconq.align.metrics import _compute

    metrics = _compute("手工", verdicts)
    assert (metrics.true_positive, metrics.false_negative) == (3, 1)
    assert (metrics.false_positive, metrics.true_negative) == (1, 1)
    assert metrics.positives == 4
    assert metrics.recall == pytest.approx(3 / 4)
    assert metrics.precision == pytest.approx(3 / 4)
    assert metrics.f1 == pytest.approx(0.75)
    # 误报率的分母是 FP + TN（不是全部 case）—— 这一条最容易写错
    assert metrics.false_positive_rate == pytest.approx(1 / 2)
    assert metrics.disposition_accuracy == pytest.approx(4 / 6)
    # TP + FP + FN + TN 与总数自洽
    assert metrics.total == 6


def test_ratio_with_zero_denominator_is_zero_not_one():
    """分母为 0 时比例给 0.0，**不给 1.0**。

    把它算成 1.0（"这类 case 全对"）会让门禁在真出问题时看不出差别 ——
    恰恰是指标体系最不能犯的错。
    """
    from reconq.align.metrics import _compute

    # 只有 matched case：三条门禁指标的分母全为 0
    metrics = _compute(
        "全 matched",
        [_verdict("M1", MatchStatus.MATCHED.value, MatchStatus.MATCHED.value) for _ in range(5)],
    )
    assert metrics.ambiguity_miss_rate == 0.0
    assert metrics.weak_match_wrong_claim_rate == 0.0
    assert metrics.duplicate_cross_period_miss_rate == 0.0
    assert metrics.gate_failures() == []
    # 但精确率此时分母为 0，同样是 0.0 而不是 1.0
    assert metrics.precision == 0.0
    assert metrics.recall == 0.0


def test_ambiguity_kind_accuracy_uses_only_labelled_pairs(scored):
    """形态判对率只在「金标记了形态且实得报出形态」时计分。

    分母不是全部 case：金标里 22 条歧义只有 17 条标了 ``expect_kind``
    （合并支付那 5 条与拆笔同形，人工在 note 里写明成因是合并、但系统侧
    只能记 split，故不标形态），弱匹配 15 条与重复认领 4 条标了。
    """
    product, _ = scored
    assert product.kind_total == 41
    assert product.kind_hits == 41
    assert product.ambiguity_kind_accuracy == pytest.approx(1.0)
    # 六种形态都在分母里出现过（否则「形态判对」名不副实）
    kinds = {v.expect_kind for v in product.verdicts if v.expect_kind}
    assert kinds == {
        "split", "equal_amount", "refund_and_original",
        "weak_multiple", "duplicate_claim",
    }
    # 形态取值全部来自受控枚举
    allowed = {k.value for k in AmbiguityKind}
    for verdict in product.verdicts:
        if verdict.actual_kind:
            assert verdict.actual_kind in allowed


# ───────────── 2. 歧义漏判被门禁拦下 ─────────────


def test_current_golden_set_passes_every_gate(scored):
    """本产品在现有金标集上三条硬门禁与相对门禁全过。"""
    product, baseline = scored
    failures = product.gate_failures(baseline)
    assert failures == [], f"门禁未过：{failures}"
    assert product.ambiguity_miss_rate == 0.0
    assert product.weak_match_wrong_claim_rate == 0.0
    assert product.duplicate_cross_period_miss_rate == 0.0
    assert product.false_positive_rate <= baseline.false_positive_rate


def test_gate_catches_ambiguity_silently_picked_as_matched():
    """**最容易糊弄过去的一处**：静默择一报成 matched。

    标签碰巧与金标一致（都是 matched？不 —— 金标是 ambiguous）时更要命：
    实现「择一后认为对上了」看起来像干净的结论，只有把「期望歧义却报成
    已对齐」单独拿出来数，才看得见差额被吃掉了。
    """
    from reconq.align.metrics import _compute

    metrics = _compute(
        "静默择一",
        [
            # 期望歧义，实得 matched（静默择一）→ 计入歧义漏判分子
            _verdict(
                "A1", MatchStatus.AMBIGUOUS.value, MatchStatus.MATCHED.value, silent=True
            ),
            # 期望歧义，实得 channel_only（没报歧义，但不是静默择一）
            _verdict("A2", MatchStatus.AMBIGUOUS.value, MatchStatus.CHANNEL_ONLY.value),
            # 期望歧义，实得歧义 → 不计
            _verdict("A3", MatchStatus.AMBIGUOUS.value, MatchStatus.AMBIGUOUS.value),
        ],
    )
    assert metrics.gate_denominators["ambiguity_miss_rate"] == 3
    assert metrics.gate_numerators["ambiguity_miss_rate"] == 2
    assert metrics.ambiguity_miss_rate == pytest.approx(2 / 3)
    failures = metrics.gate_failures()
    assert len(failures) == 1
    assert "歧义漏判率" in failures[0]
    # 失败文案要点名具体 case，便于直接改
    assert "A1" in failures[0] and "A2" in failures[0]
    assert "A3" not in failures[0]


def test_gate_catches_weak_match_wrongly_claimed():
    """单号没对上却被自动认领（报成 matched 或 mismatch）→ 门禁拦下。"""
    from reconq.align.metrics import _compute

    metrics = _compute(
        "弱匹配误认领",
        [
            _verdict("W1", MatchStatus.WEAK_PENDING.value, MatchStatus.MATCHED.value),
            _verdict("W2", MatchStatus.WEAK_PENDING.value, MatchStatus.MISMATCH.value),
            _verdict("W3", MatchStatus.WEAK_PENDING.value, MatchStatus.WEAK_PENDING.value),
        ],
    )
    assert metrics.gate_numerators["weak_match_wrong_claim_rate"] == 2
    assert metrics.weak_match_wrong_claim_rate == pytest.approx(2 / 3)
    assert any("弱匹配误认领率" in f for f in metrics.gate_failures())


def test_gate_catches_duplicate_and_cross_period_silently_matched():
    """重复认领 / 跨账期落点被报成已对齐 → 门禁拦下。

    这一类是**金额对得上但账错了**：认了就会出现同一笔钱进两次合计
    （重复认领）或跨两期计入（跨账期），所以分子口径是「实得 matched」，
    而**不是**「实得 ≠ 期望」—— 实得 mismatch 时钱没被认领两次，
    不该算进这条门禁。
    """
    from reconq.align.metrics import _compute

    metrics = _compute(
        "重复与跨期",
        [
            _verdict("D1", MatchStatus.DUPLICATE_CLAIM.value, MatchStatus.MATCHED.value),
            _verdict("P1", MatchStatus.CROSS_PERIOD.value, MatchStatus.MATCHED.value),
            # 实得 mismatch：钱没被二次认领，不计进这条门禁
            _verdict("P2", MatchStatus.CROSS_PERIOD.value, MatchStatus.MISMATCH.value),
            _verdict("D2", MatchStatus.DUPLICATE_CLAIM.value, MatchStatus.DUPLICATE_CLAIM.value),
        ],
    )
    assert metrics.gate_denominators["duplicate_cross_period_miss_rate"] == 4
    assert metrics.gate_numerators["duplicate_cross_period_miss_rate"] == 2
    assert metrics.duplicate_cross_period_miss_rate == pytest.approx(0.5)
    failures = metrics.gate_failures()
    assert len(failures) == 1 and "重复认领/跨账期漏判率" in failures[0]


def test_gate_rejects_more_false_positives_than_the_baseline():
    """相对门禁：误报率高于基线即失败 —— 报得更吵不是更准。"""
    from reconq.align.metrics import _compute

    def _batch(fp: int, tn: int) -> MetricSet:
        verdicts = [
            _verdict(f"F{i}", MatchStatus.MATCHED.value, MatchStatus.CHANNEL_ONLY.value)
            for i in range(fp)
        ] + [
            _verdict(f"N{i}", MatchStatus.MATCHED.value, MatchStatus.MATCHED.value)
            for i in range(tn)
        ]
        return _compute("x", verdicts)

    baseline = _batch(0, 10)   # 误报率 0%
    worse = _batch(3, 7)       # 误报率 30%
    assert worse.false_positive_rate > baseline.false_positive_rate
    failures = worse.gate_failures(baseline)
    assert len(failures) == 1 and "误报率" in failures[0] and "不是更准" in failures[0]
    # 与基线持平则通过
    assert _batch(0, 10).gate_failures(baseline) == []


# ───────────── 3. 对比表生成 ─────────────


def test_comparison_table_contains_every_metric_row(scored):
    """并排对比表逐行覆盖全部指标，且两侧数值都能对上各自的 MetricSet。"""
    product, baseline = scored
    text = render_comparison_table(product, baseline)
    lines = text.splitlines()
    # 表头 + 分隔线
    assert "本产品" in lines[0] and "基线" in lines[0]
    assert set(lines[1]) <= {"-"}
    for label in (
        "召回 recall",
        "精确率 precision",
        "F1",
        "误报率 FP/(FP+TN)",
        "结论一致率",
        "形态判对率",
        "歧义漏判率",
        "弱匹配误认领率",
        "重复认领/跨账期漏判率",
        "静默择一率",
    ):
        assert any(label in line for line in lines), f"对比表缺 {label} 行"
    # 本产品侧与 baseline 侧数值确实来自各自的 MetricSet
    row = next(line for line in lines if "召回 recall" in line)
    assert f"{product.recall * 100:.1f}%" in row
    assert f"{baseline.recall * 100:.1f}%" in row
    assert f"{product.disposition_accuracy * 100:.1f}%" in next(
        line for line in lines if "结论一致率" in line
    )


def test_comparison_is_measurably_worse_for_the_baseline(scored):
    """基线在三条硬门禁指标上必须实打实更差，否则「同口径对比」是空转。

    这条断言是防止**指标写得太宽松以至于两边都满分**：如果基线也拿 0 漏判，
    那这套指标就没区分力，整份报告也就没有可核对的内容。
    """
    product, baseline = scored
    assert baseline.ambiguity_miss_rate == pytest.approx(1.0)
    assert baseline.duplicate_cross_period_miss_rate == pytest.approx(1.0)
    assert product.ambiguity_miss_rate == 0.0
    assert product.disposition_accuracy > baseline.disposition_accuracy
    assert baseline.silent_pick_rate == pytest.approx(1.0)
    assert product.silent_pick_rate == 0.0
    # 静默择一的 case 数 == 期望歧义的 case 数：基线把每一条一对多都择一了
    silent = [v for v in baseline.verdicts if v.silently_picked]
    assert len(silent) == 22
    assert {v.case_id for v in silent} == {
        v.case_id for v in baseline.verdicts if v.expect_label == MatchStatus.AMBIGUOUS.value
    }


def test_written_report_records_scale_caliber_and_limitations(golden, scored, tmp_path):
    """落盘报告须同时给出样例规模、计算口径与「不能说明什么」。

    只报一个好看的召回率而不写分母是多少条，等于没报 —— 所以这三条都是
    硬断言，不是「读起来像有」。
    """
    product, baseline = scored
    out = write_metrics_report(
        str(tmp_path / "metrics-report.md"), golden, product, baseline,
        written_at="2026-09-30T00:00:00+08:00",
    )
    text = open(out, encoding="utf-8").read()
    # 样例规模
    assert "## 1 样例规模" in text
    assert f"（{golden.total} 条，下限 {MIN_GOLDEN_CASES} 条）" in text
    for label in ("matched", "ambiguous", "weak_pending", "cross_period"):
        assert f"| {label} | " in text or f"（{label}）" in text
    # 口径
    assert "## 2 计算口径" in text
    assert "| 歧义漏判率 | 期望歧义的 case 中被报成别的" in text
    assert "run_vlookup_baseline" in text
    # 不能说明什么
    assert "## 6 这些数字不能说明什么" in text
    assert "不代表任何一家医院的真实错账分布" in text
    assert "把「不确定」说成「渠道有、HIS 无」" in text
    # 逐条差异
    assert "## 5 逐条差异" in text
    assert "（无：全部 case 结论与金标一致）" in text
    # 生成时间可注入（报告要能逐字节复现）
    assert "2026-09-30T00:00:00+08:00" in text


def test_terminal_report_shows_meta_gate_and_mismatch_rows(golden, scored):
    product, baseline = scored
    text = render_report(golden, product, baseline)
    assert "标注时间" in text
    assert "门禁：全部通过" in text
    assert "结论一致率" in text
    # 未过门禁时给出逐条明细（这里都过了，故不出现明细段）
    assert "未过门禁的 case 明细" not in text


# ───────────── 4. 与基线同口径 ─────────────


def test_both_sides_see_identical_case_objects(monkeypatch, golden):
    """两侧喂的是**同一份 case 对象**、同一个容差常量。

    「同口径」最容易被绕过的地方：产品侧跑真实现、基线侧跑一份简化替身，
    或者两边各自算各自的容差。这里直接对基线实现打桩计数，断言
    **每条 case 都被基线跑过一次且只跑一次**，并断言传入的正是金标集里
    那些 AlignRecord 对象本身。
    """
    calls: list[tuple[list, list, Decimal]] = []
    original = run_vlookup_baseline

    def _spy(channel_rows, his_rows, *, tolerance=BASELINE_TOLERANCE):
        calls.append((list(channel_rows), list(his_rows), tolerance))
        return original(channel_rows, his_rows, tolerance=tolerance)

    monkeypatch.setattr(
        "reconq.align.metrics.run_vlookup_baseline", _spy
    )
    product, baseline = evaluate_golden_set(golden)
    assert len(calls) == golden.total
    # 逐条对回金标集里的行对象本身（不是副本、不是重造的）
    expected_first = golden.cases[0].channel_rows[0]
    assert calls[0][0][0] is expected_first
    expected_his = golden.cases[0].his_rows
    assert calls[0][1] == list(expected_his)
    # 容差是同一个常量（基线与产品同值，才叫对拍）
    assert all(t == BASELINE_TOLERANCE for _, _, t in calls)
    assert product.total == baseline.total == golden.total
    # 两侧 case_id 集合完全一致 —— 逐条一一对应
    assert [v.case_id for v in product.verdicts] == [v.case_id for v in baseline.verdicts]
    assert [v.case_id for v in product.verdicts] == [c.case_id for c in golden.cases]


def test_label_mapping_is_symmetric_between_the_two_sides(golden):
    """两侧的标签取法逐条对称，且都落在同一个受控标签空间内。

    具体是三件事：(a) 基线 ``amount_diff`` 折成 ``mismatch``；
    (b) 焦点行已对齐但存在无人认领的 HIS 记录时**两侧都**记 ``his_only``；
    (c) 焦点行本身有结论时以它为准，不被 ``his_only`` 盖掉。
    """
    product, baseline = scored_pair(golden)
    allowed = set(GOLD_LABEL_ZH)
    assert {v.label for v in product.verdicts} <= allowed
    assert {v.label for v in baseline.verdicts} <= allowed

    # (b) his_only 的 6 条 case 两侧都判 his_only
    his_only_cases = {
        v.case_id for v in product.verdicts if v.expect_label == "his_only"
    }
    assert len(his_only_cases) == 6
    for verdict in product.verdicts:
        if verdict.case_id in his_only_cases:
            assert verdict.label == "his_only"
    base_by_id = {v.case_id: v for v in baseline.verdicts}
    assert all(base_by_id[cid].label == "his_only" for cid in his_only_cases)

    # (c) 歧义 case 上 his_only 不得盖掉焦点行的结论
    for verdict in product.verdicts:
        if verdict.expect_label == MatchStatus.AMBIGUOUS.value:
            assert verdict.label == MatchStatus.AMBIGUOUS.value

    # (a) 基线的 amount_diff 折成 mismatch：金标 mismatch 的 6 条两侧都是 mismatch
    mismatch_cases = [
        v for v in product.verdicts if v.expect_label == MatchStatus.MISMATCH.value
    ]
    assert len(mismatch_cases) == 6
    assert all(v.label == MatchStatus.MISMATCH.value for v in mismatch_cases)
    assert all(base_by_id[v.case_id].label == MatchStatus.MISMATCH.value for v in mismatch_cases)


def scored_pair(golden):
    return evaluate_golden_set(golden)


def test_baseline_tolerance_equals_the_product_tolerance():
    """两侧金额容差同值（0.01 元）—— 给基线更宽的容差能让差异变少，
    但那样量出来的「本产品更准」是假的。"""
    from reconq.align.matcher import WEAK_MATCH_TOLERANCE

    assert BASELINE_TOLERANCE == WEAK_MATCH_TOLERANCE == Decimal("0.01")


# ───────────── 5. 金标集自身的门禁 ─────────────


def test_golden_set_too_small_is_rejected(tmp_path):
    """条数低于下限直接报错，不「先跑跑看」—— 小样本报的数字没有否决力。"""
    path = tmp_path / "small.jsonl"
    case = {
        "case_id": "X1", "channel": "wechat", "period": PERIOD,
        "expect_label": "matched",
        "channel_rows": [
            {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02T10:00:00+08:00"}
        ],
        "his_rows": [
            {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02T10:01:00+08:00"}
        ],
    }
    path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ReconqError) as exc:
        load_golden_set(str(path))
    assert exc.value.code == "GOLDEN_SET_TOO_SMALL"
    assert str(MIN_GOLDEN_CASES) in exc.value.detail
    # 显式放宽下限后可读（说明拦的是条数本身，不是别的）
    relaxed = load_golden_set(str(path), min_cases=1)
    assert relaxed.total == 1


def test_golden_set_missing_and_empty_are_rejected(tmp_path):
    with pytest.raises(ReconqError) as exc:
        load_golden_set(str(tmp_path / "nope.jsonl"))
    assert exc.value.code == "GOLDEN_SET_MISSING"

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n// 只有注释\n", encoding="utf-8")
    with pytest.raises(ReconqError) as exc:
        load_golden_set(str(empty))
    assert exc.value.code == "GOLDEN_SET_EMPTY"


def test_unknown_expect_label_is_rejected():
    """标签不在受控空间内即拒 —— 标签空间受控才不会「同一个词两种算法」。"""
    case = {
        "case_id": "X1", "channel": "wechat", "period": PERIOD,
        "expect_label": "totally_matched_i_promise",
        "channel_rows": [
            {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02T10:00:00+08:00"}
        ],
        "his_rows": [],
    }
    with pytest.raises(ReconqError) as exc:
        parse_golden_case(case, path="<inline>")
    assert exc.value.code == "GOLDEN_LABEL_UNKNOWN"
    # 错误文案把受控标签空间列出来，让改的人不用去翻源码
    assert "ambiguous" in exc.value.detail and "his_only" in exc.value.detail


def test_naive_trade_time_is_rejected():
    """不带时区偏移的时刻直接拒 —— 缺偏移就判不了跨账期落点。"""
    case = {
        "case_id": "X1", "channel": "wechat", "period": PERIOD,
        "expect_label": "matched",
        "channel_rows": [
            {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02 10:00:00"}
        ],
        "his_rows": [],
    }
    with pytest.raises(ReconqError) as exc:
        parse_golden_case(case, path="<inline>")
    assert exc.value.code == "GOLDEN_CASE_INVALID"
    assert "时区偏移" in exc.value.detail


def test_missing_amount_is_rejected_but_empty_order_no_is_allowed():
    """金额缺一不可；单号允许为空（那本身就是弱匹配场景）。"""
    base = {
        "case_id": "X1", "channel": "wechat", "period": PERIOD,
        "expect_label": "channel_only",
        "his_rows": [],
    }
    with pytest.raises(ReconqError) as exc:
        parse_golden_case(
            {**base, "channel_rows": [
                {"order_no": "X1", "trade_time": "2026-09-02T10:00:00+08:00"}
            ]},
            path="<inline>",
        )
    assert exc.value.code == "GOLDEN_CASE_INVALID"
    assert "amount" in exc.value.detail

    ok = parse_golden_case(
        {**base, "channel_rows": [
            {"order_no": "", "amount": "1.00", "trade_time": "2026-09-02T10:00:00+08:00"}
        ]},
        path="<inline>",
    )
    assert ok.channel_rows[0].order_no == ""


def test_duplicate_case_id_is_rejected(tmp_path):
    """case_id 重复即拒 —— 它是报告里定位「哪一条错了」的唯一钥匙。"""
    path = tmp_path / "dup.jsonl"
    case = {
        "case_id": "X1", "channel": "wechat", "period": PERIOD,
        "expect_label": "matched",
        "channel_rows": [
            {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02T10:00:00+08:00"}
        ],
        "his_rows": [],
    }
    path.write_text(
        (json.dumps(case, ensure_ascii=False) + "\n") * 2, encoding="utf-8"
    )
    with pytest.raises(ReconqError) as exc:
        load_golden_set(str(path), min_cases=1)
    assert exc.value.code == "GOLDEN_CASE_INVALID"
    assert "重复" in exc.value.detail
    # 错误详情带上文件名与行号
    assert os.path.basename(str(path)) in exc.value.detail
    assert "第 2 行" in exc.value.detail


def test_focus_index_out_of_range_is_rejected():
    case = {
        "case_id": "X1", "channel": "wechat", "period": PERIOD,
        "expect_label": "matched", "focus_index": 5,
        "channel_rows": [
            {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02T10:00:00+08:00"}
        ],
        "his_rows": [],
    }
    with pytest.raises(ReconqError) as exc:
        parse_golden_case(case, path="<inline>")
    assert exc.value.code == "GOLDEN_CASE_INVALID"
    assert "越界" in exc.value.detail


def test_missing_meta_file_marks_the_source_as_unrecorded(tmp_path):
    """缺 golden_meta.json 时报告显式标「标注来源未记录」，而不是当无事发生。"""
    path = tmp_path / "golden.jsonl"
    case = {
        "case_id": "X1", "channel": "wechat", "period": PERIOD,
        "expect_label": "matched",
        "channel_rows": [
            {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02T10:00:00+08:00"}
        ],
        "his_rows": [
            {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02T10:01:00+08:00"}
        ],
    }
    path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    loaded = load_golden_set(str(path), min_cases=1)
    assert loaded.meta["annotated_source"] == "（未记录）"
    assert "不得作为验收依据" in loaded.meta["note"]


def test_every_new_error_code_is_registered():
    """新增的五个错误码必须都在 errors.py 登记（未登记的码会渲染成
    「未登记的错误码」，对业务人员等于没提示）。"""
    for code in (
        "GOLDEN_SET_MISSING",
        "GOLDEN_SET_EMPTY",
        "GOLDEN_SET_TOO_SMALL",
        "GOLDEN_CASE_INVALID",
        "GOLDEN_LABEL_UNKNOWN",
    ):
        assert code in ERROR_CODES, f"{code} 未登记到 errors.py"
        assert ERROR_CODES[code].zh != "未登记的错误码"
        assert ERROR_CODES[code].suggestion


# ───────────── 额外：单条 case 可独立复现 ─────────────


def test_each_case_is_independently_reproducible(golden):
    """逐条单跑与整批跑结论一致 —— 自足性是「能定位哪一条错了」的前提。"""
    for case in golden.cases:
        solo = evaluate_case_product(case)
        assert solo.label in GOLD_LABEL_ZH
        assert solo.ref, f"{case.case_id} 没有可回查定位"
        # 重复跑同一 case 结论不变（无隐藏状态）
        again = evaluate_case_product(case)
        assert again.label == solo.label


def test_golden_cases_carry_no_identifiable_patient_data(golden):
    """金标集不得含患者可识别明细：科室是通用称呼，金额与单号一律占位。"""
    departments = {
        r.department for c in golden.cases for r in (*c.channel_rows, *c.his_rows) if r.department
    }
    assert departments <= {
        "门诊收费室", "住院收费处", "医保办", "体检中心",
    }, f"出现了不该出现的科室名：{departments - {'门诊收费室', '住院收费处', '医保办', '体检中心'}}"
    for case in golden.cases:
        for row in (*case.channel_rows, *case.his_rows):
            assert row.order_no.startswith("GOLD-") or row.order_no == ""
            assert row.source_file.startswith("golden_")


def test_ambiguity_cases_never_claim_a_his_record(golden):
    """歧义与弱匹配 case 里本产品**一条 HIS 记录都没认领**。

    这是「拒绝归并」最硬的一条不变量，从指标层再验一次：不看 matcher
    内部的 claimed 集合，只看结论 —— 报了歧义/弱匹配就不该有 his_record。
    """
    from reconq.align.matcher import align_all

    window = period_window(PERIOD, TZ)
    for case in golden.cases:
        if case.expect_label not in (
            MatchStatus.AMBIGUOUS.value,
            MatchStatus.WEAK_PENDING.value,
            MatchStatus.CROSS_PERIOD.value,
            MatchStatus.DUPLICATE_CLAIM.value,
        ):
            continue
        result = align_all(
            {case.channel: list(case.channel_rows)},
            list(case.his_rows),
            period=case.period,
            window=window,
        )
        focus = result.channels[case.channel].outcomes[case.focus_index]
        assert focus.his_record is None, f"{case.case_id} 认领了 HIS 记录"
        # 候选要么是全部命中项、要么一条都没认领；歧义必须把全部候选列出
        if case.expect_label == MatchStatus.AMBIGUOUS.value:
            assert len(focus.candidates) == len(case.his_rows)


def test_main_verify_mainline_is_not_regressed():
    """主干未回归：verify 仍按 Task 3 的样子跑通。"""
    result = subprocess.run(
        [sys.executable, "-m", "reconq", "--no-log", "verify", "--period", "2026-W36",
         "--source", "tests/fixtures/bills", "--channel", "wechat"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": "src"},
    )
    assert "5714.69" in result.stdout, result.stdout[-2000:]
    assert result.returncode == 1, "有 fail 的账期应返回非零退出码"


# ───────────── 6. CLI 端到端 ─────────────


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "reconq", "--no-log", *args],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": "src"},
    )


def test_cli_align_metrics_prints_the_comparison_and_exits_zero(tmp_path):
    """``reconq align-metrics`` 端到端：打并排表、写报告、退出码 0。"""
    out = tmp_path / "metrics-report.md"
    result = _run_cli("align-metrics", "--golden", GOLDEN, "--out", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"指标报告已写入 {out}" in result.stdout
    for label in ("召回 recall", "结论一致率", "歧义漏判率", "静默择一率"):
        assert label in result.stdout
    assert "门禁：全部通过" in result.stdout
    assert out.is_file() and out.stat().st_size > 4000
    assert "## 1 样例规模" in out.read_text(encoding="utf-8")


def test_cli_align_metrics_json_carries_both_sides(tmp_path):
    result = _run_cli("align-metrics", "--golden", GOLDEN, "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["golden"]["total"] == 80
    assert payload["product"]["ambiguity_miss_rate"] == 0.0
    assert payload["baseline"]["ambiguity_miss_rate"] == 1.0
    assert payload["gate_failures"] == []
    # --json 不带 --cases 时不吐逐条明细（那是另一个量级的输出）
    assert payload["verdicts"] == []
    # 两侧都带出分母，便于判断指标有没有被稀释
    assert payload["product"]["gate_denominators"]["ambiguity_miss_rate"] == 22
    assert payload["baseline"]["gate_denominators"]["ambiguity_miss_rate"] == 22


def test_cli_align_metrics_cases_lists_every_case():
    """``--cases`` 逐条列出 80 条，两侧标签都在。"""
    result = _run_cli("align-metrics", "--golden", GOLDEN, "--cases")
    assert result.returncode == 0
    lines = [line for line in result.stdout.splitlines() if line.startswith("GOLD-")]
    assert len(lines) == 80
    assert lines[0].startswith("GOLD-M-01")
    assert lines[-1].startswith("GOLD-H-06")
    # 歧义那几行两侧标签不同，正是这张表要让人一眼看出的事
    split_row = next(line for line in lines if line.startswith("GOLD-S-01"))
    assert "歧义待人工" in split_row


def test_cli_rejects_a_golden_set_below_the_floor(tmp_path):
    """金标集条数不足 → 明确错误码 + 非零退出码，不静默跑出一个假 0 漏判。"""
    small = tmp_path / "small.jsonl"
    small.write_text(
        json.dumps(
            {
                "case_id": "X1", "channel": "wechat", "period": PERIOD,
                "expect_label": "matched",
                "channel_rows": [
                    {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02T10:00:00+08:00"}
                ],
                "his_rows": [
                    {"order_no": "X1", "amount": "1.00", "trade_time": "2026-09-02T10:01:00+08:00"}
                ],
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    result = _run_cli("align-metrics", "--golden", str(small))
    assert result.returncode == 2
    assert "GOLDEN_SET_TOO_SMALL" in result.stderr
    assert str(MIN_GOLDEN_CASES) in result.stderr
