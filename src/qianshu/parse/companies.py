"""读取每份申请使用的私有公司对照表。"""

import csv
import logging
import re
from dataclasses import dataclass, field
from io import StringIO
from itertools import chain
from pathlib import Path

from .scope import company_mentioned, read_countries

DEFAULT_COMPANIES_FILE = Path(".private/companies.tsv")
_HEADER = ["国家", "关键词", "公司内部编号", "公司显示名"]
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompanyRow:
    display_name: str
    internal_number: str | None = field(repr=False)


type CompanyTable = dict[str, dict[str, CompanyRow]]


@dataclass(frozen=True)
class CompanyRules:
    countries: dict[str, re.Pattern[str]]
    table: CompanyTable | None = field(repr=False)

    @property
    def names(self) -> dict[str, list[str]]:
        return {country: [row.display_name for row in rows.values()]
                for country, rows in (self.table or {}).items()}


def _warn_row(line: int) -> None:
    _LOG.warning("公司对照表第 %d 行", line)


def load_company_rules(rules_dir: Path, path: Path, *, legacy: bool = False) -> CompanyRules:
    countries = read_countries((rules_dir / "countries.txt").read_text(encoding="utf-8"))
    try:
        raw = path.read_bytes()
    except OSError:
        _LOG.warning("公司对照表不可读")
        return CompanyRules(countries, None)
    try:
        table = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        line = len(re.findall(rb"\r\n|\r|\n", raw[:error.start])) + 1
        _warn_row(line)
        return CompanyRules(countries, None)
    return CompanyRules(countries, _read_companies(table, countries, legacy=legacy))


def _read_companies(
    table: str, countries: dict[str, re.Pattern[str]], *, legacy: bool,
) -> CompanyTable | None:
    source = StringIO(table, newline="")
    companies: CompanyTable = {}
    if legacy:
        for line, text in enumerate(source, 1):
            if text.lstrip().startswith("#") or (not text.strip() and "\t" not in text):
                continue
            cells = [cell.strip() for cell in text.rstrip("\r\n").split("\t")]
            if not _add_company(companies, countries, cells, line, legacy=True):
                _warn_row(line)
                return None
        return companies or None

    first = source.readline()
    delimiter = "\t" if "\t" in first else ","
    reader = csv.reader(chain((first,), source), delimiter=delimiter, strict=True)
    line = 1
    try:
        if next(reader, None) != _HEADER:
            _warn_row(line)
            return None
        while True:
            line = reader.line_num + 1
            cells = next(reader, None)
            if cells is None:
                break
            if not cells or (len(cells) == 1 and not cells[0].strip()):
                continue
            if not _add_company(companies, countries, cells, line, legacy=False):
                _warn_row(line)
                return None
    except csv.Error:
        _warn_row(line)
        return None
    return companies or None


def _add_company(
    companies: CompanyTable, countries: dict[str, re.Pattern[str]], cells: list[str], line: int, *, legacy: bool,
) -> bool:
    country = cells[0].strip()
    if not country or len(cells) not in ((2, 3) if legacy else (4,)):
        return False
    details = [cell.strip() for cell in cells[1:]]
    if legacy:
        display_name, *keywords = details
        number = None
        keyword = keywords[0] if keywords else ""
        complete = display_name and (not keywords or keyword)
    else:
        keyword, number, display_name = details
        complete = display_name and number
    if not complete:
        return False
    if country not in countries:
        _warn_row(line)
        return True
    rows = companies.setdefault(country, {})
    keyword = keyword.casefold()
    if not keyword and "" in rows:
        return False
    if keyword and any(existing and (company_mentioned(keyword, [existing])
                                     or company_mentioned(existing, [keyword])) for existing in rows):
        return False
    rows[keyword] = CompanyRow(display_name, number)
    return True
