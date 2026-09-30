"""账单下载链接有效期窗口的断言（Task 9）。

覆盖五类关键行为（对应 step 里的五类断言）：

1. 微信 300 秒 / 支付宝 30 秒的窗口边界（到点即失效，边界取等号）；
2. 已过期链接**不发请求**——假 downloader 的调用次数必须是 0；
3. 同一 download_url 第二次使用即报 ``LINK_ALREADY_USED``；
4. 链接失效后**重新取件**恢复（新链接能下载成功，refetch 计数正确）；
5. 重新取件额度用尽后标本期未取到并给出 ``LINK_EXPIRED``（cause_code）。

外加内容二次校验：长度不足、账单日对不上，都报 ``BILL_CONTENT_MISMATCH``。

本文件用假时钟，**不等待真实时间**；账单正文为脱敏占位内容，
不含任何真实商户数据，也不连接任何真实渠道端点。
"""

from __future__ import annotations

import pytest

from reconq.errors import ReconqError
from reconq.sources.link_window import (
    CHANNEL_LINK_TTL_SECONDS,
    DEFAULT_MIN_BILL_BYTES,
    DownloadLink,
    LinkFetchOutcome,
    LinkUseRegistry,
    extract_bill_dates,
    fetch_with_recovery,
    fetch_with_window,
)

BILL_DATE = "2026-09-07"


