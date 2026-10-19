"""Order lifecycle manager — 지정가 + 3분 미체결 시 취소 확인 후 잔량 재주문.

rules §6 Step3:
  - 진입가: 지정가 (슬리피지 제어)
  - 3분 내 미체결 → 1호가 조정 재주문
  - 매수는 2회 재시도 후 취소 + 로그

v4 (2026-10, 실전 직행 대비):
  - KIS 거절(rt_cd != '0')은 rejected, 네트워크 예외는 unknown (접수됐을 수 있음)
  - D8 송신 게이트: Trade 선기록이 신규 idempotency_key 일 때만 KIS 송신
    → 재시작 후 같은 신호가 재생성돼도 중복 주문이 나가지 않는다
  - 매 poll 마다 일별주문체결조회로 체결(부분 포함)을 Position 에 반영.
    체결조회 실패 시 그 poll 의 취소·재주문 판단을 건너뛴다 (fail-safe)
  - 재주문은 **취소 성공 + 체결 재조회 후 잔량만**. 취소 실패 시 재주문 금지 (이중 체결 방지)
  - 가격은 KRX 호가단위. 매수 1호가 상향(MAX_RETRIES 회), 매도 1호가 하향 무제한
    (손절이 미체결로 남지 않도록 — 결정 2026-10-19). 매도 첫 지정가는 현재가 −1%
  - 모든 KIS 송신은 `@risk_gate` 경유 (D14). SHADOW 모드는 gate 통과 후 송신만 생략
  - 15:00 이후 매수 재주문 금지, 15:15 미체결 매수는 실제 취소 TR 호출

**서브에이전트 금지 영역** — 주문 생명주기는 체결 순서·idempotency·재시도 규약이
강하게 결합되어 단일 논리 흐름 유지.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Callable

import requests

from src.common.clock import now_kst
from src.clients.discord import DiscordNotifier
from src.clients.kis import KisClient, KisOrderRejected, OrderAck, OrderFill
from src.execution.krx_tick import round_to_tick, shift_ticks
from src.execution.portfolio import LivePortfolio, TradeRecord
from src.persistence.models import Trade
from src.reporting.intraday import notify_risk_event
from src.risk.exceptions import RiskBlocked
from src.risk.limits import BuyContext, risk_gate
from src.strategy.base import Action, Signal


logger = logging.getLogger(__name__)


# Operational constants (rules §5.1, §6 Step3)
MAX_RETRIES = 2                # 매수 재주문 상한 (매도는 무제한)
RESUBMIT_AFTER_SECONDS = 180   # 3 min
ENTRY_CUTOFF = time(15, 0)
EOD_CANCEL = time(15, 15)
SELL_INITIAL_DISCOUNT = 0.01   # 매도 첫 지정가 = 현재가 × (1 − 1%) → 즉시 체결 지향
MAX_SELL_REATTEMPTS = 5        # 매도가 거절·차단으로 끝난 뒤 같은 신호의 재시도 상한 (일)

# Trade.status 값 상수 — v3 시도 로그 정규화
STATUS_SUBMITTED = "submitted"
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED_TIMEOUT = "cancelled_timeout"     # 매수 재시도 한계 초과
STATUS_CANCELLED_EOD = "cancelled_eod"             # 15:15 일괄 취소
STATUS_CANCELLED_MANUAL = "cancelled_manual"       # 외부(수동) 취소
STATUS_CANCELLED_RESUBMIT = "cancelled_resubmit"   # 잔량 재주문으로 대체됨
STATUS_UNKNOWN = "unknown"                         # 송신 결과 불명 (네트워크 예외)
STATUS_BLOCKED = "blocked"                         # 송신 직전 risk_gate 차단
STATUS_SHADOW = "shadow"                           # 섀도 모드 — 송신 안 함


def initial_price(action: Action, ref_price: float) -> int:
    """첫 지정가: 매수는 현재가 올림 정렬, 매도는 −1% 후 내림 정렬."""
    if action == Action.SELL:
        return round_to_tick(ref_price * (1.0 - SELL_INITIAL_DISCOUNT), up=False)
    return round_to_tick(ref_price, up=True)


def reprice(action: Action, *, prev_price: float, ref_price: float) -> int:
    """재주문가: 매수는 max(직전, 현재)+1호가, 매도는 min(직전, 현재)−1호가."""
    if action == Action.SELL:
        return shift_ticks(min(prev_price, ref_price), -1)
    return shift_ticks(max(prev_price, ref_price), +1)


@dataclass
class PendingOrder:
    order_id: str
    signal: Signal
    submitted_at: datetime
    price: float
    qty: int
    orgno: str = ""
    retries: int = 0
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    cancelled: bool = False
    filled: bool = False
    unknown: bool = False
    cancel_requested: bool = False
    cancel_fail_notified: bool = False
    idempotency_key: str = ""
    buy_context: BuyContext | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.qty - self.filled_qty)

    @property
    def active(self) -> bool:
        return not (self.filled or self.cancelled)


class OrderManager:
    """Live order state tracker + 체결 동기화 · 취소 · 재주문 오케스트레이터."""

    def __init__(
        self,
        *,
        kis_client: KisClient,
        portfolio: LivePortfolio,
        notifier: DiscordNotifier | None = None,
        now_provider: Callable[[], datetime] = now_kst,
        max_retries: int = MAX_RETRIES,
        resubmit_after_seconds: int = RESUBMIT_AFTER_SECONDS,
        shadow: bool = False,
    ) -> None:
        self.kis = kis_client
        self.portfolio = portfolio
        self.notifier = notifier
        self._now = now_provider
        self.max_retries = max_retries
        self.resubmit_after_seconds = resubmit_after_seconds
        self.shadow = shadow
        self.pending: dict[str, PendingOrder] = {}
        self._tracked_odnos: set[str] = set()
        self._shadow_seq = 0

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
        buy_context: BuyContext | None = None,
    ) -> PendingOrder | None:
        """지정가 주문. limit_price 는 기준가(현재가) — 호가 정렬·매도 할인은 여기서 적용."""
        now = self._now()
        if signal.action == Action.BUY and now.time() >= ENTRY_CUTOFF:
            logger.info("BUY after 15:00 cut-off ignored: %s", signal.symbol)
            return None
        if qty <= 0 or limit_price <= 0:
            return None
        price = initial_price(signal.action, limit_price)

        # D8 송신 게이트 — 기존 키면 송신하지 않는다
        trade = self.portfolio.insert_trade_if_new(TradeRecord(
            symbol=signal.symbol,
            side=signal.action.value,
            qty=qty,
            price=price,
            strategy_id=signal.strategy_id,
            strategy_ver=strategy_ver,
            signal_id=signal.signal_id,
            idempotency_key=idempotency_key,
            order_id=None,
            order_dvsn="00",
            status=STATUS_SHADOW if self.shadow else STATUS_SUBMITTED,
            note=signal.reason,
        ))
        if trade is None and signal.action == Action.SELL:
            # 청산은 거절·차단으로 끝났어도 다시 시도해야 한다 (손절이 하루 종일 막히지 않도록).
            # 재시도 가격은 할인 없이 현재가 내림 정렬 — 하한가 근처 −1% 거절을 피한다.
            retry_key = self._sell_reattempt_key(idempotency_key)
            if retry_key is not None:
                idempotency_key = retry_key
                price = round_to_tick(limit_price, up=False)
                trade = self.portfolio.insert_trade_if_new(TradeRecord(
                    symbol=signal.symbol, side=signal.action.value, qty=qty, price=price,
                    strategy_id=signal.strategy_id, strategy_ver=strategy_ver,
                    signal_id=signal.signal_id, idempotency_key=retry_key, order_id=None,
                    status=STATUS_SHADOW if self.shadow else STATUS_SUBMITTED,
                    note=f"re-attempt after rejected/blocked: {signal.reason}",
                ))
        if trade is None:
            logger.warning("duplicate idempotency_key — order NOT sent: %s", idempotency_key)
            return None
        return self._send(
            signal, trade=trade, price=price, qty=qty,
            idempotency_key=idempotency_key, retries=0, buy_context=buy_context,
        )

    def _sell_reattempt_key(self, base_key: str) -> str | None:
        """직전 시도가 rejected/blocked 로 끝났을 때만 다음 시도 키(`#aN`)를 준다."""
        prev = base_key
        for n in range(1, MAX_SELL_REATTEMPTS + 1):
            t = self.portfolio.get_trade(prev)
            if t is None or t.status not in (STATUS_REJECTED, STATUS_BLOCKED):
                return None
            cand = f"{base_key}#a{n}"
            if self.portfolio.get_trade(cand) is None:
                return cand
            prev = cand
        logger.error("sell re-attempts exhausted for %s", base_key)
        return None

    def _send(
        self,
        signal: Signal,
        *,
        trade: Trade,
        price: int,
        qty: int,
        idempotency_key: str,
        retries: int,
        buy_context: BuyContext | None,
    ) -> PendingOrder | None:
        now = self._now()
        try:
            ack = self._place(
                symbol=signal.symbol, side=signal.action.value,
                qty=qty, price=price, buy_context=buy_context,
            )
        except RiskBlocked as rb:
            logger.warning("risk_gate blocked at send %s: %s", signal.symbol, rb)
            self._set_trade(trade, status=STATUS_BLOCKED, note_add=f"blocked:{rb.code}")
            return None
        except KisOrderRejected as rej:
            logger.warning("KIS rejected %s: %s", signal.symbol, rej)
            self._set_trade(trade, status=STATUS_REJECTED, note_add=f"rejected:{rej.msg_cd} {rej.msg1}")
            return None
        except ValueError:
            logger.exception("order parameters invalid: %s", signal.symbol)
            self._set_trade(trade, status=STATUS_REJECTED, note_add="invalid_params")
            return None
        except requests.RequestException:
            # 타임아웃 등 — 접수됐을 수 있다. 체결조회로 대응 주문을 찾을 때까지 추적.
            # (그 외 예외 — 예: risk_gate 엔진 미설치 — 는 fail-loud 로 전파)
            logger.exception("order send outcome unknown: %s", signal.symbol)
            self._set_trade(trade, status=STATUS_UNKNOWN)
            po = PendingOrder(
                order_id=f"unknown:{idempotency_key}", signal=signal,
                submitted_at=now, price=price, qty=qty, retries=retries,
                unknown=True, idempotency_key=idempotency_key, buy_context=buy_context,
            )
            self.pending[po.order_id] = po
            return None

        po = PendingOrder(
            order_id=ack.odno, signal=signal, submitted_at=now, price=price, qty=qty,
            orgno=ack.orgno, retries=retries,
            idempotency_key=idempotency_key, buy_context=buy_context,
        )
        if self.shadow:
            return po   # 송신 안 함 — 생명주기 추적 대상 아님
        trade.order_id = ack.odno
        self.portfolio.session.flush()
        self.pending[ack.odno] = po
        self._tracked_odnos.add(ack.odno)
        return po

    @risk_gate
    def _place(
        self,
        *,
        symbol: str,
        side: str,
        qty: int,
        price: int,
        buy_context: BuyContext | None = None,
    ) -> OrderAck:
        """D14: 모든 KIS 송신의 단일 지점. 엔진 미설치 시 risk_gate 가 RuntimeError."""
        if self.shadow:
            self._shadow_seq += 1
            return OrderAck(odno=f"shadow-{self._shadow_seq}", orgno="")
        return self.kis.order(
            code=symbol, qty=qty, side=side, ord_dvsn="00", unit_price=str(int(price)),
        )

    # ------------------------------------------------------------------
    # Lifecycle ticks — call periodically (e.g., every bar)
    # ------------------------------------------------------------------
    def poll(self, *, current_prices: dict[str, float]) -> None:
        """체결 동기화 → EOD 취소 · 타임아웃 취소/재주문."""
        if not any(po.active for po in self.pending.values()):
            self._prune()
            return
        now = self._now()
        if not self.sync_fills():
            logger.warning("fill sync failed — skip cancel/resubmit this poll")
            return
        for po in list(self.pending.values()):
            if not po.active or po.unknown:
                continue
            is_buy = po.signal.action == Action.BUY
            if is_buy and now.time() >= EOD_CANCEL:
                self._cancel_and_close(po, STATUS_CANCELLED_EOD)
                continue
            age = (now - po.submitted_at).total_seconds()
            if age < self.resubmit_after_seconds:
                continue
            if is_buy and (
                po.retries >= self.max_retries
                or now.time() >= ENTRY_CUTOFF
                or po.buy_context is None   # 재시작 복원 주문 — gate 입력이 없으므로 재주문 안 함
            ):
                self._cancel_and_close(po, STATUS_CANCELLED_TIMEOUT)
                continue
            ref = current_prices.get(po.signal.symbol, po.price)
            self._cancel_and_resubmit(po, ref_price=ref)
        self._prune()

    def pending_buy_notional(self) -> dict[str, float]:
        """활성 매수 주문의 미체결 잔량 × 지정가 (symbol 별). 체결분은 Position 으로 잡힌다."""
        out: dict[str, float] = {}
        for po in self.pending.values():
            if po.active and po.signal.action == Action.BUY:
                sym = po.signal.symbol
                out[sym] = out.get(sym, 0.0) + po.remaining * po.price
        return out

    def sync_fills(self) -> bool:
        """일별주문체결조회로 활성 주문의 체결 진행을 반영. 조회 실패 시 False."""
        live = [po for po in self.pending.values() if po.active]
        if not live:
            return True
        try:
            fills = self.kis.inquire_today_orders(self._now().strftime("%Y%m%d"))
        except Exception:
            logger.exception("inquire_today_orders failed")
            return False
        by_odno = {f.odno: f for f in fills}
        for po in live:
            f = by_odno.get(po.order_id)
            if f is None and po.unknown:
                f = self._adopt_unknown(po, fills)
            if f is not None:
                if not po.orgno and f.orgno:
                    po.orgno = f.orgno   # 재시작 복원 주문: 취소에 필요한 조직번호 보충
                self._apply_progress(po, f)
        return True

    def restore_open_orders(self, trades: list[Trade]) -> int:
        """재시작 복원: 당일 submitted/unknown Trade 를 pending 으로 다시 추적.

        orgno 는 다음 sync_fills 에서 체결조회 결과로 보충한다. 복원된 매수는
        buy_context 가 없어 재주문하지 않고 타임아웃 시 취소만 한다.
        """
        n = 0
        for t in trades:
            if t.status not in (STATUS_SUBMITTED, STATUS_UNKNOWN):
                continue
            key = t.idempotency_key
            sig = Signal(
                action=Action(t.side), symbol=t.symbol, strategy_id=t.strategy_id,
                qty=t.qty, signal_id=t.signal_id, reason="restored",
            )
            retries = int(key.rsplit("#r", 1)[1]) if "#r" in key else 0
            oid = t.order_id or f"unknown:{key}"
            po = PendingOrder(
                order_id=oid, signal=sig, submitted_at=t.ts, price=t.price, qty=t.qty,
                retries=retries, unknown=t.order_id is None, idempotency_key=key,
            )
            self.pending[oid] = po
            if t.order_id:
                self._tracked_odnos.add(t.order_id)
            n += 1
        return n

    def _adopt_unknown(self, po: PendingOrder, fills: list[OrderFill]) -> OrderFill | None:
        """응답 유실 주문을 체결조회에서 찾아 연결 (종목·방향·수량 일치, 미추적 주문번호)."""
        for f in fills:
            if (
                f.odno and f.odno not in self._tracked_odnos
                and f.symbol == po.signal.symbol
                and f.side == po.signal.action.value
                and f.ord_qty == po.qty
            ):
                self.pending.pop(po.order_id, None)
                po.order_id, po.orgno, po.unknown = f.odno, f.orgno, False
                self.pending[f.odno] = po
                self._tracked_odnos.add(f.odno)
                trade = self.portfolio.get_trade(po.idempotency_key)
                if trade is not None:
                    self._set_trade(trade, status=STATUS_SUBMITTED, order_id=f.odno)
                logger.info("unknown order adopted: %s → %s", po.idempotency_key, f.odno)
                return f
        return None

    def _apply_progress(self, po: PendingOrder, f: OrderFill) -> None:
        delta = f.filled_qty - po.filled_qty
        if delta > 0:
            # 누적 평균가로부터 이번 증분의 체결가 역산
            prev_amt = po.avg_fill_price * po.filled_qty
            px = (f.avg_price * f.filled_qty - prev_amt) / delta if f.avg_price > 0 else po.price
            self.portfolio.apply_fill_to_position(
                symbol=po.signal.symbol,
                strategy_id=po.signal.strategy_id,
                side=po.signal.action.value,
                qty=delta,
                price=px,
                ts=self._now(),
            )
            po.filled_qty = f.filled_qty
            po.avg_fill_price = f.avg_price if f.avg_price > 0 else px
        if po.filled_qty >= po.qty:
            po.filled = True
            self._close_trade(po, STATUS_FILLED)
        elif f.cancelled and not po.cancel_requested:
            po.cancelled = True
            self._close_trade(po, STATUS_CANCELLED_MANUAL)

    # ------------------------------------------------------------------
    # External confirmation (수동 경로 — 테스트·운영자 개입)
    # ------------------------------------------------------------------
    def mark_filled(self, order_id: str, *, fill_price: float, ts: datetime | None = None) -> None:
        po = self.pending.get(order_id)
        if po is None or not po.active:
            return
        qty = po.remaining
        self.portfolio.apply_fill_to_position(
            symbol=po.signal.symbol,
            strategy_id=po.signal.strategy_id,
            side=po.signal.action.value,
            qty=qty,
            price=fill_price,
            ts=ts or self._now(),
        )
        total = po.avg_fill_price * po.filled_qty + fill_price * qty
        po.filled_qty += qty
        po.avg_fill_price = total / po.filled_qty
        po.filled = True
        self._close_trade(po, STATUS_FILLED)

    def mark_cancelled(self, order_id: str, *, status: str = STATUS_CANCELLED_MANUAL) -> None:
        po = self.pending.get(order_id)
        if po is not None and po.active:
            po.cancelled = True
            self._close_trade(po, status)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _cancel(self, po: PendingOrder) -> bool:
        po.cancel_requested = True
        try:
            self.kis.cancel_order(po.order_id, po.orgno)
            return True
        except Exception as exc:
            logger.exception("cancel failed for %s %s", po.signal.symbol, po.order_id)
            if self.notifier is not None and not po.cancel_fail_notified:
                po.cancel_fail_notified = True
                notify_risk_event(
                    self.notifier,
                    event_type="order_cancel_failed",
                    symbol=po.signal.symbol,
                    detail=f"{po.signal.action.value} {po.order_id}: {exc} — 재주문 보류",
                )
            return False

    def _cancel_and_close(self, po: PendingOrder, status: str) -> None:
        if not self._cancel(po):
            return  # 다음 poll 에서 재시도
        self.sync_fills()  # 취소 직전 체결분 반영
        if po.active:
            po.cancelled = True
            self._close_trade(po, status)
        logger.info("order cancelled: %s %s → %s", po.signal.symbol, po.order_id, status)

    def _cancel_and_resubmit(self, po: PendingOrder, *, ref_price: float) -> None:
        if not self._cancel(po):
            return  # 취소 미확인 상태에서 재주문하면 이중 체결 위험
        self.sync_fills()
        if not po.active:
            return  # 취소 직전 전량 체결
        po.cancelled = True
        self._close_trade(po, STATUS_CANCELLED_RESUBMIT)
        remaining = po.remaining
        if remaining <= 0:
            return
        action = po.signal.action
        new_price = reprice(action, prev_price=po.price, ref_price=ref_price)
        retry_no = po.retries + 1
        retry_key = f"{po.idempotency_key.split('#r')[0]}#r{retry_no}"
        trade = self.portfolio.insert_trade_if_new(TradeRecord(
            symbol=po.signal.symbol,
            side=action.value,
            qty=remaining,
            price=new_price,
            strategy_id=po.signal.strategy_id,
            strategy_ver="retry",
            signal_id=po.signal.signal_id,
            idempotency_key=retry_key,
            order_id=None,
            status=STATUS_SUBMITTED,
            note=f"retry {retry_no} of {po.idempotency_key}",
        ))
        if trade is None:
            return
        ctx = po.buy_context
        if action == Action.BUY and ctx is not None:
            ctx = replace(ctx, intended_notional=remaining * new_price)
        new_po = self._send(
            po.signal, trade=trade, price=new_price, qty=remaining,
            idempotency_key=retry_key, retries=retry_no, buy_context=ctx,
        )
        if new_po is not None and self.notifier is not None:
            notify_risk_event(
                self.notifier,
                event_type="order_resubmit",
                symbol=po.signal.symbol,
                detail=f"{action.value} retry {retry_no} qty={remaining} at {new_price}",
            )

    def _close_trade(self, po: PendingOrder, status: str) -> None:
        """주문 종료 시 Trade 확정. 체결분이 있으면 체결 수량·평균가로 'filled' 기록
        (리포트 FIFO 손익은 status='filled' 행의 qty·price 를 사용)."""
        trade = self.portfolio.get_trade(po.idempotency_key)
        if trade is None:
            return
        if po.filled_qty > 0:
            note = None
            if po.filled_qty < po.qty:
                note = f"partial {po.filled_qty}/{po.qty}, rest {status}"
            self._set_trade(
                trade, status=STATUS_FILLED, qty=po.filled_qty,
                price=po.avg_fill_price or po.price, note_add=note,
            )
        else:
            self._set_trade(trade, status=status)

    def _set_trade(
        self,
        trade: Trade,
        *,
        status: str | None = None,
        qty: int | None = None,
        price: float | None = None,
        order_id: str | None = None,
        note_add: str | None = None,
    ) -> None:
        try:
            if status is not None:
                trade.status = status
            if qty is not None:
                trade.qty = qty
            if price is not None:
                trade.price = price
            if order_id is not None:
                trade.order_id = order_id
            if note_add:
                trade.note = f"{trade.note} | {note_add}" if trade.note else note_add
            self.portfolio.session.flush()
        except Exception:
            logger.exception("failed to update Trade %s", trade.idempotency_key)

    def _prune(self) -> None:
        for oid in [k for k, po in self.pending.items() if not po.active]:
            self.pending.pop(oid, None)
