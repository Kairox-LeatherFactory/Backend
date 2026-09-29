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

THE ONE RULE THAT MATTERS MOST is `piece_complete`. Every style that predates
the material spec has no accessory lines, so `kit_required` is False for it, so
the new term collapses to True and the predicate computes exactly what it
computed before this feature existed. That is not luck — it is the reason the
requirement is keyed on "this style declares accessories" rather than on a
global switch, and every caller must keep it that way.
================================================================================
"""
from __future__ import annotations

from app.core.enums import KitStatus


def piece_complete(*, leather_in: bool, lining_in: bool, accessories_in: bool,
                   needs_lining: bool, kit_required: bool) -> bool:
    """Does the store hold everything this garment is ever going to get?

    THIS FUNCTION NEVER WAS ABOUT DRAWERS. Every argument is a fact about the
    GARMENT — its leather arrived, its lining arrived, its kit was issued, it
    takes a lining at all, it declares accessories at all. The drawer was only
    where those facts happened to be written down, which is exactly why removing
    the drawer cost this predicate a rename and nothing else.

    COMPLETENESS IS NOT THE SAME QUESTION AS THE DRAWER'S STATE. `state` names
    what is physically in there (HOLDING_LEATHER, HOLDING_BOTH); this says
    whether anything is still owed. A leather-only piece is complete on leather
    alone, and a style with no accessory spec is complete without a kit.

    This is the predicate behind RECEIVED and behind send_batch — an unkitted
    garment must not leave the store — so all of its callers must use THIS
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

    STRICTER THAN `piece_complete`, DELIBERATELY, and the difference is scar
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
    garment must not be sendable, and reporting ISSUED there would be a lie that
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


def size_matches(want, have) -> bool:
    """Is a line scoped to `want` a line for a garment of size `have`?

    THE ONE SIZE RULE, and it lives here because three places need it and a
    second spelling of it is a second answer. `StyleSpecService.applies_to_size`
    delegates here, the release-gate coverage check below uses it, and the store's
    packet scan compares a packet's size against a garment's with it.

    NULL on either side means "do not drop it": a line with no garment_size is
    for every size, and a piece whose size is unknown must not lose its recipe.
    '52' and 'L' are the same garment on the Italian ladder.
    """
    if not want:
        return True
    if not have:
        return True
    from app.core.leather_norms import normalise_size
    a, b = str(want).strip().upper(), str(have).strip().upper()
    if a == b:
        return True
    na, nb = normalise_size(a), normalise_size(b)
    return na is not None and na == nb


def accessory_sku_gaps(*, lines, ordered_skus) -> list[tuple[str, list[str]]]:
    """Which accessories are missing from some SKU the order contains?

    THE ONE SIZE RULE THAT REPLACED TWO. An accessory line now names the SKU it is
    for — a SKU being a colour and a size together — so "does this line reach this
    garment" is a single equality and the whole class of size-scoping bugs is gone
    with it. What is left to check is COVERAGE: an article somebody put on one SKU
    and forgot on another.

    WHY THAT IS WORTH A BLOCKER. A SKU with no line for an article is not a garment
    with a shorter recipe — it is a garment the store believes needs nothing. Its
    `kit_required` comes back False, completeness collapses to leather-and-lining,
    and it is sendable, shippable and missing its zip. Nothing downstream complains,
    because from that garment's point of view the style declares no accessories.

    THIS SUPERSEDES `accessory_size_gaps` AND `accessory_size_ambiguities`. Both
    existed only to police style-wide lines: one asked whether a size-scoped article
    covered every ordered size, the other whether several unscoped material sizes of
    one article had been left for the system to guess between. Neither question can
    be asked of a line that names its SKU, and both are simpler dead than kept.

    `lines` are plain dicts (category, subtype, article, sku_id, qty_per_piece) and
    `ordered_skus` a list of (sku_id, label), so this stays DB-free and testable with
    no fixtures. Returns [(article label, [uncovered SKU labels])].
    """
    wanted = [l for l in (lines or [])
              if str(l.get("category") or "").upper() == "ACCESSORY"
              and float(l.get("qty_per_piece") or 0) > 0
              and l.get("sku_id")]
    skus = [(sid, label) for sid, label in (ordered_skus or []) if sid]
    if not wanted or not skus:
        return []

    # article -> the SKUs that DO have it
    covered: dict[tuple, set] = {}
    for line in wanted:
        key = ((line.get("subtype") or "") or None, line.get("article") or "")
        covered.setdefault(key, set()).add(line["sku_id"])

    out: list[tuple[str, list[str]]] = []
    for (subtype, article), have in covered.items():
        missing = [label for sid, label in skus if sid not in have]
        if missing:
            label = f"{article} ({subtype})" if subtype else str(article)
            out.append((label, sorted(missing)))
    return sorted(out)


def release_blockers(*, style_name: str, confirmed_at, no_accessories,
                     has_accessory_lines: bool,
                     has_leather_line: bool,
                     sku_coverage_gaps: list | None = None) -> list[str]:
    """Why this style may NOT be released yet. Empty list = it may.

    THE GATE EXISTS BECAUSE RELEASE IS THE LAST MOMENT ANYONE CAN BE ASKED. After
    it the style has barcoded garments on the floor and its recipe is frozen and
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
    # A SKU WITH NO LINE IS NOT A SMALLER RECIPE, IT IS NO RECIPE. See
    # accessory_sku_gaps: the uncovered garments do not get a short kit, they get
    # kit_required=False and ship complete without the accessory at all.
    for label, uncovered in (sku_coverage_gaps or []):
        out.append(
            f"{style_name} declares {label} on some colourways but not on "
            f"{', '.join(uncovered)}. Every ordered SKU needs its own line — the "
            f"ones it covers are the only garments that will get it. Add the "
            f"missing lines, or send the accessory with apply_to: \"ALL_SKUS\" to "
            f"cover them all at once.")
    return out


# ── COMPATIBILITY ────────────────────────────────────────────────────────────
# `drawer_complete` WAS this function's name while the store was a drawer, kept
# as an alias so the drawers module could keep calling something real while it
# was being retired. That module is deleted, so the alias goes with it, exactly
# as the note here always said it would. Nothing in app/ calls it.
#
# (Leaving the old text below for the one line of history it carries:
# keeps importing something real during the changeover. New code calls
# piece_complete; when the drawer module goes, this goes with it.)
