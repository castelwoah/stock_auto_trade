"""매도 4중 방어 (rules §5.1 / v2 설계 C).

모두 pure function — PositionState 스냅샷과 옵션 입력으로 판정한다.
외부 I/O 없음. 단위 테스트 용이.

트리거 우선순위 (강→약):
  1) stop_loss    : -5% (하드 스톱)
  2) take_profit  : +7% 최초 도달 → Trailing armed
  3) trailing     : TP 이후 고점 대비 -3%
  4) event_exit   : 거래정지/관리종목 편입/스코어 급락/Daily Loss 도달 (상위 레이어)
"""
from __future__ import annotations

from dataclasses import dataclass

from src.strategy.base import Action, PositionState, Signal


STOP_LOSS_PCT = -5.0     # 진입가 대비 %
TAKE_PROFIT_PCT = 7.0
TRAILING_STOP_PCT = -3.0  # 고점 대비 %


@dataclass(frozen=True)
class ExitDecision:
    exit: bool
    reason: str          # 'stop_loss' / 'take_profit' / 'trailing_stop' / 'event' / ''
    detail: str = ""


def pnl_pct(entry_price: float, current_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return (current_price - entry_price) / entry_price * 100.0


def draw_from_peak_pct(peak_price: float, current_price: float) -> float:
    if peak_price <= 0:
        return 0.0
    return (current_price - peak_price) / peak_price * 100.0


def should_stop_loss(
    entry_price: float, current_price: float, *, threshold_pct: float = STOP_LOSS_PCT
) -> bool:
    """하드 스톱. pnl_pct ≤ -5 이면 True."""
    return pnl_pct(entry_price, current_price) <= threshold_pct


def should_arm_trailing(
    entry_price: float, current_price: float, *, threshold_pct: float = TAKE_PROFIT_PCT
) -> bool:
    """+7% 최초 도달 시 Trailing 모드 전환 시그널 (즉시 청산 아님)."""
    return pnl_pct(entry_price, current_price) >= threshold_pct


def should_trailing_stop(
    peak_price: float | None,
    current_price: float,
    *,
    threshold_pct: float = TRAILING_STOP_PCT,
) -> bool:
    """Trailing armed 이후 고점 대비 -3% 하락하면 True."""
    if peak_price is None or peak_price <= 0:
        return False
    return draw_from_peak_pct(peak_price, current_price) <= threshold_pct


# ---------------------------------------------------------------------------
# Event-driven exits (외부 상태 입력)
# ---------------------------------------------------------------------------

def event_exit_reason(
    *,
    is_halted: bool = False,
    is_newly_managed: bool = False,
    consecutive_vi_count: int = 0,
    score_drop_below_50: bool = False,
    daily_loss_triggered: bool = False,
) -> str:
    """Returns reason tag if any event-exit condition met, else ''."""
    if daily_loss_triggered:
        return "daily_loss_liquidation"
    if is_halted:
        return "halted"
    if is_newly_managed:
        return "managed_stock"
    if consecutive_vi_count >= 3:
        return "vi_repeated"
    if score_drop_below_50:
        return "score_collapse"
    return ""


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def evaluate_exit(
    state: PositionState,
    *,
    is_halted: bool = False,
    is_newly_managed: bool = False,
    consecutive_vi_count: int = 0,
    score_drop_below_50: bool = False,
    daily_loss_triggered: bool = False,
) -> ExitDecision:
    """하나의 포지션에 대한 최종 청산 판정.

    우선순위: event > stop_loss > trailing > (none).
    TP 자체는 즉시 청산 트리거가 아니라 Trailing 을 무장하는 이벤트.
    """
    ev = event_exit_reason(
        is_halted=is_halted,
        is_newly_managed=is_newly_managed,
        consecutive_vi_count=consecutive_vi_count,
        score_drop_below_50=score_drop_below_50,
        daily_loss_triggered=daily_loss_triggered,
    )
    if ev:
        return ExitDecision(exit=True, reason="event", detail=ev)

    if should_stop_loss(state.avg_price, state.current_price):
        return ExitDecision(
            exit=True,
            reason="stop_loss",
            detail=f"{pnl_pct(state.avg_price, state.current_price):.2f}%",
        )

    if state.tp_armed and should_trailing_stop(state.highest_price, state.current_price):
        return ExitDecision(
            exit=True,
            reason="trailing_stop",
            detail=f"peak={state.highest_price} cur={state.current_price}",
        )

    return ExitDecision(exit=False, reason="")


def make_exit_signal(state: PositionState, decision: ExitDecision, *, signal_id: str | None = None) -> Signal:
    return Signal(
        action=Action.SELL,
        symbol=state.symbol,
        strategy_id=state.strategy_id,
        notional=0.0,
        qty=state.qty,
        reason=f"{decision.reason}: {decision.detail}",
        signal_id=signal_id,
    )


def update_peak(state: PositionState, *, new_price: float) -> PositionState:
    """Trailing 용 highest_price 업데이트 (불변객체이므로 교체 반환).

    DB 영속화는 호출측이 담당 (D3).
    """
    new_peak = max(new_price, state.highest_price or new_price)
    # TP 도달 시 tp_armed 플래그 자동 set (한 번 armed 되면 유지)
    tp_armed = state.tp_armed or should_arm_trailing(state.avg_price, new_price)
    return PositionState(
        symbol=state.symbol,
        strategy_id=state.strategy_id,
        qty=state.qty,
        avg_price=state.avg_price,
        current_price=new_price,
        highest_price=new_peak,
        opened_at=state.opened_at,
        tp_armed=tp_armed,
    )
