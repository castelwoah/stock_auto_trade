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
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt)
    jobs["prepare_state"]()
    jobs["trading_tick"]()
    rt.prepare.assert_called_once()
    rt.tick.assert_called_once()


@pytest.mark.parametrize("job,method", [("prepare_state", "prepare"), ("trading_tick", "tick")])
def test_handler_exception_is_isolated_and_notified(patched, job, method):
    settings, rt = patched
    err = RuntimeError("boom")
    getattr(rt, method).side_effect = err
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt)
    jobs[job]()   # 예외가 밖으로 나가지 않아야 함
    rt.notify_error.assert_called_once_with(job, err)


def test_daily_report_closes_runtime_even_on_failure(patched, monkeypatch):
    settings, rt = patched
    monkeypatch.setattr(main_mod, "session_scope", MagicMock(side_effect=RuntimeError("db")))
    jobs = main_mod._make_job_handlers(settings, MagicMock(), MagicMock(), rt)
    jobs["daily_report"]()
    rt.close.assert_called_once()
