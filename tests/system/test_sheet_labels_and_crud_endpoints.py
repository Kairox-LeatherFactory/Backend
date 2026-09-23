"""
SYSTEM · the hide label screen and the two correction surfaces, over HTTP.

THE REPORTED BUG, PINNED. Creating a leather lot with `sheets` mints a
LEATHER_SHEET barcode per skin and returns them — and GET /barcode/materials
listed only the three LOT types, so those codes existed in the registry and no
screen could reprint them. `test_a_sheeted_lot_lists_its_hides_too` is the
regression; it fails against the old listing.

The rest is the CRUD that did not exist: a hide could not be read, corrected,
added or removed, and an arrival could not be read, corrected or voided. Every
409 here is a state rule — untouched is correctable, acted-upon is history.
"""
import uuid

import pytest

from app.core.enums import UserRole

API = "/api/v1"

pytestmark = pytest.mark.security


@pytest.fixture
async def sheeted(api_client, as_role):
    """The exact request from the bug report: one lot, four hides."""
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/materials/lots", json={
        "category": "LEATHER", "subtype": None, "article": "SHEEP GLESS",
        "colour": "BLACK",
        "attributes": {"thickness": "0.7", "dcm": 3400},
        "sheets": [{"dcm": 45}, {"dcm": 54}, {"dcm": 40}, {"dcm": 30}]})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
async def arrival(api_client, as_role):
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/materials/arrivals", json={
        "article": "SHEEP GLESS", "colour": "BLACK", "total_qty": 3400,
        "category": "LEATHER", "sheet_count": 4, "note": "van 2"})
    assert r.status_code == 201, r.text
    return r.json()


