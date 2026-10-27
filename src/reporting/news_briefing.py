"""장전 뉴스 종목 브리핑 (08:40) — 호재/악재 · 상승/하락 예상 2~3종목을 Discord 로.

**정보용 — 감시 종목·점수·주문에 반영하지 않는다.** 적중률이 쌓이면 매매 반영을 별도로 검토.

흐름:
  ① 종목 선정   지난 24h · 종목 확신도 ≥ NEWS_MIN_CONFIDENCE · 기사 2건↑ · 보통주 · 공개매수 제외
                → 신호 강도(기사 수 × 룰 감성 크기) 상위 BRIEFING_CANDIDATES
  ② 판단        ClaudeJudge (실패·키 없음 → 룰 판단으로 대체, 표시)
  ③ 선택        확신도 순 BRIEFING_COUNT (호재·악재가 둘 다 있으면 각각 1개 이상)
  ④ 전송·보관   Discord Embed 1건 + <ARCHIVE_ROOT>/briefing/YYYY-MM-DD.json
  ⑤ 사후 평가   16:00 — 예상 방향 vs 당일 등락률 (중립 제외), 결과를 같은 JSON 에 기록

방어 윈도우 08:35~09:05: 절전에서 늦게 깨어나 엉뚱한 시각에 나가지 않도록 그 밖이면 생략.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.analysis.llm_judge import (
    ENGINE_LLM,
    ENGINE_RULE,
    ArticleBrief,
    ClaudeJudge,
    JudgeError,
    Judgment,
    SymbolInput,
    prompt_hash,
    rule_judgment,
)
from src.persistence.models import ArticleSymbol, NewsArticle, StockScore


logger = logging.getLogger(__name__)

WINDOW = (time(8, 35), time(9, 5))
LOOKBACK = timedelta(hours=24)
MIN_ARTICLES = 2
EXCLUDE_KEYWORDS = ("공개매수",)          # 가격이 공개매수가에 묶여 뉴스 판단이 무의미
DISCLAIMER = "뉴스 기반 참고용 · 자동매매 미반영 · 투자 권유 아님"
COLOR = {"호재": 0x2ECC71, "악재": 0xE74C3C, "혼재": 0x95A5A6}
EMOJI = {"호재": "🟢", "악재": "🔴", "혼재": "⚪"}


def in_briefing_window(now: datetime) -> bool:
    return WINDOW[0] <= now.time() <= WINDOW[1]


# ---------------------------------------------------------------------------
# ① 종목 선정
# ---------------------------------------------------------------------------

def select_candidates(
    session: Session,
    *,
    now: datetime,
    names: dict[str, str],
    min_confidence: float,
    limit: int,
    min_articles: int = MIN_ARTICLES,
) -> list[SymbolInput]:
    rows = session.execute(
        select(ArticleSymbol.symbol, NewsArticle.id, NewsArticle.source, NewsArticle.title,
               NewsArticle.summary, NewsArticle.published_at, NewsArticle.sentiment_score)
        .join(NewsArticle, NewsArticle.id == ArticleSymbol.article_id)
        .where(
            NewsArticle.published_at >= now - LOOKBACK,
            NewsArticle.published_at <= now,
            ArticleSymbol.confidence >= min_confidence,
        )
        .order_by(NewsArticle.published_at.desc())
    ).all()
    by_sym: dict[str, list[ArticleBrief]] = {}
    for sym, aid, source, title, summary, pub, senti in rows:
        if sym not in names:                    # 보통주만 (ETF·SPAC·우선주 제외)
            continue
        by_sym.setdefault(sym, []).append(ArticleBrief(
            id=aid, source=source, title=title or "", summary=summary or "",
            published_at=pub, sentiment=float(senti or 0.0)))
    scores = _latest_rule_scores(session, now.date(), set(by_sym))
    cands: list[tuple[float, SymbolInput]] = []
    for sym, arts in by_sym.items():
        if len(arts) < min_articles:
            continue
        if any(k in a.title for a in arts for k in EXCLUDE_KEYWORDS):
            continue
        inp = SymbolInput(symbol=sym, name=names[sym], articles=tuple(arts),
                          rule_score=scores.get(sym))
        strength = min(len(arts), 10) * (0.5 + abs(inp.mean_sentiment))
        cands.append((strength, inp))
    cands.sort(key=lambda x: (-x[0], x[1].symbol))
    return [c for _, c in cands[:limit]]


def _latest_rule_scores(session: Session, day: date, symbols: set[str]) -> dict[str, float]:
    if not symbols:
        return {}
    # (날짜, 종목, scoring_ver) UNIQUE — universe 재구성 때마다 같은 행이 갱신된다
    return {sym: float(score) for sym, score in session.execute(
        select(StockScore.symbol, StockScore.news_score)
        .where(StockScore.score_date == day, StockScore.symbol.in_(symbols))
    ).all()}


# ---------------------------------------------------------------------------
# ②·③ 판단 · 선택
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BriefingItem:
    judgment: Judgment
    name: str
    article_count: int
    rule_score: float | None
    prev_change_pct: float | None

    @property
    def rule_mismatch(self) -> bool:
        """LLM 판단과 룰 점수가 반대 — 룰 감성 사전 보강의 근거로 쌓는다."""
        if self.judgment.engine != ENGINE_LLM or self.rule_score is None:
            return False
        return ((self.judgment.impact == "호재" and self.rule_score < 40)
                or (self.judgment.impact == "악재" and self.rule_score >= 60))


@dataclass(frozen=True)
class Briefing:
    day: date
    generated_at: datetime
    items: tuple[BriefingItem, ...]
    engine: str                       # llm / rule
    model: str | None
    prompt_hash: str | None
    input_article_ids: dict[str, list[int]] = field(default_factory=dict)
    fallback_reason: str | None = None


def judge_inputs(
    inputs: list[SymbolInput], *, now: datetime, judge: ClaudeJudge | None,
) -> tuple[list[Judgment], str, str | None]:
    """(판단들, engine, 대체 사유). LLM 실패 시 전 종목 룰 판단."""
    if judge is None:
        return [rule_judgment(i) for i in inputs], ENGINE_RULE, "LLM 키 없음"
    try:
        got = {j.symbol: j for j in judge.judge(inputs, now=now)}
    except JudgeError as e:
        logger.warning("briefing LLM failed — rule fallback: %s", e)
        return [rule_judgment(i) for i in inputs], ENGINE_RULE, f"LLM 실패 ({e})"
    except Exception:
        logger.exception("briefing LLM error — rule fallback")
        return [rule_judgment(i) for i in inputs], ENGINE_RULE, "LLM 오류"
    # 모델이 빠뜨린 종목만 룰로 채운다
    return [got.get(i.symbol) or rule_judgment(i) for i in inputs], ENGINE_LLM, None


def pick(judgments: list[Judgment], count: int) -> list[Judgment]:
    """확신도 순 count 개. 호재·악재가 모두 있으면 각각 최상위 1개는 넣는다."""
    ranked = sorted(judgments, key=lambda j: (-j.confidence, j.symbol))
    chosen: list[Judgment] = []
    for impact in ("호재", "악재"):
        best = next((j for j in ranked if j.impact == impact), None)
        if best is not None and len(chosen) < count:
            chosen.append(best)
    for j in ranked:
        if len(chosen) >= count:
            break
        if j not in chosen:
            chosen.append(j)
    return sorted(chosen, key=lambda j: (-j.confidence, j.symbol))


def build_briefing(
    session: Session,
    *,
    now: datetime,
    names: dict[str, str],
    judge: ClaudeJudge | None,
    count: int,
    candidates: int,
    min_confidence: float,
    prev_change: Callable[[str], float | None] | None = None,
) -> Briefing:
    inputs = select_candidates(session, now=now, names=names,
                               min_confidence=min_confidence, limit=candidates)
    if prev_change is not None:
        inputs = [_with_change(i, prev_change) for i in inputs]
    judgments, engine, reason = judge_inputs(inputs, now=now, judge=judge) if inputs else ([], ENGINE_RULE, None)
    by_sym = {i.symbol: i for i in inputs}
    items = tuple(
        BriefingItem(judgment=j, name=by_sym[j.symbol].name,
                     article_count=len(by_sym[j.symbol].articles),
                     rule_score=by_sym[j.symbol].rule_score,
                     prev_change_pct=by_sym[j.symbol].prev_change_pct)
        for j in pick(judgments, count)
    )
    llm = engine == ENGINE_LLM and judge is not None
    return Briefing(
        day=now.date(), generated_at=now, items=items, engine=engine,
        model=judge.model if llm else None, prompt_hash=prompt_hash() if llm else None,
        input_article_ids={i.symbol: [a.id for a in i.articles] for i in inputs},
        fallback_reason=reason,
    )


def _with_change(inp: SymbolInput, prev_change: Callable[[str], float | None]) -> SymbolInput:
    try:
        chg = prev_change(inp.symbol)
    except Exception:
        logger.warning("briefing: prev change lookup failed for %s", inp.symbol)
        chg = None
    return SymbolInput(symbol=inp.symbol, name=inp.name, articles=inp.articles,
                       prev_change_pct=chg, rule_score=inp.rule_score)


# ---------------------------------------------------------------------------
# ④ 렌더 · 보관
# ---------------------------------------------------------------------------

def render_embed(b: Briefing, *, accuracy: str | None = None) -> dict[str, Any]:
    engine = b.model or "룰 기준"
    if b.fallback_reason:
        engine = f"룰 기준 ({b.fallback_reason})"
    fields = []
    for it in b.items:
        j = it.judgment
        title = (f"{EMOJI.get(j.impact, '⚪')} {it.name} ({j.symbol}) — {j.impact} · "
                 f"{j.direction}{' 예상' if j.direction != '중립' else ''} · 확신 {j.confidence:.2f} · {j.horizon}")
        lines = []
        if j.reasons:
            lines.append("근거: " + " / ".join(j.reasons))
        if j.risks:
            lines.append("반대: " + " / ".join(j.risks))
        if j.duplicate_events:
            lines.append("중복 사건: " + " / ".join(j.duplicate_events))
        meta = f"기사 {it.article_count}건"
        if it.prev_change_pct is not None:
            meta += f" · 전일 {it.prev_change_pct:+.2f}%"
        if it.rule_score is not None:
            meta += f" · 룰 점수 {it.rule_score:.0f}"
        if it.rule_mismatch:
            meta += " ↔ LLM 판단 불일치 ⚠"
        lines.append(meta)
        fields.append({"name": title[:256], "value": "\n".join(lines)[:1024], "inline": False})
    if not fields:
        fields.append({"name": "대상 없음",
                       "value": f"지난 24시간 기사 {MIN_ARTICLES}건 이상 종목이 없습니다.", "inline": False})
    impacts = [it.judgment.impact for it in b.items]
    color = COLOR["혼재"]
    if impacts.count("호재") > impacts.count("악재"):
        color = COLOR["호재"]
    elif impacts.count("악재") > impacts.count("호재"):
        color = COLOR["악재"]
    footer = f"엔진 {engine}"
    if accuracy:
        footer += f" · {accuracy}"
    return {
        "title": f"📰 장전 종목 브리핑 · {b.generated_at:%m-%d %H:%M}",
        "description": DISCLAIMER,
        "color": color,
        "fields": fields,
        "footer": {"text": footer[:2048]},
    }


def archive_path(root: Path, day: date) -> Path:
    return root / "briefing" / f"{day.isoformat()}.json"


def write_archive(root: Path, b: Briefing) -> Path:
    p = archive_path(root, b.day)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "day": b.day.isoformat(),
        "generated_at": b.generated_at.isoformat(),
        "engine": b.engine, "model": b.model, "prompt_hash": b.prompt_hash,
        "fallback_reason": b.fallback_reason,
        "input_article_ids": b.input_article_ids,
        "items": [
            {**it.judgment.to_dict(), "name": it.name, "article_count": it.article_count,
             "rule_score": it.rule_score, "prev_change_pct": it.prev_change_pct,
             "rule_mismatch": it.rule_mismatch}
            for it in b.items
        ],
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# ⑤ 사후 평가
# ---------------------------------------------------------------------------

def is_hit(direction: str, change_pct: float) -> bool | None:
    """상승 예상 & 등락 > 0, 하락 예상 & 등락 < 0. 중립은 평가 제외(None)."""
    if direction == "상승":
        return change_pct > 0
    if direction == "하락":
        return change_pct < 0
    return None


def evaluate_day(root: Path, day: date, change_of: Callable[[str], float | None]) -> tuple[int, int] | None:
    """당일 브리핑 적중 (적중, 평가 대상) — 결과를 아카이브 JSON 에 기록. 브리핑 없으면 None."""
    p = archive_path(root, day)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    hits = total = 0
    for item in data.get("items", []):
        try:
            chg = change_of(item["symbol"])
        except Exception:
            logger.warning("briefing eval: change lookup failed for %s", item.get("symbol"))
            chg = None
        item["actual_change_pct"] = chg
        hit = is_hit(item.get("direction", ""), chg) if chg is not None else None
        item["hit"] = hit
        if hit is not None:
            total += 1
            hits += int(hit)
    data["evaluation"] = {"hits": hits, "total": total}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return hits, total


def cumulative_accuracy(root: Path, *, until: date | None = None) -> tuple[int, int]:
    hits = total = 0
    d = root / "briefing"
    if not d.exists():
        return 0, 0
    for p in sorted(d.glob("*.json")):
        if until is not None and p.stem > until.isoformat():
            continue
        try:
            ev = json.loads(p.read_text(encoding="utf-8")).get("evaluation")
        except (OSError, ValueError):
            continue
        if ev:
            hits += int(ev.get("hits", 0))
            total += int(ev.get("total", 0))
    return hits, total


def accuracy_line(root: Path, *, today: tuple[int, int] | None = None, until: date | None = None) -> str | None:
    cum = cumulative_accuracy(root, until=until)
    parts = []
    if today is not None:
        parts.append(f"오늘 적중 {today[0]}/{today[1]}")
    if cum[1]:
        parts.append(f"누적 {cum[0]}/{cum[1]} ({cum[0] / cum[1] * 100:.0f}%)")
    return " · ".join(parts) or None
