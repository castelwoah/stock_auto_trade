"""Per-symbol news factor aggregation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from src.analysis.contracts import FactorScore, linear_map
from src.analysis.sentiment import (
    SENTIMENT_MODEL_VER,
    analyze,
    source_weight,
)


@dataclass(frozen=True)
class NewsArticleInput:
    """Minimal shape expected by compute_news_score.

    Scrapers → Article 을 DB 에 저장하고, 여기서는 pure-data 튜플로 받는다.
    scorer 가 DB 에서 조회해 이 구조로 변환해 넘긴다.
    """

    source: str
    title: str
    body: str
    published_at: datetime


def compute_news_score(
    articles: Iterable[NewsArticleInput],
    *,
    now: datetime | None = None,
    half_life_hours: float = 48.0,
) -> FactorScore:
    """Aggregate news sentiment into 0~100.

    - 각 기사에 대해 `analyze(title + body)` 실행
    - 매체 가중치(source_weight) × 시간 감쇠(half_life_hours) 적용
    - 가중 평균 sentiment ∈ [-1, +1] 을 0~100 으로 선형 매핑
    - 중립(키워드 매칭 無) 기사는 평균 계산에서 제외하되 count 는 카운트
    """
    ref = now or datetime.now()
    total_w = 0.0
    total_sw = 0.0
    hit_count = 0
    total_count = 0
    pos_hits = 0
    neg_hits = 0
    for a in articles:
        total_count += 1
        text = f"{a.title}\n{a.body}".strip()
        res = analyze(text)
        if res.positive_hits == 0 and res.negative_hits == 0:
            continue
        hit_count += 1
        pos_hits += res.positive_hits
        neg_hits += res.negative_hits
        # 시간 감쇠
        age_h = max(0.0, (ref - a.published_at).total_seconds() / 3600.0)
        decay = 0.5 ** (age_h / max(half_life_hours, 1e-6))
        w = source_weight(a.source) * decay
        total_w += w
        total_sw += w * res.score
    if total_count == 0:
        return FactorScore(
            score=50.0,
            details={"article_count": 0},
            reason="no articles",
            insufficient_data=True,
        )
    if total_w == 0:
        return FactorScore(
            score=50.0,
            details={
                "article_count": total_count,
                "sentiment_hits": 0,
                "avg_sentiment": 0.0,
            },
            reason="all neutral",
            insufficient_data=False,
        )
    avg = total_sw / total_w
    score = linear_map(avg, src_lo=-1.0, src_hi=1.0)
    reason = ""
    if score >= 80:
        reason = "다수의 호재 기사"
    elif score <= 20:
        reason = "다수의 악재 기사"
    return FactorScore(
        score=score,
        details={
            "article_count": total_count,
            "sentiment_hits": hit_count,
            "avg_sentiment": avg,
            "positive_hits": pos_hits,
            "negative_hits": neg_hits,
            "model_version": SENTIMENT_MODEL_VER,
            "half_life_hours": half_life_hours,
        },
        reason=reason,
        insufficient_data=False,
    )


def filter_look_ahead(
    articles: Iterable[NewsArticleInput],
    *,
    as_of: datetime,
    min_delay_seconds: int = 60,
) -> list[NewsArticleInput]:
    """D5: 백테스트 시 published_at + 60초 이후에만 신호 유효.

    Returns only articles visible at `as_of` with delay enforced.
    """
    cutoff = as_of - timedelta(seconds=min_delay_seconds)
    return [a for a in articles if a.published_at <= cutoff]
