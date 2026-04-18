"""Naver disclosure scraper (SCAFFOLD ONLY).

이 모듈은 네이버 증권 공시 페이지로부터 상장사 공시를 수집하기 위한
스캐폴드이다. 네이버 공시 페이지의 HTML 구조는 예고 없이 변경될 수
있으므로, 본 스캐폴드 단계에서는 **인터페이스만 확정**하고 실제 HTTP
요청 및 HTML 파싱은 TODO 로 남겨둔다.

구현 완료 전까지 `fetch()` 와 `parse_disclosure_html()` 모두
`NotImplementedError` 를 발생시킨다.

의존성 정책:
    - Python 3.10 stdlib + requests 만 허용.
    - beautifulsoup4, lxml, feedparser 등 추가 파서 라이브러리는 금지.
    - 파싱은 향후 html.parser (stdlib) 기반으로 구현할 것을 권장.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from src.ingestion.base_scraper import Article, BaseScraper


class NaverDisclosureScraper(BaseScraper):
    """네이버 증권 공시 스크래퍼 (스캐폴드).

    Parameters
    ----------
    target_symbols:
        수집 대상 종목코드 리스트 (예: ["005930", "035720"]).
        None 이면 최신 전체 공시 목록을 수집한다 (향후 구현).
    """

    slug: str = "naver_disclosure"

    def __init__(self, target_symbols: list[str] | None = None) -> None:
        super().__init__()
        self.target_symbols: list[str] | None = (
            list(target_symbols) if target_symbols is not None else None
        )

    def fetch(self, since: datetime | None = None) -> Iterator[Article]:
        """`since` 이후에 게재된 공시를 Article 로 yield.

        Parameters
        ----------
        since:
            이 시각 이후 (exclusive) 에 공시된 항목만 반환.
            None 이면 첫 페이지 전체를 반환 (구현자 재량).

        Yields
        ------
        Article
            `source="naver_disclosure"`, `symbols=(종목코드,)`,
            `external_id` 은 네이버 공시 고유 ID 또는 URL 해시,
            `published_at` 은 KST tz-aware datetime 권장.

        Notes
        -----
        # =====================================================================
        # TODO: 실구현 시 아래 항목을 반드시 확인·반영할 것.
        # =====================================================================
        #
        # 1) URL 패턴 (확인 필요; finance.naver.com 계열)
        #    - 종목별 공시 리스트 페이지:
        #        https://finance.naver.com/item/news_notice.naver?code={symbol}
        #      (과거 .nhn 확장자에서 .naver 로 전환된 이력 있음)
        #    - 공시 상세/팝업:
        #        https://finance.naver.com/item/news_read.naver?article_id=...&office_id=...&code={symbol}
        #    - 전체 최신 공시(target_symbols is None 케이스):
        #        별도 통합 엔드포인트가 명확하지 않다. DART 기반으로
        #        대체하거나, 심볼별 루프로 구현하는 것을 고려.
        #
        # 2) robots.txt 확인 사항
        #    - https://finance.naver.com/robots.txt 를 최신 상태로 확인
        #    - `/item/news_notice.naver` 경로가 Disallow 되어 있는지
        #    - User-Agent 별 예외 조항이 있는지
        #    - Crawl-delay 명시 시 `min_interval_sec` 을 그에 맞게 상향
        #
        # 3) HTML DOM 구조 (참고용; 변경 가능성 매우 높음)
        #    - 공시 리스트 컨테이너: <table class="type5"> 또는 유사 table
        #    - 각 행: <tr> 내부
        #        * 공시 제목/링크: <td> 의 <a href="news_read.naver?..."> 텍스트
        #        * 정보제공자(출처): <td class="info">
        #        * 게재일시: <td class="date"> (형식 예: "2024.05.01 09:12")
        #    - 종목코드는 URL 쿼리스트링 `code=` 에서 추출
        #    - 공시 external_id 후보: `article_id` + `office_id` 조합
        #
        # 4) 파서 라이브러리 정책
        #    - 현 정책: beautifulsoup4 / lxml 도입 금지.
        #    - stdlib `html.parser.HTMLParser` 서브클래싱으로 충분히
        #      구현 가능. 정규식 파싱은 취약하므로 지양.
        #    - 장기적으로 selectolax 등 경량 파서 도입을 PR 단위로 재검토할 수 있음.
        #      (선순위: stdlib만으로 유지 → 유지보수 비용 증가 시 재논의)
        #
        # 5) Terms of Service / 법적 리스크
        #    - 네이버 이용약관에 자동화된 크롤링 금지 조항이 존재할 가능성.
        #      (ex: 약관 제7조 "회원의 의무" 내 자동 접속/수집 금지)
        #    - 상용 서비스 전 법무 검토 필수.
        #    - 공시 원문은 DART(전자공시) 가 1차 출처이므로, 법적 안전성이
        #      더 높은 DART API 를 주 소스로 사용하고 네이버는 보조·검증용
        #      으로만 활용하는 것을 권장.
        #    - 요청 빈도는 최소 1req/sec 이하, 가능하면 더 보수적으로.
        #    - User-Agent 에 연락처(이메일) 를 명시해 차단 시 소명 가능하게.
        #
        # 6) 구현 순서 제안
        #    a. robots.txt 점검 + 법무 확인
        #    b. 단일 종목 HTML 샘플 저장 (fixture) → parse_disclosure_html 구현
        #    c. fetch() 에서 target_symbols 루프 + since 필터 + dedup 키 생성
        #    d. tests/ingestion/test_naver_disclosure.py 에 fixture 기반 통합 테스트 추가
        # =====================================================================
        """
        raise NotImplementedError(
            "Naver HTML structure requires live verification — see TODO"
        )

    def parse_disclosure_html(self, html: str) -> Iterator[Article]:
        """네이버 공시 리스트 HTML 문자열을 파싱하여 Article 들을 yield.

        Parameters
        ----------
        html:
            `finance.naver.com/item/news_notice.naver?code=...` 응답 본문.

        Yields
        ------
        Article
            파싱된 공시 1건당 1개의 Article.
            - source: "naver_disclosure"
            - external_id: 네이버 article_id + office_id 조합 (예: "0123456_008")
            - url: 공시 상세 페이지 절대 URL
            - title: 공시 제목 (HTML entity 디코딩 완료)
            - body: 리스트 페이지에서는 비어있을 수 있음 ("")
            - published_at: KST tz-aware datetime
            - symbols: (종목코드,) 튜플 (예: ("005930",))
            - raw: 파싱 원천 dict (디버깅용)

        Notes
        -----
        실구현은 stdlib `html.parser.HTMLParser` 기반으로 작성 예정.
        현 시점 스캐폴드에서는 NotImplementedError 를 던진다.
        """
        raise NotImplementedError(
            "parse_disclosure_html is a scaffold; implement with stdlib html.parser"
        )
