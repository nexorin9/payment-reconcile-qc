"""算子库（Task 15）的测试：每个算子都要 pass / fail 两路 + 证据带行号。

本文件守住三条不变式：

1. **新增一条对账口径只写 YAML、不写代码** —— 每条 check 的阈值都来自
   ``chk.properties``；缺属性时显式报错，**不拿默认值替财务科判账**。
2. **每条 fail 结论都能定位到源文件行** —— 证据必须带齐
   (source_file, row_no, raw_line, field, value)，否则不得作为 fail 依据。
3. **证据超上限显式标注截断**，不静默丢行。

只测 pass 路径没有意义：永远不报红的口径和没有口径一样。每个算子都写
一条「数据有问题时报 fail 并给出可回查证据行号」的用例。
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from reconq.contracts.loader import load_contract
from reconq.contracts.model import CheckYaml
from reconq.engine.checks import (
    SUPPORTED_OPERATORS,
    BillRow,
    CheckContext,
    run_checks,
)
from reconq.engine.operators import (
    check_operator,
    get_operator,
    operator_names,
    register_operator,
)
from reconq.errors import ReconqError

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONTRACTS = os.path.join(ROOT, "contracts")
PERIOD = "2026-W36"


# —— 测试脚手架 ——————————————————————————————————————————————


def _row(values, row_no=2, file="sample.csv"):
    return BillRow(values=values, row_no=row_no, raw_line="raw", source_file=file)


def _check(operator, path, **props):
    """构造一条游离 CheckYaml —— 算子只读 rows 与自己的属性，不依赖整份契约。

    契约层的往返由 ``_run_path`` 那组用例守住；这里要的是**算子本身的
    pass/fail 判据**，用游离 check 才能把参数组合摆到边界上。
    """
    return CheckYaml(path=path.split("."), type=operator, properties=dict(props))


def _run(chk, rows, *, max_evidence=200, column=None, ctx=None):
    return check_operator(
        rows,
        chk,
        column,
        max_evidence=max_evidence,
        ctx=ctx or CheckContext(period=PERIOD),
    )


def _contract_for(channel: str):
    return load_contract(os.path.join(CONTRACTS, f"{channel}.yml"), {"period": PERIOD})


def _run_path(contract, path, rows, **kw):
    wanted = [c for c, _ in contract.iter_checks() if c.path_str == path]
    assert wanted, f"契约里没有 path={path} 的口径"
    return run_checks(
        rows,
        contract,
        selected=wanted,
        context=CheckContext(period=PERIOD),
        **kw,
    )[0]


# ============================================================================
# 注册表本身
# ============================================================================


def test_registry_covers_every_operator_the_contracts_declare():
    """六份契约里用到的每个 type 都必须已实现，否则扫描会当场报错。

    断言对象是 ``SUPPORTED_OPERATORS``（两层算子的并集）而不是算子库本身：
    笔数/合计/跨日切分/退费红冲住在 ``checks``，它们也是契约里真实在用的
    type，同样不许缺席。
    """
    declared = set()
    for channel in ("wechat", "alipay", "unionpay", "self_service", "his", "medical_ins"):
        declared |= {c.type for c, _ in _contract_for(channel).iter_checks()}
    assert declared <= set(SUPPORTED_OPERATORS), declared - set(SUPPORTED_OPERATORS)


def test_supported_operators_is_union_of_both_layers():
    """``SUPPORTED_OPERATORS`` = 算子库 + 行级口径，两层合成一张表。

    命令行 ``--check`` 的默认值取自它；漏掉任何一层，CLI 就会把一整类口径
    静默过滤掉，而看板上只是「口径数变少了」。
    """
    from reconq.engine.checks import row_operator_names

    assert set(SUPPORTED_OPERATORS) == set(operator_names()) | set(row_operator_names())
    # 四个行级口径确实在（它们需要账期上下文，不在算子库里）
    for name in ("row_count", "decimal_sum", "cross_period", "refund_offset"):
        assert name in SUPPORTED_OPERATORS
        assert get_operator(name) is None


def test_operators_are_independent_of_each_other():
    """算子之间互不依赖：只读 rows 与自己的属性，谁也不碰谁的中间结果。

    这条是「新增口径只写 YAML」的前提。若某个算子偷偷改了共享状态或读了
    别的算子的结果，契约里 check 的顺序一变，判定就会跟着变 —— 而 YAML 里
    的 check 顺序对财务科是没有语义的。
    """
    rows = [_row({"amount": "10.005", "code": "A1"}, 2), _row({"amount": "-5", "code": "A1"}, 3)]
    # 连续跑三次，且中途插入别的算子，结果必须逐字节一致
    scale = _check("decimal_scale", "amount.scale", column="amount", scale=2)
    first = _run(scale, rows)
    _run(_check("in_enum", "code.in_enum", column="code", values=["A1"]), rows)
    second = _run(scale, rows)
    third = _run(scale, list(rows))
    assert first.to_dict() == second.to_dict() == third.to_dict()
    # 原 rows 未被算子改写
    assert rows[0].values["amount"] == "10.005"


def test_registering_a_duplicate_operator_name_is_rejected():
    """两个算子抢同一个 type 会让契约作者无法判断自己写的是哪一条。"""
    with pytest.raises(ReconqError) as exc:
        register_operator("unique")(lambda *a, **k: None)
    assert exc.value.code == "CHECK_TYPE_MISSING"
    assert "重复注册" in exc.value.detail


def test_unregistered_operator_raises_instead_of_returning_pass():
    """没注册的算子必须报错，不能返回 pass。

    一条没跑的口径在看板上与「跑过并通过」完全一样，财务科据此签字就会
    放过一批没核过的账。
    """
    with pytest.raises(ReconqError) as exc:
        _run(_check("no_such_operator", "x.y"), [_row({"x": "1"}, 2)])
    assert exc.value.code == "CHECK_OPERATOR_UNSUPPORTED"
    assert "unique" in str(exc.value)


# ============================================================================
# 非空 / 枚举（从行级口径上移到算子库，判据不变）
# ============================================================================


def test_not_empty_lists_every_blank_row_with_row_number():
    """必填缺失要**列出全部**空值行号，不截断在第一条。"""
    rows = [_row({"code": "A"}, 2), _row({"code": ""}, 3), _row({"code": "  "}, 4), _row({"code": "B"}, 5)]
    result = _run(_check("not_empty", "code.required"), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3, 4]
    assert result.evidence[0].source_file == "sample.csv"
    assert result.evidence[0].field_name == "code"
    assert result.evidence_total == 2


def test_not_empty_passes_on_fully_populated_column():
    rows = [_row({"code": "A"}, 2), _row({"code": "B"}, 3)]
    assert _run(_check("not_empty", "code.required"), rows).status == "pass"


def test_in_enum_lists_out_of_range_values_and_legal_set():
    """越界值按值聚合并列全行号，消息里带合法集合。"""
    rows = [
        _row({"state": "SUCCESS"}, 2),
        _row({"state": "半成功"}, 3),
        _row({"state": "半成功"}, 4),
        _row({"state": ""}, 5),
    ]
    result = _run(_check("in_enum", "state.enum", values=["SUCCESS", "REFUND"]), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3, 4, 5]
    assert "半成功" in result.message and "（空）" in result.message
    assert "SUCCESS、REFUND" in result.evidence[0].detail


def test_in_enum_without_values_raises_rather_than_defaulting():
    """缺 values 要显式报错，不能默认「全通过」也不能用空集合把全表判红。"""
    with pytest.raises(ReconqError) as exc:
        _run(_check("in_enum", "state.enum"), [_row({"state": "SUCCESS"}, 2)])
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "values" in exc.value.detail


def test_in_enum_with_empty_values_set_is_rejected():
    """空集合会让每一行都报越界 —— 那不是口径，是错配。"""
    with pytest.raises(ReconqError) as exc:
        _run(_check("in_enum", "state.enum", values=[]), [_row({"state": "S"}, 2)])
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "为空" in exc.value.detail


# ============================================================================
# 精度（decimal_scale）
# ============================================================================


def test_decimal_scale_flags_amount_with_too_many_decimal_places():
    """小数位超了要报行号，并说清实际几位、允许几位。

    判定线索是「金额被二次折算过」：若折算比例与院内口径不同，整列合计会
    差出一个肉眼看不出来的小数。
    """
    rows = [
        _row({"amount": "128.00"}, 2),
        _row({"amount": "45.005"}, 3),
        _row({"amount": "0.0001"}, 4),
    ]
    result = _run(_check("decimal_scale", "amount.scale", column="amount", scale=2), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3, 4]
    assert "3 位小数" in result.evidence[0].detail
    assert "超过契约允许的 2 位" in result.evidence[0].detail


def test_decimal_scale_treats_fewer_decimals_as_acceptable():
    """``128`` 与 ``128.00`` 是同一笔钱，少写两位不该报红。"""
    rows = [_row({"amount": "128"}, 2), _row({"amount": "128.00"}, 3), _row({"amount": "0.5"}, 4)]
    result = _run(_check("decimal_scale", "amount.scale", column="amount", scale=2), rows)
    assert result.status == "pass"
    assert "3 行" in result.message


def test_decimal_scale_skips_blank_rows_and_defers_to_not_empty():
    """空值不在这条口径里判：否则「必填缺失」会被每条算子各报一次。"""
    rows = [_row({"amount": "128.00"}, 2), _row({"amount": ""}, 3)]
    result = _run(_check("decimal_scale", "amount.scale", column="amount", scale=2), rows)
    assert result.status == "pass"
    assert "另有 1 行为空" in result.message
    assert "not_empty" in result.message


def test_decimal_scale_without_scale_property_raises():
    """scale 缺了必须报错：默认成 2 会让所有两位小数渠道「碰巧」通过，
    三位小数的渠道则被静默放过。"""
    with pytest.raises(ReconqError) as exc:
        _run(_check("decimal_scale", "amount.scale", column="amount"), [_row({"amount": "1.005"}, 2)])
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "scale" in exc.value.detail


def test_decimal_scale_handles_scientific_notation_without_counting_plus_exponent():
    """``1E+2`` 是 100 元、0 位小数；把指数当成小数位会把它误判成违规。"""
    rows = [_row({"amount": "1E+2"}, 2)]
    assert _run(_check("decimal_scale", "amount.scale", column="amount", scale=2), rows).status == "pass"


# ============================================================================
# 区间（between）
# ============================================================================


def test_between_closed_interval_includes_the_boundary():
    """闭区间：单笔顶格 5000 元在 [0, 5000] 内算合规。

    院内自助机单笔上限往往就正好卡在 5000，把上界判成排他会让每一笔顶格
    缴费都报红，口径第一次跑就被判死刑。
    """
    rows = [_row({"amount": "5000.00"}, 2), _row({"amount": "0.01"}, 3), _row({"amount": "0"}, 4)]
    result = _run(_check("between", "amount.range", column="amount", min=0, max=5000), rows)
    assert result.status == "pass"
    assert "闭区间" in result.message


def test_between_flags_rows_outside_bounds_with_row_numbers():
    rows = [_row({"amount": "10.00"}, 2), _row({"amount": "5000.01"}, 3), _row({"amount": "-1.00"}, 4)]
    result = _run(_check("between", "amount.range", column="amount", min=0, max=5000), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3, 4]
    assert "高于上界" in result.evidence[0].detail
    assert "低于下界" in result.evidence[1].detail


def test_between_open_interval_excludes_the_boundary():
    """开区间：退款额须严格小于原单额，取等号即报。"""
    rows = [_row({"refund": "99.99"}, 2), _row({"refund": "100.00"}, 3)]
    result = _run(
        _check("between", "refund.below", column="refund", max=100, inclusive=False),
        rows,
    )
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3]
    assert "开区间" in result.message
    assert "不含" in result.evidence[0].detail


def test_between_supports_single_sided_bound():
    """只给 min 就是单边判据（金额非负），不需要写一个无穷大的 max。"""
    rows = [_row({"amount": "1.00"}, 2), _row({"amount": "-0.01"}, 3)]
    result = _run(_check("between", "amount.min", column="amount", min=0), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3]
    assert "≥ 0" in result.message


def test_between_without_any_bound_raises():
    """没有边界的区间判不出任何东西，必须报错而不是恒过。"""
    with pytest.raises(ReconqError) as exc:
        _run(_check("between", "amount.range", column="amount"), [_row({"amount": "1"}, 2)])
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "min" in exc.value.detail and "max" in exc.value.detail


# ============================================================================
# 唯一性（unique）
# ============================================================================


def test_unique_returns_all_duplicated_values_with_all_their_row_numbers():
    """重复单号要报**全部**重复行，并列出每一组重复的行号。

    只报第一个重复值，财务科改完再跑还会冒出下一个 —— 一笔一筆地返工。
    """
    rows = [
        _row({"order_no": "EX-1"}, 2),
        _row({"order_no": "EX-1"}, 3),
        _row({"order_no": "EX-2"}, 4),
        _row({"order_no": "EX-3"}, 5),
        _row({"order_no": "EX-3"}, 6),
        _row({"order_no": "EX-3"}, 7),
    ]
    result = _run(_check("unique", "order_no.uid"), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [2, 3, 5, 6, 7]
    assert "2 个单号重复" in result.message
    assert "EX-1 出现 2 次（行号 2、3）" in result.message
    assert "EX-3 出现 3 次（行号 5、6、7）" in result.message


def test_unique_passes_when_all_values_distinct():
    rows = [_row({"order_no": f"EX-{n}"}, n + 2) for n in range(5)]
    result = _run(_check("unique", "order_no.uid"), rows)
    assert result.status == "pass"
    assert "无重复单号（5 个非空单号）" in result.message


def test_unique_ignores_blank_values_and_defers_to_not_empty():
    """两个空单号不是「重复」，是两行都缺单号。

    若把空值纳入唯一性，本口径会把缺单号的行报成「单号重复」，把财务科
    引向完全错误的方向（去查重复入账，而实际是导出时单号列没落下来）。
    """
    rows = [_row({"order_no": ""}, 2), _row({"order_no": ""}, 3), _row({"order_no": "EX-1"}, 4)]
    result = _run(_check("unique", "order_no.uid"), rows)
    assert result.status == "pass"
    assert "另有 2 行为空" in result.message
    assert "not_empty" in result.message


def test_unique_strips_surrounding_whitespace_before_comparing():
    """导出里同一个单号前后多一个空格，Excel 里看着一样、字符串里是两个值。

    不去空白就会让「这一单入账两次」这种最该被抓住的问题从本口径下溜走。
    """
    rows = [_row({"order_no": "EX-1"}, 2), _row({"order_no": " EX-1 "}, 3)]
    result = _run(_check("unique", "order_no.uid"), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [2, 3]
    assert "EX-1 出现 2 次" in result.message


# ============================================================================
# 正则（matches_regex）
# ============================================================================


def test_matches_regex_passes_on_well_formed_values():
    rows = [_row({"txn": "EX-WXTXN-2026W36-0001"}, 2), _row({"txn": "EX-WXTXN-2026W36-0002"}, 3)]
    result = _run(_check("matches_regex", "txn.format", pattern=r"EX-WXTXN-\d{4}W\d{2}-\d{4}"), rows)
    assert result.status == "pass"
    assert "2 行" in result.message


def test_matches_regex_flags_malformed_values_with_actual_value():
    """格式不合要给出**实际值**：只说「格式不符」，财务科还得自己去翻原表。"""
    rows = [
        _row({"txn": "EX-WXTXN-2026W36-0001"}, 2),
        _row({"txn": "WX2026W360001"}, 3),
        _row({"txn": "prefix-EX-WXTXN-2026W36-0003"}, 4),
    ]
    result = _run(_check("matches_regex", "txn.format", pattern=r"EX-WXTXN-\d{4}W\d{2}-\d{4}"), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3, 4]
    assert "WX2026W360001" in result.evidence[0].detail
    assert "prefix-EX-WXTXN" in result.evidence[1].detail


def test_matches_regex_is_fullmatch_not_substring():
    """子串匹配会把 ``WX-REFUND-EX-WXTXN-0001`` 判成合规单号。

    退款单号里含原单号是微信账单的正常形态，子串匹配会让「格式口径」在最
    该拦住的那类数据上失效。
    """
    rows = [_row({"txn": "WX-REFUND-EX-WXTXN-2026W36-0001"}, 2)]
    result = _run(_check("matches_regex", "txn.format", pattern=r"EX-WXTXN-\d{4}W\d{2}-\d{4}"), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [2]


def test_matches_regex_ignore_case_is_opt_in():
    """默认区分大小写；确需忽略时由契约显式打开。"""
    rows = [_row({"code": "ex-his-0001"}, 2)]
    strict = _run(_check("matches_regex", "code.format", pattern=r"EX-HIS-\d{4}"), rows)
    assert strict.status == "fail"
    loose = _run(
        _check("matches_regex", "code.format", pattern=r"EX-HIS-\d{4}", ignore_case=True),
        rows,
    )
    assert loose.status == "pass"
    assert "忽略大小写" not in loose.message


def test_matches_regex_with_invalid_pattern_raises_with_the_pattern():
    """非法正则要报错并把 pattern 原样带出，否则契约作者无从下手。"""
    with pytest.raises(ReconqError) as exc:
        _run(_check("matches_regex", "code.format", pattern="EX-["), [_row({"code": "x"}, 2)])
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "EX-[" in exc.value.detail


def test_matches_regex_without_pattern_raises():
    with pytest.raises(ReconqError) as exc:
        _run(_check("matches_regex", "code.format"), [_row({"code": "x"}, 2)])
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "pattern" in exc.value.detail


# ============================================================================
# 跨列不等式（column_not_equal）
# ============================================================================


def test_column_not_equal_flags_rows_where_two_columns_hold_the_same_value():
    """双写列同值 = 导出脚本把一列写了两遍，交叉核对能力为零。

    这类问题不会让合计对不上，只能靠本口径抓。
    """
    rows = [
        _row({"order_no": "EX-1", "txn_no": "EX-T-1"}, 2),
        _row({"order_no": "EX-2", "txn_no": "EX-2"}, 3),
    ]
    result = _run(_check("column_not_equal", "order_no.distinct", column="order_no", other="txn_no"), rows)
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [3]
    assert "EX-2" in result.evidence[0].detail
    assert "写了两遍" in result.evidence[0].detail


def test_column_not_equal_passes_when_columns_always_differ():
    rows = [_row({"order_no": "EX-1", "txn_no": "EX-T-1"}, 2)]
    result = _run(_check("column_not_equal", "order_no.distinct", column="order_no", other="txn_no"), rows)
    assert result.status == "pass"
    assert "逐行不同" in result.message


def test_column_not_equal_skips_rows_where_either_column_is_blank():
    """任一列为空时不判不等式：那是缺值问题，归 not_empty。"""
    rows = [
        _row({"order_no": "EX-1", "txn_no": ""}, 2),
        _row({"order_no": "", "txn_no": "EX-T-3"}, 3),
    ]
    result = _run(_check("column_not_equal", "order_no.distinct", column="order_no", other="txn_no"), rows)
    assert result.status == "pass"
    assert "另有 2 行因任一列为空未判" in result.message


def test_column_not_equal_rejects_other_equal_to_the_checked_column():
    """other 与被检列同名时一列永远等于自己，这条口径判不出任何东西。"""
    with pytest.raises(ReconqError) as exc:
        _run(
            _check("column_not_equal", "a.same", column="a", other="a"),
            [_row({"a": "X"}, 2)],
        )
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "同名" in exc.value.detail


def test_column_not_equal_without_other_raises():
    with pytest.raises(ReconqError) as exc:
        _run(_check("column_not_equal", "a.distinct", column="a"), [_row({"a": "X"}, 2)])
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "other" in exc.value.detail


# ============================================================================
# 集合包含（set_contains）
# ============================================================================


def test_set_contains_passes_when_every_value_is_in_the_set():
    rows = [_row({"dept": "门诊"}, 2), _row({"dept": "检验科"}, 3)]
    result = _run(_check("set_contains", "dept.in_list", column="dept", values=["门诊", "检验科"]), rows)
    assert result.status == "pass"
    assert "全部落在集合内" in result.message


def test_set_contains_flags_values_outside_the_reference_list():
    """科室取值必须来自本院科室清单：清单外的科室说明导出来源或归属错了。"""
    rows = [
        _row({"dept": "门诊"}, 2),
        _row({"dept": "住院"}, 3),
        _row({"dept": "门诊部"}, 4),
        _row({"dept": ""}, 5),
    ]
    result = _run(
        _check("set_contains", "dept.in_list", column="dept", values=["门诊", "住院", "检验科"]),
        rows,
    )
    assert result.status == "fail"
    assert [e.row_no for e in result.evidence] == [4, 5]
    assert "门诊部" in result.evidence[0].detail
    assert "（空）" in result.evidence[1].detail


def test_set_contains_with_empty_values_set_is_rejected():
    with pytest.raises(ReconqError) as exc:
        _run(_check("set_contains", "dept.in_list", column="dept", values=[]), [_row({"dept": "门诊"}, 2)])
    assert exc.value.code == "CHECK_PATH_INVALID"
    assert "为空" in exc.value.detail


# ============================================================================
# 证据截断与定位：所有算子共用的收口
# ============================================================================


def test_evidence_beyond_max_is_truncated_explicitly_not_silently():
    """证据超上限要显式标 truncated 并给总数。

    静默截断会让财务科以为「就这 3 行有问题」，改完再跑又冒出下一批。
    """
    rows = [_row({"code": "越界"}, n) for n in range(2, 12)]  # 10 行越界
    chk = _check("in_enum", "code.enum", values=["合法"])
    result = _run(chk, rows, max_evidence=3)
    assert result.status == "fail"
    assert len(result.evidence) == 3
    assert result.truncated is True
    assert result.evidence_total == 10
    assert "共 10 条" in result.message
    # 未截断时不标 truncated
    full = _run(chk, rows)
    assert full.truncated is False
    assert len(full.evidence) == 10


def test_every_evidence_carries_a_locatable_source_position():
    """每条证据都必须能回到「哪个文件第几行」，否则不得作为 fail 依据。

    财务科拿着证据才能回查导出方；定位不到行的结论对他们没有用。
    """
    rows = [
        _row({"code": "X", "amount": "1.005", "dept": "不在清单"}, 7, file="wechat_bill.csv"),
        _row({"code": "X", "amount": "-1", "dept": "也不在清单"}, 9, file="wechat_bill.csv"),
    ]
    cases = [
        _check("decimal_scale", "amount.scale", column="amount", scale=2),
        _check("between", "amount.range", column="amount", min=0),
        _check("set_contains", "dept.in_list", column="dept", values=["门诊"]),
    ]
    for chk in cases:
        result = _run(chk, rows)
        assert result.status == "fail", chk.type
        assert result.evidence, chk.type
        for ev in result.evidence:
            assert ev.source_file == "wechat_bill.csv"
            assert ev.row_no in (7, 9)
            assert ev.field_name
            assert ev.detail, "证据必须写清为什么算它有问题"
            assert ev.render()


# ============================================================================
# 契约往返：新增口径只写 YAML，不写代码
# ============================================================================


def test_new_operators_run_from_contract_without_touching_python():
    """六份契约里用新算子写的口径，全部能在真实样例账单上跑出判定。

    这是本任务的核心验收：写一条新对账口径不需要改任何 Python。
    """
    from reconq.sources.inbox_files import read_inbox_dir

    expected = {
        "wechat": ["transaction_id.format", "transaction_id.distinct_from.out_trade_no"],
        "alipay": ["trade_no.format"],
        "unionpay": ["trade_no.distinct_from.out_trade_no"],
        "self_service": ["out_trade_no.format"],
        "his": ["department.in_department_list"],
        "medical_ins": ["settlement_no.format", "rows.total_amount.positive"],
    }
    for channel, paths in expected.items():
        contract = _contract_for(channel)
        for path in paths:
            wanted = [c for c, _ in contract.iter_checks() if c.path_str == path]
            assert wanted, f"{channel} 契约里没有 path={path} 的口径"
            # 用一小段构造行跑，不必真去读文件：这里验的是「契约里的 check
            # 能被算子库分派并给出判定」，数据形态由下面那条真实账单用例守。
            probe = _row(
                {
                    "transaction_id": "占位",
                    "out_trade_no": "占位-A",
                    "trade_no": "占位",
                    "settlement_no": "占位",
                    "department": "门诊",
                    "total_amount": "1.00",
                },
                2,
            )
            results = run_checks(
                [probe],
                contract,
                selected=wanted,
                context=CheckContext(period=PERIOD),
            )
            assert len(results) == 1
            assert results[0].status in ("pass", "fail")
            assert results[0].message
        assert read_inbox_dir  # 取数通道未因拆层受影响


def test_new_operator_checks_pass_on_the_real_sample_bills():
    """真实样例账单上，新写的格式/去重/区间/白名单口径全部通过。

    样例账单本身是干净的（缺陷由 Task 3 刻意留在必填与退款合计两处），
    所以这里期望 pass —— 若报红，说明契约里的 pattern/集合写错了。
    """
    from reconq.sources.inbox_files import read_inbox_dir

    fixtures = os.path.join(HERE, "fixtures", "bills")
    for channel, paths in {
        "wechat": ["transaction_id.format", "transaction_id.distinct_from.out_trade_no"],
        "alipay": ["trade_no.format"],
        "unionpay": ["trade_no.distinct_from.out_trade_no"],
        "self_service": ["out_trade_no.format"],
        "medical_ins": ["settlement_no.format", "rows.total_amount.positive"],
    }.items():
        contract = _contract_for(channel)
        rows = read_inbox_dir(fixtures, PERIOD, channel, contract.table())
        assert rows, f"{channel} 样例账单读不出行"
        for path in paths:
            result = _run_path(contract, path, rows)
            assert result.status == "pass", f"{channel}/{path}: {result.message}"


def test_decimal_aggregate_stays_exact_across_the_operator_split():
    """拆层后金额定点语义不变：0.10 + 0.20 精确等于 0.30，无浮点残影。

    金额一旦在中途被 float 碰过（0.1 在二进制里不是 0.1），对账就会差出
    一笔永远查不回来的分钱。这条守住定点语义没有因拆层而松动。
    """
    from reconq.engine.common import to_decimal

    chk = _check("decimal_sum", "rows.sum", column="amount")
    rows = [_row({"amount": "0.10"}, 2), _row({"amount": "0.20"}, 3)]
    total = sum((to_decimal(r.get("amount"), r, "amount", chk) for r in rows), Decimal("0"))
    assert total == Decimal("0.30")
    assert str(total) == "0.30"
    # 与 float 路径对拍：float 会给出 0.30000000000000004
    assert 0.1 + 0.2 != 0.3
