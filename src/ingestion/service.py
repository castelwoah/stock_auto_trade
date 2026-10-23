"""뉴스 서비스 — 스케줄러 job(master_refresh / news_collect / analyze_score)의 본체.

마스터·resolver 는 프로세스 메모리에 두고, 데몬이 장중에 재시작되면 최근 보관본을
다시 읽는다 (없으면 즉시 수신). 각 메서드는 자체 DB 세션을 연다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable, ContextManager

from sqlalchemy.orm import Session

from src.config import Settings
from src.ingestion.base_scraper import BaseScraper
from src.ingestion.collector import CollectResult, collect_news
from src.ingestion.master import Master, MasterStore
from src.ingestion.scrapers.dart import DartScraper
from src.ingestion.scrapers.yonhap_rss import YonhapRssScraper
from src.ingestion.symbol_resolver import SymbolResolver
from src.persistence.db import session_scope
from src.scheduler.watchlist import build_initial_watchlist
from src.strategy.universe import Universe, UniverseParams, build_universe, lookback_start


logger = logging.getLogger(__name__)

DART_MAX_PAGES = 10


def universe_params(settings: Settings) -> UniverseParams:
    return UniverseParams(
        mode=settings.UNIVERSE_MODE,
        max_size=settings.UNIVERSE_MAX,
        min_news=settings.UNIVERSE_MIN_NEWS,
        min_score=settings.NEWS_MIN_SCORE,
        block_score=settings.NEWS_BLOCK_SCORE,
        min_confidence=settings.NEWS_MIN_CONFIDENCE,
        half_life_hours=settings.NEWS_HALF_LIFE_HOURS,
    )


def default_scrapers(settings: Settings) -> list[BaseScraper]:
    scrapers: list[BaseScraper] = [YonhapRssScraper()]
    if settings.DART_API_KEY is not None:
        scrapers.append(DartScraper(settings.DART_API_KEY.get_secret_value(),
                                    max_pages=DART_MAX_PAGES))
    return scrapers


class NewsService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: MasterStore | None = None,
        scrapers: Callable[[], list[BaseScraper]] | None = None,
        session_factory: Callable[[], ContextManager[Session]] = session_scope,
    ) -> None:
        self.settings = settings
        self.store = store or MasterStore(settings.MASTER_DIR)
        self._scrapers = scrapers or (lambda: default_scrapers(settings))
        self._session = session_factory
        self.static = [(e.symbol, e.name) for e in build_initial_watchlist()]
        self._master: Master | None = None
        self._resolver: SymbolResolver | None = None

    # ------------------------------------------------------------------
    def refresh_master(self, today: date) -> Master | None:
        m = self.store.refresh(today)
        self._set_master(m)
        return m

    def master(self, today: date) -> Master | None:
        if self._master is None:
            self._set_master(self.store.load_latest() or self.store.refresh(today))
        return self._master

    def names(self, today: date) -> dict[str, str]:
        m = self.master(today)
        return m.common_stocks if m is not None else {}

    def _set_master(self, m: Master | None) -> None:
        if m is None:
            return
        self._master = m
        self._resolver = SymbolResolver.from_master(m)

    # ------------------------------------------------------------------
    def collect(self, now: datetime) -> CollectResult | None:
        if self.master(now.date()) is None or self._resolver is None:
            logger.warning("news_collect skipped — no symbol master available")
            return None
        with self._session() as s:
            r = collect_news(s, self._scrapers(), self._resolver, now=now,
                             since=lookback_start(now))
        logger.info("news_collect: fetched %d, new %d, dup %d, symbol links %d %s",
                    r.fetched, r.inserted, r.duplicates, r.symbol_links, r.by_source)
        return r

    def build_universe(self, now: datetime) -> Universe:
        names = self.names(now.date())
        with self._session() as s:
            return build_universe(s, now=now, params=universe_params(self.settings),
                                  static=self.static, names=names or None)
