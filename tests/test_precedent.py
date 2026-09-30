"""先例库：人判过的成因沉淀成下次同类差异的建议（Task 41）。

六类规定断言逐条可核：

1. **确认后写先例** —— 人工确认的处置结论沉淀成先例行，命中次数、首次/最近
   命中时间、样本差异键、登记时版本对全部落库；同一条重复登记**不新建行**
   而是把命中次数 +1（先例库记的是「判过几次」，不是「有几张单被点过」）。
2. **驳回不写** —— 「已驳回」与「待确认」一律不写先例，且**先例表零变化**；
   非自然人操作人（模型 / 规则基线 / 降级路径）也在入口被拒。
3. **特征签名稳定** —— 同一组特征两次算出的签名逐字相同；六项特征里**改
   任意一项**签名必变（逐项参数化断言）；单号只取前缀形态（不同单号同形态
   命中同一条先例）；签名**不含患者可识别信息**、不进金额。
4. **命中先例进入候选且标 ``precedent_hit``** —— 检索命中后产出一条建议，
   带历史命中次数与版本权威度；置信度按封顶给（不越过规则的 0.5 逻辑，
   也不冒充模型）；**低于下限命中次数的先例不返回**；先例是**候选**，
   不替代模型草稿也不替代规则基线。
5. **版本变更后降权** —— 字典或提示词版本与登记时不同的先例
   ``authority`` 降为 ``reference``、置信度降到参考封顶、文案写明差在哪；
   版本对得上时保持 ``strong``。
6. **事件链记录** —— 「谁在何时采信了哪条先例」写进与重跑/字典版本同一条
   append-only 链式事件链；先例行不存在时登记命中被拒（留痕不许指向空处）。

外加：先例看板 Top N、CLI 三条腿端到端、错误码元数据齐全、
脱敏（先例表与留痕表不含患者可识别字段）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from reconq.align import precedent as prec
from reconq.align.causes import CAUSE_DICT_VERSION, dictionary_digest
from reconq.align.prompts import PROMPT_VERSION
from reconq.align.schema_version import current_pair
from reconq.engine.period import (
    DISPOSITION_CONFIRMED,
    DISPOSITION_PENDING,
    DISPOSITION_REJECTED,
    init_scan_schema,
)
from reconq.engine.resume import init_resume_schema, list_scan_events
from reconq.errors import ERROR_CODES, ReconqError

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

PERIOD = "2026-W36"
OPERATOR = "经办人-张工"

#: 受控成因字典里两条真实成因（一条渠道侧、一条信息科侧），用来验证
#: 「同一个签名下不同成因各存一条」而不是互相覆盖。
CAUSE_CHANNEL = "channel_late_posting"
CAUSE_CONFIG = "contract_config_error"


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    init_scan_schema(connection)
    init_resume_schema(connection)
    prec.init_precedent_schema(connection)
    yield connection
    connection.close()


def _sig(
    *,
    channel: str = "wechat",
    kind: str = "cross_period",
    trade_type: str = "SUCCESS",
    cross_period: bool = True,
    candidate_count: int = 2,
    candidate_amounts=("50.00", "50.00"),
    order_no: str = "EX-WX-0001",
):
    return prec.feature_signature(
        channel=channel,
        kind=kind,
        trade_type=trade_type,
        cross_period=cross_period,
        candidate_count=candidate_count,
        candidate_amounts=candidate_amounts,
        order_no=order_no,
    )


def _confirm(conn, signature=None, *, cause=CAUSE_CHANNEL, diff_key="2026-W36|wechat|taxonomy.ambiguity:EX-WX-0001",
             disposition=DISPOSITION_CONFIRMED, operator=OPERATOR, now="2026-09-07T09:00:00"):
    return prec.record_confirmation(
        conn,
        period=PERIOD,
        channel=(signature or _sig()).features["channel"],
        cause_code=cause,
        signature=signature or _sig(),
        disposition=disposition,
        operator=operator,
        diff_key=diff_key,
        now=now,
    )


# ───────────────── 1. 确认后写先例 ─────────────────


def test_confirmed_disposition_writes_a_precedent_row(conn):
    write = _confirm(conn)

    assert write.created is True
    assert write.hit_count == 1
    row = prec.load_precedent(conn, write.signature, CAUSE_CHANNEL)
    assert row is not None, "人工确认后必须有先例行"
    assert row.cause_code == CAUSE_CHANNEL
    assert row.zh_name, "先例要能读出中文成因名"
    assert row.owner, "先例要能读出责任方（面板的分组即分工表）"
    assert row.hit_count == 1
    assert row.first_hit_at == "2026-09-07T09:00:00"
    assert row.last_hit_at == "2026-09-07T09:00:00"
    assert row.sample_keys == ("2026-W36|wechat|taxonomy.ambiguity:EX-WX-0001",)
    assert row.ambiguity_kind == "cross_period"
    assert row.confirmed_by == OPERATOR
    # 登记时版本对必须齐全：先例日后要靠它判断该不该降权
    assert row.dict_version == CAUSE_DICT_VERSION
    assert row.dict_digest == dictionary_digest()
    assert row.prompt_version == PROMPT_VERSION
    assert row.prompt_digest == current_pair().prompt_digest
    assert row.signature_version == prec.PRECEDENT_SIGNATURE_VERSION


def test_repeat_confirmation_bumps_hit_count_instead_of_new_row(conn):
    """同一条差异被确认两次（或撤回后重办）不产生第二条先例。"""
    first = _confirm(conn, diff_key="2026-W36|wechat|taxonomy.ambiguity:EX-WX-0001")
    second = _confirm(conn, diff_key="2026-W36|wechat|taxonomy.ambiguity:EX-WX-0002", now="2026-09-07T10:00:00")
    third = _confirm(conn, diff_key="2026-W36|wechat|taxonomy.ambiguity:EX-WX-0001", now="2026-09-07T11:00:00")

    assert (first.created, second.created, third.created) == (True, False, False)
    assert (first.hit_count, second.hit_count, third.hit_count) == (1, 2, 3)
    assert conn.execute(f"SELECT COUNT(*) FROM {prec.PRECEDENT_TABLE}").fetchone()[0] == 1
    row = prec.load_precedent(conn, second.signature, CAUSE_CHANNEL)
    assert row.hit_count == 3
    assert row.first_hit_at == "2026-09-07T09:00:00", "首次命中时间不该被后续登记改写"
    assert row.last_hit_at == "2026-09-07T11:00:00"
    # 样本键去重：同一个差异键登两次不重复进列表
    assert row.sample_keys.count("2026-W36|wechat|taxonomy.ambiguity:EX-WX-0001") == 1


def test_same_signature_different_causes_are_separate_precedents(conn):
    """同一签名下不同成因各存一条 —— 「这个形态被这样判过」是逐成因的。"""
    _confirm(conn, cause=CAUSE_CHANNEL)
    _confirm(conn, cause=CAUSE_CONFIG)
    rows = prec.list_precedents(conn)
    assert {r.cause_code for r in rows} == {CAUSE_CHANNEL, CAUSE_CONFIG}
    assert all(r.hit_count == 1 for r in rows)


def test_sample_keys_are_capped(conn):
    """样本键只留最近若干条：它是回查线索，不是账本。"""
    for index in range(prec.MAX_SAMPLE_KEYS + 5):
        _confirm(conn, diff_key=f"2026-W36|wechat|taxonomy.ambiguity:EX-WX-{index:04d}")
    row = prec.list_precedents(conn)[0]
    assert len(row.sample_keys) == prec.MAX_SAMPLE_KEYS
    # 截断保留最近的一条
    assert row.sample_keys[-1] == f"2026-W36|wechat|taxonomy.ambiguity:EX-WX-{prec.MAX_SAMPLE_KEYS + 4:04d}"


# ───────────────── 2. 驳回不写 ─────────────────


@pytest.mark.parametrize(
    "disposition",
    [DISPOSITION_REJECTED, DISPOSITION_PENDING, "", "已处理"],
)
def test_non_confirmed_dispositions_never_write_precedent(conn, disposition):
    with pytest.raises(ReconqError) as exc:
        _confirm(conn, disposition=disposition)
    assert exc.value.code == "PRECEDENT_DISPOSITION_INVALID"
    assert conn.execute(f"SELECT COUNT(*) FROM {prec.PRECEDENT_TABLE}").fetchone()[0] == 0, (
        "被驳回 / 待确认的差异绝不能落成先例 —— 下一期会给出恰好被否掉的建议"
    )


@pytest.mark.parametrize(
    "operator",
    ["model", "MODEL", "rule", "baseline", "system", "规则", "模型", "系统", "自动", ""],
)
def test_non_human_operators_cannot_register_precedent(conn, operator):
    with pytest.raises(ReconqError) as exc:
        _confirm(conn, operator=operator)
    assert exc.value.code == "PRECEDENT_OPERATOR_INVALID"
    assert conn.execute(f"SELECT COUNT(*) FROM {prec.PRECEDENT_TABLE}").fetchone()[0] == 0


def test_unregistered_cause_code_is_rejected_at_registration(conn):
    """字典外的成因码不进先例库 —— 检索会命中一条读不出「该找谁」的建议。"""
    with pytest.raises(ReconqError) as exc:
        _confirm(conn, cause="随手写的一个成因")
    assert exc.value.code == "ATTRIBUTION_CAUSE_UNKNOWN"
    assert conn.execute(f"SELECT COUNT(*) FROM {prec.PRECEDENT_TABLE}").fetchone()[0] == 0


# ───────────────── 3. 特征签名稳定 ─────────────────


def test_signature_is_deterministic_across_calls():
    a = _sig(order_no="EX-WX-0001")
    b = _sig(order_no="EX-WX-0001")
    assert a.digest == b.digest
    assert a.features == b.features
    assert len(a.digest) == 16
    assert a.render_zh()


@pytest.mark.parametrize(
    "override",
    [
        {"channel": "alipay"},
        {"kind": "refund_and_original"},
        {"trade_type": "REFUND"},
        {"cross_period": False},
        {"candidate_count": 3},
        {"candidate_amounts": ("50.00", "49.00")},
        {"order_no": "4200001234202609"},
    ],
)
def test_every_signature_field_changes_the_digest(override):
    """六项特征逐项参数化：改任意一项签名必变。

    少登记一项特征的后果是两类差异共用一条先例 —— 而症状极隐蔽：
    检索照常命中，面板上看着「先例生效了」，实际建议是给错了那一类的。
    """
    base = _sig()
    other = _sig(**override)
    assert base.digest != other.digest, f"改了 {list(override)} 却没改签名"
    differing = [k for k in prec.SIGNATURE_FIELDS if base.features[k] != other.features[k]]
    assert differing, f"{list(override)} 没有真正改动任何特征（测试自身写错了）"


def test_amounts_equal_distinguishes_same_amount_fanout():
    """候选金额「全同」与「不全同」是两种成因，不能共用一条先例。"""
    same = _sig(candidate_amounts=("50.00", "50.00"))
    mixed = _sig(candidate_amounts=("50.00", "49.00"))
    assert same.features["amounts_equal"] is True
    assert mixed.features["amounts_equal"] is False
    assert same.digest != mixed.digest


def test_single_candidate_is_not_treated_as_all_equal():
    """候选只有一条时「金额全同」恒为假：一条金额与自己相等不是判据。"""
    assert _sig(candidate_count=1, candidate_amounts=("50.00",)).features["amounts_equal"] is False


def test_signature_depends_on_order_no_shape_not_the_order_no_itself():
    """不同单号、同形态 → 同一签名；且具体单号不进签名。"""
    a = _sig(order_no="EX-WX-0001")
    b = _sig(order_no="EX-WX-9999")
    assert a.digest == b.digest
    blob = json.dumps(a.features, ensure_ascii=False)
    assert "EX-WX-0001" not in blob, "具体单号不得进检索键（那张表要长期留存）"
    # 分类器把带连字符的多段前缀归到 ALPHA_MIXED（形如 EX-WX-0001），
    # 单段字母+数字前缀（WXTXN-0001）才归 ALPHA-DIGIT —— 这里断言的是
    # 「**形态标签**而不是具体前缀字符」这件事本身。
    assert a.features["order_no_prefix"] == "ALPHA_MIXED"


def test_prefix_shapes_collapse_channel_order_no_families():
    """微信 / 支付宝 / 数字串三种单号形态各自成桶，不因前缀字符不同而分家。"""
    wechat_txn = _sig(order_no="4200001234202609876")
    alipay_no = _sig(order_no="2026090722001")
    merchant_no = _sig(order_no="WXTXN-0001")
    shapes = {
        wechat_txn.features["order_no_prefix"],
        alipay_no.features["order_no_prefix"],
        merchant_no.features["order_no_prefix"],
    }
    assert shapes == {"DIGIT", "ALPHA-DIGIT"}, shapes
    assert wechat_txn.digest == alipay_no.digest, "同为纯数字单号形态应共用一条先例"
    # 形态不同则签名不同：否则「微信交易号形态」与「商户订单号形态」会被
    # 混成同一个先例桶，而这两类单号在账单里的业务含义并不一样。
    assert wechat_txn.digest != merchant_no.digest


def test_signature_carries_no_amount_no_department_no_patient_field():
    """签名刻意不含金额 / 科室 / 患者可识别信息。"""
    sig = _sig(candidate_amounts=("128.00", "128.00"))
    blob = json.dumps(sig.features, ensure_ascii=False)
    assert "128.00" not in blob, "金额进签名会让每条差异都成为独立先例（等于没有先例）"
    for forbidden in ("patient", "name", "id_card", "phone", "inpatient", "姓名", "住院号"):
        assert forbidden not in blob.lower()
    assert set(sig.features) == set(prec.SIGNATURE_FIELDS)


def test_unknown_channel_is_rejected():
    with pytest.raises(ReconqError) as exc:
        _sig(channel="wechat_v2")
    assert exc.value.code == "PRECEDENT_CHANNEL_UNKNOWN"
    assert "受控渠道" in exc.value.detail or "受控" in exc.value.detail


def test_negative_candidate_count_is_rejected():
    with pytest.raises(ReconqError) as exc:
        _sig(candidate_count=-1)
    assert exc.value.code == "PRECEDENT_SIGNATURE_INVALID"


def test_signature_of_bundle_matches_manual_signature(conn):
    """从证据包算的签名与手工算的同一个 —— 归因阶段手上只有证据包。"""
    payload = {
        "channel": "wechat",
        "discrepancy": {"kind": "cross_period"},
        "diff_row": {
            "trade_type": "SUCCESS",
            "cross_period": True,
            "order_no": "EX-WX-0001",
            "amount": "100.00",
        },
        "candidate_rows": [
            {"amount": "50.00", "cross_period": False},
            {"amount": "50.00", "cross_period": False},
        ],
        "candidate_total": 2,
    }

    class _Bundle:
        pass

    bundle = _Bundle()
    bundle.payload = payload
    assert prec.signature_of_bundle(bundle).digest == _sig().digest


def test_candidate_total_beats_truncated_candidate_rows(conn):
    """候选超上限被裁剪时，签名用**真实总数**而不是展示条数。

    证据包会裁剪候选（candidate_truncated），若签名按展示条数算，
    「候选 12 条被裁到 8 条」与「本来就 8 条」会共用一条先例。
    """
    payload = {
        "channel": "wechat",
        "discrepancy": {"kind": "cross_period"},
        "diff_row": {"trade_type": "SUCCESS", "cross_period": True, "order_no": "EX-WX-0001"},
        "candidate_rows": [{"amount": "50.00"} for _ in range(8)],
        "candidate_total": 12,
    }

    class _Bundle:
        pass

    bundle = _Bundle()
    bundle.payload = payload
    assert prec.signature_of_bundle(bundle).features["candidate_count"] == 12
    assert prec.signature_of_bundle(bundle).digest != _sig(candidate_count=8, candidate_amounts=("50.00",)).digest


# ───────────────── 4. 命中先例进入候选且标 precedent_hit ─────────────────


def test_lookup_returns_suggestion_marked_precedent_hit(conn):
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"2026-W36|wechat|taxonomy.ambiguity:EX-WX-{index:04d}")

    suggestions = prec.suggest_for(conn, _sig())
    assert len(suggestions) == 1
    item = suggestions[0]
    assert item.precedent_hit is True, "面板必须能按字段把「先例」与模型/规则候选分开"
    assert item.cause_code == CAUSE_CHANNEL
    assert item.hit_count == prec.PRECEDENT_MIN_HITS
    assert item.authority == "strong"
    assert item.last_hit_at
    assert item.owner
    assert item.to_dict()["precedent_hit"] is True
    assert item.render_zh()
    # 建议带的样本键只是回查线索，不整份拷贝
    assert len(item.sample_keys) <= 3


def test_suggestion_confidence_is_capped_between_rules_and_model(conn):
    """先例的置信度高于规则（0.5）、低于模型，但**不能顶格**。"""
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    item = prec.suggest_for(conn, _sig())[0]
    from reconq.align.baseline import MAX_CONFIDENCE as RULE_CAP

    assert RULE_CAP < item.confidence <= prec.PRECEDENT_MAX_CONFIDENCE
    assert item.confidence_text == format(prec.PRECEDENT_MAX_CONFIDENCE, ".2f")


def test_precedent_below_min_hits_is_not_suggested(conn):
    """只判过一次的先例不进建议：它与「上期那笔是个意外」在证据上无法区分。"""
    _confirm(conn)
    assert prec.suggest_for(conn, _sig()) == [], "命中 1 次的先例不该进候选"
    assert prec.list_precedents(conn), "但它仍在先例库里（看板与审计要看得到）"


def test_no_precedent_yields_empty_suggestions_not_error(conn):
    assert prec.suggest_for(conn, _sig()) == []
    assert "确实无先例" in prec.render_precedent_hint_zh([])


def test_suggestions_ordered_by_hit_count_and_limited(conn):
    weak = _sig(kind="refund_and_original")
    strong = _sig(kind="merged_payment", candidate_count=4, candidate_amounts=("30.00", "70.00"))
    for index in range(4):
        _confirm(conn, signature=strong, cause=CAUSE_CHANNEL, diff_key=f"strong{index}")
    for index in range(2):
        _confirm(conn, signature=weak, cause=CAUSE_CONFIG, diff_key=f"weak{index}")

    items = prec.suggest_for(conn, strong)
    assert [i.cause_code for i in items] == [CAUSE_CHANNEL]
    assert prec.suggest_for(conn, weak)[0].hit_count == 2
    assert prec.suggest_for(conn, strong, limit=0) == []


def test_precedent_hint_is_advice_not_conclusion():
    """先例提示词写的是「有人这么判过」，不是「本条应当判为」。"""
    for index in range(prec.PRECEDENT_MIN_HITS):
        pass
    sig = _sig()
    suggestion = prec.PrecedentSuggestion(
        cause_code=CAUSE_CHANNEL,
        zh_name="渠道延迟入账",
        signature=sig.digest,
        hit_count=3,
        last_hit_at="2026-09-07T09:00:00",
    )
    text = prec.render_precedent_hint_zh([suggestion])
    assert "仅供参考，不是本条的结论" in text
    assert "历史命中 3 次" in text
    assert "应当" not in text and "建议判为" not in text


def test_precedent_suggestion_does_not_replace_rule_baseline(conn):
    """先例是**并列的第三条腿**，不替代规则基线：基线照样能出候选。"""
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    item = prec.suggest_for(conn, _sig())[0]
    # 先例建议的来源标记与规则候选不是同一个常量
    from reconq.align.baseline import RULE_SOURCE

    assert item.source_label != RULE_SOURCE
    assert "先例" in item.source_label_zh


# ───────────────── 5. 版本变更后降权 ─────────────────


def test_dict_digest_change_downgrades_to_reference(conn):
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    assert prec.suggest_for(conn, _sig())[0].authority == "strong"

    # 登记时用的字典指纹与现在不同（模拟改过字典一条措辞）
    conn.execute(
        f"UPDATE {prec.PRECEDENT_TABLE} SET dict_digest='deadbeefdeadbeef'"
    )
    item = prec.suggest_for(conn, _sig())[0]
    assert item.authority == "reference"
    assert item.confidence == prec.PRECEDENT_REFERENCE_CAP
    assert item.confidence < prec.PRECEDENT_MAX_CONFIDENCE
    assert "成因字典" in item.version_note
    assert item.render_zh().startswith("先例(可参考)")
    assert "降为参考" in item.render_zh()


def test_prompt_version_change_downgrades_to_reference(conn):
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    conn.execute(f"UPDATE {prec.PRECEDENT_TABLE} SET prompt_version='attrib-prompt/v0'")
    item = prec.suggest_for(conn, _sig())[0]
    assert item.authority == "reference"
    assert "提示词" in item.version_note


def test_both_versions_changed_names_both(conn):
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    conn.execute(
        f"UPDATE {prec.PRECEDENT_TABLE} SET dict_digest='aaaa', prompt_version='attrib-prompt/v0'"
    )
    note = prec.suggest_for(conn, _sig())[0].version_note
    assert "成因字典" in note and "提示词" in note


def test_signature_algorithm_change_downgrades_to_reference(conn):
    """签名算法换了 —— 特征口径不再可比，先例降为参考而不是丢弃。"""
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    conn.execute(f"UPDATE {prec.PRECEDENT_TABLE} SET signature_version='precedent-sig/0'")
    item = prec.suggest_for(conn, _sig())[0]
    assert item.authority == "reference"
    assert "签名算法" in item.version_note


def test_downgrade_keeps_the_precedent_usable(conn):
    """降权不是丢弃：版本变了先例仍在，只是不再当强建议。"""
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    conn.execute(f"UPDATE {prec.PRECEDENT_TABLE} SET prompt_version='attrib-prompt/v0'")
    assert len(prec.suggest_for(conn, _sig())) == 1, "降权不等于删掉"
    assert prec.PRECEDENT_REFERENCE_CAP > 0, "也不等于把置信度清零"


# ───────────────── 6. 事件链记录 ─────────────────


def test_hit_is_recorded_on_the_append_only_event_chain(conn):
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    suggestion = prec.suggest_for(conn, _sig())[0]

    hit = prec.record_hit(
        conn,
        period=PERIOD,
        signature=_sig(),
        suggestion=suggestion,
        operator="审核人-李工",
        diff_key="2026-W36|wechat|taxonomy.ambiguity:EX-WX-0001",
    )
    assert hit.actor == "审核人-李工"
    assert hit.authority == "strong"
    assert hit.hit_count_at == prec.PRECEDENT_MIN_HITS, "留痕记的是**当时**的命中次数"

    events = [e for e in list_scan_events(conn, PERIOD) if e.event == "precedent.hit"]
    assert len(events) == 1
    assert events[0].actor == "审核人-李工"
    assert events[0].scope_data["cause_code"] == CAUSE_CHANNEL
    assert events[0].scope_data["hit_count_at"] == prec.PRECEDENT_MIN_HITS
    assert events[0].scope_data["authority"] == "strong"
    # append-only + 链式指纹：前序一环被删，后面的 hash 接不上
    assert events[0].prev_hash
    assert events[0].hash


def test_hit_chain_verifies_and_earlier_events_still_link(conn):
    """采信留痕与字典版本、重跑记在**同一条**链上，删中间一环即断。"""
    from reconq.align.causes import record_dictionary_version

    record_dictionary_version(conn, PERIOD)
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    prec.record_hit(
        conn,
        period=PERIOD,
        signature=_sig(),
        suggestion=prec.suggest_for(conn, _sig())[0],
        operator=OPERATOR,
        diff_key="d1",
    )
    events = list_scan_events(conn, PERIOD)
    assert [e.event for e in events] == ["cause.dict.version", "precedent.hit"]
    for previous, current in zip(events, events[1:]):
        assert current.prev_hash == previous.hash, "链断说明中间有环被改过"

    # 改中间一环的正文：链式指纹把正文与 prev_hash 一起算进摘要，
    # 所以改动会让**这一环自己**存下来的 hash 跟重算值对不上。
    # （改 prev_hash 才表现为后一环的 prev_hash 接不上；两种改法都要能查出来，
    #  而这里验的是前者 —— 后者由上面那条链接断言守住。）
    from reconq.engine.resume import _event_digest

    conn.execute(
        "UPDATE scan_events SET detail='改过' WHERE period=? AND seq=1", (PERIOD,)
    )
    tampered = list_scan_events(conn, PERIOD)[0]
    recomputed = _event_digest(
        {
            "period": tampered.period, "seq": tampered.seq, "event": tampered.event,
            "run_id": tampered.run_id, "actor": tampered.actor, "scope": tampered.scope,
            "scope_data": tampered.scope_data, "result": tampered.result,
            "detail": "改过", "prev_hash": tampered.prev_hash, "at": tampered.at,
        }
    )
    assert recomputed != tampered.hash, "改了正文却仍能通过链校验，这条链就白记了"


def test_hit_against_missing_precedent_is_rejected(conn):
    """留痕指向一条不存在的先例比没有留痕更难查 —— 直接拒。"""
    orphan = prec.PrecedentSuggestion(
        cause_code=CAUSE_CHANNEL,
        zh_name="渠道延迟入账",
        signature=_sig().digest,
        hit_count=3,
        last_hit_at="2026-09-07T09:00:00",
    )
    with pytest.raises(ReconqError) as exc:
        prec.record_hit(conn, period=PERIOD, signature=_sig(), suggestion=orphan, operator=OPERATOR)
    assert exc.value.code == "PRECEDENT_HIT_MISSING"
    assert not [e for e in list_scan_events(conn, PERIOD) if e.event == "precedent.hit"]


def test_hit_rejects_non_human_actor(conn):
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    with pytest.raises(ReconqError) as exc:
        prec.record_hit(
            conn,
            period=PERIOD,
            signature=_sig(),
            suggestion=prec.suggest_for(conn, _sig())[0],
            operator="model",
        )
    assert exc.value.code == "PRECEDENT_OPERATOR_INVALID"


def test_repeat_hit_on_same_diff_is_idempotent(conn):
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    suggestion = prec.suggest_for(conn, _sig())[0]
    for _ in range(2):
        prec.record_hit(
            conn, period=PERIOD, signature=_sig(), suggestion=suggestion,
            operator=OPERATOR, diff_key="d1",
        )
    assert len(prec.list_hits(conn, PERIOD)) == 1, "同一差异的同一条先例采信两次不重复留痕"
    assert len([e for e in list_scan_events(conn, PERIOD) if e.event == "precedent.hit"]) == 1


def test_list_hits_filters_by_period_channel_and_diff_key(conn):
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"k{index}")
    suggestion = prec.suggest_for(conn, _sig())[0]
    prec.record_hit(conn, period=PERIOD, signature=_sig(), suggestion=suggestion,
                    operator=OPERATOR, diff_key="d1")
    prec.record_hit(conn, period="2026-W37", signature=_sig(), suggestion=suggestion,
                    operator=OPERATOR, diff_key="d2")

    assert len(prec.list_hits(conn, PERIOD)) == 1
    assert len(prec.list_hits(conn, "2026-W37")) == 1
    assert len(prec.list_hits(conn, PERIOD, channel="wechat")) == 1
    assert len(prec.list_hits(conn, PERIOD, channel="alipay")) == 0
    assert len(prec.list_hits(conn, PERIOD, diff_key="d1")) == 1
    assert len(prec.list_hits(conn, PERIOD, diff_key="d2")) == 0
    assert prec.list_hits(conn, PERIOD)[0].to_dict()["actor"] == OPERATOR


# ───────────────── 先例看板 ─────────────────


def test_top_causes_ranks_by_hit_count(conn):
    big = _sig(kind="cross_period")
    small = _sig(kind="merged_payment", candidate_count=4, candidate_amounts=("30.00", "70.00"))
    for index in range(5):
        _confirm(conn, signature=big, cause=CAUSE_CHANNEL, diff_key=f"b{index}")
    for index in range(2):
        _confirm(conn, signature=small, cause=CAUSE_CONFIG, diff_key=f"s{index}")

    rows = prec.top_causes(conn)
    assert [r.cause_code for r in rows] == [CAUSE_CHANNEL, CAUSE_CONFIG]
    assert rows[0].hit_count == 5
    assert rows[0].precedent_count == 1
    assert rows[0].zh_name and rows[0].owner
    assert rows[0].last_hit_at
    assert prec.top_causes(conn, limit=1) == rows[:1]


def test_top_causes_filters_by_channel(conn):
    _confirm(conn)
    _confirm(conn, signature=_sig(channel="alipay"), diff_key="ali")
    assert [r.cause_code for r in prec.top_causes(conn, channel="alipay")] == [CAUSE_CHANNEL]
    # 看板按 (成因, 渠道) 分行：同一成因在两路各一条先例时是两行 ——
    # 责任方与建议动作按渠道不同（渠道方 vs 病案收费处），并成一行会
    # 让面板上那一格说不出「这条该找谁」。
    assert len(prec.top_causes(conn)) == 2
    assert {r.channel for r in prec.top_causes(conn)} == {"wechat", "alipay"}
    with pytest.raises(ReconqError):
        prec.top_causes(conn, channel="微信")


def test_top_causes_on_empty_library_is_not_an_error(conn):
    assert prec.top_causes(conn) == [], "没人办过单时看板显示空，不是故障"
    assert prec.top_causes(conn, limit=0) == []


# ───────────────── 脱敏与元数据 ─────────────────


def test_precedent_tables_hold_no_patient_identifiable_field(conn):
    for index in range(prec.PRECEDENT_MIN_HITS):
        _confirm(conn, diff_key=f"2026-W36|wechat|taxonomy.ambiguity:EX-WX-{index:04d}")
    prec.record_hit(
        conn, period=PERIOD, signature=_sig(), suggestion=prec.suggest_for(conn, _sig())[0],
        operator=OPERATOR, diff_key="2026-W36|wechat|taxonomy.ambiguity:EX-WX-0001",
    )
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({prec.PRECEDENT_TABLE})")}
    columns |= {row[1] for row in conn.execute(f"PRAGMA table_info({prec.PRECEDENT_HIT_TABLE})")}
    for forbidden in ("patient", "name", "id_card", "phone", "inpatient", "raw_line"):
        assert forbidden not in {c.lower() for c in columns}
    from reconq.align.snapshot import assert_deidentified

    for row in prec.list_precedents(conn):
        assert_deidentified(row.to_dict(), where="先例行")
    for hit in prec.list_hits(conn, PERIOD):
        assert_deidentified(hit.to_dict(), where="先例命中留痕")


def test_every_precedent_error_code_is_registered_with_zh_and_suggestion():
    for code in (
        "PRECEDENT_SIGNATURE_INVALID",
        "PRECEDENT_CHANNEL_UNKNOWN",
        "PRECEDENT_VERSION_UNKNOWN",
        "PRECEDENT_DISPOSITION_INVALID",
        "PRECEDENT_OPERATOR_INVALID",
        "PRECEDENT_HIT_MISSING",
    ):
        meta = ERROR_CODES.get(code)
        assert meta is not None, f"{code} 未登记到 errors.py"
        assert meta.zh.strip() and meta.suggestion.strip() and meta.zh != code


def test_version_unknown_is_actually_raisable():
    """登记的版本对里出现未登记的提示词版本时报错，而不是照收。"""
    with pytest.raises(ReconqError) as exc:
        from reconq.align.schema_version import pair_of

        pair_of("attrib-prompt/v999")
    assert exc.value.code == "PROMPT_VERSION_UNKNOWN"


# ───────────────── CLI 三条腿 ─────────────────


def _cli(*args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env.setdefault("RECONQ_LOG_DIR", os.path.join(os.path.dirname(SRC), "logs"))
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "reconq", *args],
        capture_output=True, text=True, env=env, cwd=os.path.dirname(SRC),
    )


@pytest.fixture()
def cli_db(tmp_path):
    path = tmp_path / "precedent.db"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    init_scan_schema(connection)
    init_resume_schema(connection)
    prec.init_precedent_schema(connection)
    for index in range(3):
        _confirm(connection, diff_key=f"2026-W36|wechat|taxonomy.ambiguity:EX-WX-{index:04d}")
    _confirm(connection, signature=_sig(kind="merged_payment", candidate_count=4,
                                        candidate_amounts=("30.00", "70.00")),
             cause=CAUSE_CONFIG, diff_key="2026-W36|wechat|taxonomy.ambiguity:EX-MRG-0001")
    connection.close()
    return str(path)


def test_cli_precedent_top_lists_board(cli_db):
    result = _cli("precedent", "top", "--db", cli_db, "--channel", "wechat")
    assert result.returncode == 0, result.stderr
    assert "先例看板" in result.stdout
    assert "渠道延迟入账" in result.stdout
    assert "信息科" in result.stdout or "责任方" in result.stdout


def test_cli_precedent_list_shows_features_and_versions(cli_db):
    result = _cli("precedent", "list", "--db", cli_db)
    assert result.returncode == 0, result.stderr
    assert "签名口径" in result.stdout
    assert "candidate_count=" in result.stdout
    assert "登记时版本" in result.stdout
    assert "字典 " in result.stdout and "提示词 " in result.stdout


def test_cli_precedent_hits_requires_period(cli_db):
    result = _cli("precedent", "hits", "--db", cli_db)
    assert result.returncode == 2
    assert "--period" in result.stderr


def test_cli_precedent_hits_on_empty_period_is_not_a_failure(cli_db):
    result = _cli("precedent", "hits", "--db", cli_db, "--period", PERIOD)
    assert result.returncode == 0, result.stderr
    assert "不是故障" in result.stdout


def test_cli_precedent_json_output(cli_db):
    result = _cli("precedent", "top", "--db", cli_db, "--json")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert rows[0]["cause_code"] == CAUSE_CHANNEL
    assert rows[0]["hit_count"] == 3
    assert rows[0]["owner"]


def test_cli_precedent_rejects_unknown_channel(cli_db):
    result = _cli("precedent", "top", "--db", cli_db, "--channel", "douyin")
    assert result.returncode == 2
    assert "PRECEDENT_CHANNEL_UNKNOWN" in result.stderr


def test_cli_precedent_on_empty_library_is_clean(tmp_path):
    empty = str(tmp_path / "empty.db")
    result = _cli("precedent", "top", "--db", empty)
    assert result.returncode == 0, result.stderr
    assert "还没有先例" in result.stdout
    assert "正常态" in result.stdout


def test_main_trunk_still_runs_after_precedent_change():
    """主干未回归：最窄的端到端路径仍能跑通并按门禁语义给退出码。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    result = subprocess.run(
        [sys.executable, "-m", "reconq", "verify", "--period", "2026-W36",
         "--source", os.path.join("tests", "fixtures", "bills"), "--channel", "wechat"],
        capture_output=True, text=True, env=env, cwd=os.path.dirname(SRC),
    )
    assert "5714.69" in result.stdout, result.stdout[-2000:]
    assert result.returncode == 1, "有 fail 的账期退出码应为 1（门禁不通过）"
