#!/usr/bin/env bash
# ============================================================
# 사용자 crontab 에 mini-trading 부가 작업 추가 (1회 실행)
# 사용: bash scripts/install-crontab.sh   (sudo 불필요)
# 멱등: 기존 mini-trading 항목 제거 후 재추가
# ============================================================
set -euo pipefail

# shellcheck source=_ops_common.sh
source "$(dirname "$0")/_ops_common.sh"
SOURCE="$OPS_DIR/cron.d/mini-trading"

if [[ ! -f "$SOURCE" ]]; then
    echo "ERROR: $SOURCE 파일이 없습니다." >&2
    exit 1
fi

echo "[1/4] 기존 crontab 백업"
TMPFILE=$(mktemp)
crontab -l 2>/dev/null > "$TMPFILE" || true
cp "$TMPFILE" "$TMPFILE.bak"
echo "  백업 위치: $TMPFILE.bak"

echo "[2/4] 기존 mini-trading 블록 제거 (멱등성)"
# BEGIN ~ END 마커 사이를 제거 + 마커 자체도 제거
sed -i '/# >>> mini-trading >>>/,/# <<< mini-trading <<</d' "$TMPFILE"

echo "[3/4] 신규 항목 append (@PROJECT_ROOT@ → $PROJECT_ROOT)"
RENDERED=$(mktemp)
render "$SOURCE" "$RENDERED"
{
    echo ""
    echo "# >>> mini-trading >>>"
    cat "$RENDERED"
    echo "# <<< mini-trading <<<"
} >> "$TMPFILE"
rm -f "$RENDERED"

echo "[4/4] crontab 설치"
crontab "$TMPFILE"
rm -f "$TMPFILE"

echo
echo "✅ crontab 설치 완료"
echo
echo "현재 crontab:"
echo "----------"
crontab -l | grep -A 100 'mini-trading' || true
echo "----------"
echo
echo "전체 확인:  crontab -l"
echo "수정:       crontab -e"
