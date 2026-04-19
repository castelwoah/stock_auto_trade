"""Static + liquidity blacklist (rules §4.3).

제외 기준:
- 관리/투자경고/투자위험 종목
- 거래정지 / 상장폐지 실질심사 대상
- 우선주, SPAC, ETN (초기 버전 한정)
- 시가총액 < 500억
- 일평균 거래대금 < 10억
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Protocol


# Blacklist reason tags
MANAGED = "관리종목"
WARNING = "투자경고"
CAUTION = "투자주의"
RISK = "투자위험"
HALTED = "거래정지"
DELISTING_REVIEW = "상장폐지실질심사"
SPAC = "SPAC"
ETN = "ETN"
PREFERRED = "우선주"


@dataclass(frozen=True)
class BlacklistEntry:
    symbol: str
    reason: str
    since: date | None = None


class BlacklistProvider(Protocol):
    def is_blacklisted(self, symbol: str) -> BlacklistEntry | None: ...


@dataclass
class StaticBlacklist:
    """In-memory blacklist. Loaded from KIS/KRX feeds externally."""

    _by_symbol: dict[str, BlacklistEntry] = field(default_factory=dict)

    @classmethod
    def from_iterable(cls, entries: Iterable[BlacklistEntry]) -> "StaticBlacklist":
        b = cls()
        for e in entries:
            b._by_symbol[e.symbol] = e
        return b

    def add(self, entry: BlacklistEntry) -> None:
        self._by_symbol[entry.symbol] = entry

    def remove(self, symbol: str) -> None:
        self._by_symbol.pop(symbol, None)

    def is_blacklisted(self, symbol: str) -> BlacklistEntry | None:
        return self._by_symbol.get(symbol)

    def __len__(self) -> int:
        return len(self._by_symbol)


# ---------------------------------------------------------------------------
# Liquidity thresholds (rules §4.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LiquidityThresholds:
    min_market_cap_krw: int = 50_000_000_000   # 500억
    min_avg_volume_krw: int = 1_000_000_000    # 10억


def check_liquidity(
    *,
    market_cap: int | None,
    avg_volume_krw: int | None,
    thresholds: LiquidityThresholds = LiquidityThresholds(),
) -> tuple[bool, str]:
    """Return (ok, reason). Missing data → fail closed."""
    if market_cap is None:
        return False, "시가총액 데이터 없음"
    if avg_volume_krw is None:
        return False, "거래대금 데이터 없음"
    if market_cap < thresholds.min_market_cap_krw:
        return False, (
            f"시총 {market_cap:,} < 최저 {thresholds.min_market_cap_krw:,}"
        )
    if avg_volume_krw < thresholds.min_avg_volume_krw:
        return False, (
            f"거래대금 {avg_volume_krw:,} < 최저 {thresholds.min_avg_volume_krw:,}"
        )
    return True, ""
