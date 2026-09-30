"""对齐结论的不可变证据快照与可复算回放（Task 26）。

五类规定断言逐条可核：

1. **快照含源行号** —— 参与方逐条带源文件与行号、时间戳、金额（定点
   字符串），并记住当时的契约指纹与索引版本；
2. **重复生成冲突报错** —— 同 ``(period, conclusion_key)`` 内容不同时报
   ``SNAPSHOT_CONFLICT`` 且**两份都留着**；内容相同时判重复拍、不再插行；
3. **复算一致** —— 源账单表被重拉/清空后，仅凭快照仍能复算出同一结论；
4. **复算不一致标 ``replay_mismatch``** —— 判定状态、差额、歧义形态任一
   变化都逐条列出，且口径指纹变化**单独**报而不混进变化点；
5. **脱敏** —— 快照字段走白名单，出现患者姓名/身份证/手机号/住院号
   （含「值长得像」而不只是「键名叫」）一律拒写。

外加几条**必须成立的不变量**（本任务最容易被糊弄过去的地方）：

* 快照**只追加、永不 UPDATE**：库里对同一结论键出现多行时，较早那几行
  的内容哈希必须与当初落盘时逐字节相同。
* 复算**不读账单表**：把 ``bill_rows`` 整表删掉后复算结论不变。
* 内容哈希**不含拍摄时刻**：同一份数据拍两次必须得到同一个哈希，否则
  「重跑了一次但内容没变」会被误报成冲突。
* 结论键**不含结论类型**：否则同一实体从 matched 变 mismatch 会落到两个
  键下，最该被告知的那种变化反而测不出来。
* 连坐结论（跨渠道/跨账期）复算时**明说不可复算**并给原因，不静默跳过、
  也不硬凑一个结论出来。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from decimal import Decimal

import pytest

from reconq.align.matcher import AlignRecord, align_channel, build_index
from reconq.align.snapshot import (
    ALIGN_INDEX_VERSION,
    FORBIDDEN_PATIENT_KEYS,
    SNAPSHOT_ALLOWED_FIELDS,
    EvidenceSnapshot,
    SnapshotRef,
    assert_deidentified,
    conclusion_key_for,
    conclusion_key_of,
    count_alignment_snapshots,
    init_align_snapshot_schema,
    latest_alignment_snapshot,
    load_alignment_snapshots,
    record_alignment_snapshot,
    replay_snapshot,
    render_replay,
    render_snapshot,
    snapshot_from_outcome,
    snapshot_from_trace_conclusion,
)
from reconq.align.trace import GLOBAL_CHANNEL, TraceConclusion, TraceKind, TraceNode
from reconq.errors import ReconqError
from reconq.sources.timezone import HOSPITAL_TZ, period_window

PERIOD = "2026-W36"
TZ = HOSPITAL_TZ
FIXED_NOW = "2026-09-07T09:00:00+08:00"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _chan(order_no: str, amount: str, *, time: str = "2026-09-02 10:00:00", row: int = 1,
          refund: str = "", ch_id: int = 0) -> AlignRecord:
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
    )


def _his(order_no: str, amount: str, *, time: str = "2026-09-02 10:00:00", row: int = 1,
         dept: str = "门诊收费室", settle: str = "微信", ch_id: int = 0,
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


def _only_outcome(*channel_records, his_records=(), window=True):
    """跑一路对齐并取出唯一一条结论。"""
    index = build_index(list(his_records), period=PERIOD)
    result = align_channel(
        "wechat",
        list(channel_records),
        index,
        period=PERIOD,
        window=period_window(PERIOD, TZ) if window else None,
    )
    assert len(result.outcomes) == 1
    return result.outcomes[0]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_align_snapshot_schema(conn)
    return conn


# ───────────────────────── 一、快照含源行号 ─────────────────────────


def test_snapshot_carries_source_row_numbers_times_amounts_and_fingerprints():
    """渠道行 + 认领的 HIS 行都进快照，逐条可回查。"""
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    assert outcome.status == "matched"
    snap = snapshot_from_outcome(
        outcome,
        contract_digest=DIGEST_A,
        contract_check_digest=DIGEST_B,
        created_at=FIXED_NOW,
    )
    assert snap.conclusion_key == f"wechat:{PERIOD}:ex-1"
    assert snap.participant_total == 2
    chan_ref, his_ref = snap.channel_refs[0], snap.his_refs[0]
    # 逐条带源文件与行号 —— 结论单上「依据哪几行」问的就是这个
    assert (chan_ref.source_file, chan_ref.source_row_no) == ("wechat_bill.csv", 3)
    assert (his_ref.source_file, his_ref.source_row_no) == ("charge_record.csv", 17)
    assert chan_ref.ref == "wechat_bill.csv:3"
    assert his_ref.ref == "charge_record.csv:17"
    # 金额是定点字符串（不是 float），时间带偏移
    assert chan_ref.amount == "128.00" and isinstance(chan_ref.amount, str)
    assert chan_ref.trade_time == "2026-09-02T10:00:00+08:00"
    # 记下当时的契约指纹与索引版本：口径变了结论可能跟着变，复算要能区分
    assert snap.contract_digest == DIGEST_A
    assert snap.contract_check_digest == DIGEST_B
    assert snap.index_version == ALIGN_INDEX_VERSION
    assert snap.content_hash and len(snap.content_hash) == 64


def test_snapshot_keeps_ambiguity_candidates_not_only_the_channel_row():
    """歧义/弱匹配的候选行必须一起进快照。

    漏掉候选的话复算时索引是空的，判定必然翻成单边账 ——
    ``replay_mismatch`` 会变成每次都报的噪声。
    """
    outcome = _only_outcome(
        _chan("EX-2", "100.00", row=4),
        his_records=[
            _his("EX-2", "50.00", row=5, ch_id=1),
            _his("EX-2", "50.00", row=6, ch_id=2),
        ],
    )
    assert outcome.status == "ambiguous"
    assert len(outcome.candidates) == 2
    snap = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
    assert snap.participant_total == 3, "渠道行 + 两条候选，少一条复算就翻"
    assert len(snap.his_refs) == 2
    assert {r.source_row_no for r in snap.his_refs} == {5, 6}


def test_snapshot_render_shows_participants_and_provenance():
    outcome = _only_outcome(
        _chan("EX-3", "66.00", row=8),
        his_records=[_his("EX-3", "60.00", row=9, ch_id=1)],
    )
    text = render_snapshot(snapshot_from_outcome(
        outcome, contract_digest=DIGEST_A, created_at=FIXED_NOW
    ))
    assert "wechat_bill.csv:8" in text
    assert "charge_record.csv:9" in text
    assert "口径指纹" in text and DIGEST_A[:12] in text
    assert "复算只用这些，不读账单表现状" in text


# ───────────────────── 二、重复生成冲突报错 ─────────────────────


def test_regenerating_the_same_content_is_a_duplicate_not_a_second_row():
    """同一份数据拍两次是**重复**，不是冲突 —— 否则每次重扫都多一行。"""
    conn = _db()
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    first = record_alignment_snapshot(
        conn,
        snapshot_from_outcome(outcome, contract_digest=DIGEST_A, created_at=FIXED_NOW),
    )
    # 拍摄时刻不同但内容一样：哈希必须相同（哈希刻意不含 created_at）
    second = record_alignment_snapshot(
        conn,
        snapshot_from_outcome(
            outcome, contract_digest=DIGEST_A, created_at="2026-09-08T10:00:00+08:00"
        ),
    )
    assert not first.conflict and not first.duplicate
    assert second.duplicate and second.snap_id == first.snap_id
    assert count_alignment_snapshots(conn) == 1
    # 同一份数据两次构造的哈希逐字节相同
    a = snapshot_from_outcome(outcome, contract_digest=DIGEST_A, created_at=FIXED_NOW)
    b = snapshot_from_outcome(
        outcome, contract_digest=DIGEST_A, created_at="2026-09-08T10:00:00+08:00"
    )
    assert a.content_hash == b.content_hash


def test_different_content_for_the_same_key_raises_snapshot_conflict_and_keeps_both():
    conn = _db()
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    old = record_alignment_snapshot(
        conn, snapshot_from_outcome(outcome, contract_digest=DIGEST_A, created_at=FIXED_NOW)
    )
    # 口径指纹变了 → 同一条结论的内容哈希跟着变
    with pytest.raises(ReconqError) as exc:
        record_alignment_snapshot(
            conn, snapshot_from_outcome(outcome, contract_digest=DIGEST_B, created_at=FIXED_NOW)
        )
    assert exc.value.code == "SNAPSHOT_CONFLICT"
    assert "--replay" in exc.value.render()
    # **两份都留着**：抛异常前回滚掉新行的话，「什么时候变的」就再也查不出
    rows = load_alignment_snapshots(conn, period=PERIOD)
    assert len(rows) == 2
    assert {r.contract_digest for r in rows} == {DIGEST_A, DIGEST_B}
    # 较早那一份的内容逐字节未变（只追加、永不 UPDATE）
    raw = conn.execute(
        "SELECT content_hash FROM align_snapshots WHERE snap_id = ?", (old.snap_id,)
    ).fetchone()
    assert raw["content_hash"] == old.snapshot.content_hash


def test_conflict_can_be_recorded_without_raising_for_batch_callers():
    """批量写入方可以选择收冲突而不是中断：两份照样都写进去。"""
    conn = _db()
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    record_alignment_snapshot(conn, snapshot_from_outcome(outcome, contract_digest=DIGEST_A))
    out = record_alignment_snapshot(
        conn,
        snapshot_from_outcome(outcome, contract_digest=DIGEST_B),
        raise_on_conflict=False,
    )
    assert out.conflict and out.conflict_with
    assert count_alignment_snapshots(conn) == 2


def test_same_key_different_period_is_not_a_conflict():
    """键里带账期：同一单号在两期各拍一份互不干扰。"""
    conn = _db()
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    record_alignment_snapshot(conn, snapshot_from_outcome(outcome, contract_digest=DIGEST_A))
    other = EvidenceSnapshot(
        conclusion_key=outcome_snapshot_key(outcome),
        period="2026-W37",
        channel="wechat",
        kind="channel_only",
        order_no="EX-1",
        participants=(SnapshotRef(
            side="channel", channel="wechat", period="2026-W37", order_no="EX-1",
            amount="128.00", source_file="w.csv", source_row_no=3,
        ),),
        contract_digest=DIGEST_B,
    ).with_hash()
    out = record_alignment_snapshot(conn, other)
    assert not out.conflict
    assert count_alignment_snapshots(conn) == 2


def outcome_snapshot_key(outcome) -> str:
    return conclusion_key_of(outcome)


def test_conclusion_key_excludes_the_conclusion_kind():
    """键里不能有结论类型。

    含了的话，同一实体从 matched 变 mismatch 会落到两个不同的键下，
    冲突检测永远测不出来 —— 而「上周对齐了这笔、这周没对齐」正是财务科
    最需要被告知的一种变化。
    """
    matched = _only_outcome(
        _chan("EX-1", "128.00"), his_records=[_his("EX-1", "128.00", ch_id=1)]
    )
    mismatch = _only_outcome(
        _chan("EX-1", "130.00"), his_records=[_his("EX-1", "128.00", ch_id=1)]
    )
    assert matched.status == "matched" and mismatch.status == "mismatch"
    assert conclusion_key_of(matched) == conclusion_key_of(mismatch)
    assert conclusion_key_for("wechat", PERIOD, " EX-1 ") == conclusion_key_of(matched)


# ───────────────────────── 三、复算一致 ─────────────────────────


def test_replay_recomputes_the_same_conclusion_from_the_snapshot_alone():
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    snap = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
    result = replay_snapshot(snap)
    assert result.replayable
    assert not result.replay_mismatch
    assert result.replayed_kind == "matched" == result.recorded_kind
    assert result.change_total == 0
    assert "✓ 复算与快照记录一致" in result.render()


@pytest.mark.parametrize(
    "channel_amount,his_amount,expected",
    [
        ("128.00", "128.00", "matched"),
        ("130.00", "128.00", "mismatch"),
        ("66.00", "", "channel_only"),
    ],
)
def test_replay_agrees_for_every_single_sided_status(
    channel_amount, his_amount, expected
):
    his = [_his("EX-1", his_amount, ch_id=1)] if his_amount else []
    outcome = _only_outcome(_chan("EX-1", channel_amount, row=3), his_records=his)
    assert outcome.status == expected
    result = replay_snapshot(snapshot_from_outcome(outcome, created_at=FIXED_NOW))
    assert result.replayable and not result.replay_mismatch
    assert result.replayed_kind == expected


def test_replay_works_after_the_bill_table_has_been_wiped():
    """复算**不读账单表**：源数据被重拉/清空后结论仍要站得住。"""
    conn = _db()
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    record_alignment_snapshot(conn, snapshot_from_outcome(outcome, created_at=FIXED_NOW))

    # 同库建一张账单表，插入会让人以为复算走了它
    conn.execute(
        "CREATE TABLE bill_rows (ch_id INTEGER PRIMARY KEY, channel TEXT, amount_text TEXT)"
    )
    conn.execute(
        "INSERT INTO bill_rows (ch_id, channel, amount_text) VALUES (1, 'his', '999.00')"
    )
    conn.execute("DELETE FROM bill_rows")

    snap = latest_alignment_snapshot(conn, PERIOD, f"wechat:{PERIOD}:ex-1")
    assert snap is not None
    result = replay_snapshot(snap)
    assert not result.replay_mismatch
    assert result.replayed_kind == "matched"


def test_replay_agrees_for_ambiguity_and_weak_match_too():
    """歧义与弱匹配也必须能复算一致 —— 它们恰好是最容易被复算翻掉的。"""
    ambiguous = _only_outcome(
        _chan("EX-2", "100.00", row=4),
        his_records=[
            _his("EX-2", "50.00", row=5, ch_id=1),
            _his("EX-2", "50.00", row=6, ch_id=2),
        ],
    )
    assert ambiguous.status == "ambiguous"
    assert not replay_snapshot(snapshot_from_outcome(ambiguous)).replay_mismatch

    weak = _only_outcome(_chan("EX-9", "77.00", row=12), his_records=[
        _his("EX-OTHER", "77.00", row=13, ch_id=1),
    ])
    assert weak.status == "weak_pending"
    result = replay_snapshot(snapshot_from_outcome(weak))
    assert not result.replay_mismatch
    assert result.replayed_kind == "weak_pending"


# ───────────────── 四、复算不一致标 replay_mismatch ─────────────────


def test_replay_mismatch_when_the_recorded_amount_no_longer_reconciles():
    """差额被改写后复算不一致，且逐条列出变化点。"""
    outcome = _only_outcome(
        _chan("EX-1", "130.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    assert outcome.status == "mismatch" and outcome.diff_amount == "2.00"
    tampered = EvidenceSnapshot(
        conclusion_key=conclusion_key_of(outcome),
        period=PERIOD,
        channel="wechat",
        kind="mismatch",
        order_no="EX-1",
        diff_amount="0.00",  # 有人把差额抹平了
        participants=snapshot_from_outcome(outcome).participants,
    ).with_hash()
    result = replay_snapshot(tampered)
    assert result.replay_mismatch
    fields = {c.field_name for c in result.changes}
    assert "差额" in fields
    diff_change = next(c for c in result.changes if c.field_name == "差额")
    assert diff_change.recorded == "0.00" and diff_change.replayed == "2.00"
    text = result.render()
    assert "✗ 复算不一致" in text and "差额" in text


def test_replay_mismatch_when_the_candidate_set_was_dropped():
    """候选行被抽掉一条 → 判定从「拒绝归并」翻成「精确命中」。"""
    outcome = _only_outcome(
        _chan("EX-2", "100.00", row=4),
        his_records=[
            _his("EX-2", "50.00", row=5, ch_id=1),
            _his("EX-2", "50.00", row=6, ch_id=2),
        ],
    )
    full = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
    assert full.participant_total == 3
    stripped = EvidenceSnapshot(
        conclusion_key=full.conclusion_key,
        period=full.period,
        channel=full.channel,
        kind=full.kind,
        order_no=full.order_no,
        detail=full.detail,
        diff_amount=full.diff_amount,
        participants=full.participants[:2],  # 只留渠道行 + 第一条候选
        contract_digest=full.contract_digest,
    ).with_hash()
    result = replay_snapshot(stripped)
    assert result.replay_mismatch
    assert any(c.field_name == "判定状态" for c in result.changes)


def test_ambiguity_kind_change_is_reported_even_when_the_status_does_not():
    """状态没变（都是 ambiguous）但成因变了，同样要报。

    「退费与原单并存」被说成「一码付拆多笔」，经办人会去查拆分说明，
    而实际该查的是红冲是否生效 —— 话术错比状态错更误导。
    """
    outcome = _only_outcome(
        _chan("EX-2", "100.00", row=4),
        his_records=[
            _his("EX-2", "50.00", row=5, ch_id=1),
            _his("EX-2", "50.00", row=6, ch_id=2),
        ],
    )
    snap = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
    # 两条 50 元候选、渠道行 100 元：形态是「一码付拆多笔」
    assert snap.ambiguity_kind == "split"
    tampered = EvidenceSnapshot(
        conclusion_key=snap.conclusion_key,
        period=snap.period,
        channel=snap.channel,
        kind=snap.kind,
        order_no=snap.order_no,
        detail=snap.detail,
        diff_amount=snap.diff_amount,
        ambiguity_kind="equal_amount",
        participants=snap.participants,
    ).with_hash()
    result = replay_snapshot(tampered)
    assert result.replay_mismatch
    change = next(c for c in result.changes if c.field_name == "歧义形态")
    assert change.recorded == "equal_amount"
    assert change.replayed == "split"


def test_ambiguity_kind_is_a_field_not_parsed_back_out_of_the_prose():
    """形态存成独立字段，**不**从 detail 中文文案里反解。

    形态决定建议话术，从散文里抠字回来等于把一条业务判据绑在措辞上 ——
    改一次话术就静默改掉复算的判定基准。这里把 detail 换成完全不同的
    中文，复算仍必须按 ``ambiguity_kind`` 比。
    """
    outcome = _only_outcome(
        _chan("EX-2", "100.00", row=4),
        his_records=[
            _his("EX-2", "50.00", row=5, ch_id=1),
            _his("EX-2", "50.00", row=6, ch_id=2),
        ],
    )
    snap = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
    rewritten = EvidenceSnapshot(
        conclusion_key=snap.conclusion_key,
        period=snap.period,
        channel=snap.channel,
        kind=snap.kind,
        order_no=snap.order_no,
        detail="完全换掉的一段中文，里面没有任何形态名",
        diff_amount=snap.diff_amount,
        ambiguity_kind=snap.ambiguity_kind,
        participants=snap.participants,
    ).with_hash()
    assert not replay_snapshot(rewritten).replay_mismatch
    # matched / channel_only 没有形态，空串是正常值而不是缺失
    plain = _only_outcome(_chan("EX-1", "66.00"), his_records=[])
    assert snapshot_from_outcome(plain).ambiguity_kind == ""


def test_refund_flag_survives_the_snapshot_round_trip():
    """退费标记必须还原得回去。

    候选行只带净额、不带退款列；不补回退费标记的话，一条红冲行复算时
    会变成普通收费，「退费与原单并存」被判成「一码付拆多笔」—— 状态
    还是 ambiguous、看着没差，建议话术却把经办人引去查拆分说明，
    而实际该查的是红冲是否生效。
    """
    outcome = _only_outcome(
        _chan("EX-7", "128.00", row=4),
        his_records=[
            _his("EX-7", "86.00", row=5, ch_id=1),
            # 红冲行：金额为负（院内 HIS 的红冲形态）
            _his("EX-7", "-42.00", row=6, ch_id=2, time="2026-09-03 11:00:00"),
        ],
    )
    assert outcome.status == "ambiguous"
    snap = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
    assert snap.ambiguity_kind == "refund_and_original"
    result = replay_snapshot(snap)
    assert not result.replay_mismatch, [c.render() for c in result.changes]


def test_refund_flag_from_a_trade_type_column_survives_the_round_trip():
    """红冲标记来自**交易类型列**（金额为正）的那种形态，同样要还原。

    院内 HIS 流水有独立的 ``is_refund`` 列，落库后进 ``trade_type``，
    那一行金额是红冲后的**正**净额、退款列为空。只看「金额为负」的那条
    判据会漏掉这种行，于是它在复算时变成普通收费。
    """
    outcome = _only_outcome(
        _chan("EX-8", "128.00", row=4),
        his_records=[
            _his("EX-8", "86.00", row=5, ch_id=1),
            _his("EX-8", "42.00", row=6, ch_id=2, time="2026-09-03 11:00:00",
                 trade_type="红冲"),
        ],
    )
    assert outcome.status == "ambiguous"
    assert outcome.ambiguity is not None
    assert outcome.ambiguity.kind == "refund_and_original"
    snap = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
    # 快照里那条红冲候选确实带上了标记，且金额是 42.00（不是负数）
    flagged = [r for r in snap.his_refs if r.is_refund]
    assert len(flagged) == 1 and flagged[0].amount == "42.00"
    result = replay_snapshot(snap)
    assert not result.replay_mismatch, [c.render() for c in result.changes]
    assert result.replayed_kind == "ambiguous"


def test_contract_fingerprint_change_is_reported_separately_from_the_verdict():
    """口径指纹变化**不混进** changes。

    混在一起会让排障的人先去查数据，而真因可能是上周改了口径 ——
    「比较的前提变了」与「结论本身翻了」必须能分开看。
    """
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    snap = snapshot_from_outcome(outcome, contract_digest=DIGEST_A, created_at=FIXED_NOW)
    result = replay_snapshot(snap, current_contract_digest=DIGEST_B)
    assert result.contract_changed
    assert not result.replay_mismatch, "指纹变了但结论没变，不该报复算不一致"
    assert result.change_total == 0
    assert "口径指纹已变" in result.render()
    # 指纹没变则不报
    same = replay_snapshot(snap, current_contract_digest=DIGEST_A)
    assert not same.contract_changed


def test_index_version_change_is_reported_separately_too():
    outcome = _only_outcome(
        _chan("EX-1", "128.00"), his_records=[_his("EX-1", "128.00", ch_id=1)]
    )
    snap = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
    result = replay_snapshot(snap, current_index_version="sharded-inverted/2")
    assert result.index_version_changed and not result.replay_mismatch
    assert "索引版本已变" in result.render()


def test_cross_channel_trace_conclusion_states_it_cannot_be_replayed():
    """连坐结论跨渠道/跨账期，判定器输入是单行渠道账单 → 明说不可复算。"""
    conclusion = TraceConclusion(
        kind=TraceKind.DUPLICATE_ACROSS_CHANNEL.value,
        order_no="EX-3",
        detail="账期内同一渠道单号被两路渠道各认领一次",
        diff_amount="20.00",
        period=PERIOD,
    )
    nodes = [
        TraceNode(period=PERIOD, channel="wechat", order_no="EX-3", amount=Decimal("100.00"),
                  side="channel", source_file="wx.csv", source_row_no=3),
        TraceNode(period=PERIOD, channel="self_service", order_no="EX-3", amount=Decimal("100.00"),
                  side="channel", source_file="ss.csv", source_row_no=4),
    ]
    snap = snapshot_from_trace_conclusion(
        conclusion, period=PERIOD, order_no="EX-3", nodes=nodes, created_at=FIXED_NOW
    )
    assert snap.channel == GLOBAL_CHANNEL
    assert snap.participant_total == 2
    result = replay_snapshot(snap)
    assert not result.replayable
    assert not result.replay_mismatch, "不可复算不等于复算不一致，不能拿它开门禁"
    assert "2 条渠道行引用" in result.note
    assert "不可复算" in result.render()


# ───────────────────────── 五、脱敏 ─────────────────────────


def test_snapshot_fields_are_whitelisted():
    """字段用白名单约束：黑名单要靠人记得列全，漏一个就落进库了。"""
    assert "amount" in SNAPSHOT_ALLOWED_FIELDS
    assert not (SNAPSHOT_ALLOWED_FIELDS & FORBIDDEN_PATIENT_KEYS)
    for name in ("patient_name", "id_card", "phone", "inpatient_no", "姓名", "住院号"):
        assert name in FORBIDDEN_PATIENT_KEYS


def test_snapshot_carries_no_patient_identifiable_fields():
    """真实样例上的快照逐字段核对：只有金额、单号、账期、科室、时间。"""
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    payload = snapshot_from_outcome(outcome, created_at=FIXED_NOW).to_dict()
    blob = json.dumps(payload, ensure_ascii=False)
    for word in ("患者", "姓名", "身份证", "手机号", "住院号", "床号", "病案号"):
        assert word not in blob, f"快照里出现了 {word}"
    # 白名单逐字段过一遍
    for ref in payload["participants"]:
        assert set(ref) - {"ref"} <= SNAPSHOT_ALLOWED_FIELDS
    assert_deidentified(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"patient_name": "张某"},
        {"住院号": "0001234"},
        {"note": "13800138000"},
        {"memo": "110101199001011234"},
        {"items": [{"id_card": "110101199001011234"}]},
        {"items": [{"phone": "13900000001"}]},
    ],
)
def test_deidentified_guard_rejects_patient_identifiers(payload):
    """键名命中**或**值长得像，都拒 —— 导出方把个人信息写进备注列很常见。"""
    with pytest.raises(ReconqError) as exc:
        assert_deidentified(payload)
    assert exc.value.code == "SNAPSHOT_NOT_DEIDENTIFIED"
    assert "不落快照" in exc.value.render() or "不落患者可识别信息" in exc.value.render()


def test_deidentified_guard_does_not_reject_ordinary_order_numbers():
    """别把正常的 18 位订单号也拦掉：拦死了就是「宁可不查」而非「更安全」。"""
    assert_deidentified({"order_no": "WX202609070000000001"})
    assert_deidentified({"order_no": "20260907000001"})
    assert_deidentified({"amount": "128.00"})


# ───────────────────────── 六、落盘与 CLI ─────────────────────────


def test_snapshots_round_trip_through_the_database():
    conn = _db()
    outcome = _only_outcome(
        _chan("EX-1", "130.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    snap = snapshot_from_outcome(outcome, contract_digest=DIGEST_A, created_at=FIXED_NOW)
    out = record_alignment_snapshot(conn, snap)
    loaded = latest_alignment_snapshot(conn, PERIOD, snap.conclusion_key)
    assert loaded is not None
    assert loaded.to_dict() == snap.to_dict()
    assert loaded.content_hash == snap.content_hash
    assert out.snap_id > 0
    assert count_alignment_snapshots(conn, period=PERIOD) == 1
    assert count_alignment_snapshots(conn, period="2026-W99") == 0


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH="src")
    return subprocess.run(
        [sys.executable, "-m", "reconq", *argv],
        capture_output=True, text=True, env=env,
    )


def test_cli_replay_list_and_replay_run_against_a_real_db(tmp_path):
    """CLI 两条腿端到端：列出快照 → 复算某一条。"""
    conn = sqlite3.connect(str(tmp_path / "reconq.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_align_snapshot_schema(conn)
    outcome = _only_outcome(
        _chan("EX-1", "128.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    record_alignment_snapshot(
        conn, snapshot_from_outcome(outcome, contract_digest=DIGEST_A, created_at=FIXED_NOW)
    )
    conn.close()

    listed = _run_cli(
        "verify", "--period", PERIOD, "--db", str(tmp_path / "reconq.db"), "--replay-list"
    )
    assert listed.returncode == 0, listed.stderr
    assert "已拍证据快照 1 条" in listed.stdout
    assert f"wechat:{PERIOD}:ex-1" in listed.stdout

    replayed = _run_cli(
        "verify", "--period", PERIOD, "--db", str(tmp_path / "reconq.db"),
        "--replay", f"wechat:{PERIOD}:ex-1",
    )
    assert replayed.returncode == 0, replayed.stderr
    assert "✓ 复算与快照记录一致" in replayed.stdout
    assert "wechat_bill.csv:3" in replayed.stdout
    assert "charge_record.csv:17" in replayed.stdout

    as_json = _run_cli(
        "verify", "--period", PERIOD, "--db", str(tmp_path / "reconq.db"),
        "--replay", f"wechat:{PERIOD}:ex-1", "--json",
    )
    payload = json.loads(as_json.stdout)
    assert payload["replay_mismatch"] is False
    assert payload["replayed_kind"] == "matched"


def test_cli_replay_reports_mismatch_with_nonzero_exit(tmp_path):
    """复算不一致要非零退出：门禁语义与 verify / scan 同一套。"""
    db = str(tmp_path / "reconq.db")
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_align_snapshot_schema(conn)
    outcome = _only_outcome(
        _chan("EX-1", "130.00", row=3),
        his_records=[_his("EX-1", "128.00", row=17, ch_id=1)],
    )
    base = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
    tampered = EvidenceSnapshot(
        conclusion_key=base.conclusion_key, period=base.period, channel=base.channel,
        kind=base.kind, order_no=base.order_no, diff_amount="0.00",
        participants=base.participants,
    ).with_hash()
    record_alignment_snapshot(conn, tampered)
    conn.close()

    proc = _run_cli("verify", "--period", PERIOD, "--db", db, "--replay", base.conclusion_key)
    assert proc.returncode == 1, proc.stdout
    assert "✗ 复算不一致" in proc.stdout
    assert "差额" in proc.stdout


def test_cli_replay_without_db_fails_with_a_named_missing_store(tmp_path):
    proc = _run_cli("verify", "--period", PERIOD, "--replay", "wechat:2026-W36:ex-1")
    assert proc.returncode == 2
    assert "SNAPSHOT_NOT_FOUND" in proc.stderr or "账单库" in proc.stderr


def test_cli_replay_unknown_key_names_the_available_count(tmp_path):
    db = str(tmp_path / "reconq.db")
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_align_snapshot_schema(conn)
    conn.close()
    proc = _run_cli("verify", "--period", PERIOD, "--db", db, "--replay", "wechat:2026-W36:nope")
    assert proc.returncode == 2
    assert "replay-list" in proc.stderr


# ───────────────── 七、真实样例端到端（走主路径，不手工构造） ─────────────────

ALIGN_FIXTURES = "tests/fixtures/align/2026-W36"


def _load_fixture(channel: str, relative: str):
    from reconq.align.matcher import records_from_bill
    from reconq.contracts.loader import load_contract
    from reconq.sources.format_probe import read_bill_file_isolated
    from reconq.store.bill_store import build_record

    contract = load_contract(
        f"contracts/{channel}.yml",
        {"period": PERIOD, "session_timezone": "Asia/Shanghai"},
    )
    rows, _probe, _dirty = read_bill_file_isolated(
        f"{ALIGN_FIXTURES}/{relative}", contract.dataset.tables[0], channel, period=PERIOD
    )
    return records_from_bill(
        build_record(
            r, channel=channel, period=PERIOD,
            window=period_window(PERIOD, TZ), session_tz=TZ,
        )
        for r in rows
    )


def test_real_fixtures_produce_replayable_snapshots_for_every_pending_conclusion():
    """真实样例上：七种状态逐条拍快照，逐条复算一致。

    刻意走主路径（契约 → 文件读入 → 归一 → 对齐），不手工构造 dataclass ——
    手工构造会把契约变量注入、编码探测、列名别名、金额定点化全绕过去。
    """
    from reconq.align.matcher import align_all

    his_rows = _load_fixture("his", "his/charge_record_2026-W36.csv")
    chans = {
        "wechat": _load_fixture("wechat", "wechat/wechat_tradebill_2026-W36.csv"),
        "alipay": _load_fixture("alipay", "alipay/alipay_bill_2026-W36.csv"),
    }
    res = align_all(chans, his_rows, period=PERIOD, window=period_window(PERIOD, TZ))

    conn = _db()
    replayed = 0
    kinds = set()
    for result in res.channels.values():
        for outcome in result.outcomes:
            snap = snapshot_from_outcome(
                outcome, contract_digest=DIGEST_A, created_at=FIXED_NOW
            )
            assert snap.content_hash
            assert snap.participant_total >= 1
            # 每条待归因结论都必须能追到源文件行号
            if outcome.needs_attribution:
                assert outcome.channel_record is not None
                assert snap.channel_refs[0].source_file
                assert snap.channel_refs[0].source_row_no > 0
            record_alignment_snapshot(conn, snap)
            back = replay_snapshot(snap)
            assert back.replayable, back.note
            assert not back.replay_mismatch, [
                c.render() for c in back.changes
            ]
            assert back.replayed_kind == outcome.status
            replayed += 1
            kinds.add(outcome.status)
    assert replayed >= 8
    assert {"matched", "ambiguous", "channel_only", "weak_pending"} <= kinds
    # 全部落盘，逐条可查
    assert count_alignment_snapshots(conn, period=PERIOD) == replayed
    for snap in load_alignment_snapshots(conn, period=PERIOD):
        assert snap.participant_total >= 1
        assert snap.created_at == FIXED_NOW


def test_real_fixture_snapshot_amounts_are_decimal_strings_without_float_residue():
    """快照里的金额不得带浮点残影：0.1+0.2 那类一分钱事故的入口在这里。"""
    from reconq.align.matcher import align_all

    his_rows = _load_fixture("his", "his/charge_record_2026-W36.csv")
    res = align_all(
        {"wechat": _load_fixture("wechat", "wechat/wechat_tradebill_2026-W36.csv")},
        his_rows, period=PERIOD, window=period_window(PERIOD, TZ),
    )
    for result in res.channels.values():
        for outcome in result.outcomes:
            snap = snapshot_from_outcome(outcome, created_at=FIXED_NOW)
            for ref in snap.participants:
                assert isinstance(ref.amount, str)
                assert Decimal(ref.amount) == Decimal(ref.amount)  # 可精确解析
                assert "e" not in ref.amount.lower(), "科学计数法不得进快照"
                if outcome.diff_amount:
                    assert Decimal(snap.diff_amount) == Decimal(snap.diff_amount)
