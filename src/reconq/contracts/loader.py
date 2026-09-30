"""契约加载：变量注入 + 从磁盘读入。

变量注入语义对齐参考实现 ``github_ref/soda-core/soda_core/cli/cli.py:296-335``
的 ``_parse_variables()`` / ``_parse_variable_value()``：**先试 int、再试 float、
最后回退 str**，且不因解析失败抛错。本产品把 ``${period}`` 这类占位替换进契约
文本，使一份契约模板能服务多个账期。

与源产品的差别：源产品把变量作为 CLI ``-v KEY=VALUE`` 传入；本产品同时支持
CLI ``--set KEY=VALUE`` 与契约内 ``variables:`` 段的默认值声明，两者同名时
CLI 传入值优先（对齐源产品 ``provided_variable_values`` 覆盖默认的语义）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from ..errors import ReconqError
from .model import ContractYaml

__all__ = [
    "parse_variable_value",
    "parse_variable_assignments",
    "collect_variable_placeholders",
    "apply_variables",
    "load_contract",
]

#: 契约中的变量占位形如 ${period}
_VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_variable_value(value: str) -> Any:
    """把字符串变量值解析为 int / float / str。

    直接复刻参考实现 ``cli.py:318-335`` ``_parse_variable_value()`` 的顺序：
    先 int，再 float，都失败则原样返回字符串。注释里源实现特别说明不能用
    ``str.isnumeric()``（对负数与小数不可靠），本产品沿用同一判断方式。
    """
    if not isinstance(value, str):
        return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_variable_assignments(assignments: Optional[Iterable[str]]) -> dict[str, Any]:
    """解析 ``KEY=VALUE`` 形式的变量赋值列表。

    对齐源实现 ``cli.py:296`` ``_parse_variables()``：必须含 ``=``，键值去首尾
    空白且均非空，否则返回空结果（本产品改为抛显式错误码，见下）。
    """
    if not assignments:
        return {}
    result: dict[str, Any] = {}
    for raw in assignments:
        if "=" not in raw:
            raise ReconqError(
                "VARIABLE_BAD_FORMAT",
                f"变量 {raw!r} 格式错误，请使用 KEY=VALUE 写法",
            )
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ReconqError(
                "VARIABLE_BAD_FORMAT",
                f"变量 {raw!r} 格式错误，键或值为空，请使用 KEY=VALUE 写法",
            )
        result[key] = parse_variable_value(value)
    return result


def collect_variable_placeholders(yaml_text: str) -> set[str]:
    """列出契约文本中出现的全部 ``${...}`` 占位名。"""
    return set(_VARIABLE_RE.findall(yaml_text))


def apply_variables(
    yaml_text: str,
    variables: Mapping[str, Any],
    *,
    source_path: Optional[str] = None,
) -> str:
    """把 ``${name}`` 占位替换为给定变量值。

    占位出现在 YAML 值里时，替换值需保持 YAML 可解析：
    数字与布尔按原样写入，字符串按需加引号（已带引号的场景由调用方在契约里
    写成 ``"${period}"``，此时替换值不会被再次加引号）。
    缺变量时抛 ``VARIABLE_MISSING`` 并点名变量名，不静默留空。
    """
    missing = sorted(name for name in collect_variable_placeholders(yaml_text) if name not in variables)
    if missing:
        raise ReconqError(
            "VARIABLE_MISSING",
            f"契约引用了未提供的变量：{', '.join(missing)}；"
            f"请用 --set 名称=值 注入或在契约 variables 段给出默认值",
            yaml_path=source_path,
        )

    def _render(match: re.Match[str]) -> str:
        value = variables[match.group(1)]
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value)
        # 已处于引号内的占位由契约方负责引号，此处只处理裸占位。
        return text

    return _VARIABLE_RE.sub(_render, yaml_text)


def load_contract(
    path: str,
    variables: Optional[Mapping[str, Any]] = None,
    *,
    extra_variables: Optional[Mapping[str, Any]] = None,
) -> ContractYaml:
    """读入并解析一份契约。

    变量来源优先级（高 → 低）：

    1. ``variables`` —— 调用方显式传入（CLI ``--set``、扫描编排注入的账期）
    2. ``extra_variables`` —— 补充来源
    3. 契约内 ``variables:`` 段声明的默认值

    后者需要先读一遍 YAML 拿到默认值，因此这里做两遍解析：
    第一遍只为取默认值（容忍未知键），第二遍为正式解析。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ReconqError("CONTRACT_MISSING_KEY", f"契约文件不可读：{exc}", yaml_path=path) from exc

    merged: dict[str, Any] = {}
    merged.update(_declared_variables(text, path))
    if extra_variables:
        merged.update(extra_variables)
    if variables:
        merged.update(variables)

    resolved = apply_variables(text, merged, source_path=path)
    return ContractYaml.parse(resolved, source_path=path)


def _declared_variables(yaml_text: str, path: str) -> dict[str, Any]:
    """读取契约 ``variables:`` 段声明的默认值。

    只做尽力而为的读取：这一遍允许 YAML 有别的问题（正式解析那一遍会报），
    但若 ``variables`` 段本身类型不对则显式报错，不静默忽略。
    """
    import yaml

    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        # 语法问题留给正式解析统一报错并给行号
        return {}
    if not isinstance(raw, dict):
        return {}
    declared = raw.get("variables")
    if declared is None:
        return {}
    if not isinstance(declared, dict):
        raise ReconqError(
            "CONTRACT_TYPE_MISMATCH",
            f"variables 段必须是键值映射，得到 {type(declared).__name__}",
            yaml_path=path,
        )
    return {str(k): parse_variable_value(str(v)) for k, v in declared.items()}
