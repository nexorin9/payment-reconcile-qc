"""归因提示词模板与提示词版本（Task 34）。

为什么要版本化
--------------
提示词是**代码里唯一一处会被「凭感觉改」的地方**。改之前没人说得清
「这次改动到底让归因变好了还是变坏了」——因为模型输出本来就有随机性，
改完跑一期看着差不多就上去了，等到某天发现 Top-1 命中率从 0.7 掉到 0.4，
已经没人记得三周前动过哪句话。

本模块把提示词收成三样东西：

1. :class:`PromptTemplate` —— **一份模板 + 一个版本串**，版本串进
   :mod:`reconq.audit.model_calls` 的缓存键（见
   :func:`reconq.audit.model_calls.cache_key`），改模板不改版本串的后果
   是「改了提示词却继续吃旧答案，且没有任何痕迹能解释为什么输出没变」。
2. :data:`PROMPT_VERSION_DIGEST` —— 模板正文的 sha256。改了模板而忘了改
   版本串，:func:`assert_prompt_registry_consistent` 会当场报出来。
3. :func:`assert_no_channel_specialcase` —— **模板里不许出现渠道特例**。

为什么模板里不许出现渠道特例
----------------------------
「微信账单要带 ``bill_type=ALL`` 才能看到退费」这句话一旦写进提示词模板，
就等于把渠道版本知识焊死在提示词里：换一家渠道要改提示词，改提示词要重跑
整套回放，而回放集里的期望又是按旧提示词录的。**渠道差异属于证据包**——
:mod:`reconq.align.bundle` 已经在 ``channel_meta.bill_type`` 里带了
（并且标了 ``bill_type_source``），模型看得见；模板只需要说「按证据包里
给的渠道口径判」，不该复述任何一个渠道的取件参数。

这条不是注释层面的自律，是**代码层面的拒绝**：:func:`assert_no_channel_specialcase`
扫到渠道名或 ``bill_type=`` 字面量就抛 :class:`~reconq.errors.ReconqError`。
留着的代价是每接一个渠道改一次提示词版本；拒掉的收益是提示词只描述
「怎么读证据」，渠道知识只活在证据包与契约里。

模板里允许注入什么
------------------
**只有两样**：:func:`render` 的 ``bundle_text``（证据包正文）与
``vocabulary``（受控成因字典，由 :mod:`reconq.align.causes` 在调用时取）。
字典是**渲染时注入**而不是写死在模板里 —— 字典改了（新增一条成因、
改一条判别线索）不需要动提示词版本串，因为字典本身有独立的版本与指纹
（:func:`reconq.align.causes.dictionary_digest`）。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层，也没有需要有**：它的
``contract verify`` 输出的是**确定性判定**（「这一行的这一列不满足这条
口径」），没有「为什么会有这一行」这个问题，自然也就没有提示词、也就没有
提示词漂移。本模块新增的是「模型那一段话也要纳入版本管理」这一跳 ——
沿用的仍是源产品主路径纪律：判据收口在一处、版本进缓存键与审计链、
结论交人工。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from ..errors import ReconqError

__all__ = [
    "CHANNEL_SPECIALCASE_TOKENS",
    "PROMPTS",
    "PROMPT_VERSION",
    "PROMPT_VERSION_DIGEST",
    "PromptTemplate",
    "assert_no_channel_specialcase",
    "assert_prompt_registry_consistent",
    "prompt_digest",
    "register_prompt",
    "render",
    "template_of",
]


#: 模板正文里**不许出现**的渠道特例字面量。小写形式用于匹配。
#:
#: 收录的是「渠道/取件参数层面的知识」而不是「渠道中文名」的泛化词：
#: 「渠道」两个字本身在模板里是允许的（要区分渠道侧与 HIS 侧），
#: 出现的是「微信」「支付宝」「银联」「自助机」这类**具体渠道名**，
#: 以及 ``bill_type=ALL`` 这类**渠道取件参数**。
CHANNEL_SPECIALCASE_TOKENS: tuple[str, ...] = (
    "微信",
    "支付宝",
    "银联",
    "自助机",
    "wechat",
    "alipay",
    "unionpay",
    "self_service",
    "self-service",
    "bill_type",
    "aes-gcm",
    "aes_gcm",
)


# ─────────────────────────── 模板 ───────────────────────────


@dataclass(frozen=True)
class PromptTemplate:
    """一份归因提示词模板。

    :attr:`system` 必须**跨差异不变** —— provider 侧的 prompt 缓存按前缀
    命中，逐条内容混进去会让六路两千行差异的费用翻倍。逐条内容（证据包
    正文、补充说明）一律进 :attr:`user` 段。
    """

    version: str
    system: str
    #: 这一版相对上一版改了什么。**必填**：只有当「为什么改」能被复述出来，
    #: 回放报告里那个「命中率 +0.12」才有归属；否则半年后看到两份指标就
    #: 只能干瞪眼。
    change_note: str = ""
    #: 本版要求模型用哪种形式作答。``code`` = 必须用受控成因编码；
    #: ``phrase`` = 自由短语（历史版本，字典筛不掉，命中率天然低一截）。
    cause_form: str = "code"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "change_note": self.change_note,
            "cause_form": self.cause_form,
        }


#: 归因系统提示词。**模板正文**（不含任何渠道特例，渲染时会校验）。
_BASE_SYSTEM = (
    "你是医院支付对账的归因助手。你会看到一条对账差异的全部证据"
    "（差异行、全部候选行、该渠道当期口径、账期时间窗、近几期同因历史）。\n"
    "请给出最可能的成因排序，每条形如：\n"
    '{"cause": 成因, "confidence": 0~1 的小数, '
    '"evidence_refs": ["<证据行标识>"], "explanation_zh": 中文说明}\n'
    "硬性要求：\n"
    "1. evidence_refs 只能**原样抄**证据里出现过的行标识，不得改写、"
    "不得编造、不得写「见上」；没有证据支撑的成因不要写。\n"
    "2. 你只给**草稿**供人工判断，不做最终结论。\n"
    "3. 证据不足时如实说明，不要编一个说得通的成因。\n"
    "4. 判定成因时以证据包里给出的该渠道当期口径为准，不要套用别家渠道"
    "或别的账期的取件规则。\n"
    "5. 只输出 JSON，不要输出解释性文字或代码块围栏。"
)


#: 受控成因字典的注入段。**字典内容在渲染时填入**，模板里只留标题与
#: 格式约定 —— 字典改一条判别线索不该 bump 提示词版本（字典有自己的版本）。
_VOCAB_HEADER = "可选成因（只能用下列编码之一，不要自造）："


#: 提示词注册表。**版本串进缓存键**，正文进 :data:`PROMPT_VERSION_DIGEST`。
#:
#: ``v1``（历史版）：要求模型给中文成因短语。留着不是为了回退，是为了
#: 让回放集里那批 ``v1`` 录下来的响应仍然能被评分 —— 换版本后拿新一版
#: 与旧一版逐条对比，「到底是哪几条变好了」才有答案。
#: ``v2``（当前版）：要求直接给受控成因编码。字典外成因在
#: :func:`reconq.align.causes.screen_hits` 那一步就能拒掉，不必等到人工
#: 确认时才发现模型自造了一个词。
PROMPTS: dict[str, PromptTemplate] = {
    "v1": PromptTemplate(
        version="v1",
        system=_BASE_SYSTEM,
        change_note="初版：成因给中文短语，不注入受控字典。",
        cause_form="phrase",
    ),
    "v2": PromptTemplate(
        version="v2",
        system=(
            _BASE_SYSTEM
            + "\n6. 「cause」必须是下方可选成因清单里的**编码原文**"
            "（如 channel_late_posting），不要写中文名、不要自造新词；"
            "确实判不出来就用 undetermined。"
        ),
        change_note=(
            "v2：第 6 条要求 cause 用受控编码，并注入成因字典。"
            "针对 v1 的两个实测问题 —— 模型自造成因词导致字典筛不掉、"
            "中文名与编码混用导致按成因聚合时同一件事落成两行。"
        ),
        cause_form="code",
    ),
    # ``v2-rc1``：**已被否决的一版**，留在注册表里是为了让回放有得比。
    # 它与 v2 只差一句：把「判不出来就说 undetermined」删掉了。回放实测
    # 正是这一句的价值 —— 少了它，模型在信息耗尽的那条上会去挑一条
    # 「最像的」成因（Top-1 从 undetermined 变成 split_payment_unallocated），
    # 而 Top-1 命中率**看总分几乎没动**，只有逐条看才发现那一条错了。
    # 这条留在注册表里，是为了让「当初否决它的那份证据」可复算，而不是
    # 只留一句「试过，不行」。
    "v2-rc1": PromptTemplate(
        version="v2-rc1",
        system=(
            _BASE_SYSTEM
            + "\n6. 「cause」必须是下方可选成因清单里的**编码原文**"
            "（如 channel_late_posting），不要写中文名、不要自造新词。"
        ),
        change_note=(
            "v2-rc1：v2 去掉「判不出来就用 undetermined」一句后的候选版。"
            "已被回放否决：信息耗尽的那条上模型不再说 undetermined，"
            "转而硬猜成因，Top-1 由对变错。保留在注册表里供复算"
        ),
        cause_form="code",
    ),
}

#: 当前生效的提示词版本。**改模板必改这里**（否则回放对比会拿同一份
#: 模板的两个名字做比较，比出来的「差异」全是噪声）。
PROMPT_VERSION = "v2"


def template_of(version: Optional[str] = None) -> PromptTemplate:
    """取模板；未登记的版本号显式报错，不回落默认版。

    回落默认版是最坏的一种错法：``--compare v9,v2`` 会静默变成
    ``v2,v2``，比出来的「无变化」会被读成「v9 效果一样」。
    """
    key = (version or PROMPT_VERSION).strip()
    template = PROMPTS.get(key)
    if template is None:
        raise ReconqError(
            "PROMPT_VERSION_UNKNOWN",
            f"提示词版本 {key!r} 未登记；已登记：{sorted(PROMPTS)}。"
            "版本串要进缓存键与审计链，拼错等于静默吃旧答案",
        )
    return template


# ─────────────────────────── 指纹与一致性 ───────────────────────────


def prompt_digest(template: PromptTemplate) -> str:
    """模板正文 + 成因形式的 sha256（前 16 位）。

    **只对模板本身取指纹，不含字典**：字典每天可能因补一条成因而变，
    把它算进来会让提示词指纹跟着抖，缓存键跟着抖，回放报告里
    「今天命中率变了」的第一嫌疑就变成「字典变了」而不是「提示词变了」。
    字典有自己的版本与指纹（:func:`reconq.align.causes.dictionary_digest`）。
    """
    raw = f"{template.version}\n{template.cause_form}\n{template.system}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


#: 当前版本模板的指纹。**改模板不改这个值就是漏改**，
#: :func:`assert_prompt_registry_consistent` 会报出来。
PROMPT_VERSION_DIGEST: str = prompt_digest(template_of(PROMPT_VERSION))


def assert_prompt_registry_consistent() -> None:
    """注册表自洽性：当前版本的模板没被偷改、版本串与字典键对得上。

    两个方向都会漏：

    * 改了 ``PROMPTS[PROMPT_VERSION].system`` 却没 bump 版本 → 同一版本串
      对应两份模板正文，缓存键不变、审计链不变，而输出已经变了；
    * ``PROMPT_VERSION`` 拼错（指向一个不存在的键）→ :func:`template_of`
      在首次调用时才炸，而首次调用往往发生在半夜的扫描里。
    """
    template = template_of(PROMPT_VERSION)
    actual = prompt_digest(template)
    if actual != PROMPT_VERSION_DIGEST:
        raise ReconqError(
            "PROMPT_DIGEST_MISMATCH",
            f"提示词版本 {PROMPT_VERSION!r} 的正文指纹是 {actual}，"
            f"而 PROMPT_VERSION_DIGEST 记的是 {PROMPT_VERSION_DIGEST}。"
            "改了模板正文就必须 bump 版本串（版本串进缓存键，不 bump 会"
            "继续吃旧答案且没有痕迹能解释）",
        )
    for key, item in PROMPTS.items():
        if item.version != key:
            raise ReconqError(
                "PROMPT_REGISTRY_INCONSISTENT",
                f"注册表键 {key!r} 与模板自报版本 {item.version!r} 不一致",
            )
        if item.cause_form not in ("code", "phrase"):
            raise ReconqError(
                "PROMPT_REGISTRY_INCONSISTENT",
                f"提示词 {key!r} 的 cause_form={item.cause_form!r} 不是 "
                "code / phrase 之一",
            )
        if not item.change_note.strip():
            raise ReconqError(
                "PROMPT_REGISTRY_INCONSISTENT",
                f"提示词 {key!r} 没写 change_note —— 「为什么改」写不出来，"
                "回放报告里那个命中率变化就永远没有归属",
            )


def register_prompt(template: PromptTemplate) -> PromptTemplate:
    """登记一版新模板（改完提示词走这里，顺手补 change_note）。

    **不自动 bump** :data:`PROMPT_VERSION` —— 「新增一版」与「切到新版」
    是两个决定，后者会让线上扫描立刻换措辞。
    """
    assert_no_channel_specialcase(template.system, where=f"提示词 {template.version}")
    PROMPTS[template.version] = template
    return template


# ─────────────────────────── 渲染 ───────────────────────────


def assert_no_channel_specialcase(text: str, *, where: str = "提示词模板") -> None:
    """模板正文里不许出现渠道特例字面量。

    :raises ReconqError: ``PROMPT_CHANNEL_SPECIALCASE``
    """
    lowered = (text or "").lower()
    hits = [token for token in CHANNEL_SPECIALCASE_TOKENS if token in lowered]
    if hits:
        raise ReconqError(
            "PROMPT_CHANNEL_SPECIALCASE",
            f"{where}里出现渠道特例 {sorted(hits)}。渠道取件口径属于证据包"
            "（channel_meta.bill_type），不属于提示词 —— 把渠道知识写进模板，"
            "等于让换渠道 / 改渠道参数都变成一次提示词改版，"
            "而回放集的期望是按旧措辞录的",
        )


def _vocab_block(vocabulary: Sequence[str]) -> str:
    lines = [_VOCAB_HEADER]
    lines.extend(f"- {item}" for item in vocabulary)
    return "\n".join(lines)


def render(
    bundle_text: str,
    *,
    version: Optional[str] = None,
    instructions: str = "",
    vocabulary: Sequence[str] = (),
) -> list[dict[str, str]]:
    """证据包文本 → OpenAI-compatible messages。

    :param vocabulary: 受控成因清单（**编码 + 中文名**，由调用方从
        :mod:`reconq.align.causes` 取）。渲染时注入，模板里不写死。
    """
    template = template_of(version)
    assert_no_channel_specialcase(template.system, where=f"提示词 {template.version}")

    system = template.system
    if template.cause_form == "code" and vocabulary:
        system = f"{system}\n{_vocab_block(vocabulary)}"

    user = f"以下是这条差异的证据：\n\n{bundle_text}"
    if instructions:
        user += f"\n\n补充说明：{instructions}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def messages_digest(messages: Sequence[Mapping[str, str]]) -> str:
    """整段 messages 的指纹（前 16 位）。回放报告用它确认「录的那批
    响应确实是在这版提示词下拿到的」。"""
    raw = "\n".join(f"{m.get('role', '')}:{m.get('content', '')}" for m in messages)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
