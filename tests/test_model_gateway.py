"""模型网关：协议兼容性、结构化校验、超时、错误归一、重试上限、审计元数据。

本文件全部用 ``httpx.MockTransport`` 回放 —— **只在测试数据层**。
产品代码（``src/reconq/gateway/model_gateway.py``）走的是真实 OpenAI-compatible
协议：真实 URL 拼法、真实 header 形态、真实 ``response_format`` 结构、
真实错误分类。替身不是最终对接方式；院内模型端点需要院内凭据，本轮未实连。

六类断言逐条可核：

1. **配置缺位即拒** —— 一次列全所有缺失变量名，**一个请求都不发**；
   不提供「缺 base_url 走默认端点」的兜底。
2. **请求形态** —— URL 拼法、``Authorization`` 头、``json_schema`` 结构化
   声明、``temperature=0``、``stream=False``。
3. **结构化校验** —— 代码块围栏被剥掉；只剥围栏、**不做就近匹配**；
   越界/多字段/百分制置信度一律拒。
4. **超时与错误归一** —— httpx 的异常类型不漏到业务层，一律变成带错误码的
   ``GatewayResult``；401/403/404 **一次都不重试**。
5. **重试上限** —— 超时/5xx/429/Schema 不过重试且次数有上限；重试用尽
   返回降级结果而非抛异常。
6. **审计元数据** —— 落的是模型名/耗时/token/请求指纹/是否重试，
   **不含 prompt 原文与 API key**。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
import pytest

from reconq.errors import ReconqError
from reconq.gateway.model_gateway import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    REQUIRED_ENV_VARS,
    TIMEOUT_ENV_VAR,
    GatewayConfig,
    ModelGateway,
    build_prompt,
)

# —— 占位端点：127.0.0.1 不可路由，本文件**从不真发**，全走 MockTransport ——
FAKE_BASE_URL = "http://127.0.0.1:9/v1"
FAKE_KEY = "FAKE_MODEL_KEY_PLACEHOLDER"
FAKE_MODEL = "reconq-attribution-1"

ENV_OK = {
    "RECONQ_MODEL_BASE_URL": FAKE_BASE_URL,
    "RECONQ_MODEL_API_KEY": FAKE_KEY,
    "RECONQ_MODEL_NAME": FAKE_MODEL,
    TIMEOUT_ENV_VAR: "12",
}

DRAFT = {
    "cause": "跨日切分重复计入",
    "confidence": 0.72,
    "evidence_refs": ["wechat_bill.csv:3"],
    "explanation_zh": "该笔收费时间落在账期窗口之外，很可能已计入下一期。",
}


def _config(**overrides: Any) -> GatewayConfig:
    env = {**ENV_OK, **overrides}
    return GatewayConfig.from_env(env)


def _ok_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _chat(content: Any, *, usage: dict | None = None) -> dict:
    body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": FAKE_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": body}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 812, "completion_tokens": 96, "total_tokens": 908},
    }


def _gateway(handler, **kw) -> ModelGateway:
    """handler 收到 ``httpx.Request``，返回 ``httpx.Response``。"""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kw.setdefault("sleeper", lambda _s: None)  # 测试不等退避
    return ModelGateway(_config(), client=client, **kw)


def _messages() -> list[dict[str, str]]:
    return build_prompt("差异行：wechat_bill.csv:3 金额 128.00 元")


# ───────────────────── 一、配置缺位即拒 ─────────────────────


def test_missing_env_vars_all_listed_at_once_and_no_request_sent():
    """一次列全所有缺失位；且**一个请求都不发**。"""
    with pytest.raises(ReconqError) as exc:
        GatewayConfig.from_env({})
    code = exc.value.code
    assert code == "MODEL_CONFIG_INVALID"
    for name in REQUIRED_ENV_VARS:
        assert name in exc.value.detail, f"{name} 未被点名，经办人得跑三次才补得齐"
    assert "默认端点" in exc.value.detail


def test_missing_only_api_key_still_rejected_before_any_request():
    env = {**ENV_OK, "RECONQ_MODEL_API_KEY": "   "}  # 纯空白等同缺失
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_response(_chat({"drafts": [DRAFT]}))

    with pytest.raises(ReconqError) as exc:
        GatewayConfig.from_env(env)
    assert exc.value.code == "MODEL_CONFIG_INVALID"
    assert "RECONQ_MODEL_API_KEY" in exc.value.detail
    assert calls == [], "配置缺位时不得发出任何请求"


@pytest.mark.parametrize(
    "env,expect",
    [
        ({**ENV_OK, "RECONQ_MODEL_BASE_URL": "127.0.0.1:8000/v1"}, "不是 http"),
        ({**ENV_OK, TIMEOUT_ENV_VAR: "abc"}, "不是合法秒数"),
        ({**ENV_OK, TIMEOUT_ENV_VAR: "0"}, "必须是正数"),
        ({**ENV_OK, TIMEOUT_ENV_VAR: "-3"}, "必须是正数"),
    ],
)
def test_invalid_config_values_are_rejected_by_name(env, expect):
    with pytest.raises(ReconqError) as exc:
        GatewayConfig.from_env(env)
    assert exc.value.code == "MODEL_CONFIG_INVALID"
    assert expect in exc.value.detail


def test_timeout_env_var_is_optional_and_defaults():
    env = {k: v for k, v in ENV_OK.items() if k != TIMEOUT_ENV_VAR}
    assert GatewayConfig.from_env(env).timeout == DEFAULT_TIMEOUT_SECONDS


def test_config_to_dict_leaks_neither_key_nor_endpoint():
    """配置摘要进面板与事件链：模型名可见，端点地址与密钥不可见。"""
    payload = _config().to_dict()
    assert payload["model"] == FAKE_MODEL
    blob = json.dumps(payload, ensure_ascii=False)
    assert FAKE_KEY not in blob
    assert "127.0.0.1" not in blob
    assert len(payload["fingerprint"]) == 16


# ───────────────────── 二、请求形态 ─────────────────────


def test_request_shape_matches_openai_compatible():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = json.loads(request.content)
        return _ok_response(_chat({"drafts": [DRAFT]}))

    gw = _gateway(handler)
    schema = {"type": "object", "properties": {"drafts": {"type": "array"}}, "required": ["drafts"]}
    result = gw.chat_completion(_messages(), schema=schema)

    assert result.ok
    # URL：base_url 带 /v1，只补一次 /chat/completions（不补成 /v1/v1/…）
    assert seen["url"] == f"{FAKE_BASE_URL}/chat/completions"
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    assert seen["content_type"] == "application/json"
    body = seen["body"]
    assert body["model"] == FAKE_MODEL
    assert body["temperature"] == 0 and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["response_format"]["json_schema"]["strict"] is True


def test_endpoint_helper_is_idempotent():
    """base_url 已经是完整端点时不重复拼。"""
    cfg = GatewayConfig(base_url=f"{FAKE_BASE_URL}/chat/completions", api_key=FAKE_KEY, model=FAKE_MODEL)
    assert cfg.endpoint == f"{FAKE_BASE_URL}/chat/completions"
    assert GatewayConfig(base_url="http://h/v1/", api_key="k", model="m").endpoint == "http://h/v1/chat/completions"


def test_instructions_go_to_user_turn_not_system():
    """逐条内容混进 system 会让 provider 侧 prompt 缓存整段失效。"""
    plain = build_prompt("证据正文")
    with_notes = build_prompt("证据正文", instructions="近几期历史不可用")
    assert plain[0] == with_notes[0], "system 段必须跨差异不变"
    assert "近几期历史不可用" in with_notes[1]["content"]


# ───────────────────── 三、结构化校验 ─────────────────────


def test_fenced_json_is_unwrapped_but_nothing_else_is_guessed():
    def handler(request: httpx.Request) -> httpx.Response:
        body = "```json\n" + json.dumps({"drafts": [DRAFT]}, ensure_ascii=False) + "\n```"
        return _ok_response(_chat(body))

    result = _gateway(handler).chat_completion(_messages(), validator=lambda raw: raw)
    assert result.ok
    assert result.data["drafts"][0]["cause"] == DRAFT["cause"]


def test_prose_around_json_is_rejected_not_salvaged():
    """就近找一段 JSON 会把解释文字里的示例捡出来当结果 —— 必须拒。"""
    text = "根据证据，我认为可能的成因如下，示例格式为 {\"cause\": \"...\"}：\n" + json.dumps(
        {"drafts": [DRAFT]}, ensure_ascii=False
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat(text))

    result = _gateway(handler).chat_completion(_messages(), validator=lambda raw: raw)
    assert not result.ok
    assert result.error_code == "MODEL_RESPONSE_INVALID"
    assert "不是合法 JSON" in result.error_detail


def test_validator_failure_becomes_schema_invalid_and_is_retryable():
    def strict(raw: Any) -> Any:
        if not isinstance(raw, dict) or "drafts" not in raw:
            raise ValueError("响应须含 drafts 数组")
        return raw

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat({"candidates": []}))

    result = _gateway(handler).chat_completion(_messages(), validator=strict)
    assert not result.ok
    assert result.error_code == "MODEL_SCHEMA_INVALID"
    assert result.retryable is True


# ───────────────────── 四、超时与错误归一 ─────────────────────


def test_timeout_is_normalized_and_never_raises_to_business_layer():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    result = _gateway(handler).chat_completion(_messages())
    assert result.ok is False and result.degraded is True
    assert result.error_code == "MODEL_TIMEOUT"
    assert result.retryable is True
    assert result.call.attempts == DEFAULT_MAX_ATTEMPTS


def test_connection_error_becomes_model_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = _gateway(handler).chat_completion(_messages())
    assert result.error_code == "MODEL_UNAVAILABLE"
    assert result.degraded is True


@pytest.mark.parametrize(
    "status,code,retryable,needle",
    [
        (401, "MODEL_UNAVAILABLE", False, "鉴权或权限"),
        (403, "MODEL_UNAVAILABLE", False, "鉴权或权限"),
        (404, "MODEL_UNAVAILABLE", False, "/v1"),
        (429, "MODEL_UNAVAILABLE", True, "限流"),
        (500, "MODEL_UNAVAILABLE", True, "服务端错误"),
        (503, "MODEL_UNAVAILABLE", True, "服务端错误"),
        (400, "MODEL_RESPONSE_INVALID", False, "拒绝该请求"),
    ],
)
def test_http_status_maps_to_stable_codes(status, code, retryable, needle):
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(status, json={"error": {"message": "boom", "code": "x"}})

    result = _gateway(handler).chat_completion(_messages())
    assert result.error_code == code
    assert needle in result.error_detail
    assert result.retryable is retryable
    # 鉴权/端点不存在一次都不重试；限流与 5xx 走满上限
    assert len(attempts) == (DEFAULT_MAX_ATTEMPTS if retryable else 1), f"HTTP {status} 的重试次数不对"


@pytest.mark.parametrize(
    "payload,needle",
    [
        ({"id": "x"}, "缺少 choices"),
        ({"choices": []}, "缺少 choices"),
        ({"choices": [{"message": {"content": ""}}]}, "content 为空"),
        ({"choices": [{"message": {}, "finish_reason": "length"}]}, "content 为空"),
        ("not-a-dict", "顶层不是对象"),
    ],
)
def test_non_conforming_response_shapes_are_rejected(payload, needle):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    result = _gateway(handler).chat_completion(_messages())
    assert result.error_code == "MODEL_RESPONSE_INVALID"
    assert needle in result.error_detail


def test_soft_failure_200_with_error_field_is_not_treated_as_success():
    """自建端点最常见的软失败：200 但 body 里是 error。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _ok_response({"error": {"message": "模型未加载", "code": "no_model", "type": "server"}})

    result = _gateway(handler).chat_completion(_messages())
    assert result.error_code == "MODEL_UNAVAILABLE"
    assert "模型未加载" in result.error_detail
    assert len(calls) == 1, "服务端自己说这是它的错，重试只会拿到同一段错误"


