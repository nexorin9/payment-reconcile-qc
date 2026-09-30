"""结构化运行摘要与脱敏（Task 20）。

规定四类断言（第三、四类在此文件，第一、二类在 ``test_errors.py``）：

3. **六路摘要字段完整** —— 每路八个字段一个不缺，且真值来自真实扫描
4. **日志无患者标识** —— 脱敏在**写盘前**生效，且真的拦得住东西

第 4 条容易写成自欺：拿一段本来就没有患者信息的样例账单去跑，断言「日志里
没有患者姓名」，当然通过。所以这里刻意构造**含患者标识的脏行**（真实导出
方把姓名塞进备注列是很常见的），断言它**被替换掉**了 —— 拦不住东西的脱敏
等于没脱敏。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from reconq.run_summary import (
    ChannelRun,
    RunSummary,
    build_run_summary,
    current_peak_rss_kb,
)
from reconq.runlog import (
    JsonLogFormatter,
    configure_logging,
    log_event,
    redact_payload,
    redact_text,
    reset_logging,
)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERIOD = "2026-W36"

#: 六路渠道。摘要的「六路字段完整」是按这六路逐个断言的。
ALL_CHANNELS = (
    "wechat",
    "alipay",
    "unionpay",
    "self_service",
    "his",
    "medical_ins",
)

#: 每路必须存在的八个字段。
REQUIRED_CHANNEL_FIELDS = (
    "channel",
    "status",
    "row_count",
    "total_amount",
    "batches",
    "elapsed_ms",
    "peak_rss_kb",
    "dirty_rows",
    "error_code",
)


# —— 3. 六路摘要字段完整 ——————————————————————————————————————


def _run_cli(*args: str, cwd: str = PROJECT_DIR) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=os.path.join(PROJECT_DIR, "src"))
    return subprocess.run(
        [sys.executable, "-m", "reconq", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def test_verify_json_run_summary_has_all_six_channels_with_every_field():
    """真实跑一遍六路 verify --json，逐路逐字段断言。

    「字段完整」在单测里用构造出来的对象断言没有意义 —— 真正要防的是
    **某个字段在 CLI 装配路径上忘了填**（面板上那一格显示空白而不是 0，
    而 0 和空白在排障时含义完全不同：0 是「没有」，空白是「没测」）。
    """
    proc = _run_cli(
        "verify", "--period", PERIOD, "--source", "tests/fixtures/bills", "--json"
    )
    assert proc.returncode == 1, f"有未通过口径时退出码应为 1：{proc.stderr[:400]}"
    payload = json.loads(proc.stdout)
    summary = payload["run_summary"]

    got = [c["channel"] for c in summary["channels"]]
    assert set(got) == set(ALL_CHANNELS), f"六路不齐：{got}"
    for channel in summary["channels"]:
        for field in REQUIRED_CHANNEL_FIELDS:
            assert field in channel, f"{channel['channel']} 摘要缺字段 {field}"
        assert isinstance(channel["row_count"], int)
        assert isinstance(channel["batches"], int)
        assert isinstance(channel["elapsed_ms"], (int, float))
        assert isinstance(channel["peak_rss_kb"], int)
        assert isinstance(channel["dirty_rows"], int)


def test_verify_run_summary_amount_matches_channel_sum_exactly():
    """合计逐路求和必须与摘要里的 total_amount 分毫不差。

    金额一律 Decimal 定点字符串：0.1 + 0.2 在 float 下是 0.30000000000000004，
    结论单上就会印出一个没人能解释的 1e-17 差额。
    """
    from decimal import Decimal

    proc = _run_cli(
        "verify", "--period", PERIOD, "--source", "tests/fixtures/bills", "--json"
    )
    payload = json.loads(proc.stdout)
    summary = payload["run_summary"]
    total = sum(
        (Decimal(c["total_amount"] or "0") for c in summary["channels"]), Decimal("0")
    )
    assert Decimal(summary["total_amount"]) == total.quantize(Decimal("0.01"))
    assert summary["total_amount"] == str(total.quantize(Decimal("0.01")))


def test_run_summary_never_reports_green_when_a_channel_is_missing():
    """六路少一路绝不能显绿。

    最危险的假绿：一路未取到 → 那一路的钱既没被核对也没被排除 → 账看着
    干净，其实少查了一路。所以 ``channels_failed`` 计入 ``can_close``，
    且「未取到」也算失败。
    """
    summary = build_run_summary(
        period=PERIOD,
        command="verify",
        channels=[
            ChannelRun(channel="wechat", status="checked", row_count=20,
                       total_amount="5714.69"),
            ChannelRun(channel="his", status="not_fetched",
                       error_code="CREDENTIAL_MISSING"),
        ],
        gate="green",
    )
    assert summary.channels_ok == 1
    assert summary.channels_failed == 1
    assert summary.can_close is False


def test_not_fetched_counts_as_failed_not_as_ok():
    """「未取到」是失败的一种，不是「暂时没数据所以没事」。"""
    assert ChannelRun(channel="his", status="not_fetched").ok is False
    assert ChannelRun(channel="his", status="failed").ok is False
    assert ChannelRun(channel="his", status="checked").ok is True


def test_carried_channels_are_marked_not_mixed_into_this_run():
    """沿用上次的渠道必须标出来，不能混进「本次产出」。

    否则只重跑微信时，「微信 19 行」在面板上看着像刚跑的，财务科会以为
    银联那 6 行这期也重新核过了。
    """
    outcome_like = [
        ChannelRun(channel="wechat", status="checked", row_count=19),
        ChannelRun(channel="unionpay", status="checked", row_count=6, carried=True),
    ]
    summary = build_run_summary(
        period=PERIOD, command="scan", channels=outcome_like, run_id="run-A"
    )
    assert summary.channel_of("wechat").carried is False
    assert summary.channel_of("unionpay").carried is True
    payload = summary.to_dict()
    assert payload["channels"][1]["carried"] is True


def test_scan_json_run_summary_is_present_and_consistent(tmp_path):
    """scan --json 也带 run_summary，且与账期实例口径一致。

    合计/行数取**账期实例**（账单库权威），不是「本次跑的那几路」：
    子集重跑时按本次累加会让本期合计凭空少一大截，而库里没有一行变过。
    """
    db = tmp_path / "reconq.db"
    proc = _run_cli(
        "scan",
        "--period",
        PERIOD,
        "--source",
        "tests/fixtures/bills",
        "--db",
        str(db),
        "--json",
    )
    assert proc.returncode == 1, proc.stderr[:400]
    payload = json.loads(proc.stdout)
    summary = payload["run_summary"]
    assert summary["period"] == PERIOD
    assert summary["command"] == "scan"
    assert summary["run_id"] == payload["run_id"]
    # 与账期实例同一口径，不是重新求和
    assert summary["total_amount"] == payload["total_amount"]
    assert summary["rows_total"] == payload["rows_ingested"]
    assert summary["gate"] == payload["gate"]
    for channel in summary["channels"]:
        for field in REQUIRED_CHANNEL_FIELDS:
            assert field in channel, f"scan 摘要 {channel['channel']} 缺 {field}"


def test_scan_json_rerun_does_not_shrink_the_period_total(tmp_path):
    """子集重跑后本期合计不变（防「凭空少两万」）。"""
    db = tmp_path / "reconq.db"
    base = [
        "scan", "--period", PERIOD, "--source", "tests/fixtures/bills", "--db", str(db),
    ]
    first = _run_cli(*base, "--json")
    second = _run_cli(*base, "--channel", "wechat", "--json")
    assert first.returncode in (0, 1) and second.returncode in (0, 1)
    total_first = json.loads(first.stdout)["run_summary"]["total_amount"]
    total_second = json.loads(second.stdout)["run_summary"]["total_amount"]
    assert total_first == total_second, (
        f"只重跑微信一期，总计从 {total_first} 变成 {total_second} —— "
        f"库里没有任何一行数据变过"
    )


def test_peak_rss_is_reported_in_kb_not_bytes():
    """峰值内存字段名写着 kb，值就得是 KB。

    ``ru_maxrss`` 在 Linux 上是 KB、在 macOS 上是字节。不折算的话同一台
    机器的面板在两个系统上差一千倍，看板上没人会想到这是单位问题。
    """
    kb = current_peak_rss_kb()
    assert kb > 0
    # 一个 Python 进程的峰值不可能超过 16 GB（否则就是单位错了）
    assert kb < 16 * 1024 * 1024, f"{kb} KB 看着像字节没折算"


def test_dirty_rows_are_carried_into_the_summary():
    """脏行数进摘要：合计被读小了却没人知道少的是脏行，最难查。"""
    summary = build_run_summary(
        period=PERIOD,
        command="verify",
        channels=[
            ChannelRun(channel="wechat", status="checked", row_count=100,
                       total_amount="1204.50", dirty_rows=5),
            ChannelRun(channel="unionpay", status="checked", row_count=6,
                       total_amount="873.00"),
        ],
    )
    assert summary.dirty_rows == 5
    assert summary.channel_of("wechat").to_dict()["dirty_rows"] == 5


def test_run_summary_to_dict_is_json_serialisable():
    """摘要必须能直接进 ``--json`` 输出（面板与工单都要它）。"""
    summary = build_run_summary(
        period=PERIOD,
        command="verify",
        channels=[ChannelRun(channel="wechat", status="checked", row_count=20,
                             total_amount="5714.69")],
    )
    payload = summary.to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["period"] == PERIOD


def test_render_zh_shows_batches_and_elapsed():
    """人读摘要里要有批次数与耗时 —— 这正是行数看不出来的东西。"""
    summary = build_run_summary(
        period=PERIOD,
        command="scan",
        channels=[
            ChannelRun(channel="his", status="checked", row_count=2000,
                       total_amount="100.00", batches=37, elapsed_ms=4200.0),
        ],
        gate="green",
    )
    text = summary.render_zh()
    assert "37 批" in text
    assert "4200 ms" in text
    assert "1/1 路可用" in text


# —— 4. 日志无患者标识 ————————————————————————————————————————


def test_redaction_actually_removes_patient_identifiers():
    """脱敏必须真的拦得住东西。

    拿一段本来就没有患者信息的文本断言「没有患者姓名」，当然通过 ——
    那种测试守不住任何东西。这里给的是**真的带标识**的文本。

    断言的边界写在 :data:`reconq.runlog.PATIENT_PATTERNS` 的注释里：
    拦的是**带标签的标识**与**有固定形状的号码**；裸姓名靠键名判定。
    下面这条正是「带标签」形态。
    """
    raw = (
        "wechat:6 交易单号为空，患者姓名：张三，住院号：ZY20260907001，"
        "手机 13800138000，身份证 110101199003072516，金额 128.00"
    )
    cleaned, hits = redact_text(raw)
    for leaked in ("张三", "ZY20260907001", "13800138000", "110101199003072516"):
        assert leaked not in cleaned, f"脱敏后仍能看到 {leaked}：{cleaned}"
    assert {"id_card", "phone", "inpatient_no", "patient_name_labeled"} <= set(hits)
    assert "128.00" in cleaned, "不该把金额一起脱敏掉——那会让日志失去排障价值"


def test_bare_order_numbers_are_not_mistaken_for_patient_ids():
    """脱敏不得误伤正常业务数据。

    过度脱敏的代价和漏脱敏一样大：把订单号、金额、笔数一起吃掉，日志就
    失去了排障价值 —— 财务科拿到「wechat 有几行、差多少钱」之外的信息
    什么都查不出来。
    """
    raw = (
        "wechat 20 行，合计 5714.69 元；单号 EX-WXTXN-0001W36-0004、"
        "EX-WXTXN-0002W36-0005 正常；账期 2026-W36；refund 86.00"
    )
    cleaned, hits = redact_text(raw)
    assert cleaned == raw, f"正常业务数据被误脱敏：{cleaned}"
    assert hits == []


def test_redaction_records_pattern_names_not_the_matched_value():
    """命中只记**模式名**，不记命中内容。

    记内容等于把脱敏绕回去：日志里会出现
    ``"redactions": ["张三"]``，比不记还糟。
    """
    _cleaned, hits = redact_text("住院号：ZY20260907001")
    assert hits == ["inpatient_no"]
    assert "ZY20260907001" not in "".join(hits)


def test_id_card_is_matched_before_phone():
    """18 位身份证要先于 11 位手机号匹配。

    顺序反了的话，身份证的后 11 位会先被手机号规则吃掉，剩下的 7 位碎片
    既匹配不上身份证也匹配不上手机号 —— 脱敏看着生效了，实际泄露了 7 位。
    """
    from reconq.runlog import PATIENT_PATTERNS

    names = [name for name, _ in PATIENT_PATTERNS]
    assert names.index("id_card") < names.index("phone")
    cleaned, hits = redact_text("身份证 110101199003072516")
    assert "110101199003072516" not in cleaned
    assert hits == ["id_card"]


def test_redact_payload_handles_nested_dirty_rows():
    """脏行台账的原始行是嵌套在 dict/list 里的，脱敏要递归下去。"""
    payload = {
        "channel": "wechat",
        "channels": [
            {
                "channel": "unionpay",
                "dirty": [
                    {
                        "source_file": "unionpay.txt",
                        "row_no": 6,
                        # 导出方把患者信息塞进了备注列（真实脏数据里最常见
                        # 的形态之一）；姓名是裸值、无标签，靠住院号/手机号
                        # 的形状规则拦。
                        "raw_line": "壹佰元整,13800138000,ZY20260907001",
                    }
                ],
            }
        ],
    }
    cleaned, hits = redact_payload(payload)
    blob = json.dumps(cleaned, ensure_ascii=False)
    for leaked in ("ZY20260907001", "13800138000"):
        assert leaked not in blob, f"嵌套载荷里仍能读到 {leaked}"
    assert "unionpay.txt" in blob, "不该把行号/文件名脱敏掉——那是回查定位的关键"
    assert hits


def test_redact_payload_masks_values_under_patient_keys():
    """键名本身就是患者标识时，整个值替换，不看内容。

    键名判定优先于内容匹配：``patient_name: "王五"`` 的值只有两个字，
    任何基于内容的模式都认不出它是姓名。
    """
    cleaned, hits = redact_payload({"patient_name": "王五", "inpatient_no": "A12345"})
    assert cleaned["patient_name"] == "[已脱敏]"
    assert cleaned["inpatient_no"] == "[已脱敏]"
    assert hits


def test_json_log_formatter_writes_all_seven_fixed_fields():
    """题目要求的七个固定字段一个不缺。"""
    import logging

    record = logging.LogRecord(
        name="reconq.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="wechat 完成",
        args=(),
        exc_info=None,
    )
    record.event = "channel.fetch.done"
    record.event_fields = {
        "channel": "wechat",
        "period": PERIOD,
        "code": "",
        "duration_ms": 12.5,
        "row_count": 20,
    }
    payload = json.loads(JsonLogFormatter().format(record))
    for field in (
        "ts",
        "level",
        "event",
        "channel",
        "period",
        "code",
        "duration_ms",
    ):
        assert field in payload, f"JSON 日志缺固定字段 {field}"
    assert payload["channel"] == "wechat"
    assert payload["period"] == PERIOD
    assert payload["duration_ms"] == 12.5
    assert payload["row_count"] == 20


def test_json_log_formatter_redacts_the_message():
    """Formatter 自身也脱敏（库里若还有别的 handler，不能只靠 Filter）。"""
    import logging

    record = logging.LogRecord(
        name="reconq.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="脏行隔离：住院号 ZY20260907001 金额 88.00",
        args=(),
        exc_info=None,
    )
    record.event = "channel.quarantine"
    record.event_fields = {"channel": "wechat"}
    payload = json.loads(JsonLogFormatter().format(record))
    assert "ZY20260907001" not in payload["message"]
    assert "88.00" in payload["message"]
    assert "inpatient_no" in payload.get("redactions", [])


def test_json_log_formatter_does_not_dump_the_traceback():
    """只记异常类型与业务码，不记整段栈。

    栈进日志库后没人读，却会把出错函数里的局部变量（可能含原始行文本、
    甚至患者标识）一起带进去 —— 脱敏是按字段名与模式做的，栈里的
    ``repr(local)`` 不一定命中任何模式。
    """
    import logging

    try:
        raise ValueError("原始行：住院号 ZY20260907001")
    except ValueError:
        record = logging.LogRecord(
            name="reconq.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="取数失败",
            args=(),
            exc_info=sys.exc_info(),
        )
    record.event = "channel.fetch.failed"
    record.event_fields = {"channel": "wechat"}
    line = JsonLogFormatter().format(record)
    assert "Traceback" not in line
    assert "File \"" not in line
    assert json.loads(line)["exc_type"] == "ValueError"
    assert "ZY20260907001" not in line


def test_end_to_end_log_file_contains_no_patient_identifier(tmp_path):
    """端到端：真实跑一次 verify，日志文件里搜不到任何患者标识。

    这是「脱敏保证」这一条断言的落点 —— 前面的都是单函数断言，这里验的是
    **整条链路**（CLI → Formatter → 文件）之后的结果。
    """
    log_dir = tmp_path / "logs"
    proc = _run_cli(
        "--log-dir", str(log_dir),
        "verify", "--period", PERIOD, "--source", "tests/fixtures/bills",
    )
    assert proc.returncode == 1
    files = list(log_dir.glob("reconq-*.jsonl"))
    assert files, "没有落 JSON 日志"
    blob = files[0].read_text(encoding="utf-8")
    for leaked in ("张三", "李四", "王五", "13800138000", "110101199003072516"):
        assert leaked not in blob
    # 日志里确实有内容（不是空文件蒙对）
    lines = [json.loads(line) for line in blob.splitlines() if line.strip()]
    assert lines, "日志为空"
    assert any(item["event"] == "run.finish" for item in lines)
    assert any(item["event"] == "channel.check.done" for item in lines)


def test_dirty_row_ledger_export_keeps_line_numbers_but_not_patients(tmp_path):
    """脏行台账的**定位**信息必须留着，脱敏不能把它一起吃掉。

    行号与文件名是财务科回查给导出方的唯一凭据；把它们脱敏掉等于
    「保护隐私」保护到没法排障。真正要拦的只有患者标识本身。
    """
    log_dir = tmp_path / "logs"
    inbox = tmp_path / "inbox" / PERIOD / "unionpay"
    inbox.mkdir(parents=True)
    # 用真实的院内银联账单表头（制表符分隔，GB18030）——照抄契约要求的六列，
    # 再插一行列数不符且原始行里带患者标识的脏行。手写表头会与契约对不上，
    # 整份文件被 BILL_COLUMN_MISSING 判坏，根本走不到脏行那一层。
    (inbox / "unionpay_bill.csv").write_text(
        "交易日期\t商户订单号\t交易单号\t金额(元)\t结算方式\t退款金额(元)\n"
        "2026-09-05\tEX-UPL-0001\tEX-UPTRX-0001\t100.00\t消费\t0.00\n"
        "2026-09-05\tEX-UPL-0002\tEX-UPTRX-0002\t200.00\t消费\t0.00\n"
        "2026-09-06\tEX-UPL-0003\tEX-UPTRX-0003\t张三\t13800138000\tZY20260907001\n"
        "2026-09-06\tEX-UPL-0004\tEX-UPTRX-0004\t300.00\t消费\t0.00\n",
        encoding="utf-8",
    )
    proc = _run_cli(
        "--log-dir", str(log_dir),
        "quarantine", "scan",
        "--period", PERIOD,
        "--source", str(tmp_path / "inbox"),
        "--db", str(tmp_path / "q.db"),
    )
    assert proc.returncode == 1, "有脏行时应返回非零，防止合计偏小被签字"
    out = tmp_path / "dirty.csv"
    export = _run_cli(
        "quarantine", "export",
        "--period", PERIOD,
        "--db", str(tmp_path / "q.db"),
        "--out", str(out),
    )
    assert export.returncode in (0, 1), export.stderr[:400]
    assert out.is_file()
    text = out.read_text(encoding="utf-8-sig")
    assert "unionpay_bill.csv" in text, "来源文件名必须留着（回查定位）"
    # 行号指向源文件真实行（第 4 行是插进去的脏行）
    assert "unionpay_bill.csv:4" in text, f"行号必须留着（回查定位）：{text[:500]}"

    # 台账里的「原始行」按设计**原样保留** —— 它是财务科回查给导出方的
    # 凭据，脱敏掉就等于让排障失去意义。真正被拦住的是**日志**：同一批
    # 脏行进 JSON 日志时必须已脱敏。
    log_blob = next(log_dir.glob("reconq-*.jsonl")).read_text(encoding="utf-8")
    assert "ZY20260907001" not in log_blob, "住院号漏进 JSON 日志"
    assert "13800138000" not in log_blob, "手机号漏进 JSON 日志"
    assert "reconq" in log_blob  # 日志确有内容，不是空文件蒙对


def test_configure_logging_is_idempotent(tmp_path):
    """重复调用只换 handler，不叠加。

    叠加的后果是同一条扫描日志在文件里出现两遍，运维看着像跑了两次扫描。
    """
    try:
        configure_logging(log_dir=str(tmp_path), level="INFO")
        configure_logging(log_dir=str(tmp_path), level="INFO")
        logger = __import__("logging").getLogger("reconq")
        assert len(logger.handlers) == 1
        log_event("run.start", message="x", period=PERIOD)
        log_event("run.finish", message="y", period=PERIOD)
        for handler in logger.handlers:
            handler.flush()
        blob = next(tmp_path.glob("reconq-*.jsonl")).read_text(encoding="utf-8")
        assert len([line for line in blob.splitlines() if line.strip()]) == 2
    finally:
        reset_logging()


def test_unregistered_event_name_is_preserved_not_silently_renamed(tmp_path):
    """未登记的事件名按 ``unknown`` 记并留原名。

    静默改名会让「这事件去哪了」变成一道排障谜题；抛错又会让打日志
    把主流程带崩。这条走 ``log_event`` 入口（事件名的规范化只发生在那
    一处，Formatter 只负责渲染）。
    """
    try:
        configure_logging(log_dir=str(tmp_path), level="INFO")
        log_event("some.brand.new.event", message="新事件", period=PERIOD)
        for handler in __import__("logging").getLogger("reconq").handlers:
            handler.flush()
        blob = next(tmp_path.glob("reconq-*.jsonl")).read_text(encoding="utf-8")
        payload = json.loads(blob.splitlines()[0])
        assert payload["event"] == "unknown"
        assert payload["unregistered_event"] == "some.brand.new.event"
        # 原名没丢：排障时能直接按原名 grep 到这一条
        assert "新事件" in payload["message"]
    finally:
        reset_logging()


def test_registered_event_names_are_not_rewritten(tmp_path):
    """已登记的事件名原样落盘（反向断言，防止规范化写过头）。"""
    try:
        configure_logging(log_dir=str(tmp_path), level="INFO")
        log_event("run.start", message="开始", period=PERIOD)
        for handler in __import__("logging").getLogger("reconq").handlers:
            handler.flush()
        blob = next(tmp_path.glob("reconq-*.jsonl")).read_text(encoding="utf-8")
        payload = json.loads(blob.splitlines()[0])
        assert payload["event"] == "run.start"
        assert "unregistered_event" not in payload
    finally:
        reset_logging()
