"""规模压测与基线对拍（Task 37）。

三组验收，对应任务书的三个硬要求：

1. **真实规模跑通** —— 六路各 2000+ 行的样例经主路径
   (:func:`reconq.engine.orchestrator.scan`) 跑完，产出可核对的库表内容。
2. **峰值内存有界** —— 断言的是 :mod:`reconq.sources.his_db` **流式取数腿**
   的界（批预算 8MB × 余量 + 最宽行），**不是**整次扫描的峰值。后者要把
   整期读进内存才能跑口径，量级是几十 MB，属于设计如此；把它按流式预算去
   断言，等于要求产品做它设计上不做的事，测出来的「有界」也是假的。
3. **与基础实现对拍** —— 同一份样例上跑「按渠道单表逐笔 VLOOKUP 对」的基线，
   产出**行号与差额**可复现的差异表，并要求样本里**至少有一条**基线静默
   择一而本产品报歧义的行（否则这份样例证明不了「拒绝归并」）。

跑全量样例（2000+/路）比较慢，本文件用 **session 级夹具**只生成 + 扫一次，
各用例共用同一份产物；小规模用例另起一份 300 行/路的轻量样例跑边界。

**替身与真实实现的边界**：HIS 侧用本地 SQLite 替身数据层（``bench.py``
模块头写明了为什么 ``charge_time`` 必须存带偏移的 ISO 串），走的是真实
取数主路径；微信/支付宝的商户 API 本轮未实连，由 ``tests/protocol/`` 的
报文回放覆盖。
"""

from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
from decimal import Decimal

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import baseline_compare  # noqa: E402
import bench  # noqa: E402
import gen_fixtures  # noqa: E402

from reconq.sources.his_db import (  # noqa: E402
    STREAM_FETCH_BUDGET_BYTES,
    STREAM_FETCH_GROWTH_FACTOR,
    STREAM_FETCH_MAX_BATCH_ROWS,
)

PERIOD = gen_fixtures.PERIOD
#: 压测专用账期。**不复用** 2026-W36：那份是 Task 3 起各任务共用的样例账期，
#: 往里塞一万行会把那些用例的「20 行」断言与本任务的规模断言互相污染。
SCALE_PERIOD = PERIOD


@pytest.fixture(scope="session")
def scale_fixtures(tmp_path_factory) -> str:
    """生成一次六路 2000+ 行样例，全文件共用（生成约 3 秒）。"""
    out = str(tmp_path_factory.mktemp("scale-fixtures"))
    manifest = gen_fixtures.generate(out, period=SCALE_PERIOD)
    counts = manifest["counts"]
    for channel in ("wechat", "alipay", "unionpay", "self_service", "medical_ins"):
        assert counts[channel] >= 2000, f"{channel} 只有 {counts[channel]} 行，未达 2000+ 要求"
    return out


@pytest.fixture(scope="session")
def bench_result(scale_fixtures, tmp_path_factory) -> bench.BenchResult:
    """跑一次完整压测（生成 HIS 替身库 → 流式取数 → 六路扫描 → 对齐）。"""
    db = str(tmp_path_factory.mktemp("bench") / "scan.sqlite")
    return bench.run_bench(
        fixture_root=scale_fixtures,
        period=SCALE_PERIOD,
        db_path=db,
        out_path=os.path.join(os.path.dirname(db), "bench.json"),
    )


@pytest.fixture(scope="session")
def scale_db(bench_result, tmp_path_factory) -> str:
    return bench_result.db_path


@pytest.fixture(scope="session")
def report(scale_db) -> baseline_compare.CompareReport:
    conn = sqlite3.connect(scale_db)
    conn.row_factory = sqlite3.Row
    try:
        return baseline_compare.compare(conn, period=SCALE_PERIOD)
    finally:
        conn.close()


# ─────────────────── 一、真实规模样例本身 ───────────────────


