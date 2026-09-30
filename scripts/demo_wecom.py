"""企微推送端到端演示（Task 46）。

跑的是**产品代码的真实路径**：真实协议常量、真实凭据位、真实状态文件落盘、
真实退避重试。唯一的替身是 HTTP 传输层（本机没有企业微信机器人的 key，
也不该拿真 key 去演示脚本里发消息）—— 替身只替换「发出去的那一次 POST」，
阈值判定、内容约束、去重、入队、重试、状态判定全部走产品实现。

演示顺序刻意按财务科真实的周一早晨走一遍：

1. 干净账期（条数 12 / 金额 340 元）→ **零调用**、不落状态文件；
2. 脏账期（条数 37 / 金额 8123.45 元）→ 真的推一次，正文里没有金额；
3. 同一期再判一次 → **去重**，零新增调用；
4. 企微返回 45009 限频三次 → 入 pending 队列并落盘，重启后重发成功；
5. 另一个期 key 无效（93000）→ **重试无意义**，直接失败留痕，面板显示「推送失败」；
6. 「重新推送」→ 机器操作人被拒，自然人放行。

用法::

    PYTHONPATH=src python3 scripts/demo_wecom.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from reconq.audit.wecom import (  # noqa: E402
    AMOUNT_THRESHOLD_ENV,
    COUNT_THRESHOLD_ENV,
    PUSH_STATE_NAME,
    WEBHOOK_KEY_ENV,
    WeComConfig,
    build_deep_link,
    flush_pending,
    notifier_status_of,
    send_summary,
)

PERIOD_CLEAN = "2026-W36"
PERIOD_DIRTY = "2026-W37"
PERIOD_BROKEN = "2026-W38"


class LoopbackWebhook:
    """传输层替身：**只替换那一次 POST**，其余全走产品代码。"""

    def __init__(self, script: list[tuple[int, dict]]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def post(self, url: str, json: object = None) -> "LoopbackResponse":  # noqa: A002
        self.calls.append({"url": url, "body": json})
        status, body = self.script.pop(0) if self.script else (200, {"errcode": 0, "errmsg": "ok"})
        return LoopbackResponse(status, body)

    def close(self) -> None:
        pass


class LoopbackResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


def _bar(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 62 - len(title)))


def _show(result) -> None:
    print(
        f"   结果：{result.state_zh}（state={result.state}，"
        f"尝试 {result.attempts} 次，超阈值类型={'/'.join(result.kinds) or '—'}）"
    )
    if result.error:
        print(f"   说明：{result.error[:110]}")


def main() -> int:
    state_dir = tempfile.mkdtemp(prefix="reconq-wecom-demo-")
    # 配置全部经由产品的环境变量装配路径（凭据位名与 README 一致）
    env = {
        WEBHOOK_KEY_ENV: "demo-webhook-key-not-a-real-secret",
        COUNT_THRESHOLD_ENV: "30",
        AMOUNT_THRESHOLD_ENV: "5000.00",
    }
    config = WeComConfig.from_env(env)
    print(f"配置：条数阈值={config.count_threshold}，金额阈值={config.amount_threshold} 元")
    print(f"凭据位：{WEBHOOK_KEY_ENV}（本演示用占位 key；产品代码不硬编码任何 key）")
    print(f"状态目录：{state_dir}")

    # ── 1. 干净账期：零调用、不落盘 ──
    _bar("1. 干净账期（条数 12 / 金额 340.00）→ 未超阈值")
    clean = LoopbackWebhook([])
    result = send_summary(
        PERIOD_CLEAN, config=config, state_dir=state_dir,
        diff_keys=[f"{PERIOD_CLEAN}|wechat|taxonomy.missing:EX-{i}" for i in range(12)],
        unbalanced_count=12, unbalanced_amount=Decimal("340.00"), client=clean,
    )
    _show(result)
    print(f"   HTTP 调用数：{len(clean.calls)}（未超阈值一次都不该发）")
    print(f"   状态文件是否落盘：{os.path.exists(os.path.join(state_dir, PUSH_STATE_NAME))}")
    assert len(clean.calls) == 0 and result.state == "not_pushed"

    # ── 2. 脏账期：真推一次 ──
    _bar("2. 脏账期（条数 37 / 金额 8123.45）→ 超阈值，推一次")
    keys = [f"{PERIOD_DIRTY}|wechat|taxonomy.missing:EX-{i}" for i in range(37)]
    dirty = LoopbackWebhook([])
    result = send_summary(
        PERIOD_DIRTY, config=config, state_dir=state_dir, diff_keys=keys,
        unbalanced_count=37, unbalanced_amount=Decimal("8123.45"), client=dirty,
    )
    _show(result)
    content = dirty.calls[0]["body"]["markdown"]["content"]
    print("   ── 企微消息正文（产品代码渲染的原文）──")
    for line in content.splitlines():
        print(f"   │ {line}")
    print("   ──────────────────────────────────────")
    print(f"   正文中含金额明细？{'是（不应出现）' if '8123.45' in content else '否'}")
    assert result.state == "pushed" and "8123.45" not in content

    # ── 3. 同一期再判一次：去重 ──
    _bar("3. 同一账期再判一次 → 去重，不重复推送")
    again = send_summary(
        PERIOD_DIRTY, config=config, state_dir=state_dir, diff_keys=keys,
        unbalanced_count=37, unbalanced_amount=Decimal("8123.45"), client=dirty,
    )
    _show(again)
    print(f"   累计 HTTP 调用数：{len(dirty.calls)}（仍应是 1）")
    assert again.deduped and len(dirty.calls) == 1

    # ── 4. 限频 → 入队落盘 → 重启后续发成功 ──
    _bar("4. 另一期遇到 45009 限频 → 入队落盘，重启后重发成功")
    limited = LoopbackWebhook([(200, {"errcode": 45009, "errmsg": "rate limited"})] * 3)
    result = send_summary(
        PERIOD_BROKEN, config=config, state_dir=state_dir,
        diff_keys=[f"{PERIOD_BROKEN}|alipay|taxonomy.missing:EX-1"],
        unbalanced_count=41, unbalanced_amount=Decimal("9900.00"),
        client=limited, sleep=lambda _s: None,
    )
    _show(result)
    state = json.load(open(os.path.join(state_dir, PUSH_STATE_NAME), encoding="utf-8"))
    print(f"   队列落盘：pending {len(state['pending'])} 条，已尝试 {state['pending'][0]['attempts']} 次")
    print(f"   上次失败：{state['pending'][0]['last_error'][:80]}")
    due_at = state["pending"][0]["due_at"]
    # 模拟进程重启：新 client、时钟前拨到到期之后
    restarted = LoopbackWebhook([])
    flushed = flush_pending(state_dir, config=config, client=restarted, now=lambda: due_at + 1)
    _show(flushed[0])
    after = json.load(open(os.path.join(state_dir, PUSH_STATE_NAME), encoding="utf-8"))
    print(f"   重发后队列：{len(after['pending'])} 条（送达即出队）")
    assert flushed[0].state == "pushed" and after["pending"] == []

    # ── 5. key 无效：重试无意义，面板显示「推送失败」 ──
    _bar("5. 再一期返回 93000（key 无效）→ 不退避，失败留痕")
    broken = LoopbackWebhook([(200, {"errcode": 93000, "errmsg": "invalid webhook url"})] * 3)
    result = send_summary(
        "2026-W39", config=config, state_dir=state_dir,
        diff_keys=["2026-W39|self_service|taxonomy.missing:EX-7"],
        unbalanced_count=55, client=broken, sleep=lambda _s: None,
    )
    _show(result)
    print(f"   实际 HTTP 调用数：{len(broken.calls)}（不可重试的错误不该靠退避硬试）")

    # ── 6. 重新推送的人工边界 ──
    _bar("6. 「重新推送」：机器操作人被拒，自然人放行")
    try:
        send_summary(
            PERIOD_DIRTY, config=config, state_dir=state_dir, diff_keys=keys,
            unbalanced_count=37, unbalanced_amount=Decimal("8123.45"),
            client=dirty, force=True, operator="scheduler",
        )
        print("   机器操作人竟然通过了 —— 这是边界失效")
        return 1
    except Exception as exc:  # ReconqError
        print(f"   机器操作人被拒：{exc}")
    forced = send_summary(
        PERIOD_DIRTY, config=config, state_dir=state_dir, diff_keys=keys,
        unbalanced_count=37, unbalanced_amount=Decimal("8123.45"),
        client=dirty, force=True, operator="财务科·张敏",
    )
    _show(forced)

    # ── 收尾：面板可见状态 ──
    _bar("7. 面板「推送状态」那一行读到的内容")
    statuses = notifier_status_of(
        state_dir, [PERIOD_CLEAN, PERIOD_DIRTY, PERIOD_BROKEN, "2026-W39"]
    )
    print(f"   {'账期':<12} {'状态':<10} 已推送类型 / 待发 / 上次失败")
    for period, row in statuses.items():
        kinds = "/".join(row["kinds"]) or "—"
        err = (row["last_error"] or "")[:36]
        print(f"   {period:<12} {row['state_zh']:<16} {kinds:<18} 待发={row['pending']}  {err}")

    # 深链可解析
    _bar("8. 深链解析（面板路由入口）")
    link = build_deep_link(PERIOD_DIRTY, keys[0])
    print(f"   推送正文里的深链：{link}")
    print(f"   对应面板地址：{config.panel_base}/period/{PERIOD_DIRTY}")

    print(f"\n全部断言通过。状态文件：{os.path.join(state_dir, PUSH_STATE_NAME)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
