"""scripts/smoke_live.py — 판정 함수 + 가짜 KIS 서버로 전체 흐름."""
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from src.clients import kis as kis_mod

from tests.clients.conftest import FakeResponse, make_settings

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "smoke_live.py"
_spec = importlib.util.spec_from_file_location("smoke_live", _PATH)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["smoke_live"] = smoke
_spec.loader.exec_module(smoke)

MARKET_NOW = datetime(2026, 10, 20, 10, 0)      # 화요일 장중


# --- 판정 함수 -----------------------------------------------------------------

def test_infer_market_cap_unit():
    # 삼성전자 근사: 70,000원 × 5,969,782,550주 ≈ 417.9조 = 4,178,848 억원
    assert smoke.infer_market_cap_unit(70_000, 5_969_782_550, 4_178_848)[0] == "억원"
    assert smoke.infer_market_cap_unit(70_000, 5_969_782_550, 417_884_778)[0] == "백만원"
    assert smoke.infer_market_cap_unit(70_000, 5_969_782_550, 1_234)[0] == "unknown"
    assert smoke.infer_market_cap_unit(0, 1, 1) == ("unknown", 0.0)


def test_choose_probe_price_respects_lower_limit_and_tick():
    assert smoke.choose_probe_price(70_000, 49_000) == 63_000
    assert smoke.choose_probe_price(70_000, 65_000) == 65_000      # 하한가 미만 방지
    assert smoke.choose_probe_price(12_345, 0) % 10 == 0           # 1만~2만원 구간 10원 단위


def test_in_order_window():
    assert smoke.in_order_window(MARKET_NOW)
    assert not smoke.in_order_window(datetime(2026, 10, 20, 15, 25))
    assert not smoke.in_order_window(datetime(2026, 10, 18, 10, 0))  # 일요일


def test_find_rows():
    rows = [{"odno": "1"}, {"odno": "2", "orgn_odno": "1"}, {"odno": "3"}]
    orig, children = smoke.find_rows(rows, "1")
    assert orig == {"odno": "1"} and children == [{"odno": "2", "orgn_odno": "1"}]


# --- 가짜 KIS 서버 ---------------------------------------------------------------

class FakeKis:
    def __init__(self, *, ord_orgno="91252", ccld_orgno="91252", filled=0,
                 hts_avls="4178848", reject_order=False):
        self.ord_orgno, self.ccld_orgno, self.filled = ord_orgno, ccld_orgno, filled
        self.hts_avls, self.reject_order = hts_avls, reject_order
        self.cancelled = False
        self.orders: list[dict] = []
        self.cancels: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        tr = headers["tr_id"]
        if tr == "FHKST01010100":
            return FakeResponse({"rt_cd": "0", "output": {
                "stck_prpr": "70000", "lstn_stcn": "5969782550", "hts_avls": self.hts_avls,
                "stck_llam": "49000", "vi_cls_code": "N", "mrkt_warn_cls_code": "00",
                "temp_stop_yn": "N", "mang_issu_cls_code": "N"}})
        if tr == "FHPUP02100000":
            return FakeResponse({"rt_cd": "0", "output": {"bstp_nmix_prdy_ctrt": "-0.52"}})
        if tr == "TTTC8434R":
            return FakeResponse({"output1": [], "output2": [{"tot_evlu_amt": "10000000"}]})
        if tr == "TTTC8908R":
            return FakeResponse({"output": {"ord_psbl_cash": "10000000"}})
        if tr == "TTTC0081R":
            rows = []
            if self.orders:
                rows.append({"odno": "0000123", "pdno": "005930", "sll_buy_dvsn_cd": "02",
                             "ord_qty": "1", "tot_ccld_qty": str(self.filled),
                             "rmn_qty": "0" if self.cancelled else str(1 - self.filled),
                             "cncl_yn": "N", "ord_gno_brno": self.ccld_orgno})
            if self.cancelled:
                rows.append({"odno": "0000124", "orgn_odno": "0000123", "cncl_yn": "Y",
                             "ord_qty": "1", "ord_gno_brno": self.ccld_orgno})
            return FakeResponse({"rt_cd": "0", "output1": rows})
        raise AssertionError(tr)

    def post(self, url, headers=None, data=None, timeout=None):
        if url.endswith("/oauth2/tokenP"):
            return FakeResponse({"access_token": "AT",
                                 "access_token_token_expired": "2026-10-21 08:00:00"})
        body, tr = json.loads(data), headers["tr_id"]
        if tr == "TTTC0012U":
            if self.reject_order:
                return FakeResponse({"rt_cd": "1", "msg_cd": "APBK0013", "msg1": "주문 거절"})
            self.orders.append(body)
            return FakeResponse({"rt_cd": "0", "output": {
                "ODNO": "0000123", "KRX_FWDG_ORD_ORGNO": self.ord_orgno, "ORD_TMD": "100000"}})
        if tr == "TTTC0013U":
            self.cancels.append(body)
            self.cancelled = True
            return FakeResponse({"rt_cd": "0", "output": {}})
        raise AssertionError(tr)


