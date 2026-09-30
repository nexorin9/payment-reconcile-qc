"""渠道账单幂等落库（Task 7）。

幂等键与实体标识
----------------
spec 的幂等维度是「**账期 + 渠道 + 账单日期**」。这条链路上有两层键，
各管一件事：

* **批级幂等键** ``period|channel|bill_date`` —— 描述「这一批是哪一天的
  哪一路账单」，写进每行的 ``idempotency_key`` 列，供重放与运行摘要按批
  统计；
* **行级唯一键** ``(channel, period, bill_date, channel_order_no)`` ——
  数据库唯一索引，``INSERT ... ON CONFLICT DO NOTHING`` 撞的就是它。

两层缺一不可：只有批级键，批内重复行会重复入库；只有行级键，运营侧无法
回答「这一批到底导进去没有」。

金额为什么存文本
----------------
SQLite 的 ``REAL`` 是 IEEE754 双精度。``0.1 + 0.2`` 落库再读出来是
``0.30000000000000004``，六路账期跑下来就是一堆「差一分钱」的单边账，
而这一分钱永远查不出来源。本产品全程 ``Decimal``，落库写定点字面量
``"128.00"``，读取时 ``Decimal(str)`` 还原，不经过二进制浮点。

事务边界
--------
一次 ``upsert_bill_rows`` = 一个事务。任一行触发异常，整批回滚，
**不产生半截落库**。原因很实际：财务科在扫描中途 Ctrl-C 后重跑，如果
第一批进了库、第二批没进，对齐器就会拿一个「半个账期」去对，
产出的差异结论比不出结论更危险。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional, Sequence

from ..engine.checks import BillRow
from ..errors import ReconqError
from ..sources.timezone import PeriodWindow, parse_session_timezone, to_session_tz

__all__ = [
    "CHANNEL_COLUMN_MAP",
    "ChannelColumns",
    "BillRecord",
    "UpsertOutcome",
    "compute_idempotency_key",
    "parse_trade_time",
    "to_amount_text",
    "build_record",
    "upsert_bill_rows",
    "ingest_channel_rows",
    "query_period",
    "row_summary",
]

logger = logging.getLogger("reconq.bill_store")


#: 各路渠道到**统一落库列**的列名映射。
#:
#: 键是契约规范列名（``contracts/<channel>.yml`` 的 ``columns[].name``）；
#: 落库时按这张表把六路的差异抹平，让下游对齐器只认一套列。
#: 缺列一律留空字符串，**不猜**：某路账单本来就没有 HIS 单号，那是「渠道有
#: HIS 无」这种单边账的成因之一，落库时补一个假单号会把成因掩盖掉。
CHANNEL_COLUMN_MAP: dict[str, dict[str, str]] = {
    "wechat": {
        "order_no": "out_trade_no",
        "secondary_no": "transaction_id",
        "his_order_no": "",
        "amount": "amount",
        "refund": "refund",
        "trade_time": "success_time",
        "trade_type": "trade_state",
        "department": "",
        "settle_type": "payer",
    },
    "alipay": {
        "order_no": "out_trade_no",
        "secondary_no": "trade_no",
        "his_order_no": "",
        "amount": "amount",
        "refund": "",
        "trade_time": "trade_time",
        "trade_type": "biz_type",
        "department": "",
        "settle_type": "income_expense",
    },
    "unionpay": {
        "order_no": "out_trade_no",
        "secondary_no": "trade_no",
        "his_order_no": "",
        "amount": "amount",
        "refund": "",
        "trade_time": "trade_time",
        "trade_type": "is_refund",
        "department": "",
        "settle_type": "settle_type",
    },
    "self_service": {
        "order_no": "out_trade_no",
        "secondary_no": "terminal_no",
        "his_order_no": "",
        "amount": "amount",
        "refund": "",
        "trade_time": "pay_time",
        "trade_type": "is_refund",
        "department": "",
        "settle_type": "settle_type",
    },
    "his": {
        "order_no": "his_order_no",
        "secondary_no": "charge_id",
        "his_order_no": "his_order_no",
        "amount": "amount",
        "refund": "",
        "trade_time": "charge_time",
        "trade_type": "is_refund",
        "department": "department",
        "settle_type": "settle_type",
    },
    "medical_ins": {
        "order_no": "settlement_no",
        "secondary_no": "settlement_no",
        "his_order_no": "settlement_no",
        "amount": "total_amount",
        "refund": "",
        "trade_time": "settle_time",
        "trade_type": "settle_type",
        "department": "",
        "settle_type": "settle_type",
    },
}


class ChannelColumns(dict):
    """``CHANNEL_COLUMN_MAP[channel]``，缺失渠道抛 ``CHANNEL_UNKNOWN``。"""


@dataclass
class BillRecord:
    """一行待落库的账单，已完成渠道列名归一与金额定点化。"""

    channel: str
    period: str
    bill_date: str
    channel_order_no: str
    his_order_no: str = ""
    amount: Decimal = Decimal("0")
    refund: Optional[Decimal] = None
    trade_time: Optional[datetime] = None
    trade_type: str = ""
    department: str = ""
    settle_type: str = ""
    source_file: str = ""
    source_row_no: Optional[int] = None
    #: 账单日期缺失、由交易时间或账期边界回落而来 —— 打了标就不算「假日期」
    bill_date_inferred: bool = False
    idempotency_key: str = ""

    def amount_text(self) -> str:
        return str(self.amount)

    def refund_text(self) -> Optional[str]:
        return None if self.refund is None else str(self.refund)

    def to_tuple(self) -> tuple[Any, ...]:
        """落库参数元组（不含幂等键与入库时刻），列序与 DDL 严格一致。

        ``idempotency_key`` 与 ``ingested_at`` 由 :func:`upsert_bill_rows`
        按批统一补上——同一批的键是批级属性，不该由每一行各自带一份。
        """
        return (
            self.channel,
            self.period,
            self.bill_date,
            self.channel_order_no,
            self.his_order_no,
            self.amount_text(),
            self.refund_text(),
            self.trade_time.isoformat() if self.trade_time else None,
            self.trade_type,
            self.department,
            self.settle_type,
            self.source_file,
            self.source_row_no,
            1 if self.bill_date_inferred else 0,
        )


@dataclass
class UpsertOutcome:
    """一次落库的结果三元组 + 证据。"""

    inserted: int
    skipped_duplicates: int
    total_amount: str
    idempotency_key: str = ""
    bill_date: str = ""
    channel: str = ""
    period: str = ""
    inferred_dates: int = 0
    detail: str = ""

    def as_tuple(self) -> tuple[int, int, str]:
        """spec 要求的 ``(inserted, skipped_duplicates, total_amount)``。"""
        return (self.inserted, self.skipped_duplicates, self.total_amount)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "period": self.period,
            "bill_date": self.bill_date,
            "idempotency_key": self.idempotency_key,
            "inserted": self.inserted,
            "skipped_duplicates": self.skipped_duplicates,
            "total_amount": self.total_amount,
            "inferred_dates": self.inferred_dates,
            "detail": self.detail,
        }


def compute_idempotency_key(period: str, channel: str, bill_date: str) -> str:
    """批级幂等键：``period|channel|bill_date``。"""
    return f"{period}|{channel}|{bill_date}"


def to_amount_text(value: Any, *, channel: str = "", row_ref: Any = None) -> Decimal:
    """把任意来源的金额归一成 ``Decimal``；**不接受 float 语义**。

    float 入参会被拒绝而不是静默接受：``0.1`` 在二进制里是
    ``0.1000000000000000055511151231257827``，一旦以 float 进来，后面
    无论怎么 quantize 都追不回原值。宁可让调用方在取数阶段就崩。
    """
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, float):
        raise ReconqError(
            "AMOUNT_UNPARSABLE",
            f"金额以 float 入库被拒绝：{value!r}；"
            f"请在取数阶段就解析成 Decimal 定点数（float 进来就再也追不回去了）",
            channel=channel or None,
            row_ref=row_ref,
        )
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ReconqError(
            "AMOUNT_UNPARSABLE",
            f"金额无法解析为定点数：{value!r}",
            channel=channel or None,
            row_ref=row_ref,
        ) from exc


#: 渠道账单里的时间写法：``2026-09-07 09:12:03`` / ``2026-09-07T09:12:03+08:00``
#: / ``2026/09/07 09:12:03``。全部按**无偏移本地时间**理解，偏移由会话时区补。
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y%m%d%H%M%S",
)


def parse_trade_time(
    text: str,
    *,
    tz: Optional[Any] = None,
    channel: str = "",
    row_ref: Any = None,
) -> Optional[datetime]:
    """把渠道账单的交易时间解析成**带偏移**的 ``datetime``。

    无偏移的按会话时区（Task 4 的 ``parse_session_timezone``）补上；带偏移的
    转成同一时区。存进库的时间戳因此永远带偏移，跨日切分判定不依赖「读库
    时机器在哪个时区」这种环境假设。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    resolved = tz or to_session_tz(None)
    candidate = raw.replace("/", "-")
    # ISO 8601 的 UTC 写作尾随 ``Z``，``fromisoformat`` 在 3.11 之前不认。
    # 渠道账单与 HIS 导出里 ``...Z`` 并不罕见，先统一改写成 ``+00:00``。
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in _TIME_FORMATS:
            try:
                parsed = datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ReconqError(
            "TIMESTAMP_UNPARSABLE",
            f"交易时间无法解析：{raw!r}；支持 ISO 8601 与 "
            f"{'/'.join(_TIME_FORMATS)}；请让导出方按带秒的完整时间导出",
            channel=channel or None,
            row_ref=row_ref,
        )
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=resolved)
    return parsed.astimezone(resolved)


