"""支付宝开放平台账单取件的协议兼容性测试（Task 13）。

覆盖五类：gateway.do 请求形态（biz_content / sign / sign_type）、
取件成功、``EMPTY_DATA_WITH_BILL_FILE``（成功码不是错误）、
``isv.bill-not-exist``、限频、验签失败，外加「非 JSON 信封不得当成空数据集」。

报文样例在 ``tests/fixtures/protocol/alipay/``（测试数据层），
产品代码走真实协议。**替身不是最终对接方式** —— 院内真实对接是直连
``https://openapi.alipay.com/gateway.do``，凭据从环境变量读。
本轮**未实连**真实端点（无院内商户凭据）。

本文件不含任何真实 AppID、应用私钥或支付宝公钥。
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import urllib.parse
import zipfile

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from tests.protocol.conftest import (
    BILL_DATE,
    load_message,
    load_raw,
    no_sleep,
)

from reconq.errors import ReconqError
from reconq.sources.alipay_bill import (
    ALIPAY_BILL_METHOD,
    ALIPAY_DEFAULT_GATEWAY,
    BILL_TYPES,
    DOWNLOAD_LINK_TTL_SECONDS,
    EMPTY_DATA_WITH_BILL_FILE,
    build_rsa2_params,
    fetch_bill,
)
from reconq.sources.retry import backoff_delays
from reconq.sources.signing import rsa2_sign

#: 一份脱敏支付宝交易明细（带回单抬头 + 尾部「对账结束」分隔行）
BILL_CSV = "\n".join(
    [
        "支付宝（中国）网络技术有限公司 电子客户回单",
        "商户号：EXAM_ALIPAY_MCH    起始日期：2026-09-01    终止日期：2026-09-07",
        "",
        "支付宝交易号,商户订单号,业务类型,金额,交易时间,收/支,对方账号,交易状态",
        "2026090722000001,EX-ALIPAY-0001,销售分析,1710.55,2026-09-07 10:01:05,收入,EXAM_CP,交易成功",
        "2026090722000002,EX-ALIPAY-0002,销售分析,1920.55,2026-09-07 11:20:41,收入,EXAM_CP,交易成功",
        "------------------------支付宝对账结束------------------------",
    ]
)


def _zip_blob() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("alipay_bill_20260907.csv", BILL_CSV.encode("gb18030"))
    return buf.getvalue()


def _sign_node(node: dict, key_path: str) -> dict:
    """给 ``code=10000`` 的响应节点补 sign（成功分支要验签）。

    失败码节点不签名——它们走错误分支，本就在验签之前被拦下。
    """
    if node.get("code") != "10000":
        return node
    key = serialization.load_pem_private_key(pathlib.Path(key_path).read_bytes(), password=None)
    return {**node, "sign": rsa2_sign(node, key)}


def _transport(
    *, body: dict, key_path: str, blob: bytes | None = None, sign: bool = True
) -> tuple[httpx.MockTransport, dict]:
    payload = blob if blob is not None else _zip_blob()
    state: dict = {"take_calls": 0, "download_calls": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if request.method == "POST":
            state["take_calls"] += 1
            if not sign:
                return httpx.Response(200, json=body)
            signed = dict(body)
            node = body.get("alipay_bill_downloadurl_query_response")
            if isinstance(node, dict):
                signed["alipay_bill_downloadurl_query_response"] = _sign_node(node, key_path)
            return httpx.Response(200, json=signed)
        state["download_calls"] += 1
        return httpx.Response(200, content=payload)

    return httpx.MockTransport(handler), state


def _fetch(transport: httpx.MockTransport, env: dict[str, str], **kw):
    """跑一次取件；MockTransport 同时接管取件与下载两段。"""
    return fetch_bill(
        BILL_DATE,
        bill_type=kw.pop("bill_type", "trade"),
        client=httpx.Client(transport=transport),
        env=env,
        sleep=kw.pop("sleep", no_sleep),
        **kw,
    )


# —— 请求形态：gateway.do 表单、biz_content、sign / sign_type ——————


def test_request_is_post_form_to_gateway(alipay_env, key_paths):
    """取件必须 POST 表单到 ``gateway.do``，不能拿微信那套 GET+query 去打。

    两条协议在请求方法上就分叉：套错的话支付宝会直接 405/签名错。
    """
    transport, state = _transport(
        body=load_message("alipay/query_success.json"), key_path=key_paths["alipay_private"]
    )
    _fetch(transport, alipay_env)

    req = state["requests"][0]
    assert req.method == "POST"
    assert str(req.url) == ALIPAY_DEFAULT_GATEWAY
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")


def test_form_fields_and_biz_content_shape(alipay_env, key_paths):
    """公共参数七件套齐全；``biz_content`` 是规范化 JSON 字符串（键序稳定）。

    ``biz_content`` 序列化成不同 JSON（键序、空格）签名就对不上——这是
    RSA2 最高频的「本机验签通过、渠道验签失败」来源。
    """
    transport, state = _transport(
        body=load_message("alipay/query_success.json"), key_path=key_paths["alipay_private"]
    )
    _fetch(transport, alipay_env)

    form = urllib.parse.parse_qs(state["requests"][0].content.decode("utf-8"))
    for field in ("app_id", "biz_content", "charset", "method", "sign_type", "timestamp", "version"):
        assert field in form, f"表单缺公共参数 {field}"
    assert form["method"] == [ALIPAY_BILL_METHOD]
    assert form["sign_type"] == ["RSA2"]
    assert form["version"] == ["1.0"]
    assert form["charset"] == ["utf-8"]
    assert form["sign"][0], "sign 必须非空"

    biz = form["biz_content"][0]
    # 规范化序列化：紧凑分隔符、无多余空格、键按字典序
    assert biz == '{"bill_date":"2026-09-07","bill_type":"trade"}'
    assert ", " not in biz and '": ' not in biz


def test_biz_content_is_stable_across_key_order():
    """同一份 biz_content 写成不同键序，签名内容逐字节一致。"""
    a = build_rsa2_params(
        method=ALIPAY_BILL_METHOD, app_id="EXAM", biz_content={"b": 2, "a": 1},
        timestamp="2026-09-07 10:00:00",
    )
    b = build_rsa2_params(
        method=ALIPAY_BILL_METHOD, app_id="EXAM", biz_content={"a": 1, "b": 2},
        timestamp="2026-09-07 10:00:00",
    )
    assert a["biz_content"] == b["biz_content"]


def test_bill_types_are_the_two_official_values():
    """``bill_type`` 只接受官方两值（trade / signcustomer）。"""
    assert BILL_TYPES == ("trade", "signcustomer")
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, bill_type="trades", env={})
    assert exc.value.code == "BILL_DATE_INVALID"


def test_download_link_ttl_is_thirty_seconds():
    """``bill_url`` 30 秒有效（微信是 5 分钟）——两处常量口径必须一致。"""
    assert DOWNLOAD_LINK_TTL_SECONDS == 30


# —— 三类响应 → 错误码映射 ——————————————————————————————————


def test_empty_data_with_bill_file_is_success(alipay_env, key_paths):
    """``EMPTY_DATA_WITH_BILL_FILE`` 是**成功码**：返回空数据集 + 原因，不报失败。

    对账岗的直觉是「这是错误」，照直觉实现会让每一期都多出一路假红。
    """
    transport, _ = _transport(
        body=load_message("alipay/query_empty_data_with_bill_file.json"),
        key_path=key_paths["alipay_private"],
    )
    bill = _fetch(transport, alipay_env)
    assert bill.is_empty is True
    assert bill.rows() == []
    assert bill.empty_reason == EMPTY_DATA_WITH_BILL_FILE
    assert bill.response_verified is True


def test_bill_not_exist_maps_and_does_not_retry(alipay_env, key_paths):
    """``isv.bill-not-exist`` 映射为 ``BILL_NOT_EXIST`` 且一次都不重试。"""
    body = load_message("alipay/query_bill_not_exist.json")
    transport, state = _transport(body=body, key_path=key_paths["alipay_private"])
    with pytest.raises(ReconqError) as exc:
        _fetch(transport, alipay_env)
    assert exc.value.code == "BILL_NOT_EXIST"
    assert state["take_calls"] == 1
    assert state["download_calls"] == 0


def test_frequency_limited_does_not_hard_retry(alipay_env, key_paths):
    """支付宝限频：不硬重试，一次即失败，渠道级失败不拖住其余五路。

    与微信的 ``FREQUENCY_LIMITED``（可重试）刻意不同——支付宝侧
    ``isv.overspeed-limit`` 退避重试只会继续消耗同一份配额。
    """
    body = load_message("alipay/query_frequency_limited.json")
    transport, state = _transport(body=body, key_path=key_paths["alipay_private"])
    with pytest.raises(ReconqError) as exc:
        _fetch(transport, alipay_env)
    assert exc.value.code == "CHANNEL_REQUEST_FAILED"
    assert state["take_calls"] == 1
    assert state["download_calls"] == 0


def test_unrecognized_sub_code_is_not_treated_as_success(alipay_env, key_paths):
    """未登记的 sub_code **不得**被静默当作成功——一律 CHANNEL_REQUEST_FAILED。"""
    body = {
        "alipay_bill_downloadurl_query_response": {
            "code": "40004",
            "msg": "Business Failed",
            "sub_code": "isv.something-new-in-future",
            "sub_msg": "新增业务错误码",
        }
    }
    transport, state = _transport(body=body, key_path=key_paths["alipay_private"])
    with pytest.raises(ReconqError) as exc:
        _fetch(transport, alipay_env)
    assert exc.value.code == "CHANNEL_REQUEST_FAILED"
    assert state["download_calls"] == 0


def test_response_signature_invalid_lands_no_rows(alipay_env, key_paths):
    """成功码但验签失败：拒绝使用该响应的 bill_url，**不落任何账单行**。

    ``code=10000`` 只说明支付宝受理了，不代表返回的 bill_url 没被改过。
    """
    body = load_message("alipay/query_success.json")
    transport, state = _transport(
        body=body, key_path=key_paths["alipay_private"], sign=False
    )
    with pytest.raises(ReconqError) as exc:
        _fetch(transport, alipay_env)
    assert exc.value.code == "RESPONSE_SIGNATURE_INVALID"
    assert state["download_calls"] == 0, "验签不过连 bill_url 都不许发请求"


def test_non_json_envelope_is_not_treated_as_empty_dataset(alipay_env, key_paths):
    """非 JSON 信封（XML）必须显式失败，**不得**当成「今天没数据」。

    当空数据集处理的后果是这一路合计静默归零，结论单却照样显示为绿。
    """
    xml_body = load_raw("alipay/query_xml_error.xml")
    state_holder: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            state_holder["take_calls"] = state_holder.get("take_calls", 0) + 1
            return httpx.Response(
                200, content=xml_body.encode("utf-8"), headers={"Content-Type": "text/xml"}
            )
        state_holder["download_calls"] = state_holder.get("download_calls", 0) + 1
        return httpx.Response(200, content=_zip_blob())

    with pytest.raises(ReconqError) as exc:
        _fetch(httpx.MockTransport(handler), alipay_env)
    assert exc.value.code == "CHANNEL_REQUEST_FAILED"
    assert state_holder.get("download_calls", 0) == 0


def test_downloaded_content_must_be_zip(alipay_env, key_paths):
    """下载到的不是 zip（多半是链接失效返回的 HTML 错误页）时显式报格式错。"""
    body = load_message("alipay/query_success.json")
    transport, state = _transport(
        body=body, key_path=key_paths["alipay_private"],
        blob=b"<html><body>error</body></html>",
    )
    with pytest.raises(ReconqError) as exc:
        _fetch(transport, alipay_env)
    assert exc.value.code == "FORMAT_BROKEN"
    assert state["download_calls"] == 1


def test_download_link_expired_maps_to_link_expired(alipay_env, key_paths):
    """``bill_url`` 过期（403/410）映射为 ``LINK_EXPIRED``，可重试一次后仍失败。"""
    body = load_message("alipay/query_success.json")
    state: dict = {"take": 0, "download": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            state["take"] += 1
            node = _sign_node(
                body["alipay_bill_downloadurl_query_response"],
                alipay_env["ALIPAY_PRIVATE_KEY_PATH"],
            )
            return httpx.Response(200, json={**body, "alipay_bill_downloadurl_query_response": node})
        state["download"] += 1
        return httpx.Response(403, content=b"expired")

    transport = httpx.MockTransport(handler)
    with pytest.raises(ReconqError) as exc:
        _fetch(transport, alipay_env, sleep=no_sleep)
    assert exc.value.code in ("LINK_EXPIRED", "RATE_LIMITED_EXHAUSTED")
    if exc.value.code == "RATE_LIMITED_EXHAUSTED":
        # 退避用尽：处置结果是「重试用尽」，真实成因仍在 cause_code 上
        assert exc.value.cause_code == "LINK_EXPIRED"
    assert state["take"] == 1
    # 下载重试次数有上限，不硬打
    assert state["download"] <= len(backoff_delays(3)) + 1
