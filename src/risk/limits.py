"""Daily loss limit, Circuit Breaker, cooldown, and the @risk_gate decorator.

Invariants (rules §3.1):
  - Daily Loss Limit: -3% 도달 시 당일 신규 매수·추가 매수 전면 중단.
  - Circuit Breaker : 연속 3일 손실 OR 누적 -10% 도달 시 시스템 자동 정지.
                      수동 재가동 필요 (해제 플래그는 외부 관리).
  - 쿨다운          : 매도 후 재매수 3 거래일 / 손절 후 재진입 10 거래일.

모든 pure 함수는 side-effect 없이 (ok, reason) 반환.
RiskEngine 은 이들을 오케스트레이션하고 예외를 발생시킨다.
"""
from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import wraps
from typing import Any, Callable, Iterable

from src.risk.blacklist import (
    BlacklistProvider,
    LiquidityThresholds,
    check_liquidity,
)
from src.risk.exceptions import RiskBlocked
from src.risk.market_state import (
    MarketState,
    can_enter_symbol,
    can_exit_symbol,
    can_trade_today,
)
from src.risk.sizing import SizingPolicy, compute_max_additional_notional


# ---------------------------------------------------------------------------
# Daily loss
# ---------------------------------------------------------------------------

DAILY_LOSS_LIMIT_PCT = -3.0  # rules §3.1


@dataclass
class DailyLossState:
    """Snapshot for the current trading day.

    start_equity 는 D7 에 따라 당일 09:00 조회값으로 고정. 입·출금은 cash_flow
    테이블로 분리했으므로 여기서는 반영하지 않는다.
    """

    start_equity: float
    current_equity: float

    @property
    def loss_pct(self) -> float:
        if self.start_equity <= 0:
            return 0.0
        return (self.current_equity - self.start_equity) / self.start_equity * 100.0


def check_daily_loss(
    state: DailyLossState, *, limit_pct: float = DAILY_LOSS_LIMIT_PCT
) -> tuple[bool, str]:
    if state.loss_pct <= limit_pct:
        return False, f"일일 손실 {state.loss_pct:.2f}% ≤ {limit_pct}%"
    return True, ""


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

CB_CONSECUTIVE_LOSS_DAYS = 3
CB_CUMULATIVE_LOSS_PCT = -10.0


@dataclass
class CircuitBreakerState:
    triggered: bool = False
    reason: str = ""
    consecutive_loss_days: int = 0
    cumulative_pnl_pct: float = 0.0

    def evaluate(
        self,
        *,
        loss_days_limit: int = CB_CONSECUTIVE_LOSS_DAYS,
        cumulative_limit: float = CB_CUMULATIVE_LOSS_PCT,
    ) -> "CircuitBreakerState":
        """Recompute `triggered` / `reason` from current counters."""
        if self.consecutive_loss_days >= loss_days_limit:
            self.triggered = True
            self.reason = f"연속 {self.consecutive_loss_days}일 손실 (≥ {loss_days_limit})"
        elif self.cumulative_pnl_pct <= cumulative_limit:
            self.triggered = True
            self.reason = f"누적 {self.cumulative_pnl_pct:.2f}% (≤ {cumulative_limit}%)"
        return self

    def reset(self) -> None:
        """Manual resume — operator action only."""
        self.triggered = False
        self.reason = ""
        self.consecutive_loss_days = 0
        self.cumulative_pnl_pct = 0.0


def check_circuit_breaker(state: CircuitBreakerState) -> tuple[bool, str]:
    if state.triggered:
        return False, state.reason or "Circuit Breaker triggered"
    return True, ""


# ---------------------------------------------------------------------------
# Cooldown (rules §5.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CooldownPolicy:
    rebuy_days: int = 3              # 일반 매도 후 재매수 금지 기간 (거래일)
    stop_loss_reentry_days: int = 10 # 손절 종료 후 재진입 금지 기간


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    side: str           # BUY / SELL
    ts: datetime
    was_stop_loss: bool = False


