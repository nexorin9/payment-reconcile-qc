"""账期扫描编排：registry → 拉取 → 落库 → 口径 → 状态（Task 18）。

串起的是本产品主路径的中段，把前 17 个任务攒下的零件接成一次扫描：

    initialize_all（Task 6 逐渠道失败剔除）
        └→ source.fetch(period)（Task 5/12 真实协议与只读库）
            └→ ingest_channel_rows（Task 7 幂等落库，唯一索引挡重放）
                └→ run_checks（Task 14/15 契约口径 + 证据行号）
                    └→ upsert_diff_items + finish_scan（差异台账 + 实例状态）

源产品对应实现
--------------
* ``cli.py:832`` ``exit_with_code()``：源产品把「契约有没有问题」折成
  进程退出码。CLI 一次性进程这样做没问题；常驻服务不行 —— 服务不能靠
  ``sys.exit`` 表达结论。本轮改写成**账期扫描实例状态 + 门禁色**
  （:func:`reconq.engine.period.finish_scan` / :func:`derive_gate`），
  CLI 那条 ``verify`` 腿仍按退出码返回，两边看的是同一份结果。
* ``streaming_orchestrator.py:31-65`` 逐 consumer 失败剔除的语义在
  :func:`reconq.sources.registry.initialize_all` 里，本模块只是它的
  调用方，不另写一套剔除逻辑。

三处本轮特有、源实现没有的决定
----------------------------
1. **单渠道失败只记该渠道，其余照常**（与 Task 6 一致），但**六路全废
   直接中止**：跑出一份「零笔差异」的空结论单比报错危险得多。
2. **六路少一路就不能判绿**。少一路的「全部口径通过」不是全部通过 ——
   门禁把 ``channels_failed`` 也算进去（:func:`derive_gate`）。
3. **本模块只写差异台账的「待确认」初态**。确认与驳回是人的动作
   （Task 43），本轮任何代码路径都不会把 ``disposition`` 推成已确认。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Sequence

from ..errors import ReconqError
from .common import CheckContext
from .period import (
    ChannelScan,
    CheckResultRow,
    DiffItem,
    PeriodScan,
    STATUS_ZH,
    count_active_failures,
    count_check_results_by_origin,
    count_pending,
    finish_scan,
    init_scan_schema,
    list_channel_results,
    list_check_results,
    load_scan,
    mark_check_results_carried,
    reopen_scan,
    require_scan,
    resolve_missing_diffs,
    start_scan,
    transition,
    upsert_check_results,
    upsert_channel_results,
    upsert_diff_items,
)
from .resume import (
    RerunScope,
    acquire_scan_lock,
    append_scan_event,
    clear_checkpoints,
    describe_scope,
    heartbeat_scan_lock,
    init_resume_schema,
    load_checkpoints,
    mark_channel_done,
    release_scan_lock,
    select_scope,
)
from .checks import BillRow, run_checks

__all__ = [
    "ScanOutcome",
    "scan",
    "scan_status",
    "default_variables",
]

logger = logging.getLogger("reconq.orchestrator")

#: 账期级幂等键。行级唯一键（渠道+账期+账单日+渠道单号）在 Task 7 的
#: 唯一索引里；这一层是**批级**的，回答运营侧「这一期这一路到底导进去
#: 没有」，两层缺一不可：只有行级键，运营侧无法回答批次问题。
def _scan_idempotency_key(period: str, channel: str = "*") -> str:
    return f"{period}|{channel}"


@dataclass
class ScanOutcome:
    """一次扫描的完整结果。CLI、面板、结论单读的是同一份。"""

    period: str
    run_id: str
    scan: PeriodScan
    channels: list[ChannelScan] = field(default_factory=list)
    diffs_written: int = 0
    warnings: list[str] = field(default_factory=list)
    scope: Optional[RerunScope] = None
    carried_channels: list[str] = field(default_factory=list)
    rerun_channels: list[str] = field(default_factory=list)
    resolved_diffs: list[str] = field(default_factory=list)
    origin_counts: dict[str, int] = field(default_factory=dict)
    event_seq: int = 0
    event_hash: str = ""
    resumed: bool = False

    @property
    def gate(self) -> str:
        return self.scan.gate

    @property
    def can_close(self) -> bool:
        """这期账能不能结。"""
        return self.scan.gate == "green"

    @property
    def failed_channels(self) -> list[str]:
        return [c.channel for c in self.channels if c.error_code]

    def channel_of(self, channel: str) -> Optional[ChannelScan]:
        for item in self.channels:
            if item.channel == channel:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "run_id": self.run_id,
            "gate": self.scan.gate,
            "can_close": self.can_close,
            "status": self.scan.status,
            "status_zh": self.scan.status_zh,
            "reason": self.scan.reason,
            "rows_ingested": self.scan.rows_ingested,
            "checks_total": self.scan.checks_total,
            "checks_failed": self.scan.checks_failed,
            "unconfirmed": self.scan.unconfirmed,
            "pending_attribution": self.scan.pending_attribution,
            "total_amount": self.scan.total_amount,
            "diffs_written": self.diffs_written,
            "failed_channels": self.failed_channels,
            "warnings": self.warnings,
            "resumed": self.resumed,
            "scope": self.scope.to_dict() if self.scope else None,
            "scope_zh": describe_scope(self.scope) if self.scope else "全部渠道",
            "rerun_channels": list(self.rerun_channels),
            "carried_channels": list(self.carried_channels),
            "resolved_diffs": list(self.resolved_diffs),
            "check_results_by_origin": dict(self.origin_counts),
            "event_seq": self.event_seq,
            "event_hash": self.event_hash,
            "channels": [c.to_dict() for c in self.channels],
        }


def default_variables(period: str) -> dict[str, Any]:
    return {"period": period}


def _read_channel_rows(
    channel: str,
    period: str,
    inbox_root: str,
    table,
) -> tuple[list[BillRow], list, list]:
    """读一路账单，复用格式探测 + 脏行隔离（格式判据只有一份）。

    刻意**不复制** ``cli.py`` 里那份实现：格式判据与脏行口径一旦有两份，
    「跑口径时按 A 判格式、排障时按 B 判格式」的两套口径就成立了。

    与 ``verify`` 那条**只读**腿的一处差别：这里用
    ``isolate_empty_required=True``。``scan`` 是**落库**腿，空商户订单号
    的行进不了幂等唯一键（20 行空单号只进 1 行，两行互相冲突）；那种行
    必须进隔离区并在面板上作为「本期 N 行进隔离」显示，而不是让整路
    渠道炸掉、把另外 19 行也一起拖下水。``verify`` 不落库，所以它保留
    这些行交给 ``not_empty`` 口径报出来（那正是财务科要看的差异证据行）。
    """
    from ..sources.format_probe import read_bill_file_isolated

    directory = os.path.join(inbox_root, period, channel)
    if not os.path.isdir(directory):
        return [], [], []
    files = sorted(
        name for name in os.listdir(directory)
        if name.lower().endswith((".csv", ".txt")) and not name.startswith(".")
    )
    rows: list[BillRow] = []
    probes: list = []
    dirty: list = []
    for name in files:
        path = os.path.join(directory, name)
        got, probe, bad = read_bill_file_isolated(
            path,
            table,
            channel,
            period=period,
            isolate_empty_required=True,
        )
        rows.extend(got)
        probes.append(probe)
        dirty.extend(bad)
    return rows, probes, dirty


def _sum_rows_amount(rows: Sequence[BillRow], table) -> str:
    """本路主金额列求和（Decimal 定点）。

    列名按契约的 decimal 列与合计口径取，不硬编码 ``amount``——医保那路
    写的是 ``total_amount``，写死会让它在汇总里显示 0 元。
    """
    columns: list[str] = []
    if table is not None:
        decimals = [c.name for c in table.columns if c.data_type == "decimal"]
        preferred = [c for c in decimals if c in ("amount", "total_amount", "trade_amount")]
        columns = preferred or decimals
    if not columns:
        return "0.00"
    column = columns[0]
    total = Decimal("0")
    for row in rows:
        raw = (row.get(column) or "").strip()
        if not raw:
            continue
        try:
            total += Decimal(raw)
        except Exception:  # 金额解析已在 check 层报行号，这里不重复报
            continue
    return str(total.quantize(Decimal("0.01")))


def scan(
    conn: sqlite3.Connection,
    period: str,
    *,
    channels: Optional[list[str]] = None,
    inbox_root: str = "inbox",
    contracts_dir: str = "contracts",
    variables: Optional[dict[str, Any]] = None,
    session_timezone: Optional[str] = None,
    reopen: bool = False,
    resume: bool = False,
    only_checks: Optional[list[str]] = None,
    actor: str = "",
) -> ScanOutcome:
    """扫一个账期。返回 :class:`ScanOutcome`，不抛「有 fail」这种预期内的错。

    只有**预期外**的失败才抛 :class:`ReconqError`（契约缺失、账期非法、
    状态迁移被拒、六路全废、并发占用）。口径不通过、单渠道没取到这类
    **业务结果**一律体现在 ``gate`` 与 ``channels`` 里，不走异常 ——
    面板要显示的是「哪一路没取到」，不是一个栈。

    Task 19 加的重跑腿（关键词参数，不另开一条代码路径）：

    ``resume``
        从断点继续。已完成的渠道**不重新取数、不重新落库**，其口径
        结果标「沿用上次」，本次跑的那几路标「本次产出」。
    ``channels`` / ``only_checks``
        子集重跑。``only_checks`` 复用 Task 2 的 ``CheckSelector``
        表达式（``row_count`` / ``type=not_empty`` / 通配），不是写死的
        算子名列表。
    """
    from ..sources.registry import CHANNELS
    from ..store.quarantine import init_dirty_schema
    from ..store.schema import init_schema

    init_schema(conn)
    init_dirty_schema(conn)
    init_scan_schema(conn)
    init_resume_schema(conn)

    variables = dict(variables or default_variables(period))

    if reopen:
        # 人工显式撤回：只有这条路径能让已确认的账期重扫。
        existing = require_scan(conn, period) if _has_scan(conn, period) else None
        if existing is not None and existing.status in ("confirmed", "disputed"):
            reopen_scan(conn, period, note="scan(--reopen) 人工撤回以便重扫")

    run_id = uuid.uuid4().hex[:12]
    all_channels = [
        c for c in CHANNELS
        if os.path.isfile(os.path.join(contracts_dir, f"{c}.yml"))
    ]
    if not all_channels:
        raise ReconqError(
            "CONTRACT_MISSING_KEY",
            f"契约目录 {contracts_dir} 下没有任何渠道契约，无法扫描账期 {period}",
        )

    # 范围：先看断点（续跑）再看命令行点名。``select_scope`` 是唯一的
    # 范围判据，CLI、面板与常驻服务三条腿都走它。
    scope = select_scope(
        all_channels,
        only_channels=channels,
        only_checks=only_checks,
        resume=resume,
        checkpoints=load_checkpoints(conn, period) if resume else None,
    )
    target_channels = list(scope.channels) if scope.channels else list(all_channels)

    # 续跑时全部渠道都在断点上 = 本次无事可做。这**不是**「六路全剔除」
    # 那种中止情形：上一轮跑得好好的，只是这会儿没有要补的路。
    # 报一份「本次无新增、全部沿用」的结论，并把这次空跑也记进事件链
    # （否则审计上会出现「财务科说重跑过、日志里却查不到」的窟窿）。
    if not target_channels:
        return _noop_resume(conn, period, run_id, scope, all_channels, actor)

    # 并发保护：同一账期同时只跑一个。抢不到就抛 SCAN_IN_PROGRESS，
    # 不并行开跑 —— 两批 upsert 互相覆盖时合计在中间态上闪烁。
    lock = acquire_scan_lock(
        conn,
        period,
        run_id,
        actor=actor or os.environ.get("RECONQ_ACTOR", ""),
        scope=describe_scope(scope),
    )
    # 记下开扫前的状态：``start_scan`` 会把实例推到「扫描中」，若本次一路
    # 都没跑成（续跑时待跑的那几路全废），降级分支要把它**放回原状态**。
    # 忘了放回的话，账期会永远停在「扫描中」，面板上看起来像刚崩过一次。
    prior_status = None
    if _has_scan(conn, period):
        try:
            prior_status = load_scan(conn, period).status
        except ReconqError:  # pragma: no cover - 表未建时取不到
            prior_status = None
    try:
        return _run_scan(
            conn,
            period,
            run_id=run_id,
            scope=scope,
            target_channels=target_channels,
            all_channels=all_channels,
            lock=lock,
            inbox_root=inbox_root,
            contracts_dir=contracts_dir,
            variables=variables,
            session_timezone=session_timezone,
            actor=actor or os.environ.get("RECONQ_ACTOR", ""),
        )
    except ReconqError as exc:
        # 续跑时**只剩一路待跑**、而它正好取不到（HIS 凭据没配是最常见的一种），
        # 上面那句「全部剔除就中止」在这里是错的：库里已经有前几轮跑完的
        # 结果，这次不产出一份**空**结论单，中止反而把已经算好的东西扔了。
        # 交一份「本轮无新增 + 这一路取不到」的结论，由人决定是补凭据还是
        # 就此作废；不静默当通过，也不把已完成的结果清掉。
        if scope.resume and getattr(exc, "failures", None) and _has_scan(conn, period):
            logger.warning(
                "账期 %s 续跑时待跑的 %d 路全部未取到：%s；已完成的断点结果原样保留",
                period,
                len(target_channels),
                "、".join(sorted({f.error_code for f in exc.failures})),
            )
            noop = _noop_resume(
                conn, period, run_id, scope, all_channels,
                actor or os.environ.get("RECONQ_ACTOR", ""),
                restore_status=prior_status,
            )
            noop.warnings = [
                f"续跑中止：{'、'.join(target_channels)} 未取到（{exc.detail}）；"
                f"已完成的 {len(scope.skipped_channels)} 路结果原样保留，未重算、未消解任何差异"
            ]
            noop.rerun_channels = []
            return noop
        raise
    finally:
        release_scan_lock(lock)


def _run_scan(
    conn: sqlite3.Connection,
    period: str,
    *,
    run_id: str,
    scope: RerunScope,
    target_channels: list[str],
    all_channels: list[str],
    lock,
    inbox_root: str,
    contracts_dir: str,
    variables: dict[str, Any],
    session_timezone: Optional[str],
    actor: str,
) -> ScanOutcome:
    """锁内的那一次扫描本体（与 Task 18 的主路径逐字一致）。"""
    from ..contracts.loader import load_contract
    from ..contracts.selector import CheckSelector, filter_checks
    from ..sources.registry import initialize_all
    from ..store.bill_store import (
        compute_idempotency_key,
        ingest_channel_rows,
        query_period,
        row_summary,
        to_amount_text,
    )
    from ..store.quarantine import insert_dirty_rows
    from ..sources.timezone import period_window

    # 开扫：白名单迁移（已确认的账期在这里被拒，除非 --reopen 刚撤回过）。
    instance = start_scan(
        conn,
        period,
        run_id=run_id,
        idempotency_key=_scan_idempotency_key(period),
        channels_total=len(target_channels),
        note=f"reconq scan · {describe_scope(scope)}",
    )

    outcome_warnings: list[str] = []
    channel_results: list[ChannelScan] = []
    diff_items: list[DiffItem] = []
    upsert_check_rows: list[CheckResultRow] = []
    #: 本次重跑后**仍然 fail** 的口径集合，逐路。范围外的差异消解判断
    #: 靠它 —— 不在集合里的旧差异才标消解。
    fail_paths: dict[str, set[str]] = {}
    #: 本次**实际执行过**的口径集合，逐路。子集重跑时，只有落在这里的
    #: 差异才可能被判消解（没跑过的口径过没过未知）。
    eligible_paths: dict[str, set[str]] = {}

    rows_ingested = 0
    checks_total = 0
    checks_failed = 0
    total_amount = Decimal("0")
    idempotency_keys: list[str] = []

    # —— 逐渠道：initialize → fetch → 落库 → 口径 ————————————
    # 失败剔除语义在这里生效：某一 initialize 失败只影响那一路。
    try:
        init_outcome = initialize_all(
            period, channels=target_channels, inbox_root=inbox_root
        )
    except ReconqError as exc:
        # 本次要跑的路**全被剔除**：整次扫描中止。已在库里的实例停在
        # 「扫描中」，不推进到待确认 ——「什么都没取到」不是一种可结账的状态。
        #
        # 但报错**按本次实际范围说**，不照抄全量扫描那句「六路渠道全部
        # 剔除 / FORMAT_BROKEN」：续跑时范围可能只剩一路，失败原因也可能
        # 是环境变量没配而不是文件损坏。让人照着「账单文件结构损坏」去
        # 查一个 HIS 凭据缺失，是把排障方向直接带偏。
        codes = sorted({f.error_code for f in getattr(exc, "failures", [])}) or [exc.code]
        if len(codes) == 1 and codes[0] != exc.code:
            scoped = ReconqError(
                codes[0],
                f"账期 {period} 本次要跑的 {len(target_channels)} 路渠道"
                f"（{'、'.join(target_channels)}）全部未取到：{exc.detail}",
            )
            # 明细要**跟着走**：外层（续跑的降级分支）靠它判断「待跑的那几路
            # 全废、而已完成的结果还在」。改码不带上它，断点续跑就永远走不到
            # 那个分支。
            scoped.failures = list(getattr(exc, "failures", []))
            raise scoped from exc
        logger.error(
            "账期 %s 本次要跑的 %d 路渠道全部剔除，扫描中止（%s）",
            period,
            len(target_channels),
            "、".join(codes),
        )
        raise

    failed_map = {f.channel: f for f in init_outcome.failures}
    window = period_window(period)

    for channel in target_channels:
        result = ChannelScan(channel=channel)
        channel_results.append(result)
        failure = failed_map.get(channel)
        if failure is not None:
            result.status = "failed"
            result.error_code = failure.error_code
            result.cause_code = failure.cause_code or failure.error_code
            result.detail = failure.detail
            outcome_warnings.append(
                f"{channel} 未取到：[{failure.error_code}] {failure.detail}"
            )
            continue

        contract_path = os.path.join(contracts_dir, f"{channel}.yml")
        try:
            contract = load_contract(contract_path, variables)
            table = contract.table()
            rows, probes, dirty = _read_channel_rows(channel, period, inbox_root, table)
        except ReconqError as exc:
            result.status = "failed"
            result.error_code = exc.code
            result.cause_code = exc.cause_code
            result.detail = exc.detail
            outcome_warnings.append(f"{channel} 取数/契约失败：[{exc.code}] {exc.detail}")
            continue

        if dirty:
            # 落进隔离区台账：面板与结论单要能回答「这 N 行去哪了」，
            # 导出的脏行台账（quarantine export）也要能从同一张表出。
            insert_dirty_rows(conn, dirty)
            result.dirty_rows = len(dirty)
            outcome_warnings.append(
                f"{channel} 本期 {len(dirty)} 行进隔离区（{dirty[0].reason_code} 等），"
                f"未参与对账"
            )

        if not rows:
            result.status = "not_fetched"
            result.detail = (
                f"inbox/{period}/{channel}/ 下没有账单文件"
                if not dirty
                else f"{len(dirty)} 行全部进隔离区，本期无可对账数据"
            )
            outcome_warnings.append(f"{channel} 本期未取到账单：{result.detail}")
            continue

        result.status = "checked"
        result.row_count = len(rows)
        result.total_amount = _sum_rows_amount(rows, table)

        # 幂等落库：行级唯一索引（渠道+账期+账单日+渠道单号）挡重放。
        # 账单日**逐行**由归一时区下的交易时间推出（Task 7 的
        # ``build_record``），不在这里按文件给一个假的统一日期——那样
        # 跨日切分的行会全被塞进同一天，幂等键就分不出两天的批次了。
        upsert = ingest_channel_rows(
            conn,
            rows,
            channel=channel,
            period=period,
            window=window,
        )
        result.inserted = upsert.inserted
        result.skipped_duplicates = upsert.skipped_duplicates
        # 批级幂等键由本层登记（``ingest_channel_rows`` 是流式分批落库，
        # 逐行按自己的账单日推 key，返回值里不汇总）。留一份在渠道结果上，
        # 运营侧才能回答「这一期这一路到底导进去没有」。
        for bill_date in sorted(
            {
                str(row.get("bill_date") or "")
                for row in query_period(conn, period, channel)
            }
        ):
            key = compute_idempotency_key(period, channel, bill_date)
            result.idempotency_keys.append(key)
            idempotency_keys.append(key)
        rows_ingested += upsert.inserted

        # 口径：契约逐条 check，带证据行号。``--only-check`` 给了选择器
        # 就只跑那个子集（Task 2 的 CheckSelector 表达式，不是算子名硬编码）。
        ctx = CheckContext(
            period=period,
            session_timezone=session_timezone
            or contract.variables.get("session_timezone"),
        )
        selected = None
        if scope.check_selectors:
            selected = [
                chk
                for chk, _col in filter_checks(
                    contract, CheckSelector.parse_all(scope.check_selectors)
                )
            ]
            if not selected:
                # 选择器一条都没命中：**整次扫描直接中止**。
                #
                # 这里绝不能「记 0 条 fail 就当这一路过了」：口径名打错一个
                # 字符 → 0 条 check 0 条 fail → 这一路的旧差异在消解那一步
                # 被当成「本次对齐后消失」全部消解掉 → 门禁变绿。财务科
                # 什么都没做，结论单上却写着「差异已消解」——这是本产品
                # 最不能出的错，宁可整次停下让人改选择器。
                raise ReconqError(
                    "CHECK_SELECTOR_EMPTY",
                    f"--only-check 选择器 {'、'.join(scope.check_selectors)} 在 "
                    f"{channel} 契约里一条都没命中（共 {len(list(contract.iter_checks()))} 条口径）；"
                    f"本次扫描中止，已有差异一律不消解 —— "
                    f"把一条都没跑到当成「全部通过」会让旧差异被误消解",
                    channel=channel,
                )
        try:
            results = run_checks(
                contract=contract, rows=rows, selected=selected, context=ctx
            )
        except ReconqError as exc:
            result.status = "failed"
            result.error_code = exc.code
            result.detail = exc.detail
            outcome_warnings.append(f"{channel} 口径执行失败：[{exc.code}] {exc.detail}")
            continue

        result.checks_total = len(results)
        fails = [r for r in results if r.status == "fail"]
        result.checks_failed = len(fails)
        checks_total += len(results)
        checks_failed += len(fails)
        try:
            total_amount += Decimal(result.total_amount)
        except Exception:
            pass

        # Task 19：过没过都记进口径结果表，并标「本次产出」。财务科在
        # 结论单上看到一条「通过」，得知道它是刚跑的还是沿用上周的。
        for item in results:
            first_evidence = item.evidence[0] if item.evidence else None
            upsert_check_rows.append(
                CheckResultRow(
                    period=period,
                    channel=channel,
                    check_path=item.check_path,
                    check_name=item.check_name,
                    operator=item.operator,
                    status=item.status,
                    message=item.message,
                    diff_amount=item.diff_amount,
                    evidence_total=item.evidence_total or len(item.evidence),
                    source_file=getattr(first_evidence, "source_file", "") or "",
                    first_row_no=getattr(first_evidence, "row_no", None),
                    run_id=run_id,
                )
            )
        fail_paths[channel] = {r.check_path for r in fails}
        # 本次**真正执行过**的口径。消解只在这些口径里判：--only-check
        # 没选中的口径这次没跑，「没跑」不是「跑过且通过」。
        eligible_paths[channel] = {r.check_path for r in results}

        for failed in fails:
            first_evidence = failed.evidence[0] if failed.evidence else None
            diff_items.append(
                DiffItem(
                    period=period,
                    channel=channel,
                    check_path=failed.check_path,
                    check_name=failed.check_name,
                    status=failed.status,
                    message=failed.message,
                    diff_amount=failed.diff_amount,
                    evidence_total=failed.evidence_total or len(failed.evidence),
                    source_file=getattr(first_evidence, "source_file", "") or "",
                    first_row_no=getattr(first_evidence, "row_no", None),
                    # 初态恒为「待确认」：确认只能人工点，规则与模型都不得推进。
                    disposition="待确认",
                    needs_attribution=False,
                    run_id=run_id,
                )
            )

        # 断点：这一路跑完就记下来。中途下一路炸了，续跑从断点接着来。
        mark_channel_done(
            conn,
            period,
            channel,
            run_id=run_id,
            checks_done=[r.check_path for r in results],
            rows_done=result.row_count,
            total_amount=result.total_amount,
        )
        heartbeat_scan_lock(lock)

    # —— 未重跑渠道：结果沿用，但明确标出来 ————————————
    # 沿用不是产出。只重跑微信时，银联那几行结果仍在库里可用（面板要显示
    # 「微信 19 行 / 银联 7 行沿用上周」），但**不能**混进「本次落库 N 行」
    # 里 —— 那样周报上的入库量会随着重跑范围忽大忽小，没人解释得清。
    carried = _carried_channel_results(conn, period, target_channels)
    carried_channels = [c.channel for c in carried]

    upsert_check_results(conn, period, upsert_check_rows, run_id=run_id)
    # 出处标记要覆盖**全部**渠道，不只是重跑的那几路：只重跑微信时，银联
    # 那些结果确实是上一轮算的，面板上必须显示「沿用上次」而不是让财务科
    # 以为这一分钟刚跑过。范围内没重跑到的口径同样沿用。
    mark_check_results_carried(
        conn,
        period,
        channels=all_channels,
        keep_keys={(r.channel, r.check_path) for r in upsert_check_rows},
    )

    # 本次重跑范围内不再出现的差异标消解（不删行）。只重跑微信时，银联的
    # 差异没被碰过 —— 不能因为「这次没看见」就消解掉。
    #
    # 消解范围是 ``fail_paths`` 的键，也就是**真正跑出口径**的那些路。
    # 某路取数失败 / 没取到账单时它压根没跑口径，拿它去消解等于把「没查」
    # 当成「查了没问题」——这正是最坏的那种假绿。
    resolved = resolve_missing_diffs(
        conn,
        period,
        channels=list(fail_paths),
        seen_paths=fail_paths,
        eligible_paths=eligible_paths,
        run_id=run_id,
    )

    diffs_written = upsert_diff_items(conn, diff_items)
    upsert_channel_results(conn, period, channel_results, run_id=run_id)
    pending, attribution = count_pending(conn, period)

    # 门禁看的是**全量口径台账**（本次产出 + 沿用），不是「本次跑了几条」。
    # 子集重跑只跑了 2 条口径却拿 checks_failed=0 去判绿，等于把其余 52 条
    # 沿用口径当成没问题 —— 那一期账上一条差异都还在，账却能结。
    origin_counts = count_check_results_by_origin(conn, period)
    ledger_checks_total = sum(origin_counts.values())
    ledger_checks_failed = count_active_failures(conn, period)

    # 本期合计从**账单库**算，不从「本次跑的那几路」累加。
    #
    # 只重跑微信时若按本次累加，本期合计会从 25406.54 掉到 5394.69 ——
    # 账上一笔钱平白「少」了 2 万，而库里没有任何一行数据真的变过。合计在
    # 中间态上闪烁，比报错难查得多。账单库是权威：行级唯一索引保证不重复
    # 入库，库里有多少就是多少。``rows_ingested`` 仍说的是「本次落库多少」。
    ledger_total = sum(
        (
            to_amount_text(bucket["total_amount"])
            for bucket in row_summary(conn, period).values()
        ),
        Decimal("0"),
    )

    checked_count = sum(1 for c in channel_results if c.status == "checked")
    failed_count = sum(
        1 for c in channel_results if c.status in ("failed", "not_fetched")
    )
    finished = finish_scan(
        conn,
        period,
        channels_ok=checked_count,
        channels_failed=failed_count,
        rows_ingested=rows_ingested,
        checks_total=ledger_checks_total,
        checks_failed=ledger_checks_failed,
        unconfirmed=pending,
        pending_attribution=attribution,
        total_amount=str(ledger_total.quantize(Decimal("0.01"))),
        note="、".join(failed_map) or "全渠道成功",
    )
    finished.channels = channel_results + carried
    finished.idempotency_key = _scan_idempotency_key(period)
    finished.idempotency_key = "|".join(idempotency_keys) or finished.idempotency_key

    # —— 重跑事件链：谁、何时、什么范围、结果差几条 ————————————
    # 事件名按**这次实际跑了什么**分，不按命令行长什么样分：首次全量扫描
    # 哪怕命令行上写了 --channel（等于全部六路），也仍是一次 scan；只有
    # 真的只跑了子集才叫 rerun。结论单上「这期是重跑出来的」这句话要么
    # 成立要么不成立，不能因为命令行写法不同就变。
    subset = bool(scope.check_selectors) or bool(carried_channels) or bool(scope.resume)
    event = append_scan_event(
        conn,
        period,
        "rerun" if subset else "scan",
        run_id=run_id,
        actor=actor,
        scope=describe_scope(scope),
        scope_data=scope.to_dict(),
        result=(
            f"本次 {checked_count} 路 · 落库 {rows_ingested} 行 · "
            f"口径 {checks_total} 条（未通过 {checks_failed} 条）· "
            f"消解 {len(resolved)} 条 · 沿用 {len(carried_channels)} 路"
        ),
        detail=f"门禁 {finished.gate}：{finished.reason}",
    )

    logger.info(
        "账期 %s 扫描完成：门禁=%s（%s），%d/%d 路成功，%d 条差异待确认，消解 %d 条",
        period,
        finished.gate,
        finished.reason,
        checked_count,
        len(target_channels),
        pending,
        len(resolved),
    )
    # 结构化事件（Task 20）：与上面那条人类可读日志**并存**，不是替代。
    # 文本日志给运维在终端上扫一眼，JSON 事件给工单系统按 event/channel
    # 聚合统计——「本月 HIS 那一路失败了几次、成因码分布」这种问题，
    # 只有后者答得上来。
    from ..runlog import log_event

    for item in channel_results:
        log_event(
            "channel.fetch.done" if item.status == "checked" else "channel.fetch.failed",
            channel=item.channel,
            period=period,
            message=item.detail or f"{item.channel}：{item.status}",
            code=item.error_code or item.cause_code or "",
            row_count=item.row_count,
            total_amount=item.total_amount,
            batches=len(item.idempotency_keys),
            dirty_rows=item.dirty_rows,
            checks_total=item.checks_total,
            checks_failed=item.checks_failed,
        )
    log_event(
        "scan.gate",
        period=period,
        message=f"账期 {period} 门禁 {finished.gate}：{finished.reason}",
        gate=finished.gate,
        channels_ok=checked_count,
        channels_failed=len(target_channels) - checked_count,
        rows_ingested=rows_ingested,
        unconfirmed=pending,
        resolved=len(resolved),
    )
    return ScanOutcome(
        period=period,
        run_id=run_id,
        scan=finished,
        channels=channel_results + carried,
        diffs_written=diffs_written,
        warnings=outcome_warnings,
        scope=scope,
        carried_channels=carried_channels,
        rerun_channels=list(target_channels),
        resolved_diffs=resolved,
        origin_counts=origin_counts,
        event_seq=event.seq,
        event_hash=event.hash,
        resumed=bool(scope.resume),
    )


def _noop_resume(
    conn: sqlite3.Connection,
    period: str,
    run_id: str,
    scope: RerunScope,
    all_channels: Sequence[str],
    actor: str,
    restore_status: Optional[str] = None,
) -> ScanOutcome:
    """断点续跑但已无待跑渠道（或待跑的全废）：交一份「本次无新增」结论。

    刻意**不**动账期实例的判定结果、也**不**改任何差异行：没有新证据就
    没有新结论。此时把状态重推一遍（哪怕推到同一个值）会在事件链上多出
    一条看不出差别的记录，反而让「这一期到底被重跑过没有」变难查。

    ``restore_status`` 只有一个用途：``start_scan`` 已经把实例推到「扫描
    中」了，若本次一路都没跑成就中止，实例会**永远**停在「扫描中」——
    面板上看起来像刚崩过一次，而实际上什么都没发生。放回开扫前的状态，
    这一期才算「没动过」。
    """
    existing = require_scan(conn, period)
    if restore_status and existing.status != restore_status:
        logger.info(
            "账期 %s 本次未产出任何结果，把实例从「%s」放回「%s」",
            period,
            existing.status_zh,
            restore_status,
        )
        existing = transition(
            conn,
            period,
            restore_status,
            note=f"本次续跑未产出（待跑渠道全废），状态放回「{STATUS_ZH.get(restore_status, restore_status)}」",
        )
    origin_counts = count_check_results_by_origin(conn, period)
    event = append_scan_event(
        conn,
        period,
        "resume",
        run_id=run_id,
        actor=actor,
        scope=describe_scope(scope),
        scope_data=scope.to_dict(),
        result=f"本次无待跑渠道 · {'、'.join(scope.skipped_channels) or '无'} 已在断点上",
        detail=(
            f"账期状态保持「{existing.status_zh}」，未重跑、未重算、未消解任何差异"
        ),
    )
    existing.channels = list_channel_results(conn, period)
    return ScanOutcome(
        period=period,
        run_id=run_id,
        scan=existing,
        channels=existing.channels,
        diffs_written=0,
        warnings=[],
        scope=scope,
        carried_channels=list(scope.skipped_channels),
        rerun_channels=[],
        resolved_diffs=[],
        origin_counts=origin_counts,
        event_seq=event.seq,
        event_hash=event.hash,
        resumed=True,
    )


def _carried_channel_results(
    conn: sqlite3.Connection,
    period: str,
    rerun_channels: Sequence[str],
) -> list[ChannelScan]:
    """本次没重跑的渠道：从库里取回上轮结果，作为「沿用」附在结果里。

    只在**确实是子集重跑 / 续跑**时才回捞（全量扫描时列表必为空），
    否则每轮都白查一次库。沿用结果**不进** ``rows_ingested`` 与
    ``checks_*`` 汇总 —— 那几个数说的是「本次跑了多少」。
    """
    if not rerun_channels:
        return []
    try:
        previous = {c.channel: c for c in list_channel_results(conn, period)}
    except ReconqError:
        return []
    return [
        ChannelScan(
            channel=ch,
            status=prev.status,
            row_count=prev.row_count,
            total_amount=prev.total_amount,
            checks_total=prev.checks_total,
            checks_failed=prev.checks_failed,
            inserted=0,
            skipped_duplicates=0,
            detail=f"本次未重跑，沿用上次结果（原状态 {prev.status}）",
        )
        for ch, prev in sorted(previous.items())
        if ch not in rerun_channels
    ]



def _has_scan(conn: sqlite3.Connection, period: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='period_scans'"
    ).fetchone()
    if row is None:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM period_scans WHERE period=?", (period,)
        ).fetchone()
        is not None
    )


def scan_status(conn: sqlite3.Connection, period: str) -> dict[str, Any]:
    """只读地回答「这期账现在到哪一步」，供 CLI / 面板 / 结论单共用。

    渠道主态从**库里**读而不是从内存拿 —— 常驻服务重启后面板要照常显示
    「微信 19 行 5394.69 元 / HIS 未取到」。
    """
    from .period import list_channel_results, load_scan

    try:
        scan_obj = load_scan(conn, period)
    except ReconqError:
        # 账期实例表都还没建（第一次部署的空库）：答案就是「未扫描」，
        # 不是报错 —— 「查这期扫过没有」不该在空库上抛异常。
        scan_obj = None
    if scan_obj is None:
        return {
            "period": period,
            "status": "unscanned",
            "status_zh": "未扫描",
            "gate": None,
            "channels": [],
        }
    try:
        scan_obj.channels = list_channel_results(conn, period)
    except ReconqError:
        scan_obj.channels = []
    payload = scan_obj.to_dict()
    payload["can_close"] = scan_obj.gate == "green"
    return payload
