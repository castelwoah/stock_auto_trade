# CLAUDE.md

## Project Overview

한국투자증권(KIS) Open API를 활용한 **국내 주식 자동매매 프로그램**.
변동성 돌파 전략(Volatility Breakout Strategy)을 기반으로 자동 매수/매도를 수행한다.

## Tech Stack

- **Language**: Python 3.11 (`.venv`, uv 관리 — `scripts/setup-dev.sh`) / 시스템 python3 엔 pip 없음
- **Dependencies**: `requirements.txt` — requests, PyYAML, pydantic, pydantic-settings, SQLAlchemy, **APScheduler** / dev: pytest, pytest-cov (pandas는 legacy만 사용)
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
│   ├── clients/
│   │   ├── rate_limiter.py # Token Bucket (기본 20 rps)
│   │   ├── discord.py      # DiscordNotifier (webhook)
│   │   └── kis.py          # KisClient + TokenStore (일별 파일 캐시, expired 자동 이동)
│   ├── ingestion/          # W2 신설
│   │   ├── base_scraper.py # BaseScraper + Article dataclass (계약)
│   │   ├── dedup.py        # URL 정규화 + SimHash64 + Hamming distance
│   │   ├── symbol_resolver.py  # 종목명/코드 → symbol (조사 허용 · 긴 이름 우선 단일 정규식 · 2글자 0.7 · 일반어 반감, D4)
│   │   ├── master.py       # KIS 마스터 zip 수신·고정폭 파싱(뒷부분 KOSPI 227/KOSDAQ 221B) · 보통주 필터 · data/master/YYYYMMDD 보관 (D9)
│   │   ├── collector.py    # 수집 → 중복 제거(ID·URL·SimHash) → NewsArticle(제목+요약) → ArticleSymbol
│   │   ├── service.py      # NewsService — master_refresh / news_collect / universe 빌드 job 본체
│   │   └── scrapers/
│   │       ├── dart.py                # OpenDART list.json (실구현, 페이지네이션)
│   │       ├── yonhap_rss.py          # 연합뉴스 RSS 2.0 (economy·market·industry, stdlib XML)
│   │       ├── naver_disclosure.py    # 스캐폴드 (TODO: HTML 실구현 + 법무)
│   │       └── kis_news.py            # 스캐폴드 (TODO: KIS TR_ID 확정)
│   ├── analysis/           # W3 신설
│   │   ├── contracts.py    # FactorScore dataclass + clip_score / linear_map
│   │   ├── sentiment.py    # 룰 v1 (긍정 23 / 부정 24 키워드, 부정어 양방향 ±15자)
│   │   ├── news.py         # 심볼별 기사 집계, 시간 감쇠(48h half-life), D5 look-ahead 필터
│   │   ├── scorer.py       # 가중합(news30/fin25/tech25/flow20), CANDIDATE_THRESHOLD=70, D12 메타
│   │   ├── technical.py    # RSI14 Wilder + MA5/20/60 + MACD (3봉 cross 창, eps 1e-9)
│   │   ├── financial.py    # 부채비율·ROE·영업이익률·OCF, 균등 평균
│   │   ├── flow.py         # 외국인·기관 5d 순매수, 5d/60d 거래량비 (market_cap 정규화)
│   │   └── llm_judge.py    # 10-27 — 브리핑용 Claude API 판단 (tool_use JSON 검증) + 룰 대체 (매매 미반영)
│   ├── risk/               # W4 신설 (서브에이전트 금지 영역)
│   │   ├── exceptions.py   # RiskBlocked(code, detail)
│   │   ├── blacklist.py    # StaticBlacklist + LiquidityThresholds (시총<500억/거래대금<10억)
│   │   ├── market_state.py # MarketState, KOSPI -3% / VI / 상·하한가 ±25% / 갭상승 5%+
│   │   ├── sizing.py       # 종목 20% / 섹터 40% / Kelly/4 / 균등 1/N / lot 정렬
│   │   └── limits.py       # DailyLossState / CircuitBreaker / CooldownPolicy + RiskEngine + @risk_gate
│   ├── strategy/           # W5 신설
│   │   ├── base.py          # Signal / Action enum / Strategy ABC / PositionState / StrategyContext
│   │   ├── exit_signals.py  # SL -5% / TP +7% arm / Trailing -3% / event_exit / evaluate_exit / update_peak
│   │   ├── breakout.py      # 변동성 돌파 legacy 포팅, 15:00 매수 중단 / 15:15 EOD 청산, 빈 자리는 뉴스 점수→돌파율 순 (1주도 못 사는 종목 제외)
│   │   ├── universe.py      # news-universe-v1: 보유 + 뉴스 후보 상위 N (+ watchlist 예비), 악재 차단, StockScore 기록
│   │   ├── news_swing.py    # 뉴스 스코어 기반 스윙 (5~10종목, 당일 청산 없음)
│   │   └── registry.py      # 서브포트폴리오 현금 할당 + 신호 라우팅 (D2 격리, 금지 영역)
│   ├── reporting/          # W6 신설 (v3 확장)
│   │   ├── models.py        # DailyReport / MidDaySnapshot / AttemptEntry / MorningStats / PositionSnapshot / CandidateSnapshot / MidDayAnomaly / RESULT_* 상수
│   │   ├── daily_report.py  # render_text (rules §7.1 + v3 실패·차단 섹션) + render_embed (색상 분기)
│   │   ├── midday_report.py # v3 신설 — 12:00 장중 스냅샷, anomaly 탐지, 방어 윈도우(11:55~12:15)
│   │   ├── pnl_calc.py      # v3 신설 — FIFO BUY↔SELL 매칭, 비용 반영 (스키마 무변경)
│   │   ├── builders.py      # v3.1 신설 — DB 조회 기반 build_daily_report / build_midday_snapshot
│   │   ├── intraday.py      # 체결·리스크 즉시 Embed + notify_* helpers
│   │   ├── news_briefing.py # 10-27 — 08:40 장전 뉴스 종목 브리핑 (선정·판단·Embed·reports/briefing JSON·16:00 적중)
│   │   └── archiver.py      # 일별 JSON + trades CSV + 연간 세금 CSV
│   ├── backtest/           # W7 린 신설 (v2)
│   │   ├── portfolio_sim.py # SimFill/SimPosition + idempotency D8 + 왕복 1.03% 비용 (slip 0.25+fee 0.15+tax 0.23)
│   │   ├── engine.py        # bar-by-bar 루프, risk_engine 의존 주입, Registry + sim + loader 통합
│   │   ├── data_loader.py   # BacktestDataLoader ABC + SyntheticDataLoader(seed) + CsvDataLoader + D5 60초 필터 (단일 위치)
│   │   └── metrics.py       # Sharpe(252) / MDD / Win rate / PF(999 sentinel) / CAGR / Alpha + snapshot fixture
│   ├── execution/          # W8 신설 (reconciler · order_manager 는 서브에이전트 금지 영역)
│   │   ├── portfolio.py     # LivePortfolio (DB Position ↔ StrategyContext, Trade 멱등 INSERT, highest_price D3 영속)
│   │   ├── reconciler.py    # KIS × DB 교차검증, 3회 연속 불일치 → trading_off + RiskEvent 기록 (D6), managed_only 옵션
│   │   ├── order_manager.py # v4: 송신 게이트(D8) · 체결조회 동기화 · 취소 확인 후 잔량 재주문 · @risk_gate · shadow · 재시작 복원
│   │   ├── krx_tick.py      # KRX 호가단위 정렬 / n호가 이동
│   │   └── pipeline.py      # tick 체인: Registry → Risk → Order (trading_off·kill switch·투입 상한·고점 갱신)
│   ├── scheduler/          # W8 신설
│   │   ├── calendar.py      # KRX 2026 휴장일 (Agent), is_business_day / next·prev / business_days_between
│   │   ├── watchlist.py     # 29종목 예비 watchlist (legacy 12 + KOSPI 10 + KOSDAQ 7, 091990 상장폐지 제외)
│   │   ├── app.py           # APScheduler wrapper — 영업일 게이트 / misfire 300s / cron + interval 헬퍼
│   │   └── heartbeat.py     # 10-27 — 1분 heartbeat, 10분↑ 간격이면 실행 중단 알림 + RiskEvent(runtime_gap)
│   ├── main.py             # W8 엔트리포인트 — config→clients→runtime→scheduler wire-up, SIGINT/SIGTERM graceful shutdown
│   ├── runtime.py          # Phase A: TradingRuntime — 09:00 prepare / 5분 tick 본체 (시세·종목상태·지수 → pipeline)
│   ├── common/clock.py     # now_kst / today_kst — 시각 단일 기준 KST
│   ├── common/redact.py    # 로그·Discord 민감정보 가리기 (비밀값·URL 쿼리·웹훅·Bearer) + RedactingFormatter
│   └── persistence/        # W2 신설
│       ├── db.py           # SQLAlchemy engine/session, session_scope, init_db
│       └── models.py       # 8 tables: NewsArticle, ArticleSymbol, StockScore, Trade(idem UNIQUE), Position(symbol+strategy_id PK), RiskEvent, DailyPnl, CashFlow
├── tests/                  # W2·W3·W4 확장
│   ├── ingestion/          # 27개 테스트 (dart 7 + yonhap 9 + naver 6 + kis 5)
│   ├── analysis/           # 68개 테스트 (scorer/news/sentiment 22 + tech 15 + fin 15 + flow 12 + integration 4)
│   ├── risk/               # 82개 테스트 (blacklist 9 + market 12 + sizing 19 + limits 20 + engine 22) — 커버리지 100%
│   ├── strategy/           # 75개 테스트 (base 4 + exit 21 + breakout 12 + registry 12 + news_swing 18 + integration 8) — 커버리지 99%
│   ├── reporting/          # 40개 테스트 (daily 14 + intraday 11 + archiver 15) — 커버리지 97%
│   ├── backtest/           # 51개 테스트 (portfolio 18 + metrics 11 + data_loader 13 + engine integration 9) — 커버리지 94%
│   │   └── fixtures/metrics_snapshot.json  # W7 회귀 검증 기준
│   ├── execution/          # 커버리지 96~100% (order_manager v4 · pipeline · portfolio · reconciler · krx_tick)
│   ├── clients/            # KIS 주문·취소·체결조회·시세·토큰 + KisEnv 프로파일 — 커버리지 98%
│   ├── common/             # KST 기준 (호스트 TZ=UTC 에서도)
│   ├── test_runtime.py     # 하루 흐름 통합 (prepare → tick, shadow/live, reconcile, 종목상태 게이트, 재시작)
│   ├── test_main.py        # 엔트리 배선 · 핸들러 예외 격리
│   ├── test_ops_templates.py  # ops 템플릿 하드코딩 재유입 방지
│   └── scheduler/          # 37개 테스트 (calendar 19 + watchlist 8 + app 4 + data loader 등) — 커버리지 88%
├── docs/                   # W8+ 신설
│   ├── runbook.md          # 장애 플레이북 7 시나리오 (토큰/네트워크/Reconciler/CB/Daily Loss/주문/비정상종료)
│   └── scheduling.md       # 운영 가이드 (systemd + crontab 하이브리드, Phase 전환 절차)
├── ops/                    # 인프라 설정 (systemd 유닛 + crontab 본체) — Phase 3~5 불변
│   ├── mini-trading.service          # 데몬 (Restart=on-failure, ProtectHome 등 보안 옵션)
│   ├── mini-trading-start.timer      # Mon-Fri 07:00 KST + Persistent=true (missed trigger 보상)
│   ├── mini-trading-stop.timer       # Mon-Fri 17:00 KST
│   ├── mini-trading-stop.service     # oneshot stop
│   └── cron.d/mini-trading           # 부가 작업 (DB 백업·로그 퍼지·헬스체크 옵션)
├── scripts/                # 운영 자동화 쉘 스크립트
│   ├── _ops_common.sh      # ops 템플릿(@PROJECT_ROOT@/@RUN_USER@/@RUN_GROUP@/@PYTHON@) 렌더 공통
│   ├── install-systemd.sh  # sudo bash — 유닛 4개 렌더 + verify + daemon-reload + enable --now (멱등)
│   ├── setup-dev.sh        # uv 로 .venv(3.11) 생성 + requirements 설치 + pytest (멱등)
│   ├── install-crontab.sh  # 사용자 crontab 에 mini-trading 블록 append (멱등)
│   ├── smoke_live.py       # 실전 스모크: 시세·상태·지수·잔고 원값 + (--order) 1주 매수→취소 → logs/smoke-*.json
│   └── uninstall.sh        # 전체 제거
├── .venv/                  # Python 3.11 개발 venv (git 제외, scripts/setup-dev.sh 로 재생성)
└── .claude/
    ├── project-rules.md    # 자동매매 에이전트 시스템 프롬프트 v2.0 (차기 설계 방향성)
    ├── settings.json       # 하네스: 권한 allow/deny(민감 파일 Read 금지) + hooks 등록
    ├── hooks/
    │   ├── guard_secrets.py      # PreToolUse — config.yaml/.env/token.dat/KIS*/expired 접근·`git add -A` 차단
    │   ├── protect_invariants.py # PreToolUse — 서브에이전트의 금지 영역(risk/registry/reconciler/order_manager/models) 편집 차단
    │   └── run_layer_tests.py    # PostToolUse — 편집한 계층 tests/<layer> 자동 실행, 실패 시 모델에 반환
    └── agents/
        ├── test-runner.md        # pytest 실행·실패 요약 (haiku, 수정 금지)
        ├── wiki-curator.md       # stock-wiki ingest/lint
        └── module-implementer.md # 독립 모듈 구현 (금지 영역·불변식 브리핑 내장)
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
- **W1 커밋**: `12dc2e2` — src/ 골격 9 files, +1081 insertions.
- **W2 완료 (Ingestion + Persistence + Dedup, Agent×4 병렬 적용)**:
    - 메인 단일 세션: `ingestion/base_scraper.py`(BaseScraper ABC + Article frozen dataclass + `_get()` 공통 HTTP + 1s 최소 지연 + UA 명시), `ingestion/dedup.py`(URL 정규화·SHA-256 url_hash·SimHash64·Hamming·signed↔unsigned 변환), `ingestion/symbol_resolver.py`(이름 exact + 6자리 코드 매칭 + GENERIC_STOPWORDS 반감으로 D4 ambiguity<0.8 차단), `persistence/models.py`(8 테이블: NewsArticle / ArticleSymbol / StockScore / Trade [idempotency UNIQUE · D8] / Position [(symbol, strategy_id) 복합PK · D2·D3 highest_price] / RiskEvent / DailyPnl / CashFlow [D7 분리]), `persistence/db.py`(SQLite/PG 자동 분기, `session_scope()` + `init_db()`), `config.py` 확장(`DATABASE_URL`, `DART_API_KEY`).
    - 서브에이전트 Agent×4 병렬 실행 (약 6분 총 소요):
      * `scrapers/dart.py` — OpenDART `list.json` 실구현. 페이지네이션, corp_code 매핑, status 013(no data) 정상 처리. 테스트 7/7 통과.
      * `scrapers/yonhap_rss.py` — 연합뉴스 RSS 2.0 실구현 (stdlib xml.etree + email.utils.parsedate_to_datetime). 기본 피드 5개(economy/market/industry/finance/stock). 개별 피드 실패 시 격리. 테스트 9/9 통과.
      * `scrapers/naver_disclosure.py` — **스캐폴드**. finance.naver.com HTML 구조·robots.txt·ToS 검증 필요. 실구현 TODO 주석 6단계 명시. `NotImplementedError` 방어. 테스트 6/6 통과.
      * `scrapers/kis_news.py` — **스캐폴드**. KIS 개발자 포털에서 TR_ID(`FHKST03010100`류 후보) 확정 필요. KIS Rate limit은 기존 `TokenBucket`이 공유 관리. 테스트 5/5 통과.
    - **트러스트 버트 베리파이**: 메인이 4개 파일 전부 직접 Read하여 계약 준수 확인. 두 실구현 모두 `BaseScraper._get()`만 사용하여 UA·1s 지연 강제.
    - **전체 테스트 27/27 pass** (pytest 9.0.3).
    - **W2 Gate 상태**: 계약·DB·2개 실구현 + 2개 스캐폴드 확보. "7일간 수집 이상 無" 실주행 검증은 Phase 1 후반(스케줄러 가동 시)에 수행.
    - **requirements.txt**에 `SQLAlchemy>=2.0`, `pytest>=8.0` 추가.
