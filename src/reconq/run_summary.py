"""运行摘要对象（Task 20）。

面板、CLI、结论单三处要看的是**同一份**「这次扫描到底发生了什么」。如果
各写各的，最先出现的一定是这种局面：面板显示微信 20 行，结论单写 19 行，
而库里只有 19 行 —— 没人能判断哪个是真的，财务科只能重跑一次。

本模块给这件事一个落点：:class:`RunSummary` 是唯一摘要真源，
``reconq scan/verify --json``、面板接口、结论单导出都从它取数。

每路渠道固定八个字段（题目要求的口径，缺一不可）：

============ ==========================================================
字段          含义
============ ==========================================================
status        checked / not_fetched / failed
row_count     本期该路读到的账单行数
total_amount  本期该路金额合计（Decimal 定点字符串，不是 float）
batches       取数分批次数（流式取数的批数；文件通道按文件数记）
elapsed_ms    本路耗时（毫秒）
peak_rss_kb   本路处理期间的进程常驻内存峰值（KB）
dirty_rows    进隔离区的行数
error_code    失败时的错误码（``""`` 表示无失败）
============ ==========================================================

``batches`` 与 ``elapsed_ms`` 是**排障字段**：HIS 库那两三千行要分几十次
FETCH 往返，一路上 400ms 与一路 40s 都在「成功」里，光看行数看不出来。
``peak_rss_kb`` 同理 —— 流式取数本该把峰值压在一批之内，但某天有人给批预算
改了个 0，这个数会先于报错冒出来。

**金额与行数口径以账单库为准，不是以「本次跑了几路」为准**（与 Task 19 的
合计口径同源）：子集重跑时若按本次累加，本期合计会凭空少一大截，而库里
没有一行数据真的变过。
"""

from __future__ import annotations

import os
import resource
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Sequence

from .errors import ReconqError

__all__ = [
    "ChannelRun",
    "RunSummary",
    "build_run_summary",
    "current_peak_rss_kb",
    "STATUS_ZH",
    "STATUS_OK",
]

#: 渠道主态的中文标签。**唯一真源**在 ``engine/period.py`` 的
#: ``ChannelScan.status_zh``；这里只做「非扫描腿（verify / 面板）」的补充，
#: 两处标签用同一批字符串常量拼，避免面板和终端各说各话。
STATUS_ZH = {
    "checked": "已取到并完成口径",
    "not_fetched": "本期未取到",
    "failed": "取数或口径执行失败",
}

#: 「这路能进对账」的状态。面板据此决定是否给绿色。
STATUS_OK = frozenset({"checked"})


