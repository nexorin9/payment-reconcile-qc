"""FastAPI 应用与常驻服务入口（Task 47）。

形态（由 ``GITHUB_SEED.md``「本轮交付形态」锁定）：**常驻服务 + 浏览器
实时界面**。监听 ``127.0.0.1:8788``，只读账单库、只写处置单。

为什么 Web 层几乎不含业务判定
------------------------------
源产品的 ``contract verify`` 主路径（``github_ref/soda-core/soda-core/
src/soda_core/cli/cli.py:144``）是「读契约 → 逐 check 执行 → 打证据行 →
``exit_with_code()``」。本产品的对应链路已经全部落在
:mod:`reconq.engine` / :mod:`reconq.ledger` 里，Web 层只做三件事：

1. **投影** —— 把库里已有的东西按三栏的形状吐成 JSON；
2. **订阅** —— SSE 把变化推给浏览器；
3. **转发两个写入口** —— ``decide`` 与 ``review``，两者都只调
   :mod:`reconq.ledger.flow` 的白名单迁移，不自己判状态。

因此「非法状态返回 4xx」是**免费**的：Web 层不 catch 业务异常，
:func:`_error_response` 把 :class:`reconq.errors.ReconqError` 映射成
``{code, detail, ...}`` 与对应 HTTP 状态码。若哪天状态机放开了某个迁移，
面板会立刻跟着放开 —— 两处各写一份判据的年代已经过去了。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..errors import ReconqError
from ..ledger.models import Role
from . import evidence, state, views
from .events import DEFAULT_POLL_SECONDS, DbWatcher, EventBus, PanelEvent, bus_payload
from .state import _sum_amounts

logger = logging.getLogger("reconq.web")

#: 静态与模板目录。与本模块同包 —— 面板不是可替换的前端构建产物，
#: 它就是产品的一部分，跟着代码走。
_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(_HERE, "templates")
STATIC_DIR = os.path.join(_HERE, "static")

#: 默认监听地址。**锁 127.0.0.1**：面板要展示金额与单号，按 spec「不出网、
#: 院内本地部署」，绑 ``0.0.0.0`` 等于把整本账挂到院内网上。
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788

#: 业务错误码 → HTTP 状态码。**只在真有讲究的地方分档**：
#: 404 是「没有这条」，409 是「有但现在不能这么动」，400 是「参数不对」，
#: 422 是「参数合法但内容不合规」（如操作人写成 ``model``）。
#: 其余一律 400 —— 面板上要显示的是错误码本身，状态码只是给脚本看的。
_STATUS_BY_CODE: dict[str, int] = {
    "LEDGER_DISCREPANCY_NOT_FOUND": 404,
    "PERIOD_SCAN_NOT_INITIALIZED": 404,
    "LEDGER_VERSION_CONFLICT": 409,
    "LEDGER_TRANSITION_DENIED": 409,
    "LEDGER_ACTION_DENIED": 409,
    "LEDGER_ROLE_DENIED": 409,
    "LEDGER_ROLE_SEPARATION_DENIED": 409,
    "LEDGER_STATE_INVALID": 409,
    "LEDGER_ACTION_UNKNOWN": 400,
    "LEDGER_ROLE_INVALID": 400,
    "LEDGER_OPERATOR_INVALID": 422,
    "LEDGER_CONCLUSION_REQUIRED": 422,
    "LEDGER_CONCLUSION_TOO_LONG": 422,
    "LEDGER_VERSION_REQUIRED": 422,
    # 面板筛选入参（Task 48）。四个码都是「参数不对」而不是「有但不能动」，
    # 故一律 400 —— 与文件头「其余一律 400」的纪律一致，显式列出是为了
    # 让人一眼看出这四张 4xx 是**筛选**造成的，不是状态机造成的。
    "WEB_FILTER_STATE_UNKNOWN": 400,
    "WEB_FILTER_CHANNEL_UNKNOWN": 400,
    "WEB_FILTER_CAUSE_UNKNOWN": 400,
    "WEB_FILTER_AMOUNT_INVALID": 400,
}


def _error_response(exc: ReconqError) -> JSONResponse:
    """业务异常 → JSON 响应。**错误码原样带给前端**，不做归并。"""
    status_code = _STATUS_BY_CODE.get(exc.code, 400)
    body: dict[str, Any] = {"code": exc.code, "detail": exc.detail}
    if exc.channel:
        body["channel"] = exc.channel
    if exc.row_ref is not None:
        body["row_ref"] = str(exc.row_ref)
    return JSONResponse(status_code=status_code, content=body)


def create_app(
    db_path: str,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    start_watcher: bool = True,
) -> FastAPI:
    """构造面板应用。

    ``db_path`` 是**账单库**（与 ``reconq scan --db`` 同一个）。服务不拥有
    库：所有表都是幂等 ``init``，读不到的行显示为空态而不是报错 ——
    面板刚起来、账还没扫时，它应该是一块空看板，不是一屏异常。
    """

    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        watcher = DbWatcher(db_path, bus, poll_seconds=poll_seconds)
        if start_watcher:
            watcher.start()
        app.state.watcher = watcher
        try:
            yield
        finally:
            watcher.stop()

    app = FastAPI(
        title="支付对账数据质检系统 · 实时面板",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.db_path = db_path
    app.state.bus = bus
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # —— 业务异常 → JSON（全局）——
    #
    # 只在 :func:`_decide` 里 try/except 是不够的：筛选入参校验
    # （:func:`reconq.web.views.parse_filter`）与待办角色校验
    # （``_role_value``）也抛 :class:`ReconqError`，而它们发生在开连接
    # **之前**。逐个端点包 try 的话，漏掉一处就是一个裸栈 500 —— 而
    # 恰好是「筛选值写错了」这种最需要说清的场景。
    @app.exception_handler(ReconqError)
    async def _handle_reconq_error(_request: Request, exc: ReconqError) -> JSONResponse:
        return _error_response(exc)

    # —— 读端点（同步执行器跑 SQLite，不阻塞事件循环）——

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        from ..sources.registry import CHANNELS

        # 新版 Starlette 的签名是 ``(request, name, context)``，request 在前。
        # 传旧签名会在 :meth:`Environment.get_template` 上炸成
        # ``unhashable type: 'dict'`` —— 报错点离真正的原因隔了三层。
        return templates.TemplateResponse(
            request, "panel.html", {"db_path": db_path, "channels": list(CHANNELS)}
        )

    @app.get("/api/periods")
    def api_periods() -> dict[str, Any]:
        conn = _connect(db_path)
        try:
            found = state.periods(conn)
            return {"periods": found, "default": found[0] if found else ""}
        finally:
            conn.close()

    @app.get("/api/period/{period}/channels")
    def api_channels(period: str) -> dict[str, Any]:
        conn = _connect(db_path)
        try:
            return {"period": period, "channels": state.channel_cards(conn, period)}
        finally:
            conn.close()

    @app.get("/api/vocab")
    def api_vocab() -> dict[str, Any]:
        """筛选下拉的词表。**枚举只有服务端这一份**。

        前端硬编码一份状态名/成因码，就等于有了第二个事实来源：
        字典加了新成因而前端没跟上，那一档在面板上永远选不到，
        而经办人会以为「这类成因本期没出现」。
        """
        from ..align.causes import CAUSES
        from ..align.taxonomy import KIND_ZH
        from ..sources.registry import CHANNELS

        return {
            "states": list(views.FILTERABLE_STATES),
            "channels": list(CHANNELS),
            "causes": [
                {"code": code, "zh": entry.zh_name} for code, entry in sorted(CAUSES.items())
            ],
            "kinds": [
                # taxonomy.KIND_ZH 的值是 (中文名, 建议动作) 二元组，
                # 筛选下拉只要中文名那一半。
                {"code": code, "zh": pair[0], "hint": pair[1]}
                for code, pair in sorted(KIND_ZH.items())
                if code
            ],
            "roles": [
                {"code": role.value, "zh": role.zh}
                for role in (Role.HANDLER, Role.REVIEWER, Role.SUPERVISOR)
            ],
        }

    @app.get("/api/period/{period}/discrepancies")
    def api_discrepancies(
        period: str,
        include_resolved: bool = Query(False),
        cause_code: str = Query(""),
        channel: str = Query(""),
        row_state: str = Query("", alias="state"),
        ambiguity_kind: Optional[str] = Query(None),
        amount_min: Optional[str] = Query(None),
        amount_max: Optional[str] = Query(None),
        only_weak_match: Optional[str] = Query(None),
        only_ambiguous: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """中栏数据源。**筛选在投影之后**，且非法入参抛 4xx 而非返空列表。

        ``state`` 在函数签名里叫 ``row_state``：FastAPI 会拿形参名去
        撞 :class:`fastapi.Query` 的 ``state`` 之外的东西不算问题，但
        本地变量 ``state`` 与模块级 ``from . import state`` 同名会
        在闭包里被端点函数的形参遮蔽 —— 那一行 ``state.discrepancy_rows``
        就会去调一个字符串。``alias`` 保住对外的 query 名。
        """
        conn = _connect(db_path)
        try:
            body = state.discrepancy_rows(conn, period, include_resolved=include_resolved)
            body["overview"] = state.period_overview(conn, period)["overview"]
            body["settled"] = state.disposition_rows(conn, period)
            # 合计在**筛选后**重算，且用 state 那一处算法：不在 views 里
            # 另写一遍求和，否则「筛选后合计 0.00」与「合计不完整」会有
            # 两套口径。
            flt = views.parse_filter(
                cause_code=cause_code,
                channel=channel,
                state=row_state,
                ambiguity_kind=ambiguity_kind,
                amount_min=amount_min,
                amount_max=amount_max,
                only_weak_match=only_weak_match,
                only_ambiguous=only_ambiguous,
            )
            report = views.filter_rows_with_report(body["rows"], flt)
            total, known = _sum_amounts([r["diff_amount"] for r in report["rows"]])
            body["rows"] = report["rows"]
            body["total_amount"] = total
            body["total_known"] = known
            # 五个计数键**无条件补齐**，不读 ``body`` 上原有的值：
            # ``discrepancy_rows`` 在差异台账尚未建表时走的是一条提前
            # 返回的路径，那条路径不带这些键。前端读 ``total`` 时若拿到
            # undefined，显示的是「0 条」而不是「还没扫」—— 两者在
            # 排障时完全不同。
            body["matched"] = report["matched"]
            body["total"] = report["total"]
            body["unmatched"] = report["total"] - report["matched"]
            body["dropped_unknown_amount"] = report["dropped_unknown_amount"]
            body["dropped_amount_missing"] = report["dropped_amount_missing"]
            body["dropped_amount_unparsable"] = report["dropped_amount_unparsable"]
            body["filter"] = flt.to_dict()
            return body
        finally:
            conn.close()

    @app.get("/api/period/{period}/groups")
    def api_groups(period: str) -> dict[str, Any]:
        """按成因分组。**不带筛选参数**：分组是导航用的全量视图，
        带筛选会让「组内条数」与「筛出来的条数」两套数字并存。"""
        conn = _connect(db_path)
        try:
            rows = state.discrepancy_rows(conn, period)["rows"]
            groups = views.group_by_cause(rows)
            return {
                "period": period,
                "groups": groups,
                "group_count": len(groups),
                "ungrouped": sum(1 for g in groups if g["cause_code"] == ""),
            }
        finally:
            conn.close()

    @app.get("/api/period/{period}/todo")
    def api_todo(period: str, role: str = Query(Role.HANDLER.value)) -> dict[str, Any]:
        """某角色的待办聚合。角色非法 → 4xx（``LEDGER_ROLE_INVALID``）。"""
        conn = _connect(db_path)
        try:
            return views.todo_summary(conn, period, role)
        finally:
            conn.close()

    @app.get("/api/period/{period}/discrepancy/{key:path}/aid")
    def api_decision_aid(period: str, key: str) -> dict[str, Any]:
        """右栏「决策辅助」：四路来源并列 + 明示哪一路被采用。"""
        conn = _connect(db_path)
        try:
            body = state.discrepancy_rows(conn, period)
            for row in body["rows"]:
                if row["key"] == key:
                    return views.decision_aid(conn, period, row)
            raise ReconqError(
                "LEDGER_DISCREPANCY_NOT_FOUND",
                f"账期 {period} 的差异台账里没有键 {key!r}；"
                f"先跑一次 reconq scan --period {period} 再打开右栏",
            )
        finally:
            conn.close()

    @app.get("/api/period/{period}/disposition")
    def api_disposition(period: str) -> dict[str, Any]:
        conn = _connect(db_path)
        try:
            return {"period": period, "settled": state.disposition_rows(conn, period)}
        finally:
            conn.close()

    @app.get("/api/period/{period}/discrepancy/{key:path}/evidence")
    def api_evidence(period: str, key: str) -> dict[str, Any]:
        """右栏「展开证据」：候选行全表 + 快照引用 + 深链三出口。

        **与 :func:`api_decision_aid` 分开**：那个回答「成因是谁说的」，
        这个回答「据以判断的那几行是什么」。两者读不同的表（草稿/拒因/
        先例 vs 账单行/对齐快照），合成一个端点会让每次点开右栏都把
        账单行整期拉一遍。
        """
        conn = _connect(db_path)
        try:
            row = evidence.require_row(conn, period, key)
            return evidence.evidence_panel(conn, period, row)
        finally:
            conn.close()

    # —— SSE ——

    @app.get("/api/events")
    async def api_events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _sse_frames(bus, db_path, request=request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/open")
    def api_open(link: str = Query("")) -> RedirectResponse:
        """把 ``reconq://`` 深链 302 到面板页的 ``?period=…&diff=…``。

        **服务端解析，前端只做切账期**：深链形态由
        :func:`reconq.audit.wecom.parse_deep_link` 一处说了算；前端若
        另写一份正则，两处迟早对不上，而症状是「点开落到空页面」。

        解析不了就 4xx 并把原因带走 —— 用户点了个链接什么也没发生，
        界面不跳转也不报错，症状会被当成「链接坏了」。
        """
        from ..audit.wecom import parse_deep_link

        period, key = parse_deep_link(link)
        # 差异键含 ``|`` 与 ``:``，**必须百分号编码**才能安全地当 query
        # 值传：未编码的 ``|`` 在部分反代与前端 URLSearchParams 里会被
        # 截断，面板收到的 ``diff`` 少了后半段，症状是「点开落在一条
        # 不存在的差异上」—— 链接看起来完全正常。
        return RedirectResponse(
            url=f"/?period={urllib.parse.quote(period, safe='')}"
            f"&diff={urllib.parse.quote(key, safe='')}",
            status_code=302,
        )

    # —— 两个写入口：只转给 ledger 状态机 ——

    @app.get("/api/period/{period}/export")
    def api_export(
        period: str,
        fmt: str = Query("md"),
        operator: str = Query(""),
        note: str = Query(""),
    ) -> Any:
        """从面板导出周对账结论单（下载腿）。

        **与 :func:`reconq.export.conclusion.export_conclusion` 共用
        :func:`~reconq.export.conclusion.render_sheet`**：面板是财务科
        唯一的入口，「在面板上导出的那一份」与「命令行导出的那一份」
        必须是同一份版式 —— 两种结论单一起进同一个会签流程，版式分叉的
        代价是签字时没人知道签的是哪一版。

        指纹头走**响应头**：``X-Recoq-Fingerprint`` 让归档系统不必解析
        正文就能记下这份文件的结论集指纹。放正文里的那份已经在了，但正文
        是给人读的，机器读它得先认 Markdown 或 CSV。
        """
        from ..export.conclusion import render_sheet

        conn = _connect(db_path)
        try:
            text, doc, encoding = render_sheet(
                conn, period, fmt=fmt, exported_by=operator, note=note
            )
        finally:
            conn.close()
        payload = text.encode(encoding)
        fmt_text = str(fmt or "md").strip().lower()
        filename = f"{period}-conclusion.{fmt_text}"
        media = "text/markdown; charset=utf-8" if fmt_text == "md" else "text/csv; charset=utf-8"
        return Response(
            content=payload,
            media_type=media,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Recoq-Period": period,
                "X-Recoq-Format": fmt_text,
                "X-Recoq-Fingerprint": doc.fingerprint,
                "X-Recoq-Bytes": str(len(payload)),
                # 门禁没过也**照样给文件**，但在响应头明写：面板上那一条
                # 差异还没人确认，导出的单子上写明了，可归档时不看响应头的
                # 人会把这一版当成终版 —— 出口码该带的话由调用方自己判。
                "X-Recoq-Gate": doc.gate or "none",
                "X-Recoq-Needs-Reconfirm": ",".join(doc.contract_needs_reconfirm),
            },
        )

    @app.post("/api/discrepancy/{key}/decide")
    def api_decide(key: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        return _decide(db_path, bus, key, payload, action_override=None)

    @app.post("/api/discrepancy/{key}/review")
    def api_review(key: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        return _decide(db_path, bus, key, payload, action_override="review")

    return app


#: SSE 空闲多久补一条注释帧。15 秒：SSE 空闲久了会被反向代理按超时掐掉，
#: 一行注释就能把它撑住，而面板不会因此重连（前端只在 ``onerror`` 时兜底）。
SSE_KEEPALIVE_SECONDS = 15.0


class _AlwaysConnected:
    """没有 ``Request`` 时的替身（测试直接跑生成器用）。

    SSE 生成器只问一件事：「客户端还在吗」。真实请求走
    ``Request.is_disconnected``；单测里没有 ASGI 作用域，给一个恒真的
    替身即可 —— 断开路径由 ``aclose()`` 触发 ``finally`` 覆盖，那才是
    「订阅者一定会被摘掉」这条不变式的实际判据。
    """

    async def is_disconnected(self) -> bool:
        return False


#: 兼容旧名。测试里按这个名字构造替身。
_FakeRequest = _AlwaysConnected


async def _sse_frames(bus: EventBus, db_path: str, *, request: Any = None):
    """SSE 帧生成器。

    **抽成模块级函数而不是闭包**，是为了让它能被测试直接 ``__anext__()``：
    端点里的闭包只能经 HTTP 走到，而 ``TestClient`` 对无限流会一直读到
    流结束 —— 那条路在设计上就出不来。

    首个快照直接推出去：SSE 刚连上时前端若只能等下一次变化，那块空看板
    会一直空到有人点「扫描」为止。
    """
    probe = request if request is not None else _AlwaysConnected()
    queue = bus.subscribe()
    try:
        yield bus_payload(
            PanelEvent(
                kind="progress",
                payload={"period": "", "seq": 0, "hello": True, "db_path": db_path},
            )
        )
        while True:
            if await probe.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield bus_payload(event)
    finally:
        bus.unsubscribe(queue)


def _connect(db_path: str) -> sqlite3.Connection:
    """开一条只读用途的连接。

    **不设 ``check_same_thread=False``**：每个请求自己开自己关，
    连接不跨线程，FastAPI 的同步端点跑在线程池里也不会串。
    """
    from ..store.schema import connect, init_schema
    from ..engine.period import init_scan_schema
    from ..ledger.models import init_ledger_schema

    conn = connect(db_path)
    init_schema(conn)
    init_scan_schema(conn)
    init_ledger_schema(conn)
    return conn


def _decide(
    db_path: str,
    bus: EventBus,
    key: str,
    payload: dict[str, Any],
    *,
    action_override: Optional[str],
) -> JSONResponse:
    """处置入口。**全部经 :mod:`reconq.ledger.flow`，Web 层不判状态。**

    ``action_override="review"`` 时强制走**主管**那条路：存疑账期要
    主管签字这件事由服务端按 ``role=supervisor`` 落，不靠前端传什么
    就认什么 —— 前端传 ``role=reviewer`` 想把主管那一关自己签掉时，
    状态机会以 ``LEDGER_ROLE_DENIED`` 挡下。

    **人工边界**：``operator`` 必填且必须是自然人
    （:func:`reconq.ledger.flow.assert_human_operator` 挡掉
    ``model`` / ``rule`` / ``system`` / ``llm`` 等）。这条不是「建议」——
    模型草稿与规则基线都拿不到这个入口，模型不可达时的降级路径更不可能
    走到这里。
    """
    from ..ledger.flow import (
        approve,
        assert_human_operator,
        reject,
        require,
        submit,
        withdraw,
    )
    from ..ledger.models import Role

    # **整个函数体都在 try 里**：入参校验（操作人是不是人、决定词认不认得）
    # 抛的也是 :class:`ReconcileError`，而它们**先于**开连接。把它们放在
    # try 外面的话，「填了 model 当操作人」会变成一个裸栈 500 —— 恰好是
    # 最该给出「本产品拒绝由它推进处置状态」这句话的那条路径。
    conn: Optional[sqlite3.Connection] = None
    try:
        decision = str(payload.get("decision") or "").strip()
        operator = assert_human_operator(str(payload.get("operator") or ""))
        comment = str(payload.get("comment") or "").strip()
        role = str(payload.get("role") or "").strip()
        version = payload.get("expected_version")
        version = int(version) if version is not None else None

        if decision not in ("confirm", "reject", "withdraw"):
            raise ReconqError(
                "LEDGER_ACTION_UNKNOWN",
                f"未登记的处置决定 {decision!r}；面板上只有三种："
                f"confirm（确认/提交）、reject（驳回）、withdraw（撤回）",
            )
        if action_override == "review":
            role = Role.SUPERVISOR.value

        conn = _connect(db_path)
        item = require(conn, key)
        if decision == "confirm":
            if item.status == "待归因":
                # 经办提交处置结论：待归因 → 待确认。**必须写结论** ——
                # 没有结论的确认在结论单上是无源的。
                outcome = submit(
                    conn,
                    key,
                    comment,
                    operator=operator,
                    expected_version=version,
                    role=role or Role.HANDLER.value,
                    note=comment,
                )
            else:
                outcome = approve(
                    conn,
                    key,
                    operator=operator,
                    expected_version=version,
                    role=role or Role.REVIEWER.value,
                    note=comment,
                )
        elif decision == "reject":
            outcome = reject(
                conn,
                key,
                operator=operator,
                expected_version=version,
                role=role or Role.REVIEWER.value,
                note=comment,
            )
        else:
            outcome = withdraw(
                conn,
                key,
                operator=operator,
                expected_version=version,
                role=role or Role.HANDLER.value,
                note=comment,
            )
        if not outcome.ok:
            return JSONResponse(
                status_code=_STATUS_BY_CODE.get(outcome.error_code, 409),
                content={
                    "code": outcome.error_code,
                    "detail": outcome.detail,
                    "key": outcome.key,
                    "before": outcome.before,
                    "after": outcome.after,
                },
            )
        current = require(conn, key)
        body = {
            "ok": True,
            "key": key,
            "action": outcome.action,
            "before": outcome.before,
            "after": outcome.after,
            "version": current.version,
            "status_zh": current.status_zh,
            # 操作人与时刻**回给前端**：面板上要当场显示「谁在什么时候
            # 把它推到了哪一步」，而不是让人回日志里查。取的是行上**刚落库**
            # 的那个时间戳，与留痕表同源。
            "operator": operator,
            "at": _signoff_at(current, role or ""),
            "options": state.action_options(
                current.status, requires_supervisor=current.requires_supervisor
            ),
        }
    except ReconqError as exc:
        return _error_response(exc)
    finally:
        if conn is not None:
            conn.close()

    # 事件在**提交之后**发：先发会让页面显示出一个还没落库的状态。
    bus.publish(
        PanelEvent(
            kind="status",
            payload={
                "period": body["key"].split("|", 1)[0],
                "key": key,
                "status": body["after"],
                "status_zh": body["status_zh"],
                "version": body["version"],
                # 操作人与签字时刻**跟着事件一起走**，不能只留在响应体里：
                # 响应体只回给点按钮的那个人，而这条事件是推给**所有**
                # 开着面板的人的。少了这两个字段，旁边的审核人看到的是
                # 一行凭空变了状态、却不知道是谁改的 —— 而「谁签的这句话
                # 查不到了」正是本产品最不肯发生的那类故障。
                "operator": body["operator"],
                "at": body["at"],
                "seq": bus.next_seq(),
            },
        )
    )
    return JSONResponse(status_code=200, content=body)


def _signoff_at(item: Any, role: str) -> str:
    """刚落库的那一道签字的时刻。取不到就回空串，**不回 0 也不编一个**。

    按岗位取对应的时间列而不是回 ``updated_at``：``updated_at`` 是
    「这一行最后一次被改的时刻」，撤回也会刷新它，而撤回不是签字 ——
    拿它当签字时刻，面板上会出现「今天 10:00 主管签了字」，而真相是
    主管昨天签的、今早被人撤回了。
    """
    from ..ledger.models import Role

    column = {
        Role.HANDLER.value: "handler_at",
        Role.REVIEWER.value: "reviewer_at",
        Role.SUPERVISOR.value: "supervisor_at",
    }.get(role)
    if not column:
        return str(getattr(item, "updated_at", "") or "")
    return str(getattr(item, column, "") or "")


def run_server(
    db_path: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    log_level: str = "info",
) -> None:
    """起 uvicorn。**阻塞**直到进程被终止。

    启动时打印启动清单：监听地址、已配置的账期数、**缺哪些凭据位**。
    缺凭据清单放在启动横幅上，是因为「面板打开了但某一路永远是灰卡」
    这个现象最容易让人以为是 bug，而答案往往就是一个没设的环境变量。
    """
    import uvicorn

    from ..runlog import log_event
    from ..sources.registry import CHANNELS, probe_channel

    conn = _connect(db_path)
    try:
        found = state.periods(conn)
    finally:
        conn.close()

    missing: list[str] = []
    for channel in CHANNELS:
        probe = probe_channel(channel)
        for var in probe.get("missing_env_vars") or []:
            missing.append(f"{channel}:{var}")

    banner = [
        f"reconq serve · 监听 http://{host}:{port}",
        f"  账单库：{db_path}",
        f"  已扫账期：{'、'.join(found) if found else '（还没有，先跑 reconq scan --period 2026-W36）'}",
        f"  渠道通道：{'、'.join(CHANNELS)}",
        f"  缺失凭据：{'、'.join(missing) if missing else '无'}",
    ]
    for line in banner:
        # ``flush=True`` 不是装饰：服务跑在 ``nohup … &`` 后面时 stdout
        # 是全缓冲的，不刷这一下，**启动清单会一直躺在缓冲区里直到进程
        # 被 kill** —— 而「服务起没起来、缺哪个环境变量」恰恰是信息科
        # 最先要看的那几行。
        print(line, flush=True)
    log_event(
        "run.start",
        message="常驻服务启动：" + banner[0],
        host=host,
        port=port,
        db=db_path,
        periods=len(found),
        missing_credentials=missing,
    )

    uvicorn.run(
        create_app(db_path, poll_seconds=poll_seconds),
        host=host,
        port=port,
        log_level=log_level,
    )
