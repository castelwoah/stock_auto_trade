"""KisClient 주문·취소·체결조회·시세 — HTTP 는 monkeypatch 로 대체."""
import json

import pytest
import requests

from src.clients import kis as kis_mod
from src.clients.kis import KisOrderRejected, OrderAck

from tests.clients.conftest import FakeResponse


@pytest.fixture
def http(monkeypatch):
    calls = []
    queue = []

    def _record(method):
        def _fn(url, headers=None, params=None, data=None, timeout=None):
            calls.append({
                "method": method, "url": url, "headers": headers or {},
                "params": params, "body": json.loads(data) if data else None,
            })
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return _fn

    monkeypatch.setattr(kis_mod.requests, "get", _record("GET"))
    monkeypatch.setattr(kis_mod.requests, "post", _record("POST"))
    return calls, queue


# --- order -------------------------------------------------------------------

def test_order_success_returns_ack_with_orgno(live_client, http):
    calls, queue = http
    queue.append(FakeResponse({"rt_cd": "0", "output": {
        "ODNO": "0000111", "KRX_FWDG_ORD_ORGNO": "91252", "ORD_TMD": "093000"}}))
    ack = live_client.order("005930", 3, "BUY", unit_price="70000")
    assert ack == OrderAck(odno="0000111", orgno="91252", ord_tmd="093000")
    c = calls[0]
    assert c["headers"]["tr_id"] == "TTTC0012U"
    assert c["body"]["EXCG_ID_DVSN_CD"] == "KRX"
    assert c["body"]["ORD_DVSN"] == "00"
    assert c["body"]["ORD_UNPR"] == "70000"


def test_order_sell_uses_sell_tr(live_client, http):
    calls, queue = http
    queue.append(FakeResponse({"rt_cd": "0", "output": {"ODNO": "1", "KRX_FWDG_ORD_ORGNO": "2"}}))
    live_client.order("005930", 1, "SELL", unit_price="70000")
    assert calls[0]["headers"]["tr_id"] == "TTTC0011U"


