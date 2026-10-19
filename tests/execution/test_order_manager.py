"""OrderManager v4 — 송신 게이트 · 체결 동기화 · 취소 확인 후 재주문 · risk_gate · shadow."""
from datetime import datetime, time, timedelta

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.clients.kis import KisOrderRejected, OrderAck, OrderFill
from src.execution.order_manager import (
    ENTRY_CUTOFF,
    EOD_CANCEL,
    MAX_RETRIES,
    OrderManager,
    initial_price,
    reprice,
)
from src.execution.portfolio import LivePortfolio
from src.persistence.models import Base, Position, Trade
from src.risk.blacklist import BlacklistEntry, MANAGED, StaticBlacklist
from src.risk.limits import (
    BuyContext,
    CircuitBreakerState,
    DailyLossState,
    RiskEngine,
    set_risk_engine,
)
from src.risk.market_state import MarketState
from src.strategy.base import Action, Signal


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _engine(**over):
    kw = dict(
        blacklist=StaticBlacklist(),
        market_state=MarketState(),
        daily_loss=DailyLossState(start_equity=10_000_000, current_equity=10_000_000),
        circuit_breaker=CircuitBreakerState(),
    )
    kw.update(over)
    return RiskEngine(**kw)


@pytest.fixture(autouse=True)
def risk_engine():
    eng = _engine()
    set_risk_engine(eng)
    yield eng
    set_risk_engine(None)


class FakeKis:
    """주문 접수 · 취소 · 체결조회 시뮬레이터."""

    def __init__(self):
        self.orders: list[dict] = []
        self.cancels: list[str] = []
        self.fills: dict[str, OrderFill] = {}
        self.order_errors: list[Exception] = []
        self.cancel_error: Exception | None = None
        self.inquire_error: Exception | None = None
        self.inquire_calls = 0
        self._seq = 0

    def order(self, *, code, qty, side, ord_dvsn, unit_price):
        if self.order_errors:
            raise self.order_errors.pop(0)
        self._seq += 1
        odno = f"ORD{self._seq}"
        self.orders.append(dict(odno=odno, code=code, qty=qty, side=side, price=int(unit_price)))
        self.fills[odno] = OrderFill(odno=odno, symbol=code, side=side, ord_qty=qty,
                                     filled_qty=0, avg_price=0.0, remaining_qty=qty,
                                     cancelled=False, orgno="91252")
        return OrderAck(odno=odno, orgno="91252")

    def cancel_order(self, odno, orgno):
        if self.cancel_error is not None:
            raise self.cancel_error
        assert orgno, "cancel requires orgno"
        self.cancels.append(odno)
        f = self.fills[odno]
        self.fills[odno] = OrderFill(**{**f.__dict__, "cancelled": True, "remaining_qty": 0})

    def inquire_today_orders(self, day):
        self.inquire_calls += 1
        if self.inquire_error is not None:
            raise self.inquire_error
        return list(self.fills.values())

    def fill(self, odno, qty, price):
        f = self.fills[odno]
        self.fills[odno] = OrderFill(**{**f.__dict__, "filled_qty": qty, "avg_price": price,
                                        "remaining_qty": f.ord_qty - qty})


def _sig(action=Action.BUY, symbol="005930", qty=0):
    return Signal(action=action, symbol=symbol, strategy_id="news_swing",
                  notional=1_000_000, qty=qty, signal_id=f"sig-{symbol}-{action.value}")


def _at(h, m=0, s=0):
    return datetime(2026, 10, 20, h, m, s)


def _om(session, kis=None, clock=None, **kw):
    clock = clock or [_at(10)]
    return OrderManager(
        kis_client=kis or FakeKis(), portfolio=LivePortfolio(session),
        now_provider=lambda: clock[0], **kw,
    ), clock


def _ctx(symbol="005930"):
    return BuyContext(symbol=symbol, portfolio_value=10_000_000,
                      market_cap=10**12, avg_volume_krw=10**10, intended_notional=700_000)


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------

def test_initial_price_buy_rounds_up_sell_discounts_and_rounds_down():
    assert initial_price(Action.BUY, 70_049) == 70_100
    assert initial_price(Action.SELL, 70_000) == 69_300   # −1% → 69,300 (100원 단위)


def test_reprice_directions():
    assert reprice(Action.BUY, prev_price=70_000, ref_price=70_500) == 70_600
    assert reprice(Action.SELL, prev_price=69_300, ref_price=70_000) == 69_200


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def test_submit_sends_tick_aligned_limit_and_records(session):
    kis = FakeKis()
    om, _ = _om(session, kis)
    po = om.submit(_sig(), limit_price=70_049, qty=10, idempotency_key="k1", buy_context=_ctx())
    assert po is not None and po.order_id == "ORD1" and po.orgno == "91252"
    assert kis.orders[0]["price"] == 70_100
    t = session.query(Trade).one()
    assert t.status == "submitted" and t.order_id == "ORD1"


