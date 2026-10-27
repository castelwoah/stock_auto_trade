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
import dataclasses
import logging
import signal
import sys
import threading
import time
from datetime import timedelta

from src.analysis.llm_judge import ClaudeJudge
from src.clients.discord import DiscordNotifier
from src.clients.kis import KisClient
from src.common.clock import now_kst, today_kst
from src.common.redact import RedactingFormatter, register_secrets
from src.config import get_settings
from src.ingestion.service import NewsService
from src.persistence.db import init_db, session_scope
from src.persistence.models import RiskEvent
from src.reporting.archiver import Archiver
from src.reporting.builders import build_daily_report, build_midday_snapshot
from src.reporting.daily_report import send_daily_report
from src.reporting.midday_report import in_valid_window, send_midday_report
from src.reporting import news_briefing as nb
from src.reporting.builders import RUNTIME_GAP_EVENT
from src.runtime import ReportState, TradingRuntime
from src.scheduler.app import TradingScheduler, register_default_jobs
from src.scheduler.heartbeat import Gap, GapMonitor


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    for h in logging.getLogger().handlers:
        h.setFormatter(RedactingFormatter(LOG_FORMAT))


def _register_secrets(settings) -> None:
    """로그·Discord 출력에서 값 그대로 가릴 비밀값 (계좌번호·키·웹훅)."""
    dart = settings.DART_API_KEY.get_secret_value() if settings.DART_API_KEY else None
    anthropic = settings.ANTHROPIC_API_KEY.get_secret_value() if settings.ANTHROPIC_API_KEY else None
    register_secrets([
        settings.CANO,
        f"{settings.CANO}-{settings.ACNT_PRDT_CD}",
        settings.APP_KEY.get_secret_value(),
        settings.APP_SECRET.get_secret_value(),
        settings.DISCORD_WEBHOOK_URL.get_secret_value(),
        dart,
        anthropic,
    ])


NO_RUNTIME_STATE = "런타임 상태 없음 — 시장·평가액·리스크는 기본값 (prepare 전 또는 재시작)"


def _midday_state_kwargs(st: ReportState | None) -> dict:
    if st is None:
        return {"next_actions": [NO_RUNTIME_STATE]}
    return dict(market=st.market, risk=st.risk, current_prices=st.prices,
                cash_available=st.cash, equity=st.portfolio.total_value,
                equity_virtual=st.portfolio.virtual)


def _daily_state_kwargs(st: ReportState | None) -> dict:
    if st is None:
        return {"next_actions": [NO_RUNTIME_STATE]}
    return dict(market=st.market, portfolio=st.portfolio, risk=st.risk)


def _record_gap(gap: Gap) -> None:
    with session_scope() as session:
        session.add(RiskEvent(ts=gap.end, event_type=RUNTIME_GAP_EVENT,
                              detail=gap.label(), action_taken="resumed"))


def _briefing_judge(settings) -> ClaudeJudge | None:
    if settings.ANTHROPIC_API_KEY is None:
        return None
    return ClaudeJudge(api_key=settings.ANTHROPIC_API_KEY.get_secret_value(),
                       model=settings.BRIEFING_MODEL)


