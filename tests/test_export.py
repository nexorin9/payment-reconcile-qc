"""周对账结论单导出与签字版式归档（Task 44）。

六类规定断言逐条可核：

1. **双格式生成** —— ``md``（打印签字）与 ``csv``（回填既有电子台账）
   两条腿都跑得通，文件真的落在磁盘上；CSV 是 ``utf-8-sig``（Excel
   双击能开）；CSV 的处置单条数与 Markdown 的组数对得上。
2. **指纹头字段齐全** —— 账期、账期状态与门禁、六路主态与笔数合计、
   事件链根指纹与环数、口径指纹、模型与提示词版本、各版本号、结论集
   指纹、导出人与导出时刻，逐项断言在，且都出现在两种格式里。
3. **分组版式正确** —— 按成因分组出证明：组级结论、组内条数、涉及
   金额、确认人、确认时间、复核人；组内逐条附证据行号与快照引用。
   组内经办结论不一致时**如实说不一致**，不替它们取一条当组结论。
4. **附录四类齐** —— 跨账期连坐链、未取到渠道及错误码、脏行统计与
   隔离区文件指针、模型降级说明与规则基线占比，逐类断言。
5. **重复导出一致** —— 同账期同内容两次导出，文件内容与结论集指纹
   **完全一致**；导出人 / 导出时刻不同也**不改指纹**（它们是本次导出
   的元数据，不是结论的一部分）；账期状态一变，内容与指纹**同时变**。
6. **无患者标识** —— 落盘前过脱敏核对：结论单全文不含身份证 / 手机号
   形态，也不含患者姓名 / 住院号等键名；故意把带患者信息的经办结论
   塞进台账，导出必须**报错而不是写出去**。

外加几条容易被糊弄过去的性质：空账期导出不炸且明说「本期无处置单」；
未登记的格式被显式拒；CLI 两条腿端到端跑通且门禁没过时退出码非零。
"""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import subprocess
import sys

import pytest

from reconq.engine.period import (
    DISPOSITION_CONFIRMED,
    DISPOSITION_PENDING,
    SCAN_TABLE,
    STATUS_CONFIRMED,
    STATUS_ZH,
    ChannelScan,
    DiffItem,
    init_scan_schema,
    upsert_channel_results,
    upsert_diff_items,
)
from reconq.errors import ReconqError
from reconq.export.conclusion import (
    CONCLUSION_FORMATS,
    CSV_COLUMNS,
    build_conclusion,
    conclusion_fingerprint,
    export_conclusion,
    render_conclusion_csv,
    render_conclusion_markdown,
)
from reconq.ledger import flow as ledger_flow
from reconq.ledger.models import Role, init_ledger_schema

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

PERIOD = "2026-W36"
HANDLER = "经办-张工"
REVIEWER = "审核-李工"

#: 三组真实场景的单边账：8 + 7 + 5 = 20 条，合计 870.00 元
#: （与 :mod:`tests.test_aggregate` 同一套现场，避免两处各拍各的数）。
SCENARIO = [
    ("channel_late_posting", "wechat", "cross_period", "50.00", 8, "EX-WX-LATE"),
    ("channel_late_posting", "alipay", "cross_period", "20.00", 7, "EX-ZFB-LATE"),
    ("his_missing_order", "self_service", "", "66.00", 5, "EX-SS-MISS"),
]

#: 结论单上**不得**出现的形态。身份证 18 位、手机号 11 位 1 开头。
ID_CARD_SHAPE = re.compile(r"\b\d{17}[\dXx]\b")
PHONE_SHAPE = re.compile(r"\b1\d{10}\b")
#: 结论单上不得出现的患者标识键名 / 字样。
PATIENT_WORDS = ("住院号", "病案号", "身份证", "患者姓名", "门诊号", "床号")


# ─────────────────────────── 夹具 ───────────────────────────


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    init_scan_schema(connection)
    init_ledger_schema(connection)
    _seed_channels(connection)
    _seed_diffs(connection)
    yield connection
    connection.close()


