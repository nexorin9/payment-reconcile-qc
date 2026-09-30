"""最窄端到端切片的测试：账单文件 → 契约 check → 证据行号。

守住 Task 3 的三条不变量：

1. 必填缺失必须**报出行号**，不是只报「有错」；
2. 金额差额必须给出**差额**与**贡献该差额的那一行**；
3. 0 行（本期没取到账单）绝不静默「全部通过」。
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from decimal import Decimal

import pytest

from reconq.cli import EXIT_CHECKS_FAILED, EXIT_ERROR, EXIT_OK, main
from reconq.contracts.loader import load_contract
from reconq.contracts.model import CheckYaml, ContractYaml
from reconq.engine.checks import (
    SUPPORTED_OPERATORS,
    BillRow,
    CheckContext,
    run_checks,
)
from reconq.errors import ReconqError
from reconq.sources.inbox_files import build_column_mapping, read_bill_file, read_inbox_dir

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONTRACTS = os.path.join(ROOT, "contracts")
FIXTURE_BILLS = os.path.join(HERE, "fixtures", "bills")
INBOX = os.path.join(ROOT, "inbox")
WECHAT_CONTRACT = os.path.join(CONTRACTS, "wechat.yml")
PERIOD = "2026-W36"


def _wechat_contract():
    return load_contract(WECHAT_CONTRACT, {"period": PERIOD})


def _ctx(period=PERIOD, timezone="Asia/Shanghai"):
    """跨日切分要在**运行期**知道本次扫的是哪一期、时区归一到哪边。

    同一份契约服务多个账期，这两个值不能烤进契约，所以走 CheckContext 传。
    """
    return CheckContext(period=period, session_timezone=timezone)


def _run(rows, contract=None, ctx=None):
    """跑本轮已实现的算子 —— 与 CLI 走的路径一致。

    CLI 会先用 ``--check`` 过滤出已实现的算子再交给执行器；直接
    ``run_checks(rows, contract)``（不过滤）遇到未实现算子会显式报错，
    那条路径由 ``test_unimplemented_operator_raises_instead_of_silent_pass`` 守住。
    """
    contract = contract or _wechat_contract()
    selected = [c for c, _ in contract.iter_checks() if c.type in SUPPORTED_OPERATORS]
    return run_checks(rows, contract, selected=selected, context=ctx or _ctx())


def _sample_rows():
    contract = _wechat_contract()
    return read_inbox_dir(FIXTURE_BILLS, PERIOD, "wechat", contract.table())


# —— 契约驱动的取数与列映射 ——————————————————————————————


def test_sample_bill_is_twenty_rows_with_real_wechat_headers():
    """样例账单按微信真实表头（中文）提供，不是自造英文列名。"""
    rows = _sample_rows()
    assert len(rows) == 20
    assert rows[0].get("out_trade_no") == "EX-WX-2026W36-0001"
    assert rows[0].source_file == "wechat_tradebill_2026-W36.csv"
    # 表头是第 1 行，首个数据行是第 2 行 —— 证据行号必须能回查源文件
    assert rows[0].row_no == 2
    assert rows[-1].row_no == 21


def test_chinese_header_maps_onto_contract_column_names():
    """中文表头归一到契约列名；归一后的键就是契约里写的英文列名。"""
    contract = _wechat_contract()
    table = contract.table()
    header = ["交易时间", "商户订单号", "微信交易单号", "交易状态", "应结订单金额", "退款金额"]
    mapping = build_column_mapping(header, table, "wechat")
    assert mapping["success_time"] == "交易时间"
    assert mapping["out_trade_no"] == "商户订单号"
    assert mapping["amount"] == "应结订单金额"
    assert "refund" in mapping


def test_header_totally_unmatched_raises_with_actual_header_listed():
    """表头完全对不上时报 BILL_COLUMN_MISSING，并把实际表头列出来。"""
    contract = _wechat_contract()
    path = os.path.join(FIXTURE_BILLS, PERIOD, "wechat", "wechat_tradebill_2026-W36.csv")
    with pytest.raises(ReconqError) as exc:
        # 借用 unionpay 契约（列名与微信表头毫无交集）去读微信账单
        read_bill_file(path, load_contract(os.path.join(CONTRACTS, "unionpay.yml")).table(), "unionpay")
    assert exc.value.code == "BILL_COLUMN_MISSING"
    assert "交易时间" in exc.value.detail


# —— 两条算子 ——————————————————————————————————————————————


def test_not_empty_lists_every_empty_row_with_row_number():
    """样例里故意留了两处必填缺失，两处都必须带行号报出来。"""
    contract = _wechat_contract()
    rows = _sample_rows()
    results = {r.check_path: r for r in _run(rows) if r.operator == "not_empty"}
    txn = results["transaction_id.not_empty"]
    order = results["out_trade_no.not_empty"]
    assert txn.status == "fail"
    assert order.status == "fail"
    assert [e.row_no for e in txn.evidence] == [6]
    assert [e.row_no for e in order.evidence] == [12]
    assert txn.evidence[0].source_file == "wechat_tradebill_2026-W36.csv"
    assert txn.evidence[0].field_name == "transaction_id"


def test_decimal_sum_reports_diff_and_the_contributing_row():
    """金额差额必须给差额，并指认真正贡献差额的那一行（不是全表 20 行）。"""
    contract = _wechat_contract()
    rows = _sample_rows()
    result = next(
        r
        for r in _run(rows)
        if r.check_path == "rows.refund_sum.zero"
    )
    assert result.status == "fail"
    assert Decimal(result.diff_amount) == Decimal("86.00")
    assert result.actual == "86.00"
    assert result.expected == "0.00"
    assert [e.row_no for e in result.evidence] == [17]
    assert result.evidence[0].value == "86.00"


def test_decimal_sum_without_expected_just_reports_total():
    """没给 expected 的 decimal_sum 只报合计，不判定成败。"""
    contract = _wechat_contract()
    rows = _sample_rows()
    result = next(
        r for r in _run(rows) if r.check_path == "rows.amount_sum.compare_his"
    )
    assert result.status == "pass"
    assert Decimal(result.actual) == Decimal("5714.69")
    assert result.diff_amount is None


def test_amount_uses_decimal_not_float():
    """金额一律 Decimal：0.1+0.2 必须等于 0.3，不允许 float 误差进门禁。"""
    contract = _wechat_contract()
    rows = [
        BillRow(values={"amount": "0.10", "refund": "0", "trade_state": "SUCCESS"}, row_no=2, raw_line="a"),
        BillRow(values={"amount": "0.20", "refund": "0", "trade_state": "SUCCESS"}, row_no=3, raw_line="b"),
    ]
    result = next(
        r
        for r in _run(rows)
        if r.check_path == "rows.amount_sum.compare_his"
    )
    assert result.actual == "0.30"


def test_where_clause_filters_rows_out_of_the_sum():
    """``where`` 过滤生效：非 SUCCESS 的行不计入订单金额合计。"""
    contract = _wechat_contract()
    rows = [
        BillRow(values={"amount": "100.00", "refund": "0", "trade_state": "SUCCESS"}, row_no=2, raw_line="a"),
        BillRow(values={"amount": "50.00", "refund": "0", "trade_state": "CLOSED"}, row_no=3, raw_line="b"),
    ]
    result = next(
        r
        for r in _run(rows)
        if r.check_path == "rows.amount_sum.compare_his"
    )
    assert result.actual == "100.00"


def test_non_numeric_amount_raises_with_row_number():
    """金额列出现非数字必须显式报错并给行号，不得静默跳过。"""
    contract = _wechat_contract()
    rows = [
        BillRow(
            values={"amount": "壹佰元", "refund": "0", "trade_state": "SUCCESS"},
            row_no=9,
            raw_line="x",
            source_file="wechat_tradebill_2026-W36.csv",
        )
    ]
    with pytest.raises(ReconqError) as exc:
        _run(rows)
    assert exc.value.code == "AMOUNT_UNPARSABLE"
    assert "wechat_tradebill_2026-W36.csv:9" in str(exc.value.row_ref)


def test_unimplemented_operator_raises_instead_of_silent_pass():
    """契约里声明了本轮**未注册**的算子时必须报错，不能当作通过。

    Task 15 把 unique 落进算子库后，这条用例改用一个真正没注册的名字：
    要守的不变式是「没实现的算子不静默通过」，而不是「unique 恰好没实现」。
    一条没跑的口径在看板上与「跑过并通过」长得一模一样，财务科据此签字
    就会放过一批没核过的账。
    """
    contract = _wechat_contract()
    rows = _sample_rows()
    unknown = _rebuild(
        "transaction_id.unique",
        contract_path=os.path.join(CONTRACTS, "wechat.yml"),
        set_values={"type": "sum_everything"},
    )
    with pytest.raises(ReconqError) as exc:
        run_checks(rows, unknown, selected=[c for c, _ in unknown.iter_checks() if c.type == "sum_everything"])
    assert exc.value.code == "CHECK_OPERATOR_UNSUPPORTED"
    # 报错文案要列出已注册的算子，让写契约的人知道能用什么
    assert "unique" in str(exc.value)


# —— CLI 主路径 ——————————————————————————————————————————————


def test_cli_verify_prints_channels_and_returns_nonzero_on_fail():
    """一条命令拿到可核对产物：有 fail 行、有证据行号、退出码非零。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(
            [
                "verify",
                "--period",
                PERIOD,
                "--source",
                FIXTURE_BILLS,
                "--contracts",
                CONTRACTS,
                "--channel",
                "wechat",
            ]
        )
    out = buf.getvalue()
    assert code == EXIT_CHECKS_FAILED
    assert "渠道 wechat" in out
    assert "20 行" in out
    assert "5714.69" in out
    assert "wechat_tradebill_2026-W36.csv:6" in out  # 证据行号可回查
    assert "wechat_tradebill_2026-W36.csv:12" in out
    assert "门禁不通过" in out


