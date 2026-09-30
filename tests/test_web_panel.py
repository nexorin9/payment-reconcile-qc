"""常驻服务 + SSE 实时面板的冒烟断言（Task 47）。

范围刻意收在**本任务自己声明的验收点上** —— 面板能开、三栏各有数据、
SSE 连得上并推得出事件、两个写入口只认人。**API 级交互测试（SSE 事件
序列 / 状态机越权 / 并发冲突）留给 Task 49**，这里不抢它的活，也不为了
显得完整而把两件事混进一个文件。

夹具用**真扫一遍**出来的库（:func:`_scanned_db`）而不是手工 INSERT：
面板的每个字段都声称与 CLI 同源，那么它的测试数据就该也来自 CLI ——
手写的行一旦与 ``reconq scan`` 的实际落库形状漂了，测试会绿而面板坏。
"""

from __future__ import annotations

import json
import os
import urllib.parse

import pytest

from reconq.engine import period as period_engine
from reconq.ledger.flow import open_discrepancy
from reconq.sources.registry import CHANNELS
from reconq.store.schema import connect
from reconq.web import state
from reconq.web.app import create_app
from reconq.web.events import DbWatcher, EventBus, PanelEvent

#: 项目根（含 ``contracts/`` 与 ``inbox/`` 的那一层）。``reconq scan`` 按
#: **当前工作目录**找契约目录，所以测试要 chdir 过去。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PERIOD = "2026-W36"
CHECK_PATH = "rows.refund_sum.zero"


@pytest.fixture()
def scanned_db(tmp_path):
    """跑一次 ``reconq scan`` 造出真库。

    账单文件**直接用仓库里的脱敏样例**而不是在测试里手搓一份：微信账单
    的表头是中文且要走一套「表头在含『交易时间』的那一行」的定位逻辑
    （:mod:`reconq.sources.wechat_bill`），手写的简化表头会让取数直接
    报 ``BILL_COLUMN_MISSING`` —— 那测的就不是面板了，是取数器。
    """
    import shutil

    from reconq.cli import main

    db = str(tmp_path / "reconq.db")
    inbox = tmp_path / "inbox"
    shutil.copytree(os.path.join(_PROJECT_ROOT, "inbox", PERIOD), inbox / PERIOD)

    # ``reconq scan`` 按 cwd 找 ``contracts/``。切到项目根再跑，
    # 用 monkeypatch 而不是 os.chdir —— chdir 不复原会污染后续用例。
    cwd = os.getcwd()
    os.chdir(_PROJECT_ROOT)
    try:
        code = main(
            [
                # ``--no-log`` 是**全局**参数（控制结构化日志写不写盘），
                # 必须排在子命令前面；放在 scan 后面 argparse 会直接拒。
                "--no-log",
                "scan", "--period", PERIOD, "--db", db, "--source", str(inbox),
            ]
        )
    finally:
        os.chdir(cwd)
    # 退出码非 0 是正常的：这期账本来就有差异，而差异不是错误。
    assert code in (0, 1), f"scan 意外失败，退出码 {code}"
    return db


@pytest.fixture()
def client(scanned_db):
    from fastapi.testclient import TestClient

    with TestClient(create_app(scanned_db, start_watcher=False)) as c:
        yield c


def _open_ledger(db: str, check_path: str = CHECK_PATH, amount="86.00"):
    conn = connect(db)
    try:
        return open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=check_path,
            diff_amount=amount, evidence_rows=("wechat_tradebill_2026-W36.csv:2",),
        )
    finally:
        conn.close()


def _url(key: str, action: str) -> str:
    return f"/api/discrepancy/{urllib.parse.quote(key, safe='')}/{action}"


# —— 页面与三栏 ——


def test_index_renders_three_columns(client):
    body = client.get("/").text
    assert client.get("/").status_code == 200
    for col in ("col-channels", "col-diff", "col-detail"):
        assert col in body, f"三栏缺 {col}"
    # 静态资源挂上了：面板样式与脚本都取得到，否则页面是一张白纸
    assert client.get("/static/panel.css").status_code == 200
    assert client.get("/static/panel.js").status_code == 200


def test_periods_endpoint_lists_scanned_period(client):
    data = client.get("/api/periods").json()
    assert data["default"] == PERIOD
    assert PERIOD in data["periods"]


def test_channels_endpoint_returns_six_cards(client):
    cards = client.get(f"/api/period/{PERIOD}/channels").json()["channels"]
    assert [c["channel"] for c in cards] == list(CHANNELS), "六路顺序即面板展示顺序，不能少一张"
    wechat = next(c for c in cards if c["channel"] == "wechat")
    assert wechat["row_count"] == 19, "样例账期微信 19 行；数字对不上说明面板没在读真库"
    # 金额是定点字符串，不是 float —— 面板上「合计」不许经过二进制浮点
    assert wechat["total_amount"] == "5394.69"
    assert isinstance(wechat["total_amount"], str)


