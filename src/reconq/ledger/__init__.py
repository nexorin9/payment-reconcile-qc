"""差异处置单（Task 43）。

台账（``period_diff_items``）回答「这一期有哪些差异」，处置单回答
「这一条我办没办、谁办的」。两层并存：处置单不替代台账的逐条证据，
台账也不替人签字。

与源产品的关系：``soda-core/src/soda_core/cli/cli.py:787-829`` 的
``contract request transition`` 用**状态白名单**约束迁移（参数层
``choices`` 挡非法值，:813；``RequestStatus(args.status)`` 收回枚举，:820）。
白名单与枚举回收两条原样沿用；三角色会签、不相容岗位分离、乐观锁与
留痕表是本轮为「结论单上那几行签字必须查得到谁签的」新建的 —— 源产品
的 request 状态在远端注册表，本地不留副本、也没有操作人概念。

本包分两层：:mod:`~reconq.ledger.models` 只放类型与判据（状态、角色、
白名单、表结构），:mod:`~reconq.ledger.flow` 放执行。依赖方向单向，
「谁能改状态」只在 flow 里写一遍。
"""

from __future__ import annotations

from .flow import (
    NON_HUMAN_OPERATORS,
    BatchItemOutcome,
    BatchOutcome,
    TransitionOutcome,
    approve,
    assert_human_operator,
    batch_apply,
    batch_id_for,
    count_by_status,
    list_actions,
    list_discrepancies,
    load,
    open_discrepancy,
    period_requires_supervisor,
    reject,
    render_batch_zh,
    render_transition_zh,
    require,
    requires_supervisor_for,
    submit,
    withdraw,
)
from .models import (
    ACTION_ROLES,
    ACTION_SOURCES,
    ACTION_ZH,
    ALLOWED_TRANSITIONS,
    ALL_STATUSES,
    LEDGER_ACTION_INDEXES,
    LEDGER_ACTION_TABLE,
    LEDGER_ACTION_TABLE_DDL,
    LEDGER_DISCREPANCY_INDEXES,
    LEDGER_DISCREPANCY_TABLE,
    LEDGER_DISCREPANCY_TABLE_DDL,
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
    allowed_targets,
    can_transition,
    diff_key,
    dumps_or,
    init_ledger_schema,
    ledger_key,
    parse_amount,
    require_ledger_schema,
    required_role_for,
    row_to_discrepancy,
)

__all__ = [
    # —— 状态与角色 ——
    "ACTION_ROLES",
    "ACTION_SOURCES",
    "ACTION_ZH",
    "ALLOWED_TRANSITIONS",
    "ALL_STATUSES",
    "LEDGER_VERSION",
    "ROLE_ORDER",
    "ROLE_ZH",
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
    "required_role_for",
    # —— 表结构 ——
    "LEDGER_ACTION_INDEXES",
    "LEDGER_ACTION_TABLE",
    "LEDGER_ACTION_TABLE_DDL",
    "LEDGER_DISCREPANCY_INDEXES",
    "LEDGER_DISCREPANCY_TABLE",
    "LEDGER_DISCREPANCY_TABLE_DDL",
    "LEDGER_KEY_SEP",
    "dumps_or",
    "init_ledger_schema",
    "require_ledger_schema",
    "row_to_discrepancy",
    # —— 键与金额 ——
    "diff_key",
    "ledger_key",
    "parse_amount",
    # —— 执行 ——
    "BatchItemOutcome",
    "BatchOutcome",
    "NON_HUMAN_OPERATORS",
    "TransitionOutcome",
    "approve",
    "assert_human_operator",
    "batch_apply",
    "batch_id_for",
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
