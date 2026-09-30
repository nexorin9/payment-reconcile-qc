"""账期扫描实例状态机（Task 18）。

源产品对应实现
--------------
``github_ref/soda-core/soda-core/src/soda_core/cli/cli.py:673-829`` 的
``contract request`` 资源命令：``transition`` 子命令把 ``-s/--status``
交给 ``RequestStatus`` 枚举（``cli.py:813`` 的 ``choices=[status.value for
status in RequestStatus]``），非法状态在**参数解析层**就被拒；
``cli.py:820`` 再用 ``RequestStatus(args.status)`` 把字符串收回枚举，
``cli.py:832`` 的 ``exit_with_code()`` 决定进程退出码。

本轮怎么改写
------------
源产品的 request 状态存在**远端注册表**（Soda Cloud），本地只留一个
命令行的瞬时值；本产品要回答的是「上周那期对账现在处在哪一步」，所以：

* 状态**持久化在账单库里**（``period_scans`` 表），与账单行、契约指纹
  出处同库同事务边界 —— 状态和数据分库存，会出现「账单在 A 库、状态在
  B 库」这种对不上的局面；
* ``exit_with_code()`` 的进程退出码改写成**实例状态 + 门禁色**
  （:func:`derive_gate`）：常驻服务不能靠 ``sys.exit`` 表达结论，
  面板与结论单需要的是「这期能不能结账」。

状态迁移是**白名单**（:data:`ALLOWED_TRANSITIONS`），不是「随便设个值」。
财务科的一句话解释是：**没扫过不能说已确认，已确认不能说没扫过**。
``已确认 → 扫描中`` 的直跳被拒（spec 明确点名的反例）；要重扫必须先由
人把实例撤回 :data:`STATUS_PENDING_CONFIRM`（:func:`reopen_scan`），
再走 ``待确认 → 扫描中``，每一次状态变更都留下 note 与时间戳。

中断与恢复
----------
扫描中途进程被杀，库里会留下一条 ``扫描中``。:func:`load_scan` 原样把它
读回来（**不回退成未扫描** —— 那会让「这期扫到一半」和「这期还没开始」
在面板上长得一模一样），由 :func:`is_stale` 依据 heartbeat 判它是否
已经僵死，:func:`recover_stale_scans` 只打 ``interrupted`` 标记，
**不代替人决定重扫还是作废**。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from ..errors import ReconqError

__all__ = [
    "STATUS_UNSCANNED",
    "STATUS_SCANNING",
    "STATUS_PENDING_CONFIRM",
    "STATUS_CONFIRMED",
    "STATUS_DISPUTED",
    "ALL_STATUSES",
    "ALLOWED_TRANSITIONS",
    "STATUS_ZH",
    "GATE_GREEN",
    "GATE_YELLOW",
    "GATE_RED",
    "SCAN_TABLE",
    "SCAN_INDEXES",
    "DIFF_TABLE",
    "DIFF_INDEXES",
    "CHANNEL_RESULT_TABLE",
    "upsert_channel_results",
    "list_channel_results",
    "CHECK_RESULT_TABLE",
    "upsert_check_results",
    "list_check_results",
    "mark_check_results_carried",
    "count_check_results_by_origin",
    "resolve_missing_diffs",
    "count_active_failures",
    "list_diff_items",
    "count_diff_by_ambiguity",
    "origin_zh",
    "ORIGIN_THIS_RUN",
    "ORIGIN_CARRIED",
    "DISPOSITION_PENDING",
    "DISPOSITION_CONFIRMED",
    "DISPOSITION_REJECTED",
    "DEFAULT_STALE_AFTER_SECONDS",
    "PeriodScan",
    "ChannelScan",
    "DiffItem",
    "CheckResultRow",
    "init_scan_schema",
    "load_scan",
    "start_scan",
    "finish_scan",
    "transition",
    "reopen_scan",
    "is_stale",
    "recover_stale_scans",
    "derive_gate",
    "can_transition",
    "allowed_targets",
]

logger = logging.getLogger("reconq.period")

# —— 生命周期状态 ————————————————————————————————————————
STATUS_UNSCANNED = "unscanned"
STATUS_SCANNING = "scanning"
STATUS_PENDING_CONFIRM = "pending_confirm"
STATUS_CONFIRMED = "confirmed"
STATUS_DISPUTED = "disputed"

ALL_STATUSES: tuple[str, ...] = (
    STATUS_UNSCANNED,
    STATUS_SCANNING,
    STATUS_PENDING_CONFIRM,
    STATUS_CONFIRMED,
    STATUS_DISPUTED,
)

STATUS_ZH: dict[str, str] = {
    STATUS_UNSCANNED: "未扫描",
    STATUS_SCANNING: "扫描中",
    STATUS_PENDING_CONFIRM: "待确认",
    STATUS_CONFIRMED: "已确认",
    STATUS_DISPUTED: "已存疑",
}

#: 白名单迁移表。**不在表里的组合一律拒绝**，并报出当前可去的落点。
#:
#: 四条反直觉但必要的收紧：
#:
#: 1. ``confirmed -> scanning`` 不允许（spec 点名的反例）。已确认的账期要
#:    重扫，必须先由人撤回成 ``pending_confirm``（:func:`reopen_scan`）。
#:    理由：已确认的结论单可能已经进了财务科台账，静默重扫会让「上周签过
#:    字的那一版」和「现在这一版」在系统里对不上号。
#: 2. ``scanning -> scanning`` **允许**，且不是废话：它是断点续跑
#:    （Task 19）与「上一轮扫崩了、重启后接着扫」的合法入口。白名单要
#:    挡住的是「跳过了确认环节」，不是「同一状态再进一次」。
#: 3. ``pending_confirm -> scanning`` 允许（口径改了要重扫，Task 17 的
#:    ``needs_reconfirm`` 也走这条路），但 ``pending_confirm`` 上已有的
#:    人工确认结论由绑定表保留，不被重扫抹掉。
#: 4. ``confirmed -> pending_confirm`` 是 reopen 的正路，必须在白名单内
#:    （见下面那段注释）。
#:
#: 4. ``confirmed -> pending_confirm`` **允许**（:func:`reopen_scan` 的正路），
#:    但 ``confirmed -> scanning`` 不允许。护栏不在「不能离开已确认」，
#:    而在**必须先显式撤回一步**才能重扫：直接跳到扫描中等于「不打招呼就
#:    把已签字的那一版冲掉」，而撤回一步会留下 note 与时间戳。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_UNSCANNED: frozenset({STATUS_SCANNING}),
    STATUS_SCANNING: frozenset(
        {STATUS_SCANNING, STATUS_PENDING_CONFIRM, STATUS_CONFIRMED, STATUS_DISPUTED}
    ),
    STATUS_PENDING_CONFIRM: frozenset(
        {STATUS_SCANNING, STATUS_CONFIRMED, STATUS_DISPUTED}
    ),
    # 已确认的出路只有两条：被质疑（存疑）或被显式撤回（待确认）。
    # 两条都要人显式触发，规则、模型、定时任务都不得走到这里。
    STATUS_CONFIRMED: frozenset({STATUS_DISPUTED, STATUS_PENDING_CONFIRM}),
    STATUS_DISPUTED: frozenset({STATUS_PENDING_CONFIRM, STATUS_CONFIRMED}),
}

# —— 门禁色（源产品 exit_with_code 的改写形态）——————————————————
GATE_GREEN = "green"
GATE_YELLOW = "yellow"
GATE_RED = "red"

#: 差异条目的处置状态。完整台账状态机在 Task 43，本轮只落「扫描时写入
#: 待确认」这一条自动路径 —— 确认与驳回**只能人工点**。
DISPOSITION_PENDING = "待确认"
DISPOSITION_CONFIRMED = "已确认"
DISPOSITION_REJECTED = "已驳回"

#: 口径结果的出处。本轮跑出来的 vs 沿用上轮的。财务科在结论单上看到一条
#: 「通过」时，得知道它是刚跑的还是沿用的 —— 不标就等于默认全都新鲜。
ORIGIN_THIS_RUN = "this_run"
ORIGIN_CARRIED = "carried"

#: 这些错误码下的差异**需要人工归因**（成因未解释，单边账），门禁记黄。
#: 码本身由对齐器（Task 21+）产生，本轮先把口径定下来。
ATTRIBUTION_CODES: frozenset[str] = frozenset(
    {"AMBIGUOUS_ENTITY", "WEAK_MATCH_PENDING", "SINGLE_SIDED", "ONE_SIDED_ACCOUNT"}
)

#: 超过这个时长没有 heartbeat，扫描视为僵死（进程被杀 / 机器重启）。
DEFAULT_STALE_AFTER_SECONDS = 900

SCAN_TABLE = "period_scans"

_SCAN_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCAN_TABLE} (
    period              TEXT PRIMARY KEY,
    status              TEXT    NOT NULL,
    gate                TEXT    NOT NULL DEFAULT '',
    reason              TEXT    NOT NULL DEFAULT '',
    channels_total      INTEGER NOT NULL DEFAULT 0,
    channels_ok         INTEGER NOT NULL DEFAULT 0,
    channels_failed     INTEGER NOT NULL DEFAULT 0,
    rows_ingested       INTEGER NOT NULL DEFAULT 0,
    checks_total        INTEGER NOT NULL DEFAULT 0,
    checks_failed       INTEGER NOT NULL DEFAULT 0,
    unconfirmed         INTEGER NOT NULL DEFAULT 0,
    pending_attribution INTEGER NOT NULL DEFAULT 0,
    total_amount        TEXT    NOT NULL DEFAULT '0',
    idempotency_key     TEXT    NOT NULL DEFAULT '',
    run_id              TEXT    NOT NULL DEFAULT '',
    started_at          TEXT    NOT NULL DEFAULT '',
    finished_at         TEXT    NOT NULL DEFAULT '',
    heartbeat_at        TEXT    NOT NULL DEFAULT '',
    interrupted         INTEGER NOT NULL DEFAULT 0,
    note                TEXT    NOT NULL DEFAULT '',
    updated_at          TEXT    NOT NULL DEFAULT ''
)
"""

