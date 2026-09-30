"""Task 50 · 终端输出渲染成图（**采集辅助，不是产品代码**）。

把**真实命令的 stdout 文件**渲染成一张终端样式的 PNG。内容一字不改：
脚本只读文件、套一层终端外框（标题栏 + 等宽字体 + 深色底），不做任何
重排、截断补写或「美化」。所以图上每一行都是那台机器上真打出来的那行。

之所以要渲染而不是直接贴文本：README 的「实测证据」小节要放图，
而 GitHub 上没有终端截图附件的落点。

用法::

    python3 scripts/render_terminal.py --in /tmp/verify_full.txt \\
        --out docs/screenshots/01-cli-verify.png --title "reconq verify --period 2026-W36"
"""

from __future__ import annotations

import argparse
import html
import os
import sys

#: 终端外框模板。**刻意用最朴素的样式** —— 这不是设计稿，是一段输出的
#: 逐行转写，配色越接近系统默认终端，越不容易被误读成「做出来的界面」。
TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #1b1d23; font-family: Menlo, "SF Mono",
         "DejaVu Sans Mono", monospace; }}
  .win {{ width: {width}px; }}
  .bar {{ background: #2a2d36; color: #c8ccd4; font-size: 13px;
          padding: 9px 14px; border-bottom: 1px solid #12141a;
          display: flex; align-items: center; gap: 8px; }}
  .dot {{ width: 11px; height: 11px; border-radius: 50%; display: inline-block; }}
  .t {{ margin-left: 8px; opacity: .9; }}
  pre {{ margin: 0; padding: 14px 16px; color: #d7dae0; font-size: 13px;
         line-height: 1.42; white-space: pre-wrap; word-break: break-all; }}
  .ok {{ color: #7fd18a; }} .bad {{ color: #ef8a8a; }} .warn {{ color: #e5c07b; }}
  .dim {{ color: #6f7480; }} .cy {{ color: #6fc7d8; }} .hl {{ color: #e8d48a; }}
</style></head><body><div class="win">
  <div class="bar"><span class="dot" style="background:#ff5f57"></span>
    <span class="dot" style="background:#febc2e"></span>
    <span class="dot" style="background:#28c840"></span>
    <span class="t">{title}</span></div>
  <pre>{body}</pre>
</div></body></html>"""


def colorize(line: str) -> str:
    """按本产品自己的输出约定上色：✓/✗/·/错误码。

    只改**前景色**，不改一个字符 —— 上色是为了让「哪几条没过」在图上一眼
    可辨，判据仍然只有原文里的 ✓ 与 ✗。
    """
    esc = html.escape(line)
    if line.startswith("  ✗") or " ✗ " in line:
        return f'<span class="bad">{esc}</span>'
    if line.startswith("  ✓"):
        return f'<span class="ok">{esc}</span>'
    if line.startswith("  ·") or line.startswith("  格式") or line.startswith("  存疑"):
        return f'<span class="dim">{esc}</span>'
    if line.startswith("契约 lint"):
        return f'<span class="warn">{esc}</span>'
    if "reconq:" in line or line.startswith("✗"):
        return f'<span class="bad">{esc}</span>'
    if line.startswith("===") or line.startswith("#"):
        return f'<span class="cy">{esc}</span>'
    if line.strip().startswith("✓ [") or line.strip().startswith("证据"):
        return f'<span class="dim">{esc}</span>'
    return esc


def render(text: str, title: str, width: int) -> str:
    body = "\n".join(colorize(line.rstrip("\n")) for line in text.splitlines())
    return TEMPLATE.format(title=html.escape(title), body=body, width=width)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把真实 stdout 渲染成终端图")
    ap.add_argument("--in", dest="src", required=True, help="真实命令输出文件")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--keep-lines", type=int, default=0,
                    help="只渲染前 N 行（0 = 全文）")
    args = ap.parse_args(argv)

    with open(args.src, encoding="utf-8") as fh:
        text = fh.read()
    if args.keep_lines:
        lines = text.splitlines()
        kept = lines[: args.keep_lines]
        if len(lines) > args.keep_lines:
            kept.append(f"…（后续 {len(lines) - args.keep_lines} 行略，完整输出见 README 正文）")
        text = "\n".join(kept)

    page = render(text, args.title or os.path.basename(args.src), args.width)
    tmp = args.out + ".html"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(page)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        pg = browser.new_page(viewport={"width": args.width, "height": 900},
                              device_scale_factor=2)
        pg.goto("file://" + os.path.abspath(tmp))
        pg.wait_for_timeout(400)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        pg.locator(".win").screenshot(path=args.out)
        browser.close()
    os.remove(tmp)
    print(f"[render] 写入 {args.out}（{os.path.getsize(args.out)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