def test_cli_verify_reports_channel_without_bill_as_not_fetched():
    """没取到账单的那一路必须说「未取到」，不能报「全部通过」。

    用 ``his``：Task 5 给五路渠道都补了脱敏样例账单，HIS 只读库要到
    Task 6 才接上，所以它仍是「无账单文件」那一路。
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(
            [
                "verify",
                "--period",
                PERIOD,
                "--source",
                FIXTURE_BILLS,
                "--contracts",
                CONTRACTS,
                "--channel",
                "his",
            ]
        )
    out = buf.getvalue()
    assert "未取到" in out
    assert "全部口径通过" not in out
    # 没有取到任何数据 → 退出码走「未取到」而非门禁通过
    assert code == EXIT_OK


def test_cli_verify_all_channels_lists_five_not_fetched():
    """跑全部六路时，未取到的那一路要在汇总里点名。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["verify", "--period", PERIOD, "--source", FIXTURE_BILLS, "--contracts", CONTRACTS])
    out = buf.getvalue()
    # Task 5 后六路里只有 HIS 还没有账单文件（只读库在 Task 6 接）
    assert "未取到账单的渠道" in out
    assert "his" in out
    # 已取到的五路要真的出现在结果里，而不是被当成「未取到」
    for channel in ("wechat", "alipay", "unionpay", "self_service", "medical_ins"):
        assert f"=== 渠道 {channel}" in out


