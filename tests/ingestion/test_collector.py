"""뉴스 수집기 — 중복 제거 · 요약만 저장 · 종목 매칭 저장 · DART 종목코드/시각."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.ingestion.base_scraper import Article, BaseScraper
from src.ingestion.collector import collect_news
from src.ingestion.symbol_resolver import SymbolResolver
from src.persistence.models import ArticleSymbol, Base, NewsArticle

NOW = datetime(2026, 10, 23, 10, 0)


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


class Fake(BaseScraper):
    def __init__(self, slug, items):
        self.slug = slug
        super().__init__()
        self.items = items

    def fetch(self, since=None):
        yield from self.items


def _art(ext, title, body="", url=None, source="yonhap_rss", pub=datetime(2026, 10, 23, 9, 0), symbols=()):
    return Article(source=source, external_id=ext, url=url or f"https://yna.co.kr/{ext}",
                   title=title, body=body, published_at=pub, symbols=symbols)


RES = SymbolResolver.from_dict({"삼성전자": "005930", "카카오": "035720", "기아": "000270"})


def test_inserts_summary_only_and_links_symbols(session):
    r = collect_news(session, [Fake("yonhap_rss", [
        _art("a1", "삼성전자가 신고가", "카카오와 기아도 강세 마감"),
    ])], RES, now=NOW)
    assert (r.inserted, r.symbol_links) == (1, 3)
    row = session.query(NewsArticle).one()
    assert row.body_text == "" and row.summary.startswith("카카오와") and row.retrieved_at == NOW
    links = {a.symbol: a.confidence for a in session.query(ArticleSymbol)}
    assert links == {"005930": 1.0, "035720": 1.0, "000270": 0.7}     # 저신뢰도도 저장


def test_dedup_by_external_id_url_and_simhash(session):
    collect_news(session, [Fake("yonhap_rss", [_art("a1", "삼성전자 3분기 실적 발표 영업이익 증가 전망")])],
                 RES, now=NOW)
    r = collect_news(session, [Fake("yonhap_rss", [
        _art("a1", "다른 제목"),                                      # 같은 외부 ID
        _art("a2", "다른 제목2", url="https://yna.co.kr/a1?utm_source=x"),   # 같은 URL (추적 파라미터)
        _art("a3", "삼성전자 3분기 실적 발표 영업이익 증가 전망"),            # 같은 본문 (다른 피드)
        _art("a4", "카카오 신규 서비스 출시"),
    ])], RES, now=NOW)
    assert (r.inserted, r.duplicates) == (1, 3)


def test_dart_uses_stock_code_and_first_seen_time(session):
    collect_news(session, [Fake("dart", [
        _art("2026102300001", "주요사항보고서(유상증자결정)", source="dart",
             pub=datetime(2026, 10, 23, 9, 0), symbols=("035720",)),
        _art("2026102200009", "삼성전자 기재정정", source="dart",
             pub=datetime(2026, 10, 22, 9, 0), symbols=("005930",)),
    ])], RES, now=NOW)
    rows = {a.external_id: a for a in session.query(NewsArticle)}
    assert rows["2026102300001"].published_at == NOW                 # 당일 접수 → 처음 본 시각
    assert rows["2026102200009"].published_at == datetime(2026, 10, 22, 9, 0)
    assert {(a.symbol, a.confidence) for a in session.query(ArticleSymbol)} == {("035720", 1.0), ("005930", 1.0)}


def test_failing_scraper_is_isolated(session):
    class Boom(Fake):
        def fetch(self, since=None):
            raise RuntimeError("feed down")
    r = collect_news(session, [Boom("dart", []), Fake("yonhap_rss", [_art("a9", "기아 신차")])],
                     RES, now=NOW)
    assert r.inserted == 1 and r.by_source == {"yonhap_rss": 1}
