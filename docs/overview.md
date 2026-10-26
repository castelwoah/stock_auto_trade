# 코드 흐름 한눈에 보기

> 2026-10-26 기준. 세부 결정은 `CLAUDE.md`, 장애 대응은 `docs/runbook.md`, 설치·스케줄은 `docs/scheduling.md`.

## 1. 한 문장 요약

**뉴스로 "오늘 볼 종목"을 고르고, 그 종목이 변동성 돌파 목표가를 넘으면 리스크 검사를 거쳐 지정가로 매수, 당일 청산한다.**
지금은 섀도 모드라 주문은 보내지 않고 DB에 기록만 한다.

## 2. 하루 타임라인 (영업일, KST)

| 시각 | job | 하는 일 | 코드 |
|---|---|---|---|
| 07:00 | (systemd) | 데몬 기동 → 디스코드 `scheduler started` | `ops/mini-trading-start.timer` → `src/main.py` |
| 07:30 | `master_refresh` | KRX 종목 마스터 다운로드 → 보통주 목록 | `ingestion/master.py` |
| 08:00~14:50, 10분마다 | `news_collect` | 뉴스 수집 → 중복 제거 → 종목 매칭 → 감시 종목 재구성 | `ingestion/service.py`, `collector.py`, `strategy/universe.py` |
| 08:50 | `analyze_score` | 장 시작 전 감시 종목 확정 | `strategy/universe.py` |
| 09:00 | `prepare_state` | 잔고 대조(Reconcile), 시작 평가액·투입 상한 고정, 전일 시세 로드 → 디스코드 `[prepare]` | `runtime.py: prepare()` |
| 09:10~15:20, 5분마다 | `trading_tick` | 시세 조회 → 전략 신호 → 리스크 검사 → 주문 | `runtime.py: tick()` → `execution/pipeline.py` |
| 12:00 | `midday_report` | 오전 스냅샷 | `reporting/midday_report.py` |
| 15:00 / 15:15 | (tick 안에서) | 신규 매수 중단 / 보유 전량 청산 | `strategy/breakout.py` |
| 16:00 | `daily_report` | 일일 리포트 + 아카이브(JSON·CSV). 지수는 이때 다시 조회(종가) | `reporting/daily_report.py`, `archiver.py` |
| 17:00 | (systemd) | 데몬 정지 | `ops/mini-trading-stop.timer` |

휴장일은 `scheduler/calendar.py`가 걸러낸다. 모든 job은 예외를 삼키고 디스코드로 알린다(한 job 실패가 데몬을 멈추지 않음).

## 3. 전체 구조

```
           ┌───────────── main.py (배선 + 스케줄러) ─────────────┐
           │                                                     │
 뉴스 쪽   │ NewsService                      TradingRuntime     │  매매 쪽
           │  master → collect → universe ──▶ (감시 종목)        │
           │                                   prepare / tick    │
           │                                       │             │
           │                              TradingPipeline        │
           │                   Registry(전략) → RiskEngine → OrderManager → KIS
           │                                       │             │
           │                         DB(trading.db) ◀── 모든 기록 │
           │                                       │             │
           │                        Reporting → Discord           │
           └─────────────────────────────────────────────────────┘
```

## 4. 뉴스 → 감시 종목 (`src/ingestion`, `src/strategy/universe.py`)

1. **수집**: 연합뉴스 RSS(+ DART 키가 있으면 공시). 제목·요약만 저장.
2. **중복 제거**: 기사 ID · URL · 내용 유사도(SimHash).
3. **종목 매칭**: 기사 속 종목명/코드 → 종목코드. 확신도 0.8 미만은 매매에 안 씀.
4. **점수**: 룰 기반 감성(긍정·부정 키워드) × 매체 가중 × 시간 감쇠(반감기 12h) → 0~100.
5. **감시 종목 구성** (`UNIVERSE_MODE=news_with_fallback`):
   - 보유 종목 + 뉴스 점수 60↑ 상위 30
   - 뉴스 후보가 10개 미만이면 하드코딩 watchlist(29종목)로 채움
   - 점수 40 미만(악재) 종목은 당일 매수 금지
6. 점수는 `stock_scores` 테이블에 기록.

> 뉴스는 **무엇을 볼지**와 **매수 순서**만 정한다. 살지 말지는 아래 돌파 조건 + 리스크가 정한다.

## 5. 매매 한 tick (`runtime.tick()` → `pipeline.run_tick()`)

```
시세·종목상태·지수 조회 ─▶ 잔고 조회(실패 시 직전값, 매수만 차단)
        │                    └ 섀도 + SHADOW_CAPITAL_KRW 면 가상 자본으로 대체
        ▼
미체결 주문 처리 (체결 반영 · 3분 미체결 시 취소 후 재주문 · 15:15 일괄 취소)
        ▼
전략 신호 (매도 먼저, 매수 나중)
  breakout: 목표가 = 오늘 시가 + (전일 고가 − 전일 저가) × 0.5
            현재가 ≥ 목표가 → 매수 (최대 3종목, 빈 자리는 뉴스 점수 → 돌파율 순)
  청산: 손절 −5% / +7% 후 고점 대비 −3% / 이벤트(정지·관리종목 등) / 15:15 전량
        ▼
투입 상한으로 금액 축소 (09:00 평가액 × 5%, 미체결·섀도 매수 포함)
        ▼
RiskEngine.check_buy  (막히면 risk_blocked 기록)
  CB → 일일손실 −3% → KOSPI −3% → 블랙리스트 → 유동성 → 종목상태(VI·갭5%·±25%)
  → 쿨다운 → 종목 20%/섹터 40% → 투입 상한
        ▼
수량 계산 (1주 미만이면 qty_zero 기록)
        ▼
OrderManager.submit (@risk_gate 경유 · idempotency_key 로 중복 송신 차단)
  섀도: Trade(status=shadow) 기록만 / 실주문: KIS 지정가 송신
        ▼
체결 확정 시 디스코드 🟢 BUY / 🔴·🔵 SELL (매도는 비용 반영 손익)
```

