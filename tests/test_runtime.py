"""TradingRuntime 통합 — prepare(09:00) → tick 흐름을 Fake KIS 로 검증."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.clients.kis import OrderAck, OrderFill, Quote
from src.persistence.models import Base, Position, RiskEvent, Trade
from src.risk.limits import set_risk_engine
from src.runtime import TradingRuntime, build_market_state, quote_blacklist_entry

from tests.clients.conftest import make_settings


SYM = "005930"


def _quote(sym=SYM, price=72_000.0, **over):
    kw = dict(symbol=sym, price=price, market_cap=4e14, trade_value=1e11, change_pct=1.0,
              open_price=70_000.0, base_price=69_500.0, vi_active=False, halted=False,
              managed=False, market_warning="00", caution=False, liquidation=False)
    kw.update(over)
    return Quote(**kw)


class FakeKis:
    def __init__(self):
        self.holdings: dict[str, int] = {}
        self.equity = 10_000_000.0
        self.cash = 10_000_000
        self.quotes = {SYM: _quote()}
        # 목표가 = 70,000 + (71,000 − 69,000) × 0.5 = 71,000 → 현재가 72,000 돌파
        self.ohlc = {SYM: {"open_today": "70000", "high": "71000", "low": "69000",
                           "avg_trade_value_20d": str(5e11)}}
        self.kospi = 0.3
        self.orders: list[dict] = []
        self.fills: dict[str, OrderFill] = {}

    def get_stock_balance(self):
        return dict(self.holdings), {"tot_evlu_amt": str(self.equity)}

    def get_cash_balance(self):
        return self.cash

    def get_prev_day_ohlc(self, sym):
        return self.ohlc.get(sym)

    def get_quote(self, sym):
        if sym not in self.quotes:
            raise RuntimeError("no quote")
        return self.quotes[sym]

    def get_index(self, code="0001"):
        return (5_500.0 if code == "0001" else 1_100.0), self.kospi

    def order(self, *, code, qty, side, ord_dvsn, unit_price):
        odno = f"ORD{len(self.orders) + 1}"
        self.orders.append(dict(odno=odno, code=code, qty=qty, side=side, price=int(unit_price)))
        self.fills[odno] = OrderFill(odno=odno, symbol=code, side=side, ord_qty=qty,
                                     filled_qty=0, avg_price=0, remaining_qty=qty,
                                     cancelled=False, orgno="1")
        return OrderAck(odno=odno, orgno="1")

    def cancel_order(self, odno, orgno):
        pass

    def inquire_today_orders(self, day):
        return list(self.fills.values())


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, msg):
        self.sent.append(msg)

    def send_embed(self, *a, **k):
        pass


@pytest.fixture
def factory():
    eng = create_engine("sqlite:///:memory:", future=True,
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def _reset_engine():
    yield
    set_risk_engine(None)


def _rt(factory, kis=None, clock=None, tmp_path=None, **settings_over):
    clock = clock or [datetime(2026, 10, 20, 9, 0)]
    kill = (tmp_path / "KILL") if tmp_path else None
    if kill is not None:
        settings_over.setdefault("KILL_SWITCH_FILE", kill)
    rt = TradingRuntime(
        make_settings(**settings_over), kis or FakeKis(), FakeNotifier(),
        session_factory=factory, now_provider=lambda: clock[0], symbols=[SYM],
        reconcile_sleep=lambda s: None,
    )
    return rt, clock


def _trades(factory):
    with factory() as s:
        return s.query(Trade).all()


# ---------------------------------------------------------------------------

def test_prepare_sets_cap_and_reports_shadow(factory, tmp_path):
    rt, _ = _rt(factory, tmp_path=tmp_path)
    rt.prepare()
    assert rt.pipeline.risk.capital_cap_krw == 500_000          # 1,000만 × 5%
    msg = rt.notifier.sent[-1]
    assert "SHADOW" in msg and "breakout" in msg and "Reconcile: OK" in msg


def test_shadow_tick_records_but_never_sends(factory, tmp_path):
    kis = FakeKis()
    rt, clock = _rt(factory, kis, tmp_path=tmp_path)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 1
    assert kis.orders == []
    assert [t.status for t in _trades(factory)] == ["shadow"]


def test_shadow_capital_replaces_small_account(factory, tmp_path):
    """섀도 가상 자본: 실평가액 4.8만이어도 상한·매수 금액은 1,000만 기준."""
    kis = FakeKis()
    kis.equity, kis.cash = 48_000.0, 40_875
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_CAPITAL_KRW=10_000_000)
    rt.prepare()
    assert rt.pipeline.risk.capital_cap_krw == 500_000
    msg = rt.notifier.sent[-1]
    assert "시작 평가액(가상) 10,000,000원" in msg and "실평가액 48,000원" in msg
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 1
    assert kis.orders == []
    (t,) = _trades(factory)
    assert t.status == "shadow" and t.qty == 6                   # 500,000 / 72,000


def test_shadow_capital_cash_reduced_by_shadow_buys_after_restart(factory, tmp_path):
    kis = FakeKis()
    kis.equity, kis.cash = 48_000.0, 40_875
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_CAPITAL_KRW=10_000_000)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    rt2, _ = _rt(factory, kis, clock=clock, tmp_path=tmp_path, SHADOW_CAPITAL_KRW=10_000_000)
    rt2.prepare()
    cash, equity = rt2._virtual_account(10_000_000, clock[0].date())
    assert (cash, equity) == (10_000_000 - 6 * 72_000, 10_000_000)


def test_small_account_without_shadow_capital_records_qty_zero(factory, tmp_path):
    kis = FakeKis()
    kis.equity, kis.cash = 48_000.0, 40_875
    rt, clock = _rt(factory, kis, tmp_path=tmp_path)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 0
    assert _trades(factory) == []
    with factory() as s:
        ev = s.query(RiskEvent).filter_by(event_type="risk_blocked").one()
    assert ev.action_taken == "qty_zero"


def test_shadow_capital_ignored_in_live_mode(factory, tmp_path):
    kis = FakeKis()
    kis.equity, kis.cash = 48_000.0, 40_875
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False,
                    SHADOW_CAPITAL_KRW=10_000_000)
    rt.prepare()
    assert rt.pipeline.risk.capital_cap_krw == 48_000 * 0.05
    msg = rt.notifier.sent[-1]
    assert "SHADOW_CAPITAL_KRW 무시됨" in msg and "(가상)" not in msg
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 0
    assert kis.orders == []


def test_report_state_none_before_prepare(factory, tmp_path):
    rt, _ = _rt(factory, tmp_path=tmp_path)
    assert rt.report_state() is None


def test_report_state_reflects_tick_values_shadow_virtual(factory, tmp_path):
    kis = FakeKis()
    kis.equity, kis.cash, kis.kospi = 48_000.0, 40_875, -2.4
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_CAPITAL_KRW=10_000_000)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    st = rt.report_state()
    assert st.market.kospi == 5_500.0 and st.market.kospi_change_pct == -2.4
    assert st.market.kosdaq == 1_100.0
    assert st.portfolio.total_value == 10_000_000 and st.portfolio.virtual is True
    assert st.portfolio.cumulative_return_pct is None and st.portfolio.mdd_pct is None
    assert st.cash == 10_000_000 - 6 * 72_000
    assert st.prices[SYM] == 72_000
    assert st.risk.daily_loss_usage_pct == 0 and st.risk.circuit_breaker_active is False


def test_report_state_live_daily_loss_and_cb(factory, tmp_path):
    kis = FakeKis()
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    kis.equity = 9_850_000.0                       # −1.5% → 한도 3% 의 50%
    kis.quotes = {}
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    rt.pipeline.risk.circuit_breaker.triggered = True
    rt.pipeline.risk.circuit_breaker.reason = "3 loss days"
    st = rt.report_state()
    assert st.portfolio.virtual is False and st.portfolio.total_value == 9_850_000
    assert st.portfolio.daily_pnl_krw == -150_000
    assert st.portfolio.daily_pnl_pct == pytest.approx(-1.5)
    assert st.risk.daily_loss_usage_pct == pytest.approx(50.0)
    assert st.risk.circuit_breaker_active and st.risk.circuit_breaker_reason == "3 loss days"


def test_report_state_refresh_index_failure_keeps_last(factory, tmp_path):
    kis = FakeKis()
    rt, clock = _rt(factory, kis, tmp_path=tmp_path)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    kis.kospi = 1.1
    assert rt.report_state(refresh_index=True).market.kospi_change_pct == 1.1

    def boom(code):
        raise RuntimeError("down")
    kis.get_index = boom
    assert rt.report_state(refresh_index=True).market.kospi_change_pct == 1.1


def test_symbol_name_from_watchlist(factory, tmp_path):
    rt, _ = _rt(factory, tmp_path=tmp_path)
    assert rt.symbol_name("005930") == "삼성전자"
    assert rt.symbol_name("999999") == ""


def test_live_tick_sends_within_capital_cap(factory, tmp_path):
    kis = FakeKis()
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    assert len(kis.orders) == 1
    o = kis.orders[0]
    # 전략 신호 = 1,000만 × 20% / 3 ≈ 666,666 → 상한 500,000 으로 축소 → 6주 × 72,000
    assert o["qty"] == 6 and o["qty"] * o["price"] <= 500_000


def test_live_tick_quantity_respects_cap(factory, tmp_path):
    kis = FakeKis()
    kis.equity = 100_000_000.0                                   # 상한 500만
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    o = kis.orders[0]
    assert o["price"] == 72_000 and o["qty"] * o["price"] <= 5_000_000


def test_reconcile_mismatch_turns_trading_off(factory, tmp_path):
    kis = FakeKis()
    kis.holdings = {"MANUAL": 3}
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    assert "MISMATCH" in rt.notifier.sent[-1]
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 0 and kis.orders == []


def test_managed_only_ignores_manual_holdings(factory, tmp_path):
    kis = FakeKis()
    kis.holdings = {"MANUAL": 3}
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False,
                    RECONCILE_MANAGED_ONLY=True)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 1


@pytest.mark.parametrize("over,code", [
    ({"managed": True}, "blacklist"),
    ({"market_warning": "02"}, "blacklist"),
    ({"vi_active": True}, "symbol_state"),
    ({"change_pct": 26.0}, "symbol_state"),
    ({"open_price": 73_500.0, "base_price": 69_500.0}, "symbol_state"),   # 갭 +5.8%
])
def test_quote_status_blocks_entry(factory, tmp_path, over, code):
    kis = FakeKis()
    kis.quotes[SYM] = _quote(**over)
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 0 and kis.orders == []
    with factory() as s:
        assert s.query(RiskEvent).filter_by(event_type="risk_blocked").one().action_taken == code


def test_kospi_crash_blocks_entries(factory, tmp_path):
    kis = FakeKis()
    kis.kospi = -3.4
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 0
    with factory() as s:
        assert s.query(RiskEvent).one().action_taken == "market_halt"


def test_index_failure_does_not_block(factory, tmp_path):
    kis = FakeKis()

    def boom(code="0001"):
        raise RuntimeError("index down")

    kis.get_index = boom
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 1


def test_missing_quote_symbol_is_skipped(factory, tmp_path):
    kis = FakeKis()
    kis.quotes = {}
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 0 and kis.orders == []


def test_kill_switch_file_blocks_entries(factory, tmp_path):
    kis = FakeKis()
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    (tmp_path / "KILL").touch()
    rt.prepare()
    assert "킬 스위치 ON" in rt.notifier.sent[-1]
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 0 and kis.orders == []


def test_restart_same_day_does_not_resend(factory, tmp_path):
    kis = FakeKis()
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    assert len(kis.orders) == 1
    rt.close()
    # 프로세스 재시작 (같은 날) — Trade.ts 는 주입 시계로 기록된다
    clock2 = [datetime(2026, 10, 20, 9, 30)]
    rt2, _ = _rt(factory, kis, clock=clock2, tmp_path=tmp_path, SHADOW_MODE=False)
    rt2.prepare()
    assert "복원 주문 1건" in rt2.notifier.sent[-1]
    rt2.tick()
    assert len(kis.orders) == 1                                   # 중복 송신 없음


def test_tick_without_prepare_prepares_late(factory, tmp_path):
    rt, clock = _rt(factory, tmp_path=tmp_path, clock=[datetime(2026, 10, 20, 10, 0)])
    rt.tick()
    assert rt.prepared_for == datetime(2026, 10, 20).date()
    assert "09:05 이후 준비" in rt.notifier.sent[0]


def test_open_today_missing_is_refreshed(factory, tmp_path):
    kis = FakeKis()
    kis.ohlc[SYM] = {**kis.ohlc[SYM], "open_today": "0"}
    rt, clock = _rt(factory, kis, tmp_path=tmp_path)
    rt.prepare()
    assert rt.prev_day_ohlc[SYM]["open_today"] == 0
    kis.ohlc[SYM] = {**kis.ohlc[SYM], "open_today": "70000"}
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    assert rt.prev_day_ohlc[SYM]["open_today"] == 70_000


def test_fill_reflected_through_runtime(factory, tmp_path):
    kis = FakeKis()
    kis.equity = 100_000_000.0
    rt, clock = _rt(factory, kis, tmp_path=tmp_path, SHADOW_MODE=False)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    o = kis.orders[0]
    f = kis.fills[o["odno"]]
    kis.fills[o["odno"]] = OrderFill(**{**f.__dict__, "filled_qty": o["qty"],
                                        "avg_price": 72_000, "remaining_qty": 0})
    clock[0] = datetime(2026, 10, 20, 9, 15)
    rt.tick()
    with factory() as s:
        assert s.get(Position, (SYM, "breakout")).qty == o["qty"]


def test_notify_error_rate_limited(factory, tmp_path):
    rt, clock = _rt(factory, tmp_path=tmp_path)
    rt.notify_error("tick", RuntimeError("a"))
    rt.notify_error("tick", RuntimeError("b"))
    assert len(rt.notifier.sent) == 1
    clock[0] = datetime(2026, 10, 20, 9, 31)
    rt.notify_error("tick", RuntimeError("c"))
    assert len(rt.notifier.sent) == 2


def test_tick_rolls_back_on_error(factory, tmp_path):
    rt, clock = _rt(factory, tmp_path=tmp_path)
    rt.prepare()

    def boom(ctx):
        raise RuntimeError("x")

    rt.pipeline.run_tick = boom
    clock[0] = datetime(2026, 10, 20, 9, 10)
    with pytest.raises(RuntimeError):
        rt.tick()


def test_equity_falls_back_to_cash(factory, tmp_path):
    kis = FakeKis()
    kis.equity = 0
    kis.cash = 3_000_000
    rt, _ = _rt(factory, kis, tmp_path=tmp_path)
    rt.prepare()
    assert rt.pipeline.risk.daily_loss.start_equity == 3_000_000


def test_quote_helpers():
    assert quote_blacklist_entry(_quote()) is None
    assert quote_blacklist_entry(_quote(liquidation=True)).reason == "정리매매"
    assert quote_blacklist_entry(_quote(caution=True)).reason == "투자유의"
    assert quote_blacklist_entry(_quote(halted=True)) is not None
    ms = build_market_state({SYM: _quote(halted=True, vi_active=True)})
    assert SYM in ms.halted and SYM in ms.vi_active


# --- P1: 잔고 조회 실패 시 직전값 + 매수만 차단 -------------------------------------

def test_balance_failure_uses_last_value_and_blocks_entries(factory, tmp_path):
    kis = FakeKis()
    rt, clock = _rt(factory, kis, tmp_path=tmp_path)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 5)
    kis.quotes = {}                    # 첫 tick: 신호 없이 직전값만 확보
    rt.tick()
    kis.quotes = {SYM: _quote()}

    def boom():
        raise RuntimeError("HTTP 500")
    kis.get_cash_balance = boom
    kis.get_stock_balance = boom
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 0                                          # 매수 차단
    assert _trades(factory) == []


def test_balance_failure_without_last_value_raises(factory, tmp_path):
    kis = FakeKis()
    rt, clock = _rt(factory, kis, tmp_path=tmp_path)
    rt.prepare()
    rt._last_cash = None

    def boom():
        raise RuntimeError("HTTP 500")
    kis.get_cash_balance = boom
    clock[0] = datetime(2026, 10, 20, 9, 10)
    with pytest.raises(RuntimeError):
        rt.tick()


def test_notify_error_redacts_account(factory, tmp_path):
    rt, _ = _rt(factory, tmp_path=tmp_path)
    rt.notify_error("trading_tick", RuntimeError(
        "500 for url: https://h/uapi/x/inquire-balance?CANO=12345678&ACNT_PRDT_CD=01"))
    assert "12345678" not in rt.notifier.sent[-1]


# --- news-universe-v1 ------------------------------------------------------------

from src.strategy.universe import Universe, UniverseEntry  # noqa: E402

NEWS_SYM = "111110"


def _news_kis():
    kis = FakeKis()
    kis.quotes[NEWS_SYM] = _quote(NEWS_SYM, price=10_500.0, open_price=10_000.0, base_price=9_900.0)
    kis.ohlc[NEWS_SYM] = {"open_today": "10000", "high": "10400", "low": "9800",
                          "avg_trade_value_20d": str(5e11)}       # 목표가 10,300 → 돌파
    return kis


def _universe(now, *entries, blocked=()):
    return Universe(built_at=now, mode="news_with_fallback", entries=tuple(entries),
                    blocked=frozenset(blocked))


def _rt_news(factory, kis, builder, tmp_path, **over):
    clock = [datetime(2026, 10, 20, 9, 0)]
    rt = TradingRuntime(
        make_settings(KILL_SWITCH_FILE=tmp_path / "KILL", **over), kis, FakeNotifier(),
        session_factory=factory, now_provider=lambda: clock[0], symbols=[SYM],
        reconcile_sleep=lambda s: None, universe_builder=builder,
    )
    return rt, clock


def test_news_universe_symbol_loaded_and_bought(factory, tmp_path):
    kis = _news_kis()
    built = []

    def builder(now):
        built.append(now)
        return _universe(now, UniverseEntry(NEWS_SYM, "호재전자", "news", 85.0, 2, (7, 9)))
    rt, clock = _rt_news(factory, kis, builder, tmp_path)
    rt.prepare()
    assert built and "뉴스 1 · 예비 0" in rt.notifier.sent[-1]
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 1
    t = _trades(factory)[0]
    assert t.symbol == NEWS_SYM and "news 85" in t.note and "art 7,9" in t.note
    assert SYM not in {x.symbol for x in _trades(factory)}          # watchlist 종목은 매수 대상 아님


def test_shadow_compare_records_both_lists_once(factory, tmp_path):
    kis = _news_kis()
    rt, clock = _rt_news(factory, kis, lambda now: _universe(
        now, UniverseEntry(NEWS_SYM, "호재전자", "news", 85.0, 1, (1,))), tmp_path)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    clock[0] = datetime(2026, 10, 20, 9, 15)
    rt.tick()
    with factory() as s:
        ev = {(e.action_taken, e.symbol) for e in s.query(RiskEvent).filter_by(event_type="shadow_compare")}
        n = s.query(RiskEvent).filter_by(event_type="shadow_compare").count()
    assert ev == {("universe", NEWS_SYM), ("static", SYM)} and n == 2


def test_compare_disabled(factory, tmp_path):
    kis = _news_kis()
    rt, clock = _rt_news(factory, kis, lambda now: _universe(
        now, UniverseEntry(NEWS_SYM, "", "news", 85.0)), tmp_path, UNIVERSE_COMPARE_STATIC=False)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    rt.tick()
    with factory() as s:
        assert s.query(RiskEvent).filter_by(event_type="shadow_compare").count() == 0


def test_blocked_symbol_not_bought(factory, tmp_path):
    kis = _news_kis()
    rt, clock = _rt_news(factory, kis, lambda now: _universe(
        now, UniverseEntry(NEWS_SYM, "", "static"), blocked={NEWS_SYM}), tmp_path)
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 0


def test_static_mode_never_builds(factory, tmp_path):
    def builder(now):
        raise AssertionError("must not be called")
    rt, clock = _rt_news(factory, FakeKis(), builder, tmp_path, UNIVERSE_MODE="static")
    rt.prepare()
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 1 and _trades(factory)[0].symbol == SYM


def test_builder_failure_falls_back_to_watchlist(factory, tmp_path):
    def builder(now):
        raise RuntimeError("db locked")
    rt, clock = _rt_news(factory, FakeKis(), builder, tmp_path)
    rt.prepare()
    assert rt.universe is None and rt.active_symbols() == [SYM]
    clock[0] = datetime(2026, 10, 20, 9, 10)
    assert rt.tick() == 1
