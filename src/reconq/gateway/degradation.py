"""网关失败处理链：重试矩阵 → 超时分层 → 熔断 → 降级（Task 30）。

为什么这一层独立于 :mod:`reconq.gateway.model_gateway`
------------------------------------------------------
Task 28 的网关做的是「**一次**调用怎么发、回来怎么验」；这一层做的是
「**一整次扫描里几百条差异**连着调同一个端点时，它挂了怎么办」。

院内网里的模型服务有一个反复出现的形态：**不是拒绝，是不回应**。推理
服务被并发打满、GPU 队列排长、网关进程假死 —— 表现为每条差异都要等满
超时，而一台账有两三百条差异。财务科看到的不是「归因不可用」这四个字，
而是**扫描从 40 秒变成 40 分钟**，最后还因为拿不到成因而全部待人工判定。
纯重试解决不了这件事：重试只会把等待乘以三。

所以本模块给三样东西，各自有独立判据：

============================  =========================================
机制                          回答的问题
============================  =========================================
:data:`RETRY_MATRIX` 重试矩阵  这一类失败**值不值得**再来一次、等多久
:data:`CircuitBreaker` 熔断    端点已经**持续不回应**时还要不要继续打
:data:`DegradationLedger` 降级台账  这一期一共降级了几次、什么时候开的闸、
                              什么时候恢复的
============================  =========================================

三条不可违反的边界
--------------------

1. **4xx 业务错误一次都不重试。** 401/403/404 是配置与权限问题，重试
   只是把同一个错放大三次并白耗院内推理配额；重试矩阵把它们显式归到
   :data:`FailureKind.BUSINESS`，``retryable=False``。
2. **降级永远不推进业务状态。** 本模块不 import
   :mod:`reconq.engine.period`，也不持有任何 sqlite 连接 —— 它能做的
   全部动作只有「返回一个降级标记」和「记一条观测」。台账状态只在人工点
   上发生，这条由 :mod:`reconq.align.attribution` 与
   :func:`reconq.gateway.degradation.mark_pending_manual` 共同保证。
3. **降级要留痕，且留的是时刻。** 面板上「本期 3 条降级、熔断 10:12 开
   / 10:13 恢复」比「归因不可用」有用得多 —— 前者让人知道该找谁、
   找完再跑要不要等。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层**：``data-source test`` 是
一次性连通性探针，``contract verify`` 跑完就退。它假定「跑一次」，
而本产品是常驻服务 + 一次扫描几百次调用，**重试放大与熔断的必要性来自
调用频次而非协议本身**。可复用的只有「错误分类要先分清值不值得重试」
这条纪律 —— 源实现对 ``FREQUENCY_LIMITED`` 退避、对 ``BILL_NOT_EXIST``
不重试，本产品把同一判据搬到模型调用面上。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional

from ..errors import ReconqError
from ..runlog import log_event

__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "DegradationLedger",
    "DegradationRecord",
    "FailureKind",
    "FailurePhase",
    "RetryRule",
    "RETRY_MATRIX",
    "DEFAULT_BREAKER_COOLDOWN_SECONDS",
    "DEFAULT_BREAKER_THRESHOLD",
    "DEFAULT_JITTER_RATIO",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "admission_of",
    "classify_failure",
    "delay_for",
    "mark_pending_manual",
    "rule_for",
    "translation_of",
]


#: :class:`~reconq.gateway.model_gateway.GatewayResult.status` 的两个取值。
#: **刻意用大写**：这个字符串会进事件链与结论单，混在 ``pending_confirm``
#: 这类小写状态里容易让人以为是台账状态，而它描述的是**调用结果**。
STATUS_OK = "OK"
STATUS_DEGRADED = "DEGRADED"

#: 退避抖动比例上限（乘性）。0.2 = 实际延迟落在名义值的 80%~120%。
#:
#: **不做「全抖动」（0~base 随机）**：那会让第 1 次重试有可能几乎不等待，
#: 而首次失败往往正是端点刚被打满的时刻 —— 立刻再打一次大概率还是失败。
#: 乘性抖动只打散「几秒后一起重试」的共振（多个对账任务同时起跑时最常见）。
DEFAULT_JITTER_RATIO = 0.2

#: 连续失败多少次开闸。
#:
#: 取 5 而不是 3 或 10：一条差异最多重试 2 次共 3 个请求，开闸阈值必须
#: **大于**单条差异的尝试数，否则第一条差异失败就把闸开了，熔断退化成
#: 「一次失败即封禁」，而端点往往只是这一条证据太难。10 则太晚 ——
#: 按每次 20 秒超时算，第 10 条失败时已经白等了 10 分钟。
DEFAULT_BREAKER_THRESHOLD = 5

#: 开闸后多久允许一次试探（半开）。60 秒：够推理服务把队列排空或让进程
#: 重启完成，又不至于让财务科干等太久。
DEFAULT_BREAKER_COOLDOWN_SECONDS = 60.0

#: ``Retry-After`` 头给出的等待上限。渠道限流最多等到 30 秒 —— 再长就
#: 不如让本期降级、下期重来。
MAX_RETRY_AFTER_SECONDS = 30.0


class BreakerState(str, Enum):
    """熔断器三态。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    @property
    def status_zh(self) -> str:
        return {
            BreakerState.CLOSED: "闭合（正常调用）",
            BreakerState.OPEN: "开启（直接降级，不打模型）",
            BreakerState.HALF_OPEN: "半开（放一次试探）",
        }[self]


