"""Market-wide and per-symbol state gates (rules §5.2)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MarketState:
    kospi_change_pct: float | None = None      # 당일 KOSPI 등락률 (%)
    kosdaq_change_pct: float | None = None
    vi_active: set[str] = field(default_factory=set)           # 현재 VI 발동 중 종목 집합
    price_change_pct: dict[str, float] = field(default_factory=dict)  # symbol → 전일대비 % (부호 유지)
    gap_open_pct: dict[str, float] = field(default_factory=dict)      # symbol → 시가 갭 %
    halted: set[str] = field(default_factory=set)              # 당일 거래정지 종목


# Thresholds (rules §5.2)
KOSPI_CRASH_PCT = -3.0
PRICE_LIMIT_APPROACH_PCT = 25.0    # 상/하한가 ±30% 근접선
GAP_UP_BLOCK_PCT = 5.0             # 갭 상승 5% 이상 당일 신규 매수 보류


def can_trade_today(state: MarketState) -> tuple[bool, str]:
    """Global gate. Returns (ok, reason).

    KOSPI 가 당일 -3% 이상 급락이면 신규 매수 전면 금지 (기존 포지션 청산은 별개).
    데이터가 없으면(None) **차단하지 않는다** — 시장 지수 조회 실패를 이유로
    매매 전면 OFF 하면 오작동 가능. 단, 리스크 이벤트 로그는 남길 것(호출측 책임).
    """
    if state.kospi_change_pct is not None and state.kospi_change_pct <= KOSPI_CRASH_PCT:
        return False, f"KOSPI {state.kospi_change_pct:.2f}% (≤ {KOSPI_CRASH_PCT}%)"
    return True, ""


def can_enter_symbol(state: MarketState, symbol: str) -> tuple[bool, str]:
    """Per-symbol entry gate for NEW BUYS.

    - 거래정지: 매수 차단
    - VI 발동 중: 차단 (해제 후 5분 관망은 상위 레이어 책임)
    - 전일대비 ±25% 이상: 상/하한가 근접으로 판단, 신규 진입 차단
    - 시가 갭 상승 ≥ 5%: 당일 신규 매수 보류
    """
    if symbol in state.halted:
        return False, "거래정지"
    if symbol in state.vi_active:
        return False, "VI 발동 중"
    change = state.price_change_pct.get(symbol)
    if change is not None and abs(change) >= PRICE_LIMIT_APPROACH_PCT:
        return False, f"상/하한가 근접 ({change:+.2f}%)"
    gap = state.gap_open_pct.get(symbol)
    if gap is not None and gap >= GAP_UP_BLOCK_PCT:
        return False, f"갭 상승 {gap:+.2f}% (≥ {GAP_UP_BLOCK_PCT}%)"
    return True, ""


def can_exit_symbol(state: MarketState, symbol: str) -> tuple[bool, str]:
    """청산 경로는 거의 항상 허용.

    예외: 당일 거래정지 종목은 청산이 불가 (주문이 접수되지 않음).
    호출측은 이 경우 pending-sell 큐에 넣어 해제 후 재시도해야 함.
    """
    if symbol in state.halted:
        return False, "거래정지 (청산 불가 — 해제 대기)"
    return True, ""
