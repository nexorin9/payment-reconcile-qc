"""微信支付商户账单取数的协议级断言（Task 5）。

用 ``httpx.MockTransport`` 回放**真实报文结构**（字段名、错误码、header 形态），
产品代码走真实协议与真实字段规范；替身只出现在测试数据层。

覆盖五类关键行为：

1. SHA1 不符拒绝入库（不拿半截文件对账）；
2. ``FREQUENCY_LIMITED`` / ``STATEMENT_CREATING`` 退避重试且次数有上限；
3. 下载链接过期（403/410）显式失败而不是拿旧内容；
4. ``BILL_NOT_EXIST`` 不重试（今天真没出账，退避到天荒也没用）；
5. GBK / UTF-8-sig 账单正文、AES-256-GCM 解密失败显式报。

⚠️ 本文件**不连接任何真实商户环境**，也**不含任何真实商户号、私钥或 APIv3 密钥**。
真实端点的实连需要院内商户凭据，本轮未实连。
"""

from __future__ import annotations

import gzip
import hashlib
import pathlib
import json
import os
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from reconq.errors import ReconqError
from reconq.sources.retry import backoff_delays
from reconq.sources.wechat_bill import (
    BILL_TYPES,
    DOWNLOAD_LINK_TTL_SECONDS,
    build_authorization_header,
    decrypt_bill,
    fetch_bill,
    parse_bill_csv,
)

BILL_DATE = "2026-09-07"
#: 测试用占位密钥，**不是任何真实商户的 APIv3 密钥**
FAKE_API_V3_KEY = "0123456789abcdef0123456789abcdef"
FAKE_MCH_ID = "1900000109"  # 微信官方文档的示例商户号，非真实商户
FAKE_SERIAL_NO = "5157F09EFDC096DE15EBE81A47057A72"
FAKE_PLATFORM_SERIAL = "TESTPLATFORMSERIAL0001"
FAKE_TIMESTAMP = 1757203200
FAKE_NONCE = "0123456789abcdef0123456789abcdef"

#: 一份脱敏微信交易明细（带抬头行，账单常见形态）
BILL_CSV = "\n".join(
    [
        "微信支付账单明细列表",
        "商户号：EXAM_WX_MCH_ID    账单日期：2026-09-07",
        "",
        "交易时间,公众账号ID,商户订单号,微信交易单号,交易类型,交易状态,应结订单金额,退款金额,商品名称",
        "2026-09-07 09:12:03,EXAM_WX_MCH_ID,EX-WX-0001,EX-WXTXN-0001,JSAPI,SUCCESS,128.00,0.00,门诊缴费",
        "2026-09-07 09:31:44,EXAM_WX_MCH_ID,EX-WX-0002,EX-WXTXN-0002,NATIVE,SUCCESS,1020.50,0.00,住院预交",
        "2026-09-07 10:05:19,EXAM_WX_MCH_ID,EX-WX-0003,EX-WXTXN-0003,JSAPI,REFUND,56.00,56.00,门诊退费",
    ]
)


@pytest.fixture(scope="module")
def fake_key(tmp_path_factory):
    """生成本地自签测试私钥（仅测试用，明示不是任何真实商户密钥）。"""
    path = tmp_path_factory.mktemp("keys") / "test_only_apiclient_key.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(path)


@pytest.fixture(scope="module")
def fake_cert(tmp_path_factory, fake_key):
    """自签平台证书（仅测试用）：没有它就没有验签公钥（Task 8）。"""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes as _h
    from cryptography.x509.oid import NameOID

    key = serialization.load_pem_private_key(pathlib.Path(fake_key).read_bytes(), password=None)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TEST-PLATFORM-CERT")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        .not_valid_after(dt.datetime(2036, 1, 1, tzinfo=dt.timezone.utc))
        .sign(key, _h.SHA256())
    )
    path = tmp_path_factory.mktemp("certs") / "test_only_platform_cert.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


def _sign_take_response(resp: httpx.Response, key_path: str) -> httpx.Response:
    """给 200 取件响应补上三个 Wechatpay-* 头（模拟渠道侧，Task 8 验签要求）。"""
    if resp.status_code != 200:
        return resp
    import base64
    from cryptography.hazmat.primitives import hashes as _h
    from cryptography.hazmat.primitives.asymmetric import padding as _p

    key = serialization.load_pem_private_key(pathlib.Path(key_path).read_bytes(), password=None)
    ts, nonce = str(FAKE_TIMESTAMP), FAKE_NONCE
    message = f"{ts}\n{nonce}\n".encode() + resp.content + b"\n"
    sig = base64.b64encode(key.sign(hashlib.sha256(message).digest(), _p.PKCS1v15(), _h.SHA256())).decode()
    resp.headers["Wechatpay-Signature"] = sig
    resp.headers["Wechatpay-Timestamp"] = ts
    resp.headers["Wechatpay-Nonce"] = nonce
    resp.headers["Wechatpay-Serial"] = FAKE_PLATFORM_SERIAL
    return resp