def _business_days_between(d1: date, d2: date) -> int:
    """Mon-Fri 만 카운트 (공휴일은 별도 캘린더 없이 단순 평일 기준).

    정식 KRX 휴장일은 W6+ 에서 캘린더 모듈로 교체 예정.
    """
    if d2 < d1:
        d1, d2 = d2, d1
    days = 0
    cur = d1
    while cur < d2:
        cur = cur.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            days += 1
    return days


def check_cooldown(
    *,
    symbol: str,
    trades: Iterable[TradeRecord],
    now: datetime,
    policy: CooldownPolicy = CooldownPolicy(),
) -> tuple[bool, str]:
    """최근 매도 이력에 따라 재매수 가능 여부 판단."""
    last_sell: TradeRecord | None = None
    for t in trades:
        if t.symbol == symbol and t.side.upper() == "SELL":
            if last_sell is None or t.ts > last_sell.ts:
                last_sell = t
    if last_sell is None:
        return True, ""

    elapsed = _business_days_between(last_sell.ts.date(), now.date())
    required = (
        policy.stop_loss_reentry_days if last_sell.was_stop_loss else policy.rebuy_days
    )
    if elapsed < required:
        kind = "손절 후 재진입" if last_sell.was_stop_loss else "재매수"
        return False, f"{kind} 쿨다운 {elapsed}/{required} 거래일"
    return True, ""


# ---------------------------------------------------------------------------
# RiskEngine — orchestrator
# ---------------------------------------------------------------------------

@dataclass
class BuyContext:
    """State snapshot needed to evaluate a BUY request."""

    symbol: str
    portfolio_value: float
    existing_positions_notional: dict[str, float] = field(default_factory=dict)
    sector: str | None = None
    sector_map: dict[str, str] | None = None
    market_cap: int | None = None
    avg_volume_krw: int | None = None
    intended_notional: float | None = None


