"""FIFO realized P&L 계산 — Trade 테이블에 pnl 컬럼을 추가하지 않고도
당일 실현 손익을 산출하기 위한 순수 계산 모듈.

rules §4.2 DB 스키마 변경 금지 원칙을 유지하기 위해, 체결 시점에 P&L 을
기록하지 않고 리포트 시점에 **FIFO 로 BUY↔SELL 을 재구성**해 계산한다.

입출력:
  - 입력: Trade 레코드 리스트 (status='filled' 필터)
  - 출력: RealizedPnlSummary (총 실현 손익 + 건별 pnl 목록)

비용 모델 (backtest/portfolio_sim.py 와 동기화):
  - 매수 수수료 0.15%
  - 매도 수수료 0.15% + 거래세 0.23%
  (슬리피지는 이미 체결가에 반영되어 별도 처리 X)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Iterable


FEE_PCT = 0.15
TAX_PCT = 0.23  # 매도만


@dataclass(frozen=True)
class RealizedFill:
    """SELL 한 건에 매칭된 FIFO 결과."""
    symbol: str
    strategy_id: str
    qty: int
    buy_price: float
    sell_price: float
    sell_ts: datetime
    pnl_krw: float       # 비용 차감 후 순 실현 손익
    pnl_pct: float       # (avg) 매입가 대비 %


@dataclass(frozen=True)
class RealizedPnlSummary:
    total_realized_pnl_krw: float = 0.0
    fills: list[RealizedFill] = field(default_factory=list)
    unmatched_sells: int = 0   # 선행 BUY 를 못 찾은 SELL 개수 (이상 징후)

    @property
    def win_count(self) -> int:
        return sum(1 for f in self.fills if f.pnl_krw > 0)

    @property
    def loss_count(self) -> int:
        return sum(1 for f in self.fills if f.pnl_krw < 0)

    @property
    def take_profit_fills(self) -> list[RealizedFill]:
        return [f for f in self.fills if f.pnl_krw > 0]

    @property
    def stop_loss_fills(self) -> list[RealizedFill]:
        return [f for f in self.fills if f.pnl_krw <= 0]


def compute_realized_pnl(trades: Iterable) -> RealizedPnlSummary:
    """FIFO BUY→SELL 매칭.

    Trade 객체는 persistence.models.Trade 를 기대 (duck typing — 필요 속성:
    `symbol`, `strategy_id`, `side`, `qty`, `price`, `ts`, `status`).

    - status='filled' 만 대상 (submitted/rejected/cancelled 는 제외)
    - (symbol, strategy_id) 별로 FIFO 큐 유지
    - 매도 수량이 큐 합계 초과 시 `unmatched_sells` 카운트
    """
    # (symbol, strategy_id) → list[(qty_remaining, buy_price)]
    queues: dict[tuple[str, str], list[list]] = {}
    fills: list[RealizedFill] = []
    unmatched = 0

    # ts 기준 오름차순 (같은 ts 면 BUY 먼저)
    ordered = sorted(
        [t for t in trades if getattr(t, "status", "") == "filled"],
        key=lambda t: (t.ts, 0 if str(t.side).upper() == "BUY" else 1),
    )

    for tr in ordered:
        key = (tr.symbol, tr.strategy_id or "")
        side = str(tr.side).upper()
        if side == "BUY":
            queues.setdefault(key, []).append([int(tr.qty), float(tr.price)])
        elif side == "SELL":
            remaining = int(tr.qty)
            queue = queues.setdefault(key, [])
            weighted_sum = 0.0
            matched_qty = 0
            while remaining > 0 and queue:
                head_qty, head_price = queue[0]
                take = min(head_qty, remaining)
                weighted_sum += take * head_price
                matched_qty += take
                head_qty -= take
                remaining -= take
                if head_qty == 0:
                    queue.pop(0)
                else:
                    queue[0][0] = head_qty
            if matched_qty == 0:
                unmatched += 1
                continue
            avg_buy = weighted_sum / matched_qty
            sell_price = float(tr.price)
            # 비용 반영
            gross_pnl = matched_qty * (sell_price - avg_buy)
            sell_fee = matched_qty * sell_price * FEE_PCT / 100.0
            sell_tax = matched_qty * sell_price * TAX_PCT / 100.0
            # 매수 수수료는 체결 시점에 이미 발생했지만 P&L 계산에 반영
            buy_fee = matched_qty * avg_buy * FEE_PCT / 100.0
            pnl = gross_pnl - sell_fee - sell_tax - buy_fee
            pnl_pct = (pnl / (avg_buy * matched_qty)) * 100.0 if avg_buy > 0 else 0.0
            fills.append(RealizedFill(
                symbol=tr.symbol,
                strategy_id=tr.strategy_id or "",
                qty=matched_qty,
                buy_price=avg_buy,
                sell_price=sell_price,
                sell_ts=tr.ts,
                pnl_krw=pnl,
                pnl_pct=pnl_pct,
            ))
            if remaining > 0:
                unmatched += 1

    total = sum(f.pnl_krw for f in fills)
    return RealizedPnlSummary(
        total_realized_pnl_krw=total,
        fills=fills,
        unmatched_sells=unmatched,
    )


def filter_by_time_window(
    trades: Iterable,
    *,
    day: date,
    start: time = time(9, 10),
    end: time = time(15, 30),
) -> list:
    """특정 날짜·시간 윈도우 내 Trade 만 반환."""
    out = []
    for t in trades:
        if not hasattr(t, "ts"):
            continue
        if t.ts.date() != day:
            continue
        if not (start <= t.ts.time() <= end):
            continue
        out.append(t)
    return out
