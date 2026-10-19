"""Composite scoring.

Weights per rules §6 Step 2:
  news 30% / financial 25% / technical 25% / flow 20%

Threshold for candidacy: total ≥ 70.
Reproducibility (D12): stock_scores 저장 시 scoring_ver + sentiment_model_ver +
llm_prompt_hash 를 함께 기록.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from typing import Any

from src.analysis.contracts import FactorScore, clip_score
from src.analysis.sentiment import SENTIMENT_MODEL_VER
from src.common.clock import today_kst


SCORING_VERSION = "v1.0"
CANDIDATE_THRESHOLD = 70.0

# Weights MUST sum to 1.0
FACTOR_WEIGHTS: dict[str, float] = {
    "news": 0.30,
    "financial": 0.25,
    "technical": 0.25,
    "flow": 0.20,
}
assert abs(sum(FACTOR_WEIGHTS.values()) - 1.0) < 1e-9


@dataclass(frozen=True)
class CompositeScore:
    symbol: str
    total: float
    news: float
    financial: float
    technical: float
    flow: float
    scoring_ver: str = SCORING_VERSION
    sentiment_model_ver: str = SENTIMENT_MODEL_VER
    llm_prompt_hash: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    insufficient: list[str] = field(default_factory=list)

    @property
    def is_candidate(self) -> bool:
        return self.total >= CANDIDATE_THRESHOLD and not self.insufficient


def compose(
    *,
    symbol: str,
    news: FactorScore,
    financial: FactorScore,
    technical: FactorScore,
    flow: FactorScore,
    llm_prompt_hash: str | None = None,
) -> CompositeScore:
    """Weighted composite. Factors with insufficient_data contribute at 50 (neutral).

    insufficient 리스트를 채워 `is_candidate`를 False 로 만들어 매매 신호를 억제한다.
    """
    factors = {
        "news": news,
        "financial": financial,
        "technical": technical,
        "flow": flow,
    }
    insufficient: list[str] = []
    effective: dict[str, float] = {}
    for name, f in factors.items():
        if f.insufficient_data:
            insufficient.append(name)
            effective[name] = 50.0
        else:
            effective[name] = clip_score(f.score)

    total = sum(effective[k] * FACTOR_WEIGHTS[k] for k in FACTOR_WEIGHTS)
    details = {
        name: {
            "score": effective[name],
            "reason": factors[name].reason,
            "insufficient": factors[name].insufficient_data,
            "details": factors[name].details,
        }
        for name in factors
    }
    return CompositeScore(
        symbol=symbol,
        total=clip_score(total),
        news=effective["news"],
        financial=effective["financial"],
        technical=effective["technical"],
        flow=effective["flow"],
        llm_prompt_hash=llm_prompt_hash,
        details=details,
        insufficient=insufficient,
    )


def prompt_hash(prompt_text: str | None) -> str | None:
    """Stable hash for LLM prompt version tracking (D12)."""
    if not prompt_text:
        return None
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Persistence bridge
# ---------------------------------------------------------------------------

def to_stock_score_row(cs: CompositeScore, *, score_date: _date | None = None) -> dict[str, Any]:
    """Map CompositeScore to StockScore ORM kwargs for bulk insert/upsert."""
    return {
        "score_date": score_date or today_kst(),
        "symbol": cs.symbol,
        "news_score": cs.news,
        "financial_score": cs.financial,
        "technical_score": cs.technical,
        "flow_score": cs.flow,
        "total_score": cs.total,
        "scoring_ver": cs.scoring_ver,
        "sentiment_model_ver": cs.sentiment_model_ver,
        "llm_prompt_hash": cs.llm_prompt_hash,
    }