def current_peak_rss_kb() -> int:
    """进程常驻内存峰值（**KB**）。

    用 :mod:`resource`（POSIX 标准库）而不是 ``psutil``：峰值内存是排障
    辅助字段，为它引入一个平台相关的重依赖不值得。

    单位在不同平台不一样，**必须**折算，否则同一个字段在 Linux 上是 35 MB、
    在 macOS 上是 35000 MB，看板上两台机器的数字差一千倍：

    ============  ====================================================
    平台           ``ru_maxrss`` 单位
    ============  ====================================================
    Linux          KB
    macOS / BSD    字节
    Windows        字节（且无 :mod:`resource`，本产品不支持）
    ============  ====================================================
    """
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(usage // 1024)
    return int(usage)


@dataclass
class ChannelRun:
    """一路渠道在本次运行里的摘要。字段与本文档表格一一对应。"""

    channel: str
    status: str = "not_fetched"
    row_count: int = 0
    total_amount: str = "0.00"
    batches: int = 0
    elapsed_ms: float = 0.0
    peak_rss_kb: int = 0
    dirty_rows: int = 0
    error_code: str = ""
    cause_code: str = ""
    detail: str = ""
    checks_total: int = 0
    checks_failed: int = 0
    #: 本次重跑未覆盖、结果沿用上次的渠道。面板必须标出来，不能混进
    #: 「本次落库 N 行」——否则入库量随重跑范围忽大忽小，没人解释得清。
    carried: bool = False

    @property
    def status_zh(self) -> str:
        return STATUS_ZH.get(self.status, self.status)

    @property
    def ok(self) -> bool:
        return self.status in STATUS_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "status": self.status,
            "status_zh": self.status_zh,
            "row_count": self.row_count,
            "total_amount": self.total_amount,
            "batches": self.batches,
            "elapsed_ms": self.elapsed_ms,
            "peak_rss_kb": self.peak_rss_kb,
            "dirty_rows": self.dirty_rows,
            "error_code": self.error_code or None,
            "cause_code": self.cause_code or None,
            "detail": self.detail,
            "checks_total": self.checks_total,
            "checks_failed": self.checks_failed,
            "carried": self.carried,
        }

    @classmethod
    def from_channel_scan(cls, item: Any, *, carried: bool = False) -> "ChannelRun":
        """从 ``engine.period.ChannelScan`` 构造。

        ``ChannelScan`` 里没有 ``batches`` / ``elapsed_ms`` / ``peak_rss_kb`` /
        ``dirty_rows`` —— 那四个是**运行期**观测值，扫描过程中由本轮填充，
        不落库（重启后读到的历史渠道结果不该带着上次的耗时冒充本次）。
        """
        return cls(
            channel=item.channel,
            status=item.status,
            row_count=item.row_count,
            total_amount=item.total_amount,
            batches=getattr(item, "batches", 0) or 0,
            elapsed_ms=getattr(item, "elapsed_ms", 0.0) or 0.0,
            peak_rss_kb=getattr(item, "peak_rss_kb", 0) or 0,
            dirty_rows=getattr(item, "dirty_rows", 0) or 0,
            error_code=item.error_code,
            cause_code=item.cause_code,
            detail=item.detail,
            checks_total=item.checks_total,
            checks_failed=item.checks_failed,
            carried=carried,
        )