def _make_job_handlers(settings, kis, notifier, runtime, news):
    """Job bodies.

    - prepare_state / trading_tick: TradingRuntime (실구현, Phase A)
    - midday_report / daily_report: DB 기반 리포트 (v3.1)
    - master_refresh / news_collect / analyze_score: NewsService (news-universe-v1)
      → 수집 후마다 runtime.refresh_universe 로 감시 종목 교체
    - heartbeat: 1분 — 실행 중단(절전·단절) 감지 → 알림 + RiskEvent(runtime_gap)
    - news_briefing: 08:40 장전 종목 브리핑 (BRIEFING_ENABLED, 정보용)
    모든 핸들러는 예외를 삼키고 알린다 — 한 job 실패가 스케줄러를 멈추지 않게.
    """
    gaps = GapMonitor(notify=notifier.send, record=_record_gap)

    def heartbeat():
        try:
            gaps.beat()
        except Exception:
            logging.exception("heartbeat failed")

    def change_pct(symbol: str) -> float | None:
        return kis.get_quote(symbol).change_pct

    def news_briefing():
        if not settings.BRIEFING_ENABLED:
            return
        now = now_kst()
        if not nb.in_briefing_window(now):
            logging.warning("[job] news_briefing out of window (%s), skipping", now.time())
            return
        logging.info("[job] news_briefing — 장전 종목 브리핑")
        try:
            with session_scope() as session:
                b = nb.build_briefing(
                    session, now=now, names=news.names(now.date()),
                    judge=_briefing_judge(settings), count=settings.BRIEFING_COUNT,
                    candidates=settings.BRIEFING_CANDIDATES,
                    min_confidence=settings.NEWS_MIN_CONFIDENCE, prev_change=change_pct,
                )
            acc = nb.accuracy_line(settings.ARCHIVE_ROOT, until=now.date() - timedelta(days=1))
            notifier.send_embed(nb.render_embed(b, accuracy=acc))
            nb.write_archive(settings.ARCHIVE_ROOT, b)
            logging.info("news briefing sent: %d items (engine=%s)", len(b.items), b.engine)
        except Exception:
            logging.exception("news_briefing failed")
            notifier.send(f"[main] news_briefing 실패: {now_kst().isoformat()}")

    def briefing_summary(today) -> str | None:
        try:
            res = nb.evaluate_day(settings.ARCHIVE_ROOT, today, change_pct)
        except Exception:
            logging.exception("briefing evaluation failed")
            return None
        if res is None:
            return None
        return nb.accuracy_line(settings.ARCHIVE_ROOT, today=res, until=today)

    def master_refresh():
        logging.info("[job] master_refresh — kospi/kosdaq mst (D9)")
        try:
            if news.refresh_master(today_kst()) is None:
                notifier.send("[master_refresh] 종목 마스터 없음 — 뉴스 매칭 중단 (watchlist 로 운용)")
        except Exception as exc:
            logging.exception("master_refresh failed")
            runtime.notify_error("master_refresh", exc)

    def news_collect():
        now = now_kst()
        try:
            news.collect(now)
        except Exception:
            logging.exception("news_collect failed")   # 다음 주기에 재시도 (알림 생략)
        if settings.UNIVERSE_MODE != "static":
            runtime.refresh_universe(now)

    def analyze_score():
        now = now_kst()
        logging.info("[job] analyze_score — build universe")
        u = runtime.refresh_universe(now)
        if u is not None:
            logging.info("universe ready: %s", u.summary())

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
            st = runtime.report_state()
            with session_scope() as session:
                snap = build_midday_snapshot(
                    session, ts=now, names=news.names(now.date()),
                    candidate_min_score=settings.NEWS_MIN_SCORE,
                    **_midday_state_kwargs(st),
                )
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
            st = runtime.report_state(refresh_index=True)
            with session_scope() as session:
                report = build_daily_report(
                    session, report_date=today, names=news.names(today),
                    news_min_score=settings.NEWS_MIN_SCORE,
                    **_daily_state_kwargs(st),
                )
                summary = briefing_summary(today)
                if summary:
                    report = dataclasses.replace(report, briefing_summary=summary)
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
        "heartbeat": heartbeat,
        "news_briefing": news_briefing,
    }


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="KIS auto-trading")
    parser.add_argument("--dry-run", action="store_true", help="load but do not schedule")
    parser.add_argument("--init-db", action="store_true", help="create tables and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    _register_secrets(settings)
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
    news = NewsService(settings)
    runtime = TradingRuntime(settings, kis, notifier, universe_builder=news.build_universe)
    jobs = _make_job_handlers(settings, kis, notifier, runtime, news)
    register_default_jobs(
        sched,
        master_refresh=jobs["master_refresh"],
        news_collect=jobs["news_collect"],
        analyze_score=jobs["analyze_score"],
        prepare_state=jobs["prepare_state"],
        trading_tick=jobs["trading_tick"],
        midday_report=jobs["midday_report"],
        daily_report=jobs["daily_report"],
        news_poll_minutes=settings.NEWS_POLL_MINUTES,
        heartbeat=jobs["heartbeat"],
        news_briefing=jobs["news_briefing"],
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
