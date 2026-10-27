"""실행 중단 감지 — 1분 heartbeat 간격이 벌어지면 알림 + 기록.

노트북 절전·최대 절전, 네트워크 단절 등으로 데몬이 멈췄다 깨어나면 APScheduler 는
늦은 job 을 건너뛸 뿐 흔적을 남기지 않는다. heartbeat job 이 직전 실행 시각과 비교해
`threshold` 이상 벌어졌으면 Discord 로 알리고 기록한다 (B1·B2 지표에서 이 구간을 뺄 근거).

- 같은 날짜 안에서만 비교 (데몬 재기동 직후 첫 beat 는 기준점만 잡는다)
- 깨어난 직후에는 네트워크가 아직 없을 수 있어, 전송 실패한 알림은 다음 beat 에 재시도
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from src.common.clock import now_kst


logger = logging.getLogger(__name__)

GAP_THRESHOLD = timedelta(minutes=10)
MAX_PENDING = 5


@dataclass(frozen=True)
class Gap:
    start: datetime      # 마지막 정상 beat
    end: datetime        # 깨어난 뒤 첫 beat

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def label(self) -> str:
        h, m = divmod(self.minutes, 60)
        dur = f"{h}h{m:02d}m" if h else f"{m}분"
        return f"{self.start:%H:%M}~{self.end:%H:%M} ({dur})"


class GapMonitor:
    def __init__(
        self,
        *,
        notify: Callable[[str], bool],
        record: Callable[[Gap], None],
        threshold: timedelta = GAP_THRESHOLD,
        now_provider: Callable[[], datetime] = now_kst,
    ) -> None:
        self._notify = notify
        self._record = record
        self.threshold = threshold
        self._now = now_provider
        self._last: datetime | None = None
        self._pending: list[str] = []

    def beat(self) -> Gap | None:
        now = self._now()
        last, self._last = self._last, now
        self._flush()
        if last is None or last.date() != now.date() or now - last < self.threshold:
            return None
        gap = Gap(last, now)
        logger.warning("runtime gap detected: %s", gap.label())
        try:
            self._record(gap)
        except Exception:
            logger.exception("failed to record runtime gap")
        self._send(f"⏸ 실행 중단 감지 {gap.label()} · 복구됨 "
                   f"(절전·네트워크 단절 등 — 이 구간 매매·수집 없음)")
        return gap

    def _send(self, msg: str) -> None:
        try:
            ok = self._notify(msg)
        except Exception:
            logger.exception("gap notice failed")
            ok = False
        if not ok:
            self._pending = (self._pending + [msg])[-MAX_PENDING:]

    def _flush(self) -> None:
        pending, self._pending = self._pending, []
        for msg in pending:
            self._send(msg)