_SCAN_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_scan_status ON {SCAN_TABLE} (status, period)",
)

#: 差异台账。键 = 账期 + 渠道 + 口径 path，**覆盖语义**（重扫更新而不是
#: 追加）：面板要回答的是「这一期现在有哪些差异」，不是「历史上出现过
#: 多少次差异」。
DIFF_TABLE = "period_diff_items"

_DIFF_DDL = f"""
CREATE TABLE IF NOT EXISTS {DIFF_TABLE} (
    diff_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT    NOT NULL,
    channel         TEXT    NOT NULL,
    check_path      TEXT    NOT NULL,
    check_name      TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'fail',
    message         TEXT    NOT NULL DEFAULT '',
    diff_amount     TEXT,
    evidence_total  INTEGER NOT NULL DEFAULT 0,
    source_file     TEXT    NOT NULL DEFAULT '',
    first_row_no    INTEGER,
    disposition     TEXT    NOT NULL DEFAULT '{DISPOSITION_PENDING}',
    needs_attribution INTEGER NOT NULL DEFAULT 0,
    run_id          TEXT    NOT NULL DEFAULT '',
    recorded_at     TEXT    NOT NULL DEFAULT '',
    resolved        INTEGER NOT NULL DEFAULT 0,
    resolved_at     TEXT    NOT NULL DEFAULT '',
    resolved_run_id TEXT    NOT NULL DEFAULT '',
    ambiguity_kind  TEXT    NOT NULL DEFAULT ''
)
"""

_DIFF_INDEXES = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_diff_identity "
    f"ON {DIFF_TABLE} (period, channel, check_path)",
    f"CREATE INDEX IF NOT EXISTS ix_diff_period ON {DIFF_TABLE} (period, disposition)",
    f"CREATE INDEX IF NOT EXISTS ix_diff_resolved ON {DIFF_TABLE} (period, resolved)",
    # 面板「按成因分组」筛选（Task 25）。台账里绝大多数条目 ambiguity_kind
    # 是空串（口径差异），索引前导列用 period 让筛选只扫本账期。
    f"CREATE INDEX IF NOT EXISTS ix_diff_ambiguity ON {DIFF_TABLE} (period, ambiguity_kind)",
)

#: 每条 check 的执行结果（不管过没过都记）。差异台账只记 fail 的那几条，
#: 但**「这条口径这次过了」本身就是结论单上要写的东西** —— 财务科问
#: 「这期到底跑了哪几条口径、哪几条是沿用上周的」，答不上来就不能签字。
#: 键 = 账期 + 渠道 + 口径 path，覆盖语义；``origin`` 区分本次产出与沿用。
CHECK_RESULT_TABLE = "period_check_results"

_CHECK_RESULT_DDL = f"""
CREATE TABLE IF NOT EXISTS {CHECK_RESULT_TABLE} (
    period        TEXT    NOT NULL,
    channel       TEXT    NOT NULL,
    check_path    TEXT    NOT NULL,
    check_name    TEXT    NOT NULL DEFAULT '',
    operator      TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'pass',
    message       TEXT    NOT NULL DEFAULT '',
    diff_amount   TEXT,
    evidence_total INTEGER NOT NULL DEFAULT 0,
    source_file   TEXT    NOT NULL DEFAULT '',
    first_row_no  INTEGER,
    origin        TEXT    NOT NULL DEFAULT 'this_run',
    run_id        TEXT    NOT NULL DEFAULT '',
    recorded_at   TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (period, channel, check_path)
)
"""

_CHECK_RESULT_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_check_result_origin "
    f"ON {CHECK_RESULT_TABLE} (period, origin)",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ChannelScan:
    """一路渠道在本次扫描里的结果。"""

    channel: str
    status: str = "not_fetched"  # checked | not_fetched | failed
    row_count: int = 0
    total_amount: str = "0"
    checks_total: int = 0
    checks_failed: int = 0
    inserted: int = 0
    skipped_duplicates: int = 0
    error_code: str = ""
    cause_code: str = ""
    detail: str = ""
    idempotency_keys: list[str] = field(default_factory=list)
    #: 本期进隔离区的脏行数（Task 11 落隔离区时填）。面板要能回答
    #: 「这 N 行去哪了」——合计被读小了却没人知道少的是脏行，最难查。
    dirty_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "status": self.status,
            "row_count": self.row_count,
            "total_amount": self.total_amount,
            "checks_total": self.checks_total,
            "checks_failed": self.checks_failed,
            "inserted": self.inserted,
            "skipped_duplicates": self.skipped_duplicates,
            "error_code": self.error_code or None,
            "cause_code": self.cause_code or None,
            "detail": self.detail,
            "dirty_rows": self.dirty_rows,
        }


