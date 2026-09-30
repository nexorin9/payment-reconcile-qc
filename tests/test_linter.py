"""契约静态校验器的测试（Task 16）。

五类规定断言：

1. 三类坏契约各自报出预期 code 与**YAML 行号**
2. 好契约零 ERROR
3. WARN 不阻断（只记录）
4. 覆盖度类 WARN 按表聚合，不逐列刷屏
5. 闸门接进 ``reconq verify`` 启动路径：ERROR 时不进入取数阶段
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from reconq.contracts.linter import (
    errors_of,
    has_errors,
    lint_contract,
    lint_report,
    lint_source,
    lint_yaml,
    warnings_of,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "contracts"
CONTRACT_DIR = PROJECT_ROOT / "contracts"

BAD_DANGLING = FIXTURE_DIR / "dangling_column_ref.yml"
BAD_DUP = FIXTURE_DIR / "dup_check_path.yml"
BAD_VARIABLE = FIXTURE_DIR / "undeclared_variable.yml"
GOOD = FIXTURE_DIR / "good.yml"


def _codes(issues, *, error=True):
    pool = errors_of(issues) if error else warnings_of(issues)
    return [i.code for i in pool]


# —— 1. 三类坏契约各自的 code 与行号 ————————————————————


def test_dangling_column_ref_reports_code_and_line():
    """悬空列引用：报 DANGLING_COLUMN_REF，行号指向出问题的那条 check。"""
    issues = lint_source(str(BAD_DANGLING))
    errs = errors_of(issues)
    assert [i.code for i in errs] == ["DANGLING_COLUMN_REF", "DANGLING_COLUMN_REF"]
    for issue in errs:
        assert issue.yaml_line, "ERROR 必须带 YAML 行号"
        assert issue.path.startswith("checks[")


def test_dangling_column_ref_points_at_the_offending_check_line():
    """行号要落在真正写错的那条 check 上，不是笼统指向整份契约。

    错的是 ``column: amt``（拼错的列名）——它在文件里的行号必须与
    实际文本逐行对上，财务科在编辑器里一跳才落得到地方。
    """
    lines = BAD_DANGLING.read_text(encoding="utf-8").splitlines()
    issues = errors_of(lint_source(str(BAD_DANGLING)))
    first = issues[0]
    assert "column: amt" in lines[first.yaml_line - 1]
    second = issues[1]
    assert "where: {trade_stauts: SUCCESS}" in lines[second.yaml_line - 1]


def test_dangling_column_ref_message_names_the_column_and_the_declared_set():
    """提示要同时点名「错的那个列名」和「实际声明了哪些列」。

    只说「列不存在」而不列出本表声明了哪几列，财务科还是得自己回去数一遍
    到底哪个字打错了。
    """
    issues = errors_of(lint_source(str(BAD_DANGLING)))
    joined = " ".join(i.message + " " + i.hint for i in issues)
    assert "amt" in joined
    assert "trade_stauts" in joined
    assert "amount" in joined and "out_trade_no" in joined


def test_dup_check_path_reports_code_and_line():
    """同一 path 写两遍：报 DUP_CHECK_PATH，行号指向**第二条**（重复的那条）。"""
    issues = errors_of(lint_source(str(BAD_DUP)))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "DUP_CHECK_PATH"
    assert issue.yaml_line
    lines = BAD_DUP.read_text(encoding="utf-8").splitlines()
    assert "退款金额合计（重复定义）" in lines[issue.yaml_line - 1]
    # 提示里要指名与第几条重复
    assert "checks[0]" in issue.message and "checks[1]" in issue.message


def test_dup_check_path_is_detected_before_the_model_would_reject_it():
    """重复 path 在领域模型解析阶段就会抛错，lint 必须在**解析之前**报出来。

    这是 lint 走原始 YAML 文本而不走 ``ContractYaml.parse`` 的原因：
    走 parse 就永远拿不到带行号的 ``DUP_CHECK_PATH``。
    """
    text = BAD_DUP.read_text(encoding="utf-8")
    assert any(i.code == "DUP_CHECK_PATH" for i in errors_of(lint_yaml(text)))


def test_undeclared_variable_reports_code_and_line():
    """引用了没声明也没注入的变量：报 VARIABLE_UNDECLARED，行号指向占位所在行。"""
    issues = errors_of(lint_source(str(BAD_VARIABLE)))
    var_issues = [i for i in issues if i.code == "VARIABLE_UNDECLARED"]
    assert len(var_issues) == 1
    issue = var_issues[0]
    assert "hospital_code" in issue.message
    lines = BAD_VARIABLE.read_text(encoding="utf-8").splitlines()
    assert "${hospital_code}" in lines[issue.yaml_line - 1]


def test_undeclared_variable_is_accepted_once_provided_on_the_command_line():
    """同一个变量用 --set 注入后不再报 —— 变量可以从命令行来，不必写死在契约里。"""
    text = BAD_VARIABLE.read_text(encoding="utf-8")
    assert any(
        i.code == "VARIABLE_UNDECLARED" for i in errors_of(lint_yaml(text))
    )
    injected = lint_yaml(text, provided_variables={"hospital_code": "HOSP-A"})
    assert "VARIABLE_UNDECLARED" not in [i.code for i in errors_of(injected)]


# —— 2. 好契约零 ERROR ————————————————————————————————


def test_good_contract_has_zero_errors():
    assert errors_of(lint_source(str(GOOD))) == []


def test_good_contract_has_zero_warnings_too():
    """好契约连 WARN 都不该有：新写一份契约应该照着它抄。"""
    assert lint_source(str(GOOD)) == []


def test_all_six_real_contracts_pass_static_validation():
    """六路上线契约零 ERROR。

    这是本任务最要紧的一条：静态校验是为了在扫描前挡住写错的契约，
    要是它把现行六份契约判成红的，闸门就没人敢开。

    下划线开头的文件（``_scoring.yml`` 打分配置）**不是渠道契约**，不参与
    计数也不参与 lint：它压根没有 dataset / checks 段，按渠道契约去校验
    只会报一堆「缺 dataset」的无意义错误。与 ``cli._discover_channels`` 同一
    口径 —— 两处若漂移，这里就会在某天多出一份或少掉一份。
    """
    files = sorted(p for p in CONTRACT_DIR.glob("*.yml") if not p.name.startswith("_"))
    assert len(files) == 6, f"应只有六路契约，实际 {len(files)} 份：{[p.name for p in files]}"
    for path in files:
        issues = lint_source(str(path))
        assert not has_errors(issues), (
            f"{path.name} 出现 ERROR：\n" + lint_report(issues, source_path=str(path))
        )


# —— 3. WARN 不阻断 ——————————————————————————————————


def test_warn_only_contract_reports_no_errors():
    """只有覆盖度 WARN 的契约不得被判红。"""
    text = GOOD.read_text(encoding="utf-8").replace(
        """            - name: 金额两位小数
              path: [amount, scale]
              type: decimal_scale
              scale: 2""",
        "",
    )
    issues = lint_yaml(text)
    assert errors_of(issues) == []
    assert "AMOUNT_COLUMN_NO_SCALE_CHECK" in _codes(issues, error=False)


def test_required_column_without_not_empty_is_a_warning_not_an_error():
    """必填列缺 not_empty 只是覆盖度问题，不该阻断扫描。"""
    issues = lint_yaml(GOOD.read_text(encoding="utf-8").replace(
        """            - name: 金额必填
              path: [amount, not_empty]
              type: not_empty
