from datetime import date, datetime, time
from unittest.mock import MagicMock

import pytest

from src.reporting.daily_report import (
    COLOR_LOSS,
    COLOR_NEUTRAL,
    COLOR_PROFIT,
    COLOR_WARNING,
)
from src.reporting.midday_report import (
    WINDOW_END,
    WINDOW_START,
    detect_anomalies,
    in_valid_window,
    render_midday_embed,
    render_midday_text,
    send_midday_report,
)
from src.reporting.models import (
    AttemptEntry,
    CandidateSnapshot,
    MarketSummary,
    MidDayAnomaly,
    MidDaySnapshot,
    MorningStats,
    PositionSnapshot,
    RESULT_BLOCKED_RISK,
    RESULT_REJECTED,
    RiskStatus,
    TradeEntry,
)


def _snap(**overrides) -> MidDaySnapshot:
    base = dict(
        ts=datetime(2026, 4, 20, 12, 0, 0),
        market=MarketSummary(kospi=2680.5, kospi_change_pct=0.8, kosdaq=870.2, kosdaq_change_pct=-0.3),
        morning=MorningStats(
            new_buys=[TradeEntry(symbol="005930", name="삼성전자", qty=10, price=75000)],
            take_profits=[],
            stop_losses=[],
            realized_pnl_krw=0.0,
        ),
        positions=[
            PositionSnapshot(
                symbol="005930", name="삼성전자", strategy_id="news_swing",
                qty=10, avg_price=75000, current_price=77000,
                unrealized_pnl_krw=20000, unrealized_pnl_pct=2.67,
            ),
        ],
        top_candidates=[
            CandidateSnapshot(symbol="000660", name="SK하이닉스", total_score=88.5, dominant_factor="news"),
        ],
        total_equity=10_200_000,
        unrealized_pnl_pct=2.0,
        risk=RiskStatus(daily_loss_usage_pct=20.0, circuit_breaker_active=False),
        next_actions=["오후 트레이딩 지속"],
    )
    base.update(overrides)
    return MidDaySnapshot(**base)


# --- window guard -----------------------------------------------------------

def test_window_constants():
    assert WINDOW_START == time(11, 55)
    assert WINDOW_END == time(12, 15)


def test_in_valid_window_inside():
    assert in_valid_window(datetime(2026, 4, 20, 12, 0))


def test_in_valid_window_outside():
    assert not in_valid_window(datetime(2026, 4, 20, 10, 0))
    assert not in_valid_window(datetime(2026, 4, 20, 13, 0))


# --- render_text ------------------------------------------------------------

def test_render_text_basic_sections():
    txt = render_midday_text(_snap())
    for header in ("미드데이 스냅샷", "■ 시장 현황", "■ 오전 매매", "■ 현재 포지션", "■ 종합", "■ 리스크 상태", "■ 오후 후보"):
        assert header in txt


def test_render_text_no_positions_placeholder():
    snap = _snap(positions=[])
    txt = render_midday_text(snap)
    assert "(보유 없음)" in txt


def test_render_text_no_candidates_placeholder():
    snap = _snap(top_candidates=[])
    txt = render_midday_text(snap)
    assert "(후보 없음)" in txt


def test_render_text_tp_armed_marker():
    snap = _snap(positions=[
        PositionSnapshot(symbol="A", qty=10, avg_price=100, current_price=110,
                          unrealized_pnl_krw=100, unrealized_pnl_pct=10, tp_armed=True),
    ])
    txt = render_midday_text(snap)
    assert "[TP armed]" in txt


def test_render_text_includes_failure_breakdown():
    snap = _snap(morning=MorningStats(
        new_buys=[],
        rejections=[AttemptEntry(ts=datetime(2026, 4, 20, 11, 2), symbol="X", result=RESULT_REJECTED)],
        cancellations=[],
        risk_blocks=[
            AttemptEntry(ts=datetime(2026, 4, 20, 11, 5), symbol="Y", result=RESULT_BLOCKED_RISK, reason_code="blacklist"),
            AttemptEntry(ts=datetime(2026, 4, 20, 11, 6), symbol="Z", result=RESULT_BLOCKED_RISK, reason_code="cooldown"),
            AttemptEntry(ts=datetime(2026, 4, 20, 11, 7), symbol="W", result=RESULT_BLOCKED_RISK, reason_code="blacklist"),
        ],
    ))
    txt = render_midday_text(snap)
    assert "[실패·차단]" in txt
    assert "거절(KIS): 1건" in txt
    assert "리스크 차단: 3건" in txt
    assert "blacklist 2" in txt
    assert "cooldown 1" in txt


