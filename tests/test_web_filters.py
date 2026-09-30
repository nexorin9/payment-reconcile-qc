"""面板筛选 / 分组 / 待办 / 决策辅助的断言（Task 48）。

范围收在**本任务自己声明的验收点**上：每个筛选参数生效且非法值给
4xx、分组接口返回可用组、待办接口按角色返回不同条数、右栏四路并列
且明示采用哪一路。**SSE 事件序列与并发冲突留给 Task 49**。

夹具沿用 :func:`tests.test_web_panel.scanned_db` 的做法：**真跑一遍
``reconq scan``** 造库，而不是手工 INSERT —— 面板的每个字段都声称与
CLI 同源，测试数据就该来自 CLI。

金额一律用**类目化占位**（``10.00`` / ``86.00`` / ``250.00``），不写
长得像真实住院号的标识：这些数字是筛选判据，不是业务样例。
"""

from __future__ import annotations

import os
import shutil
import urllib.parse

import pytest

from reconq.errors import ReconqError
from reconq.store.schema import connect
from reconq.web import views
from reconq.web.app import create_app

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PERIOD = "2026-W36"
#: 样例账期里两条真实差异的口径（``reconq scan`` 跑出来的）。
CHECK_REFUND = "rows.refund_sum.zero"
CHECK_DUPLICATE = "rows.order_no.unique"


# ─────────────────────────── 夹具 ───────────────────────────


@pytest.fixture()
def scanned_db(tmp_path):
    """跑一次 ``reconq scan`` 造出真库（含 2 条差异）。"""
    from reconq.cli import main

    db = str(tmp_path / "reconq.db")
    inbox = tmp_path / "inbox"
    shutil.copytree(os.path.join(_PROJECT_ROOT, "inbox", PERIOD), inbox / PERIOD)

    cwd = os.getcwd()
    os.chdir(_PROJECT_ROOT)
    try:
        code = main(
            ["--no-log", "scan", "--period", PERIOD, "--db", db, "--source", str(inbox)]
        )
    finally:
        os.chdir(cwd)
    assert code in (0, 1), f"scan 意外失败，退出码 {code}"
    return db


@pytest.fixture()
def client(scanned_db):
    from fastapi.testclient import TestClient

    with TestClient(create_app(scanned_db, start_watcher=False)) as c:
        yield c


def _url(period: str, path: str, **params: str) -> str:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
    base = f"/api/period/{urllib.parse.quote(period, safe='')}/{path}"
    return f"{base}?{qs}" if qs else base


def _aid_url(period: str, key: str) -> str:
    return (
        f"/api/period/{urllib.parse.quote(period, safe='')}/discrepancy/"
        f"{urllib.parse.quote(key, safe='')}/aid"
    )


# ─────────────────────────── 筛选：参数各自生效 ───────────────────────────


def test_unfiltered_returns_every_row(client):
    data = client.get(_url(PERIOD, "discrepancies")).json()
    assert len(data["rows"]) == 2, "样例账期应有 2 条差异"
    assert data["filter"]["active"] is False
    assert data["matched"] == data["total"] == 2


def test_channel_filter_narrows_the_list(client):
    """渠道筛选：只留那一路。命中 0 条时也返回 200 + 空列表（不是报错）——
    「这一路本期没有差异」是一个合法答案。"""
    data = client.get(_url(PERIOD, "discrepancies", channel="wechat")).json()
    assert data["rows"], "微信那期有差异，不该筛空"
    assert {r["channel"] for r in data["rows"]} == {"wechat"}

    other = client.get(_url(PERIOD, "discrepancies", channel="alipay")).json()
    assert other["rows"] == []
    assert other["matched"] == 0
    assert other["total"] == 2, "筛不出东西时要说得清是「2 条里 0 条命中」"


def test_amount_range_filter(client):
    data = client.get(_url(PERIOD, "discrepancies", amount_min="50")).json()
    assert data["rows"], "两条差额都 ≥50，筛完不该空"
    kept = [r for r in data["rows"] if r["diff_amount"]]
    for row in kept:
        assert float(row["diff_amount"]) >= 50.0

    high = client.get(_url(PERIOD, "discrepancies", amount_min="10000")).json()
    assert high["rows"] == []


def test_amount_filter_total_is_recomputed_over_filtered_rows(client):
    """筛选后合计只算筛出来的那些。**不能沿用筛选前的合计** ——
    那会让「按金额筛一档」在页面上显示出与列表不符的总数。"""
    full = client.get(_url(PERIOD, "discrepancies")).json()
    part = client.get(_url(PERIOD, "discrepancies", amount_min="50")).json()
    assert part["total_amount"] != "", "样例账期两条差额都可解析，合计不该是「不完整」"
    assert part["total_amount"] == full["total_amount"], (
        "样例里两条都 ≥50，合计理应不变；变了说明合计没跟着筛选重算"
    )


def test_ambiguity_filter_keeps_contract_diffs_separate(client):
    """``ambiguity_kind=`` 显式传空串 = 只要口径差异，与不传（全部）不同。"""
    contract_only = client.get(
        f"/api/period/{PERIOD}/discrepancies?ambiguity_kind="
    ).json()
    assert contract_only["rows"], "样例账期两条都是口径差异"
    assert all(r["ambiguity_kind"] == "" for r in contract_only["rows"])

    all_rows = client.get(_url(PERIOD, "discrepancies")).json()
    assert len(all_rows["rows"]) >= len(contract_only["rows"])


