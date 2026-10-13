"""
UNIT · the pure predicates of the MATERIAL and BARCODE modules.

No database, no session, no HTTP. Everything here is a module-level function or
a @staticmethod, which is exactly why it belongs in layer 1: these are the
decisions the two modules make BEFORE they touch a row, and a regression in any
of them is silent at every other layer.

WHAT IS ASSERTED, AND WHY EACH ONE IS LOAD-BEARING

  encode_short / decode_short   the compact piece code (bug #19). The alphabet
                                omits 0/O and 1/I, and the encoding must be
                                fixed-width so that lexicographic order IS
                                numeric order — that property is what lets
                                `max_short_code_counter` transfer ONE row
                                instead of scanning the prefix.
  _norm                         every registry lookup normalises through it; a
                                change here silently breaks resolve().
  display_stock                 the three numbers the whole stock UI renders.
  _match_order_to_lot           decides 409-vs-accept on every receive.
  _caption                      what is printed on a lot label.
  filter_fields                 drives the Add-New form per category.
  merge_lines / applies_to_size the recipe a garment actually gets — the money
                                path, because it is what the kit SPENDS.
"""
import types
import uuid
from decimal import Decimal

import pytest

from app.core.enums import resolve_spec

from app.modules.barcode.repository import (
    SHORT_CODE_PREFIX, _B30, _norm, decode_short, encode_short,
)
from app.modules.barcode.service import BarcodeService
from app.modules.materials.service import (
    MaterialService, display_stock, sheet_rollup, stock_numbers,
)
from app.modules.materials.style_spec_service import StyleSpecService

pytestmark = pytest.mark.integrity


# ══════════════════════════════════════════════════════ the compact code
class TestShortCode:
    def test_counter_one_encodes_to_a_fixed_width_code(self):
        code = encode_short(1)
        assert code.startswith(f"{SHORT_CODE_PREFIX}-")
        assert len(code.split("-")[1]) == 6

    def test_encode_decode_round_trips_across_the_range(self):
        for n in (1, 2, 29, 30, 31, 900, 12345, 999999):
            assert decode_short(encode_short(n)) == n

    def test_the_alphabet_omits_the_four_confusable_characters(self):
        """0/O and 1/I are what a human re-keying a smudged label confuses."""
        for ch in ("0", "O", "1", "I", "L", "U"):
            assert ch not in _B30

    def test_lexicographic_order_is_numeric_order(self):
        """The property `max_short_code_counter` and `_next_code` both rely on:
        ORDER BY code DESC LIMIT 1 must return the highest counter."""
        codes = [encode_short(n) for n in range(1, 200)]
        assert codes == sorted(codes)

    def test_a_negative_counter_clamps_rather_than_raising(self):
        assert encode_short(-5) == encode_short(0)

    @pytest.mark.parametrize("junk", [None, "", "PC", "PC-", "XX-222223",
                                      "PC-2222O3", "EMP-000001"])
    def test_decode_returns_zero_for_anything_that_is_not_a_short_code(self, junk):
        """Zero, never an exception: a stray row must not poison the counter."""
        assert decode_short(junk) == 0

    def test_decode_is_case_and_whitespace_insensitive(self):
        code = encode_short(77)
        assert decode_short(f"  {code.lower()}  ") == 77


class TestNorm:
    @pytest.mark.parametrize("raw,want", [
        (None, ""), ("", ""), ("  pc-222223 ", "PC-222223"),
        ("emp-000001", "EMP-000001"), ("\tLOT-LEA-000001\n", "LOT-LEA-000001"),
    ])
    def test_codes_are_upper_cased_and_stripped(self, raw, want):
        assert _norm(raw) == want


