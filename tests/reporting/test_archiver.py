"""Tests for src.reporting.archiver.Archiver."""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from src.reporting.archiver import Archiver, TAX_HEADER, TRADES_HEADER
from src.reporting.models import (
    DailyReport,
    DataCollectionSummary,
    MarketSummary,
    PortfolioState,
    RiskStatus,
    TradeEntry,
    TradeExecutionSummary,
)


# ---------------------------------------------------------------- fixtures
def _make_report(
    report_date: date = date(2026, 4, 18),
    *,
    buys: list[TradeEntry] | None = None,
    take_profits: list[TradeEntry] | None = None,
    stop_losses: list[TradeEntry] | None = None,
) -> DailyReport:
    return DailyReport(
        report_date=report_date,
        market=MarketSummary(
            kospi=2500.0,
            kospi_change_pct=0.5,
            kosdaq=850.0,
            kosdaq_change_pct=-0.2,
            usd_krw=1380.0,
        ),
        data=DataCollectionSummary(
            news_count=10,
            source_distribution={"naver": 5, "daum": 5},
            analyzed_count=8,
            candidates_count=3,
        ),
        trades=TradeExecutionSummary(
            buys=buys or [],
            take_profits=take_profits or [],
            stop_losses=stop_losses or [],
        ),
        portfolio=PortfolioState(
            total_value=10_000_000.0,
            daily_pnl_krw=50_000.0,
            daily_pnl_pct=0.5,
            cumulative_return_pct=3.2,
            mdd_pct=-2.1,
        ),
        risk=RiskStatus(
            daily_loss_usage_pct=12.0,
            circuit_breaker_active=False,
            circuit_breaker_reason="",
            consecutive_loss_days=0,
        ),
        next_actions=["모니터링 지속", "리스크 체크"],
        strategy_version="v1.0",
    )


def _buy(symbol="005930", name="삼성전자", qty=10, price=70_000.0) -> TradeEntry:
    return TradeEntry(
        symbol=symbol, name=name, qty=qty, price=price, reason="변동성 돌파"
    )


def _tp(symbol="005930", name="삼성전자", qty=10, price=75_000.0,
        pnl_krw=50_000.0, pnl_pct=7.1) -> TradeEntry:
    return TradeEntry(
        symbol=symbol, name=name, qty=qty, price=price,
        pnl_krw=pnl_krw, pnl_pct=pnl_pct, reason="익절 +7%",
    )


def _sl(symbol="035720", name="카카오", qty=5, price=48_000.0,
        pnl_krw=-10_000.0, pnl_pct=-4.0) -> TradeEntry:
    return TradeEntry(
        symbol=symbol, name=name, qty=qty, price=price,
        pnl_krw=pnl_krw, pnl_pct=pnl_pct, reason="손절",
    )


