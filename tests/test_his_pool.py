"""HIS 只读连接保护测试（Task 12）。

对照参考实现的三处语义：

* ``data_source_connection.py:275`` ``open_connection()`` / :290
  ``close_connection()``——建连—取数—关连的骨架，关连失败不抛异常；
* 同文件 :307 ``execute_query()`` 与 :372 ``execute_update()`` 是**两个
  分开的方法**。本产品的只读会话**只保留前一半**，所以「写操作被拦」不是
  一条纪律，而是类里根本没有写入口 + 递进来的写语句仍被 ``classify_sql``
  在发库之前拦下；
* ``duckdb_data_source_connection.py:22`` 的 ``read_only: bool``——源产品
  已把「只读」做成**连接属性**。本轮把同一句翻成三种方言的会话级设置
  （PG ``SET TRANSACTION READ ONLY`` / MySQL ``SET SESSION ...`` /
  SQLite ``PRAGMA query_only``），并在其上**加一道断言**。

五类断言（任务规定的验收口径）：

1. **只读事务断言生效** —— 会话建立后探针回读为真；且**绕过守卫直接拿
   裸连接写**，数据库层自己也会拒绝（守卫不是唯一防线）。
2. **写语句被拦截** —— DML / DDL / 数据修改型 CTE / 混在 SELECT 后面的
   写 / ``SET search_path``，一条都发不出去。
3. **查询超时抛 HIS_QUERY_TIMEOUT** —— 用一条真的跑不完的递归 CTE，
   不靠 mock。
4. **认证失败错误码正确** —— 分类判据与 Task 6 同源，五个用例逐个钉。
5. **仅 SELECT 指定列** —— 守卫拒 ``SELECT *``；账期取数只发 CHARGE_COLUMNS
   那八列，可用 ``before_cursor_execute`` 抓到真实 SQL 核对。

**本测试不连任何生产 HIS 库**：用本地 SQLite 替身（脱敏样例，金额/单号
全是占位），并用「未知方言」与「只读探针失败」两条路径覆盖真实驱动下
才会出现的分支。
"""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text

from reconq.errors import ReconqError, error_meta
from reconq.sources.his_db import CHARGE_COLUMNS
from reconq.sources.his_pool import (
    DEFAULT_POOL_SIZE,
    DEFAULT_POOL_TIMEOUT_SECONDS,
    DEFAULT_QUERY_TIMEOUT_SECONDS,
    READONLY_PROBE_SQL,
    READONLY_SETUP_SQL,
    HisPoolConfig,
    HisReadOnlyPool,
    ReadOnlyGuard,
    assert_no_star,
    build_his_pool,
    classify_connection_error,
    classify_sql,
    split_sql_statements,
)
from reconq.sources.his_db import _classify_db_error

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "his", "charge_record_2026-W36.csv"
)
TABLE = "charge_record"
PERIOD = "2026-W36"


# —— 替身库 ————————————————————————————————————————————


