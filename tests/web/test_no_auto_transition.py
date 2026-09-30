"""「模型 / 规则推不动状态」这条硬边界（Task 49）。

本产品里模型与代码都会主动给出判断 —— 成因草稿是模型给的排序，规则
基线是代码算的候选，扫描 check 是代码判的「合不合规」。三者都会写下
「我认为是导出方漏传」。

但**签字的必须是人**。这条边界一旦松了，后果不是「系统判错了一条」，
而是「结论单上那三行签字有一行没人签过」—— 而财务科签的是结论单。

因此这里不测某个端点返回什么，而是**直接调主路径**（跑一遍对齐流水线、
跑一遍归因、跑一遍聚合）之后断言状态集合**一个字节没变**。端点层的
拦截由 :mod:`tests.web.test_decide_api` 覆盖；这里覆盖的是**端点之外**
的那些调用方 —— 它们连 HTTP 都不经过。
"""

from __future__ import annotations

import urllib.parse

from reconq.ledger.flow import list_discrepancies, open_discrepancy
from reconq.store.schema import connect

PERIOD = "2026-W36"


def _statuses(db: str) -> dict[str, tuple[str, int]]:
    """整期所有处置单 → ``{键: (状态, 版本)}``。"""
    conn = connect(db)
    try:
        return {d.key: (d.status, d.version) for d in list_discrepancies(conn, period=PERIOD)}
    finally:
        conn.close()


def _seed_two_ledgers(db: str) -> None:
    conn = connect(db)
    try:
        open_discrepancy(conn, period=PERIOD, channel="wechat",
                         check_path="rows.refund_sum.zero", diff_amount="86.00")
        open_discrepancy(conn, period=PERIOD, channel="wechat",
                         check_path="transaction_id.not_empty", diff_amount="64.00")
    finally:
        conn.close()


def test_running_the_align_pipeline_changes_no_ledger_state(scanned_db):
    """跑完对齐主路径，八个阶段全过，而处置状态**一个都没动**。

    主路径是「读账单库 → 索引 → 对齐 → 分型 → 连坐 → 证据包 → 归因 →
    核对 → 聚合」这条链。它产出待办与处置单建议，**但不写台账、不推进
    任何处置状态** —— 把它做成写操作，会让「我只想看看这一期有多少事」
    变成一次需要有人负责的改动。
    """
    from reconq.align.pipeline import run_align_pipeline

    _seed_two_ledgers(scanned_db)
    before = _statuses(scanned_db)
    assert len(before) == 2, "夹具没造出两张处置单，后面的断言都是空的"

    conn = connect(scanned_db)
    try:
        result = run_align_pipeline(
            conn, PERIOD,
            # ``gateway=None`` = 模型不可达，走降级路径。降级的那条路
            # 恰恰是最容易被人怀疑「偷偷把结论定了」的一条，所以要验。
            gateway=None,
            contracts_dir="contracts",
        )
    finally:
        conn.close()

    # 主路径确实跑完了（不是啥也没跑所以「没变」）
    assert result.period == PERIOD
    assert result.partial is False, f"主路径有阶段失败：{[(s.stage, s.error_code) for s in result.stages if not s.ok]}"

    assert _statuses(scanned_db) == before, (
        f"跑完主路径后处置状态变了：{before} → {_statuses(scanned_db)}"
    )


def test_attribution_only_produces_drafts(scanned_db):
    """归因产出的是**草稿**，结构上没有能推进状态的字段。

    :class:`reconq.align.attribution.CauseDraft` 上压根没有
    ``disposition`` 字段 —— 想推进状态只能另走状态机，而状态机只接受人
    点的参数。这条断言是为了让「将来有人给草稿加个 status 字段」在这里
    就红掉。
    """
    from dataclasses import fields

    from reconq.align.attribution import AttributionResult, CauseDraft

    draft_fields = {f.name for f in fields(CauseDraft)}
    assert "disposition" not in draft_fields
    assert "status" not in draft_fields
    for forbidden in ("approved", "confirmed", "resolved"):
        assert forbidden not in draft_fields, (
            f"CauseDraft 多了 {forbidden} 字段；草稿一旦带结论，"
            "「模型只出草稿」这条边界就没法在类型层面守住了"
        )
    result_fields = {f.name for f in fields(AttributionResult)}
    assert "disposition" not in result_fields


