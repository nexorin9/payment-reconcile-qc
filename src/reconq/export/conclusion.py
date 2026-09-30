"""周对账结论单：装配、渲染与导出（Task 44）。

与源产品的关系
--------------
参考实现 ``soda-core/soda-core/src/soda_core/cli/cli.py`` 的主路径
``contract verify``（:144）把逐 check 的结论**打到终端**然后
``exit_with_code()``（:291）—— 进程退出即消失，没有可归档的纸面产物，
也没有「谁在什么时候依据哪几行签的字」这一层。源产品另有
``contract publish``（:338）推的是**契约本身**，不是某次执行的结论。

本模块把同一份结论渲染成**可签字归档的结论单**：

* 文首**指纹头** —— 账期、六路主态与笔数合计、本期事件链根指纹、
  契约指纹、模型与提示词版本、导出人与导出时刻；
* 正文**按成因分组出证明**：组级结论、组内条数、涉及金额、确认人、
  确认时间、复核人；组内逐条附证据行号与快照引用；
* 附录四类 —— 跨账期连坐链、未取到渠道及错误码、脏行统计与隔离区
  文件指针、模型降级说明与规则基线占比。

幂等
----
同一账期同一内容重复导出**内容一致**（:func:`conclusion_fingerprint`
只吃业务内容，不吃导出时刻与导出人）；账期状态一变，导出内容与指纹
**同时变**。导出时刻因此单独作为「本次导出元数据」返回，不进指纹。

脱敏
----
结论单是打印出来交给财务科、可能再交给渠道方的纸面文件，
:func:`assert_deidentified` 在**落盘前**逐键逐值核对：出现患者姓名 /
住院号 / 身份证 / 手机号这类字段或形态直接抛错，不靠「导出时记得别带」。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional, Sequence

from .. import __version__
from ..align.aggregate import (
    AGGREGATE_VERSION,
    AggregationResult,
    DispositionGroup,
    aggregate_discrepancies,
    expand_group,
    load_group_entries,
)
from ..align.causes import CAUSE_DICT_VERSION, dictionary_stamp, entry_of
from ..align.snapshot import (
    FORBIDDEN_PATIENT_KEYS,
    latest_alignment_snapshot,
)
from ..align.trace import GLOBAL_CHANNEL
from ..errors import ReconqError
from ..ledger import flow as ledger_flow
from ..ledger.models import LEDGER_VERSION

__all__ = [
    "CONCLUSION_FORMATS",
    "ConclusionChannel",
    "ConclusionDoc",
    "ConclusionGroupView",
    "ConclusionLine",
    "ExportOutcome",
    "build_conclusion",
    "assert_sheet_deidentified",
    "conclusion_fingerprint",
    "export_conclusion",
    "render_conclusion_csv",
    "render_conclusion_markdown",
    "render_sheet",
]

#: 支持的结论单格式。**面板与 CLI 的下拉共用这一份**，不各写一个列表 ——
#: 少写一处的代价是「CLI 说支持 csv、渲染函数里没有 csv 分支」。
CONCLUSION_FORMATS: tuple[str, ...] = ("md", "csv")

#: 结论单版式版本。版式改了（分组顺序、附录条目）必须 bump：三个月后
#: 有人拿着两期结论单对比时，得能一眼看出「这期的单子和上期不是一套版式」。
CONCLUSION_SHEET_VERSION = "conclusion-sheet/1"

#: 默认输出目录。与 ``quarantine export`` 的 ``out/`` 同一处 —— 财务科
#: 在同一个文件夹里找得到脏行台账与结论单。
DEFAULT_OUT_DIR = "out"

#: 跨账期连坐链在附录里的追查半径（中心账期上下各几期）。
DEFAULT_TRACE_WINDOW = 2


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _safe_decimal(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except Exception:
        # 台账里的金额是定点串；解析不了说明这一格被写坏了，
        # 结论单上按「未给金额」出，而不是把合计算错。
        return None


def assert_sheet_deidentified(payload: Any, *, where: str = "结论单") -> None:
    """核对结论单里**没有患者可识别信息**。命中即抛错，不静默替换。

    与 :func:`reconq.align.snapshot.assert_deidentified` 的分工：

    * 那个核对**结构**（键名 + 值整体形态），管快照这类**已分列**的载荷；
    * 这个核对**文本**（值内部任意位置出现的形态），管结论单上的自由
      文本栏 —— 经办结论、渠道说明这类一整句话。

    只做结构核对会漏掉最危险的一种：有人把身份证号写进「经办结论」
    （``已联系住院号 110105199001011234 的家属确认``）。那个串整体不是
    18 位、也不是纯手机号，锚定正则看不见它，但它会被打印出来交给
    财务科、可能再交给渠道方。

    复用 :func:`reconq.runlog.redact_text` 的模式表而不是另写一套 ——
    日志与结论单各写一份正则，改了一处忘了另一处，脱敏就只剩一半。
    """
    from ..runlog import redact_text

    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).strip().lower() in FORBIDDEN_PATIENT_KEYS:
                raise ReconqError(
                    "SHEET_NOT_DEIDENTIFIED",
                    f"{where} 的字段 {key!r} 是患者可识别字段：结论单只带"
                    "金额、单号、账期、证据行号与处理人，不带患者明细",
                )
            assert_sheet_deidentified(value, where=f"{where}.{key}")
        return
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_sheet_deidentified(item, where=f"{where}[{index}]")
        return
    if isinstance(payload, str):
        _, hits = redact_text(payload)
        if hits:
            raise ReconqError(
                "SHEET_NOT_DEIDENTIFIED",
                f"{where} 的文本命中患者标识形态 {sorted(set(hits))}："
                "结论单会打印出来交给财务科并可能转交渠道方，患者信息不能随单流转。"
                "请改写成「已与收费处核对差异原因」这类不含患者标识的表述",
            )


# ─────────────────────────── 数据件 ───────────────────────────


@dataclass(frozen=True)
class ConclusionChannel:
    """一路渠道在结论单上的主态。"""

    channel: str
    status: str
    status_zh: str
    row_count: int = 0
    total_amount: str = "0.00"
    checks_total: int = 0
    checks_failed: int = 0
    dirty_rows: int = 0
    error_code: str = ""
    detail: str = ""

    @property
    def fetched(self) -> bool:
        return self.status == "checked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "status": self.status,
            "status_zh": self.status_zh,
            "fetched": self.fetched,
            "row_count": self.row_count,
            "total_amount": self.total_amount,
            "checks_total": self.checks_total,
            "checks_failed": self.checks_failed,
            "dirty_rows": self.dirty_rows,
            "error_code": self.error_code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ConclusionLine:
    """处置单里的一条差异（结论单正文的一行）。"""

    key: str
    order_no: str
    channel: str
    amount_text: str
    #: 台账处置状态（聚合层口径，``待确认`` / ``已确认`` / ``已驳回``）。
    disposition: str = ""
    #: 三角色会签口径的状态（可能带「待主管复核」这一关卡）。
    ledger_status: str = ""
    handler: str = ""
    handler_at: str = ""
    reviewer: str = ""
    reviewer_at: str = ""
    supervisor: str = ""
    supervisor_at: str = ""
    #: 经办人写下的处置结论原文。**与模型草稿分开** —— 结论单上要能
    #: 看出人有没有采信模型。
    conclusion: str = ""
    cause_draft: str = ""
    evidence: tuple[str, ...] = ()
    snapshot_ref: str = ""
    ambiguity_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "order_no": self.order_no,
            "channel": self.channel,
            "amount_text": self.amount_text,
            "disposition": self.disposition,
            "ledger_status": self.ledger_status,
            "handler": self.handler,
            "handler_at": self.handler_at,
            "reviewer": self.reviewer,
            "reviewer_at": self.reviewer_at,
            "supervisor": self.supervisor,
            "supervisor_at": self.supervisor_at,
            "conclusion": self.conclusion,
            "cause_draft": self.cause_draft,
            "evidence": list(self.evidence),
            "snapshot_ref": self.snapshot_ref,
            "ambiguity_kind": self.ambiguity_kind,
        }


@dataclass(frozen=True)
class ConclusionGroupView:
    """一张处置单在结论单上的样子：组头 + 组内逐条。"""

    group_key: str
    cause_code: str
    cause_zh: str
    owner: str
    channel: str
    disposition: str
    bucket: str
    bucket_text: str
    suggested_action: str = ""
    split_reason: str = ""
    lines: tuple[ConclusionLine, ...] = ()

    @property
    def size(self) -> int:
        return len(self.lines)

    @property
    def total_amount(self) -> Decimal:
        total = Decimal("0")
        for line in self.lines:
            value = _safe_decimal(line.amount_text)
            if value is not None:
                total += value
        return total

    @property
    def total_amount_text(self) -> str:
        return f"{self.total_amount:.2f}"

    @property
    def group_conclusion(self) -> str:
        """组级结论。

        组内各条的经办结论**一致**时直接用那一句；不一致时如实写
        「组内 N 条结论不一致」，而不是替它们取一条当组结论 ——
        取任意一条当组结论，等于让签字的人为没看过的结论背书。
        """
        texts = {line.conclusion.strip() for line in self.lines if line.conclusion.strip()}
        if not texts:
            return ""
        if len(texts) == 1:
            return next(iter(texts))
        return f"组内 {len(texts)} 条经办结论不一致，需逐条复核"

    def signoff(self, field_name: str) -> tuple[str, str]:
        """取某一列会签信息（人、时刻）。组内多人时按首次出现顺序去重。"""
        who: list[str] = []
        when: list[str] = []
        for line in self.lines:
            person = getattr(line, field_name).strip()
            stamp = getattr(line, f"{field_name}_at").strip()
            if person and person not in who:
                who.append(person)
            if stamp and stamp not in when:
                when.append(stamp)
        return ("、".join(who), "、".join(when))

    def to_dict(self) -> dict[str, Any]:
        handler, handler_at = self.signoff("handler")
        reviewer, reviewer_at = self.signoff("reviewer")
        supervisor, supervisor_at = self.signoff("supervisor")
        return {
            "group_key": self.group_key,
            "cause_code": self.cause_code,
            "cause_zh": self.cause_zh,
            "owner": self.owner,
            "channel": self.channel,
            "disposition": self.disposition,
            "bucket": self.bucket,
            "bucket_text": self.bucket_text,
            "suggested_action": self.suggested_action,
            "split_reason": self.split_reason,
            "size": self.size,
            "total_amount": self.total_amount_text,
            "group_conclusion": self.group_conclusion,
            "handler": handler,
            "handler_at": handler_at,
            "reviewer": reviewer,
            "reviewer_at": reviewer_at,
            "supervisor": supervisor,
            "supervisor_at": supervisor_at,
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass(frozen=True)
class ConclusionDoc:
    """一份完整的周对账结论单。"""

    period: str
    channels: tuple[ConclusionChannel, ...] = ()
    groups: tuple[ConclusionGroupView, ...] = ()
    #: 账期实例状态与门禁（``已确认`` / ``待确认`` / ``已存疑`` …）。
    period_status: str = ""
    period_status_zh: str = ""
    gate: str = ""
    gate_reason: str = ""
    #: 本期事件链根指纹（链尾 hash）与环数。
    chain_root: str = ""
    chain_length: int = 0
    #: 审计事件链的**链头锚点**（Task 45）。与 :attr:`chain_root` 是两件事：
    #: 链尾覆盖「当前内容」，锚点覆盖「这条链从哪儿开始」—— 只留链尾的话，
    #: 开头被换过而重接成自洽链的情形查不出来。没建审计目录时为空串。
    chain_anchor: str = ""
    #: 本期跑的口径指纹（按渠道）。
    contract_digests: tuple[tuple[str, str], ...] = ()
    contract_needs_reconfirm: tuple[str, ...] = ()
    #: 模型 / 提示词 / 规则基线 / 字典版本。
    model_names: tuple[str, ...] = ()
    prompt_versions: tuple[str, ...] = ()
    model_calls: int = 0
    model_degraded: int = 0
    model_errors: tuple[tuple[str, int], ...] = ()
    rule_baseline_ratio: str = "未统计"
    #: 跨账期连坐链。
    trace_chains: tuple[tuple[str, str], ...] = ()
    #: 脏行统计（按渠道）与隔离区文件指针。
    dirty_by_channel: tuple[tuple[str, int], ...] = ()
    dirty_total: int = 0
    quarantine_file: str = ""
    #: 指纹头与版本信息。
    sheet_version: str = CONCLUSION_SHEET_VERSION
    product_version: str = __version__
    ledger_version: str = LEDGER_VERSION
    aggregate_version: str = AGGREGATE_VERSION
    cause_dict_version: str = CAUSE_DICT_VERSION
    cause_dict_stamp: str = ""
    #: 结论集指纹（**不含**导出时刻与导出人）。
    fingerprint: str = ""
    #: 本次导出的元数据（**不进指纹**）。
    exported_at: str = ""
    exported_by: str = ""
    note: str = ""

    @property
    def channels_ok(self) -> int:
        return sum(1 for c in self.channels if c.fetched)

    @property
    def groups_count(self) -> int:
        return len(self.groups)

    @property
    def lines_count(self) -> int:
        return sum(g.size for g in self.groups)

    @property
    def total_amount(self) -> Decimal:
        return sum((g.total_amount for g in self.groups), Decimal("0"))

    @property
    def missing_channels(self) -> tuple[ConclusionChannel, ...]:
        return tuple(c for c in self.channels if not c.fetched)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "sheet_version": self.sheet_version,
            "product_version": self.product_version,
            "ledger_version": self.ledger_version,
            "aggregate_version": self.aggregate_version,
            "cause_dict_version": self.cause_dict_version,
            "cause_dict_stamp": self.cause_dict_stamp,
            "fingerprint": self.fingerprint,
            "exported_at": self.exported_at,
            "exported_by": self.exported_by,
            "note": self.note,
            "period_status": self.period_status,
            "period_status_zh": self.period_status_zh,
            "gate": self.gate,
            "gate_reason": self.gate_reason,
            "chain_root": self.chain_root,
            "chain_length": self.chain_length,
            "chain_anchor": self.chain_anchor,
            "contract_digests": [list(x) for x in self.contract_digests],
            "contract_needs_reconfirm": list(self.contract_needs_reconfirm),
            "model_names": list(self.model_names),
            "prompt_versions": list(self.prompt_versions),
            "model_calls": self.model_calls,
            "model_degraded": self.model_degraded,
            "model_errors": [list(x) for x in self.model_errors],
            "rule_baseline_ratio": self.rule_baseline_ratio,
            "trace_chains": [list(x) for x in self.trace_chains],
            "dirty_by_channel": [list(x) for x in self.dirty_by_channel],
            "dirty_total": self.dirty_total,
            "quarantine_file": self.quarantine_file,
            "channels": [c.to_dict() for c in self.channels],
            "groups": [g.to_dict() for g in self.groups],
            "summary": {
                "channels_ok": self.channels_ok,
                "channels_total": len(self.channels),
                "groups": self.groups_count,
                "lines": self.lines_count,
                "total_amount": f"{self.total_amount:.2f}",
            },
        }


@dataclass(frozen=True)
class ExportOutcome:
    """导出结果。"""

    path: str
    fmt: str
    doc: ConclusionDoc
    #: 实际写出的字节数。
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "fmt": self.fmt,
            "size": self.size,
            "period": self.doc.period,
            "fingerprint": self.doc.fingerprint,
            "groups": self.doc.groups_count,
            "lines": self.doc.lines_count,
            "total_amount": f"{self.doc.total_amount:.2f}",
        }


# ─────────────────────────── 指纹 ───────────────────────────


def conclusion_fingerprint(doc: ConclusionDoc) -> str:
    """结论集指纹（sha256 全长）。

    **只吃业务内容**：六路主态、处置单与逐条会签、证据与快照引用、
    附录四类、口径与版本。刻意**不含**导出时刻与导出人 —— 那两个字段
    每次导出都变，进了指纹「同内容重复导出指纹相同」这条不变量就永远
    测不出来，而这恰恰是最该守住的一条（财务科重导一次要能确认
    「结论一个字没改」）。
    """
    payload = {
        "period": doc.period,
        "sheet_version": doc.sheet_version,
        "ledger_version": doc.ledger_version,
        "aggregate_version": doc.aggregate_version,
        "cause_dict_version": doc.cause_dict_version,
        "cause_dict_stamp": doc.cause_dict_stamp,
        "period_status": doc.period_status,
        "gate": doc.gate,
        "chain_root": doc.chain_root,
        "chain_length": doc.chain_length,
        "chain_anchor": doc.chain_anchor,
        "contract_digests": [list(x) for x in doc.contract_digests],
        "contract_needs_reconfirm": list(doc.contract_needs_reconfirm),
        "model_names": list(doc.model_names),
        "prompt_versions": list(doc.prompt_versions),
        "model_calls": doc.model_calls,
        "model_degraded": doc.model_degraded,
        "model_errors": [list(x) for x in doc.model_errors],
        "rule_baseline_ratio": doc.rule_baseline_ratio,
        "trace_chains": [list(x) for x in doc.trace_chains],
        "dirty_by_channel": [list(x) for x in doc.dirty_by_channel],
        "channels": [c.to_dict() for c in doc.channels],
        "groups": [g.to_dict() for g in doc.groups],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ─────────────────────────── 装配 ───────────────────────────


def _load_channels(conn: sqlite3.Connection, period: str) -> tuple[ConclusionChannel, ...]:
    """读六路主态。**表不在时返回空**（还没扫过的账期不该把导出腿炸掉）。"""
    from ..engine.period import CHANNEL_RESULT_TABLE, list_channel_results
    from ..run_summary import STATUS_ZH as CHANNEL_STATUS_ZH

    if not _table_exists(conn, CHANNEL_RESULT_TABLE):
        return ()
    out: list[ConclusionChannel] = []
    for item in list_channel_results(conn, period):
        out.append(
            ConclusionChannel(
                channel=item.channel,
                status=item.status,
                status_zh=CHANNEL_STATUS_ZH.get(item.status, item.status),
                row_count=item.row_count,
                total_amount=item.total_amount,
                checks_total=item.checks_total,
                checks_failed=item.checks_failed,
                dirty_rows=item.dirty_rows,
                error_code=item.error_code or "",
                detail=item.detail or "",
            )
        )
    return tuple(out)


def _load_period_state(conn: sqlite3.Connection, period: str) -> tuple[str, str, str, str]:
    from ..engine.period import SCAN_TABLE

    if not _table_exists(conn, SCAN_TABLE):
        return ("", "", "", "")
    row = conn.execute(
        f"SELECT status, gate, reason FROM {SCAN_TABLE} WHERE period=?", (period,)
    ).fetchone()
    if row is None:
        return ("", "", "", "")
    from ..engine.period import STATUS_ZH

    return (str(row["status"]), STATUS_ZH.get(str(row["status"]), str(row["status"])),
            str(row["gate"] or ""), str(row["reason"] or ""))


def _load_chain(conn: sqlite3.Connection, period: str) -> tuple[str, int]:
    """本期事件链的**根指纹**（链尾 hash）与环数。

    取链尾而不是链头：链上每一环的指纹都把上一环的算进去，所以链尾
    一个值就能覆盖整条链（改中间任何一环，链尾必变）。Task 45 补全量
    审计与按账期切片检索时，结论单引用的就是这个锚点。
    """
    from ..engine.resume import SCAN_EVENT_TABLE

    if not _table_exists(conn, SCAN_EVENT_TABLE):
        return ("", 0)
    rows = conn.execute(
        f"SELECT hash FROM {SCAN_EVENT_TABLE} WHERE period=? ORDER BY seq", (period,)
    ).fetchall()
    if not rows:
        return ("", 0)
    return (str(rows[-1]["hash"] or ""), len(rows))


def _load_chain_anchor(audit_dir: str, period: str) -> str:
    """审计事件链的链头锚点（Task 45）。

    **取不到就留空，不报错。** 审计目录是可选的：没建链的账期照样能导出
    结论单，指纹头那一行显示「未建审计链」。反过来，审计链建了却因为目录
    传错而读不到锚点，同样不该拦住导出 —— 拦住的话，「目录传错」会表现成
    「这一期导不出结论单」，方向完全错。
    """
    if not audit_dir:
        return ""
    try:
        from ..audit.query import chain_anchor

        return chain_anchor(audit_dir, period)
    except ReconqError:
        return ""


def _load_contracts(conn: sqlite3.Connection, period: str) -> tuple[
    tuple[tuple[str, str], ...], tuple[str, ...]
]:
    from ..store.snapshots import BINDING_TABLE, NEEDS_RECONFIRM

    if not _table_exists(conn, BINDING_TABLE):
        return ((), ())
    digests: list[tuple[str, str]] = []
    needs: list[str] = []
    for row in conn.execute(
        f"SELECT channel, check_digest, contract_digest, status FROM {BINDING_TABLE} "
        "WHERE period=? ORDER BY channel",
        (period,),
    ):
        digests.append((str(row["channel"]), str(row["check_digest"] or row["contract_digest"] or "")))
        if str(row["status"]) == NEEDS_RECONFIRM:
            needs.append(str(row["channel"]))
    return (tuple(digests), tuple(needs))


def _load_model_usage(
    conn: sqlite3.Connection, period: str
) -> tuple[tuple[str, ...], tuple[str, ...], int, int, tuple[tuple[str, int], ...]]:
    from ..audit.model_calls import MODEL_CALL_TABLE

    if not _table_exists(conn, MODEL_CALL_TABLE):
        return ((), (), 0, 0, ())
    rows = conn.execute(
        f"SELECT model, prompt_version, status, error_code FROM {MODEL_CALL_TABLE} "
        "WHERE period=? ORDER BY seq",
        (period,),
    ).fetchall()
    models: list[str] = []
    versions: list[str] = []
    degraded = 0
    errors: dict[str, int] = {}
    for row in rows:
        for field_name, bucket in (("model", models), ("prompt_version", versions)):
            value = str(row[field_name] or "")
            if value and value not in bucket:
                bucket.append(value)
        if str(row["status"]) != "ok":
            degraded += 1
            code = str(row["error_code"] or "UNKNOWN")
            errors[code] = errors.get(code, 0) + 1
    return (
        tuple(models),
        tuple(versions),
        len(rows),
        degraded,
        tuple(sorted(errors.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


def _rule_baseline_ratio(conn: sqlite3.Connection, period: str) -> str:
    """规则基线在归因草稿里的占比。

    口径：「Top-1 成因**不是模型说的**的条数 / 有 Top-1 成因的条数」。
    ``model_name`` 为空或等于规则基线标记（``rules_baseline``）即算规则
    侧 —— 归因降级时规则基线接管，这一格留的就是那个标记或空。

    分母**只算有草稿的**：没草稿的条目既不是模型说的也不是规则说的，
    混进分母会让模型看起来比实际更能干。
    """
    from ..align.baseline import RULE_SOURCE

    table = "attribution_drafts"
    if not _table_exists(conn, table):
        return "未统计（本期无归因草稿）"
    row = conn.execute(
        f"SELECT COUNT(*) AS n, "
        f"SUM(CASE WHEN model_name IS NULL OR model_name='' OR model_name=? THEN 1 "
        f"ELSE 0 END) AS rule_side "
        f"FROM {table} WHERE period=? AND rank=1",
        (RULE_SOURCE, period),
    ).fetchone()
    total = int(row["n"] or 0)
    if not total:
        return "本期无归因草稿"
    rule_side = int(row["rule_side"] or 0)
    ratio = (Decimal(rule_side) * 100 / Decimal(total)).quantize(Decimal("0.1"))
    return f"{ratio}%（{rule_side}/{total} 条 Top-1 成因来自规则基线，未经模型）"


def _load_trace_chains(
    conn: sqlite3.Connection, period: str, *, window: int = DEFAULT_TRACE_WINDOW
) -> tuple[tuple[str, str], ...]:
    """跨账期连坐链（附录一）。

    读**台账里已经落下的连坐结论**（``check_path`` 以 ``trace.`` 开头），
    而不是现场重跑一遍追查 —— 重跑要按 (单号, 账期) 复合索引扫六路，
    一张结论单上几百条差异会把导出拖成一次全量对账；而台账里那几条
    正是扫描当时对齐器给出的结论，签字要签的也是那几条。
    """
    from ..engine.period import DIFF_TABLE, list_diff_items

    if not _table_exists(conn, DIFF_TABLE):
        return ()
    chains: list[tuple[str, str]] = []
    for item in list_diff_items(conn, period):
        if not str(item.check_path).startswith("trace."):
            continue
        # 台账行不带 order_no 列 —— 连坐结论的单号编在 ``check_path`` 的
        # ``trace.<形态>:<单号>`` 里（见 trace.chain_to_diff_items）。
        # 从那儿取，而不是给台账加一列。
        _, _, order_no = str(item.check_path).partition(":")
        chains.append(
            (
                item.check_name or item.check_path,
                f"{order_no or item.check_path} · {item.message or '—'}",
            )
        )
    return tuple(chains)


def _load_dirty(conn: sqlite3.Connection, period: str) -> tuple[
    tuple[tuple[str, int], ...], int, str
]:
    from ..store.quarantine import DIRTY_TABLE

    quarantine_file = os.path.join(DEFAULT_OUT_DIR, f"{period}-脏行台账.csv")
    if not _table_exists(conn, DIRTY_TABLE):
        return ((), 0, quarantine_file)
    by_channel = conn.execute(
        f"SELECT channel, COUNT(*) AS n FROM {DIRTY_TABLE} WHERE period=? GROUP BY channel "
        "ORDER BY channel",
        (period,),
    ).fetchall()
    rows = tuple((str(r["channel"]), int(r["n"])) for r in by_channel)
    return (rows, sum(n for _, n in rows), quarantine_file)


def _load_ledger_view(
    conn: sqlite3.Connection, period: str, key: str
) -> Optional[Any]:
    """读一条差异的会签信息。台账表不在时返回 ``None``（正常态）。"""
    if not _table_exists(conn, "ledger_discrepancies"):
        return None
    return ledger_flow.load(conn, key)


def _cause_zh(code: str) -> str:
    if not code:
        return "成因未定"
    try:
        return entry_of(code).zh_name
    except ReconqError:
        # 字典外的成因码（历史数据里可能有）**如实显示原码**而不是
        # 悄悄并到最近的一类 —— 「上周那批『渠道晚同步』和这周的
        # 『延迟入账』是不是同一件事」这个问题答不出来，正是并掉的代价。
        return code


def _snapshot_ref(conn: sqlite3.Connection, period: str, conclusion_key: str) -> str:
    if not conclusion_key or not _table_exists(conn, "align_snapshots"):
        return ""
    snapshot = latest_alignment_snapshot(conn, period, conclusion_key)
    if snapshot is None:
        return ""
    return f"快照 {snapshot.content_hash[:12]}"


def _to_group_view(
    conn: sqlite3.Connection,
    group: DispositionGroup,
    *,
    period: str,
) -> ConclusionGroupView:
    lines: list[ConclusionLine] = []
    for member in expand_group(group):
        conclusion_key = member.conclusion_key()
        ledger_item = _load_ledger_view(conn, period, member.key)
        draft = member.cause_code or ""
        lines.append(
            ConclusionLine(
                key=member.key,
                order_no=member.order_no or "",
                channel=member.channel,
                amount_text=member.amount_text(),
                disposition=member.disposition,
                ledger_status=getattr(ledger_item, "status", "") or "",
                handler=getattr(ledger_item, "handler", "") or "",
                handler_at=getattr(ledger_item, "handler_at", "") or "",
                reviewer=getattr(ledger_item, "reviewer", "") or "",
                reviewer_at=getattr(ledger_item, "reviewer_at", "") or "",
                supervisor=getattr(ledger_item, "supervisor", "") or "",
                supervisor_at=getattr(ledger_item, "supervisor_at", "") or "",
                conclusion=getattr(ledger_item, "conclusion", "") or "",
                cause_draft=draft,
                evidence=tuple(e.ref for e in member.evidence),
                snapshot_ref=_snapshot_ref(conn, period, conclusion_key),
                ambiguity_kind=member.ambiguity_kind,
            )
        )
    return ConclusionGroupView(
        group_key=group.group_key,
        cause_code=group.cause_code,
        cause_zh=_cause_zh(group.cause_code),
        owner=group.owner or "待人工判定",
        channel=group.channel,
        disposition=group.disposition,
        bucket=group.amount_bucket,
        bucket_text=group.amount_range_text(),
        suggested_action=group.suggested_action or "",
        split_reason=group.split_reason,
        lines=tuple(lines),
    )


def build_conclusion(
    conn: sqlite3.Connection,
    period: str,
    *,
    exported_by: str = "",
    note: str = "",
    now: Optional[str] = None,
    amount_bucket: str = "0.01",
    trace_window: int = DEFAULT_TRACE_WINDOW,
    audit_dir: str = "",
) -> ConclusionDoc:
    """装配一个账期的结论单（不落盘）。

    读的都是**已落库的事实**：渠道主态、差异台账、归因草稿、组级留痕、
    三角色会签、证据快照、事件链、口径绑定、模型审计、隔离区。
    本函数**不写任何状态** —— 导出是读腿。
    """
    period = str(period or "").strip()
    if not period:
        raise ReconqError(
            "EXPORT_PERIOD_REQUIRED",
            "导出结论单必须给账期：reconq export --period 2026-W36 --fmt md",
        )

    aggregation: AggregationResult = aggregate_discrepancies(
        load_group_entries(conn, period), amount_bucket_width=amount_bucket
    )
    groups = tuple(_to_group_view(conn, g, period=period) for g in aggregation.groups)
    status, status_zh, gate, gate_reason = _load_period_state(conn, period)
    chain_root, chain_length = _load_chain(conn, period)
    contract_digests, needs_reconfirm = _load_contracts(conn, period)
    models, prompt_versions, calls, degraded, errors = _load_model_usage(conn, period)
    dirty_rows, dirty_total, quarantine_file = _load_dirty(conn, period)

    doc = ConclusionDoc(
        period=period,
        channels=_load_channels(conn, period),
        groups=groups,
        period_status=status,
        period_status_zh=status_zh,
        gate=gate,
        gate_reason=gate_reason,
        chain_root=chain_root,
        chain_length=chain_length,
        chain_anchor=_load_chain_anchor(audit_dir, period),
        contract_digests=contract_digests,
        contract_needs_reconfirm=needs_reconfirm,
        model_names=models,
        prompt_versions=prompt_versions,
        model_calls=calls,
        model_degraded=degraded,
        model_errors=errors,
        rule_baseline_ratio=_rule_baseline_ratio(conn, period),
        trace_chains=_load_trace_chains(conn, period, window=trace_window),
        dirty_by_channel=dirty_rows,
        dirty_total=dirty_total,
        quarantine_file=quarantine_file,
        cause_dict_stamp=dictionary_stamp(),
        exported_at=now or _now(),
        exported_by=exported_by or "（未署名）",
        note=note,
    )
    return _with_fingerprint(doc)


def _with_fingerprint(doc: ConclusionDoc) -> ConclusionDoc:
    """补上指纹（``dataclasses.replace`` 语义，手写以免整体 import）。"""
    from dataclasses import replace

    return replace(doc, fingerprint=conclusion_fingerprint(doc))


# ─────────────────────────── 渲染 ───────────────────────────


def _md_escape(text: str) -> str:
    """把会破坏表格的竖线与换行转义掉。

    经办人写「A|B」当分隔符时，不转义会把结论单的这张表**多切一列**，
    而列数对不上在纸面上看不出来 —— 读的人只会以为这一行的金额多了个竖线。
    """
    return str(text or "").replace("|", "／").replace("\r", " ").replace("\n", " ")


def _fingerprint_header_zh(doc: ConclusionDoc) -> list[str]:
    rows = sum(c.row_count for c in doc.channels)
    total = sum((_safe_decimal(c.total_amount) or Decimal("0")) for c in doc.channels)
    contracts = "、".join(f"{ch}:{dg[:12]}" for ch, dg in doc.contract_digests if dg) or "（未登记）"
    models = "、".join(doc.model_names) or "（本期未调模型）"
    prompts = "、".join(doc.prompt_versions) or "（无）"
    return [
        f"- 账期：{doc.period}",
        f"- 账期状态：{doc.period_status_zh or '未扫描'}"
        + (f" · 门禁 {doc.gate}" if doc.gate else "")
        + (f"（{doc.gate_reason}）" if doc.gate_reason else ""),
        f"- 六路取数：{doc.channels_ok}/{len(doc.channels)} 路已取到，"
        f"合计 {rows} 笔、{total:.2f} 元",
        f"- 本期事件链根指纹：`{doc.chain_root or '（无事件）'}`"
        + (f"（{doc.chain_length} 环）" if doc.chain_length else ""),
        f"- 审计链头锚点：`{doc.chain_anchor or '（未建审计链）'}`",
        f"- 本期口径指纹：{contracts}",
        f"- 模型 / 提示词：{models} / {prompts}",
        f"- 版本：结论单 {doc.sheet_version} · 产品 {doc.product_version} · "
        f"会签 {doc.ledger_version} · 聚合 {doc.aggregate_version} · "
        f"成因字典 {doc.cause_dict_stamp}",
        f"- 结论集指纹：`{doc.fingerprint}`",
        f"- 导出人：{doc.exported_by} · 导出时刻：{doc.exported_at}"
        + (f" · 备注：{_md_escape(doc.note)}" if doc.note else ""),
    ]


def render_conclusion_markdown(doc: ConclusionDoc) -> str:
    """渲染 Markdown 结论单（打印签字版）。"""
    lines: list[str] = [
        f"# {doc.period} 周对账结论单",
        "",
        "本单由支付对账数据质检系统自动生成，**每一条差异都可回到源文件的行号**；"
        "签字前请核对「结论集指纹」与重导一次是否一致。",
        "",
        "## 指纹头",
        "",
    ]
    lines += _fingerprint_header_zh(doc)

    lines += [
        "",
        "## 一、六路取数主态",
        "",
        "| 渠道 | 主态 | 笔数 | 合计金额(元) | 口径数 | 未过 | 脏行 | 说明 |",
        "|------|------|------|--------------|--------|------|------|------|",
    ]
    if doc.channels:
        for c in doc.channels:
            lines.append(
                f"| {c.channel} | {c.status_zh} | {c.row_count} | {c.total_amount} | "
                f"{c.checks_total} | {c.checks_failed} | {c.dirty_rows} | "
                f"{_md_escape(c.detail or ('—' if not c.error_code else c.error_code))} |"
            )
    else:
        lines.append("| （本期尚无渠道结果，先跑 reconq scan） | — | 0 | 0.00 | 0 | 0 | 0 | — |")

    lines += [
        "",
        "## 二、处置单分组结论",
        "",
        f"本期共 {doc.groups_count} 张处置单，覆盖差异 {doc.lines_count} 条，"
        f"涉及金额 {doc.total_amount:.2f} 元。",
        "",
    ]
    if doc.groups:
        for index, group in enumerate(doc.groups, start=1):
            handler, handler_at = group.signoff("handler")
            reviewer, reviewer_at = group.signoff("reviewer")
            supervisor, supervisor_at = group.signoff("supervisor")
            lines += [
                f"### {index}. {group.cause_zh} · {group.channel} · "
                f"{group.bucket_text} × {group.size} 条",
                "",
                f"- 处置单键：`{group.group_key}`",
                f"- 责任方：{group.owner}",
                f"- 建议动作：{group.suggested_action or '（成因未登记，无建议动作）'}",
                f"- 本单合计：{group.total_amount_text} 元（{group.size} 条）",
                f"- 组级结论：{_md_escape(group.group_conclusion) or '（经办尚未提交结论）'}",
                f"- 当前处置：{group.disposition}",
                f"- 经办：{handler or '—'}" + (f"（{handler_at}）" if handler_at else ""),
                f"- 复核：{reviewer or '—'}" + (f"（{reviewer_at}）" if reviewer_at else ""),
            ]
            if supervisor:
                lines.append(f"- 主管复核：{supervisor}（{supervisor_at}）")
            if group.split_reason:
                lines.append(f"- ⚠ 本单因「{_md_escape(group.split_reason)}」单独拆出")
            lines += [
                "",
                "| # | 渠道单号 | 差额(元) | 处置 | 会签状态 | 结论键 | 证据行 | 快照 |",
                "|---|----------|----------|------|----------|--------|--------|------|",
            ]
            for row_index, line in enumerate(group.lines, start=1):
                signoffs = "、".join(
                    x for x in (line.handler, line.reviewer, line.supervisor) if x
                ) or "—"
                refs = "、".join(line.evidence) or "—"
                lines.append(
                    f"| {row_index} | {line.order_no or '（无单号）'} | "
                    f"{line.amount_text or '—'} | {line.disposition or '—'} | {signoffs} | "
                    f"`{line.key}` | {refs} | {line.snapshot_ref or '—'} |"
                )
            lines.append("")
    else:
        lines += ["本期无待处置的单边账，无处置单。", ""]

    lines += ["## 三、附录", ""]

    # 附录一：跨账期连坐链
    lines += [
        "### 附录一 · 跨账期连坐链",
        "",
    ]
    if doc.trace_chains:
        for kind, detail in doc.trace_chains:
            lines.append(f"- **{kind}**：{_md_escape(detail)}")
    else:
        lines.append("本期无跨账期 / 跨渠道连坐结论。")

    # 附录二：未取到渠道及错误码
    lines += ["", "### 附录二 · 未取到渠道及错误码", ""]
    missing = doc.missing_channels
    if missing:
        # 渠道给**中文名 + 渠道码**两列：这张纸是要打印出来给财务科与
        # 主管签字的，而「alipay」不是一个财务科会念出来的词 —— 拿不准
        # 哪一路没取到的人会默认「没列出来的就是取到了」。渠道码保留在
        # 括号里，是为了让人能把这一行与 inbox/ 下的目录对上。
        from ..align.bundle import CHANNEL_ZH

        lines += [
            "| 渠道 | 渠道码 | 主态 | 错误码 | 说明 | 建议 |",
            "|------|--------|------|--------|------|------|",
        ]
        for c in missing:
            lines.append(
                f"| {CHANNEL_ZH.get(c.channel, c.channel)} | {c.channel} | "
                f"{c.status_zh} | {c.error_code or '—'} | "
                f"{_md_escape(c.detail or '—')} | 见 README「错误与降级」 |"
            )
    else:
        lines.append("六路全部取到并完成口径。")

    # 附录三：脏行统计与隔离区文件指针
    lines += ["", "### 附录三 · 脏行统计与隔离区", ""]
    if doc.dirty_total:
        detail = "、".join(f"{ch} {n} 条" for ch, n in doc.dirty_by_channel)
        lines += [
            f"- 本期隔离脏行合计 {doc.dirty_total} 条（{detail}）。",
            f"- 隔离区台账：`{doc.quarantine_file}`"
            "（用 `reconq quarantine export --period "
            f"{doc.period}` 生成，utf-8-sig，Excel 可直接打开）",
            "- 脏行**不计入**上面的合计金额：它们从没进账单表，"
            "把脏行算进合计会让「这期收了多少」看着比实际齐整。",
        ]
    else:
        lines.append("本期无脏行，账单表读到的行数与源文件行数一致。")

    # 附录四：模型降级说明与规则基线占比
    lines += ["", "### 附录四 · 模型降级与规则基线", ""]
    lines.append(f"- 模型调用 {doc.model_calls} 次，其中降级 {doc.model_degraded} 次。")
    if doc.model_errors:
        detail = "、".join(f"{code} × {n}" for code, n in doc.model_errors)
        lines.append(f"- 降级原因分布：{detail}")
    lines.append(f"- 规则基线占比：{doc.rule_baseline_ratio}")
    lines.append(
        "- 成因只是**草稿**：`已确认` / `已驳回` 只能由人工在处置单上点出，"
        "模型与规则都推不动状态。"
    )

    lines += [
        "",
        "## 四、签字",
        "",
        "| 岗位 | 姓名 / 工号 | 日期 |",
        "|------|------------|------|",
        "| 经办（财务科） |  |  |",
        "| 复核（信息科） |  |  |",
        "| 主管（财务科长） |  |  |",
        "",
    ]
    return "\n".join(lines) + "\n"


#: CSV 列定义。**一张表装下全部内容**：指纹头、六路主态、处置单、逐条
#: 明细与四类附录都作为行落进同一张表，用「区块」列区分 —— 财务科回填
#: 既有电子台账时是整表导入的，多张表反而要手工拼。
#:
#: 列名带括号（``差额(元)``）**不是合法 Python 标识符**，所以渲染函数
#: 收 dict 而不是 ``**kwargs``：写成 kwargs 的话中文列名根本没法当参数名。
CSV_COLUMNS: tuple[str, ...] = (
    "区块",
    "处置单键",
    "成因",
    "责任方",
    "渠道",
    "笔数",
    "单号",
    "差额(元)",
    "状态",
    "经办",
    "经办时间",
    "复核",
    "复核时间",
    "主管",
    "主管时间",
    "经办结论",
    "证据行",
    "快照",
    "结论键",
    "字段",
    "值",
    "说明",
)


def render_conclusion_csv(doc: ConclusionDoc) -> str:
    """渲染 CSV 结论单（回填既有电子台账用）。

    落盘用 ``utf-8-sig``（见 :func:`export_conclusion`）：财务科双击就是
    Excel，直接开 UTF-8 会在 Windows Excel 里把中文显示成乱码，而
    「乱码的结论单」等于没有结论单。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)

    def row(cells: dict[str, Any]) -> None:
        writer.writerow([cells.get(name, "") for name in CSV_COLUMNS])

    for field_name, value in (
        ("账期", doc.period),
        ("结论单版式", doc.sheet_version),
        ("产品版本", doc.product_version),
        ("会签规则版本", doc.ledger_version),
        ("聚合规则版本", doc.aggregate_version),
        ("成因字典", doc.cause_dict_stamp),
        ("账期状态", doc.period_status_zh or "未扫描"),
        ("门禁", doc.gate or "—"),
        ("六路取数", f"{doc.channels_ok}/{len(doc.channels)}"),
        ("事件链根指纹", doc.chain_root or "（无事件）"),
        ("事件链环数", doc.chain_length),
        ("审计链头锚点", doc.chain_anchor or "（未建审计链）"),
        ("模型", "、".join(doc.model_names) or "（本期未调模型）"),
        ("提示词版本", "、".join(doc.prompt_versions) or "（无）"),
        ("结论集指纹", doc.fingerprint),
        ("导出人", doc.exported_by),
        ("导出时刻", doc.exported_at),
        ("备注", doc.note),
        ("处置单张数", doc.groups_count),
        ("差异条数", doc.lines_count),
        ("涉及金额", f"{doc.total_amount:.2f}"),
    ):
        row({"区块": "指纹头", "字段": field_name, "值": value})

    for c in doc.channels:
        row({
            "区块": "六路主态",
            "渠道": c.channel,
            "笔数": c.row_count,
            "差额(元)": c.total_amount,
            "状态": c.status_zh,
            "字段": "口径数/未过/脏行",
            "值": f"{c.checks_total}/{c.checks_failed}/{c.dirty_rows}",
            "说明": c.detail or c.error_code,
        })

    for group in doc.groups:
        handler, handler_at = group.signoff("handler")
        reviewer, reviewer_at = group.signoff("reviewer")
        supervisor, supervisor_at = group.signoff("supervisor")
        row({
            "区块": "处置单",
            "处置单键": group.group_key,
            "成因": group.cause_zh,
            "责任方": group.owner,
            "渠道": group.channel,
            "笔数": group.size,
            "差额(元)": group.total_amount_text,
            "状态": group.disposition,
            "经办": handler,
            "经办时间": handler_at,
            "复核": reviewer,
            "复核时间": reviewer_at,
            "主管": supervisor,
            "主管时间": supervisor_at,
            "经办结论": group.group_conclusion,
            "字段": "金额区间/建议动作",
            "值": f"{group.bucket_text}/{group.suggested_action or '—'}",
            "说明": group.split_reason,
        })
        for line in group.lines:
            row({
                "区块": "差异明细",
                "处置单键": group.group_key,
                "成因": group.cause_zh,
                "责任方": group.owner,
                "渠道": line.channel,
                "单号": line.order_no,
                "差额(元)": line.amount_text,
                "状态": line.disposition or line.ledger_status,
                "经办": line.handler,
                "经办时间": line.handler_at,
                "复核": line.reviewer,
                "复核时间": line.reviewer_at,
                "主管": line.supervisor,
                "主管时间": line.supervisor_at,
                "经办结论": line.conclusion,
                "证据行": "、".join(line.evidence),
                "快照": line.snapshot_ref,
                "结论键": line.key,
                "字段": "会签状态/形态",
                "值": f"{line.ledger_status or '—'}/{line.ambiguity_kind or '—'}",
                "说明": line.cause_draft,
            })

    for kind, detail in doc.trace_chains:
        row({"区块": "附录-跨账期连坐链", "字段": kind, "说明": detail})
    for c in doc.missing_channels:
        row({
            "区块": "附录-未取到渠道",
            "渠道": c.channel,
            "状态": c.status_zh,
            "字段": c.error_code or "—",
            "说明": c.detail,
        })
    row({
        "区块": "附录-脏行统计",
        "值": doc.dirty_total,
        "字段": doc.quarantine_file,
        "说明": "、".join(f"{ch} {n} 条" for ch, n in doc.dirty_by_channel),
    })
    row({
        "区块": "附录-模型降级",
        "值": f"{doc.model_degraded}/{doc.model_calls}",
        "字段": "降级/总调用",
        "说明": "、".join(f"{code} × {n}" for code, n in doc.model_errors) or "无降级",
    })
    row({
        "区块": "附录-规则基线",
        "字段": "规则基线占比",
        "值": doc.rule_baseline_ratio,
    })

    return buffer.getvalue()


