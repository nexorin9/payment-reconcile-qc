"""差异处置单的数据件（Task 43 · 第一半）。

本模块只放**类型与判据**：状态取值、角色、迁移白名单、表结构。真正
执行迁移的 :mod:`reconq.ledger.flow` 不在这里，避免「谁能改状态」这件事
在两个文件里各写一遍。

与源产品的关系
--------------
参考实现 ``soda-core/src/soda_core/cli/cli.py:787-829`` 的
``contract request transition`` 用一条 **状态白名单** 约束迁移：参数层
用 ``choices=[status.value for status in RequestStatus]`` 挡掉非法取值
（:813），随后 ``RequestStatus(args.status)`` 收回枚举（:820），再由
``handle_transition_request`` 推给远端。**白名单 + 枚举回收**这两条原样
沿用；差异之外的三样是本轮为「三角色会签」新建的，源产品没有：

* 源产品的迁移**只管单请求状态**（一次 verify 跑没跑完），没有操作人、
  没有角色、没有留痕；本产品的每一步都要能答出「谁、何时、依据哪几条
  证据行」。
* 源产品的状态存在远端注册表，本地只留命令行瞬时值；本产品的处置单
  **整条留在账单库里**（与账单行、契约指纹出处同库），随备份走。
* 源产品的迁移由人直接触发，本产品多一道**不相容岗位分离**（经办 /
  审核 / 主管不得由同一人担任）——财务科签字的是结论单，而结论单上
  写着「经办人经办、审核人审核」，同一个人占两岗等于那道签字没被赚到。

四态与一条旁路
--------------
``待归因 → 待确认 → 已确认 / 已驳回``，两个终态都能撤回重置为
``待确认``。之所以比 Task 18 的账期实例多一个「待主管复核」旁路，是因为
**存疑账期必须经主管**：存疑意味着这一期有人已经提出过异议，结论单上
不能只有经办与审核两道签字。主管复核不是第四个状态而是第三道**关卡**，
落在旁路上：

    待归因 ──经办提交──▶ 待确认 ──审核通过──▶ ┬─ 普通账期 ──▶ 已确认
                          │                    └─ 存疑账期 ──▶ 待主管复核
                          │                                        │
                          └──审核驳回────────────────────────────┤
                                                                   ▼
                                                              已确认 / 已驳回

（任一关卡都可以**驳回**到 ``已驳回``；两个终态都能**撤回**回
``待确认``。``待归因`` 只能由**经办**推进 —— 审核和主管都不许替经办人
把「我看过、我判了」这一句话签掉，那正是差异台账存在的意义。）
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional, Sequence

from ..errors import ReconqError

__all__ = [
    "ALL_STATUSES",
    "ACTION_ROLES",
    "ACTION_SOURCES",
    "ALLOWED_TRANSITIONS",
    "LEDGER_ACTION_TABLE",
    "LEDGER_ACTION_TABLE_DDL",
    "LEDGER_ACTION_INDEXES",
    "LEDGER_DISCREPANCY_TABLE",
    "LEDGER_DISCREPANCY_TABLE_DDL",
    "LEDGER_DISCREPANCY_INDEXES",
    "LEDGER_KEY_SEP",
    "LEDGER_VERSION",
    "STATUS_ALLOWED",
    "STATUS_CONFIRMED",
    "STATUS_PENDING_ATTRIBUTION",
    "STATUS_PENDING_CONFIRM",
    "STATUS_PENDING_SUPERVISOR",
    "STATUS_REJECTED",
    "STATUS_ZH",
    "Discrepancy",
    "LedgerAction",
    "Role",
    "allowed_targets",
    "can_transition",
    "diff_key",
    "dumps_or",
    "init_ledger_schema",
    "ledger_key",
    "parse_amount",
    "required_role_for",
]


#: 状态机版本。迁移白名单或角色约束改了必须 bump —— 结论单上要写清
#: 「这份结论是按哪一版会签规则出的」，否则规则改了之后历史结论读不出
#: 是按哪条路批下来的。
LEDGER_VERSION = "discrepancy-ledger/1"

#: 业务键分隔符。与 :data:`reconq.align.aggregate.DIFF_KEY_SEP` 同取 ``|``
#: （渠道与口径里天然带冒号），**但各自定义** —— 处置单的键多一层
#: 校验前缀将来可能变，两处共用一个常量会让改一处静默改到另一处。
LEDGER_KEY_SEP = "|"


# ─────────────────────────── 状态 ───────────────────────────


STATUS_PENDING_ATTRIBUTION = "待归因"
STATUS_PENDING_CONFIRM = "待确认"
STATUS_PENDING_SUPERVISOR = "待主管复核"
STATUS_CONFIRMED = "已确认"
STATUS_REJECTED = "已驳回"

ALL_STATUSES: tuple[str, ...] = (
    STATUS_PENDING_ATTRIBUTION,
    STATUS_PENDING_CONFIRM,
    STATUS_PENDING_SUPERVISOR,
    STATUS_CONFIRMED,
    STATUS_REJECTED,
)

STATUS_ZH: dict[str, str] = {
    STATUS_PENDING_ATTRIBUTION: "待归因（还没人看过这条差异）",
    STATUS_PENDING_CONFIRM: "待确认（经办已交结论，等审核）",
    STATUS_PENDING_SUPERVISOR: "待主管复核（存疑账期，须主管签字）",
    STATUS_CONFIRMED: "已确认",
    STATUS_REJECTED: "已驳回",
}

#: 迁移白名单。**表外组合一律拒绝**并报出「当前可去哪」，与
#: :data:`reconq.engine.period.ALLOWED_TRANSITIONS` 同一条纪律。
#:
#: 四条收紧各有理由：
#:
#: 1. ``待归因 → 已确认`` 不允许（必须先经 ``待确认``）。省掉「待确认」
#:    就等于省掉了**经办人写下的处置结论**，而结论单上那一栏正是从
#:    结论单要的 —— 没有结论的确认在纸面上是无源的。
#: 2. ``已确认 → 待归因`` 直跳被禁（spec 点名的反例），``已确认 → 已驳回``
#:    同样被禁：改判必须先撤回重办，结论单上要留得住中间那次「撤回」。
#: 3. ``待主管复核`` **只能由主管**推走，且只能去两个终态 —— 它是签字
#:    关卡不是工作台，面板上不该在这个状态上让人随便改结论。
#: 4. ``待确认 → 待主管复核`` 是唯一进关卡的口子，且**由系统按账期是否
#:    存疑决定**，不由调用方挑：让调用方决定就等于让人自己决定要不要
#:    多一道签字。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PENDING_ATTRIBUTION: frozenset({STATUS_PENDING_CONFIRM}),
    STATUS_PENDING_CONFIRM: frozenset(
        {STATUS_PENDING_SUPERVISOR, STATUS_CONFIRMED, STATUS_REJECTED}
    ),
    # 关卡只去终态，且**不能退回待确认** —— 退回是「撤回」，是另一个动作。
    STATUS_PENDING_SUPERVISOR: frozenset({STATUS_CONFIRMED, STATUS_REJECTED}),
    # 两个终态的出路只有撤回一条，且**都只回待确认**，不回待归因。
    STATUS_CONFIRMED: frozenset({STATUS_PENDING_CONFIRM}),
    STATUS_REJECTED: frozenset({STATUS_PENDING_CONFIRM}),
}

#: 仍需人处理的状态。面板的「待办」栏取这张表 —— 已确认与已驳回是**活单**
#: （要能撤回），但不是待办；这一层区分决定默认视图里出现什么。
STATUS_ALLOWED: frozenset[str] = frozenset(
    {STATUS_PENDING_ATTRIBUTION, STATUS_PENDING_CONFIRM, STATUS_PENDING_SUPERVISOR}
)


# ─────────────────────────── 角色与动作 ───────────────────────────


class Role(str, Enum):
    """三个不相容岗位。值即 ``ledger_actions.role`` 的取值。"""

    HANDLER = "handler"  # 经办：看证据、写处置结论
    REVIEWER = "reviewer"  # 审核：复核结论、必要时驳回
    SUPERVISOR = "supervisor"  # 主管复核：只管存疑账期的终态签字

    @property
    def zh(self) -> str:
        return ROLE_ZH[self.value]


ROLE_ZH: dict[str, str] = {
    Role.HANDLER.value: "经办",
    Role.REVIEWER.value: "审核",
    Role.SUPERVISOR.value: "主管复核",
}

#: 三个岗位的先后次序。用于「不相容岗位分离」判据与面板排序。
ROLE_ORDER: tuple[str, ...] = (
    Role.HANDLER.value,
    Role.REVIEWER.value,
    Role.SUPERVISOR.value,
)


class LedgerAction(str, Enum):
    """处置单动作。值即 ``ledger_actions.action`` 的取值。"""

    SUBMIT = "submit"  # 经办提交处置结论：待归因 → 待确认
    APPROVE = "approve"  # 审核通过 / 主管签字
    REJECT = "reject"  # 审核或主管驳回
    WITHDRAW = "withdraw"  # 撤回：两个终态 → 待确认

    @property
    def zh(self) -> str:
        return ACTION_ZH[self.value]


ACTION_ZH: dict[str, str] = {
    LedgerAction.SUBMIT.value: "提交处置结论",
    LedgerAction.APPROVE.value: "通过",
    LedgerAction.REJECT.value: "驳回",
    LedgerAction.WITHDRAW.value: "撤回",
}

#: 动作 → 合法来源状态。**单一真源**：白名单预检、按钮禁用态、批量
#: 筛选全从这张表推出来，三处各写一遍 ``if action == ...`` 早晚会漂。
ACTION_SOURCES: dict[str, frozenset[str]] = {
    LedgerAction.SUBMIT.value: frozenset({STATUS_PENDING_ATTRIBUTION}),
    LedgerAction.APPROVE.value: frozenset(
        {STATUS_PENDING_CONFIRM, STATUS_PENDING_SUPERVISOR}
    ),
    LedgerAction.REJECT.value: frozenset(
        {STATUS_PENDING_CONFIRM, STATUS_PENDING_SUPERVISOR}
    ),
    LedgerAction.WITHDRAW.value: frozenset({STATUS_CONFIRMED, STATUS_REJECTED}),
}

#: 动作 → 有权执行的角色。**没有**「谁都能撤回」这回事：撤回同样在动
#: 业务状态，同样要留一个自然人。
ACTION_ROLES: dict[str, frozenset[str]] = {
    LedgerAction.SUBMIT.value: frozenset({Role.HANDLER.value}),
    LedgerAction.APPROVE.value: frozenset({Role.REVIEWER.value, Role.SUPERVISOR.value}),
    LedgerAction.REJECT.value: frozenset({Role.REVIEWER.value, Role.SUPERVISOR.value}),
    LedgerAction.WITHDRAW.value: frozenset(
        {Role.HANDLER.value, Role.REVIEWER.value, Role.SUPERVISOR.value}
    ),
}


def diff_key(period: str, channel: str, check_path: str) -> str:
    """差异条目的业务键。**与台账行同键** —— 处置单不是另一份台账，
    它是台账那一行上的会签轨迹。"""
    return LEDGER_KEY_SEP.join((period or "", channel or "", check_path or ""))


#: 别名。调用方手上拿的常常已经是台账键，函数名跟着语境走更顺。
ledger_key = diff_key


def allowed_targets(status: str) -> list[str]:
    """当前状态可以去的落点（报错时用来告诉人「现在能去哪」）。"""
    return sorted(ALLOWED_TRANSITIONS.get(status, frozenset()))


def can_transition(current: str, target: str) -> bool:
    """这条迁移是否在白名单内。不在则 False（不抛错，供 UI 算禁用态）。"""
    if target not in ALL_STATUSES:
        return False
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def required_role_for(status: str, action: Any) -> frozenset[str]:
    """在 ``status`` 上执行 ``action`` 时**实际有权的角色**。

    比 :data:`ACTION_ROLES` 更严：审核与主管各管一段，不能互相代劳 ——
    ``待确认`` 上的通过只能由审核做，``待主管复核`` 上的通过只能由主管
    做。理由是主管复核的存在意义就是**多一道独立的签字**；主管顺手把
    审核那一关也签了，这一关就等于不存在。
    """
    key = _action_value(action)
    if status == STATUS_PENDING_ATTRIBUTION:
        return frozenset({Role.HANDLER.value})
    if status == STATUS_PENDING_SUPERVISOR:
        return frozenset({Role.SUPERVISOR.value})
    if status == STATUS_PENDING_CONFIRM:
        return (
            frozenset({Role.REVIEWER.value})
            if key in (LedgerAction.APPROVE.value, LedgerAction.REJECT.value)
            else frozenset()
        )
    return frozenset(ACTION_ROLES[key]) if status in ACTION_SOURCES[key] else frozenset()


def _action_value(action: Any) -> str:
    raw = action.value if isinstance(action, LedgerAction) else str(action or "")
    if raw not in ACTION_SOURCES:
        raise ReconqError(
            "LEDGER_ACTION_UNKNOWN",
            f"未登记的处置动作 {raw!r}；本产品只有四种："
            f"{'、'.join(f'{k}（{v}）' for k, v in ACTION_ZH.items())}",
        )
    return raw


# ─────────────────────────── 金额 ───────────────────────────


def parse_amount(value: Any, *, where: str = "差额") -> Decimal:
    """金额转 ``Decimal``。**float 一律拒绝**。

    与 :func:`reconq.align.aggregate.to_decimal` 同一条纪律但**各自
    实现**：处置单的差额会被算进结论单的合计，而合计是对着财务科台账
    核的，尾数差一分钱就要有人回来解释。共用一个 helper 看着省事，
    将来聚合层要改容差时会顺带改掉签字用的口径。
    """
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, float):
        raise ReconqError(
            "LEDGER_AMOUNT_FLOAT_REJECTED",
            f"{where} 收到 float（{value!r}）；账单金额一律用定点字符串或 "
            f"Decimal 传入 —— 二进制浮点尾数会让结论单上的合计对不上财务科台账",
        )
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ReconqError(
            "AMOUNT_UNPARSABLE",
            f"{where} {value!r} 不是可解析的金额（应为定点字符串或 Decimal）",
        ) from exc


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ─────────────────────────── 数据件 ───────────────────────────


@dataclass(frozen=True)
class Discrepancy:
    """一条差异的处置单。

    字段按 spec 的最小集合给：账期 / 渠道 / 单号 / 差额 / 证据行号列表 /
    归因草稿 / 状态 / 处置人 / 处置时间 / ``version``（乐观锁）。

    刻意**不落整行原文**。证据只存 ``文件名:行号``（:attr:`evidence_rows`），
    要看原文时回源文件 —— 交办单是要发给渠道方与信息科的，整行原文里
    带着渠道自定义列，那里面可能有患者信息。
    """

    key: str
    period: str
    channel: str
    check_path: str = ""
    order_no: str = ""
    diff_amount: Decimal = Decimal("0")
    #: 上游是否给过金额。差别于「金额是 0」—— 有些差异本身不带差额
    #: （如「HIS 未落单」只说明查不到行），把它当 0 元会计进合计里。
    amount_known: bool = True
    evidence_rows: tuple[str, ...] = ()
    #: 归因草稿（``{cause, confidence, evidence_refs, explanation_zh}``）。
    #: **它只是草稿**：经办的处置结论写在 :attr:`conclusion` 上，两者
    #: 分开是因为「模型说的原因」与「人签的原因」在结论单上要分两栏 ——
    #: 合成一栏就看不出人有没有采信模型。
    cause_draft: Optional[dict[str, Any]] = None
    status: str = STATUS_PENDING_ATTRIBUTION
    #: 该条是否强制走主管复核。落库时按账期是否存疑定下（见
    #: :func:`reconq.ledger.flow.period_requires_supervisor`），**不由
    #: 调用方在审核那一刻挑**。
    requires_supervisor: bool = False
    #: 经办写下的处置结论原文。
    conclusion: str = ""
    handler: str = ""
    handler_at: str = ""
    reviewer: str = ""
    reviewer_at: str = ""
    supervisor: str = ""
    supervisor_at: str = ""
    version: int = 0
    created_at: str = ""
    updated_at: str = ""

    @property
    def status_zh(self) -> str:
        return STATUS_ZH.get(self.status, self.status)

    @property
    def is_open(self) -> bool:
        """是否仍在待办里。已确认 / 已驳回是**活单**（可撤回）但不是待办。"""
        return self.status in STATUS_ALLOWED

    @property
    def amount_text(self) -> str:
        return f"{self.diff_amount:.2f}" if self.amount_known else ""

    @property
    def signoff_roles(self) -> tuple[str, ...]:
        """已经占掉的岗位（按 ROLE_ORDER 序）。面板上标「这一关谁签过」。"""
        filled = []
        if self.handler:
            filled.append(Role.HANDLER.value)
        if self.reviewer:
            filled.append(Role.REVIEWER.value)
        if self.supervisor:
            filled.append(Role.SUPERVISOR.value)
        return tuple(sorted(set(filled), key=ROLE_ORDER.index))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "ledger_version": LEDGER_VERSION,
            "period": self.period,
            "channel": self.channel,
            "check_path": self.check_path,
            "order_no": self.order_no,
            "diff_amount": self.amount_text,
            "amount_known": self.amount_known,
            "evidence_rows": list(self.evidence_rows),
            "cause_draft": dict(self.cause_draft) if self.cause_draft else None,
            "status": self.status,
            "status_zh": self.status_zh,
            "requires_supervisor": self.requires_supervisor,
            "conclusion": self.conclusion,
            "handler": self.handler,
            "handler_at": self.handler_at,
            "reviewer": self.reviewer,
            "reviewer_at": self.reviewer_at,
            "supervisor": self.supervisor,
            "supervisor_at": self.supervisor_at,
            "signoff_roles": list(self.signoff_roles),
            "version": self.version,
            "is_open": self.is_open,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def render_zh(self) -> str:
        head = f"[{self.status}] {self.channel} / {self.period} / {self.order_no or self.key}"
        if not self.evidence_rows:
            return head
        return f"{head}（证据 {'、'.join(self.evidence_rows[:3])}）"


#: 落库 ↔ 读回的列序。两处各写一遍 ``SELECT`` 列清单早晚会漏列。
_DISCREPANCY_COLUMNS: tuple[str, ...] = (
    "disc_id",
    "disc_key",
    "ledger_version",
    "period",
    "channel",
    "check_path",
    "order_no",
    "diff_amount",
    "amount_known",
    "evidence_json",
    "cause_draft_json",
    "status",
    "requires_supervisor",
    "conclusion",
    "handler",
    "handler_at",
    "reviewer",
    "reviewer_at",
    "supervisor",
    "supervisor_at",
    "version",
    "created_at",
    "updated_at",
)


# ─────────────────────────── 表结构 ───────────────────────────

LEDGER_DISCREPANCY_TABLE = "ledger_discrepancies"
LEDGER_ACTION_TABLE = "ledger_actions"

#: 处置单表。幂等键 = ``disc_key``（账期|渠道|口径），与台账行同键。
LEDGER_DISCREPANCY_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER_DISCREPANCY_TABLE} (
    disc_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    disc_key            TEXT    NOT NULL,
    ledger_version      TEXT    NOT NULL DEFAULT '{LEDGER_VERSION}',
    period              TEXT    NOT NULL,
    channel             TEXT    NOT NULL,
    check_path          TEXT    NOT NULL DEFAULT '',
    order_no            TEXT    NOT NULL DEFAULT '',
    diff_amount         TEXT    NOT NULL DEFAULT '0',
    amount_known        INTEGER NOT NULL DEFAULT 1,
    evidence_json       TEXT    NOT NULL DEFAULT '[]',
    cause_draft_json    TEXT,
    status              TEXT    NOT NULL DEFAULT '{STATUS_PENDING_ATTRIBUTION}',
    requires_supervisor INTEGER NOT NULL DEFAULT 0,
    conclusion          TEXT    NOT NULL DEFAULT '',
    handler             TEXT    NOT NULL DEFAULT '',
    handler_at          TEXT    NOT NULL DEFAULT '',
    reviewer            TEXT    NOT NULL DEFAULT '',
    reviewer_at         TEXT    NOT NULL DEFAULT '',
    supervisor          TEXT    NOT NULL DEFAULT '',
    supervisor_at       TEXT    NOT NULL DEFAULT '',
    version             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL DEFAULT '',
    updated_at          TEXT    NOT NULL DEFAULT ''
)
"""

