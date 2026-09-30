"""网关失败处理链：重试矩阵、4xx 不重试、熔断与半开、降级不推进状态、降级入链。

这一层解决的**不是**「一次调用失败怎么办」——那是
:mod:`tests.test_model_gateway` 的事。这里解决的是**一次扫描里几百条差异
连着调同一个端点**时端点挂了怎么办：每条差异都等满 30 秒超时，一次 40 秒
的扫描会变成 40 分钟，最后还因为拿不到成因而全部待人工判定。纯重试解决
不了这件事，重试只会把等待乘以三 —— 所以要有熔断。

五类规定断言：

1. **四类错误重试矩阵** —— 连接失败 / 超时 / 5xx / Schema 不过 各自有次数
   与**分档退避**（不是一律三次），且退避按失败类型不同。
2. **4xx 不重试** —— 401/403/404 一次都不重试（把同一个错放大三次并白耗
   院内推理配额）。
3. **熔断开启与半开恢复** —— 连续失败达阈值后**一个请求都不发**；冷却期到
   放**一次**试探；成功即恢复、失败重新计时。
4. **降级后状态集合不变** —— 不可违反的红线：模型不可达时差异台账状态
   一律不推进，已确认只能由人工点产生。
5. **降级事件入链** —— 降级次数、熔断开/恢复时刻进事件链与 run summary，
   且**留的是时刻不是「归因不可用」四个字**。

网络一律用 ``httpx.MockTransport`` + 假时钟：替身只在测试数据层，产品代码
走真实 OpenAI-compatible 协议。院内模型端点需院内凭据，本轮未实连。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Optional

import httpx
import pytest

from reconq.align.attribution import attribute
from reconq.align.bundle import EvidenceBundle, build_evidence_bundle
from reconq.align.matcher import AlignRecord, align_channel, build_index
from reconq.contracts.loader import load_contract
from reconq.engine.period import (
    DIFF_TABLE,
    DISPOSITION_CONFIRMED,
    DISPOSITION_PENDING,
    init_scan_schema,
    list_diff_items,
)
from reconq.errors import ReconqError, error_meta
from reconq.gateway.degradation import (
    DEFAULT_BREAKER_THRESHOLD,
    MAX_RETRY_AFTER_SECONDS,
    RETRY_MATRIX,
    STATUS_DEGRADED,
    STATUS_OK,
    BreakerState,
    CircuitBreaker,
    DegradationLedger,
    FailureKind,
    FailurePhase,
    classify_failure,
    delay_for,
    mark_pending_manual,
    rule_for,
)
from reconq.gateway.model_gateway import (
    CONNECT_TIMEOUT_ENV_VAR,
    TIMEOUT_ENV_VAR,
    GatewayConfig,
    ModelGateway,
)
from reconq.sources.timezone import HOSPITAL_TZ, period_window
from reconq.store.schema import connect, init_schema

TZ = HOSPITAL_TZ

FAKE_BASE_URL = "http://127.0.0.1:9/v1"
FAKE_KEY = "FAKE_MODEL_KEY_PLACEHOLDER"
FAKE_MODEL = "reconq-attribution-1"
PERIOD = "2026-W36"


# ───────────────────── 假时钟与替身 ─────────────────────


class FakeClock:
    """可推进的单调钟 + 固定墙上时间。**不真等**。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.wall = ["2026-09-07T09:00:00+0800"]

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def wall_clock(self, fmt: str) -> str:
        assert fmt  # 熔断只按一种格式取墙上时间
        return self.wall[0]


class Sleeper:
    """记录睡过的每一段。**退避序列因此可断言。**"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _ok(_body: str = "") -> Callable[[httpx.Request], httpx.Response]:
    """一个**永远成功**的 handler。注意不是 handler 本身 —— 直接把
    ``httpx.Response`` 交给 MockTransport 会让 httpx 去 json 编码 request
    对象（``Request is not JSON serializable``）。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return _ok_response(_body)

    return handler