def build_record(
    row: BillRow,
    *,
    channel: str,
    period: str,
    window: Optional[PeriodWindow] = None,
    session_tz: Optional[Any] = None,
    bill_date: str = "",
) -> BillRecord:
    """把一行 :class:`BillRow` 归一成一条可落库的 :class:`BillRecord`。

    账单日期优先取调用方给的（渠道按自然日取件时已知）；没有则由**归一时区
    下的交易时间**推出；连交易时间都没有（如只给了 ``bill_date`` 的对账总单）
    时回落到**账期起始日**并打 ``bill_date_inferred=True`` —— 宁可挂一个
    明示的推断标记，也不静默补一个看起来像真的假日期。
    """
    mapping = CHANNEL_COLUMN_MAP.get(channel)
    if mapping is None:
        raise ReconqError(
            "CHANNEL_UNKNOWN",
            f"未登记的渠道 {channel!r}；已登记="
            f"{sorted(CHANNEL_COLUMN_MAP)}",
            channel=channel,
            row_ref=f"{row.source_file}:{row.row_no}" if row.source_file else None,
        )

    def col(target: str) -> str:
        source = mapping.get(target, "")
        return row.get(source) if source else ""

    order_no = col("order_no").strip()
    row_ref: Any = f"{row.source_file}:{row.row_no}" if row.source_file else row.row_no
    if not order_no:
        # 唯一索引的最后一列不能为空：空串会让同一批里所有缺单号的行互相
        # 冲突，表现为「明明有 20 行却只进了 1 行」。宁可在入库前报错。
        raise ReconqError(
            "BILL_ORDER_NO_MISSING",
            f"该行没有渠道单号，无法进入幂等唯一键；"
            f"空单号行会让同批所有缺单行互相冲突（症状是「20 行只进 1 行」）",
            channel=channel,
            row_ref=row_ref,
        )

    tz = session_tz or (window.tz if window else None) or to_session_tz(None)
    trade_time = parse_trade_time(
        col("trade_time"), tz=tz, channel=channel, row_ref=row_ref
    )

    inferred = False
    resolved_bill_date = (bill_date or "").strip()
    if not resolved_bill_date:
        if trade_time is not None:
            resolved_bill_date = (
                window.bucket_date(trade_time) if window else trade_time.strftime("%Y-%m-%d")
            )
            inferred = True
        elif window is not None:
            resolved_bill_date = window.start.strftime("%Y-%m-%d")
            inferred = True
        else:
            raise ReconqError(
                "BILL_DATE_MISSING",
                "既没有账单日期也没有交易时间，无法确定该行归属的账单日；"
                "请提供 bill_date 或交易时间列",
                channel=channel,
                row_ref=row_ref,
            )

    refund_raw = col("refund").strip()
    return BillRecord(
        channel=channel,
        period=period,
        bill_date=resolved_bill_date,
        channel_order_no=order_no,
        his_order_no=col("his_order_no").strip(),
        amount=to_amount_text(col("amount"), channel=channel, row_ref=row_ref),
        refund=to_amount_text(refund_raw, channel=channel, row_ref=row_ref)
        if refund_raw
        else None,
        trade_time=trade_time,
        trade_type=col("trade_type").strip(),
        department=col("department").strip(),
        settle_type=col("settle_type").strip(),
        source_file=row.source_file,
        source_row_no=row.row_no,
        bill_date_inferred=inferred,
        idempotency_key=compute_idempotency_key(period, channel, resolved_bill_date),
    )


