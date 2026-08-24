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
from app.core.kit_rules import (auto_receive_ready, drawer_complete, kit_status,
                                kit_satisfied, release_blockers)

BOOLS = (False, True)


# ══════════════════════════════ THE BACK-COMPATIBILITY PROOF
@pytest.mark.parametrize("leather,lining,accessories,needs_lining",
                         list(itertools.product(BOOLS, BOOLS, BOOLS, BOOLS)))
def test_with_no_accessory_spec_completeness_is_the_old_two_clause_rule(
        leather, lining, accessories, needs_lining):
    old_rule = leather and (lining or not needs_lining)
    assert drawer_complete(
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
    assert drawer_complete(accessories_in=False, **args) is False
    assert drawer_complete(accessories_in=True, **args) is True


def test_auto_receive_is_stricter_than_completeness():
    """A leather-only piece is COMPLETE on leather alone, but a drawer never
    receives itself on one part — the flag that would decide it (needs_lining) is
    wrong for 925 of 1,425 pieces in one live order, so a human confirms."""
    assert drawer_complete(leather_in=True, lining_in=False, accessories_in=True,
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