def _ok_response(body: str = "") -> httpx.Response:
    payload = {
        "model": FAKE_MODEL,
        "choices": [{"message": {"role": "assistant", "content": body or "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return httpx.Response(200, json=payload)


def _config(**kwargs: Any) -> GatewayConfig:
    base = {
        "base_url": FAKE_BASE_URL,
        "api_key": FAKE_KEY,
        "model": FAKE_MODEL,
        "jitter_ratio": 0.0,  # 退避序列要可断言
    }
    base.update(kwargs)
    return GatewayConfig(**base)


def _gateway(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleeper: Optional[Sleeper] = None,
    breaker: Optional[CircuitBreaker] = None,
    ledger: Optional[DegradationLedger] = None,
    config: Optional[GatewayConfig] = None,
) -> tuple[ModelGateway, list[httpx.Request], Sleeper]:
    seen: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    slp = sleeper or Sleeper()
    client = httpx.Client(transport=httpx.MockTransport(_wrapped))
    gw = ModelGateway(
        config or _config(),
        client=client,
        sleeper=slp,
        breaker=breaker,
        ledger=ledger,
        period=PERIOD,
        channel="wechat",
    )
    return gw, seen, slp


def _always(raiser: Callable[[], Exception]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise raiser()

    return handler


def _status(code: int, headers: dict[str, str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json={"error": {"message": "端点说不行"}}, headers=headers or {})

    return handler


def _parse_fail(_request: httpx.Request) -> httpx.Response:
    """HTTP 200 但正文不是 JSON —— 走 ``MODEL_RESPONSE_INVALID``（PARSE 段）。"""
    return httpx.Response(200, text="<html>网关维护页</html>")


def _rejecting_validator(_parsed: Any) -> Any:
    """本地草稿校验不通过 —— 走 ``MODEL_SCHEMA_INVALID``（SCHEMA 段）。

    **必须真给一个 validator**：响应结构合法但草稿不合约定时，失败点才落在
    SCHEMA 段；只回一段坏 HTML 落的是 PARSE 段，而 PARSE 按设计归到服务端
    侧（坏的是端点的产出而不是我们的请求），与 SCHEMA 段节奏相同。
    """
    raise ValueError("drafts 字段缺失")


# ───────────────────── 一、四类错误重试矩阵 ─────────────────────


def test_matrix_has_four_retryable_classes_plus_a_non_retryable_one():
    retryable = {k for k, r in RETRY_MATRIX.items() if r.retryable}
    assert retryable == {
        FailureKind.CONNECT,
        FailureKind.TIMEOUT,
        FailureKind.SERVER_ERROR,
        FailureKind.SCHEMA,
    }
    business = RETRY_MATRIX[FailureKind.BUSINESS]
    assert business.retryable is False
    assert business.max_retries == 0
    # 矩阵是数据不是文档：每一行都要带得出「为什么是这组数字」
    for kind, rule in RETRY_MATRIX.items():
        assert rule.rationale, f"{kind.value} 缺 rationale，改数字的人会先读到理由"
        assert rule.max_attempts == rule.max_retries + 1


@pytest.mark.parametrize(
    "phase,code,status_code,expected",
    [
        (FailurePhase.TRANSPORT, "MODEL_UNAVAILABLE", None, FailureKind.CONNECT),
        (FailurePhase.TIMEOUT, "MODEL_TIMEOUT", None, FailureKind.TIMEOUT),
        (FailurePhase.TIMEOUT, "MODEL_CONNECT_TIMEOUT", None, FailureKind.TIMEOUT),
        (FailurePhase.HTTP, "MODEL_UNAVAILABLE", 503, FailureKind.SERVER_ERROR),
        (FailurePhase.HTTP, "MODEL_UNAVAILABLE", 429, FailureKind.SERVER_ERROR),
        (FailurePhase.SCHEMA, "MODEL_SCHEMA_INVALID", None, FailureKind.SCHEMA),
        (FailurePhase.PARSE, "MODEL_RESPONSE_INVALID", 200, FailureKind.SERVER_ERROR),
        (FailurePhase.HTTP, "MODEL_RESPONSE_INVALID", 401, FailureKind.BUSINESS),
        (FailurePhase.HTTP, "MODEL_RESPONSE_INVALID", 403, FailureKind.BUSINESS),
        (FailurePhase.HTTP, "MODEL_RESPONSE_INVALID", 404, FailureKind.BUSINESS),
        (FailurePhase.HTTP, "MODEL_RESPONSE_INVALID", 400, FailureKind.BUSINESS),
    ],
)
def test_classify_failure_maps_every_phase_and_status(phase, code, status_code, expected):
    assert classify_failure(phase=phase, code=code, status_code=status_code) is expected


def test_classify_failure_without_phase_does_not_guess_retryable():
    """阶段没传时退回按错误码判，**不默认成可重试**。

    漏传阶段的 raise 点多半是响应处理；默认成连接失败会让一个 Schema 错误
    按 0.4 秒的连接退避重试，排障时完全对不上。
    """
    assert classify_failure(code="MODEL_TIMEOUT") is FailureKind.TIMEOUT
    assert classify_failure(code="MODEL_CONNECT_TIMEOUT") is FailureKind.TIMEOUT
    assert rule_for(classify_failure(code="SOMETHING_ELSE")).retryable is False


def test_connect_failure_retries_twice_then_degrades():
    gw, seen, slp = _gateway(_always(lambda: httpx.ConnectError("拒绝连接")))
    result = gw.chat_completion([{"role": "user", "content": "证据"}])
    assert len(seen) == 3, "首次 + 2 次重试"
    assert result.ok is False
    assert result.status == STATUS_DEGRADED
    assert result.degraded is True
    assert result.error_code == "MODEL_UNAVAILABLE"
    assert result.failure_kind == FailureKind.CONNECT.value
    assert result.retryable is True
    assert result.draft is None
    assert result.call.attempts == 3
    assert result.call.retried is True


def test_read_timeout_and_connect_timeout_are_separate_error_codes():
    """连都没连上 vs 连上不回话：两种排查方向，不能合成一个码。"""
    read_gw, read_seen, _ = _gateway(_always(lambda: httpx.ReadTimeout("读超时")))
    read_result = read_gw.chat_completion([{"role": "user", "content": "证据"}])
    assert read_result.error_code == "MODEL_TIMEOUT"
    assert len(read_seen) == 3

    conn_gw, conn_seen, _ = _gateway(_always(lambda: httpx.ConnectTimeout("连接超时")))
    conn_result = conn_gw.chat_completion([{"role": "user", "content": "证据"}])
    assert conn_result.error_code == "MODEL_CONNECT_TIMEOUT"
    assert conn_result.failure_kind == FailureKind.TIMEOUT.value
    assert len(conn_seen) == 3

    # 两个码各自带处置方向，且都登记在案
    assert "队列" in error_meta("MODEL_TIMEOUT").suggestion
    assert "监听" in error_meta("MODEL_CONNECT_TIMEOUT").suggestion


def test_server_error_and_schema_failure_each_get_their_own_backoff_pace():
    """四类退避节奏必须**不同** —— 统一成一个数等于没分类。"""
    gw, _, five_xx_slp = _gateway(_status(503))
    gw.chat_completion([{"role": "user", "content": "证据"}])

    gw2, _, schema_slp = _gateway(_ok(), config=_config())
    gw2.chat_completion(
        [{"role": "user", "content": "证据"}], validator=_rejecting_validator
    )

    gw3, _, timeout_slp = _gateway(_always(lambda: httpx.ReadTimeout("读超时")))
    gw3.chat_completion([{"role": "user", "content": "证据"}])

    assert timeout_slp.calls[0] > five_xx_slp.calls[0] > schema_slp.calls[0], (
        "读超时（端点是慢不是死）应等最久，Schema 失败几乎立刻重试"
    )
    # 指数：第二次重试不短于第一次；且每段都不突破该类自己的上限
    for slp in (timeout_slp, five_xx_slp, schema_slp):
        assert slp.calls[1] >= slp.calls[0]
    assert max(timeout_slp.calls) <= rule_for(FailureKind.TIMEOUT).max_delay
    assert max(five_xx_slp.calls) <= rule_for(FailureKind.SERVER_ERROR).max_delay
    assert max(schema_slp.calls) <= rule_for(FailureKind.SCHEMA).max_delay


def test_backoff_is_capped_and_jitter_stays_multiplicative():
    """退避有上限；抖动是乘性不是全抖动（首次失败往往正是端点被打满时）。"""
    rule = RETRY_MATRIX[FailureKind.TIMEOUT]
    for attempt in range(1, 8):
        assert delay_for(rule, attempt) <= rule.max_delay

    for _ in range(40):
        value = delay_for(rule, 1)
        nominal = rule.base_delay
        assert nominal * 0.8 <= value <= nominal * 1.2

    # 显式 jitter=0 → 确定性序列
    assert delay_for(rule, 1, jitter_ratio=0.0) == pytest.approx(rule.base_delay)
    # 不重试的一类给 0，不因为「base_delay 是 0」而巧合
    assert delay_for(RETRY_MATRIX[FailureKind.BUSINESS], 1) == 0.0


def test_retry_after_header_is_honoured_but_still_capped():
    """端点说「等 600 秒」时本产品等 30 秒然后降级，不因一个头挂住整次扫描。"""
    rule = RETRY_MATRIX[FailureKind.SERVER_ERROR]
    assert delay_for(rule, 1, retry_after=600.0, jitter_ratio=0.0) == pytest.approx(MAX_RETRY_AFTER_SECONDS)
    # 开启抖动后仍不得超过 30 秒（抖动只会更短或持平，不会突破封顶）
    for _ in range(20):
        assert delay_for(rule, 1, retry_after=600.0) <= MAX_RETRY_AFTER_SECONDS
    # 比名义值小的时候不采纳 —— 端点给的比本地节奏还短，硬等会让退避失效
    assert delay_for(rule, 1, retry_after=0.01, jitter_ratio=0.0) == pytest.approx(
        rule.base_delay
    )


def test_429_honours_retry_after_from_the_real_response():
    gw, seen, slp = _gateway(_status(429, headers={"Retry-After": "2"}))
    result = gw.chat_completion([{"role": "user", "content": "证据"}])
    assert len(seen) == 3
    assert result.failure_kind == FailureKind.SERVER_ERROR.value
    assert slp.calls[0] == pytest.approx(2.0)


def test_unparseable_retry_after_does_not_read_as_zero():
    """解析不了就当没给 —— 返回 0 等于告诉退避「立刻重试」，正是限流要防的事。"""
    gw, _, slp = _gateway(_status(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
    gw.chat_completion([{"role": "user", "content": "证据"}])
    assert slp.calls[0] > 0


def test_timeout_config_rejects_connect_longer_than_read():
    """连接超时比读超时还长说明配反了，**不静默收紧**。"""
    with pytest.raises(ReconqError) as excinfo:
        GatewayConfig.from_env(
            {
                "RECONQ_MODEL_BASE_URL": FAKE_BASE_URL,
                "RECONQ_MODEL_API_KEY": FAKE_KEY,
                "RECONQ_MODEL_NAME": FAKE_MODEL,
                TIMEOUT_ENV_VAR: "5",
                CONNECT_TIMEOUT_ENV_VAR: "30",
            }
        )
    assert excinfo.value.code == "MODEL_CONFIG_INVALID"
    assert CONNECT_TIMEOUT_ENV_VAR in excinfo.value.detail


def test_httpx_timeout_layers_connect_read_write_pool():
    timeout = _config(timeout=20.0, connect_timeout=4.0).httpx_timeout()
    assert timeout.connect == 4.0
    assert timeout.read == 20.0
    assert timeout.write == 4.0
    assert timeout.pool == 4.0, "拿 30 秒读超时去等连接池，池满时每次先白等 30 秒"


# ───────────────────── 二、4xx 不重试 ─────────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_4xx_never_retries(status):
    gw, seen, slp = _gateway(_status(status))
    result = gw.chat_completion([{"role": "user", "content": "证据"}])
    assert len(seen) == 1, f"HTTP {status} 一次都不该重试"
    assert slp.calls == [], "不重试就不该有任何等待"
    assert result.retryable is False
    assert result.failure_kind == FailureKind.BUSINESS.value
    assert result.call.attempts == 1
    # 401/403/404 有专门分支归 MODEL_UNAVAILABLE（鉴权/权限/端点不存在）；
    # 其余 4xx 落到兜底的「端点拒绝该请求」。**分类一律看 failure_kind**，
    # 不按错误码反查 —— 同一个码两种语义正是 Task 30 要根治的问题。
    expected_code = "MODEL_UNAVAILABLE" if status in (401, 403, 404) else "MODEL_RESPONSE_INVALID"
    assert result.error_code == expected_code



def test_4xx_vs_5xx_retryability_comes_from_the_matrix_not_the_error_code():
    """同一个 ``MODEL_UNAVAILABLE``：5xx 值得重试，401 重试只是放大错。"""
    gw5, seen5, _ = _gateway(_status(500))
    assert gw5.chat_completion([{"role": "user", "content": "证据"}]).retryable is True
    assert len(seen5) == 3

    gw4, seen4, _ = _gateway(_status(401))
    assert gw4.chat_completion([{"role": "user", "content": "证据"}]).retryable is False
    assert len(seen4) == 1


def test_model_config_invalid_raises_before_any_request():
    """缺凭据位时**一个请求都不发** —— 账单证据不该发给一个不存在的端点。"""
    with pytest.raises(ReconqError):
        GatewayConfig.from_env({"RECONQ_MODEL_NAME": FAKE_MODEL})


# ───────────────────── 三、熔断开启与半开恢复 ─────────────────────


def test_breaker_threshold_default_exceeds_single_call_attempts():
    """阈值必须**大于**单条差异的尝试数。

    否则第一条差异失败（首次 + 2 次重试 = 3）就把闸打开，熔断退化成
    「一次失败即封禁」，而端点往往只是这一条证据太难。
    """
    assert DEFAULT_BREAKER_THRESHOLD > 3


def test_breaker_opens_after_threshold_and_sends_nothing():
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    ledger = DegradationLedger(period=PERIOD, breaker=breaker)

    # 前两条差异：各自重试用尽 → 记两次失败
    for _ in range(2):
        gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("拒绝连接")), breaker=breaker, ledger=ledger)
        gw.chat_completion([{"role": "user", "content": "证据"}])
    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 2

    # 第三条把闸打开
    gw, seen, _ = _gateway(_always(lambda: httpx.ConnectError("拒绝连接")), breaker=breaker, ledger=ledger)
    gw.chat_completion([{"role": "user", "content": "证据"}])
    assert breaker.state is BreakerState.OPEN
    assert breaker.consecutive_failures == 3
    assert len(seen) == 3  # 这条本身跑完了重试

    # 第四条：**一个请求都不发**
    gw, seen, slp = _gateway(_always(lambda: httpx.ConnectError("拒绝连接")), breaker=breaker, ledger=ledger)
    result = gw.chat_completion([{"role": "user", "content": "证据"}])
    assert seen == [], "熔断的价值全在「不打」上"
    assert slp.calls == []
    assert result.error_code == "MODEL_CIRCUIT_OPEN"
    assert result.call.attempts == 0
    assert result.breaker_state == BreakerState.OPEN.value
    assert "待人工判定" in result.error_detail
    assert breaker.opened_count == 1
    assert breaker.opened_at_wall == "2026-09-07T09:00:00+0800"


def test_breaker_holds_further_differences_without_any_request():
    """闸开着时剩下的差异全部零请求降级 —— 这就是「40 秒变 40 分钟」的解药。"""
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    ledger = DegradationLedger(period=PERIOD, breaker=breaker)
    gw, seen, _ = _gateway(_always(lambda: httpx.ConnectError("拒绝连接")), breaker=breaker, ledger=ledger)
    gw.chat_completion([{"role": "user", "content": "证据"}])
    assert breaker.state is BreakerState.OPEN
    assert len(seen) == 3

    for _ in range(20):
        gw2, seen2, _ = _gateway(_ok(), breaker=breaker, ledger=ledger)
        out = gw2.chat_completion([{"role": "user", "content": "证据"}])
        assert seen2 == [], "端点其实活着，但闸没到冷却期就不该打"
        assert out.error_code == "MODEL_CIRCUIT_OPEN"
    assert ledger.count == 21


def test_breaker_half_opens_after_cooldown_and_closes_on_probe_success():
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    ledger = DegradationLedger(period=PERIOD, breaker=breaker)

    gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("拒绝连接")), breaker=breaker, ledger=ledger)
    gw.chat_completion([{"role": "user", "content": "证据"}])
    assert breaker.state is BreakerState.OPEN
    assert breaker.remaining_seconds() == pytest.approx(60.0)

    clock.advance(59.0)
    assert breaker.allow() == (False, False), "冷却期未到仍不打"
    assert breaker.remaining_seconds() == pytest.approx(1.0)

    clock.advance(1.5)
    # 试探成功
    gw, seen, _ = _gateway(_ok(), breaker=breaker, ledger=ledger)
    result = gw.chat_completion([{"role": "user", "content": "证据"}])
    assert len(seen) == 1
    assert result.ok is True
    assert result.status == STATUS_OK
    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 0
    assert breaker.recovered_at_wall == "2026-09-07T09:00:00+0800"
    assert breaker.to_dict()["recovered_at"] == "2026-09-07T09:00:00+0800"


def test_breaker_half_open_lets_exactly_one_probe_through():
    """半开只放**一次**试探。放多次等于闸门半开着让整队挤进去。"""
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=10.0, clock=clock, wall_clock=clock.wall_clock)
    gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("x")), breaker=breaker)
    gw.chat_completion([{"role": "user", "content": "证据"}])
    clock.advance(11.0)
    assert breaker.allow() == (True, True)
    assert breaker.allow() == (False, False), "试探已在途，不放第二次"


def test_breaker_reopens_and_retimes_when_the_probe_fails():
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    ledger = DegradationLedger(period=PERIOD, breaker=breaker)
    gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("x")), breaker=breaker, ledger=ledger)
    gw.chat_completion([{"role": "user", "content": "证据"}])
    assert breaker.opened_at == 1000.0

    clock.advance(61.0)
    assert breaker.allow() == (True, True)
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.opened_at == 1061.0, "试探失败要**重新计时**，否则闸等于没开过"
    assert breaker.opened_count == 2
    assert breaker.allow() == (False, False)


