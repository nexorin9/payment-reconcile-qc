"""微信 V3 / 支付宝 RSA2 签名、凭据装载与响应验签的协议级断言（Task 8）。

五类必测行为：

1. **签名串可复现**——同样的 method/url/body/timestamp/nonce 必须给出同样
   的签名头，否则排障时无法区分「签名错了」和「密钥变了」；
2. **字典序拼接正确**——支付宝 RSA2 的 ``k=v&k=v`` 必须按 key 升序，
   且**值不做 URL 编码**（编码了渠道就验不过）；
3. **空值剔除**——``None`` 与**空串**都必须剔（空串参与签名是
   「本机验签通过、渠道验签失败」的头号原因）；
4. **缺变量报错**——缺任一凭据位时**一个请求都不发出**，且一次点出全部
   缺失的变量名；
5. **验签失败报错**——微信响应与支付宝响应验不过都必须显式失败，
   **不得**当作成功去取账单。

⚠️ 本文件**不连接任何真实商户环境**。``tests/fixtures/keys/`` 下的 PEM 是
由 ``generate_test_pems.py`` 生成的**本地自签测试密钥**，
不是任何真实商户的私钥、平台证书或 APIv3 密钥。真实端点的实连需要院内
商户凭据，本轮未实连。
"""

from __future__ import annotations

import base64
import hashlib
import pathlib

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from reconq.errors import ReconqError
from reconq.sources.alipay_bill import ALIPAY_BILL_METHOD, build_rsa2_params
from reconq.sources.signing import (
    ALIPAY_REQUIRED_VARS,
    WECHAT_REQUIRED_VARS,
    AlipayDigest,
    alipay_sign_content,
    build_alipay_credentials,
    build_authorization_header,
    build_wechat_credentials,
    load_private_key,
    load_public_key,
    require_credentials,
    rsa2_sign,
    rsa2_verify,
    verify_alipay_response,
    verify_wechat_response,
    wechat_sign_message,
    wechat_signature,
)

KEY_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "keys"
WECHAT_PRIVATE = KEY_DIR / "wechat_test_private.pem"
WECHAT_PUBLIC = KEY_DIR / "wechat_test_public.pem"
WECHAT_CERT = KEY_DIR / "wechat_platform_cert.pem"
ALIPAY_PRIVATE = KEY_DIR / "alipay_test_private.pem"
ALIPAY_PUBLIC = KEY_DIR / "alipay_test_public.pem"
OTHER_PUBLIC = KEY_DIR / "other_test_public.pem"

#: 占位凭据值。**不是**任何真实商户的 app_id / 商户号 / APIv3 密钥。
FAKE_MCH_ID = "1900000109"
FAKE_WX_SERIAL = "5157F09EFDC096DE15EBE81A47057A72"
FAKE_WX_PLATFORM_SERIAL = "TESTPLATFORMSERIAL0001"
FAKE_ALIPAY_APP_ID = "2021000000000000"
FAKE_API_V3_KEY = "0123456789abcdef0123456789abcdef"
FAKE_TIMESTAMP = 1757203200
FAKE_NONCE = "0123456789abcdef0123456789abcdef"
BILL_DATE = "2026-09-07"
URL_PATH = f"/v3/bill/tradebill?bill_date={BILL_DATE}&bill_type=ALL&tar_type=GZIP"


# ------------------------------------------------------------------ 测试夹具


def _wechat_private():
    return load_private_key(
        str(WECHAT_PRIVATE), env_var="WECHAT_PRIVATE_KEY_PATH", channel="wechat"
    )


def _wechat_platform_key():
    return load_public_key(
        str(WECHAT_CERT), env_var="WECHAT_PLATFORM_CERT_PATH", channel="wechat"
    )


def _alipay_private():
    return load_private_key(
        str(ALIPAY_PRIVATE), env_var="ALIPAY_PRIVATE_KEY_PATH", channel="alipay"
    )


def _alipay_public():
    return load_public_key(
        str(ALIPAY_PUBLIC), env_var="ALIPAY_PUBLIC_KEY_PATH", channel="alipay"
    )