# ------------------------------------------------------------ constructor
def test_archiver_creates_root_dir(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    assert not root.exists()
    archiver = Archiver(root)
    assert root.is_dir()
    assert archiver.archive_root == root


def test_archiver_accepts_existing_dir(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    Archiver(root)  # no raise
    assert root.is_dir()


def test_archiver_raises_when_root_is_file(tmp_path: Path) -> None:
    f = tmp_path / "oops.txt"
    f.write_text("not a dir", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        Archiver(f)


# ----------------------------------------------------------- write_daily_report
def test_write_daily_report_writes_expected_path(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    report = _make_report(
        buys=[_buy()],
        take_profits=[_tp()],
        stop_losses=[_sl()],
    )
    path = archiver.write_daily_report(report)

    expected = tmp_path / "2026" / "2026-04-18.json"
    assert path == expected
    assert path.exists()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["report_date"] == "2026-04-18"
    assert "market" in payload
    assert "trades" in payload
    assert "portfolio" in payload
    assert payload["trades"]["buys"][0]["name"] == "삼성전자"


def test_write_daily_report_is_idempotent_overwrite(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    r1 = _make_report(buys=[_buy(qty=1)])
    r2 = _make_report(buys=[_buy(qty=999)])
    archiver.write_daily_report(r1)
    path = archiver.write_daily_report(r2)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["trades"]["buys"][0]["qty"] == 999


def test_write_daily_report_none_fields_are_json_null(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    report = _make_report(
        take_profits=[TradeEntry(symbol="x", pnl_krw=None, pnl_pct=None)],
    )
    path = archiver.write_daily_report(report)
    raw = path.read_text(encoding="utf-8")
    assert "null" in raw


# ----------------------------------------------------------- write_trades_csv
def test_write_trades_csv_all_kinds(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    report = _make_report(
        buys=[_buy()],
        take_profits=[_tp()],
        stop_losses=[_sl()],
    )
    path = archiver.write_trades_csv(report)

    expected = tmp_path / "2026" / "2026-04-18-trades.csv"
    assert path == expected

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    assert rows[0] == TRADES_HEADER
    kinds = [row[1] for row in rows[1:]]
    assert kinds == ["BUY", "TAKE_PROFIT", "STOP_LOSS"]
    # 한글 이름 왕복
    names = [row[3] for row in rows[1:]]
    assert "삼성전자" in names
    assert "카카오" in names


def test_write_trades_csv_empty(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    report = _make_report()
    path = archiver.write_trades_csv(report)
    assert path.exists()

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [TRADES_HEADER]


def test_write_trades_csv_utf8_sig_bom(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    report = _make_report(buys=[_buy()])
    path = archiver.write_trades_csv(report)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM


# ----------------------------------------------------------- append_tax_ledger
def test_append_tax_ledger_first_call(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    report = _make_report(
        buys=[_buy()],           # 매수는 세금 신고 대상 아님
        take_profits=[_tp()],
        stop_losses=[_sl()],
    )
    path = archiver.append_tax_ledger(report)
    expected = tmp_path / "2026" / "tax-2026.csv"
    assert path == expected

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == TAX_HEADER
    # 청산 2건만
    assert len(rows) - 1 == 2
    symbols = [row[1] for row in rows[1:]]
    assert set(symbols) == {"005930", "035720"}


def test_append_tax_ledger_idempotent_same_day(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    report = _make_report(
        take_profits=[_tp()],
        stop_losses=[_sl()],
    )
    archiver.append_tax_ledger(report)
    archiver.append_tax_ledger(report)  # 두 번째 호출

    path = tmp_path / "2026" / "tax-2026.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    # 헤더 + 2행 (중복 없음)
    assert len(rows) == 3


def test_append_tax_ledger_second_day_appends(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    day1 = _make_report(date(2026, 4, 17), take_profits=[_tp()])
    day2 = _make_report(date(2026, 4, 18), stop_losses=[_sl()])

    archiver.append_tax_ledger(day1)
    archiver.append_tax_ledger(day2)

    path = tmp_path / "2026" / "tax-2026.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    # 헤더 + 2일치 1건씩
    assert len(rows) == 3
    dates = [row[0] for row in rows[1:]]
    assert dates == ["2026-04-17", "2026-04-18"]


def test_append_tax_ledger_excludes_buys(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    report = _make_report(buys=[_buy()])  # 청산 없음
    path = archiver.append_tax_ledger(report)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [TAX_HEADER]


# --------------------------------------------------------------- read_daily
def test_read_daily_report_roundtrip(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    report = _make_report(buys=[_buy(name="삼성전자")])
    archiver.write_daily_report(report)

    loaded = archiver.read_daily_report(date(2026, 4, 18))
    assert loaded is not None
    assert loaded["report_date"] == "2026-04-18"
    assert loaded["trades"]["buys"][0]["name"] == "삼성전자"


def test_read_daily_report_missing(tmp_path: Path) -> None:
    archiver = Archiver(tmp_path)
    assert archiver.read_daily_report(date(2025, 1, 1)) is None
