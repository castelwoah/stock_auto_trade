"""Strategy contracts.

Strategy 계층은 **신호만 생성**하고 실제 주문 전송은 execution/order_manager 가
담당한다. 모든 Signal 은 `strategy_id` 로 태깅되어 DB(positions/trades) 복합 PK 에
반영됨으로써 D2 (전략 격리) 불변식을 유지한다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Signal:
    action: Action
    symbol: str
    strategy_id: str
    notional: float = 0.0          # 목표 투자 원화 (매수 시). 매도는 수량 기반이므로 0 허용.
    qty: int = 0                   # 매도 시 수량 (매수는 execution 에서 notional→qty 변환)
    reason: str = ""
    signal_id: str | None = None   # idempotency_key 에 사용 (D8)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionState:
    """Strategy 가 exit 판정에 사용하는 포지션 스냅샷.

    DB persistence.models.Position 과 1:1 이 아니라, 필요한 최소 필드만 노출.
    """

    symbol: str
    strategy_id: str
    qty: int
    avg_price: float
    current_price: float
    highest_price: float | None = None   # D3: Trailing stop 용 최고점
    opened_at: datetime | None = None
    tp_armed: bool = False                # Take-profit 돌파 이력 (Trailing 전환 플래그)


@dataclass
class StrategyContext:
    """Strategy.decide_* 에 주입되는 read-only 스냅샷."""

    as_of: datetime
    today: date
    cash_available: float                 # 해당 전략 서브포트폴리오에 허용된 현금
    positions: dict[str, PositionState] = field(default_factory=dict)
    candidate_scores: dict[str, float] = field(default_factory=dict)  # symbol → total_score
    current_prices: dict[str, float] = field(default_factory=dict)
    prev_day_ohlc: dict[str, dict[str, float]] = field(default_factory=dict)
    # 확장용 (예: event_gate, sector_map 등)
    extras: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """전략 베이스.

    서브클래스는 `strategy_id` 문자열 상수를 반드시 정의.
    decide_entries → 신규 매수 후보, decide_exits → 청산 대상.
    """

    strategy_id: str = ""

    def __init__(self) -> None:
        if not self.strategy_id:
            raise ValueError(f"{type(self).__name__}.strategy_id must be set")

    @abstractmethod
    def decide_entries(self, ctx: StrategyContext) -> list[Signal]: ...

    @abstractmethod
    def decide_exits(self, ctx: StrategyContext) -> list[Signal]: ...

    def owned_positions(self, ctx: StrategyContext) -> Iterable[PositionState]:
        """해당 전략 소유 포지션만 순회 (D2 전략 격리)."""
        for p in ctx.positions.values():
            if p.strategy_id == self.strategy_id:
                yield p