def test_six_channels_each_exceed_two_thousand_rows(scale_fixtures):
    """六路**每一路**都要 2000+ 行（不是总数够）。"""
    with open(os.path.join(scale_fixtures, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    for channel in ("wechat", "alipay", "unionpay", "self_service", "medical_ins"):
        assert manifest["counts"][channel] >= 2000, (
            f"{channel} {manifest['counts'][channel]} 行，未达 2000+"
        )
    assert manifest["counts"]["his_in_window"] >= 2000


def test_fixtures_are_reproducible_for_the_same_seed(tmp_path):
    """同一种子两次生成，文件逐字节相同 —— 对拍报告的差异行号才可复算。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    gen_fixtures.generate(str(a), period=SCALE_PERIOD, rows_per_channel=60, his_rows=80)
    gen_fixtures.generate(str(b), period=SCALE_PERIOD, rows_per_channel=60, his_rows=80)
    for channel in ("wechat", "alipay", "unionpay", "self_service", "medical_ins"):
        left = sorted((a / SCALE_PERIOD / channel).iterdir())
        right = sorted((b / SCALE_PERIOD / channel).iterdir())
        assert [p.name for p in left] == [p.name for p in right]
        for lp, rp in zip(left, right):
            assert lp.read_bytes() == rp.read_bytes(), f"{channel}/{lp.name} 两次生成不一致"


def test_fixtures_carry_every_intended_layer(scale_fixtures):
    """每一层都要真的落在样例里，否则那份「分层样例」名不副实。"""
    with open(os.path.join(scale_fixtures, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    layers = manifest["layers"]
    assert layers["wechat"].get("refund", 0) > 0, "微信层缺行内退费"
    assert layers["unionpay"].get("refund", 0) > 0, "银联层缺独立红冲行"
    assert layers["his"].get("split", 0) > 0, "HIS 侧缺一码付拆笔形态"
    assert layers["his"].get("equal_amount", 0) > 0, "HIS 侧缺金额相同多候选形态"
    assert layers["his"].get("cross_period", 0) > 0, "HIS 侧缺跨账期落点形态"
    assert layers["wechat"].get("duplicate_claim", 0) > 0, "缺跨渠道重复认领形态"
    assert sum(len(v) for v in manifest["channel_only"].values()) >= 300
    assert len(manifest["his_only"]) >= 120


def test_fixtures_use_placeholder_identifiers_only(scale_fixtures):
    """单号与金额一律占位：不含患者可识别明细（姓名/住院号/身份证形态）。"""
    sample = os.path.join(scale_fixtures, SCALE_PERIOD, "wechat")
    name = sorted(os.listdir(sample))[0]
    text = open(os.path.join(sample, name), encoding="utf-8").read()
    for banned in ("身份证", "住院号", "patient_name", "id_card"):
        assert banned not in text
    # 微信账单的用户标识列必须是脱敏占位
    assert "脱敏占位用户" in text
    his = os.path.join(scale_fixtures, "his", f"charge_record_{SCALE_PERIOD}.csv")
    head = open(his, encoding="utf-8").readline()
    assert head.strip() == (
        "his_order_no,charge_id,amount,charge_time,period_code,department,settle_type,is_refund"
    )


# ─────────────────── 二、峰值内存与耗时 ───────────────────


def test_his_stream_peak_memory_stays_within_batch_budget(bench_result):
    """流式取数腿的峰值内存有界：批预算 × 余量 + 最宽行。

    这是**流式腿**的界，不是整次扫描的界 —— 两者在 :meth:`BenchResult.render`
    里分开报，混着报会把一个几十 MB 的数字说成「0.8MB 有界」。
    """
    ceiling = (
        STREAM_FETCH_BUDGET_BYTES * bench.PEAK_HEADROOM
        + bench_result.widest_row_bytes
    ) / 1048576
    assert bench_result.stream_peak_mb <= ceiling, (
        f"流式取数峰值 {bench_result.stream_peak_mb:.2f} MB 超出界 {ceiling:.2f} MB"
    )


def test_stream_fetch_batches_are_far_fewer_than_rows(bench_result):
    """分批真的生效：批次数远小于行数。

    按行累加批次数会让这个指标退化成「行数」，那样「峰值内存有界」就无从谈起
    —— 因为那说明取数是整结果集一次性物化的。
    """
    assert bench_result.stream_rows >= 2000
    assert bench_result.batches < bench_result.stream_rows / 4, (
        f"{bench_result.stream_rows} 行用了 {bench_result.batches} 批，分批没生效"
    )
    assert bench_result.windows >= 2, "账期应按天切成多窗，才能定位慢在哪天"
    assert bench_result.widest_row_bytes > 0


def test_stream_constants_match_the_referenced_source(bench_result):
    """三个常量与参考实现的取数批策略同值（防止后续任务改口径时静默漂移）。"""
    assert bench_result.budget_bytes == STREAM_FETCH_BUDGET_BYTES == 8 * 1024 * 1024
    assert bench_result.max_batch_rows == STREAM_FETCH_MAX_BATCH_ROWS == 1000
    assert bench_result.growth_factor == STREAM_FETCH_GROWTH_FACTOR == 4


def test_full_scan_runs_all_six_channels_in_bounded_time(bench_result):
    """六路都要真的取到（不是「未取到」凑数），且耗时可测。"""
    for channel in ("wechat", "alipay", "unionpay", "self_service", "medical_ins", "his"):
        assert bench_result.rows.get(channel, 0) >= 2000, (
            f"{channel} 只落库 {bench_result.rows.get(channel, 0)} 行"
        )
    assert 0 < bench_result.scan_seconds < 60, f"扫描耗时 {bench_result.scan_seconds:.1f}s 异常"


def test_scan_peak_memory_is_reported_separately_from_stream(bench_result):
    """扫描腿峰值必须**单独**报出来，且确实大于流式腿（否则就是把两者混报）。"""
    assert bench_result.scan_peak_mb > 0
    assert bench_result.stream_peak_mb > 0
    payload = bench_result.to_dict()
    assert {"scan_peak_mb", "stream_peak_mb", "align_peak_mb"} <= set(payload)
    text = bench_result.render()
    assert "不在流式预算口径内" in text, "报告必须写明扫描腿峰值不受流式预算约束"


# ─────────────────── 三、幂等与口径 ───────────────────


def test_rescanning_the_same_period_changes_nothing(bench_result, scale_db):
    """同账期重扫：行数与 Decimal 合计一字不变（幂等）。"""
    assert bench_result.replay_rows_inserted == 0, "重扫不应新增任何行"
    assert bench_result.replay_rows_skipped > 0, "重扫应命中幂等唯一键"
    assert bench_result.replay_amount_stable is True
    conn = sqlite3.connect(scale_db)
    try:
        total = sum(
            Decimal(str(r[0]))
            for r in conn.execute(
                "SELECT amount_text FROM bill_rows WHERE period = ?", (SCALE_PERIOD,)
            )
        )
    finally:
        conn.close()
    assert total > 0


def test_only_the_declared_layers_fail(bench_result, scale_fixtures):
    """红项必须与 ``manifest.json`` 里声明的「故意留下的 fail」逐条对得上。

    压测样例里出现一条**没声明**的 fail，说明生成器与契约各写各的（序号位数、
    枚举取值、勾稽等式这类），必须当场发现而不是让它混在「6 条未通过」里。

    声明里的 ``check`` 写的是 ``his_order_no.unique`` 这种「列名 + 算子」，
    而落库的 ``check_path`` 是 ``[his_order_no, unique]`` 拼出来的点路径
    （``rows.charge_time.in_period`` 这类还带一个 ``rows`` 前缀段），两者不是
    逐字相等。这里按**同一渠道内、路径以声明的末段结尾**来配对，够严（渠道
    与算子都对得上）也不会因为前缀段的写法差异而漏判。
    """
    with open(os.path.join(scale_fixtures, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    declared = manifest["expected_fail_layers"]
    assert declared, "manifest 应当声明故意留下的 fail"

    conn = sqlite3.connect(bench_result.db_path)
    conn.row_factory = sqlite3.Row
    try:
        observed = {
            (str(r["channel"]), str(r["check_path"]))
            for r in conn.execute(
                "SELECT DISTINCT channel, check_path FROM period_check_results "
                "WHERE period = ? AND status = 'fail'",
                (SCALE_PERIOD,),
            )
        }
    finally:
        conn.close()

    def _is_declared(channel: str, path: str) -> bool:
        tail = path.rsplit(".", 1)[-1]
        for item in declared:
            if item["channel"] != channel:
                continue
            name = item["check"]
            if path == name or path.endswith(f".{name}") or tail == name:
                return True
            # 声明写成「列名.算子」，路径末两段拼起来就是它
            if path.endswith(name) or path.rsplit(".", 2)[-2:] == name.split("."):
                return True
        return False

    undeclared = sorted(
        f"{ch}:{path}" for ch, path in observed if not _is_declared(ch, path)
    )
    assert not undeclared, f"出现未声明的 fail：{undeclared}"
    assert bench_result.check_failed == len(declared), (
        f"未通过 {bench_result.check_failed} 条，声明了 {len(declared)} 条"
    )


# ─────────────────── 四、与基线对拍 ───────────────────


def test_baseline_silently_picks_where_product_refuses_to_merge(report):
    """**核心断言**：基线静默择一、本产品报「歧义待人工」的样例至少 1 条。

    没有这一条，「拒绝归并」就只是一句设计宣言 —— 谁都可以说自己不会择一，
    但只有把一份真会择一的基线放在旁边逐行对，这个说法才是可核对的。
    """
    assert report.silently_picked, (
        "样例中没有任何「基线静默择一而本产品报歧义」的行，"
        "这份样例证明不了拒绝归并这条纪律"
    )
    for row in report.silently_picked:
        assert row["candidate_total"] >= 2, "歧义必须列出全部候选"
        assert row["baseline_picked"]["ref"], "基线选中的那条必须可回查"
        assert row["baseline_dropped"], "基线丢弃的候选必须列出来，否则差额无从核对"
        assert Decimal(row["hidden_amount"]) != 0, "被吃掉的差额不应恒为零"
        assert ":" in row["ref"], "差异行必须带源文件行号，供财务科回查"


def test_swallowed_his_rows_are_invisible_in_the_baseline_output(report):
    """被基线整段吞掉的 HIS 记录：同号多条的其余成员，一条都不许出现在输出里。"""
    assert report.swallowed_his_rows, "样例应包含同单号多条的 HIS 记录"
    for row in report.swallowed_his_rows:
        assert row["same_order_siblings"] >= 2
        assert row["listed_by_product"] is True, (
            "本产品必须把这条记录列出来（认领或列为候选），否则它同样消失了"
        )


def test_every_channel_row_gets_a_verdict_from_the_product(report, scale_db):
    """每一行渠道账单都要有结论 —— 规模变大不许丢行。"""
    conn = sqlite3.connect(scale_db)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM bill_rows WHERE period = ? AND channel <> 'his'",
            (SCALE_PERIOD,),
        ).fetchone()[0]
    finally:
        conn.close()
    counted = sum(
        v
        for k, v in report.product_counts.items()
        if k not in ("his_only",)
    )
    assert counted == total, f"对齐结论 {counted} 条 < 渠道行 {total} 条，有行没结论"
    assert report.product_counts.get("matched", 0) > total * 0.8, (
        "真实账期里绝大多数行应当对得上；若 matched 占比过低，说明样例配对有问题"
    )


def test_product_reports_ambiguity_and_cross_period_that_baseline_cannot(report):
    """本产品多出来的三类结论各自非空，且与基线的能力边界相符。"""
    assert report.product_counts.get("ambiguous", 0) > 0
    assert report.product_only_cross_period, "跨账期落点应有实测样本"
    assert report.product_only_weak, "弱匹配待人工应有实测样本"
    # 基线按定义没有这些结论
    assert report.baseline_counts.get("ambiguous", 0) == 0
    assert "跨账期" not in report.baseline_counts
    assert "weak_pending" not in report.baseline_counts


def test_report_is_reproducible(scale_db, tmp_path):
    """同一份库跑两次，差异表逐字节相同（对拍结论可复算）。"""
    outs = []
    for name in ("r1.md", "r2.md"):
        path = tmp_path / name
        conn = sqlite3.connect(scale_db)
        conn.row_factory = sqlite3.Row
        try:
            rep = baseline_compare.compare(conn, period=SCALE_PERIOD)
        finally:
            conn.close()
        text = baseline_compare.render_markdown(rep, sample=5)
        # 耗时随机器波动，从可复现断言里剔掉
        text = "\n".join(
            line for line in text.splitlines() if "耗时" not in line
        )
        path.write_text(text, encoding="utf-8")
        outs.append(path.read_text(encoding="utf-8"))
    assert outs[0] == outs[1], "两次对拍的差异表不一致，行号不可复算"


def test_report_states_which_parts_are_real_and_which_are_doubles(scale_db):
    """报告必须写明真实协议实现 / 测试替身 / 未实连三者的边界。"""
    conn = sqlite3.connect(scale_db)
    conn.row_factory = sqlite3.Row
    try:
        rep = baseline_compare.compare(conn, period=SCALE_PERIOD)
    finally:
        conn.close()
    text = baseline_compare.render_markdown(rep, sample=3)
    assert "哪些是真实协议实现、哪些是测试替身" in text
    assert "本轮未实连" in text
    assert "替身" in text


def test_baseline_never_silently_claims_a_second_time(scale_db):
    """基线的定义性质：同单号命中多条时只取第一条，其余进 ``dropped``。

    这条是对**基线本身**的断言 —— 如果哪天有人「顺手」给基线也加了拒绝归并，
    对拍就失去意义了（变成两个同款实现互相证明）。此时 ``--require-silent``
    应当因找不到差异而失败。
    """
    from reconq.align.matcher import AlignRecord
    from datetime import datetime

    def _rec(no: str, amount: str, row: int) -> AlignRecord:
        return AlignRecord(
            channel="wechat",
            period=SCALE_PERIOD,
            order_no=no,
            amount=Decimal(amount),
            trade_time=datetime(2026, 9, 30, 10, 0),
            source_file="x.csv",
            source_row_no=row,
        )

    his = [_rec("EX-1", "100.00", 1), _rec("EX-1", "30.00", 2)]
    chan = [_rec("EX-1", "130.00", 10)]
    verdicts, _ = baseline_compare.run_vlookup_baseline(chan, his)
    assert len(verdicts) == 1
    assert verdicts[0].hit_count == 2
    assert verdicts[0].picked is his[0]
    assert [r.ref for r in verdicts[0].dropped] == ["x.csv:2"]


# ─────────────────── 五、脚本可从命令行跑通 ───────────────────


def test_gen_fixtures_and_bench_run_as_console_scripts(tmp_path):
    """三个脚本都能按 README 里的命令跑通（不是只能在 pytest 里 import）。"""
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    fx = str(tmp_path / "fx")
    gen = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "gen_fixtures.py"),
         "--out", fx, "--rows", "300", "--his-rows", "400"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert gen.returncode == 0, gen.stderr
    assert "样例已生成" in gen.stdout

    db = str(tmp_path / "b.sqlite")
    b = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "bench.py"),
         "--fixtures", fx, "--period", SCALE_PERIOD, "--db", db,
         "--out", str(tmp_path / "bench.json")],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert b.returncode == 0, b.stderr
    assert "规模压测结果" in b.stdout
    assert "流式腿峰值" in b.stdout

    c = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "baseline_compare.py"),
         "--db", db, "--period", SCALE_PERIOD,
         "--out", str(tmp_path / "cmp.md"), "--json-out", str(tmp_path / "cmp.json")],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert c.returncode == 0, c.stderr + c.stdout
    md = (tmp_path / "cmp.md").read_text(encoding="utf-8")
    assert "对拍报告" in md
    payload = json.loads((tmp_path / "cmp.json").read_text(encoding="utf-8"))
    assert payload["silently_picked_count"] > 0


def test_main_path_still_works_on_the_small_sample():
    """主干未被本任务的样例污染：2026-W36 微信样例仍按原样跑通。"""
    from reconq.cli import main

    code = main([
        "verify", "--period", "2026-W36",
        "--source", os.path.join(ROOT, "tests", "fixtures", "bills"),
        "--channel", "wechat",
    ])
    assert code == 1, "含 fail 的账期退出码应为 1（门禁不通过）"
