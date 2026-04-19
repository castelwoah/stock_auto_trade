"""Backtest engine — bar-by-bar event loop.

각 영업일마다:
  1. 09:00 start_equity 고정 → RiskEngine refresh (D7)
  2. 5분 간격 bar 순회:
     a. loader.prices_as_of / loader.news (D5 look-ahead 필터 loader 내장)
     b. (선택) score_provider 호출
     c. Registry.all_signals(StrategyContext)
     d. 각 시그널 → RiskEngine.check_buy/sell → PortfolioSim.fill
  3. 15:30 EOD mark_to_market

책임 분리:
  * 엔진은 오케스트레이션만 담당 — analysis/risk 는 의존성 주입으로 받음.
  * D5 look-ahead 필터는 data_loader 에만 존재.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable

from src.backtest.data_loader import BacktestDataLoader
from src.backtest.portfolio_sim import PortfolioSim, SimFill, SimSkip
from src.risk.exceptions import RiskBlocked
from src.risk.limits import BuyContext, RiskEngine
from src.strategy.base import Action, Signal, StrategyContext
from src.strategy.registry import StrategyRegistry, build_idempotency_key


logger = logging.getLogger(__name__)


# --- time grid -------------------------------------------------------------

DEFAULT_BAR_INTERVAL_MIN = 5
TRADING_START = time(9, 10)   # rules §5.1: 09:10 이후 신호 생성
TRADING_END = time(15, 20)    # EOD 직전까지


def generate_bar_times(
    *, start: time = TRADING_START, end: time = TRADING_END, step_min: int = DEFAULT_BAR_INTERVAL_MIN
) -> list[time]:
    out: list[time] = []
    cur = datetime.combine(date.today(), start)
    stop = datetime.combine(date.today(), end)
    while cur <= stop:
        out.append(cur.time())
        cur += timedelta(minutes=step_min)
    return out


def business_days(start: date, end: date) -> list[date]:
    """Mon-Fri. 공휴일은 별도 캘린더 (W8 scheduler/calendar.py) 에서 주입."""
    out: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


# --- config + result --------------------------------------------------------

@dataclass(frozen=True)
class BacktestConfig:
    start_date: date
    end_date: date
    symbols: list[str]
    initial_cash: float = 10_000_000.0
    bar_interval_min: int = DEFAULT_BAR_INTERVAL_MIN


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: list[tuple[datetime, float]]
    fills: list[SimFill]
    skips: list[SimSkip]
    trading_days: int
    final_equity: float

    @property
    def total_return_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        initial = self.equity_curve[0][1]
        return (self.final_equity / initial - 1.0) * 100.0 if initial else 0.0


# --- engine -----------------------------------------------------------------

ScoreProvider = Callable[[datetime, list[str]], dict[str, float]]
PrevDayOhlcProvider = Callable[[date, list[str]], dict[str, dict[str, float]]]
RiskEngineBuilder = Callable[[float], RiskEngine]  # start_equity → engine


class BacktestEngine:
    def __init__(
        self,
        *,
        config: BacktestConfig,
        loader: BacktestDataLoader,
        registry: StrategyRegistry,
        sim: PortfolioSim,
        risk_engine_builder: RiskEngineBuilder,
        score_provider: ScoreProvider | None = None,
        prev_day_ohlc_provider: PrevDayOhlcProvider | None = None,
    ) -> None:
        self.config = config
        self.loader = loader
        self.registry = registry
        self.sim = sim
        self.risk_engine_builder = risk_engine_builder
        self.score_provider = score_provider
        self.prev_day_ohlc_provider = prev_day_ohlc_provider
        self._bar_times = generate_bar_times(step_min=config.bar_interval_min)

    # ------------------------------------------------------------------

    def run(self) -> BacktestResult:
        days = business_days(self.config.start_date, self.config.end_date)
        for d in days:
            self._run_day(d)
        eq_curve = list(self.sim.equity_curve)
        return BacktestResult(
            equity_curve=eq_curve,
            fills=list(self.sim.fills),
            skips=list(self.sim.skips),
            trading_days=len(days),
            final_equity=eq_curve[-1][1] if eq_curve else self.sim.cash,
        )

    # ------------------------------------------------------------------

    def _run_day(self, d: date) -> None:
        open_ts = datetime.combine(d, time(9, 0))
        open_prices = self.loader.prices_as_of(open_ts, self.config.symbols)
        start_equity = self.sim.equity(open_prices)
        # D7: start_equity 를 09:00 로 고정해서 RiskEngine 재구성
        risk_engine = self.risk_engine_builder(start_equity)

        for bar_time in self._bar_times:
            bar_ts = datetime.combine(d, bar_time)
            prices = self.loader.prices_as_of(bar_ts, self.config.symbols)
            if not prices:
                continue

            scores: dict[str, float] = {}
            if self.score_provider is not None:
                scores = self.score_provider(bar_ts, self.config.symbols)

            prev_day_ohlc: dict[str, dict[str, float]] = {}
            if self.prev_day_ohlc_provider is not None:
                prev_day_ohlc = self.prev_day_ohlc_provider(d, self.config.symbols)

            ctx = StrategyContext(
                as_of=bar_ts,
                today=d,
                cash_available=self.sim.cash,
                positions=self.sim.current_positions_as_state(prices=prices),
                candidate_scores=scores,
                current_prices=prices,
                prev_day_ohlc=prev_day_ohlc,
            )

            signals = self.registry.all_signals(ctx)
            for sig in signals:
                if not self._pass_risk(risk_engine, sig, prices, bar_ts):
                    continue
                self._execute(sig, prices, bar_ts, d)

            # 틱마다 DailyLossState.current_equity 갱신
            risk_engine.daily_loss.current_equity = self.sim.equity(prices)

        # EOD
        eod_ts = datetime.combine(d, time(15, 30))
        eod_prices = self.loader.prices_as_of(eod_ts, self.config.symbols) or open_prices
        self.sim.mark_to_market(eod_prices, ts=eod_ts)

    # ------------------------------------------------------------------

    def _pass_risk(
        self, risk_engine: RiskEngine, sig: Signal, prices: dict[str, float], bar_ts: datetime
    ) -> bool:
        try:
            if sig.action == Action.BUY:
                risk_engine.check_buy(self._build_buy_context(sig, prices))
            elif sig.action == Action.SELL:
                risk_engine.check_sell(sig.symbol)
            else:
                return False
            return True
        except RiskBlocked as rb:
            self.sim.skips.append(SimSkip(ts=bar_ts, signal=sig, reason=f"risk:{rb.code}"))
            return False

    def _build_buy_context(self, sig: Signal, prices: dict[str, float]) -> BuyContext:
        portfolio_value = self.sim.equity(prices)
        existing: dict[str, float] = {}
        for (sym, _sid), pos in self.sim.positions.items():
            p = prices.get(sym, pos.avg_price)
            existing[sym] = existing.get(sym, 0.0) + pos.qty * p
        return BuyContext(
            symbol=sig.symbol,
            portfolio_value=portfolio_value,
            existing_positions_notional=existing,
            # 유동성·시총은 backtest 에선 생략 (실 데이터에서 주입) → 기본 충족 시총/거래대금
            market_cap=50_000_000_000,  # 최저 임계와 동일 (통과 허용)
            avg_volume_krw=1_000_000_000,
            intended_notional=sig.notional if sig.notional > 0 else None,
        )

    def _execute(self, sig: Signal, prices: dict[str, float], bar_ts: datetime, d: date) -> None:
        bar_px = prices.get(sig.symbol, 0.0)
        if bar_px <= 0:
            return
        idem = build_idempotency_key(sig, trade_date=d.isoformat())
        fill = self.sim.fill(sig, ts=bar_ts, bar_price=bar_px, idempotency_key=idem)
        if fill is not None and fill.signal_id:
            self.registry.mark_executed([fill.signal_id])
