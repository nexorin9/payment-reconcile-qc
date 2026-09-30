"""契约快照、回归基线与「口径已变更」标记（Task 17）。

三张表，一件事：**口径变了之后，账期上已经得出的结论还作数吗？**

- ``contract_snapshots`` —— 每个渠道一份契约的历次指纹与规范化内容。
  回答「这份契约相对上次改了什么」。
- ``check_baselines`` —— 固定样例账期上各 check 的**结论基线**。
  回答「改完口径重跑，会不会把结论翻过来」。
- ``period_contract_bindings`` —— 账期 × 渠道 当时跑的是哪一版契约。
  指纹对不上就把该账期标 ``needs_reconfirm``，**不沿用旧确认结论**。

为什么必须是第三张表：``bill_rows``（Task 7）与 ``dirty_rows``（Task 11）
记的是「数据长什么样」，它们与契约版本无关 —— 同一批账单换一版契约重跑，
账单表一行都不会变，变的是**结论**。若不把「这期结论是按哪一版口径得出的」
落库，那么口径一改，面板上那条「已确认」就变成了一句没有出处的断言。
财务科被叫回来重看两百条差异时，第一个问题一定是「口径到底改了没有」。

与 Task 18 的关系：``period_contract_bindings`` 只负责**标记**
``needs_reconfirm``，状态机本身（未扫描→扫描中→待确认→已确认/已存疑）在
``engine/period.py``。本表是那台状态机的一格外部输入，不越界实现迁移规则。

源产品对应：参考实现 ``github_ref/soda-core/soda-core/src/soda_core/cli/cli.py``
的 ``contract publish``（``:338``）与 ``contract fetch``（``:390``）把契约整体
推到/拉回远端注册表，契约在源产品里**有版本**；但版本存在远端，本地不留副本，
更没有「本地这份相对远端上次那版改了什么」的入口。本产品反过来：指纹与快照
**全在本地账单库里**，随备份一起走，不依赖任何远端服务。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

from ..contracts.fingerprint import (
    CanonicalContract,
    ContractDiff,
    ContractFingerprint,
    compare_baseline,
    diff_fingerprints,
    fingerprint_source,
    impact_scope,
    ImpactScope,
)
from ..errors import ReconqError

__all__ = [
    "SNAPSHOT_TABLE",
    "BASELINE_TABLE",
    "BINDING_TABLE",
    "NEEDS_RECONFIRM",
    "SnapshotRecord",
    "BaselineRecord",
    "PeriodBinding",
    "init_snapshot_schema",
    "record_snapshot",
    "latest_snapshot",
    "list_snapshots",
    "diff_channel",
    "record_baseline",
    "load_baseline",
    "compare_with_baseline",
    "bind_period_contract",
    "list_needs_reconfirm",
    "clear_needs_reconfirm",
]


SNAPSHOT_TABLE = "contract_snapshots"
BASELINE_TABLE = "check_baselines"
BINDING_TABLE = "period_contract_bindings"

#: 账期因口径变更而需要重新确认的标记值。
NEEDS_RECONFIRM = "needs_reconfirm"


_SNAPSHOT_DDL = f"""
CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
    snap_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    channel        TEXT    NOT NULL,
    digest         TEXT    NOT NULL,
    check_digest   TEXT    NOT NULL,
    canonical_json TEXT    NOT NULL,
    source_path    TEXT    NOT NULL DEFAULT '',
    version        TEXT    NOT NULL DEFAULT '',
    recorded_at    TEXT    NOT NULL DEFAULT ''
)
"""

_SNAPSHOT_INDEXES = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshot_digest ON {SNAPSHOT_TABLE} (channel, digest)",
    f"CREATE INDEX IF NOT EXISTS ix_snapshot_channel ON {SNAPSHOT_TABLE} (channel, snap_id)",
)

#: 基线键 = 账期 + 渠道 + check path。同一账期重录基线是覆盖语义
#: （:func:`record_baseline` 走 ON CONFLICT DO UPDATE），不是追加 ——
#: 基线回答的是「上次跑出来是什么」，堆历史会让「上次」变得不确定。
_BASELINE_DDL = f"""
CREATE TABLE IF NOT EXISTS {BASELINE_TABLE} (
    baseline_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT    NOT NULL,
    channel         TEXT    NOT NULL,
    contract_digest TEXT    NOT NULL DEFAULT '',
    check_path      TEXT    NOT NULL,
    check_name      TEXT    NOT NULL DEFAULT '',
    operator        TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL,
    diff_amount     TEXT,
    evidence_total  INTEGER NOT NULL DEFAULT 0,
    recorded_at     TEXT    NOT NULL DEFAULT ''
)
"""

_BASELINE_INDEXES = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_baseline_identity "
    f"ON {BASELINE_TABLE} (period, channel, check_path)",
    f"CREATE INDEX IF NOT EXISTS ix_baseline_period ON {BASELINE_TABLE} (period, channel)",
)

#: 账期 × 渠道当时跑的是哪一版契约。口径指纹变了 → needs_reconfirm。
_BINDING_DDL = f"""
CREATE TABLE IF NOT EXISTS {BINDING_TABLE} (
    period         TEXT    NOT NULL,
    channel        TEXT    NOT NULL,
    contract_digest TEXT   NOT NULL,
    check_digest   TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'scanned',
    note           TEXT    NOT NULL DEFAULT '',
    bound_at       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (period, channel)
)
"""

_BINDING_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_binding_period ON {BINDING_TABLE} (period, status)",
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def init_snapshot_schema(conn: sqlite3.Connection) -> None:
    """建三张表与索引。幂等：重复调用不报错。"""
    for ddl in (_SNAPSHOT_DDL, _BASELINE_DDL, _BINDING_DDL):
        conn.execute(ddl)
    for statement in (*_SNAPSHOT_INDEXES, *_BASELINE_INDEXES, *_BINDING_INDEXES):
        conn.execute(statement)


def _require_schema(conn: sqlite3.Connection, table: str, code: str, hint: str) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        raise ReconqError(code, hint)


# —— 契约快照 ——————————————————————————————————————————


@dataclass(frozen=True)
class SnapshotRecord:
    """一条契约快照。``canonical`` 缺失时从 ``canonical_json`` 还原。"""

    channel: str
    digest: str
    check_digest: str
    canonical_json: str
    source_path: str = ""
    version: str = ""
    recorded_at: str = ""

    def to_fingerprint(self) -> ContractFingerprint:
        """把快照还原成可参与 diff 的指纹。"""
        payload = json.loads(self.canonical_json)
        canonical = _canonical_from_json(payload)
        return ContractFingerprint(
            digest=self.digest,
            check_digest=self.check_digest,
            canonical=canonical,
            origin=self.source_path or self.channel,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "digest": self.digest,
            "check_digest": self.check_digest,
            "source_path": self.source_path,
            "version": self.version,
            "recorded_at": self.recorded_at,
        }


def _canonical_from_json(payload: Any) -> CanonicalContract:
    """由 :meth:`CanonicalContract.to_canonical` 的产物还原。

    刻意**不走** :func:`reconq.contracts.fingerprint.canonical_source`：
    快照里存的就是规范化后的形态（含已解析的 ``${period}``），从 YAML 文本
    再解析一遍只会把同一份东西绕远路。
    """
    from ..contracts.fingerprint import CheckSpec, ColumnSpec, TableSpec

    def _check(node: dict[str, Any]) -> CheckSpec:
        return CheckSpec(
            type=str(node.get("type") or ""),
            path=tuple(str(s) for s in (node.get("path") or [])),
            properties=tuple(sorted((node.get("properties") or {}).items(), key=lambda kv: kv[0])),
            name=str(node.get("name") or ""),
        )

    def _column(node: dict[str, Any]) -> ColumnSpec:
        return ColumnSpec(
            name=str(node.get("name") or ""),
            data_type=str(node.get("data_type") or "string"),
            required=bool(node.get("required", False)),
            description=str(node.get("description") or ""),
            checks=tuple(_check(c) for c in (node.get("checks") or [])),
        )

    def _table(node: dict[str, Any]) -> TableSpec:
        return TableSpec(
            name=str(node.get("name") or ""),
            location=str(node.get("location") or ""),
            description=str(node.get("description") or ""),
            columns=tuple(_column(c) for c in (node.get("columns") or [])),
            checks=tuple(_check(c) for c in (node.get("checks") or [])),
        )

    return CanonicalContract(
        tables=tuple(_table(t) for t in (payload.get("tables") or [])),
        checks=tuple(_check(c) for c in (payload.get("checks") or [])),
        meta=tuple(sorted((payload.get("meta") or {}).items(), key=lambda kv: kv[0])),
        variables=tuple(sorted((payload.get("variables") or {}).items(), key=lambda kv: kv[0])),
    )


def record_snapshot(
    conn: sqlite3.Connection,
    channel: str,
    fingerprint: ContractFingerprint,
    *,
    source_path: str = "",
    version: str = "",
) -> SnapshotRecord:
    """把当前契约记为该渠道的快照。

    幂等：同一 (渠道, 指纹) 重复记录不会产生第二行 —— 反复 ``reconq
    contract snapshot`` 不会把快照表撑成流水账。内容未变时**只更新**
    ``source_path``/``version``，``recorded_at`` 保持首次记录的时刻：
    「这份契约是什么时候定下来的」不该被一次重复运行改写。
    """
    _require_schema(
        conn,
        SNAPSHOT_TABLE,
        "CONTRACT_SNAPSHOT_NOT_INITIALIZED",
        "契约快照表尚未建表；先跑一次 reconq contract snapshot 或用 "
        "reconq.store.snapshots.init_snapshot_schema() 建表",
    )
    canonical_json = json.dumps(
        fingerprint.canonical.to_canonical(with_label=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    existing = conn.execute(
        f"SELECT snap_id, recorded_at FROM {SNAPSHOT_TABLE} WHERE channel=? AND digest=?",
        (channel, fingerprint.digest),
    ).fetchone()
    if existing is not None:
        conn.execute(
            f"UPDATE {SNAPSHOT_TABLE} SET source_path=?, version=? WHERE snap_id=?",
            (source_path, version, existing["snap_id"]),
        )
        return _load_snapshot(conn, int(existing["snap_id"]))

    conn.execute(
        f"INSERT INTO {SNAPSHOT_TABLE} "
        "(channel, digest, check_digest, canonical_json, source_path, version, recorded_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            channel,
            fingerprint.digest,
            fingerprint.check_digest,
            canonical_json,
            source_path,
            version,
            _now(),
        ),
    )
    return _load_snapshot(conn, int(conn.execute("SELECT last_insert_rowid() AS rid").fetchone()["rid"]))


def _load_snapshot(conn: sqlite3.Connection, snap_id: int) -> SnapshotRecord:
    row = conn.execute(
        f"SELECT * FROM {SNAPSHOT_TABLE} WHERE snap_id=?", (snap_id,)
    ).fetchone()
    if row is None:
        raise ReconqError("CONTRACT_SNAPSHOT_NOT_FOUND", f"契约快照不存在：snap_id={snap_id}")
    return SnapshotRecord(
        channel=row["channel"],
        digest=row["digest"],
        check_digest=row["check_digest"],
        canonical_json=row["canonical_json"],
        source_path=row["source_path"],
        version=row["version"],
        recorded_at=row["recorded_at"],
    )


def latest_snapshot(conn: sqlite3.Connection, channel: str) -> Optional[SnapshotRecord]:
    """该渠道最近一次记录的快照；一条都没有时返回 None。"""
    _require_schema(
        conn,
        SNAPSHOT_TABLE,
        "CONTRACT_SNAPSHOT_NOT_INITIALIZED",
        "契约快照表尚未建表；先跑一次 reconq contract snapshot",
    )
    row = conn.execute(
        f"SELECT snap_id FROM {SNAPSHOT_TABLE} WHERE channel=? ORDER BY snap_id DESC LIMIT 1",
        (channel,),
    ).fetchone()
    return _load_snapshot(conn, int(row["snap_id"])) if row else None


def list_snapshots(conn: sqlite3.Connection, channel: Optional[str] = None) -> list[SnapshotRecord]:
    """列出快照历史（新的在前）。"""
    _require_schema(
        conn,
        SNAPSHOT_TABLE,
        "CONTRACT_SNAPSHOT_NOT_INITIALIZED",
        "契约快照表尚未建表；先跑一次 reconq contract snapshot",
    )
    if channel:
        rows = conn.execute(
            f"SELECT snap_id FROM {SNAPSHOT_TABLE} WHERE channel=? ORDER BY snap_id DESC",
            (channel,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT snap_id FROM {SNAPSHOT_TABLE} ORDER BY snap_id DESC"
        ).fetchall()
    return [_load_snapshot(conn, int(r["snap_id"])) for r in rows]


def diff_channel(
    conn: sqlite3.Connection,
    channel: str,
    contract_path: str,
) -> tuple[Optional[SnapshotRecord], ContractDiff, ImpactScope]:
    """对比**工作区当前契约**与该渠道**最近一次快照**。

    两条腿（``reconq contract diff`` 的两种跑法都走这里）：

    - 该渠道还没记过快照 → 交出空差异 + ``first_record=True`` 的影响面，
      提示「这是首次记录，先落快照再改才有得比」；
    - 指纹与快照一致 → 空差异，``requires_reconfirm`` 为 False。

    有差异时返回的影响面已按「只改标签 / 真改口径」区分，调用方据此决定
    要不要把账期打回待确认。
    """
    if not os.path.isfile(contract_path):
        raise ReconqError(
            "CONTRACT_MISSING_KEY",
            f"渠道 {channel} 的契约文件不存在：{contract_path}",
            channel=channel,
            yaml_path=contract_path,
        )
    current = fingerprint_source(contract_path)
    previous = latest_snapshot(conn, channel)
    if previous is None:
        empty = diff_fingerprints(current, current, channel=channel)
        return (
            None,
            empty,
            ImpactScope(
                channel=channel,
                reason=f"渠道 {channel} 还没有契约快照，本次是首次记录；"
                "先落快照，之后改口径才有得比",
                diff=empty,
            ),
        )
    diff = diff_fingerprints(previous.to_fingerprint(), current, channel=channel)
    return previous, diff, impact_scope(diff, channel=channel)


# —— 回归基线 ——————————————————————————————————————————


@dataclass(frozen=True)
class BaselineRecord:
    """一条 check 的基线结论。"""

    period: str
    channel: str
    check_path: str
    check_name: str = ""
    operator: str = ""
    status: str = ""
    diff_amount: Optional[str] = None
    evidence_total: int = 0
    contract_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_path": self.check_path,
            "check_name": self.check_name,
            "operator": self.operator,
            "status": self.status,
            "diff_amount": self.diff_amount,
            "evidence_total": self.evidence_total,
        }


def record_baseline(
    conn: sqlite3.Connection,
    period: str,
    channel: str,
    results: Sequence[Any],
    *,
    contract_digest: str = "",
) -> int:
    """把某账期某渠道的 check 结果记为基线，返回写入条数。

    整批一个事务：基线是「这次跑出来是什么」的快照，**半截基线比没有基线
    更坏** —— 它会让对拍把「没记过」误报成「结论翻转」。
    """
    _require_schema(
        conn,
        BASELINE_TABLE,
        "CONTRACT_BASELINE_NOT_INITIALIZED",
        "回归基线表尚未建表；用 reconq.store.snapshots.init_snapshot_schema() 建表",
    )
    if not period:
        raise ReconqError("CONTRACT_BASELINE_NOT_INITIALIZED", "记录回归基线必须给定账期")
    now = _now()
    conn.execute("BEGIN")
    try:
        for res in results:
            conn.execute(
                f"INSERT INTO {BASELINE_TABLE} "
                "(period, channel, contract_digest, check_path, check_name, operator, "
                "status, diff_amount, evidence_total, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(period, channel, check_path) DO UPDATE SET "
                "contract_digest=excluded.contract_digest, check_name=excluded.check_name, "
                "operator=excluded.operator, status=excluded.status, "
                "diff_amount=excluded.diff_amount, evidence_total=excluded.evidence_total, "
                "recorded_at=excluded.recorded_at",
                (
                    period,
                    channel,
                    contract_digest,
                    res.check_path,
                    res.check_name,
                    res.operator,
                    res.status,
                    res.diff_amount,
                    getattr(res, "evidence_total", 0),
                    now,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(results)


def load_baseline(
    conn: sqlite3.Connection,
    period: str,
    channel: str,
) -> Optional[dict[str, Any]]:
    """读某账期某渠道的基线；没记过返回 None。"""
    _require_schema(
        conn,
        BASELINE_TABLE,
        "CONTRACT_BASELINE_NOT_INITIALIZED",
        "回归基线表尚未建表；用 reconq.store.snapshots.init_snapshot_schema() 建表",
    )
    rows = conn.execute(
        f"SELECT * FROM {BASELINE_TABLE} WHERE period=? AND channel=?",
        (period, channel),
    ).fetchall()
    if not rows:
        return None
    head = rows[0]
    return {
        "period": head["period"],
        "channel": head["channel"],
        "contract_digest": head["contract_digest"],
        "recorded_at": head["recorded_at"],
        "results": {
            r["check_path"]: {
                "check_name": r["check_name"],
                "operator": r["operator"],
                "status": r["status"],
                "diff_amount": r["diff_amount"],
                "evidence_total": r["evidence_total"],
            }
            for r in rows
        },
    }


def compare_with_baseline(
    conn: sqlite3.Connection,
    period: str,
    channel: str,
    results: Sequence[Any],
) -> Any:
    """把本次结果与基线对拍（:func:`~reconq.contracts.fingerprint.compare_baseline`）。"""
    baseline = load_baseline(conn, period, channel)
    return compare_baseline(baseline, results, period=period)


# —— 账期 × 契约绑定 ————————————————————————————————————


@dataclass(frozen=True)
class PeriodBinding:
    """一个账期在某渠道上跑的是哪一版契约。"""

    period: str
    channel: str
    contract_digest: str
    check_digest: str = ""
    status: str = "scanned"
    note: str = ""
    bound_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "contract_digest": self.contract_digest,
            "check_digest": self.check_digest,
            "status": self.status,
            "note": self.note,
            "bound_at": self.bound_at,
        }


def bind_period_contract(
    conn: sqlite3.Connection,
    period: str,
    channel: str,
    fingerprint: ContractFingerprint,
    *,
    status: str = "scanned",
    note: str = "",
) -> PeriodBinding:
    """登记「本期这一路是按哪一版口径跑的」。

    若该账期此前已绑定过**不同**的口径指纹，且调用方没有显式确认过
    （``status`` 仍为 ``scanned``），这里把状态改写为 ``needs_reconfirm``：
    口径变了，上一版口径下的差异结论不再自动有效，需要人重新确认。

    显式传入的状态（如 ``reconfirmed``）**不被覆盖** —— 状态机的推进权
    在人手里（Task 18），本函数只在「未确认过」时补一个标记。
    """
    _require_schema(
        conn,
        BINDING_TABLE,
        "PERIOD_BINDING_NOT_INITIALIZED",
        "账期契约绑定表尚未建表；用 reconq.store.snapshots.init_snapshot_schema() 建表",
    )
    previous = conn.execute(
        f"SELECT * FROM {BINDING_TABLE} WHERE period=? AND channel=?",
        (period, channel),
    ).fetchone()
    final_status = status
    final_note = note
    if previous is not None:
        if previous["check_digest"] != fingerprint.check_digest:
            # 口径真的变了 → 打回待确认（除非调用方已显式给出别的状态）
            if status == "scanned":
                final_status = NEEDS_RECONFIRM
                final_note = note or (
                    f"口径指纹已从 {previous['check_digest'][:12]} 变为 "
                    f"{fingerprint.check_digest[:12]}，本账期结论需重新确认"
                )
        elif previous["status"] not in ("scanned", NEEDS_RECONFIRM):
            # 口径没变，而这一格已被人工确认过（reconfirmed 等）：
            # **保留**那个状态。日常重跑不该把主管已经点过的确认抹回
            # 「刚扫完」—— 否则每重跑一次，财务科就得重新确认一次。
            final_status = previous["status"]
            final_note = previous["note"] or note
    conn.execute(
        f"INSERT INTO {BINDING_TABLE} "
        "(period, channel, contract_digest, check_digest, status, note, bound_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(period, channel) DO UPDATE SET "
        "contract_digest=excluded.contract_digest, check_digest=excluded.check_digest, "
        "status=excluded.status, note=excluded.note, bound_at=excluded.bound_at",
        (
            period,
            channel,
            fingerprint.digest,
            fingerprint.check_digest,
            final_status,
            final_note,
            _now(),
        ),
    )
    return PeriodBinding(
        period=period,
        channel=channel,
        contract_digest=fingerprint.digest,
        check_digest=fingerprint.check_digest,
        status=final_status,
        note=final_note,
        bound_at=_now(),
    )


def list_needs_reconfirm(
    conn: sqlite3.Connection,
    period: Optional[str] = None,
) -> list[PeriodBinding]:
    """列出因口径变更而需要重新确认的账期 × 渠道。"""
    _require_schema(
        conn,
        BINDING_TABLE,
        "PERIOD_BINDING_NOT_INITIALIZED",
        "账期契约绑定表尚未建表；用 reconq.store.snapshots.init_snapshot_schema() 建表",
    )
    if period:
        rows = conn.execute(
            f"SELECT * FROM {BINDING_TABLE} WHERE period=? AND status=? ORDER BY channel",
            (period, NEEDS_RECONFIRM),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM {BINDING_TABLE} WHERE status=? ORDER BY period, channel",
            (NEEDS_RECONFIRM,),
        ).fetchall()
    return [
        PeriodBinding(
            period=r["period"],
            channel=r["channel"],
            contract_digest=r["contract_digest"],
            check_digest=r["check_digest"],
            status=r["status"],
            note=r["note"],
            bound_at=r["bound_at"],
        )
        for r in rows
    ]


def clear_needs_reconfirm(
    conn: sqlite3.Connection,
    period: str,
    channel: str,
    *,
    status: str = "reconfirmed",
    note: str = "",
) -> None:
    """人工确认后清掉标记。

    只接受显式调用 —— 没有任何自动路径会走到这里（模型、规则、重跑都
    不行）。口径变更后「结论要重看一遍」这件事只有人能决定。
    """
    _require_schema(
        conn,
        BINDING_TABLE,
        "PERIOD_BINDING_NOT_INITIALIZED",
        "账期契约绑定表尚未建表；用 reconq.store.snapshots.init_snapshot_schema() 建表",
    )
    cur = conn.execute(
        f"UPDATE {BINDING_TABLE} SET status=?, note=?, bound_at=? WHERE period=? AND channel=?",
        (status, note, _now(), period, channel),
    )
    if cur.rowcount == 0:
        raise ReconqError(
            "PERIOD_BINDING_NOT_INITIALIZED",
            f"账期 {period} 渠道 {channel} 没有契约绑定记录，无法确认",
        )
