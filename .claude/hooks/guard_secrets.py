#!/usr/bin/env python3
"""PreToolUse guard — 민감 파일 접근·커밋 차단 (config.yaml / .env / token.dat / KIS{YYYYMMDD} / expired/).

exit 2 + stderr 메시지 → Claude Code 가 해당 tool 호출을 차단하고 사유를 모델에 전달한다.
"""
import json
import re
import sys

SENSITIVE = re.compile(
    r"(?:^|[\s/'\"=])(?:config\.yaml|\.env(?:\.[\w.-]+)?|token\.dat|KIS\d{8}|expired/)(?=$|[\s'\";|&)])"
)
# .env.example 같은 템플릿은 허용
ALLOWED = re.compile(r"\.env\.example|config\.example\.yaml")


def _hit(text: str) -> bool:
    cleaned = ALLOWED.sub("", text)
    return bool(SENSITIVE.search(cleaned))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input") or {}

    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        target = inp.get("file_path") or inp.get("notebook_path") or ""
        if _hit(" " + target):
            print(f"[guard_secrets] 민감 파일 접근 차단: {target} — 값은 사용자가 직접 편집하세요.", file=sys.stderr)
            return 2
    elif tool == "Bash":
        cmd = inp.get("command", "")
        if _hit(" " + cmd):
            print("[guard_secrets] 민감 파일(config.yaml/.env/token/KIS*/expired)을 다루는 명령 차단.", file=sys.stderr)
            return 2
        if re.search(r"git\s+add\s+(-A|--all|\.)(\s|$)", cmd):
            print("[guard_secrets] `git add -A/.` 금지 — 파일을 명시적으로 지정하세요.", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
