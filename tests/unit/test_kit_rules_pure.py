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
from app.core.kit_rules import (accessory_size_ambiguities,
                                accessory_size_gaps, auto_receive_ready,
                                kit_satisfied, kit_status, piece_complete,
                                release_blockers, size_matches)

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


# ══════════════════════════════════════════ the size-coverage gate
def _acc(article="YKK", subtype="ZIP", garment_size=None, size=None, qty=1.0):
    return {"category": "ACCESSORY", "subtype": subtype, "article": article,
            "garment_size": garment_size, "size": size, "qty_per_piece": qty}


class TestSizeMatches:
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


class TestAccessorySizeGaps:
    def test_a_sized_article_missing_an_ordered_size_is_a_gap(self):
        """THE FAILURE THIS GATE EXISTS FOR. Zip lines for M and L, an order that
        also runs S and XL: those garments get kit_required=False and ship with no
        zip at all — not a short kit, no kit."""
        gaps = accessory_size_gaps(
            lines=[_acc(garment_size="M"), _acc(garment_size="L")],
            ordered_sizes=["S", "M", "L", "XL"])
        assert gaps == [("YKK (ZIP)", ["S", "XL"])]

    def test_one_unscoped_line_covers_every_size(self):
        """A generic 18L button is one row and must stay one row."""
        assert accessory_size_gaps(
            lines=[_acc(subtype="BUTTON", article="BTN", garment_size=None)],
            ordered_sizes=["S", "M", "L"]) == []

    def test_an_unscoped_line_beside_sized_ones_closes_the_question(self):
        assert accessory_size_gaps(
            lines=[_acc(garment_size="M"), _acc(garment_size=None)],
            ordered_sizes=["S", "M", "L"]) == []

    def test_every_ordered_size_covered_is_no_gap(self):
        assert accessory_size_gaps(
            lines=[_acc(garment_size="S"), _acc(garment_size="M")],
            ordered_sizes=["S", "M"]) == []

    def test_coverage_uses_the_ladder_so_52_covers_l(self):
        assert accessory_size_gaps(lines=[_acc(garment_size="52")],
                                   ordered_sizes=["L"]) == []

    def test_a_size_nobody_ordered_is_not_a_gap(self):
        assert accessory_size_gaps(lines=[_acc(garment_size="M")],
                                   ordered_sizes=["M"]) == []

    def test_a_zeroed_line_is_not_coverage(self):
        """qty 0 on an override is "this one takes none", so it cannot be what
        covers a size."""
        assert accessory_size_gaps(
            lines=[_acc(garment_size="M", qty=0), _acc(garment_size="L")],
            ordered_sizes=["M", "L"]) == [("YKK (ZIP)", ["M"])]

    def test_leather_and_lining_are_not_this_gate_s_business(self):
        assert accessory_size_gaps(
            lines=[{"category": "LEATHER", "subtype": None, "article": "COW",
                    "garment_size": "L", "size": None, "qty_per_piece": 12.5}],
            ordered_sizes=["S", "L"]) == []

    def test_each_article_is_judged_on_its_own(self):
        gaps = accessory_size_gaps(
            lines=[_acc(garment_size="M"),
                   _acc(subtype="BUTTON", article="BTN", garment_size=None)],
            ordered_sizes=["M", "L"])
        assert gaps == [("YKK (ZIP)", ["L"])]


class TestAccessorySizeAmbiguities:
    def test_several_material_sizes_and_no_garment_size_is_ambiguous(self):
        """ZIP 48 / 50 / 52 unscoped means every garment is issued all three.
        Neither reading can be inferred from '50', so the DM is asked."""
        out = accessory_size_ambiguities(
            lines=[_acc(size="48"), _acc(size="50"), _acc(size="52")])
        assert out == [("YKK (ZIP)", ["48", "50", "52"])]

    def test_one_line_is_never_ambiguous_however_it_is_labelled(self):
        """A single 60cm zip on every garment is exactly what an unscoped line
        means — the change must not turn that into a blocker."""
        assert accessory_size_ambiguities(lines=[_acc(size="60")]) == []

    def test_scoped_lines_are_not_ambiguous(self):
        assert accessory_size_ambiguities(
            lines=[_acc(size="48", garment_size="S"),
                   _acc(size="50", garment_size="M")]) == []

    def test_the_same_size_twice_is_not_ambiguous(self):
        assert accessory_size_ambiguities(
            lines=[_acc(size="60"), _acc(size="60")]) == []


class TestTheSizeBlockersReadAsSentences:
    def test_a_coverage_gap_names_the_style_the_article_and_the_sizes(self):
        out = _blockers(confirmed_at="now", has_leather_line=True,
                        has_accessory_lines=True,
                        size_coverage_gaps=[("YKK (ZIP)", ["S", "XL"])])
        assert len(out) == 1
        assert "CLERMONT" in out[0] and "YKK (ZIP)" in out[0]
        assert "S, XL" in out[0]

    def test_an_ambiguity_says_what_would_happen_if_it_shipped(self):
        out = _blockers(confirmed_at="now", has_leather_line=True,
                        has_accessory_lines=True,
                        size_ambiguities=[("YKK (ZIP)", ["48", "50"])])
        assert len(out) == 1
        assert "garment_size" in out[0]

    def test_neither_check_fires_on_a_sound_recipe(self):
        assert _blockers(confirmed_at="now", has_leather_line=True,
                         has_accessory_lines=True,
                         size_coverage_gaps=[], size_ambiguities=[]) == []

    def test_omitting_them_entirely_is_the_old_behaviour(self):
        """Both kwargs default to None so the three existing call sites that have
        not been taught about sizes still compute exactly what they did."""
        assert _blockers(confirmed_at="now", has_leather_line=True,
                         has_accessory_lines=True) == []
