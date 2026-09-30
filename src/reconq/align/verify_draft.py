"""归因证据一致性反幻觉核对（Task 32）。

行号合法 ≠ 说的是那一行
------------------------
Task 28 落了一条硬纪律：``evidence_refs`` 里的每个 ref 必须落在证据包真实
出现过的行标识集合内，越界即整条作废。**但那只挡住了「引用了不存在的行」**。

模型完全可能引用一个**真实存在**的行号，然后对着它说另一套话::

    证据 wechat_bill.csv:17  金额 86.00  交易时间 2026-09-02 10:00:00
    草稿 cause  「跨日切分重复计入」
         引用  wechat_bill.csv:17
         说明  「该笔 66.00 元落在 09-07 22:00，已计入下一账期」

ref 合法、语气笃定、结论具体 —— 而 66.00 / 09-07 / 22:00 三个数**在那一行上
一个都不对**。这种草稿比「ref 越界」危险得多：越界的会被拒掉，而这一条会
带着正确的行号走进经办人的确认流，经办人看到的是「有据可查」，于是签字。

本模块做**二次核对**：把 ``cause`` 与 ``explanation_zh`` 里出现的每一个
事实性字面量，逐项与**它所引用的那些证据行**对账。

四类核对
--------

=============  ===========================================================
核对项         判据
=============  ===========================================================
金额           草稿里的金额字面量必须能在**所引证据行**的金额 / 退款 /
               净额（或本期差额）上找到。找不到即 ``amount_mismatch``
时间 / 账期    草稿里的日期、时刻、ISO 周必须落在所引证据行的归一时区
               时间戳（或本期账期窗）上。**时区写法不同但同一时刻视作
               一致**（这是最容易误杀的一条，见下）
单号           草稿引用的单号必须出现在证据包内，否则
               ``order_no_not_in_evidence``
成因形态       ``cause`` 声明的成因必须与证据行的形态相容：声明
               ``split_payment`` 而证据里没有多对一扇出，即
               ``cause_evidence_conflict``
=============  ===========================================================

四条不肯让的边界
----------------

1. **不自动改写模型文本。** 核对发现「金额写成 66.00、实际 86.00」时，
   本模块**不**把 66.00 悄悄改成 86.00。改完的草稿看起来是对的，但它已经
   不是模型说的那句话了 —— 复算时对不上，指标表里的命中率也就没有意义。
   处置只有一条：整条标 ``draft_rejected``，差异标「待人工判定」。
2. **拒绝即拒绝，不降级为「已确认」。** 任一 mismatch ⇒ 草稿不进经办确认
   流。:meth:`VerificationResult.pending_manual_note` 只产出**文字**，不改
   台账、不碰 :mod:`reconq.engine.period` 的任何状态迁移 —— 与 Task 28 的
   降级纪律同一条。
3. **核对不了的项不判通过、也不判失败。** 成因短语映射不到受控形态
   （受控成因字典 Task 33 才落地）时，形态核对**整项跳过**并记进
   :attr:`VerificationResult.notes`，而不是猜一个形态去比。同理，草稿里
   没有任何时间表述就不做时间核对。
4. **判定基准是「它引用的行」。** 草稿引用 :17 却引用了 :18 上的金额，
   同样记 mismatch —— 金额本身在证据包里并不假，假的是「据以论证的那一行」。

时区为什么单独说一句
--------------------
草稿写 ``2026-09-02T02:00:00Z``、证据行是 ``2026-09-02T10:00:00+08:00``，
**这是同一时刻**，判它不一致等于逼模型把时间一律写成院内本地时间 ——
而院内自建端点的模型有时更习惯输出 UTC。本模块的做法：带偏移的时刻先
折算到**绝对时刻**再比；纯日期先折到证据行的院内时区再取日历日。这正是
「时区表述不同但同一时刻视作一致」的具体落法。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层，也无法有这一层**：``contract verify``
逐 check 出的 fail 行是**确定性判定的结果**（这一行的这一列不满足这条口径），
不存在「一段解释声称某个事实、而这个事实要与证据逐项对账」这个环节。源产品
主路径止于「打印证据行」；本产品要在此之后再走一步「把证据交给模型解释成因」，
而这一步天生带来一个新风险：解释本身会说错话。本模块就是那条新风险的对策。

依赖方向：本模块只依赖 :mod:`reconq.align.bundle`（证据包，只读）与
:mod:`reconq.align.taxonomy`（受控成因形态的枚举），**不 import**
``engine.period``（不碰台账）、不 import ``gateway``（不碰模型调用）。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional, Sequence

from ..errors import ReconqError
from ..runlog import log_event
from ..sources.timezone import HOSPITAL_TZ
from .bundle import EvidenceBundle
from .taxonomy import AMOUNT_TOLERANCE, AmbiguityKind

__all__ = [
    "AMOUNT_CHECK_TOLERANCE",
    "MISMATCH_ZH",
    "Mismatch",
    "MismatchKind",
    "VerificationLedger",
    "VerificationResult",
    "cause_form_outcome",
    "draft_rejection",
    "pending_manual_note",
    "screen_drafts",
    "verify_draft",
]

logger = logging.getLogger("reconq.verify_draft")

#: 金额比对的容差（元）。与 :data:`reconq.align.taxonomy.AMOUNT_TOLERANCE`
#: 同值但**各写一份常量** —— 与 Task 9 对下载链接 TTL 的处理同理由：跨模块
#: 共用一个常量会把两处本可独立演化的判据绑死。
AMOUNT_CHECK_TOLERANCE = AMOUNT_TOLERANCE


# ─────────────────────────── 核对项 ───────────────────────────


class MismatchKind(str):
    """核对项标识（值即落库 / 面板显示的字符串）。

    刻意用 ``str`` 子类而不是 ``Enum``：这些值要直接进
    :class:`reconq.errors.error_meta` 之外的自由文本统计与 JSON，
    ``Enum`` 在 ``json.dumps`` 上要额外转换，而这里没有任何需要
    ``Enum`` 提供的封闭成员校验（判据在 :func:`verify_draft` 一处收口）。
    """

    AMOUNT = "amount_mismatch"
    TIME = "time_mismatch"
    ORDER_NO = "order_no_not_in_evidence"
    CAUSE_FORM = "cause_evidence_conflict"


#: 核对项 → 中文说法。面板、结论单、事件链三处说同一句话。
MISMATCH_ZH: dict[str, str] = {
    MismatchKind.AMOUNT: "金额与所引证据行对不上",
    MismatchKind.TIME: "时间与所引证据行对不上",
    MismatchKind.ORDER_NO: "引用的单号不在证据包内",
    MismatchKind.CAUSE_FORM: "声明的成因与证据行形态矛盾",
}

#: 错误码。整条草稿因核对不过而被拒时用这一个码；逐条核对项在
#: :class:`Mismatch.kind` 里分列，面板可按项归组。
REJECT_CODE = "ATTRIBUTION_EVIDENCE_MISMATCH"


# ─────────────────────────── 数据结构 ───────────────────────────


@dataclass(frozen=True)
class Mismatch:
    """一条核对不过的记录。

    :attr:`claimed` 是草稿里**原样抄下来**的那个字面量。不做归一、不做
    截断 —— 面板上要让经办人看到模型到底写了什么，而不是系统认为它写了什么。
    """

    kind: str
    detail: str
    claimed: str = ""
    evidence_ref: str = ""
    evidence_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "kind_zh": MISMATCH_ZH.get(self.kind, self.kind),
            "detail": self.detail,
            "claimed": self.claimed,
            "evidence_ref": self.evidence_ref,
            "evidence_value": self.evidence_value,
        }


@dataclass(frozen=True)
class VerificationResult:
    """一条草稿的核对结论。"""

    consistent: bool
    mismatches: tuple[Mismatch, ...] = ()
    #: 各类字面量实际抽到几个（0 = 本项无可核对内容，记 skipped 不记 pass）。
    checked: dict[str, int] = field(default_factory=dict)
    #: ``cause`` 映射到的受控成因形态（:class:`~reconq.align.taxonomy.AmbiguityKind`
    #: 的值）。空串 = 映射不到，形态核对整项跳过。
    cause_kind: str = ""
    cause_kind_zh: str = ""
    #: 没判、但**必须让人看见**的项（跳过的核对、成因未映射等）。
    notes: tuple[str, ...] = ()
    conclusion_key: str = ""
    period: str = ""
    channel: str = ""

    @property
    def mismatch_kinds(self) -> tuple[str, ...]:
        return tuple(m.kind for m in self.mismatches)

    @property
    def skipped(self) -> tuple[str, ...]:
        return self.notes

    def to_dict(self) -> dict[str, Any]:
        return {
            "consistent": self.consistent,
            "error_code": "" if self.consistent else REJECT_CODE,
            "mismatch_count": len(self.mismatches),
            "mismatch_kinds": list(self.mismatch_kinds),
            "mismatches": [m.to_dict() for m in self.mismatches],
            "checked": dict(self.checked),
            "cause_kind": self.cause_kind,
            "cause_kind_zh": self.cause_kind_zh,
            "notes": list(self.notes),
            "conclusion_key": self.conclusion_key,
            "period": self.period,
            "channel": self.channel,
        }

    def pending_manual_note(self) -> str:
        """差异条目的中文标记（**只是文字**，不改台账、不推进状态）。"""
        return pending_manual_note(self)


# ─────────────────────────── 文本抽取 ───────────────────────────

#: 账期码 ``2026-W36``。
_PERIOD_RE = re.compile(r"\d{4}\s*-\s*W\d{1,2}")
#: 带时刻的日期时间：``2026-09-02 10:00:00`` / ``2026-09-02T10:00:00+08:00``。
_DATETIME_RE = re.compile(
    r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
    r"(?:[T\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:Z|[+-]\d{2}:?\d{2})?)?"
)
#: 纯时刻 ``10:00`` / ``10:00:00``。
_TIME_RE = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")
#: 百分比。先抽掉，否则「占 0.5%」里的 0.5 会被当成金额。
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*[%％]")
#: 文件名（``wechat_bill_2026-W36.csv``）。含数字且长得像单号，不先抽掉会
#: 被单号核对判成「引用了证据包外的单号」。
_FILE_RE = re.compile(r"[A-Za-z0-9_\-]+\.(?:csv|txt|tsv|json|xlsx|zip|gz|xml)")
#: 带货币单位的数字（含千分位）。前缀货币符号（``¥86``）**不进 lookbehind**
#: —— 它要能被匹配到，由 :func:`_extract_amount_literals` 单独判定「货币
#: 在前还是在后」；写进 lookbehind 会把 ``¥86`` 整段挡在匹配之外。
_NUMBER_RE = re.compile(
    r"(?<![\dA-Za-z.\-])"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"\s*(?:元|圆|块钱|¥|￥)?"
)
#: 单号候选：字母数字混排且含数字（长度 ≥4），或纯数字长串（长度 ≥12，
#: 微信 / 支付宝交易号都是 28 位）。
#:
#: **下限取 4 而不取更长**：院内单号形态很杂（``EX-1`` / ``JZ20260902001`` /
#: ``4200001234...``），阈值一高就会漏掉短形态，越界单号从缺口溜过去。
#: 误报由另一道闸挡着 —— 纯字母词（``HIS`` / ``CSV``）不含数字，进不来；
#: 日期与时刻在抽单号前已被抽掉。
_ORDER_TOKEN_RE = re.compile(r"(?<![\w])[A-Za-z0-9][A-Za-z0-9_\-]{3,}(?![\w])")
_DIGITS_ONLY_RE = re.compile(r"^\d{12,}$")

_CURRENCY_SUFFIX_RE = re.compile(r"(?:元|圆|块钱|¥|￥)\s*$")


def _strip_patterns(text: str, patterns: Sequence["re.Pattern[str]"]) -> str:
    """把若干正则的命中替换成等长空格。

    **用空格而不是删掉**：删掉会让后面几个字面量粘成一个新词（「这条 20
    行」被抽掉日期后变成「这条 20行」，20 行的行号又被当成金额）。等长
    空格保持偏移不变，下一步匹配看到的是「这里曾经有过内容」的空洞。
    """
    out = text
    for pattern in patterns:
        pieces: list[str] = []
        last = 0
        for match in pattern.finditer(out):
            pieces.append(out[last : match.start()])
            pieces.append(" " * (match.end() - match.start()))
            last = match.end()
        pieces.append(out[last:])
        out = "".join(pieces)
    return out


def _to_decimal(literal: str) -> Optional[Decimal]:
    text = literal.replace(",", "").replace(" ", "")
    text = _CURRENCY_SUFFIX_RE.sub("", text).replace("¥", "").replace("￥", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _extract_amount_literals(work: str) -> list[tuple[str, Decimal, int, int]]:
    """抽出金额字面量。

    **只有两种写法算金额**：带小数点（1–2 位小数）或带货币单位。裸整数
    不算 —— 「共 2 条候选行」里的 2 判成金额会把一份完全正确的草稿拒掉，
    而这类误杀一旦发生，经办人会开始忽略「已拒」标记，反幻觉核对就白做了。

    货币单位**前缀后缀都认**：``¥86`` 与 ``86 元`` 在院内账单里都常见，
    只认后缀会把「¥86」当成「没提金额」而静默跳过金额核对 —— 一份漏过
    核对的草稿比一份被误拒的更危险，因为它看起来是「通过」的。
    """
    found: list[tuple[str, Decimal, int, int]] = []
    for match in _NUMBER_RE.finditer(work):
        num_text = match.group("num")
        tail = work[match.end() - len(num_text) : match.end()]
        head = work[max(0, match.start() - 1) : match.start()]
        has_point = "." in num_text
        has_currency = bool(_CURRENCY_SUFFIX_RE.search(tail)) or head in ("¥", "￥")
        if not (has_point or has_currency):
            continue
        value = _to_decimal(match.group(0))
        if value is None:
            continue
        found.append((match.group(0).strip(), value, match.start(), match.end()))
    return found


def _extract_order_tokens(work: str) -> list[tuple[str, int, int]]:
    """抽出单号候选。裸短数字（行号、笔数）不抽 —— 那是列号不是单号。"""
    found: list[tuple[str, int, int]] = []
    for match in _ORDER_TOKEN_RE.finditer(work):
        token = match.group(0)
        has_digit = any(ch.isdigit() for ch in token)
        has_alpha = any(ch.isalpha() for ch in token)
        if _DIGITS_ONLY_RE.match(token):
            if not has_digit:  # pragma: no cover - 正则已保证
                continue
        elif not (has_digit and has_alpha):
            continue
        found.append((token, match.start(), match.end()))
    return found


def _normalize_token(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


# ─────────────────────────── 证据行索引 ───────────────────────────


def _iter_rows(bundle: EvidenceBundle) -> list[dict[str, Any]]:
    """证据包里全部行（差异行 + 候选行 + 同单号历史行），逐条去重。"""
    payload = bundle.payload
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("diff_row",):
        row = payload.get(key)
        if isinstance(row, dict) and row.get("ref"):
            rows.append(row)
            seen.add(str(row["ref"]))
    for key in ("candidate_rows", "same_order_no_rows"):
        for row in payload.get(key) or []:
            if not isinstance(row, dict) or not row.get("ref"):
                continue
            ref = str(row["ref"])
            if ref in seen:
                continue
            seen.add(ref)
            rows.append(row)
    return rows


def _row_amounts(row: dict[str, Any]) -> list[Decimal]:
    out: list[Decimal] = []
    for key in ("amount", "refund", "net_amount"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        value = _to_decimal(str(raw))
        if value is not None:
            out.append(value)
    return out


def _row_time(row: dict[str, Any]):
    from datetime import datetime

    raw = row.get("trade_time")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HOSPITAL_TZ)
    return parsed


def _cited_rows(bundle: EvidenceBundle, refs: Sequence[str]) -> list[dict[str, Any]]:
    wanted = {str(r) for r in refs}
    return [row for row in _iter_rows(bundle) if str(row.get("ref")) in wanted]


def _known_order_numbers(bundle: EvidenceBundle) -> set[str]:
    known: set[str] = set()
    for row in _iter_rows(bundle):
        for key in ("order_no", "his_order_no", "record_id"):
            value = row.get(key)
            if value:
                known.add(_normalize_token(str(value)))
    return known


# ─────────────────────────── 逐项核对 ───────────────────────────


def _check_amounts(
    cited: Sequence[dict[str, Any]],
    literals: Sequence[tuple[str, Decimal, int, int]],
    diff_amount: Optional[Decimal],
) -> tuple[list[Mismatch], int]:
    if not literals:
        return [], 0
    allowed: list[tuple[Decimal, str]] = []
    for row in cited:
        for value in _row_amounts(row):
            allowed.append((value, str(row.get("ref", ""))))
    if diff_amount is not None:
        allowed.append((diff_amount, "discrepancy.diff_amount"))
    mismatches: list[Mismatch] = []
    for literal, value, _start, _end in literals:
        hit = any(abs(cand - value) <= AMOUNT_CHECK_TOLERANCE for cand, _ref in allowed)
        if not hit:
            seen = "、".join(sorted({r for _v, r in allowed})) or "（无）"
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.AMOUNT,
                    claimed=literal,
                    detail=(
                        f"草稿写「{literal}」，但所引证据行（{seen}）上没有这个金额。"
                        "不改写模型文本 —— 整条草稿标待人工判定"
                    ),
                    evidence_value="",
                )
            )
    return mismatches, len(literals)


def _collect_diff_amount(bundle: EvidenceBundle) -> Optional[Decimal]:
    """本期差额（``discrepancy.diff_amount``）。

    差额**不在行上**，但它是「该去查哪条账」这件事本身的数字，草稿引用
    它是合法的（甚至更常见）。拿不到时返回 ``None`` —— 宁可少认一个合法
    金额来源，也不要凭空认一个把误杀变成漏放。
    """
    raw = bundle.payload.get("discrepancy", {}).get("diff_amount")
    if raw in (None, ""):
        return None
    return _to_decimal(str(raw))


def _check_times(
    bundle: EvidenceBundle,
    cited: Sequence[dict[str, Any]],
    cited_periods: Sequence[str],
    work: str,
) -> tuple[list[Mismatch], int]:
    mismatches: list[Mismatch] = []
    checked = 0

    row_times = [(str(row.get("ref", "")), _row_time(row)) for row in cited]
    row_times = [(ref, moment) for ref, moment in row_times if moment is not None]

    window = bundle_window(bundle)

    # —— ISO 周账期 ——
    for match in _PERIOD_RE.finditer(work):
        token = match.group(0).replace(" ", "").upper()
        checked += 1
        if token in set(cited_periods):
            continue
        mismatches.append(
            Mismatch(
                kind=MismatchKind.TIME,
                claimed=token,
                detail=(
                    f"草稿提到账期 {token}，但所引证据行只出现在 {'、'.join(cited_periods) or '（无账期）'}"
                ),
                evidence_value="、".join(cited_periods),
            )
        )

    # —— 带时刻 / 纯日期 ——
    # 先把它们从副本里挖掉，否则 ``2026-09-02T02:00:00Z`` 的 ``02:00:00``
    # 会在下面的「纯时刻」循环里被当成一个独立钟点再核一遍 —— 同一串文本
    # 判两次，其中一次必然按 UTC 钟点去比院内行，于是误杀。
    consumed = _strip_patterns(work, (_DATETIME_RE,))
    for match in _DATETIME_RE.finditer(work):
        raw = match.group(0)
        has_clock = bool(re.search(r"\d{1,2}:\d{2}", raw))
        parsed = _parse_datetime_cn(raw)
        if parsed is None:
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.TIME,
                    claimed=raw,
                    detail=f"草稿里的时间「{raw}」无法解析为时刻",
                )
            )
            checked += 1
            continue
        checked += 1
        if not has_clock:
            # 纯日期：折到院内时区再取日历日（模型写 UTC 日期时也认）
            local_date = parsed.astimezone(HOSPITAL_TZ).date()
            if any(moment.astimezone(HOSPITAL_TZ).date() == local_date for _r, moment in row_times):
                continue
            if window is not None and local_date in (window[0].date(), window[1].date()):
                continue
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.TIME,
                    claimed=raw,
                    detail=(
                        f"草稿写日期 {local_date.isoformat()}，所引证据行上没有这一天的交易时间"
                    ),
                )
            )
            continue
        # 带时刻：比绝对时刻 —— 「时区写法不同但同一时刻」在这一步被认下
        if any(abs((moment - parsed).total_seconds()) < 1 for _r, moment in row_times):
            continue
        if window is not None and _near_window_edge(window, parsed):
            # 只放行**边界本身**（周一零点前后一分钟），不放行窗口内的任意
            # 时刻。放行整个窗口会让「该笔交易时间 22:00」这种与所引行完全
            # 对不上的说法蒙混过关 —— 窗口是「跨期判据」，不是「时间随便
            # 写在窗口里就算了」的通行证。
            continue
        mismatches.append(
            Mismatch(
                kind=MismatchKind.TIME,
                claimed=raw,
                detail=(
                    f"草稿写时刻 {parsed.isoformat()}，与所引证据行的交易时间"
                    "不是同一时刻（时区写法不同但同一时刻的情形已视为一致）"
                ),
            )
        )

    # —— 纯时刻（只在上面没吃掉的残余里找）——
    for match in _TIME_RE.finditer(consumed):
        token = match.group(0)
        checked += 1
        hh, mm = int(token[:2]), int(token[3:5])
        if any(moment.hour == hh and moment.minute == mm for _r, moment in row_times):
            continue
        mismatches.append(
            Mismatch(
                kind=MismatchKind.TIME,
                claimed=token,
                detail=(
                    f"草稿写时刻 {token}，所引证据行里没有同一钟点的交易时间"
                ),
            )
        )
    return mismatches, checked


def bundle_window(bundle: EvidenceBundle):
    """账期窗的 ``(start, end)`` —— 跨账期解释里出现窗口边界是合法的。

    返回的是**带时区的时刻**而不是日期：带时刻的草稿要跟窗口比绝对时刻，
    纯日期的草稿才取 ``.date()`` 比日历日。返回日期会让前一种比较直接
    抛 ``TypeError``（date 与 datetime 不可比）。
    """
    window = bundle.payload.get("period_window") or {}
    if not window.get("available"):
        return None
    from datetime import datetime

    out: list[datetime] = []
    for key in ("start", "end"):
        raw = window.get(key)
        if not raw:
            return None
        try:
            out.append(datetime.fromisoformat(str(raw)).astimezone(HOSPITAL_TZ))
        except ValueError:
            return None
    if len(out) != 2:  # pragma: no cover - 上一步已保证
        return None
    return (out[0], out[1])


#: 边界容差（秒）。周一零点那一刻是半开区间 ``[start, end)`` 的接缝，
#: 模型把它写成 ``23:59:59`` 或 ``00:00:01`` 说的是同一件事。
_WINDOW_EDGE_TOLERANCE_SECONDS = 60


def _near_window_edge(window, moment) -> bool:
    """该时刻是否落在账期窗的**边界**上（而不是窗内任意位置）。"""
    from datetime import timedelta

    lo, hi = window
    tolerance = timedelta(seconds=_WINDOW_EDGE_TOLERANCE_SECONDS)
    return (
        lo - tolerance <= moment <= lo + tolerance
        or hi - tolerance <= moment <= hi + tolerance
    )


def _parse_datetime_cn(raw: str):
    """``2026-09-02 10:00`` / ``2026年9月2日 10:00`` / ISO 带偏移。"""
    from datetime import datetime, timezone

    text = raw.replace("年", "-").replace("月", "-").replace("日", "").strip()
    text = text.replace("/", "-")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in (
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M%z",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=HOSPITAL_TZ)
        return parsed.astimezone(timezone.utc)
    return None


def _check_order_numbers(
    bundle: EvidenceBundle, work: str, tokens: Sequence[tuple[str, int, int]]
) -> tuple[list[Mismatch], int]:
    if not tokens:
        return [], 0
    known = _known_order_numbers(bundle)
    # 证据包正文（源文件名 / 契约 check 路径 / 列名 / 渠道名）都算「包内出现过」，
    # 否则「该笔在 wechat_bill_2026-W36.csv:17」这种正常表述会被判越界。
    haystack = _normalize_token(bundle.render())
    mismatches: list[Mismatch] = []
    for token, _start, _end in tokens:
        norm = _normalize_token(token)
        if norm in known or norm in haystack:
            continue
        mismatches.append(
            Mismatch(
                kind=MismatchKind.ORDER_NO,
                claimed=token,
                detail=(
                    f"草稿引用单号 {token}，但它不在证据包的任何一行上 —— "
                    "证据包外的事实无法被本条差异核对"
                ),
            )
        )
    return mismatches, len(tokens)


# ─────────────────────────── 成因形态交叉核对 ───────────────────────────

#: 成因短语 → 受控形态。**顺序有意义**：更具体的形态排在前面。
#: 受控成因字典（Task 33）落地后本表由字典替代，本轮只做「映射得到就核对、
#: 映射不到就跳过」，不猜。
_CAUSE_KIND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        AmbiguityKind.CROSS_PERIOD.value,
        (
            "跨账期", "跨日", "跨期", "跨周", "落点", "下一期", "下一账期",
            "下期", "上期", "上一期", "本期末", "期外", "次日", "月初",
        ),
    ),
    (
        AmbiguityKind.REFUND_COEXIST.value,
        ("退费并存", "原单并存", "退费", "红冲", "退款", "退单"),
    ),
    (
        AmbiguityKind.MERGED_PAYMENT.value,
        ("合并支付", "合并成", "多笔渠道", "多笔并", "合并单", "合并到"),
    ),
    (
        AmbiguityKind.SPLIT_PAYMENT.value,
        ("一码付", "拆笔", "拆成多", "拆分", "拆成", "分摊", "多笔拆", "多笔分"),
    ),
)


def map_cause_kind(cause: str) -> str:
    """成因短语 → 受控形态值；映射不到返回空串。"""
    text = (cause or "").strip()
    if not text:
        return ""
    for kind, hints in _CAUSE_KIND_HINTS:
        if any(hint in text for hint in hints):
            return kind
    return ""


def cause_form_outcome(bundle: EvidenceBundle, cause: str) -> tuple[str, str, str]:
    """成因形态 vs 证据行形态。

    返回 ``(kind, kind_zh, detail)``；``detail`` 为空表示相容。
    **kind 为空串表示成因映射不到受控形态** —— 此时整项跳过（调用方记
    note），既不判通过也不判失败。
    """
    from .taxonomy import KIND_ZH as TAXONOMY_ZH

    kind = map_cause_kind(cause)
    if not kind:
        return "", "", ""
    kind_zh = TAXONOMY_ZH.get(kind, (kind, ""))[0]
    rows = _iter_rows(bundle)
    if not rows:
        return kind, kind_zh, "证据包内没有任何行，形态核对无法进行"

    diff_row = bundle.payload.get("diff_row") or {}
    diff_side = str(diff_row.get("side", ""))
    diff_ref = str(diff_row.get("ref", ""))
    same_side = [
        r
        for r in rows
        if str(r.get("ref", "")) != diff_ref and str(r.get("side", "")) == diff_side
    ]
    other_side = [r for r in rows if str(r.get("side", "")) != diff_side and r is not diff_row]

    if kind == AmbiguityKind.SPLIT_PAYMENT.value:
        if len(other_side) >= 2:
            return kind, kind_zh, ""
        return (
            kind,
            kind_zh,
            f"草稿声明「一码付拆笔」，但证据包里差异行（{diff_ref or '（无）'}）"
            f"对面只有 {len(other_side)} 行，不构成一拆多",
        )
    if kind == AmbiguityKind.MERGED_PAYMENT.value:
        if len(same_side) >= 2 and other_side:
            return kind, kind_zh, ""
        return (
            kind,
            kind_zh,
            f"草稿声明「合并支付」，但证据包里同侧只有 {len(same_side)} 行、"
            f"对面 {len(other_side)} 行，不构成多笔合并覆盖一条",
        )
    if kind == AmbiguityKind.CROSS_PERIOD.value:
        window = bundle.payload.get("period_window") or {}
        for row in rows:
            if row.get("cross_period"):
                return kind, kind_zh, ""
            moment = _row_time(row)
            if moment is None or not window.get("available"):
                continue
            raw_start, raw_end = window.get("start"), window.get("end")
            if not raw_start or not raw_end:
                continue
            lo = _parse_datetime_cn(str(raw_start))
            hi = _parse_datetime_cn(str(raw_end))
            if lo is None or hi is None:
                continue
            if not (lo <= moment.astimezone(lo.tzinfo) < hi):
                return kind, kind_zh, ""
        return (
            kind,
            kind_zh,
            "草稿声明「跨账期落点」，但证据包里每一行的交易时间都落在本期窗口内",
        )
    if kind == AmbiguityKind.REFUND_COEXIST.value:
        has_refund = any(bool(row.get("is_refund")) for row in rows)
        has_normal = any(not bool(row.get("is_refund")) for row in rows)
        if has_refund and has_normal:
            return kind, kind_zh, ""
        return (
            kind,
            kind_zh,
            "草稿声明「退费与原单并存」，但证据包里没有同时出现红冲行与原单行",
        )
    return kind, kind_zh, ""  # pragma: no cover - 上游四个分支已收口


# ─────────────────────────── 主入口 ───────────────────────────


def verify_draft(draft: Any, bundle: EvidenceBundle) -> VerificationResult:
    """把一条成因草稿与它所引的证据行逐项对账。

    **永不抛异常、永不改写模型文本、永不推进任何业务状态。** 核对不过
    就是 :attr:`VerificationResult.consistent=False` + 逐条 mismatch。

    :param draft: :class:`~reconq.align.attribution.CauseDraft` 或任何带
        ``cause`` / ``evidence_refs`` / ``explanation_zh`` 的对象（鸭子类型）。
    :param bundle: 该条差异的 :class:`~reconq.align.bundle.EvidenceBundle`。
    """
    cause = _get(draft, "cause")
    explanation = _get(draft, "explanation_zh")
    refs = tuple(_get(draft, "evidence_refs") or ())
    text = f"{cause}\n{explanation}".strip()

    cited = _cited_rows(bundle, refs)
    cited_periods = sorted({str(row.get("period", "")) for row in cited if row.get("period")})
    diff_amount = _collect_diff_amount(bundle)

    notes: list[str] = []

    # **缺字段不得静默变成「核对通过」**。调用方把 ``explanation_zh`` 写成
    # ``explanation`` 时，只剩 cause 的几行短文本，四项核对全部「无可核对
    # 内容」，结果是一条 ``consistent=True`` 的假通过 —— 它比一条被误拒的
    # 草稿危险得多，因为面板上它显示为「核对通过」。故显式记 note。
    missing = [
        name
        for name, value in (("cause", cause), ("explanation_zh", explanation))
        if not (value or "").strip()
    ]
    if missing:
        notes.append(
            f"草稿缺 {'、'.join(missing)}，该字段内的字面量一项都没核 —— "
            "这不是「核对通过」，请检查传入对象是否带这两个字段"
        )

    if not cited:
        notes.append(
            "所引 evidence_refs 在证据包里没有对应行（正常应在 Task 28/29 已被拒），"
            "本轮三项逐字核对均跳过"
        )

    # 抽金额与单号之前，先把账期 / 日期时刻 / 百分比 / 文件名抽掉 —— 顺序反了
    # 会把「2026-W36」里的 2026、「wechat_bill_2026-W36.csv」里的 2026
    # 分别当成金额与单号。时间核对反过来要在**原文**上抽（它自己认这些形态），
    # 故只先抽掉百分比与文件名。
    work = _strip_patterns(text, (_PERCENT_RE, _FILE_RE, _PERIOD_RE, _DATETIME_RE, _TIME_RE))
    work_for_time = _strip_patterns(text, (_PERCENT_RE, _FILE_RE))

    amount_literals = _extract_amount_literals(work)
    for _literal, _value, start, end in amount_literals:
        work = work[:start] + " " * (end - start) + work[end:]
    order_tokens = _extract_order_tokens(work)

    amount_mismatches, amount_checked = _check_amounts(cited, amount_literals, diff_amount)
    time_mismatches, time_checked = _check_times(bundle, cited, cited_periods, work_for_time)
    order_mismatches, order_checked = _check_order_numbers(bundle, work, order_tokens)

    form_kind, form_zh, form_detail = cause_form_outcome(bundle, cause)
    form_mismatches: list[Mismatch] = []
    if not form_kind:
        notes.append(
            f"成因短语「{cause}」映射不到受控形态，形态交叉核对跳过（受控字典落地后自动生效）"
        )
    elif form_detail:
        form_mismatches.append(
            Mismatch(kind=MismatchKind.CAUSE_FORM, claimed=cause, detail=form_detail)
        )

    mismatches = amount_mismatches + time_mismatches + order_mismatches + form_mismatches
    if amount_checked == 0:
        notes.append("草稿未出现金额字面量，金额核对无可核对内容（记跳过，不记通过）")
    if time_checked == 0:
        notes.append("草稿未出现时间字面量，时间核对无可核对内容（记跳过，不记通过）")
    if order_checked == 0:
        notes.append("草稿未引用单号，单号核对无可核对内容（记跳过，不记通过）")

    result = VerificationResult(
        consistent=not mismatches,
        mismatches=tuple(mismatches),
        checked={
            "amount": amount_checked,
            "time": time_checked,
            "order_no": order_checked,
            "cause_form": 0 if not form_kind else 1,
        },
        cause_kind=form_kind,
        cause_kind_zh=form_zh,
        notes=tuple(notes),
        conclusion_key=str(bundle.payload.get("conclusion_key", "")),
        period=str(bundle.payload.get("period", "")),
        channel=str(bundle.payload.get("channel", "")),
    )

    log_event(
        "attribution.verify",
        level="info" if result.consistent else "warning",
        message=(
            f"归因草稿核对{'一致' if result.consistent else '不过'}："
            f"{len(mismatches)} 项不一致 / {len(notes)} 项跳过"
        ),
        code="" if result.consistent else REJECT_CODE,
        period=result.period,
        channel=result.channel,
        conclusion_key=result.conclusion_key,
        consistent=result.consistent,
        mismatch_count=len(mismatches),
        mismatch_kinds=list(result.mismatch_kinds),
        checked=dict(result.checked),
    )
    return result


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


# ─────────────────────────── 处置与统计 ───────────────────────────


def pending_manual_note(result: VerificationResult) -> str:
    """差异条目的中文标记。**只产出文字**。

    与 :func:`reconq.gateway.degradation.mark_pending_manual` 同形、不同码：
    那一条说的是「归因能力降级」，这一条说的是「模型答了，但答的内容与
    证据对不上」。两者的下一步动作不同 —— 前者查模型服务，后者改提示词
    或直接人工判定，所以文案不能混成同一句。
    """
    if result.consistent:
        return ""
    kinds = "、".join(
        f"{MISMATCH_ZH.get(kind, kind)}×{count}"
        for kind, count in sorted(_count(result.mismatch_kinds).items())
    )
    return f"待人工判定（归因草稿与证据不一致：{kinds or '未列明'}）"


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def draft_rejection(draft: Any, result: VerificationResult) -> Any:
    """把核对不过的草稿转成 :class:`~reconq.align.attribution.RejectedDraft`。

    **刻意不在这里 import 归因模块**（与 :mod:`reconq.run_summary` 对
    ``gateway`` 的处理同一理由）：本模块只做核对，处置的落点在调用方。
    返回一个鸭子类型兼容 ``RejectedDraft`` 的
    :class:`DraftRejection`，调用方需要时自行转换。
    """
    return DraftRejection(
        cause=_get(draft, "cause"),
        error_code=REJECT_CODE,
        detail=(
            f"归因草稿与所引证据行对不上（{pending_manual_note(result)}）；"
            "整条作废，不改写模型文本，不进入经办确认流"
        ),
    )


@dataclass(frozen=True)
class DraftRejection:
    """核对不过的草稿 + 拒因（与 :class:`~reconq.align.attribution.RejectedDraft`
    同形，刻意不 import 那个模块以免形成反向依赖）。"""

    cause: str
    error_code: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"cause": self.cause, "error_code": self.error_code, "detail": self.detail}


def screen_drafts(
    drafts: Sequence[Any],
    bundle: EvidenceBundle,
    ledger: Optional["VerificationLedger"] = None,
) -> tuple[list[Any], list[DraftRejection]]:
    """逐条核对，返回 ``(通过的草稿, 被拒的草稿)``。

    **逐条独立**：一条不过不影响其余。批量的成因排序里，Top-1 说错话而
    Top-2 说得对是常态，把整批丢掉等于把唯一说对的那条也扔了。
    """
    kept: list[Any] = []
    rejected: list[DraftRejection] = []
    for draft in drafts:
        result = verify_draft(draft, bundle)
        if ledger is not None:
            ledger.record(result)
        if result.consistent:
            kept.append(draft)
        else:
            rejected.append(draft_rejection(draft, result))
    return kept, rejected


class VerificationLedger:
    """核对统计台账（一致率 + 核对项分布）。

    这一份是**给提示词迭代用的**：哪一类核对项最常不过，直接指向该改
    证据包的组织方式还是该改提示词。它不存任何业务状态。
    """

    def __init__(self, period: str = "") -> None:
        self.period = period
        self.results: list[VerificationResult] = []

    def record(self, result: VerificationResult) -> None:
        self.results.append(result)

    def extend(self, results: Iterable[VerificationResult]) -> None:
        for result in results:
            self.record(result)

    @property
    def checked(self) -> int:
        return len(self.results)

    @property
    def consistent(self) -> int:
        return sum(1 for r in self.results if r.consistent)

    @property
    def rejected(self) -> int:
        return self.checked - self.consistent

    @property
    def consistency_rate(self) -> str:
        """一致率（定点两位带 % 号）。**未核对时是空串而不是 0.00%** ——
        「一条都没核对」与「核对了、全部不一致」在提示词迭代上是两回事。"""
        if not self.results:
            return ""
        return f"{(Decimal(self.consistent) * 100 / Decimal(self.checked)):.2f}%"

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for result in self.results:
            for kind in result.mismatch_kinds:
                out[kind] = out.get(kind, 0) + 1
        return dict(sorted(out.items()))

    def by_note(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for result in self.results:
            for note in result.notes:
                out[note] = out.get(note, 0) + 1
        return dict(sorted(out.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "checked": self.checked,
            "consistent": self.consistent,
            "rejected": self.rejected,
            "consistency_rate": self.consistency_rate,
            "mismatch_by_kind": self.by_kind(),
        }

    def render_zh(self) -> str:
        if not self.results:
            return "归因草稿反幻觉核对：本期未核对任何草稿"
        top = "、".join(
            f"{MISMATCH_ZH.get(kind, kind)}×{count}" for kind, count in self.by_kind().items()
        )
        line = (
            f"归因草稿反幻觉核对 {self.checked} 条：一致 {self.consistent} / "
            f"拒 {self.rejected}（一致率 {self.consistency_rate}）"
        )
        if top:
            line += f" · 不一致项 {top}"
        return line

    def to_run_summary_fields(self) -> dict[str, Any]:
        """给 :class:`reconq.run_summary.RunSummary` 的扁平字段。

        键名与 ``RunSummary`` 字段一一对应，由
        ``build_run_summary(verification=...)`` 收口。
        """
        return {
            "verification_checked": self.checked,
            "verification_consistent": self.consistent,
            "verification_rejected": self.rejected,
            "verification_consistency_rate": self.consistency_rate,
            "verification_mismatch_by_kind": self.by_kind(),
        }
