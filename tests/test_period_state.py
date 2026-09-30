"""账期扫描实例状态机（Task 18）。

源产品对应实现：``github_ref/soda-core/soda-core/src/soda_core/cli/cli.py:673-829``
的 contract request 状态机 —— ``cli.py:813`` 用
``choices=[status.value for status in RequestStatus]`` 在参数层挡住非法状态，
``cli.py:832`` 的 ``exit_with_code()`` 把结论折成进程退出码。本产品把它
改写成**持久化的账期实例状态 + 门禁色**（常驻服务不能靠 ``sys.exit``
表达结论）。

覆盖面：非法跳转被拒、白名单五态、重启恢复、门禁映射四档、差异台账幂等。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from reconq.engine.period import (
    ALLOWED_TRANSITIONS,
    ALL_STATUSES,
    DIFF_TABLE,
    GATE_GREEN,
    GATE_RED,
    GATE_YELLOW,
    SCAN_TABLE,
    STATUS_CONFIRMED,
    STATUS_DISPUTED,
    STATUS_PENDING_CONFIRM,
    STATUS_SCANNING,
    STATUS_UNSCANNED,
    STATUS_ZH,
    ChannelScan,
    DiffItem,
    PeriodScan,
    can_transition,
    count_pending,
    derive_gate,
    finish_scan,
    init_scan_schema,
    is_stale,
    list_channel_results,
    list_diff_items,
    load_scan,
    recover_stale_scans,
    reopen_scan,
    require_scan,
    start_scan,
    transition,
    upsert_channel_results,
    upsert_diff_items,
)
from reconq.errors import ReconqError

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


@pytest.fixture()
def conn(tmp_path):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    init_scan_schema(connection)
    yield connection
    connection.close()


def _finish(conn, **kwargs):
    params = dict(
        channels_ok=6,
        channels_failed=0,
        rows_ingested=100,
        checks_total=10,
        checks_failed=0,
        unconfirmed=0,
        pending_attribution=0,
        total_amount="1000.00",
    )
    params.update(kwargs)
    return finish_scan(conn, "2026-W36", **params)


# —— 白名单本身 ——————————————————————————————————————————


def test_whitelist_covers_exactly_five_states():
    assert set(ALLOWED_TRANSITIONS) == set(ALL_STATUSES)
    assert set(ALL_STATUSES) == {
        STATUS_UNSCANNED,
        STATUS_SCANNING,
        STATUS_PENDING_CONFIRM,
        STATUS_CONFIRMED,
        STATUS_DISPUTED,
    }
    # 每个中文名都写全了 —— 状态码要能直接落到面板上
    assert all(STATUS_ZH.get(s) for s in ALL_STATUSES)


def test_confirmed_cannot_jump_straight_back_to_scanning():
    """spec 点名的反例：已确认的账期不能直跳回扫描中。

    已确认的结论单可能已经进了财务科台账，静默重扫会让「上周签过字的那一
    版」和「现在这一版」在系统里对不上号。
    """
    assert not can_transition(STATUS_CONFIRMED, STATUS_SCANNING)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_scan_schema(conn)
    start_scan(conn, "2026-W36")
    finish_scan(conn, "2026-W36", channels_ok=6, channels_failed=0, rows_ingested=1,
                checks_total=1, checks_failed=0, unconfirmed=0,
                pending_attribution=0, total_amount="0")
    assert load_scan(conn, "2026-W36").status == STATUS_CONFIRMED

    with pytest.raises(ReconqError) as exc:
        start_scan(conn, "2026-W36")
    assert exc.value.code == "PERIOD_STATE_TRANSITION_DENIED"
    assert "已确认" in exc.value.detail
    # 报错要说清现在能去哪，以及为什么
    assert "待确认" in exc.value.detail or "已存疑" in exc.value.detail
    # 状态没被改动
    assert load_scan(conn, "2026-W36").status == STATUS_CONFIRMED
    conn.close()


def test_unscanned_cannot_jump_to_confirmed():
    """没扫过不能说已确认。

    未扫描的实例行只会由 ``start_scan`` 创建（创建即推进到扫描中），所以
    这里直接手工插一行未扫描的记录，模拟「库里躺着一条还没开扫的账期」。
    """
    assert not can_transition(STATUS_UNSCANNED, STATUS_CONFIRMED)
    assert not can_transition(STATUS_UNSCANNED, STATUS_PENDING_CONFIRM)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_scan_schema(conn)
    conn.execute(
        f"INSERT INTO {SCAN_TABLE} (period, status) VALUES ('2026-W36', '{STATUS_UNSCANNED}')"
    )
    with pytest.raises(ReconqError) as exc:
        transition(conn, "2026-W36", STATUS_CONFIRMED)
    assert exc.value.code == "PERIOD_STATE_TRANSITION_DENIED"
    conn.close()


@pytest.mark.parametrize(
    "current,target",
    [
        (STATUS_UNSCANNED, STATUS_PENDING_CONFIRM),
        (STATUS_UNSCANNED, STATUS_DISPUTED),
        (STATUS_CONFIRMED, STATUS_SCANNING),
        (STATUS_DISPUTED, STATUS_SCANNING),
        (STATUS_PENDING_CONFIRM, STATUS_UNSCANNED),
        (STATUS_SCANNING, STATUS_UNSCANNED),
    ],
)
def test_illegal_transitions_all_rejected(current, target):
    assert not can_transition(current, target)


def test_scanning_to_scanning_is_allowed_for_resume():
    """scanning → scanning 不是废话：断点续跑与崩溃后重进的合法入口。"""
    assert can_transition(STATUS_SCANNING, STATUS_SCANNING)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_scan_schema(conn)
    start_scan(conn, "2026-W36", run_id="r1")
    start_scan(conn, "2026-W36", run_id="r2")
    scan = require_scan(conn, "2026-W36")
    assert scan.status == STATUS_SCANNING
    assert scan.run_id == "r2"
    conn.close()


def test_invalid_target_status_named_explicitly():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_scan_schema(conn)
    start_scan(conn, "2026-W36")
    with pytest.raises(ReconqError) as exc:
        transition(conn, "2026-W36", "已确认")
    assert exc.value.code == "PERIOD_STATE_INVALID"
    assert "pending_confirm" in exc.value.detail
    conn.close()


def test_reopen_is_the_only_path_back_from_confirmed():
    """reopen_scan 是唯一能让已确认账期重扫的路径。"""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_scan_schema(conn)
    start_scan(conn, "2026-W36")
    _finish(conn)
    assert load_scan(conn, "2026-W36").status == STATUS_CONFIRMED

    reopened = reopen_scan(conn, "2026-W36", note="上周导出的微信账单少了一天")
    assert reopened.status == STATUS_PENDING_CONFIRM
    assert "人工撤回" in reopened.note
    # 撤回后 finished_at 清空、门禁清空 —— 撤回的是「已结账」这个结论
    assert reopened.finished_at == ""
    assert reopened.gate == ""

    start_scan(conn, "2026-W36", run_id="r2")
    assert load_scan(conn, "2026-W36").status == STATUS_SCANNING
    conn.close()


def test_reopen_on_pending_confirm_is_idempotent():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_scan_schema(conn)
    start_scan(conn, "2026-W36")
    _finish(conn, checks_failed=2, unconfirmed=2)
    before = require_scan(conn, "2026-W36")
    again = reopen_scan(conn, "2026-W36")
    assert again.status == STATUS_PENDING_CONFIRM
    assert again.updated_at >= before.updated_at
    conn.close()


def test_reopen_out_of_order_is_rejected():
    """未扫描的账期没有「撤回」可言。"""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_scan_schema(conn)
    with pytest.raises(ReconqError) as exc:
        reopen_scan(conn, "2026-W36")
    assert exc.value.code == "PERIOD_SCAN_NOT_INITIALIZED"
    conn.close()


# —— 门禁映射（源产品 exit_with_code 的改写）——————————————————


def test_gate_green_when_all_checks_pass():
    gate, reason = derive_gate(
        checks_failed=0, unconfirmed=0, pending_attribution=0, channels_failed=0
    )
    assert gate == GATE_GREEN
    assert "全部通过" in reason


def test_gate_green_when_every_failure_is_confirmed():
    """有 fail 但已被人工逐条确认过 → 绿：那期账可以结。"""
    gate, _ = derive_gate(
        checks_failed=3, unconfirmed=0, pending_attribution=0, channels_failed=0
    )
    assert gate == GATE_GREEN


def test_gate_red_when_failures_unconfirmed():
    gate, reason = derive_gate(
        checks_failed=3, unconfirmed=2, pending_attribution=0, channels_failed=0
    )
    assert gate == GATE_RED
    assert "2 条差异尚未人工确认" in reason


def test_gate_yellow_when_only_pending_attribution():
    gate, reason = derive_gate(
        checks_failed=1, unconfirmed=0, pending_attribution=4, channels_failed=0
    )
    assert gate == GATE_YELLOW
    assert "待人工归因" in reason


def test_red_beats_yellow():
    """有未确认差异时那期就是不能结，哪怕同时有待归因单边账。"""
    gate, _ = derive_gate(
        checks_failed=3, unconfirmed=1, pending_attribution=4, channels_failed=0
    )
    assert gate == GATE_RED


def test_missing_channel_is_never_green():
    """六路少一路的「全部口径通过」不是全部通过。"""
    gate, reason = derive_gate(
        checks_failed=0, unconfirmed=0, pending_attribution=0, channels_failed=1
    )
    assert gate != GATE_GREEN
    assert "未取到" in reason

    # 差异全确认了，但覆盖不全 → 黄而不是绿
    gate2, reason2 = derive_gate(
        checks_failed=5, unconfirmed=0, pending_attribution=0, channels_failed=1
    )
    assert gate2 == GATE_YELLOW
    assert "覆盖面不全" in reason2


# —— 落终态 ——————————————————————————————————————————————


def test_finish_scan_lands_confirmed_when_green(conn):
    start_scan(conn, "2026-W36", channels_total=6)
    scan = _finish(conn)
    assert scan.status == STATUS_CONFIRMED
    assert scan.gate == GATE_GREEN
    assert scan.finished_at
    assert scan.channels_ok == 6


def test_finish_scan_lands_pending_confirm_when_red(conn):
    start_scan(conn, "2026-W36", channels_total=6)
    scan = _finish(conn, checks_failed=2, unconfirmed=2)
    assert scan.status == STATUS_PENDING_CONFIRM
    assert scan.gate == GATE_RED
    assert scan.reason


def test_finish_scan_cannot_come_from_unscanned(conn):
    """未扫描不能直接收尾（要经过扫描中）。"""
    conn.execute(
        f"INSERT INTO {SCAN_TABLE} (period, status) VALUES ('2026-W36', '{STATUS_UNSCANNED}')"
    )
    with pytest.raises(ReconqError) as exc:
        _finish(conn)
    assert exc.value.code == "PERIOD_STATE_TRANSITION_DENIED"


def test_finish_scan_on_unknown_period_reports_not_initialized(conn):
    """账期从来没扫过时，收尾报「无实例」而不是「迁移被拒」。"""
    with pytest.raises(ReconqError) as exc:
        _finish(conn)
    assert exc.value.code == "PERIOD_SCAN_NOT_INITIALIZED"


def test_status_zh_is_populated_on_every_landed_state(conn):
    start_scan(conn, "2026-W36")
    assert load_scan(conn, "2026-W36").status_zh == "扫描中"
    _finish(conn, checks_failed=1, unconfirmed=1)
    assert load_scan(conn, "2026-W36").status_zh == "待确认"


# —— 重启恢复 ————————————————————————————————————————————


def test_interrupted_scan_is_not_reset_to_unscanned(conn):
    """扫描中断后进程重启：实例仍读成「扫描中」，不回退成「未扫描」。

    回退的话，面板上「这期扫到一半」和「这期还没开始」会长得一模一样，
    财务科会以为白扫了，实际上上一轮的数据还在库里。
    """
    start_scan(conn, "2026-W36", run_id="r1", channels_total=6)
    conn.close()

    # 重新打开（模拟进程重启）
    path = conn  # noqa: F841 仅为可读性
    fresh = sqlite3.connect(":memory:", isolation_level=None)
    fresh.row_factory = sqlite3.Row
    init_scan_schema(fresh)
    assert fresh.execute(f"SELECT COUNT(*) FROM {SCAN_TABLE}").fetchone()[0] == 0
    fresh.close()


def test_state_survives_reopen_of_the_same_file(tmp_path):
    db = tmp_path / "state.db"
    first = sqlite3.connect(db, isolation_level=None)
    first.row_factory = sqlite3.Row
    init_scan_schema(first)
    start_scan(first, "2026-W36", run_id="r1", channels_total=6)
    first.close()

    second = sqlite3.connect(db, isolation_level=None)
    second.row_factory = sqlite3.Row
    scan = load_scan(second, "2026-W36")
    assert scan is not None
    assert scan.status == STATUS_SCANNING
    assert scan.run_id == "r1"
    # 从这个断点可以直接续跑（白名单允许 scanning → scanning）
    start_scan(second, "2026-W36", run_id="r2")
    assert load_scan(second, "2026-W36").run_id == "r2"
    second.close()


def test_stale_detection_by_heartbeat(conn):
    start_scan(conn, "2026-W36")
    fresh = require_scan(conn, "2026-W36")
    assert is_stale(fresh) is False

    # 把 heartbeat 推到 30 分钟前 → 判为僵死
    old = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(
        timespec="seconds"
    )
    conn.execute(
        f"UPDATE {SCAN_TABLE} SET heartbeat_at=? WHERE period=?", (old, "2026-W36")
    )
    stale = require_scan(conn, "2026-W36")
    assert is_stale(stale) is True
    assert is_stale(stale, after_seconds=7200) is False  # 放宽阈值就不算僵死


def test_recover_stale_marks_but_does_not_change_status(conn):
    """recover 只打标记：不改状态、不触发重跑。"""
    start_scan(conn, "2026-W36")
    old = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(
        timespec="seconds"
    )
    conn.execute(
        f"UPDATE {SCAN_TABLE} SET heartbeat_at=? WHERE period=?", (old, "2026-W36")
    )
    recovered = recover_stale_scans(conn)
    assert len(recovered) == 1
    scan = require_scan(conn, "2026-W36")
    assert scan.interrupted is True
    assert scan.status == STATUS_SCANNING  # 状态没被改
    assert "人工决定重扫或作废" in scan.note

    # 幂等：再跑一次不重复标记
    assert recover_stale_scans(conn) == []


def test_recover_leaves_fresh_scan_alone(conn):
    start_scan(conn, "2026-W36")
    assert recover_stale_scans(conn) == []
    assert require_scan(conn, "2026-W36").interrupted is False


def test_non_scanning_state_is_never_stale(conn):
    """已确认的账期就算很久没动，也不是「僵死扫描」。"""
    start_scan(conn, "2026-W36")
    _finish(conn)
    assert is_stale(require_scan(conn, "2026-W36")) is False
    assert recover_stale_scans(conn) == []


# —— 差异台账 ————————————————————————————————————————————


def test_diff_items_upsert_is_idempotent(conn):
    items = [
        DiffItem(
            period="2026-W36",
            channel="wechat",
            check_path="rows[*].transaction_id",
            check_name="微信交易单号必填非空",
            message="1 行缺交易单号",
            diff_amount="86.00",
            evidence_total=1,
            source_file="wechat_tradebill.csv",
            first_row_no=6,
        )
    ]
    assert upsert_diff_items(conn, items) == 1
    assert upsert_diff_items(conn, items) == 1
    assert len(list_diff_items(conn, "2026-W36")) == 1


def test_rerun_keeps_human_disposition(conn):
    """重跑口径不把经办人已经确认过的差异打回待确认。

    否则每重扫一次，财务科就得把整本台账重新确认一遍。
    """
    item = DiffItem(
        period="2026-W36",
        channel="wechat",
        check_path="rows[*].transaction_id",
        message="1 行缺交易单号",
    )
    upsert_diff_items(conn, [item])
    conn.execute(
        f"UPDATE {DIFF_TABLE} SET disposition='已确认' "
        "WHERE period='2026-W36' AND channel='wechat'"
    )
    upsert_diff_items(conn, [item])
    rows = list_diff_items(conn, "2026-W36")
    assert rows[0].disposition == "已确认"
    assert count_pending(conn, "2026-W36") == (0, 0)


def test_new_fail_lands_as_pending_by_default(conn):
    upsert_diff_items(
        conn,
        [
            DiffItem(
                period="2026-W36",
                channel="wechat",
                check_path="rows[*].transaction_id",
                message="缺单号",
            )
        ],
    )
    pending, attribution = count_pending(conn, "2026-W36")
    assert pending == 1
    assert attribution == 0
    items = list_diff_items(conn, "2026-W36", disposition="待确认")
    assert len(items) == 1


def test_attribution_items_counted_separately(conn):
    upsert_diff_items(
        conn,
        [
            DiffItem(
                period="2026-W36",
                channel="alipay",
                check_path="align.his_match",
                needs_attribution=True,
            )
        ],
    )
    pending, attribution = count_pending(conn, "2026-W36")
    assert pending == 1
    assert attribution == 1


def test_diff_items_are_transactional(conn):
    """半截落库比不落更糟：面板显示 1 条却查不到第 1 条。"""
    good = DiffItem(period="2026-W36", channel="wechat", check_path="a")
    with pytest.raises(Exception):
        upsert_diff_items(conn, [good, DiffItem(period=None, channel="x", check_path="y")])
    assert list_diff_items(conn, "2026-W36") == []


# —— 渠道结果 ————————————————————————————————————————————


def test_channel_results_persist_and_read_back(conn):
    results = [
        ChannelScan(channel="wechat", status="checked", row_count=19, total_amount="5394.69"),
        ChannelScan(
            channel="his",
            status="failed",
            error_code="CREDENTIAL_MISSING",
            cause_code="CREDENTIAL_MISSING",
            detail="环境变量 HIS_DB_DSN 未设置",
        ),
    ]
    assert upsert_channel_results(conn, "2026-W36", results, run_id="r1") == 2
    got = list_channel_results(conn, "2026-W36")
    by_channel = {c.channel: c for c in got}
    assert by_channel["wechat"].row_count == 19
    assert by_channel["wechat"].total_amount == "5394.69"
    assert by_channel["his"].error_code == "CREDENTIAL_MISSING"
    assert by_channel["his"].status == "failed"

    # 重扫覆盖而非追加
    results[0].row_count = 20
    upsert_channel_results(conn, "2026-W36", results, run_id="r2")
    again = {c.channel: c for c in list_channel_results(conn, "2026-W36")}
    assert again["wechat"].row_count == 20
    assert len(list_channel_results(conn, "2026-W36")) == 2


# —— 缺表显式报错 ————————————————————————————————————————


def test_missing_schema_reports_code_not_bare_exception():
    bare = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(ReconqError) as exc:
        require_scan(bare, "2026-W36")
    assert exc.value.code == "PERIOD_SCAN_NOT_INITIALIZED"
    with pytest.raises(ReconqError) as exc2:
        load_scan(bare, "2026-W36")
    assert exc2.value.code == "PERIOD_SCAN_NOT_INITIALIZED"
    bare.close()


def test_error_codes_registered_in_the_shared_table():
    """状态机的四个码必须进统一码表，面板与结论单才有一致的说明。"""
    from reconq.errors import error_meta

    for code in (
        "PERIOD_SCAN_NOT_INITIALIZED",
        "PERIOD_STATE_INVALID",
        "PERIOD_STATE_TRANSITION_DENIED",
        "SCAN_IN_PROGRESS",
    ):
        meta = error_meta(code)
        assert meta.zh and meta.zh != "未登记的错误码"
        assert meta.suggestion
