# 세션 핸드오프 — 다음 세션에서 이어 작업하기

> **작성일**: 2026-04-21
> **용도**: Claude Code 새 세션에서 현재 상태를 빠르게 파악하고 **우선순위 1 작업부터 재개**하기 위한 1페이지 가이드.
> **전제**: Claude Code 는 세션 시작 시 `CLAUDE.md` 를 자동 로드 — 본 문서는 그 위에 "**지금 뭘 해야 하는가**" 를 얹는다.

---

## 1. 현 상태 스냅샷

| 항목 | 값 |
|---|---|
| 완료 주차 | **W1 ~ W8 + ops + v3 + v3.1** (12개 커밋) |
| 테스트 | **477/477 pass** |
| 최신 커밋 | `e5af127` — v3.1 Phase 3 handler 실구현 |
| 브랜치 | `main` (origin/main 추적) |
| Phase | Phase 3 실주행 직전 — 4개 stub handler 실구현만 남음 |

### 검증 방법 (새 세션 시작 직후 실행)
```bash
cd /home/mycom/workspace/mini_project
python3 -m pytest tests/ 2>&1 | tail -3              # 477 passed
python3 -m src.main --dry-run 2>&1 | tail -3         # 7 jobs registered
git log --oneline -5                                 # 12개 커밋 확인
```

---

## 2. 즉시 착수할 작업 — 4개 stub handler 실구현

`src/main.py` 의 `_make_job_handlers()` 에 남은 stub 들. 우선순위 순서:

### 🥇 1순위: `news_collect` handler (~0.5일)
**왜 먼저?** 데이터가 쌓여야 리포트·스코어링 의미 있음.

**구현 요점**:
```python
def news_collect():
    from src.ingestion.scrapers.dart import DartScraper
    from src.ingestion.scrapers.yonhap_rss import YonhapRssScraper
    from src.ingestion.dedup import normalize_url, url_hash, simhash64, to_signed64
    from src.persistence.models import NewsArticle, ArticleSymbol
    # ...
    with session_scope() as session:
        # 1. 각 스크래퍼 .fetch(since=yesterday) 순회
        # 2. 기존 DB 에 url_hash 중복 체크 → skip
        # 3. SymbolResolver 로 symbols 추출 (ingestion/symbol_resolver.py)
        # 4. NewsArticle + ArticleSymbol INSERT
```
**의존성**: `DART_API_KEY` 가 `.env` 또는 `config.yaml` 에 설정되어 있어야 DART 스크래퍼 가동.
**테스트**: `tests/main/test_handlers.py` 신설, 모의 스크래퍼로 DB INSERT 검증.

### 🥈 2순위: `analyze_score` handler (~1일)
**구현 요점**:
- 최근 24시간 뉴스 → `compute_news_score` (symbol 별 집계)
- KIS 일봉 조회 → `compute_technical_score` (RSI/MA/MACD)
- DART 재무 → `compute_financial_score` (수집 단계가 있으면)
- 외인·기관 순매수 → `compute_flow_score` (데이터 소스 확인 필요)
- `scorer.compose()` → `to_stock_score_row()` → `StockScore` INSERT
**복잡도**: 중간. 팩터 입력 데이터 중 재무·플로우가 누락될 수 있음 → `insufficient_data` 플래그 활용.

### 🥉 3순위: `prepare_state` + `trading_tick` handler (~1~2일)
**prepare_state (09:00)**:
- `reconciler.verify_with_grace()` 실행 → 실패 시 `pipeline.set_trading_off(True)`
- `DailyLossState.start_equity` 를 KIS 조회값으로 고정 (D7)

**trading_tick (09:10~15:20, 5분)**:
- `TradingPipeline` 생성 (Registry + RiskEngine + OrderManager + LivePortfolio + Reconciler)
- `TickContext` 구성: 현재가 조회, 스코어 조회, 캐시 잔고 조회
- `pipeline.run_tick(ctx)` 호출
- **가장 복잡한 handler** — RiskEngine 을 ContextVar 에 설치(`set_risk_engine`) 하는 것도 여기서

### 4순위: `master_refresh` handler (~0.5일)
- `https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip` 다운로드
- ZIP 해제 → `kospi_code.mst` / `kosdaq_code.mst` 갱신
- 이전 파일과 diff → RiskEvent 또는 로그로 편입/폐지 기록
- `SymbolResolver.from_mst_files()` 호환 포맷 확인

---

## 3. 선행 조건 (handler 구현 전 확인)

### 3.1 config.yaml / .env 확인
```bash
cat config.yaml                      # APP_KEY/APP_SECRET/CANO/ACNT_PRDT_CD/URL_BASE/DISCORD_WEBHOOK_URL
# 추가 설정 필요:
#   DART_API_KEY: xxx      (news_collect 선결)
#   KIS_ENV: PAPER          (D11)
#   ARCHIVE_ROOT: /home/mycom/workspace/mini_project/reports  (v3.1 추가, 기본값 있음)
```

### 3.2 KIS 관련 확인 (사용자 직접)
| 항목 | 방법 |
|---|---|
| PAPER 계좌 자금 시드 | KIS 앱 → 모의투자 → 초기 1천만원 이상 설정 |
| KIS 지수 조회 TR_ID | `https://apiportal.koreainvestment.com/` 에서 KOSPI/KOSDAQ 지수 API 확인 |
| KIS 뉴스 TR_ID | `kis_news.py` 스캐폴드 완성 시점 — 우선순위 낮음 |

