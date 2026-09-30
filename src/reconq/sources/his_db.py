"""HIS 只读库流式取数（Task 6）。

源产品对应实现
--------------
1. ``github_ref/soda-core/soda-core/src/soda_core/common/data_source_connection.py:393``
   的 ``execute_query_one_by_one()``：``fetchone()`` 逐行喂给回调，调用方
   永远只看得到一行，不物化整个结果集。
2. 同文件 :425 的 ``supports_streaming_fetch()``：基类返回 ``False``，只有
   真有服务端游标的适配器才覆写为 ``True``；基类的
   ``execute_query_one_by_one_prefer_streaming()`` 用它决定「值不值得」
   走流式。
3. ``github_ref/soda-core/soda-postgres/src/soda_postgres/common/data_sources/
   postgres_data_source_connection.py:26-46`` 的自适应分批：
   ``STREAM_FETCH_BUDGET_BYTES = 8MB``、``STREAM_FETCH_MAX_BATCH_ROWS = 1000``、
   ``STREAM_FETCH_GROWTH_FACTOR = 4``、``_estimate_row_bytes()``。
4. 同文件 :165-288 的 ``_execute_query_one_by_one_streaming()``：先取**一行
   探针**，之后按「已见最宽行」反算批大小，并**限制每批最多放大 4 倍**。

本轮怎么改写
------------
源实现服务的是「诊断仓库的失败行泵送」，本产品服务的是**六路渠道账单 × HIS
收费流水**的对账取数，主路径不变：一次查询 → 逐行流式 → 每行交给对齐器。
两处语义收紧：

* **只 SELECT 需要的列**，不 ``SELECT *``。源实现没有这条约束（它的目标
  是「拿到全部失败行」），而本产品要打的是院内生产库，列越少 IO 越少、
  撞上慢查询的概率越低。
* **按账期时间窗分批**。源实现是「一个查询流到底」，本产品把账期切成
  ``[周一 00:00, 下周一 00:00)`` 半开区间逐窗拉，避免一个账期扫到全院
  三年历史。

⚠️ 本模块**只读**。产品代码在任何路径上都不发 DML/DDL（Task 12 补连接池
与只读断言）；``HIS_DB_DSN`` 必须使用只读账号，凭据从环境变量读。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator, Optional, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, NoSuchModuleError, OperationalError

from ..engine.checks import BillRow
from ..errors import ReconqError
from .properties import HisProperties, build_properties
from .timezone import PeriodWindow, period_window, to_session_tz

__all__ = [
    "STREAM_FETCH_BUDGET_BYTES",
    "STREAM_FETCH_MAX_BATCH_ROWS",
    "STREAM_FETCH_GROWTH_FACTOR",
    "CHARGE_COLUMNS",
    "estimate_row_bytes",
    "AdaptiveBatchSizer",
    "StreamStats",
    "HisFetchResult",
    "build_charge_query",
    "split_sql_statements",
    "run_with_deadline",
    "stream_charge_rows",
    "fetch_his_charge_rows",
    "probe_his_connection",
]

logger = logging.getLogger("reconq.his_db")

#: 客户端缓冲预算。与参考实现同值：8MB。
STREAM_FETCH_BUDGET_BYTES: int = 8 * 1024 * 1024
#: 单批行数上限。与参考实现同值：1000。
STREAM_FETCH_MAX_BATCH_ROWS: int = 1000
#: 每批最多放大倍数。与参考实现同值：4。
#: 没有这个爬坡，一条「探针瘦行」（NULL 多、无 ORDER BY 相关性）会直接把
#: 批大小顶到行数上限，而此时一行胖数据都还没见过——1000 × 0.5MB 就是
#: 一次 500MB 的 fetchmany。限制住之后，超调最多是上一批的 4 倍。
STREAM_FETCH_GROWTH_FACTOR: int = 4

#: 收费流水**只取这些列**。不 SELECT *：院内生产库上少读一列就少一份 IO，
#: 也少一分撞慢查询的机会。列名与 ``contracts/his.yml`` 的 column 规范名一致。
CHARGE_COLUMNS: tuple[str, ...] = (
    "his_order_no",
    "charge_id",
    "amount",
    "charge_time",
    "period_code",
    "department",
    "settle_type",
    "is_refund",
)

#: 方言 → 是否真支持服务端（命名）游标。只有 PostgreSQL 系走到了参考实现
#: 那条按字节预算分批的路径；MySQL 系走客户端游标，仍逐行 fetch，但批
#: 大小照样按预算收，不会无界增长。
_SERVER_SIDE_STREAMING_DIALECTS = frozenset({"postgresql", "postgres"})

# —— SQL 语句切分掩码（Task 12，只读守卫的判据底座）—————
_BLOCK_COMMENT_MASK = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_MASK = re.compile(r"--[^\n]*")
_STRING_MASK = re.compile(r"'(?:[^']|'')*'")
_DQ_STRING_MASK = re.compile(r'"(?:[^"]|"")*"')


def estimate_row_bytes(row: Sequence[Any]) -> int:
    """估算一行的字节数。

    参考实现的 ``_estimate_row_bytes()`` 用 ``estimate_value_size()`` **递归**
    估算，因为 psycopg3 会把 jsonb 返回成已解析的 dict/list——浅层
    ``getsizeof`` 把一个 10MB 的 json 文档算成几百字节，批大小就永远顶在
    行数上限，内存直接爆。这里保留递归：容器逐元素下钻，标量取
    ``getsizeof``，再对字符串按 ``sys.getsizeof`` 修正（DBAPI 返回的
    ``str`` 已是真实对象，``getsizeof`` 够用；容器只加自身开销）。
    """
    total = 0
    for value in row:
        total += _value_size(value)
    return total


def _value_size(value: Any) -> int:
    import sys

    if value is None:
        return 0
    if isinstance(value, (str, bytes, bytearray)):
        return sys.getsizeof(value)
    if isinstance(value, dict):
        return sys.getsizeof(value) + sum(
            _value_size(k) + _value_size(v) for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return sys.getsizeof(value) + sum(_value_size(v) for v in value)
    return sys.getsizeof(value)


class AdaptiveBatchSizer:
    """按字节预算 + 最宽行反算批大小，且每批最多放大 4 倍。

    语义照搬参考实现 ``postgres_data_source_connection.py:260-287``：
    首批只取 **1 行探针**，用它定后续批的大小；之后用**已见最宽行**（单调
    不减）反算，所以后面出现一条胖行会把批大小永久压回去。

    单独成类是为了可测：批大小变化不需要真的连库。
    """

    __slots__ = (
        "_budget",
        "_max_rows",
        "_growth",
        "_batch_size",
        "_max_row_bytes",
        "batches",
        "rows_seen",
        "probe_rows",
    )

    def __init__(
        self,
        *,
        budget_bytes: int = STREAM_FETCH_BUDGET_BYTES,
        max_batch_rows: int = STREAM_FETCH_MAX_BATCH_ROWS,
        growth_factor: int = STREAM_FETCH_GROWTH_FACTOR,
    ) -> None:
        if budget_bytes < 1 or max_batch_rows < 1 or growth_factor < 1:
            raise ReconqError(
                "HIS_POOL_CONFIG_INVALID",
                f"流式取数分批参数非法：budget_bytes={budget_bytes}、"
                f"max_batch_rows={max_batch_rows}、growth_factor={growth_factor}，"
                f"三者都必须 ≥ 1（0 或负数会让分批形同虚设，一次性把整期拉进内存）",
            )
        self._budget = budget_bytes
        self._max_rows = max_batch_rows
        self._growth = growth_factor
        #: 首批 1 行：先探一行，才知道一行到底多宽
        self._batch_size = 1
        self._max_row_bytes = 1
        #: 可观测性：批次数 / 已见行数 / 探针行数
        self.batches = 0
        self.rows_seen = 0
        self.probe_rows = 0

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def max_row_bytes(self) -> int:
        return self._max_row_bytes

    def observe(self, rows: Sequence[Any]) -> int:
        """看完一批，算下一批该取多少行；返回下一批的行数。"""
        self.batches += 1
        self.rows_seen += len(rows)
        if self.batches == 1:
            self.probe_rows = len(rows)
        if rows:
            self._max_row_bytes = max(
                self._max_row_bytes, max(estimate_row_bytes(r) for r in rows)
            )
        # 三个上界取最小：行数上限 / 预算÷最宽行 / 上一批×增长因子
        self._batch_size = max(
            1,
            min(
                self._max_rows,
                self._budget // self._max_row_bytes,
                self._batch_size * self._growth,
            ),
        )
        return self._batch_size


@dataclass
class StreamStats:
    """流式取数的可观测累加器（批次数 / 窗口数 / 已见最宽行）。"""

    batches: int = 0
    windows: int = 0
    widest_row_bytes: int = 0


@dataclass
class HisFetchResult:
    """一次 HIS 取数的可观测摘要（写进 run summary，Task 20 复用）。"""

    channel: str
    period: str
    row_count: int = 0
    total_amount: str = "0"
    batches: int = 0
    windows: int = 0
    widest_row_bytes: int = 0
    stream: str = "buffered"  # server_side | buffered
    dialect: str = ""
    #: 逐行取数期间的耗时（毫秒），由调用方注入时钟测
    elapsed_ms: int = 0
    rows: list[BillRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "period": self.period,
            "row_count": self.row_count,
            "total_amount": self.total_amount,
            "batches": self.batches,
            "windows": self.windows,
            "widest_row_bytes": self.widest_row_bytes,
            "stream": self.stream,
            "dialect": self.dialect,
            "elapsed_ms": self.elapsed_ms,
        }


def build_charge_query(
    window: PeriodWindow,
    *,
    columns: Sequence[str] = CHARGE_COLUMNS,
    table: str = "charge_record",
) -> str:
    """按账期时间窗构造收费流水的 SELECT。

    **只 SELECT ``columns`` 里的列**，不 ``SELECT *``。时间窗用半开区间
    ``[start, end)``，与 ``PeriodWindow`` 的语义一致：周一零点那笔属于
    新账期，不会被两期同时选中造成双记。
    """
    if not columns:
        raise ReconqError("FORMAT_BROKEN", "取数列清单为空，拒绝发出 SELECT * 等价查询")
    col_sql = ", ".join(columns)
    return (
        f"SELECT {col_sql} FROM {table} "
        f"WHERE charge_time >= :window_start AND charge_time < :window_end "
        f"ORDER BY charge_time"
    )


def _params(window: PeriodWindow) -> dict[str, datetime]:
    return {"window_start": window.start, "window_end": window.end}


def _open_engine(dsn: str, **engine_kwargs: Any) -> Engine:
    """按 DSN 建引擎；驱动缺失 / 网络不可达 / 认证失败分类成显式错误码。"""
    try:
        engine = create_engine(dsn, **engine_kwargs)
    except (NoSuchModuleError, ModuleNotFoundError) as exc:
        raise ReconqError(
            "HIS_DB_DRIVER_MISSING",
            f"DSN 所指方言的驱动没装：{exc}。PostgreSQL 装 psycopg、MySQL 装 pymysql",
            channel="his",
        ) from exc
    except DBAPIError as exc:
        raise _classify_db_error(exc) from exc
    return engine


def _classify_db_error(exc: Exception) -> ReconqError:
    """把驱动异常分类成 认证失败 / 库不存在 / 网络不可达 三类可读码。

    三类的判据**各自独立**、不挂在「是不是 OperationalError」这个总闸
    下面。挂上去会有一个具体的漏：``OperationalError`` 分支里若先匹配到
    了网络类词（``connection refused`` 之类），任何带 ``connection`` 字样
    的认证失败文案就可能被归进「网络不可达」；反过来，一句不含
    ``connect`` 的 ``password authentication failed`` 会被整个漏出去。
    这里改成**先按三类各自的特征词判定，判不出再兜底**。

    分不清的时候落到 ``HIS_DB_UNREACHABLE`` 并附上原始异常——**宁可说
    「不知道」也不猜一个具体成因**，排障时信息科至少知道去查网络还是查
    账号。
    """
    text_all = " ".join(
        str(part) for part in (getattr(exc, "orig", None), exc) if part is not None
    ).lower()
    if any(k in text_all for k in ("password", "authentication", "access denied", "login failed")):
        return ReconqError(
            "HIS_DB_AUTH_FAILED",
            f"HIS 只读库认证失败：{text_all.strip()[:300]}",
            channel="his",
        )
    if any(k in text_all for k in ("does not exist", "unknown database", "doesn't exist")):
        return ReconqError(
            "HIS_DB_NOT_EXIST",
            f"HIS 库不存在：{text_all.strip()[:300]}",
            channel="his",
        )
    if any(
        k in text_all
        for k in ("timeout", "timed out", "network", "unreachable", "refused", "connect")
    ):
        return ReconqError(
            "HIS_DB_UNREACHABLE",
            f"到 HIS 只读库的网络不可达：{text_all.strip()[:300]}",
            channel="his",
        )
    return ReconqError(
        "HIS_DB_UNREACHABLE",
        f"HIS 只读库连接失败（未能归入认证/库不存在/网络三类）：{text_all.strip()[:300]}",
        channel="his",
    )


def _supports_streaming(engine: Engine) -> bool:
    """该方言是否真支持服务端（命名）游标。

    与参考实现 ``supports_streaming_fetch()`` 同形：基类 ``False``，
    只有真能开命名游标的方言才 ``True``。MySQL 系走客户端游标——同样逐行
    fetch、同样按预算收批，只是没有「整批只在服务端」这层保证。
    """
    return engine.dialect.name in _SERVER_SIDE_STREAMING_DIALECTS


def _fetchmany(
    conn: Any,
    sql: str,
    params: dict[str, Any],
    sizer: AdaptiveBatchSizer,
    *,
    streaming: bool,
    stats: Optional["StreamStats"] = None,
    guard: Any = None,
    deadline: Optional[float] = None,
) -> Iterator[Sequence[Any]]:
    """执行查询并按自适应批大小逐行 yield。

    ``streaming=True`` 走服务端游标（PostgreSQL 系）；MySQL 系走客户端
    游标——同样逐行 fetch、同样按预算收批，退回是**显式**的
    （``HisFetchResult.stream`` 记 ``buffered``），不是静默降级。

    用 SQLAlchemy 的 ``execution_options(stream_results=True)`` 打开流式
    结果集：它对 PostgreSQL 下服务端（命名）游标、对 SQLite/MySQL 下
    分块取数，两条腿都保证**不把整个结果集物化**在客户端。

    只关 ``result``，不关连接——``execution_options()`` 返回的是**同一个**
    ``Connection``（不是副本），对它用 ``with`` 会把外层
    ``stream_charge_rows`` 的连接一起关掉，第二个时间窗就会报
    "This Connection is closed"。

    ``guard`` / ``deadline``（Task 12 起）让只读会话能复用同一条主路径：
    有 ``guard`` 就在发查询前过一遍只读守卫（拦写 + 拒 ``SELECT *``），
    有 ``deadline`` 就把整条查询放进带超时的执行里。不传时行为与 Task 6
    完全一致——调用方不感知这条新参数。
    """
    if guard is not None:
        guard.guard_sql(sql)
    if deadline is not None:
        yield from _fetchmany_with_deadline(conn, sql, params, sizer, stats, deadline)
        return
    streaming_conn = conn.execution_options(stream_results=True)
    result = streaming_conn.execute(text(sql), params)
    try:
        while True:
            rows = result.fetchmany(sizer.batch_size)
            if not rows:
                break
            sizer.observe(rows)
            if stats is not None:
                # 按**批**计，不按行计——批次数是「FETCH 往返次数」，
                # 按行计会退化成行数，这个指标就没意义了。
                stats.batches += 1
                stats.widest_row_bytes = max(
                    stats.widest_row_bytes, sizer.max_row_bytes
                )
            for row in rows:
                yield tuple(row)
    finally:
        result.close()


class _DropClosingCursorNoise(logging.Filter):
    """丢掉 ``invalidate()`` 之后迟到的 ``Error closing cursor``。

    超时是**设计内的处置结果**（这一路标未取到、其余五路照常），不是事故。
    而 ``invalidate()`` 之后被中断的那条查询仍可能跑完并去关自己的游标，
    驱动会为此在 ``sqlalchemy.pool`` 上打一条 ``Error closing cursor`` 加一
    屏栈。控制台正在看「HIS_QUERY_TIMEOUT：这一路没取到」时，后面跟着一屏
    与之矛盾的驱动栈，信息科会以为超时处理本身出了问题。

    为什么不能靠上下文临时压日志级别：这条记录是**异步**到达的——由超时
    之后仍在收尾的那个守护线程打出，可能落在 ``invalidate()`` 早已返回、
    上下文早就退出的之后。所以过滤必须常驻。

    匹配面刻意收窄到 ``closing cursor``：只压这一类记录，不压池的其他
    ERROR（比如真的连不上库），那些是真的要让人看见的。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 极端格式化异常
            return True
        return "closing cursor" not in message.lower()