리포트(12:00·16:00)는 `runtime.report_state()` 로 마지막 tick 의 지수·평가액(가상 표시)·Daily Loss·CB 를 받는다.

## 6. 안전장치 요약

| 장치 | 동작 | 설정 |
|---|---|---|
| 섀도 모드 | 주문 송신 안 함 | `SHADOW_MODE` (기본 true) |
| 가상 자본 | 섀도에서 금액 계산 기준 | `SHADOW_CAPITAL_KRW` |
| 투입 상한 | 전체 매수 ≤ 평가액 × % | `MAX_DEPLOY_PCT` (5) |
| 전략 배분 | breakout 20%, news_swing 0% | `ALLOC_*_PCT` |
| 킬 스위치 | 매수만 차단, 청산은 계속 | `touch run/TRADING_DISABLED` |
| Reconcile | 09:00 실잔고 ≠ DB 이면 당일 매매 OFF | `RECONCILE_MANAGED_ONLY` |
| 매도 우선 | 리스크 상태가 나빠도 청산은 허용 | (코드 불변식) |
| 민감정보 가림 | 로그·디스코드에서 키·계좌·웹훅 마스킹 | `common/redact.py` |
| 조회 재시도 | GET 만 5xx·연결오류 재시도, 주문(POST)은 재시도 안 함 | `clients/kis.py` |

## 7. 폴더별 역할

| 폴더 | 역할 | 핵심 파일 |
|---|---|---|
| `src/clients` | 외부 연결: KIS API·토큰·호출 속도 제한, 디스코드 | `kis.py`, `discord.py` |
| `src/ingestion` | 마스터·뉴스 수집, 중복 제거, 종목 매칭 | `service.py`, `collector.py`, `symbol_resolver.py` |
| `src/analysis` | 감성·뉴스 점수 (기술·재무·수급 모듈은 있으나 실운용 미사용) | `sentiment.py`, `news.py` |
| `src/strategy` | 감시 종목, 전략(breakout / news_swing), 청산 규칙, 전략 등록·현금 배분 | `universe.py`, `breakout.py`, `exit_signals.py`, `registry.py` |
| `src/risk` | 매수 9단계 검사, 사이징, 블랙리스트, 시장 상태 | `limits.py`, `sizing.py` |
| `src/execution` | tick 파이프라인, 주문 생명주기, 잔고 대조, DB 포지션 | `pipeline.py`, `order_manager.py`, `reconciler.py`, `portfolio.py` |
| `src/reporting` | 미드데이·일일 리포트, 체결 Embed, 아카이브 | `builders.py`, `daily_report.py`, `midday_report.py` |
| `src/scheduler` | 스케줄 등록, 휴장일, 예비 watchlist | `app.py`, `calendar.py`, `watchlist.py` |
| `src/persistence` | DB 8테이블 (뉴스·점수·거래·포지션·리스크이벤트 등) | `models.py` |
| `src/backtest` | 백테스트 엔진·지표 (실운용과 별개) | `engine.py` |
| `src/runtime.py` | 하루 상태 보관 + prepare/tick 본체 | |
| `src/main.py` | 설정 로드 → 객체 생성 → job 등록 → 실행 | |

## 8. DB에서 보는 법 (`trading.db`)

| 테이블 | 무엇 | 볼 때 |
|---|---|---|
| `trades` | 주문 기록 (`status`: shadow / submitted / filled / rejected / cancelled_*) | 무엇을 사려 했나 |
| `positions` | 봇 보유 포지션 (전략별, 고점 포함) | 지금 무엇을 들고 있나 |
| `risk_events` | `risk_blocked`(사유 코드) · `reconcile_mismatch` · `shadow_compare` | 왜 안 샀나 |
| `news_articles` / `article_symbols` | 수집 기사 / 기사↔종목 | 어떤 뉴스가 들어왔나 |
| `stock_scores` | 종목별 뉴스 점수 (`news-universe-v1`) | 왜 감시 종목이 됐나 |

## 9. 현재 한계 (알고 쓰기)

- 섀도 매수는 포지션을 만들지 않음 → 섀도로는 **매도 경로 검증 불가**.
- 리포트의 누적 수익률·MDD 는 `-` (일별 손익 `DailyPnl` 기록 전).
- 체결 알림은 실주문에서만 나간다 (섀도는 체결이 없음 → 리포트로 확인).
- 감성 분석이 키워드 룰이라 후보가 적고, 키워드 1개로 악재 차단될 수 있음 (B1 측정 중).
- Circuit Breaker 의 연속 손실일 입력(`DailyPnl` 기록) 미구현.