def _seed_channels(connection, *, missing=("medical_ins",)) -> None:
    """六路主态：五路已取到、一路未取到（``medical_ins`` 本期没出账）。"""
    channels = [
        ChannelScan(channel="wechat", status="checked", row_count=1200, total_amount="86400.00",
                    checks_total=9, checks_failed=2, dirty_rows=3),
        ChannelScan(channel="alipay", status="checked", row_count=900, total_amount="61200.00",
                    checks_total=9, checks_failed=1, dirty_rows=0),
        ChannelScan(channel="unionpay", status="checked", row_count=410, total_amount="30500.00",
                    checks_total=8, checks_failed=0),
        ChannelScan(channel="self_service", status="checked", row_count=260, total_amount="17160.00",
                    checks_total=8, checks_failed=1, dirty_rows=1),
        ChannelScan(channel="his", status="checked", row_count=2770, total_amount="195260.00",
                    checks_total=11, checks_failed=3),
        ChannelScan(channel="medical_ins", status="not_fetched", row_count=0, total_amount="0.00",
                    checks_total=0, checks_failed=0, error_code="BILL_NOT_EXIST",
                    detail="医保平台未出具 2026-W36 结算清单"),
    ]
    upsert_channel_results(connection, PERIOD, channels)


def _seed_diffs(connection) -> None:
    """把 20 条单边账写进差异台账，并落 Top-1 归因草稿与三角色会签。"""
    from reconq.align.aggregate import diff_key
    from reconq.gateway.schema import AttributionDraft, EvidenceRef
    from reconq.store.attribution_store import init_attribution_schema, save_drafts

    items = []
    for cause, channel, kind, amount, count, prefix in SCENARIO:
        for index in range(1, count + 1):
            order_no = f"{prefix}-{index:04d}"
            check_path = f"taxonomy.ambiguity:{order_no}"
            source_file = f"{channel}_bill_2026-W36.csv"
            row_no = index + 1
            items.append(
                DiffItem(
                    period=PERIOD,
                    channel=channel,
                    check_path=check_path,
                    check_name="歧义成因",
                    diff_amount=amount,
                    evidence_total=1,
                    source_file=source_file,
                    first_row_no=row_no,
                    disposition=DISPOSITION_PENDING,
                    needs_attribution=True,
                    ambiguity_kind=kind,
                )
            )
            _sign_off(
                connection,
                diff_key(PERIOD, channel, check_path),
                order_no=order_no,
                channel=channel,
                amount=amount,
                evidence=(f"{source_file}:{row_no}",),
                # 组内共用同一句结论：同因同渠道同金额本来就是同一件事，
                # 经办人签的话也是同一句。组级结论取的就是这一句。
                conclusion=f"与{cause}成因一致，已与收费处逐笔核对",
            )
    upsert_diff_items(connection, items)

    init_attribution_schema(connection)
    for cause, channel, kind, amount, count, prefix in SCENARIO:
        for index in range(1, count + 1):
            order_no = f"{prefix}-{index:04d}"
            save_drafts(
                connection,
                [
                    AttributionDraft(
                        cause=cause,
                        confidence=0.82,
                        evidence_refs=[
                            EvidenceRef(ref=f"{channel}_bill_2026-W36.csv:{index + 1}")
                        ],
                        explanation_zh=f"{order_no} 属 {cause}，待人工确认",
                    )
                ],
                period=PERIOD,
                channel=channel,
                conclusion_key=f"{channel}:{PERIOD}:{order_no}",
            )


def _sign_off(connection, key, *, order_no, channel, amount, evidence, conclusion="") -> None:
    """开单 → 经办提交 → 审核复核，两级会签走完。"""
    item = ledger_flow.open_discrepancy(
        connection,
        period=PERIOD,
        channel=channel,
        check_path=key.split("|", 2)[2],
        order_no=order_no,
        diff_amount=amount,
        evidence_rows=evidence,
    )
    current = ledger_flow.require(connection, key)
    ledger_flow.submit(
        connection,
        key,
        conclusion or "已与收费处核对",
        operator=HANDLER,
        expected_version=current.version,
    )
    current = ledger_flow.require(connection, key)
    ledger_flow.approve(
        connection,
        key,
        operator=REVIEWER,
        expected_version=current.version,
        role=Role.REVIEWER,
    )


def _append_scan_event(connection, period=PERIOD):
    from reconq.engine.resume import append_scan_event, init_resume_schema

    init_resume_schema(connection)
    return append_scan_event(connection, period, "scan.finished", run_id="EX-RUN-0001", actor="编排")


