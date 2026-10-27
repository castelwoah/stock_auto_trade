"""Pipeline integration test — Registry → Risk → OrderManager → Portfolio."""
import threading
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.clients.kis import OrderAck
from src.execution.order_manager import OrderManager
from src.execution.pipeline import TickContext, TradingPipeline
from src.execution.portfolio import LivePortfolio
from src.execution.reconciler import Reconciler
from src.persistence.models import Base
from src.risk.blacklist import BlacklistEntry, MANAGED, StaticBlacklist
from src.risk.limits import (
    CircuitBreakerState,
    CooldownPolicy,
    DailyLossState,
    RiskEngine,
    set_risk_engine,
)
from src.risk.market_state import MarketState
from src.risk.sizing import SizingPolicy
from src.strategy.base import Action, Signal, Strategy, StrategyContext
from src.strategy.registry import StrategyRegistry


@pytest.fixture
def session():
    # 워커 스레드 테스트를 위해 단일 연결 공유
    eng = create_engine(
        "sqlite:///:memory:", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


LIQ = {"A": (10**12, 10**10)}   # 시총 1조, 20일 평균 거래대금 100억


def _kis():
    k = MagicMock()
    k.order.return_value = OrderAck(odno="ORD1", orgno="91252")
    k.inquire_today_orders.return_value = []
    k.get_stock_balance.return_value = ({}, {})
    return k


def _tick(**over):
    kw = dict(as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
              current_prices={"A": 1000}, liquidity=LIQ)
    kw.update(over)
    return TickContext(**kw)


class _StubBuy(Strategy):
    strategy_id = "test_buy"

    def decide_entries(self, ctx):
        return [Signal(action=Action.BUY, symbol="A", strategy_id=self.strategy_id, notional=100_000, signal_id="s1")]

    def decide_exits(self, ctx):
        return []


def _risk(**over):
    defaults = dict(
        blacklist=StaticBlacklist(),
        market_state=MarketState(),
        daily_loss=DailyLossState(start_equity=10_000_000, current_equity=10_000_000),
        circuit_breaker=CircuitBreakerState(),
        sizing_policy=SizingPolicy(),
        cooldown_policy=CooldownPolicy(),
    )
    defaults.update(over)
    return RiskEngine(**defaults)


def _pipe(session, *, risk=None, kis=None, strategy=None, kill_switch=None, shadow=False,
          deploy_slots=None):
    kis = kis or _kis()
    lp = LivePortfolio(session)
    reg = StrategyRegistry()
    reg.register(strategy or _StubBuy(), allocation_pct=50)
    om = OrderManager(kis_client=kis, portfolio=lp, now_provider=lambda: datetime(2026, 4, 20, 10),
                      shadow=shadow)
    rec = Reconciler(session, kis)
    return TradingPipeline(
        registry=reg, risk_engine=risk or _risk(),
        order_manager=om, portfolio=lp, reconciler=rec, kill_switch=kill_switch,
        deploy_slots=deploy_slots,
    )


@pytest.fixture(autouse=True)
def _reset_engine():
    yield
    set_risk_engine(None)


def test_tick_submits_buy(session):
    kis = _kis()
    pipe = _pipe(session, kis=kis)
    executed = pipe.run_tick(_tick())
    assert len(executed) == 1
    assert executed[0].action == Action.BUY
    assert kis.order.call_count == 1


def test_tick_installs_engine_in_worker_thread(session):
    """APScheduler 워커 스레드처럼 ContextVar 가 비어 있는 스레드에서도 송신 가능."""
    kis = _kis()
    pipe = _pipe(session, kis=kis)
    out = []
    t = threading.Thread(target=lambda: out.append(pipe.run_tick(_tick())))
    t.start()
    t.join()
    assert len(out[0]) == 1 and kis.order.call_count == 1


def test_missing_liquidity_blocks_buy(session):
    from src.persistence.models import RiskEvent

    kis = _kis()
    pipe = _pipe(session, kis=kis)
    assert pipe.run_tick(_tick(liquidity={})) == []
    assert kis.order.call_count == 0
    assert session.query(RiskEvent).one().action_taken == "liquidity"


def test_kill_switch_blocks_entries(session):
    kis = _kis()
    pipe = _pipe(session, kis=kis, kill_switch=lambda: True)
    assert pipe.run_tick(_tick()) == []
    assert kis.order.call_count == 0


def test_kill_switch_error_is_fail_closed(session):
    def broken():
        raise OSError("disk")

    kis = _kis()
    pipe = _pipe(session, kis=kis, kill_switch=broken)
    assert pipe.run_tick(_tick()) == []
    assert kis.order.call_count == 0


class _StubSell(Strategy):
    strategy_id = "test_sell"

    def decide_entries(self, ctx):
        return []

    def decide_exits(self, ctx):
        return [Signal(action=Action.SELL, symbol=p.symbol, strategy_id=self.strategy_id,
                       qty=p.qty, signal_id=f"x-{p.symbol}") for p in self.owned_positions(ctx)]


def _seed_position(session, *, symbol="A", strategy_id="test_sell", qty=10, avg=1000.0, peak=None):
    from src.persistence.models import Position

    session.add(Position(symbol=symbol, strategy_id=strategy_id, qty=qty, avg_price=avg,
                         opened_at=datetime(2026, 4, 17, 10), highest_price=peak or avg,
                         updated_at=datetime(2026, 4, 17, 10)))
    session.flush()


def test_kill_switch_still_allows_exits(session):
    _seed_position(session)
    kis = _kis()
    pipe = _pipe(session, kis=kis, strategy=_StubSell(), kill_switch=lambda: True)
    executed = pipe.run_tick(_tick())
    assert [s.action for s in executed] == [Action.SELL]
    assert kis.order.call_args.kwargs["side"] == "SELL"


def test_tick_updates_highest_price(session):
    from src.persistence.models import Position

    _seed_position(session, strategy_id="test_buy", peak=1000.0)
    pipe = _pipe(session)
    pipe.run_tick(_tick(current_prices={"A": 1080}))
    assert session.get(Position, ("A", "test_buy")).highest_price == 1080


def test_tick_blocked_by_risk(session):
    risk = _risk(blacklist=StaticBlacklist.from_iterable([BlacklistEntry(symbol="A", reason=MANAGED)]))
    pipe = _pipe(session, risk=risk)
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    executed = pipe.run_tick(ctx)
    assert executed == []


def test_trading_off_flag_blocks_execution(session):
    pipe = _pipe(session)
    pipe.set_trading_off(True, reason="manual")
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    assert pipe.run_tick(ctx) == []


def test_reconciler_trading_off_propagates(session):
    # Reconciler 이 trading_off_required=True 를 반환하면 pipeline 자동 차단
    pipe = _pipe(session)
    # simulate 3 consecutive mismatches via direct counter manipulation
    pipe.reconciler._consecutive_failures = 5
    assert pipe.is_trading_off()
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    assert pipe.run_tick(ctx) == []


def test_zero_price_skipped(session):
    pipe = _pipe(session)
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 0},
    )
    assert pipe.run_tick(ctx) == []


