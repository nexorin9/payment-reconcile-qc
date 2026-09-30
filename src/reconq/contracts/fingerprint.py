"""契约指纹、口径变更影响面与回归基线对拍（Task 17）。

要回答的三个问题
----------------

1. **这份契约相对上次改了什么？** —— :func:`fingerprint_contract` /
   :func:`fingerprint_source` 给出一份内容指纹，:func:`diff_fingerprints`
   给出结构化差异（新增/删除/改参数的 check path），并渲染成人能读的
   Markdown 片段。
2. **这次改动会影响谁？** —— :func:`impact_scope` 把差异映射成受影响渠道与
   受影响口径，并给出面板提示文案。
3. **重跑会不会把结论翻过来？** —— :func:`compare_baseline` 把本次 check
   结果与基线快照对拍，列出**结论翻转**的 check（pass→fail / fail→pass）。

指纹的两个层级
--------------

- ``digest`` —— 整份契约的规范化内容。**连说明文字、版本号一起算**。
- ``check_digest`` —— 只算**会改变口径结论**的那部分：表与列的定义、
  check 的 type/path/参数、变量默认值。契约里 ``name`` / ``description``
  这类**纯标签**不参与。

为什么要分两层：财务科改一段口径说明、调一下契约版本号，并不会让上周
已经逐条确认过的差异重新变成待确认 —— 那会逼着他们把两百条差异再点一遍。
反过来，把 ``values`` 加一个枚举、把 ``min`` 从 0 抬到 1、把某列从可选改成
必填，结论随时可能翻，这时候**必须**把账期打回待确认。两层指纹就是为了让
「改说明」和「改口径」在系统里不是同一件事。

口径无关的指纹（``${period}`` 陷阱）
------------------------------------

契约里的 ``location: inbox/${period}/wechat/`` 在 :func:`load_contract` 注入
账期后会被替换成 ``inbox/2026-W36/wechat/``。若直接对**已加载的模型**取
``digest``，同一份契约在 2026-W36 与 2026-W40 会得到两个不同指纹 ——
「口径有没有改过」被账期噪声淹掉。

因此：

- :func:`fingerprint_source` 从**原始 YAML 文本**取指纹，占位符原样保留，
  与账期无关。做「这份契约改没改过」的判断**一律走它**。
- :func:`fingerprint_contract` 从已加载的 :class:`ContractYaml` 取指纹，
  用于只拿到模型、没有原始文件的调用方。若该模型已被注入变量，
  ``location`` 里带着账期值，此时 ``digest`` 会随账期变化 ——
  这一点有专项测试钉住（见 ``test_contract_fingerprint.py``），
  不靠注释提醒。

源产品对应
----------

参考实现 ``github_ref/soda-core/soda-core/src/soda_core/cli/cli.py`` 有
``contract verify / test / publish / fetch`` 四条腿
（``:144`` / ``:368`` / ``:338`` / ``:390``），其中 ``publish`` 与 ``fetch``
把契约整体推到/拉回远端注册表 —— 说明契约在源产品里**是被当作有版本的
对象**管理的。但源产品**没有本地指纹、没有 diff、没有影响面**：
契约改了什么只能靠 ``git diff`` 去看，而 ``contract test``（``:368``）
只做语法检查，连「这条 check 的参数变了」都读不出来。

本任务把「版本」这层落到**本地可核对**的产物上：指纹 + 快照表 + 结构化
差异 + 影响面 + 回归基线对拍。这是本轮相对源产品**新建**的部分，
新建的理由是本产品的契约是**对账口径**：口径一改，财务科上周的逐条确认
结论就要不要作废、哪些 check 会翻转，必须当场答得出，而不是回头翻 git。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Union

from ..errors import ReconqError
from .model import ContractYaml

__all__ = [
    "CheckSpec",
    "ColumnSpec",
    "TableSpec",
    "CanonicalContract",
    "ContractFingerprint",
    "CheckChange",
    "ColumnChange",
    "ContractDiff",
    "ImpactScope",
    "BaselineFlip",
    "BaselineComparison",
    "canonical_contract",
    "canonical_source",
    "fingerprint_contract",
    "fingerprint_source",
    "diff_fingerprints",
    "impact_scope",
    "render_markdown",
    "compare_baseline",
]


#: 标签类字段：只影响展示，不影响口径结论，不进 ``check_digest``。
_LABEL_FIELDS = ("name", "description")


# —— 规范化 ——————————————————————————————————————————————


@dataclass(frozen=True)
class CheckSpec:
    """一条 check 的语义描述（跨版本比对的最小单位）。"""

    type: str
    path: tuple[str, ...]
    properties: tuple[tuple[str, Any], ...] = ()
    name: str = ""

    @property
    def path_str(self) -> str:
        return ".".join(self.path)

    @property
    def key(self) -> str:
        """跨版本稳定的标识：``type:path``。与源产品同一条 check 的判定键一致。"""
        return f"{self.type}:{self.path_str}"

    def prop_map(self) -> dict[str, Any]:
        return dict(self.properties)

    def to_canonical(self, *, with_label: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "path": list(self.path),
            "properties": {k: _normalize_value(v) for k, v in self.properties},
        }
        if with_label and self.name:
            data["name"] = self.name
        return data


@dataclass(frozen=True)
class ColumnSpec:
    """一列的语义描述。"""

    name: str
    data_type: str = "string"
    required: bool = False
    description: str = ""
    checks: tuple[CheckSpec, ...] = ()

    def prop_map(self) -> dict[str, Any]:
        """列自身的属性（不含挂在其上的 check —— 那些由 check 侧比对）。"""
        return {"data_type": self.data_type, "required": self.required}

    def to_canonical(self, *, with_label: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "data_type": self.data_type,
            "required": self.required,
            "checks": [c.to_canonical(with_label=with_label) for c in self.checks],
        }
        if with_label and self.description:
            data["description"] = self.description
        return data


@dataclass(frozen=True)
class TableSpec:
    """一张账单表的语义描述。"""

    name: str
    location: str = ""
    description: str = ""
    columns: tuple[ColumnSpec, ...] = ()
    checks: tuple[CheckSpec, ...] = ()

    def to_canonical(self, *, with_label: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "location": self.location,
            "columns": [c.to_canonical(with_label=with_label) for c in self.columns],
            "checks": [c.to_canonical(with_label=with_label) for c in self.checks],
        }
        if with_label and self.description:
            data["description"] = self.description
        return data


@dataclass(frozen=True)
class CanonicalContract:
    """一份契约的规范化形态。``json.dumps(sort_keys=True)`` 后即可稳定序列化。"""

    tables: tuple[TableSpec, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    meta: tuple[tuple[str, Any], ...] = ()
    variables: tuple[tuple[str, Any], ...] = ()

    def to_canonical(self, *, with_label: bool) -> dict[str, Any]:
        # ``meta`` 里 name/version/description 是**契约级标签**：改它们
        # 不会让任何一条口径的结论变样，因此 ``with_label=False`` 时
        # 必须一并剔除 —— 否则「只改说明」和「改口径」算出来的
        # check_digest 完全一样，两层指纹就白分了。
        meta = {
            k: _normalize_value(v)
            for k, v in self.meta
            if with_label or k not in _ROOT_LABEL_KEYS
        }
        return {
            "meta": meta,
            "variables": {k: _normalize_value(v) for k, v in self.variables},
            "tables": [t.to_canonical(with_label=with_label) for t in self.tables],
            "checks": [c.to_canonical(with_label=with_label) for c in self.checks],
        }

    def check_keys(self) -> set[str]:
        """全部 check 的跨版本标识集合。"""
        keys = {c.key for c in self.checks}
        for table in self.tables:
            keys.update(c.key for c in table.checks)
            for col in table.columns:
                keys.update(c.key for c in col.checks)
        return keys


def _normalize_value(value: Any) -> Any:
    """标量规范化：数字去 ``.0``，列表按键排序去重。

    ``min: 0`` 与 ``min: 0.0`` 在 ``between`` 算子里完全等价，若不归一会
    被判成「口径改了」，白白让一个账期回到待确认。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 10)
    if isinstance(value, Mapping):
        return {str(k): _normalize_value(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(v) for v in value]
    return value


def _normalize_path(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(seg for seg in raw.split(".") if seg)
    if isinstance(raw, (list, tuple)):
        return tuple(str(seg) for seg in raw)
    raise ReconqError(
        "CHECK_PATH_INVALID",
        f"check.path 必须是列表或点分字符串，得到 {type(raw).__name__}",
    )


def _check_from_raw(raw: Mapping[str, Any]) -> CheckSpec:
    """从**原始 YAML 映射**取一条 check（属性直接写在 check 层）。"""
    reserved = {"path", "type", "name"}
    props = {str(k): v for k, v in raw.items() if k not in reserved}
    nested = raw.get("properties")
    if isinstance(nested, Mapping):
        props.update({str(k): v for k, v in nested.items()})
    return CheckSpec(
        type=str(raw.get("type") or ""),
        path=_normalize_path(raw.get("path")),
        properties=tuple(sorted(props.items(), key=lambda kv: kv[0])),
        name=str(raw.get("name") or ""),
    )


def _column_from_raw(raw: Mapping[str, Any]) -> ColumnSpec:
    checks = tuple(_check_from_raw(c) for c in (raw.get("checks") or []) if isinstance(c, Mapping))
    return ColumnSpec(
        name=str(raw.get("name") or ""),
        data_type=str(raw.get("data_type") or "string"),
        required=bool(raw.get("required", False)),
        description=str(raw.get("description") or ""),
        checks=tuple(sorted(checks, key=lambda c: c.key)),
    )


def _table_from_raw(raw: Mapping[str, Any]) -> TableSpec:
    columns = tuple(
        sorted(
            (_column_from_raw(c) for c in (raw.get("columns") or []) if isinstance(c, Mapping)),
            key=lambda c: c.name,
        )
    )
    checks = tuple(
        sorted(
            (_check_from_raw(c) for c in (raw.get("checks") or []) if isinstance(c, Mapping)),
            key=lambda c: c.key,
        )
    )
    return TableSpec(
        name=str(raw.get("name") or ""),
        location=str(raw.get("location") or ""),
        description=str(raw.get("description") or ""),
        columns=columns,
        checks=checks,
    )


#: 契约根上不参与 ``check_digest`` 的标签键。
_ROOT_LABEL_KEYS = ("name", "version", "description")


def canonical_source(yaml_text: str) -> CanonicalContract:
    """从**原始 YAML 文本**取规范化形态（占位符原样保留，与账期无关）。

    刻意不经过 :meth:`ContractYaml.parse`：一是重复 path 这类问题在解析
    阶段就会抛错（这里要能对着一份写坏的契约比指纹），二是解析会把
    ``${period}`` 换成账期值，抹掉「这份契约改没改过」的信号。
    """
    import yaml

    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise ReconqError(
            "CONTRACT_SYNTAX_ERROR",
            getattr(exc, "problem", None) or str(exc),
            yaml_line=(mark.line + 1) if mark is not None else None,
        ) from exc
    if not isinstance(raw, Mapping):
        raise ReconqError(
            "CONTRACT_TYPE_MISMATCH",
            f"契约顶层必须是键值映射，得到 {type(raw).__name__}",
        )
    return _canonical_from_mapping(raw)


def _check_spec_from_model(chk: Any) -> CheckSpec:
    """把领域模型的 :class:`CheckYaml` 转成 :class:`CheckSpec`。

    属性已由 :meth:`ContractYaml.parse` 归并进 ``properties``（源产品把
    check 的其余属性都收在 properties 下，本产品沿用同一约定），这里
    不再自己拆键。
    """
    return CheckSpec(
        type=chk.type,
        path=tuple(str(seg) for seg in chk.path),
        properties=tuple(sorted((str(k), v) for k, v in chk.properties.items())),
        name=chk.name or "",
    )


def canonical_contract(contract: ContractYaml) -> CanonicalContract:
    """从已加载的领域模型取规范化形态。

    与 :func:`canonical_source` 产出同一形状，但 ``location`` 里若已注入
    账期则带着账期值 —— 做变更比对请走 :func:`fingerprint_source`。
    """
    tables = tuple(
        sorted(
            (
                TableSpec(
                    name=tbl.name,
                    location=tbl.location or "",
                    description=tbl.description or "",
                    columns=tuple(
                        sorted(
                            (
                                ColumnSpec(
                                    name=col.name,
                                    data_type=col.data_type,
                                    required=col.required,
                                    description=col.description or "",
                                    checks=tuple(
                                        sorted(
                                            (_check_spec_from_model(c) for c in col.checks),
                                            key=lambda c: c.key,
                                        )
                                    ),
                                )
                                for col in tbl.columns
                            ),
                            key=lambda c: c.name,
                        )
                    ),
                    checks=tuple(
                        sorted(
                            (_check_spec_from_model(c) for c in tbl.checks),
                            key=lambda c: c.key,
                        )
                    ),
                )
                for tbl in contract.tables
            ),
            key=lambda t: t.name,
        )
    )
    checks = tuple(sorted((_check_spec_from_model(c) for c in contract.checks), key=lambda c: c.key))
    meta = {
        "channel": contract.channel or "",
        **{k: v for k, v in (("name", contract.name), ("version", contract.version),
                             ("description", contract.description)) if v},
    }
    return CanonicalContract(
        tables=tables,
        checks=checks,
        meta=tuple(sorted(meta.items(), key=lambda kv: kv[0])),
        variables=tuple(sorted((str(k), v) for k, v in contract.variables.items())),
    )


def _canonical_from_mapping(raw: Mapping[str, Any]) -> CanonicalContract:
    dataset = raw.get("dataset")
    if not isinstance(dataset, Mapping):
        raise ReconqError("CONTRACT_MISSING_KEY", "契约顶层缺少必填键 dataset（账单表列定义）")
    tables = tuple(
        sorted(
            (_table_from_raw(t) for t in (dataset.get("tables") or []) if isinstance(t, Mapping)),
            key=lambda t: t.name,
        )
    )
    checks = tuple(
        sorted(
            (_check_from_raw(c) for c in (raw.get("checks") or []) if isinstance(c, Mapping)),
            key=lambda c: c.key,
        )
    )
    meta = {"channel": str(raw.get("channel") or "")}
    for key in _ROOT_LABEL_KEYS:
        value = raw.get(key)
        if value:
            meta[key] = value
    variables = raw.get("variables")
    variables = variables if isinstance(variables, Mapping) else {}
    return CanonicalContract(
        tables=tables,
        checks=checks,
        meta=tuple(sorted(meta.items(), key=lambda kv: kv[0])),
        variables=tuple(sorted((str(k), v) for k, v in variables.items())),
    )


# —— 指纹 ——————————————————————————————————————————————


@dataclass(frozen=True)
class ContractFingerprint:
    """一份契约的两层指纹 + 规范化形态（diff 的输入）。"""

    digest: str
    check_digest: str
    canonical: CanonicalContract
    origin: str = ""

    @property
    def short(self) -> str:
        return self.digest[:12]

    @property
    def check_short(self) -> str:
        return self.check_digest[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "check_digest": self.check_digest,
            "origin": self.origin,
        }


def _sha256_of(canonical: CanonicalContract, *, with_label: bool) -> str:
    payload = json.dumps(
        canonical.to_canonical(with_label=with_label),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_contract(contract: ContractYaml) -> ContractFingerprint:
    """对已加载的契约取指纹。

    ``digest`` 计入标签（name/description/version），``check_digest`` 不计。
    若该模型已被 :func:`load_contract` 注入过账期变量，``location`` 里带着
    账期值 → ``digest`` 会随账期变化；口径变更比对请用
    :func:`fingerprint_source`。
    """
    canonical = canonical_contract(contract)
    return ContractFingerprint(
        digest=_sha256_of(canonical, with_label=True),
        check_digest=_sha256_of(canonical, with_label=False),
        canonical=canonical,
        origin=contract.name or contract.channel or "",
    )


def fingerprint_source(source: Union[str, bytes]) -> ContractFingerprint:
    """对**原始 YAML**（文本或文件路径）取指纹，与账期无关。

    这是「这份契约相对上次改了什么」唯一该走的入口：注释、键序、
    缩进、数字写法（``0`` vs ``0.0``）都被规范化掉，只留下实质差异。
    """
    text = _read_source(source)
    canonical = canonical_source(text)
    return ContractFingerprint(
        digest=_sha256_of(canonical, with_label=True),
        check_digest=_sha256_of(canonical, with_label=False),
        canonical=canonical,
        origin=source if isinstance(source, str) and os.path.sep in source else "",
    )


def _read_source(source: Union[str, bytes]) -> str:
    """把入参解析成 YAML 文本。

    判据是**有没有换行**而不是「像不像路径」：契约文本里满是
    ``inbox/${period}/wechat/`` 这类带斜杠的内容，按斜杠猜路径会把
    整份 YAML 当成文件名去 ``isfile``，然后报一句莫名其妙的
    「契约文件不可读」——内容明明就在手上。
    """
    if isinstance(source, bytes):
        return source.decode("utf-8")
    if "\n" in source:
        return source
    if not os.path.isfile(source):
        raise ReconqError("CONTRACT_MISSING_KEY", f"契约文件不可读：{source}", yaml_path=source)
    with open(source, "r", encoding="utf-8") as handle:
        return handle.read()


# —— 差异 ——————————————————————————————————————————————


@dataclass(frozen=True)
class CheckChange:
    """一条 check 的变化。``kind`` ∈ added / removed / changed。"""

    kind: str
    check_key: str
    check_type: str
    path: str
    name: str = ""
    prop_changes: tuple[tuple[str, Any, Any], ...] = ()

    def render(self) -> str:
        label = self.name or self.check_key
        if self.kind == "added":
            return f"新增口径 {label}（{self.check_key}）"
        if self.kind == "removed":
            return f"删除口径 {label}（{self.check_key}）"
        detail = "、".join(f"{k}：{_fmt(v0)} → {_fmt(v1)}" for k, v0, v1 in self.prop_changes)
        return f"口径 {label}（{self.check_key}）参数变化：{detail}"


@dataclass(frozen=True)
class ColumnChange:
    kind: str
    column: str
    prop_changes: tuple[tuple[str, Any, Any], ...] = ()

    def render(self) -> str:
        if self.kind == "added":
            return f"新增列 {self.column}"
        if self.kind == "removed":
            return f"删除列 {self.column}"
        detail = "、".join(f"{k}：{_fmt(v0)} → {_fmt(v1)}" for k, v0, v1 in self.prop_changes)
        return f"列 {self.column} 定义变化：{detail}"


@dataclass(frozen=True)
class ContractDiff:
    """两版契约的结构化差异。"""

    channel: str = ""
    old_digest: str = ""
    new_digest: str = ""
    old_check_digest: str = ""
    new_check_digest: str = ""
    added: tuple[CheckChange, ...] = ()
    removed: tuple[CheckChange, ...] = ()
    changed: tuple[CheckChange, ...] = ()
    column_changes: tuple[ColumnChange, ...] = ()
    meta_changes: tuple[tuple[str, Any, Any], ...] = ()
    variable_changes: tuple[tuple[str, Any, Any], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.added
            or self.removed
            or self.changed
            or self.column_changes
            or self.meta_changes
            or self.variable_changes
        )

    @property
    def semantic_changed(self) -> bool:
        """口径语义是否变化（决定要不要把账期打回待确认）。

        只改说明文字 / 版本号时 ``semantic_changed`` 为 False ——
        那类改动不该让财务科把已确认的差异再点一遍。
        """
        return self.old_check_digest != self.new_check_digest

    def changed_check_paths(self) -> list[str]:
        return sorted({c.path for c in (*self.added, *self.removed, *self.changed)})

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "old_digest": self.old_digest,
            "new_digest": self.new_digest,
            "old_check_digest": self.old_check_digest,
            "new_check_digest": self.new_check_digest,
            "is_empty": self.is_empty,
            "semantic_changed": self.semantic_changed,
            "added": [
                {"kind": c.kind, "check_key": c.check_key, "name": c.name, "path": c.path}
                for c in self.added
            ],
            "removed": [
                {"kind": c.kind, "check_key": c.check_key, "name": c.name, "path": c.path}
                for c in self.removed
            ],
            "changed": [
                {
                    "kind": c.kind,
                    "check_key": c.check_key,
                    "name": c.name,
                    "path": c.path,
                    "prop_changes": [
                        {"prop": k, "old": v0, "new": v1} for k, v0, v1 in c.prop_changes
                    ],
                }
                for c in self.changed
            ],
            "column_changes": [
                {
                    "kind": c.kind,
                    "column": c.column,
                    "prop_changes": [
                        {"prop": k, "old": v0, "new": v1} for k, v0, v1 in c.prop_changes
                    ],
                }
                for c in self.column_changes
            ],
            "meta_changes": [
                {"key": k, "old": v0, "new": v1} for k, v0, v1 in self.meta_changes
            ],
            "variable_changes": [
                {"key": k, "old": v0, "new": v1} for k, v0, v1 in self.variable_changes
            ],
        }


def _fmt(value: Any) -> str:
    if value is None:
        return "（无）"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{k}={_fmt(v)}" for k, v in sorted(value.items())) + "}"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _prop_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[tuple[str, Any, Any], ...]:
    keys = sorted(set(old) | set(new))
    out = []
    for key in keys:
        v0 = old.get(key)
        v1 = new.get(key)
        if _normalize_value(v0) != _normalize_value(v1):
            out.append((key, v0, v1))
    return tuple(out)


def diff_fingerprints(
    old: ContractFingerprint,
    new: ContractFingerprint,
    *,
    channel: str = "",
) -> ContractDiff:
    """比对两版指纹，给出结构化差异。

    比对粒度是 ``type:path``（与源产品判定一条 check 是不是同一条的键一致）：
    同键不同参数 → ``changed`` 并列出具体哪个参数从什么变成什么；
    只有一侧有 → ``added`` / ``removed``。
    """
    old_c, new_c = old.canonical, new.canonical
    old_checks = _checks_by_key(old_c)
    new_checks = _checks_by_key(new_c)

    added = tuple(
        CheckChange(
            kind="added",
            check_key=key,
            check_type=spec.type,
            path=spec.path_str,
            name=spec.name,
        )
        for key, spec in sorted(new_checks.items())
        if key not in old_checks
    )
    removed = tuple(
        CheckChange(
            kind="removed",
            check_key=key,
            check_type=spec.type,
            path=spec.path_str,
            name=spec.name,
        )
        for key, spec in sorted(old_checks.items())
        if key not in new_checks
    )
    changed: list[CheckChange] = []
    for key, new_spec in sorted(new_checks.items()):
        old_spec = old_checks.get(key)
        if old_spec is None:
            continue
        props = _prop_changes(old_spec.prop_map(), new_spec.prop_map())
        if props:
            changed.append(
                CheckChange(
                    kind="changed",
                    check_key=key,
                    check_type=new_spec.type,
                    path=new_spec.path_str,
                    name=new_spec.name or old_spec.name,
                    prop_changes=props,
                )
            )

    column_changes = _diff_columns(old_c, new_c)

    old_meta, new_meta = dict(old_c.meta), dict(new_c.meta)
    meta_changes = _prop_changes(
        {k: v for k, v in old_meta.items() if k not in _ROOT_LABEL_KEYS or k == "channel"},
        {k: v for k, v in new_meta.items() if k not in _ROOT_LABEL_KEYS or k == "channel"},
    )
    variable_changes = _prop_changes(dict(old_c.variables), dict(new_c.variables))

    return ContractDiff(
        channel=channel,
        old_digest=old.digest,
        new_digest=new.digest,
        old_check_digest=old.check_digest,
        new_check_digest=new.check_digest,
        added=added,
        removed=removed,
        changed=tuple(changed),
        column_changes=tuple(column_changes),
        meta_changes=meta_changes,
        variable_changes=variable_changes,
    )


def _checks_by_key(canonical: CanonicalContract) -> dict[str, CheckSpec]:
    out: dict[str, CheckSpec] = {}
    for spec in canonical.checks:
        out[spec.key] = spec
    for table in canonical.tables:
        for spec in table.checks:
            out[spec.key] = spec
        for col in table.columns:
            for spec in col.checks:
                out[spec.key] = spec
    return out


def _columns_by_name(canonical: CanonicalContract) -> dict[str, dict[str, ColumnSpec]]:
    return {t.name: {c.name: c for c in t.columns} for t in canonical.tables}


def _diff_columns(old: CanonicalContract, new: CanonicalContract) -> list[ColumnChange]:
    old_tables, new_tables = _columns_by_name(old), _columns_by_name(new)
    out: list[ColumnChange] = []
    for table_name in sorted(set(old_tables) | set(new_tables)):
        old_cols = old_tables.get(table_name, {})
        new_cols = new_tables.get(table_name, {})
        for col in sorted(set(new_cols) - set(old_cols)):
            out.append(ColumnChange(kind="added", column=col))
        for col in sorted(set(old_cols) - set(new_cols)):
            out.append(ColumnChange(kind="removed", column=col))
        for col in sorted(set(old_cols) & set(new_cols)):
            o, n = old_cols[col], new_cols[col]
            props = _prop_changes(o.prop_map(), n.prop_map())
            if props:
                out.append(ColumnChange(kind="changed", column=col, prop_changes=props))
    return out


# —— 影响面 ——————————————————————————————————————————————


@dataclass(frozen=True)
class ImpactScope:
    """一次契约变更的影响面。面板与结论单据此提示。"""

    channel: str = ""
    periods: tuple[str, ...] = ()
    changed_check_paths: tuple[str, ...] = ()
    requires_reconfirm: bool = False
    reason: str = ""
    diff: Optional[ContractDiff] = None

    def panel_hint(self) -> str:
        """面板顶部的一行提示。没有语义变化时返回空串（不打扰）。"""
        if not self.requires_reconfirm:
            return ""
        paths = "、".join(self.changed_check_paths[:4]) or "（无口径级差异）"
        more = f" 等 {len(self.changed_check_paths)} 条" if len(self.changed_check_paths) > 4 else ""
        return f"本期口径已变更（{self.channel}）：{paths}{more}，结论需重新确认"

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "periods": list(self.periods),
            "changed_check_paths": list(self.changed_check_paths),
            "requires_reconfirm": self.requires_reconfirm,
            "reason": self.reason,
            "panel_hint": self.panel_hint(),
        }


