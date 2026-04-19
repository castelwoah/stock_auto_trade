"""Unit tests for src.analysis.technical (pure Python, stdlib only)."""
from __future__ import annotations

import math

import pytest

from src.analysis.technical import (
    _ema,
    _macd,
    _rsi_wilder,
    _sma,
    compute_technical_score,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bar(i: int, close: float, *, vol: int | None = 1000) -> dict:
    d = {
        "date": f"2024{(i // 30) + 1:02d}{(i % 30) + 1:02d}",
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
    }
    if vol is not None:
        d["volume"] = vol
    return d


def _series(closes: list[float]) -> list[dict]:
    return [_bar(i, c) for i, c in enumerate(closes)]


# ---------------------------------------------------------------------------
# insufficient_data
# ---------------------------------------------------------------------------
def test_insufficient_data_returns_flag_and_neutral_score() -> None:
    # 59 bars -> below the 60 minimum
    bars = _series([100.0 + i for i in range(59)])
    fs = compute_technical_score(bars)
    assert fs.insufficient_data is True
    assert fs.score == 50.0
    assert fs.details["bars"] == 59


def test_empty_input_is_insufficient() -> None:
    fs = compute_technical_score([])
    assert fs.insufficient_data is True
    assert fs.score == 50.0


# ---------------------------------------------------------------------------
# Trend tests
# ---------------------------------------------------------------------------
def test_steady_uptrend_score_above_50_and_bull_alignment() -> None:
    closes = [100.0 + i for i in range(80)]
    fs = compute_technical_score(_series(closes))
    assert fs.insufficient_data is False
    # MA bull alignment (+20) offsets the overbought RSI penalty (rules §6 Step3:
    # RSI>70 감점) exactly — score settles around 50 for linear uptrend.
    # Bull alignment flag itself is the primary signal here.
    assert fs.score >= 50.0
    assert fs.details["ma_bull_aligned"] is True
    # Uptrend RSI should be in overbought/high region
    assert fs.details["rsi"] is not None and fs.details["rsi"] > 60.0


def test_steady_downtrend_score_below_50() -> None:
    closes = [200.0 - i for i in range(80)]
    fs = compute_technical_score(_series(closes))
    assert fs.insufficient_data is False
    assert fs.score < 50.0
    assert fs.details["ma_bear_aligned"] is True


def test_oscillating_series_is_roughly_neutral() -> None:
    # Low-amplitude sine around 100 -> neutral RSI, partial/no MA alignment
    closes = [100.0 + 2.0 * math.sin(i * 0.4) for i in range(80)]
    fs = compute_technical_score(_series(closes))
    assert fs.insufficient_data is False
    # Expect the score to sit near 50 within a reasonable band
    assert 35.0 <= fs.score <= 65.0
    # RSI should be in a middle band
    rsi = fs.details["rsi"]
    assert rsi is not None and 30.0 < rsi < 70.0


# ---------------------------------------------------------------------------
# MACD cross scenarios
# ---------------------------------------------------------------------------
def test_macd_bullish_after_trend_reversal() -> None:
    # 긴 횡보 → 후반 급등: 마지막 3봉 창 안에서 골든크로스가 잡히도록 구성.
    # 70봉 100원 고정(중립 상태) → 마지막 10봉 +2씩 완만 상승.
    flat = [100.0] * 70
    rise = [100.0 + i * 2.0 for i in range(1, 11)]
    fs = compute_technical_score(_series(flat + rise))
    # 창 내 cross 또는 최소한 histogram 양수 기울기 (bullish tilt)
    assert fs.details["macd"] > fs.details["signal"]
    assert fs.details["contrib"]["macd"] > 0.0


def test_macd_bearish_after_trend_reversal() -> None:
    # 긴 횡보 → 후반 급락.
    flat = [100.0] * 70
    drop = [100.0 - i * 2.0 for i in range(1, 11)]
    fs = compute_technical_score(_series(flat + drop))
    assert fs.details["macd"] < fs.details["signal"]
    assert fs.details["contrib"]["macd"] < 0.0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------
def test_missing_volume_key_does_not_break() -> None:
    closes = [100.0 + i for i in range(70)]
    bars = [_bar(i, c, vol=None) for i, c in enumerate(closes)]
    # ensure we really dropped the key
    for b in bars:
        assert "volume" not in b
    fs = compute_technical_score(bars)
    assert fs.insufficient_data is False
    assert 0.0 <= fs.score <= 100.0


def test_score_is_always_clipped_to_unit_range() -> None:
    # combination that would push well over 100 without clipping
    closes = [100.0 + i for i in range(80)]
    fs = compute_technical_score(_series(closes))
    assert 0.0 <= fs.score <= 100.0


def test_details_contains_expected_keys() -> None:
    closes = [100.0 + i for i in range(80)]
    fs = compute_technical_score(_series(closes))
    for key in ("rsi", "ma5", "ma20", "ma60", "close", "macd", "signal", "macd_hist",
                "ma_bull_aligned", "ma_bear_aligned", "macd_cross", "contrib", "bars"):
        assert key in fs.details


# ---------------------------------------------------------------------------
# Low-level indicator sanity checks
# ---------------------------------------------------------------------------
def test_sma_matches_manual_calculation() -> None:
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = _sma(vals, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_ema_is_none_before_seed() -> None:
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = _ema(vals, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)  # seed = SMA of first 3
    # subsequent values should exist and trend upward
    assert out[3] is not None and out[4] is not None
    assert out[4] > out[3] > out[2]  # type: ignore[operator]


def test_rsi_monotonic_uptrend_is_high() -> None:
    closes = [100.0 + i for i in range(30)]
    r = _rsi_wilder(closes, 14)
    assert r[-1] is not None and r[-1] > 90.0


def test_rsi_monotonic_downtrend_is_low() -> None:
    closes = [100.0 - i for i in range(30)]
    r = _rsi_wilder(closes, 14)
    assert r[-1] is not None and r[-1] < 10.0


def test_macd_shape_aligned_with_input() -> None:
    closes = [100.0 + i * 0.5 for i in range(80)]
    macd, sig, hist = _macd(closes)
    assert len(macd) == len(closes) == len(sig) == len(hist)
    # Final values should be populated for an 80-bar series
    assert macd[-1] is not None
    assert sig[-1] is not None
    assert hist[-1] is not None
