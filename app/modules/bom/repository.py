"""
================================================================================
modules/bom/repository.py — Stage-2/3 BOM data access
================================================================================

The only place that talks to the DB for bom-owned tables (spec_sheet, pom_*,
garment_type, style_consumption_template, pattern_reference, bom, bom_item) plus the
cross-cutting core tables this stage writes (document for the BOM-quote export,
notification for the MD review notice + its escalation). Cross-module data (clients,
inventory) flows through those modules' services, never here.

FUNCTION GUIDE  (all async; every method is called only by BomService)
  commit/rollback/save(obj)/add(obj)   session plumbing (save = commit + refresh).
  Document:
    get_document(id) / get_document_by_sha(sha) -> Document | None   export dedupe lookups.
    add_document(doc) -> Document                                    persist the rendered BOM-quote PDF.
  POM tables:
    pom_dictionary_rows() -> [(language, source_term, pom_code)]     feeds the PomDict standardizer.
    replace_pom_measurements(spec_sheet_id, rows)                    delete+insert (replace-on-key).
    pom_measurements(spec_sheet_id) -> [PomMeasurement]              read back the stored POMs.
  garment_type:
    get_garment_type(code) -> GarmentType | None                    the area-formula/wastage config row.
  style_consumption_template (the DCM memory):
    find_consumption_template(...)   exact Source-1 lookup on the 5-tuple key.
    find_similar_template(...)       Source-2 nearest (same type/category/size, other signature).
    get_consumption_template(id)     fetch by id (pattern-resolved template).
    upsert_consumption_template(...) the §10 back-fill (update or insert one confirmed DCM).
  pattern_reference:
    add_pattern_reference(ref) -> PatternReference.
  spec_sheet:
    get_spec_sheet(id) -> SpecSheet | None.
  bom:
    add_bom(bom) / get_bom(id, selectinload items) -> Bom | None.
    claim_revision(bom_id, base_revision) -> rowcount    atomic optimistic-lock CAS;
        0 rows ⇒ the caller raises 409 stale_revision.
  notification (core table; bom owns the BOM-ready notice + escalation read paths):
    add_notification / get_notification / list_notifications_for_user(unread_only).
    due_escalations(now) -> [Notification]   in-app review/approval notices past their
        deadline, unseen, not yet emailed (idempotent NOT-EXISTS guard) — read by the sweeper.
================================================================================
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import NotificationChannel, NotificationType
from app.core.models import Document, Notification
from app.modules.bom.models import (
    Bom,
    GarmentType,
    PomDictionary,
    PomMeasurement,
    SpecSheet,
    StyleConsumptionTemplate,
)


class BomRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def commit(self) -> None:
        await self.db.commit()

    async def rollback(self) -> None:
        await self.db.rollback()

    async def save(self, obj) -> None:
        await self.db.commit()
        await self.db.refresh(obj)

    async def add(self, obj):
        self.db.add(obj)
        await self.db.commit()
        await self.db.refresh(obj)
        return obj

    # ── document (core table; bom owns the quote-export write path) ──────────
    async def get_document(self, document_id) -> Document | None:
        res = await self.db.execute(select(Document).where(Document.id == document_id))
        return res.scalar_one_or_none()

    async def get_document_by_sha(self, sha256: str) -> Document | None:
        res = await self.db.execute(select(Document).where(Document.sha256 == sha256))
        return res.scalar_one_or_none()

    async def add_document(self, doc: Document) -> Document:
        self.db.add(doc)
        await self.db.commit()
        await self.db.refresh(doc)
        return doc

    # ── pom_dictionary / pom_measurement ─────────────────────────────────────
    async def pom_dictionary_rows(self) -> list[tuple[str, str, str]]:
        res = await self.db.execute(
            select(PomDictionary.language, PomDictionary.source_term, PomDictionary.pom_code)
        )
        return [(r[0], r[1], r[2]) for r in res.all()]

    async def replace_pom_measurements(self, spec_sheet_id, rows: list[PomMeasurement]) -> None:
        from sqlalchemy import delete
        await self.db.execute(
            delete(PomMeasurement).where(PomMeasurement.spec_sheet_id == spec_sheet_id)
        )
        for r in rows:
            self.db.add(r)
        await self.db.commit()

    async def pom_measurements(self, spec_sheet_id) -> list[PomMeasurement]:
        res = await self.db.execute(
            select(PomMeasurement).where(PomMeasurement.spec_sheet_id == spec_sheet_id)
        )
        return list(res.scalars())

    # ── garment_type ──────────────────────────────────────────────────────────
    async def get_garment_type(self, code: str | None) -> GarmentType | None:
        if not code:
            return None
        res = await self.db.execute(select(GarmentType).where(GarmentType.code == code))
        return res.scalar_one_or_none()

    # ── style_consumption_template (the DCM memory) ──────────────────────────
    async def find_consumption_template(self, *, client_id, style_signature,
                                        garment_type_id, material_category, size):
        res = await self.db.execute(
            select(StyleConsumptionTemplate).where(
                StyleConsumptionTemplate.client_id == client_id,
                StyleConsumptionTemplate.style_signature == style_signature,
                StyleConsumptionTemplate.garment_type_id == garment_type_id,
                StyleConsumptionTemplate.material_category == material_category,
                StyleConsumptionTemplate.size == size,
            )
        )
        return res.scalar_one_or_none()

    async def find_similar_template(self, *, client_id, garment_type_id,
                                    material_category, size, exclude_signature):
        stmt = select(StyleConsumptionTemplate).where(
            StyleConsumptionTemplate.client_id == client_id,
            StyleConsumptionTemplate.garment_type_id == garment_type_id,
            StyleConsumptionTemplate.material_category == material_category,
            StyleConsumptionTemplate.size == size,
            StyleConsumptionTemplate.style_signature != exclude_signature,
        ).order_by(StyleConsumptionTemplate.confirmed_at.desc())
        res = await self.db.execute(stmt)
        return res.scalars().first()

    async def get_consumption_template(self, template_id):
        res = await self.db.execute(
            select(StyleConsumptionTemplate).where(StyleConsumptionTemplate.id == template_id)
        )
        return res.scalar_one_or_none()

    async def upsert_consumption_template(self, *, client_id, style_signature,
                                          garment_type_id, material_category, size,
                                          dcm_value, uom, confirmed_by, confirmed_at,
                                          source_bom_id):
        existing = await self.find_consumption_template(
            client_id=client_id, style_signature=style_signature,
            garment_type_id=garment_type_id, material_category=material_category, size=size,
        )
        if existing:
            existing.dcm_value = dcm_value
            existing.uom = uom
            existing.confirmed_by = confirmed_by
            existing.confirmed_at = confirmed_at
            existing.source_bom_id = source_bom_id
            await self.db.commit()
            return existing
        row = StyleConsumptionTemplate(
            client_id=client_id, style_signature=style_signature,
            garment_type_id=garment_type_id, material_category=material_category, size=size,
            dcm_value=dcm_value, uom=uom, confirmed_by=confirmed_by,
            confirmed_at=confirmed_at, source_bom_id=source_bom_id,
        )
        self.db.add(row)
        await self.db.commit()
        return row

    # ── pattern_reference ────────────────────────────────────────────────────
    async def add_pattern_reference(self, ref):
        self.db.add(ref)
        await self.db.commit()
        await self.db.refresh(ref)
        return ref

    # ── spec_sheet ────────────────────────────────────────────────────────────
    async def get_spec_sheet(self, spec_sheet_id):
        res = await self.db.execute(select(SpecSheet).where(SpecSheet.id == spec_sheet_id))
        return res.scalar_one_or_none()

    # ── bom ───────────────────────────────────────────────────────────────────
    async def add_bom(self, bom: Bom) -> Bom:
        self.db.add(bom)
        await self.db.commit()
        await self.db.refresh(bom)
        return bom

    async def get_bom(self, bom_id) -> Bom | None:
        res = await self.db.execute(
            select(Bom).where(Bom.id == bom_id).options(selectinload(Bom.items))
        )
        return res.scalar_one_or_none()

    async def claim_revision(self, bom_id, base_revision: int) -> int:
        from sqlalchemy import update
        res = await self.db.execute(
            update(Bom).where(Bom.id == bom_id, Bom.revision == base_revision)
            .values(revision=base_revision + 1)
        )
        return res.rowcount

    # ══════════════════════════════════════════════════════════════════════
    # Notifications (the BOM-ready notice + 2-hour escalation; PO approval too)
    # ══════════════════════════════════════════════════════════════════════
    async def add_notification(self, n: Notification) -> Notification:
        self.db.add(n)
        await self.db.commit()
        await self.db.refresh(n)
        return n

    async def get_notification(self, notification_id) -> Notification | None:
        res = await self.db.execute(
            select(Notification).where(Notification.id == notification_id))
        return res.scalar_one_or_none()

    async def list_notifications_for_user(self, user_id, *, unread_only=False, limit=100):
        stmt = select(Notification).where(
            Notification.recipient_user_id == user_id,
            Notification.channel == NotificationChannel.IN_APP.value,
        )
        if unread_only:
            stmt = stmt.where(Notification.opened_at.is_(None))
        stmt = stmt.order_by(
            Notification.opened_at.is_(None).desc(), Notification.created_at.desc()
        ).limit(limit)
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def due_escalations(self, now) -> list[Notification]:
        """In-app review/approval notices past their deadline, still unseen, not yet
        escalated to an email child (idempotent NOT-EXISTS guard)."""
        child = Notification.__table__.alias("child")
        not_escalated = ~select(child.c.id).where(
            child.c.parent_notification_id == Notification.id,
            child.c.channel == NotificationChannel.EMAIL.value,
        ).exists()
        stmt = select(Notification).where(
            Notification.channel == NotificationChannel.IN_APP.value,
            Notification.type.in_([NotificationType.BOM_AWAITING_REVIEW.value,
                                   NotificationType.PO_AWAITING_APPROVAL.value]),
            Notification.opened_at.is_(None),
            Notification.scheduled_for.isnot(None),
            Notification.scheduled_for <= now,
            not_escalated,
        )
        res = await self.db.execute(stmt)
        return list(res.scalars())
