"""六路渠道注册表与逐渠道失败剔除测试（Task 6）。

对照参考实现 ``streaming_orchestrator.py:27-65`` 的三段语义：

* ``_consumers`` 进程级注册 + ``_rebuild_action_map()`` 按 action 建映射；
* ``initialize_action()`` 逐 consumer ``try/except``，**初始化失败的被剔除出
  本次 handle** 并 ``logger.warning``，循环继续；
* ``reset()`` 清空注册表供测试隔离。

本产品把「consumer」换成「一路渠道账单源」，并加了一条源实现没有的
下限：**六路全废时直接中止**（见 :func:`initialize_all` 的说明）。
"""

from __future__ import annotations

import os

import pytest

from reconq.errors import ReconqError
from reconq.sources.registry import (
    CHANNELS,
    ChannelFailure,
    HisChannelSource,
    InboxChannelSource,
    InitOutcome,
    SourceRegistry,
    get_source,
    initialize_all,
    probe_channel,
    registry,
)

PERIOD = "2026-W36"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    """每个用例前后都清空进程级注册表（对应源实现的 ``reset()``）。

    顺带放一个**占位** ``HIS_DB_DSN``，让 ``his`` 路能过凭据位检查——
    这里测的是注册表语义，不是 HIS 连通性（那由 ``test_his_db.py`` 负责，
    用本地 SQLite 替身）。占位串不含任何真实凭据。
    """
    monkeypatch.setenv("HIS_DB_DSN", "sqlite://")
    registry.reset()
    yield
    registry.reset()


class _AlwaysFails:
    """一支永远初始化失败的渠道源（模拟当天没出账单 / 凭据不对）。"""

    def __init__(self, channel: str = "broken") -> None:
        self.channel = channel

    def supported_actions(self):
        return ("fetch_bill",)

    def initialize(self, action, context):
        raise ReconqError(
            "BILL_NOT_EXIST",
            "渠道侧没有该账单日的账单",
            channel=self.channel,
        )

    def fetch(self, period):
        raise AssertionError("初始化失败时不应走到 fetch")


class _GlobalFailure:
    """一支初始化时抛非渠道级错误的源（模拟全局配置坏了）。"""

    def __init__(self, channel: str = "broken") -> None:
        self.channel = channel

    def supported_actions(self):
        return ("fetch_bill",)

    def initialize(self, action, context):
        raise ReconqError("CHECK_OPERATOR_UNSUPPORTED", "全局口径配置有问题")

    def fetch(self, period):
        raise AssertionError


# —— ① 注册表结构（对应 register_consumer / _rebuild_action_map）——


def test_registry_registers_six_channels():
    """六路必须都登记得进来，顺序按面板展示序。"""
    outcome = initialize_all(PERIOD)
    assert outcome.initialized == list(CHANNELS)
    assert len(CHANNELS) == 6
    assert "wechat" in registry and "medical_ins" in registry


def test_action_map_rebuilt_on_registration():
    """按 action 建映射：六路都支持 fetch_bill。"""
    initialize_all(PERIOD)
    consumers = registry.consumers_for("fetch_bill")
    assert {c.channel for c in consumers} == set(CHANNELS)
    assert registry.consumers_for("no_such_action") == []


def test_get_source_registers_lazily():
    """get_source 未登记时先跑默认登记，仍没有才报未知渠道。"""
    assert get_source("wechat").channel == "wechat"
    with pytest.raises(ReconqError) as ei:
        get_source("nope")
    assert ei.value.code == "CHANNEL_UNKNOWN"


def test_reset_clears_registrations():
    """reset() 后注册表为空——对应源实现给测试隔离用的 reset()。"""
    initialize_all(PERIOD)
    assert len(registry) == 6
    registry.reset()
    assert len(registry) == 0
    assert registry.channels() == []


# —— ② 逐渠道失败剔除：5 路成功 + 1 路 BILL_NOT_EXIST ———


def test_one_channel_failure_does_not_block_others():
    """核心语义：一路失败只剔除自己，其余五路照常初始化。

    对应源实现 ``initialize_action()`` 的逐 consumer try/except。
    """
    registry.register(_AlwaysFails())
    outcome = initialize_all(PERIOD)

    assert set(outcome.initialized) == set(CHANNELS)
    assert outcome.failed_channels == {"broken"}
    assert outcome.error_code_of("broken") == "BILL_NOT_EXIST"
    assert outcome.ok_channels.isdisjoint(outcome.failed_channels)


def test_failure_records_channel_and_error_code():
    """失败原因必须被**收集**起来，不只是打一行日志。

    源实现剔除后只 ``logger.warning``，失败原因就丢在 exc_info 里；本产品
    要把「哪一路没取到 / 什么码」放进结论单，所以必须留在返回值里。
    """
    registry.register(_AlwaysFails())
    outcome = initialize_all(PERIOD)
    failure = next(f for f in outcome.failures if f.channel == "broken")
    assert isinstance(failure, ChannelFailure)
    assert failure.error_code == "BILL_NOT_EXIST"
    assert "没有该账单日" in failure.detail
    assert failure.to_dict()["channel"] == "broken"
    assert outcome.to_dict()["failures"][0]["error_code"] == "BILL_NOT_EXIST"