#: ``version`` 列**带索引**：乐观锁的 UPDATE 走
#: ``WHERE disc_id=? AND version=?``，没有索引时并发确认会在全表上比
#: version，而并发恰恰是这条路径最常出现的场合。
LEDGER_DISCREPANCY_INDEXES = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_identity "
    f"ON {LEDGER_DISCREPANCY_TABLE} (disc_key)",
    f"CREATE INDEX IF NOT EXISTS ix_ledger_period_status "
    f"ON {LEDGER_DISCREPANCY_TABLE} (period, status)",
    f"CREATE INDEX IF NOT EXISTS ix_ledger_channel_status "
    f"ON {LEDGER_DISCREPANCY_TABLE} (channel, status)",
    f"CREATE INDEX IF NOT EXISTS ix_ledger_version "
    f"ON {LEDGER_DISCREPANCY_TABLE} (disc_id, version)",
)

#: 动作留痕表。**只追加** —— 「我明明点了确认为什么没动」与
#: 「这一条被改判过」都只能靠这张表答。
LEDGER_ACTION_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER_ACTION_TABLE} (
    action_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    period         TEXT    NOT NULL DEFAULT '',
    disc_key       TEXT    NOT NULL DEFAULT '',
    batch_id       TEXT    NOT NULL DEFAULT '',
    action         TEXT    NOT NULL,
    role           TEXT    NOT NULL,
    operator       TEXT    NOT NULL DEFAULT '',
    from_status    TEXT    NOT NULL DEFAULT '',
    to_status      TEXT    NOT NULL DEFAULT '',
    expected_version INTEGER,
    version_before INTEGER NOT NULL DEFAULT 0,
    version_after  INTEGER NOT NULL DEFAULT 0,
    note           TEXT    NOT NULL DEFAULT '',
    ok             INTEGER NOT NULL DEFAULT 1,
    error_code     TEXT    NOT NULL DEFAULT '',
    detail         TEXT    NOT NULL DEFAULT '',
    recorded_at    TEXT    NOT NULL DEFAULT ''
)
"""

LEDGER_ACTION_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_ledger_action_key "
    f"ON {LEDGER_ACTION_TABLE} (disc_key, action_id)",
    f"CREATE INDEX IF NOT EXISTS ix_ledger_action_period "
    f"ON {LEDGER_ACTION_TABLE} (period, recorded_at)",
    f"CREATE INDEX IF NOT EXISTS ix_ledger_action_batch "
    f"ON {LEDGER_ACTION_TABLE} (batch_id)",
)


def init_ledger_schema(conn: sqlite3.Connection) -> None:
    """建处置单表与留痕表。幂等：重复调用不报错。"""
    conn.execute(LEDGER_DISCREPANCY_TABLE_DDL)
    for statement in LEDGER_DISCREPANCY_INDEXES:
        conn.execute(statement)
    conn.execute(LEDGER_ACTION_TABLE_DDL)
    for statement in LEDGER_ACTION_INDEXES:
        conn.execute(statement)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def require_ledger_schema(conn: sqlite3.Connection) -> None:
    """处置单表尚未建表时**显式报错**，不静默建表。

    静默建表会让「这期到底处置过没有」这个问题在一条没扫过账的账期上
    也得到「没有」这个答案 —— 而正确的答案是「压根没开始」。两种回答
    在结论单上长得一模一样。
    """
    if not _table_exists(conn, LEDGER_DISCREPANCY_TABLE):
        raise ReconqError(
            "LEDGER_SCHEMA_MISSING",
            f"{LEDGER_DISCREPANCY_TABLE} 表尚未建表；先跑一次 "
            f"reconq ledger open --period <账期> 把本期差异开成处置单",
        )


def row_to_discrepancy(row: sqlite3.Row) -> Discrepancy:
    """``sqlite3.Row`` → :class:`Discrepancy`。金额与 JSON 各自还原。"""
    return Discrepancy(
        key=str(row["disc_key"]),
        period=str(row["period"]),
        channel=str(row["channel"]),
        check_path=str(row["check_path"] or ""),
        order_no=str(row["order_no"] or ""),
        diff_amount=parse_amount(row["diff_amount"], where="处置单差额"),
        amount_known=bool(row["amount_known"]),
        evidence_rows=tuple(json.loads(row["evidence_json"] or "[]")),
        cause_draft=json.loads(row["cause_draft_json"]) if row["cause_draft_json"] else None,
        status=str(row["status"]),
        requires_supervisor=bool(row["requires_supervisor"]),
        conclusion=str(row["conclusion"] or ""),
        handler=str(row["handler"] or ""),
        handler_at=str(row["handler_at"] or ""),
        reviewer=str(row["reviewer"] or ""),
        reviewer_at=str(row["reviewer_at"] or ""),
        supervisor=str(row["supervisor"] or ""),
        supervisor_at=str(row["supervisor_at"] or ""),
        version=int(row["version"] or 0),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def dumps_or(value: Any, default: str) -> str:
    """JSON 序列化，``None`` 落到 ``default``。

    ``sort_keys=True``：留痕与快照要能复算，草稿字典的键序不同就会让
    同一份内容算出两个指纹。
    """
    if value is None:
        return default
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
