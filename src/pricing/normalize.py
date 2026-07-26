"""Deterministic value normalization for scraped payloads.

Prices arrive in many shapes ("$329,990", "From $329,990", 329990, "329990.00").
Everything funnels through here so the same input always yields the same output.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_PRICE_RE = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


def parse_price(value: object) -> Decimal | None:
    """Extract a numeric price. Returns None for missing/unpriced values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        d = Decimal(str(value))
        return d if d > 0 else None
    if isinstance(value, Decimal):
        return value if value > 0 else None
    m = _PRICE_RE.search(str(value))
    if not m:
        return None
    try:
        d = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return d if d > 0 else None


def parse_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"[0-9][0-9,]*", str(value))
    if not m:
        return None
    return int(m.group(0).replace(",", ""))


def parse_decimal(value: object) -> Decimal | None:
    """For beds/baths style fields that may be '2.5' or 2.5."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def parse_date(value: object) -> date | None:
    """Parse common date shapes; returns None rather than guessing."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            continue
    return None


def identity_hash(
    sq_ft: int | None, beds: Decimal | None, baths: Decimal | None, stories: int | None
) -> str:
    """md5(sq_ft|beds|baths|stories) — used to detect plan renames."""
    parts = "|".join("" if v is None else str(v) for v in (sq_ft, beds, baths, stories))
    return hashlib.md5(parts.encode()).hexdigest()
