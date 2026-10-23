"""NewsService — 마스터 지연 로드 · 마스터 없으면 수집 생략 · 수집 → universe 일관 흐름."""
from contextlib import contextmanager
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.ingestion.base_scraper import Article, BaseScraper
from src.ingestion.master import MasterStore
from src.ingestion.service import NewsService, default_scrapers
from src.persistence.models import Base, NewsArticle

from tests.clients.conftest import make_settings
from tests.ingestion.master_fixture import KOSDAQ_ROWS, KOSPI_ROWS, mst_zip

NOW = datetime(2026, 10, 23, 8, 40)


class Feed(BaseScraper):
    slug = "yonhap_rss"

    def fetch(self, since=None):
        yield Article(source="yonhap_rss", external_id="x1", url="https://yna.co.kr/x1",
                      title="카카오가 대규모 수주 공급계약 체결", body="", published_at=datetime(2026, 10, 23, 8, 0))


@pytest.fixture
def factory():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)

    @contextmanager
    def scope():
        with Session(eng) as s:
            yield s
            s.commit()
    return scope


def _fetch(url):
    return mst_zip("kospi", KOSPI_ROWS) if "kospi" in url else mst_zip("kosdaq", KOSDAQ_ROWS)


def test_collect_then_universe(tmp_path, factory):
    svc = NewsService(make_settings(), store=MasterStore(tmp_path, fetch=_fetch),
                      scrapers=lambda: [Feed()], session_factory=factory)
    r = svc.collect(NOW)
    assert r.inserted == 1 and r.symbol_links == 1
    u = svc.build_universe(NOW)
    assert u.entries[0].symbol == "035720" and u.entries[0].name == "카카오"


def test_no_master_skips_collect(tmp_path, factory):
    def boom(url):
        raise OSError("dl")
    svc = NewsService(make_settings(), store=MasterStore(tmp_path, fetch=boom),
                      scrapers=lambda: [Feed()], session_factory=factory)
    assert svc.collect(NOW) is None and svc.names(NOW.date()) == {}
    with factory() as s:
        assert s.query(NewsArticle).count() == 0


def test_dart_only_with_key():
    assert [s.slug for s in default_scrapers(make_settings())] == ["yonhap_rss"]
    assert [s.slug for s in default_scrapers(make_settings(DART_API_KEY="k" * 40))] == ["yonhap_rss", "dart"]
