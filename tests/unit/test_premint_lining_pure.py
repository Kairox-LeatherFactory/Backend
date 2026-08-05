"""
UNIT · `_sku_needs_lining` — the predicate that arms the merge gate. No DB.

WHY THIS IS A MONEY-ADJACENT PREDICATE, NOT A COSMETIC FLAG
    needs_lining is set once, at breakdown upload (imports/premint.py:115), and
    drives the drawer completeness rule (drawers/service.py:96,141-146). Get it
    wrong in the TRUE direction and a leather-only garment's drawer waits forever
    for a lining nobody will cut — RECEIVED is unreachable, so SENDED is
    unreachable, so LINE_STITCHING is blocked for the whole order (the H9
    regression documented at premint.py:59-74).

    Get it wrong in the FALSE direction and a lined jacket line-stitches with no
    lining in the drawer.

The fix chose "require positive evidence". These cases pin that choice.
"""
import pytest

from app.modules.imports.premint import _sku_needs_lining


class FakeSKU:
    """Duck-typed SKU carrying only the lining columns the predicate reads.

    A real `SKU` row is not used because the predicate is pure `getattr` over
    four optional attributes — constructing an ORM object would drag in a DB
    session for a function that touches neither.
    """
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ══════════════════════════════════════════════════════════════ happy path
@pytest.mark.parametrize("attr", ["knit_color", "nylon_color",
                                  "lining_color", "lining_type"])
def test_any_lining_signal_marks_the_piece_as_lined(attr):
    """Four columns are consulted (premint.py:70); a value in ANY of them is
    positive evidence of a lining."""
    assert _sku_needs_lining(FakeSKU(**{attr: "PINE GREEN"})) is True


# ═════════════════════════════════════════════════ the H9 regression itself
def test_no_lining_signal_means_no_lining():
    """H9: this used to `return True` on both branches. A SKU with nothing in
    any lining column is leather-only and its drawer must complete on leather
    alone."""
    assert _sku_needs_lining(FakeSKU()) is False


@pytest.mark.parametrize("blank", ["", "   ", "NA", "N/A", "na", "None",
                                   "NONE", "-", None])
def test_a_placeholder_is_not_a_lining(blank):
    """Spreadsheets carry 'NA' and '-' as 'no value'. Treating those as a lining
    colour reintroduces H9 through the back door — the sentinel list at
    premint.py:72 is what stops that."""
    assert _sku_needs_lining(FakeSKU(knit_color=blank)) is False


def test_a_blank_column_does_not_mask_a_real_one():
    """Common variation: the sheet fills nylon_color but leaves knit_color as
    'NA'. The loop must keep scanning past the placeholder."""
    assert _sku_needs_lining(
        FakeSKU(knit_color="NA", nylon_color="BLACK")) is True


def test_a_sku_missing_the_columns_entirely_does_not_crash():
    """`getattr(sku, attr, None)` (premint.py:71) tolerates a SKU model that
    never grew these columns — an older order must not raise at upload."""
    assert _sku_needs_lining(FakeSKU(color_code="PINE")) is False


def test_whitespace_around_a_real_colour_still_counts():
    assert _sku_needs_lining(FakeSKU(lining_color="  KNIT BLACK  ")) is True