def test_breaker_rejects_nonsense_configuration_with_named_error():
    with pytest.raises(ReconqError) as zero:
        CircuitBreaker(threshold=0)
    assert zero.value.code == "MODEL_CONFIG_INVALID"
    assert "至少为 1" in zero.value.detail

    with pytest.raises(ReconqError) as negative:
        CircuitBreaker(cooldown_seconds=-1.0)
    assert negative.value.code == "MODEL_CONFIG_INVALID"
    assert "不能为负" in negative.value.detail


def test_breaker_counts_one_failure_per_call_not_one_per_request():
    """熔断阈值衡量「多少条差异归因不出来」，不是「发了多少个请求」。

    按请求计的话，一条差异的 3 次重试就能把闸打开。
    """
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=60.0, clock=FakeClock())
    gw, seen, _ = _gateway(_always(lambda: httpx.ConnectError("x")), breaker=breaker)
    gw.chat_completion([{"role": "user", "content": "证据"}])
    assert len(seen) == 3
    assert breaker.consecutive_failures == 1
    assert breaker.state is BreakerState.CLOSED


# ───────────────────── 四、降级后状态集合不变（红线）─────────────────────


def _channel_only_bundle() -> EvidenceBundle:
    index = build_index([], period=PERIOD)
    outcome = align_channel(
        "wechat",
        [
            AlignRecord(
                channel="wechat",
                period=PERIOD,
                order_no="EX-9",
                amount=Decimal("66.00"),
                trade_time=datetime(2026, 9, 2, 10, 0, 0),
                source_file="wechat_bill.csv",
                source_row_no=3,
                ch_id=1,
            )
        ],
        index,
        period=PERIOD,
        window=period_window(PERIOD, TZ),
    ).outcomes[0]
    return build_evidence_bundle(
        outcome,
        contract=load_contract("contracts/wechat.yml", {"period": PERIOD}),
        window=period_window(PERIOD, TZ),
    )


