"""news_briefing — 종목 선정 · 판단/대체 · 선택 · Embed · 아카이브 · 사후 적중."""
import json
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.analysis.llm_judge import ENGINE_LLM, ENGINE_RULE, JudgeError, Judgment
from src.persistence.models import ArticleSymbol, Base, NewsArticle, StockScore
from src.reporting import news_briefing as nb

NOW = datetime(2026, 10, 28, 8, 40)
NAMES = {"005930": "삼성전자", "000660": "SK하이닉스", "035720": "카카오", "114840": "아이패밀리에스씨"}


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


_ids = iter(range(1, 10_000))


def _art(session, symbol, title, *, senti=0.0, conf=1.0, pub=datetime(2026, 10, 28, 7, 0)):
    i = next(_ids)
    session.add(NewsArticle(id=i, source="yonhap_rss", external_id=str(i), url=f"u{i}", url_hash=f"h{i}",
                            title=title, summary="요약", sentiment_score=senti, published_at=pub))
    session.add(ArticleSymbol(article_id=i, symbol=symbol, confidence=conf))
    session.flush()


def _seed(session):
    for t, s in [("반도체주 급등", 0.0), ("주주환원 기대", 0.0), ("과징금 200억", -1.0), ("과징금 200억(종합)", -1.0)]:
        _art(session, "005930", t, senti=s)
    for t in ("카카오 대규모 수주", "카카오 공급계약 체결"):
        _art(session, "035720", t, senti=0.8)
    _art(session, "000660", "하이닉스 단독 기사", senti=1.0)                 # 1건 → 제외
    for t in ("공개매수 발표", "공개매수가 상향"):
        _art(session, "114840", t, senti=0.5)                               # 공개매수 → 제외
    _art(session, "999999", "ETF 기사", senti=1.0)
    _art(session, "999999", "ETF 기사2", senti=1.0)                          # 보통주 아님 → 제외
    _art(session, "005930", "오래된 기사", pub=datetime(2026, 10, 27, 8, 0))  # 24h 밖
    _art(session, "005930", "저확신 기사", conf=0.5)
    session.add(StockScore(score_date=NOW.date(), symbol="005930", news_score=0.0, total_score=0.0,
                           scoring_ver="news-universe-v1"))
    session.commit()


def test_select_candidates_filters_and_ranks(session):
    _seed(session)
    c = nb.select_candidates(session, now=NOW, names=NAMES, min_confidence=0.8, limit=6)
    assert [i.symbol for i in c] == ["005930", "035720"]          # 4건×(0.5+0.5)=4.0 > 2건×1.3=2.6
    sam = c[0]
    assert len(sam.articles) == 4 and sam.rule_score == 0.0
    assert c[1].rule_score is None


def test_select_candidates_limit(session):
    _seed(session)
    assert len(nb.select_candidates(session, now=NOW, names=NAMES, min_confidence=0.8, limit=1)) == 1


def _j(sym, impact, conf, direction=None, engine=ENGINE_LLM):
    direction = direction or {"호재": "상승", "악재": "하락", "혼재": "중립"}[impact]
    return Judgment(symbol=sym, impact=impact, direction=direction, horizon="당일",
                    confidence=conf, reasons=("r",), engine=engine)


def test_pick_keeps_both_sides_then_confidence():
    js = [_j("A", "호재", 0.9), _j("B", "호재", 0.8), _j("C", "호재", 0.7), _j("D", "악재", 0.4)]
    assert [j.symbol for j in nb.pick(js, 3)] == ["A", "B", "D"]
    assert [j.symbol for j in nb.pick(js, 1)] == ["A"]
    assert [j.symbol for j in nb.pick([_j("X", "혼재", 0.3)], 3)] == ["X"]


def test_build_briefing_llm(session):
    _seed(session)
    judge = MagicMock(model="claude-haiku-5-5")
    judge.judge.return_value = [_j("005930", "호재", 0.7), _j("035720", "호재", 0.6)]
    b = nb.build_briefing(session, now=NOW, names=NAMES, judge=judge, count=3, candidates=6,
                          min_confidence=0.8, prev_change=lambda s: {"005930": 2.0}.get(s))
    assert b.engine == ENGINE_LLM and b.model == "claude-haiku-5-5" and b.prompt_hash
    assert [it.judgment.symbol for it in b.items] == ["005930", "035720"]
    sam = b.items[0]
    assert sam.prev_change_pct == 2.0 and sam.article_count == 4
    assert sam.rule_mismatch is True                                # LLM 호재 ↔ 룰 0점
    assert sorted(b.input_article_ids) == ["005930", "035720"]


def test_build_briefing_llm_failure_falls_back_to_rule(session):
    _seed(session)
    judge = MagicMock(model="m")
    judge.judge.side_effect = JudgeError("HTTP 529")
    b = nb.build_briefing(session, now=NOW, names=NAMES, judge=judge, count=3, candidates=6, min_confidence=0.8)
    assert b.engine == ENGINE_RULE and b.model is None and "HTTP 529" in b.fallback_reason
    assert all(it.judgment.engine == ENGINE_RULE for it in b.items)
    assert all(it.rule_mismatch is False for it in b.items)


def test_build_briefing_unexpected_error_and_missing_symbol(session):
    _seed(session)
    judge = MagicMock(model="m")
    judge.judge.side_effect = ValueError("x")
    assert nb.build_briefing(session, now=NOW, names=NAMES, judge=judge, count=3, candidates=6,
                             min_confidence=0.8).fallback_reason == "LLM 오류"
    judge.judge.side_effect = None
    judge.judge.return_value = [_j("005930", "악재", 0.6)]          # 카카오 누락 → 룰로 채움
    b = nb.build_briefing(session, now=NOW, names=NAMES, judge=judge, count=3, candidates=6, min_confidence=0.8)
    engines = {it.judgment.symbol: it.judgment.engine for it in b.items}
    assert engines == {"005930": ENGINE_LLM, "035720": ENGINE_RULE}


