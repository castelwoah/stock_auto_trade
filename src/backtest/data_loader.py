"""Backtest data loaders.

제공 구현체:
  - SyntheticDataLoader: 재현 가능한 합성 OHLCV + 합성 뉴스 (테스트/스모크용)
  - CsvDataLoader: CSV 일괄 로드 (Phase 3 실 데이터 연동 전 간이 경로)

D5 look-ahead 정책: news() 는 `published_at + 60초 <= as_of` 인 기사만 반환.
이 필터는 data_loader 단일 지점에서만 적용하여 engine 에서 재필터 하지 않는다
(이중 계산 방지).
"""
from __future__ import annotations

import csv
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from src.analysis.news import NewsArticleInput


NEWS_MIN_DELAY_SECONDS = 60
_REQUIRED_BARS_HEADER = ("ts", "symbol", "open", "high", "low", "close", "volume")
_REQUIRED_NEWS_HEADER = ("published_at", "source", "title", "body", "symbols")


@dataclass(frozen=True)
class Bar:
    ts: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class BacktestDataLoader(ABC):
    @abstractmethod
    def bars(self, symbol: str, *, start: date, end: date) -> list[Bar]: ...

    @abstractmethod
    def prices_as_of(
        self, ts: datetime, symbols: list[str]
    ) -> dict[str, float]: ...

    @abstractmethod
    def news(
        self,
        *,
        as_of: datetime,
        symbols: list[str] | None = None,
    ) -> list[NewsArticleInput]: ...


def _business_days(start: date, end: date) -> list[date]:
    """start~end(inclusive) 사이 주중(Mon-Fri)만 반환."""
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # 0=Mon .. 4=Fri
            out.append(d)
        d += timedelta(days=1)
    return out


class SyntheticDataLoader(BacktestDataLoader):
    """합성 OHLCV + 합성 뉴스 생성기 (재현성 보장).

    random walk parameters:
      - daily_vol = 0.015  (1.5% 일간 변동성; 한국 대형주 일반 범위)
      - start_price = 100.0
      - intraday_spread ≈ daily_vol (high/low)
      - gap ≈ 0.3% 표준편차
    """

    DAILY_VOL = 0.015
    START_PRICE = 100.0
    GAP_STD = 0.003

    def __init__(
        self,
        *,
        symbols: list[str],
        start: date,
        end: date,
        seed: int = 42,
        news_per_day: int = 3,
    ):
        if not symbols:
            raise ValueError("symbols must be non-empty")
        if end < start:
            raise ValueError("end must be >= start")
        self._symbols = list(symbols)
        self._start = start
        self._end = end
        self._seed = seed
        self._news_per_day = news_per_day
        self._bars_by_symbol: dict[str, list[Bar]] = {}
        self._news: list[tuple[NewsArticleInput, frozenset[str]]] = []
        self._generate()

    # ---- generation ----

    def _generate(self) -> None:
        bdays = _business_days(self._start, self._end)
        # Use per-symbol RNG seeded deterministically so adding symbols does not
        # shift other symbols' price paths.
        for sym in self._symbols:
            rng = random.Random((self._seed, sym, "bars"))
            bars: list[Bar] = []
            prev_close = self.START_PRICE
            for d in bdays:
                gap = rng.gauss(0.0, self.GAP_STD)
                open_ = prev_close * (1.0 + gap)
                drift = rng.gauss(0.0, self.DAILY_VOL)
                close_ = max(0.01, open_ * (1.0 + drift))
                hi_shock = abs(rng.gauss(0.0, self.DAILY_VOL))
                lo_shock = abs(rng.gauss(0.0, self.DAILY_VOL))
                high = max(open_, close_) * (1.0 + hi_shock)
                low = min(open_, close_) * (1.0 - lo_shock)
                low = max(0.01, low)
                volume = rng.randint(10_000, 100_000)
                ts = datetime.combine(d, time(15, 30))
                bars.append(
                    Bar(
                        ts=ts,
                        symbol=sym,
                        open=round(open_, 4),
                        high=round(high, 4),
                        low=round(low, 4),
                        close=round(close_, 4),
                        volume=volume,
                    )
                )
                prev_close = close_
            self._bars_by_symbol[sym] = bars

        # News: deterministic per-day stream
        news_rng = random.Random((self._seed, "news"))
        for d in bdays:
            for i in range(self._news_per_day):
                sym = news_rng.choice(self._symbols)
                published_at = datetime.combine(d, time(9, 30)) + timedelta(
                    seconds=i
                )
                article = NewsArticleInput(
                    source="synthetic",
                    title=f"{sym} {d.isoformat()} 뉴스 {i}",
                    body=f"{sym} 관련 합성 뉴스 본문 #{i}",
                    published_at=published_at,
                )
                self._news.append((article, frozenset({sym})))

    # ---- API ----

    def bars(self, symbol: str, *, start: date, end: date) -> list[Bar]:
        if symbol not in self._bars_by_symbol:
            return []
        return [
            b
            for b in self._bars_by_symbol[symbol]
            if start <= b.ts.date() <= end
        ]

    def prices_as_of(
        self, ts: datetime, symbols: list[str]
    ) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym in symbols:
            bars = self._bars_by_symbol.get(sym, [])
            last: Bar | None = None
            for b in bars:
                if b.ts <= ts:
                    last = b
                else:
                    break
            if last is not None:
                out[sym] = last.close
        return out

    def news(
        self,
        *,
        as_of: datetime,
        symbols: list[str] | None = None,
    ) -> list[NewsArticleInput]:
        cutoff = as_of - timedelta(seconds=NEWS_MIN_DELAY_SECONDS)
        sym_filter = set(symbols) if symbols is not None else None
        out: list[NewsArticleInput] = []
        for article, linked in self._news:
            if article.published_at > cutoff:
                continue
            if sym_filter is not None and not (linked & sym_filter):
                continue
            out.append(article)
        return out


