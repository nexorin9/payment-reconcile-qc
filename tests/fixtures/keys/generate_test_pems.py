"""生成本地自签测试密钥（**不是**任何真实商户的密钥）。

跑法::

    python tests/fixtures/keys/generate_test_pems.py

产物（全部在 ``tests/fixtures/keys/`` 下）::

    wechat_test_private.pem    微信商户私钥替身
    wechat_test_public.pem      对应的公钥
    wechat_platform_cert.pem    微信平台证书替身（自签 X.509）
    alipay_test_private.pem     支付宝应用私钥替身
    alipay_test_public.pem      支付宝公钥替身

这些文件由本脚本**一次性生成后随仓库提交**，测试直接读它们，
不在每个测试里现生成（现生成会让 RSA 密钥对拖慢整个测试集）。
重新生成本身是安全的：签名与验签都在同一个测试进程内闭环。

⚠️ 它们是**自签的测试替身**，与任何真实商户号、真实平台证书、
真实支付宝应用无关，**禁止**在任何真实环境复用，**禁止**提交到
任何对外的密钥库。
"""

from __future__ import annotations

import datetime as dt
import pathlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

HERE = pathlib.Path(__file__).resolve().parent


def _write(path: pathlib.Path, data: bytes) -> None:
    path.write_bytes(data)
    print(f"  写出 {path.relative_to(HERE.parents[2])}")


def _private_pem(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_pem(private_key) -> bytes:
    key = private_key.public_key()
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _self_signed_cert(key, common_name: str) -> bytes:
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    # 固定起止日期，避免每次生成都让 git 看到变更；测试不过期校验。
    not_before = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    not_after = dt.datetime(2036, 1, 1, tzinfo=dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def main() -> None:
    print("生成本地自签测试密钥（非真实商户密钥）…")
    wechat = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _write(HERE / "wechat_test_private.pem", _private_pem(wechat))
    _write(HERE / "wechat_test_public.pem", _public_pem(wechat))
    _write(
        HERE / "wechat_platform_cert.pem",
        _self_signed_cert(wechat, "RECONQ-TEST-WECHAT-PLATFORM-CERT"),
    )

    alipay = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _write(HERE / "alipay_test_private.pem", _private_pem(alipay))
    _write(HERE / "alipay_test_public.pem", _public_pem(alipay))

    # 一把**不属于**上面两把的第三方密钥，用于构造「验签失败」的反例
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _write(HERE / "other_test_public.pem", _public_pem(other))
    print("完成。")


if __name__ == "__main__":
    main()