def _seed_diff_item(conn: sqlite3.Connection) -> None:
    init_schema(conn)
    init_scan_schema(conn)
    conn.execute(
        f"INSERT INTO {DIFF_TABLE} (period, channel, check_path, check_name, message, diff_amount,"
        " source_file, first_row_no, disposition, needs_attribution)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            PERIOD,
            "wechat",
            "align.channel_only",
            "单边账",
            "渠道有、HIS 查无此笔",
            "66.00",
            "wechat_bill.csv",
            3,
            DISPOSITION_PENDING,
            1,
        ),
    )
    conn.commit()


def _snapshot(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [d.to_dict() for d in list_diff_items(conn, period=PERIOD)]


def test_degradation_paths_leave_every_disposition_untouched():
    """不可违反的红线：模型不可达时差异台账状态一律不推进。"""
    conn = connect(":memory:")
    _seed_diff_item(conn)
    before = _snapshot(conn)

    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    failing, _, _ = _gateway(_always(lambda: httpx.ConnectError("拒绝连接")), breaker=breaker)

    paths = [
        None,  # 未配端点
        failing,  # 端点不可达 + 重试用尽
        _gateway(_status(500))[0],  # 5xx
        _gateway(_status(401))[0],  # 4xx（不重试）
        _gateway(_parse_fail)[0],  # 响应结构不合约定
    ]
    for gw in paths:
        out = attribute(_channel_only_bundle(), gw)
        assert out.needs_manual is True, "降级时必须落回「待人工判定」"
        assert out.causes == ()

    conn.commit()
    after = _snapshot(conn)
    assert after == before
    assert after[0]["disposition"] == DISPOSITION_PENDING
    assert after[0]["disposition"] != DISPOSITION_CONFIRMED
    conn.close()


def test_degradation_module_cannot_advance_ledger_state_even_structurally():
    """降级模块在**结构上**够不到台账：它不持有连接、不 import 状态机。"""
    import reconq.gateway.degradation as degradation

    source = open(degradation.__file__, encoding="utf-8").read()
    for forbidden in ("import sqlite3", "from ..store", "from ..engine.period", "transition("):
        assert forbidden not in source, (
            f"degradation.py 出现了 {forbidden!r}：降级层必须只返回标记，"
            f"状态推进是人工点的活"
        )
    # 它能做的全部动作只有「返回一句中文标记」
    assert mark_pending_manual("MODEL_UNAVAILABLE").startswith("待人工判定（归因降级")
    assert "模型服务不可达" in mark_pending_manual("MODEL_UNAVAILABLE")


def test_attribution_reports_breaker_state_on_degraded_result():
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("拒绝连接")), breaker=breaker)
    attribute(_channel_only_bundle(), gw)
    assert breaker.state is BreakerState.OPEN

    gw2, seen, _ = _gateway(_ok(), breaker=breaker)
    out = attribute(_channel_only_bundle(), gw2)
    assert out.degraded is True
    assert out.error_code == "MODEL_CIRCUIT_OPEN"
    assert seen == [], "闸开着时归因层一个请求都不该发"
    assert "熔断" in out.error_detail


