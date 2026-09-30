"""归因草稿落库与「非法结构绝不落库」的强制点（Task 29）。

一条硬纪律：**校验不过的草稿不进结果表**
----------------------------------------
归因草稿最终会出现在结论单上，而结论单是要签字的。如果「模型说了什么」与
「我们存了什么」之间隔着一次「尽量修一修」，那结论单上那条成因就既不是
模型说的、也不是我们判的 —— 它是某个中间环节脑补出来的，而那个环节没有
任何留痕。

所以本模块提供的是**唯一落库出口** :func:`save_drafts`：它只接受已经过
:mod:`reconq.gateway.schema` 六层校验的 :class:`AttributionDraft`，并把
被拒的草稿**连拒因一起**写进 ``attribution_rejections`` 表。面板上那一格
「模型输出质量」问的正是这张表：给了几条、收下几条、丢了几条、每条丢在
哪一层。丢是允许的，**静默丢**不允许 —— 那会让「模型今天不太行」看起来
和「今天没有差异」一模一样。

三处刻意为之
------------

1. **键 = 账期 + 渠道 + 差异键 + 排名**。同一账期重扫、重跑、并发重扫
   撞同一个键，``ON CONFLICT DO NOTHING`` 保证不产生重复行。排名进键是
   因为排名本身就是结论的一部分（Top-1 与 Top-3 要能分别取到）。
2. **草稿恒为草稿**。``disposition`` 列固定写 ``'待确认'`` 且**没有**任何
   更新它的接口：落库这一层在类型与表结构上就不提供推进状态的能力。
   状态迁移只走 :mod:`reconq.engine.period` 的白名单，而那条只接受人工
   点的参数。
3. **金额与置信度一律存字符串**。``confidence`` 存定点字面量（``"0.85"``）
   而不是 ``REAL``：SQLite 的 ``REAL`` 是 IEEE754 双精度，读回来可能是
   ``0.8500000000000001``，而「成因 Top-1 命中率」要按置信度排序复算，
   排序键每次读出来都略有不同就没法复算了。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层**：它的 check 结果是**当场打到终端**
的，进程一退出就没了，链路上不存在「把模型/规则的结论落库」这件事，
更不存在「落库前再校验一次」这个位置。本模块是本产品新建的：财务科要在
周一之后还能回答「上周那条 66 元的差异，当时模型给的是什么成因、谁确认的、
依据哪几行」—— 这要求结论**持久化**，而持久化就要求一个能守住边界的
写入点。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, NamedTuple, Optional, Sequence

from ..errors import ReconqError
from ..gateway.schema import AttributionDraft, SchemaLayerError

#: ``evidence_refs`` 存 JSON 数组字符串而不是分隔符拼接：行标识里本来就
#: 可能出现分隔符（``a,b.csv:3`` 这类文件名），用分隔符拼回来时无法无歧义
#: 地切开。存字符串是全仓一致的金额/置信度存储口径。

__all__ = [
    "ATTRIBUTION_DRAFT_TABLE",
    "ATTRIBUTION_DRAFT_TABLE_DDL",
    "ATTRIBUTION_DRAFT_INDEXES",
    "ATTRIBUTION_REJECTION_TABLE",
    "ATTRIBUTION_REJECTION_TABLE_DDL",
    "ATTRIBUTION_REJECTION_INDEXES",
    "DISPOSITION_DRAFT",
    "SaveOutcome",
    "init_attribution_schema",
    "layer_failure_counts",
    "list_drafts",
    "list_rejections",
    "save_drafts",
    "to_draft_row",
]


ATTRIBUTION_DRAFT_TABLE = "attribution_drafts"
ATTRIBUTION_REJECTION_TABLE = "attribution_rejections"

#: 草稿落库时的处置状态。**恒为这一值**，本模块不提供任何改写它的入口。
DISPOSITION_DRAFT = "待确认"


ATTRIBUTION_DRAFT_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {ATTRIBUTION_DRAFT_TABLE} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL,
    channel         TEXT NOT NULL,
    conclusion_key  TEXT NOT NULL,
    rank            INTEGER NOT NULL,
    cause           TEXT NOT NULL,
    confidence_text TEXT NOT NULL,
    evidence_refs   TEXT NOT NULL,
    explanation_zh  TEXT NOT NULL,
    disposition     TEXT NOT NULL DEFAULT '{DISPOSITION_DRAFT}',
    run_id          TEXT NOT NULL DEFAULT '',
    bundle_digest   TEXT NOT NULL DEFAULT '',
    model_name      TEXT NOT NULL DEFAULT '',
    recorded_at     TEXT NOT NULL DEFAULT ''
)
"""

