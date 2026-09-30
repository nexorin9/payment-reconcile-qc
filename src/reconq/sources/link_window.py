"""账单下载链接的有效期窗口管理（Task 9）。

为什么这件事值得单独一个模块
----------------------------
``download_url`` / ``bill_url`` **不是网址，是取件凭证**。两条渠道的口径
差了一个数量级：

============  ====================  ==========================================
渠道          官方有效期            过期后会发生什么
============  ====================  ==========================================
微信支付      300 秒（5 分钟）       403 / 410，下载到的是错误页不是账单
支付宝        30 秒                  30 秒——刚解完 zip 包名就已经到期
============  ====================  ==========================================

把它们当普通 URL 缓存起来、周一再来下载，是这类系统最典型也最贵的错误：
**拿一份"看起来是 CSV 的东西"去对账**。财务科不会当场发现，因为金额看起来
仍是大致对的；发现时已经是下个月对不上账、且没有任何一条日志说过什么。

四条纪律
--------
1. **取得即下载**。链接拿到手就用，不缓存、不交给上层存着。
2. **过期不发请求**。``is_expired(now)`` 为真时直接抛 ``LINK_EXPIRED``，
   一个字节都不往渠道发——用过期链接换来的只会是错误页。
3. **链接一次性**。同一个 ``url`` 第二次被使用即报错。把取件凭证当稳定
   地址复用，是"缓存了旧账单"的另一种写法。
4. **恢复路径是重新取件，不是重试旧链接**。``fetch_with_recovery`` 在链接
   失效时**重新走取件接口拿一条新链接**（退避额度内最多 1 次），仍失败才把
   该渠道本期标为未取到，并如实报出 ``LINK_EXPIRED``——不静默降级、
   不拿残缺内容凑数。

时钟可注入
----------
``now`` 全部是参数而不是 :func:`time.time` 的直接调用，测试用假时钟跑
完整的过期边界与恢复序列，不必真的等 30 秒。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..errors import ReconqError

__all__ = [
    "CHANNEL_LINK_TTL_SECONDS",
    "DEFAULT_MIN_BILL_BYTES",
    "DownloadLink",
    "LinkUseRegistry",
    "LinkFetchOutcome",
    "fetch_with_window",
    "fetch_with_recovery",
    "extract_bill_dates",
]

#: 各渠道 download_url / bill_url 的官方有效期（秒）。
#: 取自：微信支付 APIv3「download_url 有效期 5 分钟」、
#: 支付宝开放平台「bill_url 有效期 30 秒」。
CHANNEL_LINK_TTL_SECONDS: dict[str, int] = {
    "wechat": 300,
    "alipay": 30,
}

#: 账单正文的最小字节数下限。低于此值一律判内容不符——
#: 错误页、网关拦截页、连接被重置后的空响应体都远小于一个真实账单。
DEFAULT_MIN_BILL_BYTES = 64

_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


# --------------------------------------------------------------------------
# 链接对象
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DownloadLink:
    """一条取件凭证及其有效期窗口。

    字段顺序按「什么时候拿到的 / 是什么 / 什么时候到期 / 谁的」排，
    构造时按位置传参也不容易传错。
    """

    recorded_at: float
    url: str
    expires_at: float
    channel: str
    #: 取件时请求的账单日（YYYY-MM-DD），用于内容二次校验；可空。
    bill_date: str = ""

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def issue(
        cls,
        channel: str,
        url: str,
        *,
        recorded_at: float,
        bill_date: str = "",
        ttl_seconds: Optional[int] = None,
    ) -> "DownloadLink":
        """在 ``recorded_at`` 时刻取得一条链接，按渠道口径算出到期时刻。

        ``ttl_seconds`` 可显式覆盖（用于测试造边界）；生产路径**必须**
        走渠道默认口径，不允许运行期把有效期调长。
        """
        if not url:
            raise ReconqError(
                "CHANNEL_REQUEST_FAILED",
                f"渠道 {channel} 返回了空的下载链接，拒绝按「无账单」处理",
                channel=channel,
            )
        if ttl_seconds is None:
            if channel not in CHANNEL_LINK_TTL_SECONDS:
                raise ReconqError(
                    "CHANNEL_UNKNOWN",
                    f"渠道 {channel!r} 没有登记下载链接有效期；"
                    f"已登记的渠道为 {'/'.join(sorted(CHANNEL_LINK_TTL_SECONDS))}",
                    channel=channel,
                )
            ttl_seconds = CHANNEL_LINK_TTL_SECONDS[channel]
        if ttl_seconds <= 0:
            raise ReconqError(
                "CHANNEL_REQUEST_FAILED",
                f"下载链接有效期 ttl_seconds={ttl_seconds} 非法，必须为正数；"
                f"有效期是渠道侧的取件凭证时限，运行期不得调长或调成 0",
                channel=channel,
            )
        return cls(
            recorded_at=recorded_at,
            url=url,
            expires_at=recorded_at + float(ttl_seconds),
            channel=channel,
            bill_date=bill_date,
        )

    # -- 有效期判定 ---------------------------------------------------------
    def is_expired(self, now: float) -> bool:
        """截至 ``now`` 时刻链接是否已过期（边界含等号：到点即失效）。"""
        return now >= self.expires_at

    def remaining_seconds(self, now: float) -> float:
        """剩余有效秒数；已过期返回 0.0（不返回负数，避免下游做无意义比较）。"""
        return max(0.0, self.expires_at - now)

    @property
    def ttl_seconds(self) -> float:
        return self.expires_at - self.recorded_at

    def url_fingerprint(self) -> str:
        """链接指纹：用于日志与复用检测，不把整条签名 URL 打到界面上。"""
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志可读性
        return (
            f"DownloadLink(channel={self.channel}, bill_date={self.bill_date or '（未指定）'}, "
            f"ttl={self.ttl_seconds:.0f}s, url=…{self.url_fingerprint()})"
        )


# --------------------------------------------------------------------------
# 复用检测
# --------------------------------------------------------------------------
class LinkUseRegistry:
    """记录「哪条链接已经被用掉」。

    作用域刻意限定在**一次扫描会话**内：同一次扫描里同一个 ``download_url``
    被用第二次，一定是代码把链接缓存了或循环里没退出；跨会话不追溯，
    因为渠道每天都会给不同的链接，拿昨天的指纹去卡今天没有意义。
    """

    def __init__(self) -> None:
        self._used: dict[str, str] = {}  # url → 首次使用的渠道

    def mark_used(self, link: DownloadLink) -> None:
        """把链接标记为已用；重复使用抛 ``LINK_ALREADY_USED``。"""
        previous = self._used.get(link.url)
        if previous is not None:
            raise ReconqError(
                "LINK_ALREADY_USED",
                f"下载链接被重复使用（首次使用渠道={previous or '（未记录）'}，"
                f"再次使用渠道={link.channel}，链接指纹={link.url_fingerprint()}）。"
                f"download_url 是一次性取件凭证，不得当稳定地址缓存或复用",
                channel=link.channel,
            )
        self._used[link.url] = link.channel

    def is_used(self, url: str) -> bool:
        return url in self._used

    def used_count(self) -> int:
        return len(self._used)

    def reset(self) -> None:
        self._used.clear()


# --------------------------------------------------------------------------
# 内容二次校验
# --------------------------------------------------------------------------
def extract_bill_dates(blob: bytes, *, max_bytes: int = 4_000_000) -> list[str]:
    """从账单正文中抽出所有 ``YYYY-MM-DD`` 日期（保持出现顺序、去重）。

    解码顺序：UTF-8-sig → GB18030 → 都不行时按 latin-1 尽力取。
    **只用日期**、不落任何其他内容——本函数只服务于"这一份文件是不是这个
    账单日的"这一个判断，识别不出就返回空列表交由上层显式处理。
    """
    text: Optional[str] = None
    for decoder in ("utf-8-sig", "gb18030"):
        try:
            text = blob[:max_bytes].decode(decoder)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = blob[:max_bytes].decode("latin-1", errors="ignore")

    seen: list[str] = []
    for match in _DATE_RE.finditer(text):
        value = match.group(0)
        if value not in seen:
            seen.append(value)
    return seen


def _validate_content(
    link: DownloadLink,
    blob: bytes,
    *,
    min_bytes: int = DEFAULT_MIN_BILL_BYTES,
) -> None:
    """下载完成后的内容二次校验。

    两条判据，任一不过都报 ``BILL_CONTENT_MISMATCH``：

    1. **长度**：空响应、连接被重置、错误页 HTML 都远小于真实账单。
    2. **账单日**：正文中出现的日期必须包含取件时请求的那个账单日。
       渠道把上一期的文件塞回来、或导出方传错文件，日期一定对不上。
    """
    if len(blob) < min_bytes:
        raise ReconqError(
            "BILL_CONTENT_MISMATCH",
            f"下载内容仅 {len(blob)} 字节，短于账单正文下限 {min_bytes} 字节；"
            f"多半是链接失效返回的错误页或空响应体。拒绝入库，"
            f"请重新取件（渠道={link.channel}，链接指纹={link.url_fingerprint()}）",
            channel=link.channel,
        )
    if not link.bill_date:
        return
    dates = extract_bill_dates(blob)
    if not dates:
        raise ReconqError(
            "BILL_CONTENT_MISMATCH",
            f"账单正文里找不到任何 YYYY-MM-DD 日期，无法确认它属于账单日 "
            f"{link.bill_date}；本产品不接受无法核实来源的账单",
            channel=link.channel,
        )
    if link.bill_date not in dates:
        raise ReconqError(
            "BILL_CONTENT_MISMATCH",
            f"账单正文日期 {dates[:5]} 不含取件时请求的账单日 {link.bill_date}；"
            f"渠道返回的不是这一天的账单。拒绝入库，请重新取件",
            channel=link.channel,
        )


# --------------------------------------------------------------------------
# 取得即下载
# --------------------------------------------------------------------------
def fetch_with_window(
    link: DownloadLink,
    downloader: Callable[[DownloadLink], bytes],
    *,
    now: Callable[[], float] = time.time,
    registry: Optional[LinkUseRegistry] = None,
    min_bytes: int = DEFAULT_MIN_BILL_BYTES,
) -> bytes:
    """在有效期窗口内下载账单内容。

    顺序是刻意的：

    1. 复用检测（``registry`` 给了才查）——**不发起请求**就否掉；
    2. 过期检测——**不发请求**直接 ``LINK_EXPIRED``；
    3. 才是真正的一次下载；
    4. 下载后内容二次校验。

    ``downloader`` 只在第 3 步被调用，测试用「调用即计数」的假 downloader
    就能断言前两步确实没有产生任何外发请求。
    """
    if registry is not None:
        registry.mark_used(link)

    current = now()
    if link.is_expired(current):
        raise ReconqError(
            "LINK_EXPIRED",
            f"下载链接已过期（{link.channel} 有效期 {link.ttl_seconds:.0f} 秒，"
            f"取得于 {link.recorded_at:.0f}，当前 {current:.0f}，"
            f"已超时 {current - link.expires_at:.1f} 秒）。"
            f"本产品不拿过期链接重试，请重新取件",
            channel=link.channel,
        )

    blob = downloader(link)
    if not isinstance(blob, (bytes, bytearray)):
        raise ReconqError(
            "BILL_DOWNLOAD_FAILED",
            f"downloader 返回了 {type(blob).__name__} 而不是 bytes，无法入库",
            channel=link.channel,
        )
    blob = bytes(blob)
    _validate_content(link, blob, min_bytes=min_bytes)
    return blob


# --------------------------------------------------------------------------
# 过期后的正确恢复路径
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LinkFetchOutcome:
    """一次「取件 + 取得即下载」的结果。

    ``status='ok'`` 时 ``blob`` 有值；``status='not_fetched'`` 时
    ``error_code='LINK_EXPIRED'``，``error`` 是可直接写进面板的异常。
    **``not_fetched`` 不带任何内容**——宁可这一路空着，也不能让上层把
    半截文件当账单继续跑。
    """

    status: str
    channel: str
    blob: bytes = b""
    link: Optional[DownloadLink] = None
    refetch_count: int = 0
    error_code: str = ""
    error: Optional[ReconqError] = None
    timeline: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def raise_if_not_fetched(self) -> bytes:
        """未取到时把异常抛回调用方（编排层据此剔除该渠道）。"""
        if self.ok:
            return self.blob
        assert self.error is not None
        raise self.error


def fetch_with_recovery(
    link_issuer: Callable[[], DownloadLink],
    downloader: Callable[[DownloadLink], bytes],
    *,
    now: Callable[[], float] = time.time,
    registry: Optional[LinkUseRegistry] = None,
    max_refetch: int = 1,
    min_bytes: int = DEFAULT_MIN_BILL_BYTES,
    sleep: Optional[Callable[[float], None]] = None,
) -> LinkFetchOutcome:
    """取得即下载；链接失效时**重新取件**拿新链接，额度内最多 ``max_refetch`` 次。

    与「重试旧链接」的区别是本质的：微信重取一次要 5 分钟内下载到，
    支付宝只有 30 秒。正确动作永远是**重新走取件接口拿一条新链接**，
    而不是在一条已经死掉的链接上反复敲。``max_refetch=1`` 意味着：
    一条链接失效 → 重新取件一次 → 新链接仍失效 → 本期该渠道标未取到。

    只有 ``LINK_EXPIRED`` 与 ``LINK_ALREADY_USED`` 会触发重新取件；
    内容不符（``BILL_CONTENT_MISMATCH``）**不重试**——那是文件本身的问题，
    再取一次大概率还是同一个文件，重新取件纯属浪费渠道配额。
    """
    if max_refetch < 0:
        raise ReconqError(
            "CHANNEL_REQUEST_FAILED",
            f"max_refetch={max_refetch} 非法，重新取件次数不能为负；"
            f"负数会让「重取 N 次」静默变成「一次都不重取」而看不出来",
            channel=getattr(link_issuer(), "channel", "") or None,
        )

    timeline: list[str] = []
    link = link_issuer()
    timeline.append(f"取得链接（{link.channel}，剩余 {link.remaining_seconds(now()):.0f} 秒）")

    refetched = 0
    while True:
        try:
            blob = fetch_with_window(
                link,
                downloader,
                now=now,
                registry=registry,
                min_bytes=min_bytes,
            )
        except ReconqError as exc:
            if exc.code not in ("LINK_EXPIRED", "LINK_ALREADY_USED") or refetched >= max_refetch:
                if exc.code in ("LINK_EXPIRED", "LINK_ALREADY_USED"):
                    # 重新取件额度已用尽：如实报「本期未取到」，
                    # 真实成因保留在 cause_code 上（处置结果 vs 根因）。
                    err = ReconqError(
                        "CHANNEL_NOT_FETCHED",
                        f"渠道 {exc.channel or link.channel} 本期未取到账单："
                        f"重新取件 {refetched} 次仍失败。最后一次原因：[{exc.code}] {exc.detail}",
                        channel=exc.channel or link.channel,
                    )
                    err.cause_code = "LINK_EXPIRED"
                    return LinkFetchOutcome(
                        status="not_fetched",
                        channel=link.channel,
                        refetch_count=refetched,
                        error_code="LINK_EXPIRED",
                        error=err,
                        timeline=tuple(timeline + ["重新取件额度用尽，标本期未取到"]),
                    )
                raise
            refetched += 1
            timeline.append(f"链接失效（{exc.code}），第 {refetched} 次重新取件")
            if sleep is not None:
                sleep(0.5 * refetched)
            link = link_issuer()
            timeline.append(f"取得新链接（剩余 {link.remaining_seconds(now()):.0f} 秒）")
            continue

        timeline.append(f"下载完成并通过内容校验（{len(blob)} 字节）")
        return LinkFetchOutcome(
            status="ok",
            channel=link.channel,
            blob=blob,
            link=link,
            refetch_count=refetched,
            timeline=tuple(timeline),
        )
