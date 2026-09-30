"""审计事件链的端到端演示（Task 45）。

跑一遍财务科在面板上点开一条差异时会走的完整路径：

    开链 → 建差异 → 调模型取成因 → 人工确认 → 复核 → 导出
    ↓
    按账期切片读回（证明没有全量扫描）
    ↓
    给出这一条差异的完整时序
    ↓
    重算校验：链完整
    ↓
    就地篡改中间一条 → 校验报出「第几条」

用法::

    PYTHONPATH=src python3 scripts/demo_audit_events.py

**不连任何外部系统**：全部在一个临时目录里，事件文件与索引跑完即弃。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from reconq.audit.query import (  # noqa: E402
    _event_chain_hash,
    append_event,
    chain_anchor,
    discrepancy_timeline,
    open_chain,
    query_events,
    verify_chain,
)

PERIOD = "2026-W36"
OTHER = "2026-W37"
KEY = f"{PERIOD}|wechat|taxonomy.missing:EX-9"
OTHER_KEY = f"{OTHER}|alipay|taxonomy.missing:EX-1"


def _rule(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    audit_dir = tempfile.mkdtemp(prefix="reconq-audit-")
    try:
        _rule("1. 铺两期事件链（另一期铺 200 条，取一期时不该读到它）")
        genesis = open_chain(audit_dir, PERIOD, actor="财务科·张")
        open_chain(audit_dir, OTHER, actor="财务科·张")
        for index in range(200):
            append_event(audit_dir, OTHER, "CHANNEL_FETCHED", actor="reconq",
                         payload={"index": index})

        append_event(audit_dir, PERIOD, "DIFF_CREATED", actor="reconq",
                     discrepancy_key=KEY, payload={"lines": 3, "amount": "66.00"})
        append_event(audit_dir, PERIOD, "MODEL_CALLED", actor="reconq",
                     discrepancy_key=KEY,
                     payload={"model": "院内推理端点", "elapsed_ms": 812,
                              "confidence": "0.72", "cause_code": "channel_late_posting"})
        append_event(audit_dir, PERIOD, "DECIDED", actor="经办·李", discrepancy_key=KEY,
                     payload={"disposition": "已确认", "note": "导出方漏传，已联系渠道补传"})
        append_event(audit_dir, PERIOD, "REVIEWED", actor="主管·王", discrepancy_key=KEY,
                     payload={"ok": True})
        append_event(audit_dir, PERIOD, "EXPORTED", actor="财务科·张",
                     discrepancy_key=KEY, payload={"fmt": "md"})
        print(f"事件文件：{os.path.join(audit_dir, 'events.jsonl')}")
        print(f"本期锚点：{chain_anchor(audit_dir, PERIOD)}")

        _rule("2. 按账期切片读回 —— 只读本期，不读那 200 条")
        found = query_events(audit_dir, PERIOD)
        log_size = os.path.getsize(os.path.join(audit_dir, "events.jsonl"))
        for event in found:
            print(f"  #{event.seq}  {event.at}  {event.event_zh:<16} {event.actor or '—'}")
        print()
        print(f"  本期 {len(found)} 条 · 读盘 {found.bytes_scanned} 字节 / 全库 {log_size} 字节"
              f"（走索引：{found.used_index}）")
        assert found.used_index and found.bytes_scanned < log_size, "切片没走索引"

        _rule("3. 这一条差异的完整时序（面板右栏）")
        for event in discrepancy_timeline(audit_dir, KEY):
            print(f"  {event.event_zh:<16} {event.actor or '—':<10} {event.at}")

        _rule("4. 重算校验：链完整")
        verdict = verify_chain(audit_dir, PERIOD)
        print(f"  ok={verdict.ok} 已核 {verdict.checked} 环 · 锚点一致={verdict.anchor_ok}")

        _rule("5. 就地篡改本期的第 3 条 → 校验要指出是第几条")
        log = os.path.join(audit_dir, "events.jsonl")
        # 事件文件是**两期交错**的（真实并发扫描就是这样），所以按「第几条」
        # 定位而不是按「第几行」—— 按行号改会改到另一期头上，而
        # verify_chain(PERIOD) 只看得到本期的链，会一路报「链完整」。
        import json  # noqa: E402

        lines = open(log, encoding="utf-8").readlines()
        target = next(
            i for i, raw in enumerate(lines)
            if json.loads(raw)["period"] == PERIOD and json.loads(raw)["seq"] == 3
        )
        lines[target] = lines[target].replace(
            '"cause_code":"channel_late_posting"',
            '"cause_code":"patient_self_pay"',
        )
        open(log, "w", encoding="utf-8").writelines(lines)
        broken = verify_chain(audit_dir, PERIOD)
        print(f"  改的是文件第 {target + 1} 行（本期第 3 条）")
        print(f"  ok={broken.ok} 断点 seq={broken.broken_seq} · {broken.reason}")
        assert not broken.ok and broken.broken_seq == 3, "篡改没被检出"

        _rule("6. 删掉开链那条、重接成自洽的链 → 只有锚点能看出来")
        kept = [line for line in lines if json.loads(line)["period"] == PERIOD][1:]
        others = [line for line in lines if json.loads(line)["period"] != PERIOD]
        prev = "0" * 64
        rows = []
        for index, raw in enumerate(kept, start=1):
            data = json.loads(raw)
            data["seq"] = index
            data["prev_hash"] = prev
            data["chain_hash"] = _event_chain_hash(
                index, data["period"], data["event_type"], data["at"],
                data.get("actor", ""), data.get("discrepancy_key", ""),
                data.get("payload") or {}, prev,
            )
            prev = data["chain_hash"]
            rows.append(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n")
        open(log, "w", encoding="utf-8").writelines(rows + others)
        from reconq.audit.query import rebuild_index  # noqa: E402

        rebuild_index(audit_dir)
        again = verify_chain(audit_dir, PERIOD)
        print(f"  链身重算全过（自洽），但：ok={again.ok} 锚点一致={again.anchor_ok}")
        print(f"  {again.reason}")
        assert not again.ok and not again.anchor_ok, "锚点没起作用"
        return 0
    finally:
        shutil.rmtree(audit_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
