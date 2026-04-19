"""재무 건전성 팩터 테스트."""
from __future__ import annotations

import math

import pytest

from src.analysis.financial import compute_financial_score


def test_all_none_insufficient_data():
    """전부 None → insufficient_data=True, score=50."""
    fund = {
        "debt_ratio": None,
        "roe": None,
        "operating_margin": None,
        "ocf_to_revenue": None,
    }
    res = compute_financial_score(fund)
    assert res.insufficient_data is True
    assert res.score == 50.0
    assert res.reason == "재무 데이터 없음"


def test_empty_dict_insufficient_data():
    """빈 dict → insufficient_data=True."""
    res = compute_financial_score({})
    assert res.insufficient_data is True
    assert res.score == 50.0


def test_ideal_company_max_score():
    """이상적 기업: debt=30, roe=25, op=25, ocf=20 → score ≈ 100."""
    fund = {
        "debt_ratio": 30.0,
        "roe": 25.0,
        "operating_margin": 25.0,
        "ocf_to_revenue": 20.0,
    }
    res = compute_financial_score(fund)
    assert res.insufficient_data is False
    assert res.score == pytest.approx(100.0, abs=0.01)
    # sub-scores 모두 100
    for key in ("debt_ratio", "roe", "operating_margin", "ocf_to_revenue"):
        assert res.details["sub_scores"][key] == pytest.approx(100.0, abs=0.01)


def test_worst_company_min_score():
    """최악 기업: debt=300, roe=-10, op=-5, ocf=-3 → score ≈ 0."""
    fund = {
        "debt_ratio": 300.0,
        "roe": -10.0,
        "operating_margin": -5.0,
        "ocf_to_revenue": -3.0,
    }
    res = compute_financial_score(fund)
    assert res.insufficient_data is False
    assert res.score == pytest.approx(0.0, abs=0.01)


def test_average_company():
    """평균적 기업: debt=100, roe=10, op=10, ocf=7 → 40~60."""
    fund = {
        "debt_ratio": 100.0,
        "roe": 10.0,
        "operating_margin": 10.0,
        "ocf_to_revenue": 7.0,
    }
    res = compute_financial_score(fund)
    assert res.insufficient_data is False
    assert 40.0 <= res.score <= 60.0
    # debt=100 → (200-100)/(200-50)*100 = 66.67
    assert res.details["sub_scores"]["debt_ratio"] == pytest.approx(66.667, abs=0.1)
    # roe=10 → 50
    assert res.details["sub_scores"]["roe"] == pytest.approx(50.0, abs=0.01)
    # op=10 → 50
    assert res.details["sub_scores"]["operating_margin"] == pytest.approx(50.0, abs=0.01)
    # ocf=7 → 7/15*100 = 46.67
    assert res.details["sub_scores"]["ocf_to_revenue"] == pytest.approx(46.667, abs=0.1)


def test_partial_none_debt_missing():
    """debt_ratio만 None → 나머지 3개로 평균."""
    fund = {
        "debt_ratio": None,
        "roe": 20.0,        # 100점
        "operating_margin": 20.0,  # 100점
        "ocf_to_revenue": 15.0,    # 100점
    }
    res = compute_financial_score(fund)
    assert res.insufficient_data is False
    assert res.score == pytest.approx(100.0, abs=0.01)
    assert "debt_ratio" not in res.details["sub_scores"]
    assert res.details["valid_count"] == 3


def test_boundary_debt_50_is_100():
    """debt_ratio=50 → sub-score 100."""
    fund = {"debt_ratio": 50.0, "roe": None, "operating_margin": None, "ocf_to_revenue": None}
    res = compute_financial_score(fund)
    assert res.insufficient_data is False
    assert res.details["sub_scores"]["debt_ratio"] == pytest.approx(100.0, abs=0.01)
    assert res.score == pytest.approx(100.0, abs=0.01)


def test_boundary_debt_200_is_0():
    """debt_ratio=200 → sub-score 0."""
    fund = {"debt_ratio": 200.0, "roe": None, "operating_margin": None, "ocf_to_revenue": None}
    res = compute_financial_score(fund)
    assert res.insufficient_data is False
    assert res.details["sub_scores"]["debt_ratio"] == pytest.approx(0.0, abs=0.01)
    assert res.score == pytest.approx(0.0, abs=0.01)


def test_negative_operating_margin_clipped_to_zero():
    """음수 영업이익률 → 0점."""
    fund = {
        "debt_ratio": None,
        "roe": None,
        "operating_margin": -50.0,
        "ocf_to_revenue": None,
    }
    res = compute_financial_score(fund)
    assert res.details["sub_scores"]["operating_margin"] == 0.0
    assert res.score == 0.0


def test_nan_treated_as_missing():
    """NaN 값은 None 처럼 제외된다."""
    fund = {
        "debt_ratio": float("nan"),
        "roe": 20.0,
        "operating_margin": None,
        "ocf_to_revenue": None,
    }
    res = compute_financial_score(fund)
    assert res.insufficient_data is False
    assert res.details["valid_count"] == 1
    assert "debt_ratio" not in res.details["sub_scores"]
    assert res.score == pytest.approx(100.0, abs=0.01)


def test_all_nan_insufficient_data():
    """전부 NaN → insufficient_data=True, score=50."""
    fund = {
        "debt_ratio": float("nan"),
        "roe": float("nan"),
        "operating_margin": float("nan"),
        "ocf_to_revenue": float("nan"),
    }
    res = compute_financial_score(fund)
    assert res.insufficient_data is True
    assert res.score == 50.0


def test_reason_for_high_score():
    """고득점 기업은 강점 코멘트 생성."""
    fund = {
        "debt_ratio": 30.0,
        "roe": 25.0,
        "operating_margin": 25.0,
        "ocf_to_revenue": 20.0,
    }
    res = compute_financial_score(fund)
    assert "ROE 우수" in res.reason
    assert "저부채" in res.reason


def test_reason_for_low_score():
    """저득점 기업은 약점 코멘트 생성."""
    fund = {
        "debt_ratio": 300.0,
        "roe": -10.0,
        "operating_margin": -5.0,
        "ocf_to_revenue": -3.0,
    }
    res = compute_financial_score(fund)
    assert "고부채 위험" in res.reason or "ROE 부진" in res.reason


def test_score_clipped_in_range():
    """score 는 항상 0~100."""
    for debt in (-100.0, 0.0, 50.0, 125.0, 200.0, 500.0):
        fund = {
            "debt_ratio": debt,
            "roe": None,
            "operating_margin": None,
            "ocf_to_revenue": None,
        }
        res = compute_financial_score(fund)
        assert 0.0 <= res.score <= 100.0


def test_details_contains_raw_and_sub_scores():
    """details 에 원값 및 sub-score 기록."""
    fund = {
        "debt_ratio": 80.0,
        "roe": 15.0,
        "operating_margin": None,
        "ocf_to_revenue": None,
    }
    res = compute_financial_score(fund)
    assert res.details["raw"]["debt_ratio"] == 80.0
    assert res.details["raw"]["roe"] == 15.0
    assert "operating_margin" not in res.details["raw"]
    assert "debt_ratio" in res.details["sub_scores"]
    assert "roe" in res.details["sub_scores"]
    assert res.details["valid_count"] == 2
