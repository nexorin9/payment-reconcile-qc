# 归因离线回放集（Task 34）

## 这是什么

**固定证据包 + 录好的模型响应**。跑 `reconq attrib-replay` 时不调模型、不花钱、
不写任何台账，只把录好的响应喂进完整后处理链，看某一版提示词的表现。

```
tests/fixtures/attrib/
├── MANIFEST.md          本文件
├── generate_replay_set.py  重新生成 cases/*.json
├── cases/*.json         12 条固定证据包 + 期望性质
└── responses.json       三版提示词下录好的模型响应
```

重新生成证据包：`PYTHONPATH=src python3 tests/fixtures/attrib/generate_replay_set.py`

## 12 条用例覆盖什么

| # | case_id | 对齐形态 | 渠道 | 期望成因码 |
|---|---------|---------|------|-----------|
| 01 | `01-channel-only-his-missing` | `channel_only` | 微信 | `his_missing_order` / `channel_late_posting` |
| 02 | `02-weak-match-pending` | `weak_pending` | 支付宝 | `his_missing_order` / `merged_payment_allocation` / `undetermined` |
| 03 | `03-ambiguous-split-payment` | `ambiguous` | 微信 | `split_payment_unallocated` |
| 04 | `04-ambiguous-merged-payment` | `ambiguous` | 银联 | `merged_payment_allocation` |
| 05 | `05-ambiguous-refund-coexist` | `ambiguous` | 微信 | `refund_offset_in_his` / `bill_type_excludes_refund` |
| 06 | `06-cross-period-landing` | `cross_period` | 支付宝 | `period_boundary_landing` / `cross_period_duplicate` / `channel_late_posting` |
| 07 | `07-mismatch-amount` | `mismatch` | 微信 | `contract_config_error` / `undetermined` |
| 08 | `08-self-service-mismatch` | `mismatch` | 院内自助机 | `contract_config_error` / `undetermined` |
| 09 | `09-duplicate-claim` | `duplicate_claim` | 银联 | `merged_payment_allocation` / `contract_config_error` / `undetermined` |
| 10 | `10-unknown-equal-amounts` | `ambiguous` | 微信 | **只接受 `undetermined`** |
| 11 | `11-missing-his-order-no` | `channel_only` | 院内自助机 | `undetermined` / `his_missing_order` |
| 12 | `12-medical-ins-settlement-only` | `channel_only` | 医保结算 | `his_missing_order` / `channel_late_posting` |

六路渠道全覆盖（微信 5、支付宝 2、银联 2、自助机 2、医保 1），六种对齐形态
全覆盖。**第 10 条是刻意的**：候选金额全相同且与对侧不守恒时，正确答案只能是
`undetermined` —— 字典里没有「判不出来」这一项时，模型会被迫挑一条最像的，
而「我判不出来」比「我判错了」贵得多（前者让人去查证，后者让人签字）。

## 期望为什么写成「性质」而不是逐字答案

现场没有标准答案。标准答案是「归因该落到受控字典的哪一桶」—— 那才是经办人
签字前真正在核对的东西。所以每条用例的 `expect` 给三样：

- `cause_codes`：**可接受成因码的集合**（不是单值 —— 一条差异常有两个都说得通）
- `required_refs`：Top-1 **必须**引用的行号（不引用差异行却给出成因 = 凭印象归因）
- `min_refs`：全部保留草稿合计至少引用几行

模型措辞每次都不同，逐字比对只能评「像不像那句话」。

## 三版录好的响应

| 版本 | 模型行为 | 回放结论 |
|------|---------|---------|
| `v1` | 成因给**中文短语**，不注入受控字典 | 12 条里只有 6 条可评；其中 3 条「答了但措辞没对齐字典」—— 草稿过了引用与核对两道闸，却因用了字典外的措辞而落不了地 |
| `v2`（当前） | 第 6 条要求 `cause` 用**受控编码**并注入字典 | 12/12 全部可评且 Top-1 全中 |
| `v2-rc1` | v2 去掉「判不出来就用 `undetermined`」一句后的候选版 | 10/12 —— 信息耗尽的那条上模型改去硬猜，Top-1 由对变错。**已被回放否决**，留在注册表里是为了让「当初否决它的那份证据」可复算 |

```bash
reconq attrib-replay --list-versions            # 看三版与各自的变更说明
reconq attrib-replay --version v2               # 跑当前版
reconq attrib-replay --compare v1,v2            # 逐条对比（更差时退出码非零）
reconq attrib-replay --compare v2,v2-rc1        # 已否决版：报变差 + 告警 + 退出码 1
```

## 边界

* **响应是录的，不是算的**。重跑 `generate_replay_set.py` **不会**改
  `responses.json` —— 那是模型在真实端点上的实际输出，要重录得重新调模型。
  重新调模型测的是端点当天的状态，不是提示词版的差别，那是 `reconq scan` 的事。
* **证据包逐字节固定**。存 JSON 而不是每次重跑对齐器重建：重建依赖当期账单行
  与候选打分配置，两者都会漂移，漂移会让「提示词变好还是变坏」这个变量里
  混进「证据包也变了」那个说不清的量。
* **这些是脱敏样例**，单号用 `EX-*` 前缀、金额与时间取自 2026-W36 账期的
  构造数据，不含任何患者可识别明细。**不是**任何真实商户或 HIS 库的导出。
* **本轮未实连真实模型端点**。回放集里的响应是按 OpenAI-compatible
  `chat/completions` 的响应结构录的；产品代码走真实协议、凭据从环境变量读
  （见 `.env.example` 的 `RECONQ_MODEL_*`），测试替身只出现在
  `tests/fixtures/keys/` 等测试数据层。
