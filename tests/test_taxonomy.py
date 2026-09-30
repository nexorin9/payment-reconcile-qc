"""歧义四形态分类与判别规则（Task 25）。

覆盖面：四种成因 + 显式 UNKNOWN 的判别、建议动作、判别依据与「已排除的
判据」清单、与 :mod:`reconq.align.ambiguity` / :mod:`reconq.align.scoring`
两个上游产物的交互、差异台账落库与成因筛选，以及五条硬不变量：

1. **四形态互不误判** —— 同一组候选在「金额守恒 / 不守恒」「候选金额全同 /
   不全同」「在账期内 / 越界」「含退费行 / 不含」上只差一点点，就必须分到
   不同成因。把合并支付说成一码付拆笔，排查方向整个反了；
2. **UNKNOWN 必须显式且必带 excluded** —— 判不出来时逐条写明哪条规则不成立、
   为什么不成立，禁止硬塞进某一类；
3. **建议动作非空且逐类不同** —— 四种成因对应四种「该去查什么」；
4. **弱候选一律 UNKNOWN** —— 弱匹配只有金额 + 时间窗，分型是替人编；
5. **不产生任何自动处置** —— 落库条目恒为「待确认」+ ``needs_attribution``，
   模型与规则都不得 transition。

真实数据端到端那一节刻意**走主路径**（契约 → 文件读入 → 归一 → 对齐 →
分型），不手工构造 dataclass：手构会把列名别名、编码探测、金额定点这些
口径漂移全绕过去，而分型判据正是吃这些口径的。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from reconq.align.ambiguity import (
    KIND_ZH as MATCH_KIND_ZH,
    AmbiguityCandidate,
    AmbiguityKind as MatchKind,
    AmbiguityVerdict,
)
from reconq.align.matcher import (
    AlignRecord,
    MatchStatus,
    align_all,
    build_index,
)
from reconq.align.scoring import Candidate, Factor, score_pair
from reconq.align.taxonomy import (
    AMOUNT_TOLERANCE,
    KIND_ZH,
    RULE_NAMES,
    TAXONOMY_PATH_PREFIX,
    AmbiguityKind,
    ClusterRow,
    ExcludedRule,
    TaxonomyCase,
    TaxonomyVerdict,
    case_from_verdict,
    case_from_weak_candidate,
    classify_taxonomy,
    verdict_to_diff_items,
    verdicts_to_diff_items,
)
from reconq.engine.period import (
    DIFF_TABLE,
    count_diff_by_ambiguity,
    init_scan_schema,
    list_diff_items,
    upsert_diff_items,
)
from reconq.sources.timezone import period_window, to_session_tz

PERIOD = "2026-W36"
TZ = to_session_tz(None)


def _window():
    return period_window(PERIOD, TZ)


def _ch(ref: str, amount: str, *, period: str = PERIOD, refund: bool = False) -> ClusterRow:
    return ClusterRow(
        ref=ref, amount=Decimal(amount), role="channel", period=period, is_refund=refund
    )


def _his(ref: str, amount: str, *, period: str = PERIOD, refund: bool = False,
         cross: bool = False, dept: str = "门诊收费室") -> ClusterRow:
    return ClusterRow(
        ref=ref,
        amount=Decimal(amount),
        role="his",
        period=period,
        is_refund=refund,
        cross_period=cross,
        department=dept,
    )


def _case(channel_rows, his_rows, *, order: str = "EX-1", period: str = PERIOD,
          weak: bool = False, factors=()) -> TaxonomyCase:
    return TaxonomyCase(
        period=period,
        channel="wechat",
        order_no=order,
        channel_rows=tuple(channel_rows),
        his_rows=tuple(his_rows),
        weak=weak,
        factors=tuple(factors),
    )


# ───────────── 一、四形态各一断言 ─────────────


def test_split_payment_when_fanout_amounts_conserve():
    """一码付拆笔：一条渠道 128.00 = HIS 两条 50.00 + 78.00。"""
    v = classify_taxonomy(
        _case([_ch("wechat.csv:5", "128.00")], [_his("charge.csv:3", "50.00"),
                                               _his("charge.csv:4", "78.00")])
    )
    assert v.kind == AmbiguityKind.SPLIT_PAYMENT.value
    assert v.kind_zh == "一码付拆笔"
    assert v.diff_amount == "0.00"
    # 判别依据要写清是「哪几行、加起来多少」
    assert any("50" in f and "78" in f for f in v.factors)
    assert v.evidence_total == 3
    # 守恒成立时「合并支付」这条判据不成立，且**排除理由带差额**
    merged_excluded = [e for e in v.excluded if e.code == "merged"]
    assert merged_excluded == [], "守恒即判拆笔，不该再列合并支付"
    assert any(e.code == "refund" for e in v.excluded)


def test_merged_payment_when_fanout_amounts_do_not_conserve_and_differ():
    """合并支付：渠道 100.00，HIS 两条 50.00 + 30.00（差 20.00 且金额不等）。"""
    v = classify_taxonomy(
        _case([_ch("wechat.csv:7", "100.00")], [_his("charge.csv:7", "50.00"),
                                               _his("charge.csv:8", "30.00")])
    )
    assert v.kind == AmbiguityKind.MERGED_PAYMENT.value
    assert v.kind_zh == "合并支付"
    assert v.diff_amount == "20.00"
    # 守恒判据不成立，理由里必须带实测差额
    split_excluded = [e for e in v.excluded if e.code == "split"]
    assert split_excluded and "20.00" in split_excluded[0].reason
    # 扇出侧金额不全相同是判成合并的**必要**条件，判别依据里要点名
    assert any("50" in f and "30" in f for f in v.factors)


def test_cross_period_when_a_candidate_falls_outside_the_period():
    """跨账期落点：候选 charge_time 越过周一零点。"""
    v = classify_taxonomy(
        _case([_ch("wechat.csv:9", "240.00")],
              [_his("charge.csv:11", "240.00", cross=True)])
    )
    assert v.kind == AmbiguityKind.CROSS_PERIOD.value
    assert v.kind_zh == "跨账期落点"
    assert "1 条候选" in v.detail
    assert any("charge.csv:11" in f for f in v.factors)


def test_cross_period_also_fires_on_a_period_label_mismatch_even_when_in_window():
    """导出方把上期记录标成本期账期时，光看 ``cross_period`` 布尔位会漏判。

    账单文件晚到是现场真发生的事：时间落在窗外，但 ``period_code`` 写的是本
    期。只按布尔位判，「这笔其实属于上一期」这条线索就丢了。
    """
    v = classify_taxonomy(
        _case([_ch("wechat.csv:9", "240.00")],
              [_his("charge.csv:11", "240.00", period="2026-W35")])
    )
    assert v.kind == AmbiguityKind.CROSS_PERIOD.value
    assert any("2026-W35" in f for f in v.factors)


def test_refund_coexist_when_a_refund_row_sits_next_to_the_original():
    """退费与原单并存：原单 128.00 + 红冲 42.00。"""
    v = classify_taxonomy(
        _case([_ch("wechat.csv:8", "128.00")],
              [_his("charge.csv:9", "128.00"),
               _his("charge.csv:10", "42.00", refund=True)])
    )
    assert v.kind == AmbiguityKind.REFUND_COEXIST.value
    assert v.kind_zh == "退费与原单并存"
    # 退费行必须被单独点名，否则经办人不知道该查红冲
    assert any("红冲" in f for f in v.factors)
    assert any("charge.csv:10" in f for f in v.factors)


def test_refund_detected_by_negative_amount_even_without_the_flag():
    """HIS 侧红冲行金额本身为负、退款列为空，只看标志位会漏。"""
    v = classify_taxonomy(
        _case([_ch("wechat.csv:8", "128.00")],
              [_his("charge.csv:9", "128.00"), _his("charge.csv:10", "-42.00")])
    )
    assert v.kind == AmbiguityKind.REFUND_COEXIST.value


# ───────────── 二、UNKNOWN 显式且必带 excluded ─────────────


def test_unknown_when_fanout_amounts_all_equal():
    """候选金额全同 → 信息耗尽 → UNKNOWN，且**不得**判成合并支付。

    这是本任务最关键的反例：同一天同金额两笔（66.00 + 66.00 对渠道 66.00）
    满足「多对一扇出」却不守恒，若无「扇出侧金额不全相同」这道限制就会被
    判成合并支付，建议「核对分摊规则」—— 现场根本没有合并支付这回事，
    是导出方截断了单号。判错方向的代价比「不知道」大得多。
    """
    v = classify_taxonomy(
        _case([_ch("wechat.csv:6", "66.00")], [_his("charge.csv:5", "66.00"),
                                               _his("charge.csv:6", "66.00")])
    )
    assert v.kind == AmbiguityKind.UNKNOWN.value
    assert v.is_unknown
    assert v.kind_zh == "不可判定"
    assert "信息已耗尽" in v.detail
    # 排除清单必须点名「合并支付」并说明为什么不成立
    codes = {e.code for e in v.excluded}
    assert "merged" in codes and "split" in codes
    merged = next(e for e in v.excluded if e.code == "merged")
    assert "全为" in merged.reason and "66.00" in merged.reason


def test_unknown_lists_every_excluded_rule_with_a_reason():
    """UNKNOWN 的 excluded 逐条都要有原因，不能只有规则名。"""
    v = classify_taxonomy(
        _case([], [], order="EX-EMPTY")
    )
    assert v.kind == AmbiguityKind.UNKNOWN.value
    assert v.excluded, "UNKNOWN 必须显式列出排除了什么"
    for e in v.excluded:
        assert isinstance(e, ExcludedRule)
        assert e.reason.strip(), f"规则 {e.code} 的排除理由不能为空"
        assert e.name_zh == RULE_NAMES.get(e.code, e.code)


def test_one_to_one_is_not_a_fanout_and_lands_unknown():
    """1 对 1 不构成歧义扇出（那是金额不符，走 MISMATCH）。"""
    v = classify_taxonomy(
        _case([_ch("wechat.csv:1", "128.00")], [_his("charge.csv:1", "120.00")])
    )
    assert v.kind == AmbiguityKind.UNKNOWN.value
    assert any("不构成多对一扇出" in e.reason for e in v.excluded)
    assert v.diff_amount == "8.00", "UNKNOWN 也照实给差额，不藏数"


def test_missing_side_is_unknown_not_a_guess():
    """渠道侧缺失时不填 0 充数 —— 填 0 会让守恒判据永远成立。"""
    v = classify_taxonomy(_case([], [_his("charge.csv:1", "128.00")]))
    assert v.kind == AmbiguityKind.UNKNOWN.value
    assert any(e.code == "fanout" for e in v.excluded)


# ───────────── 三、建议动作非空且逐类不同 ─────────────


def test_every_kind_has_a_non_empty_suggestion_and_zh_name():
    for kind in AmbiguityKind:
        zh, suggestion = KIND_ZH[kind.value]
        assert zh.strip(), f"{kind.value} 缺中文名"
        assert len(suggestion) >= 10, f"{kind.value} 的建议动作太短，指向性不足"
        assert any(
            kw in suggestion
            for kw in ("查", "确认", "索取", "问", "核对", "补")
        ), f"{kind.value} 的建议动作没说要做什么"


def test_the_four_suggestions_point_at_four_different_things():
    """四种成因的建议必须互相区分：查的东西不一样。

    话术重复等于没分类：经办人看完不知道该找收费处还是该找导出方。
    """
    split, merged, cross, refund = (
        KIND_ZH[AmbiguityKind.SPLIT_PAYMENT.value][1],
        KIND_ZH[AmbiguityKind.MERGED_PAYMENT.value][1],
        KIND_ZH[AmbiguityKind.CROSS_PERIOD.value][1],
        KIND_ZH[AmbiguityKind.REFUND_COEXIST.value][1],
    )
    assert "拆分明细" in split and "分摊" in split
    assert "合并支付" in merged and "分摊" in merged
    assert "账期" in cross
    assert "红冲" in refund
    # 四条两两不同（逐条比，不做集合去重式断言）
    texts = [split, merged, cross, refund]
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            assert texts[i] != texts[j]


def test_verdict_suggestion_is_non_empty_on_real_verdicts():
    for case in (
        _case([_ch("w.csv:5", "128.00")], [_his("h.csv:3", "50.00"), _his("h.csv:4", "78.00")]),
        _case([_ch("w.csv:7", "100.00")], [_his("h.csv:7", "50.00"), _his("h.csv:8", "30.00")]),
        _case([_ch("w.csv:9", "240.00")], [_his("h.csv:11", "240.00", cross=True)]),
        _case([_ch("w.csv:8", "128.00")], [_his("h.csv:9", "128.00"), _his("h.csv:10", "42.00", refund=True)]),
        _case([_ch("w.csv:6", "66.00")], [_his("h.csv:5", "66.00"), _his("h.csv:6", "66.00")]),
    ):
        v = classify_taxonomy(case)
        assert v.suggestion.strip()
        assert v.suggestion == KIND_ZH[v.kind][1]
        assert v.suggestion in v.render()


# ───────────── 四、判别不误分类（边界与反例） ─────────────


def test_conservation_tolerance_boundary_is_inclusive():
    """差 0.01 判守恒（拆笔），差 0.02 判不守恒。

    院内导出常有分位抹零，容差取等号外沿会让「差一分钱」的对账每天报合并支付。
    """
    v = classify_taxonomy(
        _case([_ch("w.csv:1", "128.00")], [_his("h.csv:1", "50.00"), _his("h.csv:2", "77.99")])
    )
    assert v.kind == AmbiguityKind.SPLIT_PAYMENT.value, "差 0.01 属抹零，应判守恒"

    v2 = classify_taxonomy(
        _case([_ch("w.csv:1", "128.00")], [_his("h.csv:1", "50.00"), _his("h.csv:2", "77.98")])
    )
    assert v2.kind == AmbiguityKind.MERGED_PAYMENT.value, "差 0.02 超出容差，应判不守恒"


def test_refund_wins_over_period_and_amount_relations():
    """判据顺序：退费/跨账期（硬判据）优先于金额推断。

    一组「原单 + 红冲 + 越界 + 金额不守恒」的候选若被金额规则先判成合并支付，
    话术会把人引去查分摊，而真正该查的是红冲有没有生效。
    """
    v = classify_taxonomy(
        _case([_ch("w.csv:1", "128.00")],
              [_his("h.csv:1", "128.00"), _his("h.csv:2", "-42.00", refund=True, cross=True)])
    )
    assert v.kind == AmbiguityKind.REFUND_COEXIST.value


def test_cross_period_wins_over_conservation():
    """跨账期优先于守恒：金额对得上但落点不对，仍是跨账期问题。"""
    v = classify_taxonomy(
        _case([_ch("w.csv:1", "128.00")],
              [_his("h.csv:1", "50.00", cross=True), _his("h.csv:2", "78.00", cross=True)])
    )
    assert v.kind == AmbiguityKind.CROSS_PERIOD.value


def test_all_refund_rows_without_original_is_not_refund_coexist():
    """全是退费行、没有原单行 → 判**不是**「退费与原单并存」。

    「并存」这个词的要件是原单与退费同时在场。整组都是退费时把它报成并存，
    经纪人会去查「红冲有没有生效」—— 而这里根本没有原单可冲。
    """
    v = classify_taxonomy(
        _case([_ch("w.csv:1", "-86.00", refund=True)],
              [_his("h.csv:1", "-42.00", refund=True), _his("h.csv:2", "-44.00", refund=True)])
    )
    assert v.kind != AmbiguityKind.REFUND_COEXIST.value
    assert any(e.code == "refund" and "没有原单行" in e.reason for e in v.excluded)
    # -42 + -44 = -86 守恒，形态上就是「一笔被拆成两笔」
    assert v.kind == AmbiguityKind.SPLIT_PAYMENT.value
    assert v.diff_amount == "0.00"


def test_channel_side_fanout_is_also_judged():
    """多条渠道行认领同一条 HIS 记录（合并支付的另一形态）也要能判。

    只建模 HIS 侧扇出的话，这种「多渠道行 ↔ 一条 HIS 记录」会直接落
    UNKNOWN，而它恰恰是合并支付最常见的现场形态。
    """
    v = classify_taxonomy(
        _case([_ch("wechat.csv:5", "88.00"), _ch("wechat.csv:6", "40.00")],
              [_his("charge.csv:9", "128.00")])
    )
    assert v.kind == AmbiguityKind.SPLIT_PAYMENT.value, "88+40=128 守恒，应判拆笔"
    assert v.diff_amount == "0.00"


def test_channel_side_fanout_not_conserved_is_merged():
    v = classify_taxonomy(
        _case([_ch("wechat.csv:5", "60.00"), _ch("wechat.csv:6", "40.00")],
              [_his("charge.csv:9", "150.00")])
    )
    assert v.kind == AmbiguityKind.MERGED_PAYMENT.value
    assert v.diff_amount == "-50.00"


# ───────────── 五、与 Task 21 歧义判定的交互 ─────────────


def _verdict(kind: str, amounts, *, cross=False, refunds=(), periods=None,
             period=PERIOD) -> AmbiguityVerdict:
    cands = [
        AmbiguityCandidate(
            row_no=i + 3,
            source_file="charge_record.csv",
            amount=a,
            trade_time="2026-09-02T10:00:00+08:00",
            department="住院收费处",
            settle_type="微信",
            record_id=f"CHG-A{i + 3}",
            is_refund=i in refunds,
            cross_period=cross and i == 0,
            period=(periods[i] if periods else period),
        )
        for i, a in enumerate(amounts)
    ]
    return AmbiguityVerdict(
        channel="wechat",
        order_no="EX-WX-SPLIT-0001",
        kind=kind,
        candidates=cands,
        period=period,
        channel_row_no=5,
        source_file="wechat_tradebill.csv",
        error_code="WEAK_MATCH_PENDING" if kind == "weak_multiple" else "AMBIGUOUS_ENTITY",
    )


def test_case_from_verdict_keeps_every_candidate_and_its_source_position():
    """候选集原样透传：一条都不能少，行号必须还在。"""
    verdict = _verdict("split", ["50.00", "78.00"])
    case = case_from_verdict(verdict, channel_amount=Decimal("128.00"))
    assert len(case.his_rows) == 2
    assert [r.ref for r in case.his_rows] == [
        "charge_record.csv:3",
        "charge_record.csv:4",
    ]
    assert case.channel_total == Decimal("128.00")
    assert case.his_total == Decimal("128.00")
    v = classify_taxonomy(case)
    assert v.kind == AmbiguityKind.SPLIT_PAYMENT.value


def test_case_from_verdict_without_channel_amount_stays_unknown():
    """拿不到渠道金额时**不填 0** —— 填 0 会让守恒判据永远成立。"""
    verdict = _verdict("split", ["50.00", "78.00"])
    case = case_from_verdict(verdict, channel_amount=None)
    assert case.channel_rows == ()
    v = classify_taxonomy(case)
    assert v.kind == AmbiguityKind.UNKNOWN.value
    assert any(e.code == "fanout" for e in v.excluded)


def test_case_from_verdict_does_not_contradict_the_upstream_kind():
    """上游说 refund_and_original 的，下游必须也判退费并存。

    两层分类若给出不同结论，台账上会出现「形态写退费并存、建议写核对拆分」
    这种自相矛盾的行，经办人只能靠猜。
    """
    case = case_from_verdict(
        _verdict("refund_and_original", ["128.00", "42.00"], refunds={1}),
        channel_amount=Decimal("128.00"),
    )
    v = classify_taxonomy(case)
    assert v.kind == AmbiguityKind.REFUND_COEXIST.value
    assert MATCH_KIND_ZH[MatchKind.REFUND_AND_ORIGINAL.value][0] == "退费与原单并存"
    assert v.kind_zh == MATCH_KIND_ZH[MatchKind.REFUND_AND_ORIGINAL.value][0]


def test_case_from_verdict_cross_period_upstream_keeps_cross_period_downstream():
    case = case_from_verdict(
        _verdict("cross_period", ["240.00"], cross=True),
        channel_amount=Decimal("240.00"),
    )
    v = classify_taxonomy(case)
    assert v.kind == AmbiguityKind.CROSS_PERIOD.value


# ───────────── 六、与 Task 23 弱候选的交互 ─────────────


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _chan_rec(order_no="EX-WEAK-0001", amount="120.00", row=9) -> AlignRecord:
    return AlignRecord(
        channel="wechat", period=PERIOD, order_no=order_no, amount=Decimal(amount),
        trade_time=_t("2026-09-02 10:00:00"), source_file="wechat.csv", source_row_no=row,
    )


def _his_rec(order_no="CHG-A0020", amount="120.00", row=20,
             dept="门诊收费室") -> AlignRecord:
    return AlignRecord(
        channel="his", period=PERIOD, order_no=order_no, his_order_no=order_no,
        amount=Decimal(amount), trade_time=_t("2026-09-02 10:05:00"),
        department=dept, settle_type="微信", source_file="charge.csv", source_row_no=row,
    )


def test_weak_candidate_is_always_unknown_with_factors_explained():
    """弱候选一律 UNKNOWN，且必须附因子说明「为什么不可判定」。

    弱匹配只有金额 + 时间窗，连「是不是同一笔」都没有判据；在这之上再分型
    等于替人编，且编出来的成因会被当成结论写进签字版。
    """
    chan, his = _chan_rec(), _his_rec()
    case = case_from_weak_candidate(
        score_pair(chan, his), period=PERIOD, channel="wechat"
    )
    assert case.weak is True
    v = classify_taxonomy(case)
    assert v.kind == AmbiguityKind.UNKNOWN.value
    assert v.weak is True
    assert v.factors, "UNKNOWN 必须附因子说明"
    assert any("单号" in f or "金额" in f or "科室" in f or "结算" in f for f in v.factors)
    assert any(e.code == "weak" for e in v.excluded)
    assert "弱匹配" in v.detail


def test_weak_candidate_factors_name_the_unusable_ones():
    """导出侧没有的因子（科室/结算方式）要出现在说明里。

    医院里「渠道侧根本没有科室列」是常态，把它写出来，经办人才知道
    「科室一致」这一栏空白不是系统漏了，是数据里没有。
    """
    chan, his = _chan_rec(), _his_rec()
    cand: Candidate = score_pair(chan, his)
    assert cand.unusable_factors, "渠道侧无科室/结算方式列，应有不可用因子"
    case = case_from_weak_candidate(cand, period=PERIOD, channel="wechat")
    text = classify_taxonomy(case).render()
    assert "科室" in text or "结算方式" in text


def test_weak_case_without_factors_still_reports_a_placeholder():
    """调用方没给因子时要说「没给」，不能给一个空列表让人以为无需解释。"""
    case = _case([_ch("w.csv:9", "120.00")], [_his("h.csv:20", "120.00")], weak=True)
    v = classify_taxonomy(case)
    assert v.kind == AmbiguityKind.UNKNOWN.value
    assert v.factors and "未提供" in v.factors[0]


# ───────────── 七、落库与成因筛选 ─────────────


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_scan_schema(conn)
    return conn


def test_verdict_becomes_a_diff_ledger_row_with_its_kind():
    v = classify_taxonomy(
        _case([_ch("wechat.csv:5", "128.00")],
              [_his("charge.csv:3", "50.00"), _his("charge.csv:4", "78.00")],
              order="EX-WX-SPLIT-0001"),
    )
    items = verdict_to_diff_items(v, run_id="run-001")
    assert len(items) == 1
    item = items[0]
    assert item.check_path == f"{TAXONOMY_PATH_PREFIX}.ambiguity:EX-WX-SPLIT-0001"
    assert item.check_name == "一码付拆笔"
    assert item.ambiguity_kind == AmbiguityKind.SPLIT_PAYMENT.value
    assert item.period == PERIOD and item.channel == "wechat"
    assert item.evidence_total == 3


def test_ledger_row_never_advances_disposition():
    """分型**不推进状态**：待确认 + 需归因恒成立，且不写 resolved。"""
    v = classify_taxonomy(
        _case([_ch("wechat.csv:5", "128.00")],
              [_his("charge.csv:3", "50.00"), _his("charge.csv:4", "78.00")],
              order="EX-WX-SPLIT-0001"),
    )
    item = verdict_to_diff_items(v)[0]
    assert item.disposition == "待确认"
    assert item.needs_attribution is True
    assert item.resolved is False
    assert item.status == "fail"

    conn = _conn()
    upsert_diff_items(conn, verdict_to_diff_items(v))
    stored = list_diff_items(conn, PERIOD)[0]
    assert stored.disposition == "待确认"
    assert stored.needs_attribution is True
    assert stored.resolved is False
    assert stored.ambiguity_kind == AmbiguityKind.SPLIT_PAYMENT.value


def test_check_path_carries_the_order_no_so_two_rows_never_overwrite():
    """同期两条同成因但单号不同 → 两条台账行，不互相覆盖。"""
    conn = _conn()
    a = classify_taxonomy(
        _case([_ch("w.csv:5", "128.00")], [_his("h.csv:3", "50.00"), _his("h.csv:4", "78.00")],
              order="EX-A"),
    )
    b = classify_taxonomy(
        _case([_ch("w.csv:6", "128.00")], [_his("h.csv:5", "50.00"), _his("h.csv:6", "78.00")],
              order="EX-B"),
    )
    assert len(verdicts_to_diff_items([a, b])) == 2
    upsert_diff_items(conn, verdicts_to_diff_items([a, b]))
    rows = list_diff_items(conn, PERIOD)
    assert len(rows) == 2
    assert {r.check_path for r in rows} == {
        "taxonomy.ambiguity:EX-A",
        "taxonomy.ambiguity:EX-B",
    }


def test_ledger_round_trip_preserves_the_kind_and_survives_rewrites():
    """重扫同一账期：成因不丢、行数不增（覆盖而非追加）。"""
    conn = _conn()
    v = classify_taxonomy(
        _case([_ch("w.csv:5", "128.00")], [_his("h.csv:3", "50.00"), _his("h.csv:4", "78.00")],
              order="EX-A"),
    )
    upsert_diff_items(conn, verdict_to_diff_items(v, run_id="run-001"))
    upsert_diff_items(conn, verdict_to_diff_items(v, run_id="run-002"))
    rows = list_diff_items(conn, PERIOD)
    assert len(rows) == 1
    assert rows[0].ambiguity_kind == AmbiguityKind.SPLIT_PAYMENT.value
    assert rows[0].run_id == "run-002"


def test_ledger_rewriting_with_a_different_kind_updates_in_place():
    """同一单号重扫后成因变了（候选变了）→ 覆盖更新，不留两条。"""
    conn = _conn()
    split = classify_taxonomy(
        _case([_ch("w.csv:5", "128.00")], [_his("h.csv:3", "50.00"), _his("h.csv:4", "78.00")],
              order="EX-A"),
    )
    merged = classify_taxonomy(
        _case([_ch("w.csv:5", "128.00")], [_his("h.csv:3", "50.00"), _his("h.csv:4", "30.00")],
              order="EX-A"),
    )
    upsert_diff_items(conn, verdict_to_diff_items(split))
    upsert_diff_items(conn, verdict_to_diff_items(merged))
    rows = list_diff_items(conn, PERIOD)
    assert len(rows) == 1
    assert rows[0].ambiguity_kind == AmbiguityKind.MERGED_PAYMENT.value
    assert rows[0].check_name == "合并支付"


def test_panel_can_filter_the_ledger_by_cause():
    """面板「按成因分组」那条腿：按 kind 筛得动，且「其它」组能单独点开。"""
    conn = _conn()
    verdicts = [
        classify_taxonomy(
            _case([_ch("w.csv:5", "128.00")], [_his("h.csv:3", "50.00"), _his("h.csv:4", "78.00")],
                  order="EX-A"),
        ),
        classify_taxonomy(
            _case([_ch("w.csv:6", "240.00")], [_his("h.csv:11", "240.00", cross=True)],
                  order="EX-B"),
        ),
        classify_taxonomy(
            _case([_ch("w.csv:7", "66.00")], [_his("h.csv:5", "66.00"), _his("h.csv:6", "66.00")],
                  order="EX-C"),
        ),
    ]
    upsert_diff_items(conn, verdicts_to_diff_items(verdicts))

    # 一条口径差异（无成因），用来验证「其它」组不被算进不可判定
    from reconq.engine.period import DiffItem

    upsert_diff_items(conn, [DiffItem(period=PERIOD, channel="wechat",
                                      check_path="wechat.not_empty[out_trade_no]",
                                      check_name="商户订单号必填", status="fail",
                                      message="第 6 行商户订单号为空", diff_amount=None,
                                      evidence_total=1, source_file="wechat.csv",
                                      first_row_no=6, disposition="待确认",
                                      needs_attribution=False, run_id="run-001")])

    assert len(list_diff_items(conn, PERIOD, ambiguity_kind="split_payment")) == 1
    assert len(list_diff_items(conn, PERIOD, ambiguity_kind="cross_period")) == 1
    assert len(list_diff_items(conn, PERIOD, ambiguity_kind="unknown")) == 1
    others = list_diff_items(conn, PERIOD, ambiguity_kind="")
    assert len(others) == 1
    assert others[0].check_path.startswith("wechat.")
    # 不传该参数 = 全部 4 条
    assert len(list_diff_items(conn, PERIOD)) == 4


def test_group_counts_keep_check_diffs_out_of_the_unknown_bucket():
    """分组计数：口径差异（空串）单列，不混进「不可判定」。

    把查口径的问题算成「成因判不出来」会让不可判定那组虚高，而那正是
    最需要人推的情形。
    """
    conn = _conn()
    from reconq.engine.period import DiffItem

    unknown = classify_taxonomy(
        _case([_ch("w.csv:7", "66.00")], [_his("h.csv:5", "66.00"), _his("h.csv:6", "66.00")],
              order="EX-C"),
    )
    upsert_diff_items(conn, verdicts_to_diff_items([unknown]))
    upsert_diff_items(conn, [
        DiffItem(period=PERIOD, channel="wechat", check_path="wechat.a", check_name="口径",
                 status="fail", message="m", disposition="待确认", needs_attribution=False),
        DiffItem(period=PERIOD, channel="wechat", check_path="wechat.b", check_name="口径",
                 status="fail", message="m", disposition="待确认", needs_attribution=False),
    ])
    counts = count_diff_by_ambiguity(conn, PERIOD)
    assert counts == {"unknown": 1, "": 2}
    assert sum(counts.values()) == 3


def test_legacy_ledger_without_the_column_is_migrated_not_crashed():
    """老库没有 ``ambiguity_kind`` 列时走 ALTER 迁移，不能 KeyError。

    ``CREATE TABLE IF NOT EXISTS`` 对已建好的库不补列；不迁移的话，这个
    失败只在**升级后才出现**的老库上冒出来，而新库一切正常。
    """
    legacy = sqlite3.connect(":memory:", isolation_level=None)
    legacy.row_factory = sqlite3.Row
    legacy.execute(
        "CREATE TABLE period_diff_items ("
        "diff_id INTEGER PRIMARY KEY AUTOINCREMENT, period TEXT NOT NULL, "
        "channel TEXT NOT NULL, check_path TEXT NOT NULL, check_name TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'fail', message TEXT NOT NULL DEFAULT '', "
        "diff_amount TEXT, evidence_total INTEGER NOT NULL DEFAULT 0, "
        "source_file TEXT NOT NULL DEFAULT '', first_row_no INTEGER, "
        "disposition TEXT NOT NULL DEFAULT '待确认', needs_attribution INTEGER NOT NULL DEFAULT 0, "
        "run_id TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL DEFAULT '', "
        "resolved INTEGER NOT NULL DEFAULT 0, resolved_at TEXT NOT NULL DEFAULT '', "
        "resolved_run_id TEXT NOT NULL DEFAULT '')"
    )
    legacy.execute(
        "INSERT INTO period_diff_items (period, channel, check_path) VALUES (?,?,?)",
        (PERIOD, "wechat", "wechat.a"),
    )
    init_scan_schema(legacy)
    cols = {r["name"] for r in legacy.execute(f"PRAGMA table_info({DIFF_TABLE})")}
    assert "ambiguity_kind" in cols
    rows = list_diff_items(legacy, PERIOD)
    assert len(rows) == 1
    assert rows[0].ambiguity_kind == "", "老行的成因是空串，不是缺失"
    assert list_diff_items(legacy, PERIOD, ambiguity_kind="") == rows


def test_kind_index_is_actually_used_by_the_filter():
    """筛选走索引而不是全表扫 —— 建了索引一次都用不上是常见退化。"""
    conn = _conn()
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM period_diff_items WHERE period=? AND ambiguity_kind=?",
        (PERIOD, "split_payment"),
    ).fetchall()
    text = " ".join(str(r["detail"]) for r in plan)
    assert "ix_diff_ambiguity" in text, f"筛选未走 ix_diff_ambiguity：{text}"


# ───────────── 八、不变量与渲染 ─────────────


def test_amount_tolerance_matches_the_matcher_tolerance():
    """两处容差必须同值但各写一份常量，由测试锁死。"""
    from reconq.align.matcher import WEAK_MATCH_TOLERANCE

    assert AMOUNT_TOLERANCE == WEAK_MATCH_TOLERANCE == Decimal("0.01")


def test_verdict_is_immutable_and_does_not_mutate_the_case():
    case = _case([_ch("w.csv:5", "128.00")], [_his("h.csv:3", "50.00"), _his("h.csv:4", "78.00")])
    snapshot = case.to_dict()
    v = classify_taxonomy(case)
    with pytest.raises(Exception):
        v.kind = "unknown"  # type: ignore[misc]
    assert case.to_dict() == snapshot, "分型不得改动输入候选集"


def test_render_is_deterministic_and_carries_every_evidence_ref():
    case = _case([_ch("w.csv:5", "128.00")], [_his("h.csv:3", "50.00"), _his("h.csv:4", "78.00")])
    first = classify_taxonomy(case).render()
    second = classify_taxonomy(case).render()
    assert first == second
    for ref in ("w.csv:5", "h.csv:3", "h.csv:4"):
        assert ref in first
    assert "已排除的判据" in first
    assert "建议" in first


def test_to_dict_is_json_ready_and_carries_the_excluded_list():
    import json

    v = classify_taxonomy(
        _case([_ch("w.csv:6", "66.00")], [_his("h.csv:5", "66.00"), _his("h.csv:6", "66.00")])
    )
    payload = json.dumps(v.to_dict(), ensure_ascii=False)
    restored = json.loads(payload)
    assert restored["kind"] == "unknown"
    assert restored["is_unknown"] is True
    assert restored["excluded"], "UNKNOWN 的 excluded 不得在序列化时丢失"
    assert {e["code"] for e in restored["excluded"]} >= {"split", "merged"}
    assert restored["suggestion"]


def test_classification_never_raises_on_degenerate_input():
    """退化输入（空行、全零、账期空）出 UNKNOWN，不抛栈。"""
    for case in (
        _case([], []),
        _case([_ch("w:1", "0")], [_his("h:1", "0")]),
        _case([_ch("w:1", "10.00", period="")], [_his("h:1", "0.00", period="")]),
        TaxonomyCase(period="", channel="", order_no="", channel_rows=(), his_rows=()),
    ):
        v = classify_taxonomy(case)
        assert isinstance(v, TaxonomyVerdict)
        assert v.kind in {k.value for k in AmbiguityKind}


# ───────────── 九、真实样例端到端（走主路径） ─────────────

ALIGN_FIXTURES = "tests/fixtures/align/2026-W36"
TAXONOMY_FIXTURES = "tests/fixtures/align/taxonomy"


def _load_fixture(channel: str, relative: str, root: str = ALIGN_FIXTURES):
    """走主路径读样例：契约 → 文件读入 → 归一 → 对齐行。"""
    from reconq.align.matcher import records_from_bill
    from reconq.contracts.loader import load_contract
    from reconq.sources.format_probe import read_bill_file_isolated
    from reconq.store.bill_store import build_record

    contract = load_contract(
        f"contracts/{channel}.yml",
        {"period": PERIOD, "session_timezone": "Asia/Shanghai"},
    )
    rows, _probe, _dirty = read_bill_file_isolated(
        f"{root}/{relative}", contract.dataset.tables[0], channel, period=PERIOD
    )
    return records_from_bill(
        build_record(r, channel=channel, period=PERIOD, window=_window(), session_tz=TZ)
        for r in rows
    )


def _classify_outcome(outcome):
    """把一条歧义对齐结论分型（渠道金额取该渠道行净额）。"""
    assert outcome.ambiguity is not None
    chan_row = outcome.channel_record
    return classify_taxonomy(
        case_from_verdict(
            outcome.ambiguity,
            channel_amount=chan_row.net_amount if chan_row is not None else None,
        ),
        channel=outcome.key.channel,
        order_no=outcome.key.order_no,
    )


def test_real_wechat_fixture_classifies_into_distinct_causes():
    """真实样例上：四种成因必须真的分出四种，不能全落到一类。

    全部落成一类时台账读起来是「都对不上」，等于把 Task 21 的拒绝归并
    原样搬回来，这一层白加。
    """
    his_rows = _load_fixture("his", "his/charge_record_2026-W36.csv")
    wechat = _load_fixture("wechat", "wechat/wechat_tradebill_2026-W36.csv")
    res = align_all({"wechat": wechat}, his_rows, period=PERIOD, window=_window())

    verdicts = {
        o.key.order_no: _classify_outcome(o)
        for o in res.channels["wechat"].outcomes
        if o.ambiguity is not None
    }
    kinds = {no: v.kind for no, v in verdicts.items()}

    # 渠道 128.00 = HIS 50.00 + 78.00 → 金额守恒的一码付拆笔
    assert kinds["EX-WX-SPLIT-0001"] == AmbiguityKind.SPLIT_PAYMENT.value
    # 渠道 66.00 vs HIS 66.00 + 66.00，金额全同 → 信息耗尽
    assert kinds["EX-WX-SAME-0001"] == AmbiguityKind.UNKNOWN.value
    # 候选含 is_refund 红冲行
    assert kinds["EX-WX-REFUND-0001"] == AmbiguityKind.REFUND_COEXIST.value
    # 候选收费时间 2026-09-07 00:00:01 越过周一零点
    assert kinds["EX-WX-CROSS-0001"] == AmbiguityKind.CROSS_PERIOD.value
    # 四种分出三种以上不同结论
    assert len(set(kinds.values())) >= 3


def test_real_fixture_verdicts_carry_locatable_evidence_and_suggestions():
    his_rows = _load_fixture("his", "his/charge_record_2026-W36.csv")
    wechat = _load_fixture("wechat", "wechat/wechat_tradebill_2026-W36.csv")
    res = align_all({"wechat": wechat}, his_rows, period=PERIOD, window=_window())
    n = 0
    for outcome in res.channels["wechat"].outcomes:
        if outcome.ambiguity is None:
            continue
        v = _classify_outcome(outcome)
        n += 1
        assert v.suggestion.strip()
        assert v.evidence, f"{outcome.key.order_no} 的分型没有证据行"
        for ref in v.evidence:
            assert ":" in ref, f"证据行 {ref} 必须能定位到 文件:行号"
        assert v.period == PERIOD
        if v.is_unknown:
            assert v.excluded, f"{outcome.key.order_no} 判 UNKNOWN 却没有排除清单"
    assert n >= 4


def test_four_shape_taxonomy_fixtures_each_classify_to_their_own_cause():
    """四形态各自的脱敏样例（``tests/fixtures/align/taxonomy/``）逐一判对。

    这套夹具是本任务**新增**的最小样本集：每种成因一小撮，判别规则的
    回归基线就落在这里 —— 规则一改，先看这四条还对不对。
    """
    expected = {
        "split": AmbiguityKind.SPLIT_PAYMENT.value,
        "merged": AmbiguityKind.MERGED_PAYMENT.value,
        "cross": AmbiguityKind.CROSS_PERIOD.value,
        "refund": AmbiguityKind.REFUND_COEXIST.value,
    }
    for name, kind in expected.items():
        his_rows = _load_fixture("his", f"his/charge_record_{name}.csv", TAXONOMY_FIXTURES)
        wechat = _load_fixture("wechat", f"wechat/wechat_tradebill_{name}.csv", TAXONOMY_FIXTURES)
        assert his_rows, f"{name} 夹具的 HIS 侧读不出行"
        assert wechat, f"{name} 夹具的渠道侧读不出行"
        res = align_all({"wechat": wechat}, his_rows, period=PERIOD, window=_window())
        ambiguous = [
            o for o in res.channels["wechat"].outcomes
            if o.ambiguity is not None and o.status == MatchStatus.AMBIGUOUS.value
        ]
        assert ambiguous, f"{name} 夹具没有产生歧义行，判别无从谈起"
        v = _classify_outcome(ambiguous[0])
        assert v.kind == kind, (
            f"{name} 形态判成 {v.kind}，应为 {kind}：{v.render()}"
        )
        assert v.suggestion.strip()
        assert v.evidence


def test_fixture_rows_are_desensitised_placeholders_only():
    """夹具里不得出现疑似真实的患者/单据标识。"""
    from pathlib import Path

    for path in Path(TAXONOMY_FIXTURES).rglob("*.csv"):
        text = path.read_text(encoding="utf-8-sig")
        assert "脱敏" in text or "EX-" in text, f"{path} 缺少脱敏占位标记"
        for token in ("身份证", "patient_name", "手机号"):
            assert token not in text, f"{path} 出现疑似患者可识别列 {token}"
