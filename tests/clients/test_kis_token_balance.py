"""TokenStore · 토큰 발급 · 잔고/현금/현재가 조회."""
import json
from datetime import datetime, timedelta

import pytest

from src.clients import kis as kis_mod
from src.clients.kis import KisClient, TokenStore
from src.clients.rate_limiter import TokenBucket

from tests.clients.conftest import FakeResponse, make_settings


@pytest.fixture
def fixed_now(monkeypatch):
    now = datetime(2026, 10, 20, 8, 0)
    monkeypatch.setattr(kis_mod, "now_kst", lambda: now)
    return now


# --- TokenStore ---------------------------------------------------------------

def test_token_store_roundtrip(tmp_path, fixed_now):
    st = TokenStore(tmp_path)
    assert st.read() is None
    st.write("tok-1", "2026-10-21 08:00:00")
    cached = st.read()
    assert cached.value == "tok-1" and st.current_path().name == "KIS20261020"


def test_token_store_expired_or_garbage(tmp_path, fixed_now):
    st = TokenStore(tmp_path)
    st.current_path().write_text("token: t\nvalid-date: 2026-10-20 07:00:00\n", encoding="utf-8")
    assert st.read() is None                                   # 만료
    st.current_path().write_text("token: t\nvalid-date: not-a-date\n", encoding="utf-8")
    assert st.read() is None
    st.current_path().write_text("", encoding="utf-8")
    assert st.read() is None


def test_token_store_moves_only_past_token_files(tmp_path, fixed_now):
    (tmp_path / "KIS20261019").write_text("x")
    (tmp_path / "KIS20261020").write_text("x")
    (tmp_path / "KIS2026XXXX").write_text("x")
    (tmp_path / "other.txt").write_text("x")
    (tmp_path / "subdir").mkdir()
    st = TokenStore(tmp_path)
    assert st.move_expired() == 1
    assert (tmp_path / "KIS20261020").exists() and not (tmp_path / "KIS20261019").exists()
    assert len(list((tmp_path / "expired").iterdir())) == 1


# --- HTTP ---------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch, fixed_now):
    calls, queue = [], []

    def fake(method):
        def _fn(url, headers=None, params=None, data=None, timeout=None):
            calls.append({"method": method, "url": url, "headers": headers or {},
                          "params": params, "body": json.loads(data) if data else None})
            return queue.pop(0)
        return _fn

    monkeypatch.setattr(kis_mod.requests, "get", fake("GET"))
    monkeypatch.setattr(kis_mod.requests, "post", fake("POST"))
    c = KisClient(make_settings(), bucket=TokenBucket(capacity=100, refill_rate=100.0),
                  token_store=TokenStore(tmp_path))
    return c, calls, queue


def test_access_token_issued_then_cached(client):
    c, calls, queue = client
    queue.append(FakeResponse({"access_token": "AT", "access_token_token_expired": "2026-10-21 08:00:00"}))
    assert c.get_access_token() == "AT"
    assert calls[0]["url"].endswith("/oauth2/tokenP")
    assert c.get_access_token() == "AT" and len(calls) == 1      # 파일 캐시 사용


def test_get_current_price_and_error(client):
    c, calls, queue = client
    queue.append(FakeResponse({"access_token": "AT", "access_token_token_expired": "2026-10-21 08:00:00"}))
    queue.append(FakeResponse({"rt_cd": "0", "output": {"stck_prpr": "70100"}}))
    assert c.get_current_price("005930") == 70100
    assert calls[1]["headers"]["authorization"] == "Bearer AT"
    queue.append(FakeResponse({"rt_cd": "1", "msg1": "x"}))
    with pytest.raises(RuntimeError):
        c.get_current_price("005930")


def test_cash_balance_uses_env_tr(client):
    c, calls, queue = client
    c._access_token = "AT"
    queue.append(FakeResponse({"output": {"ord_psbl_cash": "1234567"}}))
    assert c.get_cash_balance() == 1_234_567
    assert calls[0]["headers"]["tr_id"] == "TTTC8908R"
    queue.append(FakeResponse({"msg1": "err"}))
    with pytest.raises(RuntimeError):
        c.get_cash_balance()


def test_stock_balance_parses_holdings(client):
    c, calls, queue = client
    c._access_token = "AT"
    queue.append(FakeResponse({
        "output1": [{"pdno": "005930", "hldg_qty": "10"}, {"pdno": "000660", "hldg_qty": "0"},
                    {"pdno": "035720", "hldg_qty": "bad"}],
        "output2": [{"tot_evlu_amt": "5000000"}],
    }))
    holdings, ev = c.get_stock_balance()
    assert holdings == {"005930": 10} and ev["tot_evlu_amt"] == "5000000"
    assert calls[0]["headers"]["tr_id"] == "TTTC8434R"
    queue.append(FakeResponse({"msg1": "err"}))
    with pytest.raises(RuntimeError):
        c.get_stock_balance()


def test_prev_day_ohlc_empty_and_fallback(client):
    c, _, queue = client
    c._access_token = "AT"
    queue.append(FakeResponse({"output": []}))
    assert c.get_prev_day_ohlc("005930") is None
    # 날짜 없는 행만 있으면 rows[1] 로 대체
    queue.append(FakeResponse({"output": [{"stck_oprc": "1"}, {"stck_hgpr": "2", "stck_lwpr": "1"}]}))
    out = c.get_prev_day_ohlc("005930")
    assert out["high"] == "2" and out["open_today"] == "0"
    queue.append(FakeResponse({"output": [{"stck_oprc": "1"}]}))
    assert c.get_prev_day_ohlc("005930") is None