def test_build_briefing_prev_change_failure_isolated(session):
    _seed(session)
    def boom(sym):
        raise RuntimeError("kis")
    b = nb.build_briefing(session, now=NOW, names=NAMES, judge=None, count=3, candidates=6,
                          min_confidence=0.8, prev_change=boom)
    assert b.items and all(it.prev_change_pct is None for it in b.items)
    assert b.fallback_reason == "LLM 키 없음"


def test_build_briefing_no_candidates(session):
    b = nb.build_briefing(session, now=NOW, names=NAMES, judge=None, count=3, candidates=6, min_confidence=0.8)
    assert b.items == ()
    emb = nb.render_embed(b)
    assert emb["fields"][0]["name"] == "대상 없음"


def test_render_embed(session):
    _seed(session)
    judge = MagicMock(model="claude-haiku-5-5")
    judge.judge.return_value = [
        Judgment(symbol="005930", impact="호재", direction="상승", horizon="당일", confidence=0.7,
                 reasons=("반도체 급등 [1]",), risks=("과징금 [3]",), duplicate_events=("과징금 2건",)),
        _j("035720", "악재", 0.65),
    ]
    b = nb.build_briefing(session, now=NOW, names=NAMES, judge=judge, count=3, candidates=6,
                          min_confidence=0.8, prev_change=lambda s: -1.25)
    e = nb.render_embed(b, accuracy="누적 5/8 (62%)")
    assert e["title"] == "📰 장전 종목 브리핑 · 10-28 08:40"
    assert nb.DISCLAIMER == e["description"]
    f0 = e["fields"][0]
    assert f0["name"] == "🟢 삼성전자 (005930) — 호재 · 상승 예상 · 확신 0.70 · 당일"
    assert "근거: 반도체 급등 [1]" in f0["value"] and "반대: 과징금 [3]" in f0["value"]
    assert "중복 사건: 과징금 2건" in f0["value"]
    assert "기사 4건 · 전일 -1.25% · 룰 점수 0 ↔ LLM 판단 불일치 ⚠" in f0["value"]
    assert e["fields"][1]["name"].startswith("🔴 카카오")
    assert e["color"] == nb.COLOR["혼재"]
    assert e["footer"]["text"] == "엔진 claude-haiku-5-5 · 누적 5/8 (62%)"


def test_render_embed_rule_footer_and_neutral():
    b = nb.Briefing(day=NOW.date(), generated_at=NOW, engine=ENGINE_RULE, model=None, prompt_hash=None,
                    fallback_reason="LLM 키 없음",
                    items=(nb.BriefingItem(_j("A", "혼재", 0.1, engine=ENGINE_RULE), "에이", 2, None, None),))
    e = nb.render_embed(b)
    assert e["footer"]["text"] == "엔진 룰 기준 (LLM 키 없음)"
    assert e["fields"][0]["name"] == "⚪ 에이 (A) — 혼재 · 중립 · 확신 0.10 · 당일"


def test_window():
    assert nb.in_briefing_window(NOW)
    assert not nb.in_briefing_window(datetime(2026, 10, 28, 10, 38))


def _briefing(items):
    return nb.Briefing(day=NOW.date(), generated_at=NOW, engine=ENGINE_LLM, model="m", prompt_hash="h",
                       items=tuple(nb.BriefingItem(j, j.symbol, 2, None, None) for j in items))


def test_archive_and_evaluate(tmp_path):
    b = _briefing([_j("A", "호재", 0.7), _j("B", "악재", 0.6), _j("C", "혼재", 0.3)])
    p = nb.write_archive(tmp_path, b)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["model"] == "m" and [i["symbol"] for i in data["items"]] == ["A", "B", "C"]
    res = nb.evaluate_day(tmp_path, NOW.date(), {"A": 2.0, "B": 1.0, "C": 0.5}.get)
    assert res == (1, 2)                                           # A 적중, B 빗나감, C 중립 제외
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["evaluation"] == {"hits": 1, "total": 2}
    assert [i["hit"] for i in data["items"]] == [True, False, None]


def test_evaluate_missing_and_lookup_failure(tmp_path):
    assert nb.evaluate_day(tmp_path, NOW.date(), lambda s: 1.0) is None
    nb.write_archive(tmp_path, _briefing([_j("A", "호재", 0.7)]))
    def boom(s):
        raise RuntimeError("kis")
    assert nb.evaluate_day(tmp_path, NOW.date(), boom) == (0, 0)


def test_cumulative_accuracy_and_line(tmp_path):
    assert nb.accuracy_line(tmp_path) is None
    for d, chg in [(date(2026, 10, 27), 1.0), (date(2026, 10, 28), -1.0)]:
        b = nb.Briefing(day=d, generated_at=datetime.combine(d, NOW.time()), engine=ENGINE_LLM,
                        model="m", prompt_hash="h", items=(nb.BriefingItem(_j("A", "호재", 0.7), "A", 2, None, None),))
        nb.write_archive(tmp_path, b)
        nb.evaluate_day(tmp_path, d, lambda s, c=chg: c)
    (tmp_path / "briefing" / "broken.json").write_text("{", encoding="utf-8")
    assert nb.cumulative_accuracy(tmp_path) == (1, 2)
    assert nb.cumulative_accuracy(tmp_path, until=date(2026, 10, 27)) == (1, 1)
    assert nb.accuracy_line(tmp_path, today=(0, 1)) == "오늘 적중 0/1 · 누적 1/2 (50%)"
