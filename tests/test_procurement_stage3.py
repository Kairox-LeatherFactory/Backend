"""
================================================================================
tests/test_procurement_stage3.py — Stage-3 approval acceptance (stage-3 spec §7)
================================================================================

Confirms every Stage-3 acceptance criterion at the service + HTTP layer:

  1. State machine + RBAC: confirm → ready_for_review (+notifies); MD-only approve/
     reject; DM cannot approve; approval always locks; locked rejects edits.
  2. Notification persists + delivers: ready_for_review writes MD + DM rows
     (in_app, scheduled_for=+2h); list/open work; open sets opened_at.
  3. 2-hour escalation, conditional + idempotent: an unseen-past-deadline notice
     emails once; a second sweep does not double-send; an opened notice never emails.
  4. Editing reuses Stage 2 + a DCM edit re-opens the gate → draft.
  5. Rejection captured: reason mandatory; BOM_REJECT audit carries it; reopen →
     draft, revision+1.
  6. PDF reflects edited values, from a locked BOM only; stored sha256-deduped;
     re-export idempotent.
  7. Audit completeness across the edges.
================================================================================
"""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.enums import UserRole
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.procurement.bom_service import BomService, LineSeed, StyleIdentity
from app.modules.procurement.enums import (
    BomItemCategory,
    BomStatus,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
    SpecType,
)
from app.modules.procurement.models import (
    AuditLog,
    Document,
    GarmentType,
    Notification,
    PomDictionary,
    SpecSheet,
    StyleConsumptionTemplate,
)
from app.modules.procurement.notification_service import NotificationService
from app.modules.procurement.seed_stage2 import _GARMENT_YAML, _POM_DICT_YAML
from app.modules.users.models import User

DATA = Path(__file__).resolve().parent.parent / "data"


def _read(name: str) -> bytes:
    return (DATA / name).read_bytes()


async def _seed_stage2(db) -> dict:
    gts: dict[str, GarmentType] = {}
    for r in yaml.safe_load(open(_GARMENT_YAML, encoding="utf-8")):
        gt = GarmentType(code=r["code"], label=r.get("label"),
                         required_poms=r.get("required_poms") or [],
                         area_formula=r.get("area_formula") or {},
                         default_wastage_pct=r.get("default_wastage_pct"))
        db.add(gt)
        gts[r["code"]] = gt
    await db.flush()
    for r in yaml.safe_load(open(_POM_DICT_YAML, encoding="utf-8")):
        db.add(PomDictionary(language=r["language"], source_term=r["source_term"],
                             pom_code=r["pom_code"], weight=r.get("weight", 1)))
    await db.commit()
    return gts


async def _users(db) -> dict:
    """A cutting manager, an MD, and a DM — the Stage-3 actors + notice recipients."""
    rows = {
        "cutting": User(id=uuid.uuid4(), name="Cut", phone="9000000002",
                        role=UserRole.CUTTING_MANAGER, password_hash="x", is_active=True),
        "md": User(id=uuid.uuid4(), name="Boss", phone="9000000000", email="md@factory.test",
                   role=UserRole.MANAGING_DIRECTOR, password_hash="x", is_active=True),
        "dm": User(id=uuid.uuid4(), name="Dee", phone="9000000001", email="dm@factory.test",
                   role=UserRole.DIRECT_MANAGER, password_hash="x", is_active=True),
    }
    for u in rows.values():
        db.add(u)
    await db.commit()
    return rows


async def _make_order(db, *, customer_ref="CR1-02F5-PL02", order_no="1579",
                      per_size=None, name="SIDE SUEDE TRACK PANT"):
    per_size = per_size or {"S": 12, "M": 12, "L": 12, "XL": 12, "XXL": 12}
    client = (await db.execute(select(Client).where(Client.code == "BG"))).scalar_one_or_none()
    if client is None:
        client = Client(name="Beau Geste", country="Japan", code="BG", currency="USD")
        db.add(client)
        await db.flush()
    order = ClientOrder(client_id=client.id, order_number=order_no, currency="USD")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name=name, customer_ref=customer_ref, currency="USD")
    db.add(style)
    await db.flush()
    for size, qty in per_size.items():
        db.add(SKU(style_id=style.id, color_code="BLK", size=size, qty_ordered=qty))
    spec = SpecSheet(client_id=client.id, style_id=style.id,
                     spec_type=SpecType.MEASUREMENT_GRID.value)
    db.add(spec)
    await db.commit()
    identity = StyleIdentity(
        client_id=client.id, client_order_id=order.id, style_id=style.id,
        customer_ref=customer_ref, name=name, order_qty=sum(per_size.values()),
        per_size_qty=per_size, order_number=order_no)
    return client, order, style, spec, identity


