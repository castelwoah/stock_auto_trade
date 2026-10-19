#!/usr/bin/env bash
# ============================================================
# 전체 제거 (systemd 유닛 + crontab 항목)
# 사용: sudo bash scripts/uninstall.sh
# ============================================================
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: sudo 권한이 필요합니다." >&2
    exit 1
fi

# shellcheck source=_ops_common.sh
source "$(dirname "$0")/_ops_common.sh"
TARGET=/etc/systemd/system
UNITS=(
    "mini-trading.service"
    "mini-trading-start.timer"
    "mini-trading-stop.timer"
    "mini-trading-stop.service"
)

echo "[1/4] systemd 타이머 비활성화"
systemctl disable --now mini-trading-start.timer 2>/dev/null || true
systemctl disable --now mini-trading-stop.timer 2>/dev/null || true
systemctl stop mini-trading 2>/dev/null || true

echo "[2/4] systemd 유닛 파일 삭제"
for u in "${UNITS[@]}"; do
    rm -f "$TARGET/$u"
    echo "  - $u"
done
systemctl daemon-reload

echo "[3/4] 사용자 crontab 에서 mini-trading 블록 제거"
TMPFILE=$(mktemp)
sudo -u "$RUN_USER" crontab -l 2>/dev/null > "$TMPFILE" || true
sed -i '/# >>> mini-trading >>>/,/# <<< mini-trading <<</d' "$TMPFILE"
sudo -u "$RUN_USER" crontab "$TMPFILE"
rm -f "$TMPFILE"

echo "[4/4] 정리 완료"
echo
echo "✅ 제거 완료. logs/ 와 trading.db 는 보존되었습니다 (수동 삭제 필요 시 별도)."
