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

THE ONE EXCEPTION — `explicit` (the DM's release-time declaration)
    Everything above is INFERENCE: we are reading a style's name and a sheet's
    colour columns and guessing. Since the release gate (breakdown → RELEASED), a
    human is asked the question outright — "does this style take a lining?" — and
    their answer is stored on `Style.needs_lining`.

    THAT ANSWER WINS, IN BOTH DIRECTIONS, and it is the only thing that can turn a
    requirement OFF. The reason is that inference has no way to be right about the
    exceptions: a style genuinely named "REESE WOOL" that is a wool SHELL with no
    lining at all cannot be released as leather-only while a name marker outranks
    the human looking at the garment. Half the point of asking is being able to
    say no.

    THE SAFETY THAT REPLACES ONE-DIRECTIONALITY IS THE AUDIT, not the algorithm:
    the declaration is stamped with the actor and the time at release, so a
    garment that skipped its lining leg can always be traced to the person who
    said it had none. `explicit=None` (never asked — every style released before
    this change) falls straight back to the inference above, so nothing already on
    the floor changes its verdict retroactively.

    A LINING CUT STILL OUTRANKS A 'NO'. If somebody has physically logged a
    LINING_CUTTING event for the piece, the lining exists as a matter of fact and
    the drawer must hold it, whatever the paperwork said — see lining_required().

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
                    has_lining_cut_event: bool = False,
                    explicit: bool | None = None) -> bool:
    """THE VERDICT. The DM's explicit answer wins; otherwise any positive signal.

    Every caller in the app goes through this, so the completeness gate, the
    store screen, the piece-state read and the dashboards cannot form different
    opinions about the same garment.

    ORDER OF AUTHORITY
        1. a logged LINING_CUTTING event — physical fact, outranks paperwork in
           the affirmative. A lining that has been cut has to be merged, whatever
           the release declared, or it goes in a drawer nobody is waiting on.
        2. `explicit` — the DM's release-time declaration. Decides in BOTH
           directions (see the module docstring); this is the only term that can
           answer False over a positive signal.
        3. inference — stored flag / SKU colour / style name, one-directional as
           before.
    """
    if has_lining_cut_event:
        return True
    if explicit is not None:
        return bool(explicit)
    if bool(stored_flag):
        return True
    if colour_signals_lining(*colour_values):
        return True
    return name_signals_lining(style_name, style_article)


def why_lining_required(*, stored_flag: bool | None = None,
                        style_name: str | None = None,
                        style_article: str | None = None,
                        colour_values=(),
                        has_lining_cut_event: bool = False,
                        explicit: bool | None = None) -> str | None:
    """The reason string for the operator, or None when no lining is required.

    The gate rejection has to be actionable. "Awaiting lining" on a garment whose
    stored flag says it needs none reads as a system fault; "its style is named
    ADELE KNIT" reads as an instruction to go and find the lining.

    MIRRORS lining_required()'s order of authority exactly — a reason that named
    a signal the verdict did not actually use would send the operator hunting for
    a lining the gate is not waiting on.
    """
    if has_lining_cut_event:
        return "a lining cut has already been logged for this piece"
    if explicit is not None:
        return ("the Direct Manager declared this style as lined when releasing it"
                if explicit else None)
    if bool(stored_flag):
        return "the breakdown sheet flagged it as lined"
    if colour_signals_lining(*colour_values):
        return "its SKU carries a lining colour"
    if name_signals_lining(style_name, style_article):
        marker = next(
            (m for m in LINING_NAME_MARKERS
             if m in f"{style_name or ''} {style_article or ''}".upper()), "")
        return f"its style name contains '{marker}'"
    return None


def lining_required_sql(needs_lining_col, style_name_col, style_article_col,
                        colour_cols=(), explicit_col=None):
    """The same rule as a SQLAlchemy boolean expression, for list/filter queries.

    Built here rather than inline at the call site for the reason this whole
    module exists: a query that filtered on a hand-written variant of the rule
    would offer the operator a send queue the server then refuses.

    `explicit_col` is `Style.needs_lining` — the DM's release-time declaration.
    When it is NOT NULL it decides outright, exactly as in lining_required(), so
    the send queue and the send itself agree about a style released as
    leather-only. Passing it is optional only so existing callers that do not
    join Style keep working; a caller that CAN join it should.

    Note it deliberately OMITS the has_lining_cut_event term — that needs a
    correlated EXISTS against production_event and is applied in Python by the
    caller, which already loads the per-piece cut history. Omitting it here is
    safe in one direction only, and it is the safe one: the SQL predicate can
    under-report a lining requirement, never over-report one, and the Python pass
    that follows adds the missing term before anything is allowed to move.
    """
    from sqlalchemy import case, func, or_

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
    inferred = or_(*terms)

    if explicit_col is None:
        return inferred
    # CASE, not an OR/AND of the two: the declaration REPLACES the inference
    # rather than joining it, which is the only shape that lets an explicit
    # False survive a style whose name contains KNIT.
    return case((explicit_col.isnot(None), explicit_col.is_(True)),
                else_=inferred)
