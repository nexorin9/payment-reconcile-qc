"""check 路径选择器测试。

三类表达式的语义对齐参考实现 ``github_ref/soda-core/soda_core/cli/cli.py:212-245``
``--check-filter`` 帮助文本：多字段 AND、同字段 OR、通配匹配；
多个 ``--check-filter`` 之间为并集（源产品 ``CheckSelector.parse_all()``，
调用点见 ``cli.py:270``）。
"""

from __future__ import annotations

import textwrap

import pytest

from reconq.contracts import CheckSelector, ContractYaml, filter_checks, load_contract
from reconq.contracts.selector import CheckSelectorParseError

CONTRACT = textwrap.dedent(
    """
    name: 选择器测试契约
    dataset:
      name: 账单表
      tables:
        - name: bill
          columns:
            - name: out_trade_no
              checks:
                - name: 订单号唯一
                  path: [out_trade_no, unique]
                  type: unique
            - name: amount
              checks:
                - name: 金额两位小数
                  path: [amount, scale]
                  type: decimal_scale
                  scale: 2
            - name: trade_state
              checks:
                - name: 状态枚举
                  path: [trade_state, in_enum]
                  type: in_enum
                  values: [SUCCESS, REFUND]
          checks:
            - name: 笔数不为零
              path: [rows, row_count]
              type: row_count
              min: 1
    checks:
      - name: 金额合计
        path: [rows, amount_sum]
        type: decimal_sum
        qualifier: 关键
      - name: 跨日切分
        path: [rows, success_time, in_period]
        type: cross_period
        qualifier: 关键
    """
)


@pytest.fixture()
def contract() -> ContractYaml:
    return ContractYaml.parse(CONTRACT, source_path="sel.yml")


def paths(items) -> list[str]:
    return [check.path_str for check, _ in items]


class TestParseAll:
    def test_empty_input_means_no_filter(self) -> None:
        assert CheckSelector.parse_all(None) == []
        assert CheckSelector.parse_all([]) == []

    def test_returns_one_selector_per_expression(self) -> None:
        selectors = CheckSelector.parse_all(["type=decimal_sum", "type=row_count"])
        assert len(selectors) == 2

    def test_invalid_expression_raises_with_error_code(self) -> None:
        # 裸口径名（``row_count`` / ``not_empty`` / ``transaction_id.*``）自
        # Task 19 起**合法** —— 见下方 test_bare_name_expands_to_path_and_type。
        # 所以这里要挑一个真正非法的：不支持的键。
        with pytest.raises(CheckSelectorParseError) as excinfo:
            CheckSelector.parse_all(["nosuchkey=x"])
        assert excinfo.value.code == "CHECK_SELECTOR_INVALID"

    def test_bare_name_expands_to_path_and_type(self) -> None:
        """裸口径名同时按 path 与 type 命中，两种写法都能直接跑。

        财务科在工单里抄的往往是契约里那条 path 或那个算子名，让他先翻译
        成 ``path=`` 才是没有道理的。注意是**两条**选择器而不是
        ``path=x,type=x`` 一条：同一选择器内多键是 AND，那样永远匹配不到。
        """
        selectors = CheckSelector.parse_all(["row_count"])
        assert len(selectors) == 2
        assert {tuple(sorted(s.conditions)) for s in selectors} == {
            ("path",), ("type",),
        }
        # 带 = 的表达式仍只产出一条
        assert len(CheckSelector.parse_all(["type=not_empty"])) == 1

    def test_parse_stays_strict_about_missing_equals(self) -> None:
        """``parse``（单条）保持严格；宽松只在 ``parse_all`` 这条 CLI 面上。"""
        with pytest.raises(CheckSelectorParseError):
            CheckSelector.parse("row_count")

    def test_parse_error_subclasses_reconq_error(self) -> None:
        from reconq.errors import ReconqError

        assert issubclass(CheckSelectorParseError, ReconqError)


class TestParseErrors:
    def test_missing_equals(self) -> None:
        with pytest.raises(CheckSelectorParseError) as excinfo:
            CheckSelector.parse("type")
        assert "key=value" in excinfo.value.detail

    def test_empty_key(self) -> None:
        with pytest.raises(CheckSelectorParseError):
            CheckSelector.parse("=decimal_sum")

    def test_empty_value(self) -> None:
        with pytest.raises(CheckSelectorParseError):
            CheckSelector.parse("type=")

    def test_empty_expression(self) -> None:
        with pytest.raises(CheckSelectorParseError):
            CheckSelector.parse("   ")

    def test_unknown_key_is_rejected_not_ignored(self) -> None:
        """写错键名的人以为筛过了，其实一条都没筛 —— 必须报错。"""
        with pytest.raises(CheckSelectorParseError) as excinfo:
            CheckSelector.parse("typo=decimal_sum")
        assert "typo" in excinfo.value.detail
        assert "type" in excinfo.value.detail

    def test_whitespace_is_tolerated(self) -> None:
        selector = CheckSelector.parse(" type = decimal_sum , column = amount ")
        assert selector.conditions == {"type": ["decimal_sum"], "column": ["amount"]}


