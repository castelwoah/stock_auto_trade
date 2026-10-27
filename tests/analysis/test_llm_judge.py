"""llm_judge — 프롬프트 구성 · 응답 검증 · HTTP 실패 격리 · 룰 대체."""
from datetime import datetime
from unittest.mock import MagicMock

import pytest
import requests

from src.analysis.llm_judge import (
    API_URL,
    ENGINE_RULE,
    JUDGE_TOOL,
    ArticleBrief,
    ClaudeJudge,
    JudgeError,
    SymbolInput,
    build_user_message,
    prompt_hash,
    rule_judgment,
    validate_judgments,
)

NOW = datetime(2026, 10, 28, 8, 40)


def _inp(symbol="005930", name="삼성전자", sentiments=(0.0, -1.0), prev=1.2):
    arts = tuple(
        ArticleBrief(id=i, source="yonhap_rss", title=f"기사{i}", summary=f"요약{i}" * 100,
                     published_at=datetime(2026, 10, 27, 9, i), sentiment=s)
        for i, s in enumerate(sentiments, 1)
    )
    return SymbolInput(symbol=symbol, name=name, articles=arts, prev_change_pct=prev)


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    return r


def _tool_payload(judgments):
    return {"content": [{"type": "text", "text": "x"},
                        {"type": "tool_use", "name": "submit_judgments", "input": {"judgments": judgments}}]}


GOOD = {"symbol": "005930", "impact": "호재", "direction": "상승", "horizon": "당일",
        "confidence": 0.7, "reasons": ["반도체 급등 [1]"], "risks": ["과징금 [2]"]}


def test_user_message_has_ids_change_and_truncated_summary():
    msg = build_user_message([_inp()], now=NOW)
    assert "## 삼성전자 (005930) — 전일 등락률 +1.20%" in msg
    assert "[1] (yonhap_rss, 10-27 09:01) 기사1" in msg
    line = next(l for l in msg.splitlines() if l.startswith("- [1]"))
    assert len(line) < 300                                      # 요약 200자 제한


def test_user_message_unknown_change():
    assert "전일 등락률 알 수 없음" in build_user_message([_inp(prev=None)], now=NOW)


def test_prompt_hash_stable():
    assert prompt_hash() == prompt_hash() and len(prompt_hash()) == 16


def test_judge_posts_tool_choice_and_parses():
    post = MagicMock(return_value=_resp(payload=_tool_payload([GOOD])))
    j = ClaudeJudge(api_key="sk-ant-test", model="claude-haiku-5-5", post=post)
    (out,) = j.judge([_inp()], now=NOW)
    assert out.impact == "호재" and out.confidence == 0.7 and out.reasons == ("반도체 급등 [1]",)
    args, kw = post.call_args
    assert args[0] == API_URL
    assert kw["headers"]["x-api-key"] == "sk-ant-test"
    assert kw["json"]["tool_choice"] == {"type": "tool", "name": JUDGE_TOOL["name"]}
    assert kw["json"]["model"] == "claude-haiku-5-5" and kw["timeout"] == 20.0


def test_judge_empty_inputs_no_call():
    post = MagicMock()
    assert ClaudeJudge(api_key="k", post=post).judge([], now=NOW) == []
    post.assert_not_called()


@pytest.mark.parametrize("resp,match", [
    (_resp(status=529, payload={"error": {"type": "overloaded_error"}}), "HTTP 529: overloaded_error"),
    (_resp(payload={"content": [{"type": "text", "text": "hi"}]}), "no tool_use"),
    (_resp(payload=_tool_payload([{"symbol": "999999", **{k: v for k, v in GOOD.items() if k != "symbol"}}])),
     "no valid"),
])
def test_judge_failures_raise_judge_error(resp, match):
    with pytest.raises(JudgeError, match=match):
        ClaudeJudge(api_key="k", post=MagicMock(return_value=resp)).judge([_inp()], now=NOW)


def test_judge_network_error_hides_details():
    post = MagicMock(side_effect=requests.ConnectionError("https://api.anthropic.com?key=secret"))
    with pytest.raises(JudgeError) as ei:
        ClaudeJudge(api_key="k", post=post).judge([_inp()], now=NOW)
    assert "secret" not in str(ei.value) and "ConnectionError" in str(ei.value)


def test_validate_drops_bad_items_and_clamps():
    inputs = [_inp(), _inp(symbol="000660", name="SK하이닉스")]
    raw = {"judgments": [
        {**GOOD, "confidence": 3},                               # clamp → 1.0
        {**GOOD, "symbol": "005930", "impact": "악재"},           # 중복 종목 → 무시
        {**GOOD, "symbol": "000660", "direction": "폭등"},        # enum 밖 → 버림
        "garbage",
    ]}
    (out,) = validate_judgments(raw, inputs)
    assert out.symbol == "005930" and out.confidence == 1.0 and out.impact == "호재"


def test_validate_bad_horizon_defaults_and_list_limits():
    raw = {"judgments": [{**GOOD, "horizon": "장기", "reasons": ["a", "b", "c", "d"], "risks": "x"}]}
    (out,) = validate_judgments(raw, [_inp()])
    assert out.horizon == "당일" and len(out.reasons) == 3 and out.risks == ()


def test_validate_missing_judgments():
    with pytest.raises(JudgeError):
        validate_judgments({"x": 1}, [_inp()])


@pytest.mark.parametrize("sentiments,impact,direction", [
    ((0.8, 0.6), "호재", "상승"),
    ((-1.0, -0.4), "악재", "하락"),
    ((0.1, -0.1), "혼재", "중립"),
])
def test_rule_judgment(sentiments, impact, direction):
    j = rule_judgment(_inp(sentiments=sentiments))
    assert (j.impact, j.direction, j.engine) == (impact, direction, ENGINE_RULE)
    assert j.confidence <= 0.5 and j.reasons
