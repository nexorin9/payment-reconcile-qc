"""Task 30 · 网关失败处理链 —— 端到端演示脚本。

跑法（在项目子目录下）::

    PYTHONPATH=src python3 scripts/demo_gateway_degradation.py

演示的是**模型服务挂掉时这一期会发生什么**，按时间顺序走一遍：

    1. 端点不可达 → 首次 + 2 次重试（分档退避）→ 降级
    2. 连续失败达阈值 → 熔断开启（**一个请求都不发**）
    3. 剩下的差异全部零请求降级 —— 扫描不再被超时拖住
    4. 冷却期到 → 半开放一次试探 → 成功即恢复
    5. 全程差异台账状态一字未动（已确认只能由人工点产生）

网络用 ``httpx.MockTransport``、时间用假钟，**不真等也不真发**：
替身只在测试/演示数据层，产品代码走真实 OpenAI-compatible 协议，
院内模型端点需要院内凭据，本轮未实连。

脚本里的账单行是脱敏占位（单号形如 EX-9、金额是演示值），
不含任何真实患者或真实交易明细。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from reconq.align.attribution import attribute
from reconq.align.bundle import build_evidence_bundle
from reconq.align.matcher import AlignRecord, align_channel, build_index
from reconq.contracts.loader import load_contract
from reconq.engine.period import (
    DIFF_TABLE,
    DISPOSITION_PENDING,
    init_scan_schema,
    list_diff_items,
)
from reconq.gateway.degradation import CircuitBreaker, DegradationLedger
from reconq.gateway.model_gateway import GatewayConfig, ModelGateway
from reconq.sources.timezone import HOSPITAL_TZ, period_window
from reconq.store.schema import connect, init_schema

PERIOD = "2026-W36"
CHANNEL = "wechat"
FAKE_BASE_URL = "http://127.0.0.1:9/v1"
BREAKER_THRESHOLD = 3
COOLDOWN_SECONDS = 60.0


class DemoClock:
    """演示用假钟：单调钟可推进，墙上时间每次推进后 +1 秒。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self._base = datetime(2026, 9, 7, 10, 12, 0)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def wall_clock(self, fmt: str) -> str:
        stamp = self._base + timedelta(seconds=self.now - 1000.0)
        return stamp.strftime(fmt)


def _bundle(order_no: str, amount: str, row_no: int):
    index = build_index([], period=PERIOD)
    outcome = align_channel(
        CHANNEL,
        [
            AlignRecord(
                channel=CHANNEL,
                period=PERIOD,
                order_no=order_no,
                amount=Decimal(amount),
                trade_time=datetime(2026, 9, 2, 10, 0, 0),
                source_file="wechat_bill.csv",
                source_row_no=row_no,
                ch_id=row_no,
            )
        ],
        index,
        period=PERIOD,
        window=period_window(PERIOD, HOSPITAL_TZ),
    ).outcomes[0]
    return build_evidence_bundle(
        outcome,
        contract=load_contract("contracts/wechat.yml", {"period": PERIOD}),
        window=period_window(PERIOD, HOSPITAL_TZ),
    )


def _seed_diff(conn) -> None:
    init_schema(conn)
    init_scan_schema(conn)
    conn.execute(
        f"INSERT INTO {DIFF_TABLE} (period, channel, check_path, check_name, message, diff_amount,"
        " source_file, first_row_no, disposition, needs_attribution)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (PERIOD, CHANNEL, "align.channel_only", "单边账", "渠道有、HIS 查无此笔",
         "66.00", "wechat_bill.csv", 3, DISPOSITION_PENDING, 1),
    )
    conn.commit()