# ══════════════════════════════════════════════ the three stock numbers
class TestDisplayStock:
    """arrived / used / balance — and they RECONCILE, which is the whole point.

    The middle number used to be `used + active_reserved` under the name
    "reserved", so a lot looked more and more committed the more of it was cut,
    and `used` itself appeared nowhere. `available` was right either way (the
    term cancels), which is how it survived: the one figure anybody checked was
    correct for the wrong reason.
    """

    def test_arrived_is_what_is_left_plus_what_was_spent(self):
        arrived, used, balance = display_stock(100, 400, 0)
        assert (arrived, used, balance) == (
            Decimal("500"), Decimal("400"), Decimal("100"))

    def test_a_reservation_does_not_change_any_of_the_three(self):
        """A reservation is a claim on stock, not a movement of it.

        It lowers what may be PROMISED, which is `available` on the full block —
        never what arrived, what was used, or what is on the shelf.
        """
        assert display_stock(100, 0, 30) == (
            Decimal("100"), Decimal("0"), Decimal("100"))
        assert stock_numbers(100, 0, 30)["available"] == 70.0

    def test_nulls_read_as_zero_rather_than_raising(self):
        assert display_stock(None, None, None) == (
            Decimal("0"), Decimal("0"), Decimal("0"))

    def test_the_three_always_reconcile(self):
        """arrived − used == balance, always. A stock screen whose figures do not
        add up is one the floor stops believing."""
        arrived, used, balance = display_stock(
            Decimal("12.5"), Decimal("7.25"), Decimal("1.25"))
        assert arrived - used == balance

    def test_the_block_spells_the_same_numbers_out_consistently(self):
        """stock_numbers is what every read renders, so it cannot drift from the
        function under it — and `on_hand` must mean BALANCE, not arrived."""
        out = stock_numbers(Decimal("100"), Decimal("400"), Decimal("30"))
        assert out["arrived"] == 500.0
        assert out["used"] == 400.0
        assert out["balance"] == 100.0
        assert out["on_hand"] == out["balance"]
        assert out["reserved"] == 30.0
        assert out["available"] == 70.0

    def test_the_service_staticmethod_is_the_same_function(self):
        """style_spec_service imports the module-level one; MaterialService keeps
        the staticmethod alias. They must not become two implementations."""
        assert MaterialService._display_stock(10, 1, 1) == display_stock(10, 1, 1)


# ══════════════════════════════════════════════ the same three, in HIDES
class TestSheetRollup:
    """A cutter is handed SKINS. "How many are on the shelf" is not answerable
    from a sum of decimetres, so the hide count is its own roll-up."""

    def test_the_buckets_split_the_shelf_from_what_is_out_and_what_is_gone(self):
        out = sheet_rollup({
            "IN_STOCK": {"count": 5, "dcm": 250.0},
            "RETURNED": {"count": 1, "dcm": 40.0},
            "ALLOCATED": {"count": 2, "dcm": 95.0},
            "ISSUED": {"count": 1, "dcm": 48.0},
            "CONSUMED": {"count": 9, "dcm": 430.0},
            "SCRAPPED": {"count": 1, "dcm": 12.0},
        })
        assert out["sheets_balance"] == 6          # on the shelf: IN_STOCK + RETURNED
        assert out["sheets_allocated"] == 3        # out on a row, not yet cut
        assert out["sheets_used"] == 9             # CONSUMED
        assert out["sheets_scrapped"] == 1
        assert out["sheets_arrived"] == 19
        assert out["sheets_arrived_dcm"] == 875.0

    def test_a_lot_nobody_sheeted_is_zeroes_not_a_crash(self):
        """Lining is metres and a button is a button — neither has hides."""
        out = sheet_rollup({})
        assert out["sheets_arrived"] == 0
        assert out["sheets_balance"] == 0
        assert out["sheets_by_status"] == {}


# ══════════════════════════════════════════════ PO vs delivered matching
def _order(**kw):
    base = {"article": "SUEDE-A32", "colour": None, "thickness": None, "dcm": None}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _lot(**kw):
    base = {"article": "SUEDE-A32", "colour": "PINE", "thickness": "1.2mm",
            "attributes": {"dcm": 400}}
    base.update(kw)
    return types.SimpleNamespace(**base)


