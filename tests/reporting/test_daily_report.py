from datetime import date, datetime
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
    AttemptEntry,
    DailyReport,
    DataCollectionSummary,
    MarketSummary,
    PortfolioState,
    RESULT_BLOCKED_RISK,
    RESULT_CANCELLED_EOD,
    RESULT_CANCELLED_TIMEOUT,
    RESULT_REJECTED,
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


# --- v3: 실패·차단 섹션 ---------------------------------------------------

def _with_failures(**trade_kwargs) -> DailyReport:
    r = _full_report()
    base = dict(
        buys=r.trades.buys,
        take_profits=r.trades.take_profits,
        stop_losses=r.trades.stop_losses,
    )
    base.update(trade_kwargs)
    # frozen dataclass 이므로 replace 불가 — 새로 구성
    from dataclasses import replace
    new_trades = TradeExecutionSummary(**base)
    return replace(r, trades=new_trades)


def test_render_text_includes_failures_section_when_present():
    r = _with_failures(
        rejections=[
            AttemptEntry(
                ts=datetime(2026, 4, 19, 11, 2),
                symbol="005930", action="BUY", qty=10, price=75000,
                result=RESULT_REJECTED, reason_code="rt_cd_1",
                reason_detail="주문가능시간외",
            ),
        ],
        cancellations=[
            AttemptEntry(
                ts=datetime(2026, 4, 19, 10, 45),
                symbol="035720", action="BUY", qty=5, price=55000,
                result=RESULT_CANCELLED_TIMEOUT, reason_code="timeout",
                reason_detail="3분 미체결 × 2회",
            ),
        ],
        risk_blocks=[
            AttemptEntry(
                ts=datetime(2026, 4, 19, 11, 15),
                symbol="123450", action="BUY", qty=10, price=1000,
                result=RESULT_BLOCKED_RISK, reason_code="blacklist",
            ),
            AttemptEntry(
                ts=datetime(2026, 4, 19, 11, 20),
                symbol="000660", action="BUY", qty=5, price=120000,
                result=RESULT_BLOCKED_RISK, reason_code="cooldown",
            ),
            AttemptEntry(
                ts=datetime(2026, 4, 19, 11, 25),
                symbol="267250", action="BUY", qty=3, price=80000,
                result=RESULT_BLOCKED_RISK, reason_code="blacklist",
            ),
        ],
    )
    txt = render_text(r)
    assert "■ 실패·차단" in txt
    assert "거절(KIS): 1건" in txt
    assert "005930" in txt  # 개별 건 출력 확인
    assert "취소: 1건" in txt and "timeout 1" in txt
    assert "리스크 차단: 3건" in txt
    # code 별 카운트: blacklist 2 / cooldown 1 — 순서 알파벳
    assert "blacklist 2" in txt
    assert "cooldown 1" in txt


def test_render_text_no_failures_omits_section():
    txt = render_text(_full_report())   # 기본 세팅 = 실패 없음
    assert "■ 실패·차단" not in txt


def test_embed_has_failures_field_when_present():
    r = _with_failures(
        risk_blocks=[
            AttemptEntry(
                ts=datetime(2026, 4, 19, 11, 15),
                symbol="X", action="BUY", qty=1, price=100,
                result=RESULT_BLOCKED_RISK, reason_code="daily_loss_limit",
            ),
        ],
    )
    emb = render_embed(r)
    names = [f["name"] for f in emb["fields"]]
    assert "■ 실패·차단" in names


def test_embed_no_failures_field_when_absent():
    emb = render_embed(_full_report())
    names = [f["name"] for f in emb["fields"]]
    assert "■ 실패·차단" not in names


def test_risk_block_breakdown_counts_correctly():
    r = _with_failures(
        risk_blocks=[
            AttemptEntry(ts=datetime(2026, 4, 19, 11, 0),
                         symbol="A", reason_code="blacklist"),
            AttemptEntry(ts=datetime(2026, 4, 19, 11, 1),
                         symbol="B", reason_code="blacklist"),
            AttemptEntry(ts=datetime(2026, 4, 19, 11, 2),
                         symbol="C", reason_code="cooldown"),
        ],
    )
    assert r.trades.risk_block_breakdown == {"blacklist": 2, "cooldown": 1}


def test_truncates_long_rejection_list():
    """거절 건 6개 → 5건만 상세, 나머지 '외 1건'."""
    rejections = [
        AttemptEntry(
            ts=datetime(2026, 4, 19, 10, i),
            symbol=f"SYM{i}", action="BUY", qty=1, price=100,
            result=RESULT_REJECTED, reason_code="x",
        )
        for i in range(6)
    ]
    r = _with_failures(rejections=rejections)
    txt = render_text(r)
    assert "외 1건" in txt


# --- 2026-10-26: 가상 자본 표시 · 누적/MDD 미산출 · 후보 기준 -----------------

def test_virtual_equity_and_missing_cumulative_render():
    r = _full_report(portfolio=PortfolioState(total_value=10_000_000, virtual=True))
    text = render_text(r)
    assert "평가액: ₩10,000,000 (가상)" in text
    assert "누적 수익률: -" in text and "MDD: -" in text
    port = next(f for f in render_embed(r)["fields"] if f["name"] == "■ 포트폴리오")
    assert "(가상)" in port["value"] and "누적: - | MDD: -" in port["value"]


def test_candidate_threshold_label_follows_setting():
    r = _full_report(data=DataCollectionSummary(news_count=1, candidates_count=4,
                                                candidate_min_score=60.0))
    assert "후보 편입: 4개 (스코어 60+)" in render_text(r)
