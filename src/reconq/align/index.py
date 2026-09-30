"""对齐倒排索引与账期分片（Task 22 · 核心模块的规模加深）。

这一层解决的是「**六路各两三千行一起对齐时，内存怎么办**」。

为什么不能直接拿 Task 21 的 ``dict[str, list[AlignRecord]]`` 上阵
---------------------------------------------------------------
六路账单 + HIS 流水在真实医院里是**几十万到上百万行**的量级（一个住院
预交金拆到费用项目就是好几行、一个账期跨两个收费窗口）。单号又**不唯一**
（一码付拆笔、合并支付、跨账期落点、退费与原单并存，见
:mod:`reconq.align.ambiguity`），倒排表的每个键平均挂着 1.5–3 条候选。
全量驻留的代价是：``dict`` 本身的开销 + 键字符串 + 每条候选的引用，
在百万行下轻松吃掉几个 GB。财务科点一次扫描就把信息科的面板服务打死，
这在院内是不可接受的。

四条设计，缺一条这个索引就会退化成「跑得动但没用」
----------------------------------------------------

1. **键规范化**（:func:`normalize_key`）
   院内导出里同一个单号能有四五种写法：``" wx-abc123 "``、``"ＷＸ－ＡＢＣ１２３"``、
   ``"WX-ABC123"``。不归一就等于「明明是同一笔，查不到」。
   **保留原始值**用于展示 —— 台账上给财务科看的必须是他们系统里那个写法，
   不是我们归一后的样子（对不上时他们得靠原值回查）。

2. **账期分片**（:class:`ShardedIndex`）
   按 HIS 收费时间落在哪个账期时间窗把记录切进不同的片。**跨账期的记录
   同时进相邻两片并打 ``cross_period=True``**：它在自己的片里是正常收费，
   在相邻片里是「可能跨期落点」——两种视角都要留着，砍掉任一边都会把一种
   真实形态变成「查不到」。

3. **索引分块 spill**（:meth:`InvertedIndex.spill`）
   常驻条目超过阈值（默认 200k）时把**冷块**（先进先出、命中过的优先留在
   内存）写成 JSON 行落到临时文件，只保留 ``key -> (offset, length)``。
   查询按 offset **回查**，不把整表读回来。
   **不动记录载荷**：见下方「内存预算到底管什么」。

4. **可观测 + LRU**（:class:`IndexStats`）
   构建耗时、条目数、冲突键数（同键多值）、分片数、spill 次数、LRU 命中率
   全部进 run summary。「内存打爆」这种事必须**能提前看见**，而不是等
   财务科说「昨天扫描卡死了」再回头猜。

内存预算到底管什么（一条必须写清楚的不变量）
------------------------------------------
``RECONQ_INDEX_BUDGET_MB``（默认 64MB）约束的是**倒排映射本身**
（键 → 候选引用、原始值表、冷队列），**不是** ``AlignRecord`` 载荷。
理由：记录载荷是调用方（Task 6 的流式取数、Task 21 的对齐器）本来就持有的
对象，它的内存由那边的批预算（``STREAM_FETCH_BUDGET_BYTES = 8MB``）管。
把两笔账混在一起会出现最坏的一种情况：spill 把倒排表清空了，弱匹配要遍历
全集的金额却少了载荷 —— **弱匹配会静默地少找候选**，而对齐器判「找不到」
就直接落成单边账。财务科看到的是「渠道有 HIS 无」，实际那笔钱在库里。

所以本模块宁可让倒排表 spill，也**不让** ``candidates_by_amount`` 看到
不全的数据：载荷全集由 :attr:`InvertedIndex.records` 独立持有并**永不驱逐**，
LRU 缓存与 spill 只作用在倒排表上。

源产品对照
----------
参考实现（``soda-core``）的流式语义是**取数侧**的：
``STREAM_FETCH_BUDGET_BYTES = 8MB`` / ``STREAM_FETCH_MAX_BATCH_ROWS = 1000`` /
``STREAM_FETCH_GROWTH_FACTOR = 4``，逐行 ``yield``、批大小随已见最宽行自适应
（见 ``sources/his_db.py``）。它**没有索引层**，也就没有「取回来了放哪」的问题。
本任务是把同一套「有界内存 + 自适应」纪律**延伸到索引侧**：源侧管取数批，
本侧管倒排表，两边用同一组常量做思想锚点（默认预算 64MB / 条目阈值 200k /
LRU 4096 条），并沿用源侧的「超限显式报告」而非静默降级。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Iterator, Optional, Sequence

from ..errors import ReconqError
from ..sources.timezone import PeriodWindow
from .matcher import AlignRecord

__all__ = [
    "DEFAULT_INDEX_BUDGET_MB",
    "DEFAULT_SPILL_ENTRY_THRESHOLD",
    "DEFAULT_INDEX_CACHE_SIZE",
    "INDEX_BUDGET_ENV",
    "INDEX_SPILL_THRESHOLD_ENV",
    "INDEX_CACHE_SIZE_ENV",
    "ShardHit",
    "IndexStats",
    "InvertedIndex",
    "ShardedIndex",
    "normalize_key",
    "period_of",
    "build_sharded_index",
    "index_budget_bytes_from_env",
]

logger = logging.getLogger("reconq.align.index")

#: 常驻倒排表的内存预算（MB）。默认 64MB：够放约 30 万条键+候选引用，
#: 又不至于让一台 8G 的应用服务器在六路一起对齐时被索引吃满。
DEFAULT_INDEX_BUDGET_MB: float = 64.0

#: 常驻条目数硬阈值。键数量与字节预算**双阈值**、先到先触发 ——
#: 单号特别长（院内有些系统用 32 位流水号 + 前缀）时，光键就能把预算吃光。
DEFAULT_SPILL_ENTRY_THRESHOLD: int = 200_000

#: LRU 缓存条目数（spill 之后回查结果也进这个缓存）。
DEFAULT_INDEX_CACHE_SIZE: int = 4096

INDEX_BUDGET_ENV = "RECONQ_INDEX_BUDGET_MB"
INDEX_SPILL_THRESHOLD_ENV = "RECONQ_INDEX_SPILL_ENTRIES"
INDEX_CACHE_SIZE_ENV = "RECONQ_INDEX_CACHE_SIZE"

#: 单条倒排条目的字节估算开销（dict 槽 + list 头 + 键字符串对象头，
#: 约按「键 UTF-8 长度 × 2（Python str 内部表示）+ 固定结构开销」估）。
_ENTRY_OVERHEAD_BYTES = 96
_KEY_CHAR_BYTES = 2
_REF_BYTES = 8

#: 非法预算值的上界。超过 16GB 的索引预算一定是配错了（少写小数点、
#: 或把字节数填进了 MB 位），继续跑等于没有预算。
MAX_INDEX_BUDGET_MB = 16 * 1024


def normalize_key(value: Any) -> str:
    """把单号归一成索引键。

    三步，顺序不能换：

    1. ``str`` + ``strip``：去首尾空白。院内导出的 CSV 常带尾随空格。
    2. **NFKC** 归一：全角 → 半角。这一步顺手把 ``ＷＸ－ＡＢＣ１２３``
       变成 ``WX-ABC123``，也把兼容的 ``①`` 之类折叠掉。不做这一步，
       同一笔单号会以两个键各存一份，其中一份永远查不到。
    3. ``casefold``：大小写归一。**不用 ``lower()``**：``lower()`` 对
       德文 ``ß`` 之类的字符折叠规则与 ``casefold`` 不同，键归一规则
       在不同字符集上分叉，后面就没法解释「为什么这条没命中」。
       医院场景里单号只用 ASCII，但键归一逻辑不该依赖这一点。

    归一后为空串返回 ``""``，调用方据此拒绝建键（空单号不该在倒排表里
    占一个键位 —— 它对任何查询都只会返回「无意义的全体」）。
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = unicodedata.normalize("NFKC", text).strip()
    if not text:
        return ""
    return text.casefold()


