"""面板的读侧查询层（Task 48）：筛选、按成因分组、待办聚合与决策辅助。

为什么单独一个模块，而不是把判据塞进 :mod:`reconq.web.state`
----------------------------------------------------------------
Task 47 的 :mod:`reconq.web.state` 是**三栏的原始投影**：左栏渠道卡、
中栏差异行、右栏处置单，每一栏都声称「面板上的每个数字都能在 CLI 的
同一条命令里找到同一个出处」。这一轮要加的四样东西性质不同：

* **筛选**（:func:`filter_rows`）只是把同一批行按参数裁一刀，不新增
  任何数字；
* **分组**（:func:`group_by_cause`）是把已有行按成因归堆，组内条数与
  金额都是从组员现加的，**不另存一份计数**；
* **待办聚合**（:func:`todo_summary`）回答「我这个角色该审什么」，依据是
  :mod:`reconq.ledger.models` 的 :data:`~reconq.ledger.models.required_role_for`
  —— 谁有权在哪个状态动，同一处说了算；
* **决策辅助**（:func:`decision_aid`）把四路来源**并列**摆出来：模型草稿、
  规则基线、先例、反幻觉核对结果，并明示哪一路被采用、为什么。

把「筛选判据」与「投影」混在一个文件里，会让 :mod:`reconq.web.state` 慢慢
长成第二个业务判定处 —— 那正是这个模块头 :mod:`reconq.web.app` 里写的
「两处各写一份判据的年代已经过去了」要避免的事。所以**读侧投影留在
state，查询判据住在这里**，两者只通过函数调用往来。

四路来源为什么必须并列而不是合成一个「建议成因」
----------------------------------------------
经办人在签字前要回答的问题是「**这个成因是谁说的**」，不是「成因是什么」。
模型草稿（过了反幻觉核对）、规则推断（未经模型）、历史先例（人以前这么
判过）、人工结论（人这次这么判）四者的可信度差着一档：把它们按置信度排
成一张表，读的人分不出哪一行是自己拍的板。合成一个字段更糟 —— 结论单上
「成因出处」那一栏就没法填了。

因此 :func:`decision_aid` 返回的 ``adopted`` 显式带 ``source`` 与
``reason``，且 :data:`ADOPTED_SOURCES` 是**封闭集合**：新增一条出处必须
同时说清它凭什么能被采用。``adopted_source`` 恒为 ``none`` 的情形是
「四路都没给出可采信的结论，待人工判定」—— 那是本产品最常见也最诚实的
一种状态。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Sequence

from ..errors import ReconqError

__all__ = [
    "ADOPTED_SOURCES",
    "CAUSE_GROUP_OTHER",
    "FILTERABLE_STATES",
    "RowFilter",
    "cause_code_of",
    "decision_aid",
    "filter_rows",
    "group_by_cause",
    "parse_filter",
    "todo_summary",
]


# ─────────────────────────── 筛选 ───────────────────────────


#: 差异台账里「成因未定」那一组的名字。**与 ``undetermined`` 分开**：
#: ``undetermined`` 是规则跑完了仍然判不出来（有人已经想过一遍），
#: 而口径差异是压根没有歧义形态可判（不用想）。合成一组会让「不可判定」
#: 的条数虚高，而那正是最需要人推的情形。
CAUSE_GROUP_OTHER = "_contract_diff"


#: 允许按状态筛选的取值。取 :data:`reconq.ledger.models.ALL_STATUSES`
#: 加上「未开单」—— 后者是中栏真实存在的状态（台账上有差异、处置单上
#: 还没开单），不给它单列一组就意味着**筛不出来**，而经办人恰恰最想
#: 先看这一组。
def _states() -> tuple[str, ...]:
    from ..ledger.models import ALL_STATUSES

    return ALL_STATUSES + ("未开单",)


FILTERABLE_STATES: tuple[str, ...] = _states()


class RowFilter:
    """一行差异的筛选条件。**空值 = 该维度不过滤**。

    刻意做成不可变的普通对象而不是一堆函数入参：面板上「状态=待确认 且
    渠道=wechat 且 金额≥100」这种组合会同时出现在 API、URL query 与
    导出上，一个能被 ``to_dict()`` 原样序列化回去的结构省掉三处
    「忘了把某个条件带回去」的 bug。
    """

    __slots__ = (
        "ambiguity_kind",
        "amount_max",
        "amount_min",
        "cause_code",
        "channel",
        "only_ambiguous",
        "only_weak_match",
        "state",
    )

    def __init__(
        self,
        *,
        cause_code: str = "",
        channel: str = "",
        state: str = "",
        ambiguity_kind: str = "",
        amount_min: Optional[Decimal] = None,
        amount_max: Optional[Decimal] = None,
        only_weak_match: bool = False,
        only_ambiguous: bool = False,
    ) -> None:
        self.cause_code = cause_code
        self.channel = channel
        self.state = state
        #: 歧义形态。**空串不过滤**，与「只要口径差异」不是一回事 ——
        #: 后者由 :attr:`only_ambiguous` 的反面（``ambiguity_kind=""``）
        #: 显式表达，见 :func:`parse_filter`。
        self.ambiguity_kind = ambiguity_kind
        self.amount_min = amount_min
        self.amount_max = amount_max
        self.only_weak_match = only_weak_match
        self.only_ambiguous = only_ambiguous

    @property
    def active(self) -> bool:
        """是否带了任何筛选条件。前端据此决定要不要显示「已筛选」角标。"""
        return bool(
            self.cause_code
            or self.channel
            or self.state
            or self.ambiguity_kind
            or self.amount_min is not None
            or self.amount_max is not None
            or self.only_weak_match
            or self.only_ambiguous
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_code": self.cause_code,
            "channel": self.channel,
            "state": self.state,
            "ambiguity_kind": self.ambiguity_kind,
            "amount_min": str(self.amount_min) if self.amount_min is not None else "",
            "amount_max": str(self.amount_max) if self.amount_max is not None else "",
            "only_weak_match": self.only_weak_match,
            "only_ambiguous": self.only_ambiguous,
            "active": self.active,
        }


def _parse_amount(raw: Any, *, where: str) -> Optional[Decimal]:
    """查询串里的金额 → ``Decimal``。**非法即抛**（→ 4xx），不静默忽略。

    静默忽略的后果很具体：经办人在「金额 ≥ 100」那栏输了 ``100.5元``，
    面板返回 37 条（等于没过滤），他会以为这一档本来就这么少 —— 而真相
    是筛选压根没生效。**报错比给一个假的筛选结果有用。**
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ReconqError(
            "WEB_FILTER_AMOUNT_INVALID",
            f"{where} {raw!r} 不是合法金额（应为定点数，如 100.50）；"
            f"本产品不把带单位或千分位杂物的值猜成数字",
        ) from exc


