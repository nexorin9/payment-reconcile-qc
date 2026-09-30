"""企业微信超阈值推送、深链落地与失败重试入队（Task 46）。

「推送出去」不等于「人看到了」
--------------------------------
一条 ``差异 37 条待处置`` 推到企微群，最容易出的事是**以为推了其实没推**：
机器人已被移出群、key 轮换了、限频 45009、进程在退避中途被杀 —— 四种情况
在界面上长得一模一样（都是「没收到提醒」）。财务科周一早到办公室只看到
零星几条未处置差异时，**没有人会想到去查机器人**。

所以本模块把「推送」当成一件**有状态的事**而不是一次 HTTP 调用：

============================  ==================================================
落点                          回答的问题
============================  ==================================================
:func:`evaluate_threshold`    这一期该不该推？超了哪条线？
:func:`send_summary`          推了没有？推成功没有？
:func:`notifier_state`        面板上那行字现在是什么？—— 见 :data:`PUSH_STATE_ZH`
:func:`flush_pending`         上次没送出去的补上没有？
:func:`build_deep_link`       手机上点开能不能直达那条差异？
============================  ==================================================

五条不肯让的纪律
----------------
1. **未超阈值零调用。** 阈值判定在**构造 HTTP 客户端之前**：不超阈值时连
   webhook key 都不读（:func:`reconq.errors` 的 ``WECOM_WEBHOOK_KEY_MISSING``
   不会在一期正常账上冒出来）。静默推送一条「本期无异常」是最坏结果 ——
   它把真正的异常淹在每天一条的噪音里。
2. **推送内容不含明细。** 企微消息会被转发、截图、存进同事的手机相册。
   :func:`assert_payload_clean` 在**发请求之前**核对：只允许差异条数、
   超阈值类型、面板地址与深链；出现金额数字、单号或患者标识形态即
   :class:`~reconq.errors.ReconqError` 拒发（抛错而不是脱敏后照发 ——
   脱敏后的半截消息会让读的人以为系统算错了）。
3. **失败必须留痕并可重发。** HTTP 失败 / 限频 → 落 pending 队列，
   最多 3 次指数退避，队列**落盘**（进程被杀不丢），重启后
   :func:`flush_pending` 续发。用尽仍失败则状态是「推送失败」，
   **绝不**写成「已推送」。
4. **去重按「账期 × 超阈值类型」。** 同一期同一条线不重复推；人工点
   「重新推送」可强制再推（:func:`send_summary` 的 ``force``），但那条
   动作只接受自然人（:func:`_assert_human_operator`）。
5. **深链带账期与差异键。** ``reconq://period/<period>/diff/<key>``，
   :func:`parse_deep_link` 解析回 ``(period, key)``。差异键就是台账那一行
   的业务键（``period|channel|check_path``），所以深链落到的是**处置单**
   而不是「本期第 3 条」——后者在第二次推送后就指错行了。

真实协议
--------
``POST https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<WECOM_WEBHOOK_KEY>``
body ``{"msgtype":"markdown", "markdown": {"content": "..."}}``，
响应 ``{"errcode": 0, "errmsg": "ok"}``；``errcode`` 非 0 即失败。
凭据只从环境变量 ``WECOM_WEBHOOK_KEY`` 读，无硬编码。

与审计链的关系
--------------
推送成功 / 入队 / 重试用尽都应各落一条 :mod:`reconq.audit.query` 事件
（``RUN_DEGRADED`` 等）。本模块**不自己写事件链** —— 它只返回状态，
由调用方（扫描编排 / 面板）决定上不上链，避免同一个 ``seq`` 被两处写。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional, Sequence

from ..errors import ReconqError

__all__ = [
    "WEBHOOK_URL",
    "WEBHOOK_KEY_ENV",
    "COUNT_THRESHOLD_ENV",
    "AMOUNT_THRESHOLD_ENV",
    "PANEL_BASE_ENV",
    "PUSH_STATE_NAME",
    "PUSH_STATE_ZH",
    "PUSH_STATES",
    "THRESHOLD_KINDS",
    "THRESHOLD_KIND_ZH",
    "DEEP_LINK_SCHEME",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_PANEL_BASE",
    "WeComConfig",
    "WeComResult",
    "PendingItem",
    "NotifState",
    "build_deep_link",
    "parse_deep_link",
    "evaluate_threshold",
    "assert_payload_clean",
    "build_payload",
    "send_summary",
    "flush_pending",
    "notifier_state",
    "notifier_status_of",
]


# ─────────────────────────── 常量 ───────────────────────────


#: 企业微信群机器人 webhook 地址。**不带 key** —— key 从环境变量读后拼上去，
#: 拼进 URL 而不是拼进 body 是企微机器人的协议本身如此。
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

#: 凭据位。企微机器人的 key 就是「群机器人地址末尾那段 uuid」，
#: 它同时是凭据与路由，所以**不落配置文件、不进日志、不进状态文件**。
WEBHOOK_KEY_ENV = "WECOM_WEBHOOK_KEY"

#: 单边账条数阈值。超了才推。
COUNT_THRESHOLD_ENV = "RECONQ_WECOM_UNBALANCED_COUNT_THRESHOLD"

#: 单边账金额阈值（定点数，**不是 float**）。超了才推。
AMOUNT_THRESHOLD_ENV = "RECONQ_WECOM_UNBALANCED_AMOUNT_THRESHOLD"

#: 面板基址。深链与消息正文里的面板地址都用它；本产品只监听 127.0.0.1，
#: 企微客户端拿到 ``reconq://`` 协议链接后由本机面板接住。
PANEL_BASE_ENV = "RECONQ_PANEL_BASE"

#: 面板基址默认值。与 Task 47 的 ``reconq serve`` 默认监听地址一致。
DEFAULT_PANEL_BASE = "http://127.0.0.1:8788"

#: 状态文件名（相对状态目录）。
PUSH_STATE_NAME = "wecom_state.json"

#: 状态格式版本。不认识就当没有（不静默按旧版解析）。
STATE_VERSION = 1

#: 推送状态 → 中文。**面板那行字与 CLI 输出共用这一份** ——
#: 两处各写一遍，「已推送」和「以为已推送」的差别就是这么来的。
PUSH_STATE_ZH: dict[str, str] = {
    "not_pushed": "本期未推送（未超阈值）",
    "pending": "待推送",
    "retrying": "推送失败重试中",
    "pushed": "本期已推送",
    "failed": "推送失败",
}

PUSH_STATES = frozenset(PUSH_STATE_ZH)

#: 超阈值类型。三条线各自独立判定、各自独立去重 ——
#: 「条数超了但金额没超」和「金额超了但条数没超」要能分别说清。
THRESHOLD_KINDS = ("count", "amount", "disputed")

THRESHOLD_KIND_ZH: dict[str, str] = {
    "count": "单边账条数超限",
    "amount": "单边账金额超限",
    "disputed": "本期存疑（已存疑待主管复核）",
}

#: 深链协议前缀。与 Task 47 面板路由 ``/period/<period>/diff/<key>`` 一一对应。
DEEP_LINK_SCHEME = "reconq://"

#: 失败重试上限（不含首次发送）。3 次 = 首次 + 2 次重试，
#: 与 :data:`reconq.sources.retry.DEFAULT_MAX_ATTEMPTS` 同档。
DEFAULT_MAX_ATTEMPTS = 3

#: 允许出现在消息正文里的键。**白名单而非黑名单** —— 拼正文时多带一个字段
#: 就得多一条黑名单规则，而白名单多写一个键的代价是显式的。
ALLOWED_PAYLOAD_KEYS = frozenset({"period", "kinds", "unbalanced_count", "panel_url", "deep_links"})

#: 消息正文里**禁止出现金额形态**。理由不是金额敏感，而是「37 条 / 12.34 元」
#: 这种半明细消息会让人以为推送内容是完整的：读到 12.34 元却找不到对应的那条
#: 差异，下一次就没人信这条推送了。要金额就去面板看。
_AMOUNT_PATTERN = r"\d[\d,]*\.\d{2}\s*元|\d[\d,]*\.\d{2}\s*¥|[¥￥]\s*\d[\d,]*\.\d{2}"


# ─────────────────────────── 配置 ───────────────────────────


@dataclass(frozen=True)
class WeComConfig:
    """企微推送配置。**全部来自环境变量**，无文件、无硬编码默认值之外的口径。"""

    #: webhook key。空串表示未配置 —— 那是「不推送」而不是「推送空消息」。
    webhook_key: str = ""
    #: 单边账条数阈值。``None`` 表示未配置（该条线不参与判定）。
    count_threshold: Optional[int] = None
    #: 单边账金额阈值（定点）。``None`` 表示未配置。
    amount_threshold: Optional[Decimal] = None
    #: 面板基址。
    panel_base: str = DEFAULT_PANEL_BASE
    #: 失败重试上限。
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: 请求超时（秒）。
    timeout: float = 5.0

    @property
    def webhook_endpoint(self) -> str:
        """完整 webhook 地址。**调用方不得打印它** —— key 在里面。"""
        return f"{WEBHOOK_URL}?key={self.webhook_key}"

    def redacted_endpoint(self) -> str:
        """可安全落日志 / 进事件链的地址：key 位置固定打码。"""
        return f"{WEBHOOK_URL}?key=***"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "WeComConfig":
        """从环境变量装配。**阈值解析失败即抛错**，不落一个静默的错口径。

        为什么阈值写错必须报错而不是忽略：``RECONQ_WECOM_UNBALANCED_AMOUNT_THRESHOLD=5000``
        少写了 ``.00`` 时，若按 float 解析会变成 5000.0（看起来对），按 Decimal
        字符串解析则该报错 —— 而 ``abc`` 若被静默忽略，阈值就变成「不限制」，
        结果是**每期都推**。这两种错法一个多推、一个滥推，都比报错糟。
        """
        source = os.environ if env is None else env
        return cls(
            webhook_key=(source.get(WEBHOOK_KEY_ENV) or "").strip(),
            count_threshold=_env_count_threshold(source.get(COUNT_THRESHOLD_ENV)),
            amount_threshold=_env_amount_threshold(source.get(AMOUNT_THRESHOLD_ENV)),
            panel_base=(source.get(PANEL_BASE_ENV) or "").strip() or DEFAULT_PANEL_BASE,
        )


def _env_count_threshold(raw: Optional[str]) -> Optional[int]:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        raise ReconqError(
            "WECOM_THRESHOLD_INVALID",
            f"{COUNT_THRESHOLD_ENV}={text!r} 不是整数。"
            "条数阈值只写非负整数，不要写成「10 条」「10,000」或「10.0」",
        ) from None
    if value < 0:
        raise ReconqError(
            "WECOM_THRESHOLD_INVALID",
            f"{COUNT_THRESHOLD_ENV}={value} 为负数。负阈值会让**每一期都超限**，"
            "包括完全对得上的干净账期",
        )
    return value


def _env_amount_threshold(raw: Optional[str]) -> Optional[Decimal]:
    text = (raw or "").strip()
    if not text:
        return None
    cleaned = text.replace("，", "").replace(",", "")
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        raise ReconqError(
            "WECOM_THRESHOLD_INVALID",
            f"{AMOUNT_THRESHOLD_ENV}={text!r} 不是定点数。金额阈值写 "
            "「5000」或「5000.00」，不带货币符号与千分位",
        ) from None
    if value < 0:
        raise ReconqError(
            "WECOM_THRESHOLD_INVALID",
            f"{AMOUNT_THRESHOLD_ENV}={value} 为负数。负金额阈值与负条数阈值同理："
            "每期都会超限",
        )
    return value


# ─────────────────────────── 阈值判定 ───────────────────────────


@dataclass(frozen=True)
class ThresholdVerdict:
    """阈值判定结果。"""

    period: str
    #: 命中的超阈值类型（:data:`THRESHOLD_KINDS` 的子集，保序）。
    kinds: tuple[str, ...]
    #: 是否该推。``kinds`` 非空即 True；显式保留这个字段是为了让调用方不必
    #: 写 ``if verdict.kinds:``——那种写法在「超了但 types 是空」的改动下会静默漏推。
    should_push: bool
    #: 参与判定的口径快照（供面板显示「为什么推」）。
    count: int = 0
    amount: Optional[Decimal] = None
    count_threshold: Optional[int] = None
    amount_threshold: Optional[Decimal] = None
    disputed: bool = False

    def kind_zh(self) -> tuple[str, ...]:
        return tuple(THRESHOLD_KIND_ZH.get(k, k) for k in self.kinds)

    def reason_zh(self) -> str:
        """一句中文说明「为什么推 / 为什么不推」，直接给面板显示。

        刻意区分「**配了阈值但没超**」与「**根本没配阈值**」：这两者都导致
        不推送，但在面板上是两件事 —— 前者说明「本期差异在容忍范围内」，
        后者说明「阈值还没人配」，后者需要有人去配。
        """
        if self.kinds:
            return "、".join(self.kind_zh())
        if self.count_threshold is None and self.amount_threshold is None:
            return "未配置任何阈值，本期不推送"
        parts: list[str] = []
        if self.count_threshold is not None:
            parts.append(f"单边账 {self.count} 条未超条数阈值 {self.count_threshold}")
        if self.amount_threshold is not None:
            got = "未取到金额" if self.amount is None else f"{self.amount:.2f} 元"
            parts.append(f"{got}未超金额阈值 {self.amount_threshold:.2f} 元")
        if not parts:
            return "本期无存疑，未超任何阈值"
        return "；".join(parts) + "，本期不推送"


def evaluate_threshold(
    period: str,
    *,
    unbalanced_count: int,
    unbalanced_amount: Optional[Decimal] = None,
    disputed: bool = False,
    config: Optional[WeComConfig] = None,
) -> ThresholdVerdict:
    """判定这一期该不该推送，以及超了哪条线。

    三条线**独立判定**：条数超了不该因为金额没超就不推，反之同理。合并成一个
    布尔会让「条数 38（阈值 30）但合计 12 元」这种差异被悄悄放过 —— 而 38 条
    挂着的对账单正是财务科最需要被叫醒的。

    阈值未配置（``None``）的线**不参与判定**，并在 :attr:`ThresholdVerdict`
    里留下 ``None`` 以便面板显示「该线未配阈值」。
    """
    cfg = config or WeComConfig()
    if not period:
        raise ReconqError(
            "WECOM_THRESHOLD_INVALID",
            "阈值判定需要账期（形如 2026-W36）。没有账期就无法去重 —— "
            "同一份摘要会每期重推一次",
        )
    kinds: list[str] = []
    if cfg.count_threshold is not None and unbalanced_count > cfg.count_threshold:
        kinds.append("count")
    if cfg.amount_threshold is not None and unbalanced_amount is not None:
        if unbalanced_amount > cfg.amount_threshold:
            kinds.append("amount")
    if disputed:
        kinds.append("disputed")
    return ThresholdVerdict(
        period=period,
        kinds=tuple(kinds),
        should_push=bool(kinds),
        count=unbalanced_count,
        amount=unbalanced_amount,
        count_threshold=cfg.count_threshold,
        amount_threshold=cfg.amount_threshold,
        disputed=disputed,
    )


# ─────────────────────────── 深链 ───────────────────────────


def build_deep_link(period: str, diff_key: str) -> str:
    """构造深链：``reconq://period/<period>/diff/<key>``。

    ``key`` 里的 ``|`` 与 ``:``（差异键形如 ``2026-W36|wechat|taxonomy.missing:EX-9``）
    在 URL path 里是合法字符，因此**不做百分号编码** —— 编码后财务科在手机上
    看到的是一串 ``%7C``，而企微与本机面板都能直接吃未编码形式。
    解析侧 :func:`parse_deep_link` 对两种形式都容忍。
    """
    if not period or not diff_key:
        raise ReconqError(
            "WECOM_PAYLOAD_REJECTED",
            f"深链需要账期与差异键，收到 period={period!r} key={diff_key!r}。"
            "只给一个空的深链比不给更糟：财务科点开会落到一个空页面，"
            "然后以为这期没差异",
        )
    return f"{DEEP_LINK_SCHEME}period/{period}/diff/{diff_key}"


def parse_deep_link(link: str) -> tuple[str, str]:
    """把深链解析回 ``(period, diff_key)``。解析不了即抛错，不返回空串。

    面板收到一个解析不了的深链时**必须显式失败**：静默忽略的话，用户点了
    链接什么也没发生，界面不跳转也不会报错 —— 症状是「链接是坏的」，
    而真相是「链接指向的差异已消解」。这两件事的处理完全不同。
    """
    text = (link or "").strip()
    if not text.startswith(DEEP_LINK_SCHEME):
        raise ReconqError(
            "WECOM_PAYLOAD_REJECTED",
            f"{link!r} 不是 {DEEP_LINK_SCHEME} 协议深链。支持的形态是 "
            f"{DEEP_LINK_SCHEME}period/<账期>/diff/<差异键>",
        )
    remainder = text[len(DEEP_LINK_SCHEME) :]
    parts = remainder.split("/", 3)
    if len(parts) < 4 or parts[0] != "period" or parts[2] != "diff" or not parts[1] or not parts[3]:
        raise ReconqError(
            "WECOM_PAYLOAD_REJECTED",
            f"{link!r} 的深链结构不完整。应为 "
            f"{DEEP_LINK_SCHEME}period/<账期>/diff/<差异键>，"
            f"实际拆出 {parts!r}",
        )
    return parts[1], parts[3]


# ─────────────────────────── 消息内容约束 ───────────────────────────


def assert_payload_clean(payload: Mapping[str, Any]) -> None:
    """核对推送载荷**不含金额明细与患者可识别信息**。命中即抛错拒发。

    为什么是「抛错」而不是「脱敏后照发」：脱敏意味着发出去一条**内容不完整**
    的提醒，而提醒的接收方无法知道它被截断过 —— 下一期他们就不信这条推送了。
    这里宁可让本期待推，也让调用方改对（与 :mod:`reconq.audit.query` 的
    ``assert_event_deidentified`` 同一道纪律，只是对象从审计事件换成外发消息）。

    两条检查：

    1. **结构**：键必须在 :data:`ALLOWED_PAYLOAD_KEYS` 白名单内（多带一个键就报错）；
    2. **形态**：值里不得出现金额形态，且不得命中患者标识形态
       （复用 :func:`reconq.runlog.redact_text` 的模式表，与结论单、审计链同源）。
    """
    unknown = sorted(set(payload) - ALLOWED_PAYLOAD_KEYS)
    if unknown:
        raise ReconqError(
            "WECOM_PAYLOAD_REJECTED",
            f"推送载荷出现白名单外的键 {unknown}。允许的键只有 "
            f"{sorted(ALLOWED_PAYLOAD_KEYS)} —— 企微消息会被转发与截图，"
            "明细请留在面板与结论单里",
        )

    from ..align.snapshot import FORBIDDEN_PATIENT_KEYS
    from ..runlog import redact_text

    for key, value in payload.items():
        if str(key).strip().lower() in FORBIDDEN_PATIENT_KEYS:
            raise ReconqError(
                "WECOM_PAYLOAD_REJECTED",
                f"推送载荷的字段 {key!r} 是患者可识别字段：企微群消息不带患者明细",
            )
        for text in _iter_texts(value):
            if _contains_amount(text):
                raise ReconqError(
                    "WECOM_PAYLOAD_REJECTED",
                    f"推送载荷字段 {key!r} 的文本含金额形态。企微消息只给条数与"
                    "超阈值类型，不给金额 —— 给出金额却指不到具体哪一条，"
                    "下一期就没人信这条推送了",
                )
            _, hits = redact_text(text)
            if hits:
                raise ReconqError(
                    "WECOM_PAYLOAD_REJECTED",
                    f"推送载荷字段 {key!r} 的文本命中患者标识形态 {sorted(set(hits))}："
                    "企微群消息会被转发与截图，患者信息不能随消息流转",
                )


def _iter_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _iter_texts(item)]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _iter_texts(item)]
    return []


def _contains_amount(text: str) -> bool:
    import re

    return re.search(_AMOUNT_PATTERN, text) is not None


# ─────────────────────────── 消息构造 ───────────────────────────


def build_payload(
    verdict: ThresholdVerdict,
    *,
    diff_keys: Sequence[str] = (),
    panel_base: Optional[str] = None,
    max_links: int = 20,
) -> dict[str, Any]:
    """按判定结果构造 markdown 推送载荷（**不含**明细）。

    ``max_links`` 限制深链条数：企微单条消息有长度上限，而 200 条差异
    全列出来既超长又没人点。超出的部分只给条数，面板里看全量。
    """
    if not verdict.should_push:
        raise ReconqError(
            "WECOM_PAYLOAD_REJECTED",
            f"账期 {verdict.period} 的判定结果是「不推送」（{verdict.reason_zh()}），"
            "却要求构造推送载荷。未超阈值不推送是纪律，不是可覆盖的默认值",
        )
    base = (panel_base or DEFAULT_PANEL_BASE).rstrip("/")
    keys = [str(k) for k in diff_keys if str(k).strip()]
    shown = keys[:max_links]
    payload: dict[str, Any] = {
        "period": verdict.period,
        "kinds": list(verdict.kinds),
        "unbalanced_count": verdict.count,
        "panel_url": f"{base}/period/{verdict.period}",
        "deep_links": [build_deep_link(verdict.period, key) for key in shown],
    }
    assert_payload_clean(payload)
    return payload


def render_markdown(payload: Mapping[str, Any], *, truncated: int = 0) -> str:
    """把载荷渲染成企微 markdown 正文。

    与 :func:`build_payload` 的内容约束一致：正文里出现的东西都必须在载荷里，
    渲染阶段**不再引入**新信息（金额、科室、单号都不在正文里）。
    """
    period = str(payload.get("period", ""))
    kinds = "、".join(THRESHOLD_KIND_ZH.get(str(k), str(k)) for k in payload.get("kinds", ()))
    count = int(payload.get("unbalanced_count", 0) or 0)
    lines = [
        f"**支付对账 {period} 超阈值提醒**",
        f"> 超阈值类型：{kinds}",
        f"> 单边账：{count} 条待处置",
        f"> 面板：{payload.get('panel_url', '')}",
    ]
    links = list(payload.get("deep_links", ()))
    if links:
        lines.append("> 点开直达处置单：")
        lines.extend(f"> - {link}" for link in links)
    if truncated:
        lines.append(f"> 其余 {truncated} 条请在面板查看（正文只列前若干条，避免刷屏）")
    lines.append("")
    lines.append("（本条只给条数与入口，不含金额明细；差异处置需人工确认）")
    return "\n".join(lines)


# ─────────────────────────── 状态与结果 ───────────────────────────


@dataclass
class WeComResult:
    """一次推送尝试的结果。**每个字段都要能在面板上显示出来。**"""

    period: str
    #: 推送后落定的状态（:data:`PUSH_STATES` 之一）。
    state: str
    #: 本次判定命中的超阈值类型。
    kinds: tuple[str, ...] = ()
    #: 实际发出的 HTTP 次数（失败重试也计入）。
    attempts: int = 0
    #: 成功那次返回的 errcode（企微业务码；HTTP 层失败时为 ``None``）。
    errcode: Optional[int] = None
    #: 失败原因（可读中文）。
    error: str = ""
    #: 是否被去重挡下（True 表示「这一期这条线之前推过」）。
    deduped: bool = False
    #: 是否走了人工「重新推送」强制通道。
    forced: bool = False

    @property
    def delivered(self) -> bool:
        return self.state == "pushed"

    @property
    def state_zh(self) -> str:
        return PUSH_STATE_ZH.get(self.state, f"（未登记状态 {self.state}）")

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "state": self.state,
            "state_zh": self.state_zh,
            "kinds": list(self.kinds),
            "attempts": self.attempts,
            "errcode": self.errcode,
            "error": self.error,
            "deduped": self.deduped,
            "forced": self.forced,
        }


@dataclass
class PendingItem:
    """待重发队列里的一条。**落盘后进程重启不丢**。"""

    period: str
    kinds: tuple[str, ...]
    payload: dict[str, Any]
    #: 已尝试次数（含首次）。
    attempts: int = 0
    #: 下次可重试的时刻（epoch 秒）。取自注入时钟，测试可伪造。
    due_at: float = 0.0
    #: 上次失败的可读原因。
    last_error: str = ""
    #: 上次失败的错误码。
    last_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "kinds": list(self.kinds),
            "payload": self.payload,
            "attempts": self.attempts,
            "due_at": self.due_at,
            "last_error": self.last_error,
            "last_code": self.last_code,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PendingItem":
        return cls(
            period=str(raw.get("period", "")),
            kinds=tuple(str(k) for k in raw.get("kinds", ())),
            payload=dict(raw.get("payload") or {}),
            attempts=int(raw.get("attempts", 0) or 0),
            due_at=float(raw.get("due_at", 0.0) or 0.0),
            last_error=str(raw.get("last_error", "")),
            last_code=str(raw.get("last_code", "")),
        )


@dataclass
class NotifState:
    """某账期的推送状态全貌（面板「推送状态」那一行读它）。"""

    period: str
    state: str
    kinds: tuple[str, ...] = ()
    #: 已成功推送过的超阈值类型（去重真源）。
    pushed_kinds: tuple[str, ...] = ()
    #: 待重发队列中该期的条数。
    pending: int = 0
    #: 上次失败原因（无则空串）。
    last_error: str = ""
    #: 判定但未推送时的说明（如「未配置任何阈值」）。
    note: str = ""

    @property
    def state_zh(self) -> str:
        return PUSH_STATE_ZH.get(self.state, f"（未登记状态 {self.state}）")

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "state": self.state,
            "state_zh": self.state_zh,
            "kinds": list(self.kinds),
            "pushed_kinds": list(self.pushed_kinds),
            "pending": self.pending,
            "last_error": self.last_error,
            "note": self.note,
        }


# ─────────────────────────── 状态文件 ───────────────────────────


def _state_paths(state_dir: str) -> str:
    base = str(state_dir or "").strip()
    if not base:
        raise ReconqError(
            "AUDIT_LOG_DIR_REQUIRED",
            "未指定状态目录。企微推送的去重与重试队列都落在 "
            f"{PUSH_STATE_NAME}，不写则「以为推了其实没推」无从查证",
        )
    return os.path.join(base, PUSH_STATE_NAME)


def _load_state(state_dir: str) -> dict[str, Any]:
    """读状态文件。**坏掉时抛错，不静默当空**。

    静默当空的后果是重复推送：去重依据（``pushed``）丢了，同一期会再推一遍。
    多推一次的代价远小于「以为已推送其实没推」被当成常态。
    """
    path = _state_paths(state_dir)
    if not os.path.exists(path):
        return {"version": STATE_VERSION, "pushed": {}, "pending": []}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ReconqError(
            "WECOM_STATE_CORRUPT",
            f"{path} 无法解析：{type(exc).__name__}。该文件是去重与重试队列的"
            "唯一真源，坏掉会导致重复推送或丢失待办；请交信息科恢复，"
            "不要就地删行",
        ) from exc
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        raise ReconqError(
            "WECOM_STATE_CORRUPT",
            f"{path} 的版本是 {raw.get('version') if isinstance(raw, dict) else '非对象'}，"
            f"本版本只认 {STATE_VERSION}。不静默按旧版解析 —— 旧版的字段语义"
            "可能已经变了",
        )
    raw.setdefault("pushed", {})
    raw.setdefault("pending", [])
    return raw


def _save_state(state_dir: str, state: Mapping[str, Any]) -> None:
    """原子写状态文件：先写临时文件再 ``os.replace``（与审计索引同一条纪律）。"""
    path = _state_paths(state_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _pushed_entry(state: Mapping[str, Any], period: str) -> dict[str, Any]:
    entry = state["pushed"].get(period)
    if not isinstance(entry, dict):
        entry = {"kinds": [], "at": ""}
    return entry


# ─────────────────────────── 发送 ───────────────────────────


def _assert_human_operator(operator: Optional[str], *, where: str) -> None:
    """「重新推送」只接受自然人。与台账状态机同一道边界。

    允许的值形态是**站内工号 / 中文姓名 / 英文姓名**；``system`` / ``bot`` /
    ``scheduler`` / ``model`` 这类机器操作人一律拒 —— 因为「重新推送」意味着
    绕过「别刷屏」的克制，而克制这件事只能由人决定要不要破例。
    """
    text = (operator or "").strip()
    if not text:
        raise ReconqError(
            "WECOM_PUSH_NOT_ALLOWED",
            f"{where} 需要给出操作人。空操作人会让「谁点的重新推送」在"
            "审计链上永远查不到",
        )
    lowered = text.lower()
    for reserved in ("system", "bot", "scheduler", "cron", "model", "auto", "reconq"):
        if lowered == reserved or lowered.startswith(reserved + "-") or lowered.startswith(reserved + "_"):
            raise ReconqError(
                "WECOM_PUSH_NOT_ALLOWED",
                f"操作人 {text!r} 是机器账号。「重新推送」只能由人显式发起 —— "
                "定时任务与模型不得代为打破「同账期同阈值只推一次」的克制",
            )


def _post(
    config: WeComConfig,
    content: str,
    *,
    client: Any,
) -> tuple[int, dict[str, Any]]:
    """执行一次真实 webhook POST。返回 ``(http_status, 响应 JSON)``。

    httpx 的异常一律转成 :class:`ReconqError`：调用方不该在面板分支里
    见到 ``httpx.ConnectError`` 这种漏到业务层的库异常。
    """
    import httpx

    body = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        response = client.post(config.webhook_endpoint, json=body)
    except httpx.TimeoutException as exc:
        raise ReconqError(
            "WECOM_SEND_FAILED",
            f"企微推送读超时（{config.timeout:g} 秒）：{type(exc).__name__}。"
            "推送已入 pending 队列，稍后重试",
        ) from exc
    except httpx.HTTPError as exc:
        raise ReconqError(
            "WECOM_SEND_FAILED",
            f"企微推送网络失败：{type(exc).__name__}。推送已入 pending 队列，稍后重试",
        ) from exc
    status = int(getattr(response, "status_code", 0))
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {"errcode": None, "errmsg": "响应不是 JSON 对象"}
    return status, data


def _business_error(status: int, data: Mapping[str, Any], *, endpoint: str) -> Optional[ReconqError]:
    """把 HTTP 层与业务层的失败翻译成错误码。成功返回 ``None``。"""
    errcode = data.get("errcode")
    errmsg = str(data.get("errmsg", ""))
    if status >= 400:
        if status == 429:
            return ReconqError(
                "WECOM_RATE_LIMITED",
                f"企微返回 HTTP 429：{errmsg or '触发频率限制'}",
            )
        return ReconqError(
            "WECOM_SEND_FAILED",
            f"企微返回 HTTP {status}：{errmsg or '无 errmsg'}（{endpoint}）",
        )
    if errcode in (None, 0, "0"):
        return None
    if errcode in (45009, -1, 45009.0):
        return ReconqError(
            "WECOM_RATE_LIMITED",
            f"企微 errcode={errcode}：{errmsg or '触发频率限制'}。"
            "同一机器人每分钟 20 条上限，已入队按指数退避重发",
        )
    if errcode in (93000, 40001, 40014, 40058, 41001):
        return ReconqError(
            "WECOM_WEBHOOK_INVALID",
            f"企微 errcode={errcode}：{errmsg or 'webhook key 无效或机器人不在群里'}。"
            "这类错误重试无意义，请确认 WECOM_WEBHOOK_KEY 与群机器人仍在群内",
        )
    return ReconqError(
        "WECOM_SEND_FAILED",
        f"企微 errcode={errcode}：{errmsg or '未知业务错误'}",
    )


def _backoff_delay(attempts: int, *, base: float = 2.0, cap: float = 60.0) -> float:
    """第 ``attempts`` 次失败后的等待秒数（指数退避 + 上限）。"""
    return min(base * (2 ** max(0, attempts - 1)), cap)


def send_summary(
    period: str,
    *,
    config: Optional[WeComConfig] = None,
    state_dir: str,
    diff_keys: Sequence[str] = (),
    unbalanced_count: int = 0,
    unbalanced_amount: Optional[Decimal] = None,
    disputed: bool = False,
    client: Any = None,
    now: Callable[[], float] = time.time,
    sleep: Optional[Callable[[float], None]] = None,
    force: bool = False,
    operator: Optional[str] = None,
    max_links: int = 20,
) -> WeComResult:
    """判定阈值 → 去重 → 发送 → 落状态。**这是面板与编排层的唯一入口。**

    调用顺序刻意固定成这样，理由是每一步都可能提前返回：

    1. **阈值判定先于一切**（含读 key）。未超阈值直接返回 ``not_pushed``，
       **零 HTTP 调用、零状态写入**；
    2. **人工边界**：``force=True`` 时校验操作人是自然人；
    3. **去重**：该期该类型已推过且未 ``force`` → 返回 ``deduped=True``；
    4. **内容约束**：构造载荷后 :func:`assert_payload_clean` 抛错即拒发；
    5. **缺凭据**：不入队、不假装推送，返回 ``pending`` 并说明原因 ——
       缺 key 是配置问题，重试多少次都不会好，但它也**不能**被记成「已推送」；
    6. **发送**：失败按指数退避就地重试（注入 ``sleep``，测试不真等），
       用尽则入 pending 队列并落盘。
    """
    cfg = config or WeComConfig()
    verdict = evaluate_threshold(
        period,
        unbalanced_count=unbalanced_count,
        unbalanced_amount=unbalanced_amount,
        disputed=disputed,
        config=cfg,
    )
    if not verdict.should_push:
        return WeComResult(
            period=period,
            state="not_pushed",
            kinds=(),
            error=verdict.reason_zh(),
        )

    if force:
        _assert_human_operator(operator, where="重新推送")

    state = _load_state(state_dir)
    entry = _pushed_entry(state, period)
    already = set(str(k) for k in entry.get("kinds", ()))
    fresh = [k for k in verdict.kinds if k not in already]
    if not fresh and not force:
        return WeComResult(
            period=period,
            state="pushed",
            kinds=verdict.kinds,
            deduped=True,
            error=f"该期已推送过：{'、'.join(THRESHOLD_KIND_ZH.get(k, k) for k in already)}",
        )
    # 只有「新命中」的超阈值类型进消息正文：已推过的线重推一遍只会让
    # 财务科以为那条线又恶化了一次。
    effective = ThresholdVerdict(
        period=verdict.period,
        kinds=tuple(fresh or verdict.kinds),
        should_push=True,
        count=verdict.count,
        amount=verdict.amount,
        count_threshold=verdict.count_threshold,
        amount_threshold=verdict.amount_threshold,
        disputed=verdict.disputed,
    )

    payload = build_payload(effective, diff_keys=diff_keys, panel_base=cfg.panel_base, max_links=max_links)
    content = render_markdown(payload, truncated=max(0, len(diff_keys) - len(payload["deep_links"])))

    if not cfg.webhook_key:
        # 缺凭据：**不推送、不入队**（重试无意义），但状态必须是 pending ——
        # 面板上「待推送（未配置 WECOM_WEBHOOK_KEY）」比空白有用得多。
        _remember_failure(state, state_dir, period, effective.kinds, payload, 0, 0.0,
                          "未配置 WECOM_WEBHOOK_KEY，未发送", "WECOM_WEBHOOK_KEY_MISSING")
        return WeComResult(
            period=period,
            state="pending",
            kinds=effective.kinds,
            error="未配置 WECOM_WEBHOOK_KEY，未发送",
        )

    owned = client is None
    http_client = client
    if owned:  # pragma: no cover - 产品路径才自建客户端
        import httpx

        http_client = httpx.Client(timeout=cfg.timeout)
    attempts = 0
    last_error: Optional[ReconqError] = None
    try:
        for _ in range(max(1, cfg.max_attempts)):
            attempts += 1
            try:
                status, data = _post(cfg, content, client=http_client)
                err = _business_error(status, data, endpoint=cfg.redacted_endpoint())
                if err is None:
                    _mark_pushed(state, state_dir, period, effective.kinds)
                    return WeComResult(
                        period=period,
                        state="pushed",
                        kinds=effective.kinds,
                        attempts=attempts,
                        errcode=int(data.get("errcode") or 0),
                        forced=force,
                    )
                last_error = err
            except ReconqError as exc:
                last_error = exc
            # 不可重试的错误（key 无效、机器人已出群）**当场收手**：
            # 再打两次只会把同一个 93000 重复三遍，还把面板上的状态
            # 从「推送失败」拖成「重试中」——后者在读的人看来像还有希望。
            if not _error_is_retryable(last_error):
                break
            if attempts < max(1, cfg.max_attempts) and sleep is not None:
                sleep(_backoff_delay(attempts))
    finally:
        if owned and http_client is not None:  # pragma: no cover - 见上
            http_client.close()

    assert last_error is not None  # 只在失败路径上到这里
    retryable = _error_is_retryable(last_error)
    if retryable:
        _remember_failure(
            state, state_dir, period, effective.kinds, payload, attempts,
            now() + _backoff_delay(attempts),
            last_error.detail, last_error.code,
        )
        return WeComResult(
            period=period,
            state="retrying",
            kinds=effective.kinds,
            attempts=attempts,
            error=last_error.detail,
            forced=force,
        )
    # 不可重试（key 无效、机器人不在群）：**不进重试队列** —— 队列的语义是
    # 「等会儿还会试」，把一条永远不会自愈的失败放进去只会让面板显示
    # 「重试中」而实际上没人会再试。直接落 failed_kinds，面板显示「推送失败」。
    _mark_failed(
        state, state_dir,
        PendingItem(
            period=period,
            kinds=effective.kinds,
            payload=dict(payload),
            attempts=attempts,
            due_at=0.0,
            last_error=last_error.detail,
            last_code=last_error.code,
        ),
    )
    return WeComResult(
        period=period,
        state="failed",
        kinds=effective.kinds,
        attempts=attempts,
        error=last_error.detail,
        forced=force,
    )


def _error_is_retryable(error: ReconqError) -> bool:
    """推送错误是否值得重试。看错误码元数据的 ``retryable``，不在调用点写 if。"""
    from ..errors import error_meta

    return error_meta(error.code).retryable


def _mark_pushed(state: dict[str, Any], state_dir: str, period: str, kinds: Sequence[str]) -> None:
    entry = _pushed_entry(state, period)
    merged = sorted(set(str(k) for k in entry.get("kinds", ())) | set(kinds))
    entry["kinds"] = merged
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    state["pushed"][period] = entry
    # 送达即从待发队列移除该期条目（成功比「还在等重试」更权威）。
    state["pending"] = [item for item in state["pending"] if item.get("period") != period]
    _save_state(state_dir, state)


def _remember_failure(
    state: dict[str, Any],
    state_dir: str,
    period: str,
    kinds: Sequence[str],
    payload: Mapping[str, Any],
    attempts: int,
    due_at: float,
    error: str,
    code: str,
) -> None:
    """把失败落进 pending 队列（同一期只留一条，覆盖式更新）。

    覆盖而不是追加：同一期反复扫描会反复判定超阈值，若每次失败都追加一条，
    队列里会堆着同一条消息的十几份副本，重发时把同一个提醒刷出去十几遍。
    """
    item = PendingItem(
        period=period,
        kinds=tuple(kinds),
        payload=dict(payload),
        attempts=attempts,
        due_at=due_at,
        last_error=error,
        last_code=code,
    )
    kept = [row for row in state["pending"] if row.get("period") != period]
    kept.append(item.to_dict())
    state["pending"] = kept
    _save_state(state_dir, state)


def flush_pending(
    state_dir: str,
    *,
    config: Optional[WeComConfig] = None,
    client: Any = None,
    now: Callable[[], float] = time.time,
) -> list[WeComResult]:
    """把 pending 队列里**到期**的条目重发一遍。**常驻服务启动时调一次。**

    到期判定用注入的 ``now`` 比较 ``due_at``，测试可把时钟往前拨而不用真等。
    未到期的条目原样留在队列里（不丢、不提前发）—— 这正是「重启后续发」的
    落点：进程在退避中途被杀，重启后剩下的那条仍会在到期后被送出。
    """
    cfg = config or WeComConfig()
    state = _load_state(state_dir)
    pending = list(state.get("pending", ()))
    if not pending:
        return []
    if not cfg.webhook_key:
        return [
            WeComResult(
                period=str(item.get("period", "")),
                state="pending",
                kinds=tuple(str(k) for k in item.get("kinds", ())),
                error="未配置 WECOM_WEBHOOK_KEY，队列保留待补配置",
            )
            for item in pending
        ]

    owned = client is None
    http_client = client
    if owned:  # pragma: no cover - 产品路径才自建客户端
        import httpx

        http_client = httpx.Client(timeout=cfg.timeout)

    results: list[WeComResult] = []
    remaining: list[dict[str, Any]] = []
    changed = False
    try:
        for raw in pending:
            item = PendingItem.from_dict(raw)
            if item.due_at > now():
                remaining.append(raw)
                continue
            content = render_markdown(item.payload)
            item.attempts += 1
            changed = True
            try:
                status, data = _post(cfg, content, client=http_client)
                err = _business_error(status, data, endpoint=cfg.redacted_endpoint())
                if err is None:
                    remaining = [row for row in remaining if row.get("period") != item.period]
                    _mark_pushed(state, state_dir, item.period, item.kinds)
                    results.append(
                        WeComResult(
                            period=item.period,
                            state="pushed",
                            kinds=item.kinds,
                            attempts=item.attempts,
                            errcode=int(data.get("errcode") or 0),
                        )
                    )
                    continue
                item.last_error, item.last_code = err.detail, err.code
            except ReconqError as exc:
                item.last_error, item.last_code = exc.detail, exc.code
            if not _error_is_retryable(
                ReconqError(item.last_code, item.last_error)
            ) or item.attempts >= max(1, cfg.max_attempts):
                remaining = [row for row in remaining if row.get("period") != item.period]
                _mark_failed(state, state_dir, item)
                results.append(
                    WeComResult(
                        period=item.period,
                        state="failed",
                        kinds=item.kinds,
                        attempts=item.attempts,
                        error=item.last_error,
                    )
                )
                continue
            item.due_at = now() + _backoff_delay(item.attempts)
            remaining.append(item.to_dict())
            results.append(
                WeComResult(
                    period=item.period,
                    state="retrying",
                    kinds=item.kinds,
                    attempts=item.attempts,
                    error=item.last_error,
                )
            )
        if changed:
            # 送达的那几条已由 _mark_pushed 从 state["pending"] 里摘掉；
            # 这里只补回「到期但仍未送达」的条目，不整表覆盖。
            state["pending"] = remaining + [
                row
                for row in state.get("pending", ())
                if row.get("period") in {r.get("period") for r in remaining}
            ]
            _save_state(state_dir, state)
    finally:
        if owned and http_client is not None:  # pragma: no cover - 见上
            http_client.close()
    return results


def _mark_failed(state: dict[str, Any], state_dir: str, item: PendingItem) -> None:
    """重试用尽：出队，但把「失败」写进 ``pushed`` 侧的状态表。

    **不能只是删掉队列项。** 删掉之后 ``notifier_state`` 会因为
    「既没推送成功也没待发」而回落到 ``not_pushed``（"本期未推送（未超阈值）"）——
    那一行字在面板上会被读成「一切正常」，而真相是「推了三次都没送到」。
    """
    entry = _pushed_entry(state, item.period)
    entry["kinds"] = sorted(set(str(k) for k in entry.get("kinds", ())))
    entry["failed_kinds"] = sorted(set(entry.get("failed_kinds", ())) | set(item.kinds))
    entry["last_error"] = item.last_error
    entry["last_code"] = item.last_code
    entry["failed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    state["pushed"][item.period] = entry
    state["pending"] = [row for row in state.get("pending", ()) if row.get("period") != item.period]
    _save_state(state_dir, state)


# ─────────────────────────── 状态查询 ───────────────────────────


def notifier_state(state_dir: str, period: str, *, note: str = "") -> NotifState:
    """读某账期的推送状态。**面板「推送状态」那一行就是它。**

    判定顺序是「有失败 → 有待发 → 有成功 → 未推送」：**失败优先于成功**，
    因为「推过但没送到」比「没推」更需要被看见，而失败状态只存在于
    ``failed_kinds`` 里（队列项已出队）。
    """

    state = _load_state(state_dir)
    entry = _pushed_entry(state, period)
    pushed_kinds = tuple(str(k) for k in entry.get("kinds", ()))
    failed_kinds = tuple(str(k) for k in entry.get("failed_kinds", ()))
    pending_rows = [row for row in state.get("pending", ()) if row.get("period") == period]
    if pending_rows:
        last_error = str(pending_rows[-1].get("last_error", ""))
        codes = {str(row.get("last_code", "")) for row in pending_rows}
        state_key = "pending" if "WECOM_WEBHOOK_KEY_MISSING" in codes else "retrying"
    elif failed_kinds and not pushed_kinds:
        last_error = str(entry.get("last_error", ""))
        state_key = "failed"
    elif failed_kinds:
        # 部分送达：成功与失败并存，面板要能一眼看出「哪条线没送到」。
        last_error = str(entry.get("last_error", ""))
        state_key = "failed"
    elif pushed_kinds:
        last_error = ""
        state_key = "pushed"
    else:
        last_error = ""
        state_key = "not_pushed"
    return NotifState(
        period=period,
        state=state_key,
        kinds=tuple(sorted(set(pushed_kinds) | set(failed_kinds))),
        pushed_kinds=pushed_kinds,
        pending=len(pending_rows),
        last_error=last_error,
        note=note,
    )


def notifier_status_of(state_dir: str, periods: Sequence[str]) -> dict[str, dict[str, Any]]:
    """多账期状态（面板账期列表用）。返回 ``{账期: 状态字典}``。"""

    return {period: notifier_state(state_dir, period).to_dict() for period in periods}
