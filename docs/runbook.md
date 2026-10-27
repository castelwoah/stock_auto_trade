# 운영 Runbook — 한국 주식 자동매매 시스템

> 장애 발생 시 **이 문서 1장**만 보고 대응할 수 있어야 한다.
> 각 시나리오는 **징후 → 즉시 조치 → 복구 → 사후 점검** 순으로 기술한다.

---

### 1. 개요 · 빠른 연락처

- **운영자**: kevin@aivelabs.com
- **Discord 채널**: Webhook 전용 (수신 전용, 회신 불가)
- **KIS 고객센터**: 1588-0030 (영업일 08:30 ~ 17:00)
- **`$PROJECT_ROOT`**: 저장소 경로 (현재 `/home/amoll/workspace/stock_auto_trade`)
- **Runbook 위치**: `$PROJECT_ROOT/docs/runbook.md`
- **데몬**: `$PROJECT_ROOT/.venv/bin/python -m src.main` (systemd `mini-trading.service`)
- **설정 파일**: `$PROJECT_ROOT/config.yaml` (민감정보, git 제외)

#### 1.1 실전 운용 스위치 (Phase A, 2026-10)

| 설정 (`config.yaml` 또는 ENV) | 기본값 | 의미 |
|---|---|---|
| `SHADOW_MODE` | `true` | 전 파이프라인·리스크 게이트를 돌리되 **KIS 주문 송신만 생략** (`Trade.status='shadow'`). 실주문은 `false` 로 바꾸고 재시작 |
| `SHADOW_CAPITAL_KRW` | 없음 | 섀도 모드 전용 **가상 자본**(원). 매수 금액·투입 상한·일일 손실 기준을 실계좌 대신 이 값으로 계산(실계좌 조회·Reconcile 은 그대로). 당일 섀도 매수는 투입액으로 누적. `SHADOW_MODE: false` 면 무시 |
| `MAX_DEPLOY_PCT` | `5.0` | 09:00 평가액 대비 **전체 투입 상한** %. 미체결 매수 포함, 초과 매수는 `capital_cap` 차단. 금액이 1주 가격보다 작으면 `qty_zero` 로 기록 (종목·사유별 하루 1건) |
| `MAX_DEPLOY_SLOTS` | `3` | 매수 1건 금액 ≤ 투입 상한 ÷ 슬롯 수 — 첫 종목이 상한을 다 쓰지 않게 (예: 상한 50만 → 종목당 ≤16.7만). 이보다 비싼 종목은 `qty_zero` |
| `TRADING_DISABLED` | `false` | 킬 스위치 (재시작 필요) |
| `KILL_SWITCH_FILE` | `run/TRADING_DISABLED` | **파일이 존재하면 즉시 신규 매수 차단** (재시작 불필요, 청산은 계속) |
| `ALLOC_BREAKOUT_PCT` / `ALLOC_NEWS_SWING_PCT` | `20` / `0` | 전략별 현금 배분 % |
| `RECONCILE_MANAGED_ONLY` | `false` | `true` 면 봇이 다룬 종목만 잔고 대조 (수동 보유 종목이 있는 계좌) |
| `UNIVERSE_MODE` | `news_with_fallback` | 감시 종목. `static` = 하드코딩 watchlist 만 (**뉴스 기능 즉시 되돌리기**, 재시작) / `news` = 뉴스 후보 + 보유만 / `news_with_fallback` = 뉴스 후보가 `UNIVERSE_MIN_NEWS` 미만이면 watchlist 로 `UNIVERSE_MAX` 까지 채움 |
| `UNIVERSE_MAX` / `UNIVERSE_MIN_NEWS` | `30` / `10` | 감시 종목 상한(보유 제외) / 예비 목록을 채우는 기준 |
| `NEWS_MIN_SCORE` / `NEWS_BLOCK_SCORE` | `60` / `40` | 뉴스 후보 기준 / 감성 기사가 있고 이 점수 미만이면 당일 매수 금지 |
| `NEWS_MIN_CONFIDENCE` | `0.8` | 기사↔종목 매칭 확신도 하한 (D4) |
| `NEWS_POLL_MINUTES` / `NEWS_HALF_LIFE_HOURS` | `10` / `12` | 수집 간격(08:00~14:50) / 뉴스 점수 반감기 |
| `UNIVERSE_COMPARE_STATIC` | `true` | 섀도 비교 — watchlist 돌파 신호를 `RiskEvent(shadow_compare)` 로 기록, 일일 리포트에서 나란히 표시 |
| `BRIEFING_ENABLED` | `false` | 08:40 장전 뉴스 종목 브리핑 (Discord Embed, **정보용 — 매매 미반영**). 재시작 필요 |
| `ANTHROPIC_API_KEY` | 없음 | 브리핑 판단용 Claude API 키 (민감정보). 없으면 룰 기준으로 대체되고 footer 에 `룰 기준 (LLM 키 없음)` 표시 |
| `BRIEFING_MODEL` / `BRIEFING_COUNT` / `BRIEFING_CANDIDATES` | `claude-haiku-5-5` / `3` / `6` | 판단 모델 / 보낼 종목 수 / 모델에 넘길 후보 수 |
| `DART_API_KEY` | 없음 | 있으면 OpenDART 공시도 수집 (종목코드 직접 매핑) |

