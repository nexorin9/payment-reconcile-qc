"""微信支付 APIv3 账单取件的协议兼容性测试（Task 13）。

覆盖五类：请求 URL/query/Authorization 头形态、成功、账单不存在、
账单生成中、限频、验签失败，外加「非 JSON 信封不得当成空账单」。

报文样例在 ``tests/fixtures/protocol/wechat/``（测试数据层），
产品代码走真实协议。**替身不是最终对接方式** —— 院内真实对接是直连
``https://api.mch.weixin.qq.com/v3/bill/tradebill``，凭据从环境变量读。
本轮**未实连**真实端点（无院内商户凭据）。

本文件不含任何真实商户号、私钥、平台证书或 APIv3 密钥。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json

import httpx
import pytest
from tests.protocol.conftest import (
    BILL_DATE,
    FAKE_API_V3_KEY,
    FAKE_NONCE,
    FAKE_PLATFORM_SERIAL,
    FAKE_SERIAL_NO,
    FAKE_TIMESTAMP,
    http_status_of,
    load_message,
    load_raw,
    no_sleep,
)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from reconq.errors import ReconqError
from reconq.sources.retry import backoff_delays
from reconq.sources.signing import wechat_sign_message
from reconq.sources.wechat_bill import (
    BILL_TYPES,
    DOWNLOAD_LINK_TTL_SECONDS,
    WECHAT_API_BASE,
    fetch_bill,
)

TAKE_PATH = "/v3/bill/tradebill"

#: 一份脱敏微信交易明细（带抬头与尾部，账单真实形态）
BILL_CSV = "\n".join(
    [
        "微信支付账单明细列表",
        "商户号：EXAM_WX_MCH_ID    账单日期：2026-09-07",
        "",
        "交易时间,公众账号ID,商户订单号,微信交易单号,交易类型,交易状态,应结订单金额,退款金额",
        "2026-09-07 09:12:03,EXAM_WX_MCH_ID,EX-WX-0001,EX-WXTXN-0001,JSAPI,SUCCESS,128.00,0.00",
        "2026-09-07 09:31:44,EXAM_WX_MCH_ID,EX-WX-0002,EX-WXTXN-0002,NATIVE,SUCCESS,1020.50,0.00",
    ]
)


def _encrypt(plain: bytes, key: str = FAKE_API_V3_KEY) -> bytes:
    """按微信口径封装：nonce(12B) || ciphertext || tag(16B)。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = b"\x01" * 12
    return nonce + AESGCM(key.encode()).encrypt(nonce, plain, None)


def _blob() -> bytes:
    return _encrypt(gzip.compress(BILL_CSV.encode("utf-8")))


