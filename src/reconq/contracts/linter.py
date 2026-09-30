"""契约静态校验器（lint）：在**取数之前**把写错的契约挡住。

参考实现 ``github_ref/soda-core/soda-tests/src/helpers/yaml_parsing_helpers.py``
的 ``ContractYaml.parse()`` 走的是「parse 成功即进入 verify」这一条路 ——
解析层只保证 YAML 形状对（键齐、类型对），**不保证契约语义自洽**：列名写错、
check 挂在不存在的列上、同一 path 写两遍、引用了没声明的变量，这些都要等到
真正逐 check 执行时才炸，而那时候六路账单已经取回来了。财务科白等一次扫描，
还得回头问「到底是账单的问题还是契约的问题」。

本模块把「契约自身写错」这一类问题提前到**加载阶段**，逐条给出
``code + 出处（YAML 行号）+ 中文说明``：

- **ERROR** 阻断加载（``reconq verify`` 退出码非零，不进入取数阶段）
- **WARN** 记录后放行（WARN 全是「覆盖度不够」类问题：缺一条兜底口径
  不会让口径算错，但会让某类问题查不出来）

两个入口：

- :func:`lint_yaml` —— 对**原始 YAML 文本**做校验。能报出领域模型解析阶段
  就会抛错的问题（重复 path），因此这是 CLI 启动路径真正使用的那一个。
- :func:`lint_contract` —— 对已解析的 :class:`ContractYaml` 做校验。给了
  ``yaml_text`` 时与 :func:`lint_yaml` 等价（并额外补上模型级规则），
  没给就只跑不依赖文本的规则。

**为什么不用「解析成功就等于契约对」**：源产品的契约是数据质量断言，
写错一条 path 最多少查一个字段；本产品的契约是**对账口径**，写错一条 path
会让财务科在一份「看着很专业」的差异清单上逐条回查导出方 —— 比直接报错贵得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

import yaml

from .model import ContractYaml
from .model import _line_index as yaml_line_index

__all__ = [
    "Severity",
    "LintIssue",
    "lint_yaml",
    "lint_contract",
    "lint_report",
    "lint_source",
    "errors_of",
    "warnings_of",
    "has_errors",
    "COLUMN_REF_PROPS",
    "DATASET_CHECK_PATH_ROOTS",
]

#: 严重级。ERROR 阻断，WARN 放行。
Severity = str
_ERROR: Severity = "error"
_WARN: Severity = "warning"

#: 契约 dataset / table 级 check 的 path 首段允许取的值。
#: ``rows`` 是「整张账单表」的挂载点（源产品同名的 rows 级检查点）；
#: ``dataset`` / ``table`` 是同义写法，一并放行，避免同一件事两种拼法报红。
DATASET_CHECK_PATH_ROOTS = ("rows", "dataset", "table")

#: check 属性里**指名一列**的属性名。值可以是字符串（列名）或字符串列表。
COLUMN_REF_PROPS = (
    "column",
    "other",
    "pair_column",
    "order_amount_column",
    "left",
    "right",
    "compare_column",
)

#: check 属性里**以列名为键**的条件字典（键必须落在已声明列上）。
COLUMN_KEYED_PROPS = ("where", "exclude_where")

#: 以 ``_variable`` 结尾的属性：值是**变量名**而不是列名。
#: 声明了却在契约里从没被用到的变量是 WARN（多半是复制模板时留下的）。
_VARIABLE_VALUE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: 参与 path 拼装的固定键，不算「check 自身的其他属性」
_CHECK_RESERVED_KEYS = ("path", "type", "name", "properties")


@dataclass(frozen=True)
class LintIssue:
    """一条契约问题。

    ``path`` 是**契约内的定位路径**（如
    ``dataset.tables[0].columns[2].checks[1]``），``yaml_line`` 是同一条问题在
    YAML 文件里的行号 —— 两者一起才能让财务科在编辑器里一跳到位。
    """

    code: str
    message: str
    path: str
    yaml_line: Optional[int] = None
    severity: Severity = _ERROR
    hint: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == _ERROR

    def render(self, *, source_path: Optional[str] = None) -> str:
        where = self.path
        if self.yaml_line:
            where = f"{self.path}（第 {self.yaml_line} 行）"
        head = f"[{self.severity.upper()}][{self.code}] {where}"
        parts = [f"{head}：{self.message}"]
        if self.hint:
            parts.append(f"建议：{self.hint}")
        if source_path:
            parts.append(f"契约={source_path}")
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "yaml_line": self.yaml_line,
            "hint": self.hint,
        }


# —— 入口 ——————————————————————————————————————————————


def lint_source(
    path: str,
    *,
    provided_variables: Optional[Mapping[str, Any]] = None,
) -> list[LintIssue]:
    """从磁盘读一份契约并做静态校验（不抛异常，问题以 LintIssue 交回）。"""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    return lint_yaml(text, source_path=path, provided_variables=provided_variables)


def lint_yaml(
    yaml_text: str,
    *,
    source_path: Optional[str] = None,
    provided_variables: Optional[Mapping[str, Any]] = None,
) -> list[LintIssue]:
    """对原始 YAML 文本做静态校验。

    刻意**不经过** :meth:`ContractYaml.parse`：重复 path 这类问题领域模型在
    解析阶段就会抛错，而这里要把它们作为 ``DUP_CHECK_PATH`` 带行号报出来。
    YAML 语法错误同样在这里以 LintIssue 呈现，而不是冒泡成解析异常。
    """
    issues: list[LintIssue] = []
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or str(exc)
        issues.append(
            LintIssue(
                code="CONTRACT_SYNTAX_ERROR",
                message=problem,
                path="(根)",
                yaml_line=(mark.line + 1) if mark is not None else None,
                severity=_ERROR,
                hint="按提示的行号列号检查缩进与引号；契约写错时先改这里，改完再跑扫描",
            )
        )
        return issues

    if raw is None:
        return [
            LintIssue(
                code="CONTRACT_EMPTY",
                message="契约内容为空",
                path="(根)",
                severity=_ERROR,
                hint="确认 --contracts 指向的是本渠道的契约文件而不是空文件",
            )
        ]
    if not isinstance(raw, dict):
        return [
            LintIssue(
                code="CONTRACT_NOT_MAPPING",
                message=f"契约顶层必须是键值映射，得到 {type(raw).__name__}",
                path="(根)",
                severity=_ERROR,
                hint="对照同渠道其他契约的顶层结构（name/channel/dataset/checks）",
            )
        ]

    lines = yaml_line_index(yaml_text)
    _lint_mapping(raw, lines, yaml_text, provided_variables or {}, issues)
    return issues


def lint_contract(
    contract: ContractYaml,
    *,
    source_path: Optional[str] = None,
    yaml_text: Optional[str] = None,
    provided_variables: Optional[Mapping[str, Any]] = None,
) -> list[LintIssue]:
    """对已解析的契约做静态校验。

    给了 ``yaml_text``（或能由 ``source_path`` 读到）时走与 :func:`lint_yaml`
    相同的一套规则 —— 领域模型是由同一份文本解析出来的，两者结论必须一致，
    否则会出现「CLI 放行、别处报错」这种最难查的分歧。没给文本时只跑不依赖
    文本的规则（列引用、枚举、覆盖度、算子已实现性）。
    """
    text = yaml_text
    if text is None and source_path:
        try:
            with open(source_path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            text = None
    if text is not None:
        return lint_yaml(
            text, source_path=source_path, provided_variables=provided_variables
        )

    issues: list[LintIssue] = []
    raw = _mapping_from_contract(contract)
    if raw is not None:
        _lint_mapping(raw, {}, "", provided_variables or {}, issues)
    return issues


# —— 规则实现 ————————————————————————————————————————————


def _lint_mapping(
    raw: Mapping[str, Any],
    lines: Mapping[str, int],
    text: str,
    provided: Mapping[str, Any],
    issues: list[LintIssue],
) -> None:
    def line_of(path: str) -> Optional[int]:
        return lines.get(path)

    # —— 变量 ——
    declared = raw.get("variables")
    declared_map: dict[str, Any] = {}
    if declared is None:
        pass
    elif not isinstance(declared, dict):
        issues.append(
            LintIssue(
                code="VARIABLES_NOT_MAPPING",
                message=f"variables 段必须是键值映射，得到 {type(declared).__name__}",
                path="variables",
                yaml_line=line_of("variables"),
                hint="写成 period: 2026-W36 这样的键值对",
            )
        )
    else:
        declared_map = {str(k): v for k, v in declared.items()}

    available = set(declared_map) | {str(k) for k in provided}
    used_from_text = {m for m in _PLACEHOLDER_RE.findall(text or "")}

    for name in sorted(used_from_text - available):
        issues.append(
            LintIssue(
                code="VARIABLE_UNDECLARED",
                message=f"契约使用了未声明的变量 ${{{name}}}",
                path="(契约文本)",
                yaml_line=_line_of_token(text, name),
                hint=(
                    f"在 variables 段补上 {name}: <默认值>，"
                    "或在命令行用 --set " + f"{name}=值 注入；"
                    "本产品不会拿空串去替换占位"
                ),
            )
        )

    # —— dataset ——
    dataset = raw.get("dataset")
    if dataset is None:
        issues.append(
            LintIssue(
                code="DATASET_MISSING",
                message="契约顶层缺少必填键 dataset（账单表列定义）",
                path="(根)",
                yaml_line=None,
                hint="对照 contracts/ 下同渠道其他契约补齐 dataset 段",
            )
        )
        _lint_root_checks(raw, lines, set(), issues, text or "")
        return
    if not isinstance(dataset, dict):
        issues.append(
            LintIssue(
                code="DATASET_NOT_MAPPING",
                message=f"dataset 必须是键值映射，得到 {type(dataset).__name__}",
                path="dataset",
                yaml_line=line_of("dataset"),
                hint="dataset 下至少要有 name 与 tables",
            )
        )
        _lint_root_checks(raw, lines, set(), issues, text or "")
        return

    tables = dataset.get("tables")
    if not isinstance(tables, list) or not tables:
        issues.append(
            LintIssue(
                code="TABLES_MISSING",
                message="dataset 缺少非空 tables 列表",
                path="dataset.tables",
                yaml_line=line_of("dataset.tables"),
                hint="一份契约至少声明一张渠道账单表",
            )
        )
        _lint_root_checks(raw, lines, set(), issues, text or "")
        return

    all_columns: set[str] = set()
    for t_idx, table in enumerate(tables):
        t_path = f"dataset.tables[{t_idx}]"
        if not isinstance(table, dict):
            issues.append(
                LintIssue(
                    code="TABLE_NOT_MAPPING",
                    message=f"tables[{t_idx}] 必须是键值映射，得到 {type(table).__name__}",
                    path=t_path,
                    yaml_line=line_of(t_path),
                    hint="对照 contracts/wechat.yml 的 tables 段写法",
                )
            )
            continue
        all_columns |= _lint_table(
            table,
            t_idx,
            lines,
            provided,
            issues,
            root_path=f"{t_path}.columns",
            lines_text=text or "",
        )

    _lint_root_checks(raw, lines, all_columns, issues, text or "")

    # —— 声明了却没被用到的变量 ——
    for name in sorted(declared_map):
        if name in used_from_text:
            continue
        if name in available and _used_as_variable_value(raw, name):
            continue
        issues.append(
            LintIssue(
                code="VARIABLE_UNUSED",
                message=(
                    f"variables 段声明了 {name}，但契约里没有任何地方用到它"
                    "（既没有 ${占位}，也没有被 *variable 属性引用）"
                ),
                path=f"variables.{name}",
                yaml_line=line_of(f"variables.{name}"),
                severity=_WARN,
                hint="多半是从别的渠道契约复制模板时留下的；确认无用可删，避免误以为改它能生效",
            )
        )


def _lint_table(
    table: Mapping[str, Any],
    t_idx: int,
    lines: Mapping[str, int],
    provided: Mapping[str, Any],
    issues: list[LintIssue],
    *,
    root_path: str,
    lines_text: str = "",
) -> set[str]:
    t_path = f"dataset.tables[{t_idx}]"
    columns = table.get("columns")
    if not isinstance(columns, list) or not columns:
        issues.append(
            LintIssue(
                code="TABLE_COLUMNS_MISSING",
                message="账单表没有声明任何列",
                path=f"{root_path}",
                yaml_line=lines.get(t_path),
                hint="dataset 声明的列是 check 的挂载点；没有列定义时列级 check 无处可挂",
            )
        )
        return set()

    declared: set[str] = set()
    col_types: dict[str, str] = {}
    required_no_check: list[tuple[str, int]] = []
    amount_no_scale: list[tuple[str, int]] = []
    for c_idx, col in enumerate(columns):
        c_path = f"{t_path}.columns[{c_idx}]"
        if not isinstance(col, dict):
            issues.append(
                LintIssue(
                    code="COLUMN_NOT_MAPPING",
                    message=f"columns[{c_idx}] 必须是键值映射，得到 {type(col).__name__}",
                    path=c_path,
                    yaml_line=lines.get(c_path),
                    hint="每列至少要有 name",
                )
            )
            continue
        name = col.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(
                LintIssue(
                    code="COLUMN_NAME_MISSING",
                    message=f"columns[{c_idx}] 缺少非空 name",
                    path=c_path,
                    yaml_line=lines.get(c_path),
                    hint="列名是 check 的挂载点，写不出名字就无法定位口径",
                )
            )
            continue
        if name in declared:
            issues.append(
                LintIssue(
                    code="DUPLICATE_COLUMN",
                    message=f"列 {name} 被重复声明",
                    path=c_path,
                    yaml_line=lines.get(c_path),
                    hint="同一张账单表里一个列名只能声明一次",
                )
            )
            continue
        declared.add(name)
        col_types[name] = str(col.get("data_type") or "string")
        _lint_column_checks(
            col,
            name,
            declared,
            col_types[name],
            c_idx,
            t_idx,
            lines,
            issues,
            lines_text,
        )
        # 覆盖度类 WARN 按**表**聚合，不逐列报：一份 10 列的契约逐列刷 10 条
        # WARN 没人看，合成一条「这些列缺兜底口径」才读得下去。
        if col.get("required") and not _has_check(col, "not_empty"):
            required_no_check.append((name, c_idx))
        if col_types[name] == "decimal" and not _has_check(col, "decimal_scale"):
            amount_no_scale.append((name, c_idx))

    # 覆盖度 WARN 按表聚合
    if required_no_check:
        names = "、".join(n for n, _ in required_no_check)
        issues.append(
            LintIssue(
                code="REQUIRED_COLUMN_NO_NOT_EMPTY",
                message=(
                    f"必填列未挂 not_empty 口径：{names}；"
                    "声明 required 只是给人看的，空值不会被拦下来，"
                    "后续对齐只能看到一条没有单号的行"
                ),
                path=f"{t_path}.columns",
                yaml_line=lines.get(f"{t_path}.columns[{required_no_check[0][1]}]"),
                severity=_WARN,
                hint="给这些列各加一条 type: not_empty 的口径",
            )
        )
    if amount_no_scale:
        names = "、".join(n for n, _ in amount_no_scale)
        issues.append(
            LintIssue(
                code="AMOUNT_COLUMN_NO_SCALE_CHECK",
                message=(
                    f"金额列未声明精度口径（decimal_scale）：{names}；"
                    "渠道给出「128」还是「128.00」不会被区分，"
                    "对账差额可能被当成脏数据"
                ),
                path=f"{t_path}.columns",
                yaml_line=lines.get(f"{t_path}.columns[{amount_no_scale[0][1]}]"),
                severity=_WARN,
                hint="给这些列加 decimal_scale 口径并写明 scale",
            )
        )

    # 悬空列引用 + 表级 check 的 path 根
    _lint_check_refs(
        table.get("checks"),
        f"{t_path}.checks",
        declared,
        lines,
        issues,
        scope="table",
        lines_text=lines_text,
    )
    return declared


def _has_check(col: Mapping[str, Any], chk_type: str) -> bool:
    checks = col.get("checks")
    if not isinstance(checks, list):
        return False
    return any(
        isinstance(c, dict) and str(c.get("type") or "") == chk_type for c in checks
    )


def _lint_column_checks(
    col: Mapping[str, Any],
    col_name: str,
    declared: set[str],
    data_type: str,
    c_idx: int,
    t_idx: int,
    lines: Mapping[str, int],
    issues: list[LintIssue],
    lines_text: str = "",
) -> None:
    _lint_check_refs(
        col.get("checks"),
        f"dataset.tables[{t_idx}].columns[{c_idx}].checks",
        declared,
        lines,
        issues,
        scope="column",
        owner_column=col_name,
        data_type=data_type,
        lines_text=lines_text,
    )


def _lint_root_checks(
    raw: Mapping[str, Any],
    lines: Mapping[str, int],
    known_columns: set[str],
    issues: list[LintIssue],
    lines_text: str = "",
) -> None:
    _lint_check_refs(
        raw.get("checks"),
        "checks",
        known_columns,
        lines,
        issues,
        scope="dataset",
        lines_text=lines_text,
    )


def _lint_check_refs(
    checks: Any,
    base_path: str,
    declared: set[str],
    lines: Mapping[str, int],
    issues: list[LintIssue],
    *,
    scope: str,
    owner_column: Optional[str] = None,
    data_type: str = "",
    lines_text: str = "",
) -> None:
    if checks is None:
        return
    if not isinstance(checks, list):
        issues.append(
            LintIssue(
                code="CHECKS_NOT_LIST",
                message=f"checks 必须是列表，得到 {type(checks).__name__}",
                path=base_path,
                yaml_line=lines.get(base_path),
                hint="每条口径写成 path / type / 属性 三部分",
            )
        )
        return

    seen: dict[str, int] = {}
    for idx, chk in enumerate(checks):
        c_path = f"{base_path}[{idx}]"
        if not isinstance(chk, dict):
            issues.append(
                LintIssue(
                    code="CHECK_NOT_MAPPING",
                    message=f"checks[{idx}] 必须是键值映射，得到 {type(chk).__name__}",
                    path=c_path,
                    yaml_line=lines.get(c_path),
                    hint="每条口径至少要有 path 与 type",
                )
            )
            continue

        chk_type = str(chk.get("type") or "")
        path_list = _check_path_list(chk, c_path, lines, issues)
        if chk_type and path_list is not None:
            key = f"{chk_type}:{'.'.join(path_list)}"
            if key in seen:
                issues.append(
                    LintIssue(
                        code="DUP_CHECK_PATH",
                        message=(
                            f"check path 重复定义：{key}（{base_path}[{seen[key]}] "
                            f"与 {c_path}）；两条口径指向同一个检查点，"
                            "逐 check 出证据时会重复出现"
                        ),
                        path=c_path,
                        yaml_line=lines.get(c_path),
                        hint="删掉重复的那条，或把 path 改成本产品真正要判的那个检查点",
                    )
                )
            else:
                seen[key] = idx

        if path_list is None:
            continue

        # —— path 挂载点 ——
        head = path_list[0]
        if scope == "column":
            if head != owner_column:
                issues.append(
                    LintIssue(
                        code="CHECK_PATH_COLUMN_MISMATCH",
                        message=(
                            f"列级口径的 path 首段是 {head}，但它挂在列 {owner_column} 上；"
                            "口径会作用到错误的列上"
                        ),
                        path=c_path,
                        yaml_line=lines.get(c_path),
                        hint=f"path 首段改成 {owner_column}，或把这条口径移到正确的列下",
                    )
                )
        elif head not in DATASET_CHECK_PATH_ROOTS and head not in declared:
            issues.append(
                LintIssue(
                    code="CHECK_PATH_ROOT_UNKNOWN",
                    message=(
                        f"{scope} 级口径的 path 首段 {head} 既不是 "
                        f"{'/'.join(DATASET_CHECK_PATH_ROOTS)}，也不是本表声明的列；"
                        "这条口径的落点不存在"
                    ),
                    path=c_path,
                    yaml_line=lines.get(c_path),
                    hint=(
                        "整表口径用 rows 开头（如 path: [rows, amount_sum]）；"
                        f"当前表声明的列：{', '.join(sorted(declared)) or '（无）'}"
                    ),
                )
            )

        # —— 属性里指名的列 ——
        props = _check_props(chk)
        for prop in COLUMN_REF_PROPS:
            if prop not in props:
                continue
            value = props[prop]
            names = value if isinstance(value, list) else [value]
            for name in names:
                if not isinstance(name, str) or not name:
                    continue
                if name not in declared:
                    issues.append(
                        LintIssue(
                            code="DANGLING_COLUMN_REF",
                            message=(
                                f"口径引用了未声明的列 {name}（属性 {prop}）；"
                                "该列没在 dataset.tables[].columns 里声明，"
                                "运行期取不到值却不会报错"
                            ),
                            path=c_path,
                            yaml_line=_line_of_key(
                                lines_text, lines.get(c_path), prop
                            ),
                            hint=(
                                "在 columns 里补上这一列，或把属性改成已声明的列名；"
                                f"当前表已声明：{', '.join(sorted(declared)) or '（无）'}"
                            ),
                        )
                    )

        # —— where / exclude_where 的键 ——
        for prop in COLUMN_KEYED_PROPS:
            cond = props.get(prop)
            if not isinstance(cond, dict):
                continue
            for key in cond:
                if isinstance(key, str) and key not in declared:
                    issues.append(
                        LintIssue(
                            code="DANGLING_COLUMN_REF",
                            message=(
                                f"口径的 {prop} 条件按列 {key} 过滤，"
                                "但该列没有在 dataset 里声明；条件恒不成立，"
                                "这条口径会「什么都没筛到」却报通过"
                            ),
                            path=c_path,
                            yaml_line=_line_of_key(
                                lines_text, lines.get(c_path), prop
                            ),
                            hint=(
                                "补上该列的声明，或把条件键改成已声明的列名；"
                                f"当前表已声明：{', '.join(sorted(declared)) or '（无）'}"
                            ),
                        )
                    )

        # —— 枚举口径 ——
        if chk_type == "in_enum":
            values = props.get("values")
            if not isinstance(values, list) or not values:
                issues.append(
                    LintIssue(
                        code="ENUM_VALUES_MISSING",
                        message="in_enum 口径没有给出 values（取值集合为空）",
                        path=c_path,
                        yaml_line=lines.get(c_path),
                        hint="枚举取值只能来自渠道官方规范；本产品不会拿一份猜的清单判账",
                    )
                )
            target = owner_column or head
            if scope == "column" and data_type not in ("", "string"):
                issues.append(
                    LintIssue(
                        code="ENUM_ON_NON_STRING",
                        message=(
                            f"in_enum 口径挂在 data_type={data_type} 的列 {target} 上；"
                            "非字符串列的取值与枚举清单不同构，这条口径多半配错了列"
                        ),
                        path=c_path,
                        yaml_line=lines.get(c_path),
                        severity=_WARN,
                        hint="确认这条枚举要判的确实是该列；若是，改挂到字符串列上",
                    )
                )

        # —— 算子是否已实现 ——
        if chk_type:
            unsupported = _unsupported_operator(chk_type)
            if unsupported:
                issues.append(
                    LintIssue(
                        code="CHECK_OPERATOR_UNSUPPORTED",
                        message=(
                            f"口径声明了算子 {chk_type}，本轮尚未实现"
                            f"（已实现：{', '.join(unsupported[1])}）"
                        ),
                        path=c_path,
                        yaml_line=lines.get(c_path),
                        hint="未实现的算子不会被静默当作通过；先改用已实现的算子表达同一口径",
                    )
                )


def _check_path_list(
    chk: Mapping[str, Any],
    c_path: str,
    lines: Mapping[str, int],
    issues: list[LintIssue],
) -> Optional[list[str]]:
    raw_path = chk.get("path")
    if raw_path is None:
        issues.append(
            LintIssue(
                code="CHECK_PATH_MISSING",
                message="口径缺少必填键 path",
                path=c_path,
                yaml_line=lines.get(c_path),
                hint="path 指明这条口径判的是哪个检查点，如 [rows, amount_sum]",
            )
        )
        return None
    if isinstance(raw_path, str):
        return [seg for seg in raw_path.split(".") if seg]
    if isinstance(raw_path, list):
        segs = [str(seg) for seg in raw_path if str(seg)]
        if not segs:
            issues.append(
                LintIssue(
                    code="CHECK_PATH_MISSING",
                    message="口径的 path 是空列表",
                    path=c_path,
                    yaml_line=lines.get(c_path),
                    hint="path 指明这条口径判的是哪个检查点，如 [rows, amount_sum]",
                )
            )
            return None
        return segs
    issues.append(
        LintIssue(
            code="CHECK_PATH_INVALID",
            message=f"口径的 path 必须是列表或点分字符串，得到 {type(raw_path).__name__}",
            path=c_path,
            yaml_line=lines.get(c_path),
            hint="写法一：path: [rows, amount_sum]；写法二：path: rows.amount_sum",
        )
    )
    return None


def _check_props(chk: Mapping[str, Any]) -> dict[str, Any]:
    """归并 check 的属性：``properties`` 子字典 + 写在 check 顶层的其余键。"""
    props: dict[str, Any] = {}
    declared = chk.get("properties")
    if isinstance(declared, dict):
        props.update(declared)
    for key, value in chk.items():
        if key in _CHECK_RESERVED_KEYS:
            continue
        props[key] = value
    return props


def _used_as_variable_value(raw: Mapping[str, Any], name: str) -> bool:
    """契约里是否有属性以变量名 ``name`` 为值（如 ``period_variable: period``）。"""
    stack: list[Any] = [raw]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(key, str)
                    and key.endswith("_variable")
                    and isinstance(value, str)
                    and value == name
                ):
                    return True
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _unsupported_operator(chk_type: str) -> Optional[tuple[str, Sequence[str]]]:
    """算子是否未实现。返回 ``(算子名, 已实现列表)``；已实现则返回 None。"""
    try:
        from ..engine.checks import SUPPORTED_OPERATORS
    except Exception:  # pragma: no cover - 引擎不可用时不阻断契约校验
        return None
    if chk_type in SUPPORTED_OPERATORS:
        return None
    return chk_type, list(SUPPORTED_OPERATORS)


def _line_of_token(text: str, name: str) -> Optional[int]:
    """在原文里找 ``${name}`` 第一次出现的行号（1 起）。"""
    if not text:
        return None
    needle = "${" + name + "}"
    for i, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return i
    return None


def _line_of_key(text: str, from_line: Optional[int], key: str) -> Optional[int]:
    """从 ``from_line`` 起向后找 ``key:`` 这一行。

    静态索引只能定位到 check **块**的起始行；财务科要改的是块里那一个属性
    （``column: amt``），所以再往下找几行落到真正出问题的地方。找不到就退回
    块起始行 —— 行号是给人导航的，宁可粗一点也不能错到别的 check 上。
    """
    if not text or not from_line:
        return from_line
    lines = text.splitlines()
    pattern = re.compile(r"^\s*-?\s*" + re.escape(key) + r"\s*:")
    for i in range(max(from_line - 1, 0), len(lines)):
        if pattern.match(lines[i]):
            return i + 1
        # 已经越过本 check 块（下一个同缩进或更浅的条目）就停
        if i > from_line - 1 and re.match(r"^\s*-\s", lines[i]) and not pattern.match(lines[i]):
            stripped = lines[i].lstrip()
            if stripped.startswith("- name:") or stripped.startswith("- type:"):
                break
    return from_line


def _mapping_from_contract(contract: ContractYaml) -> Optional[Mapping[str, Any]]:
    """把领域模型回退成原始映射，使规则在**没有文本**时也能跑同一套逻辑。"""
    try:
        return contract.model_dump()
    except Exception:  # pragma: no cover - 防御性
        return None


# —— 汇总工具 ————————————————————————————————————————————


def errors_of(issues: Iterable[LintIssue]) -> list[LintIssue]:
    return [i for i in issues if i.is_error]


def warnings_of(issues: Iterable[LintIssue]) -> list[LintIssue]:
    return [i for i in issues if not i.is_error]


def has_errors(issues: Iterable[LintIssue]) -> bool:
    return any(i.is_error for i in issues)


def lint_report(
    issues: Sequence[LintIssue],
    *,
    source_path: Optional[str] = None,
) -> str:
    """把问题列表渲染成给人看的一段文本（CLI 用）。"""
    if not issues:
        return "契约静态校验通过：0 个 ERROR，0 个 WARN"
    lines = [
        f"契约静态校验：{len(errors_of(issues))} 个 ERROR，"
        f"{len(warnings_of(issues))} 个 WARN"
    ]
    lines.extend(issue.render(source_path=source_path) for issue in issues)
    return "\n".join(lines)
