"""Daily report / trades archiver.

- 일별 JSON 리포트 저장
- 일별 매매 CSV 저장
- 연간 누적 세금 신고용 CSV append (청산만)

stdlib 전용 (pandas 등 외부 의존성 금지).
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .models import DailyReport, TradeEntry


TRADES_HEADER = [
    "date",
    "kind",
    "symbol",
    "name",
    "qty",
    "price",
    "pnl_krw",
    "pnl_pct",
    "reason",
]

TAX_HEADER = [
    "date",
    "symbol",
    "name",
    "qty",
    "price",
    "pnl_krw",
    "pnl_pct",
    "reason",
]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _fmt_cell(value: Any) -> str:
    """CSV 셀 포맷: None -> '' 그 외는 문자열."""
    if value is None:
        return ""
    return str(value)


def _trade_row(report_date: date, kind: str, t: TradeEntry) -> list[str]:
    return [
        report_date.isoformat(),
        kind,
        t.symbol,
        t.name,
        _fmt_cell(t.qty),
        _fmt_cell(t.price),
        _fmt_cell(t.pnl_krw),
        _fmt_cell(t.pnl_pct),
        t.reason,
    ]


def _tax_row(report_date: date, t: TradeEntry) -> list[str]:
    return [
        report_date.isoformat(),
        t.symbol,
        t.name,
        _fmt_cell(t.qty),
        _fmt_cell(t.price),
        _fmt_cell(t.pnl_krw),
        _fmt_cell(t.pnl_pct),
        t.reason,
    ]


class Archiver:
    """Filesystem-backed archive of daily reports and trades."""

    def __init__(self, archive_root: Path) -> None:
        if archive_root is None:
            raise ValueError("archive_root must not be None")
        archive_root = Path(archive_root)
        if archive_root.exists() and not archive_root.is_dir():
            raise NotADirectoryError(
                f"archive_root exists but is not a directory: {archive_root}"
            )
        archive_root.mkdir(parents=True, exist_ok=True)
        self.archive_root = archive_root

    # ------------------------------------------------------------------ paths
    def _year_dir(self, d: date) -> Path:
        p = self.archive_root / f"{d.year:04d}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _daily_json_path(self, d: date) -> Path:
        return self._year_dir(d) / f"{d.isoformat()}.json"

    def _daily_trades_path(self, d: date) -> Path:
        return self._year_dir(d) / f"{d.isoformat()}-trades.csv"

    def _tax_path(self, d: date) -> Path:
        return self._year_dir(d) / f"tax-{d.year:04d}.csv"

    # -------------------------------------------------------------- daily JSON
    def write_daily_report(self, report: DailyReport) -> Path:
        path = self._daily_json_path(report.report_date)
        payload = asdict(report)
        with path.open("w", encoding="utf-8") as f:
            json.dump(
                payload,
                f,
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            )
        return path

    # ----------------------------------------------------------- daily trades
    def write_trades_csv(self, report: DailyReport) -> Path:
        path = self._daily_trades_path(report.report_date)
        trades = report.trades
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(TRADES_HEADER)
            for t in trades.buys:
                writer.writerow(_trade_row(report.report_date, "BUY", t))
            for t in trades.take_profits:
                writer.writerow(_trade_row(report.report_date, "TAKE_PROFIT", t))
            for t in trades.stop_losses:
                writer.writerow(_trade_row(report.report_date, "STOP_LOSS", t))
        return path

    # ------------------------------------------------------------- tax ledger
    def append_tax_ledger(self, report: DailyReport) -> Path:
        path = self._tax_path(report.report_date)

        # 기존 키 수집 (date, symbol, qty)
        existing_keys: set[tuple[str, str, str]] = set()
        file_exists = path.exists() and path.stat().st_size > 0
        if file_exists:
            # utf-8-sig 로 읽으면 선두 BOM 을 자동 스킵한다.
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = (
                        row.get("date", ""),
                        row.get("symbol", ""),
                        row.get("qty", ""),
                    )
                    existing_keys.add(key)

        liquidations: Iterable[TradeEntry] = list(report.trades.take_profits) + list(
            report.trades.stop_losses
        )

        # Append 모드에서는 BOM 중복 방지를 위해 'utf-8' 로 열고,
        # 신규 파일일 때만 수동으로 BOM 을 선두에 기록한다.
        # (utf-8-sig 는 write 스트림마다 BOM 을 써서 append 시 중복 발생)
        need_header = not file_exists
        with path.open("a", encoding="utf-8", newline="") as f:
            if need_header:
                f.write("\ufeff")  # UTF-8 BOM
            writer = csv.writer(f)
            if need_header:
                writer.writerow(TAX_HEADER)
            for t in liquidations:
                key = (report.report_date.isoformat(), t.symbol, str(t.qty))
                if key in existing_keys:
                    continue
                writer.writerow(_tax_row(report.report_date, t))
                existing_keys.add(key)
        return path

    # ---------------------------------------------------------------- reader
    def read_daily_report(self, report_date: date) -> dict | None:
        path = self._daily_json_path(report_date)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
