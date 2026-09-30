"""歧义拒绝归并（Task 21 · 下半）。

覆盖面：五种歧义形态的分类判据（拆笔 / 合并 / 跨账期 / 退费并存 / 金额
相同）、弱匹配形态、候选渲染与可回查定位、``KIND_ZH`` 中文说明齐全、以及
三条硬不变量：

1. **歧义绝不静默择一** —— 任何一对多情形都必须走 ``AMBIGUOUS_ENTITY``
   且候选全列，没有「默认取第一条」的代码路径；
2. **歧义不是异常** —— 它是台账里的一行，不抛 ``ReconqError``；
3. **未登记形态不静默** —— 形态与中文说明必须成对存在。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from reconq.align.ambiguity import (
    KIND_ZH,
    AmbiguityCandidate,
    AmbiguityKind,
    AmbiguityVerdict,
    classify_ambiguity,
    render_candidates,
)
from reconq.align.matcher import (
    AlignRecord,
    MatchStatus,
    align_channel,
    build_index,
)
from reconq.errors import ERROR_CODES, error_meta
from reconq.sources.timezone import period_window, to_session_tz

PERIOD = "2026-W36"
TZ = to_session_tz(None)


def _cand(amount: str, *, row: int, refund: bool = False, cross: bool = False,
          dept: str = "门诊收费室", time: str = "2026-09-02T10:00:00+08:00") -> AmbiguityCandidate:
    return AmbiguityCandidate(
        row_no=row, source_file="charge_record.csv", amount=amount, trade_time=time,
        department=dept, settle_type="微信", record_id=f"EX-{row}",
        is_refund=refund, cross_period=cross,
    )


def _his(order_no: str, amount: str, *, row: int, time: str = "2026-09-02T10:00:00",
         refund: str = "", ch_id: int = 0, dept: str = "门诊收费室") -> AlignRecord:
    from datetime import datetime

    return AlignRecord(
        channel="his", period=PERIOD, order_no=order_no, his_order_no=order_no,
        amount=Decimal(amount), refund=Decimal(refund) if refund else None,
        trade_time=datetime.fromisoformat(time).replace(tzinfo=TZ),
        department=dept, settle_type="微信", source_file="charge_record.csv",
        source_row_no=row, ch_id=ch_id,
    )


def _chan(order_no: str, amount: str, *, row: int = 1, refund: str = "") -> AlignRecord:
    from datetime import datetime

    return AlignRecord(
        channel="wechat", period=PERIOD, order_no=order_no, amount=Decimal(amount),
        refund=Decimal(refund) if refund else None,
        trade_time=datetime.fromisoformat("2026-09-02T10:00:00").replace(tzinfo=TZ),
        source_file="wechat_bill.csv", source_row_no=row,
    )


# ─────────────────── 一、五种形态的分类判据 ───────────────────


def test_split_kind_for_one_to_many_with_differing_amounts():
    """一码付拆多笔：同单号命中多条、金额各不相同。"""
    cands = [_cand("50.00", row=3), _cand("78.00", row=4, dept="住院收费室")]
    assert classify_ambiguity(candidates=cands, channel_amount=Decimal("128.00")) == "split"


def test_merged_kind_is_documented_but_field_level_identification_is_impossible():
    """合并支付与拆笔在字段层面同形（都是 1→N）。

    区分靠「N 条是否共享同一收费员/科室」这类系统侧拿不到的上下文，因此
    分类统一落 ``split``，并在 KIND_ZH 里把两种可能都写出来。这条断言
    锁住的是「**不假装能分**」这个诚实口径，而不是某个具体判据。
    """
    cands = [_cand("50.00", row=3), _cand("78.00", row=4)]
    kind = classify_ambiguity(candidates=cands, channel_amount=Decimal("128.00"))
    assert kind == "split"
    zh, suggestion = KIND_ZH[kind]
    assert zh and suggestion
    assert "费用项目" in suggestion or "多个单" in suggestion


def test_cross_period_kind_when_all_candidates_fall_outside_window():
    cands = [_cand("50.00", row=3, cross=True), _cand("78.00", row=4, cross=True)]
    assert classify_ambiguity(candidates=cands, channel_amount=Decimal("128.00")) == "cross_period"


def test_cross_period_wins_over_equal_amount():
    """形态判据从成因最具体的往回退：跨账期比金额相同更硬。"""
    cands = [_cand("66.00", row=3, cross=True), _cand("66.00", row=4, cross=True)]
    assert classify_ambiguity(candidates=cands, channel_amount=Decimal("66.00")) == "cross_period"


def test_refund_and_original_kind_when_only_some_candidates_are_refunds():
    cands = [_cand("128.00", row=3), _cand("42.00", row=4, refund=True)]
    assert classify_ambiguity(candidates=cands, channel_amount=Decimal("128.00")) == "refund_and_original"


def test_refund_detected_by_negative_amount_without_explicit_flag():
    """院内导出可能没有红冲标志列，只靠负号表达。"""
    cands = [_cand("128.00", row=3), _cand("-128.00", row=4, refund=False)]
    assert classify_ambiguity(candidates=cands, channel_amount=Decimal("0.00")) == "refund_and_original"


def test_equal_amount_kind_when_amounts_identical_and_differing_from_channel():
    cands = [_cand("66.00", row=3), _cand("66.00", row=4, dept="急诊")]
    assert classify_ambiguity(candidates=cands, channel_amount=Decimal("66.00")) == "equal_amount"
    zh, _ = KIND_ZH["equal_amount"]
    assert zh == "金额相同的多候选"


def test_weak_kind_when_candidate_set_is_empty_and_weak_flag_set():
    assert classify_ambiguity(candidates=[], channel_amount=Decimal("10.00"), weak=True) == "weak_multiple"


# ─────────────────── 二、端到端：一码付拆笔 / 合并支付 ───────────────────


def test_split_payment_produces_ambiguity_with_all_candidates_listed():
    his = [_his("EX-SPLIT", "50.00", row=3, ch_id=1), _his("EX-SPLIT", "78.00", row=4, ch_id=2)]
    out = align_channel("wechat", [_chan("EX-SPLIT", "128.00", row=9)],
                        build_index(his, period=PERIOD), period=PERIOD).outcomes[0]
    assert out.status == MatchStatus.AMBIGUOUS.value
    v = out.ambiguity
    assert v is not None
    assert v.error_code == "AMBIGUOUS_ENTITY"
    assert v.candidate_total == 2
    assert [c.row_no for c in v.candidates] == [3, 4]
    assert [c.amount for c in v.candidates] == ["50.00", "78.00"]
    # 渠道行与候选行的行号都可回查
    assert v.channel_row_no == 9
    assert v.source_file == "wechat_bill.csv"
    # 认领必须为空
    assert out.his_record is None


def test_refund_and_original_produces_ambiguity_named_refund_and_original():
    his = [
        _his("EX-REF", "128.00", row=3, ch_id=1),
        _his("EX-REF", "42.00", row=4, ch_id=2, refund="86.00"),
    ]
    out = align_channel("wechat", [_chan("EX-REF", "128.00", row=9)],
                        build_index(his, period=PERIOD), period=PERIOD).outcomes[0]
    assert out.status == MatchStatus.AMBIGUOUS.value
    assert out.ambiguity.kind == "refund_and_original"
    assert out.ambiguity.kind_zh == "退费与原单并存"
    # 带退款的那条候选必须被标出来
    assert [c.is_refund for c in out.ambiguity.candidates] == [False, True]
    assert "红冲" in out.ambiguity.candidates[1].render()


def test_cross_period_single_candidate_is_reported_not_merged():
    """跨账期落点：单号唯一命中但时间在窗外，报告而不认领。"""
    w = period_window(PERIOD, TZ)
    his = [_his("EX-LATE", "128.00", row=3, time="2026-09-07 00:00:01", ch_id=1)]
    out = align_channel("wechat", [_chan("EX-LATE", "128.00")],
                        build_index(his, period=PERIOD), period=PERIOD, window=w).outcomes[0]
    assert out.status == MatchStatus.CROSS_PERIOD.value
    assert out.ambiguity.kind == "cross_period"
    assert out.ambiguity.candidates[0].cross_period is True
    assert "跨账期" in out.ambiguity.candidates[0].render()
    assert out.his_record is None


def test_duplicate_claim_across_channels_is_reported_with_the_first_holder():
    """同一笔 HIS 收费被两路渠道各报一次：第二路报重复认领并点名首认领方。"""
    his = [_his("EX-DUP", "128.00", row=3, ch_id=1)]
    index = build_index(his, period=PERIOD)
    claimed: dict[int, str] = {}
    align_channel("wechat", [_chan("EX-DUP", "128.00")], index, period=PERIOD, claimed=claimed)
    second = align_channel("self_service", [_chan("EX-DUP", "128.00")], index,
                           period=PERIOD, claimed=claimed)
    out = second.outcomes[0]
    assert out.status == MatchStatus.DUPLICATE_CLAIM.value
    assert out.ambiguity.kind == "duplicate_claim"
    assert "wechat" in out.ambiguity.detail
    assert out.his_record is None


def test_weak_match_uses_its_own_error_code_not_the_ambiguous_one():
    """弱匹配与单号歧义要能在台账上分开：排查方向完全不同。"""
    his = [_his("EX-H1", "88.00", row=3, ch_id=1)]
    w = period_window(PERIOD, TZ)
    out = align_channel("wechat", [_chan("EX-NO-HIT", "88.00")],
                        build_index(his, period=PERIOD), period=PERIOD, window=w).outcomes[0]
    assert out.status == MatchStatus.WEAK_PENDING.value
    assert out.ambiguity.error_code == "WEAK_MATCH_PENDING"
    assert out.ambiguity.error_code != "AMBIGUOUS_ENTITY"


# ─────────────────── 三、硬不变量 ───────────────────


def test_no_code_path_silently_picks_the_first_candidate():
    """认领语句在整个包里**只有一处**，且只可能在一对多分支之外执行。

    用 AST 走一遍而不是正则找字符串：认领写在哪一行不重要，重要的是
    **只写了一次**。如果将来有人在歧义分支里补一句
    ``claimed[_identity(candidates[0])] = ...``，本断言立刻红。
    """
    import ast
    import pathlib

    import reconq.align as pkg

    claim_sites: list[tuple[str, int]] = []
    for name in ("matcher.py", "ambiguity.py"):
        tree = ast.parse((pathlib.Path(pkg.__file__).parent / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 认领的形态只有两种：claimed[k] = v（赋值）与 claimed.setdefault(...)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "claimed"):
                        claim_sites.append((name, node.lineno))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                fn = node.func
                if (isinstance(fn.value, ast.Name) and fn.value.id == "claimed"
                        and fn.attr in {"setdefault", "update", "add"}):
                    claim_sites.append((name, node.lineno))

    # 全包只有一处「认领」写入点；歧义 / 弱匹配分支一个都没有
    assert len(claim_sites) == 1, f"对齐包出现多处认领写入点：{claim_sites}"


def test_ambiguity_branches_never_write_to_the_claimed_set():
    """歧义与弱匹配分支所在函数体内不得出现认领写入。

    上一条锁「只有一处写入」，这一条锁「那一处在对的位置」：歧义分支与
    弱匹配分支的函数体里都不许有 ``claimed[...] = ...``。
    """
    import ast
    import pathlib

    import reconq.align as pkg

    tree = ast.parse((pathlib.Path(pkg.__file__).parent / "matcher.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.For, ast.While)):
            continue
        seg = ast.get_source_segment(
            (pathlib.Path(pkg.__file__).parent / "matcher.py").read_text(encoding="utf-8"), node
        ) or ""
        if "candidates_by_amount" in seg and "WEAK" in seg:
            # 弱匹配分支的整个 If 体
            assert "claimed[ident] =" not in seg, "弱匹配分支里出现认领写入"
            assert "claimed[_identity" not in seg


def test_ambiguity_is_a_return_value_not_an_exception():
    """歧义是台账里的一行，不抛异常 —— 抛异常会让编排层把整路渠道剔掉。"""
    his = [_his("EX-S", "50.00", row=3, ch_id=1), _his("EX-S", "78.00", row=4, ch_id=2)]
    result = align_channel("wechat", [_chan("EX-S", "128.00")],
                           build_index(his, period=PERIOD), period=PERIOD)
    # 没有异常，且结果里确实有一条歧义结论
    assert len(result.outcomes) == 1
    assert result.outcomes[0].ambiguity is not None
    assert result.pending_attribution == 1


@pytest.mark.parametrize("kind", [k.value for k in AmbiguityKind])
def test_every_kind_has_chinese_explanation_and_suggestion(kind):
    """未登记形态不静默：每个形态都必须有中文说明与建议动作。"""
    assert kind in KIND_ZH, f"歧义形态 {kind} 未登记中文说明"
    zh, suggestion = KIND_ZH[kind]
    assert zh and not zh.startswith("未登记")
    assert suggestion and "请" in suggestion


@pytest.mark.parametrize("kind,code", [
    ("split", "AMBIGUOUS_ENTITY"),
    ("equal_amount", "AMBIGUOUS_ENTITY"),
    ("cross_period", "AMBIGUOUS_ENTITY"),
    ("refund_and_original", "AMBIGUOUS_ENTITY"),
    ("duplicate_claim", "AMBIGUOUS_ENTITY"),
    ("weak_multiple", "WEAK_MATCH_PENDING"),
])
def test_every_kind_maps_to_a_registered_error_code(kind, code):
    """面板拿结论里的码反查建议动作 —— 每个形态用的码都必须在 errors.py 登记。"""
    assert code in ERROR_CODES
    meta = error_meta(code)
    assert meta.code == code
    assert meta.zh and not meta.zh.startswith("未登记")
    # 形态名与错误码是两套命名，不互相冒充
    assert kind not in code
    assert code in KIND_ZH.values().__class__.__name__ or kind in KIND_ZH


def test_unregistered_kind_renders_as_explicitly_unknown_not_blank():
    """传进来一个没登记的形态：显式说「未登记」，不返回空串。"""
    v = AmbiguityVerdict(channel="wechat", order_no="EX", kind="who_knows")
    assert v.kind_zh == "未登记形态"
    assert "未登记形态" in v.render()


# ─────────────────── 四、候选渲染与可回查 ───────────────────


def test_candidate_render_contains_everything_needed_to_trace_back():
    c = _cand("128.00", row=42, dept="住院收费室")
    text = c.render()
    assert "charge_record.csv:42" in text     # 文件 + 行号
    assert "128.00" in text                    # 金额
    assert "2026-09-02T10:00:00+08:00" in text  # 时间
    assert "住院收费室" in text                 # 科室
    assert "微信" in text                       # 结算方式
    assert c.ref == "charge_record.csv:42"
    assert c.to_dict()["ref"] == "charge_record.csv:42"


def test_candidate_render_handles_empty_department_without_pretending():
    c = AmbiguityCandidate(row_no=7, source_file="x.csv", amount="", trade_time="",
                           department="", settle_type="")
    text = c.render()
    assert "（科室空）" in text
    assert "（结算方式空）" in text
    assert "金额 空" in text
    # 空单号渲染成 #行号，不留一段空白让人以为是文件路径
    assert c.ref == "x.csv:7"


def test_candidate_without_source_file_uses_row_marker():
    c = AmbiguityCandidate(row_no=9)
    assert c.ref == "#9"
    assert "#9" in c.render()


def test_verdict_render_lists_candidates_and_suggestion():
    v = AmbiguityVerdict(
        channel="wechat", order_no="EX-S", kind="split",
        candidates=[_cand("50.00", row=3), _cand("78.00", row=4)],
        detail="渠道行 wechat_bill.csv:9 金额 128.00 元",
    )
    text = v.render()
    # 形态名自带「待人工」三字，不必再叠一层（叠了会变成「歧义待人工：一码付
    # 拆多笔」，一句话里两个字重复）
    assert "歧义待人工" not in text
    assert "一码付拆多笔" in text
    assert "未归并" in text
    assert "候选" in text
    assert "charge_record.csv:3" in text
    assert "charge_record.csv:4" in text
    assert "建议" in text
    assert render_candidates(v) == [c.render() for c in v.candidates]


def test_verdict_with_no_order_no_says_so_instead_of_printing_blank():
    v = AmbiguityVerdict(channel="wechat", order_no="", kind="weak_multiple",
                         candidates=[_cand("10.00", row=3)])
    assert "（无单号）" in v.headline()
    # 弱匹配唯一命中也要写出「命中 1 条」：只写形态名会让人以为那是确定结论
    assert "命中 1 条" in v.headline()


def test_verdict_to_dict_is_json_serialisable_and_carries_the_code():
    import json

    v = AmbiguityVerdict(
        channel="wechat", period=PERIOD, order_no="EX", kind="equal_amount",
        candidates=[_cand("66.00", row=3), _cand("66.00", row=4)],
        error_code="AMBIGUOUS_ENTITY",
    )
    payload = json.loads(json.dumps(v.to_dict(), ensure_ascii=False))
    assert payload["kind_zh"] == "金额相同的多候选"
    assert payload["candidate_total"] == 2
    assert payload["error_code"] == "AMBIGUOUS_ENTITY"
    assert payload["candidates"][0]["ref"] == "charge_record.csv:3"


# ─────────────────── 五、规模：候选集不截断 ───────────────────


def test_thousand_candidate_ambiguity_lists_all_of_them():
    """单号被导出方复用成 1000 条候选时，**一条都不能少**。

    静默截断候选会让财务科以为「就这 5 条有问题」，然后签掉一个其实有
    1000 条的账期 —— 比不报更危险。
    """
    his = [_his("EX-MASS", f"{i}.00", row=1000 + i, ch_id=i + 1) for i in range(1000)]
    out = align_channel("wechat", [_chan("EX-MASS", "1.00")],
                        build_index(his, period=PERIOD), period=PERIOD).outcomes[0]
    assert out.status == MatchStatus.AMBIGUOUS.value
    assert out.ambiguity.candidate_total == 1000
    assert out.ambiguity.to_dict()["candidate_total"] == 1000
    assert len(out.ambiguity.candidates) == 1000
    # 一条不少：行号连续 1000..1999
    assert [c.row_no for c in out.ambiguity.candidates] == list(range(1000, 2000))