def test_cli_verify_json_output_is_machine_readable():
    """--json 输出可贴进工单的机器可读摘要。"""
    import json

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(
            [
                "verify",
                "--period",
                PERIOD,
                "--source",
                FIXTURE_BILLS,
                "--contracts",
                CONTRACTS,
                "--channel",
                "wechat",
                "--json",
            ]
        )
    payload = json.loads(buf.getvalue())
    assert payload["period"] == PERIOD
    wechat = next(c for c in payload["channels"] if c["channel"] == "wechat")
    assert wechat["row_count"] == 20
    assert wechat["check_failed"] == 3
    failed_paths = {r["check_path"] for r in wechat["results"] if r["status"] == "fail"}
    assert failed_paths == {
        "transaction_id.not_empty",
        "out_trade_no.not_empty",
        "rows.refund_sum.zero",
    }


def test_cli_verify_missing_contract_gives_error_code_not_traceback():
    """缺契约给明确错误码与退出码 2，不吐裸栈。"""
    import sys

    stderr = io.StringIO()
    old = sys.stderr
    sys.stderr = stderr
    try:
        code = main(
            [
                "verify",
                "--period",
                PERIOD,
                "--source",
                FIXTURE_BILLS,
                "--contracts",
                CONTRACTS,
                "--channel",
                "not_a_channel",
            ]
        )
    finally:
        sys.stderr = old
    assert code == EXIT_ERROR
    assert "CONTRACT_MISSING_KEY" in stderr.getvalue()


def test_demo_inbox_copy_is_in_place():
    """演示输入 inbox/<period>/wechat/ 已就位，README 快速开始可直接复现。"""
    assert os.path.isdir(os.path.join(INBOX, PERIOD, "wechat"))
    assert os.listdir(os.path.join(INBOX, PERIOD, "wechat"))


# ============================================================================
# Task 14：七条对账口径的 pass / fail 两路
#
# 每条口径都要能被下面这组用例钉住两件事：
#   1. 数据没问题时**真的**报 pass（不是「没抓到就当过」）；
#   2. 数据有问题时报 fail，且**带证据行号 + 差额**，财务科能回查原文件。
# 只测第 1 条的口径，等于没测 —— 永远不报红的口径和没有口径一样。
# ============================================================================


def _row(values, row_no=2, file="sample.csv"):
    return BillRow(values=values, row_no=row_no, raw_line="raw", source_file=file)


def _contract_for(channel: str, period=PERIOD):
    return load_contract(os.path.join(CONTRACTS, f"{channel}.yml"), {"period": period})


def _run_path(contract, path, rows, ctx=None):
    """只跑契约里某一条口径，返回该 CheckResult。"""
    wanted = [c for c, _ in contract.iter_checks() if c.path_str == path]
    assert wanted, f"契约里没有 path={path} 的口径"
    results = run_checks(contract=contract, rows=rows, selected=wanted, context=ctx or _ctx())
    assert len(results) == 1
    return results[0]