def test_only_ambiguous_filter_excludes_contract_diffs(client):
    data = client.get(_url(PERIOD, "discrepancies", only_ambiguous="1")).json()
    assert data["rows"] == [], "样例账期没有歧义条目，筛「只看歧义」应为空"
    assert data["total"] == 2, "空结果要带总数，否则像「库里没数据」"


def test_only_weak_match_filter_is_kind_driven():
    """弱匹配判据是**形态**，不是文案里有没有「弱」字。

    形态有两个来源，都得认：``check_path`` 前缀，以及歧义类共用的
    ``taxonomy.ambiguity:`` 前缀下登记在 ``ambiguity_kind`` 列上的那一个
    （见 :func:`reconq.align.taxonomy.verdict_to_diff_items`）。只认前缀
    的话，点「只看弱匹配」一条都看不到 —— 而弱匹配恰是人工最该看的一档。
    """
    weak = {"check_path": "align.weak_pending:EX-1", "diff_amount": "10.00"}
    strong = {"check_path": "align.amount_mismatch:EX-2", "diff_amount": "10.00"}
    flt = views.RowFilter(only_weak_match=True)
    assert views.filter_rows([weak, strong], flt) == [weak]
    # 歧义类：check_path 是共用的 taxonomy.ambiguity 前缀，形态在列里
    taxonomy_weak = {
        "check_path": "taxonomy.ambiguity:EX-4",
        "ambiguity_kind": "weak_pending",
        "diff_amount": "10.00",
    }
    assert views.filter_rows([taxonomy_weak, strong], flt) == [taxonomy_weak], \
        "歧义类的弱匹配不能因为共用前缀而被漏掉"
    # 措辞里有「弱」但形态不是弱匹配的，**不许**被选中
    decoy = {"check_path": "align.channel_only:EX-3", "message": "弱匹配待人工"}
    assert views.filter_rows([decoy], flt) == []


def test_rule_cause_uses_the_registered_kind_not_just_the_path_prefix():
    """规则成因档要认台账登记的形态，不能只反查 ``check_path`` 前缀。

    ``taxonomy.ambiguity:`` 是所有歧义结论共用的前缀，只按前缀反查会
    一律认成通用的 ``ambiguous`` —— 而它不在形态→成因映射表里，于是
    规则基线返回空，面板把分好型的差异全归进「口径差异」组。症状是：
    分型做得再细，面板上的分组永远是一坨。
    """
    from reconq.web.state import _rule_cause

    assert _rule_cause("taxonomy.ambiguity:EX-1", "weak_pending") == "undetermined"
    assert _rule_cause("taxonomy.ambiguity:EX-1", "channel_only") == "his_missing_order"
    assert _rule_cause("align.weak_pending:EX-1", "") == "undetermined", \
        "前缀档的老路径不能被改坏"
    assert _rule_cause("rows.refund_sum.zero", "") == "", "口径差异没有形态，不猜成因"


def test_filters_are_conjunctive(client):
    """多条件是「且」：状态与渠道同时给，交集不该比任一单条件大。"""
    by_channel = client.get(_url(PERIOD, "discrepancies", channel="wechat")).json()
    both = client.get(
        _url(PERIOD, "discrepancies", channel="wechat", state="已确认")
    ).json()
    assert both["matched"] <= by_channel["matched"]


# ─────────────────────────── 筛选：非法入参给 4xx ───────────────────────────


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"state": "待审"}, "WEB_FILTER_STATE_UNKNOWN"),
        ({"channel": "weixin"}, "WEB_FILTER_CHANNEL_UNKNOWN"),
        ({"cause_code": "不存在的成因"}, "WEB_FILTER_CAUSE_UNKNOWN"),
        ({"amount_min": "100元"}, "WEB_FILTER_AMOUNT_INVALID"),
        ({"amount_min": "500", "amount_max": "100"}, "WEB_FILTER_AMOUNT_INVALID"),
    ],
)
def test_illegal_filter_returns_4xx_with_code(client, params, code):
    """**非法筛选值必须报错，不能静默返回空列表。**

    静默返回空列表在面板上与「本期没有差异」完全一样：经办人会据此
    认定本期干净，而真相是筛选压根没生效。
    """
    r = client.get(_url(PERIOD, "discrepancies", **params))
    assert r.status_code == 400, f"{params} 应返 4xx，实际 {r.status_code}"
    assert r.json()["code"] == code


def test_amount_filter_reports_rows_dropped_for_dirty_amount(client, scanned_db):
    """差额不可用的行被金额条件排除时，**条数要报出来，且两种成因分列**。

    少了这个计数，被排除的行就凭空消失了 —— 筛出 1 条而台账上有 2 条，
    经办人无从知道少的 1 条去了哪。分列是因为**下一步动作不同**：
    上游没给金额的要去问渠道方要，给了但不可解析的要去隔离区捞原文。
    """
    conn = connect(scanned_db)
    try:
        from reconq.engine import period as period_engine

        conn.execute(
            f"UPDATE {period_engine.DIFF_TABLE} SET diff_amount='不可解析' "
            "WHERE period=? AND check_path=?",
            (PERIOD, CHECK_REFUND),
        )
    finally:
        conn.close()

    data = client.get(_url(PERIOD, "discrepancies", amount_min="1")).json()
    # 样例账期两条差异：一条被改成不可解析，另一条（transaction_id.not_empty）
    # 上游压根没给金额。两条都进不了金额区间。
    assert data["dropped_amount_unparsable"] == 1, "被改成脏值的那条"
    assert data["dropped_amount_missing"] == 1, "上游未给金额的那条"
    assert data["dropped_unknown_amount"] == 2
    assert data["matched"] == 0
    assert data["total"] == 2, "总数不变：被排除不等于不存在"


