"""模型网关：OpenAI-compatible 单协议调用（Task 28）。

`ai_role=core` 的**输出侧**
--------------------------

:mod:`reconq.align.bundle` 负责「把证据凑齐」，本模块负责「把这份证据送到
一个 OpenAI-compatible 端点、把回来的文本验成结构化对象、并把这次调用
留下可核的账」。它只认一条协议：``POST {base_url}/chat/completions``——
院内自建推理端点、本地部署模型与云服务共用这一份配置面，业务层不 import
任何厂商 SDK（换厂商改的是环境变量，不是代码）。

四条硬约束
----------

1. **凭据只从环境变量读，缺位在发请求前就拒。**
   四个位（``RECONQ_MODEL_BASE_URL`` / ``API_KEY`` / ``NAME`` / ``TIMEOUT``）
   任一缺位或取值非法 → :class:`~reconq.errors.ReconqError` ``MODEL_CONFIG_INVALID``
   并点名变量名。**不提供「缺 base_url 就走默认端点」的兜底** —— 那样会把
   一份含金额与订单号的证据包发到一个不知道是哪个服务的模型上。
2. **重试有上限，且分得清哪些错值得重试。** 超时 / 5xx / 429 / Schema 不过
   重试；4xx 里的配置类错误（401/403/404）**一次都不重试** —— 重试一个
   鉴权失败的请求只是把同一个错放大三次。
3. **不可达不抛到业务层。** :meth:`ModelGateway.chat_completion` 对外**永远
   返回一个 :class:`GatewayResult`**，降级时 ``ok=False`` + ``degraded=True``
   + ``error_code``，不抛异常。让编排层去 try/except 一次容易漏，而漏掉的
   那一处就是「模型挂了，扫描也跟着挂」。
4. **审计元数据不含证据正文。** 落日志的是「模型名 / 耗时 / token 用量 /
   请求摘要指纹 / 是否重试 / 错误码 / 证据包体量」，**不是** prompt 原文
   —— prompt 里是渠道账单行，落进日志库等于把账单抄了第二份。
   证据包体量与指纹由 :meth:`reconq.align.bundle.EvidenceBundle.audit_metadata`
   提供，本模块原样带上。

降级语义（与 spec 一致）
------------------------
不可达 / 超时 / 重试用尽 / 响应不合协议 → 归因链路走
:mod:`reconq.align.attribution` 的降级分支：只保留确定性口径结论，
全部差异标「待人工判定」，**台账状态不因模型不可达而推进**。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

import httpx

from ..errors import ReconqError
from ..runlog import log_event, redact_text
from .degradation import (
    DEFAULT_JITTER_RATIO,
    MAX_RETRY_AFTER_SECONDS as RETRY_AFTER_CAP,
    STATUS_DEGRADED,
    STATUS_OK,
    CircuitBreaker,
    DegradationLedger,
    FailureKind,
    FailurePhase,
    RetryRule,
    classify_failure,
    delay_for,
    rule_for,
)

logger = logging.getLogger("reconq.gateway")

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "CONNECT_TIMEOUT_ENV_VAR",
    "GatewayCall",
    "GatewayConfig",
    "GatewayError",
    "GatewayResult",
    "ModelGateway",
    "ResponseValidator",
]


#: 一次调用最多**尝试**几次（首次 + 2 次重试）。spec 定的是「重试 2 次」，
#: 这里把次数与重试分开记：``attempts`` 是真实发出去的请求数。
DEFAULT_MAX_ATTEMPTS = 3

#: 退避序列（秒）。**只在显式传 ``backoff`` 时生效** —— 默认路径走
#: :data:`reconq.gateway.degradation.RETRY_MATRIX` 的分档退避（连接失败
#: 0.4s、超时 1.2s、5xx 0.8s、Schema 0.3s），四类的等待时间本来就该不同。
#: 保留这个参数是为了让调用方与测试能钉死一条确定序列。
DEFAULT_BACKOFF = (0.5, 1.0)

#: 退避抖动比例。乘性（0.8~1.2 倍名义值），只打散「多任务同时重试」的
#: 共振；不做全抖动 —— 首次失败往往正是端点刚被打满的时刻，立刻再打
#: 一次大概率还是失败。关掉它（置 0）即可得到确定性序列。
DEFAULT_JITTER_RATIO_BACKOFF = DEFAULT_JITTER_RATIO

#: 响应体摘要在错误详情里保留的最大长度。超长响应（如模型把整张表回吐
#: 回来）只留头几百字 —— 错误详情会进面板、进日志、进结论单。
_BODY_EXCERPT_LIMIT = 400

#: 凭据位。缺位时按这个顺序逐个点名，顺序即报错顺序。
REQUIRED_ENV_VARS = ("RECONQ_MODEL_BASE_URL", "RECONQ_MODEL_API_KEY", "RECONQ_MODEL_NAME")

#: 超时位可以不配（给默认值），配了必须能解析成正数秒。
TIMEOUT_ENV_VAR = "RECONQ_MODEL_TIMEOUT"
DEFAULT_TIMEOUT_SECONDS = 30.0

#: **连接超时与读超时分开配置**（Task 30）。分开的理由是两者的处置动作
#: 完全不同：连不上先查院内网络与推理服务进程是否在监听；连上了不回话
#: 先查队列与模型是否加载。合成一个数时，财务科看到「超时」两个字只能
#: 两头都查一遍。
CONNECT_TIMEOUT_ENV_VAR = "RECONQ_MODEL_CONNECT_TIMEOUT"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

#: 合法错误码集合：归因层只认这些码决定降级分支，不认「别的什么错」。
DOWNGRADE_CODES = frozenset(
    {"MODEL_UNAVAILABLE", "MODEL_TIMEOUT", "MODEL_RESPONSE_INVALID", "MODEL_SCHEMA_INVALID"}
)

#: 这些码**不重试**：重试鉴权失败/端点不存在的请求只是把同一个错放大。
NON_RETRYABLE_CODES = frozenset({"MODEL_CONFIG_INVALID", "MODEL_RESPONSE_INVALID_AUTH"})

#: 结构化响应校验器：拿到解析后的 JSON，返回归一化后的对象；不合约定则抛
#: :class:`ValueError`（由网关归一为 ``MODEL_SCHEMA_INVALID``）。
ResponseValidator = Callable[[Any], Any]


class GatewayError(ReconqError):
    """网关内部异常。**只在网关内部流转**，不冒泡到业务层。

    多带几个字段给重试矩阵与降级链判断：``attempts``（真实发出的请求数）、
    ``phase``（失败发生在哪一段，见 :class:`FailurePhase`）、
    ``retry_after``（端点主动给的等待，429 的 ``Retry-After`` 头）。

    ``retryable`` 仍在，但**它的真源变成重试矩阵**：构造时不传就按
    ``phase`` + ``status_code`` 查 :data:`RETRY_MATRIX` 得出，而不是每个
    raise 点各写一遍 —— 那些点会漂移（Task 30 之前 401 与 429 恰好都靠
    人工记得写对 ``retryable``）。
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        attempts: int = 0,
        retryable: Optional[bool] = None,
        status_code: Optional[int] = None,
        phase: Optional[FailurePhase] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(code, detail)
        self.attempts = attempts
        self.status_code = status_code
        self.retry_after = retry_after
        # 阶段没给就按错误码兜底分类。**不默认成 TRANSPORT** —— 漏传阶段
        # 的 raise 点多半是响应处理，而默认成连接失败会让一个 Schema 错误
        # 按 0.4 秒的连接退避重试，排障时完全对不上。
        self.phase = phase if phase is not None else _phase_from_code(code, status_code)
        self.kind = classify_failure(phase=self.phase, code=code, status_code=status_code)
        self.rule: RetryRule = rule_for(self.kind)
        self.retryable = self.rule.retryable if retryable is None else retryable


