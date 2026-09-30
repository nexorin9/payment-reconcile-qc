"""差异处置单状态机与三角色确认流（Task 43 · 第二半）。

本模块是**唯一**能推进 :data:`reconq.ledger.models.Discrepancy.status`
的入口。四条纪律各有代价，代价都写在对应函数的注释里：

1. **只接受人工操作人**
   :func:`assert_human_operator` 在入口就拒掉 ``model`` / ``rule`` /
   ``baseline`` / ``系统``。与 Task 30「模型降级不得推进台账状态」同一条
   线，但落得更靠前：那条依赖每条调用路径都记得自己是谁，这条把「不是人」
   的取值挡在入口 —— 想推进状态，必须先编出一个像人的操作人，那一步就留下
   痕迹了。

2. **非法跳转被拒并报出可去落点**
   白名单在 :data:`reconq.ledger.models.ALLOWED_TRANSITIONS`，与参考实现
   ``cli.py:787-829`` 的 ``RequestStatus`` 白名单同源。spec 点名的反例是
   ``已确认 → 待归因`` 直跳：省掉的是**经办人写下的结论**，改判必须先
   撤回重办，结论单上要留得住中间那次「撤回」。

3. **并发靠乐观锁，只有一次生效**
   ``version`` 列 + ``UPDATE ... WHERE disc_id=? AND version=?``。两个
   经办人同时点确认时，后者拿到 ``LEDGER_VERSION_CONFLICT`` 而不是把前者
   的结论无声覆盖 —— 覆盖的后果不是「数据错了」，是「谁签的这句话查不
   到了」。

4. **批量逐条成败，单条失败不影响其余**
   与 :func:`reconq.align.aggregate.apply_group_action` 的「整组要么全动
   要么全不动」**刻意相反**：那张单是「一件事」，一组里必然同质；而批量
   处置的面板入口是「把这一屏里能点的都点了」，被点的条目来自不同渠道、
   不同成因、不同状态，本来就不同质。强行绑在一起会造出一个不存在的
   「一件事」，并让一条卡住的把整屏都卡住。逐条返回结果 + 逐条留痕，
   哪条没动、卡在哪、现在能去哪，都答得出来。

失败怎么报：抛异常还是回结果
----------------------------
本模块把失败分成两类，两类的报法不同，理由是**批量场景下只有后者可用**：

* **业务驳回**（状态不允许、岗位不够格、岗位分离、版本冲突、结论缺失）
  —— :func:`_single_transition` **不抛**，而是把 ``error_code`` / ``detail``
  放进 :class:`TransitionOutcome` 返回。spec 验收写的是「非法跳转被拒并
  **返回错误码**」，而批量入口一次要处理几十条：抛异常就得在循环里
  try/except，而漏掉的那一处会让一条被拒的差异把整批打断。返回值这条
  路上漏不掉。
* **入参形状错误**（未登记的动作 / 角色、操作人不是人、单条入口没带版本
  号、金额是 float）—— **抛** :class:`ReconqError`。这几类是**调用方的
  编程错误**：批量里 39 条都过了、第 40 条传了个不存在的动作名，那是
  接线错了，应该立刻炸出来，不该被当成「这一条没动」混进结果里。

一句话：**业务上「这一条办不动」回结果，程序上「你这个调用写错了」抛异常。**

存疑账期为什么要多一道主管
--------------------------
账期扫描实例处于「已存疑」（:data:`reconq.engine.period.STATUS_DISPUTED`）
意味着**有人已经对这期账提出过异议**。这时候若结论单上只有经办与审核两
道签字，那道异议就等于没有被回应。因此 :func:`period_requires_supervisor`
按账期是否存疑把 ``requires_supervisor`` 定死在**开单那一刻** ——
由系统决定，不由审核在通过那一刻挑：让调用方决定就等于让人自己决定要不要
多一道签字。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

from ..errors import ReconqError
from .models import (
    ACTION_SOURCES,
    ACTION_ZH,
    ALLOWED_TRANSITIONS,
    ALL_STATUSES,
    LEDGER_ACTION_TABLE,
    LEDGER_DISCREPANCY_TABLE,
    LEDGER_KEY_SEP,
    LEDGER_VERSION,
    ROLE_ORDER,
    ROLE_ZH,
    STATUS_ALLOWED,
    STATUS_CONFIRMED,
    STATUS_PENDING_ATTRIBUTION,
    STATUS_PENDING_CONFIRM,
    STATUS_PENDING_SUPERVISOR,
    STATUS_REJECTED,
    STATUS_ZH,
    Discrepancy,
    LedgerAction,
    Role,
    _action_value,
    _table_exists,
    allowed_targets,
    can_transition,
    diff_key,
    dumps_or,
    init_ledger_schema,
    parse_amount,
    require_ledger_schema,
    required_role_for,
    row_to_discrepancy,
)

__all__ = [
    "NON_HUMAN_OPERATORS",
    "BatchItemOutcome",
    "BatchOutcome",
    "TransitionOutcome",
    "assert_human_operator",
    "approve",
    "batch_apply",
    "count_by_status",
    "list_actions",
    "list_discrepancies",
    "load",
    "open_discrepancy",
    "period_requires_supervisor",
    "reject",
    "render_batch_zh",
    "render_transition_zh",
    "require",
    "requires_supervisor_for",
    "submit",
    "withdraw",
]

logger = logging.getLogger("reconq.ledger")

#: 不像「人」的操作人。**不**试图判断姓名格式（医院里工号、姓名拼音、
#: 代号都有人用），只挡**确定不是人**的取值。与
#: :data:`reconq.align.aggregate._NON_HUMAN_OPERATORS` 同口径但各自
#: 定义 —— 那份管「能不能推进台账状态」，这份管「能不能在结论单上签字」，
#: 共用一个常量会让两处耦合，将来改一处另一处跟着变。
NON_HUMAN_OPERATORS = frozenset(
    {
        "",
        "-",
        "auto",
        "bot",
        "model",
        "llm",
        "gateway",
        "rule",
        "rules",
        "baseline",
        "system",
        "scheduler",
        "cron",
        "orchestrator",
        "attribution",
        "系统",
        "自动",
        "模型",
        "规则",
        "网关",
        "基线",
        "编排",
        "定时",
    }
)

#: 处置结论的长度上限。留痕表按 512 存。
MAX_CONCLUSION_LEN = 512


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _table_exists_in(conn: sqlite3.Connection, table: str) -> bool:
    return _table_exists(conn, table)


# ─────────────────────────── 操作人 ───────────────────────────


def assert_human_operator(operator: str) -> str:
    """操作人必须是**人**。返回清洗后的操作人。"""
    text = str(operator or "").strip()
    if text.lower() in NON_HUMAN_OPERATORS or text in NON_HUMAN_OPERATORS:
        raise ReconqError(
            "LEDGER_OPERATOR_INVALID",
            f"操作人 {operator!r} 不是自然人标识，本产品拒绝由它推进处置状态："
            f"模型草稿、规则基线与降级路径都不得把差异推成已确认。"
            f"请传入经办人 / 审核人 / 主管的工号或姓名",
        )
    if len(text) > 64:  # pragma: no cover - 面板输入框限长已挡住
        raise ReconqError(
            "LEDGER_OPERATOR_INVALID",
            f"操作人标识过长（{len(text)} 字符）；留痕表按 64 字符存",
        )
    return text


def _role_value(role: Any) -> str:
    raw = role.value if isinstance(role, Role) else str(role or "")
    if raw not in ROLE_ZH:
        raise ReconqError(
            "LEDGER_ROLE_INVALID",
            f"未登记的处置角色 {raw!r}；本产品只有三个："
            f"{'、'.join(f'{k}（{v}）' for k, v in ROLE_ZH.items())}",
        )
    return raw


# ─────────────────────────── 存疑账期 ───────────────────────────


def period_requires_supervisor(conn: sqlite3.Connection, period: str) -> bool:
    """该账期是否**存疑**。存疑 → 本期新开的处置单一律须经主管复核。

    账期实例表没建或压根没扫过时返回 ``False``（不是错：本轮压根没扫过
    账）。但**已存疑**必须返回 ``True`` —— 漏掉这一条的后果是整期账的
    结论单都少一道本该有的签字，而签字单上不会写「本来应该有第三道」。
    """
    scan_table = "period_scans"
    if not _table_exists_in(conn, scan_table):
        return False
    row = conn.execute(
        f"SELECT status FROM {scan_table} WHERE period=?", (period,)
    ).fetchone()
    if row is None:
        return False
    return str(row["status"] or "") == "disputed"


def requires_supervisor_for(conn: sqlite3.Connection, item: Discrepancy) -> bool:
    """这条差异是否须经主管。**行上的标记优先于账期**。

    账期后来才被标成存疑的（有人在这期结账后提出异议），此前已开的处置
    单不会自动多出第三道签字 —— 但那一期的结论单**作废重开**时按存疑算。
    保留行上标记是刻意的：让结论单上「这一条当时要不要主管签」有唯一
    答案，而不是一个随账期状态漂移的复算结果。
    """
    if item.requires_supervisor:
        return True
    return period_requires_supervisor(conn, item.period)


# ─────────────────────────── 读写 ───────────────────────────


def open_discrepancy(
    conn: sqlite3.Connection,
    *,
    period: str,
    channel: str,
    check_path: str,
    order_no: str = "",
    diff_amount: Any = None,
    amount_known: bool = True,
    evidence_rows: Sequence[str] = (),
    cause_draft: Optional[dict[str, Any]] = None,
    supervisor: Optional[bool] = None,
    now: Optional[str] = None,
) -> Discrepancy:
    """开单：把一条差异落成 ``待归因`` 的处置单。

    **幂等**：``disc_key`` 上有唯一索引。同一条差异重复开单**不报错、
    不覆盖**，返回已存在的那一条 —— 面板重复点「加入待办」是常态，
    而「刷新一下页面结果这条被清空了」不是。返回既有单而不是新建单，
    是因为重复开单是同一件事，不是一次新的判断。

    ``supervisor`` 显式传 ``True/False`` 时按给的走（重开历史单、
    编制预演都要绕开账期状态）；``None`` 时按
    :func:`period_requires_supervisor` 现场判一次。
    """
    init_ledger_schema(conn)
    who_status = (
        period_requires_supervisor(conn, period) if supervisor is None else bool(supervisor)
    )
    key = diff_key(period, channel, check_path)
    existing = _select_by_key(conn, key)
    if existing is not None:
        return existing

    stamp = now or _now()
    amount = parse_amount(diff_amount) if amount_known else parse_amount(0)
    conn.execute(
        f"INSERT INTO {LEDGER_DISCREPANCY_TABLE} "
        "(disc_key, ledger_version, period, channel, check_path, order_no, "
        "diff_amount, amount_known, evidence_json, cause_draft_json, status, "
        "requires_supervisor, conclusion, handler, handler_at, reviewer, "
        "reviewer_at, supervisor, supervisor_at, version, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (
            key,
            LEDGER_VERSION,
            period,
            channel,
            check_path,
            order_no or "",
            f"{amount:.2f}",
            1 if amount_known else 0,
            dumps_or(list(evidence_rows), "[]"),
            dumps_or(cause_draft, "null") if cause_draft else None,
            STATUS_PENDING_ATTRIBUTION,
            1 if who_status else 0,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            stamp,
            stamp,
        ),
    )
    logger.info(
        "开处置单：%s / %s / %s（差额 %s，%s）",
        period, channel, check_path,
        f"{amount:.2f}" if amount_known else "未给金额",
        "须主管复核" if who_status else "两级会签",
    )
    return require(conn, key)


def _select_by_key(conn: sqlite3.Connection, key: str) -> Optional[Discrepancy]:
    row = conn.execute(
        f"SELECT * FROM {LEDGER_DISCREPANCY_TABLE} WHERE disc_key=?", (key,)
    ).fetchone()
    return row_to_discrepancy(row) if row is not None else None


def load(conn: sqlite3.Connection, key: str) -> Optional[Discrepancy]:
    """取一条处置单；没有则 ``None``（调用方自己决定要不要报错）。"""
    require_ledger_schema(conn)
    return _select_by_key(conn, key)


def require(conn: sqlite3.Connection, key: str) -> Discrepancy:
    """取一条处置单；没有则**显式报错**。"""
    require_ledger_schema(conn)
    item = _select_by_key(conn, key)
    if item is None:
        raise ReconqError(
            "LEDGER_DISCREPANCY_NOT_FOUND",
            f"处置单 {key!r} 不存在；它得先由 reconq ledger open 开成待办。"
            f"注意「没开单」与「已处理完」在结论单上长得一样 —— "
            f"本产品不接受把前者显示成后者",
        )
    return item


def list_discrepancies(
    conn: sqlite3.Connection,
    *,
    period: str = "",
    channel: str = "",
    status: str = "",
    open_only: bool = False,
    limit: Optional[int] = None,
) -> list[Discrepancy]:
    """列处置单。批量处置的候选集、面板的待办栏都走这里。"""
    require_ledger_schema(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if period:
        clauses.append("period=?")
        params.append(period)
    if channel:
        clauses.append("channel=?")
        params.append(channel)
    if status:
        clauses.append("status=?")
        params.append(status)
    if open_only:
        placeholders = ",".join("?" for _ in STATUS_ALLOWED)
        clauses.append(f"status IN ({placeholders})")
        params.extend(sorted(STATUS_ALLOWED))
    sql = f"SELECT * FROM {LEDGER_DISCREPANCY_TABLE}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    # 稳定排序：同批同状态时顺序不随机，否则「刚才那 3 条」在两次列之间
    # 会换人，逐条返回的结果就没法和屏幕上的行对上。
    sql += " ORDER BY period, channel, check_path"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [row_to_discrepancy(row) for row in conn.execute(sql, params)]


def count_by_status(conn: sqlite3.Connection, period: str = "") -> dict[str, int]:
    """按状态计数。面板三栏与门禁色都读它。"""
    require_ledger_schema(conn)
    sql = f"SELECT status, COUNT(*) AS n FROM {LEDGER_DISCREPANCY_TABLE}"
    params: list[Any] = []
    if period:
        sql += " WHERE period=?"
        params.append(period)
    sql += " GROUP BY status"
    return {
        str(row["status"]): int(row["n"]) for row in conn.execute(sql, params)
    }


# ─────────────────────────── 迁移结果 ───────────────────────────


@dataclass(frozen=True)
class TransitionOutcome:
    """一次迁移的结果。**失败也返回它**（批量场景下成败混在一起）。"""

    key: str
    action: str
    role: str
    operator: str
    before: str
    after: str
    advanced: bool
    version: int
    expected_version: Optional[int] = None
    error_code: str = ""
    detail: str = ""
    note: str = ""
    recorded_at: str = ""

    @property
    def ok(self) -> bool:
        return self.advanced and not self.error_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "ledger_version": LEDGER_VERSION,
            "action": self.action,
            "action_zh": ACTION_ZH.get(self.action, self.action),
            "role": self.role,
            "role_zh": ROLE_ZH.get(self.role, self.role),
            "operator": self.operator,
            "before": self.before,
            "after": self.after,
            "advanced": self.advanced,
            "version": self.version,
            "expected_version": self.expected_version,
            "error_code": self.error_code,
            "detail": self.detail,
            "note": self.note,
            "recorded_at": self.recorded_at,
            "ok": self.ok,
        }


def render_transition_zh(outcome: TransitionOutcome) -> str:
    """一行中文。面板与 CLI 共用。"""
    if outcome.advanced:
        return (
            f"{outcome.key}：{STATUS_ZH.get(outcome.before, outcome.before)} → "
            f"{STATUS_ZH.get(outcome.after, outcome.after)}"
            f"（{ROLE_ZH.get(outcome.role, outcome.role)}{outcome.operator}，"
            f"第 {outcome.version} 版）"
        )
    return (
        f"{outcome.key}：未推进（{outcome.error_code or 'LEDGER_ACTION_DENIED'}）"
        f" —— 当前 {STATUS_ZH.get(outcome.before, outcome.before)}。"
        f"{outcome.detail}"
    )


# ─────────────────────────── 预检 ───────────────────────────


def _assert_status(status: str) -> None:
    if status not in ALL_STATUSES:
        raise ReconqError(
            "LEDGER_STATE_INVALID",
            f"目标状态 {status!r} 不是本产品的合法状态；合法状态："
            f"{'、'.join(STATUS_ZH[s] for s in ALL_STATUSES)}",
        )


def _assert_source(item: Discrepancy, action: str) -> None:
    """来源状态是否允许这个动作。表外组合报出可去落点。"""
    sources = ACTION_SOURCES[action]
    if item.status in sources:
        return
    targets = allowed_targets(item.status)
    hint = f"当前可去：{'、'.join(STATUS_ZH.get(t, t) for t in targets)}" if targets else (
        "当前状态没有出边"
    )
    allowed_actions = [
        ACTION_ZH[a] for a in sorted(ACTION_SOURCES) if item.status in ACTION_SOURCES[a]
    ]
    raise ReconqError(
        "LEDGER_TRANSITION_DENIED",
        f"处置单 {item.key} 当前是「{STATUS_ZH.get(item.status, item.status)}」，"
        f"不能执行「{ACTION_ZH[action]}」（该动作只能从 "
        f"{'、'.join(STATUS_ZH.get(s, s) for s in sorted(sources))} 出发）。"
        f"{hint}；当前能做的动作：{'、'.join(allowed_actions) or '无'}。"
        f"改判必须先撤回重办 —— 结论单上要留得住中间那次撤回",
    )


def _assert_transition(item: Discrepancy, action: str, target: str) -> None:
    """白名单终检。**任何迁移都必须过这一关**。"""
    _assert_status(target)
    if can_transition(item.status, target):
        return
    raise ReconqError(
        "LEDGER_TRANSITION_DENIED",
        f"处置单 {item.key} 不能从「{STATUS_ZH.get(item.status, item.status)}」"
        f"直接跳到「{STATUS_ZH.get(target, target)}」：{hint_for(item.status)}。"
        f"状态迁移走白名单，不是随便设值",
    )


def hint_for(status: str) -> str:
    targets = allowed_targets(status)
    return f"当前可去：{'、'.join(STATUS_ZH.get(t, t) for t in targets)}" if targets else (
        "当前状态没有出边"
    )


def _assert_version(item: Discrepancy, expected_version: Optional[int]) -> None:
    """乐观锁预检。``expected_version`` 为 ``None`` 时**不做检查**。

    ``None`` 存在的理由是批量入口：它拿到的是一个列表，逐条传版本号会
    让调用方多写一层簿记；但**单条入口（面板上的按钮）必须传**，
    :func:`_single_transition` 会在那里强制要求。
    """
    if expected_version is None:
        return
    if int(expected_version) != int(item.version):
        raise ReconqError(
            "LEDGER_VERSION_CONFLICT",
            f"处置单 {item.key} 已被别人改到第 {item.version} 版，"
            f"你手上是第 {expected_version} 版，未做任何改动。"
            f"请刷新后重看 —— 无声覆盖的后果不是「数据错了」，"
            f"是「谁签的这句话查不到了」",
        )


def _assert_separation(item: Discrepancy, action: str, role: str, who: str) -> None:
    """不相容岗位分离：一人不得在同一条处置单上占两岗。"""
    filled = {
        Role.HANDLER.value: item.handler,
        Role.REVIEWER.value: item.reviewer,
        Role.SUPERVISOR.value: item.supervisor,
    }
    for other_role, other_who in filled.items():
        if other_role == role or not other_who:
            continue
        if other_who == who:
            raise ReconqError(
                "LEDGER_ROLE_SEPARATION_DENIED",
                f"处置单 {item.key} 的「{ROLE_ZH[other_role]}」已经是 {other_who}，"
                f"同一人不得再占「{ROLE_ZH[role]}」这一岗 —— 结论单上那三行签字"
                f"必须是三个人。财务科签字的是结论单，而结论单上写着「经办人经办、"
                f"审核人审核」，同一个人占两岗等于那道签字没被赚到",
            )


def _assert_conclusion(item: Discrepancy, conclusion: str) -> str:
    text = str(conclusion or "").strip()
    if not text:
        raise ReconqError(
            "LEDGER_CONCLUSION_REQUIRED",
            f"处置单 {item.key} 提交时必须写下处置结论 —— 结论单上"
            f"「经办人结论」那一栏就是从这里取的，空着的结论单没法签",
        )
    if len(text) > MAX_CONCLUSION_LEN:
        raise ReconqError(
            "LEDGER_CONCLUSION_TOO_LONG",
            f"处置结论 {len(text)} 字符，超过 {MAX_CONCLUSION_LEN} 上限；"
            f"请把要点写进结论、证据行号写进证据栏",
        )
    return text


# ─────────────────────────── 留痕 ───────────────────────────


def _record_action(
    conn: sqlite3.Connection,
    *,
    item: Discrepancy,
    action: str,
    role: str,
    operator: str,
    before: str,
    after: str,
    expected_version: Optional[int],
    version_after: int,
    note: str,
    ok: bool,
    error_code: str,
    detail: str,
    stamp: str,
    batch_id: str = "",
) -> None:
    """追加一条留痕。**成败都写** —— 一条被拒的点击同样要能解释
    「我明明点了确认为什么没动」。"""
    conn.execute(
        f"INSERT INTO {LEDGER_ACTION_TABLE} "
        "(period, disc_key, batch_id, action, role, operator, from_status, "
        "to_status, expected_version, version_before, version_after, note, ok, "
        "error_code, detail, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item.period,
            item.key,
            batch_id,
            action,
            role,
            operator,
            before,
            after,
            expected_version,
            item.version,
            version_after,
            note,
            1 if ok else 0,
            error_code,
            detail,
            stamp,
        ),
    )


def list_actions(
    conn: sqlite3.Connection,
    *,
    key: str = "",
    period: str = "",
    batch_id: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """查留痕。面板「这条经历过什么」与结论单的会签栏都读它。"""
    require_ledger_schema(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if key:
        clauses.append("disc_key=?")
        params.append(key)
    if period:
        clauses.append("period=?")
        params.append(period)
    if batch_id:
        clauses.append("batch_id=?")
        params.append(batch_id)
    sql = f"SELECT * FROM {LEDGER_ACTION_TABLE}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY action_id LIMIT ?"
    params.append(int(limit))
    return [{k: row[k] for k in row.keys()} for row in conn.execute(sql, params)]


# ─────────────────────────── 迁移执行 ───────────────────────────


def _single_transition(
    conn: sqlite3.Connection,
    item: Discrepancy,
    *,
    action: Any,
    role: Any,
    operator: str,
    target: str,
    expected_version: Optional[int],
    note: str = "",
    conclusion: str = "",
    stamp: Optional[str] = None,
    batch_id: str = "",
) -> TransitionOutcome:
    """执行一次迁移。**预检全过才开事务**，预检不过则连事务都不开。

    这样写而不是「先开事务再预检、失败回滚」的理由有两个：一是避免
    一个被拒的点击在库上留下一段锁与一个空洞的 undo 链；二是失败路径
    **仍然要留痕**，而留痕本身要写库 —— 分成「预检 → 留痕 → 提交」三段
    比「开事务 → 抛错 → 回滚 → 再开事务写留痕」少一次事务往返，也不会
    出现「留痕写了但状态回滚了」这种顺序倒置。
    """
    act = _action_value(action)
    rl = _role_value(role)
    who = assert_human_operator(operator)
    now_stamp = stamp or _now()

    # —— 预检（不写库）——
    # **顺序有意义**：版本号先查。拿着旧版本号的人看到的是「这条已经在
    # 已确认上了」，报「状态不允许」会让他以为自己操作错了；报
    # 「你手上是第 0 版、现在是第 1 版」才指向真正的原因 —— 他看到的
    # 已经不是最新那条了。版本 staleness 是其余所有判据的前提。
    try:
        _assert_version(item, expected_version)
        _assert_source(item, act)
        _assert_role_quiet(item, act, rl, who)
        _assert_transition(item, act, target)
        text = _assert_conclusion(item, conclusion) if act == LedgerAction.SUBMIT.value else ""
    except ReconqError as exc:
        _record_action(
            conn,
            item=item,
            action=act,
            role=rl,
            operator=who,
            before=item.status,
            after=item.status,
            expected_version=expected_version,
            version_after=item.version,
            note=note,
            ok=False,
            error_code=exc.code,
            detail=exc.detail or str(exc),
            stamp=now_stamp,
            batch_id=batch_id,
        )
        return TransitionOutcome(
            key=item.key,
            action=act,
            role=rl,
            operator=who,
            before=item.status,
            after=item.status,
            advanced=False,
            version=item.version,
            expected_version=expected_version,
            error_code=exc.code,
            detail=exc.detail or str(exc),
            note=note,
            recorded_at=now_stamp,
        )

    # —— 写入：乐观锁 UPDATE ——
    new_version = item.version + 1
    sets = ["status=?", "version=?", "updated_at=?"]
    params: list[Any] = [target, new_version, now_stamp]
    if act == LedgerAction.SUBMIT.value:
        sets += ["conclusion=?", "handler=?", "handler_at=?"]
        params += [text, who, now_stamp]
    elif rl == Role.REVIEWER.value:
        sets += ["reviewer=?", "reviewer_at=?"]
        params += [who, now_stamp]
    elif rl == Role.SUPERVISOR.value:
        sets += ["supervisor=?", "supervisor_at=?"]
        params += [who, now_stamp]
    # WHERE 的占位顺序必须与 SQL 里写的顺序一致：先 ``disc_key`` 再
    # ``version``。写反了不会报错 —— 键匹配不上任何行，UPDATE 静默落空，
    # 于是每一次迁移都被自己判成「版本冲突」。
    params += [item.key, item.version]
    cursor = conn.execute(
        f"UPDATE {LEDGER_DISCREPANCY_TABLE} SET {', '.join(sets)} "
        f"WHERE disc_key=? AND version=?",
        params,
    )
    # ``rowcount == 0`` 是**乐观锁是否生效的唯一判据**：预检与写入之间
    # 另一个连接可能已经推了一版。落空 = 冲突，**不写「已推进」的留痕**，
    # 只写一条 ``ok=0`` 的冲突记录。
    if cursor.rowcount == 0:
        current = _version_of(conn, item.key)
        detail = (
            f"处置单 {item.key} 已被别人改到第 {current} 版，"
            f"你手上是第 {item.version} 版，未做任何改动。"
            f"请刷新后重看 —— 无声覆盖的后果不是「数据错了」，"
            f"是「谁签的这句话查不到了」"
        )
        _record_action(
            conn,
            item=item,
            action=act,
            role=rl,
            operator=who,
            before=item.status,
            after=item.status,
            expected_version=expected_version,
            version_after=item.version,
            note=note,
            ok=False,
            error_code="LEDGER_VERSION_CONFLICT",
            detail=detail,
            stamp=now_stamp,
            batch_id=batch_id,
        )
        return TransitionOutcome(
            key=item.key,
            action=act,
            role=rl,
            operator=who,
            before=item.status,
            after=item.status,
            advanced=False,
            version=item.version,
            expected_version=expected_version,
            error_code="LEDGER_VERSION_CONFLICT",
            detail=detail,
            note=note,
            recorded_at=now_stamp,
        )

    _record_action(
        conn,
        item=item,
        action=act,
        role=rl,
        operator=who,
        before=item.status,
        after=target,
        expected_version=expected_version,
        version_after=new_version,
        note=note,
        ok=True,
        error_code="",
        detail="",
        stamp=now_stamp,
        batch_id=batch_id,
    )
    return TransitionOutcome(
        key=item.key,
        action=act,
        role=rl,
        operator=who,
        before=item.status,
        after=target,
        advanced=True,
        version=new_version,
        expected_version=expected_version,
        note=note,
        recorded_at=now_stamp,
    )


def _version_of(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute(
        f"SELECT version FROM {LEDGER_DISCREPANCY_TABLE} WHERE disc_key=?", (key,)
    ).fetchone()
    return int(row["version"]) if row is not None else -1


def _assert_role_quiet(item: Discrepancy, action: str, role: str, who: str) -> None:
    """岗位资格 + 分离判据。拆出来是为了 :func:`_single_transition` 预检
    与 :func:`_assert_role` 共用同一段判据（后者给 UI 算提示用）。"""
    permitted = required_role_for(item.status, action)
    if role not in permitted:
        raise ReconqError(
            "LEDGER_ROLE_DENIED",
            f"处置单 {item.key} 当前在「{STATUS_ZH.get(item.status, item.status)}」，"
            f"该状态下「{ACTION_ZH[action]}」只能由 "
            f"{'、'.join(ROLE_ZH.get(r, r) for r in sorted(permitted)) or '（无）'} 执行，"
            f"收到的是「{ROLE_ZH.get(role, role)}」。"
            f"主管复核的意义是多一道**独立**签字：主管顺手把审核那一关也签了，"
            f"这一关就等于不存在",
        )
    _assert_separation(item, action, role, who)


# ─────────────────────────── 三条业务入口 ───────────────────────────


def submit(
    conn: sqlite3.Connection,
    key: str,
    conclusion: str,
    *,
    operator: str,
    expected_version: Optional[int] = None,
    role: Any = Role.HANDLER,
    note: str = "",
    now: Optional[str] = None,
) -> TransitionOutcome:
    """经办提交处置结论：``待归因 → 待确认``。

    ``expected_version`` 在单条入口**必填**（``None`` 直接拒）——面板上的
    按钮一定带着打开那一刻的版本号过来，不带就说明中间有别人动过这条，
    而「我看到的和我确认的不是同一条」正是最该拦的那一类错。
    """
    if expected_version is None:
        raise ReconqError(
            "LEDGER_VERSION_REQUIRED",
            f"单条处置必须带 expected_version（处置单 {key!r}）。"
            f"没有版本号的确认会无声盖掉别人刚写下的结论",
        )
    item = require(conn, key)
    return _single_transition(
        conn,
        item,
        action=LedgerAction.SUBMIT,
        role=role,
        operator=operator,
        target=STATUS_PENDING_CONFIRM,
        expected_version=expected_version,
        conclusion=conclusion,
        note=note,
        stamp=now,
    )


def approve(
    conn: sqlite3.Connection,
    key: str,
    *,
    operator: str,
    expected_version: Optional[int] = None,
    role: Any = Role.REVIEWER,
    note: str = "",
    now: Optional[str] = None,
) -> TransitionOutcome:
    """审核通过（或主管签字）。

    落点由**账期是否存疑**决定：普通账期落 ``已确认``，存疑账期落
    ``待主管复核``。判定走 :func:`requires_supervisor_for`（行上标记优先），
    **调用方不能指定落点** —— 让调用方指定就等于让人自己决定要不要多一道
    签字。
    """
    item = require(conn, key)
    rl = _role_value(role)
    if item.status == STATUS_PENDING_SUPERVISOR:
        target = STATUS_CONFIRMED
    elif requires_supervisor_for(conn, item):
        target = STATUS_PENDING_SUPERVISOR
    else:
        target = STATUS_CONFIRMED
    return _single_transition(
        conn,
        item,
        action=LedgerAction.APPROVE,
        role=rl,
        operator=operator,
        target=target,
        expected_version=expected_version,
        note=note,
        stamp=now,
    )


def reject(
    conn: sqlite3.Connection,
    key: str,
    *,
    operator: str,
    expected_version: Optional[int] = None,
    role: Any = Role.REVIEWER,
    note: str = "",
    now: Optional[str] = None,
) -> TransitionOutcome:
    """审核或主管驳回：``待确认 / 待主管复核 → 已驳回``。

    ``已确认 → 已驳回`` 直跳被禁（白名单外）。要改判先撤回 —— 结论单上
    要留得住中间那次「撤回」，否则半年后复盘看到的是一条凭空的翻案。
    """
    item = require(conn, key)
    return _single_transition(
        conn,
        item,
        action=LedgerAction.REJECT,
        role=role,
        operator=operator,
        target=STATUS_REJECTED,
        expected_version=expected_version,
        note=note,
        stamp=now,
    )


def withdraw(
    conn: sqlite3.Connection,
    key: str,
    *,
    operator: str,
    expected_version: Optional[int] = None,
    role: Any = Role.HANDLER,
    note: str = "",
    now: Optional[str] = None,
) -> TransitionOutcome:
    """撤回：``已确认 / 已驳回 → 待确认``。

    撤回**不销号**：``handler`` / ``reviewer`` / ``supervisor`` 三列都
    留着，``version`` 也照常 +1。销号会让「这单曾经被谁签过」在结论单上
    消失 —— 而那恰恰是复盘时最需要的那句话。留痕表里「确认过」与
    「撤回过」两条同时在。
    """
    item = require(conn, key)
    return _single_transition(
        conn,
        item,
        action=LedgerAction.WITHDRAW,
        role=role,
        operator=operator,
        target=STATUS_PENDING_CONFIRM,
        expected_version=expected_version,
        note=note,
        stamp=now,
    )


# ─────────────────────────── 批量处置 ───────────────────────────


@dataclass(frozen=True)
class BatchItemOutcome:
    """批量里单条的结果。**逐条都要有** —— 成功的条数与失败的条数同样
    是结果的一半。"""

    key: str
    before: str
    after: str
    advanced: bool
    error_code: str = ""
    detail: str = ""
    role: str = ""
    operator: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "advanced": self.advanced,
            "error_code": self.error_code,
            "detail": self.detail,
            "role": self.role,
            "role_zh": ROLE_ZH.get(self.role, self.role),
            "operator": self.operator,
        }


@dataclass(frozen=True)
class BatchOutcome:
    """一次批量处置的整体结果。"""

    action: str
    role: str
    operator: str
    batch_id: str
    items: tuple[BatchItemOutcome, ...] = ()
    note: str = ""

    @property
    def advanced(self) -> int:
        return sum(1 for i in self.items if i.advanced)

    @property
    def failed(self) -> tuple[BatchItemOutcome, ...]:
        return tuple(i for i in self.items if not i.advanced)

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "action": self.action,
            "action_zh": ACTION_ZH.get(self.action, self.action),
            "role": self.role,
            "role_zh": ROLE_ZH.get(self.role, self.role),
            "operator": self.operator,
            "total": len(self.items),
            "advanced": self.advanced,
            "failed": len(self.failed),
            "items": [i.to_dict() for i in self.items],
            "note": self.note,
            "ok": self.ok,
        }


def batch_id_for(period: str, action: str, operator: str, stamp: str) -> str:
    """批次号。**人可读且可粘贴** —— 一条失败留痕要能靠它找回是哪一次
    批量点出来的。"""
    tail = (operator or "").strip()[:8] or "anon"
    return f"{period}/{action}/{tail}/{stamp.replace(':', '')}"


def batch_apply(
    conn: sqlite3.Connection,
    *,
    action: Any,
    role: Any,
    operator: str,
    period: str = "",
    channel: str = "",
    status: str = "",
    keys: Optional[Sequence[str]] = None,
    expected_versions: Optional[dict[str, int]] = None,
    conclusion: str = "",
    note: str = "",
    now: Optional[str] = None,
) -> BatchOutcome:
    """按渠道 / 按状态 / 按键批量处置。**逐条成败，单条失败不影响其余**。

    与 :func:`reconq.align.aggregate.apply_group_action` 的「整组要么全动
    要么全不动」**刻意相反**，理由见模块头纪律 4：聚合单是「一件事」，
    而这里的候选来自不同渠道、不同成因、不同状态，本来就不同质。

    每条**独立事务**：一条卡住不会回滚已成功的那些。留痕逐条写，
    失败的条也能解释「我点了确认为什么它没动」。
    """
    act = _action_value(action)
    rl = _role_value(role)
    who = assert_human_operator(operator)
    stamp = now or _now()
    require_ledger_schema(conn)
    batch_id = batch_id_for(period or "-", act, who, stamp)

    if keys:
        items = [_select_by_key(conn, k) for k in keys]
    else:
        items = list_discrepancies(
            conn, period=period, channel=channel, status=status
        )
    results: list[BatchItemOutcome] = []
    versions = expected_versions or {}
    for item in items:
        if item is None:
            results.append(
                BatchItemOutcome(
                    key="",
                    before="",
                    after="",
                    advanced=False,
                    error_code="LEDGER_DISCREPANCY_NOT_FOUND",
                    detail="批量里给的键在处置单上不存在",
                )
            )
            continue
        if act == LedgerAction.SUBMIT.value:
            outcome = _single_transition(
                conn,
                item,
                action=act,
                role=rl,
                operator=who,
                target=STATUS_PENDING_CONFIRM,
                expected_version=versions.get(item.key, item.version),
                conclusion=conclusion,
                note=note,
                stamp=stamp,
                batch_id=batch_id,
            )
        elif act == LedgerAction.APPROVE.value:
            if item.status == STATUS_PENDING_SUPERVISOR:
                target = STATUS_CONFIRMED
            elif requires_supervisor_for(conn, item):
                target = STATUS_PENDING_SUPERVISOR
            else:
                target = STATUS_CONFIRMED
            outcome = _single_transition(
                conn,
                item,
                action=act,
                role=rl,
                operator=who,
                target=target,
                expected_version=versions.get(item.key, item.version),
                note=note,
                stamp=stamp,
                batch_id=batch_id,
            )
        elif act == LedgerAction.REJECT.value:
            outcome = _single_transition(
                conn,
                item,
                action=act,
                role=rl,
                operator=who,
                target=STATUS_REJECTED,
                expected_version=versions.get(item.key, item.version),
                note=note,
                stamp=stamp,
                batch_id=batch_id,
            )
        else:
            outcome = _single_transition(
                conn,
                item,
                action=act,
                role=rl,
                operator=who,
                target=STATUS_PENDING_CONFIRM,
                expected_version=versions.get(item.key, item.version),
                note=note,
                stamp=stamp,
                batch_id=batch_id,
            )
        results.append(
            BatchItemOutcome(
                key=outcome.key,
                before=outcome.before,
                after=outcome.after,
                advanced=outcome.advanced,
                error_code=outcome.error_code,
                detail=outcome.detail,
                role=rl,
                operator=who,
            )
        )
    return BatchOutcome(
        action=act, role=rl, operator=who, batch_id=batch_id, items=tuple(results), note=note
    )


def render_batch_zh(outcome: BatchOutcome) -> str:
    """批量结果的中文摘要。成功与失败都写全，不只报「成功 N 条」。"""
    head = (
        f"批量「{ACTION_ZH.get(outcome.action, outcome.action)}」"
        f"（{ROLE_ZH.get(outcome.role, outcome.role)}{outcome.operator}，"
        f"批次 {outcome.batch_id}）"
    )
    if not outcome.items:
        return f"{head}：候选集为空，一条未动"
    lines = [
        f"{head}：{outcome.advanced}/{len(outcome.items)} 条已推进"
        + ("，全部成功" if outcome.ok else f"，{len(outcome.failed)} 条未推进")
    ]
    for item in outcome.failed:
        lines.append(
            f"  · {item.key or '（键不存在）'}：当前 {STATUS_ZH.get(item.before, item.before) or '—'}，"
            f"{item.error_code}（{item.detail}）"
        )
    return "\n".join(lines)