def period_of(moment: datetime, window: PeriodWindow) -> str:
    """该时刻归属的 ISO 周账期（如 ``2026-W36``）。

    按**账期时区**计算日历日与周次：渠道账单给的是本地时间、HIS 给的是库
    时区，直接对 naive 时刻取 ``isocalendar()`` 会让「周一凌晨 00:30 的
    那笔」被算到上一周去 —— 而半开窗口的判据（:meth:`PeriodWindow.contains`）
    说的是它属于新的一期。两处口径分叉，跨账期形态就会被误报成正常收费。
    """
    localized = window.localize(moment)
    iso = localized.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


@dataclass(frozen=True)
class ShardHit:
    """一次索引命中：记录 + 它所属分片 + 是否跨账期标。"""

    record: AlignRecord
    #: 该记录被放进这个分片时所依据的账期
    shard: str
    #: 该记录相对**目标账期**是跨期落点
    cross_period: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "shard": self.shard,
            "cross_period": self.cross_period,
            "record": self.record.to_dict(),
        }


@dataclass
class IndexStats:
    """索引构建与运行的可观测指标（进 run summary）。"""

    period: str = ""
    #: 记录总数（**永不驱逐**的那一份，弱匹配遍历的就是它）
    records: int = 0
    #: 倒排表当前常驻的键数
    resident_keys: int = 0
    #: 常驻条目（键 → 候选引用）数
    resident_entries: int = 0
    #: 不同键数（含已 spill 的）
    total_keys: int = 0
    #: 候选引用总数（含已 spill 的）
    total_entries: int = 0
    #: 冲突键数：同一个键挂着 ≥2 条候选 —— 歧义的**总量**口径，
    #: 面板据此显示「本期有多少笔可能要人工判」
    conflict_keys: int = 0
    #: 分片数
    shards: int = 0
    #: 被标 cross_period 的记录数
    cross_period_entries: int = 0
    #: spill 触发次数与被落盘的键数
    spill_events: int = 0
    spilled_keys: int = 0
    #: 落盘位置（无 spill 时为空串）
    spill_path: str = ""
    #: LRU 命中/未命中
    cache_hits: int = 0
    cache_misses: int = 0
    #: 常驻内存估算与预算
    resident_bytes: int = 0
    budget_bytes: int = 0
    #: 构建耗时（毫秒）
    build_ms: float = 0.0

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return 0.0 if total == 0 else self.cache_hits / total

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "records": self.records,
            "resident_keys": self.resident_keys,
            "resident_entries": self.resident_entries,
            "total_keys": self.total_keys,
            "total_entries": self.total_entries,
            "conflict_keys": self.conflict_keys,
            "shards": self.shards,
            "cross_period_entries": self.cross_period_entries,
            "spill_events": self.spill_events,
            "spilled_keys": self.spilled_keys,
            "spill_path": self.spill_path,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "resident_bytes": self.resident_bytes,
            "resident_kb": round(self.resident_bytes / 1024, 1),
            "budget_bytes": self.budget_bytes,
            "budget_kb": round(self.budget_bytes / 1024, 1),
            "build_ms": round(self.build_ms, 3),
        }

    def render(self) -> str:
        return (
            f"索引：{self.records} 条记录 / {self.total_keys} 个键 / "
            f"冲突键 {self.conflict_keys} / 分片 {self.shards} / "
            f"常驻 {self.resident_keys} 键（{round(self.resident_bytes / 1024)}KB，"
            f"预算 {round(self.budget_bytes / 1024)}KB） / "
            f"spill {self.spill_events} 次（{self.spilled_keys} 键） / "
            f"LRU 命中率 {self.cache_hit_rate * 100:.1f}% / "
            f"构建 {self.build_ms:.1f}ms"
        )


