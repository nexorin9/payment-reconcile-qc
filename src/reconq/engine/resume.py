"""断点续跑、按渠道 / 按 check 子集重跑、并发锁与重跑事件链（Task 19）。

一次扫描六路两三千行，中途某一路失败或某条口径被改，财务科要的是
**只重跑受影响的那一部分**，不是整期重来。这个模块把「重来」拆成三种
粒度，各自都有可核对的落点：

======================  ==============================================
入口                    回答的问题
======================  ==============================================
:func:`mark_channel_done`  这一路跑完了吗（断点）
:func:`load_checkpoints`    上次跑到哪儿了
:func:`select_scope`        这次要重跑哪几路 / 哪几条口径
:func:`acquire_scan_lock`   同一账期是不是已经在跑了
:func:`append_scan_event`   谁、何时、按什么范围重跑、结果差几条
======================  ==============================================

三条硬纪律
----------
1. **沿用不是产出**。没有本次重跑到的渠道 / 口径，结果行上打
   ``origin='carried'``。面板与结论单据此显示「沿用上次」，不让财务科
   把一周前跑的数当成本周一分钟前跑的数签进结论单。
2. **消失的差异标 ``resolved``，不删**。口径这次通过了，旧差异行留��，
   只打消解标记 + 消解批次。删掉的话，结论单上「上周报过 3 条、本周 0 条」
   就变成了「上周根本没报过」——归因与先例库（Task 41）全靠这条历史。
3. **同一账期同时只跑一个**。:func:`acquire_scan_lock` 在同一 SQLite 库上
   以「账期为主键」抢锁；抢不到且对方心跳未过期，抛 ``SCAN_IN_PROGRESS``
   而不是并行开跑（并行跑的结果就是两批 ``upsert`` 互相覆盖，合计在中间
   态上闪烁，比报错难查得多）。对方**已僵死**（心跳超时）时允许接管 ——
   上一轮进程被杀后锁还在，不接管就得等 15 分钟超时。

源产品对应
----------
``cli.py:832`` 的 ``exit_with_code()`` 只在**一次进程**里给一个退出码，
没有「同一目标并发执行」的判据；``streaming_orchestrator.py:31-65`` 的
逐 consumer 失败剔除是**单次扫描内**的容错，也不提供跨次重入。本轮把
这两件事补上：重跑粒度由 :func:`select_scope` 决定，并发由锁表决定 ——
常驻服务里同时来两个「扫上周账」的请求是常态，不是异常。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from ..errors import ReconqError
from .period import (
    DEFAULT_STALE_AFTER_SECONDS,
    ORIGIN_CARRIED,
    ORIGIN_THIS_RUN,
    _now,
    origin_zh,
)

__all__ = [
    "CHECKPOINT_TABLE",
    "SCAN_LOCK_TABLE",
    "SCAN_EVENT_TABLE",
    "ORIGIN_THIS_RUN",
    "ORIGIN_CARRIED",
    "origin_zh",
    "ChannelCheckpoint",
    "ScanLock",
    "ScanEvent",
    "RerunScope",
    "init_resume_schema",
    "mark_channel_done",
    "clear_checkpoints",
    "load_checkpoints",
    "select_scope",
    "describe_scope",
    "acquire_scan_lock",
    "heartbeat_scan_lock",
    "release_scan_lock",
    "append_scan_event",
    "list_scan_events",
]

logger = logging.getLogger("reconq.resume")


# —— 断点 ——————————————————————————————————————————————————

CHECKPOINT_TABLE = "scan_checkpoints"

_CHECKPOINT_DDL = f"""
CREATE TABLE IF NOT EXISTS {CHECKPOINT_TABLE} (
    period       TEXT    NOT NULL,
    channel      TEXT    NOT NULL,
    run_id       TEXT    NOT NULL DEFAULT '',
    checks_done  TEXT    NOT NULL DEFAULT '',
    rows_done    INTEGER NOT NULL DEFAULT 0,
    total_amount TEXT    NOT NULL DEFAULT '0',
    done         INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (period, channel)
)
"""

#: 结果行的出处常量与中文标签在 :mod:`reconq.engine.period`（``ORIGIN_THIS_RUN``
#: / ``ORIGIN_CARRIED`` / ``origin_zh``），本模块从那里取，不另写一份 ——
#: 同一个标签在两处各写一遍，日后就会漂移成「本次产出」和「本次结果」两种说法。


@dataclass
class ChannelCheckpoint:
    """一路渠道的断点。"""

    period: str
    channel: str
    run_id: str = ""
    checks_done: list[str] = field(default_factory=list)
    rows_done: int = 0
    total_amount: str = "0"
    done: bool = False
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "run_id": self.run_id,
            "checks_done": list(self.checks_done),
            "rows_done": self.rows_done,
            "total_amount": self.total_amount,
            "done": self.done,
            "updated_at": self.updated_at,
        }


def init_resume_schema(conn: sqlite3.Connection) -> None:
    """建断点表、锁表与事件链表。幂等。"""
    for statement in (_CHECKPOINT_DDL, _SCAN_LOCK_DDL, _SCAN_EVENT_DDL):
        conn.execute(statement)
    for statement in _RESUME_INDEXES:
        conn.execute(statement)


def mark_channel_done(
    conn: sqlite3.Connection,
    period: str,
    channel: str,
    *,
    run_id: str = "",
    checks_done: Optional[Sequence[str]] = None,
    rows_done: int = 0,
    total_amount: str = "0",
) -> ChannelCheckpoint:
    """把一路标成「本轮已完成」并记下断点内容。

    记 ``checks_done`` 而不是只记个布尔：口径被改后重跑，要能回答
    「这一路上次跑过哪几条口径」，否则只能整路重来。
    """
    conn.execute(
        f"INSERT INTO {CHECKPOINT_TABLE} "
        "(period,channel,run_id,checks_done,rows_done,total_amount,done,updated_at) "
        "VALUES (?,?,?,?,?,?,1,?) "
        f"ON CONFLICT(period,channel) DO UPDATE SET "
        "run_id=excluded.run_id, checks_done=excluded.checks_done, "
        "rows_done=excluded.rows_done, total_amount=excluded.total_amount, "
        "done=1, updated_at=excluded.updated_at",
        (
            period,
            channel,
            run_id,
            ",".join(checks_done or ()),
            rows_done,
            total_amount,
            _now(),
        ),
    )
    return ChannelCheckpoint(
        period=period,
        channel=channel,
        run_id=run_id,
        checks_done=list(checks_done or ()),
        rows_done=rows_done,
        total_amount=total_amount,
        done=True,
        updated_at=_now(),
    )


def clear_checkpoints(
    conn: sqlite3.Connection,
    period: str,
    channels: Optional[Sequence[str]] = None,
) -> int:
    """清断点。给了 ``channels`` 只清那几路 —— 断点是**按路**的，
    整期一起清会把「上次 wechat 跑完了」这条信息也抹掉。"""
    if channels:
        placeholders = ",".join("?" for _ in channels)
        cur = conn.execute(
            f"DELETE FROM {CHECKPOINT_TABLE} WHERE period=? AND channel IN ({placeholders})",
            (period, *channels),
        )
    else:
        cur = conn.execute(f"DELETE FROM {CHECKPOINT_TABLE} WHERE period=?", (period,))
    return cur.rowcount or 0


def load_checkpoints(conn: sqlite3.Connection, period: str) -> list[ChannelCheckpoint]:
    """读一个账期每路的断点（按渠道名排序，保持扫描顺序可比）。"""
    return [
        ChannelCheckpoint(
            period=r["period"],
            channel=r["channel"],
            run_id=r["run_id"],
            checks_done=[c for c in (r["checks_done"] or "").split(",") if c],
            rows_done=r["rows_done"],
            total_amount=r["total_amount"],
            done=bool(r["done"]),
            updated_at=r["updated_at"],
        )
        for r in conn.execute(
            f"SELECT * FROM {CHECKPOINT_TABLE} WHERE period=? ORDER BY channel",
            (period,),
        )
    ]


# —— 重跑范围 ——————————————————————————————————————————————


@dataclass
class RerunScope:
    """一次重跑的范围。``channels`` 为空表示全量（不限定渠道）。"""

    channels: list[str] = field(default_factory=list)
    check_selectors: list[str] = field(default_factory=list)
    check_paths: list[str] = field(default_factory=list)
    resume: bool = False
    skipped_channels: list[str] = field(default_factory=list)

    @property
    def is_full(self) -> bool:
        return not self.channels and not self.check_selectors

    def covers(self, channel: str) -> bool:
        return not self.channels or channel in self.channels

    def to_dict(self) -> dict[str, Any]:
        return {
            "channels": list(self.channels),
            "check_selectors": list(self.check_selectors),
            "check_paths": list(self.check_paths),
            "resume": self.resume,
            "skipped_channels": list(self.skipped_channels),
            "full": self.is_full,
        }


def select_scope(
    all_channels: Sequence[str],
    *,
    only_channels: Optional[Sequence[str]] = None,
    only_checks: Optional[Sequence[str]] = None,
    resume: bool = False,
    checkpoints: Optional[Sequence[ChannelCheckpoint]] = None,
) -> RerunScope:
    """算出这次要重跑哪些渠道、哪些口径。

    ``resume=True`` 时把**已完成**的那几路划进 ``skipped_channels`` ——
    续跑不是重跑：已完成的部分不重新取数、不重新落库（行级唯一索引虽然
    挡得住重复行，但白跑一遍六路账单解析没有意义，还会在隔离区留下一条
    「重复扫描」的时间戳让人误以为出了新问题）。
    """
    unknown = [c for c in (only_channels or ()) if c not in all_channels]
    if unknown:
        raise ReconqError(
            "CHANNEL_UNKNOWN",
            f"指定了不存在的渠道：{'、'.join(unknown)}；"
            f"本轮六路为 {'、'.join(all_channels)}",
        )
    scope = RerunScope(
        channels=list(only_channels or ()),
        check_selectors=[s.strip() for s in (only_checks or ()) if s and s.strip()],
        resume=resume,
    )
    if resume:
        done = {cp.channel for cp in (checkpoints or ()) if cp.done}
        if not checkpoints:
            # 这一期**从来没扫过**，没有任何断点。--resume 此时等价于全量扫描 ——
            # 「从断点继续」在没有断点的账期上不能变成「什么都不跑」。
            # 早先的实现会把目标渠道算成空集，然后去要一个根本不存在的
            # 扫描实例，报「该账期尚无扫描实例记录」：人第一次跑就撞上，
            # 而答案本该是「那就完整扫一遍」。
            return scope
        if scope.channels:
            scope.skipped_channels = [c for c in scope.channels if c in done]
            # 断点里已完成、但这次没点名的路，仍然算已完成 —— 续跑看的是
            # 账期的断点，不是命令行点了几路。
            scope.skipped_channels = sorted(
                set(scope.skipped_channels) | (done & set(scope.channels)),
            )
        else:
            scope.channels = [c for c in all_channels if c not in done]
            scope.skipped_channels = [c for c in all_channels if c in done]
    return scope


def describe_scope(scope: RerunScope) -> str:
    """一句话说明这次重跑范围（进事件链与终端输出）。"""
    bits: list[str] = []
    if scope.channels:
        bits.append(f"渠道 {'、'.join(scope.channels)}")
    else:
        bits.append("全部渠道")
    if scope.check_selectors:
        bits.append(f"口径选择器 {'、'.join(scope.check_selectors)}")
    if scope.resume:
        if scope.skipped_channels:
            bits.append(
                f"断点续跑（已完成 {'、'.join(scope.skipped_channels)}，跳过不重跑）"
            )
        else:
            bits.append("断点续跑（无已完成断点，等同全量）")
    return "；".join(bits)


# —— 并发锁 ——————————————————————————————————————————————

SCAN_LOCK_TABLE = "scan_locks"

_SCAN_LOCK_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCAN_LOCK_TABLE} (
    period       TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL DEFAULT '',
    holder       TEXT NOT NULL DEFAULT '',
    actor        TEXT NOT NULL DEFAULT '',
    scope        TEXT NOT NULL DEFAULT '',
    acquired_at  TEXT NOT NULL DEFAULT '',
    heartbeat_at TEXT NOT NULL DEFAULT ''
)
"""

