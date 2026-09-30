"""归因草稿：结构、证据引用越界作废、降级不推进状态（Task 28）。

三类规定断言逐条可核：

1. **草稿结构** —— ``{cause, confidence, evidence_refs[], explanation_zh}``
   逐字段校验；不合约定抛错并被网关归一为 ``MODEL_SCHEMA_INVALID``。
   **不做「尽量修一修」**：百分制 ``confidence=85``、字符串 ``"0.8"``、
   未登记字段一律拒收 —— 修的是模型的错，代价是这份草稿不再可复算。
2. **证据引用越界作废** —— ``evidence_refs`` 里的每个 ref 必须落在证据包
   真实出现过的行标识集合内；越界即**整条作废**并留拒因，**不做就近匹配**
   （把 ``:18`` 悄悄改成 ``:17`` 会让一份编造的结论看起来有据可查）。
3. **降级不推进状态** —— 模型不可达 / 超时 / 重试用尽 / 响应不合协议 /
   全部草稿被拒，全部收敛到 ``degraded=True`` + ``causes=()``；
   :class:`CauseDraft` **结构上没有** ``disposition`` 字段，
   降级路径不碰任何台账状态迁移。

外加几条本任务最容易被糊弄过去的性质：

* **草稿必为草稿** —— ``CauseDraft`` 上没有可写状态的字段，从类型层面
  杜绝「模型顺手把结论确认了」；
* **排名稳定** —— 同分时保持模型给出的原顺序，否则「成因 Top-1 命中率」
  这条指标无法复算；
* **可复算** —— 同一份证据包 + 同一份模型输出，两次归因结果逐字段相同；
* **审计留痕** —— 被拒的草稿带拒因进结果，不静默丢弃。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import httpx
import pytest

from reconq.align.attribution import (
    ATTRIBUTION_SCHEMA,
    MAX_CAUSES,
    AttributionResult,
    CauseDraft,
    DraftValidationError,
    attribute,
    attribute_many,
    evidence_refs_of,
    validate_draft,
)
from reconq.align.bundle import EvidenceBundle, build_evidence_bundle
from reconq.align.matcher import AlignRecord, align_channel, build_index
from reconq.contracts.loader import load_contract
from reconq.errors import error_meta
from reconq.engine.period import (
    DIFF_TABLE,
    DISPOSITION_CONFIRMED,
    DISPOSITION_PENDING,
    DiffItem,
    init_scan_schema,
    list_diff_items,
)
from reconq.gateway.model_gateway import GatewayConfig, ModelGateway, build_prompt
from reconq.sources.timezone import HOSPITAL_TZ, period_window
from reconq.store.schema import connect, init_schema

PERIOD = "2026-W36"
TZ = HOSPITAL_TZ
FAKE_BASE_URL = "http://127.0.0.1:9/v1"


def _t(text: str):
    from datetime import datetime

    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _chan(order_no: str, amount: str, *, time: str = "2026-09-02 10:00:00", row: int = 3) -> AlignRecord:
    return AlignRecord(
        channel="wechat",
        period=PERIOD,
        order_no=order_no,
        amount=Decimal(amount),
        trade_time=_t(time),
        source_file="wechat_bill.csv",
        source_row_no=row,
        ch_id=1,
    )


def _his(order_no: str, amount: str, *, time: str = "2026-09-02 10:00:00", row: int = 17, ch_id: int = 2):
    return AlignRecord(
        channel="his",
        period=PERIOD,
        order_no=order_no,
        his_order_no=order_no,
        amount=Decimal(amount),
        trade_time=_t(time),
        department="门诊收费室",
        settle_type="微信",
        source_file="charge_record.csv",
        source_row_no=row,
        ch_id=ch_id,
    )


def _ambiguous_bundle() -> EvidenceBundle:
    """一码付拆多笔的歧义：一条渠道行对上两条 HIS 记录。"""
    index = build_index([_his("EX-1", "50.00", row=17), _his("EX-1", "78.00", row=18, ch_id=3)], period=PERIOD)
    outcome = align_channel(
        "wechat", [_chan("EX-1", "128.00")], index, period=PERIOD, window=period_window(PERIOD, TZ)
    ).outcomes[0]
    return build_evidence_bundle(
        outcome, contract=load_contract("contracts/wechat.yml", {"period": PERIOD}), window=period_window(PERIOD, TZ)
    )


def _channel_only_bundle() -> EvidenceBundle:
    """单边账：渠道有、HIS 查无此笔。"""
    index = build_index([], period=PERIOD)
    outcome = align_channel(
        "wechat", [_chan("EX-9", "66.00")], index, period=PERIOD, window=period_window(PERIOD, TZ)
    ).outcomes[0]
    return build_evidence_bundle(
        outcome, contract=load_contract("contracts/wechat.yml", {"period": PERIOD}), window=period_window(PERIOD, TZ)
    )


def _gateway(drafts, *, raw: str | None = None) -> ModelGateway:
    body = raw if raw is not None else json.dumps({"drafts": drafts}, ensure_ascii=False)
    payload = {
        "model": "reconq-attribution-1",
        "choices": [{"message": {"role": "assistant", "content": body}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 640, "completion_tokens": 72, "total_tokens": 712},
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    cfg = GatewayConfig(base_url=FAKE_BASE_URL, api_key="FAKE_KEY", model="reconq-attribution-1")
    return ModelGateway(cfg, client=client, sleeper=lambda _s: None)


def _draft(cause: str = "跨日切分重复计入", confidence: float = 0.72, refs=None) -> dict:
    return {
        "cause": cause,
        "confidence": confidence,
        "evidence_refs": refs if refs is not None else ["wechat_bill.csv:3"],
        "explanation_zh": "该笔收费时间落在账期窗口之外，很可能已计入下一期。",
    }


# ───────────────────── 一、草稿结构 ─────────────────────


def test_valid_draft_normalizes_whitespace():
    draft = validate_draft(
        {
            "cause": "  退费红冲未在渠道账单反映  ",
            "confidence": 0.5,
            "evidence_refs": ["  wechat_bill.csv:3  "],
            "explanation_zh": "  HIS 侧已红冲，渠道账单未体现。  ",
        }
    )
    assert draft.cause == "退费红冲未在渠道账单反映"
    assert draft.evidence_refs == ("wechat_bill.csv:3",)
    assert draft.explanation_zh == "HIS 侧已红冲，渠道账单未体现。"


@pytest.mark.parametrize(
    "raw,needle",
    [
        ({"cause": "", "confidence": 0.5, "evidence_refs": ["a:1"], "explanation_zh": "x"}, "cause 必须"),
        ({"cause": "c", "confidence": "0.8", "evidence_refs": ["a:1"], "explanation_zh": "x"}, "confidence 必须是"),
        ({"cause": "c", "confidence": 85, "evidence_refs": ["a:1"], "explanation_zh": "x"}, "看着像百分制"),
        ({"cause": "c", "confidence": 1.4, "evidence_refs": ["a:1"], "explanation_zh": "x"}, "越界"),
        ({"cause": "c", "confidence": -0.1, "evidence_refs": ["a:1"], "explanation_zh": "x"}, "越界"),
        ({"cause": "c", "confidence": 0.5, "evidence_refs": [], "explanation_zh": "x"}, "非空数组"),
        ({"cause": "c", "confidence": 0.5, "evidence_refs": "a:1", "explanation_zh": "x"}, "非空数组"),
        ({"cause": "c", "confidence": 0.5, "evidence_refs": [""], "explanation_zh": "x"}, "非空字符串"),
        ({"cause": "c", "confidence": 0.5, "evidence_refs": ["a:1"], "explanation_zh": ""}, "explanation_zh 必须"),
        (
            {"cause": "c", "confidence": 0.5, "evidence_refs": ["a:1"], "explanation_zh": "x", "disposition": "已确认"},
            "未登记字段",
        ),
        ({"cause": "c", "confidence": True, "evidence_refs": ["a:1"], "explanation_zh": "x"}, "confidence 必须是"),
    ],
)
def test_non_conforming_drafts_are_rejected_not_repaired(raw, needle):
    """不合约定即拒。**不把 85 悄悄除以 100、不把 "0.8" 强转成数字** ——
    修的是模型的错，代价是这份草稿不再可复算。"""
    with pytest.raises(DraftValidationError) as exc:
        validate_draft(raw)
    assert needle in exc.value.detail
    # 校验失败也必须带错误码与建议动作：不允许裸 ValueError 冒到面板
    assert exc.value.code == "ATTRIBUTION_DRAFT_INVALID"
    assert error_meta(exc.value.code).suggestion


def test_draft_type_has_no_disposition_field():
    """草稿在**类型层面**就装不下「已确认」—— 想推进状态只能另走白名单迁移。"""
    assert "disposition" not in CauseDraft.__dataclass_fields__
    payload = CauseDraft(cause="c", confidence=0.1, evidence_refs=("a:1",), explanation_zh="x").to_dict()
    assert set(payload) == {"cause", "confidence", "evidence_refs", "explanation_zh", "rank"}


def test_attribution_schema_is_the_single_source_of_truth():
    """schema 同时用于发模型（response_format）与本地校验，只有一处定义。"""
    assert set(ATTRIBUTION_SCHEMA["required"]) == {
        "cause",
        "confidence",
        "evidence_refs",
        "explanation_zh",
    }
    # 未登记字段被拒的判据与 schema 的 additionalProperties=False 一致
    with pytest.raises(DraftValidationError) as exc:
        validate_draft({**_draft(), "extra": 1})
    assert "未登记字段" in exc.value.detail


# ───────────────────── 二、证据引用越界作废 ─────────────────────


def test_evidence_refs_cover_diff_row_candidates_and_history():
    bundle = _ambiguous_bundle()
    refs = evidence_refs_of(bundle)
    assert "wechat_bill.csv:3" in refs          # 差异行
    assert "charge_record.csv:17" in refs        # 候选行
    assert "charge_record.csv:18" in refs        # 候选行
    assert len(set(refs)) == len(refs), "ref 必须去重"


def test_evidence_refs_preserve_order_without_duplicates():
    bundle = _channel_only_bundle()
    refs = evidence_refs_of(bundle)
    assert refs[0] == "wechat_bill.csv:3"
    assert list(refs) == list(dict.fromkeys(refs))


def test_out_of_range_evidence_ref_voids_the_whole_draft():
    """越界即整条作废，**不做就近匹配**。"""
    bundle = _channel_only_bundle()
    result = attribute(
        bundle,
        _gateway([_draft(refs=["wechat_bill.csv:99"])]),
    )
    assert result.degraded is True
    assert result.causes == ()
    assert result.needs_manual is True
    assert result.error_code == "ATTRIBUTION_EVIDENCE_REF_INVALID"
    assert len(result.rejected) == 1
    rej = result.rejected[0]
    assert rej.cause == "跨日切分重复计入"
    assert "wechat_bill.csv:99" in rej.detail
    assert "不做就近匹配" in rej.detail


def test_partially_valid_batch_keeps_valid_and_records_rejected():
    """一批草稿里越界的那条被拒，合规的照收 —— 拒一条不废全批。"""
    bundle = _channel_only_bundle()
    result = attribute(
        bundle,
        _gateway(
            [
                _draft(cause="导出方漏传", confidence=0.6, refs=["wechat_bill.csv:3"]),
                _draft(cause="跨日切分", confidence=0.9, refs=["charge_record.csv:999"]),
            ]
        ),
    )
    assert result.degraded is False
    assert [c.cause for c in result.causes] == ["导出方漏传"]
    assert len(result.rejected) == 1
    assert result.rejected[0].cause == "跨日切分"


def test_empty_bundle_refs_reject_every_draft():
    """证据包里一行都没有时，任何草稿都无法核对依据 —— 整批拒。"""
    bundle = _channel_only_bundle()
    stripped = EvidenceBundle(
        payload={**bundle.payload, "diff_row": {}, "candidate_rows": []},
        text=bundle.text,
        digest=bundle.digest,
    )
    result = attribute(stripped, _gateway([_draft()]))
    assert result.causes == ()
    assert result.error_code == "ATTRIBUTION_EVIDENCE_REF_INVALID"
    assert "没有任何可引用的行标识" in result.rejected[0].detail


# ───────────────────── 三、降级不推进状态 ─────────────────────


@pytest.mark.parametrize(
    "status,code",
    [
        (500, "MODEL_UNAVAILABLE"),
        (401, "MODEL_UNAVAILABLE"),
        (404, "MODEL_UNAVAILABLE"),
    ],
)
def test_http_failures_degrade_to_manual_judgement(status, code):
    payload = {"error": {"message": "boom"}}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload)))
    gw = ModelGateway(
        GatewayConfig(base_url=FAKE_BASE_URL, api_key="FAKE_KEY", model="m"),
        client=client,
        sleeper=lambda _s: None,
    )
    result = attribute(_channel_only_bundle(), gw)
    assert result.degraded is True
    assert result.causes == ()
    assert result.error_code == code
    assert result.needs_manual is True
    assert "待人工判定" in result.status_zh


def test_timeout_degrades_without_blocking():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gw = ModelGateway(
        GatewayConfig(base_url=FAKE_BASE_URL, api_key="FAKE_KEY", model="m"),
        client=client,
        sleeper=lambda _s: None,
    )
    result = attribute(_channel_only_bundle(), gw)
    assert result.error_code == "MODEL_TIMEOUT"
    assert result.causes == ()
    assert result.gateway_call["attempts"] == 3


def test_schema_failure_degrades_and_gives_up_after_retry_cap():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps({"candidates": []})}}]},
            )
        )
    )
    gw = ModelGateway(
        GatewayConfig(base_url=FAKE_BASE_URL, api_key="FAKE_KEY", model="m"),
        client=client,
        sleeper=lambda _s: None,
    )
    result = attribute(_channel_only_bundle(), gw)
    assert result.degraded is True
    assert result.error_code == "MODEL_SCHEMA_INVALID"
    assert result.gateway_call["attempts"] == 3
    assert result.gateway_call["retried"] is True


def test_missing_gateway_is_stated_explicitly_not_silently_empty():
    """没给网关 = 部署时没配端点。**明说**，不静默返回「没有成因」。"""
    result = attribute(_channel_only_bundle(), None)
    assert result.degraded is True
    assert result.error_code == "MODEL_CONFIG_INVALID"
    assert result.causes == ()
    assert "RECONQ_MODEL_" in result.error_detail


def test_degradation_never_advances_ledger_state():
    """降级前后差异台账逐字段一致 —— 模型挂了不推进任何状态。"""
    conn = connect(":memory:")
    init_schema(conn)
    init_scan_schema(conn)
    item = DiffItem(
        period=PERIOD,
        channel="wechat",
        check_path="align.channel_only",
        check_name="单边账",
        message="渠道有、HIS 查无此笔",
        diff_amount="66.00",
        source_file="wechat_bill.csv",
        first_row_no=3,
        needs_attribution=True,
    )
    conn.execute(
        f"INSERT INTO {DIFF_TABLE} (period, channel, check_path, check_name, message, diff_amount,"
        f" source_file, first_row_no, disposition, needs_attribution)"
        f" VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            item.period,
            item.channel,
            item.check_path,
            item.check_name,
            item.message,
            item.diff_amount,
            item.source_file,
            item.first_row_no,
            DISPOSITION_PENDING,
            1,
        ),
    )
    conn.commit()
    before = [d.to_dict() for d in list_diff_items(conn, period=PERIOD)]

    # 三条降级路径全跑一遍
    for gw in (None, _gateway([_draft(refs=["wechat_bill.csv:99"])]), _gateway([])):
        out = attribute(_channel_only_bundle(), gw)
        assert out.needs_manual is True
        assert out.causes == ()

    conn.commit()
    after = [d.to_dict() for d in list_diff_items(conn, period=PERIOD)]
    assert after == before
    assert after[0]["disposition"] == DISPOSITION_PENDING
    assert after[0]["disposition"] != DISPOSITION_CONFIRMED
    conn.close()


def test_empty_draft_list_is_schema_failure_not_silent_success():
    result = attribute(_channel_only_bundle(), _gateway([]))
    assert result.degraded is True
    assert result.error_code == "MODEL_SCHEMA_INVALID"
    assert "drafts 为空" in result.error_detail


# ───────────────────── 四、排序与可复算 ─────────────────────


def test_causes_are_ranked_by_confidence_desc():
    bundle = _channel_only_bundle()
    result = attribute(
        bundle,
        _gateway(
            [
                _draft(cause="导出方漏传", confidence=0.35, refs=["wechat_bill.csv:3"]),
                _draft(cause="跨日切分重复计入", confidence=0.82, refs=["wechat_bill.csv:3"]),
                _draft(cause="退费红冲未反映", confidence=0.55, refs=["wechat_bill.csv:3"]),
            ]
        ),
    )
    assert [c.cause for c in result.causes] == [
        "跨日切分重复计入",
        "退费红冲未反映",
        "导出方漏传",
    ]
    assert [c.rank for c in result.causes] == [1, 2, 3]


def test_ranking_is_stable_for_equal_confidence():
    """同分保持模型给出的原顺序，否则「成因 Top-1 命中率」无法复算。"""
    bundle = _channel_only_bundle()
    drafts = [
        _draft(cause="A", confidence=0.5, refs=["wechat_bill.csv:3"]),
        _draft(cause="B", confidence=0.5, refs=["wechat_bill.csv:3"]),
        _draft(cause="C", confidence=0.5, refs=["wechat_bill.csv:3"]),
    ]
    result = attribute(bundle, _gateway(drafts))
    assert [c.cause for c in result.causes] == ["A", "B", "C"]


def test_more_than_max_causes_is_truncated_but_documented():
    bundle = _channel_only_bundle()
    drafts = [
        _draft(cause=f"成因{i}", confidence=0.9 - i * 0.01, refs=["wechat_bill.csv:3"])
        for i in range(MAX_CAUSES + 3)
    ]
    result = attribute(bundle, _gateway(drafts))
    assert len(result.causes) == MAX_CAUSES
    assert [c.cause for c in result.causes][:2] == ["成因0", "成因1"]


def test_same_input_yields_byte_identical_causes():
    """同一份证据 + 同一份模型输出 → 两次归因的**草稿部分**逐字段相同（可复算）。

    比对刻意排除 ``elapsed_ms``：耗时是真实测量值，两次必然不同。把它算进
    「可复算」的判据里，这条断言就只剩一个永远为真的空检查。
    """
    bundle = _channel_only_bundle()
    drafts = [
        _draft(cause="跨日切分重复计入", confidence=0.72),
        _draft(cause="导出方漏传", confidence=0.4),
    ]
    a = attribute(bundle, _gateway(drafts)).to_dict()
    b = attribute(bundle, _gateway(drafts)).to_dict()
    for payload in (a, b):
        payload["gateway_call"].pop("elapsed_ms")
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(
        b, ensure_ascii=False, sort_keys=True
    )


def test_batch_attribution_isolates_failures():
    """逐条独立：一条降级不影响其余拿到草稿。"""
    bundles = [_channel_only_bundle(), _ambiguous_bundle()]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][1]["content"]
        drafts = (
            [_draft(cause="导出方漏传", confidence=0.6, refs=["wechat_bill.csv:3"])]
            if "EX-9" in prompt or "wechat_bill.csv:3" in prompt
            else []
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"drafts": drafts})}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gw = ModelGateway(
        GatewayConfig(base_url=FAKE_BASE_URL, api_key="FAKE_KEY", model="m"),
        client=client,
        sleeper=lambda _s: None,
    )
    results = attribute_many(bundles, gw)
    assert len(results) == 2
    assert all(r.degraded is False for r in results), "两包证据都应给出草稿"


# ───────────────────── 五、留痕与渲染 ─────────────────────


def test_rejected_drafts_are_kept_with_reason():
    bundle = _channel_only_bundle()
    result = attribute(
        bundle,
        _gateway(
            [
                _draft(cause="好的成因", confidence=0.7, refs=["wechat_bill.csv:3"]),
                _draft(cause="编造的成因", confidence=0.95, refs=["nope.csv:1"]),
            ]
        ),
    )
    payload = result.to_dict()
    assert len(payload["causes"]) == 1
    assert len(payload["rejected"]) == 1
    assert payload["rejected"][0]["error_code"] == "ATTRIBUTION_EVIDENCE_REF_INVALID"
    assert "编造的成因" not in [c["cause"] for c in payload["causes"]]


def test_result_carries_allowed_refs_and_bundle_digest():
    bundle = _channel_only_bundle()
    result = attribute(bundle, _gateway([_draft()]))
    assert result.allowed_refs == evidence_refs_of(bundle)
    assert result.bundle_digest == bundle.digest
    assert result.conclusion_key == bundle.payload["conclusion_key"]


def test_render_shows_rank_refs_and_rejection_reason():
    bundle = _channel_only_bundle()
    result = attribute(
        bundle,
        _gateway([_draft(confidence=0.72), _draft(cause="编造", confidence=0.9, refs=["x.csv:1"])]),
    )
    text = result.render()
    assert "待人工判定" in text
    assert "1. 跨日切分重复计入" in text
    assert "0.72" in text
    assert "wechat_bill.csv:3" in text
    assert "已丢弃" in text and "ATTRIBUTION_EVIDENCE_REF_INVALID" in text


def test_degraded_render_explains_why():
    result = attribute(_channel_only_bundle(), None)
    text = result.render()
    assert "待人工判定" in text
    assert "RECONQ_MODEL_CONFIG" not in text
    assert "降级原因" in text


def test_to_dict_is_json_serializable_without_patient_fields():
    bundle = _channel_only_bundle()
    result = attribute(bundle, _gateway([_draft()]))
    blob = json.dumps(result.to_dict(), ensure_ascii=False)
    json.loads(blob)
    for banned in ("患者", "身份证", "住院号", "patient_name", "id_card"):
        assert banned not in blob


def test_gateway_audit_metadata_reaches_the_result():
    bundle = _channel_only_bundle()
    result = attribute(bundle, _gateway([_draft()]))
    call = result.gateway_call
    assert call["model"] == "reconq-attribution-1"
    assert call["attempts"] == 1
    assert call["total_tokens"] == 712
    assert len(call["request_digest"]) == 16
    # 证据包侧的体量与指纹原样带上（不含证据正文）
    assert call["bundle"]["bundle_digest"] == bundle.digest
    assert call["bundle"]["char_count"] == bundle.char_count
    assert "diff_row" not in call["bundle"]


def test_schema_is_actually_sent_to_the_model():
    """结构化声明要真的出现在请求体里，否则「结构化输出」只是文档说法。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"drafts": [_draft()]})}}]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gw = ModelGateway(
        GatewayConfig(base_url=FAKE_BASE_URL, api_key="FAKE_KEY", model="m"),
        client=client,
        sleeper=lambda _s: None,
    )
    attribute(_channel_only_bundle(), gw)
    rf = seen["body"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    # 发给模型的是外层 drafts 列表 schema，其 items 逐字段与本地校验用的
    # ATTRIBUTION_SCHEMA 一致 —— 同一份定义，不是两份写法。
    sent = rf["json_schema"]["schema"]
    assert set(sent["required"]) == {"drafts"}
    assert sent["properties"]["drafts"]["items"]["required"] == ATTRIBUTION_SCHEMA["required"]
    assert set(sent["properties"]["drafts"]["items"]["properties"]) == set(
        ATTRIBUTION_SCHEMA["properties"]
    )


def test_prompt_carries_the_draft_only_contract_in_system_turn():
    messages = build_prompt("证据正文", instructions="候选已裁剪")
    assert "草稿" in messages[0]["content"]
    assert "原样抄" in messages[0]["content"]
    assert "证据正文" in messages[1]["content"]
