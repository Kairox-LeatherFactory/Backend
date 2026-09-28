"""
SYSTEM · every MATERIAL and BARCODE endpoint, over HTTP.

ONE TEST PER ROUTE, PLUS ITS DOOR. The service layer already proves what the
rules DO (tests/integration/test_materials_service_full.py,
test_barcode_service_full.py, test_style_spec_service_full.py). This file proves
the things only the wire can show:

    the STATUS CODE       a service raising HTTPException(409) is not the same
                          fact as the client receiving 409
    the ROLE GATE         who may reach it at all
    the RESPONSE MODEL    FastAPI serialises THROUGH response_model and silently
                          DROPS any key the model does not name. That bug is
                          invisible at the service layer and has bitten this
                          module before (barcode/schemas.py:24-34: resolve()
                          returned `sheet` correctly and the operator saw
                          nothing)
    the ROUTE ORDER       /materials/lots/{id}/history must win over
                          /materials/lots/{id}, or "history" is parsed as a
                          uuid and 422s

THE FULL ROUTE INVENTORY covered here — 11 barcode (10 under /barcode plus
PATCH /employees/{id}/barcode), 16 materials + suppliers, and 8 style
material-spec routes: 35 endpoints, which is every route the two modules
register in main.py.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.enums import BarcodeStatus, BarcodeType, UserRole
from app.modules.barcode.models import (
    BarcodeRegistry, MaterialLot, MaterialSupplier, SupplierOrder,
)
from app.modules.barcode.repository import encode_short

API = "/api/v1"

pytestmark = pytest.mark.security


# ══════════════════════════════════════════════════════════════ fixtures
@pytest.fixture
async def lot(db, api_client, as_role):
    """A leather lot created THROUGH the API, so its barcode and opening
    receipt exist the way production makes them."""
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/materials/lots", json={
        "category": "LEATHER", "article": "SUEDE-A32", "colour": "PINE",
        "attributes": {"thickness": "1.2mm", "dcm": 400}})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
async def accessory_lot(db, api_client, as_role):
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/materials/lots", json={
        "category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-4H",
        "colour": "BLACK", "attributes": {"size": "18L", "count": 500}})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
async def minted(db, order_tree, pieces):
    """Registry rows as premint writes them: a compact primary code carrying
    order/style/sku, and the long code kept alive as an alias."""
    codes = []
    for i, piece in enumerate(pieces, start=1):
        legacy = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.code == piece.code))
        legacy.is_alias = True
        code = encode_short(i)
        db.add(BarcodeRegistry(
            code=code, type=BarcodeType.PIECE.value,
            status=BarcodeStatus.ACTIVE.value, piece_id=piece.id,
            caption=piece.code, order_id=order_tree["order"].id,
            style_id=order_tree["style"].id, sku_id=order_tree["sku"].id))
        codes.append(code)
    await db.commit()
    return codes


@pytest.fixture
async def draft_style(db, order_tree):
    style = order_tree["style"]
    style.production_status = "DRAFT"
    style.code = "JP-CLERMONT"
    await db.commit()
    return style


LEATHER_SPEC_LINE = {"category": "LEATHER", "article": "SUEDE-A32",
                     "colour": "PINE", "thickness": "1.2mm",
                     "qty_per_piece": 12.5}
BUTTON_SPEC_LINE = {"category": "ACCESSORY", "subtype": "BUTTON",
                    "article": "BTN-4H", "colour": "BLACK", "size": "18L",
                    "qty_per_piece": 4}


# ══════════════════════════════════════════════════════════════════════════
# BARCODE — GET /barcode/resolve
# ══════════════════════════════════════════════════════════════════════════
class TestResolveEndpoint:
    async def test_a_scan_returns_the_type_and_its_payload(
            self, api_client, as_role, pieces):
        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.get(f"{API}/barcode/resolve",
                                 params={"code": pieces[0].code})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "PIECE"
        assert body["piece"]["style_name"] == "CLERMONT"
        assert body["next_stage"] == "LEATHER_CUTTING"

    async def test_resolve_stays_reachable_for_the_gate_operator(
            self, api_client, as_role, cutter):
        """SECURITY resolves a worker's card before checking them in, so this
        route is deliberately NOT behind the manager gate (main.py:371-374)."""
        as_role(UserRole.SECURITY)
        r = await api_client.get(f"{API}/barcode/resolve",
                                 params={"code": cutter[1].code})
        assert r.status_code == 200, r.text
        assert r.json()["employee"]["name"] == "RAMESH"

    async def test_an_anonymous_scan_is_refused(self, api_client, pieces):
        r = await api_client.get(f"{API}/barcode/resolve",
                                 params={"code": pieces[0].code})
        assert r.status_code in (401, 403)

    async def test_an_unknown_code_is_404_on_the_wire(self, api_client, as_role):
        as_role(UserRole.HR)
        r = await api_client.get(f"{API}/barcode/resolve",
                                 params={"code": "NOPE"})
        assert r.status_code == 404

    async def test_a_deactivated_card_is_410_on_the_wire(
            self, api_client, as_role, db, cutter):
        """410 Gone, not 404 — so the screen can say "this card was
        deactivated" rather than "invalid barcode"."""
        from app.modules.barcode.service import BarcodeService
        await BarcodeService(db).deactivate_employee_barcode(cutter[0].id, None)
        as_role(UserRole.HR)
        r = await api_client.get(f"{API}/barcode/resolve",
                                 params={"code": cutter[1].code})
        assert r.status_code == 410

    async def test_the_code_parameter_is_required(self, api_client, as_role):
        as_role(UserRole.HR)
        assert (await api_client.get(f"{API}/barcode/resolve")).status_code == 422

    async def test_a_hide_scan_reaches_the_client_through_the_response_model(
            self, api_client, as_role, db, lot):
        """REGRESSION. `sheet` had to be declared on BarcodeResolve explicitly:
        FastAPI serialises through the response_model and drops what it does
        not name, so the service was right and the operator still saw nothing.
        This assertion only fails over HTTP."""
        from app.modules.barcode.models import MaterialSheet
        from app.core.enums import SheetStatus
        sheet = MaterialSheet(code="LS-000001", material_lot_id=lot["lot_id"],
                              dcm=Decimal("43"),
                              status=SheetStatus.IN_STOCK.value)
        db.add(sheet)
        await db.flush()
        db.add(BarcodeRegistry(
            code="LS-000001", type=BarcodeType.LEATHER_SHEET.value,
            status=BarcodeStatus.ACTIVE.value, material_sheet_id=sheet.id,
            material_lot_id=lot["lot_id"]))
        await db.commit()

        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.get(f"{API}/barcode/resolve",
                                 params={"code": "LS-000001"})
        assert r.status_code == 200, r.text
        assert r.json()["sheet"]["dcm"] == 43.0
        assert r.json()["lot"]["article"] == "SUEDE-A32"


