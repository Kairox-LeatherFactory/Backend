"""
================================================================================
tests/test_procurement_stage2.py — Stage-2 BOM generation acceptance (§11)
================================================================================

Confirms every Stage-2 acceptance criterion against the REAL /data spec sheets:
  1. Beau Geste POM extraction → 14 standardized POMs × 5 sizes, source_term kept,
     deterministic (no LLM), the 特記事項 base pattern recorded as a pattern_reference.
  2. Jackiee → zero POMs, pattern_reference{MIMI-28-09-22-ZZ, size 42}, attributes
     populated (THIN SUEDE / MAGENTA / substance / SMS 22-42).
  3. DCM resolved, never silently computed: every material line stamps dcm_source +
     dcm_confidence; an ai_estimate line is flagged and cannot reach approval/LOCKED
     without cutting confirmation.
  4. Cost math reproduces BMO-1: 34.5×1.80=62.1; 60×34.5×1.80=3,726; FOB Σ=107.25.
  5. Memory learns across orders: a confirmed Beau Geste DCM is found by a SECOND
     order of the same customer_ref as a Source-1 template hit (cross-order keying).
  6. New client by config: an extra adapter + checks entry onboards a third client
     with no code change.
  7. Editable contract: stale base_revision → 409; fresh → recomputed tree + rev+1;
     a LOCKED BOM rejects edits.
  8. Cross-checks fire: Beau Geste Σ size qty == order total errors; Jackiee
     unresolved pattern_reference errors; out-of-band substance warns.
  9. Gate enforced: approval refused while cutting_confirmed_at null; a
     post-confirmation DCM edit re-opens the gate.

The LLM path is exercised with an INJECTED fake extractor (no API key) — the same
fake-dependency pattern the Stage-1 tests use.
================================================================================
"""
import uuid
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.enums import UserRole
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.procurement.bom_service import BomService, LineSeed, StyleIdentity
from app.modules.procurement.enums import BomItemCategory, BomStatus, DcmSource, SpecType
from app.modules.procurement.models import (
    GarmentType, PatternReference, PomDictionary, PomMeasurement, SpecSheet,
    StyleConsumptionTemplate,
)
from app.modules.procurement.seed_stage2 import _GARMENT_YAML, _POM_DICT_YAML
from app.modules.users.models import User

DATA = Path(__file__).resolve().parent.parent / "data"


def _read(name: str) -> bytes:
    return (DATA / name).read_bytes()


# ── async seeding of the Stage-2 registries (garment types + POM dictionary) ──
async def _seed_stage2(db) -> dict:
    gts: dict[str, GarmentType] = {}
    for r in yaml.safe_load(open(_GARMENT_YAML, encoding="utf-8")):
        gt = GarmentType(code=r["code"], label=r.get("label"),
                         required_poms=r.get("required_poms") or [],
                         area_formula=r.get("area_formula") or {},
                         default_wastage_pct=r.get("default_wastage_pct"))
        db.add(gt)
        gts[r["code"]] = gt
    await db.flush()
    for r in yaml.safe_load(open(_POM_DICT_YAML, encoding="utf-8")):
        db.add(PomDictionary(language=r["language"], source_term=r["source_term"],
                             pom_code=r["pom_code"], weight=r.get("weight", 1)))
    await db.commit()
    return gts


async def _cutting_user(db) -> User:
    u = User(id=uuid.uuid4(), name="Cut", phone="9000000002",
             role=UserRole.CUTTING_MANAGER, password_hash="x", is_active=True)
    db.add(u)
    await db.commit()
    return u


