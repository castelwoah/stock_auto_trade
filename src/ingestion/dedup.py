from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


# Tracking params that must be stripped to make URL comparison meaningful
TRACKING_PARAM_PREFIXES: tuple[str, ...] = (
    "utm_",
    "gclid",
    "fbclid",
    "yclid",
    "_ga",
    "mc_",
)
TRACKING_PARAMS_EXACT: frozenset[str] = frozenset({
    "ref", "ref_src", "referer", "source", "from", "ck", "mkt_tok",
})


def normalize_url(url: str) -> str:
    """Canonical URL for dedup comparisons.

    - 호스트 소문자화
    - http→https 고정(대부분의 뉴스 매체가 https 지원)
    - 추적 쿼리 제거
    - fragment 제거
    - trailing slash 정규화 (경로가 "/"뿐이면 유지, 그 외 "/..."는 그대로, 중복 슬래시 축소)
    """
    p = urlparse(url.strip())
    scheme = "https" if p.scheme in ("http", "https", "") else p.scheme
    netloc = p.netloc.lower()

    # collapse duplicate slashes in path
    path = re.sub(r"/{2,}", "/", p.path or "/")

    kept = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS_EXACT
        and not any(k.lower().startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES)
    ]
    query = urlencode(sorted(kept))
    return urlunparse((scheme, netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    """SHA-256 of the normalized URL (hex, 64 chars). Matches NewsArticle.url_hash."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# SimHash64 — content near-duplicate fingerprint
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= 2]


def _hash64(s: str) -> int:
    digest = hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False)


def simhash64(text: str) -> int:
    """64-bit SimHash of a text. Returns unsigned int in [0, 2^64)."""
    tokens = _tokens(text)
    if not tokens:
        return 0
    bits = [0] * 64
    for tok in tokens:
        h = _hash64(tok)
        for i in range(64):
            bits[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if bits[i] > 0:
            out |= 1 << i
    return out


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_near_duplicate(a: int, b: int, *, threshold: int = 3) -> bool:
    """Typical threshold for 64-bit SimHash: 3 bits ≈ high similarity."""
    return hamming_distance(a, b) <= threshold


def to_signed64(u: int) -> int:
    """Convert unsigned 64-bit to signed 64-bit for BIGINT storage (SQLite ok either way,
    PostgreSQL BIGINT는 signed)."""
    return u - (1 << 64) if u >= (1 << 63) else u


def from_signed64(s: int) -> int:
    return s + (1 << 64) if s < 0 else s