@dataclass
class PeriodScan:
    """一个账期的扫描实例。"""

    period: str
    status: str = STATUS_UNSCANNED
    gate: str = ""
    reason: str = ""
    channels_total: int = 0
    channels_ok: int = 0
    channels_failed: int = 0
    rows_ingested: int = 0
    checks_total: int = 0
    checks_failed: int = 0
    unconfirmed: int = 0
    pending_attribution: int = 0
    total_amount: str = "0"
    idempotency_key: str = ""
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    heartbeat_at: str = ""
    interrupted: bool = False
    note: str = ""
    updated_at: str = ""
    channels: list[ChannelScan] = field(default_factory=list)

    @property
    def status_zh(self) -> str:
        return STATUS_ZH.get(self.status, self.status)

    @property
    def is_open(self) -> bool:
        """实例是否仍可被推进（未扫描 / 扫描中 / 待确认）。"""
        return self.status in (
            STATUS_UNSCANNED,
            STATUS_SCANNING,
            STATUS_PENDING_CONFIRM,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "status": self.status,
            "status_zh": self.status_zh,
            "gate": self.gate or None,
            "reason": self.reason,
            "channels_total": self.channels_total,
            "channels_ok": self.channels_ok,
            "channels_failed": self.channels_failed,
            "rows_ingested": self.rows_ingested,
            "checks_total": self.checks_total,
            "checks_failed": self.checks_failed,
            "unconfirmed": self.unconfirmed,
            "pending_attribution": self.pending_attribution,
            "total_amount": self.total_amount,
            "idempotency_key": self.idempotency_key,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "heartbeat_at": self.heartbeat_at,
            "interrupted": self.interrupted,
            "stale": is_stale(self),
            "note": self.note,
            "updated_at": self.updated_at,
            "channels": [c.to_dict() for c in self.channels],
        }


@dataclass(frozen=True)
class DiffItem:
    """一条差异台账记录。"""

    period: str
    channel: str
    check_path: str
    check_name: str = ""
    status: str = "fail"
    message: str = ""
    diff_amount: Optional[str] = None
    evidence_total: int = 0
    source_file: str = ""
    first_row_no: Optional[int] = None
    disposition: str = DISPOSITION_PENDING
    needs_attribution: bool = False
    run_id: str = ""
    recorded_at: str = ""
    resolved: bool = False
    resolved_at: str = ""
    resolved_run_id: str = ""
    #: 歧义成因分类（Task 25 加）。只对 ``check_path`` 以 ``taxonomy.``
    #: 开头的台账条目有意义，其余留空字符串。**空串是正常值而不是缺失**：
    #: 面板按成因分组筛选时，非歧义条目归到「口径差异」那一组，不能悄悄
    #: 混进「不可判定」—— 那会让成因筛选的数字虚高。
    ambiguity_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "check_path": self.check_path,
            "check_name": self.check_name,
            "status": self.status,
            "message": self.message,
            "diff_amount": self.diff_amount,
            "evidence_total": self.evidence_total,
            "source_file": self.source_file,
            "first_row_no": self.first_row_no,
            "disposition": self.disposition,
            "needs_attribution": self.needs_attribution,
            "run_id": self.run_id,
            "recorded_at": self.recorded_at,
            "resolved": self.resolved,
            "resolved_at": self.resolved_at,
            "resolved_run_id": self.resolved_run_id,
            "ambiguity_kind": self.ambiguity_kind,
        }


@dataclass(frozen=True)
class CheckResultRow:
    """一条口径的执行结果（过没过都记）。"""

    period: str
    channel: str
    check_path: str
    check_name: str = ""
    operator: str = ""
    status: str = "pass"
    message: str = ""
    diff_amount: Optional[str] = None
    evidence_total: int = 0
    source_file: str = ""
    first_row_no: Optional[int] = None
    origin: str = "this_run"
    run_id: str = ""
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "check_path": self.check_path,
            "check_name": self.check_name,
            "operator": self.operator,
            "status": self.status,
            "message": self.message,
            "diff_amount": self.diff_amount,
            "evidence_total": self.evidence_total,
            "source_file": self.source_file,
            "first_row_no": self.first_row_no,
            "origin": self.origin,
            "origin_zh": origin_zh(self.origin),
            "run_id": self.run_id,
            "recorded_at": self.recorded_at,
        }


def origin_zh(origin: str) -> str:
    """出处的中文标签。「本次产出 / 沿用上次」只有一个真源。"""
    return "本次产出" if origin == ORIGIN_THIS_RUN else "沿用上次"


# —— 表结构 ——————————————————————————————————————————————


#: 每路渠道的扫描结果。面板「六路各自主态」这一屏在**进程重启后**也要
#: 答得出来，所以不能只留在内存里：键 = 账期 + 渠道，覆盖语义。
CHANNEL_RESULT_TABLE = "period_channel_results"

_CHANNEL_RESULT_DDL = f"""
CREATE TABLE IF NOT EXISTS {CHANNEL_RESULT_TABLE} (
    period             TEXT    NOT NULL,
    channel            TEXT    NOT NULL,
    status             TEXT    NOT NULL,
    row_count          INTEGER NOT NULL DEFAULT 0,
    total_amount       TEXT    NOT NULL DEFAULT '0',
    checks_total       INTEGER NOT NULL DEFAULT 0,
    checks_failed      INTEGER NOT NULL DEFAULT 0,
    inserted           INTEGER NOT NULL DEFAULT 0,
    skipped_duplicates INTEGER NOT NULL DEFAULT 0,
    error_code         TEXT    NOT NULL DEFAULT '',
    cause_code         TEXT    NOT NULL DEFAULT '',
    detail             TEXT    NOT NULL DEFAULT '',
    dirty_rows         INTEGER NOT NULL DEFAULT 0,
    idempotency_keys   TEXT    NOT NULL DEFAULT '',
    run_id             TEXT    NOT NULL DEFAULT '',
    recorded_at        TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (period, channel)
)
"""

_CHANNEL_RESULT_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_channel_result_period "
    f"ON {CHANNEL_RESULT_TABLE} (period, status)",
)


