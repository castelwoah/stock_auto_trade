"""Order lifecycle manager — 지정가 IOC 3분 타임아웃 + 1호가 상향 재주문.

rules §6 Step3:
  - 진입가: 호가창 상위 2호가 이내 지정가 (슬리피지 제어)
  - 3분 내 미체결 → 1호가 상향 재주문
  - 2회 재시도 후 취소 + 로그

v2 추가:
  - 15:00 이후 신규 매수 cut-off 강제
  - 15:15 도달 시 미체결 신규 매수 일괄 취소 (M5)
  - KIS cancel TR 호출은 `cancel_hook` 주입으로 확장 (현 KisClient 미구현 부분 보강)

**서브에이전트 금지 영역** — 주문 생명주기는 체결 순서·idempotency·재시도 규약이
강하게 결합되어 단일 논리 흐름 유지.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable

from sqlalchemy import update as sa_update

from src.clients.kis import KisClient
from src.execution.portfolio import LivePortfolio, TradeRecord
from src.persistence.models import Trade
from src.reporting.intraday import notify_risk_event
from src.clients.discord import DiscordNotifier
from src.strategy.base import Action, Signal


logger = logging.getLogger(__name__)


# Operational constants (rules §5.1, §6 Step3)
MAX_RETRIES = 2
RESUBMIT_AFTER_SECONDS = 180   # 3 min
ENTRY_CUTOFF = time(15, 0)
EOD_CANCEL = time(15, 15)
TICK_UP_FRACTION = 0.005       # 1호가 근사치 (대형주 ~0.5% 기준, 보수적)

# Trade.status 값 상수 — v3 시도 로그 정규화
STATUS_SUBMITTED = "submitted"
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED_TIMEOUT = "cancelled_timeout"   # 재시도 한계 초과
STATUS_CANCELLED_EOD = "cancelled_eod"           # 15:15 일괄 취소
STATUS_CANCELLED_MANUAL = "cancelled_manual"     # 수동 취소 (향후)


# Price band helpers — 호가 단위는 종목별로 다르지만 근사치로 처리
def upward_tick(price: float, fraction: float = TICK_UP_FRACTION) -> float:
    """Round up by ~0.5%. 실 호가 단위 적용은 향후 `kospi_code.mst` 연동."""
    return price * (1.0 + fraction)


@dataclass
class PendingOrder:
    order_id: str
    signal: Signal
    submitted_at: datetime
    price: float
    qty: int
    retries: int = 0
    cancelled: bool = False
    filled: bool = False
    idempotency_key: str = ""


CancelHook = Callable[[str], bool]   # (order_id) → success


class OrderManager:
    """Live order state tracker + 재시도·취소 오케스트레이터."""

    def __init__(
        self,
        *,
        kis_client: KisClient,
        portfolio: LivePortfolio,
        notifier: DiscordNotifier | None = None,
        cancel_hook: CancelHook | None = None,
        now_provider: Callable[[], datetime] = datetime.now,
        max_retries: int = MAX_RETRIES,
        resubmit_after_seconds: int = RESUBMIT_AFTER_SECONDS,
    ) -> None:
        self.kis = kis_client
        self.portfolio = portfolio
        self.notifier = notifier
        self.cancel_hook = cancel_hook
        self._now = now_provider
        self.max_retries = max_retries
        self.resubmit_after_seconds = resubmit_after_seconds
        self.pending: dict[str, PendingOrder] = {}

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------
    def submit(
        self,
        signal: Signal,
        *,
        limit_price: float,
        qty: int,
        idempotency_key: str,
        strategy_ver: str = "v0",
    ) -> PendingOrder | None:
        """Submit a limit order. Entry cut-off at 15:00 for BUY signals."""
        now = self._now()
        if signal.action == Action.BUY and now.time() >= ENTRY_CUTOFF:
            logger.info("BUY after 15:00 cut-off ignored: %s", signal.symbol)
            return None
        if qty <= 0 or limit_price <= 0:
            return None

        # Persistence 에 idempotency 먼저 확정 (동일 key 중복 차단)
        trade = self.portfolio.record_trade(TradeRecord(
            symbol=signal.symbol,
            side=signal.action.value,
            qty=qty,
            price=limit_price,
            strategy_id=signal.strategy_id,
            strategy_ver=strategy_ver,
            signal_id=signal.signal_id,
            idempotency_key=idempotency_key,
            order_id=None,
            order_dvsn="00",  # 지정가
            status="submitted",
            note=signal.reason,
        ))

        # KIS order submission (지정가)
        try:
            res = self.kis.order(
                code=signal.symbol,
                qty=qty,
                side=signal.action.value,
                ord_dvsn="00",
                unit_price=str(int(limit_price)),
            )
        except Exception:
            logger.exception("KIS order failed: %s", signal.symbol)
            trade.status = "rejected"
            return None

        order_id = self._extract_order_id(res)
        trade.order_id = order_id

        pending = PendingOrder(
            order_id=order_id, signal=signal,
            submitted_at=now, price=limit_price, qty=qty,
            idempotency_key=idempotency_key,
        )
        self.pending[order_id] = pending
        return pending

    # ------------------------------------------------------------------
    # Lifecycle ticks — call periodically (e.g., every bar)
    # ------------------------------------------------------------------
    def poll(self, *, current_prices: dict[str, float]) -> None:
        """각 pending 주문에 대해 3분 타임아웃 · 재시도 · EOD 취소 판단.

        호출 전제: 상위 pipeline 이 체결 여부를 다른 경로(예: reconciler 의 KIS
        잔고 diff)로 파악해 `mark_filled()` 호출. 여기서는 **타이밍·재시도**만.
        """
        now = self._now()
        to_remove: list[str] = []
        # snapshot: _resubmit()가 self.pending 에 새 entry 를 추가하므로
        # iteration 중 size change 방지를 위해 리스트로 고정
        for oid, po in list(self.pending.items()):
            if po.filled or po.cancelled:
                to_remove.append(oid)
                continue
            # EOD cancel (신규 매수만)
            if po.signal.action == Action.BUY and now.time() >= EOD_CANCEL:
                self._cancel_and_notify(po, reason="eod_cancel")
                to_remove.append(oid)
                continue
            # 3분 타임아웃 재시도
            age = (now - po.submitted_at).total_seconds()
            if age >= self.resubmit_after_seconds:
                if po.retries >= self.max_retries:
                    self._cancel_and_notify(po, reason="max_retries_exceeded")
                    to_remove.append(oid)
                    continue
                # 재시도: 1호가 상향 재주문. _resubmit 이 기존 po.cancelled=True 처리.
                new_price = upward_tick(current_prices.get(po.signal.symbol, po.price))
                self._resubmit(po, new_price=new_price)
                to_remove.append(oid)   # 기존 entry 제거, 새 ORD 는 pending 에 추가됨
                continue
        for oid in to_remove:
            self.pending.pop(oid, None)

    # ------------------------------------------------------------------
    # External confirmation (체결/취소 결과가 들어왔을 때)
    # ------------------------------------------------------------------
    def mark_filled(self, order_id: str, *, fill_price: float, ts: datetime | None = None) -> None:
        po = self.pending.get(order_id)
        if po is None:
            return
        po.filled = True
        self.portfolio.apply_fill_to_position(
            symbol=po.signal.symbol,
            strategy_id=po.signal.strategy_id,
            side=po.signal.action.value,
            qty=po.qty,
            price=fill_price,
            ts=ts or self._now(),
        )
        # Trade.status 를 filled 로 갱신 (v3 실패·성공 리포트 근거 자료)
        self._update_trade_status(po.idempotency_key, STATUS_FILLED)

    def mark_cancelled(self, order_id: str, *, status: str = STATUS_CANCELLED_MANUAL) -> None:
        po = self.pending.get(order_id)
        if po is not None:
            po.cancelled = True
            self._update_trade_status(po.idempotency_key, status)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resubmit(self, po: PendingOrder, *, new_price: float) -> None:
        # 기존 주문 취소
        if self.cancel_hook is not None:
            try:
                self.cancel_hook(po.order_id)
            except Exception:
                logger.exception("cancel hook failed for %s", po.order_id)
        po.cancelled = True

        # 새 키: 재시도 횟수를 suffix 로 붙여 idempotency 유지
        retry_idem = f"{po.idempotency_key}#r{po.retries + 1}"
        try:
            res = self.kis.order(
                code=po.signal.symbol,
                qty=po.qty,
                side=po.signal.action.value,
                ord_dvsn="00",
                unit_price=str(int(new_price)),
            )
        except Exception:
            logger.exception("resubmit KIS order failed: %s", po.signal.symbol)
            return
        new_id = self._extract_order_id(res)
        new_po = PendingOrder(
            order_id=new_id, signal=po.signal,
            submitted_at=self._now(), price=new_price, qty=po.qty,
            retries=po.retries + 1,
            idempotency_key=retry_idem,
        )
        self.pending[new_id] = new_po
        if self.notifier is not None:
            notify_risk_event(
                self.notifier,
                event_type="order_resubmit",
                symbol=po.signal.symbol,
                detail=f"retry {new_po.retries}/{self.max_retries} at {new_price:.0f}",
            )

    def _cancel_and_notify(self, po: PendingOrder, *, reason: str) -> None:
        if self.cancel_hook is not None:
            try:
                self.cancel_hook(po.order_id)
            except Exception:
                logger.exception("cancel hook failed for %s", po.order_id)
        po.cancelled = True
        # Trade.status 갱신 — 리포트용 분류 (v3)
        status_map = {
            "max_retries_exceeded": STATUS_CANCELLED_TIMEOUT,
            "eod_cancel": STATUS_CANCELLED_EOD,
        }
        self._update_trade_status(
            po.idempotency_key, status_map.get(reason, STATUS_CANCELLED_MANUAL)
        )
        # NOTE: v3 결정 3-(a) — intraday Discord 알림은 과다 방지 위해 미발송.
        # 실패·차단은 daily/midday 리포트에서 집계로 전달.
        logger.info(
            "order cancelled: %s %s reason=%s", po.signal.symbol, po.order_id, reason
        )

    def _update_trade_status(self, idempotency_key: str, status: str) -> None:
        """Trade.status 갱신. 본 OrderManager 는 portfolio 의 session 을 공유."""
        if not idempotency_key:
            return
        try:
            self.portfolio.session.execute(
                sa_update(Trade)
                .where(Trade.idempotency_key == idempotency_key)
                .values(status=status)
            )
            self.portfolio.session.flush()
        except Exception:
            logger.exception("failed to update Trade.status for %s", idempotency_key)

    @staticmethod
    def _extract_order_id(kis_response: dict) -> str:
        """KIS order response 의 주문번호 추출.

        응답 스키마 가정: `{"output": {"ODNO": "0000012345"}}` 또는
        `{"output": {"KRX_FWDG_ORD_ORGNO": "..", "ODNO": "..."}}`. 실제 운영에서
        TR spec 확인 후 보강 필요.
        """
        out = (kis_response or {}).get("output") or {}
        odno = out.get("ODNO") or out.get("odno") or ""
        if not odno:
            # fallback: timestamp 로 유니크 키 생성 (로컬 추적용)
            odno = f"local-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        return str(odno)
