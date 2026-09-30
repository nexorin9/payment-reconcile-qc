"""渠道接口的退避重试（Task 5）。

源产品对"取数据"这件事没有重试策略——它的 data source 面向数据库，
连接失败就直接抛。本产品面对的是**会限频、会说"账单正在生成"、下载链接
30 秒就失效**的外部渠道接口，所以退避不是锦上添花，而是取数能不能落地的前提。

三条纪律
--------
1. **只对可重试的错误码退避**：``BILL_NOT_EXIST``（今天真没出账）退避到天荒
   也拿不到，``STATEMENT_CREATING`` / ``FREQUENCY_LIMITED`` / ``LINK_EXPIRED``
   退避才有意义，4xx 业务错误退避是浪费别人的配额。
2. **退避有上限**：重试次数与单次等待都有天花板，用尽即抛
   ``RATE_LIMITED_EXHAUSTED``，由编排层把这个渠道标成"本期未取到"，
   **不硬重试拖住整次扫描**（对齐 Task 6 的单渠道失败剔除语义）。
3. **时钟可注入**：``sleep`` 是参数，测试用假时钟跑完整退避序列，
   不用真的等几秒。
"""

from __future__ import annotations

import time
from typing import Callable, Optional, Sequence, TypeVar

from ..errors import ReconqError, error_meta

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_DELAY",
    "backoff_delays",
    "retry_call",
]

T = TypeVar("T")

#: 默认重试次数（不含首次请求）。3 次 = 首次 + 2 次重试，与 Task 30
#: 网关侧的"最多 2 次重试"是同一档，不因取数链路长就无限放大。
DEFAULT_MAX_ATTEMPTS = 3

#: 退避基数秒：0.5 → 1.0 → 2.0（上限 4.0）
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 4.0

#: 用尽重试次数后统一抛的码
_EXHAUSTED_CODE = "RATE_LIMITED_EXHAUSTED"


def backoff_delays(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    *,
    base: float = DEFAULT_BASE_DELAY,
    cap: float = DEFAULT_MAX_DELAY,
) -> list[float]:
    """给出第 1..max_attempts-1 次重试前的等待秒数（指数退避 + 上限）。

    ``max_attempts=3`` → ``[0.5, 1.0]``；``max_attempts=1`` → ``[]``（不重试）。
    """
    if max_attempts < 1:
        raise ReconqError(
            "RATE_LIMITED_EXHAUSTED",
            f"max_attempts={max_attempts} 非法，至少为 1（1 = 不重试）；"
            f"0 或负数会让「重试 N 次」静默变成一次都不重试",
        )
    delays: list[float] = []
    for i in range(max_attempts - 1):
        delays.append(min(base * (2**i), cap))
    return delays


def _is_retryable(code: str) -> bool:
    """只看错误码元数据里的 ``retryable`` 标记，不在调用点写死 if 分支。"""
    return error_meta(code).retryable


def retry_call(
    fn: Callable[[], T],
    *,
    channel: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base: float = DEFAULT_BASE_DELAY,
    cap: float = DEFAULT_MAX_DELAY,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Optional[Callable[[int, ReconqError, float], None]] = None,
) -> T:
    """执行 ``fn``，对可重试的 ``ReconqError`` 做有上限的指数退避。

    ``fn`` 成功即返回。非 ``ReconqError`` 的异常**原样冒泡**（那是编码 bug，
    不是渠道说"稍后再来"）。重试用尽时抛 ``RATE_LIMITED_EXHAUSTED``，
    ``detail`` 里带最后一次的真实错误码，人能看出到底是限频还是账单在生成。
    """
    delays: Sequence[float] = backoff_delays(max_attempts, base=base, cap=cap)
    last: Optional[ReconqError] = None

    for attempt in range(max_attempts):
        try:
            return fn()
        except ReconqError as exc:
            if not _is_retryable(exc.code):
                raise
            last = exc
            if attempt >= len(delays):  # pragma: no cover - 循环边界由 delays 长度保证
                break
            delay = delays[attempt]
            if on_retry is not None:
                on_retry(attempt + 1, exc, delay)
            sleep(delay)

    assert last is not None  # 只在重试用尽的路径上走到这里
    err = ReconqError(
        _EXHAUSTED_CODE,
        f"渠道 {channel} 退避重试 {max_attempts} 次仍未成功；最后一次错误："
        f"[{last.code}] {last.detail}",
        channel=channel,
        row_ref=last.row_ref,
    )
    #: 真实成因挂在 ``cause_code`` 上。``RATE_LIMITED_EXHAUSTED`` 只是
    #: 「重试用尽」这个**处置结果**，面板要显示的是「到底是限频还是链接过期」，
    #: 所以不能让人只看到一个笼统的码。
    err.cause_code = last.code
    raise err
