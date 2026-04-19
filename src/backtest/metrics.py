"""Backtest performance metrics (stdlib only).

Computes total return, CAGR, Sharpe (annualized), max drawdown, win rate,
profit factor, win/loss averages, trade count, average holding days, and
alpha vs a benchmark curve.

Inputs come straight from ``PortfolioSim``:
  * ``equity_curve``: list[tuple[datetime, float]]
  * ``fills``: list[SimFill]  (SELL rows carry realized ``pnl_krw``)
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, date
from typing import Iterable

from src.backtest.portfolio_sim import SimFill


# Sentinel used when losses == 0 and we'd otherwise divide by zero.
# 999.0 is JSON-safe (math.inf is not).
_PROFIT_FACTOR_INF = 999.0


@dataclass(frozen=True)
class BacktestMetrics:
    total_return_pct: float
    cagr_pct: float
    sharpe_annualized: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    avg_win_krw: float
    avg_loss_krw: float
    trade_count: int
    avg_holding_days: float
    alpha_vs_benchmark_pct: float | None = None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _daily_closes(
    equity_curve: list[tuple[datetime, float]],
) -> list[tuple[date, float]]:
    """Compress the equity curve to one value per calendar date (last wins)."""
    by_day: dict[date, tuple[datetime, float]] = {}
    for ts, value in equity_curve:
        d = ts.date()
        prev = by_day.get(d)
        if prev is None or ts >= prev[0]:
            by_day[d] = (ts, value)
    return [(d, by_day[d][1]) for d in sorted(by_day.keys())]


def _daily_returns(closes: list[tuple[date, float]]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1][1]
        cur = closes[i][1]
        if prev <= 0:
            continue
        out.append(cur / prev - 1.0)
    return out


def _max_drawdown_pct(equity_curve: list[tuple[datetime, float]]) -> float:
    peak = float("-inf")
    mdd = 0.0  # non-positive
    for _ts, v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak
            if dd < mdd:
                mdd = dd
    return mdd * 100.0


def _win_rate_and_pf(fills: Iterable[SimFill]) -> tuple[float, float, float, float, int]:
    """Return (win_rate, profit_factor, avg_win, avg_loss, trade_count)."""
    wins: list[float] = []
    losses: list[float] = []
    for f in fills:
        if f.side != "SELL" or f.pnl_krw is None:
            continue
        if f.pnl_krw > 0:
            wins.append(f.pnl_krw)
        elif f.pnl_krw < 0:
            losses.append(f.pnl_krw)
        else:
            # pnl exactly zero — counted as a closed trade but neither win nor loss.
            pass

    trade_count = sum(
        1 for f in fills if f.side == "SELL" and f.pnl_krw is not None
    )
    if trade_count == 0:
        return 0.0, 0.0, 0.0, 0.0, 0

    win_rate = len(wins) / trade_count
    sum_wins = sum(wins)
    sum_losses_abs = abs(sum(losses))
    if sum_losses_abs == 0:
        profit_factor = _PROFIT_FACTOR_INF if sum_wins > 0 else 0.0
    else:
        profit_factor = sum_wins / sum_losses_abs
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    return win_rate, profit_factor, avg_win, avg_loss, trade_count


def _avg_holding_days(fills: list[SimFill]) -> float:
    """FIFO-match BUY→SELL per (symbol, strategy_id); return avg holding days.

    Each SELL fill produces one holding-days sample, matched against the
    oldest outstanding BUY fill for that (symbol, strategy_id). Unmatched
    fills are excluded. Sells weight each matched pair equally regardless
    of share count.
    """
    from collections import defaultdict, deque

    buys: dict[tuple[str, str], "deque[list]"] = defaultdict(deque)
    # Each queued entry is [buy_fill, remaining_qty].
    holding: list[float] = []

    # Iterate in original order; fills are assumed chronological.
    for f in fills:
        key = (f.symbol, f.strategy_id)
        if f.side == "BUY":
            if f.qty > 0:
                buys[key].append([f, f.qty])
        elif f.side == "SELL":
            qty_left = f.qty
            matched_once = False
            while qty_left > 0 and buys[key]:
                head = buys[key][0]
                buy_fill, remaining = head
                if not matched_once:
                    # Record holding time once per SELL against the oldest BUY.
                    delta = (f.ts - buy_fill.ts).days
                    holding.append(float(delta))
                    matched_once = True
                take = min(remaining, qty_left)
                head[1] -= take
                qty_left -= take
                if head[1] <= 0:
                    buys[key].popleft()

    if not holding:
        return 0.0
    return statistics.fmean(holding)


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------

def compute_metrics(
    equity_curve: list[tuple[datetime, float]],
    fills: list[SimFill],
    *,
    initial_cash: float,
    benchmark_curve: list[tuple[datetime, float]] | None = None,
    trading_days_per_year: int = 252,
) -> BacktestMetrics:
    # --- empty curve short-circuit ---
    if not equity_curve:
        return BacktestMetrics(
            total_return_pct=0.0,
            cagr_pct=0.0,
            sharpe_annualized=0.0,
            max_drawdown_pct=0.0,
            win_rate=0.0,
            profit_factor=0.0,
            avg_win_krw=0.0,
            avg_loss_krw=0.0,
            trade_count=0,
            avg_holding_days=0.0,
            alpha_vs_benchmark_pct=None,
        )

    final_equity = equity_curve[-1][1]

    # --- total return ---
    if initial_cash > 0:
        total_return_pct = (final_equity / initial_cash - 1.0) * 100.0
    else:
        total_return_pct = 0.0

    # --- CAGR ---
    days_span = (equity_curve[-1][0] - equity_curve[0][0]).days
    if days_span < 1 or initial_cash <= 0 or final_equity <= 0:
        cagr_pct = 0.0
    else:
        cagr_pct = ((final_equity / initial_cash) ** (365.25 / days_span) - 1.0) * 100.0

    # --- Sharpe (annualized) ---
    closes = _daily_closes(equity_curve)
    returns = _daily_returns(closes)
    if len(returns) < 2:
        sharpe = 0.0
    else:
        std = statistics.stdev(returns)  # sample stdev
        if std < 1e-12:
            sharpe = 0.0
        else:
            sharpe = statistics.fmean(returns) / std * math.sqrt(trading_days_per_year)

    # --- Max drawdown ---
    mdd_pct = _max_drawdown_pct(equity_curve)

    # --- Trade stats ---
    win_rate, profit_factor, avg_win, avg_loss, trade_count = _win_rate_and_pf(fills)

    # --- Holding days ---
    avg_hold = _avg_holding_days(fills)

    # --- Alpha vs benchmark ---
    alpha: float | None = None
    if benchmark_curve:
        b_initial = benchmark_curve[0][1]
        b_final = benchmark_curve[-1][1]
        if b_initial > 0:
            bench_return_pct = (b_final / b_initial - 1.0) * 100.0
            alpha = total_return_pct - bench_return_pct

    return BacktestMetrics(
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        sharpe_annualized=sharpe,
        max_drawdown_pct=mdd_pct,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win_krw=avg_win,
        avg_loss_krw=avg_loss,
        trade_count=trade_count,
        avg_holding_days=avg_hold,
        alpha_vs_benchmark_pct=alpha,
    )
