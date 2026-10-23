from datetime import date, datetime

from src.strategy.base import Action, PositionState, StrategyContext
from src.strategy.breakout import (
    BREAKOUT_K,
    BreakoutStrategy,
    compute_target_price,
)


def _ctx(**over):
    base = dict(
        as_of=datetime(2026, 4, 19, 10, 0, 0),
        today=date(2026, 4, 19),
        cash_available=3_000_000,
        positions={},
        candidate_scores={},
        current_prices={"A": 105.0, "B": 210.0, "C": 95.0},
        prev_day_ohlc={
            "A": {"open_today": 100.0, "high": 103.0, "low": 97.0},  # target = 100 + 6*0.5 = 103
            "B": {"open_today": 200.0, "high": 208.0, "low": 192.0}, # target = 200 + 16*0.5 = 208
            "C": {"open_today": 100.0, "high": 105.0, "low": 95.0},  # target = 100 + 10*0.5 = 105 (not broken: 95<105)
        },
    )
    base.update(over)
    return StrategyContext(**base)


def test_target_price_formula():
    assert compute_target_price(100, 103, 97) == 103.0
    assert compute_target_price(100, 103, 97, k=0.7) == 100 + 6 * 0.7


def test_entry_on_broken_target():
    sigs = BreakoutStrategy().decide_entries(_ctx())
    syms = {s.symbol for s in sigs}
    assert "A" in syms and "B" in syms
    assert "C" not in syms  # price not above target
    for s in sigs:
        assert s.action == Action.BUY
        assert s.strategy_id == "breakout"
        assert "breakout" in s.signal_id  # naming convention


def test_entry_respects_max_positions():
    strat = BreakoutStrategy(max_positions=1)
    sigs = strat.decide_entries(_ctx())
    assert len(sigs) == 1


def test_entry_blocked_near_close():
    ctx = _ctx(as_of=datetime(2026, 4, 19, 15, 0, 0))  # exact 15:00 → blocked
    assert BreakoutStrategy().decide_entries(ctx) == []


def test_entry_skips_owned():
    owned = PositionState(
        symbol="A", strategy_id="breakout", qty=10, avg_price=100, current_price=105
    )
    ctx = _ctx(positions={"A": owned})
    sigs = BreakoutStrategy().decide_entries(ctx)
    assert all(s.symbol != "A" for s in sigs)


def test_entry_zero_cash():
    ctx = _ctx(cash_available=0)
    assert BreakoutStrategy().decide_entries(ctx) == []


def test_entry_missing_ohlc_skipped():
    ctx = _ctx(prev_day_ohlc={})
    assert BreakoutStrategy().decide_entries(ctx) == []


def test_entry_invalid_ohlc_skipped():
    ctx = _ctx(prev_day_ohlc={"A": {"open_today": 0, "high": 0, "low": 0}})
    assert BreakoutStrategy().decide_entries(ctx) == []


def test_exit_eod_liquidation():
    owned = PositionState(
        symbol="A", strategy_id="breakout", qty=10, avg_price=100, current_price=101
    )
    ctx = _ctx(
        as_of=datetime(2026, 4, 19, 15, 15, 0),
        positions={"A": owned},
    )
    sigs = BreakoutStrategy().decide_exits(ctx)
    assert len(sigs) == 1
    assert sigs[0].action == Action.SELL
    assert sigs[0].reason == "eod_liquidation"


def test_exit_stop_loss_during_day():
    owned = PositionState(
        symbol="A", strategy_id="breakout", qty=10, avg_price=100, current_price=93
    )
    ctx = _ctx(positions={"A": owned})
    sigs = BreakoutStrategy().decide_exits(ctx)
    assert len(sigs) == 1
    assert "stop_loss" in sigs[0].reason


def test_exit_ignores_other_strategy():
    # 다른 전략 포지션은 breakout 이 청산하지 않아야
    foreign = PositionState(
        symbol="A", strategy_id="news_swing", qty=10, avg_price=100, current_price=93
    )
    ctx = _ctx(positions={"A": foreign})
    assert BreakoutStrategy().decide_exits(ctx) == []


def test_breakout_uses_default_k():
    assert BreakoutStrategy().k == BREAKOUT_K


# --- news-universe-v1: 매수 순서 · 악재 차단 · 근거 기록 ---------------------------

def test_slots_filled_by_breakout_ratio_without_news():
    # A 돌파율 (105-103)/103 ≈ 1.9% > B (210-208)/208 ≈ 1.0%
    sigs = BreakoutStrategy(max_positions=1).decide_entries(_ctx())
    assert [s.symbol for s in sigs] == ["A"]


def test_news_score_ranks_before_breakout_ratio():
    ctx = _ctx(extras={"news_scores": {"B": 80.0}, "news_articles": {"B": (11, 12, 13, 14)}})
    sigs = BreakoutStrategy(max_positions=1).decide_entries(ctx)
    assert [s.symbol for s in sigs] == ["B"]
    assert "news 80" in sigs[0].reason and "art 12,13,14" in sigs[0].reason
    assert sigs[0].meta == {"news_score": 80.0}


def test_entry_blocked_symbols_skipped():
    sigs = BreakoutStrategy().decide_entries(_ctx(extras={"entry_blocked": frozenset({"A"})}))
    assert {s.symbol for s in sigs} == {"B"}