def _truthy(raw: Any) -> bool:
    """查询串里的布尔。**只认三档真值**，其余按假处理（不报错）。

    面板上这两个开关是 checkbox，发过来的是 ``1`` / ``0`` 或 ``true`` /
    ``false``。把 ``yes`` 也当真会让「筛选没生效」与「筛选生效了但结果
    一样」在排障时长得一模一样。
    """
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def parse_filter(
    *,
    cause_code: str = "",
    channel: str = "",
    state: str = "",
    ambiguity_kind: Optional[str] = None,
    amount_min: Any = None,
    amount_max: Any = None,
    only_weak_match: Any = None,
    only_ambiguous: Any = None,
) -> RowFilter:
    """把 API/URL 的入参收成 :class:`RowFilter`，**非法即抛**。

    三个取值域在这里校验，理由是它们都是**封闭词表**：

    * ``state`` 必须在 :data:`FILTERABLE_STATES` 内 —— 写错一个字
      （``待审`` 而不是 ``待确认``）会静默返回空列表，经办人以为本期
      干净了；
    * ``channel`` 必须是六路受控渠道之一；
    * ``cause_code`` 必须在受控成因字典内（:data:`reconq.align.causes.CAUSES`）。
    """
    from ..align.causes import CAUSES
    from ..sources.registry import CHANNELS

    state_text = str(state or "").strip()
    if state_text and state_text not in FILTERABLE_STATES:
        raise ReconqError(
            "WEB_FILTER_STATE_UNKNOWN",
            f"未登记的处置状态 {state_text!r}；可筛的是："
            f"{'、'.join(FILTERABLE_STATES)}。"
            f"写错状态名会静默返回空列表，看起来像「本期没有差异」",
        )
    channel_text = str(channel or "").strip()
    if channel_text and channel_text not in CHANNELS:
        raise ReconqError(
            "WEB_FILTER_CHANNEL_UNKNOWN",
            f"未登记的渠道 {channel_text!r}；六路受控渠道为：{'、'.join(CHANNELS)}",
        )
    cause_text = str(cause_code or "").strip()
    if cause_text and cause_text not in CAUSES:
        raise ReconqError(
            "WEB_FILTER_CAUSE_UNKNOWN",
            f"未登记的成因码 {cause_text!r}；可筛的是："
            f"{'、'.join(sorted(CAUSES))}。"
            f"成因码写错会让分组数字对不上台账总数",
        )
    low = _parse_amount(amount_min, where="amount_min")
    high = _parse_amount(amount_max, where="amount_max")
    if low is not None and high is not None and low > high:
        raise ReconqError(
            "WEB_FILTER_AMOUNT_INVALID",
            f"金额区间反了：下限 {low} > 上限 {high}。"
            f"这种条件永远筛不出东西，面板不该把空列表当成「本期没有差异」",
        )
    return RowFilter(
        cause_code=cause_text,
        channel=channel_text,
        state=state_text,
        # ``None`` = 不过滤；``""`` = 只要口径差异。两者不是一回事。
        ambiguity_kind="" if ambiguity_kind is None else str(ambiguity_kind).strip(),
        amount_min=low,
        amount_max=high,
        only_weak_match=_truthy(only_weak_match),
        only_ambiguous=_truthy(only_ambiguous),
    )