@dataclass(frozen=True)
class _SpillRef:
    """落盘条目的位置：``key -> (offset, length)``。"""

    offset: int
    length: int


def _record_to_json(record: AlignRecord) -> dict[str, Any]:
    return {
        "channel": record.channel,
        "period": record.period,
        "order_no": record.order_no,
        "amount": str(record.amount),
        "refund": str(record.refund) if record.refund is not None else None,
        "trade_time": record.trade_time.isoformat() if record.trade_time else None,
        "trade_type": record.trade_type,
        "department": record.department,
        "settle_type": record.settle_type,
        "his_order_no": record.his_order_no,
        "source_file": record.source_file,
        "source_row_no": record.source_row_no,
        "ch_id": record.ch_id,
        "bill_date": record.bill_date,
    }


def _record_from_json(payload: dict[str, Any]) -> AlignRecord:
    trade_time: Optional[datetime] = None
    raw = payload.get("trade_time")
    if raw:
        try:
            trade_time = datetime.fromisoformat(str(raw))
        except ValueError:
            # 落盘的是本模块自己序列化的 ISO 串，正常不会解不开；解不开时
            # 留空而不是抛错 —— 少一个时间戳不该让整条候选查不出来。
            logger.warning("spill 回查时无法解析 trade_time=%r（单号 %s）", raw, payload.get("order_no"))
    refund_raw = payload.get("refund")
    return AlignRecord(
        channel=str(payload.get("channel", "")),
        period=str(payload.get("period", "")),
        order_no=str(payload.get("order_no", "")),
        amount=Decimal(str(payload.get("amount", "0"))),
        refund=Decimal(str(refund_raw)) if refund_raw is not None else None,
        trade_time=trade_time,
        trade_type=str(payload.get("trade_type", "")),
        department=str(payload.get("department", "")),
        settle_type=str(payload.get("settle_type", "")),
        his_order_no=str(payload.get("his_order_no", "")),
        source_file=str(payload.get("source_file", "")),
        source_row_no=int(payload.get("source_row_no") or 0),
        ch_id=int(payload.get("ch_id") or 0),
        bill_date=str(payload.get("bill_date", "")),
    )


