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
from app.core.kit_rules import (accessory_sku_gaps, auto_receive_ready,
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


class TestAccessorySkuGaps:
    def test_an_article_missing_from_some_skus_is_a_gap(self):
        """THE FAILURE THIS GATE EXISTS FOR. A zip on the NAVY colourways and not the
        PINE ones: those garments get kit_required=False and ship with no zip at all
        — not a short kit, NO kit, because from their point of view the style
        declares no accessories."""
        gaps = accessory_sku_gaps(
            lines=[_acc(sku_id="navy-M"), _acc(sku_id="navy-L")],
            ordered_skus=ALL_SKUS)
        assert gaps == [("YKK (ZIP)", ["PINE · L", "PINE · M"])]

    def test_an_article_on_every_sku_is_no_gap(self):
        assert accessory_sku_gaps(
            lines=[_acc(sku_id=sid) for sid, _ in ALL_SKUS],
            ordered_skus=ALL_SKUS) == []

    def test_each_article_is_judged_on_its_own(self):
        """A button everywhere and a zip on one SKU is one gap, not two."""
        lines = [_acc(subtype="BUTTON", article="BTN", sku_id=sid)
                 for sid, _ in ALL_SKUS] + [_acc(sku_id="navy-M")]
        gaps = accessory_sku_gaps(lines=lines, ordered_skus=ALL_SKUS)
        assert [g[0] for g in gaps] == ["YKK (ZIP)"]
        assert len(gaps[0][1]) == 3

    def test_a_zeroed_line_is_not_coverage(self):
        """qty 0 is "this colourway takes none", so it cannot be what covers a SKU.

        NOTE it also does not COUNT as a gap on its own — the article is simply
        absent from that SKU, which is the same thing the DM said deliberately. The
        gate reports it, and confirming the spec with no_accessories is how a style
        that genuinely takes none passes.
        """
        lines = [_acc(sku_id=sid) for sid, _ in ALL_SKUS[:3]]
        lines.append(_acc(sku_id="pine-L", qty=0))
        gaps = accessory_sku_gaps(lines=lines, ordered_skus=ALL_SKUS)
        assert gaps == [("YKK (ZIP)", ["PINE · L"])]

    def test_leather_and_lining_are_not_this_gate_s_business(self):
        """They can still be style-wide, so a missing SKU means nothing for them."""
        assert accessory_sku_gaps(
            lines=[{"category": "LEATHER", "subtype": None, "article": "COW",
                    "sku_id": "navy-M", "qty_per_piece": 12.5}],
            ordered_skus=ALL_SKUS) == []

    def test_a_line_with_no_sku_is_ignored_rather_than_crashing(self):
        """Legacy rows from before the rule. They cannot be written any more and the
        migration deactivates them, but a gate that raised on one would take the
        whole release down with it."""
        assert accessory_sku_gaps(lines=[_acc(sku_id=None)],
                                  ordered_skus=ALL_SKUS) == []

    def test_no_ordered_skus_is_not_a_gap(self):
        """A style whose breakdown is not uploaded has no garments to cover."""
        assert accessory_sku_gaps(lines=[_acc()], ordered_skus=[]) == []


class TestTheCoverageBlockerReadsAsASentence:
    def test_it_names_the_style_the_article_and_the_missing_skus(self):
        out = _blockers(confirmed_at="now", has_leather_line=True,
                        has_accessory_lines=True,
                        sku_coverage_gaps=[("YKK (ZIP)", ["PINE · L", "PINE · M"])])
        assert len(out) == 1
        assert "CLERMONT" in out[0] and "YKK (ZIP)" in out[0]
        assert "PINE · L, PINE · M" in out[0]
        # It must say how to clear it, not just that it is blocked.
        assert "ALL_SKUS" in out[0]

    def test_no_gaps_is_no_blocker(self):
        assert _blockers(confirmed_at="now", has_leather_line=True,
                         has_accessory_lines=True, sku_coverage_gaps=[]) == []

    def test_omitting_it_entirely_is_the_old_behaviour(self):
        """The kwarg defaults to None so a caller not yet taught about coverage
        computes exactly what it always did."""
        assert _blockers(confirmed_at="now", has_leather_line=True,
                         has_accessory_lines=True) == []
