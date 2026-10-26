"""Tick pipeline: analysis → strategy → risk_gate → order_manager.

각 영업일 본 트레이딩 루프의 "하나의 tick" 단위 실행 체인.

v4:
  - tick 시작 시 `set_risk_engine(self.risk)` — APScheduler 워커 스레드는 ContextVar 를
    상속하지 않으므로 tick 을 실행하는 스레드에서 매번 설치 (D14 @risk_gate 전제)
  - kill switch: 켜져 있으면 신규 매수만 차단, 청산은 진행
  - 보유 포지션 highest_price 를 매 tick 갱신 (D3 Trailing 영속화)
  - 유동성 게이트는 TickContext.liquidity 실데이터 사용, 없으면 fail-closed
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from typing import Callable

from datetime import datetime as _dt

from src.clients.kis import KisClient
from src.execution.order_manager import OrderManager
from src.execution.portfolio import LivePortfolio
from src.execution.reconciler import Reconciler
from src.persistence.models import RiskEvent
from src.risk.exceptions import RiskBlocked
from src.risk.limits import BuyContext, RiskEngine, set_risk_engine
from src.risk.sizing import resolve_quantity
from src.strategy.base import Action, Signal, StrategyContext
from src.strategy.registry import StrategyRegistry, build_idempotency_key
from src.common.clock import now_kst


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
    # symbol → (시가총액 원, 20일 평균 거래대금 원). 누락 종목은 매수 fail-closed.
    liquidity: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    # 값이 있으면 이번 tick 신규 매수만 차단 (예: 잔고 조회 실패로 직전값 사용). 청산은 진행
    block_entries_reason: str | None = None
    # 전략 확장 입력 (StrategyContext.extras 로 전달 — 예: news_scores, entry_blocked)
    extras: dict = field(default_factory=dict)


KillSwitch = Callable[[], bool]


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
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.registry = registry
        self.risk = risk_engine
        self.orders = order_manager
        self.portfolio = portfolio
        self.reconciler = reconciler
        self.strategy_ver = strategy_ver
        self.kill_switch = kill_switch
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
        # D14: 이 스레드에 엔진 설치 (OrderManager._place 의 @risk_gate 가 사용)
        set_risk_engine(self.risk)

        # Daily loss 갱신 (평가금 기준)
        if ctx.portfolio_value is not None:
            self.risk.daily_loss.current_equity = ctx.portfolio_value

        # pending 주문: 체결 동기화 · 타임아웃 재주문 · EOD 취소
        self.orders.poll(current_prices=ctx.current_prices)

        if self.is_trading_off():
            logger.warning("trading off: %s", self._trading_off_reason)
            return []

        # D3: 보유 포지션 고점 갱신 → Trailing 무장 상태가 재시작 후에도 유지
        for pos in list(self.portfolio.iter_open_positions()):
            px = ctx.current_prices.get(pos.symbol)
            if px:
                self.portfolio.update_highest_price(pos.symbol, pos.strategy_id, new_price=px)

        entries_allowed = not self._kill_switch_on()
        if ctx.block_entries_reason:
            logger.warning("new entries blocked this tick: %s", ctx.block_entries_reason)
            entries_allowed = False
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
            extras=dict(ctx.extras),
        )

        executed: list[Signal] = []
        for sig in self.registry.all_signals(strat_ctx):
            if sig.action == Action.BUY and not entries_allowed:
                continue
            buy_ctx = None
            if sig.action == Action.BUY:
                buy_ctx = self._build_buy_context(sig, ctx)
                sig, buy_ctx = self._clip_to_capital_cap(sig, buy_ctx)
            if not self._pass_risk(sig, ctx, buy_ctx):
                continue
            if not self._execute(sig, ctx, today, buy_ctx):
                continue
            executed.append(sig)
        return executed

    def _clip_to_capital_cap(self, sig: Signal, buy_ctx: BuyContext) -> tuple[Signal, BuyContext]:
        """매수 금액을 투입 상한 잔여분으로 축소. 잔여 0 이면 그대로 두어
        RiskEngine 의 capital_cap 게이트가 차단·기록하게 한다."""
        left = self.risk.capital_headroom(buy_ctx.existing_positions_notional)
        if left is None or left <= 0 or sig.notional <= left:
            return sig, buy_ctx
        logger.info("clip %s notional %.0f → %.0f (capital cap)", sig.symbol, sig.notional, left)
        return replace(sig, notional=left), replace(buy_ctx, intended_notional=left)

    def _kill_switch_on(self) -> bool:
        if self.kill_switch is None:
            return False
        try:
            on = bool(self.kill_switch())
        except Exception:
            logger.exception("kill switch check failed — treating as ON (fail-closed)")
            return True
        if on:
            logger.warning("kill switch ON — new entries blocked, exits continue")
        return on

    # ------------------------------------------------------------------
    def _pass_risk(self, sig: Signal, ctx: TickContext, buy_ctx: BuyContext | None) -> bool:
        try:
            if sig.action == Action.BUY:
                self.risk.check_buy(buy_ctx)
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
                ts=ts if isinstance(ts, _dt) else now_kst(),
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
        # 미체결 매수 주문도 투입액으로 본다 (상한·종목 20% 계산이 체결 전에 새지 않도록)
        for sym, amount in self.orders.pending_buy_notional().items():
            existing[sym] = existing.get(sym, 0.0) + amount
        # 섀도 모드: 송신하지 않은 당일 매수도 투입액으로 본다 (상한이 실전처럼 소진되도록)
        if self.orders.shadow is True:
            d = ctx.as_of.date()
            for sym, amount in self.portfolio.shadow_buy_notional(
                datetime.combine(d, time.min), datetime.combine(d, time(23, 59, 59)),
            ).items():
                existing[sym] = existing.get(sym, 0.0) + amount
        portfolio_value = ctx.portfolio_value if ctx.portfolio_value is not None else self.portfolio.equity(ctx.cash_available, ctx.current_prices)
        market_cap, avg_value = ctx.liquidity.get(sig.symbol, (None, None))
        return BuyContext(
            symbol=sig.symbol,
            portfolio_value=portfolio_value,
            existing_positions_notional=existing,
            market_cap=int(market_cap) if market_cap else None,
            avg_volume_krw=int(avg_value) if avg_value else None,
            intended_notional=sig.notional if sig.notional > 0 else None,
        )

    def _execute(
        self, sig: Signal, ctx: TickContext, today: date, buy_ctx: BuyContext | None
    ) -> bool:
        price = ctx.current_prices.get(sig.symbol, 0.0)
        if price <= 0:
            return False
        if sig.action == Action.BUY:
            qty = resolve_quantity(max_notional=sig.notional, price=price)
            if qty <= 0:
                # 금액이 1주 가격보다 작음 — 리포트 "실패·차단" 에 집계되도록 기록
                logger.info("skip BUY %s: notional %.0f < price %.0f (qty 0)",
                            sig.symbol, sig.notional, price)
                self._record_risk_block(sig, RiskBlocked(
                    "qty_zero", f"금액 {sig.notional:,.0f} < 가격 {price:,.0f}"), ctx.as_of)
                return False
        else:  # SELL
            qty = sig.qty
            if qty <= 0:
                return False
        idem = build_idempotency_key(sig, trade_date=today.isoformat())
        po = self.orders.submit(
            sig, limit_price=price, qty=qty,
            idempotency_key=idem, strategy_ver=self.strategy_ver,
            buy_context=buy_ctx,
        )
        if po is None:
            return False
        if sig.signal_id:
            self.registry.mark_executed([sig.signal_id])
        return True