def quiet_pool_log_noise(pool: Any) -> None:
    """把上面的过滤器装到**这个池自己的 logger 实例**上（幂等）。

    装在命名空间 logger ``sqlalchemy.pool`` 上**没用**：Python 的
    ``logging`` 里 filter 只在**记录产生的那个 logger** 上生效，向上传播
    只走 handler、不走父 logger 的 filter。而 ``Error closing cursor``
    是 SQLAlchemy 在 ``engine.pool.logger``（一个
    ``sqlalchemy.pool.impl.QueuePool`` 这样的**实例 logger**）上打的，
    父命名空间的 filter 拦不到它。所以必须拿到那个 logger 对象本身。
    """
    pool_logger = getattr(pool, "logger", None)
    if pool_logger is None:  # pragma: no cover - 非标准 Pool 实现
        return
    for existing in pool_logger.filters:
        if isinstance(existing, _DropClosingCursorNoise):
            return
    pool_logger.addFilter(_DropClosingCursorNoise())


def _mask_sql_noise(sql: str) -> str:
    """把注释与字符串字面量替换成等长空格，保留字符位置。

    用于「找语句边界」而不是「当语句用」：保持长度与下标不变，调用方能
    用掩码上的分号位置去切**原始**文本。这样 :func:`split_sql_statements`
    返回的仍是原样的语句，而不是被抹掉字面量的残骸。
    """
    def _blank(match: "re.Match[str]") -> str:
        return " " * len(match.group(0))

    masked = _BLOCK_COMMENT_MASK.sub(_blank, sql)
    masked = _LINE_COMMENT_MASK.sub(_blank, masked)
    masked = _STRING_MASK.sub(_blank, masked)
    masked = _DQ_STRING_MASK.sub(_blank, masked)
    return masked