@dataclass
class RunSummary:
    """一次运行的完整摘要。面板 / CLI / 结论单共用这一份。"""

    period: str
    run_id: str = ""
    command: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed_ms: float = 0.0
    peak_rss_kb: int = 0
    gate: str = ""
    gate_reason: str = ""
    status: str = ""
    status_zh: str = ""
    total_amount: str = "0.00"
    rows_total: int = 0
    checks_total: int = 0
    checks_failed: int = 0
    dirty_rows: int = 0
    diffs_written: int = 0
    unconfirmed: int = 0
    pending_attribution: int = 0
    channels: list[ChannelRun] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 本次落库的批级幂等键。运营侧回答「这一期这一路到底导进去没有」。
    idempotency_keys: list[str] = field(default_factory=list)
    #: 事件链指纹（谁、何时、什么范围、结果差几条）。
    event_seq: int = 0
    event_hash: str = ""
    # ── 归因降级观测（Task 30）──────────────────────────────
    #: 本期降级次数。「归因能力按待人工判定处理」这句话要有个数撑着，
    #: 否则面板上只写一句「模型不可用」，财务科不知道该先救哪几条。
    degradation_count: int = 0
    #: 降级原因分布（错误码 → 条数），面板按它把降级归组显示。
    degradation_by_reason: dict[str, int] = field(default_factory=dict)
    #: 熔断器三态快照。开着时 :attr:`can_close` 不因此改变 —— 归因不可用
    #: 不等于对账没做完，**签字与否取决于差异本身是否被核对过**。
    breaker_state: str = ""
    breaker_state_zh: str = ""
    breaker_opened_at: str = ""
    breaker_recovered_at: str = ""
    breaker_opened_count: int = 0
    # ── 模型成本计量（Task 31）──────────────────────────────
    #: 本期模型调用次数（含缓存命中）。「这一期调了几次」有两个答案，
    #: 差着好几倍 —— 真正打到端点上的是 :attr:`model_actual_calls`。
    model_calls: int = 0
    model_actual_calls: int = 0
    model_cache_hits: int = 0
    #: 命中率文本（定点两位带 % 号）。**未调用时是 0.00% 而不是空串** ——
    #: 面板上「0.00%」与「没数据」要能分开，故另有 :attr:`model_calls` 兜底。
    model_cache_hit_rate: str = ""
    model_total_tokens: int = 0
    model_avg_elapsed_ms: float = 0.0
    #: 成本文本。**未配单价时是「未配置单价」而不是 0.00 元** ——
    #: 显示 0 元会被读成「这一期真的一次钱没花」。
    model_cost_text: str = ""
    # ── 反幻觉核对统计（Task 32）────────────────────────────
    #: 本期核对了多少条成因草稿。这三个数回答的是「模型说的话有多少能
    #: 直接给人看」—— 与 :attr:`model_calls` 回答的「调了几次」不是
    #: 一回事：调了 200 次、只有 40 条说得对，剩下 160 条都要人重判。
    verification_checked: int = 0
    verification_consistent: int = 0
    verification_rejected: int = 0
    #: 一致率文本。**未核对时是空串而不是 0.00%** —— 「一条都没核对」
    #: 与「核对了、全部不一致」在提示词迭代上是两回事。
    verification_consistency_rate: str = ""
    #: 核对项分布（核对项 → 次数）。这是**给提示词迭代用的**：哪一项最常
    #: 不过，直接指向该改证据包的组织方式还是该改提示词。
    verification_mismatch_by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def channels_ok(self) -> int:
        return sum(1 for c in self.channels if c.ok)

    @property
    def channels_failed(self) -> int:
        """未取到 + 取数失败。

        「未取到」也算失败：六路少一路的账期绝不能显绿 —— 那一路的钱既没被
        核对也没被排除，账上看着干净其实少查了一路。
        """
        return sum(1 for c in self.channels if not c.ok)

    @property
    def can_close(self) -> bool:
        return self.gate == "green" and self.channels_failed == 0

    def channel_of(self, channel: str) -> Optional[ChannelRun]:
        for item in self.channels:
            if item.channel == channel:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "run_id": self.run_id,
            "command": self.command,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "peak_rss_kb": self.peak_rss_kb,
            "gate": self.gate,
            "gate_reason": self.gate_reason,
            "status": self.status,
            "status_zh": self.status_zh,
            "can_close": self.can_close,
            "total_amount": self.total_amount,
            "rows_total": self.rows_total,
            "checks_total": self.checks_total,
            "checks_failed": self.checks_failed,
            "dirty_rows": self.dirty_rows,
            "diffs_written": self.diffs_written,
            "unconfirmed": self.unconfirmed,
            "pending_attribution": self.pending_attribution,
            "channels_ok": self.channels_ok,
            "channels_failed": self.channels_failed,
            "channels": [c.to_dict() for c in self.channels],
            "warnings": list(self.warnings),
            "idempotency_keys": list(self.idempotency_keys),
            "event_seq": self.event_seq,
            "event_hash": self.event_hash,
            "degradation_count": self.degradation_count,
            "degradation_by_reason": dict(self.degradation_by_reason),
            "breaker_state": self.breaker_state,
            "breaker_state_zh": self.breaker_state_zh,
            "breaker_opened_at": self.breaker_opened_at,
            "breaker_recovered_at": self.breaker_recovered_at,
            "breaker_opened_count": self.breaker_opened_count,
            "model_calls": self.model_calls,
            "model_actual_calls": self.model_actual_calls,
            "model_cache_hits": self.model_cache_hits,
            "model_cache_hit_rate": self.model_cache_hit_rate,
            "model_total_tokens": self.model_total_tokens,
            "model_avg_elapsed_ms": self.model_avg_elapsed_ms,
            "model_cost_text": self.model_cost_text,
            "verification_checked": self.verification_checked,
            "verification_consistent": self.verification_consistent,
            "verification_rejected": self.verification_rejected,
            "verification_consistency_rate": self.verification_consistency_rate,
            "verification_mismatch_by_kind": dict(self.verification_mismatch_by_kind),
        }

    def render_zh(self) -> str:
        """人读摘要（终端排障腿用；面板与结论单读 :meth:`to_dict`）。"""
        lines = [
            f"账期 {self.period}"
            + (f"（run={self.run_id}）" if self.run_id else "")
            + f" · 合计 {self.total_amount} 元 · "
            f"{self.channels_ok}/{len(self.channels)} 路可用 · "
            f"耗时 {self.elapsed_ms:.0f} ms · 峰值内存 {self.peak_rss_kb} KB"
        ]
        for ch in self.channels:
            flag = "沿用" if ch.carried else "本次"
            line = (
                f"  · {ch.channel:<14} {ch.status_zh:<18} {ch.row_count:>6} 行"
                f" {ch.total_amount:>12} 元  口径 {ch.checks_total} 条/"
                f"未通过 {ch.checks_failed} 条  {flag}"
                f"  {ch.elapsed_ms:.0f} ms/{ch.batches} 批"
            )
            if ch.dirty_rows:
                line += f"  脏行 {ch.dirty_rows}"
            if ch.error_code:
                line += f"  [{ch.error_code}]"
            lines.append(line)
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        if self.degradation_count:
            # 降级**不进 warnings**：它不是「这一期出了别的问题」，而是
            # 「成因没归出来、这些差异等人工判定」。混进 warnings 里会被
            # 当成可忽略的提示刷过去。单独一行、带着原因分布。
            top = "、".join(
                f"{code}×{count}" for code, count in list(self.degradation_by_reason.items())[:3]
            )
            lines.append(
                f"  ~ 归因降级 {self.degradation_count} 条（{top or '无原因码'}）"
                f" · 熔断 {self.breaker_state_zh or self.breaker_state or '未启用'}"
                + (f" 开于 {self.breaker_opened_at}" if self.breaker_opened_at else "")
                + " · 相关差异待人工判定，台账状态不推进"
            )
        if self.model_calls:
            # 成本行只在**有调用**时出现。没调过的账期写一行「模型调用 0 次」
            # 看起来像一条统计结论，而它其实什么都没说。
            lines.append(
                f"  ¥ 模型调用 {self.model_calls} 次"
                f"（真正发出 {self.model_actual_calls} 次，"
                f"缓存命中 {self.model_cache_hits} 次 / {self.model_cache_hit_rate}）"
                f" · token {self.model_total_tokens}"
                f" · 平均 {self.model_avg_elapsed_ms:.0f} ms"
                f" · 成本 {self.model_cost_text}"
            )
        if self.verification_checked:
            # 一致率行只在**核过**时出现。没核对过的账期写一行「一致率 0%」
            # 看着像「模型说的全不对」，而事实是一句话都没说。
            top = "、".join(
                f"{code}×{count}"
                for code, count in list(self.verification_mismatch_by_kind.items())[:3]
            )
            lines.append(
                f"  ~ 归因草稿反幻觉核对 {self.verification_checked} 条："
                f"一致 {self.verification_consistent} / 拒 {self.verification_rejected}"
                f"（一致率 {self.verification_consistency_rate}）"
                + (f" · 不一致项 {top}" if top else "")
                + " · 被拒草稿标待人工判定，不进入经办确认流"
            )
        return "\n".join(lines)


