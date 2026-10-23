"""뉴스 수집 파이프라인: scraper → 중복 제거 → NewsArticle → 종목 매칭 → ArticleSymbol.

- 중복: (source, external_id) · 정규화 URL 해시 · SimHash(제목+요약, 최근 2일) 해밍 ≤ 3
- 저장: 제목 + 요약만 (본문 미수집 — D1 / rules §12). body_text 는 빈 문자열
- 매칭 결과는 confidence 와 함께 전부 저장한다. 매매 사용 여부는 소비측이
  confidence ≥ NEWS_MIN_CONFIDENCE (D4) 로 거른다.
- DART 공시: 응답의 종목코드(Article.symbols)를 confidence 1.0 으로 사용. 접수일만
  있고 시각이 없으므로 당일 접수분은 처음 본 시각을 published_at 으로 둔다 (D5 보수적).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.analysis.sentiment import analyze
from src.ingestion.base_scraper import Article, BaseScraper, collect_all
from src.ingestion.dedup import is_near_duplicate, from_signed64, simhash64, to_signed64, url_hash
from src.ingestion.symbol_resolver import SymbolResolver
from src.persistence.models import ArticleSymbol, NewsArticle


logger = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 1000
SIMHASH_WINDOW = timedelta(days=2)


@dataclass
class CollectResult:
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    symbol_links: int = 0
    by_source: dict[str, int] = field(default_factory=dict)


def collect_news(
    session: Session,
    scrapers: Iterable[BaseScraper],
    resolver: SymbolResolver,
    *,
    now: datetime,
    since: datetime | None = None,
) -> CollectResult:
    articles = collect_all(scrapers, since)
    res = CollectResult(fetched=len(articles))

    recent_hashes = [
        from_signed64(h) for h in session.execute(
            select(NewsArticle.simhash64).where(
                NewsArticle.retrieved_at >= now - SIMHASH_WINDOW,
                NewsArticle.simhash64.is_not(None),
            )
        ).scalars()
    ]
    for a in articles:
        if _exists(session, a):
            res.duplicates += 1
            continue
        text = f"{a.title}\n{a.body}".strip()
        sh = simhash64(text)
        if sh and any(is_near_duplicate(sh, h) for h in recent_hashes):
            res.duplicates += 1
            continue
        row = NewsArticle(
            source=a.source,
            external_id=a.external_id,
            url=a.url,
            url_hash=url_hash(a.url),
            title=a.title,
            body_text="",
            summary=(a.body or "")[:SUMMARY_MAX_CHARS] or None,
            sentiment_score=analyze(text).score,
            simhash64=to_signed64(sh) if sh else None,
            published_at=_published_at(a, now),
            retrieved_at=now,
            raw=a.raw,
        )
        session.add(row)
        session.flush()
        for sym, conf in _symbols_for(a, text, resolver).items():
            session.add(ArticleSymbol(article_id=row.id, symbol=sym, confidence=conf))
            res.symbol_links += 1
        if sh:
            recent_hashes.append(sh)
        res.inserted += 1
        res.by_source[a.source] = res.by_source.get(a.source, 0) + 1
    session.flush()
    return res


def _exists(session: Session, a: Article) -> bool:
    q = select(NewsArticle.id).where(
        (NewsArticle.source == a.source) & (NewsArticle.external_id == a.external_id)
        | (NewsArticle.url_hash == url_hash(a.url))
    ).limit(1)
    return session.execute(q).first() is not None


def _published_at(a: Article, now: datetime) -> datetime:
    if a.source == "dart" and a.published_at.date() == now.date():
        return now
    return a.published_at


def _symbols_for(a: Article, text: str, resolver: SymbolResolver) -> dict[str, float]:
    out: dict[str, float] = {s: 1.0 for s in a.symbols}
    if a.source == "dart":
        return out
    for m in resolver.resolve(text):
        out[m.symbol] = max(out.get(m.symbol, 0.0), m.confidence)
    return out
