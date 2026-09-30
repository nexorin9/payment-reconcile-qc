"""存储层：渠道账单落库（Task 7）与脏行隔离区（Task 11）。

* :mod:`reconq.store.schema` —— 建表与唯一索引
* :mod:`reconq.store.bill_store` —— 幂等落库与重放
* :mod:`reconq.store.quarantine` —— 脏行隔离台账与脏行率判坏
* :mod:`reconq.store.snapshots` —— 契约快照、回归基线与口径变更标记（Task 17）
"""

from __future__ import annotations

from .schema import BILL_TABLE, BILL_TABLE_DDL, connect, init_schema
from .bill_store import (
    CHANNEL_COLUMN_MAP,
    BillRecord,
    UpsertOutcome,
    build_record,
    compute_idempotency_key,
    ingest_channel_rows,
    query_period,
    row_summary,
    upsert_bill_rows,
)
from .quarantine import (
    DEFAULT_DIRTY_RATE_THRESHOLD,
    DIRTY_TABLE,
    REASON_CODES,
    FileScanResult,
    QuarantineRow,
    channel_state,
    count_dirty,
    export_quarantine_csv,
    init_dirty_schema,
    insert_dirty_rows,
    list_dirty,
    scan_channel_dir,
    scan_channel_file,
)
from .snapshots import (
    BASELINE_TABLE,
    BINDING_TABLE,
    NEEDS_RECONFIRM,
    SNAPSHOT_TABLE,
    BaselineRecord,
    PeriodBinding,
    SnapshotRecord,
    bind_period_contract,
    clear_needs_reconfirm,
    compare_with_baseline,
    diff_channel,
    init_snapshot_schema,
    latest_snapshot,
    list_needs_reconfirm,
    list_snapshots,
    load_baseline,
    record_baseline,
    record_snapshot,
)

__all__ = [
    "BASELINE_TABLE",
    "BILL_TABLE",
    "BILL_TABLE_DDL",
    "BINDING_TABLE",
    "CHANNEL_COLUMN_MAP",
    "DEFAULT_DIRTY_RATE_THRESHOLD",
    "DIRTY_TABLE",
    "NEEDS_RECONFIRM",
    "REASON_CODES",
    "SNAPSHOT_TABLE",
    "BaselineRecord",
    "BillRecord",
    "FileScanResult",
    "PeriodBinding",
    "QuarantineRow",
    "SnapshotRecord",
    "UpsertOutcome",
    "bind_period_contract",
    "build_record",
    "channel_state",
    "clear_needs_reconfirm",
    "compare_with_baseline",
    "compute_idempotency_key",
    "connect",
    "count_dirty",
    "diff_channel",
    "export_quarantine_csv",
    "ingest_channel_rows",
    "init_dirty_schema",
    "init_schema",
    "init_snapshot_schema",
    "insert_dirty_rows",
    "latest_snapshot",
    "list_dirty",
    "list_needs_reconfirm",
    "list_snapshots",
    "load_baseline",
    "query_period",
    "record_baseline",
    "record_snapshot",
    "row_summary",
    "scan_channel_dir",
    "scan_channel_file",
    "upsert_bill_rows",
]
