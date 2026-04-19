"""Daily report renderer — rules §7.1 포맷.

순수 변환 함수: `DailyReport` → 문자열 / Discord embed dict.
Discord 전송은 `DiscordNotifier.send()` / `send_embed()` 에 위임.
"""
from __future__ import annotations

from typing import Any

from src.clients.discord import DiscordNotifier
from src.reporting.models import DailyReport, TradeEntry


# Embed 색상 (Discord 10진수)
COLOR_PROFIT = 0x2ECC71   # 녹색
COLOR_LOSS = 0xE74C3C     # 빨강
COLOR_NEUTRAL = 0x3498DB  # 파랑
COLOR_WARNING = 0xF39C12  # 주황


def _fmt_pct(v: float | None, *, sign: bool = True) -> str:
    if v is None:
        return "-"
    s = f"{v:+.2f}%" if sign else f"{v:.2f}%"
    return s


def _fmt_krw(v: float | None) -> str:
    if v is None:
        return "-"
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    return f"{sign}₩{abs(v):,.0f}"


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:,.1f}"


def _fmt_trade_entry(t: TradeEntry, *, with_pnl: bool) -> str:
    base = f"  - {t.name or '?'} ({t.symbol})"
    if with_pnl and t.pnl_pct is not None:
        base += f": {_fmt_pct(t.pnl_pct)}"
    if t.reason:
        kind = "반성" if (t.pnl_pct is not None and t.pnl_pct < 0) else "근거"
        base += f" | {kind}: {t.reason}"
    return base


def render_text(report: DailyReport) -> str:
    """rules §7.1 포맷을 충실히 재현한 단일 문자열."""
    lines: list[str] = []
    lines.append(f"[{report.report_date.isoformat()} 매매 리포트]")
    lines.append("=" * 33)

    # 시장 요약
    m = report.market
    lines.append("■ 시장 요약")
    lines.append(
        f"  - KOSPI: {_fmt_price(m.kospi)} ({_fmt_pct(m.kospi_change_pct)})"
    )
    lines.append(
        f"  - KOSDAQ: {_fmt_price(m.kosdaq)} ({_fmt_pct(m.kosdaq_change_pct)})"
    )
    lines.append(f"  - 환율(USD/KRW): {_fmt_price(m.usd_krw)}")
    lines.append("")

    # 데이터 수집
    d = report.data
    dist = (
        " (" + ", ".join(f"{k} {v}" for k, v in sorted(d.source_distribution.items())) + ")"
        if d.source_distribution
        else ""
    )
    lines.append("■ 데이터 수집")
    lines.append(f"  - 수집 뉴스: {d.news_count}건{dist}")
    lines.append(f"  - 분석 종목: {d.analyzed_count}개")
    lines.append(f"  - 후보 편입: {d.candidates_count}개 (스코어 70+)")
    lines.append("")

    # 매매 실행
    t = report.trades
    lines.append("■ 매매 실행")
    lines.append(
        f"  - 신규 매수: {len(t.buys)}종목 / 총 {_fmt_krw(t.buy_total_krw)}"
    )
    lines.append(
        f"  - 청산(익절): {len(t.take_profits)}종목 / {_fmt_krw(t.tp_total_krw)}"
    )
    lines.append(
        f"  - 청산(손절): {len(t.stop_losses)}종목 / {_fmt_krw(t.sl_total_krw)}"
    )
    lines.append("")

    # 포트폴리오 현황
    p = report.portfolio
    lines.append("■ 포트폴리오 현황")
    lines.append(f"  - 평가액: {_fmt_krw(p.total_value).lstrip('+')}")
    lines.append(
        f"  - 당일 손익: {_fmt_pct(p.daily_pnl_pct)} ({_fmt_krw(p.daily_pnl_krw)})"
    )
    lines.append(f"  - 누적 수익률: {_fmt_pct(p.cumulative_return_pct)}")
    lines.append(f"  - MDD: {_fmt_pct(p.mdd_pct, sign=False)}")
    lines.append("")

    # 상세 내역
    lines.append("■ 상세 내역")
    if t.buys:
        lines.append("  [매수]")
        for b in t.buys:
            lines.append(_fmt_trade_entry(b, with_pnl=False))
    if t.take_profits or t.stop_losses:
        lines.append("  [청산]")
        for s in t.take_profits + t.stop_losses:
            lines.append(_fmt_trade_entry(s, with_pnl=True))
    if not (t.buys or t.take_profits or t.stop_losses):
        lines.append("  (거래 없음)")
    lines.append("")

    # 리스크 상태
    r = report.risk
    cb_status = (
        f"발동 ({r.circuit_breaker_reason})" if r.circuit_breaker_active else "정상"
    )
    lines.append("■ 리스크 상태")
    lines.append(f"  - Daily Loss Limit: 사용 {r.daily_loss_usage_pct:.0f}%")
    lines.append(f"  - Circuit Breaker: {cb_status}")
    if r.consecutive_loss_days:
        lines.append(f"  - 연속 손실: {r.consecutive_loss_days}일")
    lines.append("")

    # 다음 액션
    lines.append("■ 다음 액션")
    if report.next_actions:
        for a in report.next_actions:
            lines.append(f"  - {a}")
    else:
        lines.append("  - (없음)")
    lines.append("=" * 33)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discord embed
