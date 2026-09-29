"""
UNIT · the accessory-kit predicates. No database, no fixtures.

THE ONE THAT MATTERS MOST is the back-compatibility proof: for every style with
no accessory spec, `kit_required` is False and the completeness rule must reduce
EXACTLY to the two clauses it had before this feature existed. That is asserted
here over the whole truth table rather than trusted, because it is the claim the
entire safety argument for shipping this rests on — and a regression in it would
show up on the floor as drawers that will not receive, not as a test failure
somewhere obvious.
"""
import itertools

import pytest

from app.core.enums import KitStatus
from app.core.kit_rules import (accessory_label, accessory_line_state,
                                auto_receive_ready, kit_satisfied, kit_status,
                                piece_complete, release_blockers, size_matches,
                                skus_without_accessories)

BOOLS = (False, True)


# ══════════════════════════════ THE BACK-COMPATIBILITY PROOF
@pytest.mark.parametrize("leather,lining,accessories,needs_lining",
                         list(itertools.product(BOOLS, BOOLS, BOOLS, BOOLS)))
def test_with_no_accessory_spec_completeness_is_the_old_two_clause_rule(
        leather, lining, accessories, needs_lining):
    old_rule = leather and (lining or not needs_lining)
    assert piece_complete(
        leather_in=leather, lining_in=lining, accessories_in=accessories,
        needs_lining=needs_lining, kit_required=False) is old_rule


@pytest.mark.parametrize("leather,lining,accessories",
                         list(itertools.product(BOOLS, BOOLS, BOOLS)))
def test_with_no_accessory_spec_auto_receive_is_the_old_both_parts_rule(
        leather, lining, accessories):
    assert auto_receive_ready(
        leather_in=leather, lining_in=lining, accessories_in=accessories,
        kit_required=False) is (leather and lining)


# ══════════════════════════════ what the new clause actually adds
def test_a_kit_required_drawer_is_incomplete_until_it_is_kitted():
    args = dict(leather_in=True, lining_in=True, needs_lining=True,
                kit_required=True)
    assert piece_complete(accessories_in=False, **args) is False
    assert piece_complete(accessories_in=True, **args) is True


def test_auto_receive_is_stricter_than_completeness():
    """A leather-only piece is COMPLETE on leather alone, but a drawer never
    receives itself on one part — the flag that would decide it (needs_lining) is
    wrong for 925 of 1,425 pieces in one live order, so a human confirms."""
    assert piece_complete(leather_in=True, lining_in=False, accessories_in=True,
                           needs_lining=False, kit_required=True) is True
    assert auto_receive_ready(leather_in=True, lining_in=False,
                              accessories_in=True, kit_required=True) is False


def test_kit_satisfied_degenerates_to_true_without_a_spec():
    assert kit_satisfied(accessories_in=False, kit_required=False) is True
    assert kit_satisfied(accessories_in=False, kit_required=True) is False


# ══════════════════════════════════════════════════════════ kit_status
def test_no_spec_reports_not_required_not_pending():
    """NOT_REQUIRED tells the screen to HIDE the checklist. Collapsing it into
    PENDING would show every pre-spec garment an empty panel it can never
    satisfy."""
    assert kit_status(kit_required=False, required_total=0, issued_total=0) \
        == KitStatus.NOT_REQUIRED.value


@pytest.mark.parametrize("issued,expected", [
    (0, KitStatus.PENDING.value),
    (2, KitStatus.PARTIAL.value),
    (5, KitStatus.ISSUED.value),
    (7, KitStatus.ISSUED.value),        # over-issued still counts as satisfied
])
def test_kit_status_tracks_how_much_has_gone_out(issued, expected):
    assert kit_status(kit_required=True, required_total=5,
                      issued_total=issued) == expected


def test_an_unresolved_line_holds_the_kit_at_partial_even_when_fully_issued():
    """The kit is not complete, the drawer must not be sendable, and reporting
    ISSUED here would be a lie that lets an unkitted garment onto the line."""
    assert kit_status(kit_required=True, required_total=5, issued_total=5,
                      unresolved=1) == KitStatus.PARTIAL.value


# ══════════════════════════════════════════════════ release_blockers
def _blockers(**kw):
    base = dict(style_name="CLERMONT", confirmed_at=None, no_accessories=None,
                has_accessory_lines=False, has_leather_line=False)
    return release_blockers(**{**base, **kw})


def test_a_fully_specified_confirmed_style_has_no_blockers():
    assert _blockers(confirmed_at="now", has_leather_line=True,
                     has_accessory_lines=True) == []


def test_an_accessory_free_style_passes_only_on_an_explicit_declaration():
    """NULL means NOBODY HAS BEEN ASKED — the same three-state reasoning
    Style.needs_lining uses. Only True is an answer."""
    args = dict(confirmed_at="now", has_leather_line=True,
                has_accessory_lines=False)
    assert _blockers(no_accessories=None, **args) != []
    assert _blockers(no_accessories=False, **args) != []
    assert _blockers(no_accessories=True, **args) == []


