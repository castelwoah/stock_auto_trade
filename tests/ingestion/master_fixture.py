"""KIS 마스터 고정폭 줄을 합성하는 테스트 헬퍼 (실제 파일과 같은 바이트 배치)."""
import io
import zipfile

from src.ingestion.master import TAIL_LEN


def mst_line(code: str, name: str, group: str, market: str = "kospi") -> bytes:
    head = code.ljust(9).encode("ascii") + f"KR7{code}000".ljust(12)[:12].encode("ascii") + name.encode("cp949")
    tail = group.encode("ascii") + b"0" * (TAIL_LEN[market] - 2)
    return head + tail


def mst_bytes(rows, market="kospi") -> bytes:
    return b"".join(mst_line(c, n, g, market) + b"\r\n" for c, n, g in rows)


def mst_zip(market: str, rows) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{market}_code.mst", mst_bytes(rows, market))
    return buf.getvalue()


KOSPI_ROWS = [
    ("005930", "삼성전자", "ST"),
    ("005935", "삼성전자우", "ST"),       # 우선주 (끝자리 5)
    ("00088K", "한화3우B", "ST"),        # 우선주 (영문 끝자리)
    ("069500", "KODEX 200", "EF"),       # ETF
    ("373220", "LG에너지솔루션", "ST"),
    ("003550", "LG", "ST"),
    ("001680", "대상", "ST"),
    ("000270", "기아", "ST"),
    ("F70100034", "한투한미넥스트혁신성장1(A)", "BC"),
]
KOSDAQ_ROWS = [
    ("000660", "SK하이닉스", "ST"),       # (실제는 KOSPI — 테스트용 배치)
    ("0001A0", "덕양에너젠", "ST"),       # 영숫자 신규 코드 (보통주)
    ("0004Y0", "디비금융제14호스팩", "ST"),  # 스팩
    ("035720", "카카오", "ST"),
]
