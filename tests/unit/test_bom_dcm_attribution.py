"""
UNIT · DCM category classification + per-label DXF area attribution. No database, no
event loop — every function here is a pure staticmethod on BomService or a pure
function in app/modules/bom/pattern.py.

THE CLAIM UNDER TEST is that a material line gets the consumption of the pattern
pieces it actually owns.

The defect these cover: the spec extractor files every non-shell, non-lining material
under `sub_materials`, including lining textiles. The seed builder stamped all of them
`sub_material`, so the resolver asked the DXF for a `sub_material` area that does not
exist (those pieces are labelled 裏地/スレーキ, which the lexicon maps to `lining`).
Every such line fell through the whole source ladder to provisional/qty 0 while the
one real lining line silently absorbed their area.

Worked reference is the BeauGeste jacket CR1-02F5-JK26: shell 表生地 (sheep), lining
裏地 + pocketing スレーキ (both polyester taffeta), plus a declared ダイヤキルト lining
the DXF has no piece for.
"""
from decimal import Decimal

# Constructing a BomItem configures the ORM mappers, and a mapper cannot resolve its
# relationships unless every model module is imported (CLAUDE.md §11). conftest does
# this for the DB layers; repeat it so this pure file also stands alone.
import app.core.models                      # noqa: F401
import app.modules.clients.models           # noqa: F401
import app.modules.employees.models         # noqa: F401
import app.modules.users.models             # noqa: F401
import app.modules.production.models        # noqa: F401
import app.modules.barcode.models           # noqa: F401
import app.modules.attendance.models        # noqa: F401
import app.modules.wages.models             # noqa: F401
import app.modules.procurement.models       # noqa: F401
import app.modules.supplier_po.models       # noqa: F401
import app.modules.bom.models               # noqa: F401
import app.modules.inventory.models         # noqa: F401

from app.modules.bom.pattern import (_net_qty_for_category, dcm_for_category,
                                     dcm_for_labels, net_qty_for_labels)
from app.modules.bom.service import BomService

MAIN = "表生地"        # omote-kiji — the leather shell
URAJI = "裏地"         # lining
SUREEKI = "スレーキ"    # pocketing (note the chōon; the spec writes it スレキ)
BEPPU = "別布"         # contrast leather panel


class FakePattern:
    """Anything exposing .fabric_roles / .fabric_matrix / .master_size is a pattern,
    per pattern.py's own contract."""

    def __init__(self, matrix, roles, master_size="XL"):
        self.fabric_matrix = matrix
        self.fabric_roles = roles
        self.master_size = master_size


def _jacket():
    """Shell 20.35 sf, lining 12.40 sf + pocketing 6.00 sf (18.40 sf of lining-category
    area across TWO labels — the split that used to collapse onto one line)."""
    return FakePattern(
        matrix={"XL": {MAIN: 20.35, URAJI: 12.40, SUREEKI: 6.00}},
        roles={MAIN: {"role": "main", "category": "main_material", "is_leather": True},
               URAJI: {"role": "lining", "category": "lining", "is_leather": False},
               SUREEKI: {"role": "pocketing", "category": "lining", "is_leather": False}},
    )


# ═══════════════════════════════════════════ BomService._sub_material_role
def test_lining_textile_filed_under_sub_materials_is_classified_lining():
    """裏地 is a lining, whatever list the extractor put it in. Stamping it
    `sub_material` is what made it ask the DXF for an area that never existed."""
    fr = BomService._sub_material_role({"name": URAJI, "material": "ポリエステルタフタ"})
    assert fr.category == "lining"
    assert fr.is_leather is False


def test_pocketing_matches_despite_the_missing_choon():
    """The spec writes スレキ, the DXF writes スレーキ. The lexicon compares
    chōon-insensitively, so the same fabric must resolve from either spelling."""
    fr = BomService._sub_material_role({"name": "スレキ", "material": "ポリエステルタフタ"})
    assert fr.category == "lining"


def test_a_real_contrast_panel_is_still_sub_material_leather():
    """The regression that matters most: 別布 IS a genuine secondary leather. The fix
    must not sweep real sub-materials into lining."""
    fr = BomService._sub_material_role({"name": BEPPU, "material": "GOAT"})
    assert fr.category == "sub_material"
    assert fr.is_leather is True


def test_unknown_native_label_falls_back_to_sub_material():
    """No lexicon opinion -> previous behaviour, with is_leather from the hide species."""
    fr = BomService._sub_material_role({"name": "ZZUNKNOWN", "material": "GOAT"})
    assert fr.category == "sub_material"
    assert fr.is_leather is True


def test_unknown_non_hide_material_is_sub_material_but_not_leather():
    fr = BomService._sub_material_role({"name": "ZZUNKNOWN", "material": "POLYESTER"})
    assert fr.category == "sub_material"
    assert fr.is_leather is False


