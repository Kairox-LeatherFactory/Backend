"""
FUNCTIONAL · one garment with a recipe, cut to export, through the real services.

test_garment_to_export.py walks a jacket down the nine-stage chain. This walks
the SAME chain for a garment whose style declares accessories, which changes the
shape of the store step in three ways nothing else exercises end to end:

    · the drawer does NOT auto-receive on leather + lining any more — the kit is
      still owed, and receiving there is what used to make the kit unissuable
    · a third store scan (part=ACCESSORY) spends button and zip stock
    · the drawer cannot be SENT until that kit is in it

and then, at PACKAGE_EXPORT, hands the drawer back to the pool with all THREE
buckets empty, so the next garment merged into it starts unkitted.

The accessory arithmetic is asserted across the whole run, not per scan: the
factory's question is "did 4 buttons leave stock for this jacket", and the answer
has to survive nine stages, a send and a recycle.
"""
import datetime

import pytest
from sqlalchemy import select

from app.core.enums import (StorePart, StoreState, KitStatus, ProductionStage,
                            ScreenContext)
from app.modules.barcode.models import (MaterialLot, PieceMaterialIssue,
                                        StyleMaterialSpec)
from app.modules.store.service import StoreService
from app.modules.production.service import ProductionService

TODAY = datetime.date.today()


async def _on_hand(db, lot_id) -> float:
    return float(await db.scalar(
        select(MaterialLot.on_hand).where(MaterialLot.id == lot_id)))


@pytest.fixture
async def lining_lot(db):
    lot = MaterialLot(category="LINING", subtype="PLAIN_LINING", article="PL-22",
                      colour="BLACK", thickness="0.4mm", uom="mtrs",
                      on_hand=400, is_active=True)
    db.add(lot)
    await db.commit()
    await db.refresh(lot)
    return lot


@pytest.fixture
async def kitted_style(db, order_tree):
    """CLERMONT takes 4 buttons and 1 zip, both pinned to real lots."""
    button = MaterialLot(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                         colour="BLACK", size="18L", uom="pcs", on_hand=200,
                         is_active=True)
    zipper = MaterialLot(category="ACCESSORY", subtype="ZIP", article="ZIP-YKK",
                         colour="BLACK", size="60cm", uom="pcs", on_hand=80,
                         is_active=True)
    db.add_all([button, zipper])
    await db.flush()
    style = order_tree["style"]
    db.add_all([
        StyleMaterialSpec(style_id=style.id, category="ACCESSORY",
                          subtype="BUTTON", article="BTN-4H", colour="BLACK",
                          size="18L", qty_per_piece=4, uom="pcs",
                          material_lot_id=button.id),
        StyleMaterialSpec(style_id=style.id, category="ACCESSORY", subtype="ZIP",
                          article="ZIP-YKK", colour="BLACK", size="60cm",
                          qty_per_piece=1, uom="pcs", material_lot_id=zipper.id),
    ])
    await db.commit()
    for lot in (button, zipper):
        await db.refresh(lot)
    return {"button": button, "zip": zipper}