class FailurePhase(str, Enum):
    """失败发生在哪一段。

    刻意与 HTTP 状态码**分开**：同一个 500 既可能是「模型还在加载」
    （重试有意义），也可能是响应结构不对（重试没意义），而真正决定
    值不值得重试的是**失败发生在哪一段**。
    """

    TRANSPORT = "transport"   # 连接失败 / DNS / TLS
    TIMEOUT = "timeout"       # 连接超时或读超时
    HTTP = "http"             # 端点回了状态码
    PARSE = "parse"           # 响应体不是合法 JSON / 不合响应结构
    SCHEMA = "schema"         # JSON 合法但不合草稿 Schema


class FailureKind(str, Enum):
    """重试矩阵的行。**四类可重试 + 一类不可重试。**"""

    CONNECT = "connect"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    SCHEMA = "schema"
    #: 4xx 业务 / 配置 / 权限 / 协议错误：一次都不重试
    BUSINESS = "business"

    @property
    def status_zh(self) -> str:
        return {
            FailureKind.CONNECT: "连接失败",
            FailureKind.TIMEOUT: "超时",
            FailureKind.SERVER_ERROR: "服务端错误 / 限流",
            FailureKind.SCHEMA: "响应结构不合约定",
            FailureKind.BUSINESS: "业务 / 配置 / 协议错误（不重试）",
        }[self]


@dataclass(frozen=True)
class RetryRule:
    """一类失败的重试判据。

    ``max_retries`` 是**重试**次数（不含首次），所以实际请求数是
    ``max_retries + 1``。显式分开是因为 spec 说的是「重试 2 次」，
    混成「尝试 3 次」读起来总有一处对不上。
    """

    kind: FailureKind
    max_retries: int
    base_delay: float
    factor: float = 2.0
    max_delay: float = 8.0
    retryable: bool = True
    #: 为什么是这一组数字。写在数据里而不是文档里：改数字的人会先读到
    #: 理由，改完不必再回头翻设计文档确认自己有没有改错语义。
    rationale: str = ""

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "kind_zh": self.kind.status_zh,
            "max_retries": self.max_retries,
            "max_attempts": self.max_attempts,
            "base_delay": self.base_delay,
            "factor": self.factor,
            "max_delay": self.max_delay,
            "retryable": self.retryable,
            "retryable_zh": "重试" if self.retryable else "不重试",
        }


