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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.persistence.models import Position, Trade
from src.strategy.base import PositionState
from src.strategy.exit_signals import should_arm_trailing
from src.common.clock import now_kst


logger = logging.getLogger(__name__)


def _tp_armed(avg_price: float, highest_price: float | None) -> bool:
    """D3: Trailing 무장 여부를 영속된 고점에서 유도 (스키마 변경 없음).

    고점은 단조 증가하고 무장은 한 번 되면 유지되므로(`update_peak`),
    "고점이 진입가 대비 +TP% 이상이었다" 와 동치다.
    """
    if not highest_price or avg_price <= 0:
        return False
    return should_arm_trailing(avg_price, highest_price)


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
    ts: datetime | None = None   # None 이면 DB 기본값(now_kst). 주문 관리자는 주입 시계를 넘긴다


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
                tp_armed=_tp_armed(r.avg_price, r.highest_price),
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
        if rec.ts is not None:
            trade.ts = rec.ts
        self.session.add(trade)
        self.session.flush()
        return trade

    def insert_trade_if_new(self, rec: TradeRecord) -> Trade | None:
        """D8 송신 게이트: 신규 idempotency_key 일 때만 Trade 를 만들고 반환.

        이미 있으면 None — 호출측은 KIS 주문을 **보내지 않는다** (재시작 중복 송신 차단).
        동시 삽입 경합은 UNIQUE 제약 위반을 savepoint 안에서 흡수한다.
        """
        if self.get_trade(rec.idempotency_key) is not None:
            return None
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
        if rec.ts is not None:
            trade.ts = rec.ts
        try:
            with self.session.begin_nested():
                self.session.add(trade)
                self.session.flush()
        except IntegrityError:
            logger.warning("idempotency race lost: %s", rec.idempotency_key)
            return None
        return trade

    def get_trade(self, idempotency_key: str) -> Trade | None:
        return self.session.execute(
            select(Trade).where(Trade.idempotency_key == idempotency_key)
        ).scalar_one_or_none()

    def open_trades(self, day_start: datetime, day_end: datetime) -> list[Trade]:
        """재시작 복원용: 해당 일자의 미종결 주문 (submitted / unknown)."""
        return list(self.session.execute(
            select(Trade).where(
                Trade.ts >= day_start,
                Trade.ts <= day_end,
                Trade.status.in_(("submitted", "unknown")),
            ).order_by(Trade.ts)
        ).scalars().all())

    def shadow_buy_notional(self, day_start: datetime, day_end: datetime) -> dict[str, float]:
        """섀도 모드 투입액: 해당 일자 shadow BUY 기록의 symbol 별 qty × price."""
        out: dict[str, float] = {}
        for sym, qty, price in self.session.execute(
            select(Trade.symbol, Trade.qty, Trade.price).where(
                Trade.ts >= day_start,
                Trade.ts <= day_end,
                Trade.side == "BUY",
                Trade.status == "shadow",
            )
        ).all():
            out[sym] = out.get(sym, 0.0) + qty * price
        return out

    def executed_signal_ids(self, day_start: datetime, day_end: datetime) -> list[str]:
        """registry 복원용: 해당 일자에 실제 송신(또는 시도)된 signal_id. shadow 제외."""
        rows = self.session.execute(
            select(Trade.signal_id).where(
                Trade.ts >= day_start,
                Trade.ts <= day_end,
                Trade.signal_id.is_not(None),
                Trade.status != "shadow",
            )
        ).scalars().all()
        return sorted({r for r in rows if r})

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
            pos.updated_at = now_kst()
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
