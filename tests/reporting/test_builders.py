"""Builder 함수 DB 통합 테스트."""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.persistence.models import (
    Base,
    NewsArticle,
    Position,
    RiskEvent,
    StockScore,
    Trade,
)
from src.reporting.builders import (
    build_daily_report,
    build_midday_snapshot,
)
from src.reporting.models import (
    MarketSummary,
    PortfolioState,
    RiskStatus,
)


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _add_trade(session, **kw):
    defaults = dict(
        symbol="A", side="BUY", qty=10, price=1000.0,
        strategy_id="news_swing", strategy_ver="v1",
        signal_id="s1", idempotency_key=f"idem-{id(kw)}",
        status="filled", ts=datetime(2026, 4, 20, 10, 0, 0),
        note="",
    )
    defaults.update(kw)
    t = Trade(**defaults)
    session.add(t)
    session.flush()
    return t


def _add_risk_block(session, **kw):
    defaults = dict(
        ts=datetime(2026, 4, 20, 11, 0, 0),
        event_type="risk_blocked",
        symbol="A",
        detail="BUY A: blacklist",
        action_taken="blacklist",
    )
    defaults.update(kw)
    e = RiskEvent(**defaults)
    session.add(e)
    session.flush()
    return e


def _add_score(session, **kw):
    defaults = dict(
        score_date=date(2026, 4, 20), symbol="A",
        news_score=80, financial_score=70, technical_score=75, flow_score=60,
        total_score=75.0, scoring_ver="v1",
    )
    defaults.update(kw)
    s = StockScore(**defaults)
    session.add(s)
    session.flush()
    return s


def _add_position(session, **kw):
    defaults = dict(
        symbol="A", strategy_id="news_swing",
        qty=10, avg_price=1000.0,
        opened_at=datetime(2026, 4, 20, 10, 0, 0),
    )
    defaults.update(kw)
    p = Position(**defaults)
    session.add(p)
    session.flush()
    return p


# ============================================================================
# build_daily_report
# ============================================================================

def test_daily_report_empty_day(session):
    r = build_daily_report(session, report_date=date(2026, 4, 20))
    assert r.trades.buys == []
    assert r.trades.rejections == []
    assert r.data.news_count == 0


def test_daily_report_classifies_filled_buy_sell(session):
    _add_trade(session, symbol="A", side="BUY", qty=10, price=1000,
               idempotency_key="b1", status="filled",
               ts=datetime(2026, 4, 20, 10))
    _add_trade(session, symbol="A", side="SELL", qty=10, price=1100,
               idempotency_key="s1", status="filled",
               ts=datetime(2026, 4, 20, 14))
    session.commit()
    r = build_daily_report(session, report_date=date(2026, 4, 20))
    assert len(r.trades.buys) == 1
    assert len(r.trades.take_profits) == 1  # 이익
    assert r.trades.take_profits[0].pnl_pct > 5


def test_daily_report_loss_goes_to_stop_losses(session):
    _add_trade(session, side="BUY", qty=10, price=1000, idempotency_key="b",
               status="filled", ts=datetime(2026, 4, 20, 10))
    _add_trade(session, side="SELL", qty=10, price=900, idempotency_key="s",
               status="filled", ts=datetime(2026, 4, 20, 14))
    session.commit()
    r = build_daily_report(session, report_date=date(2026, 4, 20))
    assert len(r.trades.stop_losses) == 1
    assert r.trades.stop_losses[0].pnl_krw < 0


def test_daily_report_rejections(session):
    _add_trade(session, idempotency_key="r1", status="rejected", note="시간외")
    session.commit()
    r = build_daily_report(session, report_date=date(2026, 4, 20))
    assert r.trades.rejection_count == 1
    assert r.trades.rejections[0].reason_detail == "시간외"


def test_daily_report_cancellations_with_breakdown(session):
    _add_trade(session, symbol="A", idempotency_key="c1", status="cancelled_timeout")
    _add_trade(session, symbol="B", idempotency_key="c2", status="cancelled_timeout")
    _add_trade(session, symbol="C", idempotency_key="c3", status="cancelled_eod")
    session.commit()
    r = build_daily_report(session, report_date=date(2026, 4, 20))
    assert r.trades.cancellation_count == 3
    breakdown = r.trades.cancellation_breakdown
    assert breakdown == {"timeout": 2, "eod_cancel": 1}


def test_daily_report_risk_blocks(session):
    _add_risk_block(session, action_taken="blacklist", symbol="A")
    _add_risk_block(session, action_taken="blacklist", symbol="B")
    _add_risk_block(session, action_taken="cooldown", symbol="C")
    session.commit()
    r = build_daily_report(session, report_date=date(2026, 4, 20))
    assert r.trades.risk_block_count == 3
    assert r.trades.risk_block_breakdown == {"blacklist": 2, "cooldown": 1}


def test_daily_report_filters_by_date(session):
    # 다른 날짜의 데이터는 제외되어야
    _add_trade(session, idempotency_key="prev", status="filled",
               ts=datetime(2026, 4, 19, 10))   # 전일
    _add_trade(session, idempotency_key="curr", status="filled",
               ts=datetime(2026, 4, 20, 10))
    session.commit()
    r = build_daily_report(session, report_date=date(2026, 4, 20))
    assert len(r.trades.buys) == 1


