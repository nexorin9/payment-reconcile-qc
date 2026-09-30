"""常驻服务与浏览器实时面板（Task 47）。

源产品是**纯 CLI 一次性执行**：``soda contract verify -c ... -ds ...`` 跑完
把证据行打到终端，进程退出即消失（``github_ref/soda-core/soda-core/src/
soda_core/cli/cli.py:144 _setup_contract_verify_command()``）。本产品把
**同一条主路径**（读契约 → 逐 check 执行 → 出证据行 → 门禁退出码）搬到
常驻形态上：扫描进度、渠道状态、差异台账与处置动作在浏览器里持续可见，
状态全部落在**账单库**里（与源产品的命令行瞬时值相反）。

与源产品的取舍，写在 ``spec.md → ## 开源对照``：形态是新建的，但**状态
真源没换** —— 面板只是 :mod:`reconq.engine.period` 与
:mod:`reconq.ledger` 之上的一层只读投影加两个写入口。面板**不持有**任何
业务状态，任何一次「确认 / 驳回」都必须回到 ledger 的状态机；SSE 只推
「发生了什么变了」，不推「所以你该点什么」。

三条硬边界：

1. **模型与规则推不动状态。** ``POST /api/discrepancy/{key}/decide`` 只
   接受自然人操作人（:func:`reconq.ledger.flow.assert_human_operator`
   已经在入口挡掉 ``model`` / ``rule`` / ``system``），且必须带
   ``expected_version`` 走乐观锁。
2. **非法状态返回 4xx 且带错误码。** 状态机是唯一裁决者，Web 层不
   「善意的」兜住它。
3. **凭据不出进程。** 服务只读账单库（本地 SQLite），不连 HIS / 医保；
   真实凭据仍在各渠道取数那条腿（``reconq scan`` / ``data-source test``）里，
   从环境变量读。
"""

from __future__ import annotations

from .app import create_app, run_server
from .events import EventBus, PanelEvent

__all__ = ["EventBus", "PanelEvent", "create_app", "run_server"]
