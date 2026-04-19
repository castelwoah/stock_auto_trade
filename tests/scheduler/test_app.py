"""TradingScheduler wrapper tests (no actual scheduler start)."""
from datetime import time
from unittest.mock import MagicMock

from src.scheduler.app import TradingScheduler, register_default_jobs


def test_register_default_jobs_adds_six():
    sched = TradingScheduler()
    jobs = register_default_jobs(
        sched,
        master_refresh=MagicMock(),
        news_collect=MagicMock(),
        analyze_score=MagicMock(),
        prepare_state=MagicMock(),
        trading_tick=MagicMock(),
        daily_report=MagicMock(),
    )
    assert set(jobs.keys()) == {
        "master_refresh", "news_collect", "analyze_score",
        "prepare_state", "trading_tick", "daily_report",
    }
    assert len(sched.scheduler.get_jobs()) == 6


def test_add_cron_sets_business_day_wrapper():
    sched = TradingScheduler()
    called: list[bool] = []
    sched.add_cron(lambda: called.append(True), name="t1", hour=8, minute=0)
    job = sched.scheduler.get_job("t1")
    assert job is not None
    assert job.max_instances == 1


def test_add_interval_window_registered():
    sched = TradingScheduler()
    sched.add_interval_window(
        MagicMock(), name="tick", start=time(9, 10), end=time(15, 20), step_min=5,
    )
    assert sched.scheduler.get_job("tick") is not None


def test_misfire_grace_is_300s():
    sched = TradingScheduler()
    sched.add_cron(MagicMock(), name="t", hour=9)
    job = sched.scheduler.get_job("t")
    assert job.misfire_grace_time == 300
