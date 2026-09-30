"""链式审计事件的写入、按账期切片检索与校验（Task 45）。

前四层回答的是「**能验**」，这一层回答的是「**能查**」
--------------------------------------------------------
:mod:`reconq.engine.resume` 的 ``scan_events`` 表与
:mod:`reconq.audit.model_calls` 的 ``model_calls`` 表都已经能自证没被改过
（链式指纹 + append-only 触发器）。但财务科在面板上点开一条差异时，问的
不是「这一期有没有被改过」，而是**「这一条差异是怎么变成现在这样的」** ——
它什么时候被扫出来、模型给了什么草稿、谁在什么时候确认的、谁复核的、
哪一版结论单把它导出过。

没有这一层，这五个问题只能靠人把整库导出来按时间排序肉眼找。而**人靠肉眼
找审计痕迹，恰恰是审计最不能接受的做法**：查到一半累了、少看一条，纸面上
看不出来。

所以本模块把事件链从**库里的表**搬成**可归档、可交接、可按期取片的文件**
（``events.jsonl`` + 旁挂索引），并在上面做四件事：

============================  =============================================
机制                          回答的问题
============================  =============================================
:func:`append_event`          落一条事件；指纹把上一环算进去
:func:`query_events`          按账期 / 事件类型 / 差异键 / 时间窗切片读回
:func:`discrepancy_timeline`  一条差异从建账到导出的完整时序
:func:`verify_chain`          重算校验；篡改一条即指出**第几条**
============================  =============================================

四条不可违反的边界
--------------------

1. **链头固定，锚点不可改。** 账期开链时写下的第 1 条（:func:`open_chain`）
   的 ``chain_hash`` 是全链锚点，**此后无论追加多少条都不变**。它与链尾
   指纹是两件事：链尾覆盖「当前内容」，锚点覆盖「**这条链从哪儿开始**」。
   只看链尾的话，把前 5 条删掉再从第 6 条接着往后写，链尾能自洽 —— 而
   「这一期的事件链」已经换了一条。锚点由索引文件独立记着，重算时对不上
   即判篡改（:attr:`EventChainVerdict.anchor_ok`）。
2. **按期切片不许全量扫描。** 索引文件记录每个账期的事件在 ``events.jsonl``
   里的**字节区间**，取一期只读那一段。:attr:`EventSlice.bytes_scanned`
   把「这次实际读了多少字节」交出来，测试据此断言「读了一期、没有读全库」。
   索引记的是**区间列表**而不是「起点+终点」：两期并发扫描时事件会交错，
   记成一段就会把另一期的行吞进来或漏掉。
3. **未知事件类型告警但不丢。** 读到表外类型（换了版本的产品写的、新增的
   类型）时记 ``logger.warning`` 并把它收进
   :attr:`EventSlice.unknown_types`，**事件本身照常返回**。静默丢弃会让
   「这一期发生过什么」凭空少一段，而丢的人根本不知道丢了什么。
4. **审计链不落患者可识别明细。** 写盘前逐键逐值核对，命中即**抛错拒写**
   而不是静默替换 —— 审计链是 append-only 的，写进去就删不掉了；当场拒写
   还能让调用方改对，事后清理既做不到也说不清谁改的。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层，也没有等价物**。它的 ``contract
verify`` 把逐 check 的结论打到终端、``exit_with_code()`` 退出，进程一停
什么都没留下；``data-source test`` 是一次性连通性探针。源产品假定「跑
一次」，也就从不需要「把上次那一次捞出来看它当时说了什么」。

可复用的是同一条纪律的另一半：**摘要指纹当锚点**。源产品用
``ContractYaml`` 的 ``name/version`` 标识一份契约，跨版本比对靠 ``contract
diff`` 人工看；本产品把同一件事落在**事件链**上，并且让锚点**在链头就固定
下来** —— 源产品的 ``contract diff`` 是两份文件当场比，比完就散，没有
「这一份从第一行起就没被动过」这一层。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Mapping, Optional, Sequence

from ..errors import ReconqError

logger = logging.getLogger("reconq.audit")

__all__ = [
    "EVENT_INDEX_NAME",
    "EVENT_LOG_NAME",
    "EVENT_TYPES",
    "GENESIS_HASH",
    "INDEX_VERSION",
    "TIMELINE_STAGE_OF",
    "AuditEvent",
    "EventChainVerdict",
    "EventSlice",
    "append_event",
    "assert_event_deidentified",
    "chain_anchor",
    "discrepancy_timeline",
    "event_type_zh",
    "load_index",
    "open_chain",
    "query_events",
    "verify_chain",
]


# ─────────────────────────── 常量 ───────────────────────────


#: append-only 事件文件名（相对审计目录）。
EVENT_LOG_NAME = "events.jsonl"

#: 旁挂索引文件名。**与事件文件分开放**：索引记的是「哪一期在第几字节到
#: 第几字节」，它本身可重建（重扫一遍事件文件即可），因此不值得为它牺牲
#: 事件文件的可交接性 —— 交接时给人 ``events.jsonl`` 一个文件就够。
EVENT_INDEX_NAME = "events.index.json"

#: 索引格式版本。**不认识就当没有**（走全量扫描重建），而不是报错 ——
#: 索引是加速器不是事实来源，为它停摆不划算。
INDEX_VERSION = 1

#: 链首前驱指纹。与 :data:`reconq.engine.resume.GENESIS_HASH` 同值同义。
GENESIS_HASH = "0" * 64

#: 受控事件类型表。**中文名与阶段一起登记**，面板与导出共用同一份口径 ——
#: 两处各写一份 inevitably 会漂移，漂移之后「导出说已复核、面板说没复核」。
EVENT_TYPES: dict[str, str] = {
    "SCAN_STARTED": "开始扫描",
    "CHANNEL_FETCHED": "渠道取数完成",
    "CHECK_FAILED": "口径未通过",
    "DIFF_CREATED": "差异入账",
    "MODEL_CALLED": "调用模型取成因草稿",
    "DRAFT_REJECTED": "草稿被驳回",
    "DECIDED": "人工处置",
    "REVIEWED": "主管复核",
    "EXPORTED": "结论单导出",
    "CONTRACT_CHANGED": "口径指纹变更",
    "RUN_DEGRADED": "运行降级",
}

#: 事件类型 → 差异生命周期阶段。**只有这六类进时序主线**，其余是过程类
#: 事件（取数、口径、字典）—— 它们照样上链、照样可查，但它们不是「这条
#: 差异走到哪一步了」的答案。把它们也排进主线，时序图会被取数事件撑满，
#: 反而看不清处置进度。
TIMELINE_STAGE_OF: dict[str, str] = {
    "DIFF_CREATED": "创建",
    "MODEL_CALLED": "归因草稿",
    "DRAFT_REJECTED": "草稿被驳回",
    "DECIDED": "人工确认",
    "REVIEWED": "复核",
    "EXPORTED": "导出",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def event_type_zh(event_type: str) -> str:
    """事件类型的中文名。表外类型返回带标记的原文，**不返回空串** ——
    空串会让「未知类型」在界面上和「没填」长得一样。"""
    return EVENT_TYPES.get(event_type, f"（未登记类型 {event_type}）")


# ─────────────────────────── 事件 ───────────────────────────


@dataclass(frozen=True)
class AuditEvent:
    """事件链上的一环。"""

    seq: int
    period: str
    event_type: str
    at: str
    actor: str = ""
    discrepancy_key: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    chain_hash: str = ""

    @property
    def event_zh(self) -> str:
        return event_type_zh(self.event_type)

    @property
    def stage(self) -> str:
        """差异生命周期阶段；非主线事件返回空串。"""
        return TIMELINE_STAGE_OF.get(self.event_type, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "period": self.period,
            "event_type": self.event_type,
            "at": self.at,
            "actor": self.actor,
            "discrepancy_key": self.discrepancy_key,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "chain_hash": self.chain_hash,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "AuditEvent":
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ReconqError(
                "AUDIT_EVENT_CORRUPT",
                f"事件行不是合法 JSON：{raw[:80]}…（{exc}）。"
                "事件文件是 append-only 的，一行坏掉说明写入被中断过；"
                "请把该文件与索引一起交信息科，不要就地删行 —— "
                "删掉的那一环正是校验要指出来的",
            ) from exc
        missing = [k for k in ("seq", "period", "event_type", "chain_hash") if k not in data]
        if missing:
            raise ReconqError(
                "AUDIT_EVENT_CORRUPT",
                f"事件行缺字段 {missing}：{raw[:80]}…。字段缺失的事件无法参与"
                "指纹重算，本产品不会给它编一个默认值凑成一条「看起来完整」的链",
            )
        return cls(
            seq=int(data["seq"]),
            period=str(data["period"]),
            event_type=str(data["event_type"]),
            at=str(data.get("at", "")),
            actor=str(data.get("actor", "")),
            discrepancy_key=str(data.get("discrepancy_key", "")),
            payload=dict(data.get("payload") or {}),
            prev_hash=str(data.get("prev_hash", GENESIS_HASH)),
            chain_hash=str(data["chain_hash"]),
        )

    def _digest_fields(self) -> dict[str, Any]:
        """参与指纹的字段。**显式列出而不是 ``json.dumps(self.to_dict())``** ——
        后者会让「将来给事件加一个展示用字段」这件事**静默改掉全部历史指纹**，
        把一条没被动过的链报成篡改。加字段要么进这里（则是一次显式的口径
        变更，历史链按新口径重算会报红，这是应该报的），要么不进（历史链
        不受影响）。"""
        return {
            "seq": self.seq,
            "period": self.period,
            "event_type": self.event_type,
            "at": self.at,
            "actor": self.actor,
            "discrepancy_key": self.discrepancy_key,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }


def _digest(prev_hash: str, fields: Mapping[str, Any]) -> str:
    import hashlib

    blob = json.dumps(
        {"prev_hash": prev_hash, **fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _event_chain_hash(
    seq: int,
    period: str,
    event_type: str,
    at: str,
    actor: str,
    discrepancy_key: str,
    payload: Mapping[str, Any],
    prev_hash: str,
) -> str:
    probe = AuditEvent(
        seq=seq,
        period=period,
        event_type=event_type,
        at=at,
        actor=actor,
        discrepancy_key=discrepancy_key,
        payload=dict(payload),
        prev_hash=prev_hash,
    )
    return _digest(prev_hash, probe._digest_fields())


# ─────────────────────────── 脱敏 ───────────────────────────


def assert_event_deidentified(payload: Any, *, where: str = "事件 payload") -> None:
    """核对事件 payload 里没有患者可识别信息。命中即**抛错拒写**。

    为什么不是静默替换（:mod:`reconq.runlog` 的做法）：日志是滚动写的、
    一直在被人看，替换掉一处形态只损失可读性；而**审计链是 append-only
    的** —— 替换意味着链上留着一段「当时就是这么写的」与「实际不是」的
    记录，谁都改不掉也说不清。当场拒写还能让调用方改对。

    复用既有的两套表而不是另写：键名查
    :data:`reconq.align.snapshot.FORBIDDEN_PATIENT_KEYS`，形态查
    :func:`reconq.runlog.redact_text` 的模式（后者能抓住「已联系住院号
    110105… 的家属」这种嵌在整句话中间、整体又不像号码的形态）。
    """
    from ..align.snapshot import FORBIDDEN_PATIENT_KEYS
    from ..runlog import redact_text

    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).strip().lower() in FORBIDDEN_PATIENT_KEYS:
                raise ReconqError(
                    "AUDIT_EVENT_NOT_DEIDENTIFIED",
                    f"{where} 的字段 {key!r} 是患者可识别字段：审计链只带"
                    "金额、单号、账期、证据行号与处理人，不带患者明细",
                )
            assert_event_deidentified(value, where=f"{where}.{key}")
        return
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_event_deidentified(item, where=f"{where}[{index}]")
        return
    if isinstance(payload, str):
        _, hits = redact_text(payload)
        if hits:
            raise ReconqError(
                "AUDIT_EVENT_NOT_DEIDENTIFIED",
                f"{where} 的文本命中患者标识形态 {sorted(set(hits))}："
                "审计链要随结论单归档并可能交内审复核，患者信息不能随链流转",
            )


# ─────────────────────────── 索引 ───────────────────────────


def _audit_paths(log_dir: str) -> tuple[str, str]:
    base = str(log_dir or "").strip()
    if not base:
        raise ReconqError(
            "AUDIT_LOG_DIR_REQUIRED",
            "必须给审计目录（存 events.jsonl 与索引的位置）。"
            "本产品不默认往当前工作目录写审计文件 —— 换个目录跑就以为链在那儿，"
            "是最容易出的错",
        )
    return (os.path.join(base, EVENT_LOG_NAME), os.path.join(base, EVENT_INDEX_NAME))


def _empty_index() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "bytes": 0, "tail_hash": "", "periods": {}}


def load_index(log_dir: str) -> dict[str, Any]:
    """读旁挂索引；读不到或版本不认识就返回空索引（调用方走全量扫描）。

    **索引缺失不是错误。** 它是可重建的加速器：全量扫一遍事件文件就能重建
    （:func:`rebuild_index`）。为它报错等于「索引丢了 = 审计链没了」，
    而事实是链好好的 —— 只是取片慢了一点。
    """
    _log_path, index_path = _audit_paths(log_dir)
    try:
        with open(index_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return _empty_index()
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return _empty_index()
    data.setdefault("bytes", 0)
    data.setdefault("tail_hash", "")
    data.setdefault("periods", {})
    return data


def _write_index(index_path: str, index: Mapping[str, Any]) -> None:
    """原子写索引：先写临时文件再 ``os.replace``。

    半截的索引文件比没有索引更糟 —— 读它会抛 ``ValueError``，而那正是
    「重建」这条路的入口，看着像索引坏了其实只是没写完。
    """
    tmp = f"{index_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, index_path)


def _period_entry(index: dict[str, Any], period: str) -> dict[str, Any]:
    entry = index["periods"].get(period)
    if entry is None:
        entry = {"ranges": [], "count": 0, "first_seq": 0, "last_seq": 0, "anchor": ""}
        index["periods"][period] = entry
    entry.setdefault("ranges", [])
    entry.setdefault("count", 0)
    entry.setdefault("first_seq", 0)
    entry.setdefault("last_seq", 0)
    entry.setdefault("anchor", "")
    return entry


def _absorb(entry: dict[str, Any], seq: int, start: int, end: int) -> None:
    """把一段字节区间并进账期条目（相邻则合并）。"""
    ranges = entry["ranges"]
    if ranges and ranges[-1][1] == start:
        ranges[-1][1] = end
    else:
        ranges.append([start, end])
    entry["count"] += 1
    if not entry["first_seq"]:
        entry["first_seq"] = seq
    entry["last_seq"] = seq


def rebuild_index(log_dir: str) -> dict[str, Any]:
    """扫一遍事件文件重建索引（索引丢失 / 版本不认识 / 被截断时用）。

    **保留原索引里已登记的锚点。** 重建只补「区间、条数、序号」这些可从
    文件重新数出来的量；锚点**不能**跟着重数 —— 那正是「有人把开链那条删
    掉了」与「索引坏了」的分界。跟着重数的话，重建一次就把证据擦一次，
    校验会一路报「链完整」，而这一期的链其实已经换了一条。

    代价是**索引真丢了就再也起不回锚点**（:func:`chain_anchor` 返回空串）。
    那是可接受的：索引与 ``events.jsonl`` 一起归档，锚点另有结论单上的
    引用；真只剩一个文件时，能验链身、验不了链头，如实说验不了。
    """
    log_path, index_path = _audit_paths(log_dir)
    previous = load_index(log_dir)
    index = _empty_index()
    if not os.path.isfile(log_path):
        _write_index(index_path, index)
        return index
    index["bytes"] = os.path.getsize(log_path)
    with open(log_path, "rb") as handle:
        offset = 0
        for raw in handle:
            text = raw.decode("utf-8", errors="replace").strip()
            length = len(raw)
            if text:
                event = AuditEvent.from_json(text)
                entry = _period_entry(index, event.period)
                _absorb(entry, event.seq, offset, offset + length)
                # 锚点只从原索引继承；原索引没有（首次建索引）才取当前首条。
                if not entry["anchor"]:
                    entry["anchor"] = str(
                        previous.get("periods", {}).get(event.period, {}).get("anchor", "")
                        or ""
                    ) or (event.chain_hash if entry["first_seq"] == event.seq else "")
                index["tail_hash"] = event.chain_hash
            offset += length
    _write_index(index_path, index)
    return index


#: 索引与事件文件的三种关系。
IDX_OK = "ok"
IDX_TAIL = "tail"  # 文件更长：补扫尾部
IDX_STALE = "stale"  # 文件更短或边界错位：整份重建


def _index_state(index: Mapping[str, Any], log_path: str) -> str:
    """判断索引与事件文件的关系（见 :data:`IDX_OK` 三态）。

    三种不同步，处理方式各不相同：

    * **文件比索引长**（:data:`IDX_TAIL`）—— 崩在「写完事件、没来得及更新
      索引」那一步。追加扫描尾部补登记即可，已登记的部分不动。
    * **索引比文件长**（:data:`IDX_STALE`）—— 事件文件被截断或换过。
      **此时索引里的区间会指向不存在或错位的内容**，必须整份重建；沿用旧
      索引会把别的账期的行读成这一期的，那比慢严重得多。
    * **字节数相同但边界错位**（:data:`IDX_STALE`）—— 文件被**就地改过**
      且改后长度恰好不变。只比字节数看不出来，但偏移已经指到别的行上了。
      所以还要核**区间端点是否落在行边界上**：这是「偏移还能不能用」的直接
      判据，比任何摘要都便宜，且不会把「改过内容但长度不变」误判成有效。
    """
    if not os.path.isfile(log_path):
        return IDX_OK
    size = os.path.getsize(log_path)
    recorded = int(index.get("bytes", 0) or 0)
    if recorded == size:
        if not _ranges_aligned(log_path, index) or not _tail_intact(index, log_path):
            return IDX_STALE
        return IDX_OK
    if recorded > size:
        logger.warning(
            "事件文件比索引短（文件 %d 字节 / 索引登记 %d 字节），"
            "按「被截断或被换过」处理：整份重建索引 —— 沿用旧偏移会把别的账期的"
            "行读成这一期的",
            size,
            recorded,
        )
        return IDX_STALE
    # 文件更长，**先确认多出来的确实是追加**而不是就地改长：已登记的前缀
    # 必须仍结束在行边界上。不核这一条，尾部补扫会从半行中间开始解析 ——
    # 报出来的是「某行不是合法 JSON」，把「有人就地改过某一行」说成了
    # 「文件写坏了」，方向完全错。
    if not _ends_on_boundary(log_path, recorded) or not _tail_intact(index, log_path):
        logger.warning(
            "事件文件比索引长（文件 %d 字节 / 索引登记 %d 字节），"
            "但已登记前缀的末尾 %d 不在行边界上：按「被就地改过」处理，整份重建索引",
            size,
            recorded,
            recorded,
        )
        return IDX_STALE
    logger.warning(
        "事件文件比索引长（文件 %d 字节 / 索引登记 %d 字节），"
        "疑似写入中断在「落事件」与「更新索引」之间：补扫尾部 %d 字节",
        size,
        recorded,
        size - recorded,
    )
    return IDX_TAIL


def _ends_on_boundary(log_path: str, offset: int) -> bool:
    """``offset`` 之前的最后一个字节是不是换行（``offset`` 在文件开头也算）。"""
    if offset <= 0:
        return True
    with open(log_path, "rb") as handle:
        handle.seek(offset - 1)
        return handle.read(1) == b"\n"


def _tail_intact(index: Mapping[str, Any], log_path: str) -> bool:
    """已登记的**最后一条事件**是否仍能按登记的偏移读出、且指纹不变。

    行边界核验只挡得住「改动恰好跨过已登记前缀末尾」这一种。索引里另记了
    全链链尾指纹（:attr:`_period_entry` 的 ``tail_hash``），读一次最后一行的
    偏移、比一次指纹，就把「就地改过中间某行、后面整体位移、而位移量恰好
    让已登记末尾仍落在行边界上」也挡住。

    代价是每次切片多一次 seek + 一次 sha256 —— 与账期条数无关，
    换的是「读到的一定是登记时那些行」这条保证。
    """
    tail_hash = str(index.get("tail_hash", "") or "")
    if not tail_hash or not index.get("periods"):
        return True
    last_end = 0
    for entry in index["periods"].values():
        for _start, end in entry.get("ranges") or []:
            last_end = max(last_end, int(end))
    if not last_end:
        return True
    with open(log_path, "rb") as handle:
        handle.seek(last_end)
        tail = handle.readline()
    text = tail.decode("utf-8", errors="replace").strip()
    if not text:
        return False
    try:
        return AuditEvent.from_json(text).chain_hash == tail_hash
    except ReconqError:
        return False


def _ranges_aligned(log_path: str, index: Mapping[str, Any]) -> bool:
    """索引登记的每个区间端点是否仍落在行边界上。

    区间是「这一期的第 N 行到第 M 行」在文件里的字节位置。**append-only
    时这个位置永远有效**；一旦有人就地改了某一行（改短改长都会），后面
    所有行都跟着挪，而文件总长度可能恰好没变 —— 这时按旧偏移读出来的是
    半截 JSON，报错还算好的，更坏的情况是**读到了别的账期的整行**，面板
    上显示出一条根本没发生过的处置。

    判据是端点：区间起点要么在文件开头，要么前一个字节是换行；终点要么在
    文件末尾，要么最后一个字节是换行。逐区间两次 seek 即可，与账期数同阶。
    """
    if not index.get("periods"):
        return True
    size = os.path.getsize(log_path)
    with open(log_path, "rb") as handle:
        for entry in index["periods"].values():
            for start, end in entry.get("ranges") or []:
                start, end = int(start), int(end)
                if start < 0 or end > size or start >= end:
                    return False
                if start:
                    handle.seek(start - 1)
                    if handle.read(1) != b"\n":
                        return False
                handle.seek(end - 1)
                if handle.read(1) != b"\n":
                    return False
    return True


def _sync_index(log_dir: str) -> tuple[str, dict[str, Any]]:
    """把索引补齐到与事件文件一致，返回 ``(事件文件路径, 索引)``。"""
    log_path, index_path = _audit_paths(log_dir)
    index = load_index(log_dir)
    if _index_state(index, log_path) == IDX_STALE:
        return (log_path, rebuild_index(log_dir))
    if not os.path.isfile(log_path):
        return (log_path, index)
    size = os.path.getsize(log_path)
    recorded = int(index.get("bytes", 0) or 0)
    if size > recorded:
        with open(log_path, "rb") as handle:
            handle.seek(recorded)
            offset = recorded
            for raw in handle:
                text = raw.decode("utf-8", errors="replace").strip()
                length = len(raw)
                if text:
                    event = AuditEvent.from_json(text)
                    entry = _period_entry(index, event.period)
                    _absorb(entry, event.seq, offset, offset + length)
                    if not entry["anchor"]:
                        entry["anchor"] = event.chain_hash
                    index["tail_hash"] = event.chain_hash
                offset += length
        index["bytes"] = size
        _write_index(index_path, index)
    return (log_path, index)


# ─────────────────────────── 写 ───────────────────────────


def _require_period(period: str) -> str:
    value = str(period or "").strip()
    if not value:
        raise ReconqError(
            "AUDIT_PERIOD_REQUIRED",
            "事件必须带账期：审计链按账期分片，缺账期的事件挂不到任何一条链上，"
            "「这一期发生了什么」会凭空少一截",
        )
    return value


def open_chain(
    log_dir: str,
    period: str,
    *,
    actor: str = "",
    at: str = "",
    scope: str = "",
) -> AuditEvent:
    """账期首次扫描时开链，写下第 1 条并把它的 ``chain_hash`` 记为**锚点**。

    **幂等**：已经开过链的账期直接返回第 1 条，不再追加。重复追加 genesis
    会让链上凭空多一环「又开了一次链」，而面板上只看得见一条 —— 这个数字
    对不上，且因为是 append-only，删不掉。
    """
    period = _require_period(period)
    log_path, index = _sync_index(log_dir)
    if _period_entry(index, period)["count"]:
        for event in _read_log(
            log_path,
            [[int(a), int(b)] for a, b in _period_entry(index, period)["ranges"]],
            period,
        ):
            return event
    return append_event(
        log_dir,
        period,
        "SCAN_STARTED",
        actor=actor,
        at=at,
        payload={"scope": scope} if scope else {},
        bootstrap=True,
    )


def append_event(
    log_dir: str,
    period: str,
    event_type: str,
    *,
    actor: str = "",
    discrepancy_key: str = "",
    payload: Optional[Mapping[str, Any]] = None,
    at: str = "",
    bootstrap: bool = False,
) -> AuditEvent:
    """往事件链追加一环，返回落盘后的事件（含指纹）。

    ``bootstrap=True`` 只给 :func:`open_chain` 用：开链那条必须是该账期的
    第 1 条，且它的指纹就是锚点。普通追加**不许**再抢这个位置 ——
    锚点必须是全链第一环的指纹，中途插入一条自称 genesis 的事件，等于把
    「这条链从哪儿开始」这个问题重新打开一次。
    """
    period = _require_period(period)
    event_type = str(event_type or "").strip()
    if not event_type:
        raise ReconqError(
            "AUDIT_EVENT_TYPE_REQUIRED",
            "事件类型不能为空：类型是受控词表，面板按它分组、按它排时序；"
            "空类型的事件上链后谁也查不出来",
        )
    body = dict(payload or {})
    assert_event_deidentified(body, where=f"{period} {event_type} 的 payload")

    _log, _idx = _audit_paths(log_dir)
    _makedirs(log_dir)
    # **先补齐索引再取链尾**。少了这一步，上一次崩在「落事件、没更新索引」
    # 时追加的那条就等于不存在：新事件会接在**索引记的那个旧尾巴**后面，
    # 与文件里真实存在的上一条对不上前驱指纹 —— 写是写进去了，校验立刻报
    # 「第 N 条前驱指纹对不上」，而现场刚跑完一遍正常流程。
    log_path, index = _sync_index(log_dir)
    index_path = _audit_paths(log_dir)[1]

    if bootstrap:
        if _period_entry(index, period)["count"]:
            for event in _read_log(log_path, None, period):
                return event
        prev_hash = GENESIS_HASH
        seq = 1
    else:
        prev_hash, seq = _tail_of(log_path, index, period)

    stamp = at or _now()
    digest = _event_chain_hash(
        seq, period, event_type, stamp, actor, discrepancy_key, body, prev_hash
    )
    event = AuditEvent(
        seq=seq,
        period=period,
        event_type=event_type,
        at=stamp,
        actor=actor,
        discrepancy_key=discrepancy_key,
        payload=body,
        prev_hash=prev_hash,
        chain_hash=digest,
    )

    line = (event.to_json() + "\n").encode("utf-8")
    start = os.path.getsize(log_path) if os.path.isfile(log_path) else 0
    with open(log_path, "ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    end = start + len(line)

    entry = _period_entry(index, period)
    _absorb(entry, seq, start, end)
    if not entry["anchor"]:
        entry["anchor"] = digest
    index["tail_hash"] = digest
    index["bytes"] = end
    _write_index(index_path, index)
    return event


def _makedirs(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)


def _tail_of(log_path: str, index: Mapping[str, Any], period: str) -> tuple[str, int]:
    """该账期链尾的 ``(chain_hash, next_seq)``。"""
    if not os.path.isfile(log_path):
        return (GENESIS_HASH, 1)
    size = os.path.getsize(log_path)
    entry = index.get("periods", {}).get(period) or {}
    ranges = entry.get("ranges") or []
    if not ranges:
        return (GENESIS_HASH, 1)
    events = _read_log(log_path, [(int(r[0]), int(r[1])) for r in ranges], period)
    if not events:
        return (GENESIS_HASH, 1)
    return (events[-1].chain_hash, events[-1].seq + 1)


# ─────────────────────────── 读 ───────────────────────────


@dataclass(frozen=True)
class EventSlice:
    """一次切片查询的结果。**同时是序列**（可迭代、可索引、可取长度）。"""

    events: tuple[AuditEvent, ...]
    #: 本次实际从磁盘读的字节数。用来证明「按期切片没有全量扫描」——
    #: 没有这个数，「走索引了」只是一句没人能核的话。
    bytes_scanned: int
    #: 是否命中了索引。``False`` 表示走了全量扫描（没索引 / 索引过期）。
    used_index: bool
    #: 表外事件类型（已告警但**未丢弃**）。
    unknown_types: tuple[str, ...] = ()
    #: 事件时刻无法解析的条数。带时间窗查询时它们**一律不返回** ——
    #: 猜一个时刻把它们放进来，比明确少几条更坏。
    unparsed_at: int = 0

    def __iter__(self) -> Iterator[AuditEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, item: Any) -> Any:
        return self.events[item]

    def of_type(self, event_type: str) -> tuple[AuditEvent, ...]:
        return tuple(e for e in self.events if e.event_type == event_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [e.to_dict() for e in self.events],
            "count": len(self.events),
            "bytes_scanned": self.bytes_scanned,
            "used_index": self.used_index,
            "unknown_types": list(self.unknown_types),
            "unparsed_at": self.unparsed_at,
        }


def _parse_moment(value: str, *, raw: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ReconqError(
            "AUDIT_QUERY_INVALID",
            f"时间窗参数 {raw!r} 为空。ISO 8601 形如 2026-09-07T00:00:00+08:00；"
            "空串会被当成「不限制」，那正是「限了这一期却查了全库」的成因",
        )
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReconqError(
            "AUDIT_QUERY_INVALID",
            f"时间窗参数 {raw!r} 不是 ISO 8601 时刻（{text!r}）：{exc}。"
            "请写成 2026-09-07T00:00:00+08:00 这样的完整时刻",
        ) from exc


def _read_log(
    log_path: str,
    ranges: Optional[Sequence[Sequence[int]]],
    period: Optional[str],
) -> list[AuditEvent]:
    """按字节区间读事件。``ranges=None`` 表示全量扫描。"""
    if not os.path.isfile(log_path):
        return []
    wanted = None if ranges is None else [(int(a), int(b)) for a, b in ranges]
    events: list[AuditEvent] = []
    with open(log_path, "rb") as handle:
        if wanted is None:
            handle.seek(0)
            for raw in handle:
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    events.append(AuditEvent.from_json(text))
        else:
            for start, end in wanted:
                handle.seek(start)
                for raw in handle.read(max(0, end - start)).splitlines():
                    text = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else raw.strip()
                    if text:
                        events.append(AuditEvent.from_json(text))
    if period:
        events = [e for e in events if e.period == period]
    events.sort(key=lambda e: e.seq)
    return events


def query_events(
    log_dir: str,
    period: Optional[str] = None,
    *,
    event_type: Optional[str] = None,
    discrepancy_key: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> EventSlice:
    """切片读回事件（**只读打开**，不写索引、不改事件文件）。

    给了 ``period`` 且索引命中时，只读该账期登记的字节区间；其余情况
    （没给账期 / 索引缺失）走全量扫描，并在 :attr:`EventSlice.used_index`
    上如实标出来 —— 调用方能一眼看出「这次是不是全库扫的」。
    """
    log_path, _ = _audit_paths(log_dir)
    log_path, index = _sync_index(log_dir)

    ranges: Optional[list[list[int]]] = None
    used_index = False
    if period:
        entry = index.get("periods", {}).get(period) or {}
        if entry.get("ranges"):
            ranges = [[int(a), int(b)] for a, b in entry["ranges"]]
            used_index = True

    if ranges is not None:
        size = sum(b - a for a, b in ranges)
    elif os.path.isfile(log_path):
        size = os.path.getsize(log_path)
    else:
        size = 0

    events = _read_log(log_path, ranges, period)
    unknown: list[str] = []
    for event in events:
        if event.event_type not in EVENT_TYPES and event.event_type not in unknown:
            unknown.append(event.event_type)
            logger.warning(
                "事件类型 %r 不在受控词表内（seq=%d，账期=%s）：原样返回并计入 "
                "unknown_types，**不丢弃** —— 静默丢掉等于让「这一期发生过什么」"
                "凭空少一段，而丢的人不会知道自己丢了什么",
                event.event_type,
                event.seq,
                event.period,
            )

    lower = _parse_moment(since, raw="since") if since else None
    upper = _parse_moment(until, raw="until") if until else None
    if lower and upper and lower > upper:
        raise ReconqError(
            "AUDIT_QUERY_INVALID",
            f"时间窗反了：since={since} 晚于 until={until}。这样的窗口会返回空集，"
            "而空集在面板上看起来和「这一段时间真的没事件」一模一样",
        )

    unparsed = 0
    if lower or upper:
        kept: list[AuditEvent] = []
        for event in events:
            try:
                moment = datetime.fromisoformat(event.at) if event.at else None
            except ValueError:
                moment = None
            if moment is None:
                unparsed += 1
                continue
            if lower and moment < lower:
                continue
            if upper and moment > upper:
                continue
            kept.append(event)
        events = kept

    if event_type:
        events = [e for e in events if e.event_type == event_type]
    if discrepancy_key:
        events = [e for e in events if e.discrepancy_key == discrepancy_key]

    return EventSlice(
        events=tuple(events),
        bytes_scanned=size,
        used_index=used_index,
        unknown_types=tuple(unknown),
        unparsed_at=unparsed,
    )


def discrepancy_timeline(log_dir: str, discrepancy_key: str) -> list[AuditEvent]:
    """一条差异从建账到导出的完整时序（正序）。

    账期从差异键里取：业务键的形状是 ``账期|渠道|口径路径``
    （见 :func:`reconq.align.aggregate.diff_key`），因此这一查**走索引切片**
    而不是全库扫 —— 一期几百万条事件时，面板上点一条差异不该等全库读完。
    键的形状变了（少一段、多一段）时退化为全量扫描并照常返回，**不报错**：
    时序查不到不该让面板整个打不开。
    """
    key = str(discrepancy_key or "").strip()
    if not key:
        raise ReconqError(
            "AUDIT_DIFFERENCE_KEY_REQUIRED",
            "查差异时序必须给差异键（形如 2026-W36|wechat|taxonomy.missing:EX-9）。"
            "空键会匹配到链上**所有**没有差异键的事件 —— 那不是这一条差异的时序，"
            "是一整期的流水",
        )
    period = key.split("|", 1)[0].strip()
    if period:
        found = query_events(log_dir, period, discrepancy_key=key)
        if found.events:
            return list(found.events)
    return list(query_events(log_dir, discrepancy_key=key).events)


def chain_anchor(log_dir: str, period: str) -> str:
    """该账期的**链头锚点**（第 1 条的 ``chain_hash``）；没开链返回空串。"""
    period = _require_period(period)
    _log_path, index = _sync_index(log_dir)
    entry = index.get("periods", {}).get(period) or {}
    return str(entry.get("anchor", "") or "")


# ─────────────────────────── 校验 ───────────────────────────


@dataclass(frozen=True)
class EventChainVerdict:
    """链式校验结论。"""

    ok: bool
    checked: int
    broken_seq: int
    reason: str
    expected_hash: str = ""
    actual_hash: str = ""
    #: 锚点是否仍与第 1 条的指纹一致。**与 ``ok`` 分开报**：链体重算通过
    #: 但锚点对不上，说明有人删掉了开头若干条又重接了一条能自洽的链 ——
    #: 这是「删记录」而不是「改记录」，只报 ok=True 会把它放过。
    anchor_ok: bool = True
    anchor_expected: str = ""
    anchor_actual: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "broken_seq": self.broken_seq or None,
            "reason": self.reason,
            "expected_hash": self.expected_hash[:16],
            "actual_hash": self.actual_hash[:16],
            "anchor_ok": self.anchor_ok,
            "anchor_expected": self.anchor_expected[:16],
            "anchor_actual": self.anchor_actual[:16],
        }


def verify_chain(log_dir: str, period: str) -> EventChainVerdict:
    """重算该账期整条链的指纹。

    报四类问题，各自指向不同的现场处置：

    1. **序号跳号** —— 中间有记录被删或被插；
    2. **前驱指纹对不上** —— 接缝坏了；
    3. **本条指纹与重算值不一致** —— 这条内容被改过，**并报出它是第几条**；
    4. **锚点对不上** —— 开头若干条被删掉后重接了一条自洽的链（链体重算
       全过，只有锚点能看出来）。

    **不抛异常**：校验的用途是让人看见，而不是让程序崩。
    """
    period = _require_period(period)
    log_path, index = _sync_index(log_dir)
    entry = index.get("periods", {}).get(period) or {}
    ranges = [[int(a), int(b)] for a, b in (entry.get("ranges") or [])]
    events = _read_log(log_path, ranges or None, period)
    anchor = str(entry.get("anchor", "") or "")

    if not events:
        return EventChainVerdict(
            ok=True,
            checked=0,
            broken_seq=0,
            reason="该账期还没有事件（未开链）" if not anchor else "链为空但索引记了锚点",
            anchor_ok=not anchor,
            anchor_expected=anchor,
        )

    prev = GENESIS_HASH
    expected_seq = 1
    for event in events:
        if event.seq != expected_seq:
            return EventChainVerdict(
                ok=False,
                checked=expected_seq - 1,
                broken_seq=int(event.seq),
                reason=f"序号跳号：期望第 {expected_seq} 条，实际第 {event.seq} 条"
                "（中间有记录被删除或插入）",
                expected_hash=prev,
                actual_hash=event.chain_hash,
                anchor_ok=False,
                anchor_expected=anchor,
                anchor_actual=events[0].chain_hash,
            )
        if event.prev_hash != prev:
            return EventChainVerdict(
                ok=False,
                checked=expected_seq - 1,
                broken_seq=int(event.seq),
                reason=f"第 {event.seq} 条的前驱指纹对不上：期望 {prev[:12]}…，"
                f"实际 {str(event.prev_hash)[:12]}…",
                expected_hash=prev,
                actual_hash=str(event.prev_hash),
                anchor_ok=False,
                anchor_expected=anchor,
                anchor_actual=events[0].chain_hash,
            )
        recomputed = _event_chain_hash(
            event.seq,
            event.period,
            event.event_type,
            event.at,
            event.actor,
            event.discrepancy_key,
            event.payload,
            prev,
        )
        if recomputed != event.chain_hash:
            return EventChainVerdict(
                ok=False,
                checked=expected_seq - 1,
                broken_seq=int(event.seq),
                reason=f"第 {event.seq} 条（{event.event_zh}，{event.at}）的内容"
                "与它的指纹不一致 —— 这条被改过",
                expected_hash=recomputed,
                actual_hash=event.chain_hash,
                anchor_ok=False,
                anchor_expected=anchor,
                anchor_actual=events[0].chain_hash,
            )
        prev = event.chain_hash
        expected_seq += 1

    anchor_ok = (not anchor) or anchor == events[0].chain_hash
    if not anchor_ok:
        return EventChainVerdict(
            ok=False,
            checked=len(events),
            broken_seq=1,
            reason=f"链头锚点对不上：登记的锚点 {anchor[:12]}… 与第 1 条的实际指纹 "
            f"{events[0].chain_hash[:12]}… 不一致 —— 链身自洽但**开头被换过**"
            "（删掉前若干条再重接），只看链尾是看不出来的",
            expected_hash=anchor,
            actual_hash=events[0].chain_hash,
            anchor_ok=False,
            anchor_expected=anchor,
            anchor_actual=events[0].chain_hash,
        )

    return EventChainVerdict(
        ok=True,
        checked=len(events),
        broken_seq=0,
        reason="链完整",
        expected_hash=prev,
        actual_hash=prev,
        anchor_ok=True,
        anchor_expected=anchor,
        anchor_actual=events[0].chain_hash,
    )