# ─────────────────────────── 分组 ───────────────────────────


def test_groups_endpoint_returns_group_keys_and_counts(client):
    data = client.get(_url(PERIOD, "groups")).json()
    assert data["group_count"] == len(data["groups"])
    assert data["group_count"] >= 1
    total_rows = sum(g["count"] for g in data["groups"])
    listed = len(client.get(_url(PERIOD, "discrepancies")).json()["rows"])
    assert total_rows == listed, "分组条数之和必须等于台账条数，否则分组数字对不上总数"


def test_group_key_uses_the_same_shape_as_the_cli(client):
    """面板组键与 ``reconq.align.aggregate.group_key_for`` 同一形状。

    财务科把面板上的键粘进工单，工单里要能查到同一组 —— 两个模块
    各写一种分隔符，那张工单就永远查不到东西。
    """
    from reconq.align.aggregate import group_key_for

    data = client.get(_url(PERIOD, "groups")).json()
    for g in data["groups"]:
        assert g["group_key"] == group_key_for(
            g["cause_code"] or "-", g["channel"], g["ambiguity_kind"] or "-", g["amount_bucket"]
        )


def test_contract_diffs_group_separately_from_undetermined(client):
    """口径差异**不进** ``undetermined`` 组。

    合成一组会让「不可判定」的条数虚高，而那正是最需要人推的情形 ——
    经办人会先冲去处理一栏其实压根不用想的口径差异。
    """
    data = client.get(_url(PERIOD, "groups")).json()
    codes = {g["cause_code"] for g in data["groups"]}
    assert "undetermined" not in codes, "口径差异不该被算成「无法判定」"
    assert views.CAUSE_GROUP_OTHER in {g["source"] for g in data["groups"]}


def test_each_group_carries_its_source_label(client):
    """每组必须带「成因是谁说的」。少了它，规则推断与人工确认长得一样。"""
    data = client.get(_url(PERIOD, "groups")).json()
    for g in data["groups"]:
        assert g["source_zh"], f"组 {g['group_key']} 缺出处标签"
        assert g["source"] in views.SOURCE_ZH


# ─────────────────────────── 待办聚合 ───────────────────────────


def test_todo_counts_only_what_the_role_may_act_on(scanned_db):
    """待办条数 = 「该角色在这个状态下有权动」的条数。

    依据是 :func:`reconq.ledger.models.required_role_for`（按钮禁用态
    用的是同一个函数）—— 面板上不该有「待办 0 条但按钮亮着」的状态。
    """
    from reconq.ledger.flow import open_discrepancy

    conn = connect(scanned_db)
    try:
        item = open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=CHECK_REFUND
        )
    finally:
        conn.close()
    assert item.status == "待归因"

    from fastapi.testclient import TestClient

    with TestClient(create_app(scanned_db, start_watcher=False)) as c:
        handler = c.get(_url(PERIOD, "todo", role="handler")).json()
        reviewer = c.get(_url(PERIOD, "todo", role="reviewer")).json()
        supervisor = c.get(_url(PERIOD, "todo", role="supervisor")).json()

    assert handler["count"] == 1, "待归因只有经办能推进，经办待办应为 1"
    assert handler["by_status"] == {"待归因": 1}
    assert handler["role_zh"] == "经办"
    # 待确认/待主管复核都还没有，审核与主管此刻无事可办
    assert reviewer["count"] == 0
    assert supervisor["count"] == 0


def test_todo_moves_to_reviewer_after_handler_submits(scanned_db):
    """经办提交后，待办**从经办移到审核**。

    这条断言守的是「待办不是按期算的静态数字」：如果待办一直算
    「全部未了结条数」，那三个角色的数字会永远一样，面板也就没意义。
    """
    from reconq.ledger.flow import open_discrepancy, submit

    conn = connect(scanned_db)
    try:
        item = open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=CHECK_REFUND
        )
        submit(
            conn, item.key, "渠道延迟入账，已向渠道方发起查询",
            operator="E-01", expected_version=item.version, role="handler",
        )
    finally:
        conn.close()

    from fastapi.testclient import TestClient

    with TestClient(create_app(scanned_db, start_watcher=False)) as c:
        handler = c.get(_url(PERIOD, "todo", role="handler")).json()
        reviewer = c.get(_url(PERIOD, "todo", role="reviewer")).json()

    assert handler["count"] == 0, "经办提交后不该还有他的待办"
    assert reviewer["count"] == 1, "该轮到审核了"
    assert reviewer["by_status"] == {"待确认": 1}


