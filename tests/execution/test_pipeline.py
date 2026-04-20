"""Pipeline integration test — Registry → Risk → OrderManager → Portfolio."""
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.execution.order_manager import OrderManager
from src.execution.pipeline import TickContext, TradingPipeline
from src.execution.portfolio import LivePortfolio
from src.execution.reconciler import Reconciler
from src.persistence.models import Base
from src.risk.blacklist import BlacklistEntry, MANAGED, StaticBlacklist
from src.risk.limits import (
    CircuitBreakerState,
    CooldownPolicy,
    DailyLossState,
    RiskEngine,
)
from src.risk.market_state import MarketState
from src.risk.sizing import SizingPolicy
from src.strategy.base import Action, Signal, Strategy, StrategyContext
from src.strategy.registry import StrategyRegistry


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _kis():
    k = MagicMock()
    k.order.return_value = {"output": {"ODNO": "ORD1"}}
    k.get_stock_balance.return_value = ({}, {})
    return k


class _StubBuy(Strategy):
    strategy_id = "test_buy"

    def decide_entries(self, ctx):
        return [Signal(action=Action.BUY, symbol="A", strategy_id=self.strategy_id, notional=100_000, signal_id="s1")]

    def decide_exits(self, ctx):
        return []


def _risk(**over):
    defaults = dict(
        blacklist=StaticBlacklist(),
        market_state=MarketState(),
        daily_loss=DailyLossState(start_equity=10_000_000, current_equity=10_000_000),
        circuit_breaker=CircuitBreakerState(),
        sizing_policy=SizingPolicy(),
        cooldown_policy=CooldownPolicy(),
    )
    defaults.update(over)
    return RiskEngine(**defaults)


def _pipe(session, *, risk=None, kis=None):
    kis = kis or _kis()
    lp = LivePortfolio(session)
    reg = StrategyRegistry()
    reg.register(_StubBuy(), allocation_pct=50)
    om = OrderManager(kis_client=kis, portfolio=lp, now_provider=lambda: datetime(2026, 4, 20, 10))
    rec = Reconciler(session, kis)
    return TradingPipeline(
        registry=reg, risk_engine=risk or _risk(),
        order_manager=om, portfolio=lp, reconciler=rec,
    )


def test_tick_submits_buy(session):
    pipe = _pipe(session)
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    executed = pipe.run_tick(ctx)
    assert len(executed) == 1
    assert executed[0].action == Action.BUY


def test_tick_blocked_by_risk(session):
    risk = _risk(blacklist=StaticBlacklist.from_iterable([BlacklistEntry(symbol="A", reason=MANAGED)]))
    pipe = _pipe(session, risk=risk)
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    executed = pipe.run_tick(ctx)
    assert executed == []


def test_trading_off_flag_blocks_execution(session):
    pipe = _pipe(session)
    pipe.set_trading_off(True, reason="manual")
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    assert pipe.run_tick(ctx) == []


def test_reconciler_trading_off_propagates(session):
    # Reconciler 이 trading_off_required=True 를 반환하면 pipeline 자동 차단
    pipe = _pipe(session)
    # simulate 3 consecutive mismatches via direct counter manipulation
    pipe.reconciler._consecutive_failures = 5
    assert pipe.is_trading_off()
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    assert pipe.run_tick(ctx) == []


def test_zero_price_skipped(session):
    pipe = _pipe(session)
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 0},
    )
    assert pipe.run_tick(ctx) == []


# --- v3: risk_blocked RiskEvent 기록 ----------------------------------------

def test_risk_blocked_records_risk_event(session):
    """RiskBlocked 발생 시 RiskEvent(event_type='risk_blocked') 저장되는지."""
    from src.persistence.models import RiskEvent

    risk = _risk(blacklist=StaticBlacklist.from_iterable([
        BlacklistEntry(symbol="A", reason=MANAGED)
    ]))
    pipe = _pipe(session, risk=risk)
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    pipe.run_tick(ctx)
    session.commit()

    events = session.query(RiskEvent).filter_by(event_type="risk_blocked").all()
    assert len(events) == 1
    assert events[0].action_taken == "blacklist"   # RiskBlocked.code
    assert events[0].symbol == "A"
    assert "BUY A" in (events[0].detail or "")


def test_risk_blocked_reason_code_preserved(session):
    """code 별로 구분 가능해야 (blacklist vs daily_loss_limit vs cooldown)."""
    from src.persistence.models import RiskEvent

    # Daily Loss 임계 도달
    risk = _risk(daily_loss=DailyLossState(start_equity=10_000_000, current_equity=9_700_000))
    pipe = _pipe(session, risk=risk)
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    pipe.run_tick(ctx)
    session.commit()

    events = session.query(RiskEvent).filter_by(event_type="risk_blocked").all()
    assert len(events) == 1
    assert events[0].action_taken == "daily_loss_limit"
