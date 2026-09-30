"""归因 Schema 版本与「提示词 × Schema」成对记账（Task 34）。

为什么提示词与 Schema 要成对记，而不是各记各的
--------------------------------------------
两者都会让同一份证据包的答案变，但影响面却完全不同：

* **提示词**变了 → 模型措辞变了 → 同一份证据可能给出不同的成因排序；
* **Schema** 变了（增删草稿字段、收紧枚举、放宽必填）→ 网关会把
  **以前能过的响应判成不合协议**（``MODEL_SCHEMA_INVALID``），
  或者反过来把以前拒掉的收下。

只记提示词版本，会出现一种最难查的现象：某天命中率掉了一截，翻审计链
发现提示词版本没变、模型名没变、证据包指纹也没变 —— 于是「明明什么都没改」。
真相是 Schema 那一侧动过，而它没留痕。**两者必须成对落进同一条审计记录**，
且成对的值要能被单独复算（:func:`current_pair`）。

Schema 版本号与指纹是两样东西
-----------------------------
``SCHEMA_VERSION`` 是人给的版本串（``attrib-schema/v1``），
:func:`schema_fingerprint` 是草稿 Schema 正文的 sha256。**两者对不上就是
漏改版本号**：改了 ``ATTRIBUTION_SCHEMA`` 的 ``required`` 却没 bump 版本，
那么这一版 Schema 在审计链上就与上一版长得一模一样，而它们对同一份响应
的判定可能完全相反。:func:`assert_schema_fingerprint_matches` 在每次取
版本对时都核一遍，漏改当场报出来。

与审计表的关系
--------------
成对的值落 :mod:`reconq.audit.model_calls` 的 ``prompt_version`` 与
``schema_version`` 两列；``schema_version`` 参与链式指纹
（:func:`reconq.audit.model_calls._call_digest`），因此**事后改审计记录里的
Schema 版本会被链校验报出来**。老库迁移过来、该列为 ``NULL`` 的行按
**旧字段集**重算指纹（见该函数注释），历史链不因此整条报红。

与源产品的关系
--------------
参考实现（``soda-core``）有 ``contract verify`` 的确定性判定，
**不需要 Schema 版本** —— 判据是代码与 YAML 契约，改了就是改了，重跑即可。
本产品多出来的这一跳是「判据的一部分搬进了发给模型的 JSON Schema」，
于是那份 Schema 也成了必须版本化的输入。沿用的仍是主路径纪律：
判据收口在一处、版本进缓存键与审计链、结论交人工。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from ..errors import ReconqError
from ..gateway.schema import ATTRIBUTION_DRAFT_FIELDS
from . import prompts as _prompts

__all__ = [
    "SCHEMA_FINGERPRINT",
    "SCHEMA_VERSION",
    "VersionPair",
    "assert_schema_fingerprint_matches",
    "current_pair",
    "pair_of",
    "record_attribution_call",
    "schema_fingerprint",
]


#: 归因草稿 Schema 的结构版本。**改 :data:`reconq.align.attribution.ATTRIBUTION_SCHEMA`
#: 的字段 / 必填 / 枚举就必须 bump 它**，否则审计链上两版长得一样，
#: 而它们对同一份响应的判定可能相反。
SCHEMA_VERSION = "attrib-schema/v1"

#: 指纹取前 16 位十六进制，与提示词指纹同规格（够短、够长、跨两处可比）。
_DIGEST_LEN = 16


def _schema_source() -> Mapping[str, Any]:
    """Schema 正文的**唯一真源**。

    刻意从 :mod:`reconq.align.attribution` 取而不是在本地重写一遍：
    两份写法不一致时，发给模型的 ``response_format`` 与本地校验用的
    Schema 会分叉，而这种分叉在真实端点上要到某次模型恰好少写一个字段
    时才暴露（:mod:`reconq.align.attribution` 模块头同源理由）。
    """
    from .attribution import ATTRIBUTION_SCHEMA

    return ATTRIBUTION_SCHEMA


def schema_fingerprint() -> str:
    """草稿 Schema 正文 + 字段清单的 sha256（前 16 位）。

    刻意**只对 Schema 取指纹**，不含提示词正文：两者的指纹分别在
    :func:`reconq.align.prompts.prompt_digest` 与此，成对出现在
    :class:`VersionPair` 里，混成一个哈希就分不清是提示词动的还是 Schema 动的。
    """
    raw = json.dumps(
        {"fields": list(ATTRIBUTION_DRAFT_FIELDS), "schema": _schema_source()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


#: 当前 Schema 的指纹。**改 Schema 正文却不 bump 版本号就是漏改**，
#: :func:`assert_schema_fingerprint_matches` 会报出来。
SCHEMA_FINGERPRINT: str = schema_fingerprint()


def assert_schema_fingerprint_matches() -> None:
    """Schema 正文与 :data:`SCHEMA_FINGERPRINT` 对得上。

    对不上意味着有人改了 Schema 正文却没 bump :data:`SCHEMA_VERSION`。
    这时**不能靠「指纹自己会变」蒙混过去**：版本串没变 = 缓存键没变 =
    改完 Schema 之后仍在吃改之前的缓存答案，且审计链上看不出任何变化。
    """
    actual = schema_fingerprint()
    if actual != SCHEMA_FINGERPRINT:
        raise ReconqError(
            "SCHEMA_FINGERPRINT_MISMATCH",
            f"归因 Schema 正文指纹是 {actual}，而 SCHEMA_FINGERPRINT 记的是 "
            f"{SCHEMA_FINGERPRINT}。改了 ATTRIBUTION_SCHEMA 的字段 / 必填 / "
            "枚举就必须 bump SCHEMA_VERSION —— 版本串进缓存键与审计链，"
            "不 bump 会继续吃改之前的缓存答案，且链上看不出任何变化",
        )


@dataclass(frozen=True)
class VersionPair:
    """「提示词版本 × Schema 版本」这一对。

    **成对而非各记各的**：两者任一变化都会让同一份证据包的答案变，只记一个
    就会出现「什么都没改但命中率掉了」这种无法排查的现象（见模块头）。
    """

    prompt_version: str
    prompt_digest: str
    schema_version: str
    schema_digest: str
    #: 受控成因字典的指纹。**不进缓存键**：字典改一条判别线索不该让全部
    #: 缓存失效（那会让一次补字典的操作把这一期的缓存命中率清零），
    #: 但它要跟着结论一起落库，三个月后才知道「当时字典长什么样」。
    cause_dict_digest: str = ""

    def as_audit_fields(self) -> dict[str, str]:
        """落 :mod:`reconq.audit.model_calls` 的两个版本列。"""
        return {
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
        }

    def label(self) -> str:
        """人读的一行（面板 / 回放报告 / 排障时贴给信息科）。"""
        return (
            f"{self.prompt_version}@{self.prompt_digest}"
            f" + {self.schema_version}@{self.schema_digest}"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "prompt_version": self.prompt_version,
            "prompt_digest": self.prompt_digest,
            "schema_version": self.schema_version,
            "schema_digest": self.schema_digest,
            "cause_dict_digest": self.cause_dict_digest,
        }

    @property
    def cache_tag(self) -> str:
        """进缓存键的标签。

        **只含提示词版本 + Schema 版本 + 两个指纹，不含字典**。
        字典进键的后果：补一条成因就把全部缓存作废，这一期的
        「模型成本与命中」报表会凭空多出几十次调用，而账单一模一样。
        """
        return f"{self.prompt_version}@{self.prompt_digest}/{self.schema_version}@{self.schema_digest}"


def current_pair() -> VersionPair:
    """当前生效的「提示词 × Schema」版本对。

    两侧的漏改检查都在这里做（提示词正文指纹、Schema 正文指纹），
    因此**它是一次自检入口**而不是一个纯读取函数。
    """
    _prompts.assert_prompt_registry_consistent()
    assert_schema_fingerprint_matches()
    from .causes import dictionary_digest

    template = _prompts.template_of()
    return VersionPair(
        prompt_version=template.version,
        prompt_digest=_prompts.prompt_digest(template),
        schema_version=SCHEMA_VERSION,
        schema_digest=SCHEMA_FINGERPRINT,
        cause_dict_digest=dictionary_digest(),
    )


def pair_of(
    prompt_version: Optional[str] = None,
    *,
    schema_version: Optional[str] = None,
) -> VersionPair:
    """取某一版提示词与（默认当前的）Schema 的版本对。

    回放对比旧版本时走这里 —— 提示词版本显式给，Schema 仍取当前：
    对比的变量**只有提示词一个**。把 Schema 也切成旧版去比，比出来的
    「提示词变好了」里混着 Schema 的贡献，正是这个模块要防的那类误判。
    """
    template = _prompts.template_of(prompt_version)
    from .causes import dictionary_digest

    if schema_version is not None and schema_version != SCHEMA_VERSION:
        raise ReconqError(
            "SCHEMA_VERSION_UNKNOWN",
            f"Schema 版本 {schema_version!r} 未登记；本产品当前只有 "
            f"{SCHEMA_VERSION!r}。回放对比只切提示词版本：同时切 Schema "
            "会让「哪个改动带来的提升」无法归因",
        )
    return VersionPair(
        prompt_version=template.version,
        prompt_digest=_prompts.prompt_digest(template),
        schema_version=SCHEMA_VERSION,
        schema_digest=SCHEMA_FINGERPRINT,
        cause_dict_digest=dictionary_digest(),
    )


def record_attribution_call(
    conn: Any,
    call: Any,
    *,
    period: str,
    request_fingerprint: str = "",
    prompt_version: Optional[str] = None,
    cache_hit: bool = False,
) -> Any:
    """把一次归因调用连同**版本对**记进 ``model_calls`` 审计链。

    存在的理由只有一个：让「提示词版本」与「Schema 版本」在**同一条记录
    上**。分成两处记（一次调用记提示词、另找一张表记 Schema）会出现
    配不上对的情况，而配不上对的版本对在排查时等于没有 —— 症状是
    「什么都没改，命中率却掉了」。

    审计表**不存提示词正文**：想知道「当时问了什么」，凭
    :func:`reconq.audit.model_calls.request_fingerprint` 去证据包快照取。
    这里只把版本串与两个指纹写进版本串里（见 :attr:`VersionPair.cache_tag`）。

    :param call: :class:`~reconq.gateway.model_gateway.GatewayCall`（鸭子类型，
        本模块不 import 网关包 —— 与 :mod:`reconq.audit.model_calls` 同一
        方向纪律：审计不该把「读一次审计」绑到「装了模型网关」上）。
    """
    from ..audit.model_calls import ModelCall, ModelCallLedger

    pair = pair_of(prompt_version)
    record = ModelCall.from_gateway_call(
        call,
        period=period,
        request_fingerprint=request_fingerprint,
        # 版本串写成 ``<提示词>@<提示词指纹>/<Schema>@<Schema 指纹>``：
        # 指纹进串意味着「改了模板正文却忘了 bump 版本号」这件事在审计链
        # 上看得见 —— 同一个版本号前后两条记录的版本串会不同。
        prompt_version=pair.cache_tag,
        schema_version=pair.schema_version,
        cache_hit=cache_hit,
    )
    return ModelCallLedger(conn).record(record)
