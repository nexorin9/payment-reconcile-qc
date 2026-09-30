"""SSE 事件流（Task 49 · 四类事件那条）。

要验的是**序列**：连上先拿到一帧握手，然后按 ``progress → channel →
diff → status`` 的顺序收到四类事件，事件类型齐全、载荷里的字段名与对应
JSON 端点**同名**（前端拿到 ``channel`` 事件可以直接把 payload 当成那张卡
就地替换，不必再发一次请求）。

为什么不直接 ``TestClient.stream``
---------------------------------
SSE 是设计上的**无限流**，而 ``TestClient`` 会把响应读完才交回控制权 ——
在无限流上它永远不交回，症状是「这条用例把整个测试进程挂住」，而原因
是测试在等一个按定义不会结束的东西。因此这里直接跑生成器
(:func:`reconq.web.app._sse_frames`) 并逐帧 ``__anext__()``：拿到四类事件
就 ``aclose()``。真实 HTTP 上的握手（200 + ``text/event-stream``）由本文件
末尾那条真的走一次 HTTP 的用例覆盖。
"""

from __future__ import annotations

import asyncio
import json

import pytest

#: 四类事件的面板语义名 → 服务端实际的 ``kind``。**两者不是一一同名**：
#: 事件 kind 定的是「刷哪一块区域」，而这里是「业务上发生了什么」。
#: 映射写在测试里而不是产品里，是为了让「面板上那四句话」与「四类事件」
#: 的对应关系有一处可核的书面依据 —— 它变了对不上时，红的是这里。
EXPECTED_SEQUENCE = [
    ("scan_started", "progress"),
    ("channel_status", "channel"),
    ("discrepancy_created", "diff"),
    ("decision", "status"),
]


def _parse(frame: str) -> tuple[str, dict]:
    """一帧 SSE → ``(event 名, data 载荷)``。"""
    assert frame.endswith("\n\n"), f"SSE 帧必须以空行收尾，否则浏览器不认这一帧：{frame!r}"
    lines = frame.strip("\n").split("\n")
    event = next(line[len("event: "):] for line in lines if line.startswith("event: "))
    raw = next(line[len("data: "):] for line in lines if line.startswith("data: "))
    return event, json.loads(raw)


def test_handshake_frame_arrives_before_anything_else(scanned_db):
    """连上就先有一帧 progress 握手。

    否则页面要空等到有人点扫描 —— 而这期账可能早就扫完了。
    """
    from reconq.web.app import _sse_frames
    from reconq.web.events import EventBus

    async def take_one():
        gen = _sse_frames(bus=EventBus(), db_path=scanned_db)
        frame = await gen.__anext__()
        await gen.aclose()
        return frame

    frame = asyncio.run(take_one())
    event, body = _parse(frame)
    assert event == "progress"
    assert body["payload"]["hello"] is True
    assert body["payload"]["db_path"] == scanned_db