def _append_trace_conclusion(connection, period=PERIOD) -> None:
    """台账里落一条跨账期连坐结论（``check_path`` 以 ``trace.`` 开头）。"""
    upsert_diff_items(
        connection,
        [
            DiffItem(
                period=period,
                channel="*",
                check_path="trace.duplicate_across_period:EX-WX-LATE-0001",
                check_name="跨账期重复计入",
                diff_amount="50.00",
                evidence_total=4,
                disposition=DISPOSITION_PENDING,
                needs_attribution=True,
                message="同一单号在相邻账期与本渠道各出现一次，金额相等",
            )
        ],
    )


def _append_dirty(connection, period=PERIOD) -> None:
    from reconq.store.quarantine import QuarantineRow, init_dirty_schema, insert_dirty_rows

    init_dirty_schema(connection)
    insert_dirty_rows(
        connection,
        [
            QuarantineRow(
                channel="wechat",
                period=period,
                source_file="wechat_bill_2026-W36.csv",
                row_no=118,
                reason_code="AMOUNT_UNPARSABLE",
                raw_line="该行金额列非定点数",
                detail="amount 列不是定点数",
            )
        ],
    )


def _read(path: str, encoding: str = "utf-8") -> str:
    with open(path, encoding=encoding) as handle:
        return handle.read()


def _csv_rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


# ─────────────────── 1. 双格式生成 ───────────────────


def test_both_formats_are_written_to_disk(conn, tmp_path):
    """md 与 csv 两条腿都落盘，且各自的编码与内容对得上。"""
    md_out = export_conclusion(
        conn, PERIOD, fmt="md", out_path=str(tmp_path / "conclusion.md"),
        exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
    )
    csv_out = export_conclusion(
        conn, PERIOD, fmt="csv", out_path=str(tmp_path / "conclusion.csv"),
        exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
    )

    assert os.path.isfile(md_out.path) and md_out.size > 0
    assert os.path.isfile(csv_out.path) and csv_out.size > 0
    assert md_out.fmt == "md" and csv_out.fmt == "csv"
    assert list(CONCLUSION_FORMATS) == ["md", "csv"]

    md_text = _read(md_out.path)
    assert md_text.startswith(f"# {PERIOD} 周对账结论单")
    # CSV 用 utf-8-sig：BOM 在，Excel 双击不乱码
    with open(csv_out.path, "rb") as handle:
        assert handle.read(3) == b"\xef\xbb\xbf"
    rows = _csv_rows(_read(csv_out.path, "utf-8-sig"))
    assert rows and list(rows[0].keys()) == list(CSV_COLUMNS)

    # 两种格式讲的是同一件事：处置单张数 / 差异条数对得上
    md = md_out.doc
    group_rows = [r for r in rows if r["区块"] == "处置单"]
    line_rows = [r for r in rows if r["区块"] == "差异明细"]
    assert len(group_rows) == md.groups_count == 3
    assert len(line_rows) == md.lines_count == 20
    assert f"{md.groups_count} 张处置单" in md_text
    assert f"覆盖差异 {md.lines_count} 条" in md_text


def test_default_out_path_is_under_out_dir(conn, tmp_path, monkeypatch):
    """不给 ``--out`` 时落在 ``out/<账期>-conclusion.<fmt>``。"""
    monkeypatch.chdir(tmp_path)
    outcome = export_conclusion(conn, PERIOD, fmt="md")
    assert outcome.path == os.path.join("out", f"{PERIOD}-conclusion.md")
    assert os.path.isfile(os.path.join(tmp_path, "out", f"{PERIOD}-conclusion.md"))


def test_unknown_format_is_rejected_explicitly(conn, tmp_path):
    with pytest.raises(ReconqError) as excinfo:
        export_conclusion(conn, PERIOD, fmt="pdf", out_path=str(tmp_path / "x.pdf"))
    assert excinfo.value.code == "EXPORT_FORMAT_UNKNOWN"
    assert not os.path.exists(str(tmp_path / "x.pdf"))


def test_empty_period_is_rejected(conn):
    with pytest.raises(ReconqError) as excinfo:
        build_conclusion(conn, "  ")
    assert excinfo.value.code == "EXPORT_PERIOD_REQUIRED"


# ─────────────────── 2. 指纹头字段齐全 ───────────────────