# ══════════════════════════════════════════════════════ POST /barcode/print
class TestPrintEndpoint:
    async def test_a_print_run_returns_labels(self, api_client, as_role, pieces):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/barcode/print",
                                  json={"codes": [pieces[0].code]})
        assert r.status_code == 200, r.text
        label = r.json()["labels"][0]
        assert label["symbology"] == "code128"
        assert label["details"]["serial"] == "001"

    async def test_a_request_naming_no_source_is_refused(self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/barcode/print", json={})
        assert r.status_code == 422
        assert "codes, sku_id, or order_id" in r.text

    @pytest.mark.parametrize("role", [
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR,
        UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER, UserRole.HR])
    async def test_the_roles_that_print_labels_may_reach_it(
            self, api_client, as_role, pieces, role):
        as_role(role)
        r = await api_client.post(f"{API}/barcode/print",
                                  json={"codes": [pieces[0].code]})
        assert r.status_code == 200

    @pytest.mark.parametrize("role", [UserRole.SECURITY, UserRole.VIEWER,
                                      UserRole.CLIENT])
    async def test_everyone_else_is_refused(self, api_client, as_role, role):
        as_role(role)
        r = await api_client.post(f"{API}/barcode/print", json={"codes": ["X"]})
        assert r.status_code == 403


# ══════════════════════════════════ PATCH /employees/{id}/barcode
class TestEmployeeBarcodeEndpoint:
    async def test_a_card_is_reissued(self, api_client, as_role, cutter):
        as_role(UserRole.HR)
        r = await api_client.patch(
            f"{API}/employees/{cutter[0].id}/barcode", json={"action": "reissue"})
        assert r.status_code == 200, r.text
        assert r.json()["active"] is True
        assert r.json()["employee_barcode"] != cutter[1].code
        assert r.json()["history_preserved"] is True

    async def test_a_card_is_deactivated(self, api_client, as_role, cutter):
        as_role(UserRole.MANAGING_DIRECTOR)
        r = await api_client.patch(
            f"{API}/employees/{cutter[0].id}/barcode",
            json={"action": "deactivate"})
        assert r.status_code == 200, r.text
        assert r.json()["active"] is False

    async def test_an_unknown_action_is_refused_at_the_boundary(
            self, api_client, as_role, cutter):
        as_role(UserRole.HR)
        r = await api_client.patch(
            f"{API}/employees/{cutter[0].id}/barcode", json={"action": "delete"})
        assert r.status_code == 422

    async def test_deactivating_a_worker_with_no_card_is_404(
            self, api_client, as_role, cutter):
        as_role(UserRole.HR)
        await api_client.patch(f"{API}/employees/{cutter[0].id}/barcode",
                               json={"action": "deactivate"})
        r = await api_client.patch(f"{API}/employees/{cutter[0].id}/barcode",
                                   json={"action": "deactivate"})
        assert r.status_code == 404

    @pytest.mark.parametrize("role", [UserRole.CUTTING_MANAGER,
                                      UserRole.SUPERVISOR, UserRole.SECURITY])
    async def test_the_floor_cannot_reissue_a_card(
            self, api_client, as_role, cutter, role):
        as_role(role)
        r = await api_client.patch(f"{API}/employees/{cutter[0].id}/barcode",
                                   json={"action": "reissue"})
        assert r.status_code == 403


# ══════════════════════════════════════════════ the barcode screens
class TestBarcodeScreens:
    async def test_the_material_label_list_is_paged(
            self, api_client, as_role, lot):
        as_role(UserRole.SUPERVISOR)
        r = await api_client.get(f"{API}/barcode/materials")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["code"] == lot["lot_barcode"]
        assert body["items"][0]["label_line"].startswith("SUEDE-A32")

    async def test_the_material_label_list_narrows_by_category(
            self, api_client, as_role, lot, accessory_lot):
        as_role(UserRole.HR)
        r = await api_client.get(f"{API}/barcode/materials",
                                 params={"category": "ACCESSORY"})
        assert r.json()["total"] == 1

    async def test_retired_labels_are_shown_only_when_asked_for(
            self, api_client, as_role, db, lot):
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.material_lot_id == uuid.UUID(lot["lot_id"])))
        row.status = BarcodeStatus.RETIRED.value
        await db.commit()
        as_role(UserRole.HR)
        assert (await api_client.get(
            f"{API}/barcode/materials")).json()["total"] == 0
        assert (await api_client.get(
            f"{API}/barcode/materials",
            params={"active_only": "false"})).json()["total"] == 1

    async def test_the_order_picker_lists_orders_that_minted_barcodes(
            self, api_client, as_role, minted):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/orders")
        assert r.status_code == 200, r.text
        assert r.json()["items"][0]["order_number"] == "JP-PO"
        assert r.json()["items"][0]["minted"] == 5

    async def test_the_sku_options_populate_the_filter_dropdowns(
            self, api_client, as_role, order_tree, minted):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(
            f"{API}/barcode/orders/{order_tree['order'].id}/skus")
        assert r.status_code == 200, r.text
        assert r.json()[0]["size"] == "M"

    async def test_order_analytics_proves_planned_against_generated(
            self, api_client, as_role, order_tree, minted):
        as_role(UserRole.MANAGING_DIRECTOR)
        r = await api_client.get(
            f"{API}/barcode/orders/{order_tree['order'].id}/analytics")
        assert r.status_code == 200, r.text
        total = r.json()["order_total"]
        assert total["planned"] == 5 and total["generated"] == 5
        assert total["duplicates"] == 0 and total["fully_generated"] is True

    async def test_the_history_table_is_filterable_and_paged(
            self, api_client, as_role, order_tree, minted):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(
            f"{API}/barcode/orders/{order_tree['order'].id}/barcodes",
            params={"size": "M", "status": "active", "page": 1, "page_size": 2})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 5 and body["pages"] == 3
        assert len(body["items"]) == 2
        assert body["items"][0]["article"] == "CL1"

    async def test_an_unknown_status_filter_is_refused_at_the_boundary(
            self, api_client, as_role, order_tree, minted):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(
            f"{API}/barcode/orders/{order_tree['order'].id}/barcodes",
            params={"status": "shredded"})
        assert r.status_code == 422

    async def test_an_unknown_order_is_404_on_every_order_screen(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        ghost = uuid.uuid4()
        for suffix in ("skus", "analytics", "barcodes"):
            r = await api_client.get(
                f"{API}/barcode/orders/{ghost}/{suffix}")
            assert r.status_code == 404, suffix

    async def test_an_order_number_resolves_to_its_picker_row(
            self, api_client, as_role, minted):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/orders/by-number/JP-PO")
        assert r.status_code == 200, r.text
        assert r.json()["minted"] == 5

    async def test_an_order_with_no_barcodes_yet_is_404_from_the_picker(
            self, api_client, as_role, order_tree):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/orders/by-number/JP-PO")
        assert r.status_code == 404
        assert "no barcodes yet" in r.text

    async def test_the_detail_click_through_returns_the_scan_payload(
            self, api_client, as_role, pieces):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/detail",
                                 params={"code": pieces[0].code})
        assert r.status_code == 200, r.text
        assert r.json()["piece"]["seq"] == 1

    async def test_the_scan_gun_door_onto_a_garments_materials(
            self, api_client, as_role, db, draft_style, pieces,
            accessory_lot):
        """The same service call GET /store/pieces/{code}/materials makes, so
        the store screen and the gun cannot disagree about what is in a
        garment."""
        as_role(UserRole.DIRECT_MANAGER)
        await api_client.put(
            f"{API}/styles/{draft_style.id}/material-spec",
            json={"lines": [BUTTON_SPEC_LINE]})

        r = await api_client.get(
            f"{API}/barcode/pieces/{pieces[0].code}/materials")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["piece_code"] == pieces[0].code
        assert body["applies"]["accessories"][0]["article"] == "BTN-4H"

    async def test_the_gun_door_404s_on_an_unknown_garment(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/barcode/pieces/NOPE/materials")
        assert r.status_code == 404

    @pytest.mark.parametrize("path", [
        "/barcode/materials", "/barcode/orders", "/barcode/detail"])
    @pytest.mark.parametrize("role", [UserRole.CLIENT, UserRole.VIEWER])
    async def test_the_barcode_screens_are_staff_only(
            self, api_client, as_role, path, role):
        as_role(role)
        r = await api_client.get(f"{API}{path}", params={"code": "X"})
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# MATERIALS
# ══════════════════════════════════════════════════════════════════════════
class TestLotEndpoints:
    async def test_creating_a_lot_returns_201_with_its_barcode(
            self, api_client, as_role):
        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.post(f"{API}/materials/lots", json={
            "category": "LEATHER", "article": "NAP-11", "colour": "NAVY",
            "attributes": {"thickness": "0.8mm", "dcm": 120}})
        assert r.status_code == 201, r.text
        assert r.json()["lot_barcode"].startswith("LOT-LEA-")
        assert r.json()["available"] == 120.0

    async def test_a_missing_required_attribute_is_422_on_the_wire(
            self, api_client, as_role):
        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.post(f"{API}/materials/lots", json={
            "category": "LEATHER", "article": "X", "colour": "Y",
            "attributes": {"dcm": 10}})
        assert r.status_code == 422 and "thickness" in r.text

    async def test_a_duplicate_spec_is_409_on_the_wire(
            self, api_client, as_role, lot):
        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.post(f"{API}/materials/lots", json={
            "category": "LEATHER", "article": "SUEDE-A32", "colour": "PINE",
            "attributes": {"thickness": "1.2mm", "dcm": 50}})
        assert r.status_code == 409 and "one lot per spec" in r.text

    @pytest.mark.parametrize("role", [
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR,
        UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER, UserRole.HR])
    async def test_hr_is_a_lot_writer_beside_the_floor_managers(
            self, api_client, as_role, role):
        """#2 — HR could read stock but not correct it, so a wrong lot had to be
        fixed in the database by hand. An audited API call replaces that."""
        as_role(role)
        r = await api_client.post(f"{API}/materials/lots", json={
            "category": "LINING", "subtype": "RIBS",
            "article": f"RIB-{role.value}", "colour": "GREY",
            "attributes": {"kg": 5}})
        assert r.status_code == 201, r.text

    @pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.SECURITY,
                                      UserRole.STITCHING_MANAGER])
    async def test_everyone_else_may_not_create_a_lot(
            self, api_client, as_role, role):
        as_role(role)
        r = await api_client.post(f"{API}/materials/lots", json={
            "category": "LEATHER", "article": "X", "colour": "Y",
            "attributes": {"thickness": "1mm", "dcm": 1}})
        assert r.status_code == 403

    async def test_the_form_definition_is_served_per_category(
            self, api_client, as_role):
        as_role(UserRole.STORE_MANAGER)
        r = await api_client.get(f"{API}/materials/spec",
                                 params={"category": "ACCESSORY",
                                         "subtype": "BUTTON"})
        assert r.status_code == 200, r.text
        assert r.json()["quantity_field"] == "count"
        assert r.json()["uom"] == "pcs"
        assert "size" in r.json()["filters"]

    async def test_the_category_is_required_on_the_form_definition(
            self, api_client, as_role):
        as_role(UserRole.HR)
        assert (await api_client.get(f"{API}/materials/spec")).status_code == 422

    async def test_the_lot_picker_hands_the_cut_screen_a_lot_id(
            self, api_client, as_role, lot):
        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.get(f"{API}/materials/lots",
                                 params={"category": "LEATHER", "required": 100})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 1
        assert body["lots"][0]["lot_id"] == lot["lot_id"]
        assert body["lots"][0]["covers_required"] is True
        assert body["options"]["article"] == ["SUEDE-A32"]

    async def test_one_lot_is_opened_by_id(self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.get(f"{API}/materials/lots/{lot['lot_id']}")
        assert r.status_code == 200, r.text
        assert r.json()["received"] == 400.0
        assert r.json()["deliveries"] == 1
        assert "thickness" in r.json()["editable_fields"]

    async def test_the_history_route_wins_over_the_bare_lot_route(
            self, api_client, as_role, lot):
        """Declared BEFORE /lots/{lot_id}: FastAPI matches in declaration order,
        and the bare route would otherwise swallow "history" as a lot id and 422
        on the uuid parse."""
        as_role(UserRole.HR)
        r = await api_client.get(f"{API}/materials/lots/{lot['lot_id']}/history")
        assert r.status_code == 200, r.text
        assert len(r.json()["receipts"]) == 1

    async def test_an_unknown_lot_is_404_on_both_reads(self, api_client, as_role):
        as_role(UserRole.HR)
        ghost = uuid.uuid4()
        assert (await api_client.get(
            f"{API}/materials/lots/{ghost}")).status_code == 404
        assert (await api_client.get(
            f"{API}/materials/lots/{ghost}/history")).status_code == 404

    async def test_a_non_uuid_lot_id_is_422_not_500(self, api_client, as_role):
        as_role(UserRole.HR)
        r = await api_client.get(f"{API}/materials/lots/not-a-uuid")
        assert r.status_code == 422

    async def test_a_lots_identity_is_corrected_by_patch(
            self, api_client, as_role, lot):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.patch(f"{API}/materials/lots/{lot['lot_id']}",
                                   json={"colour": "FOREST"})
        assert r.status_code == 200, r.text
        assert r.json()["colour"] == "FOREST"

    async def test_the_patch_body_does_not_even_declare_on_hand(
            self, api_client, as_role, lot):
        """Mass-assignment shape: on_hand is a LEDGER and is absent from
        LotPatch, so Pydantic drops it rather than the service refusing it."""
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.patch(f"{API}/materials/lots/{lot['lot_id']}",
                                   json={"colour": "FOREST", "on_hand": 99999})
        assert r.status_code == 200, r.text
        assert r.json()["on_hand"] == 400.0

    async def test_an_empty_patch_is_422(self, api_client, as_role, lot):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.patch(f"{API}/materials/lots/{lot['lot_id']}",
                                   json={})
        assert r.status_code == 422 and "Nothing to update" in r.text

    async def test_a_counted_correction_moves_stock_with_a_reason(
            self, api_client, as_role, lot):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.patch(
            f"{API}/materials/lots/{lot['lot_id']}/adjust",
            json={"delta": -40, "reason": "counted short at stocktake"})
        assert r.status_code == 200, r.text
        assert r.json()["on_hand"] == 360.0

    async def test_a_correction_without_a_reason_never_reaches_the_service(
            self, api_client, as_role, lot):
        """`reason` is min_length=3 on LotAdjust, so the boundary refuses it."""
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.patch(
            f"{API}/materials/lots/{lot['lot_id']}/adjust",
            json={"delta": -1, "reason": "x"})
        assert r.status_code == 422

    async def test_a_correction_below_the_reserved_quantity_is_409(
            self, api_client, as_role, db, lot):
        from app.modules.barcode.models import MaterialReservation
        db.add(MaterialReservation(material_lot_id=uuid.UUID(lot["lot_id"]),
                                   qty=Decimal("380"), status="active"))
        await db.commit()
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.patch(
            f"{API}/materials/lots/{lot['lot_id']}/adjust",
            json={"delta": -100, "reason": "stocktake"})
        assert r.status_code == 409

    @pytest.mark.parametrize("role", [UserRole.CUTTING_MANAGER,
                                      UserRole.STORE_MANAGER])
    async def test_only_receivers_may_correct_stock(
            self, api_client, as_role, lot, role):
        as_role(role)
        r = await api_client.patch(
            f"{API}/materials/lots/{lot['lot_id']}/adjust",
            json={"delta": 1, "reason": "found one"})
        assert r.status_code == 403

    async def test_retiring_a_lot_while_stock_is_reserved_is_409(
            self, api_client, as_role, db, lot):
        """The 409 guard runs BEFORE the retire itself, so this path is
        reachable — see the OPEN DEFECT note on the happy path below."""
        from app.modules.barcode.models import MaterialReservation
        db.add(MaterialReservation(material_lot_id=uuid.UUID(lot["lot_id"]),
                                   qty=Decimal("10"), status="active"))
        await db.commit()
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.delete(f"{API}/materials/lots/{lot['lot_id']}")
        assert r.status_code == 409 and "still reserved" in r.text

    async def test_retiring_an_unknown_lot_is_404(self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.delete(f"{API}/materials/lots/{uuid.uuid4()}")
        assert r.status_code == 404

    async def test_retiring_a_lot_returns_its_retirement_summary(
            self, api_client, as_role, lot):
        """REGRESSION: this route used to be an unconditional 500 — see the
        note above TestRetireLot in
        tests/integration/test_materials_service_full.py."""
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.delete(f"{API}/materials/lots/{lot['lot_id']}")
        assert r.status_code == 200, r.text
        assert r.json()["is_active"] is False
        assert r.json()["barcode_retired"] is True


class TestStockAndReceiving:
    async def test_the_stock_check_reports_the_three_numbers(
            self, api_client, as_role, lot):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/materials/stock",
                                 params={"category": "LEATHER"})
        assert r.status_code == 200, r.text
        assert r.json()["on_hand"] == 400.0
        assert r.json()["available"] == 400.0
        assert r.json()["lot_count"] == 1

    async def test_a_shortfall_comes_back_with_a_suggested_supplier(
            self, api_client, as_role, db, lot):
        db.add(MaterialSupplier(name="TANNERY SRL", articles="SUEDE-A32",
                                is_active=True))
        await db.commit()
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.get(f"{API}/materials/stock", params={
            "category": "LEATHER", "article": "SUEDE-A32", "required": 600})
        assert r.json()["short_by"] == 200.0
        assert r.json()["suggested_supplier"]["name"] == "TANNERY SRL"

    @pytest.mark.parametrize("role", [
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.HR,
        UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER,
        UserRole.LINING_MANAGER, UserRole.SECURITY, UserRole.STORE_MANAGER])
    async def test_the_whole_floor_may_read_stock(
            self, api_client, as_role, role):
        as_role(role)
        r = await api_client.get(f"{API}/materials/stock",
                                 params={"category": "LEATHER"})
        assert r.status_code == 200

    @pytest.mark.parametrize("role", [UserRole.CLIENT, UserRole.VIEWER])
    async def test_clients_and_viewers_may_not(self, api_client, as_role, role):
        as_role(role)
        r = await api_client.get(f"{API}/materials/stock")
        assert r.status_code == 403

    async def test_a_delivery_is_received_into_stock(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 100, "rejected_qty": 5})
        assert r.status_code == 200, r.text
        assert r.json()["on_hand"] == 500.0
        assert r.json()["rejected_logged"] == 5.0
        assert r.json()["substituted"] is False

    async def test_total_qty_works_out_the_rejected_split(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "total_qty": 105, "approved_qty": 100})
        assert r.status_code == 200, r.text
        assert r.json()["total_qty"] == 105.0
        assert r.json()["approved_qty"] == 100.0
        assert r.json()["rejected_logged"] == 5.0
        assert r.json()["on_hand"] == 500.0

    async def test_a_split_that_does_not_add_up_to_the_total_is_422(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "total_qty": 100,
            "approved_qty": 100, "rejected_qty": 5})
        assert r.status_code == 422 and "total_qty" in r.text

    async def test_approving_more_than_arrived_is_422(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "total_qty": 50, "approved_qty": 60})
        assert r.status_code == 422

    async def test_a_sheet_count_mismatch_is_warned_not_refused(
            self, api_client, as_role, lot):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "total_qty": 90, "approved_qty": 90,
            "sheet_count": 3, "sheets": [{"dcm": 43}, {"dcm": 47}]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sheet_count"] == 3 and body["sheets_entered"] == 2
        assert [w["kind"] for w in body["warnings"]] == ["sheet_count_mismatch"]

    async def test_a_receipt_split_is_corrected_and_stock_follows(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "total_qty": 105, "approved_qty": 100})
        receipt_id = r.json()["receipt_id"]
        assert receipt_id

        # 10 of the "approved" were actually bad: approved 90, rejected 15.
        r = await api_client.patch(f"{API}/materials/receipts/{receipt_id}", json={
            "approved_qty": 90, "rejected_qty": 15, "reason": "QC recount"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["approved_qty"] == 90.0 and body["rejected_qty"] == 15.0
        assert body["total_qty"] == 105.0
        assert body["stock_delta"] == -10.0
        assert body["on_hand"] == 490.0
        assert body["before"]["approved_qty"] == 100.0

        hist = (await api_client.get(
            f"{API}/materials/lots/{lot['lot_id']}/history")).json()
        row = next(x for x in hist["receipts"] if x["receipt_id"] == receipt_id)
        assert row["approved_qty"] == 90.0 and row["rejected_qty"] == 15.0

    async def test_a_receipt_correction_with_total_works_out_rejected(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        rid = (await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 100,
            "rejected_qty": 5})).json()["receipt_id"]
        r = await api_client.patch(f"{API}/materials/receipts/{rid}", json={
            "total_qty": 110, "approved_qty": 104, "reason": "typo at receiving"})
        assert r.status_code == 200, r.text
        assert r.json()["rejected_qty"] == 6.0
        assert r.json()["stock_delta"] == 4.0

    async def test_a_receipt_correction_needs_a_reason(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        rid = (await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 10})).json()["receipt_id"]
        r = await api_client.patch(f"{API}/materials/receipts/{rid}", json={
            "approved_qty": 8})
        assert r.status_code == 422

    async def test_a_receipt_correction_cannot_take_stock_negative(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        rid = (await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 10})).json()["receipt_id"]
        # Everything on the shelf goes out first.
        await api_client.patch(f"{API}/materials/lots/{lot['lot_id']}/adjust",
                               json={"delta": -410, "reason": "count"})
        r = await api_client.patch(f"{API}/materials/receipts/{rid}", json={
            "approved_qty": 0, "reason": "all bad"})
        assert r.status_code == 409

    async def test_an_unknown_receipt_is_404(self, api_client, as_role):
        as_role(UserRole.HR)
        r = await api_client.patch(
            f"{API}/materials/receipts/{uuid.uuid4()}",
            json={"approved_qty": 1, "reason": "whatever"})
        assert r.status_code == 404

    async def test_receive_without_a_split_is_a_pending_arrival(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "total_qty": 100, "sheet_count": 3})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "PENDING"
        assert "approved_qty" in body["outstanding"]
        assert body["approved_qty"] == 0.0       # nothing approved before QC
        assert body["total_qty"] == 100.0
        assert body["on_hand"] == 500.0          # provisional, cuttable
        assert body["pending_arrivals"] == 1
        assert body["pending_arrival_qty"] == 100.0
        rid = body["receipt_id"]

        queue = (await api_client.get(f"{API}/materials/arrivals")).json()
        assert rid in {a["receipt_id"] for a in queue["arrivals"]}

        # The split arrives later — 90 approved, 10 rejected.
        r = await api_client.patch(f"{API}/materials/arrivals/{rid}", json={
            "article": lot["article"], "approved_qty": 90, "rejected_qty": 10,
            "declared_sheet_count": 4})
        assert r.status_code == 200, r.text
        row = r.json()
        assert row["status"] == "COMPLETED"
        assert row["approved_qty"] == 90.0 and row["rejected_qty"] == 10.0
        assert row["declared_sheet_count"] == 4
        assert row["on_hand_delta"] == -10.0
        lot_now = (await api_client.get(
            f"{API}/materials/lots/{lot['lot_id']}")).json()
        assert lot_now["on_hand"] == 490.0

    async def test_arrival_patch_with_only_rejected_works_out_approved(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        rid = (await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "total_qty": 100})).json()["receipt_id"]
        r = await api_client.patch(f"{API}/materials/arrivals/{rid}",
                                   json={"rejected_qty": 25})
        assert r.status_code == 200, r.text
        assert r.json()["approved_qty"] == 75.0
        assert r.json()["status"] == "COMPLETED"

    async def test_arrival_patch_refuses_a_different_colour(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        rid = (await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "total_qty": 100})).json()["receipt_id"]
        r = await api_client.patch(f"{API}/materials/arrivals/{rid}", json={
            "colour": "NOT-THIS-COLOUR", "approved_qty": 90})
        assert r.status_code == 422 and "DELETE" in r.text

    async def test_receive_with_neither_approved_nor_total_is_422(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"]})
        assert r.status_code == 422

    async def test_negative_quantities_never_reach_the_service(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": -1})
        assert r.status_code == 422

    async def test_a_po_mismatch_is_409_and_nothing_enters_stock(
            self, api_client, as_role, db, lot):
        order = SupplierOrder(category="LEATHER", article="NAP-11",
                              qty=Decimal("100"), uom="dcm", status="ordered")
        db.add(order)
        await db.commit()
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 100,
            "supplier_order_id": str(order.id)})
        assert r.status_code == 409 and "approve_mismatch=true" in r.text

    async def test_hr_cannot_approve_a_substitution(
            self, api_client, as_role, db, lot):
        """Widening the receiving door to HR deliberately does not widen the
        mismatch approval — that one is a costing decision."""
        order = SupplierOrder(category="LEATHER", article="NAP-11",
                              qty=Decimal("100"), uom="dcm", status="ordered")
        db.add(order)
        await db.commit()
        as_role(UserRole.HR)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 100,
            "supplier_order_id": str(order.id), "approve_mismatch": True})
        assert r.status_code == 409

    async def test_a_dm_may_approve_it_into_a_substitute_lot(
            self, api_client, as_role, db, lot):
        order = SupplierOrder(category="LEATHER", article="NAP-11",
                              qty=Decimal("100"), uom="dcm", status="ordered")
        db.add(order)
        await db.commit()
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 100,
            "supplier_order_id": str(order.id), "approve_mismatch": True})
        assert r.status_code == 200, r.text
        assert r.json()["substituted"] is True
        assert r.json()["lot_id"] != lot["lot_id"]
        assert r.json()["mismatch_fields"] == ["article"]

    async def test_a_sheeted_delivery_returns_its_hide_labels(
            self, api_client, as_role, lot):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 90,
            "sheets": [{"dcm": 43}, {"dcm": 47}]})
        assert r.status_code == 200, r.text
        assert len(r.json()["sheets"]) == 2
        # The block compares the LOT's whole on_hand against ALL of its hides,
        # not this delivery against its own. The lot opened with 400 unsheeted
        # dcm, so the honest answer is a 400 difference — that is the figure
        # the store is meant to see, and it is reported rather than enforced.
        rec = r.json()["sheet_reconciliation"]
        assert rec["sheets_total"] == 2
        assert rec["sheet_dcm_in_store"] == 90.0
        assert rec["lot_on_hand"] == 490.0
        assert rec["difference"] == 400.0 and rec["reconciled"] is False

    async def test_a_hide_with_no_measurement_is_refused_at_the_boundary(
            self, api_client, as_role, lot):
        """`dcm: float = Field(gt=0)` on SheetIn."""
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 10,
            "sheets": [{"dcm": 0}]})
        assert r.status_code == 422

    @pytest.mark.parametrize("role", [UserRole.CUTTING_MANAGER,
                                      UserRole.STORE_MANAGER])
    async def test_the_floor_may_not_receive(self, api_client, as_role, lot, role):
        as_role(role)
        r = await api_client.post(f"{API}/materials/receive", json={
            "lot_id": lot["lot_id"], "approved_qty": 1})
        assert r.status_code == 403