class TestMatchOrderToLot:
    def test_an_exact_match_returns_no_differences(self):
        assert MaterialService._match_order_to_lot(
            _order(colour="PINE", thickness="1.2mm", dcm=Decimal("400")),
            _lot()) == []

    def test_a_field_the_order_never_specified_is_not_a_mismatch(self):
        """Ordering '400 dcm of SUEDE-A32' without naming a colour means any
        colour is acceptable. Flagging it 409'd perfectly good deliveries."""
        assert MaterialService._match_order_to_lot(_order(), _lot()) == []

    def test_a_wrong_colour_against_an_order_that_named_one_conflicts(self):
        assert MaterialService._match_order_to_lot(
            _order(colour="NAVY"), _lot()) == ["colour"]

    def test_article_is_always_compared(self):
        assert "article" in MaterialService._match_order_to_lot(
            _order(article="NAP-11"), _lot())

    def test_comparison_ignores_case_and_surrounding_space(self):
        assert MaterialService._match_order_to_lot(
            _order(article="  suede-a32 ", colour="pine"), _lot()) == []

    def test_thickness_and_dcm_both_report_when_both_differ(self):
        diffs = MaterialService._match_order_to_lot(
            _order(thickness="0.8mm", dcm=Decimal("300")), _lot())
        assert diffs == ["thickness", "dcm"]

    def test_a_dcm_order_against_a_lot_with_no_dcm_attribute_conflicts(self):
        assert MaterialService._match_order_to_lot(
            _order(dcm=Decimal("400")), _lot(attributes=None)) == ["dcm"]


# ══════════════════════════════════════════════════════ the lot caption
class TestCaption:
    def test_leather_prints_article_colour_thickness_and_quantity(self):
        body = types.SimpleNamespace(article="SUEDE-A32", colour="PINE")
        caption = MaterialService._caption(
            "LEATHER", None, body, {"thickness": "1.2mm"}, Decimal("400"), "dcm")
        assert caption == "SUEDE-A32 · PINE · 1.2mm · 400 dcm"

    def test_a_button_prints_its_size_instead_of_a_thickness(self):
        body = types.SimpleNamespace(article="BTN-4H", colour="BLACK")
        caption = MaterialService._caption(
            "ACCESSORY", "BUTTON", body, {"size": "18L"}, Decimal("500"), "pcs")
        assert "18L" in caption and "500 pcs" in caption

    def test_accessory_other_prints_its_description(self):
        body = types.SimpleNamespace(article="MISC", colour="RED")
        caption = MaterialService._caption(
            "ACCESSORY", "OTHER", body, {"description": "HANG TAG"},
            Decimal("10"), "pcs")
        assert "HANG TAG" in caption

    def test_an_unknown_key_falls_back_to_article_colour_quantity(self):
        body = types.SimpleNamespace(article="X", colour="Y")
        assert MaterialService._caption(
            "LINING", "UNKNOWN_SUBTYPE", body, {}, Decimal("1"), "mtrs") \
            == "X · Y · 1 mtrs"

    def test_a_missing_optional_field_is_dropped_not_printed_as_none(self):
        body = types.SimpleNamespace(article="RIB-1", colour=None)
        caption = MaterialService._caption(
            "LINING", "RIBS", body, {}, Decimal("5"), "kg")
        assert "None" not in caption


