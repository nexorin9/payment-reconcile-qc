"""Task 50 · 真实浏览器面板截图采集（**采集脚本，不是产品代码**）。

这个脚本只做一件事：把**真实运行中的** ``reconq serve`` 面板用真实浏览器
（Playwright 驱动本机 Google Chrome）打开、点、截屏，产物落到
``docs/screenshots/``。

它刻意不 mock 任何东西：

* 面板是真进程在跑（``reconq serve --db out/shot-scan.sqlite``）；
* 数据是真的 SQLite 账单库里的六路真实规模落库结果（Task 37 样例）；
* 第 3 张图是真的在浏览器里点中栏一行 → 填操作人 → 点「确认」，
  走真实的 ``POST /api/discrepancy/{key}/decide``，状态真的从
  **待归因** 推进到 **待确认**，截的是**状态已经变了**的界面；
* 脚本不吞失败：点不到按钮就 ``SystemExit(1)``，不产出「看起来对」的空图。

用法（项目子目录下，服务已起）::

    PYTHONPATH=src python3 scripts/capture_panel.py --out docs/screenshots
"""

from __future__ import annotations

import argparse
import os
import sys
import time

BASE = os.environ.get("RECONQ_PANEL_URL", "http://127.0.0.1:8788")

#: 第 3 张图点哪一条：微信「本期退款金额须为零」—— 有金额、有证据行号，
#: 差异最像现场真实发生的那一类。:attr:`DiffItem.check_path` 同名。
TARGET_CHECK_PATH = "rows.refund_sum.zero"
OPERATOR = "财务科-张"
CONCLUSION = "本期退款 47937.24 元已与财务科逐笔核到银行回单，逐笔可追。"


def _wait(page, path: str = "/") -> None:
    page.goto(BASE + path, wait_until="networkidle")
    page.wait_for_selector(".diff-row", timeout=15_000)
    time.sleep(1.0)


def capture(out_dir: str) -> list[str]:
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1680, "height": 1150})

        # —— 02：扫描后的三栏总览（六路主态 + 差异台账 + 待办）——
        _wait(page)
        target = os.path.join(out_dir, "02-panel-scan.png")
        page.screenshot(path=target)
        written.append(target)

        # —— 03：真点一条差异 → 真填处置 → 真点「确认」——
        row_sel = f'.diff-row[data-key$="{TARGET_CHECK_PATH}"]'
        page.wait_for_selector(row_sel, timeout=10_000)
        page.click(row_sel)
        page.wait_for_selector("#detail .actions input", timeout=10_000)
        time.sleep(0.8)

        inputs = page.query_selector_all("#detail .actions input")
        if len(inputs) < 2:
            raise SystemExit(
                f"[capture] 右栏处置区只找到 {len(inputs)} 个输入框，"
                "与「操作人 + 处置结论」两格不符，中止采集"
            )
        inputs[0].fill(OPERATOR)
        inputs[1].fill(CONCLUSION)

        # 按钮文案由服务端 ACTION_ZH 给（待归因 → 「提交处置结论」），
        # 前端不另写一份中文，所以这里按 primary 类选，不写死文案。
        btn = page.query_selector("#detail .actions button.primary")
        if btn is None:
            raise SystemExit("[capture] 处置区没有主按钮，中止采集（不产出假图）")
        btn.click()
        page.wait_for_timeout(1500)
        _wait(page)
        # 点完再点回同一条，右栏要显示**推进后**的状态
        page.click(row_sel)
        page.wait_for_selector("#detail .kv", timeout=10_000)
        time.sleep(1.0)
        target = os.path.join(out_dir, "03-panel-decision.png")
        page.screenshot(path=target)
        written.append(target)

        browser.close()
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="采集真实面板截图")
    ap.add_argument("--out", default="docs/screenshots")
    args = ap.parse_args(argv)
    try:
        written = capture(args.out)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[capture] 失败：{exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"[capture] 写入 {path}（{os.path.getsize(path)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