# --- v3: risk_blocked RiskEvent 기록 ----------------------------------------

def test_risk_blocked_records_risk_event(session):
    """RiskBlocked 발생 시 RiskEvent(event_type='risk_blocked') 저장되는지."""
    from src.persistence.models import RiskEvent

    risk = _risk(blacklist=StaticBlacklist.from_iterable([
        BlacklistEntry(symbol="A", reason=MANAGED)
    ]))
    pipe = _pipe(session, risk=risk)
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    pipe.run_tick(ctx)
    session.commit()

    events = session.query(RiskEvent).filter_by(event_type="risk_blocked").all()
    assert len(events) == 1
    assert events[0].action_taken == "blacklist"   # RiskBlocked.code
    assert events[0].symbol == "A"
    assert "BUY A" in (events[0].detail or "")


def test_risk_blocked_reason_code_preserved(session):
    """code 별로 구분 가능해야 (blacklist vs daily_loss_limit vs cooldown)."""
    from src.persistence.models import RiskEvent

    # Daily Loss 임계 도달
    risk = _risk(daily_loss=DailyLossState(start_equity=10_000_000, current_equity=9_700_000))
    pipe = _pipe(session, risk=risk)
    ctx = TickContext(
        as_of=datetime(2026, 4, 20, 10), cash_available=5_000_000,
        current_prices={"A": 1000},
    )
    pipe.run_tick(ctx)
    session.commit()

    events = session.query(RiskEvent).filter_by(event_type="risk_blocked").all()
    assert len(events) == 1
    assert events[0].action_taken == "daily_loss_limit"


