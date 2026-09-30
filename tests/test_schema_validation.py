"""网关结构化输出与 Schema 多层校验（Task 29）。

五类规定断言逐条可核
--------------------

1. **包裹代码块可解析** —— ``extract_json`` 容忍 ```` ```json ```` 围栏、
   容忍前后**纯说明文字**，但正文里出现**两份** JSON（哪怕只是句子里举的
   格式示例）一律拒：那说明模型在讲解而不是在作答，捡哪一份都是编的。
2. **结构缺字段报错** —— 缺 ``cause``、``cause`` 为空、``evidence_refs``
   非数组、出现**未登记字段**（含 ``disposition``）都归到 ``structure``
   层、码 ``ATTRIBUTION_DRAFT_INVALID``。草稿在**类型层面**装不下处置
   状态，这条边界不能因为模型多写一个键就漏过去。
3. **越界 confidence 报错** —— 百分制 ``85``、``1.4``、``-0.1`` 都归到
   ``confidence`` 层、码 ``ATTRIBUTION_CONFIDENCE_OUT_OF_RANGE``；字符串
   ``"0.8"`` 与布尔 ``True`` 连类型层都过不了。**不修**：悄悄除以 100
   会让同一份输入两次归因得到两个置信度，指标表无法复算。
4. **越界 evidence_ref 作废** —— 引用证据包之外的行归到
   ``evidence_domain`` 层、码 ``ATTRIBUTION_EVIDENCE_REF_INVALID``，
   **整条作废**且不做就近匹配；一批里越界那条被拒、合规那条照收。
5. **非法结构不入库计数** —— :func:`save_drafts` 只接受已过六层的
   ``AttributionDraft``；原始 dict 塞进去被显式拒；被拒的草稿连拒因一起
   落 ``attribution_rejections`` 表并按层计数，**静默丢不允许**。

外加本任务最容易被糊弄过去的几条性质
------------------------------------

* **每层独立错误码**：六个码互不相同，且都登记在 ``errors.py`` 里有中文
  说明与建议动作（漏登记会让面板显示「未登记的错误码」）；
* **未配字典 = skipped 而不是 passed**：受控成因字典在 Task 33 落地，
  这期间 ``cause_vocab`` 层必须报「跳过」—— 把「没跑」记成「通过」，
  等于给未来埋一个「字典加上了却没人发现这层从来没跑过」的坑；
* **拒一条不废全批**，且拒因带草稿序号（排障时不用自己数）；
* **落库幂等**：同键重写行数与金额不变，拒因计数也不翻倍；
* **草稿恒为草稿**：表里的 ``disposition`` 恒为「待确认」，本模块不提供
  任何改写它的入口；
* **置信度存定点字符串**而非 ``REAL``：读回来做排序键要逐次一致。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from reconq.align.attribution import (
    ATTRIBUTION_SCHEMA,
    CauseDraft,
    DraftValidationError,
    validate_draft,
)
from reconq.errors import ERROR_CODES, error_meta
from reconq.gateway.schema import (
    ATTRIBUTION_DRAFT_FIELDS,
    LAYER_CODES,
    MAX_EXPLANATION_CHARS,
    AttributionDraft,
    EvidenceRef,
    SchemaLayerError,
    ValidationLayer,
    extract_json,
    layer_zh,
    parse_candidate_text,
    validate_candidates,
    validate_draft_layers,
)
from reconq.store.attribution_store import (
    ATTRIBUTION_DRAFT_TABLE,
    ATTRIBUTION_REJECTION_TABLE,
    DISPOSITION_DRAFT,
    init_attribution_schema,
    layer_failure_counts,
    list_drafts,
    list_rejections,
    save_drafts,
)
from reconq.store.schema import connect, init_schema

PERIOD = "2026-W36"
ALLOWED_REFS = ("wechat_bill.csv:3", "charge_record.csv:17", "charge_record.csv:18")
VOCAB = ("跨日切分重复计入", "导出方漏传", "退费红冲未在渠道账单反映")


def _draft(**overrides):
    payload = {
        "cause": "跨日切分重复计入",
        "confidence": 0.8,
        "evidence_refs": ["wechat_bill.csv:3"],
        "explanation_zh": "该笔收费时间落在账期窗口之外，很可能已计入下一期。",
    }
    payload.update(overrides)
    return payload


def _conn() -> sqlite3.Connection:
    conn = connect(":memory:")
    init_schema(conn)
    init_attribution_schema(conn)
    return conn


# ───────────────────── 一、包裹代码块可解析 ─────────────────────


def test_bare_json_parses():
    assert extract_json(json.dumps(_draft(), ensure_ascii=False)) == _draft()


def test_fenced_json_is_unwrapped():
    text = "```json\n" + json.dumps(_draft(), ensure_ascii=False) + "\n```"
    assert extract_json(text) == _draft()


def test_prose_around_a_single_json_is_tolerated():
    """端点加的客套话剥掉即可 —— 正文里只有一份 JSON 时没有歧义。"""
    text = (
        "以下是对这条差异的成因分析：\n"
        + json.dumps({"drafts": [_draft()]}, ensure_ascii=False)
        + "\n以上，如需补充证据请告知。"
    )
    assert extract_json(text)["drafts"][0]["cause"] == "跨日切分重复计入"


def test_fence_with_prose_on_both_sides_is_tolerated():
    text = (
        "分析如下：\n```json\n"
        + json.dumps({"drafts": [_draft()]}, ensure_ascii=False)
        + "\n```\n结束。"
    )
    assert extract_json(text)["drafts"][0]["confidence"] == 0.8


def test_nested_values_do_not_count_as_a_second_document():
    """``{"drafts":[{...}]}`` 里那个数组**不是**第二份 JSON。

    这条曾经真错过一次：扫描器逐字符前进、把片段内部的 ``[{...}]`` 也
    当成一个候选，于是一份完全正常的响应被判成「正文里有两份 JSON」而
    拒收 —— 而误拒好内容是本模块唯一不能犯的错。
    """
    payload = {"drafts": [_draft(), _draft(cause="导出方漏传")], "meta": {"n": 2, "tags": ["a", "b"]}}
    assert extract_json("结果：" + json.dumps(payload, ensure_ascii=False)) == payload


def test_braces_inside_json_strings_are_not_mistaken_for_structure():
    """行标识里带花括号（``a{1}.csv:3``）不该把配对扫描带偏。"""
    payload = {"drafts": [_draft(evidence_refs=['a{1}.csv:3', 'b[2].csv:9'])]}
    assert extract_json(json.dumps(payload, ensure_ascii=False)) == payload


def test_two_documents_in_the_prose_are_rejected_not_salvaged():
    """**不做就近匹配** —— 句子里举的格式示例 + 真答案同时出现时，
    捡哪一份都是编的，整条拒。"""
    text = (
        '根据证据，成因格式为 {"cause": "..."}：\n'
        + json.dumps({"drafts": [_draft()]}, ensure_ascii=False)
    )
    with pytest.raises(SchemaLayerError) as exc:
        extract_json(text)
    assert exc.value.layer is ValidationLayer.JSON
    assert exc.value.code == "MODEL_OUTPUT_UNPARSEABLE"
    assert "不猜" in exc.value.detail


def test_empty_content_is_rejected_at_the_json_layer():
    with pytest.raises(SchemaLayerError) as exc:
        extract_json("   \n  ")
    assert exc.value.code == "MODEL_OUTPUT_UNPARSEABLE"
    assert "为空" in exc.value.detail


def test_non_json_prose_is_rejected_at_the_json_layer():
    with pytest.raises(SchemaLayerError) as exc:
        extract_json("我认为这条差异是导出方的问题。")
    assert exc.value.layer is ValidationLayer.JSON


def test_json_layer_keeps_a_recoverable_excerpt_only():
    """超长响应只留片段。错误详情会进面板、进日志、进结论单。"""
    with pytest.raises(SchemaLayerError) as exc:
        extract_json("没有 JSON 的长篇解释。" * 200)
    assert "已截断" in exc.value.detail
    assert len(exc.value.detail) < 600


# ───────────────────── 二、结构缺字段报错 ─────────────────────


@pytest.mark.parametrize(
    "raw,needle",
    [
        ({"confidence": 0.5, "evidence_refs": ["a:1"], "explanation_zh": "x"}, "cause 必须"),
        (_draft(cause=""), "cause 必须"),
        (_draft(cause="   "), "cause 必须"),
        (_draft(cause=None), "cause 必须"),
        (_draft(cause=123), "cause 必须"),
        (_draft(evidence_refs=[]), "非空数组"),
        (_draft(evidence_refs="a:1"), "非空数组"),
        (_draft(evidence_refs={}), "非空数组"),
        (_draft(evidence_refs=[""]), "不是非空字符串"),
        (_draft(evidence_refs=[None]), "不是非空字符串"),
        # 空说明归 **explanation** 层（见 test_empty_explanation_has_its_own_layer），
        # 「说明字段不是字符串」才归 structure。
        (_draft(explanation_zh=None), "explanation_zh 必须"),
        (_draft(explanation_zh=123), "explanation_zh 必须"),
        (_draft(extra=1), "未登记字段"),
        ("不是对象", "必须是对象"),
        (["a"], "必须是对象"),
    ],
)
def test_structure_violations_are_rejected_with_needles(raw, needle):
    with pytest.raises(SchemaLayerError) as exc:
        validate_draft_layers(raw)
    assert exc.value.layer is ValidationLayer.STRUCTURE
    assert exc.value.code == "ATTRIBUTION_DRAFT_INVALID"
    assert needle in exc.value.detail


def test_empty_explanation_has_its_own_layer():
    """「是字符串但空」归 ``explanation`` 层，不并进 ``structure``。

    两者的处置动作不同：字段类型错是让模型改格式，空说明是**去问经办人
    补充背景**（该行确实没有可解释的背景）。并进 structure 等于把这个
    区别在排障入口处就抹掉。
    """
    for empty in ("", "   "):
        with pytest.raises(SchemaLayerError) as exc:
            validate_draft_layers(_draft(explanation_zh=empty))
        assert exc.value.layer is ValidationLayer.EXPLANATION
        assert exc.value.code == "ATTRIBUTION_EXPLANATION_INVALID"
        assert "explanation_zh 必须" in exc.value.detail


def test_disposition_field_is_rejected_before_anything_else_reads_the_draft():
    """草稿结构里**装不下**处置状态。模型多写一个键就得被挡住 ——
    挡住之后再谈「反正只是草稿」就晚了。"""
    with pytest.raises(SchemaLayerError) as exc:
        validate_draft_layers(_draft(disposition="已确认"))
    assert exc.value.layer is ValidationLayer.STRUCTURE
    assert "disposition" in exc.value.detail
    assert "未登记字段" in exc.value.detail


def test_structure_error_carries_the_draft_index_in_a_batch():
    with pytest.raises(SchemaLayerError) as exc:
        validate_draft_layers(_draft(cause=""), index=3)
    assert exc.value.index == 3
    assert "第 4 条草稿" in exc.value.where_zh


def test_wire_schema_agrees_with_the_pydantic_model():
    """发给模型的 ``response_format`` 与本地 pydantic 判据**逐字段一致**。

    两份写法不一致时，模型返回的 ``evidence_refs`` 会被自己的 schema 拒掉
    —— 而这种不一致在真实端点上要等到某次模型恰好少写一个字段才暴露。
    """
    assert set(ATTRIBUTION_SCHEMA["required"]) == set(ATTRIBUTION_DRAFT_FIELDS)
    assert set(ATTRIBUTION_SCHEMA["properties"]) == set(AttributionDraft.model_fields)
    assert ATTRIBUTION_SCHEMA["additionalProperties"] is False
    assert set(AttributionDraft.model_fields) == set(ATTRIBUTION_DRAFT_FIELDS)


# ───────────────────── 三、越界 confidence 报错 ─────────────────────


@pytest.mark.parametrize("value,needle", [(85, "看着像百分制"), (1.4, "越界"), (-0.1, "越界"), (100, "百分制")])
def test_out_of_range_confidence_has_its_own_layer_and_code(value, needle):
    with pytest.raises(SchemaLayerError) as exc:
        validate_draft_layers(_draft(confidence=value))
    assert exc.value.layer is ValidationLayer.CONFIDENCE
    assert exc.value.code == "ATTRIBUTION_CONFIDENCE_OUT_OF_RANGE"
    assert needle in exc.value.detail


def test_confidence_is_never_silently_repaired():
    """百分制不除以 100、字符串不强转。修的是模型的错，代价是这份草稿
    不再可复算。"""
    with pytest.raises(SchemaLayerError) as exc:
        validate_draft_layers(_draft(confidence=85))
    assert "0.85" in exc.value.detail  # 只在提示里出现，值本身没被改

    for bad in ("0.8", True, None, [0.8]):
        with pytest.raises(SchemaLayerError) as exc:
            validate_draft_layers(_draft(confidence=bad))
        assert exc.value.layer is ValidationLayer.STRUCTURE
        assert "confidence 必须是" in exc.value.detail


def test_boundary_confidence_is_accepted():
    for value in (0, 1, 0.0, 1.0):
        draft = validate_draft_layers(_draft(confidence=value))[0]
        assert Decimal(str(draft.confidence)) == Decimal(str(value))


def test_task28_compat_entry_keeps_its_needles():
    """Task 28 的 ``validate_draft`` 出口不变：同一个码、同一批 detail 措辞。"""
    with pytest.raises(DraftValidationError) as exc:
        validate_draft(_draft(confidence=85))
    assert exc.value.code == "ATTRIBUTION_DRAFT_INVALID"
    assert "看着像百分制" in exc.value.detail
    assert exc.value.code in ERROR_CODES


def test_task28_compat_entry_still_returns_the_cause_draft_shape():
    draft = validate_draft(_draft())
    assert isinstance(draft, CauseDraft)
    assert draft.evidence_refs == ("wechat_bill.csv:3",)
    assert "disposition" not in CauseDraft.__dataclass_fields__


# ───────────────────── 四、越界 evidence_ref 作废 ─────────────────────


def test_out_of_domain_evidence_ref_voids_the_whole_draft():
    with pytest.raises(SchemaLayerError) as exc:
        validate_draft_layers(_draft(evidence_refs=["wechat_bill.csv:99"]), allowed_refs=ALLOWED_REFS)
    assert exc.value.layer is ValidationLayer.EVIDENCE_DOMAIN
    assert exc.value.code == "ATTRIBUTION_EVIDENCE_REF_INVALID"
    assert "wechat_bill.csv:99" in exc.value.detail
    assert "不做就近匹配" in exc.value.detail


def test_in_domain_refs_pass_and_keep_their_order():
    draft = validate_draft_layers(
        _draft(evidence_refs=["charge_record.csv:18", "wechat_bill.csv:3"]),
        allowed_refs=ALLOWED_REFS,
    )[0]
    assert [r.ref for r in draft.evidence_refs] == ["charge_record.csv:18", "wechat_bill.csv:3"]


def test_one_bad_draft_does_not_void_the_batch():
    """拒一条不废全批 —— 一条编造的草稿不该把另外两条正经结论一起带走。"""
    report = validate_candidates(
        {
            "drafts": [
                _draft(cause="导出方漏传", evidence_refs=["wechat_bill.csv:3"]),
                _draft(cause="跨日切分重复计入", evidence_refs=["wechat_bill.csv:99"]),
                _draft(cause="退费红冲未在渠道账单反映", evidence_refs=["charge_record.csv:17"]),
            ]
        },
        allowed_refs=ALLOWED_REFS,
    )
    assert [d.cause for d in report.drafts] == ["导出方漏传", "退费红冲未在渠道账单反映"]
    assert len(report.rejected) == 1
    assert report.rejected[0].index == 1
    assert report.raw_count == 3
    # 3 收 2 是**成功**不是失败：被拒的那条是模型编的，收下它才是事故
    assert report.ok is True
    assert report.complete is False


def test_single_draft_form_is_accepted_without_the_outer_wrapper():
    report = validate_candidates(_draft())
    assert len(report.drafts) == 1


def test_outer_shape_violations_are_reported_as_structure_failures():
    for raw, needle in (
        ({"nope": 1}, "须含 drafts 数组"),
        ({"drafts": "abc"}, "drafts 必须是数组"),
        ({"drafts": []}, "drafts 为空"),
        ("纯文本", "顶层须为对象或数组"),
    ):
        with pytest.raises(SchemaLayerError) as exc:
            validate_candidates(raw)
        assert exc.value.layer is ValidationLayer.STRUCTURE
        assert needle in exc.value.detail


def test_evidence_domain_layer_is_skipped_when_no_domain_is_supplied():
    """没给行号域 = 这一层没有判据 → **skipped**，不是 passed。"""
    _draft_obj, outcomes = validate_draft_layers(_draft(evidence_refs=["任意:1"]))
    by_layer = {o.layer: o for o in outcomes}
    assert by_layer[ValidationLayer.EVIDENCE_DOMAIN].status == "skipped"
    assert by_layer[ValidationLayer.EVIDENCE_DOMAIN].ok is True
    assert "未参与判定" in by_layer[ValidationLayer.EVIDENCE_DOMAIN].detail


# ───────────────────── 五、非法结构不入库计数 ─────────────────────


def test_save_drafts_refuses_unvalidated_input():
    """原始 dict 塞不进去 —— 纪律落在类型上，不落在「记得先校验」上。"""
    conn = _conn()
    with pytest.raises(Exception) as exc:
        save_drafts(
            conn,
            [_draft()],  # dict，不是 AttributionDraft
            period=PERIOD,
            channel="wechat",
            conclusion_key="SINGLE_SIDED:EX-1",
        )
    assert exc.value.code == "ATTRIBUTION_DRAFT_INVALID"
    assert "六层校验" in exc.value.detail
    assert list_drafts(conn, PERIOD) == []


def test_only_validated_drafts_reach_the_table_and_rejections_are_counted():
    conn = _conn()
    report = validate_candidates(
        {
            "drafts": [
                _draft(cause="导出方漏传", confidence=0.6),
                _draft(cause="跨日切分重复计入", confidence=85),          # confidence 层
                _draft(cause="自由发挥的成因", evidence_refs=["x:1"]),      # evidence_domain 层
                _draft(cause="缺字段", explanation_zh=""),                 # explanation 层
                _draft(cause="多字段", disposition="已确认"),               # structure 层
            ]
        },
        allowed_refs=ALLOWED_REFS,
    )
    outcome = save_drafts(
        conn,
        report.drafts,
        period=PERIOD,
        channel="wechat",
        conclusion_key="SINGLE_SIDED:EX-1",
        run_id="RUN-T29",
        model_name="院内推理端点",
        rejected=report.rejected,
    )
    assert outcome.inserted == 1
    assert outcome.rejected == 4

    rows = list_drafts(conn, PERIOD)
    assert len(rows) == 1
    assert rows[0]["cause"] == "导出方漏传"
    assert rows[0]["disposition"] == DISPOSITION_DRAFT == "待确认"

    rejects = list_rejections(conn, PERIOD)
    assert len(rejects) == 4
    # 四个**不同的层**各记一条 —— 拒因按层可分，才回答得了「模型输出的
    # 问题集中在哪」：confidence 高说明口径没对齐，evidence_domain 高
    # 说明模型在按记忆编（后者调 prompt 没用，得改证据包）。
    assert {r["layer"] for r in rejects} == {
        "confidence",
        "evidence_domain",
        "explanation",
        "structure",
    }
    assert {r["layer"]: r["code"] for r in rejects} == {
        "confidence": "ATTRIBUTION_CONFIDENCE_OUT_OF_RANGE",
        "evidence_domain": "ATTRIBUTION_EVIDENCE_REF_INVALID",
        "explanation": "ATTRIBUTION_EXPLANATION_INVALID",
        "structure": "ATTRIBUTION_DRAFT_INVALID",
    }
    counts = layer_failure_counts(conn, PERIOD)
    assert counts == {
        "confidence": 1,
        "evidence_domain": 1,
        "explanation": 1,
        "structure": 1,
    }
    assert all(r["code"] and r["detail"] for r in rejects)


def test_rejection_records_never_contain_the_whole_raw_response():
    """拒因表会被面板整段展示。把整份原始响应塞进去等于把渠道账单行
    再抄一份到另一个库里。"""
    conn = _conn()
    report = validate_candidates(
        {"drafts": [_draft(evidence_refs=["wechat_bill.csv:99"])]}, allowed_refs=ALLOWED_REFS
    )
    save_drafts(
        conn,
        report.drafts,
        period=PERIOD,
        channel="wechat",
        conclusion_key="SINGLE_SIDED:EX-1",
        rejected=report.rejected,
    )
    (row,) = list_rejections(conn, PERIOD)
    assert "drafts" not in row["detail"]
    assert len(row["detail"]) < 300
    assert row["cause_text"] == ""  # 结构层失败时模型可能连 cause 都没给对


def test_saving_is_idempotent_for_both_drafts_and_rejections():
    conn = _conn()
    report = validate_candidates(
        {"drafts": [_draft(), _draft(cause="导出方漏传", confidence=85)]},
        allowed_refs=ALLOWED_REFS,
    )
    first = save_drafts(
        conn,
        report.drafts,
        period=PERIOD,
        channel="wechat",
        conclusion_key="SINGLE_SIDED:EX-1",
        rejected=report.rejected,
    )
    second = save_drafts(
        conn,
        report.drafts,
        period=PERIOD,
        channel="wechat",
        conclusion_key="SINGLE_SIDED:EX-1",
        rejected=report.rejected,
    )
    assert first.inserted == 1 and first.rejected == 1
    assert second.inserted == 0 and second.skipped == 1
    # 拒因计数不翻倍：重跑一次让计数变 2 就没法复算「这期丢了多少条」了
    assert second.rejected == 0
    assert len(list_drafts(conn, PERIOD)) == 1
    assert layer_failure_counts(conn, PERIOD) == {"confidence": 1}


def test_a_failing_batch_rolls_back_entirely():
    """半截落库比不落更糟：面板显示 3 条成因、实际只有 2 条经得起复核，
    而缺的第 3 条恰好是模型给得最含糊的那条。

    失败用**真的**数据库错误注入（把拒因表删掉，让第二条 INSERT 报
    ``no such table``），而不是 mock 掉 ``execute`` —— mock 掉驱动调用
    就测不到「事务真的回滚了」，只测到「异常真的抛出来了」。
    """
    conn = _conn()
    drafts = validate_candidates({"drafts": [_draft(), _draft(cause="导出方漏传")]}).drafts
    conn.execute(f"DROP TABLE {ATTRIBUTION_REJECTION_TABLE}")
    with pytest.raises(sqlite3.Error):
        save_drafts(
            conn,
            list(drafts),
            period=PERIOD,
            channel="wechat",
            conclusion_key="SINGLE_SIDED:EX-1",
            rejected=(SchemaLayerError(ValidationLayer.CONFIDENCE, "注入的中断"),),
        )
    assert list_drafts(conn, PERIOD) == [], "草稿已插入但事务未回滚 = 半截落库"


def test_confidence_is_stored_as_a_fixed_point_string():
    """不用 ``REAL``：IEEE754 双精度读回来可能是 0.8500000000000001，
    而排序键每次读出来都略有不同就没法复算 Top-1/Top-3 了。"""
    conn = _conn()
    drafts = validate_candidates({"drafts": [_draft(confidence=0.85)]}).drafts
    save_drafts(
        conn,
        drafts,
        period=PERIOD,
        channel="wechat",
        conclusion_key="SINGLE_SIDED:EX-1",
    )
    (row,) = list_drafts(conn, PERIOD)
    assert isinstance(row["confidence_text"], str)
    assert Decimal(row["confidence_text"]) == Decimal("0.85")
    assert json.loads(row["evidence_refs"]) == ["wechat_bill.csv:3"]


def test_rank_follows_the_given_order_so_top1_and_top3_are_separate():
    """落库**不重排**：排名是归因层的结论（那边才有「同分保持原顺序」的
    稳定性保证）。这里再排一次，等于「排名由谁定」变成两处都得对。"""
    conn = _conn()
    drafts = validate_candidates(
        {
            "drafts": [
                _draft(cause="导出方漏传", confidence=0.9),
                _draft(cause="跨日切分重复计入", confidence=0.4),
                _draft(cause="退费红冲未在渠道账单反映", confidence=0.6),
            ]
        }
    ).drafts
    save_drafts(
        conn,
        drafts,
        period=PERIOD,
        channel="wechat",
        conclusion_key="SINGLE_SIDED:EX-1",
    )
    rows = list_drafts(conn, PERIOD)
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert [r["cause"] for r in rows] == [
        "导出方漏传",
        "跨日切分重复计入",
        "退费红冲未在渠道账单反映",
    ]
    # 读回按 rank 排 —— 面板上「Top-1 / Top-3」两栏能分别取到
    assert list_drafts(conn, PERIOD, conclusion_key="SINGLE_SIDED:EX-1") == rows


def test_draft_table_has_no_column_that_could_hold_a_confirmed_state():
    """表里没有 ``confirmed_by`` / ``decided_at`` 这类列。
    落库这一层在**表结构上**就不提供推进状态的能力。"""
    conn = _conn()
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({ATTRIBUTION_DRAFT_TABLE})")}
    assert {"disposition"} <= columns
    assert not (columns & {"confirmed_by", "decided_at", "decided_by", "status"})
    indexes = {row[1] for row in conn.execute(f"PRAGMA index_list({ATTRIBUTION_DRAFT_TABLE})")}
    assert "ux_attribution_identity" in indexes
    assert ATTRIBUTION_REJECTION_TABLE in {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_module_offers_no_way_to_advance_disposition():
    """没有 ``update_*`` / ``confirm_*`` 之类的出口 —— 想推进状态只能另走
    ``engine.period`` 的白名单迁移，而那条只接受人工点的参数。"""
    import reconq.store.attribution_store as module

    offenders = [name for name in dir(module) if name.startswith(("confirm", "advance", "set_disposition"))]
    assert offenders == []


# ───────────────────── 六、分层码与字典层 ─────────────────────


def test_every_layer_has_its_own_registered_code():
    codes = {layer: code for layer, code in LAYER_CODES.items()}
    assert len(set(codes.values())) == len(codes) == 6
    for layer, code in codes.items():
        assert code in ERROR_CODES, f"{layer.value} 的码 {code} 未登记"
        meta = error_meta(code)
        assert meta.zh.strip() and meta.suggestion.strip()
        assert meta.zh != code


def test_model_layer_codes_are_not_channel_level():
    """分层码同样不许标渠道级：结构化输出坏掉影响的是全期归因，
    不是某一路渠道。"""
    for code in LAYER_CODES.values():
        assert not error_meta(code).channel_level


def test_cause_vocabulary_layer_rejects_free_text_causes():
    with pytest.raises(SchemaLayerError) as exc:
        validate_draft_layers(_draft(cause="大概是导出方的问题吧"), vocabulary=VOCAB)
    assert exc.value.layer is ValidationLayer.CAUSE_VOCAB
    assert exc.value.code == "ATTRIBUTION_CAUSE_UNKNOWN"
    assert "受控成因字典" in exc.value.detail


def test_cause_vocabulary_layer_is_skipped_when_no_dictionary_is_configured():
    """没配字典 = 这一层**没有判据**，记 skipped 而不是 passed。

    受控成因字典在 Task 33 落地；把这期间记成 pass，等于给未来埋一个
    「字典加上了却没人发现这层从来没跑过」的坑。
    """
    _, outcomes = validate_draft_layers(_draft(cause="随便写个成因"))
    by_layer = {o.layer: o for o in outcomes}
    outcome = by_layer[ValidationLayer.CAUSE_VOCAB]
    assert outcome.status == "skipped"
    assert outcome.code == ""
    assert "Task 33" in outcome.detail


def test_every_per_draft_layer_appears_in_the_report_for_a_clean_draft():
    """六层里 ``json`` 是**前导层**（提取发生在「有草稿」之前），故逐草稿
    的结果里只有后五层 + structure。前导层由 :func:`extract_json` 的码
    单独代表（``MODEL_OUTPUT_UNPARSEABLE``）。"""
    _obj, outcomes = validate_draft_layers(_draft(), allowed_refs=ALLOWED_REFS, vocabulary=VOCAB)
    assert {o.layer for o in outcomes} == set(ValidationLayer) - {ValidationLayer.JSON}
    assert all(o.status == "pass" for o in outcomes)
    assert all(layer_zh(o.layer) and layer_zh(o.layer) != o.layer.value for o in outcomes)


def test_validation_report_summarises_failures_by_layer():
    report = validate_candidates(
        {
            "drafts": [
                _draft(confidence=85),
                _draft(cause="别的", evidence_refs=["nope:1"]),
            ]
        },
        allowed_refs=ALLOWED_REFS,
    )
    assert report.failures_by_layer() == {"confidence": 1, "evidence_domain": 1}
    assert report.ok is False
    payload = report.to_dict()
    assert payload["raw_count"] == 2 and payload["accepted"] == 0 and payload["rejected"] == 2
    assert payload["rejected_detail"][0]["layer"] == "confidence"


def test_explanation_length_is_capped():
    with pytest.raises(SchemaLayerError) as exc:
        validate_draft_layers(_draft(explanation_zh="很长的说明" * 200))
    assert exc.value.layer is ValidationLayer.EXPLANATION
    assert exc.value.code == "ATTRIBUTION_EXPLANATION_INVALID"
    assert str(MAX_EXPLANATION_CHARS) in exc.value.detail
    # 刚好到上限仍然收
    ok = validate_draft_layers(
        _draft(explanation_zh="说" * MAX_EXPLANATION_CHARS)
    )[0]
    assert len(ok.explanation_zh) == MAX_EXPLANATION_CHARS


def test_evidence_ref_model_is_frozen_and_rejects_extra_keys():
    ref = EvidenceRef(ref="a:1")
    with pytest.raises(Exception):
        ref.ref = "b:2"  # frozen
    with pytest.raises(Exception):
        EvidenceRef(ref="a:1", note="多余字段")


def test_parse_candidate_text_runs_extraction_and_all_layers():
    text = "分析：\n```json\n" + json.dumps(
        {"drafts": [_draft(evidence_refs=["wechat_bill.csv:3"]), _draft(confidence=2.0)]},
        ensure_ascii=False,
    ) + "\n```"
    report = parse_candidate_text(text, allowed_refs=ALLOWED_REFS, vocabulary=VOCAB)
    assert len(report.drafts) == 1
    assert report.rejected[0].layer is ValidationLayer.CONFIDENCE


def test_draft_model_has_no_state_field_by_construction():
    """pydantic 模型上同样没有处置状态字段（``extra="forbid"`` 也挡住了
    模型自己多写的那个）。"""
    assert set(AttributionDraft.model_fields) == set(ATTRIBUTION_DRAFT_FIELDS)
    with pytest.raises(Exception):
        AttributionDraft(
            **_draft(), disposition="已确认"
        )