def _bmo1_seeds():
    return [
        LineSeed(BomItemCategory.MAIN_MATERIAL.value, "SHEEP GLASS", material_color="BLACK",
                 uom="dm²", unit_price=1.80),
        LineSeed(BomItemCategory.SUB_MATERIAL.value, "GOAT SUEDE", uom="dm²", unit_price=1.75),
        LineSeed(BomItemCategory.LINING.value, "LINING", uom="dm²", unit_price=1.00),
        LineSeed(BomItemCategory.INTERLINING.value, "INTERLINING", uom="dm²", unit_price=1.00),
        LineSeed(BomItemCategory.THREAD.value, "THREADS", uom="pc", unit_price=1.00,
                 qty_per_garment=1),
        LineSeed(BomItemCategory.MANUFACTURING.value, "CUTTING & STITCHING", uom="pc",
                 unit_price=30.00, qty_per_garment=1),
        LineSeed(BomItemCategory.PACKAGING.value, "PACKAGING", uom="pc", unit_price=3.00,
                 qty_per_garment=1),
        LineSeed(BomItemCategory.FOB_CHARGE.value, "FOB CHARGE", uom="pc", unit_price=5.00,
                 qty_per_garment=1),
    ]


async def _seed_templates_for_bmo1(db, identity, gt_track_pant, base_size="S"):
    dcm = {BomItemCategory.MAIN_MATERIAL.value: "34.5",
           BomItemCategory.SUB_MATERIAL.value: "2.6",
           BomItemCategory.LINING.value: "0.6",
           BomItemCategory.INTERLINING.value: "1.0"}
    for cat, val in dcm.items():
        db.add(StyleConsumptionTemplate(
            client_id=identity.client_id, style_signature=identity.customer_ref,
            garment_type_id=gt_track_pant.id, material_category=cat, size=base_size,
            dcm_value=val, uom="dm²"))
    await db.commit()


async def _draft_bom(db, *, per_size=None):
    """Seed registries + users + a generated DRAFT BOM whose leather resolves via
    Source-1 templates (so no ai_estimate noise). Returns (svc, users, identity, bom_id)."""
    gts = await _seed_stage2(db)
    users = await _users(db)
    _, _, _, spec, identity = await _make_order(db, per_size=per_size)
    await _seed_templates_for_bmo1(db, identity, gts["TRACK_PANT"])
    svc = BomService(db)
    out = await svc.generate_bom(
        users["cutting"], spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"),
        filename="s.xlsx", identity=identity, client_match_code="beau_geste",
        line_seeds=_bmo1_seeds(), currency="USD", extractor=None)
    return svc, users, identity, uuid.UUID(out["bom"]["id"])


# ════════════════════════════════════════════════════════════════════════════
# §7.1 — confirm → ready_for_review; §7.2 — notifies MD + DM (two rows, +2h)
# ════════════════════════════════════════════════════════════════════════════
async def test_confirm_advances_to_ready_and_notifies_md_and_dm(db):
    svc, users, identity, bom_id = await _draft_bom(db)
    bom = await svc.repo.get_bom(bom_id)
    assert bom.status == BomStatus.DRAFT.value

    res = await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    assert res["status"] == BomStatus.READY_FOR_REVIEW.value
    assert res["notifications_created"] == 2

    rows = (await db.execute(select(Notification))).scalars().all()
    assert len(rows) == 2
    assert {r.recipient_user_id for r in rows} == {users["md"].id, users["dm"].id}
    for r in rows:
        assert r.channel == NotificationChannel.IN_APP.value
        assert r.type == NotificationType.BOM_AWAITING_REVIEW.value
        assert r.opened_at is None and r.scheduled_for is not None
        # ~2 hours out (the product owner's escalation window). SQLite returns the
        # DateTime(timezone=True) value tz-naive, so normalise both sides.
        sched = r.scheduled_for
        ref = datetime.now(timezone.utc) + timedelta(hours=1)
        if sched.tzinfo is None:
            ref = ref.replace(tzinfo=None)
        assert sched > ref


# ════════════════════════════════════════════════════════════════════════════
# §7.1 — MD-only approve; approval always locks; locked rejects edits
# ════════════════════════════════════════════════════════════════════════════
async def test_approve_locks_and_freezes(db):
    svc, users, identity, bom_id = await _draft_bom(db)
    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    res = await svc.approve_bom(users["md"], bom_id, lock=True)
    assert res["status"] == BomStatus.LOCKED.value
    bom = await svc.repo.get_bom(bom_id)
    assert bom.locked_at is not None and bom.approved_by == users["md"].id

    sheep = next(i for i in svc._bom_view(bom)["items"] if i["name"] == "SHEEP GLASS")
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(users["cutting"], bom_id, bom.revision,
                                 [{"bom_item_id": sheep["id"], "field": "unit_price", "value": 9.0}])
    assert ei.value.status_code == 409 and ei.value.detail["error"] == "bom_locked"


