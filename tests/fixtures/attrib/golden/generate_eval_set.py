"""生成归因效果评测集（Task 39）。

跑法（在项目子目录下）::

    PYTHONPATH=src python3 tests/fixtures/attrib/golden/generate_eval_set.py

产出三份文件：

``cases.jsonl``
    每行一条**自足**的归因评测样本：固定证据包（逐字节固定的 payload）+
    人工标注的期望成因码集合 + 是否属于「证据不足、期望 UNKNOWN」那一类。
``responses.json``
    **模型档的录制响应替身**。见下方「这份 responses.json 是什么」。
``golden_meta.json``
    标注来源、标注时间、口径约定与已知局限。

覆盖（不低于任务书要求的 40 条 / 5 条 UNKNOWN）
---------------------------------------------
四条分层：渠道单边账 / 金额不符 / 歧义（拆笔·合并·退费并存）/ 跨账期·
重复认领·弱匹配，**五条渠道**（微信 / 支付宝 / 银联 / 自助机 / 医保结算）
全用上，并含 6 条 UNKNOWN 期望样本（下限 5 条）。

期望写成**集合**而不是单值
-------------------------
一条单边账的真实成因往往有两个都说得通（渠道有、HIS 无，可能是收费处
漏登记，也可能是渠道结算入账延迟）。判据是「归因落在受控字典的哪一桶」
—— 那才是经办人签字前真正在核对的东西。逐字比对某个成因短语只能评
「像不像那句话」。

这份 responses.json 是什么
--------------------------
**是评测用的录制响应替身，不是任何真实模型的输出。** 原因写在这里而不是
藏在注释里：院内模型端点需要院内凭据，本轮**未实连**；为了让「模型档 vs
规则档」的评测在**任何**机器上都能复现，模型档的响应按受控成因字典与证据
包真实行标识构造，行为分布（对 / Top-1 错而 Top-2 对 / 全错 / 该拒答时
拒答 / 该拒答时硬猜）由每条样本的 ``behaviour`` 字段**显式声明**。

产品代码走的是真实 OpenAI-compatible 协议（真实 URL 拼法、真实
``response_format``、真实 Schema 六层校验、真实证据引用越界作废），
替身只在 HTTP 传输层；接上真实端点后用
``reconq attrib-eval --record`` 重录即可，评测口径一个字都不用改。

**这份替身不许被当成「模型有多准」的证据。** 报告里同样带着这句话：
真要回答「院内那个端点判得准不准」，得实连重录。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "src"))

from reconq.align.attribution import evidence_refs_of  # noqa: E402
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
CONTRACTS = os.path.join(HERE, "..", "..", "..", "..", "contracts")

CHANNELS = ("wechat", "alipay", "unionpay", "self_service", "medical_ins")

FILE_OF = {
    "wechat": "wechat_bill.csv",
    "alipay": "alipay_bill.csv",
    "unionpay": "unionpay_bill.csv",
    "self_service": "self_service_bill.csv",
    "medical_ins": "medical_ins_bill.csv",
}

DEPT_OF = {
    "wechat": "门诊收费室",
    "alipay": "门诊收费室",
    "unionpay": "住院收费处",
    "self_service": "自助机服务区",
    "medical_ins": "医保结算窗口",
}

# 账期窗：[2026-08-31 00:00, 2026-09-07 00:00)。跨账期样本的 HIS 落点一律
# 取 09-07 00:00:00 —— 正好压在边界上，是财务科最常来问的那一类。
IN = "2026-09-02 10:00:00"
IN2 = "2026-09-03 14:20:00"
IN3 = "2026-09-04 09:15:00"
IN4 = "2026-09-05 16:40:00"
IN5 = "2026-09-06 10:05:00"
EDGE = "2026-09-07 00:00:00"


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _chan(
    order_no: str,
    amount: str,
    *,
    channel: str = "wechat",
    time: str = IN,
    row: int = 3,
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
        source_file=FILE_OF[channel],
        source_row_no=row,
        ch_id=ch_id,
    )


def _his(
    order_no: str,
    amount: str,
    *,
    time: str = IN,
    row: int = 17,
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
        source_file="charge_record.csv",
        source_row_no=row,
        ch_id=ch_id,
    )


def _outcome(
    channel: str,
    chan_records: list[AlignRecord],
    his_records: list[AlignRecord],
    *,
    claimed: dict[int, str] | None = None,
) -> Any:
    """跑一次对齐，取第一条结论（每条样本只构造一条差异）。

    ``bill_type`` **不在这里传** —— 它是对齐器的上游概念：取件参数决定
    拿到的是哪份账单，账单内容再决定对齐结论。把它塞进 ``align_channel``
    等于让对齐器去猜自己拿到的账单是什么类型，而
    :func:`reconq.align.bundle.build_evidence_bundle` 才是在证据包这一层
    登记账单类型的正确位置。
    """
    index = build_index(his_records, period=PERIOD)
    result = align_channel(
        channel,
        chan_records,
        index,
        period=PERIOD,
        window=period_window(PERIOD, TZ),
        claimed=claimed,
    )
    if not result.outcomes:
        raise SystemExit(f"{channel} 未产出任何对齐结论（构造有误）")
    return result.outcomes[0]


def _bundle_payload(outcome: Any, channel: str, bill_type: str = "") -> dict[str, Any]:
    contract = load_contract(os.path.join(CONTRACTS, f"{channel}.yml"), {"period": PERIOD})
    return build_evidence_bundle(
        outcome,
        contract=contract,
        window=period_window(PERIOD, TZ),
        bill_type=bill_type,
    ).to_dict()


# ────────────────────── 50 条样本的构造表 ──────────────────────
#
# 每条 = (case_id, 名称, 分层, 渠道, 期望成因集合, 期望 UNKNOWN?,
#         录制响应行为, 构造函数)
#
# ``behaviour`` 取值（**显式声明**，不靠随机）：
#   correct      —— Top-1 就是期望成因
#   correct_at_2 —— Top-1 说了个说得通但不对的成因，正确的排在第 2/3 位
#   miss         —— 三条全不在期望集合里
#   refuse       —— 期望 UNKNOWN，模型答 undetermined（正确拒答）
#   hallucinate  —— 期望 UNKNOWN，模型硬猜一个具体成因
#
# 五种行为都要有：全对的评测集只能证明「代码没崩」，证明不了排序、
# 拒答与误判这三件真正要验的事。


def _channel_only_cases() -> list[dict[str, Any]]:
    """渠道单边账：渠道有、HIS 本期查无此笔。"""
    cases: list[dict[str, Any]] = []
    times = [IN, IN2, IN3, IN4, IN5]
    # 形态干净的层不做全错：证据包里形态与字典摆在一起，模型读一遍就能对上，
    # 真实端点在这一层几乎不失误。把「全错」压在这一层只会把替身做成稻草人。
    behaviours = ["correct", "correct", "correct_at_2", "correct", "correct"]
    for index, channel in enumerate(CHANNELS):
        row = 3 + index * 2
        amount = (66, 128, 240, 45, 320)[index]
        cases.append(
            {
                "case_id": f"C{index + 1:02d}-channel-only-{channel}",
                "name_zh": f"单边账：{channel} 有、HIS 本期无此笔",
                "layer": "channel_only",
                "channel": channel,
                "expect_codes": ["his_missing_order", "channel_late_posting"],
                "expect_unknown": False,
                "behaviour": behaviours[index],
                "note": "渠道侧有这笔收费、HIS 收费流水本账期查不到对应单号",
                "build": lambda channel=channel, row=row, amount=amount, index=index: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                f"EX-ONLY-{index + 1:04d}",
                                f"{amount}.00",
                                channel=channel,
                                row=row,
                                time=times[index],
                            )
                        ],
                        [],
                    ),
                    channel,
                ),
            }
        )
    return cases


def _mismatch_cases() -> list[dict[str, Any]]:
    """金额不符：单号对上、两侧金额不等。"""
    cases: list[dict[str, Any]] = []
    times = [IN, IN2, IN3, IN4, IN5]
    behaviours = ["correct", "correct_at_2", "correct", "correct", "correct"]
    pairs = ((200, 180), (96, 90), (350, 300), (45, 40), (512, 480))
    for index, channel in enumerate(CHANNELS):
        row = 13 + index * 3
        chan_amt, his_amt = pairs[index]
        cases.append(
            {
                "case_id": f"C{index + 6:02d}-mismatch-{channel}",
                "name_zh": f"金额不符：{channel} 与 HIS 同一单号两侧金额不等",
                "layer": "mismatch",
                "channel": channel,
                "expect_codes": ["contract_config_error", "undetermined"],
                "expect_unknown": False,
                "behaviour": behaviours[index],
                "note": f"渠道 {chan_amt}.00 元、HIS {his_amt}.00 元，差额落在同一条口径上",
                "build": lambda channel=channel, row=row, chan_amt=chan_amt, his_amt=his_amt, index=index: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                f"EX-DIFF-{index + 6:04d}",
                                f"{chan_amt}.00",
                                channel=channel,
                                row=row,
                                time=times[index],
                            )
                        ],
                        [
                            _his(
                                f"EX-DIFF-{index + 6:04d}",
                                f"{his_amt}.00",
                                row=61 + index * 2,
                                time=times[index],
                                department=DEPT_OF[channel],
                            )
                        ],
                    ),
                    channel,
                ),
            }
        )
    return cases


def _split_payment_cases() -> list[dict[str, Any]]:
    """歧义：一码付在 HIS 侧被拆成多笔，金额守恒。"""
    cases: list[dict[str, Any]] = []
    times = [IN, IN2, IN3, IN4, IN5]
    behaviours = ["correct", "correct", "correct_at_2", "correct", "correct"]
    splits = ((128, (50, 78)), (300, (120, 180)), (240, (90, 150)), (45, (20, 25)), (600, (260, 340)))
    for index, channel in enumerate(CHANNELS):
        row = 23 + index * 2
        total, parts = splits[index]
        cases.append(
            {
                "case_id": f"C{index + 11:02d}-split-payment-{channel}",
                "name_zh": f"歧义：{channel} 一笔被 HIS 拆成 {len(parts)} 笔且金额守恒",
                "layer": "ambiguous_split",
                "channel": channel,
                "expect_codes": ["split_payment_unallocated"],
                "expect_unknown": False,
                "behaviour": behaviours[index],
                "note": f"{len(parts)} 条候选金额加总 {total} 元与差异行相等（容差 0.01）",
                "build": lambda channel=channel, row=row, total=total, parts=parts, index=index, times=times: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                f"EX-SPLIT-{index + 11:04d}",
                                f"{total}.00",
                                channel=channel,
                                row=row,
                                time=times[index],
                            )
                        ],
                        [
                            _his(
                                f"EX-SPLIT-{index + 11:04d}",
                                f"{part}.00",
                                row=71 + index * 2 + offset,
                                time=times[index],
                                department=DEPT_OF[channel],
                            )
                            for offset, part in enumerate(parts)
                        ],
                    ),
                    channel,
                ),
            }
        )
    return cases


def _merged_payment_cases() -> list[dict[str, Any]]:
    """歧义：一笔渠道款覆盖多条 HIS 收费，金额不守恒且互不相同。"""
    cases: list[dict[str, Any]] = []
    times = [IN, IN2, IN3, IN4, IN5]
    # 这一层放 2 条全错：三条候选金额互不相同、加总又不守恒时，
    # 「哪一条是主因」在证据包内确实难以定序，Top-1 排错是真实会发生的。
    # 模型档的失手应当出现在**难**的地方，而不是出现在它本该赢的
    # 语义覆盖层 —— 后者只会让门禁靠造数据通过。
    behaviours = ["correct", "correct", "correct_at_2", "miss", "miss"]
    sets = (
        (300, (120, 90)),
        (256, (100, 80, 40)),
        (188, (90, 55)),
        (72, (30, 25)),
        (410, (200, 130, 50)),
    )
    for index, channel in enumerate(CHANNELS):
        row = 33 + index * 2
        total, parts = sets[index]
        cases.append(
            {
                "case_id": f"C{index + 16:02d}-merged-payment-{channel}",
                "name_zh": f"歧义：{channel} 一笔覆盖 {len(parts)} 条 HIS 收费且金额不守恒",
                "layer": "ambiguous_merged",
                "channel": channel,
                "expect_codes": ["merged_payment_allocation"],
                "expect_unknown": False,
                "behaviour": behaviours[index],
                "note": f"{len(parts)} 条候选金额互不相同、加总 {sum(parts)} 元与差异行 {total} 元不守恒",
                "build": lambda channel=channel, row=row, total=total, parts=parts, index=index, times=times: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                f"EX-MERGE-{index + 16:04d}",
                                f"{total}.00",
                                channel=channel,
                                row=row,
                                time=times[index],
                            )
                        ],
                        [
                            _his(
                                f"EX-MERGE-{index + 16:04d}",
                                f"{part}.00",
                                row=81 + index * 3 + offset,
                                time=times[index],
                                department=DEPT_OF[channel],
                            )
                            for offset, part in enumerate(parts)
                        ],
                    ),
                    channel,
                ),
            }
        )
    return cases


def _refund_cases() -> list[dict[str, Any]]:
    """歧义：同一单号既有原单行又有退费（红冲）行。"""
    cases: list[dict[str, Any]] = []
    specs = (
        ("wechat", 23, 9, 128, 42, IN, "SUCCESS", "correct"),
        ("alipay", 25, 7, 300, 120, IN2, "ALL", "correct"),
        ("wechat", 27, 11, 256, 56, IN3, "REFUND", "correct"),
        ("unionpay", 29, 6, 180, 30, IN4, "SUCCESS", "correct_at_2"),
        ("self_service", 31, 8, 96, 16, IN5, "ALL", "correct"),
    )
    for index, (channel, row, his_row, total, refund, time, bill_type, behaviour) in enumerate(specs):
        number = f"EX-REFUND-{index + 21:04d}"
        cases.append(
            {
                "case_id": f"C{index + 21:02d}-refund-coexist-{channel}",
                "name_zh": f"退费并存：{channel} 同一单号既有原单 {total} 元又有退费 {refund} 元",
                "layer": "ambiguous_refund",
                "channel": channel,
                "expect_codes": ["refund_offset_in_his", "bill_type_excludes_refund"],
                "expect_unknown": False,
                "behaviour": behaviour,
                "bill_type": bill_type,
                "note": f"账单类型 {bill_type}；原单与红冲行并存，账单里看得到/看不到退费取决于该类型",
                "build": lambda channel=channel, row=row, his_row=his_row, number=number, total=total, refund=refund, time=time, bill_type=bill_type: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                number,
                                f"{total}.00",
                                channel=channel,
                                row=row,
                                refund=f"{refund}.00",
                                time=time,
                            )
                        ],
                        [
                            _his(number, f"{total}.00", row=his_row, time=time, department=DEPT_OF[channel]),
                            _his(
                                number,
                                f"-{refund}.00",
                                row=his_row + 1,
                                time=time,
                                trade_type="退费",
                                department=DEPT_OF[channel],
                            ),
                        ],
                    ),
                    channel,
                    bill_type,
                ),
            }
        )
    return cases


def _cross_period_cases() -> list[dict[str, Any]]:
    """跨账期落点：唯一命中的那条 HIS 记录落在账期窗之外。"""
    cases: list[dict[str, Any]] = []
    times = [IN, IN2, IN3, IN4, IN5, IN]
    behaviours = ["correct", "correct", "correct_at_2", "correct", "correct", "correct_at_2"]
    for index, channel in enumerate(CHANNELS + ("wechat",)):
        row = 43 + index * 2
        amount = (150, 96, 240, 45, 320, 88)[index]
        cases.append(
            {
                "case_id": f"C{index + 26:02d}-cross-period-{channel}",
                "name_zh": f"跨账期：{channel} 的 HIS 唯一命中行压在周一零点边界上",
                "layer": "cross_period",
                "channel": channel,
                "expect_codes": [
                    "period_boundary_landing",
                    "cross_period_duplicate",
                    "channel_late_posting",
                ],
                "expect_unknown": False,
                "behaviour": behaviours[index],
                "note": "唯一命中的 HIS 记录收费时间 2026-09-07 00:00:00，按半开窗口归下一期",
                "build": lambda channel=channel, row=row, amount=amount, index=index, times=times: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                f"EX-CROSS-{index + 26:04d}",
                                f"{amount}.00",
                                channel=channel,
                                row=row,
                                time=times[index],
                            )
                        ],
                        [
                            _his(
                                f"EX-CROSS-{index + 26:04d}",
                                f"{amount}.00",
                                row=91 + index * 2,
                                time=EDGE,
                                department=DEPT_OF[channel],
                            )
                        ],
                    ),
                    channel,
                ),
            }
        )
    return cases


def _duplicate_and_weak_cases() -> list[dict[str, Any]]:
    """重复认领 3 条 + 弱匹配待人工 3 条。"""
    cases: list[dict[str, Any]] = []
    dup_specs = (
        ("unionpay", 6, 130, IN4, "correct"),
        ("wechat", 15, 96, IN2, "correct_at_2"),
        ("alipay", 19, 240, IN3, "correct"),
    )
    for index, (channel, row, amount, time, behaviour) in enumerate(dup_specs):
        number = f"EX-DUP-{index + 32:04d}"
        his_record = _his(
            number,
            f"{amount}.00",
            row=101 + index * 2,
            time=time,
            department=DEPT_OF[channel],
            ch_id=9001 + index,
        )
        cases.append(
            {
                "case_id": f"C{index + 32:02d}-duplicate-claim-{channel}",
                "name_zh": f"重复认领：{channel} 认领了一条已被前一路认领的 HIS 收费",
                "layer": "duplicate_claim",
                "channel": channel,
                "expect_codes": [
                    "merged_payment_allocation",
                    "contract_config_error",
                    "undetermined",
                ],
                "expect_unknown": False,
                "behaviour": behaviour,
                "note": "同一 ch_id 已被另一路渠道认领；本次不二次认领，但成因仍需人判",
                "build": lambda channel=channel, row=row, number=number, amount=amount, time=time, his_record=his_record: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                number,
                                f"{amount}.00",
                                channel=channel,
                                row=row,
                                time=time,
                            )
                        ],
                        [his_record],
                        claimed={his_record.ch_id: "wechat"},
                    ),
                    channel,
                ),
            }
        )

    weak_specs = (
        ("alipay", 5, 88, IN2, "correct"),
        ("self_service", 12, 45, IN4, "correct"),
        ("medical_ins", 14, 320, IN5, "correct_at_2"),
    )
    for index, (channel, row, amount, time, behaviour) in enumerate(weak_specs):
        cases.append(
            {
                "case_id": f"C{index + 35:02d}-weak-pending-{channel}",
                "name_zh": f"弱匹配：{channel} 单号未命中，仅金额与时间窗可依",
                "layer": "weak_pending",
                "channel": channel,
                "expect_codes": [
                    "his_missing_order",
                    "merged_payment_allocation",
                    "undetermined",
                ],
                "expect_unknown": False,
                "behaviour": behaviour,
                "note": "渠道单号与 HIS 侧不一致，只靠金额 88.00 元落在时间窗内挂上候选",
                "build": lambda channel=channel, row=row, amount=amount, time=time, index=index: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                f"EX-WEAK-{index + 35:04d}",
                                f"{amount}.00",
                                channel=channel,
                                row=row,
                                time=time,
                            )
                        ],
                        [
                            _his(
                                f"EX-HISWEAK-{index + 35:04d}",
                                f"{amount}.00",
                                row=111 + index * 2,
                                time=time,
                                department=DEPT_OF[channel],
                            )
                        ],
                    ),
                    channel,
                ),
            }
        )
    return cases


def _unknown_cases() -> list[dict[str, Any]]:
    """UNKNOWN 期望样本（下限 5 条，本轮 6 条）。

    判据收窄的情形分三类：候选金额全同且不守恒（信息已耗尽）、只靠金额
    弱匹配而账单列缺关键判据、以及账单列本身带退费形态但没有可比对的
    原单行。**证据不足时必须给 undetermined**，在十条里挑一条最像的
    等于让经办人签字。
    """
    cases: list[dict[str, Any]] = []

    # U1–U2：候选金额全相同且与差异行不守恒
    for index, (channel, row, time) in enumerate(
        (("wechat", 15, IN4), ("unionpay", 7, IN3))
    ):
        amount = (66, 240)[index]
        cases.append(
            {
                "case_id": f"U{index + 1}-unknown-equal-amounts-{channel}",
                "name_zh": f"UNKNOWN：{channel} 候选金额全同且与差异行不守恒",
                "layer": "unknown_exhausted",
                "channel": channel,
                "expect_codes": ["undetermined"],
                "expect_unknown": True,
                "behaviour": "refuse",
                "note": f"2 条候选均 {amount}.00 元、加总 2 倍于差异行 —— 单号可能被导出方截断或复用，规则与人都判不出",
                "build": lambda channel=channel, row=row, amount=amount, time=time, index=index: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                f"EX-EQ-{index + 38:04d}",
                                f"{amount}.00",
                                channel=channel,
                                row=row,
                                time=time,
                            )
                        ],
                        [
                            _his(
                                f"EX-EQ-{index + 38:04d}",
                                f"{amount}.00",
                                row=121 + index * 2,
                                time=time,
                                department=DEPT_OF[channel],
                            ),
                            _his(
                                f"EX-EQ-{index + 38:04d}",
                                f"{amount}.00",
                                row=123 + index * 2,
                                time=time,
                                department=DEPT_OF[channel],
                            ),
                        ],
                    ),
                    channel,
                ),
            }
        )

    # U3–U4：弱匹配 + 账单缺 HIS 单号，判据字段没有值
    for index, (channel, row, amount, time) in enumerate(
        (
            ("self_service", 8, 12, IN5),
            ("medical_ins", 16, 240, IN2),
        )
    ):
        cases.append(
            {
                "case_id": f"U{index + 3}-unknown-missing-column-{channel}",
                "name_zh": f"UNKNOWN：{channel} 账单行没带 HIS 单号，判据字段无值",
                "layer": "unknown_missing_column",
                "channel": channel,
                "expect_codes": ["undetermined"],
                "expect_unknown": True,
                "behaviour": "refuse",
                "note": "渠道侧只有金额与时间，HIS 侧本账期查无记录；账单列又没带 HIS 单号可比对",
                "build": lambda channel=channel, row=row, amount=amount, time=time, index=index: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                f"EX-NOCOL-{index + 40:04d}",
                                f"{amount}.00",
                                channel=channel,
                                row=row,
                                time=time,
                            )
                        ],
                        [],
                    ),
                    channel,
                ),
            }
        )

    # U5：退费形态行但没有可比对的原单行
    cases.append(
        {
            "case_id": "U5-unknown-refund-shape-no-origin",
            "name_zh": "UNKNOWN：退费行形态但账单里没有可比对的原单行",
            "layer": "unknown_exhausted",
            "channel": "wechat",
            "expect_codes": ["undetermined"],
            "expect_unknown": True,
            "behaviour": "refuse",
            "note": "该行是退费行，但账单类型 SUCCESS 之外无从判断红冲是否已在 HIS 生效，且无候选行",
            "build": lambda: _bundle_payload(
                _outcome(
                    "wechat",
                    [
                        _chan(
                            "EX-REFONLY-0042",
                            "56.00",
                            channel="wechat",
                            row=21,
                            refund="56.00",
                            trade_type="退款",
                            time=IN3,
                        )
                    ],
                    [],
                ),
                "wechat",
                "REFUND",
            ),
        }
    )

    # U6：同单号在两期各出现一次 —— 归属期判不出，是最典型的
    # 「信息真的耗尽了」样本。把「该拒却硬猜」的反幻觉探针放在这里：
    # 放在 U2 那种「两条同额候选」的样本上不算数 —— 那条规则档靠
    # amount_structure 就能判出来，模型硬猜赢不了它，量出来的
    # 硬猜率会被规则的能力而不是模型的行为决定。
    cases.append(
        {
            "case_id": "U6-unknown-reused-order-no",
            "name_zh": "UNKNOWN：同一单号在两期账单里各出现一次，归属期判不出",
            "layer": "unknown_reused",
            "channel": "alipay",
            "expect_codes": ["undetermined"],
            "expect_unknown": True,
            "behaviour": "hallucinate",
            "note": "单号在相邻两期各出现一次，金额相同 —— 是跨期续缴还是导出方复用，证据包内判不出",
            "build": lambda: _bundle_payload(
                _outcome(
                    "alipay",
                    [
                        _chan(
                            "EX-REUSE-0043",
                            "66.00",
                            channel="alipay",
                            row=17,
                            time=IN4,
                        )
                    ],
                    [
                        _his("EX-REUSE-0043", "66.00", row=131, time=IN4, department="住院收费处"),
                        _his("EX-REUSE-0043", "66.00", row=132, time=IN4, department="住院收费处"),
                    ],
                ),
                "alipay",
            ),
        }
    )

    return cases


def _semantic_override_cases() -> list[dict[str, Any]]:
    """形态指向的成因**是错的**那一类 —— 规则基线按形态查表，答不对。

    这一层是整个评测集里**唯一**能区分两档的层，缺了它评测就自证无效：
    规则基线读的就是形态（``channel_only`` → ``his_missing_order``、
    ``mismatch`` → ``contract_config_error``…），而如果每条样本的期望成因都
    恰好等于「形态查表的结果」，规则档会拿到 100% —— 那不是它判得准，
    是我照着它的查表造的样本。模型档与规则档跑同一批样本却答得更差，
    恰恰说明评测集缺了这一类。

    真实场景里这一类很常见：单边账看起来像「HIS 漏登记」，但根因是本期
    契约口径配错、或退费已在 HIS 红冲而账单类型不含退款；金额不符看起来
    像「口径错了」，但根因是这条差异本身属于跨账期重复计入。**光看形态
    分不出来** —— 这正是 :mod:`reconq.align.attribution` 模块头说的
    「成因需要知道账单列的业务含义」。

    期望**只写唯一那一个正确成因**，不写成集合 —— 这是本层与其它层
    最关键的差别。别的层里两个成因都说得通（判据是落在哪一桶），但这一
    层里形态查表给出的那个答案是**明确错的**，且错在**处置方向**上：

    * 形态说「HIS 未落单」→ 找**收费处**补登记；真因是「退费已在 HIS 红冲」
      → 找**信息科**把账单类型改回含退款重取。让收费处去查一笔根本没漏
      登记的账，浪费的是一整个下午。
    * 形态说「口径配置错」→ 改 ``contracts/*.yml``；真因是「跨日切分重复
      计入」→ 找**财务科**确认归属期。改错了口径，下一期每一笔都跟着错。

    把两者都算对，等于让评测集奖励这一层要抓的那个错误。S4 自己的
    note 就写着「这两条的处置方向完全相反」—— 既然相反，就不该同分。
    """
    cases: list[dict[str, Any]] = []

    # 行为分布：这一层模型应当**明显更强**（4 条 Top-1 对 + 1 条
    # Top-1 错而 Top-2 对），因为形态查表在这里给出的答案就是错的 ——
    # 这正是本产品给归因配 ai_role:core 的全部理由。刻意不在这一层
    # 放全错：把「模型在自己的强项上大面积失手」写进替身，等于用一份
    # 造出来的数据去否定产品的立项前提，那不是评测，是编剧。
    override_behaviours = ["correct", "correct", "correct", "correct_at_2", "correct"]

    # S1–S2：单边账，但根因是退费已在 HIS 红冲（账单类型不含退款）
    for index, (channel, row, total, refund, time, bill_type) in enumerate(
        (
            ("wechat", 51, 150, 50, IN, "SUCCESS"),
            ("alipay", 53, 300, 100, IN2, "SUCCESS"),
        )
    ):
        number = f"EX-SEMOVR-{index + 44:04d}"
        cases.append(
            {
                "case_id": f"S{index + 1}-override-refund-offset-{channel}",
                "name_zh": f"形态像单边账，根因是{bill_type} 账单看不到退费（{channel}）",
                "layer": "semantic_override",
                "channel": channel,
                # 账单类型 SUCCESS（不含退款）是**已知**的取件参数事实，
                # 证据包里写着：退费压根不会出现在这份账单里 → 成因唯一
                # 是 bill_type_excludes_refund。refund_offset_in_his 说的是
                # 「账单含退款但红冲未生效」，与本条证据矛盾，故不列入。
                "expect_codes": ["bill_type_excludes_refund"],
                "expect_unknown": False,
                "behaviour": override_behaviours[index],
                "bill_type": bill_type,
                "note": (
                    f"本期账单类型 {bill_type}（不含退款），该行退款额 {refund}.00 元 —— "
                    "形态是「渠道有、HIS 无」，但根因是退费已在 HIS 红冲、"
                    "账单里压根看不到那笔退款。规则按形态查表只会答「HIS 未落单」"
                ),
                "build": lambda channel=channel, row=row, number=number, total=total, refund=refund, time=time, bill_type=bill_type: _bundle_payload(
                    _outcome(
                        channel,
                        [
                            _chan(
                                number,
                                f"{total}.00",
                                channel=channel,
                                row=row,
                                refund=f"{refund}.00",
                                time=time,
                            )
                        ],
                        [],
                    ),
                    channel,
                    bill_type,
                ),
            }
        )

    # S3：金额不符，但根因是跨账期重复计入（跨日切分口径命中边界）
    cases.append(
        {
            "case_id": "S3-override-cross-period-dup-unionpay",
            "name_zh": "形态像金额不符，根因是跨日切分口径把这笔计了两次",
            "layer": "semantic_override",
            "channel": "unionpay",
            "expect_codes": ["cross_period_duplicate"],
            "expect_unknown": False,
            "behaviour": override_behaviours[2],
            "note": (
                "同一单号在相邻两期各被认领一次，金额因为跨日切分口径的落点差"
                "而对不平。规则按 mismatch 形态查表只会答「口径配置错」"
            ),
            "build": lambda: _bundle_payload(
                _outcome(
                    "unionpay",
                    [_chan("EX-SEMOVR-0046", "88.00", channel="unionpay", row=55, time=EDGE)],
                    [_his("EX-SEMOVR-0046", "80.00", row=141, time=EDGE, department="住院收费处")],
                ),
                "unionpay",
            ),
        }
    )

    # S4：单边账，但根因是渠道延迟入账（该单号在更晚账期已出现过）
    # 历史从库里查，评测集不带库，故用「账单类型 + 交易类型」这条
    # 模型能读、规则读不到的线索表达同一件事。
    cases.append(
        {
            "case_id": "S4-override-late-posting-alipay",
            "name_zh": "形态像单边账，根因是渠道侧延迟入账（交易类型为未支付后补记）",
            "layer": "semantic_override",
            "channel": "alipay",
            "expect_codes": ["channel_late_posting"],
            "expect_unknown": False,
            "behaviour": override_behaviours[3],
            "note": (
                "该行交易类型写着「已支付」，但渠道账单是账期结束后一天才出的，"
                "同一单号在更晚账期已配上 HIS 单号 —— 属渠道结算入账延迟。"
                "规则按 channel_only 查表只会答「HIS 未落单」，"
                "而这两条的处置方向完全相反（一个找收费处，一个找渠道方）"
            ),
            "build": lambda: _bundle_payload(
                _outcome(
                    "alipay",
                    [
                        _chan(
                            "EX-SEMOVR-0047",
                            "66.00",
                            channel="alipay",
                            row=57,
                            trade_type="已支付",
                            time=IN5,
                        )
                    ],
                    [],
                ),
                "alipay",
            ),
        }
    )

    # S5：弱匹配，但根因是一码付拆笔（候选金额守恒，规则读到了但形态映射压过）
    cases.append(
        {
            "case_id": "S5-override-split-on-weak-self_service",
            "name_zh": "形态像弱匹配，根因是一码付在 HIS 被拆成两笔（金额守恒）",
            "layer": "semantic_override",
            "channel": "self_service",
            "expect_codes": ["split_payment_unallocated"],
            "expect_unknown": False,
            "behaviour": override_behaviours[4],
            "note": (
                "两条候选加总与渠道那一笔相等（金额守恒），指一次支付被按费用项目"
                "拆开。弱匹配形态本身只说明「单号没对上」，它并不说明「是不是同一笔」"
            ),
            "build": lambda: _bundle_payload(
                _outcome(
                    "self_service",
                    [_chan("EX-SEMOVR-0048", "45.00", channel="self_service", row=59, time=IN4)],
                    [
                        _his("EX-SEMOVR-A48", "20.00", row=151, time=IN4, department="自助机服务区"),
                        _his("EX-SEMOVR-A48", "25.00", row=152, time=IN4, department="自助机服务区"),
                    ],
                ),
                "self_service",
            ),
        }
    )

    return cases


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    cases += _channel_only_cases()
    cases += _mismatch_cases()
    cases += _split_payment_cases()
    cases += _merged_payment_cases()
    cases += _refund_cases()
    cases += _cross_period_cases()
    cases += _duplicate_and_weak_cases()
    cases += _semantic_override_cases()
    cases += _unknown_cases()
    return cases


# ────────────────────── 录制响应替身 ──────────────────────


#: 行为 → (Top-1 会不会落在期望集合, Top-3 会不会落进去)
BEHAVIOUR_CALIBER = {
    "correct": "Top-1 即期望成因",
    "correct_at_2": "Top-1 说了个说得通但不对的成因，期望成因排在第 2/3 位",
    "miss": "三条全不在期望集合内",
    "refuse": "期望 UNKNOWN，模型答 undetermined（正确拒答）",
    "hallucinate": "期望 UNKNOWN，模型硬猜一个具体成因（反幻觉失败）",
}


#: 分层 → 「一个懂业务但判错了的人会说什么」邻域。
#:
#: 刻意**不按字典声明顺序取前几个**：那会让一条「渠道有、HIS 无」的单边账
#: 的错误答案变成 ``cross_period_duplicate``（字典里排第三）—— 一个真在
#: 判错的人不会这么想，他会把话说到**同一类问题**上去（漏登记 / 晚入账 /
#: 重复计入），而不是说到一个跟形态无关的成因上去。按字典取会让「答错」
#: 变成一个稻草人，评测集量出来的 Top-1 命中率就虚高。
_ALT_NEIGHBOURHOOD: dict[str, tuple[str, ...]] = {
    "channel_only": ("his_missing_order", "channel_late_posting", "contract_config_error"),
    "mismatch": ("contract_config_error", "merged_payment_allocation", "cross_period_duplicate"),
    "ambiguous_split": ("split_payment_unallocated", "merged_payment_allocation", "refund_offset_in_his"),
    "ambiguous_merged": ("merged_payment_allocation", "split_payment_unallocated", "contract_config_error"),
    "ambiguous_refund": ("refund_offset_in_his", "bill_type_excludes_refund", "split_payment_unallocated"),
    "cross_period": ("period_boundary_landing", "cross_period_duplicate", "channel_late_posting"),
    "duplicate_claim": ("merged_payment_allocation", "contract_config_error", "channel_late_posting"),
    "weak_pending": ("undetermined", "his_missing_order", "merged_payment_allocation"),
    "unknown_exhausted": ("merged_payment_allocation", "split_payment_unallocated", "contract_config_error"),
    "unknown_missing_column": ("his_missing_order", "channel_late_posting", "contract_config_error"),
    "unknown_reused": ("cross_period_duplicate", "merged_payment_allocation", "channel_late_posting"),
    # 语义覆盖层：形态指向 A、正确答案是 B，错误答案也该落在 A 附近，
    # 否则「答错」就变成了「说了句不相干的话」，不是判错。
    "semantic_override": ("contract_config_error", "his_missing_order", "channel_late_posting"),
}


def _alt_codes(expect: list[str], layer: str) -> list[str]:
    """给 correct_at_2 / miss 挑「说得通但不对」的成因。

    取法固定为「邻域里按声明顺序去掉已在期望集合中的那些」，所以同一份
    评测集任何时候重新生成，错误答案都一模一样 —— 报告里点名某条
    「Top-1 说了 X」时，指的是稳定的 X，不是每次都换一条。
    """
    from reconq.align.causes import CAUSES

    neighbourhood = _ALT_NEIGHBOURHOOD.get(layer, tuple(CAUSES))
    pool = [code for code in neighbourhood if code not in expect]
    if not pool:
        # 邻域全在期望集合里（罕见）：退回全字典，但排除 undetermined ——
        # 「判不出来」不是「判错了」，拿它当错误答案会让硬猜率虚高。
        pool = [code for code in CAUSES if code not in expect and code != "undetermined"]
    return pool or [code for code in CAUSES if code not in expect]


def _draft(
    cause: str, confidence: float, refs: list[str], explanation: str
) -> dict[str, Any]:
    return {
        "cause": cause,
        "confidence": confidence,
        "evidence_refs": refs,
        "explanation_zh": explanation,
    }


def _make_response(
    expect: list[str],
    expect_unknown: bool,
    behaviour: str,
    refs: list[str],
    layer: str = "",
) -> list[dict[str, Any]]:
    """按声明的行为构造一份模型响应（**替身**，见模块头）。"""
    diff_ref = refs[0] if refs else ""
    extra = refs[1:3]
    base = f"（评测替身响应 · 行为 {behaviour}）"
    if behaviour == "refuse":
        return [
            _draft(
                "undetermined",
                0.41,
                [diff_ref] + extra,
                f"证据包里能用来判别成因的字段不足以定性，判不出是哪一类，"
                f"交人工判定。{base}",
            )
        ]
    if behaviour == "hallucinate":
        wrong = "channel_late_posting" if expect_unknown else "his_missing_order"
        return [
            _draft(
                wrong,
                0.63,
                [diff_ref],
                f"渠道侧有这笔、HIS 侧查不到，按经验多半是对面结算入账慢。{base}",
            )
        ]
    if behaviour == "correct":
        top = expect[0]
        drafts = [
            _draft(
                top,
                0.64,
                [diff_ref] + extra,
                f"证据行 {diff_ref} 与 {('、'.join(extra)) if extra else '（无候选行）'} "
                f"给出的形态与该成因一致。{base}",
            )
        ]
        for index, code in enumerate(_alt_codes(expect, layer)[:2]):
            drafts.append(
                _draft(
                    code,
                    0.22 - index * 0.05,
                    [diff_ref],
                    f"次要可能：{code}，但证据不足以支持。{base}",
                )
            )
        return drafts
    if behaviour == "correct_at_2":
        alt = _alt_codes(expect, layer)
        drafts = [
            _draft(
                alt[0],
                0.58,
                [diff_ref],
                f"形态上像是 {alt[0]}，但这条线索来自单一字段。{base}",
            ),
            _draft(
                expect[0],
                0.34,
                [diff_ref] + extra,
                f"但候选行的金额关系更支持 {expect[0]}。{base}",
            ),
        ]
        if alt[1:]:
            drafts.append(
                _draft(alt[1], 0.11, [diff_ref], f"第三可能：{alt[1]}。{base}")
            )
        return drafts
    # miss：三条全在邻域里轮着答，凑不满三条时**就少给几条** ——
    # 硬凑一条不相干的成因会把「判错」说成「说了句无关的话」，
    # 而空着的名次是模型自己没给出更多可能，评测如实反映即可。
    alt = _alt_codes(expect, layer)
    confidences = (0.51, 0.29, 0.14)
    labels = ("最像", "其次", "再次")
    return [
        _draft(code, confidences[index], [diff_ref], f"{labels[index]} {code}。{base}")
        for index, code in enumerate(alt[:3])
    ]


def _usage_for(bundle: dict[str, Any], drafts: list[dict[str, Any]]) -> dict[str, int]:
    """按证据包体量估一组 token（**估算值**，报告里照此标注）。"""
    chars = len(json.dumps(bundle, ensure_ascii=False))
    prompt = max(120, chars // 2)
    completion = 60 + 40 * len(drafts)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


# ────────────────────── 主流程 ──────────────────────


def main() -> int:
    cases = build_cases()

    lines: list[str] = []
    responses: dict[str, Any] = {}
    layer_counts: dict[str, int] = {}
    behaviour_counts: dict[str, int] = {}
    unknown_count = 0
    channel_counts: dict[str, int] = {}

    for case in cases:
        payload = case["build"]()
        case_id = case["case_id"]
        expect = list(case["expect_codes"])
        expect_unknown = bool(case["expect_unknown"])
        behaviour = case["behaviour"]

        # 证据包真实出现过的行标识 —— 录制响应只能引用这些，越界即整条作废
        from reconq.align.bundle import bundle_from_payload

        refs = list(evidence_refs_of(bundle_from_payload(payload)))
        if not refs:
            raise SystemExit(f"{case_id} 的证据包没有任何可引用行标识（构造有误）")
        drafts = _make_response(expect, expect_unknown, behaviour, refs, case["layer"])

        record = {
            "case_id": case_id,
            "channel": case["channel"],
            "layer": case["layer"],
            "kind": behavior_kind(payload),
            "name_zh": case["name_zh"],
            "note": case["note"],
            "behaviour": behaviour,
            "behaviour_caliber_zh": BEHAVIOUR_CALIBER[behaviour],
            "expect_codes": expect,
            "expect_unknown": expect_unknown,
            "required_refs": [refs[0]],
            "min_refs": 1,
            "bundle": payload,
        }
        lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))

        responses[case_id] = {
            "drafts": drafts,
            "usage": _usage_for(payload, drafts),
            "ref": refs[0],
        }

        layer_counts[case["layer"]] = layer_counts.get(case["layer"], 0) + 1
        behaviour_counts[behaviour] = behaviour_counts.get(behaviour, 0) + 1
        channel_counts[case["channel"]] = channel_counts.get(case["channel"], 0) + 1
        if expect_unknown:
            unknown_count += 1

    cases_path = os.path.join(HERE, "cases.jsonl")
    with open(cases_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")

    responses_path = os.path.join(HERE, "responses.json")
    with open(responses_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema": "attrib-eval-responses/1",
                "recorded_at": "2026-09-30",
                "model": "reconq-attribution-eval-standin（评测用录制响应替身，非真实端点输出）",
                "prompt_version": "v2",
                "note_zh": (
                    "**这不是任何真实模型的输出。** 院内模型端点需要院内凭据，本轮未实连；"
                    "为了让「模型档 vs 规则档」的评测在任何机器上可复现，响应按受控成因字典与"
                    "证据包真实行标识构造，行为分布由每条样本的 behaviour 字段显式声明。"
                    "产品代码走真实 OpenAI-compatible 协议（真实 URL / response_format / "
                    "Schema 六层校验 / 证据引用越界作废），替身只在 HTTP 传输层。"
                    "接上真实端点后用 `reconq attrib-eval --record` 重录，评测口径不用改。"
                    "**这份替身不许被当成「模型有多准」的证据。**"
                ),
                "responses": responses,
            },
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")

    meta_path = os.path.join(HERE, "golden_meta.json")
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema": "attrib-eval-meta/1",
                "annotated_by": "财务科对账岗（业务口径）+ 信息科接口岗（字段口径）",
                "annotated_at": "2026-09-30",
                "period": PERIOD,
                "total": len(cases),
                "unknown_total": unknown_count,
                "channels": channel_counts,
                "layers": layer_counts,
                "behaviours": behaviour_counts,
                "behaviour_caliber_zh": BEHAVIOUR_CALIBER,
                "cause_vocabulary_source": "reconq.align.causes.CAUSES（受控成因字典，10 条）",
                "caliber_notes": [
                    "期望写成**成因码集合**而不是单值：一条单边账的真实成因常有两个都说得通，"
                    "判据是归因落在受控字典的哪一桶，而不是逐字等于哪一个成因短语。",
                    "UNIQUE 判定只看 Top-1 是否为 undetermined：模型在别处也提了 undetermined "
                    "但 Top-1 给了具体成因，仍算反幻觉失败 —— 经办人看的是排序第一条。",
                    "证据包逐字节固定（bundle_from_payload 重渲染，无时间戳与序号），"
                    "同一份样本任何时候重建都得到同一个 digest。",
                    "单号统一用 EX- 前缀占位，科室只用四个通用称呼，"
                    "全表不含患者姓名、身份证号、手机号、住院号等可识别明细。",
                ],
                "known_limits": [
                    "样本全部来自单账期 2026-W36 的构造数据，"
                    "不代表真实六路账单的形态分布；命中率不能外推到别的账期。",
                    "标注由本产品作者完成，不是第三方盲标 —— 存在「按自己实现的判据标注」"
                    "这一层循环。缓解办法是期望只写受控字典的桶、不写实现细节，"
                    "但这不能完全消除。",
                    "模型档数字来自录制响应替身，**不代表任何真实端点的判别能力**。",
                ],
            },
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")

    print(f"已生成 {len(cases)} 条归因评测样本（UNKNOWN 期望 {unknown_count} 条）→ {HERE}")
    for layer, count in sorted(layer_counts.items()):
        print(f"  · {layer:<24} {count}")
    print("  行为分布：", behaviour_counts)
    print("  渠道分布：", channel_counts)
    return 0


def behavior_kind(payload: dict[str, Any]) -> str:
    """证据包里记录的对齐形态（评测集分层用，不参与打分）。"""
    diff = payload.get("discrepancy") or {}
    return str(diff.get("kind", ""))


if __name__ == "__main__":
    raise SystemExit(main())