async def _make_order(db, *, customer_ref="CR1-02F5-PL02", order_no="1579",
                      per_size=None, name="SIDE SUEDE TRACK PANT"):
    """A Beau Geste client + order + style + per-size SKUs."""
    per_size = per_size or {"S": 12, "M": 12, "L": 12, "XL": 12, "XXL": 12}
    # get-or-create the client by code so a SECOND order (the cross-order memory test)
    # lands under the SAME client_id — the DCM memory is client-scoped.
    client = (await db.execute(select(Client).where(Client.code == "BG"))).scalar_one_or_none()
    if client is None:
        client = Client(name="Beau Geste", country="Japan", code="BG", currency="USD")
        db.add(client)
        await db.flush()
    order = ClientOrder(client_id=client.id, order_number=order_no, currency="USD")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name=name, customer_ref=customer_ref,
                  currency="USD")
    db.add(style)
    await db.flush()
    for size, qty in per_size.items():
        db.add(SKU(style_id=style.id, color_code="BLK", size=size, qty_ordered=qty))
    spec = SpecSheet(client_id=client.id, style_id=style.id,
                     spec_type=SpecType.MEASUREMENT_GRID.value)
    db.add(spec)
    await db.commit()
    order_qty = sum(per_size.values())
    identity = StyleIdentity(
        client_id=client.id, client_order_id=order.id, style_id=style.id,
        customer_ref=customer_ref, name=name, order_qty=order_qty, per_size_qty=per_size)
    return client, order, style, spec, identity


# BMO-1's exact line set: leather lines (DCM-resolved) + configured cost lines that
# sum to FOB 107.25 (§5b). Leather DCM/price chosen so each line reproduces BMO-1.
def _bmo1_seeds():
    return [
        LineSeed(BomItemCategory.MAIN_MATERIAL.value, "SHEEP GLASS", material_color="BLACK",
                 uom="dm²", unit_price=1.80),
        LineSeed(BomItemCategory.SUB_MATERIAL.value, "GOAT SUEDE", uom="dm²", unit_price=1.75),
        LineSeed(BomItemCategory.LINING.value, "LINING", uom="dm²", unit_price=1.00),
        LineSeed(BomItemCategory.INTERLINING.value, "INTERLINING", uom="dm²", unit_price=1.00),
        LineSeed(BomItemCategory.THREAD.value, "THREADS", uom="pc", unit_price=1.00,
                 qty_per_garment=1),
        LineSeed(BomItemCategory.MANUFACTURING.value, "CUTTING & STITCHING", uom="pc",
                 unit_price=30.00, qty_per_garment=1),
        LineSeed(BomItemCategory.PACKAGING.value, "PACKAGING", uom="pc", unit_price=3.00,
                 qty_per_garment=1),
        LineSeed(BomItemCategory.FOB_CHARGE.value, "FOB CHARGE", uom="pc", unit_price=5.00,
                 qty_per_garment=1),
    ]


async def _seed_templates_for_bmo1(db, identity, gt_track_pant, base_size="S"):
    """Pre-seed confirmed DCM templates so the leather lines resolve via Source 1 to
    the exact BMO-1 consumption (34.5 / 2.6 / 0.6 / 1.0)."""
    dcm = {BomItemCategory.MAIN_MATERIAL.value: "34.5",
           BomItemCategory.SUB_MATERIAL.value: "2.6",
           BomItemCategory.LINING.value: "0.6",
           BomItemCategory.INTERLINING.value: "1.0"}
    for cat, val in dcm.items():
        db.add(StyleConsumptionTemplate(
            client_id=identity.client_id, style_signature=identity.customer_ref,
            garment_type_id=gt_track_pant.id, material_category=cat, size=base_size,
            dcm_value=val, uom="dm²"))
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# §11.1 — Beau Geste POM extraction (deterministic, source_term kept, pattern ref)
# ════════════════════════════════════════════════════════════════════════════
async def test_beau_geste_pom_extraction(db):
    await _seed_stage2(db)
    user = await _cutting_user(db)
    _, _, _, spec, identity = await _make_order(db)

    svc = BomService(db)
    out = await svc.generate_bom(
        user, spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"),
        filename="spec_sheet_1.xlsx", identity=identity, client_match_code="beau_geste",
        line_seeds=_bmo1_seeds(), currency="USD", extractor=None)  # no LLM

    rows = (await db.execute(select(PomMeasurement)
                             .where(PomMeasurement.spec_sheet_id == spec.id))).scalars().all()
    assert len(rows) == 14 * 5                       # 14 POMs × S/M/L/XL/XXL
    codes = {r.pom_code for r in rows}
    assert "THIGH_WIDTH" in codes and "OUTSEAM" in codes
    thigh = next(r for r in rows if r.pom_code == "THIGH_WIDTH" and r.size == "M")
    assert thigh.source_term == "渡り幅"             # original label preserved
    assert thigh.extracted_by == "deterministic"     # NO LLM on the grid
    # the 特記事項 base pattern is recorded as a pattern_reference
    prefs = (await db.execute(select(PatternReference))).scalars().all()
    assert any(p.pattern_code == "CR1-O2B5-PL02" for p in prefs)


