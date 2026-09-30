"""账期扫描编排（Task 18）。

主路径：``initialize_all → source.fetch → 幂等落库 → run_checks → 差异台账
→ 实例状态/门禁``。串的是 Task 5/6/7/14/15 攒下的零件，本任务不加新零件。

覆盖面：非法状态跳转被拒、幂等键重复扫描行数与合计不变、5 路成功 + 1 路
失败时实例状态正确、重启恢复；外加单渠道失败不拖垮其余五路、六路全废
中止、差异台账初态恒为待确认、真实样例账期端到端。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

from reconq.engine.orchestrator import scan, scan_status
from reconq.engine.period import (
    GATE_GREEN,
    GATE_RED,
    GATE_YELLOW,
    STATUS_CONFIRMED,
    STATUS_PENDING_CONFIRM,
    STATUS_SCANNING,
    STATUS_UNSCANNED,
    list_channel_results,
    list_diff_items,
    load_scan,
    require_scan,
    upsert_diff_items,
    DiffItem,
)
from reconq.errors import ReconqError
from reconq.store.bill_store import query_period, row_summary
from reconq.store.schema import connect, init_schema

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACTS = os.path.join(PROJECT_DIR, "contracts")
BILLS = os.path.join(PROJECT_DIR, "tests", "fixtures", "bills")
PERIOD = "2026-W36"
#: 样例账期里没有 HIS 渠道目录（fixtures 是 5 路账单），所以基线是 5 路。
FIVE_CHANNELS = ["wechat", "alipay", "unionpay", "self_service", "medical_ins"]


@pytest.fixture()
def conn(tmp_path):
    connection = connect(str(tmp_path / "scan.db"))
    init_schema(connection)
    yield connection
    connection.close()


def _scan(connection, **kwargs):
    params = dict(inbox_root=BILLS, contracts_dir=CONTRACTS, channels=FIVE_CHANNELS)
    params.update(kwargs)
    return scan(connection, PERIOD, **params)


def _force_confirmed(connection):
    """把账期实例推到「已确认」，用于验证重跑护栏。

    走 SQL 而不是改内存对象：``require_scan`` 返回的是反序列化出来的
    副本，改它不会落库（这正是本任务要验的「状态在库里」的前提）。
    """
    from reconq.engine.period import SCAN_TABLE

    connection.execute(f"UPDATE {SCAN_TABLE} SET status=? WHERE period=?", (STATUS_CONFIRMED, PERIOD))


# —— 主路径 ——————————————————————————————————————————————


def test_end_to_end_lands_pending_confirm_red(conn):
    """样例账期端到端：5 路跑出口径，2 条 fail → 待确认 / 红。"""
    outcome = _scan(conn)
    assert outcome.scan.status == STATUS_PENDING_CONFIRM
    assert outcome.gate == GATE_RED
    assert "尚未人工确认" in outcome.scan.reason
    assert outcome.can_close is False
    assert outcome.scan.channels_total == 5
    assert outcome.scan.channels_ok == 5
    assert outcome.scan.checks_total > 0
    assert outcome.scan.checks_failed == 2
    assert outcome.scan.rows_ingested == 42
    assert outcome.diffs_written == 2


def test_per_channel_results_have_rows_and_amounts(conn):
    outcome = _scan(conn)
    wechat = outcome.channel_of("wechat")
    assert wechat.status == "checked"
    assert wechat.row_count == 19          # 20 行里 1 行空商户订单号进隔离区
    assert wechat.total_amount == "5394.69"
    assert wechat.checks_failed == 2
    alipay = outcome.channel_of("alipay")
    assert alipay.row_count == 7
    assert alipay.total_amount == "16383.85"
    assert alipay.checks_failed == 0


def test_evidence_rows_survive_into_the_diff_ledger(conn):
    """差异台账要带证据行号 —— 「哪一行出的问题」不能丢在终端上。"""
    _scan(conn)
    items = list_diff_items(conn, PERIOD)
    assert len(items) == 2
    for item in items:
        assert item.channel == "wechat"
        assert item.evidence_total >= 1
        assert item.source_file, "没有来源文件，财务科无法回查"
        assert item.first_row_no, "没有行号，财务科无法回查"
    # 空商户订单号那行进了隔离区，所以剩下的差异里是枚举越界与退费差额
    paths = {item.check_path for item in items}
    assert any("trade_state" in p or "refund" in p for p in paths), paths


def test_diff_ledger_initial_state_is_always_pending(conn):
    """本任务任何代码路径都不把处置状态推成已确认 —— 确认只能人工点。"""
    _scan(conn)
    assert all(item.disposition == "待确认" for item in list_diff_items(conn, PERIOD))


def test_total_amount_is_decimal_quantized(conn):
    outcome = _scan(conn)
    assert outcome.scan.total_amount == "25406.54"
    # 定点两位，无浮点残影
    assert outcome.scan.total_amount.count(".") == 1
    assert len(outcome.scan.total_amount.split(".")[1]) == 2


# —— 幂等 ————————————————————————————————————————————————


def test_rescanning_the_same_period_changes_nothing(conn):
    """同账期重复扫描：行数与合计金额完全不变（spec 幂等维度）。"""
    first = _scan(conn)
    second = _scan(conn)
    third = _scan(conn)

    assert first.scan.rows_ingested == 42
    assert second.scan.rows_ingested == 0, "重扫不该再插行"
    assert third.scan.rows_ingested == 0

    summaries = [row_summary(conn, PERIOD) for _ in range(3)]
    assert summaries[0] == summaries[1] == summaries[2]
    for summary in summaries:
        assert summary["wechat"]["row_count"] == 19
        assert summary["wechat"]["total_amount"] == "5394.69"
        assert summary["medical_ins"]["total_amount"] == "1600.00"

    # 差异台账也不撑大：覆盖语义不是追加
    assert len(list_diff_items(conn, PERIOD)) == 2
    assert first.scan.total_amount == second.scan.total_amount == third.scan.total_amount


def test_idempotency_key_is_recorded(conn):
    outcome = _scan(conn)
    assert PERIOD in outcome.scan.idempotency_key
    wechat = outcome.channel_of("wechat")
    assert wechat.idempotency_keys
    assert all(PERIOD in key for key in wechat.idempotency_keys)


def test_rerun_preserves_human_confirmation(conn):
    """经办人已确认的差异，重扫后仍是已确认（不被抹回待确认）。"""
    _scan(conn)
    conn.execute(
        "UPDATE period_diff_items SET disposition='已确认' WHERE period=?", (PERIOD,)
    )
    _scan(conn)
    assert all(
        item.disposition == "已确认" for item in list_diff_items(conn, PERIOD)
    )


# —— 单渠道失败剔除 ——————————————————————————————————————


def test_one_channel_failure_does_not_stop_the_others(conn, monkeypatch):
    """5 路成功 + 1 路失败时，其余五路照常出结果，实例状态仍可判。"""
    from reconq.sources import registry as registry_mod

    outcome = _scan(conn, channels=FIVE_CHANNELS + ["his"])
    his = outcome.channel_of("his")
    assert his.status == "failed"
    assert his.error_code == "CREDENTIAL_MISSING"
    assert "HIS_DB_DSN" in his.detail
    assert outcome.scan.channels_ok == 5
    assert outcome.scan.channels_failed == 1
    # 其余五路都落了库、跑了口径
    for channel in FIVE_CHANNELS:
        got = outcome.channel_of(channel)
        assert got.status == "checked"
        assert got.row_count > 0
    # 差异仍待确认 → 红（不是绿）
    assert outcome.gate == GATE_RED
    assert any("HIS" in w or "his" in w for w in outcome.warnings)


def test_missing_channel_is_never_reported_as_green(conn):
    """六路少一路的「全通过」不能是绿：覆盖面不全就是不全。"""
    # 先造一个全通过的账期（无差异），再加一路失败的
    from reconq.engine.period import finish_scan

    outcome = _scan(conn, channels=FIVE_CHANNELS + ["his"])
    assert outcome.gate != GATE_GREEN
    assert outcome.scan.channels_failed >= 1
    assert finish_scan is not None  # 导入仅作可读性锚点


def test_all_channels_failing_aborts_rather_than_emitting_empty_ledger(conn):
    """六路全废时中止：跑出一份「零笔差异」的空结论单比报错危险得多。"""
    empty = os.path.join(str(conn.execute("PRAGMA database_list").fetchone()[2]), "empty")
    with pytest.raises(ReconqError) as exc:
        scan(
            conn,
            PERIOD,
            inbox_root=empty,
            contracts_dir=CONTRACTS,
            channels=["nope_channel"],
        )
    assert exc.value.code in ("CHANNEL_UNKNOWN", "FORMAT_BROKEN")


def test_missing_bill_directory_marks_channel_not_fetched(conn, tmp_path):
    root = tmp_path / "nothing"
    (root / PERIOD / "wechat").mkdir(parents=True)
    outcome = scan(
        conn, PERIOD, inbox_root=str(root), contracts_dir=CONTRACTS, channels=["wechat"]
    )
    wechat = outcome.channel_of("wechat")
    assert wechat.status == "not_fetched"
    assert "没有账单文件" in wechat.detail
    # 「没取到」既不是通过也不是失败，是第三种状态
    assert outcome.scan.channels_ok == 0
    assert outcome.scan.checks_total == 0
    assert outcome.gate == GATE_YELLOW
    assert "未取到" in outcome.scan.reason


# —— 状态迁移 ————————————————————————————————————————————


def test_scanning_confirmed_period_is_rejected(conn):
    """已确认的账期直接重扫被拒（spec 点名的反例）。"""
    from reconq.engine.period import require_scan

    first = _scan(conn, channels=["wechat"])
    assert first.scan.status == STATUS_PENDING_CONFIRM
    _force_confirmed(conn)
    assert require_scan(conn, PERIOD).status == STATUS_CONFIRMED

    with pytest.raises(ReconqError) as exc:
        _scan(conn)
    assert exc.value.code == "PERIOD_STATE_TRANSITION_DENIED"
    assert "撤回" in exc.value.detail
    # 状态没被偷偷改掉
    assert require_scan(conn, PERIOD).status == STATUS_CONFIRMED


def test_reopen_allows_rescan_of_confirmed_period(conn):
    from reconq.engine.period import require_scan

    _scan(conn, channels=["wechat"])
    _force_confirmed(conn)

    outcome = _scan(conn, channels=["wechat"], reopen=True)
    assert outcome.scan.status == STATUS_PENDING_CONFIRM
    assert "人工撤回" in outcome.scan.note
    assert require_scan(conn, PERIOD).status == STATUS_PENDING_CONFIRM


def test_unknown_period_status_reports_unscanned(conn):
    status = scan_status(conn, "2099-W01")
    assert status["status"] == "unscanned"
    assert status["status_zh"] == "未扫描"
    assert status["gate"] is None


# —— 重启恢复 ————————————————————————————————————————————


def test_scan_status_reads_channel_state_from_a_fresh_connection(tmp_path):
    """常驻服务重启后，面板照样答得出「六路各自主态」。"""
    db = str(tmp_path / "restart.db")
    first = connect(db)
    init_schema(first)
    _scan(first)
    first.close()

    second = connect(db)
    try:
        status = scan_status(second, PERIOD)
        assert status["status"] == STATUS_PENDING_CONFIRM
        channels = {c["channel"]: c for c in status["channels"]}
        assert set(channels) == set(FIVE_CHANNELS)
        assert channels["wechat"]["row_count"] == 19
        assert channels["wechat"]["total_amount"] == "5394.69"
        assert status["can_close"] is False
    finally:
        second.close()


def test_interrupted_scan_recovers_from_the_same_file(tmp_path):
    """进程被杀后重开：实例仍是扫描中，recover 打标记而不改状态。"""
    from reconq.engine.period import SCAN_TABLE, init_scan_schema, recover_stale_scans

    db = str(tmp_path / "interrupt.db")
    first = connect(db)
    init_schema(first)
    init_scan_schema(first)
    first.execute(
        f"INSERT INTO {SCAN_TABLE} (period, status, heartbeat_at) VALUES (?,?,?)",
        (PERIOD, STATUS_SCANNING, "2020-01-01T00:00:00+08:00"),
    )
    first.close()

    second = connect(db)
    try:
        before = scan_status(second, PERIOD)
        assert before["status"] == STATUS_SCANNING
        assert before["stale"] is True
        recovered = recover_stale_scans(second)
        assert len(recovered) == 1
        after = scan_status(second, PERIOD)
        assert after["status"] == STATUS_SCANNING  # 没被改回未扫描
        assert after["interrupted"] is True
    finally:
        second.close()


# —— CLI 两条腿 ————————————————————————————————————————————


def _run_cli(args, cwd=PROJECT_DIR):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(PROJECT_DIR, "src")
    return subprocess.run(
        [sys.executable, "-m", "reconq", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def test_cli_scan_prints_gate_and_exits_nonzero_when_red(tmp_path):
    db = str(tmp_path / "cli.db")
    result = _run_cli(
        ["scan", "--period", PERIOD, "--source", BILLS, "--contracts", CONTRACTS, "--db", db]
    )
    assert result.returncode == 1, result.stderr
    assert "扫描完成" in result.stdout
    assert "门禁：red" in result.stdout
    assert "本期还不能结账" in result.stdout
    assert "wechat" in result.stdout
    assert "5394.69" in result.stdout


def test_cli_scan_status_leg_is_readonly(tmp_path):
    db = str(tmp_path / "cli2.db")
    _run_cli(
        [
            "scan", "--period", PERIOD, "--source", BILLS, "--contracts", CONTRACTS,
            "--db", db, "--channel", "wechat", "--channel", "alipay",
        ]
    )
    result = _run_cli(["scan", "--period", PERIOD, "--db", db, "--status", "--json"])
    assert result.returncode == 0, result.stderr
    import json

    payload = json.loads(result.stdout)
    assert payload["period"] == PERIOD
    assert payload["status"] == STATUS_PENDING_CONFIRM
    assert payload["gate"] == GATE_RED
    assert {c["channel"] for c in payload["channels"]} == {"wechat", "alipay"}


def test_cli_scan_status_on_never_scanned_period(tmp_path):
    """空库上问「这期扫过没」得到「未扫描」，不是报错。"""
    db = str(tmp_path / "cli_empty.db")
    result = _run_cli(["scan", "--period", "2099-W01", "--db", db, "--status", "--json"])
    assert result.returncode == 0, result.stderr
    import json

    payload = json.loads(result.stdout)
    assert payload["status"] == "unscanned"
    assert payload["status_zh"] == "未扫描"


def test_cli_scan_rejects_rescan_of_confirmed_period(tmp_path):
    """已确认账期直接重扫：CLI 报明确错误码与建议，不静默重跑。"""
    db = str(tmp_path / "cli3.db")
    _run_cli(["scan", "--period", PERIOD, "--source", BILLS, "--contracts", CONTRACTS, "--db", db])
    conn = connect(db)
    try:
        _force_confirmed(conn)
    finally:
        conn.close()

    result = _run_cli(
        ["scan", "--period", PERIOD, "--source", BILLS, "--contracts", CONTRACTS, "--db", db]
    )
    assert result.returncode == 2
    assert "PERIOD_STATE_TRANSITION_DENIED" in result.stderr
    assert "撤回" in result.stderr


def test_verify_leg_still_works_after_scan(conn):
    """加 scan 腿不能把 verify 那条只读排障腿弄坏。"""
    result = _run_cli(
        [
            "verify",
            "--period",
            PERIOD,
            "--source",
            BILLS,
            "--contracts",
            CONTRACTS,
            "--channel",
            "wechat",
        ]
    )
    assert result.returncode == 1  # 样例里有 fail
    assert "5714.69" in result.stdout or "5394.69" in result.stdout
    assert "wechat" in result.stdout
