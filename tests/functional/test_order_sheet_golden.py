"""
FUNCTIONAL · the three real order sheets, end to end through the parser.

WHY THESE THREE. They are the same document written three different ways, and
between them they break every shortcut a breakdown parser is tempted to take:

  BOGGI MAIN    letter sizes S..XXXL, plain numeric price, a DELIVERY column
  BOGGI OUTLET  letter sizes S..XXL,  price written "€ 80,00 cif"
  JOHN PETER    Italian headers (Modello/Materiale/Colore), NUMERIC sizes 38..62,
                the price column sitting BETWEEN the sizes and the total, and no
                delivery column at all

All three must parse with the SAME code and no per-brand branch. Before this
work every one of them parsed to ZERO lines: BOGGI because its header row says
"STYLE" where the detector wanted "S.NO", John Peter because "Modello" matched
nothing at all and the sheet was classified UNKNOWN and skipped in silence.
"""
import os

import pytest

from app.modules.imports.import_engine import build_preview

# tests/functional/<file> -> tests/ -> backend/. THREE dirnames, not two: the
# two-dirname version resolves to tests/ and every fixture "vanishes", which is
# exactly how test_importer_v2 and test_johnpeter_import came to skip silently.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOGGI_MAIN = os.path.join(_ROOT, "ORDER BOGGI MAIN SS27 (3).xlsx")
BOGGI_OUTLET = os.path.join(_ROOT, "ORDINE BOGGI OUTLET SS27 (2).xlsx")
JOHN_PETER = os.path.join(
    _ROOT, "data", "Full-data-spec-order", "PTE v2.0", "Client_1_JohnPeter",
    "ANELE", "DETACH", "John_Peter_Order_Data_Consolidate.xlsx")


def _need(path):
    return pytest.mark.skipif(not os.path.exists(path),
                              reason=f"fixture not present: {path}")


def _styles(path):
    summary = build_preview(path).summary()
    assert summary["clients"], "sheet produced no client block at all"
    client = list(summary["clients"].values())[0]
    return client, client["by_style"]


# ══════════════════════════════════════════════════════ BOGGI MAIN — letters
@_need(BOGGI_MAIN)
def test_boggi_main_letter_sizes_prices_and_delivery():
    client, by_style = _styles(BOGGI_MAIN)
    assert client["order_lines"] == 2
    assert client["pieces_ordered"] == 795

    main = by_style["BO27P082702"]
    assert list(main["sizes"]) == ["S", "M", "L", "XL", "XXL", "XXXL"]
    assert main["sizes"] == {"S": 56, "M": 129, "L": 143, "XL": 92,
                             "XXL": 28, "XXXL": 1}
    assert main["pieces_ordered"] == 449
    assert main["unit_price"] == 83.0
    assert main["delivery_date"] == "2026-09-15"
    assert main["size_confidence"] == "HIGH"

    assert by_style["BO27P082901"]["unit_price"] == 93.0
    assert by_style["BO27P082901"]["delivery_date"] == "2026-09-15"


# ══════════════════════════════════ BOGGI OUTLET — price with symbol + incoterm
@_need(BOGGI_OUTLET)
def test_boggi_outlet_parses_a_price_written_as_euro_comma_cif():
    client, by_style = _styles(BOGGI_OUTLET)
    assert client["order_lines"] == 2
    assert client["pieces_ordered"] == 620

    row = by_style["BF27P010501"]
    assert list(row["sizes"]) == ["S", "M", "L", "XL", "XXL"]
    assert row["pieces_ordered"] == 290
    # "€ 80,00 cif" — European decimal, currency symbol, trailing incoterm.
    assert row["unit_price"] == 80.0
    assert row["currency"] == "EUR"
    assert row["delivery_date"] == "2026-09-15"


# ═════════════════════════════════════ JOHN PETER — numeric sizes, no delivery
@_need(JOHN_PETER)
def test_john_peter_numeric_sizes_with_the_price_column_before_the_total():
    """Same code, no size list, no per-brand branch — and the price column that
    sits between the band and the total must not be counted as a size."""
    client, by_style = _styles(JOHN_PETER)
    assert client["order_lines"] == 75
    assert client["pieces_ordered"] == 1273

    clermont = by_style["CLERMONT"]
    assert set(clermont["sizes"]) <= {str(n) for n in range(38, 64, 2)}
    assert clermont["unit_price"] == 66.0
    assert clermont["size_confidence"] == "HIGH"
    # NO delivery column on this sheet. That is normal, not an error.
    assert clermont["delivery_date"] is None
    assert by_style["CLERMONT VEST"]["unit_price"] == 74.0
    assert by_style["FLAVIO KNIT"]["unit_price"] == 39.0

    # And the price is never mistaken for a size, on any style.
    for name, entry in by_style.items():
        assert "PREZZO (€)" not in entry["sizes"], name
        assert "TOTALE CAPI" not in entry["sizes"], name


# ═══════════════════════════════ classification must not over-reach
COSTING = os.path.join(_ROOT, "data", "SAMPLE COSTING SHEET FOR SIR.xlsx")
LEGACY = os.path.join(_ROOT, "data", "GARMENT_ORDERPRODUCTION_DETAILS.xlsx")


@_need(COSTING)
def test_a_costing_workbook_is_not_mistaken_for_an_order_sheet():
    """THE FALSE POSITIVE THIS GUARD EXISTS FOR. Recognising order sheets by
    SHAPE rescued two real sheets the word-based classifier dropped in silence —
    and, on the first cut, also swallowed a costing workbook and reported it as a
    16,649,667-piece order.

    A costing sheet really does have the arithmetic of a breakdown: a run of
    columns reconciling against a printed total, over 33 rows. What it does not
    have is size LABELS — its columns are headed 576000 / 0.05 / 28800. That is
    the only honest way to tell the two apart, and this test pins it."""
    preview = build_preview(COSTING).summary()
    assert all(s["type"] == "UNKNOWN" for s in preview["sheets"]), (
        "a costing workbook classified as an order sheet: "
        f"{[s for s in preview['sheets'] if s['type'] != 'UNKNOWN']}")
    assert sum(c["pieces_ordered"] for c in preview["clients"].values()) == 0


@_need(LEGACY)
def test_the_legacy_multi_block_workbook_still_reconciles_exactly():
    """REGRESSION. The six clients on the shipped workbook, with the totals the
    sheet itself prints. Every one of them is a STACKED sheet whose blocks each
    declare a different set of sizes, which is the case the arithmetic detector
    had to keep working for while gaining the flat single-header sheets."""
    clients = build_preview(LEGACY).summary()["clients"]
    expected = {"KJ": (11, 347), "GGZ": (1, 146), "NIPAL": (2, 259),
                "RICANO": (3, 150), "JP": (140, 2263), "NIPAL-NEW": (6, 393)}
    actual = {k: (v["order_lines"], v["pieces_ordered"])
              for k, v in clients.items()}
    assert actual == expected
