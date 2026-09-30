"""审计：模型调用成本（Task 31）与链式事件检索（Task 45）。

回答财务科的三个日常问题：**这一期调了几次、命中了多少缓存、花了多少钱**。

分两组对象：

* **成本**（Task 31）—— :class:`~reconq.audit.model_calls.ModelCallLedger`
  逐次留痕（append-only + 按账期链式指纹，篡改可检出也可挡住），
  :class:`~reconq.audit.model_calls.PeriodUsage` 按账期聚合，
  面板底部与 ``reconq model-usage`` 共用同一份口径；
* **事件链**（Task 45）—— :func:`~reconq.audit.query.append_event` 落一条
  链式事件，:func:`~reconq.audit.query.query_events` 按账期切片读回，
  :func:`~reconq.audit.query.discrepancy_timeline` 给出一条差异的完整时序，
  :func:`~reconq.audit.query.verify_chain` 重算校验。

另有两个支撑件：:func:`~reconq.audit.model_calls.request_fingerprint`
（证据包字节的 sha256，去重与复现的锚点）与
:class:`~reconq.audit.model_calls.DraftCache`（同一证据 + 同一提示词版本
不重复调模型）。

**本包不 import 网关包**：审计回答的是「花了多少钱」，它不该把「读一次
成本」绑到「是否装了模型端点」上 —— 与 :mod:`reconq.run_summary` 不
import 归因包同一条纪律。
"""

from __future__ import annotations

from .model_calls import (
    AUDIT_ALLOWED_FIELDS,
    DEFAULT_PROMPT_VERSION,
    GENESIS_HASH,
    MODEL_CALL_CACHE_TABLE,
    MODEL_CALL_CORRECTION_TABLE,
    MODEL_CALL_TABLE,
    ChainVerdict,
    DraftCache,
    ModelCall,
    ModelCallLedger,
    ModelUsage,
    PeriodUsage,
    PRICE_ENV_VARS,
    append_only_guard,
    cache_key,
    init_audit_schema,
    make_call_id,
    request_fingerprint,
    usage_of_period,
)
from .query import (
    EVENT_INDEX_NAME,
    EVENT_LOG_NAME,
    EVENT_TYPES,
    INDEX_VERSION,
    TIMELINE_STAGE_OF,
    AuditEvent,
    EventChainVerdict,
    EventSlice,
    append_event,
    assert_event_deidentified,
    chain_anchor,
    discrepancy_timeline,
    event_type_zh,
    load_index,
    open_chain,
    query_events,
    rebuild_index,
    verify_chain,
)
from .wecom import (
    AMOUNT_THRESHOLD_ENV,
    COUNT_THRESHOLD_ENV,
    DEEP_LINK_SCHEME,
    DEFAULT_PANEL_BASE,
    PANEL_BASE_ENV,
    PUSH_STATE_NAME,
    PUSH_STATES,
    PUSH_STATE_ZH,
    THRESHOLD_KINDS,
    THRESHOLD_KIND_ZH,
    WEBHOOK_KEY_ENV,
    WEBHOOK_URL,
    NotifState,
    PendingItem,
    ThresholdVerdict,
    WeComConfig,
    WeComResult,
    assert_payload_clean,
    build_deep_link,
    build_payload,
    evaluate_threshold,
    flush_pending,
    notifier_state,
    notifier_status_of,
    parse_deep_link,
    render_markdown,
    send_summary,
)

__all__ = [
    "AMOUNT_THRESHOLD_ENV",
    "AUDIT_ALLOWED_FIELDS",
    "COUNT_THRESHOLD_ENV",
    "DEFAULT_PROMPT_VERSION",
    "DEEP_LINK_SCHEME",
    "DEFAULT_PANEL_BASE",
    "EVENT_INDEX_NAME",
    "EVENT_LOG_NAME",
    "EVENT_TYPES",
    "GENESIS_HASH",
    "INDEX_VERSION",
    "MODEL_CALL_CACHE_TABLE",
    "MODEL_CALL_CORRECTION_TABLE",
    "MODEL_CALL_TABLE",
    "PANEL_BASE_ENV",
    "PUSH_STATES",
    "PUSH_STATE_NAME",
    "PUSH_STATE_ZH",
    "THRESHOLD_KINDS",
    "THRESHOLD_KIND_ZH",
    "TIMELINE_STAGE_OF",
    "WEBHOOK_KEY_ENV",
    "WEBHOOK_URL",
    "AuditEvent",
    "ChainVerdict",
    "DraftCache",
    "EventChainVerdict",
    "EventSlice",
    "ModelCall",
    "ModelCallLedger",
    "ModelUsage",
    "NotifState",
    "PendingItem",
    "PeriodUsage",
    "PRICE_ENV_VARS",
    "ThresholdVerdict",
    "WeComConfig",
    "WeComResult",
    "append_event",
    "append_only_guard",
    "assert_event_deidentified",
    "assert_payload_clean",
    "build_deep_link",
    "build_payload",
    "cache_key",
    "chain_anchor",
    "discrepancy_timeline",
    "evaluate_threshold",
    "event_type_zh",
    "flush_pending",
    "init_audit_schema",
    "load_index",
    "make_call_id",
    "notifier_state",
    "notifier_status_of",
    "open_chain",
    "parse_deep_link",
    "query_events",
    "rebuild_index",
    "render_markdown",
    "request_fingerprint",
    "send_summary",
    "usage_of_period",
    "verify_chain",
]