def test_fingerprint_header_carries_every_required_field(conn, tmp_path):
    """指纹头九项逐条断言，且两种格式都带。"""
    _append_scan_event(conn)
    from reconq.store.snapshots import (
        BINDING_TABLE,
        NEEDS_RECONFIRM,
        init_snapshot_schema,
    )

    init_snapshot_schema(conn)
    conn.execute(
        f"INSERT INTO {BINDING_TABLE} "
        "(period, channel, contract_digest, check_digest, status, note, bound_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (PERIOD, "wechat", "a" * 64, "b" * 64, NEEDS_RECONFIRM,
         "口径指纹已变更", "2026-09-06T09:00:00"),
    )
    conn.execute(
        f"INSERT INTO {BINDING_TABLE} "
        "(period, channel, contract_digest, check_digest, status, note, bound_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (PERIOD, "alipay", "c" * 64, "d" * 64, "scanned", "", "2026-09-06T09:00:00"),
    )

    md = render_conclusion_markdown(
        build_conclusion(conn, PERIOD, exported_by=HANDLER, now="2026-09-07T10:00:00+08:00")
    )
    csv_text = render_conclusion_csv(
        build_conclusion(conn, PERIOD, exported_by=HANDLER, now="2026-09-07T10:00:00+08:00")
    )

    head = md.split("## 一、")[0]
    # ① 账期
    assert f"- 账期：{PERIOD}" in head
    # ② 六路主态与笔数合计
    assert "- 六路取数：5/6 路已取到，合计" in head
    assert "390520.00 元" in head  # 86400+61200+30500+17160+195260
    # ③ 事件链根指纹与环数
    assert "本期事件链根指纹：" in head and "1 环" in head
    # ④ 契约指纹
    assert "本期口径指纹：alipay:dddddddddddd、wechat:bbbbbbbbbbbb" in head
    # ⑤ 模型与提示词版本（本期没调模型也要显式写「未调」）
    assert "- 模型 / 提示词：（本期未调模型） / （无）" in head
    # ⑥ 各版本号
    assert "结论单 conclusion-sheet/1" in head
    assert "会签 discrepancy-ledger/1" in head
    assert "聚合 same-cause/1" in head
    assert "成因字典 cause-dict/1+" in head
    # ⑦ 结论集指纹
    assert f"- 结论集指纹：`{build_conclusion(conn, PERIOD).fingerprint}`" in head
    # ⑧ 导出人与导出时刻
    assert f"- 导出人：{HANDLER} · 导出时刻：2026-09-07T10:00:00+08:00" in head

    # CSV 版同样带齐（整表导入，不靠 Markdown 才有）
    fields = {r["字段"]: r["值"] for r in _csv_rows(csv_text) if r["区块"] == "指纹头"}
    assert fields["账期"] == PERIOD
    assert fields["六路取数"] == "5/6"
    assert fields["涉及金额"] == "870.00"
    assert fields["差异条数"] == "20"
    assert fields["处置单张数"] == "3"
    assert fields["导出人"] == HANDLER
    assert fields["导出时刻"] == "2026-09-07T10:00:00+08:00"
    assert fields["结论集指纹"] == build_conclusion(conn, PERIOD).fingerprint
    assert fields["事件链环数"] == "1"


def test_period_status_and_gate_appear_in_header(conn):
    conn.execute(
        f"INSERT OR REPLACE INTO {SCAN_TABLE} (period, status, gate, reason) VALUES (?,?,?,?)",
        (PERIOD, STATUS_CONFIRMED, "green", "六路取到且无未确认差异"),
    )
    doc = build_conclusion(conn, PERIOD)
    md = render_conclusion_markdown(doc)
    assert f"账期状态：{STATUS_ZH[STATUS_CONFIRMED]} · 门禁 green" in md
    assert "六路取到且无未确认差异" in md


# ─────────────────── 3. 分组版式正确 ───────────────────


