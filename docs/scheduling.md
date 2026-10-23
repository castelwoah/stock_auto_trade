# 스케줄링·운영 가이드 (systemd + crontab 하이브리드)

> 본 문서는 **인프라 레이어** 운영 가이드. 트레이딩 로직 장애는 `docs/runbook.md` 참조.
> 한 번 설치하면 Phase 3 → 5 전 기간 동안 변경 불필요.

## 1. 전체 구조

```
┌─────────────────────────────────────────────────────────────────┐
│  systemd  ── 데몬 생명주기 (start / stop / 자동 재시작)          │
│             │                                                    │
│             ▼                                                    │
│  python -m src.main  ── APScheduler 가 내부 6개 job 실행          │
│             │                                                    │
│             ▼                                                    │
│  crontab  ── 부가 작업 (DB 백업·로그 퍼지·헬스체크)              │
└─────────────────────────────────────────────────────────────────┘
```

## 2. 설치 (1회)

### 사전 확인
```bash
# 시스템 시간대 확인 (Asia/Seoul 이어야 함)
timedatectl | grep "Time zone"
# 다르면: sudo timedatectl set-timezone Asia/Seoul

# Python 환경: 프로젝트 .venv (없으면 생성)
bash scripts/setup-dev.sh
# ops/*.service 는 템플릿 — install-systemd.sh 가 @PROJECT_ROOT@ / @RUN_USER@ / @PYTHON@ 를
# 실제 값(저장소 위치, sudo 실행 사용자, .venv/bin/python)으로 치환해 설치한다
```

### systemd 설치
```bash
cd "$PROJECT_ROOT"            # 예: /home/amoll/workspace/stock_auto_trade
sudo bash scripts/install-systemd.sh
```

성공 시 출력 예시:
```
[5/5] 상태 확인
NEXT                        LEFT          UNIT                       ...
Mon 2026-04-20 07:00:00 KST 8h left       mini-trading-start.timer   ...
Mon 2026-04-20 17:00:00 KST 18h left      mini-trading-stop.timer    ...
✅ systemd 유닛 설치 완료
```

### crontab 설치
```bash
bash scripts/install-crontab.sh   # sudo 불필요
```

## 3. 일상 운영

### 상태 확인
```bash
# 데몬 상태 (가장 자주 사용)
systemctl status mini-trading

# 타이머 (다음 트리거 시각)
systemctl list-timers 'mini-trading*'

# 실시간 로그
journalctl -u mini-trading -f

# 오늘 로그만
journalctl -u mini-trading --since today

# 에러만
journalctl -u mini-trading -p err

# crontab 항목 확인
crontab -l | grep -A 100 mini-trading
```

### 수동 제어
```bash
# 즉시 기동
sudo systemctl start mini-trading

# 즉시 종료 (graceful, SIGTERM 60초 유예)
sudo systemctl stop mini-trading

# 재시작 (config.yaml 수정 후 등)
sudo systemctl restart mini-trading

# 자동 기동 잠시 비활성화 (휴가 등)
sudo systemctl disable mini-trading-start.timer
# 복귀 시
sudo systemctl enable --now mini-trading-start.timer
```

## 4. 동작 타임라인 (영업일)

| 시각 | 주체 | 작업 |
|---|---|---|
| 03:00 | crontab | 30일 이상 경과 로그 삭제 |
| 03:30 | crontab | SQLite DB 백업 → `logs/trading.db.YYYYMMDD.bak` |
| 일 04:00 | crontab | 7일 이상 경과 DB 백업 삭제 |
| **07:00** | **systemd timer** | **데몬 기동** |
| 07:30 | APScheduler (내부) | KOSPI/KOSDAQ master 갱신 (D9) |
| 08:00~14:50 | APScheduler | 뉴스 수집 (연합 RSS + DART 키 있으면), `NEWS_POLL_MINUTES` 간격 → 감시 종목 재구성 |
| 08:50 | APScheduler | 감시 종목 구성 (장 시작 전 확정본, `StockScore news-universe-v1` 기록) |
| 09:00 | APScheduler | DailyLossState start_equity 고정 (D7) + reconciler.verify (D6) |
| 09:10~15:20 | APScheduler | 5분 간격 trading tick |
| 15:00 | (내부) | 신규 매수 cut-off |
| 15:15 | (내부) | EOD 미체결 일괄 취소 |
| 16:00 | APScheduler | 일일 리포트 → Discord + JSON/CSV 아카이브 |
| **17:00** | **systemd timer** | **데몬 종료 (SIGTERM)** |

## 5. Phase 전환 (설정 파일 불변)

### Phase 3 → Phase 4 (실자본 5%)
```bash
# 1. config.yaml 수정
# KIS_ENV: PAPER → LIVE
# (필요 시) DISCORD_WEBHOOK_URL: 실전 채널로 분리

# 2. 데몬만 재시작 (systemd 유닛은 변경 X)
sudo systemctl restart mini-trading

# 확인
journalctl -u mini-trading -n 20 | grep KIS_ENV
```

