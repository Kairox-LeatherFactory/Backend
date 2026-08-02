"""
================================================================================
tests/test_barcode_reads.py — barcode listing / history / analytics
================================================================================
Integration tests for the new read-side (async, real models, SQLite via the
existing `db` fixture). Mirrors the style of the audit-fix suites.

Assumes the project conftest provides an async `db` (AsyncSession) fixture, as
the existing tests.txt harness does. Seeds a small John-Peter-shaped order:
  CLERMONT (planned 5, fully minted, 1 label retired)
  VEST     (planned 4, only 2 minted -> balance 2)
so every assertion below is a real number, not a placeholder.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.enums import BarcodeStatus, BarcodeType
from app.modules.barcode.models import BarcodeRegistry
from app.modules.barcode.service import BarcodeService
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.production.models import Piece


@pytest_asyncio.fixture
async def jp_order(db):
    t0 = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    c = Client(name="John Peter"); db.add(c); await db.flush()
    o = ClientOrder(client_id=c.id, order_number="JP-READS"); db.add(o); await db.flush()

    stA = Style(client_order_id=o.id, name="CLERMONT", code="JPR-CLERMONT")
    stB = Style(client_order_id=o.id, name="VEST", code="JPR-VEST")
    db.add_all([stA, stB]); await db.flush()

    skA1 = SKU(style_id=stA.id, code="JPR-CLERMONT-PINE-M", color_name="PINE",
               size="M", qty_ordered=3)
    skA2 = SKU(style_id=stA.id, code="JPR-CLERMONT-PINE-L", color_name="PINE",
               size="L", qty_ordered=2)
    skB1 = SKU(style_id=stB.id, code="JPR-VEST-TABAC-M", color_name="TABAC",
               size="M", qty_ordered=4)
    db.add_all([skA1, skA2, skB1]); await db.flush()

    async def mint(sku, style, n, retire_last=False):
        for i in range(n):
            seq = i + 1
            code = f"{sku.code}-{seq:03d}"
            p = Piece(code=code, seq=seq, sku_id=sku.id, current_operation_id=None)
            db.add(p); await db.flush()
            status = (BarcodeStatus.RETIRED.value if (retire_last and i == n - 1)
                      else BarcodeStatus.ACTIVE.value)
            db.add(BarcodeRegistry(
                code=code, type=BarcodeType.PIECE.value, status=status,
                piece_id=p.id, caption=code,
                created_at=t0 + timedelta(minutes=seq),
                order_id=o.id, sku_id=sku.id, style_id=style.id))

    await mint(skA1, stA, 3)
    await mint(skA2, stA, 2, retire_last=True)
    await mint(skB1, stB, 2)     # 2 of 4
    await db.commit()
    return {"order_id": o.id, "styleA": stA.id, "styleB": stB.id,
            "skuA2": skA2.id}


@pytest.mark.asyncio
async def test_order_analytics_totals_and_balance(db, jp_order):
    svc = BarcodeService(db)
    a = await svc.order_analytics(jp_order["order_id"], client_scope=None)
    tot = a["order_total"]
    assert tot["planned"] == 9
    assert tot["generated"] == 7
    assert tot["balance"] == 2          # VEST short-minted by 2
    assert tot["active"] == 6 and tot["retired"] == 1
    assert tot["duplicates"] == 0        # uniqueness proven
    assert tot["half_minted"] is True
    assert tot["fully_generated"] is False


@pytest.mark.asyncio
async def test_per_style_breakdown(db, jp_order):
    svc = BarcodeService(db)
    a = await svc.order_analytics(jp_order["order_id"], client_scope=None)
    by = {r["style_name"]: r for r in a["by_style"]}
    assert by["CLERMONT"]["planned"] == 5 and by["CLERMONT"]["balance"] == 0
    assert by["VEST"]["planned"] == 4 and by["VEST"]["minted"] == 2
    assert by["VEST"]["balance"] == 2


@pytest.mark.asyncio
async def test_history_style_and_size_filter(db, jp_order):
    svc = BarcodeService(db)
    # style filter
    page = await svc.list_history(jp_order["order_id"], None,
                                  style_id=jp_order["styleB"])
    assert page["total"] == 2
    # style + size filter (CLERMONT / L)
    page = await svc.list_history(jp_order["order_id"], None,
                                  style_id=jp_order["styleA"], size="L")
    assert page["total"] == 2


@pytest.mark.asyncio
async def test_history_status_filter_and_pagination(db, jp_order):
    svc = BarcodeService(db)
    retired = await svc.list_history(jp_order["order_id"], None,
                                     status_filter="retired")
    assert retired["total"] == 1
    p1 = await svc.list_history(jp_order["order_id"], None, page=1, page_size=3)
    assert p1["total"] == 7 and len(p1["items"]) == 3 and p1["pages"] == 3


@pytest.mark.asyncio
async def test_order_picker_lists_only_orders_with_barcodes(db, jp_order):
    svc = BarcodeService(db)
    orders = await svc.list_orders(client_scope=None)
    nums = {o["order_number"]: o for o in orders}
    assert "JP-READS" in nums
    assert nums["JP-READS"]["minted"] == 7
    assert nums["JP-READS"]["first_generated_at"] is not None


@pytest.mark.asyncio
async def test_client_tenancy_scope_hides_other_orders(db, jp_order):
    """A CLIENT login scoped to a DIFFERENT client cannot see or read this order."""
    svc = BarcodeService(db)
    other_client = uuid.uuid4()
    orders = await svc.list_orders(client_scope=other_client)
    assert all(o["order_number"] != "JP-READS" for o in orders)
    with pytest.raises(Exception):
        await svc.order_analytics(jp_order["order_id"], client_scope=other_client)