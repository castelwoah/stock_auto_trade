"""main 엔트리포인트 — dry-run 배선 + 핸들러 예외 격리."""
import json
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import src.main as main_mod

from tests.clients.conftest import make_settings


@pytest.fixture
def patched(monkeypatch):
    settings = make_settings()
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "KisClient", MagicMock())
    monkeypatch.setattr(main_mod, "DiscordNotifier", MagicMock())
    rt = MagicMock()
    monkeypatch.setattr(main_mod, "TradingRuntime", MagicMock(return_value=rt))
    return settings, rt


def test_dry_run_registers_nine_jobs(patched, caplog):
    """기본 7 + heartbeat + news_briefing (10-27)."""
    caplog.set_level("INFO")
    assert main_mod.main(["--dry-run"]) == 0
    assert "9 jobs registered" in caplog.text
    assert "SHADOW=True" in caplog.text


def test_prepare_and_tick_delegate_to_runtime(patched):
    settings, rt = patched
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt, MagicMock())
    jobs["prepare_state"]()
    jobs["trading_tick"]()
    rt.prepare.assert_called_once()
    rt.tick.assert_called_once()


@pytest.mark.parametrize("job,method", [("prepare_state", "prepare"), ("trading_tick", "tick")])
def test_handler_exception_is_isolated_and_notified(patched, job, method):
    settings, rt = patched
    err = RuntimeError("boom")
    getattr(rt, method).side_effect = err
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt, MagicMock())
    jobs[job]()   # 예외가 밖으로 나가지 않아야 함
    rt.notify_error.assert_called_once_with(job, err)


def test_daily_report_closes_runtime_even_on_failure(patched, monkeypatch):
    settings, rt = patched
    monkeypatch.setattr(main_mod, "session_scope", MagicMock(side_effect=RuntimeError("db")))
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt, MagicMock())
    jobs["daily_report"]()
    rt.close.assert_called_once()


def test_news_collect_refreshes_universe_even_if_collect_fails(patched):
    settings, rt = patched
    news = MagicMock()
    news.collect.side_effect = RuntimeError("rss down")
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt, news)
    jobs["news_collect"]()
    rt.refresh_universe.assert_called_once()


def test_news_collect_static_mode_skips_universe(monkeypatch):
    settings = make_settings(UNIVERSE_MODE="static")
    rt, news = MagicMock(), MagicMock()
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt, news)
    jobs["news_collect"]()
    news.collect.assert_called_once()
    rt.refresh_universe.assert_not_called()


def test_master_refresh_failure_is_isolated(patched):
    settings, rt = patched
    news = MagicMock()
    news.refresh_master.side_effect = RuntimeError("dl")
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt, news)
    jobs["master_refresh"]()
    rt.notify_error.assert_called_once()


def test_report_state_kwargs():
    from src.reporting.models import MarketSummary, PortfolioState, RiskStatus
    from src.runtime import ReportState

    assert main_mod._midday_state_kwargs(None) == {"next_actions": [main_mod.NO_RUNTIME_STATE]}
    assert main_mod._daily_state_kwargs(None) == {"next_actions": [main_mod.NO_RUNTIME_STATE]}
    st = ReportState(market=MarketSummary(kospi=1.0), portfolio=PortfolioState(total_value=5.0, virtual=True),
                     risk=RiskStatus(), cash=3.0, prices={"A": 1.0})
    mid = main_mod._midday_state_kwargs(st)
    assert mid["equity"] == 5.0 and mid["equity_virtual"] is True and mid["cash_available"] == 3.0
    assert mid["current_prices"] == {"A": 1.0} and mid["market"] is st.market
    assert main_mod._daily_state_kwargs(st) == {"market": st.market, "portfolio": st.portfolio,
                                                "risk": st.risk}


def test_daily_report_uses_runtime_state(patched, monkeypatch):
    settings, rt = patched
    rt.report_state.return_value = None
    built = {}

    def fake_build(session, **kw):
        built.update(kw)
        raise RuntimeError("stop")
    monkeypatch.setattr(main_mod, "build_daily_report", fake_build)
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt, MagicMock())
    jobs["daily_report"]()
    rt.report_state.assert_called_once_with(refresh_index=True)
    assert built["next_actions"] == [main_mod.NO_RUNTIME_STATE]


# ---------------------------------------------------------------------------
# heartbeat · news_briefing (10-27)
# ---------------------------------------------------------------------------