- **W2 커밋**: `5ad6b5a` — 21 files, +1672 insertions.

### 2026-04-19
- **W3 완료 (Analysis 계층, Agent×3 병렬 적용)**:
    - 메인 단일 세션: `analysis/contracts.py`(FactorScore 표준 반환형 + clip_score/linear_map), `sentiment.py`(룰 v1, 긍정 23·부정 24 키워드 사전, 부정어 양방향 ±15자 체크, source 가중치 DART 1.0 > KIS 0.9 > Naver 0.85 > Yonhap 0.8), `news.py`(심볼별 집계 + 시간 감쇠 half-life 48h + D5 look-ahead 60초 지연 필터), `scorer.py`(news30/fin25/tech25/flow20 가중합, `CANDIDATE_THRESHOLD=70`, `insufficient_data` 플래그 있으면 50으로 대체 후 candidate 차단, D12 재현성 메타 `scoring_ver + sentiment_model_ver + llm_prompt_hash` 모두 stock_scores row 에 기록).
    - 서브에이전트 Agent×3 병렬 실행 (97~252초):
      * `analysis/technical.py` — RSI14 Wilder / MA5·20·60 / MACD(12,26,9), 최근 3봉 cross 창, eps 1e-9로 부동소수 잡음 차단. 15 테스트 통과.
      * `analysis/financial.py` — 부채비율(50/200) · ROE(0/20) · 영업이익률(0/20) · OCF/매출(0/15) 각 sub-score 후 균등 평균. 15 테스트 통과.
      * `analysis/flow.py` — 외국인·기관 5d 순매수 (market_cap 있으면 ±1% 스케일, 없으면 ±100억 절대 스케일 폴백) + 5d/60d 거래량비 (1.5~3.0 가점, 3.0 초과 과열 감점). 12 테스트 통과.
    - **Technical 모듈 결함 2건 수정 (Trust-but-verify 성공 사례)**:
      1. 선형 시계열에서 부동소수 잡음으로 histogram 부호가 미세 음수가 되어 중립 구간도 감점 → `eps=1e-9` 도입.
      2. MACD cross 검출이 최종 1봉에만 작동해 실제 signal 놓침 → 최근 3봉 창 내 스캔으로 확장.
    - **W3 Gate 달성**: 스코어링 100종목 < 10초 (실측 수십 ms). 전체 테스트 **95/95 pass** (W1·W2·W3 누적).
    - **통합 테스트 4건**: 이상적 종목(뉴스 호재+정배열+우량재무+수급유입) → candidate 편입 확인, 악재 종목 → threshold 미달, technical insufficient → candidacy 차단, DB row에 재현성 필드 모두 포함.
