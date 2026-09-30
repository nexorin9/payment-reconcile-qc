# 渠道协议报文样例目录（测试数据层）

本目录下的文件是**协议报文样例**，供 `tests/protocol/` 的兼容性测试回放。

## 边界声明（三条，不可含糊）

1. **替身只出现在本目录与 `tests/protocol/`**。产品代码
   （`src/reconq/sources/wechat_bill.py`、`alipay_bill.py`、`signing.py`）
   走**真实协议**：真实 URL、真实 header 形态、真实错误码表、真实凭据位。
   本目录的文件不参与产品运行路径。
2. **最终对接方式不是本目录**。院内真实对接是直连渠道正式接口
   （微信 `https://api.mch.weixin.qq.com/v3/bill/tradebill`、
   支付宝 `https://openapi.alipay.com/gateway.do`），
   凭据从环境变量读。本目录**不是**对接方式，只是让协议变更能被测出来。
3. **不含任何真实凭据**。全部商户号、订单号、密钥串均为
   `EXAM_*` / `FAKE_*` / `PLACEHOLDER_*` 占位；测试用私钥与平台证书
   由 `tests/fixtures/keys/generate_test_pems.py` 本地自签生成，
   与任何真实商户无关，**禁止**在任何真实环境复用。

## 目录内容

| 文件 | 渠道 | HTTP | 覆盖的错误/成功类别 | 产品应映射为 |
|------|------|------|-------------------|------------|
| `wechat/take_success.json` | 微信 | 200 | 取件成功 | 取得 `download_url` 后立即下载并校验 SHA1 |
| `wechat/take_bill_not_exist.json` | 微信 | 400 | 账单不存在 | `BILL_NOT_EXIST`（不可重试） |
| `wechat/take_statement_creating.json` | 微信 | 400 | 账单生成中 | `STATEMENT_CREATING`（可重试） |
| `wechat/take_frequency_limited.json` | 微信 | 429 | 触发限频 | `FREQUENCY_LIMITED`（可重试，用尽抛 `RATE_LIMITED_EXHAUSTED`） |
| `wechat/take_sign_error.json` | 微信 | 401 | 签名/鉴权错误 | `CREDENTIAL_MISSING`（不可重试） |
| `wechat/take_signature_invalid.json` | 微信 | 200 | **报文正常但验签失败** | `RESPONSE_SIGNATURE_INVALID`（不落任何账单行） |
| `wechat/take_legacy_xml_error.xml` | 微信 | 200 | 非 JSON 信封（V2 XML） | `CHANNEL_REQUEST_FAILED`（不得当成空账单） |
| `alipay/query_success.json` | 支付宝 | 200 | 取件成功 | 取得 `bill_url` 后立即下载 zip |
| `alipay/query_empty_data_with_bill_file.json` | 支付宝 | 200 | 有文件无数据 | **成功态** → 空数据集 + `empty_reason` |
| `alipay/query_bill_not_exist.json` | 支付宝 | 200 | 账单不存在 | `BILL_NOT_EXIST`（不可重试） |
| `alipay/query_frequency_limited.json` | 支付宝 | 200 | 触发限频 | `CHANNEL_REQUEST_FAILED`（渠道级，不硬重试） |
| `alipay/query_xml_error.xml` | 支付宝 | 200 | 非 JSON 信封（XML） | `CHANNEL_REQUEST_FAILED`（不得当成空数据集） |

## 两类「长得正常但不能用」的报文

`take_signature_invalid.json` 与 `query_empty_data_with_bill_file.json` 是本目录里
最值得注意的两个：**HTTP 200、字段齐全、单看内容毫无破绽**。前者必须因验签
失败被拒，后者必须被当作成功而非错误。把它们放在一起做回归，
是为了防住「一律按 200 放行」和「一律按非 10000 报错」这两种简化写法。
