"""KRX 주식 호가단위 (2023-01-25 KOSPI·KOSDAQ 통합 기준).

호가단위에 맞지 않는 지정가는 거래소에서 거절되므로, 주문 가격은 반드시 여기를
거친다. ETF/ETN 등 별도 호가 체계 종목은 watchlist 에서 제외되어 있다.
"""
from __future__ import annotations

import math

# (가격 상한 미만, 호가단위)
_TICK_TABLE: tuple[tuple[int, int], ...] = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
)
_TOP_TICK = 1_000


def tick_size(price: float) -> int:
    for bound, tick in _TICK_TABLE:
        if price < bound:
            return tick
    return _TOP_TICK


def round_to_tick(price: float, *, up: bool) -> int:
    """호가단위 정렬. 매수는 올림(up=True), 매도는 내림."""
    if price <= 0:
        raise ValueError("price must be positive")
    t = tick_size(price)
    q = price / t
    n = math.ceil(q - 1e-9) if up else math.floor(q + 1e-9)
    return max(t, int(n * t))


def shift_ticks(price: float, n: int) -> int:
    """정렬된 가격에서 n 호가 이동 (n>0 상향, n<0 하향). 구간 경계를 넘는 경우 처리."""
    p = round_to_tick(price, up=n > 0)
    for _ in range(abs(n)):
        if n > 0:
            p += tick_size(p)
        else:
            step = tick_size(p - 1)
            if p - step <= 0:
                break
            p -= step
    return p