#: **重试矩阵**。四类可重试各有各的节奏，第五类显式不重试。
#:
#: 为什么四类的 ``base_delay`` 不一样：重试的等待时间该匹配「多久之后
#: 可能变好」，而不是统一取一个数。
#:
#: * :data:`FailureKind.CONNECT` 0.4s —— 连接被拒/端点没起来，等再久也
#:   不会因为等而变好；但院内网关偶发的连接重置是瞬时的，快重试划算。
#: * :data:`FailureKind.TIMEOUT` 1.2s —— 最长的一档。端点是**慢**不是
#:   **死**，队列排空要时间，退避太短只会让并发再堆一层。
#: * :data:`FailureKind.SERVER_ERROR` 0.8s —— 5xx/429 属于「正在重启或
#:   正在限流」，比连接失败慢一点、比读超时快一点。
#: * :data:`FailureKind.SCHEMA` 0.3s —— 本地校验失败，重试几乎立刻；
#:   除非端点支持 ``temperature>0``，同一份 prompt 可能给出不同结构。
RETRY_MATRIX: dict[FailureKind, RetryRule] = {
    FailureKind.CONNECT: RetryRule(
        kind=FailureKind.CONNECT,
        max_retries=2,
        base_delay=0.4,
        max_delay=4.0,
        rationale="连接被拒不会因为等待而变好，但院内网关的瞬时重置是常见的；快重试",
    ),
    FailureKind.TIMEOUT: RetryRule(
        kind=FailureKind.TIMEOUT,
        max_retries=2,
        base_delay=1.2,
        max_delay=8.0,
        rationale="端点是慢不是死，队列排空要时间；退避太短只会让并发再堆一层",
    ),
    FailureKind.SERVER_ERROR: RetryRule(
        kind=FailureKind.SERVER_ERROR,
        max_retries=2,
        base_delay=0.8,
        max_delay=6.0,
        rationale="5xx/429 属重启或限流，比连接失败慢一点、比读超时快一点",
    ),
    FailureKind.SCHEMA: RetryRule(
        kind=FailureKind.SCHEMA,
        max_retries=2,
        base_delay=0.3,
        max_delay=2.0,
        rationale="本地校验失败，重试几乎立刻；同一份 prompt 换个输出形态有可能就对",
    ),
    FailureKind.BUSINESS: RetryRule(
        kind=FailureKind.BUSINESS,
        max_retries=0,
        base_delay=0.0,
        retryable=False,
        rationale=(
            "4xx 业务 / 配置 / 权限 / 协议错误：重试只是把同一个错放大三次，"
            "并白耗院内推理配额。端点自己都说这是请求的错，不是它没准备好"
        ),
    ),
}

#: 5xx / 429 属服务端侧；其余 4xx 属请求侧。
_SERVER_STATUS_FLOOR = 500
_RATE_LIMIT_STATUS = 429


def classify_failure(
    *,
    phase: Optional[FailurePhase] = None,
    code: str = "",
    status_code: Optional[int] = None,
) -> FailureKind:
    """一次失败归到重试矩阵的哪一行。

    判据顺序刻意是**先看阶段、再看状态码、最后按错误码兜底**：阶段信息量最大
    （连接失败和读超时都可能是同一个 ``MODEL_UNAVAILABLE``，但重试节奏
    完全不同），错误码只是兜底。

    ``phase`` 允许不传 —— :class:`~reconq.gateway.model_gateway.GatewayError`
    在 raise 点漏传时会走这条兜底路径。**兜底不猜成可重试**：漏传阶段的
    raise 点多半是响应处理，默认成连接失败会让一个 Schema 错误按 0.4 秒的
    连接退避重试，排障时完全对不上。
    """
    if phase is FailurePhase.SCHEMA:
        return FailureKind.SCHEMA
    if phase is FailurePhase.PARSE:
        # 响应体坏了但 HTTP 是 200 —— 重试有意义（下次可能给出合法 JSON），
        # 但节奏按服务端侧走：坏的是端点的产出而不是我们的请求。
        return FailureKind.SERVER_ERROR
    if phase is FailurePhase.TIMEOUT:
        return FailureKind.TIMEOUT
    if phase is FailurePhase.HTTP:
        if status_code is None:
            return FailureKind.SERVER_ERROR
        if status_code >= _SERVER_STATUS_FLOOR or status_code == _RATE_LIMIT_STATUS:
            return FailureKind.SERVER_ERROR
        return FailureKind.BUSINESS
    if phase is FailurePhase.TRANSPORT:
        return FailureKind.CONNECT
    # 阶段没给出（调用方漏传）时退回按错误码判，**不猜成可重试**。
    if code in ("MODEL_TIMEOUT", "MODEL_CONNECT_TIMEOUT"):
        return FailureKind.TIMEOUT
    return FailureKind.BUSINESS


