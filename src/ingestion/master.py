"""KIS 종목 마스터 (kospi/kosdaq_code.mst) 수신·파싱·보관 (D9).

파일 형식 (KIS 공개 마스터, cp949 고정폭 — 공식 open-trading-api 예제와 동일 위치):
  한 줄 = [앞부분: 단축코드 9 · 표준코드 12 · 한글명 가변] + [뒷부분 고정폭]
  뒷부분 길이 (줄바꿈 제외): KOSPI 227 바이트 / KOSDAQ 221 바이트, 첫 2바이트가 그룹코드
  그룹코드 ST = 주권(보통주·우선주·스팩 포함), EF = ETF, EN = ETN, BC = 수익증권 …

보관: <root>/YYYYMMDD/{kospi,kosdaq}_code.mst. 수신 실패 시 가장 최근 보관본을 쓴다.
"""
from __future__ import annotations

import io
import logging
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import requests


logger = logging.getLogger(__name__)


MASTER_URLS: dict[str, str] = {
    "kospi": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "kosdaq": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}
TAIL_LEN: dict[str, int] = {"kospi": 227, "kosdaq": 221}
HEAD_CODE_LEN = 9
HEAD_ISIN_LEN = 12
GROUP_STOCK = "ST"
KEEP_DAYS = 7


@dataclass(frozen=True)
class MasterRow:
    code: str
    name: str
    group: str
    market: str      # kospi / kosdaq


def parse_mst(data: bytes, market: str) -> list[MasterRow]:
    tail_len = TAIL_LEN[market]
    rows: list[MasterRow] = []
    for raw in data.split(b"\n"):
        raw = raw.rstrip(b"\r\n")
        if len(raw) <= tail_len + HEAD_CODE_LEN + HEAD_ISIN_LEN:
            continue
        head, tail = raw[:-tail_len], raw[-tail_len:]
        code = head[:HEAD_CODE_LEN].decode("ascii", "ignore").strip()
        name = head[HEAD_CODE_LEN + HEAD_ISIN_LEN:].decode("cp949", "ignore").strip()
        group = tail[:2].decode("ascii", "ignore")
        if code and name:
            rows.append(MasterRow(code=code, name=name, group=group, market=market))
    return rows


def is_common_stock(row: MasterRow) -> bool:
    """매매 대상 보통주: 주권(ST) · 6자리 · 끝자리 0(우선주 제외) · 스팩 제외."""
    return (
        row.group == GROUP_STOCK
        and len(row.code) == 6
        and row.code.isalnum()
        and row.code.endswith("0")
        and "스팩" not in row.name
    )


@dataclass(frozen=True)
class Master:
    as_of: str                        # 보관 디렉터리 이름 (YYYYMMDD)
    rows: dict[str, MasterRow]        # code → row (전체)

    @property
    def common_stocks(self) -> dict[str, str]:
        """code → name (보통주만)."""
        return {c: r.name for c, r in self.rows.items() if is_common_stock(r)}


Fetcher = Callable[[str], bytes]


def _http_fetch(url: str) -> bytes:
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    return res.content


class MasterStore:
    def __init__(self, root: Path, *, fetch: Fetcher = _http_fetch) -> None:
        self.root = root
        self._fetch = fetch

    def refresh(self, today: date) -> Master | None:
        """오늘자 수신 → 보관 → 전일 대비 diff 로그. 실패 시 최근 보관본."""
        prev = self.load_latest()
        day_dir = self.root / today.strftime("%Y%m%d")
        try:
            files: dict[str, bytes] = {}
            for market, url in MASTER_URLS.items():
                with zipfile.ZipFile(io.BytesIO(self._fetch(url))) as zf:
                    member = f"{market}_code.mst"
                    files[member] = zf.read(member)
            tmp = day_dir.with_suffix(".tmp")
            tmp.mkdir(parents=True, exist_ok=True)
            for member, blob in files.items():
                (tmp / member).write_bytes(blob)
            if day_dir.exists():
                shutil.rmtree(day_dir)
            tmp.rename(day_dir)
        except Exception:
            logger.exception("master download failed — using latest stored copy")
            return prev
        cur = self._load_dir(day_dir)
        if cur is not None:
            self._log_diff(prev, cur)
            self._purge(keep=KEEP_DAYS)
        return cur or prev

    def load_latest(self) -> Master | None:
        if not self.root.exists():
            return None
        for d in sorted((p for p in self.root.iterdir() if p.is_dir() and p.name.isdigit()),
                        reverse=True):
            m = self._load_dir(d)
            if m is not None:
                return m
        return None

    def _load_dir(self, d: Path) -> Master | None:
        rows: dict[str, MasterRow] = {}
        for market in TAIL_LEN:
            p = d / f"{market}_code.mst"
            if not p.exists():
                return None
            for r in parse_mst(p.read_bytes(), market):
                rows[r.code] = r
        if not rows:
            return None
        return Master(as_of=d.name, rows=rows)

    def _purge(self, *, keep: int) -> None:
        dirs = sorted((p for p in self.root.iterdir() if p.is_dir() and p.name.isdigit()),
                      reverse=True)
        for d in dirs[keep:]:
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _log_diff(prev: Master | None, cur: Master) -> None:
        cur_c = cur.common_stocks
        if prev is None:
            logger.info("master %s: %d rows, %d common stocks", cur.as_of, len(cur.rows), len(cur_c))
            return
        prev_c = prev.common_stocks
        added = sorted(set(cur_c) - set(prev_c))
        removed = sorted(set(prev_c) - set(cur_c))
        logger.info(
            "master %s vs %s: common %d → %d (+%d %s / -%d %s)",
            prev.as_of, cur.as_of, len(prev_c), len(cur_c),
            len(added), added[:10], len(removed), removed[:10],
        )
