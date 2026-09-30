"""同根因聚合与处置单合并（Task 36）。

为什么要做这一层
----------------
单边账台账是**逐条**落的（Task 25 的 :func:`~reconq.align.taxonomy.
verdict_to_diff_items` 一条结论进一行），这对追溯是对的：每条差异都能回到
源文件第几行。但它对**干活**是错的 —— 一周三张交办单，每张写着
「渠道延迟入账、门诊挂号 50.00 元、8 笔」，经办人要在这张单上点 8 次
确认。点漏一条，点过的那条还挂在「待确认」上，主管复核时看到的是
「8 条里有 1 条没处理」，而他其实想看的是「这张单处理了没有」。

两种粒度各有各的用处，因此都要有：

* **逐条粒度**（台账、快照、证据行号）—— 回答「这笔钱为什么对不上」
* **按单粒度**（处置单）—— 回答「这件事我办没办」

本模块把后者建出来，并且**刻意不让它成为前者的替代**。

分组键
------
``(cause_code, channel, ambiguity_kind, 金额分桶)``。前三项决定「同一件
事」，第四项决定「同一笔数」。

金额分桶默认 **0.01 元精确分桶**（:data:`DEFAULT_AMOUNT_BUCKET`），
理由不是「凑数」而是「一张交办单上的金额得能对得上账」：把 50.00 的
门诊挂号和 320.00 的检验费并进一张单，收费处退其中一笔时单上的合计就
不再等于实际，签了字的结论单会挂着一笔查不出来的差额。

至于「20 条差异收成 3 张单」在精确分桶下怎么成立 —— 靠的是医院账单里
**固定价目高度重复**这件事：门诊挂号 50.00、抽血 20.00、胸部 CT 220.00
这类金额在同一周里出现成十次。同因同渠道同形态同金额的重复项，本来就是
同一件事在账上落了 N 笔，而不是 N 件事。金额全不相同时分组就多，这是
精确分桶的**诚实代价**，不是缺陷：宁可给 20 张单，也不给 1 张金额对不
上的单。桶宽可调（``amount_bucket_width``），要按「同类项」归并时由
调用方显式给一个更宽的值，而不是产品替财务科猜。

打散（不聚合）的三种情形
------------------------
聚合是**有损**的：合成一张单之后，组里的成员就不再能被单独操作了。
所以下面三种情形一律打散，宁可多给几张单也不给一张含混的单：

1. **组内处置状态不一致** —— 已有 1 条确认、19 条待确认时，这张单
   不是「可确认的」，经办人点确认会 1 条成功 18 条失败。合成一张单
   只会让人以为自己还有 19 件事没做。
2. **成因未登记受控字典**（:mod:`reconq.align.causes` 里没有这个码）
   —— 绕过字典的成因没有可读的 ``zh_name`` / ``suggested_action``，
   交办单上写不出「该让谁去查」，这种单子交出去也没人认领。
3. **成员自报身份与台账坐标不符** —— 键（``账期|渠道|口径``）与它自己的
   字段拼不出来，或口径里的单号与自报单号不同。组级确认正是靠 key 回到
   台账上定位那一行的，这种条目**拒收、不成单**：给它一张单只会在面板上
   放一个点了必然失败的按钮。

组级处置
--------
:func:`apply_group_action` 是**唯一**能推进 ``period_diff_items.disposition``
的组级入口，纪律有三条：

* **只接受人工操作人**：:func:`assert_human_operator` 会拒绝
  ``model`` / ``rule`` / ``baseline`` / ``系统`` / 空值。与 Task 30
  「降级路径不得推进状态」同一条线，落到类型上。
* **整组要么全动要么全不动**：逐条先做白名单预检，任一条非法则整组
  不动，并把**逐条结果**返回（哪条不合法、当前是什么状态、能去哪）。
  部分成功是最坏的一种结果：经办人以为整单办完了。
* **落动作留痕**：:data:`DISPOSITION_ACTION_TABLE` 只追加，记操作人、
  时刻、组键、逐条前后状态。撤回整组也留一条 —— 结论单上要查得到
  「这单是撤回后重办过的」。

**组是派生的，不是存的**。组键由台账 + 归因草稿现算，不落一张
「处置单表」：成因随时可能变（重跑归因后 Top-1 从「渠道延迟入账」变成
「HIS 未落单」），存下来的组会变成一份没人更新的旧名单。落库的只有
**动作**（谁在什么时候确认了哪一组的哪些条目）。

与快照的关系
------------
聚合**不丢**逐条可回看性：:func:`expand_group` 展开后每条成员仍带
自己的证据行定位（``文件名:行号``）与结论键，:func:`conclusion_keys`
给出的键可直接喂给 :mod:`reconq.align.snapshot` 复算。合的是「操作」，
不是「证据」。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

from ..errors import ReconqError
from .causes import CAUSES

__all__ = [
    "AGGREGATE_VERSION",
    "DEFAULT_AMOUNT_BUCKET",
    "DIFF_KEY_SEP",
    "GROUP_ACTION_TABLE",
    "GROUP_ACTION_TABLE_DDL",
    "GROUP_ACTION_INDEXES",
    "AggregationResult",
    "AggregateRejection",
    "DispositionGroup",
    "DiscrepancyEntry",
    "EvidenceRow",
    "GroupAction",
    "GroupActionOutcome",
    "GroupSplit",
    "MemberOutcome",
    "action_targets",
    "aggregate_discrepancies",
    "amount_bucket",
    "apply_group_action",
    "assert_human_operator",
    "confirm_group",
    "entry_from_diff_item",
    "expand_group",
    "group_key_for",
    "init_disposition_action_schema",
    "list_group_actions",
    "load_group_entries",
    "reject_group",
    "render_group_slip_markdown",
    "render_group_zh",
    "render_summary_zh",
    "withdraw_group",
]

logger = logging.getLogger("reconq.align.aggregate")

#: 聚合逻辑版本号。与 :data:`reconq.align.snapshot.ALIGN_INDEX_VERSION`
#: 同理：分组规则改了、同样数据分出不同的单，结论单上要能说清是哪一版
#: 规则分的，否则「上周 3 张单这周 5 张」会变成查不出原因的玄学。
AGGREGATE_VERSION = "same-cause/1"

#: 差异台账行的业务键分隔符。取 ``|`` 而非 ``:`` 是因为渠道与口径里
#: 天然带冒号（``taxonomy.ambiguity:EX-WX-0001``）。
DIFF_KEY_SEP = "|"

#: 金额分桶默认宽度。0.01 元 = 精确到分 = **不同金额绝不并进同一张单**。
#: 见模块开头的理由段；要放宽必须由调用方显式传 ``amount_bucket_width``。
DEFAULT_AMOUNT_BUCKET = Decimal("0.01")

#: 无金额条目的桶标签。与 :data:`causes.CauseBucket` 的处理一致：没有金额
#: 不是「金额 0」，把它当 0 会计进合计里。
NO_AMOUNT_BUCKET = "-"

#: 差异台账 ``check_path`` 里的歧义前缀（Task 25 写死在这里）。
_AMBIGUITY_PREFIX = "taxonomy.ambiguity:"

#: 操作人不像「人」时的拒绝名单。**不**试图判断姓名格式（医院里工号、
#: 姓名拼音、代号都有人用），只挡**确定不是人**的取值。
_NON_HUMAN_OPERATORS = frozenset(
    {
        "",
        "-",
        "auto",
        "bot",
        "model",
        "llm",
        "gateway",
        "rule",
        "rules",
        "baseline",
        "system",
        "scheduler",
        "cron",
        "orchestrator",
        "attribution",
        "系统",
        "自动",
        "模型",
        "规则",
        "网关",
        "基线",
        "编排",
        "定时",
    }
)


# ─────────────────────────── 数据结构 ───────────────────────────


class GroupAction(str, Enum):
    """组级处置动作。值即 :data:`DISPOSITION_ACTION_TABLE.action` 的取值。"""

    CONFIRM = "confirm"
    REJECT = "reject"
    WITHDRAW = "withdraw"

    @property
    def zh(self) -> str:
        return ACTION_ZH[self]


#: 动作 → 中文。面板按钮与 CLI 提示共用一份，不各写一个。
ACTION_ZH: dict[str, str] = {
    GroupAction.CONFIRM.value: "确认",
    GroupAction.REJECT.value: "驳回",
    GroupAction.WITHDRAW.value: "撤回",
}

#: 动作 → 目标处置状态。**单一真源**：白名单、按钮禁用态、预检全从这张
#: 表推出来，三处各写一遍 `if action == ...` 早晚会漂。
ACTION_TARGETS: dict[str, str] = {
    GroupAction.CONFIRM.value: "已确认",
    GroupAction.REJECT.value: "已驳回",
    GroupAction.WITHDRAW.value: "待确认",
}

#: 合法来源状态。确认/驳回只从「待确认」出发，撤回从两个终态出发 ——
#: 「已确认 → 已驳回」这种直跳被禁：一条单子若从「确认」直接翻成「驳回」，
#: 结论单上会看不出中间发生过一次改判，而改判往往正是有人漏看了证据。
ACTION_SOURCES: dict[str, frozenset[str]] = {
    GroupAction.CONFIRM.value: frozenset({"待确认"}),
    GroupAction.REJECT.value: frozenset({"待确认"}),
    GroupAction.WITHDRAW.value: frozenset({"已确认", "已驳回"}),
}


def action_targets(action: str) -> dict[str, Any]:
    """某个动作的合法来源、目标落点。UI 拿它算按钮禁用态。"""
    key = _action_value(action)
    return {
        "action": key,
        "zh": ACTION_ZH[key],
        "sources": sorted(ACTION_SOURCES[key]),
        "target": ACTION_TARGETS[key],
    }


def _action_value(action: Any) -> str:
    raw = action.value if isinstance(action, GroupAction) else str(action or "")
    if raw not in ACTION_TARGETS:
        raise ReconqError(
            "GROUP_ACTION_UNKNOWN",
            f"未登记的组级处置动作 {raw!r}；本产品只有三种："
            f"{'、'.join(f'{k}（{v}）' for k, v in ACTION_ZH.items())}",
        )
    return raw


@dataclass(frozen=True)
class EvidenceRow:
    """一条证据定位。**只要能回查的那几样，不要原文**。

    与 :class:`reconq.engine.common.Evidence` 的差别是这里不保留
    ``raw_line``：交办单是发给信息科 / 渠道方的，整行原文里带着渠道自定义
    列（可能含患者信息），不该随单流转。要看原文时按 ``文件名:行号``
    回原文件。
    """

    source_file: str
    row_no: int
    field_name: str = ""
    value: str = ""

    @property
    def ref(self) -> str:
        """``文件名:行号``，与 :attr:`reconq.align.snapshot.SnapshotRef.ref` 同格式。

        行号为 0 表示「这条引用压根没带行号」（模型给了个文件名就完事），
        此时原样返回文件名而不是补成 ``文件:0`` —— ``:0`` 会被读的人
        当成「第 0 行」去源文件里找，而那里没有第 0 行。
        """
        if not self.source_file:
            return f"#{self.row_no}"
        return f"{self.source_file}:{self.row_no}" if self.row_no else self.source_file

    @classmethod
    def parse_ref(cls, ref: str, *, field_name: str = "", value: str = "") -> "EvidenceRow":
        """从 ``文件名:行号`` 解析。解析不出行号时行号记 0 并保留整个串。"""
        text = str(ref or "").strip()
        if ":" in text:
            head, _, tail = text.rpartition(":")
            if head and tail.isdigit():
                return cls(source_file=head, row_no=int(tail), field_name=field_name, value=value)
        return cls(source_file=text, row_no=0, field_name=field_name, value=value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "row_no": self.row_no,
            "field": self.field_name,
            "value": self.value,
            "ref": self.ref,
        }

    def render(self) -> str:
        head = self.ref
        return f"{head} 列 {self.field_name}={self.value}" if self.field_name else head


@dataclass(frozen=True)
class DiscrepancyEntry:
    """参与聚合的一条差异。

    刻意**不**直接复用 :class:`reconq.engine.period.DiffItem`：
    台账行没有 ``cause_code``（成因在归因草稿表里，按结论键关联），
    也没有逐条证据行。把这两样现算好塞进来，聚合层就不必知道
    「成因在另一张表」这件事 —— 上游怎么存的不该渗透到分组逻辑里。
    """

    key: str
    period: str
    channel: str
    check_path: str
    cause_code: str
    diff_amount: Decimal
    ambiguity_kind: str = ""
    order_no: str = ""
    evidence: tuple[EvidenceRow, ...] = ()
    disposition: str = "待确认"
    needs_attribution: bool = True
    #: 复扫后该差异已被消解（台账 ``resolved`` 列为真）。与处置状态**不是
    #: 一回事**：已确认的单子仍是活单（要能撤回），消解的差异才是死行。
    resolved: bool = False
    #: 上游是否给过金额。差别于「金额是 0」—— 有些差异本身不带差额
    #: （如「HIS 未落单」只说明查不到行），把它当 0 元会计进合计里。
    amount_known: bool = True
    source_file: str = ""
    first_row_no: Optional[int] = None
    cause_rank: int = 1
    cause_confidence: Optional[Decimal] = None

    def amount_text(self) -> str:
        return f"{self.diff_amount:.2f}" if self.amount_known else ""

    def conclusion_key(self) -> str:
        """对齐快照用的结论键：``渠道:账期:单号``。"""
        return f"{self.channel}:{self.period}:{self.order_no or self.key}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "period": self.period,
            "channel": self.channel,
            "check_path": self.check_path,
            "order_no": self.order_no,
            "cause_code": self.cause_code,
            "ambiguity_kind": self.ambiguity_kind,
            "diff_amount": self.amount_text(),
            "disposition": self.disposition,
            "needs_attribution": self.needs_attribution,
            "evidence": [e.to_dict() for e in self.evidence],
            "conclusion_key": self.conclusion_key(),
        }


def diff_key(period: str, channel: str, check_path: str) -> str:
    """台账行的业务键（与 :data:`reconq.engine.period.DIFF_TABLE` 唯一索引同键）。"""
    return DIFF_KEY_SEP.join((period, channel, check_path))


def _order_no_of(check_path: str) -> str:
    """从 ``taxonomy.ambiguity:<单号>`` 里取单号。取不到就留空，不编。"""
    if check_path.startswith(_AMBIGUITY_PREFIX):
        return check_path[len(_AMBIGUITY_PREFIX) :]
    return ""


def to_decimal(value: Any, *, where: str = "差额") -> Decimal:
    """金额转 ``Decimal``。**float 一律拒绝**：账单金额经一次二进制浮点
    就多出一截尾数，20 笔求和的尾数差会让「同金额归一单」这条判据失效。"""
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, float):
        raise ReconqError(
            "AMOUNT_FLOAT_REJECTED",
            f"{where} 收到 float（{value!r}）；账单金额一律用字符串或 "
            f"Decimal 传入 —— 二进制浮点尾数会让同金额分不成同一张单",
        )
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ReconqError(
            "AMOUNT_UNPARSABLE",
            f"{where} {value!r} 不是可解析的金额（应为定点字符串或 Decimal）",
        ) from exc


def amount_bucket(value: Any, width: Any = DEFAULT_AMOUNT_BUCKET) -> str:
    """金额分桶。

    向下取整到桶宽（:data:`decimal.ROUND_FLOOR`），不是四舍五入 ——
    四舍五入会把 ``49.999`` 抬进 ``50.00`` 那个桶，于是两个并不同额的
    差额进了同一张交办单，而单上写的是 50.00。向下取整宁可让同一笔
    落在低一档（多一张单），也不让它和别的一档混在一张单上。

    金额缺省（0 且无来源文件）返回 :data:`NO_AMOUNT_BUCKET`，不参与金额
    分桶：没有金额的差异按「成因 + 渠道 + 形态」归并，不该因为都是 0
    就混成一堆。
    """
    amount = to_decimal(value)
    if width is None:
        return NO_AMOUNT_BUCKET if amount == 0 else f"{amount:.2f}"
    step = to_decimal(width, where="金额分桶宽度")
    if step <= 0:
        raise ReconqError(
            "AMOUNT_BUCKET_INVALID",
            f"金额分桶宽度必须是正数，收到 {width!r}；桶宽为 0 或负会让所有"
            f"金额落进同一个桶，等于没有分桶",
        )
    if amount == 0:
        return NO_AMOUNT_BUCKET
    bucket = (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step
    return f"{bucket:.2f}"


def group_key_for(
    cause_code: str, channel: str, ambiguity_kind: str, bucket: str
) -> str:
    """组键。**人可读**：面板上要能直接把一个组键粘给同事。

    用 ``::`` 连接，与 :data:`DIFF_KEY_SEP` 区分开 —— 两处都出现冒号，
    混用分隔符会让「组键里含单号」的排障变成正则噩梦。
    """
    return "::".join(
        (cause_code or "-", channel or "-", ambiguity_kind or "-", bucket or NO_AMOUNT_BUCKET)
    )


@dataclass(frozen=True)
class GroupSplit:
    """一条被打散的成员 / 一个被打散的原因。**打散必须留痕** ——
    面板上「为什么这 20 条分成 15 张单」要答得出来。"""

    key: str
    reason_code: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "reason_code": self.reason_code, "detail": self.detail}


@dataclass(frozen=True)
class AggregateRejection:
    """一条被拒的成员。成因未登记 / 输入自相矛盾时记在这里。"""

    key: str
    reason_code: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "reason_code": self.reason_code, "detail": self.detail}


@dataclass(frozen=True)
class MemberOutcome:
    """组级动作里单个成员的结果。**逐条都要有** ——
    整组失败时正是靠它告诉经办人「哪条不合法、现在是什么状态、能去哪」。"""

    key: str
    before: str
    after: str
    advanced: bool
    error_code: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "advanced": self.advanced,
            "error_code": self.error_code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GroupActionOutcome:
    """一次组级动作的整体结果。"""

    group_key: str
    action: str
    operator: str
    advanced: int = 0
    members: tuple[MemberOutcome, ...] = ()
    error_code: str = ""
    detail: str = ""
    note: str = ""
    #: 成因是否在动台账前被复核过。``unattributed`` 表示本账期压根没跑过
    #: 归因，成因取自内存里的分组而非台账 —— 这不是错，但要让人知道
    #: 「这张单的成因没有第二来源可对」。
    cause_source: str = "verified"

    @property
    def ok(self) -> bool:
        return not self.error_code and all(m.advanced for m in self.members)

    @property
    def failed_members(self) -> tuple[MemberOutcome, ...]:
        return tuple(m for m in self.members if not m.advanced)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_key": self.group_key,
            "action": self.action,
            "action_zh": ACTION_ZH.get(self.action, self.action),
            "operator": self.operator,
            "target_disposition": ACTION_TARGETS.get(self.action, ""),
            "advanced": self.advanced,
            "members": [m.to_dict() for m in self.members],
            "error_code": self.error_code,
            "detail": self.detail,
            "note": self.note,
            "cause_source": self.cause_source,
            "ok": self.ok,
        }

    def render_zh(self) -> str:
        if not self.members:
            return f"处置单 {self.group_key}：本组没有成员，未做任何改动"
        head = (
            f"处置单 {self.group_key} · {ACTION_ZH.get(self.action, self.action)}"
            f"（操作人 {self.operator}）"
        )
        if self.ok:
            return (
                f"{head}：{self.advanced}/{len(self.members)} 条已推进为 "
                f"{ACTION_TARGETS.get(self.action, '')}；整组一致，无漏项"
            )
        lines = [f"{head}：整组未推进（{self.error_code}）", f"  {self.detail}"]
        for member in self.failed_members:
            lines.append(
                f"  · {member.key}：当前 {member.before}，{member.error_code}"
                f"（{member.detail}）"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class DispositionGroup:
    """一张处置单。"""

    group_key: str
    cause_code: str
    channel: str
    ambiguity_kind: str
    amount_bucket: str
    #: 本组是按多宽的桶分出来的。**必须记在组上**：不记的话交办单渲染
    #: 「本单金额区间」时会拿默认 0.01 去解释一个 100 元宽的桶，
    #: 写出一张「0.00 元」的单子。
    bucket_width: str = f"{DEFAULT_AMOUNT_BUCKET:.2f}"
    members: tuple[DiscrepancyEntry, ...] = ()
    #: 本组为什么被单独拆出来（若是因不一致打散）。空串表示正常聚合。
    split_reason: str = ""
    aggregate_version: str = AGGREGATE_VERSION

    # —— 组属性 ———————————————————————————————————————————

    @property
    def member_keys(self) -> tuple[str, ...]:
        return tuple(m.key for m in self.members)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def disposition(self) -> str:
        """组内共同的处置状态。**组内不一致时不存在共同状态**，
        此时 :func:`aggregate_discrepancies` 早就把它打散了。"""
        return self.members[0].disposition if self.members else ""

    @property
    def total_diff_amount(self) -> Decimal:
        return sum((m.diff_amount for m in self.members), Decimal("0"))

    @property
    def total_amount_text(self) -> str:
        return f"{self.total_diff_amount:.2f}" if self.members else ""

    @property
    def zh_name(self) -> str:
        entry = CAUSES.get(self.cause_code)
        return entry.zh_name if entry else self.cause_code

    @property
    def owner(self) -> str:
        entry = CAUSES.get(self.cause_code)
        return entry.owner if entry else ""

    @property
    def suggested_action(self) -> str:
        entry = CAUSES.get(self.cause_code)
        return entry.suggested_action if entry else ""

    def amount_text(self) -> str:
        return self.amount_bucket

    def amount_range_text(self, width: Optional[Any] = None) -> str:
        """桶的**区间**文案，给交办单用。

        精确分桶（0.01 元）下区间就是那一个数，直接写金额；放宽桶宽时
        桶标签是区间下限，光写「0.00 元」会让收费处以为自己该退 0 元 ——
        区间文案把「这一张单收的是 0.00~100.00 元之间那一档」说清楚。
        """
        step = to_decimal(width if width is not None else self.bucket_width, where="金额分桶宽度")
        if self.amount_bucket == NO_AMOUNT_BUCKET:
            return "无金额"
        if step <= DEFAULT_AMOUNT_BUCKET:
            return f"{self.amount_bucket} 元"
        low = to_decimal(self.amount_bucket)
        return f"{low:.2f}~{low + step:.2f} 元"

    def conclusion_keys(self) -> tuple[str, ...]:
        """全部成员的结论键，喂给 :mod:`reconq.align.snapshot` 复算。"""
        return tuple(m.conclusion_key() for m in self.members)

    def evidence_refs(self) -> tuple[str, ...]:
        """组内全部证据定位。**不截断**（去重后）：交办单上的证据行就是
        信息科回查的入口，截掉几条等于让他们对不上账。"""
        seen: list[str] = []
        for member in self.members:
            for row in member.evidence:
                ref = row.ref
                if ref not in seen:
                    seen.append(ref)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_key": self.group_key,
            "cause_code": self.cause_code,
            "cause_zh": self.zh_name,
            "owner": self.owner,
            "suggested_action": self.suggested_action,
            "channel": self.channel,
            "ambiguity_kind": self.ambiguity_kind,
            "amount_bucket": self.amount_bucket,
            "amount_range": self.amount_range_text(),
            "bucket_width": self.bucket_width,
            "size": self.size,
            "disposition": self.disposition,
            "total_diff_amount": self.total_amount_text,
            "member_keys": list(self.member_keys),
            "evidence_refs": list(self.evidence_refs()),
            "split_reason": self.split_reason,
            "aggregate_version": self.aggregate_version,
        }


@dataclass(frozen=True)
class AggregationResult:
    """一次聚合的结果。三样都返回，因为**打散与拒绝都要能解释**。"""

    groups: tuple[DispositionGroup, ...] = ()
    splits: tuple[GroupSplit, ...] = ()
    rejections: tuple[AggregateRejection, ...] = ()
    entry_count: int = 0

    @property
    def collapsed(self) -> int:
        """被合并掉的条目数 = 原条目数 − 组数。面板上「20 条 → 3 张单」
        的那个「20」就是它减掉组数。"""
        return self.entry_count - len(self.groups)

    @property
    def total_amount(self) -> Decimal:
        return sum((g.total_diff_amount for g in self.groups), Decimal("0"))

    def by_owner(self) -> dict[str, int]:
        """责任方 → 组数。**这张表就是交办分工表**：每张单点名一个
        责任方，财务科按这张表把单分出去。"""
        out: dict[str, int] = {}
        for group in self.groups:
            out[group.owner] = out.get(group.owner, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    def group(self, group_key: str) -> Optional[DispositionGroup]:
        for item in self.groups:
            if item.group_key == group_key:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregate_version": AGGREGATE_VERSION,
            "entry_count": self.entry_count,
            "group_count": len(self.groups),
            "collapsed": self.collapsed,
            "total_amount": f"{self.total_amount:.2f}",
            "by_owner": self.by_owner(),
            "groups": [g.to_dict() for g in self.groups],
            "splits": [s.to_dict() for s in self.splits],
            "rejections": [r.to_dict() for r in self.rejections],
        }


# ─────────────────────────── 聚合 ───────────────────────────


def aggregate_discrepancies(
    discrepancies: Sequence[DiscrepancyEntry],
    *,
    amount_bucket_width: Any = DEFAULT_AMOUNT_BUCKET,
    drop_resolved: bool = True,
) -> AggregationResult:
    """把一堆单边账按「成因 + 渠道 + 形态 + 金额分桶」收成处置单。

    :param amount_bucket_width: 桶宽。默认 :data:`DEFAULT_AMOUNT_BUCKET`
        （0.01 元精确分桶，不同金额绝不并单）。
    :param drop_resolved: 丢掉已消解的条目。默认开着 —— 复扫后已经对上的
        那几条还挂在台账上（保留可追溯），但它们不该再出现在「要办的单」
        里，否则经办人会去办一件已经办完的事。

    打散的三种情形见模块开头。返回的 :class:`AggregationResult` 里
    ``splits`` / ``rejections`` 非空不是异常，是**要让人看见的判断**。
    """
    width = to_decimal(amount_bucket_width, where="金额分桶宽度") if amount_bucket_width else None
    entries = [d for d in discrepancies if not (drop_resolved and d.resolved)]
    if not entries:
        return AggregationResult(groups=(), splits=(), rejections=(), entry_count=0)

    buckets: dict[str, list[DiscrepancyEntry]] = {}
    rejections: list[AggregateRejection] = []
    identity_splits: set[int] = set()
    for entry in entries:
        if not entry.cause_code:
            rejections.append(
                AggregateRejection(
                    key=entry.key,
                    reason_code="GROUP_CAUSE_MISSING",
                    detail="该差异没有受控成因码（本期未跑归因，或草稿被拒），"
                    "不能按受控成因归并；仍单列成一张成因「未定」的待人工判定单，"
                    "请先跑一次归因",
                )
            )
        elif entry.cause_code not in CAUSES:
            rejections.append(
                AggregateRejection(
                    key=entry.key,
                    reason_code="GROUP_CAUSE_UNREGISTERED",
                    detail=f"成因码 {entry.cause_code!r} 不在受控字典里，拒绝按成因归并："
                    f"交办单写不出「谁去查、查什么」。该条仍单列成一张成因「未定」"
                    f"的待人工判定单，不会被悄悄并进别的事由里",
                )
            )
        key = _key_of(entry, width)
        buckets.setdefault(key, []).append(entry)

    groups: list[DispositionGroup] = []
    splits: list[GroupSplit] = []
    for key, members in buckets.items():
        # —— 逐条自检「自报身份」；对不上台账坐标的**拒收，不成单** ——
        #
        # 刻意不给它单独成一张单：组级确认是靠 key 回到台账上定位并更新
        # 那一行的，key 与坐标不符的条目它压根更新不到；给它一张单，等于
        # 在面板上放一个点了必然失败的按钮。拒收 + 写清原因才是可操作的。
        stray = [m for m in members if _identity_problem(m)]
        if stray:
            stray_ids = {id(m) for m in stray}
            kept = [m for m in members if id(m) not in stray_ids]
            if kept:
                groups.append(
                    _build_group(
                        key, kept, width, split_reason="同组内有成员自报身份不符，已拒收"
                    )
                )
            for member in stray:
                splits.append(
                    GroupSplit(
                        key=member.key,
                        reason_code="GROUP_MEMBER_INCONSISTENT",
                        detail=f"{_identity_problem(member)}；该条已拒收，不参与聚合。"
                        f"自报身份与台账键不符时若成单，组级确认会去更新"
                        f"另一条差异的处置状态",
                    )
                )
                rejections.append(
                    AggregateRejection(
                        key=member.key,
                        reason_code="GROUP_MEMBER_INCONSISTENT",
                        detail=_identity_problem(member)
                        + "；本条无法定位到台账上的一行，不参与聚合，请先核对写入端",
                    )
                )
                identity_splits.add(id(member))
            # 本桶已按「拒收 + 保留其余」处理完，不再走下面的正常成组路径
            # （否则会把含被拒成员的整桶**再追加一次**，面板上出现两张同名单）
            continue

        # —— 组内处置状态必须一致 ——
        by_disp: dict[str, list[DiscrepancyEntry]] = {}
        for member in members:
            by_disp.setdefault(member.disposition, []).append(member)
        if len(by_disp) > 1:
            for disposition, group_members in sorted(by_disp.items()):
                groups.append(
                    _build_group(
                        f"{key}@{disposition}",
                        group_members,
                        width,
                        split_reason=f"组内处置状态不一致（{disposition}）",
                    )
                )
            for member in members:
                splits.append(
                    GroupSplit(
                        key=member.key,
                        reason_code="GROUP_DISPOSITION_MIXED",
                        detail="同一组里既有待确认也有已确认/已驳回，"
                        "合成一张单会让经办人点确认时只成功一部分；已按处置状态打散",
                    )
                )
            continue

        groups.append(_build_group(key, members, width))

    groups.sort(key=_group_sort_key)
    return AggregationResult(
        groups=tuple(groups),
        splits=tuple(splits),
        rejections=tuple(rejections),
        entry_count=len(entries) - len(identity_splits),
    )


def _identity_problem(entry: DiscrepancyEntry) -> str:
    """这条差异的「自报身份」与它自己的字段对不对得上。对得上返回空串。

    **这条自检是有后果的，不是洁癖**：组级确认是靠 ``entry.key``
    （``账期|渠道|口径``）去台账上定位并更新那一行的。键与字段一旦
    不符，点一次「整单确认」更新的是**另一条**差异的处置状态 —— 而
    界面上看不出任何异常，因为「8/8 条已推进」照样打出来。

    两种对不上的情形：

    * ``key`` 与 ``(period, channel, check_path)`` 拼出来的键不同
      —— 上游把 A 的差异挂到了 B 的坐标上；
    * ``check_path`` 是歧义前缀的，但 ``order_no`` 与前缀里的单号不同
      —— 交办单上的单号与台账里那一行对不上，信息科拿着单号回查不到。
    """
    expected = diff_key(entry.period, entry.channel, entry.check_path)
    if entry.key != expected:
        return (
            f"自报键 {entry.key!r} 与其自身字段拼出的键 {expected!r} 不符"
            f"（账期 {entry.period} / 渠道 {entry.channel} / 口径 {entry.check_path}）"
        )
    if entry.check_path.startswith(_AMBIGUITY_PREFIX):
        declared = entry.check_path[len(_AMBIGUITY_PREFIX) :]
        if entry.order_no and entry.order_no != declared:
            return (
                f"自报单号 {entry.order_no!r} 与口径里的单号 {declared!r} 不符"
            )
    return ""


def _key_of(entry: DiscrepancyEntry, width: Optional[Decimal]) -> str:
    """一条差异自报的组键。**宽度相关的键必须现算**，
    不能缓存 —— 桶宽一改，全部组的键都跟着变。"""
    return group_key_for(
        entry.cause_code,
        entry.channel,
        entry.ambiguity_kind,
        amount_bucket(entry.diff_amount, width),
    )


def _build_group(
    group_key: str,
    members: Sequence[DiscrepancyEntry],
    width: Optional[Decimal],
    *,
    split_reason: str = "",
) -> DispositionGroup:
    head = members[0]
    return DispositionGroup(
        group_key=group_key,
        cause_code=head.cause_code or "undetermined",
        channel=head.channel,
        ambiguity_kind=head.ambiguity_kind,
        amount_bucket=amount_bucket(head.diff_amount, width),
        bucket_width=f"{width:.2f}" if width is not None else "0.01",
        members=tuple(sorted(members, key=_member_sort_key)),
        split_reason=split_reason,
    )


def _member_sort_key(entry: DiscrepancyEntry) -> tuple[str, str]:
    """成员排序：先单号后键。同一张单上人按单号读得下去；
    单号为空时（口径差异）退回台账键，保证排序是**全序**（不会因
    两条同 key 之外的输入顺序不同而使两次聚合的输出顺序不一致）。"""
    return (entry.order_no or "~", entry.key)


def _group_sort_key(group: DispositionGroup) -> tuple[Any, ...]:
    """组排序：条数降序 → 金额降序 → 责任方 → 组键。末两级不是装饰 ——
    条数与金额都相同时顺序必须由键自身决定，否则同一份数据两次聚合
    面板排序会不同，截图对不上、也测不出回归。"""
    return (-group.size, -group.total_diff_amount, group.owner, group.group_key)


def expand_group(group: DispositionGroup) -> tuple[DiscrepancyEntry, ...]:
    """展开处置单的全部成员（含各自证据行）。

    **合并不吞证据**：展开后每条成员仍带自己的 ``证据行号`` 与
    ``conclusion_key``，可单独回看快照。台账上逐条可查这件事不受
    聚合影响 —— 这是本模块敢在前面做合并的前提。
    """
    return tuple(sorted(group.members, key=_member_sort_key))


def render_group_zh(group: DispositionGroup) -> str:
    """一张处置单的终端画面。"""
    lines = [
        f"处置单 {group.group_key}",
        f"  成因：{group.zh_name}（{group.cause_code}）"
        + (f" · 形态：{group.ambiguity_kind}" if group.ambiguity_kind else ""),
        f"  渠道：{group.channel} · 单笔差额：{group.amount_range_text()}"
        f" · 共 {group.size} 条"
        + (
            f" / 合计 {group.total_amount_text} 元"
            if any(m.amount_known for m in group.members)
            else "（本单各条均无金额）"
        ),
        f"  责任方：{group.owner}",
    ]
    if group.suggested_action:
        lines.append(f"  建议动作：{group.suggested_action}")
    if group.split_reason:
        lines.append(f"  ! 本单因「{group.split_reason}」被单独拆出，未与其余条目合并")
    lines.append(f"  逐条明细（共 {group.size} 条）：")
    for member in expand_group(group):
        refs = "、".join(e.ref for e in member.evidence) or (
            f"{member.source_file}:{member.first_row_no}"
            if member.source_file and member.first_row_no
            else "（无行级证据）"
        )
        lines.append(
            f"    · {member.order_no or member.key} "
            f"{member.amount_text() + ' 元' if member.amount_known else '（无金额）'}"
            f" [{member.disposition}] 证据 {refs}"
        )
    return "\n".join(lines)


def render_summary_zh(result: AggregationResult) -> str:
    """一屏聚合总览 + 分工表。"""
    if not result.groups:
        return "本期单边账聚合：无可成单的差异"
    lines = [
        f"单边账按同根因聚合（{AGGREGATE_VERSION}）："
        f"{result.entry_count} 条 → {len(result.groups)} 张处置单"
        f"（合并 {result.collapsed} 条，合计 {result.total_amount:.2f} 元）"
    ]
    for group in result.groups:
        lines.append(
            f"  · [{group.disposition}] {group.zh_name} / {group.channel} / "
            f"{group.amount_range_text()} × {group.size} 条 = "
            f"{group.total_amount_text + ' 元' if any(m.amount_known for m in group.members) else '无金额'}"
            f" → {group.owner}"
        )
    owners = "、".join(f"{owner} {n} 张" for owner, n in result.by_owner().items())
    if owners:
        lines.append(f"  分工：{owners}")
    if result.splits:
        lines.append(
            f"  打散说明：{len(result.splits)} 条因组内不一致被单独成单"
            f"（{'、'.join(sorted({s.reason_code for s in result.splits}))}）"
        )
    if result.rejections:
        undetermined = [g for g in result.groups if g.cause_code == "undetermined"]
        lines.append(
            f"  拒收：{len(result.rejections)} 条未登记成因/缺成因，未按成因归并"
            f"（已单列成 {len(undetermined)} 张「未定」待人工判定单）"
        )
    return "\n".join(lines)


# ─────────────────── 从台账与归因草稿装载 ───────────────────


def entry_from_diff_item(
    item: Any,
    *,
    cause_code: str = "",
    cause_rank: int = 1,
    cause_confidence: Optional[Decimal] = None,
    evidence: Sequence[EvidenceRow] = (),
) -> DiscrepancyEntry:
    """台账行 → :class:`DiscrepancyEntry`。

    **鸭子类型入参**：收任何带 ``period`` / ``channel`` / ``check_path`` /
    ``diff_amount`` / ``disposition`` / ``ambiguity_kind`` 的对象，不 import
    :class:`reconq.engine.period.DiffItem` —— ``align`` 不该被 ``engine``
    反向依赖（:mod:`reconq.align.taxonomy` 定的规矩）。
    """
    check_path = str(getattr(item, "check_path", "") or "")
    raw_amount = getattr(item, "diff_amount", None)
    return DiscrepancyEntry(
        key=diff_key(
            str(getattr(item, "period", "") or ""),
            str(getattr(item, "channel", "") or ""),
            check_path,
        ),
        period=str(getattr(item, "period", "") or ""),
        channel=str(getattr(item, "channel", "") or ""),
        check_path=check_path,
        order_no=_order_no_of(check_path),
        cause_code=cause_code or "",
        cause_rank=cause_rank,
        cause_confidence=cause_confidence,
        diff_amount=to_decimal(raw_amount, where=f"差异 {check_path} 的差额"),
        ambiguity_kind=str(getattr(item, "ambiguity_kind", "") or ""),
        evidence=tuple(evidence),
        disposition=str(getattr(item, "disposition", "待确认") or "待确认"),
        needs_attribution=bool(getattr(item, "needs_attribution", True)),
        resolved=bool(getattr(item, "resolved", False)),
        amount_known=raw_amount not in (None, ""),
        source_file=str(getattr(item, "source_file", "") or ""),
        first_row_no=getattr(item, "first_row_no", None),
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def load_group_entries(
    conn: sqlite3.Connection,
    period: str,
    *,
    channel: str = "",
    disposition: str = "",
    include_resolved: bool = False,
) -> list[DiscrepancyEntry]:
    """从台账 + 归因草稿装出参与聚合的条目。

    成因取归因草稿表的 **Top-1**（``rank=1``）—— 逐条差异的成因排序有
    1~5 条，聚合只认 Top-1：把 Top-2/Top-3 也算进来，一条差异会同时
    落进多张单，同一件事在交办单上出现两遍。
    """
    from ..engine.period import DIFF_TABLE, list_diff_items  # 局部 import：见上

    if not _table_exists(conn, DIFF_TABLE):
        raise ReconqError(
            "PERIOD_SCAN_NOT_INITIALIZED",
            f"{DIFF_TABLE} 表尚未建表；先跑一次 reconq scan --period {period}",
        )
    items = list_diff_items(
        conn, period, disposition=disposition or None, include_resolved=include_resolved
    )
    if channel:
        items = [i for i in items if i.channel == channel]

    draft_table = "attribution_drafts"
    top_causes: dict[str, tuple[str, int, Optional[Decimal]]] = {}
    evidence_by_key: dict[str, list[EvidenceRow]] = {}
    if _table_exists(conn, draft_table):
        for row in conn.execute(
            f"SELECT conclusion_key, rank, cause, confidence_text, evidence_refs "
            f"FROM {draft_table} WHERE period=? AND rank=1",
            (period,),
        ):
            key = str(row["conclusion_key"] or "")
            if not key:
                continue
            top_causes[key] = (
                str(row["cause"] or ""),
                int(row["rank"] or 1),
                _maybe_decimal(row["confidence_text"]),
            )
            parsed = _parse_evidence_refs(row["evidence_refs"])
            if parsed:
                evidence_by_key[key] = parsed

    entries: list[DiscrepancyEntry] = []
    for item in items:
        probe = entry_from_diff_item(item)
        conclusion = probe.conclusion_key()
        cause, rank, confidence = top_causes.get(conclusion, ("", 1, None))
        evidence = evidence_by_key.get(conclusion) or _head_evidence(item)
        entries.append(
            replace(
                probe,
                cause_code=cause,
                cause_rank=rank,
                cause_confidence=confidence,
                evidence=tuple(evidence),
            )
        )
    return entries


def _parse_evidence_refs(raw: Any) -> list[EvidenceRow]:
    try:
        refs = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except (TypeError, ValueError):
        return []
    out: list[EvidenceRow] = []
    for ref in refs:
        row = EvidenceRow.parse_ref(str(ref))
        if row.ref:
            out.append(row)
    return out


def _head_evidence(item: Any) -> tuple[EvidenceRow, ...]:
    """台账行只存了首条证据（``source_file`` + ``first_row_no``）。用它兜底，
    显式标出「只有首行」而不是让面板以为证据就这一行。"""
    source = str(getattr(item, "source_file", "") or "")
    row_no = getattr(item, "first_row_no", None)
    if not source or not row_no:
        return ()
    return (EvidenceRow(source_file=source, row_no=int(row_no), field_name="", value=""),)


def _maybe_decimal(raw: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(raw)) if raw not in (None, "") else None
    except (InvalidOperation, ValueError):  # pragma: no cover - 审计表里是定点串
        return None


# ─────────────────────── 组级处置（写路径） ───────────────────────


DISPOSITION_ACTION_TABLE = "disposition_actions"

DISPOSITION_ACTION_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {DISPOSITION_ACTION_TABLE} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    period        TEXT NOT NULL,
    group_key     TEXT NOT NULL,
    action        TEXT NOT NULL,
    target_state  TEXT NOT NULL,
    operator      TEXT NOT NULL,
    member_key    TEXT NOT NULL,
    before_state  TEXT NOT NULL,
    after_state   TEXT NOT NULL,
    advanced      INTEGER NOT NULL DEFAULT 0,
    error_code    TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    group_size    INTEGER NOT NULL DEFAULT 0,
    cause_source  TEXT NOT NULL DEFAULT '',
    recorded_at   TEXT NOT NULL DEFAULT ''
)
"""

