# 实测截图 · 采集说明

本目录的五张图**全部是真实运行的产物**，不是设计稿、不是概念图、不是
「照着想象补的」效果。每张图都写清采集命令与采集时间，便于逐张复核。

采集环境：macOS · Python 3.11 · Google Chrome（Playwright 驱动真实浏览器）·
账期 `2026-W36` · 账单库 `out/shot-scan.sqlite` · 采集时间 **2026-09-30 21:03–21:05**。

## 样例规模

截图用的不是十几行的玩具数据，而是 `scripts/gen_fixtures.py` 生成的
**六路各 2000+ 行**分层样例：微信 2202 / 支付宝 2252 / 银联 2232 /
自助机 2192 / 医保 2182 / HIS 10970 行，合计 22030 笔。
样本按九种真实形态分层（正常对账、渠道内退费、单边账、一码付拆笔、
跨账期落点、重复认领、账期边界行等），故意留下 4 条口径不通过。

## 五张图与采集命令

| 图 | 采集命令 / 方式 | 内容与可核对点 |
|---|---|---|
| `01-cli-verify.png` | `reconq scan --period 2026-W36 --status` + `reconq verify --period 2026-W36 --source out/scale --channel wechat` | 真实终端输出。六路各自主态与笔数/金额；微信 15 条口径逐条 ✓/✗；`rows.refund_sum.zero` 不通过并给出 `wechat_tradebill_2026-W36.csv:34 / :67 / :100 …` 逐行证据与各行 refund 金额 |
| `02-panel-scan.png` | `reconq serve --db out/shot-scan.sqlite` 后用真实浏览器打开 `http://127.0.0.1:8788/` | 扫描完成后的三栏面板：左栏六路扫描态、中栏差异与成因台账（按成因分组 3 组）、右栏待办（经办 4 条） |
| `03-panel-decision.png` | 同上，**在浏览器里真点**：选中中栏「微信支付 · 本期退款金额须为零」→ 填操作人「财务科-张」与处置结论 → 点「提交处置结论」 | 处置**之后**的界面：中栏该行状态变「待确认（经办已交结论，等审核）」，右栏「本条采用：采用人工结论（经办已签字）」并显示该条结论原文与经办。库里 `ledger_discrepancies` 状态确实从 `待归因` 推进到 `待确认`，版本 0 → 1 |
| `04-export-conclusion.png` | `reconq export --period 2026-W36 --fmt md --out out/2026-W36-conclusion.md` | 真实导出文件的**前 34 行**：指纹头（结论集指纹、事件链根指纹、导出人、导出时刻）、六路取数主态表 |
| `05-metrics-report.png` | `reconq align-metrics --out out/metrics-report.md` | 真实生成的指标报告：80 条金标集上本产品 vs 逐笔 VLOOKUP 基线的六项指标对比（结论一致率 100% vs 40%、歧义漏判率 0% vs 100%、静默择一率 0% vs 100%）与四条门禁判定 |

## 复现

```bash
# 1) 生成六路 2000+ 行样例（HIS 侧装进本地 SQLite 替身库）
PYTHONPATH=src python3 scripts/gen_fixtures.py --out out/scale --period 2026-W36 --force

# 2) 扫描 + 跑对齐主路径 + 给差异开待归因处置单
HIS_DB_DSN="sqlite:///out/shot-his.sqlite" \
  PYTHONPATH=src python3 -m reconq scan --period 2026-W36 --source out/scale --db out/shot-scan.sqlite
PYTHONPATH=src python3 -m reconq align-pipeline --period 2026-W36 --db out/shot-scan.sqlite
PYTHONPATH=src python3 scripts/open_orders_for_shot.py --db out/shot-scan.sqlite --period 2026-W36

# 3) 起常驻服务，另开一个终端采集面板截图（02 / 03）
PYTHONPATH=src python3 -m reconq serve --db out/shot-scan.sqlite --port 8788 &
PYTHONPATH=src python3 scripts/capture_panel.py --out docs/screenshots

# 4) 终端输出渲染成图（01 / 04 / 05）
PYTHONPATH=src python3 -m reconq export --period 2026-W36 --db out/shot-scan.sqlite --out out/2026-W36-conclusion.md
PYTHONPATH=src python3 -m reconq align-metrics --out out/metrics-report.md
python3 scripts/render_terminal.py --in <真实输出文件> --out docs/screenshots/01-cli-verify.png --title "…"
```

## 两个采集脚本的边界（**不是产品代码**）

- `scripts/capture_panel.py` —— 只做「打开面板、点、截图」三件事，产物落到本目录。
  它不 mock 任何东西：面板是真进程、数据是真库、点击走真的 HTTP 写入口。
  点不到主按钮就 `SystemExit(1)`，不产出「看起来对」的空图。
- `scripts/render_terminal.py` —— 把**真实命令的 stdout 文件**套一层终端外框渲染成 PNG。
  只改前景色，**不改一个字符**，所以图上每一行都是那台机器上真打出来的那行。

`scripts/open_orders_for_shot.py` 用产品自己的 `reconq.ledger.flow.open_discrepancy`
给本期差异开待归因处置单，参数全部取自台账里已有的事实（差额、证据行号），不造数。

## 与视觉目标的关系

`docs/concepts/visual-target.md` 是**设计意图**（配色、版式、信息层级），
本目录是**实测**。两者必然有差距：概念卡画的是六路齐绿、差异清零后的
「成形态」，实测截图停在「本期有 4 条差异、门禁 red」的中态 —— 因为真实
的一期账不会恰好是零差异那一刻，而那正是产品最该被人看见的样子。
**没有生成任何「事后效果图」来抹平这个差距。**
