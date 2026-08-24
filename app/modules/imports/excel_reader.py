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
        if value != value:               # NaN check (NaN != NaN is True) bz NaN is not equal to anything, including itself
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
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):#No need to loop here
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


# ── MONEY ────────────────────────────────────────────────────────────────────
# Symbol -> ISO code. Deliberately tiny: these are the marks that actually appear
# on the order sheets this factory receives. An unknown symbol is not an error,
# it just means the currency has to come from the order or the client.
_CURRENCY_SYMBOLS = {
    "\u20ac": "EUR", "EUR": "EUR",
    "$": "USD", "USD": "USD",
    "\u20b9": "INR", "RS.": "INR", "RS": "INR", "INR": "INR",
    "\u00a3": "GBP", "GBP": "GBP",
}

# Trailing trade terms clients staple onto a price: "€ 80,00 cif". They are not
# part of the number and they are not currency.
_PRICE_NOISE = re.compile(
    r"\b(cif|fob|fca|exw|ddp|dap|cfr|c&f|per\s*pc|per\s*piece|each|/pc)\b",
    re.IGNORECASE)


def to_money(value) -> tuple:
    """Coerce a price cell to (Decimal | None, currency | None). NEVER raises.

    WHY THIS IS NOT to_int WITH A DIVISION. Price cells carry three kinds of mess
    that quantity cells never do, and each one silently corrupts a naive parse:

      1. A CURRENCY MARK, which is also the only place the sheet says what money
         it is quoting. Stripping it and keeping the number would throw away the
         one fact that makes 80 mean something.
      2. EUROPEAN DECIMALS. "80,00" is eighty, and "1.234,50" is one thousand two
         hundred thirty four and a half — the exact reverse of the Anglo reading.
         Guessing wrong here is a 1000x costing error, so the separators are
         decided by POSITION (see below) rather than by locale configuration.
      3. TRADE TERMS. "€ 80,00 cif" is a price with an incoterm attached.

    THE SEPARATOR RULE, and why it is positional. Whichever of "." or "," appears
    LAST is the decimal point; the other is a thousands separator. That is true in
    both conventions and needs no locale setting:
        "1.234,50" -> comma last  -> 1234.50
        "1,234.50" -> period last -> 1234.50
    A lone separator is ambiguous ("80,00" vs "1,234"), so it is read as a decimal
    only when exactly two digits follow it, which is what a money fraction looks
    like; otherwise it is thousands.

    Unparseable input returns (None, None) rather than raising, because one junk
    price cell must not fail a 400-row import — the preview reports it instead.
    """
    from decimal import Decimal, InvalidOperation

    if value is None:
        return None, None
    # A real number in the cell (openpyxl gives floats for numeric cells) has no
    # separator ambiguity at all — Excel already parsed it.
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        if value != value:                       # NaN
            return None, None
        return Decimal(str(value)).quantize(Decimal("0.01")), None

    s = str(value).strip()
    if not s:
        return None, None

    upper = s.upper()
    currency = None
    for mark, code in _CURRENCY_SYMBOLS.items():
        if mark in upper:
            currency = code
            break

    s = _PRICE_NOISE.sub(" ", s)
    # Strip everything that is not part of a number, which removes the symbol,
    # the currency word and any stray text in one pass.
    s = re.sub(r"[^0-9.,\-]", "", s)
    if not s or not re.search(r"\d", s):
        return None, currency

    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        # Both present: the LAST one is the decimal point.
        if last_comma > last_dot:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif last_comma >= 0:
        tail = s[last_comma + 1:]
        s = s.replace(",", "." if len(tail) == 2 else "")
    elif last_dot >= 0:
        tail = s[last_dot + 1:]
        if len(tail) == 3 and s.count(".") == 1 and len(s.split(".")[0]) <= 3:
            # "1.234" with no other clue is a thousands separator, not 1.234 of
            # a euro — no factory quotes a jacket at one and a bit.
            s = s.replace(".", "")
    try:
        return Decimal(s).quantize(Decimal("0.01")), currency
    except (InvalidOperation, ValueError):
        return None, currency
