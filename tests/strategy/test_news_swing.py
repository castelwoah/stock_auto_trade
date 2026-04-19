"""Unit tests for NewsSwingStrategy."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from src.strategy.base import Action, PositionState, StrategyContext
from src.strategy.news_swing import NewsSwingStrategy


TODAY = date(2026, 4, 18)


def _ctx(
    *,
    as_of: datetime | None = None,
    cash: float = 50_000_000.0,
    positions: dict[str, PositionState] | None = None,
    scores: dict[str, float] | None = None,
    prices: dict[str, float] | None = None,
    extras: dict | None = None,
) -> StrategyContext:
    return StrategyContext(
        as_of=as_of or datetime(2026, 4, 18, 10, 30, 0),
        today=TODAY,
        cash_available=cash,
        positions=positions or {},
        candidate_scores=scores or {},
        current_prices=prices or {},
        prev_day_ohlc={},
        extras=extras or {},
    )


def _pos(symbol: str, *, avg: float = 10000.0, cur: float = 10000.0, qty: int = 10,
         highest: float | None = None, tp_armed: bool = False) -> PositionState:
    return PositionState(
        symbol=symbol,
        strategy_id="news_swing",
        qty=qty,
        avg_price=avg,
        current_price=cur,
        highest_price=highest,
        tp_armed=tp_armed,
    )


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

def test_invalid_target_position_count_raises():
    with pytest.raises(ValueError):
        NewsSwingStrategy(target_position_count=0)
    with pytest.raises(ValueError):
        NewsSwingStrategy(target_position_count=-3)


def test_default_strategy_id():
    s = NewsSwingStrategy()
    assert s.strategy_id == "news_swing"


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

def test_entries_empty_candidates():
    s = NewsSwingStrategy()
    ctx = _ctx(scores={}, prices={})
    assert s.decide_entries(ctx) == []


def test_entries_three_above_threshold_equal_notional():
    s = NewsSwingStrategy(target_position_count=5, min_score=70.0)
    ctx = _ctx(
        cash=50_000_000.0,
        scores={"A": 90.0, "B": 80.0, "C": 75.0},
        prices={"A": 1000.0, "B": 2000.0, "C": 3000.0},
    )
    signals = s.decide_entries(ctx)
    assert len(signals) == 3
    # Sorted by score desc
    assert [sig.symbol for sig in signals] == ["A", "B", "C"]
    for sig in signals:
        assert sig.action == Action.BUY
        assert sig.notional == 10_000_000.0
        assert sig.strategy_id == "news_swing"
        assert sig.signal_id == f"news_swing-{sig.symbol}-{TODAY.isoformat()}"
        assert TODAY.isoformat() in sig.signal_id


def test_entries_below_threshold_excluded():
    s = NewsSwingStrategy(target_position_count=5, min_score=70.0)
    ctx = _ctx(
        scores={"A": 90.0, "B": 65.0, "C": 50.0},
        prices={"A": 1000.0, "B": 1000.0, "C": 1000.0},
    )
    signals = s.decide_entries(ctx)
    assert [sig.symbol for sig in signals] == ["A"]


def test_entries_already_owned_excluded():
    s = NewsSwingStrategy(target_position_count=5, min_score=70.0)
    ctx = _ctx(
        scores={"A": 90.0, "B": 80.0},
        prices={"A": 1000.0, "B": 1000.0},
        positions={"A": _pos("A")},
    )
    signals = s.decide_entries(ctx)
    assert [sig.symbol for sig in signals] == ["B"]


def test_entries_after_1500_returns_empty():
    s = NewsSwingStrategy()
    ctx = _ctx(
        as_of=datetime(2026, 4, 18, 15, 0, 0),
        scores={"A": 90.0},
        prices={"A": 1000.0},
    )
    assert s.decide_entries(ctx) == []

    ctx2 = _ctx(
        as_of=datetime(2026, 4, 18, 15, 30, 0),
        scores={"A": 90.0},
        prices={"A": 1000.0},
    )
    assert s.decide_entries(ctx2) == []


def test_entries_zero_cash_returns_empty():
    s = NewsSwingStrategy()
    ctx = _ctx(cash=0.0, scores={"A": 90.0}, prices={"A": 1000.0})
    assert s.decide_entries(ctx) == []


def test_entries_missing_or_nonpositive_price_skipped():
    s = NewsSwingStrategy(target_position_count=5, min_score=70.0)
    ctx = _ctx(
        scores={"A": 90.0, "B": 85.0, "C": 80.0, "D": 75.0},
        prices={"A": 1000.0, "B": 0.0, "C": -100.0},  # D missing
    )
    signals = s.decide_entries(ctx)
    assert [sig.symbol for sig in signals] == ["A"]


def test_entries_available_slots_respected():
    s = NewsSwingStrategy(target_position_count=3, min_score=70.0)
    ctx = _ctx(
        scores={"A": 90.0, "B": 85.0, "C": 80.0, "D": 75.0},
        prices={"A": 1000.0, "B": 1000.0, "C": 1000.0, "D": 1000.0},
        positions={"X": _pos("X")},  # 1 slot already used
    )
    signals = s.decide_entries(ctx)
    # 3 - 1 = 2 available slots
    assert len(signals) == 2
    assert [sig.symbol for sig in signals] == ["A", "B"]


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------

def test_exit_stop_loss_triggers_sell():
    s = NewsSwingStrategy()
    # -5% 도달 (avg 10000 → cur 9500)
    pos = _pos("A", avg=10000.0, cur=9500.0, qty=10)
    ctx = _ctx(
        scores={"A": 75.0},  # score 정상
        positions={"A": pos},
    )
    signals = s.decide_exits(ctx)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == Action.SELL
    assert sig.symbol == "A"
    assert sig.strategy_id == "news_swing"
    assert sig.qty == 10
    assert "stop_loss" in sig.signal_id
    assert TODAY.isoformat() in sig.signal_id


def test_exit_score_collapse_triggers_sell():
    s = NewsSwingStrategy()
    # price 정상 (+2%), but score dropped to 40
    pos = _pos("A", avg=10000.0, cur=10200.0, qty=5)
    ctx = _ctx(
        scores={"A": 40.0},
        positions={"A": pos},
    )
    signals = s.decide_exits(ctx)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == Action.SELL
    # exit_signals aggregator uses reason="event", detail="score_collapse"
    # reason 문자열에 score 관련 detail 포함
    assert "score" in sig.reason.lower()


def test_exit_halted_triggers_sell():
    s = NewsSwingStrategy()
    pos = _pos("A", avg=10000.0, cur=10100.0, qty=7)
    ctx = _ctx(
        scores={"A": 80.0},
        positions={"A": pos},
        extras={"halted_symbols": {"A"}},
    )
    signals = s.decide_exits(ctx)
    assert len(signals) == 1
    assert signals[0].action == Action.SELL
    # reason is aggregated as "event: halted" by make_exit_signal
    assert "halted" in signals[0].reason


def test_exit_healthy_position_no_signal():
    s = NewsSwingStrategy()
    # +3% profit, score 75
    pos = _pos("A", avg=10000.0, cur=10300.0, qty=10)
    ctx = _ctx(
        scores={"A": 75.0},
        positions={"A": pos},
    )
    signals = s.decide_exits(ctx)
    assert signals == []


def test_exit_daily_loss_liquidates_all():
    s = NewsSwingStrategy()
    pos_a = _pos("A", avg=10000.0, cur=10100.0, qty=5)
    pos_b = _pos("B", avg=20000.0, cur=20500.0, qty=3)
    ctx = _ctx(
        scores={"A": 80.0, "B": 75.0},
        positions={"A": pos_a, "B": pos_b},
        extras={"daily_loss_triggered": True},
    )
    signals = s.decide_exits(ctx)
    assert len(signals) == 2
    symbols = {sig.symbol for sig in signals}
    assert symbols == {"A", "B"}
    for sig in signals:
        assert sig.action == Action.SELL
        assert sig.strategy_id == "news_swing"


def test_exit_strategy_id_and_signal_id_format():
    s = NewsSwingStrategy()
    pos = _pos("A", avg=10000.0, cur=9400.0, qty=10)  # stop_loss
    ctx = _ctx(scores={"A": 80.0}, positions={"A": pos})
    signals = s.decide_exits(ctx)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.strategy_id == "news_swing"
    assert sig.signal_id is not None
    assert sig.signal_id.startswith("news_swing-exit-A-")
    assert sig.signal_id.endswith(TODAY.isoformat())


def test_exit_ignores_other_strategy_positions():
    s = NewsSwingStrategy()
    foreign = PositionState(
        symbol="A", strategy_id="breakout", qty=10,
        avg_price=10000.0, current_price=9000.0,
    )
    ctx = _ctx(scores={"A": 30.0}, positions={"A": foreign})
    assert s.decide_exits(ctx) == []


def test_exit_missing_score_uses_neutral_50():
    s = NewsSwingStrategy()
    # score not provided, should default to 50.0 — NOT a collapse (50 not < 50)
    pos = _pos("A", avg=10000.0, cur=10100.0, qty=10)
    ctx = _ctx(scores={}, positions={"A": pos})
    assert s.decide_exits(ctx) == []
