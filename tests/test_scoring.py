"""弱匹配二级候选打分与分级（Task 23）。

六类规定断言：

1. **三档判定** —— 强候选 / 弱候选 / 不可用；
2. **容差边界** —— 差 0.01 元与差 0.02 元落在不同档（不是连续衰减）；
3. **权重变更生效** —— 配置改了分数与分档就跟着变，且改动写进事件链；
4. **候选数截断** —— 展示上限 5 条，被截掉的可经 expand 全查；
5. **不可用因子不进分母** —— 缺字段 ≠ 不一致；
6. **三档都不认领** —— 模块里没有认领通道。

外加三条硬不变量：

- 权重/容差/阈值全部来自 ``contracts/_scoring.yml``，不硬编码在代码分支里；
- 非法配置显式报错，不静默回落默认；
- 跨渠道印证是**线索**不是证据，单独不足以把候选抬进 strong。
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest

from reconq.align.scoring import (
    DEFAULT_SCORING_PATH,
    FACTOR_NAMES,
    FACTOR_ZH,
    TIER_ZH,
    Candidate,
    ScoringConfig,
    config_fingerprint,
    default_scoring_config,
    expand_candidates,
    explain_candidate,
    factor_rows,
    load_scoring_config,
    rank_candidates,
    record_config_change,
    score_pair,
)
from reconq.align.matcher import AlignRecord
from reconq.errors import ERROR_CODES, ReconqError

PERIOD = "2026-W36"


def _chan(
    amount: str,
    *,
    row: int = 1,
    channel: str = "wechat",
    time: str | None = "2026-09-02T10:00:00+08:00",
    dept: str = "",
    settle: str = "",
    order_no: str = "EX-UNKNOWN",
) -> AlignRecord:
    from datetime import datetime

    return AlignRecord(
        channel=channel,
        period=PERIOD,
        order_no=order_no,
        amount=Decimal(amount),
        trade_time=datetime.fromisoformat(time) if time else None,
        department=dept,
        settle_type=settle,
        source_file="wechat_tradebill_2026-W36.csv",
        source_row_no=row,
        ch_id=0,
    )


def _his(
    amount: str,
    *,
    ch_id: int = 1,
    time: str | None = "2026-09-02T10:00:00+08:00",
    dept: str = "",
    settle: str = "",
    order_no: str = "EX-H1",
) -> AlignRecord:
    from datetime import datetime

    return AlignRecord(
        channel="his",
        period=PERIOD,
        order_no=order_no,
        amount=Decimal(amount),
        trade_time=datetime.fromisoformat(time) if time else None,
        department=dept,
        settle_type=settle,
        his_order_no=order_no,
        source_file="charge_record.csv",
        source_row_no=100 + ch_id,
        ch_id=ch_id,
    )


def _config(**overrides) -> ScoringConfig:
    base = default_scoring_config()
    fields = {
        "amount_tolerance": base.amount_tolerance,
        "time_window_minutes": base.time_window_minutes,
        "weights": dict(base.weights),
        "strong_min": base.strong_min,
        "weak_min": base.weak_min,
        "max_candidates": base.max_candidates,
        "source_path": "<测试>",
    }
    fields.update(overrides)
    return ScoringConfig(**fields).validate()


# ────────────────────── 一、三档判定 ──────────────────────


def test_perfect_candidate_is_strong():
    """金额相等 + 时间同分钟 + 科室结算方式一致 → 强候选。"""
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="微信")
    candidate = score_pair(chan, his, config=_config())
    assert candidate.tier == "strong"
    assert candidate.score == Decimal("1.0000")
    assert {f.name for f in candidate.available_factors} >= {"amount", "time", "department"}


def test_department_mismatch_drops_to_weak():
    """金额与时间都对、科室对不上 → 弱候选（不是不可用，也不是强候选）。"""
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="住院收费处", settle="微信")
    candidate = score_pair(chan, his, config=_config())
    assert candidate.tier == "weak"
    assert candidate.score < Decimal("1.0000")
    dept = next(f for f in candidate.factors if f.name == "department")
    assert dept.available is True and dept.value == 0


def test_amount_beyond_tolerance_is_unusable():
    """金额超容差 → 加权分低于 weak_min → 不可用。"""
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("100.00", dept="门诊收费室", settle="微信")
    candidate = score_pair(chan, his, config=_config())
    assert candidate.tier == "unusable"
    assert candidate.score < _config().weak_min


def test_settle_type_mismatch_alone_also_downgrades():
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="现金")
    candidate = score_pair(chan, his, config=_config())
    assert candidate.tier == "weak"


def test_far_apart_in_time_cannot_be_strong_even_with_equal_amount():
    """金额相等但时间差出窗口：不是强候选。"""
    chan = _chan("128.00", dept="门诊收费室", settle="微信", time="2026-09-02T10:00:00+08:00")
    his = _his("128.00", dept="门诊收费室", settle="微信", time="2026-09-04T10:00:00+08:00")
    candidate = score_pair(chan, his, config=_config(time_window_minutes=60))
    assert candidate.tier != "strong"


# ────────────────────── 二、容差边界（0.01 vs 0.02） ──────────────────────


def test_tolerance_boundary_one_cent_vs_two_cents():
    """差 0.01 元在容差内（记 0.5 分），差 0.02 元超容差（记 0 分）。

    刻意不做连续衰减：院内导出常把分位抹零（收 100 记 99.99），差 0.01 与
    差 0.02 在业务上不是同一档 —— 后者多半是某一边漏了一笔费用项目。
    """
    config = _config()
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    inside = score_pair(chan, _his("127.99", dept="门诊收费室", settle="微信"), config=config)
    outside = score_pair(chan, _his("127.98", dept="门诊收费室", settle="微信"), config=config)
    a_in = next(f for f in inside.factors if f.name == "amount")
    a_out = next(f for f in outside.factors if f.name == "amount")
    assert a_in.value == Decimal("0.5")
    assert a_out.value == Decimal("0")
    assert inside.tier == "weak"
    assert outside.tier == "unusable"


def test_widening_tolerance_changes_the_verdict():
    """容差调宽到 0.05 后，差 0.03 元从「不可用」变「弱候选」——配置真的生效。"""
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("127.97", dept="门诊收费室", settle="微信")
    assert score_pair(chan, his, config=_config()).tier == "unusable"
    wide = _config(amount_tolerance=Decimal("0.05"))
    assert score_pair(chan, his, config=wide).tier == "weak"


# ────────────────────── 三、权重变更生效 ──────────────────────


def test_weight_change_moves_the_score():
    """把科室权重从 0 调到 0.30，科室不一致的候选分数必须变化。

    「权重 0」与「不写这个键」是**两件不同的事**，这里刻意用 0：
    权重 0 表示该路不参与加权（财务科对没有科室列的渠道的正确用法），
    缺键则是配置写漏了，:meth:`ScoringConfig.validate` 会直接报错。
    """
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="住院收费处", settle="微信")
    weights = dict(_config().weights)
    before = score_pair(chan, his, config=_config(weights={**weights, "department": Decimal("0")}))
    after = score_pair(chan, his, config=_config(weights={**weights, "department": Decimal("0.30")}))
    assert before.score != after.score
    assert after.score < before.score
    assert before.tier == "strong"      # 科室不参与加权时，权重全落在满足的因子上
    assert after.tier == "weak"


def test_strong_threshold_change_flips_the_tier():
    """同一对数据，strong_min 从 0.85 提到 0.999 就不再是强候选。

    刻意用一对**不是满分**的候选：时间差 30 分钟（时间因子 1410/1440 ≈ 0.979），
    加权分 0.9948，正好夹在 0.85 与 0.999 两个门槛之间。满分候选（1.0000）演示
    不了门槛翻转 —— 任何 ≤ 1 的门槛都拦不住 1.0，用它写这条断言等于什么都没验。
    """
    chan = _chan("128.00", dept="门诊收费室", settle="微信", time="2026-09-02T10:00:00+08:00")
    his = _his("128.00", dept="门诊收费室", settle="微信", time="2026-09-02T10:30:00+08:00")
    baseline = score_pair(chan, his, config=_config(strong_min=Decimal("0.85")))
    assert baseline.tier == "strong"
    assert baseline.score == Decimal("0.9948")
    assert score_pair(chan, his, config=_config(strong_min=Decimal("0.999"))).tier == "weak"


def test_shipped_contract_file_loads_and_is_used_not_hardcoded():
    """仓库里的 contracts/_scoring.yml 必须能解析，且默认值与内置一致。

    不一致的后果：财务科照 README 调了参数，实际跑的是内置值，界面上看不出
    任何异常，而「为什么没效果」要查到配置才答得出来。
    """
    assert os.path.isfile(DEFAULT_SCORING_PATH), "缺少 contracts/_scoring.yml"
    loaded = load_scoring_config(DEFAULT_SCORING_PATH)
    builtin = default_scoring_config()
    assert loaded.amount_tolerance == builtin.amount_tolerance
    assert loaded.time_window_minutes == builtin.time_window_minutes
    assert dict(loaded.weights) == dict(builtin.weights)
    assert loaded.strong_min == builtin.strong_min
    assert loaded.weak_min == builtin.weak_min
    assert loaded.max_candidates == builtin.max_candidates
    assert set(loaded.weights) == set(FACTOR_NAMES)


def test_missing_file_falls_back_to_builtin_but_broken_file_raises():
    """文件缺失回落默认（打分是加深能力，不该让整次扫描挂掉）；
    文件存在但写错则显式报错——写错与没写是两种完全不同的情况。"""
    fallback = load_scoring_config(os.path.join("contracts", "不存在.yml"))
    assert fallback.fingerprint() == default_scoring_config().fingerprint()

    with pytest.raises(ReconqError) as exc:
        load_scoring_config(os.path.join("tests", "fixtures", "contracts", "scoring_bad_float.yml"))
    assert exc.value.code == "SCORING_CONFIG_INVALID"
    assert "浮点" in str(exc.value)


def test_floating_point_weight_is_rejected_with_a_named_field():
    """裸小数会被 YAML 解析成 float → 报出具体字段名与文件路径。

    浮点权重在门槛比较上会产生边界翻转（本该 0.85 的候选算出 0.8499999…），
    且这种翻转会随数据变化，看起来像随机。
    """
    with pytest.raises(ReconqError) as exc:
        load_scoring_config(os.path.join("tests", "fixtures", "contracts", "scoring_bad_float.yml"))
    assert "weights.amount" in str(exc.value)
    assert "scoring_bad_float.yml" in str(exc.value)


@pytest.mark.parametrize(
    "raw,expect",
    [
        # 只写一个因子 → 缺四个。刻意与下面「表外因子」分开：缺键和写错名字是
        # 两种不同的事，财务科改配置时踩的坑也不一样，错误文案必须分开报。
        ("weights:\n  amount: \"0.4\"\n", "缺因子权重"),
        ("weights:\n  amount: \"0.4\"\n  科室: \"0.2\"\n", "未知因子"),
        ("weights: []\n", "weights 段"),
        ("tiers:\n  strong_min: \"0.2\"\n  weak_min: \"0.5\"\n", "分档门槛"),
    ],
)
def test_malformed_scoring_configs_report_their_own_problem(tmp_path, raw, expect):
    """配置写错的每一种都要报出自己的问题，而不是笼统地说「配置非法」。"""
    path = tmp_path / "_scoring.yml"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ReconqError) as exc:
        load_scoring_config(str(path))
    assert exc.value.code == "SCORING_CONFIG_INVALID"
    assert expect in str(exc.value)


def test_all_zero_weights_is_rejected():
    """权重全 0 会让所有候选同分、分档失去意义 —— 显式报错。"""
    with pytest.raises(ReconqError) as exc:
        _config(weights={name: Decimal("0") for name in FACTOR_NAMES})
    assert "全为 0" in str(exc.value)


def test_zero_weight_is_allowed_and_marks_the_factor_inert():
    """单个因子写 0 是合法用法（表示该因子不参与加权），不是非法值。"""
    config = _config(weights={**dict(_config().weights), "settle_type": Decimal("0")})
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="现金")
    candidate = score_pair(chan, his, config=config)
    settle = next(f for f in candidate.factors if f.name == "settle_type")
    assert settle.contribution == 0
    assert candidate.tier == "strong"   # 结算方式对不上但权重为 0，不影响分档


def test_config_fingerprint_ignores_source_path_but_tracks_values():
    """指纹只覆盖会改变结论的字段：换个路径不该让「口径变没变」被污染。"""
    a = _config(source_path="/a/_scoring.yml")
    b = _config(source_path="/b/_scoring.yml")
    assert config_fingerprint(a) == config_fingerprint(b)
    c = _config(source_path="/a/_scoring.yml", strong_min=Decimal("0.9"))
    assert config_fingerprint(a) != config_fingerprint(c)


def test_weight_change_is_written_to_the_event_chain(tmp_path):
    """权重变更必须留痕：上周那条是强候选、这周成了弱候选，要当场答得出原因。

    这里读**落盘的 JSONL 事件链**而不是 ``caplog``：事件链本来就是给人翻的
    （运维翻日志、结论单引证都走它），验它就该验落盘后的样子。用 caplog 验
    还有个更实际的坑—— :func:`configure_logging` 会给 ``reconq`` logger 设
    ``propagate=False``，只要别的测试先跑过一次配置，这条测试单跑绿、全量
    跑红，是典型的「顺序相关的假绿」。
    """
    from reconq.runlog import configure_logging, reset_logging

    config = _config()
    try:
        configure_logging(log_dir=str(tmp_path), level="INFO")
        first = record_config_change(config)
        assert first is not None and first["fingerprint"] == config.fingerprint()
        # 指纹没变时不重复记事件 —— 否则每次扫描都往事件链里灌一条一模一样的
        assert record_config_change(config, previous_fingerprint=first["fingerprint"]) is None
        changed = record_config_change(
            _config(weights={**dict(config.weights), "department": Decimal("0.30")}),
            previous_fingerprint=first["fingerprint"],
            channel="wechat",
            period=PERIOD,
        )
        assert changed is not None
        assert changed["previous_fingerprint"] == first["fingerprint"]
        for handler in __import__("logging").getLogger("reconq").handlers:
            handler.flush()
    finally:
        reset_logging()

    lines = [
        line for line in next(tmp_path.glob("reconq-*.jsonl")).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2, "只应有首次登记 + 权重变更两条"
    payload = json.loads(lines[1])
    assert payload["event"] == "align.scoring.config"
    assert payload["channel"] == "wechat" and payload["period"] == PERIOD
    # 变更明细要能直接读：新旧指纹 + 每个因子的当前权重（这里能看到 0.30）
    assert payload["previous_fingerprint"] == first["fingerprint"]
    assert "0.30" in payload["config"]
    assert "department=0.30" in payload["config"]
    # 机器侧要精确值时走 config_json，不用来回解析给人看的那段文本
    assert json.loads(payload["config_json"])["weights"]["department"] == "0.30"


# ────────────────────── 四、候选数截断 ──────────────────────


def test_candidates_are_truncated_to_the_configured_cap():
    """展示上限默认 5 条，且被截掉时必须显式标 truncated —— 静默截断会让
    「有 300 条候选」和「只有 5 条」在面板上长得一样。"""
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his_rows = [
        _his("128.00", ch_id=i, dept="门诊收费室", settle="微信", order_no=f"EX-H{i}")
        for i in range(1, 13)
    ]
    result = rank_candidates(chan, his_rows, config=_config())
    assert result.candidate_total == 12
    assert len(result.candidates) == 5
    assert result.truncated is True
    assert "截断" in result.render()


def test_no_truncation_when_under_the_cap():
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his_rows = [_his("128.00", ch_id=i, order_no=f"EX-H{i}") for i in range(1, 4)]
    result = rank_candidates(chan, his_rows, config=_config())
    assert result.truncated is False
    assert result.candidate_total == 3


def test_expand_returns_every_candidate_including_the_truncated_strong_ones():
    """截断只是展示上限，不是判定上限：被截掉的第 6 条里完全可能有强候选。

    这个反例是真实会发生的——把强的排在后面需要 HIS 侧顺序不利，
    而 HIS 流水是按收费时间导出的，本来就没有「强的在前」这个保证。
    """
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    weak_rows = [
        _his("120.00", ch_id=i, dept="住院收费处", settle="现金", order_no=f"EX-W{i}")
        for i in range(1, 8)
    ]
    strong_row = _his("128.00", ch_id=99, dept="门诊收费室", settle="微信", order_no="EX-S")
    result = rank_candidates(chan, weak_rows + [strong_row], config=_config())
    shown = expand_candidates(chan, weak_rows + [strong_row], config=_config())
    assert result.candidate_total == 8
    # 分数降序，强候选一定在最前 —— 若排序失效这条会红
    assert result.candidates[0].his_ref == strong_row.ref
    assert len(shown) == 8
    assert any(c.is_strong() for c in shown)


def test_candidates_are_sorted_by_score_descending():
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    rows = [
        _his("100.00", ch_id=1, dept="住院收费处", settle="现金", order_no="EX-1"),
        _his("128.00", ch_id=2, dept="门诊收费室", settle="微信", order_no="EX-2"),
        _his("127.99", ch_id=3, dept="门诊收费室", settle="微信", order_no="EX-3"),
    ]
    scores = [c.score for c in expand_candidates(chan, rows, config=_config())]
    assert scores == sorted(scores, reverse=True)


def test_ties_keep_the_input_order():
    """同分候选保持 HIS 侧原有顺序：把「时间更早的更可能」写进排序键，
    就等于给了一个没人能解释的隐藏判据。"""
    chan = _chan("128.00")
    rows = [_his("128.00", ch_id=i, order_no=f"EX-{i}") for i in (3, 1, 2)]
    ordered = expand_candidates(chan, rows, config=_config())
    assert [c.his_ref for c in ordered] == [r.ref for r in rows]


def test_displayable_excludes_unusable_but_expand_still_has_them():
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    rows = [
        _his("128.00", ch_id=1, dept="门诊收费室", settle="微信", order_no="EX-G"),
        _his("50.00", ch_id=2, order_no="EX-B"),
    ]
    result = rank_candidates(chan, rows, config=_config())
    assert len(result.displayable) == 1
    assert len(result.usable_strong() if hasattr(result, "usable_strong") else result.strong) == 1
    assert len(result.unusable) == 1
    assert result.candidate_total == 2  # 不可用的仍计入候选总数，不假装没有


# ────────────────────── 五、不可用因子不进分母 ──────────────────────


def test_missing_department_makes_the_factor_unavailable_not_false():
    """渠道账单导出普遍没有科室列 —— 空值必须记「不可用」而不是「不一致」。

    记成不一致的后果很具体：所有走文件通道的候选都会平白少 0.15/(剩余权重和)
    的分，强候选全掉成弱候选，而分档看起来「正常」。
    """
    chan = _chan("128.00", dept="", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="微信")
    candidate = score_pair(chan, his, config=_config())
    dept = next(f for f in candidate.factors if f.name == "department")
    assert dept.available is False
    assert dept.value is None
    assert dept.contribution == 0
    assert "不可用" in dept.render()
    # 不可用因子不进分母：金额与时间权重 0.70 归一后仍为满分
    assert candidate.score == Decimal("1.0000")


def test_unusable_factor_is_excluded_from_the_denominator():
    """三个因子里两个不可用时，分母只按剩下两个归一。

    显式传 ``corroborations=[]``（= 已比对过其它渠道、确实没有同额待确认
    记录），让跨渠道因子以「可用且记 0 分」进分母。不传时它记「不可用」，
    那条语义由 test_corroborations_not_supplied_marks_the_factor_unavailable 守。
    """
    chan = _chan("128.00", dept="", settle="", time=None)
    his = _his("128.00", dept="门诊收费室", settle="微信")
    candidate = score_pair(chan, his, config=_config(), corroborations=[])
    assert [f.name for f in candidate.unusable_factors] == ["time", "department", "settle_type"]
    assert [f.name for f in candidate.available_factors] == ["amount", "cross_channel"]
    # 金额 1.0×0.45 + 跨渠道 0×0.10，除以 0.55 → 0.8182
    assert candidate.score == Decimal("0.8182")


def test_missing_trade_time_is_unavailable_not_zero():
    """缺交易时间戳说明「不知道」，记 0 等于凭空造出「时间对不上」的证据。"""
    chan = _chan("128.00", time=None)
    his = _his("128.00")
    factor = next(f for f in score_pair(chan, his, config=_config()).factors if f.name == "time")
    assert factor.available is False
    assert "无交易时间" in factor.detail


def test_explain_candidate_states_the_denominator_and_the_skipped_factors():
    chan = _chan("128.00", dept="", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="微信")
    text = explain_candidate(score_pair(chan, his, config=_config()))
    assert "分母按" in text and "科室一致" in text
    assert "不构成认领" in text


# ────────────────────── 六、三档都不认领 ──────────────────────


def test_no_tier_claims_anything():
    """strong 也只是「先看哪一条」——Candidate 上没有认领字段，模块内也无认领通道。"""
    assert not hasattr(Candidate, "claimed")
    assert not hasattr(Candidate, "his_record")
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "src", "reconq", "align", "scoring.py"),
        encoding="utf-8",
    ).read()
    # 认领语义只允许出现在「本档位不构成认领」这类说明文字里
    for line in source.splitlines():
        if "认领" in line:
            stripped = line.strip()
            assert stripped.startswith(("#", "*", '"')) or "docstring" in stripped or (
                stripped.startswith("lines.append") or stripped.startswith("return")
            ) or "不" in stripped, f"疑似认领代码路径：{line}"


def test_render_marks_that_the_tier_does_not_claim():
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="微信")
    candidate = score_pair(chan, his, config=_config())
    assert candidate.tier == "strong"
    text = candidate.render()
    assert "不构成认领" in text
    assert "需人工确认" in text
    assert "强候选" in text


def test_candidate_converts_to_the_shared_ambiguity_candidate_type():
    """复用 AmbiguityCandidate：面板、台账、结论单三处渲染走同一个 render()。"""
    from reconq.align.ambiguity import AmbiguityCandidate

    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="微信")
    converted = score_pair(chan, his, config=_config()).to_candidate()
    assert isinstance(converted, AmbiguityCandidate)
    assert converted.record_id == "EX-H1"
    assert converted.amount == "128.00"
    assert "charge_record.csv" in converted.ref


# ────────────────────── 七、跨渠道印证 ──────────────────────


def test_cross_channel_corroboration_adds_score_and_a_hint():
    """同金额在另一路渠道也出现、且那一路同样无 HIS 对应 → 加分并留线索。"""
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="微信")
    alone = score_pair(chan, his, config=_config(), corroborations=[])
    boosted = score_pair(chan, his, config=_config(), corroborations=["self_service"])
    assert boosted.score > alone.score
    assert boosted.hints and "合并支付" in boosted.hints[0]
    factor = next(f for f in boosted.factors if f.name == "cross_channel")
    assert factor.value == 1
    assert "合并支付" in factor.detail


def test_cross_channel_alone_cannot_promote_to_strong():
    """跨渠道印证是**线索**不是证据：单独不足以把候选抬进 strong。

    金额差 0.01 元（容差内记 0.5）+ 跨渠道满分，仍因「金额不绝对相等」
    落弱候选 —— 否则经办人会把一笔分位抹零的款当成已对上。
    """
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("127.99", dept="门诊收费室", settle="微信")
    candidate = score_pair(chan, his, config=_config(), corroborations=["self_service"])
    assert candidate.tier == "weak"
    assert candidate.hints


def test_cross_channel_ignores_its_own_channel():
    """同一路渠道内部的同额不算互相印证。"""
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="微信")
    same = score_pair(chan, his, config=_config(), corroborations=["wechat"])
    assert next(f for f in same.factors if f.name == "cross_channel").value == 0


def test_corroborations_not_supplied_marks_the_factor_unavailable():
    """只跑单路时无法判断跨渠道印证 —— 记不可用，不记 0。

    记 0 相当于断言「其它渠道没有同额待确认记录」，而单路运行时根本没看过
    其它渠道，那是个编出来的结论。
    """
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    his = _his("128.00", dept="门诊收费室", settle="微信")
    candidate = score_pair(chan, his, config=_config(), corroborations=None)
    factor = next(f for f in candidate.factors if f.name == "cross_channel")
    assert factor.available is False
    assert "未提供" in factor.detail


# ────────────────────── 八、说明表与序列化 ──────────────────────


def test_factor_rows_come_from_the_config_not_from_hardcoded_values():
    config = _config(weights={**dict(_config().weights), "amount": Decimal("0.60")})
    rows = {r["name"]: r for r in factor_rows(config)}
    assert set(rows) == set(FACTOR_NAMES)
    assert rows["amount"]["weight"] == "0.60"
    assert all(r["name_zh"] == FACTOR_ZH[r["name"]] for r in rows.values())
    # 占比之和为 1（分母是权重和，不是写死的 1）
    total = sum(Decimal(r["share"]) for r in rows.values())
    assert total == Decimal("1.0000")


def test_config_to_dict_is_json_serialisable_with_string_decimals():
    import json

    payload = json.loads(json.dumps(_config().to_dict()))
    assert payload["amount_tolerance"] == "0.01"
    assert payload["fingerprint"] == _config().fingerprint()
    assert set(payload["weights"]) == set(FACTOR_NAMES)


def test_scored_row_to_dict_carries_truncation_and_tier_counts():
    chan = _chan("128.00", dept="门诊收费室", settle="微信")
    rows = [_his("128.00", ch_id=i, dept="门诊收费室", settle="微信", order_no=f"EX-{i}") for i in range(1, 9)]
    payload = rank_candidates(chan, rows, config=_config()).to_dict()
    assert payload["candidate_total"] == 8
    assert payload["shown"] == 5
    assert payload["truncated"] is True
    assert sum(payload["tier_counts"].values()) == 5
    assert payload["config_fingerprint"] == _config().fingerprint()
    first = payload["candidates"][0]
    assert {"tier", "score", "factors", "hints", "channel_ref", "his_ref"} <= set(first)


# ────────────────────── 九、契约目录不把配置文件当渠道 ──────────────────────


def test_scoring_config_is_not_discovered_as_a_channel():
    """contracts/_scoring.yml 与渠道契约同目录但不是渠道契约。

    当成渠道的话，reconq verify/scan 会去给 _scoring.yml 找
    inbox/<账期>/_scoring/ 账单目录，整条命令直接失败。
    """
    from reconq.cli import _discover_channels

    channels = _discover_channels("contracts")
    assert "_scoring" not in channels
    assert set(channels) == {
        "alipay", "his", "medical_ins", "self_service", "unionpay", "wechat",
    }


def test_scoring_error_code_is_registered():
    meta = ERROR_CODES["SCORING_CONFIG_INVALID"]
    assert meta.zh and meta.suggestion
    assert meta.channel_level is False   # 打分是六路共用的，不是单渠道故障
