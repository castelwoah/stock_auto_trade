"""Intraday event notifications — 체결·리스크 이벤트를 즉시 Discord 로 전송.

Embed 색상은 이벤트 종류와 손익 부호로 자동 분기.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from src.clients.discord import DiscordNotifier
from src.reporting.daily_report import (
    COLOR_LOSS,
    COLOR_NEUTRAL,
    COLOR_PROFIT,
    COLOR_WARNING,
)
from src.reporting.models import TradeEntry
from src.common.clock import now_kst


def _iso_now() -> str:
    return now_kst().isoformat(timespec="seconds")


def render_buy_embed(entry: TradeEntry, *, strategy_id: str = "") -> dict[str, Any]:
    total = entry.qty * entry.price
    desc = (
        f"**매수 체결** — {entry.name or entry.symbol} ({entry.symbol})\n"
        f"수량 {entry.qty:,}주 × ₩{entry.price:,.0f} = ₩{total:,.0f}"
    )
    if entry.reason:
        desc += f"\n근거: {entry.reason}"
    if strategy_id:
        desc += f"\n전략: `{strategy_id}`"
    return {
        "title": "🟢 BUY",
        "description": desc,
        "color": COLOR_PROFIT,
        "timestamp": _iso_now(),
    }


def render_sell_embed(entry: TradeEntry, *, strategy_id: str = "") -> dict[str, Any]:
    color = COLOR_PROFIT if (entry.pnl_pct or 0) > 0 else (
        COLOR_LOSS if (entry.pnl_pct or 0) < 0 else COLOR_NEUTRAL
    )
    total = entry.qty * entry.price
    pnl_txt = ""
    if entry.pnl_pct is not None:
        pnl_txt = f" | 손익 {entry.pnl_pct:+.2f}%"
        if entry.pnl_krw is not None:
            sign = "+" if entry.pnl_krw >= 0 else "-"
            pnl_txt += f" ({sign}₩{abs(entry.pnl_krw):,.0f})"
    desc = (
        f"**매도 체결** — {entry.name or entry.symbol} ({entry.symbol})\n"
        f"수량 {entry.qty:,}주 × ₩{entry.price:,.0f} = ₩{total:,.0f}{pnl_txt}"
    )
    if entry.reason:
        desc += f"\n사유: {entry.reason}"
    if strategy_id:
        desc += f"\n전략: `{strategy_id}`"
    return {
        "title": "🔴 SELL" if (entry.pnl_pct or 0) < 0 else "🔵 SELL",
        "description": desc,
        "color": color,
        "timestamp": _iso_now(),
    }


def render_risk_embed(
    *,
    event_type: str,      # RiskBlocked.code 또는 자유 태그
    symbol: str | None,
    detail: str,
    action_taken: str = "",
) -> dict[str, Any]:
    title = f"⚠️ RISK — {event_type}"
    desc = f"**{symbol or '시장 전체'}** · {detail}"
    if action_taken:
        desc += f"\n조치: {action_taken}"
    return {
        "title": title,
        "description": desc,
        "color": COLOR_WARNING,
        "timestamp": _iso_now(),
    }


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------

def notify_buy(
    notifier: DiscordNotifier, entry: TradeEntry, *, strategy_id: str = ""
) -> bool:
    return notifier.send_embed(render_buy_embed(entry, strategy_id=strategy_id))


def notify_sell(
    notifier: DiscordNotifier, entry: TradeEntry, *, strategy_id: str = ""
) -> bool:
    return notifier.send_embed(render_sell_embed(entry, strategy_id=strategy_id))


def notify_risk_event(
    notifier: DiscordNotifier,
    *,
    event_type: str,
    symbol: str | None,
    detail: str,
    action_taken: str = "",
) -> bool:
    return notifier.send_embed(
        render_risk_embed(
            event_type=event_type,
            symbol=symbol,
            detail=detail,
            action_taken=action_taken,
        )
    )