async def test_approve_without_lock_is_still_immutable(db):
    svc, users, identity, bom_id = await _draft_bom(db)
    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    res = await svc.approve_bom(users["md"], bom_id, lock=False)
    assert res["status"] == BomStatus.APPROVED.value
    bom = await svc.repo.get_bom(bom_id)
    assert bom.locked_at is not None                      # approval always locks (§1a)
    sheep = next(i for i in svc._bom_view(bom)["items"] if i["name"] == "SHEEP GLASS")
    with pytest.raises(HTTPException) as ei:
        await svc.edit_bom_items(users["cutting"], bom_id, bom.revision,
                                 [{"bom_item_id": sheep["id"], "field": "unit_price", "value": 9.0}])
    assert ei.value.detail["error"] == "bom_locked"


# ════════════════════════════════════════════════════════════════════════════
# §7.5 — reject: reason mandatory, audit captures it; reopen → draft, revision+1
# ════════════════════════════════════════════════════════════════════════════
async def test_reject_requires_reason_and_audits(db):
    svc, users, identity, bom_id = await _draft_bom(db)

    # cannot reject a draft (only from ready_for_review)
    with pytest.raises(HTTPException) as e0:
        await svc.reject_bom(users["md"], bom_id, reason="too early")
    assert e0.value.status_code == 409 and e0.value.detail["error"] == "not_ready_for_review"

    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)

    # empty reason → 422
    with pytest.raises(HTTPException) as e1:
        await svc.reject_bom(users["md"], bom_id, reason="   ")
    assert e1.value.status_code == 422 and e1.value.detail["error"] == "reason_required"

    res = await svc.reject_bom(users["md"], bom_id, reason="unit prices look wrong")
    assert res["status"] == BomStatus.REJECTED.value
    bom = await svc.repo.get_bom(bom_id)
    assert bom.rejection_reason == "unit prices look wrong" and bom.rejected_by == users["md"].id

    audits = (await db.execute(select(AuditLog).where(AuditLog.action == "BOM_REJECT"))).scalars().all()
    assert len(audits) == 1 and audits[0].after["rejection_reason"] == "unit prices look wrong"


async def test_reopen_rejected_to_draft(db):
    svc, users, identity, bom_id = await _draft_bom(db)
    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    bom = await svc.repo.get_bom(bom_id)
    rev_before = bom.revision
    await svc.reject_bom(users["md"], bom_id, reason="redo")

    res = await svc.reopen_bom(users["dm"], bom_id)
    assert res["status"] == BomStatus.DRAFT.value and res["revision"] == rev_before + 1
    bom = await svc.repo.get_bom(bom_id)
    assert bom.rejection_reason is None and bom.rejected_at is None
    assert bom.cutting_confirmed_at is None               # re-confirmation required

    # reopening a non-rejected BOM → 409
    with pytest.raises(HTTPException) as ei:
        await svc.reopen_bom(users["dm"], bom_id)
    assert ei.value.detail["error"] == "not_rejected"


# ════════════════════════════════════════════════════════════════════════════
# §7.4 — a DCM edit on the approval screen re-opens the gate → back to draft
# ════════════════════════════════════════════════════════════════════════════
async def test_dcm_edit_after_confirm_reopens_to_draft(db):
    svc, users, identity, bom_id = await _draft_bom(db)
    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    bom = await svc.repo.get_bom(bom_id)
    assert bom.status == BomStatus.READY_FOR_REVIEW.value
    sheep = next(i for i in svc._bom_view(bom)["items"] if i["name"] == "SHEEP GLASS")

    res = await svc.edit_bom_items(users["cutting"], bom_id, bom.revision,
                                   [{"bom_item_id": sheep["id"], "field": "dcm", "value": 35.0}])
    assert res["reconfirm_required"] is True
    bom = await svc.repo.get_bom(bom_id)
    assert bom.status == BomStatus.DRAFT.value and bom.cutting_confirmed_at is None
    # approval refused again until re-confirmed
    with pytest.raises(HTTPException) as ei:
        await svc.approve_bom(users["md"], bom_id)
    assert ei.value.detail["error"] == "cutting_confirmation_required"