def init_scan_schema(conn: sqlite3.Connection) -> None:
    """建账期实例表、渠道结果表、差异台账与口径结果表。幂等。

    **迁移先于建索引**：``ix_diff_ambiguity`` 引用 ``ambiguity_kind``，
    而老库上这一列要靠 ``ALTER TABLE`` 补。先建索引的话，老库升级时
    直接 ``no such column: ambiguity_kind`` 抛在初始化里 —— 新库一切
    正常，只有**升级上来的库**起不来，症状是「昨天还能跑」。
    """
    conn.execute(_SCAN_DDL)
    conn.execute(_CHANNEL_RESULT_DDL)
    conn.execute(_DIFF_DDL)
    conn.execute(_CHECK_RESULT_DDL)
    _migrate_diff_resolved_columns(conn)
    _migrate_diff_ambiguity_kind_column(conn)
    _migrate_channel_dirty_rows_column(conn)
    for statement in (
        *_SCAN_INDEXES,
        *_CHANNEL_RESULT_INDEXES,
        *_DIFF_INDEXES,
        *_CHECK_RESULT_INDEXES,
    ):
        conn.execute(statement)


def _migrate_diff_ambiguity_kind_column(conn: sqlite3.Connection) -> None:
    """给已存在的差异表补 ``ambiguity_kind`` 列（Task 25 加的成因分类）。

    与 :func:`_migrate_diff_resolved_columns` 同理：不 ALTER 的话，老库里
    读不出这一列，``SELECT *`` 之后的构造直接 KeyError，而这类失败只在
    **升级后才出现**的库上冒出来。
    """
    existing = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({DIFF_TABLE})")
    }
    if "ambiguity_kind" not in existing:
        conn.execute(
            f"ALTER TABLE {DIFF_TABLE} "
            "ADD COLUMN ambiguity_kind TEXT NOT NULL DEFAULT ''"
        )