# —— 笔数（row_count）—————————————————————————————————————


def test_row_count_pass_and_fail_both_paths():
    """笔数下限：够则 pass，不够则 fail 并给出实际笔数。"""
    contract = _contract_for("wechat")
    rows = [_row({"trade_state": "SUCCESS"}, n) for n in range(2, 5)]
    ok = _run_path(contract, "rows.row_count", rows)
    assert ok.status == "pass"
    assert ok.actual == "3"
    assert "满足须 ≥ 1 笔" in ok.message

    empty = _run_path(contract, "rows.row_count", [])
    assert empty.status == "fail"
    assert empty.actual == "0"
    assert "0 笔" in empty.message
    # 低于下限时没有「多出来的行」，证据退为首行锚点（空表则无证据行）
    assert empty.evidence == []


def test_row_count_where_filter_splits_directions():
    """``where`` 把笔数切到指定方向上：只数 SUCCESS，不数 CLOSED。"""
    contract = _contract_for("wechat")
    rows = [
        _row({"trade_state": "SUCCESS"}, 2),
        _row({"trade_state": "CLOSED"}, 3),
        _row({"trade_state": "SUCCESS"}, 4),
    ]
    result = _run_path(contract, "rows.success_count", rows)
    assert result.status == "pass"
    assert result.actual == "2"
    assert "trade_state=SUCCESS" in result.message
    # 过滤后一笔都不剩时是 fail —— 「本期没有成功交易」不是「通过」
    assert _run_path(contract, "rows.success_count", [rows[1]]).status == "fail"


def test_row_count_exact_bound_flags_extra_rows_with_row_numbers():
    """``max`` 上限越界时，证据是**多出来的那几行**，可定位到源文件行号。"""
    contract = _contract_for("wechat")
    rows = [_row({"trade_state": "SUCCESS"}, n) for n in range(2, 6)]
    # 契约里 rows.row_count 只有 min，用等价的带 max 口径验证上限分支
    limited_contract = _rebuild("rows.row_count", set_values={"max": 2})
    result = _run_path(limited_contract, "rows.row_count", rows)
    assert result.status == "fail"
    assert result.actual == "4"
    assert [e.row_no for e in result.evidence] == [4, 5]
    assert result.evidence[0].source_file == "sample.csv"


def _rebuild(path, *, set_values=None, drop=None, contract_path=None):
    """复制一份契约，把其中一条 check 的 properties 改掉后重新解析。

    用于在不改动真实契约文件的前提下，构造某条口径的边界分支 ——
    上限越界、缺必填属性这类分支契约里本来就不该出现。``run_checks``
    按对象身份筛 ``selected``，所以必须整份契约一起换，不能只塞一个
    游离的 CheckYaml（那样它压根不会被遍历到，测的就不是这条口径了）。
    """
    import yaml as _yaml

    source = contract_path or os.path.join(CONTRACTS, "wechat.yml")
    with open(source, encoding="utf-8") as fh:
        raw = _yaml.safe_load(fh)
    target_path = path.split(".")
    table = raw["dataset"]["tables"][0]
    buckets = [("table", table.get("checks", []))]
    buckets += [("col", c.get("checks", [])) for c in table["columns"]]
    buckets.append(("root", raw.get("checks", [])))
    for _kind, items in buckets:
        for item in items:
            if item.get("path") == target_path:
                for key in drop or ():
                    item.pop(key, None)
                item.update(set_values or {})
                return ContractYaml.parse(_yaml.safe_dump(raw, allow_unicode=True), source_path=source)
    raise AssertionError(f"{source} 里找不到 path={path} 的口径")


# —— 枚举（in_enum）———————————————————————————————————————


def test_in_enum_pass_and_out_of_range_values_are_all_listed():
    """枚举越界：每个越界值都要报出行号，消息里给出合法集合。"""
    contract = _contract_for("wechat")
    good = [_row({"trade_state": "SUCCESS"}, n) for n in range(2, 5)]
    assert _run_path(contract, "trade_state.in_enum", good).status == "pass"

    bad = good + [
        _row({"trade_state": "半成功"}, 5),
        _row({"trade_state": "半成功"}, 6),
        _row({"trade_state": ""}, 7),
    ]
    result = _run_path(contract, "trade_state.in_enum", bad)
    assert result.status == "fail"
    assert result.evidence_total == 3
    assert [e.row_no for e in result.evidence] == [5, 6, 7]
    # 渠道原始值一字不改地带出来，合法集合也在消息里
    assert "半成功" in result.message and "（空）" in result.message
    # 合法集合逐条带在证据里，财务科不必翻契约就知道差在哪个取值
    assert "SUCCESS、REFUND、CLOSED、NOTPAY" in result.evidence[0].detail