def test_four_event_kinds_arrive_in_order_over_one_connection(scanned_db):
    """一条连接上按顺序收到四类事件，且载荷字段与 JSON 端点同名。

    顺序有讲究：先说「开始扫了」，再说「哪一路扫完了」，再说「冒出来一条
    差异」，最后说「有人处理了哪一条」—— 反过来的话面板会先显示一条还没
    建立的差异，再解释它是怎么来的。
    """
    from reconq.ledger.flow import open_discrepancy
    from reconq.store.schema import connect
    from reconq.web.app import _sse_frames
    from reconq.web.events import DbWatcher, EventBus, bus_payload

    async def scenario():
        bus = EventBus()
        watcher = DbWatcher(scanned_db, bus, poll_seconds=0.05)
        watcher.poll_once()  # 首轮只铺底，不发事件

        gen = _sse_frames(bus=bus, db_path=scanned_db)
        seen = [await gen.__anext__()]  # 握手帧

        # 扫描在跑：轮询线看到 scan 行变了 → progress
        conn = connect(scanned_db)
        try:
            from reconq.engine import period as period_engine

            period_engine.start_scan(conn, "2026-W36", run_id="run-sse", channels_total=6)
        finally:
            conn.close()
        seen.extend(bus_payload(e) for e in watcher.poll_once())

        # 某一路的状态变了 → channel
        conn = connect(scanned_db)
        try:
            from reconq.engine.period import ChannelScan, upsert_channel_results

            upsert_channel_results(
                conn, "2026-W36",
                [ChannelScan(channel="alipay", status="checked", row_count=7,
                             total_amount="120.50", checks_total=4, checks_failed=0,
                             inserted=7, skipped_duplicates=0, error_code="",
                             cause_code="", detail="")]
            )
        finally:
            conn.close()
        seen.extend(bus_payload(e) for e in watcher.poll_once())

        # 新出现一条差异 → diff（``diff`` 事件盯的是**差异台账**的签名，
        # 不是处置单：开单动的是 ledger 表，那条路发的是 status）
        conn = connect(scanned_db)
        try:
            from reconq.engine.period import DiffItem, upsert_diff_items

            upsert_diff_items(conn, [DiffItem(
                period="2026-W36", channel="wechat",
                check_path="rows.order_no.unique", check_name="单号唯一",
                message="同一单号出现两笔", diff_amount="128.00",
                source_file="wechat_tradebill_2026-W36.csv", first_row_no=7,
            )])
        finally:
            conn.close()
        seen.extend(bus_payload(e) for e in watcher.poll_once())

        # 有人处理了一条 → status（处置单状态签名变了）
        conn = connect(scanned_db)
        try:
            open_discrepancy(conn, period="2026-W36", channel="wechat",
                             check_path="rows.refund_sum.zero", diff_amount="86.00")
        finally:
            conn.close()
        seen.extend(bus_payload(e) for e in watcher.poll_once())

        await gen.aclose()
        return seen, bus

    seen, bus = asyncio.run(scenario())
    kinds = [_parse(f)[0] for f in seen]
    assert kinds[0] == "progress", "第一帧必须是握手"
    for label, kind in EXPECTED_SEQUENCE:
        assert kind in kinds, f"没收到 {kind} 事件（{label}）；实际收到 {kinds}"

    # 四类各自的**首次出现**顺序必须与表一致：先说「开始扫了」，再说
    # 「哪一路扫完了」，再说「冒出来一条差异」，最后说「有人处理了哪一条」。
    # 反过来的话面板会先显示一条还没建立的差异，再解释它是怎么来的。
    first_seen: list[str] = []
    for k in kinds:
        if k not in first_seen:
            first_seen.append(k)
    expected_order = [k for _, k in EXPECTED_SEQUENCE]
    assert first_seen == expected_order, f"事件首次出现顺序 {first_seen}，应为 {expected_order}"
    assert bus.subscriber_count == 0, "生成器关掉后订阅者必须被摘掉，否则慢客户端会一直占着队列"


def test_event_kinds_are_exactly_the_four_registered_ones():
    """事件 kind 集合就是那四个，前端按它做容错。

    集合外的 kind 会被降级成 ``unknown`` 而不是抛错 —— 加一类事件不该
    让已经开着面板的财务科看到空白页。
    """
    from reconq.web.events import EVENT_KINDS, PanelEvent

    assert {k for _, k in EXPECTED_SEQUENCE} <= EVENT_KINDS
    assert len(EVENT_KINDS) == 4, f"面板只声明四类区域，事件集合却是 {sorted(EVENT_KINDS)}"
    assert PanelEvent(kind="将来才有的一类").kind == "unknown"


