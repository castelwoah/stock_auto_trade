#!/usr/bin/env bash
# ============================================================
# ops 템플릿 공통 변수 + 렌더 함수 (install-systemd / install-crontab / uninstall 이 source)
#   @PROJECT_ROOT@  이 저장소의 절대 경로
#   @RUN_USER@      데몬 실행 사용자 (sudo 실행 시 SUDO_USER, 아니면 저장소 소유자)
#   @RUN_GROUP@     그 사용자의 기본 그룹
#   @PYTHON@        $PROJECT_ROOT/.venv/bin/python (scripts/setup-dev.sh 로 생성)
# ============================================================

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPS_DIR="$PROJECT_ROOT/ops"
RUN_USER="${SUDO_USER:-$(stat -c %U "$PROJECT_ROOT")}"
RUN_GROUP="$(id -gn "$RUN_USER")"
PYTHON="$PROJECT_ROOT/.venv/bin/python"

# render <template> <dest> — 치환 후 남은 @PLACEHOLDER@ 가 있으면 실패
render() {
    local src="$1" dest="$2"
    sed -e "s|@PROJECT_ROOT@|$PROJECT_ROOT|g" \
        -e "s|@RUN_USER@|$RUN_USER|g" \
        -e "s|@RUN_GROUP@|$RUN_GROUP|g" \
        -e "s|@PYTHON@|$PYTHON|g" \
        "$src" > "$dest"
    if grep -q '@[A-Z_]*@' "$dest"; then
        echo "ERROR: 치환되지 않은 placeholder 가 남았습니다: $dest" >&2
        grep -n '@[A-Z_]*@' "$dest" >&2
        return 1
    fi
}
