"""归因证据一致性反幻觉核对（Task 32）。

五类规定断言逐条对应：

1. **金额一致 / 不一致** —— 草稿里的金额字面量必须能在**它引用的那些行**上
   找到。核心反例是「引用真实行号但说错金额」：ref 合法、语气笃定、结论
   具体，而金额在那一行上根本不存在。这类草稿比「ref 越界」危险得多 ——
   越界的会被拒掉，而它带着正确行号走进经办确认流，经办人看到「有据可查」
   于是签字。
2. **时间一致 / 不一致** —— 日期、时刻、ISO 周逐项比对；**时区写法不同但
   同一时刻视作一致**（草稿写 ``...T02:00:00Z``、证据行是 ``10:00:00+08:00``
   必须判一致，判不一致等于逼模型一律写院内本地时间）。
3. **越界单号** —— 草稿引用的单号必须在证据包内。
4. **cause 与证据矛盾** —— 声明 ``split_payment`` 而证据里没有一拆多扇出。
5. **草稿被拒** —— 任一 mismatch ⇒ 整条拒、标待人工判定、**不改写模型文本**、
   不进经办确认流。

外加几条**容易被糊弄过去**的性质：

* 判定基准是「它引用的行」：引用 :17 却搬用 :18 上的金额，同样记 mismatch
  —— 金额本身并不假，假的是「据以论证的那一行」；
* 核不了的项**既不判通过也不判失败**：成因映射不到受控形态时形态核对整项
  跳过并留 note，裸整数（「共 2 条候选」）不当金额判；
* 百分比与文件名先抽掉：「占 0.5%」「wechat_bill_2026-W36.csv」里的数字
  不能被当成金额或单号；
* 统计台账给提示词迭代用：一致率、核对项分布、空台账的**空串而非 0.00%**。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Optional

import pytest

from reconq.align.bundle import EvidenceBundle, build_evidence_bundle
from reconq.align.matcher import AlignRecord, align_channel, build_index
from reconq.align.verify_draft import (
    MISMATCH_ZH,
    REJECT_CODE,
    DraftRejection,
    MismatchKind,
    VerificationLedger,
    cause_form_outcome,
    draft_rejection,
    map_cause_kind,
    pending_manual_note,
    screen_drafts,
    verify_draft,
)
from reconq.contracts.loader import load_contract
from reconq.errors import ERROR_CODES, error_meta
from reconq.run_summary import build_run_summary
from reconq.runlog import LOG_EVENTS
from reconq.sources.timezone import HOSPITAL_TZ, period_window

PERIOD = "2026-W36"
TZ = HOSPITAL_TZ
CONTRACT = "contracts/wechat.yml"


def _t(text: str):
    from datetime import datetime

    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _chan(
    order_no: str,
    amount: str,
    *,
    time: str = "2026-09-02 10:00:00",
    row: int = 17,
    refund: str = "",
    ch_id: int = 1,
    trade_type: str = "",
    **kw,
) -> AlignRecord:
    return AlignRecord(
        channel="wechat",
        period=PERIOD,
        order_no=order_no,
        amount=Decimal(amount),
        refund=Decimal(refund) if refund else None,
        trade_type=trade_type,
        trade_time=_t(time),
        source_file="wechat_bill.csv",
        source_row_no=row,
        ch_id=ch_id,
        **kw,
    )


def _his(
    order_no: str,
    amount: str,
    *,
    time: str = "2026-09-02 10:00:00",
    row: int = 31,
    dept: str = "门诊收费室",
    settle: str = "微信",
    ch_id: int = 2,
    refund: str = "",
    trade_type: str = "",
) -> AlignRecord:
    # ``is_refund`` 是 AlignRecord 上的**派生属性**（refund 列非零 或
    # trade_type 写着退费/红冲），不是构造参数 —— 走 refund / trade_type
    # 两条通道而不是硬塞一个布尔进去。
    return AlignRecord(
        channel="his",
        period=PERIOD,
        order_no=order_no,
        his_order_no=order_no,
        amount=Decimal(amount),
        refund=Decimal(refund) if refund else None,
        trade_type=trade_type,
        trade_time=_t(time),
        department=dept,
        settle_type=settle,
        source_file="charge_record.csv",
        source_row_no=row,
        ch_id=ch_id,
    )


def _bundle(*, channel_rows=(), his_rows=(), channel: str = "wechat", window: bool = True):
    index = build_index(list(his_rows), period=PERIOD)
    result = align_channel(
        channel,
        list(channel_rows),
        index,
        period=PERIOD,
        window=period_window(PERIOD, TZ) if window else None,
    )
    assert len(result.outcomes) == 1, [o.status for o in result.outcomes]
    return build_evidence_bundle(
        result.outcomes[0],
        contract=load_contract(CONTRACT, {"period": PERIOD}),
        window=period_window(PERIOD, TZ) if window else None,
    )


def _channel_only_bundle() -> EvidenceBundle:
    """单边账：渠道有 86.00 元、HIS 查无此笔。差异行 = 渠道行。"""
    return _bundle(channel_rows=[_chan("EX-1", "86.00")])


def _split_bundle() -> EvidenceBundle:
    """一码付拆笔：一条渠道行 128.00 对上两条 HIS（50.00 + 78.00）。"""
    return _bundle(
        channel_rows=[_chan("EX-1", "128.00")],
        his_rows=[_his("EX-1", "50.00", row=31), _his("EX-1", "78.00", row=32, ch_id=3)],
    )


def _draft(
    cause: str = "导出方漏传",
    confidence: float = 0.7,
    refs: Optional[list] = None,
    explanation: str = "",
    bundle: Optional[EvidenceBundle] = None,
) -> dict[str, Any]:
    if refs is None:
        refs = [bundle.payload["diff_row"]["ref"]] if bundle else ["wechat_bill.csv:17"]
    return {
        "cause": cause,
        "confidence": confidence,
        "evidence_refs": list(refs),
        "explanation_zh": explanation or "x",
    }


def _ref(bundle: EvidenceBundle, index: int = 0) -> str:
    return str(bundle.payload["candidate_rows"][index]["ref"])


# —— 1. 金额核对 ————————————————————————————————————————————————


def test_amount_matching_the_cited_row_is_consistent():
    """草稿说的金额与所引行一致 → 通过。"""
    bundle = _channel_only_bundle()
    draft = _draft(
        explanation="渠道账单 wechat_bill.csv:17 有一笔 86.00 元，HIS 侧查无对应收费记录。",
        bundle=bundle,
    )
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]
    assert result.checked["amount"] == 1


def test_amount_quoted_wrong_on_a_real_row_is_rejected():
    """**本任务的核心反例**：ref 真实存在、金额说错。

    这类草稿过得了 Task 28 的越界筛查（ref 合法），却带着正确行号走进
    经办确认流。必须被金额核对挡住，且**不得**被自动改写成 86.00。
    """
    bundle = _channel_only_bundle()
    draft = _draft(
        explanation="渠道账单 wechat_bill.csv:17 有一笔 66.00 元，HIS 侧无对应记录。",
        bundle=bundle,
    )
    result = verify_draft(draft, bundle)
    assert not result.consistent
    assert MismatchKind.AMOUNT in result.mismatch_kinds
    mismatch = next(m for m in result.mismatches if m.kind == MismatchKind.AMOUNT)
    # claimed 原样抄下模型写的那一串（含单位），不改写
    assert mismatch.claimed.startswith("66.00")
    # 证据行上的真值 86.00 在 detail 里点名，经办人才知道该信哪个
    assert "wechat_bill.csv:17" in mismatch.detail
    # 不改写：草稿原文逐字保留
    assert draft["explanation_zh"].count("66.00") == 1
    assert "86.00" not in draft["explanation_zh"]


def test_amount_from_a_row_it_did_not_cite_is_still_a_mismatch():
    """判定基准是「它引用的行」：搬用候选行的金额同样记 mismatch。

    金额本身在证据包里并不假，假的是「据以论证的那一行」—— 草稿说
    「据 :17 金额 50.00」，而 50.00 在 :31 上。
    """
    bundle = _split_bundle()
    diff_ref = str(bundle.payload["diff_row"]["ref"])
    draft = _draft(explanation=f"据 {diff_ref}，渠道侧金额 50.00 元。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert not result.consistent
    assert MismatchKind.AMOUNT in result.mismatch_kinds


def test_citing_two_rows_legitimises_either_amount():
    """两行都引了，两个金额都认 —— 反例的正例。"""
    bundle = _split_bundle()
    diff_ref = str(bundle.payload["diff_row"]["ref"])
    cand_ref = _ref(bundle, 0)
    draft = _draft(
        explanation=f"{diff_ref} 渠道侧 128.00 元，{cand_ref} HIS 侧 50.00 元。",
        refs=[diff_ref, cand_ref],
        bundle=bundle,
    )
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]
    assert result.checked["amount"] == 2


def test_period_diff_amount_is_a_legitimate_amount_source():
    """引用本期差额（不在行上）合法 —— 它正是「该去查哪条账」那个数。"""
    bundle = _bundle(
        channel_rows=[_chan("EX-1", "128.00")],
        his_rows=[_his("EX-1", "50.00", row=31)],
    )
    diff_amount = bundle.payload["discrepancy"]["diff_amount"]
    assert diff_amount, "夹具前提：本期确有差额"
    draft = _draft(
        explanation=f"本期差额 {diff_amount} 元来自这一条未认领的渠道记录。", bundle=bundle
    )
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]


def test_tolerated_amounts_do_not_fight_over_sub_cent():
    """1 分以内视作一致（0.01 容差，与金额守恒口径同值）。"""
    bundle = _channel_only_bundle()
    draft = _draft(explanation="渠道侧 86.01 元未认领。", bundle=bundle)
    assert verify_draft(draft, bundle).consistent


def test_bare_integers_are_not_amounts():
    """裸整数不当金额：「共 2 条候选行」判成 mismatch 会误杀正确草稿。

    误杀一旦发生，经办人会开始忽略「已拒」标记，反幻觉核对就白做了。
    """
    bundle = _split_bundle()
    draft = _draft(explanation="共 2 条候选行，均未认领。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]
    assert result.checked["amount"] == 0
    assert any("未出现金额字面量" in note for note in result.notes)


def test_percentages_are_stripped_before_amount_extraction():
    """「占 0.5%」里的 0.5 不是金额；先抽百分比再抽金额。"""
    bundle = _channel_only_bundle()
    draft = _draft(explanation="该笔占本期合计 0.5%，但 HIS 侧查无此笔。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]
    assert result.checked["amount"] == 0


def test_thousands_separator_and_currency_symbol_are_parsed():
    """「1,020.00 元」与「¥86.00」都能对上。"""
    bundle = _channel_only_bundle()
    ok = verify_draft(_draft(explanation="渠道侧 ¥86.00 未认领。", bundle=bundle), bundle)
    assert ok.consistent, [m.to_dict() for m in ok.mismatches]


# —— 2. 时间核对 ————————————————————————————————————————————————


def test_time_matching_the_cited_row_is_consistent():
    bundle = _channel_only_bundle()
    draft = _draft(explanation="该笔交易时间 2026-09-02 10:00:00，HIS 侧无记录。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]
    assert result.checked["time"] >= 1


def test_same_instant_in_another_timezone_is_consistent():
    """**时区写法不同但同一时刻视作一致**（最容易误杀的一条）。

    草稿写 ``2026-09-02T02:00:00Z``，证据行是 ``2026-09-02T10:00:00+08:00``
    —— 同一时刻。判它不一致等于逼模型一律写院内本地时间，而院内自建
    端点的模型有时更习惯输出 UTC。
    """
    bundle = _channel_only_bundle()
    draft = _draft(
        explanation="该笔 UTC 时刻为 2026-09-02T02:00:00Z，HIS 侧无对应记录。", bundle=bundle
    )
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]
    assert result.checked["time"] >= 1


def test_a_different_moment_is_a_time_mismatch():
    bundle = _channel_only_bundle()
    draft = _draft(explanation="该笔交易时间为 2026-09-02 22:00:00，HIS 侧无记录。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert not result.consistent
    assert MismatchKind.TIME in result.mismatch_kinds


def test_bare_clock_time_is_compared_against_the_cited_rows():
    bundle = _channel_only_bundle()
    same = verify_draft(_draft(explanation="交易时间 10:00，HIS 无记录。", bundle=bundle), bundle)
    assert same.consistent, [m.to_dict() for m in same.mismatches]
    other = verify_draft(_draft(explanation="交易时间 22:00，HIS 无记录。", bundle=bundle), bundle)
    assert not other.consistent
    assert MismatchKind.TIME in other.mismatch_kinds


def test_date_only_claim_is_folded_into_the_hospital_timezone():
    """纯日期先折到院内时区再取日历日：10:00+08:00 的那笔，日期是 09-02。"""
    bundle = _channel_only_bundle()
    ok = verify_draft(_draft(explanation="该笔落在 2026-09-02，HIS 无记录。", bundle=bundle), bundle)
    assert ok.consistent, [m.to_dict() for m in ok.mismatches]
    bad = verify_draft(_draft(explanation="该笔落在 2026-09-05，HIS 无记录。", bundle=bundle), bundle)
    assert not bad.consistent
    assert MismatchKind.TIME in bad.mismatch_kinds


def test_period_code_not_on_the_cited_rows_is_a_mismatch():
    bundle = _channel_only_bundle()
    draft = _draft(explanation="2026-W30 那一期也有一笔同号记录，HIS 侧仍无。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert not result.consistent
    assert MismatchKind.TIME in result.mismatch_kinds


def test_cross_period_boundary_date_is_legitimate():
    """跨账期解释里出现窗口边界是合法的（那正是判据本身）。"""
    bundle = _channel_only_bundle()
    window = period_window(PERIOD, TZ)
    edge = window.end.strftime("%Y-%m-%d")
    draft = _draft(
        explanation=f"该笔收费时间在账期边界 {edge} 之后，落在下一期。", bundle=bundle
    )
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]


def test_the_window_is_not_a_pass_for_any_moment_inside_it():
    """**窗口是「跨期判据」，不是「时间写在窗口里就算了」的通行证**。

    边界那一刻（周一零点前后一分钟）放行 —— 那是半开区间 ``[start, end)``
    的接缝，模型写成 ``23:59:59`` 说的是同一件事。但窗口**内**的任意
    时刻仍要与所引行对上：证据行是 10:00，草稿说 22:00，两者差 12 小时，
    窗口救不了它。
    """
    bundle = _channel_only_bundle()
    window = period_window(PERIOD, TZ)
    edge = window.end.strftime("%Y-%m-%d %H:%M:%S")
    ok = verify_draft(
        _draft(explanation=f"该笔收费时间 {edge}，落在下一期。", bundle=bundle), bundle
    )
    assert ok.consistent, [m.to_dict() for m in ok.mismatches]
    inside = verify_draft(
        _draft(explanation="该笔交易时间 2026-09-03 22:00:00，HIS 侧无记录。", bundle=bundle), bundle
    )
    assert not inside.consistent
    assert MismatchKind.TIME in inside.mismatch_kinds


# —— 3. 单号核对 ————————————————————————————————————————————————


def test_order_no_inside_the_bundle_is_consistent():
    bundle = _split_bundle()
    diff_ref = str(bundle.payload["diff_row"]["ref"])
    draft = _draft(explanation=f"单号 EX-1 在 {diff_ref} 有渠道记录，HIS 侧拆成两笔。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]
    assert result.checked["order_no"] == 1


def test_order_no_outside_the_bundle_is_rejected():
    bundle = _channel_only_bundle()
    draft = _draft(explanation="渠道 EX-1 与 HIS EX-9 金额相同，疑为同一笔。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert not result.consistent
    assert MismatchKind.ORDER_NO in result.mismatch_kinds
    mismatch = next(m for m in result.mismatches if m.kind == MismatchKind.ORDER_NO)
    assert mismatch.claimed == "EX-9"


def test_source_file_name_is_not_mistaken_for_an_order_no():
    """``wechat_bill_2026-W36.csv`` 含数字且长得像单号，不先抽掉会被判越界。"""
    bundle = _channel_only_bundle()
    draft = _draft(
        explanation="来源 wechat_bill_2026-W36.csv 第 17 行，HIS 侧无对应记录。", bundle=bundle
    )
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]


def test_long_digit_order_numbers_are_checked_too():
    """微信 / 支付宝交易号是 28 位纯数字，一样要核。"""
    bundle = _channel_only_bundle()
    fake = "4200001234202609011234567890"
    draft = _draft(explanation=f"该渠道交易号 {fake} 在 HIS 侧无对应记录。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert not result.consistent
    assert MismatchKind.ORDER_NO in result.mismatch_kinds


# —— 4. 成因形态交叉核对 ————————————————————————————————————————


@pytest.mark.parametrize(
    "cause,kind",
    [
        ("跨日切分重复计入", "cross_period"),
        ("该笔落在下一账期", "cross_period"),
        ("一码付拆成多笔", "split_payment"),
        ("按费用项目拆分分摊", "split_payment"),
        ("合并支付覆盖多条 HIS 收费", "merged_payment"),
        ("退费红冲未在渠道账单反映", "refund_coexist"),
    ],
)
def test_cause_phrase_maps_to_controlled_kind(cause, kind):
    assert map_cause_kind(cause) == kind


def test_unmapped_cause_skips_the_cross_check_instead_of_guessing():
    """映射不到受控形态 → 整项跳过并留 note，**既不判通过也不判失败**。

    受控成因字典在后续任务落地；本轮猜一个形态去比，猜错就会把一份
    正确的草稿拒掉 —— 那比漏放一条更糟。
    """
    bundle = _channel_only_bundle()
    draft = _draft(cause="导出方漏传", explanation="HIS 侧查无此笔。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert result.consistent
    assert result.cause_kind == ""
    assert result.checked["cause_form"] == 0
    assert any("映射不到受控形态" in note for note in result.notes)


def test_declared_split_payment_without_fanout_is_a_contradiction():
    """声明「一码付拆笔」而证据里对面只有 0 行 → 形态与证据矛盾。"""
    bundle = _channel_only_bundle()
    draft = _draft(cause="一码付拆成多笔", explanation="HIS 侧拆成了几笔。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert not result.consistent
    assert MismatchKind.CAUSE_FORM in result.mismatch_kinds
    mismatch = next(m for m in result.mismatches if m.kind == MismatchKind.CAUSE_FORM)
    assert "不构成一拆多" in mismatch.detail


def test_declared_split_payment_with_real_fanout_is_consistent():
    bundle = _split_bundle()
    diff_ref = str(bundle.payload["diff_row"]["ref"])
    draft = _draft(
        cause="一码付拆成多笔",
        explanation=f"{diff_ref} 渠道侧 128.00 元在 HIS 侧按费用项目拆成了两笔。",
        bundle=bundle,
    )
    result = verify_draft(draft, bundle)
    assert result.consistent, [m.to_dict() for m in result.mismatches]
    assert result.cause_kind == "split_payment"


def test_declared_cross_period_but_everything_inside_window_is_a_contradiction():
    bundle = _channel_only_bundle()
    draft = _draft(cause="跨日切分重复计入", explanation="该笔落在下一期。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert not result.consistent
    assert MismatchKind.CAUSE_FORM in result.mismatch_kinds


def test_declared_refund_coexist_without_a_refund_row_is_a_contradiction():
    bundle = _channel_only_bundle()
    draft = _draft(cause="退费红冲未在渠道账单反映", explanation="HIS 侧已红冲。", bundle=bundle)
    result = verify_draft(draft, bundle)
    assert not result.consistent
    assert MismatchKind.CAUSE_FORM in result.mismatch_kinds


def test_cause_form_outcome_returns_empty_kind_when_unmapped():
    bundle = _channel_only_bundle()
    kind, _zh, detail = cause_form_outcome(bundle, "导出方漏传")
    assert kind == "" and detail == ""


# —— 5. 处置：拒即拒，不改写、不推进 ————————————————————————————


def test_any_mismatch_rejects_the_whole_draft():
    bundle = _channel_only_bundle()
    draft = _draft(
        cause="一码付拆成多笔",
        explanation="渠道侧 66.00 元，HIS 侧拆成了几笔。",
        bundle=bundle,
    )
    result = verify_draft(draft, bundle)
    assert not result.consistent
    # 金额 + 形态两项同时不过
    assert set(result.mismatch_kinds) == {MismatchKind.AMOUNT, MismatchKind.CAUSE_FORM}
    assert result.to_dict()["error_code"] == REJECT_CODE


def test_rejected_draft_never_enters_the_confirmation_flow():
    """拒因只产出**文字**；不碰台账、不推进任何状态迁移。"""
    bundle = _channel_only_bundle()
    draft = _draft(explanation="渠道侧 66.00 元，HIS 侧无记录。", bundle=bundle)
    result = verify_draft(draft, bundle)
    rejection = draft_rejection(draft, result)
    assert isinstance(rejection, DraftRejection)
    assert rejection.error_code == REJECT_CODE
    assert "待人工判定" in rejection.detail
    assert "不改写模型文本" in rejection.detail
    # 类型上装不下「已确认」—— 与 Task 28 的 CauseDraft 同一纪律
    assert "disposition" not in rejection.to_dict()
    assert "已确认" not in rejection.detail


def test_pending_manual_note_distinguishes_kind_counts():
    bundle = _channel_only_bundle()
    draft = _draft(
        cause="一码付拆成多笔", explanation="渠道侧 66.00 元，金额与拆笔说法都对不上。", bundle=bundle
    )
    note = pending_manual_note(verify_draft(draft, bundle))
    assert note.startswith("待人工判定（归因草稿与证据不一致：")
    assert MISMATCH_ZH[MismatchKind.AMOUNT] in note
    assert MISMATCH_ZH[MismatchKind.CAUSE_FORM] in note


def test_pending_manual_note_is_empty_when_consistent():
    bundle = _channel_only_bundle()
    result = verify_draft(_draft(explanation="渠道侧 86.00 元，HIS 侧无记录。", bundle=bundle), bundle)
    assert pending_manual_note(result) == ""


def test_screen_drafts_keeps_the_good_one_and_rejects_only_the_bad():
    """逐条独立：Top-1 说错话而 Top-2 说得对是常态，整批丢掉等于把
    唯一说对的那条也扔了。"""
    bundle = _split_bundle()
    diff_ref = str(bundle.payload["diff_row"]["ref"])
    good = _draft(
        cause="一码付拆成多笔",
        explanation=f"{diff_ref} 渠道侧 128.00 元在 HIS 侧拆成了两笔。",
        bundle=bundle,
    )
    bad = _draft(
        cause="一码付拆成多笔",
        explanation=f"{diff_ref} 渠道侧 999.00 元在 HIS 侧拆成了两笔。",
        bundle=bundle,
    )
    ledger = VerificationLedger(PERIOD)
    kept, rejected = screen_drafts([bad, good], bundle, ledger)
    assert kept == [good]
    assert len(rejected) == 1 and rejected[0].error_code == REJECT_CODE
    assert ledger.checked == 2 and ledger.consistent == 1 and ledger.rejected == 1


def test_verify_draft_never_raises_on_garbage_input():
    """脏输入返回不一致结果，不抛裸异常冒到面板。"""
    bundle = _channel_only_bundle()
    for draft in (
        {},
        {"cause": "", "evidence_refs": None, "explanation_zh": None},
        {"cause": "x" * 5000, "evidence_refs": ["a:1"], "explanation_zh": "9" * 500},
    ):
        result = verify_draft(draft, bundle)
        assert isinstance(result.consistent, bool)
    # 引用的行不存在时三项逐字核对跳过，并**明说**
    result = verify_draft({"cause": "导出方漏传", "evidence_refs": [], "explanation_zh": "x"}, bundle)
    assert any("没有对应行" in note for note in result.notes)


def test_a_draft_with_no_explanation_is_not_silently_a_pass():
    """**字段名写错不得静默变成「核对通过」**。

    调用方把 ``explanation_zh`` 写成 ``explanation`` 时，text 为空、四项
    核对全部「无可核对内容」，结果是一条 ``consistent=True`` 的假通过 ——
    它比一条被误拒的草稿危险得多，因为面板上它显示为「核对通过」。
    """
    bundle = _channel_only_bundle()
    ref = str(bundle.payload["diff_row"]["ref"])
    typo = {"cause": "一码付拆成多笔", "evidence_refs": [ref], "explanation": "渠道侧 66.00 元"}
    result = verify_draft(typo, bundle)
    # 仍是「一致」（cause 映射得到形态且相容），但**明说没核过任何字面量**
    assert any("explanation_zh" in note for note in result.notes), result.notes
    assert any("这不是「核对通过」" in note for note in result.notes), result.notes
    assert result.checked["amount"] == 0
    # 完全空壳则两个字段都点名
    empty = verify_draft({"cause": "", "evidence_refs": [], "explanation_zh": ""}, bundle)
    assert any("cause、explanation_zh" in note for note in empty.notes)


def test_verify_draft_is_deterministic():
    bundle = _channel_only_bundle()
    draft = _draft(cause="一码付拆成多笔", explanation="渠道侧 66.00 元。", bundle=bundle)
    first = verify_draft(draft, bundle).to_dict()
    second = verify_draft(draft, bundle).to_dict()
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


# —— 6. 统计台账与运行摘要 ————————————————————————————————————————


def test_ledger_reports_rate_and_kind_distribution():
    bundle = _channel_only_bundle()
    ledger = VerificationLedger(PERIOD)
    ledger.record(verify_draft(_draft(explanation="渠道侧 86.00 元，HIS 无记录。", bundle=bundle), bundle))
    ledger.record(verify_draft(_draft(explanation="渠道侧 66.00 元，HIS 无记录。", bundle=bundle), bundle))
    ledger.record(verify_draft(_draft(explanation="单号 EX-9 金额 86.00 元。", bundle=bundle), bundle))
    assert ledger.checked == 3
    assert ledger.consistent == 1
    assert ledger.consistency_rate == "33.33%"
    assert ledger.by_kind() == {MismatchKind.AMOUNT: 1, MismatchKind.ORDER_NO: 1}
    assert "反幻觉核对" in ledger.render_zh()


def test_empty_ledger_reports_blank_rate_not_zero_percent():
    """「一条都没核对」与「核对了、全部不一致」在提示词迭代上是两回事。"""
    ledger = VerificationLedger(PERIOD)
    assert ledger.consistency_rate == ""
    assert ledger.to_dict()["consistency_rate"] == ""
    assert "未核对任何草稿" in ledger.render_zh()


def test_ledger_feeds_run_summary_fields():
    bundle = _channel_only_bundle()
    ledger = VerificationLedger(PERIOD)
    ledger.record(verify_draft(_draft(explanation="渠道侧 86.00 元，HIS 无记录。", bundle=bundle), bundle))
    ledger.record(verify_draft(_draft(explanation="渠道侧 66.00 元，HIS 无记录。", bundle=bundle), bundle))
    summary = build_run_summary(
        period=PERIOD, command="scan", channels=[], verification=ledger
    )
    payload = summary.to_dict()
    assert payload["verification_checked"] == 2
    assert payload["verification_consistent"] == 1
    assert payload["verification_rejected"] == 1
    assert payload["verification_consistency_rate"] == "50.00%"
    assert payload["verification_mismatch_by_kind"] == {MismatchKind.AMOUNT: 1}
    assert "反幻觉核对" in summary.render_zh()


def test_run_summary_without_verification_has_no_rate_line():
    """没核对过的账期不写「一致率 0%」—— 那看着像「模型说的全不对」。"""
    summary = build_run_summary(period=PERIOD, command="scan", channels=[])
    assert summary.verification_checked == 0
    assert summary.verification_consistency_rate == ""
    assert "反幻觉核对" not in summary.render_zh()


def test_run_summary_rejects_a_bad_verification_object():
    from reconq.errors import ReconqError

    with pytest.raises(ReconqError) as exc:
        build_run_summary(period=PERIOD, command="scan", channels=[], verification=object())
    assert exc.value.code == "RUN_SUMMARY_INVALID"


# —— 7. 错误码与事件登记 ————————————————————————————————————————


def test_error_code_is_registered_with_zh_and_suggestion():
    meta = error_meta(REJECT_CODE)
    assert REJECT_CODE in ERROR_CODES
    assert meta.zh.strip() and meta.suggestion.strip()
    assert meta.zh != REJECT_CODE
    # 与 Task 28 的越界码分开：那一码是「行根本不存在」，这一码是
    # 「行存在但说错了话」，排障动作完全不同
    assert REJECT_CODE != "ATTRIBUTION_EVIDENCE_REF_INVALID"
    assert not meta.channel_level, "草稿被拒意味着归因走不通，剔除渠道会让差异没人解释"
    assert not meta.retryable


def test_verify_event_is_registered_in_the_event_whitelist():
    assert "attribution.verify" in LOG_EVENTS
    assert "attribution.verify" != "attribution.done"


def test_mismatch_kinds_all_have_chinese_labels():
    for kind in (
        MismatchKind.AMOUNT,
        MismatchKind.TIME,
        MismatchKind.ORDER_NO,
        MismatchKind.CAUSE_FORM,
    ):
        assert MISMATCH_ZH[kind].strip()
        assert MISMATCH_ZH[kind] != kind


# —— 8. 文本抽取层的边界 ————————————————————————————————————————


def test_file_names_and_dates_do_not_leak_into_order_tokens():
    """抽单号前必须先抽掉文件名 / 日期 / 时刻（顺序反了会误伤）。"""
    from reconq.align.verify_draft import _extract_order_tokens, _strip_patterns

    text = "来源 wechat_bill_2026-W36.csv 第 17 行，2026-09-02 10:00:00，金额 86.00"
    stripped = _strip_patterns(
        text,
        (
            __import__("re").compile(r"\d+(?:\.\d+)?\s*[%％]"),
            __import__("re").compile(r"[A-Za-z0-9_\-]+\.(?:csv|txt|tsv|json|xlsx|zip|gz|xml)"),
        ),
    )
    tokens = [t for t, _s, _e in _extract_order_tokens(stripped)]
    # 剩下的 2026-09-02 / 10:00 不在单号候选里（无字母或被后续抽取处理）
    assert "wechat_bill_2026-W36.csv" not in tokens


def test_extract_amount_literals_requires_point_or_currency():
    from reconq.align.verify_draft import _extract_amount_literals

    assert [v for _t, v, _s, _e in _extract_amount_literals("共 2 条候选行")] == []
    assert [v for _t, v, _s, _e in _extract_amount_literals("差额 86.00 元")] == [Decimal("86.00")]
    assert [v for _t, v, _s, _e in _extract_amount_literals("¥86")] == [Decimal("86")]
    assert [v for _t, v, _s, _e in _extract_amount_literals("1,020.50元")] == [Decimal("1020.50")]


def test_strip_patterns_keeps_offsets_so_later_matches_do_not_glue():
    """抽掉一段后不把两侧粘起来：「这条 20 行」不能变成「这条 20行」。"""
    from reconq.align.verify_draft import _strip_patterns

    text = "这条 2026-09-02 20 行"
    stripped = _strip_patterns(text, (re.compile(r"\d{4}-\d{2}-\d{2}"),))
    assert len(stripped) == len(text)
    assert "20" in stripped and "2026" not in stripped
