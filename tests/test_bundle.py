"""归因证据包构造（Task 27）。

五类规定断言逐条可核：

1. **字段齐全** —— 差异行、全部候选行（含打分因子）、渠道口径摘要、账期
   时间窗、渠道元信息、近 N 账期同因历史，五块都在，且逐条带源文件行号；
2. **候选裁剪与 truncated 标注** —— 超上限时按分数保留 top N，并显式带
   ``truncated`` 与**真实总数**（静默截断会让模型以为「就这些候选」）；
3. **历史先验频次正确** —— 频次来自台账里的**真实处置结论**；台账未建立 /
   连接未提供时**明说不可用 + 原因**，不返回 0（"查不到" 与 "确实没有"
   在归因上是两回事）；
4. **脱敏断言** —— 字段走白名单，患者姓名 / 身份证 / 手机号 / 住院号
   （含「值长得像」而不只是「键名叫」）一律拒构造；
5. **两次构造字节一致** —— 同一份数据构造两次，规范化 JSON 的 sha256
   逐字节相同，且**不含构造时刻**（含了的话每次重跑都换一个哈希，
   「归因可复现」这条不变量在最后一步失效）。

外加几条**必须成立的不变量**（本任务最容易被糊弄过去的地方）：

* 候选打分与 Task 26 复算用**同一套行还原口径**（``SnapshotRef``）——
  两处各写一份，退费标记会在「打分时算红冲、复算时算正常收费」上漂移；
* **因子 detail 一起给**：只给「金额差 0 × 0.45 = 0.00」而不说「差 78.00
  元，超容差 0.01 元」，模型看到的是没有来由的 0 分；
* 台账条目那一路**明说**候选来自台账、未带候选，不静默返回空列表冒充
  「没有候选」；
* 归因证据包**不推进任何台账状态**（那是 Task 43 的事）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from reconq.align.bundle import (
    BUNDLE_ROW_FIELDS,
    BUNDLE_VERSION,
    CHANNEL_BILL_TYPE,
    DEFAULT_CANDIDATE_CAP,
    EvidenceBundle,
    assert_bundle_deidentified,
    build_evidence_bundle,
    contract_slice,
    query_cause_history,
)
from reconq.align.matcher import AlignRecord, align_channel, build_index
from reconq.align.snapshot import SnapshotRef
from reconq.align.trace import GLOBAL_CHANNEL, TraceConclusion
from reconq.contracts.loader import load_contract
from reconq.engine.period import DIFF_TABLE, DiffItem, init_scan_schema
from reconq.errors import ReconqError
from reconq.sources.timezone import HOSPITAL_TZ, period_window
from reconq.store.schema import connect, init_schema

PERIOD = "2026-W36"
TZ = HOSPITAL_TZ
CONTRACT = "contracts/wechat.yml"


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _chan(order_no: str, amount: str, *, time: str = "2026-09-02 10:00:00", row: int = 3,
          refund: str = "", ch_id: int = 1, **kw) -> AlignRecord:
    return AlignRecord(
        channel="wechat",
        period=PERIOD,
        order_no=order_no,
        amount=Decimal(amount),
        refund=Decimal(refund) if refund else None,
        trade_time=_t(time),
        source_file="wechat_bill.csv",
        source_row_no=row,
        ch_id=ch_id,
        **kw,
    )


def _his(order_no: str, amount: str, *, time: str = "2026-09-02 10:00:00", row: int = 17,
         dept: str = "门诊收费室", settle: str = "微信", ch_id: int = 2,
         refund: str = "", trade_type: str = "") -> AlignRecord:
    return AlignRecord(
        channel="his",
        period=PERIOD,
        order_no=order_no,
        his_order_no=order_no,
        amount=Decimal(amount),
        refund=Decimal(refund) if refund else None,
        trade_type=trade_type,
        trade_time=_t(time),
        department=dept,
        settle_type=settle,
        source_file="charge_record.csv",
        source_row_no=row,
        ch_id=ch_id,
    )


def _outcome(*channel_records, his_records=(), channel: str = "wechat", window: bool = True):
    index = build_index(list(his_records), period=PERIOD)
    result = align_channel(
        channel,
        list(channel_records),
        index,
        period=PERIOD,
        window=period_window(PERIOD, TZ) if window else None,
    )
    assert len(result.outcomes) == 1
    return result.outcomes[0]


def _contract():
    return load_contract(CONTRACT, {"period": PERIOD})


def _db(with_diff_table: bool = True) -> sqlite3.Connection:
    conn = connect(":memory:")
    init_schema(conn)
    if with_diff_table:
        init_scan_schema(conn)
    return conn


def _bundle(outcome, **kw) -> EvidenceBundle:
    kw.setdefault("contract", _contract())
    kw.setdefault("window", period_window(PERIOD, TZ))
    return build_evidence_bundle(outcome, **kw)


# ─────────────────────── 一、字段齐全 ───────────────────────


def test_bundle_carries_all_five_blocks_with_source_row_numbers():
    """五块齐全，且差异行与候选行逐条可回查到源文件行号。"""
    outcome = _outcome(
        _chan("EX-1", "128.00"),
        his_records=[_his("EX-1", "50.00", row=17), _his("EX-1", "78.00", row=18, ch_id=3)],
    )
    assert outcome.status == "ambiguous"
    bundle = _bundle(outcome)
    payload = bundle.payload

    assert payload["bundle_version"] == BUNDLE_VERSION
    assert payload["conclusion_key"] == f"wechat:{PERIOD}:ex-1"
    assert payload["period"] == PERIOD and payload["channel"] == "wechat"

    # 1) 差异行：带源文件行号、定点金额、归一时区后的带偏移时间
    diff = payload["diff_row"]
    assert (diff["source_file"], diff["source_row_no"]) == ("wechat_bill.csv", 3)
    assert diff["amount"] == "128.00" and isinstance(diff["amount"], str)
    assert diff["trade_time"] == "2026-09-02T10:00:00+08:00"
    assert diff["side"] == "channel"

    # 2) 候选行：两条全在，逐条带行号与因子
    assert payload["candidate_total"] == 2 and payload["candidate_shown"] == 2
    refs = {(c["source_file"], c["source_row_no"]) for c in payload["candidate_rows"]}
    assert refs == {("charge_record.csv", 17), ("charge_record.csv", 18)}
    assert all(c["factors"] for c in payload["candidate_rows"]), "候选必须带打分因子"
    assert all(c["score"] is not None for c in payload["candidate_rows"])

    # 3) 渠道口径摘要
    slice_info = payload["channel_contract_slice"]
    assert slice_info["available"] is True
    assert slice_info["check_shown"] > 0
    assert all(c["operator"] for c in slice_info["checks"])

    # 4) 账期时间窗：半开，两端带偏移
    win = payload["period_window"]
    assert win["available"] is True and win["half_open"] is True
    assert win["start"] == "2026-08-31T00:00:00+08:00"
    assert win["end"] == "2026-09-07T00:00:00+08:00"

    # 5) 渠道元信息：渠道中文名 + 账单类型 + 源文件 + 证据切片指纹
    meta = payload["channel_meta"]
    assert meta["channel_zh"] == "微信支付"
    assert meta["bill_type"] == CHANNEL_BILL_TYPE["wechat"] == "ALL"
    assert meta["source_files"] == ["charge_record.csv", "wechat_bill.csv"]
    assert len(meta["evidence_slice_sha256"]) == 64


def test_bill_type_is_recorded_because_all_and_success_are_different_bills():
    """``bill_type=ALL`` 与 ``=SUCCESS`` 看到的是两份不同的账单。

    同一笔退费在 ``=SUCCESS`` 的账单里压根不出现 —— 模型若不知道本期取的是
    哪一份，会把「账单里没有这笔」说成「渠道漏报」。故 ``bill_type`` 必落
    证据包，且调用方显式给值时标出来源。
    """
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])
    default = _bundle(outcome)
    assert default.payload["channel_meta"]["bill_type"] == "ALL"
    assert default.payload["channel_meta"]["bill_type_source"] == "channel_default"

    explicit = _bundle(outcome, bill_type="SUCCESS")
    assert explicit.payload["channel_meta"]["bill_type"] == "SUCCESS"
    assert explicit.payload["channel_meta"]["bill_type_source"] == "caller"
    assert "SUCCESS" in explicit.render()
    assert explicit.digest != default.digest, "账单类型变了证据包必须换哈希"


def test_channel_only_bundle_has_the_diff_row_and_says_where_candidates_came_from():
    """单边账没有候选行，但**必须明说**候选来自哪里。"""
    outcome = _outcome(_chan("EX-SOLO", "66.00", row=8), his_records=[])
    assert outcome.status == "channel_only"
    bundle = _bundle(outcome)
    payload = bundle.payload
    assert payload["diff_row"]["source_row_no"] == 8
    assert payload["candidate_rows"] == []
    assert payload["candidate_source"] == "align.no_candidate_found", (
        "对齐器查过、确实没有第二条 —— 这是**结论**，与台账条目那种"
        "「系统压根没查过」的证据缺口必须分开报"
    )
    assert "对齐器已查过" in bundle.render()


def test_ledger_row_says_candidates_are_absent_rather_than_faking_an_empty_list():
    """台账条目那一路拿不到候选行 —— 明说，不静默返回空列表冒充「无候选」。

    空列表在两种情形下长得一模一样：「确实没有候选」与「这条来源压根不带
    候选」。前者是结论，后者是**证据缺口**，混起来会让模型以为系统已经查过
    并确认没有第二条记录。
    """
    item = DiffItem(
        period=PERIOD,
        channel="wechat",
        check_path="align.channel_only:EX-9",
        check_name="渠道有 HIS 无",
        status="fail",
        message="渠道行 EX-9 在 HIS 侧本账期无记录",
        diff_amount="66.00",
        needs_attribution=True,
    )
    bundle = build_evidence_bundle(item, window=period_window(PERIOD, TZ))
    payload = bundle.payload
    assert payload["discrepancy"]["kind"] == "channel_only", "check_path 前缀应还原出形态"
    assert payload["discrepancy"]["kind_zh"] == "渠道有 HIS 无"
    assert payload["conclusion_key"] == f"wechat:{PERIOD}:ex-9", "单号应从 check_path 尾部取回"
    assert payload["candidate_source"] == "ledger_row_without_candidates"
    assert payload["candidate_rows"] == []
    assert "未查证过是否存在候选" in bundle.render()


def test_trace_conclusion_bundle_is_marked_global_and_keeps_its_evidence_lines():
    """连坐结论渠道恒为 ``*``，候选节点在证据包里明说不可得。"""
    conclusion = TraceConclusion(
        kind="duplicate_across_period",
        order_no="EX-8",
        detail="同一笔 HIS 收费被两个账期各认领一次",
        diff_amount="86.00",
        evidence=["2026-W35 wechat/his 金额 86.00 元 · wechat_bill.csv:5",
                  "2026-W36 wechat/his 金额 86.00 元 · wechat_bill.csv:6"],
        periods=["2026-W35", PERIOD],
        period=PERIOD,
    )
    bundle = build_evidence_bundle(conclusion, window=period_window(PERIOD, TZ))
    payload = bundle.payload
    assert payload["channel"] == GLOBAL_CHANNEL == "*"
    assert payload["channel_meta"]["channel_zh"] == "跨渠道/跨账期"
    assert payload["discrepancy"]["kind_zh"] == "跨账期重复计入"
    assert payload["candidate_source"] == "trace.conclusion_nodes_unavailable"
    # 连坐结论的证据是链上节点，节点本身由 trace 层交给调用方；
    # 证据包这一路只带结论，**明说**节点不在这里而不是给一个空候选集。
    assert payload["candidate_rows"] == []


def test_unsupported_input_type_is_rejected_with_a_named_error():
    bundle_like = {"channel": "wechat", "status": "channel_only"}
    with pytest.raises(ReconqError) as exc:
        build_evidence_bundle(bundle_like)
    assert exc.value.code == "BUNDLE_INPUT_INVALID"
    assert "MatchOutcome" in exc.value.detail and "DiffItem" in exc.value.detail


# ─────────────── 二、候选裁剪与 truncated 标注 ───────────────


def test_candidates_are_truncated_by_score_and_the_real_total_is_kept():
    """超上限按分数保留 top N，并显式标 truncated + 真实总数。"""
    his_records = [
        _his("EX-M", "10.00", row=20 + i, time="2026-09-02 10:%02d:00" % (i + 1), ch_id=10 + i)
        for i in range(8)
    ]
    outcome = _outcome(_chan("EX-M", "128.00"), his_records=his_records)
    assert outcome.status == "ambiguous"

    bundle = _bundle(outcome, candidate_cap=3)
    payload = bundle.payload
    assert payload["candidate_total"] == 8, "真实总数必须留着：截掉的可能正是唯一说得通的那条"
    assert payload["candidate_shown"] == 3
    assert payload["candidate_cap"] == 3
    assert payload["candidate_truncated"] is True
    assert payload["candidate_rank_basis"] == "score"

    rows = payload["candidate_rows"]
    scores = [Decimal(r["score"]) for r in rows]
    assert scores == sorted(scores, reverse=True), "保留的必须是分数最高的 3 条"
    assert "已截断，另有 5 条未展示" in bundle.render()

    # 不截断时 truncated 为 False，且不出现「已截断」字样
    full = _bundle(outcome, candidate_cap=8)
    assert full.payload["candidate_truncated"] is False
    assert "已截断" not in full.render()
    assert full.payload["candidate_shown"] == 8

    # 默认上限 5 面对 8 条候选同样标截断（默认值不是「不限制」的同义词）
    default = _bundle(outcome)
    assert default.payload["candidate_cap"] == DEFAULT_CANDIDATE_CAP
    assert default.payload["candidate_truncated"] is True
    assert default.payload["candidate_shown"] == 5
    assert default.payload["candidate_total"] == 8


def test_ranking_basis_is_recorded_and_falls_back_to_input_order_without_scores():
    """没有分数时按输入顺序排，并**明说**排的是什么。

    无声地「按行号排」会被读成「按可信度排」—— 而歧义候选恰恰是系统
    明确拒绝排序的那一组，它给出的顺序没有任何可信度含义。
    """
    outcome = _outcome(
        _chan("EX-N", "50.00"),
        his_records=[_his("EX-N", "25.00", row=30), _his("EX-N", "25.00", row=31, ch_id=3)],
    )
    bundle = _bundle(outcome)
    assert bundle.payload["candidate_rank_basis"] == "score"
    assert "按打分给出" in bundle.render()

    # 手工构造一条无分数的证据包（台账条目那一路天然如此）
    item = DiffItem(
        period=PERIOD, channel="wechat", check_path="align.channel_only:EX-9",
        status="fail", message="x", needs_attribution=True,
    )
    ledger_bundle = build_evidence_bundle(item, window=period_window(PERIOD, TZ))
    assert ledger_bundle.payload["candidate_rank_basis"] == "input_order"


def test_negative_candidate_cap_is_rejected_rather_than_silently_unlimited():
    outcome = _outcome(_chan("EX-P", "10.00"), his_records=[])
    with pytest.raises(ReconqError) as exc:
        _bundle(outcome, candidate_cap=-1)
    assert exc.value.code == "BUNDLE_WINDOW_INVALID"


# ─────────────── 三、历史先验频次正确 ───────────────


def test_cause_history_counts_real_dispositions_from_the_ledger():
    """频次来自台账里的**真实处置结论**，按形态的 check_path 前缀归集。"""
    conn = _db()
    rows = [
        # 近 4 期里本形态的处置结论：已确认 2、已驳回 1
        DiffItem(period="2026-W32", channel="wechat", check_path="align.channel_only:EX-1",
                 status="fail", message="m", disposition="已确认", needs_attribution=True),
        DiffItem(period="2026-W33", channel="wechat", check_path="align.channel_only:EX-2",
                 status="fail", message="m", disposition="已确认", needs_attribution=True),
        DiffItem(period="2026-W34", channel="wechat", check_path="align.channel_only:EX-3",
                 status="fail", message="m", disposition="已驳回", needs_attribution=True),
        # 别的原因：不计入
        DiffItem(period="2026-W34", channel="wechat", check_path="align.weak_pending:EX-4",
                 status="fail", message="m", disposition="已确认", needs_attribution=True),
        # 本期：不计入（窗口只看往期）
        DiffItem(period=PERIOD, channel="wechat", check_path="align.channel_only:EX-5",
                 status="fail", message="m", disposition="已确认", needs_attribution=True),
    ]
    for row in rows:
        conn.execute(
            f"INSERT INTO {DIFF_TABLE} (period, channel, check_path, check_name, status,"
            " message, disposition, needs_attribution) VALUES (?,?,?,?,?,?,?,?)",
            (row.period, row.channel, row.check_path, row.check_name, row.status,
             row.message, row.disposition, int(row.needs_attribution)),
        )

    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])
    bundle = _bundle(outcome, conn=conn)
    history = bundle.payload["history"]
    assert history["available"] is True
    assert history["window_periods"] == 4
    assert history["periods"] == ["2026-W32", "2026-W33", "2026-W34", "2026-W35"]
    assert history["same_cause"]["total"] == 3
    assert history["same_cause"]["by_disposition"] == {"已驳回": 1, "已确认": 2}
    assert history["same_cause"]["by_period"] == {
        "2026-W32": 1, "2026-W33": 1, "2026-W34": 1,
    }
    assert "已确认 2 次" in bundle.render() and "已驳回 1 次" in bundle.render()


def test_history_window_excludes_the_current_period_and_never_fabricates():
    """窗口只看往期；本期结论不得混进先验。"""
    conn = _db()
    for period in (PERIOD, "2026-W35"):
        conn.execute(
            f"INSERT INTO {DIFF_TABLE} (period, channel, check_path, status, message,"
            " disposition, needs_attribution) VALUES (?,?,?,?,?,?,?)",
            (period, "wechat", "align.channel_only:EX-1", "fail", "m", "已确认", 1),
        )
    history = query_cause_history(
        conn, period=PERIOD, channel="wechat", kind="channel_only", window_periods=4
    )
    assert PERIOD not in history["periods"]
    assert history["same_cause"]["total"] == 1, "只有 W35 那条，本期那条不算先验"

    # 往期确实一条都没有 → 明说「确实无先例」，与「查不到」区分开。
    # 取一个窗口完全落在已有数据之前的账期（W44 的窗口是 W40…W43）。
    empty = query_cause_history(
        conn, period="2026-W44", channel="wechat", kind="channel_only", window_periods=4
    )
    assert empty["available"] is False
    assert "确实无先例" in empty["same_cause"]["reason"]
    conn.close()


def test_history_reports_unavailable_with_a_reason_instead_of_zero():
    """台账未建立 / 无连接 → **不可用 + 原因**，不返回 0。

    「查不到」与「确实没有」在归因上是两回事：前者是证据不足（模型该说
    「本期无先例可依」），后者是真没有（模型可以更有把握）。把前者报成后者，
    模型会把「首次出现」说成「历史上从未发生过」。
    """
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])

    # 无连接
    no_conn = _bundle(outcome)
    assert no_conn.payload["history"]["available"] is False
    assert any("未提供账单库连接" in r for r in no_conn.payload["history"]["reasons"])
    assert "不可据此断言「历史上从未发生过」" in no_conn.render()

    # 台账表未建立
    conn = _db(with_diff_table=False)
    no_table = _bundle(outcome, conn=conn)
    history = no_table.payload["history"]
    assert history["available"] is False
    assert any("尚未建立" in r for r in history["reasons"])
    assert history["same_cause"]["total"] == 0
    assert "尚未建立" in no_table.render()
    conn.close()


def test_same_order_no_history_carries_the_actual_past_rows_not_just_a_count():
    """同单号往期出现：频次之外还要把**行本身**取回来。

    只给「上周出现过 1 次」的计数，模型判「这一笔是不是上周那笔的延续」
    就只能猜 —— 而那恰恰是跨账期落点与重复计入最像的一种形态。
    """
    conn = _db()
    conn.execute(
        "INSERT INTO bill_rows (channel, period, bill_date, channel_order_no, amount_text,"
        " trade_time, source_file, source_row_no) VALUES (?,?,?,?,?,?,?,?)",
        ("wechat", "2026-W35", "2026-08-28", "EX-HIST", "86.00",
         "2026-08-28T10:00:00+08:00", "wechat_bill_2026-W35.csv", 5),
    )
    outcome = _outcome(_chan("EX-HIST", "86.00", row=9), his_records=[])
    bundle = _bundle(outcome, conn=conn)
    history = bundle.payload["history"]
    assert history["same_order_no"]["available"] is True
    assert history["same_order_no"]["occurrences"] == 1
    assert history["same_order_no"]["by_period"] == {"2026-W35": 1}

    rows = bundle.payload["same_order_no_rows"]
    assert len(rows) == 1
    assert (rows[0]["source_file"], rows[0]["source_row_no"]) == (
        "wechat_bill_2026-W35.csv", 5,
    )
    assert rows[0]["amount"] == "86.00"
    assert "2026-W35 1 行" in bundle.render()
    conn.close()


def test_negative_history_window_is_rejected():
    with pytest.raises(ReconqError) as exc:
        query_cause_history(None, period=PERIOD, channel="wechat", kind="channel_only",
                            window_periods=-1)
    assert exc.value.code == "BUNDLE_WINDOW_INVALID"


# ─────────────── 四、脱敏断言 ───────────────


def test_bundle_row_fields_are_whitelisted():
    """字段走白名单：构造时多带一个字段即拒。"""
    assert "patient_name" not in BUNDLE_ROW_FIELDS
    assert "name" not in BUNDLE_ROW_FIELDS, (
        "name 与患者姓名同名，宁可让契约口径写成 check_name"
    )
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])
    bundle = _bundle(outcome)
    for row in [bundle.payload["diff_row"]] + bundle.payload["candidate_rows"]:
        assert set(row) <= BUNDLE_ROW_FIELDS

    tampered = EvidenceBundle(
        payload={**bundle.payload, "diff_row": {**bundle.payload["diff_row"],
                                                "patient_name": "张三"}},
        text=bundle.text,
        digest=bundle.digest,
    )
    with pytest.raises(ReconqError) as exc:
        assert_bundle_deidentified(tampered)
    assert exc.value.code == "BUNDLE_FIELD_NOT_ALLOWED"
    assert "patient_name" in exc.value.detail


def test_bundle_carries_no_patient_identifiable_fields():
    """真实构造出的证据包逐字段核对：只有金额、单号、账期、科室、时间。"""
    outcome = _outcome(
        _chan("EX-1", "128.00"),
        his_records=[_his("EX-1", "50.00", row=17), _his("EX-1", "78.00", row=18, ch_id=3)],
    )
    bundle = _bundle(outcome)
    blob = json.dumps(bundle.payload, ensure_ascii=False)
    for token in ("患者", "姓名", "身份证", "手机号", "住院号", "门诊号", "病案号", "床号"):
        assert token not in blob, f"证据包不得出现「{token}」"


def test_deidentified_guard_rejects_patient_identifiers_regardless_of_key_name():
    """键名干净但**值**形似身份证/手机号，同样拒。

    导出方把患者姓名写进订单备注列是很常见的事 —— 键名挡不住，只看键名
    的脱敏检查在这种数据上会整批漏过去。
    """
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])
    bundle = _bundle(outcome)
    for value in ("110101199003071234", "13800138000"):
        tampered = EvidenceBundle(
            payload={**bundle.payload, "detail": value},
            text=bundle.text,
            digest=bundle.digest,
        )
        with pytest.raises(ReconqError) as exc:
            assert_bundle_deidentified(tampered)
        assert exc.value.code == "SNAPSHOT_NOT_DEIDENTIFIED"


def test_deidentified_guard_does_not_reject_ordinary_order_numbers():
    """反向用例：18 位订单号不是身份证。"""
    outcome = _outcome(_chan("4200001234202609123456789", "88.00"), his_records=[])
    bundle = _bundle(outcome)  # 构造本身就会过脱敏闸门，抛错即失败
    assert "4200001234202609123456789" in bundle.render()


# ─────────────── 五、两次构造字节一致 ───────────────


def test_two_constructions_from_the_same_data_are_byte_identical():
    """同一份数据构造两次，规范化 JSON 的 sha256 与渲染文本逐字节相同。"""
    outcome = _outcome(
        _chan("EX-1", "128.00"),
        his_records=[_his("EX-1", "50.00", row=17), _his("EX-1", "78.00", row=18, ch_id=3)],
    )
    first = _bundle(outcome)
    second = _bundle(outcome)
    assert first.digest == second.digest
    assert first.render() == second.render()
    assert json.dumps(first.payload, sort_keys=True) == json.dumps(second.payload, sort_keys=True)


def test_bundle_hash_excludes_any_construction_moment():
    """内容哈希**不含**构造时刻。

    含了的话每次重跑都换一个哈希，「重跑了一次但证据没变」会被误报成
    「证据变了」—— 而那恰恰是最常见的一种重跑。断言方式：两次构造间隔
    足够长（构造路径上不读时钟，故无需 sleep），并检查渲染文本里没有
    任何时间戳。
    """
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[_his("EX-1", "50.00")])
    first = _bundle(outcome)
    second = _bundle(outcome)
    assert first.digest == second.digest
    text = first.render()
    assert "构造时刻" not in text and "generated_at" not in text


def test_digest_changes_when_the_evidence_actually_changes():
    """反向用例：证据真的变了，哈希必须跟着变（否则哈希就是个摆设）。"""
    outcome_a = _outcome(_chan("EX-1", "128.00", row=3), his_records=[])
    outcome_b = _outcome(_chan("EX-1", "128.00", row=4), his_records=[])
    assert _bundle(outcome_a).digest != _bundle(outcome_b).digest, "行号变了证据就变了"


# ─────────────── 不变量：打分与复算同一套行还原口径 ───────────────


def test_candidate_scoring_reuses_the_snapshot_row_round_trip():
    """候选打分走 ``SnapshotRef`` 还原，与 Task 26 复算同一套口径。

    两处各写一份还原，退费标记会在「打分时算红冲、复算时算正常收费」上
    漂移：候选排序与复算结论对不上，而**两边都不报错**。

    这里用 Task 26 已确立的第二条退费判据（``trade_type`` 写着红冲）——
    候选行只带净额、没有退款列，**只能**靠这条判据把红冲行认出来。
    """
    outcome = _outcome(
        _chan("EX-R", "128.00", refund="86.00"),
        his_records=[
            _his("EX-R", "42.00", row=17),
            _his("EX-R", "-42.00", row=18, ch_id=3, trade_type="红冲"),
        ],
    )
    assert outcome.ambiguity is not None
    assert outcome.ambiguity.kind == "refund_and_original", (
        "形态判错话术就错：退费并存被说成一码付拆笔，经办人会去查拆分说明，"
        "而实际该查的是红冲是否生效"
    )
    bundle = _bundle(outcome)
    candidates = bundle.payload["candidate_rows"]
    assert len(candidates) == 2
    refunds = {c["is_refund"] for c in candidates}
    assert refunds == {True, False}, (
        "红冲标记必须一路带到候选行；丢了它「退费与原单并存」的建议话术"
        "就把人引去查拆分说明了"
    )
    # 还原口径确实来自 SnapshotRef：同一条候选两条路径给出同一条行
    candidate = outcome.candidates[0]
    via_snapshot = SnapshotRef.from_candidate(candidate).to_record()
    assert via_snapshot.is_refund == candidate.is_refund
    assert via_snapshot.net_amount == Decimal(candidate.amount)


def test_factor_detail_is_carried_so_a_zero_score_has_a_reason():
    """因子 detail 一起给：0 分必须带得上「差多少、为什么」。

    只给「金额差 0 × 0.45 = 0.00」而不说「差 78.00 元，超容差 0.01 元」，
    模型看到的是一个没有来由的 0，会把它理解成「金额对不上但原因未知」。
    """
    outcome = _outcome(
        _chan("EX-1", "128.00"),
        his_records=[_his("EX-1", "50.00", row=17), _his("EX-1", "78.00", row=18, ch_id=3)],
    )
    assert outcome.status == "ambiguous"
    bundle = _bundle(outcome)
    factor = bundle.payload["candidate_rows"][0]["factors"][0]
    assert factor["factor"] == "amount"
    assert factor["value"] == "0"
    assert "差 78.00 元" in factor["detail"]
    assert "差 78.00 元" in bundle.render()


def test_bundle_never_advances_a_ledger_state():
    """证据包只读：构造前后差异台账逐行不变。"""
    conn = _db()
    conn.execute(
        f"INSERT INTO {DIFF_TABLE} (period, channel, check_path, status, message,"
        " disposition, needs_attribution) VALUES (?,?,?,?,?,?,?)",
        (PERIOD, "wechat", "align.channel_only:EX-1", "fail", "m", "待确认", 1),
    )
    before = conn.execute(f"SELECT * FROM {DIFF_TABLE} ORDER BY diff_id").fetchall()
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])
    _bundle(outcome, conn=conn)
    after = conn.execute(f"SELECT * FROM {DIFF_TABLE} ORDER BY diff_id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert after[0]["disposition"] == "待确认", (
        "归因证据包不推进台账状态 —— 已确认只能由人工点产生"
    )
    conn.close()


def test_audit_metadata_reports_size_without_the_evidence_body():
    """体量统计量的是**真正发出去的那段文本**，审计元数据不含正文。

    审计要能回答「这次喂了多大一坨、依据哪条差异、裁没裁」，但正文进日志库
    等于把渠道账单细节抄了第二份。
    """
    outcome = _outcome(
        _chan("EX-1", "128.00"),
        his_records=[_his("EX-1", "50.00"), _his("EX-1", "78.00", row=18, ch_id=3)],
    )
    bundle = _bundle(outcome, candidate_cap=1)
    meta = bundle.audit_metadata()
    assert meta["bundle_digest"] == bundle.digest
    assert meta["char_count"] == bundle.char_count == len(bundle.render())
    assert meta["line_count"] == bundle.line_count
    assert meta["candidate_shown"] == 1 and meta["candidate_total"] == 2
    assert meta["candidate_truncated"] is True
    assert meta["discrepancy_kind"] == "ambiguous"
    assert "candidate_rows" not in meta and "diff_row" not in meta


def test_audit_metadata_reports_the_contract_slice_actually_shown():
    """审计元数据里的口径条数必须与渲染出来的一致。

    回归：审计读的键名一旦与 payload 里的不一致，这里会静默变成 0 ——
    网关于是把每次调用的「喂了几条口径」都记成 0，而正文里明明列着 7 条。
    排障时去查「口径是不是没进去」，方向会被直接带偏，且**没有任何报错**。
    """
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])
    bundle = _bundle(outcome)
    meta = bundle.audit_metadata()
    shown = bundle.payload["channel_contract_slice"]["check_shown"]
    assert shown > 0, "本用例前提：这一形态确实选出了相关口径"
    assert meta["contract_check_shown"] == shown
    assert meta["contract_check_total"] == bundle.payload["channel_contract_slice"]["check_total"]
    assert meta["contract_slice_available"] is True


# ─────────────── 契约口径摘要 ───────────────


def test_contract_slice_only_picks_checks_relevant_to_this_discrepancy():
    """只挑与本差异相关的口径：单号唯一性与「渠道有 HIS 无」没有因果关系。"""
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])
    bundle = _bundle(outcome)
    checks = bundle.payload["channel_contract_slice"]["checks"]
    operators = {c["operator"] for c in checks}
    assert "not_empty" in operators, "单号必填是「渠道有 HIS 无」最直接的判据"
    assert "unique" not in operators, (
        "单号唯一性与本形态无因果关系；塞进去只会让模型把两件事混为一谈"
    )
    assert all(c["why"] for c in checks), "每条都要说清为什么与本差异相关"


def test_contract_slice_says_unavailable_instead_of_faking_an_empty_list():
    """没给契约时明说不可用 + 原因，不返回空列表冒充「无相关口径」。"""
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])
    bundle = _bundle(outcome, contract=None)
    slice_info = bundle.payload["channel_contract_slice"]
    assert slice_info["available"] is False
    assert slice_info["checks"] == []
    assert "未提供该渠道契约" in slice_info["reason"]
    assert "本渠道相关口径：不可用" in bundle.render()


def test_contract_slice_is_truncated_with_a_flag():
    outcome = _outcome(_chan("EX-1", "128.00"), his_records=[])
    bundle = _bundle(outcome)
    sliced = contract_slice(
        _contract(), kind="channel_only", channel="wechat",
        rows=[bundle.payload["diff_row"]], cap=1,
    )
    assert sliced["check_shown"] == 1
    assert sliced["truncated"] is True
    assert sliced["check_total"] > 1


# ─────────────── 真实样例账单（不是手工构造的记录） ───────────────


def test_real_fixture_bills_produce_usable_bundles_for_every_pending_conclusion():
    """真实脱敏样例账单走完「读文件 → 落库 → 对齐 → 证据包」。

    单测里手工构造的 :class:`AlignRecord` 证明不了这一段：真实账单带来的是
    **脏行、多种编码、缺失的科室列、退费行**。本用例把六路样例真读一遍，
    逐条待归因结论都要求：证据包能构造、五块齐全、脱敏过闸、两次构造字节
    相同、审计元数据与渲染一致。
    """
    from reconq.align.matcher import align_all, record_from_row_dict
    from reconq.sources.inbox_files import read_inbox_dir
    from reconq.store.bill_store import build_record, ingest_channel_rows, query_period

    channels = ("wechat", "alipay", "unionpay", "self_service", "medical_ins")
    conn = _db()
    ingested = 0
    for channel in channels:
        contract = load_contract(f"contracts/{channel}.yml", {"period": PERIOD})
        rows = read_inbox_dir("tests/fixtures/bills", PERIOD, channel, contract.table())
        clean = []
        for row in rows:
            try:
                build_record(row, channel=channel, period=PERIOD)
            except ReconqError:
                continue  # 脏行进隔离区（样例里有一行缺单号），不进证据包
            clean.append(row)
        if clean:
            ingest_channel_rows(conn, clean, channel=channel, period=PERIOD)
            ingested += len(clean)
    assert ingested > 30, f"真实样例应读进数十行，实得 {ingested}"

    records = [record_from_row_dict(r) for r in query_period(conn, PERIOD)]
    his = [r for r in records if r.channel == "his"]
    by_channel: dict[str, list[AlignRecord]] = {}
    for record in records:
        if record.channel != "his":
            by_channel.setdefault(record.channel, []).append(record)
    result = align_all(by_channel, his, period=PERIOD, window=period_window(PERIOD, TZ))

    seen_statuses: set[str] = set()
    built = 0
    for channel, cres in result.channels.items():
        for outcome in cres.outcomes:
            if not outcome.needs_attribution:
                continue
            contract = load_contract(f"contracts/{channel}.yml", {"period": PERIOD})
            bundle = build_evidence_bundle(
                outcome, contract=contract, window=period_window(PERIOD, TZ), conn=conn
            )
            built += 1
            seen_statuses.add(outcome.status)
            payload = bundle.payload
            assert payload["diff_row"], f"{outcome.status} 的证据包缺差异行"
            assert payload["channel_contract_slice"]["available"] is True
            assert payload["period_window"]["available"] is True
            assert payload["channel_meta"]["evidence_slice_sha256"]
            assert payload["channel_meta"]["source_files"], "必须点名源文件，否则回查不到"
            assert bundle.char_count > 0 and bundle.line_count > 0
            assert bundle.audit_metadata()["contract_check_shown"] == (
                payload["channel_contract_slice"]["check_shown"]
            )
            # 同一份数据两次构造字节一致（真实路径上同样成立）
            again = build_evidence_bundle(
                outcome, contract=contract, window=period_window(PERIOD, TZ), conn=conn
            )
            assert again.digest == bundle.digest
            assert again.render() == bundle.render()
            # 脱敏闸门在真实数据上同样过
            assert_bundle_deidentified(bundle)
    assert built > 0, "真实样例应至少产出一条待归因结论"
    assert "channel_only" in seen_statuses
    conn.close()