def test_entry_with_no_native_label_still_resolves():
    """sub.name is optional in the extraction schema — must not raise."""
    fr = BomService._sub_material_role({"material": "GOAT"})
    assert fr.category == "sub_material"


# ═════════════════════════════ the two mirrored readers stay in agreement
def _attrs():
    return {"leather_quality": "SHEEP LEATHER",
            "sub_materials": [{"name": URAJI, "material": "ポリエステルタフタ"},
                              {"name": "スレキ", "material": "ポリエステルタフタ"}],
            "lining": "ダイヤキルト"}


def test_build_line_seeds_no_longer_mislabels_the_taffeta_lines():
    seeds = BomService._build_line_seeds(_attrs(), garment_code=None)
    by_annotation = {s.annotation: s for s in seeds if s.annotation}
    assert by_annotation[URAJI].category == "lining"
    assert by_annotation["スレキ"].category == "lining"


def test_spec_materials_for_mirrors_build_line_seeds():
    """Their docstrings require an EXACT mirror — a matched DXF label has to point at
    the same category the BOM line carries, or attribution and seeding drift."""
    seeds = BomService._build_line_seeds(_attrs(), garment_code=None)
    mats = BomService._spec_materials_for(_attrs())
    seed_cats = sorted(s.category for s in seeds
                       if s.source_ref == "spec.attributes.sub_materials")
    mat_cats = sorted(m.category for m in mats if m.name == "ポリエステルタフタ")
    assert seed_cats == mat_cats == ["lining", "lining"]


# ═══════════════════════════════════════════ pattern.net_qty_for_labels
def test_per_label_area_sums_back_to_the_category_total():
    """The conservation property: splitting by label must neither create nor lose
    area. 12.40 + 6.00 == the 18.40 the category reports as a whole."""
    p = _jacket()
    _, whole = _net_qty_for_category(p, "lining", "XL")
    _, uraji = net_qty_for_labels(p, [URAJI], "XL")
    _, sureeki = net_qty_for_labels(p, [SUREEKI], "XL")
    assert whole == 18.40
    assert round(uraji + sureeki, 2) == whole


def test_a_label_set_that_matches_nothing_yields_no_area():
    """Never 0.0-as-a-number — None, so the caller flags it provisional rather than
    costing a real line at zero."""
    p = _jacket()
    assert net_qty_for_labels(p, ["ダイヤキルト"], "XL") == (None, 0.0)
    assert net_qty_for_labels(p, [], "XL") == (None, 0.0)


def test_dcm_for_labels_applies_the_same_multiplier_as_the_category():
    """Only the measured area narrows; the leather-yield vs fabric-wastage choice
    still comes from the category."""
    p = _jacket()
    whole = dcm_for_category(p, category="lining", size="XL")
    part = dcm_for_labels(p, labels=[URAJI, SUREEKI], category="lining", size="XL")
    assert part == whole


def test_leather_category_uses_the_species_yield_not_fabric_wastage():
    p = _jacket()
    d = dcm_for_labels(p, labels=[MAIN], category="main_material", size="XL",
                       species="sheep", yields={"sheep": 2.5})
    assert d == (Decimal("20.35") * Decimal("2.5") * Decimal("9.290304")).quantize(Decimal("0.01"))


# ═══════════════════════════════ BomService._shared_material_categories
class _Seed:
    def __init__(self, category, name, annotation=None):
        self.category, self.name, self.annotation = category, name, annotation


def test_only_categories_with_siblings_need_the_split():
    """A single-line category must keep the untouched source ladder — that path is
    what currently produces the correct main_material number."""
    seeds = [_Seed("main_material", "SHEEP"), _Seed("lining", "A"),
             _Seed("lining", "B"), _Seed("accessory", "ZIP")]
    assert BomService._shared_material_categories(seeds) == {"lining"}


def test_non_material_categories_are_never_shared():
    """Three accessory lines are normal and are not DCM-resolved at all."""
    seeds = [_Seed("accessory", "A"), _Seed("accessory", "B"), _Seed("accessory", "C")]
    assert BomService._shared_material_categories(seeds) == set()


# ═══════════════════════════════════════════ BomService._labels_for_seed
def test_a_line_claims_the_dxf_label_its_native_term_names():
    """裏地 the BOM annotation binds to 裏地 the DXF label — the link that survives a
    lexicon hit, which carries no matched_material."""
    p = _jacket()
    owned = BomService._labels_for_seed(_Seed("lining", "ポリエステルタフタ", URAJI), p, {})
    assert owned == [URAJI]


def test_the_choon_spelling_difference_still_binds():
    p = _jacket()
    owned = BomService._labels_for_seed(_Seed("lining", "ポリエステルタフタ", "スレキ"), p, {})
    assert owned == [SUREEKI]


