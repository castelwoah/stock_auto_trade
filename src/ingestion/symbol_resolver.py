from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# Generic Korean business-name tokens that alone should never resolve to a single stock
GENERIC_STOPWORDS: frozenset[str] = frozenset({
    "현대", "삼성", "LG", "SK", "GS", "한화", "롯데", "신세계", "CJ", "두산",
    "증권", "전자", "화학", "건설", "해운", "산업", "그룹", "홀딩스", "자동차",
})


@dataclass(frozen=True)
class SymbolMatch:
    symbol: str
    name: str
    confidence: float  # 0.0 ~ 1.0; D4: < 0.8 이면 매매 신호 차단, 리포트만


class SymbolResolver:
    """Text → symbol-code matcher.

    MVP 전략:
      - 이름 exact (단어 경계 기준) 매칭 → 기본 1.0
      - 본문 내 종목코드 (6자리) 직접 출현 → 1.0
      - 이름이 GENERIC_STOPWORDS 에 포함되면 confidence 반감
      - 3자 미만 이름은 0.7 로 시작 (오탐 위험)
      - 같은 종목에 대한 여러 신호는 confidence 최댓값 사용

    DART corp_code 매칭은 DART scraper 자체가 이미 정확한 매핑을 제공하므로
    resolver 단계에서는 별도 처리하지 않는다.
    """

    CODE_RE = re.compile(r"\b(\d{6})\b")

    def __init__(self, name_to_code: dict[str, str]) -> None:
        self._by_name: dict[str, str] = dict(name_to_code)
        self._codes: set[str] = set(name_to_code.values())

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, mapping: dict[str, str]) -> "SymbolResolver":
        return cls(mapping)

    @classmethod
    def from_mst_files(cls, *paths: Path) -> "SymbolResolver":
        """Parse KIS-provided KOSPI/KOSDAQ code master (.mst) files.

        NOTE: KIS .mst는 고정폭 EUC-KR 텍스트이며 컬럼 포지션이 시장별로 다르다.
        여기서는 `<code> <whitespace> <name>` 포맷으로 단순화된 변환본을 가정하고,
        실제 운용 시 W3 이전에 정식 파서로 교체한다.
        """
        mapping: dict[str, str] = {}
        for p in paths:
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="euc-kr", errors="ignore")
            except UnicodeDecodeError:
                text = p.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^(\d{6})\s+(.+)$", line)
                if m:
                    code, name = m.group(1), m.group(2).strip()
                    if name:
                        mapping[name] = code
        return cls(mapping)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, text: str) -> list[SymbolMatch]:
        if not text:
            return []
        by_symbol: dict[str, SymbolMatch] = {}

        # 1) 이름 매칭
        for name, code in self._by_name.items():
            if not name:
                continue
            # 단어 경계: 한글/영문/숫자 인접 금지
            pattern = rf"(?<![0-9A-Za-z가-힣]){re.escape(name)}(?![0-9A-Za-z가-힣])"
            if re.search(pattern, text):
                conf = 1.0 if len(name) >= 3 else 0.7
                if name in GENERIC_STOPWORDS:
                    conf *= 0.5
                self._add(by_symbol, SymbolMatch(symbol=code, name=name, confidence=conf))

        # 2) 6자리 종목코드 직접 출현
        for m in self.CODE_RE.finditer(text):
            code = m.group(1)
            if code in self._codes:
                name = next((n for n, c in self._by_name.items() if c == code), code)
                self._add(by_symbol, SymbolMatch(symbol=code, name=name, confidence=1.0))

        return sorted(by_symbol.values(), key=lambda x: (-x.confidence, x.symbol))

    @staticmethod
    def _add(acc: dict[str, SymbolMatch], new: SymbolMatch) -> None:
        cur = acc.get(new.symbol)
        if cur is None or new.confidence > cur.confidence:
            acc[new.symbol] = new

    @property
    def size(self) -> int:
        return len(self._by_name)