def _sign_response(
    resp: httpx.Response, key_path: str, *, sign_body: bytes | None = None
) -> httpx.Response:
    """给 200 响应补上三个 Wechatpay-* 头（模拟渠道侧签名）。

    ``sign_body`` 用于**故意签名错内容**的样本：头部长得完全正常，
    验签必然失败——这正是要防的场景。
    """
    if resp.status_code != 200:
        return resp
    key = serialization.load_pem_private_key(open(key_path, "rb").read(), password=None)
    ts, nonce = str(FAKE_TIMESTAMP), FAKE_NONCE
    body = resp.content if sign_body is None else sign_body
    message = f"{ts}\n{nonce}\n".encode() + body + b"\n"
    sig = base64.b64encode(
        key.sign(hashlib.sha256(message).digest(), padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    resp.headers["Wechatpay-Signature"] = sig
    resp.headers["Wechatpay-Timestamp"] = ts
    resp.headers["Wechatpay-Nonce"] = nonce
    resp.headers["Wechatpay-Serial"] = FAKE_PLATFORM_SERIAL
    return resp


def _transport(
    *,
    take: httpx.Response,
    key_path: str,
    blob: bytes | None = None,
    sign_body: bytes | None = None,
) -> tuple[httpx.MockTransport, dict]:
    """回放「取件 → 下载」两段链路，并记录真实发出的请求供断言。"""
    payload = blob if blob is not None else _blob()
    state: dict = {"take_calls": 0, "download_calls": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if TAKE_PATH in str(request.url):
            state["take_calls"] += 1
            return _sign_response(take, key_path, sign_body=sign_body)
        state["download_calls"] += 1
        return httpx.Response(200, content=payload)

    return httpx.MockTransport(handler), state


# —— 请求形态：URL、query、Authorization 头 ————————————————————


def test_request_url_path_and_query_shape(wechat_env, key_paths):
    """取件请求必须打到真实官方路径，query 三个参数齐全且顺序稳定。

    少一个 query 参数渠道会回笼统的 ``INVALID_REQUEST``，不发请求
    断言就永远发现不了自己漏了参数。
    """
    blob = _blob()
    take = httpx.Response(
        200,
        json={
            "hash_type": "SHA1",
            "hash": hashlib.sha1(blob).hexdigest(),
            "download_url": f"{WECHAT_API_BASE}/v3/bill/download/EXAM",
        },
    )
    transport, state = _transport(take=take, key_path=key_paths["wechat_private"], blob=blob)
    fetch_bill(
        BILL_DATE,
        client=httpx.Client(transport=transport),
        env=wechat_env,
        sleep=no_sleep,
    )

    req = state["requests"][0]
    assert req.method == "GET"
    assert str(req.url).startswith(f"{WECHAT_API_BASE}{TAKE_PATH}?")
    assert dict(req.url.params) == {
        "bill_date": BILL_DATE,
        "bill_type": "ALL",
        "tar_type": "GZIP",
    }


def test_authorization_header_has_five_elements(wechat_env, key_paths):
    """Authorization 头五要素齐全；``Wechatpay-Serial`` 头带**商户**证书序列号。

    两个 ``serial`` 是同名不同物，写反了对方一律回笼统的 ``SIGN_ERROR``：
    请求头 ``Wechatpay-Serial`` 是**商户证书**序列号（让平台知道用哪把
    公钥验你的请求）；**响应**头里的同名头才是平台证书序列号（决定你该用
    哪张平台证书验它的响应，见 :func:`verify_wechat_response`）。
    """
    blob = _blob()
    take = httpx.Response(
        200,
        json={
            "hash_type": "SHA1",
            "hash": hashlib.sha1(blob).hexdigest(),
            "download_url": f"{WECHAT_API_BASE}/v3/bill/download/EXAM",
        },
    )
    transport, state = _transport(take=take, key_path=key_paths["wechat_private"], blob=blob)
    fetch_bill(
        BILL_DATE, client=httpx.Client(transport=transport), env=wechat_env, sleep=no_sleep
    )

    headers = state["requests"][0].headers
    auth = headers["authorization"]
    assert auth.startswith("WECHATPAY2-SHA256-RSA2048 ")
    for element in ("mchid=", "nonce_str=", "timestamp=", "serial_no=", "signature="):
        assert element in auth, f"Authorization 缺要素 {element}"
    # 请求头带商户证书序列号（与 Authorization 里的 serial_no 同值）
    assert headers["wechatpay-serial"] == FAKE_SERIAL_NO
    # 响应验签用的是平台证书序列号，与请求头那个不是同一串
    assert FAKE_PLATFORM_SERIAL != FAKE_SERIAL_NO


def test_signed_url_path_includes_query():
    """签名串里的 URL 必须**含 query**。

    漏掉 query 是另一个高频踩坑点：签名能生成，渠道一律拒。
    """
    message = wechat_sign_message(
        method="GET",
        url_path=f"{TAKE_PATH}?bill_date={BILL_DATE}&bill_type=ALL&tar_type=GZIP",
        body="",
        timestamp=FAKE_TIMESTAMP,
        nonce=FAKE_NONCE,
    )
    assert "bill_date=2026-09-07" in message
    assert message.startswith("GET\n")
    assert message.endswith("\n")  # body 为空串时第五行仍占一行


# —— 五类响应 → 错误码映射 ——————————————————————————————————


def test_bill_not_exist_maps_and_does_not_retry(wechat_env, key_paths):
    """账单不存在：映射为 ``BILL_NOT_EXIST`` 且**一次都不重试**。"""
    transport, state = _transport(
        take=httpx.Response(http_status_of("wechat/take_bill_not_exist.json"),
                            json=load_message("wechat/take_bill_not_exist.json")),
        key_path=key_paths["wechat_private"],
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(
            BILL_DATE, client=httpx.Client(transport=transport), env=wechat_env, sleep=no_sleep
        )
    assert exc.value.code == "BILL_NOT_EXIST"
    assert state["take_calls"] == 1, "BILL_NOT_EXIST 不可重试：退避到天荒也拿不到账单"
    assert state["download_calls"] == 0


def test_statement_creating_is_retryable_then_exhausts(wechat_env, key_paths):
    """账单生成中：可重试，**退避次数不超过上限**，用尽抛 ``RATE_LIMITED_EXHAUSTED``。

    真实成因挂在 ``cause_code`` 上——处置结果是「重试用尽」，
    面板要显示的是「账单还在生成」，不能被前者盖住。
    """
    transport, state = _transport(
        take=httpx.Response(http_status_of("wechat/take_statement_creating.json"),
                            json=load_message("wechat/take_statement_creating.json")),
        key_path=key_paths["wechat_private"],
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(
            BILL_DATE, client=httpx.Client(transport=transport), env=wechat_env, sleep=no_sleep
        )
    assert exc.value.code == "RATE_LIMITED_EXHAUSTED"
    assert exc.value.cause_code == "STATEMENT_CREATING"
    assert state["take_calls"] == len(backoff_delays(3)) + 1 == 3


def test_frequency_limited_triggers_exponential_backoff(wechat_env, key_paths):
    """限频：触发指数退避，延迟序列 0.5→1.0，重试次数不超上限。"""
    slept: list[float] = []
    transport, state = _transport(
        take=httpx.Response(http_status_of("wechat/take_frequency_limited.json"),
                            json=load_message("wechat/take_frequency_limited.json")),
        key_path=key_paths["wechat_private"],
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(
            BILL_DATE,
            client=httpx.Client(transport=transport),
            env=wechat_env,
            sleep=slept.append,
        )
    assert exc.value.code == "RATE_LIMITED_EXHAUSTED"
    assert exc.value.cause_code == "FREQUENCY_LIMITED"
    assert slept == backoff_delays(3) == [0.5, 1.0]
    assert state["take_calls"] == 3, "重试次数必须有上限，不硬重试拖住整次扫描"


def test_sign_error_maps_to_credential_missing(wechat_env, key_paths):
    """渠道回 ``SIGN_ERROR``：映射为 ``CREDENTIAL_MISSING`` 且不重试。

    这也说明了为什么产品坚持**缺位前置校验**——渠道只回笼统的
    ``SIGN_ERROR``，不告诉你缺的是 serial_no 还是私钥不匹配。
    """
    transport, state = _transport(
        take=httpx.Response(http_status_of("wechat/take_sign_error.json"),
                            json=load_message("wechat/take_sign_error.json")),
        key_path=key_paths["wechat_private"],
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(
            BILL_DATE, client=httpx.Client(transport=transport), env=wechat_env, sleep=no_sleep
        )
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert state["take_calls"] == 1


def test_signature_invalid_rejects_response_and_lands_no_rows(wechat_env, key_paths):
    """**报文完全正常但验签失败**：拒绝使用该响应的 download_url，不落任何账单行。

    这是本套件里最容易漏的一类——HTTP 200、字段齐全、download_url 可用，
    只按 200 放行的实现会照常把账单入库，而那是被中间设备改过的内容。
    """
    sample = load_message("wechat/take_signature_invalid.json")
    take = httpx.Response(200, json={k: v for k, v in sample.items() if not k.startswith("_")})
    # 头部长得完全正常，但签名是对**别的内容**算的
    transport, state = _transport(
        take=take,
        key_path=key_paths["wechat_private"],
        sign_body=b'{"hash_type":"SHA1","hash":"tampered"}',
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(
            BILL_DATE, client=httpx.Client(transport=transport), env=wechat_env, sleep=no_sleep
        )
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"
    assert "拒绝使用该响应取账单" in exc.value.detail
    assert state["download_calls"] == 0, "验签不过连 download_url 都不许发请求"


def test_non_json_envelope_is_not_treated_as_empty_bill(wechat_env, key_paths):
    """非 JSON 信封（V2 XML）必须显式失败，**不得**当成「今天没账单」。

    把它当空账单处理的后果是这一路合计静默归零，结论单却照样显示为绿。
    """
    xml_body = load_raw("wechat/take_legacy_xml_error.xml")
    transport, state = _transport(
        take=httpx.Response(200, content=xml_body.encode("utf-8"),
                           headers={"Content-Type": "application/xml"}),
        key_path=key_paths["wechat_private"],
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(
            BILL_DATE, client=httpx.Client(transport=transport), env=wechat_env, sleep=no_sleep
        )
    assert exc.value.code == "CHANNEL_REQUEST_FAILED"
    assert state["download_calls"] == 0


def test_bill_types_are_the_three_official_values():
    """``bill_type`` 只接受官方三值；只取 SUCCESS 会漏退款行——单边账最大来源。"""
    assert BILL_TYPES == ("ALL", "SUCCESS", "REFUND")
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, bill_type="ALLSUCCESS", env={})
    assert exc.value.code == "BILL_DATE_INVALID"


def test_download_link_ttl_is_five_minutes():
    """download_url 5 分钟有效——常量与 link_window 的窗口口径必须一致。"""
    assert DOWNLOAD_LINK_TTL_SECONDS == 300
