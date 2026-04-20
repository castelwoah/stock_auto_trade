"""DB 기반 리포트 조립기 — handler 에서 호출되는 실구현.

책임 분리:
  - **DB 에서 조회 가능한 부분** (trades, risk_events, stock_scores, news_articles)
    은 builder 가 직접 계산.
  - **런타임 상태** (RiskEngine.daily_loss, KIS market data, portfolio value)
    은 호출측이 모아서 주입 (MarketSummary/PortfolioState/RiskStatus).

이렇게 분리하면 builder 는 순수 함수로 테스트 가능 (in-memory SQLite 만 필요).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Iterable

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from src.persistence.models import (
    NewsArticle,
    Position,
    RiskEvent,
    StockScore,
    Trade,
)
from src.reporting.models import (
    AttemptEntry,
    CandidateSnapshot,
    DailyReport,
    DataCollectionSummary,
    MarketSummary,
    MidDayAnomaly,
    MidDaySnapshot,
    MorningStats,
    PortfolioState,
    PositionSnapshot,
    RESULT_BLOCKED_RISK,
    RESULT_CANCELLED_EOD,
    RESULT_CANCELLED_TIMEOUT,
    RESULT_REJECTED,
    RiskStatus,
    TradeEntry,
    TradeExecutionSummary,
)
from src.reporting.midday_report import detect_anomalies
from src.reporting.pnl_calc import compute_realized_pnl, filter_by_time_window


logger = logging.getLogger(__name__)


# 시도 결과 분류 맵 (Trade.status → AttemptEntry.reason_code 변환)
_CANCELLATION_CODE_MAP = {
    "cancelled_timeout": "timeout",
    "cancelled_eod": "eod_cancel",
    "cancelled_manual": "manual",
}
_CANCELLATION_RESULT_MAP = {
    "cancelled_timeout": RESULT_CANCELLED_TIMEOUT,
    "cancelled_eod": RESULT_CANCELLED_EOD,
    "cancelled_manual": RESULT_CANCELLED_TIMEOUT,  # 표시 분류상 취소로 통일
}


# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------

def _day_bounds(d: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(d, time(0, 0, 0)),
        datetime.combine(d, time(23, 59, 59, 999999)),
    )


def _load_trades_by_day(session: Session, d: date) -> list[Trade]:
    start, end = _day_bounds(d)
    rows = (
        session.query(Trade)
        .filter(Trade.ts >= start, Trade.ts <= end)
        .order_by(Trade.ts.asc())
        .all()
    )
    return rows


def _load_risk_blocks_by_day(session: Session, d: date) -> list[RiskEvent]:
    start, end = _day_bounds(d)
    return (
        session.query(RiskEvent)
        .filter(
            RiskEvent.ts >= start,
            RiskEvent.ts <= end,
            RiskEvent.event_type == "risk_blocked",
        )
        .order_by(RiskEvent.ts.asc())
        .all()
    )


def _trade_to_buy_entry(t: Trade) -> TradeEntry:
    return TradeEntry(
        symbol=t.symbol,
        name="",  # 이름 매핑은 향후 watchlist 연동 (Phase 3+)
        qty=t.qty,
        price=t.price,
        reason=t.note or "",
    )


def _trade_to_attempt(t: Trade, *, result: str, reason_code: str) -> AttemptEntry:
    return AttemptEntry(
        ts=t.ts,
        symbol=t.symbol,
        action=t.side,
        qty=t.qty,
        price=t.price,
        strategy_id=t.strategy_id or "",
        signal_id=t.signal_id,
        result=result,
        reason_code=reason_code,
        reason_detail=t.note or "",
    )


def _risk_event_to_attempt(e: RiskEvent) -> AttemptEntry:
    return AttemptEntry(
        ts=e.ts,
        symbol=e.symbol or "",
        action="BUY",  # pipeline 에서 BUY 차단 시에만 기록 (현 구현)
        result=RESULT_BLOCKED_RISK,
        reason_code=e.action_taken or "unknown",
        reason_detail=e.detail or "",
    )


def _classify_trades(trades: Iterable[Trade]) -> tuple[
    list[TradeEntry], list[TradeEntry], list[TradeEntry],
    list[AttemptEntry], list[AttemptEntry],
]:
    """filled/rejected/cancelled 를 섹션별로 분류.

    Returns: (buys, take_profits, stop_losses, rejections, cancellations)
    """
    filled = [t for t in trades if t.status == "filled"]
    rejected = [t for t in trades if t.status == "rejected"]
    cancelled = [t for t in trades if t.status in _CANCELLATION_CODE_MAP]

    buys = [_trade_to_buy_entry(t) for t in filled if t.side.upper() == "BUY"]

    realized = compute_realized_pnl(filled)
    tps: list[TradeEntry] = []
    sls: list[TradeEntry] = []
    for rf in realized.fills:
        entry = TradeEntry(
            symbol=rf.symbol,
            qty=rf.qty,
            price=rf.sell_price,
            pnl_krw=rf.pnl_krw,
            pnl_pct=rf.pnl_pct,
            reason="",
        )
        if rf.pnl_krw > 0:
            tps.append(entry)
        else:
            sls.append(entry)

    rejections = [
        _trade_to_attempt(t, result=RESULT_REJECTED, reason_code="rejected")
        for t in rejected
    ]
    cancellations = [
        _trade_to_attempt(
            t,
            result=_CANCELLATION_RESULT_MAP[t.status],
            reason_code=_CANCELLATION_CODE_MAP[t.status],
        )
        for t in cancelled
    ]
    return buys, tps, sls, rejections, cancellations


# ---------------------------------------------------------------------------
# build_daily_report
# ---------------------------------------------------------------------------

def build_daily_report(
    session: Session,
    *,
    report_date: date,
    market: MarketSummary | None = None,
    portfolio: PortfolioState | None = None,
    risk: RiskStatus | None = None,
    next_actions: list[str] | None = None,
    source_distribution: dict[str, int] | None = None,
    strategy_version: str = "v1.0",
) -> DailyReport:
    """하루치 Trade·RiskEvent·NewsArticle·StockScore 를 종합해 DailyReport 생성."""
    trades = _load_trades_by_day(session, report_date)
    buys, tps, sls, rejections, cancellations = _classify_trades(trades)

    risk_block_events = _load_risk_blocks_by_day(session, report_date)
    risk_blocks = [_risk_event_to_attempt(e) for e in risk_block_events]

    # 뉴스·스코어 카운트
    start, end = _day_bounds(report_date)
    news_count = (
        session.query(sa_func.count(NewsArticle.id))
        .filter(NewsArticle.published_at >= start, NewsArticle.published_at <= end)
        .scalar() or 0
    )
    analyzed_count = (
        session.query(sa_func.count(StockScore.id))
        .filter(StockScore.score_date == report_date)
        .scalar() or 0
    )
    candidates_count = (
        session.query(sa_func.count(StockScore.id))
        .filter(
            StockScore.score_date == report_date,
            StockScore.total_score >= 70,
        )
        .scalar() or 0
    )

    data_summary = DataCollectionSummary(
        news_count=int(news_count),
        source_distribution=source_distribution or {},
        analyzed_count=int(analyzed_count),
        candidates_count=int(candidates_count),
    )
    trade_summary = TradeExecutionSummary(
        buys=buys,
        take_profits=tps,
        stop_losses=sls,
        rejections=rejections,
        cancellations=cancellations,
        risk_blocks=risk_blocks,
    )

    return DailyReport(
        report_date=report_date,
        market=market or MarketSummary(),
        data=data_summary,
        trades=trade_summary,
        portfolio=portfolio or PortfolioState(),
        risk=risk or RiskStatus(),
        next_actions=list(next_actions or []),
        strategy_version=strategy_version,
    )


# ---------------------------------------------------------------------------
# build_midday_snapshot
# ---------------------------------------------------------------------------

MORNING_START = time(9, 10)
MORNING_END = time(12, 0)


def build_midday_snapshot(
    session: Session,
    *,
    ts: datetime,
    current_prices: dict[str, float] | None = None,
    market: MarketSummary | None = None,
    risk: RiskStatus | None = None,
    top_n_candidates: int = 5,
    strategy_version: str = "v1.0",
    cash_available: float = 0.0,
) -> MidDaySnapshot:
    """12:00 시점 스냅샷 — 오전(09:10~12:00) 시도 + 현재 포지션 + 오후 후보."""
    day = ts.date()
    prices = current_prices or {}

    # 1) 오전 Trade
    trades = _load_trades_by_day(session, day)
    morning_trades = filter_by_time_window(trades, day=day, start=MORNING_START, end=MORNING_END)
    buys, tps, sls, rejections, cancellations = _classify_trades(morning_trades)
    realized = compute_realized_pnl([t for t in morning_trades if t.status == "filled"])

    # 2) 오전 risk_blocked (같은 window)
    risk_events_all = _load_risk_blocks_by_day(session, day)
    morning_events = [
        e for e in risk_events_all
        if MORNING_START <= e.ts.time() <= MORNING_END
    ]
    risk_blocks = [_risk_event_to_attempt(e) for e in morning_events]

    morning = MorningStats(
        new_buys=buys,
        take_profits=tps,
        stop_losses=sls,
        rejections=rejections,
        cancellations=cancellations,
        risk_blocks=risk_blocks,
        realized_pnl_krw=realized.total_realized_pnl_krw,
    )

    # 3) 현재 포지션 (qty>0)
    pos_rows = session.query(Position).filter(Position.qty > 0).all()
    positions: list[PositionSnapshot] = []
    total_position_value = 0.0
    total_position_cost = 0.0
    for p in pos_rows:
        cur = prices.get(p.symbol, p.avg_price)
        value = p.qty * cur
        cost = p.qty * p.avg_price
        pnl_krw = value - cost
        pnl_pct = (pnl_krw / cost * 100.0) if cost > 0 else 0.0
        total_position_value += value
        total_position_cost += cost
        positions.append(PositionSnapshot(
            symbol=p.symbol,
            name="",
            strategy_id=p.strategy_id,
            qty=p.qty,
            avg_price=p.avg_price,
            current_price=cur,
            unrealized_pnl_krw=pnl_krw,
            unrealized_pnl_pct=pnl_pct,
            tp_armed=False,  # Position 테이블에 플래그 없음, in-memory 에서만
        ))

    # 4) 오후 후보 top-N
    scores = (
        session.query(StockScore)
        .filter(
            StockScore.score_date == day,
            StockScore.total_score >= 70,
        )
        .order_by(StockScore.total_score.desc())
        .limit(top_n_candidates)
        .all()
    )
    top_candidates = [
        CandidateSnapshot(
            symbol=s.symbol,
            name="",
            total_score=s.total_score,
            dominant_factor=_dominant_factor(s),
        )
        for s in scores
    ]

    # 5) equity / unrealized pct
    total_equity = cash_available + total_position_value
    overall_unrealized_pct = 0.0
    if total_position_cost > 0:
        overall_unrealized_pct = (
            (total_position_value - total_position_cost) / total_position_cost * 100.0
        )

    # 6) snapshot 조립
    snapshot = MidDaySnapshot(
        ts=ts,
        market=market or MarketSummary(),
        morning=morning,
        positions=positions,
        top_candidates=top_candidates,
        total_equity=total_equity,
        unrealized_pnl_pct=overall_unrealized_pct,
        risk=risk or RiskStatus(),
        anomalies=[],  # 아래에서 채움
        next_actions=[],
        strategy_version=strategy_version,
    )

    # anomaly 탐지는 완성된 snapshot 필요 → 재구성
    from dataclasses import replace
    snapshot = replace(snapshot, anomalies=detect_anomalies(snapshot))
    return snapshot


def _dominant_factor(s: StockScore) -> str:
    """4개 sub-score 중 가중 기여도 최대 팩터 이름."""
    weighted = {
        "news": s.news_score * 0.30,
        "financial": s.financial_score * 0.25,
        "technical": s.technical_score * 0.25,
        "flow": s.flow_score * 0.20,
    }
    return max(weighted, key=weighted.get) if weighted else ""
