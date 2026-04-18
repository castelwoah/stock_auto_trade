# CLAUDE.md

## Project Overview

한국투자증권(KIS) Open API를 활용한 **국내 주식 자동매매 프로그램**.
변동성 돌파 전략(Volatility Breakout Strategy)을 기반으로 자동 매수/매도를 수행한다.

## Tech Stack

- **Language**: Python 3.10 (dev) / **3.11+ 목표** (rules)
- **Dependencies**: `requirements.txt` — requests, PyYAML, pydantic, pydantic-settings (pandas는 legacy만 사용)
- **External API**: 한국투자증권 Open Trading API (`openapi.koreainvestment.com:9443`)
- **Notification**: Discord Webhook

## Project Structure

```
mini_project/
├── stock_auto_trading.py   # 메인 자동매매 스크립트 (전체 로직)
├── config.yaml             # API 키, 계좌번호, Discord Webhook URL 등 설정 (민감정보 포함, git 제외)
├── token.dat               # 토큰 저장 파일 (git 제외)
├── KIS{YYYYMMDD}           # 일별 토큰 캐시 파일 (당일 유효, git 제외)
├── expired/                # 만료된 토큰 파일 보관 폴더 (git 제외)
├── kospi_code.mst(.zip)    # KOSPI 종목코드 마스터 파일 (git 제외)
├── kosdaq_code.mst(.zip)   # KOSDAQ 종목코드 마스터 파일 (git 제외)
├── auto_trading_study.ipynb # 학습/실험용 Jupyter 노트북
├── requirements.txt        # Python 의존성 (W1 신설)
├── .gitignore              # 민감 파일·빌드 산출물 제외 규칙
├── src/                    # W1+ 신규 구조 (legacy 이관 중)
│   ├── config.py           # pydantic Settings, ENV>.env>YAML>default 우선순위
│   └── clients/
│       ├── rate_limiter.py # Token Bucket (기본 20 rps)
│       ├── discord.py      # DiscordNotifier (webhook)
│       └── kis.py          # KisClient + TokenStore (일별 파일 캐시, expired 자동 이동)
└── .claude/
    └── project-rules.md    # 자동매매 에이전트 시스템 프롬프트 v2.0 (차기 설계 방향성)
```

## Core Trading Logic (`stock_auto_trading.py`)

### 매매 전략
- **변동성 돌파 전략**: 목표가 = 오늘 시가 + (전일 고가 - 전일 저가) * 0.5
- 목표가를 돌파한 종목을 시장가로 매수
- 최대 3종목 분산 투자, 각 종목당 현금의 33% 비중

### 시간대별 동작
| 시간 | 동작 |
|------|------|
| 09:00~09:05 | 전일 보유 주식 전량 매도 |
| 09:05~15:15 | 매수/매도 전략 실행 (메인 루프) |
| 15:15~15:20 | 보유 주식 전량 매도 후 종료 |
| 주말 | 자동 종료 |

### 주요 함수
- `get_access_token()` — KIS API 토큰 발급/캐싱
- `get_target_price(code)` — 변동성 돌파 목표가 계산
- `get_current_price(code)` — 현재가 조회
- `get_stock_balance()` — 보유 주식 잔고 조회
- `get_balance()` — 주문 가능 현금 잔고 조회
- `buy(code, qty)` / `sell(code, qty)` — 시장가 매수/매도
- `send_message(msg)` — Discord 알림 전송
- `get_prev_day_high_low(code)` — 전일 고가/저가/시가 조회

### 자동 익절
- 매입가 대비 **7% 이상 상승** 시 자동 전량 매도

### 토큰 관리
- 일별 파일(`KIS{YYYYMMDD}`)로 토큰 캐싱
- 만료된 토큰 파일은 `expired/` 폴더로 자동 이동

## Configuration (`config.yaml`)

> **주의**: 이 파일에는 API 키, 계좌번호, Discord Webhook URL 등 민감 정보가 포함되어 있음. 절대 공개 저장소에 커밋하지 말 것.

필수 설정 항목:
- `APP_KEY` / `APP_SECRET` — KIS API 인증 키
- `CANO` / `ACNT_PRDT_CD` — 계좌번호 (앞 8자리 / 뒤 2자리)
- `URL_BASE` — API 엔드포인트 (실전/모의투자 전환 가능)
- `DISCORD_WEBHOOK_URL` — 디스코드 알림 웹훅

## 매매 대상 종목 (`symbol_dict`)

