"""右栏的证据展开：候选行全表、快照引用与深链回跳（Task 48 第 5 步）。

为什么这一块单独一个模块
------------------------
:mod:`reconq.web.views` 回答「这条差异该怎么看」（四路来源并列、明示
采用哪一路），本模块回答「这条差异**据以判断的那几行**具体是什么」。
两者在面板上挨着，但取数路径完全不同：

* **决策辅助**只读草稿表、拒因表、先例留痕表 —— 三张表都是**派生结论**；
* **证据展开**要读账单行（:data:`reconq.store.bill_rows`）、对齐快照
  （:data:`reconq.align.snapshots`）与处置单上的 ``evidence_json`` ——
  **源行本身**。

把源行也塞进 ``/discrepancies`` 列表接口，等于中栏每翻一页都把整条
渠道账期的账单行拉一遍 —— 列表接口的职责是渲染一行摘要，不是当数据
仓库使。所以本模块只经**一个按 key 展开的端点**暴露，前端点开哪条
才拉哪条。

三条不变式
----------

1. **源行原文不落 HTTP 响应。** 只回 :data:`EVIDENCE_COLUMNS` 白名单里的
   定位要素，理由与 :class:`reconq.align.snapshot.SnapshotRef` 的文档串
   同源：渠道账单可能带自定义列、可能含患者信息，而「回到第几行去看」
   本来就是财务科和渠道方之间的口头约定。面板给行号，对方在自己那份
   账单文件里查，两边对得上。

   **白名单不是脱敏保证，这一点要说清楚**：白名单管的是「不把渠道的
   原始列整行倒出来」，而其中 ``settle_type`` 一列，按
   :data:`reconq.store.bill_store.CHANNEL_COLUMN_MAP` 的既有映射，
   微信那路取的正是 ``payer``（用户标识）。也就是说这一列的值本身带
   患者相邻信息，是**全产品**从落库、快照、弱匹配打分到结论单一贯
   带着的口径（:class:`SnapshotRef` 也把它列进
   ``SNAPSHOT_ALLOWED_FIELDS``），不是本模块引入的。本模块不擅自改变
   全产品的口径，也不因此把这一列藏起来 —— 结算方式恰是判断「这笔到
   底算不算同一笔」的关键依据，藏了经办人就只能靠猜。真要收紧应改
   映射表（:mod:`reconq.store.bill_store`）并连带重跑快照，而不是在
   面板这一层单方面不打。
2. **快照引用是** ``snap_id`` + 内容哈希，不是行号。快照是**拍摄时刻
   的副本**，账单表现已被改写；复算要的是「当时那几行」，所以引用
   指向快照而不是指向现在的 ``bill_rows``。两者不一致本身就是结论
   （见 :func:`evidence_panel` 的 ``drift`` 字段）。
3. **深链双向可解析。** 构造与解析复用
   :func:`reconq.audit.wecom.build_deep_link` /
   :func:`reconq.audit.wecom.parse_deep_link` —— 与 Task 46 推送里
   用的是同一对函数，面板与企微消息对同一个键的写法不可能分叉。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ..errors import ReconqError

__all__ = [
    "EVIDENCE_COLUMNS",
    "candidate_rows",
    "deep_link_for",
    "evidence_panel",
    "snapshot_refs",
]


#: 候选行全表允许出现在 HTTP 响应里的列。**白名单而不是黑名单**：
#: 账单表将来加列时，列不在这里就自动不外发，而加一列在黑名单上的人
#: 不会想到它带着患者信息。
#:
#: 白名单的形状与 :class:`reconq.align.snapshot.SnapshotRef` 一致 ——
#: 面板上显示的每一列都能在结论单的快照附件里找到同一列，财务科拿两
#: 份东西对得上。模块头「三条不变式」第 1 条说明了白名单**不是**脱敏
#: 保证的边界（``settle_type`` 在微信那路取的是用户标识列）。
#:
#: 顺序即前端表格的列序。
EVIDENCE_COLUMNS: tuple[str, ...] = (
    "side",
    "channel",
    "order_no",
    "his_order_no",
    "amount",
    "refund",
    "trade_time",
    "trade_type",
    "department",
    "settle_type",
    "is_refund",
    "source_file",
    "source_row_no",
    "ref",
)


def _bills_table(conn: sqlite3.Connection) -> bool:
    from ..store.schema import BILL_TABLE

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (BILL_TABLE,)
    ).fetchone()
    return row is not None


def _snapshot_exists(conn: sqlite3.Connection) -> bool:
    from ..align.snapshot import ALIGN_SNAPSHOT_TABLE

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (ALIGN_SNAPSHOT_TABLE,),
    ).fetchone()
    return row is not None


def _norm_key(value: Any) -> str:
    """对齐侧的键归一。与 :func:`reconq.align.bundle` 同一口径。

    刻意**不另写一份**归一规则：面板按 ``EX-9`` 查而对齐器按
    ``ex-9`` 存，候选行就会永远查空，而症状是「快照里有候选、
    面板上展开不出行」—— 极难自查。
    """
    from ..align.bundle import normalize_key

    return normalize_key(str(value or ""))


def candidate_rows(
    conn: sqlite3.Connection, period: str, order_no: str, *, channel: str = ""
) -> list[dict[str, Any]]:
    """这条单号在账单库里的**全表**候选行（渠道侧 + HIS 侧）。

    「全表」指渠道侧与 HIS 侧都查：只回渠道侧那一条，弱匹配与歧义
    四形态（拆笔 / 合并支付 / 跨账期 / 退费并存）全都判不出来 ——
    判据正是**两侧摆在一起**看。

    HIS 侧不另查只读库：账单行落库时已把 ``his_order_no`` 写在同一行上
    （见 :data:`reconq.store.bill_rows` 的列定义），面板不为了显示一行
    候选就去连生产 HIS 库 —— 一个只读页面不该有理由触发生产库连接。
    """
    if not order_no or not _bills_table(conn):
        return []

    target = _norm_key(order_no)
    sql = (
        "SELECT channel, channel_order_no, his_order_no, amount_text, refund_text,"
        " trade_time, trade_type, department, settle_type, source_file, source_row_no,"
        " bill_date FROM bill_rows WHERE period = ?"
    )
    params: list[Any] = [period]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    sql += " ORDER BY channel, bill_date, source_row_no, ch_id"

    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, params):
        chan_order = str(row["channel_order_no"] or "")
        his_order = str(row["his_order_no"] or "")
        # 两侧的键都命中才算：渠道单号与 HIS 单号是两套编号体系，
        # 只比一侧会让「渠道号相等但 HIS 号不同」的退费行重复出现。
        if target not in (_norm_key(chan_order), _norm_key(his_order)):
            continue
        refund = row["refund_text"]
        try:
            is_refund = bool(refund) and refund not in ("0", "0.00", "0.000")
        except TypeError:  # pragma: no cover - refund_text 是 TEXT，不会有非串
            is_refund = False
        entry = {
            "side": "channel",
            "channel": row["channel"],
            "order_no": chan_order,
            "his_order_no": his_order,
            "amount": row["amount_text"],
            "refund": refund,
            "trade_time": row["trade_time"],
            "trade_type": row["trade_type"],
            "department": row["department"],
            "settle_type": row["settle_type"],
            "is_refund": is_refund,
            "source_file": row["source_file"],
            "source_row_no": row["source_row_no"],
            "bill_date": row["bill_date"],
        }
        entry["ref"] = (
            f"{entry['source_file']}:{entry['source_row_no']}"
            if entry["source_file"]
            else f"#{entry['source_row_no']}"
        )
        out.append(entry)
    return out


def snapshot_refs(
    conn: sqlite3.Connection, period: str, conclusion_key: str
) -> list[dict[str, Any]]:
    """这条结论拍过的对齐快照（拍摄先后）。

    **可能不止一份**：同键内容不同就是真冲突，两份都留着
    （:func:`reconq.align.snapshot.record_alignment_snapshot` 的纪律）。
    面板只报「有几份、哈希是否一致」，**不替人挑一份**——挑了就等于
    悄悄决定「哪次扫描才是对的」。
    """
    if not conclusion_key or not _snapshot_exists(conn):
        return []
    from ..align.snapshot import load_alignment_snapshots

    try:
        snaps = load_alignment_snapshots(conn, period=period, conclusion_key=conclusion_key)
    except Exception:  # noqa: BLE001 - 快照表损坏时右栏降级为「无快照」
        return []

    out: list[dict[str, Any]] = []
    for snap in snaps:
        out.append(
            {
                "conclusion_key": snap.conclusion_key,
                "period": snap.period,
                "channel": snap.channel,
                "kind": snap.kind,
                "order_no": snap.order_no,
                "detail": snap.detail,
                "diff_amount": snap.diff_amount,
                "ambiguity_kind": snap.ambiguity_kind,
                "participant_total": snap.participant_total,
                "content_hash": snap.content_hash,
                "contract_digest": snap.contract_digest,
                "contract_check_digest": snap.contract_check_digest,
                "index_version": snap.index_version,
                "created_at": snap.created_at,
                "participants": [p.to_dict() for p in snap.participants],
            }
        )
    return out


def deep_link_for(period: str, diff_key: str, conclusion_key: str = "") -> dict[str, Any]:
    """本条的 ``reconq://`` 深链 + 面板回跳地址 + CLI 复算命令。

    三个出口对应三种「把这条差异发出去」的场景：粘进企微（深链）、
    粘进工单（回跳地址）、发给信息科复算（CLI 命令）。构造复用
    :func:`reconq.audit.wecom.build_deep_link`，与推送消息同源。
    """
    import urllib.parse

    from ..audit.wecom import build_deep_link, parse_deep_link

    link = build_deep_link(period, diff_key)
    # 立刻回解析一次做**自证**：构造与解析必须是同一对函数，否则面板上
    # 贴出来的链接点开可能落到别处，而这种错在测试里看不出来。
    back_period, back_key = parse_deep_link(link)
    # 差异键里有 ``|``，进 query 必须编码 —— 未编码时部分反代与
    # URLSearchParams 会把它截断，点开落在半截键上。
    quoted = urllib.parse.quote(diff_key, safe="")
    return {
        "deep_link": link,
        "panel_url": f"/?period={urllib.parse.quote(period, safe='')}&diff={quoted}",
        # ``--replay`` 吃的是**结论键**（渠道:账期:单号），不是差异键
        # （账期|渠道|口径）。两者混用会得到一条「查无此结论」的复算
        # 命令 —— 面板上贴出去让人白跑一次。
        "cli_command": f"reconq verify --replay {conclusion_key}" if conclusion_key else "",
        "conclusion_key": conclusion_key,
        "parsed_period": back_period,
        "parsed_key": back_key,
        "round_trip_ok": back_period == period and back_key == diff_key,
    }


def evidence_panel(
    conn: sqlite3.Connection, period: str, row: dict[str, Any]
) -> dict[str, Any]:
    """一条差异的完整证据面板。

    返回三块：**候选行全表**（现账）、**快照引用**（当时账）、
    **深链三出口**。``drift`` 说明两者是否还对得上。
    """
    key = str(row.get("key") or "")
    order_no = str(row.get("order_no") or "")
    conclusion_key = str(row.get("conclusion_key") or "")

    rows = candidate_rows(conn, period, order_no, channel=str(row.get("channel") or ""))
    snaps = snapshot_refs(conn, period, conclusion_key)

    # 快照里的参与方行号集合。用来判「快照拍的行现在还在不在库里」——
    # 不在，说明账单被重扫覆盖或源文件被换过；这时快照仍可复算，但它与
    # 现在的账单**已经不是同一份数据**了。
    snap_refs: set[str] = set()
    for snap in snaps:
        for part in snap["participants"]:
            snap_refs.add(str(part.get("ref") or ""))
    live_refs = {r["ref"] for r in rows}
    drifted = sorted(snap_refs - live_refs) if snap_refs else []

    # 处置单上登记的证据行。**直接问处置单**，不从差异台账行上取：
    # 台账那行的 ``evidence_total`` 只是个计数，定位在哪几行只有
    # 开单时写下的 ``evidence_json`` 知道。两处不是同一份事实。
    ledger_evidence = _ledger_evidence_rows(conn, key)

    return {
        "key": key,
        "period": period,
        "order_no": order_no,
        "conclusion_key": conclusion_key,
        "candidates": rows,
        "candidate_total": len(rows),
        "snapshots": snaps,
        "snapshot_total": len(snaps),
        "snapshot_conflict": len({s["content_hash"] for s in snaps}) > 1,
        "ledger_evidence_rows": ledger_evidence,
        "drift": drifted,
        "drifted": bool(drifted),
        "links": deep_link_for(period, key, conclusion_key),
        "note": (
            "候选行来自账单库现状，快照引用来自拍摄当时；两者不一致时以快照复算为准"
            if drifted
            else "候选行与快照引用一致，可直接复算"
        ),
    }


def _ledger_evidence_rows(conn: sqlite3.Connection, key: str) -> list[str]:
    """处置单上登记的证据行定位。开单时没写就是空列表（不是报错）。"""
    from ..ledger.flow import _select_by_key

    try:
        item = _select_by_key(conn, key)
    except Exception:  # noqa: BLE001 - 处置单表不存在时右栏显示「未开单」
        return []
    if item is None:
        return []
    return list(item.evidence_rows or [])


def require_row(
    conn: sqlite3.Connection, period: str, key: str
) -> dict[str, Any]:
    """按差异键取台账行。取不到抛 ``LEDGER_DISCREPANCY_NOT_FOUND``。

    **不在这里新造一份「没有就返回空面板」**：空面板在面板上与
    「这条差异没有候选行」长得一模一样，而两者要走的排查路径完全
    不同（一个查账没扫，一个查对齐结论键）。
    """
    from .state import discrepancy_rows

    for row in discrepancy_rows(conn, period)["rows"]:
        if row["key"] == key:
            return row
    raise ReconqError(
        "LEDGER_DISCREPANCY_NOT_FOUND",
        f"账期 {period} 的差异台账里没有键 {key!r}；"
        f"先跑一次 reconq scan --period {period} 再展开证据",
    )
