"""뉴스 기반 종목 판단 — Claude API (호재/악재 · 상승/하락 예상) + 룰 대체.

장전 브리핑(reporting.news_briefing) 전용. **매매 경로에는 쓰지 않는다** (정보용).

- 입력은 이미 저장된 기사 제목·요약과 시세 수치뿐 (D1 — 전문·계좌 정보는 보내지 않음)
- 출력은 tool_use(JSON 스키마)로 강제하고, 받은 뒤 다시 검증한다
  (모르는 종목·enum 밖 값은 버림, 확신도는 0~1 로 자름)
- 재현성: 모델 ID · 프롬프트 해시 · 입력 기사 ID 를 브리핑 아카이브에 함께 남긴다 (D12 원칙)
- 실패(키 없음·HTTP 오류·타임아웃·스키마 위반)는 JudgeError → 호출측이 룰 판단으로 대체
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import requests


logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-5-5"
TIMEOUT_SEC = 20.0
MAX_TOKENS = 2000
SUMMARY_CHARS = 200
MAX_ARTICLES = 10

ENGINE_LLM, ENGINE_RULE = "llm", "rule"
IMPACTS = ("호재", "악재", "혼재")
DIRECTIONS = ("상승", "하락", "중립")
HORIZONS = ("당일", "단기(1~5일)")
RULE_THRESHOLD = 0.2          # 룰 감성 평균이 ±0.2 를 넘으면 방향 판단


class JudgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArticleBrief:
    id: int
    source: str
    title: str
    summary: str
    published_at: datetime
    sentiment: float = 0.0          # 룰 감성 (-1 ~ +1)


@dataclass(frozen=True)
class SymbolInput:
    symbol: str
    name: str
    articles: tuple[ArticleBrief, ...]
    prev_change_pct: float | None = None     # 직전 거래일 등락률
    rule_score: float | None = None          # 당일 news-universe-v1 점수 (0~100)

    @property
    def mean_sentiment(self) -> float:
        if not self.articles:
            return 0.0
        return sum(a.sentiment for a in self.articles) / len(self.articles)


@dataclass(frozen=True)
class Judgment:
    symbol: str
    impact: str
    direction: str
    horizon: str
    confidence: float
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    duplicate_events: tuple[str, ...] = ()
    engine: str = ENGINE_LLM

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "impact": self.impact, "direction": self.direction,
            "horizon": self.horizon, "confidence": self.confidence,
            "reasons": list(self.reasons), "risks": list(self.risks),
            "duplicate_events": list(self.duplicate_events), "engine": self.engine,
        }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """너는 한국 주식시장 뉴스 분석가다. 종목별로 주어진 기사(제목·요약)만 근거로,
그 뉴스가 해당 종목 주가에 호재인지 악재인지, 오늘 장에서 상승·하락 중 어느 쪽 압력이 더 큰지 판단한다.

