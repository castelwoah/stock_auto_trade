from src.risk.blacklist import BlacklistEntry, MANAGED, StaticBlacklist
from src.scheduler.watchlist import (
    LEGACY_SYMBOLS,
    TOP_MARKET_CAP_KOSDAQ,
    TOP_MARKET_CAP_KOSPI,
    build_initial_watchlist,
    symbols_of,
)


def test_default_watchlist_size_29():
    wl = build_initial_watchlist()
    assert len(wl) == 29                      # 091990 상장폐지 제외 (2026-10-23)
    assert "091990" not in symbols_of(wl)


def test_legacy_first():
    wl = build_initial_watchlist()
    legacy = [e for e in wl[:12] if e.source == "legacy"]
    assert len(legacy) == 12


def test_kospi_then_kosdaq():
    wl = build_initial_watchlist()
    kospi_idx = next(i for i, e in enumerate(wl) if e.source == "kospi_top")
    kosdaq_idx = next(i for i, e in enumerate(wl) if e.source == "kosdaq_top")
    assert kospi_idx < kosdaq_idx


def test_blacklist_exclusion():
    # 블랙리스트 종목은 watchlist 에서 제외
    bl = StaticBlacklist.from_iterable([
        BlacklistEntry(symbol="005930", reason=MANAGED),
    ])
    wl = build_initial_watchlist(blacklist=bl)
    syms = symbols_of(wl)
    assert "005930" not in syms


def test_exclude_legacy_flag():
    wl = build_initial_watchlist(include_legacy=False)
    assert all(e.source != "legacy" for e in wl)


def test_max_size_truncates():
    wl = build_initial_watchlist(max_size=5)
    assert len(wl) == 5


def test_symbol_maps_shapes():
    assert len(LEGACY_SYMBOLS) == 12
    assert len(TOP_MARKET_CAP_KOSPI) == 10
    assert len(TOP_MARKET_CAP_KOSDAQ) == 7


def test_no_duplicates():
    wl = build_initial_watchlist()
    syms = symbols_of(wl)
    assert len(syms) == len(set(syms))
