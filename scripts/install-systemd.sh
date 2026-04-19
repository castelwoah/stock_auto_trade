#!/usr/bin/env bash
# ============================================================
# systemd 유닛 설치 (1회 실행)
# 사용: sudo bash scripts/install-systemd.sh
# 멱등: 재실행 안전 (cp -f + daemon-reload + enable)
# ============================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OPS_DIR="$PROJECT_ROOT/ops"
TARGET=/etc/systemd/system

UNITS=(
    "mini-trading.service"
    "mini-trading-start.timer"
    "mini-trading-stop.timer"
    "mini-trading-stop.service"
)

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: sudo 권한이 필요합니다. 'sudo bash $0' 로 실행하세요." >&2
    exit 1
fi

echo "[1/5] logs 디렉터리 확인"
mkdir -p "$PROJECT_ROOT/logs"
chown -R mycom:mycom "$PROJECT_ROOT/logs"

echo "[2/5] systemd 유닛 파일 복사 → $TARGET"
for u in "${UNITS[@]}"; do
    cp -f "$OPS_DIR/$u" "$TARGET/$u"
    echo "  - $u"
done

echo "[3/5] systemctl daemon-reload"
systemctl daemon-reload

echo "[4/5] 타이머 활성화 (enable --now)"
systemctl enable --now mini-trading-start.timer
systemctl enable --now mini-trading-stop.timer

echo "[5/5] 상태 확인"
systemctl list-timers 'mini-trading*' --no-pager
echo
echo "✅ systemd 유닛 설치 완료"
echo
echo "다음 평일 07:00 KST 에 자동 기동됩니다."
echo "수동 즉시 기동:  sudo systemctl start mini-trading"
echo "실시간 로그:     journalctl -u mini-trading -f"
echo "상태 확인:       systemctl status mini-trading"