def impact_scope(
    diff: ContractDiff,
    *,
    channel: str = "",
    periods: Optional[Iterable[str]] = None,
) -> ImpactScope:
    """把差异映射成影响面。

    ``requires_reconfirm`` 只在**口径语义真的变了**时为真：只改说明文字
    或版本号不惊动财务科已确认的差异。
    """
    channel = channel or diff.channel
    if not diff.semantic_changed:
        reason = (
            "只改了口径说明/版本号等标签内容，check 的判定参数未变，"
            "已确认的差异结论继续有效"
        )
    elif not diff.is_empty:
        reason = _impact_reason(diff)
    else:
        reason = "契约内容与上次一致"
    return ImpactScope(
        channel=channel,
        periods=tuple(periods or ()),
        changed_check_paths=tuple(diff.changed_check_paths()),
        requires_reconfirm=diff.semantic_changed,
        reason=reason,
        diff=diff,
    )


def _impact_reason(diff: ContractDiff) -> str:
    parts: list[str] = []
    if diff.added:
        parts.append(f"新增 {len(diff.added)} 条口径")
    if diff.removed:
        parts.append(f"删除 {len(diff.removed)} 条口径")
    if diff.changed:
        parts.append(f"{len(diff.changed)} 条口径参数变化")
    if diff.column_changes:
        parts.append(f"{len(diff.column_changes)} 处列定义变化")
    if diff.variable_changes:
        parts.append(f"{len(diff.variable_changes)} 个变量默认值变化")
    return "、".join(parts) + "；口径变了，已确认的差异结论需重新确认"


