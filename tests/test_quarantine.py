"""脏行隔离区与脏行台账导出（Task 11）。

四条规定断言逐条可核：

1. **脏行被隔离且其余行正常入库** —— 100 行干净 + 5 行脏（覆盖全部六个
   原因码），干净行一行不少地进 ``bill_rows``，5 行脏行各带源文件真实行号
   与原文进 ``dirty_rows``；
2. **脏行率超阈值整文件判 FORMAT_BROKEN** —— 2/6 = 33% 的文件整份判坏，
   干净行**不入库**（不产出一份残缺却看着干净的账），隔离记录仍然写入；
3. **导出含原文与行号** —— CSV 里能拿到「文件名:行号」与原始行文本，
   财务科据此回查导出方；文件按 utf-8-sig 写，Excel 双击不乱码；
4. **重复解析不重复隔离** —— 同一文件扫两次，隔离条数不变、账单行数
   不变、金额合计不变（幂等键是渠道+账期+文件+行号）。
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import tempfile
from contextlib import redirect_stdout
from decimal import Decimal

import pytest

from reconq.cli import EXIT_CHECKS_FAILED, EXIT_OK, main
from reconq.contracts.loader import load_contract
from reconq.errors import ReconqError
from reconq.sources.format_probe import read_bill_file_isolated, read_bill_file_probed
from reconq.store.quarantine import (
    DEFAULT_DIRTY_RATE_THRESHOLD,
    REASON_CODES,
    QuarantineRow,
    channel_state,
    count_dirty,
    dirty_rate,
    export_quarantine_csv,
    init_dirty_schema,
    insert_dirty_rows,
    list_dirty,
    scan_channel_dir,
    scan_channel_file,
)
from reconq.store.schema import connect, init_schema

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS = os.path.join(os.path.dirname(HERE), "contracts")
QUARANTINE = os.path.join(HERE, "fixtures", "quarantine")
PERIOD = "2026-W36"


# —— 夹具 ————————————————————————————————————————————————


def _table(channel: str):
    return load_contract(
        os.path.join(CONTRACTS, f"{channel}.yml"), {"period": PERIOD}
    ).table()


def _store(tmp_path) -> sqlite3.Connection:
    conn = connect(str(tmp_path / "reconq.db"))
    init_schema(conn)
    init_dirty_schema(conn)
    return conn


def _fixture_file(fixture_dir: str) -> str:
    directory = os.path.join(QUARANTINE, PERIOD, fixture_dir)
    names = sorted(n for n in os.listdir(directory) if not n.startswith("."))
    assert names, f"夹具目录为空：{directory}"
    return os.path.join(directory, names[0])


def _scan(conn, fixture_dir: str, channel: str, **kwargs):
    return scan_channel_file(
        conn, _fixture_file(fixture_dir), _table(channel), channel, PERIOD, **kwargs
    )


def _rows_of(conn, channel: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT channel_order_no, amount_text FROM bill_rows "
            "WHERE channel = ? ORDER BY ch_id",
            (channel,),
        )
    )


# —— 断言 1：脏行被隔离，其余行正常入库 ————————————————————


def test_dirty_rows_quarantined_while_clean_rows_ingested(tmp_path):
    """六个原因码各出 1 条脏行，100 行干净行一行不少地进账单表。"""
    conn = _store(tmp_path)
    result = _scan(conn, "unionpay_mixed", "unionpay")

    assert result.state == "dirty"
    assert result.clean_rows == 100
    assert result.dirty_total == 5
    # 4.8% < 5% 阈值 → 判「黄」不判「红」
    assert result.dirty_rate == pytest.approx(5 / 105)
    assert result.dirty_rate < DEFAULT_DIRTY_RATE_THRESHOLD

    # 干净行真的进了账单表（不是"跳过了脏行就假装全过了"）
    bills = _rows_of(conn, "unionpay")
    assert len(bills) == 100
    assert Decimal(bills[0]["amount_text"]) == Decimal("101.50")

    # 六个原因码里这一组命中的五个，全部各 1 条
    records = list_dirty(conn, PERIOD, "unionpay")
    assert len(records) == 5
    assert {r["reason_code"] for r in records} == {
        "COLUMN_COUNT_MISMATCH",
        "AMOUNT_UNPARSABLE",
        "TIMESTAMP_UNPARSABLE",
        "REQUIRED_FIELD_EMPTY",
        "ENCODING_UNRESOLVED",
    }


def test_quarantine_rows_carry_source_line_no_and_raw_text(tmp_path):
    """隔离记录必须能定位到源文件真实行号，且带原始行文本。"""
    conn = _store(tmp_path)
    _scan(conn, "unionpay_mixed", "unionpay")
    by_reason = {r["reason_code"]: r for r in list_dirty(conn, PERIOD, "unionpay")}

    # 列数不符：行号 102（100 行数据后第一行脏行），原文可见
    ragged = by_reason["COLUMN_COUNT_MISMATCH"]
    assert ragged["source_file"] == "unionpay_bill_2026-W36.txt"
    assert ragged["row_no"] == 102
    assert "EX-UPL-D01" in ragged["raw_line"]
    assert "3" in ragged["detail"]  # 详情里点明实际列数

    # 金额大写词：原文留着，财务科看得出是导出方写了「叁佰元整」
    amount = by_reason["AMOUNT_UNPARSABLE"]
    assert amount["row_no"] == 103
    assert "叁佰元整" in amount["raw_line"]

    # 时间非法（25 点）
    assert by_reason["TIMESTAMP_UNPARSABLE"]["row_no"] == 104

    # 主键列为空
    empty_key = by_reason["REQUIRED_FIELD_EMPTY"]
    assert empty_key["row_no"] == 105
    assert "out_trade_no" in empty_key["detail"]

    # 编码坏掉的行
    assert by_reason["ENCODING_UNRESOLVED"]["row_no"] == 106


def test_empty_non_key_required_column_is_kept_for_not_empty_check(tmp_path):
    """主键列为空 → 隔离；非主键必填列（微信交易单号）为空 → **保留**。

    后者是财务科要看的差异（少一个微信交易单号，跨渠道对不上），
    属于 ``not_empty`` 口径的职责；隔离区只接「解析层真的读不出这一行」。
    拿掉它会让口径凭空少一条证据行。
    """
    conn = _store(tmp_path)
    result = _scan(conn, "wechat_emptykey", "wechat")

    assert result.state == "dirty"
    assert result.dirty_total == 1
    records = list_dirty(conn, PERIOD, "wechat")
    assert records[0]["reason_code"] == "REQUIRED_FIELD_EMPTY"
    assert records[0]["row_no"] == 3

    bills = _rows_of(conn, "wechat")
    # 20 行入库：含那条「微信交易单号为空但商户订单号正常」的行
    assert len(bills) == 20
    assert any(r["channel_order_no"] == "EX-WX-EK-0003" for r in bills)


def test_true_duplicate_row_quarantined_but_split_payment_kept(tmp_path):
    """完全重复的行隔离；同号但金额/时间不同的拆笔行**两行都保留**。

    拆笔是真实业务事实，隔离掉等于把差异从账上抹掉；它该由对齐器
    （Task 21+）判成 ``SPLIT_PAYMENT``，不是被解析层当重复丢掉。
    只有「连金额带时间都一模一样」才是导出方重复导了一次。
    """
    conn = _store(tmp_path)
    result = _scan(conn, "wechat_dup", "wechat")

    assert result.state == "dirty"
    records = list_dirty(conn, PERIOD, "wechat")
    assert len(records) == 1
    assert records[0]["reason_code"] == "DUPLICATE_ROW_CONFLICT"
    assert records[0]["row_no"] == 3  # 第 2 行是第 1 行的完全重复
    assert "第 2 行" in records[0]["detail"]

    # 拆笔两行（60.00 / 68.00，同号不同额）都没被隔离层拿走：clean_rows 22 = 21 数据行 + 1
    assert result.clean_rows == 22
    assert not any(
        "EX-WX-SPLIT-0002" in (r["raw_line"] or "") for r in list_dirty(conn, PERIOD, "wechat")
    )
    # 账单表只留第一条：唯一键 (channel, period, bill_date, channel_order_no) 去重。
    # 这是 Task 7 的账（见 discoveries.md D1），对齐器 Task 21+ 会从「渠道行数 ≠
    # 账单行数」的差额里反推拆笔 —— 隔离层不能替它做这个决定。
    split = [r for r in _rows_of(conn, "wechat") if r["channel_order_no"] == "EX-WX-SPLIT-0002"]
    assert len(split) == 1


def test_file_level_problems_still_raise_not_isolated(tmp_path):
    """缺必填列是**文件级**问题，仍然抛 —— 一行都读不出来，隔离区里也没内容。"""
    path = os.path.join(HERE, "fixtures", "dirty", "unionpay_missing_required.txt")
    with pytest.raises(ReconqError) as exc:
        read_bill_file_isolated(path, _table("unionpay"), "unionpay", period=PERIOD)
    assert exc.value.code == "BILL_COLUMN_MISSING"


# —— 断言 2：脏行率超阈值整文件判 FORMAT_BROKEN ————————————


def test_dirty_rate_over_threshold_marks_file_broken_and_skips_ingest(tmp_path):
    """2/6 = 33% 超过 5% → 整文件判 FORMAT_BROKEN，干净行不入库。"""
    conn = _store(tmp_path)
    result = _scan(conn, "unionpay_broken", "unionpay")

    assert result.file_broken is True
    assert result.state == "broken"
    assert result.error_code == "FORMAT_BROKEN"
    assert result.clean_rows == 0
    assert result.dirty_rate == pytest.approx(2 / 6)
    assert "33.3%" in result.detail
    assert "未取到" in result.detail

    # 关键：4 行干净数据**没有**入库。拿残缺数据出"干净结论"比报错危险
    assert conn.execute("SELECT COUNT(*) FROM bill_rows").fetchone()[0] == 0

    # 但隔离记录仍然写入 —— 财务科需要知道「这期有 2 行脏行」
    assert count_dirty(conn, PERIOD, "unionpay") == 2


def test_threshold_is_configurable_and_boundary_is_exclusive(tmp_path):
    """阈值可配；等于阈值不判坏，严格大于才判坏。"""
    conn = _store(tmp_path)
    result = _scan(conn, "unionpay_broken", "unionpay", rate_threshold=1 / 3)
    assert result.state == "dirty"  # 恰好 1/3，不 > 1/3
    assert conn.execute("SELECT COUNT(*) FROM bill_rows").fetchone()[0] == 4

    conn2 = _store(tmp_path / "b")
    result2 = _scan(conn2, "unionpay_broken", "unionpay", rate_threshold=0.2)
    assert result2.state == "broken"


def test_dirty_rate_helper_never_divides_by_zero():
    assert dirty_rate(0, 0) == 0.0
    assert dirty_rate(3, 0) == 1.0  # 全部脏行 → 1.0 而不是除零
    assert dirty_rate(1, 19) == pytest.approx(0.05)


# —— 断言 3：导出含原文与行号 ————————————————————————————


def test_export_csv_contains_row_no_and_raw_line(tmp_path):
    """导出的 CSV 每行都能回到「哪个文件第几行、原文是什么」。"""
    conn = _store(tmp_path)
    _scan(conn, "unionpay_mixed", "unionpay")
    out = str(tmp_path / "out" / "2026-W36-unionpay-dirty.csv")
    path, count = export_quarantine_csv(conn, PERIOD, "unionpay", out)

    assert count == 5
    assert os.path.isfile(path)
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 5
    assert set(rows[0]) >= {"渠道", "账期", "来源文件", "源文件行号", "原因码", "原始行", "回查定位"}

    by_reason = {r["原因码"]: r for r in rows}
    ragged = by_reason["COLUMN_COUNT_MISMATCH"]
    assert ragged["来源文件"] == "unionpay_bill_2026-W36.txt"
    assert ragged["源文件行号"] == "102"
    assert ragged["回查定位"] == "unionpay_bill_2026-W36.txt:102"
    assert "EX-UPL-D01" in ragged["原始行"]
    assert ragged["原因说明"]  # 中文说明非空，财务科看得懂
    assert by_reason["AMOUNT_UNPARSABLE"]["原始行"].find("叁佰元整") > 0


def test_export_csv_written_with_bom_for_excel(tmp_path):
    """utf-8-sig：Windows Excel 双击直接开，中文不乱码。"""
    conn = _store(tmp_path)
    _scan(conn, "wechat_emptykey", "wechat")
    out = str(tmp_path / "dirty.csv")
    export_quarantine_csv(conn, PERIOD, "wechat", out)
    with open(out, "rb") as handle:
        assert handle.read(3) == b"\xef\xbb\xbf"


def test_export_filters_by_channel(tmp_path):
    conn = _store(tmp_path)
    _scan(conn, "unionpay_mixed", "unionpay")
    _scan(conn, "wechat_emptykey", "wechat")
    _, only_up = export_quarantine_csv(conn, PERIOD, "unionpay", str(tmp_path / "a.csv"))
    _, all_ch = export_quarantine_csv(conn, PERIOD, None, str(tmp_path / "b.csv"))
    assert only_up == 5
    assert all_ch == 6


# —— 断言 4：重复解析不重复隔离 ————————————————————————————


def test_rescanning_same_file_does_not_duplicate_quarantine_rows(tmp_path):
    """同一文件扫两次：隔离条数、账单行数、金额合计三项全不变。"""
    conn = _store(tmp_path)
    first = _scan(conn, "unionpay_mixed", "unionpay")
    amount_sum = sum(
        Decimal(r[0]) for r in conn.execute("SELECT amount_text FROM bill_rows")
    )
    assert Decimal(_rows_of(conn, "unionpay")[0]["amount_text"]) == Decimal("101.50")

    second = _scan(conn, "unionpay_mixed", "unionpay")
    assert first.dirty_inserted == 5
    assert second.dirty_inserted == 0
    assert second.dirty_skipped == 5  # 全部命中幂等键
    assert second.dirty_total == first.dirty_total
    assert count_dirty(conn, PERIOD, "unionpay") == 5
    assert conn.execute("SELECT COUNT(*) FROM bill_rows").fetchone()[0] == 100
    assert sum(
        Decimal(r[0]) for r in conn.execute("SELECT amount_text FROM bill_rows")
    ) == amount_sum


def test_quarantine_insert_is_idempotent_on_its_own(tmp_path):
    """直接调 insert_dirty_rows 也幂等（唯一键 = 渠道+账期+文件+行号）。"""
    conn = _store(tmp_path)
    rows = [
        QuarantineRow(
            channel="unionpay",
            period=PERIOD,
            source_file="x.txt",
            row_no=n,
            reason_code="AMOUNT_UNPARSABLE",
            raw_line="原始行",
            detail="详情",
        )
        for n in (2, 3)
    ]
    assert insert_dirty_rows(conn, rows) == (2, 0)
    assert insert_dirty_rows(conn, rows) == (0, 2)
    assert count_dirty(conn, PERIOD, "unionpay") == 2
    # 唯一索引确实建在 (channel, period, source_file, row_no) 上
    idx = list(
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dirty_rows'"
        )
    )
    assert any(r[0] == "ux_dirty_identity" for r in idx)


def test_quarantine_insert_rolls_back_on_failure(tmp_path):
    """隔离记录半截落库比不落更糟：面板说 N 条却查不到第 N 条。"""

    class _Exploding:
        """在序列化时炸掉的替身，模拟批量写入中途出错。"""

        def to_tuple(self):
            raise RuntimeError("测试注入：序列化时抛错")

    conn = _store(tmp_path)
    rows = [
        QuarantineRow("unionpay", PERIOD, "x.txt", 2, "AMOUNT_UNPARSABLE"),
        _Exploding(),
    ]
    with pytest.raises(RuntimeError):
        insert_dirty_rows(conn, rows)
    # 第一条已执行过 INSERT，但整批回滚 → 隔离区仍为空
    assert count_dirty(conn, PERIOD, "unionpay") == 0


# —— 面板渠道主态 ————————————————————————————————————————


def test_channel_state_reflects_dirty_rows(tmp_path):
    """面板主态：绿（无脏行）/ 黄（有脏行）/ 红（超阈值）。"""
    conn = _store(tmp_path)
    assert channel_state(conn, PERIOD, "unionpay")[0] == "clean"

    _scan(conn, "unionpay_mixed", "unionpay")
    state, count, rate = channel_state(conn, PERIOD, "unionpay", clean_rows=100)
    assert state == "dirty"
    assert count == 5
    assert rate == pytest.approx(5 / 105)

    _scan(conn, "unionpay_broken", "unionpay")
    # 干净行未入库（判坏），按隔离行数/（隔离行数）算必然超阈值
    assert channel_state(conn, PERIOD, "unionpay")[0] == "broken"


# —— 严格模式不回归 ——————————————————————————————————————


def test_strict_reader_still_raises_on_ragged_row(tmp_path):
    """排障腿的严格行为不变：脏行照旧抛 COLUMN_COUNT_MISMATCH 带行号。"""
    path = os.path.join(HERE, "fixtures", "dirty", "unionpay_ragged_row.txt")
    with pytest.raises(ReconqError) as exc:
        read_bill_file_probed(path, _table("unionpay"), "unionpay")
    assert exc.value.code == "COLUMN_COUNT_MISMATCH"
    assert exc.value.row_ref == "unionpay_ragged_row.txt:3"


def test_strict_reader_still_raises_on_bad_amount(tmp_path):
    path = os.path.join(HERE, "fixtures", "dirty", "unionpay_bad_amount.txt")
    with pytest.raises(ReconqError) as exc:
        read_bill_file_probed(path, _table("unionpay"), "unionpay")
    assert exc.value.code == "AMOUNT_UNPARSABLE"


# —— CLI 三条腿 ——————————————————————————————————————————


def _inbox(tmp_path, channels: list[str], fixture_map: dict[str, str]) -> str:
    root = tmp_path / "inbox" / PERIOD
    for channel in channels:
        dst = root / channel
        dst.mkdir(parents=True, exist_ok=True)
        src = _fixture_file(fixture_map[channel])
        with open(src, "rb") as handle:
            (dst / os.path.basename(src)).write_bytes(handle.read())
    return str(tmp_path / "inbox")


def test_cli_quarantine_scan_reports_dirty_and_broken(tmp_path):
    """``reconq quarantine scan``：一行一渠道，主态与原因可读。"""
    inbox = _inbox(tmp_path, ["unionpay", "wechat"], {
        "unionpay": "unionpay_mixed",
        "wechat": "wechat_emptykey",
    })
    db = str(tmp_path / "reconq.db")
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main([
            "quarantine", "scan", "--period", PERIOD,
            "--source", inbox, "--db", db,
            "--channel", "unionpay", "--channel", "wechat",
        ])
    out = buf.getvalue()
    assert code == EXIT_CHECKS_FAILED  # 有脏行 → 非零，提醒财务科
    assert "黄 · 本期有脏行" in out
    assert "5 行隔离" in out
    assert "1 行隔离" in out
    assert "AMOUNT_UNPARSABLE" not in out  # 终端按中文说，码在 list/export 里
    assert "导出脏行台账" in out


def test_cli_quarantine_scan_broken_file_marks_channel_not_fetched(tmp_path):
    inbox = _inbox(tmp_path, ["unionpay"], {"unionpay": "unionpay_broken"})
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main([
            "quarantine", "scan", "--period", PERIOD,
            "--source", inbox, "--db", str(tmp_path / "r.db"),
            "--channel", "unionpay",
        ])
    out = buf.getvalue()
    assert code == EXIT_CHECKS_FAILED
    assert "红 · 脏行率超阈值" in out
    assert "33.3%" in out
    assert "其余渠道结果照常可用" in out
    assert "本期 1 份文件判坏或未取到" in out
    assert "unionpay_bill_2026-W36.txt" in out


def test_cli_quarantine_export_writes_ledger(tmp_path):
    """scan → export 串起来，产出财务科真正会拿去回查的那张表。"""
    inbox = _inbox(tmp_path, ["unionpay"], {"unionpay": "unionpay_mixed"})
    db = str(tmp_path / "reconq.db")
    with redirect_stdout(io.StringIO()):
        main([
            "quarantine", "scan", "--period", PERIOD,
            "--source", inbox, "--db", db, "--channel", "unionpay",
        ])
    out_csv = str(tmp_path / "ledger.csv")
    buf = io.StringIO()
    with redirect_stdout(buf):
        main([
            "quarantine", "export", "--period", PERIOD,
            "--db", db, "--channel", "unionpay", "--out", out_csv,
        ])
    out = buf.getvalue()
    assert os.path.isfile(out_csv)
    assert "5 行" in out
    assert "Excel 可直接打开" in out
    assert "回查给导出方" in out
    with open(out_csv, encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 5


def test_cli_quarantine_list_shows_zh_reason_and_raw_line(tmp_path):
    inbox = _inbox(tmp_path, ["unionpay"], {"unionpay": "unionpay_mixed"})
    db = str(tmp_path / "reconq.db")
    with redirect_stdout(io.StringIO()):
        main([
            "quarantine", "scan", "--period", PERIOD,
            "--source", inbox, "--db", db, "--channel", "unionpay",
        ])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["quarantine", "list", "--period", PERIOD, "--db", db, "--channel", "unionpay"])
    out = buf.getvalue()
    assert code == EXIT_CHECKS_FAILED
    assert "共 5 行" in out
    assert "AMOUNT_UNPARSABLE" in out
    assert "金额列的值不是合法数字" in out
    assert "叁佰元整" in out
    assert "unionpay_bill_2026-W36.txt:102" in out


def test_cli_quarantine_export_on_missing_db_explains(tmp_path):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main([
            "quarantine", "export", "--period", PERIOD,
            "--db", str(tmp_path / "nope.db"),
        ])
    err = io.StringIO()
    with redirect_stderr_to(err):
        code = main([
            "quarantine", "export", "--period", PERIOD,
            "--db", str(tmp_path / "nope.db"),
        ])
    assert code != EXIT_OK
    assert "reconq quarantine scan" in err.getvalue()


class redirect_stderr_to:
    def __init__(self, buffer):
        self.buffer = buffer
        self._old = None

    def __enter__(self):
        import sys

        self._old = sys.stderr
        sys.stderr = self.buffer
        return self.buffer

    def __exit__(self, *exc):
        import sys

        sys.stderr = self._old
        return False


# —— verify 腿：脏行数进渠道主态 ————————————————————————————


def test_verify_reports_dirty_rows_in_summary(tmp_path):
    """verify 是只读排障腿：脏行只计数与提示，但摘要里必须带这个数。

    合计被读小了要能立刻看出「少了 N 行」，否则这笔差额会一直挂在账上。
    """
    inbox = _inbox(tmp_path, ["unionpay"], {"unionpay": "unionpay_mixed"})
    buf = io.StringIO()
    with redirect_stdout(buf):
        main([
            "verify", "--period", PERIOD, "--source", inbox,
            "--contracts", CONTRACTS, "--channel", "unionpay", "--json",
        ])
    import json

    payload = json.loads(buf.getvalue())
    ch = payload["channels"][0]
    # 101 = 100 干净行 + 那条「主键列为空但仍保留给 not_empty 口径」的行
    assert ch["row_count"] == 101
    # 4 条：verify 这条腿**不**把「主键列为空」当脏行 —— 那行由 not_empty 口径
    # 以证据行号报出来（它是财务科要看的差异，不是解析垃圾），见下一个测试。
    assert ch["dirty_rows"] == 4
    assert {d["reason_code"] for d in ch["dirty"]} == {
        "COLUMN_COUNT_MISMATCH",
        "AMOUNT_UNPARSABLE",
        "TIMESTAMP_UNPARSABLE",
        "ENCODING_UNRESOLVED",
    }
    # 脏行证据要能定位到源行
    assert all(d["row_ref"].startswith("unionpay_bill_2026-W36.txt:") for d in ch["dirty"])


def test_verify_keeps_empty_order_no_row_for_not_empty_evidence(tmp_path):
    """主路径不回归：空商户订单号那行仍由 not_empty 报证据，不是被隔离掉。

    Task 3 立的证据行号就是 ``wechat:12``；隔离区若把它拿走，口径就凭空
    少一条差异，财务科要看的正是这一条。
    """
    bills = os.path.join(HERE, "fixtures", "bills")
    buf = io.StringIO()
    with redirect_stdout(buf):
        main([
            "verify", "--period", PERIOD, "--source", bills,
            "--contracts", CONTRACTS, "--channel", "wechat",
        ])
    out = buf.getvalue()
    assert "wechat_tradebill_2026-W36.csv:6" in out
    assert "wechat_tradebill_2026-W36.csv:12" in out
    assert "20 行" in out
    assert "隔离" not in out  # 这份样例没有解析层脏行


# —— 受控原因码 ——————————————————————————————————————————


def test_reason_codes_are_a_controlled_set():
    """原因码必须受控：自由文本会让脏行无法按成因聚合统计。"""
    assert set(REASON_CODES) == {
        "COLUMN_COUNT_MISMATCH",
        "AMOUNT_UNPARSABLE",
        "ENCODING_UNRESOLVED",
        "REQUIRED_FIELD_EMPTY",
        "DUPLICATE_ROW_CONFLICT",
        "TIMESTAMP_UNPARSABLE",
    }
    with pytest.raises(ReconqError) as exc:
        QuarantineRow("unionpay", PERIOD, "x.txt", 2, "随便写的原因")
    assert exc.value.code == "DIRTY_REASON_UNKNOWN"


def test_scan_channel_dir_covers_every_file(tmp_path):
    """目录级扫描：逐份文件都给结果，一份坏的不影响同目录其他文件。"""
    directory = tmp_path / "inbox" / PERIOD / "unionpay"
    directory.mkdir(parents=True)
    for fixture, name in (("unionpay_mixed", "a_mixed.txt"), ("unionpay_broken", "b_broken.txt")):
        src = _fixture_file(fixture)
        (directory / name).write_bytes(open(src, "rb").read())

    conn = _store(tmp_path)
    results = scan_channel_dir(conn, str(directory), _table("unionpay"), PERIOD and "unionpay", PERIOD)
    assert len(results) == 2
    states = {r.source_file: r.state for r in results}
    assert states["a_mixed.txt"] == "dirty"
    assert states["b_broken.txt"] == "broken"
    # 判坏那份的脏行也进隔离区，财务科能看见「这期还有 2 行脏的」
    assert count_dirty(conn, PERIOD, "unionpay") == 7
