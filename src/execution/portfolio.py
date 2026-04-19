"""Live portfolio state bridge: Strategy layer ↔ DB (persistence.models.Position).

책임:
  - DB(Position) 에 영속된 실시간 포지션을 StrategyContext.positions 로 변환
  - 주문 체결 기록(Trade) 삽입 (idempotency_key UNIQUE 준수 — D8)
  - highest_price 이벤트 드리븐 WRITE (D3)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src.persistence.models import Position, Trade
from src.strategy.base import PositionState


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeRecord:
    """Insert payload for persistence.models.Trade."""

    symbol: str
    side: str
    qty: int
    price: float
    strategy_id: str
    strategy_ver: str
    signal_id: str | None
    idempotency_key: str
    order_id: str | None
    order_dvsn: str = "00"   # 00=지정가 / 01=시장가
    status: str = "submitted"
    note: str | None = None


class LivePortfolio:
    """DB-backed portfolio. 세션은 외부에서 주입."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Read — StrategyContext 주입용
    # ------------------------------------------------------------------
    def positions_as_state(
        self, *, current_prices: dict[str, float] | None = None
    ) -> dict[str, PositionState]:
        rows = self.session.execute(select(Position).where(Position.qty > 0)).scalars().all()
        out: dict[str, PositionState] = {}
        cp = current_prices or {}
        for r in rows:
            cur = cp.get(r.symbol, r.avg_price)
            out[r.symbol] = PositionState(
                symbol=r.symbol,
                strategy_id=r.strategy_id,
                qty=r.qty,
                avg_price=r.avg_price,
                current_price=cur,
                highest_price=r.highest_price,
                opened_at=r.opened_at,
                tp_armed=False,   # Position 테이블에 tp_armed 필드 없음 — in-memory flag
            )
        return out

    # ------------------------------------------------------------------
    # Write — 체결 결과 반영
    # ------------------------------------------------------------------
    def record_trade(self, rec: TradeRecord) -> Trade:
        """멱등 insert. 동일 idempotency_key 가 이미 있으면 기존 row 반환."""
        existing = self.session.execute(
            select(Trade).where(Trade.idempotency_key == rec.idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            logger.info("trade already recorded: %s", rec.idempotency_key)
            return existing
        trade = Trade(
            symbol=rec.symbol,
            side=rec.side,
            qty=rec.qty,
            price=rec.price,
            order_id=rec.order_id,
            order_dvsn=rec.order_dvsn,
            strategy_id=rec.strategy_id,
            strategy_ver=rec.strategy_ver,
            signal_id=rec.signal_id,
            idempotency_key=rec.idempotency_key,
            status=rec.status,
            note=rec.note,
        )
        self.session.add(trade)
        self.session.flush()
        return trade

    def apply_fill_to_position(
        self,
        *,
        symbol: str,
        strategy_id: str,
        side: str,
        qty: int,
        price: float,
        ts: datetime,
    ) -> Position:
        """체결이 확정되면 Position 테이블 갱신. BUY 는 가중평균 / SELL 은 수량 차감."""
        key = (symbol, strategy_id)
        pos = self.session.get(Position, key)
        if side.upper() == "BUY":
            if pos is None:
                pos = Position(
                    symbol=symbol,
                    strategy_id=strategy_id,
                    qty=qty,
                    avg_price=price,
                    opened_at=ts,
                    highest_price=price,
                    updated_at=ts,
                )
                self.session.add(pos)
            else:
                new_qty = pos.qty + qty
                pos.avg_price = (pos.avg_price * pos.qty + price * qty) / new_qty
                pos.qty = new_qty
                if pos.highest_price is None or price > pos.highest_price:
                    pos.highest_price = price
                pos.updated_at = ts
        elif side.upper() == "SELL":
            if pos is None or pos.qty <= 0:
                raise ValueError(f"SELL with no existing position: {symbol}/{strategy_id}")
            pos.qty -= min(qty, pos.qty)
            pos.updated_at = ts
            if pos.qty == 0:
                # 전량 청산: 옵션 — 유지(tp_armed 이력) vs delete. 여기선 유지하고 qty=0.
                pos.opened_at = None
                pos.highest_price = None
        else:
            raise ValueError(f"unknown side: {side}")
        self.session.flush()
        return pos

    # ------------------------------------------------------------------
    # Peak update — Trailing stop 영속화 (D3)
    # ------------------------------------------------------------------
    def update_highest_price(
        self, symbol: str, strategy_id: str, *, new_price: float
    ) -> None:
        pos = self.session.get(Position, (symbol, strategy_id))
        if pos is None or pos.qty <= 0:
            return
        if pos.highest_price is None or new_price > pos.highest_price:
            pos.highest_price = new_price
            pos.updated_at = datetime.now()
            self.session.flush()

    def mark_halt(self, symbol: str, strategy_id: str, *, halted: bool = True) -> None:
        pos = self.session.get(Position, (symbol, strategy_id))
        if pos is not None:
            pos.halt_flag = halted
            self.session.flush()

    # ------------------------------------------------------------------
    # Valuation
    # ------------------------------------------------------------------
    def equity(self, cash: float, prices: dict[str, float]) -> float:
        total = cash
        rows = self.session.execute(select(Position).where(Position.qty > 0)).scalars().all()
        for p in rows:
            px = prices.get(p.symbol, p.avg_price)
            total += p.qty * px
        return total

    def iter_open_positions(self) -> Iterable[Position]:
        return self.session.execute(select(Position).where(Position.qty > 0)).scalars()