def test_groups_render_proof_and_per_line_evidence(conn):
    """按成因分组出证明：组级结论、条数、金额、确认人、确认时间、复核人。"""
    doc = build_conclusion(conn, PERIOD, exported_by=HANDLER)
    md = render_conclusion_markdown(doc)

    assert doc.groups_count == 3
    assert doc.lines_count == 20
    assert doc.total_amount == __import__("decimal").Decimal("870.00")

    # 三个组里有两个同成因（渠道延迟入账 × wechat / alipay），
    # 按成因取会取到后一个 —— 按渠道取才对得上「这一单是哪一路的」。
    wechat_group = next(
        g for g in doc.groups if g.channel == "wechat" and g.size == 8
    )
    assert wechat_group.cause_zh == "渠道延迟入账"
    assert wechat_group.channel == "wechat"
    assert wechat_group.size == 8
    assert wechat_group.total_amount_text == "400.00"
    assert wechat_group.owner  # 责任方非空（交办分工表的依据）
    assert wechat_group.suggested_action

    # 组头：组级结论 / 条数 / 金额 / 经办 / 复核
    head = md.split(f"处置单键：`{wechat_group.group_key}`", 1)[1][:800]
    assert f"经办：{HANDLER}" in head
    assert f"复核：{REVIEWER}" in head
    assert "经办-张工（20" in head  # 经办时间也带出来了
    assert f"本单合计：400.00 元（8 条）" in head
    assert "组级结论：" in head

    # 组内逐条：证据行号 + 结论键 + 快照引用位
    body = md.split(f"处置单键：`{wechat_group.group_key}`", 1)[1]
    assert "wechat_bill_2026-W36.csv:2" in body
    assert "`2026-W36|wechat|taxonomy.ambiguity:EX-WX-LATE-0001`" in body
    # 没有快照时该列显式写「—」，不留空格让人以为漏了
    assert "| — |" in body


def test_snapshot_ref_is_rendered_when_snapshot_exists(conn):
    """拍过快照的结论，逐条那一行给出快照引用。"""
    from reconq.align.snapshot import (
        EvidenceSnapshot,
        SnapshotRef,
        init_align_snapshot_schema,
        latest_alignment_snapshot,
        record_alignment_snapshot,
    )

    init_align_snapshot_schema(conn)
    key = "wechat:2026-W36:EX-WX-LATE-0001"
    written = record_alignment_snapshot(
        conn,
        EvidenceSnapshot(
            conclusion_key=key,
            period=PERIOD,
            channel="wechat",
            kind="channel_only",
            order_no="EX-WX-LATE-0001",
            diff_amount="50.00",
            participants=(
                SnapshotRef(
                    side="channel", channel="wechat", period=PERIOD,
                    order_no="EX-WX-LATE-0001", amount="50.00",
                    source_file="wechat_bill_2026-W36.csv", source_row_no=2,
                ),
            ),
        ),
    )
    assert written.snap_id > 0
    assert not written.conflict

    snap = latest_alignment_snapshot(conn, PERIOD, key)
    assert snap is not None
    md = render_conclusion_markdown(build_conclusion(conn, PERIOD))
    assert f"快照 {snap.content_hash[:12]}" in md


def test_inconsistent_member_conclusions_are_not_collapsed_to_one(conn):
    """组内经办结论不一致时**如实说明**，不替它们取一条当组结论。

    这一格不是理论风险：20 条同因差异是 8 个人分几天办完的，两个人
    对同一张单的措辞不会一模一样。而签字的人看到「组级结论」一栏只有
    一句话时，会以为全组都签了这句话 —— 那等于让签字的人为没看过的
    7 条结论背书。
    """
    from reconq.align.aggregate import diff_key

    # 另一个经办人对同组里一条给了不同措辞的结论（台账允许：会签按条
    # 记录，谁也没规定全组必须用同一句话）。
    key = diff_key(PERIOD, "wechat", "taxonomy.ambiguity:EX-WX-LATE-0003")
    conn.execute(
        "UPDATE ledger_discrepancies SET conclusion=? WHERE disc_key=?",
        ("这一笔是退费已在 HIS 红冲，走的是另一条结算口径", key),
    )

    doc = build_conclusion(conn, PERIOD)
    group = next(g for g in doc.groups if g.channel == "wechat" and g.size == 8)
    assert "2 条经办结论不一致" in group.group_conclusion
    assert "需逐条复核" in group.group_conclusion
    # 逐条那一栏仍带各自原文，不会被组级结论覆盖掉
    texts = {line.conclusion for line in group.lines}
    assert len(texts) == 2
    md = render_conclusion_markdown(doc)
    assert "2 条经办结论不一致" in md


def test_uniform_member_conclusions_become_the_group_conclusion(conn):
    """组内结论一致时，组级结论就是那一句（不写「一致」这种废话）。"""
    doc = build_conclusion(conn, PERIOD)
    group = next(g for g in doc.groups if g.channel == "wechat" and g.size == 8)
    assert group.group_conclusion == "与channel_late_posting成因一致，已与收费处逐笔核对"
    handler, _ = group.signoff("handler")
    assert handler == HANDLER


def test_group_evidence_is_not_truncated(conn):
    """组内 8 条的证据行全部出现在结论单上（不截断）。"""
    md = render_conclusion_markdown(build_conclusion(conn, PERIOD))
    for index in range(1, 9):
        assert f"wechat_bill_2026-W36.csv:{index + 1}" in md