def split_sql_statements(sql: str) -> list[str]:
    """把一段可能含多条语句的 SQL 拆成单条语句（**原样**）列表。

    先把注释（``--``、``/* */``）与字符串字面量（单引号/双引号，含
    ``''`` / ``""`` 转义）掩成等长空格，再按掩码上的 ``;`` 位置切
    **原始**文本。**不剥字符串就切会切错**——
    ``SELECT ';' FROM t`` 里那个分号不是语句边界。

    返回原样语句（不是掩码后的残骸）是为了让调用方拿到的证据行里 SQL
    与发出的一致。
    """
    masked = _mask_sql_noise(sql)
    out: list[str] = []
    start = 0
    for idx, ch in enumerate(masked):
        if ch == ";":
            chunk = sql[start:idx].strip()
            if chunk:
                out.append(chunk)
            start = idx + 1
    tail = sql[start:].strip()
    if tail:
        out.append(tail)
    return out


def run_with_deadline(
    fn: Callable[[], Any],
    deadline: float,
) -> tuple[bool, Any]:
    """在守护线程里跑 ``fn``，最多等 ``deadline`` 秒。

    返回 ``(finished, value_or_error)``。超时时 ``finished`` 为 ``False``，
    第二个元素是 ``None``。

    **为什么不用 ``concurrent.futures``**：它建的线程是**非守护**线程，
    而 CPython 在解释器退出时会把它们 join 起来。一次超时后，正在跑的
    慢查询未必真的停得下来（``interrupt()`` 能不能打断取决于驱动），
    于是「服务关了」会变成「服务等那条 HIS 慢查询跑完才关」——对账服务是
    常驻进程，这条路会直接变成一次假死。守护线程不会拖住进程退出。

    真正的防线仍然是**服务端**超时：PostgreSQL 的 ``statement_timeout``、
    MySQL 的 ``max_execution_time``（见 :func:`_apply_statement_timeout`）。
    客户端 deadline 只是保证「客户端不等了」，不是保证「数据库停了」。
    """
    import threading

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样带回调用方判定
            box["error"] = exc

    worker = threading.Thread(
        target=_target, name="reconq-his-query", daemon=True
    )
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        return False, None
    if "error" in box:
        return True, box["error"]
    return True, box.get("value")