# ══════════════════════════════════════════════ which form to render
class TestFilterFields:
    @pytest.mark.parametrize("category,subtype,qty_field,uom", [
        ("LEATHER", None, "dcm", "dcm"),
        ("LINING", None, "mtrs", "mtrs"),          # defaults to PLAIN_LINING
        ("LINING", "RIBS", "kg", "kg"),
        ("LINING", "KNIT", "pcs", "pcs"),
        ("ACCESSORY", "BUTTON", "count", "pcs"),
        ("ACCESSORY", "ZIP", "count", "pcs"),
        ("ACCESSORY", "THREAD", "mtrs", "mtrs"),
        ("ACCESSORY", "OTHER", "count", "pcs"),
    ])
    def test_every_category_reports_its_own_quantity_field_and_unit(
            self, category, subtype, qty_field, uom):
        """The CLAUDE.md §5 table, asserted. The quantity is read from the
        category's own field — never a generic 'qty'.

        ASSERTED ON `resolve_spec`, NOT THROUGH `MaterialService.filter_fields`.
        That wrapper is now async and catalogue-backed — an accessory KIND is a row
        in accessory_type, so rendering its form needs a database. The property this
        file exists to protect is the pure built-in table underneath, and asserting
        it through a wrapper that has grown a dependency is how a unit test quietly
        becomes an integration test. The wrapper's own behaviour is covered in
        tests/integration/test_accessory_catalogue.py.
        """
        spec = resolve_spec(category, subtype)
        assert spec["qty_field"] == qty_field
        assert spec["qty_uom"] == uom

    def test_leather_and_thread_filter_by_thickness_buttons_by_size(self):
        assert "thickness" in resolve_spec("LEATHER", None)["filters"]
        assert "thickness" in resolve_spec("ACCESSORY", "THREAD")["filters"]
        assert "size" in resolve_spec("ACCESSORY", "BUTTON")["filters"]

    def test_ribs_and_knit_filter_by_article_and_colour_only(self):
        for subtype in ("RIBS", "KNIT"):
            assert resolve_spec("LINING", subtype)["filters"] \
                == ["article", "colour"]

    def test_an_accessory_with_no_subtype_has_no_built_in_spec(self):
        """There is no generic accessory quantity, so the built-in table has no row
        for a subtype-less accessory. `filter_fields` is what turns that None into a
        form the screen can still render — see the integration test."""
        assert resolve_spec("ACCESSORY", None) is None

    def test_the_category_is_matched_case_insensitively(self):
        assert resolve_spec("leather", None) is resolve_spec("LEATHER", None)

    def test_an_empty_category_has_no_spec(self):
        assert resolve_spec("", None) is None


# ══════════════════════════════════════════════ which code to scan next
def _payload(**kw):
    base = {"piece": None, "employee": None, "drawer": None, "lot": None,
            "sheet": None}
    base.update(kw)
    return base


class TestNextExpectedScan:
    def test_an_employee_card_asks_for_the_garment(self):
        """The first resolve of every workflow. It used to come back null."""
        assert BarcodeService._next_expected_scan(
            _payload(employee={"employee_id": "x"})) == "PIECE"

    def test_a_lot_label_asks_for_the_garment_it_is_cut_for(self):
        assert BarcodeService._next_expected_scan(
            _payload(lot={"lot_id": "x"})) == "PIECE"

    def test_a_hide_asks_for_the_garment_it_is_cut_for(self):
        assert BarcodeService._next_expected_scan(
            _payload(sheet={"sheet_id": "x"})) == "PIECE"

    def test_a_piece_is_the_end_of_the_scan_not_the_middle(self):
        """The store scan is two codes — worker, garment. After the garment
        there is nothing left to present."""
        assert BarcodeService._next_expected_scan(
            _payload(piece={"piece_id": "x"})) is None

    def test_a_legacy_drawer_label_can_no_longer_ask_for_a_pairing_scan(self):
        assert BarcodeService._next_expected_scan(
            _payload(drawer={"drawer_id": "x"})) is None

    def test_an_empty_payload_falls_through_to_none(self):
        assert BarcodeService._next_expected_scan(_payload()) is None


# ══════════════════════════════════════════════ the lot-barcode print row
class TestLotBarcodeRow:
    def test_the_row_pre_joins_the_sticker_text(self):
        row = BarcodeService._lot_barcode_row((
            "LOT-LEA-000001", "LEATHER_LOT", "active", "cap",
            uuid.uuid4(), "LEATHER", None, "SUEDE-A32", "PINE", "1.2mm", None,
            "dcm", Decimal("400")))
        assert row["label_line"] == "SUEDE-A32 · PINE · 1.2mm · 400.0 dcm"
        assert row["on_hand"] == 400.0

    def test_a_null_thickness_falls_back_to_the_size(self):
        row = BarcodeService._lot_barcode_row((
            "LOT-ACC-000001", "ACCESSORY_LOT", "active", None,
            uuid.uuid4(), "ACCESSORY", "BUTTON", "BTN-4H", "BLACK", None, "18L",
            "pcs", Decimal("500")))
        assert "18L" in row["label_line"]

    def test_a_null_quantity_reads_as_zero(self):
        row = BarcodeService._lot_barcode_row((
            "LOT-LIN-000001", "LINING_LOT", "retired", None, uuid.uuid4(),
            "LINING", "RIBS", "RIB-1", None, None, None, "kg", None))
        assert row["on_hand"] == 0.0
        assert row["status"] == "retired"


