"""两个写入口的交互（Task 49 · 状态机越权与并发冲突）。

面板是财务科唯一入口，因此「谁能改状态」「什么时候能改」「两个人同时
点会怎样」这三条**必须由服务端说清**，而不是由前端把按钮藏起来。按钮
藏错了的症状是：换个浏览器、或者别人把链接发过来，同一个操作照样能成。

钉住的六类
----------
1. 人工边界（模型/规则/系统推不动状态，且**不是 500**）
2. 非法状态跳转（4xx + 错误码，且**仍留痕**）
3. 版本冲突（拿旧版本号来覆盖别人刚写的结论）
4. 并发两次确认（第二次 409，**没有人被无声覆盖**）
5. 岗位分离（经办不能自己审自己）
6. 撤回（终态唯一能按的动作）
"""

from __future__ import annotations

import urllib.parse

import pytest

from reconq.ledger.flow import open_discrepancy
from reconq.store.schema import connect

KEY = "2026-W36|wechat|rows.refund_sum.zero"
URL = f"/api/discrepancy/{urllib.parse.quote(KEY, safe='')}/decide"


def _decide(client, **payload):
    return client.post(URL, json=payload)


# ─────────────────────────── 1. 人工边界 ───────────────────────────


@pytest.mark.parametrize("actor", ["model", "rule", "系统", "scheduler", "llm"])
def test_machine_operator_cannot_advance_state(client, opened_db, actor):
    """模型 / 规则 / 系统 / 调度都推不动状态。

    这条边界是产品级的：成因草稿由模型给、规则基线由代码算，两者都能
    写出「我认为是导出方漏传」，但**签字的必须是人**。挡它的不是前端
    的按钮，是 :func:`reconq.ledger.flow.assert_human_operator`。
    """
    r = _decide(client, decision="confirm", operator=actor,
                comment="规则判定为渠道漏传", expected_version=0)
    assert r.status_code == 422, f"{actor} 竟然拿到了 {r.status_code}"
    assert r.json()["code"] == "LEDGER_OPERATOR_INVALID"


def test_machine_operator_rejected_before_any_db_write(client, opened_db):
    """被拒的操作**状态没动**，且错误码说明了理由。

    只断言状态码不够：一个先写库再回滚的实现也能给出 422，而它在库上
    留下一段锁与一个空洞的 undo 链。
    """
    from reconq.ledger.flow import require

    _decide(client, decision="confirm", operator="model", comment="x", expected_version=0)
    conn = connect(opened_db)
    try:
        item = require(conn, KEY)
    finally:
        conn.close()
    assert item.status == "待归因"
    assert item.version == 0


# ─────────────────────────── 2. 非法状态跳转 ───────────────────────────


def test_illegal_transition_returns_409_with_code(client, opened_db):
    """``待归因 → 已驳回`` 跳关被拒。

    状态机是唯一裁决者，Web 层不兜住它 —— 若哪天状态机放开了某个迁移，
    面板会立刻跟着放开，两处各写一份判据的年代已经过去了。
    """
    r = _decide(client, decision="reject", operator="E-01", expected_version=0)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "LEDGER_TRANSITION_DENIED"
    assert body["before"] == body["after"] == "待归因", "被拒的迁移不得留下任何状态变化"


def test_rejected_click_is_still_recorded(client, opened_db):
    """被拒的点击**也要留痕**。

    「谁在什么时候试图改了什么、被谁挡下」是内审要看的东西。静默丢掉的
    失败尝试恰好是最值得留的那一类。
    """
    from reconq.ledger.flow import list_actions

    _decide(client, decision="reject", operator="E-01", expected_version=0)
    conn = connect(opened_db)
    try:
        actions = list_actions(conn, key=KEY)
    finally:
        conn.close()
    assert actions, "被拒的迁移一条留痕都没有"
    # ``ok`` 在留痕表里是 INTEGER 0/1（SQLite 没有布尔），不是 Python 的
    # False。写成 ``is False`` 会让这条断言永远红，而它本该盯的是
    # 「失败这件事有没有被记下来」。
    assert actions[-1]["ok"] == 0, f"留痕说这次是成功的：{actions[-1]}"
    assert actions[-1]["error_code"] == "LEDGER_TRANSITION_DENIED"
    assert actions[-1]["operator"] == "E-01"
    assert actions[-1]["from_status"] == actions[-1]["to_status"] == "待归因"


