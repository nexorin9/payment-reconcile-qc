"""单边账成因受控字典与中文解释模板（Task 33）。

自由文本成因是「看起来能用、用起来没法用」
--------------------------------------------
Task 28 让模型给出 ``{cause, confidence, evidence_refs[], explanation_zh}``
的成因排序草稿。**只有不做字典，「这 20 条单边账能不能按成因聚成一屏」这件事
才有答案**：自由文本写出来的是「延迟入账」「渠道晚同步」「可能对账时间差」三种
说法，指的是同一件事，聚合时彼此不相等；而「导出方漏传」「退费未红冲」「跨日
切分重复计入」是真的三件事，却可能被写成同一种措辞。成不成组，全看措辞碰巧
不碰巧 —— 而一个月后复盘上周那期，没人说得清「上周的『渠道晚同步』和这周的
『延迟入账』是不是同一件事」。

于是本模块把成因收成**受控集合**：每项带编码、中文名、判别线索、建议动作与
建议责任方。说清「收成受控集合」之后要一并说清**代价**：现场一定会冒出现有
字典覆盖不了的成因。处理办法不是放宽成自由文本（那就回到原点），而是**显式
拒绝 + 记明缺什么**（见下方四条边界）。

字典里为什么要有「责任方」
--------------------------
经办人拿到「跨日切分重复计入」这个结论，仍然要决定去找谁。字典把
``owner`` 与成因绑在一起，是为了让面板的成因分组**直接就是分工表** —— 20 条
单边账按成因一聚，就是「8 条找渠道方、6 条找收费处、4 条找信息科改口径、2 条
你自己判」。责任方猜错比成因猜错的代价更大：成因猜错只是多查一趟，责任方
猜错会让差异在两个岗位之间来回踢两周。

解释模板为什么不交给模型
------------------------
Task 28 让模型写 ``explanation_zh``，但那段的**句式**是自由的。本模块的模板
只让模型提供填空内容（金额、单号、证据行），句式与建议动作来自字典：同一类
成因在十期账上的说明读起来应当是同一种话术，而这套话术是财务科与信息科一起
定的，不是每次由模型现场生成。后者会让「上期说该找渠道方，这期说该找信息科」
变成一件无法解释的事。

四条不肯让的边界
----------------
1. **字典里没有的成因码一律判 ``draft_rejected``**，与「引用了不存在的行」
   同等处置（都是「模型说了个系统不认识的东西」，都不能收下）。**不做就近
   归并**：把「延迟入账」悄悄并进「渠道延迟入账」，会让「模型从没产出过
   新成因」这件事在指标表里消失 —— 而那恰恰是字典该被扩写的信号。
2. **两套分类不并存。** :class:`~reconq.align.taxonomy.AmbiguityKind`（对齐器
   视角的形态）与本模块的 :class:`CauseCode`（成因视角的编码）之间由
   :func:`cause_from_ambiguity_kind` **单向映射**而不是各写一份。形态判错
   话术就错，两套各自演进则台账上会出现「同一笔差异既是『一码付拆笔』又是
   『分摊未做』」两行。
3. **模板填空缺项即报错，不渲染半句。** ``{单号}`` 拿不到时报
   ``CAUSE_TEMPLATE_SLOT_UNKNOWN``，而不是渲染成「该笔金额 元」让经办人
   自己猜少了什么。
4. **字典版本进事件链，历史结论带当时版本。** :func:`record_dictionary_version`
   把字典指纹追加到该账期的事件链（只追加，永不 UPDATE），每条成因结论自带
   ``dict_version``。三个月后字典改了一条措辞，重算上周那期仍能答出「当时
   用的哪一版、这版的建议动作是什么」—— 否则「保证可复算」只是一句口号。

按成因聚合的口径
----------------
:func:`aggregate_causes` 出的是**条数 + 涉及金额 + 明细键**三样，按条数降序、
金额降序、编码升序排。条数与金额刻意**分列**：拆笔类成因一条就带几千块，
跨期类成因可能几十条加起来不到一百元 —— 只看金额会把「20 条小额渠道漏传」
排到「1 条大额拆笔」后面，而前者才是本周最该找渠道方谈的事。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层，也没有可能有**。``contract verify``
逐 check 出的 fail 行是**确定性判定**（「这一行的这一列不满足这条口径」），
不存在「为什么会有这一行」这个问题，自然也不需要成因集合、判别线索与责任方。
本模块沿用的仍是源产品主路径纪律 —— 判据收口在一处、结构不合约定就拒收、
结论交人工；新增的是**从「哪一行不合规」到「这件事归谁」**的这一跳。
"""

from __future__ import annotations

import hashlib
import json
import logging
import string
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..errors import ReconqError
from ..runlog import log_event
from .taxonomy import AmbiguityKind

__all__ = [
    "CAUSE_DICT_VERSION",
    "CAUSE_REJECT_CODE",
    "CauseAggregate",
    "CauseBucket",
    "CauseEntry",
    "CauseHit",
    "CauseOwner",
    "CauseRejection",
    "OWNER_ZH",
    "VersionVerdict",
    "aggregate_causes",
    "cause_from_ambiguity_kind",
    "cause_names",
    "cause_vocabulary",
    "dictionary_digest",
    "dictionary_payload",
    "dictionary_stamp",
    "entry_of",
    "explanation_template",
    "facts_from_ambiguity_verdict",
    "record_dictionary_version",
    "render_explanation",
    "required_slots",
    "resolve",
    "screen_hits",
    "version_check",
]

logger = logging.getLogger("reconq.align.causes")

#: 字典结构版本。**与内容指纹分开**：改了措辞（内容变、结构没变）bump
#: 指纹即可；新增 / 删除 / 改字段（结构变）才 bump 本常量。
CAUSE_DICT_VERSION = "cause-dict/1"

