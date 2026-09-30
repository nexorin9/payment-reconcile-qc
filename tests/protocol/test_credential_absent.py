"""凭据位缺失时，客户端必须在**发出任何请求之前**就失败（Task 13）。

这是协议层最省时间的一条纪律。渠道对缺凭据的回应毫无指向性：微信一律回
``SIGN_ERROR``，支付宝一律回 ``isv.invalid-signature``，**都不告诉你缺的是
serial_no、公钥还是 APIv3 密钥**。靠渠道报错排查，要「补一个跑一次、又报
下一个」，四轮下来才跑通一次请求。

本文件逐个清空凭据位，断言：抛 ``CREDENTIAL_MISSING``、错误详情**点名具体
变量名**、且**一次请求都没发出**（用会记账的 MockTransport 计数）。

⚠️ 不连接任何真实环境；全部凭据值为占位。
"""

from __future__ import annotations

import httpx
import pytest
from tests.protocol.conftest import BILL_DATE, no_sleep

from reconq.errors import ReconqError
from reconq.sources.alipay_bill import fetch_bill as fetch_alipay
from reconq.sources.signing import ALIPAY_REQUIRED_VARS, WECHAT_REQUIRED_VARS
from reconq.sources.wechat_bill import fetch_bill as fetch_wechat


class _CountingTransport(httpx.MockTransport):
    """记录请求数的 transport：任何一次调用都会让计数 > 0。"""

    def __init__(self):  # type: ignore[no-untyped-def]
        self.calls = 0
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover
        self.calls += 1
        return httpx.Response(200, json={})


def _wechat_env(base: dict[str, str], **overrides: str) -> dict[str, str]:
    env = {**base, **overrides}
    for name, value in overrides.items():
        if value == "":
            env.pop(name, None)
    return env


def _alipay_env(base: dict[str, str], **overrides: str) -> dict[str, str]:
    env = {**base, **overrides}
    for name, value in overrides.items():
        if value == "":
            env.pop(name, None)
    return env


@pytest.mark.parametrize("missing", WECHAT_REQUIRED_VARS)
def test_each_missing_wechat_credential_is_named(wechat_env, missing):
    """逐个清空微信四个凭据位：报错点名变量名，且一个请求都不发。"""
    transport = _CountingTransport()
    env = _wechat_env(wechat_env, **{missing: ""})
    with pytest.raises(ReconqError) as exc:
        fetch_wechat(
            BILL_DATE, client=httpx.Client(transport=transport), env=env, sleep=no_sleep
        )
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert missing in exc.value.detail, f"错误详情必须点名 {missing}，实得：{exc.value.detail}"
    assert transport.calls == 0, f"缺 {missing} 时不得发出任何请求"


@pytest.mark.parametrize("missing", ALIPAY_REQUIRED_VARS)
def test_each_missing_alipay_credential_is_named(alipay_env, missing):
    """逐个清空支付宝三个凭据位：报错点名变量名，且一个请求都不发。"""
    transport = _CountingTransport()
    env = _alipay_env(alipay_env, **{missing: ""})
    with pytest.raises(ReconqError) as exc:
        fetch_alipay(
            BILL_DATE, client=httpx.Client(transport=transport), env=env, sleep=no_sleep
        )
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert missing in exc.value.detail, f"错误详情必须点名 {missing}，实得：{exc.value.detail}"
    assert transport.calls == 0


def test_all_missing_wechat_credentials_listed_at_once(wechat_env):
    """四个位全缺时**一次列全**，不让人「补一个跑一次」。

    逐个报错是这里最常见也最费时间的失败模式：四轮往返才跑通一次请求。
    """
    transport = _CountingTransport()
    with pytest.raises(ReconqError) as exc:
        fetch_wechat(BILL_DATE, client=httpx.Client(transport=transport), env={}, sleep=no_sleep)
    for name in WECHAT_REQUIRED_VARS:
        assert name in exc.value.detail
    assert transport.calls == 0


def test_all_missing_alipay_credentials_listed_at_once(alipay_env):
    """支付宝三个位全缺时同样一次列全。"""
    transport = _CountingTransport()
    with pytest.raises(ReconqError) as exc:
        fetch_alipay(BILL_DATE, client=httpx.Client(transport=transport), env={}, sleep=no_sleep)
    for name in ALIPAY_REQUIRED_VARS:
        assert name in exc.value.detail
    assert transport.calls == 0


def test_blank_string_counts_as_missing(wechat_env):
    """纯空白值等同于缺失——复制密钥时带上的换行与空格是高频来源。"""
    transport = _CountingTransport()
    env = _wechat_env(wechat_env, WECHAT_API_V3_KEY="   ")
    with pytest.raises(ReconqError) as exc:
        fetch_wechat(
            BILL_DATE, client=httpx.Client(transport=transport), env=env, sleep=no_sleep
        )
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "WECHAT_API_V3_KEY" in exc.value.detail
    assert transport.calls == 0


def test_missing_platform_cert_blocks_wechat(wechat_env):
    """缺平台证书时也拒——没有它就验不了签，验不了签就分不清渠道的真假响应。

    本产品**不提供**「只签名不验签」模式：那种模式下拿到的是被中间设备
    改过的 download_url，本产品却当真的用。
    """
    transport = _CountingTransport()
    env = _wechat_env(wechat_env, WECHAT_PLATFORM_CERT_PATH="")
    with pytest.raises(ReconqError) as exc:
        fetch_wechat(
            BILL_DATE, client=httpx.Client(transport=transport), env=env, sleep=no_sleep
        )
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "WECHAT_PLATFORM_CERT_PATH" in exc.value.detail
    assert "平台证书" in exc.value.detail
    assert transport.calls == 0


def test_missing_key_file_names_env_var_and_path(wechat_env):
    """密钥文件路径不存在时，报错点名**环境变量名**与**实际路径**。"""
    transport = _CountingTransport()
    env = _wechat_env(wechat_env, WECHAT_PRIVATE_KEY_PATH="/nonexistent/path/to/apiclient_key.pem")
    with pytest.raises(ReconqError) as exc:
        fetch_wechat(
            BILL_DATE, client=httpx.Client(transport=transport), env=env, sleep=no_sleep
        )
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "WECHAT_PRIVATE_KEY_PATH" in exc.value.detail
    assert "/nonexistent/path/to/apiclient_key.pem" in exc.value.detail
    assert transport.calls == 0


def test_credentials_never_appear_in_error_detail(wechat_env):
    """错误详情里不得出现凭据**值**——只出现变量名。

    凭据一旦进了 detail，就可能随面板、结论单、事件链一起进日志库。
    """
    transport = _CountingTransport()
    env = _wechat_env(wechat_env, WECHAT_MCH_ID="")
    with pytest.raises(ReconqError) as exc:
        fetch_wechat(
            BILL_DATE, client=httpx.Client(transport=transport), env=env, sleep=no_sleep
        )
    for secret_value in (wechat_env["WECHAT_API_V3_KEY"], wechat_env["WECHAT_SERIAL_NO"]):
        assert secret_value not in exc.value.detail