# ══════════════════════════════════════════ GET /barcode/materials
class TestMaterialBarcodeScreen:
    async def test_a_sheeted_lot_lists_its_hides_too(
            self, api_client, as_role, sheeted):
        """THE REPORTED BUG. Four hide labels were minted and the screen showed
        only the lot."""
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/materials")
        assert r.status_code == 200, r.text
        items = r.json()["items"]

        kinds = [i["kind"] for i in items]
        assert kinds.count("LOT") == 1
        assert kinds.count("SHEET") == 4

        codes = {i["code"] for i in items if i["kind"] == "SHEET"}
        assert codes == {s["code"] for s in sheeted["sheets"]}

    async def test_a_hide_row_prints_its_own_measurement_not_the_lot_total(
            self, api_client, as_role, sheeted):
        """The lot's 3400 dcm is not true of the skin in your hand."""
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/materials",
                                 params={"kind": "SHEET"})
        row = next(i for i in r.json()["items"] if i["dcm"] == 45.0)
        assert row["label_line"] == "SHEEP GLESS · BLACK · 0.7 · 45 dcm"
        assert row["sheet_status"] == "IN_STOCK"
        assert row["sheet_id"] is not None
        assert row["cutting_row_id"] is None

    async def test_a_lot_row_carries_no_hide_fields(
            self, api_client, as_role, sheeted):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/materials",
                                 params={"kind": "LOT"})
        assert r.json()["total"] == 1
        row = r.json()["items"][0]
        assert row["code"] == sheeted["lot_barcode"]
        assert row["dcm"] is None and row["sheet_id"] is None
        assert row["on_hand"] == 3400.0

    async def test_each_lot_is_followed_by_its_own_hides(
            self, api_client, as_role, sheeted):
        """A print queue is read top to bottom; a hide three pages from its lot
        is useless."""
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/materials")
        kinds = [i["kind"] for i in r.json()["items"]]
        assert kinds[0] == "LOT" and set(kinds[1:]) == {"SHEET"}

    async def test_an_unsheeted_lot_still_lists_exactly_one_row(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        await api_client.post(f"{API}/materials/lots", json={
            "category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-4H",
            "colour": "BLACK", "attributes": {"size": "18L", "count": 500}})
        r = await api_client.get(f"{API}/barcode/materials",
                                 params={"category": "ACCESSORY"})
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["kind"] == "LOT"

    async def test_an_unknown_kind_is_refused_at_the_boundary(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/materials",
                                 params={"kind": "PALLET"})
        assert r.status_code == 422

    async def test_a_deleted_hides_label_leaves_the_active_screen(
            self, api_client, as_role, sheeted):
        as_role(UserRole.DIRECT_MANAGER)
        await api_client.delete(
            f"{API}/materials/sheets/{sheeted['sheets'][0]['sheet_id']}")
        r = await api_client.get(f"{API}/barcode/materials",
                                 params={"kind": "SHEET"})
        assert r.json()["total"] == 3
        greyed = await api_client.get(
            f"{API}/barcode/materials",
            params={"kind": "SHEET", "active_only": "false"})
        assert greyed.json()["total"] == 4


# ══════════════════════════════════════════════════════ hide CRUD
class TestSheetEndpoints:
    async def test_the_hides_of_a_lot_are_listed_with_their_roll_up(
            self, api_client, as_role, sheeted):
        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.get(
            f"{API}/materials/lots/{sheeted['lot_id']}/sheets")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 4
        assert body["reconciliation"]["sheet_dcm_in_store"] == 169.0

    async def test_one_hide_is_opened_by_id(self, api_client, as_role, sheeted):
        as_role(UserRole.HR)
        sheet = sheeted["sheets"][0]
        r = await api_client.get(f"{API}/materials/sheets/{sheet['sheet_id']}")
        assert r.status_code == 200, r.text
        assert r.json()["dcm"] == 45.0
        assert r.json()["lot_barcode"] == sheeted["lot_barcode"]
        assert r.json()["editable"] is True

    async def test_a_mistyped_measurement_is_corrected(
            self, api_client, as_role, sheeted):
        as_role(UserRole.CUTTING_MANAGER)
        sheet = sheeted["sheets"][0]
        r = await api_client.patch(
            f"{API}/materials/sheets/{sheet['sheet_id']}", json={"dcm": 4.5})
        assert r.status_code == 200, r.text
        assert r.json()["dcm"] == 4.5

    async def test_a_zero_measurement_never_reaches_the_service(
            self, api_client, as_role, sheeted):
        """`dcm: float | None = Field(gt=0)` on SheetPatch."""
        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.patch(
            f"{API}/materials/sheets/{sheeted['sheets'][0]['sheet_id']}",
            json={"dcm": 0})
        assert r.status_code == 422

    async def test_a_missed_hide_is_added_at_201(
            self, api_client, as_role, sheeted):
        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.post(
            f"{API}/materials/lots/{sheeted['lot_id']}/sheets",
            json={"sheets": [{"dcm": 61, "note": "under the bundle"}]})
        assert r.status_code == 201, r.text
        assert r.json()["added"][0]["dcm"] == 61.0
        assert r.json()["reconciliation"]["sheet_dcm_in_store"] == 230.0

    async def test_adding_no_hides_is_refused_at_the_boundary(
            self, api_client, as_role, sheeted):
        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.post(
            f"{API}/materials/lots/{sheeted['lot_id']}/sheets",
            json={"sheets": []})
        assert r.status_code == 422

    async def test_a_hide_entered_by_mistake_is_deleted(
            self, api_client, as_role, sheeted):
        as_role(UserRole.DIRECT_MANAGER)
        sheet = sheeted["sheets"][3]
        r = await api_client.delete(f"{API}/materials/sheets/{sheet['sheet_id']}")
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] is True
        assert r.json()["barcode_retired"] is True

        gone = await api_client.get(f"{API}/materials/sheets/{sheet['sheet_id']}")
        assert gone.status_code == 404

    async def test_the_deleted_hides_label_scans_410_not_404(
            self, api_client, as_role, sheeted):
        """A label may already be stuck on something."""
        as_role(UserRole.DIRECT_MANAGER)
        sheet = sheeted["sheets"][3]
        await api_client.delete(f"{API}/materials/sheets/{sheet['sheet_id']}")
        r = await api_client.get(f"{API}/barcode/resolve",
                                 params={"code": sheet["code"]})
        assert r.status_code == 410

    async def test_an_unknown_hide_is_404_on_every_route(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        ghost = uuid.uuid4()
        assert (await api_client.get(
            f"{API}/materials/sheets/{ghost}")).status_code == 404
        assert (await api_client.patch(
            f"{API}/materials/sheets/{ghost}", json={"dcm": 1})).status_code == 404
        assert (await api_client.delete(
            f"{API}/materials/sheets/{ghost}")).status_code == 404

    @pytest.mark.parametrize("role", [
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR,
        UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER, UserRole.HR])
    async def test_the_lot_writers_may_correct_a_hide(
            self, api_client, as_role, sheeted, role):
        as_role(role)
        r = await api_client.patch(
            f"{API}/materials/sheets/{sheeted['sheets'][0]['sheet_id']}",
            json={"note": role.value})
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.SECURITY,
                                      UserRole.STORE_MANAGER])
    async def test_everyone_else_may_not(
            self, api_client, as_role, sheeted, role):
        as_role(role)
        r = await api_client.delete(
            f"{API}/materials/sheets/{sheeted['sheets'][0]['sheet_id']}")
        assert r.status_code == 403

    async def test_the_floor_may_read_hides(self, api_client, as_role, sheeted):
        as_role(UserRole.STORE_MANAGER)
        r = await api_client.get(
            f"{API}/materials/lots/{sheeted['lot_id']}/sheets")
        assert r.status_code == 200


