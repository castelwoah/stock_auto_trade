"""Tests for src.backtest.metrics.compute_metrics."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.backtest.metrics import BacktestMetrics, compute_metrics
from src.backtest.portfolio_sim import SimFill


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "metrics_snapshot.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _buy(ts: datetime, symbol: str, qty: int = 1, strategy_id: str = "S",
         price: float = 100.0, key: str | None = None) -> SimFill:
    return SimFill(
        ts=ts, symbol=symbol, side="BUY", qty=qty, price=price,
        fee=0.0, tax=0.0, strategy_id=strategy_id,
        idempotency_key=key or f"{symbol}-B-{ts.isoformat()}",
    )


def _sell(ts: datetime, symbol: str, qty: int = 1, strategy_id: str = "S",
          price: float = 100.0, pnl_krw: float = 0.0,
          key: str | None = None) -> SimFill:
    return SimFill(
        ts=ts, symbol=symbol, side="SELL", qty=qty, price=price,
        fee=0.0, tax=0.0, strategy_id=strategy_id,
        idempotency_key=key or f"{symbol}-S-{ts.isoformat()}",
        pnl_krw=pnl_krw,
        pnl_pct=0.0,
    )


def _daily_curve(values: list[float], start: datetime | None = None
                 ) -> list[tuple[datetime, float]]:
    start = start or datetime(2026, 1, 5, 15, 30)
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_equity_curve_returns_all_zeros():
    m = compute_metrics(equity_curve=[], fills=[], initial_cash=1_000_000.0)
    assert m.total_return_pct == 0.0
    assert m.cagr_pct == 0.0
    assert m.sharpe_annualized == 0.0
    assert m.max_drawdown_pct == 0.0
    assert m.win_rate == 0.0
    assert m.profit_factor == 0.0
    assert m.avg_win_krw == 0.0
    assert m.avg_loss_krw == 0.0
    assert m.trade_count == 0
    assert m.avg_holding_days == 0.0
    assert m.alpha_vs_benchmark_pct is None


def test_monotonic_rising_curve_positive_return_zero_mdd():
    curve = _daily_curve([1_000_000, 1_010_000, 1_020_000, 1_030_000, 1_040_000,
                          1_050_000, 1_060_000, 1_070_000, 1_080_000, 1_090_000])
    m = compute_metrics(equity_curve=curve, fills=[], initial_cash=1_000_000.0)
    assert m.total_return_pct == pytest.approx(9.0, abs=1e-9)
    assert m.max_drawdown_pct == 0.0
    assert m.sharpe_annualized > 0.0
    assert m.cagr_pct > 0.0


def test_monotonic_falling_curve_negative_return_negative_mdd():
    curve = _daily_curve([1_000_000, 990_000, 980_000, 970_000, 960_000, 950_000])
    m = compute_metrics(equity_curve=curve, fills=[], initial_cash=1_000_000.0)
    assert m.total_return_pct == pytest.approx(-5.0, abs=1e-9)
    assert m.max_drawdown_pct == pytest.approx(-5.0, abs=1e-9)
    assert m.sharpe_annualized < 0.0


def test_v_shape_recovery_mdd_exact():
    # peak 1,100,000 -> trough 880,000 -> recover 1,050,000
    curve = _daily_curve([1_000_000, 1_100_000, 880_000, 1_050_000])
    m = compute_metrics(equity_curve=curve, fills=[], initial_cash=1_000_000.0)
    # drawdown = (880,000 - 1,100,000) / 1,100,000 = -20.0%
    assert m.max_drawdown_pct == pytest.approx(-20.0, abs=1e-9)


def test_win_rate_three_wins_two_losses():
    t0 = datetime(2026, 1, 5, 10, 0)
    fills = [
        _sell(t0, "A", pnl_krw=100.0, key="s1"),
        _sell(t0, "B", pnl_krw=200.0, key="s2"),
        _sell(t0, "C", pnl_krw=150.0, key="s3"),
        _sell(t0, "D", pnl_krw=-50.0, key="s4"),
        _sell(t0, "E", pnl_krw=-80.0, key="s5"),
    ]
    curve = _daily_curve([1_000_000, 1_001_000])
    m = compute_metrics(
        equity_curve=curve, fills=fills, initial_cash=1_000_000.0
    )
    assert m.win_rate == pytest.approx(0.6, abs=1e-9)
    assert m.trade_count == 5


def test_profit_factor_basic():
    t0 = datetime(2026, 1, 5, 10, 0)
    fills = [
        _sell(t0, "A", pnl_krw=1000.0, key="s1"),
        _sell(t0, "B", pnl_krw=-500.0, key="s2"),
    ]
    curve = _daily_curve([1_000_000, 1_000_500])
    m = compute_metrics(
        equity_curve=curve, fills=fills, initial_cash=1_000_000.0
    )
    assert m.profit_factor == pytest.approx(2.0, abs=1e-9)


def test_profit_factor_no_losses_returns_sentinel():
    t0 = datetime(2026, 1, 5, 10, 0)
    fills = [
        _sell(t0, "A", pnl_krw=100.0, key="s1"),
        _sell(t0, "B", pnl_krw=200.0, key="s2"),
    ]
    curve = _daily_curve([1_000_000, 1_000_300])
    m = compute_metrics(
        equity_curve=curve, fills=fills, initial_cash=1_000_000.0
    )
    assert m.profit_factor == pytest.approx(999.0, abs=1e-9)


def test_alpha_vs_benchmark():
    # Strategy: +10%, Benchmark: +5% -> alpha = 5.0%
    strat_curve = _daily_curve([1_000_000, 1_100_000])
    bench_curve = _daily_curve([100.0, 105.0])
    m = compute_metrics(
        equity_curve=strat_curve, fills=[], initial_cash=1_000_000.0,
        benchmark_curve=bench_curve,
    )
    assert m.total_return_pct == pytest.approx(10.0, abs=1e-9)
    assert m.alpha_vs_benchmark_pct == pytest.approx(5.0, abs=1e-9)


def test_avg_holding_days_fifo():
    # Pair 1: 3 days. Pair 2: 5 days. Average = 4.0.
    t_buy1 = datetime(2026, 1, 5, 10, 0)
    t_sell1 = datetime(2026, 1, 8, 14, 0)
    t_buy2 = datetime(2026, 1, 7, 10, 0)
    t_sell2 = datetime(2026, 1, 12, 14, 0)
    fills = [
        _buy(t_buy1, "AAA", qty=1, strategy_id="S", key="b1"),
        _buy(t_buy2, "BBB", qty=1, strategy_id="S", key="b2"),
        _sell(t_sell1, "AAA", qty=1, strategy_id="S", pnl_krw=100.0, key="s1"),
        _sell(t_sell2, "BBB", qty=1, strategy_id="S", pnl_krw=-50.0, key="s2"),
    ]
    curve = _daily_curve([1_000_000, 1_000_050])
    m = compute_metrics(
        equity_curve=curve, fills=fills, initial_cash=1_000_000.0
    )
    assert m.avg_holding_days == pytest.approx(4.0, abs=1e-9)


def test_snapshot_fixture_exact_match():
    data = json.loads(FIXTURE_PATH.read_text())
    equity_curve = [
        (datetime.fromisoformat(ts), float(v))
        for ts, v in data["equity_curve"]
    ]
    fills = [
        SimFill(
            ts=datetime.fromisoformat(f["ts"]),
            symbol=f["symbol"],
            side=f["side"],
            qty=int(f["qty"]),
            price=float(f["price"]),
            fee=float(f["fee"]),
            tax=float(f["tax"]),
            strategy_id=f["strategy_id"],
            idempotency_key=f["idempotency_key"],
            pnl_krw=f.get("pnl_krw"),
        )
        for f in data["fills"]
    ]
    expected = data["expected"]
    m = compute_metrics(
        equity_curve=equity_curve,
        fills=fills,
        initial_cash=float(data["initial_cash"]),
    )
    assert m.total_return_pct == pytest.approx(expected["total_return_pct"], abs=1e-6)
    assert m.max_drawdown_pct == pytest.approx(expected["max_drawdown_pct"], abs=1e-6)
    assert m.win_rate == pytest.approx(expected["win_rate"], abs=1e-6)
    assert m.profit_factor == pytest.approx(expected["profit_factor"], abs=1e-6)
    assert m.avg_win_krw == pytest.approx(expected["avg_win_krw"], abs=1e-6)
    assert m.avg_loss_krw == pytest.approx(expected["avg_loss_krw"], abs=1e-6)
    assert m.trade_count == expected["trade_count"]
    assert m.avg_holding_days == pytest.approx(expected["avg_holding_days"], abs=1e-6)
    assert m.alpha_vs_benchmark_pct == expected["alpha_vs_benchmark_pct"]
    # Sanity sanity: cagr + sharpe are finite
    assert math.isfinite(m.cagr_pct)
    assert math.isfinite(m.sharpe_annualized)


def test_same_day_multiple_snapshots_last_wins():
    # Two snapshots on same date -> only the last is used for daily return.
    curve = [
        (datetime(2026, 1, 5, 10, 0), 1_000_000.0),
        (datetime(2026, 1, 5, 15, 30), 1_010_000.0),  # same day, last wins
        (datetime(2026, 1, 6, 15, 30), 1_020_000.0),
    ]
    m = compute_metrics(equity_curve=curve, fills=[], initial_cash=1_000_000.0)
    # total return uses raw final
    assert m.total_return_pct == pytest.approx(2.0, abs=1e-9)