def test_capital_cap_clips_strategy_cash_and_buy_fits(session):
    """전략 신호 2,500,000(현금 500만 × 50%) → 상한 300,000 으로 축소되어 송신."""

    class _HalfCash(Strategy):
        strategy_id = "test_buy"

        def decide_entries(self, ctx):
            return [Signal(action=Action.BUY, symbol="A", strategy_id=self.strategy_id,
                           notional=ctx.cash_available, signal_id="s1")]

        def decide_exits(self, ctx):
            return []

    kis = _kis()
    pipe = _pipe(session, kis=kis, risk=_risk(capital_cap_krw=300_000), strategy=_HalfCash())
    executed = pipe.run_tick(_tick())
    assert len(executed) == 1
    assert kis.order.call_args.kwargs["qty"] == 300      # 300,000 / 1,000


def test_capital_cap_exhausted_blocks_buy(session):
    _seed_position(session, strategy_id="other", qty=300, avg=1000.0)
    kis = _kis()
    pipe = _pipe(session, kis=kis, risk=_risk(capital_cap_krw=300_000))
    assert pipe.run_tick(_tick()) == []
    assert kis.order.call_count == 0



def test_pending_buy_counts_toward_cap(session):
    """같은 날 미체결 매수가 있으면 다음 매수는 상한 잔여분만 사용."""

    class _TwoBuys(Strategy):
        strategy_id = "test_buy"

        def decide_entries(self, ctx):
            return [Signal(action=Action.BUY, symbol=s, strategy_id=self.strategy_id,
                           notional=200_000, signal_id=f"s-{s}") for s in ("A", "B")]

        def decide_exits(self, ctx):
            return []

    kis = _kis()
    kis.order.side_effect = [OrderAck(odno="O1", orgno="1"), OrderAck(odno="O2", orgno="1")]
    pipe = _pipe(session, kis=kis, risk=_risk(capital_cap_krw=300_000), strategy=_TwoBuys())
    liq = {"A": LIQ["A"], "B": LIQ["A"]}
    pipe.run_tick(_tick(current_prices={"A": 1000, "B": 1000}, liquidity=liq))
    qtys = [c.kwargs["qty"] for c in kis.order.call_args_list]
    assert qtys == [200, 100]                             # 200,000 + 100,000 = 상한


def test_block_entries_reason_blocks_buys_only(session):
    from dataclasses import replace as _replace

    kis = _kis()
    pipe = _pipe(session, kis=kis)
    assert pipe.run_tick(_replace(_tick(), block_entries_reason="잔고 조회 실패")) == []
    assert kis.order.call_count == 0

    _seed_position(session)
    pipe2 = _pipe(session, kis=kis, strategy=_StubSell())
    executed = pipe2.run_tick(_replace(_tick(), block_entries_reason="잔고 조회 실패"))
    assert [s.action for s in executed] == [Action.SELL]


# --- 섀도 가상 자본: 섀도 매수 누적 + 수량 0 기록 (2026-10-26) ---------------

class _TwoBuysShadow(Strategy):
    strategy_id = "test_buy"

    def decide_entries(self, ctx):
        return [Signal(action=Action.BUY, symbol=s, strategy_id=self.strategy_id,
                       notional=200_000, signal_id=f"s-{s}") for s in ("A", "B")]

    def decide_exits(self, ctx):
        return []