def _migrate_diff_resolved_columns(conn: sqlite3.Connection) -> None:
    """给已存在的差异表补 ``resolved`` 三列（Task 19 加的消解标记）。

    ``CREATE TABLE IF NOT EXISTS`` 对**已建好**的库不会补列，所以老库必须
    走一次 ``ALTER TABLE`` 迁移。不做这一步的话，重跑后老账期的差异行
    永远没有消解标记，「消失的差异」会一直挂在待确认计数里。
    """
    existing = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({DIFF_TABLE})")
    }
    for column, ddl in (
        ("resolved", "INTEGER NOT NULL DEFAULT 0"),
        ("resolved_at", "TEXT NOT NULL DEFAULT ''"),
        ("resolved_run_id", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE {DIFF_TABLE} ADD COLUMN {column} {ddl}")


def _migrate_channel_dirty_rows_column(conn: sqlite3.Connection) -> None:
    """给已存在的渠道结果表补 ``dirty_rows`` 列（Task 20 加）。

    与 ``_migrate_diff_resolved_columns`` 同理：``CREATE TABLE IF NOT
    EXISTS`` 不给老库补列。不补的话老账期的渠道结果读出来少一个字段，
    面板会在「本期脏行」那一格显示空白而不是 0。
    """
    existing = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({CHANNEL_RESULT_TABLE})")
    }
    if "dirty_rows" not in existing:
        conn.execute(
            f"ALTER TABLE {CHANNEL_RESULT_TABLE} "
            f"ADD COLUMN dirty_rows INTEGER NOT NULL DEFAULT 0"
        )


def upsert_channel_results(
    conn: sqlite3.Connection,
    period: str,
    results: list[ChannelScan],
    *,
    run_id: str = "",
) -> int:
    """把每路渠道的结果落库（覆盖语义），供面板与结论单在重启后仍能读。"""
    _require_schema(conn, CHANNEL_RESULT_TABLE)
    written = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in results:
            conn.execute(
                f"INSERT INTO {CHANNEL_RESULT_TABLE} ("
                "period,channel,status,row_count,total_amount,checks_total,checks_failed,"
                "inserted,skipped_duplicates,error_code,cause_code,detail,"
                "dirty_rows,idempotency_keys,run_id,recorded_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(period,channel) DO UPDATE SET "
                "status=excluded.status, row_count=excluded.row_count, "
                "total_amount=excluded.total_amount, checks_total=excluded.checks_total, "
                "checks_failed=excluded.checks_failed, inserted=excluded.inserted, "
                "skipped_duplicates=excluded.skipped_duplicates, "
                "error_code=excluded.error_code, cause_code=excluded.cause_code, "
                "detail=excluded.detail, dirty_rows=excluded.dirty_rows, "
                "idempotency_keys=excluded.idempotency_keys, "
                "run_id=excluded.run_id, recorded_at=excluded.recorded_at",
                (
                    period,
                    item.channel,
                    item.status,
                    item.row_count,
                    item.total_amount,
                    item.checks_total,
                    item.checks_failed,
                    item.inserted,
                    item.skipped_duplicates,
                    item.error_code,
                    item.cause_code,
                    item.detail,
                    item.dirty_rows,
                    ",".join(item.idempotency_keys),
                    run_id,
                    _now(),
                ),
            )
            written += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return written


def list_channel_results(conn: sqlite3.Connection, period: str) -> list[ChannelScan]:
    """读一个账期每路渠道的结果（面板那屏的主数据）。"""
    _require_schema(conn, CHANNEL_RESULT_TABLE)
    return [
        ChannelScan(
            channel=r["channel"],
            status=r["status"],
            row_count=r["row_count"],
            total_amount=r["total_amount"],
            checks_total=r["checks_total"],
            checks_failed=r["checks_failed"],
            inserted=r["inserted"],
            skipped_duplicates=r["skipped_duplicates"],
            error_code=r["error_code"],
            cause_code=r["cause_code"],
            detail=r["detail"],
            dirty_rows=r["dirty_rows"] if "dirty_rows" in r.keys() else 0,
            idempotency_keys=[k for k in r["idempotency_keys"].split(",") if k],
        )
        for r in conn.execute(
            f"SELECT * FROM {CHANNEL_RESULT_TABLE} WHERE period=? ORDER BY channel",
            (period,),
        )
    ]


def _require_schema(conn: sqlite3.Connection, table: str) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        raise ReconqError(
            "PERIOD_SCAN_NOT_INITIALIZED",
            f"{table} 表尚未建表；用 reconq.store.schema.init_scan_schema() 建表"
            f"（或先跑一次 reconq scan --period <账期>）",
        )


# —— 白名单迁移 ————————————————————————————————————————————


def allowed_targets(status: str) -> list[str]:
    """当前状态可以去的落点（用于报错时告诉人「现在能去哪」）。"""
    if status not in ALLOWED_TRANSITIONS:
        return []
    return sorted(ALLOWED_TRANSITIONS[status])


def can_transition(current: str, target: str) -> bool:
    """这条迁移是否在白名单内。不在则 False（不抛错，供 UI 做禁用态）。"""
    if target not in ALL_STATUSES:
        return False
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def _assert_transition(current: str, target: str, *, period: str) -> None:
    if target not in ALL_STATUSES:
        raise ReconqError(
            "PERIOD_STATE_INVALID",
            f"账期 {period} 目标状态 {target!r} 不是本产品的合法状态；"
            f"合法状态：{'、'.join(f'{s}（{STATUS_ZH[s]}）' for s in ALL_STATUSES)}",
        )
    if target in ALLOWED_TRANSITIONS.get(current, frozenset()):
        return
    zh_now = STATUS_ZH.get(current, current)
    zh_to = STATUS_ZH.get(target, target)
    targets = allowed_targets(current)
    hint = f"当前可去：{'、'.join(STATUS_ZH.get(t, t) for t in targets)}" if targets else (
        "当前状态没有出边"
    )
    # 已确认 / 已存疑要重扫时，**把那条唯一可行的路直接说出来**。只报
    # 「当前可去：已存疑、待确认」的话，财务科还得自己猜「待确认」是
    # 怎么回去的。
    route = ""
    if current in (STATUS_CONFIRMED, STATUS_DISPUTED) and target in (
        STATUS_SCANNING,
        STATUS_PENDING_CONFIRM,
    ):
        route = (
            "已确认的账期要先由人工撤回成「待确认」才能重扫："
            "reconq scan --period <账期> --reopen"
        )
    raise ReconqError(
        "PERIOD_STATE_TRANSITION_DENIED",
        f"账期 {period} 不能从「{zh_now}」直接跳到「{zh_to}」：{hint}。"
        + (f"{route}。" if route else "")
        + "状态迁移走白名单，不是随便设值 —— "
        "没扫过不能说已确认，已确认不能说没扫过",
    )


# —— 读 ——————————————————————————————————————————————————


def _row_to_scan(row: sqlite3.Row) -> PeriodScan:
    return PeriodScan(
        period=row["period"],
        status=row["status"],
        gate=row["gate"],
        reason=row["reason"],
        channels_total=row["channels_total"],
        channels_ok=row["channels_ok"],
        channels_failed=row["channels_failed"],
        rows_ingested=row["rows_ingested"],
        checks_total=row["checks_total"],
        checks_failed=row["checks_failed"],
        unconfirmed=row["unconfirmed"],
        pending_attribution=row["pending_attribution"],
        total_amount=row["total_amount"],
        idempotency_key=row["idempotency_key"],
        run_id=row["run_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        heartbeat_at=row["heartbeat_at"],
        interrupted=bool(row["interrupted"]),
        note=row["note"],
        updated_at=row["updated_at"],
    )


def load_scan(conn: sqlite3.Connection, period: str) -> Optional[PeriodScan]:
    """读一个账期的扫描实例；从未扫过则 None。

    **原样返回** ``扫描中`` 的行：中断的扫描不回退成 ``未扫描``。
    """
    _require_schema(conn, SCAN_TABLE)
    row = conn.execute(f"SELECT * FROM {SCAN_TABLE} WHERE period=?", (period,)).fetchone()
    return _row_to_scan(row) if row is not None else None


def require_scan(conn: sqlite3.Connection, period: str) -> PeriodScan:
    scan = load_scan(conn, period)
    if scan is None:
        raise ReconqError(
            "PERIOD_SCAN_NOT_INITIALIZED",
            f"账期 {period} 还没有扫描记录；先跑 reconq scan --period {period}",
        )
    return scan


def is_stale(scan: PeriodScan, *, after_seconds: int = DEFAULT_STALE_AFTER_SECONDS) -> bool:
    """扫描中的实例是否已僵死（heartbeat 超时）。"""
    if scan.status != STATUS_SCANNING:
        return False
    if scan.interrupted:
        return True
    stamp = scan.heartbeat_at or scan.started_at
    if not stamp:
        return True
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return datetime.now(moment.tzinfo) - moment > timedelta(seconds=after_seconds)


def recover_stale_scans(
    conn: sqlite3.Connection,
    *,
    after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> list[PeriodScan]:
    """把僵死的 ``扫描中`` 打上 ``interrupted`` 标记。

    **只打标记，不改状态、不触发重扫**：这一期到底重扫还是作废，是财务科
    的决定，不是系统的。返回值是被标记的实例列表（给面板提示）。
    """
    _require_schema(conn, SCAN_TABLE)
    rows = conn.execute(
        f"SELECT * FROM {SCAN_TABLE} WHERE status=? AND interrupted=0 ORDER BY period",
        (STATUS_SCANNING,),
    ).fetchall()
    recovered: list[PeriodScan] = []
    for row in rows:
        scan = _row_to_scan(row)
        if not is_stale(scan, after_seconds=after_seconds):
            continue
        note = (
            f"上一次扫描在 {scan.heartbeat_at or scan.started_at or '未知时刻'} "
            f"中断（进程退出或机器重启），实例保持「{STATUS_ZH[STATUS_SCANNING]}」"
            f"等待人工决定重扫或作废；系统不自动重跑，也不回退成「{STATUS_ZH[STATUS_UNSCANNED]}」"
        )
        conn.execute(
            f"UPDATE {SCAN_TABLE} SET interrupted=1, note=?, updated_at=? WHERE period=?",
            (note, _now(), scan.period),
        )
        scan.interrupted = True
        scan.note = note
        scan.updated_at = _now()
        logger.warning("账期 %s 的扫描实例已僵死，打上 interrupted 标记：%s", scan.period, note)
        recovered.append(scan)
    return recovered


# —— 写 ——————————————————————————————————————————————————


def _write(conn: sqlite3.Connection, scan: PeriodScan) -> PeriodScan:
    scan.updated_at = _now()
    conn.execute(
        f"INSERT INTO {SCAN_TABLE} ("
        "period,status,gate,reason,channels_total,channels_ok,channels_failed,"
        "rows_ingested,checks_total,checks_failed,unconfirmed,pending_attribution,"
        "total_amount,idempotency_key,run_id,started_at,finished_at,heartbeat_at,"
        "interrupted,note,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(period) DO UPDATE SET "
        "status=excluded.status, gate=excluded.gate, reason=excluded.reason, "
        "channels_total=excluded.channels_total, channels_ok=excluded.channels_ok, "
        "channels_failed=excluded.channels_failed, rows_ingested=excluded.rows_ingested, "
        "checks_total=excluded.checks_total, checks_failed=excluded.checks_failed, "
        "unconfirmed=excluded.unconfirmed, pending_attribution=excluded.pending_attribution, "
        "total_amount=excluded.total_amount, idempotency_key=excluded.idempotency_key, "
        "run_id=excluded.run_id, started_at=excluded.started_at, "
        "finished_at=excluded.finished_at, heartbeat_at=excluded.heartbeat_at, "
        "interrupted=excluded.interrupted, note=excluded.note, updated_at=excluded.updated_at",
        (
            scan.period,
            scan.status,
            scan.gate,
            scan.reason,
            scan.channels_total,
            scan.channels_ok,
            scan.channels_failed,
            scan.rows_ingested,
            scan.checks_total,
            scan.checks_failed,
            scan.unconfirmed,
            scan.pending_attribution,
            scan.total_amount,
            scan.idempotency_key,
            scan.run_id,
            scan.started_at,
            scan.finished_at,
            scan.heartbeat_at,
            1 if scan.interrupted else 0,
            scan.note,
            scan.updated_at,
        ),
    )
    return scan


def transition(
    conn: sqlite3.Connection,
    period: str,
    target: str,
    *,
    note: str = "",
    run_id: str = "",
    gate: str = "",
    reason: str = "",
) -> PeriodScan:
    """白名单迁移。不在白名单内抛 :class:`ReconqError` 并列出可去落点。"""
    scan = require_scan(conn, period)
    _assert_transition(scan.status, target, period=period)
    previous = scan.status
    scan.status = target
    if gate:
        scan.gate = gate
    if reason:
        scan.reason = reason
    if run_id:
        scan.run_id = run_id
    if target == STATUS_CONFIRMED or target == STATUS_DISPUTED:
        scan.finished_at = scan.finished_at or _now()
    if target == STATUS_SCANNING:
        scan.interrupted = False
        scan.heartbeat_at = _now()
    if note:
        scan.note = f"{STATUS_ZH.get(previous, previous)} → {STATUS_ZH.get(target, target)}：{note}"
    return _write(conn, scan)


def start_scan(
    conn: sqlite3.Connection,
    period: str,
    *,
    run_id: str = "",
    idempotency_key: str = "",
    channels_total: int = 0,
    note: str = "",
) -> PeriodScan:
    """开扫：把实例推进到「扫描中」，并登记本次 run 与幂等键。

    * 首次扫描：``未扫描 → 扫描中``；
    * 断点续跑 / 重扫：``扫描中 → 扫描中``、``待确认 → 扫描中``、
      ``已存疑 → 待确认 → 扫描中``（后者由 :func:`reopen_scan` 先撤回）；
    * **已确认的账期不走这里** —— 必须先 :func:`reopen_scan`，否则
      :func:`_assert_transition` 直接拒。
    """
    scan = load_scan(conn, period)
    if scan is None:
        stamp = _now()
        scan = PeriodScan(
            period=period,
            status=STATUS_UNSCANNED,
            started_at=stamp,
            channels_total=channels_total,
        )
        _write(conn, scan)
    _assert_transition(scan.status, STATUS_SCANNING, period=period)
    previous = scan.status
    scan.status = STATUS_SCANNING
    scan.interrupted = False
    scan.started_at = scan.started_at or _now()
    scan.finished_at = ""
    scan.heartbeat_at = _now()
    scan.run_id = run_id or scan.run_id
    scan.idempotency_key = idempotency_key or scan.idempotency_key
    if channels_total:
        scan.channels_total = channels_total
    if note:
        # 人工撤回的痕迹**保留**：这一期是显式撤回后重扫的，结论单上要查得到。
        trail = scan.note if "人工撤回" in (scan.note or "") else ""
        entry = f"{STATUS_ZH.get(previous, previous)} → 扫描中：{note}"
        scan.note = f"{trail} ｜ {entry}" if trail else entry
    return _write(conn, scan)


def finish_scan(
    conn: sqlite3.Connection,
    period: str,
    *,
    channels_ok: int,
    channels_failed: int,
    rows_ingested: int,
    checks_total: int,
    checks_failed: int,
    unconfirmed: int,
    pending_attribution: int,
    total_amount: str,
    note: str = "",
) -> PeriodScan:
    """收尾：算出门禁色并落到终态。

    落 ``已确认`` 还是 ``待确认`` 由 :func:`derive_gate` 决定，**不由调用
    方**决定 —— 「能不能结账」是口径算出来的，不是调用者挑的。
    """
    scan = require_scan(conn, period)
    _assert_transition(scan.status, STATUS_PENDING_CONFIRM, period=period)
    scan.channels_ok = channels_ok
    scan.channels_failed = channels_failed
    scan.rows_ingested = rows_ingested
    scan.checks_total = checks_total
    scan.checks_failed = checks_failed
    scan.unconfirmed = unconfirmed
    scan.pending_attribution = pending_attribution
    scan.total_amount = total_amount
    scan.heartbeat_at = _now()
    gate, reason = derive_gate(
        checks_failed=checks_failed,
        unconfirmed=unconfirmed,
        pending_attribution=pending_attribution,
        channels_failed=channels_failed,
        channels_total=scan.channels_total,
        checks_total=checks_total,
    )
    scan.gate = gate
    scan.reason = reason
    target = STATUS_CONFIRMED if gate == GATE_GREEN else STATUS_PENDING_CONFIRM
    scan.status = target
    scan.finished_at = _now()
    # 保留重跑前的**人工撤回痕迹**：这一期是被显式撤回后重扫的，
    # 这条事实在结论单上要查得到，不能被「扫描完成 → …」覆盖掉。
    reopened_from = ""
    if "人工撤回" in (scan.note or ""):
        reopened_from = scan.note
    completion = f"扫描完成 → {STATUS_ZH[target]}（{reason}）"
    if note:
        completion += f"：{note}"
    scan.note = f"{reopened_from} ｜ {completion}" if reopened_from else completion
    return _write(conn, scan)


def reopen_scan(
    conn: sqlite3.Connection,
    period: str,
    *,
    note: str = "",
    target: str = STATUS_PENDING_CONFIRM,
) -> PeriodScan:
    """人工撤回：把「已确认 / 已存疑」退回「待确认」，为重扫开门。

    这是**唯一**能让已确认账期重扫的路径，且只能显式调用 —— 规则、模型、
    定时任务都不得走到这里。已确认的结论单可能已经进了财务科台账，
    静默重扫会让「上周签过字的那一版」和「现在这一版」对不上号。
    """
    scan = require_scan(conn, period)
    if scan.status == target:
        return scan
    _assert_transition(scan.status, target, period=period)
    previous = scan.status
    scan.status = target
    scan.finished_at = ""
    scan.interrupted = False
    scan.gate = ""
    scan.reason = f"人工撤回自「{STATUS_ZH.get(previous, previous)}」，待重扫"
    if note:
        scan.note = f"人工撤回：{note}"
    else:
        scan.note = f"{STATUS_ZH.get(previous, previous)} → {STATUS_ZH[target]}（人工撤回）"
    logger.info("账期 %s 由「%s」人工撤回为「%s」", period, previous, target)
    return _write(conn, scan)


# —— 门禁（源产品 exit_with_code 的改写）————————————————————


def derive_gate(
    *,
    checks_failed: int,
    unconfirmed: int,
    pending_attribution: int,
    channels_failed: int = 0,
    channels_total: int = 0,
    checks_total: int = 0,
) -> tuple[str, str]:
    """把扫描结果折成门禁色 + 中文理由。

    对应 spec 的映射：
    ``check 全通过 → 绿``；``有 fail 且已全部确认 → 绿``；
    ``有 fail 未确认 → 红``；``存在待归因单边账 → 黄``。

    **优先级刻意是 红 > 黄 > 绿**：有没被确认的差异时，那期账就是不能结，
    哪怕同时存在待归因的单边账。反过来（只有待归因）时给黄而不是红 ——
    成因还没排出来，不等于已经确认错了。任一路渠道没取到账单也**不算绿**：
    六路少一路的「全通过」不是全通过。
    """
    if checks_failed == 0 and channels_failed == 0:
        return GATE_GREEN, f"{checks_total} 条口径全部通过，六路均已取到"
    if unconfirmed > 0:
        return GATE_RED, f"{unconfirmed} 条差异尚未人工确认，本期不能结账"
    if pending_attribution > 0:
        return GATE_YELLOW, f"{pending_attribution} 条单边账待人工归因，结论尚不完整"
    if channels_failed > 0:
        if checks_failed > 0:
            return GATE_YELLOW, (
                f"{checks_failed} 条差异已全部确认，但有 {channels_failed} 路渠道未取到账单，"
                f"覆盖面不全"
            )
        return GATE_YELLOW, f"{channels_failed} 路渠道未取到账单，覆盖面不全"
    return GATE_GREEN, f"{checks_failed} 条差异已全部人工确认"


# —— 差异台账 ————————————————————————————————————————————


def upsert_diff_items(
    conn: sqlite3.Connection,
    items: list[DiffItem],
) -> int:
    """差异台账覆盖写入，返回写/更新条数。

    唯一键 ``(period, channel, check_path)`` + ``ON CONFLICT DO UPDATE``：
    重扫同一条口径只更新不追加，``disposition`` **原样保留** —— 重跑口径
    不该把经办人已经确认过的差异打回待确认，那等于让主管每周重确认一次。

    同时把 ``resolved`` 清回 0：一条差异在重跑里**又出现了**，它就不再是
    「本次对齐后消失」，消解标记必须撤掉。
    """
    if not items:
        return 0
    _require_schema(conn, DIFF_TABLE)
    written = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in items:
            conn.execute(
                f"INSERT INTO {DIFF_TABLE} ("
                "period,channel,check_path,check_name,status,message,diff_amount,"
                "evidence_total,source_file,first_row_no,disposition,needs_attribution,"
                "run_id,recorded_at,resolved,resolved_at,resolved_run_id,ambiguity_kind"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'','',?) "
                "ON CONFLICT(period,channel,check_path) DO UPDATE SET "
                "check_name=excluded.check_name, status=excluded.status, "
                "message=excluded.message, diff_amount=excluded.diff_amount, "
                "evidence_total=excluded.evidence_total, source_file=excluded.source_file, "
                "first_row_no=excluded.first_row_no, "
                "needs_attribution=excluded.needs_attribution, "
                "run_id=excluded.run_id, recorded_at=excluded.recorded_at, "
                "ambiguity_kind=excluded.ambiguity_kind, "
                "resolved=0, resolved_at='', resolved_run_id=''",
                (
                    item.period,
                    item.channel,
                    item.check_path,
                    item.check_name,
                    item.status,
                    item.message,
                    item.diff_amount,
                    item.evidence_total,
                    item.source_file,
                    item.first_row_no,
                    item.disposition,
                    1 if item.needs_attribution else 0,
                    item.run_id,
                    item.recorded_at or _now(),
                    item.ambiguity_kind or "",
                ),
            )
            written += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return written


def resolve_missing_diffs(
    conn: sqlite3.Connection,
    period: str,
    *,
    channels: Sequence[str],
    seen_paths: dict[str, set[str]],
    eligible_paths: Optional[dict[str, set[str]]] = None,
    run_id: str = "",
) -> list[str]:
    """把本次重跑**真正覆盖到**且不再出现的差异标消解，返回被消解的键。

    ``seen_paths``：本次重跑后这一路仍 fail 的口径集合。
    ``eligible_paths``：本次**实际执行过**的口径集合（逐路）。给了它就
    只在这些口径里判消解。

    这两个集合必须分开，理由是本任务里最危险的一种假绿：
    ``--only-check row_count`` 只跑了 1 条口径，另外 14 条**根本没跑**。
    若把「没跑」当成「跑过且通过」，那 14 条里原本 fail 的差异会被全部
    标成已消解，结论单上写着「差异已消解、本期可结账」—— 财务科什么都没
    做，一期账就这么凭空变绿了。所以 ``eligible_paths`` 之外的口径，
    本次不产生任何消解结论。

    为什么消解也不删行：结论单上「上周 3 条差异，本周 0 条」和「上周根本
    没报过」是两回事；归因与先例库（Task 41）要按历史同因统计，只存
    「当前还在的差异」的话，消解过的成因永远学不到。

    只处理**本次真正跑出口径**的渠道：某路取数失败 / 没取到账单时它压根
    没跑口径，拿它去消解等于把「没查」当成「查了没问题」。
    """
    _require_schema(conn, DIFF_TABLE)
    resolved: list[str] = []
    stamp = _now()
    for channel in channels:
        alive = seen_paths.get(channel, set())
        eligible = (
            None
            if eligible_paths is None
            else eligible_paths.get(channel, set())
        )
        rows = conn.execute(
            f"SELECT check_path, resolved FROM {DIFF_TABLE} "
            "WHERE period=? AND channel=? AND resolved=0",
            (period, channel),
        ).fetchall()
        for row in rows:
            if row["check_path"] in alive:
                continue
            if eligible is not None and row["check_path"] not in eligible:
                # 本次没跑这条口径：它过没过**未知**，不给它任何结论。
                logger.info(
                    "账期 %s %s 的差异 %s 本次未在重跑范围内（--only-check 未选中），"
                    "保持原状不消解",
                    period,
                    channel,
                    row["check_path"],
                )
                continue
            conn.execute(
                f"UPDATE {DIFF_TABLE} SET resolved=1, resolved_at=?, resolved_run_id=? "
                "WHERE period=? AND channel=? AND check_path=?",
                (stamp, run_id, period, channel, row["check_path"]),
            )
            resolved.append(f"{channel}:{row['check_path']}")
    if resolved:
        logger.info(
            "账期 %s 本次重跑消解 %d 条差异：%s",
            period,
            len(resolved),
            "、".join(resolved),
        )
    return resolved


def count_active_failures(conn: sqlite3.Connection, period: str) -> int:
    """本期**还活着**的差异条数（不含已消解）。

    子集重跑时门禁不能用「本次跑了几条口径、其中几条 fail」来判 ——
    那等于把没重跑的口径当成没问题。这里数的是台账里全量未消解的差异。
    """
    _require_schema(conn, DIFF_TABLE)
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {DIFF_TABLE} WHERE period=? AND resolved=0",
            (period,),
        ).fetchone()[0]
    )



