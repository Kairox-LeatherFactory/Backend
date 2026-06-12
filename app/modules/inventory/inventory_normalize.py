"""
================================================================================
modules/procurement/inventory_normalize.py — inventory text normalization (§2b/§5)
================================================================================

PURE helpers, no DB, no I/O — the single place the messy `INVENTORY (1).xlsx`
descriptions are cleaned, so the IMPORTER (§2) and the MATCHER (§5) normalize
IDENTICALLY (a key computed one way at ingest must compare equal to a key computed
the same way at check time, or nothing matches).

The raw sheet is `DESCRIPTION | NOM | PCS | RATE`:
  - NOM is the UOM (KGS/ROLL/DCM/NOS/MTRS/REEM/PCS/CARTONS),
  - PCS the on-hand qty (frequently blank),
  - and the file is full of duplicate lots + section/ledger NOISE rows
    (FACTORY NETWORK…, MARCH MONTH-2025 USAGE, I-CATEGORY MEMBERSHIP FEE, …).
================================================================================
"""
from __future__ import annotations

import re

# Canonical UOM vocabulary (uppercased). The sheet's spellings map onto these; the
# matcher's uom_conversion reference (§6.2) then reconciles them with the BOM line.
_UOM_ALIASES = {
    "DCM": "DCM", "DM2": "DCM", "DM²": "DCM", "SQDM": "DCM",
    "DM": "DCM", "SDM": "DCM",
    "KG": "KGS", "KGS": "KGS",
    "MTR": "MTRS", "MTRS": "MTRS", "MTS": "MTRS", "M": "MTRS", "MTR.": "MTRS",
    "ROLL": "ROLL", "ROLLS": "ROLL",
    "NOS": "NOS", "NO": "NOS", "NO.": "NOS", "PC": "PCS", "PCS": "PCS",
    "PCS.": "PCS", "PIECE": "PCS", "PIECES": "PCS",
    "REEM": "REEM", "REAM": "REEM",
    "CARTON": "CARTONS", "CARTONS": "CARTONS", "CTN": "CARTONS",
    "SET": "SET", "SETS": "SET", "PKT": "PKT", "PACKET": "PKT",
}

# Boilerplate stripped before keying — present on many rows, noise for matching.
_BOILERPLATE = (
    "& PACKING CHARGES", "AND PACKING CHARGES", "PACKING CHARGES",
    "& PACKING CHARGE", "PACKING CHARGE", "& CHARGES",
)

# Row-level NOISE markers: a description containing any of these is not stock (§2b.1).
_NOISE_MARKERS = (
    "MONTH USAGE", "MEMBERSHIP FEE", "FACTORY NETWORK", "FREIGHT CHARGE",
    "FREIGHT CHARGES", "SHIPMENT", "EMBOSSING CHARGES", "MONTH-", "MONTH ",
    "PACKAGE)", "GRAND TOTAL", "SUB TOTAL", "OPENING BALANCE", "CLOSING BALANCE",
)

# Colour tokens lifted into inventory_item.color and kept inside the key.
_COLORS = (
    "BLACK", "BROWN", "GREY", "GRAY", "BLUE", "WHITE", "RED", "GREEN",
    "MAGENTA", "NAVY", "TAN", "BEIGE", "OLIVE", "LEAF", "CREAM", "PINK",
    "YELLOW", "ORANGE", "PURPLE", "MAROON", "GOLD", "SILVER", "NICKEL",
)


def canonical_uom(raw: str | None) -> str | None:
    """Map a sheet UOM cell to the canonical vocabulary, or None if blank/unknown."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    return _UOM_ALIASES.get(s, s)


def normalize_key(text: str | None) -> str:
    """The matching key (§2b.3 / §5.1): uppercase, drop boilerplate suffixes + trailing
    parentheticals, collapse every non-alphanumeric run to a single space, trim. Used on
    BOTH the inventory `description` and the BOM line so the two compare equal."""
    if not text:
        return ""
    s = str(text).upper()
    s = re.sub(r"\([^)]*\)", " ", s)               # drop "(...)" parentheticals
    for b in _BOILERPLATE:
        s = s.replace(b, " ")
    s = re.sub(r"[^A-Z0-9]+", " ", s)              # punctuation/symbols → space
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_color(text: str | None) -> str | None:
    """First recognised colour token in the text (for inventory_item.color), else None."""
    key = normalize_key(text)
    for tok in key.split():
        if tok in _COLORS:
            return "GREY" if tok == "GRAY" else tok
    return None


def tokens(key: str) -> set[str]:
    """The word set of a normalized key — the unit of containment matching (§5)."""
    return set(t for t in key.split() if t)


def bom_line_key(name: str | None, color: str | None) -> str:
    """The normalized key for a BOM line, colour folded in so colour participates in
    matching (SHEEP GLASS + BLACK → 'SHEEP GLASS BLACK')."""
    return normalize_key(f"{name or ''} {color or ''}")


def is_noise_row(description: str | None, uom: str | None,
                 qty, rate) -> bool:
    """True for a non-stock row (§2b.1): a known banner/ledger marker, or a row with no
    UOM AND no qty AND no rate (a pure section header). Recorded in the preview's
    `dropped` list, never silently swallowed."""
    if not description or not str(description).strip():
        return True
    up = str(description).upper()
    if any(m in up for m in _NOISE_MARKERS):
        return True
    has_uom = canonical_uom(uom) is not None
    has_qty = qty is not None
    has_rate = rate is not None
    return not (has_uom or has_qty or has_rate)