_INSERT_SQL = """
INSERT INTO bill_rows (
    channel, period, bill_date, channel_order_no, his_order_no,
    amount_text, refund_text, trade_time, trade_type, department, settle_type,
    source_file, source_row_no, bill_date_inferred, idempotency_key, ingested_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (channel, period, bill_date, channel_order_no) DO NOTHING
"""


def upsert_bill_rows(
    conn: sqlite3.Connection,
    records: Sequence[BillRecord],
    *,
    idempotency_key: str = "",
    ingested_at: str = "",
    fail_after: int = 0,
) -> UpsertOutcome:
    """幂等落库。返回 ``(inserted, skipped_duplicates, total_amount)``。

    ``total_amount`` 是**该账期该渠道全表**的金额合计，不是本批的合计 ——
    重放一次（``skipped_duplicates == 0`` 之外的正常重跑）时它必须与第一次
    **完全相等**，这是「重复拉取不改变结论」的直接判据。只算本批的话，
    第二次重放全被跳过，合计会变成 0，看不出「重放没改数据」。

    参数
    ----
    ``fail_after``
        仅供测试：在写入第 N 行之后人为抛错，用来验证事务整批回滚。
        产品代码路径不传该参数。
    """
    if not records:
        return UpsertOutcome(0, 0, "0", idempotency_key=idempotency_key)

    key = idempotency_key or records[0].idempotency_key
    stamp = ingested_at or datetime.now().astimezone().isoformat(timespec="seconds")
    inserted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for index, record in enumerate(records, start=1):
            if fail_after and index > fail_after:
                raise RuntimeError(
                    f"测试注入的中断：写入第 {index} 行前抛错（fail_after={fail_after}）"
                )
            payload = record.to_tuple() + (key or record.idempotency_key, stamp)
            cursor = conn.execute(_INSERT_SQL, payload)
            inserted += cursor.rowcount if cursor.rowcount > 0 else 0
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    total = _period_total(conn, records[0].period, records[0].channel)
    inferred = sum(1 for r in records if r.bill_date_inferred)
    skipped = len(records) - inserted
    return UpsertOutcome(
        inserted=inserted,
        skipped_duplicates=skipped,
        total_amount=total,
        idempotency_key=key,
        bill_date=records[0].bill_date,
        channel=records[0].channel,
        period=records[0].period,
        inferred_dates=inferred,
    )


