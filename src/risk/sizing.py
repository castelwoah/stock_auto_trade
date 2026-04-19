"""Position sizing caps (rules §3.1 and §5.3).

종목당 20% / 섹터당 40% 상한. 사이징 방식 선택:
  - kelly: 1/4 Kelly (edge·win-rate 기반)
  - equal: 균등 1/N
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SizingPolicy:
    per_symbol_cap_pct: float = 20.0
    per_sector_cap_pct: float = 40.0
    target_position_count: int = 5     # 균등 분할 시 분모 (rules §6 Step3: 5~10)
    kelly_fraction: float = 0.25       # Kelly/4


def compute_max_additional_notional(
    *,
    symbol: str,
    portfolio_value: float,
    existing_positions_notional: dict[str, float],
    sector_map: dict[str, str] | None = None,
    sector: str | None = None,
    policy: SizingPolicy = SizingPolicy(),
) -> float:
    """해당 종목에 **추가로** 투입 가능한 최대 금액 (원). 0 이하이면 매수 차단.

    existing_positions_notional: 심볼 → 현재 평가액.
    sector: symbol 의 섹터. None 이면 sector_map 에서 조회.
    portfolio_value: 현금 + 평가액 전체.
    """
    if portfolio_value <= 0:
        return 0.0

    symbol_cap = portfolio_value * (policy.per_symbol_cap_pct / 100.0)
    current_symbol = existing_positions_notional.get(symbol, 0.0)
    symbol_headroom = symbol_cap - current_symbol

    sector_headroom = float("inf")
    sec = sector if sector is not None else (sector_map or {}).get(symbol)
    if sec is not None and sector_map is not None:
        sector_cap = portfolio_value * (policy.per_sector_cap_pct / 100.0)
        current_sector = sum(
            v for s, v in existing_positions_notional.items() if sector_map.get(s) == sec
        )
        sector_headroom = sector_cap - current_sector

    return max(0.0, min(symbol_headroom, sector_headroom))


def compute_equal_weight_notional(
    *,
    cash: float,
    target_count: int | None = None,
    policy: SizingPolicy = SizingPolicy(),
) -> float:
    """1/N 균등 배분."""
    n = target_count if target_count is not None else policy.target_position_count
    if n <= 0:
        return 0.0
    return max(0.0, cash) / n


def compute_kelly_notional(
    *,
    win_rate: float,
    win_loss_ratio: float,
    portfolio_value: float,
    policy: SizingPolicy = SizingPolicy(),
) -> float:
    """Kelly criterion with fractional Kelly (default 1/4).

    Kelly fraction f* = (p * b − q) / b,  where
      p = win_rate, q = 1 − p, b = win_loss_ratio (avg win / avg loss).
    f* 가 음수면 0 (betting not favorable). 최종: f* × fraction × portfolio.
    """
    if not (0.0 <= win_rate <= 1.0):
        raise ValueError("win_rate must be in [0, 1]")
    if win_loss_ratio <= 0:
        return 0.0
    p = win_rate
    q = 1.0 - p
    b = win_loss_ratio
    f_star = (p * b - q) / b
    if f_star <= 0:
        return 0.0
    return max(0.0, portfolio_value) * f_star * policy.kelly_fraction


def resolve_quantity(
    *,
    max_notional: float,
    price: float,
    lot_size: int = 1,
) -> int:
    """Convert max notional into share quantity with lot rounding (floor)."""
    if price <= 0 or max_notional <= 0 or lot_size <= 0:
        return 0
    raw = int(max_notional // price)
    return (raw // lot_size) * lot_size
