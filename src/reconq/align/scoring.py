"""弱匹配二级候选打分与分级（Task 23 · 核心模块的算法加深）。

二级候选是什么
--------------
Task 21 的对齐器在「单号没命中」时会按 **金额 + 账期时间窗** 找候选，但那一版
只给出一个布尔结论：找到了/没找到，找到就一律标 ``WEAK_MATCH_PENDING``。
财务科拿到台账看到的是「这条渠道款对不上，有 N 条候选」，**没法判断该先看
哪一条**——N 可能是 3，也可能是 300，而它们显然不是同等可疑。

本任务把「找到候选」细化成 **多因子加权打分 + 三档分级**：

============  ==========================================  ====================
档位          含义                                        处置
============  ==========================================  ====================
``strong``    金额相等 + 时间窗内 + 科室一致              优先人工确认
``weak``      部分因子满足（金额对但科室不符之类）        仍待人工确认
``unusable``  全部因子不满足（金额都超容差）              不作为候选展示
============  ==========================================  ====================

**三档都不自动认领**（本模块最重要的一条不变量）
------------------------------------------------
``strong`` 只是「最值得先看」，**不是**「就是它」。理由与 Task 21 一致：院内
一天里同金额缴费有几十笔，金额相等 + 时间接近 + 科室相同同时成立仍可能是两笔
不同的钱。把打分最高的那个自动认领，等于把不确定性藏进结论单，而单边账的
代价是财务科照着它去调账。因此本模块**只排序、不认领**——``Candidate`` 上
没有 ``claimed`` 字段，模块内没有任何一条代码路径会写进台账。

为什么不用「加权求和」一句话带过
------------------------------
四条因子各有各的失效方式，逐条说清：

1. **金额绝对差**（``amount``）
   ``Decimal`` 定点，容差默认 0.01 元。相等记 1.0，容差内非零记 0.5，超容差
   记 0.0。**中间不留连续衰减**：院内导出常把分位抹零（收 100 记 99.99），
   这属于「钱对得上」；而 87.99 对 88.00 差 0.01 与差 0.02 在业务上不是同一档
   ——后者多半是某一边漏了一笔费用项目。让分数在容差边缘连续衰减，会把这两
   种现场压成同一个「0.95 分」，台账上就看不出该去查抹零还是查漏项。

2. **时间窗距离**（``time``，单位分钟）
   距离 0 分钟记 1.0，随距离线性衰减到 ``time_window_minutes`` 处记 0.0。
   **任一侧没有交易时间即该因子不可用**（不是记 0）：缺时间戳说明「不知道」，
   记 0 等于凭空造出一条「时间对不上」的证据。

3. **科室一致**（``department``）与 **结算方式一致**（``settle_type``）
   两侧都非空且相同记 1.0，都非空但不同记 0.0，**任一侧为空即不可用**。
   这一条尤其重要：渠道账单导出里**根本没有科室列**（Task 21 的对齐器因此
   刻意不用科室收窄候选）。若把空值当成「不一致」，六路里凡是走文件通道的
   候选都会被扣分，分档就变成「谁导出得全谁分高」，与事实无关。

4. **跨渠道互相印证**（``cross_channel``）
   同一金额在**另一路渠道也出现、且那一路同样没有 HIS 对应**时，记 1.0。
   这是合并支付的典型现场：家属一次刷卡同时付了住院预交金和门诊挂号，
   渠道侧合成一笔或拆成两笔，HIS 侧对应着两条收费记录。给了 boost 并留痕
   （``hints``），但**boost 单独不足以把候选抬进 ``strong``** —— 见下。

不可用因子怎么算进总分
--------------------
分母只累加**可用**因子的权重。三个因子各占 0.15 时，其中两个不可用就按剩下
的三个重新归一，而不是「两个不可用各记 0 分」——那会让「数据不全」被当成
「数据矛盾」，而这恰恰是弱匹配最容易被误用的地方。

金额超容差是「否决项」而不是「低分项」
------------------------------------
上面第 1 条说金额容差内记 0.5、超容差记 0，但**超容差那一档不能只是 0 分**：
金额差 28 元的两行不是「不像同一笔」，是**根本不是同一笔**。若只把它记 0 分
参与加权，一个「金额 0 分 + 科室结算方式全对」的候选仍能拿 0.55 分落进弱候
选，台账上排在经办人该先看的队列里 —— 排序越靠前越像结论，越靠前错得越贵。
因此金额超容差直接**否决**：分数记 0、档位记 ``unusable``，不进默认展示。

强候选为什么还要额外两条硬判据
--------------------------
加权分 alone 不足以定 ``strong``：跨渠道印证这一项一旦拿到 0.10 的满分，
一个「金额差 0.01（在容差内算 0.5）+ 科室一致」的行就可能被推到 0.85。
而那恰恰不是「金额相等」。因此定 ``strong`` 额外要求：

1. **金额绝对相等**（容差内的 0.5 分不算）；
2. **时间在窗内**（时间因子不可用或为 0 都不算）；
3. **科室、结算方式不得有一项在权重 > 0 时取 0** —— 权重 0 的因子是「不参与」
   的意思，不构成否决（财务科把没有科室列的那几路权重调 0 是合法用法）。

跨渠道印证**不参与否决判定**：它是线索，「没有其它渠道印证」不说明这笔钱对不
上，只说明没人能从另一头佐证。

跨渠道印证的正确用法
------------------
它是一条**线索**，不是一条**证据**：告诉经办人「去看另一路渠道同一笔」，而直接
替他把 HIS 那条认领掉是不允许的。线索写进 ``Candidate.hints`` 并进事件
链，台账上与 ``strong`` 并列显示，两者不混为一谈。

配置来源与变更留痕
-----------------
权重、容差、时间窗、分档阈值全部来自 ``contracts/_scoring.yml``
（财务科可改），**不硬编码在代码分支里**。配置指纹（``fingerprint``）是规范化
后的 sha256；``record_config_change`` 在指纹变化时把「哪个因子从多少变成多少」
写进事件链——打分口径改了却没留痕，下周复核时没人说得清为什么上周那条是
强候选、这条不是。

源产品对照
----------
参考实现（``soda-core``）**没有这一层**：它的 check 在单个 dataset 内逐行比对
列值，命中就报 fail 行，**不排序、不打分、不解释「哪一行更可疑」**；
``contract verify`` 的输出是同质的证据行列表。本模块是围绕医院弱匹配场景
**新建**的，沿用的只是「结论必须能定位回源文件行号」这条主路径纪律。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence

import yaml

from ..errors import ReconqError
from ..runlog import log_event
from .matcher import AlignRecord

__all__ = [
    "FACTOR_NAMES",
    "FACTOR_ZH",
    "TIER_ZH",
    "ScoringConfig",
    "Factor",
    "Candidate",
    "ScoredRow",
    "load_scoring_config",
    "default_scoring_config",
    "rank_candidates",
    "expand_candidates",
    "factor_rows",
    "explain_candidate",
    "record_config_change",
    "config_fingerprint",
    "DEFAULT_SCORING_PATH",
]

logger = logging.getLogger("reconq.align.scoring")

#: 打分配置文件（财务科可改，权重与容差不硬编码在代码里）
DEFAULT_SCORING_PATH = os.path.join("contracts", "_scoring.yml")

#: 五个因子的规范名。配置里出现表以外的因子名一律报错，不静默忽略。
FACTOR_NAMES: tuple[str, ...] = (
    "amount",
    "time",
    "department",
    "settle_type",
    "cross_channel",
)

#: 因子 → 中文名 + 一句「它管什么」。台账与结论单里用同一套说法。
FACTOR_ZH: dict[str, str] = {
    "amount": "金额差",
    "time": "时间窗距离",
    "department": "科室一致",
    "settle_type": "结算方式一致",
    "cross_channel": "跨渠道印证",
}

#: 档位 → 中文。档位值即落库字符串。
TIER_ZH: dict[str, str] = {
    "strong": "强候选",
    "weak": "弱候选",
    "unusable": "不可用",
}


# ────────────────────────── 配置 ──────────────────────────


def _dec(value: Any, *, field_name: str, path: str) -> Decimal:
    """把配置值解析成 Decimal。

    **刻意不接受 float**：``0.45`` 在 YAML 里解析成二进制浮点，写进权重再参与
    加权求和会带上浮点残影，而门槛比较（``score >= strong_min``）在边界上会
    随机翻转。配置里写 ``"0.45"``（带引号）或整数都收，裸小数点则报出字段名
    与文件路径，让财务科知道该加引号。
    """
    if isinstance(value, bool):
        raise ReconqError(
            "SCORING_CONFIG_INVALID",
            f"打分配置 {path} 的 {field_name} 不能是布尔值（得到 {value!r}）",
            yaml_path=path,
        )
    if isinstance(value, float):
        raise ReconqError(
            "SCORING_CONFIG_INVALID",
            f"打分配置 {path} 的 {field_name}={value!r} 是浮点数，请加引号写成字符串"
            f"（如 \"{value}\"）：权重与阈值用浮点会在门槛比较时产生边界翻转",
            yaml_path=path,
        )
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ReconqError(
            "SCORING_CONFIG_INVALID",
            f"打分配置 {path} 的 {field_name}={value!r} 不是数字",
            yaml_path=path,
        ) from exc


@dataclass(frozen=True)
class ScoringConfig:
    """弱匹配打分的全部可调参数。

    冻结 dataclass：配置在一次扫描内不该被就地改掉。财务科要改参数就改
    ``contracts/_scoring.yml`` 重跑，改动会经 :func:`record_config_change`
    写进事件链。
    """

    amount_tolerance: Decimal = Decimal("0.01")
    time_window_minutes: int = 1440
    weights: Mapping[str, Decimal] = field(
        default_factory=lambda: {
            "amount": Decimal("0.45"),
            "time": Decimal("0.25"),
            "department": Decimal("0.15"),
            "settle_type": Decimal("0.15"),
            "cross_channel": Decimal("0.10"),
        }
    )
    strong_min: Decimal = Decimal("0.85")
    weak_min: Decimal = Decimal("0.40")
    max_candidates: int = 5
    source_path: str = "<内置默认>"

    # —— 校验与派生 ——

    def validate(self) -> "ScoringConfig":
        """非法配置显式报错，绝不静默回落默认。

        回落默认的后果很具体：财务科把科室权重调成 0（因为他们那几路导出
        确实没有科室列），程序静默忽略后仍按 0.15 算，所有候选凭空少 0.15 分，
        强候选全掉成弱候选 —— 而界面上看不出任何异常。
        """
        unknown = sorted(set(self.weights) - set(FACTOR_NAMES))
        if unknown:
            raise ReconqError(
                "SCORING_CONFIG_INVALID",
                f"打分配置 {self.source_path} 出现未知因子 {unknown}；"
                f"可用因子为 {list(FACTOR_NAMES)}，未登记的因子不会被静默忽略",
                yaml_path=self.source_path,
            )
        missing = sorted(set(FACTOR_NAMES) - set(self.weights))
        if missing:
            raise ReconqError(
                "SCORING_CONFIG_INVALID",
                f"打分配置 {self.source_path} 缺因子权重 {missing}；"
                f"本产品不替财务科猜权重，请逐个补齐（值可写 0 表示该因子不参与）",
                yaml_path=self.source_path,
            )
        for name, weight in self.weights.items():
            if weight < 0:
                raise ReconqError(
                    "SCORING_CONFIG_INVALID",
                    f"打分配置 {self.source_path} 的因子 {name} 权重 {weight} 为负；"
                    f"权重必须 ≥ 0（写 0 表示该因子不参与加权）",
                    yaml_path=self.source_path,
                )
        if sum(self.weights.values()) <= 0:
            raise ReconqError(
                "SCORING_CONFIG_INVALID",
                f"打分配置 {self.source_path} 的权重全为 0，分数会恒为 0、"
                f"所有候选都落同一档",
                yaml_path=self.source_path,
            )
        if self.amount_tolerance < 0:
            raise ReconqError(
                "SCORING_CONFIG_INVALID",
                f"打分配置 {self.source_path} 的 amount 容差 {self.amount_tolerance} 为负",
                yaml_path=self.source_path,
            )
        if self.time_window_minutes <= 0:
            raise ReconqError(
                "SCORING_CONFIG_INVALID",
                f"打分配置 {self.source_path} 的 time_window_minutes="
                f"{self.time_window_minutes} 必须为正整数",
                yaml_path=self.source_path,
            )
        if not (Decimal("0") <= self.weak_min <= self.strong_min <= Decimal("1")):
            raise ReconqError(
                "SCORING_CONFIG_INVALID",
                f"打分配置 {self.source_path} 的分档门槛非法：需满足 "
                f"0 ≤ weak_min({self.weak_min}) ≤ strong_min({self.strong_min}) ≤ 1",
                yaml_path=self.source_path,
            )
        if self.max_candidates < 1:
            raise ReconqError(
                "SCORING_CONFIG_INVALID",
                f"打分配置 {self.source_path} 的 max_candidates={self.max_candidates} "
                f"必须 ≥ 1（截断到 0 条等于不产出候选）",
                yaml_path=self.source_path,
            )
        return self

    def weight_sum(self) -> Decimal:
        return sum(self.weights.values())

    def fingerprint(self) -> str:
        return config_fingerprint(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount_tolerance": str(self.amount_tolerance),
            "time_window_minutes": self.time_window_minutes,
            "weights": {k: str(v) for k, v in sorted(self.weights.items())},
            "strong_min": str(self.strong_min),
            "weak_min": str(self.weak_min),
            "max_candidates": self.max_candidates,
            "source_path": self.source_path,
            "fingerprint": self.fingerprint(),
        }


def config_fingerprint(config: ScoringConfig) -> str:
    """配置的规范化 sha256。

    只覆盖**会改变结论**的字段，**不含** ``source_path``：同一份配置放在
    不同机器的不同目录下指纹必须相同，否则「口径变没变」这个问题会被路径
    差异污染。
    """
    payload = {
        "amount_tolerance": str(config.amount_tolerance),
        "time_window_minutes": config.time_window_minutes,
        "weights": {k: str(v) for k, v in sorted(config.weights.items())},
        "strong_min": str(config.strong_min),
        "weak_min": str(config.weak_min),
        "max_candidates": config.max_candidates,
    }
    blob = yaml.safe_dump(payload, sort_keys=True, allow_unicode=True, default_flow_style=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def default_scoring_config() -> ScoringConfig:
    """内置默认配置（与 ``contracts/_scoring.yml`` 的初始值一致）。

    两处保持一致靠专项测试钉住：配置模板改了默认值而这里没改，财务科不
    部署配置文件时跑出来的分档会与文档对不上。
    """
    return ScoringConfig().validate()


def load_scoring_config(path: Optional[str] = None) -> ScoringConfig:
    """读打分配置。

    文件不存在时**回落内置默认**（不是报错）：打分是加深能力，不是主路径
    的必需件，缺个配置文件不该让整次扫描挂掉。文件存在但写错则显式报错——
    写错与没写是两种完全不同的情况，后者是「用默认」，前者必须当场修。
    """
    target = path or DEFAULT_SCORING_PATH
    if not os.path.isfile(target):
        logger.info("未找到打分配置 %s，回落内置默认参数", target)
        return default_scoring_config()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ReconqError(
            "SCORING_CONFIG_INVALID",
            f"打分配置 {target} 读取或解析失败：{exc}",
            yaml_path=target,
        ) from exc
    if raw is None:
        return default_scoring_config()
    if not isinstance(raw, dict):
        raise ReconqError(
            "SCORING_CONFIG_INVALID",
            f"打分配置 {target} 顶层必须是键值映射，得到 {type(raw).__name__}",
            yaml_path=target,
        )

    tolerance = raw.get("tolerance") or {}
    weights_raw = raw.get("weights")
    tiers = raw.get("tiers") or {}
    candidates = raw.get("candidates") or {}
    if not isinstance(tolerance, dict) or not isinstance(tiers, dict) or not isinstance(candidates, dict):
        raise ReconqError(
            "SCORING_CONFIG_INVALID",
            f"打分配置 {target} 的 tolerance / tiers / candidates 三段都必须是映射",
            yaml_path=target,
        )
    if not isinstance(weights_raw, dict):
        raise ReconqError(
            "SCORING_CONFIG_INVALID",
            f"打分配置 {target} 缺 weights 段或类型不是映射（{type(weights_raw).__name__}）",
            yaml_path=target,
        )

    window = tolerance.get("time_window_minutes", 1440)
    try:
        window_int = int(str(window).strip())
    except (TypeError, ValueError) as exc:
        raise ReconqError(
            "SCORING_CONFIG_INVALID",
            f"打分配置 {target} 的 tolerance.time_window_minutes={window!r} 不是整数",
            yaml_path=target,
        ) from exc

    max_candidates = candidates.get("max_per_row", 5)
    try:
        max_candidates_int = int(str(max_candidates).strip())
    except (TypeError, ValueError) as exc:
        raise ReconqError(
            "SCORING_CONFIG_INVALID",
            f"打分配置 {target} 的 candidates.max_per_row={max_candidates!r} 不是整数",
            yaml_path=target,
        ) from exc

    config = ScoringConfig(
        amount_tolerance=_dec(
            tolerance.get("amount", "0.01"), field_name="tolerance.amount", path=target
        ),
        time_window_minutes=window_int,
        weights={
            name: _dec(value, field_name=f"weights.{name}", path=target)
            for name, value in weights_raw.items()
        },
        strong_min=_dec(tiers.get("strong_min", "0.85"), field_name="tiers.strong_min", path=target),
        weak_min=_dec(tiers.get("weak_min", "0.40"), field_name="tiers.weak_min", path=target),
        max_candidates=max_candidates_int,
        source_path=target,
    )
    return config.validate()


def record_config_change(
    config: ScoringConfig,
    previous_fingerprint: Optional[str] = None,
    *,
    channel: str = "",
    period: str = "",
) -> Optional[dict[str, Any]]:
    """权重变更写进事件链；返回变化明细（无变化时返回 ``None``）。

    「改了什么」要能当场答得出：光记一个新指纹，下周复核时没人说得清
    上周那条为什么是强候选、这周同样的数据成了弱候选。变化明细逐因子列出
    旧值→新值，面板与结论单都能直接引。
    """
    current = config.fingerprint()
    if previous_fingerprint and previous_fingerprint == current:
        return None
    payload = config.to_dict()
    log_event(
        "align.scoring.config",
        channel=channel,
        period=period,
        code="",
        source_path=config.source_path,
        fingerprint=current,
        previous_fingerprint=previous_fingerprint or "",
        # config 落成**可读文本**而不是嵌套 dict：事件链是给人翻的
        # （「上周为什么是强候选」要当场答得出），dict 在 JSONL 里还得再解一层。
        # 机器侧要精确值时用 config_json。
        config="\n".join(
            [f"fingerprint={current}"]
            + ( [f"previous_fingerprint={previous_fingerprint}"] if previous_fingerprint else [] )
            + [f"{k}={v}" for k, v in payload["weights"].items()]
            + [
                f"amount_tolerance={payload['amount_tolerance']}",
                f"time_window_minutes={payload['time_window_minutes']}",
                f"strong_min={payload['strong_min']}",
                f"weak_min={payload['weak_min']}",
                f"max_candidates={payload['max_candidates']}",
                f"source_path={payload['source_path']}",
            ]
        ),
        config_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
    return {
        "fingerprint": current,
        "previous_fingerprint": previous_fingerprint or "",
        "source_path": config.source_path,
    }


# ────────────────────────── 打分 ──────────────────────────


def _minutes_between(left: Optional[datetime], right: Optional[datetime]) -> Optional[int]:
    """两个时间相差多少分钟（取绝对值，四舍五入到整分钟）。

    四舍五入而不是截断：导出文件的时间常常只到分钟，截断会让「10:00:59 与
    10:00:00 相差 0 分钟」，在打分上与真的同一时刻完全等价，而事实并非如此。
    """
    if left is None or right is None:
        return None
    seconds = abs((left - right).total_seconds())
    return int(round(seconds / 60.0))


def _norm(value: str) -> str:
    """归一化用于「一致」比较的文本。

    只做 strip + NFKC + casefold，与 ``align.index.normalize_key`` 同一口径：
    全角半角、大小写、首尾空白不该让「同一科室」判成不一致。两处判据若漂移，
    台账上会出现「科室明明一样却被扣分」且无处可查。
    """
    import unicodedata

    return unicodedata.normalize("NFKC", (value or "").strip()).casefold()


@dataclass(frozen=True)
class Factor:
    """单个因子的取值与贡献。

    ``available=False`` 表示**这一项没法判**（缺字段），它既不加分也不扣分，
    并且不进入分母。区分「不可用」与「取 0 分」是本模块最容易做错的地方：
    「数据不全」被当成「数据矛盾」，会让文件通道的候选（普遍无科室列）
    全部掉档。
    """

    name: str
    available: bool
    value: Optional[Decimal] = None
    weight: Decimal = Decimal("0")
    detail: str = ""

    @property
    def contribution(self) -> Decimal:
        if not self.available or self.value is None:
            return Decimal("0")
        return self.weight * self.value

    @property
    def zh(self) -> str:
        return FACTOR_ZH.get(self.name, self.name)

    def render(self) -> str:
        if not self.available:
            return f"{self.zh}：不可用（{self.detail or '数据不全'}，不计入分数）"
        assert self.value is not None
        return (
            f"{self.zh}：{self.value} × 权重 {self.weight} = {self.contribution}"
            + (f"（{self.detail}）" if self.detail else "")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "name_zh": self.zh,
            "available": self.available,
            "value": str(self.value) if self.value is not None else None,
            "weight": str(self.weight),
            "contribution": str(self.contribution),
            "detail": self.detail,
        }


def _amount_factor(channel: AlignRecord, his: AlignRecord, config: ScoringConfig) -> Factor:
    diff = abs(channel.net_amount - his.net_amount)
    weight = config.weights["amount"]
    if diff == 0:
        return Factor("amount", True, Decimal("1"), weight, "金额完全相等")
    if diff <= config.amount_tolerance:
        return Factor(
            "amount", True, Decimal("0.5"), weight,
            f"差 {diff} 元（容差 {config.amount_tolerance} 元内，多为分位抹零）",
        )
    return Factor("amount", True, Decimal("0"), weight, f"差 {diff} 元，超容差 {config.amount_tolerance} 元")


def _time_factor(channel: AlignRecord, his: AlignRecord, config: ScoringConfig) -> Factor:
    minutes = _minutes_between(channel.trade_time, his.trade_time)
    weight = config.weights["time"]
    if minutes is None:
        missing = "渠道行" if channel.trade_time is None else "HIS 记录"
        return Factor("time", False, None, weight, f"{missing}无交易时间")
    window = config.time_window_minutes
    if minutes == 0:
        return Factor("time", True, Decimal("1"), weight, "交易时间同分钟")
    if minutes >= window:
        return Factor("time", True, Decimal("0"), weight, f"相差 {minutes} 分钟，超出时间窗 {window} 分钟")
    # 线性衰减：窗口边缘记 0，窗口一半处记 0.5
    ratio = Decimal(window - minutes) / Decimal(window)
    return Factor("time", True, ratio, weight, f"相差 {minutes} 分钟（窗口 {window} 分钟）")


def _bool_factor(
    name: str, channel_value: str, his_value: str, config: ScoringConfig
) -> Factor:
    weight = config.weights[name]
    left = _norm(channel_value)
    right = _norm(his_value)
    if not left or not right:
        empty_side = "渠道行" if not left else "HIS 记录"
        return Factor(
            name, False, None, weight,
            f"{empty_side}无{FACTOR_ZH.get(name, name)}字段（导出侧本就没有这一列）",
        )
    if left == right:
        return Factor(name, True, Decimal("1"), weight, "两侧一致")
    return Factor(name, True, Decimal("0"), weight, f"两侧不一致（{channel_value or '空'} vs {his_value or '空'}）")


def _cross_channel_factor(
    channel: str, corroborations: Optional[Iterable[str]], config: ScoringConfig
) -> Factor:
    weight = config.weights["cross_channel"]
    if corroborations is None:
        return Factor(
            "cross_channel", False, None, weight,
            "未提供跨渠道印证数据（本轮只跑单路，无法判断）",
        )
    peers = sorted({c for c in corroborations if c and c != channel})
    if peers:
        return Factor(
            "cross_channel", True, Decimal("1"), weight,
            f"同金额在渠道 {'、'.join(peers)} 也出现且同样无 HIS 对应，"
            f"疑合并支付（仅为线索，不作证据）",
        )
    return Factor("cross_channel", True, Decimal("0"), weight, "无其它渠道同金额待确认记录")


@dataclass(frozen=True)
class Candidate:
    """一条弱匹配候选（渠道行 × 一条 HIS 记录）。

    **本类没有 ``claimed`` 字段，也没有写回通道**：三档分级只决定「先看
    哪一条」，不决定「这条就是它」。见模块头的不变量说明。
    """

    channel_row: AlignRecord
    his_row: AlignRecord
    factors: tuple[Factor, ...]
    score: Decimal
    tier: str
    hints: tuple[str, ...] = ()

    @property
    def tier_zh(self) -> str:
        return TIER_ZH.get(self.tier, self.tier)

    @property
    def his_ref(self) -> str:
        return self.his_row.ref

    @property
    def channel_ref(self) -> str:
        return self.channel_row.ref

    @property
    def available_factors(self) -> tuple[Factor, ...]:
        return tuple(f for f in self.factors if f.available)

    @property
    def unusable_factors(self) -> tuple[Factor, ...]:
        return tuple(f for f in self.factors if not f.available)

    def is_strong(self) -> bool:
        return self.tier == "strong"

    def render(self) -> str:
        head = (
            f"{self.tier_zh}（{self.score}）：{self.channel_ref} {self.channel_row.net_amount} 元"
            f" ← {self.his_ref} {self.his_row.net_amount} 元"
        )
        lines = [head]
        lines.extend(f"  · {f.render()}" for f in self.factors)
        if self.unusable_factors:
            names = "、".join(f.zh for f in self.unusable_factors)
            lines.append(f"  · 分母只累加可用因子权重（{names} 不可用，已剔除）")
        lines.extend(f"  · 线索：{h}" for h in self.hints)
        lines.append("  · 本档位只影响「先看哪一条」，不构成认领，需人工确认")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "tier_zh": self.tier_zh,
            "score": str(self.score),
            "channel_ref": self.channel_ref,
            "his_ref": self.his_ref,
            "channel_row": self.channel_row.to_dict(),
            "his_row": self.his_row.to_dict(),
            "factors": [f.to_dict() for f in self.factors],
            "available_factor_count": len(self.available_factors),
            "hints": list(self.hints),
        }

    def to_candidate(self):
        """转成 :class:`align.ambiguity.AmbiguityCandidate`，供对齐器复用。

        复用而不是另造一个类型：面板、台账、结论单三处渲染候选行时走的是
        同一个 ``render()``，两套类型会出现「同一笔候选在面板上一种说法、
        在结论单上另一种说法」。
        """
        from .ambiguity import AmbiguityCandidate

        return AmbiguityCandidate(
            row_no=self.his_row.source_row_no,
            source_file=self.his_row.source_file,
            amount=str(self.his_row.net_amount),
            trade_time=self.his_row.trade_time.isoformat() if self.his_row.trade_time else "",
            department=self.his_row.department,
            settle_type=self.his_row.settle_type,
            record_id=self.his_row.his_order_no or self.his_row.order_no,
            is_refund=self.his_row.is_refund,
            cross_period=False,
            period=self.his_row.period,
        )


def _quantize(value: Decimal) -> Decimal:
    """分数保留 4 位小数。

    不保留更多位：四个因子的加权和有 8 位小数，比 0.85 高出 1e-9 也会被
    打成强候选，台账上却看不出差别。保留 4 位足够区分，也便于在事件链里
    按字面比对。
    """
    return value.quantize(Decimal("0.0001"))


def _score_factors(factors: Sequence[Factor]) -> Decimal:
    """按**可用**因子加权平均。"""
    usable = [f for f in factors if f.available and f.value is not None]
    if not usable:
        return Decimal("0")
    denominator = sum(f.weight for f in usable)
    if denominator <= 0:
        return Decimal("0")
    return _quantize(sum(f.contribution for f in usable) / denominator)


def _tier(factors: Sequence[Factor], score: Decimal, config: ScoringConfig) -> str:
    """定档。

    三条硬判据（加权分之外）：

    1. **金额超容差 → 直接否决**。金额差 28 元的两行不是「不像同一笔」，是
       根本不是同一笔；让它带着 0.55 分落进弱候选，只会让它排在该先看的队列
       前面。见模块头。
    2. **金额绝对相等**（容差内的 0.5 分不算相等）——否则一个分位抹零的
       候选会被跨渠道印证推上 ``strong``。
    3. **科室 / 结算方式在权重 > 0 时不得取 0**：权重 0 表示该路不参与加权
       （财务科的合法用法），不构成否决。

    跨渠道印证**不参与否决**：它只是线索，「没有其它渠道印证」不说明这笔钱
    对不上。
    """
    by_name = {f.name: f for f in factors}
    amount = by_name.get("amount")
    time = by_name.get("time")
    if amount is not None and amount.available and amount.value == 0:
        return "unusable"          # 金额超容差：否决项，不是低分项
    amount_exact = bool(amount and amount.available and amount.value == 1)
    time_ok = bool(time and time.available and time.value and time.value > 0)
    vetoed = any(
        f.available and f.weight > 0 and f.value == 0
        for f in factors
        if f.name in ("department", "settle_type")
    )
    if score >= config.strong_min and amount_exact and time_ok and not vetoed:
        return "strong"
    if score >= config.weak_min:
        return "weak"
    return "unusable"


def score_pair(
    channel_row: AlignRecord,
    his_row: AlignRecord,
    *,
    config: Optional[ScoringConfig] = None,
    corroborations: Optional[Iterable[str]] = None,
) -> Candidate:
    """给一组（渠道行, HIS 记录）打分并定档。"""
    cfg = config or default_scoring_config()
    factors = (
        _amount_factor(channel_row, his_row, cfg),
        _time_factor(channel_row, his_row, cfg),
        _bool_factor("department", channel_row.department, his_row.department, cfg),
        _bool_factor("settle_type", channel_row.settle_type, his_row.settle_type, cfg),
        _cross_channel_factor(channel_row.channel, corroborations, cfg),
    )
    score = _score_factors(factors)
    if factors[0].available and factors[0].value == 0:
        # 金额超容差：分数一并归零，让它在排序里沉到底部。
        # 只把档位判成 unusable 而分数照留（0.55）会让这条记录排在经办人该先
        # 看的队列前面 —— 排得越前越像结论，越前错得越贵。
        score = Decimal("0")
    tier = _tier(factors, score, cfg)
    hints: list[str] = []
    cross = factors[4]
    if cross.available and cross.value == 1:
        hints.append("同一金额在另一路渠道也出现且无 HIS 对应，先去查是否为合并支付")
    return Candidate(
        channel_row=channel_row,
        his_row=his_row,
        factors=factors,
        score=score,
        tier=tier,
        hints=tuple(hints),
    )


@dataclass
class ScoredRow:
    """一条渠道行的二级候选打分结果（含截断标记）。"""

    channel_row: AlignRecord
    #: 展示用的候选（已按分数降序截断到 ``max_candidates``）
    candidates: list[Candidate] = field(default_factory=list)
    #: 打分时的全部候选数（截断前的真实规模）
    candidate_total: int = 0
    truncated: bool = False
    config_fingerprint: str = ""

    @property
    def strong(self) -> list[Candidate]:
        return [c for c in self.candidates if c.tier == "strong"]

    @property
    def weak(self) -> list[Candidate]:
        return [c for c in self.candidates if c.tier == "weak"]

    @property
    def unusable(self) -> list[Candidate]:
        return [c for c in self.candidates if c.tier == "unusable"]

    @property
    def displayable(self) -> list[Candidate]:
        """值得给人看的候选：强 + 弱。``unusable`` 单独可查但不进默认展示。"""
        return [c for c in self.candidates if c.tier in ("strong", "weak")]

    def render(self) -> str:
        head = (
            f"渠道行 {self.channel_row.ref}（{self.channel_row.net_amount} 元）"
            f"共 {self.candidate_total} 条候选，展示 {len(self.candidates)} 条"
            + (f"（已截断，另有 {self.candidate_total - len(self.candidates)} 条可用 "
                   f"reconq align expand 查看）" if self.truncated else "")
        )
        lines = [head]
        for candidate in self.candidates:
            lines.append(candidate.render())
        if not self.candidates:
            lines.append("  （无可用候选）")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_ref": self.channel_row.ref,
            "channel_row": self.channel_row.to_dict(),
            "candidate_total": self.candidate_total,
            "shown": len(self.candidates),
            "truncated": self.truncated,
            "tier_counts": {
                "strong": len(self.strong),
                "weak": len(self.weak),
                "unusable": len(self.unusable),
            },
            "candidates": [c.to_dict() for c in self.candidates],
            "config_fingerprint": self.config_fingerprint,
        }


def rank_candidates(
    channel_row: AlignRecord,
    his_rows: Sequence[AlignRecord],
    *,
    config: Optional[ScoringConfig] = None,
    corroborations: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> ScoredRow:
    """给一条渠道行的全部候选打分、按分数降序排序并截断。

    排序**稳定**：同分的候选保持 HIS 侧的原有顺序。HIS 流水按收费时间导出，
    同分候选里时间更早的那笔通常更可能是本笔的上游，但这条经验不该写进
    排序键 —— 一旦写进去，同分候选的先后就变成了一个没人能解释的隐藏判据。
    """
    cfg = config or default_scoring_config()
    cap = cfg.max_candidates if limit is None else limit
    scored = [score_pair(channel_row, his, config=cfg, corroborations=corroborations) for his in his_rows]
    scored.sort(key=lambda c: c.score, reverse=True)
    total = len(scored)
    shown = scored[:cap] if cap and cap > 0 else scored
    return ScoredRow(
        channel_row=channel_row,
        candidates=shown,
        candidate_total=total,
        truncated=total > len(shown),
        config_fingerprint=cfg.fingerprint(),
    )


def expand_candidates(
    channel_row: AlignRecord,
    his_rows: Sequence[AlignRecord],
    *,
    config: Optional[ScoringConfig] = None,
    corroborations: Optional[Iterable[str]] = None,
) -> list[Candidate]:
    """返回**全部**候选，不截断。

    截断到 5 条只是台账与面板的展示上限，不是判定上限：被截掉的第 6 条里
    完全可能有强候选。把它做成单独一条腿（``reconq align expand`` 的数据源），
    而不是把 limit 设成无穷大 —— 后者会让「100 条候选」直接糊在面板上。
    """
    cfg = config or default_scoring_config()
    scored = [score_pair(channel_row, his, config=cfg, corroborations=corroborations) for his in his_rows]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def factor_rows(config: Optional[ScoringConfig] = None) -> list[dict[str, Any]]:
    """因子说明表（权重来自配置，不硬编码）。README 与台账共用。"""
    cfg = config or default_scoring_config()
    total = cfg.weight_sum()
    rows = []
    for name in FACTOR_NAMES:
        weight = cfg.weights[name]
        rows.append(
            {
                "name": name,
                "name_zh": FACTOR_ZH[name],
                "weight": str(weight),
                "share": str((weight / total).quantize(Decimal("0.0001"))) if total > 0 else "0",
                "available": "两侧都有值才计入",
            }
        )
    return rows


def explain_candidate(candidate: Candidate) -> str:
    """给经办人看的一段中文解释（分母怎么来的、为什么落这一档）。"""
    usable = candidate.available_factors
    skipped = [f.zh for f in candidate.unusable_factors]
    lines = [
        f"{candidate.channel_ref} ↔ {candidate.his_ref}：{candidate.score} 分，落「{candidate.tier_zh}」",
        f"分母按 {len(usable)} 个可用因子归一：{'、'.join(f.zh for f in usable) or '无'}",
    ]
    if skipped:
        lines.append(f"已剔除不可用因子：{'、'.join(skipped)}（缺字段不等于对不上）")
    lines.append("本档位只决定「先看哪一条」，不构成认领；是否为同一笔由经办人确认、主管复核")
    return "\n".join(lines)