def _seed_shadow_buy(session, *, symbol="A", qty=300, price=1000.0, ts=datetime(2026, 4, 20, 9, 30)):
    from src.persistence.models import Trade

    session.add(Trade(ts=ts, symbol=symbol, side="BUY", qty=qty, price=price, strategy_id="test_buy",
                      idempotency_key=f"seed-{symbol}-{ts:%H%M}", status="shadow"))
    session.flush()


def test_shadow_buys_count_toward_cap(session):
    """섀도 매수는 송신하지 않아도 투입액 — 두 번째 매수는 상한 잔여분만."""
    from src.persistence.models import Trade

    kis = _kis()
    pipe = _pipe(session, kis=kis, risk=_risk(capital_cap_krw=300_000),
                 strategy=_TwoBuysShadow(), shadow=True)
    liq = {"A": LIQ["A"], "B": LIQ["A"]}
    pipe.run_tick(_tick(current_prices={"A": 1000, "B": 1000}, liquidity=liq))
    assert kis.order.call_count == 0
    rows = session.query(Trade).order_by(Trade.id).all()
    assert [(t.symbol, t.qty, t.status) for t in rows] == [("A", 200, "shadow"), ("B", 100, "shadow")]


def test_shadow_buy_from_db_exhausts_cap(session):
    """재시작 후에도 당일 섀도 매수(DB)가 상한에 반영된다."""
    from src.persistence.models import RiskEvent

    _seed_shadow_buy(session, symbol="B", qty=300)
    pipe = _pipe(session, risk=_risk(capital_cap_krw=300_000), shadow=True)
    assert pipe.run_tick(_tick()) == []
    ev = session.query(RiskEvent).filter_by(event_type="risk_blocked").one()
    assert ev.action_taken == "capital_cap"


def test_shadow_buy_of_other_day_ignored(session):
    _seed_shadow_buy(session, symbol="B", qty=300, ts=datetime(2026, 4, 17, 10))
    pipe = _pipe(session, risk=_risk(capital_cap_krw=300_000), shadow=True)
    assert len(pipe.run_tick(_tick())) == 1


def test_live_mode_ignores_shadow_rows(session):
    _seed_shadow_buy(session, symbol="B", qty=300)
    kis = _kis()
    pipe = _pipe(session, kis=kis, risk=_risk(capital_cap_krw=300_000))
    assert len(pipe.run_tick(_tick())) == 1
    assert kis.order.call_count == 1


def test_qty_zero_buy_recorded(session, caplog):
    """금액이 1주 가격보다 작으면 버리지 않고 risk_blocked(qty_zero) 로 남긴다."""
    from src.persistence.models import RiskEvent

    caplog.set_level("INFO")
    kis = _kis()
    pipe = _pipe(session, kis=kis, risk=_risk(capital_cap_krw=500))
    assert pipe.run_tick(_tick()) == []
    assert kis.order.call_count == 0
    ev = session.query(RiskEvent).filter_by(event_type="risk_blocked").one()
    assert ev.action_taken == "qty_zero" and ev.symbol == "A"
    assert "500" in ev.detail and "1,000" in ev.detail
    assert "qty 0" in caplog.text


# ---------------------------------------------------------------------------
# 투입 상한 슬롯 분할 (10-27)
# ---------------------------------------------------------------------------

class _ThreeBuys(Strategy):
    strategy_id = "test_buy"

    def decide_entries(self, ctx):
        return [Signal(action=Action.BUY, symbol=s, strategy_id=self.strategy_id,
                       notional=666_667, signal_id=f"s-{s}") for s in ("A", "B", "C")]

    def decide_exits(self, ctx):
        return []


LIQ3 = {s: (10**12, 10**10) for s in ("A", "B", "C")}


