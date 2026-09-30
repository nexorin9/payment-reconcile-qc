"""契约指纹、口径变更影响面与回归基线对拍的测试（Task 17）。

五类规定断言：

1. 同内容不同注释/键序/数字写法 → 指纹一致（不误报口径变更）
2. 实质变更 → 指纹不同；且区分「改标签」与「改口径」
3. ``diff_fingerprints`` 列出变化的 check（新增/删除/改哪个参数）
4. 契约指纹变化后该账期标 ``needs_reconfirm``，且标记只能人工清
5. 回归基线对拍检出结论翻转的 check
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from reconq.contracts.fingerprint import (
    BaselineFlip,
    canonical_source,
    compare_baseline,
    diff_fingerprints,
    fingerprint_contract,
    fingerprint_source,
    impact_scope,
    render_markdown,
)
from reconq.contracts.loader import load_contract
from reconq.engine.checks import BillRow, CheckResult
from reconq.errors import ReconqError
from reconq.store.snapshots import (
    NEEDS_RECONFIRM,
    PeriodBinding,
    bind_period_contract,
    clear_needs_reconfirm,
    compare_with_baseline,
    diff_channel,
    init_snapshot_schema,
    latest_snapshot,
    list_needs_reconfirm,
    list_snapshots,
    load_baseline,
    record_baseline,
    record_snapshot,
)
from reconq.store.schema import connect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = PROJECT_ROOT / "contracts"
WECHAT = CONTRACT_DIR / "wechat.yml"
BILL_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "bills"


# —— 测试用最小契约 ————————————————————————————————————
#
# 用一份可读的小契约做指纹测试：真实微信契约有 15 条口径，改一处断言要
# 在几百行里找，改测试时容易改错地方。真实的六份契约另有专测（见下）。

MINIMAL = """
name: 微信账单对账契约
version: "1.0"
channel: wechat

variables:
  period: "2026-W36"

dataset:
  name: 微信支付账单
  tables:
    - name: wechat_tradebill
      location: inbox/${period}/wechat/
      columns:
        - name: out_trade_no
          data_type: string
          required: true
          description: 商户订单号
          checks:
            - name: 商户订单号必填
              path: [out_trade_no, not_empty]
              type: not_empty
        - name: trade_state
          data_type: string
          required: true
          checks:
            - name: 交易状态枚举
              path: [trade_state, in_enum]
              type: in_enum
              values: [SUCCESS, REFUND, CLOSED, NOTPAY]
      checks:
        - name: 笔数不为零
          path: [rows, row_count]
          type: row_count
          min: 1

checks:
  - name: 退款金额合计须为零
    path: [rows, refund_sum, zero]
    type: decimal_sum
    column: refund
    expected: "0.00"
    tolerance: "0.00"
