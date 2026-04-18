"""Tests for the KIS news scraper scaffold.

TR_ID·응답 스키마가 확정되지 않았기 때문에 본 테스트는 스캐폴드의
계약(슬러그, 생성자 서명, NotImplementedError)만 검증한다.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.ingestion.scrapers.kis_news import KisNewsScraper


def _make_scraper(symbols: list[str] | None = None) -> KisNewsScraper:
    kis_client = MagicMock()
    return KisNewsScraper(kis_client, symbols if symbols is not None else ["005930", "035720"])


def test_instantiation_with_mock_kis_client() -> None:
    kis_client = MagicMock()
    scraper = KisNewsScraper(kis_client, ["005930"])
    assert scraper is not None
    # 주입된 client 가 속성으로 유지되어야 이후 _kis._get 호출이 가능하다
    assert scraper._kis is kis_client


def test_slug_is_kis_news() -> None:
    scraper = _make_scraper()
    assert scraper.slug == "kis_news"
    # 클래스 레벨에서도 고정되어야 한다
    assert KisNewsScraper.slug == "kis_news"


def test_target_symbols_are_stored() -> None:
    symbols = ["005930", "035720", "000660"]
    scraper = _make_scraper(symbols)
    assert scraper.target_symbols == symbols
    # 내부 보관은 원본 리스트의 copy 여야 한다 (얕은 mutation 격리)
    symbols.append("999999")
    assert "999999" not in scraper.target_symbols


def test_fetch_raises_not_implemented() -> None:
    scraper = _make_scraper()
    with pytest.raises(NotImplementedError):
        # generator 형 구현 대비 list() 로 강제 소모
        list(scraper.fetch(since=None))


def test_fetch_symbol_news_raises_not_implemented() -> None:
    scraper = _make_scraper()
    with pytest.raises(NotImplementedError):
        list(scraper._fetch_symbol_news("005930", since=None))
