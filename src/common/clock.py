"""시각 단일 기준: KST (결정 2026-10-19).

DB 에는 tz 정보 없는(naive) KST 시각을 저장한다. 호스트 TZ(UTC 등)와 무관하게
동일한 값을 내도록 항상 Asia/Seoul 로 변환한 뒤 tzinfo 를 제거한다.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def today_kst() -> date:
    return now_kst().date()