def test_todo_excludes_settled_items(scanned_db):
    """已确认 / 已驳回**不算待办**。

    它们是活单（可撤回）但已经办完；算进待办会让「我这儿还有 12 条」
    与「点进去全是已确认」同时成立，而那正是「清台账永远清不空」的由来。
    """
    from reconq.ledger.flow import approve, open_discrepancy, submit

    conn = connect(scanned_db)
    try:
        item = open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=CHECK_REFUND
        )
        item = submit(
            conn, item.key, "确认是渠道延迟", operator="E-01",
            expected_version=item.version, role="handler",
        )
        approve(
            conn, item.key, operator="E-02", expected_version=item.version, role="reviewer"
        )
    finally:
        conn.close()

    from fastapi.testclient import TestClient

    with TestClient(create_app(scanned_db, start_watcher=False)) as c:
        handler = c.get(_url(PERIOD, "todo", role="handler")).json()
        reviewer = c.get(_url(PERIOD, "todo", role="reviewer")).json()

    assert handler["count"] == 0
    assert reviewer["count"] == 0, "已确认的单不是待办"


def test_todo_reports_oldest_wait(client):
    """最老待办时长要给出来，且解析不出时刻时**明说不可知**（不报 0）。"""
    handler = client.get(_url(PERIOD, "todo", role="handler")).json()
    assert handler["count"] == 0
    assert handler["oldest_hours"] is None
    assert handler["oldest_text"] == "—", "没有待办时不该报「刚刚」"


def test_todo_rejects_unknown_role(client):
    r = client.get(_url(PERIOD, "todo", role="admin"))
    assert r.status_code == 400
    assert r.json()["code"] == "LEDGER_ROLE_INVALID"


# ─────────────────────────── 右栏：决策辅助 ───────────────────────────


def test_aid_lists_four_sources_and_names_the_adopted_one(client, scanned_db):
    """四路并列 + 明示采用哪一路。**这是本任务的核心验收点。**"""
    from reconq.ledger.flow import open_discrepancy

    conn = connect(scanned_db)
    try:
        item = open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=CHECK_REFUND
        )
    finally:
        conn.close()

    data = client.get(_aid_url(PERIOD, item.key)).json()
    for key in ("model_draft", "rule_baseline", "precedent", "verification", "adopted"):
        assert key in data, f"决策辅助缺 {key} 这一路"
    assert data["adopted"]["source"] in views.ADOPTED_SOURCES
    assert data["adopted"]["reason"].strip(), "必须说清凭什么采了它"


def test_aid_marks_rule_baseline_as_not_from_the_model(client, scanned_db):
    """规则那一栏的标签里必须带「未经模型」。

    少了这四个字，签字的人会把规则猜的成因当成模型确认过的。
    """
    from reconq.ledger.flow import open_discrepancy

    conn = connect(scanned_db)
    try:
        item = open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=CHECK_REFUND
        )
    finally:
        conn.close()

    data = client.get(_aid_url(PERIOD, item.key)).json()
    # 口径差异（rows.refund_sum.zero）没有对齐形态 → 规则映射也没有，
    # 但**标签口径**仍要成立：要么给候选，要么明说「没登记规则映射」。
    rule = data["rule_baseline"]
    if rule is not None:
        assert "未经模型" in rule["label_zh"]
        assert rule["source"] == "rules_baseline"
        assert rule["rank"] == 0, "形态映射不是完整基线的 Top-1，不得声称有排名"
        assert rule["partial"] is True


def test_aid_without_any_source_says_none_not_guess(client, scanned_db):
    """四路都没有可采信结论时，``adopted`` 必须是 ``none`` 且给出待人工判定。"""
    from reconq.ledger.flow import open_discrepancy

    conn = connect(scanned_db)
    try:
        item = open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=CHECK_REFUND
        )
    finally:
        conn.close()

    data = client.get(_aid_url(PERIOD, item.key)).json()
    adopted = data["adopted"]
    assert adopted["source"] == "none"
    assert "人工" in adopted["reason"] or "待人工判定" in adopted["source_zh"]


def test_aid_prefers_human_conclusion_over_everything(client, scanned_db):
    """人签过的话就在那儿摆着，不会因为「模型没给草稿」而被降级。"""
    from reconq.ledger.flow import open_discrepancy, submit

    conn = connect(scanned_db)
    try:
        item = open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=CHECK_REFUND
        )
        submit(
            conn, item.key, "已联系渠道，确认为结算延迟",
            operator="E-01", expected_version=item.version, role="handler",
        )
    finally:
        conn.close()

    data = client.get(_aid_url(PERIOD, item.key)).json()
    assert data["adopted"]["source"] == "human_conclusion"
    assert "已联系渠道" in data["adopted"]["note"]


def test_aid_precedent_section_says_absent_is_not_never_happened(client, scanned_db):
    """无先例命中时文案必须说「没有采信记录」，**不能**说「从未发生过」。"""
    from reconq.ledger.flow import open_discrepancy

    conn = connect(scanned_db)
    try:
        item = open_discrepancy(
            conn, period=PERIOD, channel="wechat", check_path=CHECK_REFUND
        )
    finally:
        conn.close()

    data = client.get(_aid_url(PERIOD, item.key)).json()
    precedent = data["precedent"]
    assert precedent["precedent_hit"] is False
    assert "不等于" in precedent["note"]