def _fetchmany_with_deadline(
    conn: Any,
    sql: str,
    params: dict[str, Any],
    sizer: AdaptiveBatchSizer,
    stats: Optional["StreamStats"],
    deadline: float,
) -> Iterator[Sequence[Any]]:
    """带 deadline 的整窗取数：超时就中断并抛 HIS_QUERY_TIMEOUT。

    与上面的流式版不同，这里把**一个时间窗的整段取数**放进一个带超时的
    执行里。取舍是刻意的：一条无索引的 ``charge_time`` 范围查询不会在
    ``fetchmany`` 之间还你控制权——服务端还在算，你只是等着。所以超时的
    粒度落在「一个时间窗」上，超了整窗失败而不是整次扫描卡死。
    """
    finished, payload = run_with_deadline(
        lambda: _drain_window(conn, sql, params, sizer, stats), deadline
    )
    if finished:
        if isinstance(payload, BaseException):
            raise payload
        for row in payload:
            yield row
        return

    driver_conn = getattr(conn, "connection", None)
    driver_conn = getattr(driver_conn, "driver_connection", driver_conn)
    interrupt = getattr(driver_conn, "interrupt", None)
    if callable(interrupt):
        try:
            interrupt()
        except Exception as exc:  # pragma: no cover - 取决于驱动
            logger.warning("中断 HIS 查询失败（连接将被作废）：%s", exc)
    try:
        conn.invalidate()
    except Exception as exc:  # pragma: no cover - 取决于驱动
        logger.warning("作废 HIS 连接失败：%s", exc)
    raise ReconqError(
        "HIS_QUERY_TIMEOUT",
        f"HIS 只读查询超过 {deadline:g} 秒未返回，已中断并作废该连接："
        f"该渠道本期标未取到，其余五路照常。"
        f"若是账期跨度过大或缺索引，请信息科核对 charge_time 上是否有索引",
        channel="his",
    )


