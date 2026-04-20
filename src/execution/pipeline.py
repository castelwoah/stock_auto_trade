"""Tick pipeline: analysis → strategy → risk_gate → order_manager.

각 영업일 본 트레이딩 루프의 "하나의 tick" 단위 실행 체인.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from datetime import datetime as _dt

from src.clients.kis import KisClient
from src.execution.order_manager import OrderManager
from src.execution.portfolio import LivePortfolio
from src.execution.reconciler import Reconciler
from src.persistence.models import RiskEvent
from src.risk.exceptions import RiskBlocked
from src.risk.limits import BuyContext, RiskEngine
from src.risk.sizing import resolve_quantity
from src.strategy.base import Action, Signal, StrategyContext
from src.strategy.registry import StrategyRegistry, build_idempotency_key


logger = logging.getLogger(__name__)


ScoreProvider = Callable[[datetime, list[str]], dict[str, float]]


@dataclass
class TickContext:
    """틱당 주입되는 외부 상태 스냅샷."""

    as_of: datetime
    cash_available: float
    current_prices: dict[str, float]
    candidate_scores: dict[str, float] = field(default_factory=dict)
    prev_day_ohlc: dict[str, dict[str, float]] = field(default_factory=dict)
    portfolio_value: float | None = None


class TradingPipeline:
    """Registry → Risk → Order 전체 체인. 상위 Scheduler 가 매 tick 호출."""

    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        risk_engine: RiskEngine,
        order_manager: OrderManager,
        portfolio: LivePortfolio,
        reconciler: Reconciler,
        strategy_ver: str = "v1.0",
    ) -> None:
        self.registry = registry
        self.risk = risk_engine
        self.orders = order_manager
        self.portfolio = portfolio
        self.reconciler = reconciler
        self.strategy_ver = strategy_ver
        self._trading_off = False
        self._trading_off_reason: str | None = None

    # ------------------------------------------------------------------
    # Hard gates
    # ------------------------------------------------------------------
    def set_trading_off(self, flag: bool, *, reason: str = "") -> None:
        self._trading_off = flag
        self._trading_off_reason = reason if flag else None

    def is_trading_off(self) -> bool:
        return self._trading_off or self.reconciler.trading_off_required()

    # ------------------------------------------------------------------
    # Per-tick run
    # ------------------------------------------------------------------
    def run_tick(self, ctx: TickContext) -> list[Signal]:
        """Generate + filter + submit signals. 실행된 시그널을 반환."""
        # Daily loss 갱신 (평가금 기준)
        if ctx.portfolio_value is not None:
            self.risk.daily_loss.current_equity = ctx.portfolio_value

        # pending 주문 폴링 (타임아웃 재시도·EOD 취소)
        self.orders.poll(current_prices=ctx.current_prices)

        if self.is_trading_off():
            logger.warning("trading off: %s", self._trading_off_reason)
            return []

        positions_state = self.portfolio.positions_as_state(current_prices=ctx.current_prices)
        today = ctx.as_of.date()
        strat_ctx = StrategyContext(
            as_of=ctx.as_of,
            today=today,
            cash_available=ctx.cash_available,
            positions=positions_state,
            candidate_scores=ctx.candidate_scores,
            current_prices=ctx.current_prices,
            prev_day_ohlc=ctx.prev_day_ohlc,
        )

        executed: list[Signal] = []
        for sig in self.registry.all_signals(strat_ctx):
            if not self._pass_risk(sig, ctx):
                continue
            if not self._execute(sig, ctx, today):
                continue
            executed.append(sig)
        return executed

    # ------------------------------------------------------------------
    def _pass_risk(self, sig: Signal, ctx: TickContext) -> bool:
        try:
            if sig.action == Action.BUY:
                self.risk.check_buy(self._build_buy_context(sig, ctx))
            elif sig.action == Action.SELL:
                self.risk.check_sell(sig.symbol)
            else:
                return False
            return True
        except RiskBlocked as rb:
            logger.info("risk blocked %s %s: %s", sig.action.value, sig.symbol, rb)
            # v3: RiskEvent 에 기록 — daily/midday 리포트에서 집계
            # 스키마 재사용 (rules §4.2 준수): event_type='risk_blocked',
            # action_taken 에 세부 code(blacklist / daily_loss_limit / ...) 저장
            self._record_risk_block(sig, rb, ctx.as_of)
            return False

    def _record_risk_block(self, sig: Signal, rb: RiskBlocked, ts) -> None:
        try:
            event = RiskEvent(
                ts=ts if isinstance(ts, _dt) else _dt.now(),
                event_type="risk_blocked",
                symbol=sig.symbol,
                detail=f"{sig.action.value} {sig.symbol} @{sig.strategy_id}: {rb.detail}",
                action_taken=rb.code,
            )
            self.portfolio.session.add(event)
            self.portfolio.session.flush()
        except Exception:
            logger.exception("failed to record risk_blocked event for %s", sig.symbol)

    def _build_buy_context(self, sig: Signal, ctx: TickContext) -> BuyContext:
        existing: dict[str, float] = {}
        for pos in self.portfolio.iter_open_positions():
            p = ctx.current_prices.get(pos.symbol, pos.avg_price)
            existing[pos.symbol] = existing.get(pos.symbol, 0.0) + pos.qty * p
        portfolio_value = ctx.portfolio_value if ctx.portfolio_value is not None else self.portfolio.equity(ctx.cash_available, ctx.current_prices)
        return BuyContext(
            symbol=sig.symbol,
            portfolio_value=portfolio_value,
            existing_positions_notional=existing,
            market_cap=50_000_000_000,
            avg_volume_krw=1_000_000_000,
            intended_notional=sig.notional if sig.notional > 0 else None,
        )

    def _execute(self, sig: Signal, ctx: TickContext, today: date) -> bool:
        price = ctx.current_prices.get(sig.symbol, 0.0)
        if price <= 0:
            return False
        if sig.action == Action.BUY:
            qty = resolve_quantity(max_notional=sig.notional, price=price)
            if qty <= 0:
                return False
        else:  # SELL
            qty = sig.qty
            if qty <= 0:
                return False
        idem = build_idempotency_key(sig, trade_date=today.isoformat())
        po = self.orders.submit(
            sig, limit_price=price, qty=qty,
            idempotency_key=idem, strategy_ver=self.strategy_ver,
        )
        if po is None:
            return False
        if sig.signal_id:
            self.registry.mark_executed([sig.signal_id])
        return True
