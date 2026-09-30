"""中栏差异端点：筛选、分组、待办（Task 49）。

与 ``tests/test_web_filters.py`` 的分工：那个文件验的是
:mod:`reconq.web.views` 这一层的筛选判据；本文件验的是**过了 HTTP
之后**的那一层 —— query 参数名有没有真的对上、非法值是不是 4xx 带错误
码、合计是不是在筛选**之后**重算。

样例账期只有两条真实差异，所以「筛出几条」是能逐条核的：

======================  ==========  =========================
口径                     差额        处置状态（未开单时）
======================  ==========  =========================
``rows.refund_sum.zero``   86.00      未开单
``transaction_id.not_empty``  （空）   未开单
======================  ==========  =========================

第二条**没有金额**（上游压根没给差额），它正是「按金额筛时会被排除掉的
那一条」—— 排除掉不计数的话，筛出 1 条而台账上有 2 条，经办人无从知道
少的去了哪。
"""

from __future__ import annotations

from decimal import Decimal

#: 样例账期里两条真实差异的口径。
KEY_REFUND = "2026-W36|wechat|rows.refund_sum.zero"
KEY_NO_AMOUNT = "2026-W36|wechat|transaction_id.not_empty"


def _body(client, period, **params):
    r = client.get(f"/api/period/{period}/discrepancies", params=params)
    assert r.status_code == 200, f"筛选请求被拒 {r.status_code}：{r.text[:300]}"
    return r.json()


def test_unfiltered_returns_every_discrepancy_with_five_counts(client, period):
    """无筛选时五个计数键齐全，且 ``matched == total == 行数``。

    五个键是**无条件补齐**的：差异台账尚未建表时端点走提前返回路径，
    那一路不带这些键，前端读 ``total`` 拿到 undefined 会显示「0 条」
    而不是「还没扫」—— 两者在排障时完全相反。
    """
    body = _body(client, period)
    for key in (
        "matched", "total", "unmatched", "dropped_unknown_amount",
        "dropped_amount_missing", "dropped_amount_unparsable",
    ):
        assert key in body, f"响应缺计数键 {key}"
    assert body["matched"] == body["total"] == 2
    assert body["unmatched"] == 0
    assert body["filter"]["active"] is False
    assert {r["key"] for r in body["rows"]} == {KEY_REFUND, KEY_NO_AMOUNT}


def test_amount_filter_recomputes_total_and_counts_the_excluded(client, period):
    """金额筛选后**合计与条数一起收窄**，被排除的那条要被计数。

    「≥100」筛出 0 条、合计 0.00：若合计在筛选前算，界面顶端仍显示
    86.00，财务科按它去核一条空清单，怎么都对不上，而界面上没有任何
    地方提示这两个数字口径不同。
    """
    body = _body(client, period, amount_min="100")
    assert body["matched"] == 0
    assert body["rows"] == []
    assert Decimal(body["total_amount"]) == Decimal("0.00")
    # 无金额那条必须计入「金额不可知」，不能静默消失
    assert body["dropped_unknown_amount"] == 1
    assert body["dropped_amount_missing"] == 1
    assert body["dropped_amount_unparsable"] == 0, "空差额是「上游没给」，不是「给了但脏」"

    upper = _body(client, period, amount_max="100")
    assert upper["matched"] == 1
    assert Decimal(upper["total_amount"]) == Decimal("86.00")
    assert upper["dropped_unknown_amount"] == 1, "上限也一样要计数，不能只在某一档算"


def test_inverted_amount_range_is_rejected(client, period):
    """区间反了给 4xx 带错误码，而不是返回空列表。

    永远筛不出东西的条件被当成答案时，症状是「本期没有差异」。
    """
    r = client.get(
        f"/api/period/{period}/discrepancies", params={"amount_min": "100", "amount_max": "10"}
    )
    assert r.status_code == 400
    assert r.json()["code"] == "WEB_FILTER_AMOUNT_INVALID"


def test_non_numeric_amount_is_rejected(client, period):
    r = client.get(f"/api/period/{period}/discrepancies", params={"amount_min": "一百元"})
    assert r.status_code == 400
    assert r.json()["code"] == "WEB_FILTER_AMOUNT_INVALID"


