from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Iterator

import requests


logger = logging.getLogger(__name__)


DEFAULT_USER_AGENT = "mini-project-auto-trading/0.1 (+research; contact=kevin@aivelabs.com)"


@dataclass(frozen=True)
class Article:
    """Normalized news/disclosure article.

    서브에이전트·스크래퍼 구현자 모두 이 dataclass만을 산출해야 한다.
    source: 고정 슬러그 (dart / yonhap_rss / naver_disclosure / kis_news)
    external_id: 각 매체의 원천 ID (있으면) 또는 URL hash
    published_at: 기사 게재 시각 (tz-aware 권장; naive일 경우 KST 가정)
    body: 본문(text), 없으면 빈 문자열
    """

    source: str
    external_id: str
    url: str
    title: str
    body: str
    published_at: datetime
    symbols: tuple[str, ...] = field(default_factory=tuple)
    raw: dict | None = None


class BaseScraper(ABC):
    """Scraper 공통 베이스.

    - `slug` 클래스 상수로 source 식별자 고정
    - `fetch(since)`: 증분 수집용. since 이후 기사만 yield
    - robots.txt·레이트 리밋·UA 명시는 구현체 책임이지만,
      HTTP helper(`_get`)가 공통 지연·헤더를 강제한다
    """

    slug: str = ""  # subclass MUST override
    user_agent: str = DEFAULT_USER_AGENT
    min_interval_sec: float = 1.0  # rules §6 Step1: 각 사이트별 최소 1초 지연
    timeout: float = 10.0

    def __init__(self) -> None:
        if not self.slug:
            raise ValueError(f"{type(self).__name__}.slug must be set")
        self._last_request_at: float = 0.0

    @abstractmethod
    def fetch(self, since: datetime | None = None) -> Iterator[Article]:
        """Yield Article objects published after `since` (exclusive)."""

    # ------------------------------------------------------------------
    # HTTP helpers shared by all scrapers
    # ------------------------------------------------------------------

    def _sleep_if_needed(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval_sec - elapsed
        if wait > 0:
            time.sleep(wait)

    def _get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> requests.Response:
        self._sleep_if_needed()
        merged_headers = {"User-Agent": self.user_agent}
        if headers:
            merged_headers.update(headers)
        try:
            res = requests.get(url, params=params, headers=merged_headers, timeout=self.timeout)
            self._last_request_at = time.monotonic()
            res.raise_for_status()
            return res
        except requests.RequestException:
            self._last_request_at = time.monotonic()
            raise


def collect_all(scrapers: Iterable[BaseScraper], since: datetime | None = None) -> list[Article]:
    """Run multiple scrapers sequentially and return a flat list.

    병렬 실행은 상위 오케스트레이터(W6 scheduler)의 책임.
    """
    out: list[Article] = []
    for s in scrapers:
        try:
            for a in s.fetch(since):
                out.append(a)
        except Exception:
            logger.exception("scraper %s failed", s.slug)
    return out