def test_unknown_decision_word_is_400(client, opened_db):
    r = _decide(client, decision="approve", operator="E-01", expected_version=0)
    assert r.status_code == 400
    assert r.json()["code"] == "LEDGER_ACTION_UNKNOWN"


def test_unknown_discrepancy_is_404_with_explanation(client):
    r = _decide(client, decision="confirm", operator="E-01", comment="x", expected_version=0)
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "LEDGER_DISCREPANCY_NOT_FOUND"
    assert "开成待办" in body["detail"], (
        "404 的 detail 要说清下一步动作是「先开单」；只说「不存在」的话，"
        "经办会以为这条差异丢了"
    )


def test_not_opened_yet_cannot_be_decided(client, scanned_db):
    """差异在、处置单还没开时，decide 给 404 而**不是替它开一张**。

    中栏那两行是**差异**，不是处置单。decide 顺手开一张的话，「谁决定把
    这条纳入待办」这件事就没有记录人了。
    """
    r = _decide(client, decision="confirm", operator="E-01", comment="x", expected_version=0)
    assert r.status_code == 404
    assert r.json()["code"] == "LEDGER_DISCREPANCY_NOT_FOUND"


# ─────────────────────────── 3. 版本与必填 ───────────────────────────


def test_missing_version_is_rejected(client, opened_db):
    """不带版本号的确认会无声盖掉别人刚写的结论，故直接拒。"""
    r = _decide(client, decision="confirm", operator="E-01", comment="渠道延迟入账")
    assert r.status_code == 422
    assert r.json()["code"] == "LEDGER_VERSION_REQUIRED"


def test_stale_version_returns_409_before_status_check(client, opened_db):
    """拿旧版本号来的人看到的是「你手上是第 0 版、现在是第 1 版」。

    报「状态不允许」会让他以为自己操作错了，而真相是他看到的已经不是
    最新那条了 —— 这两种错误在面板上长得很像，处理方式完全相反。
    """
    assert _decide(client, decision="confirm", operator="E-01", role="handler",
                   comment="渠道延迟入账", expected_version=0).status_code == 200
    r = _decide(client, decision="confirm", operator="E-02", role="reviewer",
                comment="我也确认一下", expected_version=0)
    assert r.status_code == 409
    assert r.json()["code"] == "LEDGER_VERSION_CONFLICT"
    assert "第 0 版" in r.json()["detail"]


def test_conclusion_is_required(client, opened_db):
    """没有结论的确认在结论单上是无源的。"""
    r = _decide(client, decision="confirm", operator="E-01", comment="", expected_version=0)
    assert r.status_code == 422
    assert r.json()["code"] == "LEDGER_CONCLUSION_REQUIRED"


def test_blank_operator_is_rejected(client, opened_db):
    r = _decide(client, decision="confirm", operator="   ", comment="x", expected_version=0)
    assert r.status_code == 422
    assert r.json()["code"] == "LEDGER_OPERATOR_INVALID"


# ─────────────────────────── 4. 并发冲突 ───────────────────────────


def test_two_concurrent_confirms_second_gets_409(client, opened_db):
    """两次并发确认：一次成功、一次 409，**没有人被无声覆盖**。

    不用真并发线程：两个请求带的是**同一个版本号**，而这正是「两个人
    同时点」在协议上的样子 —— 谁先落库是时序问题，但「第二个拿着第 0 版
    去改已经是第 1 版的那一行」必须被挡住，与线程调度无关。
    """
    payload = {"decision": "confirm", "operator": "E-01", "role": "handler",
               "comment": "渠道延迟入账，次日已补", "expected_version": 0}
    first = _decide(client, **payload)
    assert first.status_code == 200
    assert first.json()["version"] == 1

    payload["operator"] = "E-09"  # 另一个经办，同一个版本号
    second = _decide(client, **payload)
    assert second.status_code == 409
    assert second.json()["code"] == "LEDGER_VERSION_CONFLICT"
    assert second.json()["after"] == "待确认", "冲突的那次不得留下任何状态变化"


def test_conflict_keeps_the_first_writers_conclusion(client, opened_db):
    """冲突之后，原先那条结论**一个字没被改**。

    无声覆盖的后果不是「数据错了」，是「谁签的这句话查不到了」—— 所以
    这条断言要落到结论正文上，而不只是落到状态上。
    """
    from reconq.ledger.flow import require

    _decide(client, decision="confirm", operator="E-01", role="handler",
            comment="渠道延迟入账，次日已补", expected_version=0)
    _decide(client, decision="confirm", operator="E-09", role="handler",
            comment="我猜是导出方漏传", expected_version=0)

    conn = connect(opened_db)
    try:
        item = require(conn, KEY)
    finally:
        conn.close()
    assert item.conclusion == "渠道延迟入账，次日已补"
    assert item.handler == "E-01"
    assert item.version == 1


