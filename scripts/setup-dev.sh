#!/usr/bin/env bash
# 개발 환경 재현 — uv 로 Python 3.11 venv(.venv) 생성 + requirements 설치 + 테스트.
# 시스템 python3 에 pip/ensurepip 가 없어도 동작한다 (sudo 불필요). 멱등.
set -euo pipefail
cd "$(dirname "$0")/.."

UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  UV="$HOME/.local/bin/uv"
fi

[ -x .venv/bin/python ] || "$UV" venv --python 3.11 .venv
"$UV" pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest -q -p no:cacheprovider
