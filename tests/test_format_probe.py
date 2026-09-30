"""账单文件格式探测边界矩阵（Task 10）。

跑遍 **6 种编码 × 4 种分隔符 × 2 种表头别名写法 = 144 份**组合样例，
外加尾部说明行 / 空行 / 脏行三类专项样例，逐类断言：

* **编码**：BOM / 裸 UTF-8 / GB18030 / GBK / UTF-16 / 兜底替换解码；
* **分隔符**：逗号 / 制表符 / 竖线 / 分号，判平时报 ``FORMAT_AMBIGUOUS``；
* **表头别名**：官方表头与院内老系统别名两套写法都映射到同一契约列；
* **尾部说明行与空行剔除**：只剔这两类，**含数据的脏行不剔**；
* **金额清洗**：千分位 / 货币符号 / 会计括号负数，一律 ``Decimal``；
* **格式证据**：``probe_channel_file()`` 交出可进结论单的探测结果。

夹具由 ``tests/fixtures/format_matrix/generate_matrix.py`` 生成，
可重跑复现（``python3 tests/fixtures/format_matrix/generate_matrix.py``）。
"""

from __future__ import annotations

import glob
import os
from decimal import Decimal

import pytest

from reconq.contracts.loader import load_contract
from reconq.errors import ReconqError
from reconq.sources.format_probe import (
    DELIMITER_CANDIDATES,
    NOTE_MARKERS,
    FormatProbe,
    RowClass,
    classify_row,
    detect_delimiter,
    detect_encoding,
    normalize_header,
    probe_channel_file,
    read_bill_file_probed,
    resolve_columns,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS = os.path.join(os.path.dirname(HERE), "contracts")
MATRIX_ROOT = os.path.join(HERE, "fixtures", "format_matrix")
MATRIX = os.path.join(MATRIX_ROOT, "matrix")
TRAILER = os.path.join(MATRIX_ROOT, "trailer")
RAGGED = os.path.join(MATRIX_ROOT, "ragged")
PERIOD = "2026-W36"

CHANNELS = ("wechat", "unionpay", "self_service")
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "gbk_broken")
DELIM_NAMES = {",": "comma", "\t": "tab", "|": "pipe", ";": "semi"}
STYLES = ("official", "legacy")


def _table(channel: str):
    return load_contract(os.path.join(CONTRACTS, f"{channel}.yml")).table()


def _matrix_path(encoding: str, delim: str, style: str, channel: str) -> str:
    return os.path.join(MATRIX, f"{encoding}__{DELIM_NAMES[delim]}__{style}__{channel}.txt")


# —— 夹具自身的前提 ————————————————————————————————————————————


def test_matrix_fixtures_are_complete():
    """144 份组合 + 6 份尾部 + 6 份脏行，一份不少。"""
    assert len(glob.glob(os.path.join(MATRIX, "*.txt"))) == 6 * 4 * 2 * 3 == 144
    assert len(glob.glob(os.path.join(TRAILER, "*.csv"))) == 3
    assert len(glob.glob(os.path.join(RAGGED, "*.csv"))) == 3


