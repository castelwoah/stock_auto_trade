"""KIS HTTP 오류 메시지(쿼리 미포함) · 조회 재시도 · 주문 비재시도."""
import pytest
import requests

from src.clients import kis as kis_mod
from src.clients.kis import KisHttpError

from tests.clients.conftest import FakeResponse


class Resp(FakeResponse):
    def __init__(self, payload, status=200, text=""):
        super().__init__(payload)
        self.status_code = status
        self.text = text


@pytest.fixture
def seq(monkeypatch, live_client):
    calls: list[str] = []
    script: list = []
    sleeps: list[float] = []

    def _req(url, headers=None, params=None, data=None, timeout=None):
        calls.append(url)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(kis_mod.requests, "get", _req)
    monkeypatch.setattr(kis_mod.requests, "post", _req)
    live_client._sleep = sleeps.append
    return live_client, script, calls, sleeps


def test_5xx_then_ok_is_retried(seq):
    c, script, calls, sleeps = seq
    script += [Resp({}, 500, "err"), Resp({"rt_cd": "0", "output": {"stck_prpr": "70100"}})]
    assert c.get_current_price("005930") == 70100
    assert len(calls) == 2 and sleeps == [0.5]


def test_rate_limit_body_retried(seq):
    c, script, calls, sleeps = seq
    script += [Resp({"rt_cd": "1", "msg_cd": "EGW00201"}),
               Resp({"rt_cd": "1", "msg_cd": "EGW00201"}),
               Resp({"rt_cd": "0", "output": {"stck_prpr": "1"}})]
    assert c.get_current_price("005930") == 1 and sleeps == [0.5, 1.0]


def test_persistent_5xx_raises_without_query(seq):
    c, script, calls, _ = seq
    script += [Resp({}, 500, '{"msg1":"서버 오류"}')] * 3
    with pytest.raises(KisHttpError) as ei:
        c.get_stock_balance()
    msg = str(ei.value)
    assert "12345678" not in msg and "CANO" not in msg
    assert "inquire-balance" in msg and "서버 오류" in msg and ei.value.status == 500
    assert len(calls) == 3


def test_connection_error_retried_then_wrapped(seq):
    c, script, calls, _ = seq
    err = requests.ConnectionError("conn refused for url https://x/y?CANO=12345678")
    script += [err, err, err]
    with pytest.raises(KisHttpError) as ei:
        c.get_cash_balance()
    assert "12345678" not in str(ei.value) and len(calls) == 3


def test_4xx_not_retried(seq):
    c, script, calls, _ = seq
    script += [Resp({}, 403, "forbidden")]
    with pytest.raises(KisHttpError):
        c.get_current_price("005930")
    assert len(calls) == 1


def test_order_post_never_retried(seq):
    c, script, calls, _ = seq
    script += [Resp({}, 500, "err")]
    with pytest.raises(KisHttpError):
        c.order("005930", 1, "BUY", unit_price="70000")
    assert len(calls) == 1