def test_in_enum_treats_blank_as_out_of_range_not_as_legal():
    """空值**不算**枚举内：渠道没给状态与渠道确认状态是两件事。"""
    contract = _contract_for("wechat")
    result = _run_path(contract, "trade_state.in_enum", [_row({"trade_state": "  "}, 2)])
    assert result.status == "fail"
    assert result.evidence[0].value == ""
    assert "（空）" in result.evidence[0].detail


def test_in_enum_without_values_raises_path_error():
    """枚举口径没写 values 要显式报错，不能退化成「全通过」。"""
    bare_contract = _rebuild("trade_state.in_enum", drop=["values"])
    with pytest.raises(ReconqError) as exc:
        _run_path(bare_contract, "trade_state.in_enum", [_row({"trade_state": "SUCCESS"})])
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "values" in exc.value.detail


# —— 跨列等式 / 医保勾稽（column_equality）———————————————————


def _ins_row(total, fund, supp, aid, burden, other, personal, no=2, settle="现金"):
    return _row(
        {
            "settlement_no": f"EX-INS-{no:04d}",
            "total_amount": total,
            "fund_pool": fund,
            "supplementary": supp,
            "medical_aid": aid,
            "personal_burden": burden,
            "other_payment": other,
            "personal_payment": personal,
            "settle_type": settle,
        },
        no,
        "medical_ins_2026-W36.csv",
    )


def test_medical_ins_equation_one_holds_on_clean_sample():
    """勾稽等式一：金额合计 = 医保统筹 + 补充医疗 + 医疗救助 + 个人负担。

    依据苏医保办发〔2021〕38 号修订要点第四条。
    """
    contract = _contract_for("medical_ins")
    rows = [_ins_row("240.00", "110.00", "21.00", "11.00", "98.00", "68.00", "30.00", 2)]
    result = _run_path(contract, "rows.total_amount.equation", rows)
    assert result.status == "pass"
    assert result.diff_amount == "0.00"
    assert "total_amount = fund_pool + supplementary + medical_aid + personal_burden" in result.message


def test_medical_ins_equation_one_fails_with_row_number_and_gap():
    """等式被破坏：报**逐行差额 + 行号**，并给全表合计差额。"""
    contract = _contract_for("medical_ins")
    good = _ins_row("240.00", "110.00", "21.00", "11.00", "98.00", "68.00", "30.00", 2)
    # 少 8 元：110+21+11+90 = 232 ≠ 240
    bad = _ins_row("240.00", "110.00", "21.00", "11.00", "90.00", "60.00", "30.00", 3)
    result = _run_path(contract, "rows.total_amount.equation", [good, bad])
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3]
    assert "差 8.00 元" in result.evidence[0].detail
    assert result.diff_amount == "8.00"


def test_medical_ins_equation_two_is_independent_of_equation_one():
    """两条等式**各自独立断言**：等式一过了不代表等式二也过。"""
    contract = _contract_for("medical_ins")
    # 等式一成立：110+21+11+98=240
    # 等式二不成立：个人负担 98 ≠ 其他支付 68 + 个人支付 20 = 88
    row = _ins_row("240.00", "110.00", "21.00", "11.00", "98.00", "68.00", "20.00", 2)
    eq1 = _run_path(contract, "rows.total_amount.equation", [row])
    eq2 = _run_path(contract, "rows.personal_burden.equation", [row])
    assert eq1.status == "pass"
    assert eq2.status == "fail"
    assert eq2.diff_amount == "10.00"
    assert [e.row_no for e in eq2.evidence] == [2]


def test_column_equality_tolerates_sub_cent_rounding_only():
    """容差内判过：差 0.01 元以内按四舍五入放过，超过即报。"""
    contract = _contract_for("medical_ins")
    # 差 0.01，在容差内
    near = _ins_row("240.00", "110.00", "21.00", "11.00", "98.01", "68.00", "30.00", 2)
    assert _run_path(contract, "rows.total_amount.equation", [near]).status == "pass"
    # 差 0.02，超容差
    far = _ins_row("240.00", "110.00", "21.00", "11.00", "98.02", "68.00", "30.00", 2)
    assert _run_path(contract, "rows.total_amount.equation", [far]).status == "fail"


def test_column_equality_compare_value_form_checks_his_period_code():
    """HIS 账期码逐行须等于本次扫描账期（compare_column/compare_value 形态）。"""
    contract = _contract_for("his")
    rows = [_row({"period_code": "2026-W36"}, 2), _row({"period_code": "2026-W35"}, 3)]
    target = next(c for c, _ in contract.iter_checks() if c.path_str == "period_code.equal")
    result = run_checks(rows, contract, selected=[target], context=_ctx())[0]
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3]
    assert "2026-W36" in result.message


# —— 跨日切分（cross_period）———————————————————————————————