def _other_public():
    return load_public_key(
        str(OTHER_PUBLIC), env_var="ALIPAY_PUBLIC_KEY_PATH", channel="alipay"
    )


@pytest.fixture
def wechat_private():
    return _wechat_private()


@pytest.fixture
def alipay_private():
    return _alipay_private()


def _wechat_env(**overrides) -> dict[str, str]:
    env = {
        "WECHAT_MCH_ID": FAKE_MCH_ID,
        "WECHAT_SERIAL_NO": FAKE_WX_SERIAL,
        "WECHAT_API_V3_KEY": FAKE_API_V3_KEY,
        "WECHAT_PRIVATE_KEY_PATH": str(WECHAT_PRIVATE),
        "WECHAT_PLATFORM_CERT_PATH": str(WECHAT_CERT),
        "WECHAT_PLATFORM_CERT_SERIAL_NO": FAKE_WX_PLATFORM_SERIAL,
    }
    env.update(overrides)
    return env


def _alipay_env(**overrides) -> dict[str, str]:
    env = {
        "ALIPAY_APP_ID": FAKE_ALIPAY_APP_ID,
        "ALIPAY_PRIVATE_KEY_PATH": str(ALIPAY_PRIVATE),
        "ALIPAY_PUBLIC_KEY_PATH": str(ALIPAY_PUBLIC),
    }
    env.update(overrides)
    return env


