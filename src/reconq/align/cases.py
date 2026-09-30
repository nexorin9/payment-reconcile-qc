"""对齐黄金样例回归集（Task 40）。

这份模块解决的是**回归保障**，不是**指标**：它回答「改了这版代码，有没有把
原来对的东西改坏」。散在十几个测试文件里的对齐用例收成一份可枚举的黄金集，
任何改动跑一遍就知道坏了哪条、坏在哪一种形态上。

与 Task 38 的 :mod:`reconq.align.metrics` 金标集**刻意不合并**
----------------------------------------------------------------
两者回答的问题不同，混在一起会互相拖累：

============================  ================================================
:mod:`reconq.align.metrics`   本模块（cases.yml 回归集）
============================  ================================================
回答「本产品比基础方案准不准」  回答「这次改动有没有弄坏原来对的」
衡量召回 / 误报 / 形态命中率    衡量**按形态**的回归面 + 证据行号稳定性
条数越多指标越可信            **每种形态**留够样本才可信
一条 matched 错了掉 1% 召回    一条四形态错了 = 一类差异整体判错方向
============================  ================================================

换句话说：指标金标集里 ``matched`` 占三成是正常的（一半 case 本来就该对齐），
而回归集里 ``matched`` 只是 ``single`` 形态的一部分，真正要守住的是四种
歧义形态 —— 那四种一旦判错方向，结论单上整类差异的排查建议都是错的。

**四种歧义形态的覆盖门禁**
------------------------
``one_code_multi_settle`` / ``merged`` / ``cross_period`` /
``refund_coexist`` 各自至少 :data:`MIN_CASES_PER_AMBIGUITY_FORM` 条，
缺一即加载失败。这道门禁挡的是最常见的退化：新增了「让四形态判得更细」的
逻辑，只在已经覆盖的形态上验证过，于是**另一种形态悄悄判错了半年没人发现**。
门禁在**加载期**就拒（:func:`load_case_set` 抛错），而不是跑完 40 条再告诉你
「其中一种形态一条都没有」—— 后者容易被人当成「这次改动没问题」。

**新增用例登记规范**
------------------
发现新边界形态时：**先**在 ``cases.yml`` 加 case（哪怕先把 ``expect`` 写成
现状、并用 ``note`` 说明它是错的），跑一次让回归集红，再改
:mod:`reconq.align.matcher` / :mod:`reconq.align.ambiguity` 让它绿。
顺序反过来了就会出现「代码已按新形态写、回归集里却一条都没有」—— 那种改动
下次重构就被无声抹掉。形态标签是受控词表（:data:`CASE_FORMS`），写错在加载期
报 ``ALIGN_CASE_FORM_UNKNOWN`` 并列出全部合法标签，不会被当成新形态静默接受；
要新增形态必须先在 :data:`CASE_FORMS` 与 :data:`FORM_ZH` 登记。

源产品对照
----------
参考实现（``soda-core``）**没有这一层**，也没有对应物：它只有单 dataset 内的
逐列 check 与契约 verify 的退出码，不做实体对齐、更没有「按形态守住回归面」
的概念。本模块沿用的仍是主路径纪律——**跑真实对齐器、逐条比对、失败时交出
case_id 与形态标签**（源产品 ``exit_with_code()`` 的门禁语义在这里被改写成
「退出码非零 + 逐条可定位」）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional, Sequence

import yaml

from ..errors import ReconqError
from ..sources.timezone import period_window, to_session_tz
from .ambiguity import AmbiguityKind
from .matcher import (
    STATUS_ZH,
    AlignRecord,
    MatchOutcome,
    MatchStatus,
    align_all,
)
from .taxonomy import case_from_verdict, classify_taxonomy

__all__ = [
    "CASE_FORMS",
    "FOUR_AMBIGUITY_FORMS",
    "FORM_ZH",
    "MIN_CASES",
    "MIN_CASES_PER_AMBIGUITY_FORM",
    "DEFAULT_CASES_PATH",
    "CaseExpectation",
    "AlignCase",
    "CaseSet",
    "CaseOutcome",
    "RegressionReport",
    "load_case_set",
    "run_case",
    "run_regression",
    "render_regression_report",
    "write_regression_report",
]

#: 回归集默认路径（相对项目根）
DEFAULT_CASES_PATH = os.path.join("tests", "fixtures", "align", "cases.yml")

#: 回归集条数下限。低于这个数，「跑一遍没报错」说明不了任何东西。
MIN_CASES = 40

#: **四种歧义形态** —— 医院里能当场对上号、且判错方向代价最大的四类。
#: 与 :mod:`reconq.align.taxonomy` 的四种成因一一对应（外加 ``unknown``
#: 作为「信息耗尽」的诚实落点，不单列形态）。
FOUR_AMBIGUITY_FORMS: tuple[str, ...] = (
    "one_code_multi_settle",
    "merged",
    "cross_period",
    "refund_coexist",
)

#: 四种歧义形态各自的最少用例数。**缺一即加载失败**（见模块头）。
MIN_CASES_PER_AMBIGUITY_FORM = 4

#: 受控形态词表。**刻意不含**「其他 / misc / unknown」这类兜底标签：
#: 兜底标签会让新形态悄悄塞进已有形态那一格，覆盖门禁也就跟着失效了。
#: 判不出来的形态请如实新增一条（并同步 :data:`FORM_ZH` 与门禁口径）。
CASE_FORMS: tuple[str, ...] = (
    "single",
    "one_code_multi_settle",
    "merged",
    "cross_period",
    "refund_coexist",
    "weak_match",
    "dirty_row",
)

#: 形态 → 中文名 + 一句话「这一类现场长什么样」。报告、失败信息与
#: 「这次坏了哪一类」都从这里取，不在别处各写一份。
FORM_ZH: dict[str, tuple[str, str]] = {
    "single": ("一对一 / 明确单边", "单号精确命中唯一一条 HIS 记录，或确实只有一侧有"),
    "one_code_multi_settle": (
        "一码付拆多笔",
        "一条渠道订单号在 HIS 侧有多条收费记录，两侧金额守恒（住院预交金按费用项目拆）",
    ),
    "merged": (
        "合并支付",
        "一条渠道订单号覆盖多条 HIS 收费但金额不守恒，且扇出侧金额不全相同（家属一次刷多单）",
    ),
    "cross_period": (
        "跨账期落点",
        "候选记录的收费时间落在本账期窗口之外（周一 00:00 边界或账单文件晚到）",
    ),
    "refund_coexist": (
        "退费与原单并存",
        "同一单号同时存在原单与退费行；请确认红冲是否已生效",
    ),
    "weak_match": (
        "弱匹配待人工",
        "渠道行缺单号或单号未命中，只能按金额 + 账期时间窗找候选；唯一命中也不认领",
    ),
    "dirty_row": (
        "脏数据下的结论稳定性",
        "单号带空格 / 两侧单号皆空 / 缺收费时刻 / 同一单号重复导出 —— 结论不得因此翻转",
    ),
}

#: 允许出现的 ``expect.status``（与 :class:`~reconq.align.matcher.MatchStatus`
#: 同空间）。刻意与指标层的 ``his_only`` 分开：回归集比的是**单行结论**，
#: 反向单边账由 ``expect.his_only_rows`` 单独断言。
_EXPECT_STATUSES = frozenset(s.value for s in MatchStatus)


def _err(code: str, detail: str) -> ReconqError:
    return ReconqError(code, detail)


# ───────────────────────────── 用例模型 ─────────────────────────────


@dataclass(frozen=True)
class CaseExpectation:
    """一条 case 的期望结论。

    五个字段分属三个层次，**逐层收紧**：状态对不对 → 歧义形态判对没 →
    证据行号给没给全。少断言一层，「修好一形态、弄坏另一形态」就可能溜过去。

    ``taxonomy_kind`` 留空表示**这一层不参与判定**（匹配 / 金额不符 /
    渠道有 HIS 无 本来就不判成因）。但实际分类值照样算出来写进报告 —— 留空
    是「不作为判据」，不是「不记录」。
    """

    status: str
    kind: str = ""
    taxonomy_kind: str = ""
    evidence_rows: tuple[int, ...] = ()
    diff_amount: str = ""
    his_only_rows: tuple[int, ...] = ()

    @property
    def checks_taxonomy(self) -> bool:
        return bool(self.taxonomy_kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "kind": self.kind,
            "taxonomy_kind": self.taxonomy_kind,
            "evidence_rows": list(self.evidence_rows),
            "diff_amount": self.diff_amount,
            "his_only_rows": list(self.his_only_rows),
        }


@dataclass(frozen=True)
class AlignCase:
    """一条自足的对齐黄金用例。

    **自足**是刻意的：指标要能逐条定位「哪一条错了、错在哪一步」，而不是一个
    必须整批重跑才能复现的黑盒数字。一条 case 给一组渠道行 + 一组 HIS 行，
    期望对齐器报什么；单跑这一条就够复现。
    """

    case_id: str
    form: str
    channel: str
    period: str
    note: str
    channel_rows: tuple[AlignRecord, ...]
    his_rows: tuple[AlignRecord, ...]
    focus_index: int
    expect: CaseExpectation

    @property
    def focus_row(self) -> AlignRecord:
        return self.channel_rows[self.focus_index]

    def describe(self) -> str:
        return f"{self.case_id}[{self.form}] → {self.expect.status}"


@dataclass(frozen=True)
class CaseSet:
    """一份回归集 + 标注元数据。"""

    path: str
    cases: tuple[AlignCase, ...]
    set_version: str = ""
    period: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.cases)

    def form_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {form: 0 for form in CASE_FORMS}
        for case in self.cases:
            counts[case.form] = counts.get(case.form, 0) + 1
        return {k: v for k, v in counts.items() if v}

    def by_id(self, case_id: str) -> Optional[AlignCase]:
        for case in self.cases:
            if case.case_id == case_id:
                return case
        return None


# ───────────────────────────── 加载 ─────────────────────────────


def _parse_moment(value: Any, *, where: str) -> Optional[datetime]:
    """解析时刻。**允许为空**（缺收费时刻本身就是 dirty_row 形态要测的），
    但给了就必须带时区偏移 —— 不带偏移的时刻会让「跨账期落点」这条判据失去
    可核对性，而那条判据正是四形态之一。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", ""}:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _err(
            "ALIGN_CASE_INVALID",
            f"{where} 的 trade_time={value!r} 不是 ISO 8601 时刻：{exc}",
        ) from exc
    if moment.tzinfo is None:
        raise _err(
            "ALIGN_CASE_INVALID",
            f"{where} 的 trade_time={value!r} 缺时区偏移；本产品按归一时区判跨账期落点，"
            "不带偏移的时间会让四形态之一的判据失去可核对性",
        )
    return moment