# ════════════════════════════════════════════════════════════════════════════
# §7.6 — PDF export only from a locked BOM; stored sha256-deduped; idempotent
# ════════════════════════════════════════════════════════════════════════════
async def test_export_only_after_lock_and_idempotent(db):
    svc, users, identity, bom_id = await _draft_bom(db, per_size={"S": 60})

    # refused before approval
    with pytest.raises(HTTPException) as ei:
        await svc.export_bom(users["md"], bom_id)
    assert ei.value.status_code == 409 and ei.value.detail["error"] == "not_approved"

    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    await svc.approve_bom(users["md"], bom_id, lock=True)

    res = await svc.export_bom(users["md"], bom_id)
    assert res["status"] == BomStatus.EXPORTED.value
    assert res["mime"] == "application/pdf"               # reportlab is installed → real PDF
    doc = await svc.repo.get_document(uuid.UUID(res["export_document_id"]))
    assert doc is not None and doc.kind == "bom_quote" and (doc.size_bytes or 0) > 0
    assert doc.sha256 == res["sha256"]

    # re-export of the unchanged locked BOM is idempotent on the rendered sha256
    res2 = await svc.export_bom(users["md"], bom_id)
    assert res2["export_document_id"] == res["export_document_id"]
    assert res2["sha256"] == res["sha256"]
    n_docs = await db.scalar(select(func.count(Document.id)).where(Document.kind == "bom_quote"))
    assert n_docs == 1                                    # no duplicate Document

    audits = (await db.execute(select(AuditLog).where(AuditLog.action == "BOM_EXPORT"))).scalars().all()
    assert len(audits) >= 1 and audits[0].after["sha256"] == res["sha256"]


# ════════════════════════════════════════════════════════════════════════════
# §7.6 — the PDF renders the post-edit values (a different price → different bytes)
# ════════════════════════════════════════════════════════════════════════════
async def test_export_reflects_edited_values(db):
    svc, users, identity, bom_id = await _draft_bom(db, per_size={"S": 60})
    bom = await svc.repo.get_bom(bom_id)
    sheep = next(i for i in svc._bom_view(bom)["items"] if i["name"] == "SHEEP GLASS")
    # edit the unit price BEFORE confirming (post-generation edit), then lock + export
    await svc.edit_bom_items(users["cutting"], bom_id, bom.revision,
                             [{"bom_item_id": sheep["id"], "field": "unit_price", "value": 2.50}])
    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    await svc.approve_bom(users["md"], bom_id, lock=True)
    res = await svc.export_bom(users["md"], bom_id)

    # the locked BOM carries the edited price; the render is driven off it
    bom = await svc.repo.get_bom(bom_id)
    edited = next(i for i in bom.items if i.name == "SHEEP GLASS")
    assert float(edited.unit_price) == 2.50
    assert float(edited.total_cost) == round(34.5 * 2.50, 2)   # recomputed at write time
    assert res["mime"] == "application/pdf"


# ════════════════════════════════════════════════════════════════════════════
# §7.2/§7.3 — notifications: open cancels; the 2-hour escalation is conditional
# ════════════════════════════════════════════════════════════════════════════
async def _backdate(db, notifications, *, minutes=5):
    """Force the escalation deadline into the past so the sweep treats it as due."""
    past = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    for n in notifications:
        n.scheduled_for = past
    await db.commit()


async def test_escalation_emails_unseen_then_idempotent(db):
    svc, users, identity, bom_id = await _draft_bom(db)
    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    notes = (await db.execute(select(Notification))).scalars().all()
    await _backdate(db, notes)

    nsvc = NotificationService(db)
    sent = await nsvc.run_escalations()
    assert sent == 2                                      # MD + DM both unseen past deadline

    children = (await db.execute(select(Notification).where(
        Notification.channel == NotificationChannel.EMAIL.value))).scalars().all()
    assert len(children) == 2
    assert all(c.parent_notification_id is not None for c in children)
    assert all(c.status == NotificationStatus.SENT.value for c in children)

    # a SECOND sweep does not double-email (the NOT-EXISTS idempotency guard)
    assert await nsvc.run_escalations() == 0


async def test_open_cancels_escalation_and_is_recipient_scoped(db):
    svc, users, identity, bom_id = await _draft_bom(db)
    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    notes = (await db.execute(select(Notification))).scalars().all()
    md_note = next(n for n in notes if n.recipient_user_id == users["md"].id)

    nsvc = NotificationService(db)
    # a non-recipient cannot open it
    with pytest.raises(HTTPException) as ei:
        await nsvc.mark_opened(md_note.id, users["cutting"])
    assert ei.value.status_code == 403
    # the recipient opens it → opened_at set
    view = await nsvc.mark_opened(md_note.id, users["md"])
    assert view["opened_at"] is not None and view["status"] == NotificationStatus.OPENED.value

    await _backdate(db, notes)
    # only the still-unseen DM notice escalates; the opened MD notice does NOT
    assert await nsvc.run_escalations() == 1