def test_heartbeat_exception_is_isolated(patched, monkeypatch):
    settings, rt = patched
    monkeypatch.setattr(main_mod.GapMonitor, "beat", MagicMock(side_effect=RuntimeError("x")))
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt, MagicMock())
    jobs["heartbeat"]()


def test_record_gap_writes_risk_event(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from src.persistence.models import Base, RiskEvent
    from src.scheduler.heartbeat import Gap

    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)

    @contextmanager
    def scope():
        with Session(eng) as s:
            yield s
            s.commit()
    monkeypatch.setattr(main_mod, "session_scope", scope)
    main_mod._record_gap(Gap(datetime(2026, 10, 27, 7, 15), datetime(2026, 10, 27, 8, 32)))
    with Session(eng) as s:
        ev = s.query(RiskEvent).one()
    assert ev.event_type == "runtime_gap" and ev.detail == "07:15~08:32 (1h17m)"


@pytest.fixture
def briefing_env(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from src.persistence.models import ArticleSymbol, Base, NewsArticle

    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        for i, (title, senti) in enumerate([("카카오 대규모 수주", 0.8), ("카카오 공급계약 체결", 0.6)], 1):
            s.add(NewsArticle(id=i, source="yonhap_rss", external_id=str(i), url=f"u{i}", url_hash=f"h{i}",
                              title=title, sentiment_score=senti, published_at=datetime(2026, 10, 28, 7, i)))
            s.add(ArticleSymbol(article_id=i, symbol="035720", confidence=1.0))
        s.commit()

    @contextmanager
    def scope():
        with Session(eng) as s:
            yield s
            s.commit()
    monkeypatch.setattr(main_mod, "session_scope", scope)
    monkeypatch.setattr(main_mod, "now_kst", lambda: datetime(2026, 10, 28, 8, 40))
    news = MagicMock()
    news.names.return_value = {"035720": "카카오"}
    kis = MagicMock()
    kis.get_quote.return_value = MagicMock(change_pct=1.5)
    return tmp_path, news, kis


def test_news_briefing_disabled_by_default(patched, briefing_env):
    settings, rt = patched
    _, news, kis = briefing_env
    notifier = MagicMock()
    main_mod._make_job_handlers(settings, kis, notifier, rt, news)["news_briefing"]()
    notifier.send_embed.assert_not_called()


def test_news_briefing_rule_fallback_sends_and_archives(briefing_env, monkeypatch):
    root, news, kis = briefing_env
    settings = make_settings(BRIEFING_ENABLED=True, ARCHIVE_ROOT=root)
    notifier = MagicMock()
    main_mod._make_job_handlers(settings, kis, notifier, MagicMock(), news)["news_briefing"]()
    embed = notifier.send_embed.call_args.args[0]
    assert "장전 종목 브리핑" in embed["title"]
    assert "카카오 (035720) — 호재" in embed["fields"][0]["name"]
    assert "룰 기준 (LLM 키 없음)" in embed["footer"]["text"]
    data = json.loads((root / "briefing" / "2026-10-28.json").read_text(encoding="utf-8"))
    assert data["engine"] == "rule" and data["items"][0]["prev_change_pct"] == 1.5


def test_news_briefing_out_of_window_skips(briefing_env, monkeypatch):
    root, news, kis = briefing_env
    monkeypatch.setattr(main_mod, "now_kst", lambda: datetime(2026, 10, 28, 10, 40))
    settings = make_settings(BRIEFING_ENABLED=True, ARCHIVE_ROOT=root)
    notifier = MagicMock()
    main_mod._make_job_handlers(settings, kis, notifier, MagicMock(), news)["news_briefing"]()
    notifier.send_embed.assert_not_called()


def test_daily_report_includes_briefing_accuracy(briefing_env, monkeypatch):
    root, news, kis = briefing_env
    settings = make_settings(BRIEFING_ENABLED=True, ARCHIVE_ROOT=root)
    notifier = MagicMock()
    jobs = main_mod._make_job_handlers(settings, kis, notifier, MagicMock(), news)
    jobs["news_briefing"]()
    sent = {}
    monkeypatch.setattr(main_mod, "today_kst", lambda: datetime(2026, 10, 28).date())
    monkeypatch.setattr(main_mod, "send_daily_report", lambda n, r: sent.update(r=r))
    jobs["daily_report"]()
    assert sent["r"].briefing_summary == "오늘 적중 1/1 · 누적 1/1 (100%)"
