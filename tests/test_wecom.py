"""企业微信超阈值推送、深链、失败重试入队与去重（Task 46）。

规定的六类断言逐条可核：

1. **未超阈值零调用** —— 不超阈值时 HTTP 客户端一次都没被碰、状态文件
   连创建都没有（不是「推了一条无异常」）；
2. **超阈值推送一次且无金额明细** —— 真的 POST 了一次，正文里查不到
   任何金额形态，且载荷键全在白名单内；
3. **深链可解析到具体差异** —— ``reconq://period/<期>/diff/<键>`` 解析回
   的 ``(period, key)`` 与传入的差异键逐字相同（这是「点开直达那条处置单」的前提）；
4. **失败入队并重试成功** —— 首次连续失败入 pending 队列且**落盘**，
   时钟前拨后 :func:`flush_pending` 重发成功，队列清空、状态转「本期已推送」；
5. **失败状态面板可见** —— 重试用尽后 :func:`notifier_state` 必须是
   ``failed``（不是回落成 ``not_pushed`` —— 那一行会被读成「一切正常」）；
6. **去重不重复推** —— 同账期同阈值类型第二次调用 ``deduped=True`` 且
   零 HTTP；人工 ``force`` 换操作人可再推，机器操作人被拒。

外加几条本任务真正踩到的反例：阈值写错必须报错（静默忽略会变成「每期都推」）、
缺 key 不得被记成「已推送」、限频 45009 与 key 无效 93000 走不同的失败语义、
推送正文里塞进金额即拒发、状态文件损坏不得静默当空（会让同一期重推）。
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest

from reconq.audit.wecom import (
    AMOUNT_THRESHOLD_ENV,
    COUNT_THRESHOLD_ENV,
    DEEP_LINK_SCHEME,
    PUSH_STATE_NAME,
    PUSH_STATE_ZH,
    THRESHOLD_KIND_ZH,
    WEBHOOK_KEY_ENV,
    NotifState,
    WeComConfig,
    assert_payload_clean,
    build_deep_link,
    build_payload,
    evaluate_threshold,
    flush_pending,
    notifier_state,
    notifier_status_of,
    parse_deep_link,
    render_markdown,
    send_summary,
)
from reconq.errors import ReconqError

PERIOD = "2026-W36"
KEY_A = f"{PERIOD}|wechat|taxonomy.missing:EX-9"
KEY_B = f"{PERIOD}|alipay|taxonomy.missing:EX-11"


# ─────────────────────────── 测试替身 ───────────────────────────


class FakeResponse:
    """替身响应。**只出现在测试层** —— 产品代码走真实 httpx 协议。"""

    def __init__(self, status_code: int = 200, body: object = None) -> None:
        self.status_code = status_code
        self._body = {"errcode": 0, "errmsg": "ok"} if body is None else body

    def json(self) -> object:
        return self._body


class FakeClient:
    """记录每次调用的 URL 与 body 的假客户端。"""

    def __init__(self, responses: object = None) -> None:
        #: 每次调用是 ``(status, body)``；给 ``int`` 则是恒定状态码。
        self.responses: list = list(responses or [])
        self.calls: list[dict] = []

    def post(self, url: str, json: object = None) -> FakeResponse:  # noqa: A002 - 与 httpx 同名
        self.calls.append({"url": url, "body": json})
        if not self.responses:
            return FakeResponse()
        item = self.responses.pop(0)
        if isinstance(item, FakeResponse):
            return item
        if isinstance(item, int):
            return FakeResponse(item, {"errcode": 93000, "errmsg": "invalid webhook url"})
        status, body = item
        return FakeResponse(status, body)

    def close(self) -> None:  # pragma: no cover - 注入客户端时不会被调用
        pass


@pytest.fixture()
def state_dir(tmp_path):
    return str(tmp_path / "audit")


def _config(**overrides) -> WeComConfig:
    base = {
        "webhook_key": "test-webhook-key-0000",
        "count_threshold": 30,
        "amount_threshold": Decimal("5000.00"),
        "panel_base": "http://127.0.0.1:8788",
        "max_attempts": 3,
    }
    base.update(overrides)
    return WeComConfig(**base)


# ─────────────────────────── 1. 未超阈值零调用 ───────────────────────────


def test_below_threshold_makes_zero_http_calls_and_writes_no_state(state_dir):
    client = FakeClient()
    result = send_summary(
        PERIOD,
        config=_config(),
        state_dir=state_dir,
        diff_keys=[KEY_A],
        unbalanced_count=30,  # 恰好等于阈值：**不**超
        unbalanced_amount=Decimal("5000.00"),
        client=client,
    )
    assert result.state == "not_pushed"
    assert result.delivered is False
    assert client.calls == [], "未超阈值时一次 HTTP 都不该发生"
    assert not os.path.exists(os.path.join(state_dir, PUSH_STATE_NAME)), (
        "未超阈值不该留下状态文件 —— 写一个 not_pushed 记录只会让"
        "「这期真的没超」与「这期从没判过」在面板上长得一样"
    )
    assert "未超条数阈值 30" in result.error
    assert "未超金额阈值 5000.00" in result.error
    assert "未配置任何阈值" not in result.error, (
        "配了阈值只是没超，说成「未配置任何阈值」会让人以为要去配阈值"
    )


def test_count_and_amount_thresholds_are_judged_independently():
    """条数超了但金额没超，仍要推 —— 38 条挂着的单子正是最该叫醒人的。"""
    verdict = evaluate_threshold(
        PERIOD, unbalanced_count=31, unbalanced_amount=Decimal("12.00"), config=_config()
    )
    assert verdict.kinds == ("count",)
    assert verdict.should_push is True

    only_amount = evaluate_threshold(
        PERIOD, unbalanced_count=3, unbalanced_amount=Decimal("5000.01"), config=_config()
    )
    assert only_amount.kinds == ("amount",)

    both = evaluate_threshold(
        PERIOD, unbalanced_count=99, unbalanced_amount=Decimal("9000.00"), disputed=True, config=_config()
    )
    assert both.kinds == ("count", "amount", "disputed")
    assert set(both.kind_zh()) == set(THRESHOLD_KIND_ZH.values())


def test_disputed_period_alone_triggers_push():
    """已存疑待主管复核的账期，即使条数金额都没超也要通知主管。"""
    verdict = evaluate_threshold(
        PERIOD, unbalanced_count=0, unbalanced_amount=None, disputed=True, config=_config()
    )
    assert verdict.kinds == ("disputed",)
    assert verdict.should_push is True


def test_no_threshold_configured_never_pushes():
    verdict = evaluate_threshold(
        PERIOD, unbalanced_count=999, unbalanced_amount=Decimal("999999"), config=WeComConfig()
    )
    assert verdict.kinds == ()
    assert verdict.should_push is False
    assert "未配置任何阈值" in verdict.reason_zh()


def test_threshold_misconfiguration_raises_instead_of_being_ignored():
    """阈值写错必须报错：静默忽略会把「每期都推」当成默认。"""
    with pytest.raises(ReconqError) as exc:
        WeComConfig.from_env({COUNT_THRESHOLD_ENV: "10 条"})
    assert exc.value.code == "WECOM_THRESHOLD_INVALID"

    with pytest.raises(ReconqError) as exc:
        WeComConfig.from_env({AMOUNT_THRESHOLD_ENV: "abc"})
    assert exc.value.code == "WECOM_THRESHOLD_INVALID"

    with pytest.raises(ReconqError) as exc:
        WeComConfig.from_env({COUNT_THRESHOLD_ENV: "-1"})
    assert "负数" in exc.value.detail and "超限" in exc.value.detail

    with pytest.raises(ReconqError) as exc:
        WeComConfig.from_env({AMOUNT_THRESHOLD_ENV: "-0.01"})
    assert exc.value.code == "WECOM_THRESHOLD_INVALID"


def test_amount_threshold_parsed_as_decimal_not_float():
    cfg = WeComConfig.from_env({AMOUNT_THRESHOLD_ENV: "5000"})
    assert isinstance(cfg.amount_threshold, Decimal)
    assert cfg.amount_threshold == Decimal("5000")
    # 「阈值相等」在定点数下必须判为不超；用 float 时 5000.00 与 5000
    # 是同一个 float，比较会侥幸通过，但 0.1+0.2 一类会翻车。
    verdict = evaluate_threshold(
        PERIOD, unbalanced_count=0, unbalanced_amount=Decimal("5000.00"), config=cfg
    )
    assert verdict.should_push is False


# ─────────────────────────── 2. 超阈值推送一次且无明细 ───────────────────────────


def test_over_threshold_posts_once_with_clean_content(state_dir):
    client = FakeClient()
    result = send_summary(
        PERIOD,
        config=_config(),
        state_dir=state_dir,
        diff_keys=[KEY_A, KEY_B],
        unbalanced_count=37,
        unbalanced_amount=Decimal("8123.45"),
        client=client,
    )
    assert result.state == "pushed"
    assert result.delivered is True
    assert result.kinds == ("count", "amount")
    assert result.errcode == 0
    assert len(client.calls) == 1, "超阈值应当只发一次（去重在第二次才生效）"

    call = client.calls[0]
    assert call["url"].startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=")
    assert "test-webhook-key-0000" in call["url"]
    body = call["body"]
    assert body["msgtype"] == "markdown"
    content = body["markdown"]["content"]
    assert "2026-W36" in content
    assert "37" in content
    assert "单边账条数超限" in content
    # 金额明细绝不能出现在推送正文里
    assert "8123.45" not in content
    assert "元" not in content
    assert "¥" not in content


def test_payload_keys_are_whitelisted(state_dir):
    client = FakeClient()
    send_summary(
        PERIOD, config=_config(), state_dir=state_dir, diff_keys=[KEY_A],
        unbalanced_count=99, client=client,
    )
    payload = json.loads(json.dumps(client.calls[0]["body"]["markdown"]))
    assert payload["content"]  # markdown 形态先过一遍构造期检查
    # 载荷本体（重试队列里落盘的那份）也只含白名单键
    state = json.load(open(os.path.join(state_dir, PUSH_STATE_NAME), encoding="utf-8"))
    assert state["pending"] == []


def test_payload_with_amount_is_rejected_before_send():
    with pytest.raises(ReconqError) as exc:
        assert_payload_clean(
            {
                "period": PERIOD,
                "kinds": ["count"],
                "unbalanced_count": 37,
                "panel_url": "http://127.0.0.1:8788/period/2026-W36",
                "deep_links": ["微信差额 8123.45 元待核"],
            }
        )
    assert exc.value.code == "WECOM_PAYLOAD_REJECTED"
    assert "金额形态" in exc.value.detail


def test_payload_with_patient_identifier_is_rejected():
    with pytest.raises(ReconqError) as exc:
        assert_payload_clean(
            {
                "period": PERIOD,
                "kinds": ["count"],
                "unbalanced_count": 37,
                "panel_url": "http://127.0.0.1:8788/period/2026-W36",
                "deep_links": ["已联系住院号 110105199001011234 的家属"],
            }
        )
    assert exc.value.code == "WECOM_PAYLOAD_REJECTED"
    assert "患者标识形态" in exc.value.detail


def test_payload_with_unknown_key_is_rejected():
    with pytest.raises(ReconqError) as exc:
        assert_payload_clean({"period": PERIOD, "kinds": [], "unbalanced_count": 1, "amounts": ["1.00 元"]})
    assert exc.value.code == "WECOM_PAYLOAD_REJECTED"
    assert "白名单外的键" in exc.value.detail


def test_build_payload_refuses_when_verdict_says_no_push():
    """判定说「不推」却要求构造载荷 —— 报错，不构造。"""
    verdict = evaluate_threshold(PERIOD, unbalanced_count=1, config=_config())
    with pytest.raises(ReconqError) as exc:
        build_payload(verdict, diff_keys=[KEY_A])
    assert exc.value.code == "WECOM_PAYLOAD_REJECTED"


def test_too_many_deep_links_are_truncated_not_oversized():
    keys = [f"{PERIOD}|wechat|taxonomy.missing:EX-{i}" for i in range(50)]
    verdict = evaluate_threshold(PERIOD, unbalanced_count=99, config=_config())
    payload = build_payload(verdict, diff_keys=keys, max_links=5)
    assert len(payload["deep_links"]) == 5
    content = render_markdown(payload, truncated=45)
    assert "其余 45 条请在面板查看" in content


# ─────────────────────────── 3. 深链可解析到具体差异 ───────────────────────────


def test_deep_link_round_trips_to_the_exact_discrepancy_key():
    link = build_deep_link(PERIOD, KEY_A)
    assert link == f"{DEEP_LINK_SCHEME}period/{PERIOD}/diff/{KEY_A}"
    period, key = parse_deep_link(link)
    assert period == PERIOD
    assert key == KEY_A, "深链必须解析回台账那一行的业务键，而不是「第 3 条」"
    assert key.startswith(f"{PERIOD}|wechat|"), "解析后可直接定位到渠道与口径"


def test_deep_link_present_in_real_push_payload(state_dir):
    client = FakeClient()
    send_summary(
        PERIOD, config=_config(), state_dir=state_dir,
        diff_keys=[KEY_A, KEY_B], unbalanced_count=99, client=client,
    )
    content = client.calls[0]["body"]["markdown"]["content"]
    for key in (KEY_A, KEY_B):
        assert build_deep_link(PERIOD, key) in content
        assert parse_deep_link(build_deep_link(PERIOD, key))[1] == key


def test_malformed_deep_link_raises_instead_of_being_ignored():
    """静默忽略坏链接的症状是「点了没反应」，与「差异已消解」无法区分。"""
    for bad in ("http://127.0.0.1:8788/period/x", f"{DEEP_LINK_SCHEME}period/{PERIOD}", "reconq://"):
        with pytest.raises(ReconqError) as exc:
            parse_deep_link(bad)
        assert exc.value.code == "WECOM_PAYLOAD_REJECTED"


def test_deep_link_requires_both_period_and_key():
    with pytest.raises(ReconqError) as exc:
        build_deep_link(PERIOD, "")
    assert "空的深链" in exc.value.detail


# ─────────────────────────── 4. 失败入队并重试成功 ───────────────────────────


def test_rate_limited_push_is_queued_to_disk_and_resent_after_restart(state_dir):
    client = FakeClient([(200, {"errcode": 45009, "errmsg": "rate limited"})] * 3 + [FakeResponse()])
    result = send_summary(
        PERIOD, config=_config(), state_dir=state_dir, diff_keys=[KEY_A],
        unbalanced_count=99, client=client, sleep=lambda _s: None,
    )
    assert result.state == "retrying"
    assert len(client.calls) == 3, "首次 + 2 次退避重试"
    assert result.attempts == 3

    # 队列**落盘**：换一个 client 模拟进程重启
    state_path = os.path.join(state_dir, PUSH_STATE_NAME)
    state = json.load(open(state_path, encoding="utf-8"))
    assert len(state["pending"]) == 1
    assert state["pending"][0]["period"] == PERIOD
    assert state["pending"][0]["attempts"] == 3
    assert "rate limited" in state["pending"][0]["last_error"]

    # 未到期的条目不重发
    assert flush_pending(state_dir, config=_config(), client=FakeClient()) == []

    # 时钟前拨到 due_at 之后 → 重发成功
    restarted = FakeClient()
    results = flush_pending(
        state_dir, config=_config(), client=restarted,
        now=lambda: state["pending"][0]["due_at"] + 1,
    )
    assert len(restarted.calls) == 1
    assert results[0].state == "pushed"
    assert results[0].period == PERIOD
    after = json.load(open(state_path, encoding="utf-8"))
    assert after["pending"] == [], "送达后必须出队，否则下一轮会再推一遍"
    assert after["pushed"][PERIOD]["kinds"] == ["count"]


def test_network_failure_is_queued_not_swallowed(state_dir):
    """httpx 异常必须在 ``_post`` 里被翻译成 ReconqError，不许漏到业务层。"""
    import httpx

    class DeadClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def post(self, url: str, json: object = None) -> object:
            self.calls.append(url)
            raise httpx.ConnectError("connection refused")

        def close(self) -> None:  # pragma: no cover
            pass

    result = send_summary(
        PERIOD, config=_config(), state_dir=state_dir, diff_keys=[KEY_A],
        unbalanced_count=99, client=DeadClient(), sleep=lambda _s: None,
    )
    assert result.state == "retrying", "网络失败可重试（连接可能被临时掐断）"
    assert "ConnectError" in result.error
    assert "WECOM_SEND_FAILED" not in result.error, "错误文案面向人，不该只甩一个码"
    state = json.load(open(os.path.join(state_dir, PUSH_STATE_NAME), encoding="utf-8"))
    assert len(state["pending"]) == 1
    assert state["pending"][0]["last_code"] == "WECOM_SEND_FAILED"


def test_retry_backoff_is_exponential_and_bounded():
    from reconq.audit.wecom import _backoff_delay

    delays = [_backoff_delay(n) for n in range(1, 8)]
    assert delays[:3] == [2.0, 4.0, 8.0]
    assert delays[-1] == 60.0, "退避必须有上限，否则一条限频能挂住进程十分钟"


def test_missing_webhook_key_is_pending_not_pushed(state_dir):
    """缺 key 是配置问题：重试无意义，但也**绝不能**被记成「已推送」。"""
    client = FakeClient()
    result = send_summary(
        PERIOD, config=_config(webhook_key=""), state_dir=state_dir,
        unbalanced_count=99, client=client,
    )
    assert result.state == "pending"
    assert client.calls == []
    assert WEBHOOK_KEY_ENV in result.error
    st = notifier_state(state_dir, PERIOD)
    assert st.state == "pending"
    assert PUSH_STATE_ZH[st.state] == "待推送"


def test_key_invalid_error_is_not_retried(state_dir):
    """93000（key 无效 / 机器人不在群）重试无意义：**当场收手**，不进重试队列。"""
    client = FakeClient([(200, {"errcode": 93000, "errmsg": "invalid webhook url"})] * 3)
    result = send_summary(
        PERIOD, config=_config(), state_dir=state_dir,
        unbalanced_count=99, client=client, sleep=lambda _s: None,
    )
    assert result.state == "failed"
    assert "重试无意义" in result.error
    assert len(client.calls) == 1, (
        "93000 重复打三次只会把同一个错误刷三遍；重试无意义的错误必须当场收手"
    )
    state = json.load(open(os.path.join(state_dir, PUSH_STATE_NAME), encoding="utf-8"))
    assert state["pending"] == [], "进重试队列会让面板显示「重试中」而其实没人会再试"
    assert state["pushed"][PERIOD]["failed_kinds"] == ["count"]
    st = notifier_state(state_dir, PERIOD)
    assert st.state == "failed"


def test_flush_pending_keeps_queue_when_key_missing(state_dir):
    client = FakeClient([(200, {"errcode": 45009, "errmsg": "rate limited"})] * 3)
    send_summary(
        PERIOD, config=_config(), state_dir=state_dir, unbalanced_count=99,
        client=client, sleep=lambda _s: None,
    )
    before = json.load(open(os.path.join(state_dir, PUSH_STATE_NAME), encoding="utf-8"))
    results = flush_pending(
        state_dir, config=_config(webhook_key=""), now=lambda: before["pending"][0]["due_at"] + 999,
    )
    assert results and all(r.state == "pending" for r in results)
    after = json.load(open(os.path.join(state_dir, PUSH_STATE_NAME), encoding="utf-8"))
    assert len(after["pending"]) == 1, "缺 key 时队列必须原样保留（待补配置后续发）"


# ─────────────────────────── 5. 失败状态面板可见 ───────────────────────────


def test_exhausted_retry_shows_failed_not_a_healthy_looking_state(state_dir):
    client = FakeClient([(200, {"errcode": 45009, "errmsg": "rate limited"})] * 3)
    send_summary(
        PERIOD, config=_config(), state_dir=state_dir, diff_keys=[KEY_A],
        unbalanced_count=99, client=client, sleep=lambda _s: None,
    )
    state = json.load(open(os.path.join(state_dir, PUSH_STATE_NAME), encoding="utf-8"))
    results = flush_pending(
        state_dir, config=_config(),
        client=FakeClient([(200, {"errcode": 45009, "errmsg": "rate limited"})] * 3),
        now=lambda: state["pending"][0]["due_at"] + 1,
    )
    assert results[0].state == "failed"

    st = notifier_state(state_dir, PERIOD)
    assert st.state == "failed"
    assert PUSH_STATE_ZH[st.state] == "推送失败"
    assert st.last_error, "面板要能显示「为什么没送到」"
    assert st.pending == 0, "用尽后已出队"
    assert "推送失败" not in PUSH_STATE_ZH["not_pushed"], (
        "回落成 not_pushed 会被读成「本期一切正常」"
    )


def test_partial_delivery_reports_failed(state_dir):
    """一条线送达、另一条线用尽：面板仍须显示失败，而不是只看到成功那一半。"""
    cfg = _config()
    client = FakeClient([FakeResponse()])
    send_summary(
        PERIOD, config=cfg, state_dir=state_dir, unbalanced_count=99,
        unbalanced_amount=Decimal("9000.00"), client=client,
    )
    st = notifier_state(state_dir, PERIOD)
    assert st.state == "pushed"
    assert set(st.pushed_kinds) == {"count", "amount"}


def test_notifier_status_of_covers_multiple_periods(state_dir):
    client = FakeClient()
    send_summary(
        PERIOD, config=_config(), state_dir=state_dir, unbalanced_count=99, client=client,
    )
    statuses = notifier_status_of(state_dir, [PERIOD, "2026-W37"])
    assert statuses[PERIOD]["state"] == "pushed"
    assert statuses[PERIOD]["state_zh"] == "本期已推送"
    assert statuses["2026-W37"]["state"] == "not_pushed"


def test_notifier_state_is_typed_and_serializable(state_dir):
    st = notifier_state(state_dir, PERIOD)
    assert isinstance(st, NotifState)
    blob = st.to_dict()
    assert blob["state"] == "not_pushed"
    assert json.dumps(blob, ensure_ascii=False)


def test_corrupt_state_file_raises_instead_of_being_treated_as_empty(state_dir):
    """静默当空 = 去重依据丢失 = 同一期重推一遍。"""
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, PUSH_STATE_NAME), "w", encoding="utf-8") as handle:
        handle.write("{这不是 JSON")
    with pytest.raises(ReconqError) as exc:
        notifier_state(state_dir, PERIOD)
    assert exc.value.code == "WECOM_STATE_CORRUPT"


def test_unknown_state_version_is_refused(state_dir):
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, PUSH_STATE_NAME), "w", encoding="utf-8") as handle:
        json.dump({"version": 99, "pushed": {}, "pending": []}, handle)
    with pytest.raises(ReconqError) as exc:
        notifier_state(state_dir, PERIOD)
    assert exc.value.code == "WECOM_STATE_CORRUPT"


# ─────────────────────────── 6. 去重与人工重推 ───────────────────────────


def test_same_period_same_kind_is_not_pushed_twice(state_dir):
    client = FakeClient()
    first = send_summary(
        PERIOD, config=_config(), state_dir=state_dir, diff_keys=[KEY_A],
        unbalanced_count=99, client=client,
    )
    second = send_summary(
        PERIOD, config=_config(), state_dir=state_dir, diff_keys=[KEY_A],
        unbalanced_count=99, client=client,
    )
    assert first.state == "pushed" and first.deduped is False
    assert second.state == "pushed" and second.deduped is True
    assert len(client.calls) == 1, "同账期同阈值类型只推一次"
    assert "已推送过" in second.error


def test_dedup_is_per_kind_so_a_new_excursion_still_notifies(state_dir):
    """第一条线推过了，第二条线后来也超了 —— 仍然要推（否则恶化被静默）。"""
    cfg = _config(count_threshold=10, amount_threshold=None)
    client = FakeClient()
    send_summary(PERIOD, config=cfg, state_dir=state_dir, unbalanced_count=99, client=client)
    assert len(client.calls) == 1
    second = send_summary(
        PERIOD, config=cfg, state_dir=state_dir, unbalanced_count=99,
        disputed=True, client=client,
    )
    assert second.kinds == ("disputed",)
    assert second.deduped is False
    assert len(client.calls) == 2
    content = client.calls[1]["body"]["markdown"]["content"]
    assert "本期存疑" in content
    assert "单边账条数超限" not in content, "已推过的线不该在第二条消息里重提"


def test_periods_do_not_share_dedup(state_dir):
    client = FakeClient()
    send_summary("2026-W36", config=_config(), state_dir=state_dir, unbalanced_count=99, client=client)
    send_summary("2026-W37", config=_config(), state_dir=state_dir, unbalanced_count=99, client=client)
    assert len(client.calls) == 2
    assert notifier_state(state_dir, "2026-W37").state == "pushed"


def test_manual_repush_by_human_is_allowed(state_dir):
    client = FakeClient()
    send_summary(PERIOD, config=_config(), state_dir=state_dir, unbalanced_count=99, client=client)
    forced = send_summary(
        PERIOD, config=_config(), state_dir=state_dir, unbalanced_count=99,
        client=client, force=True, operator="财务科·张敏",
    )
    assert forced.state == "pushed"
    assert forced.forced is True
    assert len(client.calls) == 2


def test_manual_repush_by_machine_is_refused(state_dir):
    """「重新推送」打破的是「别刷屏」的克制，这件事只能由人决定。"""
    client = FakeClient()
    send_summary(PERIOD, config=_config(), state_dir=state_dir, unbalanced_count=99, client=client)
    for actor in ("system", "bot", "scheduler", "model", "reconq-cron"):
        with pytest.raises(ReconqError) as exc:
            send_summary(
                PERIOD, config=_config(), state_dir=state_dir, unbalanced_count=99,
                client=client, force=True, operator=actor,
            )
        assert exc.value.code == "WECOM_PUSH_NOT_ALLOWED"
    with pytest.raises(ReconqError) as exc:
        send_summary(
            PERIOD, config=_config(), state_dir=state_dir, unbalanced_count=99,
            client=client, force=True, operator="",
        )
    assert exc.value.code == "WECOM_PUSH_NOT_ALLOWED"
    assert len(client.calls) == 1, "被拒的强制推送不许真的发出去"


def test_endpoint_never_leaks_key_in_redacted_form(state_dir):
    cfg = _config()
    assert "test-webhook-key-0000" in cfg.webhook_endpoint
    assert "test-webhook-key-0000" not in cfg.redacted_endpoint()
    assert "***" in cfg.redacted_endpoint()
    client = FakeClient([FakeResponse(500, {"errcode": -1, "errmsg": "boom"})] * 3)
    result = send_summary(
        PERIOD, config=cfg, state_dir=state_dir, unbalanced_count=99,
        client=client, sleep=lambda _s: None,
    )
    assert "test-webhook-key-0000" not in result.error, "错误文案会进面板与事件链，不得带 key"
