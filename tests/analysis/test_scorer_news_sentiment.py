"""Unit + benchmark tests for W3 main (sentiment, news, scorer).

Agent×3 factors (technical, financial, flow) are mocked with FactorScore instances.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from src.analysis.contracts import FactorScore
from src.analysis.news import NewsArticleInput, compute_news_score, filter_look_ahead
from src.analysis.scorer import (
    CANDIDATE_THRESHOLD,
    FACTOR_WEIGHTS,
    SCORING_VERSION,
    compose,
    prompt_hash,
    to_stock_score_row,
)
from src.analysis.sentiment import (
    SENTIMENT_MODEL_VER,
    aggregate_sentiment,
    analyze,
    source_weight,
)


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------

def test_analyze_positive():
    r = analyze("삼성전자 1분기 어닝서프라이즈 흑자전환")
    assert r.score > 0.5
    assert r.positive_hits >= 2
    assert r.negative_hits == 0


def test_analyze_negative():
    r = analyze("관리종목 지정 + 감사의견 거절")
    assert r.score < -0.5
    assert r.negative_hits >= 2


def test_analyze_mixed():
    r = analyze("매출은 증가했으나 영업손실 확대")
    # 매출 증가 +0.8 / 영업손실 +1.5 → 부정 쪽으로 기울어야
    assert r.score < 0


def test_analyze_empty_returns_zero():
    assert analyze("").score == 0.0
    assert analyze("   ").score == 0.0


def test_analyze_no_keywords_returns_zero():
    assert analyze("오늘 날씨가 좋다").score == 0.0


def test_negation_marker_skips_hit():
    # '철회' 가 앞에 오면 '수주' 카운트되지 않아야 함
    r = analyze("대규모 수주 철회 결정으로 주가 하락")
    # '철회' 가 NEGATION_MARKERS 에 포함 → '대규모 수주' 건너뜀
    # 하지만 '하한가' 없음, '하락' 도 사전에 없음. hits=0 가능
    assert r.positive_hits == 0


def test_source_weight_ordering():
    assert source_weight("dart") > source_weight("yonhap_rss")
    assert source_weight("unknown") == 0.5


def test_aggregate_sentiment_weighted():
    # 같은 문장이라도 source 가중치가 다르면 집계 결과가 달라야
    items = [
        ("dart", "흑자전환"),
        ("yonhap_rss", "적자전환"),
    ]
    s = aggregate_sentiment(items)
    # dart 가중치(1.0) > yonhap(0.8) 이라 양수 쪽으로 기울어야
    assert s > 0


# ---------------------------------------------------------------------------
# News factor
# ---------------------------------------------------------------------------

def _article(src, title, body, hours_ago=0):
    return NewsArticleInput(
        source=src,
        title=title,
        body=body,
        published_at=datetime.now() - timedelta(hours=hours_ago),
    )


def test_news_factor_no_articles_insufficient():
    f = compute_news_score([])
    assert f.insufficient_data
    assert f.score == 50.0


def test_news_factor_all_neutral():
    articles = [_article("dart", "날씨", "맑음")]
    f = compute_news_score(articles)
    assert not f.insufficient_data
    assert f.score == 50.0


def test_news_factor_positive():
    articles = [
        _article("dart", "흑자전환", "어닝서프라이즈"),
        _article("yonhap_rss", "대규모 수주", ""),
    ]
    f = compute_news_score(articles)
    assert f.score > 70


def test_news_factor_negative():
    articles = [
        _article("dart", "관리종목 지정", ""),
        _article("dart", "거래정지", ""),
    ]
    f = compute_news_score(articles)
    assert f.score < 30


def test_news_factor_time_decay_fresh_dominates_old_opposite():
    # 최신 호재 + 오래된 악재 → 감쇠에 의해 호재가 지배 → score > 50
    # 감쇠가 없었다면 호재/악재가 정확히 상쇄되어 50 이 나와야 함.
    mixed = compute_news_score([
        _article("dart", "흑자전환", "", hours_ago=0),
        _article("dart", "적자전환", "", hours_ago=240),  # 10일 ≈ 5 half-lives → weight ≈ 3%
    ])
    assert mixed.score > 70, f"expected fresh positive to dominate, got {mixed.score}"


def test_filter_look_ahead():
    now = datetime(2026, 4, 19, 10, 0, 0)
    items = [
        NewsArticleInput("dart", "t1", "", now - timedelta(seconds=30)),  # < 60s 전 → 탈락
        NewsArticleInput("dart", "t2", "", now - timedelta(seconds=120)),
    ]
    visible = filter_look_ahead(items, as_of=now, min_delay_seconds=60)
    assert len(visible) == 1
    assert visible[0].title == "t2"


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

def test_compose_weights_sum_to_one():
    assert abs(sum(FACTOR_WEIGHTS.values()) - 1.0) < 1e-9


def test_compose_high_total_is_candidate():
    f = lambda s: FactorScore(score=s)
    cs = compose(
        symbol="005930",
        news=f(90), financial=f(80), technical=f(85), flow=f(75),
    )
    assert cs.total >= CANDIDATE_THRESHOLD
    assert cs.is_candidate
    assert cs.scoring_ver == SCORING_VERSION
    assert cs.sentiment_model_ver == SENTIMENT_MODEL_VER


def test_compose_low_total_not_candidate():
    f = lambda s: FactorScore(score=s)
    cs = compose(symbol="X", news=f(50), financial=f(50), technical=f(50), flow=f(50))
    assert cs.total == 50.0
    assert not cs.is_candidate


def test_insufficient_blocks_candidacy_even_if_total_high():
    cs = compose(
        symbol="X",
        news=FactorScore(score=99),
        financial=FactorScore(score=99, insufficient_data=True),
        technical=FactorScore(score=99),
        flow=FactorScore(score=99),
    )
    # total >= 70 지만 insufficient 있으면 candidate 아님
    assert cs.total >= CANDIDATE_THRESHOLD
    assert not cs.is_candidate
    assert "financial" in cs.insufficient


def test_insufficient_replaces_score_with_50():
    cs = compose(
        symbol="X",
        news=FactorScore(score=100, insufficient_data=True),
        financial=FactorScore(score=100),
        technical=FactorScore(score=100),
        flow=FactorScore(score=100),
    )
    # news 만 50으로 대체, total = 50*0.3 + 100*0.7 = 85
    assert cs.news == 50.0
    assert abs(cs.total - 85.0) < 1e-6


def test_to_stock_score_row_shape():
    f = lambda s: FactorScore(score=s)
    cs = compose(symbol="005930", news=f(80), financial=f(70), technical=f(75), flow=f(65))
    row = to_stock_score_row(cs)
    assert row["symbol"] == "005930"
    assert row["scoring_ver"] == SCORING_VERSION
    assert row["sentiment_model_ver"] == SENTIMENT_MODEL_VER
    assert row["total_score"] == cs.total
    assert "score_date" in row


def test_prompt_hash_stable_and_truncated():
    h1 = prompt_hash("hello world")
    h2 = prompt_hash("hello world")
    assert h1 == h2
    assert len(h1) == 16
    assert prompt_hash(None) is None
    assert prompt_hash("") is None


# ---------------------------------------------------------------------------
# Benchmark — W3 Gate: 100 symbols < 10s
# ---------------------------------------------------------------------------

def test_scoring_100_symbols_under_10s():
    # 각 종목당 5 기사, 4 팩터 compose → 100 종목
    articles = [
        _article("dart", "흑자전환 어닝서프라이즈", "대규모 수주 체결", hours_ago=1),
        _article("yonhap_rss", "매출 증가", "영업이익 증가", hours_ago=4),
        _article("yonhap_rss", "신고가", "호실적", hours_ago=10),
        _article("dart", "자사주 매입", "배당 확대", hours_ago=18),
        _article("naver_disclosure", "특허 취득", "", hours_ago=30),
    ]
    f = lambda s: FactorScore(score=s)
    t0 = time.monotonic()
    for i in range(100):
        sym = f"{i:06d}"
        news_f = compute_news_score(articles)
        compose(
            symbol=sym,
            news=news_f,
            financial=f(70.0 + (i % 20)),
            technical=f(60.0 + (i % 30)),
            flow=f(55.0 + (i % 25)),
        )
    elapsed = time.monotonic() - t0
    # Gate: 10초 이내 (여기서는 훨씬 빨라야 정상)
    assert elapsed < 10.0, f"scoring 100 symbols took {elapsed:.3f}s (gate: <10s)"
    print(f"[bench] 100 symbols in {elapsed*1000:.1f}ms")