def _record_from_raw(
    raw: dict[str, Any],
    *,
    channel: str,
    period: str,
    case_id: str,
    index: int,
    side: str,
    source_file: str,
) -> AlignRecord:
    where = f"回归用例 {case_id} 的 {side}[{index}]"
    if not isinstance(raw, dict):
        raise _err("ALIGN_CASE_INVALID", f"{where} 不是一条映射（行定义写坏了）")
    # 单号**允许为空**（缺商户订单号是真实且常见的形态，也正是 weak_match /
    # dirty_row 两类要覆盖的场景）；金额则缺一不可 —— 缺金额既判不了精确匹配
    # 也判不了弱匹配，这条 case 就没有意义。
    if "amount" not in raw or not str(raw.get("amount", "")).strip():
        raise _err(
            "ALIGN_CASE_INVALID",
            f"{where} 缺必填键 amount；金额缺一不可 —— 缺金额既判不了精确匹配"
            "也判不了弱匹配（单号允许为空，那本身就是弱匹配场景）",
        )
    try:
        amount = Decimal(str(raw["amount"]))
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都给同一条可读错误
        raise _err(
            "ALIGN_CASE_INVALID",
            f"{where} 的 amount={raw.get('amount')!r} 不是合法金额：{exc}；"
            "金额一律用字符串定点写，不要用 YAML 浮点（0.1+0.2 会不等于 0.3）",
        ) from exc
    refund_raw = str(raw.get("refund", "") or "").strip()
    if refund_raw:
        try:
            refund = Decimal(refund_raw)
        except Exception as exc:  # noqa: BLE001
            raise _err(
                "ALIGN_CASE_INVALID", f"{where} 的 refund={refund_raw!r} 不是合法金额：{exc}"
            ) from exc
    else:
        refund = None
    return AlignRecord(
        channel=channel,
        period=period,
        order_no=str(raw.get("order_no", "") or ""),
        amount=amount,
        refund=refund,
        trade_time=_parse_moment(raw.get("trade_time"), where=where),
        trade_type=str(raw.get("trade_type", "") or ""),
        department=str(raw.get("department", "") or ""),
        settle_type=str(raw.get("settle_type", "") or ""),
        his_order_no=str(raw.get("his_order_no", "") or ""),
        source_file=str(raw.get("source_file", "") or source_file),
        source_row_no=int(raw.get("source_row_no", 0) or index + 1),
        ch_id=int(raw.get("ch_id", 0) or 0),
    )


