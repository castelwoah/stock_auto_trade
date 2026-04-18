"""KIS Open API news scraper (SCAFFOLD).

본 모듈은 한국투자증권(KIS) Open API 의 국내주식 뉴스 조회 TR 을
재사용하여 종목별 뉴스를 ``Article`` 로 정규화하기 위한 **스캐폴드**다.

실제 TR_ID 와 응답 스키마는 KIS 개발자 포털
(https://apiportal.koreainvestment.com/) 에서 환경(실전/모의)별로
확인 후 배선해야 한다. 이 파일은 인터페이스·파라미터·매핑 계획만
기술하고, 실제 호출 지점은 ``NotImplementedError`` 로 남겨둔다.

HTTP 호출 경로 정책
-------------------
KIS 뉴스 TR 은 KIS 공통 인증/레이트리밋 체계를 공유하므로
``BaseScraper._get()`` (일반 HTTP GET) 가 아니라 주입된
``KisClient._get(path, tr_id, params)`` 를 사용한다.
- KisClient 내부에서 ``TokenBucket`` (초당 20건 공유 버킷이 KIS 기본
  레이트리밋) 으로 초당 호출 수를 제어한다.
- 그러므로 본 스크래퍼는 ``min_interval_sec`` 를 직접 관리하지 않는다.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Iterator

from src.ingestion.base_scraper import Article, BaseScraper


if TYPE_CHECKING:  # 순환 import / 실행시 의존 최소화
    from src.clients.kis import KisClient


logger = logging.getLogger(__name__)


class KisNewsScraper(BaseScraper):
    """KIS Open API 국내주식 뉴스 스크래퍼 (스캐폴드).

    Parameters
    ----------
    kis_client:
        이미 구성된 ``KisClient`` 인스턴스. 본 스크래퍼는 KIS REST
        호출을 직접 수행하지 않고 ``kis_client._get(path, tr_id, params)``
        를 통해 호출한다. 따라서 인증/토큰 캐시/레이트리밋은 모두
        ``KisClient`` 가 담당한다.
    target_symbols:
        뉴스를 조회할 종목코드(6자리) 리스트. KIS 뉴스 조회 TR 은
        종목코드 단위(전종목 일괄조회 불가)인 경우가 일반적이므로
        호출측에서 대상 종목을 명시해야 한다.
    """

    slug = "kis_news"

    def __init__(self, kis_client: "KisClient", target_symbols: list[str]) -> None:
        super().__init__()
        self._kis = kis_client
        self.target_symbols = list(target_symbols)

    # ------------------------------------------------------------------
    # BaseScraper interface
    # ------------------------------------------------------------------

    def fetch(self, since: datetime | None = None) -> Iterator[Article]:
        """종목별 KIS 뉴스를 수집하여 Article 로 yield (SCAFFOLD).

        구현 TODO
        ---------
        1. TR_ID 확정
           - KIS 국내주식 뉴스 관련 TR_ID 후보: ``FHKST03010100`` 류
             (국내주식 관련 뉴스/공시 조회 엔드포인트군). 실제 값은
             KIS 개발자 포털의 "국내주식 뉴스/공시" 섹션을 확인해야 함.
           - 실전/모의투자 환경에 따라 TR_ID prefix 가 달라질 수 있음
             (T*/V* 등). settings 로부터 환경을 추론하거나 생성자
             옵션으로 받도록 확장 예정.

        2. 엔드포인트 경로
           - 예상 path: ``uapi/domestic-stock/v1/quotations/news-title``
             또는 ``.../inquire-news-title`` 계열.
           - ``self._kis._get(path, tr_id, params)`` 호출.

        3. 요청 파라미터 (종목별 루프)
           - ``FID_COND_MRKT_DIV_CODE``: 시장 구분 (J = 주식)
           - ``FID_INPUT_ISCD``        : 종목코드 (6자리)
           - ``FID_INPUT_DATE_1``      : 조회 시작일 YYYYMMDD (since)
           - ``FID_INPUT_DATE_2``      : 조회 종료일 YYYYMMDD (today)
           - ``FID_INPUT_HOUR_1``      : 조회 시작 시각 HHMMSS (optional)
           - ``FID_NEWS_TITLE_CNT``    : 조회 건수 (페이지 크기)
           - CTX_AREA_FK/NK 류 연속조회 키 (페이지네이션)

        4. 응답 스키마 추정
           - ``rt_cd`` == "0" 성공
           - ``output`` : 뉴스 항목 배열(list[dict])
             각 원소 필드 (명칭은 KIS 문서 확인 필요):
               * ``news_ofer_entp_code`` — 뉴스 제공사 코드
               * ``data_dt``             — 뉴스 게재일 YYYYMMDD
               * ``data_tm``             — 뉴스 게재시각 HHMMSS
               * ``news_ttl``            — 뉴스 제목
               * ``news_smry``           — 뉴스 요약 (본문 전체는 별도 TR
                                           필요할 수 있음)
               * ``news_ref_news_key``   — 뉴스 고유 식별자
               * ``stck_shrn_iscd``      — 종목 단축코드

        5. Article 매핑 계획
           - ``source``       = self.slug ("kis_news")
           - ``external_id``  = ``news_ref_news_key`` 우선 사용. 없으면
             ``sha1(url or title+published_at)`` 로 대체.
           - ``url``          = KIS 뉴스 상세 URL 이 응답에 없으면 빈
             문자열 또는 KIS viewer 링크 조합. (TODO: viewer URL 규약
             확정)
           - ``title``        = ``news_ttl``
           - ``body``         = ``news_smry`` (요약만; 본문 전체는 별도
             TR 로 W3+ 단계에서 보강)
           - ``published_at`` = ``datetime.strptime(data_dt + data_tm,
             "%Y%m%d%H%M%S")`` — KST naive 로 저장 (Article docstring
             규약에 따름)
           - ``symbols``      = (종목코드,)  (루프에서 넘긴 symbol)
           - ``raw``          = 원 응답 dict 사본

        6. 증분 수집
           - ``since`` 가 None 이면 오늘 00:00 부터.
           - ``since`` 보다 이전(또는 동일) published_at 은 스킵.

        7. 연속조회 (페이지네이션)
           - 응답의 CTX_AREA_FK/NK 값을 다음 요청의 동일 필드에 재주입.
           - ``tr_cont`` 헤더가 필요하면 ``KisClient._get`` 시그니처
             확장 필요 (현재는 params 만 지원).

        Raises
        ------
        NotImplementedError
            KIS 뉴스 TR_ID / 응답 스키마가 공식 문서로 검증되기 전까지
            본 메서드는 의도적으로 미구현 상태다.
        """
        raise NotImplementedError(
            "KIS news TR_ID must be verified against KIS dev portal"
        )

    # ------------------------------------------------------------------
    # internal helpers (scaffold)
    # ------------------------------------------------------------------

    def _fetch_symbol_news(self, symbol: str, since: datetime | None) -> Iterator[Article]:
        """단일 종목의 KIS 뉴스를 페이지네이션하며 Article 로 yield (SCAFFOLD).

        Parameters
        ----------
        symbol:
            6자리 종목코드. KIS 뉴스 TR 의 ``FID_INPUT_ISCD`` 로 전달.
        since:
            이 시각 이후(exclusive) 뉴스만 반환. None 이면 오늘 00:00.

        Notes
        -----
        구현 시 ``fetch`` 의 구현 TODO 블록을 따르며, 실제 호출은
        ``self._kis._get(path, tr_id=..., params={...})`` 로 수행한다.
        반환 항목이 없거나 응답 상태 코드가 "정상(뉴스 없음)" 을 나타낼
        경우 단순히 반환(yield 없음)하고, 그 외 오류는 예외로 전파한다.
        """
        raise NotImplementedError(
            "KIS news TR_ID must be verified against KIS dev portal"
        )
