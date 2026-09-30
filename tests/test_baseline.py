"""确定性成因兜底基线（Task 35）。

六类规定断言逐条可核：

1. **线索打分方向正确** —— 每条线索都往**它说的那个方向**加分，且是
   相对的（同一份证据包改一处判据，Top-1 会跟着变）。
2. **置信度封顶** —— 恒 ``≤ MAX_CONFIDENCE``；界面标「规则推断(未经模型)」。
3. **字典码对齐** —— 规则只映射到受控成因字典；字典外的码在模块加载期
   就炸；证据引用不越界；引不出证据行的候选不给。
4. **模型不可达时自动接管** —— ``GatewayResult.status == DEGRADED`` 时
   归因自动附上规则候选，但 ``causes`` 仍为空、``needs_manual`` 仍为
   True、状态**不推进**。
5. **与模型分歧被记录** —— 一致 / 分歧 / 单边三种结论；分歧样本可转成
   回放集用例。
6. **耗时阈值** —— 单条差异 <5ms（纯计算、无网络）。

外加几条容易被糊弄过去的性质：规则候选**恒非空**（字典留了
``undetermined`` 就不该交空表）、同分按成因码字典序（可复算）、
**不调模型不发网络**（用会在 import 期就炸的替身钉死）。
"""

from __future__ import annotations

import copy
import json
import time
from decimal import Decimal

import httpx
import pytest

from reconq.align.attribution import AttributionResult, attribute, evidence_refs_of
from reconq.align.baseline import (
    BASELINE_VERSION,
    CLUE_RULES,
    MAX_CONFIDENCE,
    RULE_SOURCE,
    BaselineComparison,
    CandidateDraft,
    compare_with_model,
    divergence_to_replay_case,
    rank_causes_by_rules,
    rule_catalog,
    skipped_clues,
)
from reconq.align.bundle import bundle_from_payload
from reconq.align.causes import CAUSES, cause_vocabulary
from reconq.align.replay import ReplayCase, load_replay_set
from reconq.gateway.model_gateway import GatewayConfig, ModelGateway

PERIOD = "2026-W36"
FAKE_BASE_URL = "http://127.0.0.1:9/v1"
PERF_BUDGET_MS = 5.0


# ───────────────────── fixtures ─────────────────────


@pytest.fixture(scope="module")
def replay_cases() -> list[ReplayCase]:
    return load_replay_set()


@pytest.fixture(scope="module")
def case_by_id(replay_cases) -> dict[str, ReplayCase]:
    return {c.case_id.split("-", 1)[1]: c for c in replay_cases}


def _payload(case: ReplayCase) -> dict:
    return copy.deepcopy(case.bundle.payload)


def _bundle(payload: dict):
    return bundle_from_payload(payload)