def test_the_dcm_is_a_hard_blocker():
    """"The user must enter the dcm consumption per piece" is the explicit ask,
    and the dcm is what the ledger and the costing are built on."""
    out = _blockers(confirmed_at="now", no_accessories=True,
                    has_leather_line=False)
    assert any("no LEATHER line" in b for b in out)


def test_every_blocker_names_the_style_and_what_to_do():
    """These sentences go verbatim into the release response's rejected[], which
    is what the DM reads. A blocker that does not say how to clear it is a dead
    end on the screen where the work stops."""
    for b in _blockers():
        assert "CLERMONT" in b
    joined = " ".join(_blockers())
    assert "material-spec" in joined or "no_accessories" in joined


# ══════════════════════════════════════════ the SKU-coverage gate
#
# WHAT WAS HERE BEFORE, AND WHY IT IS GONE. This section tested
# `accessory_size_gaps` and `accessory_size_ambiguities`: one asked whether a
# size-scoped accessory covered every ordered SIZE, the other whether several
# unscoped material sizes of one article had been left for the system to guess
# between. Both existed only to police STYLE-WIDE accessory lines, and an accessory
# line now names the SKU it is for — a SKU being a colour and a size together. So
# neither question can be asked any more, and the model they protected is the one
# being removed. Deleting them is the point, not an oversight.
#
# `size_matches` STAYS. Leather and lining can still be style-wide, so garment_size
# is still how one of those is scoped to a size, and the Italian-ladder rule is
# still what decides whether a '52' line reaches an 'L' garment.
def _acc(article="YKK", subtype="ZIP", sku_id="navy-M", qty=1.0):
    return {"category": "ACCESSORY", "subtype": subtype, "article": article,
            "sku_id": sku_id, "qty_per_piece": qty}


ALL_SKUS = [("navy-M", "NAVY · M"), ("navy-L", "NAVY · L"),
            ("pine-M", "PINE · M"), ("pine-L", "PINE · L")]


class TestSizeMatches:
    """Leather and lining still scope by size, so this rule is still load-bearing."""

    def test_null_on_either_side_never_drops_a_line(self):
        """NULL garment_size is every size, and a piece of unknown size keeps its
        whole recipe. Both directions, because both are load-bearing."""
        assert size_matches(None, "L") is True
        assert size_matches("L", None) is True

    def test_the_italian_ladder_makes_52_and_l_one_garment(self):
        assert size_matches("52", "L") is True

    def test_a_different_size_does_not_match(self):
        assert size_matches("L", "S") is False

    def test_case_and_space_are_ignored(self):
        assert size_matches(" l ", "L") is True


class TestAccessoriesAreIndependentPerSku:
    """THE RULE THIS CLASS EXISTS TO DEFEND (Hamthan, 2026-09-29).

    Two colourways of one style may take completely different accessories. The
    predicate this replaced — `accessory_sku_gaps` — took every article declared on
    ANY SKU and demanded EVERY SKU carry a line for it, which blocked a real
    release (order 1996) for doing the correct thing. What is left is the one case
    that is never deliberate: a SKU with nothing at all.
    """

    def test_different_accessories_per_sku_is_NOT_a_gap(self):
        """The whole point. NAVY takes a horn button, PINE takes a metal shank,
        neither owes the other anything, and the release must go through."""
        lines = [_acc(subtype="BUTTON", article="HORN BROWN", sku_id="navy-M"),
                 _acc(subtype="BUTTON", article="HORN BROWN", sku_id="navy-L"),
                 _acc(subtype="BUTTON", article="METAL SHANK", sku_id="pine-M"),
                 _acc(subtype="ZIP", article="YKK", sku_id="pine-L")]
        assert skus_without_accessories(lines=lines, ordered_skus=ALL_SKUS) == []

    def test_a_sku_with_no_accessory_line_at_all_IS_a_gap(self):
        """The failure that is worth keeping: PINE · L gets kit_required=False, so
        completeness collapses to leather-and-lining and it ships with nothing."""
        lines = [_acc(sku_id=sid) for sid, _ in ALL_SKUS[:3]]
        assert skus_without_accessories(
            lines=lines, ordered_skus=ALL_SKUS) == ["PINE · L"]

    def test_a_style_with_no_accessory_lines_anywhere_defers_to_the_style_check(self):
        """[] rather than every SKU, so the release prints ONE sentence about the
        style instead of one per colourway saying the same thing."""
        assert skus_without_accessories(lines=[], ordered_skus=ALL_SKUS) == []
        assert skus_without_accessories(
            lines=[{"category": "LEATHER", "subtype": None, "article": "COW",
                    "sku_id": "navy-M", "qty_per_piece": 12.5}],
            ordered_skus=ALL_SKUS) == []

    def test_a_zeroed_line_does_not_cover_a_sku(self):
        """qty 0 is "this colourway takes none of it", which is not a recipe."""
        lines = [_acc(sku_id=sid) for sid, _ in ALL_SKUS[:3]]
        lines.append(_acc(sku_id="pine-L", qty=0))
        assert skus_without_accessories(
            lines=lines, ordered_skus=ALL_SKUS) == ["PINE · L"]

    def test_a_line_with_no_sku_is_ignored_rather_than_crashing(self):
        """Legacy rows from before the rule; the migration deactivates them, but a
        gate that raised on one would take the whole release down with it."""
        assert skus_without_accessories(
            lines=[_acc(sku_id=None)], ordered_skus=ALL_SKUS) == []

    def test_no_ordered_skus_is_not_a_gap(self):
        """A style whose breakdown is not uploaded has no garments to cover."""
        assert skus_without_accessories(lines=[_acc()], ordered_skus=[]) == []


