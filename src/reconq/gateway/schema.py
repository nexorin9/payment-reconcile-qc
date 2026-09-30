"""网关结构化输出与多层校验链（Task 29）。

「提示模型输出 JSON」不是校验，是许愿
----------------------------------------
Task 28 已经把「响应结构不合约定 → 拒绝」这件事做在了网关里（逐字段手写
判断）。本任务把它推到**可验收深度**：用 pydantic v2 声明模型，并把校验
拆成**逐层可定位**的一条链 —— 排障时能一眼看出卡在哪一层，而不是收到一句
「Schema 不对」。

五层校验链
----------

=========================  ==============================  ==============================
层                        判据                            错误码
=========================  ==============================  ==============================
``json``                  响应里能稳健提取出 JSON          ``MODEL_OUTPUT_UNPARSEABLE``
``structure``             pydantic 结构：字段齐全、类型对  ``ATTRIBUTION_DRAFT_INVALID``
                          、无未登记字段
``cause_vocab``           ``cause`` 属于受控成因字典      ``ATTRIBUTION_CAUSE_UNKNOWN``
``evidence_domain``       ``evidence_refs`` 落在证据包    ``ATTRIBUTION_EVIDENCE_REF_INVALID``
                          真实行号域内
``confidence``            ``confidence`` 落在 [0,1]        ``ATTRIBUTION_CONFIDENCE_OUT_OF_RANGE``
``explanation``           ``explanation_zh`` 非空且不超长  ``ATTRIBUTION_EXPLANATION_INVALID``
=========================  ==============================  ==============================

每层**独立错误码**而不是共用一个 ``ATTRIBUTION_DRAFT_INVALID``：财务科在
面板上看到「置信度越界」与「引用了不存在的行」，要做的动作完全不同 ——
前者是让模型改口径，后者是让模型重读证据。共用一个码等于把排障入口又关上了。

三条不肯让的边界
----------------

1. **JSON 提取容忍前后解释文字，但**只容忍**「整段里恰好一份 JSON」**。
   正文里出现第二份 JSON（哪怕只是句子里举的格式示例）一律拒：那就说明
   模型在讲解而不是在作答，捡哪一份都是编的。这与 Task 28 网关里
   「只剥围栏、不做就近匹配」是同一条纪律，只是这里多走了一步 ——
   「前后都是散文」是端点加的客套话，剥掉即可；「正文里有两份 JSON」
   是模型没在作答，剥不掉。
2. **百分制置信度不修。** ``confidence=85`` 不是「差一点点的 0.85」，它
   意味着模型自认为高置信却没按约定写 —— 悄悄除以 100 会让这份草稿不再
   可复算，同一份输入两次归因得到两个不同的置信度，指标表里的
   「成因 Top-1 命中率」就失去意义。
3. **未配置受控字典时，``cause_vocab`` 层报「跳过」而不是「通过」。**
   受控成因字典在 Task 33 落地；在此之前这一层没有判据。把「没有判据」
   记成 pass，等于给未来埋一个「字典加上了却没人发现这一层从来没跑过」的坑。

与 pydantic 的分工（以及为什么 before-validator 里没有 ``raise ValueError``）
------------------------------------------------------------------------------
pydantic v2 的 ``before`` validator 只能靠抛 ``ValueError`` 报告问题，而本
仓库有一条硬规则（:mod:`tests.test_errors` 的 AST 扫描）：产品代码里**不允许
新建非 :class:`~reconq.errors.ReconqError` 的异常** —— 裸 ``ValueError``
冒到面板就是一段没有错误码、没有建议动作的裸栈，``contracts/model.py`` 是
全仓唯一的白名单（那里抛 ``ValueError`` 是因为 pydantic 的 API 就收它）。

所以本模块的分工是：

* **pydantic 管结构**：字段类型、必填、``extra="forbid"``、``min_length``；
* **平铺的普通函数管判据**：逐项预检查（未登记字段、显式类型、区间、
  长度、证据域），抛带错误码与建议动作的 :class:`SchemaLayerError`。

``evidence_refs: list[EvidenceRef]`` 的字符串 → 模型转换也走这条路
(:func:`normalize_ref`)，而不是塞进 before-validator：转换失败要报的
是「这不是非空字符串」这种带业务码的错误，不是 pydantic 的类型错误。

与 Task 28 的兼容
----------------
:func:`reconq.align.attribution.validate_draft` 是 Task 28 留下的**兼容
出口**：它把本模块任一层的失败统一包成 ``ATTRIBUTION_DRAFT_INVALID``，
detail 逐字保留（``越界`` / ``看着像百分制`` / ``未登记字段`` …），
既有断言与既有调用方都不受影响。**分层的码只在
:func:`validate_candidates` 这条新出口上出现** —— 两者刻意并存：老出口给
「这条草稿收不收」，新出口给「卡在哪一层、该让模型改什么」。

源产品对照
----------
参考实现（``soda-core``）**没有这一层**，也不可能有：``contract verify``
拿到的是已经物化好的表格数据，check 执行器读到的是列值，链路上根本没有
「一段不可信的自由文本」。本模块沿用的仍是源产品的主路径纪律 ——
**不合约定就拒、错误要带码与建议动作、失败路径可观测**，只是被校验的对象
从「一行账单」换成了「一次模型输出」。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import ReconqError

logger = logging.getLogger("reconq.gateway.schema")

__all__ = [
    "ATTRIBUTION_DRAFT_FIELDS",
    "AttributionDraft",
    "CandidateDraft",
    "EvidenceRef",
    "LayerOutcome",
    "LAYER_CODES",
    "MAX_EXPLANATION_CHARS",
    "SchemaLayerError",
    "ValidationLayer",
    "ValidationReport",
    "cause_outcome",
    "confidence_outcome",
    "evidence_outcome",
    "explanation_outcome",
    "extract_json",
    "layer_zh",
    "normalize_ref",
    "parse_candidate_text",
    "structure_outcome",
    "to_plain",
    "validate_candidates",
    "validate_draft",
    "validate_draft_layers",
]


# ─────────────────────────── 常量 ───────────────────────────


#: 一条草稿的约定字段。**同时**是「未登记字段」的判据与
#: ``contracts/ATTRIBUTION_SCHEMA``（发给模型的 ``response_format``）的
#: 对照面：两者必须逐字段一致，由
#: ``tests/test_schema_validation.py::test_wire_schema_agrees_with_the_model``
#: 钉死。发模型的是手写 JSON Schema（端点对 ``$ref`` / ``$defs`` 的支持
#: 参差，而 ``strict`` 模式尤其挑剔），本地校验用 pydantic 模型 ——
#: 两份写法不一致时，模型返回的 ``evidence_refs`` 会被自己的 schema 拒掉。
ATTRIBUTION_DRAFT_FIELDS = ("cause", "confidence", "evidence_refs", "explanation_zh")

#: ``explanation_zh`` 长度上限。中文说明写三五行就该说完了；超长基本只有
#: 两种成因：模型在复述整份证据包（等于把账单抄进台账），或者它把多份
#: 草稿的说明粘成了一段。上限不设的话，结论单那一列会被一坨文字撑爆，
#: 而经办人根本读不完 —— 读不完就等于没写。
MAX_EXPLANATION_CHARS = 500


class ValidationLayer(str, Enum):
    """校验链的一层。**层名会进日志与报告**，排障时按层名归类失败。"""

    JSON = "json"
    STRUCTURE = "structure"
    CAUSE_VOCAB = "cause_vocab"
    EVIDENCE_DOMAIN = "evidence_domain"
    CONFIDENCE = "confidence"
    EXPLANATION = "explanation"


#: 层 → 错误码。一层一码，理由见模块头。
LAYER_CODES: dict[ValidationLayer, str] = {
    ValidationLayer.JSON: "MODEL_OUTPUT_UNPARSEABLE",
    ValidationLayer.STRUCTURE: "ATTRIBUTION_DRAFT_INVALID",
    ValidationLayer.CAUSE_VOCAB: "ATTRIBUTION_CAUSE_UNKNOWN",
    ValidationLayer.EVIDENCE_DOMAIN: "ATTRIBUTION_EVIDENCE_REF_INVALID",
    ValidationLayer.CONFIDENCE: "ATTRIBUTION_CONFIDENCE_OUT_OF_RANGE",
    ValidationLayer.EXPLANATION: "ATTRIBUTION_EXPLANATION_INVALID",
}


class SchemaLayerError(ReconqError):
    """某一层校验不过。

    比裸 ``ValueError`` 多带两样排障要用的东西：**层名**（卡在哪一步）与
    **草稿序号**（一批里第几条坏的）。一批草稿收下三条、拒掉两条时，
    只说「Schema 不对」等于让人自己去数。
    """

    def __init__(
        self,
        layer: ValidationLayer,
        detail: str,
        *,
        index: Optional[int] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(code or LAYER_CODES[layer], detail)
        self.layer = layer
        self.index = index

    @property
    def where_zh(self) -> str:
        """「哪一层、第几条」的中文描述。"""
        return f"第 {self.layer.value} 层" + (
            f"（第 {self.index + 1} 条草稿）" if self.index is not None else ""
        )


# ─────────────────────────── pydantic 模型 ───────────────────────────


class EvidenceRef(BaseModel):
    """一条证据引用。

    只存 ``ref`` 字符串（形如 ``wechat_bill.csv:3``）：证据包里行标识就是
    这么写的，本模块**原样抄、原样比**，不做解析成 ``(file, row)`` 再格式化
    回去 —— 那一步会引入「模型写 ``bill.csv:3``、我们存成 ``wechat_bill.csv:3``」
    这种静默对齐，而越界引用正是要靠逐字符比对抓出来的。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str = Field(min_length=1)

    def to_text(self) -> str:
        return self.ref


