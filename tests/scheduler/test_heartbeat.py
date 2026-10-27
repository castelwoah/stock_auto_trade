"""heartbeat GapMonitor — 간격 감지 · 같은 날만 · 전송 실패 재시도 · 기록 실패 격리."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from src.scheduler.heartbeat import Gap, GapMonitor


def _mon(times, *, notify=None, record=None):
    it = iter(times)
    notify = notify or MagicMock(return_value=True)
    record = record or MagicMock()
    return GapMonitor(notify=notify, record=record, now_provider=lambda: next(it)), notify, record


def _t(h, m):
    return datetime(2026, 10, 27, h, m)


def test_regular_beats_no_gap():
    mon, notify, record = _mon([_t(7, 0), _t(7, 1), _t(7, 2)])
    assert [mon.beat() for _ in range(3)] == [None, None, None]
    notify.assert_not_called() and record.assert_not_called()


def test_gap_detected_recorded_and_notified():
    mon, notify, record = _mon([_t(7, 15), _t(8, 32)])
    mon.beat()
    gap = mon.beat()
    assert gap == Gap(_t(7, 15), _t(8, 32)) and gap.minutes == 77
    assert gap.label() == "07:15~08:32 (1h17m)"
    record.assert_called_once_with(gap)
    assert "⏸ 실행 중단 감지 07:15~08:32 (1h17m) · 복구됨" in notify.call_args.args[0]


def test_short_gap_label_minutes():
    assert Gap(_t(9, 0), _t(9, 12)).label() == "09:00~09:12 (12분)"


def test_below_threshold_and_new_day_ignored():
    mon, notify, _ = _mon([_t(9, 0), _t(9, 9), datetime(2026, 10, 28, 7, 0)])
    mon.beat(), mon.beat()
    assert mon.beat() is None                    # 날짜가 바뀜 (데몬 재기동 등)
    notify.assert_not_called()


def test_failed_notice_retried_on_next_beat():
    notify = MagicMock(side_effect=[False, True])
    mon, _, _ = _mon([_t(8, 0), _t(9, 0), _t(9, 1)], notify=notify)
    mon.beat(), mon.beat()                       # 깨어난 직후 네트워크 없음 → 실패
    mon.beat()                                   # 다음 beat 에 재전송
    assert notify.call_count == 2
    assert notify.call_args_list[0] == notify.call_args_list[1]


def test_record_and_notify_exceptions_isolated():
    record = MagicMock(side_effect=RuntimeError("db"))
    notify = MagicMock(side_effect=RuntimeError("discord"))
    mon, _, _ = _mon([_t(8, 0), _t(9, 0)], notify=notify, record=record)
    mon.beat()
    assert mon.beat() is not None


def test_pending_capped():
    notify = MagicMock(return_value=False)
    times = [_t(8, 0) + timedelta(minutes=11 * i) for i in range(10)]
    mon, _, _ = _mon(times, notify=notify)
    for _ in times:
        mon.beat()
    assert len(mon._pending) <= 5