# —— 口径结果表（过没过都记，区分本次产出与沿用）———————————


def upsert_check_results(
    conn: sqlite3.Connection,
    period: str,
    items: Sequence["CheckResultRow"],
    *,
    run_id: str = "",
    origin: str = "this_run",
) -> int:
    """写每条口径的执行结果。覆盖语义 + ``origin`` 标记出处。"""
    if not items:
        return 0
    _require_schema(conn, CHECK_RESULT_TABLE)
    written = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in items:
            conn.execute(
                f"INSERT INTO {CHECK_RESULT_TABLE} ("
                "period,channel,check_path,check_name,operator,status,message,"
                "diff_amount,evidence_total,source_file,first_row_no,origin,run_id,"
                "recorded_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(period,channel,check_path) DO UPDATE SET "
                "check_name=excluded.check_name, operator=excluded.operator, "
                "status=excluded.status, message=excluded.message, "
                "diff_amount=excluded.diff_amount, evidence_total=excluded.evidence_total, "
                "source_file=excluded.source_file, first_row_no=excluded.first_row_no, "
                "origin=excluded.origin, run_id=excluded.run_id, "
                "recorded_at=excluded.recorded_at",
                (
                    period,
                    item.channel,
                    item.check_path,
                    item.check_name,
                    item.operator,
                    item.status,
                    item.message,
                    item.diff_amount,
                    item.evidence_total,
                    item.source_file,
                    item.first_row_no,
                    origin,
                    run_id,
                    _now(),
                ),
            )
            written += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return written