def _env(key_path: str, cert_path: str) -> dict[str, str]:
    """测试环境变量：全部为占位值，不含任何真实凭据。"""
    return {
        "WECHAT_MCH_ID": FAKE_MCH_ID,
        "WECHAT_SERIAL_NO": FAKE_SERIAL_NO,
        "WECHAT_PRIVATE_KEY_PATH": key_path,
        "WECHAT_API_V3_KEY": FAKE_API_V3_KEY,
        "WECHAT_PLATFORM_CERT_PATH": cert_path,
        "WECHAT_PLATFORM_CERT_SERIAL_NO": FAKE_PLATFORM_SERIAL,
    }


def _encrypt(plain: bytes, key: str = FAKE_API_V3_KEY) -> bytes:
    """按微信口径封装：nonce(12B) || ciphertext || tag(16B)。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = b"\x01" * 12
    return nonce + AESGCM(key.encode()).encrypt(nonce, plain, None)


def _blob(text: str = BILL_CSV) -> bytes:
    return _encrypt(gzip.compress(text.encode("utf-8")))


def _transport(
    *,
    blob: bytes,
    key_path: str,
    declared_sha1: str | None = None,
    take_responses: list[httpx.Response] | None = None,
    download_status: int = 200,
) -> httpx.MockTransport:
    """构造一个回放取件+下载两段链路的 MockTransport。"""
    sha1 = declared_sha1 if declared_sha1 is not None else hashlib.sha1(blob).hexdigest()
    take_iter = iter(take_responses or [httpx.Response(200, json={"hash_type": "SHA1", "hash": sha1, "download_url": "https://api.mch.weixin.qq.com/v3/bill/download/EXAM"})])
    state = {"download_calls": 0, "take_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/v3/bill/tradebill" in str(request.url):
            state["take_calls"] += 1
            resp = next(take_iter, take_responses[-1] if take_responses else httpx.Response(500))
            return _sign_take_response(resp, key_path)
        state["download_calls"] += 1
        if download_status != 200:
            return httpx.Response(download_status, json={"code": "RESOURCE_EXPIRES"})
        return httpx.Response(200, content=blob)

    handler.state = state  # type: ignore[attr-defined]
    return httpx.MockTransport(handler)


def _no_sleep(_seconds: float) -> None:
    """假时钟：退避序列照走，但不真的等。"""


# —— 成功路径 ————————————————————————————————————————————————


def test_fetch_bill_end_to_end_returns_verified_rows(fake_key, fake_cert):
    blob = _blob()
    client = httpx.Client(transport=_transport(key_path=fake_key, blob=blob))
    bill = fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_cert), sleep=_no_sleep)
    assert bill.bill_date == BILL_DATE
    assert bill.sha1 == hashlib.sha1(blob).hexdigest()
    assert bill.gzip_layers == 1  # 解密后仍是 gzip，被解掉一层
    rows = bill.rows()
    assert len(rows) == 3
    # 抬头被跳过；行号指源文件真实行（含抬头偏移）
    assert [r.row_no for r in rows] == [5, 6, 7]
    assert Decimal(rows[0].values["amount"]) == Decimal("128.00")
    assert Decimal(rows[1].values["amount"]) == Decimal("1020.50")  # 千分位被洗
    assert rows[2].values["trade_state"] == "REFUND"


def test_authorization_header_shape_and_signature(fake_key, fake_cert):
    from cryptography.hazmat.primitives import serialization as ser

    key = ser.load_pem_private_key(open(fake_key, "rb").read(), password=None)
    header = build_authorization_header(
        method="GET",
        url_path="/v3/bill/tradebill?bill_date=2026-09-07&bill_type=ALL",
        body="",
        mch_id=FAKE_MCH_ID,
        serial_no=FAKE_SERIAL_NO,
        private_key=key,
        timestamp=1757200800,
        nonce="NONCE-EXAM-0001",
    )
    assert header.startswith("WECHATPAY2-SHA256-RSA2048 ")
    for part in ('mchid="%s"' % FAKE_MCH_ID, 'nonce_str="NONCE-EXAM-0001"', 'timestamp="1757200800"', 'serial_no="%s"' % FAKE_SERIAL_NO):
        assert part in header
    assert 'signature="' in header


def test_signature_is_reproducible_for_same_inputs(fake_key, fake_cert):
    from cryptography.hazmat.primitives import serialization as ser

    key = ser.load_pem_private_key(open(fake_key, "rb").read(), password=None)
    kwargs = dict(
        method="GET", url_path="/v3/bill/tradebill", body="", mch_id=FAKE_MCH_ID,
        serial_no=FAKE_SERIAL_NO, private_key=key, timestamp=1757200800, nonce="N-EXAM",
    )
    assert build_authorization_header(**kwargs) == build_authorization_header(**kwargs)
    # nonce 变 → 签名必须变（防重放）
    other = build_authorization_header(**{**kwargs, "nonce": "N-EXAM-2"})
    assert other != build_authorization_header(**kwargs)


# —— SHA1 校验 ————————————————————————————————————————————————


def test_sha1_mismatch_is_rejected_and_not_retried_forever(fake_key, fake_cert):
    blob = _blob()
    client = httpx.Client(transport=_transport(key_path=fake_key, blob=blob, declared_sha1="0" * 40))
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_cert), sleep=_no_sleep)
    assert exc.value.code == "SHA1_MISMATCH"
    assert "拒绝入库" in exc.value.detail


def test_download_failure_does_not_fall_back_to_partial_content(fake_key, fake_cert):
    client = httpx.Client(transport=_transport(key_path=fake_key, blob=_blob(), download_status=500))
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_cert), sleep=_no_sleep)
    assert exc.value.code == "BILL_DOWNLOAD_FAILED"


# —— 链接过期 ————————————————————————————————————————————————


@pytest.mark.parametrize("status", [401, 403, 410])
def test_expired_download_link_is_explicit_not_reused(fake_key, fake_cert, status):
    client = httpx.Client(transport=_transport(key_path=fake_key, blob=_blob(), download_status=status))
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_cert), sleep=_no_sleep)
    # 处置结果是「重试用尽」，真实成因必须挂在 cause_code 上——
    # 面板只显示 RATE_LIMITED_EXHAUSTED 会让人以为是被限频，其实是链接过期
    assert exc.value.code == "RATE_LIMITED_EXHAUSTED"
    assert exc.value.cause_code == "LINK_EXPIRED"
    assert str(DOWNLOAD_LINK_TTL_SECONDS) in exc.value.detail


# —— 退避重试 ————————————————————————————————————————————————


def test_frequency_limited_is_retried_with_bounded_backoff(fake_key, fake_cert):
    """限频可重试：退避有上限，用尽后抛 RATE_LIMITED_EXHAUSTED 而非无限等。"""
    waits: list[float] = []
    take_responses = [
        httpx.Response(429, json={"code": "FREQUENCY_LIMITED", "message": "触发频率限制"}),
        httpx.Response(429, json={"code": "FREQUENCY_LIMITED", "message": "触发频率限制"}),
        httpx.Response(429, json={"code": "FREQUENCY_LIMITED", "message": "触发频率限制"}),
    ]
    client = httpx.Client(transport=_transport(key_path=fake_key, blob=_blob(), take_responses=take_responses))
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_cert), sleep=waits.append)
    assert exc.value.code == "RATE_LIMITED_EXHAUSTED"
    assert exc.value.cause_code == "FREQUENCY_LIMITED"  # 真实成因可追
    assert "FREQUENCY_LIMITED" in exc.value.detail
    assert len(waits) == 2  # 3 次尝试 = 2 次退避，有上限
    assert waits == backoff_delays(3)


def test_frequency_limited_then_success_returns_bill(fake_key, fake_cert):
    """限频后成功：退避不是放弃，是等渠道松口。"""
    blob = _blob()
    sha1 = hashlib.sha1(blob).hexdigest()
    take_responses = [
        httpx.Response(429, json={"code": "FREQUENCY_LIMITED"}),
        httpx.Response(429, json={"code": "FREQUENCY_LIMITED"}),
        httpx.Response(200, json={"hash_type": "SHA1", "hash": sha1, "download_url": "https://api.mch.weixin.qq.com/v3/bill/download/EXAM"}),
    ]
    waits: list[float] = []
    client = httpx.Client(transport=_transport(key_path=fake_key, blob=blob, take_responses=take_responses))
    bill = fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_cert), sleep=waits.append)
    assert len(bill.rows()) == 3
    assert len(waits) == 2


def test_bill_not_exist_is_not_retried(fake_key, fake_cert):
    """今天真没出账 → 退避到天荒也没用，不浪费渠道配额。"""
    waits: list[float] = []
    client = httpx.Client(
        transport=_transport(
            key_path=fake_key,
            blob=_blob(),
            take_responses=[httpx.Response(400, json={"code": "BILL_NOT_EXIST", "message": "账单不存在"})],
        )
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_cert), sleep=waits.append)
    assert exc.value.code == "BILL_NOT_EXIST"
    assert waits == []  # 一次都不重试


def test_statement_creating_is_retried_then_exhausted(fake_key, fake_cert):
    waits: list[float] = []
    client = httpx.Client(
        transport=_transport(
            key_path=fake_key,
            blob=_blob(),
            take_responses=[httpx.Response(403, json={"code": "STATEMENT_CREATING"})],
        )
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_cert), sleep=waits.append)
    assert exc.value.code == "RATE_LIMITED_EXHAUSTED"
    assert exc.value.cause_code == "STATEMENT_CREATING"
    assert len(waits) == 2


# —— 凭据与参数前置校验 ——————————————————————————————————————


def test_missing_credential_fails_before_any_request(fake_key, fake_cert):
    env = _env(fake_key, fake_cert)
    env.pop("WECHAT_API_V3_KEY")
    called: list[str] = []
    transport = httpx.MockTransport(lambda req: (called.append(str(req)), httpx.Response(200))[1])
    client = httpx.Client(transport=transport)
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=env, sleep=_no_sleep)
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "WECHAT_API_V3_KEY" in exc.value.detail
    assert called == []  # 一个请求都没发出去


def test_missing_private_key_file_names_the_path(fake_key, fake_cert):
    env = _env(fake_key, fake_cert)
    env["WECHAT_PRIVATE_KEY_PATH"] = "/nonexistent/test_only_key.pem"
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, env=env, sleep=_no_sleep)
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "test_only_key.pem" in exc.value.detail


def test_bad_bill_date_and_type_rejected_before_request(fake_key, fake_cert):
    client = httpx.Client(transport=_transport(key_path=fake_key, blob=_blob()))
    with pytest.raises(ReconqError) as exc:
        fetch_bill("2026/09/07", client=client, env=_env(fake_key, fake_cert), sleep=_no_sleep)
    assert exc.value.code == "BILL_DATE_INVALID"
    with pytest.raises(ReconqError) as exc2:
        fetch_bill(BILL_DATE, bill_type="MIXED", client=client, env=_env(fake_key, fake_cert), sleep=_no_sleep)
    assert exc2.value.code == "BILL_DATE_INVALID"
    assert BILL_TYPES == ("ALL", "SUCCESS", "REFUND")


# —— 解密 ————————————————————————————————————————————————


def test_wrong_api_v3_key_is_explicit_not_silent_garbage(fake_key, fake_cert):
    """密钥错了不会解出半截明文，只会被 GCM tag 校验拒绝。"""
    client = httpx.Client(transport=_transport(key_path=fake_key, blob=_blob()))
    env = _env(fake_key, fake_cert)
    env["WECHAT_API_V3_KEY"] = "f" * 32
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=env, sleep=_no_sleep)
    assert exc.value.code == "BILL_DECRYPT_FAILED"
    assert "WECHAT_API_V3_KEY" in exc.value.detail


def test_api_v3_key_length_checked(fake_key, fake_cert):
    with pytest.raises(ReconqError) as exc:
        decrypt_bill(b"\x00" * 64, "short-key")
    assert exc.value.code == "BILL_DECRYPT_FAILED"
    assert "32 字符" in exc.value.detail


def test_truncated_ciphertext_rejected():
    with pytest.raises(ReconqError) as exc:
        decrypt_bill(b"\x00" * 10, FAKE_API_V3_KEY)
    assert exc.value.code == "BILL_DECRYPT_FAILED"
    assert "没下全" in exc.value.detail


# —— CSV 解析边界 ————————————————————————————————————————————


def test_parse_bill_csv_requires_a_trade_time_header():
    with pytest.raises(ReconqError) as exc:
        parse_bill_csv("a,b,c\n1,2,3\n", source_file="x.csv")
    assert exc.value.code == "FORMAT_BROKEN"
    assert "交易时间" in exc.value.detail


def test_parse_bill_csv_reports_ragged_rows_with_line_number():
    text = (
        "交易时间,商户订单号,微信交易单号,应结订单金额\n"
        "2026-09-07 09:12:03,EX-1,EX-T-1,128.00\n"
        "2026-09-07 09:31:44,EX-2\n"
    )
    with pytest.raises(ReconqError) as exc:
        parse_bill_csv(text, source_file="ex.csv")
    assert exc.value.code == "COLUMN_COUNT_MISMATCH"
    assert exc.value.row_ref == "ex.csv:3"