긴급 매수 중단: `touch $PROJECT_ROOT/run/TRADING_DISABLED` → 해제: `rm $PROJECT_ROOT/run/TRADING_DISABLED`

#### 1.2 실전 스모크 테스트 (`scripts/smoke_live.py`)

섀도 모드 기간 중 1회, 장중(영업일 09:00~15:20)에 실행. 결과는 화면 + `logs/smoke-*.json` (계좌번호·키 미기록).

```bash
.venv/bin/python scripts/smoke_live.py                       # 1) 읽기 전용 — 시세·종목상태·지수·잔고
.venv/bin/python scripts/smoke_live.py --order               # 2) 삼성전자 1주 현재가 −10% 지정가 매수 → 즉시 취소
.venv/bin/python scripts/smoke_live.py --status-symbols 코드1,코드2   # 투자경고·VI 발동 종목 상태 원값 확인
.venv/bin/python scripts/smoke_live.py --rate-test           # 초당 한도 (데몬 tick 시각 :x0·:x5 분은 피할 것)
```

| ID | 확인 | FAIL 시 |
|---|---|---|
| R1 | `hts_avls` 단위가 억원 | `get_quote` 환산 계수 수정 전까지 유동성 게이트 신뢰 불가 |
| R2 | 종목상태 원값 (`vi_cls_code`·`mrkt_warn_cls_code` 등) | 발동 종목 원값으로 `get_quote` 판정식 확인 |
| O2 | 체결조회 `ord_gno_brno` = 주문 응답 `KRX_FWDG_ORD_ORGNO` | 응답 유실 주문의 자동 취소 불가 — 수정 전 실주문 보류 |
| O3 | 취소 후 원주문 행 `rmn_qty`·`cncl_yn` | 수동취소 감지 가정(`cncl_yn=='Y'`) 재검토 |

- `--order` 는 `SHADOW_MODE: true` 일 때만 동작한다 (데몬 실주문과 겹침 방지). OrderManager·DB 를 거치지 않는다.
- 취소 실패(`O3 FAIL 취소 실패`) 또는 체결(`체결됨`)이 나오면 HTS/MTS 에서 해당 주문을 직접 취소·매도한다.

---

### 2. 정상 가동 상태 체크리스트

매 영업일 다음 알림이 Discord 에 **시간 순서대로** 들어와야 한다.

| 시각 | 알림 | 의미 |
|------|------|------|
| 07:30 | (로그) `master 2026MMDD vs …` | 종목 마스터 갱신 — 실패 시에만 Discord `[master_refresh] 종목 마스터 없음` |
| 08:00~14:50 | (로그) `news_collect: fetched …` · `universe HH:MM: …` | 10분마다 뉴스 수집 → 감시 종목 재구성 |
| 09:00 | `[prepare]` (감시 종목: 뉴스 n · 예비 n · 보유 n) | 시작 자산 고정 · 잔고 정합성 · 감시 종목 구성 |
| 09:10 | `trading loop started` | 메인 매매 루프 가동 |
| 16:00 | `daily report` | 일일 리포트 수신 |

- **알림이 연속 2회 누락되면 장애로 간주**하고 즉시 시나리오 3.7 을 확인한다.
- 주말·공휴일엔 알림이 없는 것이 정상이다.

---

