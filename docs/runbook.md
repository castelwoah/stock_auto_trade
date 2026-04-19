# 운영 Runbook — 한국 주식 자동매매 시스템

> 장애 발생 시 **이 문서 1장**만 보고 대응할 수 있어야 한다.
> 각 시나리오는 **징후 → 즉시 조치 → 복구 → 사후 점검** 순으로 기술한다.

---

### 1. 개요 · 빠른 연락처

- **운영자**: kevin@aivelabs.com
- **Discord 채널**: Webhook 전용 (수신 전용, 회신 불가)
- **KIS 고객센터**: 1588-0030 (영업일 08:30 ~ 17:00)
- **Runbook 위치**: `/home/mycom/workspace/mini_project/docs/runbook.md`
- **메인 스크립트**: `/home/mycom/workspace/mini_project/stock_auto_trading.py`
- **설정 파일**: `/home/mycom/workspace/mini_project/config.yaml` (민감정보, git 제외)

---

### 2. 정상 가동 상태 체크리스트

매 영업일 다음 알림이 Discord 에 **시간 순서대로** 들어와야 한다.

| 시각 | 알림 | 의미 |
|------|------|------|
| 07:30 | `master updated` | KOSPI/KOSDAQ 종목 마스터 파일 갱신 완료 |
| 08:30 | `news fetch done` | 뉴스 수집 완료 |
| 09:00 | `start_equity fixed` + `reconciler.verify() OK` | 시작 자산 고정 및 잔고 정합성 확인 |
| 09:10 | `trading loop started` | 메인 매매 루프 가동 |
| 16:00 | `daily report` | 일일 리포트 수신 |

- **알림이 연속 2회 누락되면 장애로 간주**하고 즉시 시나리오 3.7 을 확인한다.
- 주말·공휴일엔 알림이 없는 것이 정상이다.

---

### 3. 시나리오별 플레이북

#### 3.1 ⚠️ KIS 토큰 만료 / 재발급 실패

- **징후**: Discord 에 `token fetch failed` 또는 HTTP 401 응답이 빈발.
- **즉시 조치**:
  1. `sudo systemctl stop mini-trading` 실행 (또는 `pkill -f stock_auto_trading.py`).
  2. 추가 401 로그가 없는지 확인.
- **복구**:
  1. `KIS{YYYYMMDD}` 캐시 파일 삭제 (`rm /home/mycom/workspace/mini_project/KIS$(date +%Y%m%d)`).
  2. `sudo systemctl start mini-trading` 로 재시작.
  3. Discord 에 `token issued` 알림이 오는지 확인.
- **사후 점검**:
  - `config.yaml` 의 `APP_KEY` / `APP_SECRET` 유효기간 확인.
  - KIS OpenAPI 포털 로그인해 키 상태 점검.
  - `expired/` 폴더 누적 파일 정리.

---

#### 3.2 ⚠️ 네트워크 단절 / DNS 실패

- **징후**: `connection timeout`, `Name or service not known` 로그 다수.
- **즉시 조치**: 소프트웨어 내장 **5분 쿨다운**이 자동 적용됨. 대기.
- **복구**:
  1. 30분 누적 실패 시 **매매 전면 OFF**.
  2. `config.yaml` 에 `TRADING_DISABLED=true` 추가 후 재시작.
  3. 네트워크 복구 확인(`ping openapi.koreainvestment.com`) 후 플래그 해제 및 재시작.
- **사후 점검**:
  - ISP / 라우터 / DNS 서버 로그 확인.
  - 장시간 단절 구간의 미실행 주문 영향도 기록.

---

#### 3.3 🛑 Reconciler 불일치 (KIS 잔고 ≠ DB)

- **징후**: `reconcile_mismatch` RiskEvent, 매매 자동 OFF.
- **즉시 조치**: **KIS MTS 앱**에서 실제 잔고를 직접 확인 (육안 검증).
- **복구**:
  1. DB `positions` 테이블을 KIS 실제 잔고와 수동 일치.
  2. `python scripts/resume_trading.py` 실행 (스크립트 미존재 시 `TRADING_DISABLED` 플래그 해제 후 재시작).
  3. `reconciler.verify()` 재통과 확인.
- **사후 점검**:
  - 주문 체결 로그 · API 응답 타임스탬프 순서 추적.
  - 재발 방지: idempotency key 또는 체결 재조회 루틴 점검.

---

#### 3.4 🛑 Circuit Breaker 발동

