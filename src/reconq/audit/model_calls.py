"""模型调用审计与成本计量（Task 31）。

财务科的第二个问题：「这一期模型花了多少钱、慢在哪」
----------------------------------------------------
Task 28 把「模型不可达不抛到业务层」做实了，Task 30 把「端点挂了别把
40 秒拖成 40 分钟」做实了。但这两层回答的都是**故障**问题。日常问的其实
是另外三个：

* 这一期跑了几次模型调用？其中多少次是重试？
* 哪些差异命中了缓存（同一份证据、同一版提示词问过两次）？
* 花了多少 token、折算多少钱、耗时集中在哪几类差异上？

没有这一层，财务科能看到的只有事件链里几十条 ``model.call`` 的 JSON 行，
而「这一期一共多少钱」得自己把几十行加起来。**成本必须能被读出来，而不是
能被算出来** —— 科室要按这笔数去申请院内推理服务的额度。

五件事，各有独立判据
--------------------

================================ =========================================
机制                          回答的问题
================================ =========================================
:class:`ModelCallLedger`      逐次调用留痕（append-only + 链式指纹）
:func:`request_fingerprint`   「这两次问的是不是同一件事」（证据包字节的 sha256）
:class:`DraftCache`           同一 (指纹, 提示词版本, 模型) 不再重复调模型
:class:`PeriodUsage`          按账期聚合：次数 / 命中率 / 耗时 / token / 成本 / 降级
``reconq model-usage``        把上面那张聚合表打到终端
================================ =========================================

四条不可违反的边界
--------------------

1. **审计不存证据正文。** 落库的是「模型名 / 耗时 / token / 指纹 / 证据包
   体量」这类**统计量**，不是 prompt、不是证据包文本、不是模型返回的
   草稿正文。证据包里有金额、交易时间与费用类别；再抄一份进审计库，等于
   让同一个库里出现两处账单事实，而其中一处**没有**源文件行号可回查 ——
   有人拿着它去和渠道对账，对不上时查不到是哪一行错了。要复现某次调用，
   凭 :attr:`ModelCall.request_fingerprint` 去
   :mod:`reconq.align.snapshot` 的快照里取。

   **边界要说准，否则这条会变成自欺**：:attr:`ModelCall.conclusion_key`
   里**确实**含渠道单号（形如 ``align.channel_only#wechat:EX-9``），
   而且是**刻意**的 —— 它是差异台账的外键，没有它「这次花的钱对应哪条
   差异」就答不出来。同一个单号在 ``bill_rows`` 与 ``diff_items`` 里本来
   就有，审计表存它不构成「第二份账单事实」。**该挡的是证据正文**（那一整
   行账单），不是外键。
2. **审计只追加。** 与 :mod:`reconq.engine.resume` 的事件链同一套语义：
   每条记录带 ``prev_hash``，删掉中间任何一条，后面的指纹就接不上。
   **不提供 UPDATE / DELETE 接口** —— 想改一条调用记录，唯一正路是追加
   一条更正说明。
3. **缓存命中必须真的不发请求。** 命中时 :class:`ModelCall` 照样落一条
   （``cache_hit=True``、耗时 0、token 0），但网关一个请求都不发。
   不落这条记录会让「命中率」这个指标凭空消失 —— 而它恰恰是判断
   「这套缓存值不值得开着」的唯一依据。
4. **成本是估算，且必须能说清是怎么估的。** 院内自建端点通常没有公开
   单价，故单价**从环境变量读、缺位即不估**（:attr:`PeriodUsage.cost_text`
   为「未配置单价」而不是「0.00 元」）。把「没配单价」显示成「0 元」，
   财务科会以为这一期真的一次钱没花。

与源产品的关系
--------------
参考实现（``soda-core``）**没有这一层，也没有等价物**。源产品的
``contract verify`` 把逐 check 结果打到终端，进程退出即消失；``data-source
test`` 是一次性连通性探针。源产品假定「跑一次」，既没有第二次调用，也就没有
「第二次和第一次是不是同一件事」这个问题。

可复用的只有一条纪律：**摘要指纹（digest）当去重与复现的锚点**。源产品的
``ContractYaml`` 靠 ``name/version`` 标识一份契约，跨版本比对靠
``contract diff`` 人工看；本产品把同一件事落在**证据包字节**上 ——
对账归因的「同一件事」不是同一份契约，而是同一份证据，且提示词版本要一样。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional

from ..errors import ReconqError

logger = logging.getLogger("reconq.audit")

__all__ = [
    "AUDIT_ALLOWED_FIELDS",
    "DEFAULT_PROMPT_VERSION",
    "GENESIS_HASH",
    "MODEL_CALL_CACHE_TABLE",
    "MODEL_CALL_CORRECTION_TABLE",
    "MODEL_CALL_TABLE",
    "ChainVerdict",
    "DraftCache",
    "ModelCall",
    "ModelCallLedger",
    "ModelUsage",
    "PeriodUsage",
    "PRICE_ENV_VARS",
    "append_only_guard",
    "cache_key",
    "init_audit_schema",
    "make_call_id",
    "request_fingerprint",
    "usage_of_period",
]


# ─────────────────────────── 常量 ───────────────────────────


MODEL_CALL_TABLE = "model_calls"
MODEL_CALL_CACHE_TABLE = "model_call_cache"
MODEL_CALL_CORRECTION_TABLE = "model_call_corrections"

#: 链首前驱指纹。与 :data:`reconq.engine.resume.GENESIS_HASH` 同值同义：
#: 不写这一段全 0，链头和链尾长得一样就没法校验了。
GENESIS_HASH = "0" * 64

#: 提示词版本。**缓存键的一部分** —— 提示词一改，同一份证据包的答案就该
#: 重问；不把版本进键的话，改了提示词却继续吃旧答案，且没有任何痕迹能
#: 解释「为什么改完提示词输出没变」。
DEFAULT_PROMPT_VERSION = "v1"

#: 单价环境变量。**单位是元 / 千 token**，与主流推理服务的报价口径一致，
#: 且避开「是每 token 还是每千 token」这个最容易配错的点。
PRICE_ENV_VARS = {
    "prompt": "RECONQ_MODEL_PRICE_PROMPT_PER_1K",
    "completion": "RECONQ_MODEL_PRICE_COMPLETION_PER_1K",
}

#: 审计表允许落库的列。**白名单而非黑名单**：新增字段时若忘了考虑
#: 「这个字段会不会把证据正文带进来」，黑名单挡不住，白名单能挡住 ——
#: 不在白名单里的列根本不建。
AUDIT_ALLOWED_FIELDS = (
    "call_id",
    "period",
    "conclusion_key",
    "channel",
    "model",
    "request_fingerprint",
    "prompt_version",
    "schema_version",
    "elapsed_ms",
    "prompt_tokens",
    "completion_tokens",
    "attempts",
    "retried",
    "cache_hit",
    "status",
    "error_code",
    "bundle_chars",
    "bundle_lines",
    "at",
)

#: 单条审计记录的 ``status`` / ``error_code`` 允许的最大长度。
#:
#: 取 48 的依据是**本仓已登记的最长错误码** ``ATTRIBUTION_EVIDENCE_REF_INVALID``
#: （32 字符）留了一倍余量，同时短到能兜住「有人把错误详情整段塞进来」。
#: 放宽到 64 会让一句中文错误详情（40 多字）蒙混过关 —— 而那正是
#: 「把证据正文抄进审计库」最隐蔽的入口。
_MAX_CODE_LEN = 48


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


# ─────────────────────────── 指纹与缓存键 ───────────────────────────


def request_fingerprint(bundle_bytes: bytes | str) -> str:
    """证据包字节的 sha256（十六进制，取前 32 位）。

    **指纹的对象是证据包，不是整个请求体。** 请求体里还含 model 名、
    temperature、response_format —— 那些变了算「换了模型配置」，不是
    「换了一条差异」。两个层次的判据混在一个指纹里，「同一份证据问过
    几次」这个问题就再也答不清了。

    证据包是 :class:`~reconq.align.bundle.EvidenceBundle` 的 ``text``：
    同一份证据两次构造字节相同（构造时按 ``sort_keys`` 序列化），
    所以这个指纹是**可复现**的。
    """
    raw = bundle_bytes.encode("utf-8") if isinstance(bundle_bytes, str) else bundle_bytes
    return hashlib.sha256(raw).hexdigest()[:32]


def cache_key(
    request_fp: str, prompt_version: str, model: str, schema_version: str = ""
) -> str:
    """缓存键 = 指纹 + 提示词版本 + 模型名（+ Schema 版本，Task 34）。

    前三者缺一不可：缺指纹会把不同差异的答案串用；缺提示词版本会让改过
    提示词的运行继续吃旧答案；缺模型名会让换模型后仍命中旧模型的答案 ——
    而这三者任一发生，命中都是**静默的错误**（不报错、不降级，只是答案
    悄悄变了）。

    ``schema_version`` 同样进键，但**只在非空时进**：改了草稿 Schema
    （增删字段、收紧必填）之后，之前缓存的草稿 JSON 未必还能过新 Schema 的
    校验，继续命中等于拿旧格式的答案当新格式用。留空（老调用方 / 迁移前
    的记录）时键与加这一维之前逐字节相同，历史缓存不失效。
    """
    if schema_version:
        return f"{request_fp}:{prompt_version}:{schema_version}:{model}"
    return f"{request_fp}:{prompt_version}:{model}"


# ─────────────────────────── 逐次调用记录 ───────────────────────────


@dataclass(frozen=True)
class ModelCall:
    """一次模型调用的审计记录。**字段与落库列一一对应。**

    刻意**没有** ``prompt`` / ``response`` / ``detail`` 字段：模块头边界 1。
    想要「当时问了什么」，凭 :attr:`request_fingerprint` 去快照里取 ——
    那是唯一一处有证据行号可回查的地方。
    """

    call_id: str
    period: str
    conclusion_key: str
    model: str
    request_fingerprint: str
    prompt_version: str = DEFAULT_PROMPT_VERSION
    #: 归因草稿 Schema 版本（Task 34）。与 :attr:`prompt_version` **成对**：
    #: 只记提示词版本会出现「什么都没改但命中率掉了」——真相是 Schema
    #: 那一侧动过而它没留痕。空串 = 迁移前的老记录（见 :func:`init_audit_schema`）。
    schema_version: str = ""
    channel: str = ""
    elapsed_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 1
    retried: bool = False
    cache_hit: bool = False
    status: str = "ok"
    error_code: str = ""
    #: 证据包体量。**只存体量不存正文**（边界 1）。
    bundle_chars: int = 0
    bundle_lines: int = 0
    at: str = field(default_factory=_now)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_key(self) -> str:
        return cache_key(
            self.request_fingerprint, self.prompt_version, self.model, self.schema_version
        )

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in AUDIT_ALLOWED_FIELDS}

    @classmethod
    def from_gateway_call(
        cls,
        call: Any,
        *,
        period: str,
        request_fingerprint: str = "",
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        schema_version: str = "",
        cache_hit: bool = False,
    ) -> "ModelCall":
        """从 :class:`~reconq.gateway.model_gateway.GatewayCall` 造审计记录。

        **入参类型用鸭子类型而不是 import**：本模块**不 import 网关包**。
        理由与 :mod:`reconq.run_summary` 不 import 归因包同源 —— 审计是
        「回答花了多少钱」的地方，它不该把「读一次审计」绑到「装了模型
        网关」上。代价是字段名靠约定，两侧都有测试钉住。

        :attr:`GatewayCall.bundle` 里带的是证据包侧审计元数据，本函数只取
        其中两个体量字段（``char_count`` / ``line_count``），**其余一律不带**
        —— 那里面将来可能加新字段，而审计表不该跟着长。

        ``call_id`` **不在这里造**：它由 :class:`ModelCallLedger` 按账期内
        序号分配（见 :func:`make_call_id`），此处留空串。
        """
        bundle = dict(getattr(call, "bundle", None) or {})
        return cls(
            call_id="",
            period=period,
            conclusion_key=str(bundle.get("conclusion_key", "")),
            channel=str(bundle.get("channel", "")),
            model=str(getattr(call, "model", "")),
            # 网关侧的 ``request_digest`` 是**整个请求体**的指纹（含 model
            # 名与 response_format）。审计要的是「同一份证据」，故优先用
            # 显式传入的证据包指纹；没传才回落到请求体指纹，并在调用点
            # 保证回落路径不参与缓存判定。
            request_fingerprint=request_fingerprint
            or str(getattr(call, "request_digest", "")),
            prompt_version=prompt_version,
            schema_version=schema_version,
            elapsed_ms=float(getattr(call, "elapsed_ms", 0.0) or 0.0),
            prompt_tokens=int(getattr(call, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(call, "completion_tokens", 0) or 0),
            attempts=int(getattr(call, "attempts", 1) or 1),
            retried=bool(getattr(call, "retried", False)),
            cache_hit=cache_hit,
            status="ok" if getattr(call, "ok", False) else "degraded",
            error_code=str(getattr(call, "error_code", "") or ""),
            bundle_chars=int(bundle.get("char_count", 0) or 0),
            bundle_lines=int(bundle.get("line_count", 0) or 0),
        )


def make_call_id(period: str, seq: int) -> str:
    """调用 id = 账期 + 账期内序号。稳定、可复算、不用 uuid。

    用序号而不是 uuid 是为了**人工回查**：信息科报「2026-W36 第 17 次调用」
    时，按序号去库里数得出来；uuid 只能整条复制粘贴。
    """
    return f"{period}-{seq:06d}"


# ─────────────────────────── 表结构 ───────────────────────────

#: 审计表。**append-only**：``prev_hash`` / ``hash`` 两列构成链，
#: 删掉中间任何一条，后面的链就接不上（:func:`verify_chain` 能检出）。
#:
#: 链按**账期**分：跨账期串一条链的话，任一期的写入失败都会让后面所有账期
#: 的校验失败，而排障要回答的恰恰是「这一期」。
_MODEL_CALL_DDL = f"""
CREATE TABLE IF NOT EXISTS {MODEL_CALL_TABLE} (
    call_id             TEXT    PRIMARY KEY,
    period              TEXT    NOT NULL,
    seq                 INTEGER NOT NULL,
    conclusion_key      TEXT    NOT NULL DEFAULT '',
    channel             TEXT    NOT NULL DEFAULT '',
    model               TEXT    NOT NULL DEFAULT '',
    request_fingerprint TEXT    NOT NULL DEFAULT '',
    prompt_version      TEXT    NOT NULL DEFAULT '{DEFAULT_PROMPT_VERSION}',
    schema_version      TEXT    NOT NULL DEFAULT '',
    elapsed_ms          REAL    NOT NULL DEFAULT 0,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    attempts            INTEGER NOT NULL DEFAULT 1,
    retried             INTEGER NOT NULL DEFAULT 0,
    cache_hit           INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'ok',
    error_code          TEXT    NOT NULL DEFAULT '',
    bundle_chars        INTEGER NOT NULL DEFAULT 0,
    bundle_lines        INTEGER NOT NULL DEFAULT 0,
    at                  TEXT    NOT NULL DEFAULT '',
    prev_hash           TEXT    NOT NULL DEFAULT '',
    hash                TEXT    NOT NULL DEFAULT '',
    UNIQUE (period, seq)
)
"""

_MODEL_CALL_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_model_call_period "
    f"ON {MODEL_CALL_TABLE} (period, seq)",
    # 复现入口：「这条差异这一期被问过几次」。命中缓存的判定不走这个索引
    # （走缓存表的唯一键），这里服务的是人工回查。
    f"CREATE INDEX IF NOT EXISTS ix_model_call_conclusion "
    f"ON {MODEL_CALL_TABLE} (period, conclusion_key)",
    f"CREATE INDEX IF NOT EXISTS ix_model_call_fingerprint "
    f"ON {MODEL_CALL_TABLE} (request_fingerprint)",
)

#: 缓存表。**只存草稿 JSON，不存 prompt**（边界 1）。
#:
#: 存草稿而非结论：草稿还要过 :mod:`reconq.gateway.schema` 的六层校验才
#: 能落 :mod:`reconq.store.attribution_store`，而缓存里的东西是**未校验的
#: 原始 JSON 文本**。故列名写 ``draft_json`` 而不是 ``result_json`` ——
#: 让读代码的人一眼知道它不是可以直接进结论单的东西。
_MODEL_CALL_CACHE_DDL = f"""
CREATE TABLE IF NOT EXISTS {MODEL_CALL_CACHE_TABLE} (
    cache_key      TEXT    PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    prompt_version TEXT    NOT NULL,
    schema_version TEXT    NOT NULL DEFAULT '',
    model          TEXT    NOT NULL,
    draft_json     TEXT    NOT NULL,
    content_chars  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT '',
    hits           INTEGER NOT NULL DEFAULT 0
)
"""

_MODEL_CALL_CACHE_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_model_call_cache_fp "
    f"ON {MODEL_CALL_CACHE_TABLE} (request_fingerprint, prompt_version, model)",
)

#: 缓存更正表。append-only，且**只记体量与理由不记草稿正文** ——
#: 它是「谁动过缓存」的账本，不是第二个答案库。
_MODEL_CALL_CORRECTION_DDL = f"""
CREATE TABLE IF NOT EXISTS {MODEL_CALL_CORRECTION_TABLE} (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key          TEXT    NOT NULL,
    request_fingerprint TEXT   NOT NULL,
    prompt_version     TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    old_chars          INTEGER NOT NULL DEFAULT 0,
    new_chars          INTEGER NOT NULL DEFAULT 0,
    reason             TEXT    NOT NULL,
    at                 TEXT    NOT NULL DEFAULT ''
)
"""


def init_audit_schema(conn: sqlite3.Connection) -> None:
    """建审计表、缓存表、更正表与索引。幂等。

    **不装 append-only 触发器** —— 触发器由 :func:`append_only_guard`
    单独提供。分开是因为「装表」与「上锁」是两种决定：排障要读一个
    被刻意改动过的库来复现问题时，能先建表不开锁。
    """
    conn.execute(_MODEL_CALL_DDL)
    conn.execute(_MODEL_CALL_CACHE_DDL)
    _migrate_model_call_columns(conn)
    for statement in _MODEL_CALL_INDEXES:
        conn.execute(statement)
    for statement in _MODEL_CALL_CACHE_INDEXES:
        conn.execute(statement)
    conn.execute(_MODEL_CALL_CORRECTION_DDL)


#: 建表语句里后加的列。``CREATE TABLE IF NOT EXISTS`` **不会**给已存在的表
#: 补列，所以老库升级后这张表仍然缺它们 —— 缺了的表现是 ``INSERT`` 报
#: "no such column"，而这个错误会在**扫描那一期**才冒出来。与
#: :mod:`reconq.engine.period` 的 ``_ensure_columns`` 同一处置。
_MIGRATED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (MODEL_CALL_TABLE, "schema_version", "TEXT NOT NULL DEFAULT ''"),
    (MODEL_CALL_CACHE_TABLE, "schema_version", "TEXT NOT NULL DEFAULT ''"),
)


def _migrate_model_call_columns(conn: sqlite3.Connection) -> None:
    """给老库补列。幂等；已有列时一个字节都不动。"""
    for table, name, ddl in _MIGRATED_COLUMNS:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if name in present:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


# ─────────────────────────── 链式审计 ───────────────────────────


def _call_digest(payload: Mapping[str, Any]) -> str:
    """一条审计记录的指纹。``prev_hash`` 也算进去（链式）。

    与 :func:`reconq.engine.resume._event_digest` 同一形状：只把**已知
    字段**拼进去，不 ``json.dumps`` 整个 dict —— 后者会让「将来给
    ``ModelCall`` 加一个展示用字段」这件事**静默改掉历史指纹**，把一条
    没被动过的链报成篡改。

    ``schema_version`` 是唯一的例外，且是**有方向的例外**：Task 34 加这一列
    时，老库里已有按旧字段集算过指纹的记录，因此该键**仅在非空时参与**。
    空串（迁移前的老记录、或当时压根没跑归因的调用）按旧字段集重算，
    历史链不会因为加了一列而整条报红 —— 那正是「加字段」最该避免的
    副作用：让所有存量记录看起来都被人动过。
    """
    fields = {
        "call_id": payload["call_id"],
        "period": payload["period"],
        "seq": payload["seq"],
        "conclusion_key": payload["conclusion_key"],
        "channel": payload["channel"],
        "model": payload["model"],
        "request_fingerprint": payload["request_fingerprint"],
        "prompt_version": payload["prompt_version"],
        "elapsed_ms": payload["elapsed_ms"],
        "prompt_tokens": payload["prompt_tokens"],
        "completion_tokens": payload["completion_tokens"],
        "attempts": payload["attempts"],
        "retried": payload["retried"],
        "cache_hit": payload["cache_hit"],
        "status": payload["status"],
        "error_code": payload["error_code"],
        "bundle_chars": payload["bundle_chars"],
        "bundle_lines": payload["bundle_lines"],
        "at": payload["at"],
        "prev_hash": payload["prev_hash"],
    }
    schema_version = str(payload.get("schema_version", "") or "")
    if schema_version:
        fields["schema_version"] = schema_version
    raw = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ModelCallLedger:
    """逐次模型调用的 append-only 台账。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # —— 写 ————————————————————————————————————————

    def record(self, call: ModelCall) -> ModelCall:
        """追加一条调用记录，返回**落盘后**的那条（含序号与链式指纹）。

        序号与前驱指纹由本层决定，调用方给的 :attr:`ModelCall.call_id`
        会被覆盖 —— 让两个来源都能定「这是第几条」的话，重放一次就会
        撞主键，而撞主键的表现是「第二次记不进去了」，比报错更难查。
        """
        if not call.period:
            raise ReconqError(
                "MODEL_AUDIT_SCHEMA_MISSING",
                "调用记录必须带账期（period）。审计链按账期分，"
                "缺账期的记录挂不到任何一条链上，"
                "「这一期花了多少钱」会算少一半",
            )
        # 受控词表里的短码不该长。长度异常基本只有一种成因：有人把
        # 错误详情整段塞进了 error_code —— 那是把证据正文抄进审计库的
        # 最隐蔽的入口（模块头边界 1），在这里挡住。
        for name in ("status", "error_code"):
            value = str(getattr(call, name) or "")
            if len(value) > _MAX_CODE_LEN:
                raise ReconqError(
                    "MODEL_AUDIT_FIELD_INVALID",
                    f"{name} 长度 {len(value)} 超过 {_MAX_CODE_LEN}：{value[:60]}…。"
                    "审计表只接受受控词表里的短码，不接受错误详情正文 —— "
                    "详情进事件链（reconq 的 JSON 日志会脱敏），"
                    "审计表只留码",
                )
        row = self._conn.execute(
            f"SELECT seq, hash FROM {MODEL_CALL_TABLE} WHERE period = ? "
            "ORDER BY seq DESC LIMIT 1",
            (call.period,),
        ).fetchone()
        seq = (row["seq"] + 1) if row else 1
        prev_hash = (row["hash"] or GENESIS_HASH) if row else GENESIS_HASH
        payload = {
            **call.to_dict(),
            "call_id": make_call_id(call.period, seq),
            "seq": seq,
            "prev_hash": prev_hash,
        }
        digest = _call_digest(payload)
        self._conn.execute(
            f"INSERT INTO {MODEL_CALL_TABLE} ("
            "call_id, period, seq, conclusion_key, channel, model, "
            "request_fingerprint, prompt_version, schema_version, elapsed_ms, "
            "prompt_tokens, completion_tokens, attempts, retried, cache_hit, "
            "status, error_code, bundle_chars, bundle_lines, at, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payload["call_id"],
                payload["period"],
                seq,
                payload["conclusion_key"],
                payload["channel"],
                payload["model"],
                payload["request_fingerprint"],
                payload["prompt_version"],
                payload.get("schema_version", "") or "",
                payload["elapsed_ms"],
                payload["prompt_tokens"],
                payload["completion_tokens"],
                payload["attempts"],
                1 if payload["retried"] else 0,
                1 if payload["cache_hit"] else 0,
                payload["status"],
                payload["error_code"],
                payload["bundle_chars"],
                payload["bundle_lines"],
                payload["at"],
                prev_hash,
                digest,
            ),
        )
        return ModelCall(**{name: payload[name] for name in AUDIT_ALLOWED_FIELDS})

    # —— 读 ————————————————————————————————————————

    def list_calls(
        self, period: str, *, conclusion_key: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """按账期读回调用记录（正序）。"""
        sql = f"SELECT * FROM {MODEL_CALL_TABLE} WHERE period = ?"
        params: list[Any] = [period]
        if conclusion_key is not None:
            sql += " AND conclusion_key = ?"
            params.append(conclusion_key)
        sql += " ORDER BY seq"
        return [dict(row) for row in self._conn.execute(sql, params)]

    def verify_chain(self, period: str) -> "ChainVerdict":
        """重算该账期整条链的指纹。

        返回的 :class:`ChainVerdict` 说三件事：链**是否**连得上、第一条
        对不上的序号、那条的实际指纹与重算指纹。**不抛异常** —— 校验的
        用途是让人看见，而不是让程序崩；调用方（``reconq model-usage``）
        看到 ``ok=False`` 时返回非零退出码。
        """
        rows = self._conn.execute(
            f"SELECT * FROM {MODEL_CALL_TABLE} WHERE period = ? ORDER BY seq",
            (period,),
        ).fetchall()
        prev = GENESIS_HASH
        expected_seq = 1
        for row in rows:
            if row["seq"] != expected_seq:
                return ChainVerdict(
                    ok=False,
                    checked=expected_seq - 1,
                    broken_seq=int(row["seq"]),
                    reason=f"序号跳号：期望 {expected_seq}，实际 {row['seq']}"
                    "（中间有记录被删除或插入）",
                    expected_hash="",
                    actual_hash=row["hash"],
                )
            payload = {
                "call_id": row["call_id"],
                "period": row["period"],
                "seq": row["seq"],
                "conclusion_key": row["conclusion_key"],
                "channel": row["channel"],
                "model": row["model"],
                "request_fingerprint": row["request_fingerprint"],
                "prompt_version": row["prompt_version"],
                "schema_version": row["schema_version"] or "",
                "elapsed_ms": row["elapsed_ms"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "attempts": row["attempts"],
                "retried": bool(row["retried"]),
                "cache_hit": bool(row["cache_hit"]),
                "status": row["status"],
                "error_code": row["error_code"],
                "bundle_chars": row["bundle_chars"],
                "bundle_lines": row["bundle_lines"],
                "at": row["at"],
                "prev_hash": prev,
            }
            recomputed = _call_digest(payload)
            if row["prev_hash"] != prev:
                return ChainVerdict(
                    ok=False,
                    checked=expected_seq - 1,
                    broken_seq=int(row["seq"]),
                    reason=f"前驱指纹对不上：期望 {prev[:12]}…，"
                    f"实际 {str(row['prev_hash'])[:12]}…",
                    expected_hash=prev,
                    actual_hash=str(row["prev_hash"]),
                )
            if recomputed != row["hash"]:
                return ChainVerdict(
                    ok=False,
                    checked=expected_seq - 1,
                    broken_seq=int(row["seq"]),
                    reason="本条指纹与重算值不一致（记录内容被改过）",
                    expected_hash=recomputed,
                    actual_hash=str(row["hash"]),
                )
            prev = str(row["hash"])
            expected_seq += 1
        return ChainVerdict(ok=True, checked=len(rows), broken_seq=0, reason="链完整", expected_hash=prev, actual_hash=prev)


@dataclass(frozen=True)
class ChainVerdict:
    """链式校验结论。"""

    ok: bool
    checked: int
    broken_seq: int
    reason: str
    expected_hash: str
    actual_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "broken_seq": self.broken_seq or None,
            "reason": self.reason,
            "expected_hash": self.expected_hash[:16],
            "actual_hash": self.actual_hash[:16],
        }


def append_only_guard(conn: sqlite3.Connection) -> None:
    """装上 append-only 触发器：审计表与更正表**拒绝** UPDATE 与 DELETE。

    指纹能**检出**篡改，触发器能**挡住**。两者都要：检出意味着事后才知道
    （财务科看到「本周对账成本被改过」时已经晚了），挡住意味着库里根本
    不会有被动过的行。

    **更正表也上锁**：它回答的是「谁动过缓存」。这一张被改掉的话，
    「谁改的」就永远答不上来了 —— 那正是它存在的全部理由。

    SQLite 的触发器用 ``RAISE(ABORT, ...)``，错误信息里带表名 ——
    信息科拿一条 sqlite3 报错去找人时，第一行就能定位到是哪张表。
    """
    for table, why_update, why_delete in (
        (
            MODEL_CALL_TABLE,
            "改一条调用记录的唯一正路是追加一条更正说明",
            "删一条会让后续记录的链式指纹全部接不上",
        ),
        (
            MODEL_CALL_CORRECTION_TABLE,
            "更正记录本身也要 append-only，否则「谁改的」永远答不上来",
            "删除更正记录等于把改动作从账上抹掉",
        ),
    ):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} 是 append-only 审计表：'
                    || '{why_update}');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} 是 append-only 审计表：'
                    || '{why_delete}');
            END
            """
        )


# ─────────────────────────── 结果缓存 ───────────────────────────


class DraftCache:
    """同一 (指纹, 提示词版本, 模型) 的结果缓存。

    **命中时不发任何请求**（模块头边界 3）。命中仍要落一条审计记录
    （``cache_hit=True``、耗时 0、token 0），否则命中率这个指标会随
    「有没有记审计」而变，而它恰恰是判断这套缓存值不值得开着的唯一依据。

    :meth:`lookup` 返回的是**未校验的草稿 JSON 文本**。调用方必须照常
    走 :mod:`reconq.gateway.schema` 的六层校验 —— 缓存不是信任来源，
    存进去的是上一次模型的原样输出，那次可能过了也可能没过。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def lookup(
        self,
        request_fp: str,
        prompt_version: str,
        model: str,
        schema_version: str = "",
    ) -> Optional[str]:
        """命中返回草稿 JSON 文本，未命中返回 ``None``。

        **命中计数在此自增**（``hits = hits + 1``）。这张表是 append-only
        之外的**唯一可写字段**：``hits`` 不是审计事实的一部分（改它不改变
        「调了几次」的结论），而它让「这条答案被复用了几次」可回答。
        """
        key = cache_key(request_fp, prompt_version, model, schema_version)
        row = self._conn.execute(
            f"SELECT draft_json FROM {MODEL_CALL_CACHE_TABLE} WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            f"UPDATE {MODEL_CALL_CACHE_TABLE} SET hits = hits + 1 WHERE cache_key = ?",
            (key,),
        )
        return str(row["draft_json"])

    def put(
        self,
        request_fp: str,
        prompt_version: str,
        model: str,
        draft_json: str,
        *,
        schema_version: str = "",
        correction_reason: str = "",
    ) -> str:
        """写入一条缓存，返回 ``"stored"`` / ``"kept"`` / ``"corrected"``。

        **默认不覆盖**（``INSERT ... ON CONFLICT DO NOTHING``）。同一份证据
        问出两个不同答案时，保留**第一次**那个：后一次可能是提示词刚改过、
        也可能是端点那次抽风，而这两者都无法从库里分辨 —— 保留先到的，
        至少让「不一致」这件事被 :meth:`corrections` 记下来。

        ``correction_reason`` 非空时**覆盖并留痕**：往
        :data:`MODEL_CALL_CORRECTION_TABLE` 追加一条，写明谁在什么时候把
        哪条缓存换成了什么。人工改自动写入的数据必须留痕，否则下一次
        「答案为什么变了」没人答得出。

        **刻意要求给出理由**：没有 ``correction_reason`` 的覆盖会被拒。
        「顺手改一下」是缓存被污染最常见的成因。
        """
        key = cache_key(request_fp, prompt_version, model, schema_version)
        existing = self._conn.execute(
            f"SELECT draft_json FROM {MODEL_CALL_CACHE_TABLE} WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if existing is not None and str(existing["draft_json"]) == draft_json:
            return "kept"
        if existing is not None and not correction_reason.strip():
            raise ReconqError(
                "CACHE_OVERWRITE_DENIED",
                f"缓存键 {key[:16]}… 已有内容，且本次草稿与之不同。"
                "覆盖缓存必须显式给出 correction_reason —— "
                "「顺手改一下」是缓存被污染最常见的成因，"
                "而缓存一旦被污染，同一期的每一条差异都会跟着给出错答案",
            )
        if existing is not None:
            self._conn.execute(
                f"UPDATE {MODEL_CALL_CACHE_TABLE} "
                "SET draft_json = ?, content_chars = ?, created_at = ? "
                "WHERE cache_key = ?",
                (draft_json, len(draft_json), _now(), key),
            )
            self._conn.execute(
                f"INSERT INTO {MODEL_CALL_CORRECTION_TABLE} "
                "(cache_key, request_fingerprint, prompt_version, model, "
                "old_chars, new_chars, reason, at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    key,
                    request_fp,
                    prompt_version,
                    model,
                    len(str(existing["draft_json"])),
                    len(draft_json),
                    correction_reason.strip(),
                    _now(),
                ),
            )
            return "corrected"
        self._conn.execute(
            f"INSERT INTO {MODEL_CALL_CACHE_TABLE} ("
            "cache_key, request_fingerprint, prompt_version, schema_version, "
            "model, draft_json, content_chars, created_at, hits) "
            "VALUES (?,?,?,?,?,?,?,?,0) ON CONFLICT (cache_key) DO NOTHING",
            (
                key,
                request_fp,
                prompt_version,
                schema_version or "",
                model,
                draft_json,
                len(draft_json),
                _now(),
            ),
        )
        return "stored"

    def corrections(self) -> list[dict[str, Any]]:
        """列出被显式更正过的缓存（正常路径下恒为空）。

        同样 append-only：更正记录本身也不许改，否则「谁改的」这一问就
        永远答不上来了。
        """
        return [
            dict(row)
            for row in self._conn.execute(
                f"SELECT * FROM {MODEL_CALL_CORRECTION_TABLE} ORDER BY id"
            )
        ]

    def stats(self) -> dict[str, int]:
        return {
            "entries": int(
                self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {MODEL_CALL_CACHE_TABLE}"
                ).fetchone()["n"]
            ),
            "hits": int(
                self._conn.execute(
                    f"SELECT COALESCE(SUM(hits), 0) AS n FROM {MODEL_CALL_CACHE_TABLE}"
                ).fetchone()["n"]
            ),
        }