### 3. 시나리오별 플레이북

#### 3.1 ⚠️ KIS 토큰 만료 / 재발급 실패

- **징후**: Discord 에 `token fetch failed` 또는 HTTP 401 응답이 빈발.
- **즉시 조치**:
  1. `sudo systemctl stop mini-trading` 실행 (또는 `pkill -f "src.main"`).
  2. 추가 401 로그가 없는지 확인.
- **복구**:
  1. `KIS{YYYYMMDD}` 캐시 파일 삭제 (`rm $PROJECT_ROOT/KIS$(date +%Y%m%d)`).
  2. `sudo systemctl start mini-trading` 로 재시작.
  3. Discord 에 `token issued` 알림이 오는지 확인.
- **사후 점검**:
  - `config.yaml` 의 `APP_KEY` / `APP_SECRET` 유효기간 확인.
  - KIS OpenAPI 포털 로그인해 키 상태 점검.
  - `expired/` 폴더 누적 파일 정리.

---

#### 3.2 ⚠️ 네트워크 단절 / DNS 실패

- **징후**: `connection timeout`, `Name or service not known` 로그 다수.
- **즉시 조치**: 별도 쿨다운 없음 — 실패한 tick 은 건너뛰고 **다음 5분 tick 에서 재시도**한다.
  오류 알림은 30분에 1회로 제한된다. 주문 송신 중 타임아웃은 `unknown` 으로 기록되고,
  복구 후 체결조회로 실제 접수 여부를 찾아 연결한다 (중복 송신 없음).
- **복구**:
  1. 장시간 단절이 예상되면 `touch $PROJECT_ROOT/run/TRADING_DISABLED` 로 신규 매수 차단.
  2. 네트워크 복구 확인(`ping openapi.koreainvestment.com`) 후 `rm $PROJECT_ROOT/run/TRADING_DISABLED`.
  3. 다음 09:00 Reconcile 결과(Discord `[prepare]` 메시지) 확인.
- **사후 점검**:
  - ISP / 라우터 / DNS 서버 로그 확인.
  - 장시간 단절 구간의 미실행 주문 영향도 기록.

#### 3.2.1 노트북 절전·최대 절전 / 이동 중 실행 중단 (10-27 사례)

- **징후**: Discord `⏸ 실행 중단 감지 07:15~08:32 (1h17m) · 복구됨`, 일일 리포트 `■ 운영 — 실행 중단: …`.
  로그에 `was missed by` 경고 (5분 넘게 늦은 cron job 은 건너뜀).
- **자동 복구** (조치 불필요):
  - 09:00 prepare 를 놓치면 다음 tick 이 prepare 를 다시 실행한다 (`tick before prepare — preparing now`).
  - 07:30 마스터 갱신을 놓치면 뉴스 수집·universe 빌드 때 오늘자 마스터를 받는다 (실패 시 30분 간격 재시도, 그동안 전일 보관본 사용).
  - 깨어난 직후 네트워크가 없어 중단 알림이 실패하면 다음 heartbeat(1분)에 재전송.
- **주의**: 복귀 직후 첫 tick 에 이미 돌파한 종목이 한꺼번에 신호를 낼 수 있다 (10-27 10:38 — 16종목).
  중단 구간은 `RiskEvent(event_type='runtime_gap')` 으로 남으니 B1·B2 지표에서 제외한다.
- **예방**: 장중(08:30~15:30)에는 절전 해제·전원 연결.

---

#### 3.3 🛑 Reconciler 불일치 (KIS 잔고 ≠ DB)

- **징후**: `reconcile_mismatch` RiskEvent, 매매 자동 OFF.
- **즉시 조치**: **KIS MTS 앱**에서 실제 잔고를 직접 확인 (육안 검증).
- **복구**:
  1. DB `positions` 테이블을 KIS 실제 잔고와 수동 일치.
  2. `sudo systemctl restart mini-trading` — 재기동 시 prepare 가 Reconcile 을 다시 수행한다
     (수동 보유 종목이 원인이면 `RECONCILE_MANAGED_ONLY: true` 검토).
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
- `ANTHROPIC_API_KEY`(브리핑, 선택): 노출 의심 시 콘솔에서 폐기·재발급. 로그·Discord 에서는 `sk-ant-` 패턴으로 가려진다.
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