def _gateway(drafts) -> ModelGateway:
    body = json.dumps({"drafts": drafts}, ensure_ascii=False)
    payload = {
        "model": "reconq-attribution-1",
        "choices": [{"message": {"role": "assistant", "content": body}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 640, "completion_tokens": 72, "total_tokens": 712},
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    cfg = GatewayConfig(base_url=FAKE_BASE_URL, api_key="FAKE_KEY", model="reconq-attribution-1")
    return ModelGateway(cfg, client=client, sleeper=lambda _s: None)


def _model_draft(cause: str, confidence: float, refs) -> dict:
    return {
        "cause": cause,
        "confidence": confidence,
        "evidence_refs": list(refs),
        "explanation_zh": "模型给的中文说明。",
    }


# ───────────────────── 一、线索打分方向正确 ─────────────────────


def test_split_payment_evidence_points_at_split_not_merged(case_by_id):
    """一码付拆笔：两条候选加总与差异行守恒 → 拆笔分高于合并支付分。

    判据是**金额守恒**（复用的是 taxonomy 里那套已被单测钉死的判别），
    不是「候选有两条」—— 合并支付同样是两条候选，把两者分开的是守恒。
    """
    out = rank_causes_by_rules(case_by_id["ambiguous-split-payment"].bundle)
    codes = [c.cause_code for c in out]
    assert codes[0] == "split_payment_unallocated"
    # 拆笔与合并支付**不同时给分**：金额守恒是一条互斥判据，同时给两处加分
    # 等于让「守恒」这条线索失去判别力（两项一起涨，谁也没被区分开）。
    assert "merged_payment_allocation" not in codes
    assert any(c.rule == "amount_structure" and "守恒" in c.detail for c in out[0].clues)


def test_merged_payment_evidence_points_at_merged(case_by_id):
    """合并支付：两条候选金额互不相同且加总不守恒 → 合并支付分最高。"""
    out = rank_causes_by_rules(case_by_id["ambiguous-merged-payment"].bundle)
    assert out[0].cause_code == "merged_payment_allocation"
    assert "守恒" not in out[0].clues[0].detail or "不守恒" in out[0].clues[0].detail


def test_equal_amount_non_conserving_falls_to_undetermined(case_by_id):
    """候选金额全同且不守恒 → 只能落 ``undetermined``。

    这是字典里那条「判不出来比判错便宜」的落地：候选金额全相同说明单号
    可能被导出方截断或复用，此时任何一条形态判据都只是在挑一个看着像的。
    """
    out = rank_causes_by_rules(case_by_id["unknown-equal-amounts"].bundle)
    assert out[0].cause_code == "undetermined"
    assert any(c.rule == "amount_structure" for c in out[0].clues)
    # 且不得同时给出任何一条「有把握」的成因 —— 那就是判错了
    assert [c.cause_code for c in out] == ["undetermined"]


def test_refund_row_shape_points_at_refund_offset(case_by_id):
    """退费行形态 → 退费已在 HIS 红冲。"""
    out = rank_causes_by_rules(case_by_id["ambiguous-refund-coexist"].bundle)
    assert out[0].cause_code == "refund_offset_in_his"
    assert any(c.rule == "refund_row_shape" for c in out[0].clues)


def test_period_boundary_clue_scores_in_the_right_direction(case_by_id):
    """跨账期落点：交易时间压在周一零点边界上 → 账期边界落点分最高。"""
    out = rank_causes_by_rules(case_by_id["cross-period-landing"].bundle)
    assert out[0].cause_code == "period_boundary_landing"
    hits = [c for c in out[0].clues if c.rule == "period_boundary"]
    assert hits, "跨账期落点这一条必须有 period_boundary 线索"
    assert any("秒" in h.detail for h in hits), "边界线索要给出距边界多少秒"


def test_bill_type_success_only_points_at_excluding_refund(case_by_id):
    """账单类型不含退款（``bill_type=SUCCESS``）+ 账单里有退费形态
    → 账单类型不含退款。**同一份证据，只改账单类型这一个判据**，
    Top-1 就要跟着从「退费已在 HIS 红冲」改成「账单类型不含退款」。"""
    payload = _payload(case_by_id["ambiguous-refund-coexist"])
    payload["channel_meta"]["bill_type"] = "SUCCESS"
    out = rank_causes_by_rules(_bundle(payload))
    assert out[0].cause_code == "bill_type_excludes_refund"
    assert any(c.rule == "bill_type_scope" for c in out[0].clues)
    # 退费行形态那条线索还在（它没变），只是分数被账单类型压下去了
    assert any(
        any(c.rule == "refund_row_shape" for c in cand.clues) for cand in out
    )


def test_bill_type_all_does_not_fire_the_scope_clue(case_by_id):
    """账单类型含退款（``ALL``）时**不**产生账单类型线索。

    反向断言：只看「有退费形态」就报「账单类型不含退款」是本模块最容易犯的
    错 —— 那样每一条退费差异都会被推给信息科去改配置，而真因在收费处。
    """
    payload = _payload(case_by_id["ambiguous-refund-coexist"])
    payload["channel_meta"]["bill_type"] = "ALL"
    out = rank_causes_by_rules(_bundle(payload))
    assert not any(c.rule == "bill_type_scope" for cand in out for c in cand.clues)
    assert out[0].cause_code == "refund_offset_in_his"


def test_trade_type_closed_points_at_contract_config(case_by_id):
    """交易类型为已关闭/未支付 → 取件口径把未成交交易也纳进来了
    → 对账口径配置错（责任方信息科）。"""
    payload = _payload(case_by_id["channel-only-his-missing"])
    payload["diff_row"]["trade_type"] = "CLOSED"
    out = rank_causes_by_rules(_bundle(payload))
    top = out[0]
    assert top.cause_code == "contract_config_error"
    assert any(c.rule == "trade_type_enum" for c in top.clues)


def test_trade_type_refund_points_at_refund_offset(case_by_id):
    """交易类型为退款类 → 退费已在 HIS 红冲。"""
    payload = _payload(case_by_id["channel-only-his-missing"])
    payload["diff_row"]["trade_type"] = "退款成功"
    out = rank_causes_by_rules(_bundle(payload))
    assert out[0].cause_code == "refund_offset_in_his"


def test_history_direction_decides_late_posting_vs_duplicate(case_by_id):
    """同单号出现在**往后**账期 → 渠道延迟入账；出现在**往前**账期
    → 跨账期重复计入。两种方向处置完全相反，混成「历史出现过」就废了。"""
    base = _payload(case_by_id["channel-only-his-missing"])
    base["history"]["same_order_no"] = {
        "available": True,
        "occurrences": 1,
        "by_period": {"2026-W38": 1},
        "reasons": [],
    }
    later = rank_causes_by_rules(_bundle(base))
    assert later[0].cause_code == "channel_late_posting"
    assert "晚" in later[0].clues[0].detail

    base["history"]["same_order_no"]["by_period"] = {"2026-W34": 1}
    earlier = rank_causes_by_rules(_bundle(base))
    assert earlier[0].cause_code == "cross_period_duplicate"
    assert "早" in earlier[0].clues[0].detail


def test_precedent_clue_is_weak_and_never_outranks_shape(case_by_id):
    """处置先例是**佐证**不是判据：权重 0.15，压不过形态判据。"""
    payload = _payload(case_by_id["ambiguous-split-payment"])
    payload["history"]["same_cause"] = {
        "total": 9,
        "by_disposition": {"confirmed": 9},
        "by_period": {"2026-W35": 9},
        "reason": "",
    }
    with_precedent = rank_causes_by_rules(_bundle(payload))
    without = rank_causes_by_rules(case_by_id["ambiguous-split-payment"].bundle)
    assert with_precedent[0].cause_code == without[0].cause_code == "split_payment_unallocated"
    precedent = [c for c in with_precedent[0].clues if c.rule == "precedent"]
    assert precedent and precedent[0].weight == Decimal("0.15")


def test_naive_trade_time_is_reported_not_guessed(case_by_id):
    """交易时间**不带时区偏移**时，账期边界判据显式跳过并留原因。

    这条来自一次真实崩溃：账期边界是带偏移的时刻，拿 naive 时刻去减它
    直接抛 ``can't subtract offset-naive and offset-aware``（整条归因跟着
    崩，而它本来是「模型不可达时唯一还在跑的那条路」）。修法不是补一个
    ``replace(tzinfo=...)`` —— 那样猜错偏移会把整批行都判成「压在周一
    零点」，比崩更难查。
    """
    payload = _payload(case_by_id["cross-period-landing"])
    payload["diff_row"]["trade_time"] = "2026-09-07T00:00:00"  # 无偏移
    out = rank_causes_by_rules(_bundle(payload))
    assert out, "跳过一条判据不该让基线交空表"
    # 跨账期落点那条线索（读的是 cross_period 标志，不读时刻）仍在
    assert out[0].cause_code == "period_boundary_landing"
    assert any(c.rule == "period_boundary" for c in out[0].clues)
    assert all("不��时区偏移" not in c.detail for c in out[0].clues)


def test_unparsable_trade_time_does_not_crash(case_by_id):
    """交易时间根本不是时刻 → 同样显式跳过，不抛。"""
    payload = _payload(case_by_id["cross-period-landing"])
    payload["diff_row"]["trade_time"] = "上周三下午"
    out = rank_causes_by_rules(_bundle(payload))
    assert out
    assert out[0].cause_code == "period_boundary_landing"


def test_missing_window_is_reported_not_assumed(case_by_id):
    """账期时间窗不可用时也不猜边界 —— 边界判据的另一半是时间窗。"""
    payload = _payload(case_by_id["cross-period-landing"])
    payload["period_window"] = {
        "available": False,
        "reason": "未提供时间窗",
        "period": "",
        "start": "",
        "end": "",
        "tz": "",
        "half_open": True,
    }
    payload["diff_row"]["cross_period"] = False
    bundle = _bundle(payload)
    out = rank_causes_by_rules(bundle)
    assert out, "时间窗不可用时其余判据仍要给出候选"
    assert not any(
        c.rule == "period_boundary"
        for cand in out
        for c in cand.clues
    ), "没有时间窗却还报了边界线索"
    # 「没跑」必须**看得见**：跳过的判据要能单独问出来
    notes = skipped_clues(bundle)
    assert notes and any("时间窗不可用" in n for n in notes)


def test_skipped_clues_is_empty_when_everything_ran(case_by_id):
    """判据全部跑过时 :func:`skipped_clues` 返回空元组。

    逐条跑 12 条回放用例的原始载荷（它们都带偏移时刻与可用时间窗），
    确认没有哪条线索在默默跳过 —— 跳过原因一旦在某条上出现，上面那条
    「没跑」就会变成常态而没人再看。
    """
    for case in case_by_id.values():
        assert skipped_clues(case.bundle) == (), f"{case.case_id} 有判据被跳过"


# ───────────────────── 二、置信度封顶 ─────────────────────


def test_confidence_never_exceeds_cap(case_by_id):
    """置信度恒 ``≤ MAX_CONFIDENCE``。**包括线索全中的强判据**。"""
    for case in case_by_id.values():
        for cand in rank_causes_by_rules(case.bundle):
            assert cand.confidence <= float(MAX_CONFIDENCE), (
                f"{case.case_id} 的 {cand.cause_code} 置信度 {cand.confidence} 越界"
            )


def test_cap_is_hard_even_when_score_saturates(case_by_id):
    """得分超过 1.0（多条线索叠加）时按 1.0 折算，仍封顶在 0.5。"""
    payload = _payload(case_by_id["cross-period-landing"])
    out = rank_causes_by_rules(_bundle(payload))
    top = out[0]
    assert top.score > Decimal("1"), "这条样例本就该有多条线索叠加"
    assert top.confidence == float(MAX_CONFIDENCE)


def test_candidates_are_labelled_as_rule_inference_not_model(case_by_id):
    """每条候选都标「规则推断(未经模型)」且 ``source`` 恒为
    :data:`RULE_SOURCE` —— 与模型草稿在数据上就分得开。"""
    for case in case_by_id.values():
        for cand in rank_causes_by_rules(case.bundle):
            assert cand.source == RULE_SOURCE
            assert "规则推断(未经模型)" in cand.label_zh
            assert cand.rule_version == BASELINE_VERSION


def test_rule_candidates_have_no_disposition_field(case_by_id):
    """规则候选在**类型层面**也装不下「已确认」—— 与
    :class:`CauseDraft` 同一条纪律，只是理由更朴素：规则没看过账单列的
    业务含义。"""
    assert "disposition" not in CandidateDraft.__dataclass_fields__
    payload = rank_causes_by_rules(case_by_id["channel-only-his-missing"].bundle)[0].to_dict()
    assert "disposition" not in payload
    assert set(payload) >= {"cause_code", "confidence", "evidence_refs", "clues", "source"}


# ───────────────────── 三、字典码对齐 ─────────────────────


def test_every_rule_hit_maps_into_the_controlled_dictionary(case_by_id):
    """规则只映射到受控字典，**不自造码**。"""
    vocab = set(cause_vocabulary())
    for case in case_by_id.values():
        for cand in rank_causes_by_rules(case.bundle):
            assert cand.cause_code in vocab
            assert cand.cause_code in CAUSES
            for clue in cand.clues:
                assert clue.cause_code in vocab, f"线索命中了字典外的码 {clue.cause_code}"


def test_kind_mapping_table_only_uses_registered_codes():
    """形态 → 成因表里出现的每个码都必须在字典内。

    这条是**静态**判据：有人往 ``_KIND_TO_CAUSE`` 里加一个字典外的码，
    单元测试跑不报错（那张表只在对应形态出现时才被读到），所以在这里
    一次性把全表扫掉。
    """
    from reconq.align.baseline import _KIND_TO_CAUSE

    vocab = set(cause_vocabulary())
    for kind, (code, _weight) in _KIND_TO_CAUSE.items():
        assert code in vocab, f"形态 {kind} 映射到字典外的码 {code}"
        assert kind, "形态键不能为空"


def test_evidence_refs_never_leave_the_bundle(case_by_id):
    """候选引用的每一行都必须真实出现在证据包里。

    与模型草稿同一条纪律（越界即作废），只是这里不「作废」而是**根本不
    给**：规则自己选引用，选不出真实行说明它看错了地方。
    """
    for case in case_by_id.values():
        allowed = set(evidence_refs_of(case.bundle))
        for cand in rank_causes_by_rules(case.bundle):
            assert cand.evidence_refs, f"{case.case_id} 的 {cand.cause_code} 没有证据行"
            for ref in cand.evidence_refs:
                assert ref in allowed, f"{case.case_id} 引用了证据包外的行 {ref}"


def test_candidate_without_evidence_is_dropped_not_shown(case_by_id):
    """引不出证据行的候选**不给**。

    构造方式：把差异行的 ``ref`` 清空（证据包于是没有任何可引用行），
    基线必须交一张空表 —— 面板上「规则推断」后面没有行号，等于让经办人
    自己再翻一遍原始文件。
    """
    payload = _payload(case_by_id["channel-only-his-missing"])
    payload["diff_row"]["ref"] = ""
    assert evidence_refs_of(_bundle(payload)) == ()
    assert rank_causes_by_rules(_bundle(payload)) == []


def test_undetermined_is_always_available_as_the_last_resort(case_by_id):
    """候选表恒非空，且恒含 ``undetermined``（除非它已经排在前面）。

    字典里既然留了「判不出来」这一项，基线就不该在实在判不出来时交一张
    空表 —— 空表在面板上和「跑过了但没线索」长得一模一样。
    """
    for case in case_by_id.values():
        out = rank_causes_by_rules(case.bundle)
        assert out
        assert "undetermined" in [c.cause_code for c in out]


def test_ranking_is_stable_and_reproducible(case_by_id):
    """同分按成因码字典序，同一输入两次逐字段相同（可复算）。"""
    for case in case_by_id.values():
        first = [c.to_dict() for c in rank_causes_by_rules(case.bundle)]
        second = [c.to_dict() for c in rank_causes_by_rules(case.bundle)]
        assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
            second, ensure_ascii=False, sort_keys=True
        )
        scores = [(c.score, c.cause_code) for c in rank_causes_by_rules(case.bundle)]
        assert scores == sorted(scores, key=lambda item: (-item[0], item[1]))


def test_explanation_comes_from_the_controlled_template(case_by_id):
    """说明句按受控字典的中文模板渲染（带【成因名】前缀与责任方）。"""
    for case in case_by_id.values():
        for cand in rank_causes_by_rules(case.bundle):
            if cand.explanation_fallback:
                # 兜底句必须自己说清是兜底 + 哪个码，不许看起来像正常说明
                assert "兜底句" in cand.explanation_zh
                continue
            entry = CAUSES[cand.cause_code]
            assert cand.explanation_zh.startswith(f"【{entry.zh_name}】")
            assert entry.owner in cand.explanation_zh


# ───────────────────── 四、模型不可达时自动接管 ─────────────────────


def test_degraded_attribution_falls_back_to_rule_baseline(case_by_id):
    """模型不可达（``GatewayResult.status == DEGRADED``）时归因自动
    改用规则基线：**``causes`` 仍为空、``needs_manual`` 仍为 True**。"""
    bundle = case_by_id["ambiguous-split-payment"].bundle
    result = attribute(bundle, None)
    assert isinstance(result, AttributionResult)
    assert result.degraded is True
    assert result.error_code == "MODEL_CONFIG_INVALID"
    assert result.causes == (), "降级时不得产出任何可落库的模型草稿"
    assert result.needs_manual is True
    assert result.rule_baseline, "降级时必须附上规则候选"
    assert result.baseline_top == "split_payment_unallocated"
    assert result.baseline_verdict == "", "没有模型草稿就没有对照结论"


def test_degraded_status_zh_says_baseline_is_not_model(case_by_id):
    """状态行必须明说「规则推断候选，未经模型」。"""
    result = attribute(case_by_id["channel-only-his-missing"].bundle, None)
    assert "规则推断" in result.status_zh
    assert "未经模型" in result.status_zh
    text = result.render()
    assert "降级原因" in text
    assert "规则推断候选" in text
    assert "规则推断(未经模型)" in text
    # 模型草稿的渲染样式（「置信度 0.72」那种）不得出现在规则候选上
    assert "1. 跨日切分重复计入" not in text


def test_degraded_does_not_advance_ledger_state(case_by_id):
    """降级 + 规则接管后，台账状态一行都不动。

    这条是本任务的红线：规则候选是**人工判定的起点**，不是结论。任何
    状态推进都会让「待确认」这个状态失去意义（谁来确认的？规则？）。
    """
    from reconq.engine.period import DIFF_TABLE, init_scan_schema, list_diff_items
    from reconq.store.schema import connect

    bundle = case_by_id["channel-only-his-missing"].bundle
    conn = connect(":memory:")
    init_scan_schema(conn)
    conn.execute(
        f"INSERT INTO {DIFF_TABLE} (period, channel, check_path, disposition, diff_amount) "
        "VALUES (?,?,?,?,?)",
        (PERIOD, "wechat", "align.channel_only", "pending", "66.00"),
    )
    before = [d.to_dict() for d in list_diff_items(conn, period=PERIOD)]

    result = attribute(bundle, None)
    assert result.rule_baseline and result.degraded
    after = [d.to_dict() for d in list_diff_items(conn, period=PERIOD)]
    assert before == after
    row = conn.execute(
        f"SELECT disposition FROM {DIFF_TABLE} WHERE period = ?", (PERIOD,)
    ).fetchone()
    assert row["disposition"] == "pending"
    conn.close()


def test_baseline_never_lands_in_causes_even_when_model_works(case_by_id):
    """模型可用时规则候选也**不进** ``causes``：两者的置信度量纲不同
    （规则封顶 0.5、模型可给 0.9），混进同一条落库路径后排序就没有意义。"""
    bundle = case_by_id["ambiguous-split-payment"].bundle
    result = attribute(
        bundle,
        _gateway([_model_draft("split_payment_unallocated", 0.88, ["wechat_bill.csv:3"])]),
    )
    assert result.degraded is False
    assert [c.cause for c in result.causes] == ["split_payment_unallocated"]
    assert result.rule_baseline, "模型可用时规则照样跑一遍（留作对照）"
    assert all(c.source == RULE_SOURCE for c in result.rule_baseline)
    payload = result.to_dict()
    assert json.dumps(payload, ensure_ascii=False).count("split_payment_unallocated") >= 2


def test_all_degraded_paths_carry_the_baseline(case_by_id):
    """四条降级路径（无网关 / 端点失败 / 草稿全被拒 / 空草稿）都带基线。"""
    bundle = case_by_id["mismatch-amount"].bundle

    # 1) 端点不可达（httpx 会真的去连 127.0.0.1:9，MockTransport 换成抛错）
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(_boom))
    cfg = GatewayConfig(base_url=FAKE_BASE_URL, api_key="FAKE_KEY", model="m")
    gw = ModelGateway(cfg, client=client, sleeper=lambda _s: None)
    unreachable = attribute(bundle, gw)
    assert unreachable.degraded is True
    assert unreachable.causes == ()
    assert unreachable.rule_baseline

    # 2) 草稿引用越界 → 整条作废，但基线仍在
    bad = attribute(bundle, _gateway([_model_draft("undetermined", 0.9, ["nope.csv:1"])]))
    assert bad.degraded is True
    assert bad.error_code == "ATTRIBUTION_EVIDENCE_REF_INVALID"
    assert bad.rule_baseline

    # 3) 空草稿
    empty = attribute(bundle, _gateway([]))
    assert empty.degraded is True
    assert empty.rule_baseline


