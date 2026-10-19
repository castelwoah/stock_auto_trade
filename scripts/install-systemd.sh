#!/usr/bin/env bash
# ============================================================
# systemd 유닛 설치 (1회 실행)
# 사용: sudo bash scripts/install-systemd.sh
# 멱등: 재실행 안전 (cp -f + daemon-reload + enable)
# ============================================================
set -euo pipefail

# shellcheck source=_ops_common.sh
source "$(dirname "$0")/_ops_common.sh"
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
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: $PYTHON 이 없습니다. 먼저 'bash scripts/setup-dev.sh' 로 .venv 를 만드세요." >&2
    exit 1
fi
echo "  PROJECT_ROOT=$PROJECT_ROOT  RUN_USER=$RUN_USER:$RUN_GROUP  PYTHON=$PYTHON"

echo "[1/5] logs · run 디렉터리 확인 (run/ 은 킬 스위치 파일 위치)"
mkdir -p "$PROJECT_ROOT/logs" "$PROJECT_ROOT/run"
chown -R "$RUN_USER:$RUN_GROUP" "$PROJECT_ROOT/logs" "$PROJECT_ROOT/run"

echo "[2/5] systemd 유닛 렌더링 → $TARGET"
for u in "${UNITS[@]}"; do
    render "$OPS_DIR/$u" "$TARGET/$u"
    echo "  - $u"
done
systemd-analyze verify "$TARGET/mini-trading.service" || {
    echo "ERROR: 유닛 검증 실패" >&2; exit 1; }

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
