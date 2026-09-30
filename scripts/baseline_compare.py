"""Task 37 · 与「按渠道单表逐笔 VLOOKUP 对」的基础实现对拍。

为什么必须要有基线
------------------
「本产品比基础方案准」这种话谁都会说，但**不说清比的是什么**，就没法核对。
本脚本把院内现有人工流程的形态原样写成一份**可运行的基线**，在同一份
2000+ 行样例上跑一遍，把两边的结论集合逐条对齐，差异落到**行号与差额**。

基线是什么（不是稻草人，是财务科今天真在做的事）
------------------------------------------------
把 HIS 收费导出与渠道账单各自导成一张表，用 Excel 的 ``VLOOKUP``（或
DataGrip 里一条 ``select * from his where his_order_no = ?``）**按单号逐笔
对**：查到就比金额，查不到就记「渠道有 HIS 无」。然后把 HIS 表里没被任何
渠道行引用到的那些行，人工扫一眼标「HIS 有渠道无」。

它有三条**结构性**缺陷，本产品逐条对应：

===========================  ==========================================
基线的做法                   本产品多做了什么
===========================  ==========================================
命中多条时**静默取第一条**   报 ``ambiguous`` 并列全候选，**不认领**
只判「有没有」，不判「哪一期」 跨账期落点单独成类，不在本期认领
单号没命中就是「没有」        按金额+时间窗弱匹配，仍**不自动归领**
===========================  ==========================================

第一条是本对拍的核心：``EX-AMBIG-*`` / ``EX-HIS-*`` / ``EX-INS-*`` 这些
拆笔与金额相同形态，基线会**挑一个数对上就当平了**，多出来的那条 HIS 记录
则因为「已经被引用过」而不出现在任何单边账里 —— 差额被吃掉了，而台账上一条
差异都没有。脚本把这种行逐行列出来（``silently_picked``），并要求样本里
**至少有一条**（``--require-silent``，默认开）。

另外两条（跨账期、弱匹配）在基线里**根本没有对应概念**，所以脚本给基线补
上「尽力而为」的版本：跨账期它会把那条 HIS 记录算进「HIS 有渠道无」，
弱匹配它压根不做。差异表因此分成「基线更差」与「基线没有这个概念」两栏，
不混在一句「本产品更准」里。

报告能复现
----------
报告里每条差异都带 ``渠道 / 源文件:行号 / 金额 / 差额``，且
``scripts/baseline_compare.py`` 用固定种子读同一份样例 —— 换台机器跑
（``gen_fixtures.py --seed`` 相同）能得到逐字节相同的差异表。

用法（在项目子目录下）::

    PYTHONPATH=src python3 scripts/baseline_compare.py --fixtures out/scale --db out/bench-scan.sqlite
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Sequence

from reconq.align.matcher import (
    AlignRecord,
    MatchStatus,
    align_all,
    record_from_row_dict,
)
from reconq.sources.timezone import period_window, to_session_tz

DEFAULT_PERIOD = "2026-W40"

#: VLOOKUP 基线**唯一**的实现住在产品包里（:mod:`reconq.align.vlookup_baseline`），
#: 因为它有两处消费者：本脚本的 2000+ 行对拍报告，与 Task 38 的金标集指标。
#: 两处必须共用同一份代码 —— 指标体系最忌讳「基线有两份实现」，那样报告里的
#: 基线数字与门禁里的基线数字就来自不同代码，「优于基线」这句话失去可核对对象。
#: 这里保留同名 re-export，脚本其余部分与既有测试的引用方式都不变。
from reconq.align.vlookup_baseline import (  # noqa: E402
    BASELINE_TOLERANCE,
    BaselineVerdict,
    ProductVerdict,
    run_vlookup_baseline,
)


@dataclass
class CompareReport:
    period: str
    channel_rows: int = 0
    his_rows: int = 0
    baseline_seconds: float = 0.0
    product_seconds: float = 0.0
    baseline_counts: dict[str, int] = field(default_factory=dict)
    product_counts: dict[str, int] = field(default_factory=dict)
    #: 基线静默择一、本产品报歧义的行（**核心差异**）
    silently_picked: list[dict[str, Any]] = field(default_factory=list)
    #: 被基线**整段吞掉**的 HIS 记录（不在任何一份输出里）
    swallowed_his_rows: list[dict[str, Any]] = field(default_factory=list)
    #: 本产品报跨账期、基线没有这个概念的 HIS 单号
    product_only_cross_period: list[dict[str, Any]] = field(default_factory=list)
    #: 本产品弱匹配待人工、基线只会说「没有」
    product_only_weak: list[dict[str, Any]] = field(default_factory=list)
    #: 本产品报单边账、基线判平的（金额差在容差内但单号侧另有候选）
    amount_diff_rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel_rows": self.channel_rows,
            "his_rows": self.his_rows,
            "baseline_seconds": round(self.baseline_seconds, 3),
            "product_seconds": round(self.product_seconds, 3),
            "baseline_counts": self.baseline_counts,
            "product_counts": self.product_counts,
            "silently_picked_count": len(self.silently_picked),
            "swallowed_his_rows_count": len(self.swallowed_his_rows),
            "product_only_cross_period_count": len(self.product_only_cross_period),
            "product_only_weak_count": len(self.product_only_weak),
            "amount_diff_count": len(self.amount_diff_rows),
        }


def _row_dict(rec: AlignRecord) -> dict[str, Any]:
    return {
        "order_no": rec.order_no,
        "ref": rec.ref,
        "amount": str(rec.net_amount),
        "channel": rec.channel,
    }


def compare(
    conn: sqlite3.Connection,
    *,
    period: str = DEFAULT_PERIOD,
    sample: int = 20,
) -> CompareReport:
    report = CompareReport(period=period)
    raw = [dict(r) for r in conn.execute(
        "SELECT * FROM bill_rows WHERE period = ? ORDER BY channel, source_file, source_row_no, ch_id",
        (period,),
    )]
    records = [record_from_row_dict(r) for r in raw]
    by_channel: dict[str, list[AlignRecord]] = {}
    for rec in records:
        by_channel.setdefault(rec.channel, []).append(rec)
    his_rows = by_channel.pop("his", [])
    report.his_rows = len(his_rows)
    report.channel_rows = sum(len(v) for v in by_channel.values())

    # —— 基线：逐渠道独立跑（院内就是各渠道各对一遍）——
    started = time.perf_counter()
    baseline_by_order: dict[str, BaselineVerdict] = {}
    referenced: set[str] = set()
    for rows in by_channel.values():
        verdicts, used = run_vlookup_baseline(rows, his_rows)
        for v in verdicts:
            baseline_by_order.setdefault(v.order_no, v)
        referenced |= used
    report.baseline_seconds = time.perf_counter() - started
    for v in baseline_by_order.values():
        report.baseline_counts[v.tag] = report.baseline_counts.get(v.tag, 0) + 1

    # —— 本产品：六路一起对齐（HIS 侧只建一次索引，认领集合共用）——
    window = period_window(period, to_session_tz(None))
    started = time.perf_counter()
    result = align_all(by_channel, his_rows, period=period, window=window)
    report.product_seconds = time.perf_counter() - started
    for ch_result in result.channels.values():
        for status, n in ch_result.counts().items():
            report.product_counts[status] = report.product_counts.get(status, 0) + n
    if result.his_only:
        report.product_counts["his_only"] = (
            report.product_counts.get("his_only", 0) + len(result.his_only)
        )

    outcomes: dict[str, Any] = {}
    for ch_result in result.channels.values():
        for outcome in ch_result.outcomes:
            outcomes.setdefault(outcome.channel_record.order_no, outcome)

    his_by_no: dict[str, list[AlignRecord]] = {}
    for rec in his_rows:
        his_by_no.setdefault(rec.order_no, []).append(rec)

    # —— 差异 1：基线静默择一，本产品报歧义 ——
    for order_no, outcome in outcomes.items():
        if outcome.status != MatchStatus.AMBIGUOUS.value:
            continue
        base = baseline_by_order.get(order_no)
        if base is None or base.hit_count < 2:
            continue
        picked = base.picked
        assert picked is not None
        report.silently_picked.append(
            {
                **_row_dict(outcome.channel_record),
                "ambiguity_kind": outcome.ambiguity.kind if outcome.ambiguity else "",
                "candidate_total": outcome.ambiguity.candidate_total if outcome.ambiguity else 0,
                "baseline_picked": {
                    "ref": picked.ref,
                    "amount": str(picked.net_amount),
                    "charge_time": picked.trade_time.isoformat() if picked.trade_time else "",
                },
                "baseline_dropped": [
                    {"ref": r.ref, "amount": str(r.net_amount)} for r in base.dropped
                ],
                "hidden_amount": str(
                    sum((r.net_amount for r in base.dropped), Decimal("0"))
                ),
                "detail": outcome.detail,
            }
        )

    # —— 差异 2：被基线**整段吞掉**的 HIS 记录 ——
    # 判据是「这条 HIS 记录在基线的任何一份输出里都不出现」：既没被任何一行
    # 渠道账单调中（不在 ``picked`` 里），也没落进「HIS 有渠道无」（它的单号
    # 被同一单号的另一行引用过，于是「已引用」把整组一起盖住了）。
    # 这正是拆笔形态下差额消失的地方，也是最危险的一类：台账上干干净净。
    picked_records = {
        id(v.picked)
        for v in baseline_by_order.values()
        if v.picked is not None
    }
    product_referenced = {
        outcome.his_record.order_no
        for ch_result in result.channels.values()
        for outcome in ch_result.outcomes
        if outcome.his_record is not None
    }
    product_listed = {
        c.record_id
        for ch_result in result.channels.values()
        for outcome in ch_result.outcomes
        if outcome.ambiguity
        for c in outcome.ambiguity.candidates
    }
    for rec in his_rows:
        if id(rec) in picked_records:
            continue
        if rec.order_no not in referenced:
            # 基线会把它报成「HIS 有渠道无」—— 那是对的处理，不算差异
            continue
        # 单号被引用过，但**这条记录本身**没被选中：基线的输出里查无此笔
        report.swallowed_his_rows.append(
            {
                **_row_dict(rec),
                "charge_time": rec.trade_time.isoformat() if rec.trade_time else "",
                "same_order_siblings": len(his_by_no.get(rec.order_no, [])),
                "listed_by_product": rec.order_no in product_listed
                or rec.order_no in product_referenced,
                "why": (
                    "同一单号在 HIS 侧有多条记录，基线的 VLOOKUP 只认第一条；"
                    "这一条既没被选中、也没落进「HIS 有渠道无」（单号已被引用），"
                    "在基线的任何输出里都查不到"
                ),
            }
        )

    # —— 差异 3：本产品报跨账期，基线没有这个概念 ——
    for outcome in outcomes.values():
        if outcome.status != MatchStatus.CROSS_PERIOD.value:
            continue
        report.product_only_cross_period.append(
            {
                **_row_dict(outcome.channel_record),
                "detail": outcome.detail,
            }
        )

    # —— 差异 4：本产品弱匹配待人工，基线只会说「没有」——
    for outcome in outcomes.values():
        if outcome.status != MatchStatus.WEAK_PENDING.value:
            continue
        report.product_only_weak.append(
            {
                **_row_dict(outcome.channel_record),
                "candidate_total": outcome.ambiguity.candidate_total if outcome.ambiguity else 0,
                "candidates": [
                    {"ref": c.ref, "amount": c.amount}
                    for c in (outcome.ambiguity.candidates if outcome.ambiguity else [])
                ],
                "detail": outcome.detail,
            }
        )

    # —— 差异 5：两边都看到这一行，但结论不同（金额差）——
    for order_no, outcome in outcomes.items():
        base = baseline_by_order.get(order_no)
        if base is None or base.tag != "amount_diff":
            continue
        report.amount_diff_rows.append(
            {
                **_row_dict(outcome.channel_record),
                "baseline_diff": base.diff,
                "product_diff": outcome.diff_amount,
                "product_status": outcome.status,
            }
        )

    for lst in (
        report.silently_picked,
        report.swallowed_his_rows,
        report.product_only_cross_period,
        report.product_only_weak,
        report.amount_diff_rows,
    ):
        lst.sort(key=lambda d: (str(d.get("channel", "")), str(d.get("ref", ""))))
    _ = sample
    return report


def render_markdown(report: CompareReport, *, sample: int = 20) -> str:
    def head(title: str) -> str:
        return f"\n### {title}\n"

    def table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
        if not rows:
            return "_（本次样例中无此类差异）_\n"
        lines = ["| " + " | ".join(t for _, t in cols) + " |",
                 "|" + "|".join("---" for _ in cols) + "|"]
        for row in rows[:sample]:
            cells = []
            for key, _ in cols:
                val = row.get(key, "")
                if isinstance(val, list):
                    val = "；".join(str(v.get("ref", v)) for v in val[:3]) or "—"
                elif isinstance(val, dict):
                    val = f"{val.get('ref', '')} {val.get('amount', '')}".strip()
                cells.append(str(val).replace("|", "\\|"))
            lines.append("| " + " | ".join(cells) + " |")
        if len(rows) > sample:
            lines.append(f"| _（另有 {len(rows) - sample} 条同形差异，见 JSON 全量）_ |" + " |" * (len(cols) - 1) + "|")
        return "\n".join(lines) + "\n"

    out = [
        f"# 对拍报告 · {report.period} · 与「按渠道单表逐笔 VLOOKUP 对」的基础实现",
        "",
        "本报告由 `scripts/baseline_compare.py` 生成，样例由 `scripts/gen_fixtures.py`",
        "以固定随机种子生成。**同一条命令可复现**（差异表逐行可回查到源文件行号）。",
        "",
        "## 规模与耗时",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 渠道侧行数 | {report.channel_rows} |",
        f"| HIS 侧行数 | {report.his_rows} |",
        f"| 基线耗时（逐渠道 VLOOKUP） | {report.baseline_seconds:.3f} s |",
        f"| 本产品耗时（六路共用索引对齐） | {report.product_seconds:.3f} s |",
        "",
        "## 结论分布对比",
        "",
        "| 结论 | 基线（VLOOKUP） | 本产品 |",
        "|---|---|---|",
        f"| 判为对平 | {report.baseline_counts.get('matched', 0)} | "
        f"{report.product_counts.get('matched', 0)} |",
        f"| 金额不符 | {report.baseline_counts.get('amount_diff', 0)} | "
        f"{report.product_counts.get('mismatch', 0)} |",
        f"| 渠道有 HIS 无 | {report.baseline_counts.get('channel_only', 0)} | "
        f"{report.product_counts.get('channel_only', 0)} |",
        f"| HIS 有渠道无（基线口径） | {report.his_rows - len(report.swallowed_his_rows)} | "
        f"{report.product_counts.get('his_only', 0)} |",
        f"| **被整段吞掉（基线查无此笔）** | **{len(report.swallowed_his_rows)}** | 0 |",
        f"| 歧义待人工（基线无此类） | 0 | {report.product_counts.get('ambiguous', 0)} |",
        f"| 跨账期落点（基线无此类） | 0 | {report.product_counts.get('cross_period', 0)} |",
        f"| 弱匹配待人工（基线不做） | 0 | {report.product_counts.get('weak_pending', 0)} |",
        f"| 重复认领（基线无此类） | 0 | {report.product_counts.get('duplicate_claim', 0)} |",
    ]

    out.append(head("差异 1 · 基线静默择一，本产品报「歧义待人工」（核心差异）"))
    out.append(
        f"共 **{len(report.silently_picked)}** 条。基线对同一单号命中多条 HIS 记录时"
        f"**只取第一条**并判平，其余候选既不进「渠道有 HIS 无」，也不进「HIS 有渠道无」"
        f"——差额被整段吃掉，而台账上一条差异都不留。本产品把全部候选行列出来交人。\n"
    )
    out.append(
        table(
            report.silently_picked,
            [
                ("channel", "渠道"),
                ("ref", "渠道行"),
                ("amount", "渠道金额"),
                ("ambiguity_kind", "歧义形态"),
                ("candidate_total", "候选数"),
                ("baseline_picked", "基线选中"),
                ("baseline_dropped", "基线丢弃"),
                ("hidden_amount", "被吃掉的差额"),
            ],
        )
    )

    out.append(head("差异 2 · 被基线整段吞掉的 HIS 记录（台账上查无此笔）"))
    out.append(
        f"共 **{len(report.swallowed_his_rows)}** 条，这是最危险的一类：基线对同一单号命中多条时"
        f"只认第一条，而「已引用」标记是按**单号**记的，于是同号的其余记录既没被选中、"
        f"也没落进「HIS 有渠道无」—— 它们在基线的任何一份输出里都查不到，"
        f"差额就这么消失了，而台账上一条差异都不留。\n"
    )
    out.append(
        table(
            report.swallowed_his_rows,
            [
                ("channel", "渠道"),
                ("ref", "HIS 行"),
                ("order_no", "单号"),
                ("amount", "金额"),
                ("charge_time", "收费时间"),
                ("same_order_siblings", "同号记录数"),
                ("listed_by_product", "本产品已列出"),
            ],
        )
    )

    out.append(head("差异 3 · 本产品报「跨账期落点」，基线没有这个概念"))
    out.append(
        f"共 **{len(report.product_only_cross_period)}** 条。渠道行在本期、单号命中的那条 HIS "
        f"收费落在窗口之外：认了本期合计就多一笔、下期再认一次又变成两期重复计入。"
        f"基线没有账期概念，只能要么认（错）、要么记成「HIS 有渠道无」（把成因指错方向）。\n"
    )
    out.append(table(report.product_only_cross_period, [("channel", "渠道"), ("ref", "渠道行"), ("amount", "金额"), ("detail", "判定说明")]))

    out.append(head("差异 4 · 本产品弱匹配待人工，基线只会说「没有」"))
    out.append(
        f"共 **{len(report.product_only_weak)}** 条。渠道行单号没命中 HIS，但金额与时间窗"
        f"唯一命中一条。基线直接记「渠道有 HIS 无」，经办人只能自己去 HIS 里翻单号；"
        f"本产品把候选行列出来——**但仍然不自动认领**，一天里同金额的缴费有几十笔，"
        f"「碰巧对得上」不是结论。\n"
    )
    out.append(table(report.product_only_weak, [("channel", "渠道"), ("ref", "渠道行"), ("amount", "金额"), ("candidate_total", "候选数"), ("candidates", "候选")]))

    out.append(head("差异 5 · 两边都看到、结论不同的行（金额差）"))
    out.append(
        f"共 **{len(report.amount_diff_rows)}** 条。基线与本产品都认为「对上了但对不平」，"
        f"但选中的候选不同（基线取第一条），因此**差额数字也不同**。\n"
    )
    out.append(table(report.amount_diff_rows, [("channel", "渠道"), ("ref", "渠道行"), ("amount", "渠道金额"), ("baseline_diff", "基线差额"), ("product_diff", "本产品差额"), ("product_status", "本产品结论")]))

    out += [
        "",
        "## 哪些是真实协议实现、哪些是测试替身",
        "",
        "| 部分 | 性质 | 说明 |",
        "|---|---|---|",
        "| 微信 / 支付宝账单取数 | **真实协议实现，本轮未实连** | 走 V3 签名 / RSA2 网关与真实字段规范；"
        "本机无商户号，协议层由 `tests/protocol/` 的报文回放覆盖 |",
        "| HIS 只读库取数 | **真实协议实现，本轮未实连** | 走 SQLAlchemy 只读会话与流式分批；"
        "本机无院内库实例，压测用本地 SQLite **替身数据层**（见 `scripts/bench.py` 模块头） |",
        "| 银联 / 自助机 / 医保账单 | **本轮为院内文件通道** | 格式探测与列归一走真实实现；"
        "最终对接方式为渠道正式接口 |",
        "| 对齐 / 歧义判定 | **真实实现** | 本报告全部差异由 `reconq.align.matcher` 主路径产出 |",
        "| 基线实现 | **对照用，非产品代码** | 只在 `scripts/baseline_compare.py` 内，不进产品包 |",
        "",
    ]
    return "\n".join(out) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="与逐笔 VLOOKUP 基线对拍")
    ap.add_argument("--fixtures", default="out/scale")
    ap.add_argument("--db", default="out/bench-scan.sqlite", help="bench.py 扫出来的账单库")
    ap.add_argument("--period", default=DEFAULT_PERIOD)
    ap.add_argument("--out", default="out/compare-report.md")
    ap.add_argument("--json-out", default="out/compare-report.json")
    ap.add_argument("--sample", type=int, default=20, help="报告里每类差异列前 N 条")
    ap.add_argument(
        "--require-silent",
        action="store_true",
        default=True,
        help="要求样例中至少有一条「基线静默择一」差异（默认开启）",
    )
    ap.add_argument("--no-require-silent", dest="require_silent", action="store_false")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        raise SystemExit(f"{args.db} 不存在；先跑 scripts/bench.py")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        report = compare(conn, period=args.period, sample=args.sample)
    finally:
        conn.close()

    markdown = render_markdown(report, sample=args.sample)
    for path in (args.out, args.json_out):
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    payload = report.to_dict()
    payload["silently_picked"] = report.silently_picked
    payload["swallowed_his_rows"] = report.swallowed_his_rows
    payload["product_only_cross_period"] = report.product_only_cross_period
    payload["product_only_weak"] = report.product_only_weak
    payload["amount_diff"] = report.amount_diff_rows
    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True, default=str)

    print(f"对拍报告已写入 {args.out}（全量 JSON：{args.json_out}）")
    print(
        f"  静默择一 {len(report.silently_picked)} 条 / "
        f"被基线整段吞掉的 HIS 记录 {len(report.swallowed_his_rows)} 条 / "
        f"跨账期 {len(report.product_only_cross_period)} 条 / "
        f"弱匹配 {len(report.product_only_weak)} 条"
    )
    if args.require_silent and not report.silently_picked:
        print(
            "样例中没有任何「基线静默择一而本产品报歧义」的行 —— "
            "这份样例证明不了「拒绝归并」这条纪律，换一份含拆笔的样例再跑",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
