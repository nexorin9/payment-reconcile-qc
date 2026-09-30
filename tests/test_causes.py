"""受控成因字典与中文解释模板（Task 33）。

六类规定断言逐条可核：

1. **字典字段齐全** —— 十项必覆盖、每项 ``zh_name`` / ``clues`` /
   ``suggested_action`` / ``owner`` 非空，owner 落在四类责任方内，模板槽位
   可解析、字典内无重复编码；
2. **未知 ``cause_code`` 被拒** —— ``entry_of`` 抛
   ``ATTRIBUTION_CAUSE_UNKNOWN``、``screen_hits`` 整条拒且**不做就近归并**；
3. **模板填充可读** —— 槽位齐全时渲染出通顺中文，缺槽位报
   ``CAUSE_TEMPLATE_SLOT_UNKNOWN`` 而非半句，金额走 Decimal 定点两位，
   ``float`` 传参被拒；
4. **歧义分类映射正确** —— 四形态 + UNKNOWN 单向映射到成因码，且两套分类
   不并存（字典里不另写一份形态清单）；
5. **按成因聚合正确** —— 条数 / 金额 / 排序 / Top-1 口径 / 责任方分工表；
6. **字典版本记录** —— 指纹稳定性、幂等记账、版本不一致可见、历史结论带
   当时版本。

端到端那一节**走主路径**（对齐结论 → 歧义分型 → 形态映射 → 成因命中 →
聚合），不手工构造枚举值：手构会把「模型自由文本 → 受控码」这道最关键的
转换绕过去，而那正是本任务要验的东西。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from reconq.align.causes import (
    CAUSE_DICT_VERSION,
    CAUSE_REJECT_CODE,
    DICT_EVENT,
    OWNER_ZH,
    CauseAggregate,
    CauseHit,
    CauseOwner,
    aggregate_causes,
    cause_from_ambiguity_kind,
    cause_names,
    cause_vocabulary,
    dictionary_digest,
    dictionary_payload,
    dictionary_stamp,
    entry_of,
    explanation_template,
    facts_from_ambiguity_verdict,
    record_dictionary_version,
    render_explanation,
    required_slots,
    screen_hits,
    version_check,
)
from reconq.align.causes import CAUSES as DICT
from reconq.align.taxonomy import (
    AmbiguityKind,
    ClusterRow,
    TaxonomyCase,
    TaxonomyVerdict,
    classify_taxonomy,
)
from reconq.errors import ERROR_CODES, ReconqError
from reconq.runlog import LOG_EVENTS
from reconq.sources.timezone import HOSPITAL_TZ

#: 任务点名的十项成因（中文名 → 必须存在）
REQUIRED_ZH_NAMES = (
    "渠道延迟入账",
    "HIS 未落单",
    "跨日切分重复计入",
    "退费已在 HIS 红冲",
    "账单类型不含退款",
    "一码付拆笔未分摊",
    "合并支付分摊",
    "账期边界落点差",
    "对账口径配置错",
    "无法判定",
)


def _facts(**overrides):
    """一套能填满任一模板的槽位。"""
    base = {
        "count": 3,
        "order_no": "EX-2026-0001",
        "period": "2026-W36",
        "amount": Decimal("1020.00"),
        "diff_amount": Decimal("12.50"),
        "evidence": ["wechat_tradebill_2026-W36.csv:6", "wechat_tradebill_2026-W36.csv:9"],
        "clues": ["候选金额全相同且与对侧不守恒"],
    }
    base.update(overrides)
    return base


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from reconq.engine.resume import init_resume_schema

    init_resume_schema(conn)
    return conn


# —— 1. 字典字段齐全 ————————————————————————————————————


def test_all_required_causes_registered():
    """任务点名的十项成因一项不缺。"""
    zh_names = {entry.zh_name for entry in DICT.values()}
    for name in REQUIRED_ZH_NAMES:
        assert name in zh_names, f"字典缺成因「{name}」"


def test_every_entry_has_all_fields():
    """每项成因的五个字段都非空，且模板能解析出槽位。"""
    for code, entry in DICT.items():
        assert entry.code == code, f"{code} 的 code 字段与字典键不一致"
        assert entry.zh_name.strip(), f"{code} 缺中文名"
        assert entry.clues, f"{code} 缺判别线索"
        assert entry.suggested_action.strip(), f"{code} 缺建议动作"
        assert entry.owner in OWNER_ZH, f"{code} 的责任方 {entry.owner!r} 不在四类内"
        assert entry.template.strip(), f"{code} 缺说明模板"
        # 模板必须可被 str.Formatter 解析（花括号不配对会在渲染时才炸）
        assert required_slots(code), f"{code} 的模板没有任何可填槽位"


def test_owners_cover_all_four_departments():
    """四个责任方都被用到：少一个就意味着某个成因没人认领。"""
    used = {entry.owner for entry in DICT.values()}
    assert used == set(OWNER_ZH), f"责任方覆盖不全：{sorted(used)}"


def test_clues_are_actually_distinguishing():
    """不同成因的判别线索不共用同一句。

    两条成因的线索一字不差时，它们在证据包上无法区分 —— 那等价于把两件事
    合成一件，而合成件的责任方只有一个，现场就会拿着合成件的处置建议去查两
    件事。
    """
    seen: dict[str, str] = {}
    for code, entry in DICT.items():
        for clue in entry.clues:
            assert clue not in seen, (
                f"成因 {code} 与 {seen.get(clue)} 共用同一条判别线索「{clue}」，"
                f"二者在证据包上无法区分"
            )
            seen[clue] = code


def test_no_duplicate_codes_and_stable_order():
    """编码唯一；``cause_vocabulary()`` 顺序稳定（提示词里的建议优先级）。"""
    codes = list(DICT)
    assert len(codes) == len(set(codes))
    assert cause_vocabulary() == tuple(codes)
    assert cause_vocabulary() == cause_vocabulary()
    assert len(cause_names()) == len(DICT)


def test_undetermined_is_registered():
    """「无法判定」必须是一条正式的成因，不是兜底分支。

    字典里没有这一项时，模型会被迫在十条里挑一条最像的 —— 而「我判不出来」
    比「我判错了」贵得多：前者让人去查证，后者让人签字。
    """
    assert "undetermined" in DICT
    assert DICT["undetermined"].zh_name == "无法判定"


# —— 2. 未知 cause_code 被拒 ————————————————————————————


def test_unknown_cause_raises_with_reject_code():
    with pytest.raises(ReconqError) as exc:
        entry_of("渠道晚同步")
    assert exc.value.code == CAUSE_REJECT_CODE
    assert "就近归并" in exc.value.detail, "拒因必须说清系统不做就近归并"


def test_unknown_cause_rejected_by_screen_hits():
    """未登记成因整条拒，且**不落到最接近的那一项**。

    ``cause`` 只认**编码**：中文名（「渠道延迟入账」）不是受控值，模型写中文
    与写一个字典外的自造短语同样是「系统不认识的东西」，一律拒 —— 悄悄把
    中文名映射回编码等于在受控集合外又开了一个口子。
    """
    kept, rejected = screen_hits(
        [
            {"cause": "channel_late_posting", "conclusion_key": "wechat:2026-W36:ex-1"},
            {"cause": "导出方漏传", "conclusion_key": "wechat:2026-W36:ex-2"},
            {"cause": "渠道延迟入账", "conclusion_key": "wechat:2026-W36:ex-3"},
        ]
    )
    assert [k.cause_code for k in kept] == ["channel_late_posting"]
    assert len(rejected) == 2
    assert {r.cause for r in rejected} == {"导出方漏传", "渠道延迟入账"}
    assert all(r.error_code == CAUSE_REJECT_CODE for r in rejected)
    # 关键：拒掉的不能被悄悄并进 channel_late_posting
    assert "导出方漏传" not in {k.cause_code for k in kept}


def test_missing_cause_field_rejected():
    kept, rejected = screen_hits([{"conclusion_key": "wechat:2026-W36:ex-1"}])
    assert kept == []
    assert rejected[0].error_code == CAUSE_REJECT_CODE
    assert "cause" in rejected[0].detail


def test_screen_hits_is_per_item_independent():
    """Top-1 说字典外的成因、Top-2 说得对时，Top-2 仍要收下。"""
    kept, rejected = screen_hits(
        [
            {
                "cause": "不明成因",
                "conclusion_key": "k1",
                "rank": 1,
            },
            {
                "cause": "his_missing_order",
                "conclusion_key": "k1",
                "rank": 2,
            },
        ]
    )
    assert [k.cause_code for k in kept] == ["his_missing_order"]
    assert len(rejected) == 1


def test_reject_code_registered_in_error_table():
    assert CAUSE_REJECT_CODE in ERROR_CODES
    assert ERROR_CODES[CAUSE_REJECT_CODE].zh
    assert ERROR_CODES[CAUSE_REJECT_CODE].suggestion


# —— 3. 模板填充可读 ————————————————————————————————————


@pytest.mark.parametrize("code", list(DICT))
def test_every_template_renders_readable_chinese(code):
    """每条成因都能渲染出一句带证据行与责任方的中文。"""
    text = render_explanation(code, _facts())
    assert text.strip()
    assert "责任方：" in text
    assert "建议：" in text
    assert DICT[code].suggested_action in text
    assert DICT[code].zh_name in text or DICT[code].zh_name == "无法判定"


def test_template_contains_evidence_refs():
    """证据行必须出现在说明里 —— 说明不带行号，经办人就回查不了。"""
    text = render_explanation("his_missing_order", _facts())
    assert "wechat_tradebill_2026-W36.csv:6" in text
    assert "wechat_tradebill_2026-W36.csv:9" in text


def test_missing_slot_raises_instead_of_rendering_half():
    """缺槽位即报错，不渲染半句。

    渲染成「该笔金额 元」会被经办人读成「系统没查到金额」，而真实原因是
    调用方没传 —— 两者会引出完全不同的排查方向。
    """
    facts = _facts()
    del facts["evidence"]
    with pytest.raises(ReconqError) as exc:
        render_explanation("channel_late_posting", facts)
    assert exc.value.code == "CAUSE_TEMPLATE_SLOT_UNKNOWN"
    assert "证据行" in exc.value.detail


def test_empty_slot_value_treated_as_missing():
    facts = _facts(order_no="")
    with pytest.raises(ReconqError) as exc:
        render_explanation("his_missing_order", facts)
    assert exc.value.code == "CAUSE_TEMPLATE_SLOT_UNKNOWN"
    assert "单号" in exc.value.detail


def test_amount_rendered_as_fixed_point_two_digits():
    """金额一律 Decimal 定点两位：1020.00 不得渲染成 1020.0。"""
    text = render_explanation("channel_late_posting", _facts(amount=Decimal("1020")))
    assert "1020.00" in text
    assert "1020.0 元" not in text


def test_float_amount_is_rejected_not_silently_formatted():
    """float 入参必须报错。

    悄悄 float() 一下会让 0.1+0.2 变成 0.30000000000000004 出现在结论单上 ——
    财务科看到这一行会以为账单有问题。
    """
    with pytest.raises(ReconqError) as exc:
        render_explanation("channel_late_posting", _facts(amount=0.1 + 0.2))
    assert exc.value.code == "CAUSE_TEMPLATE_SLOT_UNKNOWN"
    assert "Decimal" in exc.value.detail


def test_empty_diff_amount_renders_as_no_diff_not_zero():
    """无差额时渲染成「（无差额）」而不是 0.00 元。

    0.00 会被读成「两侧恰好相等」，而这一栏在多数成因上根本没有这个量。
    断言只查「差额」后面那一段：满屏搜 ``0.00`` 会被 ``1020.00 元`` 里的
    子串命中，那是这条断言自己写错了，不是渲染错了。
    """
    facts = _facts(amount=Decimal("1234.56"), diff_amount="")
    text = render_explanation("merged_payment_allocation", facts)
    assert "差额 （无差额）" in text
    assert "0.00 元" not in text
    # 有差额时正常渲染，且带单位
    with_diff = render_explanation(
        "merged_payment_allocation", _facts(amount=Decimal("1234.56"))
    )
    assert "差额 12.50 元" in with_diff


def test_unknown_slot_rejected_in_strict_mode():
    with pytest.raises(ReconqError) as exc:
        render_explanation("channel_late_posting", _facts(typo_slot="x"))
    assert exc.value.code == "CAUSE_TEMPLATE_SLOT_UNKNOWN"
    assert "typo_slot" in exc.value.detail


def test_template_is_readable_via_template_accessor():
    assert explanation_template("undetermined") == DICT["undetermined"].template
    with pytest.raises(ReconqError):
        explanation_template("nope")


def test_facts_from_ambiguity_verdict_supplies_slots():
    """从歧义分型结论取槽位，缺项不编占位值。"""
    verdict = TaxonomyVerdict(
        kind=AmbiguityKind.SPLIT_PAYMENT.value,
        period="2026-W36",
        channel="wechat",
        order_no="EX-1",
        evidence=("wechat.csv:6", "wechat.csv:7", "wechat.csv:8"),
        diff_amount="0",
    )
    facts = facts_from_ambiguity_verdict(verdict, amount=Decimal("1020.00"))
    assert facts["count"] == 3
    assert facts["amount"] == Decimal("1020.00")
    assert facts["evidence"] == ["wechat.csv:6", "wechat.csv:7", "wechat.csv:8"]
    # 守恒为 0 时不填 0.00
    assert facts["diff_amount"] == ""
    text = render_explanation("split_payment_unallocated", facts)
    assert "wechat.csv:6" in text


# —— 4. 歧义分类映射 ————————————————————————————————————


@pytest.mark.parametrize(
    "kind,expected",
    [
        (AmbiguityKind.SPLIT_PAYMENT.value, "split_payment_unallocated"),
        (AmbiguityKind.MERGED_PAYMENT.value, "merged_payment_allocation"),
        (AmbiguityKind.CROSS_PERIOD.value, "period_boundary_landing"),
        (AmbiguityKind.REFUND_COEXIST.value, "refund_offset_in_his"),
        (AmbiguityKind.UNKNOWN.value, "undetermined"),
    ],
)
def test_ambiguity_kind_maps_to_cause(kind, expected):
    assert cause_from_ambiguity_kind(kind) == expected


def test_unregistered_ambiguity_kind_falls_to_undetermined():
    """未登记形态不 KeyError —— 落 undetermined 而不是抛异常。

    形态是内部枚举、成因码是要落台账的对外字段，中间不该有一个会抛异常的
    接缝：抛了就是「升级 align 模块时台账写不进去」。
    """
    assert cause_from_ambiguity_kind("some_future_kind") == "undetermined"
    assert cause_from_ambiguity_kind("") == "undetermined"


def test_single_mapping_source_no_duplicate_taxonomy():
    """字典里每个歧义形态只登记一次，且不另写一份形态清单。

    两套分类各自演进时，台账上会出现「同一笔差异既是『一码付拆笔』又是
    『分摊未做』」两行，经办人判两遍。
    """
    kinds = [e.ambiguity_kind for e in DICT.values() if e.ambiguity_kind]
    assert len(kinds) == len(set(kinds))
    assert set(kinds) == {
        AmbiguityKind.SPLIT_PAYMENT.value,
        AmbiguityKind.MERGED_PAYMENT.value,
        AmbiguityKind.CROSS_PERIOD.value,
        AmbiguityKind.REFUND_COEXIST.value,
    }
    # 非歧义来源的成因（单边账 / 取数 / 口径）不挂形态
    standalone = [c for c, e in DICT.items() if not e.ambiguity_kind]
    assert set(standalone) == {
        "channel_late_posting",
        "his_missing_order",
        "cross_period_duplicate",
        "bill_type_excludes_refund",
        "contract_config_error",
        "undetermined",
    }


# —— 5. 按成因聚合 ————————————————————————————————————


def _hits():
    return [
        CauseHit(
            cause_code="channel_late_posting",
            period="2026-W36",
            conclusion_key="wechat:2026-W36:ex-1",
            amount="1020.00",
        ),
        CauseHit(
            cause_code="channel_late_posting",
            period="2026-W36",
            conclusion_key="wechat:2026-W36:ex-2",
            amount="3.00",
        ),
        CauseHit(
            cause_code="channel_late_posting",
            period="2026-W36",
            conclusion_key="wechat:2026-W36:ex-3",
            amount="7.00",
        ),
        CauseHit(
            cause_code="his_missing_order",
            period="2026-W36",
            conclusion_key="alipay:2026-W36:ex-4",
            amount="56.50",
        ),
        CauseHit(
            cause_code="his_missing_order",
            period="2026-W36",
            conclusion_key="alipay:2026-W36:ex-5",
            amount="8.50",
        ),
    ]


def test_aggregate_groups_by_cause_with_count_and_amount():
    agg = aggregate_causes(_hits())
    assert isinstance(agg, CauseAggregate)
    assert agg.total_count == 5
    assert agg.total_amount == "1095.00"
    first = agg.buckets[0]
    assert first.cause_code == "channel_late_posting"
    assert first.count == 3
    assert first.amount == "1030.00"
    assert len(first.keys) == 3


def test_aggregate_sorted_by_count_then_amount_then_code():
    """条数降序 → 金额降序 → 编码升序。

    第三级不是装饰：条数与金额都相同时顺序必须由字典自身决定，否则同一份
    数据两次聚合的面板排序会不同，截图对不上。
    """
    same = [
        CauseHit(cause_code="undetermined", conclusion_key="a", amount="10.00"),
        CauseHit(cause_code="contract_config_error", conclusion_key="b", amount="10.00"),
    ]
    agg = aggregate_causes(same)
    assert [b.cause_code for b in agg.buckets] == [
        "contract_config_error",
        "undetermined",
    ]


def test_aggregate_takes_top1_only_by_default():
    """默认只统计 Top-1：三条候选全计会让「20 条单边账」变成 60 条。"""
    hits = [
        CauseHit(cause_code="channel_late_posting", conclusion_key="k1", amount="10.00", rank=1),
        CauseHit(cause_code="his_missing_order", conclusion_key="k1", amount="10.00", rank=2),
        CauseHit(cause_code="contract_config_error", conclusion_key="k1", amount="10.00", rank=3),
    ]
    agg = aggregate_causes(hits)
    assert agg.total_count == 1
    assert agg.buckets[0].cause_code == "channel_late_posting"
    assert agg.total_amount == "10.00"


def test_aggregate_all_ranks_when_asked():
    hits = [
        CauseHit(cause_code="channel_late_posting", conclusion_key="k1", amount="10.00", rank=1),
        CauseHit(cause_code="his_missing_order", conclusion_key="k1", amount="10.00", rank=2),
    ]
    agg = aggregate_causes(hits, top_rank_only=False)
    assert agg.total_count == 2


def test_aggregate_still_keeps_hits_whose_top1_absent():
    """只给了候选 2、3 的切片时，Top-1 缺席不该让整条丢失。"""
    hits = [CauseHit(cause_code="his_missing_order", conclusion_key="k1", rank=2)]
    agg = aggregate_causes(hits)
    assert agg.total_count == 1
    assert agg.buckets[0].cause_code == "his_missing_order"


def test_aggregate_by_owner_is_the_work_split_table():
    """``by_owner()`` 就是分工表：面板成因分组可直接显示给科室交办。"""
    agg = aggregate_causes(_hits())
    assert agg.by_owner() == {CauseOwner.CHANNEL.value: 3, CauseOwner.CASHIER.value: 2}
    assert agg.top_cause.cause_code == "channel_late_posting"


def test_aggregate_ignores_unregistered_codes():
    """绕过字典落库的条目**不并入任一桶**，而是记进 unregistered。

    静默并进「无法判定」会让「有没有人绕过受控字典」这个问题在面板上永远
    答不出来 —— 那正是要扩写字典的信号。
    """
    hits = _hits() + [CauseHit(cause_code="野成因", conclusion_key="x", amount="1.00")]
    agg = aggregate_causes(hits)
    assert agg.unregistered == 1
    assert agg.total_count == 5
    assert "野成因" not in {b.cause_code for b in agg.buckets}


def test_aggregate_empty_input_renders_readable():
    agg = aggregate_causes([], period="2026-W36")
    assert agg.total_count == 0
    assert "本期无按成因归好的差异" in agg.render_zh()


def test_aggregate_render_zh_lists_causes_and_work_split():
    text = aggregate_causes(_hits(), period="2026-W36").render_zh()
    assert "渠道延迟入账 3 条 / 1030.00 元 → 渠道方" in text
    assert "分工：渠道方 3 条、病案收费处 2 条" in text
    assert "2026-W36" in text


def test_aggregate_tolerates_missing_amount():
    """没有金额量的成因（渠道延迟入账的跨期重复）不参与金额合计。"""
    hits = [CauseHit(cause_code="cross_period_duplicate", conclusion_key="k1", amount="")]
    agg = aggregate_causes(hits)
    assert agg.total_count == 1
    assert agg.total_amount == "0.00"
    assert agg.buckets[0].amount == ""


# —— 6. 字典版本 ————————————————————————————————————————


def test_digest_is_stable_and_content_sensitive():
    """指纹稳定，且内容一变就变。"""
    first = dictionary_digest()
    assert first == dictionary_digest()
    assert len(first) == 16
    original = DICT["channel_late_posting"]
    DICT["channel_late_posting"] = type(original)(
        code=original.code,
        zh_name=original.zh_name,
        owner=original.owner,
        clues=original.clues,
        suggested_action=original.suggested_action + "（改）",
        template=original.template,
    )
    try:
        assert dictionary_digest() != first
    finally:
        DICT["channel_late_posting"] = original
    assert dictionary_digest() == first


def test_payload_has_no_timestamp():
    """指纹载荷不含构造时刻，否则每次调用都换一个哈希，历史版本比对全废。"""
    payload = dictionary_payload()
    # 逐键查而不是拿整段文本搜子串 —— ``template`` 里就有 ``at``，
    # 按子串查会一直失败，把一条有意义的断言变成永远红的噪声。
    def keys_of(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield str(key)
                yield from keys_of(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys_of(item)

    all_keys = set(keys_of(payload))
    assert not (all_keys & {"at", "ts", "created", "timestamp", "recorded_at"})
    assert set(payload) == set(DICT)


def test_stamp_combines_version_and_digest():
    stamp = dictionary_stamp()
    assert stamp.startswith(CAUSE_DICT_VERSION + "+")
    assert dictionary_digest() in stamp


def test_record_dictionary_version_appends_once():
    conn = _conn()
    first = record_dictionary_version(conn, "2026-W36", run_id="r1")
    assert first["recorded"] is True
    second = record_dictionary_version(conn, "2026-W36", run_id="r2")
    assert second["recorded"] is False
    assert "未重复追加" in second["reason"]
    from reconq.engine.resume import list_scan_events

    events = [e for e in list_scan_events(conn, "2026-W36") if e.event == DICT_EVENT]
    assert len(events) == 1
    assert events[0].scope_data["cause_count"] == len(DICT)
    assert events[0].scope_data["stamp"] == dictionary_stamp()


def test_version_check_ok_after_recording():
    conn = _conn()
    record_dictionary_version(conn, "2026-W36")
    verdict = version_check(conn, "2026-W36")
    assert verdict.ok
    assert verdict.current == dictionary_stamp()
    assert "版本一致" in verdict.render_zh()


def test_version_check_flags_missing_record():
    """链上没有字典版本记录 = 复算时答不出当时用的哪一版。"""
    conn = _conn()
    verdict = version_check(conn, "2026-W37")
    assert not verdict.ok
    assert "没有字典版本记录" in verdict.reason
    assert verdict.recorded == ""


def test_version_check_flags_changed_dictionary():
    """字典内容变更后，历史账期的不一致必须**可见**而不是静默。

    不一致不抛异常：它是「本期有人扩了字典」这件事，该被记录不该让运行失败。
    """
    conn = _conn()
    record_dictionary_version(conn, "2026-W36")
    original = DICT["bill_type_excludes_refund"]
    DICT["bill_type_excludes_refund"] = type(original)(
        code=original.code,
        zh_name=original.zh_name,
        owner=original.owner,
        clues=original.clues,
        suggested_action=original.suggested_action + "（措辞调整）",
        template=original.template,
    )
    try:
        verdict = version_check(conn, "2026-W36")
    finally:
        DICT["bill_type_excludes_refund"] = original
    assert not verdict.ok
    assert verdict.recorded != verdict.current
    assert "字典内容" in verdict.reason
    assert "不要拿当前措辞去覆盖历史结论" in verdict.reason


def test_record_requires_period():
    conn = _conn()
    with pytest.raises(ReconqError) as exc:
        record_dictionary_version(conn, "")
    assert exc.value.code == "CAUSE_DICT_VERSION_INVALID"
    assert "记到虚空" in exc.value.detail


def test_dict_event_registered_in_log_events():
    assert DICT_EVENT in LOG_EVENTS


def test_historical_hit_carries_its_own_dict_version():
    """每条成因结论自带当时的字典版本，历史结论不被后来的字典改写。"""
    original_stamp = dictionary_stamp()
    hit = CauseHit(
        cause_code="his_missing_order",
        conclusion_key="k1",
        dict_version=original_stamp,
    )
    assert hit.to_dict()["dict_version"] == original_stamp
    # 改字典之后，旧的命中记录上的版本**不变**
    entry = DICT["contract_config_error"]
    DICT["contract_config_error"] = type(entry)(
        code=entry.code,
        zh_name=entry.zh_name,
        owner=entry.owner,
        clues=entry.clues,
        suggested_action=entry.suggested_action + "（改）",
        template=entry.template,
    )
    try:
        assert dictionary_stamp() != original_stamp
        assert hit.to_dict()["dict_version"] == original_stamp
    finally:
        DICT["contract_config_error"] = entry


def test_aggregate_stamps_current_dictionary_version():
    agg = aggregate_causes(_hits())
    assert agg.dict_version == dictionary_stamp()
    assert agg.dict_digest == dictionary_digest()
    assert agg.to_dict()["dict_version"] == dictionary_stamp()


# —— 端到端：对齐结论 → 分型 → 成因 → 聚合 ————————————————


def test_end_to_end_ambiguity_to_cause_aggregation():
    """走主路径：歧义分型 → 成因映射 → 受控筛查 → 聚合。

    不手工构造枚举值：手构会把「形态 → 成因码」这道转换绕过去，而那正是
    本任务要验的东西。
    """
    # 一码付拆笔：金额守恒的三条 HIS 明细对一笔渠道款
    case = TaxonomyCase(
        period="2026-W36",
        channel="wechat",
        order_no="EX-2026-0001",
        channel_rows=(
            ClusterRow(ref="wechat.csv:6", amount=Decimal("1020.00"), role="channel", period="2026-W36"),
        ),
        his_rows=(
            ClusterRow(ref="his:charge:11", amount=Decimal("300.00"), period="2026-W36"),
            ClusterRow(ref="his:charge:12", amount=Decimal("500.00"), period="2026-W36"),
            ClusterRow(ref="his:charge:13", amount=Decimal("220.00"), period="2026-W36"),
        ),
    )
    verdict = classify_taxonomy(case)
    assert verdict.kind == AmbiguityKind.SPLIT_PAYMENT.value

    code = cause_from_ambiguity_kind(verdict.kind)
    assert code == "split_payment_unallocated"

    text = render_explanation(
        code, facts_from_ambiguity_verdict(verdict, amount=Decimal("1020.00"))
    )
    assert "his:charge:11" in text
    assert DICT[code].owner in text

    kept, rejected = screen_hits(
        [{"cause": code, "conclusion_key": "wechat:2026-W36:ex-2026-0001", "amount": "0.00"}]
    )
    assert rejected == []
    agg = aggregate_causes(kept, period="2026-W36")
    assert agg.total_count == 1
    assert agg.buckets[0].cause_code == "split_payment_unallocated"
    assert agg.by_owner() == {CauseOwner.CASHIER.value: 1}


def test_end_to_end_cross_period_maps_to_finance_owner():
    """跨账期落点 → 账期边界落点差 → 责任方财务科。"""
    case = TaxonomyCase(
        period="2026-W36",
        channel="alipay",
        order_no="EX-2026-0002",
        channel_rows=(
            ClusterRow(ref="alipay.csv:9", amount=Decimal("66.00"), role="channel", period="2026-W36"),
        ),
        his_rows=(
            ClusterRow(
                ref="his:charge:21",
                amount=Decimal("66.00"),
                period="2026-W37",
                cross_period=True,
            ),
        ),
    )
    verdict = classify_taxonomy(case)
    assert verdict.kind == AmbiguityKind.CROSS_PERIOD.value
    code = cause_from_ambiguity_kind(verdict.kind)
    assert code == "period_boundary_landing"
    assert DICT[code].owner == CauseOwner.FINANCE.value
    facts = facts_from_ambiguity_verdict(verdict, amount=Decimal("66.00"))
    assert "2026-W36" in render_explanation(code, facts)


def test_end_to_end_unknown_ambiguity_maps_to_undetermined():
    """信息耗尽的候选集 → 无法判定，责任方回财务科由经办人判。"""
    case = TaxonomyCase(
        period="2026-W36",
        channel="unionpay",
        order_no="EX-2026-0003",
        channel_rows=(
            ClusterRow(ref="unionpay.txt:3", amount=Decimal("66.00"), role="channel", period="2026-W36"),
        ),
        his_rows=(
            ClusterRow(ref="his:charge:31", amount=Decimal("66.00"), period="2026-W36"),
            ClusterRow(ref="his:charge:32", amount=Decimal("66.00"), period="2026-W36"),
        ),
    )
    verdict = classify_taxonomy(case)
    assert verdict.kind == AmbiguityKind.UNKNOWN.value
    code = cause_from_ambiguity_kind(verdict.kind)
    assert code == "undetermined"
    text = render_explanation(
        code, facts_from_ambiguity_verdict(verdict, amount=Decimal("66.00"))
    )
    assert "系统不猜" in text


def test_timezone_reference_unused_but_consistent():
    """账期槽位与院内时区一致（防止后续有人把模板改成 UTC 口径）。"""
    from reconq.sources.timezone import period_window

    window = period_window("2026-W36")
    assert window.start.tzinfo == HOSPITAL_TZ
    assert window.start.isoformat().startswith("2026-08-31")