def _amount_of(row: dict[str, Any]) -> Optional[Decimal]:
    """行差额 → ``Decimal``；**不可解析时返回 None 而不是 0**。

    差「不可解析」与「等于 0」在面板上是两件事：前者是上游给了脏值
    （该隔离区里躺着），后者是真的一分不差。把它当 0 会让
    「金额 ≥ 100」那档把一条脏行算进来，而它的金额根本不可信。
    """
    raw = row.get("diff_amount")
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


def filter_rows(rows: Sequence[dict[str, Any]], flt: RowFilter) -> list[dict[str, Any]]:
    """按 :class:`RowFilter` 裁行。**各条件之间是「且」。**

    刻意不在这里算合计：合计的算法只有 :func:`reconq.web.state._sum_amounts`
    一处（它还带着「有非数值差额就返回不完整」那条纪律），在这里另写一遍
    就会出现「筛选后合计 0.00」与「筛选后合计不完整」两种口径。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        # 成因走 :func:`cause_code_of`（三档取最强的一档）而不是读行上
        # 某个 ``cause_code`` 字段：台账行给的是**三档分列**
        # （``ledger_cause`` / ``model_cause`` / ``rule_cause``），哪一档
        # 作数由 :func:`cause_code_of` 说了算。直接读一个并不存在的
        # ``cause_code`` 字段不会报错，只会让这个筛选**永远返回 0 条** ——
        # 而「筛不出东西」与「本期没有这类成因」在面板上长得一模一样。
        if flt.cause_code and cause_code_of(row) != flt.cause_code:
            continue
        if flt.channel and row.get("channel") != flt.channel:
            continue
        if flt.state and row.get("ledger_status") != flt.state:
            continue
        if flt.ambiguity_kind and row.get("ambiguity_kind") != flt.ambiguity_kind:
            continue
        if flt.amount_min is not None or flt.amount_max is not None:
            amount = _amount_of(row)
            # 金额不可解析的行**被排除**并在返回里说明条数 —— 见
            # :func:`filter_rows_with_report`。
            if amount is None:
                continue
            if flt.amount_min is not None and amount < flt.amount_min:
                continue
            if flt.amount_max is not None and amount > flt.amount_max:
                continue
        if flt.only_weak_match and not _is_weak(row):
            continue
        if flt.only_ambiguous and not row.get("ambiguity_kind"):
            continue
        out.append(row)
    return out


def _is_weak(row: dict[str, Any]) -> bool:
    """这条是不是弱匹配（只靠金额 + 时间窗找到候选，缺 HIS 单号）。

    判据是**形态**，两个来源都认：台账登记的 ``ambiguity_kind`` 列
    （歧义类落台账时写的就是它）与 ``check_path`` 前缀
    :data:`align.weak_pending`。

    只看前缀会漏掉一整类：歧义结论共用 ``taxonomy.ambiguity:`` 前缀，
    ``weak_pending`` 记在 ``ambiguity_kind`` 列上（见
    :func:`reconq.align.taxonomy.verdict_to_diff_items`）。只认前缀的话
    经办人点「只看弱匹配」会一条都看不到 —— 而弱匹配恰恰是**最该人工
    看**的一档（模型信心最低）。

    反过来「看 message 里有没有『弱』字」是绝不能做的：措辞一改，筛选
    就跟着静默失效，而失效的方向恰好是「该给人看的没给人看」。
    """
    if str(row.get("ambiguity_kind") or "") == "weak_pending":
        return True
    return str(row.get("check_path") or "").startswith("align.weak_pending")


def filter_rows_with_report(
    rows: Sequence[dict[str, Any]], flt: RowFilter
) -> dict[str, Any]:
    """:func:`filter_rows` + 「因金额不可解析被排除」的条数。

    单独返回这个计数是因为它对应一个真实的运维动作：被排除的那些行在
    上游是脏数据，得去隔离区捞。**不给计数，被排除的条数就消失了** ——
    筛出 30 条而台账上有 33 条，经办人无从知道少的 3 条去了哪。

    计数口径是**只被金额条件排除**：先按其余条件滤一遍，再数其中
    金额不可用的条数。若直接「总数 − 命中数」或「在未命中的行里
    数金额为空的」，那么一条支付宝的脏行在筛微信时也会被算进去 ——
    那条行压根不是因为金额脏被排除的，混进计数只会让隔离区的条数
    对不上，而那正是这个数字存在的唯一理由。

    「金额不可用」含**两种**成因，计数不区分：上游压根没给金额
    （如「HIS 未落单」这类只说明查不到行的差异）与给了但不可解析
    （脏值）。两者对「按金额筛一档」这件事的后果是一样的 —— 都进不来。
    """
    # ``amount_missing`` / ``amount_unparsable`` 分列是因为**下一步动作
    # 不同**：前者去问渠道方要金额，后者去隔离区捞原文。面板只显示合计
    # （避免给经办人两种颜色的告警），但分开计数，排障时能直接答。
    kept = filter_rows(rows, flt)
    missing = 0
    unparsable = 0
    if flt.amount_min is not None or flt.amount_max is not None:
        without_amount = RowFilter(
            cause_code=flt.cause_code,
            channel=flt.channel,
            state=flt.state,
            ambiguity_kind=flt.ambiguity_kind,
            only_weak_match=flt.only_weak_match,
            only_ambiguous=flt.only_ambiguous,
        )
        for row in filter_rows(rows, without_amount):
            if _amount_of(row) is not None:
                continue
            if row.get("diff_amount") in (None, ""):
                missing += 1
            else:
                unparsable += 1
    return {
        "rows": kept,
        "matched": len(kept),
        "total": len(rows),
        "dropped_unknown_amount": missing + unparsable,
        "dropped_amount_missing": missing,
        "dropped_amount_unparsable": unparsable,
    }


# ─────────────────────────── 成因与分组 ───────────────────────────


def cause_code_of(row: dict[str, Any]) -> str:
    """一行的成因码。**取不到就是空串，不猜。**

    三个来源按可信度倒序：

    1. 处置单上**人工确认过的**成因（``ledger_cause``）—— 人拍的板；
    2. 模型草稿 Top-1（``model_draft``）—— 过了反幻觉核对但仍是草稿；
    3. 规则形态映射（``rule_shape``）—— 未经模型。

    口径差异（没有歧义形态、也没有草稿）返回空串，由
    :func:`group_by_cause` 归到 :data:`CAUSE_GROUP_OTHER`。**不落
    ``undetermined``**：那会把「不用想」与「想了判不出」混成同一组。
    """
    for field in ("ledger_cause", "model_cause", "rule_cause"):
        code = str(row.get(field) or "")
        if code:
            return code
    return ""


def _cause_source_of(row: dict[str, Any]) -> str:
    if row.get("ledger_cause"):
        return "ledger_cause"
    if row.get("model_cause"):
        return "model_draft"
    if row.get("rule_cause"):
        return "rule_shape"
    return CAUSE_GROUP_OTHER


def group_by_cause(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按成因分组。**组内条数与金额从组员现加，不另存计数。**

    组键复用 :func:`reconq.align.aggregate.group_key_for` 的形状
    （成因::渠道::形态::金额桶）—— 面板上的组键与 CLI
    ``reconq groups`` 打出来的组键是同一个，财务科把面板上的键粘进
    工单，工单里查得到同一组。
    """
    from ..align.aggregate import group_key_for
    from ..align.causes import CAUSES

    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        code = cause_code_of(row)
        source = _cause_source_of(row)
        channel = str(row.get("channel") or "-")
        kind = str(row.get("ambiguity_kind") or "-")
        amount = _amount_of(row)
        bucket = f"{amount:.2f}" if amount is not None else "-"
        key = group_key_for(code or "-", channel, kind, bucket)
        group = buckets.get(key)
        if group is None:
            entry = CAUSES.get(code)
            group = {
                "group_key": key,
                "cause_code": code,
                "cause_zh": entry.zh_name if entry else ("口径差异" if not code else code),
                "owner": entry.owner if entry else "",
                "suggested_action": entry.suggested_action if entry else "",
                "source": source,
                "source_zh": SOURCE_ZH.get(source, source),
                "channel": channel,
                "ambiguity_kind": kind if kind != "-" else "",
                "amount_bucket": bucket,
                "count": 0,
                "amount": Decimal("0"),
                "amount_known": True,
                "pending": 0,
                "member_keys": [],
            }
            buckets[key] = group
            order.append(key)
        group["count"] += 1
        if amount is None:
            group["amount_known"] = False
        else:
            group["amount"] += amount
        if row.get("ledger_status") in ("待归因", "待确认", "待主管复核", "未开单"):
            group["pending"] += 1
        group["member_keys"].append(str(row.get("key") or ""))

    out: list[dict[str, Any]] = []
    for key in order:
        group = buckets[key]
        group["amount"] = f"{group['amount']:.2f}" if group["amount_known"] else ""
        # 组内条数多的在前；同数按组键字典序，保证两次渲染行序不跳。
        out.append(group)
    out.sort(key=lambda g: (-g["count"], g["group_key"]))
    return out