# ─────────────────────────── 导出 ───────────────────────────


def render_sheet(
    conn: sqlite3.Connection,
    period: str,
    *,
    fmt: str = "md",
    exported_by: str = "",
    note: str = "",
    now: Optional[str] = None,
    amount_bucket: str = "0.01",
    trace_window: int = DEFAULT_TRACE_WINDOW,
    audit_dir: str = "",
) -> tuple[str, ConclusionDoc, str]:
    """渲染结论单正文，**不落盘**。返回 ``(正文, 文档, 落盘编码)``。

    与 :func:`export_conclusion` 共用同一段装配与脱敏判据 —— 面板那条
    下载腿（``GET /api/period/<账期>/export``）要的就是这份正文，只是它
    直接回给浏览器而不写 ``out/``。两条腿各自渲染一次会让「打印出来的那
    一份」与「面板上下载的那一份」在版式上慢慢分叉，而这两种恰恰要一起
    交给同一个财务科签字。
    """
    fmt = str(fmt or "md").strip().lower()
    if fmt not in CONCLUSION_FORMATS:
        raise ReconqError(
            "EXPORT_FORMAT_UNKNOWN",
            f"结论单格式 {fmt!r} 不支持；可选：{'、'.join(CONCLUSION_FORMATS)}。"
            "md 供打印签字，csv 供回填既有电子台账",
        )

    doc = build_conclusion(
        conn,
        period,
        exported_by=exported_by,
        note=note,
        now=now,
        amount_bucket=amount_bucket,
        trace_window=trace_window,
        audit_dir=audit_dir,
    )
    # 脱敏在**渲染前**核 payload：Markdown 表格与 CSV 单元格都能把
    # 一段文本拆到多处，只查渲染结果会漏掉被转义/合并掉的那一份。
    assert_sheet_deidentified(doc.to_dict(), where=f"{period} 结论单")

    text = render_conclusion_markdown(doc) if fmt == "md" else render_conclusion_csv(doc)
    return text, doc, ("utf-8" if fmt == "md" else "utf-8-sig")


