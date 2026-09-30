"""周对账结论单导出（Task 44）。

财务科要签字归档的是**一份纸面结论单**，不是终端滚动过的输出。
本包把「结论集」渲染成两种可归档格式：

* :mod:`reconq.export.conclusion` 的 Markdown 版 —— 供打印签字；
* 同一份结论集的 CSV 版 —— 供财务科回填既有电子台账。

两者出自同一个 :class:`~reconq.export.conclusion.ConclusionDoc`，
所以不会出现「Markdown 说 3 张单、CSV 里 4 行」这种对不上的情况。
"""

from __future__ import annotations

from .conclusion import (
    CONCLUSION_FORMATS,
    ConclusionDoc,
    ExportOutcome,
    build_conclusion,
    export_conclusion,
    render_conclusion_csv,
    render_conclusion_markdown,
)

__all__ = [
    "CONCLUSION_FORMATS",
    "ConclusionDoc",
    "ExportOutcome",
    "build_conclusion",
    "export_conclusion",
    "render_conclusion_csv",
    "render_conclusion_markdown",
]