# —— Markdown 渲染 ——————————————————————————————————————


def render_markdown(
    diff: ContractDiff,
    *,
    channel: str = "",
    scope: Optional[ImpactScope] = None,
) -> str:
    """把差异渲染成人能读的 Markdown 片段（贴进工单 / 面板 / 变更说明）。"""
    channel = channel or diff.channel or "(未标注渠道)"
    lines: list[str] = [f"## 契约口径变更 · {channel}"]
    lines.append("")
    lines.append(
        f"- 指纹：`{diff.old_digest[:12]}` → `{diff.new_digest[:12]}`"
        f"（口径指纹 `{diff.old_check_digest[:12]}` → `{diff.new_check_digest[:12]}`）"
    )
    if diff.is_empty:
        lines.append("- 与上次快照相比没有差异。")
        return "\n".join(lines)
    if scope is not None:
        lines.append(f"- 影响面：{scope.reason}")
    for title, items in (
        ("新增口径", diff.added),
        ("删除口径", diff.removed),
        ("参数变化", diff.changed),
    ):
        if not items:
            continue
        lines.append("")
        lines.append(f"### {title}（{len(items)}）")
        for item in items:
            lines.append(f"- {item.render()}")
    if diff.column_changes:
        lines.append("")
        lines.append(f"### 列定义变化（{len(diff.column_changes)}）")
        for item in diff.column_changes:
            lines.append(f"- {item.render()}")
    if diff.variable_changes:
        lines.append("")
        lines.append("### 变量默认值变化")
        for key, v0, v1 in diff.variable_changes:
            lines.append(f"- `{key}`：{_fmt(v0)} → {_fmt(v1)}")
    if diff.meta_changes:
        label_only = all(k in _ROOT_LABEL_KEYS for k, _, _ in diff.meta_changes)
        if not label_only:
            lines.append("")
            lines.append("### 契约属性变化")
            for key, v0, v1 in diff.meta_changes:
                lines.append(f"- `{key}`：{_fmt(v0)} → {_fmt(v1)}")
    return "\n".join(lines)


