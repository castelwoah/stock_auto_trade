"""Simulated portfolio for backtesting.

체결 모델 (rules §10.3 + KOSPI 2026 기준):
  - 슬리피지 0.25% (매수 +, 매도 -)
  - 수수료   0.15% (매수·매도 양쪽)
  - 거래세   0.23% (매도만, 증권거래세)
  → 왕복 비용 ≈ 1.03%

멱등성: `SimFill.idempotency_key` 가 중복이면 fill 거부 (D8).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from src.strategy.base import Action, PositionState, Signal


logger = logging.getLogger(__name__)


# Execution cost defaults (rules §10.3 + 국내 2026 세제 기준)
DEFAULT_SLIPPAGE_PCT = 0.25
DEFAULT_FEE_PCT = 0.15
DEFAULT_TAX_PCT = 0.23  # 매도만


@dataclass(frozen=True)
class SimFill:
    ts: datetime
    symbol: str
    side: str  # "BUY" / "SELL"
    qty: int
    price: float          # 실제 체결가 (슬리피지 반영)
    fee: float
    tax: float            # 매도만 양수
    strategy_id: str
    idempotency_key: str
    signal_id: str | None = None
    reason: str = ""
    pnl_krw: float | None = None  # SELL 시 실현 손익
    pnl_pct: float | None = None
    was_stop_loss: bool = False

    @property
    def gross(self) -> float:
        return self.qty * self.price

    @property
    def cost(self) -> float:
        return self.fee + self.tax


@dataclass
class SimPosition:
    symbol: str
    strategy_id: str
    qty: int = 0
    avg_price: float = 0.0
    opened_at: datetime | None = None
    highest_price: float | None = None
    tp_armed: bool = False


@dataclass
class SimSkip:
    ts: datetime
    signal: Signal
    reason: str


class PortfolioSim:
    """Pure-python 백테스트 포트폴리오. 외부 I/O 없음.

    - 매수: cash 충분 / 정수 수량 / 슬리피지 반영 체결가
    - 매도: 보유 수량 범위 / 거래세 부과 / 실현 손익 기록
    - mark_to_market: 일별·분기별 평가액 스냅샷
    """

    def __init__(
        self,
        initial_cash: float,
        *,
        slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
        fee_pct: float = DEFAULT_FEE_PCT,
        tax_pct: float = DEFAULT_TAX_PCT,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct
        self.tax_pct = tax_pct

        self.positions: dict[tuple[str, str], SimPosition] = {}  # (symbol, strategy_id) → SimPosition
        self.fills: list[SimFill] = []
        self.skips: list[SimSkip] = []
        self.equity_curve: list[tuple[datetime, float]] = []

        self._executed_keys: set[str] = set()
        self._realized_pnl: float = 0.0

    # ------------------------------------------------------------------
    # Core: fill a signal
    # ------------------------------------------------------------------

    def fill(
        self,
        signal: Signal,
        *,
        ts: datetime,
        bar_price: float,
        idempotency_key: str,
        qty_override: int | None = None,
    ) -> SimFill | None:
        """Execute a Signal at `bar_price` with slippage/fees.

        Returns SimFill on success, None on skip (duplicate / insufficient cash / invalid qty).
        """
        if idempotency_key in self._executed_keys:
            logger.debug("duplicate idempotency_key: %s", idempotency_key)
            return None
        if bar_price <= 0:
            self.skips.append(SimSkip(ts=ts, signal=signal, reason="nonpositive_price"))
            return None

        if signal.action == Action.BUY:
            return self._fill_buy(
                signal, ts=ts, bar_price=bar_price,
                idempotency_key=idempotency_key, qty_override=qty_override,
            )
        if signal.action == Action.SELL:
            return self._fill_sell(
                signal, ts=ts, bar_price=bar_price,
                idempotency_key=idempotency_key, qty_override=qty_override,
            )
        self.skips.append(SimSkip(ts=ts, signal=signal, reason=f"unsupported_action:{signal.action}"))
        return None

    def _fill_buy(
        self, signal: Signal, *, ts: datetime, bar_price: float,
        idempotency_key: str, qty_override: int | None,
    ) -> SimFill | None:
        exec_price = bar_price * (1.0 + self.slippage_pct / 100.0)
        qty = qty_override if qty_override is not None else self._notional_to_qty(signal.notional, exec_price)
        if qty <= 0:
            self.skips.append(SimSkip(ts=ts, signal=signal, reason="qty_zero"))
            return None

        gross = qty * exec_price
        fee = gross * self.fee_pct / 100.0
        total = gross + fee
        if total > self.cash:
            # 부분 체결 금지 — 현금 부족 시 전량 스킵
            self.skips.append(SimSkip(ts=ts, signal=signal, reason="insufficient_cash"))
            return None

        self.cash -= total
        key = (signal.symbol, signal.strategy_id)
        pos = self.positions.get(key)
        if pos is None:
            pos = SimPosition(
                symbol=signal.symbol, strategy_id=signal.strategy_id,
                qty=qty, avg_price=exec_price, opened_at=ts,
                highest_price=exec_price,
            )
            self.positions[key] = pos
        else:
            new_qty = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + exec_price * qty) / new_qty
            pos.qty = new_qty
            if pos.highest_price is None or exec_price > pos.highest_price:
                pos.highest_price = exec_price

        fill = SimFill(
            ts=ts, symbol=signal.symbol, side="BUY", qty=qty, price=exec_price,
            fee=fee, tax=0.0, strategy_id=signal.strategy_id,
            idempotency_key=idempotency_key, signal_id=signal.signal_id,
            reason=signal.reason,
        )
        self._executed_keys.add(idempotency_key)
        self.fills.append(fill)
        return fill

    def _fill_sell(
        self, signal: Signal, *, ts: datetime, bar_price: float,
        idempotency_key: str, qty_override: int | None,
    ) -> SimFill | None:
        key = (signal.symbol, signal.strategy_id)
        pos = self.positions.get(key)
        if pos is None or pos.qty <= 0:
            self.skips.append(SimSkip(ts=ts, signal=signal, reason="no_position"))
            return None

        qty = qty_override if qty_override is not None else (signal.qty or pos.qty)
        qty = min(qty, pos.qty)
        if qty <= 0:
            self.skips.append(SimSkip(ts=ts, signal=signal, reason="qty_zero"))
            return None

        exec_price = bar_price * (1.0 - self.slippage_pct / 100.0)
        gross = qty * exec_price
        fee = gross * self.fee_pct / 100.0
        tax = gross * self.tax_pct / 100.0
        proceeds = gross - fee - tax

        # 실현 손익 계산 (체결된 수량 × (체결가 - 평단))
        pnl_gross = qty * (exec_price - pos.avg_price)
        pnl_krw = pnl_gross - fee - tax  # 비용 차감 후 순손익
        pnl_pct = (pnl_krw / (pos.avg_price * qty)) * 100.0 if pos.avg_price > 0 else 0.0
        was_stop_loss = (
            (signal.reason or "").startswith("stop_loss")
            or "stop_loss" in (signal.reason or "")
        )

        self.cash += proceeds
        self._realized_pnl += pnl_krw

        pos.qty -= qty
        if pos.qty == 0:
            del self.positions[key]

        fill = SimFill(
            ts=ts, symbol=signal.symbol, side="SELL", qty=qty, price=exec_price,
            fee=fee, tax=tax, strategy_id=signal.strategy_id,
            idempotency_key=idempotency_key, signal_id=signal.signal_id,
            reason=signal.reason, pnl_krw=pnl_krw, pnl_pct=pnl_pct,
            was_stop_loss=was_stop_loss,
        )
        self._executed_keys.add(idempotency_key)
        self.fills.append(fill)
        return fill

    # ------------------------------------------------------------------
    # Valuation
    # ------------------------------------------------------------------

    def mark_to_market(self, prices: dict[str, float], *, ts: datetime) -> float:
        """Update highest_price and append equity snapshot."""
        total = self.cash
        for (sym, _sid), pos in self.positions.items():
            p = prices.get(sym, pos.avg_price)
            if p <= 0:
                p = pos.avg_price
            total += pos.qty * p
            if pos.highest_price is None or p > pos.highest_price:
                pos.highest_price = p
        self.equity_curve.append((ts, total))
        return total

    def equity(self, prices: dict[str, float] | None = None) -> float:
        """Current equity. If `prices` omitted, use last-known avg_price."""
        total = self.cash
        for (sym, _sid), pos in self.positions.items():
            p = (prices or {}).get(sym, pos.avg_price)
            total += pos.qty * p
        return total

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    # ------------------------------------------------------------------
    # Strategy-layer interop
    # ------------------------------------------------------------------

    def current_positions_as_state(
        self, *, prices: dict[str, float] | None = None
    ) -> dict[str, PositionState]:
        """Convert SimPositions to PositionState map (symbol → PositionState).

        Strategy 레이어는 symbol 단위 dict 를 받으므로, 같은 symbol 을 여러 전략이
        보유 중이면 **마지막 갱신이 승리**한다 — 실제로는 strategy_id 격리가
        Registry 수준에서 이뤄지므로 충돌 발생하지 않음.
        """
        out: dict[str, PositionState] = {}
        for (sym, sid), pos in self.positions.items():
            cur = (prices or {}).get(sym, pos.avg_price)
            out[sym] = PositionState(
                symbol=sym, strategy_id=sid, qty=pos.qty,
                avg_price=pos.avg_price, current_price=cur,
                highest_price=pos.highest_price, opened_at=pos.opened_at,
                tp_armed=pos.tp_armed,
            )
        return out

    def update_tp_armed(self, symbol: str, strategy_id: str, *, armed: bool = True) -> None:
        pos = self.positions.get((symbol, strategy_id))
        if pos is not None:
            pos.tp_armed = armed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _notional_to_qty(self, notional: float, price: float) -> int:
        if notional <= 0 or price <= 0:
            return 0
        return int(notional // price)
