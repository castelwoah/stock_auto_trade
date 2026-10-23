"""Report data models. Pure dataclasses — no I/O.

rules §7.1 포맷을 그대로 재현할 수 있도록 필드를 매핑한다.
v3 추가: AttemptEntry (실패·차단 매매 시도), TradeExecutionSummary 에 3섹션 추가.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


# --- Result code 상수 (Trade.status 와 1:1 매핑, v3) -------------------------
RESULT_FILLED = "filled"
RESULT_REJECTED = "rejected"
RESULT_CANCELLED_TIMEOUT = "cancelled_timeout"
RESULT_CANCELLED_EOD = "cancelled_eod"
RESULT_BLOCKED_RISK = "blocked_risk"


@dataclass(frozen=True)
class MarketSummary:
    kospi: float | None = None
    kospi_change_pct: float | None = None
    kosdaq: float | None = None
    kosdaq_change_pct: float | None = None
    usd_krw: float | None = None


@dataclass(frozen=True)
class DataCollectionSummary:
    news_count: int = 0
    source_distribution: dict[str, int] = field(default_factory=dict)
    analyzed_count: int = 0
    candidates_count: int = 0


@dataclass(frozen=True)
class TradeEntry:
    symbol: str
    name: str = ""
    qty: int = 0
    price: float = 0.0
    pnl_krw: float | None = None   # 청산 시 손익 금액
    pnl_pct: float | None = None   # 청산 시 손익률 (%)
    reason: str = ""


@dataclass(frozen=True)
class AttemptEntry:
    """매매 시도 기록 — 체결 성공 외 결과를 포괄 (v3).

    사용처:
      - rejected: KIS 주문 거절 (rt_cd != '0')
      - cancelled_timeout: 3분 미체결 × 2회 재시도 후 취소
      - cancelled_eod: 15:15 일괄 취소
      - blocked_risk: @risk_gate 9 게이트 중 차단
    """

    ts: datetime
    symbol: str
    name: str = ""
    action: str = ""          # "BUY" | "SELL"
    qty: int = 0
    price: float = 0.0
    strategy_id: str = ""
    signal_id: str | None = None
    result: str = ""          # RESULT_* 상수 중 하나
    reason_code: str = ""     # 머신 태그: blacklist / timeout / daily_loss_limit / ...
    reason_detail: str = ""   # 사람 읽는 설명


@dataclass(frozen=True)
class TradeExecutionSummary:
    buys: list[TradeEntry] = field(default_factory=list)
    take_profits: list[TradeEntry] = field(default_factory=list)
    stop_losses: list[TradeEntry] = field(default_factory=list)

    # v3: 시도했으나 성공하지 못한 건들
    rejections: list[AttemptEntry] = field(default_factory=list)
    cancellations: list[AttemptEntry] = field(default_factory=list)
    risk_blocks: list[AttemptEntry] = field(default_factory=list)

    @property
    def buy_total_krw(self) -> float:
        return sum(t.qty * t.price for t in self.buys)

    @property
    def tp_total_krw(self) -> float:
        return sum((t.pnl_krw or 0.0) for t in self.take_profits)

    @property
    def sl_total_krw(self) -> float:
        return sum((t.pnl_krw or 0.0) for t in self.stop_losses)

    @property
    def rejection_count(self) -> int:
        return len(self.rejections)

    @property
    def cancellation_count(self) -> int:
        return len(self.cancellations)

    @property
    def risk_block_count(self) -> int:
        return len(self.risk_blocks)

    @property
    def risk_block_breakdown(self) -> dict[str, int]:
        """reason_code → count. 리포트 embed 에 '카운트만' 표기 (v3 결정 #2)."""
        out: dict[str, int] = {}
        for a in self.risk_blocks:
            code = a.reason_code or "unknown"
            out[code] = out.get(code, 0) + 1
        return out

    @property
    def cancellation_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.cancellations:
            code = a.reason_code or "unknown"
            out[code] = out.get(code, 0) + 1
        return out


@dataclass(frozen=True)
class PortfolioState:
    total_value: float = 0.0
    daily_pnl_krw: float = 0.0
    daily_pnl_pct: float = 0.0
    cumulative_return_pct: float = 0.0
    mdd_pct: float = 0.0


@dataclass(frozen=True)
class RiskStatus:
    daily_loss_usage_pct: float = 0.0   # Daily Loss Limit 대비 사용률 (%)
    circuit_breaker_active: bool = False
    circuit_breaker_reason: str = ""
    consecutive_loss_days: int = 0


@dataclass(frozen=True)
class NewsCandidate:
    symbol: str
    name: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class NewsUniverseSummary:
    """news-universe-v1 — 뉴스 후보 + 섀도 비교(universe vs watchlist 돌파 신호)."""

    scored_count: int = 0
    candidate_count: int = 0
    top: list[NewsCandidate] = field(default_factory=list)
    universe_signals: list[str] = field(default_factory=list)
    static_signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DailyReport:
    report_date: date
    market: MarketSummary = field(default_factory=MarketSummary)
    data: DataCollectionSummary = field(default_factory=DataCollectionSummary)
    trades: TradeExecutionSummary = field(default_factory=TradeExecutionSummary)
    portfolio: PortfolioState = field(default_factory=PortfolioState)
    risk: RiskStatus = field(default_factory=RiskStatus)
    next_actions: list[str] = field(default_factory=list)
    strategy_version: str = "v1.0"
    news_universe: NewsUniverseSummary | None = None


# ---------------------------------------------------------------------------
# MidDay (v3) — 장중 12:00 스냅샷용 데이터 모델
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    name: str = ""
    strategy_id: str = ""
    qty: int = 0
    avg_price: float = 0.0
    current_price: float = 0.0
    unrealized_pnl_krw: float = 0.0
    unrealized_pnl_pct: float = 0.0
    tp_armed: bool = False


@dataclass(frozen=True)
class CandidateSnapshot:
    symbol: str
    name: str = ""
    total_score: float = 0.0
    dominant_factor: str = ""   # 'news' | 'technical' | 'financial' | 'flow'


@dataclass(frozen=True)
class MorningStats:
    new_buys: list[TradeEntry] = field(default_factory=list)
    take_profits: list[TradeEntry] = field(default_factory=list)
    stop_losses: list[TradeEntry] = field(default_factory=list)
    # v3 실패·차단도 오전 집계에 반영
    rejections: list[AttemptEntry] = field(default_factory=list)
    cancellations: list[AttemptEntry] = field(default_factory=list)
    risk_blocks: list[AttemptEntry] = field(default_factory=list)
    realized_pnl_krw: float = 0.0

    @property
    def tp_total_krw(self) -> float:
        return sum((t.pnl_krw or 0.0) for t in self.take_profits)

    @property
    def sl_total_krw(self) -> float:
        return sum((t.pnl_krw or 0.0) for t in self.stop_losses)

    @property
    def risk_block_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.risk_blocks:
            code = a.reason_code or "unknown"
            out[code] = out.get(code, 0) + 1
        return out


@dataclass(frozen=True)
class MidDayAnomaly:
    severity: str    # 'info' | 'warn' | 'critical'
    code: str
    message: str


@dataclass(frozen=True)
class MidDaySnapshot:
    ts: datetime
    market: MarketSummary = field(default_factory=MarketSummary)
    morning: MorningStats = field(default_factory=MorningStats)
    positions: list[PositionSnapshot] = field(default_factory=list)
    top_candidates: list[CandidateSnapshot] = field(default_factory=list)
    total_equity: float = 0.0
    unrealized_pnl_pct: float = 0.0
    risk: RiskStatus = field(default_factory=RiskStatus)
    anomalies: list[MidDayAnomaly] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    strategy_version: str = "v1.0"