def test_missing_channels_are_gray_cards_not_absent(client):
    """少一路渠道要给**灰卡 + 错误码**，不是不给卡。

    不给卡看起来更清爽，但那样「六路里少了哪一路」就得靠人记得住六个
    名字 —— 而这正是最常被漏掉的一类。灰卡与绿卡必须一眼可分。
    """
    cards = client.get(f"/api/period/{PERIOD}/channels").json()["channels"]
    gray = [c for c in cards if c["badge"] == state.BADGE_GRAY]
    assert len(gray) == 5, "只有微信取到账单，其余五路应是灰卡"
    his = next(c for c in gray if c["channel"] == "his")
    assert his["error_code"] == "CREDENTIAL_MISSING", "灰卡要写清是哪个环境变量没设"
    # 绿色必须真的只在「这一路全过」时出现
    assert all(c["badge"] != state.BADGE_GREEN for c in gray)


def test_discrepancies_endpoint_reports_evidence_and_amount(client):
    data = client.get(f"/api/period/{PERIOD}/discrepancies").json()
    assert data["rows"], "扫出 2 条差异，中栏不该是空的"
    assert data["total_known"] is True
    row = data["rows"][0]
    # 证据只给「文件名:行号」，**不落整行原文** —— 交办单要发给渠道方
    assert row["first_row_no"] >= 1
    assert "交易时间" not in json.dumps(row, ensure_ascii=False), "面板不该回吐账单原文"


def test_discrepancy_total_unknown_when_amount_dirty(client, scanned_db):
    """台账里有非数值差额时，合计显示「不完整」而不是给一个错数。"""
    conn = connect(scanned_db)
    try:
        conn.execute(
            f"UPDATE {period_engine.DIFF_TABLE} SET diff_amount='不是数字' "
            "WHERE period=? AND check_path=?",
            (PERIOD, CHECK_PATH),
        )
    finally:
        conn.close()
    data = client.get(f"/api/period/{PERIOD}/discrepancies").json()
    assert data["total_known"] is False
    assert data["total_amount"] == ""


# —— 写入口：只认人，非法状态给 4xx ——


def test_model_operator_cannot_advance_state(client, scanned_db):
    """人工边界：模型 / 规则 / 系统都推不动状态，且**不是 500**。"""
    item = _open_ledger(scanned_db)
    for actor in ("model", "rule", "系统", "scheduler"):
        r = client.post(
            _url(item.key, "decide"),
            json={"decision": "confirm", "operator": actor,
                  "comment": "看着像延迟入账", "expected_version": item.version},
        )
        assert r.status_code == 422, f"{actor} 竟然拿到了 {r.status_code}"
        assert r.json()["code"] == "LEDGER_OPERATOR_INVALID"