def test_daily_report_counts_news_and_scores(session):
    for i in range(3):
        session.add(NewsArticle(
            source="dart", external_id=f"ex{i}",
            url=f"https://x/{i}", url_hash=f"h{i}",
            title="t", body_text="b",
            published_at=datetime(2026, 4, 20, 9, i),
        ))
    _add_score(session, symbol="X", total_score=75)
    _add_score(session, symbol="Y", total_score=65)   # 후보 아님
    session.commit()
    r = build_daily_report(session, report_date=date(2026, 4, 20))
    assert r.data.news_count == 3
    assert r.data.analyzed_count == 2
    assert r.data.candidates_count == 1


def test_daily_report_injects_external_state(session):
    market = MarketSummary(kospi=2680.0, kospi_change_pct=0.5)
    portfolio = PortfolioState(total_value=10_500_000, daily_pnl_pct=1.2)
    risk = RiskStatus(daily_loss_usage_pct=30)
    r = build_daily_report(
        session, report_date=date(2026, 4, 20),
        market=market, portfolio=portfolio, risk=risk,
        next_actions=["tomorrow 09:10 screen"],
    )
    assert r.market.kospi == 2680.0
    assert r.portfolio.daily_pnl_pct == 1.2
    assert r.risk.daily_loss_usage_pct == 30
    assert r.next_actions == ["tomorrow 09:10 screen"]


# ============================================================================
# build_midday_snapshot
# ============================================================================

def test_midday_snapshot_empty(session):
    snap = build_midday_snapshot(session, ts=datetime(2026, 4, 20, 12, 0))
    assert snap.morning.new_buys == []
    assert snap.positions == []
    assert snap.top_candidates == []


def test_midday_morning_filter_excludes_early_and_late(session):
    _add_trade(session, idempotency_key="early", status="filled",
               ts=datetime(2026, 4, 20, 8, 0))   # 이른 시각 제외
    _add_trade(session, idempotency_key="morning", status="filled",
               ts=datetime(2026, 4, 20, 10, 0))  # 포함
    _add_trade(session, idempotency_key="late", status="filled",
               ts=datetime(2026, 4, 20, 14, 0))  # 오후 제외
    session.commit()
    snap = build_midday_snapshot(session, ts=datetime(2026, 4, 20, 12, 0))
    assert len(snap.morning.new_buys) == 1


def test_midday_positions_computed_with_prices(session):
    _add_position(session, symbol="A", qty=10, avg_price=1000)
    session.commit()
    snap = build_midday_snapshot(
        session, ts=datetime(2026, 4, 20, 12, 0),
        current_prices={"A": 1100},
    )
    assert len(snap.positions) == 1
    p = snap.positions[0]
    assert p.current_price == 1100
    assert p.unrealized_pnl_pct == pytest.approx(10.0)
    assert p.unrealized_pnl_krw == 1000


def test_midday_top_candidates_ordered(session):
    _add_score(session, symbol="X", total_score=90)
    _add_score(session, symbol="Y", total_score=80)
    _add_score(session, symbol="Z", total_score=75)
    _add_score(session, symbol="W", total_score=65)   # 70 미만 제외
    session.commit()
    snap = build_midday_snapshot(session, ts=datetime(2026, 4, 20, 12, 0), top_n_candidates=5)
    syms = [c.symbol for c in snap.top_candidates]
    assert syms == ["X", "Y", "Z"]


def test_midday_top_candidates_respects_limit(session):
    for i in range(10):
        _add_score(session, symbol=f"S{i}", total_score=90 - i)
    session.commit()
    snap = build_midday_snapshot(session, ts=datetime(2026, 4, 20, 12, 0), top_n_candidates=3)
    assert len(snap.top_candidates) == 3


def test_midday_anomalies_populated(session):
    """Daily Loss 90% risk → critical anomaly 자동 포함."""
    snap = build_midday_snapshot(
        session, ts=datetime(2026, 4, 20, 12, 0),
        risk=RiskStatus(daily_loss_usage_pct=95),
    )
    assert any(a.code == "daily_loss_near_limit" and a.severity == "critical"
               for a in snap.anomalies)


def test_midday_morning_risk_blocks(session):
    _add_risk_block(session, ts=datetime(2026, 4, 20, 11, 0), action_taken="blacklist")
    _add_risk_block(session, ts=datetime(2026, 4, 20, 13, 30), action_taken="cooldown")  # 오후 제외
    session.commit()
    snap = build_midday_snapshot(session, ts=datetime(2026, 4, 20, 12, 0))
    assert len(snap.morning.risk_blocks) == 1
    assert snap.morning.risk_blocks[0].reason_code == "blacklist"


def test_midday_total_equity_and_unrealized(session):
    _add_position(session, symbol="A", qty=10, avg_price=1000)
    session.commit()
    snap = build_midday_snapshot(
        session, ts=datetime(2026, 4, 20, 12, 0),
        current_prices={"A": 1100},
        cash_available=5_000_000,
    )
    # cash 5M + position (10*1100) = 5_011_000
    assert snap.total_equity == pytest.approx(5_011_000)
    # 미실현: (11000 - 10000) / 10000 = 10%
    assert snap.unrealized_pnl_pct == pytest.approx(10.0)


def test_midday_dominant_factor_detection(session):
    # news 가중치 0.30 이 가장 큼: news=100, others=0 → news 가 dominant
    _add_score(session, symbol="N",
               news_score=100, financial_score=0, technical_score=0, flow_score=0,
               total_score=100)
    session.commit()
    snap = build_midday_snapshot(session, ts=datetime(2026, 4, 20, 12, 0))
    assert snap.top_candidates[0].dominant_factor == "news"