#: 幂等键：账期 + 渠道 + 差异键 + 排名。重扫 / 重跑 / 并发撞同一个键。
ATTRIBUTION_DRAFT_INDEXES = (
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_attribution_identity
        ON {ATTRIBUTION_DRAFT_TABLE} (period, channel, conclusion_key, rank)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS ix_attribution_period
        ON {ATTRIBUTION_DRAFT_TABLE} (period, channel, disposition)
    """,
)


ATTRIBUTION_REJECTION_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {ATTRIBUTION_REJECTION_TABLE} (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    period         TEXT NOT NULL,
    channel        TEXT NOT NULL,
    conclusion_key TEXT NOT NULL,
    layer          TEXT NOT NULL,
    code           TEXT NOT NULL,
    draft_index    INTEGER NOT NULL DEFAULT -1,
    cause_text     TEXT NOT NULL DEFAULT '',
    detail         TEXT NOT NULL DEFAULT '',
    run_id         TEXT NOT NULL DEFAULT '',
    recorded_at    TEXT NOT NULL DEFAULT ''
)
"""

#: 幂等键：账期 + 渠道 + 差异键 + 层 + 草稿序号。同一批重跑不重复记账 ——
#: 拒因统计要能复算，重跑一次让计数翻倍就没法复算了。
#:
#: ``draft_index`` 声明 ``NOT NULL DEFAULT -1`` 而不是留可空：SQLite 的
#: 唯一索引把 ``NULL`` 视为**互不相等**，可空列上的 ``ON CONFLICT DO NOTHING``
#: 永远不会命中 —— 拒因重跑一次就多一行，计数翻倍，正是这条索引要防的事。
#: ``-1`` 是「这条拒因不属于某个批次位置」的哨兵（外层形态错时用）。
ATTRIBUTION_REJECTION_INDEXES = (
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_attribution_rejection_identity
        ON {ATTRIBUTION_REJECTION_TABLE}
           (period, channel, conclusion_key, layer, draft_index)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS ix_attribution_rejection_layer
        ON {ATTRIBUTION_REJECTION_TABLE} (period, layer)
    """,
)

#: ``draft_index`` 的「无批次位置」哨兵。见 :data:`ATTRIBUTION_REJECTION_INDEXES`
#: 的说明：留空会让唯一索引失效。
NO_DRAFT_INDEX = -1


class SaveOutcome(NamedTuple):
    """落库结果三元组 ``(inserted, skipped, rejected)``。

    用 :class:`~typing.NamedTuple`：调用方既能
    ``inserted, skipped, rejected = save_drafts(...)`` 一行解包，
    也能读 ``outcome.inserted``；两种写法在测试与 CLI 里都常见。
    """

    inserted: int
    skipped: int
    rejected: int

    def to_dict(self) -> dict[str, int]:
        return {"inserted": self.inserted, "skipped": self.skipped, "rejected": self.rejected}


def init_attribution_schema(conn: sqlite3.Connection) -> None:
    """建草稿表、拒因表与索引。幂等：重复调用不报错。"""
    conn.execute(ATTRIBUTION_DRAFT_TABLE_DDL)
    for statement in ATTRIBUTION_DRAFT_INDEXES:
        conn.execute(statement)
    conn.execute(ATTRIBUTION_REJECTION_TABLE_DDL)
    for statement in ATTRIBUTION_REJECTION_INDEXES:
        conn.execute(statement)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def to_draft_row(
    draft: AttributionDraft,
    *,
    period: str,
    channel: str,
    conclusion_key: str,
    rank: int,
    run_id: str = "",
    bundle_digest: str = "",
    model_name: str = "",
) -> tuple[Any, ...]:
    """:class:`AttributionDraft` → 一行 INSERT 参数。

    **入参类型就是防线**：收 ``AttributionDraft`` 而不是 dict，调用方就
    无法把一个没校验过的原始 dict 直接塞进来。想落库就必须先过
    :mod:`reconq.gateway.schema` 的六层校验 —— 纪律落在类型上，而不是
    落在「记得先校验」这句口头约定上。
    """
    return (
        period,
        channel,
        conclusion_key,
        rank,
        draft.cause,
        # 定点字面量：不用 REAL。读回来做排序键要逐次一致。
        format(draft.confidence, ".6f"),
        json.dumps([r.ref for r in draft.evidence_refs], ensure_ascii=False),
        draft.explanation_zh,
        DISPOSITION_DRAFT,
        run_id,
        bundle_digest,
        model_name,
        _now(),
    )


_INSERT_SQL = f"""
INSERT INTO {ATTRIBUTION_DRAFT_TABLE} (period, channel, conclusion_key, rank, cause,
    confidence_text, evidence_refs, explanation_zh, disposition, run_id,
    bundle_digest, model_name, recorded_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (period, channel, conclusion_key, rank) DO NOTHING
"""

_INSERT_REJECTION_SQL = f"""
INSERT INTO {ATTRIBUTION_REJECTION_TABLE} (period, channel, conclusion_key, layer, code,
    draft_index, cause_text, detail, run_id, recorded_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (period, channel, conclusion_key, layer, draft_index) DO NOTHING
"""


def save_drafts(
    conn: sqlite3.Connection,
    drafts: Sequence[AttributionDraft],
    *,
    period: str,
    channel: str,
    conclusion_key: str,
    run_id: str = "",
    bundle_digest: str = "",
    model_name: str = "",
    rejected: Sequence[SchemaLayerError] = (),
) -> SaveOutcome:
    """落一批草稿 + 一批拒因。**整批要么全成要么回滚。**

    ``drafts`` 里的每一条都必须是 :class:`AttributionDraft`；传了别的
    类型即抛 ``ATTRIBUTION_DRAFT_INVALID``，而不是悄悄 ``str()`` 一下
    塞进库 —— 那种「宽容」正是让非法结构混进结果表的入口。

    **按传入顺序编 rank，不在本层排序。** 排名是归因层的结论
    （:func:`reconq.align.attribution._rank` 里有稳定性保证：同分保持
    模型给出的原顺序，否则 Top-1 命中率无法复算）。落库这一层再排一次
    序，就等于把「排名是谁定的」变成两处都得对。

    批内半截落库比不落更糟：面板上会显示 3 条成因，实际只有 2 条经得起
    复核，而缺的第 3 条恰好是模型给得最含糊的那条。故整批显式
    ``BEGIN`` / ``COMMIT`` / ``ROLLBACK``。
    """
    for position, draft in enumerate(drafts, start=1):
        if not isinstance(draft, AttributionDraft):
            # 走到这里说明调用方想直接把模型原始 dict 塞进结果表。
            # 显式拒绝并说清该走哪条路，而不是 ``str(draft)`` 一下照收 ——
            # 后者正是「非法结构混进结论单」最省事也最危险的那个入口。
            raise ReconqError(
                "ATTRIBUTION_DRAFT_INVALID",
                f"第 {position} 条草稿是 {type(draft).__name__} 而不是 "
                f"AttributionDraft —— 落库前必须先过 gateway.schema 的六层校验"
                f"（validate_candidates），本产品不提供「先存着再说」的旁路",
            )
    rows = [
        to_draft_row(
            draft,
            period=period,
            channel=channel,
            conclusion_key=conclusion_key,
            rank=rank,
            run_id=run_id,
            bundle_digest=bundle_digest,
            model_name=model_name,
        )
        for rank, draft in enumerate(drafts, start=1)
    ]
    rejection_rows = [
        (
            period,
            channel,
            conclusion_key,
            error.layer.value,
            error.code,
            NO_DRAFT_INDEX if error.index is None else error.index,
            _cause_of(error),
            error.detail,
            run_id,
            _now(),
        )
        for error in rejected
    ]

    conn.execute("BEGIN")
    try:
        inserted = 0
        for row in rows:
            cursor = conn.execute(_INSERT_SQL, row)
            inserted += cursor.rowcount if cursor.rowcount > 0 else 0
        rejected_written = 0
        for row in rejection_rows:
            cursor = conn.execute(_INSERT_REJECTION_SQL, row)
            rejected_written += cursor.rowcount if cursor.rowcount > 0 else 0
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return SaveOutcome(inserted, len(rows) - inserted, rejected_written)


def _cause_of(error: SchemaLayerError) -> str:
    """拒因行上的成因文本（若有）。

    结构层失败时模型可能连 ``cause`` 都没给对，这时留空串而不是
    ``repr(整个 raw)``：拒因表会被面板整段展示，把整份原始响应塞进去
    等于把渠道账单行再抄一份到另一个库里。
    """
    marker = "cause="
    if marker in error.detail:
        return error.detail.split(marker, 1)[1].split(" ", 1)[0].strip().strip("'\"")
    return ""


def list_drafts(
    conn: sqlite3.Connection,
    period: str,
    *,
    channel: Optional[str] = None,
    conclusion_key: Optional[str] = None,
) -> list[dict[str, Any]]:
    """按排名读回草稿。默认只读**待确认**的（草稿态）。

    ``disposition`` 恒为 ``待确认``，这里仍显式带上该条件：将来若接入
    人工确认（Task 43 的白名单迁移），本函数的行为就是「草稿视图」，
    而不是「全部视图」—— 语义写在读口上，不靠调用方记得。
    """
    sql = (
        f"SELECT * FROM {ATTRIBUTION_DRAFT_TABLE} "
        "WHERE period = ? AND disposition = ?"
    )
    params: list[Any] = [period, DISPOSITION_DRAFT]
    if channel is not None:
        sql += " AND channel = ?"
        params.append(channel)
    if conclusion_key is not None:
        sql += " AND conclusion_key = ?"
        params.append(conclusion_key)
    sql += " ORDER BY channel, conclusion_key, rank"
    return [dict(row) for row in conn.execute(sql, params)]


def list_rejections(
    conn: sqlite3.Connection,
    period: str,
    *,
    channel: Optional[str] = None,
) -> list[dict[str, Any]]:
    """读回拒因记录（面板「模型输出质量」那一格的数据源）。"""
    sql = f"SELECT * FROM {ATTRIBUTION_REJECTION_TABLE} WHERE period = ?"
    params: list[Any] = [period]
    if channel is not None:
        sql += " AND channel = ?"
        params.append(channel)
    sql += " ORDER BY channel, conclusion_key, id"
    rows = [dict(row) for row in conn.execute(sql, params)]
    for row in rows:
        # 把哨兵还原成「无批次位置」，读口不暴露存储层的实现细节。
        if row["draft_index"] == NO_DRAFT_INDEX:
            row["draft_index"] = None
    return rows


def layer_failure_counts(
    conn: sqlite3.Connection,
    period: str,
    *,
    channel: Optional[str] = None,
) -> dict[str, int]:
    """按校验层统计拒因条数。

    回答的是「模型输出的问题集中在哪」：``confidence`` 层条数高说明
    口径没对齐，``evidence_domain`` 层条数高说明模型在按记忆编 —— 后者
    调 prompt 没用，得改证据包。
    """
    sql = (
        f"SELECT layer, COUNT(*) AS n FROM {ATTRIBUTION_REJECTION_TABLE} "
        "WHERE period = ?"
    )
    params: list[Any] = [period]
    if channel is not None:
        sql += " AND channel = ?"
        params.append(channel)
    sql += " GROUP BY layer"
    return {row["layer"]: int(row["n"]) for row in conn.execute(sql, params)}