#: 成因出处 → 中文。与 :data:`reconq.align.pipeline.CAUSE_SOURCE_ZH` 同义
#: 但**各自定义**：那份说的是「这条差异的成因最终是谁定的」，而这里
#: 还要多两档只在面板上出现的（``ledger_cause`` 人工确认、
#: ``rule_shape`` 规则形态映射），共用一份常量会让面板为了加一档去改
#: 归因层的字典。
SOURCE_ZH: dict[str, str] = {
    "ledger_cause": "人工已确认（经办/审核签过）",
    "model_draft": "模型草稿（已过反幻觉核对，仍是草稿）",
    "rule_shape": "规则推断(未经模型)",
    CAUSE_GROUP_OTHER: "口径差异（无歧义形态可判）",
}


# ─────────────────────────── 待办聚合 ───────────────────────────


def todo_summary(
    conn: sqlite3.Connection, period: str, role: str
) -> dict[str, Any]:
    """某角色当前该办什么：按状态分的条数、金额与最老一条的等待时长。

    「谁在哪个状态有权动」直接问
    :func:`reconq.ledger.models.required_role_for` —— 它已经是白名单
    推导的**唯一真源**（按钮禁用态用的是同一个函数）。在面板上再写一遍
    ``if role == "handler" and status == "待归因"`` 意味着状态机改了
    而待办数字没跟上：经办人看到「我这儿 0 条」，而面板其实给他开着
    提交按钮。

    **已确认 / 已驳回不算待办**：它们是活单（可撤回）但已经办完。
    把它们算进待办会让「我这儿还有 12 条」与「点进去全是已确认」同时
    成立，而那正是「清台账永远清不空」的由来。
    """
    from ..ledger.flow import _role_value, list_discrepancies
    from ..ledger.models import (
        ACTION_SOURCES,
        STATUS_ALLOWED,
        Role,
        required_role_for,
    )

    role_value = _role_value(role)
    items = list_discrepancies(conn, period=period)

    by_status: dict[str, int] = {}
    amount_by_status: dict[str, Decimal] = {}
    total_amount = Decimal("0")
    amount_known = True
    oldest_hours: Optional[float] = None
    oldest_key = ""
    mine = 0

    for item in items:
        if item.status not in STATUS_ALLOWED:
            continue
        # 该状态下**这个人**有没有任何一个动作可做。有才进他的待办。
        if not any(
            role_value in required_role_for(item.status, action)
            for action in ACTION_SOURCES
        ):
            continue
        mine += 1
        by_status[item.status] = by_status.get(item.status, 0) + 1
        if item.amount_known:
            total_amount += item.diff_amount
        else:
            amount_known = False
        age = _age_hours(item.updated_at or item.created_at)
        if age is not None and (oldest_hours is None or age > oldest_hours):
            oldest_hours = age
            oldest_key = item.key

    return {
        "period": period,
        "role": role_value,
        "role_zh": Role(role_value).zh,
        "count": mine,
        "by_status": by_status,
        "amount": f"{total_amount:.2f}" if amount_known else "",
        "amount_known": amount_known,
        "oldest_hours": round(oldest_hours, 1) if oldest_hours is not None else None,
        "oldest_key": oldest_key,
        "oldest_text": _age_text(oldest_hours),
    }