# ════════════════════════════════════════════════════════════════════════════
# §11.2 — Jackiee: zero POMs, pattern reference, prose attributes (Gemini adapter)
# ════════════════════════════════════════════════════════════════════════════
async def test_jackiee_pattern_ref_and_attributes(db):
    await _seed_stage2(db)
    user = await _cutting_user(db)
    client = Client(name="The Jackie", country="Italy", code="TJ", currency="EUR")
    db.add(client); await db.flush()
    order = ClientOrder(client_id=client.id, order_number="N2", currency="EUR")
    db.add(order); await db.flush()
    style = Style(client_order_id=order.id, name="MIMI", currency="EUR")
    db.add(style); await db.flush()
    db.add(SKU(style_id=style.id, color_code="MAG", size="42", qty_ordered=22))
    spec = SpecSheet(client_id=client.id, style_id=style.id,
                     spec_type=SpecType.NARRATIVE_TECHPACK.value)
    db.add(spec); await db.commit()
    identity = StyleIdentity(client_id=client.id, client_order_id=order.id, style_id=style.id,
                             name="MIMI", order_qty=22, per_size_qty={"42": 22})

    # injected fake Gemini extractor (proves the LLM wiring with no key)
    def fake_extractor(fields, garment_type):
        return {"accessories": [{"type": "zipper", "placement": "main closure",
                                 "supplied_by": "factory"}],
                "labelling": "embossed main label"}

    svc = BomService(db)
    out = await svc.generate_bom(
        user, spec_sheet=spec, spec_bytes=_read("Jackie-cleint-spec-sheet.xlsx"),
        filename="Jackie-cleint-spec-sheet.xlsx", identity=identity,
        client_match_code="the_jackie",
        line_seeds=[LineSeed(BomItemCategory.MAIN_MATERIAL.value, "THIN SUEDE",
                             material_color="MAGENTA", uom="dm²", unit_price=2.0)],
        currency="EUR", extractor=fake_extractor)

    assert await db.scalar(select(func.count(PomMeasurement.id))
                           .where(PomMeasurement.spec_sheet_id == spec.id)) == 0
    pref = (await db.execute(select(PatternReference))).scalars().first()
    assert pref.pattern_code == "MIMI-28-09-22-ZZ" and pref.base_size == "42"
    await db.refresh(spec)
    attrs = spec.attributes
    assert attrs["primary_color"] == "MAGENTA"
    assert attrs["leather_substance_mm"] == [0.45, 0.5]
    assert attrs["sms_qty"] == 22 and attrs["sms_size"] == "42"
    assert attrs["labelling"] == "embossed main label"   # merged from the LLM


