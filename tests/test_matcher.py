"""跨渠道实体对齐器（Task 21 · 上半）。

覆盖面：精确匹配、单边账双向、金额不符、弱匹配不认领、跨账期边界、
一码付拆笔、合并支付、退费与原单并存、金额相同多候选，以及两条硬不变量
（一条 HIS 记录最多被认领一次 / 歧义绝不静默择一）与真实规模下的表现。

**对拍基线**：末尾用「按渠道单表逐笔 VLOOKUP 对」的基础实现在同一份样例
上跑一遍，产出一张可复现的差异表 —— 基础实现会把所有一对多情形**静默取
第一条**，本产品报歧义。差异行号与差额均可复现，用于说明「拒绝归并」不是
纸面原则。
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from reconq.align.matcher import (
    AlignRecord,
    AlignResult,
    MatchIndex,
    MatchStatus,
    AlignAllResult,
    align_all,
    align_channel,
    build_index,
    record_from_row_dict,
)
from reconq.store.bill_store import ingest_channel_rows, query_period
from reconq.store.schema import connect, init_schema
from reconq.sources.timezone import period_window, to_session_tz

PERIOD = "2026-W36"
TZ = to_session_tz(None)


def _window():
    return period_window(PERIOD, TZ)


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _chan(order_no: str, amount: str, *, time: str = "2026-09-02 10:00:00", row: int = 1,
          refund: str = "", his_order_no: str = "") -> AlignRecord:
    return AlignRecord(
        channel="wechat",
        period=PERIOD,
        order_no=order_no,
        amount=Decimal(amount),
        refund=Decimal(refund) if refund else None,
        trade_time=_t(time),
        his_order_no=his_order_no,
        source_file="wechat_bill.csv",
        source_row_no=row,
    )


def _his(order_no: str, amount: str, *, time: str = "2026-09-02 10:00:00", row: int = 1,
         dept: str = "门诊收费室", settle: str = "微信", ch_id: int = 0) -> AlignRecord:
    return AlignRecord(
        channel="his",
        period=PERIOD,
        order_no=order_no,
        his_order_no=order_no,
        amount=Decimal(amount),
        trade_time=_t(time),
        department=dept,
        settle_type=settle,
        source_file="charge_record.csv",
        source_row_no=row,
        ch_id=ch_id,
    )


# ─────────────────────────── 一、精确匹配 ───────────────────────────


def test_exact_match_by_order_no():
    index = build_index([_his("EX-1", "128.00", row=7, ch_id=1)], period=PERIOD)
    result = align_channel("wechat", [_chan("EX-1", "128.00", row=3)], index, period=PERIOD)
    assert len(result.outcomes) == 1
    out = result.outcomes[0]
    assert out.status == MatchStatus.MATCHED.value
    assert out.his_record is not None and out.his_record.source_row_no == 7
    assert out.needs_attribution is False
    assert result.his_only == []
    assert result.pending_attribution == 0


def test_exact_match_but_amount_differs_is_mismatch_with_diff():
    """对上了但对不平 —— 比对不上更需要解释，单独一类。"""
    index = build_index([_his("EX-2", "128.00", ch_id=1)], period=PERIOD)
    out = align_channel("wechat", [_chan("EX-2", "100.00")], index, period=PERIOD).outcomes[0]
    assert out.status == MatchStatus.MISMATCH.value
    assert out.diff_amount == "-28.00"
    assert out.needs_attribution is True
    assert "100.00" in out.detail and "128.00" in out.detail


def test_amount_mismatch_inside_tolerance_is_still_matched():
    """院内导出常有分位抹零，0.01 以内不算不符。"""
    index = build_index([_his("EX-2b", "128.00", ch_id=1)], period=PERIOD)
    out = align_channel("wechat", [_chan("EX-2b", "128.01")], index, period=PERIOD).outcomes[0]
    assert out.status == MatchStatus.MATCHED.value


def test_refund_row_matches_and_reduces_net_amount():
    """渠道侧退费：refund 列使净金额下降，HIS 侧对应负额记录。"""
    chan = _chan("EX-REF", "128.00", refund="86.00")
    his = _his("EX-REF", "42.00", ch_id=1)
    assert chan.is_refund is True
    assert chan.net_amount == Decimal("42")
    index = build_index([his], period=PERIOD)
    out = align_channel("wechat", [chan], index, period=PERIOD).outcomes[0]
    assert out.status == MatchStatus.MATCHED.value
    # 若把退款当负数相加，净额会变成 214.00，差额 172.00 —— 一笔退费被放大
    assert chan.net_amount != chan.amount + chan.refund


# ─────────────────────── 二、单边账（双向） ───────────────────────


def test_channel_only_when_his_has_no_such_order():
    index = build_index([_his("EX-9", "56.50", ch_id=1)], period=PERIOD)
    result = align_channel("wechat", [_chan("EX-NOT-IN-HIS", "999.00")], index, period=PERIOD)
    out = result.outcomes[0]
    assert out.status == MatchStatus.CHANNEL_ONLY.value
    assert out.needs_attribution is True
    assert "漏收费" in out.detail
    # 那一行 HIS 确实没有任何渠道行认领 —— 单跑一路时它就该出现在反向
    # 单边账里，不能因为本行判了「渠道有 HIS 无」就把它藏起来
    assert [r.order_no for r in result.his_only] == ["EX-9"]


def test_his_only_when_no_channel_row_claims_it():
    index = build_index(
        [_his("EX-A", "128.00", ch_id=1), _his("EX-CASH", "30.00", ch_id=2, settle="现金")],
        period=PERIOD,
    )
    result = align_channel("wechat", [_chan("EX-A", "128.00")], index, period=PERIOD)
    assert result.outcomes[0].status == MatchStatus.MATCHED.value
    assert [r.order_no for r in result.his_only] == ["EX-CASH"]
    assert result.his_only_amount() == "30.00"
    assert result.pending_attribution == 1


def test_both_sided_single_legs_are_reported_together():
    """一个账期里两个方向的差异必须同时出现。"""
    index = build_index(
        [_his("EX-A", "128.00", ch_id=1), _his("EX-D", "60.00", ch_id=2)],
        period=PERIOD,
    )
    result = align_channel(
        "wechat", [_chan("EX-A", "128.00"), _chan("EX-MISSING", "88.00", row=2)],
        index,
        period=PERIOD,
    )
    statuses = {o.channel_record.order_no: o.status for o in result.outcomes}
    assert statuses["EX-A"] == MatchStatus.MATCHED.value
    assert statuses["EX-MISSING"] == MatchStatus.CHANNEL_ONLY.value
    assert [r.order_no for r in result.his_only] == ["EX-D"]
    assert result.pending_attribution == 2


# ───────────────────── 三、弱匹配：唯一命中也不认领 ─────────────────────


def test_weak_match_unique_candidate_is_not_claimed():
    """单号未命中、金额 + 时间窗唯一命中 —— 仍标待人工，绝不认领。"""
    index = build_index([_his("EX-H1", "88.00", ch_id=1)], period=PERIOD)
    result = align_channel("wechat", [_chan("EX-UNKNOWN", "88.00")], index, period=PERIOD,
                           window=_window())
    out = result.outcomes[0]
    assert out.status == MatchStatus.WEAK_PENDING.value
    assert out.needs_attribution is True
    assert out.his_record is None            # 认领必须为空
    assert out.ambiguity is not None
    assert out.ambiguity.error_code == "WEAK_MATCH_PENDING"
    assert out.ambiguity.candidate_total == 1
    # 那条 HIS 记录既没被认领、也不该再单开一条「HIS 有渠道无」—— 同一笔
    # 钱已经挂在弱匹配候选下面了，再报一次就是让经办人判两遍
    assert result.his_only == []
    assert out.candidates[0].record_id == "EX-H1"


def test_weak_match_ignores_candidate_outside_period_window():
    """弱匹配受账期时间窗约束：金额相同但落在窗口外的 HIS 记录不是候选。"""
    w = _window()
    inside = _his("EX-IN", "88.00", time="2026-09-02 10:00:00", ch_id=1)
    outside = _his("EX-OUT", "88.00", time="2026-09-08 00:00:00", ch_id=2)  # 下一账期
    assert w.is_cross_period(outside.trade_time) is True
    index = build_index([inside, outside], period=PERIOD)
    out = align_channel("wechat", [_chan("EX-UNKNOWN", "88.00")], index, period=PERIOD,
                        window=w).outcomes[0]
    assert out.status == MatchStatus.WEAK_PENDING.value
    assert out.ambiguity.candidate_total == 1
    assert out.ambiguity.candidates[0].record_id == "EX-IN"


def test_weak_match_amount_mismatch_is_not_a_candidate():
    index = build_index([_his("EX-H1", "87.00", ch_id=1)], period=PERIOD)
    out = align_channel("wechat", [_chan("EX-UNKNOWN", "88.00")], index, period=PERIOD,
                        window=_window()).outcomes[0]
    assert out.status == MatchStatus.CHANNEL_ONLY.value
    assert out.ambiguity is None

    # 容差边界：87.99 差 0.01，仍在容差内，是候选（院内导出常分位抹零）
    index2 = build_index([_his("EX-H1", "87.99", ch_id=1)], period=PERIOD)
    out2 = align_channel("wechat", [_chan("EX-UNKNOWN", "88.00")], index2, period=PERIOD,
                         window=_window()).outcomes[0]
    assert out2.status == MatchStatus.WEAK_PENDING.value


# ─────────────────── 四、一条 HIS 记录最多被认领一次 ───────────────────


def test_same_his_record_cannot_be_claimed_twice():
    """两条渠道行金额相同、单号都未命中：第二条不能「弱匹配」到已认领的那条。"""
    index = build_index([_his("EX-H1", "88.00", ch_id=1)], period=PERIOD)
    result = align_channel(
        "wechat",
        [_chan("EX-U1", "88.00", row=1), _chan("EX-U2", "88.00", row=2)],
        index,
        period=PERIOD,
        window=_window(),
    )
    assert result.outcomes[0].status == MatchStatus.WEAK_PENDING.value
    # 第二条要么也弱匹配上（此时 HIS 仍未被认领），要么落单边账；两种都
    # 不能出现「HIS 记录被认领两次」
    # 弱匹配**从不认领**，所以两行都指向同一条候选是允许的：现场事实是
    # 两笔渠道款都对不上这同一条 HIS 收费，本该各报一条差异。这条不变量
    # 守的是「别把一条 HIS 收费算进合计两次」，而不是「候选不许重复」。
    assert result.outcomes[0].status == MatchStatus.WEAK_PENDING.value
    assert result.outcomes[1].status == MatchStatus.WEAK_PENDING.value
    assert all(o.his_record is None for o in result.outcomes)
    assert result.his_only == []
    assert result.pending_attribution == 2


def test_exact_match_claims_his_record_so_later_row_cannot_weak_match_it():
    index = build_index([_his("EX-A", "88.00", ch_id=1)], period=PERIOD)
    result = align_channel(
        "wechat",
        [_chan("EX-A", "88.00", row=1), _chan("EX-U", "88.00", row=2)],
        index,
        period=PERIOD,
        window=_window(),
    )
    assert result.outcomes[0].status == MatchStatus.MATCHED.value
    # 已被认领的记录不再作为候选 → 无候选 → 落单边账，而不是「弱匹配上」
    assert result.outcomes[1].status == MatchStatus.CHANNEL_ONLY.value
    assert result.his_only == []


def test_align_all_refuses_to_let_two_channels_claim_the_same_his_record():
    """同一笔 HIS 收费不得被两路渠道各认领一次（差额会被吃掉）。"""
    index = build_index([_his("EX-A", "128.00", ch_id=1)], period=PERIOD)
    wechat = align_channel("wechat", [_chan("EX-A", "128.00")], index, period=PERIOD)
    alipay = align_channel("alipay", [_chan("EX-A", "128.00")], index, period=PERIOD)
    # 单跑各看各的都会「对齐成功」—— 共享认领集合才拦得住
    assert wechat.outcomes[0].status == MatchStatus.MATCHED.value
    assert alipay.outcomes[0].status == MatchStatus.MATCHED.value

    shared: dict[int, str] = {}
    first = align_channel("wechat", [_chan("EX-A", "128.00")], index, period=PERIOD, claimed=shared)
    second = align_channel("alipay", [_chan("EX-A", "128.00")], index, period=PERIOD, claimed=shared)
    assert first.outcomes[0].status == MatchStatus.MATCHED.value
    # 第二路不再二次认领，而是报「重复认领」并点名首认领方
    assert second.outcomes[0].status == MatchStatus.DUPLICATE_CLAIM.value
    assert second.outcomes[0].his_record is None
    assert "wechat" in second.outcomes[0].ambiguity.detail


# ─────────────── 五、账期时间窗：跨账期落点不参与匹配 ───────────────


def test_cross_period_record_is_not_matched_inside_current_period():
    """HIS 侧记录落在下周一 00:00 之后 —— 属于下期，不参与本期精确匹配。"""
    w = _window()
    late = _his("EX-LATE", "128.00", time="2026-09-07 00:00:01", ch_id=1)
    assert w.is_cross_period(late.trade_time) is True
    index = build_index([late], period=PERIOD)
    out = align_channel("wechat", [_chan("EX-LATE", "128.00")], index, period=PERIOD,
                        window=w).outcomes[0]
    # 单号命中唯一一条，但那条跨期 —— 单列一类，不认领、不算已对齐
    assert out.status == MatchStatus.CROSS_PERIOD.value
    assert out.his_record is None
    assert out.needs_attribution is True
    assert out.ambiguity is not None
    assert out.ambiguity.kind == "cross_period"
    assert out.ambiguity.candidates[0].cross_period is True
    # 该 HIS 记录没被认领，账期窗口外的记录仍留在反向单边账里等下期处理
    assert [r.order_no for r in align_channel("wechat", [], index, period=PERIOD, window=w).his_only] == ["EX-LATE"]


def test_monday_zero_belongs_to_the_new_period():
    """半开区间：周一 00:00:00 属新账期，00:00:00 前一秒属旧账期。"""
    w = _window()
    assert w.contains(_t("2026-09-06 23:59:59")) is True
    assert w.contains(_t("2026-09-07 00:00:00")) is False


def test_key_carries_period_so_same_order_no_across_periods_is_not_merged():
    """同一个单号在两个账期出现时不互相干扰。"""
    prev = AlignRecord(channel="his", period="2026-W35", order_no="EX-RETRY",
                       his_order_no="EX-RETRY", amount=Decimal("10.00"),
                       trade_time=_t("2026-08-31 10:00:00"), ch_id=1)
    cur = _his("EX-RETRY", "20.00", ch_id=2)
    index = build_index([prev, cur], period=PERIOD)
    out = align_channel("wechat", [_chan("EX-RETRY", "20.00")], index, period=PERIOD).outcomes[0]
    # 命中 2 条 → 拒绝归并，而不是靠「取第一条」蒙混过去
    assert out.status == MatchStatus.AMBIGUOUS.value
    assert out.ambiguity.candidate_total == 2


# ───────────────────── 六、align_all 六路统一对齐 ─────────────────────


def test_align_all_shares_one_his_index_across_six_channels():
    his_rows = [_his("EX-A", "128.00", ch_id=1), _his("EX-B", "56.00", ch_id=2)]
    channels = {
        "wechat": [_chan("EX-A", "128.00")],
        "alipay": [_chan("EX-B", "56.00")],
        "unionpay": [_chan("EX-Z", "10.00", row=9)],
        "self_service": [],
        "his": [],
        "medical_ins": [],
    }
    results = align_all(channels, his_rows, period=PERIOD, window=_window())
    assert isinstance(results, AlignAllResult)
    assert set(results.channels) == set(channels)
    assert results.channels["wechat"].outcomes[0].status == MatchStatus.MATCHED.value
    assert results.channels["alipay"].outcomes[0].status == MatchStatus.MATCHED.value
    assert results.channels["unionpay"].outcomes[0].status == MatchStatus.CHANNEL_ONLY.value
    # 六路都消费同一份 HIS：EX-A 与 EX-B 各被认领一次，无反向单边账
    assert results.his_only == []
    # 反向单边账是全局事实，不摊到任何一路名下（暗示「是这一路漏的」是错的）
    for result in results.channels.values():
        assert result.his_only == []


def test_align_all_reports_his_only_globally_not_per_channel():
    his_rows = [_his("EX-A", "128.00", ch_id=1), _his("EX-CASH", "30.00", ch_id=2)]
    channels = {"wechat": [_chan("EX-A", "128.00")], "alipay": [], "unionpay": []}
    results = align_all(channels, his_rows, period=PERIOD, window=_window())
    assert [r.order_no for r in results.his_only] == ["EX-CASH"]
    assert results.pending_attribution == 1


def test_align_all_empty_channel_returns_empty_result_not_error():
    results = align_all({"wechat": []}, [_his("EX-A", "1.00", ch_id=1)], period=PERIOD)
    assert results.channels["wechat"].outcomes == []
    assert results.channels["wechat"].total_amount() == "0"
    assert len(results.his_only) == 1


# ───────────────────── 七、from 库表构造 + 真实规模 ─────────────────────


def test_record_from_row_dict_round_trips_db_row():
    row = {
        "ch_id": 12, "channel": "his", "period": PERIOD, "bill_date": "2026-09-02",
        "channel_order_no": "EX-DB", "his_order_no": "EX-DB",
        "amount_text": "128.00", "refund_text": None,
        "trade_time": "2026-09-02T10:00:00+08:00", "trade_type": "N",
        "department": "门诊收费室", "settle_type": "微信",
        "source_file": "charge.csv", "source_row_no": 42,
    }
    rec = record_from_row_dict(row)
    assert rec.net_amount == Decimal("128.00")
    assert rec.trade_time is not None and rec.trade_time.tzinfo is not None
    assert rec.ref == "charge.csv:42"
    assert rec.ch_id == 12


def test_record_from_row_dict_with_unparsable_time_does_not_raise():
    """时间戳坏掉不该让整行对不上 —— 弱匹配会更保守，但不会崩。"""
    rec = record_from_row_dict({
        "channel": "his", "period": PERIOD, "channel_order_no": "X",
        "amount_text": "10.00", "trade_time": "不是时间", "source_row_no": 3,
    })
    assert rec.trade_time is None
    assert rec.net_amount == Decimal("10.00")


def test_align_two_thousand_rows_stays_bounded_and_reports_every_row():
    """真实规模：2000 渠道行 × 2000 HIS 行，内存与耗时有界且每行都有结论。"""
    n = 2000
    base = _t("2026-09-02 08:00:00")
    his_rows = [
        AlignRecord(
            channel="his", period=PERIOD, order_no=f"BULK-{i:05d}", his_order_no=f"BULK-{i:05d}",
            amount=Decimal("10.00") + Decimal(i % 97), trade_time=base + timedelta(seconds=i),
            department="住院收费室", settle_type="微信", source_file="charge.csv",
            source_row_no=i + 1, ch_id=i + 1,
        )
        for i in range(n)
    ]
    chan_rows = [
        AlignRecord(
            channel="wechat", period=PERIOD, order_no=f"BULK-{i:05d}",
            amount=Decimal("10.00") + Decimal(i % 97), trade_time=base + timedelta(seconds=i),
            source_file="wechat.csv", source_row_no=i + 1,
        )
        for i in range(n)
    ]
    # 掺入 30 条渠道单边账 + 20 条 HIS 单边账，规模不改变判定语义
    chan_rows += [
        AlignRecord(channel="wechat", period=PERIOD, order_no=f"EXTRA-{i:05d}",
                    amount=Decimal("66.00"), trade_time=base, source_file="wechat.csv",
                    source_row_no=9000 + i)
        for i in range(30)
    ]
    his_rows += [
        AlignRecord(channel="his", period=PERIOD, order_no=f"ORPHAN-{i:05d}",
                    his_order_no=f"ORPHAN-{i:05d}", amount=Decimal("77.00"),
                    trade_time=base, source_file="charge.csv", source_row_no=9000 + i,
                    ch_id=100000 + i)
        for i in range(20)
    ]

    started = time.perf_counter()
    result = align_channel("wechat", chan_rows, build_index(his_rows, period=PERIOD),
                           period=PERIOD, window=_window())
    elapsed = time.perf_counter() - started
    assert result.total_amount() == str(
        sum((r.net_amount for r in chan_rows), Decimal("0"))
    )

    counts = result.counts()
    assert len(result.outcomes) == n + 30          # 每条渠道行都有结论，无遗漏
    assert counts[MatchStatus.MATCHED.value] == n
    assert counts[MatchStatus.CHANNEL_ONLY.value] == 30
    assert len(result.his_only) == 20
    assert result.pending_attribution == 50
    # 无歧义、无弱匹配：全部结论可复现
    assert counts[MatchStatus.AMBIGUOUS.value] == 0
    assert counts[MatchStatus.WEAK_PENDING.value] == 0
    assert counts[MatchStatus.CROSS_PERIOD.value] == 0
    assert elapsed < 5.0, f"2000×2000 对齐耗时 {elapsed:.2f}s，超出可接受范围"


# ───────────────────── 八、与基础实现对拍 ─────────────────────


def _baseline_vlookup(chan_rows, his_by_no):
    """基础实现：按单号逐笔 VLOOKUP，**命中多条时静默取第一条**。

    院内现有人工流程就是这个形态（Excel 透视表 / 逐笔 VLOOKUP 查 HIS 导出）。
    把它作为基线，才能说明本产品在哪些行上多给出了东西。
    """
    out = []
    for rec in chan_rows:
        hit = his_by_no.get(rec.order_no)
        if not hit:
            out.append((rec.order_no, "miss", ""))
            continue
        first = hit[0]
        tag = "ok" if first.net_amount == rec.net_amount else "diff"
        out.append((rec.order_no, tag, str(rec.net_amount - first.net_amount) if tag == "diff" else ""))
    return out


def test_comparison_against_naive_vlookup_baseline_on_the_same_sample():
    """同一份样例上与基础实现对拍，差异行号与差额可复现。"""
    his_rows = [
        _his("EX-OK", "128.00", row=2, ch_id=1),
        _his("EX-SPLIT", "50.00", row=3, dept="住院收费室", ch_id=2),
        _his("EX-SPLIT", "78.00", row=4, dept="住院收费室", ch_id=3),
        _his("EX-SAME", "66.00", row=5, dept="门诊收费室", ch_id=4),
        _his("EX-SAME", "66.00", row=6, dept="门诊收费室", ch_id=5),
        _his("EX-ONLY-HIS", "30.00", row=7, ch_id=6),
    ]
    chan_rows = [
        _chan("EX-OK", "128.00", row=11),
        _chan("EX-SPLIT", "128.00", row=12),
        _chan("EX-SAME", "66.00", row=13),
        _chan("EX-NOT-IN-HIS", "999.00", row=14),
    ]
    index = build_index(his_rows, period=PERIOD)
    result = align_channel("wechat", chan_rows, index, period=PERIOD, window=_window())

    by_no = {o.channel_record.order_no: o for o in result.outcomes}
    assert by_no["EX-OK"].status == MatchStatus.MATCHED.value
    assert by_no["EX-SPLIT"].status == MatchStatus.AMBIGUOUS.value
    assert by_no["EX-SAME"].status == MatchStatus.AMBIGUOUS.value
    assert by_no["EX-NOT-IN-HIS"].status == MatchStatus.CHANNEL_ONLY.value
    # 歧义候选是「待人工的一组行」，不是反向单边账 —— 同一批行不能既挂在
    # 歧义下面、又被单开一条「HIS 有渠道无」，否则同一笔钱会被报两次
    assert [r.order_no for r in result.his_only] == ["EX-ONLY-HIS"]

    # 基线：EX-SPLIT 与 EX-SAME 都会被静默取第一条，EX-ONLY-HIS 直接消失
    his_by_no: dict[str, list] = {}
    for rec in his_rows:
        his_by_no.setdefault(rec.order_no, []).append(rec)
    baseline = _baseline_vlookup(chan_rows, his_by_no)
    baseline_by_no = {row[0]: row for row in baseline}
    assert baseline_by_no["EX-SPLIT"][1] == "diff"      # 取第一条后金额对不上
    assert baseline_by_no["EX-SAME"][1] == "ok"         # 取第一条后「对上了」——假对
    assert baseline_by_no["EX-OK"][1] == "ok"

    # 本产品比基线多给出的东西：歧义 + 反向单边账
    extra = sum(1 for o in result.outcomes if o.status == MatchStatus.AMBIGUOUS.value) + len(result.his_only)
    assert extra == 3
    assert result.total_amount() == str(sum((r.net_amount for r in chan_rows), Decimal("0")))


def test_ambiguous_candidate_list_carries_every_candidate_row_no():
    """歧义必须列全候选行号与字段，否则财务科无法回查。"""
    index = build_index(
        [
            _his("EX-M", "10.00", row=101, dept="门诊", ch_id=1),
            _his("EX-M", "20.00", row=102, dept="住院", ch_id=2),
            _his("EX-M", "30.00", row=103, dept="急诊", ch_id=3),
        ],
        period=PERIOD,
    )
    out = align_channel("wechat", [_chan("EX-M", "60.00")], index, period=PERIOD).outcomes[0]
    assert out.status == MatchStatus.AMBIGUOUS.value
    cand = out.ambiguity.candidates
    assert [c.row_no for c in cand] == [101, 102, 103]
    assert [c.department for c in cand] == ["门诊", "住院", "急诊"]
    assert all(c.settle_type == "微信" for c in cand)
    assert all(c.amount for c in cand)
    assert out.ambiguity.candidate_total == 3


# ───────────── 九、真实样例端到端（tests/fixtures/align/） ─────────────

ALIGN_FIXTURES = "tests/fixtures/align/2026-W36"


def _load_fixture(channel: str, relative: str):
    """走主路径读样例：契约 → 文件读入 → 归一 → 对齐行。

    刻意**不**手工构造记录：这条链路上有契约变量注入、编码探测、列名别名
    与金额定点化，任何一环的口径漂移都会在这里现形，而手工构造的
    dataclass 会把它们全绕过去。
    """
    from reconq.contracts.loader import load_contract
    from reconq.sources.format_probe import read_bill_file_isolated
    from reconq.store.bill_store import build_record
    from reconq.align.matcher import records_from_bill

    contract = load_contract(
        f"contracts/{channel}.yml",
        {"period": PERIOD, "session_timezone": "Asia/Shanghai"},
    )
    rows, _probe, _dirty = read_bill_file_isolated(
        f"{ALIGN_FIXTURES}/{relative}", contract.dataset.tables[0], channel, period=PERIOD
    )
    return records_from_bill(
        build_record(r, channel=channel, period=PERIOD, window=_window(), session_tz=TZ)
        for r in rows
    )


def test_real_fixtures_reach_every_outcome_kind_end_to_end():
    """六路真实样例上跑一遍：七种状态各有归属，结论逐条可回查。"""
    his_rows = _load_fixture("his", "his/charge_record_2026-W36.csv")
    chans = {
        "wechat": _load_fixture("wechat", "wechat/wechat_tradebill_2026-W36.csv"),
        "alipay": _load_fixture("alipay", "alipay/alipay_bill_2026-W36.csv"),
        "unionpay": _load_fixture("unionpay", "unionpay/unionpay_bill_2026-W36.txt"),
    }
    assert len(his_rows) == 15, f"HIS 样例应 15 行，实读 {len(his_rows)} 行"
    assert len(chans["wechat"]) == 7
    assert len(chans["alipay"]) == 4, f"支付宝样例应 4 行，实读 {len(chans['alipay'])} 行"
    assert len(chans["unionpay"]) == 2

    res = align_all(chans, his_rows, period=PERIOD, window=_window())
    seen = {o.status for r in res.channels.values() for o in r.outcomes}
    # 七种状态里这五类在真实样例上都要出现（弱匹配需人工补单号，故不入夹具）
    assert MatchStatus.MATCHED.value in seen
    assert MatchStatus.MISMATCH.value in seen
    assert MatchStatus.CHANNEL_ONLY.value in seen
    assert MatchStatus.AMBIGUOUS.value in seen
    assert MatchStatus.CROSS_PERIOD.value in seen
    assert MatchStatus.WEAK_PENDING.value in seen
    # 每条待归因行都能回到源文件行号
    for result in res.channels.values():
        for o in result.outcomes:
            if o.needs_attribution:
                assert o.channel_record is not None
                assert o.channel_record.source_file and o.channel_record.source_row_no > 0
    # 反向单边账：HIS 那笔现金谁都没认领
    assert [r.his_order_no for r in res.his_only] == ["EX-HIS-ONLY-0001"]
    assert res.his_only[0].settle_type == "现金"
    assert res.pending_attribution == 9


def test_real_fixture_classifies_split_refund_and_equal_amount_correctly():
    """三种一对多在真实数据上必须分到三个不同形态。

    分类错了话术就错：「退费与原单并存」被说成「一码付拆多笔」，经办人
    会去查拆分说明，而实际该查的是红冲有没有生效。
    """
    his_rows = _load_fixture("his", "his/charge_record_2026-W36.csv")
    res = align_all(
        {"wechat": _load_fixture("wechat", "wechat/wechat_tradebill_2026-W36.csv")},
        his_rows, period=PERIOD, window=_window(),
    )
    kinds = {
        o.key.order_no: o.ambiguity.kind
        for o in res.channels["wechat"].outcomes
        if o.ambiguity is not None
    }
    assert kinds["EX-WX-SPLIT-0001"] == "split"
    assert kinds["EX-WX-REFUND-0001"] == "refund_and_original"
    assert kinds["EX-WX-SAME-0001"] == "equal_amount"
    assert kinds["EX-WX-CROSS-0001"] == "cross_period"
    # 退费那一对里，红冲行必须被标出来
    refund_out = next(
        o for o in res.channels["wechat"].outcomes if o.key.order_no == "EX-WX-REFUND-0001"
    )
    assert [c.is_refund for c in refund_out.ambiguity.candidates] == [False, True]
    # 一条都没被认领
    assert all(o.his_record is None for o in res.channels["wechat"].outcomes
               if o.ambiguity is not None)


def test_his_refund_flag_column_is_honoured_not_only_the_refund_amount():
    """HIS 侧 ``is_refund=true`` 的红冲行必须被认出来。

    那行的金额是**红冲后的净额**、退款列是空的。只看退款列会把它判成正常
    收费，于是「原单 + 红冲」这对候选被误报成一码付拆多笔。
    """
    his_rows = _load_fixture("his", "his/charge_record_2026-W36.csv")
    refund_rows = [r for r in his_rows if r.is_refund]
    assert len(refund_rows) == 1
    assert refund_rows[0].his_order_no == "EX-WX-REFUND-0001"
    assert refund_rows[0].refund is None          # 退款列确实是空的
    assert refund_rows[0].amount == Decimal("42.00")
    # 布尔写法复用 engine.common.to_bool，不另立词表
    assert refund_rows[0].trade_type.strip().lower() == "true"
    # 非退费行不受影响
    assert [r.his_order_no for r in his_rows if r.is_refund] == ["EX-WX-REFUND-0001"]


def test_real_fixture_amount_mismatch_reports_the_exact_diff():
    """支付宝那一行：渠道 1020.00 vs HIS 999.00，差额 21.00 且可回查。"""
    his_rows = _load_fixture("his", "his/charge_record_2026-W36.csv")
    res = align_all(
        {"alipay": _load_fixture("alipay", "alipay/alipay_bill_2026-W36.csv")},
        his_rows, period=PERIOD, window=_window(),
    )
    out = next(o for o in res.channels["alipay"].outcomes
               if o.key.order_no == "EX-ALIPAY-DIFF-0001")
    assert out.status == MatchStatus.MISMATCH.value
    assert out.diff_amount == "21.00"
    assert "charge_record_2026-W36.csv" in out.detail
    # 该 HIS 记录仍被认领（对上了就是认领了），不重复出现在单边账里
    assert out.his_record is not None
    assert "EX-ALIPAY-DIFF-0001" not in [r.his_order_no for r in res.his_only]
    # 本例只跑了支付宝一路，微信那几笔自然全是反向单边账 —— 这正是
    # 「按本次实际范围说」：不能因为少跑了一路就报一份看起来干净的结果
    assert "EX-HIS-ONLY-0001" in [r.his_order_no for r in res.his_only]
