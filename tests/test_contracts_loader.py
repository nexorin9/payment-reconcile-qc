"""契约加载与变量注入测试。

变量值解析顺序（int → float → str）直接对齐参考实现
``github_ref/soda-core/soda_core/cli/cli.py:296-335`` 的 ``_parse_variables()``
与 ``_parse_variable_value()``。
"""

from __future__ import annotations

import textwrap

import pytest

from reconq.contracts import (
    apply_variables,
    collect_variable_placeholders,
    load_contract,
    parse_variable_assignments,
    parse_variable_value,
)
from reconq.errors import ReconqError

TEMPLATED_CONTRACT = textwrap.dedent(
    """
    name: 带变量的契约
    variables:
      period: "2026-W36"
      max_amount: 1000
    dataset:
      name: d
      tables:
        - name: t
          location: inbox/${period}/t/
          columns:
            - name: a
              checks:
                - name: 金额区间
                  path: [a, between]
                  type: between
                  min: 0
                  max: ${max_amount}
    checks:
      - name: 账期
        path: [rows, period]
        type: column_equality
        compare_column: period_code
        compare_value: ${period}
    """
)


class TestVariableValueParsing:
    """int → float → str 的回退顺序，源实现见 cli.py:318-335。"""

    @pytest.mark.parametrize(
        "raw, expected, expected_type",
        [
            ("2026", 2026, int),
            ("42", 42, int),
            ("-7", -7, int),
            ("0.01", 0.01, float),
            ("1e3", 1000.0, float),
            ("3.14", 3.14, float),
            ("2026-W36", "2026-W36", str),
            ("Asia/Shanghai", "Asia/Shanghai", str),
            ("", "", str),
        ],
    )
    def test_parse_variable_value_types(self, raw: str, expected, expected_type) -> None:
        value = parse_variable_value(raw)
        assert value == expected
        assert type(value) is expected_type

    def test_float_value_does_not_collapse_to_int(self) -> None:
        """0.01 必须保持 float，不能被 int() 吞成 0 —— 容差口径靠它。"""
        assert parse_variable_value("0.01") != 0
        assert repr(parse_variable_value("0.01")) == "0.01"


class TestVariableAssignments:
    def test_parses_key_value_pairs(self) -> None:
        result = parse_variable_assignments(["period=2026-W37", "tolerance=0.01", "flag=true"])
        assert result == {"period": "2026-W37", "tolerance": 0.01, "flag": "true"}

    def test_none_and_empty_give_empty_dict(self) -> None:
        assert parse_variable_assignments(None) == {}
        assert parse_variable_assignments([]) == {}

    def test_value_may_contain_equals_sign(self) -> None:
        assert parse_variable_assignments(["expr=a=b"]) == {"expr": "a=b"}

    def test_missing_equals_is_rejected(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            parse_variable_assignments(["period"])
        assert excinfo.value.code == "VARIABLE_BAD_FORMAT"
        assert "KEY=VALUE" in excinfo.value.detail

    def test_empty_key_is_rejected(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            parse_variable_assignments(["=2026-W36"])
        assert excinfo.value.code == "VARIABLE_BAD_FORMAT"

    def test_empty_value_is_rejected(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            parse_variable_assignments(["period="])
        assert excinfo.value.code == "VARIABLE_BAD_FORMAT"


class TestPlaceholderHandling:
    def test_collects_placeholder_names(self) -> None:
        text = "location: inbox/${period}/x/\nmax: ${max_amount}\nagain: ${period}\n"
        assert collect_variable_placeholders(text) == {"period", "max_amount"}

    def test_apply_variables_replaces_text(self) -> None:
        out = apply_variables("p=${period}\nn=${n}\n", {"period": "2026-W37", "n": 3})
        assert out == "p=2026-W37\nn=3\n"

    def test_apply_variables_preserves_number_type(self) -> None:
        """int 变量替换后仍是裸数字，YAML 解析出 int 而非字符串。"""
        import yaml

        out = apply_variables("max: ${max_amount}\n", {"max_amount": 1000})
        assert yaml.safe_load(out)["max"] == 1000
        assert isinstance(yaml.safe_load(out)["max"], int)

    def test_missing_variable_names_the_variable(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            apply_variables("p=${period}\n", {}, source_path="c.yml")
        err = excinfo.value
        assert err.code == "VARIABLE_MISSING"
        assert "period" in err.detail
        assert err.yaml_path == "c.yml"


class TestLoadContract:
    def test_declared_default_used_when_no_override(self, tmp_path) -> None:
        path = tmp_path / "t.yml"
        path.write_text(TEMPLATED_CONTRACT, encoding="utf-8")
        contract = load_contract(str(path))
        assert contract.table().location == "inbox/2026-W36/t/"
        # max_amount 声明为 int 1000，替换后仍是 int
        assert contract.table().column("a").checks[0].properties["max"] == 1000

    def test_explicit_variable_overrides_declared_default(self, tmp_path) -> None:
        path = tmp_path / "t.yml"
        path.write_text(TEMPLATED_CONTRACT, encoding="utf-8")
        contract = load_contract(str(path), {"period": "2026-W37"})
        assert contract.table().location == "inbox/2026-W37/t/"
        equality = contract.checks[0]
        assert equality.properties["compare_value"] == "2026-W37"

    def test_extra_variables_merge_with_declared(self, tmp_path) -> None:
        path = tmp_path / "t.yml"
        path.write_text(TEMPLATED_CONTRACT, encoding="utf-8")
        contract = load_contract(str(path), extra_variables={"max_amount": 5000})
        assert contract.table().column("a").checks[0].properties["max"] == 5000

    def test_missing_variable_reports_name_and_path(self, tmp_path) -> None:
        text = TEMPLATED_CONTRACT.replace('max: ${max_amount}', "max: 1").replace(
            'variables:\n  period: "2026-W36"\n  max_amount: 1000\n', ""
        )
        path = tmp_path / "t.yml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ReconqError) as excinfo:
            load_contract(str(path))
        err = excinfo.value
        assert err.code == "VARIABLE_MISSING"
        assert "period" in err.detail
        assert err.yaml_path == str(path)

    def test_unreadable_file_reports_path(self) -> None:
        with pytest.raises(ReconqError) as excinfo:
            load_contract("contracts/no-such-file.yml")
        assert excinfo.value.code == "CONTRACT_MISSING_KEY"

    def test_bad_variables_section_type_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "t.yml"
        path.write_text(
            TEMPLATED_CONTRACT.replace(
                'variables:\n  period: "2026-W36"\n  max_amount: 1000\n', "variables: 不是一个映射\n"
            ),
            encoding="utf-8",
        )
        with pytest.raises(ReconqError) as excinfo:
            load_contract(str(path))
        assert excinfo.value.code == "CONTRACT_TYPE_MISMATCH"

    @pytest.mark.parametrize(
        "channel", ["wechat", "alipay", "unionpay", "his", "medical_ins", "self_service"]
    )
    def test_channel_contract_location_follows_period_variable(self, channel: str) -> None:
        """location 里的 ${period} 由变量注入，扫不同账期指向不同目录。"""
        contract = load_contract(f"contracts/{channel}.yml", {"period": "2026-W40"})
        location = contract.table().location or ""
        if location.startswith("inbox/"):
            assert location == f"inbox/2026-W40/{channel}/"