def _parse_expect(raw: Any, *, case_id: str) -> CaseExpectation:
    if not isinstance(raw, dict):
        raise _err("ALIGN_CASE_INVALID", f"回归用例 {case_id} 缺 expect 段")
    status = str(raw.get("status", "")).strip()
    if status not in _EXPECT_STATUSES:
        raise _err(
            "ALIGN_CASE_INVALID",
            f"回归用例 {case_id} 的 expect.status={status!r} 不在受控状态空间内："
            f"{'、'.join(sorted(_EXPECT_STATUSES))}",
        )
    kind = str(raw.get("kind", "") or "").strip()
    if kind and kind not in {k.value for k in AmbiguityKind}:
        raise _err(
            "ALIGN_CASE_INVALID",
            f"回归用例 {case_id} 的 expect.kind={kind!r} 不在受控歧义形态空间内："
            f"{'、'.join(sorted(k.value for k in AmbiguityKind))}",
        )
    def _rows(key: str) -> tuple[int, ...]:
        raw_rows = raw.get(key) or []
        if not isinstance(raw_rows, (list, tuple)):
            raise _err(
                "ALIGN_CASE_INVALID",
                f"回归用例 {case_id} 的 expect.{key} 必须是列表，实际是 {type(raw_rows).__name__}",
            )
        return tuple(sorted({int(x) for x in raw_rows}))

    return CaseExpectation(
        status=status,
        kind=kind,
        taxonomy_kind=str(raw.get("taxonomy_kind", "") or "").strip(),
        evidence_rows=_rows("evidence_rows"),
        diff_amount=str(raw.get("diff_amount", "") or "").strip(),
        his_only_rows=_rows("his_only_rows"),
    )