def build_run_summary(
    *,
    period: str,
    command: str,
    channels: Sequence[Any],
    started_at: str = "",
    elapsed_ms: float = 0.0,
    gate: str = "",
    gate_reason: str = "",
    status: str = "",
    status_zh: str = "",
    total_amount: Optional[str] = None,
    rows_total: Optional[int] = None,
    checks_total: Optional[int] = None,
    checks_failed: Optional[int] = None,
    diffs_written: int = 0,
    unconfirmed: int = 0,
    pending_attribution: int = 0,
    warnings: Optional[Sequence[str]] = None,
    idempotency_keys: Optional[Sequence[str]] = None,
    run_id: str = "",
    event_seq: int = 0,
    event_hash: str = "",
    degradation: Optional[Any] = None,
    model_usage: Optional[Any] = None,
    verification: Optional[Any] = None,
) -> RunSummary:
    """从各路结果装配一份 :class:`RunSummary`。

    ``total_amount`` / ``rows_total`` 等汇总项**不给时**才从渠道结果求和；
    调用方给了就以调用方为准。给的口径是「账单库里的权威合计」，不是
    「本次跑的那几路的合计」——后者在子集重跑时会凭空少一大截。
    """
    items: list[ChannelRun] = []
    for item in channels:
        if isinstance(item, ChannelRun):
            items.append(item)
        else:
            items.append(ChannelRun.from_channel_scan(item))

    if total_amount is None:
        total = Decimal("0")
        for item in items:
            try:
                total += Decimal(item.total_amount or "0")
            except Exception:
                # 金额不可解析时该路已由 AMOUNT_UNPARSABLE 报出行号，
                # 摘要层不重复报错也不能让整份摘要挂掉。
                continue
        total_amount = str(total.quantize(Decimal("0.01")))
    if rows_total is None:
        rows_total = sum(item.row_count for item in items)
    if checks_total is None:
        checks_total = sum(item.checks_total for item in items)
    if checks_failed is None:
        checks_failed = sum(item.checks_failed for item in items)

    summary = RunSummary(
        period=period,
        run_id=run_id,
        command=command,
        started_at=started_at,
        finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        elapsed_ms=elapsed_ms,
        peak_rss_kb=current_peak_rss_kb(),
        gate=gate,
        gate_reason=gate_reason,
        status=status,
        status_zh=status_zh,
        total_amount=total_amount,
        rows_total=rows_total,
        checks_total=checks_total,
        checks_failed=checks_failed,
        dirty_rows=sum(item.dirty_rows for item in items),
        diffs_written=diffs_written,
        unconfirmed=unconfirmed,
        pending_attribution=pending_attribution,
        channels=items,
        warnings=list(warnings or []),
        idempotency_keys=list(idempotency_keys or []),
        event_seq=event_seq,
        event_hash=event_hash,
        **_degradation_fields(degradation),
        **_model_usage_fields(model_usage),
        **_verification_fields(verification),
    )
    return summary


