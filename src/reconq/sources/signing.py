"""微信支付 APIv3 与支付宝 RSA2 的签名构造、凭据装载与响应验签（Task 8）。

本模块把两条渠道的**协议级**部分从取数客户端里剥出来单独放，因为它们是
「签名对了才有资格拿到账单」的那一层，与「拿到账单之后怎么解析」无关。
两条协议在四个点上完全不同，**不要互相复用**：

===================  ==========================  ==============================
维度                 微信支付 APIv3              支付宝开放平台 RSA2
===================  ==========================  ==============================
请求方法             GET（query 串）              POST（表单）
签名串构造           固定顺序拼 **五行**          参数按 key **字典序**拼 ``k=v&``
被排除的参数         无（body 为空串也要占一行）  ``sign``/``sign_type``/空值
时间戳格式           Unix 秒（UTC）               ``YYYY-MM-DD HH:MM:SS`` 本地时
随机串               nonce_str，每次请求随机      无
摘要算法             SHA256withRSA                MD5withRSA（见下方说明）
===================  ==========================  ==============================

**为什么把摘要算法做成可切换常量**：支付宝官方 Java/Python SDK 的 RSA2 用的是
``SHA1withRSA``，而院内不少老接入沿用 ``MD5withRSA``。本产品按任务契约默认
``MD5``，同时把算法做成具名常量 ``AlipayDigest``，接入时按院方商户实际配置
切换，而不是把算法写死在分支里。

**凭据纪律（三条不可让步）**

1. **只从环境变量读**。本模块不读配置文件、不读命令行明文参数。
2. **缺位即拒，且点名是哪个变量**。「一个请求都不发出去」比「发出去了再
   看渠道报什么错」更省时间——微信对缺 ``serial_no`` 的返回是笼统的
   ``SIGN_ERROR``，支付宝是 ``isv.invalid-signature``，两边都不告诉你缺了啥。
3. **验签失败绝不当作成功**。微信对 ``download_url`` 响应、支付宝对 gateway
   响应都要验签；验不过就是拿到了被中间人改过的内容，宁可这一路标未取到。

⚠️ 本模块**不连接任何真实商户环境**。测试用本地自签密钥（``tests/fixtures/keys/``），
该目录下的 PEM 由 ``tests/fixtures/keys/generate_test_pems.py`` 生成，
**不是任何真实商户的私钥或平台证书**，也不要在任何真实环境复用。
"""

from __future__ import annotations

import base64
import hashlib
from datetime import date
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from ..errors import ReconqError

__all__ = [
    "AlipayDigest",
    "WechatCredentials",
    "AlipayCredentials",
    "WECHAT_REQUIRED_VARS",
    "ALIPAY_REQUIRED_VARS",
    "wechat_sign_message",
    "wechat_signature",
    "build_authorization_header",
    "verify_wechat_response",
    "alipay_sign_content",
    "rsa2_sign",
    "rsa2_verify",
    "verify_alipay_response",
    "load_private_key",
    "load_public_key",
    "require_credentials",
    "build_wechat_credentials",
    "build_alipay_credentials",
    "require_bill_date",
]


# ---------------------------------------------------------------- 微信 APIv3

#: 调用微信商户账单接口前必须齐备的凭据位。缺任意一个都**不发请求**。
WECHAT_REQUIRED_VARS = (
    "WECHAT_MCH_ID",
    "WECHAT_SERIAL_NO",
    "WECHAT_API_V3_KEY",
    "WECHAT_PRIVATE_KEY_PATH",
)

#: 响应验签需要平台证书/公钥。缺它时**只做签名不做验签**会被调用方显式拒绝，
#: 因为「能签名但不能验签」意味着我们分不清渠道的真假响应。
WECHAT_PLATFORM_CERT_VARS = ("WECHAT_PLATFORM_CERT_PATH",)


def wechat_sign_message(
    *,
    method: str,
    url_path: str,
    body: str,
    timestamp: int,
    nonce: str,
) -> str:
    """拼微信 V3 的待签名消息串（五行，行尾各带一个 ``\\n``）。

    官方口径是 ``HTTP方法\\nURL(含 query)\\n时间戳\\n随机串\\n请求报文主体\\n``。
    五行**一行都不能少**：GET 请求的 body 为空串，那一行仍要留着，
    少一个换行签名就对不上，而渠道只回一句 ``SIGN_ERROR``。

    ``url_path`` 必须是**含 query 的完整路径**（如
    ``/v3/bill/tradebill?bill_date=2026-09-07&bill_type=ALL&tar_type=GZIP``），
    漏掉 query 是另一个高频踩坑点。
    """
    method = method.strip().upper()
    if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        raise ReconqError(
            "CREDENTIAL_MISSING",
            f"微信签名串的 HTTP 方法非法：{method!r}",
            channel="wechat",
        )
    if "\n" in body:
        raise ReconqError(
            "CREDENTIAL_MISSING",
            "微信签名串的 body 含换行，无法按官方口径拼成五行；"
            "JSON body 请确认序列化后没有裸换行",
            channel="wechat",
        )
    return f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"