""",
        "",
    ))
    assert errors_of(issues) == []
    assert "REQUIRED_COLUMN_NO_NOT_EMPTY" in _codes(issues, error=False)


def test_error_and_warning_are_separated_by_severity():
    """BAD_DUP 同时有 ERROR 与 WARN，两类不得混在一个池子里。"""
    issues = lint_source(str(BAD_DUP))
    assert has_errors(issues)
    assert errors_of(issues) and warnings_of(issues)
    assert all(i.is_error for i in errors_of(issues))
    assert not any(i.is_error for i in warnings_of(issues))
    # 两池无交集
    assert not set(id(i) for i in errors_of(issues)) & set(id(i) for i in warnings_of(issues))


def test_lint_report_states_both_counts():
    text = lint_report(lint_source(str(BAD_DUP)))
    assert "1 个 ERROR" in text
    assert "2 个 WARN" in text


# —— 4. 覆盖度 WARN 按表聚合 ————————————————————————————


def test_coverage_warnings_are_aggregated_per_table_not_per_column():
    """一份 10 列的契约缺兜底口径时，报一条而不是十条。

    逐列刷屏的结果是没人看；合成一条列出缺哪几列才读得下去。
    """
    lines = ["name: 聚合样例", "channel: agg", "dataset:", "  name: t", "  tables:"]
    lines.append("    - name: t")
    lines.append("      columns:")
    for i in range(6):
        lines += [
            f"        - name: c{i}",
            "          data_type: string",
            "          required: true",
        ]
    issues = lint_yaml("\n".join(lines) + "\n")
    not_empty = [i for i in warnings_of(issues) if i.code == "REQUIRED_COLUMN_NO_NOT_EMPTY"]
    assert len(not_empty) == 1
    for i in range(6):
        assert f"c{i}" in not_empty[0].message
    # 逐列报的话这里应该是 6 条
    assert not [i for i in issues if i.code == "REQUIRED_COLUMN_NO_NOT_EMPTY"][1:]


# —— 5. 闸门接进 verify 启动路径 ————————————————————————


def _run_cli(*argv: str, cwd: Path = PROJECT_ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "reconq", *argv],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def test_verify_lint_mode_reports_zero_errors_for_real_contracts():
    """``reconq verify --lint`` 对现行六份契约退出码 0。"""
    result = _run_cli("verify", "--period", "2026-W36", "--lint")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 个 ERROR" in result.stdout


def test_verify_lint_mode_json_output_is_machine_readable():
    """``--lint --json`` 交出结构化结果，供信息科贴工单。"""
    import json

    result = _run_cli("verify", "--period", "2026-W36", "--lint", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["total_errors"] == 0
    assert len(payload["linted_channels"]) == 6
    for rep in payload["linted_channels"]:
        assert rep["errors"] == 0
        assert isinstance(rep["issues"], list)


def test_verify_lint_mode_lists_yaml_line_for_bad_contract(tmp_path):
    """坏契约经 --lint 报出 code 与行号，退出码非零。"""
    import json

    (tmp_path / "wechat.yml").write_text(
        BAD_DANGLING.read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = _run_cli(
        "verify", "--period", "2026-W36", "--contracts", str(tmp_path),
        "--channel", "wechat", "--lint", "--json",
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["total_errors"] == 2
    codes = {i["code"] for i in payload["linted_channels"][0]["issues"] if i["severity"] == "error"}
    assert codes == {"DANGLING_COLUMN_REF"}
    for issue in payload["linted_channels"][0]["issues"]:
        if issue["severity"] == "error":
            assert issue["yaml_line"]


def test_verify_startup_gate_blocks_before_fetching(tmp_path):
    """ERROR 存在时不进入取数阶段：终端只出 lint 清单，不出任何口径结果。"""
    (tmp_path / "wechat.yml").write_text(
        BAD_DANGLING.read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = _run_cli(
        "verify", "--period", "2026-W36", "--source", "tests/fixtures/bills",
        "--contracts", str(tmp_path), "--channel", "wechat",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "DANGLING_COLUMN_REF" in combined
    assert "已在取数前拦下" in combined
    # 关键：一条口径结果都没跑出来（没有口径小计、没有账期汇总表）
    assert "小计：" not in combined
    assert "账期 2026-W36 汇总" not in combined


def test_verify_startup_gate_passes_real_contracts_through(tmp_path):
    """闸门放行时主干照旧跑通，不因接了 lint 而改变原有输出。"""
    src = tmp_path / "wechat.yml"
    src.write_text((CONTRACT_DIR / "wechat.yml").read_text(encoding="utf-8"), encoding="utf-8")
    result = _run_cli(
        "verify", "--period", "2026-W36", "--source", "tests/fixtures/bills",
        "--contracts", str(tmp_path), "--channel", "wechat",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "小计：15 条口径，3 条未通过" in result.stdout
    assert "5714.69" in result.stdout


# —— 规则本身的边界 ——————————————————————————————————


def test_check_path_root_unknown_is_an_error():
    """dataset 级口径的 path 首段既不是 rows 也不是本表列 → ERROR。"""
    text = """
