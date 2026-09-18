"""
UNIT · how much hide a garment takes, by size. No database.

THE CLAIM UNDER TEST is that the cutting sheet can open with sheets already
allocated for a style nobody has measured. The DM is never asked for per-piece
dcm at release, so for most styles there is no figure in the database at all —
and without a target the allocator has nothing to aim at and the manager is back
to picking ten hides by hand for every garment, which is the Excel work this
feature exists to delete.

So the estimate has to be CLOSE ENOUGH that he edits one cell instead of ten, and
it has to get out of the way the moment a real measurement exists. Both halves are
asserted here.

The numbers are anchored on the factory's own cutting sheet: six rows off Kumar's
screen average 46-49 dcm per hide, and the leather trade puts a jacket at
45-55 sq ft (418-511 dcm). Those two agree, which is why 400 dcm at M is a
defensible anchor rather than a guess.
"""
import pytest

from app.core.leather_norms import (
    TYPICAL_SHEET_DCM, baseline_dcm, expected_sheet_count, leather_target_dcm,
    normalise_size,
)


# ══════════════════════════════════════════════════════ reading a size label
@pytest.mark.parametrize("raw,expected", [
    ("M", "M"),
    (" xl ", "XL"),                  # the importer stores labels verbatim
    ("xxl", "XXL"),
    ("2XL", "XXL"),                  # two spellings of one size
    ("3XL", "XXXL"),
])
def test_alpha_sizes_are_read_whatever_their_spelling(raw, expected):
    assert normalise_size(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("50", "M"), ("52", "L"), ("54", "XL"), ("56", "XXL"), ("48", "S"),
])
def test_the_italian_ladder_maps_onto_the_same_rungs(raw, expected):
    """John Peter, GGZ, NIPAL and KJ all size in EU numbers and never in letters.

    Keeping them on ONE table instead of two is what stops the two drifting.
    """
    assert normalise_size(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("XXL/54", "XXL"),               # the alpha half is unambiguous — prefer it
    ("48/M", "M"),
    ("S/7", "S"),
])
def test_a_combined_label_is_read_from_its_alpha_half(raw, expected):
    """'XXL/54' needs no country guess; '54' alone needs the EU table."""
    assert normalise_size(raw) == expected


def test_a_size_between_rungs_rounds_UP_not_down():
    """45 and 47 are not on the ladder. Over-allocating is the safe direction.

    A sheet too many is handed back in thirty seconds. A sheet too few stops a
    cutter mid-garment and sends him to find the manager.
    """
    assert normalise_size("45") == normalise_size("46") == "XS"
    assert baseline_dcm("45") >= baseline_dcm("44")


@pytest.mark.parametrize("raw", [None, "", "   ", "LL", "BANANA"])
def test_an_unreadable_size_falls_back_to_the_middle_of_the_ladder(raw):
    """A Japanese LL is on no ladder here, and it must not open an EMPTY row.

    M is the fallback because it bounds the error in both directions. Returning
    zero would allocate nothing, which is exactly the manual-picking outcome this
    module exists to prevent.
    """
    assert baseline_dcm(raw) == baseline_dcm("M")


# ══════════════════════════════════════════════════════════════ the ladder
def test_the_ladder_rises_with_size():
    """A bigger garment never takes less hide than a smaller one."""
    ladder = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]
    dcms = [baseline_dcm(s) for s in ladder]
    assert dcms == sorted(dcms)
    assert len(set(dcms)) == len(dcms), "every rung is a distinct target"


def test_every_rung_lands_in_the_seven_to_twelve_sheets_the_factory_quotes():
    """The factory says 7-12 hides per garment. That is the check on the anchor.

    If a rung implied 4 sheets or 20, the 400 dcm anchor would be wrong however
    tidy the arithmetic looked.
    """
    for size in ("XS", "S", "M", "L", "XL", "XXL", "XXXL"):
        n = expected_sheet_count(baseline_dcm(size))
        assert 7 <= n <= 12, f"{size} implies {n} sheets"


def test_the_anchor_agrees_with_the_leather_trade():
    """45-55 sq ft a jacket, 1 sq ft = 9.2903 dcm -> 418-511 dcm.

    L and XL are the sizes a men's outerwear order is mostly made of, so those
    are the rungs that have to sit inside the trade band.
    """
    assert 418 <= baseline_dcm("L") <= 511
    assert 418 <= baseline_dcm("XL") <= 511


def test_a_hide_averages_what_the_factory_sheet_shows():
    """Kumar's six rows average 46-49 dcm per hide. The constant must match."""
    assert 44 <= TYPICAL_SHEET_DCM <= 50


# ═══════════════════════════════════════════════════ the spec beats the guess
def test_a_confirmed_style_spec_wins_over_the_estimate():
    """A spec figure is a measurement somebody signed off. It outranks a guess.

    This is the half that makes the estimate safe to ship: the moment a DM enters
    real per-piece consumption for a style, this module stops having an opinion.
    """
    target, source = leather_target_dcm(size="M", spec_dcm_per_piece=520)
    assert (target, source) == (520.0, "style_spec")


def test_no_spec_falls_back_to_the_size_baseline_and_says_so():
    """The SOURCE is returned so the screen can tell the manager which it was.

    A spec number is a measurement; a baseline number is a guess he should expect
    to correct. Presenting them identically would hide that.
    """
    target, source = leather_target_dcm(size="M", spec_dcm_per_piece=None)
    assert (target, source) == (baseline_dcm("M"), "size_baseline")


def test_a_zero_spec_is_an_unfilled_field_not_a_garment_that_takes_no_leather():
    """A style whose leather line was left at zero must not allocate nothing.

    Honouring it would open an empty cutting row and quietly report that a jacket
    takes no hide — which is never true and is the one reading that loses money
    silently.
    """
    target, source = leather_target_dcm(size="L", spec_dcm_per_piece=0)
    assert source == "size_baseline"
    assert target == baseline_dcm("L")


def test_the_sheet_count_hint_never_returns_zero_for_a_real_target():
    """It drives a caption ('about 9 sheets'), so 0 would read as 'none needed'."""
    assert expected_sheet_count(baseline_dcm("XS")) >= 1
    assert expected_sheet_count(0) == 0        # no target -> no claim
