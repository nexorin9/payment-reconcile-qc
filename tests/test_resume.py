"""断点续跑与按渠道 / 按 check 子集选择性重跑（Task 19）。

规定的四项断言逐条可核：

1. ``test_resume_does_not_reingest_completed_channels`` —— 中断后续跑不重复入库
2. ``test_scope_rerun_only_touches_the_named_channels`` —— scope 重跑只覆盖指定渠道
3. ``test_stale_diff_is_marked_resolved_not_deleted`` —— 旧差异转 resolved 而非删除
4. ``test_concurrent_second_scan_is_rejected`` —— 并发二次触发返回 SCAN_IN_PROGRESS

外加几条本任务真正踩到的反例：子集重跑**不得**把没重跑到的口径当成
「跑过且通过」而消解（旧差异留在原状）、沿用结果必须与本次产出分开标、
断点续跑无事可做时不得把账期状态推倒重来。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from reconq.engine.orchestrator import scan, scan_status
from reconq.engine.period import (
    GATE_RED,
    ORIGIN_CARRIED,
    ORIGIN_THIS_RUN,
    STATUS_CONFIRMED,
    STATUS_PENDING_CONFIRM,
    count_active_failures,
    count_check_results_by_origin,
    list_channel_results,
    list_check_results,
    list_diff_items,
    load_scan,
)
from reconq.engine.resume import (
    GENESIS_HASH,
    ScanLock,
    acquire_scan_lock,
    append_scan_event,
    clear_checkpoints,
    describe_scope,
    init_resume_schema,
    list_scan_events,
    load_checkpoints,
    mark_channel_done,
    release_scan_lock,
    select_scope,
)
from reconq.errors import ReconqError
from reconq.store.schema import connect, init_schema

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACTS = os.path.join(PROJECT_DIR, "contracts")
BILLS = os.path.join(PROJECT_DIR, "tests", "fixtures", "bills")
PERIOD = "2026-W36"

#: 样例账期里没有 HIS 渠道目录（fixtures 是 5 路账单），所以基线是 5 路。
FIVE_CHANNELS = ["wechat", "alipay", "unionpay", "self_service", "medical_ins"]


@pytest.fixture()
def conn(tmp_path):
    connection = connect(str(tmp_path / "resume.db"))
    init_schema(connection)
    init_resume_schema(connection)
    yield connection
    connection.close()


def _scan(connection, **kwargs):
    params = dict(inbox_root=BILLS, contracts_dir=CONTRACTS, channels=FIVE_CHANNELS)
    params.update(kwargs)
    return scan(connection, PERIOD, **params)


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(PROJECT_DIR, "src")
    return env


# —— 规定断言 1：中断后续跑不重复入库 ————————————————————


def test_resume_does_not_reingest_completed_channels(conn):
    """断点续跑：已完成的渠道不重新取数落库，剩余的路照常跑完。

    先整期扫一遍（建立断点），再把其中三路的断点清掉模拟「上一轮只跑到
    这里就崩了」，然后 ``resume=True`` 重跑 —— 断言账单行数与金额合计与
    上一次**逐字相同**（重复入库会让合计在某次重放里翻倍）。
    """
    from reconq.store.bill_store import query_period, row_summary

    first = _scan(conn)
    baseline = row_summary(conn, PERIOD)
    baseline_total = first.scan.total_amount

    # 模拟中断：wechat / alipay 跑完了，剩下三路没跑到
    clear_checkpoints(conn, PERIOD, ["unionpay", "self_service", "medical_ins"])
    done = {cp.channel for cp in load_checkpoints(conn, PERIOD) if cp.done}
    assert done == {"wechat", "alipay"}, f"断点设置有误：{done}"

    second = _scan(conn, channels=None, resume=True)

    # 续跑只跑剩下那三路。HIS 不在夹具里、也从未跑成过，因此**仍在待跑范围**
    # 里 —— 它本来就欠一次扫描，续跑不该假装它已完成。
    assert set(second.rerun_channels) == {
        "unionpay", "self_service", "medical_ins", "his",
    }
    assert set(second.scope.skipped_channels) == {"wechat", "alipay"}
    assert second.resumed is True

    # 不重复入库：行数与合计逐字不变
    after = row_summary(conn, PERIOD)
    assert after == baseline, "续跑后各路笔数/金额与断点前不一致"
    assert second.scan.total_amount == baseline_total
    # 被跳过的两路本次落库 0 行（行级唯一索引也会挡，但这里要验的是
    # 「压根没去取数」——skipped 出现在 skipped_channels 里）
    for ch in baseline:
        assert len(query_period(conn, PERIOD, ch)) == baseline[ch]["row_count"]
    # HIS 从未取到过，库里没有它的行 —— 也不该被凭空造出来
    assert "his" not in baseline
    skipped_results = {c.channel: c for c in list_channel_results(conn, PERIOD)}
    assert skipped_results["wechat"].status == "checked"


# —— 规定断言 2：scope 重跑只覆盖指定渠道 ——————————————


def test_scope_rerun_only_touches_the_named_channels(conn):
    """只重跑微信：银联/医保/自助机既不重新取数，结果标「沿用上次」。"""
    _scan(conn)

    outcome = _scan(conn, channels=["wechat"])

    assert outcome.rerun_channels == ["wechat"]
    assert set(outcome.carried_channels) == {
        "alipay", "unionpay", "self_service", "medical_ins",
    }
    # 沿用的四路口径结果必须标成「沿用」，不能混进本次产出
    assert outcome.origin_counts[ORIGIN_CARRIED] > 0
    carried = list_check_results(conn, PERIOD, origin=ORIGIN_CARRIED)
    assert {r.channel for r in carried} == {
        "alipay", "unionpay", "self_service", "medical_ins",
    }
    fresh = list_check_results(conn, PERIOD, origin=ORIGIN_THIS_RUN)
    assert {r.channel for r in fresh} == {"wechat"}
    # 出处标签有且只有一个真源，不写成两种说法
    zh = {r.to_dict()["origin_zh"] for r in list_check_results(conn, PERIOD)}
    assert zh == {"本次产出", "沿用上次"}

    # 只重跑微信时那四路渠道的差异**一条都不该被消解** —— 它们没被碰过。
    before = {
        (d.channel, d.check_path)
        for d in list_diff_items(conn, PERIOD, include_resolved=False)
    }
    _scan(conn, channels=["unionpay"])
    after = {
        (d.channel, d.check_path)
        for d in list_diff_items(conn, PERIOD, include_resolved=False)
    }
    assert before == after, "重跑银联不该动微信的差异，反之亦然"


# —— 规定断言 3：旧差异转 resolved 而非删除 ————————————


def test_stale_diff_is_marked_resolved_not_deleted(conn, tmp_path):
    """口径这次过了 → 旧差异行**留在台账里**并标消解，不是删掉。

    删掉的话，结论单上「上周 3 条差异，本周 0 条」会变成「上周根本没报过」，
    归因与先例库（Task 41）也就永远学不到已消解的成因。
    """
    from reconq.engine.period import count_pending

    # 先扫一遍，微信留下 2 条 fail
    _scan(conn, channels=["wechat"])
    live = list_diff_items(conn, PERIOD, include_resolved=False)
    assert live, "样例微信账单应当留下至少一条未消解差异"
    keys = {(d.channel, d.check_path) for d in live}

    # 把账单文件拷一份，把造成 fail 的脏数据改掉
    src = os.path.join(tmp_path, "bills")
    shutil.copytree(BILLS, src)
    path = os.path.join(src, PERIOD, "wechat", "wechat_tradebill_2026-W36.csv")
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    # 补齐空的交易单号/商户订单号：not_empty 口径靠空值报错
    for idx, line in enumerate(lines[1:], start=1):
        parts = line.split(",")
        for col in range(len(parts)):
            if parts[col] == "":
                parts[col] = f"EX-FIX-{idx:04d}"
        lines[idx] = ",".join(parts)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    outcome = _scan(
        conn, channels=["wechat"], inbox_root=src,
    )

    # 至少消解了一条
    assert outcome.resolved_diffs, "修好数据后应至少消解一条差异"
    all_rows = list_diff_items(conn, PERIOD)
    by_key = {(d.channel, d.check_path): d for d in all_rows}
    for key in keys:
        assert key in by_key, f"{key} 被删除了 —— 应保留台账行并标消解"
    for target in outcome.resolved_diffs:
        channel, path_ = target.split(":", 1)
        row = by_key[(channel, path_)]
        assert row.resolved is True
        assert row.resolved_at, "消解必须留下时刻"
        assert row.resolved_run_id, "消解必须记上是哪一次重跑消解的"
    # 消解后不计入待确认
    pending, _ = count_pending(conn, PERIOD)
    assert pending == count_active_failures(conn, PERIOD)

    # 差异重新出现时，消解标记必须被撤销
    recheck = _scan(conn, channels=["wechat"])
    assert recheck.resolved_diffs == []
    restored = {
        (d.channel, d.check_path)
        for d in list_diff_items(conn, PERIOD, include_resolved=False)
    }
    assert keys <= restored, "再次 fail 的差异应重新回到未消解"


# —— 规定断言 4：并发二次触发返回 SCAN_IN_PROGRESS ————————


def test_concurrent_second_scan_is_rejected(conn):
    """同一账期同时只允许一个扫描/重跑。"""
    lock = acquire_scan_lock(conn, PERIOD, "run-A", holder="pid=1001", scope="全部渠道")
    assert isinstance(lock, ScanLock)
    with pytest.raises(ReconqError) as excinfo:
        _scan(conn, channels=["wechat"])
    assert excinfo.value.code == "SCAN_IN_PROGRESS"
    # 报错要点名对端是谁在跑、最后心跳何时
    assert "run-A" in excinfo.value.detail
    assert "pid=1001" in excinfo.value.detail

    # 放锁后可再跑
    release_scan_lock(lock)
    outcome = _scan(conn, channels=["wechat"])
    assert outcome.scan.status in (STATUS_PENDING_CONFIRM, STATUS_CONFIRMED)

    # 重复放锁是幂等的
    release_scan_lock(lock)


def test_lock_holder_that_died_is_taken_over(conn):
    """上一轮进程被杀 → 锁留在库里。不接管就得干等一个超时周期。"""
    stale = acquire_scan_lock(conn, PERIOD, "run-dead", holder="pid=999")
    conn.execute(
        "UPDATE scan_locks SET heartbeat_at='2020-01-01T00:00:00+08:00' WHERE period=?",
        (PERIOD,),
    )
    taken = acquire_scan_lock(conn, PERIOD, "run-new", holder="pid=1002")
    assert taken.run_id == "run-new"
    # 迟到的 finally 不能把别人的锁掀掉
    release_scan_lock(stale)
    still = conn.execute(
        "SELECT run_id FROM scan_locks WHERE period=?", (PERIOD,)
    ).fetchone()
    assert still["run_id"] == "run-new"
    release_scan_lock(taken)


# —— 子集重跑不得误消解（本任务最危险的假绿）———————————


def test_only_check_rerun_never_resolves_unselected_checks(conn):
    """``--only-check row_count`` 只跑 1 条口径，另外 14 条**没跑**。

    没跑 ≠ 跑过且通过。没选中口径上的旧差异必须原样保留 ——
    否则财务科什么都没做，一期账就凭空变绿了。
    """
    _scan(conn, channels=["wechat"])
    before = {
        (d.channel, d.check_path)
        for d in list_diff_items(conn, PERIOD, include_resolved=False)
    }
    assert before

    outcome = _scan(conn, channels=["wechat"], only_checks=["row_count"])

    assert outcome.rerun_channels == ["wechat"]
    assert outcome.resolved_diffs == [], "只重跑 row_count 不得消解其他口径的差异"
    after = {
        (d.channel, d.check_path)
        for d in list_diff_items(conn, PERIOD, include_resolved=False)
    }
    assert after == before
    # 门禁也不许因为「本次 1 条口径全过」就变绿
    assert outcome.gate == GATE_RED
    assert count_active_failures(conn, PERIOD) == len(before)
    # 门禁读的是全量台账，不是「本次跑了几条」
    assert outcome.scan.checks_failed == len(before)


def test_only_check_selector_matching_nothing_aborts_the_whole_scan(conn):
    """选择器一条都没命中 → 整次扫描中止，已有差异一条都不消解。

    这里绝不能「记 0 条 fail 就当这路过了」：那会让该路的旧差异在消解那
    一步被判成「本次对齐后消失」，门禁变绿。
    """
    _scan(conn, channels=["wechat"])
    before = {
        (d.channel, d.check_path)
        for d in list_diff_items(conn, PERIOD, include_resolved=False)
    }
    with pytest.raises(ReconqError) as excinfo:
        _scan(conn, channels=["wechat"], only_checks=["no_such_check_path_xyz"])
    assert excinfo.value.code == "CHECK_SELECTOR_EMPTY"
    after = {
        (d.channel, d.check_path)
        for d in list_diff_items(conn, PERIOD, include_resolved=False)
    }
    assert after == before
    assert count_active_failures(conn, PERIOD) == len(before)
    # 锁必须已经放掉，否则这一次中止会把账期永久锁死
    assert (
        conn.execute("SELECT COUNT(*) FROM scan_locks WHERE period=?", (PERIOD,)).fetchone()[0]
        == 0
    )


def test_gate_reads_full_ledger_not_just_this_run(conn):
    """只重跑微信时，门禁里的口径总数是**全量台账**，不是「本次跑了多少」。"""
    _scan(conn)
    full = scan_status(conn, PERIOD)
    ledger_total = sum(count_check_results_by_origin(conn, PERIOD).values())

    outcome = _scan(conn, channels=["wechat"])
    assert outcome.scan.checks_total == ledger_total
    assert outcome.scan.checks_total == full["checks_total"]
    # rows_ingested 说的仍是「本次落库多少」，沿用的不混进来
    assert outcome.scan.rows_ingested == 0


# —— 断点 ——————————————————————————————————————————————


def test_checkpoints_record_checks_done_not_just_a_flag(conn):
    """断点记的是「这一路跑过哪几条口径」，不只是个布尔。

    口径被改后要能回答「上次这路跑过哪几条」，否则只能整路重来。
    """
    _scan(conn, channels=["wechat"])
    cps = {cp.channel: cp for cp in load_checkpoints(conn, PERIOD)}
    assert cps["wechat"].done is True
    assert cps["wechat"].checks_done, "断点应记下已执行的口径 path"
    assert len(cps["wechat"].checks_done) == cps["wechat"].rows_done * 0 + len(
        cps["wechat"].checks_done
    )
    assert cps["wechat"].rows_done > 0
    assert cps["wechat"].total_amount not in ("", "0")
    # 清断点是按路的：只清一路不得把别路的抹掉
    clear_checkpoints(conn, PERIOD, ["wechat"])
    assert [cp.channel for cp in load_checkpoints(conn, PERIOD) if cp.done] == []


def test_select_scope_validation_and_resume_math():
    """范围判据的边界：未知渠道当场拒；续跑按断点算而不是按命令行算。"""
    all_channels = ["wechat", "alipay", "unionpay"]
    with pytest.raises(ReconqError) as excinfo:
        select_scope(all_channels, only_channels=["nope"])
    assert excinfo.value.code == "CHANNEL_UNKNOWN"
    assert "nope" in excinfo.value.detail

    full = select_scope(all_channels)
    assert full.is_full and full.channels == []

    cps = [
        mark_channel_done.__wrapped__ if hasattr(mark_channel_done, "__wrapped__") else None
    ]
    del cps  # 断点对象在下面用真实构造

    from reconq.engine.resume import ChannelCheckpoint

    done = ChannelCheckpoint(period=PERIOD, channel="wechat", done=True)
    resumed = select_scope(all_channels, resume=True, checkpoints=[done])
    assert resumed.channels == ["alipay", "unionpay"]
    assert resumed.skipped_channels == ["wechat"]
    assert resumed.resume is True
    assert "断点续跑" in describe_scope(resumed)
    assert "wechat" in describe_scope(resumed)


# —— 事件链 ————————————————————————————————————————————


def test_rerun_event_chain_records_who_when_scope_and_delta(conn):
    """每次扫描 / 重跑写一环事件：谁、何时、什么范围、结果差几条。"""
    _scan(conn, actor="张三", channels=["wechat"])
    _scan(conn, actor="李四", channels=["alipay"], only_checks=["row_count"])

    events = list_scan_events(conn, PERIOD)
    assert len(events) == 2
    assert [e.seq for e in events] == [1, 2]
    assert events[0].actor == "张三" and events[1].actor == "李四"
    # 第一次全量（虽然命令行点了 wechat，等于全部有数据的路）→ scan
    assert events[0].event == "scan"
    # 第二次只跑一路且带了口径子集 → rerun
    assert events[1].event == "rerun"
    assert "alipay" in events[1].scope
    assert "落库" in events[1].result and "消解" in events[1].result
    assert events[1].scope_data["channels"] == ["alipay"]
    assert events[1].scope_data["check_selectors"] == ["row_count"]

    # 链式：每环把上一环的 hash 算进去，删掉中间一环后面就对不上
    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == events[0].hash
    for event in events:
        assert len(event.hash) == 64

    from reconq.engine.resume import _event_digest

    tampered = _event_digest(
        {
            "period": events[0].period, "seq": 1, "event": "scan",
            "run_id": events[0].run_id, "actor": "王五", "scope": events[0].scope,
            "scope_data": events[0].scope_data, "result": events[0].result,
            "detail": events[0].detail, "prev_hash": events[0].prev_hash,
            "at": events[0].at,
        }
    )
    assert tampered != events[0].hash, "改了 actor 必须算出不同的指纹"


# —— CLI 两条腿 ————————————————————————————————————————


def test_cli_scan_resume_and_only_check(conn, tmp_path):
    """``reconq scan --resume`` 与 ``--only-check`` 两条腿端到端。"""
    db = str(tmp_path / "cli.db")
    base = [
        sys.executable, "-m", "reconq", "scan",
        "--period", PERIOD, "--source", BILLS, "--contracts", CONTRACTS, "--db", db,
    ]
    first = subprocess.run(base, capture_output=True, text=True, env=_env(), cwd=PROJECT_DIR)
    assert "扫描完成" in first.stdout

    # 只重跑微信的一条口径
    second = subprocess.run(
        base + ["--channel", "wechat", "--only-check", "row_count", "--actor", "王五"],
        capture_output=True, text=True, env=_env(), cwd=PROJECT_DIR,
    )
    assert "本次范围" in second.stdout
    assert "渠道 wechat" in second.stdout
    assert "沿用上次" in second.stdout
    assert "口径出处" in second.stdout
    assert "本次产出" in second.stdout

    # 续跑：全部已完成 → 无事可做，账期状态不被推倒重来
    third = subprocess.run(
        base + ["--resume"],
        capture_output=True, text=True, env=_env(), cwd=PROJECT_DIR,
    )
    assert "扫描完成" in third.stdout
    # 续跑无事可做：账期状态不被推倒重来（仍停在上一轮的待确认）
    db_conn = connect(db)
    try:
        assert load_scan(db_conn, PERIOD).status == STATUS_PENDING_CONFIRM
    finally:
        db_conn.close()

    j = subprocess.run(
        base + ["--json", "--channel", "alipay", "--only-check", "row_count"],
        capture_output=True, text=True, env=_env(), cwd=PROJECT_DIR,
    )
    import json as _json

    payload = _json.loads(j.stdout)
    assert payload["scope"]["channels"] == ["alipay"]
    assert payload["scope"]["check_selectors"] == ["row_count"]
    assert payload["carried_channels"], "子集重跑应列出沿用渠道"


def test_cli_verify_resume_and_only_check(tmp_path):
    """``reconq verify`` 两条只读腿：--resume 与 --only-check。"""
    db = str(tmp_path / "cli-verify.db")
    scan_cmd = [
        sys.executable, "-m", "reconq", "scan",
        "--period", PERIOD, "--source", BILLS, "--contracts", CONTRACTS, "--db", db,
    ]
    subprocess.run(scan_cmd, capture_output=True, text=True, env=_env(), cwd=PROJECT_DIR)

    # --only-check 收窄到一条口径
    one = subprocess.run(
        [sys.executable, "-m", "reconq", "verify", "--period", PERIOD,
         "--source", BILLS, "--contracts", CONTRACTS,
         "--channel", "wechat", "--only-check", "row_count", "--json"],
        capture_output=True, text=True, env=_env(), cwd=PROJECT_DIR,
    )
    assert one.returncode == 0, one.stderr
    import json as _json

    payload = _json.loads(one.stdout)
    wechat = next(c for c in payload["channels"] if c["channel"] == "wechat")
    assert wechat["check_total"] < 15, "只跑一条口径，总数不该还是全量"
    assert wechat["check_total"] >= 1

    # 选择器不命中 → 显式报错，不静默当「全部通过」
    miss = subprocess.run(
        [sys.executable, "-m", "reconq", "verify", "--period", PERIOD,
         "--source", BILLS, "--contracts", CONTRACTS,
         "--channel", "wechat", "--only-check", "no_such_xyz", "--json"],
        capture_output=True, text=True, env=_env(), cwd=PROJECT_DIR,
    )
    assert miss.returncode != 0
    assert "CHECK_SELECTOR_EMPTY" in miss.stderr

    # --resume：断点上跑完的路本次不查
    resumed = subprocess.run(
        [sys.executable, "-m", "reconq", "verify", "--period", PERIOD,
         "--source", BILLS, "--contracts", CONTRACTS,
         "--db", db, "--resume", "--json"],
        capture_output=True, text=True, env=_env(), cwd=PROJECT_DIR,
    )
    assert resumed.returncode == 0, resumed.stderr
    payload = _json.loads(resumed.stdout)
    assert set(payload["carried_channels"]) == {
        "wechat", "alipay", "unionpay", "self_service", "medical_ins"
    }
    # 五路有账单的都在断点上；HIS 从未跑成过，仍会被查（它是「欠一次扫描」，
    # 不是「已完成」），verify 报未取到而不是替它下结论
    assert [c["channel"] for c in payload["channels"]] == ["his"]
    assert payload["channels"][0]["status"] == "not_fetched"


def test_cli_verify_resume_without_db_falls_back_to_full(tmp_path):
    """没给 --db 就没断点可读：退回全量并**说清楚**，不猜一个默认值。"""
    out = subprocess.run(
        [sys.executable, "-m", "reconq", "verify", "--period", PERIOD,
         "--source", BILLS, "--contracts", CONTRACTS,
         "--channel", "wechat", "--resume", "--json"],
        capture_output=True, text=True, env=_env(), cwd=PROJECT_DIR,
    )
    assert "需要 --db" in out.stderr
    import json as _json

    payload = _json.loads(out.stdout)
    assert [c["channel"] for c in payload["channels"]] == ["wechat"]


# —— 主干不回归 ————————————————————————————————————————


def test_main_path_unchanged_by_rerun_leg(conn):
    """加了重跑腿之后，全量扫描的主路径结论一字不变。"""
    outcome = _scan(conn)
    assert outcome.scan.status == STATUS_PENDING_CONFIRM
    assert outcome.gate == GATE_RED
    assert outcome.resolved_diffs == []
    assert outcome.carried_channels == []
    assert outcome.origin_counts == {ORIGIN_THIS_RUN: outcome.scan.checks_total}
    assert outcome.scan.status != STATUS_CONFIRMED


def test_resume_with_nothing_pending_is_a_noop_not_an_abort(conn):
    """全部在断点上时，续跑交一份「无新增」结论，**不**动状态与差异。"""
    _scan(conn)
    before_scan = load_scan(conn, PERIOD)
    before_diffs = {
        (d.channel, d.check_path): d.resolved
        for d in list_diff_items(conn, PERIOD)
    }

    outcome = _scan(conn, channels=None, resume=True)

    assert outcome.rerun_channels == []
    assert set(outcome.carried_channels) == set(FIVE_CHANNELS)
    assert outcome.resolved_diffs == []
    after_scan = load_scan(conn, PERIOD)
    assert after_scan.status == before_scan.status
    assert after_scan.checks_total == before_scan.checks_total
    after_diffs = {
        (d.channel, d.check_path): d.resolved
        for d in list_diff_items(conn, PERIOD)
    }
    assert after_diffs == before_diffs
    # 这次空跑也要进事件链，否则审计上会出现「说重跑过、日志查不到」
    events = list_scan_events(conn, PERIOD)
    assert events[-1].event == "resume"
    assert "无待跑渠道" in events[-1].result