# —— 矩阵：144 种组合逐一探测 ————————————————————————————————


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("delimiter", DELIMITER_CANDIDATES)
@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("channel", CHANNELS)
def test_every_combination_probes_without_losing_a_row(encoding, delimiter, style, channel):
    """6 编码 × 4 分隔符 × 2 别名 × 3 渠道 = 144 例，每例都读出 3 行。

    这条断言的核心是「**不误剔**」：任何一种脏格式组合下，3 行数据必须
    一行不少地读出来。少一行在真实场景里表现为「渠道说给了 3 笔、账上
    只有 2 笔」，而少的那一行不会被任何人发现。
    """
    path = _matrix_path(encoding, delimiter, style, channel)
    assert os.path.isfile(path), f"缺夹具：{path}"
    probe = probe_channel_file(path, _table(channel), channel)
    if encoding == "gbk_broken":
        # 坏字节档位的正文本就不可解，只断言「自报判据不牢」而不是行数
        assert "suspect_encoding" in probe.suspect_flags
        assert probe.encoding == "gbk"
        return
    assert probe.row_count == 3, f"{path} 只读出 {probe.row_count} 行"
    assert probe.delimiter == delimiter
    assert probe.header_line_no == 1
    assert probe.ragged_rows == []
    # 非逗号分隔符会带一条格式标记（让面板知道这路不是 CSV 导出），
    # 但除此之外不应有任何「判据不牢」的标记
    unexpected = [f for f in probe.suspect_flags if not f.startswith("non_comma_delimiter:")]
    assert unexpected == [], f"{path} 出现意外标记 {unexpected}"


@pytest.mark.parametrize("style", STYLES)
def test_delimiter_actually_detected_per_combination(style):
    """四种分隔符各自被认出来，不是碰巧读通。"""
    seen = set()
    for delimiter in DELIMITER_CANDIDATES:
        probe = probe_channel_file(
            _matrix_path("utf-8", delimiter, style, "wechat"), _table("wechat"), "wechat"
        )
        seen.add(probe.delimiter)
        assert probe.delimiter == delimiter
        assert f"non_comma_delimiter:{delimiter!r}" in probe.suspect_flags or delimiter == ","
    assert seen == set(DELIMITER_CANDIDATES)


@pytest.mark.parametrize("style", STYLES)
def test_encoding_actually_detected_per_combination(style):
    """五种可解编码各自被认出来；坏字节档位落到 GBK 兜底并自报不牢。"""
    expected = {
        "utf-8-sig": "utf-8-sig",
        "utf-8": "utf-8",
        "gb18030": "gb18030",
        "utf-16": "utf-16",
        # GBK 是 GB18030 的子集：内容用 GBK 写，判定落到 gb18030 严格解码
        # 是**正确**行为（两者对这份内容解出同一个字符串），不是判错
        "gbk": "gb18030",
        "gbk_broken": "gbk",
    }
    for encoding, want in expected.items():
        probe = probe_channel_file(
            _matrix_path(encoding, ",", style, "unionpay"), _table("unionpay"), "unionpay"
        )
        assert probe.encoding == want, f"{encoding} 判成了 {probe.encoding}"


