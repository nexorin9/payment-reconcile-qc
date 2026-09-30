"""对账契约的领域模型：ColumnYaml / CheckYaml / ContractYaml。

模型语义对齐参考实现 ``github_ref/soda-core/soda-tests/src/helpers/yaml_parsing_helpers.py``
的 ``ContractYaml.parse()``：一份契约 = 1 个 dataset（账单表及其列定义）
+ 若干 check（列级 check 挂在列上，dataset 级 check 挂在契约根下），
每条 check 带 ``path``（定位到检查点）与多组属性。

本产品的差别只在**业务含义**：dataset 换成渠道账单表，check 换成对账口径
（笔数 / 必填非空 / 交易类型枚举 / 金额合计 / 跨日切分 / 退费红冲 / 医保勾稽等式）。
写这份 YAML 的人（财务科 / 信息科）认得出自己那份契约 —— 形状一样，只是内容换了。
"""

from __future__ import annotations

import re
from typing import Any, Iterator, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ReconqError

__all__ = [
    "ColumnYaml",
    "CheckYaml",
    "ContractYaml",
    "check_path_str",
]

#: 契约中 check.path 允许的段名：字母或下划线开头，其余为字母数字下划线
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_path_str(path: list[str]) -> str:
    """把 path 列表拼成点分字符串，用于界面展示与选择器匹配。"""
    return ".".join(path)


class CheckYaml(BaseModel):
    """一条对账口径 check。

    ``path`` 的语义对齐源产品：dataset 级 check 的 path 指向 dataset 上的一个
    检查点（如 ``[rows, amount_sum]``），列级 check 的 path 指向该列上的检查点
    （如 ``[amount, sum]``）。属性既可写在与源产品同构的 ``properties`` 字典里，
    也可直接写在本层（``type``/``values``/``scale`` 等），解析时统一并入 ``properties``。
    """

    model_config = ConfigDict(extra="forbid")

    path: list[str] = Field(min_length=1)
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)
    name: Optional[str] = None

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, value: Any) -> Any:
        """允许 ``path: rows.amount_sum`` 这种点分简写，落成列表。"""
        if isinstance(value, str):
            return [seg for seg in value.split(".") if seg]
        return value

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, str):
            raise ValueError(f"check.type 必须是字符串，得到 {type(value).__name__}")
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> "CheckYaml":
        if not self.type:
            raise ValueError("check 缺少必填键 type")
        for seg in self.path:
            if not _PATH_SEGMENT_RE.match(seg):
                raise ValueError(f"check.path 段 {seg!r} 非法：只允许字母/数字/下划线且不以数字开头")
        if self.name is not None and not self.name.strip():
            raise ValueError("check.name 若给出则不得为空白")
        return self

    @property
    def path_str(self) -> str:
        return check_path_str(self.path)

    @property
    def display_name(self) -> str:
        """人类可读名：优先用中文 name，其次 type:path。"""
        return self.name or f"{self.type}:{self.path_str}"


class ColumnYaml(BaseModel):
    """账单表的一列。列级 check 挂在列上（源产品同名能力）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    data_type: str = "string"
    required: bool = False
    checks: list[CheckYaml] = Field(default_factory=list)
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("column.name 不得为空")
        return value


class TableYaml(BaseModel):
    """一张渠道账单表。``location`` 指向该渠道的取数落点（文件通道本轮用）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    columns: list[ColumnYaml] = Field(min_length=1)
    location: Optional[str] = None
    checks: list[CheckYaml] = Field(default_factory=list)
    description: Optional[str] = None

    @field_validator("columns")
    @classmethod
    def _unique_columns(cls, columns: list[ColumnYaml]) -> list[ColumnYaml]:
        names = [c.name for c in columns]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"列名重复定义：{', '.join(dupes)}")
        return columns

    def column(self, name: str) -> Optional[ColumnYaml]:
        for col in self.columns:
            if col.name == name:
                return col
        return None


