"""日期格式及日历先后核对。"""

from datetime import date

from .evidence import issue, verified_value
from .protocol import FieldResult

FIELDS = ("start_date", "end_date")


def calendar_date(value: str) -> str | None:
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def verify_dates(results: dict[str, FieldResult]) -> None:
    start, end = (results[name] for name in FIELDS)
    if verified_value(start) and verified_value(end) and start.value > end.value:
        for field in FIELDS:
            results[field] = issue(results[field], "reversed_date_range")