def main() -> int:
    clock = DemoClock()
    breaker = CircuitBreaker(
        threshold=BREAKER_THRESHOLD, cooldown_seconds=COOLDOWN_SECONDS,
        clock=clock, wall_clock=clock.wall_clock,
    )
    ledger = DegradationLedger(period=PERIOD, breaker=breaker)

    requests: list[str] = []

    def _unreachable(_request: httpx.Request) -> httpx.Response:
        requests.append("unreachable")
        raise httpx.ConnectError("拒绝连接")

    # 端点恢复后给的草稿。evidence_refs 必须**原样抄**当前这条差异的真实行号
    # —— 引用不在证据包里的行会被整条作废（那是「模型编证据」的防线）。
    last_ref = {"row": 3}

    def _healthy(_request: httpx.Request) -> httpx.Response:
        requests.append("healthy")
        content = json.dumps(
            {
                "drafts": [
                    {
                        "cause": "跨日切分重复计入",
                        "confidence": 0.72,
                        "evidence_refs": [f"wechat_bill.csv:{last_ref['row']}"],
                        "explanation_zh": "该笔收费时间落在账期窗口之外。",
                    }
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={
                "model": "reconq-attribution-1",
                "choices": [{"message": {"role": "assistant", "content": content}}],
            },
        )

    config = GatewayConfig(
        base_url=FAKE_BASE_URL, api_key="FAKE_MODEL_KEY", model="reconq-attribution-1",
        jitter_ratio=0.0,
    )

    def _gateway(handler):
        return ModelGateway(
            config, client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleeper=lambda _s: None, breaker=breaker, ledger=ledger,
            period=PERIOD, channel=CHANNEL,
        )

    conn = connect(":memory:")
    _seed_diff(conn)
    before = [d.to_dict() for d in list_diff_items(conn, period=PERIOD)]

    print("=" * 74)
    print(f"账期 {PERIOD} · {CHANNEL} 路 · 模型服务不可达时会怎样")
    print("=" * 74)

    # ① 前两条差异：端点不可达，各自重试用尽后降级
    for i, (order_no, row) in enumerate((("EX-9", 3), ("EX-11", 7)), start=1):
        out = attribute(_bundle(order_no, "66.00", row), _gateway(_unreachable))
        print(
            f"[差异 {i}] 降级={out.degraded} 原因={out.error_code} "
            f"熔断={out.breaker_state or breaker.state.value}\n"
            f"         处置标记：{out.status_zh}"
        )

    # ② 第三条把闸打开
    out = attribute(_bundle("EX-13", "66.00", 11), _gateway(_unreachable))
    print(
        f"[差异 3] 降级={out.degraded} 原因={out.error_code} "
        f"→ 熔断在第 {BREAKER_THRESHOLD} 次失败后开启（{breaker.state.status_zh}）"
    )
    sent = len(requests)
    print(f"         开闸时刻 {breaker.opened_at_wall}，冷却 {COOLDOWN_SECONDS:.0f} 秒")

    # ③ 剩下 17 条差异：零请求降级
    for n in range(17):
        row = 20 + n
        last_ref["row"] = row
        attribute(_bundle(f"EX-{row}", "66.00", row), _gateway(_healthy))
    print(
        f"[差异 4-20] 熔断开启期间又跑 17 条 → 新增请求 {len(requests) - sent} 个"
        f"（端点其实是活的，但闸没到冷却期就不打）"
    )
    print(f"           扫描耗时不再被 30 秒超时拖住 —— 这就是熔断要防的事")

    # ④ 冷却期到 → 半开试探 → 成功恢复
    clock.advance(COOLDOWN_SECONDS + 1)
    last_ref["row"] = 41
    probe_before = len(requests)
    recovered = attribute(_bundle("EX-41", "66.00", 41), _gateway(_healthy))
    print(
        f"[恢复] 冷却期满放一次试探 → 新增请求 {len(requests) - probe_before} 个，"
        f"降级={recovered.degraded}，熔断={breaker.state.value}"
        f"（{breaker.state.status_zh}）"
    )
    if recovered.causes:
        top = recovered.causes[0]
        print(f"        归因恢复：Top-1「{top.cause}」置信度 {top.confidence}"
              f"，证据 {top.evidence_refs} —— 人工确认后才进台账")
    print(f"        恢复时刻 {breaker.recovered_at_wall}，累计开闸 {breaker.opened_count} 次")

    # ⑤ 台账状态一字未动
    conn.commit()
    after = [d.to_dict() for d in list_diff_items(conn, period=PERIOD)]
    print("-" * 74)
    print(f"差异台账：降级前后逐字段一致 = {after == before}；"
          f"处置状态仍是「{after[0]['disposition']}」")
    print("          已确认只能由人工点产生 —— 模型与规则都不推进状态")
    conn.close()

    # ⑥ 降级观测台账
    print("-" * 74)
    print("降级观测台账（面板与结论单读的是同一份数字）：")
    print(f"  降级次数 {ledger.count}")
    for code, n in ledger.by_reason().items():
        first = next(r for r in ledger.records if r.reason == code).to_dict()
        print(f"  · {code:<22} {first['reason_zh']} ×{n}"
              f"（{first['kind_zh']}）  首次 {first['at']}")
    print(f"  熔断：{breaker.state.status_zh}  开 {breaker.opened_at_wall}"
          f"  恢复 {breaker.recovered_at_wall}")
    print(f"  运行摘要字段：{sorted(ledger.to_run_summary_fields())}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
