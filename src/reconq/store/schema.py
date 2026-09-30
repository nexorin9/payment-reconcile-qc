"""渠道账单表结构（Task 7）。

六路账单行最终都落到**同一张表**：下游的对齐器（Task 21+）、勾稽 check
（Task 14）、差异台账与结论单都只认这一张表，渠道差异在这一层之前就被
契约列名归一掉了。

三处刻意为之的设计
------------------

1. **金额一律以 ``TEXT`` 存定点字面量**。SQLite 的 ``REAL`` 是 IEEE754
   双精度，`0.1 + 0.2` 落库再读出来就是 ``0.30000000000000004``，对账差
   一分钱的经典事故就出在这里。存 ``"128.00"``，读时 ``Decimal(str)``
   还原，全程不经过二进制浮点。
2. **唯一索引 = (渠道, 账期, 账单日期, 渠道单号)**。这正是 spec 里的
   幂等键「账期 + 渠道 + 账单日期」加上实体标识：同一笔渠道流水被重复
   拉取、重复导入、并发重跑，都撞在这一个键上被数据库挡掉，而不是靠应用
   层先去查一遍再决定写不写（那在并发下必然漏）。
3. **时间戳带偏移**。渠道账单给本地时间、HIS 给库时区，存前统一归一到会
   话时区（Task 4）再写 ISO 串，跨日切分与跨账期判定才有稳定依据。

源产品对应：参考实现里数据源只负责把行取出来交给 check 执行器
（``data_source_connection.py:393`` 的逐行流式），并不负责落库与去重；
去重语义落在它「同一份契约、同一个 data source、同一时间窗」的扫描幂等上。
本产品把它显式化成一��带唯一索引的表，让「重复拉取不重复入库」成为数据库
级保证而不是调用约定。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

__all__ = [
    "BILL_TABLE",
    "BILL_TABLE_DDL",
    "BILL_INDEXES",
    "init_schema",
    "connect",
]


BILL_TABLE = "bill_rows"

#: 渠道账单表。列名用契约规范名（``contracts/<channel>.yml`` 的 columns）。
#: ``amount_text`` 与 ``trade_time`` 存字符串而非数值/时间类型，理由见模块头。
BILL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS bill_rows (
    ch_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    channel               TEXT    NOT NULL,
    period                TEXT    NOT NULL,
    bill_date             TEXT    NOT NULL,
    channel_order_no      TEXT    NOT NULL,
    his_order_no          TEXT    NOT NULL DEFAULT '',
    amount_text           TEXT    NOT NULL DEFAULT '0',
    refund_text           TEXT,
    trade_time            TEXT,
    trade_type            TEXT    NOT NULL DEFAULT '',
    department            TEXT    NOT NULL DEFAULT '',
    settle_type           TEXT    NOT NULL DEFAULT '',
    source_file           TEXT    NOT NULL DEFAULT '',
    source_row_no         INTEGER,
    bill_date_inferred    INTEGER NOT NULL DEFAULT 0,
    idempotency_key       TEXT    NOT NULL DEFAULT '',
    ingested_at           TEXT    NOT NULL DEFAULT ''
)
"""

#: 幂等唯一索引。ON CONFLICT DO NOTHING 撞的就是它。
BILL_INDEXES = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_bill_identity
        ON bill_rows (channel, period, bill_date, channel_order_no)
    """,
    # 重放时按幂等键整批查；按账期查是面板与结论单的常规入口。
    """
    CREATE INDEX IF NOT EXISTS ix_bill_period
        ON bill_rows (period, channel)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_bill_idempotency
        ON bill_rows (idempotency_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_bill_his_order
        ON bill_rows (his_order_no)
    """,
    # 连坐追查（Task 24）的取数索引：按 (单号, 账期) 复合键定位。
    # 复合顺序刻意是「单号在前、账期在后」：连坐查询的入口永远是
    # 「这个单号在哪些账期出现过」，用 (账期, 单号) 会让单号退化成过滤条件、
    # 每期各走一次全表 —— 六路几十万行下就是几分钟。
    # 另一个理由：单号跨期重复是常态，单值索引在构造时必须丢掉后面的行，
    # 而丢掉的恰恰是「同一笔被两期各认领一次」这条结论的直接证据。
    #
    # **索引列上声明 COLLATE NOCASE** 不可省：列默认是 BINARY 排序规则，
    # 而连坐查询按归一后的键（NFKC + casefold）比大小写不敏感的单号，
    # WHERE 上写 ``= ? COLLATE NOCASE`` 时 SQLite 认为索引的排序规则不匹配，
    # 复合索引明明建了却一次都用不上、查询计划退回按账期扫。排序规则写在
    # 索引定义里，两边才对得上。
    """
    CREATE INDEX IF NOT EXISTS ix_bill_order_period
        ON bill_rows (channel_order_no COLLATE NOCASE, period)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_bill_his_order_period
        ON bill_rows (his_order_no COLLATE NOCASE, period)
    """,
)


def connect(path: str) -> sqlite3.Connection:
    """打开账单库连接。

    ``isolation_level=None`` 关掉 sqlite3 的隐式事务：本产品的写入一律由
    :func:`reconq.store.bill_store.upsert_bill_rows` 显式 ``BEGIN`` /
    ``COMMIT`` / ``ROLLBACK`` 圈定，不让驱动替我们决定半截落库。
    """
    if path != ":memory:":
        import os

        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection, *, extra_statements: Optional[tuple[str, ...]] = None) -> None:
    """建表与建索引。幂等：重复调用不报错。"""
    conn.execute(BILL_TABLE_DDL)
    for statement in BILL_INDEXES:
        conn.execute(statement)
    for statement in extra_statements or ():
        conn.execute(statement)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """``sqlite3.Row`` → 普通 dict（金额仍是字符串，由调用方决定是否还原）。"""
    return {key: row[key] for key in row.keys()}
