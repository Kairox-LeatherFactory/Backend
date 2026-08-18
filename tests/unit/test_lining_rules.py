"""
UNIT · core/lining_rules — the ONE definition of "does this garment take a
lining?" (change-list item 10).

NO DATABASE. The rule is a pure function of four inputs precisely so it can be
pinned here, and so the importer (which WRITES the flag) and the completeness
gate (which decides what may MOVE) cannot form different opinions about the same
garment. Two copies of this vocabulary is what let a KNIT jacket flagged False
walk from the store to PACKAGE_EXPORT.

THE INVARIANT WORTH REMEMBERING: signals only ever ADD a lining requirement.
Nothing removes one. A stale False cannot let a lined garment through, and a
genuinely leather-only garment still passes on its leather alone.
"""
import pytest

from app.core.lining_rules import (LINING_NAME_MARKERS, is_blank,
                                   lining_required, name_signals_lining,
                                   why_lining_required)


@pytest.mark.parametrize("name", [
    "ADELE KNIT", "REESE WOOL", "FLAVIO KNIT + FUR DETACH", "CLERMONT + VEST",
    "SHINOBI KNIT", "adele knit",              # case must not matter
])
def test_style_names_carrying_a_marker_require_a_lining(name):
    assert name_signals_lining(name) is True


@pytest.mark.parametrize("name", ["CLERMONT", "CARNABY", "ISLAY", "FRANCIS"])
def test_plain_style_names_do_not(name):
    assert name_signals_lining(name) is False


def test_the_stale_false_flag_cannot_override_a_marker():
    """THE BUG, as one assertion.

    The breakdown sheet said this garment needs no lining. The style is named
    ADELE KNIT. A knit style has a knit lining by definition, so the flag is
    wrong and the rule must not defer to it.
    """
    assert lining_required(stored_flag=False, style_name="ADELE KNIT") is True


def test_a_logged_lining_cut_settles_it_outright():
    """Somebody physically cut a lining for this garment. Whatever the sheet
    says, it has one, and the drawer must hold it before the piece moves on."""
    assert lining_required(stored_flag=False, style_name="CLERMONT",
                           has_lining_cut_event=True) is True


def test_a_lining_colour_on_the_sku_requires_a_lining():
    assert lining_required(stored_flag=False, style_name="CLERMONT",
                           colour_values=("ECRU", None)) is True


def test_placeholder_colours_are_not_a_signal():
    """The sheets are full of 'NA' / '-' / 'NONE'. Treating those as a lining
    colour would flag every garment in the factory and wedge the store."""
    assert lining_required(stored_flag=False, style_name="CLERMONT",
                           colour_values=("NA", " - ", "none", "")) is False


def test_a_genuinely_leather_only_garment_needs_nothing():
    """The false-positive guard. A rule that answered True for everything would
    satisfy every test above and strand the whole factory."""
    assert lining_required(stored_flag=False, style_name="CLERMONT",
                           style_article="GOAT SUEDE",
                           colour_values=(None, None),
                           has_lining_cut_event=False) is False


def test_the_stored_flag_alone_is_still_honoured():
    """Positive evidence from the sheet is evidence. It is only the FALSE that
    stopped being trusted."""
    assert lining_required(stored_flag=True, style_name="CLERMONT") is True


@pytest.mark.parametrize("value", ["", " ", "NA", "n/a", "None", "-", None])
def test_is_blank_covers_the_placeholders_the_sheets_use(value):
    assert is_blank(value) is True


def test_the_reason_names_the_evidence_not_the_verdict():
    """A rejection saying 'awaiting lining' on a garment the sheet says needs
    none reads as a system fault. Naming the evidence turns it into an
    instruction: go and find the knit lining for this jacket."""
    why = why_lining_required(stored_flag=False, style_name="ADELE KNIT")
    assert "KNIT" in why


def test_no_reason_when_no_lining_is_required():
    assert why_lining_required(stored_flag=False, style_name="CLERMONT") is None


def test_the_marker_vocabulary_is_uppercase_and_non_empty():
    """The rule upper-cases its haystack, so a lowercase marker would silently
    never match — a whole class of style quietly losing its lining requirement."""
    assert LINING_NAME_MARKERS
    assert all(m == m.upper() and m.strip() for m in LINING_NAME_MARKERS)


def test_the_importer_and_the_gate_share_one_vocabulary():
    """premint re-exports the list rather than keeping its own copy. If this
    fails, the two halves have been allowed to drift again."""
    from app.modules.imports.premint import LINING_NAME_MARKERS as importer_list
    assert importer_list is LINING_NAME_MARKERS
