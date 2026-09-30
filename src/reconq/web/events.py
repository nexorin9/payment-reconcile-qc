"""面板事件总线（Task 47 · SSE 的那一半）。

面板要实时，靠的是**服务端推**（SSE），不是浏览器轮询 —— 轮询在院内的
笔记本上会把六路渠道的台账反复拉一遍，而那点数据量本来可以一条事件就说
完。轮询只作为 SSE 断线时的兜底（浏览器端 ``panel.js`` 里那 8 秒一次的
``refreshAll()``）。

三类事件，四种语义
------------------
面板上会动的东西只有四类，每类一个 ``kind``：

============ ============================================ ==================
kind         什么时候发                                     面板反应
============ ============================================ ==================
``progress`` 扫描推进（账期状态、已扫渠道数、门禁色变化）   顶栏进度条 + 顶栏徽标
``channel``  某一路渠道的取数/口径结果变了                  左栏那张卡翻转
``diff``     差异台账新增或消解                            中栏追加/移除行
``status``   某条处置单的会签状态变了                       右栏结论流追加
============ ============================================ ==================

**事件只说「变了什么」，不带「所以你该点什么」。** 按钮的禁用态一律由
前端拿当前 ``status`` 现算（:func:`reconq.ledger.models.allowed_targets`
与 :func:`~reconq.ledger.models.required_role_for` 是唯一真源，服务端
也用同一份）。把「该点什么」写进事件流，等于让服务端替人做判断 ——
而这正是 Task 43 明确拒绝的那件事。

为什么轮询数据库而不是挂扫描钩子
--------------------------------
``reconq scan`` 与 ``reconq serve`` 是**两个进程**：CLI 是源用户唯一必须
保住的东西（也是排障第二条腿），它不会为了面板去 import Web 层；反过来说，
服务也不该假设扫描在自己进程内跑。因此 :class:`DbWatcher` 是一条
**只读轮询线**：每隔 :data:`DEFAULT_POLL_SECONDS` 秒比对一次账单库的
快照，变了才发事件。这样 ``scan`` 单独跑、``serve`` 单独跑、两者并行跑，
面板表现一致 —— 代价是最长一个轮询周期的延迟，院内场景完全够。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "DEFAULT_POLL_SECONDS",
    "EVENT_KINDS",
    "DbWatcher",
    "EventBus",
    "PanelEvent",
    "bus_payload",
]

logger = logging.getLogger("reconq.web.events")

#: 账单库轮询间隔（秒）。定 1.0 而不是更短：面板上「扫描进度条推进」
#: 肉眼分辨不出 0.2s 与 1.0s 的差别，而 1.0s 把 SQLite 的读压力降到
#: 可忽略 —— 面板是给人看账的，不是给人看灯的。
DEFAULT_POLL_SECONDS = 1.0

#: 四类事件的真源。前端按这个集合做未知事件的容错（不认识的 kind 记日志
#: 并忽略，而不是把整条流断掉 —— 一次前端升级不该让正在开着页面的
#: 财务科看到空白面板）。
EVENT_KINDS: frozenset[str] = frozenset({"progress", "channel", "diff", "status"})

#: 单个订阅者的队列长度。满了丢**最旧**的：面板要的是「现在什么状态」，
#: 一条三秒前的进度条没有回看价值，而把慢订阅者**断开**会让人以为服务挂了。
QUEUE_MAXSIZE = 64


@dataclass(frozen=True)
class PanelEvent:
    """一条面板事件。

    ``kind`` 决定前端刷哪一块；``payload`` 里的字段名与
    :mod:`reconq.web.state` 的 JSON 端点**同名** —— 前端收到
    ``channel`` 事件后可以直接把 ``payload`` 当成
    ``/api/period/{p}/channels`` 里的那张卡就地替换，不必再发一次请求。
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    stamp: str = ""

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            # 未知 kind 走 ``unknown`` 而不是抛错：加一类事件不该让
            # 已经在跑的面板进程崩掉。
            object.__setattr__(self, "kind", "unknown")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "payload": self.payload, "stamp": self.stamp}


def bus_payload(event: PanelEvent) -> str:
    """渲染成一条 SSE 帧（``event:`` + ``data:`` + 空行）。

    ``data`` 用 ``ensure_ascii=False``：面板要显示中文渠道名与中文成因，
    转义成 ``\\uXXXX`` 只会让排障时 ``curl`` 出来的日志没法读。
    """
    body = json.dumps(event.to_dict(), ensure_ascii=False)
    return f"event: {event.kind}\ndata: {body}\n\n"


