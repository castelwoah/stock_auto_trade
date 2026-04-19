"""Tests for src.backtest.data_loader."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.analysis.news import NewsArticleInput
from src.backtest.data_loader import (
    Bar,
    CsvDataLoader,
    SyntheticDataLoader,
    _business_days,
)


SYMS = ["AAA", "BBB"]
START = date(2026, 1, 5)   # Monday
END = date(2026, 1, 16)    # Friday (2 weeks later)


# ---------------- SyntheticDataLoader ----------------


def test_synthetic_bars_count_equals_business_days():
    loader = SyntheticDataLoader(symbols=SYMS, start=START, end=END, seed=1)
    bdays = _business_days(START, END)
    bars = loader.bars("AAA", start=START, end=END)
    assert len(bars) == len(bdays)
    # 1주 5영업일 × 2주 = 10
    assert len(bars) == 10


def test_synthetic_excludes_weekends():
    loader = SyntheticDataLoader(symbols=SYMS, start=START, end=END, seed=1)
    bars = loader.bars("AAA", start=START, end=END)
    for b in bars:
        assert b.ts.weekday() < 5, f"weekend bar: {b.ts}"
        assert b.ts.hour == 15 and b.ts.minute == 30


def test_synthetic_determinism_same_seed():
    l1 = SyntheticDataLoader(symbols=SYMS, start=START, end=END, seed=7)
    l2 = SyntheticDataLoader(symbols=SYMS, start=START, end=END, seed=7)
    assert l1.bars("AAA", start=START, end=END) == l2.bars(
        "AAA", start=START, end=END
    )
    as_of = datetime(2026, 1, 10, 10, 0, 0)
    assert l1.news(as_of=as_of) == l2.news(as_of=as_of)


def test_synthetic_different_seeds_differ():
    l1 = SyntheticDataLoader(symbols=SYMS, start=START, end=END, seed=1)
    l2 = SyntheticDataLoader(symbols=SYMS, start=START, end=END, seed=2)
    assert l1.bars("AAA", start=START, end=END) != l2.bars(
        "AAA", start=START, end=END
    )


def test_synthetic_prices_as_of_uses_only_past_bars():
    loader = SyntheticDataLoader(symbols=SYMS, start=START, end=END, seed=3)
    bars = loader.bars("AAA", start=START, end=END)
    # bars[0].ts = 2026-01-05 15:30
    # as_of just before first bar -> empty dict (no symbol entry)
    just_before = bars[0].ts - timedelta(seconds=1)
    assert loader.prices_as_of(just_before, ["AAA"]) == {}

    # as_of exactly on second bar -> second bar's close
    pricing_ts = bars[1].ts
    assert loader.prices_as_of(pricing_ts, ["AAA"]) == {"AAA": bars[1].close}

    # as_of strictly between bar[1] and bar[2] -> still bar[1]
    between = bars[1].ts + timedelta(hours=1)
    assert loader.prices_as_of(between, ["AAA"]) == {"AAA": bars[1].close}


def test_synthetic_news_d5_filter_boundary():
    loader = SyntheticDataLoader(
        symbols=SYMS, start=START, end=END, seed=9, news_per_day=1
    )
    # 09:30:00 기사가 매 영업일 1건
    first_pub = datetime(2026, 1, 5, 9, 30, 0)
    # published_at + 30s 시점 → 아직 60초 미경과 → 제외
    as_of_30s = first_pub + timedelta(seconds=30)
    pubs_30 = [a.published_at for a in loader.news(as_of=as_of_30s)]
    assert first_pub not in pubs_30

    # published_at + 61s → 60초 경과 → 포함
    as_of_61s = first_pub + timedelta(seconds=61)
    pubs_61 = [a.published_at for a in loader.news(as_of=as_of_61s)]
    assert first_pub in pubs_61


def test_synthetic_news_symbols_filter():
    loader = SyntheticDataLoader(
        symbols=SYMS, start=START, end=END, seed=11, news_per_day=5
    )
    as_of = datetime(2026, 1, 20, 0, 0, 0)
    all_news = loader.news(as_of=as_of)
    aaa_news = loader.news(as_of=as_of, symbols=["AAA"])
    # 필터 결과가 전체보다 작거나 같고, 제목에 AAA 가 있어야 함
    assert len(aaa_news) <= len(all_news)
    assert all("AAA" in a.title for a in aaa_news)
    # union(AAA, BBB) = all
    merged = loader.news(as_of=as_of, symbols=["AAA", "BBB"])
    assert len(merged) == len(all_news)


def test_synthetic_news_returns_news_article_input():
    loader = SyntheticDataLoader(
        symbols=SYMS, start=START, end=END, seed=13, news_per_day=1
    )
    as_of = datetime(2026, 1, 20, 0, 0, 0)
    items = loader.news(as_of=as_of)
    assert items
    for a in items:
        assert isinstance(a, NewsArticleInput)


# ---------------- CsvDataLoader ----------------


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join(r))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _valid_bars_csv(path: Path) -> None:
    _write_csv(
        path,
        ["ts", "symbol", "open", "high", "low", "close", "volume"],
        [
            ["2026-01-05T15:30:00", "AAA", "100.0", "101.0", "99.0", "100.5", "10000"],
            ["2026-01-06T15:30:00", "AAA", "100.5", "102.0", "100.0", "101.5", "12000"],
            ["2026-01-07T15:30:00", "AAA", "101.5", "103.0", "101.0", "102.5", "15000"],
            ["2026-01-05T15:30:00", "BBB", "50.0", "51.0", "49.5", "50.5", "8000"],
        ],
    )


def _valid_news_csv(path: Path) -> None:
    _write_csv(
        path,
        ["published_at", "source", "title", "body", "symbols"],
        [
            ["2026-01-05T09:30:00", "wire", "AAA up", "good", "AAA"],
            ["2026-01-05T10:00:00", "wire", "AAA+BBB", "mixed", "AAA|BBB"],
            ["2026-01-06T09:30:00", "blog", "BBB only", "bbb", "BBB"],
        ],
    )


def test_csv_bars_roundtrip(tmp_path: Path):
    bars_p = tmp_path / "bars.csv"
    news_p = tmp_path / "news.csv"
    _valid_bars_csv(bars_p)
    _valid_news_csv(news_p)

    loader = CsvDataLoader(bars_path=bars_p, news_path=news_p)
    bars = loader.bars("AAA", start=date(2026, 1, 5), end=date(2026, 1, 7))
    assert len(bars) == 3
    assert bars[0] == Bar(
        ts=datetime(2026, 1, 5, 15, 30),
        symbol="AAA",
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10000,
    )
    # date range filter
    bars2 = loader.bars("AAA", start=date(2026, 1, 6), end=date(2026, 1, 6))
    assert len(bars2) == 1
    # non-existent symbol
    assert loader.bars("ZZZ", start=date(2026, 1, 1), end=date(2026, 2, 1)) == []


def test_csv_prices_as_of(tmp_path: Path):
    bars_p = tmp_path / "bars.csv"
    news_p = tmp_path / "news.csv"
    _valid_bars_csv(bars_p)
    _valid_news_csv(news_p)

    loader = CsvDataLoader(bars_path=bars_p, news_path=news_p)
    # as_of 2026-01-06 20:00 → AAA 는 01-06 close, BBB 는 01-05 close
    ts = datetime(2026, 1, 6, 20, 0)
    out = loader.prices_as_of(ts, ["AAA", "BBB"])
    assert out == {"AAA": 101.5, "BBB": 50.5}

    # Before any bar
    early = datetime(2026, 1, 1, 0, 0)
    assert loader.prices_as_of(early, ["AAA", "BBB"]) == {}


def test_csv_news_d5_filter_and_symbols(tmp_path: Path):
    bars_p = tmp_path / "bars.csv"
    news_p = tmp_path / "news.csv"
    _valid_bars_csv(bars_p)
    _valid_news_csv(news_p)

    loader = CsvDataLoader(bars_path=bars_p, news_path=news_p)
    # 01-05 09:30 + 59s → 제외
    as_of_a = datetime(2026, 1, 5, 9, 30, 59)
    assert loader.news(as_of=as_of_a) == []
    # 01-05 09:30 + 60s → 포함 (cutoff = as_of - 60s == published)
    as_of_b = datetime(2026, 1, 5, 9, 31, 0)
    pubs = [a.published_at for a in loader.news(as_of=as_of_b)]
    assert datetime(2026, 1, 5, 9, 30, 0) in pubs

    # symbols filter: only AAA
    as_of_c = datetime(2026, 1, 10, 0, 0)
    aaa = loader.news(as_of=as_of_c, symbols=["AAA"])
    titles = [a.title for a in aaa]
    assert "AAA up" in titles
    assert "AAA+BBB" in titles  # pipe-separated → 포함
    assert "BBB only" not in titles


def test_csv_news_pipe_separated_symbols(tmp_path: Path):
    bars_p = tmp_path / "bars.csv"
    news_p = tmp_path / "news.csv"
    _valid_bars_csv(bars_p)
    _valid_news_csv(news_p)

    loader = CsvDataLoader(bars_path=bars_p, news_path=news_p)
    as_of = datetime(2026, 1, 10, 0, 0)
    bbb = loader.news(as_of=as_of, symbols=["BBB"])
    titles = {a.title for a in bbb}
    # AAA+BBB (pipe) 는 BBB 필터에도 포함되어야 함
    assert "AAA+BBB" in titles
    assert "BBB only" in titles
    assert "AAA up" not in titles


def test_csv_invalid_header_raises(tmp_path: Path):
    bars_p = tmp_path / "bars.csv"
    news_p = tmp_path / "news.csv"
    # wrong header
    _write_csv(
        bars_p,
        ["timestamp", "symbol", "o", "h", "l", "c", "v"],
        [["2026-01-05T15:30:00", "AAA", "1", "1", "1", "1", "1"]],
    )
    _valid_news_csv(news_p)
    with pytest.raises(ValueError):
        CsvDataLoader(bars_path=bars_p, news_path=news_p)

    # valid bars, wrong news header
    _valid_bars_csv(bars_p)
    _write_csv(
        news_p,
        ["time", "source", "title", "body", "symbols"],
        [["2026-01-05T09:30:00", "wire", "t", "b", "AAA"]],
    )
    with pytest.raises(ValueError):
        CsvDataLoader(bars_path=bars_p, news_path=news_p)
