"""OpenDART disclosure list scraper.

금융감독원 전자공시시스템(DART) OpenDART API의 `list.json` 엔드포인트를
호출하여 기간별 공시 목록을 Article 객체로 변환한다.

본문 HTML 파싱은 이후 워크스트림(W3+)에서 수행하므로, 현재 스크래퍼는
메타데이터(접수번호, 공시명, 회사정보, 접수일)만 수집하고 body는 빈
문자열로 둔다.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator

from src.ingestion.base_scraper import Article, BaseScraper


logger = logging.getLogger(__name__)


DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

# OpenDART 상태 코드
STATUS_OK = "000"
STATUS_NO_DATA = "013"


class DartScraper(BaseScraper):
    """OpenDART 공시 목록 스크래퍼."""

    slug = "dart"

    def __init__(
        self,
        api_key: str | None = None,
        corp_code_map: dict[str, str] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DartScraper requires OpenDART api_key")
        super().__init__()
        self.api_key = api_key
        self.corp_code_map = corp_code_map or {}

    # ------------------------------------------------------------------
    # BaseScraper interface
    # ------------------------------------------------------------------

    def fetch(self, since: datetime | None = None) -> Iterator[Article]:
        today = datetime.now()
        if since is None:
            bgn_de = today.strftime("%Y%m%d")
        else:
            bgn_de = since.strftime("%Y%m%d")
        end_de = today.strftime("%Y%m%d")

        page_no = 1
        total_page = 1  # 일단 1로 초기화, 첫 응답에서 갱신
        while page_no <= total_page:
            params = {
                "crtfc_key": self.api_key,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "page_count": 100,
                "page_no": page_no,
            }
            res = self._get(DART_LIST_URL, params=params)
            data = res.json()
            status = data.get("status")
            if status == STATUS_NO_DATA:
                return
            if status != STATUS_OK:
                msg = data.get("message", "")
                raise RuntimeError(
                    f"OpenDART list.json error: status={status} message={msg!r}"
                )

            # 페이지 정보 갱신
            total_page = int(data.get("total_page", 1) or 1)
            # 응답에서 page_no 가 오지만 우리 로컬 counter 를 사용
            rows = data.get("list", []) or []
            for row in rows:
                article = self._row_to_article(row)
                if article is not None:
                    yield article

            page_no += 1

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _row_to_article(self, row: dict) -> Article | None:
        rcept_no = row.get("rcept_no")
        rcept_dt = row.get("rcept_dt")
        report_nm = row.get("report_nm", "") or ""
        corp_code = row.get("corp_code", "") or ""
        if not rcept_no or not rcept_dt:
            logger.warning("dart row missing rcept_no/rcept_dt: %r", row)
            return None

        try:
            published_at = datetime.strptime(rcept_dt + "0900", "%Y%m%d%H%M")
        except ValueError:
            logger.warning("dart row has invalid rcept_dt=%r", rcept_dt)
            return None

        symbols: tuple[str, ...] = ()
        if self.corp_code_map:
            mapped = self.corp_code_map.get(corp_code)
            if mapped:
                symbols = (mapped,)

        return Article(
            source=self.slug,
            external_id=rcept_no,
            url=DART_VIEWER_URL.format(rcept_no=rcept_no),
            title=report_nm,
            body="",
            published_at=published_at,
            symbols=symbols,
            raw=dict(row),
        )