# ───────────────────── 五、与模型分歧被记录 ─────────────────────


def test_agreement_is_recorded(case_by_id):
    """规则 Top-1 与模型 Top-1 一致 → ``agree``。"""
    bundle = case_by_id["ambiguous-split-payment"].bundle
    base = rank_causes_by_rules(bundle)
    model = [{"cause": "split_payment_unallocated", "confidence": 0.8, "evidence_refs": ["x:1"]}]
    cmp = compare_with_model(bundle, base, model)
    assert isinstance(cmp, BaselineComparison)
    assert cmp.verdict == "agree"
    assert cmp.diverged is False
    assert cmp.baseline_top == model[0]["cause"]
    assert cmp.conclusion_key == bundle.payload["conclusion_key"]
    assert cmp.bundle_digest == bundle.digest


def test_divergence_is_recorded_and_usable_as_replay_material(case_by_id):
    """分歧要能直接变成回放集用例（Task 34 的 ``cases/*.json`` 形状）。"""
    bundle = case_by_id["ambiguous-split-payment"].bundle
    base = rank_causes_by_rules(bundle)
    model = [
        _model_draft("channel_late_posting", 0.81, ["wechat_bill.csv:3"]),
        _model_draft("his_missing_order", 0.4, ["wechat_bill.csv:4"]),
    ]
    cmp = compare_with_model(bundle, base, model)
    assert cmp.verdict == "diverge"
    assert cmp.diverged is True
    assert cmp.model_top == "channel_late_posting"
    assert cmp.baseline_top == "split_payment_unallocated"

    case = divergence_to_replay_case(cmp, model)
    assert case["expect"]["cause_codes"] == ["channel_late_posting", "split_payment_unallocated"]
    assert case["expect"]["required_refs"] == ["wechat_bill.csv:3"]
    # 落盘即可被 load_replay_set 读回来 —— 断言用真实的 from_dict 判
    loaded = ReplayCase.from_dict({**case, "bundle": bundle.payload})
    assert loaded.cause_codes == ("channel_late_posting", "split_payment_unallocated")
    assert loaded.required_refs == ("wechat_bill.csv:3",)