#: 未登记成因的拒因码。**与 Task 29 Schema 链的 ``cause_vocab`` 层同码** ——
#: 「模型说了个系统不认识的东西」是一个判据，写在两个地方各编一个码只会让
#: 排障时先花十分钟比对本该是同一件事的两条错误。
CAUSE_REJECT_CODE = "ATTRIBUTION_CAUSE_UNKNOWN"

#: 指纹取前 16 位十六进制。够短（结论单上放得下）、够长（改一个字必变）。
_DIGEST_LEN = 16


# ─────────────────────────── 责任方 ───────────────────────────


class CauseOwner(str, Enum):
    """建议责任方。**值即面板上显示的中文**。

    刻意只登记四个：科室交办、科室督办、医保办对接靠的就是这四类。列一串
    「信息科/财务科/渠道方/病案收费处/医务科/设备科…」看着周全，实际用起来
    是经办人自己挑 —— 挑了就不算数了。
    """

    INFO_DEPT = "信息科"
    FINANCE = "财务科"
    CHANNEL = "渠道方"
    CASHIER = "病案收费处"


#: 责任方枚举 → 说明面板上的一句话（谁该动、动了之后谁来对账）。
OWNER_ZH: dict[str, str] = {
    CauseOwner.INFO_DEPT.value: "接口或口径配置问题，信息科改完财务科复扫",
    CauseOwner.FINANCE.value: "账期与结算口径问题，财务科与收费处确认后重跑",
    CauseOwner.CHANNEL.value: "渠道侧结算与账单问题，凭结论单向渠道方发起查询",
    CauseOwner.CASHIER.value: "收费与退费业务问题，收费处补手续后财务科复扫",
}


# ─────────────────────────── 字典条目 ───────────────────────────


@dataclass(frozen=True)
class CauseEntry:
    """一条受控成因。

    :attr:`ambiguity_kind` 是与 :class:`~reconq.align.taxonomy.AmbiguityKind`
    的映射目标，**单向**：歧义形态 → 成因码。成因侧不反向登记形态，因为
    「渠道延迟入账」这类成因压根不是由歧义分型来的（它是单边账的成因，
    没有候选集可分）。
    """

    code: str
    zh_name: str
    owner: str
    clues: tuple[str, ...]
    suggested_action: str
    template: str
    ambiguity_kind: str = ""
    #: 该成因是「系统自己判出来的」还是「只能由模型/人工给」的。前者在
    #: 证据充分时可以直接落台账，后者一律待确认 —— 两类混在一起会让
    #: 「待确认」这个状态失去意义。
    deterministic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "zh_name": self.zh_name,
            "owner": self.owner,
            "clues": list(self.clues),
            "suggested_action": self.suggested_action,
            "template": self.template,
            "ambiguity_kind": self.ambiguity_kind,
            "deterministic": self.deterministic,
        }


