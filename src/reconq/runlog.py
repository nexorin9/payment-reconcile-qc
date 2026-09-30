"""结构化 JSON 日志与脱敏（Task 20）。

信息科排障时最缺的不是「报错了」，而是**「这次到底哪一路、哪一步、因为
什么没成、跑了多久、峰值内存多少」**。文本日志回答不了其中任何一个问题：
渠道名写在句子中间，耗时得靠掐表，失败原因混在一大段中文里。

本模块把每条日志固定成机器可读的一行 JSON：

    {"ts": "...", "level": "INFO", "event": "channel.fetch.done",
     "channel": "wechat", "period": "2026-W36", "code": "",
     "duration_ms": 12, "row_count": 20, "total_amount": "5714.69"}

设计口径三条：

1. **日志目录在 round 根的 ``logs/``，不写项目子目录。** 项目子目录是要
   直接发布到 GitHub 的，日志属于运行期产物；``RECONQ_LOG_DIR`` 可覆盖。
2. **脱敏在写盘前做，不靠调用方自觉。** 账单行本身不含患者明细，但脏行隔离
   台账存的是**原始行文本**——导出方哪天把患者姓名一起塞进备注列，那行
   文本就会顺着 ``logger.warning`` 流进日志库。本模块在 Formatter 里统一
   拦一道，并把命中的模式名记进 ``redactions`` 字段（**只记模式名，不记
   命中内容**：记内容等于把脱敏绕回去了）。
3. **不吞异常。** 这里只做记录与脱敏，异常怎么分类由 ``errors.py`` 决定。

源产品对应：``soda-core`` 用 ``logging`` + ``ExitCode`` 让退出码承载失败
语义，但日志是自由文本。本产品把同一批信息拆成两条通道——退出码回答
「成没成」，本模块的 JSON 日志回答「为什么」。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any, Iterable, Optional

__all__ = [
    "JsonLogFormatter",
    "RedactingFilter",
    "configure_logging",
    "log_event",
    "default_log_dir",
    "redact_text",
    "redact_payload",
    "PATIENT_PATTERNS",
    "PATIENT_KEYS",
    "LOG_EVENTS",
    "reset_logging",
]

#: 根 logger 名。所有模块用 ``logging.getLogger("reconq.xxx")``，挂到这里。
ROOT_LOGGER_NAME = "reconq"


# —— 脱敏 ——————————————————————————————————————————————————————

#: 账单行里**不该**出现的东西。命中即在写盘前替换成占位符。
#:
#: 顺序有意义：身份证与手机号放在前面，否则 18 位身份证里的后 11 位会先被
#: 手机号规则吃掉，剩下的碎片反而更容易误判成别的模式。
#:
#: **一条要说清的边界**：模式匹配的是「带标签的标识」与「有固定形状的
#: 号码」（手机号、身份证），**不是**任意人名。裸姓名（「张三」）在文本里
#: 与科室名、导出人名、备注词在形态上无法区分，硬要匹配只能靠姓氏表 ——
#: 那会大面积误伤正常中文（``李`` 开头的产品名、``王`` 姓的导出人）。
#: 姓名靠 :data:`PATIENT_KEYS` 的**键名**判定（``patient_name: 张三``
#: 一定拦得住），以及带标签的形态（``患者姓名：张三``）。
#: 真正把裸姓名拦在门外的不是正则，是**账单导出不该有这一列** ——
#: 账单对账的数据面是金额与单号，不含患者明细。
PATIENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "id_card",
        re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])"),
    ),
    ("phone", re.compile(r"(?<![0-9])1[3-9]\d{9}(?![0-9])")),
    (
        "inpatient_no",
        re.compile(r"(住院号|病案号|门诊号|就诊号)\s*[:：=]?\s*[A-Za-z0-9]{4,}"),
    ),
    (
        "patient_name_labeled",
        re.compile(r"(患者姓名|病人姓名|姓名)\s*[:：=]\s*[^,，;；\s|]{1,12}"),
    ),
    # 住院号/病案号的**裸值形态**：以 ZY/BA/ MZ 等院内常见前缀开头、后跟
    # 6 位以上数字。带前缀是刻意的 —— 裸数字串会与金额、笔数、订单号
    # 冲突（``100.00`` 也会被当成「一串数字」）。
    (
        "inpatient_no_prefixed",
        re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{6,}(?![0-9A-Za-z])"),
    ),
)

#: 载荷里这些**键**的值一律替换，不看内容（键名本身就说明是患者标识）。
PATIENT_KEYS = frozenset(
    {
        "patient_name",
        "patient_id",
        "inpatient_no",
        "outpatient_no",
        "medical_record_no",
        "mpi",
        "id_card",
        "id_no",
        "phone",
        "mobile",
        "姓名",
        "患者姓名",
        "住院号",
        "病案号",
        "门诊号",
        "身份证号",
    }
)

#: 命中后写进日志的占位符。刻意写成中性词，不复述原值。
PLACEHOLDER = "[已脱敏]"


def redact_text(text: Any) -> tuple[str, list[str]]:
    """把一段文本里的患者标识替换成占位符。

    返回 ``(脱敏后的文本, 命中的模式名列表)``。模式名去重且有序，便于面板
    显示「本期日志脱敏命中：id_card、phone」而不显示命中了什么。
    """
    if not isinstance(text, str) or not text:
        return (text if isinstance(text, str) else str(text), [])
    hits: list[str] = []
    for name, pattern in PATIENT_PATTERNS:
        replaced, count = pattern.subn(f"{PLACEHOLDER}", text)
        if count:
            hits.append(name)
            text = replaced
    return text, hits


def redact_payload(payload: Any) -> tuple[Any, list[str]]:
    """递归脱敏一个可 JSON 序列化的载荷。

    键命中 :data:`PATIENT_KEYS` 时整个值替换；其余字符串按
    :func:`redact_text` 的模式过一道。字典键本身也过一遍脱敏 ——
    脏行台账的 ``raw_line`` 是值，但导出方自造的列名可能带患者名。
    """
    hits: list[str] = []

    def _walk(node: Any, key_is_patient: bool = False) -> Any:
        if key_is_patient:
            if node not in (None, "", [], {}):
                hits.append("key:" + "<敏感键>")
            return PLACEHOLDER if node not in (None, "") else node
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for raw_key, value in node.items():
                key, key_hits = redact_text(str(raw_key))
                hits.extend(key_hits)
                out[key] = _walk(value, str(raw_key).strip().lower() in PATIENT_KEYS)
            return out
        if isinstance(node, (list, tuple)):
            walked = [_walk(item) for item in node]
            return walked if isinstance(node, list) else tuple(walked)
        if isinstance(node, str):
            text, text_hits = redact_text(node)
            hits.extend(text_hits)
            return text
        return node

    return _walk(payload), sorted(set(hits))


#: 日志字段白名单 + 事件名常量。事件名是**受控集合**：自由文本事件名会让
#: 「按事件聚合排障」这件事退化成 grep。
LOG_EVENTS = frozenset(
    {
        "run.start",
        "run.finish",
        "contract.lint",
        "channel.init",
        "channel.init.failed",
        "channel.fetch",
        "channel.fetch.done",
        "channel.fetch.failed",
        "channel.ingest",
        "channel.ingest.done",
        "channel.check.done",
        "channel.quarantine",
        "channel.not_fetched",
        "scan.lock.acquired",
        "scan.lock.denied",
        "scan.state.transition",
        "scan.resume",
        "scan.gate",
        "error.raised",
        "model.call",
        "model.call.failed",
        "model.degraded",
        "model.breaker.open",
        "model.breaker.half_open",
        "model.breaker.closed",
        "align.done",
        "align.stage",
        "align.pipeline.done",
        "align.scoring.config",
        "attribution.done",
        "attribution.verify",
        "attribution.baseline",
        "cause.dict.version",
        "export.done",
        "wechat.notify",
    }
)

#: 固定字段（题目要求的七项），其余作为 extras 落盘。
_FIXED_FIELDS = ("ts", "level", "event", "channel", "period", "code", "duration_ms")


class JsonLogFormatter(logging.Formatter):
    """把 LogRecord 渲染成一行 JSON。"""

    def __init__(self, *, redact: bool = True) -> None:
        super().__init__()
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - 覆写父类
        event = getattr(record, "event", record.name)
        extras = getattr(record, "event_fields", None) or {}
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "event": event,
            "channel": extras.pop("channel", "") or "",
            "period": extras.pop("period", "") or "",
            "code": extras.pop("code", "") or "",
            "duration_ms": extras.pop("duration_ms", None),
        }
        payload.update(extras)
        if record.exc_info:
            # 只记异常类型与业务码，不记整段栈：栈进日志库后没人读，
            # 却会把出错函数里的局部变量（可能含原始行文本）一起带进去。
            exc = record.exc_info[0]
            payload["exc_type"] = getattr(exc, "__name__", str(exc))
            payload["exc_code"] = getattr(record.exc_info[1], "code", "") or ""
        message = record.getMessage()
        if self._redact:
            payload, hits = redact_payload(payload)
            message, msg_hits = redact_text(message)
            hits = sorted(set(hits) | set(msg_hits))
            if hits:
                payload["redactions"] = hits
        elif record.exc_info:
            exc = record.exc_info[0]
            payload["exc_type"] = getattr(exc, "__name__", str(exc))
        payload["message"] = message
        return json.dumps(payload, ensure_ascii=False, default=str)


class RedactingFilter(logging.Filter):
    """在 Formatter 之前就把消息里的患者标识换掉。

    与 Formatter 里的脱敏**重复**是有意的：库内若还有别的 handler（stderr、
    第三方库自己的 handler），Formatter 只管自己那条出口。这道 Filter 挂在
    logger 上，管的是「这条记录要发往的所有出口」。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - getMessage 极少失败
            return True
        cleaned, hits = redact_text(message)
        if hits:
            record.msg = cleaned
            record.args = ()
            existing = list(getattr(record, "event_fields", {}) or {})
            existing.append("redactions")
            record.event_fields = {**(getattr(record, "event_fields", {}) or {}),
                                   "redactions": sorted(set(hits))}
        return True