def _drain_window(
    conn: Any,
    sql: str,
    params: dict[str, Any],
    sizer: AdaptiveBatchSizer,
    stats: Optional["StreamStats"],
) -> list[Sequence[Any]]:
    """在带超时的执行里把一个时间窗的行收齐。

    这里 materialize 整窗是**有意的**：窗是按天切的（``window_days=1``），
    单窗行数就是单日收费笔数，量级可控；而整窗 materialize 换来的是
    「查询确实在 deadline 内结束了」这一个可断言的性质。
    """
    streaming_conn = conn.execution_options(stream_results=True)
    result = streaming_conn.execute(text(sql), params)
    rows: list[Sequence[Any]] = []
    try:
        while True:
            batch = result.fetchmany(sizer.batch_size)
            if not batch:
                break
            sizer.observe(batch)
            if stats is not None:
                stats.batches += 1
                stats.widest_row_bytes = max(stats.widest_row_bytes, sizer.max_row_bytes)
            rows.extend(tuple(row) for row in batch)
    finally:
        result.close()
    return rows



def _to_bill_row(
    row: Sequence[Any],
    columns: Sequence[str],
    *,
    window: PeriodWindow,
) -> BillRow:
    """一行元组 → 一行账单（键是契约列名，带源行号语义）。

    HIS 侧没有「源文件行号」，取数证据靠 ``charge_id`` + ``charge_time``
    定位；``row_no`` 这里用**账期内序号**占位，Task 21 的证据快照会以
    ``charge_id`` 为准。
    """
    values: dict[str, str] = {}
    for col, value in zip(columns, row):
        if value is None:
            values[col] = ""
        elif isinstance(value, bool):
            values[col] = "true" if value else "false"
        elif isinstance(value, datetime):
            values[col] = window.localize(value).isoformat()
        else:
            values[col] = str(value)
    return BillRow(
        values=values,
        row_no=0,
        raw_line=" | ".join(f"{k}={v}" for k, v in values.items()),
        source_file="his:charge_record",
    )


