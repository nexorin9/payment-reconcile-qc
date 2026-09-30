"""院内文件通道取数（Task 5 落取数，Task 10 抽出格式探测层）。

微信/支付宝走真实商户 API（Task 5 的 ``wechat_bill`` / ``alipay_bill``），
HIS 走只读库（Task 6），但**院内银联卡与自助机账单**由院内系统按账期导出到
``inbox/<period>/<channel>/``，本产品从这里读。

Task 10 把「探格式」与「读数据」分成两层：

* **格式探测**（``format_probe``，本模块的转发真源）：编码四级判定、
  分隔符按列数打分、表头别名归一、尾部说明行与空行剔除、金额清洗规则、
  ``probe_channel_file()`` 格式证据；
* **读数据**（本模块）：拿探测结果把行变成 ``BillRow``，脏行进隔离区。

本模块保留 Task 5 的全部公开名字（``BILL_HEADER_ALIASES`` /
``detect_encoding`` / ``detect_delimiter`` / ``normalize_header`` /
``parse_amount`` / ``build_column_mapping`` / ``read_bill_file``）作为转发，
避免已有调用方（``wechat_bill``、``cli``、既有测试）跟着改。

⚠️ 院内文件通道是**本轮的取数形态，不是最终对接方式**：最终形态是各渠道
正式接口 + HIS 只读库。脱敏样例只出现在 ``tests/fixtures/``。
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Optional

from ..contracts.model import TableYaml
from ..engine.checks import BillRow
from ..errors import ReconqError
from .format_probe import (
    DECIMAL_COLUMNS,
    DELIMITER_CANDIDATES,
    HEADER_ALIASES,
    EncodingProbe,
    FormatProbe,
    detect_delimiter,
    detect_encoding,
    locate_header,
    normalize_header,
    probe_channel_file,
    read_bill_file_probed,
    resolve_columns,
)

__all__ = [
    "BILL_HEADER_ALIASES",
    "DECIMAL_COLUMNS",
    "DELIMITER_CANDIDATES",
    "EncodingProbe",
    "FormatProbe",
    "normalize_header",
    "parse_amount",
    "detect_encoding",
    "detect_delimiter",
    "build_column_mapping",
    "probe_channel_file",
    "read_bill_file",
    "read_inbox_dir",
    "resolve_inbox_root",
]


#: 兼容视图：**契约列名 → 该列的首选表头写法**。
#: 真源是 ``format_probe.HEADER_ALIASES``（每列一个**候选列表**）；这里取
#: 每个候选列表的第一项，供只关心单一写法的调用方使用。
#: 源产品同类职责是数据源的 ``field_mapping``（见
#: ``github_ref/soda-core/src/soda_core/model/data_source/
#: data_source_connection_properties.py`` 的 ``to_connection_kwargs()``）：
#: 内部叫 ``amount``，外部叫「应结订单金额」。方向相反，别混用。
BILL_HEADER_ALIASES: dict[str, dict[str, str]] = {
    channel: {column: aliases[0] for column, aliases in columns.items()}
    for channel, columns in HEADER_ALIASES.items()
}

#: 金额清洗时剥掉的字符：千分位逗号、货币符号
_CURRENCY_CHARS = "¥￥$＄元RMB人民币 "


def parse_amount(
    text: str,
    *,
    source_file: str = "",
    row_no: Optional[int] = None,
    column: str = "amount",
) -> Optional[Decimal]:
    """把一个金额单元格解析成 ``Decimal``；空值返回 ``None``。

    院内导出常见 ``1,020.00`` / ``￥1020.00`` / ``1020.00元`` / ``(86.00)``
    （会计括号负数）。这些先洗掉再解析。**解析失败抛
    ``AMOUNT_UNPARSABLE`` 并带行号** —— 绝不 ``float()`` 兜底，
    金额走 float 是对账事故的经典起点。
    """
    if text is None:
        return None
    raw = str(text).strip()
    if raw == "" or raw in {"-", "--"}:
        return None
    cleaned = raw
    for ch in _CURRENCY_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.replace(",", "").replace("，", "").strip()
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1].strip()
    if cleaned in {"", "-", "+"}:
        raise ReconqError(
            "AMOUNT_UNPARSABLE",
            f"金额列 {column} 只有符号没有数字：{raw!r}",
            row_ref=f"{source_file}:{row_no}" if row_no else source_file,
        )
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ReconqError(
            "AMOUNT_UNPARSABLE",
            f"金额列 {column} 的值不是合法数字：{raw!r}（清洗后 {cleaned!r}）",
            row_ref=f"{source_file}:{row_no}" if row_no else source_file,
        ) from exc
    return -value if negative else value


def build_column_mapping(
    header: list[str],
    table: TableYaml,
    channel: str,
) -> dict[str, str]:
    """把文件表头映射成「契约列名 → 文件表头名」（转发到 ``format_probe``）。

    映射不到的契约列不算错 —— 那是「这份账单本来就没这一列」，由
    not_empty / 缺列检查在 check 阶段报，或由 ``probe_channel_file()``
    的 ``column_mapping`` 字段作为格式证据留痕。
    """
    mapping, _ = resolve_columns(header, table, channel)
    return mapping


def read_bill_file(
    path: str,
    table: TableYaml,
    channel: str,
    *,
    delimiter: Optional[str] = None,
) -> list[BillRow]:
    """读一份账单文件，返回带源行号的行列表（转发到 ``format_probe``）。

    金额列（契约声明 ``data_type: decimal``，或落在 ``DECIMAL_COLUMNS``）
    统一洗成 ``Decimal`` 后再写回字符串字面量，保证下游拿到的是
    ``"128.00"`` 而不是浮点残影。``row_no`` 是**源文件真实行号**
    （1 基，含抬头偏移），财务科拿它回去找导出方。

    需要这份文件的格式证据（编码 / 分隔符 / 表头行号 / 别名命中情况）时
    改用 ``format_probe.probe_channel_file()``。
    """
    rows, _ = read_bill_file_probed(path, table, channel, delimiter=delimiter)
    return rows


def read_inbox_dir(
    inbox_root: str,
    period: str,
    channel: str,
    table: TableYaml,
) -> list[BillRow]:
    """读 ``<inbox_root>/<period>/<channel>/`` 下全部账单文件。"""
    directory = os.path.join(inbox_root, period, channel)
    if not os.path.isdir(directory):
        return []
    files = sorted(
        name
        for name in os.listdir(directory)
        if name.lower().endswith((".csv", ".txt")) and not name.startswith(".")
    )
    if not files:
        return []
    rows: list[BillRow] = []
    for name in files:
        rows.extend(read_bill_file(os.path.join(directory, name), table, channel))
    return rows


def resolve_inbox_root(source: Optional[str]) -> str:
    """``--source`` 默认取工作目录下的 ``inbox/``。"""
    return source or "inbox"