def _parse_moment(raw: str) -> Optional[Any]:
    if not raw:
        return None
    from datetime import datetime

    text = str(raw).strip()
    for fmt in (None, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _age_hours(raw: str) -> Optional[float]:
    """一条记录有多旧（小时）。**解析不了返回 None 而不是 0。**

    返回 0 会让「最老待办 0 小时」显示成「刚刚」，而真相是那一刻钟没记
    上、时间不可知。不可知就明说不可知。
    """
    from datetime import datetime, timezone

    moment = _parse_moment(raw)
    if moment is None:
        return None
    now = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (now - moment).total_seconds() / 3600.0)


def _age_text(hours: Optional[float]) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{int(hours * 60)} 分钟"
    if hours < 48:
        return f"{hours:.0f} 小时"
    return f"{hours / 24:.1f} 天"


# ─────────────────────────── 右栏：决策辅助 ───────────────────────────


#: 成因出处的封闭集合。新增一档必须同时说清它凭什么能被采用 ——
#: ``adopted`` 那一栏直接印在结论单上。
ADOPTED_SOURCES: tuple[str, ...] = (
    "human_conclusion",
    "model_draft",
    "rule_shape",
    "precedent",
    "none",
)


def decision_aid(
    conn: sqlite3.Connection, period: str, row: dict[str, Any]
) -> dict[str, Any]:
    """右栏「决策辅助」：四路来源并列 + 明示哪一路被采用、为什么。

    四路各自读各自的表，**不合成一个字段**：

    * :func:`_model_draft` —— 模型草稿（带 ``confidence`` 与
      ``evidence_refs``），出 :mod:`reconq.store.attribution_store`；
    * :func:`_rule_shape` —— 规则基线形态映射（标「规则推断(未经模型)」），
      出 :mod:`reconq.align.baseline`；
    * :func:`_precedent` —— 历史先例（标 ``precedent_hit`` 与命中次数），
      出 :mod:`reconq.align.precedent`；
    * :func:`_verification` —— 反幻觉核对结果（mismatch 列表），出
      :mod:`reconq.align.verify_draft` 的核对项词表。

    ``adopted`` 的取值由 :data:`ADOPTED_SOURCES` 约束，且**永远带
    ``reason``**：面板上要能回答「凭什么采了它」，而不只是「采了它」。
    """
    key = str(row.get("key") or "")
    model = _model_draft(conn, period, row)
    rule = _rule_shape(row)
    precedent = _precedent(conn, period, row)
    verification = _verification(row)
    human = str(row.get("conclusion") or "").strip()
    status = str(row.get("ledger_status") or "")

    adopted, reason = _pick_adopted(
        human=human,
        status=status,
        model=model,
        rule=rule,
        verification=verification,
    )
    return {
        "key": key,
        "model_draft": model,
        "rule_baseline": rule,
        "precedent": precedent,
        "verification": verification,
        "adopted": {
            "source": adopted,
            "source_zh": ADOPTED_ZH.get(adopted, adopted),
            "cause_code": cause_code_of(row),
            "reason": reason,
            "note": human,
        },
        "sources_zh": dict(SOURCE_ZH),
    }