종목 리스트는 `stock_auto_trading.py` 내 `symbol_dict` 딕셔너리에 하드코딩되어 있음.
현재: 삼성전자, 카카오, SK하이닉스, 세종텔레콤, 하나머티리얼즈, 롯데에너지머티리얼즈, 현대차증권, HD현대, 바이오비쥬, SFA반도체, 두산테스나, 코칩

## Running

```bash
python stock_auto_trading.py
```

## 목표 아키텍처 v2 (최종 설계안, 2026-04-18 확정)

> 세부 규칙은 `.claude/project-rules.md` v2.0이 **정본(normative)**.
> 이 섹션은 본 프로젝트에 맞게 **확정된 구현 결정**만 요약한다.

### A. 시스템 계층
```
Ingestion → Analysis → Strategy → Risk → Execution → Reporting
   ↓           ↓          ↓        ↓         ↓           ↓
 News/DART  Sentiment  Selection Limits    KIS Client  Discord
 공개 RSS   Financial  Entry    Blacklist  +Token Mgr  +Daily
 KIS News   Technical  Exit     Sizing     +RateLimit  Report
            Flow       Registry Market St. +Reconciler Archive
```

### B. 디렉터리 목표 구조
```
src/
├── config.py                # pydantic settings, ENV>YAML 우선순위
├── clients/                 # kis.py, rate_limiter.py(Token Bucket 20rps), discord.py
├── ingestion/               # base_scraper, scrapers/{dart, yonhap_rss, naver_disclosure, kis_news}
│   ├── symbol_resolver.py   # kospi/kosdaq_code.mst 활용 + ambiguity<0.8 차단
│   └── dedup.py             # SimHash + URL 정규화
├── analysis/
│   ├── sentiment.py         # 1차 룰(90%) → 2차 KoELECTRA(경계값) → 3차 LLM(옵션)
│   ├── financial.py         # DART 재무
│   ├── technical.py         # RSI14, MA5/20/60, MACD
│   ├── flow.py              # 외인·기관 순매수, 거래량
│   └── scorer.py            # 감성30/재무25/기술25/수급20, 70점 이상 후보
├── strategy/
│   ├── registry.py          # strategy_id 라우팅
│   ├── news_swing.py        # 신규 주력 (2-5일 홀딩)
│   └── breakout.py          # 기존 로직 포팅, 서브포트폴리오 격리(현금 20% 고정)
├── risk/
│   ├── blacklist.py         # 관리/경고/정지/SPAC/ETN/시총<500억/거래대금<10억
│   ├── market_state.py      # KOSPI -3%, VI, 상/하한가
│   ├── limits.py            # Daily Loss -3%, Circuit Breaker, 쿨다운 3/10일 (@risk_gate 데코레이터)
│   └── sizing.py            # 종목 20%/섹터 40% 상한, Kelly/4 or 균등
├── execution/
│   ├── order_manager.py     # 지정가 IOC 3분 → 1호가 상향 → 2회 후 취소
│   ├── reconciler.py        # KIS 잔고 × DB 교차검증, 불일치 시 매매 OFF
│   └── portfolio.py
├── persistence/             # SQLite dev / PostgreSQL prod, Alembic migrations
└── reporting/               # daily_report, intraday, archiver
```

### C. 매도 4중 방어
1. **손절 SL**: 진입가 **-5%** → 즉시 지정가 IOC (하드 스톱)
2. **익절 TP**: 진입가 **+7%** → 청산 or Trailing 모드 전환
3. **Trailing Stop**: TP 후 고점 대비 **-3%**
4. **이벤트 청산**: 거래정지·관리종목 편입·VI 연속·스코어<50 급락·Daily Loss -3% 도달

