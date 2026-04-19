"""RiskEngine orchestrator + @risk_gate decorator tests."""
from datetime import date, datetime

import pytest

from src.risk.blacklist import (
    BlacklistEntry,
    LiquidityThresholds,
    MANAGED,
    StaticBlacklist,
)
from src.risk.exceptions import RiskBlocked
from src.risk.limits import (
    BuyContext,
    CircuitBreakerState,
    CooldownPolicy,
    DailyLossState,
    RiskEngine,
    TradeRecord,
    get_risk_engine,
    risk_gate,
    set_risk_engine,
)
from src.risk.market_state import MarketState
from src.risk.sizing import SizingPolicy


def _base_engine(**overrides):
    defaults = dict(
        blacklist=StaticBlacklist(),
        market_state=MarketState(),
        daily_loss=DailyLossState(start_equity=10_000_000, current_equity=10_000_000),
        circuit_breaker=CircuitBreakerState(),
        sizing_policy=SizingPolicy(),
        cooldown_policy=CooldownPolicy(),
        liquidity_thresholds=LiquidityThresholds(),
        trades_history=[],
        now_provider=lambda: datetime(2026, 4, 19, 10, 0, 0),
    )
    defaults.update(overrides)
    return RiskEngine(**defaults)


def _ctx(**overrides):
    defaults = dict(
        symbol="005930",
        portfolio_value=10_000_000.0,
        existing_positions_notional={},
        market_cap=100_000_000_000,
        avg_volume_krw=5_000_000_000,
    )
    defaults.update(overrides)
    return BuyContext(**defaults)


# --- RiskEngine.check_buy ---------------------------------------------------

def test_engine_buy_passes_with_clean_state():
    engine = _base_engine()
    engine.check_buy(_ctx())  # must not raise


def test_engine_buy_blocked_by_circuit_breaker():
    engine = _base_engine(
        circuit_breaker=CircuitBreakerState(consecutive_loss_days=3).evaluate()
    )
    with pytest.raises(RiskBlocked) as ei:
        engine.check_buy(_ctx())
    assert ei.value.code == "circuit_breaker"


def test_engine_buy_blocked_by_daily_loss_limit():
    engine = _base_engine(
        daily_loss=DailyLossState(start_equity=10_000_000, current_equity=9_700_000)
    )
    with pytest.raises(RiskBlocked) as ei:
        engine.check_buy(_ctx())
    assert ei.value.code == "daily_loss_limit"


def test_engine_buy_blocked_by_kospi_crash():
    engine = _base_engine(market_state=MarketState(kospi_change_pct=-5.0))
    with pytest.raises(RiskBlocked) as ei:
        engine.check_buy(_ctx())
    assert ei.value.code == "market_halt"


def test_engine_buy_blocked_by_blacklist():
    engine = _base_engine(
        blacklist=StaticBlacklist.from_iterable([
            BlacklistEntry(symbol="005930", reason=MANAGED, since=date(2026, 4, 1))
        ])
    )
    with pytest.raises(RiskBlocked) as ei:
        engine.check_buy(_ctx())
    assert ei.value.code == "blacklist"


def test_engine_buy_blocked_by_liquidity_small_cap():
    with pytest.raises(RiskBlocked) as ei:
        _base_engine().check_buy(_ctx(market_cap=10_000_000_000))
    assert ei.value.code == "liquidity"


def test_engine_buy_blocked_by_vi():
    engine = _base_engine(market_state=MarketState(vi_active={"005930"}))
    with pytest.raises(RiskBlocked) as ei:
        engine.check_buy(_ctx())
    assert ei.value.code == "symbol_state"


def test_engine_buy_blocked_by_cooldown():
    trades = [TradeRecord(symbol="005930", side="SELL", ts=datetime(2026, 4, 17))]
    engine = _base_engine(trades_history=trades)
    with pytest.raises(RiskBlocked) as ei:
        engine.check_buy(_ctx())
    assert ei.value.code == "cooldown"


def test_engine_buy_blocked_by_sizing_symbol_cap():
    ctx = _ctx(existing_positions_notional={"005930": 2_000_000.0})
    with pytest.raises(RiskBlocked) as ei:
        _base_engine().check_buy(ctx)
    assert ei.value.code == "sizing"