# ─────────────────── 4. 附录四类齐 ───────────────────


def test_appendices_all_four_present(conn):
    _append_trace_conclusion(conn)
    _append_dirty(conn)
    from reconq.audit.model_calls import init_audit_schema

    init_audit_schema(conn)
    conn.execute(
        "INSERT INTO model_calls (call_id, period, seq, conclusion_key, channel, model, "
        "prompt_version, status, error_code, prev_hash, hash, at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("EX-CALL-0001", PERIOD, 1, "wechat:2026-W36:EX-WX-LATE-0001", "wechat",
         "gpt-4o-mini", "v2", "degraded", "MODEL_UNAVAILABLE", "0" * 64, "f" * 64,
         "2026-09-06T10:00:00"),
    )

    doc = build_conclusion(conn, PERIOD)
    md = render_conclusion_markdown(doc)
    csv_text = render_conclusion_csv(doc)

    # 附录一 · 跨账期连坐链
    assert "### 附录一 · 跨账期连坐链" in md
    assert "跨账期重复计入" in md and "EX-WX-LATE-0001" in md
    # 附录二 · 未取到渠道及错误码
    assert "### 附录二 · 未取到渠道及错误码" in md
    assert "medical_ins" in md and "BILL_NOT_EXIST" in md
    assert "医保平台未出具 2026-W36 结算清单" in md
    # 附录三 · 脏行统计与隔离区文件指针
    assert "### 附录三 · 脏行统计与隔离区" in md
    assert "隔离脏行合计 1 条" in md
    assert f"out/{PERIOD}-脏行台账.csv" in md
    # 附录四 · 模型降级说明与规则基线占比
    assert "### 附录四 · 模型降级与规则基线" in md
    assert "模型调用 1 次，其中降级 1 次" in md
    assert "MODEL_UNAVAILABLE × 1" in md
    assert "规则基线占比：" in md

    # CSV 版四类附录同样在
    blocks = {r["区块"] for r in _csv_rows(csv_text)}
    assert {
        "附录-跨账期连坐链",
        "附录-未取到渠道",
        "附录-脏行统计",
        "附录-模型降级",
        "附录-规则基线",
    } <= blocks


def test_no_appendix_entries_says_so_explicitly(conn):
    """没有连坐 / 没有脏行时明说「本期无」，不留空白让人以为漏统计。"""
    md = render_conclusion_markdown(build_conclusion(conn, PERIOD))
    assert "本期无跨账期 / 跨渠道连坐结论。" in md
    assert "本期无脏行，账单表读到的行数与源文件行数一致。" in md
    assert "本期无归因草稿" not in md  # 本夹具是有草稿的


def test_rule_baseline_ratio_counts_only_named_top1(conn):
    """规则基线占比的分母**只算有 Top-1 成因的条数**。"""
    doc = build_conclusion(conn, PERIOD)
    assert "20/20" in doc.rule_baseline_ratio
    assert "未经模型" in doc.rule_baseline_ratio


# ─────────────────── 5. 重复导出一致 ───────────────────


def test_repeat_export_is_byte_identical(conn, tmp_path):
    """同账期同内容两次导出：内容一致、指纹一致。"""
    _append_scan_event(conn)
    first = export_conclusion(
        conn, PERIOD, fmt="md", out_path=str(tmp_path / "a.md"),
        exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
    )
    second = export_conclusion(
        conn, PERIOD, fmt="md", out_path=str(tmp_path / "b.md"),
        exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
    )
    assert _read(first.path) == _read(second.path)
    assert first.doc.fingerprint == second.doc.fingerprint

    csv_first = export_conclusion(
        conn, PERIOD, fmt="csv", out_path=str(tmp_path / "a.csv"),
        exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
    )
    csv_second = export_conclusion(
        conn, PERIOD, fmt="csv", out_path=str(tmp_path / "b.csv"),
        exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
    )
    assert _read(csv_first.path, "utf-8-sig") == _read(csv_second.path, "utf-8-sig")