# ---------------------------------------------------------------------------

def _pick_color(report: DailyReport) -> int:
    if report.risk.circuit_breaker_active:
        return COLOR_WARNING
    pnl = report.portfolio.daily_pnl_pct
    if pnl > 0:
        return COLOR_PROFIT
    if pnl < 0:
        return COLOR_LOSS
    return COLOR_NEUTRAL


def render_embed(report: DailyReport) -> dict[str, Any]:
    """Discord Embed 포맷 (단일 embed). 긴 코드블록 대신 fields 로 분할."""
    m = report.market
    d = report.data
    t = report.trades
    p = report.portfolio
    r = report.risk

    fields: list[dict[str, Any]] = []
    fields.append({
        "name": "■ 시장 요약",
        "value": (
            f"KOSPI: {_fmt_price(m.kospi)} ({_fmt_pct(m.kospi_change_pct)})\n"
            f"KOSDAQ: {_fmt_price(m.kosdaq)} ({_fmt_pct(m.kosdaq_change_pct)})\n"
            f"USD/KRW: {_fmt_price(m.usd_krw)}"
        ),
        "inline": False,
    })
    dist_str = (
        "\n".join(f"  - {k}: {v}" for k, v in sorted(d.source_distribution.items()))
        if d.source_distribution
        else "-"
    )
    fields.append({
        "name": "■ 데이터 수집",
        "value": (
            f"뉴스 {d.news_count}건 / 분석 {d.analyzed_count} / 후보 {d.candidates_count}\n"
            f"{dist_str}"
        ),
        "inline": False,
    })
    fields.append({
        "name": "■ 매매 실행",
        "value": (
            f"매수 {len(t.buys)}종목 / {_fmt_krw(t.buy_total_krw)}\n"
            f"익절 {len(t.take_profits)}종목 / {_fmt_krw(t.tp_total_krw)}\n"
            f"손절 {len(t.stop_losses)}종목 / {_fmt_krw(t.sl_total_krw)}"
        ),
        "inline": False,
    })
    fields.append({
        "name": "■ 포트폴리오",
        "value": (
            f"평가액: {_fmt_krw(p.total_value).lstrip('+')}\n"
            f"당일: {_fmt_pct(p.daily_pnl_pct)} ({_fmt_krw(p.daily_pnl_krw)})\n"
            f"누적: {_fmt_pct(p.cumulative_return_pct)} | MDD: {_fmt_pct(p.mdd_pct, sign=False)}"
        ),
        "inline": False,
    })
    cb = "발동" if r.circuit_breaker_active else "정상"
    fields.append({
        "name": "■ 리스크",
        "value": (
            f"Daily Loss 사용: {r.daily_loss_usage_pct:.0f}%\n"
            f"Circuit Breaker: {cb}"
            + (f" ({r.circuit_breaker_reason})" if r.circuit_breaker_active else "")
        ),
        "inline": False,
    })
    if report.next_actions:
        fields.append({
            "name": "■ 다음 액션",
            "value": "\n".join(f"- {a}" for a in report.next_actions),
            "inline": False,
        })
    return {
        "title": f"[{report.report_date.isoformat()}] 매매 리포트",
        "color": _pick_color(report),
        "fields": fields,
        "footer": {"text": f"strategy_version={report.strategy_version}"},
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def send_daily_report(
    notifier: DiscordNotifier,
    report: DailyReport,
    *,
    mode: str = "embed",
) -> bool:
    """mode: 'embed' (기본) | 'text'. 실패는 로그에서 기록되고 False 반환."""
    if mode == "text":
        return notifier.send(render_text(report), include_timestamp=False)
    return notifier.send_embed(render_embed(report))
