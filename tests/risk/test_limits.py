from datetime import datetime, timedelta

import pytest

from src.risk.limits import (
    CircuitBreakerState,
    CooldownPolicy,
    DailyLossState,
    TradeRecord,
    check_circuit_breaker,
    check_cooldown,
    check_daily_loss,
)


# --- Daily loss -------------------------------------------------------------

def test_daily_loss_ok_positive_pnl():
    s = DailyLossState(start_equity=10_000_000, current_equity=10_100_000)
    ok, _ = check_daily_loss(s)
    assert ok


def test_daily_loss_ok_minor_drawdown():
    s = DailyLossState(start_equity=10_000_000, current_equity=9_800_000)  # -2%
    ok, _ = check_daily_loss(s)
    assert ok


def test_daily_loss_blocked_at_threshold():
    s = DailyLossState(start_equity=10_000_000, current_equity=9_700_000)  # -3%
    ok, reason = check_daily_loss(s)
    assert not ok
    assert "-3" in reason.replace(".00", "").replace(" ", "")


def test_daily_loss_custom_limit():
    s = DailyLossState(start_equity=10_000_000, current_equity=9_900_000)  # -1%
    ok, _ = check_daily_loss(s, limit_pct=-0.5)
    assert not ok


def test_daily_loss_zero_start_equity_returns_zero_pct():
    s = DailyLossState(start_equity=0, current_equity=-1_000_000)
    assert s.loss_pct == 0.0
    ok, _ = check_daily_loss(s)
    assert ok


# --- Circuit breaker --------------------------------------------------------

def test_cb_not_triggered_default():
    ok, _ = check_circuit_breaker(CircuitBreakerState())
    assert ok


def test_cb_triggers_on_3_consecutive_losses():
    s = CircuitBreakerState(consecutive_loss_days=3).evaluate()
    assert s.triggered
    ok, reason = check_circuit_breaker(s)
    assert not ok and "연속 3" in reason


def test_cb_triggers_on_cumulative_10pct_loss():
    s = CircuitBreakerState(cumulative_pnl_pct=-10.0).evaluate()
    assert s.triggered
    ok, _ = check_circuit_breaker(s)
    assert not ok


def test_cb_not_triggered_at_9pct():
    s = CircuitBreakerState(cumulative_pnl_pct=-9.9).evaluate()
    assert not s.triggered


def test_cb_reset_clears_state():
    s = CircuitBreakerState(consecutive_loss_days=5, cumulative_pnl_pct=-15).evaluate()
    assert s.triggered
    s.reset()
    assert not s.triggered
    assert s.consecutive_loss_days == 0
    ok, _ = check_circuit_breaker(s)
    assert ok


def test_cb_triggered_explicit_reason():
    s = CircuitBreakerState(triggered=True, reason="manual halt")
    ok, reason = check_circuit_breaker(s)
    assert not ok and "manual halt" in reason


# --- Cooldown ---------------------------------------------------------------

def _dt(y, m, d, h=10):
    return datetime(y, m, d, h, 0, 0)


def test_cooldown_no_history_allows():
    ok, _ = check_cooldown(symbol="A", trades=[], now=_dt(2026, 4, 19))
    assert ok


def test_cooldown_respects_rebuy_days():
    # Sold Apr 17 (Fri). Apr 19 (Sun) → elapsed business days = 0 → blocked.
    trades = [TradeRecord(symbol="A", side="SELL", ts=_dt(2026, 4, 17))]
    ok, reason = check_cooldown(symbol="A", trades=trades, now=_dt(2026, 4, 19))
    assert not ok and "재매수" in reason


def test_cooldown_released_after_rebuy_days():
    # 매도 월(2026-04-13) → 2026-04-17 금: 4 영업일 경과 → 3일 지나 허용
    trades = [TradeRecord(symbol="A", side="SELL", ts=_dt(2026, 4, 13))]
    ok, _ = check_cooldown(symbol="A", trades=trades, now=_dt(2026, 4, 17))
    assert ok


def test_cooldown_stop_loss_longer_reentry():
    # 손절 매도 금요일 → 화요일: 2영업일 → 재진입 10일 미충족
    trades = [TradeRecord(symbol="A", side="SELL", ts=_dt(2026, 4, 17), was_stop_loss=True)]
    ok, reason = check_cooldown(symbol="A", trades=trades, now=_dt(2026, 4, 21))
    assert not ok and "손절" in reason


def test_cooldown_stop_loss_released_after_10_days():
    # 손절 2026-04-01 (수) → 2026-04-17 (금): 영업일 12
    trades = [TradeRecord(symbol="A", side="SELL", ts=_dt(2026, 4, 1), was_stop_loss=True)]
    ok, _ = check_cooldown(symbol="A", trades=trades, now=_dt(2026, 4, 17))
    assert ok


def test_cooldown_custom_policy():
    trades = [TradeRecord(symbol="A", side="SELL", ts=_dt(2026, 4, 17))]
    policy = CooldownPolicy(rebuy_days=0)
    ok, _ = check_cooldown(
        symbol="A", trades=trades, now=_dt(2026, 4, 19), policy=policy
    )
    assert ok


def test_cooldown_only_considers_latest_sell():
    trades = [
        TradeRecord(symbol="A", side="SELL", ts=_dt(2026, 4, 1)),    # old
        TradeRecord(symbol="A", side="BUY", ts=_dt(2026, 4, 10)),
        TradeRecord(symbol="A", side="SELL", ts=_dt(2026, 4, 17)),   # latest sell
    ]
    ok, _ = check_cooldown(symbol="A", trades=trades, now=_dt(2026, 4, 18))
    assert not ok  # 1 business day since Apr 17


def test_cooldown_other_symbols_ignored():
    trades = [TradeRecord(symbol="B", side="SELL", ts=_dt(2026, 4, 17))]
    ok, _ = check_cooldown(symbol="A", trades=trades, now=_dt(2026, 4, 18))
    assert ok


def test_cooldown_reversed_date_order_defensive():
    """Defensive: caller passes `now` earlier than sell date (e.g., clock skew).
    _business_days_between swaps args to compute absolute gap."""
    # sell ts = Apr 21 (Tue), now = Apr 20 (Mon) → after swap: 1 business day.
    trades = [TradeRecord(symbol="A", side="SELL", ts=_dt(2026, 4, 21))]
    ok, _ = check_cooldown(symbol="A", trades=trades, now=_dt(2026, 4, 20))
    assert not ok  # 1 < rebuy_days(3) → cooldown active