def index_budget_bytes_from_env(env: Optional[dict[str, str]] = None) -> int:
    """从环境变量读索引内存预算（字节）。

    非法值**显式报错**而不是回落默认：预算被配成 ``64MB``（带单位）或
    ``0`` 时静默回落 64MB，看起来一切正常，实际用户以为自己放开到无限了。
    """
    src = env if env is not None else os.environ
    raw = (src.get(INDEX_BUDGET_ENV) or "").strip()
    if not raw:
        return int(DEFAULT_INDEX_BUDGET_MB * 1024 * 1024)
    try:
        mb = float(raw)
    except ValueError as exc:
        raise ReconqError(
            "INDEX_BUDGET_INVALID",
            f"{INDEX_BUDGET_ENV}={raw!r} 不是数字（只填数字，单位是 MB）",
        ) from exc
    if mb <= 0:
        raise ReconqError(
            "INDEX_BUDGET_INVALID",
            f"{INDEX_BUDGET_ENV}={raw!r} 非法：预算必须为正；"
            f"填 0 相当于没有上限，正是本索引要防的情况",
        )
    if mb > MAX_INDEX_BUDGET_MB:
        raise ReconqError(
            "INDEX_BUDGET_INVALID",
            f"{INDEX_BUDGET_ENV}={raw!r} 过大：上限 {MAX_INDEX_BUDGET_MB}MB；"
            f"请确认是否把字节数填进了 MB 位",
        )
    return int(mb * 1024 * 1024)


