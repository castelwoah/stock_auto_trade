"""섀도 가상 포지션 — status='shadow' Trade 기록으로 재구성 (SHADOW_MODE 전용).

섀도 매수는 주문을 보내지 않으므로 Position 을 만들지 않는다. 그러면 손절·Trailing·EOD
청산 경로가 섀도에서 한 번도 돌지 않는다. 이 모듈은 섀도 Trade(BUY/SELL)를 시간순으로
재생해 가상 포지션을 만들고, pipeline 이 실포지션과 같은 PositionState 로 전략에 넘긴다.

- Position 테이블·Reconciler 는 건드리지 않는다 (가상 포지션이 실계좌 불일치를 만들지 않게)
- 주문 즉시 전량 체결 가정 (기록가 = 체결가). 부분 체결·재주문·가격제한 거절은 재현하지 않음
- 고점은 메모리에만 — 재시작하면 max(평균단가, 현재가)에서 다시 시작 (breakout 은 당일 청산이라 충분)
- SHADOW_BOOK_SINCE 이전 섀도 기록은 무시 (그 전 매수에는 매도 기록이 없다)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.execution.portfolio import _tp_armed
from src.persistence.models import Trade
from src.reporting.models import PositionSnapshot, ShadowSummary
from src.reporting.pnl_calc import compute_realized_pnl
from src.strategy.base import PositionState


logger = logging.getLogger(__name__)

SHADOW_STATUS = "shadow"
SHADOW_BOOK_SINCE = datetime(2026, 10, 29)
EOD_STRATEGIES = frozenset({"breakout"})      # 당일 청산 전략 — 마감 후 잔여는 경고
EOD_CHECK_AFTER = time(15, 20)

# 매도 Trade.note(= Signal.reason) 앞부분 → 리포트 라벨
SELL_REASONS = (
    ("eod_liquidation", "EOD"),
    ("stop_loss", "손절"),
    ("trailing_stop", "Trailing"),
    ("event", "이벤트"),
)


@dataclass(frozen=True)
class ShadowPosition:
    symbol: str
    strategy_id: str
    qty: int
    avg_price: float
    opened_at: datetime | None

    @property
    def cost(self) -> float:
        return self.qty * self.avg_price


@dataclass(frozen=True)
class _Filled:
    """pnl_calc 입력용 — 섀도 기록을 체결로 본다."""
    symbol: str
    strategy_id: str
    side: str
    qty: int
    price: float
    ts: datetime
    status: str = "filled"


def sell_reason(note: str | None) -> str:
    n = (note or "").strip()
    for prefix, label in SELL_REASONS:
        if n.startswith(prefix):
            return label
    return "기타"


class ShadowBook:
    def __init__(self, session: Session, *, since: datetime = SHADOW_BOOK_SINCE) -> None:
        self.session = session
        self.since = since
        self._peaks: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    def _trades(self) -> list[Trade]:
        return list(self.session.execute(
            select(Trade).where(Trade.status == SHADOW_STATUS, Trade.ts >= self.since)
            .order_by(Trade.ts, Trade.id)
        ).scalars().all())

    def open_positions(self) -> dict[tuple[str, str], ShadowPosition]:
        """(symbol, strategy_id) → 보유 중인 가상 포지션. BUY 는 가중평균, SELL 은 수량 차감."""
        book: dict[tuple[str, str], ShadowPosition] = {}
        for t in self._trades():
            key = (t.symbol, t.strategy_id)
            cur = book.get(key)
            if t.side == "BUY":
                if cur is None:
                    book[key] = ShadowPosition(t.symbol, t.strategy_id, t.qty, float(t.price), t.ts)
                else:
                    qty = cur.qty + t.qty
                    avg = (cur.cost + t.qty * float(t.price)) / qty
                    book[key] = ShadowPosition(t.symbol, t.strategy_id, qty, avg, cur.opened_at)
            elif t.side == "SELL":
                if cur is None:
                    logger.warning("shadow SELL without position: %s/%s", t.symbol, t.strategy_id)
                    continue
                left = cur.qty - min(t.qty, cur.qty)
                if left > 0:
                    book[key] = ShadowPosition(t.symbol, t.strategy_id, left, cur.avg_price, cur.opened_at)
                else:
                    del book[key]
        return book

    # ------------------------------------------------------------------
    def update_peaks(self, prices: dict[str, float]) -> None:
        book = self.open_positions()
        self._peaks = {k: v for k, v in self._peaks.items() if k in book}   # 청산분 정리
        for key, p in book.items():
            px = prices.get(p.symbol)
            peak = self._peaks.get(key, p.avg_price)
            self._peaks[key] = max(peak, px) if px else peak

    def positions_as_state(self, prices: dict[str, float]) -> dict[str, PositionState]:
        out: dict[str, PositionState] = {}
        for key, p in self.open_positions().items():
            peak = self._peaks.get(key, max(p.avg_price, prices.get(p.symbol) or 0.0))
            out[p.symbol] = PositionState(
                symbol=p.symbol,
                strategy_id=p.strategy_id,
                qty=p.qty,
                avg_price=p.avg_price,
                current_price=prices.get(p.symbol, p.avg_price),
                highest_price=peak,
                opened_at=p.opened_at,
                tp_armed=_tp_armed(p.avg_price, peak),
            )
        return out

    def open_notional(self, prices: dict[str, float]) -> dict[str, float]:
        """symbol → 가상 보유 평가액 (투입 상한·종목 한도 계산용)."""
        out: dict[str, float] = {}
        for p in self.open_positions().values():
            out[p.symbol] = out.get(p.symbol, 0.0) + p.qty * prices.get(p.symbol, p.avg_price)
        return out

    def held_symbols(self) -> set[str]:
        return {p.symbol for p in self.open_positions().values()}

    # ------------------------------------------------------------------
    def realized(self, day: date):
        """당일 매도분의 FIFO 실현손익 (수수료·세금 반영). 매칭용 매수는 이전 날짜도 포함."""
        fills = [_Filled(t.symbol, t.strategy_id, t.side, t.qty, float(t.price), t.ts)
                 for t in self._trades() if t.ts.date() <= day]
        summary = compute_realized_pnl(fills)
        return [f for f in summary.fills if f.sell_ts.date() == day]

    def account(self, capital: float, day: date, prices: dict[str, float]) -> tuple[float, float]:
        """가상 계좌 (현금, 평가액). 현금 = 자본 − 보유 원가 + 당일 실현손익."""
        book = self.open_positions()
        cost = sum(p.cost for p in book.values())
        value = sum(p.qty * prices.get(p.symbol, p.avg_price) for p in book.values())
        cash = capital - cost + sum(f.pnl_krw for f in self.realized(day))
        return max(cash, 0.0), cash + value

    def summary(self, day: date, prices: dict[str, float], *, as_of: datetime,
                names: dict[str, str] | None = None) -> ShadowSummary:
        names = names or {}
        today = [t for t in self._trades() if t.ts.date() == day]
        sells: dict[str, int] = {}
        for t in today:
            if t.side == "SELL":
                label = sell_reason(t.note)
                sells[label] = sells.get(label, 0) + 1
        positions = []
        for key, p in self.open_positions().items():
            cur = prices.get(p.symbol, p.avg_price)
            pnl = (cur - p.avg_price) * p.qty
            peak = self._peaks.get(key, max(p.avg_price, cur))
            positions.append(PositionSnapshot(
                symbol=p.symbol, name=names.get(p.symbol, ""), strategy_id=p.strategy_id,
                qty=p.qty, avg_price=p.avg_price, current_price=cur,
                unrealized_pnl_krw=pnl,
                unrealized_pnl_pct=(pnl / p.cost * 100.0) if p.cost > 0 else 0.0,
                tp_armed=_tp_armed(p.avg_price, peak),
            ))
        leftover = []
        if as_of.time() >= EOD_CHECK_AFTER:
            leftover = [f"{names.get(p.symbol) or p.symbol} {p.qty}주"
                        for p in positions if p.strategy_id in EOD_STRATEGIES]
        return ShadowSummary(
            buys=sum(1 for t in today if t.side == "BUY"),
            sells=sells,
            realized_pnl_krw=sum(f.pnl_krw for f in self.realized(day)),
            positions=positions,
            open_after_eod=leftover,
        )