def rule_for(kind: FailureKind) -> RetryRule:
    return RETRY_MATRIX[kind]


def delay_for(
    rule: RetryRule,
    attempt: int,
    *,
    retry_after: Optional[float] = None,
    jitter_ratio: float = DEFAULT_JITTER_RATIO,
    rng: Optional[random.Random] = None,
) -> float:
    """第 ``attempt`` 次重试前该等多久（秒）。

    ``attempt`` 从 1 起（1 = 第一次重试）。指数退避封顶 ``max_delay``；
    ``retry_after`` 是端点主动给的等待（429 的 ``Retry-After`` 头），
    给了就以它为准但仍封顶 —— 端点说「等 600 秒」时，本产品等 30 秒然后
    降级，不因为一个头把整次扫描挂住。

    **抖动之后还要再夹一次上限**：``max_delay`` 是给这一类失败定的等待
    上限，抖动只该打散共振、**不该突破它**。先夹后抖的话一次 20% 的正
    抖动就把 8 秒变成 9.6 秒，文档里写的「封顶 8 秒」当场就不成立了。
    """
    if not rule.retryable or rule.base_delay <= 0:
        return 0.0
    nominal = rule.base_delay * (rule.factor ** max(0, attempt - 1))
    ceiling = rule.max_delay
    nominal = min(nominal, ceiling)
    if retry_after is not None and retry_after > nominal:
        # 端点主动给的等待可以超过本地节奏（限流就是这样），但仍封顶在
        # MAX_RETRY_AFTER_SECONDS —— 超过就不如让本期降级、下期重来。
        ceiling = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
        nominal = ceiling
    if jitter_ratio <= 0:
        return round(nominal, 4)
    source = rng if rng is not None else random
    jitter = source.uniform(-jitter_ratio, jitter_ratio)  # type: ignore[union-attr]
    return round(min(ceiling, max(0.0, nominal * (1.0 + jitter))), 4)


# ─────────────────────────── 熔断 ───────────────────────────