def test_deploy_slots_split_cap_across_symbols(session):
    """상한 500,000 ÷ 3슬롯 → 종목당 166,666 — 첫 종목이 상한을 다 쓰지 않는다."""
    kis = _kis()
    pipe = _pipe(session, kis=kis, risk=_risk(capital_cap_krw=500_000), strategy=_ThreeBuys(),
                 deploy_slots=3)
    executed = pipe.run_tick(_tick(current_prices={"A": 97_200, "B": 8_930, "C": 75_100},
                                   liquidity=LIQ3))
    assert [s.symbol for s in executed] == ["A", "B", "C"]
    qtys = [c.kwargs["qty"] for c in kis.order.call_args_list]
    assert qtys == [1, 18, 2]
    assert all(s.notional <= 500_000 / 3 + 1e-6 for s in executed)


def test_without_deploy_slots_first_symbol_takes_cap(session):
    """슬롯 미지정(이전 동작): 첫 종목이 상한 잔여 전부를 쓴다."""
    kis = _kis()
    pipe = _pipe(session, kis=kis, risk=_risk(capital_cap_krw=500_000), strategy=_ThreeBuys())
    executed = pipe.run_tick(_tick(current_prices={"A": 97_200, "B": 8_930, "C": 75_100},
                                   liquidity=LIQ3))
    assert executed[0].notional == 500_000


def test_deploy_slots_limited_by_remaining_headroom(session):
    _seed_position(session, strategy_id="other", qty=400, avg=1000.0)   # 잔여 100,000
    kis = _kis()
    pipe = _pipe(session, kis=kis, risk=_risk(capital_cap_krw=500_000), deploy_slots=3)
    executed = pipe.run_tick(_tick())
    assert executed[0].notional == 100_000


def test_deploy_slots_ignored_without_cap(session):
    kis = _kis()
    pipe = _pipe(session, kis=kis, deploy_slots=3)
    executed = pipe.run_tick(_tick())
    assert executed[0].notional == 100_000                  # 전략 금액 그대로


# ---------------------------------------------------------------------------
# risk_blocked 1일 1회 기록 (10-27)
# ---------------------------------------------------------------------------

def _blocks(session):
    from src.persistence.models import RiskEvent
    return session.query(RiskEvent).filter_by(event_type="risk_blocked").all()


def test_qty_zero_recorded_once_per_day_across_ticks(session, caplog):
    caplog.set_level("DEBUG")
    pipe = _pipe(session, risk=_risk(capital_cap_krw=500))
    for minute in (10, 15, 20):
        pipe.run_tick(_tick(as_of=datetime(2026, 4, 20, 10, minute)))
    assert len(_blocks(session)) == 1
    infos = [r for r in caplog.records if "qty 0" in r.getMessage() and r.levelname == "INFO"]
    debugs = [r for r in caplog.records if "qty 0" in r.getMessage() and r.levelname == "DEBUG"]
    assert len(infos) == 1 and len(debugs) == 2


def test_risk_block_dedupe_survives_restart(session):
    """새 pipeline(재시작)도 DB 를 보고 같은 날 같은 차단을 다시 쓰지 않는다."""
    _pipe(session, risk=_risk(capital_cap_krw=500)).run_tick(_tick())
    _pipe(session, risk=_risk(capital_cap_krw=500)).run_tick(_tick(as_of=datetime(2026, 4, 20, 11)))
    assert len(_blocks(session)) == 1


def test_risk_block_new_day_or_new_code_recorded(session):
    pipe = _pipe(session, risk=_risk(capital_cap_krw=500))
    pipe.run_tick(_tick())
    pipe.run_tick(_tick(as_of=datetime(2026, 4, 21, 10)))              # 다음 날
    risk = _risk(blacklist=StaticBlacklist.from_iterable([BlacklistEntry(symbol="A", reason=MANAGED)]))
    _pipe(session, risk=risk).run_tick(_tick(as_of=datetime(2026, 4, 21, 10, 5)))   # 다른 사유
    codes = sorted((e.ts.date().isoformat(), e.action_taken) for e in _blocks(session))
    assert codes == [("2026-04-20", "qty_zero"), ("2026-04-21", "blacklist"), ("2026-04-21", "qty_zero")]
