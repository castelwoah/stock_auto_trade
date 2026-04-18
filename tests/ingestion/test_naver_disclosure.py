"""Tests for NaverDisclosureScraper scaffold."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.ingestion.scrapers.naver_disclosure import NaverDisclosureScraper


def test_slug_is_naver_disclosure() -> None:
    assert NaverDisclosureScraper.slug == "naver_disclosure"


def test_instantiation_without_symbols() -> None:
    scraper = NaverDisclosureScraper()
    assert scraper.slug == "naver_disclosure"
    assert scraper.target_symbols is None


def test_instantiation_with_target_symbols() -> None:
    symbols = ["005930", "035720"]
    scraper = NaverDisclosureScraper(target_symbols=symbols)
    assert scraper.target_symbols == ["005930", "035720"]
    # 생성자 인자에 대한 방어적 복사 확인
    symbols.append("000660")
    assert scraper.target_symbols == ["005930", "035720"]


def test_fetch_raises_not_implemented() -> None:
    scraper = NaverDisclosureScraper(target_symbols=["005930"])
    with pytest.raises(NotImplementedError):
        # fetch 는 generator 일 수 있으므로 소비까지 수행
        list(scraper.fetch(since=datetime(2026, 1, 1)))


def test_fetch_without_since_raises_not_implemented() -> None:
    scraper = NaverDisclosureScraper()
    with pytest.raises(NotImplementedError):
        list(scraper.fetch())


def test_parse_disclosure_html_raises_not_implemented() -> None:
    scraper = NaverDisclosureScraper()
    with pytest.raises(NotImplementedError):
        list(scraper.parse_disclosure_html("<html></html>"))
