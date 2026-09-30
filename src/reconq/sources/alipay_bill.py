"""支付宝账单取数（真实协议，Task 5）。

协议链路（开放平台 ``alipay.data.dataservice.bill.downloadurl.query``）::

    POST https://openapi.alipay.com/gateway.do
    Content-Type: application/x-www-form-urlencoded
      method=alipay.data.dataservice.bill.downloadurl.query
      app_id=2021...&charset=utf-8&sign_type=RSA2
      timestamp=2026-09-07 10:00:00&version=1.0
      biz_content={"bill_type":"trade","bill_date":"2026-09-07"}
      sign=<RSA2 签名>

    200 → { "alipay_bill_downloadurl_query_response": {
              "code": "10000", "sub_code": "EMPTY_DATA_WITH_BILL_FILE",
              "bill_url": "https://dwbillcenter.alipay.com/downloadBillFile.resource?..." } }

支付宝与微信的四处关键差异（本模块逐条按对方口径实现，不套用微信的逻辑）：

1. **取件是 POST + 表单**，不是 GET + query；
2. **签名是 RSA2**：全部非空参数按 key **字典序**拼 ``k=v&k=v``、
   剔除 ``sign``/``sign_type``、空值不参与，私钥 ``MD5withRSA`` 签名；
3. **链接 30 秒有效**（微信是 5 分钟），且返回的是 **zip** 包不是加密流；
4. ``EMPTY_DATA_WITH_BILL_FILE`` 是**成功码不是错误**：意思是「这天有账单文件
   但一笔交易都没有」。对账岗的直觉是「这是错误」，本产品**按空数据集处理**并
   在结果里带 ``empty_reason``，绝不报失败把整路拖成红。
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

import httpx

from ..engine.checks import BillRow
from ..errors import ReconqError
from .inbox_files import parse_amount
from .retry import DEFAULT_MAX_ATTEMPTS, retry_call

__all__ = [
    "ALIPAY_DEFAULT_GATEWAY",
    "ALIPAY_BILL_METHOD",
    "BILL_TYPES",
    "DOWNLOAD_LINK_TTL_SECONDS",
    "EMPTY_DATA_WITH_BILL_FILE",
    "AlipayBillFile",
    "build_rsa2_params",
    "sign_rsa2",
    "fetch_bill",
    "parse_bill_csv",
]

#: 开放平台默认网关（沙箱与生产同域，靠 key 区分）
ALIPAY_DEFAULT_GATEWAY = "https://openapi.alipay.com/gateway.do"

#: 取件接口名
ALIPAY_BILL_METHOD = "alipay.data.dataservice.bill.downloadurl.query"

#: 账单类型：trade（交易）、signcustomer（客户账单）
BILL_TYPES = ("trade", "signcustomer")

#: bill_url 有效期（官方口径 30 秒）
DOWNLOAD_LINK_TTL_SECONDS = 30

#: 「有账单文件但无数据」——成功码，按空数据集处理
EMPTY_DATA_WITH_BILL_FILE = "EMPTY_DATA_WITH_BILL_FILE"

#: 支付宝 sub_code → 本产品错误码。未识别的 code 一律 CHANNEL_REQUEST_FAILED，
#: **不被静默当作成功**。
_SUB_CODE_MAP = {
    "ACQ.INVALID_PARAMETER": "BILL_DATE_INVALID",
    "isv.bill-not-exist": "BILL_NOT_EXIST",
    "isv.bill-error": "CHANNEL_REQUEST_FAILED",
    "isv.invalid-app-id": "CREDENTIAL_MISSING",
    "isv.invalid-signature": "CREDENTIAL_MISSING",
    "isv.no-permission": "CREDENTIAL_MISSING",
    "isv.bill-date-error": "BILL_DATE_INVALID",
}

#: 支付宝交易明细表头 → 契约列名
_ALIPAY_HEADER_TO_COLUMN: dict[str, str] = {
    "支付宝交易号": "trade_no",
    "商户订单号": "out_trade_no",
    "业务类型": "biz_type",
    "金额": "amount",
    "交易时间": "trade_time",
    "收/支": "income_expense",
    "交易状态": "trade_status",
    "对方账号": "counterparty",
}


@dataclass
class AlipayBillFile:
    """一份取到并解包好的支付宝账单。"""

    bill_date: str
    bill_type: str
    #: 已解包（zip → csv）后的 CSV 文本
    text: str
    #: zip 包内成员名，异常时排障用
    member_name: str = ""
    #: 空数据时给出原因（``EMPTY_DATA_WITH_BILL_FILE``），非空为 ""
    empty_reason: str = ""
    source_url: str = ""
    #: gateway 响应是否已按支付宝公钥验签通过（Task 8）
    response_verified: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def rows(self) -> list[BillRow]:
        if self.is_empty:
            return []
        return parse_bill_csv(self.text, source_file=self.member_name or f"alipay_bill_{self.bill_date}.csv")


def _load_private_key(path: str, *, channel: str = "alipay"):
    """装载支付宝应用私钥（转发到 signing 的协议级装载，Task 8）。"""
    from .signing import load_private_key as _load

    return _load(path, env_var="ALIPAY_PRIVATE_KEY_PATH", channel=channel)


def _loose_alipay_credentials(source: dict[str, str]):
    """``verify_response=False`` 时的凭据装配：只要求签名必需的两位。

    这条腿是**排查用**的：院方平台证书/支付宝公钥还没配好时，需要先把
    签名跑通看报文长什么样。它产出的结果 ``response_verified=False``，
    面板与结论单会据此提示「本期该路未经响应验签」。
    """
    from .signing import AlipayCredentials, AlipayDigest, require_credentials

    values = require_credentials(
        source,
        ("ALIPAY_APP_ID", "ALIPAY_PRIVATE_KEY_PATH"),
        channel="alipay",
        context="调用支付宝账单接口（未启用响应验签）",
    )
    return AlipayCredentials(
        app_id=values["ALIPAY_APP_ID"],
        private_key=_load_private_key(values["ALIPAY_PRIVATE_KEY_PATH"]),
        public_key=None,  # type: ignore[arg-type]
        gateway=str(source.get("ALIPAY_GATEWAY", "") or ""),
        digest=str(source.get("ALIPAY_SIGN_DIGEST", "") or AlipayDigest.MD5).upper(),
    )


def build_rsa2_params(
    *,
    method: str,
    app_id: str,
    biz_content: dict,
    timestamp: str,
    gateway: str = ALIPAY_DEFAULT_GATEWAY,
    version: str = "1.0",
    charset: str = "utf-8",
) -> dict[str, str]:
    """组装**待签名**的公共参数（``sign`` 字段尚未填）。

    ``biz_content`` 会被 JSON 序列化成字符串再进签名——这是 RSA2 最常见的
    踩坑点：序列化成不同的 JSON（键序、空格）签名就对不上。
    """
    import json

    return {
        "app_id": app_id,
        "biz_content": json.dumps(biz_content, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "charset": charset,
        "method": method,
        "sign_type": "RSA2",
        "timestamp": timestamp,
        "version": version,
        # gateway 不参与签名，但保留在返回值里方便调用方复用同一份配置
        "_gateway": gateway,
    }


def sign_rsa2(params: dict[str, str], private_key, *, digest: str = "MD5") -> str:
    """按 RSA2 口径签名（转发到 signing 的协议级实现，Task 8）。

    字典序拼接与剔空规则见 :func:`reconq.sources.signing.rsa2_sign`。
    """
    from .signing import rsa2_sign

    return rsa2_sign(params, private_key, digest=digest)


#: 支付宝账单尾部的对账结束分隔行（整行是一个单元格，以破折号包住）
_FOOTER_SEPARATORS = "-—–=＝*·• "


def _is_footer_line(raw: list[str]) -> bool:
    """判断是不是账单尾部的「对账结束」分隔行而非数据行。

    支付宝交易明细的**最后一行固定是**
    ``------------------------支付宝对账结束------------------------``——
    整行一个单元格、长度远超表头宽度。它不是脏行，把它当脏行报出去会让
    每一期扫描都以 ``COLUMN_COUNT_MISMATCH`` 收场；把它当数据行又会在
    结论单里多出一笔 0 元流水。判据：**单列**且首尾去分隔符后含
    「对账结束/交易明细/共 N 笔」这类结束语。
    """
    if len(raw) != 1:
        return False
    text = raw[0].strip().strip(_FOOTER_SEPARATORS)
    if not text:
        return True  # 纯分隔线
    return any(marker in text for marker in ("对账结束", "交易明细", "本页合计", "共"))


def parse_bill_csv(text: str, *, source_file: str) -> list[BillRow]:
    """解析支付宝交易明细 CSV。

    支付宝账单同样带抬头（``支付宝（中国）网络技术有限公司  电子客户回单``），
    且**表头前面常有一行 ``------------------------支付宝（中国）网络技术有限公司
    电子客户回单...``**。本实现找第一行含「支付宝交易号」或「商户订单号」
    的行当表头，而不是假设第一行就是表头。
    """
    raw_rows = list(csv.reader(io.StringIO(text)))
    header_idx: Optional[int] = None
    for idx, row in enumerate(raw_rows[:20]):
        normalized = [c.strip().lstrip("﻿") for c in row]
        if "支付宝交易号" in normalized or "商户订单号" in normalized:
            header_idx = idx
            break
    if header_idx is None:
        raise ReconqError(
            "FORMAT_BROKEN",
            f"账单文件前 20 行里找不到含「支付宝交易号」/「商户订单号」的表头行；文件={source_file}",
            channel="alipay",
        )

    header = [c.strip().lstrip("﻿") for c in raw_rows[header_idx]]
    positions: dict[str, int] = {}
    for pos, name in enumerate(header):
        column = _ALIPAY_HEADER_TO_COLUMN.get(name)
        if column:
            positions[column] = pos
    width = len(header)

    rows: list[BillRow] = []
    for line_no, raw in enumerate(raw_rows[header_idx + 1 :], start=header_idx + 2):
        if not raw or all(not c.strip() for c in raw):
            continue
        # 尾部「对账结束」分隔行不是数据行，但它**列数不等于表头**，
        # 当脏行丢掉会让每一期扫描都失败；当数据行又会在结论单里多出一笔
        # 0 元流水。判据见 _is_footer_line。
        joined = ",".join(raw)
        if len(raw) != width:
            if _is_footer_line(raw):
                continue
            raise ReconqError(
                "COLUMN_COUNT_MISMATCH",
                f"第 {line_no} 行列数与表头不一致：表头 {width} 列，本行 {len(raw)} 列；"
                f"原始行={joined[:160]!r}；文件={source_file}",
                channel="alipay",
                row_ref=f"{source_file}:{line_no}",
            )
        values: dict[str, str] = {}
        for column, pos in positions.items():
            cell = raw[pos].strip()
            if column == "amount" and cell:
                values[column] = str(parse_amount(cell, source_file=source_file, row_no=line_no, column=column))
            else:
                values[column] = cell
        rows.append(
            BillRow(values=values, row_no=line_no, raw_line=joined, source_file=source_file)
        )
    return rows


def _extract_zip_member(blob: bytes, *, bill_date: str) -> tuple[str, bytes]:
    """从 zip 包里取账单 CSV。支付宝的 ``bill_url`` 下载到的是 zip。"""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            members = [n for n in zf.namelist() if not n.endswith("/")]
            csv_members = [n for n in members if n.lower().endswith((".csv", ".txt"))]
            pick = csv_members or members
            if not pick:
                raise ReconqError(
                    "FORMAT_BROKEN",
                    f"账单 zip 包内没有可用成员：{members}",
                    channel="alipay",
                )
            name = pick[0]
            return name, zf.read(name)
    except zipfile.BadZipFile as exc:
        raise ReconqError(
            "FORMAT_BROKEN",
            f"账单下载内容不是合法 zip（多半是链接失效返回的 HTML 错误页）：{exc}",
            channel="alipay",
        ) from exc


def fetch_bill(
    bill_date: str,
    *,
    bill_type: str = "trade",
    client: Optional[httpx.Client] = None,
    env: Optional[dict[str, str]] = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    verify_response: bool = True,
) -> AlipayBillFile:
    """取一个账单日的支付宝账单：POST 取件 → 立即下载 zip → 解包。

    ``EMPTY_DATA_WITH_BILL_FILE`` 走**成功**路径并返回空数据集，
    ``empty_reason`` 标出原因，不抛错。

    ``verify_response=True``（默认）时按 ``ALIPAY_PUBLIC_KEY_PATH`` 验签
    gateway 响应；验不过抛 ``RESPONSE_SIGNATURE_INVALID``，不拿未验签的
    ``bill_url`` 去下载。
    """
    from .signing import require_bill_date

    if bill_type not in BILL_TYPES:
        raise ReconqError(
            "BILL_DATE_INVALID",
            f"bill_type 只能是 {'/'.join(BILL_TYPES)}，实得 {bill_type!r}",
            channel="alipay",
        )
    require_bill_date(bill_date, channel="alipay")

    source = env if env is not None else os.environ
    # 凭据装配与缺位前置校验已上移到 signing.build_alipay_credentials：
    # ALIPAY_APP_ID / ALIPAY_PRIVATE_KEY_PATH / ALIPAY_PUBLIC_KEY_PATH
    # 缺任一位都一次列全，且一个请求都不发出。
    from .signing import build_alipay_credentials, verify_alipay_response

    credentials = build_alipay_credentials(source) if verify_response else _loose_alipay_credentials(source)
    app_id = credentials.app_id
    gateway = credentials.gateway or ALIPAY_DEFAULT_GATEWAY

    params = build_rsa2_params(
        method=ALIPAY_BILL_METHOD,
        app_id=app_id,
        biz_content={"bill_type": bill_type, "bill_date": bill_date},
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        gateway=gateway,
    )
    sign_params = {k: v for k, v in params.items() if k != "_gateway"}
    sign_params["sign"] = sign_rsa2(sign_params, credentials.private_key, digest=credentials.digest)
    form = {k: v for k, v in sign_params.items() if k != "sign"} | {"sign": sign_params["sign"]}

    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0))
    try:
        def _take() -> dict:
            resp = http.post(
                gateway,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            )
            if resp.status_code != 200:
                raise ReconqError(
                    "CHANNEL_REQUEST_FAILED",
                    f"支付宝网关返回 HTTP {resp.status_code}：{resp.text[:200]}",
                    channel="alipay",
                )
            try:
                body = resp.json()
            except ValueError as exc:
                raise ReconqError(
                    "CHANNEL_REQUEST_FAILED",
                    f"支付宝网关响应不是 JSON：{resp.text[:200]}",
                    channel="alipay",
                ) from exc
            node = body.get("alipay_bill_downloadurl_query_response")
            if not isinstance(node, dict):
                raise ReconqError(
                    "CHANNEL_REQUEST_FAILED",
                    f"支付宝响应缺少 alipay_bill_downloadurl_query_response 节点：{str(body)[:200]}",
                    channel="alipay",
                )
            return node

        node = retry_call(_take, channel="alipay", max_attempts=max_attempts, sleep=sleep)

        code = str(node.get("code") or "")
        sub_code = str(node.get("sub_code") or "")
        msg = str(node.get("msg") or node.get("sub_msg") or "")

        # —— 响应验签（Task 8）——
        # 只在**成功分支**验：失败分支我们只用它的 code/sub_code 去归一错误码，
        # 不会拿它的 bill_url 或任何业务字段，验签对失败信封没有额外保护价值。
        # 成功分支不一样——那里的 bill_url 会被立刻拿去下载账单，验签是硬要求。
        response_verified = False
        if code == "10000" and verify_response and credentials.public_key is not None:
            verify_alipay_response(node, credentials.public_key, digest=credentials.digest)
            response_verified = True

        if sub_code == EMPTY_DATA_WITH_BILL_FILE and code == "10000":
            # 「有文件、无数据」是成功态。返回空数据集并带上原因。
            return AlipayBillFile(
                bill_date=bill_date,
                bill_type=bill_type,
                text="",
                empty_reason=EMPTY_DATA_WITH_BILL_FILE,
                response_verified=response_verified,
            )
        if code != "10000" or sub_code:
            mapped = _SUB_CODE_MAP.get(sub_code, "CHANNEL_REQUEST_FAILED")
            raise ReconqError(
                mapped,
                f"支付宝取件失败：code={code or '（无）'}，sub_code={sub_code or '（无）'}，"
                f"msg={msg or '（无）'}",
                channel="alipay",
            )

        bill_url = str(node.get("bill_url") or "")
        if not bill_url:
            raise ReconqError(
                "CHANNEL_REQUEST_FAILED",
                "支付宝取件成功但响应里没有 bill_url",
                channel="alipay",
            )

        # —— 取得即下载：bill_url 只有 30 秒有效 ——
        def _download() -> bytes:
            dl = http.get(bill_url, headers={"Accept": "*/*"})
            if dl.status_code in (401, 403, 410):
                raise ReconqError(
                    "LINK_EXPIRED",
                    f"bill_url 已失效（HTTP {dl.status_code}）；"
                    f"支付宝 bill_url 有效期约 {DOWNLOAD_LINK_TTL_SECONDS} 秒，需重新取件",
                    channel="alipay",
                )
            if dl.status_code != 200:
                raise ReconqError(
                    "BILL_DOWNLOAD_FAILED",
                    f"支付宝账单下载失败：HTTP {dl.status_code}",
                    channel="alipay",
                )
            return dl.content

        blob = retry_call(_download, channel="alipay", max_attempts=max_attempts, sleep=sleep)
        member_name, payload = _extract_zip_member(blob, bill_date=bill_date)
        try:
            text = payload.decode("gb18030")
        except UnicodeDecodeError:
            text = payload.decode("utf-8", errors="replace")
        return AlipayBillFile(
            bill_date=bill_date,
            bill_type=bill_type,
            text=text.lstrip("﻿"),
            member_name=member_name,
            source_url=bill_url,
            response_verified=response_verified,
        )
    finally:
        if owns_client:
            http.close()
