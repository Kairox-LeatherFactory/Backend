"""
UNIT · MATERIAL_SPEC / resolve_spec / uom_for — the strict per-category contract.

CLAUDE.md §5 states the rule as an absolute: "Every lot must carry exactly its
category's fields, else the API rejects it (422)." `GET /materials/spec` serves
this same table at runtime so the frontend renders the right form, which makes
the table a published API contract, not an internal detail — a silent change to
a `qty_field` renames a form input for two frontend teams.

These are pure dict/function assertions: no DB, no HTTP.
"""
import pytest

from app.core.enums import MATERIAL_SPEC, resolve_spec, uom_for

# The table exactly as CLAUDE.md §5 prints it:
#   (category, subtype) -> (required fields, qty field, uom, filters)
SPEC_FROM_THE_SPEC = [
    ("LEATHER", None, {"thickness", "dcm"}, "dcm", "dcm",
     ["article", "colour", "thickness"]),
    ("LINING", "PLAIN_LINING", {"thickness", "mtrs"}, "mtrs", "mtrs",
     ["article", "colour", "thickness"]),
    ("LINING", "RIBS", {"kg"}, "kg", "kg", ["article", "colour"]),
    ("LINING", "KNIT", {"pcs"}, "pcs", "pcs", ["article", "colour"]),
    ("ACCESSORY", "BUTTON", {"size", "count"}, "count", "pcs",
     ["article", "colour", "size"]),
    ("ACCESSORY", "ZIP", {"size", "count"}, "count", "pcs",
     ["article", "colour", "size"]),
    ("ACCESSORY", "THREAD", {"thickness", "mtrs"}, "mtrs", "mtrs",
     ["article", "colour", "thickness"]),
    ("ACCESSORY", "OTHER", {"description", "count"}, "count", "pcs",
     ["article", "colour"]),
]


# ══════════════════════════════════════════════════════════════ happy path
@pytest.mark.parametrize("cat,sub,required,qty_field,uom,filters",
                         SPEC_FROM_THE_SPEC)
def test_every_documented_category_matches_the_written_spec(
        cat, sub, required, qty_field, uom, filters):
    spec = resolve_spec(cat, sub)
    assert spec is not None, f"{cat}/{sub} is documented but unresolvable"
    assert spec["required"] == required
    assert spec["qty_field"] == qty_field
    assert spec["qty_uom"] == uom
    assert spec["filters"] == filters


def test_the_table_has_no_undocumented_rows():
    """A row nobody wrote down is a form field the frontend will never render."""
    assert set(MATERIAL_SPEC) == {(c, s) for c, s, *_ in SPEC_FROM_THE_SPEC}


# ═══════════════════════════════════════════════════════ common variation
@pytest.mark.parametrize("cat", ["leather", "Leather", "  LEATHER  ".strip()])
def test_category_is_case_insensitive(cat):
    """A manager's form posts whatever case the frontend sent."""
    assert resolve_spec(cat, None) is MATERIAL_SPEC[("LEATHER", None)]


def test_leather_ignores_any_subtype():
    """resolve_spec:enums_barcode — LEATHER has exactly one shape, so a stray
    subtype must not make it unresolvable."""
    assert resolve_spec("LEATHER", "ANYTHING") is MATERIAL_SPEC[("LEATHER", None)]


def test_lining_with_no_subtype_defaults_to_plain():
    """The documented default: 'LINING with no subtype defaults to PLAIN_LINING'."""
    assert resolve_spec("LINING", None) is MATERIAL_SPEC[("LINING", "PLAIN_LINING")]


# ══════════════════════════════════════════════════ error-throwing inputs
def test_accessory_without_a_subtype_is_unresolvable():
    """There is no generic accessory quantity — a button is counted, thread is
    metered. Returning None here is what produces the 422 with the
    'Accessory needs a subtype' hint (materials/service.py:75-79)."""
    assert resolve_spec("ACCESSORY", None) is None
    assert resolve_spec("ACCESSORY", "") is None


@pytest.mark.parametrize("cat", ["FABRIC", "", "  ", "TRIM", None])
def test_an_unknown_category_is_unresolvable(cat):
    assert resolve_spec(cat, None) is None


@pytest.mark.parametrize("sub", ["VELVET", "MESH", "PLAIN"])
def test_an_unknown_lining_subtype_is_unresolvable(sub):
    """'PLAIN' is included deliberately. The spec table keys the plain lining row
    as PLAIN_LINING, while CLAUDE.md §5 prints the row as 'Lining / plain' — so
    'PLAIN' is the subtype a frontend reading the prose would send, and it does
    NOT resolve. Pinning the current behaviour: if the alias is later accepted,
    this test fails and that is the signal to update the contract."""
    assert resolve_spec("LINING", sub) is None


@pytest.mark.parametrize("sub", ["BUTTONS", "ZIPPER", "CORD"])
def test_an_unknown_accessory_subtype_is_unresolvable(sub):
    assert resolve_spec("ACCESSORY", sub) is None


# ═════════════════════════════════════════════════════ integrity of the table
def test_the_quantity_field_is_always_one_of_its_own_required_fields():
    """`create_lot` reads the quantity out of `attrs[spec['qty_field']]`
    (materials/service.py:103). If a qty_field were not also required, the strict
    check would pass with no quantity present and the Decimal() cast would raise
    on None — a 500 where a 422 belongs."""
    for key, spec in MATERIAL_SPEC.items():
        assert spec["qty_field"] in spec["required"], f"{key} can create a qty-less lot"


def test_uom_for_agrees_with_the_table():
    """`uom_for` is used on the supplier-order path (materials/service.py:280)
    where no lot exists yet. It must not disagree with the lot path's uom."""
    for cat, sub, _req, _qf, uom, _f in SPEC_FROM_THE_SPEC:
        assert uom_for(cat, sub) == uom


def test_filters_never_include_the_quantity():
    """Filters drive the search boxes. Offering 'search by dcm' is meaningless —
    quantity is a level, not an identity."""
    for key, spec in MATERIAL_SPEC.items():
        assert spec["qty_field"] not in spec["filters"], f"{key} filters on quantity"


def test_article_and_colour_are_filterable_for_every_material():
    """CLAUDE.md §5: 'article + colour are required for every material.'"""
    for key, spec in MATERIAL_SPEC.items():
        assert "article" in spec["filters"] and "colour" in spec["filters"], key