규칙:
- 근거(reasons)와 반대 요인(risks)은 입력 기사에 있는 내용만 쓴다. 기사 제목을 짧게 인용하고 [기사ID] 를 붙인다.
- 입력에 없는 사실·수치·전망을 지어내지 않는다. 확신이 없으면 '혼재'/'중립' 과 낮은 confidence 를 쓴다.
- 같은 사건을 다룬 기사 여러 건은 하나의 사건으로 보고 duplicate_events 에 적는다 (중복 기사를 근거 개수로 세지 않는다).
- 이미 전일 주가에 반영됐을 가능성(전일 등락률)도 고려한다.
- confidence 는 0~1. 0.7 이상은 근거가 분명하고 반대 요인이 약할 때만.
- 모든 종목에 대해 submit_judgments 도구로 한 번에 제출한다. 문장은 한국어, 항목당 80자 이내."""

JUDGE_TOOL: dict[str, Any] = {
    "name": "submit_judgments",
    "description": "종목별 뉴스 영향 판단 결과를 제출한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "judgments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "6자리 종목코드"},
                        "impact": {"type": "string", "enum": list(IMPACTS)},
                        "direction": {"type": "string", "enum": list(DIRECTIONS)},
                        "horizon": {"type": "string", "enum": list(HORIZONS)},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                        "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
                        "duplicate_events": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    },
                    "required": ["symbol", "impact", "direction", "horizon", "confidence", "reasons"],
                },
            },
        },
        "required": ["judgments"],
    },
}


def prompt_hash() -> str:
    """시스템 프롬프트 + 도구 스키마 해시 (프롬프트를 바꾸면 값이 바뀐다)."""
    blob = SYSTEM_PROMPT + json.dumps(JUDGE_TOOL, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_user_message(inputs: list[SymbolInput], *, now: datetime) -> str:
    out = [f"기준 시각: {now:%Y-%m-%d %H:%M} KST (장 시작 전)", ""]
    for inp in inputs:
        chg = f"{inp.prev_change_pct:+.2f}%" if inp.prev_change_pct is not None else "알 수 없음"
        out.append(f"## {inp.name} ({inp.symbol}) — 전일 등락률 {chg}")
        for a in inp.articles[:MAX_ARTICLES]:
            summary = (a.summary or "").strip().replace("\n", " ")[:SUMMARY_CHARS]
            line = f"- [{a.id}] ({a.source}, {a.published_at:%m-%d %H:%M}) {a.title.strip()}"
            out.append(line + (f" — {summary}" if summary else ""))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _strs(v: Any, limit: int) -> tuple[str, ...]:
    if not isinstance(v, list):
        return ()
    return tuple(str(x).strip()[:200] for x in v if str(x).strip())[:limit]


def validate_judgments(raw: Any, inputs: list[SymbolInput]) -> list[Judgment]:
    """모델 출력 검증 — 입력에 없는 종목·enum 밖 값은 버린다. 하나도 남지 않으면 JudgeError."""
    if not isinstance(raw, dict) or not isinstance(raw.get("judgments"), list):
        raise JudgeError("schema: judgments missing")
    known = {i.symbol for i in inputs}
    out: dict[str, Judgment] = {}
    for item in raw["judgments"]:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol", "")).strip()
        if sym not in known or sym in out:
            continue
        impact, direction, horizon = item.get("impact"), item.get("direction"), item.get("horizon")
        if impact not in IMPACTS or direction not in DIRECTIONS:
            continue
        try:
            conf = min(1.0, max(0.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            continue
        out[sym] = Judgment(
            symbol=sym, impact=impact, direction=direction,
            horizon=horizon if horizon in HORIZONS else HORIZONS[0],
            confidence=round(conf, 2),
            reasons=_strs(item.get("reasons"), 3),
            risks=_strs(item.get("risks"), 2),
            duplicate_events=_strs(item.get("duplicate_events"), 3),
        )
    if not out:
        raise JudgeError("schema: no valid judgment")
    return [out[i.symbol] for i in inputs if i.symbol in out]


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

Poster = Callable[..., Any]


@dataclass
class ClaudeJudge:
    api_key: str
    model: str = DEFAULT_MODEL
    timeout: float = TIMEOUT_SEC
    post: Poster = field(default=requests.post, repr=False)

    def judge(self, inputs: list[SymbolInput], *, now: datetime) -> list[Judgment]:
        if not inputs:
            return []
        body = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_user_message(inputs, now=now)}],
            "tools": [JUDGE_TOOL],
            "tool_choice": {"type": "tool", "name": JUDGE_TOOL["name"]},
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        try:
            res = self.post(API_URL, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise JudgeError(f"request failed: {type(e).__name__}") from None
        if res.status_code != 200:
            raise JudgeError(f"HTTP {res.status_code}: {_error_type(res)}")
        try:
            data = res.json()
        except ValueError:
            raise JudgeError("invalid JSON response") from None
        for block in data.get("content") or []:
            if block.get("type") == "tool_use" and block.get("name") == JUDGE_TOOL["name"]:
                return validate_judgments(block.get("input"), inputs)
        raise JudgeError("no tool_use block")


def _error_type(res: Any) -> str:
    try:
        return str(res.json().get("error", {}).get("type", ""))[:60]
    except Exception:
        return ""


def rule_judgment(inp: SymbolInput) -> Judgment:
    """LLM 이 없거나 실패했을 때 — 룰 감성 평균으로 방향만. 확신도는 0.5 이하."""
    m = inp.mean_sentiment
    if m > RULE_THRESHOLD:
        impact, direction = "호재", "상승"
    elif m < -RULE_THRESHOLD:
        impact, direction = "악재", "하락"
    else:
        impact, direction = "혼재", "중립"
    ranked = sorted(inp.articles, key=lambda a: abs(a.sentiment), reverse=True)
    reasons = tuple(f"{a.title.strip()[:70]} [{a.id}]" for a in ranked[:2])
    return Judgment(
        symbol=inp.symbol, impact=impact, direction=direction, horizon=HORIZONS[0],
        confidence=round(min(0.5, abs(m)), 2), reasons=reasons, engine=ENGINE_RULE,
    )
