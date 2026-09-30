"""契约模型解析测试。

模型语义对齐参考实现 ``github_ref/soda-core/soda-tests/src/helpers/yaml_parsing_helpers.py``
的 ``parse_contract()`` / ``parse_column_check()``：一份 YAML 进、
``ContractYaml`` 出，列级 check 挂在列上、dataset 级 check 挂在根下。
"""

from __future__ import annotations

import textwrap

import pytest

from reconq.contracts import CheckYaml, ContractYaml
from reconq.errors import ReconqError

GOOD_CONTRACT = textwrap.dedent(
    """
    name: 测试契约
    version: "1.0"
    channel: wechat
    dataset:
      name: 账单表
      tables:
        - name: bill
          location: inbox/2026-W36/wechat/
          columns:
            - name: out_trade_no
              data_type: string
              required: true
              checks:
                - name: 订单号唯一
                  path: [out_trade_no, unique]
                  type: unique
            - name: amount
              data_type: decimal
              checks:
                - name: 金额两位小数
                  path: [amount, scale]
                  type: decimal_scale
                  scale: 2
          checks:
            - name: 笔数不为零
              path: [rows, row_count]
              type: row_count
              min: 1
    checks:
      - name: 金额合计
        path: [rows, amount_sum]
        type: decimal_sum
        column: amount
        tolerance: "0.01"
    """
)


class TestParseSuccess:
    def test_parses_dataset_columns_and_checks(self) -> None:
        contract = ContractYaml.parse(GOOD_CONTRACT, source_path="t.yml")
        assert contract.name == "测试契约"
        assert contract.channel == "wechat"
        assert contract.dataset.name == "账单表"

        table = contract.table()
        assert table.name == "bill"
        assert [c.name for c in table.columns] == ["out_trade_no", "amount"]
        assert table.column("out_trade_no") is not None
        assert table.column("nope") is None

    def test_column_level_checks_attach_to_columns(self) -> None:
        contract = ContractYaml.parse(GOOD_CONTRACT)
        table = contract.table()
        unique_check = table.column("out_trade_no").checks[0]
        assert unique_check.type == "unique"
        assert unique_check.path == ["out_trade_no", "unique"]
        assert unique_check.name == "订单号唯一"

    def test_table_level_and_dataset_level_checks_are_separate(self) -> None:
        contract = ContractYaml.parse(GOOD_CONTRACT)
        table = contract.table()
        assert [c.path_str for c in table.checks] == ["rows.row_count"]
        assert [c.path_str for c in contract.checks] == ["rows.amount_sum"]

    def test_iter_checks_yields_all_in_declaration_order(self) -> None:
        contract = ContractYaml.parse(GOOD_CONTRACT)
        paths = [chk.path_str for chk, _ in contract.iter_checks()]
        # 表级 → 列级（按列顺序）→ 契约根级
        assert paths == [
            "rows.row_count",
            "out_trade_no.unique",
            "amount.scale",
            "rows.amount_sum",
        ]

    def test_extra_check_properties_are_folded_into_properties(self) -> None:
        """check 顶层写的 scale/tolerance 等统一并入 properties，算子只读一处。"""
        contract = ContractYaml.parse(GOOD_CONTRACT)
        amount_sum = contract.checks[0]
        assert amount_sum.properties["column"] == "amount"
        assert amount_sum.properties["tolerance"] == "0.01"
        scale_check = contract.table().column("amount").checks[0]
        assert scale_check.properties["scale"] == 2

    def test_explicit_properties_block_is_also_supported(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns:
                    - name: a
                  checks:
                    - path: [rows, x]
                      type: decimal_sum
                      properties:
                        column: a
                        tolerance: "0.00"
            """
        )
        contract = ContractYaml.parse(yaml_text)
        assert contract.table().checks[0].properties == {"column": "a", "tolerance": "0.00"}

    def test_dotted_path_shorthand_is_accepted(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns: [{name: a}]
            checks:
              - path: rows.amount_sum
                type: decimal_sum
            """
        )
        contract = ContractYaml.parse(yaml_text)
        assert contract.checks[0].path == ["rows", "amount_sum"]

    def test_display_name_falls_back_to_type_and_path(self) -> None:
        check = CheckYaml(path=["rows", "amount_sum"], type="decimal_sum")
        assert check.display_name == "decimal_sum:rows.amount_sum"