#: 受控成因字典。**这一张表就是「什么叫单边账成因」的唯一定义**。
#:
#: 十条覆盖本产品七种对齐形态 + 账单取数 + 契约口径三类来源；末位
#: ``undetermined``（无法判定）是**必留**的：字典里没有「判不出来」这一项时，
#: 模型会被迫在十条里挑一条最像的，而「我判不出来」比「我判错了」贵得多 ——
#: 前者让人去查证，后者让人签字。
CAUSES: dict[str, CauseEntry] = {
    "channel_late_posting": CauseEntry(
        code="channel_late_posting",
        zh_name="渠道延迟入账",
        owner=CauseOwner.CHANNEL.value,
        clues=(
            "渠道账单里该笔交易时间落在账期窗内，但账单文件本身是账期结束后一天以上才出",
            "同渠道同期其他单号均已配上 HIS 单号，只有这一批缺",
            "下一期账单里能查到这笔的单号（下一期已配上）",
        ),
        suggested_action="向渠道方发起该笔结算查询，拿到结算单号后回填 HIS 对应关系",
        template=(
            "渠道侧已入账、HIS 侧查不到对应收费记录，属渠道结算入账延迟。"
            "本条涉及 {count} 笔、合计 {amount}，证据行：{evidence}。"
            "建议：{action}。责任方：{owner}。"
        ),
    ),
    "his_missing_order": CauseEntry(
        code="his_missing_order",
        zh_name="HIS 未落单",
        owner=CauseOwner.CASHIER.value,
        clues=(
            "渠道有单号与金额，HIS 侧该单号完全没有记录（不是金额不符，是查不到行）",
            "同期同科室其他缴费均已落单",
            "该笔对应的科室在账期内有正常的缴费流水",
        ),
        suggested_action="请收费处核查该笔是否漏做收费登记；确属漏登的补登记后复扫本期",
        template=(
            "渠道侧有这笔收费、HIS 侧完全没有对应记录（单号 {order_no}，{amount}），"
            "属 HIS 未落单。证据行：{evidence}。建议：{action}。责任方：{owner}。"
        ),
    ),
    "cross_period_duplicate": CauseEntry(
        code="cross_period_duplicate",
        zh_name="跨日切分重复计入",
        owner=CauseOwner.FINANCE.value,
        clues=(
            "该笔已按跨日切分口径被计入上一期，本期又被认领一次",
            "相邻两期账单里同一个单号各出现一次，金额相同",
            "跨日切分口径（周一 00:00 归新账期）在本期刚好命中边界",
        ),
        suggested_action="与财务科确认这笔应计入哪个账期；按结论调整跨日切分口径后重跑，不要手工挪账",
        template=(
            "这笔收费已按跨日切分口径计入相邻账期，本期被重复计入（单号 {order_no}，"
            "{amount}，账期 {period}）。证据行：{evidence}。建议：{action}。责任方：{owner}。"
        ),
    ),
    "refund_offset_in_his": CauseEntry(
        code="refund_offset_in_his",
        zh_name="退费已在 HIS 红冲",
        owner=CauseOwner.CASHIER.value,
        clues=(
            "同一单号同时存在原单行与退费（红冲）行",
            "渠道账单类型不含退款行，本期账单里看不到退费",
            "HIS 侧该单号的净额已扣掉退款",
        ),
        suggested_action="与收费处确认红冲是否已生效；若已生效，按账单类型重取（微信须用 bill_type=ALL）",
        template=(
            "该笔的退费已在 HIS 侧红冲，但渠道账单类型不含退款行，本期只看到原单"
            "（单号 {order_no}，{amount}）。证据行：{evidence}。建议：{action}。责任方：{owner}。"
        ),
        ambiguity_kind=AmbiguityKind.REFUND_COEXIST.value,
    ),
    "bill_type_excludes_refund": CauseEntry(
        code="bill_type_excludes_refund",
        zh_name="账单类型不含退款",
        owner=CauseOwner.INFO_DEPT.value,
        clues=(
            "本渠道本期取的是只含成功交易的账单（微信 bill_type=SUCCESS 一类）",
            "同期存在退费业务，但账单文件里没有对应的负额行",
            "上一期取的是含退款账单（bill_type=ALL），本期换成了不含退款的",
        ),
        suggested_action="把该渠道取件参数改回含退款的账单类型后重取本期（信息科改配置）",
        template=(
            "本期取到的账单类型不含退款行，退费业务在这份账单里看不到"
            "（单号 {order_no}，{amount}）。证据行：{evidence}。建议：{action}。责任方：{owner}。"
        ),
    ),
    "split_payment_unallocated": CauseEntry(
        code="split_payment_unallocated",
        zh_name="一码付拆笔未分摊",
        owner=CauseOwner.CASHIER.value,
        clues=(
            "一次渠道支付在 HIS 侧被按费用项目拆成多笔（常见于住院预交金）",
            "多笔明细金额加总与渠道那一笔相等（金额守恒）",
            "只有渠道侧有单号，HIS 侧各笔共用同一笔支付的单号",
        ),
        suggested_action="请收费处补充分摊说明，确认这几条 HIS 收费是否同属本次支付；属实的按同一成因结案",
        template=(
            "一次渠道支付在 HIS 侧被按费用项目拆成多笔，各笔加总与渠道侧相等"
            "（单号 {order_no}，渠道 {amount}，差额 {diff_amount}）。"
            "证据行：{evidence}。建议：{action}。责任方：{owner}。"
        ),
        ambiguity_kind=AmbiguityKind.SPLIT_PAYMENT.value,
    ),
    "merged_payment_allocation": CauseEntry(
        code="merged_payment_allocation",
        zh_name="合并支付分摊",
        owner=CauseOwner.CASHIER.value,
        clues=(
            "一笔（或数笔）渠道款覆盖了多条 HIS 收费，但两侧金额不守恒",
            "扇出侧各条金额不全相同（金额全同说明是导出重复，不是合并支付）",
            "同期有家属一次刷卡付多个单子的业务",
        ),
        suggested_action="与收费处确认该笔应覆盖哪几条 HIS 收费、分摊比例由谁定；分摊规则确认后再结案",
        template=(
            "一笔渠道款覆盖了多条 HIS 收费但两侧金额不守恒（单号 {order_no}，"
            "渠道 {amount}，差额 {diff_amount}）。证据行：{evidence}。"
            "建议：{action}。责任方：{owner}。"
        ),
        ambiguity_kind=AmbiguityKind.MERGED_PAYMENT.value,
    ),
    "period_boundary_landing": CauseEntry(
        code="period_boundary_landing",
        zh_name="账期边界落点差",
        owner=CauseOwner.FINANCE.value,
        clues=(
            "该笔收费时间落在账期半开窗口 [周一 00:00, 下周一 00:00) 的边界上",
            "两侧系统对「周一零点那一笔归哪期」的处理不一致",
            "相邻两期各记一次，合计金额本身没错",
        ),
        suggested_action="与财务科确认周一零点的归属规则，统一后调整账期口径；不要为了让账对平手工挪账",
        template=(
            "该笔收费时间落在账期 {period} 的边界上，两侧系统对归属期的处理不一致"
            "（单号 {order_no}，{amount}）。证据行：{evidence}。建议：{action}。责任方：{owner}。"
        ),
        ambiguity_kind=AmbiguityKind.CROSS_PERIOD.value,
    ),
    "contract_config_error": CauseEntry(
        code="contract_config_error",
        zh_name="对账口径配置错",
        owner=CauseOwner.INFO_DEPT.value,
        clues=(
            "该差异由契约里某条口径判出，而那条口径的取值与院内实际结算规程不符",
            "同类差异在本期成片出现，条数接近全渠道笔数",
            "换用上一期的同渠道契约重跑，该差异消失",
        ),
        suggested_action="核对 contracts/<渠道>.yml 里报错那条口径的取值，改完重跑本期（改口径要留版本）",
        template=(
            "该差异由本渠道契约口径判出，而口径取值与院内实际结算规程不符"
            "（单号 {order_no}，{amount}，差额 {diff_amount}）。"
            "证据行：{evidence}。建议：{action}。责任方：{owner}。"
        ),
    ),
    "undetermined": CauseEntry(
        code="undetermined",
        zh_name="无法判定",
        owner=CauseOwner.FINANCE.value,
        clues=(
            "候选金额全相同且与对侧不守恒（信息已耗尽，判不出是不是同一笔）",
            "只靠金额 + 时间窗的弱匹配候选，缺 HIS 单号",
            "账单列缺列导致关键判据字段没有值",
        ),
        suggested_action="由经办人人工判定：向导出方索取完整单号，或由收费员确认；系统不猜",
        template=(
            "现有账单列不足以判别成因：{clues}（单号 {order_no}，{amount}）。"
            "证据行：{evidence}。建议：{action}。责任方：{owner}。"
        ),
    ),
}

