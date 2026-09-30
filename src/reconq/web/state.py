"""面板的只读投影（Task 47 · 三栏的数据源）。

三栏各读一处，**都不新增判定**：左栏读 :mod:`reconq.engine.period` 的
渠道结果，中栏读差异台账，右栏读 :mod:`reconq.ledger` 的处置单。面板
显示的每一个数字都能在 CLI 的同一条命令里找到同一个出处 —— 一个只在
网页上存在的数字，在签字前是问不出来的。

左栏的「主态色」由 :func:`channel_badge` 给出，四色，与
``docs/concepts/visual-target.md`` 的视觉目标卡一致：

* **绿**：口径全过（六路都取到、这一路没有 fail）
* **黄**：有待归因的单边账（取到了，但有差异没定性）
* **红**：有未确认的 fail
* **灰虚线**：本期没取到，显示错误码

**六路少一路绝不给绿**（与 :func:`reconq.engine.period.derive_gate`
同一条纪律）：少一路的「全部口径通过」不是全部通过。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from ..ledger.models import LEDGER_DISCREPANCY_TABLE
from ..store.schema import BILL_TABLE

__all__ = [
    "BADGE_GRAY",
    "BADGE_GREEN",
    "BADGE_RED",
    "BADGE_YELLOW",
    "BADGE_ZH",
    "action_options",
    "channel_badge",
    "channel_cards",
    "discrepancy_rows",
    "disposition_rows",
    "order_no_of",
    "period_overview",
    "periods",
]

#: 四种渠道主态。**灰不是「正常」**：它是「本期没取到」，与绿在
#: 面板上必须一眼可分，否则少一路渠道会被读成「这一路没问题」。
BADGE_GREEN = "green"
BADGE_YELLOW = "yellow"
BADGE_RED = "red"
BADGE_GRAY = "gray"

BADGE_ZH: dict[str, str] = {
    BADGE_GREEN: "口径全过",
    BADGE_YELLOW: "有待归因单边账",
    BADGE_RED: "有未确认差异",
    BADGE_GRAY: "本期未取到",
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# ─────────────────────────── 账期 ───────────────────────────


def periods(conn: sqlite3.Connection) -> list[str]:
    """库里见过的账期，倒序（新的在上）。

    来源是**三张表的并集**而不是只查 ``period_scans``：处置单可能先于
    扫描实例存在（批量处置的台账可以手工开单），只查扫描表会让那期
    账在账期下拉里整个消失 —— 人看着「我开过单的单子不见了」。
    """
    found: set[str] = set()
    for table in ("period_scans", "period_diff_items", LEDGER_DISCREPANCY_TABLE):
        if not _table_exists(conn, table):
            continue
        for row in conn.execute(f"SELECT DISTINCT period FROM {table} WHERE period<>''"):
            found.add(str(row[0]))
    if _table_exists(conn, BILL_TABLE):
        for row in conn.execute(f"SELECT DISTINCT period FROM {BILL_TABLE} WHERE period<>''"):
            found.add(str(row[0]))
    return sorted(found, reverse=True)


def period_overview(
    conn: sqlite3.Connection, period: str = ""
) -> dict[str, Any]:
    """账期概览。``period`` 为空时只回账期清单（顶栏下拉的数据源）。"""
    found = periods(conn)
    if not period:
        return {"periods": found, "overview": {}}
    if not _table_exists(conn, "period_scans"):
        return {"periods": found, "overview": _empty_overview(period)}

    from ..engine.period import derive_gate, load_scan

    scan = load_scan(conn, period)
    if scan is None:
        return {"periods": found, "overview": _empty_overview(period)}
    return {"periods": found, "overview": scan.to_dict()}


def _empty_overview(period: str) -> dict[str, Any]:
    return {
        "period": period,
        "status": "unscanned",
        "status_zh": "未扫描",
        "gate": None,
        "reason": f"账期 {period} 还没有扫描记录；先跑 reconq scan --period {period}",
        "channels_total": 0,
        "channels_ok": 0,
        "channels_failed": 0,
        "rows_ingested": 0,
        "checks_total": 0,
        "checks_failed": 0,
        "unconfirmed": 0,
        "pending_attribution": 0,
        "total_amount": "0",
        "started_at": "",
        "finished_at": "",
        "stale": False,
        "interrupted": False,
        "note": "",
    }


# ─────────────────────────── 左栏：渠道状态 ───────────────────────────


def channel_badge(
    *,
    status: str,
    checks_failed: int,
    pending_attribution: int,
    unconfirmed: int,
) -> str:
    """一路渠道的主态色。**不读账期是否存疑** —— 存疑是账期级的事，
    由 :func:`reconq.engine.period.derive_gate` 在顶栏出；左栏只回答
    「这一路自己怎么样」。"""
    if status != "checked":
        return BADGE_GRAY
    if unconfirmed > 0 or checks_failed > 0:
        return BADGE_RED
    if pending_attribution > 0:
        return BADGE_YELLOW
    return BADGE_GREEN


def channel_cards(conn: sqlite3.Connection, period: str) -> list[dict[str, Any]]:
    """六张渠道卡。**六张恒在**（取不到也给一张灰卡）。

    少给一张卡看起来更清爽，但那样「六路里少了哪一路」这件事就得靠人
    记得住六路的名字 —— 而这正是最常被漏掉的一类。灰卡上写清错误码，
    比「卡片不见了」可核对得多。
    """
    from ..align.bundle import CHANNEL_ZH
    from ..engine.period import (
        DIFF_TABLE,
        list_channel_results,
    )
    from ..sources.registry import CHANNELS

    rows = {}
    if _table_exists(conn, "period_channel_results"):
        for item in list_channel_results(conn, period):
            rows[item.channel] = item

    pending: dict[str, int] = {}
    unresolved: dict[str, int] = {}
    failed_paths: dict[str, int] = {}
    if _table_exists(conn, DIFF_TABLE):
        for r in conn.execute(
            f"SELECT channel, "
            f"SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS unresolved_n, "
            f"SUM(CASE WHEN needs_attribution=1 AND resolved=0 THEN 1 ELSE 0 END) AS attr_n, "
            f"SUM(CASE WHEN status='fail' AND resolved=0 THEN 1 ELSE 0 END) AS fail_n "
            f"FROM {DIFF_TABLE} WHERE period=? GROUP BY channel",
            (period,),
        ):
            pending[str(r["channel"])] = int(r["attr_n"] or 0)
            unresolved[str(r["channel"])] = int(r["unresolved_n"] or 0)
            failed_paths[str(r["channel"])] = int(r["fail_n"] or 0)

    cards: list[dict[str, Any]] = []
    for channel in CHANNELS:
        item = rows.get(channel)
        if item is None:
            cards.append(
                {
                    "channel": channel,
                    "channel_zh": CHANNEL_ZH.get(channel, channel),
                    "status": "not_fetched",
                    "badge": BADGE_GRAY,
                    "badge_zh": BADGE_ZH[BADGE_GRAY],
                    "row_count": 0,
                    "total_amount": "0.00",
                    "checks_total": 0,
                    "checks_failed": 0,
                    "dirty_rows": 0,
                    "error_code": "CHANNEL_NOT_SCANNED",
                    "detail": "本期未扫描这一路",
                }
            )
            continue
        badge = channel_badge(
            status=item.status,
            checks_failed=max(item.checks_failed, failed_paths.get(channel, 0)),
            pending_attribution=pending.get(channel, 0),
            unconfirmed=unresolved.get(channel, 0),
        )
        # ``error_code`` **恒有值**：取数阶段给的是缺凭据一类的原因码，
        # 而「这一路压根没取到账单」在扫描结果里只留了一句 detail、没留码。
        # 空码的灰卡看着一样，却在两个地方会出错 —— 前端没法按码分支
        # （「缺凭据」要提示去配环境变量、「没账单」要提示去要账单文件），
        # 而结论单附录二逐条列未取到渠道时，空码那一行只能靠中文 detail
        # 区分，机器读不了。
        error_code = item.error_code or (
            "" if item.status not in ("not_fetched", "failed") else "CHANNEL_NOT_FETCHED"
        )
        cards.append(
            {
                "channel": item.channel,
                "channel_zh": CHANNEL_ZH.get(item.channel, item.channel),
                "status": item.status,
                "badge": badge,
                "badge_zh": BADGE_ZH[badge],
                "row_count": item.row_count,
                "total_amount": item.total_amount,
                "checks_total": item.checks_total,
                "checks_failed": max(item.checks_failed, failed_paths.get(channel, 0)),
                "dirty_rows": item.dirty_rows,
                "error_code": error_code,
                "detail": item.detail,
            }
        )
    return cards


# ─────────────────────────── 中栏：差异台账 ───────────────────────────

#: 歧义类台账条目的 check_path 前缀。单号就藏在它后面。
#: 与 :mod:`reconq.align.aggregate` 的 ``_AMBIGUITY_PREFIX`` 同值但
#: **各自定义**：那边改了前缀语义（比如加一截版本号），这边跟着改就会
#: 把无关的口径差异也当成单边账去取草稿。这里只负责**取单号**，
#: 取不到就留空 —— 留空不等于报错，只等于「这条没有单号可显示」。
_AMBIGUITY_PREFIX = "taxonomy.ambiguity:"


def order_no_of(check_path: str) -> str:
    """从 check_path 里取渠道单号；不是歧义类条目则返回空串。"""
    if check_path.startswith(_AMBIGUITY_PREFIX):
        return check_path[len(_AMBIGUITY_PREFIX) :]
    return ""


def _sum_amounts(values: list[Optional[str]]) -> tuple[str, bool]:
    """合计。返回 ``(字符串, 是否可信)``。

    第二位为 False 表示台账里有非数值差额 —— 面板显示「合计不完整」
    而不是给一个看起来精确的错数。这与结算口径里「宁可空着也不要
    猜」的纪律同源。
    """
    total = Decimal("0")
    for raw in values:
        if raw in (None, ""):
            continue
        try:
            total += Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return "", False
    return f"{total:.2f}", True


def _cause_zh(code: Any) -> str:
    """成因码 → 中文名。字典外返回原码（不 KeyError、不空串）。

    空串会让面板上「成因」那一栏显示成空白，而空白与「没有成因」在
    签字页上长得一模一样。
    """
    from ..align.causes import CAUSES

    text = str(code or "")
    if not text:
        return ""
    entry = CAUSES.get(text)
    return entry.zh_name if entry else text


def _ledger_cause(disp: Any) -> str:
    """处置单上**人工确认过**的成因码。没有单子或单子上没写成因则空串。

    读的是 ``cause_draft_json`` 里那个 ``cause`` 字段 —— 那是开单时
    随草稿一起落的，与草稿表同源。**刻意不读经办写的 ``conclusion``**：
    那是一段自然语言（可能写「看着像渠道延迟，等对方回复」），把它当
    成因码会让分组按一段散文归堆。
    """
    if disp is None:
        return ""
    draft = getattr(disp, "cause_draft", None) or {}
    return str(draft.get("cause") or "")


def _rule_cause(check_path: str, ambiguity_kind: str = "") -> str:
    """规则形态映射给出的成因码（未经模型）。形态没登记映射则空串。

    走 :func:`reconq.align.baseline.shape_cause` 这一档，**不重跑完整
    基线**：完整基线要证据包，而证据包只在跑主路径时现构造。

    ``ambiguity_kind`` 一并传进去：``taxonomy.ambiguity:`` 是所有歧义
    结论共用的前缀，光看 ``check_path`` 分不出 ``weak_pending`` 与
    ``channel_only``，而这两种形态映射到的成因完全不同。
    """
    from ..align.baseline import shape_cause
    from .views import _kind_of

    kind = _kind_of({"check_path": check_path, "ambiguity_kind": ambiguity_kind})
    if not kind:
        return ""
    candidate = shape_cause(kind)
    return str(candidate["cause_code"]) if candidate else ""


def discrepancy_rows(
    conn: sqlite3.Connection, period: str, *, include_resolved: bool = False
) -> dict[str, Any]:
    """中栏数据源：差异行 + 处置状态 + 成因草稿。

    **成因草稿只读不判**（:mod:`reconq.store.attribution_store`）：草稿是
    模型或规则基线给的建议，处置状态是人给的结论，两者在右栏**分栏**。
    把它们合成一栏就看不出人有没有采信模型 —— 而这恰好是签字前最该
    看的一件事。
    """
    from ..align.bundle import CHANNEL_ZH, KIND_ZH
    from ..engine.period import DIFF_TABLE, list_diff_items
    from ..ledger.flow import list_discrepancies
    from ..store.attribution_store import list_drafts

    if not _table_exists(conn, DIFF_TABLE):
        return {"period": period, "rows": [], "total_amount": "0.00", "total_known": True, "counts": {}}

    items = list_diff_items(conn, period, include_resolved=include_resolved)
    ledger = (
        {d.key: d for d in list_discrepancies(conn, period=period)}
        if _table_exists(conn, LEDGER_DISCREPANCY_TABLE)
        else {}
    )

    drafts: dict[tuple[str, str], dict[str, Any]] = {}
    rejections: dict[str, list[dict[str, Any]]] = {}
    if _table_exists(conn, "attribution_drafts"):
        for row in list_drafts(conn, period):
            key = (str(row["channel"]), str(row["conclusion_key"]))
            # rank 1 是 Top-1；面板只显示 Top-1，低排名的在 CLI 评测里看。
            if key not in drafts:
                drafts[key] = row
    if _table_exists(conn, "attribution_rejections"):
        for row in conn.execute(
            "SELECT channel, conclusion_key, layer, code, detail FROM attribution_rejections "
            "WHERE period=? ORDER BY id",
            (period,),
        ):
            rejections.setdefault(str(row["conclusion_key"]), []).append(dict(row))

    rows: list[dict[str, Any]] = []
    for item in items:
        key = f"{item.period}|{item.channel}|{item.check_path}"
        disp = ledger.get(key)
        order_no = order_no_of(item.check_path)
        conclusion_key = f"{item.channel}:{item.period}:{order_no or item.check_path}"
        draft = drafts.get((item.channel, conclusion_key))
        rows.append(
            {
                "key": key,
                "period": item.period,
                "channel": item.channel,
                "channel_zh": CHANNEL_ZH.get(item.channel, item.channel),
                # 结论键原样带给前端：右栏「决策辅助」要按它去草稿表与
                # 先例留痕表取数，而那两处的键是 ``渠道:账期:单号``。
                "conclusion_key": conclusion_key,
                "check_path": item.check_path,
                "check_name": item.check_name or KIND_ZH.get(item.check_path, item.check_path),
                "order_no": order_no,
                "message": item.message,
                "diff_amount": item.diff_amount or "",
                "evidence_total": item.evidence_total,
                "source_file": item.source_file,
                "first_row_no": item.first_row_no,
                "ambiguity_kind": item.ambiguity_kind,
                "needs_attribution": item.needs_attribution,
                "resolved": item.resolved,
                "disposition": item.disposition,
                "ledger_status": disp.status if disp else "未开单",
                "ledger_status_zh": disp.status_zh if disp else "未开单",
                "ledger_version": disp.version if disp else -1,
                "conclusion": disp.conclusion if disp else "",
                "handler": disp.handler if disp else "",
                "reviewer": disp.reviewer if disp else "",
                "supervisor": disp.supervisor if disp else "",
                "requires_supervisor": disp.requires_supervisor if disp else False,
                "actions": (
                    action_options(disp.status, requires_supervisor=disp.requires_supervisor)
                    if disp
                    else []
                ),
                "cause_draft": (
                    {
                        "cause": draft["cause"],
                        "cause_zh": _cause_zh(draft["cause"]),
                        "confidence": draft["confidence_text"],
                        "explanation_zh": draft["explanation_zh"],
                        "evidence_refs": draft["evidence_refs"],
                        "model_name": draft["model_name"],
                    }
                    if draft
                    else None
                ),
                "rejections": rejections.get(conclusion_key, []),
                # —— 三档成因出处（Task 48）——
                # 面板按成因分组/筛选时靠这三个字段判「这条的成因是谁说的」。
                # 三个都空 = 口径差异（没有歧义形态可判），由
                # reconq.web.views 按 CAUSE_GROUP_OTHER 归组，**不**折进
                # undetermined —— 那会让「不用想」与「想了判不出」混成
                # 一组，而后者才是真正需要人推的情形。
                "ledger_cause": _ledger_cause(disp),
                "model_cause": str(draft["cause"]) if draft else "",
                "rule_cause": _rule_cause(item.check_path, item.ambiguity_kind),
            }
        )

    total, known = _sum_amounts([r["diff_amount"] for r in rows])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["ledger_status"]] = counts.get(r["ledger_status"], 0) + 1
    return {
        "period": period,
        "rows": rows,
        "total_amount": total,
        "total_known": known,
        "counts": counts,
    }


# ─────────────────────────── 右栏：处置动作 ───────────────────────────


def action_options(status: str, *, requires_supervisor: bool) -> list[dict[str, Any]]:
    """当前状态下**可以按**的按钮。禁用态由这里算，前端只渲染。

    直接用 :func:`reconq.ledger.models.required_role_for` 与
    :data:`~reconq.ledger.models.ACTION_SOURCES` 推，不另写一份
    ``if status == ...`` —— 状态机改了而按钮表没跟上，面板上就会出现
    一个「点了必报错」的按钮，而经办人正是最不该被这么坑的人。
    """
    from ..ledger.models import (
        ACTION_SOURCES,
        ACTION_ZH,
        LedgerAction,
        allowed_targets,
        required_role_for,
    )

    options: list[dict[str, Any]] = []
    for action in LedgerAction:
        if status not in ACTION_SOURCES[action.value]:
            continue
        roles = sorted(required_role_for(status, action.value))
        options.append(
            {
                "action": action.value,
                "label": ACTION_ZH[action.value],
                "roles": roles,
                "targets": allowed_targets(status),
            }
        )
    return options


def disposition_rows(conn: sqlite3.Connection, period: str) -> list[dict[str, Any]]:
    """右栏「已确认结论流」：已确认 / 已驳回的单子，最新的在前。

    读的是**处置单**而不是差异台账：结论单上签字那一栏取的是处置单，
    这里显示的就是签字后那一栏的样子。已确认与已驳回都留着（活单可撤回），
    但按更新时间倒序 —— 经办人刚点完的那一条要立刻出现在最上面。
    """
    from ..ledger.flow import list_discrepancies
    from ..ledger.models import STATUS_CONFIRMED, STATUS_REJECTED

    if not _table_exists(conn, LEDGER_DISCREPANCY_TABLE):
        return []
    rows = [
        d.to_dict()
        for d in list_discrepancies(conn, period=period)
        if d.status in (STATUS_CONFIRMED, STATUS_REJECTED)
    ]
    rows.sort(key=lambda r: (r.get("updated_at") or "", r.get("key") or ""), reverse=True)
    return rows