def test_channel_event_payload_matches_channels_endpoint_shape(scanned_db):
    """``channel`` 事件的载荷字段与端点那张卡**同名**。

    同名才能让前端「就地替换那张卡」；不同名就得再发一次请求，而在扫描
    高峰期那一次请求会落在「这张卡还没写完」的瞬间。
    """
    from reconq.engine.period import ChannelScan, upsert_channel_results
    from reconq.store.schema import connect
    from reconq.web.events import DbWatcher, EventBus

    # 轮询线**先铺底再比对**：新建一个 watcher 直接 poll 只会拿到「首轮
    # 整份推一次」，那样它跟「什么都没变」在事件列表上长得一样。
    bus = EventBus()
    watcher = DbWatcher(scanned_db, bus, poll_seconds=0.05)
    watcher.poll_once()

    conn = connect(scanned_db)
    try:
        upsert_channel_results(
            conn, "2026-W36",
            [ChannelScan(channel="alipay", status="checked", row_count=7,
                         total_amount="120.50", checks_total=4, checks_failed=0,
                         inserted=7, skipped_duplicates=0, error_code="",
                         cause_code="", detail="")]
        )
    finally:
        conn.close()

    events = watcher.poll_once()
    channel_events = [e for e in events if e.kind == "channel"]
    assert channel_events, f"改了 alipay 主态却没有 channel 事件，实际 {[e.kind for e in events]}"

    from fastapi.testclient import TestClient

    from reconq.web.app import create_app

    with TestClient(create_app(scanned_db, start_watcher=False)) as c:
        card = next(x for x in c.get("/api/period/2026-W36/channels").json()["channels"]
                    if x["channel"] == "alipay")

    # 事件载荷的字段名要与端点那张卡**逐字相同**，前端才能就地替换。
    for field in ("channel", "badge", "row_count", "total_amount", "checks_failed"):
        assert field in card, f"端点那张卡缺字段 {field}，比对无从做起"
        assert field in channel_events[0].payload, (
            f"channel 事件缺字段 {field}；前端收到后还得再发一次请求，"
            "而那次请求会落在「这张卡还没写完」的瞬间"
        )
    # 载荷里的数字要与端点读出来的一致 —— 同名不等于同源
    assert channel_events[0].payload["row_count"] == card["row_count"]
    assert channel_events[0].payload["total_amount"] == card["total_amount"]


def test_status_event_is_published_after_the_write_commits(client, opened_db):
    """``status`` 事件**在提交之后**发，且带回操作人、时刻与剩余动作。

    先发会让页面显示出一个还没落库的状态；不带走操作人则要回日志里查
    「谁在什么时候把它推到了哪一步」。
    """
    import urllib.parse

    app = client.app
    bus = app.state.bus
    queue = bus.subscribe()
    key = urllib.parse.quote("2026-W36|wechat|rows.refund_sum.zero", safe="")
    r = client.post(
        f"/api/discrepancy/{key}/decide",
        json={"decision": "confirm", "operator": "E-01", "role": "handler",
              "comment": "渠道延迟入账", "expected_version": 0},
    )
    assert r.status_code == 200, r.text
    event = queue.get_nowait()
    assert event.kind == "status"
    assert event.payload["key"] == "2026-W36|wechat|rows.refund_sum.zero"
    # 载荷字段名是 ``status``（不是响应体里的 ``after``）：事件载荷的字段
    # 名与 JSON 端点同名，前端拿到就能就地替换那一行
    assert event.payload["status"] == "待确认"
    assert event.payload["status_zh"], "状态要带中文，前端不拿 key 去查字典"
    # 操作人与时刻要**跟着事件走**，不能只留在响应体里：响应体只回给
    # 点按钮的那个人，而这条事件是推给所有开着面板的人的
    assert event.payload["operator"] == "E-01"
    assert event.payload["at"], "签字时刻要跟着事件一起走"
    assert event.payload["seq"] >= 1
    bus.unsubscribe(queue)


def test_failed_decide_publishes_nothing(client, opened_db):
    """被拒的点击**不发 status 事件**。

    发了的话页面会刷新出一个根本没发生过的状态变更，而操作人看到的却是
    一条错误提示 —— 两者同时出现时，页面更新总是更抢眼。
    """
    import urllib.parse

    queue = client.app.state.bus.subscribe()
    key = urllib.parse.quote("2026-W36|wechat|rows.refund_sum.zero", safe="")
    r = client.post(
        f"/api/discrepancy/{key}/decide",
        json={"decision": "reject", "operator": "E-01", "expected_version": 0},
    )
    assert r.status_code == 409, r.text
    assert queue.empty(), "被拒的迁移也推事件，页面会显示一个没发生的状态变更"
    client.app.state.bus.unsubscribe(queue)


