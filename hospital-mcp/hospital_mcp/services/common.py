"""Small shared helpers: time, money, parsing, JSON-safe output."""
import os
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..errors import Codes, HospitalError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def hospital_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("HOSPITAL_TZ", "Asia/Kolkata"))


def local_now() -> datetime:
    """Naive hospital-local time. All stored datetimes use this convention (see docs/DECISIONS.md)."""
    return datetime.now(hospital_tz()).replace(tzinfo=None, microsecond=0)


def to_minor(rupees) -> int:
    return int((Decimal(str(rupees)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def inr(minor: Optional[int]) -> float:
    return float(Decimal(minor or 0) / 100)


def bad(msg: str, **kw) -> HospitalError:
    return HospitalError(Codes.INVALID_INPUT, msg, **kw)


def not_found(what: str, hint: Optional[str] = None) -> HospitalError:
    return HospitalError(Codes.NOT_FOUND, f"{what} not found.", hint=hint)


def parse_dt(s: Any, field: str = "datetime") -> datetime:
    """Accept ISO 8601 with or without an offset; offsets are converted to hospital-local time."""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        raise bad(f"{field} must be ISO 8601, e.g. 2026-11-17T15:00:00+05:30")
    if dt.tzinfo is not None:
        dt = dt.astimezone(hospital_tz()).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def parse_date(s: Any, field: str = "date") -> date:
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        raise bad(f"{field} must be YYYY-MM-DD")


def clean(obj: Any) -> Any:
    """Make Mongo documents JSON-safe and drop internal fields."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items() if k not in ("_id", "hospital_id")}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if type(obj).__name__ == "ObjectId":
        return str(obj)
    return obj


def page_args(limit: int, page: int, max_limit: int = 20) -> tuple:
    if not 1 <= limit <= max_limit:
        raise bad(f"limit must be 1-{max_limit}.")
    if page < 1:
        raise bad("page starts at 1.")
    return limit, (page - 1) * limit