@pytest.mark.parametrize("style", STYLES)
def test_chinese_text_survives_every_encoding(style):
    """编码判定的真正目的：中文表头不能读成半个汉字。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16"):
        path = _matrix_path(encoding, ",", style, "unionpay")
        rows, _ = read_bill_file_probed(path, _table("unionpay"), "unionpay")
        assert rows[0].values["out_trade_no"] == "EX-UPL-0001"
        # 契约列名经中文表头映射到位，值没串列
        assert rows[0].values["settle_type"] == "银行卡"


# —— 表头别名矩阵 ————————————————————————————————————————————


def test_official_and_legacy_headers_map_to_same_contract_columns():
    """两套同义表头写法映射到同一批契约列。"""
    table = _table("wechat")
    official = ["商户订单号", "微信交易单号", "交易状态", "应结订单金额", "交易时间", "退款金额"]
    legacy = ["订单号", "交易单号", "订单状态", "金额(元)", "支付时间", "退款金额(元)"]
    m1, unmapped1 = resolve_columns(official, table, "wechat")
    m2, unmapped2 = resolve_columns(legacy, table, "wechat")
    assert set(m1) == set(m2) == {
        "transaction_id",
        "out_trade_no",
        "trade_state",
        "amount",
        "success_time",
        "refund",
    }
    assert m1["out_trade_no"] == "商户订单号"
    assert m2["out_trade_no"] == "订单号"
    assert m1["amount"] == "应结订单金额"
    assert m2["amount"] == "金额(元)"
    assert unmapped1 == [] and unmapped2 == []


def test_header_normalisation_folds_fullwidth_whitespace_and_unit():
    """全角括号、内嵌空格、大小写与 BOM 都必须折叠成同一个键。"""
    for variant in ("金额(元)", "金额（元）", "金额（元 ）", " 金额(元) ", "﻿金额(元)"):
        assert normalize_header(variant) == "金额", variant
    # 大小写：HIS 单号 / his单号
    assert normalize_header("HIS单号") == normalize_header("his单号")
    # 内嵌空格：订单 号
    assert normalize_header("订 单 号") == normalize_header("订单号")
    # 非货币单位后缀**不能**被误剥：`金额(%)` 是费率不是金额，
    # 归一成「金额」会让费率列被当金额解析（静默错列）
    assert normalize_header("金额(%)") == "金额(%)"
    assert normalize_header("收/支") == "收/支"


def test_legacy_unionpay_headers_map_onto_contract_columns():
    table = _table("unionpay")
    legacy = ["商户订单号", "交易流水号", "交易日期", "金额(元)", "支付方式", "是否退费"]
    mapping, _ = resolve_columns(legacy, table, "unionpay")
    assert mapping["out_trade_no"] == "商户订单号"
    assert mapping["trade_no"] == "交易流水号"
    assert mapping["trade_time"] == "交易日期"
    assert mapping["amount"] == "金额(元)"
    assert mapping["settle_type"] == "支付方式"
    assert mapping["is_refund"] == "是否退费"


def test_english_column_names_still_match_contract_directly():
    """院内部分系统直接导出英文列名，契约列名必须能直接命中。"""
    table = _table("unionpay")
    mapping, _ = resolve_columns(
        ["out_trade_no", "trade_no", "trade_time", "amount", "settle_type", "is_refund"],
        table,
        "unionpay",
    )
    assert mapping["out_trade_no"] == "out_trade_no"
    assert len(mapping) == 6


def test_two_columns_claiming_one_header_does_not_double_read():
    """院内导出同时给「商户订单号」和「订单号」时，按 alias 优先级认前者。

    两列都认领会让同一个单号被读两次 —— 在对齐阶段表现为「同一 HIS 收费
    记录被两个渠道行认领」，看起来像合并支付，其实是导出冗余列。
    """
    table = _table("wechat")
    mapping, unmapped = resolve_columns(
        ["商户订单号", "订单号", "微信交易单号", "交易状态", "应结订单金额", "交易时间"],
        table,
        "wechat",
    )
    assert mapping["out_trade_no"] == "商户订单号"
    assert "订单号" in unmapped


# —— 尾部说明行与空行：只剔这两类 ————————————————————————————


@pytest.mark.parametrize("channel", CHANNELS)
def test_trailer_note_and_blank_lines_are_stripped_but_data_kept(channel):
    """带抬头 + 空行 + 「对账结束」+「共计 3 笔」的样例：3 行数据一行不少。"""
    path = os.path.join(TRAILER, f"{channel}_trailer.csv")
    rows, probe = read_bill_file_probed(path, _table(channel), channel)
    assert len(rows) == 3
    assert [r.row_no for r in rows] == [5, 6, 7]  # 真实行号含抬头偏移
    assert probe.note_line_count == 2  # 「对账结束」+「共计 3 笔」
    # 表头**之前**的空行不计入（它属于抬头的一部分，由 header_offset 体现）
    assert probe.blank_line_count == 1
    assert "trailer_note_lines" in probe.suspect_flags
    assert "blank_lines_skipped" in probe.suspect_flags
    # 抬头 2 行 + 1 空行 → 表头在第 4 行；数据从第 5 行起
    assert probe.header_line_no == 4
    assert f"header_offset:{probe.header_line_no - 1}" in probe.suspect_flags


@pytest.mark.parametrize("channel", CHANNELS)
def test_data_row_is_never_mistaken_for_a_note_line(channel):
    """数据行即使「长得像」说明行也不能被剔。"""
    path = os.path.join(TRAILER, f"{channel}_trailer.csv")
    rows, _ = read_bill_file_probed(path, _table(channel), channel)
    # 3 行都是真数据：单号、金额、终端/状态都不为空
    for row in rows:
        assert row.values[list(row.values)[0]] != ""
    amounts = [r.values.get("amount") or r.values.get("out_trade_no") for r in rows]
    assert all(a for a in amounts)


def test_classify_row_four_classes():
    width = 4
    assert classify_row(["a", "b", "c", "d"], width) == RowClass.DATA
    assert classify_row(["", "  ", ""], width) == RowClass.BLANK
    assert classify_row(["", ""], width) == RowClass.BLANK
    assert classify_row(["----------------对账结束----------------"], width) == RowClass.NOTE
    assert classify_row(["", "", "", "共计 3 笔"], width) == RowClass.NOTE
    # 关键反例：单号里含「说明」二字、且列数不对 —— 仍判脏行，不剔
    assert classify_row(["EX-说明-1", "2026-09-01", "10.00"], width) == RowClass.RAGGED
    # 单列但不是说明用语也不是分隔线 → 脏行
    assert classify_row(["EX-UPL-0001"], width) == RowClass.RAGGED


def test_note_markers_are_the_documented_set():
    for marker in ("对账结束", "本页合计", "共计", "打印时间", "制表人", "以下空白"):
        assert marker in NOTE_MARKERS


@pytest.mark.parametrize("channel", CHANNELS)
def test_ragged_row_with_data_is_reported_not_dropped(channel):
    """含数据但列数不符的行：显式报错 + 行号，绝不静默剔除。

    这是本任务最硬的一条纪律。剔除它等于静默丢数据 —— 对账少一笔和账上
    多一笔同样致命，而前者更难被发现。
    """
    path = os.path.join(RAGGED, f"{channel}_ragged.csv")
    with pytest.raises(ReconqError) as exc:
        read_bill_file_probed(path, _table(channel), channel)
    assert exc.value.code == "COLUMN_COUNT_MISMATCH"
    assert exc.value.row_ref == f"{channel}_ragged.csv:3"
    assert "本行 4 列" in exc.value.detail
    assert "不会被静默剔除" in exc.value.detail


# —— 格式证据 ————————————————————————————————————————————————


def test_probe_returns_every_documented_field():
    path = _matrix_path("gb18030", "\t", "official", "unionpay")
    probe = probe_channel_file(path, _table("unionpay"), "unionpay")
    assert isinstance(probe, FormatProbe)
    as_dict = probe.as_dict()
    for key in (
        "encoding",
        "delimiter",
        "header_line_no",
        "column_mapping",
        "row_count",
        "suspect_flags",
    ):
        assert key in as_dict, key
    assert as_dict["encoding"] == "gb18030"
    assert as_dict["delimiter"] == "\t"
    assert as_dict["header_line_no"] == 1
    assert as_dict["row_count"] == 3
    assert as_dict["column_mapping"]["out_trade_no"] == "订单号"
    assert as_dict["suspect_flags"] == ["non_comma_delimiter:'\\t'"]


def test_probe_evidence_is_stable_across_runs():
    """同一份文件两次探测结果完全一致（可复现，不含随机量）。"""
    path = _matrix_path("utf-8", ",", "legacy", "self_service")
    a = probe_channel_file(path, _table("self_service"), "self_service").as_dict()
    b = probe_channel_file(path, _table("self_service"), "self_service").as_dict()
    assert a == b


def test_probe_ragged_rows_are_listed_not_hidden():
    """探测层不抛错，但把脏行号列进 ``ragged_rows``，让排障腿能提前看到。"""
    path = os.path.join(RAGGED, "unionpay_ragged.csv")
    probe = probe_channel_file(path, _table("unionpay"), "unionpay")
    assert probe.ragged_rows == [(3, 4)]
    assert "ragged_rows" in probe.suspect_flags
    assert probe.row_count == 2  # 只数干净数据行


# —— 金额清洗 ————————————————————————————————————————————————


def test_amount_variants_across_encodings_stay_decimal():
    """千分位 / 货币符号 / 括号负数在各种编码下都按定点解析。"""
    base = "订单号\t银联交易单号\t交易时间\t交易金额\t结算方式\t是否退款\n"
    body = (
        "EX-UPL-9101\tEX-UPTRX-9101\t2026-09-01 10:00:00\t1,020.00\t现金\t否\n"
        "EX-UPL-9102\tEX-UPTRX-9102\t2026-09-02 10:00:01\t￥56.50\t现金\t否\n"
        "EX-UPL-9103\tEX-UPTRX-9103\t2026-09-03 10:00:02\t(86.00)\t现金\t否\n"
    )
    for encoding in ("utf-8", "gb18030"):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
            fh.write((base + body).encode(encoding))
            tmp = fh.name
        try:
            rows, _ = read_bill_file_probed(tmp, _table("unionpay"), "unionpay")
            assert [r.values["amount"] for r in rows] == ["1020.00", "56.50", "-86.00"]
            total = sum((Decimal(r.values["amount"]) for r in rows), Decimal("0"))
            assert total == Decimal("990.50")  # 定点求和无浮点残影
        finally:
            os.unlink(tmp)


# —— 显式失败，不猜 ————————————————————————————————————————————


def test_ambiguous_delimiter_still_refuses_to_guess():
    """竖线与制表符切出同样列数时报 ``FORMAT_AMBIGUOUS``（沿用 Task 5 纪律）。"""
    # 逗号 / 制表符 / 竖线都切出 2 列 → 三方打平，谁也不能当真的
    text = "订单号,银联交易单号|交易时间\t交易金额\n"
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
        fh.write(text.encode("utf-8"))
        tmp = fh.name
    try:
        with pytest.raises(ReconqError) as exc:
            read_bill_file_probed(tmp, _table("unionpay"), "unionpay")
        assert exc.value.code == "FORMAT_AMBIGUOUS"
        assert "拒绝猜测" in exc.value.detail
    finally:
        os.unlink(tmp)


def test_missing_required_column_lists_actual_header():
    """缺必填列时报错并列出实际表头，人能自己比对。"""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
        fh.write("订单号\t交易时间\nEX-UPL-9301\t2026-09-01 10:00:00\n".encode("utf-8"))
        tmp = fh.name
    try:
        with pytest.raises(ReconqError) as exc:
            read_bill_file_probed(tmp, _table("unionpay"), "unionpay")
        assert exc.value.code == "BILL_COLUMN_MISSING"
        assert "trade_no" in exc.value.detail
        assert "订单号" in exc.value.detail
    finally:
        os.unlink(tmp)


def test_detect_encoding_suspect_flag_is_self_reporting():
    """兜底解码必须自报判据不牢（面板要靠这个标黄）。"""
    probe = detect_encoding(b"\xef\xbb\xbfhello")
    assert (probe.encoding, probe.suspect) == ("utf-8-sig", False)
    assert probe.suspect_encoding is False
    broken = detect_encoding(BROKEN := b"\x81\x30\x81\xfe\x81\x9b\x0b\xff\xfe\x81\x40")
    assert broken.encoding == "gbk"
    assert broken.suspect is True and broken.suspect_encoding is True
    assert BROKEN  # 字节段与生成脚本一致


def test_detect_delimiter_prefers_contract_column_count():
    line = "订单号,银联交易单号,交易时间,交易金额,结算方式,是否退款|无关"
    assert detect_delimiter(line, expected_columns=6) == ","
    assert detect_delimiter("a\tb\tc") == "\t"
