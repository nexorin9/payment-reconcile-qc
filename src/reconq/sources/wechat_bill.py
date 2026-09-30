"""微信支付商户账单取数（真实协议，Task 5）。

协议链路（官方文档：``GET /v3/bill/tradebill``）::

    GET https://api.mch.weixin.qq.com/v3/bill/tradebill
        ?bill_date=2026-09-07
        &bill_type=ALL|SUCCESS|REFUND
        &tar_type=GZIP
    Authorization: WECHATPAY2-SHA256-RSA2048 mchid=...,nonce_str=...,
                   timestamp=...,serial_no=...,signature=...
    Wechatpay-Serial: <平台证书序列号>

    200 → { "hash_type": "SHA1", "hash": "<40 hex>",
            "download_url": "https://api.mch.weixin.qq.com/v3/bill/..." }
        download_url **5 分钟有效**，且账单以 APIv3 密钥 AES-256-GCM 加密
    400 → { "code": "BILL_NOT_EXIST" | "STATEMENT_CREATING" | "INVALID_REQUEST", ... }
    429 → { "code": "FREQUENCY_LIMITED" }

三条不可让步的纪律
------------------
1. **取得即下载**。``download_url`` 是取件凭证不是永久地址；本模块
   ``fetch_bill()`` 内部取件→下载→校验→解密一气呵成，不把 URL 交给上层
   存着（上层存了就会在周一来用时发现链接早过期了）。
2. **SHA1 不符不入库**。拿到的是哪个字节就用哪个字节，``hash`` 对不上就
   显式失败重取——**宁可这一路标"本期未取到"，也不能拿半截文件去对账**。
3. **解密失败显式报**。``AES-256-GCM`` 的 key 是 APIv3 密钥，密钥错了
   不会报错、只会解出乱码；本模块把 tag 校验失败直接变成
   ``BILL_DECRYPT_FAILED``，让信息科一眼看出是密钥问题而不是文件坏了。

Task 8 在此之上把签名串构造与凭据装载补到协议级深度（验签、密钥文件装载、
凭据位缺失前置校验）。本任务先让**取件→校验→解密**这条主路径端到端成立。
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import os
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

import httpx

from ..engine.checks import BillRow
from ..errors import ReconqError
from .inbox_files import BILL_HEADER_ALIASES, parse_amount
from .retry import DEFAULT_MAX_ATTEMPTS, retry_call

__all__ = [
    "WECHAT_API_BASE",
    "BILL_TYPES",
    "DOWNLOAD_LINK_TTL_SECONDS",
    "WechatBillFile",
    "build_authorization_header",
    "fetch_bill",
    "parse_bill_csv",
    "decrypt_bill",
]

#: 微信支付商户平台 APIv3 根地址（沙箱与生产同域，靠凭据区分）
WECHAT_API_BASE = "https://api.mch.weixin.qq.com"

#: 账单类型。``ALL`` 是对账口径要的：只取 SUCCESS 会漏掉退款行，
#: 只取 REFUND 拿不到正向流水——**单边账最大的来源就是只取了一种类型**。
BILL_TYPES = ("ALL", "SUCCESS", "REFUND")

#: download_url 有效期（官方口径 5 分钟）。Task 9 把它做成可注入时钟的
#: 窗口对象；本任务先落常量并写进超时配置。
DOWNLOAD_LINK_TTL_SECONDS = 300

#: 错误码 → 显式错误码的映射。渠道侧的 code 保留在 detail 里，
#: 排障时能直接拿去问微信。
_ERROR_CODE_MAP = {
    "BILL_NOT_EXIST": "BILL_NOT_EXIST",
    "STATEMENT_CREATING": "STATEMENT_CREATING",
    "FREQUENCY_LIMITED": "FREQUENCY_LIMITED",
    "NO_AUTH": "CREDENTIAL_MISSING",
    "SIGN_ERROR": "CREDENTIAL_MISSING",
    "APPID_MCHID_NOT_MATCH": "CREDENTIAL_MISSING",
    "INVALID_REQUEST": "CHANNEL_REQUEST_FAILED",
    "PARAM_ERROR": "BILL_DATE_INVALID",
    "INVALID_TRANSACTIONID": "CHANNEL_REQUEST_FAILED",
}

#: 微信账单表头 → 契约列名。与 inbox_files 的别名表同源，
#: 但方向相反：那边是「契约列名 → 院内导出表头」，这边是
#: 「微信交易明细表头 → 契约列名」。
_WECHAT_HEADER_TO_COLUMN: dict[str, str] = {
    "交易时间": "success_time",
    "商户订单号": "out_trade_no",
    "微信交易单号": "transaction_id",
    "用户标识": "payer",
    "交易状态": "trade_state",
    "应结订单金额": "amount",
    "退款金额": "refund",
    "退款类型": "refund_type",
}


@dataclass
class WechatBillFile:
    """一份取到并校验过的微信账单。"""

    bill_date: str
    bill_type: str
    #: 已解密的 CSV 文本
    text: str
    #: 渠道声明并已校验通过的 SHA1
    sha1: str
    #: 降级后的 gzip 层级（正常 1 层）
    gzip_layers: int = 0
    source_url: str = ""
    #: 取件响应是否已按平台证书验签通过。False 表示调用方显式选择了
    #: ``verify_response=False``（排查用），结论单与面板需据此提示。
    response_verified: bool = False

    def rows(self) -> list[BillRow]:
        return parse_bill_csv(self.text, source_file=f"wechat_tradebill_{self.bill_date}.csv")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if value == "":
        raise ReconqError(
            "CREDENTIAL_MISSING",
            f"环境变量 {name} 未设置或为空，无法调用微信商户账单接口",
            channel="wechat",
        )
    return value


def build_authorization_header(
    *,
    method: str,
    url_path: str,
    body: str,
    mch_id: str,
    serial_no: str,
    private_key,
    timestamp: Optional[int] = None,
    nonce: Optional[str] = None,
) -> str:
    """构造 ``Authorization: WECHATPAY2-SHA256-RSA2048 ...``（转发到 signing）。

    实现已上移到 :mod:`reconq.sources.signing`（Task 8），本函数保留为
    兼容入口：签名串构造、nonce 生成、响应验签都在那儿，本模块只管
    「取件→下载→校验→解密」这条数据主路径。
    """
    from .signing import build_authorization_header as _impl

    return _impl(
        method=method,
        url_path=url_path,
        body=body,
        mch_id=mch_id,
        serial_no=serial_no,
        private_key=private_key,
        timestamp=timestamp,
        nonce=nonce,
    )


def _map_channel_error(status: int, payload: dict, *, context: str) -> ReconqError:
    code = str(payload.get("code") or payload.get("sub_code") or "").strip()
    message = str(payload.get("message") or payload.get("sub_msg") or "").strip()
    mapped = _ERROR_CODE_MAP.get(code, "CHANNEL_REQUEST_FAILED")
    detail = f"{context}：HTTP {status}，渠道 code={code or '（无）'}，message={message or '（无）'}"
    if mapped == "CHANNEL_REQUEST_FAILED" and code not in _ERROR_CODE_MAP:
        detail += f"；未识别的渠道 code 未被静默当作成功"
    return ReconqError(mapped, detail, channel="wechat")


def _load_private_key(path: str):
    """读商户私钥 PEM。密钥文件不可读/解析失败都显式点名路径。"""
    if not os.path.isfile(path):
        raise ReconqError(
            "CREDENTIAL_MISSING",
            f"WECHAT_PRIVATE_KEY_PATH 指向的私钥文件不存在：{path}",
            channel="wechat",
        )
    with open(path, "rb") as handle:
        pem = handle.read()
    try:
        from cryptography.hazmat.primitives import serialization

        return serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:  # noqa: BLE001 - 任何 PEM 解析失败都归为凭据问题
        raise ReconqError(
            "CREDENTIAL_MISSING",
            f"商户私钥 PEM 解析失败：{path}（{type(exc).__name__}: {exc}）",
            channel="wechat",
        ) from exc


def decrypt_bill(blob: bytes, api_v3_key: str) -> bytes:
    """解密微信账单文件（AES-256-GCM）。

    格式：``nonce(12B) || ciphertext || tag(16B)``，AAD 为空。解密后可能还是
    gzip（``tar_type=GZIP`` 时），调用方继续解。**GCM tag 校验失败即抛
    ``BILL_DECRYPT_FAILED``** —— 密钥错了不会得到半截明文，只会被拒绝。
    """
    if len(api_v3_key) != 32:
        raise ReconqError(
            "BILL_DECRYPT_FAILED",
            f"APIv3 密钥长度应为 32 字符，实得 {len(api_v3_key)}；"
            f"长度不对通常是复制时带了空格或换行",
            channel="wechat",
        )
    if len(blob) < 12 + 16:
        raise ReconqError(
            "BILL_DECRYPT_FAILED",
            f"账单密文长度 {len(blob)} 字节，短于 nonce+tag 的最小长度，文件多半没下全",
            channel="wechat",
        )
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(api_v3_key.encode("utf-8")).decrypt(blob[:12], blob[12:], None)
    except Exception as exc:  # noqa: BLE001 - InvalidTag 等统一归为解密失败
        raise ReconqError(
            "BILL_DECRYPT_FAILED",
            f"AES-256-GCM 解密失败（多为 WECHAT_API_V3_KEY 与商户不一致）：{type(exc).__name__}",
            channel="wechat",
        ) from exc


def _gunzip_layers(blob: bytes) -> tuple[bytes, int]:
    """按层解 gzip；最多 3 层，超过报格式错（防被人为套娃拖死）。"""
    layers = 0
    while blob[:2] == b"\x1f\x8b" and layers < 3:
        try:
            blob = gzip.decompress(blob)
        except OSError as exc:
            raise ReconqError(
                "FORMAT_BROKEN",
                f"账单 gzip 解压失败（第 {layers + 1} 层）：{exc}",
                channel="wechat",
            ) from exc
        layers += 1
    if blob[:2] == b"\x1f\x8b":
        raise ReconqError("FORMAT_BROKEN", "账单 gzip 层数超过 3 层，拒绝继续解压", channel="wechat")
    return blob, layers


def parse_bill_csv(text: str, *, source_file: str) -> list[BillRow]:
    """把微信交易明细 CSV 解析成带行号的 ``BillRow`` 列表。

    微信账单前几行是 ``微信支付账单明细列表`` 之类的抬头与分隔线，
    表头在**第一个含「交易时间」的行**。这里做的是「找表头」而不是
    「假设第一行就是表头」——账单文件带抬头是常态。
    """
    raw_rows = list(csv.reader(io.StringIO(text)))
    header_idx: Optional[int] = None
    for idx, row in enumerate(raw_rows[:20]):
        normalized = [c.strip().lstrip("﻿") for c in row]
        if "交易时间" in normalized:
            header_idx = idx
            break
    if header_idx is None:
        raise ReconqError(
            "FORMAT_BROKEN",
            f"账单文件前 20 行里找不到含「交易时间」的表头行；文件={source_file}",
            channel="wechat",
        )

    header = [c.strip().lstrip("﻿") for c in raw_rows[header_idx]]
    positions: dict[str, int] = {}
    for pos, name in enumerate(header):
        column = _WECHAT_HEADER_TO_COLUMN.get(name)
        if column:
            positions[column] = pos
    width = len(header)

    rows: list[BillRow] = []
    for line_no, raw in enumerate(raw_rows[header_idx + 1 :], start=header_idx + 2):
        if not raw or all(not c.strip() for c in raw):
            continue
        if len(raw) != width:
            raise ReconqError(
                "COLUMN_COUNT_MISMATCH",
                f"第 {line_no} 行列数与表头不一致：表头 {width} 列，本行 {len(raw)} 列；文件={source_file}",
                channel="wechat",
                row_ref=f"{source_file}:{line_no}",
            )
        values: dict[str, str] = {}
        for column, pos in positions.items():
            cell = raw[pos].strip()
            if column in ("amount", "refund") and cell:
                values[column] = str(parse_amount(cell, source_file=source_file, row_no=line_no, column=column))
            else:
                values[column] = cell
        rows.append(
            BillRow(
                values=values,
                row_no=line_no,
                raw_line=",".join(raw),
                source_file=source_file,
            )
        )
    return rows


def fetch_bill(
    bill_date: str,
    *,
    bill_type: str = "ALL",
    client: Optional[httpx.Client] = None,
    env: Optional[dict[str, str]] = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: Callable[[], float] = time.time,
    verify_response: bool = True,
) -> WechatBillFile:
    """取一个账单日的微信账单文件：取件 → 立即下载 → 校验 SHA1 → 解密。

    ``client`` 可注入（测试用 ``httpx.MockTransport`` 回放报文），不传则
    按真实协议新建一个客户端。凭据全部从环境变量读，测试只走占位值。

    ``verify_response=True``（默认）时按 ``WECHAT_PLATFORM_CERT_PATH`` 指定的
    平台证书验签取件响应；验不过抛 ``RESPONSE_SIGNATURE_INVALID``，不拿未验签的
    ``download_url`` 去下载。设为 ``False`` 需要调用方显式承担这个责任
    （仅用于排查「平台证书还没配好」的情形，会在结果里标 ``response_verified=False``）。
    """
    from .signing import require_bill_date

    if bill_type not in BILL_TYPES:
        raise ReconqError(
            "BILL_DATE_INVALID",
            f"bill_type 只能是 {'/'.join(BILL_TYPES)}，实得 {bill_type!r}",
            channel="wechat",
        )
    require_bill_date(bill_date, channel="wechat")

    source = env if env is not None else os.environ
    # 凭据装配与缺位前置校验已上移到 signing.build_wechat_credentials：
    # 缺位一次列全，一个请求都不发出；验签公钥缺失时不静默降级。
    from .signing import build_wechat_credentials, verify_wechat_response

    credentials = build_wechat_credentials(source, require_platform_cert=verify_response)
    mch_id = credentials.mch_id
    serial_no = credentials.serial_no
    api_v3_key = credentials.api_v3_key
    private_key = credentials.private_key

    url_path = "/v3/bill/tradebill"
    query = f"bill_date={bill_date}&bill_type={bill_type}&tar_type=GZIP"
    full_url = f"{WECHAT_API_BASE}{url_path}?{query}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "reconq/0.1 (payment-reconcile-qc)",
    }
    headers["Authorization"] = build_authorization_header(
        method="GET",
        url_path=f"{url_path}?{query}",
        body="",
        mch_id=mch_id,
        serial_no=serial_no,
        private_key=private_key,
    )
    headers["Wechatpay-Serial"] = serial_no

    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0))
    try:
        def _take() -> httpx.Response:
            resp = http.get(full_url, headers=headers)
            if resp.status_code != 200:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {}
                raise _map_channel_error(resp.status_code, payload, context="取件接口 /v3/bill/tradebill")
            return resp

        resp = retry_call(_take, channel="wechat", max_attempts=max_attempts, sleep=sleep)
        # —— 响应验签：不过签就不用这个 download_url（Task 8）——
        response_verified = False
        if verify_response and credentials.platform_public_key is not None:
            verify_wechat_response(
                headers=resp.headers,
                body=resp.content,
                platform_public_key=credentials.platform_public_key,
                expected_serial_no=credentials.platform_serial_no or None,
            )
            response_verified = True
        try:
            payload = resp.json()
        except ValueError as exc:
            # HTTP 200 但正文不是 JSON（院方网关代理或老接入兼容层偶尔会
            # 吐回 V2 的 XML 信封）。**不能让它按空账单走成功路径**——那一路
            # 合计会静默归零，结论单却照样显示为绿。显式失败，由编排层把
            # 该渠道标为本期未取到。
            raise ReconqError(
                "CHANNEL_REQUEST_FAILED",
                f"取件接口返回 HTTP 200 但正文不是 JSON（{type(exc).__name__}）："
                f"{resp.text[:200]}；拒绝按「今天没有账单」处理",
                channel="wechat",
            ) from exc
        download_url = str(payload.get("download_url") or "")
        declared_sha1 = str(payload.get("hash") or "").strip().lower()
        hash_type = str(payload.get("hash_type") or "SHA1").upper()
        if not download_url:
            raise ReconqError(
                "CHANNEL_REQUEST_FAILED",
                "取件响应里没有 download_url，拒绝按「无账单」处理",
                channel="wechat",
            )
        if hash_type != "SHA1" or len(declared_sha1) != 40:
            raise ReconqError(
                "CHANNEL_REQUEST_FAILED",
                f"取件响应的 hash_type/hash 不符合预期：hash_type={hash_type!r}，hash 长度={len(declared_sha1)}",
                channel="wechat",
            )

        # —— 取得即下载：链接 5 分钟有效，不缓存、不交给上层存着 ——
        def _download() -> bytes:
            dl = http.get(download_url, headers={"Accept": "*/*"})
            if dl.status_code in (401, 403, 410):
                raise ReconqError(
                    "LINK_EXPIRED",
                    f"下载链接已失效（HTTP {dl.status_code}）；"
                    f"微信 download_url 有效期约 {DOWNLOAD_LINK_TTL_SECONDS} 秒，需重新取件",
                    channel="wechat",
                )
            if dl.status_code != 200:
                raise ReconqError(
                    "BILL_DOWNLOAD_FAILED",
                    f"账单文件下载失败：HTTP {dl.status_code}",
                    channel="wechat",
                )
            return dl.content

        blob = retry_call(_download, channel="wechat", max_attempts=max_attempts, sleep=sleep)

        actual_sha1 = hashlib.sha1(blob).hexdigest().lower()
        if actual_sha1 != declared_sha1:
            raise ReconqError(
                "SHA1_MISMATCH",
                f"账单文件 SHA1 校验不符：渠道声明 {declared_sha1}，实得 {actual_sha1}"
                f"（{len(blob)} 字节）。拒绝入库，本期该渠道标未取到",
                channel="wechat",
            )

        plain = decrypt_bill(blob, api_v3_key)
        plain, layers = _gunzip_layers(plain)
        try:
            text = plain.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ReconqError(
                "BILL_ENCODING_UNRESOLVED",
                f"解密后的账单不是 UTF-8：{exc}",
                channel="wechat",
            ) from exc
        return WechatBillFile(
            bill_date=bill_date,
            bill_type=bill_type,
            text=text,
            sha1=actual_sha1,
            gzip_layers=layers,
            source_url=download_url,
            response_verified=response_verified,
        )
    finally:
        if owns_client:
            http.close()