name: 落点不存在
dataset:
  name: t
  tables:
    - name: t
      columns:
        - name: amount
          data_type: decimal
          required: true
          checks:
            - name: 两位小数
              path: [amount, scale]
              type: decimal_scale
              scale: 2
checks:
  - name: 落点不存在的口径
    path: [no_such_root, something]
    type: row_count
"""
    issues = errors_of(lint_yaml(text))
    assert "CHECK_PATH_ROOT_UNKNOWN" in [i.code for i in issues]
    assert "no_such_root" in issues[0].message


def test_rows_root_is_accepted_for_dataset_level_checks():
    """``rows`` 是整表挂载点，不该被判成落点不存在。"""
    issues = lint_yaml((GOOD).read_text(encoding="utf-8"))
    assert "CHECK_PATH_ROOT_UNKNOWN" not in [i.code for i in issues]


def test_column_level_check_on_wrong_column_is_an_error():
    """列级口径的 path 首段与所挂列不符 → ERROR（口径会作用到错误的列上）。"""
    text = """
name: 挂错列
dataset:
  name: t
  tables:
    - name: t
      columns:
        - name: amount
          data_type: decimal
          required: true
          checks:
            - name: 两位小数
              path: [amount, scale]
              type: decimal_scale
              scale: 2
        - name: other_col
          data_type: string
          required: true
          checks:
            - name: 挂错了列
              path: [amount, not_empty]
              type: not_empty