def test_exported_at_and_by_do_not_change_fingerprint(conn):
    """导出人 / 导出时刻是本次导出的元数据，**不进指纹**。

    否则「同一份结论重导一次指纹就变了」，财务科无法用它判断
    「结论一个字没改」。
    """
    _append_scan_event(conn)
    a = build_conclusion(conn, PERIOD, exported_by=HANDLER, now="2026-09-07T10:00:00+08:00")
    b = build_conclusion(
        conn, PERIOD, exported_by="复核-李工", now="2026-09-30T23:59:59+08:00",
    )
    assert a.fingerprint == b.fingerprint
    assert a.exported_by != b.exported_by
    assert a.exported_at != b.exported_at
    # 导出的**文件**当然会变（那两行印着导出人）
    assert render_conclusion_markdown(a) != render_conclusion_markdown(b)


def test_state_change_changes_both_content_and_fingerprint(conn, tmp_path):
    """账期状态一变：重导出内容变、指纹变。"""
    _append_scan_event(conn)
    before = export_conclusion(
        conn, PERIOD, fmt="md", out_path=str(tmp_path / "before.md"),
        exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
    )
    # 处置状态从「待确认」推进到「已确认」
    conn.execute(
        "UPDATE period_diff_items SET disposition=? WHERE period=?",
        (DISPOSITION_CONFIRMED, PERIOD),
    )
    after = export_conclusion(
        conn, PERIOD, fmt="md", out_path=str(tmp_path / "after.md"),
        exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
    )
    assert _read(before.path) != _read(after.path)
    assert before.doc.fingerprint != after.doc.fingerprint
    assert all(g.disposition == DISPOSITION_CONFIRMED for g in after.doc.groups)
    assert "当前处置：已确认" in _read(after.path)


def test_fingerprint_is_pure_function_of_doc():
    """``conclusion_fingerprint`` 幂等：同一份 doc 算两次同一个值。"""
    doc = build_conclusion(_empty_conn(), PERIOD)
    assert conclusion_fingerprint(doc) == conclusion_fingerprint(doc)
    assert doc.fingerprint == conclusion_fingerprint(doc)
    assert len(doc.fingerprint) == 64


def _empty_conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    init_scan_schema(connection)
    return connection


def test_empty_period_export_says_so_instead_of_raising(tmp_path):
    """空账期导出不炸，且明说「本期无待处置的单边账」。"""
    conn = _empty_conn()
    try:
        outcome = export_conclusion(
            conn, PERIOD, fmt="md", out_path=str(tmp_path / "empty.md"),
            exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
        )
    finally:
        conn.close()
    text = _read(outcome.path)
    assert "本期无待处置的单边账，无处置单。" in text
    assert "本期尚无渠道结果，先跑 reconq scan" in text
    assert outcome.doc.groups_count == 0
    assert outcome.doc.channels_ok == 0


# ─────────────────── 6. 无患者标识 ───────────────────


def test_exported_sheet_has_no_patient_identifiers(conn, tmp_path):
    """落盘内容里没有身份证 / 手机号形态，也没有患者标识字样。"""
    for fmt in ("md", "csv"):
        outcome = export_conclusion(
            conn, PERIOD, fmt=fmt, out_path=str(tmp_path / f"x.{fmt}"),
            exported_by=HANDLER, now="2026-09-07T10:00:00+08:00",
        )
        text = _read(outcome.path, "utf-8-sig" if fmt == "csv" else "utf-8")
        assert not ID_CARD_SHAPE.search(text), f"{fmt} 版里出现了身份证形态"
        assert not PHONE_SHAPE.search(text), f"{fmt} 版里出现了手机号形态"
        for word in PATIENT_WORDS:
            assert word not in text, f"{fmt} 版里出现了患者标识字样「{word}」"


def test_patient_identifier_in_conclusion_blocks_export(conn, tmp_path):
    """台账里被塞进带患者信息的经办结论时，导出**报错而不是写出去**。

    这是本任务最容易糊弄过去的一条：只做「值整体形态」的核对看不见
    句子里嵌着的身份证号，而那一串会被打印出来交给财务科、可能再交给
    渠道方。这里直接改台账行（台账的职责是留痕，不是拦文案），再断言
    导出腿把它挡在落盘之前。
    """
    from reconq.align.aggregate import diff_key

    key = diff_key(PERIOD, "wechat", "taxonomy.ambiguity:EX-WX-LATE-0001")
    conn.execute(
        "UPDATE ledger_discrepancies SET conclusion=? WHERE disc_key=?",
        ("已联系住院号 110105199001011234 的家属确认，款项明日到账", key),
    )

    for fmt in ("md", "csv"):
        target = str(tmp_path / f"blocked.{fmt}")
        with pytest.raises(ReconqError) as excinfo:
            export_conclusion(conn, PERIOD, fmt=fmt, out_path=target)
        assert excinfo.value.code == "SHEET_NOT_DEIDENTIFIED"
        assert not os.path.exists(target), "脱敏未过时绝不能留下半个文件"