DEFAULT_LOCK_STALE_SECONDS = DEFAULT_STALE_AFTER_SECONDS


@dataclass
class ScanLock:
    """一把已持有的账期锁。``release()`` 幂等。"""

    conn: sqlite3.Connection
    period: str
    run_id: str
    holder: str = ""
    actor: str = ""
    scope: str = ""
    acquired_at: str = ""
    _released: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "run_id": self.run_id,
            "holder": self.holder,
            "actor": self.actor,
            "scope": self.scope,
            "acquired_at": self.acquired_at,
        }


def _parse_stamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _holder_name() -> str:
    return f"pid={os.getpid()}"


def acquire_scan_lock(
    conn: sqlite3.Connection,
    period: str,
    run_id: str,
    *,
    holder: str = "",
    actor: str = "",
    scope: str = "",
    stale_after: int = DEFAULT_LOCK_STALE_SECONDS,
) -> ScanLock:
    """抢一个账期锁。抢不到抛 ``SCAN_IN_PROGRESS``。

    对方心跳超时则**接管**并告警：进程被 kill 之后锁会永远留着，不接管
    就得干等一个超时周期。接管时日志里点名原持有者，便于事后判断
    「刚才那半截扫描是谁留下的」。
    """
    holder = holder or _holder_name()
    stamp = _now()
    try:
        conn.execute(
            f"INSERT INTO {SCAN_LOCK_TABLE} "
            "(period,run_id,holder,actor,scope,acquired_at,heartbeat_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (period, run_id, holder, actor, scope, stamp, stamp),
        )
    except sqlite3.IntegrityError:
        row = conn.execute(
            f"SELECT * FROM {SCAN_LOCK_TABLE} WHERE period=?", (period,)
        ).fetchone()
        beat = _parse_stamp((row["heartbeat_at"] if row else "") or "")
        if beat is not None and datetime.now(beat.tzinfo) - beat > timedelta(seconds=stale_after):
            conn.execute(
                f"UPDATE {SCAN_LOCK_TABLE} SET run_id=?, holder=?, actor=?, scope=?, "
                "acquired_at=?, heartbeat_at=? WHERE period=?",
                (run_id, holder, actor, scope, stamp, stamp, period),
            )
            taken_over = True
            logger.warning(
                "账期 %s 的锁已僵死（原持有者 %s，run=%s，最后心跳 %s），"
                "本次扫描接管；上一轮很可能在扫描中途被杀",
                period,
                row["holder"] if row else "?",
                row["run_id"] if row else "?",
                row["heartbeat_at"] if row else "?",
            )
        else:
            detail = (
                f"账期 {period} 的上一次扫描/重跑还在跑"
                f"（run={row['run_id'] if row else '?'}，"
                f"持有者 {row['holder'] if row else '?'}，"
                f"最后心跳 {row['heartbeat_at'] if row else '?'}）。"
                f"并行跑同一账期会让两批落库互相覆盖，合计在中间态上闪烁；"
                f"等它结束，或先 reconq scan --status 看上一次是否已中断"
            )
            raise ReconqError("SCAN_IN_PROGRESS", detail)
    return ScanLock(
        conn=conn,
        period=period,
        run_id=run_id,
        holder=holder,
        actor=actor,
        scope=scope,
        acquired_at=stamp,
    )


