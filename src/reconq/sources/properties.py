"""连接属性装配：渠道列名映射 + 环境变量凭据注入。

源产品对应实现
--------------
``github_ref/soda-core/soda-core/src/soda_core/model/data_source/
data_source_connection_properties.py:12-40`` 的 ``to_connection_kwargs()``：

1. **按 ``field_mapping`` 重命名键**——数据源内部叫 ``username``，驱动要
   ``user``；2. **解包 ``SecretStr``** 成明文凭据；3. **丢弃 ``None``**——
   驱动不需要没配的键。

本轮改写两处
------------
* ``field_mapping`` 的语义从「数据库连接参数改名」换成**渠道账单列名映射**：
  契约里的 ``out_trade_no`` 要落到微信账单的真实表头「商户订单号」、
  支付宝的「支付宝交易号」、HIS 的「收费记录主键」、医保的「结算号」。
* ``SecretStr`` 不再从配置文件读，而是**从环境变量注入**的凭据对象；
  缺必填凭据时**显式报错并点名是哪个环境变量**，不等跑到一半才炸。

本产品额外加的一条红线
----------------------
源实现在凭据缺失时安静地少传一个键，由驱动在最底层报一句含糊的错。
对账场景不行——``WECHAT_API_V3_KEY`` 缺失必须在**发请求之前**就点名报错，
否则财务科会拿到一份「静默少了六路之一」的结论单。
"""

from __future__ import annotations

import os
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..errors import ReconqError

__all__ = [
    "EnvSecret",
    "BaseConnectionProperties",
    "WechatProperties",
    "AlipayProperties",
    "UnionpayProperties",
    "SelfServiceProperties",
    "HisProperties",
    "MedicalInsProperties",
    "CHANNEL_PROPERTIES",
    "build_properties",
]


class EnvSecret(BaseModel):
    """一个凭据位：**记着自己是哪个环境变量**，值用 ``SecretStr`` 包住。

    多带一个 ``env_var`` 是本产品相对源实现的加强：报错时能直接告诉
    财务科「你缺 ``WECHAT_API_V3_KEY``」，而不是把一个空的 ``user=``
    丢给驱动再让人猜。

    打包的契约：**凭据一律从环境变量读**；``from_env`` 是唯一构造入口，
    任何直接 ``EnvSecret(value=...)`` 的调用都只允许出现在测试里。
    """

    model_config = ConfigDict(frozen=True)

    env_var: str
    value: SecretStr

    @classmethod
    def from_env(cls, env_var: str, *, required: bool = True) -> Optional["EnvSecret"]:
        """从环境变量读一个凭据位。

        缺值且 ``required`` 时抛 ``CREDENTIAL_MISSING`` 并点名变量名；
        缺值且非必填时返回 ``None``（装配时被 ``to_connection_kwargs`` 丢弃）。
        """
        raw = os.environ.get(env_var, "")
        if raw == "":
            if required:
                raise _missing_credential(env_var)
            return None
        return cls(env_var=env_var, value=SecretStr(raw))

    def unwrap(self) -> str:
        """解包成明文。调用方是真实的出站客户端，不是日志。"""
        return self.value.get_secret_value()

    def __repr__(self) -> str:  # pragma: no cover - 防明文泄进日志/异常
        return f"EnvSecret(env_var={self.env_var!r}, value='********')"


def _missing_credential(env_var: str) -> ReconqError:
    return ReconqError(
        "CREDENTIAL_MISSING",
        f"环境变量 {env_var} 未设置或为空，无法完成连接属性装配",
    )