class TestTheEmptySkuBlockerReadsAsASentence:
    def test_it_names_the_style_and_the_empty_skus(self):
        out = _blockers(confirmed_at="now", has_leather_line=True,
                        has_accessory_lines=True,
                        skus_missing_accessories=["PINE · L", "PINE · M"])
        assert len(out) == 1
        assert "CLERMONT" in out[0] and "PINE · L, PINE · M" in out[0]
        # It must say how to clear it, AND that matching the others is not required
        # — otherwise the DM copies lines across and re-creates the old restriction.
        assert "need not match" in out[0]
        assert "no_accessories: true" in out[0]

    def test_no_empty_skus_is_no_blocker(self):
        assert _blockers(confirmed_at="now", has_leather_line=True,
                         has_accessory_lines=True,
                         skus_missing_accessories=[]) == []

    def test_no_accessories_true_silences_it(self):
        """The same escape the style-level check honours. A DM who has declared the
        style accessory-free must not then be asked per colourway."""
        assert _blockers(confirmed_at="now", has_leather_line=True,
                         has_accessory_lines=True, no_accessories=True,
                         skus_missing_accessories=["PINE · L"]) == []

    def test_omitting_it_entirely_is_the_old_behaviour(self):
        assert _blockers(confirmed_at="now", has_leather_line=True,
                         has_accessory_lines=True) == []


class TestTheThreeMaterialsHaveThreeDifferentRULES:
    """Leather required, lining optional, accessories per SKU — asserted together
    because the asymmetry is deliberate and reading it in one place is the only way
    anyone will remember it (Hamthan, 2026-09-29)."""

    def test_a_missing_leather_line_blocks(self):
        out = _blockers(confirmed_at="now", has_leather_line=False,
                        has_accessory_lines=True)
        assert len(out) == 1 and "LEATHER" in out[0]

    def test_a_missing_lining_line_never_blocks(self):
        """There is no lining kwarg at all, and that is the assertion: lining
        consumption is optional on the cut path, so requiring it here would
        contradict the ledger rule downstream."""
        assert _blockers(confirmed_at="now", has_leather_line=True,
                         has_accessory_lines=True) == []


class TestAccessoryLineState:
    """The PER-LINE answer. `kit_status` rolls a garment up and so cannot say which
    packet is missing, which is the only thing the operator at the terminal needs."""

    def test_nothing_issued_is_pending(self):
        assert accessory_line_state(qty_per_piece=4, issued_qty=0) == "PENDING"

    def test_some_issued_is_partial(self):
        assert accessory_line_state(qty_per_piece=4, issued_qty=2) == "PARTIAL"

    def test_all_issued_is_issued(self):
        assert accessory_line_state(qty_per_piece=4, issued_qty=4) == "ISSUED"

    def test_float_slop_does_not_hold_a_line_at_partial(self):
        assert accessory_line_state(
            qty_per_piece=0.3, issued_qty=0.1 + 0.2) == "ISSUED"

    def test_unresolvable_is_not_pending(self):
        """PENDING means "go and fetch it"; UNRESOLVED means no lot matches the
        article, so there is nothing to fetch and the fix is a receipt."""
        assert accessory_line_state(
            qty_per_piece=4, issued_qty=0, resolvable=False) == "UNRESOLVED"
        assert accessory_line_state(
            qty_per_piece=4, issued_qty=4, resolvable=False) == "UNRESOLVED"


class TestAccessoryLabel:
    """"Waiting for the zip" is what the floor says. `still_owed` used to answer in
    bare article codes, the one field they cannot read off the packet."""

    def test_it_leads_with_the_kind(self):
        assert accessory_label({"subtype": "ZIP", "article": "YKK-60",
                                "colour": "BLACK", "size": "60cm"}) == \
            "ZIP · YKK-60 BLACK 60cm"

    def test_it_drops_the_parts_that_are_absent(self):
        assert accessory_label({"subtype": "THREAD", "article": "T40"}) == \
            "THREAD · T40"
        assert accessory_label({"article": "T40"}) == "T40"

    def test_an_empty_row_never_renders_an_empty_string(self):
        """It goes into a sentence, so "Waiting for ." must be impossible."""
        assert accessory_label({}) == "(unnamed accessory)"