def _period_total(conn: sqlite3.Connection, period: str, channel: str) -> str:
    """该账期该渠道的全表金额合计（``Decimal`` 定点求和）。"""
    cursor = conn.execute(
        "SELECT amount_text FROM bill_rows WHERE period = ? AND channel = ?",
        (period, channel),
    )
    total = Decimal("0")
    for row in cursor:
        total += to_amount_text(row["amount_text"])
    return str(total)


def ingest_channel_rows(
    conn: sqlite3.Connection,
    rows: Iterable[BillRow],
    *,
    channel: str,
    period: str,
    window: Optional[PeriodWindow] = None,
    session_tz: Optional[Any] = None,
    bill_date: str = "",
    ingested_at: str = "",
    batch_size: int = 500,
) -> UpsertOutcome:
    """把一路渠道一个账期的账单行落库，返回累计结果。

    分批提交（每 ``batch_size`` 行一个事务）而不是整账期一个大事务：
    六路各 2000+ 行时单事务会长时间占写锁，而取数是流式的、分批到齐才
    落库。分批的代价是「中途失败时前几批已落库」，但因为唯一索引是幂等
    的，重跑会把缺的那批补齐、不会产生重复 —— 这与「单事务半截落库」是
    两回事。
    """
    session_tz = session_tz or to_session_tz(None)
    inserted = 0
    skipped = 0
    batch: list[BillRecord] = []
    for row in rows:
        batch.append(
            build_record(
                row,
                channel=channel,
                period=period,
                window=window,
                session_tz=session_tz,
                bill_date=bill_date,
            )
        )
        if len(batch) >= batch_size:
            outcome = upsert_bill_rows(conn, batch, ingested_at=ingested_at)
            inserted += outcome.inserted
            skipped += outcome.skipped_duplicates
            batch = []
    if batch:
        outcome = upsert_bill_rows(conn, batch, ingested_at=ingested_at)
        inserted += outcome.inserted
        skipped += outcome.skipped_duplicates

    total = _period_total(conn, period, channel)
    return UpsertOutcome(
        inserted=inserted,
        skipped_duplicates=skipped,
        total_amount=total,
        bill_date=bill_date,
        channel=channel,
        period=period,
        detail=f"分批提交，每批 {batch_size} 行",
    )


