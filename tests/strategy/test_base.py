from datetime import date, datetime

import pytest

from src.strategy.base import (
    Action,
    PositionState,
    Signal,
    Strategy,
    StrategyContext,
)


def test_action_enum_values():
    assert Action.BUY.value == "BUY"
    assert Action.SELL.value == "SELL"
    assert Action.HOLD.value == "HOLD"


def test_signal_defaults():
    s = Signal(action=Action.BUY, symbol="005930", strategy_id="x")
    assert s.notional == 0.0 and s.qty == 0 and s.meta == {}


def test_strategy_subclass_requires_id():
    class Bad(Strategy):
        strategy_id = ""

        def decide_entries(self, ctx): return []
        def decide_exits(self, ctx): return []

    with pytest.raises(ValueError, match="strategy_id"):
        Bad()


def test_owned_positions_filters_by_strategy_id():
    class Good(Strategy):
        strategy_id = "x"

        def decide_entries(self, ctx): return []
        def decide_exits(self, ctx): return []

    ctx = StrategyContext(
        as_of=datetime(2026, 4, 19, 10, 0, 0),
        today=date(2026, 4, 19),
        cash_available=1_000_000,
        positions={
            "A": PositionState(symbol="A", strategy_id="x", qty=10, avg_price=100, current_price=100),
            "B": PositionState(symbol="B", strategy_id="other", qty=5, avg_price=200, current_price=200),
        },
    )
    owned = list(Good().owned_positions(ctx))
    assert [p.symbol for p in owned] == ["A"]