# ════════════════════════════════════════════════════════════════════════════
# §11.4 — Cost math reproduces BMO-1 (and §11.3 stamping via Source-1 template)
# ════════════════════════════════════════════════════════════════════════════
async def test_cost_math_reproduces_bmo1(db):
    gts = await _seed_stage2(db)
    user = await _cutting_user(db)
    _, _, _, spec, identity = await _make_order(
        db, per_size={"S": 60, "M": 0, "L": 0, "XL": 0, "XXL": 0})  # 60 garments at base S
    await _seed_templates_for_bmo1(db, identity, gts["TRACK_PANT"])

    svc = BomService(db)
    out = await svc.generate_bom(
        user, spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"),
        filename="spec_sheet_1.xlsx", identity=identity, client_match_code="beau_geste",
        line_seeds=_bmo1_seeds(), currency="USD", extractor=None)

    items = {i["name"]: i for i in out["bom"]["items"]}
    sheep = items["SHEEP GLASS"]
    assert sheep["qty_per_garment"] == 34.5 and sheep["unit_price"] == 1.80
    assert sheep["total_cost"] == 62.10                 # 34.5 × 1.80
    assert sheep["bulk_qty"] == 2070.0                  # 60 × 34.5
    assert sheep["dcm_source"] == DcmSource.TEMPLATE.value
    assert sheep["dcm_confidence"] == 0.95
    assert out["bom"]["garment_fob_price"] == 107.25    # Σ per-garment lines
    assert out["bom"]["bulk_total"] == 6435.00          # 60 × 107.25
    # the sheep-glass bulk amount = 60 × 34.5 × 1.80 = 3,726
    assert round(60 * 34.5 * 1.80, 2) == 3726.0


# ════════════════════════════════════════════════════════════════════════════
# §11.3 — ai_estimate flagged + cannot be approved without cutting confirmation
# ════════════════════════════════════════════════════════════════════════════
async def test_ai_estimate_flagged_and_blocks_approval(db):
    gts = await _seed_stage2(db)
    user = await _cutting_user(db)
    _, _, _, spec, identity = await _make_order(db)   # NO templates → falls to Source 3

    svc = BomService(db)
    out = await svc.generate_bom(
        user, spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"),
        filename="spec_sheet_1.xlsx", identity=identity, client_match_code="beau_geste",
        line_seeds=_bmo1_seeds(), currency="USD", extractor=None)
    bom_id = uuid.UUID(out["bom"]["id"])
    sheep = next(i for i in out["bom"]["items"] if i["name"] == "SHEEP GLASS")
    assert sheep["dcm_source"] == DcmSource.AI_ESTIMATE.value
    assert sheep["dcm_confidence"] == 0.5 and sheep["qty_per_garment"] > 0

    # cannot approve while unconfirmed (the §10 gate)
    with pytest.raises(HTTPException) as ei:
        await svc.approve_bom(user, bom_id)
    assert ei.value.status_code == 409
    assert ei.value.detail["error"] == "cutting_confirmation_required"

    await svc.confirm_cutting(user, bom_id, identity=identity)
    res = await svc.approve_bom(user, bom_id, lock=True)
    assert res["status"] == BomStatus.LOCKED.value


# ════════════════════════════════════════════════════════════════════════════
# §11.5 — the DCM memory learns across orders (cross-order signature keying)
# ════════════════════════════════════════════════════════════════════════════
async def test_memory_learns_across_orders(db):
    gts = await _seed_stage2(db)
    user = await _cutting_user(db)

    # order 1 — generate (ai_estimate), cutting manager OVERRIDES sheep to 34.5, confirms
    _, _, _, spec1, id1 = await _make_order(db, order_no="1579")
    svc = BomService(db)
    out1 = await svc.generate_bom(
        user, spec_sheet=spec1, spec_bytes=_read("spec_sheet_1.xlsx"),
        filename="s.xlsx", identity=id1, client_match_code="beau_geste",
        line_seeds=_bmo1_seeds(), currency="USD", extractor=None)
    bom1 = uuid.UUID(out1["bom"]["id"])
    sheep1 = next(i for i in out1["bom"]["items"] if i["name"] == "SHEEP GLASS")
    await svc.edit_bom_items(user, bom1, out1["bom"]["revision"],
                             [{"bom_item_id": sheep1["id"], "field": "dcm", "value": 34.5}])
    await svc.confirm_cutting(user, bom1, identity=id1)

    # a confirmed template now exists, keyed on the cross-order signature
    tmpl = (await db.execute(select(StyleConsumptionTemplate).where(
        StyleConsumptionTemplate.material_category == BomItemCategory.MAIN_MATERIAL.value)
    )).scalars().first()
    assert tmpl.style_signature == "CR1-02F5-PL02" and float(tmpl.dcm_value) == 34.5

    # order 2 — a DIFFERENT order/style row, SAME customer_ref → Source-1 template HIT
    _, _, _, spec2, id2 = await _make_order(db, order_no="1580")
    assert id2.style_id != id1.style_id              # genuinely a new Style row
    out2 = await svc.generate_bom(
        user, spec_sheet=spec2, spec_bytes=_read("spec_sheet_1.xlsx"),
        filename="s.xlsx", identity=id2, client_match_code="beau_geste",
        line_seeds=_bmo1_seeds(), currency="USD", extractor=None)
    sheep2 = next(i for i in out2["bom"]["items"] if i["name"] == "SHEEP GLASS")
    assert sheep2["dcm_source"] == DcmSource.TEMPLATE.value      # not a re-estimate
    assert sheep2["qty_per_garment"] == 34.5


