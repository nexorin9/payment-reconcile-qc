"""Task 29 · 结构化输出六层校验 —— 端到端演示脚本。

跑法（在项目子目录下）::

    PYTHONPATH=src python3 scripts/demo_schema_validation.py

演示的是**新链路**的完整一段：一份带围栏与前后散文的模型返回 →
稳健提取 → 六层校验 → 只把合格的落库、被拒的按层计数 → 重跑幂等。

脚本里的账单行是脱敏占位（单号形如 EX-66、金额是演示值），
不含任何真实患者或真实交易明细。
"""

from __future__ import annotations

import json

from reconq.gateway.schema import parse_candidate_text
from reconq.store.attribution_store import (
    init_attribution_schema,
    layer_failure_counts,
    list_drafts,
    save_drafts,
)
from reconq.store.schema import connect, init_schema

PERIOD = "2026-W36"
CHANNEL = "wechat"
KEY = "SINGLE_SIDED:EX-66"
ALLOWED_REFS = ("wechat_bill.csv:3", "charge_record.csv:17")
VOCAB = ("跨日切分重复计入", "导出方漏传", "退费红冲未在渠道账单反映")


def _draft(**overrides):
    payload = {
        "cause": "跨日切分重复计入",
        "confidence": 0.82,
        "evidence_refs": ["wechat_bill.csv:3"],
        "explanation_zh": "该笔收费时间 2026-09-07 00:00 落在账期窗口之外，很可能已计入下一期。",
    }
    payload.update(overrides)
    return payload


def main() -> None:
    text = (
        "分析如下：\n```json\n"
        + json.dumps(
            {
                "drafts": [
                    _draft(confidence=0.82),
                    _draft(cause="导出方漏传", confidence=0.61, evidence_refs=["charge_record.csv:17"]),
                    _draft(cause="跨日切分重复计入", confidence=85),  # 置信度层
                    _draft(cause="大概就是导出方的问题", confidence=0.3),  # 成因字典层
                    _draft(cause="退费红冲未在渠道账单反映", evidence_refs=["wechat_bill.csv:99"]),
                    _draft(cause="导出方漏传", explanation_zh=""),  # 说明层
                ]
            },
            ensure_ascii=False,
        )
        + "\n```\n以上。"
    )

    print("--- 模型返回原文（前两行：前后带散文 + json 围栏） ---")
    for line in text.splitlines()[:2]:
        print("  " + (line[:108] + "…" if len(line) > 108 else line))
    print()

    report = parse_candidate_text(text, allowed_refs=ALLOWED_REFS, vocabulary=VOCAB)
    print(f"提取 + 六层校验：给了 {report.raw_count} 条 → 收下 {len(report.drafts)} 条，拒 {len(report.rejected)} 条")
    print(f"按层拒因：{report.failures_by_layer()}")
    for error in report.rejected:
        print(f"  第 {error.index + 1} 条 [{error.layer.value}] {error.code}")
        print(f"      {error.detail[:88]}")
    print()

    conn = connect(":memory:")
    init_schema(conn)
    init_attribution_schema(conn)
    outcome = save_drafts(
        conn,
        report.drafts,
        period=PERIOD,
        channel=CHANNEL,
        conclusion_key=KEY,
        run_id="RUN-T29",
        model_name="院内推理端点",
        rejected=report.rejected,
    )
    print(f"落库：{outcome.to_dict()}")
    for row in list_drafts(conn, PERIOD):
        print(
            f"  rank{row['rank']} {row['cause']}  置信度={row['confidence_text']}"
            f"  证据={row['evidence_refs']}  状态={row['disposition']}"
        )
    print(f"拒因表按层计数：{layer_failure_counts(conn, PERIOD)}")
    print()

    print("--- 幂等：同键重跑 ---")
    again = save_drafts(
        conn,
        report.drafts,
        period=PERIOD,
        channel=CHANNEL,
        conclusion_key=KEY,
        run_id="RUN-T29",
        model_name="院内推理端点",
        rejected=report.rejected,
    )
    print(
        f"重跑：{again.to_dict()}  表内草稿仍 {len(list_drafts(conn, PERIOD))} 行"
        f"  拒因计数仍 {layer_failure_counts(conn, PERIOD)}"
    )
    print()

    print("--- 未配受控字典时：该层记「跳过」而不是「通过」 ---")
    loose = parse_candidate_text(
        json.dumps({"drafts": [_draft(cause="随便一个自由文本成因")]}, ensure_ascii=False),
        allowed_refs=ALLOWED_REFS,
    )
    print(f"  收下 {len(loose.drafts)} 条（未拒），逐层状态：" + ", ".join(
        f"{o.layer.value}={o.status}" for o in loose.outcomes
    ))


if __name__ == "__main__":
    main()
