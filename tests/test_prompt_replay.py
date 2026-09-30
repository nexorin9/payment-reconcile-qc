"""提示词与 Schema 版本化、离线回放回归（Task 34）。

五类规定断言逐条对应 steps：

1. **版本记录正确** —— 提示词 / Schema 各自的正文指纹、漏改检测、
   「提示词 × Schema」成对落进 ``model_calls`` 同一行、缓存键分维度、
   事后篡改 Schema 版本被链校验抓到。
2. **回放集可跑完** —— 12 条固定证据包全部跑完且逐字节可复算；
   模板里不许出现渠道特例（代码层面拒绝，不是注释层面自律）。
3. **Top-1/Top-3 指标计算正确** —— 命中口径、分母口径（可评条数而非
   总条数）、逐条结论与汇总数对得上。
4. **版本对比输出变化** —— 逐条 ``better/worse/changed/same`` 分类与
   「命中与否优先于 Top-1 文本变化」这条判据。
5. **更差版本告警** —— ``v2 → v2-rc1`` 报变差并给出中文告警，
   CLI 返回非零退出码；不差时不告警。

外加几条最容易被糊弄过去的性质：

* **回放走完整后处理链**（Schema → 引用越界 → 反幻觉核对 → 受控字典），
  绕过任一道闸评出来的命中率是虚的；
* **回放不调模型**（用录好的响应），重调测的是端点当天的状态；
* **期望是性质不是逐字答案**（可接受成因码集合 + 必需引用行号）；
* **回放集条数下限 12 条**（低于这个数分不出「真变好」与「这次运气好」）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal

import pytest

from reconq.align import prompts
from reconq.align.bundle import bundle_from_payload
from reconq.align.causes import cause_vocabulary
from reconq.align.prompts import (
    PROMPT_VERSION,
    PROMPTS,
    PromptTemplate,
    assert_no_channel_specialcase,
    assert_prompt_registry_consistent,
    prompt_digest,
    register_prompt,
    render,
    template_of,
)
from reconq.align.replay import (
    MIN_CASES,
    CaseOutcome,
    ReplayCase,
    compare_reports,
    compare_versions,
    load_replay_set,
    load_responses,
    render_comparison,
    render_report,
    run_replay,
)
from reconq.align.schema_version import (
    SCHEMA_FINGERPRINT,
    SCHEMA_VERSION,
    assert_schema_fingerprint_matches,
    current_pair,
    pair_of,
    record_attribution_call,
    schema_fingerprint,
)
from reconq.audit.model_calls import (
    MODEL_CALL_TABLE,
    DraftCache,
    ModelCall,
    ModelCallLedger,
    cache_key,
    init_audit_schema,
)
from reconq.cli import main as cli_main
from reconq.errors import error_meta

REPLAY_DIR = "tests/fixtures/attrib"


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_audit_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def cases():
    return load_replay_set(REPLAY_DIR)


def _responses():
    return load_responses(REPLAY_DIR)


# ══════════════════ 一、版本记录正确 ══════════════════


def test_each_registered_prompt_has_a_stable_digest_and_a_change_note():
    """每版模板都有指纹与变更说明。**没有 change_note 的版本不进注册表** ——
    回放报告里那个「命中率 +44pp」如果说不清是哪句话带来的，
    三个月后没人敢动下一版。"""
    assert_prompt_registry_consistent()
    for key, template in PROMPTS.items():
        assert template.change_note.strip(), f"{key} 缺变更说明"
        digest = prompt_digest(template)
        assert len(digest) == 16
        # 指纹只认「版本 + 成因形式 + 正文」三样
        assert digest == prompt_digest(PROMPTS[key])
    # 改一个字必变，还原后复原
    mutated = PromptTemplate(
        version="probe",
        system=PROMPTS["v2"].system + "多一句。",
        change_note="探针",
        cause_form="code",
    )
    assert prompt_digest(mutated) != prompt_digest(PROMPTS["v2"])


def test_unknown_prompt_version_is_rejected_not_silently_defaulted():
    """未登记版本显式报错，**不回落默认版** —— 回落会让 ``--compare v9,v2``
    静默变成 ``v2,v2``，比出来的「无变化」会被读成「v9 效果一样」。"""
    with pytest.raises(Exception) as exc:
        template_of("v9")
    assert exc.value.code == "PROMPT_VERSION_UNKNOWN"
    assert "v9" in exc.value.detail
    assert error_meta("PROMPT_VERSION_UNKNOWN").suggestion


def test_tampered_prompt_body_is_caught_before_anything_else_runs(monkeypatch):
    """改了模板正文却没 bump 版本 → 指纹对不上，当场报出来。

    这是「改了提示词却继续吃旧答案」的唯一一道自动防线：版本串进缓存键，
    不 bump 就等于没改，而审计链上也看不出任何变化。
    """
    original = PROMPTS[PROMPT_VERSION]
    PROMPTS[PROMPT_VERSION] = PromptTemplate(
        version=PROMPT_VERSION,
        system=original.system + "\n7. 顺手加的一条。",
        change_note="偷改",
        cause_form="code",
    )
    try:
        with pytest.raises(Exception) as exc:
            assert_prompt_registry_consistent()
        assert exc.value.code == "PROMPT_DIGEST_MISMATCH"
        assert PROMPT_VERSION in exc.value.detail
        # current_pair 也走同一道检查（它是一次自检入口）
        with pytest.raises(Exception):
            current_pair()
    finally:
        PROMPTS[PROMPT_VERSION] = original


def test_schema_fingerprint_detects_a_schema_change_without_a_version_bump(monkeypatch):
    """改了草稿 Schema 的 required 却没 bump SCHEMA_VERSION → 报出来。

    后果与提示词漏改同源：Schema 版本进缓存键，不 bump 就是没改，
    而网关对同一份响应的判定可能已经完全相反。
    """
    from reconq.align import attribution

    original = attribution.ATTRIBUTION_SCHEMA
    attribution.ATTRIBUTION_SCHEMA = {
        **original,
        "required": ["cause", "confidence", "evidence_refs", "explanation_zh", "note"],
    }
    try:
        assert schema_fingerprint() != SCHEMA_FINGERPRINT
        with pytest.raises(Exception) as exc:
            assert_schema_fingerprint_matches()
        assert exc.value.code == "SCHEMA_FINGERPRINT_MISMATCH"
    finally:
        attribution.ATTRIBUTION_SCHEMA = original
    assert_schema_fingerprint_matches()


def test_version_pair_carries_both_digests_and_keeps_the_dictionary_out_of_the_cache_tag():
    """「提示词 × Schema」成对，且**字典不进缓存键**。

    字典进键的后果：补一条成因就把全部缓存作废，这一期的「模型成本与
    命中」报表会凭空多出几十次调用，而账单一模一样。字典指纹仍然跟着
    版本对走 —— 三个月后要能回答「当时字典长什么样」。
    """
    pair = current_pair()
    assert pair.prompt_version == PROMPT_VERSION
    assert pair.schema_version == SCHEMA_VERSION
    assert pair.prompt_digest == prompt_digest(template_of(PROMPT_VERSION))
    assert pair.schema_digest == SCHEMA_FINGERPRINT
    assert pair.cause_dict_digest, "字典指纹要跟着版本对走"
    assert pair.cause_dict_digest not in pair.cache_tag
    assert pair.prompt_digest in pair.cache_tag and pair.schema_digest in pair.cache_tag
    # 标签是「指纹进串」：改了正文没 bump 版本时，同一版本号前后两条审计
    # 记录的版本串会不同 —— 这正是要能从审计链上看出来的那件事。
    # 用分隔符两侧的字段数判，而不是数分隔符本身：Schema 版本串
    # （attrib-schema/v1）里本来就带斜杠。
    assert pair.cache_tag == (
        f"{PROMPT_VERSION}@{pair.prompt_digest}/"
        f"{SCHEMA_VERSION}@{pair.schema_digest}"
    )
    assert pair.cache_tag.count("@") == 2


def test_pair_of_never_silently_switches_the_schema_version():
    """对比旧提示词时 Schema 仍取当前：对比的变量**只有提示词一个**。

    同时切 Schema 比出来的「提示词变好了」里混着 Schema 的贡献。
    """
    pair = pair_of("v1")
    assert pair.prompt_version == "v1"
    assert pair.schema_version == SCHEMA_VERSION
    with pytest.raises(Exception) as exc:
        pair_of("v1", schema_version="attrib-schema/v99")
    assert exc.value.code == "SCHEMA_VERSION_UNKNOWN"


def test_prompt_and_schema_versions_land_on_the_same_audit_row(conn):
    """成对落进**同一条**记录 —— 分成两处记会出现配不上对的情况，
    而配不上对的版本对在排查时等于没有。"""
    from reconq.align.schema_version import SCHEMA_VERSION as SV

    class _Call:
        model = "reconq-attribution-1"
        elapsed_ms = 12.5
        prompt_tokens = 640
        completion_tokens = 72
        attempts = 1
        retried = False
        ok = True
        error_code = ""
        request_digest = "req-fp"
        bundle = {
            "conclusion_key": "wechat:2026-W36:EX-9",
            "channel": "wechat",
            "char_count": 1200,
            "line_count": 24,
        }

    record = record_attribution_call(
        conn, _Call(), period="2026-W36", request_fingerprint="evidence-fp"
    )
    assert record.schema_version == SV
    assert record.prompt_version == current_pair().cache_tag
    row = conn.execute(
        f"SELECT * FROM {MODEL_CALL_TABLE} WHERE call_id = ?", (record.call_id,)
    ).fetchone()
    assert row["schema_version"] == SV
    assert row["prompt_version"] == current_pair().cache_tag
    # 审计表不因多这一列而长出可装正文的列
    columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({MODEL_CALL_TABLE})")}
    assert columns & {"prompt", "messages", "response", "content", "detail", "text"} == set()


def test_changing_the_schema_version_after_the_fact_breaks_the_chain(conn):
    """事后改审计记录里的 Schema 版本会被链校验抓到。

    不抓的后果：有人为了让某条记录「看起来对」把它改成自己那版 Schema，
    链上却毫无异样。
    """

    class _Call:
        model = "m"
        elapsed_ms = 1.0
        prompt_tokens = 10
        completion_tokens = 5
        attempts = 1
        retried = False
        ok = True
        error_code = ""
        request_digest = "d"
        bundle = {"conclusion_key": "k", "channel": "wechat", "char_count": 1, "line_count": 1}

    record = record_attribution_call(conn, _Call(), period="2026-W36")
    assert ModelCallLedger(conn).verify_chain("2026-W36").ok
    conn.execute(
        f"UPDATE {MODEL_CALL_TABLE} SET schema_version = 'attrib-schema/v99' "
        "WHERE call_id = ?",
        (record.call_id,),
    )
    conn.commit()
    verdict = ModelCallLedger(conn).verify_chain("2026-W36")
    assert not verdict.ok
    assert verdict.broken_seq == 1


def test_cache_key_separates_prompt_version_schema_version_and_model(conn):
    """三个维度任一变化都必须换键，且留空时与加这一维之前逐字节相同
    （老调用方 / 迁移前的缓存不能整体失效）。"""
    base = cache_key("fp", "v2", "m")
    assert base != cache_key("fp", "v1", "m"), "换提示词版本必须换键"
    assert base != cache_key("fp", "v2", "other"), "换模型必须换键"
    assert cache_key("fp", "v2", "m", "s1") != cache_key("fp", "v2", "m", "s2"), (
        "换 Schema 版本必须换键 —— 改了草稿 Schema 之后旧缓存的 JSON "
        "未必还能过新 Schema 的校验"
    )
    assert cache_key("fp", "v2", "m", "") == base

    cache = DraftCache(conn)
    cache.put("fp", "v2", "m", "[]", schema_version="attrib-schema/v1")
    assert cache.lookup("fp", "v2", "m", "attrib-schema/v1") is not None
    assert cache.lookup("fp", "v2", "m", "attrib-schema/v2") is None
    assert cache.lookup("fp", "v2", "m") is None


# ══════════════════ 二、回放集可跑完 ══════════════════


def test_prompt_templates_reject_channel_special_cases():
    """模板里不许出现渠道特例 —— **代码层面拒绝**，不是注释层面自律。

    渠道取件口径属于证据包（channel_meta.bill_type）。写进模板等于让
    「换渠道 / 改渠道参数」都变成一次提示词改版，而回放集的期望是按
    旧措辞录的。
    """
    for token in ("微信账单要带 bill_type=ALL", "支付宝走 gateway.do", "银联走文件导出"):
        with pytest.raises(Exception) as exc:
            assert_no_channel_specialcase(token)
        assert exc.value.code == "PROMPT_CHANNEL_SPECIALCASE"
        assert error_meta("PROMPT_CHANNEL_SPECIALCASE").suggestion
    # 已登记的三版模板全部通过这道检查
    for key, template in PROMPTS.items():
        assert_no_channel_specialcase(template.system, where=f"提示词 {key}")


def test_render_injects_bundle_and_dictionary_and_keeps_system_turn_stable():
    """模板只允许注入**证据包**与**受控字典**两样；system 段跨差异不变
    （逐条内容混进去会让 provider 侧的 prompt 缓存整段失效）。"""
    vocabulary = [f"{code}" for code in cause_vocabulary()]
    plain = render("证据正文", version="v2", vocabulary=vocabulary)
    with_notes = render(
        "证据正文", version="v2", instructions="候选已裁剪", vocabulary=vocabulary
    )
    assert plain[0] == with_notes[0], "system 段必须跨差异不变"
    assert "候选已裁剪" in with_notes[1]["content"]
    assert "证据正文" in plain[1]["content"]
    # 受控字典注入到 system 段，且是渲染时注入（字典不在模板正文里）
    assert "channel_late_posting" in plain[0]["content"]
    assert "可选成因" in plain[0]["content"]
    # v1 不注入字典（那是 v2 才加的）
    v1 = render("证据正文", version="v1", vocabulary=vocabulary)
    assert "channel_late_posting" not in v1[0]["content"]
    # 渲染两次逐字节相同：回放集里录的响应才有意义
    assert render("证据正文", version="v2", vocabulary=vocabulary) == plain


def test_registering_a_prompt_with_a_channel_special_case_is_refused():
    """新增一版也要过渠道特例检查 —— 否则 register 成了绕开检查的后门。"""
    with pytest.raises(Exception) as exc:
        register_prompt(
            PromptTemplate(
                version="probe-bad",
                system="判因时参考微信的 bill_type=ALL。",
                change_note="探针",
                cause_form="code",
            )
        )
    assert exc.value.code == "PROMPT_CHANNEL_SPECIALCASE"
    assert "probe-bad" not in PROMPTS


def test_replay_set_has_twelve_fixed_cases_covering_the_main_cause_codes(cases):
    """回放集 ≥12 条，覆盖字典主要成因码，且含一条 UNKNOWN。"""
    assert len(cases) >= MIN_CASES == 12
    covered: set[str] = set()
    for case in cases:
        covered.update(case.cause_codes)
        assert case.cause_codes, f"{case.case_id} 没写可接受成因码"
        assert case.bundle.payload["discrepancy"]["kind"] == case.kind
    # 字典里的主要成因码都被回放集覆盖到
    for code in (
        "his_missing_order",
        "channel_late_posting",
        "split_payment_unallocated",
        "merged_payment_allocation",
        "refund_offset_in_his",
        "period_boundary_landing",
        "cross_period_duplicate",
        "contract_config_error",
        "undetermined",
    ):
        assert code in covered, f"回放集没覆盖成因 {code}"
    # 有一条专门考「判不出来时必须说 undetermined」
    unknown = [c for c in cases if c.cause_codes == ("undetermined",)]
    assert unknown, "回放集必须有一条期望 undetermined 的用例（信息耗尽）"


def test_bundles_are_byte_identical_across_loads(cases):
    """证据包**逐字节固定**且可复算：重建依赖当期账单行与候选打分配置，
    两者都会漂移，漂移会让「提示词变好还是变坏」这个变量里混进
    「证据包也变了」那个说不清的量。"""
    again = load_replay_set(REPLAY_DIR)
    assert [c.bundle.digest for c in cases] == [c.bundle.digest for c in again]
    for case in cases:
        reloaded = bundle_from_payload(case.bundle.payload)
        assert reloaded.digest == case.bundle.digest
        assert reloaded.text == case.bundle.text, "重渲染必须逐字节相同"


def test_replay_set_rejects_a_missing_or_too_small_set(tmp_path):
    """空回放集与条数不足都要显式报错，不能跑出一个「命中率 0%」让人
    误以为模型不行。"""
    from reconq.align.replay import load_replay_set as load

    with pytest.raises(Exception) as exc:
        load(str(tmp_path / "nope"))
    assert exc.value.code == "REPLAY_SET_EMPTY"

    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "only.json").write_text(
        json.dumps({"case_id": "x", "expect": {"cause_codes": ["undetermined"]}}), encoding="utf-8"
    )
    with pytest.raises(Exception) as exc:
        load(str(tmp_path))
    assert exc.value.code == "REPLAY_SET_TOO_SMALL" or exc.value.code == "REPLAY_CASE_INVALID"


def test_replay_runs_all_cases_for_each_registered_version(cases):
    """12 条全部跑完、每条都有可评产出，且回放**不调模型**。"""
    responses = _responses()
    for version in ("v1", "v2", "v2-rc1"):
        report = run_replay(version, directory=REPLAY_DIR, responses=responses)
        assert report.total == len(cases) == 12
        assert report.usable > 0
        assert {o.case_id for o in report.outcomes} == {c.case_id for c in cases}
        assert report.pair.prompt_version == version
        assert report.pair.schema_version == SCHEMA_VERSION
        assert report.model, "报告必须带模型名：换个模型跑同一份响应，指标含义就变了"
        # 同版本跑两次逐字段相同
        again = run_replay(version, directory=REPLAY_DIR, responses=responses)
        assert [o.to_dict() for o in report.outcomes] == [o.to_dict() for o in again.outcomes]


def test_missing_recording_for_a_version_is_refused(cases):
    """缺一版响应 = 缺一条基线，而「基线缺失」在报告上看起来和
    「这一版没提上来」一模一样。"""
    responses = _responses()
    trimmed = {**responses, "responses": {}}
    with pytest.raises(Exception) as exc:
        run_replay("v2", directory=REPLAY_DIR, responses=trimmed)
    assert exc.value.code == "REPLAY_RESPONSE_MISSING"


# ══════════════════ 三、Top-1 / Top-3 指标计算正确 ══════════════════


def test_top1_and_top3_hits_match_the_per_case_outcomes(cases):
    """汇总数 = 逐条数。这条断言的作用是防「报告层另算一遍」——
    面板上打的总命中率与逐条清单对不上时，没人知道该信哪个。"""
    for version in ("v1", "v2", "v2-rc1"):
        report = run_replay(version, directory=REPLAY_DIR)
        usable = [o for o in report.outcomes if o.usable]
        assert report.usable == len(usable)
        assert report.top1_hits == sum(1 for o in usable if o.top1_hit)
        assert report.top3_hits == sum(1 for o in usable if o.top3_hit)
        assert report.ref_hits == sum(1 for o in usable if o.ref_hit)
        assert report.top1_rate == (
            Decimal(report.top1_hits) / Decimal(report.usable) if report.usable else Decimal("0")
        )
        # Top-1 命中必然蕴含 Top-3 命中；Top-1 就是前三条里的第一条
        for outcome in usable:
            if outcome.top1_hit:
                assert outcome.top3_hit
            assert outcome.top1_code, f"{outcome.case_id} 可评却无 Top-1"
            assert outcome.top3_codes[:1] == (outcome.top1_code,)
            assert outcome.top1_code in outcome.top3_codes


def test_fully_rejected_case_is_excluded_from_the_rate_denominator(cases):
    """分母是**可评条数**而不是总条数。

    否则「让所有草稿都过不了 Schema」反而会让命中率好看一点 ——
    那是一条「什么都没产出」的版本，不该与「说得准」同分。
    """
    report = run_replay("v1", directory=REPLAY_DIR)
    unusable = [o for o in report.outcomes if not o.usable]
    assert unusable, "v1 下应有整条没有可用成因的用例（模型给了中文短语，字典筛不掉）"
    assert report.usable < report.total
    assert report.top1_hits <= report.usable
    for outcome in unusable:
        # 不可评必须能说出**是哪道闸造成的**，不能只有一个「未通过」。
        # 两条路径要分开：
        #   answered_but_unusable —— 草稿过了引用与核对两道闸，是「措辞
        #     没对齐受控字典」；模型确实回答了，只是答案落不了地。
        #   全无 kept —— 前两道闸就把它拦下了，是「模型在说胡话」。
        if outcome.answered_but_unusable:
            assert outcome.kept, "答了但用不了：草稿过了前两道闸"
            assert outcome.rejected_cause_unknown > 0
        else:
            assert not outcome.kept
        assert (
            outcome.rejected_ref_invalid
            + outcome.rejected_cause_unknown
            + outcome.rejected_evidence_mismatch
            + outcome.rejected_schema
        ) > 0
    # 两种不可评在 v1 下都真实发生（否则这条断言的分支有一边永远走不到）
    kinds = {o.answered_but_unusable for o in unusable}
    assert kinds == {True, False}, f"两类不可评应都出现，实际 {kinds}"


def test_replay_walks_the_full_post_processing_chain(cases):
    """回放必须走完整四道闸，绕过任一道评出来的命中率都是虚的。

    v1 下引用了不存在行的那条（08）要落到 ``rejected_ref_invalid``，
    而**不是**「引用越界后仍算命中」—— 线上会被拒，回放里若算对，
    这套回放就抓不到它本该抓的 bug。
    """
    report = run_replay("v1", directory=REPLAY_DIR)
    out_of_bundle = report.outcome_of("08-self-service-mismatch")
    assert out_of_bundle.rejected_ref_invalid == 1, (
        "引用 self_service_bill.csv:99（不在证据包内）必须被引用越界筛查拒掉"
    )
    assert not out_of_bundle.kept
    # 中文短语那条要落到受控字典筛（字典外成因）
    v1_dict = report.outcome_of("01-channel-only-his-missing")
    assert v1_dict.rejected_cause_unknown == 1
    # v2 全条走通
    v2 = run_replay("v2", directory=REPLAY_DIR)
    assert all(o.rejected_ref_invalid == 0 for o in v2.outcomes)
    assert all(o.rejected_cause_unknown == 0 for o in v2.outcomes)
    assert all(o.rejected_schema == 0 for o in v2.outcomes)


def test_schema_violating_draft_is_counted_as_rejected_not_dropped(cases):
    """结构不合约定的草稿计入 ``rejected_schema``，不静默消失。"""
    responses = _responses()
    broken = json.loads(json.dumps(responses))
    broken["responses"]["01-channel-only-his-missing"]["v2"] = {
        "drafts": [
            {
                "cause": "his_missing_order",
                "confidence": 85,  # 看着像百分制
                "evidence_refs": ["wechat_bill.csv:3"],
                "explanation_zh": "写错了置信度。",
            }
        ]
    }
    report = run_replay("v2", directory=REPLAY_DIR, responses=broken)
    outcome = report.outcome_of("01-channel-only-his-missing")
    assert outcome.rejected_schema == 1
    assert not outcome.kept
    assert not outcome.usable
    # 不修不猜：85 不被悄悄除以 100
    assert report.usable < report.total


def test_ref_hit_requires_the_mandatory_rows_to_be_cited(cases):
    """``ref_hit`` 要求 Top-1 保留草稿**引用了必须引用的行** ——
    不引用差异行却给出成因，等于凭印象归因。"""
    report = run_replay("v2", directory=REPLAY_DIR)
    for case in cases:
        outcome = report.outcome_of(case.case_id)
        if outcome.ref_hit:
            assert set(case.required_refs) <= set(outcome.cited_refs)
            assert len(outcome.cited_refs) >= case.min_refs
        else:
            # 没达标要说得出差在哪，而不是含糊地「未达标」
            assert not set(case.required_refs) <= set(outcome.cited_refs) or (
                len(outcome.cited_refs) < case.min_refs
            )


def test_unknown_case_must_say_undetermined_under_v2_but_not_under_rc1(cases):
    """这条是本任务最有产品价值的一条断言：信息耗尽时，
    「我判不出来」比「我判错了」贵得多 —— 前者让人去查证，后者让人签字。"""
    case_id = "10-unknown-equal-amounts"
    v2 = run_replay("v2", directory=REPLAY_DIR).outcome_of(case_id)
    assert v2.top1_code == "undetermined"
    assert v2.top1_hit
    rc1 = run_replay("v2-rc1", directory=REPLAY_DIR).outcome_of(case_id)
    assert rc1.top1_code == "split_payment_unallocated"
    assert not rc1.top1_hit, "少了一句「判不出来就说 undetermined」，这里就该硬猜"


# ══════════════════ 四、版本对比输出变化 ══════════════════


def test_comparison_classifies_each_case_and_reports_metric_deltas():
    """逐条分类 + 指标增减。v1→v2 是「由错变对」为主。"""
    comparison = compare_versions("v1", "v2", directory=REPLAY_DIR)
    assert comparison.baseline.prompt_version == "v1"
    assert comparison.candidate.prompt_version == "v2"
    assert len(comparison.changes) == 12
    assert comparison.candidate.top1_hits > comparison.baseline.top1_hits
    assert comparison.candidate.top1_rate > comparison.baseline.top1_rate
    for change in comparison.changes:
        assert change.verdict in ("better", "worse", "changed", "same")
    assert len(comparison.better_cases) >= 5


def test_hit_verdict_takes_priority_over_top1_text_change():
    """命中与否**优先于** Top-1 文本变化。

    Top-1 从 A 变成 B 而两者都在期望集合里，这是「换了个同样对的说法」，
    不是变好也不是变坏 —— 把它算成变化，回放报告里会凭空多出十几条
    「结论变了」，真正该盯的那一条反而被淹了。
    """
    change = next(
        c for c in compare_versions("v1", "v2", directory=REPLAY_DIR).changes
        if c.case_id == "12-medical-ins-settlement-only"
    )
    assert change.baseline_top1 != change.candidate_top1
    assert change.baseline_hit and change.candidate_hit
    assert change.verdict == "changed"

    # 全库不存在「两边都没命中但 Top-1 变了」被算成 better/worse 的情况
    for c in compare_versions("v1", "v2", directory=REPLAY_DIR).changes:
        if c.verdict == "changed":
            assert c.baseline_hit == c.candidate_hit


def test_same_top1_and_same_hit_is_reported_as_same():
    comparison = compare_versions("v1", "v2", directory=REPLAY_DIR)
    unchanged = [c for c in comparison.changes if c.verdict == "same"]
    assert unchanged, "Top-1 与命中都没变的用例应报 same，而不是全报「变化」"
    for change in unchanged:
        assert change.baseline_top1 == change.candidate_top1
        assert change.baseline_hit == change.candidate_hit


def test_comparison_render_shows_metrics_changes_and_every_case():
    text = render_comparison(compare_versions("v1", "v2", directory=REPLAY_DIR))
    assert "Top-1 命中率" in text and "Top-3 命中率" in text
    assert "v1" in text and "v2" in text
    assert "pp" in text  # 指标增减带单位
    for case in load_replay_set(REPLAY_DIR):
        assert case.case_id in text, f"逐条清单漏了 {case.case_id}"


def test_report_render_shows_rejection_reasons_not_just_a_rate():
    """报告要能回答「模型给了几条、收下几条、丢了几条、为什么丢」——
    只给一个总命中率的话，改提示词的人学不到东西。"""
    text = render_report(run_replay("v1", directory=REPLAY_DIR))
    # v1 的响应里真的被拒过的三类，逐类都要在报告上看得见
    report = run_replay("v1", directory=REPLAY_DIR)
    shown = {
        "引用越界": sum(o.rejected_ref_invalid for o in report.outcomes),
        "核对不过": sum(o.rejected_evidence_mismatch for o in report.outcomes),
        "字典外": sum(o.rejected_cause_unknown for o in report.outcomes),
    }
    for label, count in shown.items():
        assert count > 0, f"{label} 这一类在 v1 下没有发生，断言要改"
        assert label in text
    assert "反幻觉核对拒" in text
    assert "可评" in text and "被拒" in text


# ══════════════════ 五、更差版本告警 ══════════════════


def test_worse_version_raises_the_warning_with_the_offending_cases():
    """新版本「由对变错」的条数多于变好 → 告警，点名是哪几条。"""
    comparison = compare_versions("v2", "v2-rc1", directory=REPLAY_DIR)
    assert comparison.regressed
    assert len(comparison.worse_cases) == 2
    assert {c.case_id for c in comparison.worse_cases} == {
        "10-unknown-equal-amounts",
        "11-missing-his-order-no",
    }
    warning = comparison.warning
    assert "v2-rc1" in warning and "v2" in warning
    assert "不要静默采用更差版本" in warning
    assert "10-unknown-equal-amounts" in warning
    assert error_meta  # 错误码元数据在 errors.py 里可查


def test_better_version_does_not_warn():
    comparison = compare_versions("v1", "v2", directory=REPLAY_DIR)
    assert not comparison.regressed
    assert comparison.warning == ""
    assert "变差" not in render_comparison(comparison).splitlines()[-1] or "未出现" in (
        render_comparison(comparison)
    )


def test_regression_is_decided_per_case_not_by_the_average():
    """判据是「变差条数 > 变好条数」而不是「命中率下降」。

    命中率是 12 条上的一个比例，改一条就从 11/12 掉到 10/12 ——
    逐条看得见，总分看不见。反过来，「命中率没动但 Top-1 换了同样对的
    说法」也不该被算成变差（那些是 ``changed`` 不是 ``worse``）。
    """
    base = run_replay("v2", directory=REPLAY_DIR)
    # 手工造一份「只有一条变差、Top-1 命中率必然下降」的候选
    worse = CaseOutcome(
        case_id=base.outcomes[0].case_id,
        name_zh=base.outcomes[0].name_zh,
        channel=base.outcomes[0].channel,
        kind=base.outcomes[0].kind,
        prompt_version="probe",
        kept=base.outcomes[0].kept,
        top1_code="contract_config_error",
        top3_codes=("contract_config_error",),
        top1_hit=False,
        top3_hit=False,
        cited_refs=base.outcomes[0].cited_refs,
    )
    from dataclasses import replace

    candidate = replace(
        base, prompt_version="probe", outcomes=(worse, *base.outcomes[1:])
    )
    comparison = compare_reports(base, candidate)
    assert comparison.regressed
    assert len(comparison.worse_cases) == 1
    assert len(comparison.better_cases) == 0


def test_cli_compare_returns_nonzero_on_regression_and_zero_on_improvement(capsys):
    """更差时非零退出码：让 CI / 排障脚本把「别上这版」变成一道门禁，
    而不必靠人读终端最后那行告警。"""
    assert cli_main(["attrib-replay", "--set", REPLAY_DIR, "--compare", "v2,v2-rc1"]) == 1
    out = capsys.readouterr().out
    assert "不要静默采用更差版本" in out
    assert cli_main(["attrib-replay", "--set", REPLAY_DIR, "--compare", "v1,v2"]) == 0
    assert "未出现" in capsys.readouterr().out


def test_cli_replay_and_list_versions(capsys):
    assert cli_main(["attrib-replay", "--set", REPLAY_DIR, "--version", "v2"]) == 0
    out = capsys.readouterr().out
    assert "Top-1 命中" in out and "12" in out

    assert cli_main(["attrib-replay", "--list-versions", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["current"] == PROMPT_VERSION
    assert {row["version"] for row in payload["versions"]} == set(PROMPTS)
    for row in payload["versions"]:
        assert row["change_note"], "版本清单里每版都要带变更说明"
        assert row["prompt_digest"]


def test_cli_compare_requires_two_versions(capsys):
    assert cli_main(["attrib-replay", "--set", REPLAY_DIR, "--compare", "v2"]) == 2
    err = capsys.readouterr().err
    assert "REPLAY_COMPARE_ARGS_INVALID" in err
    assert "只给一版比不了" in err
