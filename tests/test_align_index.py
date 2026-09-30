"""对齐倒排索引与账期分片（Task 22）。

六类规定断言逐条可核：

1. **倒排命中** —— 单号 → 候选记录，一条不少；
2. **单号规范化等价** —— 大小写 / 全半角 / 兼容字符折叠后同键；
3. **空格与全角变体归一** —— 院内导出的四五种写法互相命中；
4. **账期分片跨界记录双片** —— 跨账期记录同时在本片与邻片，各带正确标记；
5. **预算超限 spill** —— 常驻条目回落到预算内，spill 计数与落盘文件可核，
   且**已落盘的键仍能按 offset 回查命中**（spill 不能让记录查不到）；
6. **单号多值冲突计数** —— 冲突键数与实际一对多单号数一致。

外加几条**必须成立的不变量**：spill 之后弱匹配（按金额 + 时间窗找候选）
仍能看到全集 —— 拿已 spill 的倒排表当全集，会让「找不到候选」直接落成
单边账，财务科看到的是「渠道有 HIS 无」，而那笔钱其实在库里。
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime
from decimal import Decimal

import pytest

from reconq.align.index import (
    DEFAULT_INDEX_BUDGET_MB,
    DEFAULT_INDEX_CACHE_SIZE,
    DEFAULT_SPILL_ENTRY_THRESHOLD,
    INDEX_BUDGET_ENV,
    INDEX_CACHE_SIZE_ENV,
    INDEX_SPILL_THRESHOLD_ENV,
    InvertedIndex,
    ShardedIndex,
    build_sharded_index,
    index_budget_bytes_from_env,
    normalize_key,
    period_of,
)
from reconq.align.matcher import AlignRecord, align_channel, build_index
from reconq.errors import ReconqError
from reconq.sources.timezone import period_window, to_session_tz

PERIOD = "2026-W36"
PREV_PERIOD = "2026-W35"
NEXT_PERIOD = "2026-W37"
TZ = to_session_tz(None)
WINDOW = period_window(PERIOD, TZ)


def _his(order_no: str, amount: str = "128.00", *, row: int = 1, ch_id: int = 0,
         when: datetime | None = None, his_order_no: str = "") -> AlignRecord:
    """一条 HIS 侧记录。默认落在账期窗口内。"""
    return AlignRecord(
        channel="his",
        period=PERIOD,
        order_no=order_no,
        amount=Decimal(amount),
        trade_time=when or datetime(2026, 9, 2, 10, 30, tzinfo=TZ),
        his_order_no=his_order_no,
        source_file="charge_2026-W36.csv",
        source_row_no=row,
        ch_id=ch_id,
    )


# —— 1. 倒排命中 ——


def test_lookup_returns_every_candidate_for_a_key():
    index = InvertedIndex(PERIOD)
    a, b = _his("EX-1", row=1, ch_id=1), _his("EX-1", "56.50", row=2, ch_id=2)
    index.add(a)
    index.add(b)

    hits = index.lookup("EX-1")
    assert [r.ch_id for r in hits] == [1, 2]
    # 单号缺失时返回空列表，不拿「全体」当候选
    assert index.lookup("") == []
    assert index.lookup("   ") == []
    assert index.lookup("NOT-EXIST") == []


def test_both_order_no_and_his_order_no_are_indexed():
    """HIS 侧只给其中一个单号时两个字段都能查到（院内导出常见）。"""
    index = InvertedIndex(PERIOD)
    rec = _his("HIS-ONLY-1", "90.00", ch_id=9, his_order_no="HIS-ONLY-1")
    index.add(rec)
    assert len(index.lookup("his-only-1")) == 1
    # 只给 order_no、his_order_no 为空时也不能漏
    index2 = InvertedIndex(PERIOD)
    index2.add(_his("ORDER-ONLY-2", ch_id=10))
    assert len(index2.lookup("order-only-2")) == 1


# —— 2. 单号规范化等价 ——


def test_normalize_key_folds_case_and_fullwidth():
    assert normalize_key("WX-ABC123") == normalize_key("wx-abc123") == "wx-abc123"
    # 全角字母数字与全角连字符 → 半角
    assert normalize_key("ＷＸ－ＡＢＣ１２３") == "wx-abc123"
    # 全角空格（U+3000）也在 NFKC 折叠范围内
    assert normalize_key("　wx-abc123　") == "wx-abc123"
    # NFKC 顺带折叠兼容字符：① → 1
    assert normalize_key("①") == "1"
    # 大小写不同的两笔被判为同一个键 —— 这是"同一个单号"的本意
    assert normalize_key("Wx-AbC") == normalize_key("wx-abc")


def test_normalize_key_rejects_blank():
    for value in (None, "", "   ", "\t\n", "　"):
        assert normalize_key(value) == ""


# —— 3. 空格与全角变体归一（台账上常见的几种写法）——


@pytest.mark.parametrize(
    "stored,queried",
    [
        ("wx-abc123", " WX-ABC123 "),
        ("wx-abc123", "ＷＸ－ＡＢＣ１２３"),
        ("wx-abc123", "Ｗｘ－Ａｂｃ１２３ "),
        ("  wx-abc123", "wx-abc123"),
    ],
)
def test_order_no_variants_hit_the_same_key(stored, queried):
    index = InvertedIndex(PERIOD)
    index.add(_his(stored, ch_id=7))
    assert [r.ch_id for r in index.lookup(queried)] == [7]


def test_original_value_is_preserved_for_display():
    """台账要给财务科看他们系统里那个写法，不是我们归一后的样子。"""
    index = InvertedIndex(PERIOD)
    index.add(_his("  WX-ABC123  ", ch_id=7))
    assert index.lookup("wx-abc123")  # 归一后可命中
    assert index.original_value("wx-abc123") == "WX-ABC123"  # 展示保留原写法
    # 没建过键时回落入参本身，不返回空串让人以为「这单没有原值」
    assert index.original_value("NEVER-SEEN") == "NEVER-SEEN"
    assert index.original_value("  ") == ""


# —— 4. 账期分片跨界记录双片 ——


def test_period_of_uses_the_period_window_timezone():
    inside = datetime(2026, 9, 2, 10, 30, tzinfo=TZ)
    assert period_of(inside, WINDOW) == PERIOD
    # 周一 00:00:00 归**新**账期（半开区间），不是上一期
    monday_zero = datetime(2026, 9, 7, 0, 0, 0, tzinfo=TZ)
    assert period_of(monday_zero, WINDOW) == NEXT_PERIOD
    # 跨年周边界也由 isocalendar 算对
    year_edge = period_window("2027-W01", TZ)
    assert period_of(datetime(2027, 1, 4, 9, 0, tzinfo=TZ), year_edge) == "2027-W01"


def test_cross_period_record_lands_in_both_adjacent_shards():
    idx = build_sharded_index([], period=PERIOD, window=WINDOW)
    # 上一账期的一条：2026-W36 的窗口是 [08-31 00:00, 09-07 00:00)，
    # 所以 08-30 23:30 属于 W35，08-31 23:30 已经是 W36 了（周一）。
    prev = _his("CROSS-1", when=datetime(2026, 8, 30, 23, 30, tzinfo=TZ), ch_id=1)
    idx.add(prev)

    assert set(idx.shard_periods) == {PERIOD, PREV_PERIOD}
    # 本片能查到（跨期落点候选视角）
    assert len(idx.lookup("CROSS-1")) == 1
    # 邻片也能查到（它本来就是那一期的正常收费）
    assert len(idx.shard(PREV_PERIOD).lookup("CROSS-1")) == 1
    # 两种视角靠 cross_period 标记区分，绝不静默择一
    hits = idx.hits("CROSS-1")
    by_shard = {h.shard: h for h in hits}
    assert set(by_shard) == {PERIOD, PREV_PERIOD}
    assert by_shard[PERIOD].cross_period is True
    assert by_shard[PREV_PERIOD].cross_period is False
    assert idx.stats().cross_period_entries == 1
    # 记录全集按对象去重：同一笔钱只报一条，不开两张单边账单
    assert len(idx.all) == 1


def test_next_period_record_also_lands_in_both_shards():
    """下一账期的记录同理 —— 双向都不能漏。"""
    idx = build_sharded_index([], period=PERIOD, window=WINDOW)
    idx.add(_his("CROSS-2", when=datetime(2026, 9, 7, 0, 30, tzinfo=TZ), ch_id=1))
    assert set(idx.shard_periods) == {PERIOD, NEXT_PERIOD}
    by_shard = {h.shard: h.cross_period for h in idx.hits("CROSS-2")}
    assert by_shard == {PERIOD: True, NEXT_PERIOD: False}


def test_in_period_record_only_lands_in_its_own_shard():
    idx = build_sharded_index([], period=PERIOD, window=WINDOW)
    idx.add(_his("INSIDE-1", ch_id=1))
    assert idx.shard_periods == [PERIOD]
    hits = idx.hits("INSIDE-1")
    assert len(hits) == 1 and hits[0].cross_period is False
    assert idx.stats().cross_period_entries == 0


def test_record_without_trade_time_goes_to_current_shard_only():
    """没有收费时间就不猜账期 —— 猜一个会让"跨期"变成"正常收费"。"""
    idx = build_sharded_index([], period=PERIOD, window=WINDOW)
    idx.add(_his("NO-TIME", ch_id=1, when=datetime(2026, 9, 2, 10, 30, tzinfo=TZ)))
    idx.records[-1] = AlignRecord(**{**_his("NO-TIME", ch_id=1).__dict__ | {"trade_time": None}})
    assert idx.shard_periods == [PERIOD]
    assert len(idx.lookup("NO-TIME")) == 1


def test_sharded_index_is_interchangeable_with_the_flat_one():
    """分片索引与 Task 21 的扁平索引对同一份数据给出相同对齐结论。"""
    his_rows = [_his("SAME-1", "128.00", row=3, ch_id=1)]
    chan = [
        AlignRecord(
            channel="wechat", period=PERIOD, order_no="SAME-1",
            amount=Decimal("128.00"), trade_time=datetime(2026, 9, 2, 10, 35, tzinfo=TZ),
            source_file="wechat.csv", source_row_no=7,
        )
    ]
    flat = align_channel("wechat", chan, build_index(his_rows, period=PERIOD), period=PERIOD, window=WINDOW)
    sharded = build_sharded_index(his_rows, period=PERIOD, window=WINDOW)
    split = align_channel("wechat", chan, sharded, period=PERIOD, window=WINDOW)
    assert flat.counts() == split.counts()
    assert flat.outcomes[0].status == split.outcomes[0].status == "matched"


# —— 5. 预算超限 spill ——


def test_spill_triggers_on_entry_threshold_and_keeps_lookups_working():
    spill_dir = tempfile.mkdtemp(prefix="reconq-test-spill-")
    index = InvertedIndex(PERIOD, spill_threshold=10, spill_dir=spill_dir)
    for i in range(200):
        index.add(_his(f"BULK-{i:04d}", ch_id=i))

    stats = index.stats()
    assert stats.spill_events >= 1
    assert stats.spilled_keys > 0
    # 常驻键数回落到阈值以内（不是全倒，也不是没倒）
    assert stats.resident_keys < 200
    assert stats.resident_keys <= 10
    assert stats.total_keys == 200  # 键总数不因 spill 改变
    assert stats.spill_path and os.path.exists(stats.spill_path)

    # 关键：已落盘的键仍能命中（按 offset 回查），一条都不能少
    for i in (0, 5, 50, 120, 199):
        hits = index.lookup(f"BULK-{i:04d}")
        assert len(hits) == 1, f"BULK-{i:04d} 在 spill 后查不到"
        assert hits[0].ch_id == i
    index.cleanup()


def test_spill_triggers_on_byte_budget_even_below_entry_threshold():
    """单号特别长时，光键就能吃光预算 —— 双阈值缺一不可。"""
    spill_dir = tempfile.mkdtemp(prefix="reconq-test-spill-bytes-")
    # 条目阈值放到 10000（远高于 200 条），只让字节预算触发
    index = InvertedIndex(
        PERIOD,
        budget_bytes=8 * 1024,
        spill_threshold=10_000,
        spill_dir=spill_dir,
    )
    for i in range(200):
        index.add(_his(f"LONG-ORDER-NO-{i:04d}-{'x' * 40}", ch_id=i))
    stats = index.stats()
    assert stats.spill_events >= 1
    assert stats.resident_bytes <= stats.budget_bytes
    assert len(index.lookup("LONG-ORDER-NO-0199-" + "x" * 40)) == 1
    index.cleanup()


def test_spill_never_hides_records_from_weak_match_candidates():
    """**本模块最要紧的一条不变量**。

    spill 腾的是倒排表；记录全集**永不驱逐**。若拿已 spill 的倒排表当
    弱匹配的全集，「找不到候选」会直接落成单边账 —— 财务科看到的是
    「渠道有 HIS 无」，而那笔钱其实在库里，只是被索引腾走了。
    """
    spill_dir = tempfile.mkdtemp(prefix="reconq-test-spill-weak-")
    index = InvertedIndex(PERIOD, spill_threshold=5, spill_dir=spill_dir)
    for i in range(50):
        index.add(_his(f"WEAK-{i:04d}", f"{100 + i}.50", ch_id=i))

    assert index.stats().spilled_keys > 0
    # 金额弱匹配：单号根本没给，靠金额找 —— 被 spill 的那批也要找得到
    for i in (0, 20, 49):
        cands = index.candidates_by_amount(
            amount=Decimal(f"{100 + i}.50"),
            tolerance=Decimal("0.01"),
            window=WINDOW,
        )
        assert [c.ch_id for c in cands] == [i], f"{i} 被 spill 后弱匹配找不到"
    index.cleanup()


def test_spilled_key_written_again_merges_into_the_reloaded_bucket():
    """落盘的键之后又来一条记录：先回查再追加，不能追加到"只有文件里有"的键上。"""
    spill_dir = tempfile.mkdtemp(prefix="reconq-test-spill-merge-")
    index = InvertedIndex(PERIOD, spill_threshold=2, spill_dir=spill_dir)
    index.add(_his("DUP-A", ch_id=1))
    index.add(_his("FILL-1", ch_id=2))
    index.add(_his("FILL-2", ch_id=3))
    assert "dup-a" in index._spilled, "本用例需要 DUP-A 已被 spill"

    index.add(_his("DUP-A", "56.50", ch_id=2))
    hits = index.lookup("dup-a")
    assert sorted(r.ch_id for r in hits) == [1, 2]
    index.cleanup()


def test_lru_cache_hit_rate_is_observable():
    index = InvertedIndex(PERIOD, cache_size=2)
    for i in range(5):
        index.add(_his(f"LRU-{i}", ch_id=i))

    index.lookup("LRU-0")  # miss
    index.lookup("LRU-0")  # hit
    index.lookup("LRU-1")  # miss（首次）→ 此时缓存满 2 项
    index.lookup("LRU-1")  # hit
    stats = index.stats()
    assert (stats.cache_misses, stats.cache_hits) == (2, 2)
    assert stats.cache_hit_rate == pytest.approx(0.5)

    # 容量上限真的生效：装 2 个键的 LRU 装不下 5 个
    for i in range(2, 5):
        index.lookup(f"LRU-{i}")
    assert len(index._cache) == 2


def test_lru_evicts_by_recency_not_by_insertion_order():
    """淘汰看「最近用过」，不看「最早插入」。

    构造：容量 2。查 A 再查 B（A 先、B 后）→ 现在 **A 才是最久没用过的**，
    尽管 A 查得更早。再查 C 触发淘汰，被挤掉的必须是 A。
    若实现按「最近查过」以外的规则（比如纯插入序、或随机）淘汰，
    这条断言就会失败 —— 插入序里 A 也最早，两种规则在**本例**同结果，
    所以下面另有一条把两者**拧反**的用例。
    """
    index = InvertedIndex(PERIOD, cache_size=2)
    for key in ("A", "B"):
        index.add(_his(key, ch_id=1))
    index.add(_his("C", ch_id=2))
    assert len(index._cache) == 0  # 写入不进缓存，只有查询才进

    index.lookup("A")
    assert list(index._cache) == ["a"]  # 只有查过的进缓存
    index.lookup("B")
    # 缓存键是**归一后**的（小写），倒排表与 LRU 用的是同一套键
    assert list(index._cache) == ["a", "b"]

    index.lookup("C")  # 容量 2 装不下第 3 个 → 淘汰最久没用的 a
    assert list(index._cache) == ["b", "c"]


def test_lru_evicts_the_key_not_looked_up_most_recently():
    """把「插入序」与「最近使用序」**拧反**，让两种规则给出不同答案。

    容量 2；查 B 再查 A（**B 更早、A 更晚**），再查 C 触发淘汰。
    按最近使用序，被挤掉的是 B；按插入序（更早插入的 B 先被淘汰）也是 B
    —— 所以这一条单独还分不开。真正能分开的是下面
    :func:`test_lru_eviction_differs_from_insertion_order`。
    """
    index = InvertedIndex(PERIOD, cache_size=2)
    for key in ("A", "B"):
        index.add(_his(key, ch_id=1))
    index.add(_his("C", ch_id=2))
    index.lookup("B")
    index.lookup("A")
    assert list(index._cache) == ["b", "a"]
    index.lookup("C")
    # A 刚被查过（最新）→ 留；B 更久没碰 → 走
    assert list(index._cache) == ["a", "c"]


def test_lru_eviction_differs_from_insertion_order():
    """**唯一**能把两种规则分开的构造。

    容量 2；先查 A 让它进缓存，再查 B 把 A 顶掉并让 B 进缓存。
    此时再查 A（**它被查过所以写热**，但它也是最早插入的）。
    接着查 C 触发淘汰：
    * 按最近使用序 → 淘汰 B（最后才用的）；
    * 按插入序 → 淘汰 A（最早插入的）。
    断言留下的正是 A 与 C，即可证明实现走的是最近使用序。
    """
    index = InvertedIndex(PERIOD, cache_size=2)
    for key in ("A", "B"):
        index.add(_his(key, ch_id=1))
    index.add(_his("C", ch_id=2))

    index.lookup("A")  # 缓存 {a}
    index.lookup("B")  # 缓存 {a, b}
    index.lookup("A")  # 命中并移到最新 → 顺序 {b, a}
    index.lookup("C")  # 淘汰 b（最久没用）→ {a, c}
    assert list(index._cache) == ["a", "c"]


def test_lookup_marks_a_key_hot_so_spill_keeps_it_resident():
    """被查过的键留在内存里，没被查过的冷键先落盘 —— 这就是「冷块」的判据。

    热度**只**由查询产生，不由「同一个单号被重复写入」产生：一码付拆笔
    在一次批量建索引里本来就会把同一键写 2–3 次，拿重复写入当热信号，
    冷队列会永远为空、spill 永远不触发，预算形同虚设。
    """
    spill_dir = tempfile.mkdtemp(prefix="reconq-test-hot-")
    index = InvertedIndex(PERIOD, spill_threshold=3, spill_dir=spill_dir)
    for i in range(10):
        index.add(_his(f"HOT-{i}", ch_id=i))
    # 查 HOT-0（让它写热），再灌一批把预算顶破
    index.lookup("HOT-0")
    for i in range(10, 40):
        index.add(_his(f"HOT-{i}", ch_id=i))
    assert index.stats().spilled_keys > 0
    # HOT-0 被查过 → 仍在常驻表里，没有被当成冷块落盘
    assert "hot-0" in index._by_no
    assert "hot-0" not in index._spilled
    index.cleanup()


# —— 6. 单号多值冲突计数 ——


def test_conflict_key_count_matches_the_real_ambiguity_volume():
    index = InvertedIndex(PERIOD)
    # 2 个拆笔单号（一码付拆 2 笔）+ 1 个拆 3 笔 + 1 个普通单号
    index.add(_his("SPLIT-2A", row=1, ch_id=1))
    index.add(_his("SPLIT-2A", row=2, ch_id=2))
    index.add(_his("SPLIT-3A", row=3, ch_id=3))
    index.add(_his("SPLIT-3A", row=4, ch_id=4))
    index.add(_his("SPLIT-3A", row=5, ch_id=5))
    index.add(_his("PLAIN-1", row=6, ch_id=6))

    # 冲突键 = 挂着 ≥2 条候选的键数（拆 3 笔仍只算 1 个冲突键）
    assert index.stats().conflict_keys == 2
    # 候选总数照实给：5 条多值 + 1 条单值
    assert index.stats().total_entries == 6
    assert index.stats().total_keys == 3


def test_conflict_count_survives_spill():
    """spill 之后冲突键数不能掉到 0 —— 面板要靠它显示"有多少笔要人工判"。"""
    spill_dir = tempfile.mkdtemp(prefix="reconq-test-spill-conflict-")
    index = InvertedIndex(PERIOD, spill_threshold=2, spill_dir=spill_dir)
    index.add(_his("CF-1A", ch_id=1))
    index.add(_his("CF-1A", "56.50", ch_id=2))
    for i in range(20):
        index.add(_his(f"FILL-{i:04d}", ch_id=100 + i))
    assert index.stats().spilled_keys > 0
    assert index.stats().conflict_keys == 1
    index.cleanup()


# —— 可观测性与配置面 ——


def test_stats_expose_every_required_observable():
    index = InvertedIndex(PERIOD, spill_threshold=3, cache_size=8)
    for i in range(20):
        index.add(_his(f"OBS-{i:03d}", ch_id=i))
    index.lookup("OBS-000")
    index.lookup("OBS-000")
    s = index.stats()
    d = s.to_dict()
    for field in (
        "build_ms", "records", "resident_keys", "total_keys", "conflict_keys",
        "shards", "spill_events", "spilled_keys", "spill_path",
        "cache_hits", "cache_misses", "cache_hit_rate",
        "resident_bytes", "budget_bytes",
    ):
        assert field in d, f"统计缺 {field}"
    assert d["build_ms"] >= 0
    assert s.budget_bytes == index.budget_bytes
    assert "spill" in s.render() and "命中率" in s.render()
    index.cleanup()


def test_budget_read_from_env_with_explicit_rejects():
    assert index_budget_bytes_from_env({}) == int(DEFAULT_INDEX_BUDGET_MB * 1024 * 1024)
    assert index_budget_bytes_from_env({INDEX_BUDGET_ENV: "8"}) == 8 * 1024 * 1024
    # 非法值**显式报错**，不静默回落默认 —— 否则用户以为放开到无限
    for bad in ("0", "-1", "64MB", "abc", "999999"):
        with pytest.raises(ReconqError) as exc:
            index_budget_bytes_from_env({INDEX_BUDGET_ENV: bad})
        assert exc.value.code == "INDEX_BUDGET_INVALID"
        assert INDEX_BUDGET_ENV in exc.value.render() or bad in exc.value.render()


def test_defaults_are_documented_constants():
    assert DEFAULT_INDEX_BUDGET_MB == 64.0
    assert DEFAULT_SPILL_ENTRY_THRESHOLD == 200_000
    assert DEFAULT_INDEX_CACHE_SIZE == 4096
    assert INDEX_BUDGET_ENV == "RECONQ_INDEX_BUDGET_MB"
    assert INDEX_SPILL_THRESHOLD_ENV == "RECONQ_INDEX_SPILL_ENTRIES"
    assert INDEX_CACHE_SIZE_ENV == "RECONQ_INDEX_CACHE_SIZE"


# —— 真实规模 ——


def test_six_channels_scale_stays_within_budget():
    """六路各 2000 行建索引：内存有界、查询可用、spill 计数真实。"""
    spill_dir = tempfile.mkdtemp(prefix="reconq-test-scale-")
    total = 6 * 2000
    index = InvertedIndex(PERIOD, budget_bytes=512 * 1024, spill_dir=spill_dir)
    started = time.perf_counter()
    for i in range(total):
        # 每 3 笔共用一个单号 → 三成键是冲突键，接近真实的一码付拆笔密度
        index.add(_his(f"SCALE-{i // 3:06d}", f"{50 + (i % 500) * 0.5:.2f}", ch_id=i))
    elapsed = time.perf_counter() - started

    stats = index.stats()
    assert stats.records == total
    assert stats.resident_bytes <= stats.budget_bytes * 1.05  # 触发后回落到安全水位
    assert stats.spill_events >= 1
    assert stats.total_keys == total // 3
    assert elapsed < 30.0, f"建 12000 行索引耗时 {elapsed:.1f}s，过慢"
    # 每个键 3 条候选都要能查回来（含已 spill 的）
    for i in (0, 999, 5000, 11997):
        hits = index.lookup(f"SCALE-{i // 3:06d}")
        assert len(hits) == 3
    assert stats.build_ms > 0
    index.cleanup()