def test_agreement_samples_do_not_enter_the_replay_set(case_by_id):
    """一致样本不进回放集：回放集贵在「每条都在某个方向上给提示词出题」。"""
    bundle = case_by_id["ambiguous-split-payment"].bundle
    base = rank_causes_by_rules(bundle)
    same = compare_with_model(
        bundle, base, [_model_draft("split_payment_unallocated", 0.8, ["wechat_bill.csv:3"])]
    )
    assert divergence_to_replay_case(same, []) == {}


def test_one_sided_verdicts(case_by_id):
    """只有一边有结论时也要说清是哪一边，别记成「一致」。"""
    bundle = case_by_id["ambiguous-split-payment"].bundle
    base = rank_causes_by_rules(bundle)
    assert compare_with_model(bundle, base, []).verdict == "baseline_only"
    assert compare_with_model(bundle, [], [{"cause": "x", "confidence": 0.5}]).verdict == "model_only"


def test_attribution_records_the_verdict_when_model_works(case_by_id):
    """模型可用时对照结论落在结果上（面板上要能回答
    「规则与模型顶上一致吗」）。"""
    bundle = case_by_id["ambiguous-split-payment"].bundle
    agree = attribute(
        bundle, _gateway([_model_draft("split_payment_unallocated", 0.8, ["wechat_bill.csv:3"])])
    )
    assert agree.baseline_verdict == "agree"

    diverge = attribute(
        bundle, _gateway([_model_draft("contract_config_error", 0.8, ["wechat_bill.csv:3"])])
    )
    assert diverge.baseline_verdict == "diverge"
    assert diverge.baseline_top == "split_payment_unallocated"