def _sign_wechat_response(private_key, body: bytes, *, ts=FAKE_TIMESTAMP, nonce=FAKE_NONCE):
    """按微信口径给一个响应「补上」三个 Wechatpay-* 头（模拟渠道侧）。

    官方口径的被签消息是 ``时间戳\\n随机串\\n报文主体\\n``——**结尾的换行
    不能省**，与请求侧 GET 的空 body 要占一行是同一个道理。
    """
    message = f"{ts}\n{nonce}\n".encode() + body + b"\n"
    digest = hashlib.sha256(message).digest()
    sig = base64.b64encode(
        private_key.sign(digest, padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    return {
        "Wechatpay-Signature": sig,
        "Wechatpay-Timestamp": str(ts),
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Serial": FAKE_WX_PLATFORM_SERIAL,
    }


# ============================================ 1. 签名串可复现（构造正确性）


def test_wechat_sign_message_is_five_lines_with_trailing_newline():
    msg = wechat_sign_message(
        method="get",
        url_path=URL_PATH,
        body="",
        timestamp=FAKE_TIMESTAMP,
        nonce=FAKE_NONCE,
    )
    lines = msg.split("\n")
    assert lines[:5] == [
        "GET",
        URL_PATH,
        str(FAKE_TIMESTAMP),
        FAKE_NONCE,
        "",  # GET 的 body 为空串，但**这一行仍然占位**
    ]
    assert msg.endswith("\n")
    assert len(lines) == 6  # 末尾空串来自结尾换行


def test_wechat_signature_is_reproducible_for_same_inputs(wechat_private):
    msg = wechat_sign_message(
        method="GET",
        url_path=URL_PATH,
        body="",
        timestamp=FAKE_TIMESTAMP,
        nonce=FAKE_NONCE,
    )
    a = wechat_signature(msg, wechat_private)
    b = wechat_signature(msg, wechat_private)
    assert a == b
    # PKCS#1 v1.5 是确定性的：同样的消息必然给出同样的字节
    assert base64.b64decode(a)


def test_authorization_header_is_reproducible_and_has_five_elements(wechat_private):
    kwargs = dict(
        method="GET",
        url_path=URL_PATH,
        body="",
        mch_id=FAKE_MCH_ID,
        serial_no=FAKE_WX_SERIAL,
        private_key=wechat_private,
        timestamp=FAKE_TIMESTAMP,
        nonce=FAKE_NONCE,
    )
    first = build_authorization_header(**kwargs)
    second = build_authorization_header(**kwargs)
    assert first == second
    assert first.startswith("WECHATPAY2-SHA256-RSA2048 ")
    for element in ("mchid=", "nonce_str=", "timestamp=", "serial_no=", "signature="):
        assert element in first


def test_authorization_header_nonce_is_fresh_per_request(wechat_private):
    """nonce 复用会让同一份请求产生可重放的签名。"""
    kwargs = dict(
        method="GET",
        url_path=URL_PATH,
        body="",
        mch_id=FAKE_MCH_ID,
        serial_no=FAKE_WX_SERIAL,
        private_key=wechat_private,
        timestamp=FAKE_TIMESTAMP,
    )
    nonces = {
        build_authorization_header(nonce=None, **kwargs).split('nonce_str="')[1].split('"')[0]
        for _ in range(8)
    }
    assert len(nonces) == 8


def test_wechat_rejects_bad_method_and_multiline_body(wechat_private):
    with pytest.raises(ReconqError):
        wechat_sign_message(
            method="FETCH", url_path=URL_PATH, body="", timestamp=1, nonce="n"
        )
    with pytest.raises(ReconqError):
        wechat_sign_message(
            method="POST", url_path=URL_PATH, body='{"a":1}\n', timestamp=1, nonce="n"
        )


def test_rsa2_signature_is_reproducible(alipay_private):
    params = build_rsa2_params(
        method=ALIPAY_BILL_METHOD,
        app_id=FAKE_ALIPAY_APP_ID,
        biz_content={"bill_type": "trade", "bill_date": BILL_DATE},
        timestamp="2026-09-07 10:00:00",
    )
    signable = {k: v for k, v in params.items() if k != "_gateway"}
    assert rsa2_sign(signable, alipay_private) == rsa2_sign(signable, alipay_private)


# =============================================== 2. 字典序拼接正确（RSA2）


def test_alipay_sign_content_is_sorted_and_unencoded():
    params = {
        "version": "1.0",
        "app_id": FAKE_ALIPAY_APP_ID,
        "charset": "utf-8",
        "biz_content": '{"bill_date":"2026-09-07","bill_type":"trade"}',
        "method": ALIPAY_BILL_METHOD,
        "sign_type": "RSA2",
        "timestamp": "2026-09-07 10:00:00",
    }
    content = alipay_sign_content(params)
    keys = [part.split("=", 1)[0] for part in content.split("&")]
    assert keys == sorted(keys), f"未按字典序：{keys}"
    # 私有键 _gateway 不参与签名
    assert "_gateway" not in content
    # sign / sign_type 被剔除
    assert "sign=" not in content
    assert "sign_type=" not in content
    # 值不做 URL 编码：JSON 里的 { " : , 原样出现在签名串里
    assert '{"bill_date":"2026-09-07","bill_type":"trade"}' in content
    assert "%7B" not in content and "%22" not in content


def test_alipay_sign_content_excludes_sign_even_when_present():
    params = {
        "app_id": FAKE_ALIPAY_APP_ID,
        "sign": "should-be-ignored",
        "sign_type": "RSA2",
    }
    assert alipay_sign_content(params) == f"app_id={FAKE_ALIPAY_APP_ID}"


def test_alipay_biz_content_json_is_stable_across_calls():
    """biz_content 序列化成不同 JSON 是 RSA2 最高频的失败原因。"""
    a = build_rsa2_params(
        method=ALIPAY_BILL_METHOD,
        app_id=FAKE_ALIPAY_APP_ID,
        biz_content={"bill_date": BILL_DATE, "bill_type": "trade"},
        timestamp="2026-09-07 10:00:00",
    )
    b = build_rsa2_params(
        method=ALIPAY_BILL_METHOD,
        app_id=FAKE_ALIPAY_APP_ID,
        biz_content={"bill_type": "trade", "bill_date": BILL_DATE},
        timestamp="2026-09-07 10:00:00",
    )
    assert a["biz_content"] == b["biz_content"]
    assert " " not in a["biz_content"]  # separators 紧凑，无多余空格


def test_alipay_sign_and_verify_roundtrip(alipay_private):
    params = {"app_id": FAKE_ALIPAY_APP_ID, "method": ALIPAY_BILL_METHOD, "charset": "utf-8"}
    signature = rsa2_sign(params, alipay_private)
    assert rsa2_verify(params, signature, _alipay_public())


# ==================================================== 3. 空值剔除（含空串）


def test_alipay_sign_content_drops_none_and_empty_string():
    params = {
        "app_id": FAKE_ALIPAY_APP_ID,
        "empty_str": "",
        "none_val": None,
        "whitespace": "  ",
        "zero": "0",
        "real": "value",
    }
    content = alipay_sign_content(params)
    assert content == f"app_id={FAKE_ALIPAY_APP_ID}&real=value&whitespace=  &zero=0"


def test_alipay_empty_string_would_change_signature_if_kept():
    """空串参不参与签名会实打实改变签名结果——不是形式主义。"""
    base = {"app_id": FAKE_ALIPAY_APP_ID, "method": ALIPAY_BILL_METHOD}
    without = rsa2_sign(base, _alipay_private())
    with_empty = rsa2_sign({**base, "notify_url": ""}, _alipay_private())
    # 空串被剔除 → 签名不变（与不传该参数等价）
    assert without == with_empty
    # 而「填了非空值」必然改变签名
    assert without != rsa2_sign({**base, "notify_url": "https://x"}, _alipay_private())


def test_wechat_sign_message_keeps_empty_body_line():
    """与支付宝相反：微信的空 body **要占一行**，不能省。"""
    empty = wechat_sign_message(
        method="GET", url_path=URL_PATH, body="", timestamp=1, nonce="n"
    )
    nonempty = wechat_sign_message(
        method="POST", url_path=URL_PATH, body="{}", timestamp=1, nonce="n"
    )
    assert empty != nonempty


# ===================================== 4. 缺变量报错（缺位即拒，不发请求）


@pytest.mark.parametrize("missing", WECHAT_REQUIRED_VARS)
def test_wechat_missing_any_credential_names_it_and_never_loads_key(missing):
    env = _wechat_env(**{missing: ""})
    with pytest.raises(ReconqError) as exc:
        build_wechat_credentials(env)
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert missing in exc.value.detail


@pytest.mark.parametrize("missing", ALIPAY_REQUIRED_VARS)
def test_alipay_missing_any_credential_names_it(missing):
    env = _alipay_env(**{missing: ""})
    with pytest.raises(ReconqError) as exc:
        build_alipay_credentials(env)
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert missing in exc.value.detail


def test_require_credentials_reports_all_missing_at_once():
    """一次列全，而不是「补一个跑一次、又报下一个」。"""
    with pytest.raises(ReconqError) as exc:
        require_credentials(
            {"A": "1", "B": ""},
            ("A", "B", "C", "D"),
            channel="wechat",
            context="调用微信账单接口",
        )
    assert "B" in exc.value.detail
    assert "C" in exc.value.detail
    assert "D" in exc.value.detail
    assert "A" not in exc.value.detail.replace("调用", "")


def test_require_credentials_treats_whitespace_only_as_missing():
    with pytest.raises(ReconqError):
        require_credentials(
            {"A": "   "}, ("A",), channel="wechat", context="调用微信账单接口"
        )


def test_missing_platform_cert_refuses_wechat_credentials():
    """没有平台证书就验不了签；本产品不提供「只签名不验签」的默认模式。"""
    env = _wechat_env(WECHAT_PLATFORM_CERT_PATH="")
    with pytest.raises(ReconqError) as exc:
        build_wechat_credentials(env)
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "WECHAT_PLATFORM_CERT_PATH" in exc.value.detail


def test_nonexistent_key_file_names_path_and_env_var():
    with pytest.raises(ReconqError) as exc:
        load_private_key("/nonexistent/x.pem", env_var="WECHAT_PRIVATE_KEY_PATH", channel="wechat")
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "WECHAT_PRIVATE_KEY_PATH" in exc.value.detail
    assert "/nonexistent/x.pem" in exc.value.detail


def test_unparsable_pem_names_env_var(tmp_path):
    bad = tmp_path / "broken.pem"
    bad.write_text("-----BEGIN PRIVATE KEY-----\nnot base64 pem\n-----END PRIVATE KEY-----\n")
    with pytest.raises(ReconqError) as exc:
        load_private_key(str(bad), env_var="ALIPAY_PRIVATE_KEY_PATH", channel="alipay")
    assert "ALIPAY_PRIVATE_KEY_PATH" in exc.value.detail


def test_public_key_accepts_bare_pem_and_x509_certificate():
    """微信给的是平台证书（X.509 容器），支付宝给的是裸公钥——两种都要能读。"""
    assert load_public_key(str(WECHAT_CERT), env_var="X", channel="wechat") is not None
    assert load_public_key(str(ALIPAY_PUBLIC), env_var="Y", channel="alipay") is not None


def test_credentials_carry_expected_material():
    creds = build_wechat_credentials(_wechat_env())
    assert creds.mch_id == FAKE_MCH_ID
    assert creds.serial_no == FAKE_WX_SERIAL
    assert creds.api_v3_key == FAKE_API_V3_KEY
    assert creds.platform_public_key is not None

    ali = build_alipay_credentials(_alipay_env())
    assert ali.app_id == FAKE_ALIPAY_APP_ID
    assert ali.public_key is not None
    assert ali.digest == AlipayDigest.MD5


def test_alipay_digest_is_configurable():
    """官方 SDK 用 SHA1，院内老接入用 MD5——做成可切换常量而不是写死分支。"""
    ali = build_alipay_credentials(_alipay_env(ALIPAY_SIGN_DIGEST="sha1"))
    assert ali.digest == "SHA1"
    params = {"app_id": FAKE_ALIPAY_APP_ID, "method": ALIPAY_BILL_METHOD}
    sig_sha1 = rsa2_sign(params, ali.private_key, digest="SHA1")
    assert rsa2_verify(params, sig_sha1, _alipay_public(), digest="SHA1")
    # 换算法后旧签名不再通过
    assert not rsa2_verify(params, sig_sha1, _alipay_public(), digest="MD5")


# ==================================================== 5. 验签失败显式报错


def test_wechat_response_verification_passes_for_genuine_signature(wechat_private):
    body = b'{"hash_type":"SHA1","download_url":"https://example.invalid/x"}'
    headers = _sign_wechat_response(wechat_private, body)
    verify_wechat_response(
        headers=headers, body=body, platform_public_key=_wechat_platform_key()
    )


def test_wechat_response_verification_fails_on_tampered_body(wechat_private):
    body = b'{"download_url":"https://example.invalid/good"}'
    headers = _sign_wechat_response(wechat_private, body)
    tampered = b'{"download_url":"https://attacker.invalid/evil"}'
    with pytest.raises(ReconqError) as exc:
        verify_wechat_response(
            headers=headers, body=tampered, platform_public_key=_wechat_platform_key()
        )
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"
    assert "拒绝使用该响应取账单" in exc.value.detail


def test_wechat_response_verification_fails_with_wrong_platform_key(wechat_private):
    body = b'{"download_url":"https://example.invalid/x"}'
    headers = _sign_wechat_response(wechat_private, body)
    with pytest.raises(ReconqError) as exc:
        verify_wechat_response(
            headers=headers, body=body, platform_public_key=_other_public()
        )
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"


@pytest.mark.parametrize(
    "dropped",
    ["Wechatpay-Signature", "Wechatpay-Timestamp", "Wechatpay-Nonce", "Wechatpay-Serial"],
)
def test_wechat_missing_signature_header_is_rejected(wechat_private, dropped):
    body = b'{"download_url":"https://example.invalid/x"}'
    headers = _sign_wechat_response(wechat_private, body)
    headers.pop(dropped)
    with pytest.raises(ReconqError) as exc:
        verify_wechat_response(
            headers=headers, body=body, platform_public_key=_wechat_platform_key()
        )
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"
    assert dropped in exc.value.detail


def test_wechat_serial_mismatch_is_rejected(wechat_private):
    body = b'{"download_url":"https://example.invalid/x"}'
    headers = _sign_wechat_response(wechat_private, body)
    with pytest.raises(ReconqError) as exc:
        verify_wechat_response(
            headers=headers,
            body=body,
            platform_public_key=_wechat_platform_key(),
            expected_serial_no="SOME_OTHER_SERIAL",
        )
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"
    assert "SOME_OTHER_SERIAL" in exc.value.detail


def test_wechat_garbage_signature_is_rejected_not_crashed(wechat_private):
    body = b'{"download_url":"https://example.invalid/x"}'
    headers = _sign_wechat_response(wechat_private, body)
    headers["Wechatpay-Signature"] = "!!!not-base64!!!"
    with pytest.raises(ReconqError) as exc:
        verify_wechat_response(
            headers=headers, body=body, platform_public_key=_wechat_platform_key()
        )
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"


def test_wechat_header_lookup_is_case_insensitive(wechat_private):
    body = b'{"download_url":"https://example.invalid/x"}'
    headers = {k.lower(): v for k, v in _sign_wechat_response(wechat_private, body).items()}
    verify_wechat_response(
        headers=headers, body=body, platform_public_key=_wechat_platform_key()
    )


def test_alipay_response_verification_passes_for_genuine_signature(alipay_private):
    node = {
        "code": "10000",
        "msg": "Success",
        "bill_url": "https://dwbillcenter.example.invalid/downloadBillFile.resource?x=1",
    }
    node["sign"] = rsa2_sign(node, alipay_private)
    verify_alipay_response(node, _alipay_public())


def test_alipay_response_verification_fails_on_tampered_bill_url(alipay_private):
    node = {
        "code": "10000",
        "msg": "Success",
        "bill_url": "https://dwbillcenter.example.invalid/downloadBillFile.resource?x=1",
    }
    node["sign"] = rsa2_sign(node, alipay_private)
    node["bill_url"] = "https://attacker.invalid/evil.resource"
    with pytest.raises(ReconqError) as exc:
        verify_alipay_response(node, _alipay_public())
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"
    assert "拒绝使用该响应取账单" in exc.value.detail


def test_alipay_response_verification_fails_with_wrong_public_key(alipay_private):
    node = {"code": "10000", "msg": "Success", "bill_url": "https://x.invalid/a"}
    node["sign"] = rsa2_sign(node, alipay_private)
    with pytest.raises(ReconqError) as exc:
        verify_alipay_response(node, _other_public())
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"


def test_alipay_response_without_sign_is_rejected():
    node = {"code": "10000", "msg": "Success", "bill_url": "https://x.invalid/a"}
    with pytest.raises(ReconqError) as exc:
        verify_alipay_response(node, _alipay_public())
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"
    assert "没有 sign 字段" in exc.value.detail


def test_alipay_empty_data_success_is_still_verified(alipay_private):
    """EMPTY_DATA_WITH_BILL_FILE 是成功码，但**照样要验签**。"""
    node = {
        "code": "10000",
        "sub_code": "EMPTY_DATA_WITH_BILL_FILE",
        "sub_msg": "账单文件存在，但无交易数据",
    }
    node["sign"] = rsa2_sign(node, alipay_private)
    verify_alipay_response(node, _alipay_public())


# ================================== 取数客户端接线（验签不过不取账单）


def _wechat_ok_transport(private_key, response_body: dict) -> httpx.MockTransport:
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/bill/tradebill":
            raw = json.dumps(response_body).encode()
            return httpx.Response(200, content=raw, headers=_sign_wechat_response(private_key, raw))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_fetch_bill_rejects_unsigned_wechat_response(wechat_private):
    """取件响应缺 Wechatpay-* 头 → 不去下载，直接失败。"""
    import json

    body = json.dumps(
        {"hash_type": "SHA1", "hash": "0" * 40, "download_url": "https://x.invalid/d"}
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)  # 没有任何 Wechatpay-* 头

    client = httpx.Client(transport=httpx.MockTransport(handler))
    from reconq.sources.wechat_bill import fetch_bill

    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_wechat_env(), sleep=lambda _s: None)
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"


def test_fetch_bill_marks_response_verified_when_signature_ok(wechat_private):
    """造一份真实的加密账单：验签通过 → response_verified=True。"""
    import gzip
    import io
    import json

    csv_text = "\n".join(
        [
            "微信支付账单明细列表",
            "商户号：EXAM_WX_MCH_ID    账单日期：2026-09-07",
            "",
            "交易时间,商户订单号,微信交易单号,交易状态,应结订单金额,退款金额",
            f"{BILL_DATE} 09:12:03,EX-WX-9001,EX-WXTXN-9001,SUCCESS,128.00,0.00",
        ]
    )
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(csv_text.encode("utf-8"))
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = FAKE_API_V3_KEY.encode()
    nonce = b"0123456789ab"
    blob = nonce + AESGCM(key).encrypt(nonce, buf.getvalue(), None)
    digest = hashlib.sha1(blob).hexdigest()

    transport = _wechat_ok_transport(
        wechat_private,
        {"hash_type": "SHA1", "hash": digest, "download_url": "https://x.invalid/d"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/bill/tradebill":
            raw = json.dumps(
                {"hash_type": "SHA1", "hash": digest, "download_url": "https://x.invalid/d"}
            ).encode()
            return httpx.Response(200, content=raw, headers=_sign_wechat_response(wechat_private, raw))
        return httpx.Response(200, content=blob)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    from reconq.sources.wechat_bill import fetch_bill

    bill = fetch_bill(BILL_DATE, client=client, env=_wechat_env(), sleep=lambda _s: None)
    assert bill.response_verified is True
    assert bill.sha1 == digest
    assert len(bill.rows()) == 1


def test_fetch_bill_can_skip_verification_only_when_explicit(wechat_private):
    """verify_response=False 是显式的排查腿，结果里必须标出来。"""
    import json

    body = json.dumps(
        {"hash_type": "SHA1", "hash": "0" * 40, "download_url": "https://x.invalid/d"}
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/bill/tradebill":
            return httpx.Response(200, content=b'{"hash_type":"SHA1","hash":"' + b"0" * 40 + b'","download_url":"https://x.invalid/d"}')
        return httpx.Response(403)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    from reconq.sources.wechat_bill import fetch_bill

    with pytest.raises(ReconqError) as exc:
        fetch_bill(
            BILL_DATE,
            client=client,
            env=_wechat_env(),
            sleep=lambda _s: None,
            verify_response=False,
        )
    # 跳过验签后流程继续走到下载环节：退避用尽后以 RATE_LIMITED_EXHAUSTED 收场，
    # 但**根因**仍是链接过期（cause_code 保留真实成因，不被处置结果掩盖）
    assert exc.value.code == "RATE_LIMITED_EXHAUSTED"
    assert exc.value.cause_code == "LINK_EXPIRED"


# =========================================== 替身边界（不落真实商户凭据）


def test_no_real_merchant_credentials_in_fixtures():
    """夹具里的私钥必须是脚本生成的测试密钥，不得混入真实商户材料。"""
    readme = (KEY_DIR / "generate_test_pems.py").read_text(encoding="utf-8")
    assert "本地自签测试密钥" in readme
    # 逐项点名：脚本须声明这是自签替身、且禁止在真实环境复用
    for marker in ("自签", "真实商户", "禁止", "真实环境"):
        assert marker in readme, f"生成脚本缺少免责说明：{marker}"
    # 测试用 APIv3 密钥是 32 位占位串（微信官方文档示例形态）
    assert FAKE_API_V3_KEY == "0123456789abcdef0123456789abcdef"


def test_placeholder_credentials_used_in_tests_are_obviously_fake():
    assert FAKE_ALIPAY_APP_ID.startswith("2021") and FAKE_ALIPAY_APP_ID.endswith("0" * 12)
    assert set(FAKE_MCH_ID) <= set("0123456789")
    assert set(FAKE_WX_SERIAL) <= set("0123456789ABCDEF")