#: 歧义形态 → 成因码。**单一映射源**，不做「各写一份然后人工对齐」。
#:
#: ``UNKNOWN``（歧义分型的判不出来）映射到 ``undetermined`` 而不是留空：
#: 两者说的是同一件事 —— 都是「系统判不出来，交给人」。留空会让台账上出现
#: 一批没有成因码的歧义条目，聚合时不知道该归到哪一组。
_AMBIGUITY_TO_CAUSE: dict[str, str] = {
    entry.ambiguity_kind: entry.code for entry in CAUSES.values() if entry.ambiguity_kind
}


# ─────────────────────────── 字典读取 ───────────────────────────


def cause_vocabulary() -> tuple[str, ...]:
    """传给 :mod:`reconq.gateway.schema` ``vocabulary=`` 的成因码元组。

    **顺序稳定**（按 :data:`CAUSES` 的声明顺序），不排序 —— 提示词里给出的
    顺序就是给模型看的建议优先级（渠道延迟入账与 HIS 未落单在前，配置类在
    后），排序会把「最常见的成因」排到字典后面。
    """
    return tuple(CAUSES)


def cause_names() -> tuple[str, ...]:
    """``编码 + 中文名`` 的展示串（面板分组标题、结论单用语）。"""
    return tuple(f"{entry.zh_name}（{code}）" for code, entry in CAUSES.items())


def entry_of(code: str) -> CauseEntry:
    """取字典条目；未登记抛 :class:`~reconq.errors.ReconqError`。

    **不返回 ``None``**：调用方若不判就会拿一个空条目去渲染说明，产出
    「：本条涉及  笔、合计  元」这种没法看的东西。抛出来才会被归因链记成
    拒因。
    """
    entry = CAUSES.get((code or "").strip())
    if entry is None:
        raise ReconqError(
            CAUSE_REJECT_CODE,
            f"cause={code!r} 不在受控成因字典内；字典现有 {len(CAUSES)} 项，"
            f"可用编码见 reconq.align.causes.cause_vocabulary()。"
            f"系统不把未登记成因就近归并 —— 请把该成因补进字典后重跑",
        )
    return entry


#: 保留一个短别名给内部与测试用（不导进 ``__all__`` 的公开面）。
resolve = entry_of


# ─────────────────────────── 解释模板 ───────────────────────────

#: 模板里由 :attr:`CauseEntry` 自己提供的槽位，调用方**不传**。
_AUTO_SLOTS = ("cause", "action", "owner")

#: 模板里必须由调用方提供的槽位 → 缺项时的中文说明。
_REQUIRED_SLOT_ZH: dict[str, str] = {
    "count": "条数",
    "amount": "金额",
    "diff_amount": "差额",
    "evidence": "证据行",
    "order_no": "单号",
    "period": "账期",
    "clues": "判别线索",
}

#: **允许为空值**的槽位：空不是「缺项」，而是「这一栏在这条成因上根本没有
#: 这个量」。只登记这一个 —— 放宽到「所有槽位都可空」，模板就会渲染出
#: 「该笔金额 元、单号 、证据行：」这种让人以为系统没查的半句。
#:
#: ``diff_amount`` 之所以允许空：渠道延迟入账这类成因压根没有「两侧相差
#: 多少」这个量（它是一笔压根没落单的款，不是两笔对不上）。渲染成 0.00 元
#: 会被读成「两侧恰好相等」，那比留空错得更远。
_OPTIONAL_SLOTS: frozenset[str] = frozenset({"diff_amount"})


def explanation_template(code: str) -> str:
    """取该成因的中文说明模板（原样，含 ``{}`` 占位）。"""
    return entry_of(code).template


def required_slots(code: str) -> tuple[str, ...]:
    """该模板要求调用方提供的槽位（自动槽位不在其中）。"""
    template = entry_of(code).template
    slots = set()
    for _literal, field_name, _spec, _conv in string.Formatter().parse(template):
        if field_name:
            slots.add(field_name.split(".")[0].split("[")[0])
    return tuple(sorted(slots - set(_AUTO_SLOTS)))


def _render_slots(
    code: str, entry: CauseEntry, facts: Mapping[str, Any]
) -> dict[str, str]:
    """把 ``facts`` 规整成模板可 ``format_map`` 的字符串字典。

    金额一律走 :class:`~decimal.Decimal`：模板里的 ``{amount}`` 若用 ``float``
    填，``1020.00`` 会渲染成 ``1020.0`` —— 结论单上少两位小数，财务科第一眼
    会以为抄错了。
    """
    out: dict[str, str] = {
        "cause": entry.zh_name,
        "action": entry.suggested_action,
        "owner": f"{entry.owner}（{OWNER_ZH.get(entry.owner, '')}）",
    }
    for slot in required_slots(code):
        missing = slot not in facts or facts[slot] in (None, "")
        if missing and slot not in _OPTIONAL_SLOTS:
            raise ReconqError(
                "CAUSE_TEMPLATE_SLOT_UNKNOWN",
                f"成因 {code!r}（{entry.zh_name}）的说明模板需要 "
                f"{{{slot}}}（{_REQUIRED_SLOT_ZH.get(slot, slot)}），"
                f"本次未提供。渲染半句会让人误以为系统没查 —— 请补齐该槽位，"
                f"确无可填内容时显式传占位值而不是省略",
            )
        if slot in _OPTIONAL_SLOTS and facts.get(slot) in (None, ""):
            out[slot] = _NO_DIFF
            continue
        out[slot] = _slot_text(slot, facts[slot])
    return out