def test_a_line_the_dxf_has_no_piece_for_owns_nothing():
    """ダイヤキルト is declared in the spec but absent from the pattern. It must own no
    labels, so it stays provisional instead of absorbing its siblings' area — which is
    exactly what the old category-total path did."""
    p = _jacket()
    owned = BomService._labels_for_seed(_Seed("lining", "ダイヤキルト"), p, {})
    assert owned == []


def test_no_pattern_means_no_labels():
    assert BomService._labels_for_seed(_Seed("lining", "A", URAJI), None, {}) == []


# ════════════════════════════════ _build_items, end to end on the real order
class _StubRepo:
    """Every lookup misses, so the DXF is the only source in play — the state a first
    order for a style is actually in."""

    async def get_material_rate(self, name, uom=None):
        return None

    async def find_consumption_template(self, **kw):
        return None

    async def get_consumption_template(self, *a, **kw):
        return None

    async def find_similar_template(self, **kw):
        return None


class _Identity:
    client_id = None


def _build(seeds, pattern, yields):
    import asyncio
    svc = BomService.__new__(BomService)
    svc.repo = _StubRepo()
    return asyncio.run(svc._build_items(
        seeds, identity=_Identity(), sig="CR1-02F5-JK26", gt=None, gt_id=None,
        base_size="XL", poms_for_size={}, pattern_template_id=None,
        pattern=pattern, attr_by_label={}, dxf_yields=yields))


def test_the_taffeta_lines_now_carry_their_own_measured_consumption():
    """The reported defect. Both taffeta lines came back provisional/0 while the whole
    18.40 sf of lining area sat on the ダイヤキルト line. Each must now carry the area
    of the pattern pieces it owns."""
    items = _build(BomService._build_line_seeds(_attrs(), garment_code=None),
                   _jacket(), {"sheep": 2.5, "_default": 2.5})
    by_annotation = {i.annotation: i for i in items if i.annotation}

    uraji, sureki = by_annotation[URAJI], by_annotation["スレキ"]
    assert uraji.dcm_source == "dxf" and uraji.qty_per_garment == Decimal("132.48")
    assert sureki.dcm_source == "dxf" and sureki.qty_per_garment == Decimal("64.10")


def test_the_split_conserves_the_orders_total_lining_area():
    """132.48 + 64.10 == 196.58 — the exact figure the old single lining line reported.
    The fix re-attributes area; it must not invent or lose any."""
    items = _build(BomService._build_line_seeds(_attrs(), garment_code=None),
                   _jacket(), {"sheep": 2.5, "_default": 2.5})
    lining_total = sum(i.qty_per_garment for i in items if i.category == "lining")
    assert lining_total == Decimal("196.58")


def test_a_declared_material_the_pattern_lacks_stays_provisional():
    """ダイヤキルト is named by the spec but has no DXF piece. Per the agreed rule it
    holds at 0/provisional to be confirmed — it must never inherit a sibling's area."""
    items = _build(BomService._build_line_seeds(_attrs(), garment_code=None),
                   _jacket(), {"sheep": 2.5, "_default": 2.5})
    quilt = next(i for i in items if i.name == "ダイヤキルト")
    assert quilt.dcm_source == "provisional"
    assert quilt.qty_per_garment == Decimal("0")


def test_two_lines_claiming_one_label_both_stay_provisional():
    """The other door onto the same over-count: if the extractor lists 裏地 twice, both
    lines match the one 裏地 piece and would EACH take its full area. A contested label
    is withdrawn from both instead, so the area is flagged rather than doubled."""
    attrs = {"leather_quality": "SHEEP LEATHER",
             "sub_materials": [{"name": URAJI, "material": "ポリエステルタフタ"},
                               {"name": URAJI, "material": "ナイロンタフタ"}],
             "lining": "ダイヤキルト"}
    items = _build(BomService._build_line_seeds(attrs, garment_code=None),
                   _jacket(), {"sheep": 2.5, "_default": 2.5})
    claimants = [i for i in items if i.annotation == URAJI]
    assert len(claimants) == 2
    assert all(i.dcm_source == "provisional" and i.qty_per_garment == Decimal("0")
               for i in claimants)


def test_the_single_line_main_material_number_is_unchanged():
    """The regression guard. main_material is alone in its category, so it keeps the
    untouched category-total ladder and must still report the production figure."""
    items = _build(BomService._build_line_seeds(_attrs(), garment_code=None),
                   _jacket(), {"sheep": 2.5, "_default": 2.5})
    shell = next(i for i in items if i.category == "main_material")
    assert shell.dcm_source == "dxf"
    assert shell.qty_per_garment == Decimal("472.64")