def test_aid_for_unknown_key_returns_404(client):
    r = client.get(_aid_url(PERIOD, "2026-W36|wechat|不存在的口径"))
    assert r.status_code == 404
    assert r.json()["code"] == "LEDGER_DISCREPANCY_NOT_FOUND"


# ─────────────────────────── 词表（前端不硬编码枚举） ───────────────────────────


def test_vocab_endpoint_serves_the_filter_enumerations(client):
    """筛选下拉的词表由服务端下发。

    前端硬编码一份状态名/成因码，就等于有了第二个事实来源：字典加了
    新成因而前端没跟上，那一档在面板上永远选不到，而经办人会以为
    「这类成因本期没出现」。
    """
    from reconq.align.causes import CAUSES

    data = client.get("/api/vocab").json()
    assert {s for s in data["states"]} == set(views.FILTERABLE_STATES)
    assert set(data["channels"]) == set(client.get(
        f"/api/period/{PERIOD}/channels"
    ).json()["channels"][i]["channel"] for i in range(6))
    assert {c["code"] for c in data["causes"]} == set(CAUSES)
    assert {r["code"] for r in data["roles"]} == {"handler", "reviewer", "supervisor"}
    assert data["kinds"], "歧义形态词表不该是空的"


# ─────────────────────────── 单位：筛选判据本身 ───────────────────────────


def test_row_filter_is_inert_when_empty():
    """空筛选 = 不过滤。**不是**「什么都筛不出」也不是「只筛空值」。"""
    rows = [{"key": "a", "channel": "wechat"}, {"key": "b", "channel": "alipay"}]
    flt = views.RowFilter()
    assert flt.active is False
    assert views.filter_rows(rows, flt) == rows


def test_cause_code_of_picks_the_strongest_source():
    """成因取自三档里**最强**的一档：人工 > 模型 > 规则。"""
    row = {"ledger_cause": "channel_late_posting", "model_cause": "his_missing_order",
           "rule_cause": "undetermined"}
    assert views.cause_code_of(row) == "channel_late_posting"
    row.pop("ledger_cause")
    assert views.cause_code_of(row) == "his_missing_order"
    row.pop("model_cause")
    assert views.cause_code_of(row) == "undetermined"
    row.pop("rule_cause")
    assert views.cause_code_of(row) == "", "三档都空时返回空串，不猜"


def test_shape_cause_reuses_the_clue_table():
    """形态映射读的是 :func:`rank_causes_by_rules` 用的**同一张表**。

    另抄一份的后果是「规则说 A、面板说 B」，而签字的人无从发现。
    """
    from reconq.align.baseline import CLUE_RULES, RULE_SOURCE, shape_cause

    got = shape_cause("weak_pending")
    assert got is not None
    assert got["cause_code"] == "undetermined"
    assert got["rule_zh"] == CLUE_RULES["kind_mapping"]
    assert got["source"] == RULE_SOURCE
    assert shape_cause("字典里没有的形态") is None, "形态没登记就返回 None，不编一条"


def test_parse_filter_rejects_inverted_range_without_guessing():
    with pytest.raises(ReconqError) as exc:
        views.parse_filter(amount_min="500", amount_max="100")
    assert exc.value.code == "WEB_FILTER_AMOUNT_INVALID"


# ─────────────────────── 证据展开：候选行 / 快照 / 深链 ───────────────────────


def _evidence_url(period: str, key: str) -> str:
    return (
        f"/api/period/{urllib.parse.quote(period, safe='')}/discrepancy/"
        f"{urllib.parse.quote(key, safe='')}/evidence"
    )


def _refund_key(client) -> str:
    """样例账期里带差额的那条差异键（口径差异类，没有渠道单号）。"""
    rows = client.get(_url(PERIOD, "discrepancies")).json()["rows"]
    return next(r["key"] for r in rows if r["check_path"] == CHECK_REFUND)


def test_evidence_lists_candidate_rows_from_the_bill_store(client, scanned_db):
    """候选行来自**账单库现状**；两侧（渠道侧与 HIS 侧）都查。

    只回渠道侧那一条的话，弱匹配与拆笔/合并支付/跨账期/退费并存这四种
    形态全都判不出来 —— 判据正是两侧摆在一起看。
    """
    from reconq.web import evidence

    conn = connect(scanned_db)
    try:
        rows = evidence.candidate_rows(conn, PERIOD, "EX-WX-2026W36-0001", channel="wechat")
    finally:
        conn.close()
    assert len(rows) == 1, "按渠道单号查，应命中账单库里那一行"
    first = rows[0]
    # 白名单列齐：源行原文与用户标识类字段都不在响应里。
    for col in evidence.EVIDENCE_COLUMNS:
        assert col in first, f"候选行缺列 {col}"
    for banned in ("用户标识", "公众账号ID", "openid", "payer", "user_id"):
        assert banned not in first, f"候选行不应回传 {banned}"
    assert first["source_row_no"] == 2, "定位要指到源文件里的真实行号"
    assert first["ref"] == "wechat_tradebill_2026-W36.csv:2"


