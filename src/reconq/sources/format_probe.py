"""账单文件格式探测（Task 10）。

把院内文件通道的「脏数据边界」做成**可核对的格式证据**，而不是等到
check 阶段才以「N 条 not_empty fail」的形式冒出来 —— 那种报错对财务科
毫无指向性，看起来像数据填漏了，其实是导出格式不对。

本模块负责五件事：

* **编码**：BOM → UTF-8 严格 → GB18030 严格 → GBK 替换解码，四级判定；
  落到兜底级时打 ``suspect_encoding``，让面板知道这路的编码判据不牢。
* **分隔符**：逗号 / 制表符 / 竖线 / 分号，按「切出的列数是否等于表头
  声明列数」打分；打平时显式报 ``FORMAT_AMBIGUOUS``，**拒绝猜**。
* **表头别名归一**：每路渠道维护 alias 表，同一契约列接受多个同义表头
  （微信「商户订单号」/「订单号」、「应结订单金额」/「金额(元)」…）；
  归一后仍缺必填列则报缺列并列出**实际表头**。
* **尾部说明行与空行剔除**：只剔空行与**纯说明行**；含数据但列数不符的
  行**判为脏行**、绝不剔除（Task 11 的隔离区接手）。
* **金额清洗**：去千分位、去货币符号、会计括号负数，一律 ``Decimal``。

``probe_channel_file()`` 输出 ``{encoding, delimiter, header_line_no,
column_mapping, row_count, suspect_flags}`` 作为面板与结论单的格式证据。

源产品对应：``data_source_connection_properties.py`` 的 ``field_mapping``
（把外部列名映射成内部列名）与 ``execute_query_one_by_one()`` 的
「先探后取」语义；本产品把「探」独立成可出证的一层。

⚠️ 院内文件通道是**本轮的取数形态，不是最终对接方式**：最终形态是各渠道
正式接口 + HIS 只读库。脱敏样例只出现在 ``tests/fixtures/``。
"""

from __future__ import annotations

import csv
import io
import os
import re
import unicodedata
from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from typing import Optional

from ..contracts.model import TableYaml
from ..errors import ReconqError

__all__ = [
    "HEADER_ALIASES",
    "DECIMAL_COLUMNS",
    "DELIMITER_CANDIDATES",
    "EncodingProbe",
    "FormatProbe",
    "RowClass",
    "classify_row",
    "normalize_header",
    "detect_encoding",
    "detect_delimiter",
    "resolve_columns",
    "probe_channel_file",
    "read_bill_file_probed",
    "read_bill_file_isolated",
]


#: 表头别名矩阵：**渠道 → 契约列名 → 该列可接受的表头写法**。
#: 与源产品 ``field_mapping`` 的方向相反：那边是「外部键 → 内部名」，
#: 这里是「契约列名 → 外部表头候选列表」，因为账单是**文件**（表头由导出
#: 方决定），不是配置里写死的映射。
HEADER_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "wechat": {
        "transaction_id": ("微信交易单号", "交易单号", "微信支付订单号"),
        "out_trade_no": ("商户订单号", "订单号", "商户订单号(必填)"),
        "trade_state": ("交易状态", "订单状态"),
        "amount": ("应结订单金额", "订单金额", "金额", "金额(元)"),
        "success_time": ("交易时间", "支付时间"),
        "refund": ("退款金额",),
        "payer": ("用户标识", "用户标识(脱敏)"),
    },
    "alipay": {
        "trade_no": ("支付宝交易号", "交易号"),
        "out_trade_no": ("商户订单号", "订单号"),
        "biz_type": ("业务类型", "交易类型"),
        "amount": ("金额", "金额(元)"),
        "trade_time": ("交易时间", "交易创建时间"),
        "income_expense": ("收/支", "收/支类型", "收支"),
        "counterparty": ("对方账号", "对方账户"),
    },
    "unionpay": {
        "out_trade_no": ("订单号", "商户订单号"),
        "trade_no": ("银联交易单号", "交易单号", "交易流水号"),
        "amount": ("交易金额", "金额", "金额(元)"),
        "trade_time": ("交易时间", "交易日期"),
        "settle_type": ("结算方式", "支付方式"),
        "is_refund": ("是否退款", "是否退费"),
    },
    "self_service": {
        "out_trade_no": ("收费记录号", "订单号", "自助机订单号"),
        "terminal_no": ("终端号", "终端编号"),
        "amount": ("收费金额", "金额", "金额(元)", "实收金额"),
        "pay_time": ("缴费时间", "交易时间", "支付时间"),
        "settle_type": ("结算方式", "支付方式"),
        "is_refund": ("是否退费", "是否退款"),
    },
    "medical_ins": {
        "settlement_no": ("结算号", "结算单号"),
        "total_amount": ("金额合计", "合计金额", "金额合计(元)"),
        "fund_pool": ("基金支付", "医保统筹"),
        "supplementary": ("补充医疗", "补充医疗支付"),
        "medical_aid": ("医疗救助", "救助支付"),
        "personal_burden": ("个人负担", "个人自付"),
        "other_payment": ("其他支付", "其它支付"),
        "personal_payment": ("个人支付",),
        "settle_time": ("结算时间", "结算日期"),
        "settle_type": ("结算方式", "支付方式"),
    },
    "his": {
        "his_order_no": ("收费记录主键", "收费记录号", "HIS单号"),
        "charge_id": ("结算单号", "结算号"),
        "amount": ("金额", "收费金额"),
        "charge_time": ("收费时间", "收费日期"),
        "period_code": ("账期", "账期代码"),
        "department": ("科室", "收费科室"),
        "settle_type": ("结算方式", "支付方式"),
        "is_refund": ("是否退费", "是否红冲"),
    },
}

