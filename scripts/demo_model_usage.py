"""Task 31 · 模型调用审计与成本计量 —— 端到端演示脚本。

跑法（在项目子目录下）::

    PYTHONPATH=src python3 scripts/demo_model_usage.py

演示的是**财务科周一打开面板时会看到的那张成本表**，按顺序走一遍：

    1. 一期扫出 12 条单边账 → 12 次真调用
    2. 第二次扫（口径改了要重扫）→ 前 6 条命中缓存，**一个请求都不发**
    3. 端点不回话 2 次 → 降级，按错误码分组
    4. 成本表（次数 / 命中率 / 耗时 / token / 钱）+ 面板底部那一行
    5. 审计链校验通过；拆掉触发器改一条之后**被检出并指出第几条**
    6. 什么进了审计库、什么没进：证据正文没进，外键与指纹进了

网络用 ``httpx.MockTransport``、时间用假钟，**不真等也不真发**：
替身只在测试/演示数据层，产品代码走真实 OpenAI-compatible 协议，
院内模型端点需要院内凭据，本轮未实连。

脚本里的账单行是脱敏占位（单号形如 ``EX-WX-0001``、金额是演示值），
不含任何真实患者或真实交易明细。
"""

from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal

import httpx

from reconq.audit.model_calls import (
    MODEL_CALL_TABLE,
    DraftCache,
    ModelCall,
    ModelCallLedger,
    append_only_guard,
    init_audit_schema,
    request_fingerprint,
    usage_of_period,
)
from reconq.gateway.model_gateway import GatewayConfig, ModelGateway
from reconq.run_summary import build_run_summary
from reconq.store.schema import connect, init_schema

PERIOD = "2026-W36"
CHANNEL = "wechat"
MODEL = "reconq-attribution-1"
FAKE_BASE_URL = "http://127.0.0.1:9/v1"
FAKE_KEY = "FAKE_MODEL_KEY_PLACEHOLDER"

# 演示单价（元/千 token）。**故意不对称**：对称的话「提示词侧与生成侧
# 算反了」在终端输出里一眼看不出来。
PRICE_PROMPT = Decimal("0.012")
PRICE_COMPLETION = Decimal("0.036")

#: 演示账期的差异条数与其中命中缓存的条数。
DIFFS_PER_SCAN = 12
CACHED_ON_RESCAN = 6


