"""Task 37 · 真实规模压测：六路各 2000+ 行跑完整扫描，测耗时与峰值内存。

这个脚本回答的是财务科周一早上真正会问的那句话
------------------------------------------------
「六路一起扫一遍要多久？会不会把信息科的面板服务打死？」

所以它测的是**主路径本身**（:func:`reconq.engine.orchestrator.scan` 六路取数
→ 落库 → 逐条口径），不是造一批假对象跑个 for 循环。主路径没跑过的性能数字
没有任何意义 —— 真实耗时的大头在**格式探测**（GB18030 严格解码 + 分隔符打分）
与**幂等落库**（唯一索引冲突逐行判定），这两处都在脚本跑的这条路径里。

测什么、不测什么
----------------
============================  ==========================================
测                            不测
============================  ==========================================
六路逐路耗时与合计行数        模型端到端延迟（要凭据，留给 Task 39）
每路批次数 / FETCH 往返次数    并发（单账期本来就禁止并发扫）
峰值内存（tracemalloc）        面板渲染耗时
幂等重放的行数与金额是否不变  网络往返（走文件通道）
============================  ==========================================

峰值内存这条必须说清楚它管什么
------------------------------
断言的界是「**约一批 + 最大单行**」，指的是 :mod:`reconq.sources.his_db` 的
**流式取数腿**：批预算 ``STREAM_FETCH_BUDGET_BYTES = 8MB``、批上限
``STREAM_FETCH_MAX_BATCH_ROWS = 1000``、每批最多放大 4 倍（对齐参考实现的
soda-postgres 连接类）。该腿逐行 yield、调用方永远只看得到一行。

**不**把「整次扫描的峰值」也断言在这个界内：``orchestrator.scan`` 要把一路
渠道的整个账期读进内存才能跑口径（``_read_channel_rows`` 返回 list），六路
各 2000 行时这个量级是几十 MB，属于设计如此、不是缺陷。脚本把两个数字**分别
打出来**（``stream_peak_mb`` 与 ``scan_peak_mb``）并在报告里写明各自的口径，
避免「峰值 3MB」这种把两条腿混着报的漂亮话。

HIS 只读库替身（测试数据层，不是最终对接方式）
----------------------------------------------
本机没有院内 HIS 实例，脚本用**本地 SQLite 替身库**装脱敏样例，走的是
:mod:`reconq.sources.his_db` 的**真实主路径**（``stream_charge_rows`` 逐行
流式 + 自适应分批 + 只 SELECT 八列）。替身只替「数据在哪」，不替「怎么取」。

替身库有一处**必须与生产一致**的细节：``charge_time`` 存**带偏移**的 ISO 串
而不是裸文本。原因是 SQLAlchemy 把产品侧的 ``+08:00`` 感知 datetime 绑成
``'2026-09-28 00:00:00+08:00'``，而 SQLite 的 TEXT 比较是**字典序**的 ——
存裸 ``'2026-09-28 00:00:00'`` 时它比 ``'2026-09-28 00:00:00+08:00'`` 小，
于是**恰好落在账期窗口首秒的那一行会被静默丢掉**（实测 5 行：五路各一条
边界行 + 一条普通行）。生产库 ``charge_time`` 是 timestamp 列，比较是数值
比较，不存在这个问题；但替身库必须按同一形态存，否则压测的分母就是错的，
而「边界行被丢掉」恰恰是本脚本要验证的东西。脚本对本地产物做行数对账
（:func:`assert_his_row_count`），差一条就报错退出。

用法（在项目子目录下）::

    PYTHONPATH=src python3 scripts/gen_fixtures.py --out out/scale --force
    PYTHONPATH=src python3 scripts/bench.py --fixtures out/scale --out out/bench.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from reconq.align.matcher import AlignRecord, align_all, record_from_row_dict
from reconq.engine.orchestrator import scan
from reconq.sources.his_db import (
    STREAM_FETCH_BUDGET_BYTES,
    STREAM_FETCH_GROWTH_FACTOR,
    STREAM_FETCH_MAX_BATCH_ROWS,
    StreamStats,
    stream_charge_rows,
)
from reconq.sources.timezone import period_window, to_session_tz
from reconq.store.bill_store import query_period
from reconq.store.schema import connect

DEFAULT_PERIOD = "2026-W40"
#: 流式腿峰值内存的界：批字节预算 + 一条最宽行的两倍余量。
#: 留 2 倍余量而不是 1 倍，是因为 tracemalloc 记的是**峰值**而不是均值，
#: 一批组装完、还没被消费完的那一刻同时驻留着「上一批 + 下一批」的部分。
PEAK_HEADROOM = 2


@dataclass
class BenchResult:
    period: str
    fixture_root: str
    db_path: str
    his_dsn: str
    rows: dict[str, int] = field(default_factory=dict)
    amounts: dict[str, str] = field(default_factory=dict)
    check_total: int = 0
    check_failed: int = 0
    gate: str = ""
    scan_seconds: float = 0.0
    scan_peak_mb: float = 0.0
    stream_seconds: float = 0.0
    stream_peak_mb: float = 0.0
    stream_rows: int = 0
    batches: int = 0
    windows: int = 0
    widest_row_bytes: int = 0
    budget_bytes: int = STREAM_FETCH_BUDGET_BYTES
    max_batch_rows: int = STREAM_FETCH_MAX_BATCH_ROWS
    growth_factor: int = STREAM_FETCH_GROWTH_FACTOR
    align_seconds: float = 0.0
    align_peak_mb: float = 0.0
    align_counts: dict[str, int] = field(default_factory=dict)
    replay_rows_inserted: int = 0
    replay_rows_skipped: int = 0
    replay_amount_stable: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "fixture_root": self.fixture_root,
            "rows": self.rows,
            "amounts": self.amounts,
            "check_total": self.check_total,
            "check_failed": self.check_failed,
            "gate": self.gate,
            "scan_seconds": round(self.scan_seconds, 3),
            "scan_peak_mb": round(self.scan_peak_mb, 2),
            "stream_seconds": round(self.stream_seconds, 3),
            "stream_peak_mb": round(self.stream_peak_mb, 2),
            "stream_rows": self.stream_rows,
            "batches": self.batches,
            "windows": self.windows,
            "widest_row_bytes": self.widest_row_bytes,
            "budget_bytes": self.budget_bytes,
            "max_batch_rows": self.max_batch_rows,
            "growth_factor": self.growth_factor,
            "align_seconds": round(self.align_seconds, 3),
            "align_peak_mb": round(self.align_peak_mb, 2),
            "align_counts": self.align_counts,
            "replay_rows_inserted": self.replay_rows_inserted,
            "replay_rows_skipped": self.replay_rows_skipped,
            "replay_amount_stable": self.replay_amount_stable,
            "warnings": self.warnings,
        }

    def render(self) -> str:
        lines = [
            f"账期 {self.period} · 规模压测结果",
            "",
            "── 取数规模 ──",
        ]
        for ch, n in sorted(self.rows.items()):
            lines.append(f"  {ch:<13} {n:>6} 行   合计 {self.amounts.get(ch, '-')} 元")
        lines += [
            "",
            "── 口径 ──",
            f"  共执行 {self.check_total} 条，未通过 {self.check_failed} 条，门禁 {self.gate}",
            "",
            "── 耗时（主路径实跑）──",
            f"  完整扫描 scan()      {self.scan_seconds:>7.3f} s   峰值内存 {self.scan_peak_mb:>7.2f} MB",
            f"  HIS 流式取数          {self.stream_seconds:>7.3f} s   峰值内存 {self.stream_peak_mb:>7.2f} MB"
            f"   {self.stream_rows} 行 / {self.batches} 批 / {self.windows} 窗",
            f"  六路对齐 align_all()  {self.align_seconds:>7.3f} s   峰值内存 {self.align_peak_mb:>7.2f} MB",
            "",
            "── 内存口径（两条腿分开报，不混算）──",
            f"  流式腿峰值 {self.stream_peak_mb:.2f} MB，界 = 批预算 {self.budget_bytes / 1048576:.0f} MB"
            f" × {PEAK_HEADROOM} + 最宽行 {self.widest_row_bytes} B",
            f"  扫描腿峰值 {self.scan_peak_mb:.2f} MB：scan() 要把整期读进内存才能跑口径，"
            f"这个量级是设计如此，不在流式预算口径内",
            "",
            "── 幂等重放 ──",
            f"  第二次扫同一账期：新增 {self.replay_rows_inserted} 行 / 跳过重复 "
            f"{self.replay_rows_skipped} 行，合计金额不变={self.replay_amount_stable}",
            "",
            "── 对齐结论分布 ──",
        ]
        for status, n in sorted(self.align_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {status:<20} {n:>6} 条")
        if self.warnings:
            lines += ["", "── 警告 ──"]
            lines.extend(f"  ! {w}" for w in self.warnings)
        return "\n".join(lines)


# ────────────────────────── HIS 替身库 ──────────────────────────

HIS_COLUMNS = (
    "his_order_no",
    "charge_id",
    "amount",
    "charge_time",
    "period_code",
    "department",
    "settle_type",
    "is_refund",
)


def load_his_double(
    fixture_root: str, period: str, db_path: str
) -> tuple[str, int]:
    """把脱敏样例装进本地 SQLite 替身库，返回 ``(dsn, 期望行数)``。

    ``charge_time`` 存**带偏移** ISO 串（见模块头「HIS 只读库替身」一节）：
    存裸文本会让恰好落在窗口首秒的行被字典序比较静默丢掉。
    """
    from sqlalchemy import create_engine, text

    his_dir = os.path.join(fixture_root, "his")
    files = [
        os.path.join(his_dir, f"charge_record_{period}.csv"),
        os.path.join(his_dir, f"charge_record_{period}-late.csv"),
    ]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        raise SystemExit(f"{his_dir} 下没有 charge_record_{period}.csv，先跑 gen_fixtures.py")

    if os.path.exists(db_path):
        os.remove(db_path)
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    engine = create_engine(f"sqlite:///{db_path}")
    cols = ", ".join(f"{c} TEXT" for c in HIS_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in HIS_COLUMNS)
    expected = 0
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE charge_record ({cols})"))
        for path in files:
            with open(path, encoding="utf-8") as fh:
                for rec in csv.DictReader(fh):
                    payload = {c: rec[c] for c in HIS_COLUMNS}
                    payload["charge_time"] = _aware_iso(rec["charge_time"])
                    conn.execute(
                        text(f"INSERT INTO charge_record VALUES ({placeholders})"), payload
                    )
                    expected += 1
    engine.dispose()
    return f"sqlite:///{db_path}", expected


def _aware_iso(text: str) -> str:
    """裸 ``YYYY-MM-DD HH:MM:SS`` → 带院内时区偏移的 ISO 串。"""
    naive = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=to_session_tz(None)).isoformat(sep=" ")


def assert_his_row_count(
    dsn: str, period: str, expected: int, *, late_rows: int
) -> tuple[int, StreamStats]:
    """跑一次流式取数并对账行数，返回 ``(实到行数, stats)``。

    对账是硬断言：替身库少存一行、或窗口边界被字典序吃掉一行，**压测的分母
    就是错的**，而边界行恰恰是本脚本要验证的东西。差一条就抛错，不静默继续。
    """
    from sqlalchemy import create_engine

    engine = create_engine(dsn)
    stats = StreamStats()
    got = sum(1 for _ in stream_charge_rows(engine, period, stats=stats))
    engine.dispose()
    # 跨账期补录那批按设计落在窗口外，不该被取到
    want = expected - late_rows
    if got != want:
        raise SystemExit(
            f"HIS 替身库行数对账不平：期望窗口内 {want} 行（装入 {expected} 行，"
            f"其中 {late_rows} 行按设计跨账期），实到 {got} 行。"
            f"差值多半是 charge_time 的时区形态不对（存裸文本会被字典序丢掉边界行）"
        )
    return got, stats


# ────────────────────────── 压测 ──────────────────────────


def run_bench(
    *,
    fixture_root: str,
    period: str = DEFAULT_PERIOD,
    db_path: str = "out/bench-scan.sqlite",
    contracts_dir: str = "contracts",
    out_path: str = "out/bench.json",
) -> BenchResult:
    manifest_path = os.path.join(fixture_root, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise SystemExit(f"{manifest_path} 不存在，先跑 scripts/gen_fixtures.py")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    dsn, expected = load_his_double(fixture_root, period, db_path)
    late_rows = int(manifest.get("counts", {}).get("his_late", 0))
    result = BenchResult(
        period=period,
        fixture_root=fixture_root,
        db_path=db_path,
        his_dsn=dsn,
        budget_bytes=STREAM_FETCH_BUDGET_BYTES,
        max_batch_rows=STREAM_FETCH_MAX_BATCH_ROWS,
        growth_factor=STREAM_FETCH_GROWTH_FACTOR,
    )

    # —— 腿 1：HIS 流式取数（峰值内存受批预算约束的那条腿）——
    tracemalloc.start()
    started = time.perf_counter()
    got, stats = assert_his_row_count(dsn, period, expected, late_rows=late_rows)
    result.stream_seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result.stream_peak_mb = peak / 1048576
    result.stream_rows = got
    result.batches = stats.batches
    result.windows = stats.windows
    result.widest_row_bytes = stats.widest_row_bytes

    ceiling = STREAM_FETCH_BUDGET_BYTES * PEAK_HEADROOM + stats.widest_row_bytes
    if peak > ceiling:
        raise SystemExit(
            f"HIS 流式取数峰值 {peak / 1048576:.2f} MB 超出界 "
            f"{ceiling / 1048576:.2f} MB（批预算 {STREAM_FETCH_BUDGET_BYTES} B "
            f"×{PEAK_HEADROOM} + 最宽行 {stats.widest_row_bytes} B）"
        )
    if stats.batches >= got and got > 0:
        raise SystemExit(
            f"批次数 {stats.batches} 不小于行数 {got} —— 分批没生效，"
            f"这个指标已经退化成行数，压测结论不可信"
        )

    # —— 腿 2：完整扫描（六路取数 → 幂等落库 → 逐条口径）——
    if os.path.exists(db_path):
        os.remove(db_path)
    os.environ["HIS_DB_DSN"] = dsn
    conn = connect(db_path)
    try:
        tracemalloc.start()
        started = time.perf_counter()
        outcome = scan(
            conn,
            period,
            inbox_root=fixture_root,
            contracts_dir=contracts_dir,
        )
        result.scan_seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result.scan_peak_mb = peak / 1048576

        for ch in outcome.channels:
            result.rows[ch.channel] = ch.row_count
            result.amounts[ch.channel] = ch.total_amount
        result.check_total = outcome.scan.checks_total
        result.check_failed = outcome.scan.checks_failed
        result.gate = outcome.gate
        result.warnings.extend(outcome.warnings)

        # —— 腿 3：六路对齐 ——
        tracemalloc.start()
        started = time.perf_counter()
        counts = _align_from_db(conn, period)
        result.align_seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result.align_peak_mb = peak / 1048576
        result.align_counts = counts

        # —— 幂等重放：同账期再扫一次，行数与金额必须一字不变 ——
        before = _snapshot(conn, period)
        outcome2 = scan(
            conn,
            period,
            inbox_root=fixture_root,
            contracts_dir=contracts_dir,
            reopen=True,
        )
        after = _snapshot(conn, period)
        result.replay_rows_inserted = sum(c.inserted for c in outcome2.channels)
        result.replay_rows_skipped = sum(c.skipped_duplicates for c in outcome2.channels)
        result.replay_amount_stable = before == after
    finally:
        conn.close()
        os.environ.pop("HIS_DB_DSN", None)

    payload = result.to_dict()
    payload["manifest_counts"] = manifest.get("counts", {})
    payload["expected_fail_layers"] = manifest.get("expected_fail_layers", [])
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return result


def _snapshot(conn: sqlite3.Connection, period: str) -> dict[str, Any]:
    """账期当前形态：逐渠道行数与 Decimal 合计（幂等对拍的比对基准）。

    刻意**不用** SQL 侧 ``SUM(amount_text)``：金额存的是定点字面量，SQL 求和
    只能走 REAL，那一分钱的浮点残影会让「两次扫描金额相等」这条断言时对时错。
    与 :func:`reconq.store.bill_store.row_summary` 同一口径。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in query_period(conn, period):
        buckets.setdefault(str(r["channel"]), []).append(r)
    out: dict[str, Any] = {}
    for ch, items in sorted(buckets.items()):
        total = Decimal("0")
        for item in items:
            total += Decimal(str(item["amount_text"]))
        out[ch] = {"rows": len(items), "total": str(total)}
    return out