#: 哪些契约列按金额处理（Decimal）。以契约 ``data_type`` 为准，此表只作
#: 「契约没写 data_type」时的兜底。
DECIMAL_COLUMNS = frozenset(
    {
        "amount",
        "refund",
        "total_amount",
        "fund_pool",
        "supplementary",
        "medical_aid",
        "personal_burden",
        "other_payment",
        "personal_payment",
    }
)

#: 分隔符候选（院内导出常见：CSV / TSV / 竖线 / 分号）
DELIMITER_CANDIDATES = (",", "\t", "|", ";")

#: 尾部说明行的判别用语。命中即认定是「说明行」而非数据行。
NOTE_MARKERS = (
    "对账结束",
    "本页合计",
    "交易明细",
    "打印时间",
    "制表人",
    "制表",
    "说明",
    "备注",
    "以下空白",
    "共计",
)

#: 纯分隔线（整行只有这些符号，没有可读文字）
_SEPARATOR_CHARS = set("-—–=＝*·•_~#　 \t")

#: 表头里允许出现的**货币单位后缀**：`金额(元)` 与 `金额` 是同一列。
#: 只收货币单位，**不收 ``%``** —— ``金额(%)`` 是费率不是金额，把它归一成
#: 「金额」会让费率列被当金额解析，是一个静默错列。
_UNIT_SUFFIX = re.compile(r"[(\[（](元|人民币|rmb|cny)[\)\]】]$", re.IGNORECASE)


@dataclass(frozen=True)
class EncodingProbe:
    """一次编码判定的结果。``suspect`` 为真表示用了兜底解码，可能有替换字符。"""

    encoding: str
    suspect: bool
    detail: str = ""

    @property
    def suspect_encoding(self) -> bool:
        """与 ``suspect_flags`` 里的键同名，方便直接进结论单。"""
        return self.suspect


@dataclass(frozen=True)
class FormatProbe:
    """一份账单文件的格式证据。

    这份对象会原样进 run summary 与结论单：财务科问「这路为什么金额对不
    上」时，先看它 —— 编码判据牢不牢、分隔符怎么定的、表头在第几行、
    哪几列认出来了、剔掉了多少说明行。
    """

    path: str
    channel: str
    encoding: str
    encoding_detail: str
    delimiter: str
    header_line_no: int
    column_mapping: dict[str, str]
    row_count: int
    total_line_count: int
    note_line_count: int
    blank_line_count: int
    ragged_rows: list[tuple[int, int]] = dc_field(default_factory=list)
    suspect_flags: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        """面板 / 结论单用的扁平结构。"""
        return {
            "path": self.path,
            "channel": self.channel,
            "encoding": self.encoding,
            "encoding_detail": self.encoding_detail,
            "delimiter": self.delimiter,
            "header_line_no": self.header_line_no,
            "column_mapping": dict(self.column_mapping),
            "row_count": self.row_count,
            "total_line_count": self.total_line_count,
            "note_line_count": self.note_line_count,
            "blank_line_count": self.blank_line_count,
            "ragged_rows": [{"row_no": n, "columns": c} for n, c in self.ragged_rows],
            "suspect_flags": list(self.suspect_flags),
        }


