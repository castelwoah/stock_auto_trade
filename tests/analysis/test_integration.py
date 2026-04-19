"""W3 integration — all four factors feeding into the composite scorer."""
from __future__ import annotations

from datetime import datetime

from src.analysis.financial import compute_financial_score
from src.analysis.flow import compute_flow_score
from src.analysis.news import NewsArticleInput, compute_news_score
from src.analysis.scorer import CANDIDATE_THRESHOLD, compose
from src.analysis.technical import compute_technical_score


def _ohlcv(closes):
    return [
        {"date": f"2026{(i // 30)+1:02d}{(i % 30)+1:02d}", "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000}
        for i, c in enumerate(closes)
    ]


def test_ideal_stock_crosses_candidate_threshold():
    news_f = compute_news_score([
        NewsArticleInput("dart", "어닝서프라이즈 흑자전환", "최대 실적", published_at=datetime.now()),
        NewsArticleInput("dart", "대규모 수주 체결", "자사주 소각", published_at=datetime.now()),
    ])
    tech_f = compute_technical_score(_ohlcv([100.0 + i * 0.3 for i in range(80)]))
    fin_f = compute_financial_score({"debt_ratio": 40, "roe": 22, "operating_margin": 18, "ocf_to_revenue": 14})
    flow_f = compute_flow_score({"foreign_net_5d": 20_000_000_000, "institutional_net_5d": 10_000_000_000, "volume_ratio_5d_vs_60d": 2.0})
    cs = compose(symbol="005930", news=news_f, financial=fin_f, technical=tech_f, flow=flow_f)
    assert cs.is_candidate, f"total={cs.total} details={cs.details}"
    assert cs.total >= CANDIDATE_THRESHOLD


def test_bad_stock_does_not_cross_threshold():
    news_f = compute_news_score([
        NewsArticleInput("dart", "관리종목 지정", "", published_at=datetime.now()),
        NewsArticleInput("dart", "감사의견 거절", "", published_at=datetime.now()),
    ])
    tech_f = compute_technical_score(_ohlcv([200.0 - i * 0.5 for i in range(80)]))
    fin_f = compute_financial_score({"debt_ratio": 250, "roe": -5, "operating_margin": -3, "ocf_to_revenue": -2})
    flow_f = compute_flow_score({"foreign_net_5d": -10_000_000_000, "institutional_net_5d": -5_000_000_000, "volume_ratio_5d_vs_60d": 0.3})
    cs = compose(symbol="XXXXXX", news=news_f, financial=fin_f, technical=tech_f, flow=flow_f)
    assert not cs.is_candidate
    assert cs.total < CANDIDATE_THRESHOLD


def test_insufficient_technical_blocks_candidacy():
    # short ohlcv -> technical insufficient; other factors high
    news_f = compute_news_score([NewsArticleInput("dart", "흑자전환", "", published_at=datetime.now())])
    tech_f = compute_technical_score(_ohlcv([100.0] * 20))  # < 60 bars
    fin_f = compute_financial_score({"debt_ratio": 40, "roe": 22, "operating_margin": 18, "ocf_to_revenue": 14})
    flow_f = compute_flow_score({"foreign_net_5d": 20_000_000_000, "institutional_net_5d": 10_000_000_000, "volume_ratio_5d_vs_60d": 2.0})
    cs = compose(symbol="Y", news=news_f, financial=fin_f, technical=tech_f, flow=flow_f)
    assert "technical" in cs.insufficient
    assert not cs.is_candidate


def test_db_row_persists_reproducibility_fields():
    from src.analysis.scorer import to_stock_score_row

    news_f = compute_news_score([NewsArticleInput("dart", "흑자전환", "", published_at=datetime.now())])
    tech_f = compute_technical_score(_ohlcv([100.0 + i * 0.3 for i in range(80)]))
    fin_f = compute_financial_score({"debt_ratio": 80, "roe": 12, "operating_margin": 10, "ocf_to_revenue": 8})
    flow_f = compute_flow_score({"foreign_net_5d": 5_000_000_000, "institutional_net_5d": 3_000_000_000, "volume_ratio_5d_vs_60d": 1.3})
    cs = compose(symbol="Z", news=news_f, financial=fin_f, technical=tech_f, flow=flow_f)
    row = to_stock_score_row(cs)
    # D12: reproducibility metadata must be persisted
    assert row["scoring_ver"]
    assert row["sentiment_model_ver"]
    assert "llm_prompt_hash" in row
    # Per-factor scores round-trip
    assert row["news_score"] == cs.news
    assert row["technical_score"] == cs.technical
