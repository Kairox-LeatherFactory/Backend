"""
UNIT · size detection by arithmetic, and money parsing. No database.

THE CLAIM UNDER TEST is that the importer needs no size vocabulary. A brand in a
new country ships sizes nobody catalogued, and the parser must take them with
zero code or config change — so these tests deliberately use size labels that
appear on NO list anywhere in the codebase (Japanese LL/3L, a combined "48/M")
and assert they are picked up anyway.

The second half is `to_money`, which is where a costing error would come from:
"1.234,50" read the Anglo way is a 1000x mistake on every garment in the order.
"""
from datetime import datetime
from decimal import Decimal

import openpyxl
import pytest

from app.modules.imports._size_band import (
    _looks_like_a_size_label, detect_size_band, find_header_row)
from app.modules.imports.excel_reader import to_money


def _sheet(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    return ws


# ══════════════════════════════════════════════════ sizes, with no word list
def test_a_size_band_is_found_from_labels_no_list_has_ever_seen():
    """LL / 3L / 5L are Japanese sizing. Nothing in this codebase knows them, and
    that is exactly the point: the band is solved against the printed total."""
    ws = _sheet([
        ["STYLE", "COLOUR", "LL", "3L", "5L", "TOTAL"],
        ["AOI-01", "NAVY", 4, 6, 2, 12],
        ["AOI-02", "BLACK", 1, 3, 1, 5],
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.confidence == "HIGH"
    assert list(band.size_cols.values()) == ["LL", "3L", "5L"]
    assert band.total_col == 6


def test_a_decoy_numeric_column_is_not_a_size():
    """A year column sits in the row and holds integers, so any "an integer
    header is a size" rule swallows it and invents a size called 2027. The
    arithmetic cannot: adding it breaks the sum."""
    ws = _sheet([
        ["STYLE", "YEAR", "S", "M", "L", "TOTAL"],
        ["BO-1", 2027, 10, 20, 5, 35],
        ["BO-2", 2027, 4, 8, 3, 15],
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.confidence == "HIGH"
    assert list(band.size_cols.values()) == ["S", "M", "L"]
    assert "YEAR" not in band.size_cols.values()
    assert 2 not in band.size_cols            # the decoy column index


def test_a_price_column_between_the_sizes_and_the_total_is_excluded():
    """The John Peter layout: ... | 62 | Prezzo | Totale Capi. A rule that took
    "the run immediately left of the total" would eat the price and report every
    order as wildly over-quantity."""
    ws = _sheet([
        ["MODELLO", "COLORE", "48", "50", "52", "PREZZO", "TOTALE CAPI"],
        ["CLERMONT", "706", 2, 3, 1, 66, 6],
        ["FLAVIO", "309", 5, 5, 2, 39, 12],
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.confidence == "HIGH"
    assert list(band.size_cols.values()) == ["48", "50", "52"]
    assert band.price_col == 6 and band.total_col == 7


def test_sizes_are_stored_verbatim_and_case_folded_only():
    """"XL ", "xl" and "XL" must be ONE size — three would fork into three SKUs,
    three codes and three barcodes for the same garment. But "XXL/54" keeps its
    slash: whatever the client wrote is what the floor reads back."""
    ws = _sheet([
        ["STYLE", " xl ", "XXL/54", "TOTAL"],
        ["Z-1", 3, 2, 5],
    ])
    band = detect_size_band(ws, 1, [2])
    assert list(band.size_cols.values()) == ["XL", "XXL/54"]


def test_a_sheet_with_no_total_falls_back_to_medium_never_silently_high():
    """No printed total means nothing to reconcile against. The band is still
    usable, but a human is told to look — it is never presented as proven."""
    ws = _sheet([
        ["STYLE", "COLOUR", "S", "M", "L"],
        ["N-1", "NAVY", 4, 6, 2],
    ])
    band = detect_size_band(ws, 1, [2])
    assert band.confidence == "MEDIUM"
    assert list(band.size_cols.values()) == ["S", "M", "L"]


def test_a_total_that_does_not_add_up_is_reported_not_swallowed():
    """A torn or hand-edited sheet. This is the case the arithmetic exists to
    catch, so the disagreeing rows are named with both numbers."""
    ws = _sheet([
        ["STYLE", "S", "M", "TOTAL"],
        ["A-1", 4, 6, 10],
        ["A-2", 5, 5, 99],          # wrong on the sheet
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.confidence == "MEDIUM"
    assert band.mismatched_rows, "a mismatching printed total must be reported"
    row, printed, summed = band.mismatched_rows[0]
    assert (row, printed, summed) == (3, 99, 10)


def test_the_header_row_is_found_under_a_title_row():
    """Real sheets open with a title ("MAIN SS27"). The header is the row above a
    band that adds up, so the title is skipped without naming it."""
    ws = _sheet([
        ["MAIN SS27", None, None, None, None],
        ["STYLE", "COLOUR", "S", "M", "TOT."],
        ["BO-1", "TAUPE", 10, 20, 30],
    ])
    header, band = find_header_row(ws)
    assert header == 2
    assert list(band.size_cols.values()) == ["S", "M"]


def test_an_all_zero_run_never_reconciles():
    """0 + 0 + 0 == 0 is true and proves nothing.

    A weekly production sheet whose stage columns are all zero offers a run that
    reconciles against an all-zero total on every row — arithmetically perfect,
    and HIGH confidence for a band carrying no order at all. `NIPAL-NEW
    PRODUCTION` is this sheet: it was classified ORDER, parsed to zero lines, and
    vanished from the import without a warning.
    """
    ws = _sheet([
        ["WEEK", "CUTTING", "FUSING", "PASTING", "TOTAL"],
        ["w1", 0, 0, 0, 0],
        ["w2", 0, 0, 0, 0],
        ["w3", 0, 0, 0, 0],
    ])
    band = detect_size_band(ws, 1, [2, 3, 4])
    assert band.confidence != "HIGH"

    header, solved = find_header_row(ws)
    assert not (header is not None and solved.confidence == "HIGH"
                and len(solved.size_cols) >= 2 and solved.reconciled_rows >= 2)


def test_a_footer_caption_inside_the_band_does_not_drop_its_column():
    """The GGZ case, in miniature.

    A 'GRAND TOTAL' caption printed in the size-50 column makes that column look
    like text. If footer rows are profiled alongside data rows the column is
    dropped, the contiguous run splits, and a 146-piece order reports 65. Footers
    are excluded from the PROFILE only — see `profile_rows` in _size_band.py.
    """
    ws = _sheet([
        ["S.NO", "STYLE", "COLOUR", "44", "46", "48", "50", "52", "54", "TOTAL"],
        [1, "BOMBER A", "MORO", 2, 23, 40, 43, 28, 10, 146],
        [None, None, None, None, None, None, None, None, None, 146],
        [None, None, None, None, None, None, "GRAND TOTAL", None, None, 146],
    ])
    band = detect_size_band(ws, 1, [2, 3, 4])
    assert band.confidence == "HIGH"
    assert list(band.size_cols.values()) == ["44", "46", "48", "50", "52", "54"]


# ═══════════════════════════════════════════════════ the tail: price/delivery
def test_price_after_the_total_is_read__the_95_percent_layout():
    """TOTAL | PRICE | DELIVERY — what the KairoX template ships."""
    ws = _sheet([
        ["STYLE", "COLOUR", "S", "M", "L", "TOTAL", "PRICE", "DELIVERY"],
        ["BO-1", "TAUPE", 10, 20, 30, 60, 83, "15/09/2026"],
        ["BO-2", "BEIGE", 5, 5, 5, 15, 93, "15/09/2026"],
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.total_col == 6
    assert band.price_col == 7
    assert band.delivery_col == 8
    assert band.warnings == []          # the usual layout is not worth a warning


def test_price_before_the_total_is_read_and_flagged__john_peter():
    """... | 62 | Prezzo (€) | Totale Capi — a real client, not a broken sheet.

    It must parse correctly, and it must say so, because a tail printed the
    unusual way round is exactly where a silent misread would cost money.
    """
    ws = _sheet([
        ["Modello", "Colore", "38", "40", "42", "Prezzo", "Totale Capi"],
        ["CLERMONT", "WHISKY", 2, 3, 2, 66, 7],
        ["FLAVIO", "FOREST", 5, 5, 5, 74, 15],
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.total_col == 7
    assert band.price_col == 6
    assert any("LEFT of TOTAL" in w for w in band.warnings)


def test_an_uncatalogued_price_header_is_read_by_shape():
    """'Precio Unitario' is on no list here. Its CELLS are money, so it is price.

    This is the whole reason the tail is read by shape: a maintained header list
    is a promise to edit code every time a client writes a new word.
    """
    ws = _sheet([
        ["STYLE", "COLOUR", "S", "M", "TOTAL", "Precio Unitario"],
        ["BO-1", "TAUPE", 10, 20, 30, "80,00"],
        ["BO-2", "BEIGE", 5, 5, 10, "75,50"],
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.total_col == 5
    assert band.price_col == 6


def test_a_bare_integer_tail_column_does_not_steal_the_price():
    """`to_money(3)` is Decimal 3.00, so a carton count used to read as money.

    `_classify_tail` took the FIRST money-shaped tail column, so CARTONS won and
    the real PRICE was dropped — a unit price of 3 instead of 83, at HIGH
    confidence, with no warning. A parser may be generous; a classifier may not.
    """
    ws = _sheet([
        ["STYLE", "COLOUR", "S", "M", "TOTAL", "CARTONS", "PRICE", "DELIVERY"],
        ["BO-1", "TAUPE", 10, 20, 30, 3, 83, "15/09/2026"],
        ["BO-2", "BEIGE", 5, 5, 10, 1, 93, "15/09/2026"],
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.total_col == 5
    assert band.price_col == 7, "the column headed PRICE, not the carton count"
    assert band.delivery_col == 8


def test_a_delivery_written_as_a_week_number_is_not_read_as_a_price():
    """'Sett. 38' is an Italian ship week, not 38 euros.

    to_money strips the letters and returns 0.38, so shape called it money and —
    because shape beat the header name unconditionally — the delivery column was
    silently dropped. A price is a number with decoration, never a phrase with a
    number in it.
    """
    ws = _sheet([
        ["STYLE", "COLOUR", "S", "M", "TOTAL", "PRICE", "CONSEGNA"],
        ["BO-1", "TAUPE", 10, 20, 30, 83, "Sett. 38"],
        ["BO-2", "BEIGE", 5, 5, 10, 93, "Sett. 40"],
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.price_col == 6
    assert band.delivery_col == 7, "CONSEGNA is a delivery, whatever it holds"


def test_a_foreign_price_column_is_evicted_by_the_delivery_anchor():
    """The bug this whole change exists for.

    Spell the header PRICE and the sheet parses. Spell it `Preis` and the column
    joins the band as a size: a 22-piece order reported 102 with an extra SKU
    called PREIS at quantity 80. No total is printed, so nothing can reconcile —
    but `Liefertermin` holds DATES, and the layout is TOTAL | PRICE | DELIVERY,
    so the column immediately left of a proven delivery is the price.
    """
    ws = _sheet([
        ["Modell", "Farbe", "38", "40", "42", "Preis", "Liefertermin"],
        ["CLERMONT", "WHISKY", None, 7, 13, 80, "15/09/2026"],
        ["FLAVIO", "FOREST", 5, 11, None, 74, "15/09/2026"],
        ["ANELE", "MORO", None, 3, None, 66, "22/09/2026"],
    ])
    band = detect_size_band(ws, 1, [2, 3, 4])
    assert list(band.size_cols.values()) == ["38", "40", "42"]
    assert "PREIS" not in band.size_cols.values()
    assert band.price_col == 6
    assert band.delivery_col == 7
    assert any("read as the PRICE" in w for w in band.warnings),         "an unprovable eviction must never be silent"


def test_the_eviction_never_eats_a_size_that_continues_the_series():
    """The guard on the rule above, and it has to hold.

    `... | 58 | 60 | 62 | DELIVERY` with no total: a 62 ordered on every line is
    DENSE while 58 is not, which is the same evidence the Preis case offers. If
    density alone decided, a real size would be evicted and the order would ship
    SHORT by a whole column. 62 continues a run of numbers; Preis does not.
    """
    ws = _sheet([
        ["STYLE", "COLOUR", "58", "60", "62", "DELIVERY"],
        ["BO-1", "TAUPE", None, 7, 13, "15/09/2026"],
        ["BO-2", "BEIGE", 5, 11, 17, "15/09/2026"],
        ["BO-3", "NAVY", None, 3, 9, "22/09/2026"],
    ])
    band = detect_size_band(ws, 1, [2, 3, 4])
    assert list(band.size_cols.values()) == ["58", "60", "62"]
    assert band.price_col is None


def test_a_total_next_to_a_delivery_is_never_renamed_the_price():
    """`... | TOTAL | DELIVERY` with no price column at all.

    The positional chain says the column right of the total is the price. Here
    there is no price, and mistaking the total for one would quote the customer
    the order quantity.
    """
    ws = _sheet([
        ["STYLE", "COLOUR", "S", "M", "L", "TOTAL", "DELIVERY"],
        ["BO-1", "TAUPE", 10, 20, 31, 61, "15/09/2026"],
        ["BO-2", "BEIGE", 5, 5, 7, 17, "15/09/2026"],
    ])
    band = detect_size_band(ws, 1, [2, 3])
    assert band.total_col == 6
    assert band.delivery_col == 7
    assert band.price_col is None


def test_a_size_label_is_short_one_word_and_a_plausible_number():
    """The shape test, pinned at its edges.

    Loose enough that a new country passes untouched — EU children's 92-164 and
    Asian cm 160-185 are real sizing — and tight enough that a caption, a year or
    an item code cannot stand as a size candidate waiting for the arithmetic to
    notice it.
    """
    for label in ("S", "XXXL", "XXL/54", "48/M", "S/7", "38", "62", "164", "0"):
        assert _looks_like_a_size_label(label), label
    for label in ("ONE SIZE", "CUTTING", "Material", "2027", "245", "576000",
                  "0.05", "71 Euro", "STYLE :"):
        assert not _looks_like_a_size_label(label), label
    assert not _looks_like_a_size_label(datetime(2026, 9, 15))


# ═══════════════════════════════════════════════════════ descriptor columns
def test_the_95_percent_descriptor_order_is_the_default():
    """STYLE, then COLOUR, then ARTICLE — assumed with no header vocabulary."""
    from app.modules.imports.parse_orders import parse_order_sheet
    ws = _sheet([
        ["Modelo", "Tono", "Tejido", "S", "M", "Suma"],
        ["BO-1", "TAUPE", "SUEDE", 10, 20, 30],
        ["BO-2", "BEIGE", "SUEDE", 5, 5, 10],
    ])
    lines, _warnings, verdict = parse_order_sheet(ws)
    assert verdict == "ORDER"
    assert [(l.style, l.color, l.article) for l in lines] == [
        ("BO-1", "TAUPE", "SUEDE"), ("BO-2", "BEIGE", "SUEDE")]


def test_a_recognised_header_beats_the_positional_default():
    """John Peter prints `Modello | Materiale | Colore` — STYLE, ARTICLE, COLOUR.

    Position alone would read MATERIALE as the colour and COLORE as the article,
    swapping two fields on every row of a real client's every sheet. The header
    wins, and the deviation is reported once.
    """
    from app.modules.imports.parse_orders import parse_order_sheet
    ws = _sheet([
        ["Modello", "Materiale", "Colore", "38", "40", "Totale"],
        ["CLERMONT", "BURNED SUEDE", "WHISKY", 2, 3, 5],
        ["FLAVIO", "WINTER SUEDE", "FOREST", 5, 5, 10],
    ])
    lines, warnings, verdict = parse_order_sheet(ws)
    assert verdict == "ORDER"
    assert [(l.style, l.color, l.article) for l in lines] == [
        ("CLERMONT", "WHISKY", "BURNED SUEDE"),
        ("FLAVIO", "FOREST", "WINTER SUEDE")]
    assert sum("usual STYLE, COLOUR, ARTICLE order" in w for w in warnings) == 1


# ══════════════════════════════════════════════════════════════════ to_money
@pytest.mark.parametrize("raw,expected,currency", [
    ("83", Decimal("83.00"), None),
    (83, Decimal("83.00"), None),
    ("\u20ac 80,00 cif", Decimal("80.00"), "EUR"),      # symbol + incoterm
    ("1.234,50", Decimal("1234.50"), None),             # European decimals
    ("1,234.50", Decimal("1234.50"), None),             # Anglo decimals
    ("80,00", Decimal("80.00"), None),                  # lone comma = decimal
    ("$ 12.50", Decimal("12.50"), "USD"),
    ("Rs 1500", Decimal("1500.00"), "INR"),
    ("", None, None),
    (None, None, None),
    ("abc", None, None),
])
def test_to_money_reads_every_shape_a_price_cell_arrives_in(raw, expected, currency):
    value, cur = to_money(raw)
    assert value == expected
    assert cur == currency


def test_to_money_never_raises_on_junk():
    """One junk price cell must not fail a 400-row import. The preview reports
    it; the parser keeps going."""
    for junk in ("--", "€", "n/a", "TBC", "1.2.3.4", " ", "?"):
        assert to_money(junk)[0] is None or isinstance(to_money(junk)[0], Decimal)