class BaseConnectionProperties(BaseModel):
    """连接属性基类：重命名 + 解包 + 丢 ``None``。

    方法体与源实现 ``to_connection_kwargs()`` 同序：
    先按 ``field_mapping`` 改名，再把 ``EnvSecret`` 解成明文，最后丢 ``None``。
    多了一步**必填凭据前置校验**，让缺位在装配阶段就报错。
    """

    model_config = ConfigDict(frozen=True, extra="allow", validate_by_name=True)

    #: 内部键名 → 渠道/HIS 侧真实名字。子类覆盖。
    field_mapping: ClassVar[dict[str, str]] = {}

    #: 必填凭据位名（环境变量名）。装配时逐个检查，缺一个即报错。
    required_credentials: ClassVar[tuple[str, ...]] = ()

    def to_connection_kwargs(self) -> dict[str, Any]:
        """装配成可直接交给客户端的 kwargs。

        顺序与源实现一致：重命名 → 解包凭据 → 丢 None。
        重命名对**所有**键生效（``field_mapping`` 里没有的键映射为自身），
        这样单号过滤条件之类的「声明字段」与「extra 字段」走同一条路径，
        不会出现「过滤条件改了名、extra 字段没改」的不一致。

        这里按属性取值而不用 ``model_dump()``：dump 会把嵌套的
        ``EnvSecret`` 拍平成 ``{"env_var":..., "value": SecretStr('***')}``，
        后面就认不出凭据对象、也不会解包了。
        """
        data: dict[str, Any] = {name: getattr(self, name) for name in self.__class__.model_fields}
        data.update(dict(self.__pydantic_extra__ or {}))

        mapped: dict[str, Any] = {}
        for key, value in data.items():
            out_key = self.field_mapping.get(key, key)
            if value is not None:
                mapped[out_key] = value

        # 解包凭据 + 丢 None
        cleaned: dict[str, Any] = {}
        for key, value in mapped.items():
            if isinstance(value, EnvSecret):
                value = value.unwrap()
            if value is not None:
                cleaned[key] = value
        return cleaned

    @classmethod
    def column_mapping(cls) -> dict[str, str]:
        """本渠道的**列名映射**：契约规范列名 → 渠道/HIS 真实列名。

        与 ``to_connection_kwargs`` 分开：前者给取数客户端组装连接参数，
        后者给行读取器把渠道表头翻译回契约列名。方向相反，别混用。
        """
        return dict(cls.field_mapping)

    def check_credentials(self) -> list[str]:
        """返回缺失的必填凭据位名；一个不缺时返回空列表。

        与 ``from_env`` 的即时抛错互补：批量装配六路时，先把六路缺的
        变量一次列全，再报错，避免「修一个报一个」。
        """
        missing: list[str] = []
        for env_var in self.required_credentials:
            secret = getattr(self, env_var, None)
            if secret is None or secret.unwrap() == "":
                missing.append(env_var)
        return missing


# —— 六路渠道的列名映射 ——
# 键 = 契约里的规范列名；值 = 渠道账单 / HIS 库里的真实列名。
# 与 inbox_files.BILL_HEADER_ALIASES 的方向相反：那边是「表头 → 规范名」，
# 这边是「规范名 → 外部名」，因为装配结果要交给外部客户端。

_WECHAT_CREDENTIALS = (
    "WECHAT_MCH_ID",
    "WECHAT_SERIAL_NO",
    "WECHAT_PRIVATE_KEY_PATH",
    "WECHAT_API_V3_KEY",
)

_ALIPAY_CREDENTIALS = (
    "ALIPAY_APP_ID",
    "ALIPAY_PRIVATE_KEY_PATH",
    "ALIPAY_PUBLIC_KEY_PATH",
    "ALIPAY_GATEWAY",
)


class WechatProperties(BaseConnectionProperties):
    """微信支付账单。"""

    field_mapping: ClassVar[dict[str, str]] = {
        "out_trade_no": "商户订单号",
        "transaction_id": "微信交易单号",
        "success_time": "交易时间",
        "trade_state": "交易状态",
        "amount": "应结订单金额",
        "refund": "退款金额",
    }
    required_credentials: ClassVar[tuple[str, ...]] = _WECHAT_CREDENTIALS

    WECHAT_MCH_ID: Optional[EnvSecret] = None
    WECHAT_SERIAL_NO: Optional[EnvSecret] = None
    WECHAT_PRIVATE_KEY_PATH: Optional[str] = None
    WECHAT_API_V3_KEY: Optional[EnvSecret] = None
    bill_type: str = "ALL"
    period: Optional[str] = None
    #: 取某笔明细时的过滤条件（可选）。装配后会按 field_mapping 改名成
    #: 渠道账单的真实表头，下游读到的键就是「商户订单号」而不是内部列名。
    out_trade_no: Optional[str] = None
    transaction_id: Optional[str] = None


class AlipayProperties(BaseConnectionProperties):
    """支付宝账单。"""

    field_mapping: ClassVar[dict[str, str]] = {
        "trade_no": "支付宝交易号",
        "out_trade_no": "商户订单号",
        "biz_type": "业务类型",
        "amount": "金额",
        "trade_time": "交易时间",
        "income_expense": "收/支",
    }
    required_credentials: ClassVar[tuple[str, ...]] = _ALIPAY_CREDENTIALS

    ALIPAY_APP_ID: Optional[EnvSecret] = None
    ALIPAY_PRIVATE_KEY_PATH: Optional[str] = None
    ALIPAY_PUBLIC_KEY_PATH: Optional[str] = None
    ALIPAY_GATEWAY: str = "https://openapi.alipay.com/gateway.do"
    bill_type: str = "trade"
    period: Optional[str] = None
    out_trade_no: Optional[str] = None
    trade_no: Optional[str] = None


