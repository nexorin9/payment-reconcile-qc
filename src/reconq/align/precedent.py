"""先例库：人判过的成因沉淀成「下次同类差异先给建议」（Task 41）。

先例不是「上一期结论的复制」
--------------------------
台账（``period_diff_items``）已经能回答「近几期同类差异有没有被处置过」，
:func:`reconq.align.bundle.query_cause_history` 也已经把这份频次送进证据包。
那为什么还要先例库？因为**台账记的是「有没有人办过」，先例库记的是
「办的人当时判成了什么」** —— 台账的 ``disposition`` 列只有「已确认 /
已驳回」两态，确认的那条差异的成因落在另一张表（``attribution_drafts``）
的**草稿**上，而草稿是模型给的、不是人给的。人点确认时确认的是「这张单
我认了」，不是「草稿里第一条就是真相」。先例库记的是**人最终认下的那个
成因码**，这才是下一期值得先看一眼的东西。

三条不肯让的边界
----------------
1. **只有人工确认才写先例，驳回的不写。** 把经办人判为「不是这个原因」
   的样本也存成先例，下一期会给出恰好被否掉的建议 —— 而这一期没人再去查
   证据，因为「上次就是这么判的」。:func:`record_confirmation` 在**入口**
   校验处置状态，且拒绝非自然人操作人（与
   :func:`reconq.align.aggregate.assert_human_operator` 同一条纪律，但这里是
   先例库自己的关口，不依赖调用方记得调它）。
2. **先例是建议，不是结论。** :func:`suggest_for` 产出的
   :class:`PrecedentSuggestion` 只能作为**一条候选**出现在归因草稿里，**不
   替代**模型草稿，也不替代规则基线；它的置信度另设封顶
   （:data:`PRECEDENT_MAX_CONFIDENCE`，高于规则的 0.5 —— 它确实来自人的
   判断，但低于模型，因为它没有看过这期的账单列业务含义）。
3. **版本一变即降权。** 先例登记时记下当时的成因字典版本与提示词版本。改版
   后旧先例**降为参考**（:attr:`PrecedentSuggestion.authority` 从
   ``strong`` 变 ``reference``，置信度另按 :data:`PRECEDENT_REFERENCE_CAP`
   封顶并在文案里写明「当时的字典/提示词版本与现在不同」），而不是直接丢弃
   也不是照单全收。丢弃浪费人的判断；照单全收会让一条按旧规则判出来的成因
   在新规则下继续被推荐，而两版规则为什么不同通常没人记得。

特征签名：为什么不是「按成因查」
--------------------------------
检索键是**可比较特征的稳定哈希**（:func:`feature_signature`），不是成因码，
也不是单号。理由是「同类差异」这件事本来就说不清边界：上期是
「微信 + 一码付拆多笔 + 候选 3 条 + 金额守恒」，这期是「微信 + 一码付拆多笔
+ 候选 4 条 + 金额守恒」—— 候选条数差一条，它们是不是同一件事？按成因查
会把「上期同形态但成因不同」的那几条也捞进来（形态到成因是多对一，
:func:`reconq.align.causes.cause_from_ambiguity_kind`），于是「上期有人这么判过」
这句话变得没有信息量。

六项特征（:data:`SIGNATURE_FIELDS`）刻意**全部是可比较的形态量**，且**不含
患者可识别信息**：

======================  ====================================================
特征                    为什么进签名
======================  ====================================================
``channel``             不同渠道的成因分布差别极大
``kind``                差异形态（对齐器视角）
``trade_type``          渠道给出的交易类型枚举
``cross_period``        是否压在账期边界上
``candidate_count``     候选条数
``amounts_equal``       候选金额是否全部相等
``order_no_prefix``     单号前缀形态（``WXTXN`` / ``EX-WX`` …）
======================  ====================================================

刻意**不**进签名的：金额本身、单号全量、科室、导出文件名。金额进签名会让
每一条差异都成为独立先例（等于没有先例）；单号全量与患者可识别性沾边
（:func:`reconq.align.snapshot.assert_deidentified` 会拦），科室与文件名
则会把「同一种问题在不同科室重复导出」这一最值得复用的形态切碎。

**单号只取前缀形态**：用 :func:`_prefix_shape` 把单号归成
``ALPHA2`` / ``ALPHA-DIGIT`` / ``DIGIT`` 之类的形态标签，不保留具体前缀
字符。这样「微信交易单号」与「支付宝交易号」不会因为前缀不同而各自分成
两个先例桶，而具体单号也不会进检索键。

先例可审计：留痕记的是**引用**不是拷贝
--------------------------------------
:func:`record_hit` 把「谁在何时采信了哪条先例」写进与
:mod:`reconq.engine.resume` 同一条事件链（append-only + sha256 链式指纹）。
只记 ``signature`` 不记整份特征是为了让事件链上的体积可控；代价是**特征
签名被改算法后历史命中对不上** —— 所以 :data:`PRECEDENT_SIGNATURE_VERSION``
进先例行，改算法必须 bump 它（与 :mod:`reconq.align.schema_version` 对提示词
指纹的处理同一条纪律）。

先例看板
--------
:func:`top_causes` 回答「本渠道历史最常见成因 Top5」：按命中次数降序，
条数与金额分列（理由与 :func:`reconq.align.causes.aggregate_causes` 相同：
拆笔类成因一条带几千块，渠道漏传类几十条加起来不到一百元，只看金额会把
最该找渠道方谈的那一组排到后面）。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层，也不会有**。它的 ``contract verify``
逐 check 出的 fail 是**确定性判定**，不存在「为什么会有这一行」这个问题；
即便有，源产品也是把结果打到终端、进程退出即消失。本模块沿用的仍是那套主
路径纪律 —— 判据收口在一处、结构不合约定就拒收、结论交人工；新增的是
「从「哪一行不合规」到「这件事归谁、上期谁这么判过」」的这一跳，以及
「人判过的结论能被复用」这个闭环。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from ..errors import ReconqError
from .causes import CAUSE_DICT_VERSION, CAUSES, entry_of
from .schema_version import current_pair

__all__ = [
    "PRECEDENT_MAX_CONFIDENCE",
    "PRECEDENT_SOURCE",
    "PRECEDENT_MIN_HITS",
    "PRECEDENT_REFERENCE_CAP",
    "PRECEDENT_SIGNATURE_VERSION",
    "SIGNATURE_FIELDS",
    "FeatureSignature",
    "Precedent",
    "PrecedentHit",
    "PrecedentSuggestion",
    "PrecedentWrite",
    "PrecedentTopRow",
    "assert_human_operator",
    "feature_signature",
    "init_precedent_schema",
    "list_hits",
    "list_precedents",
    "load_precedent",
    "record_confirmation",
    "record_hit",
    "render_precedent_hint_zh",
    "signature_of_bundle",
    "suggest_for",
    "top_causes",
]

logger = logging.getLogger("reconq.align.precedent")

#: 六路受控渠道。**刻意不接受任意字符串** —— 渠道是特征签名的一半，
#: 写错渠道等于把先例存到了另一个地方，而那种错误事后查不出来
#: （检索时两边都少一半命中，各自看起来都「正常」）。
CONTROLLED_CHANNELS: frozenset[str] = frozenset(
    {"wechat", "alipay", "unionpay", "self_service", "his", "medical_ins"}
)

#: 特征签名的**算法版本**。进先例行：改了参与签名的特征集或前缀形态规则
#: 就必须 bump，否则历史先例的签名与新算出来的对不上，检索会静默地一条也
#: 不命中（表现为「先例库好像没东西」，而真因是算法换了）。
PRECEDENT_SIGNATURE_VERSION = "precedent-sig/1"

#: 进签名的特征名。**顺序即拼接顺序**，改动必须 bump 上面的版本串。
SIGNATURE_FIELDS: tuple[str, ...] = (
    "channel",
    "kind",
    "trade_type",
    "cross_period",
    "candidate_count",
    "amounts_equal",
    "order_no_prefix",
)

#: 先例建议的置信度封顶。**高于规则的 0.5**（它确实来自人的判断，
#: 规则读不出成因），**低于模型**（它没看过这期的账单列业务含义）。
PRECEDENT_MAX_CONFIDENCE = Decimal("0.7")

#: 版本对不上时的封顶。给得比 :data:`PRECEDENT_MAX_CONFIDENCE` 低一截，
#: 但**不为零** —— 丢弃人的判断同样是一种浪费，降到「佐证」这一档才是诚实的。
PRECEDENT_REFERENCE_CAP = Decimal("0.4")

#: 命中次数下限。低于这个数不进建议：一条先例只能说明「有人这么判过」，
#: 而「有人这么判过一次」与「上期那笔是个意外」在证据上无法区分。
PRECEDENT_MIN_HITS = 2

#: 留痕表里存的样本差异键上限。**刻意截断**：样本键是给人回查的线索，
#: 不是账本；一条先例攒到 50 个样本键时，面板上要的是「最近命中时间」，
#: 不是一份越来越长的列表。
MAX_SAMPLE_KEYS = 20

#: 先例行上「先例被采纳过几次」与「先例被登记过几次」是两个数，刻意分列：
#: 前者是先例好不好用的指标，后者只说明有没有人在判。
_DIGEST_LEN = 16

#: 先例建议的来源标记。**与规则基线的 ``RULE_SOURCE`` 不同值**：面板上
#: 模型草稿 / 规则推断 / 先例三者混在一列时，靠它就能分开「这期算的、
#: 这期推的、人以前判的」。
PRECEDENT_SOURCE = "precedent_library"

#: 非自然人操作人。与 aggregate 的白名单同口径，但**各自定义** ——
#: aggregate 那份管的是「能不能推进台账状态」，这份管的是「能不能登记先例」。
#: 共用一个常量会让两处耦合，将来改一处另一处跟着变，而它们本就该能独立演化。
_NON_HUMAN_OPERATORS = frozenset(
    {"model", "rule", "rules", "baseline", "system", "auto", "precedent", "规则", "模型", "系统", "自动"}
)


# ─────────────────────────── 表结构 ───────────────────────────


PRECEDENT_TABLE = "precedent_library"
PRECEDENT_HIT_TABLE = "precedent_hits"

PRECEDENT_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {PRECEDENT_TABLE} (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    signature          TEXT NOT NULL,
    signature_version  TEXT NOT NULL DEFAULT '{PRECEDENT_SIGNATURE_VERSION}',
    cause_code         TEXT NOT NULL,
    channel            TEXT NOT NULL,
    ambiguity_kind     TEXT NOT NULL DEFAULT '',
    features_json      TEXT NOT NULL DEFAULT '{{}}',
    hit_count          INTEGER NOT NULL DEFAULT 0,
    first_hit_at       TEXT NOT NULL DEFAULT '',
    last_hit_at        TEXT NOT NULL DEFAULT '',
    sample_keys_json   TEXT NOT NULL DEFAULT '[]',
    dict_version       TEXT NOT NULL DEFAULT '',
    dict_digest        TEXT NOT NULL DEFAULT '',
    prompt_version     TEXT NOT NULL DEFAULT '',
    prompt_digest      TEXT NOT NULL DEFAULT '',
    confirmed_by       TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL DEFAULT '',
    updated_at         TEXT NOT NULL DEFAULT ''
)
"""