def test_duplicate_key_not_sent_again(session):
    """재시작 시나리오: 같은 idempotency_key 로 다시 submit → KIS 송신 0회."""
    kis = FakeKis()
    om, _ = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k1", buy_context=_ctx())
    om2, _ = _om(session, kis)   # 새 프로세스 (pending 비어 있음)
    assert om2.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k1",
                      buy_context=_ctx()) is None
    assert len(kis.orders) == 1


def test_buy_after_cutoff_not_sent(session):
    kis = FakeKis()
    om, _ = _om(session, kis, clock=[_at(15, 1)])
    assert om.submit(_sig(), limit_price=70_000, qty=1, idempotency_key="k",
                     buy_context=_ctx()) is None
    assert kis.orders == []


def test_sell_after_cutoff_allowed(session):
    kis = FakeKis()
    om, _ = _om(session, kis, clock=[_at(15, 10)])
    assert om.submit(_sig(Action.SELL, qty=5), limit_price=70_000, qty=5,
                     idempotency_key="s") is not None


def test_submit_invalid_qty(session):
    om, _ = _om(session)
    assert om.submit(_sig(), limit_price=70_000, qty=0, idempotency_key="k") is None


def test_kis_reject_marks_rejected(session):
    kis = FakeKis()
    kis.order_errors.append(KisOrderRejected("APBK0919", "주문가능금액 부족"))
    om, _ = _om(session, kis)
    assert om.submit(_sig(), limit_price=70_000, qty=1, idempotency_key="k",
                     buy_context=_ctx()) is None
    t = session.query(Trade).one()
    assert t.status == "rejected" and "APBK0919" in t.note
    assert om.pending == {}


def test_invalid_params_marks_rejected(session):
    kis = FakeKis()
    kis.order_errors.append(ValueError("bad"))
    om, _ = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=1, idempotency_key="k", buy_context=_ctx())
    assert session.query(Trade).one().status == "rejected"


def test_network_error_marks_unknown_and_adopts_later(session):
    kis = FakeKis()
    kis.order_errors.append(requests.Timeout("t/o"))
    om, clock = _om(session, kis)
    assert om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k",
                     buy_context=_ctx()) is None
    assert session.query(Trade).one().status == "unknown"
    assert "unknown:k" in om.pending
    # 실제로는 접수돼 있었다 → 체결조회에 나타남
    kis.fills["ORD9"] = OrderFill(odno="ORD9", symbol="005930", side="BUY", ord_qty=10,
                                  filled_qty=10, avg_price=70_000, remaining_qty=0,
                                  cancelled=False, orgno="91252")
    om.poll(current_prices={"005930": 70_000})
    t = session.query(Trade).one()
    assert t.status == "filled" and t.order_id == "ORD9"
    assert session.get(Position, ("005930", "news_swing")).qty == 10


def test_risk_gate_blocks_at_send(session, risk_engine):
    set_risk_engine(_engine(blacklist=StaticBlacklist.from_iterable(
        [BlacklistEntry(symbol="005930", reason=MANAGED)])))
    kis = FakeKis()
    om, _ = _om(session, kis)
    assert om.submit(_sig(), limit_price=70_000, qty=1, idempotency_key="k",
                     buy_context=_ctx()) is None
    assert kis.orders == []
    assert session.query(Trade).one().status == "blocked"


def test_no_engine_fails_loud(session):
    set_risk_engine(None)
    om, _ = _om(session)
    with pytest.raises(RuntimeError, match="no RiskEngine"):
        om.submit(_sig(), limit_price=70_000, qty=1, idempotency_key="k", buy_context=_ctx())


def test_buy_without_context_fails_loud(session):
    om, _ = _om(session)
    with pytest.raises(RuntimeError, match="buy_context"):
        om.submit(_sig(), limit_price=70_000, qty=1, idempotency_key="k")


def test_shadow_mode_never_calls_kis(session):
    kis = FakeKis()
    om, _ = _om(session, kis, shadow=True)
    po = om.submit(_sig(), limit_price=70_000, qty=1, idempotency_key="k", buy_context=_ctx())
    assert po is not None and po.order_id.startswith("shadow-")
    assert kis.orders == [] and om.pending == {}
    assert session.query(Trade).one().status == "shadow"