# ══════════════════════════════════════════════ is this a GARMENT size?
class TestReadsAsGarmentSize:
    """WHAT THIS PREDICATE IS FOR CHANGED, and the tests changed with it.

    It used to INFER `garment_size` from a material size. It no longer infers
    anything — garment_size is explicit or NULL — and this is now only the trigger
    for the 422 that REFUSES a line whose material size is unmistakably a garment
    size and which does not say which garments it is for. So the bar moved: it must
    be certain, not merely plausible.
    """
    @pytest.mark.parametrize("value", ["S", "M", "L", "XL", "XXL", "2XL"])
    def test_an_alpha_rung_is_unmistakably_a_garment_size(self, value):
        assert StyleSpecService._reads_as_garment_size(value) is True

    @pytest.mark.parametrize("value", ["48", "52", "70", "60", "45", "63"])
    def test_a_BARE_NUMBER_IS_NOT_READ_AS_A_SIZE_ANY_MORE(self, value):
        """THE BUG THIS FIXES, and it cost a shipment's worth of missing zips.

        Every number from 30 to 70 used to read as a garment size, because those
        are the EU jacket rungs — so a 60cm zip entered as size '60' was scoped to
        4XL garments and a 45cm zip to XS. And a line scoped to a size no garment
        has is not a smaller recipe: it is ABSENT. merge_lines drops it,
        kit_required comes back False, and every other size ships with no zip.

        '50' is a garment size on one client's sheet and a centimetre length on the
        next, so it cannot be told from the token. Recipe-wide,
        kit_rules.accessory_size_ambiguities asks the DM instead of guessing.
        """
        assert StyleSpecService._reads_as_garment_size(value) is False

    @pytest.mark.parametrize("value", ["60CM", "18L", "", None, "  "])
    def test_a_material_measurement_does_not(self, value):
        """A 60cm zip is not an L jacket. Reading it as one would confine a
        perfectly general line to a size that does not exist."""
        assert StyleSpecService._reads_as_garment_size(value) is False

    @pytest.mark.parametrize("value", ["M/L", "XL-2", "SIZE 4", "4.5MM",
                                       "63 · YKK-169"])
    def test_a_token_with_letters_and_punctuation_is_a_material_label(self, value):
        """Letters plus anything non-alphanumeric is a description, not a rung
        on the size ladder."""
        assert StyleSpecService._reads_as_garment_size(value) is False

    def test_the_check_is_case_insensitive(self):
        assert StyleSpecService._reads_as_garment_size("m") is True


# ══════════════════════════════════════════════════ does a line apply?
def _line(**kw):
    """A stand-in for one StyleMaterialSpec row.

    `thickness` IS HERE BECAUSE THE REAL MODEL ALWAYS HAS IT. It was missing, and
    the fake only got away with it while nothing read the column — the moment the
    accessory merge key started distinguishing materials by their full identity,
    every test using this blew up with an AttributeError that said nothing about
    what had actually changed. A fake that is missing a column the model guarantees
    is a fake that can only mislead.
    """
    base = {"id": uuid.uuid4(), "sku_id": None, "category": "ACCESSORY",
            "subtype": "BUTTON", "article": "BTN-4H", "colour": "BLACK",
            "thickness": None, "size": None, "garment_size": None,
            "qty_per_piece": Decimal("4"), "uom": "pcs"}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _wide(**kw):
    """A LINING line — the kind that can still be style-wide.

    Accessories name their SKU now, so the style-wide/override merge these tests
    pin is leather and lining's behaviour. Asserting it through an accessory would
    be asserting a shape the write path refuses to create.
    """
    base = {"category": "LINING", "subtype": "KNIT", "article": "KNIT-1",
            "colour": "BLACK", "thickness": "0.4mm", "uom": "pcs"}
    base.update(kw)
    return _line(**base)


