"""
================================================================================
modules/procurement/repository.py — Async data access for Stage-1 upload/validation
================================================================================

The ONLY place that talks to the DB for the procurement Stage-1 surface
(submission, document, client_template). Service holds the business rules; this
holds the queries. Mirrors the attendance/production repository style.
================================================================================
"""
from __future__ import annotations

import uuid

from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.procurement.enums import (
    InventoryCheckStatus,
    NotificationChannel,
    NotificationType,
    POStatus,
    ReservationStatus,
)
from app.modules.procurement.models import (
    Bom,
    BomItem,
    ClientTemplate,
    Document,
    GarmentType,
    InventoryCheck,
    InventoryCheckLine,
    InventoryItem,
    InventoryReservation,
    MaterialAlias,
    Notification,
    PatternReference,
    PoItem,
    PomDictionary,
    PomMeasurement,
    PoResponse,
    PoTrackingEvent,
    ProductionTracking,
    PurchaseOrder,
    SpecSheet,
    StyleConsumptionTemplate,
    Submission,
    Supplier,
    SupplierSupplyHistory,
    UomConversion,
)


class ProcurementRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def commit(self) -> None:
        await self.db.commit()

    # ── submission ───────────────────────────────────────────────────────────
    async def create_submission(
        self, *, client_id: uuid.UUID | None, created_by: uuid.UUID | None, status: str
    ) -> Submission:
        sub = Submission(client_id=client_id, created_by=created_by, status=status)
        self.db.add(sub)
        await self.db.commit()
        await self.db.refresh(sub)
        return sub

    async def get_submission(self, submission_id: uuid.UUID) -> Submission | None:
        res = await self.db.execute(select(Submission).where(Submission.id == submission_id))
        return res.scalar_one_or_none()

    async def save(self, obj) -> None:
        await self.db.commit()
        await self.db.refresh(obj)

    # ── document ─────────────────────────────────────────────────────────────
    async def get_document(self, document_id: uuid.UUID) -> Document | None:
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

    # ── client_template registry ─────────────────────────────────────────────
    async def active_templates(self, doc_kind: str | None = None) -> list[ClientTemplate]:
        stmt = select(ClientTemplate).where(ClientTemplate.is_active.is_(True))
        if doc_kind is not None:
            stmt = stmt.where(ClientTemplate.doc_kind == doc_kind)
        res = await self.db.execute(stmt)
        return list(res.scalars())

    # ══════════════════════════════════════════════════════════════════════
    # Stage 2 — extraction, DCM memory, BOM
    # ══════════════════════════════════════════════════════════════════════
    async def add(self, obj):
        self.db.add(obj)
        await self.db.commit()
        await self.db.refresh(obj)
        return obj

    # ── pom_dictionary ────────────────────────────────────────────────────────
    async def pom_dictionary_rows(self) -> list[tuple[str, str, str]]:
        res = await self.db.execute(
            select(PomDictionary.language, PomDictionary.source_term, PomDictionary.pom_code)
        )
        return [(r[0], r[1], r[2]) for r in res.all()]

    # ── pom_measurement (replace-on-key per spec sheet) ──────────────────────
    async def replace_pom_measurements(self, spec_sheet_id: uuid.UUID,
                                       rows: list[PomMeasurement]) -> None:
        await self.db.execute(
            delete(PomMeasurement).where(PomMeasurement.spec_sheet_id == spec_sheet_id)
        )
        for r in rows:
            self.db.add(r)
        await self.db.commit()

    async def pom_measurements(self, spec_sheet_id: uuid.UUID) -> list[PomMeasurement]:
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
    async def find_consumption_template(
        self, *, client_id, style_signature, garment_type_id, material_category, size
    ) -> StyleConsumptionTemplate | None:
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

    async def find_similar_template(
        self, *, client_id, garment_type_id, material_category, size, exclude_signature
    ) -> StyleConsumptionTemplate | None:
        """Source 2 (rule-based): a CONFIRMED template for the same client +
        garment_type + material + size but a DIFFERENT style — the nearest neighbour."""
        stmt = select(StyleConsumptionTemplate).where(
            StyleConsumptionTemplate.client_id == client_id,
            StyleConsumptionTemplate.garment_type_id == garment_type_id,
            StyleConsumptionTemplate.material_category == material_category,
            StyleConsumptionTemplate.size == size,
            StyleConsumptionTemplate.style_signature != exclude_signature,
        ).order_by(StyleConsumptionTemplate.confirmed_at.desc())
        res = await self.db.execute(stmt)
        return res.scalars().first()

    async def get_consumption_template(self, template_id) -> StyleConsumptionTemplate | None:
        res = await self.db.execute(
            select(StyleConsumptionTemplate).where(StyleConsumptionTemplate.id == template_id)
        )
        return res.scalar_one_or_none()

    async def upsert_consumption_template(self, *, client_id, style_signature,
                                          garment_type_id, material_category, size,
                                          dcm_value, uom, confirmed_by, confirmed_at,
                                          source_bom_id) -> StyleConsumptionTemplate:
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
    async def add_pattern_reference(self, ref: PatternReference) -> PatternReference:
        self.db.add(ref)
        await self.db.commit()
        await self.db.refresh(ref)
        return ref

    # ── bom ───────────────────────────────────────────────────────────────────
    async def add_bom(self, bom: Bom) -> Bom:
        self.db.add(bom)
        await self.db.commit()
        await self.db.refresh(bom)
        return bom

    async def get_bom(self, bom_id: uuid.UUID) -> Bom | None:
        res = await self.db.execute(
            select(Bom).where(Bom.id == bom_id).options(selectinload(Bom.items))
        )
        return res.scalar_one_or_none()

    async def claim_revision(self, bom_id: uuid.UUID, base_revision: int) -> int:
        """Optimistic-lock CLAIM: bump revision with a conditional UPDATE so two
        concurrent writers that both read `base_revision` cannot both succeed — the
        loser gets rowcount 0. Returns the affected-row count (1 = claimed, 0 = stale).
        Does NOT commit — the caller owns the transaction so the item edits + this
        bump commit together (atomic bulk edit)."""
        res = await self.db.execute(
            update(Bom)
            .where(Bom.id == bom_id, Bom.revision == base_revision)
            .values(revision=base_revision + 1)
        )
        return res.rowcount

    async def rollback(self) -> None:
        await self.db.rollback()

    async def get_spec_sheet(self, spec_sheet_id: uuid.UUID) -> SpecSheet | None:
        res = await self.db.execute(select(SpecSheet).where(SpecSheet.id == spec_sheet_id))
        return res.scalar_one_or_none()

    # ══════════════════════════════════════════════════════════════════════
    # Stage 3 — notifications (the BOM-ready notice + 2-hour escalation, §2)
    # ══════════════════════════════════════════════════════════════════════
    async def add_notification(self, n: Notification) -> Notification:
        self.db.add(n)
        await self.db.commit()
        await self.db.refresh(n)
        return n

    async def get_notification(self, notification_id: uuid.UUID) -> Notification | None:
        res = await self.db.execute(
            select(Notification).where(Notification.id == notification_id)
        )
        return res.scalar_one_or_none()

    async def list_notifications_for_user(
        self, user_id: uuid.UUID, *, unread_only: bool = False, limit: int = 100
    ) -> list[Notification]:
        """The caller's in-app notifications, unread first then newest first — the
        GET poll fallback + the SSE initial load."""
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
        """In-app BOM-review notices past their `scheduled_for` deadline, still
        UNSEEN (`opened_at` null), not already escalated to an email child. The
        `~exists(...)` guard makes the sweeper idempotent — a second sweep never
        double-emails (stage-3 spec §2c)."""
        # NOT EXISTS (an email child of THIS row). Aliased correlation:
        child_alias = Notification.__table__.alias("child")
        not_escalated = ~select(child_alias.c.id).where(
            child_alias.c.parent_notification_id == Notification.id,
            child_alias.c.channel == NotificationChannel.EMAIL.value,
        ).exists()
        stmt = select(Notification).where(
            Notification.channel == NotificationChannel.IN_APP.value,
            # Stage 5 (§3c): the SAME 2-hour in-app→email fallback nudges the PO
            # cross-check approver, reusing this idempotent sweep verbatim.
            Notification.type.in_([NotificationType.BOM_AWAITING_REVIEW.value,
                                   NotificationType.PO_AWAITING_APPROVAL.value]),
            Notification.opened_at.is_(None),
            Notification.scheduled_for.isnot(None),
            Notification.scheduled_for <= now,
            not_escalated,
        )
        res = await self.db.execute(stmt)
        return list(res.scalars())

    # ══════════════════════════════════════════════════════════════════════
    # Stage 4 — inventory master, matching candidates, reservations, checks
    # ══════════════════════════════════════════════════════════════════════
    # ── inventory_item (the master; the importer upserts by normalized_key) ──
    async def get_inventory_item_by_key(self, normalized_key: str) -> InventoryItem | None:
        res = await self.db.execute(
            select(InventoryItem).where(InventoryItem.normalized_key == normalized_key)
        )
        return res.scalars().first()

    async def get_inventory_item(self, item_id) -> InventoryItem | None:
        res = await self.db.execute(
            select(InventoryItem).where(InventoryItem.id == item_id)
        )
        return res.scalar_one_or_none()

    async def upsert_inventory_item(self, *, normalized_key, description, uom,
                                    qty_on_hand, rate, color) -> InventoryItem:
        """Sync one article (§2c): the sheet wins on qty_on_hand (snapshot replace —
        safe because reservations live in a separate ledger), metadata fills gaps,
        and a row reappearing reactivates."""
        existing = await self.get_inventory_item_by_key(normalized_key)
        if existing:
            existing.qty_on_hand = qty_on_hand
            existing.description = description
            existing.uom = uom or existing.uom
            existing.rate = rate if rate is not None else existing.rate
            existing.color = color or existing.color
            existing.is_active = True
            return existing
        row = InventoryItem(
            description=description, normalized_key=normalized_key, uom=uom,
            qty_on_hand=qty_on_hand, rate=rate, color=color, is_active=True,
        )
        self.db.add(row)
        return row

    async def deactivate_keys_not_in(self, keep_keys: set[str]) -> int:
        """Soft-deactivate articles absent from the new sheet (§2c) — never hard-delete
        (an open check_line / reservation may still FK them). Returns the count."""
        res = await self.db.execute(
            select(InventoryItem).where(InventoryItem.is_active.is_(True))
        )
        n = 0
        for item in res.scalars():
            if item.normalized_key not in keep_keys:
                item.is_active = False
                n += 1
        return n

    async def list_inventory_items(self, *, search: str | None = None,
                                   limit: int = 100, offset: int = 0) -> list[InventoryItem]:
        stmt = select(InventoryItem).where(InventoryItem.is_active.is_(True))
        if search:
            stmt = stmt.where(InventoryItem.normalized_key.like(f"%{search.upper()}%"))
        stmt = stmt.order_by(InventoryItem.description).limit(limit).offset(offset)
        res = await self.db.execute(stmt)
        return list(res.scalars())

    # ── matching candidates (set-based; lock for the reservation claim, §7) ──
    async def fetch_inventory_candidates(self, *, exact_keys: set[str],
                                         like_terms: set[str],
                                         lock: bool = True) -> list[InventoryItem]:
        """One set-based fetch of the small candidate pool for a check — exact keys
        plus alias/fuzzy probe terms — NOT the whole master (§7). `FOR UPDATE` serializes
        two approvals competing for the same stock (no-op on SQLite tests)."""
        conds = []
        if exact_keys:
            conds.append(InventoryItem.normalized_key.in_(exact_keys))
        for t in like_terms:
            if t:
                conds.append(InventoryItem.normalized_key.like(f"%{t}%"))
        if not conds:
            return []
        stmt = select(InventoryItem).where(
            InventoryItem.is_active.is_(True), or_(*conds)
        )
        if lock:
            stmt = stmt.with_for_update()
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def active_aliases(self) -> list[MaterialAlias]:
        res = await self.db.execute(
            select(MaterialAlias).where(MaterialAlias.is_active.is_(True))
        )
        return list(res.scalars())

    async def uom_conversions(self) -> dict[tuple[str, str], object]:
        res = await self.db.execute(select(UomConversion))
        return {(r.from_uom.upper(), r.to_uom.upper()): r.factor for r in res.scalars()}

    # ── reservations (the soft allocation ledger, §3) ─────────────────────────
    async def active_reservation_sums(self, item_ids: list,
                                      exclude_bom_id=None) -> dict:
        """Σ active reservation qty per inventory_item, optionally excluding the BOM
        being (re)checked. Drives `available = qty_on_hand − Σ active(others)`."""
        if not item_ids:
            return {}
        stmt = select(
            InventoryReservation.inventory_item_id,
            func.coalesce(func.sum(InventoryReservation.qty), 0),
        ).where(
            InventoryReservation.inventory_item_id.in_(item_ids),
            InventoryReservation.status == ReservationStatus.ACTIVE.value,
        )
        if exclude_bom_id is not None:
            stmt = stmt.where(InventoryReservation.bom_id != exclude_bom_id)
        stmt = stmt.group_by(InventoryReservation.inventory_item_id)
        res = await self.db.execute(stmt)
        return {row[0]: row[1] for row in res.all()}

    async def release_reservations(self, bom_id, *, reason: str) -> int:
        """Free a BOM's prior claims (§3c): cancel/reopen/re-run. Idempotent — an
        already-released row is left alone. Does NOT commit (caller owns the txn)."""
        res = await self.db.execute(
            update(InventoryReservation)
            .where(InventoryReservation.bom_id == bom_id,
                   InventoryReservation.status == ReservationStatus.ACTIVE.value)
            .values(status=ReservationStatus.RELEASED.value,
                    released_at=datetime.now(timezone.utc), released_reason=reason)
        )
        return res.rowcount

    # ── inventory_check + lines + reservations (one transaction, §7) ──────────
    async def add_inventory_check(self, check: InventoryCheck) -> InventoryCheck:
        self.db.add(check)
        await self.db.commit()
        await self.db.refresh(check)
        return check

    async def get_inventory_check(self, check_id) -> InventoryCheck | None:
        res = await self.db.execute(
            select(InventoryCheck).where(InventoryCheck.id == check_id)
            .options(selectinload(InventoryCheck.lines))
        )
        return res.scalar_one_or_none()

    async def latest_check_for_bom(self, bom_id) -> InventoryCheck | None:
        res = await self.db.execute(
            select(InventoryCheck).where(InventoryCheck.bom_id == bom_id)
            .options(selectinload(InventoryCheck.lines))
            .order_by(InventoryCheck.created_at.desc())
        )
        return res.scalars().first()

    async def latest_checks(self) -> list[InventoryCheck]:
        """Every BOM's most-recent check, newest first — the dashboard source (§8b).
        Grouping by client/order/style happens in the service via clients.service."""
        res = await self.db.execute(
            select(InventoryCheck).options(selectinload(InventoryCheck.lines))
            .order_by(InventoryCheck.created_at.desc())
        )
        seen: set = set()
        out: list[InventoryCheck] = []
        for c in res.scalars():
            if c.bom_id in seen:
                continue
            seen.add(c.bom_id)
            out.append(c)
        return out

    # ══════════════════════════════════════════════════════════════════════
    # Stage 5 — suppliers, supply-history index, purchase orders, tracking
    # ══════════════════════════════════════════════════════════════════════
    # ── supplier (the importer upserts by exact name; CRUD §9) ───────────────
    async def get_supplier(self, supplier_id) -> Supplier | None:
        res = await self.db.execute(
            select(Supplier).where(Supplier.id == supplier_id)
            .options(selectinload(Supplier.supply_history))
        )
        return res.scalar_one_or_none()

    async def get_supplier_by_name(self, name: str) -> Supplier | None:
        res = await self.db.execute(select(Supplier).where(Supplier.name == name))
        return res.scalars().first()

    async def list_suppliers(self, *, search=None, service=None, active=None,
                             limit=100, offset=0) -> list[Supplier]:
        stmt = select(Supplier)
        if active is not None:
            stmt = stmt.where(Supplier.is_active.is_(active))
        if search:
            like = f"%{search}%"
            stmt = stmt.where(or_(Supplier.name.ilike(like), Supplier.service.ilike(like)))
        if service:
            stmt = stmt.where(Supplier.service.ilike(f"%{service}%"))
        stmt = stmt.order_by(Supplier.name).limit(limit).offset(offset)
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def all_active_suppliers(self) -> list[Supplier]:
        res = await self.db.execute(select(Supplier).where(Supplier.is_active.is_(True)))
        return list(res.scalars())

    async def supplier_open_pos(self, supplier_id) -> list[PurchaseOrder]:
        res = await self.db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.supplier_id == supplier_id,
                PurchaseOrder.status.notin_([POStatus.CANCELLED.value, POStatus.CONFIRMED.value]),
            )
        )
        return list(res.scalars())

    # ── supplier_supply_history (the §1d matching index) ─────────────────────
    async def clear_supply_history(self) -> int:
        """Rebuilt wholesale on each import (§1d 'refreshed on re-import')."""
        res = await self.db.execute(delete(SupplierSupplyHistory))
        return res.rowcount or 0

    async def supply_history_for_supplier(self, supplier_id) -> list[SupplierSupplyHistory]:
        res = await self.db.execute(
            select(SupplierSupplyHistory).where(
                SupplierSupplyHistory.supplier_id == supplier_id)
            .order_by(SupplierSupplyHistory.last_purchased_at.desc())
        )
        return list(res.scalars())

    async def fetch_supply_history_candidates(self, *, like_terms: set[str],
                                              modes: set[str] | None = None,
                                              limit: int = 600) -> list[tuple]:
        """The matcher's set-based candidate fetch (§1b): supply-history rows whose
        article contains a probe term OR (for the category fallback) whose mode matches
        the line bucket — joined to an ACTIVE supplier for contactability ranking. NOT
        the whole ledger. Returns (history, supplier) tuples."""
        conds = []
        for t in like_terms:
            if t:
                conds.append(SupplierSupplyHistory.normalized_description.like(f"%{t}%"))
        if modes:
            conds.append(SupplierSupplyHistory.mode.in_(list(modes)))
        if not conds:
            return []
        stmt = (
            select(SupplierSupplyHistory, Supplier)
            .join(Supplier, Supplier.id == SupplierSupplyHistory.supplier_id)
            .where(Supplier.is_active.is_(True), or_(*conds))
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return [(h, s) for (h, s) in res.all()]

    # ── purchase_order ────────────────────────────────────────────────────────
    async def get_po(self, po_id) -> PurchaseOrder | None:
        res = await self.db.execute(
            select(PurchaseOrder).where(PurchaseOrder.id == po_id).options(
                selectinload(PurchaseOrder.items),
                selectinload(PurchaseOrder.responses),
                selectinload(PurchaseOrder.supplier),
            )
        )
        return res.scalar_one_or_none()

    async def get_po_by_token(self, token: str) -> PurchaseOrder | None:
        res = await self.db.execute(
            select(PurchaseOrder).where(PurchaseOrder.tracking_token == token).options(
                selectinload(PurchaseOrder.responses))
        )
        return res.scalars().first()

    async def list_pos(self, *, status=None, needs_supplier=None, bom_id=None,
                       supplier_id=None, limit=200, offset=0) -> list[PurchaseOrder]:
        stmt = select(PurchaseOrder).options(
            selectinload(PurchaseOrder.items), selectinload(PurchaseOrder.supplier))
        if status is not None:
            stmt = stmt.where(PurchaseOrder.status == status)
        if needs_supplier is not None:
            stmt = stmt.where(PurchaseOrder.needs_supplier.is_(needs_supplier))
        if bom_id is not None:
            stmt = stmt.where(PurchaseOrder.bom_id == bom_id)
        if supplier_id is not None:
            stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)
        stmt = stmt.order_by(PurchaseOrder.created_at.desc()).limit(limit).offset(offset)
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def pos_for_bom(self, bom_id) -> list[PurchaseOrder]:
        res = await self.db.execute(
            select(PurchaseOrder).where(PurchaseOrder.bom_id == bom_id).options(
                selectinload(PurchaseOrder.items))
        )
        return list(res.scalars())

    async def claim_po_revision(self, po_id, base_revision: int) -> int:
        """Optimistic-lock CLAIM mirroring `claim_revision` for BOMs (§4). 1 = claimed,
        0 = stale. Does NOT commit — the caller's item edits + this bump commit together."""
        res = await self.db.execute(
            update(PurchaseOrder)
            .where(PurchaseOrder.id == po_id, PurchaseOrder.revision == base_revision)
            .values(revision=base_revision + 1)
        )
        return res.rowcount

    async def next_po_number(self, fy: str) -> str:
        """Allocate the next monotonic NN for a financial year (§2d), formatted
        PO-{NN:02d}({fy}). Scans issued numbers for the FY — a row lock would serialize
        this across replicas (the documented single-writer caveat)."""
        res = await self.db.execute(
            select(PurchaseOrder.po_number).where(
                PurchaseOrder.po_number.like(f"%({fy})"))
        )
        max_n = 0
        for (num,) in res.all():
            if not num:
                continue
            head = num.split("(")[0].replace("PO-", "").strip()
            if head.isdigit():
                max_n = max(max_n, int(head))
        return f"PO-{max_n + 1:02d}({fy})"

    async def get_po_response(self, response_id) -> PoResponse | None:
        res = await self.db.execute(
            select(PoResponse).where(PoResponse.id == response_id))
        return res.scalar_one_or_none()

    async def latest_response(self, po_id) -> PoResponse | None:
        res = await self.db.execute(
            select(PoResponse).where(PoResponse.purchase_order_id == po_id)
            .order_by(PoResponse.created_at.desc())
        )
        return res.scalars().first()

    async def add_tracking_event(self, ev: PoTrackingEvent) -> None:
        self.db.add(ev)

    async def due_po_escalations(self, now) -> list[PurchaseOrder]:
        """POs whose escalation window has lapsed with no acknowledgement and rungs
        left to climb (§7c). The `acknowledged_at IS NULL` + `current_rung < 3` guards
        make a second sweep a no-op (idempotent)."""
        res = await self.db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.status.in_([POStatus.SENT.value, POStatus.ESCALATED.value]),
                PurchaseOrder.acknowledged_at.is_(None),
                PurchaseOrder.current_rung < 3,
                PurchaseOrder.next_escalation_at.isnot(None),
                PurchaseOrder.next_escalation_at <= now,
            ).options(selectinload(PurchaseOrder.supplier),
                      selectinload(PurchaseOrder.items))
        )
        return list(res.scalars())

    # ── production_tracking (§8) ──────────────────────────────────────────────
    async def get_production_tracking(self, client_order_id, style_id) -> ProductionTracking | None:
        res = await self.db.execute(
            select(ProductionTracking).where(
                ProductionTracking.client_order_id == client_order_id,
                ProductionTracking.style_id == style_id)
        )
        return res.scalar_one_or_none()

    async def get_production_tracking_by_id(self, tracking_id) -> ProductionTracking | None:
        res = await self.db.execute(
            select(ProductionTracking).where(ProductionTracking.id == tracking_id))
        return res.scalar_one_or_none()

    async def list_production_tracking(self) -> list[ProductionTracking]:
        res = await self.db.execute(
            select(ProductionTracking).order_by(ProductionTracking.created_at.desc()))
        return list(res.scalars())
