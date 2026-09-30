"""跨账期与跨渠道连坐追查（Task 24）。

五类规定断言逐条可核：

1. **跨账期两期同单号成链** —— 同一单号在 W35/W36/W37 出现时串成一条链，
   节点逐条带渠道、账期、金额、时间、结算方式，且可回查到源文件行号；
2. **跨渠道重复计入** —— 同一渠道单号被两路渠道各认领一次即报出，
   列出两期（或两路）证据行，**不择一认领**；
3. **金额一致通过** —— 退款为负、合并支付为分摊和两条关系成立时不出结论；
4. **金额不一致给差额** —— 两条关系被破坏时分别报 `amount_inconsistent`
   并给出**实测差额**（不是「金额不符」四个字）；
5. **链导出含全部节点** —— ``reconq trace`` 与 ``chain.render()`` 把链上
   每一个节点都列出来，窗口外的出现单独提示而不是悄悄丢掉。

外加几条**必须成立的不变量**（本任务最容易被糊弄过去的地方）：

* 结论归属是全局的：``channel`` 恒为 ``*``，两路渠道名写进 ``message``。
  记在任一路渠道名下等于暗示「是这一路多记了一笔」，而实际可能两路
  导出方都有份。
* 只认 ``matched``：``ambiguous`` / ``weak_pending`` / ``cross_period``
  本质是**拒绝归并**，把它们算成认领会让连坐在一条「系统都没认下的
  关系」上判重复计入。
* 窗口只约束结论、不删证据：窗口外的出现进 ``outside_window``。
  悄悄丢掉窗口外的行，「这笔在 W33 也有一份」这条线索就永远问不出来。
* ``merged payment`` 只在「恰好一个渠道节点 + 期内无红冲」时判：多渠道
  节点之间存在分摊关系，判据需要系统侧拿不到的上下文。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from reconq.align.matcher import AlignRecord, AlignResult, MatchOutcome, MatchStatus, align_all
from reconq.align.trace import (
    DEFAULT_WINDOW_PERIODS,
    GLOBAL_CHANNEL,
    Claim,
    EntityIndex,
    TraceKind,
    TraceNode,
    build_entity_index,
    chain_to_diff_items,
    claims_from_align_result,
    claims_from_nodes,
    load_entity_nodes,
    node_from_record,
    period_window_of,
    render_chain,
    shift_period,
    trace_entity,
    trace_from_records,
)
from reconq.errors import ReconqError
from reconq.sources.timezone import period_window, to_session_tz

PERIOD = "2026-W36"
PREV = "2026-W35"
NEXT = "2026-W37"
FAR = "2026-W33"
TZ = to_session_tz(None)
WINDOW = period_window(PERIOD, TZ)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ───────────── 构造工具 ─────────────


def _node(
    order_no: str,
    amount: str,
    *,
    period: str = PERIOD,
    channel: str = "wechat",
    side: str = "channel",
    row: int = 1,
    when: str = "2026-09-02T10:00:00+08:00",
    settle: str = "微信",
    refund: bool = False,
    dept: str = "门诊收费处",
) -> TraceNode:
    return TraceNode(
        period=period,
        channel=channel,
        order_no=order_no,
        amount=Decimal(amount),
        side=side,
        trade_time=when,
        settle_type=settle,
        department=dept,
        source_file=f"{channel}_{period}.csv",
        source_row_no=row,
        his_order_no=order_no,
        is_refund=refund,
    )


def _claim(
    order_no: str,
    *,
    period: str = PERIOD,
    channel: str = "wechat",
    row: int = 1,
    amount: str = "128.00",
    his_order_no: str = "",
) -> Claim:
    return Claim(
        period=period,
        channel=channel,
        order_no=order_no,
        his_order_no=his_order_no or order_no,
        source_file=f"{channel}_{period}.csv",
        source_row_no=row,
        amount=Decimal(amount),
    )


# ───────────── 一、跨账期两期同单号成链 ─────────────


def test_same_order_no_across_three_periods_forms_one_chain():
    """同一单号在 W35/W36/W37 各出现一次 → 一条链，三个节点齐全。"""
    nodes = [
        _node("EX-CHAIN-0001", "128.00", period=PREV, row=5),
        _node("EX-CHAIN-0001", "128.00", period=PERIOD, row=6),
        _node("EX-CHAIN-0001", "128.00", period=NEXT, row=7),
    ]
    chain = trace_entity("EX-CHAIN-0001", nodes=nodes, focus_period=PERIOD, window_periods=1)

    assert chain.periods == [PREV, PERIOD, NEXT]
    assert len(chain.nodes) == 3
    # 节点带齐财务科回查要的五样：渠道、账期、金额、时间、结算方式 + 行号
    for node in chain.nodes:
        assert node.channel and node.period and node.trade_time and node.settle_type
        assert node.source_file.endswith(".csv")
        assert node.source_row_no > 0
    # 相邻期成对：W35-W36 与 W36-W37 = 2 对
    assert chain.adjacent_period_hits == 2
    assert chain.occurrences == {PREV: 1, PERIOD: 1, NEXT: 1}


def test_chain_nodes_are_ordered_and_each_carries_a_source_position():
    """链上节点按 (账期, 时间, 行号) 排序，且每条都能回到源文件行号。"""
    nodes = [
        _node("EX-ORDER-0001", "50.00", period=NEXT, row=3, when="2026-09-09T09:00:00+08:00"),
        _node("EX-ORDER-0001", "50.00", period=PREV, row=9, when="2026-09-01T09:00:00+08:00"),
        _node("EX-ORDER-0001", "50.00", period=PERIOD, row=4, when="2026-09-05T09:00:00+08:00"),
    ]
    chain = trace_entity("EX-ORDER-0001", nodes=nodes, focus_period=PERIOD, window_periods=1)
    assert [n.period for n in chain.nodes] == [PREV, PERIOD, NEXT]
    assert [n.source_row_no for n in chain.nodes] == [9, 4, 3]
    for node in chain.nodes:
        assert node.ref == f"{node.source_file}:{node.source_row_no}"


def test_window_only_filters_conclusions_never_silently_drops_evidence():
    """窗口外的出现进 ``outside_window``，不被悄悄丢掉。"""
    nodes = [
        _node("EX-WIDE-0001", "10.00", period=FAR, row=2),
        _node("EX-WIDE-0001", "10.00", period=PERIOD, row=3),
    ]
    chain = trace_entity("EX-WIDE-0001", nodes=nodes, focus_period=PERIOD, window_periods=0)
    # 窗口半径 0 = 只看 center 自己
    assert [n.period for n in chain.nodes] == [PERIOD]
    # 但窗口外那一期必须被点名，否则「W33 也有一份」这条线索永远问不出来
    assert chain.outside_window == [FAR]
    assert FAR in render_chain(chain)


def test_non_adjacent_periods_do_not_count_as_adjacent_hits():
    """隔了三期还在同一个单号上是重名/单号复用，不是落点差异。"""
    nodes = [
        _node("EX-GAP-0001", "10.00", period=FAR, row=2),
        _node("EX-GAP-0001", "10.00", period=PERIOD, row=3),
    ]
    chain = trace_entity("EX-GAP-0001", nodes=nodes, focus_period=PERIOD, window_periods=4)
    assert chain.periods == [FAR, PERIOD]
    assert chain.adjacent_period_hits == 0


# ───────────── 二、跨账期 / 跨渠道重复计入 ─────────────


def test_same_his_record_claimed_in_two_periods_is_flagged():
    """同一笔 HIS 收费被两个账期各认领一次 → duplicate_across_period。"""
    claims = [
        _claim("EX-DUP-0001", period=PREV, channel="wechat", row=5),
        _claim("EX-DUP-0001", period=PERIOD, channel="wechat", row=6),
    ]
    chain = trace_entity("EX-DUP-0001", nodes=[], claims=claims, focus_period=PERIOD, window_periods=1)
    kinds = [c.kind for c in chain.conclusions]
    assert kinds == [TraceKind.DUPLICATE_ACROSS_PERIOD.value]

    conclusion = chain.conclusions[0]
    assert conclusion.periods == [PREV, PERIOD]
    # 两期各一条证据行，逐条可回查
    assert conclusion.evidence_total == 2
    assert any(PREV in e and ":5" in e for e in conclusion.evidence)
    assert any(PERIOD in e and ":6" in e for e in conclusion.evidence)
    # 结论落在**最早**那一期：财务科按时间顺序处理，先撤哪一笔要有落点
    assert conclusion.period == PREV
    assert "记了 2 次" in conclusion.detail


def test_duplicate_across_period_dedups_evidence_within_one_period():
    """同一期内被两行认领是跨渠道的事，混进来会让「跨了哪几期」被淹没。"""
    claims = [
        _claim("EX-DUP-0002", period=PREV, channel="wechat", row=5),
        _claim("EX-DUP-0002", period=PREV, channel="wechat", row=6),
        _claim("EX-DUP-0002", period=PERIOD, channel="wechat", row=7),
    ]
    chain = trace_entity("EX-DUP-0002", nodes=[], claims=claims, focus_period=PERIOD, window_periods=1)
    cross = [c for c in chain.conclusions if c.kind == TraceKind.DUPLICATE_ACROSS_PERIOD.value]
    assert len(cross) == 1
    # 每期只留一条：2 期 → 2 条证据，不是 3 条
    assert cross[0].evidence_total == 2
    assert cross[0].periods == [PREV, PERIOD]


def test_same_order_claimed_by_two_channels_is_flagged():
    """同一渠道单号被两路渠道各认领一次 → duplicate_across_channel。"""
    claims = [
        _claim("EX-CH-0001", period=PERIOD, channel="wechat", row=5),
        _claim("EX-CH-0001", period=PERIOD, channel="self_service", row=3),
    ]
    chain = trace_entity("EX-CH-0001", nodes=[], claims=claims, focus_period=PERIOD)
    kinds = [c.kind for c in chain.conclusions]
    assert kinds == [TraceKind.DUPLICATE_ACROSS_CHANNEL.value]

    conclusion = chain.conclusions[0]
    assert conclusion.channels == ["self_service", "wechat"]
    assert conclusion.evidence_total == 2
    # 两路渠道名都要出现在说明里（结论本身挂在全局，不摊给任一路）
    assert "self_service" in conclusion.detail and "wechat" in conclusion.detail


def test_cross_period_and_cross_channel_produce_two_separate_conclusions():
    """同一笔既跨了期又跨了渠道 → 出两条，不揉成一条。

    揉成一条的后果是没法分别确认、也没法分别冲销。
    """
    claims = [
        _claim("EX-BOTH-0001", period=PREV, channel="wechat", row=5),
        _claim("EX-BOTH-0001", period=PERIOD, channel="wechat", row=6),
        _claim("EX-BOTH-0001", period=PERIOD, channel="alipay", row=2),
    ]
    chain = trace_entity("EX-BOTH-0001", nodes=[], claims=claims, focus_period=PERIOD, window_periods=1)
    kinds = {c.kind for c in chain.conclusions}
    assert TraceKind.DUPLICATE_ACROSS_PERIOD.value in kinds
    assert TraceKind.DUPLICATE_ACROSS_CHANNEL.value in kinds


def test_duplicate_conclusions_are_global_not_attributed_to_one_channel():
    """连坐结论的 channel 恒为 *，两路渠道名写进 message。"""
    claims = [
        _claim("EX-GLOBAL-0001", period=PERIOD, channel="wechat", row=5),
        _claim("EX-GLOBAL-0001", period=PERIOD, channel="alipay", row=6),
    ]
    chain = trace_entity("EX-GLOBAL-0001", nodes=[], claims=claims, focus_period=PERIOD)
    items = chain_to_diff_items(chain, run_id="run-T")
    assert len(items) == 1
    item = items[0]
    assert item.channel == GLOBAL_CHANNEL
    assert item.needs_attribution is True
    assert item.disposition == "待确认"
    assert item.check_path.startswith("trace.")
    assert item.check_name == "跨渠道重复计入"
    # 两路渠道名都在 message 里 —— 不能因为 channel 记 * 就丢掉「是哪两路」
    assert "alipay" in item.message and "wechat" in item.message


def test_only_matched_outcomes_count_as_claims():
    """只认 matched：歧义 / 弱匹配 / 跨账期都是「拒绝归并」，不是认领。"""
    his = AlignRecord(
        channel="his", period=PERIOD, order_no="EX-CLM-0001", amount=Decimal("128.00"),
        source_file="his.csv", source_row_no=2, ch_id=1,
        trade_time=datetime(2026, 9, 2, 10, 0, tzinfo=TZ),
    )
    ch = AlignRecord(
        channel="wechat", period=PERIOD, order_no="EX-CLM-0001", amount=Decimal("128.00"),
        source_file="wx.csv", source_row_no=3, ch_id=2,
        trade_time=datetime(2026, 9, 2, 10, 1, tzinfo=TZ),
    )
    result = align_all({"wechat": [ch]}, [his], period=PERIOD, window=WINDOW)
    claims = claims_from_align_result(result.channels["wechat"])
    assert len(claims) == 1
    assert claims[0].channel == "wechat"
    assert claims[0].his_order_no == "EX-CLM-0001"


def test_refused_merge_outcomes_produce_no_claims():
    """歧义与弱匹配不产认领 —— 否则连坐会在「系统都没认下的关系」上判重复。"""
    his = [
        AlignRecord(channel="his", period=PERIOD, order_no="EX-AMB-0001", amount=Decimal("60.00"),
                    source_file="his.csv", source_row_no=2, ch_id=1,
                    trade_time=datetime(2026, 9, 2, 10, 0, tzinfo=TZ)),
        AlignRecord(channel="his", period=PERIOD, order_no="EX-AMB-0001", amount=Decimal("68.00"),
                    source_file="his.csv", source_row_no=3, ch_id=2,
                    trade_time=datetime(2026, 9, 2, 10, 0, tzinfo=TZ)),
    ]
    ch = AlignRecord(channel="wechat", period=PERIOD, order_no="EX-AMB-0001", amount=Decimal("128.00"),
                     source_file="wx.csv", source_row_no=4, ch_id=3,
                     trade_time=datetime(2026, 9, 2, 10, 1, tzinfo=TZ))
    result = align_all({"wechat": [ch]}, his, period=PERIOD, window=WINDOW)
    outcome = result.channels["wechat"].outcomes[0]
    assert outcome.status == MatchStatus.AMBIGUOUS.value
    assert claims_from_align_result(result.channels["wechat"]) == []


# ───────────── 三、金额一致通过 ─────────────


def test_refund_chain_with_correct_negative_amount_passes():
    """原单 +128、跨期红冲 -128 → 退款为负的关系成立，无结论。"""
    nodes = [
        _node("EX-REF-OK", "128.00", period=PREV, row=5, when="2026-09-01T10:00:00+08:00"),
        _node("EX-REF-OK", "-128.00", period=PERIOD, row=6, refund=True,
              when="2026-09-03T10:00:00+08:00"),
    ]
    chain = trace_entity("EX-REF-OK", nodes=nodes, focus_period=PERIOD, window_periods=1)
    assert chain.conclusions == []


def test_merged_payment_sum_holds_passes():
    """渠道一笔 128 = HIS 三笔之和 → 分摊和成立，无结论。"""
    nodes = [
        _node("EX-MERGE-OK", "128.00", period=PERIOD, channel="wechat", side="channel", row=5),
        _node("EX-MERGE-OK", "50.00", period=PERIOD, channel="his", side="his", row=2, settle="现金"),
        _node("EX-MERGE-OK", "30.00", period=PERIOD, channel="his", side="his", row=3, settle="现金"),
        _node("EX-MERGE-OK", "48.00", period=PERIOD, channel="his", side="his", row=4, settle="现金"),
    ]
    chain = trace_entity("EX-MERGE-OK", nodes=nodes, focus_period=PERIOD)
    assert chain.conclusions == []


def test_tolerance_boundary_does_not_produce_a_phantom_diff():
    """差 0.01 视为抹零不算不一致（Task 21 同一套容差口径）。"""
    nodes = [
        _node("EX-TOL", "128.00", period=PERIOD, channel="wechat", side="channel", row=5),
        _node("EX-TOL", "64.00", period=PERIOD, channel="his", side="his", row=2),
        _node("EX-TOL", "63.99", period=PERIOD, channel="his", side="his", row=3),
    ]
    chain = trace_entity("EX-TOL", nodes=nodes, focus_period=PERIOD)
    assert chain.conclusions == []


# ───────────── 四、金额不一致给差额 ─────────────


def test_refund_not_negative_reports_the_actual_diff():
    """红冲金额不是原单的相反数 → amount_inconsistent + 实测差额。"""
    nodes = [
        _node("EX-REF-BAD", "128.00", period=PREV, row=5, when="2026-09-01T10:00:00+08:00"),
        _node("EX-REF-BAD", "-100.00", period=PERIOD, row=6, refund=True,
              when="2026-09-03T10:00:00+08:00"),
    ]
    chain = trace_entity("EX-REF-BAD", nodes=nodes, focus_period=PERIOD, window_periods=1)
    assert [c.kind for c in chain.conclusions] == [TraceKind.AMOUNT_INCONSISTENT.value]
    conclusion = chain.conclusions[0]
    # 应为 -128，实际 -100 → 差 28.00
    assert conclusion.diff_amount == "28.00"
    assert "-128" in conclusion.detail
    assert conclusion.evidence_total == 2
    assert conclusion.periods == [PREV, PERIOD]


def test_merged_payment_sum_mismatch_reports_the_actual_diff():
    """渠道 128 = HIS 50+30+40（120）→ 差 8.00。"""
    nodes = [
        _node("EX-MERGE-BAD", "128.00", period=PERIOD, channel="wechat", side="channel", row=5),
        _node("EX-MERGE-BAD", "50.00", period=PERIOD, channel="his", side="his", row=2, settle="现金"),
        _node("EX-MERGE-BAD", "30.00", period=PERIOD, channel="his", side="his", row=3, settle="现金"),
        _node("EX-MERGE-BAD", "40.00", period=PERIOD, channel="his", side="his", row=4, settle="现金"),
    ]
    chain = trace_entity("EX-MERGE-BAD", nodes=nodes, focus_period=PERIOD)
    assert [c.kind for c in chain.conclusions] == [TraceKind.AMOUNT_INCONSISTENT.value]
    assert chain.conclusions[0].diff_amount == "8.00"
    assert "分摊和" in chain.conclusions[0].detail
    assert chain.conclusions[0].evidence_total == 4


def test_same_period_refund_pair_is_not_judged_as_inconsistent():
    """同期原单 + 红冲并存是正常形态（Task 21 的 refund_and_original），
    跨期退款关系核对不适用；分摊和也不适用。"""
    nodes = [
        _node("EX-SAME-PER", "128.00", period=PERIOD, channel="wechat", side="channel", row=5),
        _node("EX-SAME-PER", "128.00", period=PERIOD, channel="his", side="his", row=2, settle="现金"),
        _node("EX-SAME-PER", "-128.00", period=PERIOD, channel="his", side="his", row=3,
              settle="现金", refund=True),
    ]
    chain = trace_entity("EX-SAME-PER", nodes=nodes, focus_period=PERIOD)
    assert chain.conclusions == []


def test_multi_channel_nodes_are_not_judged_as_merged_payment():
    """两个渠道节点之间存在分摊关系，判据需要「谁分摊给谁」这种系统侧拿不到的
    上下文 —— 一律不判，而不是猜一个。"""
    nodes = [
        _node("EX-MULTI", "128.00", period=PERIOD, channel="wechat", side="channel", row=5),
        _node("EX-MULTI", "28.00", period=PERIOD, channel="alipay", side="channel", row=3),
        _node("EX-MULTI", "100.00", period=PERIOD, channel="his", side="his", row=2, settle="现金"),
        _node("EX-MULTI", "56.00", period=PERIOD, channel="his", side="his", row=3, settle="现金"),
    ]
    chain = trace_entity("EX-MULTI", nodes=nodes, focus_period=PERIOD)
    assert [c.kind for c in chain.conclusions] == []


def test_refund_without_an_earlier_original_in_window_is_not_flagged():
    """窗口里只有红冲、没有更早的原单 → 报成「金额不成立」会给台账塞一条
    查不出所以然的差异。只记日志，不出结论。"""
    nodes = [
        _node("EX-NO-BASE", "-128.00", period=PERIOD, row=5, refund=True),
    ]
    chain = trace_entity("EX-NO-BASE", nodes=nodes, focus_period=PERIOD, window_periods=1)
    assert chain.conclusions == []


# ───────────── 五、链导出含全部节点 ─────────────


def test_render_lists_every_node_and_every_conclusion():
    """链导出必须含**全部**节点与结论，不能只给摘要。"""
    nodes = [
        _node("EX-EXPORT-0001", "128.00", period=PREV, row=5),
        _node("EX-EXPORT-0001", "128.00", period=PERIOD, row=6),
    ]
    claims = [
        _claim("EX-EXPORT-0001", period=PREV, channel="wechat", row=5),
        _claim("EX-EXPORT-0001", period=PERIOD, channel="wechat", row=6),
    ]
    chain = trace_entity("EX-EXPORT-0001", nodes=nodes, claims=claims,
                         focus_period=PERIOD, window_periods=1)
    text = render_chain(chain)
    for node in chain.nodes:
        assert node.source_file in text
        assert f":{node.source_row_no}" in text
    for conclusion in chain.conclusions:
        assert conclusion.kind_zh in text
    # 相邻期成对数与合计也写进去，财务科不用另算
    assert "相邻期成对 1 对" in text
    assert "256.00" in text


def test_render_of_empty_chain_says_so_instead_of_pretending():
    """窗口内没有任何记录时直说，不要输出一个看起来很完整的空结论。"""
    chain = trace_entity("EX-NONE-0001", nodes=[], focus_period=PERIOD)
    text = render_chain(chain)
    assert "窗口内没有任何记录" in text
    assert chain.needs_attribution is False


def test_json_round_trip_keeps_every_node_and_conclusion():
    """JSON 输出（面板与结论单附录消费）节点与结论一条不少。"""
    nodes = [
        _node("EX-JSON-0001", "128.00", period=PREV, row=5),
        _node("EX-JSON-0001", "128.00", period=PERIOD, row=6),
    ]
    claims = [
        _claim("EX-JSON-0001", period=PREV, channel="wechat", row=5),
        _claim("EX-JSON-0001", period=PERIOD, channel="wechat", row=6),
    ]
    chain = trace_entity("EX-JSON-0001", nodes=nodes, claims=claims,
                         focus_period=PERIOD, window_periods=1)
    payload = json.loads(json.dumps(chain.to_dict(), ensure_ascii=False))
    assert len(payload["nodes"]) == 2
    assert len(payload["claims"]) == 2
    assert payload["conclusions"][0]["kind"] == TraceKind.DUPLICATE_ACROSS_PERIOD.value
    assert payload["conclusions"][0]["kind_zh"] == "跨账期重复计入"
    assert payload["adjacent_period_hits"] == 1
    assert payload["needs_attribution"] is True


# ───────────── 六、账期算术与索引不变量 ─────────────


def test_period_shift_crosses_year_boundary_correctly():
    """跨年周边界必须由 ISO 周历算，手工推必错。"""
    assert shift_period("2026-W36", -1) == "2026-W35"
    assert shift_period("2026-W36", 1) == "2026-W37"
    # 2026 年第 1 周的前一周属于 2025 年
    assert shift_period("2026-W01", -1) == "2025-W52"
    assert shift_period("2025-W52", 1) == "2026-W01"
    # 2026 年确有第 53 周
    assert shift_period("2026-W52", 1) == "2026-W53"


def test_window_expansion_is_centered_and_length_matches_radius():
    window = period_window_of("2026-W36", center="2026-W36", window_periods=2)
    assert window == ["2026-W34", "2026-W35", "2026-W36", "2026-W37", "2026-W38"]
    assert period_window_of("2026-W36", center="2026-W36", window_periods=0) == ["2026-W36"]


def test_negative_window_is_an_explicit_error_not_a_silent_zero():
    """负窗口静默当成 0 的话，用户以为看了 5 期、实际只看 1 期。"""
    with pytest.raises(ReconqError) as exc:
        period_window_of("2026-W36", center="2026-W36", window_periods=-1)
    assert exc.value.code == "TRACE_WINDOW_INVALID"


def test_invalid_period_text_in_scope_is_rejected():
    with pytest.raises(ReconqError) as exc:
        period_window_of("第36周", center="第36周", window_periods=1)
    assert exc.value.code == "PERIOD_INVALID"


def test_index_normalizes_order_no_and_never_indexes_empty():
    """索引键与 Task 22 同一份归一口径；空单号不进索引。"""
    index = EntityIndex()
    index.add(_node("ＥＸ-ＩＤＸ-0001", "10.00", row=2))
    index.add(_node(" EX-idx-0001 ", "10.00", row=3))
    index.add(_node("   ", "10.00", row=4))  # 纯空白单号
    assert index.key_total() == 1
    assert len(index.nodes_for("ex-idx-0001")) == 2
    assert index.nodes_for("  ") == []
    assert index.nodes_for("查无此单") == []


def test_index_lookup_is_equivalent_to_a_linear_scan_over_all_records():
    """索引命中集合 == 在全集中按归一键筛出的集合。"""
    nodes = [
        _node("EX-A-0001", "10.00", row=2),
        _node("EX-A-0001", "20.00", period=PREV, row=3),
        _node("EX-B-0001", "30.00", row=4),
    ]
    index = build_entity_index(nodes)
    indexed = index.nodes_for("ex-a-0001")
    expected = [n for n in nodes if n.order_no.lower() == "ex-a-0001"]
    assert [id(n) for n in indexed] == [id(n) for n in expected]


def test_node_from_record_uses_net_amount_and_does_not_double_deduct_refund():
    """node_from_record 收的就是净额；两条腿都扣退款会让退款变成正数、
    金额关系核对会以「总额不成立」报出，而真因是退款被扣了两次。"""
    record = AlignRecord(
        channel="wechat", period=PERIOD, order_no="EX-NET-0001",
        amount=Decimal("214.00"), refund=Decimal("86.00"),
        source_file="wx.csv", source_row_no=5, ch_id=1,
    )
    node = node_from_record(record)
    assert node.amount == Decimal("128.00")
    assert node.net_amount == Decimal("128.00")
    assert node.side == "channel"
    assert node.ref == "wx.csv:5"


def test_node_from_record_marks_his_side_and_refund_flag():
    his = AlignRecord(
        channel="his", period=PERIOD, order_no="EX-HIS-0001", amount=Decimal("-42.00"),
        trade_type="退费", source_file="his.csv", source_row_no=3, ch_id=9,
    )
    node = node_from_record(his)
    assert node.side == "his"
    assert node.is_refund is True


def test_claims_from_nodes_reconstructs_a_claim_only_when_his_side_exists():
    """由落库行反推认领：渠道行带着 his_order_no 且链上有 HIS 侧记录才算。"""
    nodes = [
        _node("EX-CLN-0001", "128.00", channel="wechat", side="channel", row=5),
        _node("EX-CLN-0001", "128.00", channel="his", side="his", row=2),
    ]
    claims = claims_from_nodes(nodes)
    assert len(claims) == 1
    assert claims[0].channel == "wechat"
    assert claims[0].period == PERIOD

    orphan = [_node("EX-CLN-0002", "128.00", channel="wechat", side="channel", row=6)]
    assert claims_from_nodes(orphan) == []


# ───────────── 七、存储支持：复合索引（Task 24 步骤 5） ─────────────


def _seed_db(path: str, rows: list[tuple]) -> None:
    """把 (渠道, 账期, 单号, 金额, 行号) 落进账单库。"""
    from reconq.store.schema import connect, init_schema

    conn = connect(path)
    init_schema(conn)
    for channel, period, order_no, amount, row_no in rows:
        conn.execute(
            "INSERT INTO bill_rows (channel, period, bill_date, channel_order_no, his_order_no, "
            "amount_text, refund_text, trade_time, trade_type, department, settle_type, "
            "source_file, source_row_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (channel, period, period[:4] + "-01-01", order_no, order_no, amount, None,
             "2026-09-02T10:00:00+08:00", "", "门诊收费处", "微信",
             f"{channel}_{period}.csv", row_no),
        )
    conn.close()


def test_load_entity_nodes_uses_the_composite_index_not_a_full_scan(tmp_path):
    """连坐查询必须走 (单号, 账期) 复合索引，否则六路几十万行下是分钟级。

    断言取 SQLite 的查询计划本身，并且断言的是**产品实际执行的那两条 SQL**
    （单号列各查一次）。这里也是本任务踩过的坑的钉子：写成
    ``channel_order_no = ? OR his_order_no = ?`` 一条时，SQLite 对 OR 两列
    只能逐行判断，查询计划会退回按账期扫 ``ix_bill_period`` —— 复合索引
    明明建了却一次都用不上。只断言「结果对」的话，把两个索引都删掉照样全绿。
    """
    from reconq.store.schema import connect, init_schema

    db = str(tmp_path / "trace.db")
    conn = connect(db)
    init_schema(conn)
    for i in range(200):
        conn.execute(
            "INSERT INTO bill_rows (channel, period, bill_date, channel_order_no, his_order_no, "
            "amount_text, refund_text, trade_time, trade_type, department, settle_type, "
            "source_file, source_row_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("wechat", PERIOD, "2026-09-01", f"EX-BULK-{i:04d}", f"EX-BULK-{i:04d}",
             "10.00", None, "2026-09-02T10:00:00+08:00", "", "门诊", "微信",
             "wx.csv", i + 2),
        )
    placeholders = ",".join("?" for _ in (PERIOD, PREV))
    for column, expected_index in (
        ("channel_order_no", "ix_bill_order_period"),
        ("his_order_no", "ix_bill_his_order_period"),
    ):
        sql = (
            f"SELECT * FROM bill_rows WHERE {column} = ? COLLATE NOCASE "
            f"AND period IN ({placeholders}) "
            "ORDER BY period, channel, source_file, source_row_no"
        )
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql, ["EX-BULK-0001", PERIOD, PREV]).fetchall()
        detail = " | ".join(str(row[-1]) for row in plan)
        assert "SEARCH" in detail.upper(), f"{column} 查询走了全表扫：{detail}"
        assert expected_index in detail, f"{column} 未命中 {expected_index}：{detail}"
        # 首列必须是单号：用账期索引的话，每期各扫一遍全表
        assert f"{expected_index} ({column}=?" in detail, detail
    conn.close()


def test_or_across_two_order_no_columns_cannot_use_the_composite_index(tmp_path):
    """反向钉子：说明为什么必须拆成两条 —— OR 两列时索引一定用不上。

    这条测试的价值是**防止有人「顺手把两条查询合并成一条」**：合并后
    结果完全一样、测试照样全绿，只有查询计划变慢，六路几十万行下追查一条
    链从毫秒掉到秒。
    """
    from reconq.store.schema import connect, init_schema

    conn = connect(str(tmp_path / "or.db"))
    init_schema(conn)
    for i in range(50):
        conn.execute(
            "INSERT INTO bill_rows (channel, period, bill_date, channel_order_no, his_order_no, "
            "amount_text, refund_text, trade_time, trade_type, department, settle_type, "
            "source_file, source_row_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("wechat", PERIOD, "2026-09-01", f"EX-OR-{i:04d}", f"EX-OR-{i:04d}",
             "10.00", None, "2026-09-02T10:00:00+08:00", "", "门诊", "微信", "wx.csv", i + 2),
        )
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM bill_rows WHERE period IN (?,?) "
        "AND (channel_order_no = ? COLLATE NOCASE OR his_order_no = ? COLLATE NOCASE)",
        (PERIOD, PREV, "EX-OR-0001", "EX-OR-0001"),
    ).fetchall()
    detail = " | ".join(str(row[-1]) for row in plan)
    assert "ix_bill_order_period" not in detail, (
        f"OR 形式意外命中了复合索引（SQLite 行为可能变化，值得重新评估是否拆分）：{detail}"
    )
    conn.close()


def test_composite_index_exists_on_both_order_no_columns(tmp_path):
    """两个单号列都要有 (单号, 账期) 索引：渠道单号与 his_order_no 各一条。"""
    from reconq.store.schema import BILL_TABLE, connect, init_schema

    conn = connect(str(tmp_path / "idx.db"))
    init_schema(conn)
    names = {r["name"] for r in conn.execute(f"PRAGMA index_list({BILL_TABLE})")}
    assert "ix_bill_order_period" in names
    assert "ix_bill_his_order_period" in names
    # 复合顺序：单号在前、账期在后（反了就退化成每期各扫一次全表）
    cols = [r["name"] for r in conn.execute("PRAGMA index_info(ix_bill_order_period)")]
    assert cols == ["channel_order_no", "period"]
    conn.close()


def test_load_entity_nodes_scopes_to_the_given_periods(tmp_path):
    """追查范围只取指定账期，窗口外的行不混进来。"""
    db = str(tmp_path / "scope.db")
    _seed_db(db, [
        ("wechat", PREV, "EX-SCOPE-0001", "128.00", 5),
        ("wechat", PERIOD, "EX-SCOPE-0001", "128.00", 6),
        ("wechat", "2026-W34", "EX-SCOPE-0001", "128.00", 4),
        ("wechat", PERIOD, "EX-OTHER-0001", "99.00", 7),
    ])
    from reconq.store.schema import connect

    conn = connect(db)
    nodes = load_entity_nodes(conn, "EX-SCOPE-0001", [PREV, PERIOD])
    assert {n.period for n in nodes} == {PREV, PERIOD}
    assert len(nodes) == 2
    assert load_entity_nodes(conn, "查无此单", [PERIOD]) == []
    conn.close()


def test_load_entity_nodes_refuses_an_empty_period_scope(tmp_path):
    """空账期等于全表扫 —— 显式报错而不是默默扫六路几十万行。"""
    db = str(tmp_path / "empty.db")
    _seed_db(db, [("wechat", PERIOD, "EX-EMPTY-0001", "1.00", 2)])
    from reconq.store.schema import connect

    conn = connect(db)
    with pytest.raises(ReconqError) as exc:
        load_entity_nodes(conn, "EX-EMPTY-0001", [])
    assert exc.value.code == "TRACE_PERIOD_REQUIRED"
    conn.close()


def test_load_entity_nodes_normalizes_order_no_on_lookup(tmp_path):
    """归一后仍能命中：台账上给财务科看的是他们系统里那个写法。"""
    db = str(tmp_path / "norm.db")
    _seed_db(db, [("wechat", PERIOD, "EX-NORM-0001", "50.00", 3)])
    from reconq.store.schema import connect

    conn = connect(db)
    assert load_entity_nodes(conn, "ex-norm-0001", [PERIOD])
    assert load_entity_nodes(conn, " EX-NORM-0001 ", [PERIOD])
    conn.close()


def test_load_entity_nodes_deducts_refund_column_once(tmp_path):
    """账单表里退款列是正数，净额 = 金额 − 退款；只扣一次。"""
    from reconq.store.schema import connect

    db = str(tmp_path / "refund.db")
    conn = connect(db)
    from reconq.store.schema import init_schema

    init_schema(conn)
    conn.execute(
        "INSERT INTO bill_rows (channel, period, bill_date, channel_order_no, his_order_no, "
        "amount_text, refund_text, trade_time, trade_type, department, settle_type, "
        "source_file, source_row_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("wechat", PERIOD, "2026-09-01", "EX-REF-DB", "EX-REF-DB", "214.00", "86.00",
         "2026-09-02T10:00:00+08:00", "", "门诊", "微信", "wx.csv", 5),
    )
    nodes = load_entity_nodes(conn, "EX-REF-DB", [PERIOD])
    assert len(nodes) == 1
    assert nodes[0].amount == Decimal("128.00")
    assert nodes[0].is_refund is True
    conn.close()


# ───────────── 八、CLI 腿 ─────────────


def _run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO, "src")
    return subprocess.run(
        [sys.executable, "-m", "reconq", "--no-log", *args],
        capture_output=True, text=True, cwd=cwd or REPO, env=env, timeout=120,
    )


def test_cli_trace_outputs_a_readable_chain_with_all_nodes(tmp_path):
    """reconq trace 端到端：链上每个节点都出现在终端输出里。"""
    db = str(tmp_path / "cli.db")
    _seed_db(db, [
        # 渠道行 + 同单号的 HIS 侧记录各两条：由落库行反推得出「两期各认领一次」
        ("wechat", PREV, "EX-CLI-0001", "128.00", 5),
        ("his", PREV, "EX-CLI-0001", "128.00", 2),
        ("wechat", PERIOD, "EX-CLI-0001", "128.00", 6),
        ("his", PERIOD, "EX-CLI-0001", "128.00", 2),
        ("wechat", PREV, "EX-OTHER-0001", "1.00", 9),
    ])
    proc = _run_cli("trace", "--order", "EX-CLI-0001", "--period", PERIOD,
                    "--window", "1", "--db", db,)
    assert proc.returncode == EXIT_CODE_WITH_CONCLUSION, proc.stderr
    assert "EX-CLI-0001" in proc.stdout
    assert PREV in proc.stdout and PERIOD in proc.stdout
    assert "wechat_2026-W35.csv:5" in proc.stdout
    assert "wechat_2026-W36.csv:6" in proc.stdout
    assert "相邻期成对 1 对" in proc.stdout
    # 未命中的单号不出现在输出里
    assert "EX-OTHER-0001" not in proc.stdout


def test_cli_trace_explicit_periods_scope_overrides_the_window(tmp_path):
    """--periods 给出显式范围时按它取数，不受 --window 影响。"""
    db = str(tmp_path / "scope2.db")
    _seed_db(db, [
        ("wechat", PREV, "EX-SCOPE2", "128.00", 5),
        ("his", PREV, "EX-SCOPE2", "128.00", 2),
        ("wechat", PERIOD, "EX-SCOPE2", "128.00", 6),
        ("his", PERIOD, "EX-SCOPE2", "128.00", 2),
        ("wechat", "2026-W34", "EX-SCOPE2", "128.00", 4),
    ])
    proc = _run_cli("trace", "--order", "EX-SCOPE2", f"--periods={PREV},{PERIOD}",
                    "--db", db,)
    assert proc.returncode == EXIT_CODE_WITH_CONCLUSION, proc.stderr
    assert "2026-W34" not in proc.stdout.split("链上节点：")[1].split("提示")[0]


def test_cli_trace_exits_zero_when_the_chain_is_clean(tmp_path):
    """链上干净即退出码 0：信息科在脚本里靠 $? 判「这期有没有连坐问题」。"""
    db = str(tmp_path / "clean.db")
    _seed_db(db, [("wechat", PERIOD, "EX-CLEAN-0001", "128.00", 5)])
    proc = _run_cli("trace", "--order", "EX-CLEAN-0001", "--period", PERIOD,
                    "--db", db,)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "连坐结论：0 条" in proc.stdout


def test_cli_trace_json_output_is_machine_readable(tmp_path):
    db = str(tmp_path / "j.db")
    _seed_db(db, [
        ("wechat", PREV, "EX-J-0001", "128.00", 5),
        ("his", PREV, "EX-J-0001", "128.00", 2),
        ("wechat", PERIOD, "EX-J-0001", "128.00", 6),
        ("his", PERIOD, "EX-J-0001", "128.00", 2),
    ])
    proc = _run_cli("trace", "--order", "EX-J-0001", "--period", PERIOD,
                    "--db", db, "--json",)
    assert proc.returncode == EXIT_CODE_WITH_CONCLUSION, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["order_no"] == "EX-J-0001"
    # 两期 × （微信行 + HIS 行）= 4 个节点
    assert len(payload["nodes"]) == 4
    assert payload["conclusions"][0]["kind"] == TraceKind.DUPLICATE_ACROSS_PERIOD.value
    assert payload["scope_periods"] == ["2026-W34", "2026-W35", "2026-W36", "2026-W37", "2026-W38"]


def test_cli_trace_rejects_period_and_periods_together(tmp_path):
    """两个都给 → 「窗口到底多大」没有唯一答案，显式报错而不是挑一个。"""
    proc = _run_cli("trace", "--order", "EX-X", "--period", PERIOD,
                    "--periods", f"{PREV},{PERIOD}", "--db", str(tmp_path / "x.db"),)
    assert proc.returncode == 2, proc.stdout
    assert "TRACE_WINDOW_INVALID" in proc.stderr
    assert "只能给一个" in proc.stderr


def test_cli_trace_rejects_empty_periods_list(tmp_path):
    proc = _run_cli("trace", "--order", "EX-X", "--periods=,,", "--db", str(tmp_path / "x.db"),)
    assert proc.returncode == 2, proc.stdout
    assert "TRACE_PERIOD_REQUIRED" in proc.stderr


def test_cli_trace_no_index_leg_finds_the_same_nodes(tmp_path):
    """排障腿绕开索引取全量，结果必须与索引路径一致。

    不一致就说明索引路径漏了行 —— 那正是最该被抓到的一类偏差。
    """
    db = str(tmp_path / "ni.db")
    _seed_db(db, [
        ("wechat", PREV, "EX-NI-0001", "128.00", 5),
        ("his", PREV, "EX-NI-0001", "128.00", 2),
        ("wechat", PERIOD, "EX-NI-0001", "128.00", 6),
        ("his", PERIOD, "EX-NI-0001", "128.00", 2),
    ])
    fast = _run_cli("trace", "--order", "EX-NI-0001", "--period", PERIOD,
                    "--window", "2", "--db", db, "--json",)
    slow = _run_cli("trace", "--order", "EX-NI-0001", "--period", PERIOD,
                    "--window", "2", "--db", db, "--json", "--no-index",)
    assert fast.returncode == slow.returncode == EXIT_CODE_WITH_CONCLUSION
    a, b = json.loads(fast.stdout), json.loads(slow.stdout)
    assert [n["ref"] for n in a["nodes"]] == [n["ref"] for n in b["nodes"]]
    assert a["conclusions"] == b["conclusions"]


def test_cli_trace_reports_nothing_for_an_unknown_order_no(tmp_path):
    db = str(tmp_path / "none.db")
    _seed_db(db, [("wechat", PERIOD, "EX-A-0001", "1.00", 2)])
    proc = _run_cli("trace", "--order", "查无此单", "--period", PERIOD,
                    "--db", db,)
    assert proc.returncode == 0
    assert "窗口内没有任何记录" in proc.stdout


def test_cli_trace_help_documents_both_scope_forms():
    proc = _run_cli("trace", "--help")
    assert proc.returncode == 0
    assert "--order" in proc.stdout
    assert "--periods" in proc.stdout
    assert "--window" in proc.stdout


EXIT_CODE_WITH_CONCLUSION = 1


# ───────────── 九、真实规模 ─────────────


def test_trace_over_four_thousand_rows_stays_responsive():
    """真实规模：六路各 700 行（六路共 4200 节点），追查一条链在毫秒级完成。

    连坐追查是经办人逐条点差异时随手触发的动作，慢到秒级就没法用。
    注入一笔真实的跨账期重复：同一单号在 W35 与 W36 各被认领一次。
    """
    nodes: list[TraceNode] = []
    for channel_no, channel in enumerate(
        ("wechat", "alipay", "unionpay", "self_service", "medical_ins", "his")
    ):
        for i in range(700):
            order = f"EX-SCALE-{i:05d}"
            # 账期按 (i + 渠道序号) 奇偶错开：同一个单号在六路上会同时出现在
            # W35 与 W36 —— 这才是「六路各记一次」的真实形态。若只按 i 错开，
            # 六路会整齐落在同一期，测的就不是连坐而是重名。
            nodes.append(
                TraceNode(
                    period=PERIOD if (i + channel_no) % 2 == 0 else PREV,
                    channel=channel,
                    order_no=order,
                    amount=Decimal("10.00"),
                    side="his" if channel == "his" else "channel",
                    trade_time="2026-09-02T10:00:00+08:00",
                    settle_type="微信",
                    source_file=f"{channel}.csv",
                    source_row_no=i + 2,
                    his_order_no=order,
                )
            )
    assert len(nodes) == 4200
    index = build_entity_index(nodes)
    claims = [
        _claim("EX-SCALE-00300", period=PREV, channel="wechat", row=302),
        _claim("EX-SCALE-00300", period=PERIOD, channel="wechat", row=302),
    ]
    started = time.perf_counter()
    chain = trace_entity("EX-SCALE-00300", nodes=[], index=index, claims=claims,
                         focus_period=PERIOD, window_periods=2)
    elapsed = time.perf_counter() - started
    # 六个渠道按奇偶错开：三个记在 W35、三个记在 W36，同一单号被六路各记一次
    assert len(chain.nodes) == 6
    assert chain.occurrences == {PREV: 3, PERIOD: 3}
    assert set(chain.channels) == {
        "wechat", "alipay", "unionpay", "self_service", "medical_ins", "his",
    }
    assert chain.adjacent_period_hits == 1
    # 4200 节点里那笔跨账期重复一条不少地被检出
    assert TraceKind.DUPLICATE_ACROSS_PERIOD.value in {c.kind for c in chain.conclusions}
    assert elapsed < 1.0, f"追查耗时 {elapsed:.3f}s，应在毫秒级"
