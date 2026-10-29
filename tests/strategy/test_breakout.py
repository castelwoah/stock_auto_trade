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


# --- 살 수 없는 종목 건너뛰기 (10-28) ---------------------------------------

def _priced_ctx(budget=None, **over):
    """오늘(10-28) 재현: 뉴스 점수 높은 비싼 3종목 + 싼 2종목이 모두 돌파."""
    prices = {"HYNIX": 1_371_000.0, "SAMSUNG": 230_500.0, "NAVER": 183_500.0,
              "CHEAP1": 8_930.0, "CHEAP2": 75_100.0}
    ohlc = {s: {"open_today": p * 0.95, "high": p, "low": p * 0.9} for s, p in prices.items()}   # target = 0.95p + 0.05p = p → +1 로 돌파
    extras = {"news_scores": {"HYNIX": 90.0, "SAMSUNG": 80.0, "NAVER": 70.0}}
    if budget is not None:
        extras["max_entry_notional"] = budget
    base = dict(cash_available=10_000_000, current_prices={s: p + 1 for s, p in prices.items()},
                prev_day_ohlc=ohlc, extras=extras)
    base.update(over)
    return _ctx(**base)


def test_unaffordable_top_ranked_skipped_and_slots_refilled():
    sigs = BreakoutStrategy().decide_entries(_priced_ctx(budget=166_667))
    assert [s.symbol for s in sigs] == ["CHEAP1", "CHEAP2"]       # 비싼 3종목 대신 다음 순위


def test_without_budget_keeps_previous_ranking():
    sigs = BreakoutStrategy().decide_entries(_priced_ctx())
    assert [s.symbol for s in sigs] == ["HYNIX", "SAMSUNG", "NAVER"]


def test_budget_partially_affordable_keeps_rank_order():
    sigs = BreakoutStrategy().decide_entries(_priced_ctx(budget=250_000))
    assert [s.symbol for s in sigs] == ["SAMSUNG", "NAVER", "CHEAP1"]


def test_price_equal_to_budget_is_affordable():
    ctx = _ctx(extras={"max_entry_notional": 105.0})
    assert "A" in {s.symbol for s in BreakoutStrategy().decide_entries(ctx)}


def test_cash_allocation_also_limits_budget():
    """상한보다 현금 배분(cash/3)이 작으면 그쪽이 기준."""
    sigs = BreakoutStrategy().decide_entries(_priced_ctx(budget=10_000_000, cash_available=600_000))
    assert [s.symbol for s in sigs] == ["NAVER", "CHEAP1", "CHEAP2"]   # 200,000: 삼성 230,501 제외, NAVER 183,501 포함


def test_nothing_affordable_no_signals():
    assert BreakoutStrategy().decide_entries(_priced_ctx(budget=1_000)) == []


def test_unaffordable_logged_once_per_day(caplog):
    caplog.set_level("DEBUG")
    strat = BreakoutStrategy()
    strat.decide_entries(_priced_ctx(budget=166_667))
    strat.decide_entries(_priced_ctx(budget=166_667))
    infos = [r for r in caplog.records if "skip unaffordable HYNIX" in r.getMessage() and r.levelname == "INFO"]
    debugs = [r for r in caplog.records if "skip unaffordable HYNIX" in r.getMessage() and r.levelname == "DEBUG"]
    assert len(infos) == 1 and len(debugs) == 1


# --- 유동성 미달 종목 건너뛰기 (10-29) ---------------------------------------

def test_illiquid_top_ranked_skipped_and_slots_refilled():
    ctx = _priced_ctx(budget=10_000_000)
    ctx.extras["entry_illiquid"] = frozenset({"HYNIX"})
    sigs = BreakoutStrategy().decide_entries(ctx)
    assert [s.symbol for s in sigs] == ["SAMSUNG", "NAVER", "CHEAP1"]     # HYNIX 대신 다음 순위


def test_illiquid_and_unaffordable_logged_separately(caplog):
    caplog.set_level("DEBUG")
    ctx = _priced_ctx(budget=166_667)
    ctx.extras["entry_illiquid"] = frozenset({"CHEAP1"})
    strat = BreakoutStrategy()
    assert [s.symbol for s in strat.decide_entries(ctx)] == ["CHEAP2"]
    strat.decide_entries(ctx)
    msgs = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert msgs.count(("INFO", "skip illiquid CHEAP1")) == 1
    assert msgs.count(("DEBUG", "skip illiquid CHEAP1")) == 1
    assert sum(1 for lv, m in msgs if lv == "INFO" and m.startswith("skip unaffordable HYNIX")) == 1
