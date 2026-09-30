"""渠道协议兼容性测试的共用夹具（Task 13）。

本目录的测试**只出现在测试数据层**：报文样例放
``tests/fixtures/protocol/``，用 ``httpx.MockTransport`` 回放。
产品代码（``src/reconq/sources/*``）走的是真实协议——真实 URL、
真实 header 形态、真实错误码表、真实凭据位；替身不是最终对接方式。

⚠️ 本目录**不连接任何真实商户环境**，也**不含任何真实商户号、
私钥、平台证书或 APIv3 密钥**。真实端点的实连需要院内商户凭据，
本轮未实连。
"""

from __future__ import annotations

import json
import pathlib

import pytest

PROTOCOL_DIR = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "protocol"
KEYS_DIR = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "keys"

# —— 占位值：全部是 EXAM_/FAKE_ 前缀，不对应任何真实商户 ——
FAKE_API_V3_KEY = "0123456789abcdef0123456789abcdef"
FAKE_MCH_ID = "1900000109"  # 微信官方文档示例商户号，非真实商户
FAKE_SERIAL_NO = "5157F09EFDC096DE15EBE81A47057A72"
FAKE_PLATFORM_SERIAL = "TESTPLATFORMSERIAL0001"
FAKE_TIMESTAMP = 1757203200
FAKE_NONCE = "0123456789abcdef0123456789abcdef"
FAKE_APP_ID = "2021000000000000"

BILL_DATE = "2026-09-07"


def load_message(relative: str) -> dict:
    """读一个报文样例并去掉 ``_`` 前缀的说明键。"""
    raw = json.loads((PROTOCOL_DIR / relative).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_raw(relative: str) -> str:
    """读原始报文（JSON 或 XML 均适用）。"""
    return (PROTOCOL_DIR / relative).read_text(encoding="utf-8")


def http_status_of(relative: str) -> int:
    return int(json.loads((PROTOCOL_DIR / relative).read_text(encoding="utf-8"))["_http_status"])


def no_sleep(_seconds: float) -> None:
    """假时钟：退避序列照走，但不真的等几秒。"""


@pytest.fixture(scope="session")
def key_paths() -> dict[str, str]:
    """本地自签测试密钥路径（由 generate_test_pems.py 生成，非任何真实密钥）。"""
    paths = {
        "wechat_private": KEYS_DIR / "wechat_test_private.pem",
        "wechat_public": KEYS_DIR / "wechat_test_public.pem",
        "wechat_cert": KEYS_DIR / "wechat_platform_cert.pem",
        "alipay_private": KEYS_DIR / "alipay_test_private.pem",
        "alipay_public": KEYS_DIR / "alipay_test_public.pem",
    }
    missing = [name for name, p in paths.items() if not p.is_file()]
    if missing:
        pytest.fail(
            f"缺少测试密钥：{missing}；"
            f"请先运行 python tests/fixtures/keys/generate_test_pems.py 生成"
        )
    return {name: str(p) for name, p in paths.items()}


@pytest.fixture()
def wechat_env(key_paths) -> dict[str, str]:
    """微信凭据环境变量：全部为占位值，键名与产品读取的完全一致。"""
    return {
        "WECHAT_MCH_ID": FAKE_MCH_ID,
        "WECHAT_SERIAL_NO": FAKE_SERIAL_NO,
        "WECHAT_PRIVATE_KEY_PATH": key_paths["wechat_private"],
        "WECHAT_API_V3_KEY": FAKE_API_V3_KEY,
        "WECHAT_PLATFORM_CERT_PATH": key_paths["wechat_cert"],
        "WECHAT_PLATFORM_CERT_SERIAL_NO": FAKE_PLATFORM_SERIAL,
    }


@pytest.fixture()
def alipay_env(key_paths) -> dict[str, str]:
    """支付宝凭据环境变量：全部为占位值，键名与产品读取的完全一致。"""
    return {
        "ALIPAY_APP_ID": FAKE_APP_ID,
        "ALIPAY_PRIVATE_KEY_PATH": key_paths["alipay_private"],
        "ALIPAY_PUBLIC_KEY_PATH": key_paths["alipay_public"],
    }
