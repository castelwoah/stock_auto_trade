"""KST 단일 기준 — 호스트 TZ 와 무관해야 한다 (결정 2026-10-19)."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.common.clock import now_kst, today_kst
from src.persistence.models import Base, RiskEvent, Trade


@pytest.fixture
def host_tz_utc():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_now_kst_is_utc_plus_9_regardless_of_host(host_tz_utc):
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    diff = now_kst() - utc
    assert timedelta(hours=8, minutes=59) < diff < timedelta(hours=9, minutes=1)
    assert now_kst().tzinfo is None
    assert today_kst() == now_kst().date()


def test_db_defaults_store_kst(host_tz_utc):
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(Trade(symbol="A", side="BUY", qty=1, price=1.0, idempotency_key="k"))
        s.add(RiskEvent(event_type="x"))
        s.flush()
        kst = now_kst()
        for row in (s.query(Trade).one(), s.query(RiskEvent).one()):
            assert abs((row.ts - kst).total_seconds()) < 5
