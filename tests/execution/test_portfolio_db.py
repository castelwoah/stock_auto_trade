"""LivePortfolio DB-backed tests — in-memory SQLite."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.execution.portfolio import LivePortfolio, TradeRecord
from src.persistence.models import Base, Position, Trade


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def test_record_trade_idempotent(session):
    lp = LivePortfolio(session)
    rec = TradeRecord(
        symbol="A", side="BUY", qty=10, price=1000.0,
        strategy_id="news_swing", strategy_ver="v1",
        signal_id="s1", idempotency_key="k1", order_id="o1",
    )
    t1 = lp.record_trade(rec)
    t2 = lp.record_trade(rec)  # 같은 key → 중복 추가 X
    session.commit()
    count = session.query(Trade).count()
    assert count == 1
    assert t1.id == t2.id


def test_apply_fill_buy_creates_position(session):
    lp = LivePortfolio(session)
    pos = lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="BUY",
        qty=10, price=1000.0, ts=datetime(2026, 4, 19),
    )
    session.commit()
    assert pos.qty == 10
    assert pos.avg_price == 1000.0
    assert pos.highest_price == 1000.0


def test_apply_fill_buy_averages(session):
    lp = LivePortfolio(session)
    lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="BUY",
        qty=10, price=1000.0, ts=datetime(2026, 4, 19),
    )
    pos = lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="BUY",
        qty=10, price=1200.0, ts=datetime(2026, 4, 19),
    )
    session.commit()
    assert pos.qty == 20
    assert pos.avg_price == pytest.approx(1100.0)


def test_apply_fill_sell_reduces(session):
    lp = LivePortfolio(session)
    lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="BUY",
        qty=10, price=1000.0, ts=datetime(2026, 4, 19),
    )
    pos = lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="SELL",
        qty=5, price=1100.0, ts=datetime(2026, 4, 19),
    )
    assert pos.qty == 5


def test_apply_fill_sell_with_no_position_raises(session):
    lp = LivePortfolio(session)
    with pytest.raises(ValueError):
        lp.apply_fill_to_position(
            symbol="X", strategy_id="x", side="SELL",
            qty=5, price=100.0, ts=datetime.now(),
        )


def test_apply_fill_unknown_side_raises(session):
    lp = LivePortfolio(session)
    with pytest.raises(ValueError):
        lp.apply_fill_to_position(
            symbol="A", strategy_id="x", side="HOLD",
            qty=1, price=100.0, ts=datetime.now(),
        )


def test_update_highest_price_persists(session):
    lp = LivePortfolio(session)
    lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="BUY",
        qty=10, price=1000.0, ts=datetime(2026, 4, 19),
    )
    lp.update_highest_price("A", "x", new_price=1200.0)
    lp.update_highest_price("A", "x", new_price=1100.0)  # 하락은 무시
    pos = session.get(Position, ("A", "x"))
    assert pos.highest_price == pytest.approx(1200.0)


def test_positions_as_state_filters_zero_qty(session):
    lp = LivePortfolio(session)
    lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="BUY",
        qty=10, price=1000.0, ts=datetime(2026, 4, 19),
    )
    lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="SELL",
        qty=10, price=1100.0, ts=datetime(2026, 4, 19),
    )
    session.commit()
    states = lp.positions_as_state(current_prices={"A": 1100})
    assert "A" not in states


def test_equity_sums_cash_and_positions(session):
    lp = LivePortfolio(session)
    lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="BUY",
        qty=10, price=1000.0, ts=datetime(2026, 4, 19),
    )
    total = lp.equity(cash=500_000, prices={"A": 1200})
    assert total == pytest.approx(500_000 + 10 * 1200)


def test_mark_halt(session):
    lp = LivePortfolio(session)
    lp.apply_fill_to_position(
        symbol="A", strategy_id="x", side="BUY",
        qty=10, price=1000.0, ts=datetime(2026, 4, 19),
    )
    lp.mark_halt("A", "x")
    pos = session.get(Position, ("A", "x"))
    assert pos.halt_flag is True


# --- v4: 송신 게이트 · 복원 · Trailing 무장 유도 -------------------------------

def _rec(key, *, signal_id="s1", status="submitted"):
    return TradeRecord(
        symbol="A", side="BUY", qty=10, price=1000.0, strategy_id="news_swing",
        strategy_ver="v1", signal_id=signal_id, idempotency_key=key, order_id=None,
        status=status,
    )


def test_insert_trade_if_new_returns_none_on_duplicate(session):
    lp = LivePortfolio(session)
    assert lp.insert_trade_if_new(_rec("k1")) is not None
    assert lp.insert_trade_if_new(_rec("k1")) is None
    assert session.query(Trade).count() == 1


def test_insert_trade_if_new_absorbs_unique_race(session, monkeypatch):
    lp = LivePortfolio(session)
    lp.insert_trade_if_new(_rec("k1"))
    monkeypatch.setattr(lp, "get_trade", lambda key: None)   # 경합: 사전 조회는 통과
    assert lp.insert_trade_if_new(_rec("k1")) is None
    assert session.query(Trade).count() == 1


def test_executed_signal_ids_excludes_shadow(session):
    lp = LivePortfolio(session)
    lp.insert_trade_if_new(_rec("k1", signal_id="real-1"))
    lp.insert_trade_if_new(_rec("k2", signal_id="shadow-1", status="shadow"))
    lp.insert_trade_if_new(_rec("k3", signal_id=None))
    ids = lp.executed_signal_ids(datetime(2000, 1, 1), datetime(2999, 1, 1))
    assert ids == ["real-1"]


@pytest.mark.parametrize("peak,armed", [(None, False), (1000.0, False), (1069.0, False),
                                        (1070.0, True), (1200.0, True)])
def test_positions_as_state_derives_tp_armed(session, peak, armed):
    session.add(Position(symbol="A", strategy_id="breakout", qty=1, avg_price=1000.0,
                         highest_price=peak, updated_at=datetime(2026, 1, 1)))
    session.flush()
    st = LivePortfolio(session).positions_as_state(current_prices={"A": 1000.0})
    assert st["A"].tp_armed is armed


def test_trailing_stop_fires_from_persisted_peak(session):
    """재시작 후에도: 고점 1,100(+10%) 영속 → 현재가 1,060(고점 −3.6%) → trailing_stop."""
    from src.strategy.exit_signals import evaluate_exit

    session.add(Position(symbol="A", strategy_id="breakout", qty=1, avg_price=1000.0,
                         highest_price=1100.0, updated_at=datetime(2026, 1, 1)))
    session.flush()
    st = LivePortfolio(session).positions_as_state(current_prices={"A": 1060.0})["A"]
    assert evaluate_exit(st).reason == "trailing_stop"