@dataclass
class CircuitBreaker:
    """熔断器。

    三态迁移（与主流实现一致，但参数为本产品定）：

    .. code-block:: text

        闭合 ──连续失败 ≥ 阈值──▶ 开启 ──冷却期到──▶ 半开
          ▲                                    │
          └────────── 试探成功 ────────────────┘
          ▲                                    │
          └────── 试探失败（重新计时）── 开启 ◀┘

    **半开只放一次试探**，不是「冷却期后恢复成闭合」。放多次试探等于
    闸门半开着让整队人挤进去 —— 那正是熔断要防的场景。
    """

    threshold: int = DEFAULT_BREAKER_THRESHOLD
    cooldown_seconds: float = DEFAULT_BREAKER_COOLDOWN_SECONDS
    clock: Callable[[], float] = time.monotonic
    #: 人类可读时刻。排障时「什么时候开的闸」要的是墙上时间而不是
    #: 单调秒数 —— 单调钟的 812.4 秒换算不出「10:12」。
    wall_clock: Callable[[], str] = time.strftime
    state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    consecutive_failures: int = field(default=0, init=False)
    opened_at: float = field(default=0.0, init=False)
    opened_at_wall: str = field(default="", init=False)
    recovered_at: float = field(default=0.0, init=False)
    recovered_at_wall: str = field(default="", init=False)
    opened_count: int = field(default=0, init=False)
    probe_in_flight: bool = field(default=False, init=False)
    probe_succeeded: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        # 用 :class:`~reconq.errors.ReconqError` 而不是裸 ``ValueError``：
        # 熔断阈值与冷却期终归要从环境变量读，裸栈冒到面板上时既没有错误码
        # 也没有建议动作，而这两处的排障动作完全不同（一个改阈值、一个改等待）。
        if self.threshold < 1:
            raise ReconqError(
                "MODEL_CONFIG_INVALID",
                f"熔断阈值 {self.threshold} 至少为 1。"
                f"取 1 会让「任何一条差异失败即封禁整个端点」，"
                f"而端点往往只是这一条证据太难",
            )
        if self.cooldown_seconds < 0:
            raise ReconqError(
                "MODEL_CONFIG_INVALID",
                f"熔断冷却期 {self.cooldown_seconds} 秒不能为负；"
                f"负数会让开闸后立刻允许试探，熔断形同虚设",
            )

    # —— 准入 ————————————————————————————————————————————

    def allow(self) -> tuple[bool, bool]:
        """能不能打模型。返回 ``(允许, 是否半开试探)``。

        调用方**必须**在 ``(False, ...)`` 时一个请求都不发 —— 熔断的价值
        全在「不打」上。
        """
        if self.state is BreakerState.CLOSED:
            return True, False
        if self.state is BreakerState.HALF_OPEN:
            if self.probe_in_flight:
                return False, False
            self.probe_in_flight = True
            return True, True
        # 开启态：冷却期到了才转半开
        if self.clock() - self.opened_at >= self.cooldown_seconds:
            self.state = BreakerState.HALF_OPEN
            self.probe_in_flight = True
            log_event(
                "model.breaker.half_open",
                level="info",
                message="熔断转入半开：放一次试探",
                breaker_state=self.state.value,
                opened_at=self.opened_at_wall,
            )
            return True, True
        return False, False

    def remaining_seconds(self) -> float:
        """距离允许试探还有多久（秒）。已到期返回 0.0，不给负数。"""
        if self.state is not BreakerState.CLOSED:
            return max(0.0, self.cooldown_seconds - (self.clock() - self.opened_at))
        return 0.0

    # —— 记账 ————————————————————————————————————————————

    def record_success(self, *, was_probe: bool = False) -> None:
        self.consecutive_failures = 0
        if self.state is BreakerState.CLOSED and not was_probe:
            return
        self.state = BreakerState.CLOSED
        self.probe_in_flight = False
        self.probe_succeeded = True
        self.recovered_at = self.clock()
        self.recovered_at_wall = self._now_wall()
        log_event(
            "model.breaker.closed",
            level="info",
            message=f"熔断恢复（试探成功）：累计开启 {self.opened_count} 次",
            breaker_state=self.state.value,
            opened_count=self.opened_count,
            recovered_at=self.recovered_at_wall,
        )

    def record_failure(self) -> None:
        """记一次失败。**半开试探失败会重新开闸并重新计时。**"""
        self.consecutive_failures += 1
        if self.state is BreakerState.HALF_OPEN:
            self._open()
            return
        if self.state is BreakerState.CLOSED and self.consecutive_failures >= self.threshold:
            self._open()

    def _open(self) -> None:
        self.state = BreakerState.OPEN
        self.probe_in_flight = False
        self.probe_succeeded = False
        self.opened_at = self.clock()
        self.opened_at_wall = self._now_wall()
        self.opened_count += 1
        log_event(
            "model.breaker.open",
            level="warning",
            message=(
                f"熔断开启：连续失败 {self.consecutive_failures} 次"
                f"（阈值 {self.threshold}）"
            ),
            breaker_state=self.state.value,
            consecutive_failures=self.consecutive_failures,
            opened_at=self.opened_at_wall,
            cooldown_seconds=self.cooldown_seconds,
        )

    def _now_wall(self) -> str:
        try:
            return self.wall_clock("%Y-%m-%dT%H:%M:%S%z")
        except (TypeError, ValueError):  # pragma: no cover - 注入的钟写错签名
            return ""

    def reset(self) -> None:
        """回到闭合态。**只给测试与运维显式重置用**，不在失败路径上调。"""
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self.probe_in_flight = False
        self.probe_succeeded = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "state_zh": self.state.status_zh,
            "consecutive_failures": self.consecutive_failures,
            "threshold": self.threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "remaining_seconds": round(self.remaining_seconds(), 3),
            "opened_count": self.opened_count,
            "opened_at": self.opened_at_wall,
            "recovered_at": self.recovered_at_wall,
        }