def test_channel_filter_selects_only_that_channel(client, period):
    """按渠道筛：命中微信时两条都在，换成支付宝一条都没有。

    「筛不出东西」与「本期这路没有差异」在面板上长得一模一样，所以
    这里要同时钉住**命中与落空**两侧。
    """
    body = _body(client, period, channel="wechat")
    assert body["matched"] == 2
    assert {r["channel"] for r in body["rows"]} == {"wechat"}
    assert body["filter"]["channel"] == "wechat"

    empty = _body(client, period, channel="alipay")
    assert empty["matched"] == 0
    assert empty["total"] == 2, "筛不中时 total 仍是台账总数，unmatched 才解释差在哪"
    assert empty["unmatched"] == 2


def test_state_filter_and_illegal_state_value(client, period, scanned_db):
    """合法状态名筛得到、写错的状态名给 4xx 带错误码。

    写错状态名（``待审`` 而不是 ``待确认``）静默返回空列表时，经办人
    看到的是「本期没有待审的差异」—— 他会以为已经审完了。
    """
    from reconq.ledger.flow import open_discrepancy
    from reconq.store.schema import connect

    conn = connect(scanned_db)
    try:
        open_discrepancy(conn, period=period, channel="wechat",
                         check_path="rows.refund_sum.zero", diff_amount="86.00")
    finally:
        conn.close()

    assert _body(client, period, state="待归因")["matched"] == 1
    # 剩下那条仍显示「未开单」—— 中栏真实存在的状态，不给它单列一组
    # 就意味着筛不出来，而经办人恰恰最想先看这一组
    assert _body(client, period, state="未开单")["matched"] == 1
    assert _body(client, period, state="已确认")["matched"] == 0

    r = client.get(f"/api/period/{period}/discrepancies", params={"state": "待审"})
    assert r.status_code == 400
    assert r.json()["code"] == "WEB_FILTER_STATE_UNKNOWN"


def test_illegal_channel_and_cause_are_rejected(client, period):
    for params, code in (
        ({"channel": "wechatpay"}, "WEB_FILTER_CHANNEL_UNKNOWN"),
        ({"cause_code": "渠道没传"}, "WEB_FILTER_CAUSE_UNKNOWN"),
    ):
        r = client.get(f"/api/period/{period}/discrepancies", params=params)
        assert r.status_code == 400, f"{params} 竟返回 {r.status_code}"
        assert r.json()["code"] == code


def test_settled_row_stays_visible_with_its_final_state(client, period, scanned_db):
    """办结的差异**留在中栏**、并显示终态与结论，而不是消失。

    办结后行不见了，财务科会以为这条从来没发生过；而它恰恰是结论单上
    要签字的那一条。中栏要做的是**标出已办结**，不是把它藏起来。
    """
    import urllib.parse

    from reconq.ledger.flow import open_discrepancy
    from reconq.store.schema import connect

    # 处置单得先由开单动作建出来 —— 中栏那两行是**差异**，不是**处置单**，
    # 差异在、处置单还没开时 decide 端点给 404 而不是替它开一张。
    conn = connect(scanned_db)
    try:
        open_discrepancy(conn, period=period, channel="wechat",
                         check_path="rows.refund_sum.zero", diff_amount="86.00")
    finally:
        conn.close()

    for payload, expect in (
        ({"decision": "confirm", "operator": "E-01", "role": "handler",
          "comment": "渠道延迟入账，次日已补", "expected_version": 0}, "待确认"),
        ({"decision": "confirm", "operator": "E-02", "role": "reviewer",
          "comment": "复核无误", "expected_version": 1}, "已确认"),
    ):
        r = client.post(
            f"/api/discrepancy/{urllib.parse.quote(KEY_REFUND, safe='')}/decide", json=payload
        )
        assert r.status_code == 200, r.text
        assert r.json()["after"] == expect

    row = next(r for r in _body(client, period)["rows"] if r["key"] == KEY_REFUND)
    assert row["ledger_status"] == "已确认"
    assert row["conclusion"] == "渠道延迟入账，次日已补"
    assert row["handler"] == "E-01" and row["reviewer"] == "E-02"
    # 已确认的单子上再给「通过/驳回」按钮，就是让人点一个必报错的东西
    assert [a["action"] for a in row["actions"]] == ["withdraw"]

    settled = client.get(f"/api/period/{period}/disposition").json()["settled"]
    assert [s["status"] for s in settled] == ["已确认"]
    assert settled[0]["conclusion"] == "渠道延迟入账，次日已补"


