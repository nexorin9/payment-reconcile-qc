"""时区归一与账期时间窗测试（Task 4）。

对照参考实现 ``data_source_connection.py:121-189`` 的
``parse_session_timezone()``，并锁死其注释里点名的那个真事故：
``+05:99`` 经 ``timedelta`` 溢出成 ``06:39``——一个真但完全错的偏移。
本产品对越界值必须**报错**，对空值也必须**报错**（比源实现更严，
理由见 ``timezone.py`` 模块 docstring）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from reconq.errors import ReconqError
from reconq.sources.timezone import (
    HOSPITAL_TZ,
    parse_session_timezone,
    period_window,
    to_session_tz,
)


def _offset(tz) -> timedelta:
    """取 tzinfo 在某个基准时刻的 UTC 偏移。"""
    moment = datetime(2026, 9, 7, 12, 0, 0, tzinfo=tz)
    return moment.utcoffset()


# —— ① 接受域：三种写法归一到同一偏移 ——


def test_iana_and_numeric_offset_normalize_equal():
    """Asia/Shanghai 与 +08:00 必须归一成同一偏移——这是 Task 的核心断言。"""
    assert _offset(parse_session_timezone("Asia/Shanghai")) == _offset(
        parse_session_timezone("+08:00")
    )
    assert _offset(parse_session_timezone("Asia/Shanghai")) == timedelta(hours=8)


def test_compact_numeric_offset_accepted():
    assert _offset(parse_session_timezone("+0800")) == timedelta(hours=8)
    assert _offset(parse_session_timezone("-05:00")) == timedelta(hours=-5)


def test_utc_literals_and_aliases_collapse_to_singleton():
    """UTC 按名字、按别名、按偏移报的，都必须落到同一个 timezone.utc 单例。"""
    for text in ("UTC", "utc", "GMT", "Z", "Etc/UTC", "Zulu", "Universal", "+00:00", "-00:00"):
        assert parse_session_timezone(text) is timezone.utc, text


def test_utc_prefixed_offset_accepted():
    assert _offset(parse_session_timezone("UTC+02:00")) == timedelta(hours=2)


def test_quoted_and_padded_values_accepted():
    """驱动可能给带引号、带头尾空白的结果。"""
    assert _offset(parse_session_timezone("  'Asia/Shanghai'  ")) == timedelta(hours=8)


# —— ② 越界脏值必须报错（源实现的 +05:99 事故）——


def test_out_of_range_minutes_rejected():
    """+05:99 在源实现里会溢出成 06:39；本产品必须报错而不是给一个假偏移。"""
    with pytest.raises(ReconqError) as exc:
        parse_session_timezone("+05:99")
    assert exc.value.code == "TIMEZONE_INVALID"
    # 报错信息要点明「会溢出」，让排障的人知道为什么这个值危险
    assert "溢出" in exc.value.detail


def test_out_of_range_hours_rejected():
    with pytest.raises(ReconqError) as exc:
        parse_session_timezone("+99:00")
    assert exc.value.code == "TIMEZONE_INVALID"


def test_boundary_values_accepted():
    """+23:59 合法（不是整小时也不能拒），+24:00 非法。"""
    assert _offset(parse_session_timezone("+23:59")) == timedelta(hours=23, minutes=59)
    with pytest.raises(ReconqError):
        parse_session_timezone("+24:00")


# —— ③ 空值必须报错（比源实现更严的一处收紧）——


def test_none_rejected_not_silently_utc():
    """源实现对 None 静默返回 UTC；本产品拒绝——静默 UTC 等于把交易时间挪 8 小时。"""
    with pytest.raises(ReconqError) as exc:
        parse_session_timezone(None)
    assert exc.value.code == "TIMEZONE_UNRESOLVED"
    assert "UTC" in exc.value.detail


def test_blank_rejected():
    for text in ("", "   ", "\t\n"):
        with pytest.raises(ReconqError) as exc:
            parse_session_timezone(text)
        assert exc.value.code == "TIMEZONE_UNRESOLVED", repr(text)


def test_garbage_string_rejected():
    with pytest.raises(ReconqError) as exc:
        parse_session_timezone("chinese")
    assert exc.value.code == "TIMEZONE_INVALID"
    assert "Asia/Shanghai" in exc.value.detail  # 报错信息要给出可用的写法


def test_to_session_tz_falls_back_to_hospital_explicitly():
    """兜底只在显式开关下发生，且兜到院内东八区而不是 UTC。"""
    assert to_session_tz(None) is HOSPITAL_TZ
    assert to_session_tz("") is HOSPITAL_TZ
    assert _offset(to_session_tz(None)) == timedelta(hours=8)


# —— ④ 账期时间窗：半开区间 + 跨日切分 ——


def test_period_window_is_half_open_monday_to_monday():
    window = period_window("2026-W36")
    assert window.start == datetime(2026, 8, 31, 0, 0, 0, tzinfo=HOSPITAL_TZ)
    assert window.end == datetime(2026, 9, 7, 0, 0, 0, tzinfo=HOSPITAL_TZ)


def test_monday_midnight_belongs_to_new_period_not_old():
    """周一 00:00:00 那笔属于新账期。两端都闭会双记一笔。"""
    week36 = period_window("2026-W36")
    week37 = period_window("2026-W37")
    boundary = datetime(2026, 9, 7, 0, 0, 0)
    assert week36.contains(boundary) is False
    assert week37.contains(boundary) is True


def test_sunday_last_second_still_in_period():
    window = period_window("2026-W36")
    assert window.contains(datetime(2026, 9, 6, 23, 59, 59)) is True
    assert window.contains(datetime(2026, 9, 7, 0, 0, 0)) is False


def test_cross_period_detection_around_midnight():
    """跨日切分 check 的判据：边界外一侧的时刻被标为跨期。"""
    window = period_window("2026-W36")
    assert window.is_cross_period(datetime(2026, 9, 6, 23, 30)) is False
    assert window.is_cross_period(datetime(2026, 9, 7, 0, 30)) is True


def test_aware_datetime_converted_into_window_tz():
    """渠道给本地时间、HIS 给库时区：带偏移的时刻要转进账期时区再比。"""
    window = period_window("2026-W36")
    # UTC 2026-09-06 16:30 == 东八区 2026-09-07 00:30，落在 W37 而非 W36
    utc_moment = datetime(2026, 9, 6, 16, 30, tzinfo=timezone.utc)
    assert window.contains(utc_moment) is False
    assert window.bucket_date(utc_moment) == "2026-09-07"


def test_naive_datetime_assumed_hospital_tz():
    """naive 时刻按账期时区贴偏移——渠道账单就是这么给的。"""
    window = period_window("2026-W36")
    naive = datetime(2026, 9, 1, 10, 0, 0)
    assert window.contains(naive) is True
    assert window.localize(naive).tzinfo is HOSPITAL_TZ


def test_window_respects_explicit_tz():
    utc_window = period_window("2026-W36", tz=timezone.utc)
    assert utc_window.start == datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc)


# —— ⑤ 账期格式校验 ——


def test_period_format_accepted_both_spellings():
    assert period_window("2026-W36").period == "2026-W36"
    assert period_window("2026w36").period == "2026w36"


def test_invalid_period_rejected():
    for text in ("2026-36", "W36", "2026-W00", "2026-W54", "上上周"):
        with pytest.raises(ReconqError) as exc:
            period_window(text)
        assert exc.value.code == "PERIOD_INVALID", text


def test_year_boundary_period_computed_correctly():
    """跨年周最容易手推错，用 fromisocalendar 才能算对。"""
    window = period_window("2027-W01")
    assert window.start == datetime(2027, 1, 4, 0, 0, 0, tzinfo=HOSPITAL_TZ)
    assert window.end == datetime(2027, 1, 11, 0, 0, 0, tzinfo=HOSPITAL_TZ)
