"""脏行隔离区与脏行台账导出（Task 11）。

一条硬纪律：**账单里的脏行不得被静默丢掉**。
「丢掉」在对账里的后果是双向的 —— 少算一笔会让本路合计比 HIS 少，
差额是负的；财务科看到负差额的第一反应是「渠道少给了钱」，
于是去找渠道对质，而真正的���因是自己的解析层把那一行吃掉了。
这类事故排查成本极高，且事后无法从结论单复原。

所以本模块给出的是**隔离**而不是**丢弃**：脏行进 ``dirty_rows`` 表，
带上渠道、账期、源文件、**源文件真实行号**与**原始行文本**，
财务科拿着 ``文件名:行号`` 就能直接回查导出方；其余行照常入库、照常跑口径。

设计要点
--------

1. **唯一键 = 渠道 + 账期 + 文件 + 行号**。同一份文件被重复扫描（重跑、
   多文件、并发）不会产生重复隔离记录 —— 扫描是幂等的，隔离区必须也是。
   同一行只保留**第一个**原因码：财务科回查时关心的是「这一行有问题」，
   至于它同时违反了三条规则，要在面板上展开，不该在台账里占三行。

2. **脏行率阈值（默认 5%）**。少量脏行是导出瑕疵，隔离后继续对账是合理的；
   超过阈值说明**整份文件的导出方式不对**（列错位、分隔符猜错、编码坏了），
   此时继续拿剩下的行对账会产出「看着干净其实只对了一部分」的结论 ——
   比报错危险得多。整文件判 ``FORMAT_BROKEN``，该渠道本期标未取到，
   其余五路照常。

3. **重复单号进隔离区**。同一份文件里同一个渠道单号出现两次：
   无论两行金额是否相同，都只有**第一行**能进账单表（唯一索引也只留一条），
   后面重复的行如果不隔离，就会在 ``decimal_sum`` 里被算两遍，
   凭空多出一笔金额。隔离 + 标 ``DUPLICATE_ROW_CONFLICT``，
   经办人一眼能看出「导出方重复导了一次」。

4. **空必填列要分流**。空的是**主键列**（如商户订单号）时，该行根本进不了
   幂等唯一键，留下来只会让同批所有空串行互相冲突（症状是「20 行只进 1 行」），
   因此隔离为 ``REQUIRED_FIELD_EMPTY``；空的是**非主键必填列**（如微信交易单号）
   时，本产品**保留该行**并让 ``not_empty`` 口径以证据行号报出来 ——
   那是财务科要看的差异，不是解析层的垃圾。

源产品对应：参考实现 ``data_source_connection.py`` 的
``execute_query_one_by_one()`` 是「逐行流式、逐行 try/except」，
坏行被吞掉后不影响其余行；本产品把「吞掉」改成「隔离 + 可导出 + 逐行可追」，
因为对账场景下「被吞掉的那一行」本身就是财务科必须知道的结论。
"""

from __future__ import annotations

import csv
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

from ..errors import ReconqError

__all__ = [
    "DIRTY_TABLE",
    "DIRTY_TABLE_DDL",
    "DIRTY_INDEXES",
    "DEFAULT_DIRTY_RATE_THRESHOLD",
    "REASON_CODES",
    "REASON_ZH",
    "QuarantineRow",
    "FileScanResult",
    "init_dirty_schema",
    "insert_dirty_rows",
    "count_dirty",
    "dirty_rate",
    "list_dirty",
    "channel_state",
    "export_quarantine_csv",
    "scan_channel_file",
    "scan_channel_dir",
]


DIRTY_TABLE = "dirty_rows"

#: 脏行隔离表。列名与账单表同风格：金额不存这里，脏行根本不进金额口径。
DIRTY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS dirty_rows (
    q_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT NOT NULL,
    period       TEXT NOT NULL,
    source_file  TEXT NOT NULL,
    row_no       INTEGER NOT NULL,
    raw_line     TEXT NOT NULL DEFAULT '',
    reason_code  TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    detected_at  TEXT NOT NULL DEFAULT ''
)
"""

#: 隔离幂等键：渠道 + 账期 + 文件 + 行号。重复扫描撞它，不会重复记账。
DIRTY_INDEXES = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_dirty_identity
        ON dirty_rows (channel, period, source_file, row_no)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_dirty_period
        ON dirty_rows (period, channel)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_dirty_reason
        ON dirty_rows (period, reason_code)
    """,
)


