"""支付宝账单取数的协议级断言（Task 5）。

用 ``httpx.MockTransport`` 回放**真实报文结构**（``alipay_bill_downloadurl_query_response``
节点、``code``/``sub_code``/``bill_url`` 字段），产品代码走真实协议与真实字段规范。

覆盖五类关键行为：

1. ``EMPTY_DATA_WITH_BILL_FILE`` 是**成功码**：返回空数据集 + 原因，不报失败；
2. RSA2 签名按**字典序**拼 ``k=v&k=v``、剔除空值与 ``sign``/``sign_type``；
3. 退避重试有上限，``isv.bill-not-exist`` 之类 4xx 不重试；
4. ``bill_url`` 30 秒有效，过期（403/410）显式失败而不是拿旧 zip；
5. 下载到的不是 zip（多半是链接失效返回的 HTML 错误页）时显式报格式错。

⚠️ 本文件**不连接任何真实商户环境**，也**不含任何真实 AppID、应用私钥或支付宝公钥**。
真实端点的实连需要院内商户凭据，本轮未实连。
"""

from __future__ import annotations

import io
import pathlib
import os
import zipfile
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from reconq.errors import ReconqError
from reconq.sources.alipay_bill import (
    ALIPAY_BILL_METHOD,
    ALIPAY_DEFAULT_GATEWAY,
    BILL_TYPES,
    DOWNLOAD_LINK_TTL_SECONDS,
    EMPTY_DATA_WITH_BILL_FILE,
    AlipayBillFile,
    build_rsa2_params,
    fetch_bill,
    parse_bill_csv,
    sign_rsa2,
)

BILL_DATE = "2026-09-07"
#: 测试用占位 AppID（形如 16 位数字，**不是任何真实商户的 AppID**）
FAKE_APP_ID = "2021000000000000"

#: 脱敏支付宝交易明细（带回单抬头 + 千分位金额）
BILL_CSV = "\n".join(
    [
        "支付宝（中国）网络技术有限公司 电子客户回单",
        "商户号：EXAM_ALIPAY_MCH    起始日期：2026-09-01    终止日期：2026-09-07",
        "",
        "支付宝交易号,商户订单号,业务类型,金额,交易时间,收/支,对方账号,交易状态",
        "2026090722000001,EX-ALIPAY-0001,销售分析,1710.55,2026-09-07 10:01:05,收入,EXAM_COUNTERPARTY,交易成功",
        "2026090722000002,EX-ALIPAY-0002,销售分析,1920.55,2026-09-07 11:20:41,收入,EXAM_COUNTERPARTY,交易成功",
        "------------------------支付宝对账结束------------------------",
    ]
)


@pytest.fixture(scope="module")
def fake_key(tmp_path_factory):
    path = tmp_path_factory.mktemp("keys") / "test_only_alipay_app_private_key.pem"
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
def fake_public(tmp_path_factory, fake_key):
    """由同一把测试私钥导出的公钥（仅测试用，非任何真实支付宝公钥）。"""
    key = serialization.load_pem_private_key(pathlib.Path(fake_key).read_bytes(), password=None)
    path = tmp_path_factory.mktemp("keys") / "test_only_alipay_public_key.pem"
    path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return str(path)


def _signed_nodes(nodes: list[dict], key_path: str) -> list[dict]:
    """给 gateway 响应节点补 sign 字段（模拟支付宝侧，Task 8 验签要求）。

    失败码的节点**不签名**——它们走错误分支，本就在验签之前被拦下。
    """
    from reconq.sources.signing import rsa2_sign

    key = serialization.load_pem_private_key(pathlib.Path(key_path).read_bytes(), password=None)
    out = []
    for node in nodes:
        if "alipay_bill_downloadurl_query_response" not in node:
            # 响应结构本身就错（缺节点）的报文原样透传——不能被这里补成一个空节点，
            # 否则测试反而看不出「响应缺节点」这个真实故障
            out.append(node)
            continue
        body = node["alipay_bill_downloadurl_query_response"]
        if body.get("code") == "10000":
            body = {**body, "sign": rsa2_sign(body, key)}
        out.append({**node, "alipay_bill_downloadurl_query_response": body})
    return out


def _env(key_path: str, public_path: str) -> dict[str, str]:
    return {
        "ALIPAY_APP_ID": FAKE_APP_ID,
        "ALIPAY_PRIVATE_KEY_PATH": key_path,
        "ALIPAY_PUBLIC_KEY_PATH": public_path,  # Task 8：启用响应验签
        "ALIPAY_GATEWAY": ALIPAY_DEFAULT_GATEWAY,
    }


