"""
================================================================================
core/lining_rules.py — ONE definition of "does this garment take a lining?"
================================================================================

WHY THIS FILE EXISTS (bug: a piece with no lining stage reached STORE and ran to
PACKAGE_EXPORT)

    `piece.needs_lining` is written ONCE, at breakdown upload, and never
    recomputed. On the live database that made it unreliable: two orders holding
    the SAME 17 styles came out 0-flagged and 925-flagged
    (see scripts/backfill_needs_lining.py). The completeness gate then read that
    frozen flag as gospel:

        complete = leather_in and (lining_in or not needs_lining)

    …so a KNIT jacket whose flag said False was "complete" on its leather alone.
    It received, it sent, the merge gate opened, and the garment walked the whole
    chain to PACKAGE_EXPORT having never had a lining cut. That is the bug.

THE RULE NOW
    The stored flag is EVIDENCE, not the verdict. A garment needs a lining if ANY
    of these say so:

      1. `piece.needs_lining` is True                     (the stored flag)
      2. the SKU carries a lining colour                  (knit_color / nylon_color)
      3. the style NAME/ARTICLE carries a lining marker   (KNIT, WOOL, FUR, …)
      4. a LINING_CUTTING event already exists on the piece — somebody has
         physically cut a lining for it, which settles the question outright

    The OR is deliberate and one-directional: every source can only ever ADD a
    lining requirement, never remove one. A stale False can no longer let a lined
    garment through, and a genuinely leather-only garment (no flag, no colour, no
    marker, no lining cut) still passes on its leather alone, so nothing that used
    to be sendable stops being sendable except the class that was wrong.

WHY THE MARKERS LIVE HERE AND NOT IN premint.py
    premint owns the WRITE (what gets stored at upload); the drawers module owns
    the GATE (what is allowed to move). Two copies of the vocabulary would drift,
    and the drift is silent: the importer would flag a style the gate did not
    recognise, or the reverse. premint imports LINING_NAME_MARKERS from here.

PURE ON PURPOSE
    Nothing in this module touches a session or a model class — it takes strings
    and booleans. `core` may not import `app.modules` (import-linter's
    core-independence contract), and keeping it pure is also what lets the unit
    tests exercise the rule with no database at all.
================================================================================
"""
from __future__ import annotations

# Style-name tokens that are positive evidence of a lining / second component.
# KNIT and WOOL are lining materials in their own right (MaterialSubtype.KNIT,
# CLAUDE.md §5 "Lining / knit"); FUR and the DETACH/VEST companion pieces are a
# second component that must be merged in the drawer before line-stitching.
# EDIT THIS LIST, not the functions — it is the whole vocabulary, and both the
# importer and the completeness gate read it.
LINING_NAME_MARKERS: tuple[str, ...] = (
    "KNIT", "WOOL", "FUR", "LINING", "NYLON", "QUILT", "DETACH", "VEST", "MIX",
)

# The SKU columns that, when populated, name a lining colour.
LINING_COLOUR_FIELDS: tuple[str, ...] = (
    "knit_color", "nylon_color", "lining_color", "lining_type",
)

_BLANKS = {"", "NA", "N/A", "NONE", "-"}


def is_blank(val) -> bool:
    """True for None, empty, and the placeholder strings the sheets actually use."""
    return not val or str(val).strip().upper() in _BLANKS


def name_signals_lining(style_name: str | None,
                        style_article: str | None = None) -> bool:
    """Does the style's own name/article say it carries a lining?

    Source 2 of the rule. 8 of the 17 real styles are named ADELE KNIT,
    FLAVIO KNIT + FUR DETACH, REESE WOOL … and a KNIT style has a knit lining by
    definition.
    """
    haystack = f"{style_name or ''} {style_article or ''}".upper()
    return any(marker in haystack for marker in LINING_NAME_MARKERS)


def colour_signals_lining(*colour_values) -> bool:
    """Does any supplied SKU colour field name a lining colour? (Source 1.)"""
    return any(not is_blank(v) for v in colour_values)


def lining_required(*, stored_flag: bool | None = None,
                    style_name: str | None = None,
                    style_article: str | None = None,
                    colour_values=(),
                    has_lining_cut_event: bool = False) -> bool:
    """THE VERDICT. Any positive signal wins; nothing can cancel one.

    Every caller in the app goes through this, so the completeness gate, the
    store screen, the piece-state read and the dashboards cannot form different
    opinions about the same garment.
    """
    if bool(stored_flag):
        return True
    if has_lining_cut_event:
        return True
    if colour_signals_lining(*colour_values):
        return True
    return name_signals_lining(style_name, style_article)


def why_lining_required(*, stored_flag: bool | None = None,
                        style_name: str | None = None,
                        style_article: str | None = None,
                        colour_values=(),
                        has_lining_cut_event: bool = False) -> str | None:
    """The reason string for the operator, or None when no lining is required.

    The gate rejection has to be actionable. "Awaiting lining" on a garment whose
    stored flag says it needs none reads as a system fault; "its style is named
    ADELE KNIT" reads as an instruction to go and find the lining.
    """
    if has_lining_cut_event:
        return "a lining cut has already been logged for this piece"
    if colour_signals_lining(*colour_values):
        return "its SKU carries a lining colour"
    if name_signals_lining(style_name, style_article):
        marker = next(
            (m for m in LINING_NAME_MARKERS
             if m in f"{style_name or ''} {style_article or ''}".upper()), "")
        return f"its style name contains '{marker}'"
    if bool(stored_flag):
        return "the breakdown sheet flagged it as lined"
    return None


def lining_required_sql(needs_lining_col, style_name_col, style_article_col,
                        colour_cols=()):
    """The same rule as a SQLAlchemy boolean expression, for list/filter queries.

    Built here rather than inline at the call site for the reason this whole
    module exists: a query that filtered on a hand-written variant of the rule
    would offer the operator a send queue the server then refuses.

    Note it deliberately OMITS the has_lining_cut_event term — that needs a
    correlated EXISTS against production_event and is applied in Python by the
    caller, which already loads the per-piece cut history. Omitting it here is
    safe in one direction only, and it is the safe one: the SQL predicate can
    under-report a lining requirement, never over-report one, and the Python pass
    that follows adds the missing term before anything is allowed to move.
    """
    from sqlalchemy import func, or_

    terms = [needs_lining_col.is_(True)]
    for col in colour_cols:
        terms.append(
            col.isnot(None) & (func.upper(func.trim(col)).notin_(sorted(_BLANKS)))
        )
    haystack = func.upper(
        func.coalesce(style_name_col, "") + " " + func.coalesce(style_article_col, "")
    )
    for marker in LINING_NAME_MARKERS:
        terms.append(haystack.like(f"%{marker}%"))
    return or_(*terms)
