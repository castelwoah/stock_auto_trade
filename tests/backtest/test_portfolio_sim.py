from datetime import datetime

import pytest

from src.backtest.portfolio_sim import (
    DEFAULT_FEE_PCT,
    DEFAULT_SLIPPAGE_PCT,
    DEFAULT_TAX_PCT,
    PortfolioSim,
    SimFill,
    SimPosition,
)
from src.strategy.base import Action, Signal


def _sig(action, symbol="A", strategy_id="news_swing", notional=100_000, qty=0, signal_id=None, reason=""):
    return Signal(
        action=action, symbol=symbol, strategy_id=strategy_id,
        notional=notional, qty=qty, signal_id=signal_id, reason=reason,
    )


def _ts(y=2026, m=4, d=19, h=10, mm=0):
    return datetime(y, m, d, h, mm, 0)


# --- construction -----------------------------------------------------------

def test_initial_cash_positive_required():
    with pytest.raises(ValueError):
        PortfolioSim(0)
    with pytest.raises(ValueError):
        PortfolioSim(-1)


def test_defaults_match_spec():
    # Total round-trip cost ≈ 1.03% (slippage×2 + fee×2 + tax)
    assert DEFAULT_SLIPPAGE_PCT == 0.25
    assert DEFAULT_FEE_PCT == 0.15
    assert DEFAULT_TAX_PCT == 0.23


# --- BUY --------------------------------------------------------------------

def test_buy_fills_with_slippage_and_fee():
    sim = PortfolioSim(1_000_000)
    fill = sim.fill(
        _sig(Action.BUY, notional=100_000),
        ts=_ts(), bar_price=1000.0, idempotency_key="k1",
    )
    assert fill is not None
    # price = 1000 * 1.0025 = 1002.5
    assert fill.price == pytest.approx(1002.5)
    # qty = floor(100000 / 1002.5) = 99
    assert fill.qty == 99
    # gross = 99 * 1002.5 = 99247.5; fee = 148.87125
    assert fill.fee == pytest.approx(99 * 1002.5 * 0.0015)
    assert fill.tax == 0.0
    # cash = 1_000_000 - (99247.5 + fee)
    assert sim.cash == pytest.approx(1_000_000 - (99 * 1002.5 + fill.fee))
    assert ("A", "news_swing") in sim.positions


def test_buy_rejected_when_cash_insufficient():
    sim = PortfolioSim(1000)
    fill = sim.fill(
        _sig(Action.BUY, notional=100_000),
        ts=_ts(), bar_price=1000.0, idempotency_key="k",
    )
    assert fill is None
    assert len(sim.skips) == 1
    assert sim.skips[0].reason == "insufficient_cash"


def test_buy_idempotency():
    sim = PortfolioSim(1_000_000)
    f1 = sim.fill(_sig(Action.BUY), ts=_ts(), bar_price=100.0, idempotency_key="dup")
    f2 = sim.fill(_sig(Action.BUY), ts=_ts(), bar_price=100.0, idempotency_key="dup")
    assert f1 is not None
    assert f2 is None  # 중복 차단
    assert len(sim.fills) == 1


def test_buy_qty_zero_skipped():
    sim = PortfolioSim(1_000_000)
    fill = sim.fill(
        _sig(Action.BUY, notional=10),  # price 1000 → qty 0
        ts=_ts(), bar_price=1000.0, idempotency_key="k",
    )
    assert fill is None
    assert sim.skips[0].reason == "qty_zero"


def test_buy_nonpositive_price_skipped():
    sim = PortfolioSim(1_000_000)
    fill = sim.fill(_sig(Action.BUY), ts=_ts(), bar_price=0.0, idempotency_key="k")
    assert fill is None


