"""KRX (Korea Exchange) business day calendar.

Python 3.10 stdlib only. No external deps (pandas/numpy/pytz/holidays 금지).

Holiday source reference (do NOT fetch at runtime; maintained manually):
- KRX 공식 휴장일 공지: https://open.krx.co.kr/
- 공공데이터포털 "특일 정보" API 문서 (수동 참조용)

2027년 이후 확장:
    KRX_HOLIDAYS_2027 등 연도별 frozenset 을 추가하고
    _ALL_HOLIDAYS 에 union 하라.
"""
from __future__ import annotations

from datetime import date, timedelta

# ---------------------------------------------------------------------------
# 2026 KRX 휴장일 (확정분 + 관례적 추정)
# ---------------------------------------------------------------------------
KRX_HOLIDAYS_2026: frozenset[date] = frozenset({
    date(2026, 1, 1),    # 신정
    # 설 연휴 (음력 1/1 = 2026-02-17 화).
    # 2026-02-16 월 ~ 2026-02-18 수 3일 휴장.
    date(2026, 2, 16),   # 설 연휴 (전일)
    date(2026, 2, 17),   # 설날
    date(2026, 2, 18),   # 설 연휴 (익일)
    # 삼일절: 2026-03-01 일요일 → 3/2 월 대체공휴일
    date(2026, 3, 2),    # 삼일절 대체공휴일
    date(2026, 5, 5),    # 어린이날 (화)
    # 석가탄신일: 2026년 음력 4/8 = 2026-05-24 일요일 → 5/25 월 대체
    date(2026, 5, 25),   # 석가탄신일 대체공휴일
    date(2026, 6, 6),    # 현충일 (토) — KRX 휴장 (토요일이라 어차피 휴장)
    date(2026, 8, 15),   # 광복절 (토) — 토요일
    date(2026, 8, 17),   # 광복절 대체공휴일 (월) — 공휴일법 개정 반영
    # 추석: 2026년 음력 8/15 = 2026-09-25 금.
    # 연휴 9/24 목 ~ 9/26 토. 9/26 은 토요일이라 어차피 휴장이지만
    # 명시적으로 포함한다.
    date(2026, 9, 24),   # 추석 전날 (목)
    date(2026, 9, 25),   # 추석 (금)
    date(2026, 9, 26),   # 추석 익일 (토)
    date(2026, 10, 3),   # 개천절 (토)
    date(2026, 10, 5),   # 개천절 대체공휴일 (월)
    date(2026, 10, 9),   # 한글날 (금)
    date(2026, 12, 25),  # 기독탄신일 (금)
    date(2026, 12, 31),  # 연말 휴장일 (KRX 관례: 12/31 휴장, 마지막 거래일 12/30)
})

# 2027년 이후는 확정되는 대로 채워 넣는다. 비어 있어도 평일은 기본 영업일.
KRX_HOLIDAYS_2027: frozenset[date] = frozenset()

# 통합 휴장일 (연도별 frozenset 의 union).
_ALL_HOLIDAYS: frozenset[date] = (
    KRX_HOLIDAYS_2026 | KRX_HOLIDAYS_2027
)

# 무한 루프 방지 상한선.
_MAX_SEARCH_DAYS = 60


def is_weekend(d: date) -> bool:
    """토요일(5) 또는 일요일(6) 이면 True."""
    return d.weekday() >= 5


def is_business_day(d: date) -> bool:
    """평일이고 KRX 휴장일이 아니면 True."""
    if is_weekend(d):
        return False
    if d in _ALL_HOLIDAYS:
        return False
    return True


def next_business_day(d: date) -> date:
    """d 다음 영업일 (d 자체는 제외)."""
    candidate = d + timedelta(days=1)
    for _ in range(_MAX_SEARCH_DAYS):
        if is_business_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    raise ValueError(
        f"next_business_day: {_MAX_SEARCH_DAYS}일 내 영업일을 찾지 못함 (start={d})"
    )


def prev_business_day(d: date) -> date:
    """d 이전 영업일 (d 자체는 제외)."""
    candidate = d - timedelta(days=1)
    for _ in range(_MAX_SEARCH_DAYS):
        if is_business_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise ValueError(
        f"prev_business_day: {_MAX_SEARCH_DAYS}일 내 영업일을 찾지 못함 (start={d})"
    )


def business_days_between(start: date, end: date) -> list[date]:
    """start ~ end 영업일 목록 (양 끝 포함). end < start 면 빈 리스트."""
    if end < start:
        return []
    result: list[date] = []
    cursor = start
    while cursor <= end:
        if is_business_day(cursor):
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def business_days_count(start: date, end: date) -> int:
    """start ~ end 영업일 개수 (양 끝 포함). end < start 면 0."""
    return len(business_days_between(start, end))