def test_render_text_anomalies_section():
    snap = _snap(anomalies=[
        MidDayAnomaly(severity="critical", code="circuit_breaker_active", message="CB: test"),
        MidDayAnomaly(severity="warn", code="daily_loss_near_limit", message="80%"),
    ])
    txt = render_midday_text(snap)
    assert "■ 이상 신호" in txt
    assert "🔴" in txt
    assert "🟠" in txt


# --- embed color ------------------------------------------------------------

def test_embed_color_profit_when_positive_unrealized():
    assert render_midday_embed(_snap(unrealized_pnl_pct=2.0))["color"] == COLOR_PROFIT


def test_embed_color_loss_when_negative_unrealized():
    assert render_midday_embed(_snap(unrealized_pnl_pct=-2.0))["color"] == COLOR_LOSS


def test_embed_color_warning_when_critical_anomaly():
    snap = _snap(
        unrealized_pnl_pct=5.0,   # profit 이어도
        anomalies=[MidDayAnomaly(severity="critical", code="x", message="y")],
    )
    assert render_midday_embed(snap)["color"] == COLOR_WARNING


def test_embed_color_neutral_when_flat():
    assert render_midday_embed(_snap(unrealized_pnl_pct=0.0))["color"] == COLOR_NEUTRAL


# --- anomaly detection ------------------------------------------------------

def test_detect_daily_loss_critical():
    snap = _snap(risk=RiskStatus(daily_loss_usage_pct=95.0))
    anomalies = detect_anomalies(snap)
    assert any(a.code == "daily_loss_near_limit" and a.severity == "critical" for a in anomalies)


def test_detect_daily_loss_warn():
    snap = _snap(risk=RiskStatus(daily_loss_usage_pct=75.0))
    anomalies = detect_anomalies(snap)
    assert any(a.code == "daily_loss_near_limit" and a.severity == "warn" for a in anomalies)


def test_detect_position_near_sl():
    snap = _snap(positions=[
        PositionSnapshot(symbol="A", qty=10, avg_price=100, current_price=96,
                         unrealized_pnl_krw=-40, unrealized_pnl_pct=-4.5),
    ])
    anomalies = detect_anomalies(snap)
    assert any(a.code == "position_near_sl" for a in anomalies)


def test_detect_position_near_tp_only_if_not_armed():
    snap1 = _snap(positions=[
        PositionSnapshot(symbol="A", qty=10, avg_price=100, current_price=107,
                         unrealized_pnl_krw=70, unrealized_pnl_pct=6.5, tp_armed=False),
    ])
    assert any(a.code == "position_near_tp" for a in detect_anomalies(snap1))
    snap2 = _snap(positions=[
        PositionSnapshot(symbol="A", qty=10, avg_price=100, current_price=107,
                         unrealized_pnl_krw=70, unrealized_pnl_pct=6.5, tp_armed=True),
    ])
    assert not any(a.code == "position_near_tp" for a in detect_anomalies(snap2))


def test_detect_no_morning_activity():
    snap = _snap(morning=MorningStats())  # 전부 빈
    assert any(a.code == "no_morning_activity" for a in detect_anomalies(snap))


def test_detect_cb_active_critical():
    snap = _snap(risk=RiskStatus(circuit_breaker_active=True, circuit_breaker_reason="x"))
    assert any(a.code == "circuit_breaker_active" and a.severity == "critical"
               for a in detect_anomalies(snap))


# --- send ------------------------------------------------------------------

def test_send_midday_report_embed_default():
    n = MagicMock()
    n.send_embed.return_value = True
    send_midday_report(n, _snap())
    n.send_embed.assert_called_once()
    n.send.assert_not_called()


def test_send_midday_report_text_mode():
    n = MagicMock()
    n.send.return_value = True
    send_midday_report(n, _snap(), mode="text")
    n.send.assert_called_once()
    n.send_embed.assert_not_called()


def test_virtual_equity_tag():
    s = _snap(total_equity=10_000_000, equity_virtual=True)
    assert "평가액: ₩10,000,000 (가상)" in render_midday_text(s)
    total = next(f for f in render_midday_embed(s)["fields"] if f["name"] == "■ 종합")
    assert "(가상)" in total["value"]
    assert "(가상)" not in render_midday_text(_snap())
