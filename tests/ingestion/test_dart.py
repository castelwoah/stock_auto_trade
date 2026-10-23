"""Tests for DartScraper."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.ingestion.scrapers.dart import DartScraper


def _mock_response(payload: dict) -> MagicMock:
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    r.status_code = 200
    return r


def _row(rcept_no: str, corp_code: str = "00126380", corp_name: str = "삼성전자",
         report_nm: str = "주요사항보고서", rcept_dt: str = "20260418") -> dict:
    return {
        "rcept_no": rcept_no,
        "corp_code": corp_code,
        "corp_name": corp_name,
        "report_nm": report_nm,
        "rcept_dt": rcept_dt,
        "flr_nm": "삼성전자",
        "rm": "",
    }


def test_api_key_required():
    with pytest.raises(ValueError):
        DartScraper(api_key=None)
    with pytest.raises(ValueError):
        DartScraper(api_key="")


def test_fetch_no_data_returns_empty():
    scraper = DartScraper(api_key="dummy")
    scraper.min_interval_sec = 0.0  # avoid sleeps in tests
    payload = {"status": "013", "message": "조회된 데이터가 없습니다.", "list": []}
    with patch("src.ingestion.base_scraper.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        articles = list(scraper.fetch())
    assert articles == []
    assert mock_get.call_count == 1


def test_fetch_paginates_two_pages():
    scraper = DartScraper(api_key="dummy")
    scraper.min_interval_sec = 0.0

    page1 = {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "page_count": 100,
        "total_count": 2,
        "total_page": 2,
        "list": [_row("20260418000001"), _row("20260418000002")],
    }
    page2 = {
        "status": "000",
        "message": "정상",
        "page_no": 2,
        "page_count": 100,
        "total_count": 2,
        "total_page": 2,
        "list": [_row("20260418000003")],
    }

    with patch("src.ingestion.base_scraper.requests.get") as mock_get:
        mock_get.side_effect = [_mock_response(page1), _mock_response(page2)]
        articles = list(scraper.fetch(since=datetime(2026, 4, 18)))

    assert mock_get.call_count == 2
    assert len(articles) == 3
    external_ids = [a.external_id for a in articles]
    assert external_ids == [
        "20260418000001",
        "20260418000002",
        "20260418000003",
    ]
    # page_no 파라미터가 올바르게 증가했는지 확인
    first_params = mock_get.call_args_list[0].kwargs["params"]
    second_params = mock_get.call_args_list[1].kwargs["params"]
    assert first_params["page_no"] == 1
    assert second_params["page_no"] == 2
    assert first_params["bgn_de"] == "20260418"


def test_article_fields_and_published_at():
    scraper = DartScraper(api_key="dummy")
    scraper.min_interval_sec = 0.0
    payload = {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "total_page": 1,
        "list": [_row("20260418000099", rcept_dt="20260417")],
    }
    with patch("src.ingestion.base_scraper.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        articles = list(scraper.fetch())

    assert len(articles) == 1
    a = articles[0]
    assert a.source == "dart"
    assert a.external_id == "20260418000099"
    assert a.url == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260418000099"
    assert a.title == "주요사항보고서"
    assert a.body == ""
    assert a.published_at == datetime(2026, 4, 17, 9, 0)
    assert a.symbols == ()
    assert a.raw is not None
    assert a.raw["rcept_no"] == "20260418000099"


def test_corp_code_map_maps_symbols():
    corp_map = {"00126380": "005930"}  # 삼성전자 매핑
    scraper = DartScraper(api_key="dummy", corp_code_map=corp_map)
    scraper.min_interval_sec = 0.0
    payload = {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "total_page": 1,
        "list": [
            _row("20260418000001", corp_code="00126380"),
            _row("20260418000002", corp_code="99999999"),  # 매핑 없음
        ],
    }
    with patch("src.ingestion.base_scraper.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        articles = list(scraper.fetch())

    assert len(articles) == 2
    assert articles[0].symbols == ("005930",)
    assert articles[1].symbols == ()


def test_error_status_raises_runtime_error():
    scraper = DartScraper(api_key="dummy")
    scraper.min_interval_sec = 0.0
    payload = {"status": "010", "message": "미등록 키입니다.", "list": []}
    with patch("src.ingestion.base_scraper.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        with pytest.raises(RuntimeError):
            list(scraper.fetch())


def test_uses_api_key_and_default_date_range():
    scraper = DartScraper(api_key="MYKEY")
    scraper.min_interval_sec = 0.0
    payload = {"status": "013", "message": "no data", "list": []}
    with patch("src.ingestion.base_scraper.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        list(scraper.fetch())

    params = mock_get.call_args.kwargs["params"]
    assert params["crtfc_key"] == "MYKEY"
    # since=None → bgn_de == end_de == today
    assert params["bgn_de"] == params["end_de"]
    assert params["page_count"] == 100
    assert params["page_no"] == 1


def test_stock_code_maps_symbol_and_max_pages():
    scraper = DartScraper(api_key="dummy", max_pages=1)
    scraper.min_interval_sec = 0.0
    row = dict(_row("20261023000001"), stock_code="005930")
    page = {"status": "000", "total_page": 5, "list": [row, _row("20261023000002")]}
    with patch("src.ingestion.base_scraper.requests.get", return_value=_mock_response(page)) as g:
        arts = list(scraper.fetch())
    assert g.call_count == 1                       # 최근 1 페이지만
    assert arts[0].symbols == ("005930",) and arts[1].symbols == ()
