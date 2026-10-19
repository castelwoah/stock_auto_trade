"""End-to-end backtest engine integration.

SyntheticDataLoader + Registry + RiskEngine + PortfolioSim 을 실제로 연결해
하루 이상 이벤트 루프가 돌고, equity curve / fills / skips 가 올바르게
쌓이는지 확인한다.
"""
from datetime import date, datetime, time, timedelta
from typing import Callable

import pytest

from src.backtest.data_loader import SyntheticDataLoader
from src.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    business_days,
    generate_bar_times,
)
from src.backtest.metrics import compute_metrics
from src.backtest.portfolio_sim import PortfolioSim
from src.risk.blacklist import StaticBlacklist
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


# --- helpers ---------------------------------------------------------------

def _risk_engine_builder(start_equity: float) -> RiskEngine:
    return RiskEngine(
        blacklist=StaticBlacklist(),
        market_state=MarketState(),
        daily_loss=DailyLossState(start_equity=start_equity, current_equity=start_equity),
        circuit_breaker=CircuitBreakerState(),
        sizing_policy=SizingPolicy(),
        cooldown_policy=CooldownPolicy(),
        trades_history=[],
    )


class _AlwaysBuyStrategy(Strategy):
    """Test-only: 후보 종목 1개를 매 bar 마다 buy 시도 (이미 보유면 스킵)."""

    strategy_id = "test_always_buy"

    def __init__(self, symbol: str, per_notional: float = 100_000) -> None:
        super().__init__()
        self.symbol = symbol
        self.per_notional = per_notional

    def decide_entries(self, ctx: StrategyContext) -> list[Signal]:
        if self.symbol in ctx.positions:
            return []
        if self.symbol not in ctx.current_prices:
            return []
        if ctx.cash_available < self.per_notional:
            return []
        return [Signal(
            action=Action.BUY,
            symbol=self.symbol,
            strategy_id=self.strategy_id,
            notional=self.per_notional,
            signal_id=f"{self.symbol}-{ctx.as_of.isoformat()}",
        )]

    def decide_exits(self, ctx: StrategyContext) -> list[Signal]:
        return []


# --- time grid utilities ----------------------------------------------------

def test_business_days_skips_weekends():
    days = business_days(date(2026, 4, 17), date(2026, 4, 21))  # Fri → Tue
    assert days == [date(2026, 4, 17), date(2026, 4, 20), date(2026, 4, 21)]


def test_generate_bar_times_default_grid():
    times = generate_bar_times()
    assert times[0] == time(9, 10)
    assert time(15, 20) in times
    # 5분 간격
    assert (times[1].hour, times[1].minute) == (9, 15)


# --- full engine run --------------------------------------------------------

def _build_engine(symbols, start, end, *, initial_cash=1_000_000) -> BacktestEngine:
    # 백테스트 첫날 09:10 에도 prior close 가 존재하도록 로더 범위를 앞으로 확장.
    # SyntheticDataLoader 는 하루 1봉(15:30 기준) 이므로 직전 영업일 bar 필요.
    loader_start = start - timedelta(days=7)
    loader = SyntheticDataLoader(symbols=symbols, start=loader_start, end=end, seed=7)
    sim = PortfolioSim(initial_cash)
    reg = StrategyRegistry()
    reg.register(_AlwaysBuyStrategy(symbols[0], per_notional=50_000), allocation_pct=50)
    config = BacktestConfig(
        start_date=start, end_date=end, symbols=symbols,
        initial_cash=initial_cash, bar_interval_min=30,  # 속도 위해 30분 간격
        assume_liquid=True,   # 합성 데이터 — 유동성 정보 없음
    )
    return BacktestEngine(
        config=config, loader=loader, registry=reg, sim=sim,
        risk_engine_builder=_risk_engine_builder,
    )


def test_engine_run_single_day_produces_equity_curve():
    eng = _build_engine(["AAA"], date(2026, 4, 20), date(2026, 4, 20))
    result = eng.run()
    assert isinstance(result, BacktestResult)
    assert result.trading_days == 1
    assert len(result.equity_curve) == 1
    assert result.final_equity > 0


def test_engine_run_multi_day():
    eng = _build_engine(["AAA"], date(2026, 4, 20), date(2026, 4, 24))
    result = eng.run()
    # Mon-Fri = 5 business days
    assert result.trading_days == 5
    assert len(result.equity_curve) == 5