class TestParseErrorsAreExplicit:
    def test_syntax_error_reports_file_and_line(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse("dataset:\n  name: d\n  tables: [\n", source_path="bad.yml")
        err = excinfo.value
        assert err.code == "CONTRACT_SYNTAX_ERROR"
        assert err.yaml_path == "bad.yml"
        assert err.yaml_line is not None

    def test_missing_dataset_key_is_rejected(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse("name: 只有名字\n", source_path="no-dataset.yml")
        assert excinfo.value.code == "CONTRACT_MISSING_KEY"

    def test_empty_document_is_rejected(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse("", source_path="empty.yml")
        assert excinfo.value.code == "CONTRACT_MISSING_KEY"

    def test_non_mapping_document_is_rejected(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse("- 只是一个列表\n", source_path="list.yml")
        assert excinfo.value.code == "CONTRACT_TYPE_MISMATCH"

    def test_missing_required_key_is_rejected_with_line(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns:
                    - data_type: string
            """
        )
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse(yaml_text, source_path="no-col-name.yml")
        assert excinfo.value.code == "CONTRACT_TYPE_MISMATCH"
        assert "name" in excinfo.value.detail
        assert excinfo.value.yaml_line is not None

    def test_unknown_key_is_not_silently_accepted(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns:
                    - name: a
                      datatpye: string
            """
        )
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse(yaml_text, source_path="typo.yml")
        assert excinfo.value.code == "CONTRACT_TYPE_MISMATCH"

    def test_check_without_path_is_rejected(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns: [{name: a}]
            checks:
              - type: decimal_sum
            """
        )
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse(yaml_text, source_path="no-path.yml")
        assert excinfo.value.code == "CHECK_PATH_INVALID"

    def test_check_without_type_is_rejected(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns: [{name: a}]
            checks:
              - path: [rows, amount_sum]
            """
        )
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse(yaml_text, source_path="no-type.yml")
        assert excinfo.value.code == "CHECK_TYPE_MISSING"

    def test_illegal_check_path_segment_is_rejected(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns: [{name: a}]
            checks:
              - path: ["rows", "金额合计"]
                type: decimal_sum
            """
        )
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse(yaml_text, source_path="bad-path.yml")
        assert excinfo.value.code == "CHECK_PATH_INVALID"
        assert "金额合计" in excinfo.value.detail

    def test_empty_path_list_is_rejected(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns: [{name: a}]
            checks:
              - path: []
                type: decimal_sum
            """
        )
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse(yaml_text, source_path="empty-path.yml")
        assert excinfo.value.code == "CHECK_PATH_INVALID"

    def test_duplicate_check_path_is_rejected(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns: [{name: a}]
            checks:
              - path: [rows, amount_sum]
                type: decimal_sum
              - path: [rows, amount_sum]
                type: decimal_sum
            """
        )
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse(yaml_text, source_path="dup.yml")
        assert excinfo.value.code == "CONTRACT_TYPE_MISMATCH"
        assert "重复定义" in excinfo.value.detail

    def test_duplicate_column_names_are_rejected(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns:
                    - name: a
                    - name: a
            """
        )
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse(yaml_text, source_path="dup-col.yml")
        assert "列名重复定义" in excinfo.value.detail

    def test_columns_must_be_a_list(self) -> None:
        yaml_text = textwrap.dedent(
            """
            dataset:
              name: d
              tables:
                - name: t
                  columns:
                    a: string
            """
        )
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse(yaml_text, source_path="cols-map.yml")
        assert excinfo.value.code == "CONTRACT_TYPE_MISMATCH"
        assert "columns 必须是列表" in excinfo.value.detail

    def test_missing_tables_key_is_rejected(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.parse("dataset:\n  name: d\n", source_path="no-tables.yml")
        assert excinfo.value.code == "CONTRACT_MISSING_KEY"
        assert "tables" in excinfo.value.detail

    def test_load_missing_file_reports_path(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            ContractYaml.load("contracts/does-not-exist.yml")
        assert excinfo.value.code == "CONTRACT_MISSING_KEY"


class TestChannelContracts:
    """六路渠道契约均须能解析，且每路至少有 dataset 级与列级口径。"""

    CHANNELS = [
        "wechat",
        "alipay",
        "unionpay",
        "his",
        "medical_ins",
        "self_service",
    ]

    @pytest.mark.parametrize("channel", CHANNELS)
    def test_channel_contract_parses(self, channel: str) -> None:
        from reconq.contracts import load_contract

        contract = load_contract(f"contracts/{channel}.yml")
        assert contract.channel == channel
        assert contract.dataset.name
        assert contract.table().columns
        assert list(contract.iter_checks()), "每路渠道至少要有一条对账口径"

    @pytest.mark.parametrize("channel", CHANNELS)
    def test_channel_contract_has_required_and_enum_checks(self, channel: str) -> None:
        from reconq.contracts import load_contract

        contract = load_contract(f"contracts/{channel}.yml")
        table = contract.table()
        required_cols = [c.name for c in table.columns if c.required]
        assert required_cols, f"{channel} 至少要声明一个必填列"
        types = {chk.type for chk, _ in contract.iter_checks()}
        assert "cross_period" in types, f"{channel} 缺少跨日切分口径"
        assert "row_count" in types, f"{channel} 缺少笔数口径"
        assert "decimal_sum" in types, f"{channel} 缺少金额合计口径"

    @pytest.mark.parametrize("channel", ["wechat", "alipay", "unionpay", "self_service"])
    def test_amount_column_declares_decimal_scale(self, channel: str) -> None:
        """金额列必须声明精度口径 —— 金额一律 Decimal，浮点误差不许进对账。"""
        from reconq.contracts import load_contract

        contract = load_contract(f"contracts/{channel}.yml")
        amount_col = contract.table().column("amount")
        assert amount_col is not None and amount_col.data_type == "decimal"
        scale_checks = [c for c in amount_col.checks if c.type == "decimal_scale"]
        assert scale_checks, f"{channel} 的 amount 列未声明精度口径"
        assert scale_checks[0].properties["scale"] == 2
