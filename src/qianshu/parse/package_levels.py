"""私有销售品导出快照。汇总行不能证明具体销售品。"""

import csv
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_LEVELS_FILE = Path(".private/package-levels.csv")
_COLUMNS = set(("record_type company_id company_name offer_id offer_name offer_type "
                "is_synchronized parent_offer_id offer_level_type offer_level status "
                "sale_start_date sale_end_date").split())
_SUMMARY = {"COMPANY_SUMMARY", "INVALID_COMPANY", "INVALID_TOTAL", "TIER_COUNT"}


@dataclass(frozen=True, repr=False)
class Offer:
    subject: str
    company: str
    id: str
    name: str
    kind: str
    sync: bool
    parent: str
    platform: str
    level: int | None
    on_sale: bool
    start: date
    end: date

    def active(self, day: date) -> bool:
        return self.on_sale and self.start <= day <= self.end


@dataclass(frozen=True, repr=False)
class PackageLevels:
    offers: tuple[Offer, ...] = ()

    def for_company(self, company: str, number: str) -> tuple[Offer, ...]:
        rows = tuple(row for row in self.offers if row.subject == number or row.company == company)
        if any(row.subject != number or row.company != company for row in rows):
            return ()
        return rows


def load_package_levels(path: Path = DEFAULT_LEVELS_FILE) -> PackageLevels:
    offers = []
    line = 1
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            header = reader.fieldnames or []
            if not _COLUMNS.issubset(header) or len(set(header)) != len(header):
                raise ValueError
            for row in reader:
                line = reader.line_num
                if None in row or None in row.values():
                    raise ValueError
                if row["record_type"] in _SUMMARY:
                    continue
                if (row["record_type"] != "OFFER" or row["offer_type"] not in {"基本", "附属"}
                    or row["is_synchronized"] not in {"是", "否"} or row["status"] not in {"在售", "停售"}
                    or not all(row[key] for key in ("company_id", "company_name", "offer_id", "offer_name"))
                    or bool(row["offer_level_type"]) != bool(row["offer_level"])
                    or (row["offer_level"] and not row["offer_level"].isdecimal())
                    or bool(row["parent_offer_id"]) != (row["offer_type"] == "附属")):
                    raise ValueError
                start = date.fromisoformat(row["sale_start_date"])
                end = date.fromisoformat(row["sale_end_date"])
                if start.isoformat() != row["sale_start_date"] or end.isoformat() != row["sale_end_date"] or end < start:
                    raise ValueError
                offers.append(Offer(
                    row["company_id"], row["company_name"], row["offer_id"], row["offer_name"],
                    row["offer_type"], row["is_synchronized"] == "是", row["parent_offer_id"],
                    row["offer_level_type"], int(row["offer_level"]) if row["offer_level"] else None,
                    row["status"] == "在售", start, end,
                ))
    except (OSError, UnicodeError, csv.Error, ValueError):
        logging.getLogger(__name__).warning("销售品级别表第 %d 行", line)
        return PackageLevels()
    return PackageLevels(tuple(offers))


def unique_offer(rows: tuple[Offer, ...], name: str, kind: str, day: date) -> Offer | None:
    matches = [row for row in rows if row.name == name]
    if len(matches) != 1:
        return None
    row = matches[0]
    if row.kind != kind or not row.active(day) or sum(other.id == row.id for other in rows) != 1:
        return None
    if row.kind == "基本" and row.level is not None and sum(
        other.kind == "基本" and (other.platform, other.level) == (row.platform, row.level)
        for other in rows
    ) != 1:
        return None
    return row


def copy_for(rows: tuple[Offer, ...], base: Offer, day: date) -> Offer | None:
    child = unique_offer(rows, base.name + "_copy", "附属", day)
    if base.level is None or child is None or not child.sync or child.parent != base.id:
        return None
    if (child.platform, child.level) != (base.platform, base.level):
        return None
    siblings = [row for row in rows if row.parent == base.id and row.sync and row.active(day)]
    return child if siblings == [child] else None