def mark_check_results_carried(
    conn: sqlite3.Connection,
    period: str,
    *,
    channels: Sequence[str],
    keep_keys: set[tuple[str, str]],
) -> int:
    """把范围内、本次没重跑到的口径结果标成「沿用上次」。

    财务科在结论单上看到一条「通过」时，得知道它是**这一分钟刚跑的**还是
    **上周跑完沿用下来的**。两者都不标的话，签字的人无从判断这份结论的
    时效。口径被改后重跑，选中的那几条变「本次产出」、同渠道其余口径
    留「沿用」，这个区分才是实的。
    """
    _require_schema(conn, CHECK_RESULT_TABLE)
    marked = 0
    for channel in channels:
        rows = conn.execute(
            f"SELECT check_path, origin FROM {CHECK_RESULT_TABLE} "
            "WHERE period=? AND channel=? AND origin!=?",
            (period, channel, "carried"),
        ).fetchall()
        for row in rows:
            if (channel, row["check_path"]) in keep_keys:
                continue
            conn.execute(
                f"UPDATE {CHECK_RESULT_TABLE} SET origin='carried' "
                "WHERE period=? AND channel=? AND check_path=?",
                (period, channel, row["check_path"]),
            )
            marked += 1
    return marked


def list_check_results(
    conn: sqlite3.Connection,
    period: str,
    *,
    origin: Optional[str] = None,
) -> list["CheckResultRow"]:
    """读一个账期的口径结果（面板「本次产出 / 沿用」两栏的同源视图）。"""
    _require_schema(conn, CHECK_RESULT_TABLE)
    sql = f"SELECT * FROM {CHECK_RESULT_TABLE} WHERE period=?"
    params: list[Any] = [period]
    if origin:
        sql += " AND origin=?"
        params.append(origin)
    sql += " ORDER BY channel, check_path"
    return [
        CheckResultRow(
            period=r["period"],
            channel=r["channel"],
            check_path=r["check_path"],
            check_name=r["check_name"],
            operator=r["operator"],
            status=r["status"],
            message=r["message"],
            diff_amount=r["diff_amount"],
            evidence_total=r["evidence_total"],
            source_file=r["source_file"],
            first_row_no=r["first_row_no"],
            origin=r["origin"],
            run_id=r["run_id"],
            recorded_at=r["recorded_at"],
        )
        for r in conn.execute(sql, params)
    ]


