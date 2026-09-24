"""
SYSTEM · "what is merged into this garment" — over HTTP, through both doors.

WHY BOTH. The store screen and the scan gun ask the same question and must get
the same answer, so they call one service method and publish one response model.
Two handlers over one service is fine; two shapes is how they drift.

WHAT ONLY THIS LAYER SEES: that the response MODEL does not drop the fields the
service returns. `not_applicable` and `issued` are new, and a response_model
that failed to declare them would silently strip them — the service tests would
stay green while the screen received nothing.
"""
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.enums import UserRole
from app.main import app
from app.modules.barcode.models import (
    BarcodeRegistry, MaterialLot, StyleMaterialSpec,
)
from app.modules.clients.models import SKU
from app.modules.production.models import Piece
from app.modules.users.deps import get_current_user

API = "/api/v1"
pytestmark = pytest.mark.asyncio


class FakeUser:
    def __init__(self, role):
        self.id = uuid.uuid4()
        self.role = role
        self.name = role.value
        self.phone = "9000000000"
        self.employee_id = None
        self.client_id = None
        self.is_active = True


@pytest_asyncio.fixture
async def client(db):
    async def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = \
        lambda: FakeUser(UserRole.MANAGING_DIRECTOR)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def navy_piece(db, order_tree):
    """A NAVY garment beside the seeded PINE GREEN colourway, barcoded.

    The shape the field report arrived in: a spec line pinned to one colourway
    and a scan on another.
    """
    style = order_tree["style"]
    navy = SKU(style_id=style.id, color_code="NAVY", color_name="NAVY",
               size="S", qty_ordered=10, code="JP-CLERMONT-NAVY-S")
    db.add(navy)
    await db.flush()
    piece = Piece(code="JP-CLERMONT-NAVY-S-004", seq=4, sku_id=navy.id)
    db.add(piece)
    await db.flush()
    db.add(BarcodeRegistry(code=piece.code, type="PIECE", status="ACTIVE",
                           piece_id=piece.id))
    db.add(MaterialLot(category="ACCESSORY", subtype="BUTTON",
                       article="BTN-4H", colour="NAVY", size="18L", uom="pcs",
                       on_hand=500, is_active=True))
    # One line for THIS garment, one pinned to the other colourway.
    db.add(StyleMaterialSpec(
        style_id=style.id, sku_id=None, category="ACCESSORY", subtype="BUTTON",
        article="BTN-4H", colour="NAVY", size="18L", qty_per_piece=4,
        uom="pcs"))
    db.add(StyleMaterialSpec(
        style_id=style.id, sku_id=order_tree["sku"].id, category="ACCESSORY",
        subtype="THREAD", article="THREAD", colour="PINE GREEN",
        qty_per_piece=1, uom="mtrs"))
    await db.commit()
    await db.refresh(piece)
    return piece


async def test_the_store_door_returns_both_halves(client, navy_piece):
    r = await client.get(f"{API}/store/pieces/{navy_piece.code}/materials")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["piece_code"] == navy_piece.code
    assert body["sku_label"] == "NAVY · S"
    assert body["garment_size"] == "S"

    # THE HALF THAT APPLIES.
    assert [a["article"] for a in body["applies"]["accessories"]] == ["BTN-4H"]

    # THE HALF THAT DOES NOT — the field that makes the confusion visible, and
    # the one a response_model would silently strip if it forgot to declare it.
    assert len(body["not_applicable"]) == 1
    dropped = body["not_applicable"][0]
    assert dropped["article"] == "THREAD"
    assert dropped["reason"] == "other_sku"
    assert "PINE GREEN" in dropped["reason_note"]


async def test_the_scan_gun_door_returns_the_same_thing(client, navy_piece):
    """One question, one answer, whichever screen asked it."""
    store = await client.get(f"{API}/store/pieces/{navy_piece.code}/materials")
    gun = await client.get(f"{API}/barcode/pieces/{navy_piece.code}/materials")
    assert gun.status_code == 200, gun.text

    a, b = store.json(), gun.json()
    # `needs_lining` is the store's own overlay; everything about the MATERIALS
    # must match exactly.
    for field in ("piece_code", "sku_label", "applies", "not_applicable",
                  "issued", "kit_required", "kit_status"):
        assert a[field] == b[field], f"the two doors disagree about {field}"


async def test_the_ledger_shows_what_was_actually_issued(client, navy_piece,
                                                         cutter, db):
    """`issued` is the record of what went in the bag, not what was asked for."""
    scan = await client.post(f"{API}/store/scan", json={
        "employee_id": str(cutter[0].id),
        "piece_barcode": navy_piece.code,
        "part": "ACCESSORY"})
    assert scan.status_code == 201, scan.text

    body = (await client.get(
        f"{API}/store/pieces/{navy_piece.code}/materials")).json()
    assert len(body["issued"]) == 1
    row = body["issued"][0]
    assert row["article"] == "BTN-4H"
    assert row["qty"] == 4.0
    assert row["source"] == "STORE_KIT"
    assert row["issued_by_employee_id"] == str(cutter[0].id)
    # And the checklist now shows nothing owed on that line.
    assert body["applies"]["accessories"][0]["outstanding"] == 0.0


async def test_an_unknown_barcode_is_404_not_a_crash(client):
    r = await client.get(f"{API}/store/pieces/NOPE-0001/materials")
    assert r.status_code == 404