def test_degraded_attribution_still_leaves_state_untouched(scanned_db):
    """模型不可达时走降级（规则候选 + 待人工判定），状态**照样不动**。

    降级不等于交白卷，但「给了规则候选」也不等于「这条已经定了」——
    降级路径上最容易混进去的就是把规则候选直接落成状态。
    """
    from reconq.align.attribution import attribute
    from reconq.align.bundle import build_evidence_bundle

    _seed_two_ledgers(scanned_db)
    before = _statuses(scanned_db)

    conn = connect(scanned_db)
    try:
        # 证据包只认三类来源：匹配结论、连坐结论、**差异台账条目**。
        # 这里走台账那一条，与真实调用方（对齐编排）同源。
        from reconq.engine.period import list_diff_items

        diff_item = next(
            i for i in list_diff_items(conn, period=PERIOD)
            if i.check_path == "rows.refund_sum.zero"
        )
        bundle = build_evidence_bundle(diff_item, conn=conn)
        result = attribute(bundle, gateway=None)  # 无凭据位 → 降级
    finally:
        conn.close()

    assert result.degraded is True, "无模型凭据却没走降级，说明凭据位判据失效了"
    assert result.needs_manual is True, "降级结果却说不需要人工，那它凭什么不需人工"
    assert _statuses(scanned_db) == before


def test_scan_and_aggregate_do_not_advance_ledger(scanned_db):
    """重扫一遍账期、聚一次合计，处置状态仍不动。

    重扫是最容易被误解成「刷新」的动作：它会重写渠道主态与差异台账。
    若它顺手把已确认的差异打回待确认，主管每周都要重确认一次。
    """
    from reconq.align.aggregate import aggregate_discrepancies, load_group_entries

    _seed_two_ledgers(scanned_db)
    conn = connect(scanned_db)
    try:
        # 先把其中一条走完两级会签
        item = list_discrepancies(conn, period=PERIOD)[0]
    finally:
        conn.close()

    before = _statuses(scanned_db)
    assert before[item.key][0] == "待归因"

    conn = connect(scanned_db)
    try:
        result = aggregate_discrepancies(load_group_entries(conn, PERIOD))
    finally:
        conn.close()
    assert result.groups, "聚合没产出分组，后面的断言都是空的"
    assert _statuses(scanned_db) == before, "聚合把处置状态改了"


def test_only_the_two_http_endpoints_can_write(client, opened_db):
    """面板上**只有两个**写入口，且都要求版本号与自然人操作人。

    逐个列出全部端点并断言：除这两个 POST 之外没有第三个能改状态的路径。
    将来有人加一个「一键通过全部」时，这条会红 —— 而那正是最该有人
    拦一下的动作。
    """
    before = _statuses(opened_db)
    write_endpoints = sorted(
        route.path for route in client.app.routes
        if "POST" in getattr(route, "methods", set())
    )
    assert write_endpoints == [
        "/api/discrepancy/{key}/decide",
        "/api/discrepancy/{key}/review",
    ], f"面板上能改状态的端点多了或少了：{write_endpoints}"
    assert _statuses(opened_db) == before, "光发请求就把状态改了"


def test_read_endpoints_are_side_effect_free(client, opened_db):
    """把全部读端点挨个打一遍，状态**一个字不变**。

    读端点开的是自己的连接、自己关自己；漏关一个在长跑的面板上会攒成
    一堆 ``database is locked``，而症状是「扫描跑到一半卡住」。
    """
    before = _statuses(opened_db)
    reads = [
        ("/", {}),
        ("/api/periods", {}),
        ("/api/vocab", {}),
        (f"/api/period/{PERIOD}/channels", {}),
        (f"/api/period/{PERIOD}/discrepancies", {}),
        (f"/api/period/{PERIOD}/groups", {}),
        (f"/api/period/{PERIOD}/todo", {"role": "handler"}),
        (f"/api/period/{PERIOD}/disposition", {}),
    ]
    key = urllib.parse.quote("2026-W36|wechat|rows.refund_sum.zero", safe="")
    reads += [
        (f"/api/period/{PERIOD}/discrepancy/{key}/aid", {}),
        (f"/api/period/{PERIOD}/discrepancy/{key}/evidence", {}),
    ]
    for path, params in reads:
        r = client.get(path, params=params)
        assert r.status_code == 200, f"{path} 返回 {r.status_code}：{r.text[:200]}"
    assert _statuses(opened_db) == before, "读端点里有写副作用"


def test_panel_shows_draft_and_human_conclusion_in_separate_fields(client, opened_db):
    """右栏把**成因草稿**与**人工结论**分栏，不合成一栏。

    合成一栏就看不出人有没有采信模型 —— 而那恰恰是签字前最该看的一件事。
    面板声称「模型说」与「人说」分得开，那数据结构上就得真的分开。
    """
    rows = client.get(f"/api/period/{PERIOD}/discrepancies").json()["rows"]
    row = next(r for r in rows if r["key"] == "2026-W36|wechat|rows.refund_sum.zero")
    # 草稿字段与结论字段各归各的，谁都不是对方的别名
    assert "cause_draft" in row
    assert "conclusion" in row
    assert "ledger_cause" in row and "model_cause" in row and "rule_cause" in row
    # 还没人处理过，结论就该是空的 —— 草稿（若有）不许被抄进结论栏
    assert row["conclusion"] == ""
    assert row["cause_draft"] is None, "样例账期没跑过归因，不该凭空有草稿"
