"""main 엔트리포인트 — dry-run 배선 + 핸들러 예외 격리."""
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


def test_dry_run_registers_seven_jobs(patched, caplog):
    caplog.set_level("INFO")
    assert main_mod.main(["--dry-run"]) == 0
    assert "7 jobs registered" in caplog.text
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
