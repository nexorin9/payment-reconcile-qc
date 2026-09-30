"""全测试套件共用的夹具。

只放**跨文件的隔离保证**；单个模块自己的 helper 留在各自文件里。

为什么需要 logger 快照夹具
--------------------------
``reconq.cli.main()`` 在派发任何子命令前都会调
:func:`reconq.runlog.configure_logging`，而它会给 ``reconq`` logger 设
``propagate=False`` + 挂一个 ``FileHandler``。产品语义这是对的（同一条
记录不在 root 上被打两遍），但对测试是**全局状态泄漏**：任何跑过一条
CLI 命令的用例，都会让其后所有用 ``caplog`` 收事件的用例收不到东西。

症状极具迷惑性 —— 目标用例**单跑绿、全量红**，报错是「事件列表为空」，
完全看不出是前一个用例的副作用。本夹具在每个用例前后各拍一次快照、
跑完复原，把这个顺序相关性彻底掐断（而不是让每个用 ``caplog`` 的用例
各自记得调一次 :func:`reset_logging`）。
"""

from __future__ import annotations

import logging

import pytest

#: 需要快照的 logger。除 ``reconq`` 外还有渠道取数用的命名空间 logger
#: （``reconq.gateway`` / ``reconq.period`` 等）——它们默认向 ``reconq``
#: 冒泡，一并复原才不会漏。
_SNAPSHOT_LOGGERS = ("reconq",)


@pytest.fixture(autouse=True)
def _isolate_reconq_logger():
    """每个用例前后复原 ``reconq`` logger 的 handler / propagate / level。"""
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in _SNAPSHOT_LOGGERS
    }
    try:
        yield
    finally:
        for name, (handlers, propagate, level) in saved.items():
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                if handler not in handlers:
                    logger.removeHandler(handler)
                    try:
                        handler.close()
                    except Exception:  # pragma: no cover - 关 handler 失败不该让测试红
                        pass
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)