def default_log_dir() -> str:
    """默认日志目录：round 根的 ``logs/``，不写项目子目录。

    判定顺序：``RECONQ_LOG_DIR`` → 上级目录里有 ``task.json``（说明当前目录
    是 round 下的项目子目录）则用 ``../logs`` → 当前目录 ``logs/``。
    """
    env = os.environ.get("RECONQ_LOG_DIR")
    if env:
        return env
    cwd = os.getcwd()
    parent = os.path.dirname(cwd)
    if parent and os.path.isfile(os.path.join(parent, "task.json")):
        return os.path.join(parent, "logs")
    return os.path.join(cwd, "logs")


def configure_logging(
    *,
    log_dir: Optional[str] = None,
    level: str = "INFO",
    to_file: bool = True,
    to_stderr: bool = False,
    console_level: str = "WARNING",
    logger_name: str = ROOT_LOGGER_NAME,
) -> logging.Logger:
    """配置结构化日志。幂等：重复调用只换 handler，不叠加。"""
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - 关 handler 失败不该挡住主流程
            pass

    logger.addFilter(RedactingFilter())
    # propagate=False：JSON 日志走自己的出口；不重复冒泡到 root（root 上
    # 若有 basicConfig 的 handler，同一条记录会被打两遍，运维看着像跑了两次）。
    logger.propagate = False

    formatter = JsonLogFormatter()
    if to_file:
        directory = log_dir or default_log_dir()
        os.makedirs(directory, exist_ok=True)
        name = time.strftime("reconq-%Y%m%d.jsonl", time.localtime())
        file_handler = logging.FileHandler(
            os.path.join(directory, name), encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.addHandler(file_handler)

    if to_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(getattr(logging, console_level.upper(), logging.WARNING))
        logger.addHandler(stream)
    return logger


def reset_logging(logger_name: str = ROOT_LOGGER_NAME) -> None:
    """拆掉本模块装过的 handler，并恢复默认传播（测试隔离用）。

    ``propagate`` 必须一并复原。:func:`configure_logging` 把它设成
    ``False``（避免同一条记录在 root 上被打两遍），而 ``caplog`` 靠的正是
    向 root 冒泡 —— 只拆 handler 不复原 propagate，后面任何用 ``caplog``
    的测试都会**单跑绿、全量红**：症状是「前一个测试先跑过一次
    ``configure_logging``，我的事件就再也收不到了」，而报错信息里完全
    看不出是这个原因。这正是 tests/test_scoring.py 里点名要避开的
    「顺序相关的假绿」，本函数就是那道防线本身。
    """
    logger = logging.getLogger(logger_name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover
            pass
    for filt in list(logger.filters):
        if isinstance(filt, RedactingFilter):
            logger.removeFilter(filt)
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


def log_event(
    event: str,
    *,
    level: str = "info",
    message: str = "",
    channel: str = "",
    period: str = "",
    code: str = "",
    duration_ms: Optional[float] = None,
    logger_name: str = ROOT_LOGGER_NAME,
    **fields: Any,
) -> None:
    """记一条结构化事件。

    ``event`` 必须在 :data:`LOG_EVENTS` 里；不在其中时按 ``unknown`` 记并在
    字段里留原名——不静默改名，也不因此抛错（打日志不该把主流程带崩）。
    """
    logger = logging.getLogger(logger_name)
    effective = event if event in LOG_EVENTS else "unknown"
    payload: dict[str, Any] = {
        "channel": channel,
        "period": period,
        "code": code,
        "duration_ms": duration_ms,
    }
    payload.update(fields)
    if effective == "unknown":
        payload["unregistered_event"] = event
    logger.log(
        getattr(logging, level.upper(), logging.INFO),
        message or event,
        extra={"event": effective, "event_fields": payload},
    )


class elapsed:
    """``with elapsed() as t: ...`` → ``t.ms`` 是这段耗时的毫秒数。

    刻意用 :func:`time.perf_counter` 而不是 ``time.time``：扫描里要做
    渠道级耗时对比，墙上时钟被 NTP 校时跳一下就会算出负数耗时。
    """

    __slots__ = ("_start", "ms")

    def __enter__(self) -> "elapsed":
        self._start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc: Any) -> None:
        self.ms = round((time.perf_counter() - self._start) * 1000, 3)


def iter_log_files(log_dir: str) -> Iterable[str]:
    """列出某目录下已落盘的 JSON 日志文件（排障腿用）。"""
    if not os.path.isdir(log_dir):
        return []
    return sorted(
        os.path.join(log_dir, name)
        for name in os.listdir(log_dir)
        if name.startswith("reconq-") and name.endswith(".jsonl")
    )