ADOPTED_ZH: dict[str, str] = {
    "human_conclusion": "采用人工结论（经办已签字）",
    "model_draft": "采用模型草稿 Top-1（已过反幻觉核对，仍需人工确认）",
    "rule_shape": "采用规则推断 Top-1（未经模型，置信度封顶 0.5）",
    "precedent": "采用历史先例（人以前这么判过，仅供参考）",
    "none": "无可采信的结论，待人工判定",
}


def _pick_adopted(
    *,
    human: str,
    status: str,
    model: Optional[dict[str, Any]],
    rule: Optional[dict[str, Any]],
    verification: dict[str, Any],
) -> tuple[str, str]:
    """哪一路被采用、为什么。**顺序即优先级**，不按置信度排。

    人工结论排第一且不看状态 —— 人签过的话就在那儿摆着，没人会去
    「因为模型不同意所以改回人的结论」。
    """
    if human:
        return (
            "human_conclusion",
            f"经办已写下处置结论（当前状态「{status}」）；模型与规则只提供参考，"
            f"不覆盖人的判断",
        )
    if model and verification.get("consistent"):
        return (
            "model_draft",
            f"模型草稿 Top-1「{model.get('cause_zh') or model.get('cause')}」"
            f"置信度 {model.get('confidence')}，已过反幻觉核对；"
            f"仍只是草稿，未经任何人确认",
        )
    if model and not verification.get("consistent", True):
        return (
            "rule_shape" if rule else "none",
            f"模型草稿与所引证据行对不上（{verification.get('summary')}），"
            f"整条作废；"
            + (f"改看规则推断「{rule.get('zh_name')}」（未经模型）" if rule else "无规则候选可看")
            + "，待人工判定",
        )
    if rule:
        return (
            "rule_shape",
            f"本期没有模型草稿（模型未配置或已降级）；"
            f"规则推断「{rule.get('zh_name')}」是未经模型的一档，置信度封顶 0.5，待人工判定",
        )
    return ("none", "四路都没有可采信的结论；这条差异必须由人直接判")


