from datetime import date, datetime

import pytest

from src.strategy.base import Action, PositionState, Signal, Strategy, StrategyContext
from src.strategy.registry import StrategyRegistry, build_idempotency_key


class _StubStrategy(Strategy):
    def __init__(self, sid, entries=(), exits=()):
        self.strategy_id = sid
        super().__init__()
        self._entries = list(entries)
        self._exits = list(exits)
        self.last_ctx = None

    def decide_entries(self, ctx):
        self.last_ctx = ctx
        return self._entries

    def decide_exits(self, ctx):
        return self._exits


def _ctx(**over):
    base = dict(
        as_of=datetime(2026, 4, 19, 10, 0, 0),
        today=date(2026, 4, 19),
        cash_available=10_000_000,
        positions={
            "A": PositionState("A", "news_swing", 10, 100, 105),
            "B": PositionState("B", "breakout", 20, 200, 210),
        },
    )
    base.update(over)
    return StrategyContext(**base)


def test_register_and_list():
    reg = StrategyRegistry()
    s = _StubStrategy("x")
    reg.register(s, allocation_pct=30)
    assert reg.strategies() == [s]
    assert reg.allocation("x") == 30.0
    assert reg.reserve_pct() == 70.0


def test_register_duplicate_raises():
    reg = StrategyRegistry()
    reg.register(_StubStrategy("x"), allocation_pct=20)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_StubStrategy("x"), allocation_pct=10)


def test_register_invalid_allocation():
    reg = StrategyRegistry()
    with pytest.raises(ValueError):
        reg.register(_StubStrategy("x"), allocation_pct=120)
    with pytest.raises(ValueError):
        reg.register(_StubStrategy("x"), allocation_pct=-1)


def test_register_exceeds_total():
    reg = StrategyRegistry()
    reg.register(_StubStrategy("a"), allocation_pct=70)
    with pytest.raises(ValueError, match="100"):
        reg.register(_StubStrategy("b"), allocation_pct=40)


def test_unregister_idempotent():
    reg = StrategyRegistry()
    reg.register(_StubStrategy("x"), allocation_pct=20)
    reg.unregister("x")
    reg.unregister("x")  # no-op
    assert reg.allocation("x") == 0.0


def test_sub_context_isolates_cash_and_positions():
    reg = StrategyRegistry()
    stub = _StubStrategy("news_swing")
    reg.register(stub, allocation_pct=50)
    reg.all_signals(_ctx())
    sub = stub.last_ctx
    # 10M × 50% = 5M
    assert sub.cash_available == 5_000_000
    # news_swing 소유 포지션만 노출
    assert set(sub.positions.keys()) == {"A"}


def test_all_signals_exits_first_then_entries():
    reg = StrategyRegistry()
    entry = Signal(action=Action.BUY, symbol="X", strategy_id="x", notional=1, signal_id="e1")
    exit_s = Signal(action=Action.SELL, symbol="Y", strategy_id="x", qty=10, signal_id="x1")
    reg.register(_StubStrategy("x", entries=[entry], exits=[exit_s]), allocation_pct=20)
    out = reg.all_signals(_ctx())
    assert [s.action for s in out] == [Action.SELL, Action.BUY]


def test_wrong_strategy_id_signals_filtered():
    reg = StrategyRegistry()
    wrong = Signal(action=Action.BUY, symbol="X", strategy_id="WRONG", notional=1)
    reg.register(_StubStrategy("x", entries=[wrong]), allocation_pct=20)
    assert reg.all_signals(_ctx()) == []


def test_executed_signal_ids_filtered():
    reg = StrategyRegistry()
    s = Signal(action=Action.BUY, symbol="X", strategy_id="x", notional=1, signal_id="e1")
    reg.register(_StubStrategy("x", entries=[s]), allocation_pct=20)
    # first call
    out1 = reg.all_signals(_ctx())
    assert len(out1) == 1
    reg.mark_executed(["e1"])
    out2 = reg.all_signals(_ctx())
    assert out2 == []  # filtered as duplicate


def test_strategy_exception_isolated():
    class Explodes(Strategy):
        strategy_id = "boom"
        def decide_entries(self, ctx): raise RuntimeError("boom")
        def decide_exits(self, ctx): raise RuntimeError("boom")

    reg = StrategyRegistry()
    reg.register(Explodes(), allocation_pct=10)
    healthy = _StubStrategy(
        "ok",
        entries=[Signal(action=Action.BUY, symbol="X", strategy_id="ok", notional=1)],
    )
    reg.register(healthy, allocation_pct=10)
    out = reg.all_signals(_ctx())
    assert any(s.strategy_id == "ok" for s in out)


def test_build_idempotency_key_uses_signal_id():
    s = Signal(action=Action.BUY, symbol="005930", strategy_id="news_swing", signal_id="abc")
    assert build_idempotency_key(s, trade_date="2026-04-19") == "2026-04-19:005930:news_swing:abc"


def test_build_idempotency_key_fallback_to_action():
    s = Signal(action=Action.SELL, symbol="005930", strategy_id="breakout")
    assert build_idempotency_key(s, trade_date="2026-04-19") == "2026-04-19:005930:breakout:SELL"
