from datetime import date

import pytest

from src.risk.blacklist import (
    BlacklistEntry,
    LiquidityThresholds,
    MANAGED,
    StaticBlacklist,
    check_liquidity,
)


def test_static_blacklist_empty():
    b = StaticBlacklist()
    assert b.is_blacklisted("005930") is None
    assert len(b) == 0


def test_static_blacklist_from_iterable_and_lookup():
    b = StaticBlacklist.from_iterable([
        BlacklistEntry(symbol="123450", reason=MANAGED, since=date(2026, 4, 1)),
    ])
    hit = b.is_blacklisted("123450")
    assert hit is not None and hit.reason == MANAGED
    assert len(b) == 1


def test_static_blacklist_add_remove():
    b = StaticBlacklist()
    b.add(BlacklistEntry(symbol="123450", reason="SPAC"))
    assert b.is_blacklisted("123450") is not None
    b.remove("123450")
    assert b.is_blacklisted("123450") is None
    b.remove("non-existent")  # no-op


def test_liquidity_ok():
    ok, reason = check_liquidity(market_cap=100_000_000_000, avg_volume_krw=5_000_000_000)
    assert ok and reason == ""


def test_liquidity_missing_market_cap_fails_closed():
    ok, reason = check_liquidity(market_cap=None, avg_volume_krw=5_000_000_000)
    assert not ok and "시가총액" in reason


def test_liquidity_missing_volume_fails_closed():
    ok, reason = check_liquidity(market_cap=100_000_000_000, avg_volume_krw=None)
    assert not ok and "거래대금" in reason


def test_liquidity_below_market_cap_threshold():
    ok, reason = check_liquidity(market_cap=10_000_000_000, avg_volume_krw=5_000_000_000)
    assert not ok and "시총" in reason


def test_liquidity_below_volume_threshold():
    ok, reason = check_liquidity(market_cap=100_000_000_000, avg_volume_krw=100_000_000)
    assert not ok and "거래대금" in reason


def test_liquidity_custom_thresholds():
    t = LiquidityThresholds(min_market_cap_krw=1, min_avg_volume_krw=1)
    ok, _ = check_liquidity(market_cap=10, avg_volume_krw=10, thresholds=t)
    assert ok
