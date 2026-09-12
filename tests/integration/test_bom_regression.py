"""
================================================================================
tests/integration/test_bom_regression.py — regression guards for the BOM module
================================================================================
Follows the project's `_fixes.py` convention (see test_drawers_fixes.py,
test_attendance_fixes.py): each case locks in one specific, previously-verified
behaviour of app/modules/bom so a future change to service.py/router.py either
leaves it alone deliberately, or breaks a named test instead of shipping
silently.

Two kinds of guard live here:
  (a) a documented GAP — approve_bom does not itself re-check bom.status, only
      cutting_confirmed_at — captured as a passing test so it can never regress
      *silently*; if someone tightens the check on purpose, this test is the
      one that has to change, on purpose, in the same commit.
  (b) closed-loop confirmations — the same gap, immediately followed by the
      guard rail that actually closes it in the normal flow (reopen clears the
      confirmation), so the pair together prove the loophole is narrow, not
      wide open.

R1/R2  — reject_bom leaves cutting_confirmed_at untouched, and approve_bom does
         not check status -> a rejected-but-still-"confirmed" BOM can be
         approved without ever calling reopen.
R3     — reopen_bom clears cutting_confirmed_at, which puts R1's gap back
         behind the "cutting_confirmation_required" gate.
R4     — the §10 reconfirm flip fires only for MATERIAL_DCM_CATEGORIES fields,
         not for a plain unit_price edit.
R5     — edit_bom_items' optimistic lock is a real compare-and-swap
         (claim_revision), not just the earlier equality check.
R6     — export_bom's idempotent replay returns the SAME document instead of
         re-rendering (dedup by bom.export_document_id, short-circuit).
R7     — RBAC: DIRECT_MANAGER passes the "_MD"-only router gates (approve/
         reject/export) because SUPERUSER_ROLES bypasses require_roles
         entirely — verified at the router layer via api_client/as_role, since
         that bypass lives in users.deps, not in BomService.
R8     — isolates a LIVE BUG found while writing this suite: confirm_cutting
         (service.py ~1327-1338) commits+refreshes `bom` via repo.save, which
         expires the `items` relationship, then immediately loops
         `for item in bom.items:` — an implicit async lazy-load that raises
         `sqlalchemy.exc.MissingGreenlet`. R1-R7 need a BOM in "already cutting
         confirmed" state as a PRECONDITION, not as their subject, so they set
         that state directly via `_mark_cutting_confirmed` (defined below)
         instead of calling the real (broken) method — R8 is the one test
         that calls it for real and isolates the bug with nothing else in the
         way. Left unfixed deliberately: R8 asserts the intended behaviour, so
         its failure itself is the bug report — run this file, take the
         MissingGreenlet traceback under R8 to the backend dev.
================================================================================
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.core.enums import UserRole
from app.modules.bom.enums import BomItemCategory, BomStatus, DcmSource
from app.modules.bom.models import Bom, BomItem
from app.modules.bom.service import BomService


@pytest_asyncio.fixture
async def draft_bom(db):
    bom = Bom(status=BomStatus.DRAFT.value, currency="INR", order_qty=60,
              revision=1, dcm_base_size="M")
    db.add(bom)
    await db.flush()
    leather = BomItem(
        bom_id=bom.id, category=BomItemCategory.MAIN_MATERIAL.value,
        name="Cow Nappa Leather", qty_per_garment=Decimal("34.5"), uom="dm2",
        unit_price=Decimal("1.80"), dcm_source=DcmSource.TEMPLATE.value,
        dcm_confidence=Decimal("0.9000"),
    )
    db.add(leather)
    await db.commit()
    for obj in (bom, leather):
        await db.refresh(obj)
    return bom, leather


@pytest.fixture
def svc(db):
    return BomService(db)


async def _mark_cutting_confirmed(db, bom, user):
    """Set the fields confirm_cutting() would, without calling the (buggy — see
    R8) real method. R1-R7's own claims have nothing to do with confirm_cutting
    itself, so they use this to reach their precondition state directly."""
    from datetime import datetime, timezone
    bom.cutting_confirmed_by = user.id
    bom.cutting_confirmed_at = datetime.now(timezone.utc)
    bom.status = BomStatus.READY_FOR_REVIEW.value
    await db.commit()
    await db.refresh(bom)


@pytest.fixture(autouse=True)
def _stub_approve_side_effects(monkeypatch):
    """Same isolation rationale as test_bom_lifecycle.py: approve_bom's fan-out
    into production-tracking + inventory-check belongs to other modules'
    suites, not this one."""
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


# ══════════════════════════════════════════ R1/R2 — the approve-without-reopen gap
@pytest.mark.integrity
async def test_r1_reject_does_not_clear_cutting_confirmation(svc, draft_bom, cutting_mgr, md, db):
    """LOCKED BEHAVIOUR, not a recommendation: reject_bom's current implementation
    only sets status/rejected_by/rejected_at/rejection_reason. If a future edit
    starts clearing cutting_confirmed_at on reject, this assertion is the one
    that must change — deliberately, alongside R2 below."""
    bom, _leather = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    await svc.reject_bom(md, bom.id, reason="wrong leather grade")
    await db.refresh(bom)
    assert bom.status == BomStatus.REJECTED.value
    assert bom.cutting_confirmed_at is not None   # <- survives the rejection


@pytest.mark.integrity
async def test_r2_a_rejected_bom_can_still_be_approved_without_reopen(svc, draft_bom, cutting_mgr, md, db):
    """Direct consequence of R1: approve_bom's ONLY gate is
    `cutting_confirmed_at is not None` — it never re-reads bom.status. So a
    REJECTED BOM that was confirmed before rejection is still approvable.
    This test exists so that behaviour is a documented, tested fact rather than
    a surprise found in production. If the product decision is to close this,
    the fix belongs in approve_bom (re-check status) — and this test should
    then assert the opposite (409) instead of being deleted."""
    bom, _leather = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    await svc.reject_bom(md, bom.id, reason="wrong leather grade")
    await db.refresh(bom)
    assert bom.status == BomStatus.REJECTED.value

    result = await svc.approve_bom(md, bom.id)     # no reopen in between
    assert result["status"] == BomStatus.APPROVED.value


# ══════════════════════════════════════════ R3 — reopen closes the gap in the normal flow
@pytest.mark.integrity
async def test_r3_reopen_clears_the_confirmation_so_approve_is_blocked_again(svc, draft_bom, cutting_mgr, md, dm, db):
    """The counterpart to R2: once a rejected BOM actually goes through
    `reopen`, cutting_confirmed_at IS cleared, so approve correctly refuses
    until Confirm Cutting runs again. This is what makes R2 a narrow loophole
    (only reachable by skipping reopen) rather than the approval gate being
    broken outright."""
    bom, _leather = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    await svc.reject_bom(md, bom.id, reason="wrong leather grade")
    await svc.reopen_bom(dm, bom.id)
    await db.refresh(bom)
    assert bom.cutting_confirmed_at is None

    with pytest.raises(HTTPException) as ei:
        await svc.approve_bom(md, bom.id)
    assert ei.value.status_code == 409
    assert ei.value.detail["error"] == "cutting_confirmation_required"


# ══════════════════════════════════════════ R4 — the §10 reconfirm flip is field-scoped
@pytest.mark.integrity
async def test_r4_reconfirm_flip_is_scoped_to_dcm_qty_not_price(svc, draft_bom, cutting_mgr, db):
    """`dcm_changed` (and therefore reconfirm_required + the draft revert) must
    be driven by a dcm/qty_per_garment edit on a MATERIAL_DCM_CATEGORIES line —
    a unit_price-only edit on the very same line, on the very same
    already-confirmed BOM, must NOT trip it. This is the one-line difference
    between EI‑L2 and EI‑L3 in the QA test plan, pinned as one test so the two
    branches can't silently drift onto the same behaviour."""
    bom, leather = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    confirmed_revision = bom.revision

    # NOTE: no db.refresh(bom) between these calls. edit_bom_items ends by
    # re-querying the bom (with selectinload) and reassigning its local
    # `bom` — the SAME identity-mapped object our `bom` variable already
    # points at — so this object's attributes are already current after each
    # await below. Manually refreshing it here would expire its `items`
    # relationship right before the next call's own selectinload query
    # touches that same object, which trips an unrelated SQLAlchemy async
    # nested-load error (not the confirm_cutting bug) — keep this shape.
    price_only = await svc.edit_bom_items(
        cutting_mgr, bom.id, base_revision=confirmed_revision,
        edits=[{"bom_item_id": str(leather.id), "field": "unit_price", "value": 3.0}])
    assert price_only["reconfirm_required"] is False
    assert bom.status == BomStatus.READY_FOR_REVIEW.value
    assert bom.cutting_confirmed_at is not None

    dcm_edit = await svc.edit_bom_items(
        cutting_mgr, bom.id, base_revision=bom.revision,
        edits=[{"bom_item_id": str(leather.id), "field": "dcm", "value": 41.0}])
    assert dcm_edit["reconfirm_required"] is True
    assert bom.status == BomStatus.DRAFT.value
    assert bom.cutting_confirmed_at is None