# —— 回归基线对拍 ——————————————————————————————————————


@dataclass(frozen=True)
class BaselineFlip:
    """一条结论翻转的 check。"""

    check_path: str
    check_name: str
    operator: str
    before_status: str
    after_status: str
    before_diff_amount: Optional[str] = None
    after_diff_amount: Optional[str] = None

    @property
    def direction(self) -> str:
        return f"{self.before_status}→{self.after_status}"

    def render(self) -> str:
        label = self.check_name or self.check_path
        head = f"{label}（{self.check_path}）{self.before_status} → {self.after_status}"
        if self.before_diff_amount or self.after_diff_amount:
            return f"{head}，差额 {self.before_diff_amount or '—'} → {self.after_diff_amount or '—'}"
        return head

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_path": self.check_path,
            "check_name": self.check_name,
            "operator": self.operator,
            "direction": self.direction,
            "before_status": self.before_status,
            "after_status": self.after_status,
            "before_diff_amount": self.before_diff_amount,
            "after_diff_amount": self.after_diff_amount,
        }


@dataclass(frozen=True)
class BaselineComparison:
    """本次结果与基线快照的对拍结论。"""

    flips: tuple[BaselineFlip, ...] = ()
    unchanged: int = 0
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    baseline_period: str = ""
    baseline_digest: str = ""

    @property
    def has_flip(self) -> bool:
        return bool(self.flips)

    def render(self) -> str:
        lines = [
            f"回归基线对拍（基线账期 {self.baseline_period or '—'} · 指纹 {self.baseline_digest[:12]}）"
        ]
        if self.has_flip:
            lines.append(f"  {len(self.flips)} 条口径结论翻转：")
            for flip in self.flips:
                lines.append(f"    - {flip.render()}")
        else:
            lines.append("  结论未翻转")
        lines.append(f"  未变化 {self.unchanged} 条")
        if self.added:
            lines.append(f"  本次新增口径 {len(self.added)} 条（基线里没有，不算翻转）")
        if self.removed:
            lines.append(f"  基线里有、本次没有的口径 {len(self.removed)} 条（口径被删，不算翻转）")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_period": self.baseline_period,
            "baseline_digest": self.baseline_digest,
            "has_flip": self.has_flip,
            "flips": [f.to_dict() for f in self.flips],
            "unchanged": self.unchanged,
            "added": list(self.added),
            "removed": list(self.removed),
        }


