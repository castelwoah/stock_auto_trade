"""Tests for src.analysis.flow.compute_flow_score."""
from __future__ import annotations

import pytest

from src.analysis.flow import compute_flow_score


OKEUK = 1e8  # 1억 원


def test_all_none_insufficient():
    result = compute_flow_score(
        {
            "foreign_net_5d": None,
            "institutional_net_5d": None,
            "volume_ratio_5d_vs_60d": None,
        }
    )
    assert result.insufficient_data is True
    assert result.score == 50.0
    assert result.reason == "수급 데이터 없음"


def test_strong_supply_score_above_70():
    # 외인 +100억, 기관 +50억, volume 2.0x (market_cap 없음 → abs 기준)
    result = compute_flow_score(
        {
            "foreign_net_5d": 100 * OKEUK,
            "institutional_net_5d": 50 * OKEUK,
            "volume_ratio_5d_vs_60d": 2.0,
        }
    )
    assert result.insufficient_data is False
    assert result.score > 70, f"expected >70, got {result.score}"
    # 외인 sub = 100 (saturated), 기관 sub = 75, vol sub ≈ 76.67 → avg ≈ 83.9
    assert result.details["foreign_sub_score"] == 100.0
    assert 70 <= result.details["institutional_sub_score"] <= 80


def test_selling_pressure_score_below_30():
    # 외인 -50억, 기관 -30억, volume 0.5x
    result = compute_flow_score(
        {
            "foreign_net_5d": -50 * OKEUK,
            "institutional_net_5d": -30 * OKEUK,
            "volume_ratio_5d_vs_60d": 0.5,
        }
    )
    assert result.insufficient_data is False
    assert result.score < 30, f"expected <30, got {result.score}"
    assert "순매도" in result.reason


def test_neutral_scores_around_50():
    result = compute_flow_score(
        {
            "foreign_net_5d": 0.0,
            "institutional_net_5d": 0.0,
            "volume_ratio_5d_vs_60d": 1.0,
        }
    )
    assert result.insufficient_data is False
    assert 45 <= result.score <= 55
    assert result.details["foreign_sub_score"] == 50.0
    assert result.details["institutional_sub_score"] == 50.0
    assert result.details["volume_sub_score"] == 50.0


def test_volume_overheated_region():
    # ratio 4.0 = 과열 구간 → volume sub 약 80점 (3.0→85, 6.0→70, 선형 → 4.0 = 80)
    result = compute_flow_score(
        {
            "foreign_net_5d": None,
            "institutional_net_5d": None,
            "volume_ratio_5d_vs_60d": 4.0,
        }
    )
    assert result.insufficient_data is False
    vol_sub = result.details["volume_sub_score"]
    assert 75 <= vol_sub <= 85, f"overheated vol sub out of range: {vol_sub}"
    assert "과열" in result.reason


def test_volume_sweet_spot():
    # ratio 2.0 = 관심 급증 가점 → 약 76.67
    result = compute_flow_score(
        {
            "foreign_net_5d": None,
            "institutional_net_5d": None,
            "volume_ratio_5d_vs_60d": 2.0,
        }
    )
    vol_sub = result.details["volume_sub_score"]
    assert 70 <= vol_sub <= 90
    assert "급증" in result.reason


def test_volume_decrease_below_50():
    # ratio 0.5 → vol sub 25 (거래 감소)
    result = compute_flow_score(
        {
            "foreign_net_5d": None,
            "institutional_net_5d": None,
            "volume_ratio_5d_vs_60d": 0.5,
        }
    )
    vol_sub = result.details["volume_sub_score"]
    assert vol_sub < 50
    assert abs(vol_sub - 25.0) < 1e-6


def test_market_cap_changes_normalization():
    """market_cap 있으면 %기준으로 정규화 → 같은 절대 순매수액도 다른 점수."""
    # 외인 +100억, market_cap 1조 (=1%) → sub = 100 (saturated at +1%)
    big_cap = compute_flow_score(
        {
            "foreign_net_5d": 100 * OKEUK,
            "institutional_net_5d": None,
            "volume_ratio_5d_vs_60d": None,
            "market_cap": 1e12,  # 1조 원
        }
    )
    assert big_cap.details["market_cap_used"] is True
    assert big_cap.details["foreign_basis"] == "pct_of_mcap"
    assert big_cap.details["foreign_sub_score"] == 100.0

    # 외인 +100억, market_cap 100조 (=0.01%) → sub ≈ 50.5 (거의 중립)
    huge_cap = compute_flow_score(
        {
            "foreign_net_5d": 100 * OKEUK,
            "institutional_net_5d": None,
            "volume_ratio_5d_vs_60d": None,
            "market_cap": 1e14,  # 100조 원
        }
    )
    assert huge_cap.details["foreign_basis"] == "pct_of_mcap"
    # 0.01% of mcap → very close to neutral
    assert 49 <= huge_cap.details["foreign_sub_score"] <= 52

    # 동일 순매수액 + market_cap 없음 → abs 기준, sub = 100 (±100억 saturates)
    no_cap = compute_flow_score(
        {
            "foreign_net_5d": 100 * OKEUK,
            "institutional_net_5d": None,
            "volume_ratio_5d_vs_60d": None,
        }
    )
    assert no_cap.details["market_cap_used"] is False
    assert no_cap.details["foreign_basis"] == "abs_krw"
    assert no_cap.details["foreign_sub_score"] == 100.0


def test_partial_data_some_none():
    """일부 지표만 None → insufficient=False, 나머지 평균."""
    result = compute_flow_score(
        {
            "foreign_net_5d": 100 * OKEUK,
            "institutional_net_5d": None,
            "volume_ratio_5d_vs_60d": None,
        }
    )
    assert result.insufficient_data is False
    # 오직 외인 sub = 100
    assert result.score == 100.0
    assert result.details["institutional_sub_score"] is None
    assert result.details["volume_sub_score"] is None


def test_mixed_foreign_sell_institutional_buy():
    """외인 매도 + 기관 매수 → 중립 근처."""
    result = compute_flow_score(
        {
            "foreign_net_5d": -100 * OKEUK,
            "institutional_net_5d": 100 * OKEUK,
            "volume_ratio_5d_vs_60d": 1.0,
        }
    )
    # 외인 sub = 0, 기관 sub = 100, vol sub = 50 → avg = 50
    assert 45 <= result.score <= 55


def test_details_contain_all_fields():
    result = compute_flow_score(
        {
            "foreign_net_5d": 10 * OKEUK,
            "institutional_net_5d": 5 * OKEUK,
            "volume_ratio_5d_vs_60d": 1.2,
            "market_cap": 5e11,
        }
    )
    d = result.details
    for k in (
        "foreign_net_5d",
        "institutional_net_5d",
        "volume_ratio_5d_vs_60d",
        "market_cap",
        "foreign_sub_score",
        "institutional_sub_score",
        "volume_sub_score",
        "foreign_basis",
        "institutional_basis",
        "market_cap_used",
    ):
        assert k in d, f"missing key {k}"


def test_strong_foreign_inst_buy_triggers_combined_reason():
    result = compute_flow_score(
        {
            "foreign_net_5d": 200 * OKEUK,
            "institutional_net_5d": 200 * OKEUK,
            "volume_ratio_5d_vs_60d": 2.5,
        }
    )
    assert "외인·기관 동반 순매수" in result.reason
    assert "거래량 급증" in result.reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
