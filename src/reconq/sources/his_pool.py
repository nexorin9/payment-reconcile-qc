"""HIS 只读连接保护：只读会话、连接池、查询超时、写操作拦截（Task 12）。

源产品对应实现
--------------
1. ``github_ref/soda-core/soda-core/src/soda_core/common/data_source_connection.py:275``
   ``open_connection()``：连接建立后由 ``_create_connection()`` 落到适配器；
   :290 ``close_connection()`` 关闭且**不抛异常**。本轮照搬「建连—取数—关连」
   的骨架，但把连接对象换成一个**只读投影**：本产品打的是院内生产库。
2. 同文件 :307 ``execute_query()`` 与 :372 ``execute_update()`` 是**两个分开
   的方法**——源产品能写，是因为它的目标是诊断仓库。本产品**只保留 query
   一半**：``HisReadOnlySession`` 根本没有 ``execute_update``；即使调用方
   用 ``execute()`` 递进写语句，也会在**发到数据库之前**被
   :func:`classify_sql` 拦下抛 ``READONLY_VIOLATION``。
3. ``github_ref/soda-core/soda-duckdb/src/soda_duckdb/common/data_sources/
   duckdb_data_source_connection.py:22`` 的 ``read_only: bool``：源产品最新
   的适配器已经把「只读」做成了**连接属性**而不是调用约定。本轮把这句
   翻成三种院内真实方言的会话级只读设置（见 :data:`READONLY_SETUP_SQL`），
   并在其之上再加一道**断言**：只读没真正生效就拒绝取数，而不是照样
   发 SELECT。

三条红线（对应 spec「不写 HIS 账」）
------------------------------------
* **只读**：会话建立即进入只读事务，并当场断言生效；任何 DML/DDL 在
  *执行前*被拦下。断言不通过 = 拒绝取数，不是「先查着看看」。
* **超时**：查询超过 ``HIS_DB_QUERY_TIMEOUT_SECONDS`` 显式失败
  ``HIS_QUERY_TIMEOUT``，该路标未取到，其余五路照常——**不挂死**整次扫描。
* **最小权限**：只 SELECT 收费流水所需列；``SELECT *`` 被显式拒绝
  （``HIS_SELECT_STAR_REJECTED``），生产库上少读一列就少一份 IO。

本模块**不引入新凭据位**：``HIS_DB_DSN`` 沿用 Task 6，新增的三个是纯
运维参数（池大小 / 取连接超时 / 查询超时），都有保守默认值，缺省不配也能
跑。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, Sequence

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, NoSuchModuleError
from sqlalchemy.pool import QueuePool, StaticPool

from ..errors import ReconqError
from .his_db import (
    CHARGE_COLUMNS,
    _classify_db_error,
    quiet_pool_log_noise,
    _mask_sql_noise,
    build_charge_query,
    run_with_deadline,
    split_sql_statements,
)
from .timezone import PeriodWindow, period_window

__all__ = [
    "DEFAULT_POOL_SIZE",
    "DEFAULT_POOL_TIMEOUT_SECONDS",
    "DEFAULT_QUERY_TIMEOUT_SECONDS",
    "READONLY_SETUP_SQL",
    "READONLY_PROBE_SQL",
    "HisPoolConfig",
    "split_sql_statements",
    "classify_sql",
    "assert_no_star",
    "ReadOnlyGuard",
    "HisReadOnlySession",
    "HisReadOnlyPool",
    "classify_connection_error",
    "build_his_pool",
]

logger = logging.getLogger("reconq.his_pool")

#: 保守默认：财务科内网机器一般同时只有一次扫描，取 5 条连接足够，
#: 又不至于把 HIS 的 ``max_connections`` 顶满（院内 HIS 常有别的系统在用）。
DEFAULT_POOL_SIZE = 5
#: 取连接等待上限。与池大小配套：池空且新连接建不起来时，最多等 10 秒。
DEFAULT_POOL_TIMEOUT_SECONDS = 10
#: 单条查询执行上限。一条跨三个月账期的无索引查询能把 HIS 拖到慢查询，
#: 30 秒是我们宁可判本期未取到、也不让生产库替我们背锅的取舍。
DEFAULT_QUERY_TIMEOUT_SECONDS = 30
#: 池满后的额外连接上限。默认 0 = **绝不**因为对账把 HIS 连接数顶穿。
DEFAULT_POOL_MAX_OVERFLOW = 0
#: 连接回收时间（秒）：0 = 不主动回收。院内网络稳定，默认不回收，
#: 避免扫描跑到一半换连接导致只读断言要重做一遍。
DEFAULT_POOL_RECYCLE_SECONDS = 0

#: 方言 → 建立只读会话要执行的语句。
#:
#: * PostgreSQL：``SET TRANSACTION READ ONLY`` 必须**在事务内**作为第一条
#:   语句执行；SQLAlchemy 2.0 的 autobegin 正好在首次 execute 时开事务，
#:   所以这句话落进去时事务已经开了。
#: * MySQL：``SET SESSION TRANSACTION READ ONLY``——MySQL 的
#:   ``SET TRANSACTION`` 只影响**下一个**事务，写 SESSION 才是整会话生效。
#: * SQLite（测试替身与院内部分历史库）：``PRAGMA query_only = 1``，
#:   它是 SQLite 上真正生效的只读开关（写操作直接报
#:   "attempt to write a readonly database"）。
READONLY_SETUP_SQL: dict[str, tuple[str, ...]] = {
    "postgresql": ("SET TRANSACTION READ ONLY",),
    "postgres": ("SET TRANSACTION READ ONLY",),
    "mysql": ("SET SESSION TRANSACTION READ ONLY",),
    "mariadb": ("SET SESSION TRANSACTION READ ONLY",),
    "sqlite": ("PRAGMA query_only = 1",),
}

#: 方言 → 「只读是否真生效」的探针语句。返回值由 :func:`_probe_value` 判真。
READONLY_PROBE_SQL: dict[str, str] = {
    "postgresql": "SHOW transaction_read_only",
    "postgres": "SHOW transaction_read_only",
    "mysql": "SELECT @@session.transaction_read_only",
    "mariadb": "SELECT @@session.transaction_read_only",
    "sqlite": "PRAGMA query_only",
}

#: 只读事务的探针失败 / 为假时的兜底方言：没登记就**拒绝取数**而不是放行。
_UNKNOWN_DIALECT_REASON = "未登记该方言的只读设置语句"

# —— SQL 语句分类 ——————————————————————————————————————

#: 允许出现的首个关键字。前三条是查数据，后三条是看元数据。
_ALLOWED_VERBS: frozenset[str] = frozenset(
    {"SELECT", "WITH", "SHOW", "EXPLAIN", "DESC", "DESCRIBE", "PRAGMA", "VALUES", "TABLE"}
)

#: ``SET`` 只放行两种：只读开关本身与事务隔离级别。其余 ``SET``（改
#: ``search_path``、改时区、改 sql_mode）一律算写操作——它改的是**会话
#: 状态**，而跨连接池复用时会把状态泄漏给下一个请求。
_ALLOWED_SET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^SET\s+(?:SESSION\s+)?TRANSACTION\s+READ\s+ONLY$", re.I),
    re.compile(r"^SET\s+TRANSACTION\s+ISOLATION\s+LEVEL\b", re.I),
)

#: DML / DDL 关键字，命中即写操作。用于**错误文案**（告诉人到底写了什么）
#: 以及 WITH（CTE）里的数据修改子句识别。
_WRITE_VERBS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "UPSERT",
        "REPLACE",
        "TRUNCATE",
        "DROP",
        "ALTER",
        "CREATE",
        "GRANT",
        "REVOKE",
        "COMMENT",
        "CALL",
        "EXEC",
        "EXECUTE",
        "COPY",
        "LOAD",
        "VACUUM",
        "ATTACH",
        "DETACH",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "LOCK",
        "ANALYZE",
        "SET",
    }
)

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_DQ_STRING_LITERAL_RE = re.compile(r'"(?:[^"]|"")*"')
_STAR_SELECT_RE = re.compile(r"\bSELECT\s+(?:DISTINCT\s+)?(?:\w+\s*\.\s*)?\*", re.I)


def _first_keyword(stmt: str) -> str:
    match = re.search(r"[A-Za-z_]+", stmt)
    return match.group(0).upper() if match else ""


def classify_sql(sql: str) -> tuple[str, str]:
    """判定一段 SQL 是不是只读。

    返回 ``(verdict, reason)``，``verdict`` 为 ``"read"`` 或 ``"write"``。
    **整段 SQL 里的每一条语句都要过判定**：PostgreSQL 的
    ``exec_driver_sql("UPDATE ...; SELECT ...")`` 会把两条都执行，只看
    第一条是最常见的绕过姿势。

    判定分三档：

    1. 首关键字在 :data:`_ALLOWED_VERBS` 里 → 只读。但 ``WITH`` 要再查一
       遍里面有没有 ``INSERT/UPDATE/DELETE ... RETURNING``（数据修改型
       CTE，PostgreSQL 15+ / SQLite 3.35+ 支持），有则算写。
    2. 首关键字是 ``SET`` 且整条匹配 :data:`_ALLOWED_SET_PATTERNS` → 只读
       （本产品自己开只读事务用的就是它）。
    3. 其余 → 写。空语句返回 ``("read", "空语句")``：不执行任何东西，
       不构成风险。
    """
    statements = split_sql_statements(sql)
    if not statements:
        return "read", "空语句"
    for stmt in statements:
        verb = _first_keyword(stmt)
        if verb == "":
            continue
        if verb in ("SET", "BEGIN", "START"):
            if any(pattern.match(stmt) for pattern in _ALLOWED_SET_PATTERNS):
                continue
            return "write", f"会话状态变更语句（{verb}）"
        if verb not in _ALLOWED_VERBS:
            return "write", f"数据修改或 DDL 语句（{verb}）"
        if verb == "WITH":
            inner = re.search(
                r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b", stmt, re.I
            )
            if inner is not None:
                return "write", f"数据修改型 CTE（{inner.group(0).upper()}）"
    return "read", "全部语句均为只读查询"


def assert_no_star(sql: str) -> None:
    """最小权限：显式拒绝 ``SELECT *`` / ``SELECT t.*``。

    源产品的 ``execute_query()`` 从不限制列（它要拿全表失败行）。本产品
    打的是 HIS 生产库，``SELECT *`` 会把 ``charge_record`` 里不在契约里的
    列也拖过来——多一份 IO，多一分撞慢查询的机会，还可能把不该进证据包
    的字段带进对齐器。列清单由 :data:`CHARGE_COLUMNS` 给定。

    判定跑在**掩码后的**文本上（注释与字符串字面量已替换成等长空格）：
    注释里写一句「SELECT * 是被禁的」不该把自己判成违规。
    """
    for stmt in split_sql_statements(_mask_sql_noise(sql)):
        match = _STAR_SELECT_RE.search(stmt)
        if match is not None:
            verb = match.group(0).strip()
            raise ReconqError(
                "HIS_SELECT_STAR_REJECTED",
                f"拒绝 SELECT *（命中 {verb}）：本产品只 SELECT 收费流水所需列，"
                f"请显式列出 {', '.join(CHARGE_COLUMNS)}",
                channel="his",
            )


def _probe_value(row: Optional[Sequence[Any]]) -> bool:
    """把探针返回值判成 bool。

    各方言给的东西不一样：PG 的 ``SHOW`` 给 ``'on'``，MySQL 的
    ``@@session.transaction_read_only`` 给 ``0/1``，SQLite 的
    ``PRAGMA query_only`` 给一行 ``(1,)``。认不出的形状一律**判否**——
    「读不出只读状态」和「只读没生效」都必须走拒绝取数，不能放行。
    """
    if row is None or len(row) == 0:
        return False
    value = row[0]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("on", "1", "true", "yes", "read only")
    return False


@dataclass(frozen=True)
class HisPoolConfig:
    """连接池与超时配置；三个值全部来自环境变量，都可缺省。

    值不是合法正数时报 ``HIS_POOL_CONFIG_INVALID`` 并点名变量——运维在
    ``.env`` 里写了 ``HIS_DB_POOL_SIZE=many`` 时，拿到的是一句
    「该变量必须是正整数」，而不是池初始化时的一句 ``ValueError``。
    """

    pool_size: int = DEFAULT_POOL_SIZE
    pool_timeout: int = DEFAULT_POOL_TIMEOUT_SECONDS
    query_timeout: float = float(DEFAULT_QUERY_TIMEOUT_SECONDS)
    max_overflow: int = DEFAULT_POOL_MAX_OVERFLOW
    pool_recycle: int = DEFAULT_POOL_RECYCLE_SECONDS
    statement_timeout_ms: Optional[int] = None

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "HisPoolConfig":
        src = env if env is not None else os.environ
        return cls(
            pool_size=_int_from_env(src, "HIS_DB_POOL_SIZE", DEFAULT_POOL_SIZE),
            pool_timeout=_int_from_env(
                src, "HIS_DB_POOL_TIMEOUT", DEFAULT_POOL_TIMEOUT_SECONDS
            ),
            query_timeout=_float_from_env(
                src, "HIS_DB_QUERY_TIMEOUT_SECONDS", DEFAULT_QUERY_TIMEOUT_SECONDS
            ),
            max_overflow=_int_from_env(
                src, "HIS_DB_POOL_MAX_OVERFLOW", DEFAULT_POOL_MAX_OVERFLOW
            ),
            pool_recycle=_int_from_env(
                src, "HIS_DB_POOL_RECYCLE", DEFAULT_POOL_RECYCLE_SECONDS
            ),
            statement_timeout_ms=_optional_int_from_env(
                src, "HIS_DB_STATEMENT_TIMEOUT_MS"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_size": self.pool_size,
            "pool_timeout": self.pool_timeout,
            "query_timeout": self.query_timeout,
            "max_overflow": self.max_overflow,
            "pool_recycle": self.pool_recycle,
            "statement_timeout_ms": self.statement_timeout_ms,
        }


def _raw(env: dict[str, str], name: str) -> str:
    value = env.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _int_from_env(env: dict[str, str], name: str, default: int) -> int:
    raw = _raw(env, name)
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReconqError(
            "HIS_POOL_CONFIG_INVALID",
            f"环境变量 {name} 必须是正整数，当前值 {raw!r} 不是数字",
            channel="his",
        ) from exc
    if value < 1:
        raise ReconqError(
            "HIS_POOL_CONFIG_INVALID",
            f"环境变量 {name} 必须 ≥ 1，当前值 {raw!r}",
            channel="his",
        )
    return value


def _float_from_env(env: dict[str, str], name: str, default: float) -> float:
    raw = _raw(env, name)
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ReconqError(
            "HIS_POOL_CONFIG_INVALID",
            f"环境变量 {name} 必须是正数，当前值 {raw!r} 不是数字",
            channel="his",
        ) from exc
    if value <= 0:
        raise ReconqError(
            "HIS_POOL_CONFIG_INVALID",
            f"环境变量 {name} 必须 > 0，当前值 {raw!r}",
            channel="his",
        )
    return value


def _optional_int_from_env(env: dict[str, str], name: str) -> Optional[int]:
    raw = _raw(env, name)
    if raw == "":
        return None
    value = _int_from_env(env, name, 0)
    return value if value > 0 else None


class ReadOnlyGuard:
    """只读守卫：开只读 → 断言生效 → 拦写。

    三件事的顺序**不能换**。先断言再生效就等于没断言；先放行再拦写，
    拦的也只是本产品自己写的代码，调用方拿裸连接就绕过去了——所以
    :class:`HisReadOnlyPool` 只暴露 :class:`HisReadOnlySession`，不暴露
    裸 ``Connection``。
    """

    __slots__ = ("_dialect",)

    def __init__(self, dialect: str) -> None:
        self._dialect = dialect

    @property
    def dialect(self) -> str:
        return self._dialect

    def setup(self, conn: Connection) -> None:
        """建立只读会话。方言未登记时**直接拒绝**。

        不做「登记了就用、没登记就跳过」——跳过意味着这条连接根本没进
        只读事务，而后面的断言在多数方言上也查不出来，结果就是默默以读写
        身份连上生产库。
        """
        statements = READONLY_SETUP_SQL.get(self._dialect)
        if statements is None:
            raise ReconqError(
                "HIS_READONLY_UNSUPPORTED",
                f"{_UNKNOWN_DIALECT_REASON}（方言 {self._dialect}）："
                f"请由信息科确认该库的只读接入方式后，在 "
                f"READONLY_SETUP_SQL 中补上该方言的只读设置语句",
                channel="his",
            )
        for statement in statements:
            # 走 exec_driver_sql：这一步是**开**只读，不能被本产品自己的
            # 只读守卫拦下（守卫拦的是读写混合的业务语句）。
            conn.exec_driver_sql(statement)

    def assert_active(self, conn: Connection) -> None:
        """当场断言只读真生效；不生效即拒绝取数。"""
        probe = READONLY_PROBE_SQL.get(self._dialect)
        if probe is None:
            raise ReconqError(
                "HIS_READONLY_UNSUPPORTED",
                f"{_UNKNOWN_DIALECT_REASON}（方言 {self._dialect}）："
                f"缺少只读状态探针，无法断言本产品确实没拿到写权限",
                channel="his",
            )
        try:
            row = conn.exec_driver_sql(probe).fetchone()
        except DBAPIError as exc:
            raise ReconqError(
                "HIS_READONLY_ASSERT_FAILED",
                f"只读状态探针执行失败：{exc}（{_UNKNOWN_DIALECT_REASON}）",
                channel="his",
            ) from exc
        if not _probe_value(row):
            raise ReconqError(
                "HIS_READONLY_ASSERT_FAILED",
                f"连接建立后未进入只读事务（探针 {probe} 返回 {row!r}）："
                f"本产品只读 HIS，拒绝对该连接发任何业务查询。"
                f"请确认 HIS_DB_DSN 使用的账号确实只有只读权限，"
                f"或补上该方言的只读设置语句",
                channel="his",
            )

    def guard_sql(self, sql: str) -> str:
        """业务 SQL 进门前的检查：拦写 + 拒 ``SELECT *``。"""
        verdict, reason = classify_sql(sql)
        if verdict == "write":
            verb = _first_keyword(sql)
            raise ReconqError(
                "READONLY_VIOLATION",
                f"拦截到写操作（{reason}，首关键字 {verb}）："
                f"本产品永不写 HIS 账，这条语句不会发到数据库",
                channel="his",
            )
        assert_no_star(sql)
        return sql


def _apply_statement_timeout(engine: Engine, dialect: str, timeout_ms: Optional[int]) -> None:
    """在**建连那一刻**给 PostgreSQL / MySQL 装上服务端语句超时。

    客户端超时只能事后中断（见 :class:`HisReadOnlySession`），数据库那边的
    查询其实还在跑。``statement_timeout`` / ``max_execution_time`` 让服务
    端自己收手——院内 HIS 上这是唯一不会「拖慢别人」的做法。
    """
    if not timeout_ms:
        return
    if dialect in ("postgresql", "postgres"):
        statement = f"SET statement_timeout = {int(timeout_ms)}"
    elif dialect in ("mysql", "mariadb"):
        statement = f"SET SESSION max_execution_time = {int(timeout_ms)}"
    else:
        # SQLite 没有服务端超时；客户端 deadline 已经够用，不静默假装设上了
        logger.info("方言 %s 无服务端语句超时设置，跳过（客户端 deadline 仍生效）", dialect)
        return

    @event.listens_for(engine, "connect")
    def _set_timeout(dbapi_conn, _record):  # pragma: no cover - 依赖真实驱动
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(statement)
        finally:
            cursor.close()


def _open_engine(dsn: str, config: HisPoolConfig) -> Engine:
    """按 DSN + 池配置建引擎；驱动缺失 / 连不上分类成显式错误码。"""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    lowered = dsn.lower()
    in_memory = ":memory:" in lowered or "mode=memory" in lowered
    kwargs: dict[str, Any] = {
        "pool_size": config.pool_size,
        "pool_timeout": config.pool_timeout,
    }
    if in_memory:
        # QueuePool 配 sqlite:// 时每条连接都是一个**独立的空库**，
        # 第二条连接看不到第一条建的表。测试替身用 StaticPool 固定单连接；
        # 真实 HIS DSN 不走这条分支。
        kwargs["poolclass"] = StaticPool
    else:
        kwargs["poolclass"] = QueuePool
        kwargs["max_overflow"] = config.max_overflow
        if config.pool_recycle:
            kwargs["pool_recycle"] = config.pool_recycle
    if in_memory and "pool_size" in kwargs and kwargs.get("poolclass") is StaticPool:
        kwargs.pop("pool_size")
        kwargs.pop("pool_timeout")

    try:
        engine = create_engine(dsn, **kwargs)
    except (NoSuchModuleError, ModuleNotFoundError) as exc:
        raise ReconqError(
            "HIS_DB_DRIVER_MISSING",
            f"DSN 所指方言的驱动没装：{exc}。PostgreSQL 装 psycopg、MySQL 装 pymysql",
            channel="his",
        ) from exc
    _apply_statement_timeout(engine, engine.dialect.name, config.statement_timeout_ms)
    quiet_pool_log_noise(engine.pool)
    return engine


#: 连接失败分类（认证 / 库不存在 / 网络不可达）——**直接别名**到 Task 6 的
#: 判据，不是包装函数。包装一层会留下一条「包装版与被包装版判据将来漂移」
#: 的缝，而「扫描时报 A、data-source test 报 B」是最难排的一类问题。
classify_connection_error = _classify_db_error


class HisReadOnlySession:
    """一条只读会话：只暴露 query，没有任何写方法。

    刻意**没有** ``execute_update`` / ``commit``：不是靠「调用方记得不写」，
    是靠这个类压根不提供写入口。真有写语句递进来，:meth:`execute` 也会在
    发到数据库之前抛 ``READONLY_VIOLATION``。
    """

    __slots__ = ("_conn", "_guard", "_config", "_closed")

    def __init__(
        self,
        conn: Connection,
        guard: ReadOnlyGuard,
        config: HisPoolConfig,
    ) -> None:
        self._conn = conn
        self._guard = guard
        self._config = config
        self._closed = False

    # —— 生命周期 ————————————————————————————————————————

    def close(self) -> None:
        """关连接，**不抛异常**。

        与源实现 ``close_connection()`` 同形：关连接的失败不该盖掉取数
        过程中真正需要被看见的那个异常。
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except Exception as exc:  # pragma: no cover - 依赖驱动行为
            logger.warning("关闭 HIS 只读连接失败（已忽略）：%s", exc)

    def __enter__(self) -> "HisReadOnlySession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # —— 查询 ————————————————————————————————————————

    def execute(
        self,
        sql: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> list[tuple]:
        """执行一条只读查询，返回全部行。

        带 deadline：超过 ``HIS_DB_QUERY_TIMEOUT_SECONDS`` 就中断并抛
        ``HIS_QUERY_TIMEOUT``，**不让一条慢查询把整次扫描挂死**。中断
        之后连接作废（``invalidate``）——被中断的连接状态不可信，让它回到
        池里给下一个请求用，等于把「这次超时」传染成「下次莫名其妙也错」。
        """
        self._ensure_open()
        self._guard.guard_sql(sql)
        deadline = self._config.query_timeout if timeout is None else timeout
        rows = self._run_with_deadline(sql, params, deadline)
        return list(rows)

    def first(self, sql: str, params: Optional[dict[str, Any]] = None) -> Optional[tuple]:
        """只取第一行（探针类查询用）。"""
        rows = self.execute(sql, params)
        return rows[0] if rows else None

    def _run_with_deadline(
        self,
        sql: str,
        params: Optional[dict[str, Any]],
        deadline: float,
    ) -> Sequence[Sequence[Any]]:
        finished, payload = run_with_deadline(
            lambda: self._do_execute(sql, params), deadline
        )
        if not finished:
            self._invalidate_running_connection()
            raise ReconqError(
                "HIS_QUERY_TIMEOUT",
                f"HIS 只读查询超过 {deadline:g} 秒未返回，已中断并作废该连接："
                f"该渠道本期标未取到，其余五路照常。"
                f"若是账期跨度过大或缺索引，请信息科核对 charge_time 上是否有索引",
                channel="his",
            )
        if isinstance(payload, BaseException):
            if isinstance(payload, ReconqError):
                raise payload
            if isinstance(payload, DBAPIError):
                raise classify_connection_error(payload) from payload
            raise payload
        return payload

    def _do_execute(
        self, sql: str, params: Optional[dict[str, Any]]
    ) -> Sequence[Sequence[Any]]:
        result = self._conn.execute(text(sql), params or {})
        try:
            return result.fetchall()
        finally:
            result.close()

    def _invalidate_running_connection(self) -> None:
        """中断正在跑的查询，并把这���连接踢出池。"""
        driver_conn = getattr(self._conn, "connection", None)
        driver_conn = getattr(driver_conn, "driver_connection", driver_conn)
        interrupt = getattr(driver_conn, "interrupt", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception as exc:  # pragma: no cover - 取决于驱动
                logger.warning("中断 HIS 查询失败（连接将被作废）：%s", exc)
        try:
            self._conn.invalidate()
        except Exception as exc:  # pragma: no cover - 取决于驱动
            logger.warning("作废 HIS 连接失败：%s", exc)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ReconqError(
                "HIS_SESSION_CLOSED",
                "只读会话已关闭；本产品按需建连、逐账期用完即关，"
                "不在关闭后的会话上继续取数",
                channel="his",
            )

    # —— 主路径：按账期逐行流式取收费流水 ————————————————————

    def stream_charge_rows(
        self,
        period: str,
        *,
        table: str = "charge_record",
        columns: Sequence[str] = CHARGE_COLUMNS,
        window_days: int = 1,
        stats: Any = None,
        timeout: Optional[float] = None,
    ) -> Iterator[Any]:
        """按账期时间窗逐行 yield 收费流水（复用 Task 6 的流式主路径）。

        主路径**与 :func:`reconq.sources.his_db.stream_charge_rows` 完全
        相同**：账期按天切窗 → 每窗一条带时间窗的 SELECT → 8MB 字节预算的
        自适应分批 → 逐行 yield。区别只有两处：

        1. 跑在**已断言只读**的这条连接上，而不是裸 ``engine.connect()``；
        2. 每批 fetch 各自带一个 deadline。整段取数共用一个 deadline 会让
           「7 个时间窗里第 6 窗慢」把前 5 窗已取到的行一起作废——按批算
           才是「哪一批慢就哪一批超时」。
        """
        from datetime import timedelta

        from .his_db import AdaptiveBatchSizer, StreamStats, _fetchmany, _to_bill_row

        self._ensure_open()
        window = period_window(period)
        deadline = self._config.query_timeout if timeout is None else timeout
        edge = window.start
        while edge < window.end:
            end = min(edge + timedelta(days=window_days), window.end)
            sub = PeriodWindow(period, edge, end, window.tz)
            sql = build_charge_query(sub, columns=columns, table=table)
            self._guard.guard_sql(sql)
            sizer = AdaptiveBatchSizer()
            window_stats = stats if stats is not None else StreamStats()
            try:
                for row in _fetchmany(
                    self._conn,
                    sql,
                    _window_params(sub),
                    sizer,
                    streaming=False,
                    stats=window_stats,
                    guard=self._guard,
                    deadline=deadline,
                ):
                    yield _to_bill_row(row, columns, window=window)
            except ReconqError:
                raise
            except DBAPIError as exc:
                raise classify_connection_error(exc) from exc
            window_stats.windows += 1
            edge = end


def _window_params(window: PeriodWindow) -> dict[str, Any]:
    return {"window_start": window.start, "window_end": window.end}


class HisReadOnlyPool:
    """HIS 只读连接池。

    只暴露 :meth:`session`（拿一条只读会话）；**不暴露** 裸 ``Connection``
    与 ``Engine`` 的 ``connect()``。这不是洁癖：只读这件事如果能被绕过去
    一行代码拿到裸连接去，它就不再是红线而是一句口号。
    """

    def __init__(
        self,
        dsn: str,
        *,
        config: Optional[HisPoolConfig] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        if dsn.strip() == "":
            raise ReconqError(
                "HIS_DB_DSN_MISSING",
                "HIS 只读库连接串为空：请设置环境变量 HIS_DB_DSN（只读账号）",
                channel="his",
            )
        self._dsn = dsn
        self._config = config or HisPoolConfig.from_env(env)
        self._engine = _open_engine(dsn, self._config)
        self._guard = ReadOnlyGuard(self._engine.dialect.name)

    # —— 元信息 ————————————————————————————————————————

    @property
    def config(self) -> HisPoolConfig:
        return self._config

    @property
    def dialect(self) -> str:
        return self._engine.dialect.name

    def pool_status(self) -> dict[str, Any]:
        """池的实时状态，写进 run summary 供面板显示。

        QueuePool 的 ``size()`` / ``checkedout()`` 是**方法**不是属性，
        直接 ``getattr`` 会把一个 bound method 塞进 JSON 摘要里，面板拿到
        的是 ``<bound method ...>`` 这种没法看的东西。这里统一调用。
        """
        pool = self._engine.pool
        status: dict[str, Any] = {
            "dialect": self.dialect,
            "pool_class": type(pool).__name__,
        }
        for attr in ("size", "checkedin", "checkedout", "overflow"):
            value = getattr(pool, attr, None)
            if value is None:
                continue
            status[attr] = value() if callable(value) else value
        return status

    # —— 会话 ————————————————————————————————————————

    def session(self) -> HisReadOnlySession:
        """取一条只读会话：建连 → 开只读 → 断言生效 → 才交出去。"""
        try:
            conn = self._engine.connect()
        except DBAPIError as exc:
            raise classify_connection_error(exc) from exc
        session = HisReadOnlySession(conn, self._guard, self._config)
        try:
            self._guard.setup(conn)
            self._guard.assert_active(conn)
        except ReconqError:
            session.close()
            raise
        return session

    def dispose(self) -> None:
        self._engine.dispose()

    def __enter__(self) -> "HisReadOnlyPool":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.dispose()


def build_his_pool(
    *,
    dsn: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> HisReadOnlyPool:
    """按 ``HIS_DB_DSN`` + 池环境变量建池。"""
    src = env if env is not None else os.environ
    raw = dsn or src.get("HIS_DB_DSN", "") or ""
    if raw.strip() == "":
        raise ReconqError(
            "HIS_DB_DSN_MISSING",
            "HIS 只读库连接串为空：请设置环境变量 HIS_DB_DSN（只读账号）",
            channel="his",
        )
    return HisReadOnlyPool(raw, env=src)