def _model_draft(
    conn: sqlite3.Connection, period: str, row: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """模型草稿 Top-1。表没有就返回 ``None``（**不是空 dict**）。

    空 dict 与 ``None`` 在面板上会渲染成同一个「无」，而它们对应两种
    完全不同的情况：压根没跑过归因（该去查模型配置）与跑了但一条都没
    留下（该去查拒因）。
    """
    from ..store.attribution_store import list_drafts

    draft = row.get("cause_draft")
    if not draft:
        return None
    conclusion_key = str(row.get("conclusion_key") or "")
    recorded = ""
    if conclusion_key:
        try:
            found = list_drafts(
                conn, period, channel=str(row.get("channel") or ""), conclusion_key=conclusion_key
            )
        except Exception:  # noqa: BLE001 - 草稿表不存在时面板不该整页崩
            found = []
        if found:
            recorded = str(found[0].get("recorded_at") or "")
    return {
        "cause": draft.get("cause"),
        "cause_zh": draft.get("cause_zh") or draft.get("cause"),
        "confidence": draft.get("confidence"),
        "evidence_refs": list(draft.get("evidence_refs") or []),
        "explanation_zh": draft.get("explanation_zh") or "",
        "model_name": draft.get("model_name") or "",
        "recorded_at": recorded,
        "label_zh": "模型草稿（未经人工确认）",
    }


def _rule_shape(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """规则基线的形态映射档。**刻意不重跑完整基线**（见
    :func:`reconq.align.baseline.shape_cause` 的说明）。"""
    from ..align.baseline import shape_cause

    kind = _kind_of(row)
    if not kind:
        return None
    return shape_cause(kind)


def _kind_of(row: dict[str, Any]) -> str:
    """这一行属于哪种对齐形态。**先认台账登记的 ``ambiguity_kind``。**

    为什么不能只从 ``check_path`` 前缀反查：``taxonomy.ambiguity:`` 是
    **所有**歧义结论共用的前缀（见
    :func:`reconq.align.taxonomy.verdict_to_diff_items`，单号之后不再带
    形态），具体形态（``weak_pending`` / ``channel_only`` / …）记在台账
    那一列上。只按前缀反查的话，每一条歧义差异都会被认成通用的
    ``ambiguous``，而它不在 :data:`reconq.align.baseline._KIND_TO_CAUSE`
    里 —— 于是规则基线返回 ``None``，面板把它归到「口径差异」组。
    症状是：分型做得再细，面板上的分组永远是一坨。

    ``ambiguity_kind`` 空时才退回前缀反查（老库或口径差异类条目那一列
    本来就是空的）。反查取**最长匹配**而不是第一个命中：``taxonomy.ambiguity``
    与 ``align.his_only`` 互不为前缀，但将来若有人登记了一个
    ``align.weak`` 之类的短前缀，先命中它就会把长前缀的形态认错。
    """
    from ..align.bundle import KIND_CHECK_PATH_PREFIX

    registered = str(row.get("ambiguity_kind") or "").strip()
    if registered:
        return registered

    path = str(row.get("check_path") or "")
    best: tuple[int, str] = (0, "")
    for kind, prefix in KIND_CHECK_PATH_PREFIX.items():
        if path.startswith(prefix + ":") and len(prefix) > best[0]:
            best = (len(prefix), kind)
    return best[1]


def _precedent(
    conn: sqlite3.Connection, period: str, row: dict[str, Any]
) -> dict[str, Any]:
    """历史先例。**只报「有没有命中过、命中几次」，不替人下结论。**

    先例要按特征签名检索，而签名要证据包 —— 面板手上没有证据包（见
    :func:`_rule_shape` 的同一条理由）。所以这里退一步读
    :mod:`reconq.align.precedent` 的**命中留痕表**：那条差异历史上被
    采信过哪条先例、当时命中几次，是真事实；「本次该不该照着判」不是。
    """
    from ..align.causes import CAUSES
    from ..align.precedent import list_hits

    key = str(row.get("key") or "")
    hits: list[dict[str, Any]] = []
    try:
        for hit in list_hits(conn, period, channel=str(row.get("channel") or ""), diff_key=key):
            entry = CAUSES.get(hit.cause_code)
            hits.append(
                {
                    "cause_code": hit.cause_code,
                    "cause_zh": entry.zh_name if entry else hit.cause_code,
                    "hit_count_at": hit.hit_count_at,
                    "authority": hit.authority,
                    "actor": hit.actor,
                    "recorded_at": hit.recorded_at,
                    "note": hit.note,
                }
            )
    except Exception:  # noqa: BLE001 - 先例表不存在时面板显示「无先例」即可
        hits = []
    return {
        "precedent_hit": bool(hits),
        "hit_count": sum(h["hit_count_at"] for h in hits),
        "hits": hits,
        "label_zh": "历史先例（人工确认过，仅供参考）",
        "note": (
            "本条差异历史上采信过先例，面板只显示命中记录，不据此建议处置"
            if hits
            else "本条差异没有先例采信记录；**这不等于「历史上从未发生过」**"
        ),
    }


def _verification(row: dict[str, Any]) -> dict[str, Any]:
    """反幻觉核对结果。**用已落库的拒因，而不是重跑核对。**

    核对要证据包（:func:`reconq.align.verify_draft.verify_draft` 的入参
    之一），面板没有；而 :mod:`reconq.store.attribution_store` 的拒因表
    记的正是核对不过的项。两者同源，不必重跑。
    """
    from ..align.verify_draft import MISMATCH_ZH, REJECT_CODE

    rejections = list(row.get("rejections") or [])
    mismatches = [
        {
            "code": str(r.get("code") or ""),
            "kind_zh": MISMATCH_ZH.get(str(r.get("code") or ""), ""),
            "layer": str(r.get("layer") or ""),
            "detail": str(r.get("detail") or ""),
        }
        for r in rejections
        if str(r.get("code") or "") == REJECT_CODE
        or str(r.get("layer") or "") in ("evidence_ref", "cause_vocab", "schema")
    ]
    consistent = not mismatches
    kinds = sorted({m["kind_zh"] for m in mismatches if m["kind_zh"]})
    return {
        "consistent": consistent,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "summary": "、".join(kinds) if kinds else (f"{len(rejections)} 条拒因" if rejections else "无"),
        "label_zh": "反幻觉核对（草稿与所引证据行是否对得上）",
        "note": (
            "本条草稿通过了落库前的结构与证据引用校验"
            if consistent and not rejections
            else "草稿与所引证据行对不上，整条作废，不进入经办确认流"
        ),
    }
