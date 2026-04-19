from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.execution.reconciler import Reconciler, ReconcileResult, MAX_CONSECUTIVE_MISMATCH
from src.persistence.models import Base, Position, RiskEvent


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _pos(session, symbol, strategy, qty=10, price=1000):
    p = Position(
        symbol=symbol, strategy_id=strategy, qty=qty, avg_price=price,
        opened_at=datetime(2026, 4, 19),
    )
    session.add(p)
    session.commit()
    return p


def _kis_mock(holdings):
    k = MagicMock()
    k.get_stock_balance.return_value = (dict(holdings), {})
    return k


def test_verify_ok_when_match(session):
    _pos(session, "A", "x", qty=10)
    rec = Reconciler(session, _kis_mock({"A": 10}))
    r = rec.verify()
    assert r.ok and not r.diffs


def test_verify_detects_diff(session):
    _pos(session, "A", "x", qty=10)
    rec = Reconciler(session, _kis_mock({"A": 7}))
    r = rec.verify()
    assert not r.ok
    assert r.diffs == [("A", 7, 10)]


def test_mismatch_records_risk_event(session):
    _pos(session, "A", "x", qty=10)
    rec = Reconciler(session, _kis_mock({"A": 5}))
    rec.verify()
    session.commit()
    events = session.query(RiskEvent).filter_by(event_type="reconcile_mismatch").all()
    assert len(events) == 1
    assert "KIS=5" in (events[0].detail or "")


def test_consecutive_mismatch_triggers_trading_off(session):
    _pos(session, "A", "x", qty=10)
    rec = Reconciler(session, _kis_mock({"A": 7}), retry_grace_seconds=0, sleep_fn=lambda s: None)
    for _ in range(MAX_CONSECUTIVE_MISMATCH):
        rec.verify()
    assert rec.trading_off_required()


def test_success_resets_counter(session):
    _pos(session, "A", "x", qty=10)
    kis = MagicMock()
    # 처음 2번 mismatch, 3번째 match
    kis.get_stock_balance.side_effect = [
        ({"A": 5}, {}),
        ({"A": 5}, {}),
        ({"A": 10}, {}),
    ]
    rec = Reconciler(session, kis)
    rec.verify()
    rec.verify()
    r = rec.verify()
    assert r.ok
    assert not rec.trading_off_required()


def test_reset_clears_counter(session):
    _pos(session, "A", "x", qty=10)
    rec = Reconciler(session, _kis_mock({"A": 5}), retry_grace_seconds=0, sleep_fn=lambda s: None)
    for _ in range(MAX_CONSECUTIVE_MISMATCH):
        rec.verify()
    assert rec.trading_off_required()
    rec.reset()
    assert not rec.trading_off_required()


def test_verify_with_grace_retries(session):
    _pos(session, "A", "x", qty=10)
    kis = MagicMock()
    # 2번 fail → 3번째 pass
    kis.get_stock_balance.side_effect = [
        ({"A": 5}, {}),
        ({"A": 5}, {}),
        ({"A": 10}, {}),
    ]
    sleeps: list[float] = []
    rec = Reconciler(session, kis, retry_grace_seconds=0, sleep_fn=lambda s: sleeps.append(s))
    r = rec.verify_with_grace()
    assert r.ok
    assert len(sleeps) >= 1


def test_sums_same_symbol_across_strategies(session):
    _pos(session, "A", "x", qty=5)
    _pos(session, "A", "y", qty=3)
    rec = Reconciler(session, _kis_mock({"A": 8}))
    r = rec.verify()
    assert r.ok  # 5 + 3 = 8


def test_missing_in_db(session):
    # KIS 에 있는데 DB 에 없는 경우
    rec = Reconciler(session, _kis_mock({"A": 3}))
    r = rec.verify()
    assert not r.ok
    assert r.diffs[0] == ("A", 3, 0)


def test_missing_in_kis(session):
    _pos(session, "A", "x", qty=10)
    rec = Reconciler(session, _kis_mock({}))
    r = rec.verify()
    assert not r.ok
    assert r.diffs[0] == ("A", 0, 10)