def test_illegal_transition_returns_4xx_with_code(client, scanned_db):
    """``待归因 → 已确认`` 跳关被拒。状态机是唯一裁决者，Web 层不兜住它。"""
    item = _open_ledger(scanned_db)
    r = client.post(
        _url(item.key, "decide"),
        json={"decision": "reject", "operator": "E-01", "expected_version": item.version},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "LEDGER_TRANSITION_DENIED"


def test_missing_version_rejected(client, scanned_db):
    """不带版本号的确认会无声盖掉别人刚写的结论，故直接拒。"""
    item = _open_ledger(scanned_db)
    r = client.post(
        _url(item.key, "decide"),
        json={"decision": "confirm", "operator": "E-01", "comment": "渠道延迟入账"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "LEDGER_VERSION_REQUIRED"


def test_submit_without_conclusion_rejected(client, scanned_db):
    """没有结论的确认在结论单上是无源的。"""
    item = _open_ledger(scanned_db)
    r = client.post(
        _url(item.key, "decide"),
        json={"decision": "confirm", "operator": "E-01", "comment": "",
              "expected_version": item.version},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "LEDGER_CONCLUSION_REQUIRED"


def test_full_signoff_path_then_confirm_flow(client, scanned_db):
    """两级会签走通；终态的按钮只剩「撤回」，已确认的不再给「驳回」。"""
    item = _open_ledger(scanned_db)
    r = client.post(_url(item.key, "decide"), json={
        "decision": "confirm", "operator": "E-01", "role": "handler",
        "comment": "渠道延迟入账，次日已补", "expected_version": item.version})
    assert r.status_code == 200 and r.json()["after"] == "待确认"

    r = client.post(_url(item.key, "decide"), json={
        "decision": "confirm", "operator": "E-02", "role": "reviewer",
        "comment": "复核无误", "expected_version": 1})
    assert r.status_code == 200 and r.json()["after"] == "已确认"

    rows = {row["key"]: row for row in
            client.get(f"/api/period/{PERIOD}/discrepancies").json()["rows"]}
    settled = rows[item.key]
    assert settled["conclusion"] == "渠道延迟入账，次日已补"
    assert [a["action"] for a in settled["actions"]] == ["withdraw"], (
        "已确认的单子上再给「通过/驳回」按钮，就是让人点一个必报错的东西"
    )
    flow = client.get(f"/api/period/{PERIOD}/disposition").json()["settled"]
    assert [f["status"] for f in flow] == ["已确认"]


def test_unknown_discrepancy_returns_404(client):
    r = client.post(
        _url(f"{PERIOD}|wechat|不存在的口径", "decide"),
        json={"decision": "confirm", "operator": "E-01", "comment": "x",
              "expected_version": 0},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "LEDGER_DISCREPANCY_NOT_FOUND"


# —— SSE ——


def test_sse_first_frame_is_progress():
    """连上就得先有一帧 progress：否则页面要空等到有人点扫描。

    **直接跑生成器，不用 ``TestClient.stream``**：SSE 是设计上的无限流，
    而 ``TestClient`` 会把响应读完才交回控制权 —— 在无限流上它永远不
    交回，症状是「这条用例把整个测试进程挂住」，而原因是测试在等一个
    按定义不会结束的东西。真实 HTTP 上的握手由 Task 47 的 curl 验收
    覆盖（``GET /api/events`` 200 + ``content-type: text/event-stream``）。
    """
    import asyncio

    from reconq.web.app import _sse_frames

    async def take_one():
        gen = _sse_frames(bus=EventBus(), db_path="/tmp/x.db")
        return await gen.__anext__()

    frame = asyncio.run(take_one())
    assert frame.startswith("event: progress\ndata: ")
    body = json.loads(frame.split("data: ", 1)[1].split("\n", 1)[0])
    assert body["payload"]["hello"] is True
    assert frame.endswith("\n\n"), "SSE 帧必须以空行收尾，否则浏览器不认这一帧"


def test_sse_yields_published_event_after_hello():
    """订阅者先收到 hello，再收到别人推来的事件。"""
    import asyncio

    from reconq.web.app import _sse_frames

    async def scenario():
        bus = EventBus()
        gen = _sse_frames(bus=bus, db_path="/tmp/x.db")
        hello = await gen.__anext__()
        bus.publish(PanelEvent(kind="status", payload={"period": PERIOD, "key": "k"}))
        nxt = await gen.__anext__()
        await gen.aclose()
        return hello, nxt

    hello, nxt = asyncio.run(scenario())
    assert "hello" in hello
    assert nxt.startswith("event: status")
    body = json.loads(nxt.split("data: ", 1)[1].split("\n", 1)[0])
    assert body["payload"]["key"] == "k"


def test_bus_publishes_and_counts_subscribers():
    bus = EventBus()
    assert bus.subscriber_count == 0
    q = bus.subscribe()
    bus.publish(PanelEvent(kind="progress", payload={"period": PERIOD}))
    assert bus.subscriber_count == 1
    bus.unsubscribe(q)
    assert bus.subscriber_count == 0


def test_unknown_event_kind_degrades_instead_of_crashing():
    """加一类事件不该让已经开着面板的财务科看到空白页。"""
    assert PanelEvent(kind="将来才有的一类").kind == "unknown"


def test_watcher_emits_events_when_db_changes(scanned_db):
    """``scan`` 与 ``serve`` 是两个进程：轮询线要能把**别的进程**的写入推出去。"""
    bus = EventBus()
    watcher = DbWatcher(scanned_db, bus, poll_seconds=0.05)
    watcher.poll_once()  # 首轮只铺底

    conn = connect(scanned_db)
    try:
        open_discrepancy(conn, period=PERIOD, channel="wechat",
                         check_path=CHECK_PATH, diff_amount="86.00")
    finally:
        conn.close()

    kinds = [e.kind for e in watcher.poll_once()]
    assert "status" in kinds, f"开单后应有 status 事件，实际 {kinds}"
    assert bus.subscriber_count == 0, "没订阅者时轮询线也不该报错"


def test_watcher_snapshot_is_stable_when_nothing_changes(scanned_db):
    """没有变化就不该推事件 —— 否则每秒钟刷一次页面，没人受得了。"""
    watcher = DbWatcher(scanned_db, EventBus(), poll_seconds=0.05)
    watcher.poll_once()
    assert watcher.poll_once() == []