def export_conclusion(
    conn: sqlite3.Connection,
    period: str,
    *,
    fmt: str = "md",
    out_path: str = "",
    exported_by: str = "",
    note: str = "",
    now: Optional[str] = None,
    amount_bucket: str = "0.01",
    trace_window: int = DEFAULT_TRACE_WINDOW,
    audit_dir: str = "",
) -> ExportOutcome:
    """导出结论单，返回 :class:`ExportOutcome`。

    ``out_path`` 不给时按 ``out/<账期>-conclusion.<fmt>`` 落盘。
    **落盘前**先过一遍脱敏核对：结论单要打印、签字、可能再交给渠道方，
    患者可识别信息混进去的代价远高于当场拒导。
    """
    text, doc, encoding = render_sheet(
        conn,
        period,
        fmt=fmt,
        exported_by=exported_by,
        note=note,
        now=now,
        amount_bucket=amount_bucket,
        trace_window=trace_window,
        audit_dir=audit_dir,
    )
    fmt = str(fmt or "md").strip().lower()
    target = out_path or os.path.join(DEFAULT_OUT_DIR, f"{period}-conclusion.{fmt}")
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    encoding = "utf-8" if fmt == "md" else "utf-8-sig"
    with open(target, "w", encoding=encoding, newline="") as handle:
        handle.write(text)
    return ExportOutcome(path=target, fmt=fmt, doc=doc, size=len(text.encode(encoding)))
