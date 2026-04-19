from unittest.mock import MagicMock

from src.reporting.daily_report import (
    COLOR_LOSS,
    COLOR_NEUTRAL,
    COLOR_PROFIT,
    COLOR_WARNING,
)
from src.reporting.intraday import (
    notify_buy,
    notify_risk_event,
    notify_sell,
    render_buy_embed,
    render_risk_embed,
    render_sell_embed,
)
from src.reporting.models import TradeEntry


def test_buy_embed_is_green_and_has_quantity():
    e = render_buy_embed(
        TradeEntry(symbol="005930", name="삼성전자", qty=10, price=75000, reason="score 85"),
        strategy_id="news_swing",
    )
    assert e["title"] == "🟢 BUY"
    assert e["color"] == COLOR_PROFIT
    assert "삼성전자" in e["description"]
    assert "750,000" in e["description"]  # total
    assert "news_swing" in e["description"]
    assert "근거: score 85" in e["description"]


def test_buy_embed_without_reason_skips_reason_line():
    e = render_buy_embed(TradeEntry(symbol="A", qty=1, price=1000))
    assert "근거" not in e["description"]


def test_sell_embed_green_when_positive_pnl():
    e = render_sell_embed(
        TradeEntry(symbol="A", qty=10, price=1000, pnl_pct=7.5, pnl_krw=75000)
    )
    assert e["color"] == COLOR_PROFIT
    assert "+7.50%" in e["description"]
    assert "+₩75,000" in e["description"]
    assert "🔵" in e["title"] or "🟢" in e["title"]  # title is 🔵 SELL for profit


def test_sell_embed_red_when_loss():
    e = render_sell_embed(
        TradeEntry(symbol="A", qty=10, price=1000, pnl_pct=-5.0, pnl_krw=-50000)
    )
    assert e["color"] == COLOR_LOSS
    assert "🔴 SELL" == e["title"]
    assert "-5.00%" in e["description"]
    assert "-₩50,000" in e["description"]


def test_sell_embed_neutral_when_zero_pnl():
    e = render_sell_embed(TradeEntry(symbol="A", qty=10, price=1000, pnl_pct=0.0, pnl_krw=0.0))
    assert e["color"] == COLOR_NEUTRAL


def test_sell_embed_no_pnl_available():
    e = render_sell_embed(TradeEntry(symbol="A", qty=10, price=1000))
    # pnl_pct 없으면 손익 표기 생략되고 색상은 NEUTRAL
    assert "손익" not in e["description"]
    assert e["color"] == COLOR_NEUTRAL


def test_risk_embed_orange_and_formats():
    e = render_risk_embed(
        event_type="circuit_breaker",
        symbol=None,
        detail="연속 3일 손실",
        action_taken="신규 매수 중단",
    )
    assert e["color"] == COLOR_WARNING
    assert "⚠️" in e["title"]
    assert "시장 전체" in e["description"]
    assert "조치: 신규 매수 중단" in e["description"]


def test_risk_embed_with_symbol():
    e = render_risk_embed(event_type="blacklist", symbol="005930", detail="관리종목")
    assert "005930" in e["description"]
    assert "관리종목" in e["description"]


# --- transport helpers -----------------------------------------------------

def test_notify_buy_calls_send_embed():
    n = MagicMock()
    n.send_embed.return_value = True
    assert notify_buy(n, TradeEntry(symbol="A", qty=1, price=100)) is True
    n.send_embed.assert_called_once()


def test_notify_sell_calls_send_embed():
    n = MagicMock()
    n.send_embed.return_value = True
    assert notify_sell(n, TradeEntry(symbol="A", qty=1, price=100, pnl_pct=5.0)) is True


def test_notify_risk_event_calls_send_embed():
    n = MagicMock()
    n.send_embed.return_value = False  # 실패도 그대로 반환
    res = notify_risk_event(n, event_type="x", symbol=None, detail="d")
    assert res is False