class TestSupplierOrderEndpoints:
    async def test_an_order_is_raised_at_201(self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/suppliers/orders", json={
            "category": "LEATHER", "article": "SUEDE-A32", "qty": 400})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "ordered" and r.json()["uom"] == "dcm"

    async def test_a_zero_quantity_never_reaches_the_service(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/suppliers/orders", json={
            "category": "LEATHER", "article": "X", "qty": 0})
        assert r.status_code == 422

    async def test_a_supplier_that_does_not_carry_the_article_is_422(
            self, api_client, as_role, db):
        sup = MaterialSupplier(name="TRIMS", articles="BTN-4H", is_active=True)
        db.add(sup)
        await db.commit()
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/suppliers/orders", json={
            "category": "LEATHER", "article": "SUEDE-A32", "qty": 1,
            "supplier_id": str(sup.id)})
        assert r.status_code == 422 and "does not supply" in r.text

    async def test_an_order_is_flipped_to_arrived(self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        created = await api_client.post(f"{API}/suppliers/orders", json={
            "category": "LEATHER", "article": "X", "qty": 1})
        oid = created.json()["order_id"]
        r = await api_client.patch(f"{API}/suppliers/orders/{oid}",
                                   json={"status": "arrived"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "arrived"
        assert r.json()["arrived_at"] is not None

    async def test_only_arrived_is_an_accepted_transition(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        created = await api_client.post(f"{API}/suppliers/orders", json={
            "category": "LEATHER", "article": "X", "qty": 1})
        r = await api_client.patch(
            f"{API}/suppliers/orders/{created.json()['order_id']}",
            json={"status": "cancelled"})
        assert r.status_code == 422

    async def test_an_ordered_spec_is_corrected(self, api_client, as_role):
        as_role(UserRole.MANAGING_DIRECTOR)
        created = await api_client.post(f"{API}/suppliers/orders", json={
            "category": "LEATHER", "article": "SUEDE-A32", "qty": 400})
        oid = created.json()["order_id"]
        r = await api_client.patch(f"{API}/suppliers/orders/{oid}/spec",
                                   json={"article": "NAP-11", "qty": 380})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ordered"

    async def test_an_arrived_orders_spec_is_frozen(self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        created = await api_client.post(f"{API}/suppliers/orders", json={
            "category": "LEATHER", "article": "X", "qty": 1})
        oid = created.json()["order_id"]
        await api_client.patch(f"{API}/suppliers/orders/{oid}",
                               json={"status": "arrived"})
        r = await api_client.patch(f"{API}/suppliers/orders/{oid}/spec",
                                   json={"article": "Y"})
        assert r.status_code == 409

    async def test_an_unknown_order_is_404_on_both_writes(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        ghost = uuid.uuid4()
        assert (await api_client.patch(
            f"{API}/suppliers/orders/{ghost}",
            json={"status": "arrived"})).status_code == 404
        assert (await api_client.patch(
            f"{API}/suppliers/orders/{ghost}/spec",
            json={"article": "Y"})).status_code == 404

    @pytest.mark.parametrize("role", [UserRole.HR, UserRole.CUTTING_MANAGER,
                                      UserRole.STORE_MANAGER])
    async def test_supplier_orders_are_dm_and_md_only(
            self, api_client, as_role, role):
        as_role(role)
        r = await api_client.post(f"{API}/suppliers/orders", json={
            "category": "LEATHER", "article": "X", "qty": 1})
        assert r.status_code == 403


class TestReportEndpoints:
    async def test_the_arrived_consumed_available_report_is_paged(
            self, api_client, as_role, lot):
        as_role(UserRole.HR)
        r = await api_client.get(f"{API}/materials/leather-by-style")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 0        # nothing cut yet
        assert r.json()["items"] == []

    async def test_one_garments_consumption_is_readable_by_id(
            self, api_client, as_role, db, pieces, order_tree, operations, lot):
        from datetime import date
        from app.modules.production.models import ProductionEvent
        db.add(ProductionEvent(
            sku_id=order_tree["sku"].id, piece_id=pieces[0].id,
            operation_id=operations["LEATHER_CUTTING"].id,
            work_date=date.today(),
            leather_lot_id=uuid.UUID(lot["lot_id"]),
            consumption_qty=Decimal("12.5")))
        await db.commit()

        as_role(UserRole.CUTTING_MANAGER)
        r = await api_client.get(
            f"{API}/materials/pieces/{pieces[0].id}/consumption")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 12.5
        assert r.json()["events"][0]["stage"] == "LEATHER_CUTTING"

    async def test_a_garment_nobody_cut_reports_zero_not_404(
            self, api_client, as_role, pieces):
        as_role(UserRole.HR)
        r = await api_client.get(
            f"{API}/materials/pieces/{pieces[0].id}/consumption")
        assert r.status_code == 200 and r.json()["total"] == 0


# ══════════════════════════════════════════════════════════════════════════
# THE STYLE MATERIAL SPEC  —  /styles/{id}/material-spec
# ══════════════════════════════════════════════════════════════════════════
class TestMaterialSpecEndpoints:
    async def test_the_grid_is_saved_in_one_put(
            self, api_client, as_role, draft_style):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.put(
            f"{API}/styles/{draft_style.id}/material-spec",
            json={"lines": [LEATHER_SPEC_LINE, BUTTON_SPEC_LINE]})
        assert r.status_code == 200, r.text
        assert len(r.json()["lines"]) == 2
        assert r.json()["editable"] is True

    async def test_the_grid_is_read_back_with_its_release_blockers(
            self, api_client, as_role, draft_style):
        as_role(UserRole.DIRECT_MANAGER)
        await api_client.put(f"{API}/styles/{draft_style.id}/material-spec",
                             json={"lines": [LEATHER_SPEC_LINE]})
        r = await api_client.get(f"{API}/styles/{draft_style.id}/material-spec")
        assert r.status_code == 200, r.text
        assert r.json()["confirmed"] is False
        assert r.json()["release_blockers"]

    @pytest.mark.parametrize("role", [
        UserRole.CUTTING_MANAGER, UserRole.STORE_MANAGER, UserRole.HR,
        UserRole.STITCHING_MANAGER])
    async def test_the_whole_floor_may_READ_the_recipe(
            self, api_client, as_role, draft_style, role):
        """The cutting manager needs the dcm and the store manager needs the
        accessory list."""
        as_role(role)
        r = await api_client.get(f"{API}/styles/{draft_style.id}/material-spec")
        assert r.status_code == 200

    @pytest.mark.parametrize("role", [UserRole.CUTTING_MANAGER,
                                      UserRole.STORE_MANAGER, UserRole.HR])
    async def test_only_dm_and_md_may_WRITE_it(
            self, api_client, as_role, draft_style, role):
        as_role(role)
        r = await api_client.put(f"{API}/styles/{draft_style.id}/material-spec",
                                 json={"lines": [LEATHER_SPEC_LINE]})
        assert r.status_code == 403

    async def test_one_line_is_added_at_201(
            self, api_client, as_role, draft_style):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(
            f"{API}/styles/{draft_style.id}/material-spec/lines",
            json=BUTTON_SPEC_LINE)
        assert r.status_code == 201, r.text
        assert r.json()["article"] == "BTN-4H" and r.json()["uom"] == "pcs"

    async def test_adding_the_same_material_twice_is_409(
            self, api_client, as_role, draft_style):
        as_role(UserRole.DIRECT_MANAGER)
        await api_client.post(
            f"{API}/styles/{draft_style.id}/material-spec/lines",
            json=BUTTON_SPEC_LINE)
        r = await api_client.post(
            f"{API}/styles/{draft_style.id}/material-spec/lines",
            json=BUTTON_SPEC_LINE)
        assert r.status_code == 409

    async def test_one_line_is_patched_and_omitted_fields_survive(
            self, api_client, as_role, draft_style):
        as_role(UserRole.DIRECT_MANAGER)
        created = await api_client.post(
            f"{API}/styles/{draft_style.id}/material-spec/lines",
            json=BUTTON_SPEC_LINE)
        line_id = created.json()["line_id"]
        r = await api_client.patch(
            f"{API}/styles/{draft_style.id}/material-spec/lines/{line_id}",
            json={"qty_per_piece": 6})
        assert r.status_code == 200, r.text
        assert r.json()["qty_per_piece"] == 6.0
        assert r.json()["article"] == "BTN-4H"

    async def test_one_line_is_removed_softly(
            self, api_client, as_role, draft_style):
        as_role(UserRole.DIRECT_MANAGER)
        created = await api_client.post(
            f"{API}/styles/{draft_style.id}/material-spec/lines",
            json=BUTTON_SPEC_LINE)
        r = await api_client.delete(
            f"{API}/styles/{draft_style.id}/material-spec/lines/"
            f"{created.json()['line_id']}")
        assert r.status_code == 200, r.text
        assert r.json()["deactivated"] is True

    async def test_an_unknown_line_is_404_on_patch_and_delete(
            self, api_client, as_role, draft_style):
        as_role(UserRole.DIRECT_MANAGER)
        ghost = uuid.uuid4()
        assert (await api_client.patch(
            f"{API}/styles/{draft_style.id}/material-spec/lines/{ghost}",
            json={"qty_per_piece": 1})).status_code == 404
        assert (await api_client.delete(
            f"{API}/styles/{draft_style.id}/material-spec/lines/{ghost}"
        )).status_code == 404

    async def test_the_sign_off_clears_the_release_gate(
            self, api_client, as_role, draft_style):
        as_role(UserRole.DIRECT_MANAGER)
        await api_client.put(f"{API}/styles/{draft_style.id}/material-spec",
                             json={"lines": [LEATHER_SPEC_LINE]})
        r = await api_client.post(
            f"{API}/styles/{draft_style.id}/material-spec/confirm",
            json={"no_accessories": True})
        assert r.status_code == 200, r.text
        assert r.json()["confirmed"] is True
        assert r.json()["release_blockers"] == []

    async def test_declaring_no_accessories_while_they_exist_is_422(
            self, api_client, as_role, draft_style):
        as_role(UserRole.DIRECT_MANAGER)
        await api_client.put(
            f"{API}/styles/{draft_style.id}/material-spec",
            json={"lines": [LEATHER_SPEC_LINE, BUTTON_SPEC_LINE]})
        r = await api_client.post(
            f"{API}/styles/{draft_style.id}/material-spec/confirm",
            json={"no_accessories": True})
        assert r.status_code == 422

    async def test_a_recipe_is_copied_from_another_style_at_201(
            self, api_client, as_role, db, draft_style, order_tree):
        from app.modules.clients.models import Style
        source = Style(client_order_id=order_tree["order"].id, name="SOURCE",
                       article="SRC", production_status="DRAFT", code="SRC-1")
        db.add(source)
        await db.commit()
        as_role(UserRole.DIRECT_MANAGER)
        await api_client.put(f"{API}/styles/{source.id}/material-spec",
                             json={"lines": [LEATHER_SPEC_LINE]})
        r = await api_client.post(
            f"{API}/styles/{draft_style.id}/material-spec/copy-from",
            json={"source_style_id": str(source.id)})
        assert r.status_code == 201, r.text
        assert r.json()["copied"] == 1

    async def test_the_requirement_screen_projects_the_order(
            self, api_client, as_role, draft_style, accessory_lot):
        as_role(UserRole.DIRECT_MANAGER)
        await api_client.put(f"{API}/styles/{draft_style.id}/material-spec",
                             json={"lines": [BUTTON_SPEC_LINE]})
        r = await api_client.get(
            f"{API}/styles/{draft_style.id}/material-spec/requirement")
        assert r.status_code == 200, r.text
        assert r.json()["qty_ordered"] == 5
        assert r.json()["lines"][0]["total_required"] == 20.0

    async def test_a_released_styles_leather_line_is_frozen_at_409(
            self, api_client, as_role, order_tree):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(
            f"{API}/styles/{order_tree['style'].id}/material-spec/lines",
            json=LEATHER_SPEC_LINE)
        assert r.status_code == 409 and "/materials/issues" in r.text

    async def test_an_accessory_line_stays_correctable_after_release(
            self, api_client, as_role, order_tree):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(
            f"{API}/styles/{order_tree['style'].id}/material-spec/lines",
            json=BUTTON_SPEC_LINE)
        assert r.status_code == 201, r.text

    async def test_an_unknown_style_is_404_on_every_spec_route(
            self, api_client, as_role):
        as_role(UserRole.DIRECT_MANAGER)
        ghost = uuid.uuid4()
        assert (await api_client.get(
            f"{API}/styles/{ghost}/material-spec")).status_code == 404
        assert (await api_client.get(
            f"{API}/styles/{ghost}/material-spec/requirement")).status_code == 404
        assert (await api_client.post(
            f"{API}/styles/{ghost}/material-spec/confirm",
            json={"no_accessories": True})).status_code == 404


# ══════════════════════════════════ POST /materials/issues
class TestManualIssueEndpoint:
    async def test_a_correction_may_be_recorded_by_barcode_alone(
            self, api_client, as_role, db, pieces, accessory_lot, cutter):
        """Both doors take barcodes, so a store operator never types an id.

        REGRESSION: the router called `resolve_actor(employee_barcode=...)` and
        omitted its other keyword-only argument, so this — the endpoint's
        PRIMARY door — was a TypeError -> 500 while the employee_id door beside
        it worked. The companion test below is what made the split visible.
        """
        code = await db.scalar(select(BarcodeRegistry.code).where(
            BarcodeRegistry.material_lot_id == uuid.UUID(
                accessory_lot["lot_id"])))
        as_role(UserRole.STORE_MANAGER)
        r = await api_client.post(f"{API}/materials/issues", json={
            "piece_barcode": pieces[0].code, "lot_barcode": code,
            "employee_barcode": cutter[1].code, "qty": 2,
            "note": "wrong button on the sheet"})
        assert r.status_code == 201, r.text
        assert r.json()["source"] == "MANUAL"
        assert r.json()["available_after"] == 498.0

    async def test_a_correction_with_an_employee_ID_works_today(
            self, api_client, as_role, pieces, accessory_lot, cutter):
        """The SAME endpoint, reached by the other door. This is what proves the
        xfail above is about the barcode branch specifically and not about the
        endpoint being broken outright."""
        as_role(UserRole.STORE_MANAGER)
        r = await api_client.post(f"{API}/materials/issues", json={
            "piece_barcode": pieces[0].code,
            "material_lot_id": accessory_lot["lot_id"],
            "employee_id": str(cutter[0].id), "qty": 2})
        assert r.status_code == 201, r.text
        assert r.json()["available_after"] == 498.0

    async def test_ids_are_accepted_in_place_of_barcodes(
            self, api_client, as_role, pieces, accessory_lot):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/materials/issues", json={
            "piece_id": str(pieces[0].id),
            "material_lot_id": accessory_lot["lot_id"], "qty": 1})
        assert r.status_code == 201, r.text

    async def test_naming_no_garment_is_422(
            self, api_client, as_role, accessory_lot):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/materials/issues", json={
            "material_lot_id": accessory_lot["lot_id"], "qty": 1})
        assert r.status_code == 422
        assert "piece_barcode or piece_id" in r.text

    async def test_naming_no_material_is_422(self, api_client, as_role, pieces):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/materials/issues", json={
            "piece_id": str(pieces[0].id), "qty": 1})
        assert r.status_code == 422
        assert "lot_barcode or material_lot_id" in r.text

    async def test_an_unknown_piece_barcode_is_404(
            self, api_client, as_role, accessory_lot):
        as_role(UserRole.DIRECT_MANAGER)
        r = await api_client.post(f"{API}/materials/issues", json={
            "piece_barcode": "NOPE",
            "material_lot_id": accessory_lot["lot_id"], "qty": 1})
        assert r.status_code == 404

    @pytest.mark.parametrize("role", [UserRole.HR, UserRole.CUTTING_MANAGER,
                                      UserRole.SUPERVISOR])
    async def test_only_dm_md_and_the_store_may_record_one(
            self, api_client, as_role, pieces, accessory_lot, role):
        as_role(role)
        r = await api_client.post(f"{API}/materials/issues", json={
            "piece_id": str(pieces[0].id),
            "material_lot_id": accessory_lot["lot_id"], "qty": 1})
        assert r.status_code == 403