"""
    issues = errors_of(lint_yaml(text))
    assert "CHECK_PATH_COLUMN_MISMATCH" in [i.code for i in issues]
    assert "other_col" in issues[0].message


def test_column_ref_props_are_all_checked():
    """所有指名列的属性都要查，不能只查 column 一个。"""
    for prop in ("pair_column", "order_amount_column", "other", "left", "compare_column"):
        text = f"""
name: 悬空 {prop}
dataset:
  name: t
  tables:
    - name: t
      columns:
        - name: amount
          data_type: decimal
          required: true
          checks:
            - name: 两位小数
              path: [amount, scale]
              type: decimal_scale
              scale: 2
      checks:
        - name: 笔数不为零
          path: [rows, row_count]
          type: row_count
          min: 1
checks:
  - name: 引用了不存在的列
    path: [rows, x_{prop}]
    type: decimal_sum
    {prop}: no_such_column
"""
        issues = errors_of(lint_yaml(text))
        assert "DANGLING_COLUMN_REF" in [i.code for i in issues], f"{prop} 未被检查"
        assert "no_such_column" in " ".join(i.message for i in issues)


def test_in_enum_without_values_is_an_error():
    """枚举口径的取值只能来自渠道规范，空清单会让口径无意义。"""
    text = """
name: 空枚举
dataset:
  name: t
  tables:
    - name: t
      columns:
        - name: state
          data_type: string
          required: true
          checks:
            - name: 枚举
              path: [state, in_enum]
              type: in_enum
"""
    issues = errors_of(lint_yaml(text))
    assert "ENUM_VALUES_MISSING" in [i.code for i in issues]


def test_in_enum_on_non_string_column_is_a_warning():
    """枚举挂在非字符串列上多半配错了列，但只是提醒，不阻断。"""
    text = """
name: 枚举挂错类型
dataset:
  name: t
  tables:
    - name: t
      columns:
        - name: amount
          data_type: decimal
          required: true
          checks:
            - name: 枚举挂在金额列上
              path: [amount, in_enum]
              type: in_enum
              values: [1, 2]
"""
    issues = lint_yaml(text)
    assert errors_of(issues) == []
    assert "ENUM_ON_NON_STRING" in [i.code for i in warnings_of(issues)]


def test_unimplemented_operator_is_an_error():
    """声明了未实现算子的口径不得放行 —— 它既跑不出结果也不该被跳过。"""
    text = """
name: 未实现算子
dataset:
  name: t
  tables:
    - name: t
      columns:
        - name: amount
          data_type: decimal
          required: true
          checks:
            - name: 两位小数
              path: [amount, scale]
              type: decimal_scale
              scale: 2
      checks:
        - name: 笔数不为零
          path: [rows, row_count]
          type: row_count
          min: 1
checks:
  - name: 尚未实现的算子
    path: [rows, z_score]
    type: z_score_check
"""
    issues = errors_of(lint_yaml(text))
    codes = [i.code for i in issues]
    assert "CHECK_OPERATOR_UNSUPPORTED" in codes
    hit = next(i for i in issues if i.code == "CHECK_OPERATOR_UNSUPPORTED")
    assert "decimal_scale" in hit.message, "提示里要列出已实现的算子供替换"


def test_declared_but_unused_variable_is_a_warning():
    """复制模板留下的僵尸变量是 WARN：改它不会生效，但也不该阻断。"""
    text = """
name: 僵尸变量
variables:
  period: "2026-W36"
  orphan: "没人用"
dataset:
  name: t
  tables:
    - name: t
      location: inbox/${period}/x/
      columns:
        - name: amount
          data_type: decimal
          required: true
          checks:
            - name: 两位小数
              path: [amount, scale]
              type: decimal_scale
              scale: 2