# ─────────────────────────── 降级台账 ───────────────────────────


@dataclass(frozen=True)
class DegradationRecord:
    """一次降级。**只记观测，不记业务状态。**"""

    period: str
    channel: str
    status: str
    reason: str
    kind: str
    attempts: int
    breaker_state: str = ""
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "status": self.status,
            "reason": self.reason,
            "reason_zh": _reason_zh(self.reason),
            "kind": self.kind,
            "kind_zh": FailureKind(self.kind).status_zh if _is_kind(self.kind) else self.kind,
            "attempts": self.attempts,
            "breaker_state": self.breaker_state,
            "at": self.at,
        }


def _is_kind(value: str) -> bool:
    return any(k.value == value for k in FailureKind)


_REASON_ZH: Mapping[str, str] = {
    "MODEL_UNAVAILABLE": "模型服务不可达",
    "MODEL_TIMEOUT": "模型调用超时",
    "MODEL_CONNECT_TIMEOUT": "模型连接超时",
    "MODEL_SCHEMA_INVALID": "返回结构不合约定",
    "MODEL_RESPONSE_INVALID": "响应不符合协议",
    "MODEL_CONFIG_INVALID": "网关配置缺位或非法",
    "MODEL_CIRCUIT_OPEN": "熔断已开启，未发请求",
    "ATTRIBUTION_EVIDENCE_REF_INVALID": "草稿引用了证据包之外的行",
    # Task 32。与上一码分开的理由同上：**行号真实、但对着它说错了话**，
    # 排障动作完全不同（改证据包组织方式或提示词，而不是让模型重读行号）。
    "ATTRIBUTION_EVIDENCE_MISMATCH": "草稿引用的行真实存在，但说法与该行对不上",
}


def _reason_zh(code: str) -> str:
    return _REASON_ZH.get(code, code)


