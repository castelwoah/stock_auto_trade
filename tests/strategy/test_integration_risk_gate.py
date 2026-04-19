"""W5 integration — Registry → Strategy Signal → @risk_gate 파이프라인.

전략 계층과 리스크 계층이 `strategy_id` 전파와 9단계 게이트를 통해 정상
결합되는지 엔드투엔드 검증한다.
"""
from datetime import date, datetime

import pytest

from src.risk.blacklist import BlacklistEntry, MANAGED, StaticBlacklist
from src.risk.exceptions import RiskBlocked
from src.risk.limits import (
    BuyContext,
    CircuitBreakerState,
    CooldownPolicy,
    DailyLossState,
    RiskEngine,
    risk_gate,
    set_risk_engine,
)
from src.risk.market_state import MarketState
from src.risk.sizing import SizingPolicy
from src.strategy.base import Action, PositionState, StrategyContext
from src.strategy.breakout import BreakoutStrategy
from src.strategy.news_swing import NewsSwingStrategy
from src.strategy.registry import StrategyRegistry, build_idempotency_key


def _engine(**over):
    base = dict(
        blacklist=StaticBlacklist(),
        market_state=MarketState(),
        daily_loss=DailyLossState(start_equity=10_000_000, current_equity=10_000_000),
        circuit_breaker=CircuitBreakerState(),
        sizing_policy=SizingPolicy(),
        cooldown_policy=CooldownPolicy(),
        trades_history=[],
        now_provider=lambda: datetime(2026, 4, 19, 10, 0, 0),
    )
    base.update(over)
    return RiskEngine(**base)


def _buy_ctx(symbol, notional):
    return BuyContext(
        symbol=symbol,
        portfolio_value=10_000_000,
        existing_positions_notional={},
        market_cap=100_000_000_000,
        avg_volume_krw=5_000_000_000,
        intended_notional=notional,
    )


def _global_ctx(**over):
    base = dict(
        as_of=datetime(2026, 4, 19, 10, 0, 0),
        today=date(2026, 4, 19),
        cash_available=10_000_000,
        positions={},
        candidate_scores={"005930": 85.0, "000660": 78.0, "035720": 72.0},
        current_prices={"005930": 75_000, "000660": 120_000, "035720": 55_000},
        prev_day_ohlc={
            "005930": {"open_today": 74_000, "high": 76_000, "low": 73_000},
        },
    )
    base.update(over)
    return StrategyContext(**base)


def test_registry_routes_strategy_id_correctly():
    reg = StrategyRegistry()
    reg.register(BreakoutStrategy(max_positions=2), allocation_pct=20)
    reg.register(NewsSwingStrategy(target_position_count=3), allocation_pct=70)
    signals = reg.all_signals(_global_ctx())
    # 각 signal 의 strategy_id 가 라우팅된 슬롯과 일치
    for s in signals:
        assert s.strategy_id in {"breakout", "news_swing"}


def test_news_swing_gets_allocated_cash():
    reg = StrategyRegistry()
    reg.register(NewsSwingStrategy(target_position_count=3), allocation_pct=50)
    # cash_available 10M → 50% = 5M → per_position = 5M/3
    ctx = _global_ctx()
    signals = reg.all_signals(ctx)
    buys = [s for s in signals if s.action == Action.BUY]
    assert len(buys) == 3
    for s in buys:
        assert abs(s.notional - (10_000_000 * 0.5 / 3)) < 1


def test_idempotency_key_includes_strategy_id():
    reg = StrategyRegistry()
    reg.register(NewsSwingStrategy(target_position_count=3), allocation_pct=100)
    signals = reg.all_signals(_global_ctx())
    buys = [s for s in signals if s.action == Action.BUY]
    for s in buys:
        key = build_idempotency_key(s, trade_date="2026-04-19")
        assert "news_swing" in key
        assert s.symbol in key
        assert "2026-04-19" in key
    # 서로 다른 심볼의 key 는 유니크
    keys = {build_idempotency_key(s, trade_date="2026-04-19") for s in buys}
    assert len(keys) == len(buys)


def test_risk_gate_allows_valid_strategy_signal():
    engine = _engine()
    set_risk_engine(engine)
    try:
        executed = []

        @risk_gate
        def execute(*, symbol, side, buy_context=None, **_):
            executed.append((symbol, side))
            return "ok"

        assert execute(
            symbol="005930", side="BUY", buy_context=_buy_ctx("005930", 1_500_000)
        ) == "ok"
        assert executed == [("005930", "BUY")]
    finally:
        set_risk_engine(None)


def test_risk_gate_blocks_blacklisted_strategy_signal():
    engine = _engine(
        blacklist=StaticBlacklist.from_iterable([
            BlacklistEntry(symbol="005930", reason=MANAGED)
        ])
    )
    set_risk_engine(engine)
    try:
        @risk_gate
        def execute(**kw):
            return "should-not-run"

        with pytest.raises(RiskBlocked) as ei:
            execute(symbol="005930", side="BUY", buy_context=_buy_ctx("005930", 1_500_000))
        assert ei.value.code == "blacklist"
    finally:
        set_risk_engine(None)


def test_daily_loss_blocks_new_entries_but_not_exits():
    engine = _engine(
        daily_loss=DailyLossState(start_equity=10_000_000, current_equity=9_700_000)
    )
    set_risk_engine(engine)
    try:
        @risk_gate
        def execute(**kw):
            return "ok"

        # Entry blocked
        with pytest.raises(RiskBlocked) as ei:
            execute(symbol="005930", side="BUY", buy_context=_buy_ctx("005930", 1_500_000))
        assert ei.value.code == "daily_loss_limit"

        # But SELL still allowed (risk reduction path)
        assert execute(symbol="005930", side="SELL") == "ok"
    finally:
        set_risk_engine(None)


def test_breakout_eod_signal_flows_to_gate_as_sell():
    reg = StrategyRegistry()
    reg.register(BreakoutStrategy(), allocation_pct=30)

    owned = PositionState(
        symbol="005930", strategy_id="breakout", qty=10, avg_price=75_000, current_price=76_000
    )
    ctx = _global_ctx(
        as_of=datetime(2026, 4, 19, 15, 15, 0),  # EOD
        positions={"005930": owned},
    )
    signals = reg.all_signals(ctx)
    sells = [s for s in signals if s.action == Action.SELL]
    assert any(s.strategy_id == "breakout" and s.symbol == "005930" for s in sells)

    engine = _engine()
    set_risk_engine(engine)
    try:
        # SELL 은 정지 아닌 이상 항상 통과
        engine.check_sell("005930")
    finally:
        set_risk_engine(None)


def test_registry_exits_precede_entries_in_risk_pipeline():
    """리스크 저감(매도) 이 먼저 흘러 cash 확보 후 신규 진입 가능."""
    reg = StrategyRegistry()
    reg.register(NewsSwingStrategy(target_position_count=3), allocation_pct=100)

    losing_pos = PositionState(
        symbol="035720", strategy_id="news_swing", qty=10, avg_price=60_000, current_price=55_000
    )  # -8.3% → stop_loss
    ctx = _global_ctx(positions={"035720": losing_pos})
    signals = reg.all_signals(ctx)

    # 첫 시그널은 SELL 이어야 (exits 우선)
    assert signals[0].action == Action.SELL
    assert signals[0].strategy_id == "news_swing"