def test_shadow_still_respects_risk_gate(session):
    set_risk_engine(_engine(blacklist=StaticBlacklist.from_iterable(
        [BlacklistEntry(symbol="005930", reason=MANAGED)])))
    om, _ = _om(session, shadow=True)
    assert om.submit(_sig(), limit_price=70_000, qty=1, idempotency_key="k",
                     buy_context=_ctx()) is None
    assert session.query(Trade).one().status == "blocked"


# ---------------------------------------------------------------------------
# poll — fills
# ---------------------------------------------------------------------------

def test_full_fill_updates_position_and_trade(session):
    kis = FakeKis()
    om, _ = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    kis.fill("ORD1", 10, 70_000)
    om.poll(current_prices={})
    assert session.get(Position, ("005930", "news_swing")).qty == 10
    assert session.query(Trade).one().status == "filled"
    assert om.pending == {}


def test_partial_fill_then_timeout_resubmits_only_remaining(session):
    kis = FakeKis()
    om, clock = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    kis.fill("ORD1", 4, 70_000)
    clock[0] = _at(10, 4)
    om.poll(current_prices={"005930": 70_200})
    assert kis.cancels == ["ORD1"]
    assert kis.orders[1]["qty"] == 6
    assert kis.orders[1]["price"] == 70_300            # max(70,000, 70,200) + 1호가
    assert session.get(Position, ("005930", "news_swing")).qty == 4
    rows = {t.idempotency_key: t for t in session.query(Trade).all()}
    assert rows["k"].status == "filled" and rows["k"].qty == 4      # 부분 체결 확정
    assert rows["k#r1"].qty == 6 and rows["k#r1"].status == "submitted"


def test_cancel_failure_blocks_resubmit(session):
    kis = FakeKis()
    kis.cancel_error = KisOrderRejected("X", "취소 불가")
    om, clock = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    clock[0] = _at(10, 4)
    om.poll(current_prices={"005930": 70_000})
    assert len(kis.orders) == 1                        # 재주문 없음
    assert "ORD1" in om.pending and om.pending["ORD1"].active


def test_fill_sync_failure_skips_actions(session):
    kis = FakeKis()
    om, clock = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    kis.inquire_error = RuntimeError("down")
    clock[0] = _at(10, 4)
    om.poll(current_prices={"005930": 70_000})
    assert kis.cancels == [] and len(kis.orders) == 1


def test_fill_right_before_cancel_no_resubmit(session):
    """취소 직전 전량 체결 → 재조회에서 filled → 재주문하지 않음."""
    kis = FakeKis()
    om, clock = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    original_cancel = kis.cancel_order

    def cancel_after_fill(odno, orgno):
        kis.fill(odno, 10, 70_000)
        original_cancel(odno, orgno)

    kis.cancel_order = cancel_after_fill
    clock[0] = _at(10, 4)
    om.poll(current_prices={"005930": 70_000})
    assert len(kis.orders) == 1
    assert session.query(Trade).one().status == "filled"


def test_buy_retries_capped_then_cancelled(session):
    kis = FakeKis()
    om, clock = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    for i in range(1, 6):
        clock[0] = _at(10) + timedelta(minutes=4 * i)
        om.poll(current_prices={"005930": 70_000})
    assert len(kis.orders) == 1 + MAX_RETRIES
    statuses = sorted(t.status for t in session.query(Trade).all())
    assert statuses == ["cancelled_resubmit", "cancelled_resubmit", "cancelled_timeout"]
    assert om.pending == {}


def test_sell_keeps_lowering_without_cap(session):
    kis = FakeKis()
    om, clock = _om(session, kis)
    om.submit(_sig(Action.SELL, qty=10), limit_price=70_000, qty=10, idempotency_key="s")
    for i in range(1, 6):
        clock[0] = _at(10) + timedelta(minutes=4 * i)
        om.poll(current_prices={"005930": 70_000})
    prices = [o["price"] for o in kis.orders]
    assert len(prices) == 6                             # 1 + 5 재주문, 상한 없음
    assert prices == sorted(prices, reverse=True) and len(set(prices)) == 6
    assert prices[0] == 69_300


def test_buy_resubmit_blocked_after_cutoff(session):
    kis = FakeKis()
    om, clock = _om(session, kis, clock=[_at(14, 58)])
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    clock[0] = _at(15, 2)
    om.poll(current_prices={"005930": 70_000})
    assert len(kis.orders) == 1 and kis.cancels == ["ORD1"]
    assert session.query(Trade).one().status == "cancelled_timeout"


def test_eod_cancel_calls_cancel_tr(session):
    kis = FakeKis()
    om, clock = _om(session, kis, resubmit_after_seconds=99_999)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    clock[0] = _at(15, 15)
    om.poll(current_prices={"005930": 70_000})
    assert kis.cancels == ["ORD1"]
    assert session.query(Trade).one().status == "cancelled_eod"


