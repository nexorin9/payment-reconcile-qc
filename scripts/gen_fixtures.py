"""Task 37 · 真实规模样例生成器（六路各 2000+ 行 / 分层含歧义与单边账）。

为什么要有这一层
----------------
前 36 个任务的样例都是**十几行到几十行**的形态：够覆盖每一条口径、每一个错误码，
但撑不起「六路一起对齐要多久、要多少内存」这个问题。真实的医院一个账期是
**六路各两三千行、HIS 侧同步量**，财务科关心的是「周一早上点一次扫描，多久出
结论单、会不会把信息科的面板服务打死」。

所以这里生成一份**分层**规模样例：绝大多数是正常对得上的行，另有若干层是真实
世界里最费人工、也最容易被基础实现悄悄做错的形态。

======================  ====================================================
层                       现场长什么样
======================  ====================================================
``normal``              单号精确命中、金额相等
``refund``              渠道行内退费（微信 refund 列）/ 独立红冲行（负额行）
``channel_only``        渠道有、HIS 无（漏收费 / 渠道已入账 HIS 未记）
``his_only``            HIS 有、渠道无（现金、断网、导出漏行）
``split``               一码付拆笔：同一单号在 HIS 侧两条、金额不同
``equal_amount``        同一单号在 HIS 侧两条、金额与日期完全相同
``cross_period``        渠道行在本期、单号命中的 HIS 收费落在窗口之外
``duplicate_claim``     同一笔 HIS 收费被两路渠道各报一次
``edge_day``            交易时间压在账期窗口首秒与末秒
======================  ====================================================

五条不可让的纪律
----------------
1. **单号与金额一律占位**：商户订单号形如 ``EX-WX-40-000123``、HIS 收费记录
   主键形如 ``EX-HIS-0123``、金额是定点随机值。样例**不含任何患者可识别明细**
   （无姓名、无住院号、无身份证、无真实商户号）。
2. **可复现**：固定随机种子，同一命令两次跑出的文件逐字节相同 —— 对拍报告里的
   差异行号必须能被另一台机器复算出来。
3. **契约正则必须过**：``wechat.transaction_id`` / ``alipay.trade_no`` /
   ``self_service.out_trade_no`` / ``medical_ins.settlement_no`` 四列在契约里
   钉了 ``matches_regex``（4 位序号）。样例生成器**从契约正则里推出可用序号
   位数**而不是自己拍脑袋，否则六千行账单会整片报 format fail，把真正的
   歧义层淹掉。
4. **歧义层必须落在 HIS 侧且跨日**：``split`` / ``equal_amount`` 是「一个渠道
   单号对多条 HIS 收费记录」。而 ``bill_rows`` 的幂等唯一键是
   ``(渠道, 账期, 账单日, 渠道单号)`` —— **同单号同一天的第二条 HIS 记录会被
   ``ON CONFLICT DO NOTHING`` 静默丢掉**，歧义层就凭空消失了。所以第二条 HIS
   记录排在**次日**（仍在同一账期内）：既有真实形态（住院预交金分两天入账），
   又绕开了唯一键。
5. **故意留下的 fail 要写进清单**：歧义层会让 HIS 契约的「收费记录主键唯一」
   报 fail、退费层会让微信契约的「本期退款金额须为零」报 fail。这些**都是真的
   该报**，生成器把它们列进 ``manifest.json`` 的 ``expected_fail_layers``，
   压测报告据此逐条核对，不允许「跑出来一堆红但说不清哪几条是故意的」。

用法（在项目子目录下）::

    PYTHONPATH=src python3 scripts/gen_fixtures.py --out out/scale
    PYTHONPATH=src python3 scripts/gen_fixtures.py --out out/scale --force
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional

from reconq.sources.timezone import period_window, to_session_tz

PERIOD = "2026-W40"
DEFAULT_ROWS = 2100
DEFAULT_HIS_ROWS = 2400
DEFAULT_SEED = 20260930

TERMINALS = ["EXAM-TERM-01", "EXAM-TERM-02", "EXAM-TERM-03"]
WX_STATES = ["零钱", "招行", "工商银行"]
ALIPAY_BIZ = "销售分析"

#: 契约正则里固定的量词段（``\d{4}`` 之类）。从契约文本里抽出来是为了让
#: 生成器跟着契约走：契约把序号从 4 位放宽到 6 位时，样例不用改。
_SEQ_RE = re.compile(r"\\d\{(\d+)\}")


def _contract_regex(contracts_dir: str, channel: str, column: str) -> str:
    """取某列（或表级）``matches_regex`` 的 pattern。

    **列级与表级都要扫**：四路渠道的写法并不一致 —— 微信/支付宝/自助机把
    格式口径挂在列的 ``checks`` 下，医保挂在 ``tables[0].checks`` 下（连
    ``path`` 也写成 ``[settlement_no, format]``）。只扫列级会让医保那路
    静默按 6 位序号生成，2200 行账单整片报 format fail，把真正的歧义层
    淹掉 —— 而这种「生成器和契约各写各的」正是本任务要消灭的东西。
    """
    import yaml

    with open(os.path.join(contracts_dir, f"{channel}.yml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    table = doc["dataset"]["tables"][0]
    for col in table["columns"]:
        if col["name"] != column:
            continue
        for check in col.get("checks", []) or []:
            if check.get("type") == "matches_regex":
                return str(check["pattern"])
    for check in table.get("checks", []) or []:
        if check.get("type") != "matches_regex":
            continue
        path = check.get("path") or []
        if path and str(path[0]) == column:
            return str(check.get("pattern", ""))
    return ""


def _serial_digits(pattern: str) -> int:
    """从契约正则里取**最后一段** ``\\d{N}`` 的位数（序号段）。"""
    found = _SEQ_RE.findall(pattern or "")
    return int(found[-1]) if found else 6


def _enum_values(contracts_dir: str, channel: str, column: str) -> list[str]:
    """从契约里取某列 ``in_enum`` / ``set_contains`` 的取值清单。"""
    import yaml

    with open(os.path.join(contracts_dir, f"{channel}.yml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    for col in doc["dataset"]["tables"][0]["columns"]:
        if col["name"] != column:
            continue
        for check in col.get("checks", []) or []:
            if check.get("type") in ("in_enum", "set_contains"):
                return list(check["values"])
    return []


# ────────────────────────── 数据模型 ──────────────────────────


@dataclass
class ChanRow:
    """一路渠道账单的一行（已按该渠道的字段名拍平）。"""

    order_no: str
    secondary: str
    amount: Decimal
    at: Any  # datetime，带 +08:00
    layer: str
    settle: str = ""
    department: str = ""
    is_refund: bool = False
    refund: Optional[Decimal] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class HisRow:
    """一条 HIS 收费记录。"""

    his_order_no: str
    charge_id: str
    amount: Decimal
    at: Any
    department: str
    settle_type: str
    is_refund: bool = False
    layer: str = "normal"
    #: 该行是否属于「跨账期补录」集合（收费时间落在账期窗口之外）
    late: bool = False


# ────────────────────────── 生成 ──────────────────────────


class ScaleFixtureBuilder:
    def __init__(
        self,
        out_dir: str,
        *,
        period: str = PERIOD,
        rows_per_channel: int = DEFAULT_ROWS,
        his_rows: int = DEFAULT_HIS_ROWS,
        seed: int = DEFAULT_SEED,
        contracts_dir: str = "contracts",
    ) -> None:
        self.out_dir = out_dir
        self.period = period
        self.rows_per_channel = rows_per_channel
        self.his_rows = his_rows
        self.rng = random.Random(seed)
        self.contracts_dir = contracts_dir

        self.departments = _enum_values(contracts_dir, "his", "department") or ["门诊"]
        self.his_settle = _enum_values(contracts_dir, "his", "settle_type") or ["现金"]
        self.union_settle = _enum_values(contracts_dir, "unionpay", "settle_type") or ["银行卡"]
        self.ss_settle = _enum_values(contracts_dir, "self_service", "settle_type") or ["现金"]
        self.ins_settle = _enum_values(contracts_dir, "medical_ins", "settle_type") or ["现金"]
        self.alipay_income = _enum_values(contracts_dir, "alipay", "income_expense") or ["收入"]
        self.alipay_biz = _enum_values(contracts_dir, "alipay", "biz_type") or [ALIPAY_BIZ]
        self.wx_state = _enum_values(contracts_dir, "wechat", "trade_state") or ["SUCCESS"]

        # 序号位数从契约正则来（见纪律 3）
        self.wx_seq = _serial_digits(_contract_regex(contracts_dir, "wechat", "transaction_id"))
        self.ali_seq = _serial_digits(_contract_regex(contracts_dir, "alipay", "trade_no"))
        self.ss_seq = _serial_digits(_contract_regex(contracts_dir, "self_service", "out_trade_no"))
        self.ins_seq = _serial_digits(_contract_regex(contracts_dir, "medical_ins", "settlement_no"))
        for name, width in (
            ("wechat", self.wx_seq),
            ("alipay", self.ali_seq),
            ("self_service", self.ss_seq),
            ("medical_ins", self.ins_seq),
        ):
            if 10 ** width < rows_per_channel + 200:
                raise SystemExit(
                    f"{name} 契约正则只允许 {width} 位序号，放不下 "
                    f"{rows_per_channel + 200} 行；请调小 --rows 或放宽契约正则"
                )

        self.window = period_window(period, to_session_tz(None))
        self.his: list[HisRow] = []
        self.channels: dict[str, list[ChanRow]] = {}
        self._charge_seq = 0
        self._seq: dict[str, int] = {}
        self.manifest: dict[str, Any] = {
            "period": period,
            "window": [self.window.start.isoformat(), self.window.end.isoformat()],
            "seed": seed,
            "rows_per_channel": rows_per_channel,
            "his_rows_target": his_rows,
            "serial_widths": {
                "wechat": self.wx_seq,
                "alipay": self.ali_seq,
                "self_service": self.ss_seq,
                "medical_ins": self.ins_seq,
            },
            "layers": {},
            "counts": {},
            "split_order_nos": [],
            "equal_amount_order_nos": [],
            "channel_only": {},
            "cross_period": [],
            "duplicate_claim": [],
            "his_only": [],
            "expected_fail_layers": [],
        }

    # —— 基础工具 ——

    def _next(self, channel: str) -> int:
        self._seq[channel] = self._seq.get(channel, 0) + 1
        return self._seq[channel]

    def _at(self, day_offset: int, second: int) -> Any:
        base = self.window.start + timedelta(days=day_offset)
        return base + timedelta(seconds=second)

    def _random_at(self) -> Any:
        return self._at(self.rng.randrange(0, 7), self.rng.randrange(0, 86400))

    def _money(self, lo: int, hi: int) -> Decimal:
        return (Decimal(self.rng.randrange(lo, hi)) / Decimal(100)).quantize(Decimal("0.01"))

    def _charge_id(self) -> str:
        self._charge_seq += 1
        return f"CHG-S40-{self._charge_seq:06d}"

    def _dept(self) -> str:
        return self.rng.choice(self.departments)

    def _push_his(self, row: HisRow) -> None:
        self.his.append(row)

    def _ins_split(self, total: Decimal) -> dict[str, Decimal]:
        """把医保金额合计拆成满足两条勾稽等式的六段。"""
        burden = (total * Decimal("0.40")).quantize(Decimal("0.01"))
        other = (burden * Decimal("0.60")).quantize(Decimal("0.01"))
        rest = total - burden
        fund = (rest * Decimal("0.60")).quantize(Decimal("0.01"))
        supp = (rest * Decimal("0.25")).quantize(Decimal("0.01"))
        return {
            "fund_pool": fund,
            "supplementary": supp,
            "medical_aid": rest - fund - supp,
            "personal_burden": burden,
            "other_payment": other,
            "personal_payment": burden - other,
        }

    # —— 各路渠道的普通行 ——

    def _build_normal(self) -> None:
        n = self.rows_per_channel

        wx: list[ChanRow] = []
        for _ in range(n):
            i = self._next("wechat")
            amount = self._money(500, 500000)
            refund = None
            layer = "normal"
            if i % 33 == 0:  # 行内退费：退款额记在原单行上
                refund = (amount * Decimal("0.30")).quantize(Decimal("0.01"))
                layer = "refund"
            wx.append(
                ChanRow(
                    order_no=f"EX-WX-40-{i:06d}",
                    secondary=f"EX-WXTXN-2026W40-{i:0{self.wx_seq}d}",
                    amount=amount,
                    at=self._random_at(),
                    layer=layer,
                    settle=self.rng.choice(WX_STATES),
                    refund=refund,
                    extra={"trade_state": "SUCCESS", "trade_type": "JSAPI"},
                )
            )
        self.channels["wechat"] = wx

        ali: list[ChanRow] = []
        for _ in range(n):
            i = self._next("alipay")
            ali.append(
                ChanRow(
                    order_no=f"EX-ALIPAY-{i:06d}",
                    secondary=f"EX-ALIPAYTRX-{i:0{self.ali_seq}d}",
                    amount=self._money(500, 800000),
                    at=self._random_at(),
                    layer="normal",
                    settle=self.alipay_income[0],
                    extra={"biz_type": self.alipay_biz[0]},
                )
            )
        # 退款：支出行与同单号收入行成对（契约 refund_offset 按 out_trade_no 配对）
        income, expense = "收入", "支出"
        for _ in range(60):
            src = ali[self.rng.randrange(len(ali))]
            i = self._next("alipay")
            ali.append(
                ChanRow(
                    order_no=src.order_no,
                    secondary=f"EX-ALIPAYTRX-{i:0{self.ali_seq}d}",
                    amount=src.amount,
                    at=src.at + timedelta(minutes=30),
                    layer="refund",
                    settle=expense if expense in self.alipay_income else src.settle,
                    extra={"biz_type": "订单退款"},
                )
            )
        self.channels["alipay"] = ali

        upl: list[ChanRow] = []
        for _ in range(n):
            i = self._next("unionpay")
            upl.append(
                ChanRow(
                    order_no=f"EX-UPL-{i:06d}",
                    secondary=f"EX-UPTRX-{i:06d}",
                    amount=self._money(300, 300000),
                    at=self._random_at(),
                    layer="normal",
                    settle=self.rng.choice(self.union_settle),
                )
            )
        # 独立红冲行：负额 + is_refund，与同单号正向行成对。
        # 代价是「商户订单号唯一」口径会报 fail —— 真实导出里红冲行确实复用
        # 原单号，这正是那条口径要抓的形态，写进 expected_fail_layers。
        for _ in range(40):
            src = upl[self.rng.randrange(len(upl))]
            upl.append(
                ChanRow(
                    order_no=src.order_no,
                    secondary=f"EX-UPTRX-R{len(upl):06d}",
                    amount=-src.amount,
                    at=src.at + timedelta(hours=2),
                    layer="refund",
                    is_refund=True,
                    settle=src.settle,
                )
            )
        self.channels["unionpay"] = upl

        ss: list[ChanRow] = []
        for _ in range(n):
            i = self._next("self_service")
            ss.append(
                ChanRow(
                    order_no=f"EX-HIS-{i:0{self.ss_seq}d}",
                    secondary=self.rng.choice(TERMINALS),
                    amount=self._money(200, 200000),
                    at=self._random_at(),
                    layer="normal",
                    settle=self.rng.choice(self.ss_settle),
                    department=self._dept(),
                )
            )
        # **不造自助机的红冲行**：`contracts/self_service.yml` 把 amount 钉在
        # ``between 0 … 1000000``，而院内红冲行的金额是负的 —— 这份契约按
        # 现状根本表达不了一笔自助机退费。硬造一行负额进去，只会让 2000 行
        # 样例平白多出一条 ``amount.between`` fail，把真正的歧义层盖住。
        # 真实形态（负额红冲行）留给 Task 38 的指标集按需构造小样例。
        self.channels["self_service"] = ss

        ins: list[ChanRow] = []
        for _ in range(n):
            i = self._next("medical_ins")
            total = self._money(1000, 900000)
            ins.append(
                ChanRow(
                    order_no=f"EX-INS-{i:0{self.ins_seq}d}",
                    secondary="",
                    amount=total,
                    at=self._random_at(),
                    layer="normal",
                    settle=self.rng.choice(self.ins_settle),
                    extra=self._ins_split(total),
                )
            )
        self.channels["medical_ins"] = ins

    # —— 跨渠道层 ——

    def _append_only(self, ch: str, order: str, layer: str) -> ChanRow:
        """往某路追加一行（默认没有 HIS 对应记录）。

        ``order`` 只在**该路契约没有对单号钉格式**时生效。医保与自助机的
        契约给单号钉了 ``matches_regex``（``EX-INS-\\d{4}`` / ``EX-HIS-\\d{4}``），
        那些路必须从**本路自己的号段**里取号 —— 真实账单里不会有一行写着
        「EX-ONLY-SS-0001」，导出方不按「这行是不是单边账」来编单号，
        格式漂移正是那条口径要抓的东西。
        """
        at = self._random_at()
        rows = self.channels[ch]
        if ch == "wechat":
            i = self._next("wechat")
            row = ChanRow(
                order_no=order,
                secondary=f"EX-WXTXN-2026W40-{i:0{self.wx_seq}d}",
                amount=self._money(500, 300000),
                at=at,
                layer=layer,
                settle=self.rng.choice(WX_STATES),
                extra={"trade_state": "SUCCESS", "trade_type": "JSAPI"},
            )
        elif ch == "alipay":
            i = self._next("alipay")
            row = ChanRow(
                order_no=order,
                secondary=f"EX-ALIPAYTRX-{i:0{self.ali_seq}d}",
                amount=self._money(500, 300000),
                at=at,
                layer=layer,
                settle=self.alipay_income[0],
                extra={"biz_type": self.alipay_biz[0]},
            )
        elif ch == "unionpay":
            row = ChanRow(
                order_no=order,
                secondary=f"EX-UPTRX-ONLY{len(rows):06d}",
                amount=self._money(300, 300000),
                at=at,
                layer=layer,
                settle=self.rng.choice(self.union_settle),
            )
        elif ch == "self_service":
            i = self._next("self_service")
            row = ChanRow(
                order_no=f"EX-HIS-{i:0{self.ss_seq}d}",
                secondary=self.rng.choice(TERMINALS),
                amount=self._money(200, 200000),
                at=at,
                layer=layer,
                settle=self.rng.choice(self.ss_settle),
                department=self._dept(),
            )
        else:
            i = self._next("medical_ins")
            total = self._money(1000, 500000)
            row = ChanRow(
                order_no=f"EX-INS-{i:0{self.ins_seq}d}",
                secondary="",
                amount=total,
                at=at,
                layer=layer,
                settle=self.rng.choice(self.ins_settle),
                extra=self._ins_split(total),
            )
        rows.append(row)
        return row

    def _inject_channel_only(self) -> None:
        for ch in ("wechat", "alipay", "unionpay", "self_service", "medical_ins"):
            made = []
            for _ in range(60):
                # 微信/支付宝/银联契约没给单号钉格式，用可读的层标记号段；
                # 医保/自助机从本路号段取号（见 _append_only 的说明）
                order = (
                    f"EX-ONLY-{ch[:2].upper()}-{len(made) + 1:04d}"
                    if ch in ("wechat", "alipay", "unionpay")
                    else ""
                )
                row = self._append_only(ch, order, "channel_only")
                made.append(row.order_no)
            self.manifest["channel_only"][ch] = made

    def _inject_his_only(self) -> None:
        """HIS 有、渠道无：现金收费与导出漏行。"""
        for k in range(1, 121):
            order = f"EX-CASHONLY-{k:06d}"
            self._push_his(
                HisRow(
                    order,
                    self._charge_id(),
                    self._money(100, 50000),
                    self._random_at(),
                    self._dept(),
                    self.rng.choice(self.his_settle),
                    layer="his_only",
                )
            )
            self.manifest["his_only"].append(order)

    def _inject_ambiguity(self) -> None:
        """一码付拆笔 / 金额相同多候选：每路 20 组，HIS 侧同单号两条（跨日）。

        渠道行与 HIS 侧共用**同一个单号**，且该单号取自本路号段
        （``EX-INS-####`` / ``EX-HIS-####`` / …）—— 拆笔与金额相同两种形态
        都是「同一个真实单号在 HIS 侧对应多条收费记录」，单号必须过得了
        契约的格式口径，否则歧义会被 format fail 盖住。
        """
        plan = (
            ("wechat", "split"),
            ("alipay", "split"),
            ("unionpay", "split"),
            ("self_service", "equal_amount"),
            ("medical_ins", "equal_amount"),
        )
        for ch, kind in plan:
            for _ in range(20):
                at = self._at(self.rng.randrange(1, 6), self.rng.randrange(0, 86400))
                if kind == "split":
                    total = self._money(2000, 400000)
                    first = (total * Decimal("0.35")).quantize(Decimal("0.01"))
                    second = total - first
                else:
                    first = self._money(2000, 400000)
                    second = first
                if ch in ("wechat", "alipay", "unionpay"):
                    order = f"EX-AMBIG-{ch[:2].upper()}-{len(self.manifest['split_order_nos']) + len(self.manifest['equal_amount_order_nos']) + 1:04d}"
                    row = self._append_only(ch, order, kind)
                else:
                    # 单号**由 _append_only 从本路号段取**（见其 docstring）。
                    # 曾经在这里先自增一次序号再把空串传进去，结果渠道行拿到的是
                    # 下一个序号、HIS 侧记的是这个序号 —— 歧义层静默退化成
                    # 300 条「渠道有 HIS 无」。单号必须**只从一处**产生。
                    row = self._append_only(ch, "", kind)
                    order = row.order_no
                # 渠道侧那一笔的金额：拆笔形态给全额（等于两条 HIS 之和），
                # 金额相同形态给与候选同额 —— 两种都不该被自动归并
                row.amount = total if kind == "split" else first
                if ch == "medical_ins":
                    # 医保的六段必须跟着金额合计一起重算，否则勾稽等式一
                    # 会因为「改了 total_amount 没改分项」而报假 fail
                    row.extra = self._ins_split(row.amount)
                self._push_his(HisRow(order, self._charge_id(), first, at, self._dept(), "微信", layer=kind))
                # 第二条排在次日：账单日不同才绕得开幂等唯一键（纪律 4）
                self._push_his(
                    HisRow(order, self._charge_id(), second, at + timedelta(days=1), self._dept(), "微信", layer=kind)
                )
                self.manifest[
                    "split_order_nos" if kind == "split" else "equal_amount_order_nos"
                ].append(order)

    def _inject_cross_period(self) -> None:
        """跨账期落点：渠道行在本期，命中单号的 HIS 收费落在窗口之外。"""
        k = 0
        for ch in ("wechat", "alipay", "unionpay", "self_service"):
            for _ in range(10):
                k += 1
                if ch in ("wechat", "alipay", "unionpay"):
                    order = f"EX-XPERIOD-{ch[:2].upper()}-{k:04d}"
                    row = self._append_only(ch, order, "cross_period")
                else:
                    row = self._append_only(ch, "", "cross_period")
                    order = row.order_no
                self._push_his(
                    HisRow(
                        order,
                        self._charge_id(),
                        row.amount,
                        self.window.end + timedelta(hours=1, minutes=k),
                        self._dept(),
                        "微信",
                        layer="cross_period",
                        late=True,
                    )
                )
                self.manifest["cross_period"].append(order)

    def _inject_duplicate_claim(self) -> None:
        """同一笔 HIS 收费被两路渠道各报一次：微信账单里混入自助机号段。"""
        ss_rows = [r for r in self.channels["self_service"] if r.layer == "normal"]
        for k in range(1, 11):
            src = ss_rows[k * 7 % len(ss_rows)]
            i = self._next("wechat")
            self.channels["wechat"].append(
                ChanRow(
                    order_no=src.order_no,
                    secondary=f"EX-WXTXN-2026W40-{i:0{self.wx_seq}d}",
                    amount=src.amount,
                    at=src.at,
                    layer="duplicate_claim",
                    settle=self.rng.choice(WX_STATES),
                    extra={"trade_state": "SUCCESS", "trade_type": "JSAPI"},
                )
            )
            # 微信单号「渠道订单号与微信交易单号不得同值」口径要求两列不同值，
            # 这里两者天然不同；同时 HIS 侧该单号只有一条自助机记录。
            self.manifest["duplicate_claim"].append(
                {"channel": "wechat", "order_no": src.order_no, "also_in": "self_service"}
            )

    def _inject_edge_day(self) -> None:
        """账期窗口首秒与末秒的各路边界行。"""
        for ch in ("wechat", "alipay", "unionpay", "self_service", "medical_ins"):
            for offset in (0, 86399):
                if ch in ("wechat", "alipay", "unionpay"):
                    order = f"EX-EDGE-{ch[:2].upper()}-{offset}"
                    row = self._append_only(ch, order, "edge_day")
                else:
                    row = self._append_only(ch, "", "edge_day")
                    order = row.order_no
                at = self.window.start + timedelta(seconds=offset)
                row.at = at
                self._push_his(
                    HisRow(order, self._charge_id(), row.amount, at, self._dept(), "微信", layer="edge_day")
                )

    def _fill_his_to_target(self) -> None:
        while len(self.his) < self.his_rows:
            order = f"EX-HISONLY-{len(self.his) + 1:06d}"
            self._push_his(
                HisRow(
                    order,
                    self._charge_id(),
                    self._money(100, 80000),
                    self._random_at(),
                    self._dept(),
                    self.rng.choice(self.his_settle),
                    layer="his_only",
                )
            )
            self.manifest["his_only"].append(order)

    def _mirror_normal_to_his(self) -> None:
        """给每一路 ``normal`` 行配一条 HIS 收费记录。

        **这一步是整套样例的地基**：没有它，六路 11000 行里 10990 行都是
        「渠道有 HIS 无」，对齐器全部走 ``channel_only`` 分支，压根走不到
        精确匹配 —— 那样一份「2000+ 行规模样例」验证的是最省事的那条路径，
        测出来的耗时与内存都不作数（真实账期里绝大多数行是**对得上**的）。

        配对规则刻意贴着真实账务走：

        * **金额相等**（净收款口径）—— 微信行内退费行的 HIS 侧记**净额**
          （原单额 - 退款额），与 :attr:`AlignRecord.net_amount` 同口径；
        * **时间相等** —— HIS 收费时间与渠道支付时间同一秒，避免弱匹配
          分支被时间窗过滤掉；
        * **科室与结算方式**从契约枚举里取，不自造。
        """
        settle_of = {
            "wechat": "微信",
            "alipay": "支付宝",
            "unionpay": "银联",
            "self_service": "自助机",
            "medical_ins": "医保",
        }
        #: 需要配 HIS 对应记录的层。刻意**包含 ``refund``**：微信的行内退费
        #: 行在真实账期里同样有 HIS 侧的净额收费记录，没有它就会平白多出
        #: 一批「渠道有 HIS 无」。其余各层（单边账 / 歧义 / 跨账期 / 重复认领 /
        #: 边界）都**故意**不配 —— 它们要的正是「对不上」。
        mirrored = {"normal", "refund"}
        for ch, rows in self.channels.items():
            for row in rows:
                if row.layer not in mirrored:
                    continue
                net = row.amount - (row.refund if row.refund else Decimal("0"))
                self._push_his(
                    HisRow(
                        row.order_no,
                        self._charge_id(),
                        net,
                        row.at,
                        row.department or self._dept(),
                        settle_of[ch],
                        layer="normal",
                    )
                )

    def build(self) -> None:
        self._build_normal()
        self._mirror_normal_to_his()
        self._inject_channel_only()
        self._inject_his_only()
        self._inject_ambiguity()
        self._inject_cross_period()
        self._inject_duplicate_claim()
        self._inject_edge_day()
        self._fill_his_to_target()
        for ch, rows in self.channels.items():
            counts: dict[str, int] = {}
            for r in rows:
                counts[r.layer] = counts.get(r.layer, 0) + 1
            self.manifest["layers"][ch] = counts
        his_counts: dict[str, int] = {}
        for r in self.his:
            his_counts[r.layer] = his_counts.get(r.layer, 0) + 1
        self.manifest["layers"]["his"] = his_counts
        self.manifest["expected_fail_layers"] = [
            {
                "channel": "his",
                "check": "his_order_no.unique",
                "why": "歧义层刻意让同一 his_order_no 在 HIS 侧出现两次（跨日），主键重复正是要报的事实",
                "rows": len(self.manifest["split_order_nos"]) + len(self.manifest["equal_amount_order_nos"]),
            },
            {
                "channel": "wechat",
                "check": "rows.refund_sum.zero",
                "why": "微信层含行内退费，契约要求本期退款合计为零、出现退费须财务科逐笔确认",
                "rows": self.manifest["layers"]["wechat"].get("refund", 0),
            },
            {
                "channel": "unionpay",
                "check": "out_trade_no.unique",
                "why": "银联层含独立红冲行，真实导出里红冲行复用原单号",
                "rows": self.manifest["layers"]["unionpay"].get("refund", 0),
            },
            {
                "channel": "his",
                "check": "rows.charge_time.in_period",
                "why": (
                    "跨账期补录那批 HIS 记录收费时间按设计落在账期窗口之外，"
                    "「跨日切分」口径正是要把它报出来"
                ),
                "rows": self.manifest["layers"]["his"].get("cross_period", 0),
            },
        ]

    # ────────────────────────── 落盘 ──────────────────────────

    def _write_wechat(self, path: str) -> None:
        lines = [
            "交易时间,公众账号ID,商户订单号,微信交易单号,用户标识,交易类型,交易状态,"
            "付款银行,货币种类,应结订单金额,代金券金额,微信退款单号,商户退款单号,退款金额,"
            "充值券退款金额,退款类型,退款状态,商品名称,商户数据包,手续费,费率"
        ]
        for i, r in enumerate(self.channels["wechat"], start=1):
            lines.append(
                ",".join(
                    [
                        r.at.strftime("%Y-%m-%d %H:%M:%S"),
                        "EXAM_WX_MCH_ID",
                        r.order_no,
                        r.secondary,
                        f"脱敏占位用户{(i % 97) + 1:02d}",
                        r.extra.get("trade_type", "JSAPI"),
                        r.extra.get("trade_state", "SUCCESS"),
                        r.settle,
                        "CNY",
                        f"{r.amount}",
                        "0.00",
                        "",
                        "",
                        "" if r.refund is None else f"{r.refund}",
                        "",
                        "",
                        "",
                        "门诊缴费",
                        f"EXAM-DATA-{i:06d}",
                        "0.000000",
                        "0.00",
                    ]
                )
            )
        _write_text(path, "\n".join(lines) + "\n", "utf-8")

    def _write_alipay(self, path: str) -> None:
        lines = [
            "支付宝（中国）网络技术有限公司 电子客户回单",
            f"商户号：EXAM_ALIPAY_MCH  起始日期：{self.window.start.date()}  "
            f"终止日期：{(self.window.end - timedelta(days=1)).date()}",
            "",
            "支付宝交易号,商户订单号,业务类型,金额,交易时间,收/支,对方账号,交易状态",
        ]
        for r in self.channels["alipay"]:
            lines.append(
                ",".join(
                    [
                        r.secondary,
                        r.order_no,
                        r.extra.get("biz_type", self.alipay_biz[0]),
                        f"{r.amount}",
                        r.at.strftime("%Y-%m-%d %H:%M:%S"),
                        r.settle,
                        "EXAM_COUNTERPARTY",
                        "交易成功",
                    ]
                )
            )
        _write_text(path, "\n".join(lines) + "\n", "gb18030")

    def _write_unionpay(self, path: str) -> None:
        clear = (self.window.end - timedelta(days=1)).date().isoformat()
        lines = ["订单号\t银联交易单号\t交易时间\t交易金额\t结算方式\t清算日期\t是否退款"]
        for r in self.channels["unionpay"]:
            lines.append(
                "\t".join(
                    [
                        r.order_no,
                        r.secondary,
                        r.at.strftime("%Y-%m-%d %H:%M:%S"),
                        f"{r.amount}",
                        r.settle,
                        clear,
                        "是" if r.is_refund else "否",
                    ]
                )
            )
        _write_text(path, "\n".join(lines) + "\n", "gb18030")

    def _write_self_service(self, path: str) -> None:
        lines = ["收费记录号|终端号|缴费时间|收费金额|结算方式|是否退费"]
        for r in self.channels["self_service"]:
            lines.append(
                "|".join(
                    [
                        r.order_no,
                        r.secondary,
                        r.at.strftime("%Y-%m-%d %H:%M:%S"),
                        f"{r.amount}",
                        r.settle,
                        "是" if r.is_refund else "否",
                    ]
                )
            )
        _write_text(path, "\n".join(lines) + "\n", "utf-8-sig")

    def _write_medical_ins(self, path: str) -> None:
        lines = [
            "结算号|金额合计|基金支付|补充医疗|医疗救助|个人负担|其他支付|个人支付|结算时间|结算方式"
        ]
        for r in self.channels["medical_ins"]:
            e = r.extra
            lines.append(
                "|".join(
                    [
                        r.order_no,
                        f"{r.amount}",
                        f"{e['fund_pool']}",
                        f"{e['supplementary']}",
                        f"{e['medical_aid']}",
                        f"{e['personal_burden']}",
                        f"{e['other_payment']}",
                        f"{e['personal_payment']}",
                        r.at.strftime("%Y-%m-%d %H:%M:%S"),
                        r.settle,
                    ]
                )
            )
        _write_text(path, "\n".join(lines) + "\n", "utf-8")

    def _write_his_csv(self, path: str, rows: list[HisRow]) -> None:
        lines = ["his_order_no,charge_id,amount,charge_time,period_code,department,settle_type,is_refund"]
        for r in rows:
            lines.append(
                ",".join(
                    [
                        r.his_order_no,
                        r.charge_id,
                        f"{r.amount}",
                        r.at.strftime("%Y-%m-%d %H:%M:%S"),
                        self.period,
                        r.department,
                        r.settle_type,
                        "true" if r.is_refund else "false",
                    ]
                )
            )
        _write_text(path, "\n".join(lines) + "\n", "utf-8")

    def write(self) -> dict[str, Any]:
        base = os.path.join(self.out_dir, self.period)
        if os.path.isdir(base):
            shutil.rmtree(base)
        for ch in self.channels:
            os.makedirs(os.path.join(base, ch), exist_ok=True)
        os.makedirs(os.path.join(self.out_dir, "his"), exist_ok=True)
        self._write_wechat(os.path.join(base, "wechat", f"wechat_tradebill_{self.period}.csv"))
        self._write_alipay(os.path.join(base, "alipay", f"alipay_bill_{self.period}.csv"))
        self._write_unionpay(os.path.join(base, "unionpay", f"unionpay_bill_{self.period}.txt"))
        self._write_self_service(os.path.join(base, "self_service", f"selfservice_{self.period}.csv"))
        self._write_medical_ins(os.path.join(base, "medical_ins", f"medical_ins_{self.period}.csv"))
        in_window = [r for r in self.his if not r.late]
        late = [r for r in self.his if r.late]
        self._write_his_csv(os.path.join(self.out_dir, "his", f"charge_record_{self.period}.csv"), in_window)
        self._write_his_csv(
            os.path.join(self.out_dir, "his", f"charge_record_{self.period}-late.csv"), late
        )
        # HIS 侧另存一份到账期目录：**scan() 目前对六路一律走文件通道**
        # （orchestrator._read_channel_rows），registry 里的 HisChannelSource
        # 没被扫描主路径调用。压测要跑真实的六路 scan，HIS 就得按它今天
        # 实际被读的方式摆进去。这不是「把 HIS 假装成文件渠道」——HIS 侧
        # 真正的只读库取数主路径由 scripts/bench.py 的第 1 条腿单独压测
        # （stream_charge_rows 逐行流式 + 自适应分批），两条腿都测、都不假装。
        os.makedirs(os.path.join(base, "his"), exist_ok=True)
        self._write_his_csv(
            os.path.join(base, "his", f"charge_record_{self.period}.csv"), self.his
        )
        self.manifest["counts"] = {
            **{ch: len(rows) for ch, rows in self.channels.items()},
            "his_in_window": len(in_window),
            "his_late": len(late),
        }
        with open(os.path.join(self.out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(self.manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return self.manifest


def _write_text(path: str, text: str, encoding: str) -> None:
    with open(path, "w", encoding=encoding, newline="\n") as fh:
        fh.write(text)


def generate(
    out_dir: str,
    *,
    period: str = PERIOD,
    rows_per_channel: int = DEFAULT_ROWS,
    his_rows: int = DEFAULT_HIS_ROWS,
    seed: int = DEFAULT_SEED,
    contracts_dir: str = "contracts",
) -> dict[str, Any]:
    builder = ScaleFixtureBuilder(
        out_dir,
        period=period,
        rows_per_channel=rows_per_channel,
        his_rows=his_rows,
        seed=seed,
        contracts_dir=contracts_dir,
    )
    builder.build()
    return builder.write()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="生成六路各 2000+ 行的分层对账样例")
    ap.add_argument("--out", default="out/scale", help="输出根目录（默认 out/scale）")
    ap.add_argument("--period", default=PERIOD)
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="每路渠道行数")
    ap.add_argument("--his-rows", type=int, default=DEFAULT_HIS_ROWS, help="HIS 侧行数")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--force", action="store_true", help="已存在时先删掉旧样例")
    args = ap.parse_args(argv)

    if os.path.isdir(os.path.join(args.out, args.period)) and not args.force:
        print(f"{args.out}/{args.period} 已存在；加 --force 覆盖（或换一个 --out）")
        return 1
    manifest = generate(
        args.out,
        period=args.period,
        rows_per_channel=args.rows,
        his_rows=args.his_rows,
        seed=args.seed,
    )
    counts = manifest["counts"]
    print(f"样例已生成于 {args.out}/{args.period}/（seed={args.seed}，账期窗口 {manifest['window'][0]} ~ {manifest['window'][1]}）")
    for ch in ("wechat", "alipay", "unionpay", "self_service", "medical_ins"):
        print(f"  {ch:<13} {counts[ch]:>6} 行")
    print(f"  {'his':<13} {counts['his_in_window']:>6} 行（窗口内）+ {counts['his_late']} 行跨账期补录")
    print("  故意留下的 fail（对拍报告据此核对）：")
    for item in manifest["expected_fail_layers"]:
        print(f"    - {item['channel']}.{item['check']}：{item['rows']} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
