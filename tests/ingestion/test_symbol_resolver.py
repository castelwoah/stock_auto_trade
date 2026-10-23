"""종목명 매칭 — 조사 허용 · 긴 이름 우선 · 짧은/일반어 이름 확신도 · 코드 직접 출현."""
import pytest

from src.ingestion.master import Master, parse_mst
from src.ingestion.symbol_resolver import SymbolResolver

from tests.ingestion.master_fixture import KOSDAQ_ROWS, KOSPI_ROWS, mst_bytes


@pytest.fixture(scope="module")
def res():
    rows = {r.code: r for r in parse_mst(mst_bytes(KOSPI_ROWS), "kospi")
            + parse_mst(mst_bytes(KOSDAQ_ROWS, "kosdaq"), "kosdaq")}
    return SymbolResolver.from_master(Master(as_of="x", rows=rows))


def _m(res, text):
    return {m.symbol: m.confidence for m in res.resolve(text)}


@pytest.mark.parametrize("text", [
    "삼성전자가 신고가", "삼성전자는 약보합", "삼성전자의 실적", "삼성전자 주가", "[삼성전자] 공시",
    "삼성전자에서는 감산을",
])
def test_particles_and_boundaries(res, text):
    assert _m(res, text) == {"005930": 1.0}


def test_no_match_inside_longer_word(res):
    assert _m(res, "삼성전자우선주 강세") == {}
    assert _m(res, "비삼성전자 계열") == {}


def test_longest_name_wins(res):
    assert _m(res, "LG에너지솔루션과 SK하이닉스의 실적") == {"373220": 1.0, "000660": 1.0}


def test_short_and_ambiguous_names_below_d4(res):
    assert _m(res, "기아 신차 출시") == {"000270": 0.7}            # 2글자 → 0.7 (<0.8)
    assert _m(res, "지원 대상 기업")["001680"] < 0.5                 # 일반어
    assert _m(res, "LG 그룹 계열사")["003550"] < 0.5


def test_code_in_text_is_certain(res):
    assert _m(res, "기아(000270) 신차") == {"000270": 1.0}
    assert _m(res, "덕양에너젠(0001A0) 상장") == {"0001A0": 1.0}
    assert _m(res, "주가 123456원") == {}


def test_non_common_names_not_matched(res):
    assert _m(res, "KODEX 200 순매수, 삼성전자우 강세") == {}


def test_from_dict_still_supported():
    r = SymbolResolver.from_dict({"카카오": "035720"})
    assert [m.symbol for m in r.resolve("카카오가 급등")] == ["035720"] and r.size == 1
    assert SymbolResolver.from_dict({}).resolve("아무 텍스트") == []
