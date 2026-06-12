"""
================================================================================
tests/test_procurement_stage4.py — Stage-4 inventory check acceptance (stage-4 §9)
================================================================================

Confirms every Stage-4 acceptance criterion at the service layer:

  1. Importer normalizes + dedups the real INVENTORY (1).xlsx; commit is idempotent.
  2. Re-sync conflict rule: sheet wins on qty_on_hand; reservations (separate ledger)
     survive; absent rows soft-deactivate, not delete.
  3. Per-line math: required/on_hand/available → sufficient / partial / out_of_stock.
  4. Reservation prevents double-spend; release restores availability.
  5. Matching: alias hit; unmatched → out_of_stock + `unmatched` (never a silent match).
  6. Edge cases: unmatched full shortfall; UOM mismatch → conservative out_of_stock;
     multi-lot stock summed.
  7. Excluded categories (manufacturing/fob_charge) produce no line.
  8. Report shape + grouped dashboard + auto-fire on approve.
================================================================================
"""
import glob
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.enums import UserRole
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.bom.service import BomService
from app.modules.bom.enums import BomItemCategory, BomStatus
from app.modules.inventory.enums import (
    InventoryLineStatus,
    MatchMethod,
    ReservationStatus,
)
from app.modules.inventory.inventory_import import parse_inventory
from app.modules.inventory.inventory_normalize import normalize_key
from app.modules.inventory.service import InventoryService
from app.modules.bom.models import Bom, BomItem
from app.modules.inventory.models import (
    InventoryItem,
    InventoryReservation,
    MaterialAlias,
    UomConversion,
)
from app.modules.users.models import User
from sqlalchemy import func, select

DATA = Path(__file__).resolve().parent.parent / "data"


def _inventory_file() -> bytes:
    hits = [p for p in glob.glob(str(DATA / "INVENTORY*.xlsx")) if "~$" not in p]
    return Path(hits[0]).read_bytes()


async def _md(db) -> User:
    u = User(id=uuid.uuid4(), name="Boss", phone="9000000000",
             role=UserRole.MANAGING_DIRECTOR, password_hash="x", is_active=True)
    db.add(u)
    await db.commit()
    return u


async def _add_item(db, desc, *, uom="DCM", qty="0", rate="1", color=None):
    it = InventoryItem(description=desc, normalized_key=normalize_key(desc), uom=uom,
                       qty_on_hand=Decimal(qty), rate=Decimal(rate), color=color, is_active=True)
    db.add(it)
    await db.commit()
    return it


async def _alias(db, bom_term, inventory_key):
    db.add(MaterialAlias(bom_term=bom_term, inventory_key=inventory_key, is_active=True))
    await db.commit()


async def _approved_bom(db, *, lines, order_qty=60, customer_ref="CR1-02F5-PL02",
                        order_no="1579"):
    """Construct an APPROVED bom + bom_item tree directly (bypassing the Stage-2
    generation pipeline) so the check is tested in isolation. `lines` = list of
    (category, name, color, uom, qty_per_garment)."""
    client = (await db.execute(select(Client).where(Client.code == "BG"))).scalar_one_or_none()
    if client is None:
        client = Client(name="Beau Geste", country="Japan", code="BG", currency="USD")
        db.add(client)
        await db.flush()
    order = ClientOrder(client_id=client.id, order_number=order_no, currency="USD")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name="SIDE SUEDE TRACK PANT",
                  customer_ref=customer_ref, currency="USD")
    db.add(style)
    await db.flush()
    db.add(SKU(style_id=style.id, color_code="BLK", size="M", qty_ordered=order_qty))
    now = datetime.now(timezone.utc)
    bom = Bom(client_order_id=order.id, style_id=style.id, status=BomStatus.LOCKED.value,
              currency="USD", order_qty=order_qty, revision=1, approved_at=now, locked_at=now)
    bom.items = [
        BomItem(category=cat, name=name, material_color=color, uom=uom,
                qty_per_garment=Decimal(str(qpg)), unit_price=Decimal("1"),
                bulk_qty=Decimal(str(order_qty)) * Decimal(str(qpg)))
        for (cat, name, color, uom, qpg) in lines
    ]
    db.add(bom)
    await db.commit()
    return client, order, style, bom