class TestAppliesToSize:
    def test_a_line_with_no_garment_size_applies_to_every_size(self):
        """NULL means every size — what makes the column back-compatible with
        every line that predates it."""
        assert StyleSpecService.applies_to_size(_line(), "M") is True

    def test_a_sized_line_applies_to_its_own_size(self):
        assert StyleSpecService.applies_to_size(_line(garment_size="L"), "L") is True

    def test_a_sized_line_does_not_reach_another_size(self):
        assert StyleSpecService.applies_to_size(_line(garment_size="L"), "S") is False

    def test_a_piece_of_unknown_size_is_never_dropped(self):
        assert StyleSpecService.applies_to_size(_line(garment_size="L"), None) is True

    def test_the_italian_ladder_makes_52_and_l_the_same_garment(self):
        assert StyleSpecService.applies_to_size(_line(garment_size="52"), "L") is True

    def test_matching_ignores_case_and_space(self):
        assert StyleSpecService.applies_to_size(_line(garment_size=" l "), "L") is True


# ══════════════════════════════════════════════════════ the recipe merge
class TestMergeLines:
    """THE STYLE-WIDE / OVERRIDE MERGE, which is leather and lining's now.

    An accessory line names the SKU it is for, so there is no style-wide accessory
    to override and none of the precedence below applies to one. What accessories
    need from this function is further down: that two materials which differ only
    by colour do not collapse into one.
    """

    def test_a_style_wide_line_reaches_every_colourway(self):
        line = _wide()
        assert StyleSpecService.merge_lines([line], uuid.uuid4(), "M") == [line]

    def test_a_sku_line_replaces_the_style_line_with_the_same_key(self):
        sku = uuid.uuid4()
        wide = _wide(qty_per_piece=Decimal("4"))
        narrow = _wide(sku_id=sku, qty_per_piece=Decimal("6"))
        merged = StyleSpecService.merge_lines([wide, narrow], sku, "M")
        assert merged == [narrow]

    def test_a_sku_line_with_a_different_key_is_added_alongside(self):
        sku = uuid.uuid4()
        wide = _wide(article="KNIT-1")
        extra = _wide(sku_id=sku, article="KNIT-2")
        merged = StyleSpecService.merge_lines([wide, extra], sku, "M")
        assert {l.article for l in merged} == {"KNIT-1", "KNIT-2"}

    def test_another_colourways_override_never_reaches_this_garment(self):
        """Issuing it would put the wrong colour in the bag."""
        theirs = _line(sku_id=uuid.uuid4(), article="KNIT-PINE")
        assert StyleSpecService.merge_lines([theirs], uuid.uuid4(), "M") == []

    def test_a_zeroed_override_removes_the_material_for_that_colourway(self):
        sku = uuid.uuid4()
        wide = _wide()
        zeroed = _wide(sku_id=sku, qty_per_piece=Decimal("0"))
        assert StyleSpecService.merge_lines([wide, zeroed], sku, "M") == []

    def test_garment_size_is_part_of_the_key_so_sized_lines_do_not_collapse(self):
        """Thread S / M / L used to be one key — the last one silently won.

        LINING, because garment_size is how a style-wide line is scoped to a size
        and only leather and lining can still be style-wide.
        """
        s_line = _wide(article="KNIT-S", garment_size="S")
        m_line = _wide(article="KNIT-S", garment_size="M")
        merged_for_m = StyleSpecService.merge_lines([s_line, m_line], None, "M")
        assert merged_for_m == [m_line]

    def test_a_none_sku_id_ignores_every_override(self):
        theirs = _line(sku_id=uuid.uuid4())
        assert StyleSpecService.merge_lines([theirs], None, "M") == []