# ───────────────────── 五、降级事件入链 ─────────────────────


def test_degradation_events_land_in_the_event_chain(caplog):
    """降级要留痕，且**留的是时刻** —— 面板要显示「什么时候开始坏的」。"""
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    ledger = DegradationLedger(period=PERIOD, breaker=breaker)
    gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("拒绝连接")), breaker=breaker, ledger=ledger)

    with caplog.at_level(logging.INFO, logger="reconq"):
        gw.chat_completion([{"role": "user", "content": "证据"}])

    by_event = {}
    for record in caplog.records:
        by_event.setdefault(getattr(record, "event", ""), []).append(record)

    assert "model.breaker.open" in by_event, "开闸时刻必须留痕"
    opened = by_event["model.breaker.open"][0]
    assert opened.levelname == "WARNING"
    assert opened.event_fields["breaker_state"] == "open"
    assert opened.event_fields["opened_at"] == "2026-09-07T09:00:00+0800"
    assert opened.event_fields["cooldown_seconds"] == 60.0

    assert "model.degraded" in by_event
    degraded = by_event["model.degraded"][0]
    fields = degraded.event_fields
    assert fields["period"] == PERIOD
    assert fields["channel"] == "wechat"
    assert fields["code"] == "MODEL_UNAVAILABLE"
    assert fields["status"] == STATUS_DEGRADED
    assert fields["failure_kind"] == FailureKind.CONNECT.value
    assert fields["attempts"] == 3
    assert fields["at"] == "2026-09-07T09:00:00+0800"

    # 失败调用也留审计，且带上分类
    failed = by_event["model.call.failed"]
    assert failed and failed[-1].event_fields["failure_kind"] == "connect"
    assert failed[-1].event_fields["breaker_state"] == "open"


