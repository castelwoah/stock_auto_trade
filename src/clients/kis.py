from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from src.clients.rate_limiter import TokenBucket
from src.config import Settings
from src.common.clock import now_kst, today_kst


logger = logging.getLogger(__name__)


# 주문 거래소 구분 (공식 예제: order_cash 필수 필드 EXCG_ID_DVSN_CD)
EXCHANGE_CODE = "KRX"
# 지정가만 허용 (D14 · rules §6: 시장가 금지)
ORD_DVSN_LIMIT = "00"
ALLOWED_ORD_DVSN = frozenset({ORD_DVSN_LIMIT})
# 일별주문체결조회 연속조회 상한 (페이지 15~100건 → 충분)
CCLD_MAX_PAGES = 20


class KisOrderRejected(RuntimeError):
    """KIS 가 HTTP 200 으로 응답했으나 rt_cd != '0' (주문 거절)."""

    def __init__(self, msg_cd: str, msg1: str) -> None:
        super().__init__(f"[{msg_cd}] {msg1}")
        self.msg_cd = msg_cd
        self.msg1 = msg1


@dataclass(frozen=True)
class OrderAck:
    """주문 접수 응답. 취소에는 odno + orgno(한국거래소전송주문조직번호) 모두 필요."""

    odno: str
    orgno: str
    ord_tmd: str = ""


@dataclass(frozen=True)
class OrderFill:
    """일별주문체결조회 output1 1건 (해당 주문의 누적 상태)."""

    odno: str
    symbol: str
    side: str            # BUY / SELL
    ord_qty: int
    filled_qty: int
    avg_price: float
    remaining_qty: int
    cancelled: bool
    # 주문채번지점번호 — 주문 응답의 KRX_FWDG_ORD_ORGNO 와 같은 값으로 가정
    # (응답 유실 주문을 체결조회로 복구해 취소할 때 사용. 실전 스모크 테스트에서 확인)
    orgno: str = ""


@dataclass
class CachedToken:
    value: str
    valid_until: datetime


class TokenStore:
    """Daily-file token cache. Mirrors legacy KIS{YYYYMMDD} scheme."""

    TOKEN_PREFIX = "KIS"
    EXPIRED_DIR_NAME = "expired"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.expired_dir = root / self.EXPIRED_DIR_NAME

    def current_path(self) -> Path:
        return self.root / f"{self.TOKEN_PREFIX}{now_kst().strftime('%Y%m%d')}"

    def read(self) -> CachedToken | None:
        p = self.current_path()
        if not p.exists() or p.stat().st_size == 0:
            return None
        token: str | None = None
        valid: datetime | None = None
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("token:"):
                token = line.split(":", 1)[1].strip()
            elif line.startswith("valid-date:"):
                raw = line.split(":", 1)[1].strip()
                try:
                    valid = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    valid = None
        if token and valid and valid > now_kst():
            return CachedToken(value=token, valid_until=valid)
        return None

    def write(self, token: str, expired_str: str) -> None:
        valid = datetime.strptime(expired_str, "%Y-%m-%d %H:%M:%S")
        p = self.current_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"token: {token}\nvalid-date: {valid.strftime('%Y-%m-%d %H:%M:%S')}\n",
            encoding="utf-8",
        )

    def move_expired(self) -> int:
        self.expired_dir.mkdir(parents=True, exist_ok=True)
        today = today_kst()
        moved = 0
        for f in self.root.iterdir():
            if not f.is_file():
                continue
            name = f.name
            if not (name.startswith(self.TOKEN_PREFIX) and len(name) >= 11 and name[3:11].isdigit()):
                continue
            try:
                d = datetime.strptime(name[3:11], "%Y%m%d").date()
            except ValueError:
                continue
            if d < today:
                ts = now_kst().strftime("%Y%m%d_%H%M%S")
                dest = self.expired_dir / f"{ts}_{name}"
                shutil.move(str(f), str(dest))
                moved += 1
        return moved


