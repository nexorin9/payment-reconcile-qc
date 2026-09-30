"""渠道账单幂等落库测试（Task 7）。

验证四件事（对应 task steps 的四项断言）：

1. **首次写入**行数与合计正确；
2. **同键重写**行数不变、合计金额不变（``total_amount`` 是全表合计，
   不是本批合计 —— 后者在重放时会变成 0，看不出「重放没改数据」）；
3. **事务中断回滚**：整批要么全成要么全不成，不留半截；
4. **``bill_date_inferred`` 标记**：账单日期缺失时回落并打标，不静默补假日期。

另加几组加固断言：float 入库被拒、批内重复行只进一条、缺单号显式报错、
时间戳带偏移、Decimal 无浮点残影。
"""

from __future__ import annotations

import os
import sqlite3
from decimal import Decimal

import pytest

from reconq.engine.checks import BillRow
from reconq.errors import ReconqError
from reconq.sources.timezone import period_window
from reconq.store import bill_store
from reconq.store.bill_store import (
    CHANNEL_COLUMN_MAP,
    BillRecord,
    build_record,
    compute_idempotency_key,
    ingest_channel_rows,
    parse_trade_time,
    query_period,
    row_summary,
    to_amount_text,
    upsert_bill_rows,
)
from reconq.store.schema import BILL_TABLE, connect, init_schema

PERIOD = "2026-W36"


def make_row(
    order_no: str,
    amount: str = "128.00",
    *,
    trade_time: str = "2026-09-07 09:12:03",
    refund: str = "",
    source_file: str = "wechat_tradebill_2026-W36.csv",
    row_no: int = 5,
) -> BillRow:
    """构造一行脱敏微信账单。单号与金额用占位类目，不含患者可识别明细。"""
    return BillRow(
        values={
            "out_trade_no": order_no,
            "transaction_id": f"TXN-{order_no}",
            "amount": amount,
            "success_time": trade_time,
            "trade_state": "SUCCESS",
            "refund": refund,
        },
        row_no=row_no,
        raw_line=f"EX,{order_no},{amount}",
        source_file=source_file,
    )


@pytest.fixture()
def conn():
    connection = connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture()
def window():
    return period_window(PERIOD)


# --------------------------------------------------------------------------
# 1. 首次写入
# --------------------------------------------------------------------------


def test_first_write_row_count_and_total(conn, window):
    rows = [
        make_row("EX-WX-0001", "128.00", row_no=5),
        make_row("EX-WX-0002", "56.50", row_no=6),
        make_row("EX-WX-0003", "1020.00", row_no=7),
    ]
    outcome = ingest_channel_rows(
        conn, rows, channel="wechat", period=PERIOD, window=window
    )
    assert outcome.inserted == 3
    assert outcome.skipped_duplicates == 0
    # 128.00 + 56.50 + 1020.00 = 1204.50
    assert outcome.total_amount == "1204.50"

    stored = query_period(conn, PERIOD, "wechat")
    assert len(stored) == 3
    assert [r["channel_order_no"] for r in stored] == [
        "EX-WX-0001",
        "EX-WX-0002",
        "EX-WX-0003",
    ]
    # 金额以定点字面量存文本，不是二进制浮点
    assert stored[0]["amount_text"] == "128.00"
    assert isinstance(stored[0]["amount_text"], str)
    # 唯一索引存在
    indexes = {
        r["name"]: r["unique"]
        for r in conn.execute("PRAGMA index_list(bill_rows)")
    }
    assert indexes["ux_bill_identity"] == 1


def test_decimal_no_float_residue(conn, window):
    """0.1 + 0.2 必须精确等于 0.30，不留二进制残影。"""
    rows = [make_row("EX-WX-A", "0.10"), make_row("EX-WX-B", "0.20")]
    outcome = ingest_channel_rows(
        conn, rows, channel="wechat", period=PERIOD, window=window
    )
    assert outcome.total_amount == "0.30"
    assert "0.30000000000000004" not in outcome.total_amount