### 3.3 DB 초기화 (최초 1회)
```bash
python3 -m src.main --init-db
# → trading.db 생성, 8개 테이블
```

---

## 4. 작업 착수 전 체크리스트 (새 세션)

```
[ ] CLAUDE.md 로드 확인 (Claude 자동)
[ ] git log --oneline -5 → 12커밋, 최신 e5af127
[ ] python3 -m pytest tests/ → 477 passed
[ ] python3 -m src.main --dry-run → 7 jobs registered
[ ] docs/session-handoff.md (본 문서) 읽기
[ ] config.yaml 확인 (DART_API_KEY 필요시 추가)
[ ] 어느 handler 부터 착수할지 결정 (권장: news_collect)
[ ] TodoWrite 로 단계별 태스크 세팅
```

---

## 5. 핵심 설계 결정 (D1~D14) 빠른 참조

가장 자주 충돌하는 규칙들 — 새 코드 작성 시 반드시 점검:

| # | 규칙 | 대표 위치 |
|---|---|---|
| **D2** | `strategy_id` 를 positions·trades 에 전파 — 절대 symbol 단독 PK 사용 금지 | `persistence/models.py` Position 복합 PK |
| **D5** | 뉴스 `published_at + 60초` 이후에만 신호 유효 | `backtest/data_loader.py` news() 필터 |
| **D8** | `Trade.idempotency_key` UNIQUE — 중복 매매 차단 | `strategy/registry.py::build_idempotency_key` |
| **D14** | `@risk_gate` 데코레이터 우회 불가 — 모든 매매 경로에 적용 | `risk/limits.py::set_risk_engine` |
| **§4.2** | **DB 스키마 변경 금지** — 필드 필요 시 기존 테이블 재사용·FIFO 계산 우회 | `reporting/pnl_calc.py` 선례 참조 |

---

## 6. 자주 쓰는 명령

```bash
# 테스트
python3 -m pytest tests/ -q                          # 전체 477
python3 -m pytest tests/reporting/ -v                # 특정 계층
python3 -m pytest tests/reporting/ --cov=src/reporting --cov-report=term-missing

# 실행
python3 -m src.main --init-db                        # DB 생성
python3 -m src.main --dry-run                        # 스케줄러 검증만 (실행 X)
python3 -m src.main                                  # 실제 가동 (Ctrl+C = graceful)

# Git
git log --oneline -15
git status
git diff --stat

# 리포트 조회 (JSON 파일)
ls reports/$(date +%Y)/                              # 아카이브된 리포트
```

---

## 7. 운영 플레이북 (장애 시)

`docs/runbook.md` 의 7 시나리오 참조:
1. KIS 토큰 만료 · 재발급 실패
2. 네트워크 단절 / DNS
3. Reconciler 불일치 → 매매 OFF
4. Circuit Breaker 발동
5. Daily Loss Limit 도달
6. 주문 미체결 / Rate limit
7. 프로세스 비정상 종료

---

## 8. "다음 세션 첫 대화" 권장 프롬프트

```
docs/session-handoff.md 를 읽고 현재 상태 확인 후,
우선순위 1순위 news_collect handler 실구현부터 착수해줘.
중간 보고와 완료 보고 해줘.
```

또는 사용자가 직접 4개 중 선택:
```
우선순위 2순위 analyze_score handler 부터 시작해줘.
```

---

## 9. 파일 위치 치트시트

| 목적 | 경로 |
|---|---|
| 메인 엔트리포인트 | `src/main.py` |
| Handler stubs 정의 | `src/main.py::_make_job_handlers` |
| 스케줄 타임라인 | `src/scheduler/app.py::register_default_jobs` |
| 리포트 빌더 (DB 조립) | `src/reporting/builders.py` |
| 뉴스 스크래퍼 | `src/ingestion/scrapers/{dart,yonhap_rss}.py` |
| 팩터 스코어러 | `src/analysis/{technical,financial,flow,news}.py` |
| 전략 레지스트리 | `src/strategy/registry.py` |
| 리스크 엔진 | `src/risk/limits.py::RiskEngine` |
| 실행 파이프라인 | `src/execution/pipeline.py` |
| 주문 관리 | `src/execution/order_manager.py` |
| 포지션 대조 | `src/execution/reconciler.py` |
| KIS 클라이언트 | `src/clients/kis.py` |
| 설정 | `src/config.py`, `config.yaml`, `.env` |
| DB 세션 | `src/persistence/db.py::session_scope` |

---

## 10. 남은 전체 로드맵

```
[지금] Phase 3 실주행 직전
   │
   ├─ news_collect + analyze_score (데이터 파이프라인 활성화)  ← 다음 세션 1~2일차
   │
   ├─ prepare_state + trading_tick (매매 루프 활성화)           ← 3~4일차
   │
   ├─ master_refresh (마스터 갱신)                               ← 4일차
   │
   ├─ KIS 지수 조회 추가 (MarketSummary 채움)                    ← 5일차
   │
   ├─ Dry-run 3영업일 (PAPER 환경, 실 API 호출)                  ← 1주차
   │
   └─ Phase 3 Paper 관찰 4주 (실전 괴리 < 20% 게이트)            ← 2~5주차
        ↓
        Phase 4 실자본 5% (systemd 자동 재시작 필수)
        Phase 5 점진 확대 (최대 50%)
```

---

**문서 끝.** 새 세션에서 이 파일을 먼저 읽고 CLAUDE.md 와 대조해 작업 시작하세요.