- **징후**: Discord `CB triggered: 연속 3일 손실` 또는 `누적 -10%` 알림.
- **즉시 조치**: 당일 및 익일 거래 **자동 중단** (수동 개입 불필요).
- **복구 절차 — 다음 거래일 개장 전 수행**:
  1. 전일 손익 확정 (모든 SELL 체결이 DB 에 반영되었는지 확인).
  2. 보유 포지션 점검 → 수동 청산 필요 여부 판단.
  3. 전략 파라미터·설정 검토 (슬리피지·쿨다운 등).
  4. `CircuitBreakerState.reset()` 호출 또는 프로세스 재시작.
  5. Discord 에 `CB manually resumed` 로그 전송.
- **사후 점검**:
  - Post-mortem 문서 1장 작성 (원인·재발 방지 액션).
  - 필요 시 Phase 다운그레이드 (§7 참조).

---

#### 3.5 ⚠️ Daily Loss Limit 도달 (-3%)

- **징후**: `daily_loss_limit triggered` 알림.
- **즉시 조치**: 당일 **신규 매수 자동 중단**. 기존 포지션 **청산은 허용**.
- **복구**: 다음 영업일 09:00 `start_equity` 재조회 시 **자동 리셋**.
- **사후 점검**:
  - 손실 원인 분석 리포트 1장 (종목·진입가·슬리피지).
  - 동일 종목 연속 손실 시 `symbol_dict` 재검토.

---

#### 3.6 ⚠️ 주문 미체결 / API rate limit 경합

- **징후**: 지정가 주문이 3분 이상 미체결, 재시도 2회 후 취소 로그.
- **즉시 조치**: `order_manager` 가 자동 처리 (수동 개입 불필요).
- **복구**:
  - 수집 작업(TR 호출) 우선순위를 **장 마감 후**로 이동.
  - 동시 실행 중인 배치/크롤러를 일시 중단.
- **사후 점검**:
  - Token Bucket 사용률 로그 확인 (`logs/app.log.*`).
  - KIS Rate Limit(20 req/s) 대비 피크 사용률 기록.

---

#### 3.7 🛑 장 마감 비정상 종료 (프로세스 크래시)

- **징후**: 16:00 리포트 미도착, `systemctl status mini-trading` 가 `activating (auto-restart)`.
- **즉시 조치**:
  1. `journalctl -u mini-trading -n 200` 로 최근 로그 확인.
  2. 재시작 대기 중이면 그대로 두고 `reconciler.verify()` 결과 확인.
- **복구**:
  - 불일치 발생 시 **3.3 절차** 따름.
  - 정상이면 별도 조치 불필요.
- **사후 점검**:
  - stderr 로그 / Python traceback 보관.
  - OOM / 디스크 Full 여부 확인 (`dmesg`, `df -h`).

---

### 4. 운영 상수 (변경 시 PR 필요)

| 항목 | 값 |
|------|----|
| 슬리피지 | 0.25% (백테스트·실전 동일) |
| 수수료 | 0.15% |
| 거래세 | 0.23% (매도 시) |
| Daily Loss Limit | -3% |
| Circuit Breaker | 연속 3일 손실 **OR** 누적 -10% |
| 재매수 쿨다운 | 3 영업일 |
| 손절 후 재진입 쿨다운 | 10 영업일 |
| KIS Rate Limit | 20 req/s (Token Bucket 공유) |

---

### 5. Secret 관리

- **민감정보 파일**: `config.yaml`, `.env` — 반드시 `.gitignore` 에 포함.
- **로컬 백업**: 1Password 또는 팀 볼트(Vault).
- **회전 주기**:
  - `APP_SECRET`: **90일**.
  - `DISCORD_WEBHOOK_URL`: 노출 의심 시 **즉시 폐기 + 재발급**.
- 유출 시 즉시 `systemctl stop mini-trading` 후 키 회전.

---

### 6. 로그 보존 정책

- **Hot (즉시 조회용)**: 30일, 로컬 디스크 (`logs/app.log.*`, `RotatingFileHandler`).
- **Cold (장기 보관)**: 90일, 원격 백업 추후 연동.
- **감사용**: RiskEvent / trades 테이블은 **영구 보관** (DB).

---

### 7. 점진적 자본 확대 (rules §13 Phase 4 → 5)

- **Phase 4**: 총자산의 **5% 이내**로 운영. 1개월 이상 안정 운영 확인.
- **Phase 5**: 최대 **50%** 까지 확대. 월 단위 리밸런스.
- **자본 확대는 서면 또는 커밋 메시지 승인이 반드시 필요**하다. 무단 상향 금지.

---

_Last updated: 2026-04-18 · Maintainer: kevin@aivelabs.com_
