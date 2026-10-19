"""Entry point for live/paper trading.

Wires up all layers:
  config → KIS client / Discord → persistence → strategy registry →
  risk engine → execution pipeline → scheduler.

Startup sequence:
  1. Validate config (pydantic Settings — KIS_ENV ↔ URL_BASE 불일치 시 abort)
  2. init_db() (dev, --init-db)
  3. TradingRuntime 생성 (risk engine 은 tick 마다 해당 스레드에 설치)
  4. Register scheduler jobs
  5. Install SIGINT/SIGTERM handlers (graceful shutdown)
  6. Block on scheduler until signal
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from src.clients.discord import DiscordNotifier
from src.clients.kis import KisClient
from src.common.clock import now_kst, today_kst
from src.config import get_settings
from src.persistence.db import init_db, session_scope
from src.reporting.archiver import Archiver
from src.reporting.builders import build_daily_report, build_midday_snapshot
from src.reporting.daily_report import send_daily_report
from src.reporting.midday_report import in_valid_window, send_midday_report
from src.runtime import TradingRuntime
from src.scheduler.app import TradingScheduler, register_default_jobs


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _make_job_handlers(settings, kis, notifier, runtime):
    """Job bodies.

    - prepare_state / trading_tick: TradingRuntime (실구현, Phase A)
    - midday_report / daily_report: DB 기반 리포트 (v3.1)
    - master_refresh / news_collect / analyze_score: stub (Phase C)
    모든 핸들러는 예외를 삼키고 알린다 — 한 job 실패가 스케줄러를 멈추지 않게.
    """
    def master_refresh():
        logging.info("[job] master_refresh (stub) — re-download kospi/kosdaq mst")

    def news_collect():
        logging.info("[job] news_collect (stub) — ingestion scrapers")

    def analyze_score():
        logging.info("[job] analyze_score (stub) — compute factor scores")

    def prepare_state():
        logging.info("[job] prepare_state — reconcile KIS↔DB, start equity, daily data")
        try:
            runtime.prepare()
        except Exception as exc:
            logging.exception("prepare_state failed")
            runtime.notify_error("prepare_state", exc)

    def trading_tick():
        try:
            runtime.tick()
        except Exception as exc:
            logging.exception("trading_tick failed")
            runtime.notify_error("trading_tick", exc)

    def midday_report():
        now = now_kst()
        if not in_valid_window(now):
            logging.warning("[job] midday_report out of window (%s), skipping", now.time())
            return
        logging.info("[job] midday_report — 장중 스냅샷 생성 중")
        try:
            with session_scope() as session:
                snap = build_midday_snapshot(session, ts=now)
                send_midday_report(notifier, snap)
            logging.info("midday snapshot sent (%d positions, %d candidates, %d anomalies)",
                         len(snap.positions), len(snap.top_candidates), len(snap.anomalies))
        except Exception:
            logging.exception("midday_report failed")
            notifier.send(f"[main] midday_report 실패: {now_kst().isoformat()}")

    def daily_report():
        today = today_kst()
        logging.info("[job] daily_report for %s", today)
        try:
            archiver = Archiver(settings.ARCHIVE_ROOT)
            with session_scope() as session:
                report = build_daily_report(session, report_date=today)
                send_daily_report(notifier, report)
                archiver.write_daily_report(report)
                archiver.write_trades_csv(report)
                archiver.append_tax_ledger(report)
            logging.info(
                "daily report: buys=%d tp=%d sl=%d rej=%d cxl=%d risk=%d",
                len(report.trades.buys), len(report.trades.take_profits),
                len(report.trades.stop_losses), report.trades.rejection_count,
                report.trades.cancellation_count, report.trades.risk_block_count,
            )
        except Exception:
            logging.exception("daily_report failed")
            notifier.send(f"[main] daily_report 실패: {now_kst().isoformat()}")
        finally:
            runtime.close()   # 당일 세션·상태 정리 (다음 영업일 09:00 에 새로 준비)

    return {
        "master_refresh": master_refresh,
        "news_collect": news_collect,
        "analyze_score": analyze_score,
        "prepare_state": prepare_state,
        "trading_tick": trading_tick,
        "midday_report": midday_report,
        "daily_report": daily_report,
    }


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="KIS auto-trading")
    parser.add_argument("--dry-run", action="store_true", help="load but do not schedule")
    parser.add_argument("--init-db", action="store_true", help="create tables and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.info(
        "KIS_ENV=%s URL=%s SHADOW=%s MAX_DEPLOY_PCT=%s KILL=%s",
        settings.KIS_ENV.value, settings.URL_BASE, settings.SHADOW_MODE,
        settings.MAX_DEPLOY_PCT, settings.kill_switch_on(),
    )

    if args.init_db:
        init_db()
        logging.info("DB initialized at %s", settings.DATABASE_URL)
        return 0

    # Clients
    kis = KisClient(settings)
    notifier = DiscordNotifier(settings.DISCORD_WEBHOOK_URL.get_secret_value())

    # Scheduler
    sched = TradingScheduler(timezone="Asia/Seoul")
    runtime = TradingRuntime(settings, kis, notifier)
    jobs = _make_job_handlers(settings, kis, notifier, runtime)
    register_default_jobs(
        sched,
        master_refresh=jobs["master_refresh"],
        news_collect=jobs["news_collect"],
        analyze_score=jobs["analyze_score"],
        prepare_state=jobs["prepare_state"],
        trading_tick=jobs["trading_tick"],
        midday_report=jobs["midday_report"],
        daily_report=jobs["daily_report"],
    )

    if args.dry_run:
        logging.info("dry-run complete — %d jobs registered", len(sched.scheduler.get_jobs()))
        return 0

    # Graceful shutdown signals
    stop_event = threading.Event()

    def _signal_handler(sig, frame):
        logging.warning("signal %s received, initiating shutdown", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    sched.start()
    mode = "SHADOW" if settings.SHADOW_MODE else "LIVE 실주문"
    notifier.send(f"[main] trading scheduler started — {settings.KIS_ENV.value} / {mode}")
    try:
        while not stop_event.is_set():
            time.sleep(1.0)
    finally:
        logging.info("shutting down scheduler...")
        sched.shutdown(wait=True)
        notifier.send("[main] trading scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
