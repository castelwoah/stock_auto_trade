#!/usr/bin/env python3
"""PreToolUse guard — 서브에이전트 금지 영역 보호 (CLAUDE.md "구현 실행 전략" §3).

risk/*, strategy/registry.py, execution/reconciler.py, execution/order_manager.py,
persistence/models.py(스키마) 는 메인 세션만 수정한다. 서브에이전트 호출(payload 에
agent_id 가 있는 경우)에서 Edit/Write 를 차단한다. 메인 세션에는 경고 없이 통과.
"""
import json
import re
import sys

PROTECTED = re.compile(
    r"src/(?:risk/[^/]+\.py|strategy/registry\.py|execution/(?:reconciler|order_manager)\.py|persistence/models\.py)$"
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not payload.get("agent_id"):
        return 0
    path = (payload.get("tool_input") or {}).get("file_path", "")
    if PROTECTED.search(path):
        print(
            f"[protect_invariants] 서브에이전트 금지 영역: {path} — 변경안을 보고만 하고 메인 세션에 넘기세요.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