### D. 확정된 핵심 결정 (project-rules 대비 본 프로젝트 특화)
| # | 결정 |
|---|---|
| D1 | **수집 매체 MVP 제한**: DART + 연합 RSS + 네이버 공시 + KIS 뉴스 API만. 한경·매경·서경·이데일리는 **계약 체결 후** 단계 편입 (rules §12 ToS 준수) |
| D2 | **전략 격리**: `strategy_id`를 positions·trades에 전파. breakout은 당일청산, news_swing은 스윙. 포트폴리오 서브 할당 분리 |
| D3 | **Trailing 상태 DB 영속화**: `positions.highest_price` 이벤트 드리븐 WRITE (프로세스 재시작 복원) |
| D4 | **종목 매핑 ambiguity 차단**: 회사명 exact + 종목코드 본문 + DART corp_code 매칭 점수 < 0.8이면 매매 신호 무효, 리포트만 |
| D5 | **백테스트 look-ahead 방지**: 뉴스 `published_at + 60초` 이후에만 신호 유효 |
| D6 | **포지션 정본**: 세션 시작 시 `reconciler`가 KIS × DB 교차검증 실패 시 매매 전면 OFF + 수동 확인 알림 |
| D7 | **Daily Loss 기준**: `start_equity` = 당일 09:00 조회값 고정, 입출금은 별도 `cash_flow` 테이블 |
| D8 | **동시성**: 종목 단위 `asyncio.Lock` + `trades.idempotency_key = (date, symbol, strategy_ver, signal_id)` UNIQUE |
| D9 | **Master 갱신**: 매 영업일 07:30 `kospi/kosdaq_code.mst` 재다운로드 + diff 로그 |
| D10 | **Config 우선순위**: `ENV > .env > config.yaml > default`, 기동 시 pydantic validator로 필수 키 검증 실패 즉시 abort |
| D11 | **PAPER/LIVE 어댑터**: `KisEnv` enum으로 URL + TR_ID 매핑, 환경변수 + 재기동으로 전환, 코드 수정 無 |
| D12 | **스코어 재현성**: `stock_scores`에 `scoring_ver + sentiment_model_ver + llm_prompt_hash` 모두 기록 |
| D13 | **주말·이벤트 게이트**: 금요일 14:50 보유 종목별 실적·FOMC·리밸런싱 캘린더 점검 → Discord 승인형 알림(Phase 4까지 수동 승인) |
| D14 | **리스크 우회 불가 코드 경로**: `@risk_gate` 데코레이터로 `buy/sell` 전부 감싸서 Daily Loss/Circuit Breaker 통과 못하면 호출 자체가 실패 |

### E. 기존 코드 수정 대상 (리팩토링 시)
- 평단가 분기 L575-582: `stock_dict`가 `dict[str, int]`라 dict 형 체크가 항상 False → `buy_prices` dict 일원화
- `buy_amount`(L541,608): 시작값 고정을 **매 사이클 현금 재조회**로 교체
- 최상위 `try/except`(L525,640): 각 태스크 격리 + 지수 백오프 재시도
- 시장가 주문(`ORD_DVSN="01"`): 지정가 + IOC/FOK로 전환
- 하드코딩 `symbol_dict` / 토큰 경로(`~/workspace/mini_project`): 설정 주입

### F. 롤아웃 주차별 산출물 (Phase 1~3)
| 주 | 산출물 | 게이트 |
|---|---|---|
| W1 | `src/` 골격 + config + KIS client + rate limiter + Discord 이관 | legacy 동작 동일성 검증 |
| W2 | DART + 연합 RSS 수집 + DB 스키마 + dedup | 7일간 수집 이상 無 |
| W3 | 스코어링 v1 (룰 기반 감성 + 기술 지표) | 스코어링 100종목 < 10초 |
| W4 | 리스크 모듈 + 블랙리스트 + order_manager + reconciler | 리스크 테스트 커버리지 95%+ |
| W5 | 전략 레지스트리 + news_swing entry/exit | 단위 테스트 통과 |
| W6 | Discord 일일 리포트 + 아카이브 | rules §7.1 포맷 준수 |
| W7 | 백테스트 프레임워크 3년 데이터 | Sharpe > 1.0 |
| W8 | 모의투자 연동 + 4주 가동 개시 | 실전 괴리 < 20% |

---

## 구현 실행 전략 (Agent vs 단일 세션)

### 권고: **계층별 하이브리드 운용**

| 작업 유형 | 방식 | 이유 |
|---|---|---|
| **아키텍처 결정·안전 불변식** (risk/, strategy/registry, execution/reconciler, `@risk_gate`) | **단일 세션(메인)** 필수 | 불변식은 전역 일관성이 핵심. 서브에이전트는 세션 간 컨텍스트 공유가 없어 "손절 우회 금지" 같은 교차 규칙을 놓칠 위험 |
| **독립·정형 모듈** (개별 scraper 구현, utility 함수, 단위 테스트 scaffold, docstring 작성) | **Agent(general-purpose) 병렬** | 상호 의존성 적고 입출력 계약만 명확하면 독립 실행 가능. 메인 컨텍스트 절약 + 병렬 속도 |
| **탐색·조사** (KIS API TR ID 매핑표 확인, KRX 휴장일 캘린더 라이브러리 비교, DART OpenAPI 스펙 리딩) | **Agent(Explore)** | 결과만 메인으로 회수, 원본 문서는 서브에서 소화 |
| **리뷰·통합** (PR 전 최종 읽기, 계층 간 일관성 점검, 테스트 실행) | **단일 세션** | 통합 지점은 분산 금지 |

