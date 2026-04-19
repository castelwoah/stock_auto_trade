"""Report data models. Pure dataclasses — no I/O.

rules §7.1 포맷을 그대로 재현할 수 있도록 필드를 매핑한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


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
class TradeExecutionSummary:
    buys: list[TradeEntry] = field(default_factory=list)
    take_profits: list[TradeEntry] = field(default_factory=list)
    stop_losses: list[TradeEntry] = field(default_factory=list)

    @property
    def buy_total_krw(self) -> float:
        return sum(t.qty * t.price for t in self.buys)

    @property
    def tp_total_krw(self) -> float:
        return sum((t.pnl_krw or 0.0) for t in self.take_profits)

    @property
    def sl_total_krw(self) -> float:
        return sum((t.pnl_krw or 0.0) for t in self.stop_losses)


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
class DailyReport:
    report_date: date
    market: MarketSummary = field(default_factory=MarketSummary)
    data: DataCollectionSummary = field(default_factory=DataCollectionSummary)
    trades: TradeExecutionSummary = field(default_factory=TradeExecutionSummary)
    portfolio: PortfolioState = field(default_factory=PortfolioState)
    risk: RiskStatus = field(default_factory=RiskStatus)
    next_actions: list[str] = field(default_factory=list)
    strategy_version: str = "v1.0"