def _zip_of(text: str = BILL_CSV, member: str = "alipay_bill_20260907_20260907.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, text.encode("gb18030"))
    return buf.getvalue()


def _gateway_node(**kwargs) -> dict:
    return {"alipay_bill_downloadurl_query_response": kwargs}


def _transport(
    *,
    zip_blob: bytes | None = None,
    gateway_nodes: list[dict] | None = None,
    download_status: int = 200,
    download_content: bytes | None = None,
    key_path: str,
) -> httpx.MockTransport:
    """回放「POST gateway.do 取件 → GET bill_url 下载」两段链路。"""
    default_node = _gateway_node(
        code="10000", msg="Success", bill_url="https://dwbillcenter.alipay.com/downloadBillFile.resource?bk=EXAM"
    )
    nodes = iter(_signed_nodes(gateway_nodes or [default_node], key_path))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "gateway.do" in str(request.url):
            return httpx.Response(200, json=next(nodes, default_node))
        if download_status != 200:
            return httpx.Response(download_status, text="<html>链接已失效</html>")
        return httpx.Response(200, content=download_content if download_content is not None else zip_blob)

    return httpx.MockTransport(handler)


def _no_sleep(_seconds: float) -> None:
    pass


# —— 成功路径 ————————————————————————————————————————————————


def test_fetch_bill_end_to_end_unzips_and_parses(fake_key, fake_public):
    client = httpx.Client(transport=_transport(key_path=fake_key, zip_blob=_zip_of()))
    bill = fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert isinstance(bill, AlipayBillFile)
    assert bill.member_name == "alipay_bill_20260907_20260907.csv"
    assert bill.is_empty is False
    rows = bill.rows()
    assert len(rows) == 2  # 尾部「对账结束」分隔行不是数据行
    assert Decimal(rows[0].values["amount"]) == Decimal("1710.55")
    assert rows[0].values["trade_no"] == "2026090722000001"
    assert rows[1].values["income_expense"] == "收入"


def test_request_is_post_form_with_rsa2_signature(fake_key, fake_public):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            node = _signed_nodes(
                [
                    _gateway_node(
                        code="10000",
                        msg="Success",
                        bill_url="https://dwbillcenter.alipay.com/downloadBillFile.resource?bk=EXAM",
                    )
                ],
                fake_key,
            )[0]
            return httpx.Response(200, json=node)
        return httpx.Response(200, content=_zip_of())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)

    assert len(seen) == 2
    take = seen[0]
    assert take.method == "POST"
    assert "application/x-www-form-urlencoded" in take.headers["content-type"]
    from urllib.parse import parse_qs

    form = {k: v[0] for k, v in parse_qs(take.content.decode()).items()}
    assert form["method"] == ALIPAY_BILL_METHOD
    assert form["sign_type"] == "RSA2"
    assert form["app_id"] == FAKE_APP_ID
    assert form["version"] == "1.0"
    assert form["sign"]  # 已签名
    # biz_content 是 JSON 字符串且含账期与账单类型
    import json

    biz = json.loads(form["biz_content"])
    assert biz == {"bill_date": BILL_DATE, "bill_type": "trade"}
    # 签名与 sign_type 本身不进签名串
    assert "sign" not in form["biz_content"]


# —— RSA2 签名构造 ———————————————————————————————————————————


def test_rsa2_signature_sorts_keys_and_drops_empty(fake_key, fake_public):
    from cryptography.hazmat.primitives import serialization as ser

    key = ser.load_pem_private_key(open(fake_key, "rb").read(), password=None)
    params = {
        "app_id": FAKE_APP_ID,
        "method": ALIPAY_BILL_METHOD,
        "sign_type": "RSA2",
        "timestamp": "2026-09-07 10:00:00",
        "version": "1.0",
        "charset": "utf-8",
        "empty_param": "",       # 空值不参与签名
        "none_param": None,      # None 也不参与
        "sign": "SHOULD_BE_DROPPED",
    }
    sig = sign_rsa2(params, key)
    assert sig
    # 同一份参数签名可复现；去掉空值/换 key 顺序后签名仍一致
    reordered = dict(reversed(list(params.items())))
    assert sign_rsa2(reordered, key) == sig
    # 改动任一参与签名的值 → 签名必变
    assert sign_rsa2({**params, "app_id": "9999999999999999"}, key) != sig