class RowClass:
    """``classify_row()`` 的行类常量。"""

    DATA = "data"
    BLANK = "blank"
    NOTE = "note"
    RAGGED = "ragged"


def normalize_header(name: str) -> str:
    """表头归一：全半角折叠、去 BOM 与所有空白、剥单位后缀、转小写。

    院内导出里同一个概念有三种写法：``金额(元)``（全角括号）、
    ``金额（元）``、``金额``；还有 ``订单号`` 与 ``订单 号``（导出时对齐
    插入的空格）。归一后它们必须落成同一个键，否则「一份账单 20 行里
    19 行认得出、1 行认不出」这种错位会以 not_empty fail 的形式出现。
    """
    text = (name or "").replace("﻿", "")
    text = unicodedata.normalize("NFKC", text)
    text = "".join(text.split())
    text = _UNIT_SUFFIX.sub("", text)
    return text.lower()


def detect_encoding(raw: bytes) -> EncodingProbe:
    """四级编码判定：BOM → UTF-8 严格 → GB18030 严格 → GBK 替换。

    院内导出工具杂：有的 UTF-8 带 BOM，有的老系统导 GBK。**不做严格判定
    就会在单号里读出半个汉字**，对账时表现为「莫名少了几笔」。最后一级
    用替换字符兜底并显式标 ``suspect=True``，让面板知道这路的编码判据不牢。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return EncodingProbe("utf-8-sig", False, "BOM 头")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return EncodingProbe("utf-16", False, "UTF-16 BOM 头")
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return EncodingProbe("utf-32", False, "UTF-32 BOM 头")
    for enc in ("utf-8", "gb18030"):
        try:
            raw.decode(enc)
            return EncodingProbe(enc, False, f"{enc} 严格解码通过")
        except UnicodeDecodeError:
            continue
    return EncodingProbe("gbk", True, "前三级均失败，用 GBK 替换解码（可能出现替换字符）")


def detect_delimiter(header_line: str, *, expected_columns: Optional[int] = None) -> str:
    """按「切出的列数」打分选分隔符；平手时显式报 ``FORMAT_AMBIGUOUS``。

    不按「哪个分隔符出现得多」选——账单里商品名带逗号是常事，按频次选会
    把一行切成十几列。按列数选，并在 ``expected_columns`` 已知时优先与
    契约声明的列数吻合的那个。
    """
    scores: list[tuple[int, str]] = []
    for cand in DELIMITER_CANDIDATES:
        try:
            count = len(next(csv.reader(io.StringIO(header_line), delimiter=cand)))
        except (csv.Error, StopIteration):
            continue
        if count < 2:
            continue
        score = 1000 + count if expected_columns and count == expected_columns else count
        scores.append((score, cand))

    if not scores:
        raise ReconqError(
            "FORMAT_AMBIGUOUS",
            f"表头里找不到任何候选分隔符可切出多列：{header_line[:120]!r}",
        )
    best = max(score for score, _ in scores)
    winners = [cand for score, cand in scores if score == best]
    if len(winners) > 1:
        raise ReconqError(
            "FORMAT_AMBIGUOUS",
            f"多个分隔符切出同样的列数（{best}），拒绝猜测；"
            f"候选={winners}；表头={header_line[:120]!r}。请让信息科按 CSV 重新导出",
        )
    return winners[0]


def sniff_delimiter(
    lines: list[str], *, expected_columns: Optional[int] = None
) -> str:
    """在前几行里找**能切出多列**的那一行来判分隔符。

    渠道账单常有抬头（支付宝「电子客户回单」、院内系统的导出水印行），
    拿抬头去判分隔符只会得到「一个字段」，从而误报歧义。逐行往后试，
    第一个切得出多列的候选行说了算。
    """
    last_error: Optional[ReconqError] = None
    for line in lines[:20]:
        if not line.strip():
            continue
        try:
            return detect_delimiter(line, expected_columns=expected_columns)
        except ReconqError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise ReconqError("FORMAT_BROKEN", "账单文件前 20 行全是空行，没有可判定的表头")


def resolve_columns(
    header: list[str],
    table: TableYaml,
    channel: str,
) -> tuple[dict[str, str], list[str]]:
    """把文件表头解析成「契约列名 → 文件表头名」与「未认出的表头」。

    匹配顺序：先按该渠道的 alias 列表，再让契约列名直接命中（文件已用
    英文列名的情况）。同一个表头被两个契约列认领时，取 alias 列表里
    靠前者 —— 例如院内导出同时有「商户订单号」与「订单号」两列时，
    按 alias 优先级认「商户订单号」，避免同一个单号被读两次。
    """
    aliases = HEADER_ALIASES.get(channel, {})
    normalized = {normalize_header(h): h for h in header}
    mapping: dict[str, str] = {}
    claimed: dict[str, str] = {}
    for col in table.columns:
        picked: Optional[str] = None
        for alias in aliases.get(col.name, ()):
            target = normalize_header(alias)
            if target in normalized and target not in claimed:
                picked = normalized[target]
                break
        if picked is None and normalize_header(col.name) in normalized:
            candidate = normalized[normalize_header(col.name)]
            if candidate not in claimed:
                picked = candidate
        if picked is not None:
            mapping[col.name] = picked
            claimed[normalize_header(picked)] = col.name
    unmapped = [h for h in header if normalize_header(h) not in claimed]
    return mapping, unmapped


def classify_row(raw: list[str], width: int) -> str:
    """把一行分成 **数据 / 空行 / 说明行 / 脏行** 四类。

    关键纪律：**含数据但列数不符的行判 ``RAGGED``（脏行），不剔除**。
    剔除它等于静默丢数据 —— 对账少一笔和账上多一笔同样致命，而且前者
    更难被发现（差额是负的，财务科会以为是渠道少给）。
    只有「一个非空格都没有」或「至多一个非空格且命中说明用语 / 纯分隔线」
    才允许剔除。
    """
    cells = [c.strip() for c in raw]
    non_empty = [c for c in cells if c]
    if not non_empty:
        return RowClass.BLANK
    if len(non_empty) == 1:
        text = non_empty[0]
        if all(ch in _SEPARATOR_CHARS for ch in text):
            return RowClass.NOTE
        if any(marker in text for marker in NOTE_MARKERS):
            return RowClass.NOTE
    if len(raw) != width:
        return RowClass.RAGGED
    return RowClass.DATA


def _read_bytes(path: str, channel: str) -> bytes:
    if not os.path.isfile(path):
        raise ReconqError("BILL_FILE_NOT_FOUND", f"账单文件不存在：{path}", channel=channel)
    with open(path, "rb") as handle:
        raw_bytes = handle.read()
    if not raw_bytes:
        raise ReconqError("FORMAT_BROKEN", f"账单文件为空：{path}", channel=channel)
    return raw_bytes


def probe_channel_file(path: str, table: TableYaml, channel: str) -> FormatProbe:
    """探测一份账单文件的格式，返回可进结论单的证据对象。

    只做「探」不做「取」：不解析金额、不构造 ``BillRow``，因此可以在
    check 之前跑，用于「这路账单格式到底对不对」的独立排障腿
    （``reconq data-source test`` 会调用它）。
    """
    raw_bytes = _read_bytes(path, channel)
    probe = detect_encoding(raw_bytes)
    text = raw_bytes.decode(probe.encoding, errors="replace").lstrip("﻿")

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ReconqError("FORMAT_BROKEN", f"账单文件没有非空行：{path}", channel=channel)
    delimiter = sniff_delimiter(lines, expected_columns=len(table.columns))
    try:
        rows_raw = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as exc:
        raise ReconqError(
            "FORMAT_BROKEN", f"账单文件结构损坏：{path}（{exc}）", channel=channel
        ) from exc
    if not rows_raw:
        raise ReconqError("FORMAT_BROKEN", f"账单文件为空：{path}", channel=channel)

    header_idx, mapping, _ = locate_header(rows_raw, table, channel)
    header = [c.strip().lstrip("﻿") for c in rows_raw[header_idx]]
    width = len(header)

    data_rows = 0
    note_lines = 0
    blank_lines = 0
    ragged: list[tuple[int, int]] = []
    for idx, raw in enumerate(rows_raw[header_idx + 1 :], start=header_idx + 2):
        kind = classify_row(raw, width)
        if kind == RowClass.DATA:
            data_rows += 1
        elif kind == RowClass.BLANK:
            blank_lines += 1
        elif kind == RowClass.NOTE:
            note_lines += 1
        else:
            ragged.append((idx, len(raw)))

    flags: list[str] = []
    if probe.suspect:
        flags.append("suspect_encoding")
    if note_lines:
        flags.append("trailer_note_lines")
    if blank_lines:
        flags.append("blank_lines_skipped")
    if ragged:
        flags.append("ragged_rows")
    if delimiter != ",":
        flags.append(f"non_comma_delimiter:{delimiter!r}")
    if header_idx:
        flags.append(f"header_offset:{header_idx}")

    return FormatProbe(
        path=path,
        channel=channel,
        encoding=probe.encoding,
        encoding_detail=probe.detail,
        delimiter=delimiter,
        header_line_no=header_idx + 1,
        column_mapping=dict(mapping),
        row_count=data_rows,
        total_line_count=len(rows_raw),
        note_line_count=note_lines,
        blank_line_count=blank_lines,
        ragged_rows=ragged,
        suspect_flags=tuple(flags),
    )


def locate_header(
    rows_raw: list[list[str]],
    table: TableYaml,
    channel: str,
    *,
    max_scan: int = 20,
) -> tuple[int, dict[str, str], list[str]]:
    """在前 ``max_scan`` 行里找**能映射出必填列**的那一行当表头。

    渠道账单带抬头是常态（支付宝回单抬头、微信明细列表说明行、院内系统的
    「XX 系统导出」水印行）。假设「第一行就是表头」会在这些文件上直接炸掉，
    而炸掉的报错（「找不到必填列」）对财务科毫无指向性。改成「找表头」，
    并且在报缺列时把**实际找到的表头**列出来。

    返回 ``(表头行下标, 契约列→表头映射, 缺列列表)``。
    """
    best_idx: Optional[int] = None
    best_mapping: dict[str, str] = {}
    best_missing: list[str] = []
    for idx, row in enumerate(rows_raw[:max_scan]):
        if not row or not any(cell.strip() for cell in row):
            continue
        header = [c.strip().lstrip("﻿") for c in row]
        mapping, _ = resolve_columns(header, table, channel)
        missing = [c.name for c in table.columns if c.required and c.name not in mapping]
        if not missing:
            return idx, mapping, []
        # 记住「认出的必填列最多」的那一行，报错时把它的实际表头列给人看
        if best_idx is None or len(missing) < len(best_missing):
            best_idx, best_mapping, best_missing = idx, mapping, missing
    if best_idx is None:
        raise ReconqError(
            "FORMAT_BROKEN",
            f"账单文件前 {max_scan} 行没有任何非空行，没有可判定的表头",
        )
    return best_idx, best_mapping, best_missing


def read_bill_file_probed(
    path: str,
    table: TableYaml,
    channel: str,
    *,
    delimiter: Optional[str] = None,
    quarantine_sink: Optional[list] = None,
    period: str = "",
    isolate_empty_required: bool = False,
) -> tuple[list, FormatProbe]:
    """读一份账单文件，同时交出它的 ``FormatProbe`` 格式证据。

    返回 ``(BillRow 列表, FormatProbe)``。金额列统一洗成 ``Decimal`` 后再
    写回字符串字面量，保证下游拿到的是 ``"128.00"`` 而不是浮点残影。
    ``row_no`` 是**源文件真实行号**（1 基，含抬头偏移）。

    ``quarantine_sink`` 不为 ``None`` 时切到**隔离模式**（Task 11）：
    行级错误（列数不符、金额不可解析、时间不可解析、主键列为空）不再
    向上抛泡把整份文件打断，而是追加一条 ``QuarantineRow`` 到 sink 里
    并继续处理下一行。文件级问题（缺必填列、结构损坏、表头认不出）仍然
    抛 —— 那种情况下一行都读不出来，隔离区里也不会有内容。
    严格模式（``quarantine_sink=None``，排障腿用）行为不变。
    """
    from ..engine.checks import BillRow  # 局部导入避免循环依赖

    raw_bytes = _read_bytes(path, channel)
    probe = detect_encoding(raw_bytes)
    text = raw_bytes.decode(probe.encoding, errors="replace").lstrip("﻿")
    source_name = os.path.basename(path)

    if delimiter:
        rows_raw = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    else:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise ReconqError("FORMAT_BROKEN", f"账单文件没有非空行：{path}", channel=channel)
        delimiter = sniff_delimiter(lines, expected_columns=len(table.columns))
        try:
            rows_raw = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        except csv.Error as exc:
            raise ReconqError(
                "FORMAT_BROKEN", f"账单文件结构损坏：{path}（{exc}）", channel=channel
            ) from exc
    if not rows_raw:
        raise ReconqError("FORMAT_BROKEN", f"账单文件为空：{path}", channel=channel)

    header_idx, mapping, missing_required = locate_header(rows_raw, table, channel)
    if missing_required:
        raise ReconqError(
            "BILL_COLUMN_MISSING",
            f"账单文件缺少契约要求的必填列：{', '.join(missing_required)}；"
            f"文件={path}；实际表头（第 {header_idx + 1} 行）={rows_raw[header_idx]}；"
            f"契约列={[c.name for c in table.columns]}",
            channel=channel,
            row_ref=f"{source_name}:{header_idx + 1}",
        )

    header = [c.strip().lstrip("﻿") for c in rows_raw[header_idx]]
    normalized_header = [normalize_header(h) for h in header]
    positions: dict[str, int] = {}
    for col_name, header_name in mapping.items():
        target = normalize_header(header_name)
        if target in normalized_header:
            positions[col_name] = normalized_header.index(target)
    amount_cols = {
        c.name for c in table.columns if c.data_type == "decimal" or c.name in DECIMAL_COLUMNS
    }
    width = len(header)

    from .inbox_files import parse_amount  # 局部导入：金额清洗真源在 inbox_files

    # 隔离模式（Task 11）：行级错误收进 sink 而不是打断整份文件。
    # 关键列与交易时间列取自渠道列名映射，与落库层同一份真源。
    QuarantineRow = None
    key_column = ""
    time_column = ""
    if quarantine_sink is not None:
        from ..store.bill_store import CHANNEL_COLUMN_MAP
        from ..store.quarantine import QuarantineRow as _QR

        QuarantineRow = _QR
        mapping = CHANNEL_COLUMN_MAP.get(channel) or {}
        key_column = mapping.get("order_no", "")
        time_column = mapping.get("trade_time", "")

    def isolate(idx: int, raw_line: str, reason: str, detail: str) -> None:
        quarantine_sink.append(
            QuarantineRow(
                channel=channel,
                period=period,
                source_file=source_name,
                row_no=idx,
                reason_code=reason,
                raw_line=raw_line[:500],
                detail=detail,
            )
        )

    rows: list = []
    note_lines = 0
    blank_lines = 0
    ragged: list[tuple[int, int]] = []
    for idx, raw in enumerate(rows_raw[header_idx + 1 :], start=header_idx + 2):
        kind = classify_row(raw, width)
        if kind == RowClass.BLANK:
            blank_lines += 1
            continue
        if kind == RowClass.NOTE:
            note_lines += 1
            continue
        if kind == RowClass.RAGGED:
            ragged.append((idx, len(raw)))
            detail = (
                f"第 {idx} 行列数与表头不一致：表头 {width} 列，本行 {len(raw)} 列；"
                f"文件={source_name}"
            )
            if quarantine_sink is not None:
                isolate(idx, delimiter.join(raw), "COLUMN_COUNT_MISMATCH", detail)
                continue
            raise ReconqError(
                "COLUMN_COUNT_MISMATCH",
                f"{detail}；原始行={delimiter.join(raw)[:160]!r}。"
                f"该行含数据，不会被静默剔除（请交由隔离区逐行回查）",
                channel=channel,
                row_ref=f"{source_name}:{idx}",
            )
        raw_line = delimiter.join(raw)
        values: dict[str, str] = {}
        row_failed = False
        for col_name, pos in positions.items():
            cell = raw[pos].strip() if pos < len(raw) else ""
            if col_name in amount_cols and cell != "":
                try:
                    amount = parse_amount(
                        cell, source_file=source_name, row_no=idx, column=col_name
                    )
                except ReconqError as exc:
                    if quarantine_sink is None:
                        raise
                    isolate(idx, raw_line, "AMOUNT_UNPARSABLE", exc.detail)
                    row_failed = True
                    break
                values[col_name] = "" if amount is None else str(amount)
            else:
                values[col_name] = cell
        if row_failed:
            continue
        # 编码判据不牢时，含替换字符的行单独隔离：拿乱码当单号去对账，
        # 比这一行直接报错更糟 —— 它会安静地变成一条永不匹配的记录。
        if probe.suspect and "�" in raw_line:
            isolate(
                idx,
                raw_line,
                "ENCODING_UNRESOLVED",
                f"该行含无法解码的字节（文件编码判为 {probe.encoding} 的替换解码）；"
                f"请让信息科按 UTF-8 或 GBK 重新导出",
            )
            continue
        if quarantine_sink is not None and time_column:
            from ..store.bill_store import parse_trade_time

            try:
                parse_trade_time(
                    values.get(time_column, ""), channel=channel,
                    row_ref=f"{source_name}:{idx}",
                )
            except ReconqError as exc:
                isolate(idx, raw_line, "TIMESTAMP_UNPARSABLE", exc.detail)
                continue
        if (
            quarantine_sink is not None
            and isolate_empty_required
            and key_column
            and not values.get(key_column, "").strip()
        ):
            isolate(
                idx,
                raw_line,
                "REQUIRED_FIELD_EMPTY",
                f"主键列 {key_column} 为空，该行进不了幂等唯一键；"
                f"请让导出方补齐该列",
            )
            continue
        rows.append(
            BillRow(
                values=values,
                row_no=idx,
                raw_line=raw_line,
                source_file=source_name,
            )
        )

    flags: list[str] = []
    if probe.suspect:
        flags.append("suspect_encoding")
    if note_lines:
        flags.append("trailer_note_lines")
    if blank_lines:
        flags.append("blank_lines_skipped")
    if delimiter != ",":
        flags.append(f"non_comma_delimiter:{delimiter!r}")
    if header_idx:
        flags.append(f"header_offset:{header_idx}")
    if quarantine_sink is not None and len(quarantine_sink):
        flags.append(f"quarantined_rows:{len(quarantine_sink)}")

    format_probe = FormatProbe(
        path=path,
        channel=channel,
        encoding=probe.encoding,
        encoding_detail=probe.detail,
        delimiter=delimiter,
        header_line_no=header_idx + 1,
        column_mapping=dict(mapping),
        row_count=len(rows),
        total_line_count=len(rows_raw),
        note_line_count=note_lines,
        blank_line_count=blank_lines,
        ragged_rows=ragged,
        suspect_flags=tuple(flags),
    )
    return rows, format_probe


def read_bill_file_isolated(
    path: str,
    table: TableYaml,
    channel: str,
    *,
    period: str = "",
    isolate_empty_required: bool = True,
    delimiter: Optional[str] = None,
) -> tuple[list, FormatProbe, list]:
    """隔离模式读一份账单文件：返回 ``(干净行, FormatProbe, 脏行列表)``。

    与 :func:`read_bill_file_probed` 的差别只有一处，但它是对账场景下
    最重要的那处：**行级错误不再打断整份文件**。

    严格模式（``read_bill_file_probed``）遇到第一行脏数据就抛
    ``COLUMN_COUNT_MISMATCH``，剩下的行谁也看不到 —— 对「这文件到底
    有多少行能对」这个问题毫无回答。隔离模式把每一行脏数据连同源文件
    真实行号与原文收起来，其余行照常返回，由调用方（隔离区 + 落库层）
    决定脏行率超阈值时是否整文件判坏。

    两条仍然**向上抛**的是文件级问题：缺必填列、结构损坏、表头认不出。
    那种文件一行都读不出来，隔离区里也不会有内容，抛出来才有指向性。
    """
    sink: list = []
    rows, probe = read_bill_file_probed(
        path,
        table,
        channel,
        delimiter=delimiter,
        quarantine_sink=sink,
        period=period,
        isolate_empty_required=isolate_empty_required,
    )
    return rows, probe, sink
