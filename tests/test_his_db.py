"""HIS 只读库流式取数测试（Task 6）。

对照参考实现的三处语义：

* ``data_source_connection.py:393`` ``execute_query_one_by_one()``——逐行
  喂给消费方，调用方永远只看得到一行；
* 同文件 :425 ``supports_streaming_fetch()``——基类 ``False``，只有真有
  服务端游标的方言才 ``True``；
* ``postgres_data_source_connection.py:26-46, 260-287``——8MB 字节预算、
  1000 行上限、每批最多放大 4 倍、首批取 1 行探针、用**已见最宽行**反算。

本测试库用**本地 SQLite 替身**（``tests/fixtures/`` 脱敏样例，不连任何
生产 HIS 库）：SQLite 走客户端游标腿，正好把「非服务端游标方言」的
显式退回路径也测到。
"""

from __future__ import annotations

import csv
import os
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from reconq.errors import ReconqError
from reconq.sources.his_db import (
    CHARGE_COLUMNS,
    STREAM_FETCH_BUDGET_BYTES,
    STREAM_FETCH_GROWTH_FACTOR,
    STREAM_FETCH_MAX_BATCH_ROWS,
    AdaptiveBatchSizer,
    build_charge_query,
    estimate_row_bytes,
    fetch_his_charge_rows,
    probe_his_connection,
    stream_charge_rows,
)
from reconq.sources.timezone import period_window

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "his", "charge_record_2026-W36.csv"
)

PERIOD = "2026-W36"
TABLE = "charge_record"


def _load_fixture_engine(rows: int | None = None) -> Engine:
    """把脱敏样例 CSV 灌进一个本地 SQLite 替身库。

    ``rows`` 给定时复制首行凑到指定行数（规模断言用）。样例里金额、单号
    全是占位与类目，**不含患者可识别明细**。
    """
    engine = create_engine("sqlite://")
    with open(FIXTURE, newline="", encoding="utf-8") as fh:
        records = list(csv.DictReader(fh))
    if rows is not None:
        base = list(records)
        while len(records) < rows:
            for rec in base:
                if len(records) >= rows:
                    break
                clone = dict(rec)
                clone["charge_id"] = f"CHG-BULK-{len(records):05d}"
                clone["his_order_no"] = f"HIS-<CHARGE-BULK-{len(records):05d}>"
                records.append(clone)

    with engine.begin() as conn:
        cols = ", ".join(f"{c} TEXT" for c in CHARGE_COLUMNS)
        conn.execute(text(f"CREATE TABLE {TABLE} ({cols})"))
        for rec in records:
            placeholders = ", ".join(f":{c}" for c in CHARGE_COLUMNS)
            conn.execute(
                text(f"INSERT INTO {TABLE} VALUES ({placeholders})"),
                {c: rec[c] for c in CHARGE_COLUMNS},
            )
    return engine


# —— ① 批大小策略：8MB 预算 / 1000 行上限 / 4 倍爬坡 / 1 行探针 ———


def test_stream_constants_match_reference():
    """三个常量必须与参考实现同值——不是「差不多」，是同一组数字。"""
    assert STREAM_FETCH_BUDGET_BYTES == 8 * 1024 * 1024
    assert STREAM_FETCH_MAX_BATCH_ROWS == 1000
    assert STREAM_FETCH_GROWTH_FACTOR == 4


def test_first_batch_is_single_probe_row():
    """首批只取 1 行探针：先知道一行多宽，才知道后面该取多少。"""
    sizer = AdaptiveBatchSizer()
    assert sizer.batch_size == 1


def test_batch_grows_at_most_factor_four_per_step():
    """每批最多放大 4 倍。瘦行也不能一步顶到行数上限。

    对应参考实现 :35-37 注释点名的那个坑：一条 NULL 多的探针瘦行会把
    批直接顶到 1000，而此时一行胖数据都还没见过。
    """
    sizer = AdaptiveBatchSizer()
    sizes = [sizer.batch_size]
    for _ in range(6):
        prev = sizes[-1]
        nxt = sizer.observe([("x",)] * prev)  # 恒定瘦行
        assert nxt <= prev * STREAM_FETCH_GROWTH_FACTOR
        assert nxt >= prev  # 批大小单调不减
        sizes.append(nxt)
    # 1 → 4 → 16 → 64 → 256 → 1000（末步被行数上限截住）
    assert sizes == [1, 4, 16, 64, 256, 1000, 1000]