class AttributionDraft(BaseModel):
    """一条成因草稿（pydantic v2）。

    **类型层面就装不下处置状态**：这里没有 ``disposition``、没有
    ``confirmed_by``、没有 ``status``。想推进台账状态只能另走
    :mod:`reconq.engine.period` 的白名单迁移，而那条迁移只接受人工点的
    参数。

    ``confidence`` 用**严格浮点**（``strict=True``）：字符串 ``"0.8"`` 与
    布尔 ``True`` 都不接受。pydantic 默认的宽松模式会把 ``"0.8"`` 强转成
    ``0.8``、把 ``True`` 强转成 ``1.0`` —— 那正是「修模型的错」，代价是
    这份草稿不再可复算（见模块头第 2 条边界）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    cause: str = Field(min_length=1)
    confidence: float
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    explanation_zh: str = Field(min_length=1)

    def to_plain(self) -> dict[str, Any]:
        """回到线上形态（四字段 + ``evidence_refs`` 为字符串数组）。"""
        return to_plain(self)


class CandidateDraft(BaseModel):
    """一次归因的成因候选集合（响应外层）。

    单条与多条两种形态都要能解析：``[draft]``、``{"drafts": [draft, ...]}``、
    以及**单个 draft 对象**（模型只给一条时很常见）。三条路径都归一到这一
    个模型上，下游只认一种形态。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    drafts: list[AttributionDraft] = Field(min_length=1)

    def to_plain(self) -> dict[str, Any]:
        return {"drafts": [d.to_plain() for d in self.drafts]}