# --------------------------------------------------------------------------
# 2. 同键重写：行数不变、合计不变（幂等的核心判据）
# --------------------------------------------------------------------------


def test_replay_same_key_keeps_row_count_and_total(conn, window):
    rows = [
        make_row("EX-WX-0001", "128.00", row_no=5),
        make_row("EX-WX-0002", "56.50", row_no=6),
    ]
    first = ingest_channel_rows(
        conn, rows, channel="wechat", period=PERIOD, window=window
    )
    second = ingest_channel_rows(
        conn, rows, channel="wechat", period=PERIOD, window=window
    )
    third = ingest_channel_rows(
        conn, rows, channel="wechat", period=PERIOD, window=window
    )

    assert first.as_tuple() == (2, 0, "184.50")
    assert second.inserted == 0
    assert second.skipped_duplicates == 2
    assert second.total_amount == first.total_amount == "184.50"
    assert third.total_amount == first.total_amount
    assert len(query_period(conn, PERIOD, "wechat")) == 2


def test_replay_with_extra_rows_only_adds_new_ones(conn, window):
    """重跑时账单比上次多出一笔：只应新增那一笔，合计相应增加。"""
    first = ingest_channel_rows(
        conn,
        [make_row("EX-WX-0001", "128.00")],
        channel="wechat",
        period=PERIOD,
        window=window,
    )
    second = ingest_channel_rows(
        conn,
        [make_row("EX-WX-0001", "128.00"), make_row("EX-WX-0002", "56.50")],
        channel="wechat",
        period=PERIOD,
        window=window,
    )
    assert (first.inserted, first.skipped_duplicates) == (1, 0)
    assert (second.inserted, second.skipped_duplicates) == (1, 1)
    assert second.total_amount == "184.50"
    assert len(query_period(conn, PERIOD, "wechat")) == 2


def test_duplicate_within_single_batch_inserted_once(conn, window):
    """同一批里出现两条完全相同的单号：只进一条，另计为重复。"""
    records = [
        build_record(make_row("EX-WX-0001", "128.00"), channel="wechat", period=PERIOD, window=window),
        build_record(make_row("EX-WX-0001", "128.00"), channel="wechat", period=PERIOD, window=window),
    ]
    outcome = upsert_bill_rows(conn, records)
    assert outcome.inserted == 1
    assert outcome.skipped_duplicates == 1
    assert len(query_period(conn, PERIOD, "wechat")) == 1


def test_idempotency_key_is_period_channel_bill_date():
    assert compute_idempotency_key(PERIOD, "wechat", "2026-09-07") == (
        "2026-W36|wechat|2026-09-07"
    )


def test_different_period_is_not_deduped(conn, window):
    """幂等键含账期：同一笔单号在两个账期应各进一行，不是被判成重复。"""
    records = [
        build_record(
            make_row("EX-WX-0001", "128.00", trade_time="2026-09-07 09:12:03"),
            channel="wechat",
            period=PERIOD,
            window=window,
        ),
    ]
    upsert_bill_rows(conn, records)
    other = period_window("2026-W37")
    records2 = [
        build_record(
            make_row("EX-WX-0001", "128.00", trade_time="2026-09-14 09:12:03"),
            channel="wechat",
            period="2026-W37",
            window=other,
        ),
    ]
    upsert_bill_rows(conn, records2)
    assert len(query_period(conn, PERIOD, "wechat")) == 1
    assert len(query_period(conn, "2026-W37", "wechat")) == 1


# --------------------------------------------------------------------------
# 3. 事务中断回滚
# --------------------------------------------------------------------------