def stream_charge_rows(
    engine: Engine,
    period: str,
    *,
    table: str = "charge_record",
    columns: Sequence[str] = CHARGE_COLUMNS,
    window_days: int = 1,
    stats: Optional["StreamStats"] = None,
) -> Iterator[BillRow]:
    """按账期时间窗逐窗流式 yield 收费流水。

    ``window_days`` 决定一个账期切成几窗：默认按天切，让每窗的批次数与
    耗时可观测（一次扫描里 7 窗比 1 窗更容易定位慢在哪天）。窗口之间
    半开、不重叠，因此不存在重复计入。

    ``stats`` 可传入一个累加器收集批次数 / 最宽行 / 窗口数；不传则只逐行
    产出。**逐行产出本身就是主路径**——调用方可以边拉边对齐，不必等整个
    账期到齐。
    """
    from datetime import timedelta

    window = period_window(period)
    streaming = _supports_streaming(engine)
    if not streaming:
        logger.warning(
            "HIS 方言 %s 无服务端游标，退回客户端缓冲游标：峰值内存会升到整批结果集",
            engine.dialect.name,
        )

    edge = window.start
    with engine.connect() as conn:
        while edge < window.end:
            end = min(edge + timedelta(days=window_days), window.end)
            sub = PeriodWindow(period, edge, end, window.tz)
            sql = build_charge_query(sub, columns=columns, table=table)
            sizer = AdaptiveBatchSizer()
            try:
                for row in _fetchmany(
                    conn, sql, _params(sub), sizer, streaming=streaming, stats=stats
                ):
                    yield _to_bill_row(row, columns, window=window)
            except DBAPIError as exc:
                raise _classify_db_error(exc) from exc
            if stats is not None:
                stats.windows += 1
            edge = end