def parse_case(raw: dict[str, Any], *, path: str, default_period: str) -> AlignCase:
    case_id = str(raw.get("case_id", "")).strip()
    if not case_id:
        raise _err("ALIGN_CASE_INVALID", f"回归集 {path} 里有 case 缺 case_id")
    form = str(raw.get("form", "")).strip()
    if form not in CASE_FORMS:
        raise _err(
            "ALIGN_CASE_FORM_UNKNOWN",
            f"回归用例 {case_id} 的 form={form!r} 不在受控形态词表内。"
            f"合法形态：{'、'.join(CASE_FORMS)}。"
            "**新增形态必须先在 reconq.align.cases.CASE_FORMS 与 FORM_ZH 登记**，"
            "否则「把新形态塞进已有形态那一格」会让四形态覆盖门禁失效",
        )
    channel = str(raw.get("channel", "")).strip()
    period = str(raw.get("period", "") or default_period).strip()
    if not channel or not period:
        raise _err(
            "ALIGN_CASE_INVALID",
            f"回归用例 {case_id} 缺 channel 或 period；每条 case 都要能独立复现，"
            "账期写空就没法算跨账期落点（四形态之一）",
        )
    ch_rows = tuple(
        _record_from_raw(
            item,
            channel=channel,
            period=period,
            case_id=case_id,
            index=i,
            side="channel_rows",
            source_file=f"cases_{case_id}_{channel}.csv",
        )
        for i, item in enumerate(raw.get("channel_rows") or [])
    )
    if not ch_rows:
        raise _err(
            "ALIGN_CASE_INVALID",
            f"回归用例 {case_id} 的 channel_rows 为空；每条 case 至少要有一行渠道行",
        )
    focus_index = int(raw.get("focus_index", -1))
    if focus_index < 0:
        focus_index = len(ch_rows) - 1
    if focus_index >= len(ch_rows):
        raise _err(
            "ALIGN_CASE_INVALID",
            f"回归用例 {case_id} 的 focus_index={focus_index} 越界：该 case 只有 "
            f"{len(ch_rows)} 行渠道行",
        )
    his_rows = tuple(
        _record_from_raw(
            item,
            channel="his",
            period=period,
            case_id=case_id,
            index=i,
            side="his_rows",
            source_file=f"cases_{case_id}_his.csv",
        )
        for i, item in enumerate(raw.get("his_rows") or [])
    )
    return AlignCase(
        case_id=case_id,
        form=form,
        channel=channel,
        period=period,
        note=str(raw.get("note", "") or ""),
        channel_rows=ch_rows,
        his_rows=his_rows,
        focus_index=focus_index,
        expect=_parse_expect(raw.get("expect"), case_id=case_id),
    )