def test_resubmit_blocked_by_risk_gate(session):
    """재주문도 gate 를 통과해야 한다 (예: 그 사이 Daily Loss 도달)."""
    kis = FakeKis()
    om, clock = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    set_risk_engine(_engine(daily_loss=DailyLossState(start_equity=10_000_000,
                                                      current_equity=9_600_000)))
    clock[0] = _at(10, 4)
    om.poll(current_prices={"005930": 70_000})
    assert len(kis.orders) == 1
    rows = {t.idempotency_key: t.status for t in session.query(Trade).all()}
    assert rows == {"k": "cancelled_resubmit", "k#r1": "blocked"}


def test_external_cancel_detected(session):
    kis = FakeKis()
    om, _ = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    f = kis.fills["ORD1"]
    kis.fills["ORD1"] = OrderFill(**{**f.__dict__, "cancelled": True})
    om.poll(current_prices={})
    assert session.query(Trade).one().status == "cancelled_manual"


def test_mark_filled_manual_path(session):
    om, _ = _om(session)
    om.submit(_sig(), limit_price=70_000, qty=5, idempotency_key="k", buy_context=_ctx())
    om.mark_filled("ORD1", fill_price=70_000, ts=_at(10, 30))
    assert session.get(Position, ("005930", "news_swing")).qty == 5
    assert session.query(Trade).one().status == "filled"


def test_mark_cancelled_manual_path(session):
    om, _ = _om(session)
    om.submit(_sig(), limit_price=70_000, qty=5, idempotency_key="k", buy_context=_ctx())
    om.mark_cancelled("ORD1")
    assert session.query(Trade).one().status == "cancelled_manual"


def test_poll_without_pending_does_not_query(session):
    kis = FakeKis()
    om, _ = _om(session, kis)
    om.poll(current_prices={})
    assert kis.inquire_calls == 0


def test_constants_match_rules():
    assert ENTRY_CUTOFF == time(15, 0)
    assert EOD_CANCEL == time(15, 15)
    assert MAX_RETRIES == 2


# ---------------------------------------------------------------------------
# 재시작 복원
# ---------------------------------------------------------------------------

def _restart(session, kis, clock):
    om2 = OrderManager(kis_client=kis, portfolio=LivePortfolio(session),
                       now_provider=lambda: clock[0])
    lp = LivePortfolio(session)
    n = om2.restore_open_orders(lp.open_trades(datetime(2000, 1, 1), datetime(2999, 1, 1)))
    return om2, n


def test_restore_tracks_fills_after_restart(session):
    kis = FakeKis()
    om, clock = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    session.commit()
    om2, n = _restart(session, kis, clock)
    assert n == 1 and om2.pending["ORD1"].orgno == ""
    kis.fill("ORD1", 10, 70_000)
    om2.poll(current_prices={})
    assert session.get(Position, ("005930", "news_swing")).qty == 10
    assert session.query(Trade).one().status == "filled"


def test_restored_buy_is_cancelled_not_resubmitted(session):
    kis = FakeKis()
    om, clock = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="k", buy_context=_ctx())
    session.commit()
    om2, _ = _restart(session, kis, clock)
    clock[0] = _at(10, 4)
    om2.poll(current_prices={"005930": 70_000})
    assert kis.cancels == ["ORD1"] and len(kis.orders) == 1    # orgno 보충 후 취소
    assert session.query(Trade).one().status == "cancelled_timeout"


def test_restored_sell_keeps_repricing(session):
    kis = FakeKis()
    om, clock = _om(session, kis)
    om.submit(_sig(Action.SELL, qty=10), limit_price=70_000, qty=10, idempotency_key="s")
    session.commit()
    om2, _ = _restart(session, kis, clock)
    clock[0] = _at(10, 4)
    om2.poll(current_prices={"005930": 70_000})
    assert len(kis.orders) == 2 and kis.orders[1]["price"] < kis.orders[0]["price"]


def test_restore_skips_closed_and_restores_unknown(session):
    kis = FakeKis()
    kis.order_errors.append(requests.Timeout("t/o"))
    om, clock = _om(session, kis)
    om.submit(_sig(), limit_price=70_000, qty=10, idempotency_key="u", buy_context=_ctx())
    om.submit(_sig(symbol="000660"), limit_price=100_000, qty=1, idempotency_key="f",
              buy_context=_ctx("000660"))
    kis.fill("ORD1", 1, 100_000)
    om.poll(current_prices={})
    session.commit()
    om2, n = _restart(session, kis, clock)
    assert n == 1 and "unknown:u" in om2.pending
