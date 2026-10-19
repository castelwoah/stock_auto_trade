"""Trading runtime — prepare_state(09:00) / trading_tick(09:10~15:20) 핸들러 본체 (A13).

하루 단위 상태를 소유한다:
  - DB 세션 1개 (tick 마다 commit, 일과 종료 시 close)
  - RiskEngine (start_equity = 09:00 평가액 고정 — D7, 투입 상한 = start × MAX_DEPLOY_PCT)
  - StrategyRegistry (당일 실행 signal_id 를 DB 에서 복원 — 재시작 중복 차단)
  - OrderManager (당일 미종결 주문을 DB 에서 복원)
  - 시장 데이터: 전일 OHLC·20일 평균 거래대금(09:00), 시세·종목 상태(매 tick)

데이터 누락은 fail-closed: 시세가 없는 종목은 신호 대상에서 빠지고, 유동성
데이터가 없는 종목은 RiskEngine 이 매수를 차단한다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from src.clients.discord import DiscordNotifier
from src.clients.kis import KisClient, Quote
from src.common.clock import now_kst
from src.config import Settings
from src.execution.order_manager import OrderManager
from src.execution.pipeline import TickContext, TradingPipeline
from src.execution.portfolio import LivePortfolio
from src.execution.reconciler import Reconciler
from src.persistence.db import get_session_factory
from src.risk.blacklist import (
    CAUTION,
    HALTED,
    MANAGED,
    RISK,
    WARNING,
    BlacklistEntry,
    StaticBlacklist,
)
from src.risk.limits import CircuitBreakerState, DailyLossState, RiskEngine
from src.risk.market_state import MarketState
from src.scheduler.watchlist import build_initial_watchlist, symbols_of
from src.strategy.breakout import BreakoutStrategy
from src.strategy.news_swing import NewsSwingStrategy
from src.strategy.registry import StrategyRegistry


logger = logging.getLogger(__name__)

ERROR_NOTICE_INTERVAL = timedelta(minutes=30)
# mrkt_warn_cls_code → 블랙리스트 사유 (01 주의 / 02 경고 / 03 위험 — 코드 의미는 가정)
_MARKET_WARNING = {"01": CAUTION, "02": WARNING, "03": RISK}


def quote_blacklist_entry(q: Quote) -> BlacklistEntry | None:
    """시세 응답의 종목 상태로 동적 블랙리스트 사유 결정 (신규 매수만 차단)."""
    if q.managed:
        return BlacklistEntry(symbol=q.symbol, reason=MANAGED)
    if q.liquidation:
        return BlacklistEntry(symbol=q.symbol, reason="정리매매")
    if q.market_warning in _MARKET_WARNING:
        return BlacklistEntry(symbol=q.symbol, reason=_MARKET_WARNING[q.market_warning])
    if q.caution:
        return BlacklistEntry(symbol=q.symbol, reason="투자유의")
    if q.halted:
        return BlacklistEntry(symbol=q.symbol, reason=HALTED)
    return None


def build_market_state(quotes: dict[str, Quote]) -> MarketState:
    ms = MarketState()
    for sym, q in quotes.items():
        ms.price_change_pct[sym] = q.change_pct
        gap = q.gap_open_pct
        if gap is not None:
            ms.gap_open_pct[sym] = gap
        if q.vi_active:
            ms.vi_active.add(sym)
        if q.halted:
            ms.halted.add(sym)
    return ms


class TradingRuntime:
    def __init__(
        self,
        settings: Settings,
        kis: KisClient,
        notifier: DiscordNotifier,
        *,
        session_factory: Callable[[], Session] | None = None,
        now_provider: Callable[[], datetime] = now_kst,
        symbols: list[str] | None = None,
        reconcile_sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings
        self.kis = kis
        self.notifier = notifier
        self._session_factory = session_factory or get_session_factory()
        self._now = now_provider
        self.symbols = symbols if symbols is not None else symbols_of(build_initial_watchlist())
        self._reconcile_sleep = reconcile_sleep
        self.session: Session | None = None
        self.pipeline: TradingPipeline | None = None
        self.prepared_for: date | None = None
        self.prev_day_ohlc: dict[str, dict[str, float]] = {}
        self.avg_trade_value: dict[str, float] = {}
        self._last_error_notice: datetime | None = None

    # ------------------------------------------------------------------
    # 09:00
    # ------------------------------------------------------------------
    def prepare(self) -> None:
        now = self._now()
        late = now.time() > time(9, 5)
        self.close()
        self.session = self._session_factory()
        portfolio = LivePortfolio(self.session)

        reconciler_kw = {"managed_only": self.settings.RECONCILE_MANAGED_ONLY}
        if self._reconcile_sleep is not None:
            reconciler_kw["sleep_fn"] = self._reconcile_sleep
        reconciler = Reconciler(self.session, self.kis, **reconciler_kw)
        recon = reconciler.verify_with_grace()

        start_equity = self._read_equity()
        cap = start_equity * self.settings.MAX_DEPLOY_PCT / 100.0
        risk = RiskEngine(
            blacklist=StaticBlacklist(),
            market_state=MarketState(),
            daily_loss=DailyLossState(start_equity=start_equity, current_equity=start_equity),
            circuit_breaker=CircuitBreakerState(),
            capital_cap_krw=cap,
            now_provider=self._now,
        )

        day_start = datetime.combine(now.date(), time(0, 0))
        day_end = datetime.combine(now.date(), time(23, 59, 59))
        registry = self.build_registry()
        registry.mark_executed(portfolio.executed_signal_ids(day_start, day_end))

        orders = OrderManager(
            kis_client=self.kis, portfolio=portfolio, notifier=self.notifier,
            now_provider=self._now, shadow=self.settings.SHADOW_MODE,
        )
        restored = orders.restore_open_orders(portfolio.open_trades(day_start, day_end))

        self.pipeline = TradingPipeline(
            registry=registry, risk_engine=risk, order_manager=orders,
            portfolio=portfolio, reconciler=reconciler,
            kill_switch=self.settings.kill_switch_on,
        )
        if not recon.ok:
            self.pipeline.set_trading_off(True, reason=f"reconcile mismatch: {recon.detail}")

        self._load_daily_data()
        self.session.commit()
        self.prepared_for = now.date()

        mode = "SHADOW(주문 미송신)" if self.settings.SHADOW_MODE else "LIVE(실주문)"
        lines = [
            f"[prepare] {now:%Y-%m-%d %H:%M} {self.settings.KIS_ENV.value} / {mode}",
            f"시작 평가액 {start_equity:,.0f}원 · 투입 상한 {cap:,.0f}원 ({self.settings.MAX_DEPLOY_PCT}%)",
            f"전략: {', '.join(s.strategy_id for s in registry.strategies()) or '없음'} · 감시 {len(self.symbols)}종목 · 복원 주문 {restored}건",
            f"Reconcile: {'OK' if recon.ok else 'MISMATCH → 매매 OFF: ' + recon.detail}",
        ]
        if late:
            lines.append("⚠️ 09:05 이후 준비 — 시작 평가액이 D7 기준(09:00)과 다를 수 있음")
        if self.settings.kill_switch_on():
            lines.append("⚠️ 킬 스위치 ON — 신규 매수 차단 중")
        self.notifier.send("\n".join(lines))

    def build_registry(self) -> StrategyRegistry:
        reg = StrategyRegistry()
        if self.settings.ALLOC_BREAKOUT_PCT > 0:
            reg.register(BreakoutStrategy(), allocation_pct=self.settings.ALLOC_BREAKOUT_PCT)
        if self.settings.ALLOC_NEWS_SWING_PCT > 0:
            reg.register(NewsSwingStrategy(), allocation_pct=self.settings.ALLOC_NEWS_SWING_PCT)
        return reg

    # ------------------------------------------------------------------
    # 09:10 ~ 15:20 / 5분
    # ------------------------------------------------------------------
    def tick(self) -> int:
        now = self._now()
        if self.prepared_for != now.date() or self.pipeline is None or self.session is None:
            logger.warning("tick before prepare for %s — preparing now", now.date())
            self.prepare()
        assert self.pipeline is not None and self.session is not None

        self._refresh_missing_open()
        held = {p.symbol for p in self.pipeline.portfolio.iter_open_positions()}
        quotes = self._fetch_quotes(sorted(set(self.symbols) | held))
        self._apply_market_state(quotes)

        ctx = TickContext(
            as_of=now,
            cash_available=float(self.kis.get_cash_balance()),
            current_prices={s: q.price for s, q in quotes.items()},
            prev_day_ohlc=self.prev_day_ohlc,
            portfolio_value=self._read_equity(),
            liquidity={
                s: (q.market_cap or None, self.avg_trade_value.get(s) or None)
                for s, q in quotes.items()
            },
        )
        try:
            executed = self.pipeline.run_tick(ctx)
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        if executed:
            logger.info("tick %s executed %d signals", now.time(), len(executed))
        return len(executed)

    def close(self) -> None:
        if self.session is not None:
            try:
                self.session.commit()
            finally:
                self.session.close()
        self.session = None
        self.pipeline = None
        self.prepared_for = None

    def notify_error(self, where: str, exc: BaseException) -> None:
        """장중 오류 알림 — 30분에 1회로 제한 (Discord 과다 방지)."""
        now = self._now()
        if self._last_error_notice and now - self._last_error_notice < ERROR_NOTICE_INTERVAL:
            return
        self._last_error_notice = now
        self.notifier.send(f"[{where}] 오류: {type(exc).__name__}: {exc} ({now:%H:%M})")

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------
    def _read_equity(self) -> float:
        _, evaluation = self.kis.get_stock_balance()
        try:
            total = float(evaluation.get("tot_evlu_amt") or 0)
        except (TypeError, ValueError):
            total = 0.0
        if total > 0:
            return total
        return float(self.kis.get_cash_balance())

    def _load_daily_data(self) -> None:
        for sym in self.symbols:
            self._load_ohlc(sym)

    def _load_ohlc(self, sym: str) -> None:
        try:
            o = self.kis.get_prev_day_ohlc(sym)
        except Exception:
            logger.exception("prev-day OHLC failed: %s", sym)
            return
        if not o:
            return
        self.prev_day_ohlc[sym] = {
            "open_today": float(o.get("open_today") or 0),
            "high": float(o.get("high") or 0),
            "low": float(o.get("low") or 0),
        }
        self.avg_trade_value[sym] = float(o.get("avg_trade_value_20d") or 0)

    def _refresh_missing_open(self) -> None:
        """09:00 준비 시점엔 당일 시가가 없을 수 있다 → 시가가 채워질 때까지 재조회."""
        for sym, o in list(self.prev_day_ohlc.items()):
            if o.get("open_today", 0) <= 0:
                self._load_ohlc(sym)

    def _fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for sym in symbols:
            try:
                out[sym] = self.kis.get_quote(sym)
            except Exception:
                logger.exception("quote failed: %s", sym)
        return out

    def _apply_market_state(self, quotes: dict[str, Quote]) -> None:
        risk = self.pipeline.risk
        ms = build_market_state(quotes)
        try:
            ms.kospi_change_pct = self.kis.get_index_change_pct("0001")
            ms.kosdaq_change_pct = self.kis.get_index_change_pct("1001")
        except Exception:
            # 지수 조회 실패는 차단 사유로 삼지 않는다 (market_state.can_trade_today 정책)
            logger.exception("index fetch failed — KOSPI gate inactive this tick")
        risk.market_state = ms
        risk.blacklist = StaticBlacklist.from_iterable(
            e for e in (quote_blacklist_entry(q) for q in quotes.values()) if e is not None
        )