def wechat_signature(message: str, private_key) -> str:
    """对微信 V3 签名串做 SHA256withRSA，返回 base64 签名。

    注意与 ``rsa2_sign`` 的差别：微信签的是五行的 **SHA256 摘要**，
    支付宝签的是 ``k=v&k=v`` 串**本身**。
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    digest = hashlib.sha256(message.encode("utf-8")).digest()
    signature = private_key.sign(digest, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[arg-type]
    return base64.b64encode(signature).decode("ascii")


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
    """构造 ``Authorization: WECHATPAY2-SHA256-RSA2048 ...`` 头。

    ``timestamp`` / ``nonce`` 可显式传入（测试要可复现）；不传则取本机
    时间戳与一个密码学随机串。**nonce 每次请求都不同**——复用 nonce 会让
    同一台商户对同一份请求产生可被重放的签名。
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    rnd = nonce if nonce is not None else secrets.token_hex(16)
    message = wechat_sign_message(
        method=method, url_path=url_path, body=body, timestamp=ts, nonce=rnd
    )
    signature = wechat_signature(message, private_key)
    return (
        "WECHATPAY2-SHA256-RSA2048 "
        f'mchid="{mch_id}",nonce_str="{rnd}",timestamp="{ts}",'
        f'serial_no="{serial_no}",signature="{signature}"'
    )


def verify_wechat_response(
    *,
    headers: Mapping[str, str],
    body: bytes,
    platform_public_key,
    expected_serial_no: Optional[str] = None,
) -> None:
    """按微信平台证书验签响应；验不过抛 ``RESPONSE_SIGNATURE_INVALID``。

    微信的响应签名是 ``时间戳\\n随机串\\n报文主体\\n``，用
    ``Wechatpay-Signature`` 头里的值、以平台证书公钥做 SHA256withRSA 校验。
    三个 ``Wechatpay-*`` 头缺任意一个都不放行——少一个就没法验，
    静默跳过验签等于把中间人改过的 ``download_url`` 当成真的。
    """
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    signature = lowered.get("wechatpay-signature", "").strip()
    timestamp = lowered.get("wechatpay-timestamp", "").strip()
    nonce = lowered.get("wechatpay-nonce", "").strip()
    serial_no = lowered.get("wechatpay-serial", "").strip()

    missing = [
        name
        for name, val in (
            ("Wechatpay-Signature", signature),
            ("Wechatpay-Timestamp", timestamp),
            ("Wechatpay-Nonce", nonce),
            ("Wechatpay-Serial", serial_no),
        )
        if not val
    ]
    if missing:
        raise ReconqError(
            "RESPONSE_SIGNATURE_INVALID",
            f"微信响应缺少验签所需的头：{'、'.join(missing)}；"
            f"缺头时无法验签，本产品拒绝把未验签的 download_url 当成真的",
            channel="wechat",
        )
    if expected_serial_no and serial_no != expected_serial_no:
        raise ReconqError(
            "RESPONSE_SIGNATURE_INVALID",
            f"微信响应的 Wechatpay-Serial={serial_no!r} 与配置的 "
            f"WECHAT_PLATFORM_CERT_SERIAL_NO={expected_serial_no!r} 不一致，"
            f"拒绝按未知平台证书验签",
            channel="wechat",
        )

    text_body = body.decode("utf-8", errors="replace")
    message = f"{timestamp}\n{nonce}\n{text_body}\n"
    if not _rsa_verify_sha256(message, signature, platform_public_key):
        raise ReconqError(
            "RESPONSE_SIGNATURE_INVALID",
            "微信响应验签失败：Wechatpay-Signature 与平台证书不匹配，"
            "响应内容可能在传输中被改动；本产品拒绝使用该响应取账单",
            channel="wechat",
        )