#: 「这一栏没有这个量」时的显式渲染。**刻意不写 0.00** —— 0.00 会被读成
#: 「两侧恰好相等」，而这一栏在多数成因上根本没有这个量。
_NO_DIFF = "（无差额）"


def _slot_text(slot: str, value: Any) -> str:
    if slot == "count":
        return str(int(value))
    if slot in ("amount", "diff_amount"):
        return _money_text(value)
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(item) for item in value) if value else "（未提供）"
    return str(value)


#: 金额槽位渲染时**自带单位**（模板里不再写「元」）。
#:
#: 写在模板里的做法有个具体的坏处：``{diff_amount} 元`` 在无差额时渲染成
#: 「（无差额） 元」，括号里已经说了没有，尾巴上再挂一个「元」会让读的人
#: 再愣一下。单位跟着值走，模板就只管句式。
_UNIT = " 元"


def _money_text(value: Any) -> str:
    """金额定点两位并带单位。**float 入参显式拒绝**。

    两道关卡都必要：float 悄悄格式化会让 ``1020.00`` 渲染成 ``1020.0``（抄错
    的错觉），而 ``0.1 + 0.2`` 渲染成 ``0.30000000000000004`` 会让财务科
    以为账单本身有问题。不可解析同样报错 —— 渲染成 ``0.00`` 会被当成
    「差额就是零」。
    """
    if isinstance(value, float):
        raise ReconqError(
            "CAUSE_TEMPLATE_SLOT_UNKNOWN",
            f"模板槽位金额收到 float：{value!r}。模板槽位一律走 Decimal —— "
            f"float 会让 1020.00 渲染成 1020.0，且 0.1+0.2 会渲染成 "
            f"0.30000000000000004",
        )
    if isinstance(value, Decimal):
        return f"{value:.2f}{_UNIT}"
    text = str(value).strip()
    if text in ("", "-"):
        return _NO_DIFF if text == "" else text
    try:
        return f"{Decimal(text):.2f}{_UNIT}"
    except InvalidOperation as exc:
        raise ReconqError(
            "CAUSE_TEMPLATE_SLOT_UNKNOWN",
            f"模板槽位金额不可解析为定点数：{value!r}（{exc}）。"
            f"模板槽位一律走 Decimal 字符串",
        ) from exc


#: 全字典用到的槽位全集（自动槽位 + 各模板要求的槽位的并集）。
#:
#: :func:`render_explanation` 的严格检查按**这张全集**判，而不是按当前模板
#: 的槽位 —— 调用方一次构造好一份 ``facts`` 传给多条成因是常态
#: （聚合前逐条渲染），按单模板判会把「这份 facts 里带了别的成因也用得上的
#: ``period``」误报成拼写错误，那样的报错会逼调用方为每条成因重建一份
#: facts，最终让「一套 facts 传到底」这个正确用法消失。
_ALL_SLOTS: frozenset[str] = frozenset(_AUTO_SLOTS).union(
    slot for code in CAUSES for slot in required_slots(code)
)


def render_explanation(
    code: str,
    facts: Mapping[str, Any],
    *,
    strict: bool = True,
) -> str:
    """按 ``code`` 的模板填 ``facts``，出一句中文说明。

    :param strict: ``facts`` 里不得出现**全字典都没有**的槽位名。关掉它
        （调试用）会让 ``{typo}`` 这类拼写错误静默丢弃 —— 默认开着。
    """
    entry = entry_of(code)
    slots = _render_slots(code, entry, facts)
    if strict:
        # 查的是**调用方传进来的键**（含当前模板没用上的），不是 ``slots``
        # —— 后者只装已渲染的槽位，拼错的名字根本不会出现在里面，这条检查
        # 就成了永远为真的空话。
        unknown = set(map(str, facts)) - _ALL_SLOTS
        if unknown:
            raise ReconqError(
                "CAUSE_TEMPLATE_SLOT_UNKNOWN",
                f"成因 {code!r} 的模板家族没有这些槽位：{sorted(unknown)}；"
                f"全字典可用槽位 {sorted(_ALL_SLOTS)}。多传槽位通常是调用方"
                f"拼错了占位名 —— 静默丢弃会让说明少掉一句关键信息",
            )
    # 成因名做前缀而不是只塞在句子里：同一句说明在台账、面板、结论单三处
    # 出现时，前缀让「这属于哪一类」不必读到句尾才知道，也保证按成因聚合
    # 出来的每一桶在导出的清单里都自带标签。
    return f"【{entry.zh_name}】{entry.template.format_map(slots)}"


def facts_from_ambiguity_verdict(verdict: Any, *, amount: Any = "") -> dict[str, Any]:
    """从 :class:`~reconq.align.taxonomy.TaxonomyVerdict` 取模板槽位。

    ``amount`` **由调用方给**：分型结论只记两侧差额（守恒时为 0），不记
    「这笔本身多少钱」—— 把它从差额反推出来是编的。调用方拿不到就留空，
    由 :func:`render_explanation` 点名报错（该成因模板用到 ``{amount}`` 时）。

    槽位取不到的**不编占位值**：``evidence`` 真的空着时给一句
    「（证据行未随结论传来）」是为了让渲染结果读起来知道**系统没拿到**，
    而不是伪造一条行号。
    """
    evidence = tuple(getattr(verdict, "evidence", ()) or ())
    facts: dict[str, Any] = {
        "count": len(evidence) or 1,
        "order_no": getattr(verdict, "order_no", "") or "",
        "period": getattr(verdict, "period", "") or "",
        "evidence": list(evidence) or ["（证据行未随结论传来）"],
        "clues": list(getattr(verdict, "factors", ()) or ()) or ["证据包内无可用判别线索"],
    }
    if amount != "" and amount is not None:
        facts["amount"] = amount
    # 「无差额」不等于 0.00 —— 0.00 会被读成「两侧恰好相等」，而这一栏在
    # 多数成因上根本没有这个量。守恒为 0 时留空，模板会渲染成「（无差额）」。
    diff_amount = getattr(verdict, "diff_amount", "") or ""
    facts["diff_amount"] = diff_amount if diff_amount not in ("0", "0.00") else ""
    return facts


