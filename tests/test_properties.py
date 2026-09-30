"""连接属性装配测试（Task 4）。

对照参考实现 ``data_source_connection_properties.py:12-40`` 的
``to_connection_kwargs()`` 三步语义：重命名、解包、丢 None；
外加本产品新增的「缺必填凭据点名环境变量」。

⚠️ 测试里的凭据位一律是**占位值**，不是任何真实商户密钥，也不连真实环境。
"""

from __future__ import annotations

import pytest

from reconq.errors import ReconqError
from reconq.sources.properties import (
    CHANNEL_PROPERTIES,
    AlipayProperties,
    EnvSecret,
    HisProperties,
    WechatProperties,
    build_properties,
)

PLACEHOLDER_KEY = "<PLACEHOLDER_KEY>"
PLACEHOLDER_MCH = "<PLACEHOLDER_MCH_ID>"


def _wechat_env() -> dict[str, str]:
    return {
        "WECHAT_MCH_ID": PLACEHOLDER_MCH,
        "WECHAT_SERIAL_NO": "<PLACEHOLDER_SERIAL_NO>",
        "WECHAT_PRIVATE_KEY_PATH": "/tmp/placeholder-apiclient_key.pem",
        "WECHAT_API_V3_KEY": PLACEHOLDER_KEY,
    }


# —— ① field_mapping 重命名 ——


def test_field_mapping_renames_known_fields():
    props = WechatProperties(period="2026-W36", out_trade_no="<PLACEHOLDER_ORDER>")
    kwargs = props.to_connection_kwargs()
    # 契约列名 out_trade_no → 微信账单真实表头「商户订单号」
    assert kwargs["商户订单号"] == "<PLACEHOLDER_ORDER>"
    assert "out_trade_no" not in kwargs
    # 未在 field_mapping 里的键保持原名
    assert kwargs["period"] == "2026-W36"


def test_column_mapping_covers_all_six_channels():
    """六路渠道各自的列名映射都要有对外的真实列名，不能留空表。"""
    assert set(CHANNEL_PROPERTIES) == {
        "wechat",
        "alipay",
        "unionpay",
        "self_service",
        "his",
        "medical_ins",
    }
    for model in CHANNEL_PROPERTIES.values():
        assert model.field_mapping, f"{model.__name__} 缺列名映射"
        # 映射值不得与键同名（否则等于没映射）
        for internal, external in model.field_mapping.items():
            assert internal != external, f"{model.__name__} 的 {internal} 映射到自身"


def test_his_and_medical_ins_order_no_mapping():
    """HIS 收费记录主键 ↔ his_order_no；医保结算号 ↔ settle_no。"""
    assert HisProperties.column_mapping()["his_order_no"] == "收费记录主键"
    med = CHANNEL_PROPERTIES["medical_ins"].column_mapping()
    assert med["settle_no"] == "结算号"
    assert med["his_order_no"] == "结算号"


def test_column_mapping_returns_a_copy():
    """返回副本，改它不能污染类级映射表。"""
    mapping = WechatProperties.column_mapping()
    mapping["out_trade_no"] = "被改了"
    assert WechatProperties.column_mapping()["out_trade_no"] == "商户订单号"


# —— ② 凭据解包 ——


def test_env_secret_unwrapped_to_plaintext():
    secret = EnvSecret(env_var="WECHAT_API_V3_KEY", value=PLACEHOLDER_KEY)
    props = WechatProperties(WECHAT_API_V3_KEY=secret, period="2026-W36")
    kwargs = props.to_connection_kwargs()
    # 交给出站客户端的 kwargs 里是明文
    assert kwargs["WECHAT_API_V3_KEY"] == PLACEHOLDER_KEY


def test_secret_never_leaks_in_repr():
    secret = EnvSecret(env_var="WECHAT_API_V3_KEY", value=PLACEHOLDER_KEY)
    assert PLACEHOLDER_KEY not in repr(secret)
    assert "WECHAT_API_V3_KEY" in repr(secret)


# —— ③ 丢 None ——


def test_none_values_are_dropped():
    props = WechatProperties(period="2026-W36", WECHAT_MCH_ID=None)
    kwargs = props.to_connection_kwargs()
    assert "WECHAT_MCH_ID" not in kwargs
    assert "WECHAT_SERIAL_NO" not in kwargs


def test_empty_string_is_kept_but_none_is_dropped():
    """与源实现一致：只丢 None，空串照传（驱动需要区分「配了空」与「没配」）。"""
    props = AlipayProperties(ALIPAY_GATEWAY="")
    assert "ALIPAY_GATEWAY" in props.to_connection_kwargs()


# —— ④ 缺必填凭据 → 点名环境变量 ——


def test_missing_credential_names_the_env_var():
    env = _wechat_env()
    del env["WECHAT_API_V3_KEY"]
    with pytest.raises(ReconqError) as exc:
        build_properties("wechat", env=env)
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "WECHAT_API_V3_KEY" in exc.value.detail


def test_missing_credential_empty_string_also_rejected():
    env = _wechat_env()
    env["WECHAT_SERIAL_NO"] = ""
    with pytest.raises(ReconqError) as exc:
        build_properties("wechat", env=env)
    assert exc.value.code == "CREDENTIAL_MISSING"
    assert "WECHAT_SERIAL_NO" in exc.value.detail


def test_unknown_channel_is_rejected():
    with pytest.raises(ReconqError) as exc:
        build_properties("wechat_v2", env={})
    assert exc.value.code == "CHANNEL_UNKNOWN"


def test_check_credentials_lists_all_missing():
    props = WechatProperties()
    missing = props.check_credentials()
    assert set(missing) == set(WechatProperties.required_credentials)


def test_no_credentials_needed_for_file_channels():
    """院内银联/自助机走文件通道，本轮不要求凭据。"""
    for channel in ("unionpay", "self_service"):
        assert build_properties(channel, env={}) is not None