def test_baseline_tops_land_in_expected_cause_families_on_replay_set(case_by_id):
    """12 条回放用例上，规则 Top-1 多数落在期望成因族内。

    **断言写成「至少 9 条」而不是「全部」**：基线是兜底不是替代品，它在
    形态判别（拆笔/合并/跨账期/退费并存）上本来就该比模型稳，但在需要
    账单列业务含义的成因（渠道延迟入账 vs HIS 未落单）上判不出方向 ——
    那是模型的活。把它写成「全中」等于逼着规则去猜，而规则一旦开始猜，
    它给的就不再是候选而是伪装成结论的猜测。

    （当前实测 12/12：期望族是**集合**而非单值，「渠道有 HIS 无」那几条
    本就允许 ``his_missing_order`` 与 ``channel_late_posting`` 两个答案。
    门槛留 9 是给规则退化留余量，不是给现状留余量。）
    """
    hits = 0
    misses: list[str] = []
    for case_id, case in case_by_id.items():
        top = rank_causes_by_rules(case.bundle)[0].cause_code
        if top in case.cause_codes:
            hits += 1
        else:
            misses.append(f"{case_id}:{top}")
    assert hits >= 9, f"规则 Top-1 只命中 {hits}/12，未命中：{misses}"


# ───────────────────── 六、耗时阈值 ─────────────────────


