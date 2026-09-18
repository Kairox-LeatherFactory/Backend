"""
================================================================================
core/leather_norms.py — how much hide one garment takes, by size
================================================================================
WHY A DEFAULT TABLE EXISTS AT ALL

    The cutting sheet has to OPEN with sheets already allocated, or the manager is
    back to picking ten hides by hand for every garment — which is the Excel work
    this feature exists to delete. To allocate, it needs a target: roughly how many
    dcm does one garment of this size take?

    Nobody supplies that number. The DM is never asked for per-piece consumption at
    release, so for most styles there is simply no figure in the database. The
    alternatives were to demand one (a new form the DM would have to fill for every
    style before cutting could start) or to estimate. This module estimates.

THE ESTIMATE IS MEANT TO BE WRONG, AND THAT IS SAFE
    Every cell of the cutting row is editable, and the cutter reports back what was
    actually needed — a sheet short, a sheet over — before the row is approved. So
    the allocator's job is not to be right, it is to be CLOSE ENOUGH that the
    manager edits one or two cells instead of entering ten. An estimate 15% out
    costs one added sheet; no estimate at all costs ten manual picks per garment.

    `style_material_spec` WINS WHENEVER IT EXISTS. When a DM has entered the
    leather line for a style (qty_per_piece, in dcm), that is a real measurement of
    a real garment and this table must not override it. See
    leather_target_dcm() — the spec is the first branch, and this table is the
    fallback for the styles that have none.

WHERE THE NUMBERS COME FROM
    Two independent sources that agree:

    1. The trade figure. A men's leather jacket takes 45-55 sq ft of hide.
       1 sq ft = 9.2903 dcm², so that is 418-511 dcm.

    2. THE FACTORY'S OWN CUTTING SHEET. Six rows off Kumar's screen:
           6 sheets / 273 dcm      5 sheets / 242 dcm      7 sheets / 346 dcm
           7 sheets / 324 dcm      6 sheets / 277 dcm      6 sheets / 279 dcm
       Every row averages 46-49 dcm per sheet — a goat/sheep hide at 5-7 sq ft.
       At ~46 dcm a hide, a 400 dcm garment is ~9 sheets, which is the "7 to 12
       sheets for one garment" the factory quotes.

    So: M is anchored at 400 dcm, and each alpha size step is ~30 dcm. That lands
    the whole ladder inside the trade's 418-511 band around L/XL and gives 7-12
    sheets across the range.

    Note the factory's own rows run 5-7 sheets (~290 dcm), BELOW the 7-12 they
    quote — those rows are mid-cut, not finished garments. The table sits between
    the two readings deliberately; "near enough, then edit" is the contract.

CORE IMPORTS NOTHING FROM app.modules — plain values in, plain values out, so this
is testable with no database, like kit_rules.py and lining_rules.py beside it.
================================================================================
"""
from __future__ import annotations

import re

# One hide, on this factory's own numbers. Used only to turn a dcm target into a
# human "≈ N sheets" hint; the allocator itself sums REAL sheet measurements.
TYPICAL_SHEET_DCM = 46.0

# The ladder. M is the anchor; ±30 dcm per alpha step.
_ALPHA_DCM: dict[str, float] = {
    "XXS": 310.0,
    "XS":  340.0,
    "S":   370.0,
    "M":   400.0,
    "L":   430.0,
    "XL":  465.0,
    "XXL": 500.0,
    "XXXL": 540.0,
    "4XL": 580.0,
    "5XL": 620.0,
}

# Spellings of the same size. "2XL" and "XXL" are one size, and a sheet headed
# "XXL/54" is the same garment as one headed "54".
_ALPHA_ALIASES: dict[str, str] = {
    "2XL": "XXL", "3XL": "XXXL", "XXXXL": "4XL", "XXXXXL": "5XL",
    "2XS": "XXS",
}