def load_case_set(
    path: str = DEFAULT_CASES_PATH,
    *,
    min_cases: int = MIN_CASES,
    min_per_ambiguity_form: int = MIN_CASES_PER_AMBIGUITY_FORM,
) -> CaseSet:
    """读回归集并做**加载期门禁**。

    三道门禁都在这里（而不是跑完 40 条再报），因为「跑完没报错」很容易被
    当成「改动没问题」：

    1. 条数下限（:data:`MIN_CASES`）—— 低于此数回归集没有否决力；
    2. **四形态覆盖门禁**（:data:`MIN_CASES_PER_AMBIGUITY_FORM`）——
       缺任一形态即拒，这是本模块最核心的一道；
    3. ``case_id`` 唯一 + 形态词表受控（:data:`CASE_FORMS`）。

    元数据读同目录的 ``cases.yml`` 内的 ``meta:`` 段（可选）：标注来源、时间、
    口径约定。缺 ``meta`` 照读，但报告里会显式标出「标注来源未记录」——
    没有出处的回归集不该被当成验收依据。
    """
    if not os.path.isfile(path):
        raise _err(
            "ALIGN_CASE_SET_MISSING",
            f"找不到对齐回归集 {path}；没有回归集就没有「改动有没有弄坏原来对的」"
            "这个问题的答案。先补 tests/fixtures/align/cases.yml",
        )
    with open(path, "r", encoding="utf-8") as handle:
        try:
            doc = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise _err(
                "ALIGN_CASE_SET_INVALID",
                f"回归集 {path} 不是合法 YAML：{exc}",
            ) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise _err(
            "ALIGN_CASE_SET_INVALID",
            f"回归集 {path} 顶层必须是含 cases 列表的映射",
        )
    default_period = str(doc.get("period", "") or "").strip()
    cases: list[AlignCase] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc["cases"]):
        if not isinstance(raw, dict):
            raise _err("ALIGN_CASE_INVALID", f"回归集 {path} 的 cases[{i}] 不是一条映射")
        case = parse_case(raw, path=path, default_period=default_period)
        if case.case_id in seen:
            raise _err(
                "ALIGN_CASE_INVALID",
                f"回归集 {path} 的 case_id={case.case_id} 与前面的行重复；"
                "case_id 是报告与失败信息里定位「哪一条错了」的唯一钥匙，不能重复",
            )
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise _err("ALIGN_CASE_SET_EMPTY", f"回归集 {path} 一条 case 都没有")
    # **覆盖门禁先于条数门禁**。顺序是有讲究的：整类形态缺失必然同时把总条数
    # 打到下限以下，若先查条数，使用者拿到的是「只有 39 条，低于下限」—— 照着
    # 这句话去补任意 1 条，形态照样缺、照样过不了，等于把人引到错的方向。
    # 形态门禁的诊断更具体，必须先说。
    counts: dict[str, int] = {form: 0 for form in CASE_FORMS}
    for case in cases:
        counts[case.form] += 1
    gaps = [form for form in FOUR_AMBIGUITY_FORMS if counts[form] < min_per_ambiguity_form]
    if gaps:
        detail = "、".join(
            f"{form}（{FORM_ZH[form][0]}）{counts[form]} 条 < {min_per_ambiguity_form}"
            for form in gaps
        )
        raise _err(
            "ALIGN_FORM_COVERAGE_INSUFFICIENT",
            f"{path} 的四形态覆盖门禁未过：{detail}（全库共 {len(cases)} 条）。"
            "四种歧义形态少一种，「新增逻辑只在已覆盖形态上正确」这件事就没人拦得住 —— "
            "典型后果是为一种形态调细了判据，另一种形态悄悄判错半年没被发现。"
            f"补齐**缺的这几种形态**（每种至少 {min_per_ambiguity_form} 条），"
            "不要靠补别的形态凑总数，也不要放宽门禁",
        )
    if len(cases) < min_cases:
        raise _err(
            "ALIGN_CASE_SET_TOO_SMALL",
            f"回归集 {path} 只有 {len(cases)} 条，低于下限 {min_cases} 条"
            "（四形态覆盖已达标，缺的是其余形态的厚度）。"
            "一条差异偶然对一次是常事，条数不够就没有否决力；"
            "宁可先补用例，也不要用小样本报一句「全绿」",
        )
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    if not meta:
        meta = {
            "annotated_source": "（未记录）",
            "annotated_at": "",
            "note": "回归集未记录 meta 段：标注来源与时间缺失，该回归集不得作为验收依据",
        }
    return CaseSet(
        path=path,
        cases=tuple(cases),
        set_version=str(doc.get("set_version", "") or ""),
        period=default_period,
        meta=meta,
    )


