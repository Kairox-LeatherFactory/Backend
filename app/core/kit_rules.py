"""
================================================================================
core/kit_rules.py — the accessory-kit predicates, as pure functions
================================================================================
WHY A CORE MODULE AND NOT A SERVICE METHOD. These four answers are asked from
five different places — the release gate (imports/breakdown), the store scan and
the drawer list (drawers/service), the scan payloads (barcode/service) and the
production log — and three of them are on hot paths that must not import a
service to ask a yes/no question. core/lining_rules.py exists for exactly this
reason and this module is its sibling: same shape, same discipline.

CORE IMPORTS NOTHING FROM app.modules. Everything here takes plain values, so
the whole file is testable with no database and is mirrored in
verify/run_logic_checks.py, which runs with no dependencies at all.

THE ONE RULE THAT MATTERS MOST is `drawer_complete`. Every style that predates
the material spec has no accessory lines, so `kit_required` is False for it, so
the new term collapses to True and the predicate computes exactly what it
computed before this feature existed. That is not luck — it is the reason the
requirement is keyed on "this style declares accessories" rather than on a
global switch, and every caller must keep it that way.
================================================================================
"""
from __future__ import annotations

from app.core.enums import KitStatus


def drawer_complete(*, leather_in: bool, lining_in: bool, accessories_in: bool,
                    needs_lining: bool, kit_required: bool) -> bool:
    """Does this drawer hold everything the garment in it is ever going to get?

    COMPLETENESS IS NOT THE SAME QUESTION AS THE DRAWER'S STATE. `state` names
    what is physically in there (HOLDING_LEATHER, HOLDING_BOTH); this says
    whether anything is still owed. A leather-only piece is complete on leather
    alone, and a style with no accessory spec is complete without a kit.

    This is the predicate behind RECEIVED and behind send_batch — an unkitted
    drawer must not leave the store — so all of its callers must use THIS
    function rather than re-spelling the three clauses.
    """
    return (bool(leather_in)
            and (bool(lining_in) or not needs_lining)
            and (bool(accessories_in) or not kit_required))


def kit_satisfied(*, accessories_in: bool, kit_required: bool) -> bool:
    """The accessory clause on its own — for callers that already know the rest.

    Named separately because the auto-RECEIVE branch needs it beside a stricter
    leather/lining test (both booleans physically true, not merely "complete"),
    and inlining `accessories_in or not kit_required` in two places is how the
    two drift.
    """
    return bool(accessories_in) or not kit_required


def auto_receive_ready(*, leather_in: bool, lining_in: bool,
                       accessories_in: bool, kit_required: bool) -> bool:
    """May the drawer advance to RECEIVED by itself, with no human confirming?

    STRICTER THAN `drawer_complete`, DELIBERATELY, and the difference is scar
    tissue. Auto-receive first fired on completeness, which let a piece flagged
    needs_lining=False jump to RECEIVED the moment its leather was stored — over
    an empty lining side, on the strength of a flag known to be wrong for 925 of
    1,425 pieces in one live order. Two booleans set by two physical scans are
    not a guess, so auto-receive asks for both parts literally present.

    The kit clause is the ordinary `kit_satisfied`: for a style with no accessory
    spec it is True, so this reduces exactly to the pre-existing
    `leather_in and lining_in`.
    """
    return (bool(leather_in) and bool(lining_in)
            and kit_satisfied(accessories_in=accessories_in,
                              kit_required=kit_required))


def kit_status(*, kit_required: bool, required_total: float, issued_total: float,
               unresolved: int = 0) -> str:
    """Where a piece stands against its accessory spec, as a KitStatus value.

    NOT_REQUIRED IS NOT "NOTHING ISSUED". It means the style declares no
    accessories at all, and it tells the screen to HIDE the checklist rather than
    render an empty one. Collapsing it into PENDING would show every garment
    released before this feature an empty accessory panel it can never satisfy.

    An UNRESOLVED line (its article matches no lot) holds the piece at PARTIAL
    even when everything resolvable was issued: the kit is not complete, the
    drawer must not be sendable, and reporting ISSUED there would be a lie that
    lets an unkitted garment onto the line.
    """
    if not kit_required:
        return KitStatus.NOT_REQUIRED.value
    if unresolved > 0:
        return KitStatus.PARTIAL.value
    if issued_total <= 0:
        return KitStatus.PENDING.value
    if issued_total + 1e-9 >= required_total:
        return KitStatus.ISSUED.value
    return KitStatus.PARTIAL.value


def release_blockers(*, style_name: str, confirmed_at, no_accessories,
                     has_accessory_lines: bool,
                     has_leather_line: bool) -> list[str]:
    """Why this style may NOT be released yet. Empty list = it may.

    THE GATE EXISTS BECAUSE RELEASE IS THE LAST MOMENT ANYONE CAN BE ASKED. After
    it the style has barcoded garments in drawers and its recipe is frozen and
    being spent; before it the sheet is still a spreadsheet row. So "what does one
    of these take?" is asked here, exactly where "does this take a lining?"
    already is.

    THREE-STATE `no_accessories`, and the NULL matters. A recipe with no accessory
    lines is ambiguous on its own: it could be a garment that genuinely takes none,
    or one whose buttons nobody has entered yet. Releasing the second kind
    silently is how a whole order reaches the store with no kit — so an empty
    accessory list needs an explicit True to pass, and NULL (nobody asked) does
    not count.

    A MISSING LINING LINE IS NOT A BLOCKER. Lining consumption has been optional
    on the cut path since bugs #9/#10 — the client confirmed every lining field is
    optional — so requiring it here would contradict the ledger rule downstream.

    Returns SENTENCES, not codes: they go straight into the existing per-style
    `rejected[]` on the release response, which the DM reads verbatim.
    """
    out: list[str] = []
    if confirmed_at is None:
        out.append(
            f"{style_name}'s material spec has not been confirmed. Enter the "
            f"per-piece consumption (PUT /styles/{{id}}/material-spec) and "
            f"confirm it.")
    if not has_leather_line:
        out.append(
            f"{style_name} has no LEATHER line — the dcm consumed per piece is "
            f"what the material ledger and the costing are built on. Add it "
            f"before releasing.")
    if not has_accessory_lines and no_accessories is not True:
        out.append(
            f"{style_name}'s spec names no accessories and nobody has declared "
            f"that it needs none. Add the accessory lines, or confirm the spec "
            f"with no_accessories: true.")
    return out
