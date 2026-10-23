"""APScheduler 오케스트레이터 — 영업일 타임라인 orchestration.

타임라인 (KST, 영업일만):
  07:30  master 갱신 (D9)
  08:00~14:50  뉴스 수집 (NEWS_POLL_MINUTES 간격) → 감시 종목 재구성
  08:50  감시 종목 구성 (장 시작 전 확정본)
  09:00  DailyLossState.start_equity 고정 (D7) + reconciler.verify()
  09:10~15:00  본 트레이딩 루프 (5분 간격 tick)
  15:00~15:20  청산 전용 (신규 매수 없음)
  16:00  일일 리포트 + 아카이브
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.scheduler.calendar import is_business_day
from src.common.clock import now_kst, today_kst


logger = logging.getLogger(__name__)


SchedulerJob = Callable[[], None]


class TradingScheduler:
    """Thin wrapper around APScheduler with KRX business-day awareness.

    각 job 은 내부에서 `is_business_day(today)` 를 확인하거나, 또는 상위가
    `skip_weekends=True` 스케줄 wrapper 를 사용하도록 한다.
    """

    # v2 H4: misfire_grace_time 를 300초로 확대 (API 타임아웃 내성)
    DEFAULT_MISFIRE_GRACE = 300

    def __init__(self, *, timezone: str = "Asia/Seoul") -> None:
        self.scheduler = BackgroundScheduler(timezone=timezone)

    # ------------------------------------------------------------------
    def add_cron(
        self,
        job: SchedulerJob,
        *,
        name: str,
        hour: int,
        minute: int = 0,
        business_days_only: bool = True,
    ) -> str:
        wrapped = self._wrap_business(job) if business_days_only else job
        trigger = CronTrigger(
            day_of_week="mon-fri" if business_days_only else "*",
            hour=hour, minute=minute,
        )
        j = self.scheduler.add_job(
            wrapped, trigger=trigger, id=name, name=name,
            max_instances=1,
            misfire_grace_time=self.DEFAULT_MISFIRE_GRACE,
            replace_existing=True,
        )
        return j.id

    def add_interval_window(
        self,
        job: SchedulerJob,
        *,
        name: str,
        start: time,
        end: time,
        step_min: int,
        business_days_only: bool = True,
    ) -> str:
        """start ~ end 시간대에만 실행되는 interval job."""
        def _gated():
            now = now_kst()
            if business_days_only and not is_business_day(now.date()):
                return
            t = now.time()
            if not (start <= t <= end):
                return
            job()
        j = self.scheduler.add_job(
            _gated, trigger=IntervalTrigger(minutes=step_min), id=name, name=name,
            max_instances=1,
            misfire_grace_time=self.DEFAULT_MISFIRE_GRACE,
            replace_existing=True,
        )
        return j.id

    # ------------------------------------------------------------------
    def start(self) -> None:
        self.scheduler.start()
        logger.info("scheduler started")

    def shutdown(self, *, wait: bool = True) -> None:
        self.scheduler.shutdown(wait=wait)
        logger.info("scheduler shutdown")

    # ------------------------------------------------------------------
    @staticmethod
    def _wrap_business(job: SchedulerJob) -> SchedulerJob:
        def _inner():
            today = today_kst()
            if not is_business_day(today):
                logger.debug("skipped non-business day: %s", today)
                return
            job()
        return _inner


# ---------------------------------------------------------------------------
# Standard daily schedule registration
# ---------------------------------------------------------------------------

def register_default_jobs(
    sched: TradingScheduler,
    *,
    master_refresh: SchedulerJob,
    news_collect: SchedulerJob,
    analyze_score: SchedulerJob,
    prepare_state: SchedulerJob,
    trading_tick: SchedulerJob,
    midday_report: SchedulerJob,     # v3 추가
    daily_report: SchedulerJob,
    news_poll_minutes: int = 10,
) -> dict[str, str]:
    """rules §5.1 타임라인 + v3 미드데이 리포트 타임라인에 맞춰 기본 작업 등록.

    타임라인 (영업일만):
      07:30 master / 08:00~14:50 news (간격 news_poll_minutes) / 08:50 universe / 09:00 state /
      09:10~15:20 tick / **12:00 midday** / 16:00 report
    """
    out: dict[str, str] = {}
    out["master_refresh"] = sched.add_cron(master_refresh, name="master_refresh", hour=7, minute=30)
    out["news_collect"] = sched.add_interval_window(
        news_collect, name="news_collect",
        start=time(8, 0), end=time(14, 50), step_min=news_poll_minutes,
    )
    out["analyze_score"] = sched.add_cron(analyze_score, name="analyze_score", hour=8, minute=50)
    out["prepare_state"] = sched.add_cron(prepare_state, name="prepare_state", hour=9, minute=0)
    out["trading_tick"] = sched.add_interval_window(
        trading_tick, name="trading_tick",
        start=time(9, 10), end=time(15, 20), step_min=5,
    )
    out["midday_report"] = sched.add_cron(midday_report, name="midday_report", hour=12, minute=0)
    out["daily_report"] = sched.add_cron(daily_report, name="daily_report", hour=16, minute=0)
    return out