#: 脏行率阈值。超过即判整文件 ``FORMAT_BROKEN``。
#:
#: 5% 这个值来自院内导出的实际经验：一份六路账单动辄两三千行，
#: 个位数的脏行是导出瑕疵（某一笔商品��带了逗号、某一行少个字段），
#: 隔离后继续对账是对的；一旦脏行成片出现，问题基本都在「导出方式」
#: 而不是「这几行数据」上 —— 这时继续跑只会得到一份残缺的干净结论。
DEFAULT_DIRTY_RATE_THRESHOLD = 0.05


#: 脏行原因码（受控集合，不接受自由文本）。
#: 面板按这个分类筛选、结论单按这个归组；出现表外的码一律视为程序缺陷。
REASON_CODES = (
    "COLUMN_COUNT_MISMATCH",
    "AMOUNT_UNPARSABLE",
    "ENCODING_UNRESOLVED",
    "REQUIRED_FIELD_EMPTY",
    "DUPLICATE_ROW_CONFLICT",
    "TIMESTAMP_UNPARSABLE",
)

#: 原因码的中文说明与建议动作。与 ``errors.py`` 的错误码表同一风格 ——
#: 财务科看到的是「这句话什么意思、该找谁」，不是一个大写英文串。
REASON_ZH: dict[str, tuple[str, str]] = {
    "COLUMN_COUNT_MISMATCH": (
        "该行列数与表头不一致",
        "多半是导出时某行少了个分隔符（或数据里带了分隔符）；"
        "把文件名与行号回查给导出方重导该账期",
    ),
    "AMOUNT_UNPARSABLE": (
        "金额列的值不是合法数字",
        "让导出方按纯数字导出金额（不要带大写金额、中文单位）；"
        "千分位逗号与货币符号本产品会自动清洗",
    ),
    "ENCODING_UNRESOLVED": (
        "该行含无法解码的字节",
        "文件编码判据不牢；请让信息科按 UTF-8 或 GBK 重新导出，"
        "本产品不会把乱码当成单号去对账",
    ),
    "REQUIRED_FIELD_EMPTY": (
        "主键列（渠道单号）为空",
        "该行进不了幂等唯一键，留着会让同批空单号行互相冲突；"
        "请让导出方补齐商户订单号列",
    ),
    "DUPLICATE_ROW_CONFLICT": (
        "同一文件内渠道单号重复",
        "同一笔被导出两次；本产品只保留第一行（否则金额会被算两遍），"
        "重复行请导出方核查",
    ),
    "TIMESTAMP_UNPARSABLE": (
        "交易时间无法解析",
        "请让导出方按带秒的完整时间导出，不要只给日期",
    ),
}


@dataclass(frozen=True)
class QuarantineRow:
    """一条脏行隔离记录。字段与 ``dirty_rows`` 表列序严格一致。"""

    channel: str
    period: str
    source_file: str
    row_no: int
    reason_code: str
    raw_line: str = ""
    detail: str = ""
    detected_at: str = ""

    def __post_init__(self) -> None:
        if self.reason_code not in REASON_CODES:
            raise ReconqError(
                "DIRTY_REASON_UNKNOWN",
                f"未登记的脏行原因码 {self.reason_code!r}；已登记={list(REASON_CODES)}",
                channel=self.channel or None,
                row_ref=f"{self.source_file}:{self.row_no}" if self.source_file else None,
            )

    @property
    def row_ref(self) -> str:
        """财务科拿这个串去回查导出方：``文件名:行号``。"""
        return f"{self.source_file}:{self.row_no}"

    def zh(self) -> str:
        return REASON_ZH.get(self.reason_code, (self.reason_code, ""))[0]

    def suggestion(self) -> str:
        return REASON_ZH.get(self.reason_code, ("", ""))[1]

    def to_tuple(self) -> tuple[Any, ...]:
        return (
            self.channel,
            self.period,
            self.source_file,
            self.row_no,
            self.raw_line,
            self.reason_code,
            self.detail,
            self.detected_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "period": self.period,
            "source_file": self.source_file,
            "row_no": self.row_no,
            "row_ref": self.row_ref,
            "reason_code": self.reason_code,
            "reason_zh": self.zh(),
            "suggestion": self.suggestion(),
            "raw_line": self.raw_line,
            "detail": self.detail,
            "detected_at": self.detected_at,
        }