def test_batch_shrinks_immediately_after_fat_row():
    """出现一条胖行后，批大小**立即**收缩（增长才要爬坡，收缩不爬坡）。"""
    sizer = AdaptiveBatchSizer()
    for _ in range(4):
        sizer.observe([("x",)] * sizer.batch_size)
    grown = sizer.batch_size
    assert grown > 1

    fat = ("x" * (STREAM_FETCH_BUDGET_BYTES // 8),)  # 一行就占 1/8 预算
    nxt = sizer.observe([fat] + [("x",)] * (grown - 1))
    # 预算 8MB ÷ 最宽行 ≥ 1/8 预算 = 8 行；但最宽行仍含其他行，取 ≤8
    assert nxt < grown
    assert nxt >= 1


def test_estimate_row_bytes_recurses_into_containers():
    """估算必须**递归**下钻容器。

    参考实现 :41-43 点名过：浅层 getsizeof 把一个 10MB 的 json 文档算成
    几百字节，批大小就永远顶在行数上限，内存直接爆。psycopg3 会把 jsonb
    返回成已解析的 dict/list，所以这里必须递归。
    """
    doc = {"k": "v" * 50_000, "list": ["x" * 1_000 for _ in range(20)]}
    shallow = len(str(doc))  # 字符串化长度，明显小于真实驻留
    assert estimate_row_bytes([doc]) > shallow


# —— ② 只 SELECT 需要的列，不 SELECT * ———


def test_query_selects_only_needed_columns():
    """生产库上少读一列就少一份 IO。SQL 里不得出现 SELECT *。"""
    sql = build_charge_query(period_window(PERIOD))
    assert "SELECT *" not in sql.upper()
    head = sql.split("FROM")[0]
    for col in CHARGE_COLUMNS:
        assert col in head
    # 契约里没声明的列不得被顺手拉走
    assert "patient" not in sql.lower()
    assert "name" not in sql.lower()


def test_query_window_is_half_open():
    """时间窗必须半开 [start, end)：周一零点那笔属新账期，不被两期双记。"""
    sql = build_charge_query(period_window(PERIOD))
    assert "charge_time >= :window_start" in sql
    assert "charge_time < :window_end" in sql
    assert "<=" not in sql.split("WHERE")[1].split("AND")[0].replace(">=", "")


def test_empty_column_list_refuses_to_build_query():
    """取数列清单为空时显式报错，绝不退化成 SELECT * 等价查询。"""
    with pytest.raises(ReconqError) as ei:
        build_charge_query(period_window(PERIOD), columns=[])
    assert ei.value.code == "FORMAT_BROKEN"


# —— ③ 逐行流式取数端到端 ———


def test_stream_charge_rows_yields_all_fixture_rows():
    """脱敏样例 9 行全部产出，含退费红冲行与账期末日行。"""
    engine = _load_fixture_engine()
    rows = list(stream_charge_rows(engine, PERIOD, table=TABLE))
    assert len(rows) == 9
    assert rows[0].get("his_order_no") == "HIS-<CHARGE-0001>"
    # 退费红冲行保留为独立行（不与原单合并）
    refunds = [r for r in rows if r.get("is_refund") == "true"]
    assert len(refunds) == 1
    assert refunds[0].get("charge_id") == "CHG-0006"
    engine.dispose()


def test_stream_is_lazy_not_materialized():
    """主路径是**逐行**：取前 3 行不应把整个结果集走完。"""
    engine = _load_fixture_engine()
    stream = stream_charge_rows(engine, PERIOD, table=TABLE)
    first_three = [next(stream) for _ in range(3)]
    assert len(first_three) == 3
    assert first_three[0].get("charge_id") == "CHG-0001"
    stream.close()
    engine.dispose()


def test_rows_outside_period_window_excluded():
    """账期窗口外的行不得被选中——跨日切分的取数侧底线。"""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        cols = ", ".join(f"{c} TEXT" for c in CHARGE_COLUMNS)
        conn.execute(text(f"CREATE TABLE {TABLE} ({cols})"))
        rows = [
            # 窗口内（2026-W36 = 08-31 ~ 09-07）
            "HIS-<CHARGE-0001>,CHG-0001,100.00,2026-09-01 10:00:00,2026-W36,门诊收费室,微信,false",
            # 窗口外：上一账期
            "HIS-<CHARGE-0000>,CHG-0000,999.00,2026-08-30 10:00:00,2026-W35,门诊收费室,微信,false",
            # 窗口外：下一账期
            "HIS-<CHARGE-0009>,CHG-0009,888.00,2026-09-08 10:00:00,2026-W37,门诊收费室,微信,false",
        ]
        for raw in rows:
            values = raw.split(",")
            params = dict(zip(CHARGE_COLUMNS, values))
            placeholders = ", ".join(f":{c}" for c in CHARGE_COLUMNS)
            conn.execute(text(f"INSERT INTO {TABLE} VALUES ({placeholders})"), params)
    got = list(stream_charge_rows(engine, PERIOD, table=TABLE))
    assert [r.get("charge_id") for r in got] == ["CHG-0001"]
    engine.dispose()


def test_daily_windows_do_not_duplicate_rows():
    """按天切窗时，窗口半开且不重叠——同一行不得被两个窗口各取一次。"""
    engine = _load_fixture_engine()
    whole = list(stream_charge_rows(engine, PERIOD, table=TABLE, window_days=7))
    per_day = list(stream_charge_rows(engine, PERIOD, table=TABLE, window_days=1))
    assert len(whole) == len(per_day) == 9
    assert {r.get("charge_id") for r in whole} == {r.get("charge_id") for r in per_day}
    engine.dispose()


# —— ④ 结果摘要（写进 run summary，Task 20 复用）——


def test_fetch_result_summary_is_complete():
    """取数摘要的字段必须齐全，且金额是 Decimal 定点串。"""
    engine = _load_fixture_engine()
    result = fetch_his_charge_rows(PERIOD, engine=engine, table=TABLE)
    assert result.row_count == 9
    assert result.windows == 7  # 一个账期按天切成 7 窗
    assert result.batches > 0
    assert result.widest_row_bytes > 0
    assert result.stream == "buffered"  # SQLite 无服务端游标，显式退回
    # 120 + 86.5 + 1500 + 42 + 260 + 120 + 3200 + 75 + 18.6 = 5422.10
    assert Decimal(result.total_amount) == Decimal("5422.10")
    assert isinstance(result.elapsed_ms, int)
    engine.dispose()


def test_batches_count_fetch_round_trips_not_rows():
    """批次数 = FETCH 往返次数，**不是行数**。

    这条曾经被写错过（按行累加，2000 行就报 2000 批），症状是自适应分批
    看起来完全没生效：批大小涨到 1000 也没用，因为「批数」恒等于行数。
    2000 行按 1→4→16→64→256→1000 的爬坡走完，批数应远小于行数。
    """
    engine = _load_fixture_engine(rows=2000)
    result = fetch_his_charge_rows(PERIOD, engine=engine, table=TABLE)
    assert result.row_count == 2000
    # 每个时间窗一批 1 行探针起步；按天切 7 窗，故批数 ≥ 7
    assert result.batches >= result.windows
    # 关键：批数必须显著少于行数（否则等于没分批）
    assert result.batches < result.row_count / 10
    engine.dispose()


def test_batches_scale_with_row_count():
    """行数上去，批次数必须跟着上去（而不是恒为 1 或恒为 0）。"""
    engine = _load_fixture_engine(rows=2000)
    result = fetch_his_charge_rows(PERIOD, engine=engine, table=TABLE)
    assert result.row_count == 2000
    assert result.batches > 0
    assert result.batches < result.row_count
    engine.dispose()


def test_missing_dsn_reports_named_env_var(monkeypatch):
    """缺 HIS_DB_DSN 时点名变量名，且不建任何连接。"""
    monkeypatch.delenv("HIS_DB_DSN", raising=False)
    with pytest.raises(ReconqError) as ei:
        fetch_his_charge_rows(PERIOD, dsn="")
    assert ei.value.code in ("HIS_DB_DSN_MISSING", "CREDENTIAL_MISSING")
    assert "HIS_DB_DSN" in ei.value.detail


# —— ⑤ probe：连通性预检只问「通不通」，不问「有多少数据」——


def test_probe_missing_dsn_names_env_var(monkeypatch):
    monkeypatch.delenv("HIS_DB_DSN", raising=False)
    result = probe_his_connection(env={})
    assert result["ok"] is False
    assert result["error_code"] == "HIS_DB_DSN_MISSING"
    assert "HIS_DB_DSN" in result["credential_env_vars"]
    assert result["connected"] is False


def test_probe_unknown_dialect_reports_driver_missing():
    """方言驱动没装时点明是驱动问题，不猜是网络问题。"""
    result = probe_his_connection(dsn="weirddb://user:pw@host/db")
    assert result["ok"] is False
    assert result["error_code"] == "HIS_DB_DRIVER_MISSING"


def test_probe_reachable_db_reports_connected(tmp_path):
    """本地 SQLite 替身：连通 → detail 里说清走的哪条游标腿。"""
    db = tmp_path / "his_probe.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text(f"CREATE TABLE {TABLE} (his_order_no TEXT)"))
    eng.dispose()

    result = probe_his_connection(dsn=f"sqlite:///{db}")
    assert result["ok"] is True
    assert result["connected"] is True
    assert "客户端游标" in result["detail"]


def test_probe_missing_table_reports_db_error(tmp_path):
    """表不存在时不能报「连通正常」——data-source test 要真查一次。"""
    db = tmp_path / "his_empty.db"
    result = probe_his_connection(dsn=f"sqlite:///{db}")
    assert result["ok"] is False
    assert result["connected"] is False
    assert result["error_code"] in ("HIS_DB_NOT_EXIST", "HIS_DB_UNREACHABLE")