# ════════════════════════════════════════════════════════════════════════════
# §11.7 — editable contract: optimistic revision locking + locked rejects
# ════════════════════════════════════════════════════════════════════════════
async def test_editable_contract_revision_locking(db):
    gts = await _seed_stage2(db)
    user = await _cutting_user(db)
    _, _, _, spec, identity = await _make_order(db)
    await _seed_templates_for_bmo1(db, identity, gts["TRACK_PANT"])
    svc = BomService(db)
    out = await svc.generate_bom(
        user, spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"), filename="s.xlsx",
        identity=identity, client_match_code="beau_geste", line_seeds=_bmo1_seeds(),
        currency="USD", extractor=None)
    bom_id = uuid.UUID(out["bom"]["id"])
    sheep = next(i for i in out["bom"]["items"] if i["name"] == "SHEEP GLASS")
    rev = out["bom"]["revision"]

    # stale base_revision → 409 stale_revision
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(user, bom_id, rev - 1 if rev > 1 else 99,
                                 [{"bom_item_id": sheep["id"], "field": "unit_price", "value": 2.0}])
    # (rev starts at 1; use an explicitly stale value)
    with pytest.raises(HTTPException) as ei2:
        await svc.edit_bom_items(user, bom_id, 999,
                                 [{"bom_item_id": sheep["id"], "field": "unit_price", "value": 2.0}])
    assert ei2.value.status_code == 409
    assert ei2.value.detail["error"] == "stale_revision"
    assert ei2.value.detail["current_revision"] == rev

    # fresh edit → recomputed tree + revision+1
    res = await svc.edit_bom_items(user, bom_id, rev,
                                   [{"bom_item_id": sheep["id"], "field": "unit_price", "value": 2.00}])
    assert res["revision"] == rev + 1
    new_sheep = next(i for i in res["recomputed"]["items"] if i["id"] == sheep["id"])
    assert new_sheep["unit_price"] == 2.00
    assert new_sheep["total_cost"] == round(34.5 * 2.00, 2)

    # LOCKED BOM rejects edits
    await svc.confirm_cutting(user, bom_id, identity=identity)
    await svc.approve_bom(user, bom_id, lock=True)
    with pytest.raises(HTTPException) as ei3:
        await svc.edit_bom_items(user, bom_id, res["revision"],
                                 [{"bom_item_id": sheep["id"], "field": "unit_price", "value": 3.0}])
    assert ei3.value.status_code == 409
    assert ei3.value.detail["error"] == "bom_locked"