def compare_baseline(
    baseline: Optional[Mapping[str, Any]],
    current: Iterable[Any],
    *,
    period: str = "",
    digest: str = "",
) -> BaselineComparison:
    """把本次 check 结果与基线快照对拍，列出**结论翻转**的 check。

    只看 ``status``：pass→fail 与 fail→pass 都算翻转。基线里没有、本次新加
    的口径算 ``added`` 而不算翻转（它本来就没有「之前」可比）；基线里有、
    本次被删掉的算 ``removed`` —— 两者都不应被报成「结论变了」，
    否则每次改契约都会淹在假警报里。
    """
    rows = list(current)
    if not baseline:
        return BaselineComparison(
            added=tuple(sorted(r.check_path for r in rows)),
            baseline_period=period,
            baseline_digest=digest,
        )
    baseline_period = str(baseline.get("period") or period)
    baseline_digest = str(baseline.get("contract_digest") or digest)
    base_map: dict[str, Any] = dict(baseline.get("results") or {})

    flips: list[BaselineFlip] = []
    unchanged = 0
    current_paths: set[str] = set()
    for res in rows:
        current_paths.add(res.check_path)
        before = base_map.get(res.check_path)
        if before is None:
            continue
        before_status = str(before.get("status") or "")
        if before_status == res.status:
            unchanged += 1
            continue
        flips.append(
            BaselineFlip(
                check_path=res.check_path,
                check_name=res.check_name,
                operator=res.operator,
                before_status=before_status or "（无）",
                after_status=res.status,
                before_diff_amount=before.get("diff_amount"),
                after_diff_amount=res.diff_amount,
            )
        )
    added = tuple(sorted(p for p in current_paths if p not in base_map))
    removed = tuple(sorted(p for p in base_map if p not in current_paths))
    return BaselineComparison(
        flips=tuple(flips),
        unchanged=unchanged,
        added=added,
        removed=removed,
        baseline_period=baseline_period,
        baseline_digest=baseline_digest,
    )
