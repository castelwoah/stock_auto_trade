import pytest

from src.risk.sizing import (
    SizingPolicy,
    compute_equal_weight_notional,
    compute_kelly_notional,
    compute_max_additional_notional,
    resolve_quantity,
)


def test_symbol_cap_headroom_empty_portfolio():
    # 10M portfolio, 20% cap → 2M per symbol
    h = compute_max_additional_notional(
        symbol="A", portfolio_value=10_000_000, existing_positions_notional={}
    )
    assert h == pytest.approx(2_000_000.0)


def test_symbol_cap_reduces_by_existing():
    h = compute_max_additional_notional(
        symbol="A",
        portfolio_value=10_000_000,
        existing_positions_notional={"A": 1_500_000},
    )
    assert h == pytest.approx(500_000.0)


def test_symbol_cap_saturated_returns_zero():
    h = compute_max_additional_notional(
        symbol="A",
        portfolio_value=10_000_000,
        existing_positions_notional={"A": 2_000_000},
    )
    assert h == 0.0


def test_sector_cap_limits_even_when_symbol_has_room():
    pv = 10_000_000
    # 섹터 'tech' 에 이미 4M (40% cap 도달), 새 심볼 A (tech) 진입 불가
    h = compute_max_additional_notional(
        symbol="A",
        portfolio_value=pv,
        existing_positions_notional={"B": 2_000_000, "C": 2_000_000},
        sector="tech",
        sector_map={"A": "tech", "B": "tech", "C": "tech"},
    )
    assert h == 0.0


def test_sector_cap_accommodates_partial():
    pv = 10_000_000
    # 섹터 tech 에 1M 있음 → sector headroom = 4M - 1M = 3M
    # symbol headroom = 2M - 0 = 2M → min = 2M
    h = compute_max_additional_notional(
        symbol="A",
        portfolio_value=pv,
        existing_positions_notional={"B": 1_000_000},
        sector="tech",
        sector_map={"A": "tech", "B": "tech"},
    )
    assert h == pytest.approx(2_000_000.0)


def test_zero_portfolio_returns_zero():
    h = compute_max_additional_notional(
        symbol="A", portfolio_value=0, existing_positions_notional={}
    )
    assert h == 0.0


def test_sector_inferred_from_map():
    pv = 10_000_000
    h = compute_max_additional_notional(
        symbol="A",
        portfolio_value=pv,
        existing_positions_notional={"B": 3_500_000},
        sector_map={"A": "tech", "B": "tech"},
    )
    # sector headroom = 4M - 3.5M = 0.5M, symbol 2M → min 0.5M
    assert h == pytest.approx(500_000.0)


# --- Equal weight -----------------------------------------------------------

def test_equal_weight_default():
    assert compute_equal_weight_notional(cash=5_000_000) == pytest.approx(1_000_000)


def test_equal_weight_custom_count():
    assert compute_equal_weight_notional(cash=5_000_000, target_count=10) == 500_000


def test_equal_weight_zero_target_count():
    assert compute_equal_weight_notional(cash=5_000_000, target_count=0) == 0.0


def test_equal_weight_negative_cash_clipped():
    assert compute_equal_weight_notional(cash=-100) == 0.0


# --- Kelly ------------------------------------------------------------------

def test_kelly_positive_edge():
    # p=0.6, b=2 → f* = (0.6*2 - 0.4) / 2 = 0.4 → × 0.25 × 10M = 1M
    k = compute_kelly_notional(win_rate=0.6, win_loss_ratio=2.0, portfolio_value=10_000_000)
    assert k == pytest.approx(1_000_000.0)


def test_kelly_zero_or_negative_edge_returns_zero():
    assert compute_kelly_notional(win_rate=0.3, win_loss_ratio=1.0, portfolio_value=10_000_000) == 0.0


def test_kelly_invalid_win_rate_raises():
    with pytest.raises(ValueError):
        compute_kelly_notional(win_rate=1.5, win_loss_ratio=2, portfolio_value=1)
    with pytest.raises(ValueError):
        compute_kelly_notional(win_rate=-0.1, win_loss_ratio=2, portfolio_value=1)


def test_kelly_zero_ratio_returns_zero():
    assert compute_kelly_notional(win_rate=0.6, win_loss_ratio=0, portfolio_value=10_000_000) == 0.0


def test_kelly_negative_portfolio_clipped():
    assert compute_kelly_notional(win_rate=0.6, win_loss_ratio=2, portfolio_value=-100) == 0.0


# --- resolve_quantity -------------------------------------------------------

def test_resolve_quantity_basic():
    assert resolve_quantity(max_notional=100_000, price=1_000) == 100


def test_resolve_quantity_with_lot_size():
    assert resolve_quantity(max_notional=100_000, price=1_000, lot_size=10) == 100
    assert resolve_quantity(max_notional=95_000, price=1_000, lot_size=10) == 90


def test_resolve_quantity_edge_cases():
    assert resolve_quantity(max_notional=0, price=1) == 0
    assert resolve_quantity(max_notional=100, price=0) == 0
    assert resolve_quantity(max_notional=100, price=-1) == 0
    assert resolve_quantity(max_notional=100, price=10, lot_size=0) == 0