"""
    issues = lint_yaml(text)
    assert errors_of(issues) == []
    assert "VARIABLE_UNUSED" in [i.code for i in warnings_of(issues)]


def test_variable_used_as_variable_attribute_is_not_reported_unused():
    """``period_variable: period`` 算用到了变量，不该被当成僵尸。"""
    issues = lint_yaml((CONTRACT_DIR / "wechat.yml").read_text(encoding="utf-8"))
    assert "VARIABLE_UNUSED" not in [i.code for i in issues]


def test_yaml_syntax_error_is_reported_as_issue_not_raised():
    """语法错误走 LintIssue，不冒泡成解析异常 —— 闸门要能把它和其他问题一起列出来。"""
    issues = errors_of(lint_yaml("name: 坏\ndataset:\n  name: t\n   tables: []\n"))
    assert issues
    assert issues[0].code == "CONTRACT_SYNTAX_ERROR"
    assert issues[0].yaml_line


def test_empty_and_non_mapping_contracts_are_rejected():
    assert errors_of(lint_yaml(""))[0].code == "CONTRACT_EMPTY"
    assert errors_of(lint_yaml("- 一\n- 二\n"))[0].code == "CONTRACT_NOT_MAPPING"


def test_missing_dataset_is_an_error():
    issues = errors_of(lint_yaml("name: 没有 dataset\n"))
    assert [i.code for i in issues] == ["DATASET_MISSING"]


def test_lint_contract_with_text_matches_lint_yaml():
    """给了 YAML 文本时，``lint_contract`` 与 ``lint_yaml`` 结论必须一致。

    不一致意味着「CLI 放行、别处报错」这种最难查的分歧。
    """
    from reconq.contracts.loader import load_contract

    text = BAD_DUP.read_text(encoding="utf-8")
    # BAD_DUP 的重复 path 会让 load_contract 抛错，因此改用好契约比对
    good_text = GOOD.read_text(encoding="utf-8")
    contract = load_contract(str(GOOD))
    assert lint_contract(contract, yaml_text=good_text) == lint_yaml(good_text)


def test_lint_contract_without_text_still_finds_dangling_refs():
    """没给文本时也跑同一套规则（从领域模型回退出映射）。"""
    from reconq.contracts.loader import load_contract

    contract = load_contract(str(GOOD))
    issues = lint_contract(contract)
    assert errors_of(issues) == []


def test_duplicate_column_declaration_is_an_error():
    text = """
name: 重复列
dataset:
  name: t
  tables:
    - name: t
      columns:
        - name: amount
          data_type: decimal
          required: true
        - name: amount
          data_type: string
          required: false
"""
    issues = errors_of(lint_yaml(text))
    assert "DUPLICATE_COLUMN" in [i.code for i in issues]


def test_check_without_path_is_an_error():
    text = """
name: 缺 path
dataset:
  name: t
  tables:
    - name: t
      columns:
        - name: amount
          data_type: decimal
          required: true
          checks:
            - name: 没有 path 的口径
              type: not_empty
"""
    issues = errors_of(lint_yaml(text))
    assert "CHECK_PATH_MISSING" in [i.code for i in issues]


def test_dotted_path_shorthand_is_accepted():
    """``path: rows.amount_sum`` 这种点分简写与列表写法等价。"""
    text = """
name: 点分简写
dataset:
  name: t
  tables:
    - name: t
      columns:
        - name: amount
          data_type: decimal
          required: true
          checks:
            - name: 两位小数
              path: amount.scale
              type: decimal_scale
              scale: 2
      checks:
        - name: 笔数不为零
          path: rows.row_count
          type: row_count
          min: 1
"""
    assert errors_of(lint_yaml(text)) == []


def test_lint_issue_as_dict_round_trips_every_field():
    issues = errors_of(lint_source(str(BAD_DUP)))
    payload = issues[0].as_dict()
    assert set(payload) == {"code", "severity", "message", "path", "yaml_line", "hint"}
    assert payload["severity"] == "error"
    assert payload["yaml_line"] == issues[0].yaml_line


def test_lint_never_mutates_the_contract_file(tmp_path):
    """校验是只读操作：跑一百遍文件字节不变。"""
    target = tmp_path / "wechat.yml"
    target.write_text((CONTRACT_DIR / "wechat.yml").read_text(encoding="utf-8"), encoding="utf-8")
    before = target.read_bytes()
    for _ in range(3):
        lint_source(str(target))
    assert target.read_bytes() == before


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