@dataclass
class FileScanResult:
    """一份账单文件扫描隔离后的结果。"""

    channel: str
    period: str
    source_file: str
    #: 干净行数（已入库）
    clean_rows: int
    #: 本次新写入隔离区的条数
    dirty_inserted: int
    #: 命中隔离幂等键被跳过的条数（重复扫描时非零）
    dirty_skipped: int
    #: 脏行总数（本次文件内被隔离的行数，含此前已隔离过的）
    dirty_total: int
    #: 脏行率 = 脏行 / (干净行 + 脏行)
    dirty_rate: float
    #: ``clean`` / ``dirty`` / ``broken``
    state: str
    error_code: str = ""
    detail: str = ""
    probe: Any = None

    @property
    def file_broken(self) -> bool:
        return self.state == "broken"

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "period": self.period,
            "source_file": self.source_file,
            "clean_rows": self.clean_rows,
            "dirty_inserted": self.dirty_inserted,
            "dirty_skipped": self.dirty_skipped,
            "dirty_total": self.dirty_total,
            "dirty_rate": round(self.dirty_rate, 6),
            "state": self.state,
            "error_code": self.error_code,
            "detail": self.detail,
        }


# —— 建表与写入 ——————————————————————————————————————————


def init_dirty_schema(conn: sqlite3.Connection) -> None:
    """建隔离表与索引。幂等：重复调用不报错。"""
    conn.execute(DIRTY_TABLE_DDL)
    for statement in DIRTY_INDEXES:
        conn.execute(statement)


_INSERT_SQL = """
INSERT INTO dirty_rows (channel, period, source_file, row_no, raw_line,
                        reason_code, detail, detected_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (channel, period, source_file, row_no) DO NOTHING
"""


def insert_dirty_rows(
    conn: sqlite3.Connection,
    rows: Sequence[QuarantineRow],
    *,
    detected_at: str = "",
) -> tuple[int, int]:
    """幂等写隔离区，返回 ``(inserted, skipped_duplicates)``。

    整个写入是一个事务：隔离记录半截落库比不落更糟 —— 面板会显示
    「脏行 N 条」却查不到第 N 条，财务科按图索骥扑空。
    """
    if not rows:
        return (0, 0)
    stamp = detected_at or datetime.now().astimezone().isoformat(timespec="seconds")
    inserted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            payload = row.to_tuple()
            cursor = conn.execute(_INSERT_SQL, payload[:-1] + (payload[-1] or stamp,))
            inserted += cursor.rowcount if cursor.rowcount > 0 else 0
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return (inserted, len(rows) - inserted)


# —— 查询与统计 ————————————————————————————————————————


def list_dirty(
    conn: sqlite3.Connection,
    period: str,
    channel: Optional[str] = None,
) -> list[dict[str, Any]]:
    """列出某账期（可选某渠道）的隔离记录，按文件与行号排序。"""
    sql = "SELECT * FROM dirty_rows WHERE period = ?"
    params: list[Any] = [period]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    sql += " ORDER BY channel, source_file, row_no, q_id"
    return [{k: r[k] for k in r.keys()} for r in conn.execute(sql, params)]


def count_dirty(
    conn: sqlite3.Connection,
    period: str,
    channel: Optional[str] = None,
) -> int:
    """某账期（可选某渠道）的脏行条数。"""
    sql = "SELECT COUNT(*) FROM dirty_rows WHERE period = ?"
    params: list[Any] = [period]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    return int(conn.execute(sql, params).fetchone()[0])


def dirty_rate(dirty: int, clean: int) -> float:
    """脏行率 = 脏行 / (干净行 + 脏行)。

    全部行都是脏行时返回 ``1.0`` 而不是除零：那种文件的正确处置是
    ``FORMAT_BROKEN``，让阈值比较能正常走到「超限」分支。
    """
    total = dirty + clean
    if total <= 0:
        return 0.0
    return dirty / total


def channel_state(
    conn: sqlite3.Connection,
    period: str,
    channel: str,
    *,
    clean_rows: int = 0,
) -> tuple[str, int, float]:
    """面板渠道主态：``(state, dirty_count, rate)``。

    面板按这个显示颜色：``clean`` 绿、``dirty`` 黄（本期有脏行，需回查导出方）、
    ``broken`` 红（整文件判坏，本期该路未取到）。
    """
    dirty = count_dirty(conn, period, channel)
    rate = dirty_rate(dirty, clean_rows)
    if rate > DEFAULT_DIRTY_RATE_THRESHOLD:
        return ("broken", dirty, rate)
    if dirty:
        return ("dirty", dirty, rate)
    return ("clean", 0, 0.0)


# —— 导出 ————————————————————————————————————————————————