class CsvDataLoader(BacktestDataLoader):
    """CSV 일괄 로드 기반 로더.

    bars.csv 스키마: ts(ISO),symbol,open,high,low,close,volume
    news.csv 스키마: published_at(ISO),source,title,body,symbols(pipe-separated)
    """

    def __init__(self, *, bars_path: Path, news_path: Path):
        self._bars_by_symbol: dict[str, list[Bar]] = {}
        self._news: list[tuple[NewsArticleInput, frozenset[str]]] = []
        self._load_bars(bars_path)
        self._load_news(news_path)

    def _load_bars(self, path: Path) -> None:
        with Path(path).open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise ValueError(f"Empty bars CSV: {path}") from exc
            if tuple(h.strip() for h in header) != _REQUIRED_BARS_HEADER:
                raise ValueError(
                    f"Invalid bars header {header!r}; expected "
                    f"{list(_REQUIRED_BARS_HEADER)}"
                )
            for row in reader:
                if not row:
                    continue
                if len(row) != len(_REQUIRED_BARS_HEADER):
                    raise ValueError(f"Malformed bars row: {row!r}")
                ts_s, symbol, o, h, l, c, v = row
                bar = Bar(
                    ts=datetime.fromisoformat(ts_s),
                    symbol=symbol,
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=int(v),
                )
                self._bars_by_symbol.setdefault(symbol, []).append(bar)
        for sym in self._bars_by_symbol:
            self._bars_by_symbol[sym].sort(key=lambda b: b.ts)

    def _load_news(self, path: Path) -> None:
        with Path(path).open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise ValueError(f"Empty news CSV: {path}") from exc
            if tuple(h.strip() for h in header) != _REQUIRED_NEWS_HEADER:
                raise ValueError(
                    f"Invalid news header {header!r}; expected "
                    f"{list(_REQUIRED_NEWS_HEADER)}"
                )
            for row in reader:
                if not row:
                    continue
                if len(row) != len(_REQUIRED_NEWS_HEADER):
                    raise ValueError(f"Malformed news row: {row!r}")
                pub_s, source, title, body, symbols_s = row
                symbols = frozenset(
                    s for s in (x.strip() for x in symbols_s.split("|")) if s
                )
                article = NewsArticleInput(
                    source=source,
                    title=title,
                    body=body,
                    published_at=datetime.fromisoformat(pub_s),
                )
                self._news.append((article, symbols))
        self._news.sort(key=lambda item: item[0].published_at)

    # ---- API ----

    def bars(self, symbol: str, *, start: date, end: date) -> list[Bar]:
        return [
            b
            for b in self._bars_by_symbol.get(symbol, [])
            if start <= b.ts.date() <= end
        ]

    def prices_as_of(
        self, ts: datetime, symbols: list[str]
    ) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym in symbols:
            bars = self._bars_by_symbol.get(sym, [])
            last: Bar | None = None
            for b in bars:
                if b.ts <= ts:
                    last = b
                else:
                    break
            if last is not None:
                out[sym] = last.close
        return out

    def news(
        self,
        *,
        as_of: datetime,
        symbols: list[str] | None = None,
    ) -> list[NewsArticleInput]:
        cutoff = as_of - timedelta(seconds=NEWS_MIN_DELAY_SECONDS)
        sym_filter = set(symbols) if symbols is not None else None
        out: list[NewsArticleInput] = []
        for article, linked in self._news:
            if article.published_at > cutoff:
                continue
            if sym_filter is not None and not (linked & sym_filter):
                continue
            out.append(article)
        return out
