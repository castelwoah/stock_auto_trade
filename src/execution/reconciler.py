"""Reconciler — KIS 잔고 × DB Position 교차검증 (D6).

v2 추가사항:
  - False positive 방지: 1분 유예 후 재시도, **3회 연속 불일치** 시 OFF.
  - RiskEvent 테이블에 event_type="reconcile_mismatch" 로 기록 (rules §4.2 스키마 변경 금지).
  - 수동 재가동 UX: config 플래그 `TRADING_DISABLED` 와 연계 (runbook 3.3 절차 참조).

**서브에이전트 금지 영역** — 상태 정합성은 단일 논리 흐름 유지.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.clients.kis import KisClient
from src.persistence.models import Position, RiskEvent
from src.common.clock import now_kst


logger = logging.getLogger(__name__)


MAX_CONSECUTIVE_MISMATCH = 3
RETRY_GRACE_SECONDS = 60


@dataclass(frozen=True)
class ReconcileResult:
    ok: bool
    detail: str = ""
    kis_holdings: dict[str, int] = field(default_factory=dict)
    db_holdings: dict[str, int] = field(default_factory=dict)
    diffs: list[tuple[str, int, int]] = field(default_factory=list)  # (symbol, kis_qty, db_qty)


class Reconciler:
    """KIS 잔고와 DB Position 을 교차검증해 매매 전면 OFF 를 결정.

    외부 연계:
      - 불일치 → RiskEvent row INSERT (audit trail)
      - 연속 실패 카운터는 in-memory (프로세스 재시작 시 0부터 재시작, 안전한 기본)
      - 최종 판정은 `verify()` 반환값으로, 상위(pipeline/scheduler)가 매매 OFF 플래그 제어
    """

    def __init__(
        self,
        session: Session,
        kis_client: KisClient,
        *,
        max_consecutive: int = MAX_CONSECUTIVE_MISMATCH,
        retry_grace_seconds: int = RETRY_GRACE_SECONDS,
        sleep_fn=time.sleep,
        managed_only: bool = False,
    ) -> None:
        self.session = session
        self.kis = kis_client
        # True: 봇이 한 번이라도 다룬 종목(Position 행 존재)만 비교 — 수동 보유 종목이
        # 있는 계좌용. 같은 종목을 수동·봇이 함께 보유하면 여전히 불일치로 잡힌다(안전).
        self.managed_only = managed_only
        self.max_consecutive = max_consecutive
        self.retry_grace_seconds = retry_grace_seconds
        self._sleep = sleep_fn
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    def verify(self) -> ReconcileResult:
        """단일 검사 — 불일치 시 RiskEvent 기록 후 카운터 증가.

        DB 는 `(symbol, strategy_id)` 복합 PK 라 같은 symbol 을 여러 전략이 보유
        가능. KIS 잔고는 symbol 단위이므로 **symbol 총합**으로 비교한다.
        """
        kis_holdings = self._read_kis_holdings()
        db_holdings = self._read_db_holdings()
        if self.managed_only:
            managed = self._managed_symbols()
            external = sorted(set(kis_holdings) - managed)
            if external:
                logger.info("reconcile: ignoring external holdings %s", external)
            kis_holdings = {s: q for s, q in kis_holdings.items() if s in managed}
        diffs: list[tuple[str, int, int]] = []
        all_symbols = set(kis_holdings) | set(db_holdings)
        for sym in sorted(all_symbols):
            kq = kis_holdings.get(sym, 0)
            dq = db_holdings.get(sym, 0)
            if kq != dq:
                diffs.append((sym, kq, dq))
        if not diffs:
            self._consecutive_failures = 0
            return ReconcileResult(ok=True, kis_holdings=kis_holdings, db_holdings=db_holdings)
        self._consecutive_failures += 1
        detail = self._format_diffs(diffs)
        self._record_event(detail, diffs)
        return ReconcileResult(
            ok=False, detail=detail,
            kis_holdings=kis_holdings, db_holdings=db_holdings, diffs=diffs,
        )

    def verify_with_grace(self) -> ReconcileResult:
        """`verify()` 를 실패 시 1분 간격으로 최대 `max_consecutive` 회까지 재시도.

        True 가 한 번이라도 돌아오면 즉시 성공 반환. 끝까지 실패면 최종 False.
        """
        for attempt in range(self.max_consecutive):
            result = self.verify()
            if result.ok:
                return result
            if attempt < self.max_consecutive - 1:
                logger.warning(
                    "reconcile mismatch (attempt %d/%d), retrying in %ds",
                    attempt + 1, self.max_consecutive, self.retry_grace_seconds,
                )
                self._sleep(self.retry_grace_seconds)
        # 모두 실패
        return result

    def trading_off_required(self) -> bool:
        """연속 실패 카운터가 임계 이상이면 True → pipeline 이 매매 전면 OFF."""
        return self._consecutive_failures >= self.max_consecutive

    def reset(self) -> None:
        """수동 재가동 시 호출 (runbook 3.3)."""
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _read_kis_holdings(self) -> dict[str, int]:
        holdings, _ = self.kis.get_stock_balance()
        return dict(holdings)

    def _read_db_holdings(self) -> dict[str, int]:
        rows = self.session.execute(
            select(Position).where(Position.qty > 0)
        ).scalars().all()
        out: dict[str, int] = {}
        for r in rows:
            out[r.symbol] = out.get(r.symbol, 0) + r.qty
        return out

    def _managed_symbols(self) -> set[str]:
        """Position 행이 있는(전량 청산돼 qty=0 인 것 포함) 종목 = 봇 관리 종목."""
        return set(self.session.execute(select(Position.symbol)).scalars().all())

    @staticmethod
    def _format_diffs(diffs: list[tuple[str, int, int]]) -> str:
        items = [f"{s}: KIS={k} DB={d}" for s, k, d in diffs]
        return "; ".join(items)

    def _record_event(self, detail: str, diffs: list[tuple[str, int, int]]) -> None:
        self.session.add(RiskEvent(
            ts=now_kst(),
            event_type="reconcile_mismatch",
            symbol=None if len(diffs) > 1 else diffs[0][0],
            detail=detail,
            action_taken=(
                "trading_off" if self._consecutive_failures >= self.max_consecutive
                else "retry_scheduled"
            ),
        ))
        self.session.flush()