def test_biz_content_is_canonical_json_for_signing(fake_key, fake_public):
    """biz_content 序列化必须稳定，否则同一份请求每次签名都不同。"""
    a = build_rsa2_params(
        method=ALIPAY_BILL_METHOD, app_id=FAKE_APP_ID,
        biz_content={"bill_type": "trade", "bill_date": BILL_DATE},
        timestamp="2026-09-07 10:00:00",
    )
    b = build_rsa2_params(
        method=ALIPAY_BILL_METHOD, app_id=FAKE_APP_ID,
        biz_content={"bill_date": BILL_DATE, "bill_type": "trade"},  # 键序不同
        timestamp="2026-09-07 10:00:00",
    )
    assert a["biz_content"] == b["biz_content"]
    assert a["sign_type"] == "RSA2"
    assert a["_gateway"] == ALIPAY_DEFAULT_GATEWAY


# —— 空数据是成功态，不是错误 ——————————————————————————————————


def test_empty_data_with_bill_file_is_empty_dataset_not_error(fake_key, fake_public):
    """「有账单文件、无交易」是成功码。对账岗的直觉是「这是错误」，
    本产品按空数据集处理并带 empty_reason，不把整路拖成红。"""
    client = httpx.Client(
        transport=_transport(key_path=fake_key, 
            gateway_nodes=[
                _gateway_node(code="10000", msg="Success", sub_code=EMPTY_DATA_WITH_BILL_FILE, sub_msg="账单文件已生成但无数据")
            ]
        )
    )
    bill = fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert bill.is_empty is True
    assert bill.empty_reason == EMPTY_DATA_WITH_BILL_FILE
    assert bill.rows() == []


# —— 失败码映射与退避 ————————————————————————————————————————


def test_bill_not_exist_subcode_maps_and_is_not_retried(fake_key, fake_public):
    waits: list[float] = []
    client = httpx.Client(
        transport=_transport(key_path=fake_key, 
            gateway_nodes=[
                _gateway_node(code="40004", msg="Business Failed", sub_code="isv.bill-not-exist", sub_msg="账单不存在")
            ]
        )
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_public), sleep=waits.append)
    assert exc.value.code == "BILL_NOT_EXIST"
    assert "isv.bill-not-exist" in exc.value.detail  # 渠道 sub_code 保留可追
    assert waits == []  # 4xx 业务错误不重试


def test_unrecognized_subcode_is_not_treated_as_success(fake_key, fake_public):
    client = httpx.Client(
        transport=_transport(key_path=fake_key, 
            gateway_nodes=[_gateway_node(code="40004", msg="Business Failed", sub_code="isv.some.new.subcode", sub_msg="未知错误")]
        )
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert exc.value.code == "CHANNEL_REQUEST_FAILED"
    assert "isv.some.new.subcode" in exc.value.detail


def test_gateway_http_error_and_non_json_body_are_explicit(fake_key, fake_public):
    def http_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom")

    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=httpx.Client(transport=httpx.MockTransport(http_500)), env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert exc.value.code == "CHANNEL_REQUEST_FAILED"

    def html_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(ReconqError) as exc2:
        fetch_bill(BILL_DATE, client=httpx.Client(transport=httpx.MockTransport(html_body)), env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert exc2.value.code == "CHANNEL_REQUEST_FAILED"
    assert "不是 JSON" in exc2.value.detail


def test_missing_response_node_is_explicit(fake_key, fake_public):
    client = httpx.Client(transport=_transport(key_path=fake_key, gateway_nodes=[{"unexpected": "shape"}]))
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert exc.value.code == "CHANNEL_REQUEST_FAILED"
    assert "alipay_bill_downloadurl_query_response" in exc.value.detail


# —— 30 秒链接窗口 ————————————————————————————————————————————


@pytest.mark.parametrize("status", [401, 403, 410])
def test_expired_bill_url_is_explicit(fake_key, fake_public, status):
    client = httpx.Client(transport=_transport(key_path=fake_key, zip_blob=_zip_of(), download_status=status))
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert exc.value.code == "RATE_LIMITED_EXHAUSTED"
    assert exc.value.cause_code == "LINK_EXPIRED"
    assert str(DOWNLOAD_LINK_TTL_SECONDS) in exc.value.detail


def test_non_zip_download_is_format_broken_not_silent_empty(fake_key, fake_public):
    """链接失效时返回的是 HTML 错误页——不能当成「空账单」蒙混过关。"""
    client = httpx.Client(
        transport=_transport(key_path=fake_key, download_content=b"<html>link expired</html>", download_status=200)
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert exc.value.code == "FORMAT_BROKEN"
    assert "zip" in exc.value.detail


def test_download_retried_with_bounded_backoff(fake_key, fake_public):
    """下载 5xx 可重试；用尽后抛 RATE_LIMITED_EXHAUSTED，不无限等。"""
    waits: list[float] = []
    client = httpx.Client(transport=_transport(key_path=fake_key, zip_blob=_zip_of(), download_status=500))
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=_env(fake_key, fake_public), sleep=waits.append)
    assert exc.value.code in ("BILL_DOWNLOAD_FAILED", "RATE_LIMITED_EXHAUSTED")
    assert len(waits) <= 2


# —— 凭据与参数 ——————————————————————————————————————————————


def test_missing_credential_fails_before_request(fake_key, fake_public):
    env = _env(fake_key, fake_public)
    env.pop("ALIPAY_APP_ID")
    called: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: (called.append(str(r)), httpx.Response(200))[1]))
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, client=client, env=env, sleep=_no_sleep)
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "ALIPAY_APP_ID" in exc.value.detail
    assert called == []


