"""模型调用审计与成本计量（Task 31）。

Task 28/30 回答的是**故障**（不可达、不回话、挂了别拖时间）；这一层回答
的是**日常**：这一期调了几次、命中多少缓存、花了多少 token 与钱。

五类规定断言：

1. **调用元数据字段齐全** —— 每条记录都能回答「谁、何时、哪条差异、哪个
   模型、哪份证据、多少 token、多久、什么结果」，且**一个证据正文字段都
   没有**（审计库不是第二个账单库）。
2. **缓存命中不重复调模型** —— 命中时网关**一个请求都不发**，但仍落一条
   ``cache_hit=True`` 的审计记录（不落的话命中率这个指标会随「有没有记
   审计」而变，而它恰是判断缓存值不值得开着的唯一依据）。
3. **按账期聚合数字正确** —— 次数 / 命中率 / 耗时 / token / 成本 / 降级，
   且口径是「**真正发出的调用**」：命中不发请求就不该计入耗时与 token。
4. **审计中无证据原文** —— 库表结构本身就不给正文留位置（白名单建列），
   且正文里的渠道单号与金额在整库扫不到第二份。
5. **append-only 校验** —— 触发器挡住 UPDATE/DELETE；把触发器拆掉改一条
   之后，指纹链校验能检出并指出断点序号。

替身只在测试数据层：模型端点用 ``httpx.MockTransport`` 回放，**不真发
请求也不真等**；产品代码走真实 OpenAI-compatible 协议。院内模型端点需
院内凭据，本轮未实连。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from decimal import Decimal

import httpx
import pytest

from reconq.audit.model_calls import (
    AUDIT_ALLOWED_FIELDS,
    DEFAULT_PROMPT_VERSION,
    GENESIS_HASH,
    MODEL_CALL_CACHE_TABLE,
    MODEL_CALL_CORRECTION_TABLE,
    MODEL_CALL_TABLE,
    PRICE_ENV_VARS,
    DraftCache,
    ModelCall,
    ModelCallLedger,
    ModelUsage,
    append_only_guard,
    cache_key,
    init_audit_schema,
    make_call_id,
    request_fingerprint,
    usage_of_period,
)
from reconq.cli import main
from reconq.errors import ReconqError
from reconq.gateway.model_gateway import GatewayConfig, ModelGateway
from reconq.run_summary import build_run_summary
from reconq.store.schema import connect, init_schema

PERIOD = "2026-W36"
MODEL = "reconq-attribution-1"
FAKE_BASE_URL = "http://127.0.0.1:9/v1"
FAKE_KEY = "FAKE_MODEL_KEY_PLACEHOLDER"

#: 单价测试值（元/千 token）。**故意选不对称的两个数**（0.012 / 0.036），
#: 对称的话「提示词与生成侧算反了」这类错误在断言里看不出来。
PRICE_PROMPT = "0.012"
PRICE_COMPLETION = "0.036"

PRICED_ENV = {
    "RECONQ_MODEL_PRICE_PROMPT_PER_1K": PRICE_PROMPT,
    "RECONQ_MODEL_PRICE_COMPLETION_PER_1K": PRICE_COMPLETION,
}
UNPRICED_ENV: dict[str, str] = {}


# ───────────────────── 夹具 ─────────────────────


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(str(tmp_path / "audit.db"))
    init_schema(c)
    init_audit_schema(c)
    yield c
    c.close()


def _call(
    *,
    period: str = PERIOD,
    conclusion_key: str = "align.channel_only#wechat:EX-9",
    fingerprint: str = "fp-0001",
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    model: str = MODEL,
    elapsed_ms: float = 1200.0,
    prompt_tokens: int = 800,
    completion_tokens: int = 200,
    cache_hit: bool = False,
    status: str = "ok",
    error_code: str = "",
    retried: bool = False,
    attempts: int = 1,
) -> ModelCall:
    """一条审计记录。参数默认值刻意是「一次成功的真实调用」。"""
    return ModelCall(
        call_id="",
        period=period,
        conclusion_key=conclusion_key,
        channel="wechat",
        model=model,
        request_fingerprint=fingerprint,
        prompt_version=prompt_version,
        elapsed_ms=elapsed_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        attempts=attempts,
        retried=retried,
        cache_hit=cache_hit,
        status=status,
        error_code=error_code,
        bundle_chars=4096,
        bundle_lines=88,
    )


# ───────────────────── 一、调用元数据字段齐全 ─────────────────────


def test_model_call_carries_every_field_the_finance_desk_needs():
    call = _call(retried=True, attempts=2)
    payload = call.to_dict()
    # 「谁、何时、哪条差异、哪个模型、哪份证据、多少 token、多久、什么结果」
    for field in (
        "call_id",
        "period",
        "conclusion_key",
        "channel",
        "model",
        "request_fingerprint",
        "prompt_version",
        "elapsed_ms",
        "prompt_tokens",
        "completion_tokens",
        "attempts",
        "retried",
        "cache_hit",
        "status",
        "error_code",
        "at",
    ):
        assert field in payload, f"审计元数据缺字段 {field}"
    assert payload["retried"] is True
    assert payload["attempts"] == 2
    assert payload["at"], "时间戳不能为空 —— 「这一周花的」要能定位到具体时刻"
    assert call.total_tokens == 1000


def test_call_id_is_period_plus_sequence_and_is_recomputable():
    """调用 id 用**账期 + 账期内序号**，不是 uuid。

    理由是人工可回查：信息科报「2026-W36 第 3 次调用」时按序号数得出来，
    uuid 只能整条复制粘贴。
    """
    assert make_call_id(PERIOD, 1) == f"{PERIOD}-000001"
    assert make_call_id(PERIOD, 17) == f"{PERIOD}-000017"


def test_ledger_assigns_sequence_and_overrides_caller_supplied_call_id(conn):
    """序号由台账分配；调用方给的 call_id 会被覆盖。

    让两个来源都能定「这是第几条」的话，重放一次就撞主键，而撞主键的
    表现是「第二次记不进去了」—— 比报错更难查。
    """
    ledger = ModelCallLedger(conn)
    first = ledger.record(_call(fingerprint="fp-a"))
    # 第二次显式带上一个调用方自编的 call_id：它必须被台账覆盖
    second = ledger.record(
        dataclasses.replace(_call(fingerprint="fp-b"), call_id="调用方自己编的标识")
    )
    assert first.call_id == f"{PERIOD}-000001"
    assert second.call_id == f"{PERIOD}-000002"
    # 前驱指纹串成链
    rows = ledger.list_calls(PERIOD)
    assert rows[0]["prev_hash"] == GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["hash"]


def test_ledger_refuses_a_record_without_period(conn):
    """缺账期的记录挂不到任何一条链上，「这一期花了多少」会算少一半。"""
    with pytest.raises(ReconqError) as exc:
        ModelCallLedger(conn).record(_call(period=""))
    assert exc.value.code == "MODEL_AUDIT_SCHEMA_MISSING"


def test_ledger_rejects_an_error_code_that_smells_like_a_detail_paragraph(conn):
    """``error_code`` 是受控短码。把错误详情整段塞进来是「把证据正文抄进
    审计库」最隐蔽的入口，在这里挡住。"""
    # 本仓最长的已登记错误码是 ATTRIBUTION_EVIDENCE_REF_INVALID（32 字符），
    # 阈值 48 留了一倍余量。下面这句 60+ 字的中文详情必然越界；
    # 而一个 32 字符的合法码必须放过（否则阈值定太紧，正常码会被误杀）。
    ModelCallLedger(conn).record(
        _call(status="degraded", error_code="ATTRIBUTION_EVIDENCE_REF_INVALID")
    )
    with pytest.raises(ReconqError) as exc:
        ModelCallLedger(conn).record(
            _call(
                status="degraded",
                error_code="渠道导出方在 2026-09-02 那批账单里把订单号 EX-9 "
                "漏了一行，金额 66.00 元，去查导出模板",
            )
        )
    assert exc.value.code == "MODEL_AUDIT_FIELD_INVALID"


def test_model_call_is_built_from_a_gateway_call_without_evidence_text():
    """从 :class:`GatewayCall` 造审计记录时**只取两个体量字段**。

    证据包侧的审计元数据将来会加新字段；本函数只认 ``char_count`` 与
    ``line_count``，其余一律不带 —— 否则审计表会跟着长成一个影子账单库。
    """
    class FakeGatewayCall:
        model = MODEL
        attempts = 1
        elapsed_ms = 880.0
        retried = False
        ok = True
        error_code = ""
        prompt_tokens = 640
        completion_tokens = 160
        request_digest = "req-body-digest"
        response_chars = 220
        bundle = {
            "conclusion_key": "align.channel_only#wechat:EX-9",
            "channel": "wechat",
            "char_count": 5120,
            "line_count": 120,
            "source_files": ["wechat_bill_2026-W36.csv"],
            "raw_line": "EX-9,66.00,2026-09-02 10:00:00",  # 将来可能混进来的正文
        }

    call = ModelCall.from_gateway_call(
        FakeGatewayCall(), period=PERIOD, request_fingerprint="fp-evidence"
    )
    assert call.request_fingerprint == "fp-evidence"
    assert call.bundle_chars == 5120
    assert call.bundle_lines == 120
    assert call.conclusion_key == "align.channel_only#wechat:EX-9"
    # bundle 里那个 raw_line **没有**进审计记录的任何字段
    assert "EX-9,66.00" not in json.dumps(call.to_dict(), ensure_ascii=False)
    assert not hasattr(call, "raw_line")


# ───────────────────── 二、缓存命中不重复调模型 ─────────────────────


def test_cache_key_covers_fingerprint_prompt_version_and_model():
    """三者缺一不可，且缺任一都是**静默的错误**（不报错、不降级）。"""
    base = cache_key("fp-1", "v1", MODEL)
    assert base != cache_key("fp-2", "v1", MODEL), "换证据包必须换键"
    assert base != cache_key("fp-1", "v2", MODEL), "换提示词版本必须换键"
    assert base != cache_key("fp-1", "v1", "other-model"), "换模型必须换键"


def test_request_fingerprint_is_reproducible_and_evidence_scoped():
    """同一份证据两次构造指纹相同；换个模型名**不**改指纹。

    指纹的对象是证据包，不是整个请求体 —— 请求体里含 model 名与
    response_format，把它们算进去就答不了「同一份证据问过几次」。
    """
    evidence = "渠道行：wechat_bill_2026-W36.csv:3 金额 66.00"
    assert request_fingerprint(evidence) == request_fingerprint(evidence)
    assert request_fingerprint(evidence) != request_fingerprint(evidence + " 补一行")
    assert request_fingerprint(evidence.encode("utf-8")) == request_fingerprint(evidence)
    assert len(request_fingerprint(evidence)) == 32


def test_cache_hit_sends_no_request_but_still_records_the_call(conn):
    """**本任务最核心的一条**：命中时网关一个请求都不发，但仍落审计记录。

    不落的话命中率会随「有没有记审计」而变，而它恰是判断这套缓存值不
    值得开着的唯一依据；落成「调用次数」的话，财务科又会以为真花了钱。
    """
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return _ok_response()

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = ModelGateway(_config(), client=client)
    ledger = ModelCallLedger(conn)
    cache = DraftCache(conn)
    fp = request_fingerprint("证据包正文")

    # —— 第一次：未命中，真发一次请求
    assert cache.lookup(fp, DEFAULT_PROMPT_VERSION, MODEL) is None
    first = gateway.chat_completion([{"role": "user", "content": "证据"}])
    cache.put(fp, DEFAULT_PROMPT_VERSION, MODEL, first.content)
    ledger.record(
        ModelCall.from_gateway_call(first.call, period=PERIOD, request_fingerprint=fp)
    )
    assert len(requests_seen) == 1

    # —— 第二次：命中，**请求数不增加**
    cached = cache.lookup(fp, DEFAULT_PROMPT_VERSION, MODEL)
    assert cached == first.content
    ledger.record(
        ModelCall(
            call_id="",
            period=PERIOD,
            conclusion_key="align.channel_only#wechat:EX-9",
            channel="wechat",
            model=MODEL,
            request_fingerprint=fp,
            cache_hit=True,
            elapsed_ms=0.0,
            prompt_tokens=0,
            completion_tokens=0,
        )
    )
    assert len(requests_seen) == 1, "命中缓存却发了请求 —— 缓存等于没开"

    usage = usage_of_period(conn, PERIOD, env=PRICED_ENV)
    assert usage.calls == 2
    assert usage.cache_hits == 1
    assert usage.actual_calls == 1
    assert usage.cache_hit_rate == Decimal("50.00")


def test_cache_hit_under_a_different_prompt_version_does_not_hit(conn):
    """改了提示词版本必须重问 —— 否则「改完提示词输出没变」无人能解释。"""
    cache = DraftCache(conn)
    fp = request_fingerprint("同一份证据")
    cache.put(fp, "v1", MODEL, "[]")
    assert cache.lookup(fp, "v1", MODEL) is not None
    assert cache.lookup(fp, "v2", MODEL) is None


def test_cache_keeps_the_first_answer_and_a_silent_overwrite_is_refused(conn):
    """同一份证据问出两个不同答案时**保留第一次**；要换必须给出理由。"""
    cache = DraftCache(conn)
    fp = request_fingerprint("证据")
    assert cache.put(fp, "v1", MODEL, "第一次的答案") == "stored"
    assert cache.put(fp, "v1", MODEL, "第一次的答案") == "kept"
    with pytest.raises(ReconqError) as exc:
        cache.put(fp, "v1", MODEL, "第二次的答案")
    assert exc.value.code == "CACHE_OVERWRITE_DENIED"
    # 拒掉之后缓存里仍是第一次那份
    assert cache.lookup(fp, "v1", MODEL) == "第一次的答案"


def test_explicit_cache_correction_is_recorded_and_counted(conn):
    """人工改自动写入的数据必须留痕，否则「答案为什么变了」没人答得出。"""
    cache = DraftCache(conn)
    fp = request_fingerprint("证据")
    cache.put(fp, "v1", MODEL, "原答案")
    assert cache.put(fp, "v1", MODEL, "新答案", correction_reason="端点当时抽风") == "corrected"
    assert cache.lookup(fp, "v1", MODEL) == "新答案"
    corrections = cache.corrections()
    assert len(corrections) == 1
    assert corrections[0]["reason"] == "端点当时抽风"
    # 更正表**不存草稿正文** —— 它是「谁动过」的账本，不是第二个答案库
    assert "新答案" not in json.dumps(corrections, ensure_ascii=False)


def test_cache_lookup_counts_hits_but_never_reports_a_miss(conn):
    """"这条答案被复用了几次"可回答；未命中不该把计数加上去。"""
    cache = DraftCache(conn)
    fp = request_fingerprint("证据")
    cache.put(fp, "v1", MODEL, "答案")
    assert cache.lookup(fp, "v1", "别的模型") is None
    cache.lookup(fp, "v1", MODEL)
    cache.lookup(fp, "v1", MODEL)
    stats = cache.stats()
    assert stats["entries"] == 1
    assert stats["hits"] == 2


# ───────────────────── 三、按账期聚合数字正确 ─────────────────────


def test_period_aggregate_counts_cache_hits_separately_from_real_calls(conn):
    """「这一期调了几次」有两个答案，差着好几倍。

    财务科问「花了多少钱」时指的是**真正发出的**那几次。
    """
    ledger = ModelCallLedger(conn)
    ledger.record(_call(fingerprint="fp-1", elapsed_ms=1000.0, prompt_tokens=800, completion_tokens=200))
    ledger.record(_call(fingerprint="fp-2", elapsed_ms=3000.0, prompt_tokens=1200, completion_tokens=400))
    ledger.record(_call(fingerprint="fp-1", cache_hit=True, elapsed_ms=0.0, prompt_tokens=0, completion_tokens=0))
    ledger.record(_call(fingerprint="fp-3", cache_hit=True, elapsed_ms=0.0, prompt_tokens=0, completion_tokens=0))

    usage = usage_of_period(conn, PERIOD, env=PRICED_ENV)
    assert usage.calls == 4
    assert usage.cache_hits == 2
    assert usage.actual_calls == 2
    assert usage.cache_hit_rate == Decimal("50.00")
    # 耗时只按真正发出的两次算：把耗时 0 的命中混进平均会让「模型变慢」消失
    assert usage.total_elapsed_ms == 4000.0
    assert usage.avg_elapsed_ms == 2000.0
    assert usage.max_elapsed_ms == 3000.0
    assert usage.prompt_tokens == 2000
    assert usage.completion_tokens == 600
    assert usage.distinct_fingerprints == 3


def test_period_cost_uses_per_thousand_token_and_separates_prompt_from_completion(conn):
    """单价单位是**元/千 token**；提示词与生成两侧分开计价。

    故意用不对称的 0.012 / 0.036：对称的话「两侧算反了」在断言里看不出来。
    """
    ModelCallLedger(conn).record(
        _call(prompt_tokens=1000, completion_tokens=1000)
    )
    usage = usage_of_period(conn, PERIOD, env=PRICED_ENV)
    assert usage.priced is True
    # 1000 token × 0.012 元/千 = 0.012；1000 × 0.036 = 0.036
    assert usage.cost == Decimal("0.0480")
    assert usage.cost_text == "0.0480 元"


def test_cost_is_not_estimated_when_prices_are_absent(conn):
    """**缺单价时显示「未配置单价」，不是「0.00 元」**。

    显示 0 元会被读成「这一期真的一次钱没花」—— 而真相是「不知道」。
    """
    ModelCallLedger(conn).record(_call())
    usage = usage_of_period(conn, PERIOD, env=UNPRICED_ENV)
    assert usage.priced is False
    assert usage.cost_text == "未配置单价"
    assert "0.00" not in usage.cost_text
    # token 数照样给得出来 —— 单价缺的是钱，不是用量
    assert usage.total_tokens == 1000


def test_half_configured_price_does_not_produce_a_half_cost(conn):
    """只配提示词单价**不**做半截计价：生成侧往往不比提示词少。"""
    ModelCallLedger(conn).record(_call())
    usage = usage_of_period(
        conn,
        PERIOD,
        env={"RECONQ_MODEL_PRICE_PROMPT_PER_1K": PRICE_PROMPT},
    )
    assert usage.priced is False
    assert usage.cost_text == "未配置单价"


def test_empty_period_does_not_report_zero_cost_as_a_real_conclusion(conn):
    """零次调用的账期不能显示「成本 0.0000 元」—— 那看起来像「真没花钱」。"""
    usage = usage_of_period(conn, PERIOD, env=PRICED_ENV)
    assert usage.calls == 0
    assert usage.actual_calls == 0
    assert usage.cache_hit_rate == Decimal("0.00")
    assert usage.avg_elapsed_ms == 0.0
    assert usage.cost_text == "未配置单价"


def test_period_aggregate_is_scoped_to_one_period(conn):
    """口径按账期分：上一期的调用不能算进这一期。"""
    ledger = ModelCallLedger(conn)
    ledger.record(_call(fingerprint="fp-a"))
    ledger.record(
        ModelCall(
            call_id="",
            period="2026-W35",
            conclusion_key="上一期",
            channel="wechat",
            model=MODEL,
            request_fingerprint="fp-z",
            prompt_tokens=9999,
        )
    )
    usage = usage_of_period(conn, PERIOD, env=PRICED_ENV)
    assert usage.calls == 1
    assert usage.prompt_tokens == 800
    assert ModelCallLedger(conn).verify_chain(PERIOD).ok is True
    assert ModelCallLedger(conn).verify_chain("2026-W35").ok is True


def test_degraded_calls_are_counted_by_real_error_code(conn):
    """降级按**错误码**分组，不是一句「归因不可用」。

    重试用尽、连接超时、熔断开启的处置动作完全不同，合并成一个数之后
    没人知道该先查网络还是先查端点。
    """
    ledger = ModelCallLedger(conn)
    ledger.record(_call(fingerprint="fp-1", status="degraded", error_code="MODEL_TIMEOUT"))
    ledger.record(_call(fingerprint="fp-2", status="degraded", error_code="MODEL_TIMEOUT"))
    ledger.record(_call(fingerprint="fp-3", status="degraded", error_code="MODEL_CIRCUIT_OPEN"))
    usage = usage_of_period(conn, PERIOD, env=PRICED_ENV)
    assert usage.degraded == 3
    assert usage.errors == {"MODEL_TIMEOUT": 2, "MODEL_CIRCUIT_OPEN": 1}
    assert "MODEL_TIMEOUT×2" in usage.render()


def test_retried_calls_are_counted_separately_from_attempts(conn):
    ledger = ModelCallLedger(conn)
    ledger.record(_call(fingerprint="fp-1", retried=True, attempts=3))
    ledger.record(_call(fingerprint="fp-2"))
    usage = usage_of_period(conn, PERIOD, env=PRICED_ENV)
    assert usage.calls == 2
    assert usage.retried == 1


def test_usage_of_call_refuses_to_price_a_half_configured_pair():
    """单条层面就拒：任一单价缺位即 ``priced=False``，不做半截计价。"""
    unpriced = ModelUsage.of_call(100, 200, prompt_price=None, completion_price=Decimal("1"))
    assert unpriced.priced is False
    assert unpriced.cost == Decimal("0")
    priced = ModelUsage.of_call(
        100, 200, prompt_price=Decimal("0.01"), completion_price=Decimal("0.02")
    )
    assert priced.priced is True
    assert priced.cost == Decimal("0.005")
    # 相加时只要有一条没定价，整体就不是「可定价」的
    assert (priced + unpriced).priced is False


def test_run_summary_picks_up_model_usage_without_disturbing_other_fields(conn):
    """摘要层只加成本几项，**其余汇总项一字不变**（Task 30 的同一条纪律）。"""
    ModelCallLedger(conn).record(_call())
    usage = usage_of_period(conn, PERIOD, env=PRICED_ENV)
    summary = build_run_summary(
        period=PERIOD,
        command="scan",
        channels=[],
        model_usage=usage,
    )
    assert summary.model_calls == 1
    assert summary.model_actual_calls == 1
    assert summary.model_cache_hits == 0
    assert summary.model_cache_hit_rate == "0.00%"
    assert summary.model_total_tokens == 1000
    # 800 提示 token × 0.012 元/千 = 0.0096；200 生成 token × 0.036 = 0.0072
    assert summary.model_cost_text == "0.0168 元"
    # 没给 model_usage 时其余字段与「不给」完全一致
    plain = build_run_summary(period=PERIOD, command="scan", channels=[])
    assert plain.model_calls == 0
    assert plain.to_dict()["model_cost_text"] == ""
    assert "model" not in plain.render_zh()
    # 给了却读不懂 → 显式报错，不静默丢字段
    with pytest.raises(ReconqError) as exc:
        build_run_summary(
            period=PERIOD, command="scan", channels=[], model_usage=object()
        )
    assert exc.value.code == "RUN_SUMMARY_INVALID"


# ───────────────────── 四、审计中无证据原文 ─────────────────────


#: 「这列可能装得下自由文本」的判据。**精确列名而非子串匹配** ——
#: 上一版用子串，结果把 ``prompt_version`` 与 ``prompt_tokens`` 一起判成
#: 可疑列（它们一个存版本串、一个存计数，都是受控的）。子串判据一旦误报，
#: 人就会去改判据而不是改表 —— 断言随即失去防住真问题的能力。
_TEXT_BEARING_COLUMN_NAMES = frozenset(
    {
        "prompt",
        "prompt_text",
        "messages",
        "response",
        "response_text",
        "content",
        "raw",
        "raw_line",
        "detail",
        "details",
        "text",
        "body",
        "draft",
        "draft_text",
        "evidence",
        "evidence_text",
    }
)


def test_audit_table_has_no_column_that_could_hold_evidence_text(conn):
    """**白名单建列**：不在 :data:`AUDIT_ALLOWED_FIELDS` 里的列根本不建。

    黑名单挡不住「将来有人加一列 detail」；白名单能。
    """
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({MODEL_CALL_TABLE})")
    }
    suspicious = columns & _TEXT_BEARING_COLUMN_NAMES
    assert suspicious == set(), f"审计表出现了可装正文的列：{suspicious}"
    # 受控的两列刻意留着：它们一个存版本串一个存计数，都不是正文。
    assert {"prompt_version", "prompt_tokens"} <= columns
    # 缓存表存草稿 JSON 是**另一回事**：草稿是模型的产出而非证据包正文，
    # 且它必须落到某个地方才能复用。断言它用的是专门的列名而不是通用 detail。
    cache_columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({MODEL_CALL_CACHE_TABLE})")
    }
    assert cache_columns & _TEXT_BEARING_COLUMN_NAMES == set()
    assert "draft_json" in cache_columns

    # 反向也钉一条：白名单里的每个字段都得真的建了列。白名单只写不校验
    # 的话，加字段的人会以为「已经加进审计了」，而它其实只活在内存里 ——
    # 症状是成本表少一列，且没有任何报错。
    for name in AUDIT_ALLOWED_FIELDS:
        assert name in columns, f"白名单字段 {name} 没有建列"
    # 链式三列（seq / prev_hash / hash）是存储实现，不进 ModelCall，
    # 但它们必须在 —— 少了任一条，链校验就退化成「逐条独立」，
    # 也就检不出「删掉中间一条」。
    assert {"seq", "prev_hash", "hash"} <= columns


def test_whole_database_scan_finds_no_evidence_text_after_a_real_call(conn):
    """跑一次真链路（走 MockTransport）后，**证据正文**在整库扫不到第二份。

    边界要写准，否则这条断言会变成自欺：``conclusion_key`` 里**确实**含
    渠道单号（它是台账外键，没有它「这次花的钱对应哪条差异」就答不出来），
    而同一个单号在 ``bill_rows`` 与 ``diff_items`` 里本来就有。存外键
    不构成「第二份账单事实」。

    真正不该出现的是**证据正文**：那一行里的金额、交易时间、费用类别
    拼起来的那一整条 —— 它只该存在于账单表与证据快照，**那两处都带源
    文件行号可回查，审计库不带**。没有行号的副本才是危险的那一份：
    有人拿着它去和渠道对账，对不上时查不到是哪一行错了。
    """
    evidence_row = "EX-SECRET-7788,66.00,2026-09-02 10:00:00,挂号费"
    conclusion_key = "align.channel_only#wechat:EX-SECRET-7788"
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return _ok_response()

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = ModelGateway(_config(), client=client)
    ledger = ModelCallLedger(conn)
    cache = DraftCache(conn)
    fp = request_fingerprint(evidence_row)

    result = gateway.chat_completion(
        [{"role": "user", "content": evidence_row}],
        bundle_audit={"char_count": 42, "line_count": 1, "conclusion_key": conclusion_key},
    )
    ledger.record(
        ModelCall.from_gateway_call(result.call, period=PERIOD, request_fingerprint=fp)
    )
    cache.put(fp, DEFAULT_PROMPT_VERSION, MODEL, result.content)

    # 逐表逐列扫
    dumps: list[str] = []
    for table in (MODEL_CALL_TABLE, MODEL_CALL_CACHE_TABLE, MODEL_CALL_CORRECTION_TABLE):
        for row in conn.execute(f"SELECT * FROM {table}"):
            dumps.append(json.dumps(dict(row), ensure_ascii=False, default=str))
    blob = "\n".join(dumps)

    # —— 证据正文：一个片段都不该在
    assert evidence_row not in blob
    assert "66.00" not in blob, "金额是证据正文的一部分"
    assert "挂号费" not in blob, "费用类别同属证据正文"
    assert "2026-09-02 10:00:00" not in blob, "交易时间同属证据正文"

    # —— 外键与统计量：该在，且是「哪条差异」与「多大一坨」
    assert conclusion_key in blob, "台账外键必须在，否则审计指不回差异"
    assert fp in blob, "指纹是复现的锚点，必须在"
    assert "42" in blob, "证据包体量（char_count）必须在"
    assert len(requests_seen) == 1


# ───────────────────── 五、append-only 校验 ─────────────────────


def test_audit_and_correction_tables_reject_update_and_delete(conn):
    """触发器**挡住**篡改（指纹只能事后**检出**）。"""
    append_only_guard(conn)
    ModelCallLedger(conn).record(_call())
    DraftCache(conn).put(request_fingerprint("e"), "v1", MODEL, "答案")

    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute(f"UPDATE {MODEL_CALL_TABLE} SET prompt_tokens = 0")
    assert MODEL_CALL_TABLE in str(exc.value)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {MODEL_CALL_TABLE}")

    # 更正表同样上锁：它回答的是「谁动过缓存」，被改掉就永远答不上来
    DraftCache(conn).put(
        request_fingerprint("e"), "v1", MODEL, "另一个答案", correction_reason="更正"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {MODEL_CALL_CORRECTION_TABLE}")


def test_chain_verification_passes_on_an_untouched_ledger(conn):
    ledger = ModelCallLedger(conn)
    for i in range(4):
        ledger.record(_call(fingerprint=f"fp-{i}"))
    verdict = ledger.verify_chain(PERIOD)
    assert verdict.ok is True
    assert verdict.checked == 4
    assert verdict.broken_seq == 0
    assert "链完整" in verdict.reason


def test_chain_verification_detects_a_tampered_value_and_names_the_row(conn):
    """把触发器拆掉改一条之后，链校验能检出并**指出断点序号**。

    检出是第二道防线（第一道是触发器挡住）。它必须能回答「第几条被改过」，
    否则财务科只知道「有问题」，不知道该找哪一条。
    """
    ledger = ModelCallLedger(conn)
    for i in range(4):
        ledger.record(_call(fingerprint=f"fp-{i}", prompt_tokens=100 * (i + 1)))
    # 模拟有人绕过触发器改了第 2 条的 token 数
    conn.execute(
        f"UPDATE {MODEL_CALL_TABLE} SET prompt_tokens = 999999 "
        "WHERE period = ? AND seq = 2",
        (PERIOD,),
    )
    verdict = ledger.verify_chain(PERIOD)
    assert verdict.ok is False
    assert verdict.broken_seq == 2
    assert verdict.checked == 1
    assert "被改过" in verdict.reason
    assert verdict.expected_hash != verdict.actual_hash
    payload = verdict.to_dict()
    assert payload["ok"] is False
    assert payload["broken_seq"] == 2


def test_chain_verification_detects_a_deleted_row_by_the_sequence_gap(conn):
    """删掉中间一条：序号跳号，而不是悄悄少一条。"""
    ledger = ModelCallLedger(conn)
    for i in range(4):
        ledger.record(_call(fingerprint=f"fp-{i}"))
    conn.execute(f"DELETE FROM {MODEL_CALL_TABLE} WHERE period = ? AND seq = 2", (PERIOD,))
    verdict = ledger.verify_chain(PERIOD)
    assert verdict.ok is False
    assert verdict.broken_seq == 3
    assert "跳号" in verdict.reason


def test_chain_verification_detects_a_rewritten_predecessor_hash(conn):
    """前驱指纹被改：即使本条自身内容完好，也接不上上一环。"""
    ledger = ModelCallLedger(conn)
    for i in range(3):
        ledger.record(_call(fingerprint=f"fp-{i}"))
    conn.execute(
        f"UPDATE {MODEL_CALL_TABLE} SET prev_hash = ? WHERE period = ? AND seq = 3",
        ("f" * 64, PERIOD),
    )
    verdict = ledger.verify_chain(PERIOD)
    assert verdict.ok is False
    assert verdict.broken_seq == 3
    assert "前驱指纹" in verdict.reason


def test_usage_surfaces_a_broken_chain_in_the_rendered_table(conn):
    """链断了要在终端看得见 —— 成本数字因此不可信，必须说出来。"""
    ledger = ModelCallLedger(conn)
    for i in range(3):
        ledger.record(_call(fingerprint=f"fp-{i}"))
    conn.execute(
        f"UPDATE {MODEL_CALL_TABLE} SET elapsed_ms = 1 WHERE period = ? AND seq = 1",
        (PERIOD,),
    )
    usage = usage_of_period(conn, PERIOD, env=PRICED_ENV)
    assert usage.chain is not None
    assert usage.chain.ok is False
    text = usage.render()
    assert "审计链校验未通过" in text
    assert "第 1 条" in text


# ───────────────────── CLI：reconq model-usage ─────────────────────


def test_cli_model_usage_prints_the_cost_table(tmp_path, capsys):
    db = str(tmp_path / "usage.db")
    c = connect(db)
    try:
        init_schema(c)
        init_audit_schema(c)
        ledger = ModelCallLedger(c)
        ledger.record(_call(fingerprint="fp-1", conclusion_key="align.channel_only#wechat:EX-9"))
        ledger.record(_call(fingerprint="fp-1", conclusion_key="align.channel_only#wechat:EX-9", cache_hit=True))
    finally:
        c.close()

    assert main(["model-usage", "--period", PERIOD, "--db", db]) == 0
    out = capsys.readouterr().out
    assert "模型调用 2 次" in out
    assert "真正发出 1 次" in out
    assert "缓存命中 1 次" in out
    assert "50.00%" in out


def test_cli_model_usage_json_carries_every_number_the_panel_needs(tmp_path, capsys):
    db = str(tmp_path / "usage.db")
    c = connect(db)
    try:
        init_schema(c)
        init_audit_schema(c)
        ModelCallLedger(c).record(_call())
    finally:
        c.close()

    assert main(["model-usage", "--period", PERIOD, "--db", db, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["calls"] == 1
    assert payload["actual_calls"] == 1
    assert payload["cache_hits"] == 0
    assert payload["total_tokens"] == 1000
    assert payload["chain"]["ok"] is True


def test_cli_model_usage_returns_nonzero_when_the_chain_is_broken(tmp_path, capsys, monkeypatch):
    """链断时表仍打得出来（让人看得见），但退出码非零（告诉脚本不可信）。"""
    for name, value in PRICE_ENV_VARS.items():
        monkeypatch.setenv(name, value)
    db = str(tmp_path / "usage.db")
    c = connect(db)
    try:
        init_schema(c)
        init_audit_schema(c)
        for i in range(3):
            ModelCallLedger(c).record(_call(fingerprint=f"fp-{i}"))
        c.execute(
            f"UPDATE {MODEL_CALL_TABLE} SET prompt_tokens = 0 WHERE period = ? AND seq = 2",
            (PERIOD,),
        )
    finally:
        c.close()

    assert main(["model-usage", "--period", PERIOD, "--db", db, "--calls"]) == 1
    out = capsys.readouterr().out
    assert "审计链校验未通过" in out
    assert "逐次调用" in out


def test_cli_model_usage_says_so_when_a_period_has_no_calls(tmp_path, capsys):
    db = str(tmp_path / "usage.db")
    c = connect(db)
    try:
        init_schema(c)
        init_audit_schema(c)
    finally:
        c.close()
    assert main(["model-usage", "--period", PERIOD, "--db", db]) == 0
    out = capsys.readouterr().out
    assert "没有任何模型调用记录" in out
    assert "model.degraded" in out


# ───────────────────── 小工具 ─────────────────────


def _config() -> GatewayConfig:
    return GatewayConfig(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_KEY,
        model=MODEL,
        jitter_ratio=0.0,
    )


def _ok_response(body: str = "[]") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": MODEL,
            "choices": [
                {
                    "message": {"role": "assistant", "content": body},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 800,
                "completion_tokens": 200,
                "total_tokens": 1000,
            },
        },
    )