# ─────────────────────────── 配置 ───────────────────────────


@dataclass(frozen=True)
class GatewayConfig:
    """模型网关配置。四个位全部来自环境变量，构造即校验。"""

    base_url: str
    api_key: str
    model: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: **连接超时**（秒），与 :attr:`timeout`（读超时）分层。见
    #: :data:`CONNECT_TIMEOUT_ENV_VAR` 的说明。
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    #: 请求指纹是否记进审计元数据。关掉不影响功能，但会让「同一份证据包
    #: 两次调用是不是同一个请求」这一条无法回答 —— 故默认开。
    audit: bool = True
    #: 退避抖动比例。0 = 确定性退避序列（测试与对拍场景用）。
    jitter_ratio: float = DEFAULT_JITTER_RATIO_BACKOFF

    def httpx_timeout(self) -> httpx.Timeout:
        """给 httpx 的分层超时。

        写/池超时对齐连接超时：写超时本质是「发不出去」，池超时是
        「拿不到连接」。两者都用连接超时的值，比统一用读超时合理 ——
        拿一个 30 秒的读超时去等连接池，池满时每次调用先白等 30 秒。
        """
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.timeout,
            write=self.connect_timeout,
            pool=self.connect_timeout,
        )

    @property
    def endpoint(self) -> str:
        """完整的 chat/completions 端点。

        ``base_url`` 允许带 ``/v1`` 尾巴，这里只补一次 ``/chat/completions``，
        不做「智能拼接」：补两次会得到 ``.../v1/v1/chat/completions``，
        而少补一次会打到 ``/v1`` 本身（多数端点 404，报错信息还很难懂）。
        """
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def auth_header(self) -> dict[str, str]:
        """Authorization 头。**只在内部拼**，不进审计元数据。"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def fingerprint(self) -> str:
        """配置指纹（不含凭据）。审计用：换了哪个模型/端点一眼可见。"""
        import hashlib

        raw = (
            f"{self.base_url}|{self.model}|{self.timeout}|{self.connect_timeout}"
            f"|{self.max_attempts}|{self.jitter_ratio}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """配置摘要。**刻意不含 base_url 与 api_key**：面板与事件链要的是
        「用的是哪个模型」，不是端点地址与密钥。"""
        return {
            "model": self.model,
            "timeout": self.timeout,
            "connect_timeout": self.connect_timeout,
            "max_attempts": self.max_attempts,
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "GatewayConfig":
        """从环境变量读配置；缺位或非法即抛 ``MODEL_CONFIG_INVALID``。

        一次列全**所有**缺失位，不是一个一个报 —— 让财务科跑五次补齐五个
        变量，比一次看清五个差在哪多跑四趟。
        """
        source = os.environ if env is None else env
        missing = [name for name in REQUIRED_ENV_VARS if not (source.get(name) or "").strip()]
        if missing:
            raise ReconqError(
                "MODEL_CONFIG_INVALID",
                f"模型网关配置缺位：{'、'.join(missing)}。"
                f"请在 .env 中配置后重启；本产品不提供默认端点兜底"
                f"（缺 base_url 就发请求 = 把账单证据发给不知道是哪个服务的模型）",
            )
        raw_timeout = (source.get(TIMEOUT_ENV_VAR) or "").strip()
        timeout = _positive_seconds(raw_timeout, TIMEOUT_ENV_VAR, DEFAULT_TIMEOUT_SECONDS)
        raw_connect = (source.get(CONNECT_TIMEOUT_ENV_VAR) or "").strip()
        connect_timeout = _positive_seconds(
            raw_connect, CONNECT_TIMEOUT_ENV_VAR, DEFAULT_CONNECT_TIMEOUT_SECONDS
        )
        if connect_timeout > timeout:
            # 不静默收紧。连接超时比读超时还长，说明配反了；把它悄悄改成
            # 读超时的值会让人以为「已经调过连接超时了」。
            raise ReconqError(
                "MODEL_CONFIG_INVALID",
                f"{CONNECT_TIMEOUT_ENV_VAR}={connect_timeout:g} 大于读超时 "
                f"{TIMEOUT_ENV_VAR}={timeout:g}，两者配反了。"
                f"连接超时应当**短于**读超时：连不上是网络与进程的问题，"
                f"连上不回话才是队列与模型的问题",
            )
        base_url = (source.get("RECONQ_MODEL_BASE_URL") or "").strip()
        if not base_url.lower().startswith(("http://", "https://")):
            raise ReconqError(
                "MODEL_CONFIG_INVALID",
                f"RECONQ_MODEL_BASE_URL={base_url!r} 不是 http(s) 端点。"
                f"应形如 http://127.0.0.1:8000/v1",
            )
        return cls(
            base_url=base_url,
            api_key=(source.get("RECONQ_MODEL_API_KEY") or "").strip(),
            model=(source.get("RECONQ_MODEL_NAME") or "").strip(),
            timeout=timeout,
            connect_timeout=connect_timeout,
        )


# ───────────────────── 审计元数据与返回值 ─────────────────────


@dataclass(frozen=True)
class GatewayCall:
    """一次调用的可审计元数据。

    **不含 prompt 原文、不含响应正文**。想看「这次问了什么」请看证据包
    自己的 ``digest``（同一份数据两次构造字节相同），别把正文抄进日志库。
    """

    model: str
    attempts: int
    elapsed_ms: float
    retried: bool = False
    ok: bool = True
    error_code: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    request_digest: str = ""
    response_chars: int = 0
    #: 证据包侧的审计字段（bundle.audit_metadata() 原样带上）
    bundle: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "retried": self.retried,
            "ok": self.ok,
            "error_code": self.error_code,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "request_digest": self.request_digest,
            "response_chars": self.response_chars,
            "bundle": dict(self.bundle),
        }


@dataclass(frozen=True)
class GatewayResult:
    """一次调用的结果。**降级时 ``ok=False`` 而不是抛异常**。

    Task 30 把三态显式写进类型：``status`` 取 :data:`STATUS_OK` /
    :data:`STATUS_DEGRADED`；降级时 ``reason`` 是错误码、``draft`` 必为
    ``None``。``ok`` / ``degraded`` / ``error_code`` 保留为兼容别名
    （``ok`` 即 ``status == STATUS_OK``），既有调用方与测试不必改。
    """

    ok: bool
    content: str = ""
    data: Any = None
    call: Optional[GatewayCall] = None
    error_code: str = ""
    error_detail: str = ""
    #: True 表示「这是降级结果，不是因为模型说了不能」。业务层据它决定
    #: 要不要把差异标成「待人工判定」。
    degraded: bool = False
    #: **这次失败**值不值得再来一次。刻意从 :class:`GatewayError` 带出来而
    #: 不是按错误码反查 —— ``MODEL_UNAVAILABLE`` 既可能是 5xx（值得重试）
    #: 也可能是 401（重试只是把同一个错放大三次），同一个码两种语义。
    retryable: bool = False
    #: 降级原因分类。面板与结论单用它把降级归组（连接 / 超时 / 5xx /
    #: Schema / 熔断），中文说明走 :data:`reconq.gateway.degradation`。
    failure_kind: str = ""
    #: 熔断器当时的状态快照（开着时降级的原因就是它）。
    breaker_state: str = ""

    @property
    def status(self) -> str:
        return STATUS_OK if self.ok else STATUS_DEGRADED

    @property
    def reason(self) -> str:
        """降级原因码。成功时为空串。"""
        return "" if self.ok else self.error_code

    @property
    def draft(self) -> Any:
        """校验通过的结构化草稿。**降级时恒为 ``None``** —— 降级路径拿不到
        任何可落库的成因，调用方若拿到非 None 就是在读一条不该存在的结论。"""
        return None if self.degraded else self.data

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "degraded": self.degraded,
            "reason": self.reason,
            "draft_is_none": self.draft is None,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "retryable": self.retryable,
            "failure_kind": self.failure_kind,
            "breaker_state": self.breaker_state,
            "call": self.call.to_dict() if self.call else None,
        }


def _phase_from_code(code: str, status_code: Optional[int]) -> FailurePhase:
    """错误码 → 阶段的兜底判据。

    只在 raise 点漏传 ``phase`` 时用。顺序刻意先看 Schema（本地校验，
    与协议无关）再看响应结构，最后才按「有没有 HTTP 状态码」分两段。
    """
    if code == "MODEL_SCHEMA_INVALID":
        return FailurePhase.SCHEMA
    if code == "MODEL_RESPONSE_INVALID":
        return FailurePhase.PARSE
    if code in ("MODEL_TIMEOUT", "MODEL_CONNECT_TIMEOUT"):
        return FailurePhase.TIMEOUT
    if status_code is not None:
        return FailurePhase.HTTP
    return FailurePhase.TRANSPORT


def _positive_seconds(raw: str, env_var: str, default: float) -> float:
    """环境变量里的秒数：空 → 默认；非数字 / 非正 → 点名变量报。"""
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ReconqError(
            "MODEL_CONFIG_INVALID",
            f"{env_var}={raw!r} 不是合法秒数；请写成正数（例如 30）",
        ) from None
    if value <= 0:
        raise ReconqError(
            "MODEL_CONFIG_INVALID",
            f"{env_var}={raw!r} 必须是正数；"
            f"0 或负数会让每次调用立刻「超时」，看起来像服务挂了",
        )
    return value


# ─────────────────────────── prompt 构造 ───────────────────────────


#: 系统提示词。**刻意把三条边界写进 prompt**：输出只是草稿、证据引用必须
#: 原样抄、没有证据就说不知道。prompt 不是防线（校验才是），但让模型
#: 一开始就知道边界在哪，比事后再纠正便宜得多。
SYSTEM_PROMPT = (
    "你是医院支付对账的归因助手。你会看到一条对账差异的全部证据"
    "（差异行、全部候选行、该渠道当期口径、账期时间窗、近几期同因历史）。\n"
    "请给出最可能的成因排序，每条形如：\n"
    '{"cause": 成因短语, "confidence": 0~1 的小数, '
    '"evidence_refs": ["<证据行标识>"], "explanation_zh": 中文说明}\n'
    "硬性要求：\n"
    "1. evidence_refs 只能**原样抄**证据里出现过的行标识，不得改写、"
    "不得编造、不得写「见上」；没有证据支撑的成因不要写。\n"
    "2. 你只给**草稿**供人工判断，不做最终结论。\n"
    "3. 证据不足时如实说明，不要编一个说得通的成因。\n"
    "4. 只输出 JSON，不要输出解释性文字或代码块围栏。"
)


def build_prompt(bundle_text: str, *, instructions: str = "") -> list[dict[str, str]]:
    """证据包文本 → OpenAI-compatible messages。

    ``instructions`` 是本条差异的补充交代（历史不可用原因、候选被裁剪等）。
    不塞进 system 段：system 段是**跨差异不变**的，混进逐条内容后
    provider 侧的 prompt 缓存会整段失效，六路各两千行差异下来费用翻倍。
    """
    user = f"以下是这条差异的证据：\n\n{bundle_text}"
    if instructions:
        user += f"\n\n补充说明：{instructions}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ─────────────────────────── 网关 ───────────────────────────


class ModelGateway:
    """单一模型网关。业务层只经它拿结果，不直接碰 httpx。"""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        client: Optional[httpx.Client] = None,
        backoff: Optional[Sequence[float]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        breaker: Optional[CircuitBreaker] = None,
        ledger: Optional[DegradationLedger] = None,
        period: str = "",
        channel: str = "",
        rng: Optional[Any] = None,
    ) -> None:
        self.config = config
        self._client = client
        #: ``backoff=None`` → 走重试矩阵的分档退避（默认）；给了序列 → 钉死
        #: 那条序列（测试与对拍用）。两者不混：显式序列是「运维要求精确
        #: 等待」的表达，矩阵是「按失败类型挑节奏」的表达。
        self._backoff = tuple(backoff) if backoff is not None else None
        self._clock = clock
        self._sleep = sleeper
        self.breaker = breaker
        self.ledger = ledger
        self._period = period
        self._channel = channel
        self._rng = rng

    @property
    def breaker_state(self) -> str:
        return self.breaker.state.value if self.breaker else ""

    # —— 对外唯一入口 ——————————————————————————————

    def chat_completion(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Optional[Mapping[str, Any]] = None,
        validator: Optional[ResponseValidator] = None,
        bundle_audit: Optional[Mapping[str, Any]] = None,
        extra_body: Optional[Mapping[str, Any]] = None,
    ) -> GatewayResult:
        """发一次（必要时重试）结构化补全。

        **不抛异常**：任何失败都变成 ``status=DEGRADED`` 的降级结果。见模块头
        「不可达不抛到业务层」。

        四段失败处理链依次生效：

        1. **熔断准入** —— 闸开着就一个请求都不发，直接降级（省掉的是
           「每条差异都等满超时」的时间，不是请求数）；
        2. **单次尝试** —— 失败按 :class:`FailurePhase` 归一；
        3. **重试矩阵** —— 按失败类型取次数与退避，不是一律三次；
        4. **降级** —— 次数用尽或熔断，返回 ``reason`` + ``draft=None``，
           并记进 :class:`DegradationLedger`。
        """
        body = self._build_body(messages, schema=schema, extra_body=extra_body)
        digest = _request_digest(body)
        audit_bundle = dict(bundle_audit or {})

        # —— 1. 熔断准入：一个请求都不发就降级 ——
        if self.breaker is not None:
            allowed, was_probe = self.breaker.allow()
            if not allowed:
                return self._degraded_for_open_breaker(digest, audit_bundle, body)
        else:
            was_probe = False

        started = self._clock()
        attempts = 0
        last: Optional[GatewayError] = None
        # 本次调用的重试上限：矩阵给的分档次数，再受网关总上限约束。
        retry_cap = self.config.max_attempts - 1

        for index in range(self.config.max_attempts):
            attempts = index + 1
            try:
                content, usage = self._attempt(body)
            except GatewayError as exc:
                exc.attempts = attempts
                last = exc
                if not self._should_retry(exc, attempts, retry_cap):
                    break
                self._log_call(
                    ok=False,
                    code=exc.code,
                    detail=exc.detail,
                    attempts=attempts,
                    elapsed_ms=round((self._clock() - started) * 1000, 3),
                    retried=True,
                    digest=digest,
                    bundle=audit_bundle,
                    failure_kind=exc.kind.value,
                )
                self._sleep(self._delay_for(exc, attempts))
                continue

            # —— 响应到手：解析与校验 ——
            try:
                parsed = self._parse(content, validator)
            except GatewayError as exc:
                exc.attempts = attempts
                last = exc
                if not self._should_retry(exc, attempts, retry_cap):
                    break
                self._log_call(
                    ok=False,
                    code=exc.code,
                    detail=exc.detail,
                    attempts=attempts,
                    elapsed_ms=round((self._clock() - started) * 1000, 3),
                    retried=True,
                    digest=digest,
                    bundle=audit_bundle,
                    failure_kind=exc.kind.value,
                )
                self._sleep(self._delay_for(exc, attempts))
                continue

            elapsed = round((self._clock() - started) * 1000, 3)
            call = GatewayCall(
                model=self.config.model,
                attempts=attempts,
                elapsed_ms=elapsed,
                retried=attempts > 1,
                ok=True,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                request_digest=digest,
                response_chars=len(content),
                bundle=audit_bundle,
            )
            self._log_call(
                ok=True,
                code="",
                detail="",
                attempts=attempts,
                elapsed_ms=elapsed,
                retried=attempts > 1,
                digest=digest,
                bundle=audit_bundle,
            )
            # 成功：熔断连续失败计数清零；半开试探成功即恢复。
            if self.breaker is not None:
                self.breaker.record_success(was_probe=was_probe)
            return GatewayResult(ok=True, content=content, data=parsed, call=call)

        assert last is not None
        elapsed = round((self._clock() - started) * 1000, 3)
        if self.breaker is not None:
            # **整次调用**才算一次失败，不是每个请求算一次：熔断阈值衡量
            # 的是「多少条差异归因不出来」，不是「发了多少个请求」。
            # 按请求计的话，一条差异的 3 次重试就能把闸打开。
            self.breaker.record_failure()
        self._log_call(
            ok=False,
            code=last.code,
            detail=last.detail,
            attempts=attempts,
            elapsed_ms=elapsed,
            retried=attempts > 1,
            digest=digest,
            bundle=audit_bundle,
            failure_kind=last.kind.value,
        )
        logger.warning(
            "模型调用降级：code=%s kind=%s attempts=%d detail=%s",
            last.code,
            last.kind.value,
            attempts,
            last.detail,
        )
        if self.ledger is not None:
            self.ledger.record(
                channel=self._channel,
                status=STATUS_DEGRADED,
                reason=last.code,
                kind=last.kind,
                attempts=attempts,
            )
        return GatewayResult(
            ok=False,
            degraded=True,
            error_code=last.code,
            error_detail=last.detail,
            retryable=last.retryable,
            failure_kind=last.kind.value,
            breaker_state=self.breaker_state,
            call=GatewayCall(
                model=self.config.model,
                attempts=attempts,
                elapsed_ms=elapsed,
                retried=attempts > 1,
                ok=False,
                error_code=last.code,
                request_digest=digest,
                bundle=audit_bundle,
            ),
        )

    # —— 失败处理链的三段判据 ————————————————————————

    def _should_retry(self, exc: GatewayError, attempts: int, retry_cap: int) -> bool:
        """值不值得再试一次。

        三个条件都要满足：本类失败可重试（重试矩阵）、还没用尽本类次数、
        还没用尽网关总上限。任一不满足即止。
        """
        if not exc.retryable:
            return False
        allowed = min(exc.rule.max_retries, retry_cap)
        return attempts <= allowed

    def _delay_for(self, exc: GatewayError, attempt: int) -> float:
        """第 ``attempt`` 次请求**之前**该等多久。

        显式给了 ``backoff`` 序列就用那条（测试与对拍要确定性）；否则按
        重试矩阵取该失败类型的分档退避 + 乘性抖动。
        """
        if self._backoff:
            index = min(attempt - 1, len(self._backoff) - 1)
            return self._backoff[index]
        return delay_for(
            exc.rule,
            attempt,
            retry_after=exc.retry_after,
            jitter_ratio=self.config.jitter_ratio,
            rng=self._rng,
        )

    def _degraded_for_open_breaker(
        self,
        digest: str,
        audit_bundle: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> GatewayResult:
        """熔断开启时的降级。**不发任何请求**，因此 ``attempts=0``。"""
        assert self.breaker is not None
        remaining = self.breaker.remaining_seconds()
        detail = (
            f"熔断已开启（连续失败达阈值 {self.breaker.threshold} 次），"
            f"本次未发任何请求直接降级；约 {remaining:.0f} 秒后放一次试探，"
            f"成功即恢复、失败重新计时。"
            f"本期归因能力按待人工判定处理，台账状态不推进"
        )
        code = "MODEL_CIRCUIT_OPEN"
        self._log_call(
            ok=False,
            code=code,
            detail=detail,
            attempts=0,
            elapsed_ms=0.0,
            retried=False,
            digest=digest,
            bundle=audit_bundle,
            failure_kind=FailureKind.BUSINESS.value,
        )
        if self.ledger is not None:
            self.ledger.record(
                channel=self._channel,
                status=STATUS_DEGRADED,
                reason=code,
                kind=FailureKind.BUSINESS,
                attempts=0,
            )
        return GatewayResult(
            ok=False,
            degraded=True,
            error_code=code,
            error_detail=detail,
            retryable=False,
            failure_kind=FailureKind.BUSINESS.value,
            breaker_state=self.breaker.state.value,
            call=GatewayCall(
                model=self.config.model,
                attempts=0,
                elapsed_ms=0.0,
                retried=False,
                ok=False,
                error_code=code,
                request_digest=digest,
                bundle=dict(audit_bundle),
            ),
        )

    # —— 内部 ——————————————————————————————————————

    def _build_body(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Optional[Mapping[str, Any]],
        extra_body: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [dict(m) for m in messages],
            "temperature": 0,
            "stream": False,
        }
        if schema is not None:
            # json_schema 是 OpenAI-compatible 里对结构化输出表达力最强的一档。
            # 并非所有兼容端点都认它，故**同时**在 system 段用文字约定结构
            # （见 SYSTEM_PROMPT）—— 端点只支持 json_object 时仍能靠文字
            # 约束 + 本地校验兜住，不至于整条链路瘫在「不支持该参数」上。
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "reconq_attribution",
                    "schema": dict(schema),
                    "strict": True,
                },
            }
        if extra_body:
            body.update(dict(extra_body))
        return body

    def _attempt(self, body: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """发一次请求。**任何失败都归一成带错误码的 ``GatewayError``**，
        不让 httpx 的异常类型漏到业务层。

        **连接超时与读超时分开归码**（Task 30）：``ConnectTimeout`` 是
        「连都没连上」（查网络与进程是否在监听），``ReadTimeout`` 是
        「连上了不回话」（查队列与模型是否加载）。合成一个码时两种排查
        方向都指向同一句话。
        """
        try:
            client = self._client
            if client is None:
                with httpx.Client(timeout=self.config.httpx_timeout()) as owned:
                    response = owned.post(
                        self.config.endpoint, json=dict(body), headers=self.config.auth_header()
                    )
            else:
                response = client.post(
                    self.config.endpoint, json=dict(body), headers=self.config.auth_header()
                )
        except httpx.ConnectTimeout as exc:
            raise GatewayError(
                "MODEL_CONNECT_TIMEOUT",
                f"连接模型服务超时（{self.config.connect_timeout:g} 秒连不上）："
                f"{type(exc).__name__}。可在 {CONNECT_TIMEOUT_ENV_VAR} 调大；"
                f"连不上先查院内网络与推理服务进程是否在监听",
                phase=FailurePhase.TIMEOUT,
            ) from exc
        except httpx.TimeoutException as exc:
            raise GatewayError(
                "MODEL_TIMEOUT",
                f"调用模型服务读超时（{self.config.timeout:g} 秒）：{type(exc).__name__}。"
                f"可在 {TIMEOUT_ENV_VAR} 调大；连上了不回话先查队列与模型是否加载。"
                f"超时走降级路径不阻塞扫描",
                phase=FailurePhase.TIMEOUT,
            ) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(
                "MODEL_UNAVAILABLE",
                f"模型服务不可达：{type(exc).__name__}：{_excerpt(str(exc))}",
                phase=FailurePhase.TRANSPORT,
            ) from exc

        if response.status_code >= 400:
            raise self._http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GatewayError(
                "MODEL_RESPONSE_INVALID",
                f"模型响应不是合法 JSON（HTTP {response.status_code}）："
                f"{_excerpt(response.text)}",
                status_code=response.status_code,
                phase=FailurePhase.PARSE,
            ) from exc
        return self._extract_content(payload, response)

    def _http_error(self, response: httpx.Response) -> ReconqError:
        """把 HTTP 状态码分类成带错误码的网关异常。

        ``retryable`` 一律**不显式传** —— 真源是重试矩阵按状态码判定，
        免得这一处的布尔与矩阵漂移（Task 30 之前 429 与 401 各写一遍，
        改矩阵时这两行不会跟着改）。

        返回注解写 ``ReconqError``（实际是 :class:`GatewayError` 子类）：
        这条返回注解是 ``tests/test_errors.py`` 的 AST 规则用来识别
        「工厂函数式建异常」的唯一判据，写窄了会被判成裸栈出口。
        """
        status = response.status_code
        detail = f"HTTP {status}：{_excerpt(_error_body(response))}"
        if status in (401, 403):
            return GatewayError(
                "MODEL_UNAVAILABLE",
                f"模型服务拒绝本次调用（鉴权或权限）：{detail}。"
                f"核对 RECONQ_MODEL_API_KEY 与该端点的授权范围。"
                f"鉴权/权限类一次都不重试 —— 重试只是把同一个错放大三次，"
                f"并白耗院内推理服务的配额",
                status_code=status,
                phase=FailurePhase.HTTP,
            )
        if status == 404:
            return GatewayError(
                "MODEL_UNAVAILABLE",
                f"端点不存在：{detail}。核对 RECONQ_MODEL_BASE_URL —— "
                f"多数兼容服务需要带 /v1 尾巴",
                status_code=status,
                phase=FailurePhase.HTTP,
            )
        if status == 429:
            return GatewayError(
                "MODEL_UNAVAILABLE",
                f"模型服务限流：{detail}。按 {RETRY_AFTER_CAP} 秒封顶等待",
                status_code=status,
                phase=FailurePhase.HTTP,
                retry_after=_retry_after_of(response),
            )
        if status >= 500:
            return GatewayError(
                "MODEL_UNAVAILABLE",
                f"模型服务返回服务端错误：{detail}",
                status_code=status,
                phase=FailurePhase.HTTP,
            )
        return GatewayError(
            "MODEL_RESPONSE_INVALID",
            f"模型服务拒绝该请求：{detail}",
            status_code=status,
            phase=FailurePhase.HTTP,
        )

    def _extract_content(
        self, payload: Any, response: httpx.Response
    ) -> tuple[str, dict[str, Any]]:
        """从 OpenAI-compatible 响应里取 ``choices[0].message.content``。"""
        if not isinstance(payload, dict):
            # 响应结构坏了但 HTTP 是 200：归 PARSE，按矩阵走服务端侧退避
            # 重试一次（截断的响应体下次可能完整）。**不显式传 retryable**。
            raise GatewayError(
                "MODEL_RESPONSE_INVALID",
                f"模型响应顶层不是对象而是 {type(payload).__name__}："
                f"{_excerpt(_safe_json(payload))}",
                status_code=response.status_code,
                phase=FailurePhase.PARSE,
            )
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            # 200 + error 是自建端点最常见的「软失败」形态。**这一类显式
            # 不重试**：服务端自己都说这是它的错（模型没加载、参数不认），
            # 重试同一份请求只会拿到同一段错误。归 HTTP 阶段（状态码 200
            # 在矩阵里属不可重试的请求侧）而不是靠布尔硬写。
            raise GatewayError(
                "MODEL_UNAVAILABLE",
                f"模型服务返回 error 字段：{_excerpt(str(error.get('message')))}"
                f"（code={error.get('code')} type={error.get('type')}）",
                status_code=response.status_code,
                phase=FailurePhase.HTTP,
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GatewayError(
                "MODEL_RESPONSE_INVALID",
                f"响应缺少 choices 数组（顶层键：{sorted(payload)[:8]}）。"
                f"该端点可能并不真的兼容 OpenAI 协议",
                status_code=response.status_code,
                phase=FailurePhase.PARSE,
            )
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            finish = first.get("finish_reason") if isinstance(first, dict) else None
            raise GatewayError(
                "MODEL_RESPONSE_INVALID",
                f"响应 content 为空或不是字符串（finish_reason={finish!r}）。"
                f"常见于内容过滤命中或该端点只支持流式返回",
                status_code=response.status_code,
                phase=FailurePhase.PARSE,
            )
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return content, usage

    def _parse(self, content: str, validator: Optional[ResponseValidator]) -> Any:
        """content → 结构化对象。

        刻意容忍代码块围栏（``\\`\\`\\`json … \\`\\`\\```）：不少兼容端点即使
        认了 ``response_format`` 也会顺手包一层围栏。**只剥围栏，不做
        「就近找一段 JSON」** —— 后者会把一段解释文字里的示例 JSON 捡出来
        当成结果，那比解析失败危险得多。
        """
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 2:
                text = "\n".join(lines[1:]).strip()
            if text.endswith("```"):
                text = text[: -len("```")].strip()
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            # JSON 坏了但 HTTP 是 200 —— 归 PARSE，按服务端侧节奏重试。
            # 上一轮 Task 28 把它标成不可重试，理由是「重试只会拿到同一段
            # 文本」；实际上带围栏/截断的响应体换一次输出形态就能合法，
            # 而本产品 ``temperature=0`` 仍挡不住端点侧的采样差异。
            raise GatewayError(
                "MODEL_RESPONSE_INVALID",
                f"模型返回的不是合法 JSON：{_excerpt(content)}（{exc}）",
                phase=FailurePhase.PARSE,
            ) from exc
        if validator is None:
            return parsed
        try:
            return validator(parsed)
        except ReconqError as exc:
            # 校验器自己抛的是**业务异常**（带自己的码与建议动作，如
            # ATTRIBUTION_DRAFT_INVALID）。这里归一成 MODEL_SCHEMA_INVALID
            # —— 对外是「模型返回结构不合约定」，内层那层码在 detail 里，
            # 排障时能分开看是协议不合约还是草稿字段不合约。
            raise GatewayError(
                "MODEL_SCHEMA_INVALID",
                f"模型返回结构不符合约定 Schema：{exc.detail}"
                f"（只剥了代码块围栏，不做就近匹配）",
                phase=FailurePhase.SCHEMA,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 校验器抛什么都归一
            raise GatewayError(
                "MODEL_SCHEMA_INVALID",
                f"模型返回结构不符合约定 Schema：{exc}。"
                f"（只剥了代码块围栏，不做就近匹配）",
                phase=FailurePhase.SCHEMA,
            ) from exc

    def _log_call(
        self,
        *,
        ok: bool,
        code: str,
        detail: str,
        attempts: int,
        elapsed_ms: float,
        retried: bool,
        digest: str,
        bundle: Mapping[str, Any],
        failure_kind: str = "",
    ) -> None:
        """落一条 ``model.call`` 事件。

        **落的是元数据不是正文**：detail 走一遍
        :func:`~reconq.runlog.redact_text`（模型偶尔会把 prompt 里的
        手机号样串回显在错误里），bundle 侧只带体量与指纹。

        ``failure_kind`` 是 Task 30 加的一维：同一个 ``MODEL_UNAVAILABLE``
        既可能是连接失败也可能是 5xx，光看错误码排障要再去翻一次状态码；
        带上分类后，面板可以直接按「连接 / 超时 / 5xx / Schema / 熔断」
        分组显示降级次数。
        """
        safe_detail, _ = redact_text(detail)
        log_event(
            "model.call" if ok else "model.call.failed",
            level="info" if ok else "warning",
            message=f"模型调用 {'成功' if ok else '失败'}：{code or 'OK'}",
            code=code,
            duration_ms=elapsed_ms,
            model=self.config.model,
            attempts=attempts,
            retried=retried,
            request_digest=digest,
            config_fingerprint=self.config.fingerprint(),
            failure_kind=failure_kind,
            breaker_state=self.breaker_state,
            detail=safe_detail[:_BODY_EXCERPT_LIMIT],
            bundle=dict(bundle),
        )


# ─────────────────────────── 小工具 ───────────────────────────


def _request_digest(body: Mapping[str, Any]) -> str:
    """请求摘要指纹（sha256 前 16 位）。

    刻意**不含** Authorization 头：请求体里没有凭据，指纹可以安全进日志
    与事件链。同一份证据包两次构造指纹相同 → 可回答「这两次问的是不是
    同一件事」。
    """
    import hashlib

    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _retry_after_of(response: httpx.Response) -> Optional[float]:
    """读 ``Retry-After`` 头。**解析不了就返回 None 而不是 0** ——
    返回 0 等于告诉退避「端点让你立刻重试」，那正是限流要防的事。"""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (TypeError, ValueError):
        # HTTP-date 形态（Retry-After: Wed, 21 Oct 2015 07:28:00 GMT）。
        # 不去解析日期：算出来的秒数误差可达分钟级，而退避本来就封顶在
        # MAX_RETRY_AFTER_SECONDS，误差不影响结果。
        return None


def _excerpt(text: str) -> str:
    """截断长文本。错误详情会进面板/日志/结论单，不能整段贴。"""
    flat = " ".join((text or "").split())
    if len(flat) <= _BODY_EXCERPT_LIMIT:
        return flat
    return flat[:_BODY_EXCERPT_LIMIT] + "…（已截断）"


def _error_body(response: httpx.Response) -> str:
    """从错误响应里挖出最能说明问题的那段文本。

    顺序刻意是 ``error.message`` → ``message`` → 原始正文：OpenAI-compatible
    的错误信封有两种常见写法，兼容端点爱用后者。
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if payload.get("message"):
            return str(payload["message"])
    return _safe_json(payload)


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)[:_BODY_EXCERPT_LIMIT]
    except (TypeError, ValueError):
        return repr(value)[:_BODY_EXCERPT_LIMIT]