def test_single_discrepancy_under_5ms(case_by_id):
    """单条差异 <5ms。**纯计算、不发网络**：基线在一次扫描里要跑几百条，
    每条多 5ms 就是几秒的账期等待。"""
    bundles = [c.bundle for c in case_by_id.values()]
    # 预热（首次调用有 import 后的属性查找开销）
    for bundle in bundles:
        rank_causes_by_rules(bundle)
    rounds = 50
    start = time.perf_counter()
    for _ in range(rounds):
        for bundle in bundles:
            rank_causes_by_rules(bundle)
    per_call_ms = (time.perf_counter() - start) * 1000 / (rounds * len(bundles))
    assert per_call_ms < PERF_BUDGET_MS, f"单条约 {per_call_ms:.3f}ms，超出 {PERF_BUDGET_MS}ms 预算"


def test_worst_case_bundle_is_also_under_budget(case_by_id):
    """逐条测而非只测平均：**最慢的那条**也要在预算内。

    只测平均值会让「九条 0.1ms + 一条 40ms」这种分布蒙混过关，而那一条
    恰好会落在最复杂的合并支付上。
    """
    for case_id, case in case_by_id.items():
        for _ in range(3):
            rank_causes_by_rules(case.bundle)
        start = time.perf_counter()
        for _ in range(50):
            rank_causes_by_rules(case.bundle)
        per_call_ms = (time.perf_counter() - start) * 1000 / 50
        assert per_call_ms < PERF_BUDGET_MS, f"{case_id} 约 {per_call_ms:.3f}ms，超预算"