class TestAndAcrossFields:
    def test_two_fields_are_anded(self, contract: ContractYaml) -> None:
        """多字段 AND：类型与列名同时满足才命中。"""
        selectors = CheckSelector.parse_all(["type=decimal_scale,column=amount"])
        assert paths(filter_checks(contract, selectors)) == ["amount.scale"]

    def test_and_with_mismatched_column_yields_nothing(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["type=decimal_scale,column=trade_state"])
        assert filter_checks(contract, selectors) == []

    def test_three_fields_anded(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["type=in_enum,column=trade_state,name=状态枚举"])
        assert paths(filter_checks(contract, selectors)) == ["trade_state.in_enum"]


class TestOrWithinSameField:
    def test_same_key_repeated_is_or(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["type=decimal_sum,type=cross_period"])
        assert paths(filter_checks(contract, selectors)) == [
            "rows.amount_sum",
            "rows.success_time.in_period",
        ]

    def test_or_with_column_field(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["column=amount,column=trade_state"])
        assert paths(filter_checks(contract, selectors)) == [
            "amount.scale",
            "trade_state.in_enum",
        ]


class TestWildcards:
    def test_star_prefix(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["column=am*"])
        assert paths(filter_checks(contract, selectors)) == ["amount.scale"]

    def test_question_mark_single_char(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["type=d?cimal_sum"])
        assert paths(filter_checks(contract, selectors)) == ["rows.amount_sum"]

    def test_star_suffix(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["type=*_sum"])
        assert paths(filter_checks(contract, selectors)) == ["rows.amount_sum"]

    def test_wildcard_in_or_group(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["type=decimal_*,type=cross_*"])
        assert paths(filter_checks(contract, selectors)) == [
            "amount.scale",
            "rows.amount_sum",
            "rows.success_time.in_period",
        ]


class TestOtherKeys:
    def test_filter_by_path(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["path=rows.row_count"])
        assert paths(filter_checks(contract, selectors)) == ["rows.row_count"]

    def test_filter_by_name(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["name=跨日切分"])
        assert paths(filter_checks(contract, selectors)) == ["rows.success_time.in_period"]

    def test_filter_by_qualifier(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["qualifier=关键"])
        assert paths(filter_checks(contract, selectors)) == [
            "rows.amount_sum",
            "rows.success_time.in_period",
        ]

    def test_filter_by_attribute(self, contract: ContractYaml) -> None:
        """源产品称 attributes，本产品落到 properties，用 attributes.<key> 前缀访问。"""
        selectors = CheckSelector.parse_all(["attributes.scale=2"])
        assert paths(filter_checks(contract, selectors)) == ["amount.scale"]

    def test_attribute_absent_means_no_match(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["attributes.scale=3"])
        assert filter_checks(contract, selectors) == []

    def test_dataset_level_check_has_no_column(self, contract: ContractYaml) -> None:
        """dataset 级 check 的列名为 None，按 column 筛选时不应误命中。"""
        selectors = CheckSelector.parse_all(["column=amount"])
        assert all(col == "amount" for _, col in filter_checks(contract, selectors))


class TestFilterChecksOrdering:
    def test_no_selector_returns_everything_in_declaration_order(
        self, contract: ContractYaml
    ) -> None:
        assert paths(filter_checks(contract, None)) == [
            "rows.row_count",
            "out_trade_no.unique",
            "amount.scale",
            "trade_state.in_enum",
            "rows.amount_sum",
            "rows.success_time.in_period",
        ]
        assert paths(filter_checks(contract, [])) == paths(filter_checks(contract, None))

    def test_selected_subset_keeps_original_order(self, contract: ContractYaml) -> None:
        """先声明的 check 永远排在后面声明的之前，不按选择器里的出现顺序排。"""
        selectors = CheckSelector.parse_all(["type=cross_period,type=unique"])
        assert paths(filter_checks(contract, selectors)) == [
            "out_trade_no.unique",
            "rows.success_time.in_period",
        ]

    def test_multiple_selectors_are_unioned(self, contract: ContractYaml) -> None:
        selectors = CheckSelector.parse_all(["type=unique", "type=decimal_sum"])
        assert paths(filter_checks(contract, selectors)) == [
            "out_trade_no.unique",
            "rows.amount_sum",
        ]


class TestChannelContractFiltering:
    def test_wechat_amount_checks_selectable_by_column(self) -> None:
        contract = load_contract("contracts/wechat.yml")
        selectors = CheckSelector.parse_all(["column=amount"])
        assert paths(filter_checks(contract, selectors)) == ["amount.scale", "amount.min"]

    def test_medical_ins_equation_checks_are_targetable(self) -> None:
        """医保两条勾稽等式可被单独点名 —— 医保办只想看等式时不必重跑全量。"""
        contract = load_contract("contracts/medical_ins.yml")
        selectors = CheckSelector.parse_all(["type=column_equality"])
        selected = paths(filter_checks(contract, selectors))
        assert "rows.total_amount.equation" in selected
        assert "rows.personal_burden.equation" in selected