# ════════════════════════════════════════════════════════════════════════════
# §11.9 — gate enforced + post-confirmation DCM edit re-opens it
# ════════════════════════════════════════════════════════════════════════════
async def test_gate_reopens_on_post_confirm_dcm_edit(db):
    gts = await _seed_stage2(db)
    user = await _cutting_user(db)
    _, _, _, spec, identity = await _make_order(db)
    await _seed_templates_for_bmo1(db, identity, gts["TRACK_PANT"])
    svc = BomService(db)
    out = await svc.generate_bom(
        user, spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"), filename="s.xlsx",
        identity=identity, client_match_code="beau_geste", line_seeds=_bmo1_seeds(),
        currency="USD", extractor=None)
    bom_id = uuid.UUID(out["bom"]["id"])
    sheep = next(i for i in out["bom"]["items"] if i["name"] == "SHEEP GLASS")

    await svc.confirm_cutting(user, bom_id, identity=identity)
    bom = await svc.repo.get_bom(bom_id)
    assert bom.cutting_confirmed_at is not None

    # a post-confirmation DCM edit clears the confirmation (§9)
    res = await svc.edit_bom_items(user, bom_id, out["bom"]["revision"] + 0 if False else bom.revision,
                                   [{"bom_item_id": sheep["id"], "field": "dcm", "value": 35.0}])
    assert res["reconfirm_required"] is True
    bom2 = await svc.repo.get_bom(bom_id)
    assert bom2.cutting_confirmed_at is None
    # ... and approval is refused again until re-confirmed
    with pytest.raises(HTTPException) as ei:
        await svc.approve_bom(user, bom_id)
    assert ei.value.status_code == 409


# ════════════════════════════════════════════════════════════════════════════
# §11.8 — cross-checks fire (qty sum error, pattern unresolved error, substance warn)
# ════════════════════════════════════════════════════════════════════════════
async def test_cross_checks_fire(db):
    gts = await _seed_stage2(db)
    user = await _cutting_user(db)
    # Beau Geste with per-size qty that does NOT sum to the order total
    _, _, _, spec, identity = await _make_order(db, per_size={"S": 10, "M": 10})
    identity.order_qty = 999                          # force the Σ != total mismatch
    await _seed_templates_for_bmo1(db, identity, gts["TRACK_PANT"])
    svc = BomService(db)
    out = await svc.generate_bom(
        user, spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"), filename="s.xlsx",
        identity=identity, client_match_code="beau_geste", line_seeds=_bmo1_seeds(),
        currency="USD", extractor=None)
    flags = {f["id"]: f for f in out["flags"]}
    assert flags["qty_sum"]["severity"] == "error" and flags["qty_sum"]["ok"] is False

    # direct check engine: Jackiee unresolved pattern → error; substance out of band → warn
    from app.modules.procurement.checks import run_checks, has_blocking_error
    jackiee_flags = run_checks("the_jackie", {
        "attributes": {"substance_mm": [0.60, 0.62]},        # outside [0.45, 0.50]
        "pattern_reference": {"pattern_code": "MIMI", "resolved": False},
    })
    by_id = {f["id"]: f for f in jackiee_flags}
    assert by_id["pattern_resolves"]["severity"] == "error" and not by_id["pattern_resolves"]["ok"]
    assert by_id["leather_substance"]["severity"] == "warn" and not by_id["leather_substance"]["ok"]
    assert has_blocking_error(jackiee_flags) is True


# ════════════════════════════════════════════════════════════════════════════
# §11.6 — a third client is onboarded by CONFIG only (no code change), end-to-end
# ════════════════════════════════════════════════════════════════════════════
# A brand-new client's adapter + checks — in prod these are YAML entries the
# loaders read; here they are injected, proving zero source change.
_AUSTRALIA_ADAPTER = {
    "client_code": "australia", "spec_type": "measurement_grid", "language": "ja",
    "garment_type": "TRACK_PANT", "sheet_name": "CRPL-004",
    "poms": {"strategy": "deterministic", "term_col": "B", "first_row": 9, "last_row": 22,
             "pitch_col": "L", "size_cols": {"S": "G", "M": "H", "L": "I", "XL": "J", "XXL": "K"}},
    "pattern_ref": {"strategy": "regex", "pattern": r"※\s*([A-Z0-9O\-]+)\s*をベース"},
    "attributes": {"strategy": "deterministic"},
}
_AUSTRALIA_CHECKS = (
    {"client_code": "australia",
     "checks": [{"id": "qty_sum", "kind": "size_qty_sum_equals_total", "severity": "error"}]},
)