@pytest.mark.asyncio
async def test_a_jacket_with_a_recipe_walks_the_chain_and_spends_its_kit_once(
        db, operations, pieces, leather_lot, lining_lot, kitted_style,
        cutter, lining_cutter, paster, tailor,
        cutting_mgr, lining_mgr, stitching_mgr, dm):
    piece = pieces[0]
    button, zipper = kitted_style["button"], kitted_style["zip"]
    btn_before, zip_before = await _on_hand(db, button.id), await _on_hand(db, zipper.id)
    svc, drawers = ProductionService(db), StoreService(db)

    # ── 1 · the two cut paths, each charging its own lot ─────────────────────
    r = await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                            piece_ids=[piece.id], work_date=TODAY,
                            screen=ScreenContext.LEATHER_CUT,
                            leather_lot_id=leather_lot.id, consumption_qty=14.0)
    assert r["stage"] == "LEATHER_CUTTING"
    # The kit rides the LOG response too, so the cutting screen can already show
    # what this garment is going to need downstream.
    assert r["kit_by_piece"][piece.code]["kit_status"] == KitStatus.PENDING.value
    assert r["kit_by_piece"][piece.code]["outstanding"] == pytest.approx(5)

    await svc.log_batch(user=lining_mgr, employee_id=lining_cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LINING_CUT,
                        lining_lot_id=lining_lot.id, consumption_qty=3.0)

    # ── 2 · the leather side finishes before it may be stored ───────────────
    await svc.log_batch(user=stitching_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.PIPELINE)                # FUSING
    await svc.log_batch(user=stitching_mgr, employee_id=paster[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.PIPELINE)                # PASTING

    # ── 3 · storage — and it does NOT receive itself this time ──────────────
    store = StoreService(db)
    await store.store_scan(piece_id=piece.id, employee_id=cutter[0].id,
                             part=StorePart.LEATHER)
    s = await store.store_scan(piece_id=piece.id, employee_id=cutter[0].id,
                                 part=StorePart.LINING)
    assert s["holding"] == "HOLDING BOTH"
    assert s["auto_received"] is False, (
        "the kit is still owed — receiving here is what made it unissuable")
    assert s["store_state"] == StoreState.HOLDING_BOTH.value
    assert "ACCESSORIES" in s["awaiting"]
    # The operator holding the lining is told what else to fetch, on this scan.
    assert "BTN-4H" in s["kit"]["summary_line"]

    # ── 4 · an unkitted drawer cannot leave the store ────────────────────────
    blocked = await store.send(piece_ids=[piece.id], actor_user_id=dm.id)
    assert blocked["sent"] == []
    assert "accessory kit" in blocked["not_ready"][0]["reason"]

    # ── 5 · the kit scan: the money moves here ───────────────────────────────
    k = await store.store_scan(piece_id=piece.id,
                                part=StorePart.ACCESSORY,
                                employee_id=cutter[0].id, entered_by="STORE")
    assert k["kit"]["status"] == KitStatus.ISSUED.value
    assert k["auto_received"] is True                  # now it completes
    assert k["store_state"] == StoreState.RECEIVED.value
    assert await _on_hand(db, button.id) == pytest.approx(btn_before - 4)
    assert await _on_hand(db, zipper.id) == pytest.approx(zip_before - 1)

    # ── 6 · the send now succeeds and opens the merge gate ──────────────────
    out = await store.send(piece_ids=[piece.id], actor_user_id=dm.id)
    assert out["count_sent"] == 1

    # ── 7 · the rest of the chain, to the shipping box ──────────────────────
    for stage, user, actor in (
            (ProductionStage.LINE_STITCHING, stitching_mgr, tailor[0]),
            (ProductionStage.SHELL_STITCHING, stitching_mgr, tailor[0]),
            (ProductionStage.FINAL_FINISH, stitching_mgr, tailor[0]),
            (ProductionStage.FINAL_INSPECTION, dm, tailor[0]),
            (ProductionStage.PACKAGE_EXPORT, dm, tailor[0]),
    ):
        r = await svc.log_batch(user=user, employee_id=actor.id,
                                piece_ids=[piece.id], work_date=TODAY,
                                screen=ScreenContext.PIPELINE)
        assert r["stage"] == stage.value, r["message"]
        assert r["count_logged"] == 1, r["message"]

    # ── 8 · the garment leaves the store with ALL THREE buckets empty ───────
    await db.refresh(piece)
    assert piece.store_state == StoreState.WAITING.value
    assert (piece.leather_in, piece.lining_in, piece.accessories_in) == (
        False, False, False)

    # ── 9 · and the ledger still says what this garment cost, across the run ─
    rows = (await db.execute(
        select(PieceMaterialIssue).where(
            PieceMaterialIssue.piece_id == piece.id))).scalars().all()
    assert {r.article: float(r.qty) for r in rows} == {"BTN-4H": 4.0,
                                                       "ZIP-YKK": 1.0}
    # Exactly once, over nine stages, a send and a recycle.
    assert await _on_hand(db, button.id) == pytest.approx(btn_before - 4)
    assert await _on_hand(db, zipper.id) == pytest.approx(zip_before - 1)
