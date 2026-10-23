from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.ingestion.master import Master


# Generic Korean business-name tokens that alone should never resolve to a single stock
GENERIC_STOPWORDS: frozenset[str] = frozenset({
    "현대", "삼성", "LG", "SK", "GS", "한화", "롯데", "신세계", "CJ", "두산",
    "증권", "전자", "화학", "건설", "해운", "산업", "그룹", "홀딩스", "자동차",
})
# 일반 명사와 같은 종목명 — 기사 속 일반 표현을 종목으로 오인하기 쉽다 (2026-10 실측 오탐)
AMBIGUOUS_NAMES: frozenset[str] = frozenset({
    "대상", "노을", "서한", "보령", "한독", "선진", "대원", "동방", "미래산업",
})
# 종목명 바로 뒤에 붙어도 같은 종목으로 보는 조사·접미 (긴 것부터)
PARTICLES: tuple[str, ...] = (
    "에서는", "으로", "에서", "에게", "까지", "부터", "처럼", "보다", "과의", "와의", "이나",
    "이", "가", "은", "는", "을", "를", "의", "와", "과", "도", "로", "에", "만", "측", "株", "발", "향",
)
_BOUNDARY = "0-9A-Za-z가-힣"
SHORT_NAME_CONFIDENCE = 0.7     # 2글자 이하 — 코드가 같이 나오지 않으면 D4 문턱(0.8) 미달


@dataclass(frozen=True)
class SymbolMatch:
    symbol: str
    name: str
    confidence: float  # 0.0 ~ 1.0; D4: < 0.8 이면 매매 신호 차단, 리포트만


class SymbolResolver:
    """Text → symbol-code matcher.

    - 이름 매칭: 앞은 한글·영숫자와 붙지 않고, 뒤는 경계 또는 조사(PARTICLES) 1개
      ("삼성전자가 신고가" → 005930). 모든 이름을 긴 것부터 하나의 정규식으로 묶어
      한 번에 훑는다 → 'LG에너지솔루션' 안의 'LG' 같은 짧은 이름 중복 매칭이 없다.
    - 본문 내 종목코드 (6자리) 직접 출현 → 1.0
    - 3자 미만 이름 0.7, GENERIC_STOPWORDS · AMBIGUOUS_NAMES 는 ×0.5
    - 같은 종목에 대한 여러 신호는 confidence 최댓값 사용

    DART 공시는 응답의 stock_code 가 정확한 매핑이므로 resolver 를 거치지 않는다.
    """

    CODE_RE = re.compile(r"(?<![0-9A-Za-z])([0-9][0-9A-Z]{4}0)(?![0-9A-Za-z])")

    def __init__(self, name_to_code: dict[str, str]) -> None:
        self._by_name: dict[str, str] = {n: c for n, c in name_to_code.items() if n}
        self._by_code: dict[str, str] = {}
        for n, c in self._by_name.items():
            self._by_code.setdefault(c, n)
        names = sorted(self._by_name, key=len, reverse=True)
        if names:
            alt = "|".join(re.escape(n) for n in names)
            part = "|".join(re.escape(p) for p in PARTICLES)
            self._name_re: re.Pattern[str] | None = re.compile(
                rf"(?<![{_BOUNDARY}])({alt})(?:{part})?(?![{_BOUNDARY}])"
            )
        else:
            self._name_re = None

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, mapping: dict[str, str]) -> "SymbolResolver":
        return cls(mapping)

    @classmethod
    def from_master(cls, master: "Master") -> "SymbolResolver":
        """보통주만 대상 (ETF·ETN·우선주·스팩 이름은 매칭하지 않는다)."""
        return cls({name: code for code, name in master.common_stocks.items()})

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, text: str) -> list[SymbolMatch]:
        if not text:
            return []
        by_symbol: dict[str, SymbolMatch] = {}

        # 1) 이름 매칭
        if self._name_re is not None:
            for m in self._name_re.finditer(text):
                name = m.group(1)
                conf = 1.0 if len(name) >= 3 else SHORT_NAME_CONFIDENCE
                if name in GENERIC_STOPWORDS or name in AMBIGUOUS_NAMES:
                    conf *= 0.5
                self._add(by_symbol, SymbolMatch(symbol=self._by_name[name], name=name, confidence=conf))

        # 2) 6자리 종목코드 직접 출현
        for m in self.CODE_RE.finditer(text):
            code = m.group(1)
            if code in self._by_code:
                self._add(by_symbol, SymbolMatch(symbol=code, name=self._by_code[code], confidence=1.0))

        return sorted(by_symbol.values(), key=lambda x: (-x.confidence, x.symbol))

    @staticmethod
    def _add(acc: dict[str, SymbolMatch], new: SymbolMatch) -> None:
        cur = acc.get(new.symbol)
        if cur is None or new.confidence > cur.confidence:
            acc[new.symbol] = new

    @property
    def size(self) -> int:
        return len(self._by_name)
