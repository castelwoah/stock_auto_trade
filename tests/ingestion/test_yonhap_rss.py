"""Tests for YonhapRssScraper."""

from __future__ import annotations

import hashlib
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from src.ingestion.scrapers.yonhap_rss import YonhapRssScraper


def _sample_rss(items: list[dict]) -> str:
    item_xml = []
    for it in items:
        guid = it.get("guid", "")
        guid_el = f"<guid>{guid}</guid>" if guid else "<guid></guid>"
        item_xml.append(
            f"""
        <item>
            <title><![CDATA[{it['title']}]]></title>
            <link>{it['link']}</link>
            <description><![CDATA[{it['description']}]]></description>
            <pubDate>{it['pubDate']}</pubDate>
            {guid_el}
        </item>"""
        )
    joined = "\n".join(item_xml)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Yonhap News Test</title>
    <link>https://www.yna.co.kr</link>
    <description>Sample feed for tests</description>
    {joined}
  </channel>
</rss>
"""


def _mock_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.content = text.encode("utf-8")
    resp.text = text
    resp.status_code = 200
    return resp


SAMPLE_ITEMS = [
    {
        "title": "삼성전자 주가 급등",
        "link": "https://www.yna.co.kr/view/AKR20260418001",
        "description": "<p>삼성전자가 장중 5% 상승했다.</p>",
        "pubDate": "Wed, 18 Apr 2026 09:00:00 +0900",
        "guid": "AKR20260418001",
    },
    {
        "title": "SK하이닉스 실적 발표",
        "link": "https://www.yna.co.kr/view/AKR20260418002",
        "description": "<b>SK하이닉스</b>가 분기 실적을 공개했다.",
        "pubDate": "Wed, 18 Apr 2026 10:30:00 +0900",
        "guid": "AKR20260418002",
    },
    {
        "title": "카카오 신사업 진출",
        "link": "https://www.yna.co.kr/view/AKR20260418003",
        "description": "카카오가 새로운 사업에 진출한다.",
        "pubDate": "Wed, 18 Apr 2026 12:00:00 +0900",
        "guid": "",  # empty guid → URL hash fallback
    },
]


def test_fetch_returns_all_articles_with_single_feed():
    xml = _sample_rss(SAMPLE_ITEMS)
    scraper = YonhapRssScraper(feed_urls=["https://example.com/feed.xml"])

    with patch.object(YonhapRssScraper, "_get", return_value=_mock_response(xml)) as mock_get:
        articles = list(scraper.fetch())

    assert mock_get.call_count == 1
    assert len(articles) == 3

    # 필드 검증
    a = articles[0]
    assert a.source == "yonhap_rss"
    assert a.title == "삼성전자 주가 급등"
    assert a.url == "https://www.yna.co.kr/view/AKR20260418001"
    assert a.external_id == "AKR20260418001"
    assert "삼성전자" in a.body
    assert "<" not in a.body and ">" not in a.body  # HTML 태그 제거 확인
    assert a.published_at == datetime(2026, 4, 18, 9, 0, 0)
    assert a.published_at.tzinfo is None  # naive
    assert a.symbols == ()
    assert a.raw == {"feed": "https://example.com/feed.xml", "guid": "AKR20260418001"}


def test_since_filter_excludes_older_items():
    xml = _sample_rss(SAMPLE_ITEMS)
    scraper = YonhapRssScraper(feed_urls=["https://example.com/feed.xml"])

    # 10:00 이후만 원함 → 09:00 아이템은 제외, 10:30 / 12:00 은 포함
    since = datetime(2026, 4, 18, 10, 0, 0)

    with patch.object(YonhapRssScraper, "_get", return_value=_mock_response(xml)):
        articles = list(scraper.fetch(since=since))

    assert len(articles) == 2
    titles = [a.title for a in articles]
    assert "삼성전자 주가 급등" not in titles
    assert "SK하이닉스 실적 발표" in titles
    assert "카카오 신사업 진출" in titles


def test_since_none_returns_all():
    xml = _sample_rss(SAMPLE_ITEMS)
    scraper = YonhapRssScraper(feed_urls=["https://example.com/feed.xml"])

    with patch.object(YonhapRssScraper, "_get", return_value=_mock_response(xml)):
        articles = list(scraper.fetch(since=None))

    assert len(articles) == 3


def test_one_feed_fails_others_continue():
    xml_ok = _sample_rss(SAMPLE_ITEMS[:2])

    scraper = YonhapRssScraper(
        feed_urls=[
            "https://example.com/broken.xml",
            "https://example.com/ok.xml",
        ]
    )

    def side_effect(url, **kwargs):
        if "broken" in url:
            raise RuntimeError("network boom")
        return _mock_response(xml_ok)

    with patch.object(YonhapRssScraper, "_get", side_effect=side_effect):
        articles = list(scraper.fetch())

    # 실패한 피드는 스킵되고, 정상 피드의 아이템은 반환됨
    assert len(articles) == 2
    assert all(a.source == "yonhap_rss" for a in articles)


def test_xml_parse_error_in_one_feed_others_continue():
    xml_ok = _sample_rss([SAMPLE_ITEMS[0]])

    scraper = YonhapRssScraper(
        feed_urls=[
            "https://example.com/bad.xml",
            "https://example.com/ok.xml",
        ]
    )

    def side_effect(url, **kwargs):
        if "bad" in url:
            return _mock_response("this is <not valid xml")
        return _mock_response(xml_ok)

    with patch.object(YonhapRssScraper, "_get", side_effect=side_effect):
        articles = list(scraper.fetch())

    assert len(articles) == 1
    assert articles[0].title == "삼성전자 주가 급등"


def test_empty_guid_falls_back_to_url_hash():
    xml = _sample_rss([SAMPLE_ITEMS[2]])  # guid is empty
    scraper = YonhapRssScraper(feed_urls=["https://example.com/feed.xml"])

    with patch.object(YonhapRssScraper, "_get", return_value=_mock_response(xml)):
        articles = list(scraper.fetch())

    assert len(articles) == 1
    a = articles[0]
    expected_hash = hashlib.sha256(
        "https://www.yna.co.kr/view/AKR20260418003".encode("utf-8")
    ).hexdigest()
    assert a.external_id == expected_hash
    # hex length check (256-bit → 64 chars)
    assert len(a.external_id) == 64


def test_default_feed_urls_are_yonhap_pattern():
    scraper = YonhapRssScraper()
    assert len(scraper.feed_urls) >= 3
    for url in scraper.feed_urls:
        assert url.startswith("https://www.yna.co.kr/rss/")
        assert url.endswith(".xml")


def test_slug_and_source():
    assert YonhapRssScraper.slug == "yonhap_rss"
    scraper = YonhapRssScraper(feed_urls=["https://example.com/feed.xml"])
    xml = _sample_rss([SAMPLE_ITEMS[0]])
    with patch.object(YonhapRssScraper, "_get", return_value=_mock_response(xml)):
        articles = list(scraper.fetch())
    assert articles[0].source == "yonhap_rss"


def test_multiple_feeds_aggregated():
    xml1 = _sample_rss([SAMPLE_ITEMS[0]])
    xml2 = _sample_rss([SAMPLE_ITEMS[1], SAMPLE_ITEMS[2]])

    scraper = YonhapRssScraper(
        feed_urls=["https://example.com/a.xml", "https://example.com/b.xml"]
    )

    def side_effect(url, **kwargs):
        if url.endswith("a.xml"):
            return _mock_response(xml1)
        return _mock_response(xml2)

    with patch.object(YonhapRssScraper, "_get", side_effect=side_effect):
        articles = list(scraper.fetch())

    assert len(articles) == 3