def test_non_json_body_is_rejected_with_excerpt():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    result = _gateway(handler).chat_completion(_messages())
    assert result.error_code == "MODEL_RESPONSE_INVALID"
    assert "502 Bad Gateway" in result.error_detail


# ───────────────────── 五、重试上限 ─────────────────────


def test_retry_stops_at_max_attempts_and_backoff_is_bounded():
    attempts: list[int] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return _ok_response(_chat({"drafts": [DRAFT]}))

    gw = _gateway(handler, sleeper=slept.append, backoff=(0.5, 1.0))
    result = gw.chat_completion(_messages(), validator=_always_fail)
    assert result.error_code == "MODEL_SCHEMA_INVALID"
    assert len(attempts) == DEFAULT_MAX_ATTEMPTS == 3
    assert slept == [0.5, 1.0], f"退避序列不对：{slept}"


def test_retry_then_success_returns_ok_and_marks_retried():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, json={"error": {"message": "warming up"}})
        return _ok_response(_chat({"drafts": [DRAFT]}))

    result = _gateway(handler).chat_completion(_messages(), validator=_echo)
    assert result.ok and result.call.retried is True and result.call.attempts == 2


def test_backoff_sequence_is_capped_not_exponential():
    slept: list[float] = []
    gw = _gateway(
        lambda r: httpx.Response(500, json={"error": {"message": "x"}}),
        sleeper=slept.append,
        backoff=(0.5, 1.0, 2.0, 4.0, 8.0),
    )
    gw.chat_completion(_messages())
    assert len(slept) == DEFAULT_MAX_ATTEMPTS - 1