class DatasetYaml(BaseModel):
    """账单表数据集。列定义 + 表级 check 挂载点。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    tables: list[TableYaml] = Field(min_length=1)


class ContractYaml(BaseModel):
    """一份渠道对账契约。"""

    model_config = ConfigDict(extra="forbid")

    dataset: DatasetYaml
    checks: list[CheckYaml] = Field(default_factory=list)
    name: Optional[str] = None
    version: Optional[str] = None
    channel: Optional[str] = None
    description: Optional[str] = None
    variables: dict[str, Any] = Field(default_factory=dict)

    @field_validator("checks")
    @classmethod
    def _unique_check_paths(cls, checks: list[CheckYaml]) -> list[CheckYaml]:
        seen: dict[str, int] = {}
        for idx, chk in enumerate(checks):
            key = f"{chk.type}:{chk.path_str}"
            if key in seen:
                raise ValueError(
                    f"重复定义的 check：{key}（dataset 级第 {seen[key]} 条与第 {idx} 条）"
                )
            seen[key] = idx
        return checks

    # —— 解析入口 ——————————————————————————————————————————————

    @classmethod
    def parse(
        cls,
        yaml_text: str,
        *,
        source_path: Optional[str] = None,
    ) -> "ContractYaml":
        """把契约 YAML 文本解析为领域模型。

        对齐源产品 ``ContractYaml.parse()`` 的入口形态：文本进、模型出。
        语法错误、缺必填键、类型不符、未知键一律抛 :class:`ReconqError`，
        并带契约文件路径与 YAML 行号，**不静默丢列、不填默认值**。
        """
        try:
            raw = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = (mark.line + 1) if mark is not None else None
            problem = getattr(exc, "problem", None) or str(exc)
            raise ReconqError(
                "CONTRACT_SYNTAX_ERROR",
                problem,
                yaml_path=source_path,
                yaml_line=line,
            ) from exc

        if raw is None:
            raise ReconqError(
                "CONTRACT_MISSING_KEY",
                "契约内容为空",
                yaml_path=source_path,
            )
        if not isinstance(raw, dict):
            raise ReconqError(
                "CONTRACT_TYPE_MISMATCH",
                f"契约顶层必须是键值映射，得到 {type(raw).__name__}",
                yaml_path=source_path,
            )
        if "dataset" not in raw:
            raise ReconqError(
                "CONTRACT_MISSING_KEY",
                "契约顶层缺少必填键 dataset（账单表列定义）",
                yaml_path=source_path,
            )

        lines = _line_index(yaml_text)
        data = dict(raw)
        data["dataset"] = _build_dataset(data["dataset"], source_path, lines, "dataset")
        if "checks" in data:
            data["checks"] = _build_checks(data["checks"], source_path, lines, "checks")

        try:
            return cls(**data)
        except ReconqError:
            raise
        except Exception as exc:  # pydantic 校验失败 → 显式错误码 + 行号
            field_path, message = _first_pydantic_error(exc)
            raise ReconqError(
                "CONTRACT_TYPE_MISMATCH",
                f"{field_path}：{message}",
                yaml_path=source_path,
                yaml_line=lines.get(field_path) if lines else None,
            ) from exc

    @classmethod
    def load(cls, path: str) -> "ContractYaml":
        """从磁盘读一份契约并解析。"""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ReconqError("CONTRACT_MISSING_KEY", f"契约文件不可读：{exc}", yaml_path=path) from exc
        return cls.parse(text, source_path=path)

    # —— 便捷访问 ——————————————————————————————————————————————

    @property
    def tables(self) -> list[TableYaml]:
        return self.dataset.tables

    def table(self, name: Optional[str] = None) -> TableYaml:
        """按名取表；不传则取第一张。"""
        if not self.dataset.tables:
            raise ReconqError("CONTRACT_MISSING_KEY", "契约未声明任何账单表", yaml_path=self.name)
        if name is None:
            return self.dataset.tables[0]
        for tbl in self.dataset.tables:
            if tbl.name == name:
                return tbl
        raise ReconqError(
            "CONTRACT_MISSING_KEY",
            f"契约中没有名为 {name!r} 的账单表，可选：{[t.name for t in self.dataset.tables]}",
            yaml_path=self.name,
        )

    def iter_checks(self) -> Iterator[tuple[CheckYaml, Optional[ColumnYaml]]]:
        """按声明顺序遍历全部 check，附带其所在列（dataset 级 check 为 None）。"""
        for tbl in self.dataset.tables:
            for chk in tbl.checks:
                yield chk, None
            for col in tbl.columns:
                for chk in col.checks:
                    yield chk, col
        for chk in self.checks:
            yield chk, None

    def all_check_paths(self) -> list[str]:
        return [chk.path_str for chk, _ in self.iter_checks()]


# —— 内部构造工具 ——————————————————————————————————————————————


def _build_dataset(
    raw: Any,
    source_path: Optional[str],
    lines: dict[str, int],
    yaml_key: str,
) -> DatasetYaml:
    if not isinstance(raw, dict):
        raise ReconqError(
            "CONTRACT_TYPE_MISMATCH",
            f"dataset 必须是键值映射，得到 {type(raw).__name__}",
            yaml_path=source_path,
            yaml_line=lines.get(yaml_key),
        )
    if "tables" not in raw:
        raise ReconqError(
            "CONTRACT_MISSING_KEY",
            "dataset 缺少必填键 tables（账单表列表）",
            yaml_path=source_path,
            yaml_line=lines.get(yaml_key),
        )

    dataset_raw = dict(raw)
    tables: list[TableYaml] = []
    for idx, tbl_raw in enumerate(raw["tables"]):
        tbl_key = f"{yaml_key}.tables[{idx}]"
        if not isinstance(tbl_raw, dict):
            raise ReconqError(
                "CONTRACT_TYPE_MISMATCH",
                f"tables[{idx}] 必须是键值映射，得到 {type(tbl_raw).__name__}",
                yaml_path=source_path,
                yaml_line=lines.get(tbl_key),
            )
        tbl_data = dict(tbl_raw)
        if "columns" in tbl_data:
            tbl_data["columns"] = _build_columns(tbl_data["columns"], source_path, lines, tbl_key)
        if "checks" in tbl_data:
            tbl_data["checks"] = _build_checks(tbl_data["checks"], source_path, lines, f"{tbl_key}.checks")
        try:
            tables.append(TableYaml(**tbl_data))
        except ReconqError:
            raise
        except Exception as exc:
            field_path, message = _first_pydantic_error(exc)
            raise ReconqError(
                "CONTRACT_TYPE_MISMATCH",
                f"{field_path}：{message}",
                yaml_path=source_path,
                yaml_line=lines.get(tbl_key),
            ) from exc
    dataset_raw["tables"] = tables
    return DatasetYaml(**dataset_raw)


def _build_columns(
    raw: Any,
    source_path: Optional[str],
    lines: dict[str, int],
    table_key: str,
) -> list[ColumnYaml]:
    if not isinstance(raw, list):
        raise ReconqError(
            "CONTRACT_TYPE_MISMATCH",
            f"columns 必须是列表，得到 {type(raw).__name__}",
            yaml_path=source_path,
            yaml_line=lines.get(table_key),
        )
    columns: list[ColumnYaml] = []
    for idx, col_raw in enumerate(raw):
        col_key = f"{table_key}.columns[{idx}]"
        if not isinstance(col_raw, dict):
            raise ReconqError(
                "CONTRACT_TYPE_MISMATCH",
                f"columns[{idx}] 必须是键值映射，得到 {type(col_raw).__name__}",
                yaml_path=source_path,
                yaml_line=lines.get(col_key),
            )
        col_data = dict(col_raw)
        if "checks" in col_data:
            col_data["checks"] = _build_checks(col_data["checks"], source_path, lines, f"{col_key}.checks")
        try:
            columns.append(ColumnYaml(**col_data))
        except ReconqError:
            raise
        except Exception as exc:
            field_path, message = _first_pydantic_error(exc)
            raise ReconqError(
                _check_error_code(field_path, message),
                f"{field_path}：{message}",
                yaml_path=source_path,
                yaml_line=lines.get(col_key),
            ) from exc
    return columns


def _build_checks(
    raw: Any,
    source_path: Optional[str],
    lines: dict[str, int],
    yaml_key: str,
) -> list[CheckYaml]:
    if not isinstance(raw, list):
        raise ReconqError(
            "CONTRACT_TYPE_MISMATCH",
            f"checks 必须是列表，得到 {type(raw).__name__}",
            yaml_path=source_path,
            yaml_line=lines.get(yaml_key),
        )
    checks: list[CheckYaml] = []
    for idx, chk_raw in enumerate(raw):
        chk_key = f"{yaml_key}[{idx}]"
        if not isinstance(chk_raw, dict):
            raise ReconqError(
                "CONTRACT_TYPE_MISMATCH",
                f"checks[{idx}] 必须是键值映射，得到 {type(chk_raw).__name__}",
                yaml_path=source_path,
                yaml_line=lines.get(chk_key),
            )
        data = dict(chk_raw)
        # 与源产品同构：check 的其余属性都收在 properties 下；本产品允许
        # 把常见属性直接写在 check 层，这里归并回 properties 供算子统一读取。
        properties: dict[str, Any] = dict(data.pop("properties", None) or {})
        for key in [k for k in data if k not in ("path", "type", "name")]:
            properties[key] = data.pop(key)
        data["properties"] = properties
        if "path" not in data:
            raise ReconqError(
                "CHECK_PATH_INVALID",
                f"checks[{idx}] 缺少必填键 path",
                yaml_path=source_path,
                yaml_line=lines.get(chk_key),
            )
        if "type" not in data:
            raise ReconqError(
                "CHECK_TYPE_MISSING",
                f"checks[{idx}] 缺少必填键 type（决定由哪个算子执行）",
                yaml_path=source_path,
                yaml_line=lines.get(chk_key),
            )
        try:
            checks.append(CheckYaml(**data))
        except ReconqError:
            raise
        except Exception as exc:
            field_path, message = _first_pydantic_error(exc)
            raise ReconqError(
                _check_error_code(field_path, message),
                f"checks[{idx}].{field_path}：{message}",
                yaml_path=source_path,
                yaml_line=lines.get(chk_key),
            ) from exc
    return checks


def _check_error_code(field_path: str, message: str = "") -> str:
    """按出错的字段挑一个更贴切的错误码。

    ``model_validator(mode="after")`` 抛出的错误 pydantic 会归到 ``(根)``，
    因此同时看消息里点名的字段。
    """
    tail = field_path.split(".")[-1]
    if tail in ("path",) or "check.path" in message or "path 段" in message:
        return "CHECK_PATH_INVALID"
    if tail == "type" or "缺少必填键 type" in message:
        return "CHECK_TYPE_MISSING"
    return "CONTRACT_TYPE_MISMATCH"


def _line_index(yaml_text: str) -> dict[str, int]:
    """建立 ``dataset.tables[0].columns[2]`` → YAML 行号的粗索引。

    只用于报错时给出「大概在哪一行」；行号不参与任何业务判断。
    """
    index: dict[str, int] = {}

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                key = key_node.value
                child = f"{prefix}.{key}" if prefix else str(key)
                index[child] = key_node.start_mark.line + 1
                walk(value_node, child)
        elif isinstance(node, yaml.SequenceNode):
            for idx, item in enumerate(node.value):
                child = f"{prefix}[{idx}]"
                index[child] = item.start_mark.line + 1
                walk(item, child)

    try:
        root = yaml.compose(yaml_text)
    except yaml.YAMLError:
        return {}
    if root is not None:
        walk(root, "")
    return index


def _first_pydantic_error(exc: Exception) -> tuple[str, str]:
    """从 pydantic 校验异常里取出第一条错误（字段路径 + 消息）。"""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            loc = ".".join(str(p) for p in first.get("loc", ())) or "(根)"
            return loc, str(first.get("msg", ""))
        except Exception:  # pragma: no cover - 防御性
            pass
    return "(根)", str(exc)