def test_engine_buy_blocked_when_intended_exceeds_headroom():
    ctx = _ctx(intended_notional=3_000_000.0)  # > 20% of 10M
    with pytest.raises(RiskBlocked) as ei:
        _base_engine().check_buy(ctx)
    assert ei.value.code == "sizing"


def test_engine_buy_passes_with_intended_within_headroom():
    ctx = _ctx(intended_notional=1_500_000.0)
    _base_engine().check_buy(ctx)


# --- RiskEngine.check_sell --------------------------------------------------

def test_engine_sell_blocked_when_symbol_halted():
    engine = _base_engine(market_state=MarketState(halted={"005930"}))
    with pytest.raises(RiskBlocked) as ei:
        engine.check_sell("005930")
    assert ei.value.code == "symbol_halted"


def test_engine_sell_allowed_even_when_daily_loss_limit_hit():
    # DL/CB 상태에서도 기존 포지션 청산은 허용 — 청산이 리스크 저감 수단.
    engine = _base_engine(
        daily_loss=DailyLossState(start_equity=10_000_000, current_equity=9_000_000),
        circuit_breaker=CircuitBreakerState(consecutive_loss_days=3).evaluate(),
    )
    engine.check_sell("005930")  # must not raise


def test_engine_records_trade():
    engine = _base_engine()
    engine.record_trade(TradeRecord(symbol="A", side="BUY", ts=datetime.now()))
    assert len(engine.trades_history) == 1


# --- @risk_gate decorator ---------------------------------------------------

def test_risk_gate_requires_engine():
    set_risk_engine(None)
    try:
        @risk_gate
        def place(**kwargs):
            return "ordered"

        with pytest.raises(RuntimeError, match="no RiskEngine"):
            place(symbol="005930", side="BUY", buy_context=_ctx())
    finally:
        set_risk_engine(None)


def test_risk_gate_enforces_symbol_kwarg():
    engine = _base_engine()
    set_risk_engine(engine)
    try:
        @risk_gate
        def place(**kwargs):
            return "ok"

        with pytest.raises(RuntimeError, match="symbol"):
            place(side="BUY", buy_context=_ctx())
    finally:
        set_risk_engine(None)


def test_risk_gate_buy_requires_buy_context():
    engine = _base_engine()
    set_risk_engine(engine)
    try:
        @risk_gate
        def place(**kwargs):
            return "ok"

        with pytest.raises(RuntimeError, match="buy_context"):
            place(symbol="005930", side="BUY")
    finally:
        set_risk_engine(None)


def test_risk_gate_unknown_side():
    engine = _base_engine()
    set_risk_engine(engine)
    try:
        @risk_gate
        def place(**kwargs):
            return "ok"

        with pytest.raises(RuntimeError, match="unknown side"):
            place(symbol="005930", side="HOLD")
    finally:
        set_risk_engine(None)


def test_risk_gate_allows_clean_buy_and_sell():
    engine = _base_engine()
    set_risk_engine(engine)
    try:
        calls: list[str] = []

        @risk_gate
        def place(*, symbol, side, **kwargs):
            calls.append(f"{side}:{symbol}")
            return "ok"

        assert place(symbol="005930", side="BUY", buy_context=_ctx()) == "ok"
        assert place(symbol="005930", side="SELL") == "ok"
        assert calls == ["BUY:005930", "SELL:005930"]
    finally:
        set_risk_engine(None)


def test_risk_gate_propagates_risk_blocked():
    engine = _base_engine(
        blacklist=StaticBlacklist.from_iterable([
            BlacklistEntry(symbol="005930", reason=MANAGED)
        ])
    )
    set_risk_engine(engine)
    try:
        @risk_gate
        def place(**kwargs):
            return "never"

        with pytest.raises(RiskBlocked) as ei:
            place(symbol="005930", side="BUY", buy_context=_ctx())
        assert ei.value.code == "blacklist"
    finally:
        set_risk_engine(None)


def test_get_set_risk_engine_roundtrip():
    assert get_risk_engine() is None
    e = _base_engine()
    set_risk_engine(e)
    try:
        assert get_risk_engine() is e
    finally:
        set_risk_engine(None)
    assert get_risk_engine() is None


def test_risk_blocked_repr_and_str():
    err = RiskBlocked("code1", "detail info")
    assert err.code == "code1"
    assert "code1" in str(err) and "detail info" in str(err)
    assert "code1" in repr(err)
