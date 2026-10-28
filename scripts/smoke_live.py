#!/usr/bin/env python
"""실전(LIVE) 스모크 테스트 — Phase A 가정값 검증.

  .venv/bin/python scripts/smoke_live.py                    # 읽기 전용 (주문 없음)
  .venv/bin/python scripts/smoke_live.py --order            # + 1주 지정가 매수 → 즉시 취소 (장중만)
  옵션: --symbol 005930  --status-symbols 000000,111111  --rate-test  --max-krw 300000

확인 항목
  R1 hts_avls 단위        시총 = 현재가 × 상장주식수 와 비교해 억원 여부 판정
  R2 종목상태 원값         vi_cls_code / mrkt_warn_cls_code / temp_stop_yn / mang_issu_cls_code 등
  R3 지수 TR               FHPUP02100000 KOSPI·KOSDAQ 등락률
  R4 잔고·주문가능현금     TTTC8434R / TTTC8908R 응답 형식
  R5 초당 한도 (--rate-test) 프로파일 rps 로 연속 조회 시 EGW00201(초과) 발생 여부
  O1 주문 접수 (--order)    ODNO·KRX_FWDG_ORD_ORGNO 수신
  O2 orgno 일치             체결조회 ord_gno_brno == 주문 응답 KRX_FWDG_ORD_ORGNO
  O3 취소 접수·확인          취소 후 체결조회 원주문 행의 rmn_qty / cncl_yn 원값 (수동취소 감지 근거)

주문은 현재가 −10%(하한가 이상) 1주 매수라 정상 시장에선 체결되지 않는다. 체결되면
1주 보유 상태가 되므로 결과에 FAIL 로 표시하고 수동 매도를 안내한다.
OrderManager·DB·@risk_gate 를 거치지 않는 직접 호출이다 (DB 무변경). 데몬이 SHADOW_MODE
로 돌고 있을 때 실행하는 것을 전제로 한다.

결과는 화면 + logs/smoke-YYYYMMDD-HHMMSS.json 에 기록한다. 계좌번호·키·토큰은 기록하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.clients.kis import KisClient, KisOrderRejected  # noqa: E402
from src.common.clock import now_kst  # noqa: E402
from src.config import KisEnv, get_settings  # noqa: E402
from src.execution.krx_tick import round_to_tick  # noqa: E402
from src.scheduler.calendar import is_business_day  # noqa: E402

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
RATE_LIMIT_MSG_CD = "EGW00201"
QUOTE_FIELDS = (
    "stck_prpr", "hts_avls", "lstn_stcn", "stck_sdpr", "stck_oprc", "stck_llam", "stck_mxpr",
    "vi_cls_code", "mrkt_warn_cls_code", "temp_stop_yn", "mang_issu_cls_code",
    "invt_caful_yn", "sltr_yn", "iscd_stat_cls_code", "short_over_yn",
)
ORDER_DISCOUNT = 0.10          # 현재가 대비 −10%
MARKET_OPEN = (9, 0)
MARKET_CLOSE = (15, 20)        # 15:20 이후는 동시호가 — 주문 테스트 금지


# ---------------------------------------------------------------------------
# 결과 수집
# ---------------------------------------------------------------------------

@dataclass
class Report:
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, check_id: str, status: str, msg: str, **data: Any) -> None:
        self.checks.append({"id": check_id, "status": status, "msg": msg, **data})
        print(f"[{status:4}] {check_id:3} {msg}")
        for k, v in data.items():
            print(f"             {k}: {v}")

    @property
    def failed(self) -> bool:
        return any(c["status"] == FAIL for c in self.checks)


class RawRecorder:
    """KisClient 의 HTTP 응답 원문을 가로채 기록 (파싱 가정 검증용)."""

    def __init__(self, kis: KisClient) -> None:
        self.kis = kis
        self.last: dict[str, Any] = {}
        orig = kis._get_with_cont

        def _wrapped(path, tr_id, params, *, tr_cont=""):
            data, cont = orig(path, tr_id, params, tr_cont=tr_cont)
            self.last = data
            return data, cont

        kis._get_with_cont = _wrapped  # type: ignore[method-assign]

    def get(self, path: str, tr_id: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.kis._get(path, tr_id, params)


# ---------------------------------------------------------------------------
# 순수 판정 함수 (tests/scripts/test_smoke_live.py)
# ---------------------------------------------------------------------------

def infer_market_cap_unit(price: float, listed_shares: float, hts_avls: float) -> tuple[str, float]:
    """현재가 × 상장주식수 / hts_avls 비율로 hts_avls 단위 판정."""
    if price <= 0 or listed_shares <= 0 or hts_avls <= 0:
        return "unknown", 0.0
    ratio = price * listed_shares / hts_avls
    for unit, scale in (("억원", 1e8), ("백만원", 1e6), ("원", 1.0)):
        if abs(ratio / scale - 1.0) < 0.05:
            return unit, ratio
    return "unknown", ratio


def choose_probe_price(price: float, lower_limit: float) -> int:
    """체결되지 않을 매수 지정가: 현재가 −10%, 하한가 미만이면 하한가. 호가단위 올림."""
    target = price * (1.0 - ORDER_DISCOUNT)
    if lower_limit > 0:
        target = max(target, lower_limit)
    return round_to_tick(target, up=True)


def in_order_window(now) -> bool:
    hm = (now.hour, now.minute)
    return is_business_day(now.date()) and MARKET_OPEN <= hm < MARKET_CLOSE


def find_rows(rows: list[dict[str, Any]], odno: str) -> tuple[dict | None, list[dict]]:
    """체결조회 원문에서 원주문 행과 그 주문을 원주문으로 갖는 정정·취소 행."""
    orig = next((r for r in rows if r.get("odno") == odno), None)
    children = [r for r in rows if r.get("orgn_odno") == odno and r.get("odno") != odno]
    return orig, children


def _pick(d: dict[str, Any], keys) -> dict[str, Any]:
    return {k: d.get(k) for k in keys if k in d}


# ---------------------------------------------------------------------------
# 단계
# ---------------------------------------------------------------------------

def check_quotes(rec: RawRecorder, rep: Report, symbol: str, status_symbols: list[str]) -> dict:
    main_out: dict = {}
    for i, sym in enumerate([symbol, *status_symbols]):
        try:
            data = rec.get(
                "uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": sym},
            )
        except Exception as exc:  # noqa: BLE001
            rep.add("R2", FAIL, f"{sym} 시세 조회 실패: {exc}")
            continue
        out = data.get("output") or {}
        if data.get("rt_cd") != "0" or not out:
            rep.add("R2", FAIL, f"{sym} 시세 응답 오류", msg_cd=data.get("msg_cd"), msg1=data.get("msg1"))
            continue
        if i == 0:
            main_out = out
            unit, ratio = infer_market_cap_unit(
                float(out.get("stck_prpr") or 0), float(out.get("lstn_stcn") or 0),
                float(out.get("hts_avls") or 0),
            )
            rep.add("R1", PASS if unit == "억원" else FAIL,
                    f"hts_avls 단위 = {unit} (코드 가정: 억원)",
                    hts_avls=out.get("hts_avls"), ratio=f"{ratio:,.0f}")
        rep.add("R2", INFO, f"{sym} 종목상태 원값", **_pick(out, QUOTE_FIELDS[3:]))
    return main_out


def check_index(kis: KisClient, rep: Report) -> None:
    for code, name in (("0001", "KOSPI"), ("1001", "KOSDAQ")):
        try:
            rep.add("R3", PASS, f"{name} 등락률 {kis.get_index_change_pct(code):+.2f}%")
        except Exception as exc:  # noqa: BLE001
            rep.add("R3", FAIL, f"{name} 지수 조회 실패: {exc}")


def check_balance(kis: KisClient, rep: Report) -> int:
    cash = 0
    try:
        holdings, ev = kis.get_stock_balance()
        rep.add("R4", PASS, f"잔고 조회 — 보유 {len(holdings)}종목",
                tot_evlu_amt=ev.get("tot_evlu_amt"))
        if holdings:
            rep.add("R4", WARN, "봇 외 보유 종목이 있으면 RECONCILE_MANAGED_ONLY: true 필요",
                    symbols=sorted(holdings))
    except Exception as exc:  # noqa: BLE001
        rep.add("R4", FAIL, f"잔고 조회 실패: {exc}")
    try:
        cash = kis.get_cash_balance()
        rep.add("R4", PASS, f"주문가능현금 {cash:,}원")
    except Exception as exc:  # noqa: BLE001
        rep.add("R4", FAIL, f"주문가능현금 조회 실패: {exc}")
    return cash


def check_rate(rec: RawRecorder, rep: Report, symbol: str, n: int) -> None:
    """버킷 속도(프로파일 rps)로 n회 연속 조회 — KIS 가 초과로 거절하는지."""
    limited = errors = 0
    rec.kis.get_retry_delays = ()      # 클라이언트 재시도가 초과 응답을 가리지 않게
    t0 = time.monotonic()
    for _ in range(n):
        try:
            data = rec.get(
                "uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": symbol},
            )
            if data.get("msg_cd") == RATE_LIMIT_MSG_CD:
                limited += 1
            elif data.get("rt_cd") != "0":
                errors += 1
        except Exception:  # noqa: BLE001
            errors += 1
    dt = time.monotonic() - t0
    rps = rec.kis.bucket.refill_rate
    status = PASS if limited == errors == 0 else (FAIL if limited else WARN)
    rep.add("R5", status, f"{n}회 / {dt:.1f}s (설정 {rps} rps) — 초과거절 {limited}, 기타오류 {errors}")


def probe_order(kis: KisClient, rec: RawRecorder, rep: Report, symbol: str,
                quote: dict, cash: int, max_krw: int) -> None:
    price = float(quote.get("stck_prpr") or 0)
    llam = float(quote.get("stck_llam") or 0)
    if price <= 0:
        rep.add("O1", FAIL, "현재가 없음 — 주문 생략")
        return
    px = choose_probe_price(price, llam)
    if px > max_krw:
        rep.add("O1", FAIL, f"주문가 {px:,}원 > --max-krw {max_krw:,} — 주문 생략")
        return
    if cash and px > cash:
        rep.add("O1", FAIL, f"주문가 {px:,}원 > 주문가능현금 — 주문 생략")
        return

    try:
        ack = kis.order(symbol, 1, "BUY", unit_price=str(px))
    except KisOrderRejected as exc:
        rep.add("O1", FAIL, f"주문 거절 {exc}", price=px)
        return
    rep.add("O1", PASS if ack.odno and ack.orgno else FAIL,
            f"매수 1주 @{px:,} 접수", odno=ack.odno, orgno=ack.orgno, ord_tmd=ack.ord_tmd)
    if not ack.odno:
        return

    day = now_kst().strftime("%Y%m%d")
    try:
        time.sleep(1.5)
        fills = kis.inquire_today_orders(day)
        f = next((x for x in fills if x.odno == ack.odno), None)
        if f is None:
            rep.add("O2", FAIL, "체결조회에서 주문을 찾지 못함 (unknown 복구 경로 불가)")
        else:
            rep.add("O2", PASS if f.orgno == ack.orgno else FAIL,
                    "ord_gno_brno == KRX_FWDG_ORD_ORGNO",
                    ord_gno_brno=f.orgno, order_orgno=ack.orgno,
                    filled_qty=f.filled_qty, remaining_qty=f.remaining_qty)
    finally:
        _cancel_and_confirm(kis, rec, rep, ack, day)


def _cancel_and_confirm(kis: KisClient, rec: RawRecorder, rep: Report, ack, day: str) -> None:
    try:
        kis.cancel_order(ack.odno, ack.orgno)
        rep.add("O3", PASS, "취소 접수")
    except Exception as exc:  # noqa: BLE001
        rep.add("O3", FAIL, f"취소 실패: {exc} — HTS/MTS 에서 주문 {ack.odno} 직접 취소 필요")
        return
    time.sleep(1.5)
    kis.inquire_today_orders(day)
    orig, children = find_rows(rec.last.get("output1") or [], ack.odno)
    keys = ("odno", "orgn_odno", "ord_qty", "tot_ccld_qty", "rmn_qty", "cncl_yn",
            "cncl_cfrm_qty", "ord_gno_brno", "sll_buy_dvsn_cd")
    if orig is None:
        rep.add("O3", WARN, "취소 후 체결조회 첫 페이지에 원주문 행 없음 — HTS 로 취소 확인")
        return
    filled = int(float(orig.get("tot_ccld_qty") or 0))
    rep.add("O3", FAIL if filled else PASS,
            "원주문 행 (체결 0 · 잔량 0 기대)" if not filled
            else "체결됨 — 1주 보유 상태, 수동 매도 필요", **_pick(orig, keys))
    for c in children:
        rep.add("O3", INFO, "취소 행", **_pick(c, keys))
    rep.add("O3", INFO,
            "수동취소 감지(OrderFill.cancelled)는 cncl_yn=='Y' 또는 잔량 0·체결<주문수량 — 위 원값으로 확인")


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="005930", help="시세·주문 대상 (기본 삼성전자)")
    ap.add_argument("--status-symbols", default="",
                    help="상태 원값만 볼 추가 종목 (쉼표 구분, 예: 투자경고·VI 발동 종목)")
    ap.add_argument("--order", action="store_true", help="1주 지정가 매수 → 취소 실행 (실주문)")
    ap.add_argument("--max-krw", type=int, default=300_000, help="주문가 상한 (기본 30만원)")
    ap.add_argument("--rate-test", action="store_true", help="초당 한도 측정 (데몬 tick 과 겹치지 않게)")
    ap.add_argument("--rate-n", type=int, default=40)
    args = ap.parse_args(argv)

    settings = get_settings()
    rep = Report()
    now = now_kst()
    print(f"== smoke {now:%Y-%m-%d %H:%M:%S} KST  KIS_ENV={settings.KIS_ENV.value}"
          f"  SHADOW_MODE={settings.SHADOW_MODE}  rps={settings.kis_profile.rate_per_sec}")
    if settings.KIS_ENV != KisEnv.LIVE:
        rep.add("ENV", WARN, "KIS_ENV 가 LIVE 가 아님 — 실전 가정 검증이 아님")

    kis = KisClient(settings)
    rec = RawRecorder(kis)
    try:
        kis.get_access_token()
        rep.add("TOK", PASS, "토큰 확보 (일별 캐시 공유)")
    except Exception as exc:  # noqa: BLE001
        rep.add("TOK", FAIL, f"토큰 발급 실패: {exc}")
        return _finish(rep, now)

    quote = check_quotes(rec, rep, args.symbol, [s for s in args.status_symbols.split(",") if s])
    check_index(kis, rep)
    cash = check_balance(kis, rep)
    if args.rate_test:
        check_rate(rec, rep, args.symbol, args.rate_n)

    if args.order:
        if not in_order_window(now):
            rep.add("O1", FAIL, "장중(영업일 09:00~15:20 KST)이 아님 — 주문 생략")
        elif not settings.SHADOW_MODE:
            rep.add("O1", FAIL, "SHADOW_MODE=false — 데몬 실주문과 겹칠 수 있어 주문 생략")
        else:
            probe_order(kis, rec, rep, args.symbol, quote, cash, args.max_krw)
    return _finish(rep, now)


def _finish(rep: Report, now) -> int:
    out_dir = PROJECT_ROOT / "logs"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"smoke-{now:%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(rep.checks, ensure_ascii=False, indent=2), encoding="utf-8")
    n = {s: sum(c["status"] == s for c in rep.checks) for s in (PASS, WARN, FAIL)}
    print(f"== PASS {n[PASS]} / WARN {n[WARN]} / FAIL {n[FAIL]}  → {path.relative_to(PROJECT_ROOT)}")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
