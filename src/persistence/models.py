from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from src.common.clock import now_kst


class Base(DeclarativeBase):
    pass


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    simhash64: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=now_kst)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    symbols: Mapped[list["ArticleSymbol"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_article_source_ext"),
        Index("ix_article_source_pub", "source", "published_at"),
    )


class ArticleSymbol(Base):
    """Article ↔ Symbol mapping with ambiguity confidence (D4)."""

    __tablename__ = "article_symbols"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("news_articles.id", ondelete="CASCADE"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    article: Mapped[NewsArticle] = relationship(back_populates="symbols")


class StockScore(Base):
    """Daily composite score per symbol. Versioned for reproducibility (D12)."""

    __tablename__ = "stock_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    score_date: Mapped[date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    news_score: Mapped[float] = mapped_column(Float, default=0.0)
    financial_score: Mapped[float] = mapped_column(Float, default=0.0)
    technical_score: Mapped[float] = mapped_column(Float, default=0.0)
    flow_score: Mapped[float] = mapped_column(Float, default=0.0)
    total_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    scoring_ver: Mapped[str] = mapped_column(String(32))
    sentiment_model_ver: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=now_kst)

    __table_args__ = (
        UniqueConstraint(
            "score_date", "symbol", "scoring_ver", name="uq_score_daily_ver"
        ),
    )


class Trade(Base):
    """Order/fill ledger. `idempotency_key` UNIQUE blocks duplicate trades (D8)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True, default=now_kst)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    side: Mapped[str] = mapped_column(String(4))  # BUY / SELL
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_dvsn: Mapped[str] = mapped_column(String(4), default="00")  # 00 지정 / 01 시장
    strategy_id: Mapped[str] = mapped_column(String(32), default="breakout")
    strategy_ver: Mapped[str] = mapped_column(String(32), default="v0")
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="submitted")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_trade_idem"),
        Index("ix_trade_symbol_strategy", "symbol", "strategy_id"),
    )


class Position(Base):
    """Current position. Composite PK (symbol, strategy_id) enforces D2 격리."""

    __tablename__ = "positions"

    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # D3 trailing
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    halt_flag: Mapped[bool] = mapped_column(default=False)  # D13 거래정지 표시
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_kst, onupdate=now_kst
    )


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_kst, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str | None] = mapped_column(String(12), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_taken: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DailyPnl(Base):
    """Daily P&L snapshot — `start_equity` is KIS-query at 09:00 (D7)."""

    __tablename__ = "daily_pnl"

    pnl_date: Mapped[date] = mapped_column(Date, primary_key=True)
    start_equity: Mapped[float] = mapped_column(Float)
    end_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    mdd_today: Mapped[float] = mapped_column(Float, default=0.0)
    circuit_breaker_triggered: Mapped[bool] = mapped_column(default=False)


class CashFlow(Base):
    """Deposits/withdrawals kept apart from daily_pnl to avoid loss-rate skew (D7)."""

    __tablename__ = "cash_flow"

    id: Mapped[int] = mapped_column(primary_key=True)
    flow_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float)  # 양수 입금, 음수 출금
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
