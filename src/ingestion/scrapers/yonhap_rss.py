"""Yonhap News RSS scraper.

연합뉴스(Yonhap News Agency)의 공개 RSS 2.0 피드를 수집한다.

- 피드 구조: `https://www.yna.co.kr/rss/{category}.xml`
- 표준 RSS 2.0: `rss/channel/item`
- stdlib 만 사용 (xml.etree.ElementTree, email.utils)
- 외부 의존성 금지: feedparser, lxml, beautifulsoup4 사용하지 않음
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Iterator
from xml.etree import ElementTree as ET

from ..base_scraper import Article, BaseScraper


logger = logging.getLogger(__name__)


# 기본 피드: 경제·마켓·산업 관련 카테고리.
# 자동매매 시스템에서 주가에 영향을 줄 만한 뉴스 스트림 위주.
DEFAULT_FEED_URLS: list[str] = [
    "https://www.yna.co.kr/rss/economy.xml",
    "https://www.yna.co.kr/rss/market.xml",
    "https://www.yna.co.kr/rss/industry.xml",
    "https://www.yna.co.kr/rss/finance.xml",
    "https://www.yna.co.kr/rss/stock.xml",
]


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.DOTALL)


def _unwrap_cdata(text: str | None) -> str:
    if text is None:
        return ""
    s = text.strip()
    m = _CDATA_RE.match(s)
    if m:
        return m.group(1).strip()
    return s


def _strip_html(text: str) -> str:
    no_tags = _HTML_TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", no_tags).strip()


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _find_text(item: ET.Element, tag: str) -> str:
    el = item.find(tag)
    if el is None:
        return ""
    # ElementTree already decodes CDATA into .text; but also handle raw <![CDATA[
    return _unwrap_cdata(el.text if el.text is not None else "")


def _parse_pubdate(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # 요구사항: tzinfo 제거해 KST naive 로 저장
    return dt.replace(tzinfo=None)


class YonhapRssScraper(BaseScraper):
    """연합뉴스 RSS 스크래퍼."""

    slug = "yonhap_rss"

    def __init__(self, feed_urls: list[str] | None = None) -> None:
        super().__init__()
        self.feed_urls: list[str] = (
            list(feed_urls) if feed_urls is not None else list(DEFAULT_FEED_URLS)
        )

    # ------------------------------------------------------------------
    # BaseScraper
    # ------------------------------------------------------------------
    def fetch(self, since: datetime | None = None) -> Iterator[Article]:
        for url in self.feed_urls:
            try:
                res = self._get(url)
            except Exception:
                logger.exception("yonhap_rss: failed to GET feed %s", url)
                continue

            try:
                body_bytes = res.content if hasattr(res, "content") else res.text.encode("utf-8")
                root = ET.fromstring(body_bytes)
            except ET.ParseError:
                logger.exception("yonhap_rss: XML parse error for feed %s", url)
                continue
            except Exception:
                logger.exception("yonhap_rss: unexpected parse failure for feed %s", url)
                continue

            # RSS 2.0: root=<rss>, channel child, item children
            channel = root.find("channel")
            items = channel.findall("item") if channel is not None else root.findall(".//item")

            for item in items:
                try:
                    article = self._item_to_article(item, url)
                except Exception:
                    logger.exception("yonhap_rss: failed to convert item in %s", url)
                    continue

                if article is None:
                    continue

                if since is not None and article.published_at is not None:
                    # since 이후(초과)만 yield
                    if article.published_at <= since:
                        continue

                yield article

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _item_to_article(self, item: ET.Element, feed_url: str) -> Article | None:
        title = _strip_html(_find_text(item, "title"))
        link = _find_text(item, "link")
        description = _find_text(item, "description")
        pub_raw = _find_text(item, "pubDate")
        guid = _find_text(item, "guid")

        published_at = _parse_pubdate(pub_raw)
        if published_at is None:
            # pubDate 없으면 epoch 로 fallback (정렬/필터 정확도 저하되지만 드랍 대신 포함)
            published_at = datetime(1970, 1, 1)

        body = _strip_html(description)

        if guid:
            external_id = guid
        else:
            external_id = _sha256_hex(link or title or feed_url)

        url = link or feed_url

        return Article(
            source=self.slug,
            external_id=external_id,
            url=url,
            title=title,
            body=body,
            published_at=published_at,
            symbols=(),
            raw={"feed": feed_url, "guid": guid},
        )
