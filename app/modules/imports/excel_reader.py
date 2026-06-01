"""
excel_reader.py — Low-level, defensive cell reading.

Real spreadsheets are messy: trailing spaces, numbers stored as text, merged
cells, stray formatting. Every other layer reads cells THROUGH these helpers,
never directly, so the messiness is handled in exactly one place.
"""
from __future__ import annotations
import re
from datetime import datetime, date


def clean_str(value) -> str | None:
    """Return a trimmed string, or None if effectively empty.

    Why: '  CARNABY ' and 'CARNABY' must be treated as the same value, or we
    create duplicate styles. Collapses internal runs of whitespace too.
    """
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    return re.sub(r"\s+", " ", s)


def to_int(value) -> int | None:
    """Coerce a cell to an int, tolerating '12', 12.0, ' 12 ', or junk.

    Why: Excel may store a quantity as the float 12.0 or the text '12'. We want
    a clean int 12, and None for anything that isn't a positive whole number.
    """
    if value is None:
        return None
    if isinstance(value, bool):          # bools are ints in Python; reject them
        return None
    if isinstance(value, (int, float)):
        if value != value:               # NaN check (NaN != NaN is True)
            return None
        return int(value)
    s = str(value).strip()
    if re.fullmatch(r"-?\d+(\.0+)?", s):  # "12" or "12.0"
        return int(float(s))
    return None


def to_date(value) -> date | None:
    """Parse a date from a datetime cell or common dd/mm/yyyy text."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def looks_like_week_period(text: str) -> bool:
    """True if the text contains a date range like '14/01/2026 TO 22/01/2026'."""
    return bool(re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", text or ""))


# Tokens that mark non-data rows in production sheets. Compared case-insensitively
# against the row's label cell.
SUMMARY_TOKENS = ("qty :", "qty:", "rate :", "rate:", "total :", "total:",
                  "overall total", "overall-pcs", "grand total")


def is_summary_label(text: str | None) -> str | None:
    """Classify a production-sheet label cell. Returns a tag or None."""
    if not text:
        return None
    low = text.strip().lower()
    if low.startswith("qty"):
        return "QTY"
    if low.startswith("rate"):
        return "RATE"
    if low.startswith("total"):
        return "TOTAL"
    if low.startswith("overall"):
        return "OVERALL"
    return None