def test_breaker_half_open_and_recovery_events_land_in_the_chain(caplog):
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("x")), breaker=breaker)
    gw.chat_completion([{"role": "user", "content": "证据"}])
    clock.advance(61.0)

    good, _, _ = _gateway(_ok(), breaker=breaker)
    with caplog.at_level(logging.INFO, logger="reconq"):
        good.chat_completion([{"role": "user", "content": "证据"}])

    events = {getattr(r, "event", "") for r in caplog.records}
    assert "model.breaker.half_open" in events
    assert "model.breaker.closed" in events
    closed = [r for r in caplog.records if getattr(r, "event", "") == "model.breaker.closed"][0]
    assert closed.event_fields["recovered_at"] == "2026-09-07T09:00:00+0800"
    assert closed.event_fields["opened_count"] == 1


def test_degradation_ledger_reports_counts_moments_and_breaker_snapshot():
    clock = FakeClock()
    # 阈值取 4：三次连接失败不开闸，第 4 次（401）也还差一次。
    # **不能取 1** —— 那样第二条差异起就变成 MODEL_CIRCUIT_OPEN，
    # 测的就不是「原因分布」而是熔断本身（熔断由上面两条用例覆盖）。
    breaker = CircuitBreaker(threshold=4, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    ledger = DegradationLedger(period=PERIOD, breaker=breaker)
    for _ in range(3):
        gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("x")), breaker=breaker, ledger=ledger)
        gw.chat_completion([{"role": "user", "content": "证据"}])
    gw, _, _ = _gateway(_status(401), breaker=breaker, ledger=ledger)
    gw.chat_completion([{"role": "user", "content": "证据"}])

    payload = ledger.to_dict()
    assert payload["count"] == 4
    assert payload["by_reason"] == {"MODEL_UNAVAILABLE": 4}, "401 走鉴权分支，同样是 MODEL_UNAVAILABLE"
    assert payload["by_channel"] == {"wechat": 4}
    assert payload["first_at"] and payload["last_at"]
    assert payload["breaker_state"] == "open", "第 4 次失败恰好触达阈值 4"
    assert payload["breaker_opened_count"] == 1
    assert payload["breaker_opened_at"] == "2026-09-07T09:00:00+0800"
    assert payload["breaker_state_zh"]
    # 每条记录都带中文说明，面板不必自己翻译
    assert all(item["reason_zh"] for item in (r.to_dict() for r in ledger.records))
    assert ledger.records[0].to_dict()["kind_zh"] == "连接失败"


