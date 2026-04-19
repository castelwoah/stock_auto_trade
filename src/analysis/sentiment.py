"""Rule-based sentiment analysis (1차) for Korean financial news.

rules §6 Step 2: 1차 룰(90%) → 2차 KoELECTRA(경계값) → 3차 LLM(옵션).
본 모듈은 1차 룰만 구현. 2/3차는 향후 추가.

Output: sentiment_score ∈ [-1.0, 1.0]
  +1.0 = 강한 호재, 0 = 중립, -1.0 = 강한 악재
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


SENTIMENT_MODEL_VER = "rule_v1"


# 사전은 의도적으로 적게 유지 — 정밀도 우선, 재현율은 2차 모델 도입 시 확장.
POSITIVE_KEYWORDS: dict[str, float] = {
    # 실적·성과
    "어닝서프라이즈": 2.0,
    "흑자전환": 2.0,
    "최대 실적": 1.5,
    "영업이익 증가": 1.0,
    "순이익 증가": 1.0,
    "매출 증가": 0.8,
    "컨센서스 상회": 1.5,
    "호실적": 1.2,
    # 이벤트
    "자사주 매입": 1.5,
    "자사주 소각": 1.8,
    "배당 확대": 1.2,
    "특별배당": 1.5,
    "액면분할": 0.5,
    "무상증자": 0.8,
    # M&A·파트너십
    "인수합병": 0.8,
    "수주": 1.0,
    "대규모 수주": 1.5,
    "공급계약 체결": 1.2,
    "파트너십": 0.6,
    "전략적 제휴": 0.8,
    # 승인·인허가
    "승인": 1.0,
    "특허 취득": 0.8,
    "임상 성공": 1.5,
    "품목허가": 1.2,
    # 주가
    "신고가": 1.0,
    "상한가": 1.2,
}

NEGATIVE_KEYWORDS: dict[str, float] = {
    # 실적·성과
    "어닝쇼크": 2.5,
    "적자전환": 2.0,
    "영업손실": 1.5,
    "순손실": 1.5,
    "매출 감소": 0.8,
    "컨센서스 하회": 1.5,
    "실적 부진": 1.0,
    # 거버넌스·리스크
    "감사의견 거절": 3.0,
    "감사의견 한정": 2.0,
    "관리종목 지정": 3.0,
    "거래정지": 3.0,
    "상장폐지": 4.0,
    "횡령": 2.5,
    "배임": 2.5,
    "분식회계": 3.0,
    # 법적·규제
    "소송": 0.8,
    "과징금": 1.2,
    "리콜": 1.5,
    "제재": 1.0,
    # 주가
    "하한가": 1.5,
    "급락": 1.0,
    # 자본
    "유상증자": 0.8,  # 일반적으로 주가 희석 신호
    "감자": 1.5,
}


# 부정 수식어가 앞에 붙으면 부호 반전
NEGATION_MARKERS: tuple[str, ...] = ("아니", "없", "못", "무산", "철회")


# 매체 신뢰도 — DART/공식 공시가 가장 높음, 포털 뉴스는 낮음
SOURCE_WEIGHTS: dict[str, float] = {
    "dart": 1.0,
    "kis_news": 0.9,
    "yonhap_rss": 0.8,
    "naver_disclosure": 0.85,
    # fallback (unknown): 0.5
}


@dataclass(frozen=True)
class SentimentResult:
    score: float  # -1.0 ~ +1.0
    positive_hits: int
    negative_hits: int
    model_version: str = SENTIMENT_MODEL_VER


def _count_hits(text: str, dictionary: dict[str, float]) -> tuple[int, float]:
    """Return (hit_count, weighted_sum).

    부정 수식어(NEGATION_MARKERS)가 키워드 ±15자 이내에 있으면 스킵.
    한국어는 "수주 철회"처럼 키워드 뒤에 부정어가 붙는 패턴이 흔해 양방향 검사.
    """
    hits = 0
    total = 0.0
    for kw, w in dictionary.items():
        for m in re.finditer(re.escape(kw), text):
            start = m.start()
            end = m.end()
            before = text[max(0, start - 15) : start]
            after = text[end : end + 15]
            if any(neg in before or neg in after for neg in NEGATION_MARKERS):
                continue
            hits += 1
            total += w
    return hits, total


def analyze(text: str) -> SentimentResult:
    """Analyze a single article text. Range: [-1, +1]."""
    if not text or not text.strip():
        return SentimentResult(score=0.0, positive_hits=0, negative_hits=0)
    pos_hits, pos_w = _count_hits(text, POSITIVE_KEYWORDS)
    neg_hits, neg_w = _count_hits(text, NEGATIVE_KEYWORDS)
    if pos_w == 0 and neg_w == 0:
        return SentimentResult(score=0.0, positive_hits=0, negative_hits=0)
    score = (pos_w - neg_w) / max(pos_w + neg_w, 1e-9)
    # clamp
    if score > 1.0:
        score = 1.0
    elif score < -1.0:
        score = -1.0
    return SentimentResult(score=score, positive_hits=pos_hits, negative_hits=neg_hits)


def source_weight(source_slug: str) -> float:
    return SOURCE_WEIGHTS.get(source_slug, 0.5)


def aggregate_sentiment(items: Iterable[tuple[str, str]]) -> float:
    """Aggregate sentiment across articles.

    items: iterable of (source_slug, article_text).
    Returns weighted average sentiment in [-1, +1].
    """
    total_w = 0.0
    total_sw = 0.0
    for src, text in items:
        res = analyze(text)
        if res.positive_hits == 0 and res.negative_hits == 0:
            continue  # 중립 기사는 평균에서 제외
        w = source_weight(src)
        total_w += w
        total_sw += w * res.score
    if total_w == 0:
        return 0.0
    return total_sw / total_w
