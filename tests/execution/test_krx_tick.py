import pytest

from src.execution.krx_tick import round_to_tick, shift_ticks, tick_size


@pytest.mark.parametrize("price,tick", [
    (1_999, 1), (2_000, 5), (4_995, 5), (5_000, 10), (19_990, 10), (20_000, 50),
    (49_950, 50), (50_000, 100), (199_900, 100), (200_000, 500), (499_500, 500),
    (500_000, 1_000), (1_200_000, 1_000),
])
def test_tick_size_table(price, tick):
    assert tick_size(price) == tick


@pytest.mark.parametrize("price,up,expected", [
    (70_049, True, 70_100), (70_049, False, 70_000), (70_000, True, 70_000),
    (4_997, True, 5_000), (4_997, False, 4_995), (1_999.4, False, 1_999),
    (19_995, True, 20_000),
])
def test_round_to_tick(price, up, expected):
    assert round_to_tick(price, up=up) == expected


def test_round_to_tick_rejects_nonpositive():
    with pytest.raises(ValueError):
        round_to_tick(0, up=True)


@pytest.mark.parametrize("price,n,expected", [
    (70_000, 1, 70_100), (70_000, -1, 69_900), (50_000, -1, 49_950),
    (49_950, 1, 50_000), (2_000, -1, 1_999), (1_999, 1, 2_000), (70_000, -3, 69_700),
    (1, -1, 1),
])
def test_shift_ticks(price, n, expected):
    assert shift_ticks(price, n) == expected


def test_results_are_tick_aligned():
    for p in range(1_000, 600_000, 3_331):
        for n in (-2, -1, 1, 2):
            q = shift_ticks(p, n)
            assert q % tick_size(q) == 0 or q < 2_000