class DemoClock:
    """可推进的单调钟。**不真等** —— 退避全走假 sleeper。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _ok_response(chars: int = 180) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            [
                                {
                                    "cause": "导出方漏传该笔",
                                    "confidence": 0.72,
                                    "evidence_refs": ["wechat_bill.csv:41"],
                                    "explanation_zh": "渠道账单有、HIS 查无此笔。",
                                }
                            ],
                            ensure_ascii=False,
                        )[:chars],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1840, "completion_tokens": 260, "total_tokens": 2100},
        },
    )


def _config() -> GatewayConfig:
    return GatewayConfig(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_KEY,
        model=MODEL,
        jitter_ratio=0.0,
    )


def _banner(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="reconq-demo-audit-")
    db = os.path.join(workdir, "reconq.db")
    conn = connect(db)
    init_schema(conn)
    init_audit_schema(conn)
    append_only_guard(conn)

    ledger = ModelCallLedger(conn)
    cache = DraftCache(conn)
    clock = DemoClock()
    requests_seen: list[httpx.Request] = []
    # 端点从第 N 次起开始不回话（httpx.ReadTimeout）：演示降级按错误码分组
    state: dict[str, int] = {"n": 0, "timeout_from": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        state["n"] += 1
        clock.advance(0.12)  # 一次成功调用约 120ms
        if state["n"] >= state["timeout_from"]:
            raise httpx.ReadTimeout("端点不回话")
        return _ok_response()

    # 端点只在**第 3 段**开始不回话。开关做成显式的：让 handler 一上来
    # 就超时的话，第 1 段「12 条逐条问模型」会变成「12 条里 5 条降级」，
    # 演示的叙事就断了 —— 看的人会以为正常路径本来就带降级。
    state["timeout_from"] = DIFFS_PER_SCAN + 1

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = ModelGateway(
        _config(), client=client, clock=clock, sleeper=Sleeper(), period=PERIOD
    )

    # ── 1. 第一次扫描：12 条差异，逐条真问模型 ──────────────────────
    _banner(f"1 · 第一次扫描：{DIFFS_PER_SCAN} 条单边账逐条问模型")
    for i in range(DIFFS_PER_SCAN):
        row = f"EX-WX-{i:04d},{Decimal('66.00') + i},2026-09-02 10:00:00,挂号费"
        fp = request_fingerprint(row)
        cached = cache.lookup(fp, "v1", MODEL)
        if cached is not None:
            ledger.record(
                ModelCall(
                    call_id="",
                    period=PERIOD,
                    conclusion_key=f"align.channel_only#{CHANNEL}:EX-WX-{i:04d}",
                    channel=CHANNEL,
                    model=MODEL,
                    request_fingerprint=fp,
                    cache_hit=True,
                )
            )
            continue
        result = gateway.chat_completion(
            [{"role": "user", "content": row}],
            bundle_audit={"char_count": len(row), "line_count": 1,
                          "conclusion_key": f"align.channel_only#{CHANNEL}:EX-WX-{i:04d}"},
        )
        cache.put(fp, "v1", MODEL, result.content)
        ledger.record(
            ModelCall.from_gateway_call(
                result.call,
                period=PERIOD,
                request_fingerprint=fp,
                prompt_version="v1",
            )
        )
    print(f"  发出的请求数：{len(requests_seen)}")
    print(f"  审计记录数：  {len(ledger.list_calls(PERIOD))}")

    # ── 2. 第二次扫描（口径改了要重扫）：前 6 条命中缓存 ──────────────
    _banner(f"2 · 第二次扫描：前 {CACHED_ON_RESCAN} 条命中缓存，一个请求都不发")
    before = len(requests_seen)
    for i in range(CACHED_ON_RESCAN):
        row = f"EX-WX-{i:04d},{Decimal('66.00') + i},2026-09-02 10:00:00,挂号费"
        fp = request_fingerprint(row)
        cached = cache.lookup(fp, "v1", MODEL)
        assert cached is not None, f"第 {i} 条本该命中缓存"
        ledger.record(
            ModelCall(
                call_id="",
                period=PERIOD,
                conclusion_key=f"align.channel_only#{CHANNEL}:EX-WX-{i:04d}",
                channel=CHANNEL,
                model=MODEL,
                request_fingerprint=fp,
                cache_hit=True,
            )
        )
    print(f"  缓存命中 {CACHED_ON_RESCAN} 条，新增请求数：{len(requests_seen) - before}")
    assert len(requests_seen) == before, "命中缓存却发了请求"

    # ── 3. 中途端点超时 → 降级按错误码分组 ──────────────────────────
    _banner("3 · 端点不回话：2 次降级，按真实错误码分组（不是一个「归因不可用」）")
    for i in range(2):
        result = gateway.chat_completion(
            [{"role": "user", "content": f"EX-WX-9{i:03d},66.00,2026-09-02 10:00:00,挂号费"}],
            bundle_audit={"char_count": 40, "line_count": 1,
                          "conclusion_key": f"align.channel_only#{CHANNEL}:EX-WX-9{i:03d}"},
        )
        ledger.record(
            ModelCall.from_gateway_call(
                result.call, period=PERIOD, prompt_version="v1"
            )
        )
        print(f"  第 {i + 1} 次：{result.status}  code={result.error_code}  "
              f"attempts={result.call.attempts}")

    # ── 4. 成本表 ──────────────────────────────────────────────────
    _banner("4 · 成本表（单价 0.012 / 0.036 元每千 token）")
    priced_env = {
        "RECONQ_MODEL_PRICE_PROMPT_PER_1K": str(PRICE_PROMPT),
        "RECONQ_MODEL_PRICE_COMPLETION_PER_1K": str(PRICE_COMPLETION),
    }
    usage = usage_of_period(conn, PERIOD, env=priced_env)
    print(usage.render())
    print(f"\n  缓存统计：{cache.stats()}")

    # 未配单价时必须说「未配置单价」，不是「0.00 元」
    unpriced = usage_of_period(conn, PERIOD, env={})
    print(f"  未配单价时 cost_text = {unpriced.cost_text!r}")

    # 摘要层读的是同一个对象
    summary = build_run_summary(
        period=PERIOD, command="scan", channels=[], model_usage=usage
    )
    print("\n  面板底部那一行：")
    for line in summary.render_zh().splitlines():
        if "模型调用" in line:
            print(f"  {line.strip()}")

    # ── 5. 审计链：完整 → 篡改后被检出 ──────────────────────────────
    _banner("5 · 审计链：完整 → 改一条之后被检出并指出第几条")
    verdict = ModelCallLedger(conn).verify_chain(PERIOD)
    print(f"  校验通过：{verdict.ok}  已核 {verdict.checked} 条  {verdict.reason}")
    conn.execute("DROP TRIGGER trg_model_calls_no_update")
    conn.execute(
        f"UPDATE {MODEL_CALL_TABLE} SET prompt_tokens = 999999 "
        "WHERE period = ? AND seq = 5",
        (PERIOD,),
    )
    broken = ModelCallLedger(conn).verify_chain(PERIOD)
    print(f"  改第 5 条之后：ok={broken.ok}  断点={broken.broken_seq}  {broken.reason}")
    assert broken.ok is False and broken.broken_seq == 5

    # ── 6. 证据正文不进审计库 ───────────────────────────────────────
    _banner("6 · 什么进了审计库、什么没进：一句话说清边界")
    # 拿一条**真的落过审计**的证据行：第 7 条差异（EX-WX-0006，金额 72.00）。
    # 用一条没进过库的样例行去搜当然搜不到 —— 那种「验证」什么都没证明。
    audited_row = f"EX-WX-0006,{Decimal('66.00') + 6},2026-09-02 10:00:00,挂号费"
    fp_sample = request_fingerprint(audited_row)
    blob = "\n".join(
        json.dumps(dict(row), ensure_ascii=False, default=str)
        for row in conn.execute(f"SELECT * FROM {MODEL_CALL_TABLE}")
    )
    print(f"  已落审计的证据行：{audited_row}")
    print()
    print("  【没进】证据行原文（含金额、交易时间、费用类别）：")
    print(f"        整行搜得到吗：{audited_row in blob}")
    print(f"        金额 72.00 搜得到吗：{'72.00' in blob}")
    print("        —— 审计要的是「这次问了多大一坨」（char_count/line_count），")
    print("           不是那一坨是什么。证据正文只存在于账单表与证据快照，")
    print("           那两处都带源文件行号可回查，审计库不带。")
    print()
    print("  【进了】conclusion_key（含渠道单号，作为台账外键）：")
    print(f"        'align.channel_only#wechat:EX-WX-0006' 在库里："
          f"{'align.channel_only#wechat:EX-WX-0006' in blob}")
    print("        —— 这是**外键不是正文**：没有它，审计记录就没法指回")
    print("           差异台账里那一条，「这次花的钱对应哪条差异」就答不出来。")
    print("           同一个单号在 bill_rows 与 diff_items 里本来就有，")
    print("           审计表存它不构成「第二份账单事实」。")
    print()
    print("  【进了】证据包指纹（复现的锚点）：")
    print(f"        {fp_sample[:16]}… 在库里：{fp_sample in blob}")
    assert audited_row not in blob
    assert "72.00" not in blob
    assert fp_sample in blob, "指纹必须在库里 —— 那才是复现的锚点"
    assert "align.channel_only#wechat:EX-WX-0006" in blob
    conn.close()
    print(f"\n演示库（可自行打开查）：{db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