def _rsa_verify_sha256(message: str, signature_b64: str, public_key) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except Exception:  # noqa: BLE001 - base64 本身坏了也算验签失败
        return False
    digest = hashlib.sha256(message.encode("utf-8")).digest()
    try:
        public_key.verify(raw, digest, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[arg-type]
    except InvalidSignature:
        return False
    return True


# ---------------------------------------------------------------- 支付宝 RSA2


class AlipayDigest:
    """支付宝 RSA2 的摘要算法具名常量。

    默认 ``MD5``（任务契约口径，与院内多数老接入一致）；官方 SDK 的实现是
    ``SHA1``。做成常量是为了让「按院方商户实际配置切换」成为一行配置，
    而不是散落在代码分支里的算法选择。
    """

    MD5 = "MD5"
    SHA1 = "SHA1"


#: 调用支付宝取件接口前必须齐备的凭据位。
ALIPAY_REQUIRED_VARS = (
    "ALIPAY_APP_ID",
    "ALIPAY_PRIVATE_KEY_PATH",
    "ALIPAY_PUBLIC_KEY_PATH",
)


def alipay_sign_content(
    params: Mapping[str, object],
    *,
    exclude: Iterable[str] = ("sign", "sign_type"),
    extra_exclude: Iterable[str] = ("_gateway",),
) -> str:
    """按 RSA2 口径拼待签名字符串：按 key 字典序拼 ``k=v&k=v``。

    规则（官方口径）：

    1. 剔除 ``sign`` 与 ``sign_type`` 本身；
    2. 剔除空值——``None`` 与**空串都剔**（空串参与签名是最高频的
       「本机验签通过、渠道验签失败」原因，因为两边对「空值」的定义不一致）；
    3. 剩余参数按 key **升序**排列；
    4. 用 ``&`` 连接，值不做 URL 编码。

    ``_gateway`` 是本产品自己塞进参数表以便调用方复用配置的私有键，
    不参与签名——见 ``alipay_bill.build_rsa2_params``。
    """
    drop = set(exclude) | set(extra_exclude)
    items = [
        (k, v)
        for k, v in params.items()
        if k not in drop and v is not None and str(v) != ""
    ]
    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)


def _hashes_for(digest: str):
    from cryptography.hazmat.primitives import hashes

    upper = str(digest).upper()
    if upper == "MD5":
        return hashes.MD5()
    if upper == "SHA1":
        return hashes.SHA1()
    raise ReconqError(
        "CREDENTIAL_MISSING",
        f"支付宝 RSA2 摘要算法只支持 MD5 / SHA1，实得 {digest!r}",
        channel="alipay",
    )


def rsa2_sign(
    params: Mapping[str, object],
    private_key,
    *,
    digest: str = AlipayDigest.MD5,
) -> str:
    """对参数表做 RSA2 签名，返回 base64 签名。

    与微信的 ``wechat_signature`` **完全不同**：这里签的是
    ``k=v&k=v`` 串本身（不是它的 SHA256 摘要），微信那边签的是五行的
    SHA256 摘要。混用会得到一个「本地看着像签名、渠道一律拒绝」的结果。
    """
    from cryptography.hazmat.primitives.asymmetric import padding

    content = alipay_sign_content(params)
    signature = private_key.sign(  # type: ignore[arg-type]
        content.encode("utf-8"), padding.PKCS1v15(), _hashes_for(digest)
    )
    return base64.b64encode(signature).decode("ascii")


