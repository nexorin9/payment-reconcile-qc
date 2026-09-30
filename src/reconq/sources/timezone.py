"""会话时区归一与账期时间窗。

源产品对应实现
--------------
``github_ref/soda-core/soda-core/src/soda_core/common/data_source_connection.py:121-189``
的 ``parse_session_timezone()``。本轮**直接复用其接受域与拒绝域**，
但把「空值静默回落 UTC」改成「空值显式报错」——理由见下。

为什么跨日切分必须建立在归一时区上
----------------------------------
渠道账单给的是**本地时间**（微信「交易时间」不带偏移），HIS 库里给的是
**库时区**的时间。同一个账期边界，周一 00:00:00 的那一笔，两个来源会
落在不同的一侧。归一时区错一小时，跨日切分口径就整周翻转。

脏值必须报错，不能静默产生偏移
------------------------------
参考实现的注释里写着一处真实事故：``+05:99`` 里的 ``99`` 分钟经
``timedelta`` 溢出后变成 ``06:39``——一个**看起来完全合法、实际是垃圾输入**
的真偏移。源实现已经堵上了（``hours >= 24 or minutes >= 60`` 即抛错）。

本产品在此之上再加一条：``None`` / 空串在源实现里静默回落 UTC。
对账场景不能这样——渠道没配时区而我们默认按 UTC 切账期，等于**把整院
交易时间默默挪了 8 小时**，且没有任何人看得见。所以空值在本产品里是
``TIMEZONE_UNRESOLVED`` 错误，由配置显式补齐。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import ReconqError

__all__ = [
    "parse_session_timezone",
    "to_session_tz",
    "period_window",
    "PeriodWindow",
    "HOSPITAL_TZ",
]


#: 院内账期一律按东八区切。渠道账单给本地时间，HIS 库多半也是 +08:00；
#: 归一到这一个偏移，跨日切分才有唯一答案。
HOSPITAL_TZ = ZoneInfo("Asia/Shanghai")

_UTC_LITERALS = frozenset({"UTC", "GMT", "Z"})

#: 与源实现一致的 UTC 等价 IANA 别名，统一收敛到 ``timezone.utc`` 单例，
#: 免得「按名字报的 UTC」和「按偏移报的 UTC」走到两条分支上。
_UTC_ALIAS_KEYS = frozenset(
    {
        "Etc/UTC",
        "Etc/GMT",
        "GMT",
        "GMT0",
        "GMT+0",
        "GMT-0",
        "Greenwich",
        "Universal",
        "Zulu",
        "Etc/Universal",
        "Etc/Zulu",
        "Etc/Greenwich",
    }
)

#: 驱动用这两个字面量表示「用宿主机本地时区」（见参考实现 :111-118）。
_HOST_LOCAL_LITERALS = frozenset({"localtime", "system", "local"})

_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2}):?(\d{2})?$")


def parse_session_timezone(value: Optional[str]) -> tzinfo:
    """把会话时区表述归一成一个 ``tzinfo``。

    接受：``Asia/Shanghai``（IANA 名）、``+08:00`` / ``+0800`` / ``-05:00``
    （数值偏移）、``UTC`` / ``GMT`` / ``Z`` 及其 IANA 别名、``localtime``
    （宿主机本地时区）。

    拒绝并抛 ``TIMEZONE_UNRESOLVED``：``None``、空串、纯空白、``+05:99``、
    ``+99:00``、``chinese`` 这类无法解析或越界的脏值。

    与源实现的差异只有一处，且是收紧：源实现对空值静默返回 UTC 并注释
    「每个 adapter 在查不到时都这么兜底」；本产品对空值报错，因为空时区
    会把跨日切分悄悄挪 8 小时，属于「真但错」的错误——见模块 docstring。

    另有一条**主动收紧**：源实现允许 ``hours >= 24`` 才抛错，本产品同时
    拒绝 ``minutes >= 60``（源实现 :182 已含此判断，此处显式沿用），
    避免 ``+05:99`` 经 ``timedelta`` 溢出成 ``06:39``。
    """
    if value is None:
        raise ReconqError(
            "TIMEZONE_UNRESOLVED",
            "会话时区为空。对账不允许静默按 UTC 切账期——请显式配置时区"
            "（如 RECONQ_SESSION_TZ=Asia/Shanghai）",
        )

    text = value.strip(" \r\n\t'\"")
    if not text:
        raise ReconqError(
            "TIMEZONE_UNRESOLVED",
            "会话时区为空白串，等同于未配置；拒绝静默按 UTC 切账期",
        )

    if text.upper() in _UTC_LITERALS:
        return timezone.utc

    if text.lower() in _HOST_LOCAL_LITERALS:
        return datetime.now().astimezone().tzinfo or timezone.utc

    try:
        zi = ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError, KeyError):
        zi = None
    if zi is not None:
        if zi.key in _UTC_ALIAS_KEYS:
            return timezone.utc
        return zi

    match = _OFFSET_RE.match(text)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        # 关键：越界必须显式拒绝。``+05:99`` 若放行，会经 timedelta 溢出
        # 变成 ``06:39``——一个真但完全错误的偏移。
        if hours >= 24 or minutes >= 60:
            raise ReconqError(
                "TIMEZONE_INVALID",
                f"会话时区 {value!r} 越界：小时需 <24、分钟需 <60。"
                f"放行会把 {value!r} 静默算成另一个偏移（分钟经 timedelta 溢出）",
            )
        offset = sign * timedelta(hours=hours, minutes=minutes)
        if offset == timedelta(0):
            return timezone.utc
        return timezone(offset)

    raise ReconqError(
        "TIMEZONE_INVALID",
        f"无法解析会话时区 {value!r}；支持 IANA 名（如 Asia/Shanghai）、"
        f"数值偏移（如 +08:00）、UTC/GMT/Z 或 localtime",
    )


def to_session_tz(value: Optional[str], *, default_to_hospital: bool = True) -> tzinfo:
    """归一时区；``value`` 为空时按院内东八区兜底（**显式**，非静默）。"""
    if (value is None or value.strip() == "") and default_to_hospital:
        return HOSPITAL_TZ
    return parse_session_timezone(value)


class PeriodWindow:
    """一个账期的闭开时间窗 ``[start, end)``（带时区）。

    半开区间是刻意的：周一 00:00:00 那笔属于**新**账期，不属于旧的。
    两端都闭的话，周一零点会同时落进两个账期，对账就会双记。
    """

    __slots__ = ("period", "start", "end", "tz")

    def __init__(self, period: str, start: datetime, end: datetime, tz: tzinfo) -> None:
        self.period = period
        self.start = start
        self.end = end
        self.tz = tz

    def contains(self, moment: datetime) -> bool:
        """判断一个时刻是否落在本账期内（先把无偏移时刻按归一时区贴上）。"""
        localized = self.localize(moment)
        return self.start <= localized < self.end

    def localize(self, moment: datetime) -> datetime:
        """把 naive 时刻按归一时区贴上偏移；已带偏移的时刻转成本窗时区。"""
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.tz)
        return moment.astimezone(self.tz)

    def bucket_date(self, moment: datetime) -> str:
        """该时刻归属的**账单日期**（``YYYY-MM-DD``，账期时区下的日历日）。"""
        return self.localize(moment).strftime("%Y-%m-%d")

    def is_cross_period(self, moment: datetime) -> bool:
        """该时刻是否落在账期边界外（跨日切分 check 的判据）。"""
        return not self.contains(moment)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"PeriodWindow({self.period}, {self.start.isoformat()} → {self.end.isoformat()})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PeriodWindow):
            return NotImplemented
        return (
            self.period == other.period
            and self.start == other.start
            and self.end == other.end
        )


def parse_period(period: str) -> tuple[int, int]:
    """解析 ``2026-W36`` → ``(2026, 36)``；非法格式抛 ``PERIOD_INVALID``。"""
    match = re.match(r"^(\d{4})-?W(\d{1,2})$", period.strip().upper())
    if not match:
        raise ReconqError(
            "PERIOD_INVALID",
            f"账期格式非法 {period!r}；应为 2026-W36 这样的 ISO 周格式",
        )
    year, week = int(match.group(1)), int(match.group(2))
    if not 1 <= week <= 53:
        raise ReconqError(
            "PERIOD_INVALID",
            f"账期周次越界：{week}（合法范围 1–53），账期 {period!r} 不存在",
        )
    return year, week


def period_window(period: str, tz: Optional[tzinfo] = None) -> PeriodWindow:
    """计算一个 ISO 周账期的闭开时间窗（周一 00:00:00 → 下周一 00:00:00）。"""
    resolved = tz or HOSPITAL_TZ
    year, week = parse_period(period)
    # date.fromisocalendar 是唯一不必自己算 ISO 周边界的可靠途径：
    # 手工推 4 月/年末的周边界极易差一天，而差一天就是一整批单边账。
    monday = date.fromisocalendar(year, week, 1)
    next_monday = monday + timedelta(days=7)
    start = datetime(monday.year, monday.month, monday.day, tzinfo=resolved)
    end = datetime(next_monday.year, next_monday.month, next_monday.day, tzinfo=resolved)
    return PeriodWindow(period, start, end, resolved)