def _align_from_db(conn: sqlite3.Connection, period: str) -> dict[str, int]:
    """从库里读回六路行做一次 :func:`align_all`，返回各状态条数。"""
    rows = query_period(conn, period)
    by_channel: dict[str, list[AlignRecord]] = {}
    for raw in rows:
        rec = record_from_row_dict(raw)
        by_channel.setdefault(rec.channel, []).append(rec)
    his_rows = by_channel.pop("his", [])
    window = period_window(period, to_session_tz(None))
    result = align_all(by_channel, his_rows, period=period, window=window)
    counts: dict[str, int] = {}
    for ch_result in result.channels.values():
        for status, n in ch_result.counts().items():
            counts[status] = counts.get(status, 0) + n
    if result.his_only:
        counts["his_only"] = counts.get("his_only", 0) + len(result.his_only)
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="六路各 2000+ 行的规模压测")
    ap.add_argument("--fixtures", default="out/scale", help="gen_fixtures.py 的输出根目录")
    ap.add_argument("--period", default=DEFAULT_PERIOD)
    ap.add_argument("--db", default="out/bench-scan.sqlite")
    ap.add_argument("--contracts", default="contracts")
    ap.add_argument("--out", default="out/bench.json")
    args = ap.parse_args(argv)

    result = run_bench(
        fixture_root=args.fixtures,
        period=args.period,
        db_path=args.db,
        contracts_dir=args.contracts,
        out_path=args.out,
    )
    print(result.render())
    print(f"\n机器可读结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