# ───────────────────────────── 逐条跑 ─────────────────────────────


@dataclass(frozen=True)
class CaseOutcome:
    """一条 case 上「期望 vs 实得」。"""

    case_id: str
    form: str
    channel: str
    note: str
    status: str = ""
    kind: str = ""
    taxonomy_kind: str = ""
    evidence_rows: tuple[int, ...] = ()
    his_only_rows: tuple[int, ...] = ()
    diff_amount: str = ""
    expect: Optional[CaseExpectation] = None
    mismatches: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.mismatches

    @property
    def form_zh(self) -> str:
        return FORM_ZH.get(self.form, (self.form, ""))[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "form": self.form,
            "form_zh": self.form_zh,
            "channel": self.channel,
            "status": self.status,
            "kind": self.kind,
            "taxonomy_kind": self.taxonomy_kind,
            "evidence_rows": list(self.evidence_rows),
            "his_only_rows": list(self.his_only_rows),
            "diff_amount": self.diff_amount,
            "expect": self.expect.to_dict() if self.expect else None,
            "mismatches": list(self.mismatches),
            "passed": self.passed,
            "note": self.note,
        }

    def render(self) -> str:
        """一行结论。**形态标签与中文名都带**：

        * 中文名（``一对一 / 明确单边``）给读报告的财务科看；
        * 原始 tag（``single``）给改代码的人看 —— 中文名在
          :data:`FORM_ZH` 里一改，这里就跟着变，而失败信息是要贴进工单的。
        """
        head = f"{self.case_id}[{self.form_zh}/{self.form}] {self.channel}：{self.status}"
        if not self.passed:
            head += "  ✗ " + "；".join(self.mismatches)
        return head


def _evidence_rows(outcome: MatchOutcome) -> tuple[int, ...]:
    """这条结论里带出来的证据行号（排序去重）。

    取的是**结论真正指向的行**，不是「跑过哪些行」：歧义/弱匹配/跨账期/
    重复认领给候选行号，匹配 / 金额不符给命中的那条 HIS 记录，
    渠道有 HIS 无则为空（它没有对面可指）。
    """
    if outcome.candidates:
        return tuple(sorted({c.row_no for c in outcome.candidates}))
    if outcome.his_record is not None:
        return (outcome.his_record.source_row_no,)
    return ()


def _taxonomy_of(outcome: MatchOutcome, *, period: str) -> str:
    """成因分类。**没有歧义判定就没有成因**（返回空串），不硬凑一个。"""
    if outcome.ambiguity is None:
        return ""
    case = case_from_verdict(
        outcome.ambiguity,
        channel_amount=(
            outcome.channel_record.net_amount if outcome.channel_record is not None else None
        ),
    )
    return classify_taxonomy(case, channel=outcome.key.channel, order_no=outcome.key.order_no).kind


def run_case(case: AlignCase, *, tz: Optional[Any] = None) -> CaseOutcome:
    """跑一条 case 并逐项比对。

    比对分五项，**逐项独立报**：状态、歧义形态、证据行号、差额、反向单边账。
    只报「结论不对」等于把「状态对但少给了一条候选行」这种改动放过去 ——
    那正是经办人要逐条回查时最先用到的东西。
    """
    window = period_window(case.period, tz or to_session_tz(None))
    result = align_all(
        {case.channel: list(case.channel_rows)},
        list(case.his_rows),
        period=case.period,
        window=window,
    )
    outcome = result.channels[case.channel].outcomes[case.focus_index]
    actual = CaseOutcome(
        case_id=case.case_id,
        form=case.form,
        channel=case.channel,
        note=case.note,
        status=outcome.status,
        kind=outcome.ambiguity.kind if outcome.ambiguity else "",
        taxonomy_kind=_taxonomy_of(outcome, period=case.period),
        evidence_rows=_evidence_rows(outcome),
        his_only_rows=tuple(sorted({r.source_row_no for r in result.his_only})),
        diff_amount=outcome.diff_amount,
        expect=case.expect,
    )
    return _compare(case, actual)