def test_engine_records_fills_and_skips():
    eng = _build_engine(["AAA"], date(2026, 4, 20), date(2026, 4, 22))
    result = eng.run()
    assert len(result.fills) >= 1  # 첫 날 최소 1회 매수
    # 이미 보유 상태에서 추가 매수 시도 → decide_entries 에서 스킵되므로 실제 fills 는 ≤ 3
    assert len(result.fills) <= 3


def test_engine_weekend_skipped():
    # Fri → Mon (주말 포함 4일 범위, 영업일 2일)
    eng = _build_engine(["AAA"], date(2026, 4, 17), date(2026, 4, 20))
    result = eng.run()
    assert result.trading_days == 2


def test_engine_metrics_pipeline():
    eng = _build_engine(["AAA"], date(2026, 4, 20), date(2026, 4, 24), initial_cash=1_000_000)
    result = eng.run()
    m = compute_metrics(
        result.equity_curve,
        result.fills,
        initial_cash=eng.config.initial_cash,
    )
    # 짧은 기간이라 Sharpe 는 의미 없지만 숫자는 계산되어야 함
    assert isinstance(m.total_return_pct, float)
    assert isinstance(m.max_drawdown_pct, float)
    assert m.max_drawdown_pct <= 0


def test_engine_marks_signal_executed():
    eng = _build_engine(["AAA"], date(2026, 4, 20), date(2026, 4, 20))
    result = eng.run()
    # 같은 signal_id 가 여러 bar 에서 반복 emit 되지만 registry.mark_executed 로 차단 → fill 1건만
    fills = [f for f in result.fills if f.side == "BUY"]
    # 하루 내 30분 간격 최대 13 bar. 그러나 첫 fill 이후 decide_entries 가 이미 보유로 스킵.
    assert len(fills) == 1


def test_engine_risk_blocked_appears_in_skips():
    """블랙리스트에 등록된 종목은 모든 BUY 가 skips 에 기록되어야."""
    from src.risk.blacklist import BlacklistEntry, MANAGED, StaticBlacklist

    loader = SyntheticDataLoader(
        symbols=["AAA"], start=date(2026, 4, 13), end=date(2026, 4, 20), seed=1
    )
    sim = PortfolioSim(1_000_000)
    reg = StrategyRegistry()
    reg.register(_AlwaysBuyStrategy("AAA", per_notional=50_000), allocation_pct=50)
    config = BacktestConfig(
        start_date=date(2026, 4, 20), end_date=date(2026, 4, 20),
        symbols=["AAA"], initial_cash=1_000_000, bar_interval_min=30,
        assume_liquid=True,
    )

    def _bl_engine(start_equity):
        return RiskEngine(
            blacklist=StaticBlacklist.from_iterable([BlacklistEntry(symbol="AAA", reason=MANAGED)]),
            market_state=MarketState(),
            daily_loss=DailyLossState(start_equity=start_equity, current_equity=start_equity),
            circuit_breaker=CircuitBreakerState(),
            sizing_policy=SizingPolicy(),
            cooldown_policy=CooldownPolicy(),
            trades_history=[],
        )

    eng = BacktestEngine(
        config=config, loader=loader, registry=reg, sim=sim,
        risk_engine_builder=_bl_engine,
    )
    result = eng.run()
    assert len(result.fills) == 0  # 블랙리스트 차단
    assert any("risk:blacklist" in s.reason for s in result.skips)



def test_engine_without_liquidity_data_blocks_buys():
    """유동성 정보가 없고 assume_liquid 도 아니면 매수는 risk:liquidity 로 스킵 (실전과 동일)."""
    from dataclasses import replace

    eng = _build_engine(["AAA"], date(2026, 4, 20), date(2026, 4, 24))
    eng.config = replace(eng.config, assume_liquid=False)
    result = eng.run()
    assert not any(f.side == "BUY" for f in result.fills)
    assert any(s.reason == "risk:liquidity" for s in result.skips)


def test_engine_uses_provided_liquidity():
    from dataclasses import replace

    eng = _build_engine(["AAA"], date(2026, 4, 20), date(2026, 4, 24))
    eng.config = replace(eng.config, assume_liquid=False, liquidity={"AAA": (1e12, 1e11)})
    result = eng.run()
    assert not any(s.reason == "risk:liquidity" for s in result.skips)