def test_order_rejected_raises(live_client, http):
    _, queue = http
    queue.append(FakeResponse({"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "주문가능금액 부족"}))
    with pytest.raises(KisOrderRejected) as ei:
        live_client.order("005930", 1, "BUY", unit_price="70000")
    assert ei.value.msg_cd == "APBK0919"


def test_order_network_error_propagates(live_client, http):
    _, queue = http
    queue.append(requests.Timeout("timeout"))
    with pytest.raises(requests.RequestException):
        live_client.order("005930", 1, "BUY", unit_price="70000")


@pytest.mark.parametrize("dvsn", ["01", "11", ""])
def test_order_non_limit_refused_before_http(live_client, http, dvsn):
    calls, _ = http
    with pytest.raises(ValueError, match="not allowed"):
        live_client.order("005930", 1, "BUY", ord_dvsn=dvsn, unit_price="70000")
    assert calls == []


@pytest.mark.parametrize("qty,price", [(0, "70000"), (1, "0"), (-1, "100")])
def test_order_requires_positive_qty_and_price(live_client, http, qty, price):
    calls, _ = http
    with pytest.raises(ValueError):
        live_client.order("005930", qty, "BUY", unit_price=price)
    assert calls == []


def test_order_bad_side(live_client, http):
    with pytest.raises(ValueError):
        live_client.order("005930", 1, "HOLD", unit_price="100")


# --- cancel ------------------------------------------------------------------

def test_cancel_order_body(live_client, http):
    calls, queue = http
    queue.append(FakeResponse({"rt_cd": "0", "output": {}}))
    live_client.cancel_order("0000111", "91252")
    c = calls[0]
    assert c["headers"]["tr_id"] == "TTTC0013U"
    assert c["url"].endswith("order-rvsecncl")
    assert c["body"]["ORGN_ODNO"] == "0000111"
    assert c["body"]["KRX_FWDG_ORD_ORGNO"] == "91252"
    assert c["body"]["RVSE_CNCL_DVSN_CD"] == "02"
    assert c["body"]["QTY_ALL_ORD_YN"] == "Y"


def test_cancel_rejected_raises(live_client, http):
    _, queue = http
    queue.append(FakeResponse({"rt_cd": "1", "msg_cd": "X", "msg1": "이미 체결"}))
    with pytest.raises(KisOrderRejected):
        live_client.cancel_order("1", "2")


def test_cancel_requires_ids(live_client, http):
    with pytest.raises(ValueError):
        live_client.cancel_order("1", "")


# --- daily ccld --------------------------------------------------------------

def _row(odno, filled, rmn, side="02", cncl="N", avg="1000"):
    return {"odno": odno, "pdno": "005930", "sll_buy_dvsn_cd": side, "ord_qty": "10",
            "tot_ccld_qty": str(filled), "avg_prvs": avg, "rmn_qty": str(rmn), "cncl_yn": cncl}


def test_inquire_today_orders_paginates(live_client, http):
    calls, queue = http
    queue.append(FakeResponse(
        {"rt_cd": "0", "output1": [_row("1", 10, 0)], "ctx_area_fk100": "F1", "ctx_area_nk100": "N1"},
        headers={"tr_cont": "M"},
    ))
    queue.append(FakeResponse(
        {"rt_cd": "0", "output1": [_row("2", 4, 6, side="01", cncl="Y")]},
        headers={"tr_cont": "D"},
    ))
    fills = live_client.inquire_today_orders("20261020")
    assert [f.odno for f in fills] == ["1", "2"]
    assert fills[0].side == "BUY" and fills[0].filled_qty == 10
    assert fills[1].side == "SELL" and fills[1].remaining_qty == 6 and fills[1].cancelled
    assert calls[0]["headers"]["tr_id"] == "TTTC0081R"
    assert "tr_cont" not in calls[0]["headers"]
    assert calls[1]["headers"]["tr_cont"] == "N"
    assert calls[1]["params"]["CTX_AREA_NK100"] == "N1"


def test_inquire_today_orders_error(live_client, http):
    _, queue = http
    queue.append(FakeResponse({"rt_cd": "1", "msg1": "err"}))
    with pytest.raises(RuntimeError):
        live_client.inquire_today_orders("20261020")


def test_parse_fill_handles_garbage():
    f = kis_mod._parse_fill({"odno": "9", "tot_ccld_qty": "x", "avg_prvs": None})
    assert f.filled_qty == 0 and f.avg_price == 0.0


# --- quotes ------------------------------------------------------------------

def test_get_quote_converts_market_cap(live_client, http):
    _, queue = http
    queue.append(FakeResponse({"rt_cd": "0", "output": {
        "stck_prpr": "70000", "hts_avls": "4000000", "acml_tr_pbmn": "123"}}))
    q = live_client.get_quote("005930")
    assert q["price"] == 70000.0
    assert q["market_cap"] == 4_000_000 * 100_000_000


def test_prev_day_ohlc_avg_trade_value(live_client, http, monkeypatch):
    _, queue = http
    rows = [{"stck_bsop_date": "29991231", "stck_oprc": "100"}]  # '오늘' 행 (제외)
    rows += [{"stck_bsop_date": f"2026100{i}", "stck_hgpr": "110", "stck_lwpr": "90",
              "stck_clpr": "100", "acml_vol": "1000"} for i in range(9, 0, -1)]
    queue.append(FakeResponse({"output": rows}))
    monkeypatch.setattr(kis_mod, "datetime", _FixedDT)
    out = live_client.get_prev_day_ohlc("005930")
    assert out["open_today"] == "100"
    assert out["date"] == "20261009"
    assert float(out["avg_trade_value_20d"]) == 100_000.0


class _FixedDT(kis_mod.datetime):
    @classmethod
    def today(cls):
        return cls(2026, 10, 20)