def test_transaction_rolls_back_whole_batch(conn, window):
    records = [
        build_record(make_row(f"EX-WX-{i:04d}", "10.00", row_no=i), channel="wechat",
                     period=PERIOD, window=window)
        for i in range(1, 6)
    ]
    with pytest.raises(RuntimeError, match="测试注入的中断"):
        upsert_bill_rows(conn, records, fail_after=3)

    # 整批回滚：一条都不该留下（半截落库比不出结论更危险）
    assert query_period(conn, PERIOD, "wechat") == []
    assert conn.execute("SELECT COUNT(*) FROM bill_rows").fetchone()[0] == 0


def test_rollback_then_rerun_completes(conn, window):
    """中断后重跑补齐，且不与已有数据重复。"""
    records = [
        build_record(make_row(f"EX-WX-{i:04d}", "10.00", row_no=i), channel="wechat",
                     period=PERIOD, window=window)
        for i in range(1, 6)
    ]
    with pytest.raises(RuntimeError):
        upsert_bill_rows(conn, records, fail_after=3)
    outcome = upsert_bill_rows(conn, records)
    assert outcome.inserted == 5
    assert outcome.total_amount == "50.00"
    # 再跑一次仍为 5 行、合计不变
    again = upsert_bill_rows(conn, records)
    assert (again.inserted, again.skipped_duplicates) == (0, 5)
    assert again.total_amount == "50.00"


def test_rollback_does_not_swallow_real_errors(conn, window):
    records = [
        build_record(make_row("EX-WX-0001", "10.00"), channel="wechat",
                     period=PERIOD, window=window)
    ]
    # 触发真实异常：把表改名，让 INSERT 直接失败
    conn.execute("ALTER TABLE bill_rows RENAME TO bill_rows_moved")
    with pytest.raises(sqlite3.Error):
        upsert_bill_rows(conn, records)
    conn.execute("ALTER TABLE bill_rows_moved RENAME TO bill_rows")
    assert conn.execute("SELECT COUNT(*) FROM bill_rows").fetchone()[0] == 0


# --------------------------------------------------------------------------
# 4. bill_date_inferred 标记
# --------------------------------------------------------------------------


def test_bill_date_inferred_when_missing(conn, window):
    """账单日期缺失时由交易时间推出，并打 inferred 标记。"""
    record = build_record(
        make_row("EX-WX-0001", trade_time="2026-09-07 09:12:03"),
        channel="wechat",
        period=PERIOD,
        window=window,
    )
    assert record.bill_date == "2026-09-07"
    assert record.bill_date_inferred is True

    upsert_bill_rows(conn, [record])
    stored = query_period(conn, PERIOD, "wechat")[0]
    assert stored["bill_date"] == "2026-09-07"
    assert stored["bill_date_inferred"] == 1


def test_bill_date_explicit_not_inferred(conn, window):
    """调用方给了账单日期（如渠道按自然日取件）时不打 inferred 标记。"""
    record = build_record(
        make_row("EX-WX-0001", trade_time="2026-09-07 09:12:03"),
        channel="wechat",
        period=PERIOD,
        window=window,
        bill_date="2026-09-06",
    )
    assert record.bill_date == "2026-09-06"
    assert record.bill_date_inferred is False
    upsert_bill_rows(conn, [record])
    assert query_period(conn, PERIOD, "wechat")[0]["bill_date_inferred"] == 0


def test_bill_date_falls_back_to_period_start_without_trade_time(conn, window):
    """既无账单日期又无交易时间：回落到账期起始日并**显式打标**。

    这是「不静默补一个假日期」的落点 —— 行还在库、账期归属正确，
    但任何按日聚合的结论都能一眼看出这一天是推出来的。
    """
    row = BillRow(
        values={"out_trade_no": "EX-WX-TOTAL", "amount": "100.00", "trade_state": "SUCCESS"},
        row_no=99,
        raw_line="EX,EX-WX-TOTAL,100.00",
        source_file="wechat_tradebill_2026-W36.csv",
    )
    record = build_record(row, channel="wechat", period=PERIOD, window=window)
    assert record.bill_date == window.start.strftime("%Y-%m-%d") == "2026-08-31"
    assert record.bill_date_inferred is True
    assert record.trade_time is None