class EventBus:
    """进程内发布订阅。

    **刻意不引 Redis / 消息中间件**：本服务按 spec 部署在院内单机、不出网，
    多一个中间件就多一个信息科要运维的东西，而面板的订阅者就是本进程里
    那几个 SSE 连接。跨进程那一段由 :class:`DbWatcher` 读账单库解决。
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[PanelEvent]] = []
        self._lock = threading.Lock()
        self._seq = 0

    def subscribe(self) -> asyncio.Queue[PanelEvent]:
        q: asyncio.Queue[PanelEvent] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[PanelEvent]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def next_seq(self) -> int:
        """事件序号。**跨线程自增**（watcher 线程 + 请求处理线程共用）。"""
        with self._lock:
            self._seq += 1
            return self._seq

    def publish(self, event: PanelEvent) -> None:
        """广播。队列满则丢最旧的一条，**不阻塞发布方**。

        阻塞在这里等于让一个网速慢的浏览器把扫描线程卡住 —— 面板是
        看板，不是业务链路，它慢不该让对账停下来。
        """
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)


class DbWatcher:
    """只读轮询账单库，把变化翻译成面板事件。

    比对的是**快照**而不是逐行 diff：快照是「每路渠道的状态/笔数/合计/
    失败数」「差异条数与合计差额」「每条处置单的状态与版本」三组小字典。
    逐行比对在这个数据量级上不划算，而快照比对漏掉的那一类变化
    （同一状态下的内容改动）本来就**不该**推到面板上让人看见 ——
    面板显示的是「还剩几条没处理」，不是「上一秒的中间值」。
    """

    def __init__(
        self,
        db_path: str,
        bus: EventBus,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        periods_provider: Optional[Any] = None,
    ) -> None:
        self._db_path = db_path
        self._bus = bus
        self._poll = max(0.05, float(poll_seconds))
        #: 由 app 层注入：给定连接返回「当前要盯的账期列表」。
        self._periods_provider = periods_provider
        self._prev: dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # —— 生命周期 ——

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="reconq-panel-watcher", daemon=True
        )
        self._thread.start()
        logger.info("面板轮询线已启动：每 %.1fs 读一次 %s", self._poll, self._db_path)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # —— 主循环 ——

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self.poll_once()
            except sqlite3.Error as exc:  # pragma: no cover - 库被换掉时
                logger.warning("面板轮询读库失败：%s", exc)
            except Exception:  # noqa: BLE001 - 看门狗不许把服务带崩
                logger.exception("面板轮询出现未预期异常，本轮跳过")

    def poll_once(self) -> list[PanelEvent]:
        """跑一次比对并发出变化事件。**返回本轮发出的事件**（测试用）。"""
        snapshot = self.snapshot()
        events = self._diff(self._prev, snapshot)
        self._prev = snapshot
        for event in events:
            self._bus.publish(event)
        return events

    # —— 快照 ——

    def snapshot(self) -> dict[str, Any]:
        from ..store.schema import connect
        from .state import channel_cards, period_overview

        conn = connect(self._db_path)
        try:
            from ..engine.period import init_scan_schema
            from ..ledger.models import init_ledger_schema
            from ..store.schema import init_schema

            # 只在**表确实不存在**时建 —— 服务不拥有库，它只是读它；
            # 但空库上直接开面板不该炸，故各 init 都是幂等的。
            init_schema(conn)
            init_scan_schema(conn)
            init_ledger_schema(conn)

            periods = (
                list(self._periods_provider())
                if self._periods_provider is not None
                else list(period_overview(conn)["periods"])
            )
            snap: dict[str, Any] = {"periods": periods, "scan": {}, "channel": {}, "diff": {}, "status": {}}
            for period in periods:
                snap["scan"][period] = period_overview(conn, period)["overview"]
                snap["channel"][period] = channel_cards(conn, period)
                snap["diff"][period] = _diff_signature(conn, period)
                snap["status"][period] = _status_signature(conn, period)
            return snap
        finally:
            conn.close()

    # —— 比对 ——

    def _diff(self, old: dict[str, Any], new: dict[str, Any]) -> list[PanelEvent]:
        events: list[PanelEvent] = []
        if not old:
            # 首轮：把「现在什么样」整份推一次，页面不必先等一次 REST 才有内容。
            for period in new.get("periods", []):
                events.append(self._event("progress", period, new["scan"].get(period, {})))
            return events

        for period in new.get("periods", []):
            prev_scan = old.get("scan", {}).get(period)
            if prev_scan != new["scan"].get(period):
                events.append(self._event("progress", period, new["scan"][period]))

            prev_channels = {c["channel"]: c for c in old.get("channel", {}).get(period, [])}
            for card in new["channel"][period]:
                if prev_channels.get(card["channel"]) != card:
                    events.append(self._event("channel", period, card))

            prev_diff = old.get("diff", {}).get(period, {})
            cur_diff = new["diff"][period]
            if prev_diff.get("count") != cur_diff.get("count") or prev_diff.get(
                "open_count"
            ) != cur_diff.get("open_count"):
                events.append(self._event("diff", period, cur_diff))

            prev_status = old.get("status", {}).get(period, {})
            for key, sig in new["status"][period].items():
                if prev_status.get(key) != sig:
                    events.append(self._event("status", period, {"key": key, **sig}))
        return events

    def _event(self, kind: str, period: str, payload: dict[str, Any]) -> PanelEvent:
        body = {"period": period, "seq": self._bus.next_seq(), **payload}
        return PanelEvent(kind=kind, payload=body, stamp=time.strftime("%Y-%m-%dT%H:%M:%S"))


def _diff_signature(conn: sqlite3.Connection, period: str) -> dict[str, Any]:
    """差异台账的签名：条数 / 未确认条数 / 合计差额 / 最新记录时间。

    合计差额用 ``Decimal`` 累加成**字符串**再入签名：这里比的是
    「面板上那行合计有没有变」，用浮点会让 0.1+0.2 != 0.3 变成一条
    永远推不完的差异事件。
    """
    from decimal import Decimal, InvalidOperation

    from ..engine.period import DIFF_TABLE, DISPOSITION_PENDING, _require_schema

    try:
        _require_schema(conn, DIFF_TABLE)
    except Exception:  # noqa: BLE001 - 空库上先当零
        return {"count": 0, "open_count": 0, "total_amount": "0.00", "latest": ""}
    row = conn.execute(
        f"SELECT COUNT(*) AS n, "
        f"SUM(CASE WHEN disposition=? AND resolved=0 THEN 1 ELSE 0 END) AS open_n, "
        f"COALESCE(MAX(recorded_at), '') AS latest "
        f"FROM {DIFF_TABLE} WHERE period=?",
        (DISPOSITION_PENDING, period),
    ).fetchone()
    total = Decimal("0")
    for r in conn.execute(
        f"SELECT diff_amount FROM {DIFF_TABLE} WHERE period=? AND diff_amount IS NOT NULL",
        (period,),
    ):
        try:
            total += Decimal(str(r["diff_amount"]))
        except (InvalidOperation, TypeError):
            # 台账里出现非数值差额时**不猜**：跳过这一行并让签名里带上
            # 「有脏数据」标记，面板据此显示「合计不完整」而不是给一个
            # 看起来很精确的错数。
            return {
                "count": int(row["n"] or 0),
                "open_count": int(row["open_n"] or 0),
                "total_amount": "",
                "total_known": False,
                "latest": str(row["latest"] or ""),
            }
    return {
        "count": int(row["n"] or 0),
        "open_count": int(row["open_n"] or 0),
        "total_amount": f"{total:.2f}",
        "total_known": True,
        "latest": str(row["latest"] or ""),
    }


def _status_signature(conn: sqlite3.Connection, period: str) -> dict[str, dict[str, Any]]:
    """处置单状态签名：``键 → {status, version}``。

    带 ``version`` 是为了把**冲突**也变成可见事件：两个人同时点确认，
    第二个人的写入被乐观锁挡下，他那一侧必须收到一条 ``status`` 事件
    才看得到「已经被人改到第 N 版」，而不是停在旧画面上以为没点上。
    """
    from ..ledger.flow import list_discrepancies

    try:
        items = list_discrepancies(conn, period=period)
    except Exception:  # noqa: BLE001 - 还没开过单时表可能不存在
        return {}
    return {
        item.key: {"status": item.status, "version": item.version, "is_open": item.is_open}
        for item in items
    }