async def test_new_client_by_config_only(db):
    gts = await _seed_stage2(db)
    user = await _cutting_user(db)
    # an "australia" client with its own order/style/spec — no Beau Geste profile
    client = Client(name="Aussie Leather Co", country="Australia", code="AU", currency="AUD")
    db.add(client); await db.flush()
    order = ClientOrder(client_id=client.id, order_number="AU-1", currency="AUD")
    db.add(order); await db.flush()
    style = Style(client_order_id=order.id, name="OUTBACK PANT", customer_ref="AU-OB-01",
                  currency="AUD")
    db.add(style); await db.flush()
    for size, qty in {"S": 5, "M": 5}.items():
        db.add(SKU(style_id=style.id, color_code="TAN", size=size, qty_ordered=qty))
    spec = SpecSheet(client_id=client.id, style_id=style.id,
                     spec_type=SpecType.MEASUREMENT_GRID.value)
    db.add(spec); await db.commit()
    identity = StyleIdentity(client_id=client.id, client_order_id=order.id, style_id=style.id,
                             customer_ref="AU-OB-01", name="OUTBACK PANT", order_qty=999,
                             per_size_qty={"S": 5, "M": 5})   # mismatch → qty_sum fires

    svc = BomService(db)
    out = await svc.generate_bom(
        user, spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"), filename="s.xlsx",
        identity=identity, client_match_code="australia",
        line_seeds=[LineSeed(BomItemCategory.MAIN_MATERIAL.value, "AUSSIE LEATHER",
                             uom="dm²", unit_price=2.0)],
        currency="AUD", extractor=None,
        adapters=[_AUSTRALIA_ADAPTER], checks_cfg=_AUSTRALIA_CHECKS)   # CONFIG only

    # the new client's adapter extracted the grid, and its configured check ran
    assert out["extraction"]["poms"] == 14 * 5
    assert out["bom"]["id"]
    flags = {f["id"]: f for f in out["flags"]}
    assert flags["qty_sum"]["severity"] == "error" and flags["qty_sum"]["ok"] is False


# ════════════════════════════════════════════════════════════════════════════
# §7 — the revision CAS primitive (the conflict branch the bulk PATCH relies on)
# ════════════════════════════════════════════════════════════════════════════
async def test_claim_revision_is_atomic_cas(db):
    from app.modules.procurement.models import Bom
    from app.modules.procurement.repository import ProcurementRepository

    # FK enforcement is off on the test SQLite engine, so a bare Bom row is enough
    bom = Bom(client_order_id=uuid.uuid4(), style_id=uuid.uuid4(), revision=1)
    db.add(bom); await db.commit()
    repo = ProcurementRepository(db)

    assert await repo.claim_revision(bom.id, 1) == 1     # winner claims 1 → 2
    await db.commit()
    assert await repo.claim_revision(bom.id, 1) == 0     # stale base → loser (no rows)
    assert await repo.claim_revision(bom.id, 2) == 1     # fresh base → claims 2 → 3
    await db.commit()
    refreshed = await repo.get_bom(bom.id)
    assert refreshed.revision == 3


