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
  spec_extraction:
    add_spec_extraction(row) -> SpecExtraction.
  order_extraction:
    add_order_extraction(row) -> OrderExtraction.
    get_order_extraction(oe_id) -> OrderExtraction | None.
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

from decimal import Decimal
from decimal import Decimal
import hashlib
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import NotificationChannel, NotificationType
from app.core.models import Document, Notification
from app.modules.bom.fabric_roles import attribute_fabrics
from app.modules.bom.models import (
    Bom,
    DxfYieldObservation,
    GarmentType,
    OrderStyle,
    PatternExtraction,
    PatternPiece,
    PatternReference,
    PomDictionary,
    PomMeasurement,
    SpecSheet,
    StyleConsumptionTemplate,
    SpecExtraction,
    OrderExtraction
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
    
    async def replace_pom_measurements_atomic(self, spec_sheet: SpecSheet, pom_rows: list[PomMeasurement]) -> None:
        await self.db.execute(
                delete(PomMeasurement).where(PomMeasurement.spec_sheet_id == spec_sheet.id))
        if pom_rows:
                self.db.add_all(pom_rows)

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

    async def get_bom_by_submission(self, submission_id) -> Bom | None:
        """The (at-most-one, via uq_bom_submission) BOM anchored on a submission. Used by
        the Stage-1→2 trigger to make re-generation idempotent — a prior run that created
        the BOM but didn't flip the submission to consumed is replayed, not duplicated."""
        res = await self.db.execute(
            select(Bom).where(Bom.submission_id == submission_id)
            .options(selectinload(Bom.items))
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
    
    async def add_spec_extraction(self, row: SpecExtraction) -> SpecExtraction:
        """Insert a SpecExtraction row + flush so it has an id. No commit — the
        enclosing service call commits at the right boundary."""
        self.db.add(row)
        await self.db.flush()
        return row
    
    
    async def add_order_extraction(self, row: OrderExtraction) -> OrderExtraction:
        """Insert an OrderExtraction row + flush."""
        self.db.add(row)
        await self.db.flush()
        return row
    
    
    async def get_order_extraction(self, oe_id: uuid.UUID) -> OrderExtraction | None:
        """Lookup by primary key — used to stamp promoted_at after the Bom row
        is created."""
        return await self.db.get(OrderExtraction, oe_id)
    
    
    # ── load: row -> PatternData for the resolver ──────────────────────────────
    async def get_current_pattern(self, style_signature: str,
                                  client_id=None) -> PatternExtraction | None:
        q = select(PatternExtraction).where(
            PatternExtraction.style_signature == style_signature,
            PatternExtraction.is_current.is_(True),
        )
        if client_id is not None:
            q = q.where(PatternExtraction.client_id == client_id)
        return await self.db.scalar(q.order_by(PatternExtraction.created_at.desc()).limit(1))
    
    async def supersede_patterns(self, style_signature: str, client_id=None) -> None:
        q = select(PatternExtraction).where(
            PatternExtraction.style_signature == style_signature,
            PatternExtraction.is_current.is_(True),
        )
        if client_id is not None:
            q = q.where(PatternExtraction.client_id == client_id)
        for p in (await self.db.scalars(q)).all():
            p.is_current = False
        await self.db.flush()
        
    # ── learning loop: yield observations ──────────────────────────────────────
    async def add_yield_observation(self, obs: dict, *, source_bom_id=None,
                                    confirmed_by=None, confirmed_at=None) -> None:
        """Persist one (net area -> confirmed DCM) reconciliation from pattern.learn_yield."""
        self.db.add(DxfYieldObservation(
            style_signature=obs["style"], species=obs["species"], size=obs.get("size"),
            net_qty_sf=Decimal(str(obs["net_qty_sf"])),
            confirmed_dcm_sf=Decimal(str(obs["confirmed_dcm_sf"])),
            implied_yield=Decimal(str(obs["implied_yield"])),
            source_bom_id=source_bom_id, confirmed_by=confirmed_by, confirmed_at=confirmed_at))
        await self.db.flush()

    async def yields_by_species(self) -> dict[str, list[float]]:
        """All implied yields grouped by species -> feeds effective_dxf_yields() so the
        seed bootstrap (2.50/2.10) is replaced by measured means as confirms land."""
        rows = await self.db.execute(
            select(DxfYieldObservation.species, DxfYieldObservation.implied_yield))
        out: dict[str, list[float]] = {}
        for sp, y in rows.all():
            out.setdefault(sp, []).append(float(y))
        return out
    
    
    async def persist_dxf(self, data: bytes, *, style_signature: str | None = None,
                          client_id=None, garment_type_id=None,
                          source_document_id=None, storage_key: str | None = None
                          ) -> tuple[PatternExtraction, list[str]]:
        """Parse DXF bytes, attribute fabrics, write PatternExtraction + its
        PatternPieces, flipping any earlier current pattern of the same style to
        is_current=False. Returns (row, unknown_fabrics). Does NOT commit.
        Idempotency: the (style_signature, sha256) unique constraint makes a re-upload
        of the identical file a duplicate-key error — catch it upstream as a 409/no-op."""
        import os
        import tempfile

        from starlette.concurrency import run_in_threadpool
        from app.modules.bom import dxf_pattern as DP

        sha = hashlib.sha256(data).hexdigest()

        def _parse():
            with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as fh:
                fh.write(data)
                tmp = fh.name
            try:
                return DP.parse_pattern(tmp)
            finally:
                os.remove(tmp)

        parsed = await run_in_threadpool(_parse)
        sig = (style_signature or parsed.style or "").strip().upper()
        if not sig:
            raise ValueError("DXF carries no DESIGN/style tag; pass style_signature.")

        await self.supersede_patterns(sig, client_id=client_id)

        roles, unknown = attribute_fabrics(parsed.fabrics, C.fabric_lexicon())
        fabric_roles = {f: {"role": r.role, "category": r.category, "is_leather": r.is_leather}
                        for f, r in roles.items()}
        warnings = list(parsed.warnings or [])
        if unknown:
            warnings.append("dxf_unknown_fabric:" + "|".join(unknown))

        row = PatternExtraction(
            client_id=client_id, style_signature=sig, garment_type_id=garment_type_id,
            source_document_id=source_document_id, source_system=parsed.source_system,
            parser_version=parsed.parser_version, unit=parsed.unit,
            master_size=parsed.master_size, n_pieces=len(parsed.pieces), sha256=sha,
            storage_key=storage_key, is_current=True, area_matrix=parsed.area_matrix,
            fabric_matrix=parsed.fabric_matrix, fabric_roles=fabric_roles, warnings=warnings,
        )
        row.pieces = [PatternPiece(
            block=p.block, name=p.name, fabric=p.fabric, size=p.size, qty=p.qty,
            net_area_sf=Decimal(str(p.net_area_sf)), longest_cm=Decimal(str(p.longest_cm)),
        ) for p in parsed.pieces]
        self.db.add(row)
        await self.db.flush()
        return row, unknown
    
async def upsert_dxf_yield(self, *, species, factor, note=None):
    from app.modules.bom.models import DxfYield
    row = await self.db.scalar(select(DxfYield).where(DxfYield.species == species))
    if row:
        row.factor, row.note = factor, note
    else:
        row = DxfYield(species=species, factor=factor, note=note)
        self.db.add(row)
    await self.db.commit()
    return row

async def upsert_fabric_role(self, *, label, role, category, is_leather=False):
    from app.modules.bom.models import FabricRoleRow
    row = await self.db.scalar(select(FabricRoleRow).where(FabricRoleRow.label == label))
    if row:
        row.role, row.category, row.is_leather = role, category, is_leather
    else:
        row = FabricRoleRow(label=label, role=role, category=category, is_leather=is_leather)
        self.db.add(row)
    await self.db.commit()
    return row

async def replace_cost_catalog(self, garment_code: str, lines: list[dict]):
        from app.modules.bom.models import CostCatalogLine
        from sqlalchemy import delete
        await self.db.execute(delete(CostCatalogLine).where(
            CostCatalogLine.garment_code == garment_code))
        for i, ln in enumerate(lines):
            self.db.add(CostCatalogLine(garment_code=garment_code, sort_order=i, **ln))
        await self.db.commit()
        
async def replace_client_checks(self, client_code: str, rules: list[dict]):
        from app.modules.bom.models import ClientCheckRule
        from sqlalchemy import delete
        await self.db.execute(delete(ClientCheckRule).where(
            ClientCheckRule.client_code == client_code))
        for i, r in enumerate(rules):
            rng = r.get("range") or [None, None]
            self.db.add(ClientCheckRule(
                client_code=client_code, rule_id=r["id"], kind=r["kind"],
                severity=r.get("severity", "warn"), field=r.get("field"),
                range_lo=rng[0], range_hi=rng[1], params=r.get("params"), sort_order=i))
        await self.db.commit()
        
async def upsert_pom_mapping(self, *, language, source_term, pom_code,
                                 garment_type_id=None, weight=1):
    from app.modules.bom.models import PomDictionary
    row = await self.db.scalar(select(PomDictionary).where(
        PomDictionary.language == language,
        PomDictionary.source_term == source_term,
        PomDictionary.garment_type_id == garment_type_id))
    if row:
        row.pom_code, row.weight = pom_code, weight
    else:
        row = PomDictionary(language=language, source_term=source_term,
                            pom_code=pom_code, garment_type_id=garment_type_id, weight=weight)
        self.db.add(row)
    await self.db.commit()
    return row

async def list_pom_mappings(self):
    from app.modules.bom.models import PomDictionary
    return list(await self.db.scalars(
        select(PomDictionary).order_by(PomDictionary.source_term)))
    
async def get_order_styles(self, submission_id) -> list["OrderStyle"]:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from app.modules.bom.models import OrderStyle
        res = await self.db.execute(
            select(OrderStyle)
            .where(OrderStyle.submission_id == submission_id)
            .options(selectinload(OrderStyle.colors))
            .order_by(OrderStyle.style_name))
        return list(res.scalars().all())

async def get_order_style(self, order_style_id) -> "OrderStyle | None":
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.modules.bom.models import OrderStyle
    res = await self.db.execute(
        select(OrderStyle)
        .where(OrderStyle.id == order_style_id)
        .options(selectinload(OrderStyle.colors)))
    return res.scalar_one_or_none()

async def patterns_for_client(self, client_id) -> list["PatternReference"]:
    from sqlalchemy import select
    from app.modules.bom.models import PatternReference
    stmt = select(PatternReference)
    if client_id is not None:
        stmt = stmt.where(PatternReference.client_id == client_id)
    res = await self.db.execute(stmt.order_by(PatternReference.created_at.desc()))
    return list(res.scalars().all())