# THE ITALIAN LADDER, because it is what the real order sheets carry. John Peter,
# GGZ, NIPAL and KJ all size in EU numbers (38-62) and never in letters — see the
# breakdown importer's size band. Mapping them onto the same alpha rungs keeps ONE
# table of truth instead of two that drift.
_EU_TO_ALPHA: dict[int, str] = {
    44: "XS", 46: "XS", 48: "S", 50: "M", 52: "L",
    54: "XL", 56: "XXL", 58: "XXXL", 60: "4XL", 62: "5XL",
}

# Women's numeric sizing tops out far lower than men's outerwear, so a bare "38"
# is ambiguous — it is an EU 38 jacket (small) either way, which is why the EU
# table starts at 44 and anything below it falls to the smallest rung rather than
# being read as a men's 44+.
_EU_MIN, _EU_MAX = 30, 70


def normalise_size(raw: str | None) -> str | None:
    """'  xxl/54 ' -> 'XXL'. The one place a size label is canonicalised.

    Takes whatever the breakdown sheet printed — the importer stores size labels
    VERBATIM on purpose, so this sees ' xl ', 'XXL/54', '48/M' and '52' — and
    returns an alpha rung, or None when it cannot tell.

    A COMBINED LABEL IS READ FROM ITS ALPHA HALF FIRST ('XXL/54' -> XXL), because
    that half is unambiguous; the numeric half needs the EU table and a guess about
    which country's ladder it is on.
    """
    if raw is None:
        return None
    token = re.sub(r"[\s]+", "", str(raw)).upper()
    if not token:
        return None

    # Split combined labels: XXL/54, 48/M, S-7.
    parts = [p for p in re.split(r"[/\-]", token) if p]
    for part in parts:                       # alpha half wins
        canon = _ALPHA_ALIASES.get(part, part)
        if canon in _ALPHA_DCM:
            return canon
    for part in parts:                       # then the numeric half
        if part.isdigit():
            n = int(part)
            if _EU_MIN <= n <= _EU_MAX:
                if n in _EU_TO_ALPHA:
                    return _EU_TO_ALPHA[n]
                # Between rungs (odd numbers, 45/47/…): take the next rung up,
                # which over-allocates rather than under-allocates. A sheet too
                # many is handed back; a sheet too few stops a cutter mid-garment.
                for eu in sorted(_EU_TO_ALPHA):
                    if eu >= n:
                        return _EU_TO_ALPHA[eu]
                return "5XL"
    return None


def baseline_dcm(size: str | None) -> float:
    """The estimated dcm one garment of this size takes. Never raises.

    An unrecognised size falls back to M — the middle of the ladder, so the error
    is bounded in both directions. Returning 0 or None instead would open the
    cutting row with no sheets at all, which is the manual-picking outcome this
    exists to avoid.
    """
    rung = normalise_size(size)
    return _ALPHA_DCM.get(rung or "M", _ALPHA_DCM["M"])


def leather_target_dcm(*, size: str | None,
                       spec_dcm_per_piece: float | None = None) -> tuple:
    """How much hide to allocate for one garment, and on whose authority.

    Returns (target_dcm, source) where source is "style_spec" or "size_baseline",
    so the screen can tell the manager whether the number came from the style's own
    confirmed recipe or from this module's estimate. That distinction matters: a
    spec figure is a measurement somebody signed off, and a baseline figure is a
    guess he should expect to correct.

    THE SPEC WINS, AND ONLY A POSITIVE SPEC WINS. A style whose leather line was
    left at zero is not a measurement of a garment that takes no leather — it is an
    unfilled field, and honouring it would allocate nothing and open an empty row.
    """
    if spec_dcm_per_piece is not None and float(spec_dcm_per_piece) > 0:
        return float(spec_dcm_per_piece), "style_spec"
    return baseline_dcm(size), "size_baseline"


def expected_sheet_count(target_dcm: float,
                         typical_sheet_dcm: float = TYPICAL_SHEET_DCM) -> int:
    """≈ how many hides a target implies — a HINT for the screen, never the plan.

    The allocator picks real sheets and sums their real measurements; this only
    answers "about how many am I about to be handed?" before any are picked.
    """
    if target_dcm <= 0 or typical_sheet_dcm <= 0:
        return 0
    return max(1, round(target_dcm / typical_sheet_dcm))
