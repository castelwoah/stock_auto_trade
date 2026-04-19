"""Technical factor computation (pure Python stdlib only).

Calculates RSI(14), MA5/MA20/MA60, and MACD (EMA12/EMA26, signal=EMA9)
from an OHLCV time series and aggregates them into a FactorScore (0~100).

No external deps (pandas/numpy/talib are forbidden here).
"""
from __future__ import annotations

from typing import Any, Sequence

from .contracts import FactorScore, clip_score, linear_map


# ---------------------------------------------------------------------------
# Low-level indicator helpers
# ---------------------------------------------------------------------------
def _sma(values: Sequence[float], period: int) -> list[float | None]:
    """Simple moving average. Output aligned with input (None until enough data)."""
    out: list[float | None] = []
    if period <= 0:
        raise ValueError("period must be > 0")
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i + 1 >= period:
            out.append(running / period)
        else:
            out.append(None)
    return out


def _ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with SMA of first `period` values."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    # seed with SMA
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, len(values)):
        cur = values[i] * k + prev * (1.0 - k)
        out[i] = cur
        prev = cur
    return out


def _rsi_wilder(closes: Sequence[float], period: int = 14) -> list[float | None]:
    """RSI using Wilder's smoothing. Output aligned with input."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out

    gains = 0.0
    losses = 0.0
    # first `period` diffs (indices 1..period)
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, n):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def _macd(
    closes: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return (macd_line, signal_line, histogram) aligned with closes."""
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line: list[float | None] = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)
    # EMA of macd_line from the first index where it is not None
    first_valid = next((i for i, v in enumerate(macd_line) if v is not None), None)
    signal_line: list[float | None] = [None] * len(closes)
    if first_valid is not None:
        dense = [v for v in macd_line[first_valid:] if v is not None]
        sig_dense = _ema(dense, signal)
        for off, v in enumerate(sig_dense):
            signal_line[first_valid + off] = v
    hist: list[float | None] = []
    for m, s in zip(macd_line, signal_line):
        if m is None or s is None:
            hist.append(None)
        else:
            hist.append(m - s)
    return macd_line, signal_line, hist


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_MIN_BARS = 60


def compute_technical_score(ohlcv: list[dict[str, Any]]) -> FactorScore:
    """Compute a 0~100 technical factor score from an OHLCV series.

    Scoring (centered at 50):
      * RSI:   ~30 (oversold, buy-friendly) → +, >70 (overbought) → -
      * MA alignment (close > ma5 > ma20 > ma60): strong +
      * MACD golden cross (just crossed up): +; dead cross: -
    """
    closes: list[float] = [float(b.get("close", 0.0)) for b in ohlcv]
    n = len(closes)

    if n < _MIN_BARS:
        return FactorScore(
            score=50.0,
            details={"bars": n, "required": _MIN_BARS},
            reason="데이터 부족 (최소 60봉 필요)",
            insufficient_data=True,
        )

    # --- Indicators ----------------------------------------------------------
    rsi_series = _rsi_wilder(closes, 14)
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    macd_line, signal_line, hist = _macd(closes)

    i = n - 1
    rsi = rsi_series[i]
    last_close = closes[i]
    m5 = ma5[i]
    m20 = ma20[i]
    m60 = ma60[i]
    macd_now = macd_line[i]
    sig_now = signal_line[i]
    macd_prev = macd_line[i - 1] if i - 1 >= 0 else None
    sig_prev = signal_line[i - 1] if i - 1 >= 0 else None

    # --- Score components (each roughly in [-x, +x]) -------------------------
    score = 50.0
    reasons: list[str] = []

    # RSI contribution: map to [-20, +20]
    #   25-35: peak buy-friendly (+20 at 30)
    #   >70: overbought (penalty up to -20 at 80+)
    #   <20: potential falling knife (mild penalty)
    rsi_contrib = 0.0
    if rsi is not None:
        if 25.0 <= rsi <= 35.0:
            # triangular peak centered at 30
            rsi_contrib = 20.0 * (1.0 - abs(rsi - 30.0) / 5.0)
            reasons.append("RSI 매수우호")
        elif rsi < 25.0:
            # very oversold: small neutral-to-slight penalty
            rsi_contrib = -5.0
        elif 35.0 < rsi < 70.0:
            rsi_contrib = 0.0
        elif 70.0 <= rsi < 80.0:
            rsi_contrib = linear_map(rsi, src_lo=70.0, src_hi=80.0, dst_lo=-10.0, dst_hi=-20.0)
            reasons.append("RSI 과열")
        else:  # rsi >= 80
            rsi_contrib = -20.0
            reasons.append("RSI 과열")

    # MA alignment contribution: in [-20, +20]
    ma_aligned_bull = False
    ma_aligned_bear = False
    ma_contrib = 0.0
    if None not in (m5, m20, m60):
        if last_close > m5 > m20 > m60:  # type: ignore[operator]
            ma_aligned_bull = True
            ma_contrib = 20.0
            reasons.append("MA 정배열")
        elif last_close < m5 < m20 < m60:  # type: ignore[operator]
            ma_aligned_bear = True
            ma_contrib = -20.0
            reasons.append("MA 역배열")
        else:
            # partial credit: compare close vs MA20 slope
            if last_close > m20:  # type: ignore[operator]
                ma_contrib = 5.0
            else:
                ma_contrib = -5.0

    # MACD cross contribution: in [-15, +15]
    # Detect cross within the last `cross_window` bars; freshest cross wins.
    # Epsilon guards against floating-point noise making `now_diff` slightly
    # negative on perfectly linear series (where MACD-signal should be ~0).
    macd_contrib = 0.0
    cross = "none"
    cross_window = 3
    eps = 1e-9
    if macd_now is not None and sig_now is not None:
        diffs: list[float | None] = [
            (macd_line[k] - signal_line[k])  # type: ignore[operator]
            if (macd_line[k] is not None and signal_line[k] is not None)
            else None
            for k in range(max(0, i - cross_window), i + 1)
        ]
        # scan from freshest backwards for a sign change
        for k in range(len(diffs) - 1, 0, -1):
            a, b = diffs[k - 1], diffs[k]
            if a is None or b is None:
                continue
            if a <= eps and b > eps:
                cross = "golden"
                macd_contrib = 15.0
                reasons.append("MACD 골든크로스")
                break
            if a >= -eps and b < -eps:
                cross = "dead"
                macd_contrib = -15.0
                reasons.append("MACD 데드크로스")
                break
        if cross == "none":
            now_diff = macd_now - sig_now  # type: ignore[operator]
            if now_diff > eps:
                macd_contrib = 5.0
            elif now_diff < -eps:
                macd_contrib = -5.0

    score += rsi_contrib + ma_contrib + macd_contrib
    score = clip_score(score)

    details: dict[str, Any] = {
        "rsi": rsi,
        "ma5": m5,
        "ma20": m20,
        "ma60": m60,
        "close": last_close,
        "macd": macd_now,
        "signal": sig_now,
        "macd_hist": hist[i],
        "ma_bull_aligned": ma_aligned_bull,
        "ma_bear_aligned": ma_aligned_bear,
        "macd_cross": cross,
        "contrib": {
            "rsi": rsi_contrib,
            "ma": ma_contrib,
            "macd": macd_contrib,
        },
        "bars": n,
    }

    reason = " + ".join(reasons) if reasons else "중립 구간"
    return FactorScore(score=score, details=details, reason=reason, insufficient_data=False)
