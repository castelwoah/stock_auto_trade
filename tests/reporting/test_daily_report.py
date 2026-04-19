from datetime import date
from unittest.mock import MagicMock

from src.reporting.daily_report import (
    COLOR_LOSS,
    COLOR_NEUTRAL,
    COLOR_PROFIT,
    COLOR_WARNING,
    render_embed,
    render_text,
    send_daily_report,
)
from src.reporting.models import (
    DailyReport,
    DataCollectionSummary,
    MarketSummary,
    PortfolioState,
    RiskStatus,
    TradeEntry,
    TradeExecutionSummary,
)


def _full_report(**overrides) -> DailyReport:
    base = dict(
        report_date=date(2026, 4, 19),
        market=MarketSummary(
            kospi=2680.5, kospi_change_pct=0.8,
            kosdaq=870.2, kosdaq_change_pct=-0.3,
            usd_krw=1345.0,
        ),
        data=DataCollectionSummary(
            news_count=120,
            source_distribution={"dart": 40, "yonhap_rss": 70, "kis_news": 10},
            analyzed_count=50,
            candidates_count=8,
        ),
        trades=TradeExecutionSummary(
            buys=[TradeEntry(symbol="005930", name="삼성전자", qty=10, price=75000, reason="score 85")],
            take_profits=[TradeEntry(symbol="000660", name="SK하이닉스", qty=5, price=120000, pnl_krw=50000, pnl_pct=9.1, reason="TP +7%")],
            stop_losses=[TradeEntry(symbol="035720", name="카카오", qty=10, price=55000, pnl_krw=-30000, pnl_pct=-5.2, reason="거래량 부족 신호 간과")],
        ),
        portfolio=PortfolioState(
            total_value=10_500_000, daily_pnl_krw=20_000,
            daily_pnl_pct=0.2, cumulative_return_pct=5.0, mdd_pct=-3.5,
        ),
        risk=RiskStatus(daily_loss_usage_pct=30.0, circuit_breaker_active=False),
        next_actions=["watchlist 업데이트", "내일 09:10 스크리닝"],
    )
    base.update(overrides)
    return DailyReport(**base)


# --- render_text ------------------------------------------------------------

def test_render_text_contains_all_sections():
    txt = render_text(_full_report())
    # 필수 섹션 헤더 전부 포함
    for header in (
        "2026-04-19 매매 리포트",
        "■ 시장 요약",
        "■ 데이터 수집",
        "■ 매매 실행",
        "■ 포트폴리오 현황",
        "■ 상세 내역",
        "■ 리스크 상태",
        "■ 다음 액션",
    ):
        assert header in txt


def test_render_text_formats_numbers():
    txt = render_text(_full_report())
    assert "2,680.5" in txt
    assert "+0.80%" in txt
    assert "-0.30%" in txt
    assert "1,345.0" in txt


def test_render_text_no_trades_shows_placeholder():
    txt = render_text(_full_report(trades=TradeExecutionSummary()))
    assert "(거래 없음)" in txt


def test_render_text_handles_cb_active():
    r = _full_report(
        risk=RiskStatus(
            daily_loss_usage_pct=100.0,
            circuit_breaker_active=True,
            circuit_breaker_reason="연속 3일 손실",
            consecutive_loss_days=3,
        )
    )
    txt = render_text(r)
    assert "Circuit Breaker: 발동" in txt
    assert "연속 손실: 3일" in txt


def test_render_text_no_next_actions_shows_none():
    txt = render_text(_full_report(next_actions=[]))
    assert "(없음)" in txt


def test_render_text_reason_annotation():
    txt = render_text(_full_report())
    # 매수는 '근거', 음수 청산은 '반성'
    assert "근거: score 85" in txt
    assert "반성: 거래량 부족 신호 간과" in txt


# --- render_embed -----------------------------------------------------------

def test_embed_color_profit_when_positive_pnl():
    r = _full_report()
    emb = render_embed(r)
    assert emb["color"] == COLOR_PROFIT


def test_embed_color_loss_when_negative_pnl():
    r = _full_report(
        portfolio=PortfolioState(
            total_value=10_000_000, daily_pnl_pct=-1.5, daily_pnl_krw=-150_000
        )
    )
    assert render_embed(r)["color"] == COLOR_LOSS


def test_embed_color_warning_when_cb_active():
    r = _full_report(
        risk=RiskStatus(circuit_breaker_active=True, circuit_breaker_reason="x"),
        portfolio=PortfolioState(daily_pnl_pct=5.0),  # 이익이어도 CB가 우선
    )
    assert render_embed(r)["color"] == COLOR_WARNING


def test_embed_has_all_primary_fields():
    emb = render_embed(_full_report())
    names = [f["name"] for f in emb["fields"]]
    assert "■ 시장 요약" in names
    assert "■ 데이터 수집" in names
    assert "■ 매매 실행" in names
    assert "■ 포트폴리오" in names
    assert "■ 리스크" in names
    assert "■ 다음 액션" in names


def test_embed_footer_contains_strategy_version():
    emb = render_embed(_full_report())
    assert "strategy_version=v1.0" in emb["footer"]["text"]


def test_embed_neutral_color_when_flat():
    r = _full_report(portfolio=PortfolioState(daily_pnl_pct=0.0))
    assert render_embed(r)["color"] == COLOR_NEUTRAL


# --- send_daily_report ------------------------------------------------------

def test_send_default_uses_embed():
    notifier = MagicMock()
    notifier.send_embed.return_value = True
    send_daily_report(notifier, _full_report())
    notifier.send_embed.assert_called_once()
    notifier.send.assert_not_called()


def test_send_text_mode_uses_plain():
    notifier = MagicMock()
    notifier.send.return_value = True
    send_daily_report(notifier, _full_report(), mode="text")
    notifier.send.assert_called_once()
    notifier.send_embed.assert_not_called()
