from dataclasses import dataclass
from datetime import date, datetime, time

import pytest

from src.reporting.pnl_calc import (
    compute_realized_pnl,
    filter_by_time_window,
)


@dataclass
class _T:  # minimal Trade stub matching required attributes
    symbol: str
    strategy_id: str
    side: str
    qty: int
    price: float
    ts: datetime
    status: str = "filled"


def _ts(h, m=0, s=0, d=20):
    return datetime(2026, 4, d, h, m, s)


# --- basic matching ---------------------------------------------------------

def test_empty_input_returns_zero():
    s = compute_realized_pnl([])
    assert s.total_realized_pnl_krw == 0
    assert s.fills == []


def test_simple_buy_sell_profit():
    trades = [
        _T("A", "x", "BUY", 10, 1000, _ts(10)),
        _T("A", "x", "SELL", 10, 1100, _ts(14)),
    ]
    s = compute_realized_pnl(trades)
    assert len(s.fills) == 1
    f = s.fills[0]
    # gross pnl = 10 * (1100 - 1000) = 1000
    # sell fee 10*1100*0.0015 = 16.5
    # sell tax 10*1100*0.0023 = 25.3
    # buy fee  10*1000*0.0015 = 15.0
    # net pnl = 1000 - 16.5 - 25.3 - 15.0 = 943.2
    assert f.pnl_krw == pytest.approx(943.2, abs=0.01)
    assert f.pnl_pct > 9.0 and f.pnl_pct < 10.0


def test_partial_sell():
    trades = [
        _T("A", "x", "BUY", 10, 1000, _ts(10)),
        _T("A", "x", "SELL", 4, 1100, _ts(14)),
    ]
    s = compute_realized_pnl(trades)
    assert len(s.fills) == 1
    assert s.fills[0].qty == 4


def test_fifo_two_buys_one_sell():
    trades = [
        _T("A", "x", "BUY", 5, 1000, _ts(10)),
        _T("A", "x", "BUY", 5, 1200, _ts(11)),
        _T("A", "x", "SELL", 8, 1300, _ts(14)),
    ]
    s = compute_realized_pnl(trades)
    assert len(s.fills) == 1
    # 5 @ 1000 + 3 @ 1200 = 5000 + 3600 = 8600 for 8 units → avg 1075
    assert s.fills[0].buy_price == pytest.approx(1075.0)


def test_strategy_isolation():
    trades = [
        _T("A", "x", "BUY", 10, 1000, _ts(10)),
        _T("A", "y", "SELL", 10, 1100, _ts(14)),  # 다른 전략 매도 → 매칭 불가
    ]
    s = compute_realized_pnl(trades)
    assert s.fills == []
    assert s.unmatched_sells == 1


def test_unmatched_sell_counts():
    trades = [
        _T("A", "x", "SELL", 5, 1000, _ts(14)),  # 선행 BUY 없음
    ]
    s = compute_realized_pnl(trades)
    assert s.fills == []
    assert s.unmatched_sells == 1


def test_ignores_non_filled():
    trades = [
        _T("A", "x", "BUY", 10, 1000, _ts(10), status="rejected"),
        _T("A", "x", "SELL", 10, 1100, _ts(14), status="cancelled_timeout"),
    ]
    s = compute_realized_pnl(trades)
    assert s.fills == []


def test_loss_scenario():
    trades = [
        _T("A", "x", "BUY", 10, 1000, _ts(10)),
        _T("A", "x", "SELL", 10, 900, _ts(14)),
    ]
    s = compute_realized_pnl(trades)
    assert s.fills[0].pnl_krw < 0
    assert s.loss_count == 1
    assert s.win_count == 0


def test_win_loss_counts():
    trades = [
        _T("A", "x", "BUY", 10, 1000, _ts(10)),
        _T("A", "x", "SELL", 10, 1100, _ts(14)),
        _T("B", "x", "BUY", 5, 500, _ts(10, 30)),
        _T("B", "x", "SELL", 5, 450, _ts(14, 30)),
    ]
    s = compute_realized_pnl(trades)
    assert s.win_count == 1
    assert s.loss_count == 1


def test_tp_sl_classification():
    trades = [
        _T("A", "x", "BUY", 10, 1000, _ts(10)),
        _T("A", "x", "SELL", 10, 1100, _ts(14)),
        _T("B", "x", "BUY", 5, 500, _ts(10, 30)),
        _T("B", "x", "SELL", 5, 450, _ts(14, 30)),
    ]
    s = compute_realized_pnl(trades)
    assert len(s.take_profit_fills) == 1
    assert len(s.stop_loss_fills) == 1


def test_total_sum():
    trades = [
        _T("A", "x", "BUY", 10, 1000, _ts(10)),
        _T("A", "x", "SELL", 10, 1100, _ts(14)),
        _T("B", "x", "BUY", 5, 500, _ts(10, 30)),
        _T("B", "x", "SELL", 5, 450, _ts(14, 30)),
    ]
    s = compute_realized_pnl(trades)
    assert s.total_realized_pnl_krw == pytest.approx(
        sum(f.pnl_krw for f in s.fills), abs=0.01
    )


# --- time filter -----------------------------------------------------------

def test_filter_by_time_window():
    trades = [
        _T("A", "x", "BUY", 1, 100, _ts(8, 0)),    # 이른 시각 → 제외
        _T("A", "x", "BUY", 1, 100, _ts(10, 0)),   # 포함
        _T("A", "x", "SELL", 1, 110, _ts(14, 0)),  # 포함
        _T("A", "x", "SELL", 1, 110, _ts(16, 0)),  # 늦은 시각 → 제외
    ]
    out = filter_by_time_window(trades, day=date(2026, 4, 20))
    assert len(out) == 2


def test_filter_by_different_date():
    trades = [
        _T("A", "x", "BUY", 1, 100, datetime(2026, 4, 19, 10, 0)),
        _T("A", "x", "BUY", 1, 100, datetime(2026, 4, 20, 10, 0)),
    ]
    out = filter_by_time_window(trades, day=date(2026, 4, 20))
    assert len(out) == 1