def _compare(case: AlignCase, actual: CaseOutcome) -> CaseOutcome:
    exp = case.expect
    problems: list[str] = []
    if actual.status != exp.status:
        problems.append(
            f"状态：期望 {exp.status}（{STATUS_ZH.get(exp.status, '')}），"
            f"实得 {actual.status}（{STATUS_ZH.get(actual.status, '')}）"
        )
    if exp.kind != actual.kind:
        problems.append(f"歧义形态：期望 {exp.kind or '（无）'}，实得 {actual.kind or '（无）'}")
    if exp.checks_taxonomy and exp.taxonomy_kind != actual.taxonomy_kind:
        problems.append(
            f"成因分类：期望 {exp.taxonomy_kind}，实得 {actual.taxonomy_kind or '（无）'}"
        )
    if exp.evidence_rows != actual.evidence_rows:
        problems.append(
            f"证据行号：期望 {list(exp.evidence_rows)}，实得 {list(actual.evidence_rows)}"
        )
    if exp.his_only_rows != actual.his_only_rows:
        problems.append(
            f"反向单边账行号：期望 {list(exp.his_only_rows)}，"
            f"实得 {list(actual.his_only_rows)}"
        )
    if exp.diff_amount != actual.diff_amount:
        problems.append(f"差额：期望 {exp.diff_amount or '（空）'}，实得 {actual.diff_amount or '（空）'}")
    return CaseOutcome(
        case_id=actual.case_id,
        form=actual.form,
        channel=actual.channel,
        note=actual.note,
        status=actual.status,
        kind=actual.kind,
        taxonomy_kind=actual.taxonomy_kind,
        evidence_rows=actual.evidence_rows,
        his_only_rows=actual.his_only_rows,
        diff_amount=actual.diff_amount,
        expect=exp,
        mismatches=tuple(problems),
    )


# ───────────────────────────── 整批跑 ─────────────────────────────