class TestMergeLinesKeepsAccessoriesApart:
    """AN ACCESSORY'S KEY IS ITS FULL MATERIAL IDENTITY, and that is not symmetry.

    The leather/lining key deliberately ignores colour, because a NAVY knit line
    SHOULD override the style-wide knit — same material, one colourway's version of
    it. Accessories have no overriding to do, so ignoring colour buys nothing and
    costs a real case: a two-tone garment taking the same button article in two
    colours is two lines, and a key without colour would silently keep one.
    """

    def test_two_colours_of_one_button_on_one_sku_are_two_lines(self):
        sku = uuid.uuid4()
        black = _line(sku_id=sku, colour="BLACK")
        navy = _line(sku_id=sku, colour="NAVY")
        merged = StyleSpecService.merge_lines([black, navy], sku, "M")
        assert {l.colour for l in merged} == {"BLACK", "NAVY"}

    def test_two_sizes_of_one_button_on_one_sku_are_two_lines(self):
        sku = uuid.uuid4()
        small = _line(sku_id=sku, size="18L")
        large = _line(sku_id=sku, size="20L")
        merged = StyleSpecService.merge_lines([small, large], sku, "M")
        assert {l.size for l in merged} == {"18L", "20L"}

    def test_the_same_material_twice_still_collapses(self):
        """The key is an identity, so a genuine duplicate is still one line."""
        sku = uuid.uuid4()
        merged = StyleSpecService.merge_lines(
            [_line(sku_id=sku), _line(sku_id=sku)], sku, "M")
        assert len(merged) == 1

    def test_an_accessory_for_another_sku_is_dropped(self):
        assert StyleSpecService.merge_lines(
            [_line(sku_id=uuid.uuid4())], uuid.uuid4(), "M") == []


class TestLineNotApplicableReason:
    def test_a_line_that_reaches_the_garment_has_no_reason(self):
        assert StyleSpecService.line_not_applicable_reason(_line(), uuid.uuid4(),
                                                           "M") is None

    def test_another_colourways_line_reports_other_sku(self):
        assert StyleSpecService.line_not_applicable_reason(
            _line(sku_id=uuid.uuid4()), uuid.uuid4(), "M") == "other_sku"

    def test_a_zeroed_line_reports_zeroed(self):
        sku = uuid.uuid4()
        assert StyleSpecService.line_not_applicable_reason(
            _line(sku_id=sku, qty_per_piece=Decimal("0")), sku, "M") == "zeroed"

    def test_a_line_for_another_size_reports_other_size(self):
        assert StyleSpecService.line_not_applicable_reason(
            _line(garment_size="L"), None, "S") == "other_size"

    def test_the_sku_check_wins_over_the_size_check(self):
        """Order matters: a line scoped to another colourway is not this
        garment's whatever its size says."""
        assert StyleSpecService.line_not_applicable_reason(
            _line(sku_id=uuid.uuid4(), garment_size="L"), uuid.uuid4(),
            "S") == "other_sku"


# ══════════════════════════════════════════════ the printable kit summary
class TestSummaryLine:
    def test_no_accessories_prints_nothing(self):
        assert StyleSpecService._summary_line([]) is None

    def test_each_accessory_prints_quantity_unit_article_colour_size(self):
        line = StyleSpecService._summary_line([
            {"article": "BTN-4H", "colour": "BLACK", "size": "18L",
             "qty_per_piece": 4.0, "uom": "pcs"}])
        assert line == "4 pcs BTN-4H BLACK 18L"

    def test_several_accessories_are_joined_with_the_label_separator(self):
        line = StyleSpecService._summary_line([
            {"article": "A", "colour": None, "size": None,
             "qty_per_piece": 1.0, "uom": "pcs"},
            {"article": "B", "colour": None, "size": None,
             "qty_per_piece": 2.5, "uom": "mtrs"}])
        assert line == "1 pcs A · 2.5 mtrs B"


class TestKitLine:
    def test_the_kit_line_carries_the_spec_id_and_both_quantities(self):
        line = _line()
        row = StyleSpecService._kit_line(line, 4.0)
        assert row["spec_id"] == str(line.id)
        assert row["qty"] == 4.0 and row["qty_per_piece"] == 4.0
        assert "issued" not in row

    def test_an_issued_figure_is_added_only_when_supplied(self):
        row = StyleSpecService._kit_line(_line(), 0.0, issued=4.0)
        assert row["issued"] == 4.0
