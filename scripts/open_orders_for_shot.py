"""Task 50 · 为截图准备真实的待归因处置单（**采集辅助，不是产品代码**）。

面板右栏的「处置」按钮**只对已开单的差异渲染**（:func:`reconq.ledger.flow`
的约定：差异 ≠ 处置单，面板不替它开单）。而 CLI 目前没有「给某条差异开单」
的子命令 —— 开单这一步在主路径上由归因阶段落，本轮截图要拍的就是
「点完确认之后的样子」，所以这里用**产品自己的** :func:`open_discrepancy`
给本期真实差异开单，参数全部取自台账里已有的事实（差额、证据行号），
不造数、不改判定。

开单后由 :mod:`scripts.capture_panel` 在真实浏览器里点「确认」，
走真实的 ``POST /api/discrepancy/{key}/decide``，状态真的从
「待归因」推进到「待确认」。

用法::

    PYTHONPATH=src python3 scripts/open_orders_for_shot.py --db out/shot-scan.sqlite --period 2026-W36
"""

from __future__ import annotations

import argparse
import os
import sys

from reconq.engine.period import list_diff_items
from reconq.ledger.flow import open_discrepancy
from reconq.store.schema import connect


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="给本期差异开待归因处置单")
    ap.add_argument("--db", required=True)
    ap.add_argument("--period", required=True)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        items = list_diff_items(conn, args.period)
        if not items:
            print("本期没有差异，先跑一次 reconq scan", file=sys.stderr)
            return 1
        for item in items:
            evidence = []
            if item.source_file:
                for row_no in (item.first_row_no,):
                    if row_no:
                        evidence.append(f"{item.source_file}:{row_no}")
            disc = open_discrepancy(
                conn,
                period=item.period,
                channel=item.channel,
                check_path=item.check_path,
                order_no=item.order_no if hasattr(item, "order_no") else "",
                diff_amount=item.diff_amount,
                amount_known=bool(item.diff_amount),
                evidence_rows=evidence,
            )
            print(f"开单 {disc.key} → {disc.status}（差额 "
                  f"{item.diff_amount or '上游未给金额'}）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