def test_cross_period_pass_inside_window():
    """账期窗口内：pass 且消息里带出窗口两端，便于核对。"""
    contract = _contract_for("wechat")
    rows = [_row({"success_time": "2026-09-02 10:00:00"}, 2)]
    result = _run_path(contract, "rows.success_time.in_period", rows)
    assert result.status == "pass"
    assert "2026-W36" in result.message
    assert "2026-08-31 00:00" in result.message
    assert "2026-09-07 00:00" in result.message


def test_cross_period_flags_row_outside_window_with_actual_落点():
    """窗口外：报行号，并把该笔实际落在哪天写进证据。"""
    contract = _contract_for("wechat")
    inside = _row({"success_time": "2026-09-02 10:00:00"}, 2)
    outside = _row({"success_time": "2026-09-07 00:00:01"}, 3)
    result = _run_path(contract, "rows.success_time.in_period", [inside, outside])
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3]
    assert "2026-09-07 00:00:01" in result.evidence[0].detail
    assert "之外" in result.evidence[0].detail


def test_cross_period_window_is_half_open_at_monday_midnight():
    """周一 00:00 归**新**账期：2026-W37 的首秒不算 W36 越界，反之亦然。"""
    contract = _contract_for("wechat")
    # 09-07 00:00:00 是 W37 的起点：属于 W37，不属于 W36
    exact = [_row({"success_time": "2026-09-07 00:00:00"}, 2)]
    assert _run_path(contract, "rows.success_time.in_period", exact, ctx=_ctx(period="2026-W37")).status == "pass"
    assert _run_path(contract, "rows.success_time.in_period", exact).status == "fail"
    # 08-31 00:00:00 是 W36 的起点：属于 W36
    start = [_row({"success_time": "2026-08-31 00:00:00"}, 2)]
    assert _run_path(contract, "rows.success_time.in_period", start).status == "pass"


def test_cross_period_without_period_context_refuses_to_guess():
    """运行上下文没给账期时**显式报错**，不许拿「今天」当账期判跨日切分。"""
    contract = _contract_for("wechat")
    rows = [_row({"success_time": "2026-09-02 10:00:00"}, 2)]
    with pytest.raises(ReconqError) as exc:
        _run_path(contract, "rows.success_time.in_period", rows, ctx=CheckContext())
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "period" in exc.value.detail


def test_cross_period_honours_session_timezone_normalization():
    """时区归一后才判窗口：+08:00 与 Asia/Shanghai 必须得出同一结论。"""
    contract = _contract_for("wechat")
    rows = [_row({"success_time": "2026-09-06T23:30:00+00:00"}, 2)]
    # UTC 23:30 = 东八区次日 07:30 → 越过 W36 右边界
    sh = _run_path(contract, "rows.success_time.in_period", rows, ctx=_ctx(timezone="Asia/Shanghai"))
    assert sh.status == "fail"
    utc = _run_path(contract, "rows.success_time.in_period", rows, ctx=_ctx(timezone="+00:00"))
    assert utc.status == "pass"  # UTC 口径下 09-06 23:30 仍在 W36 内


def test_cross_period_empty_time_is_flagged_not_ignored():
    """交易时间为空也要报：无法判定落在哪期 ≠ 落在期内。"""
    contract = _contract_for("wechat")
    result = _run_path(contract, "rows.success_time.in_period", [_row({"success_time": ""}, 2)])
    assert result.status == "fail"
    assert "为空" in result.evidence[0].detail


# —— 退费红冲（refund_offset）———————————————————————————————


def test_refund_offset_inline_form_pairs_with_own_order_row():
    """行内退费（微信形态）：退款记在原单行上，配对对象是本行原单金额。"""
    contract = _contract_for("wechat")
    rows = [
        _row({"out_trade_no": "EX-1", "amount": "112.40", "refund": "86.00", "trade_state": "SUCCESS"}, 2),
        _row({"out_trade_no": "EX-2", "amount": "50.00", "refund": "0.00", "trade_state": "SUCCESS"}, 3),
    ]
    result = _run_path(contract, "rows.refund.offset", rows)
    assert result.status == "pass"
    assert "行内退费" in result.message
    assert "退费/红冲 1 行" in result.message