def rsa2_verify(
    params: Mapping[str, object],
    signature_b64: str,
    public_key,
    *,
    digest: str = AlipayDigest.MD5,
) -> bool:
    """用支付宝公钥验签；返回布尔（不抛），由调用方决定怎么归一错误码。"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except Exception:  # noqa: BLE001
        return False
    content = alipay_sign_content(params)
    try:
        public_key.verify(raw, content.encode("utf-8"), padding.PKCS1v15(), _hashes_for(digest))  # type: ignore[arg-type]
    except InvalidSignature:
        return False
    return True


def verify_alipay_response(
    node: Mapping[str, object],
    alipay_public_key,
    *,
    digest: str = AlipayDigest.MD5,
) -> None:
    """按支付宝口径验签 gateway 响应节点；验不过抛 ``RESPONSE_SIGNATURE_INVALID``。

    验签对象是响应节点里**除 ``sign`` 外**的全部参数（与请求侧同规则：
    剔空、字典序、``&`` 连接）。``sign_type`` 不在响应节点里，因此不需要额外剔除。

    **成功码也必须验**：``code=10000`` 只说明支付宝受理了，不代表返回给我们的
    ``bill_url`` 没被改过。
    """
    signature = str(node.get("sign") or "").strip()
    if not signature:
        raise ReconqError(
            "RESPONSE_SIGNATURE_INVALID",
            "支付宝响应节点里没有 sign 字段，无法验签；"
            "本产品拒绝把未验签的 bill_url 当成真的",
            channel="alipay",
        )
    if not rsa2_verify(node, signature, alipay_public_key, digest=digest):
        raise ReconqError(
            "RESPONSE_SIGNATURE_INVALID",
            "支付宝响应验签失败：sign 与 ALIPAY_PUBLIC_KEY_PATH 指定的公钥不匹配，"
            "响应内容可能在传输中被改动；本产品拒绝使用该响应取账单",
            channel="alipay",
        )


# ------------------------------------------------------------ 凭据装载与校验


def require_credentials(
    env: Mapping[str, str],
    names: Iterable[str],
    *,
    channel: str,
    context: str,
) -> dict[str, str]:
    """一次性点出**全部**缺失的凭据位。

    逐个报错会让人「补一个再跑一次、又报下一个」，四轮下来才跑通一次请求。
    本函数把缺的位一次列全，并原样报出变量名。
    """
    missing = [name for name in names if not str(env.get(name, "") or "").strip()]
    if missing:
        raise ReconqError(
            "CREDENTIAL_MISSING",
            f"{context}前缺少凭据位：{'、'.join(missing)}；"
            f"凭据只从环境变量读，缺位时一个请求都不会发出",
            channel=channel,
        )
    return {name: str(env[name]) for name in names}


def _read_pem(path: str, *, env_var: str, channel: str) -> bytes:
    if not os.path.isfile(path):
        raise ReconqError(
            "CREDENTIAL_MISSING",
            f"{env_var} 指向的密钥文件不存在：{path}",
            channel=channel,
        )
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ReconqError(
            "CREDENTIAL_MISSING",
            f"{env_var} 指向的密钥文件不可读：{path}（{type(exc).__name__}: {exc}）",
            channel=channel,
        ) from exc


def load_private_key(path: str, *, env_var: str, channel: str):
    """装载 PKCS#8/PKCS#1 私钥 PEM；路径不存在或 PEM 坏掉都点名 ``env_var``。"""
    from cryptography.hazmat.primitives import serialization

    pem = _read_pem(path, env_var=env_var, channel=channel)
    for loader in (serialization.load_pem_private_key,):
        try:
            return loader(pem, password=None)
        except Exception as exc:  # noqa: BLE001 - 任何 PEM 解析失败都归为凭据问题
            raise ReconqError(
                "CREDENTIAL_MISSING",
                f"{env_var} 指向的私钥 PEM 解析失败：{path}"
                f"（{type(exc).__name__}: {exc}）",
                channel=channel,
            ) from exc