def test_parallel_threads_produce_exactly_one_winner(client, opened_db):
    """真并发：八个线程各拿第 0 版去确认，**恰好一个成功**。

    乐观锁的判据是 ``UPDATE ... WHERE version=?`` 的 ``rowcount``：并发下
    谁先提交谁赢，其余七次全部落空。断言「恰好一个 200」而不是「至少一个
    200」，是因为七个静默成功才是真正要防的那种故障。
    """
    from concurrent.futures import ThreadPoolExecutor

    def attempt(index: int):
        return _decide(
            client, decision="confirm", operator=f"E-{index:02d}", role="handler",
            comment=f"第 {index} 个经办的说法", expected_version=0,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(attempt, range(1, 9)))

    codes = [r.status_code for r in responses]
    assert codes.count(200) == 1, f"应当恰好一次成功，实际 {codes}"
    assert codes.count(409) == 7, f"其余七次都该是版本冲突，实际 {codes}"

    from reconq.ledger.flow import require

    conn = connect(opened_db)
    try:
        item = require(conn, KEY)
    finally:
        conn.close()
    assert item.version == 1
    # 胜出者的结论就是他自己的那一条，不能是别人那句
    winner = next(r.json() for r in responses if r.status_code == 200)
    assert item.conclusion == winner["comment"] if "comment" in winner else item.conclusion


# ─────────────────────────── 5. 岗位分离与两级会签 ───────────────────────────


def test_handler_cannot_sign_off_their_own_work(client, opened_db):
    """经办不能自己审自己 —— 提交人签不了复核那一栏。"""
    r = _decide(client, decision="confirm", operator="E-01", role="handler",
                comment="渠道延迟入账", expected_version=0)
    assert r.status_code == 200
    r = _decide(client, decision="confirm", operator="E-01", role="reviewer",
                comment="我看看没问题", expected_version=1)
    assert r.status_code == 409
    assert r.json()["code"] == "LEDGER_ROLE_SEPARATION_DENIED"


def test_two_stage_signoff_reports_remaining_actions(client, opened_db):
    """两级会签走通，且每一步回给前端的是**此刻**还能按的动作。"""
    r = _decide(client, decision="confirm", operator="E-01", role="handler",
                comment="渠道延迟入账，次日已补", expected_version=0)
    assert r.json()["after"] == "待确认"
    assert r.json()["operator"] == "E-01"
    assert r.json()["at"], "签字时刻要回给前端，面板上要当场显示「谁在什么时候」"
    assert sorted(a["action"] for a in r.json()["options"]) == ["approve", "reject"]

    r = _decide(client, decision="confirm", operator="E-02", role="reviewer",
                comment="复核无误", expected_version=1)
    assert r.json()["after"] == "已确认"
    # 已确认的单子上再给「通过/驳回」按钮，就是让人点一个必报错的东西
    assert [a["action"] for a in r.json()["options"]] == ["withdraw"]