class RiskEngine:
    """Aggregates all gates. `check_buy` raises RiskBlocked on first failure.

    Construction is pure DI — no global state, trivially testable.
    """

    def __init__(
        self,
        *,
        blacklist: BlacklistProvider,
        market_state: MarketState,
        daily_loss: DailyLossState,
        circuit_breaker: CircuitBreakerState,
        sizing_policy: SizingPolicy = SizingPolicy(),
        cooldown_policy: CooldownPolicy = CooldownPolicy(),
        liquidity_thresholds: LiquidityThresholds = LiquidityThresholds(),
        trades_history: Iterable[TradeRecord] = (),
        now_provider: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.blacklist = blacklist
        self.market_state = market_state
        self.daily_loss = daily_loss
        self.circuit_breaker = circuit_breaker
        self.sizing_policy = sizing_policy
        self.cooldown_policy = cooldown_policy
        self.liquidity_thresholds = liquidity_thresholds
        self.trades_history: list[TradeRecord] = list(trades_history)
        self._now = now_provider

    # ------------------------------------------------------------------
    # BUY
    # ------------------------------------------------------------------
    def check_buy(self, ctx: BuyContext) -> None:
        # Ordering: highest-precedence gates first (system-wide → symbol-specific).
        cb_ok, cb_reason = check_circuit_breaker(self.circuit_breaker)
        if not cb_ok:
            raise RiskBlocked("circuit_breaker", cb_reason)

        dl_ok, dl_reason = check_daily_loss(self.daily_loss)
        if not dl_ok:
            raise RiskBlocked("daily_loss_limit", dl_reason)

        mk_ok, mk_reason = can_trade_today(self.market_state)
        if not mk_ok:
            raise RiskBlocked("market_halt", mk_reason)

        bl_entry = self.blacklist.is_blacklisted(ctx.symbol)
        if bl_entry is not None:
            raise RiskBlocked("blacklist", f"{ctx.symbol}: {bl_entry.reason}")

        lq_ok, lq_reason = check_liquidity(
            market_cap=ctx.market_cap,
            avg_volume_krw=ctx.avg_volume_krw,
            thresholds=self.liquidity_thresholds,
        )
        if not lq_ok:
            raise RiskBlocked("liquidity", lq_reason)

        sym_ok, sym_reason = can_enter_symbol(self.market_state, ctx.symbol)
        if not sym_ok:
            raise RiskBlocked("symbol_state", sym_reason)

        cd_ok, cd_reason = check_cooldown(
            symbol=ctx.symbol,
            trades=self.trades_history,
            now=self._now(),
            policy=self.cooldown_policy,
        )
        if not cd_ok:
            raise RiskBlocked("cooldown", cd_reason)

        headroom = compute_max_additional_notional(
            symbol=ctx.symbol,
            portfolio_value=ctx.portfolio_value,
            existing_positions_notional=ctx.existing_positions_notional,
            sector=ctx.sector,
            sector_map=ctx.sector_map,
            policy=self.sizing_policy,
        )
        if headroom <= 0:
            raise RiskBlocked("sizing", f"{ctx.symbol} 종목/섹터 상한 도달")
        if ctx.intended_notional is not None and ctx.intended_notional > headroom:
            raise RiskBlocked(
                "sizing",
                f"요청 {ctx.intended_notional:,.0f} > 가능 {headroom:,.0f}",
            )

    # ------------------------------------------------------------------
    # SELL
    # ------------------------------------------------------------------
    def check_sell(self, symbol: str) -> None:
        """청산 gate: 거래정지 시에만 차단 (청산 주문 불가). CB·DL 상태에서도
        기존 포지션 **청산은 허용** (오히려 청산이 리스크 저감 수단)."""
        ok, reason = can_exit_symbol(self.market_state, symbol)
        if not ok:
            raise RiskBlocked("symbol_halted", reason)

    # ------------------------------------------------------------------
    # Event recording (hook for persistence)
    # ------------------------------------------------------------------
    def record_trade(self, record: TradeRecord) -> None:
        self.trades_history.append(record)


# ---------------------------------------------------------------------------
# @risk_gate decorator — enforces engine presence
# ---------------------------------------------------------------------------

_engine: ContextVar["RiskEngine | None"] = ContextVar("risk_engine", default=None)
_engine_lock = threading.RLock()


def set_risk_engine(engine: "RiskEngine | None") -> None:
    """Install the active RiskEngine for the current context.

    Production: call at startup (with DB-backed state) and refresh on each
    trading cycle. Tests: wrap in a try/finally.
    """
    with _engine_lock:
        _engine.set(engine)


def get_risk_engine() -> "RiskEngine | None":
    return _engine.get()


def risk_gate(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: enforces RiskEngine gate before executing `fn`.

    The wrapped callable MUST accept keyword arguments `symbol` (str) and
    `side` ('BUY' | 'SELL'). For BUY, a `buy_context: BuyContext` keyword
    must be supplied. Missing the engine → RuntimeError (fail-loud on purpose).
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        engine = get_risk_engine()
        if engine is None:
            raise RuntimeError(
                "risk_gate: no RiskEngine installed — call set_risk_engine() first"
            )
        side = str(kwargs.get("side", "")).upper()
        symbol = kwargs.get("symbol")
        if not symbol:
            raise RuntimeError("risk_gate: wrapped call missing `symbol` kwarg")
        if side == "BUY":
            ctx: BuyContext | None = kwargs.get("buy_context")
            if ctx is None:
                raise RuntimeError("risk_gate: BUY requires `buy_context` kwarg")
            engine.check_buy(ctx)
        elif side == "SELL":
            engine.check_sell(symbol)
        else:
            raise RuntimeError(f"risk_gate: unknown side={side!r}")
        return fn(*args, **kwargs)

    return wrapper