def to_plain(draft: AttributionDraft) -> dict[str, Any]:
    """:class:`AttributionDraft` → 四字段 dict（``EvidenceRef`` 拍回字符串）。"""
    return {
        "cause": draft.cause,
        "confidence": draft.confidence,
        "evidence_refs": [r.ref for r in draft.evidence_refs],
        "explanation_zh": draft.explanation_zh,
    }


# ─────────────────────────── JSON 提取 ───────────────────────────


def extract_json(text: str) -> Any:
    """从模型返回文本里稳健提取 JSON。

    三步，逐级放宽但**不放宽到「就近捡一段」**：

    1. 整段就是 JSON（可能裹着 ```` ```json ```` 围栏）→ 直接用；
    2. 整段有前后散文、**且正文里恰好一份 JSON** → 取那一份；
    3. 正文里有**两份以上** JSON → 拒。

    第 3 步是 Task 28 纪律的延续。端点加的客套话（「以下是分析结果：」）
    剥掉即可；而句子里举的格式示例 + 真正的答案同时出现时，捡哪一份都是
    编的 —— 那种响应说明模型在讲解而不是在作答，整条拒掉并让人去看原文。

    :raises SchemaLayerError: ``json`` 层，``MODEL_OUTPUT_UNPARSEABLE``
    """
    stripped = (text or "").strip()
    if not stripped:
        raise SchemaLayerError(
            ValidationLayer.JSON, "模型返回内容为空（content 为空白）"
        )

    candidate = _strip_fence(stripped)
    parsed = _try_json(candidate)
    if parsed is not _NO_JSON:
        return parsed

    span = _sole_json_span(stripped)
    if span is None:
        raise SchemaLayerError(
            ValidationLayer.JSON,
            f"模型返回里提取不出唯一一份 JSON：{_excerpt(stripped)}。"
            f"若正文里出现了第二份 JSON（哪怕只是举例），本产品宁可拒收，"
            f"也不猜模型想给哪一份",
        )
    fragment, _offset = span
    parsed = _try_json(fragment)
    if parsed is _NO_JSON:  # pragma: no cover - _sole_json_span 已保证可解析
        raise SchemaLayerError(
            ValidationLayer.JSON, f"提取到的片段不是合法 JSON：{_excerpt(fragment)}"
        )
    return parsed