def _make_his_db(path: str, rows: int | None = None) -> str:
    """把脱敏样例 CSV 灌进一个**磁盘上的** SQLite 替身库，返回 DSN。

    用磁盘文件而不是 ``sqlite://``（内存库）：内存库配 QueuePool 时每条
    连接都是一个独立的空库，测不出连接池行为。文件库让多条连接看到同一
    份数据，才能验「连接回来后事务状态是干净的」。

    样例里金额、单号全��占位与类目，**不含患者可识别明细**。
    """
    with open(FIXTURE, newline="", encoding="utf-8") as fh:
        records = list(csv.DictReader(fh))
    if rows is not None:
        base = list(records)
        while len(records) < rows:
            for rec in base:
                if len(records) >= rows:
                    break
                clone = dict(rec)
                clone["charge_id"] = f"CHG-BULK-{len(records):05d}"
                clone["his_order_no"] = f"HIS-<CHARGE-BULK-{len(records):05d}>"
                records.append(clone)

    if os.path.exists(path):
        os.remove(path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        cols = ", ".join(f"{c} TEXT" for c in CHARGE_COLUMNS)
        conn.execute(text(f"CREATE TABLE {TABLE} ({cols})"))
        for rec in records:
            placeholders = ", ".join(f":{c}" for c in CHARGE_COLUMNS)
            conn.execute(
                text(f"INSERT INTO {TABLE} VALUES ({placeholders})"),
                {c: rec[c] for c in CHARGE_COLUMNS},
            )
    engine.dispose()
    return f"sqlite:///{path}"


@pytest.fixture()
def his_dsn(tmp_path) -> str:
    return _make_his_db(str(tmp_path / "his_charge.db"))


@pytest.fixture()
def pool(his_dsn):
    p = build_his_pool(dsn=his_dsn, env={})
    yield p
    p.dispose()


# —— ① 只读事务断言生效 ————————————————————————————————


def test_readonly_setup_and_probe_registered_for_all_three_dialects():
    """三种院内真实方言都要有「设置 + 探针」两条语句，缺一条就不敢连。"""
    for dialect in ("postgresql", "mysql", "sqlite"):
        assert READONLY_SETUP_SQL.get(dialect), f"{dialect} 缺只读设置语句"
        assert READONLY_PROBE_SQL.get(dialect), f"{dialect} 缺只读探针"
    # PG 与 MySQL 的只读开关**不是同一句**：MySQL 的 SET TRANSACTION 只影响
    # 下一个事务，写 SESSION 才是整会话生效。写成一样就等于 MySQL 上没设。
    assert READONLY_SETUP_SQL["postgresql"] == ("SET TRANSACTION READ ONLY",)
    assert READONLY_SETUP_SQL["mysql"] == ("SET SESSION TRANSACTION READ ONLY",)


def test_readonly_assertion_passes_on_real_session(pool):
    """正常路径：会话建立后只读断言当场通过。"""
    with pool.session() as session:
        assert session.first("PRAGMA query_only")[0] == 1


def test_readonly_assertion_failure_refuses_fetch():
    """断言不过 = **拒绝取数**，不是「先查着看看」。

    用一个探针恒为假的方言（这里造一个 ``probe_always_off``）模拟
    「只读设置没真生效」——真实场景是院方给了读写账号，SET 静默失败。
    """
    guard = ReadOnlyGuard("sqlite")
    conn = _fake_conn([(0,)])
    with pytest.raises(ReconqError) as excinfo:
        guard.assert_active(conn)
    assert excinfo.value.code == "HIS_READONLY_ASSERT_FAILED"
    # 拒绝的理由必须能让人自己判断下一步查什么
    assert "HIS_DB_DSN" in excinfo.value.detail
    assert "只读" in excinfo.value.detail


def test_unknown_dialect_refuses_to_connect_at_all():
    """未登记方言 → 拒绝建连。

    「登记了就用、没登记就跳过」= 这条连接根本没进只读事务，而多数方言
    上后面的断言也查不出来，结果就是默默以读写身份连上生产库。
    """
    guard = ReadOnlyGuard("oracle")
    with pytest.raises(ReconqError) as excinfo:
        guard.setup(_fake_conn([(1,)]))
    assert excinfo.value.code == "HIS_READONLY_UNSUPPORTED"
    assert "oracle" in excinfo.value.detail


def test_unknown_dialect_also_refuses_assertion():
    guard = ReadOnlyGuard("oracle")
    with pytest.raises(ReconqError) as excinfo:
        guard.assert_active(_fake_conn([(1,)]))
    assert excinfo.value.code == "HIS_READONLY_UNSUPPORTED"


@pytest.mark.parametrize(
    "value,expected",
    [
        ((1,), True),
        ((0,), False),
        (("on",), True),
        (("off",), False),
        (("1",), True),
        ((True,), True),
        ((False,), False),
        ((), False),          # 探针没返回行
        (("readable",), False),  # 认不出的形状 → 判否（宁可拒绝取数）
    ],
)
def test_probe_value_treats_unknown_shape_as_not_readonly(value, expected):
    """探针返回认不出的形状时判**否**。

    「读不出只读状态」和「只读没生效」都必须走拒绝取数。放行 = 拿一个
    说不清状态的连接去查生产库。
    """
    from reconq.sources.his_pool import _probe_value

    assert _probe_value(value) is expected


def test_database_layer_also_refuses_write_when_guard_bypassed(his_dsn, pool):
    """**守卫不是唯一防线**：绕过守卫拿裸连接写，数据库自己也要拒。

    这一条是整套只读保护里最硬的保证。守卫挡的是「本产品自己的代码」，
    而库层的只读会话/只读账号挡的是「任何代码」。
    """
    with pool.session() as session:
        raw = session._conn  # 刻意绕过守卫
        with pytest.raises(Exception) as excinfo:
            raw.exec_driver_sql(
                f"INSERT INTO {TABLE} VALUES "
                "('HIS-X','CHG-X','1.00','2026-09-01 10:00:00','2026-W36','门诊收费室','微信','false')"
            )
        assert "readonly" in str(excinfo.value).lower()
        # 数据确实没变
        assert session.first(f"SELECT COUNT(*) FROM {TABLE}")[0] == 9


# —— ② 写语句被拦截 ——————————————————————————————————


def test_session_has_no_write_entrypoint(pool):
    """只读会话**根本没有** ``execute_update`` / ``commit``。

    靠「调用方记得不写」是纪律；类里没有写入口才是结构保证。
    """
    with pool.session() as session:
        for forbidden in ("execute_update", "commit", "insert", "update", "delete"):
            assert not hasattr(session, forbidden), f"只读会话不该有 {forbidden}"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE charge_record SET amount = '0'",
        "DELETE FROM charge_record",
        "INSERT INTO charge_record (amount) VALUES ('1')",
        "CREATE TABLE evil (a int)",
        "DROP TABLE charge_record",
        "ALTER TABLE charge_record ADD COLUMN x int",
        "TRUNCATE TABLE charge_record",
        "MERGE INTO charge_record USING charge_record ON 1=1",
        "GRANT ALL ON charge_record TO public",
        "CALL some_proc()",
        "VACUUM",
        "LOCK TABLE charge_record",
        "SET search_path TO evil",
        "SET TIME ZONE 'UTC'",
        # 只读排在写前面也一样要拦——这是最常见的绕过姿势
        "SELECT 1 FROM charge_record; UPDATE charge_record SET amount='0'",
        # 注释里藏写语句
        "SELECT 1 -- harmless\n; DELETE FROM charge_record",
        # 数据修改型 CTE
        "WITH moved AS (SELECT * FROM charge_record) DELETE FROM charge_record",
        "WITH x AS (SELECT 1) INSERT INTO charge_record (amount) SELECT '1'",
    ],
)
def test_write_statements_are_blocked_before_sending(pool, sql):
    """写/DDL/会话状态变更，一条都发不出去。"""
    with pool.session() as session:
        with pytest.raises(ReconqError) as excinfo:
            session.execute(sql)
        assert excinfo.value.code == "READONLY_VIOLATION", sql
        # 数据没被动过
        assert session.first(f"SELECT COUNT(*) FROM {TABLE}")[0] == 9


