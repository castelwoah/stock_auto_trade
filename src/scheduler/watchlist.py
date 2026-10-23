"""예비 watchlist — rules §3.1 + v2 개선점 C6.

news-universe-v1 (2026-10-23) 이후 역할: UNIVERSE_MODE=static 일 때의 감시 종목이자,
news_with_fallback 에서 뉴스 후보가 모자랄 때 채우는 예비 목록.
29종목: legacy symbol_dict 12 + 시총 상위 17 (KOSPI 10 + KOSDAQ 7).
091990 셀트리온헬스케어는 상장폐지(종목 마스터 부재 확인)로 제외. 블랙리스트 자동 제외.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.risk.blacklist import BlacklistProvider


# Legacy symbol_dict (W1 이관 원본)
LEGACY_SYMBOLS: dict[str, str] = {
    "005930": "삼성전자",
    "035720": "카카오",
    "000660": "SK하이닉스",
    "036630": "세종텔레콤",
    "166090": "하나머티리얼즈",
    "020150": "롯데에너지머티리얼즈",
    "001500": "현대차증권",
    "267250": "HD현대",
    "489460": "바이오비쥬",
    "036540": "SFA반도체",
    "131970": "두산테스나",
    "126730": "코칩",
}

# 시총 상위 18 (KOSPI 10 + KOSDAQ 8; 2026-04 기준 추정, 주기적 재평가 필요)
TOP_MARKET_CAP_KOSPI: dict[str, str] = {
    "005935": "삼성전자우",
    "000270": "기아",
    "005380": "현대차",
    "068270": "셀트리온",
    "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스",
    "051910": "LG화학",
    "035420": "NAVER",
    "006400": "삼성SDI",
    "105560": "KB금융",
}

TOP_MARKET_CAP_KOSDAQ: dict[str, str] = {
    "247540": "에코프로비엠",
    "086520": "에코프로",
    "196170": "알테오젠",
    "240810": "원익IPS",
    "039030": "이오테크닉스",
    "028300": "HLB",
    "263750": "펄어비스",
}


@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    name: str
    source: str   # 'legacy' | 'kospi_top' | 'kosdaq_top'


def build_initial_watchlist(
    blacklist: BlacklistProvider | None = None,
    *,
    include_legacy: bool = True,
    max_size: int = 30,
) -> list[WatchlistEntry]:
    """Paper trading Phase 3 초기 30종목 구성.

    - legacy 12 우선 → KOSPI 상위 10 → KOSDAQ 상위 8
    - 블랙리스트 제외
    - max_size 초과 시 앞에서부터 자름
    """
    entries: list[WatchlistEntry] = []
    seen: set[str] = set()

    def _try_add(symbol: str, name: str, source: str) -> None:
        if symbol in seen:
            return
        if blacklist is not None and blacklist.is_blacklisted(symbol):
            return
        entries.append(WatchlistEntry(symbol=symbol, name=name, source=source))
        seen.add(symbol)

    if include_legacy:
        for sym, name in LEGACY_SYMBOLS.items():
            _try_add(sym, name, "legacy")
    for sym, name in TOP_MARKET_CAP_KOSPI.items():
        _try_add(sym, name, "kospi_top")
    for sym, name in TOP_MARKET_CAP_KOSDAQ.items():
        _try_add(sym, name, "kosdaq_top")

    return entries[:max_size]


def symbols_of(entries: Iterable[WatchlistEntry]) -> list[str]:
    return [e.symbol for e in entries]