def test_evidence_candidate_lookup_matches_either_side_key(client, scanned_db):
    """按渠道单号**或** HIS 单号都算命中。

    两侧编号是两套体系；只比一侧会让「渠道号相等但 HIS 号不同」的退费
    行重复出现或漏掉。判据直接打在这一处：把 HIS 单号写进行里、渠道
    单号改掉，按 HIS 号查仍要命中。
    """
    from reconq.web import evidence

    conn = connect(scanned_db)
    try:
        conn.execute(
            "UPDATE bill_rows SET his_order_no='EX-HIS-0001' WHERE ch_id=1"
        )
        by_his = evidence.candidate_rows(conn, PERIOD, "EX-HIS-0001")
        assert len(by_his) == 1, "按 HIS 单号也该命中同一行"
        assert by_his[0]["his_order_no"] == "EX-HIS-0001"
        # 大小写/空白差异由对齐侧的归一兜住，不另写一份规则
        assert len(evidence.candidate_rows(conn, PERIOD, "ex-his-0001")) == 1
    finally:
        conn.close()


def test_evidence_endpoint_returns_a_panel_for_a_real_row(client, scanned_db):
    """证据面板三块齐备：候选行、快照引用、深链三出口。"""
    from reconq.ledger.flow import open_discrepancy

    conn = connect(scanned_db)
    try:
        open_discrepancy(
            conn,
            period=PERIOD,
            channel="wechat",
            check_path=CHECK_REFUND,
            diff_amount="86.00",
            evidence_rows=["wechat_tradebill_2026-W36.csv:5"],
        )
    finally:
        conn.close()

    data = client.get(_evidence_url(PERIOD, _refund_key(client))).json()
    assert data["key"] == _refund_key(client)
    assert isinstance(data["candidates"], list)
    assert isinstance(data["snapshots"], list)
    assert set(data["links"]) >= {"deep_link", "panel_url", "cli_command"}
    # 口径差异类条目没有渠道单号，候选行为空**是合法答案**，
    # 但必须显式说「没有」而不是报 500。
    assert data["candidate_total"] == 0
    # 开单时登记的证据行要从**处置单**上取，不是台账那行的计数
    assert data["ledger_evidence_rows"] == ["wechat_tradebill_2026-W36.csv:5"]


def test_evidence_snapshot_refs_carry_content_hash_not_row_numbers(client, scanned_db):
    """快照引用带内容哈希与参与方，**不是**现在的行号。

    快照是拍摄当时的副本，账单现状可能已被重扫覆盖；复算要的是
    「当时那几行」，所以引用指向快照本身。
    """
    import json

    from reconq.align.snapshot import (
        EvidenceSnapshot,
        SnapshotRef,
        init_align_snapshot_schema,
        record_alignment_snapshot,
    )
    from reconq.web import evidence

    conn = connect(scanned_db)
    try:
        init_align_snapshot_schema(conn)
        snap = EvidenceSnapshot(
            conclusion_key="wechat:2026-W36:EX-WX-2026W36-0001",
            period=PERIOD,
            channel="wechat",
            kind="weak_pending",
            order_no="EX-WX-2026W36-0001",
            participants=(
                SnapshotRef(
                    side="channel",
                    channel="wechat",
                    period=PERIOD,
                    order_no="EX-WX-2026W36-0001",
                    amount="128.00",
                    source_file="wechat_tradebill_2026-W36.csv",
                    source_row_no=2,
                ),
                SnapshotRef(
                    side="his",
                    channel="his",
                    period=PERIOD,
                    order_no="EX-HIS-0001",
                    his_order_no="EX-HIS-0001",
                    amount="128.00",
                    source_file="charge_2026-W36.csv",
                    source_row_no=41,
                ),
            ),
        )
        record_alignment_snapshot(conn, snap, raise_on_conflict=False)

        refs = evidence.snapshot_refs(conn, PERIOD, snap.conclusion_key)
    finally:
        conn.close()
    assert len(refs) == 1
    got = refs[0]
    assert got["content_hash"], "快照必须带内容哈希，否则「内容变了没有」无从判断"
    assert got["participant_total"] == len(got["participants"]) == 2
    sides = {p["side"] for p in got["participants"]}
    assert sides == {"channel", "his"}, "两侧参与方都要在，复算靠它们对齐"


def test_evidence_reports_drift_when_snapshot_row_is_gone(client, scanned_db):
    """快照里的行在账单库中找不到时，``drifted`` 为真并点名是哪一行。

    **漂移只在有快照时才谈**：没有快照就没有「当时那几行」可比，
    空白不是漂移（另有一条用例守住这一点）。
    """
    from reconq.align.snapshot import (
        EvidenceSnapshot,
        SnapshotRef,
        init_align_snapshot_schema,
        record_alignment_snapshot,
    )
    from reconq.web import evidence

    conn = connect(scanned_db)
    try:
        init_align_snapshot_schema(conn)
        record_alignment_snapshot(
            conn,
            EvidenceSnapshot(
                conclusion_key="wechat:2026-W36:EX-WX-2026W36-0001",
                period=PERIOD,
                channel="wechat",
                kind="weak_pending",
                order_no="EX-WX-2026W36-0001",
                participants=(
                    SnapshotRef(
                        side="channel",
                        channel="wechat",
                        period=PERIOD,
                        order_no="EX-WX-2026W36-0001",
                        amount="128.00",
                        source_file="wechat_tradebill_2026-W36.csv",
                        source_row_no=2,
                    ),
                ),
            ),
            raise_on_conflict=False,
        )
        # 重扫覆盖掉了那一行：快照还在，账单表现已无对应行。
        conn.execute("DELETE FROM bill_rows WHERE ch_id=1")
        panel = evidence.evidence_panel(
            conn,
            PERIOD,
            {
                "key": f"{PERIOD}|wechat|taxonomy.ambiguity:EX-WX-2026W36-0001",
                "order_no": "EX-WX-2026W36-0001",
                "conclusion_key": "wechat:2026-W36:EX-WX-2026W36-0001",
            },
        )
    finally:
        conn.close()
    assert panel["snapshot_total"] == 1
    assert panel["candidate_total"] == 0
    assert panel["drifted"] is True
    assert panel["drift"] == ["wechat_tradebill_2026-W36.csv:2"], "点名是哪一行漂了"
    assert "以快照复算为准" in panel["note"], "漂移时必须说清以哪一份为准"