def test_inferred_count_visible_in_summary(conn, window):
    """run summary 能数出推断日期的笔数，供面板提示。"""
    ingest_channel_rows(
        conn,
        [
            make_row("EX-WX-0001", "10.00", trade_time="2026-09-07 09:12:03"),
            make_row("EX-WX-0002", "20.00", trade_time="2026-09-07 10:00:00"),
        ],
        channel="wechat",
        period=PERIOD,
        window=window,
    )
    summary = row_summary(conn, PERIOD)
    assert summary["wechat"]["row_count"] == 2
    assert summary["wechat"]["total_amount"] == "30.00"
    assert summary["wechat"]["inferred_dates"] == 2


# --------------------------------------------------------------------------
# 加固：金额 float 拒绝、单号缺失、时区归一
# --------------------------------------------------------------------------


def test_float_amount_rejected(conn):
    with pytest.raises(ReconqError) as exc:
        to_amount_text(0.1, channel="wechat")
    assert "float" in exc.value.detail
    assert exc.value.code == "AMOUNT_UNPARSABLE"


def test_str_decimal_and_none_amount():
    assert to_amount_text("128.00") == Decimal("128.00")
    assert to_amount_text("") == Decimal("0")
    assert to_amount_text(None) == Decimal("0")
    assert to_amount_text(Decimal("-86.00")) == Decimal("-86.00")


def test_missing_order_no_raises_explicit(conn, window):
    """没有渠道单号必须显式报错：否则同批所有缺单行会互相冲突。"""
    row = BillRow(
        values={"amount": "128.00", "success_time": "2026-09-07 09:12:03"},
        row_no=7,
        raw_line="EX,,128.00",
        source_file="wechat_tradebill_2026-W36.csv",
    )
    with pytest.raises(ReconqError) as exc:
        build_record(row, channel="wechat", period=PERIOD, window=window)
    assert exc.value.code == "BILL_ORDER_NO_MISSING"
    assert "20 行只进 1 行" in exc.value.detail
    assert query_period(conn, PERIOD, "wechat") == []


def test_unknown_channel_raises():
    row = make_row("EX-XX-0001")
    with pytest.raises(ReconqError) as exc:
        build_record(row, channel="wechat_alipay_x", period=PERIOD)
    assert exc.value.code == "CHANNEL_UNKNOWN"


def test_trade_time_normalized_to_offset():
    """无偏移本地时间按会话时区补偏移；带偏移的转成同一时区。"""
    naive = parse_trade_time("2026-09-07 09:12:03")
    assert naive is not None and naive.utcoffset() is not None
    assert naive.utcoffset().total_seconds() == 8 * 3600

    with_offset = parse_trade_time("2026-09-07T01:12:03Z")
    assert with_offset.utcoffset().total_seconds() == 8 * 3600
    # 同一绝对时刻
    assert with_offset == naive

    assert parse_trade_time("") is None


def test_unparsable_trade_time_raises():
    with pytest.raises(ReconqError) as exc:
        parse_trade_time("2026年9月7日", channel="wechat", row_ref="f.csv:9")
    assert exc.value.code == "TIMESTAMP_UNPARSABLE"
    assert exc.value.row_ref == "f.csv:9"


def test_cross_period_trade_time_still_gets_bucket_date(conn, window):
    """跨账期落点的交易时间按其自身日历日归账单日，不被强行改写。

    这类行是「跨日切分重复计入」的原料，落库时改写它就等于把成因抹掉了。
    """
    record = build_record(
        make_row("EX-WX-0001", trade_time="2026-09-08 00:30:00"),
        channel="wechat",
        period=PERIOD,
        window=window,
    )
    assert record.bill_date == "2026-09-08"
    assert window.is_cross_period(record.trade_time) is True


