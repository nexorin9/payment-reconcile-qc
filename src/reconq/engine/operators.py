"""对账断言算子库：新增一条口径只写 YAML，不写代码。

源产品主路径的兑现点在 ``contract verify``：check 的 ``type`` 决定由哪段
代码执行，属性（阈值、枚举集合、精度位数…）全部来自契约 YAML
（参考 ``github_ref/soda-core/soda_core/cli/cli.py:212-245`` 的
``CheckSelector.parse_all()`` 按 ``type=`` 筛选，以及
``github_ref/soda-tests/src/helpers/yaml_parsing_helpers.py`` 的
``ContractYaml.parse()`` 把 check 属性收进 ``properties``）。本产品把同一
条主路径搬到六路渠道账单上，``operators.py`` 就是那个「type → 执行函数」
的注册表。

**为什么必须是注册表而不是一串 if**：财务科与信息科会持续加口径
（院内自定义的科室白名单、单笔金额上限、结算方式必填…）。若每加一条就要
改 Python，本产品的口径就会停在「产品方写死的几条」，而对账真正要核的
恰恰是院内那几十条细口径。因此本文件只提供**通用断言原语**，业务阈值一律
由 ``contracts/<channel>.yml`` 提供 —— 代码里出现硬编码阈值的那一刻，
口径就再也不能由财务科自己调整了。

设计口径（三条硬约束）：

1. **算子之间互不依赖**：每个算子只读 ``rows`` 与自己的契约属性，不碰别的
   算子的中间结果，也不写全局状态。组合口径（如「金额非负且两位小数」）
   在 YAML 里写成两条 check，而不是在 Python 里拼一条。
2. **证据必须可回查**：每条证据都带 (source_file, row_no, raw_line,
   field, value)，由 :func:`~reconq.engine.common.evidence_of` 统一构造。
   定位不到行的结论不得作为 fail 依据。
3. **阈值只来自契约**：精度、区间、集合、正则一律读 ``chk.properties``；
   缺属性时显式抛 ``CHECK_PATH_INVALID``，**不拿一个自己猜的默认值
   去判账** —— 猜出来的阈值会报出一堆看着很专业的假问题。

本文件不实现需要账期侧上下文的口径（跨日切分、退费红冲、金额合计对照
基准），那些留在 :mod:`reconq.engine.checks`；两边共用 ``common`` 的取值
判据与证据结构。
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional, Sequence

from ..contracts.model import CheckYaml, ColumnYaml
from ..errors import ReconqError
from .common import (
    DEFAULT_MAX_EVIDENCE,
    BillRow,
    CheckContext,
    CheckResult,
    build_result,
    evidence_of,
    target_column,
    to_decimal,
    trunc_note,
)

__all__ = [
    "OperatorResult",
    "Operator",
    "OPERATORS",
    "get_operator",
    "register_operator",
    "operator_names",
    "check_operator",
]

#: 算子返回值。与 :class:`~reconq.engine.checks.CheckResult` 同构：算子层
#: 只管产出「状态 + 证据 + 差额」，check 路径与中文名由执行器补齐。
OperatorResult = CheckResult

#: 算子签名：``(rows, chk, column, max_evidence, ctx) → OperatorResult``。
#: ``column`` 是该 check 所在列（dataset 级 check 为 None），算子据此知道
#: 自己被挂在哪一列上；``ctx`` 保留给需要账期上下文的算子（当前这层的
#: 算子不依赖它，但签名统一后新增这类算子不必再改调用点）。
Operator = Callable[
    [Sequence[BillRow], CheckYaml, Optional[ColumnYaml], int, CheckContext],
    OperatorResult,
]

#: 算子注册表：``check.type`` → 执行函数。**本轮唯一**的 type 分派真源；
#: :mod:`reconq.engine.checks` 的行级口径也注册进同一张表。
OPERATORS: dict[str, Operator] = {}


def register_operator(name: str) -> Callable[[Operator], Operator]:
    """把一个算子登记到注册表。名称即契约里的 ``check.type``。"""

    def deco(fn: Operator) -> Operator:
        if name in OPERATORS:
            raise ReconqError(
                "CHECK_TYPE_MISSING",
                f"算子 {name} 重复注册；两个算子抢同一个 type 会让契约作者"
                f"无法判断自己写的是哪一条",
            )
        OPERATORS[name] = fn
        return fn

    return deco


def get_operator(name: str) -> Optional[Operator]:
    return OPERATORS.get(name)


def operator_names() -> tuple[str, ...]:
    """已注册的算子名（供 CLI 提示与自检用）。"""
    return tuple(sorted(OPERATORS))


# —— 参数读取 ——————————————————————————————————————————————


def _require(chk: CheckYaml, key: str, what: str) -> Any:
    """取契约属性；缺了就显式报错，**不返回默认值**。

    精度、区间、集合、正则这类判账阈值一旦猜错，报出来的是「一堆看着很
    专业的假问题」，财务科会去逐条回查导出方 —— 比直接报错贵得多。
    """
    if key not in chk.properties or chk.properties[key] is None:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"口径 {chk.display_name}（type={chk.type}）缺少必填属性 "
            f"{key}（{what}）；阈值只来自契约 YAML，本产品不会用自己猜的"
            f"默认值替你判账",
        )
    return chk.properties[key]


def _optional_decimal(chk: CheckYaml, key: str, default: Decimal) -> Decimal:
    if key not in chk.properties or chk.properties[key] is None:
        return default
    try:
        return Decimal(str(chk.properties[key]).strip())
    except InvalidOperation as exc:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"口径 {chk.display_name} 的 {key}={chk.properties[key]!r} 不是合法数值",
        ) from exc


def _non_blank(rows: Sequence[BillRow], column: str) -> list[BillRow]:
    """只取该列有值的行，并把空值行数报出来。

    算子之间不重复劳动：必填非空由 ``not_empty`` 负责，本层的精度/区间/
    唯一性/正则/枚举类算子一律**跳过空值**并在消息里点名跳过了几行 ——
    否则「必填缺失」会被五条算子各报一次，证据列表被同一批行刷屏。
    """
    return [r for r in rows if str(r.get(column) or "").strip()]


def _blank_note(rows: Sequence[BillRow], column: str) -> str:
    blanks = len(rows) - len(_non_blank(rows, column))
    return f"，另有 {blanks} 行为空（由 not_empty 口径判定）" if blanks else ""


# —— 算子实现 ——————————————————————————————————————————————


@register_operator("not_empty")
def _op_not_empty(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> OperatorResult:
    """必填非空：失败时**列出全部**空值行号，不截断在第一条。"""
    target = target_column(chk, column)
    hits = [
        evidence_of(row, target, detail="必填字段为空")
        for row in rows
        if not str(row.get(target) or "").strip()
    ]
    return build_result(
        chk,
        "not_empty",
        hits,
        max_evidence,
        fail_msg=lambda n, m: f"必填列 {target} 有 {n} 行为空" + trunc_note(n, m),
        pass_msg=f"必填列 {target} 全部非空（{len(rows)} 行）",
    )


@register_operator("in_enum")
def _op_in_enum(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> OperatorResult:
    """交易类型 / 结算方式枚举：越界值全部列出，并给合法集合。

    空值**也算越界**：渠道导出里这一格空着，与填了「SUCCESS」是完全不同的
    两件事 —— 后者是渠道确认过交易状态，前者是渠道没给出状态。不把空值
    混进枚举判断，就等于替渠道把「未知」当「合法」放过去了。
    """
    target = target_column(chk, column)
    allowed = [str(v) for v in (_require(chk, "values", "合法取值集合") or [])]
    if not allowed:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"in_enum 口径 {chk.display_name} 的 values 为空，"
            f"空集合会让每一行都报越界",
        )
    hits: list = []
    offenders: dict[str, list[int]] = {}
    for row in rows:
        value = str(row.get(target) or "").strip()
        if value in allowed:
            continue
        offenders.setdefault(value or "（空）", []).append(row.row_no)
        hits.append(
            evidence_of(
                row,
                target,
                detail=(
                    f"渠道原始值「{value or '（空）'}」不在合法集合内"
                    f"（合法值：{'、'.join(allowed)}）"
                ),
            )
        )
    detail = "；".join(
        f"「{val}」× {len(nos)} 行（行号 {'、'.join(str(n) for n in nos[:8])}"
        + ("…）" if len(nos) > 8 else "）")
        for val, nos in offenders.items()
    )
    return build_result(
        chk,
        "in_enum",
        hits,
        max_evidence,
        fail_msg=lambda n, m: f"列 {target} 有 {n} 行取值越界：{detail}" + trunc_note(n, m),
        pass_msg=f"列 {target} 全部落在合法集合内（{len(rows)} 行，取值 {'、'.join(allowed)}）",
    )


@register_operator("decimal_scale")
def _op_decimal_scale(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> OperatorResult:
    """金额精度：值的小数位须**不超过**契约声明的位数。

    判「不超过」而不是「恰好等于」：``128.00`` 与 ``128`` 是同一笔钱，
    渠道导出少写两位小数不该报红；但 ``128.005`` 落在分位之下，说明这份
    账单的金额被二次折算过 —— 折算用的汇率/比例若与院内口径不同，整列
    合计就会差出一个看不出来的小数。

    空值不在这里判（交给 not_empty），否则「必填缺失」会被这条口径再报一次。
    """
    target = target_column(chk, column)
    scale = int(_require(chk, "scale", "允许的小数位数"))
    if scale < 0:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"decimal_scale 口径 {chk.display_name} 的 scale={scale} 非法，"
            f"小数位数不能为负",
        )
    checked = _non_blank(rows, target)
    hits: list = []
    for row in checked:
        raw = str(row.get(target)).strip()
        try:
            value = Decimal(raw.replace(",", "").replace("¥", "").replace("￥", ""))
        except InvalidOperation:
            raise ReconqError(
                "AMOUNT_UNPARSABLE",
                f"列 {target} 的值 {raw!r} 不是合法金额，无法判小数位",
                row_ref=f"{row.source_file}:{row.row_no}",
            ) from None
        # as_tuple().exponent 是负的小数位数；用 -exponent 取位数，
        # 指数为正（1E+2 这类科学计数写法）时按 0 位处理。
        actual_scale = max(0, -value.as_tuple().exponent)
        if actual_scale > scale:
            hits.append(
                evidence_of(
                    row,
                    target,
                    detail=(
                        f"本行 {raw} 有 {actual_scale} 位小数，"
                        f"超过契约允许的 {scale} 位（金额被二次折算过的典型迹象）"
                    ),
                )
            )
    return build_result(
        chk,
        "decimal_scale",
        hits,
        max_evidence,
        fail_msg=lambda n, m: (
            f"列 {target} 有 {n} 行小数位超过 {scale} 位" + trunc_note(n, m)
        ),
        pass_msg=(
            f"列 {target} 全部不超过 {scale} 位小数（{len(checked)} 行）"
            + _blank_note(rows, target)
        ),
    )


@register_operator("between")
def _op_between(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> OperatorResult:
    """数值区间：开闭区间均可配，边界值判定符合财务惯例。

    闭区间（``inclusive: true``，默认）：单笔缴费 5000.00 元在
    ``min: 0, max: 5000`` 内算合规 —— 院内自助机单笔上限往往就正好卡在
    5000，把上限判成排他会让每一笔顶格缴费都报红。
    开区间（``inclusive: false``）：用于「退款额须严格小于原单额」这类
    本来就不该取等号的判据。

    区间端点只来自契约；min/max 至少给一个，只给单边就是单边判据。
    """
    target = target_column(chk, column)
    has_min = chk.properties.get("min") is not None
    has_max = chk.properties.get("max") is not None
    if not has_min and not has_max:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"between 口径 {chk.display_name} 既没写 min 也没写 max，"
            f"没有任何边界的区间判不出任何东西",
        )
    low = _optional_decimal(chk, "min", Decimal("-Infinity")) if has_min else None
    high = _optional_decimal(chk, "max", Decimal("Infinity")) if has_max else None
    inclusive = bool(chk.properties.get("inclusive", True))

    checked = _non_blank(rows, target)
    hits: list = []
    for row in checked:
        value = to_decimal(row.get(target), row, target, chk)
        if inclusive:
            too_low = low is not None and value < low
            too_high = high is not None and value > high
        else:
            too_low = low is not None and value <= low
            too_high = high is not None and value >= high
        if not (too_low or too_high):
            continue
        bound = []
        if low is not None:
            bound.append(f"下界 {low}（{'含' if inclusive else '不含'}）")
        if high is not None:
            bound.append(f"上界 {high}（{'含' if inclusive else '不含'}）")
        hits.append(
            evidence_of(
                row,
                target,
                detail=(
                    f"本行 {target}={value} 越出区间 "
                    + "、".join(bound)
                    + ("（低于下界）" if too_low else "（高于上界）")
                ),
            )
        )
    span = "、".join(
        part
        for part in (
            f"≥ {low}" if low is not None else "",
            f"≤ {high}" if high is not None else "",
        )
        if part
    )
    return build_result(
        chk,
        "between",
        hits,
        max_evidence,
        fail_msg=lambda n, m: (
            f"列 {target} 有 {n} 行越出区间 {span}"
            f"（{'闭' if inclusive else '开'}区间）" + trunc_note(n, m)
        ),
        pass_msg=(
            f"列 {target} 全部落在区间 {span} 内（{'闭' if inclusive else '开'}区间，"
            f"{len(checked)} 行）"
            + _blank_note(rows, target)
        ),
    )


@register_operator("unique")
def _op_unique(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> OperatorResult:
    """单号唯一性：返回**全部**重复值与它们的行号。

    空值不参与唯一性判定：两个空单号不是「重复」，而是两行都缺单号 ——
    那是 ``not_empty`` 的职责。若把空值也纳入，本口径会把缺单号的行报成
    「单号重复」，把财务科引向完全错误的方向（去查重复入账，而实际是导出
    时单号列没落下来）。

    单号比对按**去首尾空白**处理：导出里同一个单号前后多一个空格，在
    Excel 里看着一样，在字符串比对里是两个不同值，会让「这一单入账两次」
    这种最该被抓住的问题从本口径下溜走。
    """
    target = target_column(chk, column)
    buckets: dict[str, list[BillRow]] = defaultdict(list)
    for row in rows:
        value = str(row.get(target) or "").strip()
        if not value:
            continue
        buckets[value].append(row)

    duplicated = {v: rs for v, rs in buckets.items() if len(rs) > 1}
    hits: list = []
    for value in sorted(duplicated):
        for row in duplicated[value]:
            hits.append(
                evidence_of(
                    row,
                    target,
                    detail=(
                        f"单号 {value} 在本表内出现 {len(duplicated[value])} 次"
                        f"（重复行号 {'、'.join(str(r.row_no) for r in duplicated[value])}）"
                    ),
                )
            )
    detail = "；".join(
        f"{value} 出现 {len(rs)} 次（行号 {'、'.join(str(r.row_no) for r in rs)}）"
        for value, rs in sorted(duplicated.items())
    )
    return build_result(
        chk,
        "unique",
        hits,
        max_evidence,
        fail_msg=lambda n, m: (
            f"列 {target} 有 {len(duplicated)} 个单号重复，共 {n} 行：{detail}"
            + trunc_note(n, m)
        ),
        pass_msg=(
            f"列 {target} 无重复单号（{len(buckets)} 个非空单号）"
            + _blank_note(rows, target)
        ),
    )


@register_operator("matches_regex")
def _op_matches_regex(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> OperatorResult:
    """整列正则匹配：格式不合的行全列出，并给出实际值。

    用途是把「渠道换了导出格式」挡在口径之前：单号列忽然多出一段前缀、
    时间列从 ``2026-09-02 10:01:05`` 变成 ``2026/09/02 10:01:05``，
    后续单号对齐与跨日切分都会跟着失准，而报错现场只剩一句含糊的
    「对不上」。正则写在契约里，渠道换格式时财务科自己改一行即可。

    正则只作**整串匹配**（``re.fullmatch``）：``pattern: 'EX-WX-.*'`` 的
    含义是「整串以 EX-WX- 开头」，不是「串里某处出现过 EX-WX-」——
    对账单号做子串匹配会把 ``WX-REFUND-EX-WX-001`` 判成合规单号。
    """
    target = target_column(chk, column)
    pattern_text = str(_require(chk, "pattern", "整串匹配的正则"))
    try:
        re.compile(pattern_text)
    except re.error as exc:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"matches_regex 口径 {chk.display_name} 的 pattern={pattern_text!r} "
            f"不是合法正则：{exc}",
        ) from exc
    ignore_case = bool(chk.properties.get("ignore_case", False))
    # 编译时带 flag，而不是 fullmatch 时传 —— ``Pattern.fullmatch(string,
    # pos, endpos)`` 的第二个位置参数是**起始位置**不是 flags，传错会让
    # ignore_case 静默失效（匹配从第 0 个 flag 值位置开始，等于不区分大小写
    # 的假象），或直接抛 TypeError。
    matcher = re.compile(pattern_text, re.IGNORECASE if ignore_case else 0)

    checked = _non_blank(rows, target)
    hits: list = []
    for row in checked:
        value = str(row.get(target)).strip()
        if matcher.fullmatch(value):
            continue
        hits.append(
            evidence_of(
                row,
                target,
                detail=(
                    f"实际值「{value}」不整串匹配 {pattern_text}"
                    + ("（忽略大小写）" if ignore_case else "")
                ),
            )
        )
    return build_result(
        chk,
        "matches_regex",
        hits,
        max_evidence,
        fail_msg=lambda n, m: (
            f"列 {target} 有 {n} 行不符合格式 {pattern_text}" + trunc_note(n, m)
        ),
        pass_msg=(
            f"列 {target} 全部符合格式 {pattern_text}（{len(checked)} 行）"
            + _blank_note(rows, target)
        ),
    )


@register_operator("column_not_equal")
def _op_column_not_equal(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> OperatorResult:
    """跨列不等式：两列逐行不得相等。

    院内最常见的真实用途是**对账双写列防呆**：院方要求账单同时带
    「商户订单号」与「渠道交易号」两列以备交叉核对，若某行两列取值相同，
    说明导出脚本把同一列写了两遍 —— 看着「双写齐全」，实际交叉核对能力
    为零。这类问题不会让合计对不上，只能靠这条不等式抓。

    两列任一为空时**不判不等式**：那是缺值问题，归 ``not_empty``；在此处
    报「两列不相等」既不成立也会把结论带偏。
    """
    target = target_column(chk, column)
    other = str(_require(chk, "other", "要比对的那一列名"))
    if other == target:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"column_not_equal 口径 {chk.display_name} 的 other 与被检列同名"
            f"（都是 {target}），一列永远等于自己，这条口径判不出任何东西",
        )
    checked = [
        r
        for r in rows
        if str(r.get(target) or "").strip() and str(r.get(other) or "").strip()
    ]
    hits: list = []
    for row in checked:
        left = str(row.get(target)).strip()
        right = str(row.get(other)).strip()
        if left != right:
            continue
        hits.append(
            evidence_of(
                row,
                target,
                detail=(
                    f"本行 {target} 与 {other} 取值相同（均为「{left}」），"
                    f"两列同值时无法交叉核对，多半是导出脚本把一列写了两遍"
                ),
            )
        )
    skipped = len(rows) - len(checked)
    return build_result(
        chk,
        "column_not_equal",
        hits,
        max_evidence,
        fail_msg=lambda n, m: (
            f"有 {n} 行 {target} 与 {other} 取值相同" + trunc_note(n, m)
        ),
        pass_msg=(
            f"{target} 与 {other} 逐行不同（{len(checked)} 行）"
            + (f"，另有 {skipped} 行因任一列为空未判" if skipped else "")
        ),
    )


@register_operator("set_contains")
def _op_set_contains(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> OperatorResult:
    """集合包含：整列取值须落在给定集合内（``in_enum`` 的严格同义写法）。

    两者判据其实相同，区别在**表达意图**与可维护性：``in_enum`` 是
    「这一列的取值只能是这样」的枚举校验；``set_contains`` 额外支持
    ``allow_extra``（列里出现的取值必须在集合内，但**并集**另行校验），
    用于「科室列必须落在本院科室清单内」这类需要引用外部清单的口径。

    与 ``in_enum`` 一样，空值算越界：集合没覆盖到空值，说明渠道没给这一格。
    """
    target = target_column(chk, column)
    allowed = {str(v) for v in (_require(chk, "values", "允许取值集合") or [])}
    if not allowed:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"set_contains 口径 {chk.display_name} 的 values 为空，"
            f"空集合会让每一行都报越界",
        )
    hits: list = []
    offenders: dict[str, list[int]] = {}
    for row in rows:
        value = str(row.get(target) or "").strip()
        if value in allowed:
            continue
        offenders.setdefault(value or "（空）", []).append(row.row_no)
        hits.append(
            evidence_of(
                row,
                target,
                detail=(
                    f"取值「{value or '（空）'}」不在集合内"
                    f"（集合：{'、'.join(sorted(allowed))}）"
                ),
            )
        )
    detail = "；".join(
        f"「{val}」× {len(nos)} 行（行号 {'、'.join(str(n) for n in nos[:8])}"
        + ("…）" if len(nos) > 8 else "）")
        for val, nos in offenders.items()
    )
    return build_result(
        chk,
        "set_contains",
        hits,
        max_evidence,
        fail_msg=lambda n, m: (
            f"列 {target} 有 {n} 行取值不在集合内：{detail}" + trunc_note(n, m)
        ),
        pass_msg=f"列 {target} 全部落在集合内（{len(rows)} 行）",
    )


# —— 跨列等式（等式与「逐行等于给定值」两种形态）——————————————


@register_operator("column_equality")
def _op_column_equality(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> OperatorResult:
    """跨列等式：医保勾稽等式（苏医保办发〔2021〕38 号 修订要点第四条）。

    两种写法：

    - ``left: A, right: [B, C], aggregate: sum`` —— A = B + C（勾稽等式）
    - ``compare_column`` + ``compare_value`` —— 某列逐行等于给定值
      （HIS 账期码须与本次扫描账期一致）

    两条医保等式**各自独立断言**：等式一挂了不代表等式二也挂，两条都要
    各出各的差额与行号 —— 财务科要按条追责，不是看一个总判。
    """
    compare_column = chk.properties.get("compare_column")
    compare_value = chk.properties.get("compare_value")
    if compare_column is not None:
        return _column_equals_constant(rows, chk, max_evidence, str(compare_column), compare_value)

    left = str(chk.properties.get("left") or "")
    right = [str(c) for c in (chk.properties.get("right") or [])]
    if not left or not right:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"column_equality 口径 {chk.display_name} 须写 left 与 right"
            f"（等式两侧列名），或写 compare_column/compare_value",
        )
    aggregate = str(chk.properties.get("aggregate") or "sum")
    if aggregate != "sum":
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"column_equality 口径 {chk.display_name} 的 aggregate={aggregate} 暂不支持，"
            f"本轮只支持 sum（勾稽等式按行求和）",
        )
    tolerance = to_decimal(chk.properties.get("tolerance", "0.01"), None, left, chk)

    left_total = Decimal("0")
    right_total = Decimal("0")
    hits: list = []
    for row in rows:
        lv = to_decimal(row.get(left), row, left, chk)
        rv = sum((to_decimal(row.get(c), row, c, chk) for c in right), Decimal("0"))
        left_total += lv
        right_total += rv
        gap = lv - rv
        if abs(gap) > tolerance:
            hits.append(
                evidence_of(
                    row,
                    left,
                    detail=(
                        f"等式不成立：{left}={lv} ≠ "
                        + " + ".join(f"{c}={to_decimal(row.get(c), row, c, chk)}" for c in right)
                        + f"（差 {gap} 元）"
                    ),
                )
            )
    total_gap = left_total - right_total
    expression = f"{left} = {' + '.join(right)}"
    return build_result(
        chk,
        "column_equality",
        hits,
        max_evidence,
        fail_msg=lambda n, m: (
            f"勾稽等式「{expression}」有 {n} 行不成立（容差 {tolerance}）；"
            f"全表 {left} 合计 {left_total} 元，右侧合计 {right_total} 元，差 {total_gap} 元"
        )
        + trunc_note(n, m),
        pass_msg=(
            f"勾稽等式「{expression}」逐行成立（{len(rows)} 行，"
            f"合计 {left_total} 元，容差 {tolerance}）"
        ),
        diff_amount=str(total_gap),
        actual=str(left_total),
        expected=str(right_total),
    )


def _column_equals_constant(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    max_evidence: int,
    target: str,
    expected: Any,
) -> OperatorResult:
    """某列逐行须等于给定值（如 HIS 账期码须与本次扫描账期一致）。"""
    if expected is None:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"column_equality 口径 {chk.display_name} 写了 compare_column"
            f"（{target}）但没写 compare_value",
        )
    want = str(expected)
    hits = [
        evidence_of(row, target, detail=f"本行取值为「{row.get(target) or '（空）'}」，应为「{want}」")
        for row in rows
        if str(row.get(target) or "").strip() != want
    ]
    return build_result(
        chk,
        "column_equality",
        hits,
        max_evidence,
        fail_msg=lambda n, m: f"列 {target} 有 {n} 行不等于「{want}」" + trunc_note(n, m),
        pass_msg=f"列 {target} 全部等于「{want}」（{len(rows)} 行）",
        expected=want,
    )


# —— 执行入口 ——————————————————————————————————————————————


def check_operator(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int = DEFAULT_MAX_EVIDENCE,
    ctx: Optional[CheckContext] = None,
) -> OperatorResult:
    """按 ``chk.type`` 从注册表取算子执行；未注册则显式报错。

    未注册算子**不静默当 pass**：一条没跑的口径在看板上与「跑过并通过」
    完全一样，财务科据此签字就会放过一批没核过的账。
    """
    handler = get_operator(chk.type)
    if handler is None:
        raise ReconqError(
            "CHECK_OPERATOR_UNSUPPORTED",
            f"口径 {chk.display_name} 使用的算子 {chk.type} 未注册；"
            f"本产品已注册：{', '.join(operator_names())}；"
            f"不允许把未实现的算子静默当作 pass",
        )
    return handler(rows, chk, column, max_evidence, ctx or CheckContext())