# ─────────────────────────── 成因与形态的映射 ───────────────────────────


def cause_from_ambiguity_kind(kind: str) -> str:
    """歧义形态 → 成因码。

    未登记的形态（含 :attr:`~reconq.align.taxonomy.AmbiguityKind.UNKNOWN`
    之外的任意字符串）一律落 ``undetermined``，**不 KeyError**：
    形态是内部枚举，成因码是要落台账的对外字段，中间不该有一个会抛异常的
    接缝 —— 抛了就是「升级 align 模块时台账写不进去」。
    """
    if kind == AmbiguityKind.UNKNOWN.value:
        return "undetermined"
    return _AMBIGUITY_TO_CAUSE.get(kind, "undetermined")


# ─────────────────────────── 成因命中与聚合 ───────────────────────────


@dataclass(frozen=True)
class CauseHit:
    """一条按成因归好的差异。聚合与版本留痕的最小单位。"""

    cause_code: str
    period: str = ""
    channel: str = ""
    #: 台账唯一键（``taxonomy.ambiguity:<单号>`` 一类）。聚合结果要能
    #: 点回台账，光有编码与金额点不回去。
    conclusion_key: str = ""
    order_no: str = ""
    #: 涉及金额（``Decimal``）。空串表示这条成因没有金额量。
    amount: str = ""
    #: 成因排序内的名次（1 = Top-1）。同一条差异有多条成因草稿时，
    #: 默认只统计 Top-1 —— 把三条候选都计进条数会让「本期 20 条单边账」
    #: 变成 60 条，面板上的数字会骗人。
    rank: int = 1
    #: 作出该结论时**用的字典版本**。历史结论靠它复算。
    dict_version: str = ""
    confidence: str = ""

    @property
    def entry(self) -> CauseEntry:
        return CAUSES.get(self.cause_code) or entry_of(self.cause_code)

    @property
    def zh_name(self) -> str:
        entry = CAUSES.get(self.cause_code)
        return entry.zh_name if entry else f"未登记成因（{self.cause_code}）"

    def amount_decimal(self) -> Optional[Decimal]:
        if self.amount in ("", None):
            return None
        try:
            return Decimal(str(self.amount))
        except InvalidOperation:
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_code": self.cause_code,
            "zh_name": self.zh_name,
            "period": self.period,
            "channel": self.channel,
            "conclusion_key": self.conclusion_key,
            "order_no": self.order_no,
            "amount": self.amount,
            "rank": self.rank,
            "dict_version": self.dict_version,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class CauseBucket:
    """一个成因下的全部差异。"""

    cause_code: str
    zh_name: str
    owner: str
    count: int
    amount: str
    keys: tuple[str, ...] = ()

    @property
    def owner_note(self) -> str:
        return OWNER_ZH.get(self.owner, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_code": self.cause_code,
            "zh_name": self.zh_name,
            "owner": self.owner,
            "owner_note": self.owner_note,
            "count": self.count,
            "amount": self.amount,
            "keys": list(self.keys),
        }


@dataclass(frozen=True)
class CauseAggregate:
    """本期按成因聚合的结果。"""

    period: str
    buckets: tuple[CauseBucket, ...] = ()
    #: 未登记成因的条数。**恒为 0**（未登记成因在 :func:`screen_hits` 阶段
    #: 就被拒了），保留这个字段是为了让「有没有绕过受控字典」这个问题在
    #: 面板上有一个可以恒为 0 的位置 —— 一旦它不为 0，说明有人绕过字典直接
    #: 落库了。
    unregistered: int = 0
    dict_version: str = ""
    dict_digest: str = ""
    total_count: int = 0
    total_amount: str = "0.00"

    @property
    def top_cause(self) -> Optional[CauseBucket]:
        return self.buckets[0] if self.buckets else None

    def by_owner(self) -> dict[str, int]:
        """责任方 → 条数。**这张表就是分工表**。"""
        out: dict[str, int] = {}
        for bucket in self.buckets:
            out[bucket.owner] = out.get(bucket.owner, 0) + bucket.count
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "dict_version": self.dict_version,
            "dict_digest": self.dict_digest,
            "total_count": self.total_count,
            "total_amount": self.total_amount,
            "unregistered": self.unregistered,
            "by_owner": self.by_owner(),
            "buckets": [b.to_dict() for b in self.buckets],
        }

    def render_zh(self) -> str:
        """面板一屏（成因条数与金额 + 分工表）。"""
        if not self.buckets:
            return f"{self.period} 单边账成因聚合：本期无按成因归好的差异"
        lines = [
            f"{self.period} 单边账成因聚合（字典 {self.dict_version}，"
            f"共 {self.total_count} 条、{self.total_amount} 元）："
        ]
        for bucket in self.buckets:
            lines.append(
                f"  · {bucket.zh_name} {bucket.count} 条 / {bucket.amount} 元"
                f" → {bucket.owner}"
            )
        owners = "、".join(f"{owner} {n} 条" for owner, n in self.by_owner().items())
        if owners:
            lines.append(f"  分工：{owners}")
        if self.unregistered:  # pragma: no cover - 恒为 0，保留显式分支
            lines.append(
                f"  ! 有 {self.unregistered} 条绕过了受控字典落库，请检查归因链"
            )
        return "\n".join(lines)