def heartbeat_scan_lock(lock: ScanLock) -> None:
    """续期。扫描过程里每路跑完调一次，长扫描不会把自己锁僵死。"""
    lock.conn.execute(
        f"UPDATE {SCAN_LOCK_TABLE} SET heartbeat_at=? WHERE period=? AND run_id=?",
        (_now(), lock.period, lock.run_id),
    )


def release_scan_lock(lock: ScanLock) -> None:
    """放锁。**只放自己那一把** —— 别人接管之后不能被自己迟到的
    ``finally`` 掀掉（那会把并发保护整个作废）。"""
    if lock._released:
        return
    lock._released = True
    lock.conn.execute(
        f"DELETE FROM {SCAN_LOCK_TABLE} WHERE period=? AND run_id=?",
        (lock.period, lock.run_id),
    )


# —— 重跑事件链 ——————————————————————————————————————————

SCAN_EVENT_TABLE = "scan_events"

_SCAN_EVENT_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCAN_EVENT_TABLE} (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    period     TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    event      TEXT    NOT NULL,
    run_id     TEXT    NOT NULL DEFAULT '',
    actor      TEXT    NOT NULL DEFAULT '',
    scope      TEXT    NOT NULL DEFAULT '',
    scope_json TEXT    NOT NULL DEFAULT '',
    result     TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT '',
    prev_hash  TEXT    NOT NULL DEFAULT '',
    hash       TEXT    NOT NULL DEFAULT '',
    at         TEXT    NOT NULL DEFAULT '',
    UNIQUE (period, seq)
)
"""

#: 链首的前驱指纹。不写这一段全 0，链头和链尾长得一样就没法校验了。
GENESIS_HASH = "0" * 64

_RESUME_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_checkpoint_period ON {CHECKPOINT_TABLE} (period, done)",
    f"CREATE INDEX IF NOT EXISTS ix_event_period ON {SCAN_EVENT_TABLE} (period, seq)",
)


@dataclass
class ScanEvent:
    """一条重跑 / 扫描事件（链上的一环）。"""

    period: str
    seq: int
    event: str
    run_id: str = ""
    actor: str = ""
    scope: str = ""
    scope_data: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    detail: str = ""
    prev_hash: str = GENESIS_HASH
    hash: str = ""
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "seq": self.seq,
            "event": self.event,
            "run_id": self.run_id,
            "actor": self.actor,
            "scope": self.scope,
            "scope_data": self.scope_data,
            "result": self.result,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "at": self.at,
        }


def _event_digest(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def append_scan_event(
    conn: sqlite3.Connection,
    period: str,
    event: str,
    *,
    run_id: str = "",
    actor: str = "",
    scope: str = "",
    scope_data: Optional[dict[str, Any]] = None,
    result: str = "",
    detail: str = "",
) -> ScanEvent:
    """往事件链追加一环，返回落盘后的事件（含指纹）。

    指纹把**上一环的 hash 也算进去**（``prev_hash``），所以删掉中间任何
    一环，后面的 hash 就对不上 —— 结论单要回答「谁、何时、依据哪几条
    证据行」时，改一条事件是能查出来的。Task 45 在这条链上补全量审计
    与按账期切片检索。
    """
    scope_data = scope_data or {}
    row = conn.execute(
        f"SELECT seq, hash FROM {SCAN_EVENT_TABLE} WHERE period=? "
        "ORDER BY seq DESC LIMIT 1",
        (period,),
    ).fetchone()
    seq = (row["seq"] + 1) if row else 1
    prev_hash = (row["hash"] or GENESIS_HASH) if row else GENESIS_HASH
    at = _now()
    digest = _event_digest(
        {
            "period": period,
            "seq": seq,
            "event": event,
            "run_id": run_id,
            "actor": actor,
            "scope": scope,
            "scope_data": scope_data,
            "result": result,
            "detail": detail,
            "prev_hash": prev_hash,
            "at": at,
        }
    )
    conn.execute(
        f"INSERT INTO {SCAN_EVENT_TABLE} "
        "(period,seq,event,run_id,actor,scope,scope_json,result,detail,prev_hash,hash,at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            period,
            seq,
            event,
            run_id,
            actor,
            scope,
            json.dumps(scope_data, ensure_ascii=False, sort_keys=True),
            result,
            detail,
            prev_hash,
            digest,
            at,
        ),
    )
    return ScanEvent(
        period=period,
        seq=seq,
        event=event,
        run_id=run_id,
        actor=actor,
        scope=scope,
        scope_data=scope_data,
        result=result,
        detail=detail,
        prev_hash=prev_hash,
        hash=digest,
        at=at,
    )


def list_scan_events(conn: sqlite3.Connection, period: str) -> list[ScanEvent]:
    """按账期切片读事件链（正序）。"""
    return [
        ScanEvent(
            period=r["period"],
            seq=r["seq"],
            event=r["event"],
            run_id=r["run_id"],
            actor=r["actor"],
            scope=r["scope"],
            scope_data=json.loads(r["scope_json"] or "{}"),
            result=r["result"],
            detail=r["detail"],
            prev_hash=r["prev_hash"],
            hash=r["hash"],
            at=r["at"],
        )
        for r in conn.execute(
            f"SELECT * FROM {SCAN_EVENT_TABLE} WHERE period=? ORDER BY seq", (period,)
        )
    ]
