"""재무 건전성 팩터 (Step2 25% 비중).

입력 fundamentals dict (값은 float 또는 None):
  - debt_ratio       : 부채비율 (%)
  - roe              : 자기자본이익률 (%)
  - operating_margin : 영업이익률 (%)
  - ocf_to_revenue   : 영업활동현금흐름 / 매출 (%)

각 지표를 0~100 sub-score 로 정규화 후 유효 지표의 균등 평균으로 최종 점수 산출.
"""
from __future__ import annotations

from typing import Any

from src.analysis.contracts import FactorScore, clip_score, linear_map


def _is_valid(x: Any) -> bool:
    """None/NaN 필터."""
    if x is None:
        return False
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return False
    if xf != xf:  # NaN
        return False
    return True


def _score_debt_ratio(x: float) -> float:
    """부채비율: 낮을수록 좋음. 50% 이하 → 100, 200% 이상 → 0 (선형 감소)."""
    # 역매핑: src_lo=200 → dst_lo=0, src_hi=50 → dst_hi=100
    return linear_map(x, src_lo=200.0, src_hi=50.0, dst_lo=0.0, dst_hi=100.0)


def _score_roe(x: float) -> float:
    """ROE: 높을수록 좋음. 0% 이하 → 0, 20% 이상 → 100."""
    return linear_map(x, src_lo=0.0, src_hi=20.0)


def _score_operating_margin(x: float) -> float:
    """영업이익률: 0% 이하 → 0, 20% 이상 → 100. 음수는 0."""
    if x < 0:
        return 0.0
    return linear_map(x, src_lo=0.0, src_hi=20.0)


def _score_ocf_to_revenue(x: float) -> float:
    """OCF/Revenue: 0% 이하 → 0, 15% 이상 → 100."""
    return linear_map(x, src_lo=0.0, src_hi=15.0)


_SCORERS = {
    "debt_ratio": _score_debt_ratio,
    "roe": _score_roe,
    "operating_margin": _score_operating_margin,
    "ocf_to_revenue": _score_ocf_to_revenue,
}


def _build_reason(subs: dict[str, float], raws: dict[str, float]) -> str:
    """대표 코멘트 생성."""
    parts: list[str] = []
    # 강점
    strengths: list[str] = []
    weaknesses: list[str] = []
    if "roe" in subs and subs["roe"] >= 80:
        strengths.append("ROE 우수")
    if "debt_ratio" in subs and subs["debt_ratio"] >= 80:
        strengths.append("저부채")
    if "operating_margin" in subs and subs["operating_margin"] >= 80:
        strengths.append("높은 영업이익률")
    if "ocf_to_revenue" in subs and subs["ocf_to_revenue"] >= 80:
        strengths.append("현금창출력 우수")

    if "roe" in subs and subs["roe"] <= 20:
        weaknesses.append("ROE 부진")
    if "debt_ratio" in subs and subs["debt_ratio"] <= 20:
        weaknesses.append("고부채 위험")
    if "operating_margin" in subs and subs["operating_margin"] <= 20:
        weaknesses.append("영업이익률 저조")
    if "ocf_to_revenue" in subs and subs["ocf_to_revenue"] <= 20:
        weaknesses.append("현금흐름 취약")

    if strengths:
        parts.append(" + ".join(strengths))
    if weaknesses:
        parts.append(" + ".join(weaknesses))
    return ", ".join(parts)


def compute_financial_score(fundamentals: dict) -> FactorScore:
    """재무 건전성 0~100 점수 산출.

    유효 지표가 하나도 없으면 insufficient_data=True, score=50 반환.
    """
    sub_scores: dict[str, float] = {}
    raws: dict[str, float] = {}

    for key, scorer in _SCORERS.items():
        raw = fundamentals.get(key) if isinstance(fundamentals, dict) else None
        if not _is_valid(raw):
            continue
        raw_f = float(raw)
        raws[key] = raw_f
        sub_scores[key] = scorer(raw_f)

    details: dict[str, Any] = {
        "sub_scores": dict(sub_scores),
        "raw": dict(raws),
        "valid_count": len(sub_scores),
    }

    if not sub_scores:
        return FactorScore(
            score=50.0,
            details=details,
            reason="재무 데이터 없음",
            insufficient_data=True,
        )

    avg = sum(sub_scores.values()) / len(sub_scores)
    final = clip_score(avg)
    reason = _build_reason(sub_scores, raws)
    return FactorScore(
        score=final,
        details=details,
        reason=reason,
        insufficient_data=False,
    )