"""


def _write(tmp_path: Path, text: str, name: str = "wechat.yml") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _conn(tmp_path: Path, name: str = "reconq.db") -> sqlite3.Connection:
    conn = connect(str(tmp_path / name))
    init_snapshot_schema(conn)
    return conn


# —— 1. 同内容不同写法 → 指纹一致 ———————————————————————


def test_comments_and_key_order_do_not_change_fingerprint():
    """只加注释、换键序、换缩进，指纹必须一致。

    财务科改口径时顺手加两行说明、调一下 YAML 排版是常事。若这类改动
    被判成「口径变了」，一账期就要把已确认的差异重新过一遍 —— 这是
    最容易让人从此不再信任这个功能的原因。
    """
    import yaml

    with_comments = MINIMAL.replace(
        "name: 微信账单对账契约",
        "# 维护人：财务科对账岗\nname: 微信账单对账契约  # 这行是行内注释",
    )
    # 键序打乱：safe_load 后按**相反**的键序 dump 回来（sort_keys=False
    # 时按插入序输出，这里显式倒序构造），指纹仍应一致。
    raw = yaml.safe_load(MINIMAL)
    reversed_order = {k: raw[k] for k in reversed(list(raw))}
    reordered = yaml.safe_dump(reversed_order, allow_unicode=True, sort_keys=False)

    base = fingerprint_source(MINIMAL)
    assert fingerprint_source(with_comments).digest == base.digest
    assert fingerprint_source(with_comments).check_digest == base.check_digest
    assert fingerprint_source(reordered).digest == base.digest
    assert fingerprint_source(reordered).check_digest == base.check_digest


def test_numeric_spelling_does_not_change_fingerprint():
    """``min: 1`` 与 ``min: 1.0`` 在 ``between`` 里等价，不该算口径变更。"""
    as_float = MINIMAL.replace("      min: 1", "      min: 1.0")
    assert fingerprint_source(as_float).check_digest == fingerprint_source(MINIMAL).check_digest


def test_period_placeholder_does_not_leak_into_fingerprint(tmp_path):
    """``${period}`` 占位原样保留：同一份契约在两个账期指纹相同。

    这是本模块最容易踩的坑：``load_contract`` 会把 ``${period}`` 换成
    账期值，若拿**已加载的模型**取指纹，同一份契约在 2026-W36 与
    2026-W40 会得到两个不同 digest，「口径改没改过」被账期噪声淹没。
    """
    path = _write(tmp_path, MINIMAL)
    base = fingerprint_source(str(path))
    assert "${period}" in base.canonical.tables[0].location

    loaded_w36 = load_contract(str(path), {"period": "2026-W36"})
    loaded_w40 = load_contract(str(path), {"period": "2026-W40"})
    fp36 = fingerprint_contract(loaded_w36)
    fp40 = fingerprint_contract(loaded_w40)

    # 模型路径确实会随账期变（location 里带了账期值）—— 这正是必须走
    # fingerprint_source 的理由。把这个事实钉死，而不是靠注释提醒。
    assert fp36.digest != fp40.digest
    # 原始文件路径与账期无关：同一份文件读两次、任何账期，指纹都一样。
    assert fingerprint_source(str(path)).digest == base.digest
    assert fingerprint_source(str(path)).check_digest == base.check_digest


# —— 2. 实质变更 → 指纹不同；标签 vs 口径 ——————————————————


def test_check_parameter_change_changes_check_digest():
    """加一个枚举值 = 口径变了。"""
    widened = MINIMAL.replace(
        "values: [SUCCESS, REFUND, CLOSED, NOTPAY]",
        "values: [SUCCESS, REFUND, CLOSED, NOTPAY, REVOKED]",
    )
    base = fingerprint_source(MINIMAL)
    new = fingerprint_source(widened)
    assert new.check_digest != base.check_digest
    assert new.digest != base.digest


def test_label_only_change_keeps_check_digest():
    """只改 name/description/version → 口径指纹不变，整份 digest 变。

    这条是「改说明不惊动财务科」的技术根据：两层指纹分开算，就是为了让
    这两种改动在系统里不是同一件事。
    """
    relabeled = MINIMAL.replace('name: 微信账单对账契约', 'name: 微信支付账单对账口径（财务科）')
    relabeled = relabeled.replace('version: "1.0"', 'version: "1.1"')
    relabeled = relabeled.replace(
        "description: 商户订单号", "description: 商户订单号（对应 HIS 收费记录主键）"
    )
    base = fingerprint_source(MINIMAL)
    new = fingerprint_source(relabeled)
    assert new.digest != base.digest, "整份内容确实变了"
    assert new.check_digest == base.check_digest, "但口径语义没变，不该要求重新确认"


def test_column_required_flip_changes_check_digest():
    """把某列从必填改成可选 —— 口径变了。"""
    loosened = MINIMAL.replace(
        "        - name: out_trade_no\n          data_type: string\n          required: true",
        "        - name: out_trade_no\n          data_type: string\n          required: false",
    )
    assert "required: false" in loosened
    assert fingerprint_source(loosened).check_digest != fingerprint_source(MINIMAL).check_digest


def test_every_shipped_contract_has_a_stable_fingerprint():
    """六份真实契约逐一取指纹：同内容两次调用必须一致且 check_digest 非空。

    真实契约里带中文、带 ``${period}``、带行内注释，是规范化逻辑最容易
    悄悄漏掉的情形（例如某个字段没进 canonical 形态，指纹会恒定不变）。
    """
    expected_counts = {
        "wechat.yml": 15,
        "alipay.yml": 11,
        "unionpay.yml": 8,
        "self_service.yml": 10,
        "his.yml": 12,
        "medical_ins.yml": 10,
    }
    for name, count in expected_counts.items():
        path = CONTRACT_DIR / name
        fp = fingerprint_source(str(path))
        assert fp.digest and fp.check_digest
        assert len(fp.digest) == 64 and len(fp.check_digest) == 64
        assert fingerprint_source(str(path)).digest == fp.digest
        assert len(fp.canonical.check_keys()) == count, f"{name} 口径条数对不上"


# —— 3. diff 列出变化的 check ————————————————————————————


def test_diff_lists_added_removed_and_changed_checks():
    """同 path 不同参数 → changed 并点名是哪个参数从什么变成什么。"""
    base = fingerprint_source(MINIMAL)
    row_count_block = (
        "        - name: 笔数不为零\n"
        "          path: [rows, row_count]\n"
        "          type: row_count\n"
        "          min: 1\n"
    )
    # 三处改动一次做完：改 refund_sum 的 expected、删掉笔数不为零、新增一条区间口径。
    # 顺序要紧：新增那条挂在**表级** checks 段（6 空格缩进）上，锚点里带着
    # 「笔数不为零」那几行，所以必须**先**插入再删除。
    table_checks_anchor = "      checks:\n        - name: 笔数不为零\n"
    assert table_checks_anchor in MINIMAL
    edited = (
        MINIMAL.replace('expected: "0.00"', 'expected: "100.00"')
        .replace(
            table_checks_anchor,
            "      checks:\n"
            "        - name: 退款额不得为负\n"
            "          path: [rows, refund, min]\n"
            "          type: between\n"
            "          column: refund\n"
            "          min: 0\n"
            "        - name: 笔数不为零\n",
        )
        .replace(row_count_block, "")
    )
    assert row_count_block not in edited, "「笔数不为零」这条应已被删掉"

    diff = diff_fingerprints(base, fingerprint_source(edited), channel="wechat")

    assert not diff.is_empty
    assert diff.semantic_changed
    changed = {(c.check_key, c.prop_changes) for c in diff.changed}
    assert ("decimal_sum:rows.refund_sum.zero", (("expected", "0.00", "100.00"),)) in changed
    assert [c.check_key for c in diff.removed] == ["row_count:rows.row_count"]
    assert [c.check_key for c in diff.added] == ["between:rows.refund.min"]
    assert "rows.refund_sum.zero" in diff.changed_check_paths()


def test_diff_renders_readable_markdown():
    """人可读片段要能直接贴进工单：含标题、指纹、新增/删除/参数变化。"""
    base = fingerprint_source(MINIMAL)
    edited = MINIMAL.replace('values: [SUCCESS, REFUND, CLOSED, NOTPAY]',
                             'values: [SUCCESS, REFUND, CLOSED, NOTPAY, REVOKED]')
    scope = impact_scope(diff_fingerprints(base, fingerprint_source(edited), channel="wechat"),
                         channel="wechat")
    md = render_markdown(diff_fingerprints(base, fingerprint_source(edited), channel="wechat"),
                         channel="wechat", scope=scope)
    assert md.startswith("## 契约口径变更 · wechat")
    assert base.digest[:12] in md
    assert "### 参数变化（1）" in md
    assert "values" in md and "REVOKED" in md
    assert "重新确认" in md


def test_identical_fingerprints_produce_empty_diff():
    base = fingerprint_source(MINIMAL)
    diff = diff_fingerprints(base, fingerprint_source(MINIMAL))
    assert diff.is_empty
    assert not diff.semantic_changed
    assert impact_scope(diff).requires_reconfirm is False
    assert impact_scope(diff).panel_hint() == ""


def test_label_only_diff_does_not_require_reconfirm():
    """只改说明 → 影响面说「不需要重新确认」，面板提示为空。"""
    base = fingerprint_source(MINIMAL)
    relabeled = MINIMAL.replace('version: "1.0"', 'version: "1.2"')
    scope = impact_scope(diff_fingerprints(base, fingerprint_source(relabeled), channel="wechat"),
                         channel="wechat")
    assert not scope.requires_reconfirm
    assert "确认的差异结论继续有效" in scope.reason
    assert scope.panel_hint() == ""


def test_column_added_or_removed_shows_up_in_diff():
    base = fingerprint_source(MINIMAL)
    with_extra = MINIMAL.replace(
        "      checks:\n        - name: 笔数不为零",
        "        - name: payer\n"
        "          data_type: string\n"
        "          required: false\n"
        "          description: 付款方\n"
        "      checks:\n        - name: 笔数不为零",
    )
    assert "name: payer" in with_extra
    diff = diff_fingerprints(base, fingerprint_source(with_extra), channel="wechat")
    assert [c.column for c in diff.column_changes if c.kind == "added"] == ["payer"]

    without = MINIMAL.replace(
        "        - name: trade_state\n"
        "          data_type: string\n"
        "          required: true\n"
        "          checks:\n"
        "            - name: 交易状态枚举\n"
        "              path: [trade_state, in_enum]\n"
        "              type: in_enum\n"
        "              values: [SUCCESS, REFUND, CLOSED, NOTPAY]\n",
        "",
    )
    diff2 = diff_fingerprints(base, fingerprint_source(without), channel="wechat")
    removed_cols = [c.column for c in diff2.column_changes if c.kind == "removed"]
    assert removed_cols == ["trade_state"]
    assert [c.check_key for c in diff2.removed] == ["in_enum:trade_state.in_enum"]


# —— 4. 口径变更 → 账期标 needs_reconfirm ——————————————————


def test_binding_first_time_is_plain_scanned(tmp_path):
    conn = _conn(tmp_path)
    try:
        fp = fingerprint_source(MINIMAL)
        binding = bind_period_contract(conn, "2026-W36", "wechat", fp)
        assert binding.status == "scanned"
        assert list_needs_reconfirm(conn) == []
    finally:
        conn.close()


def test_contract_change_marks_period_needs_reconfirm(tmp_path):
    """同一账期换一版口径跑 → 该账期这一路被标 needs_reconfirm。"""
    conn = _conn(tmp_path)
    try:
        bind_period_contract(conn, "2026-W36", "wechat", fingerprint_source(MINIMAL))
        edited = MINIMAL.replace('values: [SUCCESS, REFUND, CLOSED, NOTPAY]',
                                 'values: [SUCCESS, REFUND, CLOSED, NOTPAY, REVOKED]')
        binding = bind_period_contract(conn, "2026-W36", "wechat", fingerprint_source(edited))
        assert binding.status == NEEDS_RECONFIRM
        assert "口径指纹已从" in binding.note

        pending = list_needs_reconfirm(conn, "2026-W36")
        assert [(p.period, p.channel) for p in pending] == [("2026-W36", "wechat")]
        # 别的账期不受影响
        assert list_needs_reconfirm(conn, "2026-W37") == []
    finally:
        conn.close()


def test_label_only_change_does_not_mark_period(tmp_path):
    """只改契约说明不惊动已确认的账期 —— 这条不成立的话功能会被绕过。"""
    conn = _conn(tmp_path)
    try:
        bind_period_contract(conn, "2026-W36", "wechat", fingerprint_source(MINIMAL))
        relabeled = MINIMAL.replace('version: "1.0"', 'version: "1.3"')
        binding = bind_period_contract(conn, "2026-W36", "wechat", fingerprint_source(relabeled))
        assert binding.status == "scanned"
        assert list_needs_reconfirm(conn) == []
    finally:
        conn.close()


def test_reconfirm_marker_is_only_cleared_by_explicit_human_call(tmp_path):
    """标记只能由显式调用清掉；且清的是「人确认过」这件事，不是口径。"""
    conn = _conn(tmp_path)
    try:
        bind_period_contract(conn, "2026-W36", "wechat", fingerprint_source(MINIMAL))
        edited = MINIMAL.replace('expected: "0.00"', 'expected: "5.00"')
        edited_fp = fingerprint_source(edited)
        bind_period_contract(conn, "2026-W36", "wechat", edited_fp)
        assert list_needs_reconfirm(conn)

        clear_needs_reconfirm(conn, "2026-W36", "wechat", note="经办人复核，主管确认")
        assert list_needs_reconfirm(conn) == []

        # 口径没变的情况下再跑一次，状态**不回到**待确认 —— 确认过的
        # 账期不该因为日常重跑又被拉回去。
        again = bind_period_contract(conn, "2026-W36", "wechat", edited_fp)
        assert again.status == "reconfirmed"
        assert list_needs_reconfirm(conn) == []
    finally:
        conn.close()


def test_clearing_a_nonexistent_binding_is_an_explicit_error(tmp_path):
    conn = _conn(tmp_path)
    try:
        with pytest.raises(ReconqError) as exc:
            clear_needs_reconfirm(conn, "2026-W36", "wechat")
        assert exc.value.code == "PERIOD_BINDING_NOT_INITIALIZED"
    finally:
        conn.close()


# —— 5. 回归基线对拍 ————————————————————————————————————


def _result(path: str, status: str, *, name: str = "", amount: str | None = None) -> CheckResult:
    return CheckResult(
        check_path=path,
        check_name=name or path,
        operator="not_empty",
        status=status,
        diff_amount=amount,
    )


def test_baseline_detects_status_flips(tmp_path):
    """pass→fail 与 fail→pass 都算翻转；没变的不算。"""
    conn = _conn(tmp_path)
    try:
        record_baseline(
            conn, "2026-W36", "wechat",
            [
                _result("out_trade_no.not_empty", "fail", amount="1"),
                _result("rows.refund_sum.zero", "pass", amount="0.00"),
                _result("rows.row_count", "pass"),
            ],
        )
        comparison = compare_with_baseline(
            conn, "2026-W36", "wechat",
            [
                _result("out_trade_no.not_empty", "pass", amount="0"),
                _result("rows.refund_sum.zero", "fail", amount="86.00"),
                _result("rows.row_count", "pass"),
            ],
        )
        assert comparison.has_flip
        directions = {(f.check_path, f.direction) for f in comparison.flips}
        assert ("out_trade_no.not_empty", "fail→pass") in directions
        assert ("rows.refund_sum.zero", "pass→fail") in directions
        assert comparison.unchanged == 1
        assert comparison.flips[0].before_diff_amount in ("0.00", "1", "86.00", "0")
        rendered = comparison.render()
        assert "结论翻转" in rendered
    finally:
        conn.close()


def test_baseline_treats_new_and_deleted_checks_as_not_flips(tmp_path):
    """新增/删除的口径没有「之前」可比，不该报成结论翻转。"""
    conn = _conn(tmp_path)
    try:
        record_baseline(conn, "2026-W36", "wechat", [_result("a", "pass"), _result("b", "pass")])
        comparison = compare_with_baseline(
            conn, "2026-W36", "wechat", [_result("a", "pass"), _result("c", "fail")]
        )
        assert not comparison.has_flip
        assert comparison.added == ("c",)
        assert comparison.removed == ("b",)
        assert "不算翻转" in comparison.render()
    finally:
        conn.close()


def test_baseline_recording_is_idempotent_overwrite(tmp_path):
    """同账期重录基线是覆盖语义：条数不增，且读出来的是最新一版。"""
    conn = _conn(tmp_path)
    try:
        record_baseline(conn, "2026-W36", "wechat", [_result("a", "pass"), _result("b", "fail")])
        record_baseline(conn, "2026-W36", "wechat", [_result("a", "fail"), _result("b", "pass")])
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM check_baselines WHERE period='2026-W36'"
        ).fetchone()
        assert rows["n"] == 2
        baseline = load_baseline(conn, "2026-W36", "wechat")
        assert baseline["results"]["a"]["status"] == "fail"
        assert baseline["results"]["b"]["status"] == "pass"
    finally:
        conn.close()


def test_compare_baseline_without_baseline_lists_everything_as_added():
    comparison = compare_baseline(None, [_result("a", "pass"), _result("b", "fail")])
    assert not comparison.has_flip
    assert comparison.added == ("a", "b")


def test_baseline_records_real_check_results_from_a_real_period(tmp_path):
    """真跑一遍 2026-W36 微信样例账单：基线落下真实结论，对拍能读回来。"""
    from reconq.contracts.loader import load_contract
    from reconq.engine.checks import CheckContext, run_checks
    from reconq.sources.format_probe import read_bill_file_isolated

    contract = load_contract(str(WECHAT), {"period": "2026-W36"})
    rows: list[BillRow] = []
    bill_dir = BILL_ROOT / "2026-W36" / "wechat"
    for name in sorted(os.listdir(bill_dir)):
        # ``isolate_empty_required=False`` 与 verify 那条腿一致：空商户订单号
        # 恰恰是财务科要看的差异（not_empty 口径的职责），不能让解析层
        # 把它当垃圾行拿走 —— 拿走的话基线里就永远没有这条 fail，
        # 「口径改了结论会不会翻」这件事会缺一块。
        file_rows, _probe, _dirty = read_bill_file_isolated(
            str(bill_dir / name), contract.table(), "wechat",
            period="2026-W36", isolate_empty_required=False,
        )
        rows.extend(file_rows)
    assert len(rows) == 20, f"样例账单应读出 20 行，实得 {len(rows)}"

    # 跨日切分那条口径要账期时区；不给就显式报错而不是拿「今天」蒙混
    # （与 CLI 的取值路径一致：先看注入变量，再看契约声明的默认值）。
    results = run_checks(
        rows,
        contract,
        context=CheckContext(
            period="2026-W36",
            session_timezone=contract.variables.get("session_timezone"),
        ),
    )
    conn = _conn(tmp_path)
    try:
        written = record_baseline(
            conn, "2026-W36", "wechat", results,
            contract_digest=fingerprint_source(str(WECHAT)).digest,
        )
        assert written == len(results)
        baseline = load_baseline(conn, "2026-W36", "wechat")
        assert len(baseline["results"]) == len(results)
        # 同一批数据再跑一次，结论不应翻转
        again = compare_with_baseline(conn, "2026-W36", "wechat", results)
        assert not again.has_flip
        # 真实样例里本来就有 3 条未通过，基线如实记下来
        assert baseline["results"]["out_trade_no.not_empty"]["status"] == "fail"
    finally:
        conn.close()


# —— 快照存储与 CLI ————————————————————————————————————


def test_record_snapshot_is_idempotent(tmp_path):
    """同一份契约反复记录不产生第二行，也不改写首次记录时刻。"""
    conn = _conn(tmp_path)
    try:
        fp = fingerprint_source(MINIMAL)
        first = record_snapshot(conn, "wechat", fp, source_path="contracts/wechat.yml", version="1.0")
        second = record_snapshot(conn, "wechat", fp, source_path="contracts/wechat.yml", version="1.0")
        assert first.digest == second.digest
        assert first.recorded_at == second.recorded_at
        assert len(list_snapshots(conn, "wechat")) == 1
        edited = fingerprint_source(MINIMAL.replace('expected: "0.00"', 'expected: "9.00"'))
        record_snapshot(conn, "wechat", edited)
        assert len(list_snapshots(conn, "wechat")) == 2
        assert latest_snapshot(conn, "wechat").digest == edited.digest
    finally:
        conn.close()


def test_diff_channel_first_run_has_no_previous(tmp_path):
    conn = _conn(tmp_path)
    try:
        path = _write(tmp_path / "c", MINIMAL)
        previous, diff, scope = diff_channel(conn, "wechat", str(path))
        assert previous is None
        assert diff.is_empty
        assert not scope.requires_reconfirm
        assert "首次记录" in scope.reason
    finally:
        conn.close()


def test_diff_channel_missing_contract_is_explicit(tmp_path):
    conn = _conn(tmp_path)
    try:
        with pytest.raises(ReconqError) as exc:
            diff_channel(conn, "wechat", str(tmp_path / "nope.yml"))
        assert exc.value.code == "CONTRACT_MISSING_KEY"
    finally:
        conn.close()


def test_snapshot_roundtrip_preserves_fingerprint(tmp_path):
    """快照存盘再取出，diff 仍能算出与原文件一致的差异。"""
    conn = _conn(tmp_path)
    try:
        fp = fingerprint_source(MINIMAL)
        record_snapshot(conn, "wechat", fp)
        restored = latest_snapshot(conn, "wechat").to_fingerprint()
        assert restored.digest == fp.digest
        assert restored.check_digest == fp.check_digest
        edited = MINIMAL.replace('expected: "0.00"', 'expected: "1.00"')
        diff = diff_fingerprints(restored, fingerprint_source(edited), channel="wechat")
        assert [c.check_key for c in diff.changed] == ["decimal_sum:rows.refund_sum.zero"]
    finally:
        conn.close()


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "reconq", *args],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )


@pytest.fixture()
def workspace(tmp_path):
    """一份可跑 CLI 的最小工作区：契约目录 + 样例账单 + 空库。"""
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    shutil.copy(str(WECHAT), contracts / "wechat.yml")
    shutil.copy(str(PROJECT_ROOT / "contracts" / "alipay.yml"), contracts / "alipay.yml")
    inbox = tmp_path / "inbox" / "2026-W36" / "wechat"
    inbox.mkdir(parents=True)
    for name in sorted(os.listdir(BILL_ROOT / "2026-W36" / "wechat")):
        shutil.copy(str(BILL_ROOT / "2026-W36" / "wechat" / name), inbox / name)
    return tmp_path


def test_cli_contract_snapshot_diff_and_status(workspace):
    """CLI 三条腿端到端：落快照 → 改契约 → diff 报出参数变化 → status 列标记。"""
    db = str(workspace / "reconq.db")
    res = _run(["contract", "snapshot", "--channel", "wechat", "--db", db], workspace)
    assert res.returncode == 0, res.stderr
    assert "已记录契约快照" in res.stdout

    # 改一处口径参数（枚举加一个值）
    path = workspace / "contracts" / "wechat.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "values: [SUCCESS, REFUND, CLOSED, NOTPAY]",
            "values: [SUCCESS, REFUND, CLOSED, NOTPAY, REVOKED]",
        ),
        encoding="utf-8",
    )
    res = _run(["contract", "diff", "--channel", "wechat", "--db", db, "--json"], workspace)
    assert res.returncode == 0, res.stderr
    import json

    payload = json.loads(res.stdout)
    assert payload["requires_reconfirm"] == ["wechat"]
    assert payload["panel_hints"], "面板提示文案不能为空"
    hint = payload["panel_hints"][0]
    assert "本期口径已变更" in hint and "wechat" in hint
    changed = payload["channels"][0]["diff"]["changed"]
    assert any("REVOKED" in str(c) for c in changed)

    # 只改版本号 → 不要求重新确认。
    # 先把上一处改动（枚举加 REVOKED）落成新快照，否则这条 diff 仍会
    # 把它算进来，测的就不是「只改版本号」这件事了。
    res = _run(["contract", "snapshot", "--channel", "wechat", "--db", db], workspace)
    assert res.returncode == 0, res.stderr
    path.write_text(
        path.read_text(encoding="utf-8").replace('version: "1.0"', 'version: "2.0"'),
        encoding="utf-8",
    )
    res = _run(["contract", "diff", "--channel", "wechat", "--db", db, "--json"], workspace)
    payload = json.loads(res.stdout)
    assert payload["requires_reconfirm"] == []
    assert payload["panel_hints"] == []

    res = _run(["contract", "status", "--db", db], workspace)
    assert res.returncode == 0
    assert "没有因口径变更而需要重新确认的账期" in res.stdout


def test_cli_diff_record_makes_the_current_contract_the_new_baseline(workspace):
    """``--record`` 后「上一份快照」变成当前口径：同一处改动不再反复报。"""
    db = str(workspace / "reconq.db")
    path = workspace / "contracts" / "wechat.yml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("values: [SUCCESS, REFUND, CLOSED, NOTPAY]",
                     "values: [SUCCESS, REFUND, CLOSED, NOTPAY, REVOKED]"),
        encoding="utf-8",
    )

    # 首次跑：还没有快照可比，如实说是首次记录
    import json

    res = _run(["contract", "diff", "--channel", "wechat", "--db", db, "--record", "--json"],
               workspace)
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["channels"][0]["has_snapshot"] is False
    assert "首次记录" in payload["channels"][0]["impact"]["reason"]

    # 快照已落；再改一处口径，diff 要报出来
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "          path: [rows, row_count]\n          type: row_count\n          min: 1\n",
            "          path: [rows, row_count]\n          type: row_count\n          min: 100\n",
        ),
        encoding="utf-8",
    )
    res = _run(["contract", "diff", "--channel", "wechat", "--db", db, "--json"], workspace)
    payload = json.loads(res.stdout)
    assert payload["requires_reconfirm"] == ["wechat"]
    changed = payload["channels"][0]["diff"]["changed"]
    assert any(c["check_key"] == "row_count:rows.row_count" for c in changed)
    # 上一条改动（枚举）不该还在差异里 —— 快照就是从这儿之后记的
    assert not any(c["check_key"] == "in_enum:trade_state.in_enum" for c in changed)

    # --record 之后同一处改动不再反复报，且快照表不因重复运行膨胀
    res = _run(["contract", "diff", "--channel", "wechat", "--db", db, "--record", "--json"],
               workspace)
    assert res.returncode == 0, res.stderr
    res = _run(["contract", "diff", "--channel", "wechat", "--db", db, "--record", "--json"],
               workspace)
    payload = json.loads(res.stdout)
    assert payload["requires_reconfirm"] == []
    res = _run(["contract", "snapshot", "--channel", "wechat", "--db", db, "--list", "--json"],
               workspace)
    assert len(json.loads(res.stdout)["snapshots"]) == 2


def test_cli_verify_bind_contract_marks_period_after_change(workspace):
    """verify --bind-contract 落口径出处；之后改契约，该账期标待确认。"""
    db = str(workspace / "reconq.db")
    res = _run(
        ["verify", "--period", "2026-W36", "--source", "inbox", "--channel", "wechat",
         "--contracts", "contracts", "--bind-contract", "--db", db, "--json"],
        workspace,
    )
    assert res.returncode == 1, res.stdout + res.stderr
    import json

    payload = json.loads(res.stdout)
    binding = payload["contract_bindings"][0]
    assert binding["status"] == "scanned"
    assert binding["period"] == "2026-W36" and binding["channel"] == "wechat"

    path = workspace / "contracts" / "wechat.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('expected: "0.00"', 'expected: "1.00"'),
        encoding="utf-8",
    )
    res = _run(
        ["verify", "--period", "2026-W36", "--source", "inbox", "--channel", "wechat",
         "--contracts", "contracts", "--bind-contract", "--db", db, "--json"],
        workspace,
    )
    payload = json.loads(res.stdout)
    assert payload["contract_bindings"][0]["status"] == NEEDS_RECONFIRM

    res = _run(["contract", "status", "--period", "2026-W36", "--db", db, "--json"], workspace)
    payload = json.loads(res.stdout)
    assert [(b["period"], b["channel"]) for b in payload["needs_reconfirm"]] == [
        ("2026-W36", "wechat")
    ]


def test_cli_verify_without_bind_stays_read_only(workspace):
    """不加 --bind-contract 时 verify 不写任何状态：只读排障腿保持只读。"""
    db = str(workspace / "reconq.db")
    res = _run(
        ["verify", "--period", "2026-W36", "--source", "inbox", "--channel", "wechat",
         "--contracts", "contracts", "--json"],
        workspace,
    )
    assert res.returncode == 1
    import json

    assert json.loads(res.stdout)["contract_bindings"] == []
    assert not os.path.exists(db), "只读腿不应创建账单库"

    # 显式要绑定却不给 --db → 显式错误，不猜默认值
    res = _run(
        ["verify", "--period", "2026-W36", "--source", "inbox", "--channel", "wechat",
         "--contracts", "contracts", "--bind-contract"],
        workspace,
    )
    assert res.returncode == 2
    assert "PERIOD_BINDING_NOT_INITIALIZED" in res.stderr


def test_cli_contract_baseline_records_and_compares(workspace):
    """基线：先 --record 落一条，再改契约重跑，对拍列出翻转的口径。"""
    db = str(workspace / "reconq.db")
    res = _run(
        ["contract", "baseline", "--period", "2026-W36", "--channel", "wechat",
         "--source", "inbox", "--contracts", "contracts", "--db", db, "--record"],
        workspace,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "已记录回归基线" in res.stdout

    # 未改契约时对拍：不应有翻转
    res = _run(
        ["contract", "baseline", "--period", "2026-W36", "--channel", "wechat",
         "--source", "inbox", "--contracts", "contracts", "--db", db, "--json"],
        workspace,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    import json

    payload = json.loads(res.stdout)
    assert payload["has_baseline"] is True
    assert payload["comparison"]["has_flip"] is False
    assert payload["comparison"]["baseline_period"] == "2026-W36"

    # 把「账单笔数不为零」的门槛从 1 抬到 100 → 本期 20 笔，由 pass 翻成 fail
    path = workspace / "contracts" / "wechat.yml"
    text = path.read_text(encoding="utf-8")
    assert "          path: [rows, row_count]\n          type: row_count\n          min: 1\n" in text
    path.write_text(
        text.replace(
            "          path: [rows, row_count]\n          type: row_count\n          min: 1\n",
            "          path: [rows, row_count]\n          type: row_count\n          min: 100\n",
        ),
        encoding="utf-8",
    )
    res = _run(
        ["contract", "baseline", "--period", "2026-W36", "--channel", "wechat",
         "--source", "inbox", "--contracts", "contracts", "--db", db],
        workspace,
    )
    assert res.returncode == 1, "有口径翻转时退出码应为 1（门禁语义）"
    assert "结论翻转" in res.stdout
    assert "rows.row_count" in res.stdout and "pass → fail" in res.stdout


def test_cli_contract_baseline_without_data_is_explicit(workspace):
    """没账单就不建基线 —— 对着空气记一条基线，比没有基线更坏。"""
    db = str(workspace / "reconq.db")
    res = _run(
        ["contract", "baseline", "--period", "2026-W39", "--channel", "wechat",
         "--source", "inbox", "--contracts", "contracts", "--db", db, "--record"],
        workspace,
    )
    assert res.returncode == 2
    assert "BILL_FILE_NOT_FOUND" in res.stderr
