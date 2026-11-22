"""国家和公司表中的规范名称、别名与标识格式。"""

import re
from collections.abc import Iterable
from unicodedata import normalize

_HAN_RANGES = r"\u3400-\u9fff\U00020000-\U000323af"
_HAN = re.compile(rf"[{_HAN_RANGES}]")
_IDENTIFIER_CHAR = rf"[^\W{_HAN_RANGES}]"
_COUNTRY_PUNCTUATION = str.maketrans("‘’ʼ‐‑‒–—−", "'''------")


def _normalize_country_text(text: str) -> str:
    text = " ".join(normalize("NFKC", text).translate(_COUNTRY_PUNCTUATION).split())
    return re.sub(r"\s*([()'\-])\s*", r"\1", text)


def read_countries(table: str) -> dict[str, re.Pattern[str]]:
    countries = {}
    for line in table.splitlines():
        if not line.strip():
            continue
        name, *aliases = (cell.strip() for cell in line.split("\t"))
        if not _HAN.search(name) or name in countries or any(not alias for alias in aliases):
            raise ValueError
        terms = []
        for term in map(_normalize_country_text, (name, *aliases)):
            escaped = re.escape(term)
            if _HAN.search(term):
                terms.append(escaped)
                continue
            code = term.upper() if re.fullmatch(r"[A-Za-z]{2}", term) else None
            alias = code if code is not None else rf"(?i:{escaped})"
            terms.append(rf"(?<!{_IDENTIFIER_CHAR}){alias}(?!{_IDENTIFIER_CHAR})")
        countries[name] = re.compile("|".join(terms))
    if not countries:
        raise ValueError
    return countries


def country_matches(
    texts: Iterable[str], countries: dict[str, re.Pattern[str]], *, exact: bool = False,
) -> set[str]:
    texts = tuple(map(_normalize_country_text, texts))
    return {
        name for name, pattern in countries.items()
        if any((pattern.fullmatch if exact else pattern.search)(text) for text in texts)
    }


def company_mentioned(name: str, texts: Iterable[str]) -> bool:
    pattern = re.compile(rf"(?<!{_IDENTIFIER_CHAR}){re.escape(name)}(?!{_IDENTIFIER_CHAR})", re.IGNORECASE)
    return any(pattern.search(text) for text in texts)
