"""check 路径选择器：只跑一部分对账口径。

语义对齐参考实现 ``github_ref/soda-core/soda_core/cli/cli.py:212-245``
``--check-filter`` 的帮助文本与 ``CheckSelector.parse_all()`` 调用点
（``cli.py:270``）：

- **多字段 AND**：``type=decimal_sum,column=amount`` 要求两个条件同时满足
- **同字段 OR**：``type=decimal_sum,type=row_count`` 两个类型任一即可
- **通配**：``column=am*``、``type=?ecimal_sum``，用 ``fnmatch`` 语义

多个 ``--check-filter`` 之间为**并集**：财务科只想看跨日切分一条口径时
不必重跑全量。

本产品另加一条源产品没有的能力：``path=rows.amount_sum`` 直接按 check 路径
点名（对齐源产品 ``-cp/--check-paths`` 的按路径筛选），并在
``filter_checks()`` 中保持契约声明的原始顺序。
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Iterable, Optional, Sequence

from ..errors import ReconqError
from .model import CheckYaml, ContractYaml

__all__ = [
    "CheckSelector",
    "CheckSelectorParseError",
    "filter_checks",
]

#: 支持按属性筛选的键（对齐源产品 --check-filter 支持的 key 集合）
SUPPORTED_KEYS = ("type", "name", "column", "path", "qualifier", "attributes.<key>")

#: check 自身可被筛选的属性（源产品称 attributes，本产品落到 properties）
_ATTRIBUTE_PREFIX = "attributes."


class CheckSelectorParseError(ReconqError):
    """选择器表达式非法。错误码沿用统一的 ``CHECK_SELECTOR_INVALID``。"""

    def __init__(self, detail: str) -> None:
        super().__init__("CHECK_SELECTOR_INVALID", detail)


class CheckSelector:
    """一条 check 过滤条件：``key=value`` 的组合。

    多个键之间 AND；同一键重复出现时 OR；值支持 ``*`` / ``?`` 通配。
    """

    def __init__(self, conditions: dict[str, list[str]]) -> None:
        if not conditions:
            raise CheckSelectorParseError("选择器至少要有一个 key=value 条件")
        self.conditions = {k: list(v) for k, v in conditions.items()}

    @classmethod
    def parse(cls, expression: str) -> "CheckSelector":
        """解析单条 ``--check-filter`` 表达式。"""
        if expression is None or not expression.strip():
            raise CheckSelectorParseError("选择器表达式为空，请写成 key=value")
        conditions: dict[str, list[str]] = {}
        for part in expression.split(","):
            token = part.strip()
            if not token:
                continue
            if "=" not in token:
                raise CheckSelectorParseError(
                    f"条件 {token!r} 非法：缺少 '='，请写成 key=value"
                )
            key, value = token.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or not value:
                raise CheckSelectorParseError(
                    f"条件 {token!r} 非法：键或值为空，请写成 key=value"
                )
            if key != "attributes" and not key.startswith(_ATTRIBUTE_PREFIX) and key not in SUPPORTED_KEYS:
                # 未知键不静默接受：写错键名的人以为筛过了，其实筛的是全部
                raise CheckSelectorParseError(
                    f"条件 {token!r} 的键 {key!r} 不受支持；"
                    f"支持的键：{', '.join(SUPPORTED_KEYS)}"
                )
            conditions.setdefault(key, []).append(value)
        if not conditions:
            raise CheckSelectorParseError(f"选择器 {expression!r} 未解析出任何条件")
        return cls(conditions)

    @classmethod
    def parse_all(cls, selector_list: Optional[Iterable[str]]) -> list["CheckSelector"]:
        """解析一组 ``--check-filter``，每条一个选择器。

        对齐源产品 ``CheckSelector.parse_all(args.check_filter)``：
        传空（``None`` 或空列表）返回空列表，表示**不筛选**。
        任意一条非法即整体抛错（源产品在 CLI 层捕获后 ``exit_with_code``，
        本产品改为抛带错误码的异常）。

        同时接受**裸口径路径**：``row_count``、``transaction_id.*``、
        ``rows.refund_sum.zero``。财务科在工单里抄的往往就是契约里那条
        path，让人先翻译成 ``path=rows.*`` 才能跑一遍是没有道理的 ——
        裸路径按 ``path=`` 前缀归一，判据仍是同一张表。
        """
        if not selector_list:
            return []
        selectors: list[CheckSelector] = []
        for expression in selector_list:
            if expression is None:
                continue
            selectors.extend(cls.parse_each(expression))
        return selectors

    @classmethod
    def parse_each(cls, expression: str) -> list["CheckSelector"]:
        """把一条命令行表达式拆成**若干条**选择器（并集）。

        裸口径名按「path 命中或 type 命中」处理，所以一条 ``row_count``
        展开成两条选择器（``path=row_count`` 与 ``type=row_count``）。
        财务科在工单里抄的往往就是契约里那条 path 或那个算子名
        （``row_count``、``not_empty``、``transaction_id.*``、
        ``rows.refund_sum.zero``）。两种写法都得能直接跑，而不是逼人先想
        清楚它到底是哪一类 —— 想不清楚正是让人卡住的原因。

        注意是**两条选择器**而不是 ``path=x,type=x`` 一条：同一个选择器
        内多键之间是 AND，那样写会要求同时满足「path 等于 x」且「type
        等于 x」，永远匹配不到。命令行多个表达式之间才是并集。
        """
        text = (expression or "").strip()
        if not text:
            return []
        if "=" in text:
            return [cls.parse(text)]
        tokens = [p.strip() for p in text.split(",") if p.strip()]
        if not tokens:
            return []
        return [cls.parse(expr) for token in tokens for expr in (f"path={token}", f"type={token}")]

    def matches(self, check: CheckYaml, column: Optional[str] = None) -> bool:
        """判断一条 check 是否命中本选择器。键之间 AND，同键多值 OR。"""
        for key, values in self.conditions.items():
            actual = self._actual_value(check, column, key)
            if actual is None:
                return False
            if not any(fnmatch(str(actual), pattern) for pattern in values):
                return False
        return True

    @staticmethod
    def _actual_value(check: CheckYaml, column: Optional[str], key: str) -> Optional[str]:
        if key == "type":
            return check.type
        if key == "path":
            return check.path_str
        if key == "name":
            return check.display_name
        if key == "column":
            return column
        if key == "qualifier":
            qualifier = check.properties.get("qualifier")
            return None if qualifier is None else str(qualifier)
        if key.startswith(_ATTRIBUTE_PREFIX):
            attr = key[len(_ATTRIBUTE_PREFIX):]
            if attr not in check.properties:
                return None
            return str(check.properties[attr])
        return None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        inner = ",".join(f"{k}={'|'.join(v)}" for k, v in self.conditions.items())
        return f"CheckSelector({inner})"


def filter_checks(
    contract: ContractYaml,
    selectors: Optional[Sequence[CheckSelector]] = None,
) -> list[tuple[CheckYaml, Optional[str]]]:
    """按选择器挑出要执行的 check，**保持契约声明的原始顺序**。

    返回 ``(check, 所在列名)`` 列表；列名为 ``None`` 表示 dataset 级 check。
    ``selectors`` 为空表示不过滤，返回全部 —— 与源产品 ``--check-filter``
    未给时跑全量的语义一致。
    """
    items = [(check, column.name if column else None) for check, column in contract.iter_checks()]
    if not selectors:
        return items
    return [item for item in items if any(sel.matches(item[0], item[1]) for sel in selectors)]