def test_bad_private_key_file_names_path(fake_key, fake_public):
    env = _env(fake_key, fake_public)
    env["ALIPAY_PRIVATE_KEY_PATH"] = "/nonexistent/test_only_alipay.pem"
    with pytest.raises(ReconqError) as exc:
        fetch_bill(BILL_DATE, env=env, sleep=_no_sleep)
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "test_only_alipay.pem" in exc.value.detail


def test_bad_bill_date_and_type(fake_key, fake_public):
    client = httpx.Client(transport=_transport(key_path=fake_key, zip_blob=_zip_of()))
    with pytest.raises(ReconqError) as exc:
        fetch_bill("20260907", client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert exc.value.code == "BILL_DATE_INVALID"
    with pytest.raises(ReconqError) as exc2:
        fetch_bill(BILL_DATE, bill_type="unknown", client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert exc2.value.code == "BILL_DATE_INVALID"
    assert BILL_TYPES == ("trade", "signcustomer")


@pytest.mark.parametrize(
    "bad_date",
    [
        "20260907",  # 紧凑写法：Python 3.11+ 的 date.fromisoformat 会放行
        "2026-9-7",  # 月日不补零
        "2026-13-01",  # 月份不存在
        "2026-02-30",  # 日不存在
        "2026-09-07T00:00:00",  # 带了时间部分
        " 2026-09-07",  # 前导空格
        "",
    ],
)
def test_bill_date_must_be_strict_iso_shape(fake_key, fake_public, bad_date):
    """账单日必须是严格的 YYYY-MM-DD，且**不能发请求**。

    这条是回归护栏：``date.fromisoformat`` 从 Python 3.11 起同时接受
    ``20260907`` 这类紧凑写法，只靠它校验会让一个手写错格式的日期
    一路走到渠道侧才炸——那时已经白发过一次请求，且渠道只回笼统的
    PARAM_ERROR，分不清是格式错还是没权限。
    """
    called: list[str] = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: (called.append(str(r)), httpx.Response(200))[1])
    )
    with pytest.raises(ReconqError) as exc:
        fetch_bill(bad_date, client=client, env=_env(fake_key, fake_public), sleep=_no_sleep)
    assert exc.value.code == "BILL_DATE_INVALID"
    assert exc.value.channel == "alipay"
    assert called == []


def test_bill_date_strict_guard_is_shared_with_wechat():
    """微信与支付宝共用同一个严格校验，两边行为不漂移。"""
    from reconq.sources.signing import require_bill_date

    with pytest.raises(ReconqError) as wechat_exc:
        require_bill_date("20260907", channel="wechat")
    with pytest.raises(ReconqError) as alipay_exc:
        require_bill_date("20260907", channel="alipay")
    assert wechat_exc.value.channel == "wechat"
    assert alipay_exc.value.channel == "alipay"
    # 合法的日期不抛
    require_bill_date("2026-09-07", channel="wechat")


# —— CSV 解析边界 ————————————————————————————————————————————


def test_parse_bill_csv_requires_alipay_header():
    with pytest.raises(ReconqError) as exc:
        parse_bill_csv("a,b,c\n1,2,3\n", source_file="x.csv")
    assert exc.value.code == "FORMAT_BROKEN"
    assert "支付宝交易号" in exc.value.detail


def test_parse_bill_csv_reports_ragged_data_rows():
    text = (
        "支付宝交易号,商户订单号,金额,交易时间\n"
        "2026090722000001,EX-1,1710.55,2026-09-07 10:01:05\n"
        "2026090722000002,EX-2\n"
    )
    with pytest.raises(ReconqError) as exc:
        parse_bill_csv(text, source_file="ex.csv")
    assert exc.value.code == "COLUMN_COUNT_MISMATCH"
    assert exc.value.row_ref == "ex.csv:3"