#: 幂等键 = 签名 + 成因码。**同一条差异被确认两次不会记成两条先例**：
#: 命中次数按 (签名, 成因) 累加，样本键去重后追加。先例库记的是「这类问题
#: 历史上被这样判过几次」，不是「有几张单被点过」。
PRECEDENT_INDEXES = (
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_precedent_identity
        ON {PRECEDENT_TABLE} (signature, cause_code)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS ix_precedent_channel
        ON {PRECEDENT_TABLE} (channel, hit_count DESC)
    """,
)

PRECEDENT_HIT_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {PRECEDENT_HIT_TABLE} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    period        TEXT NOT NULL,
    channel       TEXT NOT NULL,
    diff_key      TEXT NOT NULL DEFAULT '',
    signature     TEXT NOT NULL,
    cause_code    TEXT NOT NULL,
    precedent_id  INTEGER NOT NULL,
    hit_count_at  INTEGER NOT NULL DEFAULT 0,
    authority     TEXT NOT NULL DEFAULT 'strong',
    actor         TEXT NOT NULL DEFAULT '',
    version_pair  TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    recorded_at   TEXT NOT NULL DEFAULT ''
)
"""

#: 幂等键 = 账期 + 渠道 + 差异键 + 签名 + 成因。同一差异的同一条先例
#: 采信两次（例如经办人点开面板两次）不重复留痕。
PRECEDENT_HIT_INDEXES = (
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_precedent_hit_identity
        ON {PRECEDENT_HIT_TABLE} (period, channel, diff_key, signature, cause_code)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS ix_precedent_hit_period
        ON {PRECEDENT_HIT_TABLE} (period, channel)
    """,
)


def init_precedent_schema(conn: sqlite3.Connection) -> None:
    """建先例表与命中留痕表。幂等：重复调用不报错。"""
    conn.execute(PRECEDENT_TABLE_DDL)
    for statement in PRECEDENT_INDEXES:
        conn.execute(statement)
    conn.execute(PRECEDENT_HIT_TABLE_DDL)
    for statement in PRECEDENT_HIT_INDEXES:
        conn.execute(statement)


# ─────────────────────────── 特征签名 ───────────────────────────

#: 单号前缀形态的分类正则。**只保留形态标签，不保留前缀字符**。
_PREFIX_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("ALPHA-DIGIT", re.compile(r"^[A-Za-z]+-\d")),
    ("ALPHA", re.compile(r"^[A-Za-z]+$")),
    ("ALPHA_MIXED", re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")),
    ("DIGIT", re.compile(r"^\d+$")),
    ("OTHER", re.compile(r"^.")),
)


def _prefix_shape(order_no: Any) -> str:
    """单号 → **形态标签**（``ALPHA`` / ``ALPHA-DIGIT`` / ``DIGIT`` …）。

    刻意不返回前缀本身：先例库是要长期留存并被反复检索的表，把具体单号
    （乃至具体前缀）放进检索键，等于让这张表变成患者标识的旁路存储。而
    「微信交易单号 vs 商户订单号 vs 数字串」这个区分，用形态标签就够了。
    """
    text = str(order_no or "").strip()
    if not text:
        return "EMPTY"
    for label, pattern in _PREFIX_PATTERNS:
        if pattern.match(text):
            return label
    return "OTHER"  # pragma: no cover - 最后一条正则 ^. 兜住一切


def _amounts_equal(values: Sequence[Any]) -> bool:
    """候选金额是否**全部相等**。

    比「金额差是不是零」更贴近成因判据：扇出侧 66.00 + 66.00 与
    66.00 + 65.00 在「守恒」上可能同样成立，但前者是典型的
    「同额两笔被并进来」，后者是「分摊没做对」，成因不同。
    """
    amounts = [str(v) for v in values if v not in (None, "")]
    if len(amounts) < 2:
        return False
    first = amounts[0]
    return all(a == first for a in amounts)


@dataclass(frozen=True)
class FeatureSignature:
    """一条差异的**可比较特征**及其稳定哈希。

    :attr:`features` 是参与签名的原始特征字典（可读、可在结论单上展示
    「为什么这两条被认为同类」）；:attr:`digest` 是它的 sha256 前 16 位。

    **不落单号全量、不落金额、不落患者可识别信息** —— 理由见模块头。
    """

    features: Mapping[str, Any]
    digest: str
    version: str = PRECEDENT_SIGNATURE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.digest,
            "signature_version": self.version,
            "features": dict(self.features),
        }

    def render_zh(self) -> str:
        """人读的一行（面板 / CLI 展示「这两条为什么算同类」）。"""
        items = (
            f"渠道 {self.features.get('channel', '')}",
            f"形态 {self.features.get('kind', '') or '（无）'}",
            f"交易类型 {self.features.get('trade_type', '') or '（空）'}",
            f"跨日边界 {'是' if self.features.get('cross_period') else '否'}",
            f"候选 {self.features.get('candidate_count', 0)} 条",
            f"候选金额{'全同' if self.features.get('amounts_equal') else '不全同'}",
            f"单号形态 {self.features.get('order_no_prefix', '')}",
        )
        return " · ".join(items)


def feature_signature(
    *,
    channel: str,
    kind: str = "",
    trade_type: str = "",
    cross_period: bool = False,
    candidate_count: int = 0,
    candidate_amounts: Sequence[Any] = (),
    order_no: str = "",
) -> FeatureSignature:
    """算一条差异的特征签名。

    六项特征里**只有渠道是必填**：其余五项取不到时按「空」参与签名，
    而不是拒绝 —— 一条差异确实可能没有候选行（口径差异而不是对齐差异），
    那时 ``candidate_count=0`` 就是**事实**而不是缺失。渠道不同则是**错误**，
    因为它会让先例存到另一个地方去（:data:`CONTROLLED_CHANNELS`）。
    """
    chan = str(channel or "").strip()
    if chan not in CONTROLLED_CHANNELS:
        raise ReconqError(
            "PRECEDENT_CHANNEL_UNKNOWN",
            f"渠道 {channel!r} 不在六路受控渠道内（{sorted(CONTROLLED_CHANNELS)}）。"
            "渠道是特征签名的一半，写错渠道等于把先例存到另一个地方，"
            "而那种错误事后查不出来",
        )
    if candidate_count < 0:
        raise ReconqError(
            "PRECEDENT_SIGNATURE_INVALID",
            f"候选条数不能为负（收到 {candidate_count}）",
        )
    features: dict[str, Any] = {
        "channel": chan,
        "kind": str(kind or "").strip(),
        "trade_type": str(trade_type or "").strip(),
        "cross_period": bool(cross_period),
        "candidate_count": int(candidate_count),
        "amounts_equal": _amounts_equal(candidate_amounts),
        "order_no_prefix": _prefix_shape(order_no),
    }
    raw = json.dumps(
        {"version": PRECEDENT_SIGNATURE_VERSION, "features": features},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    return FeatureSignature(features=features, digest=digest)


def signature_of_bundle(bundle: Any) -> FeatureSignature:
    """从证据包（:class:`~reconq.align.bundle.EvidenceBundle`）算特征签名。

    归因阶段手上是证据包而不是原始行，所以这一路是主路径；直接拿行的调用
    走 :func:`feature_signature`。

    取值口径刻意与 :func:`reconq.align.baseline._Ctx` 一致（同一份证据包
    在两处读出的形态不能不一样），候选金额取 ``candidate_rows`` 里每行的
    ``amount``。
    """
    payload = getattr(bundle, "payload", None) or {}
    diff_row = payload.get("diff_row") or {}
    candidate_rows = payload.get("candidate_rows") or []
    return feature_signature(
        channel=payload.get("channel", ""),
        kind=(payload.get("discrepancy") or {}).get("kind", ""),
        trade_type=diff_row.get("trade_type", ""),
        cross_period=bool(
            diff_row.get("cross_period")
            or any(row.get("cross_period") for row in candidate_rows)
        ),
        candidate_count=int(payload.get("candidate_total", len(candidate_rows)) or 0),
        candidate_amounts=[row.get("amount", "") for row in candidate_rows],
        order_no=diff_row.get("order_no") or diff_row.get("record_id", ""),
    )


# ─────────────────────────── 数据结构 ───────────────────────────


@dataclass(frozen=True)
class Precedent:
    """一条先例。**只由人工确认产生。**"""

    id: int
    signature: str
    cause_code: str
    channel: str
    ambiguity_kind: str = ""
    features: Mapping[str, Any] = field(default_factory=dict)
    hit_count: int = 0
    first_hit_at: str = ""
    last_hit_at: str = ""
    sample_keys: tuple[str, ...] = ()
    dict_version: str = ""
    dict_digest: str = ""
    prompt_version: str = ""
    prompt_digest: str = ""
    signature_version: str = PRECEDENT_SIGNATURE_VERSION
    confirmed_by: str = ""

    @property
    def zh_name(self) -> str:
        entry = CAUSES.get(self.cause_code)
        return entry.zh_name if entry else f"未登记成因（{self.cause_code}）"

    @property
    def owner(self) -> str:
        entry = CAUSES.get(self.cause_code)
        return entry.owner if entry else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "signature": self.signature,
            "signature_version": self.signature_version,
            "cause_code": self.cause_code,
            "zh_name": self.zh_name,
            "owner": self.owner,
            "channel": self.channel,
            "ambiguity_kind": self.ambiguity_kind,
            "features": dict(self.features),
            "hit_count": self.hit_count,
            "first_hit_at": self.first_hit_at,
            "last_hit_at": self.last_hit_at,
            "sample_keys": list(self.sample_keys),
            "dict_version": self.dict_version,
            "dict_digest": self.dict_digest,
            "prompt_version": self.prompt_version,
            "prompt_digest": self.prompt_digest,
            "confirmed_by": self.confirmed_by,
        }


@dataclass(frozen=True)
class PrecedentWrite:
    """一次先例登记的结果。``created=True`` 表示新建了一条先例行，
    否则是命中已有先例、命中次数 +1。"""

    signature: str
    cause_code: str
    created: bool
    hit_count: int
    precedent_id: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "cause_code": self.cause_code,
            "created": self.created,
            "hit_count": self.hit_count,
            "precedent_id": self.precedent_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PrecedentHit:
    """一条「谁在何时采信了哪条先例」的留痕。**引用而非拷贝**：记
    ``precedent_id`` 与当时的 ``hit_count``，先例行被改后仍能答出
    「当时它被记成命中过几次」。"""

    period: str
    channel: str
    diff_key: str
    signature: str
    cause_code: str
    precedent_id: int
    hit_count_at: int = 0
    authority: str = "strong"
    actor: str = ""
    version_pair: str = ""
    note: str = ""
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "diff_key": self.diff_key,
            "signature": self.signature,
            "cause_code": self.cause_code,
            "precedent_id": self.precedent_id,
            "hit_count_at": self.hit_count_at,
            "authority": self.authority,
            "actor": self.actor,
            "version_pair": self.version_pair,
            "note": self.note,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class PrecedentSuggestion:
    """检索到的一条先例建议。**是建议，不是结论。**

    :attr:`authority` 取 ``strong``（版本对得上）或 ``reference``（版本已变，
    降权为参考）。两种情况下它都**不推进任何状态**，都只是归因草稿里的一条候选。
    """

    cause_code: str
    zh_name: str
    signature: str
    hit_count: int
    last_hit_at: str
    authority: str = "strong"
    confidence: Decimal = PRECEDENT_MAX_CONFIDENCE
    version_note: str = ""
    owner: str = ""
    sample_keys: tuple[str, ...] = ()

    @property
    def precedent_hit(self) -> bool:
        """恒为真。存在的意义是让面板与 ``--json`` 摘要能**按字段筛**出
        「哪些候选是先例来的」—— 与模型草稿、规则候选混在同一列时，
        「哪条是经验、哪条是推理」必须一眼能分开。"""
        return True

    @property
    def source_label(self) -> str:
        """来源标记。**刻意与规则基线的 ``RULE_SOURCE`` 不同值** ——
        面板上三者（模型草稿 / 规则推断 / 先例）混在一列时，靠这一个串
        就能分开「哪条是这期算的、哪条是这期推的、哪条是人以前判的」。
        """
        return PRECEDENT_SOURCE

    @property
    def source_label_zh(self) -> str:
        return "先例(人工确认过)" if self.authority == "strong" else "先例(仅供参考，版本已变)"

    @property
    def confidence_text(self) -> str:
        return format(self.confidence, ".2f")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_code": self.cause_code,
            "zh_name": self.zh_name,
            "signature": self.signature,
            "hit_count": self.hit_count,
            "last_hit_at": self.last_hit_at,
            "precedent_hit": True,
            "source": self.source_label,
            "source_label_zh": self.source_label_zh,
            "authority": self.authority,
            "confidence": self.confidence_text,
            "version_note": self.version_note,
            "owner": self.owner,
            "sample_keys": list(self.sample_keys),
        }

    def render_zh(self) -> str:
        mark = "先例(可参考)" if self.authority == "reference" else "先例"
        line = (
            f"{mark} · {self.zh_name}（历史命中 {self.hit_count} 次，"
            f"最近 {self.last_hit_at or '未记录'}，置信度 {self.confidence_text}）"
        )
        if self.version_note:
            line += f" —— {self.version_note}"
        return line


@dataclass(frozen=True)
class PrecedentTopRow:
    """看板一行：本渠道历史最常见成因 Top N。条数与涉及金额分列。"""

    cause_code: str
    zh_name: str
    owner: str
    precedent_count: int
    hit_count: int
    amount: str
    channel: str = ""
    last_hit_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_code": self.cause_code,
            "zh_name": self.zh_name,
            "owner": self.owner,
            "precedent_count": self.precedent_count,
            "hit_count": self.hit_count,
            "amount": self.amount,
            "channel": self.channel,
            "last_hit_at": self.last_hit_at,
        }


# ─────────────────────────── 人工关口 ───────────────────────────


def assert_human_operator(operator: str) -> str:
    """操作人必须是**人**。返回清洗后的标识。

    与 :func:`reconq.align.aggregate.assert_human_operator` 同一条纪律、两个
    独立实现：先例是「人判过的」才叫先例，而模型草稿、规则基线与降级路径
    都不该有登记先例的能力。放在**入口**而不是靠每条调用路径记得调它。
    """
    text = str(operator or "").strip()
    if not text:
        raise ReconqError(
            "PRECEDENT_OPERATOR_INVALID",
            "登记先例必须给操作人（经办人 / 审核人 / 主管的工号或姓名）。"
            "先例是「人判过的」，没有人判过的先例不成立",
        )
    if text.lower() in _NON_HUMAN_OPERATORS:
        raise ReconqError(
            "PRECEDENT_OPERATOR_INVALID",
            f"操作人 {operator!r} 不是自然人标识，本产品拒绝由它登记先例："
            "模型草稿、规则基线与降级路径都不得把「算出来的成因」"
            "沉淀成「人判过的先例」",
        )
    if len(text) > 64:
        raise ReconqError(
            "PRECEDENT_OPERATOR_INVALID",
            f"操作人标识过长（{len(text)} 字符）；先例表按 64 字符存",
        )
    return text


# ─────────────────────────── 登记（回流） ───────────────────────────


_ROW_TO_PRECEDENT = """
SELECT * FROM {table} WHERE signature=? AND cause_code=?
"""


def _row_to_precedent(row: sqlite3.Row) -> Precedent:
    try:
        features = json.loads(row["features_json"] or "{}")
    except json.JSONDecodeError:  # pragma: no cover - 落库时序列化，写不出坏 JSON
        features = {}
    try:
        samples = json.loads(row["sample_keys_json"] or "[]")
    except json.JSONDecodeError:  # pragma: no cover - 同上
        samples = []
    return Precedent(
        id=int(row["id"]),
        signature=str(row["signature"]),
        cause_code=str(row["cause_code"]),
        channel=str(row["channel"]),
        ambiguity_kind=str(row["ambiguity_kind"] or ""),
        features=features if isinstance(features, dict) else {},
        hit_count=int(row["hit_count"]),
        first_hit_at=str(row["first_hit_at"] or ""),
        last_hit_at=str(row["last_hit_at"] or ""),
        sample_keys=tuple(str(s) for s in samples) if isinstance(samples, list) else (),
        dict_version=str(row["dict_version"] or ""),
        dict_digest=str(row["dict_digest"] or ""),
        prompt_version=str(row["prompt_version"] or ""),
        prompt_digest=str(row["prompt_digest"] or ""),
        signature_version=str(row["signature_version"] or PRECEDENT_SIGNATURE_VERSION),
        confirmed_by=str(row["confirmed_by"] or ""),
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def record_confirmation(
    conn: sqlite3.Connection,
    *,
    period: str,
    channel: str,
    cause_code: str,
    signature: FeatureSignature,
    disposition: str,
    operator: str,
    ambiguity_kind: str = "",
    diff_key: str = "",
    dict_version: str = "",
    dict_digest: str = "",
    prompt_version: str = "",
    prompt_digest: str = "",
    now: Optional[str] = None,
) -> PrecedentWrite:
    """把一条**人工确认**的处置结论沉淀为先例。

    :param disposition: 该差异在台账上的处置状态。**只有「已确认」才写**；
        「已驳回」与「待确认」一律不写（见模块头边界 1）。
    :param signature: :func:`feature_signature` 的产物。
    :param operator: 自然人标识。

    幂等：同一 (签名, 成因) 二次登记**不新建行**，命中次数 +1、样本键去重后
    追加。重复登记的典型场景是「同一条差异被撤回后重办」—— 那是一次新的人工
    确认，但不是两个先例。
    """
    from ..engine.period import DISPOSITION_CONFIRMED  # 局部 import：align 不反向依赖 engine

    who = assert_human_operator(operator)
    if str(disposition or "").strip() != DISPOSITION_CONFIRMED:
        raise ReconqError(
            "PRECEDENT_DISPOSITION_INVALID",
            f"差异当前处置状态是 {disposition!r}，只有「{DISPOSITION_CONFIRMED}」"
            "才沉淀先例。把经办人判为「不是这个原因」的样本存成先例，"
            "会让下一期给出恰好被否掉的建议，而这一期没人再去查证据",
        )
    # 成因当场校验：字典外的码进先例库会让检索命中一条读不出「该找谁」的建议。
    entry_of(cause_code)
    # 版本对**必须**齐全（先例日后要靠它判断该不该降权）。调用方显式给了
    # 四个值就按给的走 —— 回放旧版本时要的就是当时的版本对，而不是当前的。
    pair = current_pair()
    dict_version = dict_version or CAUSE_DICT_VERSION
    dict_digest = dict_digest or pair.cause_dict_digest
    prompt_version = prompt_version or pair.prompt_version
    prompt_digest = prompt_digest or pair.prompt_digest
    stamp = now or _now()
    init_precedent_schema(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            _ROW_TO_PRECEDENT.format(table=PRECEDENT_TABLE),
            (signature.digest, cause_code),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                f"INSERT INTO {PRECEDENT_TABLE} (signature, signature_version, "
                "cause_code, channel, ambiguity_kind, features_json, hit_count, "
                "first_hit_at, last_hit_at, sample_keys_json, dict_version, "
                "dict_digest, prompt_version, prompt_digest, confirmed_by, "
                "created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)",
                (
                    signature.digest,
                    signature.version,
                    cause_code,
                    signature.features.get("channel", channel),
                    ambiguity_kind or signature.features.get("kind", ""),
                    json.dumps(dict(signature.features), ensure_ascii=False, sort_keys=True),
                    stamp,
                    stamp,
                    json.dumps([diff_key] if diff_key else [], ensure_ascii=False),
                    dict_version,
                    dict_digest,
                    prompt_version,
                    prompt_digest,
                    who,
                    stamp,
                    stamp,
                ),
            )
            precedent_id = int(cursor.lastrowid or 0)
            created = True
            reason = "新建先例"
            hit_count = 1
        else:
            existing = _row_to_precedent(row)
            samples = list(existing.sample_keys)
            if diff_key and diff_key not in samples:
                samples.append(diff_key)
            # 截断保留**最近**的样本键：先例看板要回答「最近一次是谁判的」。
            samples = samples[-MAX_SAMPLE_KEYS:]
            hit_count = existing.hit_count + 1
            conn.execute(
                f"UPDATE {PRECEDENT_TABLE} SET hit_count=?, last_hit_at=?, "
                "sample_keys_json=?, updated_at=? WHERE id=?",
                (
                    hit_count,
                    stamp,
                    json.dumps(samples, ensure_ascii=False),
                    stamp,
                    existing.id,
                ),
            )
            precedent_id = existing.id
            created = False
            reason = "命中已有先例，命中次数 +1"
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    logger.info(
        "先例登记：%s / %s / %s（%s，命中 %d 次）",
        channel, cause_code, signature.digest, who, hit_count,
    )
    return PrecedentWrite(
        signature=signature.digest,
        cause_code=cause_code,
        created=created,
        hit_count=hit_count,
        precedent_id=precedent_id,
        reason=reason,
    )


# ─────────────────────────── 检索 ───────────────────────────


def load_precedent(conn: sqlite3.Connection, signature: str, cause_code: str) -> Optional[Precedent]:
    """取一条先例；不存在返回 ``None``（不是错：先例库本来就是空的）。"""
    init_precedent_schema(conn)
    row = conn.execute(
        _ROW_TO_PRECEDENT.format(table=PRECEDENT_TABLE), (signature, cause_code)
    ).fetchone()
    return _row_to_precedent(row) if row is not None else None


def list_precedents(
    conn: sqlite3.Connection,
    *,
    channel: str = "",
    signature: str = "",
    cause_code: str = "",
    min_hits: int = 0,
) -> list[Precedent]:
    """列先例。默认按命中次数降序、最近命中时间降序、编码升序。"""
    init_precedent_schema(conn)
    sql = f"SELECT * FROM {PRECEDENT_TABLE} WHERE 1=1"
    params: list[Any] = []
    if channel:
        sql += " AND channel=?"
        params.append(channel)
    if signature:
        sql += " AND signature=?"
        params.append(signature)
    if cause_code:
        sql += " AND cause_code=?"
        params.append(cause_code)
    if min_hits:
        sql += " AND hit_count>=?"
        params.append(min_hits)
    sql += " ORDER BY hit_count DESC, last_hit_at DESC, cause_code ASC"
    return [_row_to_precedent(row) for row in conn.execute(sql, params)]


def _authority_of(
    precedent: Precedent, *, dict_digest: str, prompt_version: str
) -> tuple[str, str]:
    """先例相对当前版本的权威度。返回 ``(authority, note)``。

    两个版本都对照：只对照字典会漏掉「字典没动、提示词改了」的情形，
    而提示词改版同样会改变「当时的人是在哪套措辞下判的」。
    """
    if precedent.signature_version != PRECEDENT_SIGNATURE_VERSION:
        return (
            "reference",
            f"先例签名算法已从 {precedent.signature_version} 变为 "
            f"{PRECEDENT_SIGNATURE_VERSION}，特征口径不再可比，仅供参考",
        )
    notes: list[str] = []
    if precedent.dict_digest and dict_digest and precedent.dict_digest != dict_digest:
        notes.append(f"成因字典已从 {precedent.dict_version or '未记录'} 变更")
    if precedent.prompt_version and prompt_version and precedent.prompt_version != prompt_version:
        notes.append(f"提示词已从 {precedent.prompt_version} 变为 {prompt_version}")
    if notes:
        return "reference", "；".join(notes) + "，本条先例降为参考"
    return "strong", ""


def suggest_for(
    conn: sqlite3.Connection,
    signature: FeatureSignature,
    *,
    min_hits: int = PRECEDENT_MIN_HITS,
    limit: int = 3,
) -> list[PrecedentSuggestion]:
    """给一条差异检索先例建议。**只给建议，不推进任何状态。**

    同一签名下多条先例按命中次数降序返回，最多 :paramref:`limit` 条。低于
    :data:`PRECEDENT_MIN_HITS` 的一律不返回：命中一次的先例与「上期那笔是个
    意外」在证据上无法区分。
    """
    rows = list_precedents(
        conn, signature=signature.digest, min_hits=max(1, int(min_hits))
    )
    if not rows:
        return []
    pair = current_pair()
    suggestions: list[PrecedentSuggestion] = []
    for row in rows[: max(0, int(limit))]:
        authority, note = _authority_of(
            row, dict_digest=pair.cause_dict_digest, prompt_version=pair.prompt_version
        )
        cap = (
            PRECEDENT_MAX_CONFIDENCE if authority == "strong" else PRECEDENT_REFERENCE_CAP
        )
        suggestions.append(
            PrecedentSuggestion(
                cause_code=row.cause_code,
                zh_name=row.zh_name,
                signature=row.signature,
                hit_count=row.hit_count,
                last_hit_at=row.last_hit_at,
                authority=authority,
                confidence=cap,
                version_note=note,
                owner=row.owner,
                sample_keys=row.sample_keys[-3:],
            )
        )
    return suggestions


def render_precedent_hint_zh(suggestions: Sequence[PrecedentSuggestion]) -> str:
    """给证据包 / 提示词用的一段先例提示（**只出提示词，不改判据**）。

    刻意写成「历史上有人这么判过」而不是「本条应当判为」—— 前者是事实，
    后者是结论，而结论只能由人点。
    """
    if not suggestions:
        return "先例库中没有同类差异的人工确认记录（确实无先例）"
    lines = ["历史先例（人工确认过的处置结论，仅供参考，不是本条的结论）："]
    for item in suggestions:
        lines.append(
            f"- {item.zh_name}（{item.cause_code}）：历史命中 {item.hit_count} 次，"
            f"最近 {item.last_hit_at or '未记录'}"
            + (f"；{item.version_note}" if item.version_note else "")
        )
    return "\n".join(lines)


# ─────────────────────────── 命中留痕 ───────────────────────────


_INSERT_HIT_SQL = f"""
INSERT INTO {PRECEDENT_HIT_TABLE} (period, channel, diff_key, signature, cause_code,
    precedent_id, hit_count_at, authority, actor, version_pair, note, recorded_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT (period, channel, diff_key, signature, cause_code) DO NOTHING
"""


def record_hit(
    conn: sqlite3.Connection,
    *,
    period: str,
    signature: FeatureSignature,
    suggestion: PrecedentSuggestion,
    operator: str,
    diff_key: str = "",
    note: str = "",
    now: Optional[str] = None,
) -> PrecedentHit:
    """记「谁在何时采信了哪条先例」。

    记的是**引用**（``precedent_id`` + 当时的 ``hit_count``）而不是把先例整份
    拷一份：先例行的命中次数会继续涨，拷一份就会与「当时记成几次」分叉。

    先例行不存在即抛 :class:`~reconq.errors.ReconqError` ``PRECEDENT_HIT_MISSING``
    —— 留痕指向一条空处，比没有留痕更难查。
    """
    from ..engine.period import DISPOSITION_CONFIRMED  # 局部 import：见上

    who = assert_human_operator(operator)
    existing = load_precedent(conn, signature.digest, suggestion.cause_code)
    if existing is None:
        raise ReconqError(
            "PRECEDENT_HIT_MISSING",
            f"先例 {suggestion.cause_code!r}（签名 {signature.digest}）在先例库里"
            "不存在，无法登记命中。先例行不应被删 —— 删了会让历史命中留痕"
            "指向空处，比没有留痕更难查",
        )
    pair = current_pair()
    stamp = now or _now()
    init_precedent_schema(conn)
    cursor = conn.execute(
        _INSERT_HIT_SQL,
        (
            period,
            signature.features.get("channel", existing.channel),
            diff_key,
            signature.digest,
            suggestion.cause_code,
            existing.id,
            existing.hit_count,
            suggestion.authority,
            who,
            pair.label(),
            note,
            stamp,
        ),
    )
    # 命中留痕**不进** scan_events 事件链的表结构，但 append_scan_event 让它
    # 与重跑 / 字典版本记在同一条链上（只追加、链式指纹）。刻意放在落库之后：
    # 先例行写不进去就不该有「采信过」的痕迹。
    from ..engine.resume import append_scan_event  # 局部 import：align 不反向依赖 engine

    # rowcount 为 0 表示这条留痕已存在（同一差异的同一条先例被点开两次）——
    # 此时**不追加链上事件**：事件链 append-only，不看 rowcount 直接追加，
    # 链上就会多一环「又采信了一次」，而面板上只看得到一条先例被采信过。
    # 链上多出来的环让「本周采信了几次先例」这个数字对不上，且删不掉。
    if cursor.rowcount > 0:
        append_scan_event(
            conn,
            period,
            "precedent.hit",
            actor=who,
            scope="precedent",
            scope_data={
                "signature": signature.digest,
                "cause_code": suggestion.cause_code,
                "precedent_id": existing.id,
                "hit_count_at": existing.hit_count,
                "authority": suggestion.authority,
                "diff_key": diff_key,
            },
            result="accepted",
            detail=(
                f"采信先例 {suggestion.zh_name}（{suggestion.cause_code}，"
                f"历史命中 {existing.hit_count} 次，权威度 {suggestion.authority}）"
            ),
        )
    logger.info(
        "先例命中：%s / %s / %s（%s，%s）",
        period, suggestion.cause_code, signature.digest, who, suggestion.authority,
    )
    return PrecedentHit(
        period=period,
        channel=signature.features.get("channel", existing.channel),
        diff_key=diff_key,
        signature=signature.digest,
        cause_code=suggestion.cause_code,
        precedent_id=existing.id,
        hit_count_at=existing.hit_count,
        authority=suggestion.authority,
        actor=who,
        version_pair=pair.label(),
        note=note,
        recorded_at=stamp,
    )


def list_hits(
    conn: sqlite3.Connection,
    period: str,
    *,
    channel: str = "",
    diff_key: str = "",
) -> list[PrecedentHit]:
    """列一条账期（或一条差异）的先例采信留痕。"""
    init_precedent_schema(conn)
    sql = f"SELECT * FROM {PRECEDENT_HIT_TABLE} WHERE period=?"
    params: list[Any] = [period]
    if channel:
        sql += " AND channel=?"
        params.append(channel)
    if diff_key:
        sql += " AND diff_key=?"
        params.append(diff_key)
    sql += " ORDER BY channel, recorded_at, id"
    return [
        PrecedentHit(
            period=row["period"],
            channel=row["channel"],
            diff_key=row["diff_key"],
            signature=row["signature"],
            cause_code=row["cause_code"],
            precedent_id=int(row["precedent_id"]),
            hit_count_at=int(row["hit_count_at"]),
            authority=row["authority"],
            actor=row["actor"],
            version_pair=row["version_pair"],
            note=row["note"],
            recorded_at=row["recorded_at"],
        )
        for row in conn.execute(sql, params)
    ]


# ─────────────────────────── 先例看板 ───────────────────────────


def top_causes(
    conn: sqlite3.Connection,
    *,
    channel: str = "",
    limit: int = 5,
) -> list[PrecedentTopRow]:
    """本渠道历史最常见成因 Top N。**条数与涉及金额分列。**

    涉及金额按先例的样本差异键**不在此处求和** —— 先例行不存金额（金额刻意
    不进签名，见模块头），所以这一列的语义是「先例覆盖的差异条数」，不是金额。
    面板上那一格要显示金额，应当走
    :func:`reconq.align.causes.aggregate_causes`（它拿的是台账金额）。

    没有先例时返回空列表而不是抛错：「今天还没人办过单」显示成一条红色
    故障是最差的排障体验。
    """
    if channel and channel not in CONTROLLED_CHANNELS:
        raise ReconqError(
            "PRECEDENT_CHANNEL_UNKNOWN",
            f"渠道 {channel!r} 不在六路受控渠道内（{sorted(CONTROLLED_CHANNELS)}）",
        )
    init_precedent_schema(conn)
    sql = (
        f"SELECT cause_code, channel, COUNT(*) AS precedent_count, "
        f"SUM(hit_count) AS hit_count, MAX(last_hit_at) AS last_hit_at "
        f"FROM {PRECEDENT_TABLE} WHERE 1=1"
    )
    params: list[Any] = []
    if channel:
        sql += " AND channel=?"
        params.append(channel)
    sql += " GROUP BY cause_code, channel ORDER BY hit_count DESC, cause_code ASC"
    rows = list(conn.execute(sql, params))
    out: list[PrecedentTopRow] = []
    for row in rows[: max(0, int(limit))]:
        entry = CAUSES.get(row["cause_code"])
        out.append(
            PrecedentTopRow(
                cause_code=row["cause_code"],
                zh_name=entry.zh_name if entry else row["cause_code"],
                owner=entry.owner if entry else "",
                precedent_count=int(row["precedent_count"]),
                hit_count=int(row["hit_count"] or 0),
                # 先例行不存金额（金额不进签名），这一列恒为空并说明原因，
                # 而不是拿命中次数冒充金额。
                amount="",
                channel=str(row["channel"] or ""),
                last_hit_at=str(row["last_hit_at"] or ""),
            )
        )
    return out