@pytest.fixture
def run(tmp_path, monkeypatch):
    def _run(server: FakeKis, argv, *, now=MARKET_NOW, **settings_over):
        settings = make_settings(TOKEN_DIR=tmp_path / "tok", **settings_over)
        monkeypatch.setattr(smoke, "get_settings", lambda: settings)
        monkeypatch.setattr(smoke, "now_kst", lambda: now)
        monkeypatch.setattr(kis_mod, "now_kst", lambda: now)
        monkeypatch.setattr(smoke, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(smoke.time, "sleep", lambda s: None)
        monkeypatch.setattr(kis_mod.requests, "get", server.get)
        monkeypatch.setattr(kis_mod.requests, "post", server.post)
        rc = smoke.main(argv)
        out = json.loads(next((tmp_path / "logs").glob("smoke-*.json")).read_text(encoding="utf-8"))
        return rc, {(c["id"], c["status"]) for c in out}, out
    return _run


def test_read_only_run_no_orders(run):
    server = FakeKis()
    rc, ids, out = run(server, [])
    assert rc == 0 and server.orders == []
    assert ("R1", "PASS") in ids and ("R3", "PASS") in ids and ("R4", "PASS") in ids
    dump = json.dumps(out)
    assert "12345678" not in dump and "AT" not in json.dumps([c.get("msg") for c in out])


def test_hts_avls_unit_mismatch_fails(run):
    rc, ids, _ = run(FakeKis(hts_avls="417884778"), [])
    assert rc == 1 and ("R1", "FAIL") in ids


def test_order_probe_buy_then_cancel(run):
    server = FakeKis()
    rc, ids, out = run(server, ["--order"])
    assert rc == 0
    assert server.orders[0]["ORD_QTY"] == "1" and server.orders[0]["ORD_UNPR"] == "63000"
    assert server.cancels[0]["ORGN_ODNO"] == "0000123"
    assert server.cancels[0]["KRX_FWDG_ORD_ORGNO"] == "91252"
    assert {("O1", "PASS"), ("O2", "PASS"), ("O3", "PASS")} <= ids
    assert any(c["msg"] == "취소 행" for c in out)


def test_orgno_mismatch_is_reported_and_still_cancels(run):
    server = FakeKis(ccld_orgno="00950")
    rc, ids, _ = run(server, ["--order"])
    assert rc == 1 and ("O2", "FAIL") in ids and server.cancels


def test_filled_probe_is_fail(run):
    rc, ids, _ = run(FakeKis(filled=1), ["--order"])
    assert rc == 1 and ("O3", "FAIL") in ids


def test_order_rejected(run):
    server = FakeKis(reject_order=True)
    rc, ids, _ = run(server, ["--order"])
    assert rc == 1 and ("O1", "FAIL") in ids and server.cancels == []


@pytest.mark.parametrize("kwargs, argv", [
    ({"now": datetime(2026, 10, 20, 15, 30)}, ["--order"]),          # 장 마감 후
    ({"SHADOW_MODE": False}, ["--order"]),                            # 데몬 실주문 중
    ({}, ["--order", "--max-krw", "50000"]),                          # 주문가 상한 초과
])
def test_order_guards_skip_sending(run, kwargs, argv):
    server = FakeKis()
    rc, ids, _ = run(server, argv, **kwargs)
    assert rc == 1 and ("O1", "FAIL") in ids and server.orders == []


def test_rate_test(run):
    rc, ids, _ = run(FakeKis(), ["--rate-test", "--rate-n", "5"],
                     TOKEN_BUCKET_REFILL_RATE=1000.0, TOKEN_BUCKET_CAPACITY=1000)
    assert rc == 0 and ("R5", "PASS") in ids
