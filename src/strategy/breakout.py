"""Volatility-breakout strategy (legacy port).

목표가 = 금일 시가 + (전일 고가 − 전일 저가) × K
K 는 일반적으로 0.5.
진입: 현재가 > 목표가
청산: 장 마감(15:15) 일괄 청산 (당일 청산 전략) — registry 가 스케줄 트리거.
서브포트폴리오: 전체 현금의 고정 비율(기본 20%) 만 이 전략에 할당 (D2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.strategy.base import Action, PositionState, Signal, Strategy, StrategyContext
from src.strategy.exit_signals import evaluate_exit, make_exit_signal


BREAKOUT_K = 0.5
DEFAULT_MAX_POSITIONS = 3
EOD_CUTOFF_HOUR = 15
EOD_CUTOFF_MINUTE = 15


def compute_target_price(
    open_today: float, prev_high: float, prev_low: float, *, k: float = BREAKOUT_K
) -> float:
    """목표가 공식. 음수 입력 방어는 호출측."""
    return open_today + (prev_high - prev_low) * k


@dataclass
class BreakoutStrategy(Strategy):
    strategy_id: str = "breakout"
    k: float = BREAKOUT_K
    max_positions: int = DEFAULT_MAX_POSITIONS

    def __post_init__(self) -> None:
        super().__init__()

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------
    def decide_entries(self, ctx: StrategyContext) -> list[Signal]:
        if self._is_eod_cutoff(ctx.as_of):
            return []  # 장 마감 30분 전부터는 신규 매수 중단 (rules §5.1)

        owned = {p.symbol for p in self.owned_positions(ctx)}
        available_slots = max(0, self.max_positions - len(owned))
        if available_slots == 0 or ctx.cash_available <= 0:
            return []

        per_position = ctx.cash_available / self.max_positions
        signals: list[Signal] = []
        for symbol, cur in ctx.current_prices.items():
            if symbol in owned:
                continue
            ohlc = ctx.prev_day_ohlc.get(symbol)
            if not ohlc:
                continue
            open_today = float(ohlc.get("open_today") or 0.0)
            prev_high = float(ohlc.get("high") or 0.0)
            prev_low = float(ohlc.get("low") or 0.0)
            if open_today <= 0 or prev_high <= 0 or prev_low <= 0:
                continue
            target = compute_target_price(open_today, prev_high, prev_low, k=self.k)
            if cur > target:
                sig_id = f"breakout-{symbol}-{ctx.today.isoformat()}"
                signals.append(
                    Signal(
                        action=Action.BUY,
                        symbol=symbol,
                        strategy_id=self.strategy_id,
                        notional=per_position,
                        reason=f"breakout: {cur:.0f} > target {target:.0f}",
                        signal_id=sig_id,
                    )
                )
                if len(signals) >= available_slots:
                    break
        return signals

    # ------------------------------------------------------------------
    # Exits
    # ------------------------------------------------------------------
    def decide_exits(self, ctx: StrategyContext) -> list[Signal]:
        exits: list[Signal] = []
        eod = self._is_eod_liquidation(ctx.as_of)
        for p in self.owned_positions(ctx):
            # EOD 일괄 청산 (breakout 은 당일 청산 전략)
            if eod:
                exits.append(
                    Signal(
                        action=Action.SELL,
                        symbol=p.symbol,
                        strategy_id=self.strategy_id,
                        qty=p.qty,
                        reason="eod_liquidation",
                        signal_id=f"breakout-eod-{p.symbol}-{ctx.today.isoformat()}",
                    )
                )
                continue
            # 4중 방어
            decision = evaluate_exit(p)
            if decision.exit:
                sid = f"breakout-exit-{p.symbol}-{decision.reason}-{ctx.today.isoformat()}"
                exits.append(make_exit_signal(p, decision, signal_id=sid))
        return exits

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_eod_cutoff(now: datetime) -> bool:
        """15:00 이후는 신규 매수 중단 (rules §5.1)."""
        return (now.hour, now.minute) >= (15, 0)

    @staticmethod
    def _is_eod_liquidation(now: datetime) -> bool:
        """15:15 이후 보유 전량 청산 (legacy 동작 유지)."""
        return (now.hour, now.minute) >= (EOD_CUTOFF_HOUR, EOD_CUTOFF_MINUTE)