def test_blocked_statement_never_reaches_the_database(his_dsn):
    """拦截发生在**发库之前**：用 before_cursor_execute 抓真实 SQL。

    只断言抛了异常不够——万一是数据库拒绝的，异常码就不是
    READONLY_VIOLATION 了；这里显式抓「真正发出去的语句」来钉死。
    """
    pool = build_his_pool(dsn=his_dsn, env={})
    sent: list[str] = []

    @event.listens_for(pool._engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _params, _ctx, _many):
        sent.append(statement)

    try:
        with pool.session() as session:
            before = len(sent)
            with pytest.raises(ReconqError):
                session.execute("UPDATE charge_record SET amount = '0'")
            after_guard = list(sent[before:])
            before = len(sent)
            with pytest.raises(ReconqError):
                session.execute("DELETE FROM charge_record")
            after_guard += list(sent[before:])
    finally:
        pool.dispose()
    assert after_guard == [], f"写语句被发出去了：{after_guard}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT amount FROM charge_record",
        "SELECT DISTINCT department FROM charge_record",
        "  select amount from charge_record  ",
        "PRAGMA table_info(charge_record)",
        "WITH x AS (SELECT amount FROM charge_record) SELECT COUNT(*) FROM x",
        "SELECT ';' AS semi FROM charge_record",
        "SELECT amount FROM charge_record -- 尾注释里带 ; 和 UPDATE",
        "SELECT /* 块注释 ; DROP TABLE x */ amount FROM charge_record",
    ],
)
def test_read_statements_pass_the_guard(pool, sql):
    with pool.session() as session:
        session.execute(sql)  # 不抛即通过