def test_refund_offset_inline_form_flags_refund_exceeding_order():
    """行内退费金额不得大于原单金额：退 200、原单 100 一定是账错了。"""
    contract = _contract_for("wechat")
    rows = [_row({"out_trade_no": "EX-1", "amount": "100.00", "refund": "200.00", "trade_state": "SUCCESS"}, 2)]
    result = _run_path(contract, "rows.refund.offset", rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [2]
    assert "金额方向反了" in result.evidence[0].detail


def test_refund_offset_inline_form_flags_refund_on_dead_state():
    """未支付/已关闭的单不存在退款 —— 出现退款金额即报。"""
    contract = _contract_for("wechat")
    rows = [_row({"out_trade_no": "EX-1", "amount": "100.00", "refund": "30.00", "trade_state": "NOTPAY"}, 2)]
    result = _run_path(contract, "rows.refund.offset", rows)
    assert result.status == "fail"
    assert "NOTPAY" in result.evidence[0].detail


def test_refund_offset_separate_form_flags_unpaired_refund_row():
    """独立红冲行（HIS 形态）：同号找不到正向行 = 单边红冲。"""
    contract = _contract_for("self_service")
    rows = [
        _row({"out_trade_no": "EX-1", "amount": "100.00", "is_refund": "否"}, 2),
        _row({"out_trade_no": "EX-9", "amount": "30.00", "is_refund": "是"}, 3),
    ]
    result = _run_path(contract, "rows.is_refund.offset", rows)
    assert result.status == "fail"
    assert result.evidence_total == 1
    assert "EX-9" in result.evidence[0].detail
    assert "单边红冲" in result.evidence[0].detail


def test_refund_offset_separate_form_passes_when_paired():
    """成对时 pass：红冲行与同号正向行都在表内。"""
    contract = _contract_for("self_service")
    rows = [
        _row({"out_trade_no": "EX-1", "amount": "100.00", "is_refund": "否"}, 2),
        _row({"out_trade_no": "EX-1", "amount": "30.00", "is_refund": "是"}, 3),
    ]
    result = _run_path(contract, "rows.is_refund.offset", rows)
    assert result.status == "pass"
    assert "独立红冲行" in result.message


def test_refund_offset_boolean_flag_accepts_chinese_yes_no():
    """布尔列归一：「是/否」「true/false」「1/0」都要能命中 where 过滤。

    不归一的后果：红冲口径一条都命中不了，却仍报「0 条退费、通过」——
    口径形同虚设，而面板上看不出任何异常。
    """
    from reconq.engine.checks import matches_where

    for text in ("是", "true", "True", "TRUE", "1", "Y", "y", "yes"):
        assert matches_where(_row({"is_refund": text}), {"is_refund": True}) is True, text
    for text in ("否", "false", "0", "N", "no", ""):
        assert matches_where(_row({"is_refund": text}), {"is_refund": True}) is False, text
    # 认不出的写法不当成 False，而是「不命中」——由调用方去报口径配置问题
    assert matches_where(_row({"is_refund": "待定"}), {"is_refund": True}) is False


# —— 金额合计对照基准（decimal_sum + 运行期基准）—————————————


def test_decimal_sum_uses_runtime_baseline_when_contract_has_no_expected():
    """契约没写死 expected、但运行期给了 HIS 侧基准时，要比出差额。"""
    contract = _contract_for("wechat")
    rows = [
        _row({"amount": "100.00", "refund": "0", "trade_state": "SUCCESS"}, 2),
        _row({"amount": "50.00", "refund": "0", "trade_state": "SUCCESS"}, 3),
    ]
    ctx = CheckContext(period=PERIOD, compare_baselines={"rows.amount_sum.compare_his": Decimal("150.00")})
    ok = _run_path(contract, "rows.amount_sum.compare_his", rows, ctx=ctx)
    assert ok.status == "pass"
    assert ok.actual == "150.00"
    assert ok.diff_amount == "0.00"
    assert "HIS 侧对照基准" in ok.message

    bad_ctx = CheckContext(period=PERIOD, compare_baselines={"rows.amount_sum.compare_his": Decimal("140.00")})
    bad = _run_path(contract, "rows.amount_sum.compare_his", rows, ctx=bad_ctx)
    assert bad.status == "fail"
    assert bad.diff_amount == "10.00"
    # 证据指认真正贡献金额的行，而不是把两行全列出来
    assert [e.row_no for e in bad.evidence] == [2, 3]


def test_decimal_sum_without_baseline_says_it_only_reports_total():
    """既无 expected 又无基准时，必须说清「只报合计、不判定」，不能装作通过。"""
    contract = _contract_for("wechat")
    rows = [_row({"amount": "100.00", "refund": "0", "trade_state": "SUCCESS"}, 2)]
    result = _run_path(contract, "rows.amount_sum.compare_his", rows)
    assert result.diff_amount is None
    assert "未提供对照基准" in result.message


# —— 证据截断 ——————————————————————————————————————————————


def test_evidence_truncation_is_explicit_not_silent():
    """证据超过上限要显式标 truncated 并给总数，不静默截断。"""
    contract = _contract_for("wechat")
    rows = [
        _row({"trade_state": "异常"}, n) for n in range(2, 12)
    ]  # 10 行越界
    result = _run_path(contract, "trade_state.in_enum", rows, ctx=_ctx())
    full = run_checks(
        rows,
        contract,
        selected=[c for c, _ in contract.iter_checks() if c.path_str == "trade_state.in_enum"],
        context=_ctx(),
        max_evidence=3,
    )[0]
    assert full.status == "fail"
    assert len(full.evidence) == 3
    assert full.truncated is True
    assert full.evidence_total == 10
    assert "共 10 条" in full.message
    assert result.truncated is False


# —— CLI 主路径：六路口径全跑 ——————————————————————————————


def test_cli_runs_all_seven_checks_per_channel():
    """一条命令把六路口径全跑一遍，终端能看全每条口径的判定。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(
            [
                "verify",
                "--period",
                PERIOD,
                "--source",
                FIXTURE_BILLS,
                "--contracts",
                CONTRACTS,
            ]
        )
    out = buf.getvalue()
    # 七条口径里，这批样例账单都应至少被跑到一次
    for op_path in (
        "rows.row_count",
        "trade_state.in_enum",
        "rows.amount_sum.compare_his",
        "rows.success_time.in_period",
        "rows.refund.offset",
    ):
        assert op_path in out, op_path
    # 医保两条勾稽等式
    assert "rows.total_amount.equation" in out
    assert "rows.personal_burden.equation" in out
    # 样例里刻意留的缺陷仍要报出来
    assert "wechat_tradebill_2026-W36.csv:6" in out
    assert "wechat_tradebill_2026-W36.csv:12" in out
    assert "wechat_tradebill_2026-W36.csv:17" in out
    assert code == EXIT_CHECKS_FAILED


def test_cli_json_carries_every_check_result_with_evidence():
    """--json 摘要逐条带证据，信息科可贴进工单。"""
    import json

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(
            [
                "verify",
                "--period",
                PERIOD,
                "--source",
                FIXTURE_BILLS,
                "--contracts",
                CONTRACTS,
                "--channel",
                "wechat",
                "--json",
            ]
        )
    payload = json.loads(buf.getvalue())
    wechat = next(c for c in payload["channels"] if c["channel"] == "wechat")
    by_path = {r["check_path"]: r for r in wechat["results"]}
    # Task 3 的三条缺陷仍在，且各自带行号
    assert by_path["transaction_id.not_empty"]["status"] == "fail"
    assert by_path["transaction_id.not_empty"]["evidence"][0]["row_no"] == 6
    assert by_path["rows.refund_sum.zero"]["diff_amount"] == "86.00"
    # 新增口径也进来了
    assert by_path["trade_state.in_enum"]["status"] == "pass"
    assert by_path["rows.success_time.in_period"]["status"] == "pass"
    assert by_path["rows.refund.offset"]["status"] == "pass"
    # 证据行带 detail，财务科不必再翻原文件才知道差在哪
    refund_zero = by_path["rows.refund_sum.zero"]
    assert "86.00" in refund_zero["evidence"][0]["detail"]


def test_six_contracts_all_declare_at_least_one_task14_check():
    """六份契约每份至少挂上一条 Task 14 口径，保证每路都真在跑。"""
    task14 = {"not_empty", "in_enum", "row_count", "decimal_sum", "column_equality", "cross_period", "refund_offset"}
    for channel in ("wechat", "alipay", "unionpay", "self_service", "his", "medical_ins"):
        contract = _contract_for(channel)
        types = {c.type for c, _ in contract.iter_checks()}
        assert types & task14, f"{channel} 契约没挂任何 Task 14 口径"


def test_unimplemented_operator_still_raises_after_task15():
    """算子库落地之后，**未注册**的算子仍须显式报未实现。

    Task 15 之前这条用例盯的是 unique/decimal_scale「还没实现」；算子库把它们
    实现了，所以这里改盯一个不存在的算子名 —— 不变式是分派表里没有就报错，
    而不是「某个具体算子恰好缺席」。
    """
    contract = _rebuild(
        "transaction_id.unique",
        contract_path=os.path.join(CONTRACTS, "wechat.yml"),
        set_values={"type": "no_such_operator"},
    )
    rows = _sample_rows()
    with pytest.raises(ReconqError) as exc:
        run_checks(
            rows,
            contract,
            selected=[c for c, _ in contract.iter_checks() if c.type == "no_such_operator"],
            context=_ctx(),
        )
    assert exc.value.code == "CHECK_OPERATOR_UNSUPPORTED"
    # 反面：unique / decimal_scale 现在必须**能跑**，且跑出真实判定
    ok_contract = _contract_for("wechat")
    results = run_checks(
        rows,
        ok_contract,
        selected=[c for c, _ in ok_contract.iter_checks() if c.type in ("unique", "decimal_scale")],
        context=_ctx(),
    )
    assert {r.operator for r in results} == {"unique", "decimal_scale"}
    assert all(r.status in ("pass", "fail") for r in results)