def test_failure_is_frozen_so_outcome_cannot_drift():
    """失败明细一旦生成就不许被改写——否则「哪些路没取到」会随调用顺序漂移。"""
    registry.register(_AlwaysFails())
    outcome = initialize_all(PERIOD)
    failure = outcome.failures[0]
    with pytest.raises(Exception):
        failure.error_code = "SOMETHING_ELSE"  # type: ignore[misc]


def test_failed_channel_is_excluded_from_initialized():
    """被剔除的渠道不得同时出现在 initialized 里（源实现的关键保证）。"""
    registry.register(_AlwaysFails())
    outcome = initialize_all(PERIOD)
    assert "broken" not in outcome.initialized
    assert "broken" not in outcome.ok_channels


# —— ③ 收紧：六路全废时中止，不产出空结论单 ———


def test_all_channels_failed_aborts_scan():
    """六路全剔除时直接抛错。

    源实现剔到零个 consumer 也会返回一个空 handle 让流程继续；本产品若
    继续跑，财务科会拿到一份「零笔差异、零条单边账」的结论单——看着最
    干净，其实是最危险的。
    """
    # 换成只认这套失败源的注册表，绕开默认登记（六路都真实可初始化）
    monkey = SourceRegistry()
    for ch in CHANNELS:
        monkey.register(_AlwaysFails(ch))
    registry.__dict__.update(monkey.__dict__)
    with pytest.raises(ReconqError) as ei:
        initialize_all(PERIOD)
    assert "全部剔除" in ei.value.detail
    assert "BILL_NOT_EXIST" in ei.value.detail


def test_global_failure_is_kept_as_failure_not_silent_pass():
    """全局级错误码也记为失败并可见，不会因为「不是渠道级」就消失。"""
    registry.register(_GlobalFailure())
    outcome = initialize_all(PERIOD)
    assert outcome.error_code_of("broken") == "CHECK_OPERATOR_UNSUPPORTED"
    # 但其余五路仍然可用——不让一路的问题拖垮整次扫描
    assert set(outcome.initialized) == set(CHANNELS)


# —— ④ data-source test：逐路给出可读答案 ———


def test_probe_file_channel_needs_no_credentials(monkeypatch):
    """院内文件通道（银联/自助机）不配凭据也要给出可读答案。"""
    for var in ("WECHAT_API_V3_KEY", "ALIPAY_APP_ID", "HIS_DB_DSN"):
        monkeypatch.delenv(var, raising=False)
    result = probe_channel("unionpay")
    assert result["ok"] is True
    assert result["mode"] == "inbox_files"
    assert result["credential_env_vars"] == []


def test_probe_missing_credential_names_env_var(monkeypatch):
    """缺凭据时点名环境变量名——不给「认证失败」这种没法行动的答案。"""
    for var in ("WECHAT_MCH_ID", "WECHAT_SERIAL_NO", "WECHAT_PRIVATE_KEY_PATH", "WECHAT_API_V3_KEY"):
        monkeypatch.delenv(var, raising=False)
    result = probe_channel("wechat")
    assert result["ok"] is False
    assert result["error_code"] == "CREDENTIAL_MISSING"
    # 四个缺失位**一次列全**——信息科一次办完，而不是报一个补一个
    assert set(result["missing_env_vars"]) == {
        "WECHAT_MCH_ID",
        "WECHAT_SERIAL_NO",
        "WECHAT_PRIVATE_KEY_PATH",
        "WECHAT_API_V3_KEY",
    }
    assert "WECHAT_API_V3_KEY" in result["detail"]


def test_probe_unknown_channel_lists_registered():
    """未知渠道要说清已登记了哪些，而不是只抛一句 KeyError。"""
    result = probe_channel("nope")
    assert result["ok"] is False
    assert result["error_code"] == "CHANNEL_UNKNOWN"
    assert "wechat" in result["detail"]


def test_probe_his_reports_dsn_env_var(monkeypatch):
    """HIS 走真实连接探针；没配 DSN 时点名 HIS_DB_DSN。"""
    monkeypatch.delenv("HIS_DB_DSN", raising=False)
    result = probe_channel("his")
    assert result["ok"] is False
    assert result["error_code"] == "HIS_DB_DSN_MISSING"
    assert "HIS_DB_DSN" in result["credential_env_vars"]


# —— ⑤ 源类型与动作契约 ———


def test_source_rejects_unknown_action():
    """渠道源只支持 fetch_bill；传别的动作要显式报错。"""
    src = InboxChannelSource("wechat")
    with pytest.raises(ReconqError) as ei:
        src.initialize("no_such_action", {})
    assert ei.value.code == "CHANNEL_UNKNOWN"


def test_his_source_supported_action():
    assert HisChannelSource().supported_actions() == ("fetch_bill",)


def test_init_outcome_defaults_are_empty():
    """空的 InitOutcome 不得谎报成功渠道。"""
    outcome = InitOutcome(period=PERIOD)
    assert outcome.ok_channels == set()
    assert outcome.failed_channels == set()
    assert outcome.error_code_of("wechat") is None


def test_project_root_inbox_exists_for_file_channels():
    """文件通道的默认根目录在项目内（项目结构约定）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.isdir(os.path.join(root, "inbox"))