### 세부 실행 룰
1. **메인 세션은 "아키텍트 + 통합자" 역할**: W1 골격(config, kis client, rate limiter, discord)은 처음부터 끝까지 메인이 직접 작성 → 이후 모든 서브에이전트가 이 계약을 재사용
2. **서브에이전트 투입 시점은 W2 이후**: 각 scraper(dart, yonhap, naver, kis_news)는 `base_scraper` 인터페이스 확정 후 **4개 병렬** 실행 가능
3. **서브에이전트 금지 영역**:
   - `risk/*` (리스크 로직 — 단일 논리 흐름 필수)
   - `strategy/registry.py` (전략 라우팅)
   - `execution/reconciler.py` (상태 정합성)
   - `execution/order_manager.py` (주문 생명주기)
   - 모든 DB 스키마 변경(migrations)
4. **서브에이전트 브리핑 필수 포함 항목**: 입출력 타입·파일 경로·의존 모듈·테스트 요구사항(커버리지)·rules 인용. "코드만 짜라" 식 터스 프롬프트 금지.
5. **통합 체크포인트**: 서브에이전트 결과물은 메인이 **항상 읽고 검증한 뒤** 채택. "에이전트가 완료 보고"를 그대로 수락하지 않음.
6. **세션 경계**: 주 단위(W1~W8) 구현이 끝날 때마다 `CLAUDE.md` Progress Log 기록 → 다음 세션에서 컨텍스트 이어받기.

### 회피할 안티패턴
- ❌ 전체 8주 작업을 "멀티에이전트 팀"에 맡기고 메인은 오케스트레이션만 — 리스크 불변식 붕괴 위험
- ❌ 같은 서브에이전트에 "여러 모듈 동시에" 시키기 — 결과물이 서로 불일치하는 디자인으로 귀결
- ❌ 메인이 읽지 않은 채 서브 산출물 커밋 — Trust but verify 원칙 위반

### 정리: 단계별 매핑
```
W1 (골격)       → 단일 세션
W2 (수집)       → 메인(계약) + Agent×4 (scraper 병렬)
W3 (분석)       → 메인(scorer) + Agent×3 (technical, financial, flow 병렬)
W4 (리스크)     → 단일 세션 (전 구간 메인)
W5 (전략)       → 단일 세션 (registry 메인) + Agent×1 (news_swing 초안)
W6 (리포트)     → 메인(템플릿) + Agent×1 (archiver)
W7 (백테스트)   → 메인 + Agent×1 (데이터 파이프라인)
W8 (모의투자)   → 단일 세션 (관찰·튜닝)
```

---

## 작업 진행 상황 (Progress Log)

> 이 섹션은 대화 세션 간 지속되는 프로젝트 타임라인이다.
> 실제 변경된 코드/파일은 `git log`가 정본(source of truth)이며, 여기는 "왜/무엇을" 맥락을 기록한다.

