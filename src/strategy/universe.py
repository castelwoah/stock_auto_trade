"""감시 종목(universe) 구성 — 뉴스 후보 + 보유 종목 + 예비 목록(하드코딩 watchlist).

규칙 (news-universe-v1, 2026-10-23 확정):
  1. 기사 범위: 직전 영업일 15:30 이후 ~ now − 60초 (D5)
  2. 종목 매칭: ArticleSymbol.confidence ≥ min_confidence (D4), 매매 대상 보통주만
  3. 뉴스 점수: compute_news_score (감성 × 매체 가중 × 반감기 half_life_hours)
  4. 후보: 점수 ≥ min_score · 기사 1건+ / 악재: 감성 기사가 있고 점수 < block_score → 당일 매수 금지
  5. 구성: 보유 종목(상한 미포함) + 후보 상위 max_size.
     news_with_fallback 에서 후보 < min_news 이면 예비 목록으로 max_size 까지 채움
  6. 종목별 점수는 StockScore(scoring_ver=news-universe-v1)에 기록 (D12) —
     total_score = 뉴스 점수, 재무·기술·수급은 미산출이라 중립 50

파이프라인 역할 분리: 뉴스 점수는 감시 대상과 매수 우선순위만 정하고, 매수 여부는
변동성 돌파 조건 + RiskEngine 이 결정한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.analysis.news import NewsArticleInput, compute_news_score
from src.analysis.sentiment import SENTIMENT_MODEL_VER
from src.persistence.models import ArticleSymbol, NewsArticle, Position, StockScore
from src.scheduler.calendar import prev_business_day


logger = logging.getLogger(__name__)

SCORING_VER = "news-universe-v1"
NEUTRAL_SCORE = 50.0
LOOKBACK_FROM = time(15, 30)
LOOK_AHEAD_DELAY = timedelta(seconds=60)

MODE_STATIC = "static"
MODE_NEWS = "news"
MODE_NEWS_WITH_FALLBACK = "news_with_fallback"
MODES = (MODE_STATIC, MODE_NEWS, MODE_NEWS_WITH_FALLBACK)

SOURCE_NEWS = "news"
SOURCE_STATIC = "static"
SOURCE_HELD = "held"


@dataclass(frozen=True)
class UniverseParams:
    mode: str = MODE_NEWS_WITH_FALLBACK
    max_size: int = 30
    min_news: int = 10
    min_score: float = 60.0
    block_score: float = 40.0
    min_confidence: float = 0.8
    half_life_hours: float = 12.0


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    name: str
    source: str                       # news / static / held
    news_score: float | None = None
    n_articles: int = 0
    article_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Universe:
    built_at: datetime
    mode: str
    entries: tuple[UniverseEntry, ...]
    blocked: frozenset[str] = frozenset()      # 악재 — 당일 신규 매수 금지

    @property
    def symbols(self) -> list[str]:
        return [e.symbol for e in self.entries]

    @property
    def news_scores(self) -> dict[str, float]:
        return {e.symbol: e.news_score for e in self.entries if e.news_score is not None}

    def count(self, source: str) -> int:
        return sum(1 for e in self.entries if e.source == source)

    def summary(self) -> str:
        return (f"뉴스 {self.count(SOURCE_NEWS)} · 예비 {self.count(SOURCE_STATIC)} · "
                f"보유 {self.count(SOURCE_HELD)} · 악재 차단 {len(self.blocked)}")


def lookback_start(now: datetime) -> datetime:
    """직전 영업일 15:30 (장 마감 이후 뉴스부터 당일 재료로 본다)."""
    return datetime.combine(prev_business_day(now.date()), LOOKBACK_FROM)


def static_universe(now: datetime, static: Iterable[tuple[str, str]]) -> Universe:
    return Universe(
        built_at=now, mode=MODE_STATIC,
        entries=tuple(UniverseEntry(symbol=s, name=n, source=SOURCE_STATIC) for s, n in static),
    )


@dataclass(frozen=True)
class _Scored:
    symbol: str
    score: float
    insufficient: bool
    sentiment_hits: int
    article_ids: tuple[int, ...]


def score_symbols(
    session: Session,
    *,
    now: datetime,
    since: datetime,
    min_confidence: float,
    half_life_hours: float,
    tradable: set[str] | None = None,
) -> list[_Scored]:
    rows = session.execute(
        select(ArticleSymbol.symbol, NewsArticle.id, NewsArticle.source, NewsArticle.title,
               NewsArticle.summary, NewsArticle.published_at)
        .join(NewsArticle, NewsArticle.id == ArticleSymbol.article_id)
        .where(
            NewsArticle.published_at >= since,
            NewsArticle.published_at <= now - LOOK_AHEAD_DELAY,
            ArticleSymbol.confidence >= min_confidence,
        )
    ).all()
    by_sym: dict[str, list] = {}
    for sym, aid, source, title, summary, pub in rows:
        if tradable is not None and sym not in tradable:
            continue
        by_sym.setdefault(sym, []).append((aid, NewsArticleInput(
            source=source, title=title or "", body=summary or "", published_at=pub)))
    out: list[_Scored] = []
    for sym, items in by_sym.items():
        fs = compute_news_score([a for _, a in items], now=now, half_life_hours=half_life_hours)
        out.append(_Scored(
            symbol=sym, score=fs.score, insufficient=fs.insufficient_data,
            sentiment_hits=int(fs.details.get("sentiment_hits", 0)),
            article_ids=tuple(sorted(aid for aid, _ in items)),
        ))
    return out


def build_universe(
    session: Session,
    *,
    now: datetime,
    params: UniverseParams,
    static: Iterable[tuple[str, str]],
    names: dict[str, str] | None = None,
    held: Iterable[str] | None = None,
) -> Universe:
    """names: 매매 대상 보통주 code → name (마스터). None 이면 종목 제한 없음."""
    static = list(static)
    if params.mode == MODE_STATIC:
        return static_universe(now, static)

    names = names or {}
    tradable = set(names) if names else None
    scored = score_symbols(
        session, now=now, since=lookback_start(now),
        min_confidence=params.min_confidence, half_life_hours=params.half_life_hours,
        tradable=tradable,
    )
    _write_scores(session, now.date(), scored)

    blocked = frozenset(
        s.symbol for s in scored
        if not s.insufficient and s.sentiment_hits > 0 and s.score < params.block_score
    )
    candidates = sorted(
        (s for s in scored if s.score >= params.min_score),
        key=lambda s: (-s.score, -len(s.article_ids), s.symbol),
    )[: params.max_size]

    if held is None:
        held = [p.symbol for p in session.execute(
            select(Position).where(Position.qty > 0)).scalars()]
    static_names = dict(static)
    entries: list[UniverseEntry] = []
    seen: set[str] = set()
    for sym in sorted(set(held)):
        entries.append(UniverseEntry(symbol=sym, name=names.get(sym) or static_names.get(sym, ""),
                                     source=SOURCE_HELD))
        seen.add(sym)
    n_market = 0
    for c in candidates:
        n_market += 1
        if c.symbol in seen:
            continue
        entries.append(UniverseEntry(
            symbol=c.symbol, name=names.get(c.symbol) or static_names.get(c.symbol, ""),
            source=SOURCE_NEWS, news_score=c.score, n_articles=len(c.article_ids),
            article_ids=c.article_ids,
        ))
        seen.add(c.symbol)
    if params.mode == MODE_NEWS_WITH_FALLBACK and len(candidates) < params.min_news:
        for sym, name in static:
            if n_market >= params.max_size:
                break
            if sym in seen or sym in blocked:
                continue
            if tradable is not None and sym not in tradable:
                continue        # 상장폐지·거래 대상 아님 (마스터에 없음)
            entries.append(UniverseEntry(symbol=sym, name=name, source=SOURCE_STATIC))
            seen.add(sym)
            n_market += 1
    u = Universe(built_at=now, mode=params.mode, entries=tuple(entries), blocked=blocked)
    logger.info("universe %s: %s (scored %d)", now.strftime("%H:%M"), u.summary(), len(scored))
    return u


def _write_scores(session: Session, day: date, scored: list[_Scored]) -> None:
    """당일 news-universe-v1 점수를 최신 계산값으로 교체 (UNIQUE score_date·symbol·ver)."""
    session.execute(delete(StockScore).where(
        StockScore.score_date == day, StockScore.scoring_ver == SCORING_VER))
    for s in scored:
        session.add(StockScore(
            score_date=day, symbol=s.symbol, news_score=s.score,
            financial_score=NEUTRAL_SCORE, technical_score=NEUTRAL_SCORE, flow_score=NEUTRAL_SCORE,
            total_score=s.score, scoring_ver=SCORING_VER, sentiment_model_ver=SENTIMENT_MODEL_VER,
            llm_prompt_hash=None,
        ))
    session.flush()