class _NoJson:
    """哨兵：``None`` 是合法 JSON（``null``），不能拿来当「解析失败」。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<no-json>"


_NO_JSON = _NoJson()


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return _NO_JSON


def _strip_fence(text: str) -> str:
    """剥 ```` ```json ```` 围栏。只剥围栏本身，不动里面的内容。"""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) < 2:
        return text
    body = "\n".join(lines[1:]).strip()
    return body[: -len("```")].strip() if body.endswith("```") else body


def _sole_json_span(text: str) -> Optional[tuple[str, int]]:
    """找出正文里**唯一**的顶层 JSON 值。

    返回 ``(片段, 在原文中的起始下标)``；找不到恰好一份时返回 ``None``。
    扫描时逐字符跟踪字符串/转义状态，避免把 JSON 字符串里的 ``{`` 当成
    嵌套结构 —— 那种误判会让「唯一一份」变成「两份」，好内容被误拒。
    """
    spans: list[tuple[str, int]] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char not in "{[":
            index += 1
            continue
        end = _match_value_end(text, index)
        if end is None:
            # 括号不配对：后面不会再有更完整的候选，提前收手。
            break
        fragment = text[index:end]
        if _try_json(fragment) is not _NO_JSON:
            spans.append((fragment, index))
            if len(spans) > 1:
                return None
        # **跳到片段末尾**：片段内部的嵌套值（``{"drafts":[{...}]}`` 里的
        # 那个 ``[{...}]``）已被上面的括号配对覆盖。不跳过去的话，一份
        # 完全正常的响应会被数成「正文里有两份 JSON」而误拒 —— 而这正是
        # 本函数唯一不能犯的错：宁可拒，不能把好内容当成编的。
        index = end
    return spans[0] if len(spans) == 1 else None


def _match_value_end(text: str, start: int) -> Optional[int]:
    """从 ``text[start]`` 处的 ``{``/``[`` 找到配对闭合的**右开区间**下标。"""
    pairs = {"{": "}", "[": "]"}
    stack = [pairs[text[start]]]
    in_string = False
    escaped = False
    for position in range(start + 1, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in ("}", "]"):
            if not stack or stack[-1] != char:
                return None
            stack.pop()
            if not stack:
                return position + 1
    return None


# ─────────────────────────── 单层判据 ───────────────────────────


def normalize_ref(value: Any, *, index: Optional[int] = None) -> EvidenceRef:
    """字符串 → :class:`EvidenceRef`。

    刻意做成**平铺函数**而不是 pydantic ``before``-validator：后者只能抛
    ``ValueError``，而本仓库禁止产品代码抛裸 ``ValueError``（见模块头）。
    """
    if not isinstance(value, str) or not value.strip():
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE,
            f"evidence_refs 里的 {value!r} 不是非空字符串（"
            f"行标识形如 wechat_bill.csv:3，须原样抄自证据包）",
            index=index,
        )
    return EvidenceRef(ref=value.strip())


def _draft_cause(draft: AttributionDraft) -> str:
    return draft.cause.strip()


def _confidence_outcome(draft: AttributionDraft) -> Optional[SchemaLayerError]:
    value = draft.confidence
    if value < 0.0 or value > 1.0:
        hint = (
            "（看着像百分制，约定是 0~1 的小数：85% 请写 0.85）"
            if value > 1.0
            else ""
        )
        return SchemaLayerError(
            ValidationLayer.CONFIDENCE,
            f"confidence={value} 越界，必须落在 [0, 1]{hint}",
        )
    return None


def _explanation_outcome(draft: AttributionDraft) -> Optional[SchemaLayerError]:
    text = draft.explanation_zh.strip()
    if not text:
        return SchemaLayerError(
            ValidationLayer.EXPLANATION, "explanation_zh 必须是非空字符串"
        )
    if len(text) > MAX_EXPLANATION_CHARS:
        return SchemaLayerError(
            ValidationLayer.EXPLANATION,
            f"explanation_zh 长度 {len(text)} 超过上限 {MAX_EXPLANATION_CHARS} 字。"
            f"超长通常是模型把整份证据包复述了一遍 —— 说明该写的是"
            f"「哪几行支持这个成因」，不是把证据再抄一次",
        )
    return None


def _cause_outcome(
    draft: AttributionDraft, vocabulary: Optional[Sequence[str]]
) -> Optional[SchemaLayerError]:
    if vocabulary is None:
        return None
    allowed = {str(item) for item in vocabulary}
    if _draft_cause(draft) in allowed:
        return None
    return SchemaLayerError(
        ValidationLayer.CAUSE_VOCAB,
        f"cause={_draft_cause(draft)!r} 不在受控成因字典内；"
        f"字典现有 {len(allowed)} 项。自由文本成因无法归组、无法统计命中率，"
        f"请改用字典里的成因短语",
    )


def _evidence_outcome(
    draft: AttributionDraft, allowed_refs: Optional[Sequence[str]]
) -> Optional[SchemaLayerError]:
    if allowed_refs is None:
        return None
    allowed = set(allowed_refs)
    outside = [r.ref for r in draft.evidence_refs if r.ref not in allowed]
    if outside:
        return SchemaLayerError(
            ValidationLayer.EVIDENCE_DOMAIN,
            f"引用了证据包中不存在的行 {outside}；证据包实际可引用 {len(allowed)} 行。"
            f"整条作废 —— 不做就近匹配（把 :18 悄悄改成 :17 会让一份编造的结论"
            f"看起来有据可查）",
        )
    return None


# ─────────────────────────── 逐层判据（结果形态） ───────────────────────────


@dataclass(frozen=True)
class LayerOutcome:
    """一层的判定结果。

    ``status`` 三态而不是两态：``skipped`` 专门表示「这一层没有判据」。
    把「没跑」记成 ``passed`` 是本模块最想避免的事 —— 受控成因字典在
    Task 33 才落地，这期间 ``cause_vocab`` 层必须是 ``skipped``，
    否则字典加上时会静悄悄继承一个「从来没跑过」的通过记录。
    """

    layer: ValidationLayer
    status: str  # pass | fail | skipped
    code: str = ""
    detail: str = ""
    index: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status in ("pass", "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
            "index": self.index,
        }


def _outcome(
    layer: ValidationLayer, error: Optional[SchemaLayerError], *, index: Optional[int] = None
) -> LayerOutcome:
    if error is None:
        return LayerOutcome(layer=layer, status="pass", index=index)
    return LayerOutcome(
        layer=layer,
        status="fail",
        code=error.code,
        detail=error.detail,
        index=error.index if error.index is not None else index,
    )


def structure_outcome(draft: AttributionDraft, *, index: Optional[int] = None) -> LayerOutcome:
    """``structure`` 层：pydantic 结构是否成立。

    走到这里说明 :class:`AttributionDraft` 已经构造成功，故本层**只会
    pass** —— 它是「为什么下面几层能拿到一个对象」的凭据。构造阶段的失败
    由 :func:`_build_draft` 归到本层。
    """
    return LayerOutcome(layer=ValidationLayer.STRUCTURE, status="pass", index=index)


def cause_outcome(
    draft: AttributionDraft,
    vocabulary: Optional[Sequence[str]] = None,
    *,
    index: Optional[int] = None,
) -> LayerOutcome:
    """``cause_vocab`` 层。未传字典 → ``skipped``。"""
    if vocabulary is None:
        return LayerOutcome(
            layer=ValidationLayer.CAUSE_VOCAB,
            status="skipped",
            detail="未提供受控成因字典（Task 33 落地），本层无判据、未参与判定",
            index=index,
        )
    return _outcome(ValidationLayer.CAUSE_VOCAB, _cause_outcome(draft, vocabulary), index=index)


def evidence_outcome(
    draft: AttributionDraft,
    allowed_refs: Optional[Sequence[str]] = None,
    *,
    index: Optional[int] = None,
) -> LayerOutcome:
    """``evidence_domain`` 层。未给证据包行号域 → ``skipped``。"""
    if allowed_refs is None:
        return LayerOutcome(
            layer=ValidationLayer.EVIDENCE_DOMAIN,
            status="skipped",
            detail="未提供证据包行号域，本层无判据、未参与判定",
            index=index,
        )
    return _outcome(
        ValidationLayer.EVIDENCE_DOMAIN, _evidence_outcome(draft, allowed_refs), index=index
    )


def confidence_outcome(draft: AttributionDraft, *, index: Optional[int] = None) -> LayerOutcome:
    """``confidence`` 层：区间。"""
    return _outcome(ValidationLayer.CONFIDENCE, _confidence_outcome(draft), index=index)


def explanation_outcome(draft: AttributionDraft, *, index: Optional[int] = None) -> LayerOutcome:
    """``explanation`` 层：非空 + 长度上限。"""
    return _outcome(ValidationLayer.EXPLANATION, _explanation_outcome(draft), index=index)


# ─────────────────────────── 校验主链 ───────────────────────────


def _build_draft(raw: Any, *, index: Optional[int] = None) -> AttributionDraft:
    """原始 dict → :class:`AttributionDraft`。**不合约定抛 ``structure`` 层错**。

    顺序刻意是「未登记字段 → 缺字段 → 逐字段预检查 → pydantic」：
    * 未登记字段排第一，因为 ``{"disposition": "已确认"}`` 必须在任何人
      看到这条草稿之前就被挡住 —— 草稿结构里**装不下**处置状态，这是
      Task 28 立下的类型级边界，不能因为模型多写一个字段就漏过去；
    * 预检查在 pydantic 之前，因为预检查能给**带业务语气的** detail
      （「看着像百分制」），而 pydantic 的 ``msg`` 是英文的
      ``Input should be less than or equal to 1``。
    """
    if not isinstance(raw, dict):
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE,
            f"草稿必须是对象，实际是 {type(raw).__name__}",
            index=index,
        )
    unknown = sorted(set(raw) - set(ATTRIBUTION_DRAFT_FIELDS))
    if unknown:
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE,
            f"草稿出现未登记字段 {unknown}；约定字段只有 "
            f"{sorted(ATTRIBUTION_DRAFT_FIELDS)}",
            index=index,
        )

    cause = raw.get("cause")
    if not isinstance(cause, str) or not cause.strip():
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE, "cause 必须是非空字符串", index=index
        )

    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE,
            f"confidence 必须是 0~1 的数字，实际是 {confidence!r}"
            f"（{type(confidence).__name__}）。字符串与布尔一律拒收："
            f"强转会让这份草稿不再可复算",
            index=index,
        )

    refs = raw.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE, "evidence_refs 必须是非空数组", index=index
        )
    normalized = [normalize_ref(item, index=index) for item in refs]

    explanation = raw.get("explanation_zh")
    if not isinstance(explanation, str):
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE,
            f"explanation_zh 必须是非空字符串，实际是 {explanation!r}"
            f"（{type(explanation).__name__}）",
            index=index,
        )
    if not explanation.strip():
        # 「不是字符串」是结构问题；「是字符串但空」归 explanation 层 ——
        # 那是「说明写得太少」，要让人做的事不一样（去问经办人补充背景，
        # 而不是让模型把字段补齐）。刻意在进 pydantic 之前拦：模型的
        # ``min_length=1`` 会把空串并进 structure 层的 ValidationError。
        raise SchemaLayerError(
            ValidationLayer.EXPLANATION, "explanation_zh 必须是非空字符串", index=index
        )

    payload = {
        "cause": cause.strip(),
        # 区间不在 pydantic 的 Field 里设：越界要单独占一层、单独一个码，
        # 而 Field(ge/le) 只会把它并进 structure 层的 ValidationError。
        "confidence": confidence,
        "evidence_refs": normalized,
        "explanation_zh": explanation.strip(),
    }
    try:
        return AttributionDraft.model_validate(payload)
    except ValidationError as exc:
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE,
            "草稿结构不符合约定：" + "; ".join(_format_pydantic_error(e) for e in exc.errors()),
            index=index,
        ) from exc


def _format_pydantic_error(error: Mapping[str, Any]) -> str:
    location = ".".join(str(part) for part in error.get("loc", ())) or "(根)"
    return f"{location}：{error.get('msg', '不合约定')}"


def validate_draft_layers(
    raw: Any,
    *,
    allowed_refs: Optional[Sequence[str]] = None,
    vocabulary: Optional[Sequence[str]] = None,
    index: Optional[int] = None,
) -> tuple[AttributionDraft, list[LayerOutcome]]:
    """跑完整六层，返回 ``(合格草稿, 逐层结果)``。

    :raises SchemaLayerError: 任一层不过。层名在 ``exc.layer``、序号在
        ``exc.index``、错误码即该层的码。
    """
    draft = _build_draft(raw, index=index)
    outcomes = [structure_outcome(draft, index=index)]
    checks: tuple[tuple[ValidationLayer, SchemaLayerError | None], ...] = (
        (ValidationLayer.CAUSE_VOCAB, _cause_outcome(draft, vocabulary)),
        (ValidationLayer.EVIDENCE_DOMAIN, _evidence_outcome(draft, allowed_refs)),
        (ValidationLayer.CONFIDENCE, _confidence_outcome(draft)),
        (ValidationLayer.EXPLANATION, _explanation_outcome(draft)),
    )
    for layer, error in checks:
        if layer is ValidationLayer.CAUSE_VOCAB:
            outcome = cause_outcome(draft, vocabulary, index=index)
        elif layer is ValidationLayer.EVIDENCE_DOMAIN:
            outcome = evidence_outcome(draft, allowed_refs, index=index)
        elif layer is ValidationLayer.CONFIDENCE:
            outcome = confidence_outcome(draft, index=index)
        else:
            outcome = explanation_outcome(draft, index=index)
        outcomes.append(outcome)
        if error is not None:
            # 把批次序号补进异常对象再抛：上面几层的判据函数是**单条**
            # 作用域的（它们不该知道自己在第几条），而「一批里第几条被拒」
            # 恰恰是排障时要的第一个问题。
            if error.index is None:
                error.index = index
            raise error
    return draft, outcomes


def validate_draft(
    raw: Any,
    *,
    allowed_refs: Optional[Sequence[str]] = None,
    vocabulary: Optional[Sequence[str]] = None,
) -> AttributionDraft:
    """只取合格草稿（不通过即抛）。"""
    draft, _ = validate_draft_layers(raw, allowed_refs=allowed_refs, vocabulary=vocabulary)
    return draft


@dataclass(frozen=True)
class ValidationReport:
    """一批草稿的校验结果。

    ``drafts`` 是**收下**的（按模型给出的原顺序，排名由归因层做），``rejected``
    是**逐条**拒因。拒一条不废全批 —— 一条编造的草稿不该把同一批里
    另外三条正经结论一起带走。
    """

    drafts: tuple[AttributionDraft, ...] = ()
    rejected: tuple[SchemaLayerError, ...] = ()
    outcomes: tuple[LayerOutcome, ...] = field(default_factory=tuple)
    raw_count: int = 0

    @property
    def ok(self) -> bool:
        """**至少收到一条可用草稿**。

        刻意不等于「一条都没被拒」：一批里 3 条收 2 条是**成功**，不是
        失败 —— 那条被拒的是模型编的，收下它才是事故。调用方要判断
        「模型是不是完全没给出可用东西」看这一条；要判断「有没有内容
        被丢」看 :attr:`complete`。
        """
        return bool(self.drafts)

    @property
    def complete(self) -> bool:
        """一条都没被拒。面板上「给了 N 条、收下 N 条」那一格用它。"""
        return bool(self.drafts) and not self.rejected

    def failures_by_layer(self) -> dict[str, int]:
        """按层统计失败条数。面板上「模型输出质量」那一格直接用它。"""
        counts: dict[str, int] = {}
        for error in self.rejected:
            counts[error.layer.value] = counts.get(error.layer.value, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "complete": self.complete,
            "raw_count": self.raw_count,
            "accepted": len(self.drafts),
            "rejected": len(self.rejected),
            "drafts": [d.to_plain() for d in self.drafts],
            "rejected_detail": [
                {
                    "layer": e.layer.value,
                    "code": e.code,
                    "index": e.index,
                    "detail": e.detail,
                }
                for e in self.rejected
            ],
            "failures_by_layer": self.failures_by_layer(),
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


def _iter_raw_drafts(raw: Any) -> list[Any]:
    """把单条 / 多条 / ``{"drafts": [...]}`` 三种响应形态摊平成一个列表。

    外层包不包、多条还是单条，是**格式差异不是语义差异** —— 为它烧掉一次
    重试额度不划算（重试要钱，且同样的形态下次还会来）。真正不合约定的
    形态（顶层是字符串、``drafts`` 不是数组、数组为空）仍然显式报错。
    """
    if isinstance(raw, list):
        items: Any = raw
    elif isinstance(raw, dict):
        if "drafts" not in raw:
            if set(raw) == set(ATTRIBUTION_DRAFT_FIELDS):
                return [raw]  # 单条草稿，省掉外层包装
            raise SchemaLayerError(
                ValidationLayer.STRUCTURE,
                f"响应须含 drafts 数组，实际键为 {sorted(raw)}；"
                f"单个草稿也须包在 drafts 里",
            )
        items = raw["drafts"]
    else:
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE,
            f"响应顶层须为对象或数组，实际是 {type(raw).__name__}",
        )
    if not isinstance(items, list):
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE, f"drafts 必须是数组，实际是 {type(items).__name__}"
        )
    if not items:
        raise SchemaLayerError(
            ValidationLayer.STRUCTURE, "drafts 为空：模型没有给出任何成因草稿"
        )
    return list(items)


def validate_candidates(
    raw: Any,
    *,
    allowed_refs: Optional[Sequence[str]] = None,
    vocabulary: Optional[Sequence[str]] = None,
    outer_index: Optional[int] = None,
) -> ValidationReport:
    """一批草稿 → :class:`ValidationReport`。

    外层形态错（顶层不是对象/数组、``drafts`` 为空）直接抛
    :class:`SchemaLayerError`；**逐条**的错收进 ``rejected``，好草稿照收。
    """
    items = _iter_raw_drafts(raw)
    drafts: list[AttributionDraft] = []
    rejected: list[SchemaLayerError] = []
    outcomes: list[LayerOutcome] = []
    for position, item in enumerate(items):
        index = position if outer_index is None else outer_index
        try:
            draft, item_outcomes = validate_draft_layers(
                item, allowed_refs=allowed_refs, vocabulary=vocabulary, index=index
            )
        except SchemaLayerError as exc:
            rejected.append(exc)
            outcomes.append(
                LayerOutcome(
                    layer=exc.layer,
                    status="fail",
                    code=exc.code,
                    detail=exc.detail,
                    index=index,
                )
            )
            continue
        drafts.append(draft)
        outcomes.extend(item_outcomes)
    return ValidationReport(
        drafts=tuple(drafts),
        rejected=tuple(rejected),
        outcomes=tuple(outcomes),
        raw_count=len(items),
    )


def parse_candidate_text(
    text: str,
    *,
    allowed_refs: Optional[Sequence[str]] = None,
    vocabulary: Optional[Sequence[str]] = None,
) -> ValidationReport:
    """模型原始文本 → :class:`ValidationReport`（提取 + 六层校验一步到位）。"""
    return validate_candidates(
        extract_json(text), allowed_refs=allowed_refs, vocabulary=vocabulary
    )


def layer_zh(layer: ValidationLayer | str) -> str:
    """层名的中文说明，供面板分组显示。"""
    value = layer.value if isinstance(layer, ValidationLayer) else str(layer)
    return {
        "json": "响应里提取不出唯一一份 JSON",
        "structure": "草稿结构不合约定",
        "cause_vocab": "成因不在受控字典内",
        "evidence_domain": "引用了证据包之外的行",
        "confidence": "置信度越界",
        "explanation": "中文说明为空或超长",
    }.get(value, value)


def _excerpt(text: str, limit: int = 300) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…（已截断）"
