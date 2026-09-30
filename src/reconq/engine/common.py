"""算子与执行器共用的数据件与取值判据。

拆出这一层是为了让依赖方向单向：

``common``（本文件，只有类型与纯判据）
→ ``operators``（可组合断言算子库，Task 15）
→ ``checks``（对账口径算子 + check 执行入口）

``checks`` 里的行级算子（笔数 / 金额合计 / 跨日切分 / 退费红冲）需要账期侧
上下文，因此留在 ``checks``；而**不依赖账期**的列级断言（必填、枚举、精度、
区间、唯一性、正则、跨列等式…）属于可组合算子库，在 ``operators`` 里注册。
两层共用本文件的 :class:`Evidence` / :class:`BillRow` / :class:`CheckResult`
与 :func:`to_decimal`，避免同一套取值判据在两处各写一遍 —— 那正是「金额
判定口径漂移」的来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from ..contracts.model import CheckYaml, ColumnYaml
from ..errors import ReconqError

__all__ = [
    "Evidence",
    "BillRow",
    "CheckResult",
    "CheckContext",
    "DEFAULT_MAX_EVIDENCE",
    "DEFAULT_TOLERANCE",
    "matches_where",
    "to_bool",
    "target_column",
    "evidence_of",
    "where_desc",
    "trunc_note",
    "build_result",
    "to_decimal",
]

#: 单条 check 最多保留的证据行数。超出部分**显式标注** truncated 与总数，
#: 静默截断会让财务科以为「就这 3 行有问题」。
DEFAULT_MAX_EVIDENCE = 200

#: 金额比较的默认容差（元）。逐条口径可用 ``tolerance`` 覆盖。
DEFAULT_TOLERANCE = Decimal("0.01")

#: 院内导出里「是否退费 / 是否红冲」这类布尔列的常见写法。
#: 契约里写 ``is_refund: true``，导出文件里可能是「是」「否」「Y」「1」……
#: 不做归一就会让退费红冲口径**一条都命中不了**且照样报 pass ——
#: 这是「口径看起来在跑、其实从没生效」最隐蔽的一种。
_TRUE_TOKENS = frozenset({"是", "true", "t", "yes", "y", "1", "有", "红冲", "退费"})
_FALSE_TOKENS = frozenset({"否", "false", "f", "no", "n", "0", "无", "正常"})


@dataclass(frozen=True)
class Evidence:
    """一条证据：出问题的源行定位。

    必须能回到「哪个文件、第几行、原始行是什么、哪个字段、值是多少」——
    财务科拿着这个才能回查导出方。定位不到行的结论不得作为 fail 依据。
    """

    source_file: str
    row_no: int
    raw_line: str
    field_name: str = ""
    value: str = ""
    detail: str = ""

    def render(self) -> str:
        where = f"{self.source_file}:{self.row_no}"
        head = f"{where} 列 {self.field_name}={self.value or '（空）'}" if self.field_name else f"{where} {self.raw_line[:120]}"
        return f"{head} —— {self.detail}" if self.detail else head

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "row_no": self.row_no,
            "raw_line": self.raw_line,
            "field": self.field_name,
            "value": self.value,
            "detail": self.detail,
        }


@dataclass
class BillRow:
    """一行账单。``values`` 的键是**契约列名**（已完成表头归一）。

    保留 ``row_no``（源文件行号，1 基，含表头偏移）与 ``raw_line``（原始行），
    使任何一条 check 结论都能回到源行。
    """

    values: dict[str, str]
    row_no: int
    raw_line: str
    source_file: str = ""

    def get(self, column: str, default: str = "") -> str:
        return self.values.get(column, default)


@dataclass
class CheckResult:
    """一条 check 的执行结果。"""

    check_path: str
    check_name: str
    operator: str
    status: str  # pass | fail
    message: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    diff_amount: Optional[str] = None
    actual: Optional[str] = None
    expected: Optional[str] = None
    truncated: bool = False
    evidence_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_path": self.check_path,
            "check_name": self.check_name,
            "operator": self.operator,
            "status": self.status,
            "message": self.message,
            "evidence": [e.to_dict() for e in self.evidence],
            "diff_amount": self.diff_amount,
            "actual": self.actual,
            "expected": self.expected,
            "truncated": self.truncated,
            "evidence_total": self.evidence_total,
        }


@dataclass(frozen=True)
class CheckContext:
    """执行 check 需要的账期侧上下文。

    契约里的 ``${period}`` 在加载期已被替换成账期值，但**跨日切分**这条
    口径要在运行期才知道「本次扫的是哪一期、账期时区是哪边」——同一份契约
    服务多个账期，这两个值随每次扫描变，不能烤进契约。

    ``compare_baselines`` 是**渠道侧金额合计的对照基准**（典型是 HIS 侧
    同期收费合计）。``decimal_sum`` 口径若未写死 ``expected``，但这里给了
    对照基准，就比差额并给证据行；两者都没有时只报合计，不判定成败 ——
    「报了个总数」与「核过了没问题」是两回事，不能混。
    """

    period: str = ""
    session_timezone: Optional[str] = None
    compare_baselines: Mapping[str, Decimal] = field(default_factory=dict)


def matches_where(row: BillRow, where: Optional[Mapping[str, Any]]) -> bool:
    """判断一行是否命中 ``where`` 过滤条件。

    布尔值做**归一比较**而非字符串比较：契约写 ``is_refund: true``，
    院内导出写「是」「1」「Y」都应算命中。直接比字符串会让红冲口径
    一条都命中不了，却仍报「0 条退费、合计 0 元、通过」——口径形同虚设。
    """
    if not where:
        return True
    for key, expected in where.items():
        actual = str(row.get(key) or "").strip()
        if isinstance(expected, bool):
            flag = to_bool(actual)
            if flag is None:
                return False
            if flag is not expected:
                return False
        elif actual != str(expected):
            return False
    return True


def to_bool(text: str) -> Optional[bool]:
    """把院内导出的布尔写法归一成三态；认不出返回 None（不当成 False）。"""
    lowered = text.strip().lower()
    if lowered in _TRUE_TOKENS:
        return True
    if lowered in _FALSE_TOKENS:
        return False
    return None


def target_column(chk: CheckYaml, column: Optional[ColumnYaml]) -> str:
    """取这条 check 针对的列：显式 ``column`` 优先，否则用 path 首段。"""
    target = str(chk.properties.get("column") or "")
    if target:
        return target
    if column is not None:
        return column.name
    if chk.path:
        return str(chk.path[0])
    raise ReconqError(
        "CHECK_PATH_INVALID",
        f"口径 {chk.display_name} 未指明检查哪一列",
    )


def evidence_of(
    row: BillRow,
    field_name: str,
    *,
    detail: str = "",
    value: Optional[str] = None,
) -> Evidence:
    """构造一条证据 —— 算子**只能**经由本函数落证据。

    收口在此的理由：每条证据都必须带齐 (source_file, row_no, raw_line,
    field, value) 五元组，缺一项这条结论就无法回查源文件。凡是无法定位到
    行的结论，不得作为 fail 依据。``value`` 默认取该行该列的原始值，
    需要给出别的值（如归一后的判定结果）时显式传入。
    """
    return Evidence(
        source_file=row.source_file,
        row_no=row.row_no,
        raw_line=row.raw_line,
        field_name=field_name,
        value=(str(row.get(field_name) or "").strip() if value is None else value),
        detail=detail,
    )


def where_desc(chk: CheckYaml) -> str:
    parts = []
    if chk.properties.get("where"):
        parts.append("筛选 " + "、".join(f"{k}={v}" for k, v in chk.properties["where"].items()))
    if chk.properties.get("exclude_where"):
        parts.append("剔除 " + "、".join(f"{k}={v}" for k, v in chk.properties["exclude_where"].items()))
    return f"（{'，'.join(parts)}）" if parts else ""


def trunc_note(total: int, kept: int) -> str:
    return f"（证据已截断，共 {total} 条，此处列前 {kept} 条）" if total > kept else ""


def build_result(
    chk: CheckYaml,
    operator: str,
    hits: list[Evidence],
    max_evidence: int,
    *,
    fail_msg,
    pass_msg: str,
    diff_amount: Optional[str] = None,
    actual: Optional[str] = None,
    expected: Optional[str] = None,
    force_status: Optional[str] = None,
) -> CheckResult:
    """统一收口：截断证据 + 生成 CheckResult。

    证据超过 ``max_evidence`` 时**显式标注 truncated 与总数**，不静默截断 ——
    截断而不说，财务科会以为「就这 3 行有问题」。
    """
    total = len(hits)
    kept = hits[:max_evidence]
    status = force_status or ("fail" if total else "pass")
    message = fail_msg(total, max_evidence) if status == "fail" else pass_msg
    return CheckResult(
        check_path=chk.path_str,
        check_name=chk.display_name,
        operator=operator,
        status=status,
        message=message,
        evidence=kept,
        evidence_total=total,
        truncated=total > max_evidence,
        diff_amount=diff_amount,
        actual=actual,
        expected=expected,
    )


def to_decimal(
    raw: Any,
    row: Optional[BillRow],
    column: str,
    chk: CheckYaml,
) -> Decimal:
    """把账单里的金额字符串转 Decimal；非数字一律显式报错并给行号。

    **禁用 float**：金额一律定点，``0.1 + 0.2 != 0.3`` 在对账里就是错账。
    """
    if raw is None:
        if row is None:
            return Decimal("0")
        raise ReconqError(
            "AMOUNT_UNPARSABLE",
            "金额列为空，无法计入合计",
            row_ref=f"{row.source_file}:{row.row_no}",
        )
    text = str(raw).strip().replace(",", "").replace("¥", "").replace("￥", "")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ReconqError(
            "AMOUNT_UNPARSABLE",
            f"列 {column} 的值 {raw!r} 不是合法金额（口径 {chk.display_name}）",
            row_ref=(f"{row.source_file}:{row.row_no}" if row else None),
        ) from exc