class FakeClock:
    """可注入的假时钟：只有显式推进才走时间，测试不真等。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> "FakeClock":
        self.t += seconds
        return self


class RecordingDownloader:
    """记录调用次数的假 downloader——用来断言「过期时一个请求都没发」。"""

    def __init__(self, payload: bytes = b"", *, error: Exception | None = None) -> None:
        self.calls = 0
        self.seen_links: list[DownloadLink] = []
        self.payload = payload
        self.error = error

    def __call__(self, link: DownloadLink) -> bytes:
        self.calls += 1
        self.seen_links.append(link)
        if self.error is not None:
            raise self.error
        return self.payload


def make_bill_text(bill_date: str = BILL_DATE) -> bytes:
    """一份脱敏账单正文（占位单号，UTF-8）。"""
    return (
        "微信支付账单明细列表\n"
        f"商户号：EXAM_MCH    账单日期：{bill_date}\n"
        "\n"
        "交易时间,商户订单号,微信交易单号,交易状态,应结订单金额,退款金额\n"
        f"{bill_date} 09:12:03,EX-ORD-0001,EX-TXN-0001,SUCCESS,128.00,0.00\n"
        f"{bill_date} 09:31:44,EX-ORD-0002,EX-TXN-0002,SUCCESS,1020.50,0.00\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. 窗口边界
# ---------------------------------------------------------------------------
class TestWindowBoundaries:
    def test_channel_ttls_match_official_values(self):
        assert CHANNEL_LINK_TTL_SECONDS["wechat"] == 300
        assert CHANNEL_LINK_TTL_SECONDS["alipay"] == 30

    @pytest.mark.parametrize(
        "channel, ttl",
        [("wechat", 300), ("alipay", 30)],
    )
    def test_issue_sets_expires_at_from_channel_ttl(self, channel, ttl):
        link = DownloadLink.issue(channel, f"https://example.invalid/{channel}/x", recorded_at=1000.0)
        assert link.expires_at == 1000.0 + ttl
        assert link.ttl_seconds == ttl
        assert link.channel == channel

    @pytest.mark.parametrize(
        "channel, elapsed, expired",
        [
            # 微信 300 秒：299 秒还在，300 秒整判失效（到点即失效）
            ("wechat", 299.0, False),
            ("wechat", 299.999, False),
            ("wechat", 300.0, True),
            ("wechat", 300.001, True),
            # 支付宝 30 秒：29 秒还在，30 秒整判失效
            ("alipay", 29.0, False),
            ("alipay", 29.999, False),
            ("alipay", 30.0, True),
            ("alipay", 45.0, True),
        ],
    )
    def test_is_expired_boundary_inclusive(self, channel, elapsed, expired):
        link = DownloadLink.issue(channel, "https://example.invalid/l", recorded_at=0.0)
        assert link.is_expired(elapsed) is expired

    def test_remaining_seconds_never_negative(self):
        link = DownloadLink.issue("alipay", "https://example.invalid/l", recorded_at=0.0)
        assert link.remaining_seconds(10.0) == pytest.approx(20.0)
        assert link.remaining_seconds(30.0) == 0.0
        assert link.remaining_seconds(999.0) == 0.0

    def test_unknown_channel_without_ttl_is_rejected(self):
        with pytest.raises(ReconqError) as exc:
            DownloadLink.issue("unionpay", "https://example.invalid/l", recorded_at=0.0)
        assert exc.value.code == "CHANNEL_UNKNOWN"
        assert "unionpay" in exc.value.detail

    def test_empty_url_rejected_at_issue(self):
        with pytest.raises(ReconqError) as exc:
            DownloadLink.issue("wechat", "", recorded_at=0.0)
        assert exc.value.code == "CHANNEL_REQUEST_FAILED"


# ---------------------------------------------------------------------------
# 2. 已过期不发请求
# ---------------------------------------------------------------------------
class TestExpiredSendsNoRequest:
    def test_expired_link_raises_and_downloader_never_called(self):
        clock = FakeClock()
        link = DownloadLink.issue(
            "alipay", "https://example.invalid/bill.zip", recorded_at=clock.t, bill_date=BILL_DATE
        )
        clock.advance(31.0)  # 30 秒有效期已过
        downloader = RecordingDownloader(make_bill_text())

        with pytest.raises(ReconqError) as exc:
            fetch_with_window(link, downloader, now=clock)

        assert exc.value.code == "LINK_EXPIRED"
        assert exc.value.channel == "alipay"
        assert downloader.calls == 0, "过期链接一个字节都不该往渠道发"
        assert "重新取件" in exc.value.detail

    def test_valid_link_does_download(self):
        clock = FakeClock()
        payload = make_bill_text()
        link = DownloadLink.issue(
            "wechat", "https://example.invalid/bill", recorded_at=clock.t, bill_date=BILL_DATE
        )
        clock.advance(10.0)
        downloader = RecordingDownloader(payload)

        assert fetch_with_window(link, downloader, now=clock) == payload
        assert downloader.calls == 1

    def test_expiry_error_carries_both_clocks(self):
        clock = FakeClock()
        link = DownloadLink.issue("alipay", "https://example.invalid/l", recorded_at=clock.t)
        clock.advance(75.0)
        with pytest.raises(ReconqError) as exc:
            fetch_with_window(link, RecordingDownloader(), now=clock)
        detail = exc.value.detail
        assert "30 秒" in detail
        assert "45.0" in detail  # 超时 45 秒，把超时量摆出来而不是笼统说失效


# ---------------------------------------------------------------------------
# 3. 链接复用检测
# ---------------------------------------------------------------------------
class TestLinkReuse:
    def test_second_use_of_same_url_is_rejected(self):
        clock = FakeClock()
        registry = LinkUseRegistry()
        payload = make_bill_text()
        link = DownloadLink.issue(
            "wechat", "https://example.invalid/same-url", recorded_at=clock.t, bill_date=BILL_DATE
        )
        downloader = RecordingDownloader(payload)

        assert fetch_with_window(link, downloader, now=clock, registry=registry) == payload
        assert registry.used_count() == 1

        with pytest.raises(ReconqError) as exc:
            fetch_with_window(link, downloader, now=clock, registry=registry)
        assert exc.value.code == "LINK_ALREADY_USED"
        assert downloader.calls == 1, "复用检测必须发生在发请求之前"

    def test_reuse_detection_off_when_no_registry(self):
        """不传 registry 时不拦复用——调用方明确选择不做会话级追踪。"""
        clock = FakeClock()
        link = DownloadLink.issue("wechat", "https://example.invalid/u", recorded_at=clock.t)
        link = DownloadLink(link.recorded_at, link.url, link.expires_at, link.channel, BILL_DATE)
        payload = make_bill_text()
        downloader = RecordingDownloader(payload)
        assert fetch_with_window(link, downloader, now=clock) == payload
        assert fetch_with_window(link, downloader, now=clock) == payload
        assert downloader.calls == 2

    def test_registry_is_used_and_reset(self):
        registry = LinkUseRegistry()
        link = DownloadLink.issue("wechat", "https://example.invalid/u", recorded_at=0.0)
        registry.mark_used(link)
        assert registry.is_used(link.url) is True
        assert registry.used_count() == 1
        registry.reset()
        assert registry.is_used(link.url) is False

    def test_fingerprint_does_not_leak_full_url(self):
        link = DownloadLink.issue("alipay", "https://example.invalid/secret-token-abc", recorded_at=0.0)
        assert "secret-token-abc" not in link.url_fingerprint()
        assert len(link.url_fingerprint()) == 16


# ---------------------------------------------------------------------------
# 4. 内容二次校验
# ---------------------------------------------------------------------------
class TestContentValidation:
    def test_short_body_rejected(self):
        clock = FakeClock()
        link = DownloadLink.issue(
            "wechat", "https://example.invalid/b", recorded_at=clock.t, bill_date=BILL_DATE
        )
        short = b"<html>403 Forbidden</html>"
        with pytest.raises(ReconqError) as exc:
            fetch_with_window(link, RecordingDownloader(short), now=clock)
        assert exc.value.code == "BILL_CONTENT_MISMATCH"
        assert str(len(short)) in exc.value.detail

    def test_empty_body_rejected(self):
        clock = FakeClock()
        link = DownloadLink.issue(
            "wechat", "https://example.invalid/b", recorded_at=clock.t, bill_date=BILL_DATE
        )
        with pytest.raises(ReconqError) as exc:
            fetch_with_window(link, RecordingDownloader(b""), now=clock)
        assert exc.value.code == "BILL_CONTENT_MISMATCH"

    def test_wrong_bill_date_rejected(self):
        """渠道把上一期文件返回——日期对不上，拒绝入库。"""
        clock = FakeClock()
        link = DownloadLink.issue(
            "wechat", "https://example.invalid/b", recorded_at=clock.t, bill_date=BILL_DATE
        )
        stale = make_bill_text("2026-09-06")
        with pytest.raises(ReconqError) as exc:
            fetch_with_window(link, RecordingDownloader(stale), now=clock)
        assert exc.value.code == "BILL_CONTENT_MISMATCH"
        assert BILL_DATE in exc.value.detail

    def test_body_without_any_date_rejected(self):
        clock = FakeClock()
        link = DownloadLink.issue(
            "wechat", "https://example.invalid/b", recorded_at=clock.t, bill_date=BILL_DATE
        )
        blob = b"x" * 200
        with pytest.raises(ReconqError) as exc:
            fetch_with_window(link, RecordingDownloader(blob), now=clock)
        assert exc.value.code == "BILL_CONTENT_MISMATCH"
        assert "找不到" in exc.value.detail

    def test_gb18030_body_date_still_extracted(self):
        text = f"账单日期：{BILL_DATE}\n商户订单号,金额\nEX-1,10.00\n"
        blob = ("列A,列B\n" + text).encode("gb18030")
        assert BILL_DATE in extract_bill_dates(blob)

    def test_no_bill_date_skips_date_check(self):
        """未指定账单日时只做长度校验——不猜。"""
        clock = FakeClock()
        link = DownloadLink.issue("wechat", "https://example.invalid/b", recorded_at=clock.t)
        payload = b"z" * 500
        assert fetch_with_window(link, RecordingDownloader(payload), now=clock) == payload

    def test_non_bytes_payload_rejected(self):
        clock = FakeClock()
        link = DownloadLink.issue("wechat", "https://example.invalid/b", recorded_at=clock.t)

        class BadDownloader:
            def __call__(self, link):
                return "not-bytes"

        with pytest.raises(ReconqError) as exc:
            fetch_with_window(link, BadDownloader(), now=clock)
        assert exc.value.code == "BILL_DOWNLOAD_FAILED"

    def test_min_bytes_default_is_sane(self):
        assert DEFAULT_MIN_BILL_BYTES > 32
        assert make_bill_text().__len__() > DEFAULT_MIN_BILL_BYTES


# ---------------------------------------------------------------------------
# 5. 过期后的重新取件恢复
# ---------------------------------------------------------------------------
class TestRefetchRecovery:
    def test_expired_link_triggers_refetch_and_succeeds(self):
        """第一条链接在下载前就已过期 → 重新取件 → 第二条取到内容。"""
        clock = FakeClock()
        issued: list[DownloadLink] = []

        def issue_next() -> DownloadLink:
            idx = len(issued)
            link = DownloadLink.issue(
                "alipay",
                f"https://example.invalid/bill-{idx}.zip",
                recorded_at=clock.t,
                bill_date=BILL_DATE,
            )
            issued.append(link)
            if idx == 0:
                # 模拟「取件后到下载前耗掉了 31 秒」——真实排障里最常见的一条：
                # 财务科先点开页面看了一眼账单，晚了几十秒才点下载。
                clock.advance(31.0)
            return link

        downloader = RecordingDownloader(make_bill_text())
        outcome = fetch_with_recovery(issue_next, downloader, now=clock)

        assert outcome.status == "ok"
        assert outcome.refetch_count == 1
        assert len(issued) == 2, "失效后必须重新走一次取件接口拿新链接"
        assert issued[0].url != issued[1].url
        assert downloader.calls == 1, "过期那条链接一个请求都不发，只有新链接下载了一次"
        assert outcome.blob == make_bill_text()
        assert any("重新取件" in line for line in outcome.timeline)

    def test_refetch_budget_exhausted_marks_not_fetched(self):
        """两条链接都过期 → 本期未取到，真实成因 LINK_EXPIRED 可见。"""
        clock = FakeClock()
        issued: list[DownloadLink] = []

        def issue_next() -> DownloadLink:
            link = DownloadLink.issue(
                "alipay",
                f"https://example.invalid/dead-{len(issued)}.zip",
                recorded_at=clock.t,
                bill_date=BILL_DATE,
            )
            issued.append(link)
            clock.advance(31.0)  # 每条链接都在下载前就过期
            return link

        downloader = RecordingDownloader(make_bill_text())
        outcome = fetch_with_recovery(issue_next, downloader, now=clock)

        assert outcome.ok is False
        assert outcome.status == "not_fetched"
        assert outcome.refetch_count == 1
        assert outcome.blob == b"", "未取到时不得携带任何内容"
        assert outcome.error_code == "LINK_EXPIRED"
        assert len(issued) == 2, "退避额度内最多重新取件 1 次，不硬重试"
        assert downloader.calls == 0, "两条链接都过期，一个请求都不该发出去"

        with pytest.raises(ReconqError) as exc:
            outcome.raise_if_not_fetched()
        assert exc.value.code == "CHANNEL_NOT_FETCHED"
        assert exc.value.cause_code == "LINK_EXPIRED"
        assert exc.value.channel == "alipay"

    def test_content_mismatch_does_not_trigger_refetch(self):
        """内容不符不重新取件——再取一次大概率还是同一个错文件。"""
        clock = FakeClock()
        issued = []

        def issue_next() -> DownloadLink:
            idx = len(issued)
            link = DownloadLink.issue(
                "wechat", f"https://example.invalid/w-{idx}", recorded_at=clock.t, bill_date=BILL_DATE
            )
            issued.append(link)
            return link

        downloader = RecordingDownloader(make_bill_text("2026-09-06"))  # 日期错

        with pytest.raises(ReconqError) as exc:
            fetch_with_recovery(issue_next, downloader, now=clock)

        assert exc.value.code == "BILL_CONTENT_MISMATCH"
        assert len(issued) == 1, "内容不符不消耗重新取件额度"

    def test_zero_refetch_budget_disabled(self):
        clock = FakeClock()
        issued: list[DownloadLink] = []

        def issue_next() -> DownloadLink:
            link = DownloadLink.issue(
                "alipay",
                f"https://example.invalid/z-{len(issued)}",
                recorded_at=clock.t,
                bill_date=BILL_DATE,
            )
            issued.append(link)
            clock.advance(31.0)
            return link

        outcome = fetch_with_recovery(
            issue_next, RecordingDownloader(make_bill_text()), now=clock, max_refetch=0
        )
        assert outcome.status == "not_fetched"
        assert outcome.refetch_count == 0
        assert len(issued) == 1, "额度为 0 时不重新取件"

    def test_sleep_called_between_refetch(self):
        clock = FakeClock()
        issued: list[DownloadLink] = []

        def issue_next() -> DownloadLink:
            link = DownloadLink.issue(
                "alipay",
                f"https://example.invalid/s-{len(issued)}",
                recorded_at=clock.t,
                bill_date=BILL_DATE,
            )
            issued.append(link)
            if len(issued) == 1:
                clock.advance(31.0)
            return link

        slept: list[float] = []
        outcome = fetch_with_recovery(
            issue_next,
            RecordingDownloader(make_bill_text()),
            now=clock,
            sleep=slept.append,
        )
        assert outcome.status == "ok"
        assert slept == [0.5], "重新取件前应按退避节奏等一下，不立刻硬打渠道"

    def test_negative_max_refetch_rejected(self):
        clock = FakeClock()
        link = DownloadLink.issue("alipay", "https://example.invalid/x", recorded_at=clock.t)
        # Task 20 起参数校验也走 ReconqError：裸 ValueError 冒到 CLI 就是
        # 一段没有错误码、没有建议动作的裸栈。
        with pytest.raises(ReconqError) as excinfo:
            fetch_with_recovery(lambda: link, RecordingDownloader(), now=clock, max_refetch=-1)
        assert excinfo.value.code == "CHANNEL_REQUEST_FAILED"
        assert "max_refetch=-1" in excinfo.value.detail

    def test_outcome_ok_helper_returns_blob(self):
        outcome = LinkFetchOutcome(status="ok", channel="wechat", blob=b"abc")
        assert outcome.ok is True
        assert outcome.raise_if_not_fetched() == b"abc"


# ---------------------------------------------------------------------------
# 6. 与取数主路径接线（真实协议层的取件 → 立即下载）
# ---------------------------------------------------------------------------
class TestWiringIntoChannelFetch:
    def test_wechat_ttl_matches_channel_module(self):
        """本模块的微信 TTL 必须与取数模块保持同源，避免两处口径漂移。"""
        from reconq.sources.wechat_bill import DOWNLOAD_LINK_TTL_SECONDS as wechat_ttl

        assert CHANNEL_LINK_TTL_SECONDS["wechat"] == wechat_ttl

    def test_alipay_ttl_matches_channel_module(self):
        from reconq.sources.alipay_bill import DOWNLOAD_LINK_TTL_SECONDS as alipay_ttl

        assert CHANNEL_LINK_TTL_SECONDS["alipay"] == alipay_ttl

    def test_window_used_with_mock_transport_end_to_end(self):
        """用 httpx.MockTransport 走一遍真实的「取件 → 取得即下载 → 校验」。

        账单正文是脱敏占位内容，**不连接任何真实商户端点**。
        """
        import hashlib

        import httpx

        payload = make_bill_text()
        sha1 = hashlib.sha1(payload).hexdigest()
        requests_seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append(request.url.path)
            if request.url.path == "/v3/bill/tradebill":
                return httpx.Response(
                    200,
                    json={"hash_type": "SHA1", "hash": sha1, "download_url": "https://example.invalid/dl"},
                )
            return httpx.Response(200, content=payload)

        clock = FakeClock()
        link = DownloadLink.issue(
            "wechat", "https://example.invalid/dl", recorded_at=clock.t, bill_date=BILL_DATE
        )
        client = httpx.Client(transport=httpx.MockTransport(handler))

        def downloader(lk):
            resp = client.get(lk.url)
            assert resp.status_code == 200
            return resp.content

        result = fetch_with_window(link, downloader, now=clock)
        assert result == payload
        assert len(requests_seen) == 1

        # 同一 URL 再次使用被复用检测拦下（在 mock transport 之前）
        registry = LinkUseRegistry()
        registry.mark_used(link)
        with pytest.raises(ReconqError) as exc:
            fetch_with_window(link, downloader, now=clock, registry=registry)
        assert exc.value.code == "LINK_ALREADY_USED"
        assert len(requests_seen) == 1, "复用检测后不应产生第二个请求"