def aggregate_causes(
    hits: Sequence[CauseHit], *, period: str = "", top_rank_only: bool = True
) -> CauseAggregate:
    """按成因聚合。

    :param top_rank_only: 只统计每条差异的 Top-1 成因。**默认开着** ——
        一条差异的成因排序有 1~5 条，全计会让条数虚高到 2~3 倍。关掉它只
        该出现在「看模型给了几条候选」的分析里，不该出现在面板上。
    """
    chosen: list[CauseHit] = []
    best_rank: dict[str, CauseHit] = {}
    for hit in hits:
        if not top_rank_only:
            # 全量口径：一条差异的几条候选全算，不做 Top-1 收敛。
            chosen.append(hit)
            if hit.conclusion_key:
                best_rank.setdefault(hit.conclusion_key, hit)
            continue
        if hit.rank > 1:
            # 同一条差异的次位成因：若 Top-1 已在则忽略；若 Top-1 还没出现
            # （调用方只给了候选 2、3 的切片），仍收下并把名次归一，
            # 避免「没有 Top-1 就整条丢失」。
            if hit.conclusion_key in best_rank:
                continue
            hit = CauseHit(
                cause_code=hit.cause_code,
                period=hit.period,
                channel=hit.channel,
                conclusion_key=hit.conclusion_key,
                order_no=hit.order_no,
                amount=hit.amount,
                rank=1,
                dict_version=hit.dict_version,
                confidence=hit.confidence,
            )
        elif hit.conclusion_key and hit.conclusion_key in best_rank:
            continue
        chosen.append(hit)
        if hit.conclusion_key:
            best_rank[hit.conclusion_key] = hit

    grouped: dict[str, list[CauseHit]] = {}
    for hit in chosen:
        grouped.setdefault(hit.cause_code, []).append(hit)

    buckets: list[CauseBucket] = []
    total = Decimal("0")
    unregistered = 0
    for code, items in grouped.items():
        entry = CAUSES.get(code)
        if entry is None:
            unregistered += len(items)
            continue
        amount = Decimal("0")
        has_amount = False
        for item in items:
            value = item.amount_decimal()
            if value is not None:
                amount += value
                has_amount = True
        buckets.append(
            CauseBucket(
                cause_code=code,
                zh_name=entry.zh_name,
                owner=entry.owner,
                count=len(items),
                amount=f"{amount:.2f}" if has_amount else "",
                keys=tuple(i.conclusion_key for i in items if i.conclusion_key),
            )
        )
        if has_amount:
            total += amount
    # 条数降序 → 金额降序 → 编码升序。第三级不是装饰：两个成因条数与金额
    # 都相同时，顺序必须由字典自身决定，否则同一份数据两次聚合的面板排序
    # 会不同，截图对不上。
    buckets.sort(key=lambda b: (-b.count, -(Decimal(b.amount or "0")), b.cause_code))
    if not period:
        period = next((h.period for h in chosen if h.period), "")
    return CauseAggregate(
        period=period,
        buckets=tuple(buckets),
        unregistered=unregistered,
        dict_version=dictionary_stamp(),
        dict_digest=dictionary_digest(),
        total_count=sum(b.count for b in buckets),
        total_amount=f"{total:.2f}",
    )


# ─────────────────────────── 受控筛查 ───────────────────────────


@dataclass(frozen=True)
class CauseRejection:
    """未登记成因的拒因。与 :class:`~reconq.align.attribution.RejectedDraft`
    同形（刻意不 import 那个模块以免形成反向依赖）。"""

    cause: str
    error_code: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"cause": self.cause, "error_code": self.error_code, "detail": self.detail}


def screen_hits(
    candidates: Iterable[Any], *, period: str = "", default_rank: int = 1
) -> tuple[list[CauseHit], list[CauseRejection]]:
    """把归因产物筛成受控成因命中 + 拒因。

    **逐条独立**：Top-1 说了个字典外的成因，Top-2 说得对，就把 Top-2 收下
    —— 把整批丢掉等于把唯一说对的那条也扔了。经办人看到的是「收下 1 条、
    丢弃 1 条、丢弃原因：cause_vocab」，不是「这一条什么都没给」。
    """
    kept: list[CauseHit] = []
    rejected: list[CauseRejection] = []
    known = set(CAUSES)
    for item in candidates:
        code = str(_get(item, "cause_code") or _get(item, "cause") or "").strip()
        if not code:
            rejected.append(
                CauseRejection(
                    cause="",
                    error_code=CAUSE_REJECT_CODE,
                    detail="归因产物没有 cause 字段：无成因不可归组，整条作废",
                )
            )
            continue
        if code not in known:
            rejected.append(
                CauseRejection(
                    cause=code,
                    error_code=CAUSE_REJECT_CODE,
                    detail=(
                        f"cause={code!r} 不在受控成因字典内（现有 {len(known)} 项）。"
                        f"整条作废 —— 不做就近归并；请把该成因补进 "
                        f"reconq.align.causes.CAUSES 后重跑"
                    ),
                )
            )
            continue
        confidence = _get(item, "confidence")
        kept.append(
            CauseHit(
                cause_code=code,
                period=str(_get(item, "period") or period or ""),
                channel=str(_get(item, "channel") or ""),
                conclusion_key=str(_get(item, "conclusion_key") or ""),
                order_no=str(_get(item, "order_no") or ""),
                amount=_amount_text(_get(item, "amount") or _get(item, "diff_amount")),
                rank=int(_get(item, "rank") or default_rank or 1),
                dict_version=str(_get(item, "dict_version") or ""),
                confidence=str(confidence) if confidence is not None else "",
            )
        )
    return kept, rejected