def test_clean_conclusion_with_order_no_is_not_false_positive(conn, tmp_path):
    """反向：含单号、金额、科室、导出人姓名但**不含**患者标识的文本不被误伤。

    「误伤」比「漏放」更难查：结论单上每条经办结论都被打回，人只会
    觉得这工具挑刺，于是绕过它直接手工导出 —— 脱敏就白做了。
    """
    text = render_conclusion_markdown(build_conclusion(conn, PERIOD))
    assert "EX-WX-LATE-0001" in text          # 渠道单号放行
    assert "wechat_bill_2026-W36.csv" in text  # 文件名放行
    assert HANDLER in text                     # 导出人姓名放行
    outcome = export_conclusion(
        conn, PERIOD, fmt="md", out_path=str(tmp_path / "ok.md"), exported_by=HANDLER
    )
    assert os.path.isfile(outcome.path)


# ─────────────────── CLI 两条腿 ───────────────────


def _run_cli(*args, cwd=None):
    env = dict(os.environ, PYTHONPATH=SRC)
    return subprocess.run(
        [sys.executable, "-m", "reconq", *args],
        capture_output=True, text=True, env=env, cwd=cwd,
    )


def test_cli_export_both_legs_and_gate_exit_code(conn, tmp_path):
    """``reconq export`` md / csv 两条腿端到端；门禁没过退出码为 1。"""
    db_path = str(tmp_path / "reconq.db")
    out_db = sqlite3.connect(db_path, isolation_level=None)
    out_db.row_factory = sqlite3.Row
    init_scan_schema(out_db)
    init_ledger_schema(out_db)
    _seed_channels(out_db)
    _seed_diffs(out_db)
    out_db.close()

    for fmt in ("md", "csv"):
        result = _run_cli(
            "--no-log", "export", "--period", PERIOD, "--fmt", fmt,
            "--db", db_path, "--out", str(tmp_path / f"cli.{fmt}"),
            "--operator", HANDLER,
        )
        assert result.returncode == 1, (fmt, result.stdout, result.stderr)
        assert f"结论单已导出：{tmp_path / f'cli.{fmt}'}" in result.stdout
        assert "3 张处置单" in result.stdout
        assert "20 条差异" in result.stdout
        assert "结论集指纹：" in result.stdout
        # 门禁没过时把原因说清，不让人以为导完就能归档
        assert "未取到的渠道" in result.stdout
        assert "不要当成「这路没有账单」" in result.stdout
        assert os.path.isfile(str(tmp_path / f"cli.{fmt}"))

    # 全部处置完成后退出码转 0
    done_db = sqlite3.connect(db_path, isolation_level=None)
    done_db.execute(
        "UPDATE period_diff_items SET disposition=? WHERE period=?", (DISPOSITION_CONFIRMED, PERIOD)
    )
    done_db.execute(
        "UPDATE period_channel_results SET status='checked' WHERE period=? AND channel=?",
        (PERIOD, "medical_ins"),
    )
    done_db.close()
    result = _run_cli(
        "--no-log", "export", "--period", PERIOD, "--fmt", "md", "--db", db_path,
        "--out", str(tmp_path / "cli-ok.md"), "--operator", HANDLER,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "已取到并完成口径" in _read(str(tmp_path / "cli-ok.md"))


def test_cli_export_json_summary(conn, tmp_path):
    """``--json`` 给机器可读的导出摘要。"""
    db_path = str(tmp_path / "reconq2.db")
    out_db = sqlite3.connect(db_path, isolation_level=None)
    out_db.row_factory = sqlite3.Row
    init_scan_schema(out_db)
    init_ledger_schema(out_db)
    _seed_channels(out_db)
    _seed_diffs(out_db)
    out_db.close()

    import json

    result = _run_cli(
        "--no-log", "export", "--period", PERIOD, "--fmt", "csv", "--db", db_path,
        "--out", str(tmp_path / "j.csv"), "--json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["period"] == PERIOD
    assert payload["fmt"] == "csv"
    assert payload["groups"] == 3
    assert payload["lines"] == 20
    assert payload["total_amount"] == "870.00"
    assert len(payload["fingerprint"]) == 64
