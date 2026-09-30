"""Web 层交互测试的共用夹具（Task 49）。

与 ``tests/test_web_panel.py`` / ``tests/test_web_filters.py`` 的分工
--------------------------
那两个文件是**冒烟**与**筛选**的断言；本目录是**服务端 API 级交互**
的验收入口，覆盖 SSE 事件序列、状态机越权、并发冲突与「模型/规则推不
动状态」这条硬边界。放一个子目录而不是继续往那两个文件里堆，是因为
这一层的用例都**慢**（每个用例要真跑一遍 ``reconq scan``）且共享同一
批夹具，混在一起会让两个文件都变得没法单独跑。

库是真扫出来的
--------------
夹具用**真跑一遍** ``reconq scan`` 造库，而不是手工 ``INSERT``。面板的
每个字段都声称与 CLI 同源，那么它的测试数据就该也来自 CLI —— 手写的行
一旦与 ``reconq scan`` 的实际落库形状漂了，测试会绿而面板坏。

⚠️ 本目录**不连接任何真实渠道或 HIS 库**。账单来自仓库里的脱敏样例
``inbox/2026-W36/``，凭据环境变量一个都没设，因此另外五路渠道在面板上
是**灰卡 + 错误码** —— 那正是本轮真实的部署形态，测试不假装它们是绿的。
"""

from __future__ import annotations

import os
import shutil
import urllib.parse

import pytest

#: 项目根（含 ``contracts/`` 与 ``inbox/`` 的那一层）。``reconq scan`` 按
#: **当前工作目录**找契约目录，所以测试要 chdir 过去。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 本轮唯一的样例账期。**做成夹具而不是裸常量**：``tests/web/`` 不是
#: Python 包（同级 ``tests/`` 也没有 ``__init__.py``），模块之间
#: ``from .conftest import ...`` 会直接 ImportError，而复制一份常量到
#: 八个文件里则迟早分叉。
PERIOD = "2026-W36"

#: 样例账期里两条真实差异的口径（``reconq scan`` 跑出来的）。
CHECK_REFUND = "rows.refund_sum.zero"
CHECK_DUPLICATE = "rows.order_no.unique"

#: 样例账期微信账单的真实口径数字。**写死是故意的** —— 面板上「合计」
#: 走定点字符串，若哪天取数器把它算成了 float 或漏了一行，这条断言会
#: 立刻红，而不是让一个「差不多对」的数字一路走进结论单。
WECHAT_ROWS = 19
WECHAT_TOTAL = "5394.69"


@pytest.fixture()
def period() -> str:
    return PERIOD


@pytest.fixture()
def check_refund() -> str:
    return CHECK_REFUND


@pytest.fixture()
def check_duplicate() -> str:
    return CHECK_DUPLICATE


@pytest.fixture()
def wechat_rows() -> int:
    return WECHAT_ROWS


@pytest.fixture()
def wechat_total() -> str:
    return WECHAT_TOTAL


@pytest.fixture()
def opened_db(scanned_db):
    """在真库上开一张 ``待归因`` 处置单。

    中栏那两行是**差异**，不是**处置单** —— 差异在、处置单还没开时
    ``decide`` 端点给 404 而不替它开一张。凡是要打写入口的用例都得先过
    这个夹具，否则它们测的是「找不到单」而不是「改得动单」。
    """
    from reconq.ledger.flow import open_discrepancy
    from reconq.store.schema import connect

    conn = connect(scanned_db)
    try:
        open_discrepancy(
            conn, period=PERIOD, channel="wechat",
            check_path=CHECK_REFUND, diff_amount="86.00",
            evidence_rows=("wechat_tradebill_2026-W36.csv:2",),
        )
    finally:
        conn.close()
    return scanned_db


@pytest.fixture()
def scanned_db(tmp_path):
    """跑一次 ``reconq scan`` 造出真库（每个用例一份，互不干扰）。"""
    from reconq.cli import main

    db = str(tmp_path / "reconq.db")
    inbox = tmp_path / "inbox"
    shutil.copytree(os.path.join(_PROJECT_ROOT, "inbox", PERIOD), inbox / PERIOD)

    # ``reconq scan`` 按 cwd 找 ``contracts/``。切到项目根再跑，
    # 用 try/finally 而不是 pytest 的 chdir —— 后者不复原会污染后续用例。
    cwd = os.getcwd()
    os.chdir(_PROJECT_ROOT)
    try:
        code = main(
            [
                # ``--no-log`` 是**全局**参数（控制结构化日志写不写盘），
                # 必须排在子命令前面；放在 scan 后面 argparse 会直接拒。
                "--no-log",
                "scan",
                "--period", PERIOD,
                "--db", db,
                "--source", str(inbox),
            ]
        )
    finally:
        os.chdir(cwd)
    # 退出码非 0 是正常的：这期账本来就有差异，而差异不是错误。
    assert code in (0, 1), f"scan 意外失败，退出码 {code}"
    return db


@pytest.fixture()
def client(scanned_db):
    """起一个测试应用。``start_watcher=False`` 是**默认选择**。

    轮询线自己会起一个后台线程去 poll 账单库，而本目录的并发用例要断言
    「同一时刻只有一个确认成功」—— 多一个轮询线程在写同一个库，这条
    断言就成了在测线程调度。需要看轮询线行为的用例自己去建
    :class:`~reconq.web.events.DbWatcher`。
    """
    from fastapi.testclient import TestClient

    from reconq.web.app import create_app

    with TestClient(create_app(scanned_db, start_watcher=False)) as c:
        yield c


# ─────────────────────────── 小工具 ───────────────────────────


def open_ledger(db: str, *, channel: str = "wechat", check_path: str = CHECK_REFUND,
                amount: str = "86.00", order_no: str = ""):
    """在真库上开一张 ``待归因`` 处置单，返回那一行。"""
    from reconq.ledger.flow import open_discrepancy
    from reconq.store.schema import connect

    conn = connect(db)
    try:
        return open_discrepancy(
            conn,
            period=PERIOD,
            channel=channel,
            check_path=check_path,
            order_no=order_no,
            diff_amount=amount,
            evidence_rows=(f"{channel}_bill_2026-W36.csv:2",),
        )
    finally:
        conn.close()


def url_of(key: str, action: str = "decide") -> str:
    """差异键含 ``|`` 与 ``.``，**必须百分号编码**才能当路径段。

    浏览器与多数反代不会把未编码的 ``|`` 截断，但 FastAPI 的路径匹配会：
    未编码时 ``/api/discrepancy/2026-W36|wechat|rows.refund_sum.zero/decide``
    会被解析成 ``{key:path}`` 之外的形状，404 的原因与「这条不存在」
    长得一模一样 —— 而它其实只是链接坏了。
    """
    return f"/api/discrepancy/{urllib.parse.quote(key, safe='')}/{action}"