#: 幂等键 = 账期 + 组键 + 动作 + 成员键。同一张单被重复点两次不会留下
#: 两条一模一样的动作记录（第二次的预检就会因状态已变而整体被拒，
#: 仍留下一条 ``advanced=0`` 的记录 —— **拒也要留痕**，否则「我明明
#: 没点过」这件事无法解释）。
DISPOSITION_ACTION_INDEXES = (
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_disposition_action_identity
        ON {DISPOSITION_ACTION_TABLE} (period, group_key, action, member_key, recorded_at)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS ix_disposition_action_period
        ON {DISPOSITION_ACTION_TABLE} (period, group_key)
    """,
)


def init_disposition_action_schema(conn: sqlite3.Connection) -> None:
    """建动作留痕表。幂等。"""
    conn.execute(DISPOSITION_ACTION_TABLE_DDL)
    for statement in DISPOSITION_ACTION_INDEXES:
        conn.execute(statement)


def assert_human_operator(operator: str) -> str:
    """操作人必须是**人**。返回清洗后的操作人。

    与 Task 30「模型降级不得推进台账状态」是同一条线，但落在这里更靠后
    也更硬：那条线依赖每条调用路径都记得自己是谁，而这条把「不是人」
    的取值在**入口**就拒掉 —— 模型草稿、规则基线、网关返回值想推进
    状态，必须先编出一个像人的操作人，那一步就已经留下痕迹了。
    """
    text = str(operator or "").strip()
    if text.lower() in _NON_HUMAN_OPERATORS or text in _NON_HUMAN_OPERATORS:
        raise ReconqError(
            "GROUP_ACTION_OPERATOR_INVALID",
            f"操作人 {operator!r} 不是自然人标识，本产品拒绝由它推进处置状态："
            f"模型草稿、规则基线与降级路径都不得把差异推成已确认。"
            f"请传入经办人 / 审核人 / 主管的工号或姓名",
        )
    if len(text) > 64:  # pragma: no cover - 面板输入框限长已挡住
        raise ReconqError(
            "GROUP_ACTION_OPERATOR_INVALID",
            f"操作人标识过长（{len(text)} 字符）；留痕表按 64 字符存",
        )
    return text


def _require_schema(conn: sqlite3.Connection, table: str) -> None:
    if not _table_exists(conn, table):
        raise ReconqError(
            "PERIOD_SCAN_NOT_INITIALIZED",
            f"{table} 表尚未建表；先跑一次 reconq scan --period <账期>",
        )


def _read_live_members(
    conn: sqlite3.Connection, group: DispositionGroup
) -> tuple[list[tuple[str, str, str]], str]:
    """回台账逐条复核组成员。返回 ``[(key, disposition, ambiguity_kind)]``
    与成因核验来源。

    **为什么要回读而不是直接用内存里的组**：组是从面板那一刻的台账算
    出来的，而台账随时可能被重扫 / 重跑归因改掉（成因从「渠道延迟入账」
    变成「HIS 未落单」是常事）。拿着一个已经过期的组去确认，等于把
    上一轮的结论盖到这一轮的账上。回读一次，代价是一次主键查询，
    换来「点确认的一定是刚才看到的那批」。
    """
    from ..engine.period import DIFF_TABLE  # 局部 import：见上

    _require_schema(conn, DIFF_TABLE)
    out: list[tuple[str, str, str]] = []
    for member in group.members:
        period, _, rest = member.key.partition(DIFF_KEY_SEP)
        channel, _, check_path = rest.partition(DIFF_KEY_SEP)
        row = conn.execute(
            f"SELECT disposition, ambiguity_kind FROM {DIFF_TABLE} "
            f"WHERE period=? AND channel=? AND check_path=?",
            (period, channel, check_path),
        ).fetchone()
        if row is None:
            raise ReconqError(
                "GROUP_MEMBER_MISSING",
                f"处置单 {group.group_key} 的成员 {member.key} 在台账上已不存在；"
                f"多半是复扫后该差异被消解。请重新拉一次处置单（reconq groups list）",
            )
        out.append(
            (member.key, str(row["disposition"] or "待确认"), str(row["ambiguity_kind"] or ""))
        )
    return out, "verified"


def _verify_causes(
    conn: sqlite3.Connection, group: DispositionGroup
) -> str:
    """复核成员当前 Top-1 成因仍与本组一致。

    返回 ``verified`` / ``unattributed`` / 抛错。归因草稿表不存在或本
    账期一条草稿都没有时返回 ``unattributed``：不是错（本轮压根没跑
    归因，成因只有分组这一个来源），但要让面板知道。
    """
    draft_table = "attribution_drafts"
    if not _table_exists(conn, draft_table):
        return "unattributed"
    checked = 0
    for member in group.members:
        row = conn.execute(
            f"SELECT cause FROM {draft_table} "
            f"WHERE period=? AND conclusion_key=? AND rank=1",
            (member.period, member.conclusion_key()),
        ).fetchone()
        if row is None:
            continue
        checked += 1
        current = str(row["cause"] or "")
        if current and current != member.cause_code:
            raise ReconqError(
                "GROUP_MEMBER_CAUSE_CHANGED",
                f"处置单 {group.group_key} 的成员 {member.key} 当前 Top-1 成因已是 "
                f"{current!r}，与本单的 {member.cause_code!r} 不一致。"
                f"请重新拉处置单（reconq groups list）—— 拿旧成因确认会把"
                f"上一轮的结论盖到这一轮的账上",
            )
    return "verified" if checked else "unattributed"


def apply_group_action(
    conn: sqlite3.Connection,
    group: DispositionGroup,
    action: Any,
    *,
    operator: str,
    note: str = "",
    now: Optional[str] = None,
) -> GroupActionOutcome:
    """组级确认 / 驳回 / 撤回。**整组要么全动要么全不动。**

    流程刻意是「先全量预检、再一次性提交」：

    1. 校验操作人是人；
    2. 回台账逐条读当前状态（防止拿着过期组操作）；
    3. 复核成员成因仍与本组一致；
    4. 逐条过白名单预检（确认/驳回只从「待确认」出发，撤回只从两个
       终态出发）；
    5. **任一条不过 → 整组不动**，返回逐条结果后写留痕并返回；
    6. 全过 → 单个事务内逐条 UPDATE，任一条写失败整组回滚。

    留痕表**无论成败都写**：一条被拒的点击同样要能解释「我明明点了
    确认为什么没动」。
    """
    act = _action_value(action)
    who = assert_human_operator(operator)
    target = ACTION_TARGETS[act]
    sources = ACTION_SOURCES[act]
    stamp = now or datetime.now().astimezone().isoformat(timespec="seconds")
    init_disposition_action_schema(conn)

    members: list[MemberOutcome] = []
    cause_source = "unattributed"
    group_error = ""
    group_detail = ""

    try:
        live, cause_source = _read_live_members(conn, group)
        cause_source = _verify_causes(conn, group)
        for member, (key, disposition, ambiguity_kind) in zip(group.members, live):
            if ambiguity_kind != group.ambiguity_kind and member.ambiguity_kind:
                members.append(
                    MemberOutcome(
                        key=key,
                        before=disposition,
                        after=disposition,
                        advanced=False,
                        error_code="GROUP_MEMBER_KIND_CHANGED",
                        detail=f"台账上该条的形态已是 {ambiguity_kind!r}，"
                        f"与本单的 {group.ambiguity_kind!r} 不一致；请重新拉处置单",
                    )
                )
                continue
            if disposition not in sources:
                members.append(
                    MemberOutcome(
                        key=key,
                        before=disposition,
                        after=disposition,
                        advanced=False,
                        error_code="GROUP_ACTION_DENIED",
                        detail=f"「{ACTION_ZH[act]}」只能从 {'、'.join(sorted(sources))} 出发，"
                        f"该条当前是 {disposition}",
                    )
                )
                continue
            members.append(
                MemberOutcome(key=key, before=disposition, after=target, advanced=True)
            )
    except ReconqError as exc:
        group_error = exc.code
        group_detail = exc.detail or str(exc)

    advanced = sum(1 for m in members if m.advanced)
    if not group_error and advanced != len(members):
        failed = [m for m in members if not m.advanced]
        codes = sorted({m.error_code for m in failed}) or ["GROUP_ACTION_DENIED"]
        group_error = codes[0]
        group_detail = (
            f"本单 {len(members)} 条里有 {len(failed)} 条状态不允许「{ACTION_ZH[act]}」，"
            f"整组未推进（错误码 {'、'.join(codes)}）。部分成功最危险："
            f"经办人会以为整单办完了。请按下面逐条提示处理后重试"
        )
    if group_error:
        # 整组未推进时，**逐条结论都要改成「没动」**：预检阶段判过的那几条
        # 是「本可推进」，若照原样返回 advanced=True，而事务里又一条都没
        # 提交，调用方拿到的就是一份与台账对不上的成功名单。
        members = [
            replace(
                m,
                advanced=False,
                after=m.before,
                error_code=m.error_code or group_error,
                detail=m.detail or group_detail or "整组预检未通过，未做任何改动",
            )
            for m in members
        ] or [
            MemberOutcome(
                key=member.key,
                before="",
                after="",
                advanced=False,
                error_code=group_error,
                detail=group_detail or "整组预检未通过，未做任何改动",
            )
            for member in group.members
        ]
        advanced = 0

    conn.execute("BEGIN IMMEDIATE")
    try:
        if advanced:
            for member, outcome in zip(group.members, members):
                period, _, rest = member.key.partition(DIFF_KEY_SEP)
                channel, _, check_path = rest.partition(DIFF_KEY_SEP)
                conn.execute(
                    "UPDATE period_diff_items SET disposition=? "
                    "WHERE period=? AND channel=? AND check_path=?",
                    (target, period, channel, check_path),
                )
                _record_action(
                    conn,
                    period=period,
                    group_key=group.group_key,
                    action=act,
                    target=target,
                    operator=who,
                    member=outcome,
                    note=note,
                    group_size=len(members),
                    cause_source=cause_source,
                    stamp=stamp,
                )
        else:
            # 成败都留痕。整组未推进时逐条写一条 advanced=0 的记录，
            # 记下「点了但没生效」以及每条卡在哪 —— 否则「我明明点了确认为什么
            # 没动」这件事无法解释。
            for member, outcome in zip(group.members, members):
                period, _, rest = member.key.partition(DIFF_KEY_SEP)
                _record_action(
                    conn,
                    period=period or member.period,
                    group_key=group.group_key,
                    action=act,
                    target=target,
                    operator=who,
                    member=outcome,
                    note=note,
                    group_size=len(members),
                    cause_source=cause_source,
                    stamp=stamp,
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return GroupActionOutcome(
        group_key=group.group_key,
        action=act,
        operator=who,
        advanced=advanced,
        members=tuple(members),
        error_code=group_error,
        detail=group_detail,
        note=note,
        cause_source=cause_source,
    )


def _record_action(
    conn: sqlite3.Connection,
    *,
    period: str,
    group_key: str,
    action: str,
    target: str,
    operator: str,
    member: MemberOutcome,
    note: str,
    group_size: int,
    cause_source: str,
    stamp: str,
) -> None:
    conn.execute(
        f"INSERT OR IGNORE INTO {DISPOSITION_ACTION_TABLE} "
        f"(period, group_key, action, target_state, operator, member_key, "
        f" before_state, after_state, advanced, error_code, note, group_size, "
        f" cause_source, recorded_at) "
        f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            period,
            group_key,
            action,
            target,
            operator,
            member.key,
            member.before,
            member.after,
            1 if member.advanced else 0,
            member.error_code,
            note,
            group_size,
            cause_source,
            stamp,
        ),
    )


def confirm_group(
    conn: sqlite3.Connection, group: DispositionGroup, *, operator: str, note: str = ""
) -> GroupActionOutcome:
    """整单确认。"""
    return apply_group_action(conn, group, GroupAction.CONFIRM, operator=operator, note=note)


def reject_group(
    conn: sqlite3.Connection, group: DispositionGroup, *, operator: str, note: str = ""
) -> GroupActionOutcome:
    """整单驳回。"""
    return apply_group_action(conn, group, GroupAction.REJECT, operator=operator, note=note)


def withdraw_group(
    conn: sqlite3.Connection, group: DispositionGroup, *, operator: str, note: str = ""
) -> GroupActionOutcome:
    """整单撤回 → 全部回「待确认」，逐条记操作人与时刻。"""
    return apply_group_action(conn, group, GroupAction.WITHDRAW, operator=operator, note=note)


def list_group_actions(
    conn: sqlite3.Connection,
    period: str,
    *,
    group_key: str = "",
) -> list[dict[str, Any]]:
    """列出动作留痕（结论单与 Task 45 事件链的反查入口）。"""
    if not _table_exists(conn, DISPOSITION_ACTION_TABLE):
        # 从没点过任何一次处置 → 没有留痕是正常态，不是「库坏了」。
        # 抛错会让「今天还没人办过单」显示成一条红色故障。
        return []
    sql = f"SELECT * FROM {DISPOSITION_ACTION_TABLE} WHERE period=?"
    params: list[Any] = [period]
    if group_key:
        sql += " AND group_key=?"
        params.append(group_key)
    sql += " ORDER BY id"
    cursor = conn.execute(sql, params)
    columns = [c[0] for c in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ─────────────────────── 合并台账（交办单导出） ───────────────────────


_WHITESPACE = re.compile(r"\s+")


def render_group_slip_markdown(
    groups: Sequence[DispositionGroup],
    *,
    period: str,
    operator: str = "",
    note: str = "",
    actions: Optional[Sequence[dict[str, Any]]] = None,
) -> str:
    """导出交办单（Markdown）。**按组出证明、按条附证据**。

    版式上刻意分两级：组头给「成因 / 渠道 / 单笔 / 条数 / 合计 /
    责任方 / 建议动作」，组内逐条给「单号 / 金额 / 处置 / 证据行号」。
    主管复核时看组头即可判断该不该签字，信息科回查时看组内行号即可定位
    源文件 —— 两类人不需要读同一段文字。
    """
    who = assert_human_operator(operator) if operator else "（未署名）"
    total_amount = sum((g.total_diff_amount for g in groups), Decimal("0"))
    lines: list[str] = [
        f"# {period} 支付对账同根因处置单",
        "",
        f"- 聚合规则：{AGGREGATE_VERSION}（成因 + 渠道 + 形态 + 金额分桶，"
        f"默认按 {DEFAULT_AMOUNT_BUCKET} 元精确分桶，不同金额不并单）",
        f"- 处置单张数：{len(groups)} 张，覆盖差异 "
        f"{sum(g.size for g in groups)} 条，合计 {total_amount:.2f} 元",
        f"- 出单人：{who}" + (f" · 备注：{note}" if note else ""),
    ]
    if not groups:
        lines += ["", "本期无单边账，无处置单。"]
        return "\n".join(lines) + "\n"

    owners: dict[str, int] = {}
    for group in groups:
        owners[group.owner] = owners.get(group.owner, 0) + 1
    lines += [
        "",
        "## 分工表",
        "",
        "| 责任方 | 处置单张数 | 涉及差异条数 | 涉及金额 |",
        "|--------|------------|--------------|----------|",
    ]
    for owner, count in sorted(owners.items(), key=lambda kv: (-kv[1], kv[0])):
        picked = [g for g in groups if g.owner == owner]
        lines.append(
            f"| {owner} | {count} | {sum(g.size for g in picked)} | "
            f"{sum((g.total_diff_amount for g in picked), Decimal('0')):.2f} |"
        )

    for index, group in enumerate(groups, start=1):
        lines += [
            "",
            f"## {index}. {group.zh_name} · {group.channel} · "
            f"{group.amount_range_text()} × {group.size} 条",
            "",
            f"- 处置单键：`{group.group_key}`",
            f"- 责任方：{group.owner}",
            f"- 建议动作：{group.suggested_action or '（成因未登记，无建议动作）'}",
            f"- 本单合计：{group.total_amount_text} 元（{group.size} 条）"
            if any(m.amount_known for m in group.members)
            else f"- 本单各条均无金额（{group.size} 条）",
            f"- 当前处置：{group.disposition}",
        ]
        if group.split_reason:
            lines.append(f"- ⚠ 本单因「{group.split_reason}」单独拆出，未与其余条目合并")
        if group.ambiguity_kind:
            entry = CAUSES.get(group.cause_code)
            kind_zh = (entry.clues[0] if entry and entry.clues else "")
            lines.append(f"- 形态：{group.ambiguity_kind}" + (f"（{kind_zh}）" if kind_zh else ""))
        lines += [
            "",
            "| # | 渠道单号 | 差额(元) | 处置 | 证据行 | 结论键 |",
            "|---|----------|----------|------|--------|--------|",
        ]
        for row_index, member in enumerate(expand_group(group), start=1):
            refs = "、".join(e.ref for e in member.evidence) or (
                f"{member.source_file}:{member.first_row_no}"
                if member.source_file and member.first_row_no
                else "—"
            )
            lines.append(
                f"| {row_index} | {member.order_no or '（无单号）'} | "
                f"{member.amount_text()} | {member.disposition} | {refs} | "
                f"`{member.conclusion_key()}` |"
            )

    if actions:
        # 留痕表是**逐条成员**记的（8 条成员 = 8 行）。交办单上照抄一遍等于
        # 8 行一模一样的话，把真正要看的「谁在什么时候办了这张单」淹没掉 ——
        # 这里按「一次动作」归并成一行，成员数与推进数各给一个合计。
        merged: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for row in actions:
            sig = (
                str(row.get("recorded_at", "")),
                str(row.get("action", "")),
                str(row.get("operator", "")),
                str(row.get("group_key", "")),
                _WHITESPACE.sub(" ", str(row.get("note") or "")),
            )
            bucket = merged.setdefault(
                sig,
                {
                    "rows": 0,
                    "advanced": 0,
                    "size": int(row.get("group_size") or 0),
                    "errors": set(),
                },
            )
            bucket["rows"] += 1
            bucket["advanced"] += int(row.get("advanced") or 0)
            if row.get("error_code"):
                bucket["errors"].add(str(row["error_code"]))
        lines += [
            "",
            "## 处置留痕",
            "",
            "| 时刻 | 操作 | 操作人 | 处置单 | 推进/组内 | 备注 |",
            "|------|------|--------|--------|-----------|------|",
        ]
        for (when, action, operator, key, note), bucket in merged.items():
            detail = f"{bucket['advanced']}/{bucket['size']}"
            if bucket["errors"]:
                detail += " · " + "、".join(sorted(bucket["errors"]))
            lines.append(
                f"| {when} | {ACTION_ZH.get(action, action)} | {operator} | `{key}` | "
                f"{detail} | {note} |"
            )
    return "\n".join(lines) + "\n"