def test_run_summary_carries_degradation_count_and_breaker_moments():
    """摘要、面板、结论单读的是**同一份**降级数字。"""
    from reconq.run_summary import build_run_summary

    clock = FakeClock()
    # 阈值 2：两次失败恰好开闸，两次都是真实的 MODEL_UNAVAILABLE
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    ledger = DegradationLedger(period=PERIOD, breaker=breaker)
    for _ in range(2):
        gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("x")), breaker=breaker, ledger=ledger)
        gw.chat_completion([{"role": "user", "content": "证据"}])

    summary = build_run_summary(period=PERIOD, command="verify", channels=[], degradation=ledger)
    assert summary.degradation_count == 2
    assert summary.degradation_by_reason == {"MODEL_UNAVAILABLE": 2}
    assert summary.breaker_state == "open"
    assert summary.breaker_state_zh == "开启（直接降级，不打模型）"
    assert summary.breaker_opened_at == "2026-09-07T09:00:00+0800"
    assert summary.breaker_opened_count == 1
    assert "归因降级 2 条" in summary.render_zh()
    assert "待人工判定，台账状态不推进" in summary.render_zh()

    # 不传 degradation 就不带这些字段，**其余汇总项一字不变**
    plain = build_run_summary(period=PERIOD, command="verify", channels=[])
    assert plain.degradation_count == 0
    assert plain.breaker_state == ""
    assert "归因降级" not in plain.render_zh()