### 2026-04-18
- **원격 저장소 연결**: `git@github.com:castelwoah/stock_auto_trade.git` → `origin/main`, 업스트림 추적 설정 완료 (`git push` / `git pull` 단축 사용 가능)
- **초기 커밋**: `5593f38` — `.gitignore`, `CLAUDE.md`, `stock_auto_trading.py`, `auto_trading_study.ipynb` 4개 파일 (7,393 insertions)
- **.gitignore 생성**: `config.yaml`, `token.dat`, `KIS*`, `expired/`, `*.mst*`, Python/Jupyter/OS 산출물 제외
- **`.claude/project-rules.md` v2.0 작성**: 뉴스 기반 자동매매 에이전트 시스템 프롬프트 14개 섹션. Phase 0~5 단계 게이트, 리스크 한도, 블랙리스트, 법규 준수 체크리스트 포함. 본 프로젝트의 **다음 단계 방향성 문서**로서 설계·구현 작업 시 참조.
- **코드 분석 브리핑**: `stock_auto_trading.py` 설계 점검 완료. 관찰된 잠재 이슈 — 평단가 분기 L575-582(dict 재할당으로 조건 항상 False), `buy_amount` 시작값 고정(L608), 최상위 try/except가 루프 밖에 위치하여 예외 시 재시작 불가.
- **목표 아키텍처 v2 확정**: 뉴스 기반 멀티팩터 자동매매로 확장. 6계층(Ingestion→Analysis→Strategy→Risk→Execution→Reporting), 매도 4중 방어(손절-5% / 익절+7% / Trailing-3% / 이벤트), 14개 핵심 결정(D1~D14) 수립. 본 문서 상단 "목표 아키텍처 v2" 섹션 참조.
- **구현 실행 전략 확정**: 계층별 하이브리드 — 아키텍처·리스크 불변식은 단일 세션, 독립 모듈(scraper·팩터 계산·테스트 scaffold)은 서브에이전트 병렬. `risk/*`, `execution/reconciler`, `strategy/registry`, migrations는 서브에이전트 금지 영역으로 지정.
- **W1 완료 (src/ 골격 + clients 이관)**:
    - `requirements.txt` 신설 (requests/PyYAML/pydantic/pydantic-settings)
    - `src/config.py` — pydantic `Settings` + `settings_customise_sources`로 `init > ENV > .env > YAML > secrets` 우선순위 확정 (D10 구현). `CANO 8자리 / ACNT_PRDT_CD 2자리 / URL_BASE 말미 슬래시 제거` validator 포함. `KisEnv` enum(PAPER/LIVE) 정의 — URL 라우팅은 D11에서 연결 예정.
    - `src/clients/rate_limiter.py` — Thread-safe Token Bucket (기본 20 rps, monotonic clock). `acquire()`/`try_acquire()` 제공.
    - `src/clients/discord.py` — `DiscordNotifier.send()`(텍스트) / `send_embed()`(W6 리포트 대비). 실패 시 로그만 남기고 파이프라인 비차단.
    - `src/clients/kis.py` — `TokenStore`(일별 파일 캐시 + `expired/` 자동 이동)와 `KisClient` 분리. legacy 6개 함수 전부 동등 메서드 제공: `get_access_token / get_current_price / get_cash_balance / get_stock_balance / get_prev_day_ohlc / order(side=BUY|SELL)`. 모든 HTTP 호출 전 `bucket.acquire()` 통과 강제.
    - **legacy 대비 개선**: 전역 `ACCESS_TOKEN` 제거(인스턴스 필드), `print` → `logging`, 토큰 경로 하드코딩 제거(`Settings.TOKEN_DIR` 주입), 응답 실패 시 명시적 예외 발생.
    - **스모크 검증 통과**: 설정 로드, Token Bucket burst/대기 타이밍(< 1ms / ≈200ms), 클라이언트 인스턴스 초기화. 실 API 호출은 미실행(안전).
    - **Phase 1 Gate "legacy 동작 동일성 검증"**: 함수 시그니처/TR_ID 레벨 매핑 완료. 실주행 동등성 검증은 W2 수집 파이프라인 연동 시점에 수행 예정.

## Notes

- `.gitignore` 설정됨 — `config.yaml`, `token.dat`, `KIS*`, `expired/`, `*.mst*` 등 민감·산출물 파일 제외
- 향후 기능 확장 방향은 `.claude/project-rules.md`(v2.0) 참조 — 뉴스 기반 멀티팩터 스코어링, 리스크 한도, Phase 게이트 등 명세
- 모든 알림은 Discord Webhook으로 전송됨
- KIS API는 호출 빈도 제한이 있으므로 `time.sleep()` 사용 중

## 문서 유지 관리 규칙 (For Claude)

이 `CLAUDE.md`는 세션 간 공유되는 프로젝트 **최우선 컨텍스트 문서**다. Claude Code는 세션 시작 시 이 파일을 자동 로드하므로, 여기에 최신 정보를 유지하면 중복 탐색·재설명 없이 즉시 작업에 착수할 수 있어 토큰이 절약된다.

### 업데이트 트리거 (이런 일이 있으면 이 파일을 갱신)
- 새 파일·모듈·설정 추가 또는 기존 것의 구조적 변경
- 의존성(`requirements.txt` 등) 추가/제거
- 매매 전략·파라미터·종목 리스트 변경
- Git 원격/브랜치 구조 변경, 주요 커밋·푸시
- 설계상의 결정(예: `.claude/project-rules.md` 조항 채택/유예)
- 알려진 버그·제약(TODO)의 발견 또는 해결

### 업데이트 방법
- **Progress Log** 섹션에 날짜별 bullet 추가 ("왜 했는가" 중심, 코드 diff 복붙 금지)
- 기술 스택·프로젝트 구조·함수 목록 등 **정적 섹션**도 변경 발생 시 동시에 수정
- 3개월 이상 경과한 Progress Log 엔트리는 "YYYY-Q(n) 요약" 한 줄로 압축

### 제외 원칙 (여기에 쓰지 말 것)
- `git log --oneline`으로 복원 가능한 단순 커밋 이력
- 민감 정보 (API 키, 계좌번호, Webhook URL)
- 일시적 작업 메모 (TODO는 이슈 트래커 또는 코드 `# TODO:` 주석으로)
