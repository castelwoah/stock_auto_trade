"""종목 마스터 고정폭 파서 · 보통주 필터 · 보관(MasterStore)."""
from datetime import date

from src.ingestion.master import Master, MasterStore, is_common_stock, parse_mst

from tests.ingestion.master_fixture import KOSDAQ_ROWS, KOSPI_ROWS, mst_bytes, mst_zip


def _fetch_ok(url):
    return mst_zip("kospi", KOSPI_ROWS) if "kospi" in url else mst_zip("kosdaq", KOSDAQ_ROWS)


def test_parse_fixed_width_fields():
    rows = parse_mst(mst_bytes(KOSPI_ROWS), "kospi")
    by = {r.code: r for r in rows}
    assert by["005930"].name == "삼성전자" and by["005930"].group == "ST"
    assert by["069500"].group == "EF" and by["F70100034"].group == "BC"
    assert len(rows) == len(KOSPI_ROWS)


def test_parse_kosdaq_tail_length():
    rows = parse_mst(mst_bytes(KOSDAQ_ROWS, "kosdaq"), "kosdaq")
    assert {r.code: r.name for r in rows}["0001A0"] == "덕양에너젠"


def test_common_stock_filter():
    rows = {r.code: r for r in parse_mst(mst_bytes(KOSPI_ROWS), "kospi")
            + parse_mst(mst_bytes(KOSDAQ_ROWS, "kosdaq"), "kosdaq")}
    common = Master(as_of="x", rows=rows).common_stocks
    assert {"005930", "373220", "0001A0", "035720"} <= set(common)
    for excluded in ("005935", "00088K", "069500", "F70100034", "0004Y0"):
        assert excluded not in common
    assert not is_common_stock(rows["0004Y0"])


def test_store_refresh_saves_and_loads(tmp_path):
    store = MasterStore(tmp_path, fetch=_fetch_ok)
    m = store.refresh(date(2026, 10, 23))
    assert m is not None and m.as_of == "20261023" and "005930" in m.common_stocks
    assert (tmp_path / "20261023" / "kosdaq_code.mst").exists()
    assert MasterStore(tmp_path).load_latest().as_of == "20261023"


def test_store_download_failure_falls_back_to_latest(tmp_path):
    MasterStore(tmp_path, fetch=_fetch_ok).refresh(date(2026, 10, 22))

    def boom(url):
        raise OSError("network")
    m = MasterStore(tmp_path, fetch=boom).refresh(date(2026, 10, 23))
    assert m is not None and m.as_of == "20261022"
    assert MasterStore(tmp_path / "none", fetch=boom).refresh(date(2026, 10, 23)) is None


def test_store_purges_old_days(tmp_path):
    store = MasterStore(tmp_path, fetch=_fetch_ok)
    for d in range(1, 11):
        store.refresh(date(2026, 10, d))
    kept = sorted(p.name for p in tmp_path.iterdir())
    assert len(kept) == 7 and kept[-1] == "20261010"
