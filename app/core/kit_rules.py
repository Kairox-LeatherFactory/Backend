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


def accessory_size_gaps(*, lines, ordered_sizes) -> list[tuple[str, list[str]]]:
    """Which size-varying accessories have NO line for some size in the order?

    THE HOLE THIS CLOSES COSTS A SHIPMENT. A line's garment_size is what scopes it
    to one size, and a line that reaches no garment is silently absent rather than
    wrong: `merge_lines` drops it, `kit_required` comes back False for that size,
    `piece_complete` collapses to leather-and-lining, and the garment is sendable,
    shippable and missing its zip. Nothing warns, because from the garment's point
    of view the style simply declares no accessories.

    So: if ANY line for an article names a garment size, that article is
    SIZE-VARYING, and every size the order actually contains must have its own
    line. One unscoped line for the article covers everything and closes the whole
    question — that is how a generic 18L button stays one row.

    `lines` are plain dicts (category, subtype, article, garment_size,
    qty_per_piece) so this stays DB-free and mirrors into run_logic_checks.py.
    Returns [(article label, [uncovered sizes])], empty when the recipe is sound.
    """
    wanted = [l for l in (lines or [])
              if str(l.get("category") or "").upper() == "ACCESSORY"
              and float(l.get("qty_per_piece") or 0) > 0]
    sizes = [s for s in (ordered_sizes or []) if s]
    if not wanted or not sizes:
        return []

    groups: dict[tuple, list[dict]] = {}
    for line in wanted:
        key = ((line.get("subtype") or "") or None, line.get("article") or "")
        groups.setdefault(key, []).append(line)

    out: list[tuple[str, list[str]]] = []
    for (subtype, article), group in groups.items():
        # An unscoped line covers every size, so the article is not size-varying
        # and nothing can be missing from it.
        if any(not l.get("garment_size") for l in group):
            continue
        uncovered = [s for s in sizes
                     if not any(size_matches(l.get("garment_size"), s)
                                for l in group)]
        if uncovered:
            label = f"{article} ({subtype})" if subtype else str(article)
            out.append((label, uncovered))
    return sorted(out)


def accessory_size_ambiguities(*, lines) -> list[tuple[str, list[str]]]:
    """Same accessory, several material sizes, and not one of them says which
    garments it is for.

    THE OTHER HALF OF THE SIZE QUESTION, and it exists because the fix for the
    first half opened it. `garment_size` used to be GUESSED from the material size
    whenever that size read as a garment size — which quietly confined a 60cm zip
    to 4XL garments (60 is on the EU ladder), so the guess had to go. But the guess
    was also the only thing stopping the opposite failure: a DM entering ZIP 48,
    ZIP 50 and ZIP 52 meaning three garment sizes now has three UNSCOPED lines,
    every one of which applies to every garment, and the kit spends all three zips
    on one jacket.

    Neither reading can be inferred from '50' — it is a garment size on one sheet
    and a centimetre length on the next — so this asks instead of guessing. One
    line per article is never ambiguous, however its size is labelled: a single
    60cm zip on every garment is exactly what an unscoped line means.

    Returns [(article label, [the competing material sizes])].
    """
    wanted = [l for l in (lines or [])
              if str(l.get("category") or "").upper() == "ACCESSORY"
              and float(l.get("qty_per_piece") or 0) > 0
              and not l.get("garment_size")]
    groups: dict[tuple, set] = {}
    for line in wanted:
        key = ((line.get("subtype") or "") or None, line.get("article") or "")
        size = (str(line.get("size") or "").strip().upper()) or None
        if size:
            groups.setdefault(key, set()).add(size)

    out: list[tuple[str, list[str]]] = []
    for (subtype, article), sizes in groups.items():
        if len(sizes) > 1:
            label = f"{article} ({subtype})" if subtype else str(article)
            out.append((label, sorted(sizes)))
    return sorted(out)


def release_blockers(*, style_name: str, confirmed_at, no_accessories,
                     has_accessory_lines: bool,
                     has_leather_line: bool,
                     size_coverage_gaps: list | None = None,
                     size_ambiguities: list | None = None) -> list[str]:
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
    # A SIZE WITH NO LINE IS NOT A SMALLER RECIPE, IT IS NO RECIPE. See
    # accessory_size_gaps: the uncovered sizes do not get a short kit, they get
    # kit_required=False and ship complete without the accessory at all.
    for label, uncovered in (size_coverage_gaps or []):
        out.append(
            f"{style_name} declares {label} per garment size but has no line "
            f"for {', '.join(uncovered)}. Every size in this order needs its own "
            f"line, or add one line with no garment_size to cover them all — "
            f"the sizes it does cover are the only ones that will get it.")
    # AMBIGUOUS, SO ASK. '50' is a garment size on one sheet and a centimetre
    # length on the next, and the two readings differ by a whole kit per garment.
    for label, sizes in (size_ambiguities or []):
        out.append(
            f"{style_name} has {len(sizes)} {label} lines ({', '.join(sizes)}) "
            f"and none of them says which garment sizes it is for — so every "
            f"garment would be issued all {len(sizes)}. Set garment_size on each, "
            f"or combine them into one line if every garment really takes them "
            f"all.")
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
