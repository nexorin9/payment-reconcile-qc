"""六路渠道消费者注册表与逐渠道失败剔除（Task 6）。

源产品对应实现
--------------
``github_ref/soda-core/soda-core/src/soda_core/common/streaming/
streaming_orchestrator.py:27-65``：

* ``_consumers`` 是**类级可变状态**——消费者在插件加载期注册，之后活到
  进程结束；``_rebuild_action_map()`` 按 ``supported_actions()`` 建一张
  action → consumers 的映射；
* ``initialize_action(action, context)`` 逐个 consumer 调
  ``consumer.initialize()``，**初始化失败的 consumer 被剔除出本次 handle**
  （不进 ``active_consumers``），并 ``logger.warning(...)`` 记一笔，
  **循环继续**；返回值里只带成功的那些；
* ``reset()`` 清空注册表，给测试隔离用。

本轮怎么改写
------------
「consumer」在这里是**一路渠道账单源**。语义照搬、改名不改义：

* 某渠道当天拉不到账单（``BILL_NOT_EXIST``）→ **只剔除这一路**，
  其余五路照常出结果；
* 失败是**可见的**：记 ``(channel, error_code)`` 进 ``ChannelFailure``，
  面板与结论单能显示「微信没取到 / 支付宝 BILL_NOT_EXIST」，
  而不是悄悄少一路让人以为「全渠道已核」。

源实现在剔除时只有一行 ``logger.warning``，失败原因就丢在 exc_info 里。
本产品把失败**收集**起来，是因为对账场景里「哪一路没取到」本身就是
结论单上的一行——财务科要据此决定本期能不能结账。

与源实现的另一处差别：源实现的 consumer 初始化**失败即剔除**，
本产品只剔除**渠道级**失败（``error_meta(code).channel_level``）；
凭据配错这类**全局配置问题**应当让整次扫描停下并报错，而不是
「六路全剔除、跑出一份空结论单」——那比报错更危险。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from ..engine.checks import BillRow
from ..errors import ReconqError, error_meta

__all__ = [
    "CHANNELS",
    "ChannelSource",
    "ChannelFetch",
    "ChannelFailure",
    "InitOutcome",
    "SourceRegistry",
    "InboxChannelSource",
    "HisChannelSource",
    "register",
    "registry",
    "get_source",
    "initialize_all",
    "probe_channel",
]

logger = logging.getLogger("reconq.registry")

#: 六路渠道。顺序即面板展示顺序：微信 / 支付宝在最前（量最大），
#: 院内两路在中间，HIS 与医保在最后（它们是另外五路的对照基准）。
CHANNELS: tuple[str, ...] = (
    "wechat",
    "alipay",
    "unionpay",
    "self_service",
    "his",
    "medical_ins",
)


@runtime_checkable
class ChannelSource(Protocol):
    """一路渠道账单的取数源。

    对应源实现的 ``StreamingConsumer``：实现类要能回答「支持哪些 action」
    与「怎么把自己初始化起来」。本产品只需要一个账期取数动作，所以
    协议面比源实现窄。
    """

    channel: str

    def supported_actions(self) -> tuple[str, ...]:
        """本源支持的动作。本产品目前只有 ``fetch_bill``。"""
        ...

    def initialize(self, action: str, context: dict[str, Any]) -> None:
        """初始化。失败抛 ``ReconqError``，由 :func:`initialize_all` 剔除。"""
        ...

    def fetch(self, period: str) -> ChannelFetch:
        """取一个账期的账单行。"""
        ...


@dataclass
class ChannelFetch:
    """一路渠道一个账期的取数结果。"""

    channel: str
    period: str
    rows: list[BillRow] = field(default_factory=list)
    total_amount: str = "0"
    #: 该路是否本期压根没账单（区别于「有账单但口径不通过」）
    not_fetched: bool = False
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelFailure:
    """一路渠道初始化/取数失败的记录。

    刻意 ``frozen``：失败明细一旦生成就不许被后续代码改写，否则
    「本期哪些路没取到」这个结论会随调用顺序漂移。
    """

    channel: str
    error_code: str
    detail: str
    #: 是否渠道级（只影响本路）还是全局级（应让整次扫描停下）
    channel_level: bool = True
    cause_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "error_code": self.error_code,
            "cause_code": self.cause_code or self.error_code,
            "detail": self.detail,
            "channel_level": self.channel_level,
        }


@dataclass
class InitOutcome:
    """:func:`initialize_all` 的返回：成功的路 + 失败的明细。"""

    period: str
    initialized: list[str] = field(default_factory=list)
    failures: list[ChannelFailure] = field(default_factory=list)

    @property
    def ok_channels(self) -> set[str]:
        return set(self.initialized)

    @property
    def failed_channels(self) -> set[str]:
        return {f.channel for f in self.failures}

    def error_code_of(self, channel: str) -> Optional[str]:
        for f in self.failures:
            if f.channel == channel:
                return f.error_code
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "initialized": list(self.initialized),
            "failures": [f.to_dict() for f in self.failures],
        }


class InboxChannelSource:
    """文件通道源（微信/支付宝/银联/自助机，院内导出到 ``inbox/``）。

    微信与支付宝在 Task 5 已有真实协议客户端（商户 API）。本类的
    ``fetch`` 走文件通道，是**本轮形态**：财务科把渠道账单与院内导出
    放到 ``inbox/<period>/<channel>/`` 即可跑通整条主路径，不必先打通
    商户号。最终对接方式是渠道正式接口（见 ``spec.md`` 对接层）。
    """

    def __init__(self, channel: str, *, inbox_root: str = "inbox", table: Any = None) -> None:
        self.channel = channel
        self.inbox_root = inbox_root
        self._table = table

    def supported_actions(self) -> tuple[str, ...]:
        return ("fetch_bill",)

    def initialize(self, action: str, context: dict[str, Any]) -> None:
        if action != "fetch_bill":
            raise ReconqError(
                "CHANNEL_UNKNOWN",
                f"渠道 {self.channel} 不支持动作 {action!r}",
                channel=self.channel,
            )
        context.setdefault("inbox_root", self.inbox_root)

    def fetch(self, period: str) -> ChannelFetch:
        from .inbox_files import read_inbox_dir

        table = self._table
        if table is None:
            from ..contracts.loader import load_contract

            table = load_contract(f"contracts/{self.channel}.yml", {"period": period}).table()
        rows = read_inbox_dir(self.inbox_root, period, self.channel, table)
        return ChannelFetch(
            channel=self.channel,
            period=period,
            rows=rows,
            not_fetched=not rows,
            note="" if rows else f"inbox/{period}/{self.channel}/ 下没有账单文件",
        )


class HisChannelSource:
    """HIS 只读库源。走 :mod:`reconq.sources.his_db` 的流式取数。"""

    def __init__(self, channel: str = "his", *, table: str = "charge_record") -> None:
        self.channel = channel
        self.table = table

    def supported_actions(self) -> tuple[str, ...]:
        return ("fetch_bill",)

    def initialize(self, action: str, context: dict[str, Any]) -> None:
        if action != "fetch_bill":
            raise ReconqError(
                "CHANNEL_UNKNOWN",
                f"渠道 {self.channel} 不支持动作 {action!r}",
                channel=self.channel,
            )
        # 凭据位检查放在 initialize：源实现是在 consumer.initialize 里做
        # 同样的事（连接参数、密钥可读性），失败即被剔除。
        from .properties import build_properties

        build_properties(self.channel)

    def fetch(self, period: str) -> ChannelFetch:
        from .his_db import fetch_his_charge_rows

        result = fetch_his_charge_rows(period, table=self.table)
        return ChannelFetch(
            channel=self.channel,
            period=period,
            rows=result.rows,
            total_amount=result.total_amount,
            not_fetched=result.row_count == 0,
            note="" if result.row_count else f"账期 {period} 没有查到收费流水",
            extra=result.to_dict(),
        )


class SourceRegistry:
    """渠道源注册表。

    结构与源实现的 ``StreamingOrchestrator`` 同形：``_consumers`` 进程级
    持有、``_rebuild_action_map()`` 按 action 建映射、``reset()`` 给测试
    隔离。差别是本注册表按**渠道名**索引（对账是一次一渠道的事，不需要
    一个 action 挂多个 consumer 的扇出）。
    """

    def __init__(self) -> None:
        self._sources: dict[str, ChannelSource] = {}
        self._action_map: dict[str, list[ChannelSource]] = {}

    # —— 注册（对应源实现 register_consumer / _rebuild_action_map）——
    def register(self, source: ChannelSource) -> None:
        self._sources[source.channel] = source
        self._rebuild_action_map()

    def _rebuild_action_map(self) -> None:
        action_map: dict[str, list[ChannelSource]] = {}
        for source in self._sources.values():
            for action in source.supported_actions():
                action_map.setdefault(action, []).append(source)
        self._action_map = action_map

    def reset(self) -> None:
        self._sources.clear()
        self._action_map.clear()

    # —— 查询 ——
    def get(self, channel: str) -> Optional[ChannelSource]:
        return self._sources.get(channel)

    def channels(self) -> list[str]:
        """按 CHANNELS 的展示顺序返回已注册渠道。"""
        return [c for c in CHANNELS if c in self._sources] + [
            c for c in sorted(self._sources) if c not in CHANNELS
        ]

    def consumers_for(self, action: str) -> list[ChannelSource]:
        return list(self._action_map.get(action, []))

    def __contains__(self, channel: object) -> bool:
        return channel in self._sources

    def __len__(self) -> int:
        return len(self._sources)


#: 进程级注册表
registry = SourceRegistry()


def _default_registration(inbox_root: str = "inbox") -> None:
    """按 ``CHANNELS`` 登记六路。重复调用是幂等的。"""
    for channel in CHANNELS:
        if channel in registry:
            continue
        if channel == "his":
            registry.register(HisChannelSource(channel))
        else:
            registry.register(InboxChannelSource(channel, inbox_root=inbox_root))


def register(source: ChannelSource) -> None:
    registry.register(source)


def get_source(channel: str) -> ChannelSource:
    """取一路渠道源；未注册则先跑默认登记，仍没有则报未知渠道。"""
    if channel not in registry:
        _default_registration()
    source = registry.get(channel)
    if source is None:
        raise ReconqError(
            "CHANNEL_UNKNOWN",
            f"未知渠道 {channel!r}，已登记：{'、'.join(registry.channels()) or '（空）'}",
            channel=channel,
        )
    return source


def initialize_all(
    period: str,
    *,
    action: str = "fetch_bill",
    channels: Optional[list[str]] = None,
    inbox_root: str = "inbox",
) -> InitOutcome:
    """逐渠道初始化，**失败的只剔除自己**，其余照常。

    对应源实现 ``initialize_action()`` 的逐 consumer ``try/except`` +
    ``logger.warning`` + 「只把成功的放进 handle」。本产品把失败原因收进
    :class:`InitOutcome` 而不只打日志。

    一处**收紧**：**六路全被剔除时直接抛错**。源实现只剔除不设下限，
    剔到零个 consumer 也会返回一个空 handle 继续跑下去；本产品若六路
    全废仍继续，财务科会拿到一份「零笔差异、零条单边账」的结论单——
    看着最干净，其实是最危险的。至少要有一路成功，结论才谈得上成立。
    """
    _default_registration(inbox_root)
    outcome = InitOutcome(period=period)
    for channel in channels or registry.channels():
        source = registry.get(channel)
        if source is None:
            outcome.failures.append(
                ChannelFailure(
                    channel=channel,
                    error_code="CHANNEL_UNKNOWN",
                    detail=f"渠道 {channel!r} 未注册",
                )
            )
            continue
        try:
            source.initialize(action, {"period": period, "channel": channel})
        except ReconqError as exc:
            logger.warning(
                "渠道 %s 初始化失败，已剔除（其余渠道照常）：[%s] %s",
                channel,
                exc.code,
                exc.detail,
            )
            outcome.failures.append(
                ChannelFailure(
                    channel=channel,
                    error_code=exc.code,
                    detail=exc.detail,
                    channel_level=error_meta(exc.code).channel_level,
                    cause_code=exc.cause_code,
                )
            )
            continue
        outcome.initialized.append(channel)

    if not outcome.initialized:
        codes = "、".join(sorted({f.error_code for f in outcome.failures})) or "（无）"
        err = ReconqError(
            "FORMAT_BROKEN",
            f"账期 {period} 六路渠道全部剔除，没有任何一路可用；失败码：{codes}。"
            "此时继续跑只会得到一份「零笔差异」的空结论单，因此中止本次扫描。"
            "先按 reconq data-source test <渠道> 逐路排查",
        )
        # 把逐路失败明细挂在异常上：调用方（编排层）知道本次**实际**跑了哪
        # 几路，能据此把「六路全废」改写成「本次要跑的那几路全废」并报出
        # 真实成因码。挂在异常上而不是改这里的文案，是因为这句话的视角
        # 取决于调用方的重跑范围，registry 并不知道。
        err.failures = list(outcome.failures)
        raise err
    return outcome


#: ``data-source test`` 的连通性探针表。真实协议由各源自己实现；
#: 这里只保证六路都有可读的回答，而不是「未实现」。
_PROBES: dict[str, Callable[[], dict[str, Any]]] = {}


def register_probe(channel: str, fn: Callable[[], dict[str, Any]]) -> None:
    _PROBES[channel] = fn


def _his_probe() -> dict[str, Any]:
    """HIS 走**只读连接池**探针（延迟导入，避免循环依赖）。

    Task 12 起不再走「裸连 + ``SELECT 1``」那条路：连上之后还要**当场
    断言只读真生效**，并把池的实际配置与状态报出来。理由很直接——
    信息科问「这一路通不通」时，他想知道的恰恰是「我连上去之后能不能写」。
    答案必须是「连上就写不了」，而且是**当场验出来的**，不是文档里承诺的。

    探针只查「能否连通 + 是否只读」，不拉任何业务行。
    """
    import os

    from ..errors import ReconqError
    from .his_pool import build_his_pool

    if os.environ.get("HIS_DB_DSN", "").strip() == "":
        from .his_db import probe_his_connection

        return probe_his_connection()

    try:
        pool = build_his_pool()
    except ReconqError as exc:
        return {
            "channel": "his",
            "ok": False,
            "error_code": exc.code,
            "detail": exc.detail,
            "credential_env_vars": ["HIS_DB_DSN"],
            "connected": False,
        }
    try:
        with pool.session():
            status = pool.pool_status()
        return {
            "channel": "his",
            "ok": True,
            "error_code": None,
            "detail": (
                f"HIS 只读库连通（{pool.dialect}）："
                f"只读事务已断言生效，本产品在该连接上发不出任何写语句"
            ),
            "credential_env_vars": ["HIS_DB_DSN"],
            "connected": True,
            "dialect": pool.dialect,
            "read_only_verified": True,
            "pool": status,
            "pool_config": pool.config.to_dict(),
        }
    except ReconqError as exc:
        return {
            "channel": "his",
            "ok": False,
            "error_code": exc.code,
            "detail": exc.detail,
            "credential_env_vars": ["HIS_DB_DSN"],
            "connected": False,
            "read_only_verified": False,
        }
    finally:
        pool.dispose()


register_probe("his", _his_probe)


def probe_channel(channel: str) -> dict[str, Any]:
    """``reconq data-source test <channel>``：查凭据位与协议连通性。

    返回**可读结果**而不是抛异常：``data-source test`` 的用途就是
    「告诉信息科缺什么、怎么补」，抛栈反而挡住了那句话。缺凭据时结果
    里点名环境变量名。
    """
    if channel not in registry:
        _default_registration()
    if channel not in registry:
        return {
            "channel": channel,
            "ok": False,
            "error_code": "CHANNEL_UNKNOWN",
            "detail": f"未知渠道 {channel!r}；已登记：{'、'.join(registry.channels())}",
            "connected": False,
        }

    probe = _PROBES.get(channel)
    if probe is not None:
        return probe()

    from .properties import CHANNEL_PROPERTIES, build_properties

    props_cls = CHANNEL_PROPERTIES.get(channel)
    credential_env_vars = list(props_cls.required_credentials) if props_cls else []

    # 文件通道（院内银联 / 自助机）不配凭据，只查目录与文件是否存在。
    if credential_env_vars:
        import os

        missing = [v for v in credential_env_vars if os.environ.get(v, "") == ""]
        if missing:
            # **一次列全**缺的变量，而不是报一个补一个：``build_properties``
            # 抛的是首个缺失位，但「补哪个」是信息科一次要办完的事。
            return {
                "channel": channel,
                "ok": False,
                "error_code": "CREDENTIAL_MISSING",
                "detail": f"渠道 {channel} 缺少必填凭据位：{'、'.join(missing)}",
                "credential_env_vars": credential_env_vars,
                "missing_env_vars": missing,
                "connected": False,
            }

    if channel == "his":
        from .his_db import probe_his_connection

        result = probe_his_connection()
        result.setdefault("credential_env_vars", credential_env_vars)
        return result

    return {
        "channel": channel,
        "ok": True,
        "error_code": None,
        "detail": "文件通道：本轮不需要凭据（院内导出账单到 inbox/<账期>/<渠道>/）",
        "credential_env_vars": credential_env_vars,
        "connected": True,
        "mode": "inbox_files",
    }