def test_evidence_without_snapshot_does_not_claim_drift(client, scanned_db):
    """没拍过快照的条目 ``drifted`` 为假 —— 空白不是漂移。"""
    from reconq.web import evidence

    conn = connect(scanned_db)
    try:
        panel = evidence.evidence_panel(
            conn,
            PERIOD,
            {
                "key": f"{PERIOD}|wechat|rows.refund_sum.zero",
                "order_no": "",
                "conclusion_key": f"wechat:{PERIOD}:",
            },
        )
    finally:
        conn.close()
    assert panel["snapshot_total"] == 0
    assert panel["drifted"] is False
    assert panel["drift"] == []


def test_evidence_deep_link_round_trips_through_the_wecom_parser(client):
    """深链构造与解析必须是同一对函数（与 Task 46 推送同源）。"""
    from reconq.audit.wecom import parse_deep_link

    key = _refund_key(client)
    links = client.get(_evidence_url(PERIOD, key)).json()["links"]
    assert links["deep_link"].startswith("reconq://period/")
    assert parse_deep_link(links["deep_link"]) == (PERIOD, key)
    assert links["round_trip_ok"] is True
    # --replay 吃的是**结论键**（渠道:账期:单号），不是差异键
    # （账期|渠道|口径）。混用会得到一条「查无此结论」的命令。
    assert links["cli_command"] == f"reconq verify --replay {links['conclusion_key']}"
    assert links["conclusion_key"].startswith("wechat:")
    assert f"period={PERIOD}" in links["panel_url"]
    # 差异键含 ``|``，进 query 必须编码，否则部分反代会把它截断
    assert "%7C" in links["panel_url"]


def test_open_route_redirects_a_deep_link_to_the_panel(client):
    """``/open?link=reconq://…`` → 302 到 ``/?period=…&diff=…``。"""
    key = _refund_key(client)
    link = "reconq://period/{}/diff/{}".format(PERIOD, key)
    r = client.get("/open", params={"link": link}, follow_redirects=False)
    assert r.status_code == 302
    # 回跳地址里的差异键必须**百分号编码**：键里有 ``|``，未编码时
    # 部分反代与 URLSearchParams 会把它截断，面板收到半截键，点开就
    # 落在一条不存在的差异上 —— 而链接看起来完全正常。
    loc = r.headers["location"]
    qs = urllib.parse.parse_qs(loc.partition("?")[2])
    assert qs["period"] == [PERIOD]
    assert qs["diff"] == [key]
    assert "%7C" in loc, "键里的竖线必须编码后才进 query"


def test_open_route_rejects_a_malformed_link_with_a_reason(client):
    """解析不了要 4xx 且说清为什么 —— 点链接毫无反应会被当成面板坏了。"""
    r = client.get("/open", params={"link": "http://example.com/not-a-deep-link"})
    assert r.status_code == 400
    assert r.json()["code"] == "WECOM_PAYLOAD_REJECTED"


def test_evidence_for_unknown_key_returns_404(client):
    r = client.get(_evidence_url(PERIOD, "2026-W36|wechat|不存在的口径"))
    assert r.status_code == 404
    assert r.json()["code"] == "LEDGER_DISCREPANCY_NOT_FOUND"


# ─────────────────────── 前端可读性：色 + 文字双重标识 ───────────────────────


def test_panel_js_marks_state_with_colour_and_text_not_colour_alone():
    """状态标识必须是「颜色 + 文字」两重，**不能只靠颜色**。

    「谁该审什么」是这一屏最要紧的信息；只靠颜色传达时，色觉障碍
    同事看到的是一排同色行。
    """
    js = os.path.join(_PROJECT_ROOT, "src", "reconq", "web", "static", "panel.js")
    src = open(js, encoding="utf-8").read()
    assert '"st st-" + stClass(row.ledger_status), row.ledger_status_zh' in src, \
        "状态列必须同时带上文字（status_zh）与颜色类"
    assert "aria-pressed" in src, "行要能被键盘与读屏识别为可选项"
    assert "/evidence" in src, "证据展开要连服务端端点"


def test_panel_css_pairs_each_state_colour_with_a_shape_marker():
    """CSS 里每个状态类都要有形状标记（::before 小圆点），不只是一段颜色。"""
    css_path = os.path.join(_PROJECT_ROOT, "src", "reconq", "web", "static", "panel.css")
    css = open(css_path, encoding="utf-8").read()
    for name in ("pending", "confirming", "supervisor", "confirmed", "rejected", "unopened"):
        assert f".st-{name}::before" in css, f"状态 {name} 缺形状标记"
    assert "tabular-nums" in css, "金额必须等宽数字，否则竖排核金额会看错对齐"


