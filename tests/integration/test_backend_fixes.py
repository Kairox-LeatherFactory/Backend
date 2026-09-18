"""
INTEGRATION · the backend items from the bugs list.

Each test names the reported symptom, because the symptom is what makes the fix
checkable by the person who reported it.
"""
import uuid

import pytest

from app.modules.materials.schemas import LotCreate, ReceiveRequest, SheetIn
from app.modules.materials.service import MaterialService

pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════ #26 · "Received is showing as 0"
async def test_received_reports_everything_that_ever_arrived(db):
    """REPORTED: "the Received value is showing as 0 in the backend, even though
    material has been recorded."

    It read 0 because NOTHING EVER ASKED. Every receive has always written a
    material_receipt row, but no read path summed them, so the field the stock
    screen renders had no source at all — not a wrong one.

    RECEIVED IS NOT on_hand, and the difference is the point: on_hand is what is
    LEFT after consumption, received is everything that ever arrived. Both are
    needed to answer "how much of this article have we bought".
    """
    svc = MaterialService(db)
    lot = await svc.create_lot(LotCreate(
        category="LEATHER", article="SUEDE-A32", colour="NAVY",
        attributes={"thickness": "1.2", "dcm": 90}))
    await MaterialService(db).receive(ReceiveRequest(
        lot_id=lot["lot_id"], approved_qty=200, rejected_qty=15), actor_id=None)
    await MaterialService(db).receive(ReceiveRequest(
        lot_id=lot["lot_id"], approved_qty=100, rejected_qty=0), actor_id=None)
    await MaterialService(db).decrement_for_cut_nocommit(lot["lot_id"], 120.0)
    await db.commit()

    d = await MaterialService(db).get_lot(lot["lot_id"])
    assert d["received"] == 390.0, "90 opening + 200 + 100"
    assert d["on_hand"] == 270.0, "390 arrived - 120 cut"
    assert d["rejected"] == 15.0, "supplier quality history survives"
    assert d["deliveries"] == 3, "the opening quantity is a delivery too"
    # THE INVARIANT THE TWO NUMBERS EXIST TO SUPPORT.
    assert d["received"] - d["on_hand"] == 120.0, "received - on_hand == consumed"


async def test_creating_a_lot_counts_as_its_first_delivery(db):
    """Without this, `received` undercounts by exactly the opening quantity.

    A lot created with 90 and topped up by 300 read on_hand 390 / received 300,
    and the two disagreed with nothing to explain the gap. The material arrived
    when the lot was created — that is what creating it means.
    """
    svc = MaterialService(db)
    lot = await svc.create_lot(LotCreate(
        category="ACCESSORY", subtype="BUTTON", article="BTN-18L", colour="NAVY",
        attributes={"size": "18L", "count": 5000}))
    d = await MaterialService(db).get_lot(lot["lot_id"])
    assert d["received"] == 5000.0
    assert d["deliveries"] == 1


# ═══════════════════════════════════════════════════ #16 · purchase history
async def test_every_delivery_of_a_lot_can_be_read_back(db):
    """REPORTED: "there is no way to view past material purchase records — only
    the current stock is visible."

    The rows were always there; nothing exposed them. So this is a query and a
    route, not a migration.
    """
    svc = MaterialService(db)
    lot = await svc.create_lot(LotCreate(
        category="LEATHER", article="NAP-11", colour="BLACK",
        attributes={"thickness": "0.9", "dcm": 50}))
    await MaterialService(db).receive(ReceiveRequest(
        lot_id=lot["lot_id"], approved_qty=120, rejected_qty=8), actor_id=None)

    h = await MaterialService(db).lot_history(lot["lot_id"])
    assert h["received"] == 170.0
    assert h["rejected"] == 8.0
    assert len(h["receipts"]) == 2
    # BOTH deliveries are there, each with its own quantities. The listing is
    # ordered newest-first, but two receipts written in the SAME instant (as
    # these are, inside one test) share a created_at and their relative order is
    # genuinely undefined — so the assertion is on the set, not the sequence.
    assert {r["approved_qty"] for r in h["receipts"]} == {50.0, 120.0}
    assert {r["rejected_qty"] for r in h["receipts"]} == {0.0, 8.0}
    assert all(r["received_at"] for r in h["receipts"]), "each delivery is dated"


# ═══════════════════════════════════════════════ #18 · sheets in the directory
async def test_the_lot_directory_reports_how_many_hides_it_holds(db):
    """REPORTED: "the lot directory does not show the total sheets purchased."

    Suppliers send leather by sheet count — "Sheep Nappa, 5 sheets" — and Kumar
    needs the number without opening the cutting screen.
    """
    svc = MaterialService(db)
    lot = await svc.create_lot(LotCreate(
        category="LEATHER", article="SHEEP-NAPPA", colour="CAFFE",
        attributes={"thickness": "1.0", "dcm": 230},
        sheets=[SheetIn(dcm=d) for d in (43, 47, 47, 46, 47)]))
    d = await MaterialService(db).get_lot(lot["lot_id"])
    assert d["sheets_total"] == 5
    assert d["sheets_by_status"]["IN_STOCK"]["count"] == 5


# ═════════════════════════════════════ #29 · the Analytics piece-code error
async def test_a_garment_opens_by_either_of_its_two_codes(db):
    """REPORTED: Analytics called .../pieces/PC-222223 and errored, because the
    lookup only accepted the long code.

    A piece answers to TWO live codes: the compact primary printed on the sticker
    (what a scanner returns) and the long business identity kept as an alias.
    Matching only Piece.code accepted one and 404'd the other, so a screen passing
    the scanned code could never open a garment. The registry already knows both.
    """
    from app.modules.barcode.models import BarcodeRegistry
    from app.modules.clients.models import Client, ClientOrder, SKU, Style
    from app.modules.dashboard.service import DashboardService
    from app.modules.production.models import Piece

    cl = Client(id=uuid.uuid4(), name="BOGGI"); db.add(cl); await db.flush()
    o = ClientOrder(id=uuid.uuid4(), client_id=cl.id, order_number="SS27")
    db.add(o); await db.flush()
    st = Style(id=uuid.uuid4(), client_order_id=o.id, name="BOMBER", article="A",
               code="ST-29", production_status="RELEASED")
    db.add(st); await db.flush()
    sku = SKU(id=uuid.uuid4(), style_id=st.id, color_code="NAVY",
              color_name="NAVY", size="S", qty_ordered=1, code="SK-29")
    db.add(sku); await db.flush()
    long_code = "N1-BF27P010501-SUEDE_BOMBER-NAVY-S-034"
    piece = Piece(id=uuid.uuid4(), code=long_code, seq=34, sku_id=sku.id)
    db.add(piece); await db.flush()
    db.add(BarcodeRegistry(code="PC-222223", type="PIECE", status="active",
                           piece_id=piece.id, is_alias=False))
    db.add(BarcodeRegistry(code=long_code, type="PIECE", status="active",
                           piece_id=piece.id, is_alias=True))
    await db.commit()

    svc = DashboardService(db)
    by_compact = await svc.piece_trace(piece_code="PC-222223")
    by_long = await svc.piece_trace(piece_code=long_code)
    assert by_compact is not None, "the scanned code must open the garment"
    assert by_long is not None
    assert by_compact.piece_code == by_long.piece_code == long_code
    assert await svc.piece_trace(piece_code="PC-NOTHING") is None