def test_six_channel_column_map_complete():
    """六路都登记了；HIS 单号只给本来就有该语义的渠道。"""
    assert set(CHANNEL_COLUMN_MAP) == {
        "wechat",
        "alipay",
        "unionpay",
        "self_service",
        "his",
        "medical_ins",
    }
    for channel, mapping in CHANNEL_COLUMN_MAP.items():
        assert mapping["order_no"], f"{channel} 缺 order_no"
        assert mapping["amount"], f"{channel} 缺 amount"
    # 微信/支付宝账单里本来就没有 HIS 收费记录主键，不能凭空补
    assert CHANNEL_COLUMN_MAP["wechat"]["his_order_no"] == ""
    assert CHANNEL_COLUMN_MAP["his"]["his_order_no"] == "his_order_no"


def test_his_rows_land_with_his_order_no(conn):
    """HIS 一路：单号就是收费记录主键，供对齐器按主键反查。"""
    row = BillRow(
        values={
            "his_order_no": "HIS-2026W36-0001",
            "charge_id": "JS-0001",
            "amount": "128.00",
            "charge_time": "2026-09-07 09:12:03",
            "department": "门诊收费室",
            "settle_type": "微信",
            "is_refund": "0",
        },
        row_no=2,
        raw_line="HIS-2026W36-0001,128.00",
        source_file="his_charge_2026-W36.csv",
    )
    outcome = ingest_channel_rows(
        conn, [row], channel="his", period=PERIOD, window=period_window(PERIOD)
    )
    assert outcome.inserted == 1
    stored = query_period(conn, PERIOD, "his")[0]
    assert stored["his_order_no"] == "HIS-2026W36-0001"
    assert stored["channel_order_no"] == "HIS-2026W36-0001"
    assert stored["department"] == "门诊收费室"
    assert stored["trade_time"].endswith("+08:00")


def test_empty_rows_returns_zero_outcome(conn):
    outcome = ingest_channel_rows(
        conn, [], channel="wechat", period=PERIOD, window=period_window(PERIOD)
    )
    assert outcome.as_tuple() == (0, 0, "0")


def test_batched_ingest_matches_single_batch(conn, window):
    """分批落库（模拟 >2000 行）与单批落库结果一致。"""
    rows = [make_row(f"EX-WX-{i:04d}", "10.00", row_no=i + 4) for i in range(25)]
    outcome = ingest_channel_rows(
        conn, rows, channel="wechat", period=PERIOD, window=window, batch_size=7
    )
    assert outcome.inserted == 25
    assert outcome.total_amount == "250.00"
    # 重跑分批也是零新增
    again = ingest_channel_rows(
        conn, rows, channel="wechat", period=PERIOD, window=window, batch_size=7
    )
    assert (again.inserted, again.skipped_duplicates) == (0, 25)
    assert again.total_amount == "250.00"


def test_init_schema_is_idempotent(conn):
    init_schema(conn)
    init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM bill_rows").fetchone()[0] == 0


def test_persist_to_file_and_reopen(tmp_path):
    """落库能跨进程：写文件后重开，行还在，合计不变。"""
    db_path = os.path.join(tmp_path, "nested", "reconq.db")
    window = period_window(PERIOD)
    rows = [make_row("EX-WX-0001", "128.00"), make_row("EX-WX-0002", "56.50")]

    first = connect(db_path)
    init_schema(first)
    ingest_channel_rows(first, rows, channel="wechat", period=PERIOD, window=window)
    first.close()

    second = connect(db_path)
    try:
        assert len(query_period(second, PERIOD, "wechat")) == 2
        replay = ingest_channel_rows(
            second, rows, channel="wechat", period=PERIOD, window=window
        )
        assert (replay.inserted, replay.skipped_duplicates) == (0, 2)
        assert replay.total_amount == "184.50"
    finally:
        second.close()
