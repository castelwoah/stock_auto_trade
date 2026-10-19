#!/usr/bin/env python3
"""PostToolUse — src/<layer>/ 또는 tests/<layer>/ 수정 시 해당 계층 테스트만 즉시 실행.

실패하면 exit 2 로 pytest 요약을 모델에 되돌려 바로 고치게 한다. 전체 스위트는 ~2초라
계층 매핑이 없는 파일(src/main.py, src/config.py 등)은 전체를 돌린다.
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = os.path.join(ROOT, ".venv", "bin", "python")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = (payload.get("tool_input") or {}).get("file_path", "")
    if not path.endswith(".py"):
        return 0
    rel = os.path.relpath(os.path.abspath(path), ROOT)
    if rel.startswith(".."):
        return 0
    m = re.match(r"(?:src|tests)/([a-z_]+)/", rel)
    if m and os.path.isdir(os.path.join(ROOT, "tests", m.group(1))):
        target = os.path.join("tests", m.group(1))
    elif rel.startswith("src/") or rel.startswith("tests/"):
        target = "tests"
    else:
        return 0
    if not os.path.exists(PY):
        print("[run_layer_tests] .venv 없음 — scripts/setup-dev.sh 실행 필요", file=sys.stderr)
        return 0
    proc = subprocess.run(
        [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", target],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode not in (0, 5):  # 5 = no tests collected
        tail = "\n".join(proc.stdout.strip().splitlines()[-25:])
        print(f"[run_layer_tests] {target} 실패:\n{tail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