#: 导出 CSV 的列顺序。财务科是拿这张表去跟导出方逐行核的，
#: 所以「渠道/文件/行号/原因码/原文」五列必须最显眼。
EXPORT_COLUMNS = (
    "渠道",
    "账期",
    "来源文件",
    "源文件行号",
    "原因码",
    "原因说明",
    "原始行",
    "详情",
    "发现时间",
    "回查定位",
)


def export_quarantine_csv(
    conn: sqlite3.Connection,
    period: str,
    channel: Optional[str],
    out_path: str,
) -> tuple[str, int]:
    """把隔离区导出成 CSV，返回 ``(路径, 行数)``。

    用 ``utf-8-sig`` 写：财务科双击就是 Excel，直接开 UTF-8 会在
    Windows Excel 里把中文显示成乱码，而「乱码的脏行台账」等于没有台账。
    """
    records = list_dirty(conn, period, channel)
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(EXPORT_COLUMNS)
        for rec in records:
            code = rec["reason_code"]
            zh, suggestion = REASON_ZH.get(code, (code, ""))
            writer.writerow(
                [
                    rec["channel"],
                    rec["period"],
                    rec["source_file"],
                    rec["row_no"],
                    code,
                    zh,
                    rec["raw_line"],
                    rec["detail"],
                    rec["detected_at"],
                    f"{rec['source_file']}:{rec['row_no']}",
                ]
            )
    return (out_path, len(records))


# —— 扫描编排：读文件 → 隔离脏行 → 其余行入库 ——————————————


def scan_channel_file(
    conn: sqlite3.Connection,
    path: str,
    table,
    channel: str,
    period: str,
    *,
    rate_threshold: float = DEFAULT_DIRTY_RATE_THRESHOLD,
    detected_at: str = "",
    batch_size: int = 500,
    ingest: bool = True,
) -> FileScanResult:
    """扫描一份账单文件：脏行进隔离区，干净行进账单表。

    这是「部分失败不静默改数据」的落点 —— 解析阶段的单行错误不再向上
    抛泡把整份文件打断（Task 5/10 的 ``read_bill_file_probed`` 仍然抛，
    那是排障腿要的严格行为），而是收进隔离区；只有**文件级**问题
    （表头认不出必填列、结构损坏）才继续抛，因为它意味着
    「这份文件一行都读不出来」，隔离区里也不会有内容。

    脏行率超过 ``rate_threshold`` 时整文件判 ``FORMAT_BROKEN``：
    此时干净行**不入库**（避免产出一份残缺却看着干净的账），
    隔离记录仍然写入 —— 财务科需要知道「这期有 400 行脏行」，
    而不是「这期没取到账单、两处都不提」。
    """
    from ..sources.format_probe import read_bill_file_isolated
    from .bill_store import build_record, upsert_bill_rows

    source_name = os.path.basename(path)
    rows, probe, dirty = read_bill_file_isolated(
        path, table, channel, period=period, isolate_empty_required=True
    )
    dirty = _mark_duplicate_keys(dirty, rows, channel, source_name, period)

    total_rows = len(rows) + len(dirty)
    rate = dirty_rate(len(dirty), len(rows))
    if rate > rate_threshold:
        inserted, skipped = insert_dirty_rows(conn, dirty, detected_at=detected_at)
        zh_rate = f"{rate * 100:.1f}%"
        return FileScanResult(
            channel=channel,
            period=period,
            source_file=source_name,
            clean_rows=0,
            dirty_inserted=inserted,
            dirty_skipped=skipped,
            dirty_total=len(dirty),
            dirty_rate=rate,
            state="broken",
            error_code="FORMAT_BROKEN",
            detail=(
                f"脏行率 {zh_rate} 超过阈值 {rate_threshold * 100:.1f}%："
                f"{source_name} 共 {total_rows} 行，其中 {len(dirty)} 行进隔离区，"
                f"干净行未入库（避免拿残缺数据出干净结论）。"
                f"本渠道本期标未取到，其余渠道照常。"
                f"隔离台账：reconq quarantine export --period {period} --channel {channel}"
            ),
            probe=probe,
        )

    inserted, skipped = insert_dirty_rows(conn, dirty, detected_at=detected_at)

    ingested = 0
    if ingest and rows:
        stamp = detected_at or datetime.now().astimezone().isoformat(timespec="seconds")
        batch: list = []
        for row in rows:
            batch.append(build_record(row, channel=channel, period=period))
            if len(batch) >= batch_size:
                ingested += upsert_bill_rows(conn, batch, ingested_at=stamp).inserted
                batch = []
        if batch:
            ingested += upsert_bill_rows(conn, batch, ingested_at=stamp).inserted

    state = "dirty" if dirty else "clean"
    return FileScanResult(
        channel=channel,
        period=period,
        source_file=source_name,
        clean_rows=len(rows),
        dirty_inserted=inserted,
        dirty_skipped=skipped,
        dirty_total=len(dirty),
        dirty_rate=rate,
        state=state,
        detail=(
            (
                f"{len(rows)} 行入库，{len(dirty)} 行进隔离区"
                f"（本次新增 {inserted} 条，命中幂等键跳过 {skipped} 条）；"
                f"隔离台账：reconq quarantine export --period {period} --channel {channel}"
            )
            if dirty
            else f"{len(rows)} 行全部干净入库"
        ),
        probe=probe,
    )


