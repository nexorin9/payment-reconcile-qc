"""reconq 命令行入口。

源产品主路径（参考 ``github_ref/soda-core/soda-core/src/soda_core/cli/cli.py``）：

    soda contract verify -c contract.yml -ds ds.yml [--check-filter ...]
      → 读契约 → 逐 check 执行 → 打印失败证据 → exit_with_code(非零即门禁)

本产品把同一主路径搬到六路渠道账单对账上：

    reconq verify --period 2026-W36 --source inbox
      → 读 contracts/<channel>.yml → 取数 → 逐口径执行 → 打印证据行号 → 退出码

``data-source`` / ``export`` / ``serve`` 三条腿已在 Task 47 全部接上
（``serve`` 是常驻服务 + SSE 实时面板，见 :mod:`reconq.web`）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unicodedata
from decimal import Decimal
from typing import Optional

from . import __version__
from .contracts.loader import load_contract, parse_variable_assignments
from .export.conclusion import CONCLUSION_FORMATS, export_conclusion
from .contracts.selector import CheckSelector, filter_checks
from .engine.checks import (
    SUPPORTED_OPERATORS,
    BillRow,
    CheckContext,
    CheckResult,
    run_checks,
)
from .errors import ReconqError
from .runlog import log_event
from .run_summary import ChannelRun, build_run_summary
from .sources.inbox_files import read_inbox_dir, resolve_inbox_root
from .store.quarantine import DEFAULT_DIRTY_RATE_THRESHOLD, REASON_ZH

#: 后续任务逐步接线的子命令
SUBCOMMANDS = (
    "verify",
    "scan",
    "data-source",
    "quarantine",
    "contract",
    "trace",
    "align-metrics",
    "align-pipeline",
    "model-usage",
    "attrib-replay",
    "attrib-eval",
    "groups",
    "export",
    "serve",
)

#: 与参考产品 ``exit_with_code()`` 同构的门禁退出码
EXIT_OK = 0
EXIT_CHECKS_FAILED = 1
EXIT_ERROR = 2


def _peak_rss_kb() -> int:
    """进程常驻内存峰值（KB）。``run_summary`` 的字段真源在那边，这里薄转发。"""
    from .run_summary import current_peak_rss_kb

    return current_peak_rss_kb()

def _pad(text: str, width: int, *, right: bool = False) -> str:
    """按**显示宽度**补齐（中文按 2 列算）。

    ``f"{'渠道延迟入账':<14}"`` 这种按字符数补齐的写法在含中文的表格上
    必然错位：中文一个字占两列终端宽度，而 Python 只数它是一个字符。
    终端表格错位比不画表格更糟 —— 看的人会以为列与列之间有语义关系。
    """
    body = str(text)
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in body)
    filler = " " * max(0, width - shown)
    return filler + body if right else body + filler


#: 账单库默认路径（隔离区与账单表同库 —— 两者必须在同一次扫描里对齐）
DEFAULT_DB = os.path.join("data", "reconq.db")

#: 连坐追查默认窗口半径（中心账期上下各几期，共 2N+1 期）
DEFAULT_TRACE_WINDOW = 2

#: ``reconq trace`` 不给 --period/--periods 时的中心账期
DEFAULT_TRACE_PERIOD = "2026-W36"

#: ``reconq align-metrics`` 的默认金标集路径
DEFAULT_GOLDEN_SET = os.path.join("tests", "fixtures", "align", "golden.jsonl")

#: ``reconq attrib-eval`` 的默认评测集目录
DEFAULT_ATTRIB_EVAL_DIR = os.path.join("tests", "fixtures", "attrib", "golden")

#: ``reconq align-golden`` 的默认回归集路径
DEFAULT_ALIGN_CASES = os.path.join("tests", "fixtures", "align", "cases.yml")

_HELP = {
    "verify": "按契约校验一个账期的六路账单",
    "scan": "扫描一个账期：逐路取数、落库、跑口径，并把这期账推进到可结/不可结",
    "data-source": "检查某一路数据源的凭据与连通性",
    "quarantine": "脏行隔离区：扫描、列出、导出台账",
    "contract": "契约口径指纹：落快照、看变更影响面、回归基线对拍",
    "trace": "连坐追查：把同一单号在相邻账期与全部六路上的出现串成一条链",
    "align-metrics": "对齐质量指标：在人工标注金标集上与「逐笔 VLOOKUP 对」基线同口径对比",
    "model-usage": "模型成本与命中：这一期调了几次、命中多少缓存、花了多少 token 与钱",
    "attrib-replay": "归因离线回放：某版提示词在固定证据包上的 Top-1/Top-3 命中率，与两版逐条对比",
    "attrib-eval": "归因效果评测：模型档 vs 规则基线档的 Top-1/Top-3 命中率、反幻觉拒绝率与成本",
    "align-golden": "对齐黄金样例回归：按形态跑回归集，报告哪些原来判对的被这次改动弄坏了",
    "groups": "同根因聚合：把逐条单边账收成按成因分组的处置单，并按单确认/驳回/撤回",
    "align-pipeline": "对齐主路径：一次跑完对齐/分型/连坐/证据包/归因/核对/聚合，出开工前待办汇总",
    "precedent": "先例库：把人工确认过的成因沉淀成下次同类差异的建议，并查历史最常见成因",
    "export": "导出周对账结论单（md 打印签字 / csv 回填既有电子台账，带指纹头）",
    "serve": "启动常驻服务与浏览器实时面板（127.0.0.1:8788，SSE 实时推送）",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconq",
        description="支付对账数据质检系统 — 六路渠道账单与 HIS 收费流水的契约化对账",
    )
    parser.add_argument("--version", action="version", version=f"reconq {__version__}")
    parser.add_argument(
        "--log-dir",
        default=None,
        help=(
            "结构化 JSON 日志目录（默认 round 根 logs/，可用 RECONQ_LOG_DIR 覆盖）。"
            "日志不写项目子目录——项目子目录是要直接发布到 GitHub 的"
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="JSON 日志级别（默认 INFO）",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="本次不落 JSON 日志（排障时想看干净终端可用）",
    )
    sub = parser.add_subparsers(dest="command", metavar="<子命令>")

    verify = sub.add_parser("verify", help=_HELP["verify"], description=_HELP["verify"])
    verify.add_argument("--period", required=True, help="账期，如 2026-W36")
    verify.add_argument(
        "--source",
        default=None,
        help="账单文件通道根目录（默认 ./inbox），布局为 <source>/<period>/<channel>/",
    )
    verify.add_argument(
        "--contracts",
        default="contracts",
        help="契约目录（默认 ./contracts）",
    )
    verify.add_argument(
        "--channel",
        action="append",
        default=None,
        help="只跑指定渠道，可重复；不传则跑目录下全部渠道",
    )
    verify.add_argument(
        "--check",
        default=",".join(SUPPORTED_OPERATORS),
        help=(
            "本次要跑的口径算子，逗号分隔"
            f"（本轮已实现：{', '.join(SUPPORTED_OPERATORS)}；"
            "其余算子会显式报未实现，不会静默当作通过）"
        ),
    )
    verify.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读摘要（供信息科贴进工单）",
    )
    verify.add_argument(
        "--lint",
        action="store_true",
        help="只做契约静态校验就退出，不取数（写完契约先跑这条）",
    )
    verify.add_argument(
        "--resume",
        action="store_true",
        help=(
            "只跑上次没跑完的渠道（需 --db 指向账单库读断点）。"
            "verify 是只读排障腿：续跑只影响本次查哪几路，不写任何状态"
        ),
    )
    verify.add_argument(
        "--only-channel",
        action="append",
        default=None,
        metavar="渠道",
        help="只查指定渠道，可重复（语义同 --channel，用于与 --only-check 配对表达重跑范围）",
    )
    verify.add_argument(
        "--only-check",
        action="append",
        default=None,
        metavar="选择器",
        help=(
            "只跑匹配该选择器的口径，可重复。表达式复用契约引擎的选择器语法："
            "row_count（数据集级路径）/ type=not_empty（按算子）/ "
            "transaction_id.*（按列通配）；多条件之间是「或」"
        ),
    )
    verify.add_argument(
        "--set",
        dest="variables",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="向契约注入变量，可重复",
    )
    verify.add_argument(
        "--bind-contract",
        action="store_true",
        help=(
            "把「本期这一路是按哪一版口径跑的」写进账单库（需 --db）。"
            "口径指纹日后一变，该账期自动标 needs_reconfirm，不沿用旧确认结论"
        ),
    )
    verify.add_argument(
        "--db",
        default=None,
        help=(
            f"账单库路径（默认 {DEFAULT_DB}），仅 --bind-contract 与 --replay 时使用。"
            "不传则 verify 保持只读排障腿，不写任何状态"
        ),
    )
    verify.add_argument(
        "--replay",
        default=None,
        metavar="结论键",
        help=(
            "复算某条对齐结论（键形如 微信:2026-W36:EX-WX-0001）。"
            "只用证据快照里记的候选集重跑判定，不读账单表 —— "
            "源数据已被重拉时，结论单上那条差异仍要能被独立复算一遍。"
            "需同时给 --db 指向存了快照的账单库"
        ),
    )
    verify.add_argument(
        "--replay-list",
        action="store_true",
        help="列出本期已拍的全部证据快照（结论键、类型、快照哈希），不跑口径",
    )

    scan = sub.add_parser("scan", help=_HELP["scan"], description=_HELP["scan"])
    scan.add_argument("--period", required=True, help="账期，如 2026-W36")
    scan.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    scan.add_argument("--source", default=None, help="账单文件通道根目录（默认 ./inbox）")
    scan.add_argument("--contracts", default="contracts", help="契约目录（默认 ./contracts）")
    scan.add_argument(
        "--channel", action="append", default=None, help="只扫指定渠道，可重复"
    )
    scan.add_argument("--set", dest="variables", action="append", default=None, metavar="KEY=VALUE",
                      help="向契约注入变量，可重复")
    scan.add_argument(
        "--reopen",
        action="store_true",
        help="人工撤回：把已确认/已存疑的账期退回「待确认」以便重扫（唯一能让已确认账期重扫的路径）",
    )
    scan.add_argument(
        "--resume",
        action="store_true",
        help=(
            "从断点继续：上次已跑完的渠道不重新取数、不重新落库，"
            "其口径结果标「沿用上次」并在结论单上与本次产出分开"
        ),
    )
    scan.add_argument(
        "--only-check",
        action="append",
        default=None,
        metavar="选择器",
        help=(
            "只重跑匹配该选择器的口径，可重复（row_count / type=not_empty / "
            "transaction_id.*）。未重跑到的口径标「沿用上次」"
        ),
    )
    scan.add_argument(
        "--actor",
        default=os.environ.get("RECONQ_ACTOR", ""),
        help="本次重跑的操作人，写进事件链（默认取环境变量 RECONQ_ACTOR）",
    )
    scan.add_argument("--json", action="store_true", help="输出机器可读摘要")
    scan.add_argument(
        "--status",
        action="store_true",
        help="只读地回答「这期账现在到哪一步」，不扫描",
    )

    data_source = sub.add_parser("data-source", help=_HELP["data-source"])
    ds_sub = data_source.add_subparsers(dest="data_source_cmd", metavar="<动作>")
    ds_test = ds_sub.add_parser(
        "test",
        help="检查某一路数据源的凭据位与协议连通性",
        description="逐项检查凭据位存在性与协议连通性，缺变量时点名是哪个环境变量",
    )
    ds_test.add_argument(
        "channel", nargs="?", help="渠道名（wechat/alipay/unionpay/self_service/his/medical_ins）"
    )
    ds_test.add_argument(
        "--all", action="store_true", help="逐路检查全部六路，输出汇总表"
    )
    ds_test.add_argument("--json", action="store_true", help="输出机器可读结果")
    ds_probe = ds_sub.add_parser(
        "probe",
        help="探测账单文件的格式（编码/分隔符/表头别名/脏行），输出格式证据",
        description="只探不取：交出 {encoding, delimiter, header_line_no, column_mapping, "
        "row_count, suspect_flags}，供信息科判断「这路为什么对不上」是格式问题还是数据问题",
    )
    ds_probe.add_argument("channel", help="渠道名（wechat/alipay/unionpay/self_service/his/medical_ins）")
    ds_probe.add_argument("--period", default="2026-W36", help="账期（默认 2026-W36）")
    ds_probe.add_argument("--source", default="inbox", help="账单根目录（默认 inbox）")
    ds_probe.add_argument("--contracts", default="contracts", help="契约目录（默认 contracts）")
    ds_probe.add_argument("--json", action="store_true", help="输出机器可读结果")

    usage = sub.add_parser(
        "model-usage",
        help=_HELP["model-usage"],
        description=_HELP["model-usage"]
        + "。**只读**，不调模型、不改台账。审计链按账期分，"
        "改过或删过的记录会被校验出来并返回非零退出码。",
    )
    usage.add_argument(
        "--period", required=True, help="账期（如 2026-W36）"
    )
    usage.add_argument(
        "--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）"
    )
    usage.add_argument(
        "--calls", action="store_true",
        help="附带逐次调用明细（默认只出成本聚合表）",
    )
    usage.add_argument(
        "--no-chain-check", action="store_true",
        help="跳过审计链指纹校验（排障腿：只想看数字时的快路径）",
    )
    usage.add_argument("--json", action="store_true", help="输出机器可读结果")

    replay = sub.add_parser(
        "attrib-replay",
        help=_HELP["attrib-replay"],
        description=_HELP["attrib-replay"]
        + "。**离线**：跑的是回放集里录好的模型响应，不调模型、不花钱、"
        "不写任何台账。评的是「这版提示词 + 这批录好的响应」经完整后处理链"
        "（Schema → 证据引用越界 → 反幻觉核对 → 受控字典）之后的结果。",
    )
    replay.add_argument(
        "--version", dest="prompt_version", default=None,
        help="要回放的提示词版本（默认当前生效版）",
    )
    replay.add_argument(
        "--compare", default="",
        help="两版逐条对比，格式 <旧版本>,<新版本>（如 v1,v2）。"
        "新版本出现「由对变错」的用例时给出告警并返回非零退出码",
    )
    replay.add_argument(
        "--set", dest="replay_dir", default="tests/fixtures/attrib",
        help="回放集目录（默认 tests/fixtures/attrib）",
    )
    replay.add_argument(
        "--list-versions", action="store_true",
        help="只列出已登记的提示词版本与各自的变更说明，不跑回放",
    )
    replay.add_argument("--json", action="store_true", help="输出机器可读结果")

    export = sub.add_parser("export", help=_HELP["export"])
    export.add_argument("--period", required=True, help="账期，如 2026-W36")
    export.add_argument(
        "--fmt", default="md", choices=list(CONCLUSION_FORMATS),
        help="结论单格式：md 供打印签字，csv 供回填既有电子台账（默认 md）",
    )
    export.add_argument(
        "--out", default="",
        help="输出路径（默认 out/<账期>-conclusion.<fmt>）",
    )
    export.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    export.add_argument("--operator", default="", help="导出人（工号或姓名，印在结论单上）")
    export.add_argument("--note", default="", help="导出备注（印在结论单上）")
    export.add_argument(
        "--amount-bucket", default="0.01",
        help="分组金额分桶宽度（默认 0.01 元精确分桶，不同金额不并单）",
    )
    export.add_argument(
        "--audit-dir", default="",
        help="审计事件链目录（存 events.jsonl，默认不建链则不显示锚点）",
    )
    export.add_argument("--json", action="store_true", help="输出机器可读导出摘要")
    serve = sub.add_parser("serve", help=_HELP["serve"], description=_HELP["serve"])
    serve.add_argument(
        "--host",
        default=os.environ.get("RECONQ_SERVE_HOST", "127.0.0.1"),
        help="监听地址（默认 127.0.0.1；面板显示金额与单号，绑 0.0.0.0 等于把整本账挂到院内网上）",
    )
    serve.add_argument(
        "--port", type=int, default=int(os.environ.get("RECONQ_SERVE_PORT", "8788")),
        help="监听端口（默认 8788）",
    )
    serve.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    serve.add_argument(
        "--poll-seconds", type=float, default=1.0,
        help="账单库轮询间隔秒数（SSE 的数据源；默认 1.0）",
    )
    serve.add_argument(
        # **刻意不叫 ``--log-level``**：顶层解析器已经有一个同名参数
        # （控制 JSON 日志级别）。两个 ``dest`` 同名时 argparse 让**子解析器
        # 赢**，于是 ``reconq serve --log-level warning`` 会顺手把结构化
        # 日志降到 WARNING，而 ``run.start`` 是 INFO —— 结果是「服务起没起、
        # 缺哪个环境变量」那几行永远不落盘，且没有任何报错。两个日志级别
        # 是两件事，参数名也该是两件事。
        "--uvicorn-log-level",
        dest="uvicorn_log_level",
        default="info",
        help="uvicorn 访问日志级别（默认 info；不影响结构化 JSON 日志，那是顶层 --log-level）",
    )

    groups = sub.add_parser("groups", help=_HELP["groups"])
    groups.add_argument(
        "groups_cmd",
        nargs="?",
        default="list",
        choices=["list", "confirm", "reject", "withdraw", "slip", "actions"],
        help="默认 list：按同根因把单边账收成处置单（其它动作见各自 help）",
    )
    groups.add_argument("--period", default="", help="账期（如 2026-W36）")
    groups.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    groups.add_argument(
        "--group-key", default="",
        help="处置单键（list/slip 留空表示全部；confirm/reject/withdraw 必填）。"
        "键形如 channel_late_posting::wechat::cross_period::50.00",
    )
    groups.add_argument(
        "--operator", default="",
        help="操作人工号或姓名。**必填且必须是自然人** —— 模型草稿、规则基线与"
        "降级路径都不得推进处置状态",
    )
    groups.add_argument("--note", default="", help="处置备注（写进留痕表）")
    groups.add_argument(
        "--amount-bucket", default="0.01",
        help="金额分桶宽度（元，默认 0.01 = 精确到分，不同金额绝不并单；"
        "放宽到 100 表示按 0~100 / 100~200 这样的档位归并同类项）",
    )
    groups.add_argument(
        "--only-channel", default="", help="只看某一路渠道"
    )
    groups.add_argument(
        "--include-resolved", action="store_true",
        help="连复扫后已消解的差异一并成单（默认只看还活着的差异）",
    )
    groups.add_argument(
        "--out", default="", help="slip 子命令的输出文件（默认打到标准输出）"
    )
    groups.add_argument("--json", action="store_true", help="输出机器可读结果")

    quarantine = sub.add_parser("quarantine", help=_HELP["quarantine"])
    q_sub = quarantine.add_subparsers(dest="quarantine_cmd", metavar="<动作>")
    q_scan = q_sub.add_parser(
        "scan",
        help="扫描某账期账单，脏行进隔离区、干净行进账单表",
        description="按渠道扫描账单文件：列数不符 / 金额不可解析 / 时间不可解析 / "
        "主键列为空 / 单号重复的行进隔离区（带源文件行号与原文），其余行正常入库。"
        f"脏行率超过 {int(DEFAULT_DIRTY_RATE_THRESHOLD * 100)}% 时整文件判 FORMAT_BROKEN，"
        "该渠道本期标未取到",
    )
    q_scan.add_argument("--period", required=True, help="账期，如 2026-W36")
    q_scan.add_argument(
        "--channel", action="append", default=None,
        help="只扫指定渠道，可重复；不传则扫 contracts/ 下全部渠道",
    )
    q_scan.add_argument("--source", default="inbox", help="账单根目录（默认 inbox）")
    q_scan.add_argument("--contracts", default="contracts", help="契约目录（默认 contracts）")
    q_scan.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    q_scan.add_argument(
        "--rate-threshold", type=float, default=DEFAULT_DIRTY_RATE_THRESHOLD,
        help=f"脏行率阈值（默认 {DEFAULT_DIRTY_RATE_THRESHOLD}）",
    )
    q_scan.add_argument("--json", action="store_true", help="输出机器可读结果")
    q_list = q_sub.add_parser(
        "list", help="列出隔离区脏行（面板主态的同源视图）", description="列出某账期的脏行隔离记录"
    )
    q_list.add_argument("--period", required=True, help="账期，如 2026-W36")
    q_list.add_argument("--channel", default=None, help="只列某渠道")
    q_list.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    q_list.add_argument("--json", action="store_true", help="输出机器可读结果")
    q_export = q_sub.add_parser(
        "export", help="导出脏行台账 CSV（财务科拿去回查导出方）",
        description="把隔离区导出成带 BOM 的 CSV，Excel 双击即开；"
        "每行含渠道、源文件、**源文件行号**、原因码与原始行",
    )
    q_export.add_argument("--period", required=True, help="账期，如 2026-W36")
    q_export.add_argument("--channel", default=None, help="只导某渠道")
    q_export.add_argument("--out", default="", help="输出 CSV 路径（默认 out/<账期>-脏行台账.csv）")
    q_export.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")

    contract = sub.add_parser("contract", help=_HELP["contract"])
    c_sub = contract.add_subparsers(dest="contract_cmd", metavar="<动作>")
    c_snapshot = c_sub.add_parser(
        "snapshot",
        help="记录当前契约的指纹快照（口径变更比对的第一份「上次」）",
        description="把 contracts/<channel>.yml 的规范化内容记成一份快照："
        "digest 计入说明文字与版本号，check_digest 只算会改变结论的部分。"
        "同一份内容重复记录是幂等的，不会把快照表撑成流水账。",
    )
    c_snapshot.add_argument(
        "--channel", action="append", default=None,
        help="只记指定渠道，可重复；不传则记 contracts/ 下全部渠道",
    )
    c_snapshot.add_argument("--contracts", default="contracts", help="契约目录（默认 contracts）")
    c_snapshot.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    c_snapshot.add_argument(
        "--list", action="store_true", help="只列该渠道已有的快照历史，不写入"
    )
    c_snapshot.add_argument("--json", action="store_true", help="输出机器可读结果")
    c_diff = c_sub.add_parser(
        "diff",
        help="对比工作区当前契约与最近快照：改了什么、影响哪几路、要不要重新确认",
        description="口径本身会改（渠道换列名、医院加一条本地口径）。"
        "本命令回答「相对上次改了什么、哪些 check 的参数变了、"
        "本期结论要不要打回待确认」——只改说明文字与版本号不惊动已确认的差异。",
    )
    c_diff.add_argument("--channel", action="append", default=None,
                        help="只比对指定渠道，可重复；不传则比对全部渠道")
    c_diff.add_argument("--contracts", default="contracts", help="契约目录（默认 contracts）")
    c_diff.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    c_diff.add_argument(
        "--record", action="store_true",
        help="比对完顺手把当前契约记为新快照：让「上一份快照」始终是上一次"
        "确认过的口径（内容没变时幂等，不产生重复快照）",
    )
    c_diff.add_argument("--json", action="store_true", help="输出机器可读结果")
    c_baseline = c_sub.add_parser(
        "baseline",
        help="把某账期某渠道的 check 结果记为回归基线，或与已有基线对拍",
        description="基线回答「改完口径重跑，结论会不会翻过来」。"
        "不给 --record 时只对拍：列出 pass↔fail 翻转的 check。",
    )
    c_baseline.add_argument("--period", required=True, help="基线账期，如 2026-W36")
    c_baseline.add_argument("--channel", required=True, help="渠道名")
    c_baseline.add_argument("--source", default="inbox", help="账单根目录（默认 inbox）")
    c_baseline.add_argument("--contracts", default="contracts", help="契约目录（默认 contracts）")
    c_baseline.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    c_baseline.add_argument(
        "--record", action="store_true",
        help="把本次结果记为基线（覆盖同账期同渠道的旧基线）",
    )
    c_baseline.add_argument("--json", action="store_true", help="输出机器可读结果")
    c_status = c_sub.add_parser(
        "status",
        help="列出因口径变更而需要重新确认的账期（面板顶部提示的同源视图）",
        description="口径指纹一变，该账期这一路的旧确认结论不再自动有效。"
        "本命令只读展示这些标记；清标记必须由人工显式确认，"
        "没有任何自动路径会替财务科点掉它。",
    )
    c_status.add_argument("--period", default=None, help="只看某账期；不传则看全部")
    c_status.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    c_status.add_argument("--json", action="store_true", help="输出机器可读结果")

    trace = sub.add_parser("trace", help=_HELP["trace"])
    trace.add_argument(
        "--order", required=True, help="要追查的渠道单号（如 EX-WX-SPLIT-0001）"
    )
    trace.add_argument(
        "--period", default="", help="中心账期（与 --periods 二选一；给了就按它展开窗口）"
    )
    trace.add_argument(
        "--periods", default="",
        help="显式账期列表，逗号分隔（如 2026-W35,2026-W36）；给了就以它为追查范围",
    )
    trace.add_argument(
        "--window", type=int, default=DEFAULT_TRACE_WINDOW,
        help=f"追查窗口半径：中心账期上下各展开几期（默认 {DEFAULT_TRACE_WINDOW}）",
    )
    trace.add_argument(
        "--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）"
    )
    trace.add_argument(
        "--no-index", action="store_true",
        help="排障腿：强制全表扫取数（用于验证复合索引真的被用上了）",
    )
    trace.add_argument("--json", action="store_true", help="输出机器可读结果")

    align_metrics = sub.add_parser(
        "align-metrics",
        help=_HELP["align-metrics"],
        description=_HELP["align-metrics"],
    )
    align_metrics.add_argument(
        "--golden",
        default=DEFAULT_GOLDEN_SET,
        help=f"人工标注的对齐金标集 JSONL（默认 {DEFAULT_GOLDEN_SET}）",
    )
    align_metrics.add_argument(
        "--out",
        default="",
        help="把完整指标报告写到该 Markdown 文件（默认只打终端）",
    )
    align_metrics.add_argument(
        "--json", action="store_true", help="输出机器可读指标（两侧全量）"
    )
    align_metrics.add_argument(
        "--cases",
        action="store_true",
        help="逐条列出两侧结论（含金标期望），用于定位「哪一条错了」",
    )
    attrib_eval = sub.add_parser(
        "attrib-eval",
        help=_HELP["attrib-eval"],
        description=_HELP["attrib-eval"],
    )
    attrib_eval.add_argument(
        "--set",
        dest="set_dir",
        default=DEFAULT_ATTRIB_EVAL_DIR,
        help=f"归因评测集目录（默认 {DEFAULT_ATTRIB_EVAL_DIR}）",
    )
    attrib_eval.add_argument(
        "--out",
        default="",
        help="把完整评测报告写到该 Markdown 文件（默认只打终端摘要）",
    )
    attrib_eval.add_argument(
        "--live",
        action="store_true",
        help="实连真实模型端点（需 RECONQ_MODEL_*）；默认回放录制响应替身，不发真实请求",
    )
    attrib_eval.add_argument(
        "--json", action="store_true", help="输出机器可读指标（两档全量）"
    )
    attrib_eval.add_argument(
        "--cases",
        action="store_true",
        help="逐条列出两档结论（含评测集期望），用于定位「哪一条错了」",
    )
    align_golden = sub.add_parser(
        "align-golden",
        help=_HELP["align-golden"],
        description=_HELP["align-golden"],
    )
    align_golden.add_argument(
        "--cases",
        dest="cases_path",
        default=DEFAULT_ALIGN_CASES,
        help=f"对齐黄金样例回归集 YAML（默认 {DEFAULT_ALIGN_CASES}）",
    )
    align_golden.add_argument(
        "--out",
        default="",
        help="把完整回归报告写到该 Markdown 文件（默认只打终端）",
    )
    align_golden.add_argument("--json", action="store_true", help="输出机器可读回归结论（全量）")
    align_golden.add_argument(
        "--cases-only",
        action="store_true",
        help="逐条列出结论与期望，用于定位「哪一条坏了、坏在哪一项」",
    )

    precedent = sub.add_parser(
        "precedent",
        help=_HELP["precedent"],
        description=_HELP["precedent"],
    )
    precedent.add_argument(
        "precedent_cmd",
        nargs="?",
        default="top",
        choices=["top", "list", "hits"],
        help="默认 top：查本渠道历史最常见成因（list 列先例明细，hits 列采信留痕）",
    )
    precedent.add_argument("--period", default="", help="账期（如 2026-W36）；hits 必填")
    precedent.add_argument("--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）")
    precedent.add_argument(
        "--channel",
        default="",
        help="只查某一路渠道（wechat/alipay/unionpay/self_service/his/medical_ins）",
    )
    precedent.add_argument(
        "--cause", default="", help="只查某个成因码（list 子命令）"
    )
    precedent.add_argument(
        "--diff-key", default="", help="只查某条差异的采信留痕（hits 子命令）"
    )
    precedent.add_argument("--limit", type=int, default=5, help="top 子命令取前 N 条（默认 5）")
    precedent.add_argument(
        "--min-hits",
        type=int,
        default=0,
        help="list 子命令只列命中次数不低于该值的先例（默认 0 = 全列）",
    )
    precedent.add_argument("--json", action="store_true", help="输出机器可读结果")

    align_pipeline = sub.add_parser(
        "align-pipeline",
        help=_HELP["align-pipeline"],
        description=_HELP["align-pipeline"],
    )
    align_pipeline.add_argument("--period", required=True, help="账期，如 2026-W36")
    align_pipeline.add_argument(
        "--db", default=DEFAULT_DB, help=f"账单库路径（默认 {DEFAULT_DB}）"
    )
    align_pipeline.add_argument(
        "--contracts", default="contracts", help="契约目录（默认 contracts）"
    )
    align_pipeline.add_argument(
        "--amount-bucket",
        default="0.01",
        help="金额分桶宽度（元，默认 0.01 = 精确到分，不同金额绝不并单）",
    )
    align_pipeline.add_argument(
        "--trace-window",
        type=int,
        default=2,
        help="跨账期连坐追查窗口半径（中心账期上下各几期，默认 2 = 共 5 期）",
    )
    align_pipeline.add_argument(
        "--history-periods",
        type=int,
        default=3,
        help="证据包里近 N 账期同因历史的回看期数（默认 3）",
    )
    align_pipeline.add_argument(
        "--instructions",
        default="",
        help="给归因模型的补充说明（如「本期含自助机批量导出」）",
    )
    align_pipeline.add_argument(
        "--progress", action="store_true", help="逐阶段打印进度（阶段名 / 条数 / 耗时）"
    )
    align_pipeline.add_argument(
        "--stages", action="store_true", help="只打印逐阶段耗时表（哪一段慢）"
    )
    align_pipeline.add_argument("--json", action="store_true", help="输出机器可读结果")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 结构化日志先于任何业务逻辑装好：契约加载失败、账单文件找不到这类
    # 最早期的失败也要在 JSON 日志里留痕，否则「命令什么都没打就退了」
    # 恰恰是最需要日志的那种情况。
    from .runlog import configure_logging

    configure_logging(
        log_dir=getattr(args, "log_dir", None),
        level=getattr(args, "log_level", "INFO"),
        to_file=not getattr(args, "no_log", False),
    )

    if not args.command:
        parser.print_help()
        return EXIT_OK
    return _dispatch(args)


#: 预期内的业务失败：带错误码、中文说明与建议动作，直接打到 stderr。
#: 任何**预期外**的异常都不该以裸栈的形式出现在财务科面前——栈是给开发看的，
#: 而对方看到的是一段 ``File "..." line N``，既读不出是哪一路、也不知道
#: 下一步做什么。统一降级为「已记录完整栈，请把 run_id 与该码给信息科」。
def _handle_error(exc: ReconqError) -> int:
    print(f"reconq: {exc}", file=sys.stderr)
    log_event(
        "error.raised",
        level="error",
        message=exc.render(),
        channel=exc.channel or "",
        code=exc.code,
        cause_code=exc.cause_code,
    )
    return EXIT_ERROR


def _handle_unexpected(exc: BaseException, command: str) -> int:
    """预期外异常的统一出口：记全栈到 JSON 日志，界面上只给可读结论。"""
    import traceback

    log_event(
        "error.raised",
        level="error",
        message=f"{command} 出现未预期异常：{type(exc).__name__}: {exc}",
        code="UNEXPECTED_INTERNAL",
        traceback=traceback.format_exc(),
    )
    print(
        f"reconq: 子命令 {command} 出现未预期异常（{type(exc).__name__}）：{exc}\n"
        f"  完整调用栈已记入 JSON 日志；请把该条 event=error.raised 的记录"
        f"连同 --log-dir 路径一并给信息科。本产品不会用裸栈打扰业务人员。",
        file=sys.stderr,
    )
    return EXIT_ERROR


def _dispatch(args: argparse.Namespace) -> int:
    command = args.command
    try:
        if command == "data-source":
            if getattr(args, "data_source_cmd", None) == "probe":
                return _cmd_data_source_probe(args)
            return _cmd_data_source(args)
        if command == "quarantine":
            return _cmd_quarantine(args)
        if command == "contract":
            return _cmd_contract(args)
        if command == "trace":
            return _cmd_trace(args)
        if command == "align-metrics":
            return _cmd_align_metrics(args)
        if command == "align-golden":
            return _cmd_align_golden(args)
        if command == "align-pipeline":
            return _cmd_align_pipeline(args)
        if command == "model-usage":
            return _cmd_model_usage(args)
        if command == "attrib-replay":
            return _cmd_attrib_replay(args)
        if command == "attrib-eval":
            return _cmd_attrib_eval(args)
        if command == "groups":
            return _cmd_groups(args)
        if command == "export":
            return _cmd_export(args)
        if command == "precedent":
            return _cmd_precedent(args)
        if command == "scan":
            return _cmd_scan(args)
        if command == "serve":
            return _cmd_serve(args)
        if command == "verify":
            return _cmd_verify(args)
    except ReconqError as exc:
        return _handle_error(exc)
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        print("reconq: 已被用户中断", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - 这里就是要兜住一切
        return _handle_unexpected(exc, command)
    print(f"reconq {__version__} · 子命令 '{command}' 已注册，实现见后续 task")
    return EXIT_OK


# —— data-source test ——————————————————————————————————————


def _cmd_data_source(args: argparse.Namespace) -> int:
    """``reconq data-source test <channel> [--all]``。

    对应源产品主路径里的 ``data-source test``（源实现是「先验通不通」，
    免得契约 verify 跑一半才炸在连库上）。本产品同样把「凭据位齐不齐、
    通不通」提前到一条独立命令里，且**缺变量时点名是哪个环境变量**。
    """
    from .sources.registry import CHANNELS, probe_channel

    if not getattr(args, "data_source_cmd", None):
        print("用法：reconq data-source test <渠道> | --all [--json]")
        return EXIT_OK
    if not args.all and not args.channel:
        print("用法：reconq data-source test <渠道> | --all [--json]")
        return EXIT_OK

    targets = list(CHANNELS) if args.all else [args.channel]
    results = [probe_channel(c) for c in targets]
    failed = [r for r in results if not r["ok"]]

    if args.json:
        import json

        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        for res in results:
            mark = "✓" if res["ok"] else "✗"
            envs = res.get("credential_env_vars") or []
            env_txt = f"（凭据位：{'、'.join(envs)}）" if envs else "（文件通道，无需凭据）"
            print(f"{mark} {res['channel']:<14} {res['detail']}{env_txt}")
        if failed:
            print(
                f"\n{len(failed)}/{len(results)} 路不可用："
                + "、".join(f"{r['channel']}({r['error_code']})" for r in failed)
            )
            print("提示：凭据一律从环境变量读，缺哪个就补哪个；确认后重跑本命令。")
        else:
            print(f"\n六路数据源全部可用（{len(results)} 路已连通）")
    return EXIT_CHECKS_FAILED if failed else EXIT_OK


# —— data-source probe ———————————————————————————————————————


def _cmd_data_source_probe(args: argparse.Namespace) -> int:
    """``reconq data-source probe <channel> --period 2026-W36``。

    Task 10 的排障腿：**只探格式、不跑口径**。财务科拿着「这路金额对不上」
    来找信息科时，第一件要分清的是「格式没读对」还是「数据真的对不上」——
    前者表现为行数凭空少、金额差个零头、列名认不出；后者才是对账问题。
    本命令交出 ``{encoding, delimiter, header_line_no, column_mapping,
    row_count, suspect_flags}``，让这一步判断有据可依。
    """
    from .sources.format_probe import probe_channel_file

    contract_path = os.path.join(args.contracts, f"{args.channel}.yml")
    if not os.path.isfile(contract_path):
        raise ReconqError(
            "CONTRACT_MISSING_KEY",
            f"渠道 {args.channel} 没有对应契约文件：{contract_path}",
            channel=args.channel,
        )
    table = load_contract(contract_path, {"period": args.period}).table()
    directory = os.path.join(resolve_inbox_root(args.source), args.period, args.channel)
    if not os.path.isdir(directory):
        raise ReconqError(
            "BILL_FILE_NOT_FOUND",
            f"账单目录不存在：{directory}/",
            channel=args.channel,
        )
    files = sorted(
        name for name in os.listdir(directory)
        if name.lower().endswith((".csv", ".txt")) and not name.startswith(".")
    )
    if not files:
        raise ReconqError(
            "BILL_FILE_NOT_FOUND",
            f"账单目录里没有可探测的文件：{directory}/",
            channel=args.channel,
        )

    probes = []
    problems: list[str] = []
    for name in files:
        probe = probe_channel_file(os.path.join(directory, name), table, args.channel)
        probes.append(probe.as_dict())
        for flag in probe.suspect_flags:
            if flag in ("suspect_encoding", "ragged_rows"):
                problems.append(f"{name}: {flag}")

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "channel": args.channel,
                    "period": args.period,
                    "files": probes,
                    "suspect": problems,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"=== 渠道 {args.channel} · 账期 {args.period} · 格式探测 ===")
        for item in probes:
            name = os.path.basename(item["path"])
            print(f"\n  {name}")
            print(f"    编码    ：{item['encoding']}（{item['encoding_detail']}）")
            print(f"    分隔符  ：{item['delimiter']!r}")
            print(f"    表头行号：第 {item['header_line_no']} 行")
            print(
                f"    数据行数：{item['row_count']}"
                f"（总行 {item['total_line_count']}，"
                f"说明行 {item['note_line_count']}，空行 {item['blank_line_count']}）"
            )
            mapped = "、".join(f"{k}←{v}" for k, v in item["column_mapping"].items())
            print(f"    列映射  ：{mapped or '（无）'}")
            if item["ragged_rows"]:
                where = "、".join(f"第{r['row_no']}行({r['columns']}列)" for r in item["ragged_rows"])
                print(f"    脏行    ：{where}（含数据，不会被静默剔除）")
            if item["suspect_flags"]:
                print(f"    存疑标记：{'、'.join(item['suspect_flags'])}")
        if problems:
            print(f"\n存疑项 {len(problems)} 处：{'、'.join(problems)}")
            print("提示：脏行请交由隔离区逐行回查（reconq quarantine export）；"
                  "编码判据不牢时请让信息科按 UTF-8 或 GBK 重新导出。")
    return EXIT_CHECKS_FAILED if problems else EXIT_OK


# —— quarantine ——————————————————————————————————————————


def _open_store(db_path: str, *, create: bool):
    """打开账单库并确保账单表与隔离表都在。

    隔离区与账单表**同库**：脏行率是「脏行 / (脏行 + 入库行)」，
    两者不在同一个库里就没法在扫描当场算出这个比值。
    """
    if not create and not os.path.isfile(db_path):
        raise ReconqError(
            "BILL_FILE_NOT_FOUND",
            f"账单库不存在：{db_path}；先跑一次 reconq quarantine scan --period <账期>",
        )
    from .store.quarantine import init_dirty_schema
    from .store.schema import connect, init_schema

    conn = connect(db_path)
    init_schema(conn)
    init_dirty_schema(conn)
    return conn


def _cmd_quarantine(args: argparse.Namespace) -> int:
    """``reconq quarantine {scan,list,export}``（Task 11）。"""
    action = getattr(args, "quarantine_cmd", None)
    if action == "scan":
        return _cmd_quarantine_scan(args)
    if action == "list":
        return _cmd_quarantine_list(args)
    if action == "export":
        return _cmd_quarantine_export(args)
    print("用法：reconq quarantine {scan|list|export} ...")
    return EXIT_OK


def _cmd_quarantine_scan(args: argparse.Namespace) -> int:
    """扫描账期账单：脏行隔离、干净行入库、超阈值整文件判坏。"""
    from .store.quarantine import scan_channel_dir

    inbox_root = resolve_inbox_root(args.source)
    conn = _open_store(args.db, create=True)
    try:
        channels = args.channel or _discover_channels(args.contracts)
        results: list[dict] = []
        for channel in channels:
            contract_path = os.path.join(args.contracts, f"{channel}.yml")
            if not os.path.isfile(contract_path):
                raise ReconqError(
                    "CONTRACT_MISSING_KEY",
                    f"渠道 {channel} 没有对应契约文件：{contract_path}",
                    channel=channel,
                )
            table = load_contract(contract_path, {"period": args.period}).table()
            directory = os.path.join(inbox_root, args.period, channel)
            if not os.path.isdir(directory):
                results.append(
                    {
                        "channel": channel,
                        "state": "not_fetched",
                        "error_code": "BILL_FILE_NOT_FOUND",
                        "detail": f"账单目录不存在：{directory}/",
                    }
                )
                continue
            try:
                scans = scan_channel_dir(
                    conn, directory, table, channel, args.period,
                    rate_threshold=args.rate_threshold,
                )
            except ReconqError as exc:
                results.append(
                    {
                        "channel": channel,
                        "state": "not_fetched",
                        "error_code": exc.code,
                        "detail": exc.detail,
                    }
                )
                continue
            for scan in scans:
                item = scan.as_dict()
                results.append(item)

        # 退出码口径：判坏或未取到 → 该路本期不能对账；只是有脏行 → 账能对，
        # 但**合计偏小**，也必须让人看见。两类都给非零，否则财务科把
        # 「19 行入库、1 行隔离」当成「20 行全部对上」签字。
        dirty = [r for r in results if r.get("state") == "dirty"]
        broken = [r for r in results if r.get("state") in ("broken", "not_fetched")]
        # Task 20：逐份文件一条结构化事件。信息科排障时最常问的是
        # 「上周那期到底哪几个文件进了隔离、因为什么」——这条腿此前
        # 只在终端上打印，跑完就没了。
        for item in results:
            state = item.get("state", "")
            log_event(
                "channel.quarantine"
                if state == "dirty"
                else ("channel.fetch.failed" if state == "broken" else "channel.not_fetched"),
                channel=item.get("channel", ""),
                period=args.period,
                message=(
                    f"{item.get('source_file') or item.get('channel','')}："
                    f"{_state_zh(state)}；{item.get('detail', '')}"
                ),
                code=item.get("error_code", "") or "",
                source_file=item.get("source_file", "") or "",
                clean_rows=item.get("clean_rows", 0) or 0,
                dirty_total=item.get("dirty_total", 0) or 0,
                dirty_rate=item.get("dirty_rate", 0.0) or 0.0,
            )
        log_event(
            "run.finish",
            message=(
                f"quarantine scan 账期 {args.period}：{len(dirty)} 份有脏行、"
                f"{len(broken)} 份判坏或未取到"
            ),
            period=args.period,
            dirty=len(dirty),
            broken=len(broken),
        )
        if args.json:
            import json

            print(
                json.dumps(
                    {"period": args.period, "db": args.db, "files": results},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"=== 脏行隔离扫描 · 账期 {args.period} · 库 {args.db} ===")
            for item in results:
                state = item.get("state", "")
                if state == "not_fetched":
                    print(f"\n  ○ {item['channel']}：{item.get('detail','')}")
                    continue
                rate_pct = item.get("dirty_rate", 0.0) * 100
                print(
                    f"\n  {item['source_file']}"
                    f"（渠道 {item['channel']}）：{item['clean_rows']} 行入库，"
                    f"{item['dirty_total']} 行隔离（{rate_pct:.1f}%）"
                )
                print(f"    状态：{_state_zh(state)}")
                if item.get("detail"):
                    print(f"    {item['detail']}")
            if dirty:
                print(
                    f"\n本期 {len(dirty)} 份文件有脏行但未超阈值：干净行已入库，"
                    f"合计因此偏小，须回查后重扫。"
                )
            if broken:
                which = "、".join(
                    r.get("source_file") or r["channel"] for r in broken
                )
                print(
                    f"\n本期 {len(broken)} 份文件判坏或未取到：{which}；"
                    f"对应渠道本期标未取到，其余渠道结果照常可用。"
                )
            print(
                f"导出脏行台账：reconq quarantine export --period {args.period} "
                f"--out out/{args.period}-脏行台账.csv"
            )
        return EXIT_CHECKS_FAILED if (dirty or broken) else EXIT_OK
    finally:
        conn.close()


def _state_zh(state: str) -> str:
    """渠道主态的中文说法（面板同一套颜色语义）。"""
    return {
        "clean": "绿 · 本期无脏行",
        "dirty": "黄 · 本期有脏行，需回查导出方",
        "broken": "红 · 脏行率超阈值，整文件判 FORMAT_BROKEN，本渠道标未取到",
        "not_fetched": "灰 · 本期未取到账单",
    }.get(state, state)


def _cmd_quarantine_list(args: argparse.Namespace) -> int:
    """列出隔离区脏行。"""
    from .store.quarantine import list_dirty

    conn = _open_store(args.db, create=False)
    try:
        records = list_dirty(conn, args.period, args.channel)
        if args.json:
            import json

            print(json.dumps({"period": args.period, "dirty": records},
                             ensure_ascii=False, indent=2))
            return EXIT_OK
        if not records:
            print(f"账期 {args.period} 隔离区为空（本期没有被隔离的脏行）")
            return EXIT_OK
        print(f"=== 脏行隔离台账 · 账期 {args.period} · 共 {len(records)} 行 ===")
        for rec in records:
            print(f"\n  {rec['source_file']}:{rec['row_no']}  [{rec['reason_code']}]")
            print(f"    说明：{REASON_ZH.get(rec['reason_code'], ('',''))[0]}")
            if rec["detail"]:
                print(f"    详情：{rec['detail']}")
            if rec["raw_line"]:
                print(f"    原文：{rec['raw_line'][:120]}")
        return EXIT_CHECKS_FAILED
    finally:
        conn.close()


def _cmd_quarantine_export(args: argparse.Namespace) -> int:
    """导出脏行台账 CSV。"""
    from .store.quarantine import export_quarantine_csv

    out = args.out or os.path.join("out", f"{args.period}-脏行台账.csv")
    conn = _open_store(args.db, create=False)
    try:
        path, count = export_quarantine_csv(conn, args.period, args.channel, out)
        print(f"已导出脏行台账：{path}（{count} 行，utf-8-sig，Excel 可直接打开）")
        if count:
            suffix = f" --channel {args.channel}" if args.channel else ""
            print(
                f"下一步：把这张表连同文件名与行号回查给导出方；"
                f"修好后在 {args.period} 账期重跑 reconq quarantine scan"
                f"{suffix}，隔离记录会按幂等键更新不重复累计"
            )
        else:
            print(f"账期 {args.period} 本期没有脏行，导出的是一份空台账（仅表头）")
        return EXIT_OK
    finally:
        conn.close()


def _cmd_export(args: argparse.Namespace) -> int:
    """``reconq export``（Task 44）：导出周对账结论单。

    **只读腿**：打开账单库读已落库的事实（六路主态、差异台账、归因草稿、
    组级留痕、三角色会签、证据快照、事件链、口径绑定、模型审计、隔离区），
    渲染成可签字归档的纸面结论单，**不推进任何业务状态**。结论单上每一条
    都能追到「谁、何时、依据哪几条证据行」。

    退出码沿用本产品口径：0 = 导出完成；1 = 门禁没过（本期还有未办完的
    单，导出的单子上会写明，但不当作成功归档）；2 = 运行不了。
    """
    import json as _json

    conn = _open_store(args.db, create=False)
    try:
        outcome = export_conclusion(
            conn,
            args.period,
            fmt=args.fmt,
            out_path=args.out,
            exported_by=args.operator,
            note=args.note,
            amount_bucket=getattr(args, "amount_bucket", "0.01") or "0.01",
            audit_dir=getattr(args, "audit_dir", "") or "",
        )
    finally:
        conn.close()

    doc = outcome.doc
    if args.json:
        print(_json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
    else:
        encoding = "UTF-8" if outcome.fmt == "md" else "UTF-8-SIG（Excel 可直接打开）"
        print(
            f"结论单已导出：{outcome.path}"
            f"（{outcome.fmt} · {encoding} · {outcome.size} 字节）"
        )
        print(
            f"  账期 {doc.period} · {doc.channels_ok}/{len(doc.channels)} 路已取到 · "
            f"{doc.groups_count} 张处置单 · {doc.lines_count} 条差异 · "
            f"涉及 {doc.total_amount:.2f} 元"
        )
        print(f"  结论集指纹：{doc.fingerprint}")
        if doc.period_status_zh:
            print(f"  账期状态：{doc.period_status_zh}" + (f" · 门禁 {doc.gate}" if doc.gate else ""))
        if doc.contract_needs_reconfirm:
            print(
                f"  ⚠ 以下渠道口径指纹已变更、结论需重新确认："
                f"{'、'.join(doc.contract_needs_reconfirm)}"
            )
        if doc.missing_channels:
            print(
                f"  ⚠ 未取到的渠道："
                + "、".join(f"{c.channel}（{c.status_zh}）" for c in doc.missing_channels)
                + " —— 结论单附录二已逐条列出，不要当成「这路没有账单」"
            )
        if doc.dirty_total:
            print(
                f"  ⚠ 本期隔离脏行 {doc.dirty_total} 条，不计入合计金额；"
                f"台账见 {doc.quarantine_file}"
            )
        print(
            f"\n下一步：把 {os.path.basename(outcome.path)} 打印后由经办 / 复核 / 主管三级签字，"
            f"再回填财务科既有电子台账（{outcome.fmt} 版可直接整表导入）。"
            f"重导一次若「结论集指纹」变了，说明这期账的状态在这期间被改过 —— "
            f"先查清是谁改的，别直接覆盖归档。"
        )

    # 门禁没过时给非零退出码：CI 或脚本里靠 $? 就能判「这期能不能归档」。
    open_groups = [g for g in doc.groups if g.disposition not in ("已确认", "已驳回")]
    if open_groups or doc.missing_channels:
        return EXIT_CHECKS_FAILED
    return EXIT_OK


# —— verify ——————————————————————————————————————————————


def _cmd_contract(args: argparse.Namespace) -> int:
    """``reconq contract {snapshot,diff,baseline,status}``（Task 17）。"""
    action = getattr(args, "contract_cmd", None)
    if action == "snapshot":
        return _cmd_contract_snapshot(args)
    if action == "diff":
        return _cmd_contract_diff(args)
    if action == "baseline":
        return _cmd_contract_baseline(args)
    if action == "status":
        return _cmd_contract_status(args)
    print("用法：reconq contract {snapshot|diff|baseline|status} ...")
    return EXIT_OK


def _open_snapshot_store(db_path: str, *, create: bool):
    """打开账单库并确保契约快照/基线/绑定三张表都在。

    与账单表、隔离表**同库**：口径指纹要回答的是「这一期这批数据当时是
    按哪一版口径跑的」，数据与口径出处必须落在同一个事务边界里，
    分库存会让「账单在 A 库、口径出处在 B 库」这种对不上的状态成为可能。
    """
    from .store.schema import connect, init_schema
    from .store.snapshots import init_snapshot_schema

    conn = _open_store(db_path, create=create)
    init_snapshot_schema(conn)
    return conn


def _cmd_contract_snapshot(args: argparse.Namespace) -> int:
    """``reconq contract snapshot``：记录/列出契约指纹快照。"""
    import json as _json

    from .contracts.fingerprint import fingerprint_source
    from .store.snapshots import init_snapshot_schema, list_snapshots, record_snapshot
    from .store.schema import connect

    channels = args.channel or _discover_channels(args.contracts)
    if args.list:
        conn = _open_snapshot_store(args.db, create=True)
        try:
            records = list_snapshots(conn, channels[0] if len(channels) == 1 else None)
        finally:
            conn.close()
        if args.json:
            print(_json.dumps(
                {"snapshots": [r.to_dict() for r in records]},
                ensure_ascii=False, indent=2,
            ))
        elif not records:
            print("还没有任何契约快照；跑一次 reconq contract snapshot 记录当前口径")
        else:
            print("=== 契约快照历史（新的在前）===")
            for rec in records:
                print(
                    f"  {rec.channel:<14} 指纹 {rec.digest[:12]}  口径指纹 {rec.check_digest[:12]}"
                    f"  版本 {rec.version or '—'}  记录于 {rec.recorded_at}"
                    f"  {rec.source_path}"
                )
        return EXIT_OK

    # 首次 snapshot 时账单库可能还不存在：connect() 会按需建目录建文件，
    # 快照与后续落库的账单行进同一个文件。
    conn = connect(args.db)
    try:
        init_snapshot_schema(conn)
        records = []
        for channel in channels:
            path = os.path.join(args.contracts, f"{channel}.yml")
            if not os.path.isfile(path):
                raise ReconqError(
                    "CONTRACT_MISSING_KEY",
                    f"渠道 {channel} 没有对应契约文件：{path}",
                    channel=channel,
                )
            fp = fingerprint_source(path)
            rec = record_snapshot(
                conn, channel, fp, source_path=path, version=_contract_version(path)
            )
            records.append(rec)
    finally:
        conn.close()

    if args.json:
        print(_json.dumps(
            {"recorded": [r.to_dict() for r in records]},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(f"=== 已记录契约快照（{len(records)} 路）===")
        for rec in records:
            print(
                f"  {rec.channel:<14} 指纹 {rec.digest[:12]}  口径指纹 {rec.check_digest[:12]}"
                f"  版本 {rec.version or '—'}  记录于 {rec.recorded_at}"
            )
        print(
            "\n提示：改完口径后跑 reconq contract diff --channel <渠道> 看改了哪几条口径、"
            "本期要不要重新确认"
        )
    return EXIT_OK


def _contract_version(path: str) -> str:
    """取契约的 ``version`` 字段（仅用于快照列表展示）。"""
    import yaml

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return ""
    if isinstance(raw, dict):
        return str(raw.get("version") or "")
    return ""


def _cmd_contract_diff(args: argparse.Namespace) -> int:
    """``reconq contract diff``：工作区契约 vs 最近快照。"""
    import json as _json

    from .contracts.fingerprint import render_markdown
    from .store.snapshots import diff_channel, record_snapshot
    from .contracts.fingerprint import fingerprint_source

    channels = args.channel or _discover_channels(args.contracts)
    conn = _open_snapshot_store(args.db, create=True)
    try:
        reports = []
        semantic_channels = []
        try:
            for channel in channels:
                path = os.path.join(args.contracts, f"{channel}.yml")
                previous, diff, scope = diff_channel(conn, channel, path)
                if args.record:
                    # 「比对完顺手把当前口径记成新的『上次』」—— 让「上一份
                    # 快照」始终是**上一次确认过的口径**，而不是三天前那份。
                    # 幂等：内容没变时 record_snapshot 只更新元信息，不会
                    # 多出一行，内容也没变时不会凭空多一条快照。
                    record_snapshot(
                        conn, channel, fingerprint_source(path),
                        source_path=path, version=_contract_version(path),
                    )
                if scope.requires_reconfirm:
                    semantic_channels.append(channel)
                reports.append(
                    {
                        "channel": channel,
                        "contract": path,
                        "has_snapshot": previous is not None,
                        "markdown": render_markdown(diff, channel=channel, scope=scope),
                        "diff": diff.to_dict(),
                        "impact": scope.to_dict(),
                    }
                )
        finally:
            conn.close()
    finally:
        pass

    if args.json:
        print(_json.dumps(
            {
                "channels": reports,
                "requires_reconfirm": semantic_channels,
                "panel_hints": [r["impact"]["panel_hint"] for r in reports if r["impact"]["panel_hint"]],
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        for rep in reports:
            print()
            print(rep["markdown"])
            hint = rep["impact"]["panel_hint"]
            if hint:
                print(f"\n  ⚠ {hint}")
            elif not rep["diff"]["is_empty"]:
                print(f"\n  ○ {rep['impact']['reason']}")
        if semantic_channels:
            print(
                f"\n结论：{', '.join(semantic_channels)} 的口径语义已变更，"
                "相关账期需重新确认（reconq contract status 看具体账期）"
            )
    return EXIT_OK


def _cmd_contract_baseline(args: argparse.Namespace) -> int:
    """``reconq contract baseline``：落基线 / 与基线对拍。"""
    import json as _json

    from .contracts.fingerprint import fingerprint_source
    from .store.snapshots import (
        compare_with_baseline,
        load_baseline,
        record_baseline,
    )

    contract_path = os.path.join(args.contracts, f"{args.channel}.yml")
    if not os.path.isfile(contract_path):
        raise ReconqError(
            "CONTRACT_MISSING_KEY",
            f"渠道 {args.channel} 没有对应契约文件：{contract_path}",
            channel=args.channel,
        )
    contract = load_contract(contract_path, {"period": args.period})
    table = contract.table()
    inbox_root = resolve_inbox_root(args.source)
    rows, _probes, _dirty = _read_inbox_dir_isolated(
        inbox_root, args.period, args.channel, table
    )
    if not rows:
        raise ReconqError(
            "BILL_FILE_NOT_FOUND",
            f"账期 {args.period} 渠道 {args.channel} 没有账单文件，无法建立回归基线："
            f"{os.path.join(inbox_root, args.period, args.channel)}/",
            channel=args.channel,
        )
    ctx = CheckContext(
        period=args.period,
        session_timezone=contract.variables.get("session_timezone"),
    )
    results = run_checks(rows, contract, context=ctx)
    fp = fingerprint_source(contract_path)

    conn = _open_snapshot_store(args.db, create=True)
    try:
        previous = load_baseline(conn, args.period, args.channel)
        if args.record:
            written = record_baseline(
                conn, args.period, args.channel, results, contract_digest=fp.digest
            )
            if args.json:
                print(_json.dumps(
                    {
                        "period": args.period,
                        "channel": args.channel,
                        "recorded": written,
                        "contract_digest": fp.digest,
                        "results": [r.to_dict() for r in results],
                    },
                    ensure_ascii=False, indent=2,
                ))
            else:
                failed = sum(1 for r in results if r.status == "fail")
                print(
                    f"已记录回归基线：{args.period} / {args.channel}，"
                    f"{len(results)} 条口径（{failed} 条未通过），指纹 {fp.digest[:12]}"
                )
            return EXIT_OK
        comparison = compare_with_baseline(conn, args.period, args.channel, results)
    finally:
        conn.close()

    if args.json:
        print(_json.dumps(
            {
                "period": args.period,
                "channel": args.channel,
                "has_baseline": previous is not None,
                "contract_digest": fp.digest,
                "comparison": comparison.to_dict(),
                "results": [r.to_dict() for r in results],
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        if previous is None:
            print(
                f"账期 {args.period} 渠道 {args.channel} 还没有回归基线，"
                f"本次 {len(results)} 条口径结果不会与任何东西对拍。"
            )
            print("先落一条基线：reconq contract baseline --period "
                  f"{args.period} --channel {args.channel} --record")
        else:
            print(f"当前契约指纹 {fp.digest[:12]}")
            print(comparison.render())
    return EXIT_CHECKS_FAILED if comparison.has_flip else EXIT_OK


def _cmd_contract_status(args: argparse.Namespace) -> int:
    """``reconq contract status``：列出需重新确认的账期。"""
    import json as _json

    from .store.snapshots import list_needs_reconfirm

    conn = _open_snapshot_store(args.db, create=True)
    try:
        bindings = list_needs_reconfirm(conn, args.period)
    finally:
        conn.close()

    if args.json:
        print(_json.dumps(
            {
                "period": args.period,
                "needs_reconfirm": [b.to_dict() for b in bindings],
            },
            ensure_ascii=False, indent=2,
        ))
    elif not bindings:
        print(
            "没有因口径变更而需要重新确认的账期"
            + (f"（账期 {args.period}）" if args.period else "")
        )
    else:
        print("=== 因口径变更需重新确认的账期 ===")
        for b in bindings:
            print(f"  {b.period}  {b.channel:<14} 口径指纹 {b.check_digest[:12]}")
            if b.note:
                print(f"      {b.note}")
        print(
            "\n提示：本系统不自动清这个标记。经办人逐条复核后由主管确认，"
            "确认动作会写进事件链（结论单「谁、何时、依据哪几条证据行」那一栏）"
        )
    return EXIT_OK


def _cmd_trace(args: argparse.Namespace) -> int:
    """``reconq trace --order <单号> [--period 2026-W36 | --periods ...]``。

    连坐追查的**只读排障腿**：把同一单号在相邻账期与全部六路上的出现
    串成一条链，指出链上的跨账期重复、跨渠道重复与金额关系不成立。

    刻意**不写台账、不推进任何状态**：认领是从落库行反推的（比对齐器
    当时真正的认领弱一层，见 :func:`reconq.align.trace.claims_from_nodes`），
    拿它当台账依据会把「碰巧同号」也判成重复计入。正式结论由
    :func:`reconq.align.trace.chain_to_diff_items` 在对齐编排里写入。
    """
    import json as _json

    from .align.trace import (
        claims_from_nodes,
        load_entity_nodes,
        period_window_of,
        trace_entity,
    )
    from .store.schema import connect, init_schema

    if args.periods and args.period:
        raise ReconqError(
            "TRACE_WINDOW_INVALID",
            "--period 与 --periods 只能给一个：前者以它为中心展开 --window 半径，"
            "后者直接指定追查范围。两个都给会让「窗口到底多大」没有唯一答案",
        )

    if args.periods:
        periods = [p.strip() for p in args.periods.split(",") if p.strip()]
        if not periods:
            raise ReconqError(
                "TRACE_PERIOD_REQUIRED",
                "--periods 给的是空列表（只有逗号或空白）；"
                "请写成 --periods 2026-W35,2026-W36",
            )
        focus, window, scope = "", 0, periods
    else:
        focus = args.period or DEFAULT_TRACE_PERIOD
        window = args.window
        scope = period_window_of(focus, center=focus, window_periods=window)

    conn = connect(args.db)
    try:
        init_schema(conn)
        if args.no_index:
            # 排障用：故意绕开复合索引取全量，验证索引路径确实在用
            nodes = _load_nodes_full_scan(conn, args.order)
        else:
            nodes = load_entity_nodes(conn, args.order, scope)
    finally:
        conn.close()

    chain = trace_entity(
        args.order,
        nodes=nodes,
        claims=claims_from_nodes(nodes),
        # focus 为空（走 --periods 显式指定范围）时不过滤窗口：scope 已经在
        # 取数时限定死了，再按窗口裁一遍等于把显式范围又缩一次。
        focus_period=focus,
        window_periods=window if focus else 0,
    )

    if args.json:
        payload = chain.to_dict()
        payload["scope_periods"] = scope
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(chain.render())
        if chain.conclusions:
            print(
                "\n提示：以上结论不写入台账。本产品的连坐结论由扫描编排在对齐阶段"
                "生成（那时才知道对齐器真正认领了哪几条），台账里渠道记为 *。"
                "如需把某条结论纳入本期处置单，请在扫描后由经办人逐条确认。"
            )
    # 有连坐结论即非零退出：与 verify / scan 同一套门禁语义，
    # 信息科在 CI 或脚本里靠 $? 就能判「这期有没有连坐问题」。
    return EXIT_CHECKS_FAILED if chain.conclusions else EXIT_OK


# —— align-metrics ————————————————————————————————————


def _cmd_align_metrics(args: argparse.Namespace) -> int:
    """``reconq align-metrics --golden <jsonl> [--out out/metrics-report.md]``。

    核心模块（跨渠道对齐）的**可度量验收**。把同一份人工标注金标集分别喂给
    本产品与「按单号逐笔 VLOOKUP 对」基线，出并排指标表与门禁结论。

    门禁三条硬指标任一不过即**非零退出**，与 verify / scan 同一套语义：
    歧义漏判（该报歧义却静默择一）、弱匹配误认领（单号没对上就自动归并）、
    重复认领·跨账期漏判（同一笔被算进两次合计 / 跨两期计入）。另加一条相对
    门禁：**误报率不得高于基线** —— 报得更全可以，报得更吵不行。

    ``--out`` 会把带口径、样例规模与「这些数字不能说明什么」的完整报告落盘；
    只打终端的那份是摘要，签验收用 ``--out``。
    """
    import json as _json

    from .align.metrics import (
        GOLD_LABEL_ZH,
        evaluate_golden_set,
        load_golden_set,
        render_comparison_table,
        render_report,
        write_metrics_report,
    )

    golden = load_golden_set(args.golden)
    product, baseline = evaluate_golden_set(golden)
    failures = product.gate_failures(baseline)

    if args.out:
        path = write_metrics_report(args.out, golden, product, baseline)
        print(f"指标报告已写入 {path}")

    if args.json:
        payload = {
            "golden": {
                "path": golden.path,
                "total": golden.total,
                "layers": golden.layer_counts(),
                "labels": golden.label_counts(),
                "meta": golden.meta,
            },
            "product": product.to_dict(),
            "baseline": baseline.to_dict(),
            "gate_failures": failures,
            "verdicts": [v.to_dict() for v in product.verdicts] if args.cases else [],
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_CHECKS_FAILED if failures else EXIT_OK

    if args.cases:
        by_case = {v.case_id: v for v in baseline.verdicts}
        print(f"{'case':<14}{'分层':<28}{'金标期望':<16}{'本产品':<16}{'基线':<16}")
        print("-" * 90)
        for verdict in product.verdicts:
            base = by_case.get(verdict.case_id)
            base_txt = GOLD_LABEL_ZH.get(base.label, base.label) if base else "—"
            print(
                f"{verdict.case_id:<14}{verdict.layer:<28}"
                f"{GOLD_LABEL_ZH.get(verdict.expect_label, verdict.expect_label):<16}"
                f"{GOLD_LABEL_ZH.get(verdict.label, verdict.label):<16}{base_txt:<16}"
            )
        print()

    print(render_report(golden, product, baseline))
    return EXIT_CHECKS_FAILED if failures else EXIT_OK


def _cmd_align_golden(args: argparse.Namespace) -> int:
    """``reconq align-golden [--cases cases.yml] [--out out/align-regression.md]``。

    核心对齐模块的**回归保障**，与 ``align-metrics`` 分工明确：

    * ``align-metrics`` 回答「本产品比基础方案准不准」（召回 / 误报 / 形态命中）；
    * ``align-golden`` 回答「这次改动有没有把原来判对的弄坏」（按形态守住回归面）。

    两道门禁都在**加载期**：条数下限 + **四形态覆盖门禁**（一码付拆多笔 /
    合并支付 / 跨账期落点 / 退费与原单并存各 ≥4 条）。加载期就拒而不是跑完
    再报，是因为「跑完没报错」极易被当成「这次改动没问题」—— 而最危险的
    退化恰恰是「某种形态一条用例都没有」。

    任一 case 失败即**非零退出**，与 verify / scan 同一套门禁语义；失败项逐条
    给出 case_id、形态标签与逐项差异（状态 / 歧义形态 / 成因 / 证据行号 /
    差额 / 反向单边账）。
    """
    import json as _json

    from .align.cases import (
        FORM_ZH,
        MIN_CASES_PER_AMBIGUITY_FORM,
        load_case_set,
        render_regression_report,
        run_regression,
        write_regression_report,
    )

    case_set = load_case_set(args.cases_path)
    report = run_regression(case_set)

    if args.out:
        path = write_regression_report(args.out, report)
        print(f"回归报告已写入 {path}")

    if args.json:
        print(_json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return EXIT_CHECKS_FAILED if not report.ok else EXIT_OK

    if args.cases_only:
        print(f"{'case':<12}{'形态':<24}{'结论':<18}{'歧义形态':<20}{'结果'}")
        print("-" * 88)
        for outcome in report.outcomes:
            print(
                f"{outcome.case_id:<12}{FORM_ZH[outcome.form][0]:<24}{outcome.status:<18}"
                f"{outcome.kind or '—':<20}{'通过' if outcome.passed else '失败'}"
            )
        print()

    print(render_regression_report(report, out_path=args.out or ""))
    if not report.ok:
        print()
        print(f"四形态覆盖门禁下限：每种 ≥{MIN_CASES_PER_AMBIGUITY_FORM} 条（加载期已过）")
    return EXIT_CHECKS_FAILED if not report.ok else EXIT_OK


# —— align-pipeline ——————————————————————————————————————————————


def _cmd_align_pipeline(args: argparse.Namespace) -> int:
    """``reconq align-pipeline --period 2026-W36 [--progress] [--stages]``。

    **对齐主路径的「跑一遍看看」腿**：一次跑完索引 → 对齐 → 歧义分型 →
    跨账期连坐 → 证据包 → 归因 → 反幻觉核对 → 同根因聚合，出经办人开工前
    的待办汇总（成因 / 条数 / 金额 / 责任方 / 证据条数 / 建议动作）。

    **只读**：不写台账、不推进任何处置状态。它读的是账单库里已落的那份行
    （``reconq scan`` 的产物），不自己再去读渠道文件 —— 渠道文件会被下一次
    取件覆盖，重读会让同一期账两次跑出的证据指向不同内容。

    退出码：全部阶段通过且本期有待办 → 1（与 verify / scan 同一套门禁语义：
    「有单没办完」是非零）；**任一阶段失败 → 2**（本期结论不完整，
    不能拿去签字，与「运行不了」区分开靠 :attr:`PipelineResult.partial`）。
    """
    import json as _json

    from .align.pipeline import STAGE_ZH, run_align_pipeline
    from .store.schema import connect

    conn = connect(args.db)
    try:
        gateway = _pipeline_gateway()
        progress_sink = None
        if args.progress and not args.json:
            def progress_sink(payload):  # noqa: ANN001 - CLI 内联回调
                mark = "✓" if payload["ok"] else "✗"
                line = (
                    f"  [{payload['stage_index']}/{payload['stage_total']}] "
                    f"{mark} {payload['stage_zh']:<10} "
                    f"{payload['processed']:>4} 条 {payload['elapsed_ms']:>8.1f} ms"
                )
                if payload["detail"]:
                    line += f"  {payload['detail']}"
                print(line, flush=True)

        result = run_align_pipeline(
            conn,
            args.period,
            gateway=gateway,
            contracts_dir=args.contracts,
            on_progress=progress_sink,
            amount_bucket_width=args.amount_bucket,
            history_periods=args.history_periods,
            trace_window_periods=args.trace_window,
            instructions=args.instructions,
        )
    finally:
        conn.close()

    if args.json:
        print(_json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        if args.progress:
            print(f"账期 {args.period} 主路径阶段进度（pipeline "
                  f"{result.pipeline_version}）：")
        print()
        print(result.brief_zh())
        if args.stages or args.progress:
            print()
            print(f"  逐阶段耗时（共 {result.elapsed_ms:.0f} ms）：")
            for row in result.stage_timings():
                mark = "✓" if row["ok"] else f"✗ [{row['error_code']}]"
                print(
                    f"    {row['stage_zh']:<10} {mark} {row['processed']:>4} 条 "
                    f"{row['elapsed_ms']:>8.1f} ms"
                )
        if result.partial:
            print()
            print(
                f"  ! 本期结论**不完整**（{result.partial_reason}）。"
                "已完成阶段的结果在上面，但**不能据此出结论单** —— "
                "「已归因 N 条、聚合那一步崩了」与「本期零差异」在签字页上"
                "长得一模一样。请把该码连同 --db 路径给信息科。"
            )
    if result.partial:
        return EXIT_ERROR
    return EXIT_CHECKS_FAILED if result.needs_manual else EXIT_OK


def _pipeline_gateway() -> Optional[object]:
    """按环境变量建模型网关；**没配就返回 None**，由归因层走降级路径。

    刻意不在这里抛 ``MODEL_CONFIG_INVALID``：本命令的第一读者是财务科，
    「模型没配」不该让「这一期有多少件事要我判」这个问题答不出来 ——
    答出来并在汇总里标「成因取自规则基线、未经模型」才是有用的降级。
    """
    from .gateway.model_gateway import GatewayConfig, ModelGateway

    try:
        config = GatewayConfig.from_env()
    except ReconqError as exc:
        logger_notice = f"模型网关未配置，本次按规则基线归因：{exc.code}"
        print(f"  ~ {logger_notice}", file=sys.stderr)
        return None
    return ModelGateway(config)


def _cmd_attrib_eval(args: argparse.Namespace) -> int:
    """``reconq attrib-eval [--set DIR] [--live] [--out out/attrib-eval.md]``。

    ``ai_role: core`` 的效果验收：在同一份**人工标注**的评测集上，把
    模型档与规则基线档并排跑一遍，出 Top-1 / Top-3 命中率、反幻觉拒绝率
    与成本。

    门禁：**模型档 Top-3 命中率不得低于规则基线档**。规则档是「完全不调
    模型时能做到的下限」，模型档比它差，这一层的模型调用就没有挣回成本。
    不过即**非零退出**，与 verify / scan 同一套语义。

    刻意**不**要求 Top-1 更高、也不要求拒答率更高：规则在「形态 → 成因」
    这类确定性映射上本就强，模型的优势在弱匹配与 UNKNOWN 拒答上。把那两条
    也写成硬门禁，会逼着人对着评测集调提示词。

    默认**回放**录制响应替身（不发真实请求、不花钱）；``--live`` 走真实
    端点，需配好 ``RECONQ_MODEL_*``。两种跑法只有 HTTP 传输层不同，评测
    口径一字不改。
    """
    import json as _json

    from .align.eval import (
        RUN_MODES,
        evaluate_eval_set,
        load_eval_set,
        render_report,
        write_eval_report,
    )

    eval_set = load_eval_set(args.set_dir)
    model, rules = evaluate_eval_set(eval_set, live=bool(args.live))
    failures = model.gate_failures(rules)

    if args.out:
        path = write_eval_report(args.out, eval_set, model, rules)
        print(f"评测报告已写入 {path}")

    if args.json:
        print(_json.dumps(
            {
                "eval_set": {
                    "path": eval_set.path,
                    "total": eval_set.total,
                    "unknown_total": eval_set.unknown_total,
                    "layers": eval_set.layer_counts(),
                    "channels": eval_set.channel_counts(),
                    "cause_coverage": eval_set.cause_coverage(),
                    "behaviours": eval_set.behaviour_counts(),
                    "meta": eval_set.meta,
                },
                "run_mode": model.run_mode,
                "run_mode_zh": RUN_MODES.get(model.run_mode, model.run_mode),
                "model": model.to_dict(),
                "rules": rules.to_dict(),
                "gate_failures": failures,
                "verdicts": (
                    [v.to_dict() for v in model.verdicts] + [v.to_dict() for v in rules.verdicts]
                    if args.cases
                    else []
                ),
            },
            ensure_ascii=False,
            indent=2,
        ))
        return EXIT_CHECKS_FAILED if failures else EXIT_OK

    if args.cases:
        model_by_case = {v.case_id: v for v in model.verdicts}
        print(f"{'case_id':<40}{'期望':<34}{'模型 Top-1':<30}{'规则 Top-1':<24}")
        print("-" * 128)
        for case in eval_set.cases:
            mv = model_by_case[case.case_id]
            rv = next(v for v in rules.verdicts if v.case_id == case.case_id)
            print(
                f"{case.case_id:<40}{'/'.join(case.expect_codes):<34}"
                f"{mv.top1_code or '（无可用成因）':<30}{rv.top1_code or '（无可用成因）':<24}"
            )
        print()

    print(render_report(eval_set, model, rules))
    return EXIT_CHECKS_FAILED if failures else EXIT_OK


def _cmd_model_usage(args: argparse.Namespace) -> int:
    """``reconq model-usage --period 2026-W36 [--calls] [--json]``。

    财务科问「这一期模型花了多少钱、慢在哪」时跑的那一条。

    **只读**：不调模型、不改台账、不推进任何状态。审计表里全是既有的
    调用记录，这条命令只是把它们按账期加在一起 —— 加法口径与
    :func:`reconq.audit.model_calls.usage_of_period` 完全一致（面板底部读
    的是同一个函数），两处各算一次必然对不上。

    退出码：审计链校验通过 → 0；**校验不通过 → 1**（不是 2）。理由是 2
    在本产品里是「运行不了」，而链断掉时这张表仍然打得出来 —— 让人看得见
    数字、同时用一个非零码告诉脚本「这些数字不可信」，正是这里要的。
    """
    import json as _json

    from .audit.model_calls import (
        ModelCallLedger,
        init_audit_schema,
        usage_of_period,
    )
    from .store.schema import connect, init_schema

    conn = connect(args.db)
    try:
        init_schema(conn)
        init_audit_schema(conn)
        has_table = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' "
            "AND name='model_calls'"
        ).fetchone()["n"]
        usage = usage_of_period(
            conn, args.period, verify_chain=not args.no_chain_check
        )
        calls = ModelCallLedger(conn).list_calls(args.period) if args.calls else []
    finally:
        conn.close()

    if not has_table:
        raise ReconqError(
            "MODEL_AUDIT_SCHEMA_MISSING",
            f"账单库 {args.db} 里没有模型调用审计表。"
            "审计表由 reconq.audit.init_audit_schema() 建；"
            "若本期确实没调过模型，init 已建好空表 —— "
            "报这个错通常意味着 --db 指错了库",
        )

    chain_ok = usage.chain is None or usage.chain.ok
    if args.json:
        payload = usage.to_dict()
        # 键名**不能叫 calls**：to_dict() 里 ``calls`` 已经是「调用次数」
        # 这个整数，直接覆盖会把「2 次」变成一个列表，而读的人看不出
        # 类型变了。逐次明细另起一个键。
        payload["call_records"] = calls
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(usage.render())
        if calls:
            print("  逐次调用：")
            for row in calls:
                mark = "缓存" if row["cache_hit"] else f"{row['elapsed_ms']:.0f}ms"
                print(
                    f"    {row['call_id']}  {row['conclusion_key'] or '-':<28}"
                    f" {mark:>8}  {row['prompt_tokens']}+{row['completion_tokens']}"
                    f" token  {row['status']}"
                    + (f" [{row['error_code']}]" if row["error_code"] else "")
                )
        if not usage.calls:
            print(
                "\n提示：这一期没有任何模型调用记录。可能是本期没有单边账差异，"
                "也可能是归因走的是确定性兜底（模型未配置）—— "
                "后者在扫描事件里是 model.degraded。"
            )
    return EXIT_OK if chain_ok else EXIT_CHECKS_FAILED


def _load_nodes_full_scan(conn, order_no: str):
    """排障腿：不用 (单号, 账期) 复合索引，全表取回该单号的全部行。

    只为**对比验证索引路径**而存在：信息科怀疑「索引没建上导致每次追查都
    全表扫」时，用它跑一遍比耗时就知道了。产品路径不走这里。
    行 → 节点的转换复用 :func:`reconq.align.trace._node_from_row`，
    免得两条腿的退款扣减口径漂移（漂了就会「索引路径与全表扫结果不同」，
    而排障的人会以为是索引在漏行）。
    """
    from .align.trace import _node_from_row

    return [
        _node_from_row(row)
        for row in conn.execute(
            "SELECT * FROM bill_rows "
            "WHERE channel_order_no = ? COLLATE NOCASE OR his_order_no = ? COLLATE NOCASE",
            (order_no, order_no),
        )
    ]


def _cmd_attrib_replay(args: argparse.Namespace) -> int:
    """``reconq attrib-replay [--version v2 | --compare v1,v2] [--json]``。

    改提示词之前跑的那一条：**离线**回放，不调模型、不花钱、不写台账。

    它跑的是回放集里**录好的模型响应**，经完整后处理链（草稿 Schema →
    证据引用越界筛查 → 反幻觉核对 → 受控字典筛查）之后与期望性质比对。
    刻意不重调模型：重调测的是端点当天的状态，不是提示词版的差别 ——
    那件事该由 ``reconq scan`` 去做。

    ``--compare`` 给出两版逐条变化与指标增减；**新版本出现「由对变错」的
    用例时返回非零退出码**并在终端打告警 —— 静默采用更差版本是这套回放
    最要防的那件事。
    """
    import json as _json

    from .align import prompts as _prompts
    from .align.replay import (
        compare_versions,
        render_comparison,
        render_report,
        run_replay,
    )

    if args.list_versions:
        rows = [
            {
                "version": key,
                "change_note": item.change_note,
                "cause_form": item.cause_form,
                "prompt_digest": _prompts.prompt_digest(item),
                "current": key == _prompts.PROMPT_VERSION,
            }
            for key, item in _prompts.PROMPTS.items()
        ]
        if args.json:
            print(_json.dumps(
                {"current": _prompts.PROMPT_VERSION, "versions": rows},
                ensure_ascii=False, indent=2,
            ))
        else:
            print(f"已登记提示词版本（当前生效：{_prompts.PROMPT_VERSION}）")
            for row in rows:
                mark = "*" if row["current"] else " "
                print(f" {mark} {row['version']:<8} {row['prompt_digest']}  {row['change_note']}")
        return EXIT_OK

    if args.compare:
        parts = [p.strip() for p in str(args.compare).split(",") if p.strip()]
        if len(parts) != 2:
            raise ReconqError(
                "REPLAY_COMPARE_ARGS_INVALID",
                f"--compare 要两版，格式 <旧版本>,<新版本>；实际收到 {parts}。"
                "只给一版比不了 —— 「变好还是变坏」是相对基线说的",
            )
        comparison = compare_versions(parts[0], parts[1], directory=args.replay_dir)
        if args.json:
            print(_json.dumps(comparison.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(render_comparison(comparison))
        # 更差时非零：让 CI / 排障脚本能直接把「别上这版」变成一道门禁，
        # 而不必靠人读终端最后那行告警
        return EXIT_CHECKS_FAILED if comparison.regressed else EXIT_OK

    report = run_replay(args.prompt_version, directory=args.replay_dir)
    if args.json:
        print(_json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_report(report))
    # 一条都没产出也是非零：全被拒掉的回放集跑出来的「命中率 0%」不是
    # 「模型不行」，是压根没有可评的东西，不该被当成一次正常结果
    return EXIT_OK if report.usable else EXIT_CHECKS_FAILED


def _cmd_serve(args: argparse.Namespace) -> int:
    """``reconq serve``：常驻服务 + 浏览器实时面板（Task 47）。

    **CLI 仍是排障第二条腿**：``reconq verify --period 2026-W36`` 与
    ``reconq data-source test wechat`` 照旧可用，网页挂了不影响它们 ——
    源产品只有 CLI 这一条腿，本产品加面板的目的不是替掉它。
    """
    from .web.app import run_server

    run_server(
        args.db,
        host=args.host,
        port=args.port,
        poll_seconds=args.poll_seconds,
        log_level=args.uvicorn_log_level,
    )
    return EXIT_OK


def _cmd_scan(args: argparse.Namespace) -> int:
    """``reconq scan --period <账期>``：一次扫描推进这期账的状态。

    与 ``verify`` 的分工：``verify`` 是**只读排障腿**（不落库、不写状态），
    ``scan`` 是**这期账的正式扫描**——取数、幂等落库、跑口径、把差异写进
    台账、把账期实例推进到「可结 / 不可结」。两者读同一份契约与同一套
    口径，所以口径不会两样。
    """
    import json as _json

    from .engine.orchestrator import scan as run_scan
    from .engine.orchestrator import scan_status
    from .store.schema import connect
    from .store.snapshots import init_snapshot_schema

    conn = connect(args.db)
    init_snapshot_schema(conn)
    try:
        if args.status:
            status = scan_status(conn, args.period)
            if args.json:
                print(_json.dumps(status, ensure_ascii=False, indent=2))
            else:
                print(
                    f"账期 {status['period']}：{status.get('status_zh', status['status'])}"
                    f"（{status['status']}）"
                )
                if status.get("gate"):
                    print(f"  门禁：{status['gate']} · {status.get('reason', '')}")
                if status.get("stale"):
                    print("  ⚠ 上一次扫描已中断，等人工决定重扫或作废")
                for ch in status.get("channels", []):
                    print(
                        f"  · {ch['channel']:<14} {ch['status']:<12}"
                        f" {ch['row_count']:>6} 行  {ch['total_amount']:>12} 元"
                        + (f"  [{ch['error_code']}]" if ch.get("error_code") else "")
                    )
            return EXIT_OK if not status.get("stale") else EXIT_CHECKS_FAILED

        variables = {"period": args.period}
        variables.update(parse_variable_assignments(args.variables))
        outcome = run_scan(
            conn,
            args.period,
            channels=args.channel,
            inbox_root=resolve_inbox_root(args.source),
            contracts_dir=args.contracts,
            variables=variables,
            reopen=args.reopen,
            resume=args.resume,
            only_checks=args.only_check,
            actor=args.actor,
        )
    finally:
        conn.close()

    if args.json:
        payload = outcome.to_dict()
        payload["run_summary"] = build_run_summary(
            period=outcome.period,
            command="scan",
            run_id=outcome.run_id,
            channels=[
                ChannelRun.from_channel_scan(
                    ch, carried=ch.channel in outcome.carried_channels
                )
                for ch in outcome.channels
            ],
            gate=outcome.gate,
            gate_reason=outcome.scan.reason,
            status=outcome.scan.status,
            status_zh=outcome.scan.status_zh,
            # 合计与行数取**账期实例**（= 账单库口径），不是本次跑的那几路：
            # 子集重跑时按本次累加会让本期合计凭空少一大截，而库里没有一行
            # 数据真的变过。
            total_amount=outcome.scan.total_amount,
            rows_total=outcome.scan.rows_ingested,
            checks_total=outcome.scan.checks_total,
            checks_failed=outcome.scan.checks_failed,
            diffs_written=outcome.diffs_written,
            unconfirmed=outcome.scan.unconfirmed,
            pending_attribution=outcome.scan.pending_attribution,
            warnings=outcome.warnings,
            event_seq=outcome.event_seq,
            event_hash=outcome.event_hash,
        ).to_dict()
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        from .engine.resume import describe_scope

        print(f"账期 {outcome.period} 扫描完成（run={outcome.run_id}）")
        print(
            f"  状态：{outcome.scan.status_zh}（{outcome.scan.status}）"
            f" · 门禁：{outcome.gate} · {outcome.scan.reason}"
        )
        print(
            f"  落库 {outcome.scan.rows_ingested} 行 · 合计 {outcome.scan.total_amount} 元 · "
            f"口径 {outcome.scan.checks_total} 条（未通过 {outcome.scan.checks_failed} 条）"
        )
        # 重跑范围必须在结论之前说清楚：财务科看到「微信 19 行」时要立刻
        # 知道这是刚重跑的，还是上周跑完沿用下来的。
        if outcome.scope and not outcome.scope.is_full:
            print(f"  本次范围：{describe_scope(outcome.scope)}")
        for ch in outcome.channels:
            carried = "（沿用上次）" if ch.channel in outcome.carried_channels else ""
            suffix = f"  [{ch.error_code}] {ch.detail}" if ch.error_code else ""
            print(
                f"  · {ch.channel:<14} {ch.status:<12} {ch.row_count:>6} 行"
                f" {ch.total_amount:>12} 元"
                f"  口径 {ch.checks_total} 条/未通过 {ch.checks_failed} 条"
                f"  新增 {ch.inserted} 行/重复 {ch.skipped_duplicates} 行{carried}{suffix}"
            )
        if outcome.origin_counts:
            carried_n = outcome.origin_counts.get("carried", 0)
            fresh_n = outcome.origin_counts.get("this_run", 0)
            print(f"  口径出处：本次产出 {fresh_n} 条 · 沿用上次 {carried_n} 条")
        if outcome.resolved_diffs:
            print(
                f"  本次重跑消解 {len(outcome.resolved_diffs)} 条差异（保留台账行，"
                f"不计入待确认）：{'、'.join(outcome.resolved_diffs)}"
            )
        for warning in outcome.warnings:
            print(f"  ! {warning}")
        if outcome.can_close:
            print("  ✓ 本期口径全部通过且六路均已取到，可以结账")
        else:
            print(f"  ✗ 本期还不能结账（{outcome.scan.reason}）")
    return EXIT_OK if outcome.can_close else EXIT_CHECKS_FAILED


def _cmd_verify(args: argparse.Namespace) -> int:
    # Task 26：证据快照复算腿。这两条**先于取数**处理 —— 它们只读账单库里的
    # 快照，不碰 inbox、不跑口径。放在后面会被契约静态校验闸门拦下
    # （快照复算与当期契约写没写错无关），也白等一次六路取数。
    if args.replay or args.replay_list:
        return _cmd_verify_replay(args)

    inbox_root = resolve_inbox_root(args.source)
    variables = {"period": args.period}
    variables.update(parse_variable_assignments(args.variables))

    channels = list(args.only_channel or []) or list(args.channel or []) or _discover_channels(
        args.contracts
    )

    # --resume：只跑上次没跑完的渠道。verify 是**只读排障腿**，所以续跑在
    # 这里只影响「本次查哪几路」——读断点要账单库（--db），没给就退回全量
    # 并说清楚，而不是猜一个默认值当断点。
    carried: list[str] = []
    if args.resume:
        from .engine.resume import load_checkpoints

        if not args.db:
            print(
                "提示：--resume 需要 --db 指定账单库才能读断点；未指定，按全量渠道查询",
                file=sys.stderr,
            )
        else:
            conn_r = _open_snapshot_store(args.db, create=False)
            try:
                done = {cp.channel for cp in load_checkpoints(conn_r, args.period) if cp.done}
            finally:
                conn_r.close()
            if done:
                carried = [c for c in channels if c in done]
                channels = [c for c in channels if c not in done]
            if not channels:
                print(
                    f"账期 {args.period} 的 {len(carried)} 路渠道均已在断点上跑完；"
                    f"要重新取数请去掉 --resume"
                )
                return EXIT_OK

    # —— 契约静态校验闸门（Task 16）——
    # 放在**取数之前**：契约自己写错了（列名拼错、check 挂在不存在的列上、
    # 同一 path 写两遍、引用了没声明的变量），先取回六路账单再发现，财务科
    # 白等一次扫描还得回头分「是账单的问题还是契约的问题」。有 ERROR 直接
    # 拦下并给 YAML 行号；WARN 记录后放行。
    if args.lint:
        return _print_lint_only(args, channels, variables)

    if _lint_contracts_gate(args, channels, variables):
        return EXIT_ERROR

    operators = [op.strip() for op in args.check.split(",") if op.strip()]
    unsupported = [op for op in operators if op not in SUPPORTED_OPERATORS]
    if unsupported:
        raise ReconqError(
            "CHECK_OPERATOR_UNSUPPORTED",
            f"--check 指定了尚未实现的算子：{', '.join(unsupported)}；"
            f"本轮已实现：{', '.join(SUPPORTED_OPERATORS)}",
        )

    summary: list[dict] = []
    #: Task 20：每路的运行期观测值（批次数 / 耗时 / 峰值内存 / 脏行数）。
    #: 刻意**不落库**——重启后读到的历史渠道结果不该带着上次的耗时冒充本次。
    telemetry: dict[str, ChannelRun] = {}
    total_fail = 0
    bound: list[dict] = []
    run_started = time.time()
    run_start_perf = time.perf_counter()
    if args.bind_contract and not args.db:
        raise ReconqError(
            "PERIOD_BINDING_NOT_INITIALIZED",
            "--bind-contract 需要同时给 --db 指定账单库路径；"
            "不指定时 verify 保持只读排障腿，不写任何状态",
        )
    _bind_conn = _open_snapshot_store(args.db, create=True) if args.bind_contract else None
    for channel in channels:
        channel_start = time.perf_counter()
        contract_path = os.path.join(args.contracts, f"{channel}.yml")
        if not os.path.isfile(contract_path):
            raise ReconqError(
                "CONTRACT_MISSING_KEY",
                f"渠道 {channel} 没有对应契约文件：{contract_path}",
                channel=channel,
            )
        contract = load_contract(contract_path, variables)
        # --only-check 是**收窄**而不是追加：给了它就跑它选中的那些，
        # 不再叠加 --check 的默认全量算子。两者求并集的话「只重跑这一条
        # 口径」会变成「跑这一条 + 跑全部 13 个算子」，等于没收窄。
        # 判据仍然只有一份（CheckSelector），不因为多一个开关就多一套规则。
        if args.only_check:
            selectors = list(args.only_check)
        else:
            selectors = [f"type={op}" for op in operators]
        selected = [chk for chk, _col in filter_checks(contract, CheckSelector.parse_all(selectors))]
        if args.only_check and not selected:
            raise ReconqError(
                "CHECK_SELECTOR_EMPTY",
                f"--only-check 选择器 {'、'.join(args.only_check)} 在 {channel} "
                f"契约里一条都没命中（共 {len(list(contract.iter_checks()))} 条口径）；"
                f"本产品不会把「一条没跑到」当成「全部通过」",
                channel=channel,
            )
        table = contract.table()
        rows, format_probes, dirty = _read_inbox_dir_isolated(
            inbox_root, args.period, channel, table
        )
        # 格式证据随每路一起进 run summary：金额对不上时，先看这路的编码/
        # 分隔符/表头判据牢不牢，再谈口径。Task 10 的产出不能只活在单测里。
        format_evidence = [p.as_dict() for p in format_probes]
        # Task 11：脏行数进渠道主态。verify 这条腿是只读排障腿，不落库，
        # 因此脏行只计数与提示（真正可导出的台账由 ``quarantine scan/export``
        # 落盘那条腿产出），但**摘要里必须带这个数** —— 面板与结论单
        # 都要知道「这路有 N 行被隔离」，否则合计被读小了就查不出来源。
        dirty_count = len(dirty)
        dirty_evidence = [d.as_dict() for d in dirty]
        # 批次数 = 本路读到的账单文件数（文件通道一文件一批；HIS 那一路由
        # its_db 的流式 FETCH 往返数给出）。「跑了多少批」和「跑了多少行」
        # 一起看才判断得出分批是否生效：2000 行 2000 批说明分批没起作用。
        telemetry[channel] = ChannelRun(
            channel=channel,
            status="checked" if rows else "not_fetched",
            row_count=len(rows),
            total_amount=_sum_amount(rows, contract.table()) if rows else "0.00",
            batches=len(format_evidence),
            elapsed_ms=round((time.perf_counter() - channel_start) * 1000, 3),
            dirty_rows=dirty_count,
        )
        if not rows:
            telemetry[channel].elapsed_ms = round(
                (time.perf_counter() - channel_start) * 1000, 3
            )
            telemetry[channel].peak_rss_kb = _peak_rss_kb()
            log_event(
                "channel.not_fetched",
                channel=channel,
                period=args.period,
                message=f"{channel}：本期未取到账单（{dirty_count} 行进隔离区）",
                duration_ms=telemetry[channel].elapsed_ms,
                dirty_rows=dirty_count,
                code="CHANNEL_NOT_FETCHED",
            )
            # 本期该路没取到账单：**不跑口径**。0 行跑 not_empty 会「全部非空」、
            # 跑 decimal_sum 会「合计 0 元」——那是对着空气说通过，比报错更危险。
            note = (
                f"本期未取到账单文件：{os.path.join(inbox_root, args.period, channel)}/"
                if not dirty_count
                else (
                    f"{dirty_count} 行全部进隔离区，本期无可对账数据："
                    f"{os.path.join(inbox_root, args.period, channel)}/"
                )
            )
            summary.append(
                {
                    "channel": channel,
                    "status": "not_fetched",
                    "row_count": 0,
                    "total_amount": "0.00",
                    "check_total": 0,
                    "check_failed": 0,
                    "results": [],
                    "format": format_evidence,
                    "dirty_rows": dirty_count,
                    "dirty": dirty_evidence,
                }
            )
            if not args.json:
                _print_channel(
                    channel,
                    contract.name or channel,
                    0,
                    [],
                    note=note,
                )
            continue
        # 跨日切分这条口径要在**运行期**才知道本次扫的是哪一期、账期时区
        # 归一到哪一边：同一份契约服务多个账期，这两个值随扫描变。
        check_ctx = CheckContext(
            period=args.period,
            session_timezone=variables.get("session_timezone")
            or contract.variables.get("session_timezone"),
        )
        results = run_checks(rows, contract, selected=selected, context=check_ctx)

        # Task 17：口径出处随结论一起落库。指纹取自**原始契约文件**而非
        # 已注入变量的模型 —— 后者的 location 里带着账期值，同一份契约在
        # 两个账期会得到两个指纹，「口径改没改过」就被账期噪声淹掉。
        if args.bind_contract:
            from .contracts.fingerprint import fingerprint_source
            from .store.snapshots import bind_period_contract

            binding = bind_period_contract(
                _bind_conn, args.period, channel, fingerprint_source(contract_path)
            )
            bound.append(binding.to_dict())

        fails = [r for r in results if r.status == "fail"]
        total_fail += len(fails)
        run = telemetry[channel]
        run.checks_total = len(results)
        run.checks_failed = len(fails)
        run.elapsed_ms = round((time.perf_counter() - channel_start) * 1000, 3)
        run.peak_rss_kb = _peak_rss_kb()
        log_event(
            "channel.check.done",
            channel=channel,
            period=args.period,
            message=(
                f"{channel}：{len(rows)} 行 / 口径 {len(results)} 条 / "
                f"未通过 {len(fails)} 条"
            ),
            duration_ms=run.elapsed_ms,
            row_count=len(rows),
            batches=run.batches,
            dirty_rows=dirty_count,
            checks_total=len(results),
            checks_failed=len(fails),
        )
        summary.append(
            {
                "channel": channel,
                "status": "checked",
                "row_count": len(rows),
                "total_amount": _sum_amount(rows, contract.table()),
                "check_total": len(results),
                "check_failed": len(fails),
                "results": [r.to_dict() for r in results],
                "format": format_evidence,
                "dirty_rows": dirty_count,
                "dirty": dirty_evidence,
            }
        )
        if not args.json:
            _print_channel(
                channel, contract.name or channel, len(rows), results,
                format_probes=format_probes, dirty=dirty,
            )

    if _bind_conn is not None:
        _bind_conn.close()
        reconfirmed = [b for b in bound if b["status"] == "needs_reconfirm"]
        if reconfirmed and not args.json:
            print(
                "\n⚠ 以下账期/渠道因口径指纹变更被标为需重新确认："
                + "、".join(f"{b['period']}/{b['channel']}" for b in reconfirmed)
                + "\n  旧口径下的确认结论不再自动有效，需经办人复核后由主管确认"
            )

    run_summary = build_run_summary(
        period=args.period,
        command="verify",
        channels=[telemetry[c] for c in channels if c in telemetry],
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(run_started)),
        elapsed_ms=round((time.perf_counter() - run_start_perf) * 1000, 3),
        gate="red" if total_fail else "green",
        gate_reason=(
            f"{total_fail} 条口径未通过" if total_fail else "全部口径通过"
        ),
        checks_failed=total_fail,
    )
    log_event(
        "run.finish",
        message=(
            f"verify 账期 {args.period}：{run_summary.channels_ok}/"
            f"{len(run_summary.channels)} 路可用，口径未通过 {total_fail} 条"
        ),
        period=args.period,
        duration_ms=run_summary.elapsed_ms,
        gate=run_summary.gate,
        rows_total=run_summary.rows_total,
        total_amount=run_summary.total_amount,
    )
    if args.json:
        import json

        print(
            json.dumps(
                {
                    "period": args.period,
                    "source": inbox_root,
                    "only_channels": args.only_channel,
                    "only_checks": args.only_check,
                    "carried_channels": carried,
                    "channels": summary,
                    "contract_bindings": bound,
                    # 摘要对象：面板 / 结论单 / 工单三处读的是同一份口径。
                    # 与上面的 ``channels``（逐 check 证据明细）互补而非重复：
                    # 那是「哪条口径没过、证据在第几行」，这是「这一趟整体怎么样」。
                    "run_summary": run_summary.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if carried:
            print(
                f"\n（--resume：{'、'.join(carried)} 已在断点上跑完，本次未查）"
            )
        _print_footer(args.period, summary, total_fail)
    return EXIT_CHECKS_FAILED if total_fail else EXIT_OK


def _lint_contracts_gate(
    args: argparse.Namespace,
    channels: list[str],
    variables: dict,
) -> bool:
    """逐渠道跑契约静态校验；有任何 ERROR 时打印问题清单并返回 True。

    WARN 一律放行 —— 覆盖度类问题（缺兜底口径）不会让口径算错，只会让某类
    问题查不出来，拿它阻断扫描是本末倒置。
    """
    from .contracts.linter import errors_of, lint_source

    if args.json:
        return False  # JSON 腿的 lint 结果随 --lint 输出

    failed = False
    for channel in channels:
        contract_path = os.path.join(args.contracts, f"{channel}.yml")
        if not os.path.isfile(contract_path):
            continue
        issues = lint_source(contract_path, provided_variables=variables)
        if not issues:
            continue
        for issue in issues:
            print(
                f"契约 lint · {channel}：{issue.render(source_path=contract_path)}",
                file=sys.stderr,
            )
        if errors_of(issues):
            failed = True
    if failed:
        print(
            "reconq: 契约静态校验未通过，已在取数前拦下；"
            "按上面的 YAML 行号改契约后重跑（只看校验结果可用 --lint）",
            file=sys.stderr,
        )
    return failed


def _cmd_verify_replay(args: argparse.Namespace) -> int:
    """``reconq verify --replay <结论键>`` / ``--replay-list``：证据快照复算。

    **只读排障腿**：只打开账单库读快照表，不取数、不跑口径、不写任何状态。
    复算用的是快照里记的候选集，因此源账单表已被重拉、上一期的数据已被
    覆盖，本命令仍应给出与快照记录相同的结论 —— 这正是签字前要的那句
    「当时比的是哪几行，现在重算还是不是这个结果」。
    """
    import json as _json

    from .align.snapshot import (
        count_alignment_snapshots,
        init_align_snapshot_schema,
        latest_alignment_snapshot,
        load_alignment_snapshots,
        render_replay,
        render_snapshot,
        replay_snapshot,
    )
    from .contracts.fingerprint import fingerprint_source
    from .store.schema import connect

    if not args.db:
        raise ReconqError(
            "SNAPSHOT_NOT_FOUND",
            "--replay / --replay-list 需要同时给 --db 指定账单库路径"
            f"（默认 {DEFAULT_DB}）：证据快照与账单行同库，"
            "同库才能保证「快照记的候选集」与「当时的账单」在同一个事务边界里",
        )
    conn = connect(args.db)
    try:
        init_align_snapshot_schema(conn)
        if args.replay_list:
            snaps = load_alignment_snapshots(conn, period=args.period)
            if args.json:
                print(_json.dumps(
                    {
                        "period": args.period,
                        "total": len(snaps),
                        "snapshots": [s.to_dict() for s in snaps],
                    },
                    ensure_ascii=False, indent=2,
                ))
            else:
                print(f"账期 {args.period}：已拍证据快照 {len(snaps)} 条")
                for snap in snaps:
                    print(
                        f"  · {snap.conclusion_key:<48} {snap.kind:<16}"
                        f" 快照 {snap.content_hash[:12]}"
                        f" 参与方 {snap.participant_total} 条"
                        f"（{snap.created_at}）"
                    )
                if not snaps:
                    print(
                        "  提示：本期还没有快照。先跑一次 reconq scan，"
                        "对齐阶段会为每条待归因结论拍一份"
                    )
            return EXIT_OK

        snapshot = latest_alignment_snapshot(conn, args.period, args.replay)
        if snapshot is None:
            available = load_alignment_snapshots(conn, period=args.period)
            raise ReconqError(
                "SNAPSHOT_NOT_FOUND",
                f"账期 {args.period} 下没有结论键 {args.replay!r} 的快照"
                + (
                    f"；本期共 {len(available)} 条可用快照"
                    f"（用 reconq verify --replay-list --period {args.period} 列出）"
                    if available
                    else f"；账期 {args.period} 还没有任何快照，先跑一次 reconq scan"
                ),
            )
        # 当前口径指纹：契约文件被改过时，复算结果翻转就可能来自口径变更
        # 而非数据出错。取不到指纹不猜（--contracts 指错了目录时按原样比）。
        current_digest = ""
        contract_path = os.path.join(args.contracts, f"{snapshot.channel}.yml")
        if os.path.isfile(contract_path):
            current_digest = fingerprint_source(contract_path).digest
        result = replay_snapshot(snapshot, current_contract_digest=current_digest)
    finally:
        conn.close()

    if args.json:
        print(_json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_replay(result))
        print()
        print(render_snapshot(snapshot))
    return EXIT_CHECKS_FAILED if result.replay_mismatch else EXIT_OK


def _print_lint_only(
    args: argparse.Namespace,
    channels: list[str],
    variables: dict,
) -> int:
    """``reconq verify --lint``：只校验契约就退出，不碰账单文件。"""
    import json as _json

    from .contracts.linter import errors_of, lint_source

    reports = []
    total_errors = 0
    for channel in channels:
        contract_path = os.path.join(args.contracts, f"{channel}.yml")
        if not os.path.isfile(contract_path):
            continue
        issues = lint_source(contract_path, provided_variables=variables)
        errors = errors_of(issues)
        total_errors += len(errors)
        reports.append(
            {
                "channel": channel,
                "contract": contract_path,
                "errors": len(errors),
                "warnings": len(issues) - len(errors),
                "issues": [i.as_dict() for i in issues],
            }
        )
    if args.json:
        print(
            _json.dumps(
                {"linted_channels": reports, "total_errors": total_errors},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for rep in reports:
            print(
                f"契约 {rep['contract']}：{rep['errors']} 个 ERROR，"
                f"{rep['warnings']} 个 WARN"
            )
            for issue in rep["issues"]:
                marker = "ERROR" if issue["severity"] == "error" else "WARN "
                where = issue["path"]
                if issue.get("yaml_line"):
                    where += f"（第 {issue['yaml_line']} 行）"
                print(f"  [{marker}] {issue['code']} {where}：{issue['message']}")
        if not reports:
            print(f"契约目录 {args.contracts} 下没有可校验的 .yml 契约")
    return EXIT_ERROR if total_errors else EXIT_OK


def _read_inbox_dir_isolated(inbox_root: str, period: str, channel: str, table):
    """读一路账单，交出每份文件的 ``FormatProbe`` 与**隔离的脏行**。

    复用 ``format_probe`` 的探测结果，不再单独探一遍 —— 探测与取数共用同
    一套判据，避免出现「跑口径时按 A 判格式、排障时按 B 判格式」的两套口径。

    ``isolate_empty_required=False``：verify 这条腿**不**因为主键列为空
    就把行拿走。空商户订单号恰恰是财务科要看的差异（Task 3 的
    ``wechat:12`` 证据行就是这么来的），它属于 ``not_empty`` 口径的职责；
    隔离区只接**解析层真的读不出这一行**的情形（列数错、金额解析不了、
    时间解析不了、含乱码）。落库那条腿（``quarantine scan``）才会用
    ``isolate_empty_required=True``，因为空主键行进不了幂等唯一键。
    """
    from .sources.format_probe import read_bill_file_isolated

    directory = os.path.join(inbox_root, period, channel)
    if not os.path.isdir(directory):
        return [], [], []
    files = sorted(
        name for name in os.listdir(directory)
        if name.lower().endswith((".csv", ".txt")) and not name.startswith(".")
    )
    rows: list[BillRow] = []
    probes: list = []
    dirty: list = []
    for name in files:
        file_rows, probe, file_dirty = read_bill_file_isolated(
            os.path.join(directory, name),
            table,
            channel,
            period=period,
            isolate_empty_required=False,
        )
        rows.extend(file_rows)
        probes.append(probe)
        dirty.extend(file_dirty)
    return rows, probes, dirty


def _discover_channels(contracts_dir: str) -> list[str]:
    """列出 ``contracts/`` 下的渠道契约。

    **下划线开头的文件不是渠道契约**（如 ``_scoring.yml`` 打分配置）。它们和
    渠道契约同目录，却不是 dataset 契约：当成渠道会让 verify/scan 去给
    ``_scoring.yml`` 找账单目录，整条命令直接失败。
    """
    if not os.path.isdir(contracts_dir):
        raise ReconqError("CONTRACT_MISSING_KEY", f"契约目录不存在：{contracts_dir}")
    return sorted(
        name[:-4]
        for name in os.listdir(contracts_dir)
        if name.endswith(".yml") and not name.startswith("_")
    )


def _sum_amount(rows: list[BillRow], table=None) -> str:
    """汇总本路的「主金额列」。

    列名不是硬编码的 ``amount``：医保契约写的是 ``total_amount``（金额合计），
    银联/自助机/微信叫 ``amount``。按契约 ``data_type: decimal`` 且**优先取
    契约里那条合计口径的列**来定主金额列，否则医保那路会在汇总里显示 0 元
    ——「显示 0」比「报错」更危险，财务科会以为这路没数据。
    """
    if table is not None:
        decimals = [c.name for c in table.columns if c.data_type == "decimal"]
        # 契约里的合计口径列优先
        preferred = [c for c in decimals if c in ("amount", "total_amount", "trade_amount")]
        if preferred:
            columns = preferred
        elif len(decimals) == 1:
            columns = decimals
        else:
            # 多列且无约定主列：取第一个（财务科看个量级），并由 check 逐列核
            columns = decimals[:1]
    else:
        columns = ["amount"]

    total = Decimal("0")
    for row in rows:
        for col in columns:
            raw = row.get(col)
            if not raw:
                continue
            try:
                total += Decimal(str(raw).replace(",", ""))
                break
            except Exception:
                continue
    return str(total)


def _print_channel(
    channel: str,
    name: str,
    row_count: int,
    results: list[CheckResult],
    note: str = "",
    format_probes: Optional[list] = None,
    dirty: Optional[list] = None,
) -> None:
    failed = sum(1 for r in results if r.status == "fail")
    print(f"\n=== 渠道 {channel}（{name}）· {row_count} 行 ===")
    if note:
        print(f"  ○ 未取到 — {note}")
        return
    if not results:
        print("  （本次没有匹配到要执行的口径）")
        return
    for probe in format_probes or []:
        print(
            f"  格式 {os.path.basename(probe.path)}："
            f"{probe.encoding} / 分隔符 {probe.delimiter!r} / 表头第 {probe.header_line_no} 行"
            + (f" / 剔除说明行 {probe.note_line_count} 条" if probe.note_line_count else "")
            + (f" / 剔除空行 {probe.blank_line_count} 条" if probe.blank_line_count else "")
        )
        if probe.suspect_flags:
            print(f"        存疑标记：{'、'.join(probe.suspect_flags)}")
    for res in results:
        mark = "✗" if res.status == "fail" else "✓"
        print(f"  {mark} [{res.check_path}] {res.check_name} — {res.message}")
        for ev in res.evidence:
            print(f"      证据 {ev.render()}")
        if res.truncated:
            print(f"      （证据已截断，共 {res.evidence_total} 条）")
    # Task 11：隔离的脏行必须在这一屏说清楚 —— 合计被读小了要能立刻看出
    # 「少了 N 行、去哪了、怎么回查」，否则这笔差额会一直挂在账上没人动。
    for rec in dirty or []:
        print(f"  ⚠ 隔离 {rec.row_ref}  [{rec.reason_code}] {rec.zh()}")
        if rec.detail:
            print(f"      {rec.detail}")
    print(f"  小计：{len(results)} 条口径，{failed} 条未通过")


def _print_footer(period: str, summary: list[dict], total_fail: int) -> None:
    not_fetched = [s["channel"] for s in summary if s["status"] == "not_fetched"]
    with_dirty = [s for s in summary if s.get("dirty_rows")]
    print(f"\n=== 账期 {period} 汇总 ===")
    print("  渠道        状态      笔数        金额合计(元)   口径(未通过)  脏行")
    for item in summary:
        if item["status"] == "checked":
            state = "已核对·有脏行" if item.get("dirty_rows") else "已核对"
        else:
            state = "未取到"
        print(
            f"  {item['channel']:<12} {state:<12} {item['row_count']:>6}  "
            f"{item['total_amount']:>14}   {item['check_total']}({item['check_failed']})"
            f"{item.get('dirty_rows', 0):>6}"
        )
    if with_dirty:
        detail = "、".join(
            f"{s['channel']}({s['dirty_rows']} 行)" for s in with_dirty
        )
        print(f"\n本期有隔离脏行的渠道：{detail}")
        print(
            "提示：这些行没参与口径，合计因此偏小。导出脏行台账回查导出方："
            f"reconq quarantine export --period {period} --out out/{period}-脏行台账.csv"
        )
    if not_fetched:
        print(f"\n未取到账单的渠道：{', '.join(not_fetched)}（本期结论不含这些渠道，需先补齐账单）")
    if total_fail:
        print(f"结论：{total_fail} 条口径未通过，账期 {period} 门禁不通过（退出码 1）")
    elif not_fetched:
        print(f"结论：已取到账单的渠道口径全部通过，但 {len(not_fetched)} 路未取到，账期 {period} 不可结账")
    elif with_dirty:
        print(
            f"结论：口径全部通过，但 {len(with_dirty)} 路有隔离脏行，"
            f"账期 {period} 的合计偏小，需回查后重跑（退出码 0，脏行不参与门禁）"
        )
    else:
        print(f"结论：全部口径通过，账期 {period} 门禁通过（退出码 0）")


# —— groups：同根因聚合与组级处置 ——————————————————————


def _groups_result(args: argparse.Namespace, conn):
    """拉本期条目并聚合。``list`` / ``slip`` / 三个动作腿共用这一段。"""
    from .align.aggregate import aggregate_discrepancies, load_group_entries

    entries = load_group_entries(
        conn,
        args.period,
        channel=getattr(args, "only_channel", "") or "",
        include_resolved=bool(getattr(args, "include_resolved", False)),
    )
    return aggregate_discrepancies(
        entries, amount_bucket_width=getattr(args, "amount_bucket", "") or "0.01"
    )


def _cmd_groups(args: argparse.Namespace) -> int:
    """``reconq groups [list|confirm|reject|withdraw|slip|actions]``。

    逐条台账回答「这笔钱为什么对不上」，本命令回答「这件事我办没办」——
    同一批单边账按「成因 + 渠道 + 形态 + 金额分桶」收成处置单，确认与
    驳回**按单**操作，组内逐条同步。

    退出码沿用本产品口径：0 = 这条腿做完了，1 = 门禁没过（有单没办完
    或本次动作被拒），2 = 运行不了（库没建、组键不存在、键不合法）。
    """
    import json as _json

    from .store.schema import connect

    from .align.aggregate import (
        ACTION_ZH,
        list_group_actions,
        render_group_slip_markdown,
        render_group_zh,
        render_summary_zh,
    )

    cmd = getattr(args, "groups_cmd", "list") or "list"
    if not getattr(args, "period", ""):
        print("用法：reconq groups <子动作> --period <账期，如 2026-W36> …")
        return EXIT_OK

    conn = connect(args.db)
    try:
        result = _groups_result(args, conn)

        if cmd == "list":
            if args.json:
                print(_json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            else:
                print(render_summary_zh(result))
                for group in result.groups:
                    print()
                    print(render_group_zh(group))
            pending = [g for g in result.groups if g.disposition == "待确认"]
            if pending and not args.json:
                print(
                    f"\n待办：{len(pending)} 张处置单（{sum(g.size for g in pending)} 条差异）"
                    f"待确认。整单办完用：\n"
                    f"  reconq groups confirm --period {args.period} "
                    f"--group-key '<处置单键>' --operator <你的工号>"
                )
            return EXIT_CHECKS_FAILED if pending else EXIT_OK

        if cmd == "slip":
            picked = (
                [g for g in result.groups if g.group_key == args.group_key]
                if args.group_key
                else list(result.groups)
            )
            if args.group_key and not picked:
                print(
                    f"reconq: 处置单 {args.group_key!r} 在 {args.period} 不存在；"
                    f"先跑 reconq groups list --period {args.period} 看本期有哪些单",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            actions = list_group_actions(conn, args.period, group_key=args.group_key)
            markdown = render_group_slip_markdown(
                picked,
                period=args.period,
                operator=args.operator,
                note=args.note,
                actions=actions,
            )
            if args.out:
                os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
                with open(args.out, "w", encoding="utf-8") as handle:
                    handle.write(markdown)
                print(f"交办单已导出：{args.out}（{len(picked)} 张处置单）")
            else:
                print(markdown)
            return EXIT_OK

        if cmd == "actions":
            rows = list_group_actions(conn, args.period, group_key=args.group_key)
            if args.json:
                print(_json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            elif not rows:
                print(f"{args.period} 没有任何组级处置留痕")
            else:
                print(f"{args.period} 组级处置留痕（共 {len(rows)} 条）")
                for row in rows:
                    mark = "✓" if int(row.get("advanced") or 0) else "✗"
                    print(
                        f" {mark} {row.get('recorded_at', '')} "
                        f"{ACTION_ZH.get(str(row.get('action')), row.get('action'))} "
                        f"操作人 {row.get('operator', '')} "
                        f"单 {row.get('group_key', '')} "
                        f"{row.get('member_key', '')}"
                        + (f"  [{row.get('error_code')}]" if row.get("error_code") else "")
                    )
            return EXIT_OK

        # —— confirm / reject / withdraw ——
        if not args.group_key:
            print(
                f"用法：reconq groups {cmd} --period {args.period} "
                f"--group-key '<处置单键>' --operator <你的工号>",
                file=sys.stderr,
            )
            return EXIT_OK
        if not args.operator:
            print(
                f"reconq: --operator 必填。处置状态只能由人推进 —— "
                f"模型草稿、规则基线与降级路径都不会、也不该替你点这一下",
                file=sys.stderr,
            )
            return EXIT_ERROR
        group = result.group(args.group_key)
        if group is None:
            print(
                f"reconq: 处置单 {args.group_key!r} 在 {args.period} 不存在"
                f"（可能已被上一轮动作改判而重新分组）。"
                f"先跑 reconq groups list --period {args.period} 看本期有哪些单",
                file=sys.stderr,
            )
            return EXIT_ERROR

        from .align.aggregate import apply_group_action

        outcome = apply_group_action(
            conn, group, cmd, operator=args.operator, note=args.note
        )
        if args.json:
            print(_json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2, default=str))
        else:
            print(outcome.render_zh())
        log_event(
            "group.action",
            level="info" if outcome.ok else "warning",
            message=outcome.render_zh().splitlines()[0],
            period=args.period,
            code=outcome.error_code,
        )
        return EXIT_OK if outcome.ok else EXIT_CHECKS_FAILED
    finally:
        conn.close()


def _cmd_precedent(args: argparse.Namespace) -> int:
    """``reconq precedent [top|list|hits]``。

    回答「这类问题历史上有人怎么判过」：``top`` 给本渠道历史最常见成因 Top N
    （先例看板那一格），``list`` 列先例明细与其特征签名，``hits`` 列谁在何时
    采信了哪条先例（审计反查）。

    退出码沿用本产品口径：0 = 这条腿做完了，2 = 运行不了（渠道名不合法 /
    ``hits`` 缺账期）。
    """
    import json as _json

    from .align.precedent import (
        SIGNATURE_FIELDS,
        list_hits,
        list_precedents,
        top_causes,
    )
    from .store.schema import connect

    cmd = getattr(args, "precedent_cmd", "top") or "top"
    diff_key = getattr(args, "diff_key", "") or ""

    if cmd == "hits" and not getattr(args, "period", ""):
        print(
            "reconq: precedent hits 需要 --period（如 2026-W36）—— "
            "采信留痕是按账期记的",
            file=sys.stderr,
        )
        return EXIT_ERROR

    conn = connect(args.db)
    try:
        if cmd == "hits":
            rows = [
                hit.to_dict()
                for hit in list_hits(
                    conn, args.period, channel=args.channel, diff_key=diff_key
                )
            ]
            if args.json:
                print(_json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            elif not rows:
                print(
                    f"{args.period} 没有任何先例采信留痕"
                    f"{'（' + args.channel + '）' if args.channel else ''}。"
                    "先例只是建议，是否采信由人点；没人点就没有留痕，这不是故障"
                )
            else:
                print(f"{args.period} 先例采信留痕（共 {len(rows)} 条）")
                for row in rows:
                    print(
                        f" {row['recorded_at']} {row['channel']} "
                        f"{row['cause_code']}（权威度 {row['authority']}，"
                        f"当时命中 {row['hit_count_at']} 次）"
                        f" ← {row['actor']} {row['diff_key']}"
                    )
                    if row["note"]:
                        print(f"    备注：{row['note']}")
            return EXIT_OK

        if cmd == "list":
            rows = [
                item.to_dict()
                for item in list_precedents(
                    conn,
                    channel=args.channel,
                    cause_code=args.cause,
                    min_hits=args.min_hits,
                )
            ]
            if args.json:
                print(_json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            elif not rows:
                print(
                    "先例库为空。经办人确认过的处置结论才会沉淀成先例 —— "
                    "还没人办过单时这是正常态，不是故障"
                )
            else:
                print(f"先例明细（共 {len(rows)} 条，按命中次数降序）")
                print(f"检索特征（签名口径）：{'、'.join(SIGNATURE_FIELDS)}")
                for row in rows:
                    print(
                        f" {row['zh_name']}（{row['cause_code']}）"
                        f" 渠道 {row['channel']} 命中 {row['hit_count']} 次"
                        f" 最近 {row['last_hit_at'] or '未记录'}"
                        f" 签名 {row['signature']}"
                    )
                    features = row["features"]
                    print(
                        "    特征："
                        + "、".join(f"{k}={features.get(k, '')}" for k in SIGNATURE_FIELDS)
                    )
                    print(
                        f"    登记时版本：字典 {row['dict_version']}"
                        f"@{row['dict_digest']} / 提示词 {row['prompt_version']}"
                        f"@{row['prompt_digest']}"
                    )
                    if row["sample_keys"]:
                        print(f"    样本差异键（最近）：{'、'.join(row['sample_keys'][-3:])}")
            return EXIT_OK

        # —— top：先例看板 ——
        rows = [row.to_dict() for row in top_causes(conn, channel=args.channel, limit=args.limit)]
        if args.json:
            print(_json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        elif not rows:
            print(
                f"{args.channel or '全渠道'} 还没有先例。"
                "先例来自经办人确认过的处置结论，本周还没人办过单时这是正常态"
            )
        else:
            print(
                f"历史最常见成因 Top {len(rows)}（先例看板）"
                f"{'· ' + args.channel if args.channel else ''}"
            )
            print(_pad("成因", 34) + _pad("责任方", 14) + "先例条数  命中次数  最近命中")
            print("-" * 92)
            for row in rows:
                label = f"{row['zh_name']}（{row['cause_code']}）"
                print(
                    _pad(label, 34)
                    + _pad(row["owner"], 14)
                    + _pad(str(row["precedent_count"]), 8, right=True)
                    + _pad(str(row["hit_count"]), 9, right=True)
                    + f"  {row['last_hit_at'] or '未记录'}"
                )
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