class KisClient:
    """KIS REST wrapper with token manager and rate limiting.

    Legacy 함수 동등 매핑:
      - get_access_token     → self.get_access_token()
      - get_current_price    → self.get_current_price()
      - get_balance          → self.get_cash_balance()
      - get_stock_balance    → self.get_stock_balance()
      - get_prev_day_high_low→ self.get_prev_day_ohlc()
      - buy/sell             → self.order(side=...)
    """

    def __init__(
        self,
        settings: Settings,
        *,
        bucket: TokenBucket | None = None,
        token_store: TokenStore | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.settings = settings
        self.base_url = settings.URL_BASE
        self.app_key = settings.APP_KEY.get_secret_value()
        self.app_secret = settings.APP_SECRET.get_secret_value()
        self.cano = settings.CANO
        self.acnt_prdt_cd = settings.ACNT_PRDT_CD
        self.timeout = timeout
        self.tr_ids = settings.kis_profile.tr_ids
        rate = settings.TOKEN_BUCKET_REFILL_RATE or settings.kis_profile.rate_per_sec
        self.bucket = bucket or TokenBucket(
            capacity=settings.TOKEN_BUCKET_CAPACITY or max(1, int(rate)),
            refill_rate=rate,
        )
        self.token_store = token_store or TokenStore(settings.TOKEN_DIR)
        self._access_token: str | None = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def get_access_token(self) -> str:
        cached = self.token_store.read()
        if cached is not None:
            self._access_token = cached.value
            self.token_store.move_expired()
            return cached.value

        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        self.bucket.acquire()
        res = requests.post(
            url,
            headers={"content-type": "application/json"},
            data=json.dumps(body),
            timeout=self.timeout,
        )
        res.raise_for_status()
        data = res.json()
        token = data["access_token"]
        expired = data["access_token_token_expired"]
        self.token_store.write(token, expired)
        self.token_store.move_expired()
        self._access_token = token
        return token

    def _headers(self, tr_id: str, *, custtype: str = "P") -> dict[str, str]:
        if self._access_token is None:
            self.get_access_token()
        return {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self._access_token}",
            "appKey": self.app_key,
            "appSecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": custtype,
        }

    # ------------------------------------------------------------------
    # HTTP primitives
    # ------------------------------------------------------------------

    def _get(self, path: str, tr_id: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._get_with_cont(path, tr_id, params)[0]

    def _get_with_cont(
        self, path: str, tr_id: str, params: dict[str, Any], *, tr_cont: str = ""
    ) -> tuple[dict[str, Any], str]:
        """GET + 응답 헤더 tr_cont (연속조회: 'M'/'F' = 다음 페이지 있음)."""
        self.bucket.acquire()
        headers = self._headers(tr_id)
        if tr_cont:
            headers["tr_cont"] = tr_cont
        res = requests.get(
            f"{self.base_url}/{path}",
            headers=headers,
            params=params,
            timeout=self.timeout,
        )
        res.raise_for_status()
        return res.json(), res.headers.get("tr_cont", "")

    def _post(self, path: str, tr_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bucket.acquire()
        res = requests.post(
            f"{self.base_url}/{path}",
            headers=self._headers(tr_id),
            data=json.dumps(body),
            timeout=self.timeout,
        )
        res.raise_for_status()
        return res.json()

    # ------------------------------------------------------------------
    # Domain methods
    # ------------------------------------------------------------------

    def get_current_price(self, code: str) -> int:
        data = self._get(
            "uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": code,
            },
        )
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"current price fetch failed: {data.get('msg1')}")
        return int(data["output"]["stck_prpr"])

    def get_quote(self, code: str) -> dict[str, float]:
        """현재가 + 시가총액 (P0-5).

        hts_avls 단위는 억원으로 가정 → 원 환산 (공식 예제에 단위 미표기 — 실전
        스모크 테스트에서 확인). trade_value 는 당일 누적이라 장 초반엔 작으므로
        유동성 게이트는 get_prev_day_ohlc 의 avg_trade_value_20d 를 쓴다.
        """
        data = self._get(
            "uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": code,
            },
        )
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"quote fetch failed: {data.get('msg1')}")
        out = data["output"]
        return {
            "price": float(out["stck_prpr"]),
            "market_cap": float(out.get("hts_avls") or 0) * 100_000_000,
            "trade_value": float(out.get("acml_tr_pbmn") or 0),
        }

    def get_cash_balance(self, ref_code: str = "005930", ref_price: str = "65500") -> int:
        data = self._get(
            "uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id=self.tr_ids["psbl_order"],
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "PDNO": ref_code,
                "ORD_UNPR": ref_price,
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "Y",
                "OVRS_ICLD_YN": "Y",
            },
        )
        if "output" not in data:
            raise RuntimeError(f"cash balance fetch failed: {data.get('msg1', data)}")
        return int(data["output"]["ord_psbl_cash"])

    def get_stock_balance(self) -> tuple[dict[str, int], dict[str, Any]]:
        data = self._get(
            "uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=self.tr_ids["balance"],
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        if "output1" not in data:
            raise RuntimeError(f"stock balance fetch failed: {data.get('msg1', data)}")
        holdings: dict[str, int] = {}
        for s in data.get("output1") or []:
            try:
                qty = int(s.get("hldg_qty", "0"))
            except (TypeError, ValueError):
                qty = 0
            if qty > 0:
                holdings[s.get("pdno", "")] = qty
        evaluation = (data.get("output2") or [{}])[0]
        return holdings, evaluation

    def get_prev_day_ohlc(self, code: str) -> dict[str, str] | None:
        data = self._get(
            "uapi/domestic-stock/v1/quotations/inquire-daily-price",
            tr_id="FHKST01010400",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": code,
                "fid_org_adj_prc": "1",
                "fid_period_div_code": "D",
            },
        )
        rows = data.get("output") or []
        if not rows:
            return None
        today_str = now_kst().strftime("%Y%m%d")
        prev = None
        for r in rows:
            d = r.get("stck_bsop_date")
            if d and d < today_str:
                if prev is None or d > prev.get("stck_bsop_date", ""):
                    prev = r
        if prev is None and len(rows) > 1:
            prev = rows[1]
        if prev is None:
            return None
        # 유동성 게이트용 20일 평균 거래대금 ≈ mean(거래량 × 종가), 당일 제외
        past = [r for r in rows if (r.get("stck_bsop_date") or "") < today_str][:20]
        values = [_to_int(r.get("acml_vol")) * _to_int(r.get("stck_clpr")) for r in past]
        avg_value = sum(values) / len(values) if values else 0.0
        return {
            "date": prev.get("stck_bsop_date"),
            "high": prev.get("stck_hgpr"),
            "low": prev.get("stck_lwpr"),
            "open_today": rows[0].get("stck_oprc"),
            "avg_trade_value_20d": str(avg_value),
        }

    def order(
        self,
        code: str,
        qty: int,
        side: str,
        *,
        ord_dvsn: str = ORD_DVSN_LIMIT,
        unit_price: str = "0",
    ) -> OrderAck:
        """지정가 주문. 거절(rt_cd != '0')은 KisOrderRejected.

        네트워크 예외(requests.RequestException)는 그대로 전파된다 — 주문이 실제로
        접수됐을 수 있으므로 호출측은 '거절'이 아니라 '불명(unknown)'으로 다룬다.
        """
        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"}:
            raise ValueError("side must be 'BUY' or 'SELL'")
        if ord_dvsn not in ALLOWED_ORD_DVSN:
            raise ValueError(f"ord_dvsn {ord_dvsn!r} not allowed (limit orders only)")
        if int(qty) <= 0 or int(float(unit_price)) <= 0:
            raise ValueError("qty and unit_price must be positive")
        tr_id = self.tr_ids["buy" if side_upper == "BUY" else "sell"]
        data = self._post(
            "uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_id,
            body={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "PDNO": code,
                "ORD_DVSN": ord_dvsn,
                "ORD_QTY": str(int(qty)),
                "ORD_UNPR": str(int(float(unit_price))),
                "EXCG_ID_DVSN_CD": EXCHANGE_CODE,
            },
        )
        if data.get("rt_cd") != "0":
            raise KisOrderRejected(str(data.get("msg_cd", "")), str(data.get("msg1", "")))
        out = data.get("output") or {}
        return OrderAck(
            odno=str(out.get("ODNO") or ""),
            orgno=str(out.get("KRX_FWDG_ORD_ORGNO") or ""),
            ord_tmd=str(out.get("ORD_TMD") or ""),
        )

    def cancel_order(self, odno: str, orgno: str) -> None:
        """잔량 전부 취소 (RVSE_CNCL_DVSN_CD=02, QTY_ALL_ORD_YN=Y). 실패 시 예외."""
        if not odno or not orgno:
            raise ValueError("cancel requires both odno and orgno")
        data = self._post(
            "uapi/domestic-stock/v1/trading/order-rvsecncl",
            tr_id=self.tr_ids["cancel"],
            body={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "KRX_FWDG_ORD_ORGNO": orgno,
                "ORGN_ODNO": odno,
                "ORD_DVSN": ORD_DVSN_LIMIT,
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": "0",
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y",
                "EXCG_ID_DVSN_CD": EXCHANGE_CODE,
            },
        )
        if data.get("rt_cd") != "0":
            raise KisOrderRejected(str(data.get("msg_cd", "")), str(data.get("msg1", "")))

    def inquire_today_orders(self, day: str) -> list[OrderFill]:
        """당일(day=YYYYMMDD) 전체 주문의 체결 상태. 연속조회로 전 페이지 수집."""
        fills: list[OrderFill] = []
        fk = nk = ""
        tr_cont = ""
        for _ in range(CCLD_MAX_PAGES):
            data, next_cont = self._get_with_cont(
                "uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=self.tr_ids["daily_ccld"],
                params={
                    "CANO": self.cano,
                    "ACNT_PRDT_CD": self.acnt_prdt_cd,
                    "INQR_STRT_DT": day,
                    "INQR_END_DT": day,
                    "SLL_BUY_DVSN_CD": "00",
                    "PDNO": "",
                    "CCLD_DVSN": "00",
                    "INQR_DVSN": "00",
                    "INQR_DVSN_3": "00",
                    "ORD_GNO_BRNO": "",
                    "ODNO": "",
                    "INQR_DVSN_1": "",
                    "CTX_AREA_FK100": fk,
                    "CTX_AREA_NK100": nk,
                    "EXCG_ID_DVSN_CD": EXCHANGE_CODE,
                },
                tr_cont=tr_cont,
            )
            if data.get("rt_cd") != "0":
                raise RuntimeError(f"daily ccld fetch failed: {data.get('msg1')}")
            for r in data.get("output1") or []:
                fills.append(_parse_fill(r))
            if next_cont not in ("M", "F"):
                break
            fk = data.get("ctx_area_fk100", "")
            nk = data.get("ctx_area_nk100", "")
            tr_cont = "N"
        return fills


def _to_int(v: Any) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _parse_fill(r: dict[str, Any]) -> OrderFill:
    try:
        avg = float(r.get("avg_prvs") or 0)
    except (TypeError, ValueError):
        avg = 0.0
    return OrderFill(
        odno=str(r.get("odno") or ""),
        symbol=str(r.get("pdno") or ""),
        side="SELL" if r.get("sll_buy_dvsn_cd") == "01" else "BUY",
        ord_qty=_to_int(r.get("ord_qty")),
        filled_qty=_to_int(r.get("tot_ccld_qty")),
        avg_price=avg,
        remaining_qty=_to_int(r.get("rmn_qty")),
        cancelled=(r.get("cncl_yn") == "Y"),
        orgno=str(r.get("ord_gno_brno") or ""),
    )