def _degradation_fields(degradation: Optional[Any]) -> dict[str, Any]:
    """把 :class:`reconq.gateway.degradation.DegradationLedger` 摊成摘要字段。

    **刻意不在此处 import 归因包**（:mod:`reconq.gateway.degradation`）：
    摘要模块是面板 / CLI / 结论单的公共底座，让它反过来依赖网关会把
    「读一份运行摘要」这件事绑到「是否装了模型端点」上。调用方传进来的
    对象只要有 ``to_run_summary_fields()`` 即可鸭子类型通过。
    """
    if degradation is None:
        return {}
    getter = getattr(degradation, "to_run_summary_fields", None)
    if getter is None:
        raise ReconqError(
            "RUN_SUMMARY_INVALID",
            f"degradation 参数没有 to_run_summary_fields()：{type(degradation).__name__}。"
            f"请传 DegradationLedger（reconq.gateway.degradation）",
        )
    return dict(getter())


def _model_usage_fields(model_usage: Optional[Any]) -> dict[str, Any]:
    """把 :class:`reconq.audit.model_calls.PeriodUsage` 摊成摘要字段。

    **刻意不在此处 import 审计包**（理由同 :func:`_degradation_fields`）：
    摘要模块是面板 / CLI / 结论单的公共底座，让它反过来依赖审计会把
    「读一份运行摘要」绑到「是否装了模型调用面」上。鸭子类型通过即可。
    """
    if model_usage is None:
        return {}
    getter = getattr(model_usage, "to_run_summary_fields", None)
    if getter is None:
        raise ReconqError(
            "RUN_SUMMARY_INVALID",
            f"model_usage 参数没有 to_run_summary_fields()：{type(model_usage).__name__}。"
            f"请传 PeriodUsage（reconq.audit.model_calls）",
        )
    return dict(getter())


def _verification_fields(verification: Optional[Any]) -> dict[str, Any]:
    """把 :class:`reconq.align.verify_draft.VerificationLedger` 摊成摘要字段。

    **刻意不在此处 import 归因核对包**（理由同 :func:`_degradation_fields`）：
    摘要模块是面板 / CLI / 结论单的公共底座，让它反过来依赖
    ``align.verify_draft`` 会把「读一份运行摘要」绑到「是否装了归因核对面」
    上。鸭子类型通过即可。
    """
    if verification is None:
        return {}
    getter = getattr(verification, "to_run_summary_fields", None)
    if getter is None:
        raise ReconqError(
            "RUN_SUMMARY_INVALID",
            f"verification 参数没有 to_run_summary_fields()：{type(verification).__name__}。"
            "请传 VerificationLedger（reconq.align.verify_draft）",
        )
    return dict(getter())