@pytest.mark.parametrize(
    "sql",
    [
        # 这几条语法正确但 **SQLite 解析不了**（那是 PG / MySQL 侧语句），
        # 所以只验守卫判定放行，不真发到库。真实发库路径由
        # test_period_fetch_sends_only_the_eight_charge_columns 覆盖。
        "SELECT 1 FROM charge_record; SELECT 2 FROM charge_record",
        "SHOW transaction_read_only",
        "EXPLAIN SELECT 1",
        "SET SESSION TRANSACTION READ ONLY",
        "SET TRANSACTION READ ONLY",
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
    ],
)
def test_guard_allows_non_sqlite_dialect_read_statements(pool, sql):
    """只读放行判定跨方言一致，不因测试替身是 SQLite 就缩窄。"""
    assert classify_sql(sql)[0] == "read", sql
    # 守卫本身不抛（真正执行会因 SQLite 语法而失败，那不是守卫的责任）
    pool._guard.guard_sql(sql)


def test_classify_sql_never_treats_write_as_read():
    """classify_sql 本身的判定矩阵（不依赖库）。"""
    assert classify_sql("UPDATE t SET a=1")[0] == "write"
    assert classify_sql("SELECT a FROM t")[0] == "read"
    # 字符串字面量里的分号不是语句边界
    assert split_sql_statements("SELECT ';' FROM t") == ["SELECT ';' FROM t"]
    assert split_sql_statements("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]
    assert classify_sql("")[0] == "read"  # 空语句不执行任何东西
    assert classify_sql("   \n  ")[0] == "read"


# —— ③ 查询超时 ——————————————————————————————————————


def test_slow_query_times_out_with_named_error(his_dsn):
    """真的跑不完的查询 → HIS_QUERY_TIMEOUT（不 mock，用递归 CTE）。

    走的是产品真路径：``session.execute`` → deadline → 中断 → 作废连接
    → 抛可读错误码。断言错误文案里带上了「其余五路照常」，因为
    「超时」在这个产品里的处置结果就是**这一路标未取到**，不是整次扫描
    失败。
    """
    pool = build_his_pool(
        dsn=his_dsn, env={"HIS_DB_QUERY_TIMEOUT_SECONDS": "0.3"}
    )
    try:
        with pool.session() as session:
            started = time.monotonic()
            with pytest.raises(ReconqError) as excinfo:
                session.execute(
                    "WITH RECURSIVE c(x) AS "
                    "(SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<90000000) "
                    "SELECT count(*) FROM c"
                )
            elapsed = time.monotonic() - started
    finally:
        pool.dispose()
    assert excinfo.value.code == "HIS_QUERY_TIMEOUT"
    # 0.3 秒 deadline，实际不能拖成几秒
    assert elapsed < 5, f"超时没在 deadline 附近生效（实测 {elapsed:.2f}s）"
    assert "未取到" in excinfo.value.detail
    assert "charge_time" in excinfo.value.detail  # 指向可排查的方向


def test_timeout_thread_is_daemon_so_service_can_exit(his_dsn):
    """超时后残留的查询线程必须是**守护线程**。

    ``concurrent.futures`` 建的是非守护线程，CPython 退出时会 join 它们。
    一次超时后慢查询未必真停得下来（interrupt 能否打断取决于驱动），
    于是「服务关了」会变成「等那条 HIS 慢查询跑完才关」——常驻进程上
    这就是一次假死。
    """
    pool = build_his_pool(
        dsn=his_dsn, env={"HIS_DB_QUERY_TIMEOUT_SECONDS": "0.2"}
    )
    try:
        with pool.session() as session:
            with pytest.raises(ReconqError):
                session.execute(
                    "WITH RECURSIVE c(x) AS "
                    "(SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<90000000) "
                    "SELECT count(*) FROM c"
                )
        names = [t.name for t in threading.enumerate() if t.name.startswith("reconq-his")]
        alive = [
            t
            for t in threading.enumerate()
            if t.name == "reconq-his-query" and t.daemon
        ]
        assert names, "没找到残留的查询线程，测试前提不成立"
        assert alive, "残留查询线程不是守护线程：进程退出会被它拖住"
    finally:
        pool.dispose()


def test_fast_query_is_not_disturbed_by_deadline(his_dsn):
    """正常查询不该被 deadline 误伤。"""
    pool = build_his_pool(
        dsn=his_dsn, env={"HIS_DB_QUERY_TIMEOUT_SECONDS": "30"}
    )
    try:
        with pool.session() as session:
            assert session.first(f"SELECT COUNT(*) FROM {TABLE}")[0] == 9
    finally:
        pool.dispose()


def test_stream_charge_rows_respects_deadline(his_dsn):
    """流式主路径上的超时也走同一条 deadline。

    逐窗取数（Task 6 的主路径）在只读会话上跑时，超时粒度落在**一个
    时间窗**上：第 6 窗慢不会把前 5 窗已取到的行一起作废。
    """
    pool = build_his_pool(
        dsn=his_dsn, env={"HIS_DB_QUERY_TIMEOUT_SECONDS": "30"}
    )
    try:
        with pool.session() as session:
            rows = list(session.stream_charge_rows(PERIOD))
            assert len(rows) == 9, "账期内 9 行收费流水应全部取到"
            assert rows[0].get("his_order_no") == "HIS-<CHARGE-0001>"
            # 金额仍是定点字符串，不因绕了一圈变回 float
            assert Decimal(rows[0].get("amount")) == Decimal("120.00")
    finally:
        pool.dispose()


# —— ④ 连接失败分类 ————————————————————————————————————


@pytest.mark.parametrize(
    "fragment,expected_code",
    [
        ("password authentication failed for user \"reconq\"", "HIS_DB_AUTH_FAILED"),
        ("Access denied for user 'reconq'@'10.0.0.9'", "HIS_DB_AUTH_FAILED"),
        ("Login failed for user reconq", "HIS_DB_AUTH_FAILED"),
        ('database "his_prod" does not exist', "HIS_DB_NOT_EXIST"),
        ("Unknown database 'his_prod'", "HIS_DB_NOT_EXIST"),
        ("Connection refused", "HIS_DB_UNREACHABLE"),
        ("could not translate host name", "HIS_DB_UNREACHABLE"),
        ("Network is unreachable", "HIS_DB_UNREACHABLE"),
        ("connection timed out", "HIS_DB_UNREACHABLE"),
    ],
)
def test_connection_error_classification(fragment, expected_code):
    """三类连接失败分得开：认证 / 库不存在 / 网络不可达。

    分不清时落到 HIS_DB_UNREACHABLE 并附原异常——**宁可说「不知道」也不
    猜一个具体成因**，排障时信息科至少知道去查网络还是查账号。
    """
    err = classify_connection_error(RuntimeError(fragment))
    assert err.code == expected_code, fragment
    assert err.channel == "his"


def test_unclassifiable_error_does_not_guess_a_cause():
    err = classify_connection_error(RuntimeError("some entirely novel failure"))
    assert err.code == "HIS_DB_UNREACHABLE"
    assert "novel failure" in err.detail, "原始异常必须带出来，否则没法排障"


def test_classification_is_shared_with_task6_path():
    """与 Task 6 的 ``_classify_db_error`` 是同一套判据。

    出现「扫描时报 A、data-source test 报 B」是最难排的一类问题。
    """
    assert classify_connection_error is _classify_db_error


def test_auth_failure_error_code_is_channel_level():
    """认证失败属渠道级：这一路标未取到，其余五路照常。"""
    for code in (
        "HIS_DB_AUTH_FAILED",
        "HIS_DB_NOT_EXIST",
        "HIS_DB_UNREACHABLE",
        "HIS_QUERY_TIMEOUT",
        "HIS_READONLY_ASSERT_FAILED",
    ):
        assert error_meta(code).channel_level is True, code


def test_missing_dsn_names_the_env_var():
    with pytest.raises(ReconqError) as excinfo:
        build_his_pool(dsn="", env={})
    assert excinfo.value.code == "HIS_DB_DSN_MISSING"
    assert "HIS_DB_DSN" in excinfo.value.detail


# —— ⑤ 仅 SELECT 指定列 ——————————————————————————————————


def test_select_star_is_rejected():
    """最小权限：``SELECT *`` 与 ``SELECT t.*`` 都被显式拒绝。"""
    for sql in ("SELECT * FROM charge_record", "select * from charge_record",
                "SELECT DISTINCT * FROM charge_record",
                "SELECT cr.* FROM charge_record cr"):
        with pytest.raises(ReconqError) as excinfo:
            assert_no_star(sql)
        assert excinfo.value.code == "HIS_SELECT_STAR_REJECTED", sql
    # 正常显式列不被误伤
    assert_no_star("SELECT amount, charge_time FROM charge_record")
    # 注释里出现的 SELECT * 不算
    assert_no_star("SELECT amount FROM charge_record -- SELECT * 是被禁的")


def test_session_rejects_select_star(his_dsn):
    pool = build_his_pool(dsn=his_dsn, env={})
    try:
        with pool.session() as session:
            with pytest.raises(ReconqError) as excinfo:
                session.execute(f"SELECT * FROM {TABLE}")
            assert excinfo.value.code == "HIS_SELECT_STAR_REJECTED"
            # 错误里点名该选哪些列，省一次翻契约
            assert "his_order_no" in excinfo.value.detail
    finally:
        pool.dispose()


def test_period_fetch_sends_only_the_eight_charge_columns(his_dsn):
    """账期取数发出去的 SQL 只含 CHARGE_COLUMNS 那八列。

    用 ``before_cursor_execute`` 抓真实 SQL 核对，不是看代码里写的常量。
    生产库上少读一列就少一份 IO。
    """
    pool = build_his_pool(dsn=his_dsn, env={})
    sent: list[str] = []

    @event.listens_for(pool._engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _params, _ctx, _many):
        sent.append(statement)

    try:
        with pool.session() as session:
            list(session.stream_charge_rows(PERIOD))
    finally:
        pool.dispose()
    charge_sqls = [s for s in sent if TABLE in s and s.lstrip().upper().startswith("SELECT")]
    assert charge_sqls, f"没抓到收费流水查询：{sent}"
    for sql in charge_sqls:
        select_list = sql.split("FROM")[0]
        assert "*" not in select_list, f"取数 SQL 里出现星号：{sql}"
        for col in CHARGE_COLUMNS:
            assert col in select_list, f"{col} 不在取数列里：{sql}"
    # 时间窗仍是半开区间，周一零点那笔不会被两期同时选中
    assert "charge_time >=" in charge_sqls[0] and "charge_time <" in charge_sqls[0]


# —— 池配置与可观测性 ————————————————————————————————


def test_pool_config_defaults_are_conservative():
    """默认池大小 5、超时 10s、查询超时 30s，缺省不配也能跑。"""
    cfg = HisPoolConfig.from_env({})
    assert cfg.pool_size == DEFAULT_POOL_SIZE == 5
    assert cfg.pool_timeout == DEFAULT_POOL_TIMEOUT_SECONDS == 10
    assert cfg.query_timeout == pytest.approx(float(DEFAULT_QUERY_TIMEOUT_SECONDS))
    # 绝不因为对账把院内 HIS 的 max_connections 顶穿
    assert cfg.max_overflow == 0
    assert cfg.statement_timeout_ms is None


def test_pool_config_reads_env_and_names_bad_var():
    cfg = HisPoolConfig.from_env(
        {
            "HIS_DB_POOL_SIZE": "3",
            "HIS_DB_POOL_TIMEOUT": "7",
            "HIS_DB_QUERY_TIMEOUT_SECONDS": "12.5",
            "HIS_DB_STATEMENT_TIMEOUT_MS": "9000",
        }
    )
    assert (cfg.pool_size, cfg.pool_timeout, cfg.query_timeout) == (3, 7, 12.5)
    assert cfg.statement_timeout_ms == 9000
    # 值不合法时点名变量，而不是用自己猜的默认值去连生产库
    for bad in ("many", "0", "-1"):
        with pytest.raises(ReconqError) as excinfo:
            HisPoolConfig.from_env({"HIS_DB_POOL_SIZE": bad})
        assert excinfo.value.code == "HIS_POOL_CONFIG_INVALID"
        assert "HIS_DB_POOL_SIZE" in excinfo.value.detail


def test_pool_status_is_json_serialisable(pool):
    """run summary 要能直接进 JSON 摘要与面板。

    QueuePool 的 ``size()`` 是**方法**不是属性，直接 getattr 会把一个
    bound method 塞进 JSON，面板拿到的是 ``<bound method ...>``。
    """
    import json

    with pool.session():
        status = pool.pool_status()
    assert json.loads(json.dumps(status))["dialect"] == "sqlite"
    assert status["checkedout"] == 1
    assert all(not callable(v) for v in status.values())


def test_session_closes_and_refuses_reuse(pool):
    with pool.session() as session:
        assert session.first("SELECT 1")[0] == 1
    with pytest.raises(ReconqError) as excinfo:
        session.execute("SELECT 1")
    assert excinfo.value.code == "HIS_SESSION_CLOSED"


def test_close_never_raises(pool):
    """关连接失败不抛异常（与源实现 close_connection() 同形）。

    否则关连接的次生异常会盖掉取数过程中真正需要被看见的那个。
    """
    session = pool.session()
    session.close()
    session.close()  # 幂等
    assert True


def test_pool_does_not_expose_raw_connection(his_dsn):
    """池**不暴露** connect() / 裸 Engine 属性——只给只读会话。

    只读这件事如果能被一行代码绕过去拿裸连接，它就不再是红线。
    """
    pool = HisReadOnlyPool(his_dsn, config=HisPoolConfig())
    try:
        assert not hasattr(pool, "connect")
        for attr in ("_engine", "_dsn"):
            assert isinstance(getattr(pool, attr), object)
        # 公开 API 只有这两条
        public = {n for n in dir(pool) if not n.startswith("_")}
        assert "session" in public
        assert "connect" not in public
        assert "engine" not in public
    finally:
        pool.dispose()


# —— 规模 ————————————————————————————————————————————————


def test_two_thousand_rows_stream_within_bounded_memory(tmp_path):
    """真实规模：2000+ 行流式取数，内存不随行数线性涨。

    对应 spec「容量与存储演进」的验收：峰值内存约一批 + 最大单行。
    这里退一步验可观测的性质——行数取到、批次数远小于行数（说明确实是
    分批而不是一次性物化）。
    """
    dsn = _make_his_db(str(tmp_path / "big.db"), rows=2000)
    pool = build_his_pool(dsn=dsn, env={})
    from reconq.sources.his_db import StreamStats

    stats = StreamStats()
    try:
        with pool.session() as session:
            rows = list(
                session.stream_charge_rows(PERIOD, stats=stats, window_days=7)
            )
    finally:
        pool.dispose()
    assert len(rows) == 2000
    assert 0 < stats.batches < 2000, (
        f"批次数 {stats.batches} 退化成行数了，说明没有真正分批"
    )
    assert stats.windows == 1
    assert stats.widest_row_bytes > 0


# —— 替身 ————————————————————————————————————————————


def _fake_conn(rows):
    """一个只会回固定行的假连接，用于测断言分支（不连任何库）。"""

    class _Result:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return self._value

    class _Conn:
        def exec_driver_sql(self, statement):
            return _Result(rows[0])

    return _Conn()


# —— data-source test 走只读池（信息科排障腿）——————————————


def test_data_source_test_his_reports_readonly_verified(his_dsn, monkeypatch):
    """``reconq data-source test his`` 走只读池，并报出只读已验。

    信息科问「这一路通不通」时，他想知道的是「我连上去之后能不能写」。
    答案必须是当场验出来的，不是文档里承诺的。
    """
    from reconq.sources.registry import probe_channel

    monkeypatch.setenv("HIS_DB_DSN", his_dsn)
    result = probe_channel("his")
    assert result["ok"] is True
    assert result["read_only_verified"] is True
    assert result["connected"] is True
    assert "只读事务已断言生效" in result["detail"]
    assert result["pool_config"]["pool_size"] == DEFAULT_POOL_SIZE
    assert result["pool"]["dialect"] == "sqlite"


def test_data_source_test_his_reports_readonly_refusal(his_dsn, monkeypatch):
    """连得上但只读没生效 → 预检就该失败，且不连着业务查询。

    预检放行、扫描时才拒，信息科会以为「通了」，白等一次扫描。
    """
    from reconq.sources.registry import probe_channel
    from reconq.sources import his_pool as his_pool_mod

    monkeypatch.setenv("HIS_DB_DSN", his_dsn)
    original = his_pool_mod.READONLY_SETUP_SQL["sqlite"]
    his_pool_mod.READONLY_SETUP_SQL["sqlite"] = ("PRAGMA query_only = 0",)
    try:
        result = probe_channel("his")
    finally:
        his_pool_mod.READONLY_SETUP_SQL["sqlite"] = original
    assert result["ok"] is False
    assert result["connected"] is False
    assert result["read_only_verified"] is False
    assert result["error_code"] == "HIS_READONLY_ASSERT_FAILED"


def test_data_source_test_his_missing_dsn_still_names_env_var(monkeypatch):
    """没配 DSN 时仍点名环境变量，而不是抛栈。"""
    from reconq.sources.registry import probe_channel

    monkeypatch.delenv("HIS_DB_DSN", raising=False)
    result = probe_channel("his")
    assert result["ok"] is False
    assert result["error_code"] == "HIS_DB_DSN_MISSING"
    assert "HIS_DB_DSN" in result["detail"]


def test_timeout_does_not_flood_console_with_driver_stack(his_dsn, caplog):
    """超时**不该**往控制台甩一屏与结论矛盾的驱动栈。

    超时是设计内的处置结果（这一路标未取到、其余五路照常）。控制台正在
    展示 HIS_QUERY_TIMEOUT 时，后面跟着一屏 ``Error closing cursor`` 加
    驱动栈，信息科会以为超时处理本身出了问题。

    难点在于这条记录是**异步**到达的（由超时后仍在收尾的守护线程打出），
    落在 ``invalidate()`` 已返回之后——所以过滤必须常驻，不能靠临时压
    上下文里的日志级别。
    """
    import time as _time

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector()
    pool = build_his_pool(
        dsn=his_dsn, env={"HIS_DB_QUERY_TIMEOUT_SECONDS": "0.25"}
    )
    # 观察**池自己的 logger 实例**——记录就打在那儿（见 his_db 里的说明）
    pool_logger = pool._engine.pool.logger
    pool_logger.addHandler(handler)
    try:
        with pool.session() as session:
            with pytest.raises(ReconqError):
                session.execute(
                    "WITH RECURSIVE c(x) AS "
                    "(SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<90000000) "
                    "SELECT count(*) FROM c"
                )
        # 给收尾中的线程一点时间把记录吐出来
        _time.sleep(0.6)
    finally:
        pool.dispose()
        pool_logger.removeHandler(handler)

    noise = [r for r in records if "closing cursor" in r.getMessage().lower()]
    assert not noise, f"超时后仍向控制台甩了 {len(noise)} 条游标关闭栈"
    # 过滤器只压这一类，不压池的其他 ERROR（真连不上库那种仍要让人看见）
    from reconq.sources.his_db import _DropClosingCursorNoise

    flt = _DropClosingCursorNoise()
    assert flt.filter(_record("Error closing cursor")) is False
    assert flt.filter(_record("connection refused")) is True


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("sqlalchemy.pool", logging.ERROR, __file__, 1, message, None, None)