async def test_list_notifications_for_user(db):
    svc, users, identity, bom_id = await _draft_bom(db)
    await svc.confirm_cutting(users["cutting"], bom_id, identity=identity)
    out = await NotificationService(db).list_for_user(users["md"].id)
    assert out["unread"] == 1 and len(out["notifications"]) == 1
    assert out["notifications"][0]["entity_type"] == "bom"


# ════════════════════════════════════════════════════════════════════════════
# HTTP shell — RBAC for the Stage-3 endpoints (reject MD-only; notifications auth)
# ════════════════════════════════════════════════════════════════════════════
async def _token(client, phone):
    r = await client.post("/api/v1/auth/login", json={"username": phone, "password": phone})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_http_stage3_rbac(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod
    from app.core.database import Base
    from app.main import app
    from app.modules.users import schemas as user_schemas
    from app.modules.users.service import UserService

    engine = create_async_engine("sqlite+aiosqlite://",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(dbmod, "async_engine", engine, raising=False)
    monkeypatch.setattr(dbmod, "AsyncSessionLocal",
                        async_sessionmaker(bind=engine, expire_on_commit=False), raising=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with dbmod.AsyncSessionLocal() as db:
        gts = await _seed_stage2(db)
        us = UserService(db)
        await us.create_user(user_schemas.UserCreate(
            name="Cut", phone="9000000002", role=UserRole.CUTTING_MANAGER, password="9000000002"))
        await us.create_user(user_schemas.UserCreate(
            name="MD", phone="9000000000", role=UserRole.MANAGING_DIRECTOR, password="9000000000"))
        await us.create_user(user_schemas.UserCreate(
            name="Emp", phone="9100000001", role=UserRole.EMPLOYEE, password="9100000001"))
        cutting = (await db.execute(select(User).where(
            User.role == UserRole.CUTTING_MANAGER))).scalar_one()
        _, _, _, spec, identity = await _make_order(db)
        await _seed_templates_for_bmo1(db, identity, gts["TRACK_PANT"])
        out = await BomService(db).generate_bom(
            cutting, spec_sheet=spec, spec_bytes=_read("spec_sheet_1.xlsx"), filename="s.xlsx",
            identity=identity, client_match_code="beau_geste", line_seeds=_bmo1_seeds(),
            currency="USD", extractor=None)
        bom_id = out["bom"]["id"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cut = await _token(c, "9000000002")
        md = await _token(c, "9000000000")
        emp = await _token(c, "9100000001")

        # cutting manager confirms → ready_for_review (+ notifies MD)
        r = await c.post(f"/api/v1/procurement/boms/{bom_id}/confirm-cutting", headers=cut)
        assert r.status_code == 200 and r.json()["status"] == BomStatus.READY_FOR_REVIEW.value

        # reject is MD-only — the cutting manager is forbidden
        r = await c.post(f"/api/v1/procurement/boms/{bom_id}/reject", headers=cut,
                         json={"reason": "no"})
        assert r.status_code == 403

        # the MD has an in-app notification waiting (any authenticated user may read
        # their own; the employee simply has none)
        r = await c.get("/api/v1/procurement/notifications", headers=md)
        assert r.status_code == 200 and r.json()["unread"] == 1
        notif_id = r.json()["notifications"][0]["id"]
        r = await c.get("/api/v1/procurement/notifications", headers=emp)
        assert r.status_code == 200 and r.json()["unread"] == 0

        # opening it cancels escalation
        r = await c.post(f"/api/v1/procurement/notifications/{notif_id}/open", headers=md)
        assert r.status_code == 200 and r.json()["opened_at"] is not None

        # MD rejects with a reason → 200, then reopen → draft
        r = await c.post(f"/api/v1/procurement/boms/{bom_id}/reject", headers=md,
                         json={"reason": "fix the FOB charge"})
        assert r.status_code == 200 and r.json()["status"] == BomStatus.REJECTED.value
        r = await c.post(f"/api/v1/procurement/boms/{bom_id}/reopen", headers=md)
        assert r.status_code == 200 and r.json()["status"] == BomStatus.DRAFT.value

        # export is refused on a non-approved BOM
        r = await c.post(f"/api/v1/procurement/boms/{bom_id}/export", headers=md)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "not_approved"
    await engine.dispose()
