"""Supply/Demand (수급) factor: foreign + institutional net buy + volume.

모든 서브스코어는 0~100. None 지표는 평균에서 제외된다.
insufficient_data: 모든 지표가 None 일 때만 True 를 반환한다.
"""
from __future__ import annotations

from typing import Any

from src.analysis.contracts import FactorScore, clip_score, linear_map


# 절대금액 기준 정규화 스케일 (market_cap 없을 때 폴백)
# 5거래일 누적 ±100억 원 = ±100 / 0 점
_ABS_NET_SCALE_KRW = 100.0 * 1e8  # 100억 원

# market_cap 대비 %기준 정규화: ±1% of market_cap over 5 days
_PCT_LO = -1.0
_PCT_HI = 1.0


def _score_net_buy(net: float | None, market_cap: float | None) -> tuple[float | None, str]:
    """외인/기관 순매수 → 0~100.

    market_cap 있으면 비율 기준 (소형주/대형주 과민반응 방지).
    없으면 절대금액 폴백 (±100억 스케일).
    """
    if net is None:
        return None, "none"
    if market_cap is not None and market_cap > 0:
        pct = (net / market_cap) * 100.0
        return linear_map(pct, src_lo=_PCT_LO, src_hi=_PCT_HI), "pct_of_mcap"
    return linear_map(float(net), src_lo=-_ABS_NET_SCALE_KRW, src_hi=_ABS_NET_SCALE_KRW), "abs_krw"


def _score_volume_ratio(ratio: float | None) -> float | None:
    """거래량비 (5d avg / 60d avg) → 0~100.

    구간:
      - ratio < 1.0 : 거래 감소 → 0 ~ 50 선형 (0.3 이하는 0점 근처)
      - 1.0 <= ratio < 1.5 : 정상 소폭 증가 → 50 ~ 70
      - 1.5 <= ratio <= 3.0 : 관심 급증, 가점 → 70 ~ 90
      - ratio > 3.0 : 과열 의심 감점 → 80 근방 (최대 3x 기점 85 → 점진 하락)
    """
    if ratio is None:
        return None
    r = float(ratio)
    if r < 1.0:
        # 0.0 → 0, 1.0 → 50
        return linear_map(r, src_lo=0.0, src_hi=1.0, dst_lo=0.0, dst_hi=50.0)
    if r < 1.5:
        # 1.0 → 50, 1.5 → 70
        return linear_map(r, src_lo=1.0, src_hi=1.5, dst_lo=50.0, dst_hi=70.0)
    if r <= 3.0:
        # 1.5 → 70, 3.0 → 90 (관심 급증 가점)
        return linear_map(r, src_lo=1.5, src_hi=3.0, dst_lo=70.0, dst_hi=90.0)
    # r > 3.0 : 과열 감점. 3.0 → 85 (살짝 감점), 6.0 이상 → 70
    return linear_map(r, src_lo=3.0, src_hi=6.0, dst_lo=85.0, dst_hi=70.0)


def _build_reason(
    foreign_sub: float | None,
    inst_sub: float | None,
    vol_sub: float | None,
    vol_ratio: float | None,
) -> str:
    bits: list[str] = []
    foreign_pos = foreign_sub is not None and foreign_sub >= 65
    inst_pos = inst_sub is not None and inst_sub >= 65
    foreign_neg = foreign_sub is not None and foreign_sub <= 35
    inst_neg = inst_sub is not None and inst_sub <= 35
    if foreign_pos and inst_pos:
        bits.append("외인·기관 동반 순매수")
    elif foreign_pos:
        bits.append("외인 순매수")
    elif inst_pos:
        bits.append("기관 순매수")
    elif foreign_neg and inst_neg:
        bits.append("외인·기관 동반 순매도")
    elif foreign_neg:
        bits.append("외인 순매도")
    elif inst_neg:
        bits.append("기관 순매도")

    if vol_ratio is not None:
        if vol_ratio > 3.0:
            bits.append("거래량 과열")
        elif vol_ratio >= 1.5:
            bits.append("거래량 급증")
        elif vol_ratio < 0.7:
            bits.append("거래량 감소")
    return " + ".join(bits)


def compute_flow_score(flow: dict) -> FactorScore:
    """수급 팩터 스코어링.

    입력 키:
      - foreign_net_5d (float|None)        : 5일 누적 외국인 순매수액 (원)
      - institutional_net_5d (float|None)  : 5일 누적 기관 순매수액 (원)
      - volume_ratio_5d_vs_60d (float|None): 최근 5일/60일 평균 거래량비
      - market_cap (float|None, optional)  : 시가총액 (원). 있으면 %기준 정규화.
    """
    foreign = flow.get("foreign_net_5d")
    inst = flow.get("institutional_net_5d")
    vol_ratio = flow.get("volume_ratio_5d_vs_60d")
    market_cap = flow.get("market_cap")

    foreign_sub, foreign_basis = _score_net_buy(foreign, market_cap)
    inst_sub, inst_basis = _score_net_buy(inst, market_cap)
    vol_sub = _score_volume_ratio(vol_ratio)

    subs = [s for s in (foreign_sub, inst_sub, vol_sub) if s is not None]

    details: dict[str, Any] = {
        "foreign_net_5d": foreign,
        "institutional_net_5d": inst,
        "volume_ratio_5d_vs_60d": vol_ratio,
        "market_cap": market_cap,
        "foreign_sub_score": foreign_sub,
        "institutional_sub_score": inst_sub,
        "volume_sub_score": vol_sub,
        "foreign_basis": foreign_basis,
        "institutional_basis": inst_basis,
        "market_cap_used": market_cap is not None and market_cap > 0,
    }

    if not subs:
        return FactorScore(
            score=50.0,
            details=details,
            reason="수급 데이터 없음",
            insufficient_data=True,
        )

    avg = sum(subs) / len(subs)
    score = clip_score(avg)
    reason = _build_reason(foreign_sub, inst_sub, vol_sub, vol_ratio)
    return FactorScore(
        score=score,
        details=details,
        reason=reason,
        insufficient_data=False,
    )