def count_check_results_by_origin(
    conn: sqlite3.Connection,
    period: str,
) -> dict[str, int]:
    """按出处统计口径条数（面板「沿用 N 条」那一行）。"""
    _require_schema(conn, CHECK_RESULT_TABLE)
    out: dict[str, int] = {}
    for row in conn.execute(
        f"SELECT origin, COUNT(*) AS n FROM {CHECK_RESULT_TABLE} "
        "WHERE period=? GROUP BY origin",
        (period,),
    ):
        out[row["origin"]] = int(row["n"])
    return out


def list_diff_items(
    conn: sqlite3.Connection,
    period: str,
    *,
    disposition: Optional[str] = None,
    include_resolved: bool = True,
    ambiguity_kind: Optional[str] = None,
) -> list[DiffItem]:
    """列一个账期的差异台账。

    ``include_resolved=False`` 只列**还活着**的差异（面板主态与门禁
    计数用这个口径）；默认带上已消解的，供结论单与先例库回查历史。

    ``ambiguity_kind`` 是 Task 25 加的成因筛选（面板「按成因分组」那条腿）。
    传空串 ``""`` 表示**只要口径差异**（``ambiguity_kind=''``），与不传
    这个参数（全部）不是一回事 —— 面板上「其它」那一组必须能被单独点开，
    否则分组数字对不上总数，经纪人会以为台账漏了条。
    """
    _require_schema(conn, DIFF_TABLE)
    sql = f"SELECT * FROM {DIFF_TABLE} WHERE period=?"
    params: list[Any] = [period]
    if disposition:
        sql += " AND disposition=?"
        params.append(disposition)
    if ambiguity_kind is not None:
        sql += " AND ambiguity_kind=?"
        params.append(ambiguity_kind)
    if not include_resolved:
        sql += " AND resolved=0"
    sql += " ORDER BY channel, check_path"
    return [
        DiffItem(
            period=r["period"],
            channel=r["channel"],
            check_path=r["check_path"],
            check_name=r["check_name"],
            status=r["status"],
            message=r["message"],
            diff_amount=r["diff_amount"],
            evidence_total=r["evidence_total"],
            source_file=r["source_file"],
            first_row_no=r["first_row_no"],
            disposition=r["disposition"],
            needs_attribution=bool(r["needs_attribution"]),
            run_id=r["run_id"],
            recorded_at=r["recorded_at"],
            resolved=bool(r["resolved"]),
            resolved_at=r["resolved_at"],
            resolved_run_id=r["resolved_run_id"],
            ambiguity_kind=r["ambiguity_kind"],
        )
        for r in conn.execute(sql, params)
    ]


def count_diff_by_ambiguity(
    conn: sqlite3.Connection,
    period: str,
    *,
    include_resolved: bool = False,
) -> dict[str, int]:
    """按歧义成因分组计数（面板分组视图的数据源）。

    **口径差异（``ambiguity_kind=''``）单列一组**，不混进「不可判定」：
    把查口径的问题算成「成因判不出来」会让不可判定那组虚高，而那正是
    最需要人去推的情形。默认只数还活着的差异（``include_resolved=False``），
    与 :func:`count_pending` 的口径一致。
    """
    _require_schema(conn, DIFF_TABLE)
    sql = (
        f"SELECT ambiguity_kind, COUNT(*) FROM {DIFF_TABLE} "
        "WHERE period=?"
    )
    params: list[Any] = [period]
    if not include_resolved:
        sql += " AND resolved=0"
    sql += " GROUP BY ambiguity_kind"
    counts: dict[str, int] = {}
    for row in conn.execute(sql, params):
        counts[row[0] or ""] = int(row[1])
    return counts


def count_pending(conn: sqlite3.Connection, period: str) -> tuple[int, int]:
    """返回 ``(待确认条数, 待归因条数)``。

    **已消解的差异不计入**：它们这次对齐后消失了，还挂在「待确认」里
    会让本期永远红着 —— 那是重跑把门禁钉死了，人却什么也没做错。
    """
    _require_schema(conn, DIFF_TABLE)
    pending = conn.execute(
        f"SELECT COUNT(*) FROM {DIFF_TABLE} "
        "WHERE period=? AND disposition=? AND resolved=0",
        (period, DISPOSITION_PENDING),
    ).fetchone()[0]
    attribution = conn.execute(
        f"SELECT COUNT(*) FROM {DIFF_TABLE} "
        "WHERE period=? AND needs_attribution=1 AND resolved=0",
        (period,),
    ).fetchone()[0]
    return int(pending), int(attribution)