def test_sse_endpoint_answers_with_event_stream_headers_over_real_http(scanned_db):
    """真起一个 uvicorn、真发一次 HTTP：握手帧拿到且响应头对。

    为什么要真 socket 而不是 ``TestClient.stream``：SSE 是设计上的**无限
    流**，而 ``TestClient`` 内部用 portal 把响应读完才交回控制权 —— 在
    无限流上它永远不交回，症状是「这条用例把整个测试进程挂住」，而原因
    是测试在等一个按定义不会结束的东西。这里用 :mod:`httpx` 直连真服务、
    :meth:`iter_lines` 只取**两行**（``event:`` 与 ``data:``）就退出
    ``with``，流随连接关闭而释放。
    """
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    from reconq.web.app import create_app

    # 端口由内核分配：写死 8788 会与真跑着的面板服务抢端口，而那台机器
    # 上「��试跑不起来」与「面板占着端口」的症状完全一样。
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = uvicorn.Server(
        uvicorn.Config(create_app(scanned_db, start_watcher=False),
                       host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn 10 秒内没起来"

        with httpx.Client(timeout=10) as hc, hc.stream(
            "GET", f"http://127.0.0.1:{port}/api/events",
            headers={"Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200
            headers = response.headers
            lines = response.iter_lines()
            first = [next(lines), next(lines)]
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert headers["content-type"].startswith("text/event-stream"), (
        f"content-type 是 {headers['content-type']}；浏览器会当普通文档解析，"
        "然后什么都不显示 —— 而界面上看不出「连上了」"
    )
    # 反向代理会缓冲响应体，缓冲住则整条流攒到结束才吐给浏览器 ——
    # 面板上表现为「连接着但什么都不动」
    assert headers.get("x-accel-buffering") == "no"
    assert headers.get("cache-control") == "no-cache"

    assert first[0] == "event: progress", f"第一帧不是握手：{first[0]!r}"
    assert json.loads(first[1][len("data: "):])["payload"]["hello"] is True


def test_keepalive_comment_pads_idle_stream(scanned_db, monkeypatch):
    """空闲时补一条注释帧，撑住反代的 SSE 超时。

    SSE 连着不动超过反代的读超时就会被掐掉，而面板上表现为「实时推送
    断线」—— 每隔几秒自己重连，财务科看不出发生了什么。
    """
    from reconq.web import app as app_module
    from reconq.web.events import EventBus

    monkeypatch.setattr(app_module, "SSE_KEEPALIVE_SECONDS", 0.05)

    async def scenario():
        gen = app_module._sse_frames(bus=EventBus(), db_path=scanned_db)
        await gen.__anext__()  # 握手帧先吃掉
        pad = await gen.__anext__()
        await gen.aclose()
        return pad

    assert asyncio.run(scenario()) == ": keep-alive\n\n"


@pytest.mark.parametrize("kind", ["progress", "channel", "diff", "status"])
def test_each_frame_is_standalone_json(kind):
    """每帧都是独立可解析的 JSON，中文不转义。

    转义成 ``\\uXXXX`` 只会让排障时 ``curl`` 出来的日志没法读 —— 而
    「刚才面板上刷的那条是什么」正是排障时要答的第一个问题。
    """
    from reconq.web.events import PanelEvent, bus_payload

    frame = bus_payload(PanelEvent(kind=kind, payload={"渠道": "微信支付", "备注": "带竖线|的说明"}))
    event, body = _parse(frame)
    assert event == kind
    assert body["payload"]["渠道"] == "微信支付"
    assert "微信支付" in frame, "中文被转义成 \\uXXXX 了"
