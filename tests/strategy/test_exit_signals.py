import pytest

from src.strategy.base import Action, PositionState
from src.strategy.exit_signals import (
    ExitDecision,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TRAILING_STOP_PCT,
    draw_from_peak_pct,
    evaluate_exit,
    event_exit_reason,
    make_exit_signal,
    pnl_pct,
    should_arm_trailing,
    should_stop_loss,
    should_trailing_stop,
    update_peak,
)


def _pos(**over):
    base = dict(symbol="A", strategy_id="news_swing", qty=10, avg_price=100.0, current_price=100.0)
    base.update(over)
    return PositionState(**base)


# --- primitives -------------------------------------------------------------

def test_pnl_pct():
    assert pnl_pct(100, 107) == pytest.approx(7.0)
    assert pnl_pct(0, 100) == 0.0  # zero-entry guard


def test_draw_from_peak():
    assert draw_from_peak_pct(200, 194) == pytest.approx(-3.0)
    assert draw_from_peak_pct(0, 100) == 0.0


def test_should_stop_loss():
    assert should_stop_loss(100, 94)  # -6% triggers
    assert should_stop_loss(100, 95)  # exact -5%
    assert not should_stop_loss(100, 96)  # -4%


def test_should_arm_trailing():
    assert should_arm_trailing(100, 107)
    assert should_arm_trailing(100, 108)
    assert not should_arm_trailing(100, 106.99)


def test_should_trailing_stop():
    assert should_trailing_stop(200, 194)  # -3%
    assert not should_trailing_stop(200, 195)
    assert not should_trailing_stop(None, 100)
    assert not should_trailing_stop(0, 100)


# --- event_exit_reason ------------------------------------------------------

def test_event_exit_priority_daily_loss_first():
    assert event_exit_reason(daily_loss_triggered=True, is_halted=True) == "daily_loss_liquidation"


def test_event_exit_halted():
    assert event_exit_reason(is_halted=True) == "halted"


def test_event_exit_managed():
    assert event_exit_reason(is_newly_managed=True) == "managed_stock"


def test_event_exit_vi_repeated():
    assert event_exit_reason(consecutive_vi_count=3) == "vi_repeated"
    assert event_exit_reason(consecutive_vi_count=2) == ""


def test_event_exit_score_collapse():
    assert event_exit_reason(score_drop_below_50=True) == "score_collapse"


def test_event_exit_none():
    assert event_exit_reason() == ""


# --- evaluate_exit ----------------------------------------------------------

def test_evaluate_exit_none_when_healthy():
    d = evaluate_exit(_pos(current_price=103.0))
    assert not d.exit


def test_evaluate_exit_stop_loss():
    d = evaluate_exit(_pos(current_price=94.0))
    assert d.exit and d.reason == "stop_loss"


def test_evaluate_exit_event_beats_stop_loss():
    d = evaluate_exit(_pos(current_price=94.0), is_halted=True)
    assert d.exit and d.reason == "event"
    assert "halted" in d.detail


def test_evaluate_exit_trailing_only_if_armed():
    d = evaluate_exit(_pos(current_price=105.0, highest_price=110.0, tp_armed=False))
    assert not d.exit  # TP 돌파 전에는 trailing 무장 안 됨
    d2 = evaluate_exit(_pos(current_price=106.0, highest_price=110.0, tp_armed=True))
    assert d2.exit and d2.reason == "trailing_stop"


def test_make_exit_signal_shape():
    pos = _pos(current_price=95.0)
    d = evaluate_exit(pos)
    s = make_exit_signal(pos, d, signal_id="x")
    assert s.action == Action.SELL
    assert s.strategy_id == pos.strategy_id
    assert s.qty == pos.qty
    assert s.signal_id == "x"


# --- update_peak ------------------------------------------------------------

def test_update_peak_tracks_max():
    p = _pos(current_price=100, highest_price=105)
    p2 = update_peak(p, new_price=103)
    assert p2.highest_price == 105
    p3 = update_peak(p2, new_price=110)
    assert p3.highest_price == 110


def test_update_peak_initializes_when_none():
    p = _pos(current_price=100, highest_price=None)
    p2 = update_peak(p, new_price=102)
    assert p2.highest_price == 102


def test_update_peak_arms_tp_at_threshold():
    p = _pos(current_price=100, highest_price=100, tp_armed=False)
    p2 = update_peak(p, new_price=107)  # +7%
    assert p2.tp_armed is True


def test_update_peak_tp_armed_sticky():
    p = _pos(current_price=110, highest_price=110, tp_armed=True)
    p2 = update_peak(p, new_price=105)
    assert p2.tp_armed is True  # once armed, stays armed


# --- constants (sanity check with rules)

def test_thresholds_match_rules():
    assert STOP_LOSS_PCT == -5.0
    assert TAKE_PROFIT_PCT == 7.0
    assert TRAILING_STOP_PCT == -3.0