def _int_from_env(name: str, default: int, env: Optional[dict[str, str]] = None) -> int:
    src = env if env is not None else os.environ
    raw = (src.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReconqError("INDEX_BUDGET_INVALID", f"{name}={raw!r} 不是整数") from exc
    if value <= 0:
        raise ReconqError("INDEX_BUDGET_INVALID", f"{name}={raw!r} 必须为正整数")
    return value


class InvertedIndex:
    """单号 → 候选记录列表的倒排索引（可 spill、内存有界）。

    与 Task 21 ``MatchIndex`` 的差别集中在三处：键归一、spill、统计。
    记录全集 :attr:`records` **永不驱逐** —— 见模块头「内存预算到底管什么」。
    """

    def __init__(
        self,
        period: str = "",
        *,
        budget_bytes: Optional[int] = None,
        spill_threshold: Optional[int] = None,
        cache_size: Optional[int] = None,
        spill_dir: Optional[str] = None,
    ) -> None:
        self.period = period
        self.budget_bytes = budget_bytes if budget_bytes is not None else index_budget_bytes_from_env()
        self.spill_threshold = (
            spill_threshold
            if spill_threshold is not None
            else _int_from_env(INDEX_SPILL_THRESHOLD_ENV, DEFAULT_SPILL_ENTRY_THRESHOLD)
        )
        self.cache_size = (
            cache_size if cache_size is not None else _int_from_env(INDEX_CACHE_SIZE_ENV, DEFAULT_INDEX_CACHE_SIZE)
        )

        #: 记录全集。弱匹配（按金额 + 时间窗找候选）遍历的就是它。
        self.records: list[AlignRecord] = []
        #: 常驻倒排表：归一键 → 候选列表
        self._by_no: dict[str, list[AlignRecord]] = {}
        #: 归一键 → 展示用原始值（财务科在自己系统里看到的那份写法）
        self._original: dict[str, str] = {}
        #: 落盘条目的位置
        self._spilled: dict[str, _SpillRef] = {}
        #: 冷块候选队列（先进先出）
        self._cold: list[str] = []
        self._resident_bytes = 0
        self._spill_events = 0
        self._spill_handle = None
        self._spill_reader = None
        self._spill_dir = spill_dir
        self._spill_path = ""
        self._cache: "OrderedDict[str, list[AlignRecord]]" = OrderedDict()
        self._cache_hits = 0
        self._cache_misses = 0
        self._total_keys = 0
        self._total_entries = 0
        self._conflict_keys = 0
        self._build_started = time.perf_counter()

    # —— 建索引 ——

    @staticmethod
    def _keys_of(record: AlignRecord) -> list[tuple[str, str]]:
        """该记录可被哪些单号命中，返回 ``(归一键, 原始值)``。

        HIS 侧自己的 ``channel_order_no`` 恒等于 ``his_order_no``（见
        ``store.bill_store.CHANNEL_COLUMN_MAP``），但院内导出偶尔只给其中
        一个；两个都收，归一后去重。

        **原始值跟着键一起返回**：归一后的键只用于查找，台账上要给财务科
        看他们系统里那个写法（「ＷＸ－ＡＢＣ」还是「WX-ABC」），对不上时
        他们得靠原值回查。
        """
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw in (record.order_no, record.his_order_no):
            key = normalize_key(raw)
            if key and key not in seen:
                seen.add(key)
                out.append((key, str(raw).strip()))
        return out

    def add(self, record: AlignRecord) -> None:
        """把一条 HIS 侧记录并入索引。"""
        self.records.append(record)
        for key, original in self._keys_of(record):
            self._add_key(key, original, record)
        self._maybe_spill()

    def add_all(self, records: Iterable[AlignRecord]) -> None:
        """批量并入（流式：逐条落键，最后统一判定是否 spill）。"""
        for record in records:
            self.records.append(record)
            for key, original in self._keys_of(record):
                self._add_key(key, original, record)
        self._maybe_spill()

    def _add_key(self, key: str, original: str, record: AlignRecord) -> None:
        bucket = self._by_no.get(key)
        if bucket is None and key in self._spilled:
            # 该键之前被 spill 掉了。先把落盘的那份读回内存再追加，
            # 否则这份记录会被追加到一个**只存在于文件里**的键上，
            # 而内存里的倒排表看不到它 —— 命中与否全看查询是否恰好走了
            # 回查分支，语义变得不可预测。
            bucket = self._reload_key(key)
        is_new_key = bucket is None
        if is_new_key:
            bucket = []
            self._by_no[key] = bucket
            self._total_keys += 1
            self._cold.append(key)
            self._resident_bytes += _KEY_CHAR_BYTES * len(key) + _ENTRY_OVERHEAD_BYTES
        if original and key not in self._original:
            self._original[key] = original
        bucket.append(record)
        self._total_entries += 1
        self._resident_bytes += _REF_BYTES
        if not is_new_key and len(bucket) == 2:
            # 刚从单值变成多值 —— 这就是一个冲突键（歧义的总量口径）
            self._conflict_keys += 1
        # **刻意不在这里给键「加热」**：同一个单号在一次批量建索引里被写
        # 2–3 次（一码付拆笔）是常态，拿重复写入当「热」信号，等于每个键
        # 刚建好就被摘出冷队列 → 冷队列永远为空 → spill 永不触发 → 预算
        # 形同虚设。热度只由**查询**产生（见 :meth:`lookup` 的 `_mark_hot`）。
        self._cache.pop(key, None)

    def _mark_hot(self, key: str) -> None:
        """把键移出冷队列（被查到过 = 值得留在内存里）。"""
        if key in self._cold:
            self._cold.remove(key)

    def conflict_keys(self) -> int:
        """当前常驻表里挂着 ≥2 条候选的键数（spill 后按落盘部分另计）。"""
        resident = sum(1 for bucket in self._by_no.values() if len(bucket) > 1)
        spilled = sum(
            1
            for key in self._spilled
            if len(self._read_spilled(key)) > 1
        )
        return max(self._conflict_keys, resident + spilled)

    def original_value(self, order_no: str) -> str:
        """取该键对应的展示原值；没建过键时回落入参本身。"""
        key = normalize_key(order_no)
        if not key:
            return ""
        return self._original.get(key, order_no)

    # —— spill ——

    def _open_spill_file(self) -> Any:
        """打开落盘文件，**读写两个句柄**。

        刻意不共用一个句柄：spill 是「顺序追加 + 随机回查」并行的，
        共用一个 ``w+b`` 时回查的 ``seek()`` 会把写指针一起挪走，
        下一条追加就从**回查位置上**覆写下去，把刚落盘的那条记录截断 ——
        查出来的 JSON 缺尾、当场报 JSONDecodeError，而被截断的往往正是
        财务科最要看的那条跨账期记录。两个句柄各走各的指针，互不干扰。
        """
        if self._spill_handle is not None:
            return self._spill_handle
        directory = self._spill_dir or tempfile.mkdtemp(prefix="reconq-index-")
        self._spill_dir = directory
        os.makedirs(directory, exist_ok=True)
        self._spill_path = os.path.join(directory, f"index-{os.getpid()}.jsonl")
        self._spill_handle = open(self._spill_path, "w+b")
        self._spill_reader = open(self._spill_path, "rb")
        return self._spill_handle

    def _maybe_spill(self) -> None:
        """双阈值触发：常驻键数超 ``spill_threshold`` **或**估算字节超预算。"""
        if not self._cold:
            return
        if len(self._by_no) <= self.spill_threshold and self._resident_bytes <= self.budget_bytes:
            return
        # 每次只腾出超出的部分到安全水位，不一次全倒 —— 全倒等于把索引变成
        # 一个顺序文件，后续每条查询都要落盘，延迟会从毫秒级掉到百毫秒级。
        target_keys = max(1, int(self.spill_threshold * 0.8))
        target_bytes = int(self.budget_bytes * 0.8)
        moved = 0
        while self._cold and (
            len(self._by_no) > target_keys or (self._resident_bytes > target_bytes and moved < 100_000)
        ):
            key = self._cold.pop(0)
            if key in self._by_no:
                self._spill_key(key)
                moved += 1
        if moved:
            self._spill_events += 1
            logger.info(
                "索引 spill：已落盘 %s 键 → %s，常驻 %s 键 / %sKB，预算 %sKB",
                len(self._spilled),
                self._spill_path,
                len(self._by_no),
                round(self._resident_bytes / 1024),
                round(self.budget_bytes / 1024),
            )

    def _spill_key(self, key: str) -> None:
        records = self._by_no.pop(key, None)
        if not records:
            return
        handle = self._open_spill_file()
        line = json.dumps(
            {
                "key": key,
                "original": self._original.get(key, key),
                "records": [_record_to_json(r) for r in records],
            },
            ensure_ascii=False,
        ).encode("utf-8") + b"\n"
        offset = handle.tell()
        handle.write(line)
        handle.flush()
        self._spilled[key] = _SpillRef(offset=offset, length=len(line))
        self._resident_bytes -= _KEY_CHAR_BYTES * len(key) + _ENTRY_OVERHEAD_BYTES + _REF_BYTES * len(records)

    def _read_spilled(self, key: str) -> list[AlignRecord]:
        """只读取已落盘键的候选，**不**搬回常驻表。"""
        ref = self._spilled.get(key)
        if ref is None:
            return []
        self._open_spill_file()  # 确保两个句柄都已建立
        reader = self._spill_reader
        reader.seek(ref.offset)
        raw = reader.read(ref.length)
        payload = json.loads(raw.decode("utf-8"))
        return [_record_from_json(item) for item in payload.get("records", [])]

    def _reload_key(self, key: str) -> list[AlignRecord]:
        """把已落盘的键读回常驻表（查询命中 spill 键时走这里）。"""
        records = self._read_spilled(key)
        if not records:
            return []
        self._by_no[key] = records
        self._spilled.pop(key, None)
        self._resident_bytes += _KEY_CHAR_BYTES * len(key) + _ENTRY_OVERHEAD_BYTES + _REF_BYTES * len(records)
        # 读回即写热（它刚被查到过），别让它又立刻被冷掉
        self._mark_hot(key)
        return records

    # —— 查询 ——

    def lookup(self, order_no: str) -> list[AlignRecord]:
        """按单号取候选；空单号返回空列表（不猜）。

        命中口径与 Task 21 一致，但键先过 :func:`normalize_key`。
        """
        key = normalize_key(order_no)
        if not key:
            return []
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self._cache_hits += 1
            return list(cached)
        self._cache_misses += 1
        bucket = self._by_no.get(key)
        if bucket is None and key in self._spilled:
            bucket = self._reload_key(key)
        result = list(bucket) if bucket else []
        # 查得到就值得留在内存里（LRU 冷键的依据只此一条）
        self._mark_hot(key)
        self._store_cache(key, result)
        return result

    def _store_cache(self, key: str, value: list[AlignRecord]) -> None:
        if self.cache_size <= 0:
            return
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def hits(self, order_no: str) -> list[ShardHit]:
        """带分片与跨期标记的命中（本片视角）。"""
        return [
            ShardHit(record=r, shard=self.period, cross_period=False)
            for r in self.lookup(order_no)
        ]

    def candidates_by_amount(
        self,
        *,
        amount: Decimal,
        tolerance: Decimal,
        window: Optional[PeriodWindow] = None,
    ) -> list[AlignRecord]:
        """弱匹配候选：金额落在容差内 **且** 时间落在账期窗内。

        遍历 :attr:`records`（**不**遍历倒排表）：spill 之后倒排表不全，
        拿它当全集会让弱匹配静默少找候选、把「找不到」直接变成单边账。
        """
        out: list[AlignRecord] = []
        for record in self.records:
            if abs(record.net_amount - amount) > tolerance:
                continue
            if window is not None:
                if record.trade_time is None or not window.contains(record.trade_time):
                    continue
            out.append(record)
        return out

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[AlignRecord]:
        return iter(self.records)

    @property
    def all(self) -> list[AlignRecord]:
        """记录全集（与 Task 21 ``MatchIndex.all`` 同名同义）。"""
        return list(self.records)

    def close(self) -> None:
        """关闭 spill 文件句柄（不删目录，留给排障回查）。"""
        if self._spill_handle is not None:
            self._spill_handle.close()
            self._spill_handle = None
        if self._spill_reader is not None:
            self._spill_reader.close()
            self._spill_reader = None

    def cleanup(self) -> None:
        """关闭并删除 spill 临时目录（发布前清理与测试隔离用）。"""
        self.close()
        if self._spill_dir and os.path.isdir(self._spill_dir):
            shutil.rmtree(self._spill_dir, ignore_errors=True)
        self._spill_dir = ""
        self._spill_path = ""

    def stats(self) -> IndexStats:
        return IndexStats(
            period=self.period,
            records=len(self.records),
            resident_keys=len(self._by_no),
            resident_entries=sum(len(v) for v in self._by_no.values()),
            total_keys=self._total_keys,
            total_entries=self._total_entries,
            conflict_keys=self.conflict_keys(),
            shards=1,
            spill_events=self._spill_events,
            spilled_keys=len(self._spilled),
            spill_path=self._spill_path,
            cache_hits=self._cache_hits,
            cache_misses=self._cache_misses,
            resident_bytes=self._resident_bytes,
            budget_bytes=self.budget_bytes,
            build_ms=(time.perf_counter() - self._build_started) * 1000.0,
        )


class ShardedIndex:
    """按账期分片的倒排索引；跨账期记录同时进相邻两片。

    对齐器（Task 21）只需要 ``lookup`` / ``candidates_by_amount`` /
    ``records`` / ``period`` 这几个成员，因此本类与 :class:`InvertedIndex`
    是**可互换**的：把 ``build_index`` 的产物换成分片索引，对齐逻辑一行不改。
    """

    def __init__(
        self,
        period: str,
        window: PeriodWindow,
        *,
        budget_bytes: Optional[int] = None,
        spill_threshold: Optional[int] = None,
        cache_size: Optional[int] = None,
        spill_dir: Optional[str] = None,
    ) -> None:
        self.period = period
        self.window = window
        self._options = {
            "budget_bytes": budget_bytes,
            "spill_threshold": spill_threshold,
            "cache_size": cache_size,
            "spill_dir": spill_dir,
        }
        #: 账期 → 该片自己的倒排表
        self._shards: dict[str, InvertedIndex] = {}
        #: 记录全集（跨片去重，按加入顺序）
        self.records: list[AlignRecord] = []
        self._cross_period_entries = 0
        #: 跨账期记录的标识集合。**不能从「在哪个分片」反推**：
        #: 一条属于 W35 的记录进了 W36 片（跨期视角），但它在 W35 片里
        #: 是正常收费。同一个对象在两片里 cross_period 取值**相反**，
        #: 只有在 add 时把真相记下来才分得清。第一版按「分片 ≠ 本期」
        #: 一刀切，恰好把这两个标记**同时取反**了。
        self._cross_ids: set[int] = set()
        self._build_started = time.perf_counter()

    # —— 分片 ——

    def shard(self, period: str) -> InvertedIndex:
        """取（或建）某个账期的分片。"""
        if period not in self._shards:
            self._shards[period] = InvertedIndex(period, **self._options)
        return self._shards[period]

    def add(self, record: AlignRecord) -> None:
        """把一条 HIS 侧记录按收费时间落片。

        落在本账期窗口内 → 只进本片。
        落在窗口外 → 进**它真正所属的账期片**（那边它是正常收费），
        **同时**进本片并打 ``cross_period=True``（这边它是跨期落点候选）。
        两种视角都要留着：只留本片就查不到「这笔其实属于下一期」，
        只留邻片则本账期对齐会凭空少一笔，两边都显绿、差额凭空消失。

        同一个 ``record`` 对象在本索引内**只入一次全集**；它会出现在两个
        分片的倒排表里，这是**有意的**（两种视角），不是重复入库。
        """
        self.records.append(record)
        own_period = period_of(record.trade_time, self.window) if record.trade_time else ""
        if not own_period:
            # 没有收费时间的记录：只进本片，标成跨期不可判（而不是猜一个账期）
            self.shard(self.period).add(record)
            return
        self.shard(own_period).add(record)
        if own_period != self.period:
            self.shard(self.period).add(record)
            self._cross_period_entries += 1
            self._cross_ids.add(id(record))

    def add_all(self, records: Iterable[AlignRecord]) -> None:
        for record in records:
            self.add(record)

    # —— 查询 ——

    def lookup(self, order_no: str) -> list[AlignRecord]:
        """本片视角的命中（与 Task 21 ``MatchIndex.lookup`` 同签名）。"""
        return self.shard(self.period).lookup(order_no)

    def hits(self, order_no: str) -> list[ShardHit]:
        """本片 + 相邻片，带 ``cross_period`` 标记。

        邻片的候选**照样返回**：是否认领由对齐器的跨账期判定决定，
        索引只负责「把可能的候选都摆出来」。

        **同一 ``record`` 对象会在两个分片各出一条命中**，这是**有意的**：
        它是同一笔收费，但「在本期看」和「在它真正所属的那期看」是两种
        不同的判断依据，按对象去重会把其中一个视角悄悄吃掉。
        去重只在**同一分片内**做（防同一条记录被重复登记进同一片）。

        ``cross_period`` 取自 :attr:`_cross_ids`（add 时记下的真相），
        **不是**「所在分片 ≠ 本期」：同一条跨账期记录在本期片里是跨期落点、
        在它自己的片里是正常收费，两个标记是相反的。
        """
        out: list[ShardHit] = []
        for shard_period, shard in self._shards.items():
            seen: set[int] = set()
            for record in shard.lookup(order_no):
                if id(record) in seen:
                    continue
                seen.add(id(record))
                cross = id(record) in self._cross_ids and shard_period == self.period
                out.append(ShardHit(record=record, shard=shard_period, cross_period=cross))
        return out

    def candidates_by_amount(
        self,
        *,
        amount: Decimal,
        tolerance: Decimal,
        window: Optional[PeriodWindow] = None,
    ) -> list[AlignRecord]:
        """弱匹配候选：遍历记录全集，不受分片与 spill 影响。"""
        effective = window if window is not None else self.window
        out: list[AlignRecord] = []
        for record in self.records:
            if abs(record.net_amount - amount) > tolerance:
                continue
            if effective is not None:
                if record.trade_time is None or not effective.contains(record.trade_time):
                    continue
            out.append(record)
        return out

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[AlignRecord]:
        return iter(self.records)

    @property
    def all(self) -> list[AlignRecord]:
        """记录全集（与 Task 21 ``MatchIndex.all`` 同名同义）。

        对齐器算「HIS 有渠道无」时遍历的就是它 —— **按对象去重**：
        跨账期记录在两个分片的倒排表里各有一份，但它是**同一条收费**，
        报两次就成了同一笔钱开两条单边账。
        """
        seen: set[int] = set()
        out: list[AlignRecord] = []
        for record in self.records:
            if id(record) in seen:
                continue
            seen.add(id(record))
            out.append(record)
        return out

    def close(self) -> None:
        for shard in self._shards.values():
            shard.close()

    def cleanup(self) -> None:
        for shard in self._shards.values():
            shard.cleanup()

    @property
    def shard_periods(self) -> list[str]:
        return sorted(self._shards)

    def stats(self) -> IndexStats:
        shards = list(self._shards.values())
        base = shards[0].stats() if shards else IndexStats(period=self.period)
        return IndexStats(
            period=self.period,
            records=len(self.records),
            resident_keys=sum(len(s._by_no) for s in shards),
            resident_entries=sum(sum(len(v) for v in s._by_no.values()) for s in shards),
            total_keys=sum(s._total_keys for s in shards),
            total_entries=sum(s._total_entries for s in shards),
            conflict_keys=sum(s.conflict_keys() for s in shards),
            shards=len(shards),
            cross_period_entries=self._cross_period_entries,
            spill_events=sum(s._spill_events for s in shards),
            spilled_keys=sum(len(s._spilled) for s in shards),
            spill_path=next((s._spill_path for s in shards if s._spill_path), ""),
            cache_hits=sum(s._cache_hits for s in shards),
            cache_misses=sum(s._cache_misses for s in shards),
            resident_bytes=sum(s._resident_bytes for s in shards),
            budget_bytes=base.budget_bytes,
            build_ms=(time.perf_counter() - self._build_started) * 1000.0,
        )


def build_sharded_index(
    his_records: Sequence[AlignRecord],
    *,
    period: str,
    window: PeriodWindow,
    budget_bytes: Optional[int] = None,
    spill_threshold: Optional[int] = None,
    cache_size: Optional[int] = None,
    spill_dir: Optional[str] = None,
) -> ShardedIndex:
    """把 HIS 侧记录建成**按账期分片**的倒排索引。"""
    index = ShardedIndex(
        period,
        window,
        budget_bytes=budget_bytes,
        spill_threshold=spill_threshold,
        cache_size=cache_size,
        spill_dir=spill_dir,
    )
    index.add_all(his_records)
    return index
