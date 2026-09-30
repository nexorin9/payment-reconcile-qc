"""对账口径 check 执行器（Task 3 主干 + Task 14 行级口径 + Task 15 算子库接线）。

源产品主路径：``soda contract verify`` 读一份 YAML 契约 → 逐 check 执行 →
每条 check 产出「pass/fail + 失败行证据」→ 汇总后用退出码做门禁
（参考 ``github_ref/soda-core/soda_core/cli/cli.py:832``
``exit_with_code()``；入口装配见同文件 ``:144``
``_setup_contract_verify_command()``，check 选择器见 ``:212-245``，
变量注入见 ``:296`` ``_parse_variables()``）。本产品把同一主路径搬到六路
渠道账单上：``contracts/<channel>.yml`` 的每条 check 就是一条对账口径，
执行结果带**证据行号与差额**，退出码非零即门禁。

算子按「是否需要账期侧上下文」分两层：

- :mod:`reconq.engine.operators` —— **可组合断言算子库**（Task 15）。
  非空 / 枚举 / 精度 / 区间 / 唯一性 / 正则 / 跨列等式与不等式 / 集合包含，
  全部注册在 ``OPERATORS`` 表里，阈值只来自契约 YAML。**新增一条对账口径
  只需改 YAML，不改 Python。**
- 本模块 —— **行级对账口径**（Task 14）。笔数、金额合计、跨日切分、
  退费红冲：这四条都要读账期窗口或对照基准，运行期才拿得到，注册在
  ``_ROW_OPERATORS`` 里，与上面的注册表合成同一张分派表。

两层共用 :mod:`reconq.engine.common` 的 :class:`BillRow` /
:class:`Evidence` / :func:`to_decimal`，避免同一套取值判据在两处漂移。

医保两条勾稽等式的依据是江苏省医保办《关于贯彻落实国家医疗保障局办公室
修订〈医疗保障基金结算清单〉〈医疗保障基金结算清单填写规范〉的通知》
（苏医保办发〔2021〕38 号）修订要点第四条：基金支付类型细分为「医保统筹
基金支付」「补充医疗保险支付」「医疗救助支付」，并明确收费信息勾稽关系
**金额合计 = 医保统筹 + 补充医疗 + 医疗救助 + 个人负担，
个人负担 = 其他支付 + 个人支付**。

未注册的算子显式报 ``CHECK_OPERATOR_UNSUPPORTED``，**不静默当成 pass**。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional, Sequence

from ..contracts.model import CheckYaml, ColumnYaml, ContractYaml
from ..errors import ReconqError
from . import operators as _operators
from .common import (
    DEFAULT_MAX_EVIDENCE,
    BillRow,
    CheckContext,
    CheckResult,
    Evidence,
    build_result,
    evidence_of,
    matches_where,
    target_column,
    to_decimal,
    trunc_note,
    where_desc,
)
# ``OperatorResult`` / ``check_operator`` 在此重导出：调用方（cli、测试）按
# 算子库的名字引用它们，不必知道分层就多写一个 import。
from .operators import (
    Operator,
    OperatorResult,
    check_operator,
)

__all__ = [
    "Evidence",
    "OperatorResult",
    "check_operator",
    "CheckResult",
    "BillRow",
    "CheckContext",
    "run_checks",
    "matches_where",
    "SUPPORTED_OPERATORS",
    "row_operator_names",
]

#: 行级口径注册表。与 :data:`reconq.engine.operators.OPERATORS` 合成
#: 同一张分派表 —— 调用方看不出某个算子住在哪个模块，新增口径也不必
#: 在 run_checks 里加一条 if 分支。实际填充在本模块**末尾**（须等四个
#: handler 定义完成）。
_ROW_OPERATORS: dict[str, Operator] = {}


def row_operator_names() -> tuple[str, ...]:
    """行级口径算子名（需要账期上下文的四条）。"""
    return tuple(sorted(_ROW_OPERATORS))


def _supported_operators() -> tuple[str, ...]:
    """本轮支持的算子全集 = 可组合算子库 + 行级口径。

    命令行 ``--check`` 的默认值与「本轮支持哪些算子」的提示都以它为准；
    未列出的算子会在执行时显式报错，不会静默通过。
    """
    return tuple(sorted(set(_operators.operator_names()) | set(_ROW_OPERATORS)))


# —— 执行入口 ——————————————————————————————————————————————


def run_checks(
    rows: Sequence[BillRow],
    contract: ContractYaml,
    *,
    selected: Optional[Iterable[CheckYaml]] = None,
    max_evidence: int = DEFAULT_MAX_EVIDENCE,
    context: Optional[CheckContext] = None,
) -> list[CheckResult]:
    """按契约逐条执行 check，返回带证据的结果集。

    ``selected`` 为 None 时跑契约声明的全部 check；给了则只跑该子集
    （由 :func:`reconq.contracts.selector.filter_checks` 产出，保持原顺序）。

    分派走**同一张表** ``_ROW_OPERATORS`` + ``operators.OPERATORS``：
    算子库里的算子与本模块的行级口径对调用方没有区别，新增口径不需要
    在这里加一条 if 分支。

    ``context`` 供跨日切分等需要账期侧上下文的口径使用；不给时这些口径
    显式报 ``CHECK_PATH_INVALID``（缺 ``period``），**不拿「今天」当账期
    蒙混过去** —— 拿错账期窗口判跨日切分，结论 100% 是错的而且看不出来。
    """
    wanted: Optional[list[CheckYaml]] = None if selected is None else list(selected)
    wanted_set = {id(c) for c in wanted} if wanted is not None else None
    ctx = context or CheckContext()

    results: list[CheckResult] = []
    for chk, column in contract.iter_checks():
        if wanted_set is not None and id(chk) not in wanted_set:
            continue
        handler = _ROW_OPERATORS.get(chk.type) or _operators.get_operator(chk.type)
        if handler is None:
            raise ReconqError(
                "CHECK_OPERATOR_UNSUPPORTED",
                f"口径 {chk.display_name}（type={chk.type}）尚未实现，"
                f"本轮只支持 {_supported_operators()}；"
                f"不允许把未实现的算子静默当作 pass",
                channel=contract.channel,
            )
        results.append(handler(rows, chk, column, max_evidence, ctx))
    return results


# —— 行级对账口径（需要账期窗口 / 对照基准）—————————————————————
#
# 这四条与 operators.py 里的断言算子不同：它们要么要账期时间窗
# （跨日切分、退费红冲），要么要 HIS 侧对照基准（金额合计）。把它们留在
# checks.py 是为了让算子库保持「纯列级断言、无运行期状态」的形状。


def _run_row_count(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> CheckResult:
    """笔数口径：支持 ``min`` / ``max`` / ``exact`` 与 ``where`` 过滤。

    「本期一笔都没有」和「本期 300 笔」是两种完全不同的业务事实，都要能在
    门禁上分得开 —— 账单没导出成功时笔数为 0，若口径只判下限不判 0，
    就会把「没取到数据」说成「一切正常」。
    """
    matched = [r for r in rows if matches_where(r, chk.properties.get("where"))]
    where_txt = where_desc(chk)

    low = chk.properties.get("min")
    high = chk.properties.get("max")
    exact = chk.properties.get("exact")
    count = len(matched)
    bound = []
    if exact is not None:
        bound.append(f"须为 {exact} 笔")
    if low is not None:
        bound.append(f"须 ≥ {low} 笔")
    if high is not None:
        bound.append(f"须 ≤ {high} 笔")

    if not bound:
        # 无边界就是纯计数口径：报个数，不判成败。
        return CheckResult(
            check_path=chk.path_str,
            check_name=chk.display_name,
            operator="row_count",
            status="pass",
            message=f"笔数{where_txt} {count} 笔（全表 {len(rows)} 行）",
            actual=str(count),
            evidence_total=count,
        )

    ok = True
    if exact is not None and count != int(exact):
        ok = False
    if low is not None and count < int(low):
        ok = False
    if high is not None and count > int(high):
        ok = False

    hits: list[Evidence] = []
    if not ok:
        # 笔数越界时，证据是「多出来的行」或「整表不足」——把可定位的行指出来，
        # 而不是只丢一个数字。低于下限时没有「多出来的行」，证据退为首行锚点。
        if high is not None and count > int(high):
            anchor = matched[int(high):]
        else:
            anchor = matched[:1]
        for row in anchor[:max_evidence]:
            hits.append(
                evidence_of(
                    row,
                    str(chk.path[1]) if len(chk.path) > 1 else "",
                    detail=f"本行计入笔数核对（实际 {count} 笔，{'、'.join(bound)}）",
                )
            )
    return build_result(
        chk,
        "row_count",
        hits,
        max_evidence,
        fail_msg=lambda n, m: f"笔数{where_txt} {count} 笔，{'、'.join(bound)} 不满足",
        pass_msg=f"笔数{where_txt} {count} 笔，满足{'、'.join(bound)}",
        actual=str(count),
        expected="、".join(bound),
        force_status=None if ok else "fail",
    )


def _run_decimal_sum(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> CheckResult:
    """金额合计：一律 Decimal；给了 ``expected`` 或对照基准则比差额。

    ``where`` / ``exclude_where`` 支持等值与布尔过滤（退费红冲行单列、
    成功交易合计正是靠 ``where: {trade_state: SUCCESS}`` 切出来的）。

    差额的证据只列**真正贡献了金额**的行：把 20 行全列出来等于没指认。
    参与行全为零而合计对不上时（多半是口径配错列），退回列参与行首行并
    在 detail 里点明。
    """
    target = str(chk.properties.get("column") or "")
    if not target:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"decimal_sum 口径 {chk.display_name} 缺少 column 属性（要合计哪一列）",
        )
    matched = [
        r
        for r in rows
        if matches_where(r, chk.properties.get("where"))
        and not (
            chk.properties.get("exclude_where")
            and matches_where(r, chk.properties["exclude_where"])
        )
    ]

    total = Decimal("0")
    contributing: list[BillRow] = []
    for row in matched:
        value = to_decimal(row.get(target), row, target, chk)
        total += value
        if value != 0:
            contributing.append(row)

    actual = str(total)
    where_txt = where_desc(chk)
    expected_raw = chk.properties.get("expected")
    if expected_raw is None:
        # 没写死 expected 时看运行期对照基准（典型是 HIS 侧同期收费合计）。
        baseline = ctx.compare_baselines.get(chk.path_str)
        if baseline is not None:
            expected_raw = str(baseline)
            source = f"HIS 侧对照基准 {baseline}"
        else:
            return CheckResult(
                check_path=chk.path_str,
                check_name=chk.display_name,
                operator="decimal_sum",
                status="pass",
                message=(
                    f"{target} 合计 {actual} 元{where_txt}（{len(matched)} 行参与，"
                    f"本期未提供对照基准，只报合计不判定差额）"
                ),
                actual=actual,
                evidence_total=len(matched),
            )
    else:
        source = "契约 expected"

    exp = to_decimal(expected_raw, None, target, chk)
    tolerance = to_decimal(chk.properties.get("tolerance", "0.01"), None, target, chk)
    diff = total - exp
    status = "pass" if abs(diff) <= tolerance else "fail"
    hits: list[Evidence] = []
    if status == "fail":
        targets = contributing or matched[:1]
        only_zero = not contributing
        for row in targets[:max_evidence]:
            hits.append(
                evidence_of(
                    row,
                    target,
                    detail=(
                        f"本行 {target}={row.get(target) or '（空）'}，"
                        + (
                            "参与行金额全为 0，合计对不上多半是口径配错了列"
                            if only_zero
                            else "计入合计差额"
                        )
                    ),
                )
            )
    message = (
        f"{target} 合计 {actual} 元，{source} {exp} 元，差额 {diff} 元"
        f"（容差 {tolerance}，{len(matched)} 行参与）"
        if status == "fail"
        else f"{target} 合计 {actual} 元，与{source} {exp} 元一致（容差 {tolerance}）"
    )
    return CheckResult(
        check_path=chk.path_str,
        check_name=chk.display_name,
        operator="decimal_sum",
        status=status,
        message=message,
        evidence=hits,
        diff_amount=str(diff),
        actual=actual,
        expected=str(exp),
        evidence_total=len(matched),
        truncated=len(targets) > max_evidence if status == "fail" else False,
    )


def _run_cross_period(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> CheckResult:
    """跨日切分：交易时间须落在本次账期窗口内。

    账期窗口取自 ``context.period`` 与 ``context.session_timezone``（Task 4
    的 ``parse_session_timezone``），**半开区间** ``[周一 00:00, 下周一
    00:00)``：周一零点那笔属于新账期。

    判定建立在**归一时区**上：渠道账单给的是本地时间，HIS 给的是库时区。
    不先归一就比大小，跨日那批行会被算进错误的一期，差额看着合理、实际全错。

    证据给**实际落点**：该行交易时间实际落在哪一天、由哪个账期窗口收，
    财务科据此判断是「导出方切错了日界」还是「真有一笔跨期交易」。
    """
    from ..store.bill_store import parse_trade_time  # 延迟导入：避免与 store 循环依赖
    from ..sources.timezone import period_window, to_session_tz

    target = str(chk.properties.get("column") or target_column(chk, column))
    if not ctx.period:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"跨日切分口径 {chk.display_name} 需要本次扫描的账期，"
            f"但运行上下文未提供 period；拒绝用「今天」当账期判定，"
            f"请通过 --period 指定",
        )
    tz = to_session_tz(ctx.session_timezone or chk.properties.get("timezone_variable"))
    window = period_window(ctx.period, tz)

    hits: list[Evidence] = []
    for row in rows:
        raw = str(row.get(target) or "").strip()
        if not raw:
            hits.append(evidence_of(row, target, detail="交易时间为空，无法判定是否落在账期内"))
            continue
        moment = parse_trade_time(
            raw,
            tz=window.tz,
            channel=str(chk.properties.get("channel") or ""),
            row_ref=f"{row.source_file}:{row.row_no}",
        )
        if moment is None:
            hits.append(evidence_of(row, target, detail="交易时间无法解析"))
            continue
        if window.is_cross_period(moment):
            local = window.localize(moment)
            hits.append(
                evidence_of(
                    row,
                    target,
                    detail=(
                        f"交易时间 {local:%Y-%m-%d %H:%M:%S}（{window.tz}）"
                        f"落在账期 {ctx.period} 窗口 "
                        f"[{window.start:%Y-%m-%d %H:%M}, {window.end:%Y-%m-%d %H:%M}) 之外"
                    ),
                )
            )
    return build_result(
        chk,
        "cross_period",
        hits,
        max_evidence,
        fail_msg=lambda n, m: (
            f"有 {n} 行交易时间落在账期 {ctx.period} 窗口之外"
            f"（窗口 [{window.start:%Y-%m-%d %H:%M}, {window.end:%Y-%m-%d %H:%M})，"
            f"时区 {window.tz}）" + trunc_note(n, m)
        ),
        pass_msg=(
            f"全部 {len(rows)} 行交易时间落在账期 {ctx.period} 窗口内"
            f"（[{window.start:%Y-%m-%d %H:%M}, {window.end:%Y-%m-%d %H:%M})）"
        ),
    )


def _run_refund_offset(
    rows: Sequence[BillRow],
    chk: CheckYaml,
    column: Optional[ColumnYaml],
    max_evidence: int,
    ctx: CheckContext,
) -> CheckResult:
    """退费红冲：退费必须挂得住原单，且金额不得反向。

    两种账形态，配对判据**完全不同**，混为一谈会把正常的退费报成异常：

    **A. 行内退费（inline）** —— 渠道把退款金额记在**原单行**的退款列上，
    微信/银联账单即是：一行同时有订单金额 112.40 与退款金额 86.00。
    此时「退费行」= 该列金额非零的行，它要配对的正向行就是它**自己**
    （原单就在本行）。判据是：退款额 ≤ 原单金额，且原单不是未支付/已关闭
    状态（未支付的单不存在退款）。不按这个判，就会把每一笔正常退费都报成
    「单边退费」——口径报假警，财务科第一次跑就会把这套工具判死刑。

    **B. 独立红冲行（separate）** —— 院内导出用「是否退费」标记行，
    HIS 红冲、自助机退费即是：退费行与原单行是**两行**、同号。
    此时判据是：该行单号必须在本表内找到同号且非退费的另一行；
    找不到就是单边红冲（HIS 记了退费、渠道账单里没有，或反过来）。

    两种形态都额外校验金额方向：退费额不得大于原单额 ——「退 200、原单
    100」无论哪种形态都是账错了。
    """
    pair_column = str(chk.properties.get("pair_column") or "")
    if not pair_column:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"refund_offset 口径 {chk.display_name} 缺少 pair_column"
            f"（退费行与哪一列的同号正向行配对）",
        )
    amount_column = str(chk.properties.get("column") or "")
    where = chk.properties.get("where")
    mode = "separate" if where else "inline"
    if not amount_column and not where:
        raise ReconqError(
            "CHECK_PATH_INVALID",
            f"refund_offset 口径 {chk.display_name} 既没有 where，"
            f"也没写 column，退费行无从判定",
        )

    if where:

        def is_refund(row: BillRow) -> bool:
            return matches_where(row, where)

    else:

        def is_refund(row: BillRow) -> bool:
            return to_decimal(row.get(amount_column), row, amount_column, chk) != 0

    # 一次遍历分成两组。判据是谓词而不是 ``r not in refund_rows`` ——
    # BillRow 按值相等，同号同额的两行会被当成同一行，正向行凭空少一条，
    # 配对随即误报「单边红冲」。
    refund_rows: list[BillRow] = []
    normal_rows: list[BillRow] = []
    for row in rows:
        (refund_rows if is_refund(row) else normal_rows).append(row)

    hits: list[Evidence] = []

    if mode == "separate":
        by_order: dict[str, list[BillRow]] = {}
        for row in normal_rows:
            key = str(row.get(pair_column) or "").strip()
            if key:
                by_order.setdefault(key, []).append(row)
        for row in refund_rows:
            key = str(row.get(pair_column) or "").strip()
            if not key:
                hits.append(
                    evidence_of(row, pair_column, detail="退费/红冲行没有单号，无法与原单配对")
                )
                continue
            pairs = by_order.get(key) or []
            if not pairs:
                hits.append(
                    evidence_of(
                        row,
                        pair_column,
                        detail=(
                            f"单边红冲：单号 {key} 在本表内找不到同号的正向行"
                            f"（{pair_column} 同号且非退费），需与 HIS 侧红冲记录核对"
                        ),
                    )
                )
                continue
            if amount_column:
                refund_amt = abs(to_decimal(row.get(amount_column), row, amount_column, chk))
                orig_amt = abs(to_decimal(pairs[0].get(amount_column), pairs[0], amount_column, chk))
                if refund_amt > orig_amt:
                    hits.append(
                        evidence_of(
                            row,
                            amount_column,
                            detail=(
                                f"退费金额 {refund_amt} 元大于同号正向行金额 {orig_amt} 元"
                                f"（配对行 {pairs[0].source_file}:{pairs[0].row_no}）"
                            ),
                        )
                    )
    else:
        # 行内退费：原单就在本行，配对对象是本行自身的订单金额列
        order_column = str(chk.properties.get("order_amount_column") or "amount")
        dead_states = {str(s) for s in (chk.properties.get("dead_states") or [])}
        for row in refund_rows:
            refund_amt = abs(to_decimal(row.get(amount_column), row, amount_column, chk))
            order_amt = to_decimal(row.get(order_column), None, order_column, chk)
            if order_amt == 0:
                hits.append(
                    evidence_of(
                        row,
                        order_column,
                        detail=(
                            f"行内退费 {refund_amt} 元，但本行原单金额列 "
                            f"{order_column} 为 0/空，退费挂不到原单上"
                        ),
                    )
                )
                continue
            if refund_amt > abs(order_amt):
                hits.append(
                    evidence_of(
                        row,
                        amount_column,
                        detail=(
                            f"退费金额 {refund_amt} 元大于本行原单金额 "
                            f"{abs(order_amt)} 元，金额方向反了"
                        ),
                    )
                )
                continue
            if dead_states:
                state = str(row.get("trade_state") or row.get("income_expense") or "").strip()
                if state in dead_states:
                    hits.append(
                        evidence_of(
                            row,
                            amount_column,
                            detail=(
                                f"本行状态为「{state}」（{'、'.join(sorted(dead_states))}），"
                                f"这类状态不存在退款，退费行 {refund_amt} 元存疑"
                            ),
                        )
                    )

    desc = f"退费/红冲 {len(refund_rows)} 行，正向 {len(normal_rows)} 行"
    mode_desc = "行内退费（配对本行原单）" if mode == "inline" else "独立红冲行（配对同号正向行）"
    return build_result(
        chk,
        "refund_offset",
        hits,
        max_evidence,
        fail_msg=lambda n, m: (
            f"退费红冲配对有 {n} 处异常（{mode_desc}，{desc}）" + trunc_note(n, m)
        ),
        pass_msg=f"退费红冲逐笔挂得住原单且金额方向正确（{mode_desc}，{desc}）",
    )


# —— 行级口径注册（须在本模块所有 handler 定义之后）——————————
_ROW_OPERATORS.update(
    {
        "row_count": _run_row_count,
        "decimal_sum": _run_decimal_sum,
        "cross_period": _run_cross_period,
        "refund_offset": _run_refund_offset,
    }
)

#: 本轮已实现的对账口径算子全集（模块导入时定值，供 cli 与自检读取）。
SUPPORTED_OPERATORS = _supported_operators()
