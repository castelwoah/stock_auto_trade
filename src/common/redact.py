"""민감정보 가리기 — 로그·Discord 로 나가는 문자열의 단일 관문.

- 등록된 비밀값(계좌번호·앱키·시크릿·웹훅 URL)은 값 그대로 치환
- URL 쿼리스트링은 통째로 제거 (KIS GET 은 CANO 를 쿼리로 보낸다)
- Discord 웹훅 URL · Bearer 토큰 · 'NNNNNNNN-NN' 형태 계좌번호는 패턴으로 치환

비밀값 등록은 기동 시 1회(`register_secrets`). 등록 전에도 패턴 치환은 동작한다.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Iterable


MASK = "***"
# 너무 짧은 값은 일반 숫자·단어까지 지워 버리므로 등록하지 않는다
_MIN_SECRET_LEN = 6

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+"), "<webhook>"),
    (re.compile(r"(https?://[^\s?#'\"<>]+)\?[^\s'\"<>)]*"), r"\1?<query>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1" + MASK),
    (re.compile(r"(?<!\d)\d{8}-\d{2}(?!\d)"), MASK),
)

_lock = threading.Lock()
_secrets: set[str] = set()


def register_secrets(values: Iterable[str | None]) -> None:
    with _lock:
        for v in values:
            if v and len(v) >= _MIN_SECRET_LEN:
                _secrets.add(v)


def clear_secrets() -> None:
    """테스트용."""
    with _lock:
        _secrets.clear()


def redact(text: object) -> str:
    s = str(text)
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for v in secrets:
        s = s.replace(v, MASK)
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s


class RedactingFormatter(logging.Formatter):
    """traceback 을 포함한 최종 로그 문자열 전체를 가린다."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