# ══════════════════════════════════════════ R5 — optimistic lock is a real CAS
@pytest.mark.integrity
async def test_r5_a_second_writer_is_refused_and_its_value_never_lands(svc, draft_bom, db):
    """Complements test_edit_a_second_writer_loses_the_revision_race in
    test_bom_lifecycle.py: that test asserts the 409; this one additionally
    asserts the DATA side — the second writer's rejected value must not have
    been written even transiently. A naive optimistic-lock implementation can
    get the error code right while still flushing the losing write before
    rolling back; this pins the actual column value, not just the response."""
    bom, leather = draft_bom
    # No db.refresh() between these two calls — see the note in R4 just above:
    # edit_bom_items already re-syncs this identity-mapped `bom`/`leather` pair
    # via its own post-commit selectinload re-query.
    await svc.edit_bom_items(object(), bom.id, base_revision=1,
                             edits=[{"bom_item_id": str(leather.id),
                                     "field": "unit_price", "value": 1.5}])
    assert bom.revision == 2

    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(object(), bom.id, base_revision=1,   # the now-stale value
                                 edits=[{"bom_item_id": str(leather.id),
                                         "field": "unit_price", "value": 1.6}])
    assert ei.value.status_code == 409
    assert ei.value.detail["error"] == "stale_revision"
    assert ei.value.detail["current_revision"] == 2

    # and the second writer's value must NOT have been applied
    assert leather.unit_price == Decimal("1.50")