# ════════════════════════════════════════════════════════════════════════════
# §9.1 — importer normalizes + dedups; commit idempotent
# ════════════════════════════════════════════════════════════════════════════
async def test_importer_normalizes_dedups_and_is_idempotent(db):
    data = _inventory_file()
    prev = parse_inventory(data)
    assert prev.kept < prev.raw_count                 # dedup + noise drop happened
    assert any("MEMBERSHIP FEE" in (d["description"] or "") for d in prev.dropped)
    nappa = [r for r in prev.rows if r.normalized_key == "SHEEP NAPPA BLACK"]
    assert nappa and nappa[0].lots > 1                # multiple lots merged into one row

    svc = InventoryService(db)
    first = await svc.commit(data)
    n1 = await db.scalar(select(func.count(InventoryItem.id)))
    second = await svc.commit(data)                   # re-commit the same bytes
    n2 = await db.scalar(select(func.count(InventoryItem.id)))
    assert first["committed"] == second["committed"]
    assert n1 == n2                                   # no duplicate rows
    row = (await db.execute(select(InventoryItem).where(
        InventoryItem.normalized_key == "SHEEP NAPPA BLACK"))).scalar_one()
    assert float(row.qty_on_hand) == float(nappa[0].qty_on_hand)


# ════════════════════════════════════════════════════════════════════════════
# §9.2 — re-sync conflict rule (sheet wins on qty; reservations survive; soft-deactivate)
# ════════════════════════════════════════════════════════════════════════════
async def test_resync_sheet_wins_but_reservation_survives(db):
    repo = InventoryService(db).repo
    item = await _add_item(db, "SHEEP NAPPA BLACK", qty="100", uom="DCM")
    # a competing BOM holds 30
    db.add(InventoryReservation(inventory_item_id=item.id, bom_id=uuid.uuid4(),
                                qty=Decimal("30"), status=ReservationStatus.ACTIVE.value))
    await db.commit()

    # re-sync: the sheet says 50 now → snapshot replace
    await repo.upsert_inventory_item(normalized_key="SHEEP NAPPA BLACK",
                                     description="SHEEP NAPPA BLACK", uom="DCM",
                                     qty_on_hand=Decimal("50"), rate=None, color="BLACK")
    await repo.commit()
    refreshed = await repo.get_inventory_item(item.id)
    assert float(refreshed.qty_on_hand) == 50.0                     # sheet won
    sums = await repo.active_reservation_sums([item.id])
    assert float(sums[item.id]) == 30.0                             # reservation survived

    # a key absent from the new sheet soft-deactivates, not deletes
    other = await _add_item(db, "OBSOLETE TRIM", qty="5")
    await repo.deactivate_keys_not_in({"SHEEP NAPPA BLACK"})
    await repo.commit()
    gone = await repo.get_inventory_item(other.id)
    assert gone is not None and gone.is_active is False


# ════════════════════════════════════════════════════════════════════════════
# §9.3 / §9.5 — per-line math + alias matching + unmatched
# ════════════════════════════════════════════════════════════════════════════
async def test_check_math_alias_and_unmatched(db):
    md = await _md(db)
    await _alias(db, "SHEEP GLASS", "SHEEP NAPPA")
    await _add_item(db, "SHEEP NAPPA BLACK & PACKING CHARGES", uom="DCM",
                    qty="1529", rate="6.15", color="BLACK")
    _, _, _, bom = await _approved_bom(db, order_qty=60, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, "SHEEP GLASS", "BLACK", "dm²", 34.5),  # req 2070
        (BomItemCategory.ACCESSORY.value, "YKK N5 ZIPPER", None, "pc", 2),           # req 120, no stock
    ])
    out = await InventoryService(db).run_check(md, bom.id)

    by_name = {ln["name"]: ln for ln in out["lines"]}
    sheep = by_name["SHEEP GLASS"]
    assert sheep["matched"]["method"] == MatchMethod.ALIAS.value
    assert sheep["required_qty"] == 2070.0 and sheep["on_hand_qty"] == 1529.0
    assert sheep["status"] == InventoryLineStatus.PARTIAL.value
    assert sheep["shortfall_qty"] == 541.0

    zipper = by_name["YKK N5 ZIPPER"]
    assert zipper["matched"] is None
    assert "unmatched" in zipper["flags"]
    assert zipper["status"] == InventoryLineStatus.OUT_OF_STOCK.value
    assert zipper["shortfall_qty"] == 120.0
    assert out["summary"]["badge"] == InventoryLineStatus.OUT_OF_STOCK.value