def fetch_his_charge_rows(
    period: str,
    *,
    dsn: Optional[str] = None,
    table: str = "charge_record",
    columns: Sequence[str] = CHARGE_COLUMNS,
    window_days: int = 1,
    engine: Optional[Engine] = None,
    now: Callable[[], float] = None,
) -> HisFetchResult:
    """取一个账期的 HIS 收费流水，返回可观测摘要。

    ``engine`` 可注入（测试用本地 SQLite 替身库），不传则按 ``HIS_DB_DSN``
    建连接。**逐行的主路径是 :func:`stream_charge_rows`**；本函数是给
    「一次拉完、拿摘要」的场景（如 ``data-source`` 预检、规模压测）用的
    便利封装，六路 2000+ 行时它的 ``rows`` 就是全部 2000 行。
    """
    import time

    clock = now or time.time
    start = clock()

    owned = engine is None
    if engine is None:
        raw_dsn = dsn or ""
        if raw_dsn == "":
            # 走 properties 的凭据装配，缺位时点名 HIS_DB_DSN
            props: Any = build_properties("his")
            raw_dsn = props.to_connection_kwargs().get("HIS_DB_DSN", "")
        if raw_dsn == "":
            raise ReconqError(
                "HIS_DB_DSN_MISSING",
                "HIS 只读库连接串为空：请设置环境变量 HIS_DB_DSN（只读账号）",
                channel="his",
            )
        engine = _open_engine(raw_dsn)

    result = HisFetchResult(
        channel="his",
        period=period,
        dialect=engine.dialect.name,
        stream="server_side" if _supports_streaming(engine) else "buffered",
    )
    stats = StreamStats()
    total_amount = None
    from decimal import Decimal

    try:
        for row in stream_charge_rows(
            engine,
            period,
            table=table,
            columns=columns,
            window_days=window_days,
            stats=stats,
        ):
            result.rows.append(row)
            result.row_count += 1
            raw_amount = row.get("amount")
            if raw_amount:
                try:
                    amount = Decimal(str(raw_amount))
                    total_amount = amount if total_amount is None else total_amount + amount
                except Exception:
                    pass
    except ReconqError:
        if owned:
            engine.dispose()
        raise
    else:
        if owned:
            engine.dispose()

    result.total_amount = str(total_amount or Decimal("0"))
    result.batches = stats.batches
    result.windows = stats.windows
    result.widest_row_bytes = stats.widest_row_bytes
    result.elapsed_ms = int((clock() - start) * 1000)
    return result


