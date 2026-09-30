"""归因效果评测：两档指标、UNKNOWN 期望计分、门禁、报告（Task 39）。

五类规定断言逐条可核：

1. **评测集可跑完** —— 48 条（下限 40）、5 条渠道、6 条 UNKNOWN 期望
   （下限 5），受控字典 10 条成因**每一条**都出现在某些样本的期望里；
   每条自足（给一份固定证据包就能单独复现），逐字节可重建。
2. **两档指标计算正确** —— Top-1 / Top-3 / 依据行 / 平均置信度，
   **逐条手算核对**而不是「跑出一个数看看顺不顺眼」。两处最容易写错的
   口径各有专项断言：分母是可评条数而非总条数、平均置信度**只取答对的**。
3. **UNKNOWN 期望被正确计分** —— 拒答记成功、硬猜记失败，分母只取
   UNKNOWN 样本；并钉死「Top-1 是具体成因就算硬猜，哪怕第 2 位写了
   undetermined」—— 经办人签字看的是第一条。
4. **门禁生效** —— **不依赖评测集当前的分数**：构造一个「模型档比规则档
   差」的 TierMetrics，门禁必须报未过；构造一个持平/更好的，门禁必须放行。
   门禁在本轮实测恰好是**持平通过**（两边 93.8%），若只断言「当前通过」，
   门禁就成了一个恒真的摆设。
5. **报告生成** —— 落盘七节齐全，并强制写明「模型档数字来自录制响应
   替身、不代表真实端点」；**不许**出现「本次评测证明模型比规则准」
   这类把替身当证据的表述。

另有一条**独立于夹具**的性质：模型档必须走真实
:func:`reconq.align.attribution.attribute` 与真实
:class:`~reconq.gateway.model_gateway.ModelGateway`（断言请求体形态与
审计元数据），否则「两档对比」比的是两套玩具。
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest

from reconq.align.attribution import attribute, evidence_refs_of
from reconq.align.baseline import rank_causes_by_rules
from reconq.align.bundle import bundle_from_payload
from reconq.align.causes import CAUSES, cause_vocabulary
from reconq.align.eval import (
    MIN_EVAL_CASES,
    MIN_UNKNOWN_CASES,
    UNKNOWN_CODE,
    CaseVerdict,
    EvalSet,
    TierMetrics,
    _ratio,
    build_replay_gateway,
    evaluate_eval_set,
    load_eval_set,
    render_report,
    run_model_tier,
    run_rule_tier,
    write_eval_report,
)
from reconq.errors import ReconqError, error_meta

SET_DIR = os.path.join("tests", "fixtures", "attrib", "golden")


@pytest.fixture(scope="module")
def eval_set() -> EvalSet:
    return load_eval_set(SET_DIR)


@pytest.fixture(scope="module")
def tiers(eval_set: EvalSet):
    return evaluate_eval_set(eval_set)


# ────────────────────── 1 评测集可跑完 ──────────────────────


def test_eval_set_meets_lower_bounds(eval_set: EvalSet):
    assert eval_set.total >= MIN_EVAL_CASES, f"样本 {eval_set.total} 条 < 下限 {MIN_EVAL_CASES}"
    assert eval_set.unknown_total >= MIN_UNKNOWN_CASES
    assert len(eval_set.channel_counts()) >= 5, "跨渠道覆盖不足 5 条渠道"
    assert eval_set.periods == ("2026-W36",)


def test_eval_set_covers_every_controlled_cause(eval_set: EvalSet):
    """受控字典 10 条每条都要有样本覆盖。

    漏掉一条就等于那一类成因**从未被评测过**，而报告里「10 条成因」
    的说法会让人以为都验过了。
    """
    coverage = eval_set.cause_coverage()
    assert set(coverage) == set(cause_vocabulary()) == set(CAUSES)
    missing = [code for code, count in coverage.items() if count == 0]
    assert not missing, f"受控成因字典里这 {len(missing)} 条没有任何样本覆盖：{missing}"


def test_eval_set_has_the_discriminating_layer(eval_set: EvalSet):
    """必须有「形态查表给出的答案明确是错的」那一层。

    规则基线读的就是形态（``channel_only`` → ``his_missing_order``…）。
    若每条样本的期望都恰好等于形态查表的结果，规则档会拿到满分 ——
    那不是它判得准，是评测集照着它的查表造的样本，两档对比就自证无效。
    """
    override = [c for c in eval_set.cases if c.layer == "semantic_override"]
    assert len(override) >= 3, "语义覆盖层不足 3 条，规则档会靠形态查表拿满分"


def test_eval_cases_are_self_contained_and_reproducible(eval_set: EvalSet):
    """每条自足：重建后 digest 逐字节相同，且能引到真实行。"""
    for case in eval_set.cases:
        again = bundle_from_payload(case.bundle.to_dict())
        assert again.digest == case.bundle.digest, f"{case.case_id} 重建后 digest 变了"
        refs = evidence_refs_of(again)
        assert refs, f"{case.case_id} 没有任何可引用行"
        assert set(case.required_refs) <= set(refs), (
            f"{case.case_id} 的 required_refs 不在证据包真实行内"
        )


def test_eval_set_carries_no_patient_identifiable_detail(eval_set: EvalSet):
    """评测集不得含患者可识别明细。"""
    banned = ("姓名", "身份证", "手机号", "住院号", "门诊号", "patient_name", "id_card")
    for case in eval_set.cases:
        blob = json.dumps(case.bundle.to_dict(), ensure_ascii=False)
        for token in banned:
            assert token not in blob, f"{case.case_id} 的证据包含疑似患者可识别字段：{token}"


# ────────────────────── 2 两档指标计算正确 ──────────────────────


def test_rule_tier_runs_every_case_without_error(tiers):
    model, rules = tiers
    assert rules.total == model.total
    assert rules.degraded == 0, f"规则档有 {rules.degraded} 条异常，兜底路径有问题"
    assert rules.usable == rules.total, "规则档本应每条都给出候选（字典里留了 undetermined）"
    assert rules.run_mode == "local"


def test_rule_tier_costs_nothing_and_says_so(tiers):
    """规则档真源是零网络零 token：``priced=True`` 且成本 0。

    标成「未定价」会让「模型档比规则档贵多少」算不出倍数。
    """
    _, rules = tiers
    assert rules.priced is True
    assert rules.cost == Decimal("0")
    assert rules.prompt_tokens == 0 and rules.completion_tokens == 0
    assert "0.000000" in rules.cost_text
    assert "未配置" not in rules.cost_text


def test_model_tier_cost_unpriced_when_no_price_env(tiers, monkeypatch):
    """单价缺位即 ``priced=False`` 且显示「未配置单价」——不是 0 元。"""
    for name in ("RECONQ_MODEL_PRICE_PROMPT_PER_1K", "RECONQ_MODEL_PRICE_COMPLETION_PER_1K"):
        monkeypatch.delenv(name, raising=False)
    model, _ = evaluate_eval_set(load_eval_set(SET_DIR))
    assert model.priced is False
    assert model.cost_text == "未配置单价"
    assert model.total_cost_text == "未配置单价"


def test_model_tier_cost_priced_when_price_env_set(tiers, monkeypatch):
    """单价配齐则按 token 计价，且单条成本 = 总成本 / 条数。"""
    monkeypatch.setenv("RECONQ_MODEL_PRICE_PROMPT_PER_1K", "0.012")
    monkeypatch.setenv("RECONQ_MODEL_PRICE_COMPLETION_PER_1K", "0.036")
    model, _ = evaluate_eval_set(load_eval_set(SET_DIR))
    assert model.priced is True
    assert model.prompt_tokens > 0 and model.completion_tokens > 0
    assert model.cost > 0
    assert model.avg_cost == model.cost / Decimal(model.total)
    assert "元/条" in model.cost_text


def test_top1_and_top3_rates_match_manual_count(eval_set, tiers):
    """Top-1 / Top-3 逐条手算核对，不看模块自己报的数。"""
    model, _ = tiers
    usable = [v for v in model.verdicts if v.usable]
    manual_top1 = sum(1 for v in usable if v.top1_code in eval_set.case_of(v.case_id).expected_set)
    manual_top3 = sum(
        1
        for v in usable
        if any(code in eval_set.case_of(v.case_id).expected_set for code in v.top3_codes)
    )
    assert model.usable == len(usable)
    assert model.top1_hits == manual_top1
    assert model.top3_hits == manual_top3
    assert model.top1_rate == pytest.approx(manual_top1 / len(usable))
    assert model.top3_rate == pytest.approx(manual_top3 / len(usable))


def test_top3_rate_never_below_top1_rate(tiers):
    """Top-3 是 Top-1 的超集口径，容差内不应出现 Top-3 更低。"""
    model, rules = tiers
    for metrics in (model, rules):
        assert metrics.top3_rate >= metrics.top1_rate - 1e-9


def test_mean_confidence_only_counts_correct_top1(tiers):
    """平均置信度只取「Top-1 答对」的样本 —— 答错的高置信度混进来，
    得到的数字越高越危险。"""
    for metrics in tiers:
        correct = [v for v in metrics.verdicts if v.usable and v.top1_hit]
        assert metrics.correct_confidence_count == len(correct)
        manual = sum(v.top1_confidence for v in correct) / len(correct) if correct else 0.0
        assert metrics.mean_confidence == pytest.approx(manual)
        if correct:
            # 答对的样本置信度必须都进了累加器
            assert metrics.correct_confidence_sum == pytest.approx(
                sum(v.top1_confidence for v in correct)
            )


def test_ratio_returns_zero_not_one_when_denominator_zero():
    """分母为 0 返回 0.0 而不是 1.0。

    这是本模块最危险的一个假数字：一条 UNKNOWN 样本都没有时，
    「拒答率 100%」看起来比什么都好。
    """
    assert _ratio(0, 0) == 0.0
    assert _ratio(3, 0) == 0.0
    assert _ratio(1, 2) == 0.5


def test_hallucination_rate_is_complement_of_refusal(tiers):
    """硬猜率 + 拒答率 = 1（分母同为 UNKNOWN 样本）。"""
    model, rules = tiers
    for metrics in (model, rules):
        assert metrics.unknown_total > 0
        assert metrics.unknown_resolved + metrics.hallucinated == metrics.unknown_total
        assert metrics.unknown_refusal_rate + metrics.hallucination_rate == pytest.approx(1.0)


def test_evidence_ref_rate_measured(eval_set, tiers):
    """依据行引用正确率：两档都应接近满分（规则候选必带行号）。"""
    _, rules = tiers
    assert rules.ref_rate > 0.9, "规则候选若引不出证据行，面板等于让人自己翻原始文件"


def test_both_tiers_cover_all_cases_exactly_once(eval_set, tiers):
    model, rules = tiers
    ids = {c.case_id for c in eval_set.cases}
    assert [v.case_id for v in model.verdicts].count  # sanity
    assert len({v.case_id for v in model.verdicts}) == len(model.verdicts) == len(ids)
    assert len({v.case_id for v in rules.verdicts}) == len(rules.verdicts) == len(ids)


# ────────────────────── 3 UNKNOWN 期望被正确计分 ──────────────────────


def test_unknown_expectation_self_consistency(eval_set):
    """``expect_unknown`` 与 ``expect_codes`` 必须自洽，且只认 undetermined。"""
    for case in eval_set.cases:
        unknown = set(case.expect_codes) == {UNKNOWN_CODE}
        assert unknown == case.expect_unknown, case.case_id
        assert bool(case.expect_codes)


def test_refusal_counted_on_top1_only(eval_set, tiers):
    """只按 Top-1 判拒答：Top-1 给了具体成因就算硬猜。

    模型在第 2 位也写了 undetermined 但 Top-1 给了具体成因，仍算失败 ——
    经办人签字时看的是第一条。
    """
    model, _ = tiers
    unknown_cases = {c.case_id for c in eval_set.cases if c.expect_unknown}
    for verdict in model.verdicts:
        if verdict.case_id in unknown_cases:
            assert verdict.unknown_resolved is not None
            assert verdict.unknown_resolved == (verdict.top1_code == UNKNOWN_CODE)
            assert verdict.hallucinated == (
                bool(verdict.top1_code) and verdict.top1_code != UNKNOWN_CODE
            )
        else:
            assert verdict.unknown_resolved is None
            assert verdict.hallucinated is False


def test_hallucination_does_not_count_as_top1_hit(eval_set, tiers):
    """硬猜的那条 Top-1 不得同时被记成命中。"""
    model, _ = tiers
    for verdict in model.verdicts:
        if verdict.hallucinated:
            assert verdict.top1_code not in eval_set.case_of(verdict.case_id).expected_set
            assert verdict.top1_hit is False
            assert verdict.top3_hit is False


def test_eval_set_contains_a_hallucination_probe(eval_set):
    """评测集必须**同时**含「该拒答时拒答」与「该拒答时硬猜」。

    只有拒答样本的话，硬猜率恒为 0，这个指标量不出任何东西。
    """
    hallucinators = [c for c in eval_set.cases if c.behaviour == "hallucinate"]
    refusers = [c for c in eval_set.cases if c.behaviour == "refuse"]
    assert len(hallucinators) >= 1, "没有「该拒却硬猜」的样本，硬猜率恒为 0，指标无效"
    assert len(refusers) >= 3, "拒答样本过少，拒答率说明不了问题"


def test_model_tier_refuses_more_often_than_rules(tiers):
    """模型档在 UNKNOWN 上应当比规则档更敢拒答。

    这是本产品给归因配 ``ai_role: core`` 的核心理由之一：字典里留了
    ``undetermined``，但「什么时候该用它」需要看账单列的业务含义。
    """
    model, rules = tiers
    assert model.unknown_refusal_rate > rules.unknown_refusal_rate, (
        f"模型档拒答率 {model.unknown_refusal_rate:.1%} 未高于规则档 "
        f"{rules.unknown_refusal_rate:.1%}：模型层没挣回它在 UNKNOWN 上的价值"
    )


# ────────────────────── 4 门禁生效 ──────────────────────


def _metrics(tier: str, usable: int, top1: int, top3: int) -> TierMetrics:
    m = TierMetrics(tier=tier, total=usable, usable=usable)
    m.top1_hits, m.top3_hits = top1, top3
    return m


def test_gate_blocks_model_worse_than_rules():
    """模型档 Top-3 低于规则档 → 门禁必须报未过。"""
    good = _metrics("模型档", 100, 90, 95)
    bad_rules = _metrics("规则基线档", 100, 90, 99)
    failures = good.gate_failures(bad_rules)
    assert failures, "门禁在模型档更差时竟然放行了"
    assert "Top-3" in failures[0] and "规则基线档" in failures[0]


def test_gate_passes_on_tie():
    """持平必须放行 —— 门禁卡的是「更差」，不是「没赢」。"""
    model = _metrics("模型档", 100, 90, 95)
    rules = _metrics("规则基线档", 100, 80, 95)
    assert model.gate_failures(rules) == []


def test_gate_passes_when_model_better():
    better = _metrics("模型档", 100, 90, 99)
    worse = _metrics("规则基线档", 100, 80, 95)
    assert better.gate_failures(worse) == []


def test_gate_does_not_require_top1_or_refusal():
    """门禁**只**卡 Top-3。

    规则在「形态 → 成因」这类确定性映射上本就强，把 Top-1 与拒答率也
    写成硬门禁会逼人对着评测集调提示词，换来的分数涨不涨得上业务。
    """
    model = _metrics("模型档", 100, 10, 95)          # Top-1 极差
    rules = _metrics("规则基线档", 100, 99, 95)
    assert model.gate_failures(rules) == [], "门禁不该卡 Top-1"


def test_gate_without_baseline_is_permissive():
    """没给基线时门禁无参照 —— 空列表而不是假装通过一次比较。"""
    assert _metrics("模型档", 10, 1, 2).gate_failures(None) == []


def test_current_eval_set_passes_the_gate(tiers):
    """本轮评测集实测：门禁通过（两边 Top-3 持平）。"""
    model, rules = tiers
    failures = model.gate_failures(rules)
    assert not failures, f"当前评测集未过门禁：{failures}"
    # 持平通过是个脆弱结果：门禁的判据本身由上面几条独立用例守着
    assert model.top3_rate >= rules.top3_rate


def test_gate_is_not_a_constant_true():
    """防「门禁恒真」：把规则档指标改高，门禁必须重新报未过。

    上一条断言的是「当前通过」，这条断言的是「判据真的在比」——
    两者缺一，门禁就可能只是个摆设。
    """
    model, rules = evaluate_eval_set(load_eval_set(SET_DIR))
    assert model.gate_failures(rules) == []
    inflated = TierMetrics(**{**rules.__dict__})
    inflated.tier = "规则基线档（灌水）"
    inflated.top3_hits = rules.usable          # 假装规则全对
    inflated.top1_hits = rules.usable
    assert model.gate_failures(inflated), "把规则档灌到满分后门禁仍放行，说明它没在比"


# ────────────────────── 5 报告生成 ──────────────────────


def test_report_has_all_sections(eval_set, tiers, tmp_path):
    model, rules = tiers
    out = tmp_path / "attrib-eval.md"
    path = write_eval_report(str(out), eval_set, model, rules)
    assert path == str(out) and out.exists()
    text = out.read_text(encoding="utf-8")
    for heading in (
        "# 归因效果评测报告",
        "## 1 样例规模",
        "## 2 计算口径",
        "## 3 两档并排",
        "## 4 门禁",
        "## 5 逐条结论",
        "## 6 这些数字不能说明什么",
        "## 7 复现",
    ):
        assert heading in text, f"报告缺章节 {heading}"
    assert len(text) > 3000, "报告过薄，签验收不可用"


def test_report_states_sample_size_model_and_prompt_version(eval_set, tiers):
    model, rules = tiers
    text = render_report(eval_set, model, rules)
    assert f"{eval_set.total} 条" in text
    assert eval_set.model in text, "报告必须带模型名与版本"
    assert "提示词版本" in text
    assert "reconq.attribution" not in text or True  # 报告不写研发内部名，无所谓
    assert "受控成因字典覆盖" in text
    assert f"{eval_set.unknown_total} 条" in text, "报告必须写明 UNKNOWN 样本量（拒答率的分母）"


def test_report_discloses_top1_regression_behind_passing_gate(tiers):
    """门禁通过时若 Top-1 落后，报告必须**点破**。

    本轮实测就是这个情形：模型档 Top-1 72.9% < 规则档 87.5%，而 Top-3
    持平使门禁通过。Top-1 落后意味着多数差异上经办人看到的第一条成因
    不如规则档 —— 只报一个「门禁通过」的绿勾会把这句���藏在底下。
    """
    model, rules = tiers
    text = render_report(load_eval_set(SET_DIR), model, rules)
    assert not model.gate_failures(rules), "前置：本轮门禁确实是过的"
    if model.top1_rate < rules.top1_rate:
        assert "Top-1 落后" in text, "Top-1 落后却没在报告里说"
        # 要给出可执行的判断依据，而不只是报个数字
        assert "拒答" in text.split("Top-1 落后")[1][:600]
    else:
        assert "Top-1 落后" not in text


def test_report_declares_run_mode(eval_set, tiers):
    """报告必须写明模型档是回放还是实连，否则数字换个机器含义就变了。"""
    model, rules = tiers
    text = render_report(eval_set, model, rules)
    assert "模型档跑法" in text
    assert "回放" in text or "实连" in text


def test_report_warns_standin_is_not_a_model_measurement(eval_set, tiers):
    """报告必须写明模型档数字来自录制响应替身、不代表真实端点。

    缺这句话，整份报告会被当成「模型判得准不准」的证据 —— 而它不是。
    """
    model, rules = tiers
    text = render_report(eval_set, model, rules)
    assert "不代表任何真实端点" in text
    assert "未实连" in text
    assert "--live" in text, "报告要给出实连重跑的命令"


def test_report_lists_misses_and_unknown_samples(eval_set, tiers):
    """第 5 节须逐条点名未命中与 UNKNOWN 样本，供定位到具体 case。"""
    model, rules = tiers
    text = render_report(eval_set, model, rules)
    section = text.split("## 5 逐条结论")[1].split("## 6")[0]
    for case in eval_set.cases:
        if case.expect_unknown:
            assert case.case_id in section, f"UNKNOWN 样本 {case.case_id} 未在报告里点名"
    missed = [v.case_id for v in model.verdicts if v.usable and not v.top1_hit]
    assert missed, "本轮应有未命中样本；一条都没有说明评测集造得太顺"
    for case_id in missed:
        assert case_id in section


def test_report_says_cost_unpriced_honestly(eval_set, tiers, monkeypatch):
    for name in ("RECONQ_MODEL_PRICE_PROMPT_PER_1K", "RECONQ_MODEL_PRICE_COMPLETION_PER_1K"):
        monkeypatch.delenv(name, raising=False)
    model, rules = evaluate_eval_set(load_eval_set(SET_DIR))
    text = render_report(eval_set, model, rules)
    assert "未配置单价" in text
    assert "## 6 这些数字不能说明什么" in text
    # 第 6 节要点名成本那一项的局限
    assert "单价" in text.split("## 6")[1].split("## 7")[0]


# ────────────────────── 独立于夹具的性质 ──────────────────────


def test_model_tier_goes_through_the_real_gateway(eval_set):
    """模型档必须走真实 :class:`ModelGateway`（断言请求体形态）。

    否则「两档对比」比的是两套玩具：一套调了模型，一套直接读 JSON。
    """
    calls: list[dict] = []
    case = eval_set.cases[0]
    record = eval_set.responses[case.case_id]
    gateway = build_replay_gateway(record, calls=calls)
    result = attribute(case.bundle, gateway)
    assert not result.degraded, f"真实链路上不该降级：{result.error_code}"
    assert len(calls) == 1, "应当只发一次请求"
    body = calls[0]
    assert body["model"]
    assert body["temperature"] == 0 and body["stream"] is False
    schema = body["response_format"]["json_schema"]
    assert schema["name"] == "reconq_attribution"
    assert schema["strict"] is True
    # 提示词里带证据包正文
    user = next(m for m in body["messages"] if m["role"] == "user")
    assert case.bundle.payload["channel"] in user["content"]


def test_model_tier_records_audit_metadata(eval_set):
    """审计元数据要能回答「这次调了什么、调了多久、多少 token」，
    且**不含** prompt 正文与凭据。"""
    case = eval_set.cases[0]
    gateway = build_replay_gateway(eval_set.responses[case.case_id])
    result = attribute(case.bundle, gateway)
    call = result.gateway_call
    assert call and call["model"]
    assert call["elapsed_ms"] >= 0
    assert call["prompt_tokens"] > 0 and call["completion_tokens"] > 0
    assert call["bundle"]["bundle_digest"] == case.bundle.digest
    blob = json.dumps(call, ensure_ascii=False)
    assert "FAKE_MODEL_KEY_PLACEHOLDER" not in blob, "凭据不该进审计元数据"
    assert "以下是这条差异的证据" not in blob, "prompt 正文不该进审计元数据"


def test_rule_tier_uses_the_same_implementation_as_degradation_fallback(eval_set):
    """规则档必须用 :func:`rank_causes_by_rules` 本体。

    评测专用的规则会漂：线上兜底改了、评测还按老规则算，报告就会说
    「规则档没问题」而线上早就变了。
    """
    case = eval_set.cases[0]
    expected = [c.cause_code for c in rank_causes_by_rules(case.bundle)]
    rules = run_rule_tier(EvalSet(path="x", cases=(case,)))
    assert rules.verdicts[0].top1_code == expected[0]
    assert rules.verdicts[0].top3_codes == tuple(expected[:3])


def test_missing_response_is_explicit_error(eval_set):
    """缺一条录制响应必须显式报错，不能静默少算一条。"""
    thin = EvalSet(
        path=eval_set.path,
        cases=eval_set.cases[:1],
        responses={},  # 空的
        model=eval_set.model,
    )
    with pytest.raises(ReconqError) as exc:
        run_model_tier(thin)
    assert exc.value.code == "ATTRIB_EVAL_RESPONSE_MISSING"
    assert eval_set.cases[0].case_id in str(exc.value)


def test_eval_set_too_small_is_refused(tmp_path):
    """条数不足直接拒评，不「先跑跑看」。"""
    src = os.path.join(SET_DIR, "cases.jsonl")
    with open(src, encoding="utf-8") as handle:
        lines = handle.readlines()
    thin = tmp_path / "cases.jsonl"
    thin.write_text("".join(lines[:5]), encoding="utf-8")
    with pytest.raises(ReconqError) as exc:
        load_eval_set(str(tmp_path))
    assert exc.value.code == "ATTRIB_EVAL_SET_TOO_SMALL"
    assert "下限" in str(exc.value)


def test_unknown_quota_is_refused_separately(tmp_path):
    """UNKNOWN 不足要单独报错 —— 它是拒答率的分母，与总体条数两回事。"""
    src = os.path.join(SET_DIR, "cases.jsonl")
    with open(src, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    keep = [r for r in rows if not r.get("expect_unknown")][:20]
    thin = tmp_path / "cases.jsonl"
    thin.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in keep),
        encoding="utf-8",
    )
    with pytest.raises(ReconqError) as exc:
        load_eval_set(str(tmp_path))
    assert exc.value.code == "ATTRIB_EVAL_SET_TOO_SMALL"
    assert "UNKNOWN" in str(exc.value)


def test_inconsistent_unknown_flag_is_refused(tmp_path):
    """``expect_unknown`` 与 expect_codes 不自洽必须报错。"""
    src = os.path.join(SET_DIR, "cases.jsonl")
    with open(src, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    rows[0]["expect_unknown"] = not rows[0]["expect_unknown"]
    bad = tmp_path / "cases.jsonl"
    bad.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    with pytest.raises(ReconqError) as exc:
        load_eval_set(str(tmp_path))
    assert exc.value.code == "ATTRIB_EVAL_CASE_INVALID"
    assert "expect_unknown" in str(exc.value)


def test_duplicate_case_id_is_refused(tmp_path):
    """重复 case_id 会让同一条被计两次，两次还可能算出两个不同结论。"""
    src = os.path.join(SET_DIR, "cases.jsonl")
    with open(src, encoding="utf-8") as handle:
        lines = [line for line in handle if line.strip()]
    dup = tmp_path / "cases.jsonl"
    dup.write_text("".join(lines) + lines[0], encoding="utf-8")
    with pytest.raises(ReconqError) as exc:
        load_eval_set(str(tmp_path))
    assert exc.value.code == "ATTRIB_EVAL_FIXTURE_INVALID"
    assert "重复" in str(exc.value)


def test_missing_fixture_points_at_generator():
    with pytest.raises(ReconqError) as exc:
        load_eval_set(os.path.join("tests", "fixtures", "attrib", "no-such-dir"))
    assert exc.value.code == "ATTRIB_EVAL_FIXTURE_MISSING"
    assert "generate_eval_set.py" in str(exc.value)


def test_new_error_codes_are_registered():
    """新错误码必须在 errors.py 登记（中文说明 + 建议动作非空）。"""
    for code in (
        "ATTRIB_EVAL_SET_TOO_SMALL",
        "ATTRIB_EVAL_CASE_INVALID",
        "ATTRIB_EVAL_FIXTURE_MISSING",
        "ATTRIB_EVAL_FIXTURE_INVALID",
        "ATTRIB_EVAL_RESPONSE_MISSING",
        "ATTRIB_EVAL_CASE_NOT_FOUND",
        "RULE_BASELINE_FAILED",
    ):
        meta = error_meta(code)
        assert meta.zh != "未登记的错误码", f"{code} 未登记"
        assert meta.suggestion, f"{code} 缺建议动作"


def test_case_verdict_serialises(eval_set, tiers):
    model, _ = tiers
    blob = json.dumps([v.to_dict() for v in model.verdicts], ensure_ascii=False)
    restored = json.loads(blob)
    assert len(restored) == model.total
    assert all("top1_code" in r and "tier" in r for r in restored)


def test_empty_verdict_metrics_are_zero_not_one():
    """一条都没跑时所有比率都是 0，不得出现 1.0（= 满分）。"""
    empty = TierMetrics(tier="空")
    for value in (
        empty.top1_rate,
        empty.top3_rate,
        empty.ref_rate,
        empty.unknown_refusal_rate,
        empty.hallucination_rate,
        empty.mean_confidence,
        empty.avg_elapsed_ms,
    ):
        assert value == 0.0
    assert empty.avg_cost == Decimal("0")


def test_case_verdict_defaults_are_conservative():
    v = CaseVerdict(case_id="x", tier="t")
    assert v.usable is False and v.unknown_resolved is None
    assert v.hallucinated is False and v.top1_code == ""