async def test_sufficient_when_stock_covers(db):
    md = await _md(db)
    await _alias(db, "SHEEP GLASS", "SHEEP NAPPA")
    await _add_item(db, "SHEEP NAPPA BLACK", uom="DCM", qty="5000", rate="6", color="BLACK")
    _, _, _, bom = await _approved_bom(db, order_qty=60, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, "SHEEP GLASS", "BLACK", "dm²", 34.5),  # req 2070
    ])
    out = await InventoryService(db).run_check(md, bom.id)
    ln = out["lines"][0]
    assert ln["status"] == InventoryLineStatus.SUFFICIENT.value
    assert ln["shortfall_qty"] == 0.0
    assert ln["reserved_for_this_bom"] == 2070.0


# ════════════════════════════════════════════════════════════════════════════
# §9.4 — reservation prevents double-spend; release restores availability
# ════════════════════════════════════════════════════════════════════════════
async def test_reservation_prevents_double_spend(db):
    md = await _md(db)
    await _alias(db, "SHEEP GLASS", "SHEEP NAPPA")
    await _add_item(db, "SHEEP NAPPA BLACK", uom="DCM", qty="1500", color="BLACK")
    line = [(BomItemCategory.MAIN_MATERIAL.value, "SHEEP GLASS", "BLACK", "dm²", 1000.0 / 60)]
    # two BOMs each needing ~1000 dm² (qpg × 60 ≈ 1000)
    _, _, _, bom1 = await _approved_bom(db, order_qty=60, lines=line, order_no="A1",
                                        customer_ref="REF-A")
    _, _, _, bom2 = await _approved_bom(db, order_qty=60, lines=line, order_no="A2",
                                        customer_ref="REF-B")
    svc = InventoryService(db)
    out1 = await svc.run_check(md, bom1.id)
    assert out1["lines"][0]["status"] == InventoryLineStatus.SUFFICIENT.value

    out2 = await svc.run_check(md, bom2.id)          # only ~500 left
    ln2 = out2["lines"][0]
    assert ln2["status"] == InventoryLineStatus.PARTIAL.value
    assert ln2["available_qty"] == pytest.approx(500.0, abs=1.0)

    # release BOM1's claim → BOM2 re-run sees full stock again
    await svc.repo.release_reservations(bom1.id, reason="cancelled")
    await svc.repo.commit()
    out2b = await svc.run_check(md, bom2.id)
    assert out2b["lines"][0]["status"] == InventoryLineStatus.SUFFICIENT.value


# ════════════════════════════════════════════════════════════════════════════
# §9.6 — UOM mismatch is a conservative out_of_stock, never a false sufficient
# ════════════════════════════════════════════════════════════════════════════
async def test_uom_mismatch_is_conservative(db):
    md = await _md(db)
    await _alias(db, "SHEEP GLASS", "SHEEP NAPPA")
    # plenty of stock by count, but in KGS — cannot convert to the BOM's dm²
    await _add_item(db, "SHEEP NAPPA BLACK", uom="KGS", qty="9999", color="BLACK")
    _, _, _, bom = await _approved_bom(db, order_qty=60, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, "SHEEP GLASS", "BLACK", "dm²", 34.5),
    ])
    out = await InventoryService(db).run_check(md, bom.id)
    ln = out["lines"][0]
    assert "uom_mismatch" in ln["flags"]
    assert ln["status"] == InventoryLineStatus.OUT_OF_STOCK.value      # NOT sufficient
    assert ln["on_hand_qty"] == 0.0