def probe_his_connection(
    *,
    dsn: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    table: str = "charge_record",
) -> dict[str, Any]:
    """``reconq data-source test his`` 的实现：只查**能否连通**。

    只做凭据位检查 + 一条 ``SELECT 1``，不拉任何业务行——``data-source
    test`` 问的是「这个源通不通」，不是「这个源有多少数据」。返回的
    ``credential_env_vars`` 是可读的原因码，不含凭据明文。
    """
    raw_dsn = dsn or ""
    if raw_dsn == "":
        raw_dsn = (env or {}).get("HIS_DB_DSN", "") or _env("HIS_DB_DSN")
    if raw_dsn == "":
        return {
            "channel": "his",
            "ok": False,
            "error_code": "HIS_DB_DSN_MISSING",
            "detail": "环境变量 HIS_DB_DSN 未设置；本产品只读 HIS，不写 HIS 账",
            "credential_env_vars": ["HIS_DB_DSN"],
            "connected": False,
        }

    try:
        engine = _open_engine(raw_dsn)
    except ReconqError as exc:
        # 驱动缺失 / DSN 不可解析也是「不通」，同样给可读答案而不是抛栈
        return {
            "channel": "his",
            "ok": False,
            "error_code": exc.code,
            "detail": exc.detail,
            "credential_env_vars": ["HIS_DB_DSN"],
            "connected": False,
        }
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1")
        return {
            "channel": "his",
            "ok": True,
            "error_code": None,
            "detail": f"HIS 只读库连通（{engine.dialect.name}，"
            f"{'服务端游标' if _supports_streaming(engine) else '客户端游标'}）",
            "credential_env_vars": ["HIS_DB_DSN"],
            "connected": True,
            "dialect": engine.dialect.name,
        }
    except DBAPIError as exc:
        err = _classify_db_error(exc)
        return {
            "channel": "his",
            "ok": False,
            "error_code": err.code,
            "detail": err.detail,
            "credential_env_vars": ["HIS_DB_DSN"],
            "connected": False,
        }
    finally:
        engine.dispose()


def _env(name: str) -> str:
    import os

    return os.environ.get(name, "")


#: 保留引用，避免 linter 判定为未使用（本模块把属性模型作为公开契约暴露）
_HIS_PROPERTIES_CLS = HisProperties