# ══════════════════════════════════════════════════════ arrival CRUD
class TestArrivalEndpoints:
    async def test_one_arrival_is_opened_by_id(self, api_client, as_role, arrival):
        as_role(UserRole.HR)
        r = await api_client.get(
            f"{API}/materials/arrivals/{arrival['receipt_id']}")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "PENDING"
        assert r.json()["declared_qty"] == 3400.0
        assert r.json()["note"] == "van 2"

    async def test_correcting_the_quantity_moves_the_stock(
            self, api_client, as_role, arrival):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.patch(
            f"{API}/materials/arrivals/{arrival['receipt_id']}",
            json={"declared_qty": 340})
        assert r.status_code == 200, r.text
        assert r.json()["declared_qty"] == 340.0

        lot = await api_client.get(f"{API}/materials/lots/{arrival['lot_id']}")
        assert lot.json()["balance"] == 340.0

    async def test_a_zero_quantity_never_reaches_the_service(
            self, api_client, as_role, arrival):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.patch(
            f"{API}/materials/arrivals/{arrival['receipt_id']}",
            json={"declared_qty": 0})
        assert r.status_code == 422

    async def test_the_van_entered_twice_is_voided(
            self, api_client, as_role, arrival):
        as_role(UserRole.DIRECT_MANAGER)
        second = await api_client.post(f"{API}/materials/arrivals", json={
            "article": "SHEEP GLESS", "colour": "BLACK", "total_qty": 3400,
            "category": "LEATHER"})
        assert second.status_code == 201

        r = await api_client.delete(
            f"{API}/materials/arrivals/{second.json()['receipt_id']}")
        assert r.status_code == 200, r.text
        assert r.json()["voided"] is True
        assert r.json()["qty_removed"] == 3400.0
        assert r.json()["lot_retained"] is True

        lot = await api_client.get(f"{API}/materials/lots/{arrival['lot_id']}")
        assert lot.json()["balance"] == 3400.0

    async def test_a_voided_arrival_leaves_the_queue(
            self, api_client, as_role, arrival):
        as_role(UserRole.DIRECT_MANAGER)
        assert (await api_client.get(
            f"{API}/materials/arrivals")).json()["total"] == 1
        await api_client.delete(
            f"{API}/materials/arrivals/{arrival['receipt_id']}")
        assert (await api_client.get(
            f"{API}/materials/arrivals")).json()["total"] == 0

    async def test_a_completed_arrival_is_frozen_at_409(
            self, api_client, as_role, arrival):
        as_role(UserRole.DIRECT_MANAGER)
        done = await api_client.post(
            f"{API}/materials/arrivals/{arrival['receipt_id']}/complete",
            json={"approved_qty": 3400, "rejected_qty": 0})
        assert done.status_code == 200, done.text

        assert (await api_client.patch(
            f"{API}/materials/arrivals/{arrival['receipt_id']}",
            json={"declared_qty": 340})).status_code == 409
        assert (await api_client.delete(
            f"{API}/materials/arrivals/{arrival['receipt_id']}")).status_code == 409

    async def test_an_unknown_arrival_is_404_on_every_route(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        ghost = uuid.uuid4()
        assert (await api_client.get(
            f"{API}/materials/arrivals/{ghost}")).status_code == 404
        assert (await api_client.patch(
            f"{API}/materials/arrivals/{ghost}",
            json={"note": "x"})).status_code == 404
        assert (await api_client.delete(
            f"{API}/materials/arrivals/{ghost}")).status_code == 404

    @pytest.mark.parametrize("role", [UserRole.CUTTING_MANAGER,
                                      UserRole.STORE_MANAGER])
    async def test_voiding_is_restricted_to_the_receivers(
            self, api_client, as_role, arrival, role):
        as_role(role)
        r = await api_client.delete(
            f"{API}/materials/arrivals/{arrival['receipt_id']}")
        assert r.status_code == 403
