"""
================================================================================
tests/integration/test_material_reservation_release.py — F-03 / F-04
================================================================================
A RESERVATION IS A CLAIM ON STOCK THAT HAS NOT BEEN SPENT YET.

The moment it IS spent, the claim has to go. If it does not, the same quantity is
subtracted twice from `available = on_hand - reserved`: once because on_hand
fell, and again because the reservation is still sitting there active.

That is exactly what happened. `add_reservation_nocommit` was wired up at
receiving, but `consume_reservations_nocommit` had NO CALLER ANYWHERE in the
codebase — so every reservation ever created stayed "active" for the life of the
row. `available` fell monotonically and never recovered, and a lot that was
physically full eventually read as unavailable, with nothing on screen to explain
why. These tests hold that door shut.
================================================================================
"""
from decimal import Decimal

import pytest

from app.modules.materials.repository import MaterialRepository
from app.modules.materials.schemas import LotCreate
from app.modules.materials.service import MaterialService, display_stock


async def _lot(db, qty=100):
    """A leather lot with `qty` dcm on hand."""
    out = await MaterialService(db).create_lot(LotCreate(
        category="LEATHER", article="RESV-A", colour="BLACK",
        attributes={"thickness": "1.2mm", "dcm": qty}))
    await db.commit()
    return out["lot_id"]


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_cutting_against_a_reservation_releases_it(db):
    """Reserve 40, cut 40 — the reservation must be gone, not still claiming 40."""
    lot_id = await _lot(db, 100)
    repo, svc = MaterialRepository(db), MaterialService(db)

    repo.add_reservation_nocommit(lot_id, Decimal("40"), reason="test")
    await db.commit()
    assert await repo.active_reserved(lot_id) == Decimal("40")

    await svc.decrement_for_cut_nocommit(lot_id, 40)
    await db.commit()

    # THE POINT: the claim was satisfied, so it is no longer outstanding.
    assert await repo.active_reserved(lot_id) == Decimal("0"), (
        "the reservation survived the cut that spent it — `available` will now "
        "under-report this lot by 40 dcm forever")

    lot = await repo.get_lot(lot_id)
    assert lot.on_hand == Decimal("60")


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_available_is_not_double_counted_after_a_cut(db):
    """available must equal what is physically left, not left-minus-a-ghost."""
    lot_id = await _lot(db, 100)
    repo, svc = MaterialRepository(db), MaterialService(db)

    repo.add_reservation_nocommit(lot_id, Decimal("30"), reason="test")
    await db.commit()

    await svc.decrement_for_cut_nocommit(lot_id, 30)
    await db.commit()

    lot = await repo.get_lot(lot_id)
    _received, _reserved, available = display_stock(
        lot.on_hand, lot.used, await repo.active_reserved(lot_id))

    # 100 on hand, 30 reserved, 30 cut. 70 dcm is on the shelf and none of it is
    # spoken for. Before the fix this read 40 — the spent reservation was still
    # being subtracted alongside the stock it had already paid for.
    assert available == Decimal("70")


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_a_partial_cut_leaves_the_rest_of_the_reservation_standing(db):
    """Reserve 50, cut 20 — 30 is still legitimately claimed."""
    lot_id = await _lot(db, 100)
    repo, svc = MaterialRepository(db), MaterialService(db)

    repo.add_reservation_nocommit(lot_id, Decimal("50"), reason="test")
    await db.commit()

    await svc.decrement_for_cut_nocommit(lot_id, 20)
    await db.commit()

    assert await repo.active_reserved(lot_id) == Decimal("30")


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_reservations_are_consumed_oldest_first(db):
    """FIFO: the oldest claim is the one the consumption pays off."""
    lot_id = await _lot(db, 100)
    repo, svc = MaterialRepository(db), MaterialService(db)

    repo.add_reservation_nocommit(lot_id, Decimal("10"), reason="first")
    await db.commit()
    repo.add_reservation_nocommit(lot_id, Decimal("25"), reason="second")
    await db.commit()

    await svc.decrement_for_cut_nocommit(lot_id, 10)
    await db.commit()

    # the 10 is fully paid off and closed; the 25 is untouched
    assert await repo.active_reserved(lot_id) == Decimal("25")


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_cutting_more_than_was_reserved_does_not_go_negative(db):
    """Consumption is capped at what was actually claimed."""
    lot_id = await _lot(db, 100)
    repo, svc = MaterialRepository(db), MaterialService(db)

    repo.add_reservation_nocommit(lot_id, Decimal("10"), reason="test")
    await db.commit()

    await svc.decrement_for_cut_nocommit(lot_id, 60)
    await db.commit()

    # the claim is cleared, never driven below zero
    assert await repo.active_reserved(lot_id) == Decimal("0")


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_the_locking_read_returns_the_same_lot(db):
    """F-04: get_lot_for_update is get_lot plus a row lock, nothing else.

    SQLite has no row locks and ignores FOR UPDATE, so this pins the contract
    (same row, same numbers) rather than the concurrency. The lock is what
    protects Postgres, where two cutting managers really can scan the same lot at
    the same instant and the loser's decrement would otherwise vanish silently.
    """
    lot_id = await _lot(db, 100)
    repo = MaterialRepository(db)

    plain = await repo.get_lot(lot_id)
    locked = await repo.get_lot_for_update(lot_id)

    assert locked is not None
    assert locked.id == plain.id
    assert locked.on_hand == plain.on_hand
    assert await repo.get_lot_for_update(uuid_missing()) is None


def uuid_missing():
    import uuid
    return uuid.uuid4()
