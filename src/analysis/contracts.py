from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FactorScore:
    """All factor computers (technical/financial/flow/news) return this shape.

    - score: 0.0 ~ 100.0 (clipped)
    - details: 원천 지표(RSI, 부채비율 등)를 디버깅/리포트용으로 포함
    - reason: 스코어가 특히 높/낮을 때의 간단 사유 (리포트에 노출)
    - insufficient_data: 입력 데이터가 모자라 신뢰할 수 없다는 플래그
      (scorer는 이 팩터를 평균 대체값으로 교체하거나 가중치에서 제외)
    """

    score: float
    details: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    insufficient_data: bool = False


def clip_score(x: float, *, lo: float = 0.0, hi: float = 100.0) -> float:
    if x != x:  # NaN
        return lo
    if x < lo:
        return lo
    if x > hi:
        return hi
    return float(x)


def linear_map(x: float, *, src_lo: float, src_hi: float, dst_lo: float = 0.0, dst_hi: float = 100.0) -> float:
    """Map x ∈ [src_lo, src_hi] to [dst_lo, dst_hi], clipping outside range."""
    if src_hi == src_lo:
        return (dst_lo + dst_hi) / 2.0
    t = (x - src_lo) / (src_hi - src_lo)
    y = dst_lo + t * (dst_hi - dst_lo)
    return clip_score(y, lo=dst_lo, hi=dst_hi)