def test_run_summary_rejects_a_degradation_object_it_cannot_read():
    from reconq.run_summary import build_run_summary

    with pytest.raises(ReconqError) as excinfo:
        build_run_summary(period=PERIOD, command="verify", channels=[], degradation=object())
    assert excinfo.value.code == "RUN_SUMMARY_INVALID"
    assert "DegradationLedger" in excinfo.value.detail


def test_degradation_does_not_change_can_close_gate():
    """归因不可用**不等于**对账没做完：签字取决于差异是否被核对过。"""
    from reconq.run_summary import build_run_summary

    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60.0, clock=clock, wall_clock=clock.wall_clock)
    ledger = DegradationLedger(period=PERIOD, breaker=breaker)
    gw, _, _ = _gateway(_always(lambda: httpx.ConnectError("x")), breaker=breaker, ledger=ledger)
    gw.chat_completion([{"role": "user", "content": "证据"}])

    summary = build_run_summary(
        period=PERIOD, command="verify", channels=[], gate="green", degradation=ledger
    )
    assert summary.degradation_count == 1
    assert summary.can_close is True
    # 降级不进 warnings —— 混进去会被当成可忽略的提示刷过去
    assert summary.warnings == []


def test_gateway_without_breaker_still_degrades_and_never_raises():
    """没接熔断器时降级链照常工作 —— 熔断是加速器不是前提。"""
    ledger = DegradationLedger(period=PERIOD)
    gw, seen, _ = _gateway(_always(lambda: httpx.ConnectError("x")), ledger=ledger)
    result = gw.chat_completion([{"role": "user", "content": "证据"}])
    assert result.degraded is True
    assert result.breaker_state == ""
    assert ledger.count == 1
    assert len(seen) == 3


def test_retry_matrix_is_the_single_source_of_retryability():
    """网关的 ``retryable`` 真源是矩阵，不是每个 raise 点各写一遍。"""
    from reconq.gateway.model_gateway import GatewayError

    five_xx = GatewayError("MODEL_UNAVAILABLE", "x", phase=FailurePhase.HTTP, status_code=500)
    auth = GatewayError("MODEL_UNAVAILABLE", "x", phase=FailurePhase.HTTP, status_code=401)
    assert five_xx.retryable is rule_for(FailureKind.SERVER_ERROR).retryable is True
    assert auth.retryable is rule_for(FailureKind.BUSINESS).retryable is False
    assert five_xx.kind is FailureKind.SERVER_ERROR
    assert auth.kind is FailureKind.BUSINESS


def test_deterministic_two_call_replay_for_retry_accounting():
    """同一份失败两端跑，逐字段一致 —— 「重试了几次」这条数字要可复算。"""
    payloads = []
    for _ in range(2):
        gw, seen, slp = _gateway(_status(503))
        out = gw.chat_completion([{"role": "user", "content": "同一份证据"}])
        payloads.append(
            {
                "requests": len(seen),
                "sleeps": slp.calls,
                "attempts": out.call.attempts,
                "code": out.error_code,
                "kind": out.failure_kind,
                "digest": out.call.request_digest,
            }
        )
    assert payloads[0] == payloads[1]
    assert payloads[0]["requests"] == 3
    assert payloads[0]["sleeps"] == [0.8, 1.6]
    assert json.dumps(payloads[0], ensure_ascii=False)
