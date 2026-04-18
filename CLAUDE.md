# CLAUDE.md

## Project Overview

한국투자증권(KIS) Open API를 활용한 **국내 주식 자동매매 프로그램**.
변동성 돌파 전략(Volatility Breakout Strategy)을 기반으로 자동 매수/매도를 수행한다.

## Tech Stack

- **Language**: Python 3
- **Dependencies**: requests, pandas, PyYAML
- **External API**: 한국투자증권 Open Trading API (`openapi.koreainvestment.com:9443`)
- **Notification**: Discord Webhook

## Project Structure

```
mini_project/
├── stock_auto_trading.py   # 메인 자동매매 스크립트 (전체 로직)
├── config.yaml             # API 키, 계좌번호, Discord Webhook URL 등 설정 (민감정보 포함)
├── token.dat               # 토큰 저장 파일
├── KIS{YYYYMMDD}           # 일별 토큰 캐시 파일 (당일 유효)
├── expired/                # 만료된 토큰 파일 보관 폴더
├── kospi_code.mst(.zip)    # KOSPI 종목코드 마스터 파일
├── kosdaq_code.mst(.zip)   # KOSDAQ 종목코드 마스터 파일
└── auto_trading_study.ipynb # 학습/실험용 Jupyter 노트북
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

## Notes

- `.gitignore`가 설정되어 있지 않음 — `config.yaml`, `token.dat`, `KIS*` 파일 등 민감 파일이 git에 노출될 수 있으므로 `.gitignore` 추가 권장
- 모든 알림은 Discord Webhook으로 전송됨
- KIS API는 호출 빈도 제한이 있으므로 `time.sleep()` 사용 중
