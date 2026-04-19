"""Tests for src.scheduler.calendar."""
from __future__ import annotations

from datetime import date

import pytest

from src.scheduler.calendar import (
    KRX_HOLIDAYS_2026,
    business_days_between,
    business_days_count,
    is_business_day,
    is_weekend,
    next_business_day,
    prev_business_day,
)


# ---------------------------------------------------------------------------
# is_business_day
# ---------------------------------------------------------------------------

def test_new_year_day_is_not_business_day():
    # 2026-01-01 신정
    assert is_business_day(date(2026, 1, 1)) is False


def test_jan_2_2026_is_business_day():
    # 2026-01-02 금요일, 평일 & 휴장일 아님
    assert is_business_day(date(2026, 1, 2)) is True


def test_jan_3_2026_is_saturday():
    # 2026-01-03 토요일
    assert is_business_day(date(2026, 1, 3)) is False


def test_jan_5_2026_is_business_day():
    # 2026-01-05 월요일
    assert is_business_day(date(2026, 1, 5)) is True


# ---------------------------------------------------------------------------
# is_weekend
# ---------------------------------------------------------------------------

def test_is_weekend_true_for_saturday_and_sunday():
    assert is_weekend(date(2026, 1, 3)) is True   # 토
    assert is_weekend(date(2026, 1, 4)) is True   # 일


def test_is_weekend_false_for_weekdays():
    assert is_weekend(date(2026, 1, 2)) is False  # 금
    assert is_weekend(date(2026, 1, 5)) is False  # 월


# ---------------------------------------------------------------------------
# next_business_day
# ---------------------------------------------------------------------------

def test_next_business_day_skips_new_year():
    # 2026-01-01(목, 신정) → 다음 영업일은 2026-01-02(금)
    assert next_business_day(date(2026, 1, 1)) == date(2026, 1, 2)


def test_next_business_day_skips_lunar_new_year():
    # 2026-02-13(금) 다음 영업일은 2026-02-19(목)
    # (2/14 토, 2/15 일, 2/16~18 설 연휴)
    assert next_business_day(date(2026, 2, 13)) == date(2026, 2, 19)


def test_next_business_day_weekend_jump():
    # 2026-01-02(금) 다음 영업일은 2026-01-05(월)
    assert next_business_day(date(2026, 1, 2)) == date(2026, 1, 5)


# ---------------------------------------------------------------------------
# prev_business_day
# ---------------------------------------------------------------------------

def test_prev_business_day_basic():
    # 2026-01-05(월) 이전 영업일은 2026-01-02(금)
    assert prev_business_day(date(2026, 1, 5)) == date(2026, 1, 2)


def test_prev_business_day_skips_lunar_new_year():
    # 2026-02-19(목) 이전 영업일은 2026-02-13(금)
    assert prev_business_day(date(2026, 2, 19)) == date(2026, 2, 13)


# ---------------------------------------------------------------------------
# business_days_between / count
# ---------------------------------------------------------------------------

def test_business_days_between_inclusive():
    # 2026-01-05(월) ~ 2026-01-09(금) = 5 영업일
    result = business_days_between(date(2026, 1, 5), date(2026, 1, 9))
    assert result == [
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
        date(2026, 1, 9),
    ]


def test_business_days_count_one_week():
    assert business_days_count(date(2026, 1, 5), date(2026, 1, 9)) == 5


def test_business_days_count_with_new_year_holiday():
    # 2026-01-01(신정) ~ 2026-01-09(금)
    # 영업일: 1/2, 1/5, 1/6, 1/7, 1/8, 1/9 = 6일
    assert business_days_count(date(2026, 1, 1), date(2026, 1, 9)) == 6


def test_business_days_between_reverse_range_returns_empty():
    # end < start → 빈 리스트
    assert business_days_between(date(2026, 1, 9), date(2026, 1, 5)) == []
    assert business_days_count(date(2026, 1, 9), date(2026, 1, 5)) == 0


def test_business_days_between_same_day_business():
    d = date(2026, 1, 5)  # 월
    assert business_days_between(d, d) == [d]


def test_business_days_between_same_day_holiday():
    d = date(2026, 1, 1)  # 신정
    assert business_days_between(d, d) == []


# ---------------------------------------------------------------------------
# 설 연휴 구간 확인
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("d", [
    date(2026, 2, 14),  # 토
    date(2026, 2, 15),  # 일
    date(2026, 2, 16),  # 설 전일 (휴장)
    date(2026, 2, 17),  # 설날
    date(2026, 2, 18),  # 설 익일
])
def test_lunar_new_year_block_all_non_business(d):
    assert is_business_day(d) is False


def test_lunar_new_year_days_in_holiday_set():
    assert date(2026, 2, 16) in KRX_HOLIDAYS_2026
    assert date(2026, 2, 17) in KRX_HOLIDAYS_2026
    assert date(2026, 2, 18) in KRX_HOLIDAYS_2026


# ---------------------------------------------------------------------------
# 추석 연휴 확인
# ---------------------------------------------------------------------------

def test_chuseok_block_non_business():
    # 2026-09-24(목) ~ 2026-09-26(토) 모두 비영업일
    for d in (date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26)):
        assert is_business_day(d) is False


def test_chuseok_next_business_day():
    # 2026-09-23(수) 다음 영업일은 2026-09-28(월)
    assert next_business_day(date(2026, 9, 23)) == date(2026, 9, 28)