- **W3 커밋**: `2df57da` — 15 files, +1964 insertions.
- **W4 완료 (Risk 계층, 단일 세션 전 구간)**:
    - `risk/exceptions.py` — `RiskBlocked(code, detail)` 단일 예외. code는 머신 태그(`circuit_breaker`/`daily_loss_limit`/`market_halt`/`blacklist`/`liquidity`/`symbol_state`/`cooldown`/`sizing`/`symbol_halted`).
    - `risk/blacklist.py` — `StaticBlacklist` DI 공급자 + `BlacklistEntry`(관리/경고/주의/위험/정지/상장폐지실질심사/SPAC/ETN/우선주 상수). `check_liquidity` (시총 < 500억 또는 거래대금 < 10억 → 실패, 데이터 누락 시 fail-closed).
    - `risk/market_state.py` — `MarketState`(kospi_change_pct, vi_active, price_change_pct, gap_open_pct, halted). `can_trade_today`(KOSPI ≤ -3% 차단), `can_enter_symbol`(정지/VI/±25%/갭5%+ 차단), `can_exit_symbol`(정지만 차단 — DL/CB 상태에서도 기존 포지션 **청산은 허용**).
    - `risk/sizing.py` — `SizingPolicy`(per_symbol 20%, per_sector 40%, target_count 5, kelly 1/4), `compute_max_additional_notional` (sector_map 있을 때 섹터 헤드룸까지 min), `compute_equal_weight_notional`, `compute_kelly_notional`(p/b 기반 fractional Kelly), `resolve_quantity`(lot 정렬).
    - `risk/limits.py` — `DailyLossState.loss_pct`(start_equity=D7 09:00 값 기준), `CircuitBreakerState.evaluate`(연속 3일 손실 or 누적 -10% 자동 triggered), `CooldownPolicy`(재매수 3일/손절 후 재진입 10일), `check_cooldown`이 영업일만 카운트(공휴일 캘린더 W6+), `RiskEngine.check_buy` 9단계 게이트(CB → DL → KOSPI → 블랙리스트 → 유동성 → 심볼상태 → 쿨다운 → 사이징 헤드룸 → 의도 금액), `check_sell`은 정지 종목만 차단, **D14 구현 `@risk_gate` 데코레이터 + `set_risk_engine`/`get_risk_engine` ContextVar API** — 엔진 미설치 시 `RuntimeError`로 fail-loud, `symbol`/`side` kwarg 강제, BUY는 `buy_context: BuyContext` 필수.
    - **W4 Gate 달성**: 82/82 pass, **risk/* 커버리지 100%**. 전체 누적 177/177 pass.
    - **설계 의도**: 매수 경로는 9중 관문이 모두 통과해야 호출 진입, 매도 경로는 청산 차단 사유를 최소화하여 리스크 저감 경로를 보존. `@risk_gate` 우회 불가(엔진 미설치 시 호출 자체가 예외).
- **W4 커밋**: `7c280ce` — 13 files, +1370 insertions.
- **W5 완료 (Strategy 계층, 메인 4개 + Agent×1)**:
    - 메인 단일 세션 (registry는 서브에이전트 금지 영역):
      * `strategy/base.py` — `Action` enum(BUY/SELL/HOLD), `Signal`(strategy_id·signal_id·notional·qty), `PositionState`(highest_price·tp_armed D3 지원), `StrategyContext`(cash_available·candidate_scores·current_prices·prev_day_ohlc), `Strategy` ABC (`owned_positions`가 strategy_id 필터).
      * `strategy/exit_signals.py` — pure: `should_stop_loss`(-5%), `should_arm_trailing`(+7%), `should_trailing_stop`(-3%), `event_exit_reason`(daily_loss_liquidation > halted > managed_stock > vi_repeated > score_collapse), `evaluate_exit` 우선순위 aggregator (event > stop_loss > trailing), `update_peak`(D3 Trailing 상태 갱신 — armed 플래그 sticky).
      * `strategy/breakout.py` — legacy 동작 포팅. 목표가 = `open + (high - low) × 0.5`, 15:00 신규 매수 중단, 15:15 EOD 전량 청산, 당일 중 4중 방어 병행.
      * `strategy/registry.py` — **서브포트폴리오 현금 격리** (등록 시 `allocation_pct`, 합 ≤ 100%), `_build_sub_context`가 해당 전략 소유 포지션만 격리 주입 (D2), `all_signals()`는 **exits → entries** 순서로 수집(리스크 저감 우선), `signal_id` 집합으로 중복 방지 (D8), 전략 예외 격리 (한 전략 실패가 다른 전략에 전파 안 됨), `build_idempotency_key` 는 `date:symbol:strategy_id:signal_id|action` 포맷.
    - 서브에이전트 Agent×1 (131초):
      * `strategy/news_swing.py` — 스코어 ≥70 정렬, 15:00 이후 매수 중단, 당일 청산 없음 (스윙 보유), exit에서 `extras` 입력 기반 halted/managed/vi/daily_loss 이벤트 전파, 누락 score는 중립 50 가정. 18 테스트 통과.
    - **통합 테스트 8건**: Registry 라우팅이 strategy_id 를 전파, cash 할당 격리, idempotency_key 유일성, `@risk_gate`가 유효 시그널 통과 / 블랙리스트 차단 / Daily Loss 는 entries만 차단하고 exits 허용, breakout EOD 시그널도 게이트 통과, exits가 entries보다 먼저 emit 되는 리스크 저감 흐름.
    - **W5 Gate 달성**: 전체 누적 **252/252 pass**, strategy/* 커버리지 **99%** (1줄은 news_swing 가드 분기).
- **W5 커밋**: `75dce15` — 14 files, +1603 insertions.
- **W6 완료 (Reporting 계층, 메인 3개 + Agent×1)**:
    - 메인: `reporting/models.py`(rules §7.1 필드 매핑 dataclass — 모두 frozen), `daily_report.py`(`render_text` rules §7.1 포맷 재현 + `render_embed` 색상 자동 분기 [녹 이익·빨 손실·주황 CB·파 중립] + `send_daily_report(mode=embed|text)`), `intraday.py`(`render_buy_embed`/`render_sell_embed`(손익 부호별 색상)/`render_risk_embed`(주황) + `notify_*` transport helper, `DiscordNotifier.send_embed()` 재사용).
    - 서브에이전트 Agent×1 (127초):
      * `archiver.py` — `<root>/YYYY/YYYY-MM-DD.{json,-trades.csv}` + 연간 `tax-YYYY.csv`. JSON `ensure_ascii=False`, CSV `utf-8-sig` (엑셀 한글 호환). 세금 CSV append 모드에서 신규 파일에만 BOM 수동 기록(중복 BOM 방지). 중복 키 `(date, symbol, qty)` 로 append 멱등. `write_daily_report` 덮어쓰기 멱등.
    - **W6 Gate 달성**: rules §7.1 포맷 재현 (모든 섹션 헤더 8종 + 숫자 포맷팅 + '근거/반성' 태그), Embed 색상 코드 4종 분기 확인, 아카이브 경로·멱등·UTF-8 BOM·한글 라운드트립 검증.
    - 테스트 40/40 pass, reporting/* 커버리지 **97%**. 전체 누적 **292/292 pass**.
- **W6 커밋**: `34c9e17` — 10 files, +1214 insertions.
- **Phase 2·3 진행 방식 확정 — 옵션 B v2 (W7 린 + W8 병행)**:
    - 배경: 원안 W7 풀스펙(1~2주 walk-forward)은 Phase 2 게이트가 backtest 에 과도 의존하게 되고, W1~W6 에 이미 `@risk_gate`/idempotency/reconciler 계획이 확보되어 있어 **W7 을 실전 피드백 이후로 확장하는 편이 효율적**.
    - 17개 개선점 자체검토 결과 v2 반영: Dry-run 3영업일, Budget 현실화(W7 린 2~3일 / W8 인프라 5일), 스키마 변경 대신 `RiskEvent` 재사용(rules §4.2 준수), 슬리피지+수수료+거래세 왕복 **1.03%** 정확 반영, Agent 배분 일관성(metrics·data_loader·calendar·runbook 4건), Paper watchlist **legacy 12 + 시총 상위 18 = 30종목 제한**, 괴리 측정식 `|paper_return - backtest_return| / max(|backtest_return|, 1%) < 20%`, Reconciler 수동 재가동 UX(`docs/runbook.md`), D5 필터 책임은 `data_loader` 단일 위치, KIS PAPER 한계(체결 엔진이 VI·하한가·부분체결 완전 재현 X) 명시.
    - **W7 린 scope**: `backtest/{portfolio_sim, engine, data_loader, metrics}.py` + tests. Gate = "수학적 정확성" (Sharpe 수식 + snapshot fixture + 회귀 테스트), 커버리지 ≥ 85%. **Sharpe > 1.0 수치 게이트는 W7 확장(실 데이터)으로 이월**.
    - **W8 인프라 scope**: `execution/{portfolio, order_manager, reconciler, pipeline}.py` + `scheduler/{app, calendar, watchlist}.py` + `main.py` + `docs/runbook.md`. 금지 영역: `reconciler`, `order_manager`. Dry-run 3영업일(월/화/수).
    - **W7 확장은 W8 paper Week 2 이후 병행**(주 4~8h). 실 3년 데이터·walk-forward 는 Phase 3 완료 시점까지 사후 충족 목표.
- **W7 린 완료 (Backtest 계층, 메인 2개 + Agent×2)**:
    - 메인 `backtest/portfolio_sim.py` — `PortfolioSim` 클래스 (슬리피지 0.25% / 수수료 0.15% / 거래세 0.23% 매도만, 왕복 1.03% 정확 반영), `SimFill` dataclass (idempotency_key UNIQUE D8), `SimPosition` (highest_price·tp_armed D3 지원), `current_positions_as_state(prices)` 로 Strategy 레이어 PositionState 변환, `mark_to_market` / `equity()` / 부분 체결 금지 / 현금 부족 시 전량 스킵.
    - 메인 `backtest/engine.py` — `BacktestEngine` bar-by-bar 이벤트 루프 (기본 5분 간격 09:10~15:20), `business_days()` Mon-Fri 필터, `_run_day` 에서 09:00 open_equity 고정 후 RiskEngineBuilder 호출 (D7), `_build_buy_context` 에서 sim 상태로 BuyContext 구성, `check_buy/sell` 실패 시 `skips` 에 `risk:{code}` 기록, `registry.mark_executed` 로 signal_id 중복 방지.
    - 서브에이전트 Agent×2 병렬 (143초/232초):
      * `backtest/metrics.py` — `BacktestMetrics` dataclass + `compute_metrics()`. Sharpe 연율화 252 (KRX 246~250 대비 보수적, US 관행 호환), MDD 누적 최고가 기준, FIFO BUY↔SELL 매칭으로 avg_holding_days, profit_factor 무한대 방지용 999.0 sentinel (JSON 직렬화 호환). snapshot fixture `tests/backtest/fixtures/metrics_snapshot.json` 회귀 기준. 11 테스트 통과.
      * `backtest/data_loader.py` — `BacktestDataLoader` ABC + `SyntheticDataLoader`(seed 기반 재현성, daily vol 1.5%, 종목별 분리 시드) + `CsvDataLoader`. **D5 look-ahead 60초 필터를 단일 위치(`news` 메서드)** 에 둠 → engine 에서 추가 필터 금지(이중 배제 방지). 13 테스트 통과.
    - **Trust-but-verify 통합 이슈 1건 해결**: engine 은 직전 영업일 close 필요 → 테스트 로더 범위를 시작일보다 7일 앞으로 확장 (실 데이터에서는 히스토리 자연 확보).
    - **W7 린 Gate 달성**: **51/51 pass** (portfolio 18 + metrics 11 + data_loader 13 + engine integration 9), `backtest/*` 커버리지 **94%** (Gate 85% 초과). Sharpe/MDD/PF 수학적 정확성 snapshot fixture 로 회귀 보장. **수치 Sharpe > 1.0 게이트는 W7 확장 실 데이터로 사후 충족 예정**.
    - **전체 누적**: **343/343 pass** (ingestion 27 + analysis 68 + risk 82 + strategy 75 + reporting 40 + backtest 51).
- **W7 린 커밋**: `0c46a3f` — 12 files, +2089 insertions.
- **W8 완료 (Execution + Scheduler + Runbook, 메인 4개 + Agent×2)**:
    - 메인 (금지 영역 2개 포함):
      * `execution/portfolio.py` — `LivePortfolio` DB-backed. `record_trade`(idempotency_key 멱등), `apply_fill_to_position`(BUY 가중평균 / SELL 수량 차감), `update_highest_price`(D3 이벤트 드리븐 WRITE), `mark_halt`, `equity()`.
      * `execution/reconciler.py` — **금지 영역**. `verify()`는 symbol 단위로 KIS×DB 비교(같은 symbol 을 여러 전략이 보유 가능 → 총합 비교). `verify_with_grace()`는 1분 유예 × 3회 재시도. 3회 연속 불일치 시 `trading_off_required()=True`. 불일치마다 `RiskEvent(event_type="reconcile_mismatch")` INSERT — **rules §4.2 스키마 변경 금지 준수** (신규 테이블 X, RiskEvent 재사용).
      * `execution/order_manager.py` — **금지 영역**. `submit` 은 지정가 주문(ORD_DVSN=00), idempotency_key 로 Trade 사전 기록, KIS 실패 시 status='rejected'. `poll()` 은 3분 타임아웃 → 1호가 상향(≈0.5%) 재시도 최대 2회 후 cancel + notify. **15:00 이후 BUY 차단 / 15:15 EOD 일괄 취소 (M5)**. dict iteration size-change bug 발견·수정 (Trust-but-verify 성과).
      * `execution/pipeline.py` — `TradingPipeline` tick 단위 체인. `run_tick`: pending poll → trading_off 체크 → Registry → @risk_gate 대응(check_buy/sell) → `order_manager.submit`. `trading_off` 플래그 + reconciler 상태 OR 결합.
      * `scheduler/watchlist.py` — legacy 12 + KOSPI 상위 10 + KOSDAQ 상위 8 = **30종목 제한** (v2 C6), 블랙리스트 자동 제외, `max_size` 파라미터.
      * `scheduler/app.py` — `TradingScheduler`(APScheduler BackgroundScheduler, KST), `misfire_grace_time=300s` (v2 H4), 영업일 gate 래퍼, `register_default_jobs` 로 6개 job 자동 등록(07:30 master / 08:00 news / 08:30 score / 09:00 state / 09:10~15:20 tick / 16:00 report).
      * `main.py` — 엔트리포인트: `--init-db` / `--dry-run` / 정상 가동. SIGINT/SIGTERM 핸들러로 graceful shutdown (H3 #8). dry-run 실측: 6 jobs 정상 등록.
    - 서브에이전트 Agent×2 병렬:
      * `scheduler/calendar.py` — KRX 2026 휴장일 18개 frozenset (신정·설·삼일절·어린이날·석탄일·현충일·광복절·추석·개천절·한글날·기독탄신일·대체공휴일·연말). `is_business_day`/`next`/`prev`/`business_days_between`. 2027+ 는 확장 가이드 포함. 19 테스트 통과.
      * `docs/runbook.md` — 7 시나리오 플레이북: 토큰 만료, 네트워크 단절, Reconciler 불일치, Circuit Breaker, Daily Loss Limit, 주문 미체결, 비정상 종료. 각 시나리오 4단계(징후→즉시 조치→복구→사후). Secret 관리 / 로그 보존 / 자본 확대 게이트 포함. Agent 제안 추가: "휴장일 sync 실패" + "KIS API 장애" 별도 분리 시나리오 (향후 보강 후보).
    - **Trust-but-verify 수확**: poll() 의 dict iteration size-change 버그를 통합 테스트 중 발견 → `list(pending.items())` + `to_remove` 후처리로 수정.
    - **W8 Gate 달성**: 73/73 pass (portfolio 10 / reconciler 10 / order_manager 10 / pipeline 5 / calendar 19 / watchlist 8 / app 4), execution/* 커버리지 **93%** / scheduler/* **88%** (APScheduler start/shutdown 실행 경로는 설계상 제외). main 엔트리포인트 dry-run 정상 (`--dry-run` 으로 6 jobs 등록 확인).
    - **전체 누적**: **416/416 pass** (W1~W8).
    - **다음 단계**: Phase 2/3 실주행 — 1) `python3 -m src.main --init-db` 로 DB 생성, 2) KIS PAPER 환경에서 3영업일 dry-run, 3) 4주 paper 관찰 개시. W7 확장(3년 실 데이터·walk-forward)은 Week 2 이후 병행.
- **인프라 스케줄링 — systemd + crontab 하이브리드 (Ubuntu, sudo)**:
    - 결정: APScheduler 가 데몬 내부 6개 job(07:30~16:00)을 담당하므로 systemd/cron 의 역할은 "데몬 생명주기 + 부가 작업"으로 분리. **Phase 3 → 5 전 기간 인프라 설정 변경 불필요** 가 핵심 목표.
    - **systemd (4개 유닛, `ops/`)**:
      * `mini-trading.service` — 데몬, `Restart=on-failure` (Phase 4 실자본 단계 핵심), `ProtectHome=read-only` + `ReadWritePaths` 보안 옵션, `Environment="TZ=Asia/Seoul"`.
      * `mini-trading-start.timer` — `OnCalendar=Mon..Fri 07:00 Asia/Seoul`, **`Persistent=true`** 로 missed trigger 보상 (재부팅 후 자동 복구).
      * `mini-trading-stop.timer` + `mini-trading-stop.service` — 17:00 oneshot stop.
    - **crontab (`ops/cron.d/mini-trading`)** — 부가 작업만:
      * 03:00 30일+ 로그 삭제, 03:30 SQLite DB 백업, 일 04:00 7일+ 백업 삭제.
      * 헬스체크 `*/15 9-15 * * 1-5` 는 주석으로 옵션 제공.
    - **설치 자동화 (`scripts/`)**:
      * `install-systemd.sh` (sudo) — 유닛 4개 cp + daemon-reload + enable --now. 멱등.
      * `install-crontab.sh` (user) — 기존 mini-trading 블록 sed 제거 후 append. 멱등.
      * `uninstall.sh` (sudo) — 전체 제거.
    - **운영 가이드 `docs/scheduling.md`**: 11 섹션 — 설치, 일상 운영(systemctl/journalctl), 타임라인, Phase 전환 절차(인프라 불변), 장애 대응, FAQ, 점검 체크리스트.
    - **Phase 전환 시 변경 대상**: `config.yaml` (KIS_ENV) + 데몬 재시작 (`sudo systemctl restart mini-trading`) **만**. systemd 유닛·crontab·코드는 불변.
- **v3 리포트 확장 — 매매 시도 전체 로그화 + 미드데이(12:00) 스냅샷** (2026-04-20):
    - 배경: 기존 리포트는 성공 체결만 표기 → 거절·취소·리스크 차단 이력이 전혀 드러나지 않음 → 전략 튜닝·장애 진단 자료 부족. 사용자 결정 4건: (1) v3 전체 채택, (2) 리스크 차단 code 별 카운트만, (3) intraday Discord 알림 과다 방지, (4) RiskEvent 재사용 (스키마 무변경).
    - **인프라 보강**:
      * `reporting/pnl_calc.py` — FIFO BUY↔SELL 매칭, 비용 정확 반영 (매수 0.15%, 매도 0.15% + 거래세 0.23%). Trade 테이블 스키마 변경 없이 리포트 시점 계산. 13 테스트 pass.
      * `execution/order_manager.py` — STATUS 상수 5개 (`submitted/filled/rejected/cancelled_timeout/cancelled_eod/cancelled_manual`) + `mark_filled`·`_cancel_and_notify`·`mark_cancelled` 에서 `sqlalchemy.update` 로 Trade.status 자동 갱신.
      * `execution/pipeline.py` — `_pass_risk` 에서 `RiskBlocked` catch 시 `RiskEvent(event_type='risk_blocked', action_taken=<code>)` INSERT. 2 테스트 추가.
    - **모델 확장** (`reporting/models.py`):
      * `AttemptEntry` — 실패·차단 시도 단일 레코드 (ts, symbol, action, qty, price, result, reason_code, reason_detail).
      * `TradeExecutionSummary` — `rejections`/`cancellations`/`risk_blocks` 3 섹션 추가 + `risk_block_breakdown`/`cancellation_breakdown` 카운트 properties.
      * v3 MidDay: `PositionSnapshot`/`CandidateSnapshot`/`MorningStats`/`MidDayAnomaly`/`MidDaySnapshot`.
    - **Daily report render 확장** (`daily_report.py`):
      * `■ 실패·차단` 섹션 추가. 거절은 최대 5건 상세 + "외 N건". 리스크 차단은 **code 별 카운트만** (가독성 우선).
      * Embed 에도 "■ 실패·차단" 필드 추가. 6 테스트 추가 (20/20).
    - **Midday report 신설** (`midday_report.py`, 12:00 KST):
      * `render_midday_text` / `render_midday_embed` / `send_midday_report` / `detect_anomalies` / `in_valid_window`
      * **7가지 anomaly 자동 탐지**: `circuit_breaker_active`/`daily_loss_near_limit`(90→critical, 70→warn)/`position_near_sl` (-4%↓)/`position_near_tp` (+6%↑, tp_armed=False 만)/`kospi_down_2pct`/`no_morning_activity` — critical 이상 1건이라도 있으면 Embed 색상 주황.
      * **방어 윈도우 11:55~12:15** — Persistent=true timer 로 missed trigger 보상 시 엉뚱한 시각 발송 방지.
      * 21 테스트 pass.
    - **스케줄러 통합**:
      * `scheduler/app.py` `register_default_jobs` 시그니처에 `midday_report` 추가 — 6→**7개 job**.
      * `main.py` handler stub + scheduler wire-up + dry-run 검증.
    - **v3 Gate 달성**: **459/459 pass** (기존 416 + v3 43 신규: pnl_calc 13 / pipeline risk_blocked 2 / daily 6 / midday 21 / scheduler 1).
    - **rules 준수**:
      * §4.2 DB 스키마 변경 금지 → Trade.pnl_krw 컬럼 추가 X, FIFO 매칭으로 우회.
      * RiskEvent 재사용 (event_type='risk_blocked' / 'reconcile_mismatch').
      * intraday Discord 과다 방지 → 실패·차단은 daily/midday 집계로만 전달.
    - **다음 단계 (Phase 3 실주행 시 추가 실구현)**: handler 들의 실 DB/KIS 데이터 수집 (현재 stub). `build_midday_snapshot(session, kis)` 함수, `build_daily_report(session, kis)` 함수 추가 필요.
- **v3.1 Phase 3 handler 실구현** (2026-04-21):
    - 배경: v3 에서 리포트 렌더 함수는 완성됐으나 실제 DB 조회·조립 로직이 없어 handler 가 여전히 stub. Paper trading 시작 전 **daily/midday 자동 전송** 이 동작하도록 실구현.
    - `reporting/builders.py` 신설:
      * `build_daily_report(session, *, report_date, market?, portfolio?, risk?, ...)` — DB 기반(Trade/RiskEvent/NewsArticle/StockScore 조회), 런타임 상태(market/portfolio/risk)는 주입.
      * `build_midday_snapshot(session, *, ts, current_prices?, ...)` — 오전(09:10~12:00) window 필터, 포지션별 미실현 P&L, top-5 candidates, anomaly 자동 탐지.
      * `_classify_trades()` — Trade.status 별로 buys/TP/SL/rejections/cancellations 자동 분류. FIFO 매칭은 기존 `pnl_calc.compute_realized_pnl` 재사용.
      * `_dominant_factor()` — StockScore 4개 sub-score 에 가중치(30/25/25/20) 적용 후 최대 팩터 선택.
    - `main.py` handler 실구현:
      * `daily_report` handler: Archiver(settings.ARCHIVE_ROOT) + session_scope() + build + send + 3종 아카이브(JSON/trades CSV/세금 CSV) 일괄.
      * `midday_report` handler: 방어 윈도우 11:55~12:15 체크 → build_midday_snapshot → send_midday_report. 예외 시 Discord 경고만 보내고 프로세스 유지.
      * 에러 격리: builder/archiver 예외가 스케줄러 전체를 죽이지 않음.
    - `Settings.ARCHIVE_ROOT` 필드 추가 (기본 `PROJECT_ROOT/reports`), `.gitignore` 에 logs/reports 이미 포함.
    - **v3.1 Gate 달성**: 18 builder 테스트 추가, 전체 **477/477 pass**. dry-run 7 jobs 정상.
    - **미완 항목 (Paper 실주행 중 점진 보강)**:
      * `master_refresh` handler: `kospi/kosdaq_code.mst` 재다운로드 (D9)
      * `news_collect` handler: DART + Yonhap 스크래퍼 호출
      * `analyze_score` handler: 팩터 계산 + StockScore INSERT
      * `prepare_state` handler: reconciler.verify_with_grace() + RiskEngine.daily_loss 리프레시
      * `trading_tick` handler: TradingPipeline.run_tick() 호출
      * KIS 시장 지수(KOSPI/KOSDAQ) 조회 — 별도 TR_ID 필요
      * builder 에 portfolio/risk 주입 (RiskEngine ContextVar 구독)

### 2026-10-19
- **개발 하네스 구축**: 시스템 python3(3.14)에 pip/ensurepip 부재로 테스트 불가 상태였음 → uv 로 Python 3.11 `.venv` 구성 (`scripts/setup-dev.sh`, 멱등). `.claude/settings.json` + hooks 3종(민감 파일 가드 / 서브에이전트 금지 영역 가드 / 편집 계층 자동 테스트) + agents 3종(test-runner / wiki-curator / module-implementer) 추가.
- **버그 수정 `src/backtest/data_loader.py`**: `random.Random((seed, sym, "bars"))` 튜플 시드가 Python 3.11+에서 TypeError (15 테스트 실패). 3.10에서도 `hash()` 기반이라 PYTHONHASHSEED 마다 값이 달라 seed 재현성이 실제로는 깨져 있었음 → 문자열 시드로 교체, 프로세스 간 동일성 확인. 전체 **477/477 pass**.
- **LLM Wiki**: MCP `wiki`(user 스코프, `~/.local/lib/mcp-wiki/wrapper.mjs`) + Obsidian vault `stock-wiki`(`C:\Users\amoll\Documents\stock-wiki`) 기존 설치 확인. modules/decisions/concepts/ops 페이지 전면 작성.
- **결정 (사용자)**: ① 시각 KST 통일 ② 손절 매도는 취소 없이 하향 재주문 ③ ~~PAPER dry-run~~ → **모의투자 생략, 실전 직행** (rules §13 Phase 3 게이트 미충족 — 사용자 결정으로 생략, 대신 섀도 모드 3영업일 + 투입 상한 5% + 킬 스위치) ④ 구 `.coverage`(W8 시점, /home/mycom 경로) 삭제 ⑤ news_swing 보유 기본 3영업일·조건부 5일 (Phase C).
- **Phase A 완료 (브랜치 `feat/live-readiness-phase-a`, push 안 함)** — 실전 주문 경로 안전화:
    - **KIS**: `rt_cd` 검사(거절 예외), 필수 `EXCG_ID_DVSN_CD=KRX`, `ODNO`+`KRX_FWDG_ORD_ORGNO` 보관, 지정가 외 거부, 취소(`TTTC0013U`)·체결조회(`TTTC0081R`, 연속조회)·지수(`FHPUP02100000`)·종목상태(관리·경고·VI·정지) 조회. TR_ID 는 공식 `koreainvestment/open-trading-api` 예제로 확정. `KisEnv` 프로파일(URL·TR·초당 한도 LIVE 18 / PAPER 4) + URL 불일치 시 기동 abort.
    - **OrderManager v4**: 신규 idempotency_key 일 때만 송신(재시작 중복 차단), 매 poll 체결조회로 부분 체결까지 반영, **취소 성공 확인 후에만 잔량 재주문**, KRX 호가단위, 매수 +1호가 2회 / 매도 −1호가 무제한, 매도 첫가 −1%, 거절·차단 매도 재시도 5회, 네트워크 예외만 `unknown`(체결조회로 복구), 재시작 시 미종결 주문 복원. 모든 송신 `@risk_gate` 경유(D14 실제 적용).
    - **Risk/Pipeline**: 투입 상한 `capital_cap`(09:00 평가액 × `MAX_DEPLOY_PCT`, 미체결 매수 포함, 배분 후 금액 축소), 킬 스위치(`TRADING_DISABLED` / `run/TRADING_DISABLED` 파일 — 매수만 차단), tick 마다 `set_risk_engine`(스케줄러 스레드), 고점 갱신 + `tp_armed` 를 영속 고점에서 유도(D3, 스키마 무변경), 유동성 하드코딩 제거(실데이터·fail-closed, 백테스트는 `assume_liquid` 명시).
    - **Runtime/main**: `prepare_state`·`trading_tick` 실구현(`src/runtime.py`). 동적 블랙리스트·MarketState(등락률·갭·VI·정지)·KOSPI/KOSDAQ 지수 매 tick 반영. 기본 `SHADOW_MODE=True`, 배분 breakout 20% / news_swing 0%.
    - **KST 통일**: `src/common/clock.py`, ORM default 만 변경(DDL 무변경), 영업일 게이트·토큰·리포트 전부 KST.
    - **ops**: systemd/cron 템플릿화(`@PROJECT_ROOT@` 등) + `scripts/_ops_common.sh` 렌더 + `systemd-analyze verify` 통과. runbook 에 실전 운용 스위치 표.
    - **테스트**: 477 → **652 pass**, 전체 커버리지 91% (kis 98% / execution 96~100% / runtime 96% / risk 100%).
- **스모크 테스트 스크립트** `scripts/smoke_live.py` (runbook §1.2) — 아래 가정값을 R1·R2·O2·O3 로 판정. 원주문 행 `cncl_yn`(수동취소 감지 근거)도 함께 확인.
- **확인 필요 (실전 스모크 테스트)**: ~~`hts_avls` 단위(억원 가정)~~ · ~~체결조회 `ord_gno_brno` = 주문 응답 `KRX_FWDG_ORD_ORGNO` 가정~~ (10-28 확인), `vi_cls_code` 값 체계, `mrkt_warn_cls_code` 01/02/03 의미, 실전 초당 한도.
- **알려진 제약(Phase A)**: 섀도 모드의 매수는 미체결 추적을 하지 않아 상한 계산이 실전보다 느슨함. 하한가 근처 매도는 가격제한폭 거절 가능(재시도 5회 후 수동 대응). Circuit Breaker 의 연속 손실일 입력(`DailyPnl` 기록)은 Phase B.

### 2026-10-23
- **장애 분석 (10-23)**: 잔고 조회 HTTP 500 (KIS 측 일시 오류 추정, 약 600회 중 5회) → tick 전체 중단 + Discord/`main.err` 에 **계좌번호 노출** (requests 오류 메시지의 쿼리스트링). 실제 매매 중단 원인은 036630 reconcile 불일치 (legacy 봇 보유 추정).
- **P0·P1·P2** (`088d2a1`): `common/redact.py` + 로그 포매터·Discord·`notify_error` 가리기, `KisHttpError`(쿼리 미포함 + 응답 본문 300자). KIS GET 만 5xx·연결오류·EGW00201 재시도(0.5/1s, 3회), POST 재시도 없음. 잔고 실패 시 직전값 + 해당 tick 매수만 차단. 로그 파일 사전 생성.
- **news-universe-v1 (사용자 결정: 권장안 전부 채택)**: 뉴스 → 종목 → 감시 종목, 매수 판단은 기존 변동성 돌파 그대로.
    - 결정값: `UNIVERSE_MODE=news_with_fallback`, 상한 30 / 예비 채움 기준 10, 후보 60점 / 악재 40점, 확신도 0.8, 10분 수집, 반감기 12h, 매수 순서 뉴스 점수→돌파율. DART 는 키 발급 시 자동 사용.
    - 발견·수정한 결함: 마스터 파서가 실제 고정폭 형식을 못 읽음(0종목), 조사 붙은 종목명 미매칭(`삼성전자가`), 연합 finance/stock 피드 404, news_collect/analyze_score stub.
    - 마스터 형식 주의: 뒷부분 길이는 **줄바꿈 제외** KOSPI 227 / KOSDAQ 221 바이트, 그룹코드 = 뒷부분 [0:2] (공식 예제의 228/222 는 줄바꿈 포함 길이).
    - 섀도 비교: 감시 종목과 watchlist 의 돌파 신호를 `RiskEvent(event_type='shadow_compare', action_taken=universe|static)` 로 하루 1회씩 기록 (스키마 무변경), 일일 리포트 "■ 뉴스 감시 종목".
    - 실데이터 점검(10-23 장중, 임시 DB): 마스터 보통주 2,535 · 연합 341건 중 신규 265 · 종목 링크 88(≥0.8: 75) · 후보 1 · 악재 차단 15 (삼성전자·SK하이닉스 포함 — 당일 반도체 급락 기사). 룰 감성 v1 은 긍정 키워드가 적어 후보가 적고, 키워드 1개로도 차단된다(예: '제재'). B1 관찰 기간에 측정·조정 대상.
    - 테스트 666 → **731 pass**. 신규 모듈 커버리지 95~100%.
- **진행 순서**: B1(수집·리포트 관찰 2~3영업일) → B2(섀도 비교 3영업일) → B3(`SHADOW_MODE: false`, 사용자 판단).

### 2026-10-26
- **B1 첫날 운영 확인**: 마스터 2,535 · 뉴스 수집·universe(08:50 뉴스 3·예비 27) 정상, `RECONCILE_MANAGED_ONLY` 로 036630 무시 → Reconcile OK, 잔고 HTTP 500 은 재시도로 전부 복구.
- **발견**: 실계좌 평가액이 매우 작아(투입 상한 약 2,400원) 돌파 신호가 1주도 못 사고 **로그 없이 버려짐** → 섀도 주문 0건, B2 주문 흐름 검증 불가.
- **섀도 가상 자본 (사용자 결정: 1,000만 원, 섀도 매수 누적 포함)**: `SHADOW_CAPITAL_KRW` — `SHADOW_MODE=True` 일 때만 시작 평가액·투입 상한·현금(자본 − 당일 섀도 매수액)을 가상 값으로 계산, 실계좌 조회·Reconcile 은 유지. 당일 `status=shadow` BUY 를 투입액에 포함(DB 기반이라 재시작 복원). 실주문 모드에서는 무시 + prepare 경고. 수량 0 매수는 `RiskEvent(risk_blocked, qty_zero)` 로 기록(모드 무관).
- **제약**: 섀도 매수는 포지션을 만들지 않아 매도 경로(SL/TP/Trailing/EOD)는 섀도로 검증되지 않음 — 가상 포트폴리오는 B2 이후 별도 과제.
- 테스트 731 → **741 pass**.
- **리포트 실값 + 체결 알림 (사용자 승인)**: 미드데이·일일 리포트가 기본값(`-`·₩0·0%)만 찍던 문제 — builder 에 런타임 값 미주입. `runtime.report_state()` 신설(마지막 tick 의 지수 수치·등락률, 평가액(섀도 가상이면 "(가상)"), 당일 손익, Daily Loss 사용률, CB), 16:00 은 지수 재조회. 누적·MDD 는 `DailyPnl` 전까지 `-`. KIS `get_index` 가 `bstp_nmix_prpr`(지수) 함께 반환 — 실 API 로 확인. 수집 뉴스 수를 매체 분포와 같은 `retrieved_at` 기준으로, 후보 수·문구를 `NEWS_MIN_SCORE` 로. 체결 Embed 를 `OrderManager._close_trade` 에 연결(주문 1건당 1회, 부분 체결 표기, 매도 손익은 수수료·세금 반영, 섀도 제외, 알림 실패는 주문 처리와 격리), 종목명은 `runtime.symbol_name`.
- 테스트 741 → **762 pass**.

### 2026-10-27
- **운영 관찰**: 노트북 최대 절전 + 이동 중 네트워크 단절로 07:15~08:32 · 09:35~10:38 실행 중단 (DNS 실패 포함). 09:00 prepare 실패 → 10:38 tick 에서 자동 재준비. 07:30 마스터 갱신은 300초 misfire 초과로 건너뜀. 가상 자본은 정상 적용(상한 50만) → 첫 섀도 매수 2건(247540 5주 · 036540 1주). 그러나 종목당 금액(66.7만) > 상한(50만)이라 첫 종목이 상한을 소진, 이후 `qty_zero` 가 tick 마다 75건 기록.
- **발견 — 룰 감성 오판**: 삼성전자 기사 7건 중 "급등"·"주주환원" 기사는 0점, 과징금 기사만 부정 → 뉴스 점수 0 → 악재 차단. 키워드 사전 누락이 원인 (보강은 브리핑 LLM↔룰 불일치 데이터를 근거로 추후 제안).
- **수정 (사용자 승인, 권장안 전부)**:
    - 1A `MAX_DEPLOY_SLOTS`(3) — 매수 1건 ≤ min(상한 잔여, 상한 ÷ 슬롯). `pipeline._clip_to_capital_cap`. 10-27 데이터 재계산 시 3종목 분산(1·18·2주).
    - 2A `risk_blocked` 를 (날짜·종목·전략·사유) 당 1회만 기록(메모리 + DB 확인 → 재시작에도 유지), 반복 로그는 DEBUG. 리포트 집계도 같은 기준으로 중복 제거(10-27 이전 기록 포함).
    - 3a `NewsService.ensure_master` — 수집·universe 빌드 때 오늘자 마스터가 없으면 갱신(실패 시 30분 간격 재시도, 전일 보관본 유지).
    - 3b `scheduler/heartbeat.py` — 1분 heartbeat, 10분↑ 간격이면 Discord `⏸ 실행 중단 감지` + `RiskEvent(runtime_gap)`. 전송 실패 시 다음 beat 재전송. 일일 리포트 `■ 운영` 섹션.
    - 4 장전 뉴스 브리핑 — `analysis/llm_judge.py` + `reporting/news_briefing.py`, 08:40 job(총 9 job). Claude API(`claude-haiku-5-5`, tool_use 강제 + 재검증, requests 직접 호출 — 새 의존성 없음), 키 없음·실패 시 룰 대체 표시. 정보용(매매 미반영), 08:35~09:05 윈도우, 결과·모델·프롬프트 해시·입력 기사 ID 는 `reports/briefing/*.json`(스키마 무변경), 16:00 적중 평가. `BRIEFING_ENABLED` 기본 false, `ANTHROPIC_API_KEY` 는 redact 등록 + `sk-ant-` 패턴.
- 테스트 762 → **819 pass**. 신규 모듈 커버리지 heartbeat 100% / news_briefing 99% / llm_judge 95%, 전체 95%.
- **사용자 작업**: 브리핑을 켜려면 `config.yaml` 에 `BRIEFING_ENABLED: true` + `ANTHROPIC_API_KEY` 추가 후 재시작(또는 다음 07:00 기동).

### 2026-10-28
- **발견 (10-27 슬롯 분할의 부작용)**: 종목당 한도 16.7만 < SK하이닉스 137.1만 · 삼성전자 23.05만 · NAVER 18.35만. breakout 은 뉴스 점수 상위 3개를 살 수 있는지와 무관하게 고르므로, 돌파 20종목 중 이 3종목이 매 tick 자리를 차지 → 섀도 매수 0건 (qty_zero 3건, 1일 1회 기록은 정상).
- **수정 A (사용자 승인)**: pipeline 이 tick 시작 시 매수 1건 한도 `min(상한 잔여, 상한 ÷ MAX_DEPLOY_SLOTS)` 를 `extras["max_entry_notional"]` 로 전략에 전달(`_clip_to_capital_cap` 과 같은 식, 투입액 계산은 `_existing_notional` 로 공용화). breakout 은 1주 가격 > min(현금 배분, 한도) 인 종목을 후보에서 빼고 다음 순위로 채움 — 정렬 규칙·신호별 최종 축소·리스크 게이트는 그대로. 건너뜀은 종목·일 1회 INFO 로그. 키가 없으면(백테스트·상한 없음) 기존 동작. news_swing 은 배분 0% 라 미적용.
- 테스트 819 → **830 pass** (breakout 100%, pipeline 96%).
- **실전 스모크 테스트 (사용자 승인)**: 읽기 전용 10:07 PASS 6 / WARN 1 — `hts_avls` **억원 확인**. 실주문 11:33 카카오(035720) 1주 @27,350(−10%) 매수 → 취소, PASS 10 / WARN 1 / FAIL 0 — 체결조회 `ord_gno_brno` = 주문 응답 `KRX_FWDG_ORD_ORGNO` **확인**, 체결 0·잔량 0.
- **발견·수정 — 수동취소 미감지**: 실전 원주문 행은 취소 뒤에도 `cncl_yn` 이 빈 값 → `cncl_yn=='Y'` 가정으로는 HTS 수동취소를 못 알아채 주문이 열린 채 남음(매수는 상한 점유, 매도는 재매도 안 됨, 취소 재시도 실패 알림). 봇 자체 취소는 취소 API 결과로 판단해 영향 없음. `kis._parse_fill` 이 `cncl_yn=='Y'` **또는 잔량 0 · 체결 < 주문수량** 이면 취소로 판정. 10-28 원문으로 회귀 테스트.
- 테스트 830 → **835 pass**. 남은 확인: `mrkt_warn_cls_code` 01/02/03 · `vi_cls_code` 발동 원값(해당 종목 발생 시), 실전 초당 한도(R5).

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