def test_buy_averages_on_additional_purchase():
    sim = PortfolioSim(10_000_000)
    sim.fill(_sig(Action.BUY, notional=1_000_000), ts=_ts(), bar_price=1000.0, idempotency_key="k1")
    sim.fill(_sig(Action.BUY, notional=1_000_000), ts=_ts(h=11), bar_price=1100.0, idempotency_key="k2")
    pos = sim.positions[("A", "news_swing")]
    # 두 체결의 가중평균 평단가
    assert pos.qty == pytest.approx(int(1_000_000 // (1000 * 1.0025)) + int(1_000_000 // (1100 * 1.0025)))
    assert 1000 < pos.avg_price < 1110


# --- SELL -------------------------------------------------------------------

def test_sell_realizes_pnl_with_tax():
    sim = PortfolioSim(1_000_000)
    sim.fill(_sig(Action.BUY, notional=500_000), ts=_ts(), bar_price=1000.0, idempotency_key="b")
    pos = sim.positions[("A", "news_swing")]
    qty_held = pos.qty
    # 매도 — 10% 상승
    sell = sim.fill(
        _sig(Action.SELL, qty=qty_held), ts=_ts(h=14), bar_price=1100.0, idempotency_key="s",
    )
    assert sell is not None
    assert sell.tax > 0  # 거래세 부과
    assert sell.fee > 0
    assert sell.pnl_krw is not None
    assert sell.pnl_pct is not None and sell.pnl_pct > 5  # 10% - (수수료·거래세·슬리피지)
    assert sim.realized_pnl == pytest.approx(sell.pnl_krw)
    assert ("A", "news_swing") not in sim.positions


def test_sell_no_position_skipped():
    sim = PortfolioSim(1_000_000)
    fill = sim.fill(
        _sig(Action.SELL, qty=10), ts=_ts(), bar_price=1000.0, idempotency_key="k",
    )
    assert fill is None
    assert sim.skips[0].reason == "no_position"


def test_sell_partial_when_qty_exceeds_holding():
    sim = PortfolioSim(1_000_000)
    sim.fill(_sig(Action.BUY, notional=500_000), ts=_ts(), bar_price=1000.0, idempotency_key="b")
    held = sim.positions[("A", "news_swing")].qty
    sell = sim.fill(
        _sig(Action.SELL, qty=held + 999), ts=_ts(h=14), bar_price=1100.0, idempotency_key="s",
    )
    assert sell is not None
    assert sell.qty == held  # 보유 수량까지만


def test_sell_stop_loss_flag_detected():
    sim = PortfolioSim(1_000_000)
    sim.fill(_sig(Action.BUY, notional=500_000), ts=_ts(), bar_price=1000.0, idempotency_key="b")
    held = sim.positions[("A", "news_swing")].qty
    sell = sim.fill(
        _sig(Action.SELL, qty=held, reason="stop_loss: -5.2%"),
        ts=_ts(h=14), bar_price=940.0, idempotency_key="s",
    )
    assert sell is not None
    assert sell.was_stop_loss is True


# --- Valuation --------------------------------------------------------------

def test_mark_to_market_updates_equity_curve():
    sim = PortfolioSim(1_000_000)
    sim.fill(_sig(Action.BUY, notional=500_000), ts=_ts(), bar_price=1000.0, idempotency_key="b")
    eq = sim.mark_to_market({"A": 1100.0}, ts=_ts(h=15, mm=30))
    assert len(sim.equity_curve) == 1
    assert eq > 1_000_000  # 평가이익 반영


def test_mark_to_market_updates_highest_price():
    sim = PortfolioSim(1_000_000)
    sim.fill(_sig(Action.BUY, notional=500_000), ts=_ts(), bar_price=1000.0, idempotency_key="b")
    sim.mark_to_market({"A": 1200.0}, ts=_ts(h=11))
    sim.mark_to_market({"A": 1100.0}, ts=_ts(h=12))
    pos = sim.positions[("A", "news_swing")]
    assert pos.highest_price == pytest.approx(1200.0)


def test_equity_without_prices_uses_avg():
    sim = PortfolioSim(1_000_000)
    sim.fill(_sig(Action.BUY, notional=500_000), ts=_ts(), bar_price=1000.0, idempotency_key="b")
    assert sim.equity() == pytest.approx(sim.cash + sim.positions[("A", "news_swing")].qty * sim.positions[("A", "news_swing")].avg_price)


# --- Strategy interop -------------------------------------------------------

def test_current_positions_as_state_converts_shape():
    sim = PortfolioSim(1_000_000)
    sim.fill(_sig(Action.BUY, notional=500_000), ts=_ts(), bar_price=1000.0, idempotency_key="b")
    states = sim.current_positions_as_state(prices={"A": 1100.0})
    assert "A" in states
    ps = states["A"]
    assert ps.strategy_id == "news_swing"
    assert ps.current_price == 1100.0
    assert ps.highest_price is not None


def test_update_tp_armed():
    sim = PortfolioSim(1_000_000)
    sim.fill(_sig(Action.BUY, notional=500_000), ts=_ts(), bar_price=1000.0, idempotency_key="b")
    assert sim.positions[("A", "news_swing")].tp_armed is False
    sim.update_tp_armed("A", "news_swing", armed=True)
    assert sim.positions[("A", "news_swing")].tp_armed is True


def test_round_trip_cost_approx_one_percent():
    """매수 + 10% 상승 매도 → 비용 차감 후 순수익 약 9% 근처."""
    sim = PortfolioSim(10_000_000)
    sim.fill(_sig(Action.BUY, notional=1_000_000), ts=_ts(), bar_price=1000.0, idempotency_key="b")
    held = sim.positions[("A", "news_swing")].qty
    sell = sim.fill(
        _sig(Action.SELL, qty=held), ts=_ts(h=14), bar_price=1100.0, idempotency_key="s",
    )
    # 이론 왕복 비용 ≈ slip(0.25×2) + fee(0.15×2) + tax(0.23) = 1.03%
    # 10% 상승 → 실 수익률 약 8.97%. 허용 오차 ±0.5%.
    assert 8.3 < sell.pnl_pct < 9.5