def _mark_duplicate_keys(
    dirty: Iterable[QuarantineRow],
    rows: Sequence,
    channel: str,
    source_name: str,
    period: str,
) -> list[QuarantineRow]:
    """把同一文件内**完全重复**的行（第二行起）标进隔离区。

    判据刻意收窄到「单号 + 金额 + 交易时间三者全同」，而不是只看单号。
    原因是渠道账单里**同号多行本来就是合法的**：一码付拆笔、合并支付、
    退费行与原单并存（微信明细里退款会复用原商户订单号）都会让同一个
    商户订单号出现多次。那些行是真实的业务事实，隔离掉等于把差异
    从账上抹掉 —— 它们该由对齐器（Task 21+）判成拆笔/合并/退费并存，
    而不是被解析层当成「重复导出」丢掉。

    真正要拦的是**导出方把同一行导了两遍**：连金额带时间都一模一样，
    留着只会在 ``decimal_sum`` 里被算两遍，凭空多出一笔。
    """
    from .bill_store import CHANNEL_COLUMN_MAP

    mapping = CHANNEL_COLUMN_MAP.get(channel) or {}
    key_column = mapping.get("order_no", "")
    amount_column = mapping.get("amount", "")
    time_column = mapping.get("trade_time", "")
    if not key_column:
        return list(dirty)

    def signature(row) -> tuple[str, str, str]:
        return (
            (row.get(key_column) or "").strip(),
            (row.get(amount_column) or "").strip() if amount_column else "",
            (row.get(time_column) or "").strip() if time_column else "",
        )

    seen: set[tuple[str, str, str]] = set()
    first_row_no: dict[tuple[str, str, str], int] = {}
    extra: list[QuarantineRow] = []
    for row in rows:
        sig = signature(row)
        if not sig[0] or sig not in seen:
            if sig[0]:
                seen.add(sig)
                first_row_no[sig] = row.row_no
            continue
        extra.append(
            QuarantineRow(
                channel=channel,
                period=period,
                source_file=source_name,
                row_no=row.row_no,
                reason_code="DUPLICATE_ROW_CONFLICT",
                raw_line=row.raw_line,
                detail=(
                    f"渠道单号 {key_column}={sig[0]!r} 连同金额与交易时间在本文件内"
                    f"已于第 {first_row_no[sig]} 行完整出现（同一笔被导出两次）；"
                    f"只保留首次出现的一行，否则金额会被算两遍"
                ),
            )
        )
    return list(dirty) + extra


def scan_channel_dir(
    conn: sqlite3.Connection,
    directory: str,
    table,
    channel: str,
    period: str,
    *,
    rate_threshold: float = DEFAULT_DIRTY_RATE_THRESHOLD,
    detected_at: str = "",
    ingest: bool = True,
) -> list[FileScanResult]:
    """扫描 ``<directory>`` 下的全部账单文件，逐份给出隔离结果。"""
    if not os.path.isdir(directory):
        raise ReconqError(
            "BILL_FILE_NOT_FOUND",
            f"账单目录不存在：{directory}/",
            channel=channel,
        )
    names = sorted(
        n for n in os.listdir(directory)
        if n.lower().endswith((".csv", ".txt")) and not n.startswith(".")
    )
    if not names:
        raise ReconqError(
            "BILL_FILE_NOT_FOUND",
            f"账单目录里没有可扫描的文件：{directory}/",
            channel=channel,
        )
    return [
        scan_channel_file(
            conn,
            os.path.join(directory, name),
            table,
            channel,
            period,
            rate_threshold=rate_threshold,
            detected_at=detected_at,
            ingest=ingest,
        )
        for name in names
    ]
