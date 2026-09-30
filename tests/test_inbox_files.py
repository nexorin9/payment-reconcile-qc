"""文件通道取数的脏数据断言（Task 5）。

覆盖六路样例账单（微信 UTF-8 / 支付宝 GBK 带抬头 / 银联 GBK-制表符 /
自助机 UTF-8-BOM-竖线 / 医保 UTF-8-分号）与五类脏数据：

* 编码：GBK、UTF-8-BOM、不可解字节兜底；
* 金额：千分位、货币符号、括号负数、占位短横、非数字；
* 分隔符：自动判定、契约列数优先、歧义拒绝猜；
* 脏行：列数不符不静默丢、缺必填列显式报。
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from reconq.contracts.loader import load_contract
from reconq.errors import ReconqError
from reconq.sources.inbox_files import (
    detect_delimiter,
    detect_encoding,
    parse_amount,
    read_bill_file,
    read_inbox_dir,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS = os.path.join(os.path.dirname(HERE), "contracts")
BILLS = os.path.join(HERE, "fixtures", "bills", "2026-W36")
DIRTY = os.path.join(HERE, "fixtures", "dirty")
PERIOD = "2026-W36"


def _table(channel: str):
    return load_contract(os.path.join(CONTRACTS, f"{channel}.yml")).table()


def _read(channel: str, filename: str, base: str = BILLS):
    """``base`` 是渠道目录的父目录；脏数据样例平铺在 ``fixtures/dirty``。"""
    return read_bill_file(os.path.join(base, channel, filename), _table(channel), channel)


def _read_dirty(channel: str, filename: str):
    return read_bill_file(os.path.join(DIRTY, filename), _table(channel), channel)


# —— 六路样例账单读通 ——————————————————————————————————————————


def test_wechat_utf8_sample_reads_20_rows():
    rows = _read("wechat", "wechat_tradebill_2026-W36.csv")
    assert len(rows) == 20
    assert all(r.source_file == "wechat_tradebill_2026-W36.csv" for r in rows)
    # 行号 1 基且跳过抬头
    assert rows[0].row_no == 2


def test_alipay_gbk_sample_reads_all_rows():
    rows = _read("alipay", "alipay_bill_2026-W36.csv")
    assert len(rows) == 7
    # 千分位金额被洗成 Decimal 字面量
    assert Decimal(rows[0].values["amount"]) == Decimal("1710.55")
    assert rows[0].values["out_trade_no"] == "EX-ALIPAY-0001"


def test_unionpay_gbk_tab_separated_reads():
    rows = _read("unionpay", "unionpay_bill_2026-W36.txt")
    assert len(rows) == 6
    assert rows[0].values["out_trade_no"] == "EX-UPL-0001"
    assert Decimal(rows[0].values["amount"]) == Decimal("113.00")


def test_self_service_utf8_bom_pipe_separated_reads():
    rows = _read("self_service", "selfservice_2026-W36.csv")
    assert len(rows) == 5
    # 表头第一列是「自助机流水号」，BOM 不得残留进列名或值
    assert not rows[0].values["out_trade_no"].startswith("﻿")
    assert rows[0].values["terminal_no"].startswith("EXAM-TERM-")
    assert rows[0].values["pay_time"].startswith("2026-09-")


def test_medical_ins_semicolon_file_reads():
    rows = _read("medical_ins", "medical_ins_2026-W36.csv")
    assert len(rows) == 5
    # 勾稽等式：合计 = 统筹 + 补充 + 救助 + 个人负担
    assert Decimal(rows[0].values["total_amount"]) == Decimal("240.00")
    assert Decimal(rows[0].values["personal_burden"]) == Decimal("98.00")


def test_read_inbox_dir_reads_whole_channel_folder():
    rows = read_inbox_dir(os.path.dirname(BILLS), PERIOD, "unionpay", _table("unionpay"))
    assert len(rows) == 6
    assert read_inbox_dir(os.path.dirname(BILLS), PERIOD, "not_a_channel", _table("unionpay")) == []


# —— 编码判定 ————————————————————————————————————————————————


def test_detect_encoding_four_levels():
    assert detect_encoding(b"\xef\xbb\xbfhello").encoding == "utf-8-sig"
    assert detect_encoding("交易时间\n".encode("utf-8")).encoding == "utf-8"
    gbk = "订单号\n".encode("gb18030")
    assert detect_encoding(gbk).encoding == "gb18030"
    broken = detect_encoding(b"\x81\x30\x81\xfe\x81\x9b\x0b\xff\xfe\x81\x40")
    assert broken.encoding == "gbk"
    assert broken.suspect is True  # 兜底路径必须自报「判据不牢」


# —— 金额清洗 ————————————————————————————————————————————————


def test_parse_amount_variants_all_decimal():
    assert parse_amount("1,020.00") == Decimal("1020.00")
    assert parse_amount("￥56.50") == Decimal("56.50")
    assert parse_amount(" 86.00 ") == Decimal("86.00")
    assert parse_amount("(86.00)") == Decimal("-86.00")  # 会计括号负数
    assert parse_amount("-") is None
    assert parse_amount("") is None


def test_amount_variants_in_file_do_not_float():
    rows = _read_dirty("unionpay", "unionpay_amount_variants.txt")
    assert [r.values["amount"] for r in rows] == [
        "1020.00",
        "56.50",
        "-86.00",
        "",
    ]
    # 定点求和不出现 0.1+0.2 类残影
    total = sum((Decimal(r.values["amount"]) for r in rows if r.values["amount"]), Decimal("0"))
    assert total == Decimal("990.50")


def test_non_numeric_amount_raises_with_row_number():
    with pytest.raises(ReconqError) as exc:
        _read_dirty("unionpay", "unionpay_bad_amount.txt")
    assert exc.value.code == "AMOUNT_UNPARSABLE"
    assert ":2" in (exc.value.row_ref or "")


# —— 分隔符判定 ————————————————————————————————————————————————


def test_detect_delimiter_picks_the_multi_column_one():
    line = "订单号\t银联交易单号\t交易时间\t交易金额\t结算方式\t清算日期"
    assert detect_delimiter(line) == "\t"
    assert detect_delimiter("a,b,c") == ","


def test_detect_delimiter_prefers_contract_column_count():
    # 契约声明 6 列；表头用逗号切出 6 列，用竖线切出 5 列 → 选逗号
    line = "订单号,银联交易单号,交易时间,交易金额,结算方式,清算日期|无关"
    assert detect_delimiter(line, expected_columns=6) == ","


def test_ambiguous_delimiter_is_rejected_not_guessed():
    with pytest.raises(ReconqError) as exc:
        _read_dirty("self_service", "self_service_ambiguous_delim.csv")
    assert exc.value.code == "FORMAT_AMBIGUOUS"
    assert "拒绝猜测" in exc.value.detail


# —— 脏行与缺列 ————————————————————————————————————————————————


def test_ragged_row_is_not_silently_dropped():
    with pytest.raises(ReconqError) as exc:
        _read_dirty("unionpay", "unionpay_ragged_row.txt")
    assert exc.value.code == "COLUMN_COUNT_MISMATCH"
    assert exc.value.row_ref == "unionpay_ragged_row.txt:3"
    assert "本行 4 列" in exc.value.detail


def test_missing_required_column_lists_actual_header():
    with pytest.raises(ReconqError) as exc:
        _read_dirty("unionpay", "unionpay_missing_required.txt")
    assert exc.value.code == "BILL_COLUMN_MISSING"
    # 缺的是契约列名，错误里同时给出实际表头，人能自己比对
    assert "trade_no" in exc.value.detail  # 缺的契约列
    assert "订单号" in exc.value.detail  # 实际表头被列出来，人能自己比对


def test_blank_lines_skipped_but_data_rows_kept():
    path = os.path.join(DIRTY, "unionpay_blank_lines.txt")
    with open(path, "w", encoding="gb18030") as fh:
        fh.write(
            "订单号\t银联交易单号\t交易时间\t交易金额\t结算方式\t清算日期\t是否退款\n"
            "EX-UPL-9401\tEX-UPTRX-9401\t2026-09-01 10:00:00\t10.00\t现金\t2026-09-07\t否\n"
            "\n"
            "\t\t\t\t\t\t\t\n"
            "EX-UPL-9402\tEX-UPTRX-9402\t2026-09-01 10:00:01\t20.00\t现金\t2026-09-07\t否\n"
        )
    rows = read_bill_file(path, _table("unionpay"), "unionpay")
    assert [r.row_no for r in rows] == [2, 5]  # 空行与全空行剔了，行号仍指源文件真实行
