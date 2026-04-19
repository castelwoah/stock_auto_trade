"""Strategy registry — 서브포트폴리오 현금 할당 + 신호 라우팅 (D2).

**서브에이전트 금지 영역** — 전략 라우팅은 전역 일관성이 핵심이므로 단일 논리
흐름만 유지한다.

책임:
  1) 전략별 현금 할당 비율 관리 (합 ≤ 100%, 나머지는 reserve).
  2) 각 전략에 격리된 `StrategyContext` 조립 (해당 전략 포지션/허용현금만 주입).
  3) entries / exits 수집하여 단일 Signal 리스트 반환.
  4) idempotency_key 중복 방지 (같은 signal_id 이미 실행된 경우 필터).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from src.strategy.base import Action, PositionState, Signal, Strategy, StrategyContext


logger = logging.getLogger(__name__)


@dataclass
class _Slot:
    strategy: Strategy
    allocation_pct: float    # 0.0 ~ 100.0


@dataclass
class StrategyRegistry:
    """전략 등록·라우팅 허브.

    사용 예:
        reg = StrategyRegistry()
        reg.register(BreakoutStrategy(), allocation_pct=20.0)
        reg.register(NewsSwingStrategy(), allocation_pct=70.0)
        signals = reg.all_signals(global_ctx)
    """

    _slots: dict[str, _Slot] = field(default_factory=dict)
    _executed_signal_ids: set[str] = field(default_factory=set)

    def register(self, strategy: Strategy, *, allocation_pct: float) -> None:
        if not (0.0 <= allocation_pct <= 100.0):
            raise ValueError("allocation_pct must be in [0, 100]")
        if strategy.strategy_id in self._slots:
            raise ValueError(f"strategy_id '{strategy.strategy_id}' already registered")
        total = sum(s.allocation_pct for s in self._slots.values()) + allocation_pct
        if total > 100.0 + 1e-9:
            raise ValueError(
                f"total allocation would exceed 100% (got {total:.1f}%)"
            )
        self._slots[strategy.strategy_id] = _Slot(strategy=strategy, allocation_pct=allocation_pct)

    def unregister(self, strategy_id: str) -> None:
        self._slots.pop(strategy_id, None)

    def strategies(self) -> list[Strategy]:
        return [s.strategy for s in self._slots.values()]

    def allocation(self, strategy_id: str) -> float:
        slot = self._slots.get(strategy_id)
        return slot.allocation_pct if slot else 0.0

    def reserve_pct(self) -> float:
        return max(0.0, 100.0 - sum(s.allocation_pct for s in self._slots.values()))

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------
    def all_signals(self, global_ctx: StrategyContext) -> list[Signal]:
        """Entries + exits 를 모든 등록 전략에서 수집해 반환.

        global_ctx 의 `cash_available` 은 **총 가용 현금**. 각 전략에는 자신의
        할당 비율만큼 분배한 격리 컨텍스트가 주입된다.
        """
        out: list[Signal] = []
        for slot in self._slots.values():
            sub_ctx = self._build_sub_context(global_ctx, slot)
            try:
                exits = slot.strategy.decide_exits(sub_ctx)
            except Exception:
                logger.exception("strategy %s: decide_exits failed", slot.strategy.strategy_id)
                exits = []
            try:
                entries = slot.strategy.decide_entries(sub_ctx)
            except Exception:
                logger.exception("strategy %s: decide_entries failed", slot.strategy.strategy_id)
                entries = []
            # 각 전략 안에서도 exits 을 entries 보다 우선 (리스크 저감 우선)
            for s in exits + entries:
                if s.strategy_id != slot.strategy.strategy_id:
                    logger.warning(
                        "strategy %s emitted signal with wrong strategy_id=%s",
                        slot.strategy.strategy_id,
                        s.strategy_id,
                    )
                    continue
                if s.signal_id and s.signal_id in self._executed_signal_ids:
                    continue  # 중복 방지 (D8)
                out.append(s)
        return out

    def mark_executed(self, signal_ids: Iterable[str]) -> None:
        """execution 레이어가 주문 전송 성공 시 호출."""
        self._executed_signal_ids.update(sid for sid in signal_ids if sid)

    # ------------------------------------------------------------------
    def _build_sub_context(self, global_ctx: StrategyContext, slot: _Slot) -> StrategyContext:
        sub_cash = global_ctx.cash_available * (slot.allocation_pct / 100.0)
        # D2: 해당 전략이 소유한 포지션만 격리 주입
        sub_positions = {
            sym: p
            for sym, p in global_ctx.positions.items()
            if p.strategy_id == slot.strategy.strategy_id
        }
        return StrategyContext(
            as_of=global_ctx.as_of,
            today=global_ctx.today,
            cash_available=sub_cash,
            positions=sub_positions,
            candidate_scores=dict(global_ctx.candidate_scores),
            current_prices=dict(global_ctx.current_prices),
            prev_day_ohlc=dict(global_ctx.prev_day_ohlc),
            extras=dict(global_ctx.extras),
        )


# ---------------------------------------------------------------------------
# Convenience: signal → Trade idempotency_key (D8)
# ---------------------------------------------------------------------------

def build_idempotency_key(signal: Signal, *, trade_date: str) -> str:
    """trades.idempotency_key UNIQUE 후보.

    규약: `<date>:<symbol>:<strategy_id>:<signal_id or action>`
    """
    tail = signal.signal_id or signal.action.value
    return f"{trade_date}:{signal.symbol}:{signal.strategy_id}:{tail}"