### Phase 4 → Phase 5 (자본 확대)
- 파라미터 (Kelly fraction, watchlist 크기) 만 코드/설정에서 조정
- 데몬 재시작
- **systemd 유닛·crontab 모두 불변**

## 6. 장애 시 대응 (요약)

| 증상 | 1차 진단 | 2차 조치 |
|---|---|---|
| Discord 16:00 리포트 미도착 | `systemctl status mini-trading` | 재시작 시도 → `journalctl -u mini-trading -p err` |
| 데몬 status: `failed` | `journalctl -u mini-trading -n 50` | 마지막 traceback 확인 → `runbook.md` 매칭 |
| 데몬 status: `inactive (dead)` | 다음 timer 트리거 시각 확인 | `systemctl start mini-trading` 즉시 기동 |
| 자동 재시작 반복 (`Restart=on-failure` 가 도는 중) | `journalctl -u mini-trading -n 200` | 30초 간격 크래시 → 코드 버그. config 수정 후 재배포 |
| 토큰 만료 / Reconciler 불일치 | `runbook.md` §3.1, §3.3 | 데몬은 살아있고 내부 trading_off 만 활성 |

상세는 `docs/runbook.md` 참조.

## 7. 보안·권한 정리

- `mini-trading.service` 의 `User=@RUN_USER@` (설치한 sudo 사용자) — root 로 실행하지 않음
- `ProtectHome=read-only` — 다른 디렉터리 쓰기 금지
- `ReadWritePaths=@PROJECT_ROOT@` — 본 프로젝트만 쓰기 허용
- crontab 도 같은 사용자 권한
- `config.yaml` 권한: `chmod 600 config.yaml` (소유자만 읽기) 권장

## 8. 제거 (필요 시)

```bash
sudo bash scripts/uninstall.sh
```

systemd 유닛·crontab 항목 모두 제거. `logs/`, `trading.db` 는 수동 삭제.

## 9. 자주 묻는 질문

**Q. 휴장일에는 어떻게 되나?**
- systemd timer 는 Mon-Fri 만 기동 → 토/일 자체 비활성
- 평일 휴장(설·추석 등) 도 데몬은 기동되지만 내부 `scheduler/calendar.py` 가 job 본문 skip
- 결과: 거래 발생 안 함, 로그만 짧게 남음

**Q. 서버 재부팅 후?**
- `Persistent=true` 로 설정된 start.timer 가 부팅 후 즉시 missed trigger 보상 실행
- 평일 07:00 이후 재부팅 → 즉시 데몬 기동
- 평일 17:00 이후 재부팅 → 다음 평일 07:00 까지 대기

**Q. 코드 수정 후 반영하려면?**
```bash
git pull   # 또는 직접 수정
sudo systemctl restart mini-trading
```

**Q. 모의투자 ↔ 실전 전환은?**
- `config.yaml` 의 `KIS_ENV` 와 `URL_BASE` 만 수정 후 데몬 재시작
- systemd / crontab 설정 파일은 절대 건드리지 않음

**Q. 한 번 설치 후 정말 변경 안 해도 되나?**
- 정상 운영 시: 인프라 레이어 (systemd 유닛 4개 + crontab) 는 **불변**
- 변경하는 것: `config.yaml`, Python 코드, `git pull`
- 인프라 변경 사유는 사실상 "서버 이전" 정도

## 10. 업그레이드·이전

### Python 버전 업그레이드
```bash
# 새 Python 경로로 ops/mini-trading.service 의 ExecStart 수정
# 재설치
sudo bash scripts/install-systemd.sh   # 멱등 — 재실행 안전
```

### 서버 이전
```bash
# 신규 서버에서
git clone <repo>
cd stock_auto_trade
bash scripts/setup-dev.sh
# config.yaml 복사
sudo bash scripts/install-systemd.sh
bash scripts/install-crontab.sh
# 끝
```

## 11. 점검 체크리스트 (설치 후 1주)

- [ ] D1 (월) 07:00:30 이내 데몬 기동, journalctl 로 확인
- [ ] D1 07:30 master_refresh 로그 확인
- [ ] D1 09:00 reconciler 통과 로그
- [ ] D1 16:00 Discord 리포트 도착
- [ ] D1 17:00 graceful shutdown ("trading scheduler stopped")
- [ ] D2 03:30 DB 백업 파일 생성 (`logs/trading.db.YYYYMMDD.bak`)
- [ ] D5 (금) 17:00 정상 종료
- [ ] 토 / 일 자동 기동 안 됨 확인
- [ ] D8 (월) 정상 재기동
- [ ] 첫 일요일 04:00 8일 이상 경과 백업 삭제 확인