def query_period(
    conn: sqlite3.Connection,
    period: str,
    channel: Optional[str] = None,
) -> list[dict[str, Any]]:
    """查某账期（可选某渠道）的账单行，按来源行号排序。"""
    sql = "SELECT * FROM bill_rows WHERE period = ?"
    params: list[Any] = [period]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    sql += " ORDER BY channel, bill_date, source_file, source_row_no, ch_id"
    return [{k: r[k] for k in r.keys()} for r in conn.execute(sql, params)]


def row_summary(conn: sqlite3.Connection, period: str) -> dict[str, dict[str, Any]]:
    """按渠道汇总一行数与金额合计，供 run summary 与面板共用。

    刻意**不用** ``SUM(amount_text)``：金额存的是定点字面量，SQL 侧求和只能
    走 ``REAL``，那一分钱的浮点残影会从汇总表直接冒到结论单上。这里逐行取
    回在 Python 侧用 ``Decimal`` 累加，与 :func:`_period_total` 同一口径。
    """
    cursor = conn.execute(
        "SELECT channel, amount_text, bill_date_inferred "
        "FROM bill_rows WHERE period = ? ORDER BY channel",
        (period,),
    )
    buckets: dict[str, dict[str, Any]] = {}
    for row in cursor:
        bucket = buckets.setdefault(
            row["channel"],
            {"channel": row["channel"], "row_count": 0, "total_amount": Decimal("0"),
             "inferred_dates": 0},
        )
        bucket["row_count"] += 1
        bucket["total_amount"] += to_amount_text(row["amount_text"])
        bucket["inferred_dates"] += 1 if row["bill_date_inferred"] else 0
    for bucket in buckets.values():
        bucket["total_amount"] = str(bucket["total_amount"])
    return buckets