class UnionpayProperties(BaseConnectionProperties):
    """院内银联账单（本轮走 ``inbox/<period>/<channel>/`` 文件通道）。

    文件通道是**本轮取数形态，不是最终对接方式**——最终形态是银联正式接口。
    """

    field_mapping: ClassVar[dict[str, str]] = {
        "out_trade_no": "订单号",
        "transaction_id": "银联交易单号",
        "amount": "交易金额",
        "trade_time": "交易时间",
        "settle_date": "清算日期",
    }
    required_credentials: ClassVar[tuple[str, ...]] = ()

    inbox_root: str = "inbox"
    period: Optional[str] = None


class SelfServiceProperties(BaseConnectionProperties):
    """院内自助机账单（同上，文件通道）。"""

    field_mapping: ClassVar[dict[str, str]] = {
        "out_trade_no": "自助机订单号",
        "transaction_id": "自助机流水号",
        "amount": "缴费金额",
        "trade_time": "交易时间",
        "terminal_no": "终端号",
    }
    required_credentials: ClassVar[tuple[str, ...]] = ()

    inbox_root: str = "inbox"
    period: Optional[str] = None


class HisProperties(BaseConnectionProperties):
    """HIS 收费流水（只读库）。"""

    field_mapping: ClassVar[dict[str, str]] = {
        "his_order_no": "收费记录主键",
        "amount": "金额",
        "charge_time": "收费时间",
        "period": "账期",
        "dept": "科室",
        "settle_type": "结算方式",
    }
    required_credentials: ClassVar[tuple[str, ...]] = ("HIS_DB_DSN",)

    HIS_DB_DSN: Optional[EnvSecret] = None
    read_only: bool = True
    period: Optional[str] = None
    his_order_no: Optional[str] = None


class MedicalInsProperties(BaseConnectionProperties):
    """医保结算回传。"""

    field_mapping: ClassVar[dict[str, str]] = {
        "settle_no": "结算号",
        "his_order_no": "结算号",
        "amount": "金额合计",
        "fund_pay": "基金支付",
        "supp_ins": "补充医疗",
        "aid_pay": "医疗救助",
        "self_pay": "个人负担",
    }
    required_credentials: ClassVar[tuple[str, ...]] = ("HIS_DB_DSN",)

    HIS_DB_DSN: Optional[EnvSecret] = None
    read_only: bool = True
    period: Optional[str] = None


#: 渠道 → 连接属性模型。与 registry.py 的注册表同源，此处先给静态映射。
CHANNEL_PROPERTIES: dict[str, type[BaseConnectionProperties]] = {
    "wechat": WechatProperties,
    "alipay": AlipayProperties,
    "unionpay": UnionpayProperties,
    "self_service": SelfServiceProperties,
    "his": HisProperties,
    "medical_ins": MedicalInsProperties,
}


def build_properties(channel: str, *, env: Optional[dict[str, str]] = None) -> BaseConnectionProperties:
    """从环境变量装配某一路渠道的连接属性。

    缺任一必填凭据 → ``CREDENTIAL_MISSING``，错误信息点名环境变量名。
    """
    model_cls = CHANNEL_PROPERTIES.get(channel)
    if model_cls is None:
        raise ReconqError(
            "CHANNEL_UNKNOWN",
            f"未知渠道 {channel!r}，已登记渠道：{'、'.join(sorted(CHANNEL_PROPERTIES))}",
            channel=channel,
        )

    values: dict[str, Any] = {}
    for env_var in model_cls.required_credentials:
        raw = (env or os.environ).get(env_var, "")
        if raw == "":
            raise _missing_credential(env_var)
        # DSN 类是连接串，同样按凭据位包起来，不进日志
        values[env_var] = EnvSecret(env_var=env_var, value=SecretStr(raw))

    props = model_cls(**values)
    missing = props.check_credentials()
    if missing:
        raise ReconqError(
            "CREDENTIAL_MISSING",
            f"渠道 {channel} 缺少必填凭据位：{'、'.join(missing)}",
            channel=channel,
        )
    return props