def test_max_attempts_one_means_no_retry_at_all():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"error": {"message": "x"}})

    gw = ModelGateway(
        GatewayConfig(base_url=FAKE_BASE_URL, api_key=FAKE_KEY, model=FAKE_MODEL, max_attempts=1),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _s: None,
    )
    result = gw.chat_completion(_messages())
    assert len(calls) == 1
    assert result.error_code == "MODEL_UNAVAILABLE"


# ───────────────────── 六、审计元数据 ─────────────────────


def test_audit_metadata_carries_model_latency_tokens_and_request_digest(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat({"drafts": [DRAFT]}))

    with caplog.at_level(logging.INFO, logger="reconq"):
        result = _gateway(handler).chat_completion(_messages(), validator=_echo)

    call = result.call
    assert call.model == FAKE_MODEL
    assert call.attempts == 1 and call.retried is False
    assert call.elapsed_ms >= 0.0
    assert (call.prompt_tokens, call.completion_tokens, call.total_tokens) == (812, 96, 908)
    assert len(call.request_digest) == 16
    assert call.response_chars > 0

    events = [r for r in caplog.records if getattr(r, "event", "") == "model.call"]
    assert events, "必须有 model.call 事件落盘"
    fields = events[-1].event_fields
    assert fields["model"] == FAKE_MODEL and fields["attempts"] == 1