def test_groups_endpoint_matches_unfiltered_total(client, period):
    """分组条数之和 = 未筛选时的条数。

    两处数字对不上时，经办人会先信「按成因分组」那一张（它更接近交办
    单的样子），而那一张才是错的。
    """
    body = client.get(f"/api/period/{period}/groups").json()
    assert body["period"] == period
    assert body["group_count"] == len(body["groups"])
    total = _body(client, period)["total"]
    assert sum(g["count"] for g in body["groups"]) == total
    for group in body["groups"]:
        assert group["cause_zh"], "分组要带中文成因名"
        assert group["source_zh"], "每组要写明成因是谁说的（模型 / 规则 / 口径差异）"
    # 分组端点**不带筛选参数**：带筛选会让「组内条数」与「筛出来的条数」
    # 两套数字并存
    assert "filter" not in body


def test_todo_endpoint_splits_by_role(client, period, scanned_db):
    """待办按岗位分列，且非法岗位给 4xx。

    面板上「我是经办 / 我是审核 / 我是主管」三个下拉给的是**不同条数**：
    三个数字相同时这个下拉就是个摆设，而它恰恰是让人以为自己审完了的
    那个控件。
    """
    from reconq.ledger.flow import open_discrepancy
    from reconq.store.schema import connect

    conn = connect(scanned_db)
    try:
        open_discrepancy(conn, period=period, channel="wechat",
                         check_path="rows.refund_sum.zero", diff_amount="86.00")
    finally:
        conn.close()

    handler = client.get(f"/api/period/{period}/todo", params={"role": "handler"}).json()
    reviewer = client.get(f"/api/period/{period}/todo", params={"role": "reviewer"}).json()
    assert handler["count"] == 1, "经办看得到待归因"
    assert reviewer["count"] == 0, "还没提交结论时审核岗没有待办"
    assert Decimal(handler["amount"]) == Decimal("86.00")
    assert handler["oldest_key"] == KEY_REFUND, "最老待办要能点回具体那一条"
    # 没有待办时给「—」而不是 0 小时：0 小时读起来像「刚刚才出现」，
    # 而真相是这一档一条都没有
    assert reviewer["oldest_hours"] is None and reviewer["oldest_text"] == "—"

    r = client.get(f"/api/period/{period}/todo", params={"role": "老板"})
    assert r.status_code == 400
    assert r.json()["code"] == "LEDGER_ROLE_INVALID"


def test_response_never_echoes_raw_bill_lines(client, period):
    """中栏不回吐账单原文。

    交办单要发给渠道方，而证据只给「文件名:行号」就够对方自己回查；
    把整行原文吐回前端只多一条患者可识别的泄露路径。
    """
    import json

    blob = json.dumps(_body(client, period), ensure_ascii=False)
    for raw_field in ("交易时间", "商户号", "payer", "openid", "身份证"):
        assert raw_field not in blob, f"差异列表里出现了账单原始列 {raw_field}"


def test_vocab_endpoint_is_the_single_source_of_enums(client):
    """词表只有服务端这一份，且覆盖筛选下拉要用的全部枚举。

    前端硬编码一份状态名，那一档就永远选不到，而经办人会以为「这类成因
    本期没出现」。
    """
    from reconq.align.causes import CAUSES
    from reconq.sources.registry import CHANNELS
    from reconq.web.views import FILTERABLE_STATES

    vocab = client.get("/api/vocab").json()
    assert list(vocab["states"]) == list(FILTERABLE_STATES)
    assert vocab["channels"] == list(CHANNELS)
    assert {c["code"] for c in vocab["causes"]} == set(CAUSES)
    assert all(c["zh"] for c in vocab["causes"]), "成因码必须带中文解释"
    assert {r["code"] for r in vocab["roles"]} == {"handler", "reviewer", "supervisor"}
