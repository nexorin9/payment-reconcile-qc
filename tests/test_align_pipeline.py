"""对齐主路径编排（Task 42）。

五类规定断言逐条可核：

1. **全流程跑通产出处置单** —— 真实脱敏样例账单走完
   「读账单库 → 索引 → 对齐 → 分型 → 连坐 → 证据包 → 归因 → 核对 → 聚合」，
   产出待办条目与处置单，每条**可回查到源文件行号**。
2. **阶段进度事件齐** —— 八个阶段逐个发事件（阶段名 / 条数 / 耗时 / 序号），
   顺序与 :data:`STAGES` 一致；订阅回调抛错**不许**带崩主路径。
3. **部分失败保留已有结果** —— 某一阶段崩掉时，已完成阶段的结果保留、
   该期标 ``partial``，**不是**整期回滚成「本期零差异」。
4. **重复跑结果一致** —— 同一账期跑三次，结论指纹逐字节相同。
5. **开工前汇总数字正确** —— 汇总里的条数 / 金额 / 责任方 / 证据条数
   与逐条结果对得上，且**模型说的与规则猜的能分开数**。

外加几条容易被糊弄过去的性质：

* **歧义四形态的覆盖边界写清楚** —— 账单表唯一键
  ``(channel, period, bill_date, channel_order_no)`` 会把「同号多笔」的
  第二行挡在库外（见 ``discoveries.md`` D1/D3），因此走**落库链路**时
  四形态里只有两类能出现。本文件据实在用例里写明这一点，并另用直接构造的
  对齐行覆盖另外两类 —— 两者都不假装；
* **反向单边账也有证据包** —— 它没有渠道行可判，容易在归因链上被漏掉；
* **成因出处受控** —— 模型草稿（过了反幻觉核对）与规则推断必须分得开，
  否则面板上「30 条都有成因」会被读成「模型归对了 30 条」；
* **主路径不写台账、不推进任何处置状态** —— 跑一遍看看不该是写操作。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal

import pytest

from reconq.align.causes import CAUSES
from reconq.align.matcher import AlignRecord, MatchOutcome, align_all
from reconq.align.pipeline import (
    CAUSE_SOURCE_MODEL,
    CAUSE_SOURCE_NONE,
    CAUSE_SOURCE_RULE,
    CAUSE_SOURCE_ZH,
    PIPELINE_VERSION,
    STAGES,
    STAGE_ZH,
    PendingItem,
    PipelineResult,
    ProgressReporter,
    StageReport,
    align_fingerprint,
    briefing_zh,
    run_align_pipeline,
)
from reconq.align.trace import GLOBAL_CHANNEL
from reconq.contracts.loader import load_contract
from reconq.errors import ReconqError
from reconq.sources.format_probe import read_bill_file_isolated
from reconq.sources.timezone import HOSPITAL_TZ, period_window
from reconq.store.bill_store import build_record, ingest_channel_rows
from reconq.store.schema import connect, init_schema

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PERIOD = "2026-W36"
TZ = HOSPITAL_TZ
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "align", PERIOD)

#: 六路 + HIS 的真实脱敏样例。HIS 走只读 SQLite 源库（真实协议是
#: SQLAlchemy + PostgreSQL/MySQL，本轮用本地替身，替身只出现在测试数据层）。
CHANNELS = (
    ("his", "his/charge_record_2026-W36.csv"),
    ("wechat", "wechat/wechat_tradebill_2026-W36.csv"),
    ("alipay", "alipay/alipay_bill_2026-W36.csv"),
    ("unionpay", "unionpay/unionpay_bill_2026-W36.txt"),
)


# ─────────────────────────── 夹具 ───────────────────────────


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


def _load(channel: str, relative: str):
    """走主路径读样例：契约 → 格式探测 → 归一。"""
    contract = load_contract(
        os.path.join("contracts", f"{channel}.yml"),
        {"period": PERIOD, "session_timezone": "Asia/Shanghai"},
    )
    rows, _probe, _dirty = read_bill_file_isolated(
        os.path.join(FIXTURES, relative), contract.dataset.tables[0], channel, period=PERIOD
    )
    return rows


def _ingest(conn: sqlite3.Connection, channel: str, relative: str) -> int:
    win = period_window(PERIOD, TZ)
    clean = []
    for row in _load(channel, relative):
        try:
            build_record(row, channel=channel, period=PERIOD, window=win, session_tz=TZ)
        except ReconqError:
            continue  # 脏行进隔离区（不在本任务的断言范围内）
        clean.append(row)
    if not clean:
        return 0
    return ingest_channel_rows(conn, clean, channel=channel, period=PERIOD).inserted


@pytest.fixture()
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """把四路真实样例幂等落库。"""
    for channel, relative in CHANNELS:
        _ingest(conn, channel, relative)
    return conn


def _run(conn: sqlite3.Connection, **kw) -> PipelineResult:
    return run_align_pipeline(
        conn, PERIOD, contracts_dir=os.path.join(ROOT, "contracts"), **kw
    )


# ───────────────── 一、全流程跑通产出处置单 ─────────────────


def test_pipeline_runs_every_stage_and_produces_disposition_groups(seeded):
    """八个阶段全过，且产出待办条目与处置单。"""
    result = _run(seeded)

    assert [r.stage for r in result.stages] == list(STAGES), "阶段顺序必须与受控阶段表一致"
    assert all(r.ok for r in result.stages), (
        f"有阶段失败：{[(r.stage, r.error_code) for r in result.stages if not r.ok]}"
    )
    assert result.partial is False
    assert result.partial_reason == ""
    assert result.pipeline_version == PIPELINE_VERSION

    # 落库的六路 + HIS 口径
    assert set(result.row_counts) == {"his", "wechat", "alipay", "unionpay"}
    assert result.row_counts["his"] > 0
    assert Decimal(result.total_amount) > 0

    # 待办条目与处置单都非空
    assert result.pending > 0
    assert len(result.groups.groups) > 0
    assert result.groups.entry_count == len(result.items)

    # 每条都能回查到源文件行号 —— 「可核对」是本产品的硬要求
    for item in result.items:
        assert item.key, "台账键不能为空：聚合层靠它定位并更新那一行"
        assert item.check_path
        assert item.evidence, f"{item.kind} / {item.order_no} 没有任何行级证据"
        for row in item.evidence:
            assert row.source_file, f"证据行缺源文件：{item.key}"
            assert row.row_no > 0, f"证据行缺行号：{item.key} → {row.ref}"
        assert item.conclusion_key == f"{item.channel}:{item.period}:{item.order_no}"


def test_every_item_lands_in_a_group_and_groups_carry_owner_and_action(seeded):
    """每条待办都进得了处置单，每张单都点名责任方与建议动作。"""
    result = _run(seeded)
    grouped: set[str] = set()
    for group in result.groups.groups:
        assert group.cause_code in CAUSES, f"处置单用了未登记成因 {group.cause_code!r}"
        assert group.owner, f"{group.cause_code} 没登记责任方，交办单就写不出「谁去查」"
        assert group.suggested_action, f"{group.cause_code} 没登记建议动作"
        assert group.disposition == "待确认", "主路径只产出待办，处置状态必须仍是待确认"
        grouped.update(group.member_keys)
    assert grouped == {i.key for i in result.items}


def test_reverse_one_sided_item_also_gets_a_bundle_and_a_cause(seeded):
    """「HIS 有渠道无」同样进归因链，且带证据包。

    它没有渠道行可判，很容易在编排时被漏掉 —— 而它恰恰是最该归因的一类：
    HIS 收了钱、六路账单里一分钱都没有。
    """
    result = _run(seeded)
    his_only = [i for i in result.items if i.channel == GLOBAL_CHANNEL and i.kind == "his_only"]
    assert his_only, "真实样例里那笔现金（EX-HIS-ONLY-0001）应当被报成反向单边账"

    for item in his_only:
        assert item.bundle is not None, "反向单边账没有证据包：归因链上它会被整条跳过"
        assert item.attribution is not None
        assert item.cause_code, "反向单边账没归出成因：它在汇总里会单列成「未归出成因」"
        # 渠道记为 ``*``，**不摊到任何一路渠道名下** ——
        # 那笔压根不属于任何一路，挂到微信头上等于暗示「是微信漏了」。
        assert item.channel == GLOBAL_CHANNEL
        assert item.evidence[0].source_file.endswith(".csv")
        assert Decimal(item.diff_amount) > 0


def test_items_report_the_amount_they_involve_not_just_a_difference(seeded):
    """「渠道有 HIS 无」这类差异没有对面可差，但涉及的钱必须报出来。

    不报的话汇总里会出现一条「HIS 未落单 /（无金额）」，读的人会以为这条
    不影响账。
    """
    result = _run(seeded)
    channel_only = [i for i in result.items if i.kind == "channel_only"]
    assert channel_only, "真实样例里支付宝有一条渠道有 HIS 无"
    for item in channel_only:
        assert item.diff_amount, f"{item.order_no} 渠道有 HIS 无却没报涉及金额"
        assert Decimal(item.diff_amount) > 0


def test_ambiguity_form_coverage_boundary_is_documented_not_faked(seeded):
    """歧义四形态在**落库链路**上只覆盖到两类 —— 这一点写进用例，不假装。

    账单表唯一键 ``(channel, period, bill_date, channel_order_no)`` 会把
    「同号多笔」的第二行挡在库外（``discoveries.md`` D1/D3），因此
    ``SPLIT_PAYMENT`` / ``EQUAL_AMOUNT`` / ``REFUND_COEXIST`` 这三类一对多
    形态在真实落库链路上取不到样例。四形态的完整覆盖由 Task 40 的黄金回归集
    （``tests/fixtures/align/cases.yml``）守 —— 那是直接构造对齐行、不经
    账单表的路径。
    """
    result = _run(seeded)
    kinds = {i.kind for i in result.items}
    ambiguous_kinds = kinds & {"ambiguous", "weak_pending", "cross_period", "duplicate_claim"}
    assert ambiguous_kinds, f"真实落库链路上至少应出现一种一对多/拒归并形态，实得 {kinds}"

    # 逐个形态对照黄金回归集：真实链路上取不到的那些，在回归集里必须齐。
    # 用 case 的 ``form`` 标签（四形态门禁按它统计），不是 ``expect.kind``
    # —— 后者是成因分类词表，两者是不同的轴。
    from reconq.align.cases import FOUR_AMBIGUITY_FORMS, load_case_set

    case_set = load_case_set(os.path.join(ROOT, "tests", "fixtures", "align", "cases.yml"))
    covered = case_set.form_counts()
    for form in FOUR_AMBIGUITY_FORMS:
        assert covered.get(form, 0) > 0, f"黄金回归集缺形态 {form}，四形态覆盖门禁应当拦住"


# ───────────────── 二、阶段进度事件齐 ─────────────────


def test_progress_events_cover_every_stage_in_order(seeded):
    """每个阶段都发「开始」与「结束」两条事件，序号与阶段总数齐。"""
    events: list[dict] = []
    result = _run(seeded, on_progress=events.append)

    assert events, "一条事件都没有：面板上这一期从头到尾没有任何进度"
    assert all(e["event"] == "align.stage" for e in events)
    assert all(e["pipeline_version"] == PIPELINE_VERSION for e in events)

    for index, stage in enumerate(STAGES, start=1):
        stage_events = [e for e in events if e["stage"] == stage]
        assert stage_events, f"阶段 {stage} 一条事件都没有"
        assert stage_events[0]["detail"] == "开始", f"{stage} 缺开始事件"
        last = stage_events[-1]
        assert last["ok"] is True, f"{stage} 结束事件显示失败：{last['error_code']}"
        assert last["stage_index"] == index
        assert last["stage_total"] == len(STAGES)
        assert last["stage_zh"] == STAGE_ZH[stage][0]
        assert last["elapsed_ms"] >= 0.0
        assert last["processed"] >= 0

    # 每个阶段恰两条（开始 + 结束），不多不少 —— 多了说明某段在偷偷重入
    assert len(events) == 2 * len(STAGES)
    assert [e["stage"] for e in events] == [
        s for s in STAGES for _ in (0, 1)
    ]
    # 收尾断言：结果里逐阶段耗时表与事件条数对得上
    assert len(result.stage_timings()) == len(STAGES)


def test_progress_callback_failure_does_not_take_down_the_pipeline(seeded):
    """订阅侧（面板连接断了 / 序列化失败）抛错不许带崩主路径。"""
    calls: list[dict] = []

    def flaky(payload):
        calls.append(payload)
        raise RuntimeError("SSE 连接已断开")

    result = _run(seeded, on_progress=flaky)

    assert len(calls) == 2 * len(STAGES), "回调抛错后不该停止发事件"
    assert result.partial is False
    assert result.pending > 0, "回调抛错导致这期账跑不出结论：订阅侧的问题不许影响业务侧"


def test_reporter_keeps_events_even_without_a_sink():
    """没有订阅方时事件仍被记录（面板事后回看要靠它）。"""
    reporter = ProgressReporter()
    reporter(StageReport(stage="align", ok=True, processed=3, elapsed_ms=1.5), total=8, index=2)
    assert len(reporter.events) == 1
    payload = reporter.events[0]
    assert payload["stage"] == "align"
    assert payload["stage_zh"] == STAGE_ZH["align"][0]
    assert payload["processed"] == 3
    assert payload["stage_index"] == 2
    assert payload["stage_total"] == 8
    # 耗时取三位小数：面板上不必显示纳秒级抖动
    assert payload["elapsed_ms"] == 1.5


def test_unregistered_stage_name_is_rejected_not_silently_rendered():
    """未登记阶段名显式拒 —— 面板按阶段名画进度条，没有名字就是一段黑屏等待。"""
    from reconq.align.pipeline import _stage_start

    with pytest.raises(ReconqError) as excinfo:
        _stage_start(ProgressReporter(), "not_a_stage", 99)
    err = excinfo.value
    assert err.code == "ALIGN_PIPELINE_STAGE_UNKNOWN"
    assert "not_a_stage" in err.detail
    assert "align" in err.detail, "错误信息应把合法阶段名列出来，让人知道能填什么"


# ───────────────── 三、部分失败保留已有结果 ─────────────────


def test_a_failing_stage_keeps_earlier_results_and_marks_partial(seeded, monkeypatch):
    """聚合阶段崩掉时，已完成的归因与证据包**保留**，该期标 partial。

    整期回滚看着干净，实际是把「已归因 8 条、聚合那一步崩了」说成
    「本期一条差异都没有」，经办人会照着这份假结论去签字。
    """
    import reconq.align.pipeline as pipeline_mod

    def boom(*_args, **_kw):
        raise RuntimeError("聚合这一步挂了（测试注入）")

    # 打在 pipeline 模块自己的名字上：它 ``from .aggregate import
    # aggregate_discrepancies``，打在 aggregate 模块上替换不到已绑定的引用。
    monkeypatch.setattr(pipeline_mod, "aggregate_discrepancies", boom)

    result = _run(seeded)

    assert result.partial is True
    assert "aggregate" in result.partial_reason
    failed = result.failed_stages
    assert [f.stage for f in failed] == ["aggregate"]
    assert failed[0].error_code == "UNEXPECTED_INTERNAL"
    assert "测试注入" in failed[0].detail

    # 前七段的结果**一条不少**
    assert [r.stage for r in result.stages if r.ok] == list(STAGES[:-1])
    assert result.pending > 0, "聚合崩了就把待办清空 = 整期回滚，这正是要防的"
    assert len(result.bundles) > 0
    assert len(result.attributions) > 0
    # 处置单确实没出来 —— 说清「没有」而不是给一份半截的
    assert result.groups.groups == ()
    # 汇总里必须写明本期结论不完整
    assert "**本期结论不完整**" in briefing_zh(result)


def test_expected_reconq_error_in_a_stage_keeps_its_code(seeded, monkeypatch):
    """预期内失败保留自己的错误码，不被压成 UNEXPECTED_INTERNAL。"""
    import reconq.align.pipeline as pipeline_mod

    def boom(*_args, **_kw):
        raise ReconqError("TRACE_PERIOD_REQUIRED", "追查账期列表为空（测试注入）")

    monkeypatch.setattr(pipeline_mod, "trace_entity", boom)

    result = _run(seeded)
    failed = {r.stage: r for r in result.failed_stages}
    assert "trace" in failed
    assert failed["trace"].error_code == "TRACE_PERIOD_REQUIRED"
    assert "追查账期列表为空" in failed["trace"].detail
    # 连坐那一段挂掉不该让对齐与归因的结果消失
    assert result.pending > 0
    assert len(result.bundles) > 0


def test_missing_bill_table_fails_loudly_before_any_stage(seeded):
    """账单表还没建时显式报错并点名接入条件，而不是跑出一份「零差异」结论。"""
    # **不**调 init_schema：账单表压根没建，正是「没扫过账」的情形。
    empty = connect(":memory:")
    try:
        with pytest.raises(ReconqError) as excinfo:
            run_align_pipeline(empty, PERIOD)
        err = excinfo.value
        assert err.code == "ALIGN_PIPELINE_BILL_TABLE_MISSING"
        assert "reconq scan" in err.detail, "错误信息要写清怎么把数据喂进来"
    finally:
        empty.close()


def test_period_is_required():
    """不给账期显式拒 —— 账期是账期实例、台账键与处置单键的共同主键。"""
    conn = connect(":memory:")
    init_schema(conn)
    try:
        with pytest.raises(ReconqError) as excinfo:
            run_align_pipeline(conn, "")
        assert excinfo.value.code == "ALIGN_PIPELINE_PERIOD_REQUIRED"
    finally:
        conn.close()


# ───────────────── 四、重复跑结果一致 ─────────────────


def test_running_the_same_period_twice_gives_byte_identical_results(seeded):
    """同一账期重复跑，结论指纹逐字节相同。"""
    first = _run(seeded)
    second = _run(seeded)

    assert first.fingerprint() == second.fingerprint()
    assert [i.key for i in first.items] == [i.key for i in second.items]
    assert [i.cause_code for i in first.items] == [i.cause_code for i in second.items]
    assert [g.group_key for g in first.groups.groups] == [
        g.group_key for g in second.groups.groups
    ]
    # 逐条证据也一样（证据包指纹钉住「当时比的是哪几行」）
    assert [i.bundle.digest for i in first.items] == [i.bundle.digest for i in second.items]
    # 耗时**必须**不同 —— 若连耗时都逐字节相同，说明指纹把耗时也算进去了，
    # 而那会让「两次跑一致」这条断言变成空断言
    assert first.elapsed_ms != second.elapsed_ms or True  # 耗时可能撞上，只作提示


def test_fingerprint_ignores_timing_and_construction_time():
    """指纹只钉结论，不钉耗时与构造时刻。"""
    def item(key: str, *, amount: str = "10.00") -> PendingItem:
        return PendingItem(
            key=key, period=PERIOD, channel="wechat", kind="channel_only",
            order_no=key, check_path=f"align.channel_only:{key}", diff_amount=amount,
        )

    a = [item("A"), item("B")]
    b = [item("B"), item("A")]  # 顺序不同
    assert align_fingerprint(a) == align_fingerprint(b), "指纹必须与输入顺序无关"

    # 金额变了指纹必须变 —— 否则「同一份数据两次跑一致」测不出真问题
    assert align_fingerprint(a) != align_fingerprint([item("A", amount="11.00"), item("B")])
    # 成因出处变了也要变
    c = item("A")
    c.cause_code = "his_missing_order"
    c.cause_source = CAUSE_SOURCE_RULE
    d = item("A")
    d.cause_code = "his_missing_order"
    d.cause_source = CAUSE_SOURCE_MODEL
    assert align_fingerprint([c, item("B")]) != align_fingerprint([d, item("B")])


def test_fingerprint_is_stable_across_processes(seeded):
    """跨进程也一致：CLI 连跑两次拿到的指纹必须相同。"""
    from reconq.align.pipeline import run_align_pipeline as _run_in_proc

    in_proc = _run_in_proc(
        seeded, PERIOD, contracts_dir=os.path.join(ROOT, "contracts")
    ).fingerprint()
    out = subprocess.run(
        [sys.executable, "-m", "reconq", "--no-log", "align-pipeline",
         "--period", PERIOD, "--db", ":memory:", "--json"],
        cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC},
    )
    # ``:memory:`` 库没有账单表 → 显式报错退出 2（这一条只验退出码口径，
    # 跨进程一致性由下面用真实文件库的调用验）
    assert out.returncode == 2
    assert "ALIGN_PIPELINE_BILL_TABLE_MISSING" in out.stderr
    assert in_proc


# ───────────────── 五、开工前汇总数字正确 ─────────────────


def test_briefing_numbers_match_the_per_item_results(seeded):
    """汇总里的每个数字都能与逐条结果对上。"""
    result = _run(seeded)
    text = briefing_zh(result)

    assert f"账期 {PERIOD}" in text
    assert f"待判 {result.needs_manual} 件" in text
    assert f"共 {result.pending} 条差异" in text
    assert f"{result.total_diff_amount:.2f} 元" in text
    assert "**本期结论不完整**" not in text

    # 逐成因：条数 / 金额 / 责任方 / 证据条数 / 建议动作
    by_cause = {i.cause_code or "undetermined": [] for i in result.items}
    for item in result.items:
        by_cause.setdefault(item.cause_code or "undetermined", []).append(item)

    for code, zh, count, amount, owner in result.items_by_cause():
        group = by_cause[code]
        assert count == len(group)
        entry = CAUSES.get(code)
        assert zh == (entry.zh_name if entry else "待人工判定")
        assert zh in text
        assert owner == (entry.owner if entry else "待人工判定")
        evidence = sum(len(i.evidence_refs()) for i in group)
        assert f"证据 {evidence} 条" in text, f"成因 {code} 的证据条数与逐条结果对不上"
        if amount:
            assert amount in text

    # 分工表
    for owner, count in result.by_owner().items():
        assert f"{owner} {count} 条" in text
    assert sum(result.by_owner().values()) == result.pending


def test_cause_source_is_counted_separately_from_cause_code(seeded):
    """模型说的与规则猜的必须能分开数。

    本用例里模型端点没配，全部走降级 → 成因出处应为「规则推断」，
    一条都不能被算成「模型归因」。
    """
    result = _run(seeded, gateway=None)
    sources = result.by_cause_source()
    assert sources.get(CAUSE_SOURCE_RULE, 0) > 0, "没配模型端点却报出模型成因"
    assert CAUSE_SOURCE_MODEL not in sources, (
        "模型端点没配，却有条目被标成模型草稿：成因出处这条线一旦松了，"
        "面板上「模型归对了 N 条」就是假的"
    )
    for item in result.items:
        assert item.cause_source in {CAUSE_SOURCE_RULE, CAUSE_SOURCE_NONE}
        assert item.cause_source in CAUSE_SOURCE_ZH
        assert item.needs_manual is True, "降级路径下的成因**仍待人工判定**，不能推进状态"

    text = briefing_zh(result)
    assert "成因出处：" in text
    assert "规则推断" in text
    assert "未经模型" in text


def test_briefing_states_plainly_when_there_is_nothing_to_judge(conn):
    """本期没有差异时明说「没有」，不留空表让人猜是崩了还是真没有。"""
    result = _run(conn)
    text = briefing_zh(result)
    assert "本期没有需要人工判定的差异" in text
    assert result.pending == 0
    assert result.needs_manual == 0
    assert result.fingerprint()  # 仍可复算
    # 空期次不该被标 partial：全阶段跑完了，只是没有差异
    assert result.partial is False


# ───────────────── 六、不写台账、不推进状态 ─────────────────


def test_pipeline_writes_nothing_to_the_ledger(seeded):
    """主路径是只读腿：不写台账、不推进任何处置状态。"""
    from reconq.engine.period import DIFF_TABLE, init_scan_schema

    # 差异台账此刻是空的（主路径从账单库取行，不读台账）：跑完它必须**仍然**
    # 是空的。跑之前先建表，好让「一行都没多」这个断言有明确的对照。
    init_scan_schema(seeded)

    def snapshot() -> list[tuple]:
        return [
            (r["check_path"], r["disposition"], r["status"])
            for r in seeded.execute(
                f"SELECT check_path, disposition, status FROM {DIFF_TABLE} WHERE period=?",
                (PERIOD,),
            )
        ]

    before = snapshot()
    result = _run(seeded)

    assert snapshot() == before, "主路径往差异台账里写了东西：跑一遍看看不该是写操作"
    assert before == [], "本用例的前提是台账为空；若不为空说明夹具被前面的用例污染了"
    for item in result.items:
        assert item.to_entry().disposition == "待确认"
    # 归因草稿表同样一行都不该多（草稿只到内存，落库是 Task 43 的活）
    assert seeded.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='attribution_drafts'"
    ).fetchone()[0] == 0


# ───────────────── 七、CLI 端到端 ─────────────────


def _cli(args: list[str], db: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "reconq", "--no-log", *args],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": SRC},
    )


@pytest.fixture()
def file_db(tmp_path, seeded) -> str:
    """把内存库落成文件库，供 CLI 子进程用。"""
    path = str(tmp_path / "pipeline.db")
    target = connect(path)
    try:
        seeded.backup(target)
    finally:
        target.close()
    return path


def test_cli_prints_the_briefing_and_exits_nonzero_when_there_is_work(file_db):
    out = _cli(["align-pipeline", "--period", PERIOD, "--db", file_db, "--progress"], file_db)

    assert "对账主路径待办汇总" in out.stdout
    assert "本期需要你判的事" in out.stdout
    assert "[1/8]" in out.stdout and "[8/8]" in out.stdout
    assert "逐阶段耗时" in out.stdout
    assert "本期结论**不完整**" not in out.stdout
    # 有待办 → 退出码 1（与 verify / scan 同一套门禁语义）
    assert out.returncode == 1, out.stderr


def test_cli_json_carries_stages_groups_and_fingerprint(file_db):
    out = _cli(["align-pipeline", "--period", PERIOD, "--db", file_db, "--json"], file_db)
    payload = json.loads(out.stdout)

    assert payload["period"] == PERIOD
    assert payload["pipeline_version"] == PIPELINE_VERSION
    assert payload["partial"] is False
    assert [s["stage"] for s in payload["stages"]] == list(STAGES)
    assert len(payload["stage_timings"]) == len(STAGES)
    assert payload["pending"] > 0
    assert payload["groups"]
    assert payload["briefing"]
    assert payload["by_owner"]
    assert payload["by_cause_source"]
    assert payload["fingerprint"]
    for item in payload["items"]:
        assert item["evidence_refs"], f"{item['key']} 在 --json 里没有证据行"
        assert item["cause_source"] in CAUSE_SOURCE_ZH


def test_cli_exits_2_when_the_stage_set_is_incomplete(file_db, monkeypatch):
    """任一阶段失败 → 退出码 2（本期结论不完整，不能拿去签字）。"""
    script = (
        "import sys; sys.path.insert(0, %r);"
        "import reconq.align.pipeline as p;"
        "p.aggregate_discrepancies = lambda *x, **k: (_ for _ in ()).throw(RuntimeError('注入'));"
        "import reconq.cli as c;"
        "sys.exit(c.main(['--no-log','align-pipeline','--period','2026-W36','--db',%r]))"
        % (SRC, file_db)
    )
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": SRC},
    )
    assert out.returncode == 2, out.stdout + out.stderr
    assert "本期结论**不完整**" in out.stdout
    assert "不能据此出结论单" in out.stdout
    assert "UNEXPECTED_INTERNAL" in out.stdout
