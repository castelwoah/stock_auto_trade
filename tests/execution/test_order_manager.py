from datetime import datetime, time
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.execution.order_manager import (
    ENTRY_CUTOFF,
    EOD_CANCEL,
    MAX_RETRIES,
    OrderManager,
    upward_tick,
)
from src.execution.portfolio import LivePortfolio
from src.persistence.models import Base, Trade
from src.strategy.base import Action, Signal


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _kis_mock(order_id="ORD1"):
    k = MagicMock()
    k.order.return_value = {"output": {"ODNO": order_id}}
    return k


def _sig(action=Action.BUY, symbol="A", notional=100_000, qty=0):
    return Signal(
        action=action, symbol=symbol, strategy_id="news_swing",
        notional=notional, qty=qty, signal_id=f"sig-{symbol}",
    )


def _now_at(h, m=0):
    return datetime(2026, 4, 20, h, m, 0)


def test_upward_tick_increments():
    assert upward_tick(1000, fraction=0.01) == 1010


def test_submit_records_trade_and_pending(session):
    kis = _kis_mock("ORD1")
    lp = LivePortfolio(session)
    om = OrderManager(kis_client=kis, portfolio=lp, now_provider=lambda: _now_at(10))
    po = om.submit(_sig(), limit_price=1000, qty=50, idempotency_key="k1")
    assert po is not None and po.order_id == "ORD1"
    assert "ORD1" in om.pending
    session.commit()
    assert session.query(Trade).count() == 1


def test_buy_after_cutoff_rejected(session):
    kis = _kis_mock()
    lp = LivePortfolio(session)
    om = OrderManager(kis_client=kis, portfolio=lp, now_provider=lambda: _now_at(15, 1))
    po = om.submit(_sig(Action.BUY), limit_price=1000, qty=10, idempotency_key="k")
    assert po is None
    assert kis.order.call_count == 0


def test_sell_after_cutoff_allowed(session):
    kis = _kis_mock("S1")
    lp = LivePortfolio(session)
    om = OrderManager(kis_client=kis, portfolio=lp, now_provider=lambda: _now_at(15, 10))
    po = om.submit(_sig(Action.SELL, qty=5), limit_price=1000, qty=5, idempotency_key="sellk")
    # 15:00 이후지만 SELL 은 허용
    assert po is not None


def test_submit_qty_zero_rejected(session):
    kis = _kis_mock()
    lp = LivePortfolio(session)
    om = OrderManager(kis_client=kis, portfolio=lp, now_provider=lambda: _now_at(10))
    assert om.submit(_sig(), limit_price=1000, qty=0, idempotency_key="k") is None


def test_kis_order_failure_marks_rejected(session):
    kis = MagicMock()
    kis.order.side_effect = RuntimeError("boom")
    lp = LivePortfolio(session)
    om = OrderManager(kis_client=kis, portfolio=lp, now_provider=lambda: _now_at(10))
    om.submit(_sig(), limit_price=1000, qty=50, idempotency_key="k1")
    session.commit()
    t = session.query(Trade).one()
    assert t.status == "rejected"


def test_poll_resubmits_after_timeout(session):
    kis = MagicMock()
    kis.order.side_effect = [
        {"output": {"ODNO": "ORD1"}},
        {"output": {"ODNO": "ORD2"}},
    ]
    lp = LivePortfolio(session)
    clock = [_now_at(10)]
    om = OrderManager(
        kis_client=kis, portfolio=lp, now_provider=lambda: clock[0],
        resubmit_after_seconds=60,
    )
    om.submit(_sig(), limit_price=1000, qty=50, idempotency_key="k1")
    # 2분 뒤 polling
    from datetime import timedelta
    clock[0] = _now_at(10) + timedelta(seconds=120)
    om.poll(current_prices={"A": 1010})
    # 원래 주문은 cancelled, 새 order 가 추가
    assert "ORD1" not in om.pending
    assert "ORD2" in om.pending
    assert om.pending["ORD2"].retries == 1


def test_poll_cancels_after_max_retries(session):
    kis = MagicMock()
    kis.order.side_effect = [
        {"output": {"ODNO": f"ORD{i}"}} for i in range(10)
    ]
    lp = LivePortfolio(session)
    clock = [_now_at(10)]
    cancel_hook = MagicMock(return_value=True)
    om = OrderManager(
        kis_client=kis, portfolio=lp, now_provider=lambda: clock[0],
        resubmit_after_seconds=1, cancel_hook=cancel_hook,
    )
    om.submit(_sig(), limit_price=1000, qty=50, idempotency_key="k1")
    from datetime import timedelta
    # 반복 poll → 두 번 재시도 후 취소
    for i in range(5):
        clock[0] = _now_at(10) + timedelta(seconds=10 * (i + 1))
        om.poll(current_prices={"A": 1010})
    # MAX_RETRIES=2 후 최종 cancel
    assert all(po.cancelled or po.filled for po in om.pending.values()) or not om.pending


def test_poll_eod_cancels_buy(session):
    kis = _kis_mock("ORD1")
    lp = LivePortfolio(session)
    clock = [_now_at(10)]
    om = OrderManager(
        kis_client=kis, portfolio=lp, now_provider=lambda: clock[0],
        resubmit_after_seconds=9999,   # timeout 보다 EOD 가 먼저
    )
    om.submit(_sig(), limit_price=1000, qty=50, idempotency_key="k1")
    clock[0] = _now_at(15, 15)
    om.poll(current_prices={"A": 1000})
    assert "ORD1" not in om.pending  # 취소 후 제거


def test_mark_filled_updates_position(session):
    kis = _kis_mock("ORD1")
    lp = LivePortfolio(session)
    om = OrderManager(kis_client=kis, portfolio=lp, now_provider=lambda: _now_at(10))
    om.submit(_sig(), limit_price=1000, qty=50, idempotency_key="k1")
    om.mark_filled("ORD1", fill_price=1000, ts=_now_at(10, 30))
    session.commit()
    from src.persistence.models import Position
    p = session.get(Position, ("A", "news_swing"))
    assert p is not None and p.qty == 50


def test_constants_match_rules():
    assert ENTRY_CUTOFF == time(15, 0)
    assert EOD_CANCEL == time(15, 15)
    assert MAX_RETRIES == 2