def _amount_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    text = str(value).strip()
    try:
        return f"{Decimal(text):.2f}"
    except InvalidOperation:
        return text


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


# ─────────────────────────── 字典版本与事件链 ───────────────────────────

#: 事件名。登记在 :data:`reconq.runlog.LOG_EVENTS` 里，未登记的事件会被
#: 降级成 ``unknown`` 并把原名塞进 ``unregistered_event`` 字段。
DICT_EVENT = "cause.dict.version"


def dictionary_payload() -> dict[str, Any]:
    """字典的规范化内容（**排序后的 JSON**，不取构造时刻）。

    刻意不含时间戳：指纹要回答的是「这两版字典是不是同一套判据」，把时刻
    算进去会让每次调用都变一个哈希，历史版本比对全废。
    """
    return {code: entry.to_dict() for code, entry in sorted(CAUSES.items())}


def dictionary_digest() -> str:
    """字典内容指纹（sha256 前 16 位）。"""
    blob = json.dumps(
        dictionary_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


def dictionary_stamp() -> str:
    """``版本+指纹`` 合成串，落进每条成因结论。"""
    return f"{CAUSE_DICT_VERSION}+{dictionary_digest()}"


@dataclass(frozen=True)
class VersionVerdict:
    """字典版本核对结论。"""

    ok: bool
    current: str
    recorded: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "current": self.current,
            "recorded": self.recorded,
            "reason": self.reason,
            "detail": dict(self.detail),
        }

    def render_zh(self) -> str:
        if self.ok:
            return f"受控成因字典版本一致（{self.current}）"
        return f"受控成因字典版本不一致：当前 {self.current}，该账期当时记的是 {self.recorded or '（未记）'}。{self.reason}"


def record_dictionary_version(
    conn: Any,
    period: str,
    *,
    run_id: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """把当前字典版本追加到该账期的事件链。

    **幂等**：本账期链尾已经记着同一指纹时不再追加。重复记账本身不是错误，
    但会让「本周改了几次字典」这个问题答成「扫了几次账」。

    与 :mod:`reconq.engine.resume` 的事件链同一条（append-only + sha256
    链式指纹），不在另开一张表 —— 结论单导出时只走一条链，且删掉中间任何
    一环后面的哈希就接不上。
    """
    from ..engine.resume import append_scan_event, list_scan_events  # 局部 import：align 不反向依赖 engine

    stamp = dictionary_stamp()
    if not period:
        raise ReconqError(
            "CAUSE_DICT_VERSION_INVALID",
            "记账期不能为空：字典版本要挂到某个账期的事件链上才能复算，"
            "不指定账期等于记到虚空",
        )
    for event in reversed(list_scan_events(conn, period)):
        if event.event == DICT_EVENT:
            if (event.scope_data or {}).get("stamp") == stamp:
                return {
                    "recorded": False,
                    "stamp": stamp,
                    "reason": "本账期事件链链尾已是同一字典版本，未重复追加",
                    "event": event.to_dict(),
                }
            break
    event = append_scan_event(
        conn,
        period,
        DICT_EVENT,
        run_id=run_id,
        actor=actor,
        scope="cause_dictionary",
        scope_data={
            "stamp": stamp,
            "version": CAUSE_DICT_VERSION,
            "digest": dictionary_digest(),
            "cause_count": len(CAUSES),
            "codes": list(CAUSES),
        },
        result="recorded",
        detail=f"受控成因字典 {CAUSE_DICT_VERSION}（{len(CAUSES)} 项，指纹 {dictionary_digest()}）",
    )
    log_event(
        DICT_EVENT,
        message=f"受控成因字典版本入事件链：{stamp}",
        period=period,
        code="",
        dict_version=CAUSE_DICT_VERSION,
        dict_digest=dictionary_digest(),
        cause_count=len(CAUSES),
        event_seq=event.seq,
        event_hash=event.hash,
    )
    return {
        "recorded": True,
        "stamp": stamp,
        "reason": "",
        "event": event.to_dict(),
    }


def version_check(conn: Any, period: str) -> VersionVerdict:
    """核对本账期链上记录的字典版本与当前代码里的版本。

    **不一致不抛异常**：它是一件该被看见、该被记录的事（本期有人扩了字典），
    不是一次运行失败。复算上周那期时会撞上它，那时真正该问的是「上周用
    哪一版判的」。
    """
    from ..engine.resume import list_scan_events  # 局部 import：同上

    current = dictionary_stamp()
    recorded = ""
    seq = 0
    for event in list_scan_events(conn, period):
        if event.event == DICT_EVENT:
            recorded = str((event.scope_data or {}).get("stamp", ""))
            seq = event.seq
    if not recorded:
        return VersionVerdict(
            ok=False,
            current=current,
            recorded="",
            reason="该账期事件链上没有字典版本记录（可能未接归因链），"
            "复算时无法确认当时用的是哪一版",
            detail={"scanned": True},
        )
    if recorded == current:
        return VersionVerdict(
            ok=True, current=current, recorded=recorded, detail={"event_seq": seq}
        )
    return VersionVerdict(
        ok=False,
        current=current,
        recorded=recorded,
        reason=f"字典内容在链上第 {seq} 环之后发生过变更；"
        f"当时那期结论按「{recorded}」判出，与当前代码的「{current}」不同，"
        f"复算时按链上记录的版本解释，不要拿当前措辞去覆盖历史结论",
        detail={"event_seq": seq},
    )