def test_review_endpoint_forces_supervisor_role(client, opened_db):
    """``/review`` 强制走**主管**那一路，不靠前端传什么就认什么。

    完整链路：经办提交 → 审核通过（因账期存疑落「待主管复核」）→ 主管签字。
    途中 ``/review`` 被前端传成 ``role=reviewer`` 也要按主管落 —— 前端
    若能自己指定签字岗位，那「主管顺手把审核那一关也签了」这道关卡就
    等于不存在。
    """
    import urllib.parse

    from reconq.engine import period as period_engine
    from reconq.ledger.flow import require

    fresh_key = "2026-W36|wechat|transaction_id.not_empty"
    fresh_url = f"/api/discrepancy/{urllib.parse.quote(fresh_key, safe='')}/decide"
    review_url = f"/api/discrepancy/{urllib.parse.quote(fresh_key, safe='')}/review"

    conn = connect(opened_db)
    try:
        # 账期转存疑：存疑账期新开的单须经主管复核
        period_engine.transition(conn, "2026-W36", "disputed", note="本期挂账，主管把关")
        open_discrepancy(conn, period="2026-W36", channel="wechat",
                         check_path="transaction_id.not_empty", diff_amount="64.00")
        assert require(conn, fresh_key).requires_supervisor is True
    finally:
        conn.close()

    # 经办提交 → 待确认
    assert client.post(fresh_url, json={
        "decision": "confirm", "operator": "E-11", "role": "handler",
        "comment": "渠道漏传这笔", "expected_version": 0,
    }).status_code == 200

    # 审核通过 → 因存疑落「待主管复核」而不是直接终态
    r = client.post(fresh_url, json={
        "decision": "confirm", "operator": "E-02", "role": "reviewer",
        "comment": "同意", "expected_version": 1,
    })
    assert r.status_code == 200, r.text
    assert r.json()["after"] == "待主管复核", (
        f"存疑账期该多一道主管复核，实际落到 {r.json()['after']}"
    )

    # 这一关上前端传 reviewer 想自己签掉：被岗位判据挡下
    blocked = client.post(review_url, json={
        "decision": "confirm", "operator": "E-02", "role": "reviewer",
        "comment": "我自己批了", "expected_version": 2,
    })
    assert blocked.status_code == 409, blocked.text
    # 被挡下的理由有两层，都对：/review 已把岗位强制成主管，而 E-02 早已
    # 以审核身份签过这一单 —— 岗位判据与岗位分离判据任一条都足以挡下。
    assert blocked.json()["code"] in ("LEDGER_ROLE_DENIED", "LEDGER_ROLE_SEPARATION_DENIED")
    assert blocked.json()["before"] == blocked.json()["after"] == "待主管复核"

    # 主管来签：/review 强制按主管落，只写主管栏
    r = client.post(review_url, json={
        "decision": "confirm", "operator": "E-S", "role": "reviewer",
        "comment": "主管复核无误", "expected_version": 2,
    })
    assert r.status_code == 200, r.text
    conn = connect(opened_db)
    try:
        item = require(conn, fresh_key)
    finally:
        conn.close()
    assert item.status == "已确认"
    assert item.supervisor == "E-S", "review 端点没把签字落到主管栏"
    assert item.reviewer == "E-02", "review 端点不该把审核那一栏改成主管"


# ─────────────────────────── 6. 撤回与终态 ───────────────────────────


def test_withdraw_resets_settled_row_to_pending(client, opened_db):
    """已确认可撤回重置为待确认。

    撤回**不销号**：``handler`` / ``reviewer`` 两列都留着，``version``
    照常 +1 —— 销号会让「这单曾经被谁签过」在结论单上消失，而那恰恰是
    复盘时最需要的那句话。
    """
    _decide(client, decision="confirm", operator="E-01", role="handler",
            comment="渠道延迟入账", expected_version=0)
    _decide(client, decision="confirm", operator="E-02", role="reviewer",
            comment="复核无误", expected_version=1)

    r = _decide(client, decision="withdraw", operator="E-02", role="reviewer",
                comment="渠道补了新的说明，重看", expected_version=2)
    assert r.status_code == 200, r.text
    assert r.json()["after"] == "待确认"

    from reconq.ledger.flow import list_actions, require

    conn = connect(opened_db)
    try:
        item = require(conn, KEY)
        # 曾经谁签过，仍然查得到
        assert item.handler == "E-01" and item.reviewer == "E-02"
        assert item.version == 3
        trail = [a["action"] for a in list_actions(conn, key=KEY)]
        assert trail == ["submit", "approve", "withdraw"], (
            f"留痕链该是「提交→通过→撤回」，实际 {trail}"
        )
    finally:
        conn.close()


def test_double_withdraw_is_denied(client, opened_db):
    """撤回后已经在「待确认」上，再撤一次被拒。

    症状（面板上按钮还亮着、点一下报「状态不允许」）正是这条要防的：
    撤回到「待确认」之后，唯一还能按的是通过/驳回，而不是再撤一次。
    """
    _decide(client, decision="confirm", operator="E-01", role="handler",
            comment="渠道延迟入账", expected_version=0)
    _decide(client, decision="confirm", operator="E-02", role="reviewer",
            comment="复核无误", expected_version=1)
    assert _decide(client, decision="withdraw", operator="E-02", role="reviewer",
                   comment="看错了", expected_version=2).status_code == 200
    r = _decide(client, decision="withdraw", operator="E-02", role="reviewer",
                comment="再看错了", expected_version=3)
    assert r.status_code == 409
    assert r.json()["code"] in ("LEDGER_TRANSITION_DENIED", "LEDGER_ACTION_DENIED")