# ─────────────────────────── 按账期聚合 ───────────────────────────


@dataclass(frozen=True)
class ModelUsage:
    """单条调用在成本口径下的贡献。聚合时逐条相加（:func:`usage_of_period`
    真走这条路径，不另写一遍加法）。

    ``priced=False`` 表示**这一条没参与计价**（单价未配置）。它与
    ``cost=0`` 是两件事：前者是「不知道」，后者是「不要钱」。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: Decimal = Decimal("0")
    priced: bool = True

    def __add__(self, other: "ModelUsage") -> "ModelUsage":
        # 两条里只要有一条没定价，整体就不是「可定价」的 ——
        # 把没定价的那条当 0 元加进去，得到的总数是假的。
        return ModelUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost=self.cost + other.cost,
            priced=self.priced and other.priced,
        )

    @classmethod
    def of_call(
        cls,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        prompt_price: Optional[Decimal],
        completion_price: Optional[Decimal],
    ) -> "ModelUsage":
        """单条调用的成本。**单价任一缺位即 ``priced=False``**。

        刻意不做「只配了提示词单价就按提示词算」的半截计价：那会让
        :attr:`PeriodUsage.cost_text` 显示一个看起来完整的数字，而它漏掉了
        生成侧 —— 归因任务的生成 token 往往不比提示词少。
        """
        if prompt_price is None or completion_price is None:
            return cls(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=Decimal("0"),
                priced=False,
            )
        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=(
                Decimal(prompt_tokens) / Decimal(1000) * prompt_price
                + Decimal(completion_tokens) / Decimal(1000) * completion_price
            ),
            priced=True,
        )


@dataclass(frozen=True)
class PeriodUsage:
    """一个账期的成本与命中聚合。**面板底部与 ``model-usage`` 共用这一份。**"""

    period: str
    calls: int = 0
    cache_hits: int = 0
    degraded: int = 0
    retried: int = 0
    total_elapsed_ms: float = 0.0
    max_elapsed_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    distinct_fingerprints: int = 0
    models: tuple[str, ...] = ()
    prompt_versions: tuple[str, ...] = ()
    errors: dict[str, int] = field(default_factory=dict)
    #: 单价是否配齐。**未配齐时** :attr:`cost_text` 是「未配置单价」而不是
    #: 「0.00 元」（边界 4）。
    priced: bool = True
    cost: Decimal = Decimal("0")
    chain: Optional[ChainVerdict] = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def actual_calls(self) -> int:
        """**真正打到模型上**的次数 = 总次数 − 缓存命中。

        「这一期调了几次模型」这个问题有两个答案，差着好几倍：命中缓存的
        那几次**一个请求都没发**。财务科问「花了多少钱」时指的是后者。
        """
        return self.calls - self.cache_hits

    @property
    def cache_hit_rate(self) -> Decimal:
        """命中率，定点两位。零次调用时是 ``0.00`` 而不是除零。"""
        if not self.calls:
            return Decimal("0.00")
        return (Decimal(self.cache_hits) * 100 / Decimal(self.calls)).quantize(
            Decimal("0.01")
        )

    @property
    def avg_elapsed_ms(self) -> float:
        """平均耗时。**只按真正发出的调用算**（边界 3 的同一条纪律）：
        把耗时 0 的缓存命中混进平均里，会让「模型变慢了」这个信号消失。"""
        if not self.actual_calls:
            return 0.0
        return round(self.total_elapsed_ms / self.actual_calls, 3)

    @property
    def cost_text(self) -> str:
        """成本文本。**未配单价时明说未配**，不显示 0.00 元。"""
        if not self.priced:
            return "未配置单价"
        return f"{self.cost.quantize(Decimal('0.0001'))} 元"

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "calls": self.calls,
            "actual_calls": self.actual_calls,
            "cache_hits": self.cache_hits,
            "cache_hit_rate": f"{self.cache_hit_rate}%",
            "degraded": self.degraded,
            "retried": self.retried,
            "total_elapsed_ms": round(self.total_elapsed_ms, 3),
            "avg_elapsed_ms": self.avg_elapsed_ms,
            "max_elapsed_ms": round(self.max_elapsed_ms, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "distinct_fingerprints": self.distinct_fingerprints,
            "models": list(self.models),
            "prompt_versions": list(self.prompt_versions),
            "errors": dict(self.errors),
            "priced": self.priced,
            "cost_text": self.cost_text,
            "chain": self.chain.to_dict() if self.chain else None,
        }

    def to_run_summary_fields(self) -> dict[str, Any]:
        """给 :class:`reconq.run_summary.RunSummary` 的扁平字段。

        与 :meth:`reconq.gateway.degradation.DegradationLedger.to_run_summary_fields`
        同一形状：键名与摘要字段一一对应，由 ``build_run_summary`` 收口，
        不在两处各写一遍。
        """
        return {
            "model_calls": self.calls,
            "model_actual_calls": self.actual_calls,
            "model_cache_hits": self.cache_hits,
            "model_cache_hit_rate": f"{self.cache_hit_rate}%",
            "model_total_tokens": self.total_tokens,
            "model_avg_elapsed_ms": self.avg_elapsed_ms,
            "model_cost_text": self.cost_text,
        }

    def render(self) -> str:
        """人读成本表（终端 ``reconq model-usage`` 用）。"""
        lines = [
            f"账期 {self.period} · 模型调用 {self.calls} 次"
            f"（真正发出 {self.actual_calls} 次，缓存命中 {self.cache_hits} 次 "
            f"/ {self.cache_hit_rate}%）",
            f"  token  提示 {self.prompt_tokens} + 生成 {self.completion_tokens}"
            f" = 合计 {self.total_tokens}",
            f"  耗时  合计 {self.total_elapsed_ms:.0f} ms · 平均 "
            f"{self.avg_elapsed_ms:.0f} ms · 单次最慢 {self.max_elapsed_ms:.0f} ms",
            f"  成本  {self.cost_text}",
        ]
        if self.models:
            lines.append(f"  模型  {'、'.join(self.models)}")
        if self.prompt_versions:
            lines.append(f"  提示词版本  {'、'.join(self.prompt_versions)}")
        lines.append(f"  去重  不同证据包 {self.distinct_fingerprints} 份")
        if self.retried:
            lines.append(f"  重试  {self.retried} 次调用发生过重试")
        if self.degraded:
            top = "、".join(
                f"{code}×{n}" for code, n in list(self.errors.items())[:5]
            )
            lines.append(f"  降级  {self.degraded} 次（{top or '无原因码'}）")
        if self.chain is not None and not self.chain.ok:
            lines.append(
                f"  ! 审计链校验未通过：{self.chain.reason}"
                f"（第 {self.chain.broken_seq} 条）"
            )
        return "\n".join(lines)


#: 单价读取。**从环境变量读、缺位即不估**，不内置任何厂商价格 ——
#: 内置价格会在换模型时静默算错，而算错的钱数没人会去核。
def _prices(env: Optional[Mapping[str, str]] = None) -> tuple[Optional[Decimal], Optional[Decimal]]:
    import os

    source = os.environ if env is None else env
    out: list[Optional[Decimal]] = []
    for key in ("prompt", "completion"):
        raw = (source.get(PRICE_ENV_VARS[key]) or "").strip()
        if not raw:
            out.append(None)
            continue
        try:
            value = Decimal(raw)
        except Exception:  # noqa: BLE001 - Decimal 对任何非数字都抛
            raise ReconqError(
                "MODEL_PRICE_INVALID",
                f"{PRICE_ENV_VARS[key]}={raw!r} 不是合法数字；"
                f"单位是元/千 token，例如 0.012",
            ) from None
        if value < 0:
            raise ReconqError(
                "MODEL_PRICE_INVALID",
                f"{PRICE_ENV_VARS[key]}={raw!r} 为负数；单价为负会让成本合计"
                "出现「用得越多越便宜」的荒谬结果",
            )
        out.append(value)
    return out[0], out[1]


def usage_of_period(
    conn: sqlite3.Connection,
    period: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    verify_chain: bool = True,
) -> PeriodUsage:
    """从审计表按账期聚合出 :class:`PeriodUsage`。

    **口径以审计表为准**，不是以「本次跑了几条差异」为准：重扫一次会让
    调用数翻倍，而账单库里一行数据没变过。面板与 ``model-usage`` 读的是
    这一份，两处各算一次必然对不上。
    """
    rows = conn.execute(
        f"SELECT * FROM {MODEL_CALL_TABLE} WHERE period = ? ORDER BY seq", (period,)
    ).fetchall()
    prompt_price, completion_price = _prices(env)

    cache_hits = 0
    degraded = 0
    retried = 0
    total_elapsed = 0.0
    max_elapsed = 0.0
    fingerprints: set[str] = set()
    models: list[str] = []
    versions: list[str] = []
    errors: dict[str, int] = {}
    # 成本累加走 :class:`ModelUsage.__add__`（不另写一遍加法）。
    # 起始值是 ``__add__`` 的**单位元**：priced=True 才能让「每一条都有
    # 单价」这件事 AND 成 True。上一版拿 priced=False 起手，
    # ``False AND True`` 永远是 False —— 于是**配了单价也永远显示
    # 「未配置单价」**，而症状是「成本算不出来」，极难往累加初值上想。
    usage = ModelUsage()

    for row in rows:
        is_hit = bool(row["cache_hit"])
        if is_hit:
            cache_hits += 1
            # 命中不发请求：耗时与 token 都是 0，**整条不累加**。
            # 累加 0 不改变 token 总和，但把 max_elapsed 也算进来会显示成
            # 「最慢的那次是缓存命中」——不可能，故这里连 max 都不碰。
        else:
            elapsed = float(row["elapsed_ms"] or 0.0)
            total_elapsed += elapsed
            max_elapsed = max(max_elapsed, elapsed)
            usage = usage + ModelUsage.of_call(
                int(row["prompt_tokens"] or 0),
                int(row["completion_tokens"] or 0),
                prompt_price=prompt_price,
                completion_price=completion_price,
            )
        if row["status"] != "ok":
            degraded += 1
            code = str(row["error_code"] or "UNKNOWN")
            errors[code] = errors.get(code, 0) + 1
        if row["retried"]:
            retried += 1
        if row["request_fingerprint"]:
            fingerprints.add(str(row["request_fingerprint"]))
        for field_name, bucket in (("model", models), ("prompt_version", versions)):
            value = str(row[field_name] or "")
            if value and value not in bucket:
                bucket.append(value)

    chain = None
    if verify_chain:
        chain = ModelCallLedger(conn).verify_chain(period)

    # 零次真正调用时不能因为「没有一条没定价」就把 priced 判成 True ——
    # 那会让一个空账期显示成「成本 0.0000 元」，读起来像「这次真没花钱」。
    priced = usage.priced and usage.prompt_tokens + usage.completion_tokens > 0

    return PeriodUsage(
        period=period,
        calls=len(rows),
        cache_hits=cache_hits,
        degraded=degraded,
        retried=retried,
        total_elapsed_ms=round(total_elapsed, 3),
        max_elapsed_ms=round(max_elapsed, 3),
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        distinct_fingerprints=len(fingerprints),
        models=tuple(models),
        prompt_versions=tuple(versions),
        errors=errors,
        priced=priced,
        cost=usage.cost.quantize(Decimal("0.0001")) if priced else Decimal("0"),
        chain=chain,
    )
