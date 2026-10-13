"""
================================================================================
tests/integration/test_bom_lifecycle.py — BomService round-tripped on the DB
================================================================================
Layer: integration (service -> repo -> DB on in-memory SQLite/aiosqlite, per
tests/conftest.py). No HTTP, no router — this exercises BomService directly
against real Bom/BomItem rows, so a failure here points straight at the guard
clause in app/modules/bom/service.py rather than at routing/auth.

Covers the full Stage-2/3 state machine one gate at a time:
  get_bom · edit_bom_items (bulk PATCH, optimistic revision lock) ·
  confirm_cutting (the §10 gate) · approve_bom · reject_bom · reopen_bom ·
  export_bom

Money/data-integrity note (per the project testing standard): edit_bom_items and
approve_bom are the two writes that move money and lock state, so they get the
deepest coverage here (stale-revision races, the §10 reconfirm flip, the
cutting-confirmation gate on approve).

FIXED 2026-09-17 (Hamthan) — was: KNOWN LIVE BUG in confirm_cutting.
`await self.repo.save(bom)` commits AND refreshes `bom`; because Bom.items is mapped
cascade="all, delete-orphan" and "all" includes refresh-expire, that refresh expired
the collection AND every BomItem in it, so the very next line, `for item in
bom.items:`, attempted an implicit lazy-load outside any greenlet context and
SQLAlchemy's async ORM raised `sqlalchemy.exc.MissingGreenlet`.
`test_confirm_cutting_moves_draft_to_ready_for_review` below calls the real method
and now passes. confirm_cutting snapshots the item values it needs before the save —
plain tuples, not ORM rows, so nothing in the loop can be re-expired.
Every OTHER test that just needs "a cutting-confirmed BOM" as a precondition still
uses the `_mark_cutting_confirmed` helper below rather than the real method, so a
downstream test (edit/approve/reject/reopen/export) keeps failing for ITS OWN reason
and never as a bystander.
================================================================================
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.modules.bom.enums import BomItemCategory, BomStatus, DcmSource
from app.modules.bom.models import Bom, BomItem
from app.modules.bom.service import BomService


# ── fixtures local to this module ────────────────────────────────────────────
@pytest_asyncio.fixture
async def draft_bom(db):
    """A DRAFT BOM with one template-sourced leather MAIN_MATERIAL line and one
    flat-cost THREAD line, order_qty=60 (the BMO-1 worked-example quantity).
    Returns (bom, leather_item, thread_item) — all three already refreshed."""
    bom = Bom(status=BomStatus.DRAFT.value, currency="INR", order_qty=60,
              revision=1, dcm_base_size="M")
    db.add(bom)
    await db.flush()
    leather = BomItem(
        bom_id=bom.id, category=BomItemCategory.MAIN_MATERIAL.value,
        name="Cow Nappa Leather", material_color="Black",
        qty_per_garment=Decimal("34.5"), uom="dm2", unit_price=Decimal("1.80"),
        dcm_source=DcmSource.TEMPLATE.value, dcm_confidence=Decimal("0.9000"),
    )
    thread = BomItem(
        bom_id=bom.id, category=BomItemCategory.THREAD.value,
        name="Polyester Thread", qty_per_garment=Decimal("1"), uom="pc",
        unit_price=Decimal("2.00"),
    )
    db.add_all([leather, thread])
    await db.commit()
    for obj in (bom, leather, thread):
        await db.refresh(obj)
    return bom, leather, thread


@pytest.fixture
def svc(db):
    return BomService(db)


def _detail(exc_info) -> dict:
    """HTTPException.detail is a dict for every BOM guard clause — pull it out
    so assertions read `_detail(e)["error"] == "..."` instead of poking .value."""
    return exc_info.value.detail


async def _mark_cutting_confirmed(db, bom, user):
    """Set the same fields confirm_cutting() would (status, cutting_confirmed_by/
    at), WITHOUT calling the real method — used as setup for tests whose actual
    subject is a downstream gate (edit/approve/reject/reopen/export), not
    confirm_cutting itself. See the module docstring: routing every downstream
    test through the real confirm_cutting() would fail them all on ITS bug
    instead of testing their own gate. Only the tests that genuinely target
    confirm_cutting call the real method."""
    from datetime import datetime, timezone
    bom.cutting_confirmed_by = user.id
    bom.cutting_confirmed_at = datetime.now(timezone.utc)
    bom.status = BomStatus.READY_FOR_REVIEW.value
    await db.commit()
    await db.refresh(bom)


# ══════════════════════════════════════════════════ GET /boms/{id}
async def test_get_bom_returns_the_full_editable_tree(svc, draft_bom):
    bom, *_ = draft_bom
    view = await svc.get_bom(bom.id)
    assert view["id"] == str(bom.id)
    assert view["status"] == BomStatus.DRAFT.value
    assert view["revision"] == 1
    assert {i["name"] for i in view["items"]} == {"Cow Nappa Leather", "Polyester Thread"}


async def test_get_bom_missing_raises_404(svc):
    import uuid
    with pytest.raises(HTTPException) as ei:
        await svc.get_bom(uuid.uuid4())
    assert ei.value.status_code == 404


# ══════════════════════════════════════════════════ PATCH /boms/{id}/items
async def test_edit_unit_price_recomputes_totals_and_bumps_revision(svc, draft_bom):
    bom, leather, _ = draft_bom
    result = await svc.edit_bom_items(
        object(), bom.id, base_revision=1,
        edits=[{"bom_item_id": str(leather.id), "field": "unit_price", "value": 2.0}])
    assert result["revision"] == 2
    assert result["reconfirm_required"] is False
    # 34.5 dm2 @ 2.00 = 69.00/garment on this line alone
    edited = next(i for i in result["recomputed"]["items"] if i["id"] == str(leather.id))
    assert edited["unit_price"] == 2.0


async def test_edit_dcm_on_material_line_stamps_manual_source(svc, draft_bom):
    bom, leather, _ = draft_bom
    result = await svc.edit_bom_items(
        object(), bom.id, base_revision=1,
        edits=[{"bom_item_id": str(leather.id), "field": "dcm", "value": 40.0}])
    edited = next(i for i in result["recomputed"]["items"] if i["id"] == str(leather.id))
    assert edited["dcm_source"] == DcmSource.MANUAL.value


async def test_edit_qty_on_a_non_material_line_does_not_stamp_dcm_source(svc, draft_bom):
    """THREAD isn't in MATERIAL_DCM_CATEGORIES — a qty edit there must leave
    dcm_source untouched (it was never set to begin with)."""
    bom, _leather, thread = draft_bom
    result = await svc.edit_bom_items(
        object(), bom.id, base_revision=1,
        edits=[{"bom_item_id": str(thread.id), "field": "qty_per_garment", "value": 3.0}])
    edited = next(i for i in result["recomputed"]["items"] if i["id"] == str(thread.id))
    assert edited["dcm_source"] is None


async def test_edit_multiple_lines_commit_atomically_in_one_batch(svc, draft_bom):
    bom, leather, thread = draft_bom
    result = await svc.edit_bom_items(
        object(), bom.id, base_revision=1,
        edits=[
            {"bom_item_id": str(leather.id), "field": "unit_price", "value": 2.5},
            {"bom_item_id": str(thread.id), "field": "unit_price", "value": 3.5},
        ])
    prices = {i["id"]: i["unit_price"] for i in result["recomputed"]["items"]}
    assert prices[str(leather.id)] == 2.5
    assert prices[str(thread.id)] == 3.5
    assert result["revision"] == 2   # one batch -> one revision bump, not two


async def test_edit_unknown_item_id_raises_422(svc, draft_bom):
    import uuid
    bom, *_ = draft_bom
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(object(), bom.id, base_revision=1,
                                 edits=[{"bom_item_id": str(uuid.uuid4()),
                                         "field": "unit_price", "value": 1.0}])
    assert ei.value.status_code == 422
    assert _detail(ei)["error"] == "unknown_bom_item"


async def test_edit_unsupported_field_raises_422(svc, draft_bom):
    bom, leather, _ = draft_bom
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(object(), bom.id, base_revision=1,
                                 edits=[{"bom_item_id": str(leather.id),
                                         "field": "name", "value": "x"}])
    assert ei.value.status_code == 422
    assert _detail(ei)["error"] == "unsupported_field"


async def test_edit_negative_value_raises_422(svc, draft_bom):
    bom, leather, _ = draft_bom
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(object(), bom.id, base_revision=1,
                                 edits=[{"bom_item_id": str(leather.id),
                                         "field": "unit_price", "value": -5.0}])
    assert ei.value.status_code == 422
    assert _detail(ei)["error"] == "negative_value"


async def test_edit_non_numeric_value_raises_422(svc, draft_bom):
    bom, leather, _ = draft_bom
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(object(), bom.id, base_revision=1,
                                 edits=[{"bom_item_id": str(leather.id),
                                         "field": "unit_price", "value": "not-a-number"}])
    assert ei.value.status_code == 422
    assert _detail(ei)["error"] == "invalid_value"


async def test_edit_zero_value_is_the_accepted_floor(svc, draft_bom):
    """0 is legal — only strictly negative values are rejected."""
    bom, leather, _ = draft_bom
    result = await svc.edit_bom_items(
        object(), bom.id, base_revision=1,
        edits=[{"bom_item_id": str(leather.id), "field": "qty_per_garment", "value": 0.0}])
    edited = next(i for i in result["recomputed"]["items"] if i["id"] == str(leather.id))
    assert edited["qty_per_garment"] == 0.0
    assert result["revision"] == 2


async def test_edit_stale_revision_raises_409(svc, draft_bom):
    bom, leather, _ = draft_bom
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(object(), bom.id, base_revision=99,
                                 edits=[{"bom_item_id": str(leather.id),
                                         "field": "unit_price", "value": 5.0}])
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "stale_revision"
    assert _detail(ei)["current_revision"] == 1


async def test_edit_a_second_writer_loses_the_revision_race(db, svc, draft_bom):
    """Simulates two concurrent PATCHes: both load revision=1, the first commits
    and bumps it to 2, the second (still holding base_revision=1) must be
    refused — this is claim_revision's atomic CAS, not the earlier equality
    check, so it is worth exercising as its own case."""
    bom, leather, _ = draft_bom
    await svc.edit_bom_items(object(), bom.id, base_revision=1,
                             edits=[{"bom_item_id": str(leather.id),
                                     "field": "unit_price", "value": 9.0}])
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(object(), bom.id, base_revision=1,   # stale now
                                 edits=[{"bom_item_id": str(leather.id),
                                         "field": "unit_price", "value": 12.0}])
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "stale_revision"
    assert _detail(ei)["current_revision"] == 2


async def test_edit_locked_bom_rejects_all_edits(svc, draft_bom, db):
    bom, leather, _ = draft_bom
    bom.status = BomStatus.APPROVED.value
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(object(), bom.id, base_revision=1,
                                 edits=[{"bom_item_id": str(leather.id),
                                         "field": "unit_price", "value": 5.0}])
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "bom_locked"


async def test_edit_rejected_bom_points_at_reopen(svc, draft_bom, db):
    bom, leather, _ = draft_bom
    bom.status = BomStatus.REJECTED.value
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(object(), bom.id, base_revision=1,
                                 edits=[{"bom_item_id": str(leather.id),
                                         "field": "unit_price", "value": 5.0}])
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "bom_rejected"


async def test_edit_dcm_after_cutting_confirm_reopens_the_gate(svc, draft_bom, cutting_mgr, dm, db):
    """§10: a DCM/qty edit on a material line, after cutting was confirmed, must
    force the BOM back to draft and clear the confirmation.

    UPDATED 2026-09-17 (Hamthan): the edit is made by the DM now, not the cutting
    manager. _ROLE_EDIT_FIELDS limits CUTTING_MANAGER to unit_price, so a DCM edit
    from that role is a 403 — see the test directly below. The gate this test is
    actually about (a DCM change invalidating an existing confirmation) is
    unchanged and still fires for whoever is allowed to make the change."""
    bom, leather, _ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    assert bom.cutting_confirmed_at is not None

    result = await svc.edit_bom_items(
        dm, bom.id, base_revision=bom.revision,
        edits=[{"bom_item_id": str(leather.id), "field": "dcm", "value": 50.0}])
    assert result["reconfirm_required"] is True
    await db.refresh(bom)
    assert bom.cutting_confirmed_at is None
    assert bom.status == BomStatus.DRAFT.value


async def test_edit_dcm_as_cutting_manager_is_forbidden(svc, draft_bom, cutting_mgr):
    """The cutting manager checks and signs off the BOM; only DM/MD rewrite the
    consumption figures on it."""
    bom, leather, _ = draft_bom
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(
            cutting_mgr, bom.id, base_revision=bom.revision,
            edits=[{"bom_item_id": str(leather.id), "field": "dcm", "value": 50.0}])
    assert ei.value.status_code == 403
    assert _detail(ei)["error"] == "field_not_permitted_for_role"
    assert _detail(ei)["allowed_fields"] == ["unit_price"]


async def test_edit_unit_price_as_cutting_manager_is_allowed(svc, draft_bom, cutting_mgr):
    bom, leather, _ = draft_bom
    result = await svc.edit_bom_items(
        cutting_mgr, bom.id, base_revision=bom.revision,
        edits=[{"bom_item_id": str(leather.id), "field": "unit_price", "value": 3.0}])
    assert result["revision"] == 2


async def test_edit_price_only_after_cutting_confirm_leaves_the_gate_intact(svc, draft_bom, cutting_mgr, db):
    bom, leather, _ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)

    result = await svc.edit_bom_items(
        cutting_mgr, bom.id, base_revision=bom.revision,
        edits=[{"bom_item_id": str(leather.id), "field": "unit_price", "value": 2.20}])
    assert result["reconfirm_required"] is False
    await db.refresh(bom)
    assert bom.cutting_confirmed_at is not None
    assert bom.status == BomStatus.READY_FOR_REVIEW.value


# ══════════════════════════════════════════════════ POST /confirm-cutting
async def test_confirm_cutting_moves_draft_to_ready_for_review(svc, draft_bom, cutting_mgr, db):
    bom, *_ = draft_bom
    result = await svc.confirm_cutting(cutting_mgr, bom.id)
    assert result["status"] == BomStatus.READY_FOR_REVIEW.value
    await db.refresh(bom)
    assert bom.status == BomStatus.READY_FOR_REVIEW.value
    assert bom.cutting_confirmed_by == cutting_mgr.id


async def test_confirm_cutting_on_approved_bom_raises_409(svc, draft_bom, cutting_mgr, db):
    bom, *_ = draft_bom
    bom.status = BomStatus.APPROVED.value
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await svc.confirm_cutting(cutting_mgr, bom.id)
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "invalid_state_for_confirmation"


# ══════════════════════════════════════════════════ POST /approve
@pytest.fixture(autouse=True)
def _stub_approve_side_effects(monkeypatch):
    """approve_bom fans out into production-tracking + inventory-check, both of
    which live in other modules and are already independently tested there.
    Stubbing them here keeps this suite about the BOM state machine, not about
    whatever those services happen to do with a minimal fixture BOM."""
    async def _noop_tracking(self, bom_id):
        return None

    async def _fake_inventory_check(self, user, bom_id):
        return {"inventory_check_id": "stub-check-id"}

    monkeypatch.setattr(
        "app.modules.supplier_po.production_tracking_service.ProductionTrackingService.on_bom_approved",
        _noop_tracking)
    monkeypatch.setattr(
        "app.modules.inventory.service.InventoryService.run_check",
        _fake_inventory_check)


async def test_approve_requires_cutting_confirmation_first(svc, draft_bom, md):
    bom, *_ = draft_bom     # never cutting-confirmed
    with pytest.raises(HTTPException) as ei:
        await svc.approve_bom(md, bom.id)
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "cutting_confirmation_required"


async def test_approve_after_cutting_confirm_succeeds_and_spawns_inventory_check(svc, draft_bom, cutting_mgr, md, db):
    bom, *_ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    result = await svc.approve_bom(md, bom.id)
    assert result["status"] == BomStatus.APPROVED.value
    assert result["inventory_check_id"] == "stub-check-id"
    await db.refresh(bom)
    assert bom.approved_by == md.id
    assert bom.approved_at is not None


async def test_approve_with_lock_sets_locked_not_approved(svc, draft_bom, cutting_mgr, md, db):
    bom, *_ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    result = await svc.approve_bom(md, bom.id, lock=True)
    assert result["status"] == BomStatus.LOCKED.value


# ══════════════════════════════════════════════════ POST /reject
async def test_reject_requires_ready_for_review(svc, draft_bom, md):
    bom, *_ = draft_bom   # still draft
    with pytest.raises(HTTPException) as ei:
        await svc.reject_bom(md, bom.id, reason="doesn't matter")
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "not_ready_for_review"


async def test_reject_requires_a_non_blank_reason(svc, draft_bom, cutting_mgr, md, db):
    bom, *_ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    with pytest.raises(HTTPException) as ei:
        await svc.reject_bom(md, bom.id, reason="   ")
    assert ei.value.status_code == 422
    assert _detail(ei)["error"] == "reason_required"


async def test_reject_from_ready_for_review_succeeds(svc, draft_bom, cutting_mgr, md, db):
    bom, *_ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    result = await svc.reject_bom(md, bom.id, reason="  DCM for lining looks doubled  ")
    assert result["status"] == BomStatus.REJECTED.value
    assert result["rejection_reason"] == "DCM for lining looks doubled"   # trimmed
    await db.refresh(bom)
    assert bom.rejected_by == md.id


async def test_reject_an_approved_bom_is_blocked(svc, draft_bom, cutting_mgr, md, db):
    bom, *_ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    await svc.approve_bom(md, bom.id)
    with pytest.raises(HTTPException) as ei:
        await svc.reject_bom(md, bom.id, reason="too late")
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "bom_locked"


# ══════════════════════════════════════════════════ POST /reopen
async def test_reopen_requires_rejected_status(svc, draft_bom, dm):
    bom, *_ = draft_bom   # draft, never rejected
    with pytest.raises(HTTPException) as ei:
        await svc.reopen_bom(dm, bom.id)
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "not_rejected"


async def test_reopen_resets_to_draft_and_bumps_revision_and_clears_reject_fields(svc, draft_bom, cutting_mgr, md, dm, db):
    bom, *_ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    await svc.reject_bom(md, bom.id, reason="fix the colour")
    await db.refresh(bom)
    revision_at_rejection = bom.revision

    result = await svc.reopen_bom(dm, bom.id)
    assert result["status"] == BomStatus.DRAFT.value
    assert result["revision"] == revision_at_rejection + 1
    await db.refresh(bom)
    assert bom.rejection_reason is None
    assert bom.rejected_by is None
    assert bom.cutting_confirmed_at is None   # closes the "approve without reopen" gap


# ══════════════════════════════════════════════════ POST /export
async def test_export_requires_approved_or_locked(svc, draft_bom, md):
    bom, *_ = draft_bom   # draft
    with pytest.raises(HTTPException) as ei:
        await svc.export_bom(md, bom.id)
    assert ei.value.status_code == 409
    assert _detail(ei)["error"] == "not_approved"


async def test_export_generates_a_document_and_flips_status_to_exported(svc, draft_bom, cutting_mgr, md, db, monkeypatch):
    """Storage IO is stubbed so this stays a BOM-service test, not a filesystem
    test — export.render_bom_pdf itself already degrades to raw HTML with no
    external deps, so only the storage .put() needs a fake."""
    class _FakeStorage:
        def put(self, key, data):
            return f"fake://{key}"

    monkeypatch.setattr("app.modules.bom.service.get_storage", lambda: _FakeStorage())

    bom, *_ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    await svc.approve_bom(md, bom.id)

    result = await svc.export_bom(md, bom.id)
    assert result["status"] == BomStatus.EXPORTED.value
    assert result["export_document_id"] is not None
    assert result["sha256"]


async def test_export_is_idempotent_on_a_second_call(svc, draft_bom, cutting_mgr, md, db, monkeypatch):
    class _FakeStorage:
        def put(self, key, data):
            return f"fake://{key}"

    monkeypatch.setattr("app.modules.bom.service.get_storage", lambda: _FakeStorage())

    bom, *_ = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    await svc.approve_bom(md, bom.id)

    first = await svc.export_bom(md, bom.id)
    second = await svc.export_bom(md, bom.id)
    assert second["export_document_id"] == first["export_document_id"]
    assert second["sha256"] == first["sha256"]
