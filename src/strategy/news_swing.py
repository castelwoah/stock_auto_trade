"""News-driven swing strategy (초안).

뉴스/공시 기반 스코어링 파이프라인이 생성한 `candidate_scores` (0~100) 를
입력으로 받아 상위 종목을 선별, 5~10 개 분산 포트폴리오를 구성한다.

breakout 과의 주요 차이:
  * 당일 청산 없음 — 다음날 이월 (스윙).
  * 진입 기준은 기술적 돌파가 아니라 외부 스코어 임계치.
  * 스코어 붕괴(점수 < 50) 를 이벤트 청산 사유로 활용.

모든 메서드는 pure — I/O·DB 호출 없음.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.strategy.base import Action, Signal, Strategy, StrategyContext
from src.strategy.exit_signals import evaluate_exit, make_exit_signal


DEFAULT_TARGET_POSITION_COUNT = 5
DEFAULT_MIN_SCORE = 70.0
DEFAULT_SCORE_COLLAPSE = 50.0
ENTRY_CUTOFF_HOUR = 15   # 15:00 이후 신규 매수 중단


@dataclass
class NewsSwingStrategy(Strategy):
    strategy_id: str = "news_swing"
    target_position_count: int = DEFAULT_TARGET_POSITION_COUNT
    min_score: float = DEFAULT_MIN_SCORE
    score_collapse_threshold: float = DEFAULT_SCORE_COLLAPSE

    def __post_init__(self) -> None:
        if self.target_position_count <= 0:
            raise ValueError(
                f"target_position_count must be positive, got {self.target_position_count}"
            )
        super().__init__()

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------
    def decide_entries(self, ctx: StrategyContext) -> list[Signal]:
        # 장 마감 임박(15:00+) 신규 매수 중단
        if ctx.as_of.hour >= ENTRY_CUTOFF_HOUR:
            return []

        if ctx.cash_available <= 0:
            return []

        owned = {p.symbol for p in self.owned_positions(ctx)}
        available_slots = self.target_position_count - len(owned)
        if available_slots <= 0:
            return []

        # 임계치 이상인 후보를 score 내림차순 정렬
        ranked = sorted(
            (
                (symbol, score)
                for symbol, score in ctx.candidate_scores.items()
                if score is not None and score >= self.min_score
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )

        # 사이즈: 전체 target 기준 균등 분할 (순차 진입 일관성 유지)
        per_position = ctx.cash_available / self.target_position_count

        signals: list[Signal] = []
        for symbol, score in ranked:
            if symbol in owned:
                continue
            price = ctx.current_prices.get(symbol)
            if price is None or price <= 0:
                continue
            sig_id = f"news_swing-{symbol}-{ctx.today.isoformat()}"
            signals.append(
                Signal(
                    action=Action.BUY,
                    symbol=symbol,
                    strategy_id=self.strategy_id,
                    notional=per_position,
                    reason=f"score {score:.1f} >= {self.min_score}",
                    signal_id=sig_id,
                )
            )
            if len(signals) >= available_slots:
                break
        return signals

    # ------------------------------------------------------------------
    # Exits
    # ------------------------------------------------------------------
    def decide_exits(self, ctx: StrategyContext) -> list[Signal]:
        extras: dict[str, Any] = ctx.extras or {}
        halted_symbols: set[str] = set(extras.get("halted_symbols") or ())
        newly_managed: set[str] = set(extras.get("newly_managed_symbols") or ())
        vi_counts: dict[str, int] = extras.get("vi_counts") or {}
        daily_loss_triggered: bool = bool(extras.get("daily_loss_triggered", False))

        exits: list[Signal] = []
        for p in self.owned_positions(ctx):
            current_score = ctx.candidate_scores.get(p.symbol)
            if current_score is None:
                current_score = 50.0  # 중립
            score_drop = current_score < self.score_collapse_threshold

            decision = evaluate_exit(
                p,
                is_halted=(p.symbol in halted_symbols),
                is_newly_managed=(p.symbol in newly_managed),
                consecutive_vi_count=int(vi_counts.get(p.symbol, 0)),
                score_drop_below_50=score_drop,
                daily_loss_triggered=daily_loss_triggered,
            )
            if decision.exit:
                sid = (
                    f"news_swing-exit-{p.symbol}-{decision.reason}"
                    f"-{ctx.today.isoformat()}"
                )
                exits.append(make_exit_signal(p, decision, signal_id=sid))
        return exits
