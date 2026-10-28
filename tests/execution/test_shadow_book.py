"""ShadowBook — 섀도 Trade 재생 · 고점 · 가상 계좌 · 하루 요약."""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.execution.shadow_book import ShadowBook, sell_reason
from src.persistence.models import Base, Trade

DAY = date(2026, 10, 29)
_n = iter(range(1, 10_000))


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _t(session, side, qty, price, *, sym="A", hm=(10, 0), day=DAY, status="shadow", note=None, sid="breakout"):
    session.add(Trade(ts=datetime.combine(day, datetime.min.time()).replace(hour=hm[0], minute=hm[1]),
                      symbol=sym, side=side, qty=qty, price=price, strategy_id=sid,
                      idempotency_key=f"k{next(_n)}", status=status, note=note))
    session.flush()


def _book(session):
    return ShadowBook(session, since=datetime(2026, 10, 29))


def test_buys_average_and_partial_sell(session):
    _t(session, "BUY", 10, 1000, hm=(9, 30))
    _t(session, "BUY", 10, 1200, hm=(9, 35))
    _t(session, "SELL", 5, 1100, hm=(9, 40))
    (p,) = _book(session).open_positions().values()
    assert (p.qty, p.avg_price, p.opened_at.hour, p.opened_at.minute) == (15, 1100.0, 9, 30)


def test_full_sell_closes_and_ignores_other_rows(session):
    _t(session, "BUY", 10, 1000)
    _t(session, "SELL", 10, 1000, hm=(10, 5))
    _t(session, "BUY", 3, 500, sym="B", status="filled")              # 실주문 기록 → 무시
    _t(session, "BUY", 3, 500, sym="C", day=date(2026, 10, 28))       # 시작일 이전 → 무시
    _t(session, "SELL", 1, 500, sym="D")                              # 보유 없는 매도 → 무시
    assert _book(session).open_positions() == {}


def test_peaks_tp_armed_and_restart(session):
    _t(session, "BUY", 10, 1000)
    book = _book(session)
    book.update_peaks({"A": 1080})
    book.update_peaks({"A": 1050})
    st = book.positions_as_state({"A": 1050})["A"]
    assert st.highest_price == 1080 and st.tp_armed and st.current_price == 1050
    restarted = _book(session)                                        # 메모리 고점 없음
    st2 = restarted.positions_as_state({"A": 1050})["A"]
    assert st2.highest_price == 1050 and not st2.tp_armed


def test_peaks_dropped_after_close(session):
    _t(session, "BUY", 10, 1000)
    book = _book(session)
    book.update_peaks({"A": 1100})
    _t(session, "SELL", 10, 1100, hm=(10, 5))
    book.update_peaks({"A": 1100})
    assert book._peaks == {}


def test_account_cash_equity_with_costs(session):
    _t(session, "BUY", 100, 1000, hm=(9, 30))
    _t(session, "SELL", 100, 1100, hm=(10, 0), note="trailing_stop: x")
    _t(session, "BUY", 10, 2000, sym="B", hm=(10, 30))
    cash, equity = _book(session).account(1_000_000, DAY, {"B": 2100})
    # 실현 = 10,000 − 매도 수수료 165 − 세금 253 − 매수 수수료 150 = 9,432
    assert cash == pytest.approx(1_000_000 - 20_000 + 9_432)
    assert equity == pytest.approx(cash + 21_000)


def test_realized_uses_earlier_day_buy(session):
    _t(session, "BUY", 10, 1000, day=date(2026, 10, 29))
    _t(session, "SELL", 10, 1000, day=date(2026, 10, 30), note="eod_liquidation")
    book = _book(session)
    assert len(book.realized(date(2026, 10, 30))) == 1 and book.realized(DAY) == []


def test_summary_counts_and_leftover(session):
    _t(session, "BUY", 10, 1000, hm=(9, 30))
    _t(session, "SELL", 10, 950, hm=(10, 0), note="stop_loss: -5.0%")
    _t(session, "BUY", 5, 2000, sym="B", hm=(10, 30))
    book = _book(session)
    s = book.summary(DAY, {"B": 2100}, as_of=datetime(2026, 10, 29, 12), names={"B": "비"})
    assert (s.buys, s.sells, s.sell_count) == (2, {"손절": 1}, 1)
    assert s.realized_pnl_krw < -500 and s.open_after_eod == []
    (pos,) = s.positions
    assert (pos.name, pos.qty, round(pos.unrealized_pnl_pct, 1)) == ("비", 5, 5.0)
    late = book.summary(DAY, {"B": 2100}, as_of=datetime(2026, 10, 29, 16), names={"B": "비"})
    assert late.open_after_eod == ["비 5주"]


def test_leftover_only_for_eod_strategies(session):
    _t(session, "BUY", 5, 2000, sid="news_swing")
    s = _book(session).summary(DAY, {}, as_of=datetime(2026, 10, 29, 16))
    assert s.open_after_eod == [] and len(s.positions) == 1


@pytest.mark.parametrize("note,label", [
    ("eod_liquidation", "EOD"), ("stop_loss: -5%", "손절"), ("trailing_stop: x", "Trailing"),
    ("event: halted", "이벤트"), (None, "기타"),
])
def test_sell_reason(note, label):
    assert sell_reason(note) == label
