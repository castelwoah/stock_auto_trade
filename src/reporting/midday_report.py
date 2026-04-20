"""Midday (12:00 KST) snapshot report — 장중 중간 점검 알림.

v3 범위:
  - 오전(09:10~12:00) 체결·실패·차단 요약
  - 현재 포지션 미실현 P&L
  - 오후 후보 종목 top-N
  - 이상 신호 플래그 (Daily Loss 사용률·TP/SL 임박·CB 활성)

rules §7 의 일일 리포트(16:00)를 보완하는 장중 스냅샷. 정본(종가 기준)이 아님.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

from src.clients.discord import DiscordNotifier
from src.reporting.daily_report import (
    COLOR_LOSS,
    COLOR_NEUTRAL,
    COLOR_PROFIT,
    COLOR_WARNING,
    _fmt_krw,
    _fmt_pct,
    _fmt_price,
)
from src.reporting.models import (
    MidDayAnomaly,
    MidDaySnapshot,
)


# v3 방어 윈도우: 11:55 ~ 12:15 외 호출은 skip (재부팅 후 missed trigger 오작동 차단)
WINDOW_START = time(11, 55)
WINDOW_END = time(12, 15)


def in_valid_window(ts: datetime) -> bool:
    return WINDOW_START <= ts.time() <= WINDOW_END


# ---------------------------------------------------------------------------
# Anomaly 탐지
# ---------------------------------------------------------------------------

def detect_anomalies(snap: MidDaySnapshot) -> list[MidDayAnomaly]:
    out: list[MidDayAnomaly] = []
    r = snap.risk
    if r.circuit_breaker_active:
        out.append(MidDayAnomaly(
            severity="critical", code="circuit_breaker_active",
            message=f"Circuit Breaker: {r.circuit_breaker_reason or 'active'}",
        ))
    if r.daily_loss_usage_pct >= 90.0:
        out.append(MidDayAnomaly(
            severity="critical", code="daily_loss_near_limit",
            message=f"Daily Loss 사용 {r.daily_loss_usage_pct:.0f}%",
        ))
    elif r.daily_loss_usage_pct >= 70.0:
        out.append(MidDayAnomaly(
            severity="warn", code="daily_loss_near_limit",
            message=f"Daily Loss 사용 {r.daily_loss_usage_pct:.0f}%",
        ))
    for p in snap.positions:
        if p.unrealized_pnl_pct <= -4.0:
            out.append(MidDayAnomaly(
                severity="warn", code="position_near_sl",
                message=f"{p.symbol} {p.unrealized_pnl_pct:+.1f}% (SL -5% 임박)",
            ))
        elif p.unrealized_pnl_pct >= 6.0 and not p.tp_armed:
            out.append(MidDayAnomaly(
                severity="info", code="position_near_tp",
                message=f"{p.symbol} {p.unrealized_pnl_pct:+.1f}% (TP +7% 임박)",
            ))
    if snap.market.kospi_change_pct is not None and snap.market.kospi_change_pct <= -2.0:
        out.append(MidDayAnomaly(
            severity="warn", code="kospi_down_2pct",
            message=f"KOSPI {snap.market.kospi_change_pct:+.2f}%",
        ))
    total_morning_activity = (
        len(snap.morning.new_buys) + len(snap.morning.take_profits) + len(snap.morning.stop_losses)
    )
    if total_morning_activity == 0:
        out.append(MidDayAnomaly(
            severity="info", code="no_morning_activity",
            message="오전 체결 0건",
        ))
    return out


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _severity_emoji(sev: str) -> str:
    return {"critical": "🔴", "warn": "🟠", "info": "🔵"}.get(sev, "·")


def _pick_color(snap: MidDaySnapshot) -> int:
    if any(a.severity == "critical" for a in snap.anomalies):
        return COLOR_WARNING
    if snap.unrealized_pnl_pct >= 1.0:
        return COLOR_PROFIT
    if snap.unrealized_pnl_pct <= -1.0:
        return COLOR_LOSS
    return COLOR_NEUTRAL


def render_midday_text(snap: MidDaySnapshot) -> str:
    lines: list[str] = []
    lines.append(f"[{snap.ts.strftime('%Y-%m-%d %H:%M')} 미드데이 스냅샷]")
    lines.append("=" * 36)

    # 이상 신호 (상단)
    if snap.anomalies:
        lines.append("■ 이상 신호")
        for a in snap.anomalies:
            lines.append(f"  {_severity_emoji(a.severity)} [{a.code}] {a.message}")
        lines.append("")

    # 시장
    m = snap.market
    lines.append("■ 시장 현황 (오전)")
    lines.append(f"  - KOSPI: {_fmt_price(m.kospi)} ({_fmt_pct(m.kospi_change_pct)})")
    lines.append(f"  - KOSDAQ: {_fmt_price(m.kosdaq)} ({_fmt_pct(m.kosdaq_change_pct)})")
    lines.append("")

    # 오전 체결
    mo = snap.morning
    lines.append("■ 오전 매매 (09:10 ~ 12:00)")
    lines.append(f"  - 신규 매수: {len(mo.new_buys)}종목")
    lines.append(f"  - 익절 청산: {len(mo.take_profits)}종목 / {_fmt_krw(mo.tp_total_krw)}")
    lines.append(f"  - 손절 청산: {len(mo.stop_losses)}종목 / {_fmt_krw(mo.sl_total_krw)}")
    if mo.rejections or mo.cancellations or mo.risk_blocks:
        lines.append("  [실패·차단]")
        if mo.rejections:
            lines.append(f"  - 거절(KIS): {len(mo.rejections)}건")
        if mo.cancellations:
            lines.append(f"  - 취소: {len(mo.cancellations)}건")
        if mo.risk_blocks:
            parts = [f"{k} {v}" for k, v in sorted(mo.risk_block_breakdown.items())]
            lines.append(f"  - 리스크 차단: {len(mo.risk_blocks)}건 ({', '.join(parts)})")
    lines.append("")

    # 현재 포지션
    lines.append("■ 현재 포지션 (미실현 P&L)")
    if not snap.positions:
        lines.append("  (보유 없음)")
    else:
        for p in snap.positions:
            armed = " [TP armed]" if p.tp_armed else ""
            lines.append(
                f"  - {p.name or p.symbol} ({p.symbol}): "
                f"{_fmt_pct(p.unrealized_pnl_pct)} ({_fmt_krw(p.unrealized_pnl_krw)}){armed}"
            )
    lines.append("")

    # 종합
    lines.append("■ 종합")
    lines.append(f"  - 평가액: {_fmt_krw(snap.total_equity).lstrip('+')}")
    lines.append(f"  - 미실현 손익: {_fmt_pct(snap.unrealized_pnl_pct)}")
    lines.append(f"  - 오전 실현: {_fmt_krw(mo.realized_pnl_krw)}")
    lines.append("")

    # 리스크
    r = snap.risk
    cb = f"발동 ({r.circuit_breaker_reason})" if r.circuit_breaker_active else "정상"
    lines.append("■ 리스크 상태")
    lines.append(f"  - Daily Loss 사용: {r.daily_loss_usage_pct:.0f}%")
    lines.append(f"  - Circuit Breaker: {cb}")
    lines.append("")

    # 오후 후보
    lines.append("■ 오후 후보 (스코어 순, 상위 {})".format(len(snap.top_candidates)))
    if not snap.top_candidates:
        lines.append("  (후보 없음)")
    else:
        for c in snap.top_candidates:
            extra = f" [{c.dominant_factor}]" if c.dominant_factor else ""
            lines.append(f"  - {c.symbol} {c.name}: {c.total_score:.1f}{extra}")
    lines.append("")

    # 다음 액션
    if snap.next_actions:
        lines.append("■ 다음 액션")
        for a in snap.next_actions:
            lines.append(f"  - {a}")
    lines.append("=" * 36)
    return "\n".join(lines)


def render_midday_embed(snap: MidDaySnapshot) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []

    if snap.anomalies:
        fields.append({
            "name": "⚠️ 이상 신호",
            "value": "\n".join(
                f"{_severity_emoji(a.severity)} [{a.code}] {a.message}"
                for a in snap.anomalies
            ),
            "inline": False,
        })

    m = snap.market
    fields.append({
        "name": "■ 시장 (오전)",
        "value": (
            f"KOSPI: {_fmt_price(m.kospi)} ({_fmt_pct(m.kospi_change_pct)})\n"
            f"KOSDAQ: {_fmt_price(m.kosdaq)} ({_fmt_pct(m.kosdaq_change_pct)})"
        ),
        "inline": False,
    })

    mo = snap.morning
    morning_parts = [
        f"매수 {len(mo.new_buys)}종목",
        f"익절 {len(mo.take_profits)}종목 ({_fmt_krw(mo.tp_total_krw)})",
        f"손절 {len(mo.stop_losses)}종목 ({_fmt_krw(mo.sl_total_krw)})",
    ]
    if mo.rejections or mo.cancellations or mo.risk_blocks:
        sub: list[str] = []
        if mo.rejections:
            sub.append(f"거절 {len(mo.rejections)}")
        if mo.cancellations:
            sub.append(f"취소 {len(mo.cancellations)}")
        if mo.risk_blocks:
            parts = [f"{k} {v}" for k, v in sorted(mo.risk_block_breakdown.items())]
            sub.append(f"리스크 {len(mo.risk_blocks)} ({', '.join(parts)})")
        morning_parts.append("실패·차단: " + " / ".join(sub))
    fields.append({
        "name": "■ 오전 매매",
        "value": "\n".join(morning_parts),
        "inline": False,
    })

    if snap.positions:
        pos_lines = [
            f"{p.name or p.symbol} ({p.symbol}): "
            f"{_fmt_pct(p.unrealized_pnl_pct)}" + (" [TP armed]" if p.tp_armed else "")
            for p in snap.positions[:10]
        ]
        fields.append({
            "name": "■ 포지션 (미실현)",
            "value": "\n".join(pos_lines) if pos_lines else "(없음)",
            "inline": False,
        })

    fields.append({
        "name": "■ 종합",
        "value": (
            f"평가액: {_fmt_krw(snap.total_equity).lstrip('+')}\n"
            f"미실현: {_fmt_pct(snap.unrealized_pnl_pct)}\n"
            f"오전 실현: {_fmt_krw(mo.realized_pnl_krw)}"
        ),
        "inline": False,
    })

    r = snap.risk
    cb = "발동" if r.circuit_breaker_active else "정상"
    fields.append({
        "name": "■ 리스크",
        "value": (
            f"Daily Loss 사용: {r.daily_loss_usage_pct:.0f}%\n"
            f"Circuit Breaker: {cb}"
        ),
        "inline": False,
    })

    if snap.top_candidates:
        lines_c = [
            f"{c.symbol} {c.name}: {c.total_score:.1f}"
            for c in snap.top_candidates
        ]
        fields.append({
            "name": f"■ 오후 후보 (상위 {len(snap.top_candidates)})",
            "value": "\n".join(lines_c),
            "inline": False,
        })

    return {
        "title": f"[{snap.ts.strftime('%Y-%m-%d %H:%M')}] 미드데이 스냅샷",
        "color": _pick_color(snap),
        "fields": fields,
        "footer": {"text": f"strategy_version={snap.strategy_version}"},
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def send_midday_report(
    notifier: DiscordNotifier,
    snap: MidDaySnapshot,
    *,
    mode: str = "embed",
) -> bool:
    if mode == "text":
        return notifier.send(render_midday_text(snap), include_timestamp=False)
    return notifier.send_embed(render_midday_embed(snap))