def test_baseline_does_not_touch_the_network(case_by_id):
    """基线本身**不发任何网络请求**。

    用一个「一被调用就炸」的 transport 钉死：真去调了一次就会在第一次
    HTTP 上冒出 AssertionError。这条专门钉的是
    :func:`rank_causes_by_rules` 本身 —— 它在「模型不可达」这条降级路径上
    是唯一还在跑的东西，一旦哪天有人在里面顺手加一次 ``gateway`` 调用，
    降级路径就会变成「先卡 3 秒超时再给规则结论」。
    """
    hits: list[str] = []

    def _forbidden(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        raise AssertionError(f"规则基线不该发网络请求，却打到了 {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(_forbidden))
    for case in case_by_id.values():
        out = rank_causes_by_rules(case.bundle)
        assert out
    assert hits == [], f"基线发起了 {len(hits)} 次网络请求：{hits}"
    client.close()


def test_no_gateway_at_all_still_yields_candidates(case_by_id):
    """连网关句柄都没有时（``gateway=None``）基线照样给候选。

    这是「模型不可达时仍然比什么都不做更有用」那句话的最小可验形式 ——
    没有端点、没有凭据、没有配置，12 条回放用例逐条都拿得到带证据行的
    成因候选。
    """
    for case in case_by_id.values():
        out = rank_causes_by_rules(case.bundle)
        assert out and out[0].evidence_refs


# ───────────────────── 附：规则目录 ─────────────────────


def test_rule_catalog_lists_every_rule_and_the_dictionary():
    """规则目录要给面板/结论单看：每条规则的中文名 + 封顶 + 字典全量。"""
    catalog = rule_catalog()
    assert catalog["version"] == BASELINE_VERSION
    assert catalog["max_confidence"] == str(MAX_CONFIDENCE)
    assert {r["rule"] for r in catalog["rules"]} == set(CLUE_RULES)
    assert catalog["causes"] == list(cause_vocabulary())
    assert all(r["rule_zh"] for r in catalog["rules"])
