"""Entry point for live/paper trading.

Wires up all layers:
  config → KIS client / Discord → persistence → strategy registry →
  risk engine → execution pipeline → scheduler.

Startup sequence:
  1. Validate config (pydantic Settings)
  2. init_db() (dev)
  3. Install risk engine (ContextVar)
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
from datetime import datetime

from datetime import date

from src.clients.discord import DiscordNotifier
from src.clients.kis import KisClient
from src.config import get_settings
from src.persistence.db import init_db, session_scope
from src.reporting.archiver import Archiver
from src.reporting.builders import build_daily_report, build_midday_snapshot
from src.reporting.daily_report import send_daily_report
from src.reporting.midday_report import in_valid_window, send_midday_report
from src.scheduler.app import TradingScheduler, register_default_jobs
from src.common.clock import now_kst, today_kst


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _make_job_handlers(settings, kis, notifier):
    """Job bodies — 실제 구현은 placeholder (stub).

    전체 실구현은 Phase 3 실주행 단계에서 점진 채움. 현재는:
      - 스케줄러가 정시에 호출되는지 확인 가능 (로그)
      - midday_report 는 방어 윈도우 체크 후 빈 스냅샷으로 Discord 전송 시도
      - 실 DB/KIS 데이터 수집은 Phase 3 에서 보강
    """
    def master_refresh():
        logging.info("[job] master_refresh (stub) — re-download kospi/kosdaq mst")

    def news_collect():
        logging.info("[job] news_collect (stub) — ingestion scrapers")

    def analyze_score():
        logging.info("[job] analyze_score (stub) — compute factor scores")

    def prepare_state():
        logging.info("[job] prepare_state — reconcile KIS↔DB at 09:00")
        # 실제 Reconciler.verify_with_grace() 호출은 pipeline 주입 후 활성

    def trading_tick():
        logging.info("[job] trading_tick at %s", now_kst().time())

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
    logging.info("KIS_ENV=%s URL=%s", settings.KIS_ENV.value, settings.URL_BASE)

    if args.init_db:
        init_db()
        logging.info("DB initialized at %s", settings.DATABASE_URL)
        return 0

    # Clients
    kis = KisClient(settings)
    notifier = DiscordNotifier(settings.DISCORD_WEBHOOK_URL.get_secret_value())

    # Scheduler
    sched = TradingScheduler(timezone="Asia/Seoul")
    jobs = _make_job_handlers(settings, kis, notifier)
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
    notifier.send("[main] trading scheduler started")
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
