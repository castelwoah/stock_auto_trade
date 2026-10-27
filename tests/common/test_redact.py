"""민감정보 가리기 — 등록 비밀값 · URL 쿼리 · 웹훅 · Bearer · 계좌번호 패턴."""
import logging

import pytest

from src.common.redact import RedactingFormatter, clear_secrets, redact, register_secrets


@pytest.fixture(autouse=True)
def _clean():
    clear_secrets()
    yield
    clear_secrets()


def test_query_string_removed():
    msg = ("500 Server Error: Internal Server Error for url: https://openapi.koreainvestment.com:9443"
           "/uapi/domestic-stock/v1/trading/inquire-balance?CANO=12345678&ACNT_PRDT_CD=01")
    out = redact(msg)
    assert "12345678" not in out and "inquire-balance?<query>" in out


def test_registered_secret_and_patterns():
    register_secrets(["87654321", "short", None])
    out = redact("acct 87654321 / 11112222-01 / Bearer abc.def-123 / "
                 "https://discord.com/api/webhooks/1/xyz")
    assert "87654321" not in out and "11112222-01" not in out
    assert "abc.def-123" not in out and "xyz" not in out
    assert redact("short") == "short"          # 6자 미만은 등록 안 함 (일반 단어 보호)


def test_prices_untouched():
    assert redact("현재가 70000 목표가 71000 수량 12345678주") == "현재가 70000 목표가 71000 수량 12345678주"


def test_formatter_redacts_traceback():
    register_secrets(["87654321"])
    fmt = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError("boom 87654321")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "fail %s", ("87654321",), sys.exc_info())
    out = fmt.format(rec)
    assert "87654321" not in out and "RuntimeError" in out


def test_anthropic_key_pattern():
    out = redact("auth failed for sk-ant-api03-AbC_d-123 at api")
    assert "AbC_d-123" not in out and "sk-ant-" not in out
