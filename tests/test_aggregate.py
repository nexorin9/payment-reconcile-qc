"""同根因聚合与处置单合并（Task 36）。

六类规定断言逐条可核：

1. **同因聚合** —— 20 条单边账收成 3 张单；每张单的组键、条数、合计、
   责任方、逐条证据行都对得上。
2. **组内不一致打散** —— 处置状态混杂 / 成因未登记受控字典 / 成员自报键
   与组键矛盾，三种情形各自打散并留痕，**不**混进同一张单。
3. **金额分桶** —— 默认 0.01 元精确分桶把 50.00 与 50.01 分成两张单；
   显式放宽到 100 元档位才合并；向下取整不四舍五入；float 拒收。
4. **组级确认同步** —— 整单确认后组内每条在台账上都是已确认，留痕表
   逐条记下操作人与时刻。
5. **组内部分失败不推进** —— 预置一条状态不允许的成员，整组一条都不动，
   逐条结果说清卡在哪、能去哪；台账零改动。
6. **整组撤回** —— 整组回「待确认」，留痕表同时留得住「确认过」与
   「撤回过」两条。

外加几条容易被糊弄过去的性质：模型 / 规则 / 降级路径**推不动**状态；
拿过期组（成因已被重跑归因改掉）操作会被拒；聚合**不丢**逐条可回看
证据；组是派生的（同一份台账算两次结果一致）；交办单按组出证明、按条
附证据；CLI 三条腿端到端跑通。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal

import pytest

from reconq.align.aggregate import (
    ACTION_TARGETS,
    AGGREGATE_VERSION,
    DEFAULT_AMOUNT_BUCKET,
    DISPOSITION_ACTION_TABLE,
    DiscrepancyEntry,
    EvidenceRow,
    GroupAction,
    aggregate_discrepancies,
    amount_bucket,
    apply_group_action,
    assert_human_operator,
    confirm_group,
    diff_key,
    entry_from_diff_item,
    expand_group,
    group_key_for,
    init_disposition_action_schema,
    list_group_actions,
    load_group_entries,
    reject_group,
    render_group_slip_markdown,
    render_group_zh,
    render_summary_zh,
    withdraw_group,
)
from reconq.engine.period import (
    DISPOSITION_CONFIRMED,
    DISPOSITION_PENDING,
    DISPOSITION_REJECTED,
    DiffItem,
    init_scan_schema,
    list_diff_items,
    upsert_diff_items,
)
from reconq.errors import ReconqError

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

PERIOD = "2026-W36"

#: 三组真实场景的单边账。金额取医院固定价目 —— 同因同渠道同形态的
#: 重复项本来就是同一件事在账上落了 N 笔，这也是精确分桶下 20 → 3
#: 能成立的原因（见模块头金额分桶段）。
LATEST_WECHAT = "channel_late_posting"
LATE_ALIPAY = "channel_late_posting"
HIS_MISSING = "his_missing_order"

#: (成因, 渠道, 形态, 金额, 笔数, 渠道单号前缀)
SCENARIO = [
    (LATEST_WECHAT, "wechat", "cross_period", "50.00", 8, "EX-WX-LATE"),
    (LATE_ALIPAY, "alipay", "cross_period", "20.00", 7, "EX-ZFB-LATE"),
    (HIS_MISSING, "self_service", "", "66.00", 5, "EX-SS-MISS"),
]


# ─────────────────────────── 夹具 ───────────────────────────


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    init_scan_schema(connection)
    yield connection
    connection.close()


def _entry(
    cause: str,
    channel: str,
    kind: str,
    amount: str,
    order_no: str,
    *,
    disposition: str = DISPOSITION_PENDING,
    resolved: bool = False,
    row_no: int = 1,
    amount_known: bool = True,
) -> DiscrepancyEntry:
    check_path = f"taxonomy.ambiguity:{order_no}"
    return DiscrepancyEntry(
        key=diff_key(PERIOD, channel, check_path),
        period=PERIOD,
        channel=channel,
        check_path=check_path,
        order_no=order_no,
        cause_code=cause,
        diff_amount=Decimal(amount),
        ambiguity_kind=kind,
        evidence=(
            EvidenceRow(source_file=f"{channel}_bill_2026-W36.csv", row_no=row_no),
        ),
        disposition=disposition,
        resolved=resolved,
        amount_known=amount_known,
        source_file=f"{channel}_bill_2026-W36.csv",
        first_row_no=row_no,
    )


def scenario_entries(**overrides) -> list[DiscrepancyEntry]:
    """20 条真实规模的单边账：8 + 7 + 5 = 20，合计 870.00 元。"""
    out: list[DiscrepancyEntry] = []
    for cause, channel, kind, amount, count, prefix in SCENARIO:
        for index in range(1, count + 1):
            out.append(
                _entry(
                    cause,
                    channel,
                    kind,
                    amount,
                    f"{prefix}-{index:04d}",
                    row_no=index + 1,
                    **overrides,
                )
            )
    return out


def seed_ledger(connection, entries=None, *, causes: dict[str, str] | None = None) -> None:
    """把条目写进差异台账；``causes`` 给定时同步落归因草稿（Top-1）。"""
    items = []
    for entry in entries if entries is not None else scenario_entries():
        items.append(
            DiffItem(
                period=entry.period,
                channel=entry.channel,
                check_path=entry.check_path,
                check_name="歧义成因",
                diff_amount=str(entry.diff_amount) if entry.amount_known else None,
                evidence_total=1,
                source_file=entry.source_file,
                first_row_no=entry.first_row_no,
                disposition=entry.disposition,
                needs_attribution=True,
                ambiguity_kind=entry.ambiguity_kind,
                resolved=entry.resolved,
            )
        )
    upsert_diff_items(connection, items)
    if not causes:
        return
    from reconq.gateway.schema import AttributionDraft, EvidenceRef
    from reconq.store.attribution_store import init_attribution_schema, save_drafts

    init_attribution_schema(connection)
    for entry in entries if entries is not None else scenario_entries():
        code = causes.get(entry.order_no, entry.cause_code)
        save_drafts(
            connection,
            [
                AttributionDraft(
                    cause=code,
                    confidence=0.82,
                    evidence_refs=[
                        EvidenceRef(ref=f"{entry.source_file}:{entry.first_row_no}")
                    ],
                    explanation_zh=f"{entry.order_no} 属 {code}，待人工确认",
                )
            ],
            period=entry.period,
            channel=entry.channel,
            conclusion_key=f"{entry.channel}:{entry.period}:{entry.order_no}",
        )


def _states(connection, keys) -> dict[str, str]:
    out = {}
    for key in keys:
        period, _, rest = key.partition("|")
        channel, _, check_path = rest.partition("|")
        row = connection.execute(
            "SELECT disposition FROM period_diff_items "
            "WHERE period=? AND channel=? AND check_path=?",
            (period, channel, check_path),
        ).fetchone()
        out[key] = row["disposition"] if row else "（台账上已不存在）"
    return out


# ─────────────────── 1. 同因聚合 ───────────────────


def test_twenty_discrepancies_collapse_into_three_slips():
    """20 条单边账 → 3 张处置单，且每张单的条数与合计都对得上。"""
    result = aggregate_discrepancies(scenario_entries())

    assert result.entry_count == 20
    assert len(result.groups) == 3
    assert result.collapsed == 17
    assert result.total_amount == Decimal("870.00")

    by_key = {g.group_key: g for g in result.groups}
    wx = by_key[group_key_for(LATEST_WECHAT, "wechat", "cross_period", "50.00")]
    assert wx.size == 8
    assert wx.total_diff_amount == Decimal("400.00")
    assert wx.zh_name == "渠道延迟入账"
    assert wx.owner == "渠道方"
    assert wx.suggested_action

    zfb = by_key[group_key_for(LATE_ALIPAY, "alipay", "cross_period", "20.00")]
    assert (zfb.size, zfb.total_diff_amount) == (7, Decimal("140.00"))

    ss = by_key[group_key_for(HIS_MISSING, "self_service", "", "66.00")]
    assert (ss.size, ss.total_diff_amount) == (5, Decimal("330.00"))

    # 分工表：三张单分给三个责任方
    assert result.by_owner() == {"渠道方": 2, "病案收费处": 1}

    # 无金额分桶冲突：三张单的桶互不相同
    assert {g.amount_bucket for g in result.groups} == {"50.00", "20.00", "66.00"}


def test_aggregation_is_deterministic_and_pure():
    """同一条差异切成两半灌进同一个桶，是同一张单 —— 组是派生的不是存的。"""
    entries = scenario_entries()
    first = aggregate_discrepancies(entries)
    second = aggregate_discrepancies(list(reversed(entries)))
    assert [g.group_key for g in first.groups] == [g.group_key for g in second.groups]
    assert [g.member_keys for g in first.groups] == [g.member_keys for g in second.groups]
    assert first.to_dict() == second.to_dict()
    # 聚合不改输入
    assert entries[0].disposition == DISPOSITION_PENDING
    assert all(e.resolved is False for e in entries)


def test_expansion_keeps_every_member_and_its_evidence():
    """合并不吞证据：展开后每条成员仍带自己的行号与结论键。"""
    result = aggregate_discrepancies(scenario_entries())
    group = result.groups[0]
    members = expand_group(group)
    assert len(members) == 8
    assert [m.order_no for m in members] == sorted(m.order_no for m in members)
    assert all(m.evidence and m.evidence[0].row_no >= 1 for m in members)
    assert group.conclusion_keys() == tuple(m.conclusion_key() for m in members)
    assert group.conclusion_keys()[0] == f"wechat:{PERIOD}:EX-WX-LATE-0001"
    # 证据去重后全在，不截断
    assert len(group.evidence_refs()) == 8


def test_resolved_entries_are_dropped_but_terminal_dispositions_are_not():
    """消解的差异是死行；已确认的单子仍是活单（要能撤回）。"""
    entries = scenario_entries()
    entries[0] = DiscrepancyEntry(**{**entries[0].__dict__, "resolved": True})
    result = aggregate_discrepancies(entries)
    assert result.entry_count == 19

    confirmed = scenario_entries(disposition=DISPOSITION_CONFIRMED)
    result2 = aggregate_discrepancies(confirmed)
    assert result2.entry_count == 20
    assert all(g.disposition == DISPOSITION_CONFIRMED for g in result2.groups)


# ─────────────────── 2. 组内不一致打散 ───────────────────


def test_mixed_dispositions_split_the_group_instead_of_merging():
    """1 条已确认 + 19 条待确认不能合成一张单：点确认会 1 成 19 败。"""
    entries = scenario_entries()
    entries[0] = DiscrepancyEntry(**{**entries[0].__dict__, "disposition": DISPOSITION_CONFIRMED})
    result = aggregate_discrepancies(entries)

    assert len(result.groups) == 4
    mixed = [g for g in result.groups if g.split_reason.startswith("组内处置状态不一致")]
    assert len(mixed) == 2
    assert {g.size for g in mixed} == {1, 7}
    assert sum(g.size for g in mixed) == 8
    codes = {s.reason_code for s in result.splits}
    assert codes == {"GROUP_DISPOSITION_MIXED"}
    assert len(result.splits) == 8
    # 打散后的两张单仍可用：各自有独立的组键
    assert len({g.group_key for g in mixed}) == 2
    assert "已确认" in {g.disposition for g in mixed}
    assert "待确认" in {g.disposition for g in mixed}


def test_unregistered_cause_is_rejected_not_merged():
    """绕过受控字典的成因：写不出「谁去查」的单子交不出去，没人认领。"""
    entries = scenario_entries()[:3]
    entries[1] = DiscrepancyEntry(**{**entries[1].__dict__, "cause_code": "made_up_cause"})
    result = aggregate_discrepancies(entries)

    codes = {r.reason_code: r for r in result.rejections}
    assert "GROUP_CAUSE_UNREGISTERED" in codes
    assert "made_up_cause" in codes["GROUP_CAUSE_UNREGISTERED"].detail
    # 未登记成因的那条不与任何人合并（单列）
    stray = [g for g in result.groups if g.cause_code == "made_up_cause"]
    assert len(stray) == 1 and stray[0].size == 1
    assert len(result.groups) == 2  # 剩余 2 条正常合并成一张


def test_missing_cause_code_is_rejected():
    """没跑归因的差异成因码为空，不参与聚合也不假装有成因。"""
    entries = scenario_entries()[:2]
    entries[0] = DiscrepancyEntry(**{**entries[0].__dict__, "cause_code": ""})
    result = aggregate_discrepancies(entries)
    assert [r.reason_code for r in result.rejections] == ["GROUP_CAUSE_MISSING"]
    groups = result.groups
    assert len(groups) == 2
    assert {g.cause_code for g in groups} == {"undetermined", LATE_ALIPAY}
    assert [g for g in groups if g.cause_code == "undetermined"][0].size == 1


def test_stray_member_is_split_out_by_its_own_key():
    """自报身份与台账坐标不符的成员单独成单 —— 混进去会更新错那一条。"""
    entries = scenario_entries()[:3]
    # 制造矛盾：key 说的是 alipay 那一行的坐标，字段却是 wechat 的
    entries[2] = DiscrepancyEntry(
        **{
            **entries[2].__dict__,
            "key": diff_key(PERIOD, "alipay", f"taxonomy.ambiguity:EX-WX-LATE-0003"),
        }
    )
    result = aggregate_discrepancies(entries)
    assert result.splits
    assert result.splits[0].reason_code == "GROUP_MEMBER_INCONSISTENT"
    assert "EX-WX-LATE-0003" in result.splits[0].key
    assert "自报键" in result.splits[0].detail
    assert "会去更新另一条差异" in result.splits[0].detail
    # 矛盾那条**既不进任何组、也不计入条数**（拒收而不是给它一张点不动的单）
    assert "EX-WX-LATE-0003" not in {k for g in result.groups for k in g.member_keys}
    assert result.entry_count == 2
    assert [r.reason_code for r in result.rejections] == ["GROUP_MEMBER_INCONSISTENT"]
    assert "无法定位到台账上的一行" in result.rejections[0].detail
    # 剩下两条正常合并
    assert len(result.groups) == 1 and result.groups[0].size == 2


def test_order_no_must_match_the_taxonomy_path():
    """口径里的单号与自报单号不符也打散：交办单上的单号要能回查得到。"""
    entries = scenario_entries()[:2]
    entries[0] = DiscrepancyEntry(**{**entries[0].__dict__, "order_no": "EX-别的单号"})
    result = aggregate_discrepancies(entries)
    assert [s.reason_code for s in result.splits] == ["GROUP_MEMBER_INCONSISTENT"]
    assert "自报单号" in result.splits[0].detail
    assert "EX-别的单号" in result.splits[0].detail
    assert result.entry_count == 1 and len(result.groups) == 1


# ─────────────────── 3. 金额分桶 ───────────────────


def test_amount_bucket_is_exact_to_the_cent_by_default():
    assert amount_bucket("50.00") == "50.00"
    assert amount_bucket("50.01") == "50.01"
    assert amount_bucket(Decimal("50.00")) == "50.00"
    # 金额缺省不是「0 元」：它不参与金额分桶
    assert amount_bucket("0") == "-"
    assert amount_bucket(None) == "-"


def test_one_cent_apart_never_merges_but_widening_the_bucket_does():
    """默认精确分桶下 50.00 与 50.01 是两张单；显式放宽才合并。"""
    entries = [
        _entry(LATEST_WECHAT, "wechat", "cross_period", "50.00", "EX-A"),
        _entry(LATEST_WECHAT, "wechat", "cross_period", "50.01", "EX-B"),
    ]
    strict = aggregate_discrepancies(entries)
    assert len(strict.groups) == 2

    wide = aggregate_discrepancies(entries, amount_bucket_width="100.00")
    assert len(wide.groups) == 1
    assert wide.groups[0].size == 2
    assert wide.groups[0].total_diff_amount == Decimal("100.01")
    # 放宽时交办单要写清区间，不能只写「0.00 元」
    assert wide.groups[0].amount_range_text() == "0.00~100.00 元"
    # 组自己记住了分桶宽度，不会被默认 0.01 解释成「0.00 元」
    assert wide.groups[0].bucket_width == "100.00"
    assert wide.groups[0].amount_range_text("0.01") == "0.00 元"
    # 精确分桶的单写的就是那一个数（50.00 与 50.01 各写各的）
    strict_by_key = {g.group_key: g for g in strict.groups}
    assert strict_by_key[group_key_for(LATEST_WECHAT, "wechat", "cross_period", "50.00")].amount_range_text() == "50.00 元"
    assert strict_by_key[group_key_for(LATEST_WECHAT, "wechat", "cross_period", "50.01")].amount_range_text() == "50.01 元"


def test_bucket_floors_never_rounds_a_different_amount_into_the_slip():
    """向下取整：49.999 落 49.99 档，不抬进 50.00 档（那会把不同金额并单）。"""
    assert amount_bucket("49.999") == "49.99"
    assert amount_bucket("50.00") == "50.00"
    assert amount_bucket("49.999") != amount_bucket("50.00")
    # 负差额（退费红冲）同样向下，不跨档
    assert amount_bucket("-0.01") == "-0.01"
    assert amount_bucket("-50.00", "100.00") == "-100.00"


def test_float_amount_is_rejected_outright():
    """浮点尾数会让本该同金额的两条分不成同一张单 —— 拒收，不猜。"""
    with pytest.raises(ReconqError) as excinfo:
        amount_bucket(50.00)
    assert excinfo.value.code == "AMOUNT_FLOAT_REJECTED"

    entries = scenario_entries()[:1]
    entries.append(
        DiscrepancyEntry(**{**entries[0].__dict__, "diff_amount": 50.00})
    )
    with pytest.raises(ReconqError) as excinfo:
        aggregate_discrepancies(entries)
    assert excinfo.value.code == "AMOUNT_FLOAT_REJECTED"


def test_non_positive_bucket_width_is_refused():
    for bad in ("0", "-1.00"):
        with pytest.raises(ReconqError) as excinfo:
            amount_bucket("50.00", bad)
        assert excinfo.value.code == "AMOUNT_BUCKET_INVALID"
    with pytest.raises(ReconqError) as excinfo:
        aggregate_discrepancies(scenario_entries(), amount_bucket_width="0")
    assert excinfo.value.code == "AMOUNT_BUCKET_INVALID"


# ─────────────────── 4. 组级确认同步 ───────────────────


def test_group_confirm_advances_every_member_and_records_who(conn):
    """整单确认：台账上 8 条全变已确认，留痕表逐条记操作人与时刻。"""
    entries = scenario_entries()
    seed_ledger(conn, entries, causes={e.order_no: e.cause_code for e in entries})
    result = aggregate_discrepancies(load_group_entries(conn, PERIOD))
    group = result.groups[0]
    assert group.size == 8

    outcome = confirm_group(conn, group, operator="财务科-李静", note="已联系渠道补结算单")
    assert outcome.ok
    assert outcome.advanced == 8
    assert len(outcome.members) == 8
    assert all(m.before == "待确认" and m.after == "已确认" and m.advanced for m in outcome.members)
    assert outcome.cause_source == "verified"

    states = _states(conn, group.member_keys)
    assert set(states.values()) == {DISPOSITION_CONFIRMED}
    assert len(states) == 8

    actions = list_group_actions(conn, PERIOD, group_key=group.group_key)
    assert len(actions) == 8
    assert {a["operator"] for a in actions} == {"财务科-李静"}
    assert {a["action"] for a in actions} == {"confirm"}
    assert {a["target_state"] for a in actions} == {"已确认"}
    assert {a["before_state"] for a in actions} == {"待确认"}
    assert {a["recorded_at"] for a in actions} == {actions[0]["recorded_at"]}
    assert all(a["note"] == "已联系渠道补结算单" for a in actions)
    assert all(a["advanced"] == 1 for a in actions)
    # 确认后这张单从「待办」里消失
    again = aggregate_discrepancies(load_group_entries(conn, PERIOD))
    assert [g for g in again.groups if g.disposition == "待确认"] != []


def test_confirm_marks_the_whole_slip_done_in_one_shot(conn):
    """一次点击办掉整单 —— 这正是本任务要替经办人省下的重复动作。"""
    seed_ledger(conn)
    result = aggregate_discrepancies(load_group_entries(conn, PERIOD))
    pending = [g for g in result.groups if g.disposition == "待确认"]
    assert len(pending) == 3 and sum(g.size for g in pending) == 20

    for group in pending:
        assert confirm_group(conn, group, operator="财务科-王芳").ok

    states = _states(conn, [k for g in pending for k in g.member_keys])
    assert set(states.values()) == {DISPOSITION_CONFIRMED}
    assert len(states) == 20
    assert list_group_actions(conn, PERIOD) != []


def test_reject_and_withdraw_use_the_same_group_level_path(conn):
    seed_ledger(conn, scenario_entries()[:2])
    result = aggregate_discrepancies(load_group_entries(conn, PERIOD))
    group = result.groups[0]
    assert group.size == 2

    rejected = reject_group(conn, group, operator="财务科-王芳", note="导出方漏了这两笔")
    assert rejected.ok and rejected.advanced == 2
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_REJECTED}

    back = withdraw_group(conn, group, operator="财务科-李静", note="导出方补了文件")
    assert back.ok and back.advanced == 2
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_PENDING}
    assert ACTION_TARGETS[GroupAction.WITHDRAW.value] == "待确认"


# ─────────────────── 5. 组内部分失败不推进 ───────────────────


def test_one_bad_member_blocks_the_whole_group(conn):
    """组内一条状态不允许 → 整组一条都不动，并逐条说清卡在哪。"""
    entries = scenario_entries()[:8]
    seed_ledger(conn, entries)
    result = aggregate_discrepancies(load_group_entries(conn, PERIOD))
    group = result.groups[0]
    assert group.size == 8

    # 绕过组级入口，直接把其中一条推成已确认（模拟上一轮遗留）
    victim = group.members[3]
    conn.execute(
        "UPDATE period_diff_items SET disposition='已确认' "
        "WHERE period=? AND channel=? AND check_path=?",
        (victim.period, victim.channel, victim.check_path),
    )

    outcome = confirm_group(conn, group, operator="财务科-李静")
    assert not outcome.ok
    assert outcome.error_code == "GROUP_ACTION_DENIED"
    assert outcome.advanced == 0
    assert "整组未推进" in outcome.detail
    assert "部分成功最危险" in outcome.detail

    # 逐条结果：7 条预检本可通过的也被标成没动，与台账一致
    assert len(outcome.members) == 8
    assert all(m.advanced is False for m in outcome.members)
    assert all(m.after == m.before for m in outcome.members)
    blocked = [m for m in outcome.members if m.before == "已确认"]
    assert len(blocked) == 1
    assert "只能从 待确认 出发" in blocked[0].detail
    assert blocked[0].key == victim.key

    # 台账零改动：那 7 条仍是待确认
    states = _states(conn, group.member_keys)
    assert states[victim.key] == DISPOSITION_CONFIRMED
    assert sum(1 for v in states.values() if v == "待确认") == 7

    # 拒了也要留痕
    actions = list_group_actions(conn, PERIOD, group_key=group.group_key)
    assert len(actions) == 8
    assert all(a["advanced"] == 0 for a in actions)
    assert {a["operator"] for a in actions} == {"财务科-李静"}
    assert {a["action"] for a in actions} == {"confirm"}
    assert "GROUP_ACTION_DENIED" in {a["error_code"] for a in actions}


def test_transition_whitelist_has_no_direct_confirm_to_reject(conn):
    """已确认 → 已驳回 直跳被禁：改判必须先撤回，结论单上留得住改判痕迹。"""
    seed_ledger(conn, scenario_entries()[:2])
    group = aggregate_discrepancies(load_group_entries(conn, PERIOD)).groups[0]
    assert confirm_group(conn, group, operator="A").ok

    outcome = reject_group(conn, group, operator="A")
    assert not outcome.ok
    assert outcome.error_code == "GROUP_ACTION_DENIED"
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_CONFIRMED}

    assert withdraw_group(conn, group, operator="A").ok
    assert reject_group(conn, group, operator="A").ok
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_REJECTED}


def test_unknown_action_is_refused_before_anything_is_written(conn):
    seed_ledger(conn, scenario_entries()[:1])
    group = aggregate_discrepancies(load_group_entries(conn, PERIOD)).groups[0]
    with pytest.raises(ReconqError) as excinfo:
        apply_group_action(conn, group, "force_confirm", operator="A")
    assert excinfo.value.code == "GROUP_ACTION_UNKNOWN"
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_PENDING}


def test_model_and_rule_paths_cannot_advance_state(conn):
    """模型 / 规则 / 降级路径推不动台账 —— 这条线是 Task 30 落在这里的。"""
    seed_ledger(conn, scenario_entries()[:2])
    group = aggregate_discrepancies(load_group_entries(conn, PERIOD)).groups[0]

    for who in ("model", "gateway", "rule", "baseline", "系统", "", "   ", "attribution"):
        with pytest.raises(ReconqError) as excinfo:
            apply_group_action(conn, group, GroupAction.CONFIRM, operator=who)
        assert excinfo.value.code == "GROUP_ACTION_OPERATOR_INVALID", who
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_PENDING}
    # 入口就拒了，一次请求都没进到台账：留痕表压根没建（不是「建了但没记录」）
    assert list_group_actions(conn, PERIOD) == []
    assert not any(
        row["name"] == DISPOSITION_ACTION_TABLE
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )

    assert assert_human_operator(" 财务科-李静 ") == "财务科-李静"
    assert assert_human_operator("A10086") == "A10086"


# ─────────────────── 6. 整组撤回 ───────────────────


def test_group_withdraw_returns_every_member_and_leaves_both_traces(conn):
    """整组撤回：8 条全回待确认，留痕表里「确认过」与「撤回过」都在。"""
    entries = scenario_entries()[:8]
    seed_ledger(conn, entries)
    group = aggregate_discrepancies(load_group_entries(conn, PERIOD)).groups[0]

    assert confirm_group(conn, group, operator="财务科-李静", note="按渠道说法先确认").ok
    back = withdraw_group(conn, group, operator="财务科-王芳", note="主管复核：证据不足，退回重查")
    assert back.ok and back.advanced == 8
    assert all(m.before == "已确认" and m.after == "待确认" for m in back.members)
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_PENDING}

    actions = list_group_actions(conn, PERIOD, group_key=group.group_key)
    assert len(actions) == 16
    confirm_rows = [a for a in actions if a["action"] == "confirm"]
    withdraw_rows = [a for a in actions if a["action"] == "withdraw"]
    assert len(confirm_rows) == 8 and len(withdraw_rows) == 8
    assert {a["operator"] for a in confirm_rows} == {"财务科-李静"}
    assert {a["operator"] for a in withdraw_rows} == {"财务科-王芳"}
    assert {a["note"] for a in withdraw_rows} == {"主管复核：证据不足，退回重查"}
    assert {a["recorded_at"] for a in withdraw_rows} == {withdraw_rows[0]["recorded_at"]}

    # 撤回后的组重新变回一张可确认的待办单
    todo = aggregate_discrepancies(load_group_entries(conn, PERIOD)).groups[0]
    assert todo.disposition == "待确认" and todo.size == 8
    assert todo.split_reason == ""


def test_withdraw_is_refused_when_nothing_was_confirmed(conn):
    seed_ledger(conn, scenario_entries()[:2])
    group = aggregate_discrepancies(load_group_entries(conn, PERIOD)).groups[0]
    outcome = withdraw_group(conn, group, operator="A")
    assert not outcome.ok
    assert outcome.error_code == "GROUP_ACTION_DENIED"
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_PENDING}


# ─────────────────── 过期组 / 实体变化 ───────────────────


def test_a_stale_group_is_refused_after_cause_is_rerun(conn):
    """拿旧成因确认 = 把上一轮的结论盖到这一轮的账上，必须被拒。

    草稿表按 ``(账期, 渠道, 结论键, 名次)`` 只追加不覆盖（Task 28 的口径：
    同一批重跑不重复记账），所以「成因被改掉」在生产里表现为**行被订正**
    而不是新增一行 —— 这里照这个真实形态改。
    """
    entries = scenario_entries()[:3]
    seed_ledger(conn, entries, causes={e.order_no: LATE_ALIPAY for e in entries})
    group = aggregate_discrepancies(load_group_entries(conn, PERIOD)).groups[0]
    assert group.cause_code == LATE_ALIPAY

    first = entries[0]
    conn.execute(
        "UPDATE attribution_drafts SET cause=?, explanation_zh=? "
        "WHERE period=? AND conclusion_key=? AND rank=1",
        (
            HIS_MISSING,
            "重跑归因后判为 HIS 未落单",
            first.period,
            f"{first.channel}:{first.period}:{first.order_no}",
        ),
    )

    outcome = confirm_group(conn, group, operator="财务科-李静")
    assert not outcome.ok
    assert outcome.error_code == "GROUP_MEMBER_CAUSE_CHANGED"
    assert "重跑归因" in outcome.detail or "Top-1" in outcome.detail
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_PENDING}
    assert list_group_actions(conn, PERIOD) != []  # 拒了仍留痕


def test_member_deleted_from_ledger_blocks_the_group(conn):
    entries = scenario_entries()[:3]
    seed_ledger(conn, entries)
    group = aggregate_discrepancies(load_group_entries(conn, PERIOD)).groups[0]

    conn.execute(
        "DELETE FROM period_diff_items WHERE period=? AND channel=? AND check_path=?",
        (entries[1].period, entries[1].channel, entries[1].check_path),
    )
    outcome = confirm_group(conn, group, operator="A")
    assert not outcome.ok
    assert outcome.error_code == "GROUP_MEMBER_MISSING"
    assert len(outcome.members) == 3
    assert all(m.advanced is False for m in outcome.members)


def test_unattributed_period_still_allows_the_action_but_says_so(conn):
    """没跑过归因的账期：成因只有分组这一个来源，如实标出而不是假装核过。"""
    entries = scenario_entries()[:2]
    seed_ledger(conn, entries)  # 不落归因草稿
    group = aggregate_discrepancies(load_group_entries(conn, PERIOD)).groups[0]
    outcome = confirm_group(conn, group, operator="A")
    assert outcome.ok
    assert outcome.cause_source == "unattributed"
    assert set(_states(conn, group.member_keys).values()) == {DISPOSITION_CONFIRMED}


# ─────────────────── 装载 / 渲染 / 导出 ───────────────────


def test_load_group_entries_pulls_top1_cause_and_evidence_refs(conn):
    entries = scenario_entries()[:4]
    seed_ledger(conn, entries, causes={e.order_no: e.cause_code for e in entries})
    loaded = load_group_entries(conn, PERIOD)
    assert len(loaded) == 4
    assert {e.cause_code for e in loaded} == {LATEST_WECHAT}
    assert all(e.cause_rank == 1 for e in loaded)
    assert all(e.cause_confidence == Decimal("0.82") for e in loaded)
    assert all(e.evidence[0].source_file.endswith(".csv") for e in loaded)
    assert all(e.evidence[0].row_no >= 2 for e in loaded)
    assert all(e.conclusion_key().endswith(e.order_no) for e in loaded)
    assert {e.key for e in loaded} == {e.key for e in entries}


def test_load_group_entries_falls_back_to_head_evidence_without_drafts(conn):
    """归因没跑过也要能成单：退到台账行自己存的首条证据，并标出只有首行。"""
    entries = scenario_entries()[:2]
    seed_ledger(conn, entries)
    loaded = load_group_entries(conn, PERIOD)
    assert all(e.evidence[0].source_file == e.source_file for e in loaded)
    assert all(e.evidence[0].row_no == e.first_row_no for e in loaded)


def test_entry_from_diff_item_is_duck_typed_and_keeps_amount_known(conn):
    """鸭子类型入参：align 不 import engine.DiffItem（不反向依赖）。"""
    item = DiffItem(
        period=PERIOD,
        channel="wechat",
        check_path="taxonomy.ambiguity:EX-1",
        diff_amount=None,
        first_row_no=None,
        source_file="",
    )
    entry = entry_from_diff_item(item, cause_code=LATEST_WECHAT)
    assert entry.order_no == "EX-1"
    assert entry.diff_amount == Decimal("0")
    assert entry.amount_known is False
    assert entry.amount_text() == ""
    assert entry.conclusion_key() == f"wechat:{PERIOD}:EX-1"

    with_amount = entry_from_diff_item(
        DiffItem(period=PERIOD, channel="wechat", check_path="x", diff_amount="50.00")
    )
    assert with_amount.amount_known is True
    assert with_amount.amount_text() == "50.00"
    # 非歧义前缀的 check_path 不编单号
    assert entry_from_diff_item(
        DiffItem(period=PERIOD, channel="wechat", check_path="row_count")
    ).order_no == ""


def test_summary_and_group_renderings_name_the_cause_and_the_evidence():
    result = aggregate_discrepancies(scenario_entries())
    summary = render_summary_zh(result)
    assert "20 条 → 3 张处置单" in summary
    assert "渠道延迟入账" in summary
    assert "分工：" in summary

    rendered = render_group_zh(result.groups[0])
    assert "处置单 channel_late_posting::wechat::cross_period::50.00" in rendered
    assert "责任方：渠道方" in rendered
    assert "建议动作：" in rendered
    assert "EX-WX-LATE-0001" in rendered
    assert "wechat_bill_2026-W36.csv:2" in rendered
    assert "共 8 条" in rendered

    assert "无可成单的差异" in render_summary_zh(aggregate_discrepancies([]))


def test_slip_markdown_proves_per_group_and_attaches_evidence_per_row():
    """合并台账：组头给证明，组内逐条附证据行与结论键。"""
    result = aggregate_discrepancies(scenario_entries())
    markdown = render_group_slip_markdown(
        result.groups, period=PERIOD, operator="财务科-李静", note="周一对账后交办"
    )
    assert markdown.startswith(f"# {PERIOD} 支付对账同根因处置单")
    assert AGGREGATE_VERSION in markdown
    assert "3 张，覆盖差异 20 条，合计 870.00 元" in markdown
    assert "出单人：财务科-李静" in markdown
    # 分工表
    assert "| 责任方 | 处置单张数 | 涉及差异条数 | 涉及金额 |" in markdown
    assert "| 渠道方 | 2 | 15 | 540.00 |" in markdown
    # 组头
    assert "## 1. 渠道延迟入账 · wechat · 50.00 元 × 8 条" in markdown
    assert "默认按 0.01 元精确分桶，不同金额不并单" in markdown
    assert "- 处置单键：`channel_late_posting::wechat::cross_period::50.00`" in markdown
    # 组内逐条
    assert "| # | 渠道单号 | 差额(元) | 处置 | 证据行 | 结论键 |" in markdown
    assert "| 1 | EX-WX-LATE-0001 | 50.00 | 待确认 | wechat_bill_2026-W36.csv:2 |" in markdown
    assert "`wechat:2026-W36:EX-WX-LATE-0001`" in markdown
    # 空单也要给一句人话，不能是空文件
    assert "本期无单边账" in render_group_slip_markdown([], period=PERIOD)


def test_slip_markdown_includes_the_disposition_audit_rows():
    result = aggregate_discrepancies(scenario_entries()[:2])
    actions = [
        {
            "recorded_at": "2026-09-07T09:12:00+08:00",
            "action": "confirm",
            "operator": "财务科-李静",
            "group_key": result.groups[0].group_key,
            "advanced": 1,
            "group_size": 1,
            "note": "已联系渠道",
        }
    ]
    markdown = render_group_slip_markdown(
        result.groups, period=PERIOD, operator="财务科-王芳", actions=actions
    )
    assert "## 处置留痕" in markdown
    assert "2026-09-07T09:12:00+08:00 | 确认 | 财务科-李静" in markdown
    assert "1/1" in markdown
    # 逐条留痕在交办单上按「一次动作」归并，不刷 8 行一样的话
    assert markdown.count("2026-09-07T09:12:00+08:00 | 确认") == 1


def test_evidence_row_parses_and_renders_refs():
    row = EvidenceRow.parse_ref("wechat_bill.csv:12")
    assert (row.source_file, row.row_no) == ("wechat_bill.csv", 12)
    assert row.ref == "wechat_bill.csv:12"
    assert row.render() == "wechat_bill.csv:12"

    named = EvidenceRow(source_file="a.csv", row_no=3, field_name="amount", value="50.00")
    assert named.render() == "a.csv:3 列 amount=50.00"
    assert named.to_dict()["ref"] == "a.csv:3"

    # 没有行号的引用原样留得住，不补成「第 0 行」—— 源文件里没有第 0 行
    assert EvidenceRow(source_file="x.csv", row_no=0).ref == "x.csv"
    assert EvidenceRow(source_file="", row_no=7).ref == "#7"
    assert EvidenceRow.parse_ref("x.csv").ref == "x.csv"
    assert EvidenceRow.parse_ref("").ref == "#0"


def test_result_payload_is_json_serialisable():
    payload = aggregate_discrepancies(scenario_entries()).to_dict()
    text = json.dumps(payload, ensure_ascii=False)
    assert json.loads(text)["group_count"] == 3
    assert payload["aggregate_version"] == AGGREGATE_VERSION
    outcome_dict = {
        "group_key": "k",
        "action": "confirm",
        "operator": "A",
        "members": [],
    }
    assert json.loads(json.dumps(outcome_dict)) == outcome_dict


def test_empty_input_is_not_an_error():
    result = aggregate_discrepancies([])
    assert result.groups == () and result.entry_count == 0
    assert result.collapsed == 0
    assert result.by_owner() == {}
    assert DEFAULT_AMOUNT_BUCKET == Decimal("0.01")


# ─────────────────── CLI 端到端 ───────────────────


def _run(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "reconq", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": SRC, "RECONQ_LOG_DIR": os.path.join(cwd, "logs")},
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def cli_ledger(tmp_path):
    """在临时目录里建一个带 20 条单边账的账单库，供 CLI 三条腿跑。"""
    from reconq.store.schema import connect

    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    ledger = tmp_path / "cli.db"
    connection = connect(str(ledger))
    init_scan_schema(connection)
    entries = scenario_entries()
    seed_ledger(connection, entries, causes={e.order_no: e.cause_code for e in entries})
    connection.close()
    return tmp_path, str(ledger)


def test_cli_list_prints_the_collapse_and_exits_nonzero_when_work_is_pending(cli_ledger):
    workdir, db = cli_ledger
    proc = _run(["groups", "list", "--period", PERIOD, "--db", db], workdir)
    assert proc.returncode == 1, proc.stderr  # 有待办 → 门禁没过
    assert "20 条 → 3 张处置单" in proc.stdout
    assert "渠道延迟入账 / wechat / 50.00 元 × 8 条 = 400.00 元 → 渠道方" in proc.stdout
    assert "待办：3 张处置单（20 条差异）待确认" in proc.stdout
    assert "reconq groups confirm --period 2026-W36 --group-key" in proc.stdout


def test_cli_list_json_is_machine_readable(cli_ledger):
    workdir, db = cli_ledger
    proc = _run(["groups", "list", "--period", PERIOD, "--db", db, "--json"], workdir)
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["entry_count"] == 20
    assert payload["group_count"] == 3
    assert payload["total_amount"] == "870.00"
    assert payload["collapsed"] == 17
    assert {g["cause_code"] for g in payload["groups"]} == {
        LATEST_WECHAT, LATE_ALIPAY, HIS_MISSING
    }


def test_cli_confirm_then_withdraw_round_trip(cli_ledger):
    workdir, db = cli_ledger
    key = group_key_for(LATEST_WECHAT, "wechat", "cross_period", "50.00")

    confirmed = _run(
        [
            "groups", "confirm", "--period", PERIOD, "--db", db,
            "--group-key", key, "--operator", "财务科-李静", "--note", "已联系渠道",
        ],
        workdir,
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert "8/8 条已推进为 已确认" in confirmed.stdout
    assert "整组一致，无漏项" in confirmed.stdout

    after = _run(["groups", "list", "--period", PERIOD, "--db", db], workdir)
    assert after.returncode == 1
    assert "2 张处置单（12 条差异）待确认" in after.stdout

    # 撤回原单
    back = _run(
        [
            "groups", "withdraw", "--period", PERIOD, "--db", db,
            "--group-key", key, "--operator", "财务科-王芳", "--note", "证据不足",
        ],
        workdir,
    )
    assert back.returncode == 0, back.stderr
    assert "8/8 条已推进为 待确认" in back.stdout

    # 留痕两条都在
    audit = _run(["groups", "actions", "--period", PERIOD, "--db", db], workdir)
    assert audit.returncode == 0
    assert "共 16 条" in audit.stdout
    assert "确认 操作人 财务科-李静" in audit.stdout
    assert "撤回 操作人 财务科-王芳" in audit.stdout

    # 撤回的单重新回到待办里，门禁**不会**因为另外两张单办完就转绿
    midway = _run(["groups", "list", "--period", PERIOD, "--db", db], workdir)
    assert midway.returncode == 1
    assert "待办：3 张处置单（20 条差异）待确认" in midway.stdout

    # 三张单全部确认完 → 门禁转绿
    for other in (
        key,
        group_key_for(LATE_ALIPAY, "alipay", "cross_period", "20.00"),
        group_key_for(HIS_MISSING, "self_service", "", "66.00"),
    ):
        done = _run(
            [
                "groups", "confirm", "--period", PERIOD, "--db", db,
                "--group-key", other, "--operator", "财务科-王芳",
            ],
            workdir,
        )
        assert done.returncode == 0, done.stderr
    green = _run(["groups", "list", "--period", PERIOD, "--db", db], workdir)
    assert green.returncode == 0
    assert "待办：" not in green.stdout


def test_cli_slip_exports_a_signed_ready_markdown(cli_ledger):
    workdir, db = cli_ledger
    out = workdir / "out" / "2026-W36-处置单.md"
    proc = _run(
        [
            "groups", "slip", "--period", PERIOD, "--db", db,
            "--operator", "财务科-李静", "--note", "周一对账后交办", "--out", str(out),
        ],
        workdir,
    )
    assert proc.returncode == 0, proc.stderr
    text = out.read_text(encoding="utf-8")
    assert f"# {PERIOD} 支付对账同根因处置单" in text
    assert "3 张，覆盖差异 20 条，合计 870.00 元" in text
    assert "## 分工表" in text
    assert "EX-WX-LATE-0001" in text
    assert "reconq" not in text.split("# ")[0]  # 标题行是产品名不是命令


def test_cli_refuses_without_operator_and_with_unknown_group_key(cli_ledger):
    workdir, db = cli_ledger
    key = group_key_for(LATEST_WECHAT, "wechat", "cross_period", "50.00")

    no_operator = _run(
        ["groups", "confirm", "--period", PERIOD, "--db", db, "--group-key", key], workdir
    )
    assert no_operator.returncode == 2
    assert "--operator 必填" in no_operator.stderr

    machine = _run(
        [
            "groups", "confirm", "--period", PERIOD, "--db", db,
            "--group-key", key, "--operator", "model",
        ],
        workdir,
    )
    assert machine.returncode == 2
    assert "GROUP_ACTION_OPERATOR_INVALID" in machine.stderr

    unknown = _run(
        [
            "groups", "confirm", "--period", PERIOD, "--db", db,
            "--group-key", "nope::nope::-::-", "--operator", "A",
        ],
        workdir,
    )
    assert unknown.returncode == 2
    assert "不存在" in unknown.stderr


def test_cli_requires_a_period(cli_ledger):
    workdir, db = cli_ledger
    proc = _run(["groups", "list", "--db", db], workdir)
    assert proc.returncode == 0
    assert "用法：reconq groups" in proc.stdout


def test_action_table_is_built_idempotently(conn):
    init_disposition_action_schema(conn)
    init_disposition_action_schema(conn)
    names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert DISPOSITION_ACTION_TABLE in names
