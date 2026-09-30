"""生成归因离线回放集（Task 34）。

跑法（在项目子目录下）::

    PYTHONPATH=src python3 tests/fixtures/attrib/generate_replay_set.py

产出 ``cases/*.json``：每条一个固定证据包 + 期望性质。**期望不是「模型该说
什么」，而是「这条差异的正确成因属于哪一档」** —— 现场没有标准答案，标准
答案是「归因落到受控字典的哪一桶」，那才是经办人签字前真正在核对的东西。

每条 case 的字段
----------------
``case_id``      稳定标识（含形态与渠道）
``name_zh``      中文名，回放报告里逐条显示
``channel``      渠道
``kind``         对齐器给出的差异形态
``bundle``       证据包 payload（**逐字节固定**，见
                 :func:`reconq.align.bundle.bundle_from_payload` 的说明）
``expect``       期望性质：
                   - ``cause_codes``：可接受的成因码集合（**集合不是单值** ——
                     一条差异的真实成因往往有两个都说得通的，判据是「落在
                     哪一桶」而不是「逐字等于哪一个」）
                   - ``required_refs``：Top-1 成因**必须**引用的行号。
                     不引用差异行却给出成因，等于凭印象归因。
                   - ``min_refs``：全部草稿合计至少要引用几行

为什么要把「期望」写成性质而不是逐字答案
--------------------------------------
模型措辞每次都不同，逐字比对只能评「像不像那句话」。而本产品要评的是
「Top-1 落没落在正确的成因桶里、依据行有没有引对」—— 后者才是
:mod:`reconq.align.causes` 聚合与 :mod:`reconq.align.verify_draft` 核对
真正在意的两件事。

回放集里**刻意包含一条 UNKNOWN**：成因判不出来时模型必须说
``undetermined``，而不是在十条里挑一条最像的。字典里没有「判不出来」
这一项时，模型会被迫猜，而「我判不出来」比「我判错了」贵得多 ——
前者让人去查证，后者让人签字。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from reconq.align.bundle import build_evidence_bundle  # noqa: E402
from reconq.align.matcher import (  # noqa: E402
    AlignRecord,
    align_channel,
    build_index,
)
from reconq.contracts.loader import load_contract  # noqa: E402
from reconq.sources.timezone import HOSPITAL_TZ, period_window  # noqa: E402

PERIOD = "2026-W36"
TZ = HOSPITAL_TZ
HERE = os.path.dirname(os.path.abspath(__file__))
CASES_DIR = os.path.join(HERE, "cases")
CONTRACTS = os.path.join(HERE, "..", "..", "..", "contracts")


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _chan(
    order_no: str,
    amount: str,
    *,
    channel: str = "wechat",
    time: str = "2026-09-02 10:00:00",
    row: int = 3,
    file: str = "",
    refund: str = "",
    trade_type: str = "",
    ch_id: int = 0,
) -> AlignRecord:
    return AlignRecord(
        channel=channel,
        period=PERIOD,
        order_no=order_no,
        amount=Decimal(amount),
        refund=Decimal(refund) if refund else None,
        trade_time=_t(time),
        trade_type=trade_type,
        source_file=file or f"{channel}_bill.csv",
        source_row_no=row,
        ch_id=ch_id,
    )


def _his(
    order_no: str,
    amount: str,
    *,
    time: str = "2026-09-02 10:00:00",
    row: int = 17,
    file: str = "charge_record.csv",
    refund: str = "",
    trade_type: str = "",
    department: str = "门诊收费室",
    settle_type: str = "微信",
    ch_id: int = 0,
) -> AlignRecord:
    return AlignRecord(
        channel="his",
        period=PERIOD,
        order_no=order_no,
        his_order_no=order_no,
        amount=Decimal(amount),
        refund=Decimal(refund) if refund else None,
        trade_time=_t(time),
        trade_type=trade_type,
        department=department,
        settle_type=settle_type,
        source_file=file,
        source_row_no=row,
        ch_id=ch_id,
    )


def _bundle(case_id: str, outcome: Any, channel: str) -> dict[str, Any]:
    contract = load_contract(
        os.path.join(CONTRACTS, f"{channel}.yml"), {"period": PERIOD}
    )
    return build_evidence_bundle(
        outcome, contract=contract, window=period_window(PERIOD, TZ)
    ).to_dict()


def _first(
    channel: str,
    chan_records: list[AlignRecord],
    his_records: list[AlignRecord],
    *,
    claimed: dict[int, str] | None = None,
) -> Any:
    index = build_index(his_records, period=PERIOD)
    result = align_channel(
        channel,
        chan_records,
        index,
        period=PERIOD,
        window=period_window(PERIOD, TZ),
        claimed=claimed,
    )
    return result.outcomes[0]


# ────────────────────── 12 条固定证据包 ──────────────────────


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # 1. 单边账：渠道有、HIS 查无此笔 → HIS 未落单 / 渠道延迟入账
    cases.append(
        {
            "case_id": "01-channel-only-his-missing",
            "name_zh": "单边账：渠道有、HIS 查无此笔",
            "channel": "wechat",
            "kind": "channel_only",
            "expect": {
                "cause_codes": ["his_missing_order", "channel_late_posting"],
                "required_refs": ["wechat_bill.csv:3"],
                "min_refs": 1,
            },
            "bundle": _bundle(
                "01",
                _first("wechat", [_chan("EX-ONLY-0001", "66.00")], []),
                "wechat",
            ),
        }
    )

    # 2. 弱匹配：单号对不上，靠金额 + 时间窗挂上的候选
    cases.append(
        {
            "case_id": "02-weak-match-pending",
            "name_zh": "弱匹配：单号缺失，仅金额与时间窗可依",
            "channel": "alipay",
            "kind": "weak_pending",
            "expect": {
                "cause_codes": ["his_missing_order", "merged_payment_allocation", "undetermined"],
                "required_refs": ["alipay_bill.csv:5"],
                "min_refs": 1,
            },
            "bundle": _bundle(
                "02",
                _first(
                    "alipay",
                    [_chan("EX-WEAK-0002", "88.00", channel="alipay", row=5, time="2026-09-03 14:20:00")],
                    [_his("EX-HIS-0002", "88.00", row=21, time="2026-09-03 14:05:00")],
                ),
                "alipay",
            ),
        }
    )

    # 3. 歧义：一码付拆多笔（金额守恒）
    cases.append(
        {
            "case_id": "03-ambiguous-split-payment",
            "name_zh": "歧义：一码付在 HIS 被拆成多笔",
            "channel": "wechat",
            "kind": "ambiguous",
            "expect": {
                "cause_codes": ["split_payment_unallocated"],
                "required_refs": ["wechat_bill.csv:3"],
                "min_refs": 2,
            },
            "bundle": _bundle(
                "03",
                _first(
                    "wechat",
                    [_chan("EX-SPLIT-0003", "128.00")],
                    [
                        _his("EX-SPLIT-0003", "50.00", row=17),
                        _his("EX-SPLIT-0003", "78.00", row=18),
                    ],
                ),
                "wechat",
            ),
        }
    )

    # 4. 歧义：合并支付（金额不守恒、扇出侧金额不全相同）
    cases.append(
        {
            "case_id": "04-ambiguous-merged-payment",
            "name_zh": "歧义：一笔渠道款覆盖多条 HIS 收费且金额不守恒",
            "channel": "unionpay",
            "kind": "ambiguous",
            "expect": {
                "cause_codes": ["merged_payment_allocation"],
                "required_refs": ["unionpay_bill.csv:7"],
                "min_refs": 2,
            },
            "bundle": _bundle(
                "04",
                _first(
                    "unionpay",
                    [_chan("EX-MERGE-0004", "300.00", channel="unionpay", row=7, time="2026-09-04 09:15:00")],
                    [
                        _his("EX-MERGE-0004", "120.00", row=31, time="2026-09-04 09:10:00"),
                        _his("EX-MERGE-0004", "90.00", row=32, time="2026-09-04 09:11:00"),
                    ],
                ),
                "unionpay",
            ),
        }
    )

    # 5. 歧义：退费与原单并存
    cases.append(
        {
            "case_id": "05-ambiguous-refund-coexist",
            "name_zh": "歧义：同一单号既有原单行又有退费行",
            "channel": "wechat",
            "kind": "ambiguous",
            "expect": {
                "cause_codes": ["refund_offset_in_his", "bill_type_excludes_refund"],
                "required_refs": ["wechat_bill.csv:9"],
                "min_refs": 2,
            },
            "bundle": _bundle(
                "05",
                _first(
                    "wechat",
                    [_chan("EX-REFUND-0005", "128.00", row=9, refund="42.00")],
                    [
                        _his("EX-REFUND-0005", "128.00", row=41),
                        _his(
                            "EX-REFUND-0005",
                            "-42.00",
                            row=42,
                            trade_type="退费",
                        ),
                    ],
                ),
                "wechat",
            ),
        }
    )

    # 6. 跨账期落点：唯一命中那条落在账期窗口之外
    cases.append(
        {
            "case_id": "06-cross-period-landing",
            "name_zh": "跨账期：HIS 唯一命中行落在账期窗口之外",
            "channel": "alipay",
            "kind": "cross_period",
            "expect": {
                "cause_codes": [
                    "period_boundary_landing",
                    "cross_period_duplicate",
                    "channel_late_posting",
                ],
                "required_refs": ["alipay_bill.csv:11"],
                "min_refs": 1,
            },
            "bundle": _bundle(
                "06",
                _first(
                    "alipay",
                    [_chan("EX-CROSS-0006", "150.00", channel="alipay", row=11, time="2026-09-07 00:00:00")],
                    [_his("EX-CROSS-0006", "150.00", row=51, time="2026-09-07 00:00:00")],
                ),
                "alipay",
            ),
        }
    )

    # 7. 金额不符：对上了但对不平
    cases.append(
        {
            "case_id": "07-mismatch-amount",
            "name_zh": "金额不符：单号对上但两侧金额不等",
            "channel": "wechat",
            "kind": "mismatch",
            "expect": {
                "cause_codes": ["contract_config_error", "undetermined"],
                "required_refs": ["wechat_bill.csv:13"],
                "min_refs": 1,
            },
            "bundle": _bundle(
                "07",
                _first(
                    "wechat",
                    [_chan("EX-DIFF-0007", "200.00", row=13)],
                    [_his("EX-DIFF-0007", "180.00", row=61)],
                ),
                "wechat",
            ),
        }
    )

    # 8. 自助机金额不符
    cases.append(
        {
            "case_id": "08-self-service-mismatch",
            "name_zh": "自助机：单号对上但两侧金额不等",
            "channel": "self_service",
            "kind": "mismatch",
            "expect": {
                "cause_codes": ["contract_config_error", "undetermined"],
                "required_refs": ["self_service_bill.csv:4"],
                "min_refs": 1,
            },
            "bundle": _bundle(
                "08",
                _first(
                    "self_service",
                    [
                        _chan(
                            "EX-SELF-0008",
                            "45.00",
                            channel="self_service",
                            row=4,
                            file="self_service_bill.csv",
                            time="2026-09-05 16:40:00",
                        )
                    ],
                    [
                        _his(
                            "EX-SELF-0008",
                            "40.00",
                            row=71,
                            time="2026-09-05 16:41:00",
                            settle_type="现金",
                        )
                    ],
                ),
                "self_service",
            ),
        }
    )

    # 9. 重复认领：同一条 HIS 收费已被另一路渠道行认领过一次。
    # ``claimed`` 要显式传 —— 单跑一路时对齐器自己算认领集合，永远撞不出
    # duplicate_claim；这个形态**只在六路一起跑时**才可能出现，所以回放集里
    # 必须手动把「谁先认领的」喂进去，否则这一格会悄悄退化成 mismatch，
    # 而报告上仍然写着「重复认领」。
    dup_his = _his("EX-DUP-0009", "130.00", row=81, time="2026-09-05 11:00:00", ch_id=9001)
    cases.append(
        {
            "case_id": "09-duplicate-claim",
            "name_zh": "重复认领：同一条 HIS 收费已被另一路渠道认领",
            "channel": "unionpay",
            "kind": "duplicate_claim",
            "expect": {
                "cause_codes": [
                    "merged_payment_allocation",
                    "contract_config_error",
                    "undetermined",
                ],
                "required_refs": ["unionpay_bill.csv:6"],
                "min_refs": 1,
            },
            "bundle": _bundle(
                "09",
                _first(
                    "unionpay",
                    [
                        _chan(
                            "EX-DUP-0009",
                            "130.00",
                            channel="unionpay",
                            row=6,
                            time="2026-09-05 11:00:00",
                        )
                    ],
                    [dup_his],
                    claimed={dup_his.ch_id: "wechat"},
                ),
                "unionpay",
            ),
        }
    )

    # 10. UNKNOWN：候选金额全相同且与对侧不守恒 → 信息耗尽
    cases.append(
        {
            "case_id": "10-unknown-equal-amounts",
            "name_zh": "UNKNOWN：候选金额全相同且与对侧不守恒",
            "channel": "wechat",
            "kind": "ambiguous",
            "expect": {
                "cause_codes": ["undetermined"],
                "required_refs": ["wechat_bill.csv:15"],
                "min_refs": 1,
            },
            "bundle": _bundle(
                "10",
                _first(
                    "wechat",
                    [_chan("EX-EQ-0010", "66.00", row=15, time="2026-09-05 08:30:00")],
                    [
                        _his("EX-EQ-0010", "66.00", row=91, time="2026-09-05 08:30:00"),
                        _his("EX-EQ-0010", "66.00", row=92, time="2026-09-05 08:31:00"),
                    ],
                ),
                "wechat",
            ),
        }
    )

    # 11. 缺列：账单列没带 HIS 单号，判据字段没有值
    cases.append(
        {
            "case_id": "11-missing-his-order-no",
            "name_zh": "缺列：账单行没带 HIS 单号，判据字段无值",
            "channel": "self_service",
            "kind": "channel_only",
            "expect": {
                "cause_codes": ["undetermined", "his_missing_order"],
                "required_refs": ["self_service_bill.csv:8"],
                "min_refs": 1,
            },
            "bundle": _bundle(
                "11",
                _first(
                    "self_service",
                    [
                        _chan(
                            "EX-NOCOL-0011",
                            "12.00",
                            channel="self_service",
                            row=8,
                            file="self_service_bill.csv",
                            time="2026-09-06 10:05:00",
                        )
                    ],
                    [],
                ),
                "self_service",
            ),
        }
    )

    # 12. 医保侧单边账：结算单有、HIS 收费流水查无
    cases.append(
        {
            "case_id": "12-medical-ins-settlement-only",
            "name_zh": "医保侧：结算单有、HIS 收费流水查无对应记录",
            "channel": "medical_ins",
            "kind": "channel_only",
            "expect": {
                "cause_codes": ["his_missing_order", "channel_late_posting"],
                "required_refs": ["medical_ins_bill.csv:3"],
                "min_refs": 1,
            },
            "bundle": _bundle(
                "12",
                _first(
                    "medical_ins",
                    [
                        _chan(
                            "EX-MI-0012",
                            "320.00",
                            channel="medical_ins",
                            row=3,
                            file="medical_ins_bill.csv",
                            time="2026-09-06 15:20:00",
                        )
                    ],
                    [],
                ),
                "medical_ins",
            ),
        }
    )

    return cases


def main() -> int:
    os.makedirs(CASES_DIR, exist_ok=True)
    cases = build_cases()
    for case in cases:
        path = os.path.join(CASES_DIR, f"{case['case_id']}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    print(f"已生成 {len(cases)} 条回放用例 → {CASES_DIR}")
    for case in cases:
        print(f"  · {case['case_id']:<34} {case['name_zh']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