# ════════════════════════════════════════════════════════════════════════════
# §9.6 — multi-lot stock summed
# ════════════════════════════════════════════════════════════════════════════
async def test_multi_lot_on_hand_summed(db):
    md = await _md(db)
    await _alias(db, "SHEEP GLASS", "SHEEP NAPPA")
    await _add_item(db, "SHEEP NAPPA BLACK", uom="DCM", qty="600", color="BLACK")
    await _add_item(db, "SHEEP NAPPA BLACK", uom="DCM", qty="900", color="BLACK")  # 2nd lot
    _, _, _, bom = await _approved_bom(db, order_qty=60, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, "SHEEP GLASS", "BLACK", "dm²", 20),  # req 1200
    ])
    out = await InventoryService(db).run_check(md, bom.id)
    ln = out["lines"][0]
    assert ln["on_hand_qty"] == 1500.0                # 600 + 900 across lots
    assert ln["status"] == InventoryLineStatus.SUFFICIENT.value


# ════════════════════════════════════════════════════════════════════════════
# §9.8 — excluded categories produce no check line; auto-fire on approve; dashboard
# ════════════════════════════════════════════════════════════════════════════
async def test_excluded_categories_and_auto_fire_and_dashboard(db):
    md = await _md(db)
    await _alias(db, "SHEEP GLASS", "SHEEP NAPPA")
    await _add_item(db, "SHEEP NAPPA BLACK", uom="DCM", qty="5000", color="BLACK")

    # build a DRAFT bom and drive it through confirm → approve (auto-fires the check)
    client = Client(name="Beau Geste", country="Japan", code="BG", currency="USD")
    db.add(client)
    await db.flush()
    order = ClientOrder(client_id=client.id, order_number="1579", currency="USD")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name="TRACK PANT", customer_ref="CR1", currency="USD")
    db.add(style)
    await db.flush()
    db.add(SKU(style_id=style.id, color_code="BLK", size="M", qty_ordered=60))
    bom = Bom(client_order_id=order.id, style_id=style.id, status=BomStatus.READY_FOR_REVIEW.value,
              currency="USD", order_qty=60, revision=1,
              cutting_confirmed_at=datetime.now(timezone.utc), cutting_confirmed_by=md.id)
    bom.items = [
        BomItem(category=BomItemCategory.MAIN_MATERIAL.value, name="SHEEP GLASS",
                material_color="BLACK", uom="dm²", qty_per_garment=Decimal("34.5"),
                unit_price=Decimal("1.8"), bulk_qty=Decimal("2070")),
        BomItem(category=BomItemCategory.MANUFACTURING.value, name="CUTTING & STITCHING",
                uom="pc", qty_per_garment=Decimal("1"), unit_price=Decimal("30"),
                bulk_qty=Decimal("60")),
        BomItem(category=BomItemCategory.FOB_CHARGE.value, name="FOB CHARGE",
                uom="pc", qty_per_garment=Decimal("1"), unit_price=Decimal("5"),
                bulk_qty=Decimal("60")),
    ]
    db.add(bom)
    await db.commit()

    approve = await BomService(db).approve_bom(md, bom.id, lock=True)
    assert approve["inventory_check_id"] is not None     # auto-fired on approve

    svc = InventoryService(db)
    out = await svc.get_check(uuid.UUID(approve["inventory_check_id"]))
    assert out["summary"]["lines_total"] == 1            # only the stockable material line
    excluded_names = {e["name"] for e in out["excluded"]}
    assert excluded_names == {"CUTTING & STITCHING", "FOB CHARGE"}

    dash = await svc.dashboard()
    assert dash["totals"]["boms_checked"] == 1
    cl = dash["clients"][0]
    assert cl["client_name"] == "Beau Geste"
    style_row = cl["orders"][0]["styles"][0]
    assert style_row["badge"] == InventoryLineStatus.SUFFICIENT.value
    assert style_row["bom_id"] == str(bom.id)