@dataclass(frozen=True)
class RegressionReport:
    """一批 case 的回归结论。"""

    case_set: CaseSet
    outcomes: tuple[CaseOutcome, ...]

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def ok(self) -> bool:
        return self.failed == 0

    @property
    def failures(self) -> tuple[CaseOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.passed)

    def form_matrix(self) -> dict[str, dict[str, int]]:
        """形态 × {总数, 通过, 失败}。报告的主表。"""
        matrix: dict[str, dict[str, int]] = {}
        for outcome in self.outcomes:
            cell = matrix.setdefault(outcome.form, {"total": 0, "passed": 0, "failed": 0})
            cell["total"] += 1
            cell["passed" if outcome.passed else "failed"] += 1
        return matrix

    def to_dict(self) -> dict[str, Any]:
        return {
            "set_path": self.case_set.path,
            "set_version": self.case_set.set_version,
            "period": self.case_set.period,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "ok": self.ok,
            "forms": self.form_matrix(),
            "failures": [o.to_dict() for o in self.failures],
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


def run_regression(case_set: CaseSet, *, tz: Optional[Any] = None) -> RegressionReport:
    """跑完整份回归集。**逐条独立**（不共用认领集合），一条崩了不影响其余。"""
    outcomes: list[CaseOutcome] = []
    for case in case_set.cases:
        try:
            outcomes.append(run_case(case, tz=tz))
        except Exception as exc:  # noqa: BLE001 - 一条 case 崩了整批仍要能出报告
            outcomes.append(
                CaseOutcome(
                    case_id=case.case_id,
                    form=case.form,
                    channel=case.channel,
                    note=case.note,
                    expect=case.expect,
                    mismatches=(f"跑这条 case 时抛异常：{type(exc).__name__}: {exc}",),
                )
            )
    return RegressionReport(case_set=case_set, outcomes=tuple(outcomes))


# ───────────────────────────── 报告 ─────────────────────────────


def _meta_line(meta: dict[str, Any]) -> str:
    source = str(meta.get("annotated_source", "") or "（未记录）")
    at = str(meta.get("annotated_at", "") or "")
    return f"标注来源：{source}" + (f" · 标注时间：{at}" if at else "")


def render_regression_report(
    report: RegressionReport, *, out_path: str = "", written_at: Optional[str] = None
) -> str:
    """回归报告（终端摘要 / 落盘全文同一份渲染）。

    报告必须同时说清三件事，缺一件这份报告就不能拿来签字：

    1. **跑了多少、按形态怎么分**（通过数 / 失败数 / 形态分布）；
    2. **这些数字不能说明什么**（它守的是回归面，不证明覆盖率与召回）；
    3. **失败的是哪几条、坏在哪一项**（case_id + 形态标签 + 逐项差异）。
    """
    case_set = report.case_set
    matrix = report.form_matrix()
    stamp = written_at or datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    lines = [
        "# 对齐黄金样例回归报告",
        "",
        f"- 回归集：`{case_set.path}`"
        + (f"（{case_set.set_version}）" if case_set.set_version else ""),
        f"- 账期口径：{case_set.period or '（各 case 自带）'}",
        f"- {_meta_line(case_set.meta)}",
        f"- 生成时间：{stamp}",
        f"- 结果：**通过 {report.passed} / {report.total}**"
        + ("" if report.ok else f"，失败 {report.failed}"),
        "",
        "## 1. 形态分布",
        "",
        "| 形态 | 说明 | 总数 | 通过 | 失败 |",
        "|------|------|------|------|------|",
    ]
    for form in CASE_FORMS:
        cell = matrix.get(form)
        zh, desc = FORM_ZH[form]
        if not cell:
            lines.append(f"| {zh}（`{form}`） | {desc} | 0 | 0 | 0 |")
            continue
        lines.append(
            f"| {zh}（`{form}`） | {desc} | {cell['total']} | {cell['passed']} | {cell['failed']} |"
        )
    lines += [
        "",
        f"> 四形态覆盖门禁在**加载期**已过（每种 ≥{MIN_CASES_PER_AMBIGUITY_FORM} 条）；"
        "表中为 0 的形态属于未覆盖的辅助形态，不参与四形态门禁。",
        "",
        "## 2. 这些数字不能说明什么",
        "",
        "- 这份报告守的是**回归面**：改动有没有把原来判对的判错。",
        "- 它**不**证明召回率或误报率 —— 那是 `reconq align-metrics` 用"
        "另一份金标集（`golden.jsonl`）与基础方案并排跑出来的结论，"
        "两套用例刻意不合并，指标口径见那份报告。",
        "- 全部通过不等于「没有 bug」：只说明这 40 余条形态化用例覆盖到的判定"
        "没有回退。",
        "",
        "## 3. 失败明细",
        "",
    ]
    if report.ok:
        lines.append("本次回归无失败项。")
    else:
        for outcome in report.failures:
            lines += [
                f"### {outcome.case_id} · {outcome.form_zh}（`{outcome.form}` · {outcome.channel}）",
                "",
                f"- 实得：{outcome.status}"
                + (f" / {outcome.kind}" if outcome.kind else "")
                + (f" / {outcome.taxonomy_kind}" if outcome.taxonomy_kind else ""),
                f"- 证据行号：{list(outcome.evidence_rows)}"
                f" · 反向单边账：{list(outcome.his_only_rows)}"
                f" · 差额：{outcome.diff_amount or '（空）'}",
                f"- 逐项差异：{'；'.join(outcome.mismatches)}",
            ]
            if outcome.note:
                lines.append(f"- 用例意图：{outcome.note}")
            lines.append("")
    lines += [
        "## 4. 逐条结论",
        "",
        "| case_id | 形态 | 渠道 | 结论 | 歧义形态 | 成因 | 证据行号 | 反向单边账 | 结果 |",
        "|---------|------|------|------|----------|------|----------|------------|------|",
    ]
    for outcome in report.outcomes:
        lines.append(
            f"| {outcome.case_id} | {outcome.form} | {outcome.channel} | {outcome.status} | "
            f"{outcome.kind or '—'} | {outcome.taxonomy_kind or '—'} | "
            f"{list(outcome.evidence_rows)} | {list(outcome.his_only_rows)} | "
            f"{'通过' if outcome.passed else '失败'} |"
        )
    if out_path:
        lines += [
            "",
            "---",
            "",
            f"复现命令：`reconq align-golden --cases {case_set.path} "
            f"--out {out_path}`；单条定位：`pytest tests/test_align_golden.py -k {report.failures[0].case_id if report.failures else '<case_id>'}`。",
        ]
    return "\n".join(lines)


def write_regression_report(
    out_path: str, report: RegressionReport, *, written_at: Optional[str] = None
) -> str:
    """写 ``out/align-regression.md``，返回落盘路径。"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(render_regression_report(report, out_path=out_path, written_at=written_at))
    return out_path