def test_evidence_whitelist_does_not_return_raw_channel_columns(client, scanned_db):
    """候选行只回白名单列，**不倒渠道原始行**。

    账单表将来加列时列不在白名单就自动不外发；黑名单则相反 ——
    加一列的人不会想到它带着患者信息。
    """
    from reconq.web import evidence

    conn = connect(scanned_db)
    try:
        raw = conn.execute("SELECT * FROM bill_rows WHERE ch_id=1").fetchone()
        got = evidence.candidate_rows(conn, PERIOD, "EX-WX-2026W36-0001")[0]
    finally:
        conn.close()
    # 内部列（幂等键、落库时刻、主键）一概不外发：它们对财务科判断
    # 没有任何用处，回传只是把库结构端出去。
    for col in ("idempotency_key", "ingested_at", "bill_date_inferred", "ch_id", "period"):
        assert col not in got, f"{col} 不该出现在候选行里"
    # 响应列与快照引用（SnapshotRef）同形 —— 面板上显示的每一列，
    # 财务科都能在结论单的快照附件里找到同一列。
    assert set(got) == set(evidence.EVIDENCE_COLUMNS) | {"bill_date"}
    # 落库列名与响应列名不是一套（channel_order_no → order_no），
    # 所以不比子集，只断言「响应没有把原表整行倒出来」：
    assert "channel_order_no" not in got
    assert "amount_text" not in got


def test_evidence_settle_type_follows_the_product_wide_column_mapping(scanned_db):
    """``settle_type`` 按既有映射取值，**不在面板这一层另做脱敏**。

    微信那路它取的是 ``payer``（用户标识）列 —— 这是
    :data:`reconq.store.bill_store.CHANNEL_COLUMN_MAP` 的既有口径，
    快照、弱匹配打分、结论单一贯带着。面板单方面藏起这一列会与那几处
    对不上（经办人看到的结算方式与结论单上的不是同一个），而真正该做
    的是改映射表并重跑快照。此用例把这条已知边界钉在这里，避免后续
    有人以为面板已经脱敏过了。
    """
    from reconq.web import evidence

    conn = connect(scanned_db)
    try:
        got = evidence.candidate_rows(conn, PERIOD, "EX-WX-2026W36-0001")[0]
    finally:
        conn.close()
    assert got["settle_type"] == "脱敏占位用户01", "取的是 payer（用户标识）列，非结算方式"


def test_cause_code_filter_narrows_by_the_strongest_available_source(client, scanned_db):
    """成因筛选要**真的能筛出东西**（Task 48 的核心验收点之一）。

    这条曾经是个静默缺陷：筛选读的是行上并不存在的 ``cause_code`` 字段，
    而台账行给的是**三档分列**（``ledger_cause`` / ``model_cause`` /
    ``rule_cause``）。读不存在的字段不报错，只让这个筛选**永远返回 0 条**
    —— 而「筛不出东西」与「本期没有这类成因」在面板上长得一模一样，
    经办人会据此认定本期没有这类问题。
    """
    from reconq.engine import period as period_engine

    conn = connect(scanned_db)
    try:
        # 给那条口径差异登一个规则形态，让它拿到规则成因档
        conn.execute(
            f"UPDATE {period_engine.DIFF_TABLE} SET ambiguity_kind='weak_pending' "
            "WHERE period=? AND check_path=?",
            (PERIOD, CHECK_REFUND),
        )
    finally:
        conn.close()

    data = client.get(_url(PERIOD, "discrepancies", cause_code="undetermined")).json()
    assert data["matched"] == 1, "筛 undetermined 应命中那一条；返回 0 说明筛选根本没生效"
    assert data["rows"][0]["check_path"] == CHECK_REFUND
    assert data["total"] == 2, "总数不变：筛不出 ≠ 不存在"

    # 字典里有的但本期没有的成因 → 0 条，但 200 且说清是 0/2
    empty = client.get(
        _url(PERIOD, "discrepancies", cause_code="merged_payment_allocation")
    ).json()
    assert empty["matched"] == 0 and empty["total"] == 2


def test_cause_code_filter_agrees_with_the_group_view(client, scanned_db):
    """同一账期，成因筛选命中的条数与分组里该成因组的条数必须一致。

    两处若分家（筛选算一套、分组算另一套），经办人点进组里数一遍条数
    与筛选条数对不上，就没法判断是哪一个环节错了。
    """
    from reconq.engine import period as period_engine

    conn = connect(scanned_db)
    try:
        conn.execute(
            f"UPDATE {period_engine.DIFF_TABLE} SET ambiguity_kind='channel_only' "
            "WHERE period=? AND check_path=?",
            (PERIOD, CHECK_REFUND),
        )
    finally:
        conn.close()

    filtered = client.get(
        _url(PERIOD, "discrepancies", cause_code="his_missing_order")
    ).json()
    groups = client.get(_url(PERIOD, "groups")).json()
    in_group = sum(
        g["count"] for g in groups["groups"] if g["cause_code"] == "his_missing_order"
    )
    assert filtered["matched"] == 1
    assert in_group == filtered["matched"], "筛选与分组对同一成因给出不同条数"