@dataclass
class DegradationLedger:
    """本期降级观测台账。

    财务科要回答的是「这一期有几条差异没归因、为什么、什么时候开始的」，
    这三个数都在这里。**它不存任何业务状态** —— 已确认只能由人工点产生。
    """

    period: str = ""
    records: list[DegradationRecord] = field(default_factory=list)
    #: 墙上时间。**不给时跟随熔断器的钟**（:attr:`breaker.wall_clock`）——
    #: 两处各用各的钟会出现「降级记 10:12、熔断记 10:13」这种同一次故障
    #: 两个时刻的日志，面板上再也对不出谁先坏。都没给才退回系统时间。
    wall_clock: Optional[Callable[..., str]] = None
    #: 附带一份熔断器快照，写进 run summary 的就是它
    breaker: Optional[CircuitBreaker] = None

    def __post_init__(self) -> None:
        if self.wall_clock is None:
            self.wall_clock = self.breaker.wall_clock if self.breaker else time.strftime

    def record(
        self,
        *,
        channel: str,
        status: str,
        reason: str,
        kind: Any,
        attempts: int = 0,
    ) -> DegradationRecord:
        kind_value = kind.value if isinstance(kind, FailureKind) else str(kind)
        entry = DegradationRecord(
            period=self.period,
            channel=channel,
            status=status,
            reason=reason,
            kind=kind_value,
            attempts=attempts,
            breaker_state=self.breaker.state.value if self.breaker else "",
            at=self._now(),
        )
        self.records.append(entry)
        log_event(
            "model.degraded",
            level="warning",
            message=f"归因降级：{reason}（{FailureKind(kind_value).status_zh if _is_kind(kind_value) else kind_value}）",
            period=self.period,
            channel=channel,
            code=reason,
            status=status,
            failure_kind=kind_value,
            attempts=attempts,
            breaker_state=entry.breaker_state,
            # 显式带上降级发生的时刻。事件本身有 ts，但面板聚合时要把
            # 「本期第一次降级是什么时候」与「哪条差异先坏的」对起来看，
            # 靠 ts 就得先把 JSON 解析一遍再排序。
            at=entry.at,
        )
        return entry

    def _now(self) -> str:
        try:
            return self.wall_clock("%Y-%m-%dT%H:%M:%S%z")  # type: ignore[misc]
        except (TypeError, ValueError):  # pragma: no cover - 注入的钟写错签名
            return ""

    @property
    def count(self) -> int:
        return len(self.records)

    def by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.records:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def by_channel(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.records:
            counts[item.channel] = counts.get(item.channel, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def to_dict(self) -> dict[str, Any]:
        breaker = self.breaker.to_dict() if self.breaker else {}
        return {
            "period": self.period,
            "count": self.count,
            "by_reason": self.by_reason(),
            "by_channel": self.by_channel(),
            "first_at": self.records[0].at if self.records else "",
            "last_at": self.records[-1].at if self.records else "",
            "breaker_state": breaker.get("state", ""),
            "breaker_state_zh": breaker.get("state_zh", ""),
            "breaker_opened_at": breaker.get("opened_at", ""),
            "breaker_recovered_at": breaker.get("recovered_at", ""),
            "breaker_opened_count": breaker.get("opened_count", 0),
        }

    def to_run_summary_fields(self) -> dict[str, Any]:
        """给 :class:`reconq.run_summary.RunSummary` 的扁平字段。

        摘要、面板、结论单三处读的是**同一份**数字；这里返回的键名与
        ``RunSummary`` 的字段一一对应，由
        ``build_run_summary(degradation=...)`` 收口，不在两处各写一遍。
        """
        payload = self.to_dict()
        return {
            "degradation_count": payload["count"],
            "degradation_by_reason": payload["by_reason"],
            "breaker_state": payload["breaker_state"],
            "breaker_state_zh": payload["breaker_state_zh"],
            "breaker_opened_at": payload["breaker_opened_at"],
            "breaker_recovered_at": payload["breaker_recovered_at"],
            "breaker_opened_count": payload["breaker_opened_count"],
        }


def admission_of(breaker: Optional[CircuitBreaker]) -> tuple[bool, bool]:
    """无熔断器时一律放行（返回 ``(True, False)``）。"""
    if breaker is None:
        return True, False
    return breaker.allow()


def mark_pending_manual(reason: str) -> str:
    """降级 → 差异条目的中文标记。

    **刻意返回文字而不是去改台账**：本模块不持有连接也不 import
    ``engine.period``。调用方拿这句话去写消息 / 面板文案 / 结论单备注，
    处置状态仍由人工点推进。
    """
    zh = _reason_zh(reason)
    return f"待人工判定（归因降级：{reason} {zh}）"


def translation_of(reason: str) -> str:
    """:func:`mark_pending_manual` 的英文键别名（读代码时更清楚）。"""
    return mark_pending_manual(reason)
