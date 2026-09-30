"""模型网关与失败处理链（Task 28 / Task 30）。

**只选一种协议**：OpenAI-compatible ``POST {base_url}/chat/completions``。
院内自建端点、本地模型与云服务都走这一份配置面（``RECONQ_MODEL_*``），
本包不 import 任何厂商 SDK —— 换厂商时改的是环境变量，不是业务代码。

分两层：

* :mod:`~reconq.gateway.model_gateway` —— **一次**调用怎么发、回来怎么验；
* :mod:`~reconq.gateway.degradation` —— **一整次扫描里几百条差异**连着调
  同一个端点时，它挂了怎么办（重试矩阵 / 熔断 / 降级台账）。

两层的边界是「一次调用」与「一批调用」：前者决定这一次算不算成功，
后者决定这一期还值不值得继续打。后者不做，院内模型服务的挂法会把一次
40 秒的扫描拖成 40 分钟，且最终拿不到任何成因。
"""

from __future__ import annotations

from .degradation import (
    RETRY_MATRIX,
    STATUS_DEGRADED,
    STATUS_OK,
    BreakerState,
    CircuitBreaker,
    DegradationLedger,
    DegradationRecord,
    FailureKind,
    FailurePhase,
    RetryRule,
    classify_failure,
    delay_for,
    mark_pending_manual,
    rule_for,
)
from .model_gateway import (
    DEFAULT_MAX_ATTEMPTS,
    GatewayCall,
    GatewayConfig,
    GatewayResult,
    ModelGateway,
    ResponseValidator,
)

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "RETRY_MATRIX",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "BreakerState",
    "CircuitBreaker",
    "DegradationLedger",
    "DegradationRecord",
    "FailureKind",
    "FailurePhase",
    "GatewayCall",
    "GatewayConfig",
    "GatewayResult",
    "ModelGateway",
    "ResponseValidator",
    "RetryRule",
    "classify_failure",
    "delay_for",
    "mark_pending_manual",
    "rule_for",
]