# ══════════════════════════════════════════ R6 — export replay is idempotent
@pytest.mark.integrity
async def test_r6_export_replay_returns_the_same_document_not_a_new_one(svc, draft_bom, cutting_mgr, md, db, monkeypatch):
    class _FakeStorage:
        def __init__(self):
            self.put_calls = 0

        def put(self, key, data):
            self.put_calls += 1
            return f"fake://{key}"

    fake_storage = _FakeStorage()
    monkeypatch.setattr("app.modules.bom.service.get_storage", lambda: fake_storage)

    bom, _leather = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)
    await svc.approve_bom(md, bom.id)

    first = await svc.export_bom(md, bom.id)
    second = await svc.export_bom(md, bom.id)

    assert second["export_document_id"] == first["export_document_id"]
    assert second["sha256"] == first["sha256"]
    assert fake_storage.put_calls == 1   # the second export must not re-render/re-store


# ══════════════════════════════════════════ R7 — router-level RBAC bypass (system layer)
@pytest.mark.security
async def test_r7_direct_manager_bypasses_the_md_only_approve_gate(api_client, as_role, draft_bom, cutting_mgr, svc, db):
    """approve_bom's router dependency is `require_roles(MANAGING_DIRECTOR)`,
    documented ("MD") as MD-only — but users.deps.require_roles lets every
    SUPERUSER_ROLES member through regardless of the roles it names, and
    DIRECT_MANAGER is one. Verified over HTTP (not at the service layer,
    which has no opinion on roles) so a future change to require_roles or to
    SUPERUSER_ROLES is caught here first."""
    bom, _leather = draft_bom
    await _mark_cutting_confirmed(db, bom, cutting_mgr)

    as_role(UserRole.DIRECT_MANAGER)
    resp = await api_client.post(f"/api/v1/procurement/boms/{bom.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == BomStatus.APPROVED.value


@pytest.mark.security
async def test_r7b_cutting_manager_is_correctly_refused_the_same_gate(api_client, as_role, draft_bom):
    """Contrast case for R7: a role that is neither MD nor a superuser must
    still get a hard 403, proving the bypass is specific to SUPERUSER_ROLES
    and not a general hole in the gate. No cutting-confirm setup needed here —
    require_roles runs as a router dependency, before the service body (and
    therefore before it could ever reach the BOM's actual state)."""
    bom, _leather = draft_bom

    as_role(UserRole.CUTTING_MANAGER)
    resp = await api_client.post(f"/api/v1/procurement/boms/{bom.id}/approve")
    assert resp.status_code == 403


# ══════════════════════════════════════════ R8 — isolates the confirm_cutting bug itself
@pytest.mark.integrity
async def test_r8_confirm_cutting_succeeds_on_a_plain_draft_bom(svc, draft_bom, cutting_mgr):
    """THE bug, isolated: this asserts the INTENDED behaviour (confirm_cutting
    on a plain fresh DRAFT BOM just works), same as every other case in this
    suite. Nothing above this line needed the real confirm_cutting to reach ITS
    OWN assertions (they all use _mark_cutting_confirmed instead), so this is
    the one test in the whole file that calls the actual method — with nothing
    else going on, so a failure here has exactly one possible cause. This is
    expected to currently FAIL with sqlalchemy.exc.MissingGreenlet; that
    traceback is the bug report. See the module docstring for the root cause
    and the one-line fix, intentionally not applied here."""
    bom, _leather = draft_bom
    result = await svc.confirm_cutting(cutting_mgr, bom.id)
    assert result["status"] == BomStatus.READY_FOR_REVIEW.value
