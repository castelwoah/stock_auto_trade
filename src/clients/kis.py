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


logger = logging.getLogger(__name__)


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
        return self.root / f"{self.TOKEN_PREFIX}{datetime.today().strftime('%Y%m%d')}"

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
        if token and valid and valid > datetime.now():
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
        today = datetime.today().date()
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
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
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
        self.bucket = bucket or TokenBucket(
            capacity=settings.TOKEN_BUCKET_CAPACITY,
            refill_rate=settings.TOKEN_BUCKET_REFILL_RATE,
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
        self.bucket.acquire()
        res = requests.get(
            f"{self.base_url}/{path}",
            headers=self._headers(tr_id),
            params=params,
            timeout=self.timeout,
        )
        res.raise_for_status()
        return res.json()

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

    def get_cash_balance(self, ref_code: str = "005930", ref_price: str = "65500") -> int:
        data = self._get(
            "uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id="TTTC8908R",
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
            tr_id="TTTC8434R",
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
        today_str = datetime.today().strftime("%Y%m%d")
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
        return {
            "date": prev.get("stck_bsop_date"),
            "high": prev.get("stck_hgpr"),
            "low": prev.get("stck_lwpr"),
            "open_today": rows[0].get("stck_oprc"),
        }

    def order(
        self,
        code: str,
        qty: int,
        side: str,
        *,
        ord_dvsn: str = "01",
        unit_price: str = "0",
    ) -> dict[str, Any]:
        """Place an order.

        side: 'BUY' | 'SELL'
        ord_dvsn: '00' = 지정가, '01' = 시장가 (legacy default; W5+에서 지정가로 이행)
        unit_price: 지정가 시 가격, 시장가는 '0'
        """
        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"}:
            raise ValueError("side must be 'BUY' or 'SELL'")
        tr_id = "TTTC0012U" if side_upper == "BUY" else "TTTC0011U"
        return self._post(
            "uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_id,
            body={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "PDNO": code,
                "ORD_DVSN": ord_dvsn,
                "ORD_QTY": str(int(qty)),
                "ORD_UNPR": str(unit_price),
            },
        )