def load_public_key(path: str, *, env_var: str, channel: str):
    """装载验签用的公钥：X.509 平台证书或裸 SubjectPublicKeyInfo 公钥。

    两条渠道给的**不是同一种文件**：

    - ``WECHAT_PLATFORM_CERT_PATH`` 指向**平台证书**（``BEGIN CERTIFICATE``，
      X.509 容器，公钥在证书体内）；
    - ``ALIPAY_PUBLIC_KEY_PATH`` 指向**裸公钥**（``BEGIN PUBLIC KEY``）。

    两种都接受，按「先证书后公钥」的顺序试；两种都不成立才报错，
    并在报错里点名该配哪种文件——院方把这两个变量配反是高频故障。
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    pem = _read_pem(path, env_var=env_var, channel=channel)

    try:
        return x509.load_pem_x509_certificate(pem).public_key()
    except Exception:  # noqa: BLE001 - 不是证书就试裸公钥
        pass
    try:
        return serialization.load_pem_public_key(pem)
    except Exception as exc:  # noqa: BLE001
        raise ReconqError(
            "CREDENTIAL_MISSING",
            f"{env_var} 指向的文件既不是 X.509 证书也不是公钥 PEM：{path}"
            f"（{type(exc).__name__}: {exc}）；"
            f"微信的 WECHAT_PLATFORM_CERT_PATH 应指向平台证书（BEGIN CERTIFICATE），"
            f"支付宝的 ALIPAY_PUBLIC_KEY_PATH 应指向公钥（BEGIN PUBLIC KEY）",
            channel=channel,
        ) from exc


@dataclass(frozen=True)
class WechatCredentials:
    """微信商户账单接口的齐备凭据。构造成功即意味着「可以发请求了」。"""

    mch_id: str
    serial_no: str
    api_v3_key: str
    private_key: object
    #: 平台证书公钥；缺它时本产品拒绝验签，因而拒绝使用该渠道的响应
    platform_public_key: Optional[object] = None
    platform_serial_no: str = ""


@dataclass(frozen=True)
class AlipayCredentials:
    """支付宝开放平台的齐备凭据。"""

    app_id: str
    private_key: object
    public_key: object
    gateway: str = ""
    digest: str = AlipayDigest.MD5


def build_wechat_credentials(
    env: Optional[Mapping[str, str]] = None,
    *,
    require_platform_cert: bool = True,
) -> WechatCredentials:
    """从环境变量装配微信凭据；缺位**一次列全**且不发出任何请求。

    ``require_platform_cert=True``（默认）时，缺平台证书也拒——因为没有它
    就验不了签，验不了签就分不清渠道的真假响应。
    """
    source = os.environ if env is None else env
    values = require_credentials(
        source, WECHAT_REQUIRED_VARS, channel="wechat", context="调用微信账单接口"
    )
    cert_path = str(source.get("WECHAT_PLATFORM_CERT_PATH", "") or "").strip()
    if require_platform_cert and not cert_path:
        raise ReconqError(
            "CREDENTIAL_MISSING",
            "调用微信账单接口前缺少凭据位：WECHAT_PLATFORM_CERT_PATH；"
            "没有平台证书就无法验签，本产品不提供「只签名不验签」模式",
            channel="wechat",
        )

    private_key = load_private_key(
        values["WECHAT_PRIVATE_KEY_PATH"],
        env_var="WECHAT_PRIVATE_KEY_PATH",
        channel="wechat",
    )
    platform_public_key = None
    if cert_path:
        platform_public_key = load_public_key(
            cert_path, env_var="WECHAT_PLATFORM_CERT_PATH", channel="wechat"
        )
    return WechatCredentials(
        mch_id=values["WECHAT_MCH_ID"],
        serial_no=values["WECHAT_SERIAL_NO"],
        api_v3_key=values["WECHAT_API_V3_KEY"],
        private_key=private_key,
        platform_public_key=platform_public_key,
        platform_serial_no=str(source.get("WECHAT_PLATFORM_CERT_SERIAL_NO", "") or "").strip(),
    )


def build_alipay_credentials(env: Optional[Mapping[str, str]] = None) -> AlipayCredentials:
    """从环境变量装配支付宝凭据；缺位一次列全且不发出任何请求。"""
    source = os.environ if env is None else env
    values = require_credentials(
        source, ALIPAY_REQUIRED_VARS, channel="alipay", context="调用支付宝账单接口"
    )
    return AlipayCredentials(
        app_id=values["ALIPAY_APP_ID"],
        private_key=load_private_key(
            values["ALIPAY_PRIVATE_KEY_PATH"],
            env_var="ALIPAY_PRIVATE_KEY_PATH",
            channel="alipay",
        ),
        public_key=load_public_key(
            values["ALIPAY_PUBLIC_KEY_PATH"],
            env_var="ALIPAY_PUBLIC_KEY_PATH",
            channel="alipay",
        ),
        gateway=str(source.get("ALIPAY_GATEWAY", "") or ""),
        digest=str(source.get("ALIPAY_SIGN_DIGEST", "") or AlipayDigest.MD5).upper(),
    )


# ------------------------------------------------------------ 账单日参数校验

#: 账单日必须是这个形状：四位年、两位月、两位日，且真实存在。
#:
#: **不能只靠** ``date.fromisoformat``：Python 3.11 起它同时接受紧凑写法
#: ``20260907`` 与其它 ISO 变体，于是 ``bill_date=20260907`` 能悄悄通过本地
#: 校验，直到渠道侧回一个 PARAM_ERROR 才暴露——那时已经白发过一次请求，
#: 排障时还分不清是「日期写错」还是「账号没有该账单日的权限」。
#: 本地先把形状钉死，错误就留在本地，出错时点名是哪个参数、实得什么。
_BILL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def require_bill_date(bill_date: str, *, channel: str) -> None:
    """校验账单日为严格的 ``YYYY-MM-DD``；不合法抛 ``BILL_DATE_INVALID``。"""
    if not _BILL_DATE_RE.match(bill_date or ""):
        raise ReconqError(
            "BILL_DATE_INVALID",
            f"bill_date 须为 YYYY-MM-DD，实得 {bill_date!r}",
            channel=channel,
        )
    try:
        date.fromisoformat(bill_date)
    except ValueError as exc:
        raise ReconqError(
            "BILL_DATE_INVALID",
            f"bill_date 不是真实存在的日期：{bill_date!r}",
            channel=channel,
        ) from exc
