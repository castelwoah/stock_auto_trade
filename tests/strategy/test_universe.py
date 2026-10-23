"""감시 종목 구성 — 뉴스 후보 · 예비 목록 채움 · 보유 유지 · 악재 차단 · D4/D5 · 점수 기록."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.persistence.models import ArticleSymbol, Base, NewsArticle, Position, StockScore
from src.strategy.universe import (
    SCORING_VER,
    SOURCE_HELD,
    SOURCE_NEWS,
    SOURCE_STATIC,
    UniverseParams,
    build_universe,
    lookback_start,
)

NOW = datetime(2026, 10, 23, 9, 30)          # 금요일 → 직전 영업일 10-22 15:30
STATIC = [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("035720", "카카오")]
NAMES = {"005930": "삼성전자", "000660": "SK하이닉스", "035720": "카카오",
         "111110": "호재전자", "222220": "악재바이오", "333330": "중립상사", "444440": "짧은"}


@pytest.fixture
def s():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as session:
        yield session


_n = [0]


def _news(s, sym, title, *, pub=datetime(2026, 10, 23, 8, 0), conf=1.0, source="yonhap_rss"):
    _n[0] += 1
    a = NewsArticle(source=source, external_id=f"e{_n[0]}", url=f"u{_n[0]}", url_hash=f"h{_n[0]}",
                    title=title, summary="", published_at=pub)
    s.add(a)
    s.flush()
    s.add(ArticleSymbol(article_id=a.id, symbol=sym, confidence=conf))
    s.flush()
    return a.id


def _build(s, **params):
    return build_universe(s, now=NOW, params=UniverseParams(**params), static=STATIC, names=NAMES)


def test_lookback_is_prev_business_day_close():
    assert lookback_start(NOW) == datetime(2026, 10, 22, 15, 30)
    assert lookback_start(datetime(2026, 10, 26, 9, 0)) == datetime(2026, 10, 23, 15, 30)   # 월 → 금


def test_news_candidates_ranked_and_static_fallback(s):
    _news(s, "111110", "호재전자 대규모 수주 공급계약 체결")
    _news(s, "333330", "중립상사 대표 인터뷰")
    u = _build(s, min_news=10, max_size=3)
    assert u.entries[0].symbol == "111110" and u.entries[0].source == SOURCE_NEWS
    assert u.entries[0].news_score >= 60 and u.entries[0].article_ids
    assert [e.symbol for e in u.entries[1:]] == ["005930", "000660"]          # max_size 까지 채움
    assert all(e.source == SOURCE_STATIC for e in u.entries[1:])
    assert "333330" not in u.symbols                                            # 중립(50) 은 후보 아님


def test_news_mode_has_no_fallback(s):
    _news(s, "111110", "호재전자 흑자전환")
    u = _build(s, mode="news", min_news=10)
    assert u.symbols == ["111110"]


def test_enough_news_skips_fallback(s):
    _news(s, "111110", "호재전자 흑자전환")
    u = _build(s, min_news=1)
    assert u.symbols == ["111110"]


def test_negative_news_blocks_entry_and_static_fill(s):
    _news(s, "222220", "악재바이오 횡령 배임 혐의 거래정지")
    _news(s, "005930", "삼성전자 어닝쇼크 영업손실")
    u = _build(s)
    assert {"222220", "005930"} <= u.blocked
    assert "005930" not in u.symbols                                           # 예비 목록에서도 제외


def test_held_symbols_always_included(s):
    s.add(Position(symbol="999990", strategy_id="breakout", qty=3, avg_price=1000.0,
                   opened_at=NOW, highest_price=1000.0, updated_at=NOW))
    s.flush()
    u = _build(s, mode="news")
    assert u.symbols == ["999990"] and u.entries[0].source == SOURCE_HELD


def test_d4_d5_and_lookback_filters(s):
    _news(s, "111110", "호재전자 흑자전환", conf=0.7)                              # D4 미달
    _news(s, "444440", "짧은 흑자전환", pub=datetime(2026, 10, 23, 9, 29, 30))     # 60초 미경과
    _news(s, "035720", "카카오 흑자전환", pub=datetime(2026, 10, 22, 15, 0))       # 전일 장중
    _news(s, "999999", "상장 안 된 종목 흑자전환")                                  # 마스터 밖
    u = _build(s, mode="news")
    assert u.symbols == []


def test_scores_written_and_replaced(s):
    _news(s, "111110", "호재전자 흑자전환")
    _build(s)
    _news(s, "333330", "중립상사 수주")
    _build(s)
    rows = {r.symbol: r for r in s.query(StockScore).filter_by(scoring_ver=SCORING_VER)}
    assert set(rows) == {"111110", "333330"}
    r = rows["111110"]
    assert r.total_score == r.news_score and r.technical_score == 50.0 and r.sentiment_model_ver


def test_static_mode_ignores_news(s):
    _news(s, "111110", "호재전자 흑자전환")
    u = _build(s, mode="static")
    assert u.symbols == [c for c, _ in STATIC] and s.query(StockScore).count() == 0