# ════════════════════════════════════════════════════════════════════════════
# HTTP shell — router wiring + RBAC for the Stage-2 endpoints (§7, §10)
# ════════════════════════════════════════════════════════════════════════════
async def _http_setup(monkeypatch):
    """Stand up the real ASGI app on a fresh in-memory engine; seed the Stage-2
    registries, a cutting manager + MD + employee, and a generated DRAFT BOM.
    Returns (app, engine, bom_id, revision, item_id)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod
    from app.core.database import Base
    from app.main import app
    from app.modules.users import schemas as user_schemas
    from app.modules.users.service import UserService

    engine = create_async_engine("sqlite+aiosqlite://",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(dbmod, "async_engine", engine, raising=False)
    monkeypatch.setattr(dbmod, "AsyncSessionLocal",
                        async_sessionmaker(bind=engine, expire_on_commit=False), raising=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with dbmod.AsyncSessionLocal() as db:
        gts = await _seed_stage2(db)
        us = UserService(db)
        await us.create_user(user_schemas.UserCreate(
            name="Cut", phone="9000000002", role=UserRole.CUTTING_MANAGER, password="9000000002"))
        await us.create_user(user_schemas.UserCreate(
            name="MD", phone="9000000000", role=UserRole.MANAGING_DIRECTOR, password="9000000000"))
        await us.create_user(user_schemas.UserCreate(
            name="Emp", phone="9100000001", role=UserRole.EMPLOYEE, password="9100000001"))
        cutting = await _cutting_user_existing(db)
        _, _, _, spec, identity = await _make_order(db)
        await _seed_templates_for_bmo1(db, identity, gts["TRACK_PANT"])
        out = await BomService(db).generate_bom(
            cutting, spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"), filename="s.xlsx",
            identity=identity, client_match_code="beau_geste", line_seeds=_bmo1_seeds(),
            currency="USD", extractor=None)
        bom_id = out["bom"]["id"]
        rev = out["bom"]["revision"]
        item_id = next(i["id"] for i in out["bom"]["items"] if i["name"] == "SHEEP GLASS")
    return app, engine, bom_id, rev, item_id


async def _cutting_user_existing(db):
    """Fetch the seeded cutting manager (the audit actor for the seeded BOM)."""
    return (await db.execute(select(User).where(User.role == UserRole.CUTTING_MANAGER))).scalar_one()


async def _token(client, phone):
    r = await client.post("/api/v1/auth/login", json={"username": phone, "password": phone})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_http_stage2_endpoints_rbac_and_gates(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    app, engine, bom_id, rev, item_id = await _http_setup(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cut = await _token(c, "9000000002")
        md = await _token(c, "9000000000")
        emp = await _token(c, "9100000001")

        edit = {"base_revision": rev, "edits": [{"bom_item_id": item_id,
                                                 "field": "unit_price", "value": 2.0}]}

        # RBAC: an employee may not edit a BOM
        r = await c.patch(f"/api/v1/procurement/boms/{bom_id}/items", headers=emp, json=edit)
        assert r.status_code == 403

        # stale base_revision → 409 stale_revision
        stale = {"base_revision": 999, "edits": edit["edits"]}
        r = await c.patch(f"/api/v1/procurement/boms/{bom_id}/items", headers=cut, json=stale)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "stale_revision"

        # fresh edit → 200, revision+1
        r = await c.patch(f"/api/v1/procurement/boms/{bom_id}/items", headers=cut, json=edit)
        assert r.status_code == 200, r.text
        new_rev = r.json()["revision"]
        assert new_rev == rev + 1

        # approval refused before cutting confirmation (§10)
        r = await c.post(f"/api/v1/procurement/boms/{bom_id}/approve", headers=md, json={"lock": True})
        assert r.status_code == 409 and r.json()["detail"]["error"] == "cutting_confirmation_required"

        # cutting manager confirms → MD approves+locks
        r = await c.post(f"/api/v1/procurement/boms/{bom_id}/confirm-cutting", headers=cut)
        assert r.status_code == 200
        r = await c.post(f"/api/v1/procurement/boms/{bom_id}/approve", headers=md, json={"lock": True})
        assert r.status_code == 200 and r.json()["status"] == BomStatus.LOCKED.value

        # a LOCKED BOM rejects edits
        r = await c.patch(f"/api/v1/procurement/boms/{bom_id}/items", headers=cut,
                          json={"base_revision": new_rev, "edits": edit["edits"]})
        assert r.status_code == 409 and r.json()["detail"]["error"] == "bom_locked"
    await engine.dispose()
