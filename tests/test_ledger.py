"""差异处置单状态机与三角色确认流（Task 43）。

五类规定断言逐条可核：

1. **合法迁移全路径** —— ``待归因→待确认→已确认`` 全程走得通，每一步的
   操作人、时刻、版本号都落库；**驳回**与**撤回**两条分支同样走得通；
   存疑账期多走一道 ``待主管复核``，普通账期不绕这一道。
2. **非法跳转被拒** —— spec 点名的 ``已确认 → 待归因`` 直跳、``待归因 →
   已确认`` 跳关、``已确认 → 已驳回`` 直翻案一律被拒，**台账零改动**，
   错误详情里写明当前可去落点；被拒的点击**仍然留痕**。
3. **撤回** —— 两个终态都能撤回重置为 ``待确认``，**不销号**：
   经办 / 审核 / 主管三列都留着，版本号照常 +1，留痕表里「确认过」与
   「撤回过」两条同时在。
4. **乐观锁并发冲突** —— 同一份版本号并发确认只有一次生效，另一条拿到
   ``LEDGER_VERSION_CONFLICT`` 且状态不被覆盖；单条入口不传版本号直接
   被拒。
5. **批量部分失败** —— 一批里混进一条状态不允许的，其余照常推进，逐条
   结果说清哪条没动、卡在哪、现在能去哪；批量**不**整批回滚（与组级
   处置单「整组要么全动要么全不动」刻意相反，理由见 flow 模块头）。

外加几条容易被糊弄过去的性质：模型 / 规则 / 降级路径**推不动**状态；
经办人不得自己审自己（不相容岗位分离）；开单幂等；错误码元数据齐全。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from reconq.errors import ERROR_CODES, ReconqError
from reconq.ledger import flow, models
from reconq.ledger.models import (
    LEDGER_VERSION,
    STATUS_CONFIRMED,
    STATUS_PENDING_ATTRIBUTION,
    STATUS_PENDING_CONFIRM,
    STATUS_PENDING_SUPERVISOR,
    STATUS_REJECTED,
    Discrepancy,
    LedgerAction,
    Role,
)

PERIOD = "2026-W36"
HANDLER = "经办-张工"
REVIEWER = "审核-李工"
SUPERVISOR = "主管-王工"
CHECK_PATH = "taxonomy.ambiguity:EX-WX-0001"


# ─────────────────────────── 夹具 ───────────────────────────


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    models.init_ledger_schema(c)
    yield c
    c.close()


@pytest.fixture()
def item(conn):
    return flow.open_discrepancy(
        conn,
        period=PERIOD,
        channel="wechat",
        check_path=CHECK_PATH,
        order_no="EX-WX-0001",
        diff_amount="66.00",
        evidence_rows=("wechat_2026W36.csv:18", "his_2026W36.csv:41"),
    )


def _submit(conn, key=CHECK_PATH, who=HANDLER, conclusion="渠道延迟入账，次日已到账"):
    current = flow.require(conn, models.diff_key(PERIOD, "wechat", key))
    return flow.submit(
        conn,
        models.diff_key(PERIOD, "wechat", key),
        conclusion,
        operator=who,
        expected_version=current.version,
    )


def _approve(conn, key=CHECK_PATH, who=REVIEWER, role=Role.REVIEWER):
    current = flow.require(conn, models.diff_key(PERIOD, "wechat", key))
    return flow.approve(
        conn,
        models.diff_key(PERIOD, "wechat", key),
        operator=who,
        expected_version=current.version,
        role=role,
    )


def _mark_period_disputed(conn, period=PERIOD):
    """把账期扫描实例标成「已存疑」——存疑期的新开单一律须经主管。"""
    from reconq.engine.period import STATUS_DISPUTED, init_scan_schema, upsert_channel_results

    init_scan_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO period_scans (period, status, updated_at) VALUES (?,?,?)",
        (period, STATUS_DISPUTED, "2026-09-07T10:00:00+08:00"),
    )


# ─────────────────────────── 1. 合法迁移全路径 ───────────────────────────


def test_full_legal_path_records_who_when_and_which_version(conn, item):
    key = item.key
    assert item.status == STATUS_PENDING_ATTRIBUTION
    assert item.version == 0

    out = _submit(conn)
    assert out.ok and out.before == STATUS_PENDING_ATTRIBUTION
    assert out.after == STATUS_PENDING_CONFIRM and out.version == 1

    out2 = _approve(conn)
    assert out2.ok and out2.after == STATUS_CONFIRMED and out2.version == 2

    final = flow.require(conn, key)
    assert final.status == STATUS_CONFIRMED
    assert final.handler == HANDLER and final.handler_at
    assert final.reviewer == REVIEWER and final.reviewer_at
    assert final.conclusion == "渠道延迟入账，次日已到账"
    # 普通账期**不**绕主管那一道：三岗里只签了两行。
    assert final.signoff_roles == ("handler", "reviewer")
    assert final.requires_supervisor is False

    # 留痕表逐条记下「谁、何时、从哪到哪」
    trail = flow.list_actions(conn, key=key)
    assert [(a["action"], a["from_status"], a["to_status"]) for a in trail] == [
        (LedgerAction.SUBMIT.value, STATUS_PENDING_ATTRIBUTION, STATUS_PENDING_CONFIRM),
        (LedgerAction.APPROVE.value, STATUS_PENDING_CONFIRM, STATUS_CONFIRMED),
    ]
    assert [a["operator"] for a in trail] == [HANDLER, REVIEWER]
    assert all(a["ok"] == 1 and a["recorded_at"] for a in trail)


def test_reject_branch_is_reachable_from_pending_confirm(conn, item):
    _submit(conn)
    current = flow.require(conn, item.key)
    out = flow.reject(
        conn, item.key, operator=REVIEWER, expected_version=current.version
    )
    assert out.ok and out.after == STATUS_REJECTED
    assert flow.require(conn, item.key).status == STATUS_REJECTED


def test_disputed_period_forces_a_third_signoff(conn):
    """存疑账期 → 审核通过后落「待主管复核」，主管签字才终态。"""
    _mark_period_disputed(conn)
    item = flow.open_discrepancy(
        conn,
        period=PERIOD,
        channel="wechat",
        check_path=CHECK_PATH,
        diff_amount="66.00",
    )
    # 标记在**开单那一刻**就定死，不在审核那一刻才挑。
    assert item.requires_supervisor is True

    _submit(conn)
    out = _approve(conn)
    assert out.ok and out.after == STATUS_PENDING_SUPERVISOR

    current = flow.require(conn, item.key)
    final = flow.approve(
        conn,
        item.key,
        operator=SUPERVISOR,
        expected_version=current.version,
        role=Role.SUPERVISOR,
    )
    assert final.ok and final.after == STATUS_CONFIRMED
    assert flow.require(conn, item.key).signoff_roles == (
        Role.HANDLER.value,
        Role.REVIEWER.value,
        Role.SUPERVISOR.value,
    )


def test_supervisor_cannot_sign_on_a_normal_period_as_a_shortcut(conn, item):
    """主管不能替审核把「待确认」直接签成终态 —— 那一关就得有人签。"""
    _submit(conn)
    out = _approve(conn, who=SUPERVISOR, role=Role.SUPERVISOR)
    assert not out.advanced and out.error_code == "LEDGER_ROLE_DENIED"
    assert flow.require(conn, item.key).status == STATUS_PENDING_CONFIRM
    # 被拒的点击留痕了，面板上答得出「为什么没动」
    assert flow.list_actions(conn, key=item.key)[-1]["ok"] == 0


# ─────────────────────────── 2. 非法跳转被拒 ───────────────────────────


@pytest.mark.parametrize(
    "advance_to,second_action,second_role,second_target",
    [
        (STATUS_CONFIRMED, LedgerAction.REJECT, Role.SUPERVISOR, STATUS_REJECTED),
        (STATUS_REJECTED, LedgerAction.SUBMIT, Role.HANDLER, STATUS_PENDING_CONFIRM),
    ],
)
def test_terminal_states_have_no_edge_back_to_pending_attribution(
    conn, item, advance_to, second_action, second_role, second_target
):
    """两个终态的出边**只有**「待确认」—— 直跳一律被拒且台账零改动。"""
    from reconq.ledger.flow import _single_transition

    _submit(conn)
    cur = flow.require(conn, item.key)
    if advance_to == STATUS_CONFIRMED:
        flow.approve(conn, item.key, operator=REVIEWER, expected_version=cur.version)
    else:
        flow.reject(conn, item.key, operator=REVIEWER, expected_version=cur.version)
    settled = flow.require(conn, item.key)
    assert settled.status == advance_to

    out = _single_transition(
        conn,
        settled,
        action=second_action,
        role=second_role,
        operator=SUPERVISOR,
        target=STATUS_PENDING_ATTRIBUTION,
        expected_version=settled.version,
    )
    assert not out.advanced
    assert out.error_code == "LEDGER_TRANSITION_DENIED"
    assert flow.require(conn, item.key).status == advance_to


def test_confirmed_cannot_jump_straight_back_to_pending_attribution(conn, item):
    """spec 验收点名：``已确认 → 待归因`` 直跳必须被拒并返回错误码。"""
    _submit(conn)
    _approve(conn)
    current = flow.require(conn, item.key)

    # 撤回的落点是「待确认」；把它强行改写成「待归因」必须被白名单挡下。
    from reconq.ledger.flow import _single_transition

    out = _single_transition(
        conn,
        current,
        action=LedgerAction.WITHDRAW,
        role=Role.HANDLER,
        operator=HANDLER,
        target=STATUS_PENDING_ATTRIBUTION,
        expected_version=current.version,
    )
    assert not out.advanced
    assert out.error_code == "LEDGER_TRANSITION_DENIED"
    assert "当前可去" in out.detail
    # 台账零改动
    assert flow.require(conn, item.key).status == STATUS_CONFIRMED
    # 但被拒的点击**留痕**了 —— 「我明明点了确认为什么没动」要答得出来。
    trail = flow.list_actions(conn, key=item.key)
    assert trail[-1]["ok"] == 0 and trail[-1]["error_code"] == "LEDGER_TRANSITION_DENIED"


def test_pending_attribution_cannot_skip_straight_to_confirmed(conn, item):
    """省掉「待确认」就等于省掉经办人写下的结论。"""
    from reconq.ledger.flow import _single_transition

    out = _single_transition(
        conn,
        item,
        action=LedgerAction.APPROVE,
        role=Role.REVIEWER,
        operator=REVIEWER,
        target=STATUS_CONFIRMED,
        expected_version=item.version,
    )
    assert not out.advanced and out.error_code == "LEDGER_TRANSITION_DENIED"
    assert flow.require(conn, item.key).status == STATUS_PENDING_ATTRIBUTION


def test_confirmed_cannot_be_flipped_to_rejected_without_withdrawing(conn, item):
    _submit(conn)
    _approve(conn)
    current = flow.require(conn, item.key)
    out = flow.reject(
        conn, item.key, operator=SUPERVISOR, expected_version=current.version, role=Role.SUPERVISOR
    )
    assert not out.advanced and out.error_code == "LEDGER_TRANSITION_DENIED"
    assert flow.require(conn, item.key).status == STATUS_CONFIRMED


def test_reopen_whitelist_has_no_edge_back_to_pending_attribution():
    """白名单表本身：两个终态的出边**只有**「待确认」。"""
    assert models.ALLOWED_TRANSITIONS[STATUS_CONFIRMED] == frozenset({STATUS_PENDING_CONFIRM})
    assert models.ALLOWED_TRANSITIONS[STATUS_REJECTED] == frozenset({STATUS_PENDING_CONFIRM})
    assert models.can_transition(STATUS_CONFIRMED, STATUS_PENDING_ATTRIBUTION) is False
    assert models.can_transition(STATUS_REJECTED, STATUS_PENDING_ATTRIBUTION) is False


# ─────────────────────────── 3. 撤回 ───────────────────────────


def test_withdraw_resets_to_pending_confirm_without_erasing_signoffs(conn, item):
    _submit(conn)
    _approve(conn)
    current = flow.require(conn, item.key)

    out = flow.withdraw(
        conn, item.key, operator=REVIEWER, expected_version=current.version, role=Role.REVIEWER
    )
    assert out.ok and out.after == STATUS_PENDING_CONFIRM and out.version == 3

    after = flow.require(conn, item.key)
    assert after.status == STATUS_PENDING_CONFIRM
    # **不销号**：结论单上「这单曾经被谁签过」不能因为撤回就消失。
    assert after.handler == HANDLER and after.reviewer == REVIEWER
    assert after.conclusion == "渠道延迟入账，次日已到账"

    trail = flow.list_actions(conn, key=item.key)
    assert [(a["action"], a["ok"]) for a in trail] == [
        (LedgerAction.SUBMIT.value, 1),
        (LedgerAction.APPROVE.value, 1),
        (LedgerAction.WITHDRAW.value, 1),
    ]


def test_rejected_item_can_also_be_withdrawn(conn, item):
    _submit(conn)
    current = flow.require(conn, item.key)
    flow.reject(conn, item.key, operator=REVIEWER, expected_version=current.version)
    current = flow.require(conn, item.key)
    out = flow.withdraw(
        conn, item.key, operator=REVIEWER, expected_version=current.version, role=Role.REVIEWER
    )
    assert out.ok and out.after == STATUS_PENDING_CONFIRM


# ─────────────────────────── 4. 乐观锁与并发 ───────────────────────────


def test_stale_version_conflicts_and_never_overwrites(conn, item):
    """两个面板都打开在第 0 版，各自点确认 —— 只有一次生效。"""
    key = item.key
    stale = flow.require(conn, key)  # 两边都拿到第 0 版

    first = flow.submit(
        conn, key, "渠道延迟入账", operator=HANDLER, expected_version=stale.version
    )
    assert first.ok and first.version == 1

    second = flow.submit(
        conn, key, "HIS 未落单", operator="经办-赵工", expected_version=stale.version
    )
    assert not second.advanced
    assert second.error_code == "LEDGER_VERSION_CONFLICT"
    # 状态与结论都没被后一次覆盖
    after = flow.require(conn, key)
    assert after.status == STATUS_PENDING_CONFIRM
    assert after.conclusion == "渠道延迟入账"
    assert after.version == 1


def test_concurrent_review_leaves_exactly_one_winner(conn, item):
    """并发审核（两个审核人拿同一份版本号）同样只有一次生效。"""
    _submit(conn)
    stale = flow.require(conn, item.key)

    first = flow.approve(conn, item.key, operator=REVIEWER, expected_version=stale.version)
    second = flow.approve(conn, item.key, operator="审核-钱工", expected_version=stale.version)

    assert first.advanced is True
    assert second.advanced is False and second.error_code == "LEDGER_VERSION_CONFLICT"
    after = flow.require(conn, item.key)
    assert after.reviewer == REVIEWER and after.version == 2


def test_single_entry_refuses_to_act_without_a_version(conn, item):
    with pytest.raises(ReconqError) as exc:
        flow.submit(conn, item.key, "渠道延迟入账", operator=HANDLER)
    assert exc.value.code == "LEDGER_VERSION_REQUIRED"
    assert flow.require(conn, item.key).status == STATUS_PENDING_ATTRIBUTION


def test_refresh_then_act_succeeds(conn, item):
    """冲突之后刷新重看再点，**是能走通的** —— 不是死锁。"""
    _submit(conn)
    stale = flow.require(conn, item.key)
    flow.approve(conn, item.key, operator=REVIEWER, expected_version=stale.version)

    fresh = flow.require(conn, item.key)
    out = flow.withdraw(
        conn, item.key, operator=REVIEWER, expected_version=fresh.version, role=Role.REVIEWER
    )
    assert out.ok and out.after == STATUS_PENDING_CONFIRM


# ─────────────────────────── 5. 批量部分失败 ───────────────────────────


def _open_three(conn, statuses=("待确认", "待确认", "已确认")):
    keys = []
    for i, want in enumerate(statuses, start=1):
        path = f"taxonomy.ambiguity:EX-WX-100{i}"
        flow.open_discrepancy(
            conn,
            period=PERIOD,
            channel="wechat",
            check_path=path,
            order_no=f"EX-WX-100{i}",
            diff_amount="10.00",
        )
        key = models.diff_key(PERIOD, "wechat", path)
        cur = flow.require(conn, key)
        flow.submit(conn, key, "渠道延迟入账", operator=HANDLER, expected_version=cur.version)
        if want == "已确认":
            cur = flow.require(conn, key)
            flow.approve(conn, key, operator=REVIEWER, expected_version=cur.version)
        keys.append(key)
    return keys


def test_batch_advances_the_rest_when_one_item_is_illegal(conn):
    keys = _open_three(conn)  # 第 3 条已是「已确认」，通过会被拒

    out = flow.batch_apply(
        conn, action=LedgerAction.APPROVE, role=Role.REVIEWER, operator=REVIEWER, period=PERIOD
    )
    assert out.advanced == 2 and len(out.failed) == 1
    assert not out.ok
    by_key = {i.key: i for i in out.items}
    assert by_key[keys[0]].after == STATUS_CONFIRMED and by_key[keys[0]].advanced
    assert by_key[keys[1]].after == STATUS_CONFIRMED and by_key[keys[1]].advanced
    assert by_key[keys[2]].advanced is False
    assert by_key[keys[2]].error_code == "LEDGER_TRANSITION_DENIED"
    assert STATUS_CONFIRMED in by_key[keys[2]].detail

    # 库上逐条跟上返回的结论
    assert flow.require(conn, keys[0]).status == STATUS_CONFIRMED
    assert flow.require(conn, keys[1]).status == STATUS_CONFIRMED
    assert flow.require(conn, keys[2]).status == STATUS_CONFIRMED


def test_batch_by_channel_filters_candidates(conn):
    _open_three(conn)
    flow.open_discrepancy(
        conn, period=PERIOD, channel="alipay", check_path="taxonomy.ambiguity:EX-ZF-9001",
        order_no="EX-ZF-9001", diff_amount="20.00",
    )
    out = flow.batch_apply(
        conn,
        action=LedgerAction.SUBMIT,
        role=Role.HANDLER,
        operator=HANDLER,
        channel="alipay",
        conclusion="支付宝侧延迟",
    )
    assert len(out.items) == 1 and out.advanced == 1
    assert out.items[0].after == STATUS_PENDING_CONFIRM


def test_batch_by_status_selects_only_the_named_status(conn):
    _open_three(conn)
    out = flow.batch_apply(
        conn, action=LedgerAction.WITHDRAW, role=Role.REVIEWER, operator=REVIEWER,
        period=PERIOD, status=STATUS_CONFIRMED,
    )
    assert len(out.items) == 1 and out.advanced == 1
    assert out.items[0].after == STATUS_PENDING_CONFIRM


def test_batch_leaves_trace_per_item_including_the_failures(conn):
    _open_three(conn)
    out = flow.batch_apply(
        conn, action=LedgerAction.APPROVE, role=Role.REVIEWER, operator=REVIEWER, period=PERIOD
    )
    trail = flow.list_actions(conn, period=PERIOD, batch_id=out.batch_id)
    assert len(trail) == 3
    assert sum(1 for a in trail if a["ok"] == 1) == 2
    failed = [a for a in trail if a["ok"] == 0]
    assert len(failed) == 1 and failed[0]["error_code"] == "LEDGER_TRANSITION_DENIED"


def test_batch_render_reports_both_halves(conn):
    _open_three(conn)
    out = flow.batch_apply(
        conn, action=LedgerAction.APPROVE, role=Role.REVIEWER, operator=REVIEWER, period=PERIOD
    )
    text = flow.render_batch_zh(out)
    assert "2/3" in text and "1 条未推进" in text
    assert "LEDGER_TRANSITION_DENIED" in text


def test_batch_on_empty_candidate_set_is_a_no_op_not_a_silent_success(conn):
    out = flow.batch_apply(
        conn, action=LedgerAction.APPROVE, role=Role.REVIEWER, operator=REVIEWER, period="1999-W01"
    )
    assert out.items == () and out.advanced == 0
    assert "候选集为空" in flow.render_batch_zh(out)


# ─────────────────────────── 岗位分离与操作人 ───────────────────────────


def test_one_person_cannot_hold_two_posts_on_the_same_item(conn, item):
    _submit(conn, who=SUPERVISOR)  # 让「经办」这一栏被主管本人占上
    current = flow.require(conn, item.key)
    out = flow.approve(
        conn, item.key, operator=SUPERVISOR, expected_version=current.version, role=Role.SUPERVISOR
    )
    assert not out.advanced and out.error_code == "LEDGER_ROLE_DENIED"

    # 换个真人来审就通得过 —— 拒绝的是「主管要兼这一岗」，不是「不许主管出面」。
    ok = flow.approve(
        conn, item.key, operator=REVIEWER, expected_version=current.version, role=Role.REVIEWER
    )
    assert ok.ok


def test_handler_cannot_self_review(conn, item):
    _submit(conn, who=HANDLER)
    current = flow.require(conn, item.key)
    out = flow.approve(
        conn, item.key, operator=HANDLER, expected_version=current.version, role=Role.REVIEWER
    )
    assert not out.advanced and out.error_code == "LEDGER_ROLE_SEPARATION_DENIED"
    assert "同一人不得再占" in out.detail
    assert flow.require(conn, item.key).status == STATUS_PENDING_CONFIRM
    assert flow.require(conn, item.key).reviewer == ""


@pytest.mark.parametrize("operator", ["model", "rule", "baseline", "系统", "自动", "", "-", "模型"])
def test_non_human_operator_cannot_advance_any_state(conn, item, operator):
    cur = flow.require(conn, item.key)
    with pytest.raises(ReconqError) as exc:
        flow.submit(conn, item.key, "草稿", operator=operator, expected_version=cur.version)
    assert exc.value.code == "LEDGER_OPERATOR_INVALID"
    assert flow.require(conn, item.key).status == STATUS_PENDING_ATTRIBUTION


def test_conclusion_is_mandatory_on_submit(conn, item):
    out = flow.submit(conn, item.key, "   ", operator=HANDLER, expected_version=item.version)
    assert not out.advanced and out.error_code == "LEDGER_CONCLUSION_REQUIRED"
    assert flow.require(conn, item.key).status == STATUS_PENDING_ATTRIBUTION
    assert flow.require(conn, item.key).version == 0


# ─────────────────────────── 开单与不变量 ───────────────────────────


def test_open_is_idempotent_and_never_resets_a_progressed_item(conn, item):
    again = flow.open_discrepancy(
        conn,
        period=PERIOD,
        channel="wechat",
        check_path=CHECK_PATH,
        order_no="EX-WX-0001",
        diff_amount="66.00",
    )
    assert again.key == item.key and again.status == STATUS_PENDING_ATTRIBUTION

    _submit(conn)
    after_progress = flow.open_discrepancy(
        conn, period=PERIOD, channel="wechat", check_path=CHECK_PATH, diff_amount="66.00"
    )
    assert after_progress.status == STATUS_PENDING_CONFIRM
    assert after_progress.version == 1


def test_unknown_action_and_role_are_refused_before_anything_is_written(conn, item):
    with pytest.raises(ReconqError) as exc:
        flow.batch_apply(
            conn, action="delete", role=Role.REVIEWER, operator=REVIEWER, period=PERIOD
        )
    assert exc.value.code == "LEDGER_ACTION_UNKNOWN"

    with pytest.raises(ReconqError) as exc:
        flow.batch_apply(
            conn, action=LedgerAction.APPROVE, role="财务总监", operator=REVIEWER, period=PERIOD
        )
    assert exc.value.code == "LEDGER_ROLE_INVALID"
    assert flow.list_actions(conn, key=item.key) == []


def test_missing_item_is_an_explicit_error_not_an_empty_result(conn):
    with pytest.raises(ReconqError) as exc:
        flow.require(conn, models.diff_key(PERIOD, "wechat", "taxonomy.ambiguity:EX-WX-9999"))
    assert exc.value.code == "LEDGER_DISCREPANCY_NOT_FOUND"


def test_counting_and_listing_answer_what_the_panel_shows(conn):
    _open_three(conn)  # 两条待确认、一条已确认
    counts = flow.count_by_status(conn, PERIOD)
    assert counts == {STATUS_PENDING_CONFIRM: 2, STATUS_CONFIRMED: 1}
    # 「待办」与「活单」不是一回事：已确认仍可撤回，但它不进待办栏。
    assert len(flow.list_discrepancies(conn, period=PERIOD, open_only=True)) == 2
    assert len(flow.list_discrepancies(conn, period=PERIOD, status=STATUS_CONFIRMED)) == 1


def test_ledger_never_stores_evidence_raw_lines(conn):
    """证据只存「文件名:行号」，不存整行原文。"""
    item = flow.open_discrepancy(
        conn,
        period=PERIOD,
        channel="wechat",
        check_path=CHECK_PATH,
        diff_amount="66.00",
        evidence_rows=("wechat_2026W36.csv:18",),
    )
    stored = flow.require(conn, item.key)
    assert stored.evidence_rows == ("wechat_2026W36.csv:18",)
    blob = repr(stored.to_dict())
    assert "身份证" not in blob and "raw" not in blob


def test_float_amount_is_refused(conn):
    with pytest.raises(ReconqError) as exc:
        flow.open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=CHECK_PATH, diff_amount=66.0
        )
    assert exc.value.code == "LEDGER_AMOUNT_FLOAT_REJECTED"


def test_ledger_tables_hold_no_patient_identifiable_columns(conn, item):
    cols = {
        str(r["name"])
        for r in conn.execute(
            "SELECT name FROM pragma_table_info(?)", (models.LEDGER_DISCREPANCY_TABLE,)
        )
    }
    banned = {"patient_name", "id_card", "phone", "inpatient_no", "his_patient_no"}
    assert not (cols & banned)
    assert {"order_no", "period", "channel", "check_path", "version"} <= cols


# ─────────────────────────── 错误码元数据 ───────────────────────────


@pytest.mark.parametrize(
    "code",
    [
        "LEDGER_SCHEMA_MISSING",
        "LEDGER_DISCREPANCY_NOT_FOUND",
        "LEDGER_STATE_INVALID",
        "LEDGER_TRANSITION_DENIED",
        "LEDGER_ACTION_UNKNOWN",
        "LEDGER_ROLE_INVALID",
        "LEDGER_ROLE_DENIED",
        "LEDGER_ROLE_SEPARATION_DENIED",
        "LEDGER_OPERATOR_INVALID",
        "LEDGER_VERSION_CONFLICT",
        "LEDGER_VERSION_REQUIRED",
        "LEDGER_CONCLUSION_REQUIRED",
        "LEDGER_CONCLUSION_TOO_LONG",
        "LEDGER_AMOUNT_FLOAT_REJECTED",
    ],
)
def test_every_ledger_error_code_has_chinese_guidance(code):
    meta = ERROR_CODES[code]
    assert meta.zh and meta.suggestion
    # 兜底元数据的文案是「未登记的错误码」；登记表里出现它就等于这张码
    # 在 errors.py 里根本没登记，面板上只能显示一个裸英文串。
    assert "未登记的错误码" not in meta.zh


def test_ledger_version_is_recorded_on_every_row(conn, item):
    _submit(conn)
    row = conn.execute(
        f"SELECT ledger_version FROM {models.LEDGER_DISCREPANCY_TABLE} WHERE disc_key=?",
        (item.key,),
    ).fetchone()
    assert row["ledger_version"] == LEDGER_VERSION
    assert flow.require(conn, item.key).to_dict()["ledger_version"] == LEDGER_VERSION