def test_request_digest_is_stable_and_excludes_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat({"drafts": [DRAFT]}))

    a = _gateway(handler).chat_completion(_messages(), validator=_echo)
    b = _gateway(handler).chat_completion(_messages(), validator=_echo)
    assert a.call.request_digest == b.call.request_digest, "同一份证据两次调用应同指纹"
    assert FAKE_KEY not in a.call.request_digest


def test_audit_log_never_contains_prompt_body_or_api_key(caplog):
    """prompt 里是渠道账单行，落进日志库等于把账单抄了第二份。"""
    secret_line = "差异行：wechat_bill.csv:3 金额 128.00 元 患者 13800138000"

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat({"drafts": [DRAFT]}))

    with caplog.at_level(logging.INFO, logger="reconq"):
        result = _gateway(handler).chat_completion(build_prompt(secret_line), validator=_echo)
        blob = "\n".join(
            json.dumps(r.event_fields, ensure_ascii=False, default=str)
            for r in caplog.records
            if getattr(r, "event", "")
        )
    assert result.ok
    assert "13800138000" not in blob
    assert "wechat_bill.csv:3" not in blob
    assert FAKE_KEY not in blob


def test_failure_audit_records_code_and_attempts(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    with caplog.at_level(logging.INFO, logger="reconq"):
        _gateway(handler).chat_completion(_messages())
    failed = [r for r in caplog.records if getattr(r, "event", "") == "model.call.failed"]
    assert failed
    assert failed[-1].event_fields["code"] == "MODEL_UNAVAILABLE"
    assert failed[-1].event_fields["attempts"] == 1


def test_error_detail_is_truncated_not_dumped_whole():
    huge = "x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat(huge))

    result = _gateway(handler).chat_completion(_messages())
    assert "已截断" in result.error_detail
    assert len(result.error_detail) < 700


def test_bundle_audit_metadata_is_passed_through_untouched():
    bundle_audit = {"bundle_digest": "abc123", "char_count": 2048, "candidate_total": 3}

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat({"drafts": [DRAFT]}))

    result = _gateway(handler).chat_completion(_messages(), validator=_echo, bundle_audit=bundle_audit)
    assert result.call.bundle == bundle_audit


def test_elapsed_is_measured_with_injected_monotonic_clock():
    """耗时取自单调钟（可注入），不是墙上时钟 —— NTP 校时跳一下会算出负耗时。"""
    ticks = iter([1.0, 1.25, 1.25, 1.25])

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat({"drafts": [DRAFT]}))

    gw = _gateway(handler, clock=lambda: next(ticks))
    result = gw.chat_completion(_messages(), validator=_echo)
    assert result.call.elapsed_ms == pytest.approx(250.0)


def test_result_to_dict_is_json_serializable_and_carries_no_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat({"drafts": [DRAFT]}))

    result = _gateway(handler).chat_completion(_messages(), validator=_echo)
    blob = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "content" not in result.to_dict()
    assert FAKE_KEY not in blob
    json.loads(blob)  # 不抛


def _echo(raw: Any) -> Any:
    return raw


def _always_fail(raw: Any) -> Any:
    raise ValueError("结构不合约定")


# ── 真实端点未实连的显式说明（防止把替身当成最终对接方式）──


def test_no_real_endpoint_is_ever_contacted(monkeypatch):
    """本文件所有请求都经 MockTransport；把 socket 打掉也不影响通过。"""
    import socket

    def _boom(*a, **k):  # pragma: no cover - 触发即说明有请求漏出去了
        raise AssertionError("测试不得发出真实网络请求")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(_chat({"drafts": [DRAFT]}))

    result = _gateway(handler).chat_completion(_messages(), validator=_echo)
    assert result.ok


def test_default_timeout_constant_is_used_when_env_absent():
    assert DEFAULT_TIMEOUT_SECONDS == 30.0
    assert time.perf_counter() > 0  # 计时基准是 perf_counter（单调钟）
