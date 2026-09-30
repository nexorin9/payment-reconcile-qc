"""生成 Task 10 的格式探测夹具矩阵。

**6 种编码 × 4 种分隔符 × 2 种表头别名写法 = 48 份**脱敏样例，
外加「说明行 / 空行 / 脏行」三类尾部样例。

跑法（在项目根目录下）::

    python3 tests/fixtures/format_matrix/generate_matrix.py

生成的目录结构::

    tests/fixtures/format_matrix/
    ├── <编码>__<分隔符>__<别名写法>__<渠道>.txt
    └── ...

编码覆盖：BOM 的 UTF-8、裸 UTF-8、GB18030、GBK、UTF-16、兜底 GBK 替换解码
（最后一档只能由「字节故意写坏」的样例触发，因此单独放在 ``broken/``）。
分隔符覆盖：逗号、制表符、竖线、分号。
别名写法覆盖：「官方表头」与「院内老系统别名」两套同义写法
（如「商户订单号」vs「订单号」、「应结订单金额」vs「金额(元)」）。

金额与单号一律用占位/类目，不含患者可识别明细。
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))

#: 6 种编码档位。``gbk_broken`` 的字节是故意写坏的，只能落到兜底解码。
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "gbk_broken")

#: 4 种分隔符
DELIMITERS = (",", "\t", "|", ";")

#: 2 种表头别名写法：(名字, {契约列: 该写法下的表头})
HEADER_STYLES = (
    (
        "official",
        {
            "wechat": ["商户订单号", "微信交易单号", "交易状态", "应结订单金额", "交易时间", "退款金额"],
            "unionpay": ["订单号", "银联交易单号", "交易时间", "交易金额", "结算方式", "是否退款"],
            "self_service": ["收费记录号", "终端号", "缴费时间", "收费金额", "结算方式", "是否退费"],
        },
    ),
    (
        "legacy",
        {
            # 院内老系统导出的同义写法：订单号 / 金额(元) / 交易时间
            "wechat": ["订单号", "交易单号", "订单状态", "金额(元)", "支付时间", "退款金额(元)"],
            "unionpay": ["商户订单号", "交易流水号", "交易日期", "金额(元)", "支付方式", "是否退费"],
            "self_service": ["订单号", "终端编号", "交易时间", "金额(元)", "支付方式", "是否退费"],
        },
    ),
)

#: 每路 3 行样例数据（占位单号 + 定点金额）
ROWS = {
    "wechat": [
        ["EX-WX-0001", "EX-WXTX-0001", "SUCCESS", "128.00", "2026-09-01 09:10:11", ""],
        ["EX-WX-0002", "EX-WXTX-0002", "SUCCESS", "56.50", "2026-09-02 10:11:12", ""],
        ["EX-WX-0003", "EX-WXTX-0003", "REFUND", "86.00", "2026-09-03 11:12:13", "86.00"],
    ],
    "unionpay": [
        ["EX-UPL-0001", "EX-UPTRX-0001", "2026-09-01 12:00:01", "113.00", "银行卡", "否"],
        ["EX-UPL-0002", "EX-UPTRX-0002", "2026-09-02 12:00:02", "126.00", "银行卡", "否"],
        ["EX-UPL-0003", "EX-UPTRX-0003", "2026-09-03 12:00:03", "139.00", "现金", "否"],
    ],
    "self_service": [
        ["EX-SS-0001", "EXAM-TERM-02", "2026-09-01 08:00:01", "77.00", "现金", "否"],
        ["EX-SS-0002", "EXAM-TERM-03", "2026-09-02 08:00:02", "154.00", "微信", "否"],
        ["EX-SS-0003", "EXAM-TERM-02", "2026-09-03 08:00:03", "231.00", "银行卡", "否"],
    ],
}

#: 尾部说明行（含一个非空格 + 说明用语）与纯空行
TRAILER_NOTE = "------------------------支付宝对账结束------------------------"
#: 含数据但列数不符的脏行（比表头少两列）
RAGGED_ROW = ["EX-WX-0099", "EX-WXTX-0099", "SUCCESS", "77.00"]


#: 故意写坏的字节段：GBK 非法双字节 + UTF-16 BOM 片段，
#: 四级编码判定在这段上只能落到 GBK 替换解码（``suspect_encoding=True``）。
BROKEN_TAIL = b"\x81\x30\x81\xfe\x81\x9b\x0b\xff\xfe\x81\x40"


def _encode(text: str, encoding: str, *, delimiter: str = ",") -> bytes:
    if encoding == "gbk_broken":
        # 表头仍按本组合的分隔符写，保证「坏编码」与「分隔符」两个变量独立
        head = text.splitlines()[0].encode("gb18030")
        return head + b"\n" + BROKEN_TAIL + b"\n" + b"EX-WX-0001" + delimiter.encode() + b"1.00\n"
    return text.encode(encoding)


def _matrix_text(channel: str, header: list[str], delimiter: str) -> str:
    rows = ROWS[channel]
    return delimiter.join(header) + "\n" + "\n".join(delimiter.join(r) for r in rows) + "\n"


def _trailer_text(channel: str, header: list[str], delimiter: str) -> str:
    """带抬头 + 说明行 + 空行 + 尾部说明行的样例。"""
    rows = ROWS[channel]
    lines = [
        "院内自助机缴费明细导出（占位导出方）",
        "账期：2026-W36    制表：占位导出方    打印时间：2026-09-07 06:00:00",
        "",
        delimiter.join(header),
    ]
    lines += [delimiter.join(r) for r in rows]
    lines += ["", TRAILER_NOTE, "共计 3 笔"]
    return "\n".join(lines) + "\n"


def _ragged_text(channel: str, header: list[str], delimiter: str) -> str:
    rows = ROWS[channel]
    lines = [delimiter.join(header)]
    lines += [delimiter.join(r) for r in rows[:1]]
    lines.append(delimiter.join(RAGGED_ROW))
    lines.append(delimiter.join(rows[1]))
    return "\n".join(lines) + "\n"


def generate() -> list[str]:
    written: list[str] = []
    matrix_dir = os.path.join(HERE, "matrix")
    trailer_dir = os.path.join(HERE, "trailer")
    ragged_dir = os.path.join(HERE, "ragged")
    for d in (matrix_dir, trailer_dir, ragged_dir):
        os.makedirs(d, exist_ok=True)

    for encoding in ENCODINGS:
        for delimiter in DELIMITERS:
            delim_name = {",": "comma", "\t": "tab", "|": "pipe", ";": "semi"}[delimiter]
            for style_name, style_map in HEADER_STYLES:
                for channel, header in style_map.items():
                    text = _matrix_text(channel, header, delimiter)
                    name = f"{encoding}__{delim_name}__{style_name}__{channel}.txt"
                    with open(os.path.join(matrix_dir, name), "wb") as fh:
                        fh.write(_encode(text, encoding, delimiter=delimiter))
                    written.append(os.path.join("matrix", name))

    # 尾部样例只造一套（utf-8 + 逗号 + official），专项验证说明行/空行/脏行
    for channel, header in HEADER_STYLES[0][1].items():
        with open(os.path.join(trailer_dir, f"{channel}_trailer.csv"), "wb") as fh:
            fh.write(_trailer_text(channel, header, ",").encode("utf-8"))
        written.append(os.path.join("trailer", f"{channel}_trailer.csv"))
        with open(os.path.join(ragged_dir, f"{channel}_ragged.csv"), "wb") as fh:
            fh.write(_ragged_text(channel, header, ",").encode("utf-8"))
        written.append(os.path.join("ragged", f"{channel}_ragged.csv"))
    return written


if __name__ == "__main__":
    files = generate()
    print(f"已生成 {len(files)} 份夹具 → {HERE}")
