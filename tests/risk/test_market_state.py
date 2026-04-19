import pytest

from src.risk.market_state import (
    GAP_UP_BLOCK_PCT,
    KOSPI_CRASH_PCT,
    MarketState,
    PRICE_LIMIT_APPROACH_PCT,
    can_enter_symbol,
    can_exit_symbol,
    can_trade_today,
)


def test_trade_today_no_data_allows():
    ok, _ = can_trade_today(MarketState())
    assert ok


def test_trade_today_blocked_on_kospi_crash():
    ok, reason = can_trade_today(MarketState(kospi_change_pct=-3.5))
    assert not ok
    assert "KOSPI" in reason


def test_trade_today_threshold_exact():
    ok, _ = can_trade_today(MarketState(kospi_change_pct=KOSPI_CRASH_PCT))
    assert not ok


def test_trade_today_just_above_threshold_allows():
    ok, _ = can_trade_today(MarketState(kospi_change_pct=-2.99))
    assert ok


def test_enter_blocked_by_halt():
    s = MarketState(halted={"005930"})
    ok, reason = can_enter_symbol(s, "005930")
    assert not ok and "거래정지" in reason


def test_enter_blocked_by_vi():
    s = MarketState(vi_active={"005930"})
    ok, reason = can_enter_symbol(s, "005930")
    assert not ok and "VI" in reason


def test_enter_blocked_near_upper_limit():
    s = MarketState(price_change_pct={"005930": PRICE_LIMIT_APPROACH_PCT + 1})
    ok, reason = can_enter_symbol(s, "005930")
    assert not ok and "상/하한가" in reason


def test_enter_blocked_near_lower_limit_absolute():
    s = MarketState(price_change_pct={"005930": -(PRICE_LIMIT_APPROACH_PCT + 1)})
    ok, _ = can_enter_symbol(s, "005930")
    assert not ok


def test_enter_blocked_on_gap_up():
    s = MarketState(gap_open_pct={"005930": GAP_UP_BLOCK_PCT + 0.1})
    ok, reason = can_enter_symbol(s, "005930")
    assert not ok and "갭" in reason


def test_enter_allowed_normal():
    s = MarketState(
        price_change_pct={"005930": 2.0}, gap_open_pct={"005930": 1.0}
    )
    ok, _ = can_enter_symbol(s, "005930")
    assert ok


def test_exit_blocked_when_halted():
    s = MarketState(halted={"005930"})
    ok, reason = can_exit_symbol(s, "005930")
    assert not ok and "거래정지" in reason


def test_exit_allowed_normally():
    ok, _ = can_exit_symbol(MarketState(), "005930")
    assert ok
