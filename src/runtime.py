"""Trading runtime — prepare_state(09:00) / trading_tick(09:10~15:20) 핸들러 본체 (A13).

하루 단위 상태를 소유한다:
  - DB 세션 1개 (tick 마다 commit, 일과 종료 시 close)
  - RiskEngine (start_equity = 09:00 평가액 고정 — D7, 투입 상한 = start × MAX_DEPLOY_PCT)
    섀도 모드 + SHADOW_CAPITAL_KRW 이면 평가액·현금은 가상 자본 기준 (실계좌 조회는 계속)
  - StrategyRegistry (당일 실행 signal_id 를 DB 에서 복원 — 재시작 중복 차단)
  - OrderManager (당일 미종결 주문을 DB 에서 복원)
  - 시장 데이터: 전일 OHLC·20일 평균 거래대금(09:00), 시세·종목 상태(매 tick)
  - 감시 종목: UNIVERSE_MODE 에 따라 watchlist(static) 또는 뉴스 universe
    (universe_builder 가 만들고 뉴스 수집 job 이 refresh_universe 로 교체).
    새로 들어온 종목은 다음 tick 에서 전일 OHLC 를 조회한다.

데이터 누락은 fail-closed: 시세가 없는 종목은 신호 대상에서 빠지고, 유동성
데이터가 없는 종목은 RiskEngine 이 매수를 차단한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from src.clients.discord import DiscordNotifier
from src.clients.kis import KisClient, Quote
from src.common.clock import now_kst
from src.common.redact import redact
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
from src.risk.limits import DAILY_LOSS_LIMIT_PCT, CircuitBreakerState, DailyLossState, RiskEngine
from src.risk.market_state import MarketState
from src.persistence.models import RiskEvent
from src.reporting.models import MarketSummary, PortfolioState, RiskStatus
from src.scheduler.watchlist import build_initial_watchlist, symbols_of
from src.strategy.breakout import compute_target_price
from src.strategy.breakout import BreakoutStrategy
from src.strategy.news_swing import NewsSwingStrategy
from src.strategy.registry import StrategyRegistry
from src.strategy.universe import MODE_STATIC, Universe


logger = logging.getLogger(__name__)

ERROR_NOTICE_INTERVAL = timedelta(minutes=30)
COMPARE_EVENT = "shadow_compare"
KOSPI, KOSDAQ = "0001", "1001"
COMPARE_CUTOFF = time(15, 0)       # breakout 신규 매수 중단 시각과 동일
# mrkt_warn_cls_code → 블랙리스트 사유 (01 주의 / 02 경고 / 03 위험 — 코드 의미는 가정)
_MARKET_WARNING = {"01": CAUTION, "02": WARNING, "03": RISK}


@dataclass(frozen=True)
class ReportState:
    """리포트(미드데이·일일)에 넘길 런타임 값 — 마지막 tick 기준."""
    market: MarketSummary
    portfolio: PortfolioState
    risk: RiskStatus
    cash: float
    prices: dict[str, float] = field(default_factory=dict)


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
        universe_builder: Callable[[datetime], Universe] | None = None,
    ) -> None:
        self.settings = settings
        self.kis = kis
        self.notifier = notifier
        self._session_factory = session_factory or get_session_factory()
        self._now = now_provider
        # 예비 watchlist (UNIVERSE_MODE=static 이면 이것만 감시)
        self.symbols = symbols if symbols is not None else symbols_of(build_initial_watchlist())
        self._static_names = {e.symbol: e.name for e in build_initial_watchlist()}
        self._universe_builder = universe_builder
        self.universe: Universe | None = None     # 참조 교체만 하므로 job 스레드 간 공유 안전
        self._compare_seen: set[tuple[str, str]] = set()
        self._reconcile_sleep = reconcile_sleep
        self.session: Session | None = None
        self.pipeline: TradingPipeline | None = None
        self.prepared_for: date | None = None
        self.prev_day_ohlc: dict[str, dict[str, float]] = {}
        self.avg_trade_value: dict[str, float] = {}
        self._last_error_notice: datetime | None = None
        # 잔고 조회 실패 시 대체값 (해당 tick 은 매수 차단) — P1
        self._last_cash: float | None = None
        self._last_equity: float | None = None
        # 리포트용 — 마지막 tick 의 지수·시세·현금·평가액 (섀도 가상 자본이면 가상 값)
        self._index: dict[str, tuple[float | None, float]] = {}
        self._report_prices: dict[str, float] = {}
        self._report_cash: float | None = None
        self._report_equity: float | None = None

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

        real_equity = self._read_equity()
        self._last_equity = real_equity
        virtual = self.settings.shadow_capital()
        start_equity = virtual if virtual is not None else real_equity
        self._report_equity = start_equity
        self._report_cash = virtual
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
            now_provider=self._now, shadow=self.settings.SHADOW_MODE, name_of=self.symbol_name,
        )
        restored = orders.restore_open_orders(portfolio.open_trades(day_start, day_end))

        self.pipeline = TradingPipeline(
            registry=registry, risk_engine=risk, order_manager=orders,
            portfolio=portfolio, reconciler=reconciler,
            kill_switch=self.settings.kill_switch_on,
        )
        if not recon.ok:
            self.pipeline.set_trading_off(True, reason=f"reconcile mismatch: {recon.detail}")

        if self.universe is None or self.universe.built_at.date() != now.date():
            self.session.commit()      # 쓰기 잠금 해제 — builder 는 별도 세션으로 StockScore 를 쓴다
            self.refresh_universe(now)
        self._compare_seen = self._load_compare_seen(day_start, day_end)
        self._load_daily_data()
        self.session.commit()
        self.prepared_for = now.date()

        mode = "SHADOW(주문 미송신)" if self.settings.SHADOW_MODE else "LIVE(실주문)"
        equity_line = f"시작 평가액 {start_equity:,.0f}원 · 투입 상한 {cap:,.0f}원 ({self.settings.MAX_DEPLOY_PCT}%)"
        if virtual is not None:
            equity_line = (f"시작 평가액(가상) {start_equity:,.0f}원 · 투입 상한 {cap:,.0f}원 "
                           f"({self.settings.MAX_DEPLOY_PCT}%) · 실평가액 {real_equity:,.0f}원")
        lines = [
            f"[prepare] {now:%Y-%m-%d %H:%M} {self.settings.KIS_ENV.value} / {mode}",
            equity_line,
            f"전략: {', '.join(s.strategy_id for s in registry.strategies()) or '없음'} · 감시 {len(self.active_symbols())}종목 · 복원 주문 {restored}건",
            f"감시 종목: {self._universe_label()}",
            f"Reconcile: {'OK' if recon.ok else 'MISMATCH → 매매 OFF: ' + recon.detail}",
        ]
        if not self.settings.SHADOW_MODE and self.settings.SHADOW_CAPITAL_KRW is not None:
            lines.append("⚠️ SHADOW_CAPITAL_KRW 무시됨 — 실주문 모드는 실계좌 기준")
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

        universe = self._active_universe()
        active = set(universe.symbols) if universe is not None else set(self.symbols)
        compare = set(self.symbols) if self._comparing(universe) else set()
        held = {p.symbol for p in self.pipeline.portfolio.iter_open_positions()}
        self._ensure_daily_data(active | compare)
        quotes = self._fetch_quotes(sorted(active | held | compare))
        self._apply_market_state(quotes)
        tradable = {s: q for s, q in quotes.items() if s in active or s in held}

        cash, equity, stale = self._read_account()
        virtual = self.settings.shadow_capital()
        if virtual is not None:
            cash, equity = self._virtual_account(virtual, now.date())
        self._report_cash, self._report_equity = cash, equity
        self._report_prices = {s: q.price for s, q in quotes.items()}
        ctx = TickContext(
            as_of=now,
            cash_available=cash,
            current_prices={s: q.price for s, q in tradable.items()},
            prev_day_ohlc=self.prev_day_ohlc,
            portfolio_value=equity,
            block_entries_reason=stale,
            liquidity={
                s: (q.market_cap or None, self.avg_trade_value.get(s) or None)
                for s, q in tradable.items()
            },
            extras=self._universe_extras(universe),
        )
        try:
            executed = self.pipeline.run_tick(ctx)
            if compare:
                self._record_compare(now, quotes, {"universe": active - held, "static": compare})
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        if executed:
            logger.info("tick %s executed %d signals", now.time(), len(executed))
        return len(executed)

    # ------------------------------------------------------------------
    # universe
    # ------------------------------------------------------------------
    def refresh_universe(self, now: datetime | None = None) -> Universe | None:
        """뉴스 universe 재구성. 실패하면 직전 universe(없으면 watchlist)를 유지."""
        if self._universe_builder is None or self.settings.UNIVERSE_MODE == MODE_STATIC:
            return None
        now = now or self._now()
        try:
            u = self._universe_builder(now)
        except Exception:
            logger.exception("universe build failed — keeping previous")
            return None
        self.universe = u
        return u

    def symbol_name(self, symbol: str) -> str:
        """체결 알림용 종목명 — 감시 종목(마스터 이름) → 예비 watchlist → 빈 문자열."""
        u = self.universe
        if u is not None:
            for e in u.entries:
                if e.symbol == symbol and e.name:
                    return e.name
        return self._static_names.get(symbol, "")

    def active_symbols(self) -> list[str]:
        u = self._active_universe()
        return u.symbols if u is not None else list(self.symbols)

    def _active_universe(self) -> Universe | None:
        if self.settings.UNIVERSE_MODE == MODE_STATIC:
            return None
        return self.universe

    def _comparing(self, universe: Universe | None) -> bool:
        return universe is not None and self.settings.UNIVERSE_COMPARE_STATIC

    def _universe_label(self) -> str:
        u = self._active_universe()
        if u is None:
            return f"watchlist {len(self.symbols)} (mode={self.settings.UNIVERSE_MODE})"
        return f"{u.summary()} (mode={u.mode}, {u.built_at:%H:%M} 기준)"

    @staticmethod
    def _universe_extras(u: Universe | None) -> dict:
        if u is None:
            return {}
        return {
            "news_scores": u.news_scores,
            "entry_blocked": u.blocked,
            "news_articles": {e.symbol: e.article_ids for e in u.entries if e.article_ids},
        }

    def _load_compare_seen(self, start: datetime, end: datetime) -> set[tuple[str, str]]:
        rows = self.session.query(RiskEvent.action_taken, RiskEvent.symbol).filter(
            RiskEvent.event_type == COMPARE_EVENT, RiskEvent.ts >= start, RiskEvent.ts <= end,
        ).all()
        return {(g or "", s or "") for g, s in rows}

    def _record_compare(self, now: datetime, quotes: dict[str, Quote],
                        groups: dict[str, set[str]]) -> None:
        """섀도 비교: 그룹(universe / static)별로 돌파 조건을 처음 만족한 종목을 하루 1회 기록.
        슬롯·현금·리스크와 무관한 순수 신호 — 리포트에서 두 목록의 신호를 나란히 본다."""
        if now.time() >= COMPARE_CUTOFF:
            return
        for group, syms in groups.items():
            for sym in sorted(syms):
                if (group, sym) in self._compare_seen:
                    continue
                q, o = quotes.get(sym), self.prev_day_ohlc.get(sym)
                if q is None or not o or min(o["open_today"], o["high"], o["low"]) <= 0:
                    continue
                target = compute_target_price(o["open_today"], o["high"], o["low"])
                if q.price <= target:
                    continue
                self.session.add(RiskEvent(
                    ts=now, event_type=COMPARE_EVENT, symbol=sym, action_taken=group,
                    detail=f"{group} breakout {q.price:.0f} > {target:.0f}",
                ))
                self._compare_seen.add((group, sym))

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
        self.notifier.send(redact(f"[{where}] 오류: {type(exc).__name__}: {exc} ({now:%H:%M})"))

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------
    def _read_account(self) -> tuple[float, float, str | None]:
        """(현금, 평가액, 직전값 사용 사유). 조회 실패 시 직전 성공값으로 대체하고
        사유를 돌려준다 → 해당 tick 은 매수만 막고 청산은 진행. 직전값도 없으면 예외."""
        stale: list[str] = []
        try:
            self._last_cash = float(self.kis.get_cash_balance())
        except Exception as exc:
            if self._last_cash is None:
                raise
            logger.warning("cash balance failed — using last value: %s", redact(exc))
            stale.append("현금")
        try:
            self._last_equity = self._read_equity()
        except Exception as exc:
            if self._last_equity is None:
                raise
            logger.warning("equity read failed — using last value: %s", redact(exc))
            stale.append("평가액")
        reason = f"잔고 조회 실패({'·'.join(stale)}) — 직전값 사용" if stale else None
        return self._last_cash, self._last_equity, reason

    def _fetch_index(self) -> None:
        for code in (KOSPI, KOSDAQ):
            self._index[code] = self.kis.get_index(code)

    # ------------------------------------------------------------------
    # report
    # ------------------------------------------------------------------
    def report_state(self, *, refresh_index: bool = False) -> ReportState | None:
        """리포트용 런타임 값. prepare 전이면 None (리포트는 기본값 + 안내 줄).
        refresh_index=True 면 지수를 다시 조회 (16:00 종가) — 실패 시 마지막 값."""
        if self.pipeline is None or self.prepared_for is None:
            return None
        if refresh_index:
            try:
                self._fetch_index()
            except Exception:
                logger.warning("index refresh for report failed — using last tick values")
        risk = self.pipeline.risk
        dl, cb = risk.daily_loss, risk.circuit_breaker
        kospi = self._index.get(KOSPI, (None, None))
        kosdaq = self._index.get(KOSDAQ, (None, None))
        equity = self._report_equity if self._report_equity is not None else dl.current_equity
        cash = self._report_cash if self._report_cash is not None else (self._last_cash or 0.0)
        virtual = self.settings.shadow_capital()
        if virtual is not None and self.prepared_for is not None:
            cash, equity = self._virtual_account(virtual, self.prepared_for)   # tick 이후 섀도 매수 반영
        pnl = equity - dl.start_equity
        return ReportState(
            market=MarketSummary(kospi=kospi[0], kospi_change_pct=kospi[1],
                                 kosdaq=kosdaq[0], kosdaq_change_pct=kosdaq[1]),
            portfolio=PortfolioState(
                total_value=equity, daily_pnl_krw=pnl,
                daily_pnl_pct=pnl / dl.start_equity * 100.0 if dl.start_equity > 0 else 0.0,
                virtual=virtual is not None,
            ),
            risk=RiskStatus(
                daily_loss_usage_pct=max(0.0, -dl.loss_pct) / abs(DAILY_LOSS_LIMIT_PCT) * 100.0,
                circuit_breaker_active=cb.triggered,
                circuit_breaker_reason=cb.reason,
                consecutive_loss_days=cb.consecutive_loss_days,
            ),
            cash=cash,
            prices=dict(self._report_prices),
        )

    def _virtual_account(self, capital: float, day: date) -> tuple[float, float]:
        """섀도 가상 자본: (현금 = 자본 − 당일 섀도 매수액, 평가액 = 자본)."""
        assert self.pipeline is not None
        spent = sum(self.pipeline.portfolio.shadow_buy_notional(
            datetime.combine(day, time.min), datetime.combine(day, time(23, 59, 59)),
        ).values())
        return max(capital - spent, 0.0), capital

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
        syms = set(self.active_symbols())
        if self._comparing(self._active_universe()):
            syms |= set(self.symbols)
        for sym in sorted(syms):
            self._load_ohlc(sym)

    def _ensure_daily_data(self, symbols: set[str]) -> None:
        """universe 에 새로 들어온 종목은 전일 OHLC 조회, 당일 시가 미확정 종목은 재조회."""
        for sym in sorted(symbols):
            o = self.prev_day_ohlc.get(sym)
            if o is None or o.get("open_today", 0) <= 0:
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
            self._fetch_index()
            ms.kospi_change_pct = self._index[KOSPI][1]
            ms.kosdaq_change_pct = self._index[KOSDAQ][1]
        except Exception:
            # 지수 조회 실패는 차단 사유로 삼지 않는다 (market_state.can_trade_today 정책)
            logger.exception("index fetch failed — KOSPI gate inactive this tick")
        risk.market_state = ms
        risk.blacklist = StaticBlacklist.from_iterable(
            e for e in (quote_blacklist_entry(q) for q in quotes.values()) if e is not None
        )
