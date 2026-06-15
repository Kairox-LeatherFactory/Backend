"""
================================================================================
modules/bom/service.py — Stage-2/3 BOM generation + approval engine (BomService)
================================================================================

The business brain of Stage 2 (the workflow's engine). Consumes a completed
submission's spec sheet + the resolved order/style identity and produces the
DRAFT `bom` + `bom_item` tree shaped like BMO-1, then governs the editable
contract and the cutting-manager confirmation gate. Owns:

  - extraction (§1): POMs → pom_measurement, prose → SpecSheet.attributes, "follow
    pattern X" → pattern_reference (the heavy openpyxl/LLM work in a threadpool);
  - the DCM resolution policy (§2): the ordered fallback template → similar →
    ai_estimate → manual, every material line STAMPED dcm_source + dcm_confidence;
  - the cost math (§5) via costing.py;
  - the editable bulk-PATCH + optimistic `revision` locking (§7);
  - the cutting-manager confirmation gate (§10) that BACK-FILLS the DCM memory, and
    the Stage-3 approval gate that refuses an unconfirmed BOM (§9/§10).

LAYERING. This service reads only procurement-owned tables (spec_sheet, pom_*,
style_consumption_template, pattern_reference, garment_type, bom*). The order/style
identity it needs (client_id, signature fields, per-size qty) is passed IN by the
caller (resolved via clients.service upstream) — procurement never queries the
clients module's repository, honouring the one non-negotiable rule (CLAUDE.md §3.2).

FUNCTION / METHOD GUIDE  (everything is called from bom/router.py unless noted)
  Dataclasses
    LineSeed       one BOM line BEFORE DCM resolution + costing (extracted/configured input).
    StyleIdentity  the order/style identity passed in by the caller (resolved upstream).
  BomService(db)   holds the AsyncSession + a BomRepository.
  get_bom_dto(bom_id) -> SimpleNamespace | None
      A session-free BOM snapshot for OTHER modules (inventory check, PO generation,
      production board) so they never touch this repo/models. Returns None if absent.
  generate_bom(user, *, spec_sheet, spec_bytes, filename, identity, client_match_code,
               line_seeds, ...) -> dict
      The Stage-2 engine. Steps: select adapter → extract POMs/attrs/pattern (threadpool)
      → persist pom_measurement → merge spec attributes → resolve pattern reference →
      build bom_items with _resolve_dcm + costing → run cross-checks → audit BOM_GENERATE.
      Returns {bom: view, flags, extraction}. CALLED FROM: the generate endpoint / pipeline.
  _resolve_dcm(...) -> (Decimal|None, DcmSource|None)   [private]
      The ordered DCM fallback: Source 1 template → 1b pattern-template → 2 similar-style
      → 3 ai_estimate. Returns (value, source) or (None, None) when manual confirm is needed.
  _poms_by_size(poms) -> dict   [private] reshape POM list → {size: {pom_code: value}}.
  edit_bom_items(user, bom_id, base_revision, edits) -> dict
      The editable contract (Stage 2 §7 / Stage 3 §3): bulk edit guarded by optimistic
      `revision` locking. Validates state (draft/ready_for_review only), applies edits
      (a DCM edit → dcm_source=manual; a late DCM edit re-opens the cutting gate → draft),
      recomputes, does an atomic revision CAS (claim_revision → 409 on stale), audits
      BOM_EDIT. Returns {revision, recomputed, reconfirm_required}.
  confirm_cutting(user, bom_id, identity?) -> dict
      The §10 cutting gate. Stamps cutting_confirmed_*, flips status→ready_for_review,
      BACK-FILLS style_consumption_template for every confirmed material line (the DCM
      memory learns), audits BOM_SUBMIT_FOR_REVIEW, and fires the Stage-3 review
      notifications. Returns {bom_id, status, templates_backfilled, notifications_created}.
  approve_bom(user, bom_id, *, lock=False) -> dict
      Stage-3 MD approve. REFUSES if cutting_confirmed_at is null (409). Stamps
      approved_by/at + locked_at, status→approved/locked, audits BOM_APPROVE, then
      best-effort advances the production board AND auto-fires the Stage-4 inventory
      check (a check failure never rolls back a valid approval). Returns
      {bom_id, status, inventory_check_id}.
  reject_bom(user, bom_id, *, reason) -> dict   reason mandatory; status→rejected; audit BOM_REJECT.
  reopen_bom(user, bom_id) -> dict   rejected→draft, revision++, clears rejection + cutting confirm.
  export_bom(user, bom_id) -> dict
      Stage-3 §4 PDF export from an approved/locked BOM. Renders the CURRENT persisted
      rows (threadpool), dedupes on sha256, stores a Document(kind=bom_quote), links it
      via export_document_id, status→exported, audit BOM_EXPORT. Idempotent re-export.
  get_bom(bom_id) -> dict   the _bom_view of a loaded BOM.
  Helpers [private]: _load_bom (404 if absent), _resolve_identity (via clients.service),
      _base_size, _recompute (delegates to costing.recompute_bom), _bom_snapshot (audit
      diff), _bom_view (the API/PDF dict), _audit (write one AuditLog row + commit).
================================================================================
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.modules.bom import checks as checks_mod
from app.modules.bom import costing
from app.modules.bom.dcm import (
    CONFIDENCE,
    estimate_area_dcm,
    style_signature,
)
from app.core.enums import DocumentKind
from app.modules.bom.enums import (
    BomItemCategory,
    BomStatus,
    DcmSource,
    ExtractionSource,
)
from app.modules.bom.extraction import (
    AttributeExtractor,
    PomDict,
    extract_spec,
    select_adapter,
)
from app.modules.bom.export import render_bom_pdf
from app.core.models import AuditLog, Document
from app.modules.bom.models import (
    Bom,
    BomItem,
    PatternReference,
    PomMeasurement,
)
from app.modules.bom.repository import BomRepository
from app.core.storage import get_storage

# Which categories get DCM-resolved (leather AREA materials, in dm²). Threads /
# accessories / manufacturing / packaging / FOB carry given qty + price, no DCM (§2).
MATERIAL_DCM_CATEGORIES = {
    BomItemCategory.MAIN_MATERIAL.value,
    BomItemCategory.SUB_MATERIAL.value,
    BomItemCategory.LINING.value,
    BomItemCategory.INTERLINING.value,
}

logger = logging.getLogger(__name__)


@dataclass
class LineSeed:
    """One BOM line BEFORE DCM resolution + costing — the materials/accessories/cost
    lines extracted from the spec materials block + the quote sheet + catalog
    defaults (§5b: a mix of extracted / configured). Material lines have their
    qty_per_garment RESOLVED by the §2 policy; others keep the given value."""
    category: str
    name: str
    material_color: str | None = None
    uom: str | None = None
    unit_price: Decimal | float | None = None
    qty_per_garment: Decimal | float | None = None   # non-material / buyer-supplied
    annotation: str | None = None
    source_ref: str | None = None
    supplied_by: str | None = None                    # client/buyer → unit_price 0


@dataclass
class StyleIdentity:
    """The order/style identity passed in by the caller (resolved upstream via
    clients.service) — procurement does not query the clients module for it."""
    client_id: uuid.UUID
    client_order_id: uuid.UUID
    style_id: uuid.UUID
    customer_ref: str | None = None
    internal_ref: str | None = None
    name: str | None = None
    order_qty: int = 0
    per_size_qty: dict = field(default_factory=dict)   # {"S": 12, ...} for the §8 qty check
    order_number: str | None = None                    # for the Stage-3 review notice/PDF


class BomService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = BomRepository(db)

    async def get_bom_dto(self, bom_id):
        """A session-free BOM snapshot for cross-module consumers (inventory check,
        supplier-PO generation, the production board) — the strict-DTO boundary so they
        never touch the bom repository/models. Returns None if the BOM is absent."""
        from types import SimpleNamespace
        bom = await self.repo.get_bom(bom_id)
        if bom is None:
            return None
        return SimpleNamespace(
            id=bom.id, status=bom.status, client_order_id=bom.client_order_id,
            style_id=bom.style_id, order_qty=bom.order_qty,
            items=[SimpleNamespace(
                id=i.id, category=i.category, name=i.name,
                material_color=i.material_color, uom=i.uom, bulk_qty=i.bulk_qty,
                unit_price=i.unit_price) for i in bom.items],
        )

    # ══════════════════════════════════════════════════════════════════════
    # Generation (§1, §2, §5, §6)
    # ══════════════════════════════════════════════════════════════════════
    async def generate_bom(
        self, user, *, spec_sheet, spec_bytes: bytes, filename: str,
        identity: StyleIdentity, client_match_code: str | None,
        line_seeds: list[LineSeed], currency: str | None = None,
        extractor: AttributeExtractor | None = None,
        adapters: list | tuple | None = None, checks_cfg: tuple | None = None,
    ) -> dict:
        # `adapters`/`checks_cfg` default to None → the shipped YAML registries.
        # Passing them in lets a brand-new client be onboarded by CONFIG ALONE (the
        # prod path being a YAML entry the loaders read) — proves §11.6 end-to-end.
        # ── 1. extract POMs / attributes / pattern ref (threadpool) ───────────
        logger.info("generate_bom start: order=%s style=%s spec_type=%s client_match=%s seeds=%d",
                    identity.client_order_id, identity.style_id, spec_sheet.spec_type,
                    client_match_code, len(line_seeds))
        adapter = select_adapter(spec_sheet.spec_type, client_match_code, adapters)
        pom_dict = PomDict(await self.repo.pom_dictionary_rows())
        intermediate = await run_in_threadpool(
            extract_spec, spec_bytes, filename, adapter, pom_dict,
            spec_sheet_id=str(spec_sheet.id), extractor=extractor,
        )

        # ── 2. persist pom_measurement rows (replace-on-key) ──────────────────
        pom_rows: list[PomMeasurement] = []
        for p in intermediate["poms"]:
            for size, value in p["by_size"].items():
                pom_rows.append(PomMeasurement(
                    spec_sheet_id=spec_sheet.id, size=size, pom_code=p["pom_code"],
                    value=Decimal(str(value)),
                    pitch=Decimal(str(p["pitch"])) if p.get("pitch") is not None else None,
                    source_term=p.get("source_term"),
                    extracted_by=p.get("extracted_by", ExtractionSource.DETERMINISTIC.value),
                    confidence=Decimal(str(p.get("confidence", 0.99))),
                ))
        await self.repo.replace_pom_measurements(spec_sheet.id, pom_rows)

        # ── 3. merge extracted attributes into the spec sheet ─────────────────
        if intermediate.get("attributes"):
            spec_sheet.attributes = {**(spec_sheet.attributes or {}),
                                     **intermediate["attributes"]}
            await self.repo.save(spec_sheet)

        # ── 4. pattern reference (resolve to template memory if possible) ─────
        pattern_template_id = None
        pr_block = None
        if intermediate.get("pattern_reference"):
            pr = intermediate["pattern_reference"]
            ref = await self.repo.add_pattern_reference(PatternReference(
                client_id=identity.client_id, spec_sheet_id=spec_sheet.id,
                pattern_code=pr["pattern_code"], base_size=pr.get("base_size"),
                notes=pr.get("source_term"),
            ))
            # Try to resolve to a confirmed template keyed on the pattern code as a
            # signature (the base style's confirmed consumption — §2 Source 1 path).
            tmpl = await self.repo.find_consumption_template(
                client_id=identity.client_id,
                style_signature=pr["pattern_code"].strip().upper(),
                garment_type_id=None, material_category=BomItemCategory.MAIN_MATERIAL.value,
                size=pr.get("base_size") or "",
            )
            if tmpl:
                ref.resolved_template_id = tmpl.id
                pattern_template_id = tmpl.id
                await self.repo.save(ref)
            pr_block = {"pattern_code": pr["pattern_code"],
                        "resolved": ref.resolved_template_id is not None
                        or ref.resolved_style_id is not None}

        # ── 5. resolution context ─────────────────────────────────────────────
        gt = await self.repo.get_garment_type(intermediate.get("garment_type_guess"))
        gt_id = gt.id if gt else None
        sig = style_signature(customer_ref=identity.customer_ref,
                              internal_ref=identity.internal_ref, name=identity.name)
        sizes = intermediate.get("sizes") or []
        base_size = (intermediate.get("pattern_reference") or {}).get("base_size") \
            or (sizes[0] if sizes else None) \
            or (intermediate.get("attributes") or {}).get("sms_size")
        poms_for_size = self._poms_by_size(intermediate["poms"])

        # ── 6. build bom_items with DCM resolution + costing ──────────────────
        bom = Bom(
            client_order_id=identity.client_order_id, style_id=identity.style_id,
            status=BomStatus.DRAFT.value, currency=currency,
            order_qty=identity.order_qty, revision=1,
            garment_type_id=gt_id, dcm_base_size=base_size,
        )

        items: list[BomItem] = []
        for seed in line_seeds:
            price = Decimal("0") if (seed.supplied_by or "").lower() in ("client", "buyer") \
                else (Decimal(str(seed.unit_price)) if seed.unit_price is not None else Decimal("0"))
            dcm_source = None
            dcm_conf = None
            if seed.category in MATERIAL_DCM_CATEGORIES:
                dcm, src = await self._resolve_dcm(
                    client_id=identity.client_id, style_signature=sig, garment_type_id=gt_id,
                    garment_type_row=gt, material_category=seed.category, size=base_size,
                    poms_for_size=poms_for_size, pattern_template_id=pattern_template_id,
                )
                qpg = dcm
                if src is not None:
                    dcm_source = src.value
                    dcm_conf = CONFIDENCE[src]
            else:
                qpg = Decimal(str(seed.qty_per_garment)) if seed.qty_per_garment is not None \
                    else Decimal("1")

            item = BomItem(
                category=seed.category, name=seed.name, material_color=seed.material_color,
                qty_per_garment=qpg, uom=seed.uom, unit_price=price,
                annotation=seed.annotation, source_ref=seed.source_ref,
                dcm_source=dcm_source, dcm_confidence=dcm_conf,
            )
            items.append(item)

        # cost rollup
        self._recompute(bom, items, base_size)
        bom.items = items
        bom.source_document_id = spec_sheet.source_document_id
        await self.repo.add_bom(bom)
        # reload with the items collection eagerly loaded — after commit the
        # relationship is unloaded, and a lazy load under async raises MissingGreenlet.
        bom = await self.repo.get_bom(bom.id)

        # ── 7. cross-checks (§8) ──────────────────────────────────────────────
        ctx = {
            "attributes": spec_sheet.attributes or {},
            "poms": intermediate["poms"], "sizes": sizes,
            "per_size_qty": identity.per_size_qty, "order_qty": identity.order_qty,
            "pattern_reference": pr_block,
        }
        flags = checks_mod.run_checks(client_match_code, ctx, checks_cfg)

        await self._audit(user, "BOM_GENERATE", bom.id, after=self._bom_snapshot(bom))
        logger.info("generate_bom done: bom=%s items=%d fob=%s flags=%d unresolved=%d",
                    bom.id, len(bom.items), bom.garment_fob_price, len(flags),
                    len(intermediate["unresolved"]))
        return {"bom": self._bom_view(bom), "flags": flags,
                "extraction": {"poms": len(pom_rows), "unresolved": intermediate["unresolved"],
                               "pattern_reference": pr_block}}

    # ── the ordered DCM fallback (§2 Sources 1→3; Source 4 is the edit/confirm) ─
    async def _resolve_dcm(self, *, client_id, style_signature, garment_type_id,
                           garment_type_row, material_category, size, poms_for_size,
                           pattern_template_id):
        # Source 1 — the DCM memory (exact, confirmed)
        if size is not None:
            tmpl = await self.repo.find_consumption_template(
                client_id=client_id, style_signature=style_signature,
                garment_type_id=garment_type_id, material_category=material_category, size=size,
            )
            if tmpl:
                return Decimal(str(tmpl.dcm_value)), DcmSource.TEMPLATE
        # Source 1b — via a resolved pattern reference's template
        if pattern_template_id is not None:
            tmpl = await self.repo.get_consumption_template(pattern_template_id)
            if tmpl and tmpl.material_category == material_category:
                return Decimal(str(tmpl.dcm_value)), DcmSource.TEMPLATE
        # Source 2 — similar style (rule-based nearest neighbour)
        if size is not None:
            sim = await self.repo.find_similar_template(
                client_id=client_id, garment_type_id=garment_type_id,
                material_category=material_category, size=size, exclude_signature=style_signature,
            )
            if sim:
                return Decimal(str(sim.dcm_value)), DcmSource.SIMILAR_STYLE
        # Source 3 — AI heuristic from POM area (last resort, FLAGGED)
        est = estimate_area_dcm(
            getattr(garment_type_row, "area_formula", None) if garment_type_row else None,
            getattr(garment_type_row, "default_wastage_pct", None) if garment_type_row else None,
            poms_for_size.get(size, {}) if size else {},
        )
        if est is not None and est > 0:
            return est, DcmSource.AI_ESTIMATE
        # nothing resolved → manual confirmation required (no silent value)
        return None, None

    @staticmethod
    def _poms_by_size(poms: list[dict]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for p in poms:
            for size, val in (p.get("by_size") or {}).items():
                out.setdefault(size, {})[p["pom_code"]] = val
        return out

    # ══════════════════════════════════════════════════════════════════════
    # Editable contract — bulk PATCH + optimistic revision locking (§7)
    # ══════════════════════════════════════════════════════════════════════
    _EDIT_FIELDS = {"dcm", "qty_per_garment", "unit_price"}
    _EDITABLE_STATES = {BomStatus.DRAFT.value, BomStatus.READY_FOR_REVIEW.value}

    async def edit_bom_items(self, user, bom_id: uuid.UUID, base_revision: int,
                             edits: list[dict]) -> dict:
        bom = await self._load_bom(bom_id)
        # Stage 3 (§3): editable only in draft / ready_for_review. An approved/locked/
        # exported BOM is frozen; a rejected BOM must be reopened first. (Same Stage-2
        # bulk-PATCH contract — only the status-aware guard is new.)
        if bom.status not in self._EDITABLE_STATES:
            if bom.status == BomStatus.REJECTED.value:
                raise HTTPException(409, detail={
                    "error": "bom_rejected",
                    "message": "Reopen the rejected BOM before editing."})
            raise HTTPException(409, detail={"error": "bom_locked",
                                             "message": "A locked/approved BOM rejects all edits."})
        if bom.revision != base_revision:
            raise HTTPException(409, detail={"error": "stale_revision",
                                             "current_revision": bom.revision})

        before = self._bom_snapshot(bom)
        by_id = {str(i.id): i for i in bom.items}
        dcm_changed = False
        for e in edits:
            item = by_id.get(str(e.get("bom_item_id")))
            if item is None:
                raise HTTPException(422, detail={"error": "unknown_bom_item",
                                                 "bom_item_id": e.get("bom_item_id")})
            field_ = e.get("field")
            if field_ not in self._EDIT_FIELDS:
                raise HTTPException(422, detail={"error": "unsupported_field", "field": field_})
            try:
                value = Decimal(str(e.get("value")))
            except (InvalidOperation, TypeError, ValueError):
                raise HTTPException(422, detail={"error": "invalid_value",
                                                 "bom_item_id": e.get("bom_item_id"),
                                                 "field": field_, "value": e.get("value")})
            if field_ in ("dcm", "qty_per_garment"):
                item.qty_per_garment = value
                # a human-entered DCM is Source 4 (manual, conf 1.00) — §2 source of truth
                item.dcm_source = DcmSource.MANUAL.value
                item.dcm_confidence = CONFIDENCE[DcmSource.MANUAL]
                dcm_changed = True
            elif field_ == "unit_price":
                item.unit_price = value

        # re-gate on a late DCM edit (§9/§10): a confirmed number can never be
        # silently changed underneath the MD — clear the stamp, force re-confirm.
        # reconfirm_required is True ONLY when an edit actually re-opened a gate that
        # was already confirmed (not for a manual DCM on a never-confirmed draft).
        was_confirmed = bom.cutting_confirmed_at is not None
        reconfirm_required = dcm_changed and was_confirmed
        if reconfirm_required:
            bom.cutting_confirmed_at = None
            bom.cutting_confirmed_by = None
            # Stage 3 (§1b): re-opening the cutting gate sends the BOM back to draft —
            # a confirmed number can never change underneath the MD while it sits in
            # `ready_for_review`. It must be re-confirmed before the next review.
            bom.status = BomStatus.DRAFT.value

        self._recompute(bom, bom.items, self._base_size(bom))

        # Optimistic-lock CLAIM (atomic CAS in the repository): the item edits + the
        # recompute (dirty ORM rows) flush alongside the conditional revision bump and
        # commit in ONE transaction, so the whole bulk edit stays atomic. A concurrent
        # writer that already moved the revision makes this claim affect 0 rows → 409.
        if await self.repo.claim_revision(bom_id, base_revision) == 0:
            await self.repo.rollback()
            fresh = await self.repo.get_bom(bom_id)
            raise HTTPException(409, detail={
                "error": "stale_revision",
                "current_revision": fresh.revision if fresh else None})
        await self.repo.commit()
        # reload eagerly — after commit the items collection is unloaded, and a lazy
        # load under async raises MissingGreenlet.
        bom = await self.repo.get_bom(bom.id)
        await self._audit(user, "BOM_EDIT", bom.id, before=before, after=self._bom_snapshot(bom))
        return {"revision": bom.revision, "recomputed": self._bom_view(bom),
                "reconfirm_required": reconfirm_required}

    # ══════════════════════════════════════════════════════════════════════
    # Cutting-manager confirmation gate (§10) + Stage-3 approval gate (§9/§10)
    # ══════════════════════════════════════════════════════════════════════
    async def confirm_cutting(self, user, bom_id: uuid.UUID,
                              identity: StyleIdentity | None = None) -> dict:
        bom = await self._load_bom(bom_id)
        if bom.status == BomStatus.LOCKED.value:
            raise HTTPException(409, detail={"error": "bom_locked"})
        now = datetime.now(timezone.utc)
        bom.cutting_confirmed_by = getattr(user, "id", None)
        bom.cutting_confirmed_at = now
        # Stage 3 (§1b): cutting confirmation advances the BOM to `ready_for_review`.
        bom.status = BomStatus.READY_FOR_REVIEW.value
        await self.repo.save(bom)

        # back-fill the DCM memory for every CONFIRMED material line so the next
        # order of this style is a Source-1 template hit (§2 Source 4, §10 learning).
        # Keyed IDENTICALLY to generation (same garment_type_id + base size off the
        # bom header) so the second order actually hits its own confirmed value.
        if identity is None:
            identity = await self._resolve_identity(bom)
        backfilled = 0
        if identity is not None:
            sig = style_signature(customer_ref=identity.customer_ref,
                                  internal_ref=identity.internal_ref, name=identity.name)
            base_size = bom.dcm_base_size
            gt_id = bom.garment_type_id
            for item in bom.items:
                if item.category in MATERIAL_DCM_CATEGORIES and item.qty_per_garment is not None:
                    await self.repo.upsert_consumption_template(
                        client_id=identity.client_id, style_signature=sig,
                        garment_type_id=gt_id, material_category=item.category,
                        size=base_size or "", dcm_value=item.qty_per_garment,
                        uom=item.uom, confirmed_by=getattr(user, "id", None),
                        confirmed_at=now, source_bom_id=bom.id,
                    )
                    backfilled += 1
        await self._audit(user, "BOM_SUBMIT_FOR_REVIEW", bom.id,
                          after={"status": bom.status,
                                 "cutting_confirmed_at": now.isoformat(),
                                 "templates_backfilled": backfilled})

        # Stage 3 (§2): entering `ready_for_review` notifies the MD (+ DM) in-app,
        # each with a 2-hour auto-email escalation. Recipients resolve via the users
        # service; with none seeded (unit tests) this is a no-op.
        from app.modules.bom.notification_service import NotificationService

        notes = await NotificationService(self.db).create_review_notifications(
            bom.id,
            style_name=getattr(identity, "name", None) if identity else None,
            order_number=getattr(identity, "order_number", None) if identity else None,
        )
        return {"bom_id": str(bom.id), "status": bom.status,
                "cutting_confirmed_at": now.isoformat(),
                "templates_backfilled": backfilled,
                "notifications_created": len(notes)}

    async def approve_bom(self, user, bom_id: uuid.UUID, *, lock: bool = False) -> dict:
        """Stage-3 MD approve/lock. REFUSES a BOM whose cutting_confirmed_at is null —
        the mandatory §10 gate sits between generation and approval."""
        bom = await self._load_bom(bom_id)
        if bom.cutting_confirmed_at is None:
            raise HTTPException(409, detail={
                "error": "cutting_confirmation_required",
                "message": "The cutting manager must confirm the BOM before approval."})
        before = self._bom_snapshot(bom)
        now = datetime.now(timezone.utc)
        # Stage 3 (§1a): approval ALWAYS locks (the approved BOM is immutable —
        # `locked_at` stamped regardless). The `lock` flag only selects the surfaced
        # status label (LOCKED vs APPROVED); both reject edits via _EDITABLE_STATES.
        bom.status = BomStatus.LOCKED.value if lock else BomStatus.APPROVED.value
        bom.approved_by = getattr(user, "id", None)
        bom.approved_at = now
        bom.locked_at = now
        await self.repo.save(bom)
        await self._audit(user, "BOM_APPROVE", bom.id, before=before,
                          after={"status": bom.status, "approved_at": now.isoformat(),
                                 "locked_at": now.isoformat(), "revision": bom.revision})
        # Stage 5 (§8c): advance the production board BOM_APPROVED edge as a service→service
        # side-effect. Best-effort — the board never blocks a valid approval.
        try:
            from app.modules.supplier_po.production_tracking_service import (
                ProductionTrackingService,
            )
            await ProductionTrackingService(self.db).on_bom_approved(bom.id)
        except Exception:
            pass
        # Stage 4 (§8c): approval auto-fires the inventory check so the freshly approved
        # BOM lands on the procurement dashboard with its stock badge without a manual
        # step. Best-effort — a check failure must not roll back a valid approval; the
        # explicit POST /boms/{id}/inventory-check re-run is always available.
        inventory_check_id = None
        try:
            from app.modules.inventory.service import InventoryService

            res = await InventoryService(self.db).run_check(user, bom.id)
            inventory_check_id = res.get("inventory_check_id")
        except Exception:
            pass
        return {"bom_id": str(bom.id), "status": bom.status,
                "inventory_check_id": inventory_check_id}

    async def reject_bom(self, user, bom_id: uuid.UUID, *, reason: str) -> dict:
        """Stage-3 MD reject (§1b). Only from `ready_for_review`; a reason is
        mandatory and is captured in the BOM_REJECT audit `after`."""
        if not reason or not reason.strip():
            raise HTTPException(422, detail={"error": "reason_required",
                                             "message": "A rejection reason is required."})
        bom = await self._load_bom(bom_id)
        if bom.status in (BomStatus.APPROVED.value, BomStatus.LOCKED.value,
                          BomStatus.EXPORTED.value):
            raise HTTPException(409, detail={"error": "bom_locked",
                                             "message": "An approved BOM cannot be rejected."})
        if bom.status != BomStatus.READY_FOR_REVIEW.value:
            raise HTTPException(409, detail={"error": "not_ready_for_review",
                                             "current_status": bom.status})
        before = self._bom_snapshot(bom)
        now = datetime.now(timezone.utc)
        bom.status = BomStatus.REJECTED.value
        bom.rejected_by = getattr(user, "id", None)
        bom.rejected_at = now
        bom.rejection_reason = reason.strip()
        await self.repo.save(bom)
        await self._audit(user, "BOM_REJECT", bom.id, before=before,
                          after={"status": bom.status, "rejected_at": now.isoformat(),
                                 "rejection_reason": bom.rejection_reason})
        return {"bom_id": str(bom.id), "status": bom.status,
                "rejection_reason": bom.rejection_reason}

    async def reopen_bom(self, user, bom_id: uuid.UUID) -> dict:
        """Stage-3 reopen a rejected BOM for rework (§1b): → `draft`, `revision++`,
        clears the rejection + the cutting confirmation (re-confirmation required)."""
        bom = await self._load_bom(bom_id)
        if bom.status != BomStatus.REJECTED.value:
            raise HTTPException(409, detail={"error": "not_rejected",
                                             "current_status": bom.status})
        before = self._bom_snapshot(bom)
        bom.status = BomStatus.DRAFT.value
        bom.revision = (bom.revision or 1) + 1
        bom.rejected_by = None
        bom.rejected_at = None
        bom.rejection_reason = None
        bom.cutting_confirmed_by = None
        bom.cutting_confirmed_at = None
        await self.repo.save(bom)
        await self._audit(user, "BOM_REOPEN", bom.id, before=before,
                          after={"status": bom.status, "revision": bom.revision})
        return {"bom_id": str(bom.id), "status": bom.status, "revision": bom.revision}

    async def export_bom(self, user, bom_id: uuid.UUID) -> dict:
        """Stage-3 PDF export (§4). Renders the CURRENT persisted BOM (post-edit
        source of truth) from an approved/locked BOM, stores it as a sha256-deduped
        Document(kind=bom_quote), links it via `bom.export_document_id`, → `exported`.
        Re-export of an unchanged BOM is idempotent on the rendered sha256."""
        bom = await self._load_bom(bom_id)
        if bom.status not in (BomStatus.APPROVED.value, BomStatus.LOCKED.value,
                              BomStatus.EXPORTED.value):
            raise HTTPException(409, detail={
                "error": "not_approved",
                "message": "Export is only allowed from an approved/locked BOM."})

        # Idempotent re-export (§4b): an already-exported BOM is immutable, so re-export
        # returns the SAME stored Document. (PDF renderers embed a generation timestamp,
        # so the bytes — hence the sha — are not reproducible across runs; the BOM's
        # locked state, not a byte-compare, is the reliable idempotency key.)
        if bom.status == BomStatus.EXPORTED.value and bom.export_document_id is not None:
            doc = await self.repo.get_document(bom.export_document_id)
            if doc is not None:
                return {"bom_id": str(bom.id), "status": bom.status,
                        "export_document_id": str(doc.id), "sha256": doc.sha256,
                        "mime": doc.mime, "storage_url": doc.storage_url}

        view = self._bom_view(bom)
        identity = await self._resolve_identity(bom)
        approver = None
        if bom.approved_by is not None:
            from app.modules.users.service import UserService
            approver = await UserService(self.db).get(bom.approved_by)
        now = datetime.now(timezone.utc)
        meta = {
            "style_name": getattr(identity, "name", None) if identity else None,
            "order_number": getattr(identity, "order_number", None) if identity else None,
            "approved_by": getattr(approver, "name", None),
            "approved_at": bom.approved_at.isoformat() if bom.approved_at else None,
            "generated_at": now.isoformat(),
        }

        # render (blocking → threadpool, per the house async rule)
        pdf_bytes, mime, ext = await run_in_threadpool(render_bom_pdf, view, meta)
        sha = hashlib.sha256(pdf_bytes).hexdigest()

        # idempotent on sha256 (mirrors the Stage-1 dedupe) — a byte-identical render
        # of an unchanged BOM reuses the existing Document, no duplicate row/object.
        doc = await self.repo.get_document_by_sha(sha)
        if doc is None:
            key = f"exports/{bom.id}/{sha}{ext}"
            try:
                storage_url = get_storage().put(key, pdf_bytes)
            except Exception:
                storage_url = None
            doc = await self.repo.add_document(Document(
                client_id=None,
                kind=DocumentKind.BOM_QUOTE.value,
                filename=f"BOM-{bom.id}{ext}",
                mime=mime,
                storage_url=storage_url,
                sha256=sha,
                size_bytes=len(pdf_bytes),
                uploaded_by=getattr(user, "id", None),
            ))

        bom.export_document_id = doc.id
        bom.exported_at = now
        bom.status = BomStatus.EXPORTED.value
        await self.repo.save(bom)
        await self._audit(user, "BOM_EXPORT", bom.id,
                          after={"export_document_id": str(doc.id), "sha256": sha,
                                 "exported_at": now.isoformat(), "revision": bom.revision,
                                 "mime": mime})
        return {"bom_id": str(bom.id), "status": bom.status,
                "export_document_id": str(doc.id), "sha256": sha,
                "mime": mime, "storage_url": doc.storage_url}

    async def get_bom(self, bom_id: uuid.UUID) -> dict:
        return self._bom_view(await self._load_bom(bom_id))

    # ══════════════════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════════════════
    async def _load_bom(self, bom_id: uuid.UUID) -> Bom:
        bom = await self.repo.get_bom(bom_id)
        if bom is None:
            raise HTTPException(404, "BOM not found.")
        return bom

    async def _resolve_identity(self, bom: Bom) -> "StyleIdentity | None":
        """Resolve order/style identity for the DCM back-fill via the clients service
        (a permitted service→service call) when the caller didn't pass it in. Reads
        the style's stable refs + the order's client + the per-size quantities."""
        from app.modules.clients.service import ClientService

        cs = ClientService(self.db)
        style = await cs.get_style(bom.style_id)
        if style is None:
            return None
        order = await cs.get_order(bom.client_order_id)
        if order is None:
            return None
        skus = await cs.get_skus_for_style(bom.style_id)
        per_size = {}
        for s in skus:
            per_size[s.size] = per_size.get(s.size, 0) + (s.qty_ordered or 0)
        return StyleIdentity(
            client_id=order.client_id, client_order_id=order.id, style_id=style.id,
            customer_ref=style.customer_ref, internal_ref=style.internal_ref,
            name=style.name, order_qty=bom.order_qty or 0, per_size_qty=per_size,
            order_number=getattr(order, "order_number", None),
        )

    @staticmethod
    def _base_size(bom: Bom) -> str | None:
        return bom.dcm_base_size

    def _recompute(self, bom: Bom, items: list[BomItem], base_size) -> None:
        result = costing.recompute_bom(
            [{"qty_per_garment": i.qty_per_garment, "unit_price": i.unit_price} for i in items],
            bom.order_qty or 0,
        )
        for item, derived in zip(items, result["lines"]):
            item.total_cost = derived["total_cost"]
            item.bulk_qty = derived["bulk_qty"]
        bom.garment_fob_price = result["garment_fob_price"]
        bom.bulk_total = result["bulk_total"]

    @staticmethod
    def _bom_snapshot(bom: Bom) -> dict:
        return {
            "revision": bom.revision, "status": bom.status,
            "garment_fob_price": str(bom.garment_fob_price),
            "bulk_total": str(bom.bulk_total),
            "items": [{"id": str(i.id), "name": i.name,
                       "qty_per_garment": str(i.qty_per_garment),
                       "unit_price": str(i.unit_price), "total_cost": str(i.total_cost),
                       "dcm_source": i.dcm_source} for i in bom.items],
        }

    @staticmethod
    def _bom_view(bom: Bom) -> dict:
        return {
            "id": str(bom.id), "status": bom.status, "revision": bom.revision,
            "currency": bom.currency, "order_qty": bom.order_qty,
            "garment_fob_price": float(bom.garment_fob_price) if bom.garment_fob_price is not None else None,
            "bulk_total": float(bom.bulk_total) if bom.bulk_total is not None else None,
            "cutting_confirmed_at": bom.cutting_confirmed_at.isoformat() if bom.cutting_confirmed_at else None,
            "approved_at": bom.approved_at.isoformat() if bom.approved_at else None,
            "rejection_reason": bom.rejection_reason,
            "export_document_id": str(bom.export_document_id) if bom.export_document_id else None,
            "exported_at": bom.exported_at.isoformat() if bom.exported_at else None,
            "items": [{
                "id": str(i.id), "category": i.category, "name": i.name,
                "material_color": i.material_color,
                "qty_per_garment": float(i.qty_per_garment) if i.qty_per_garment is not None else None,
                "uom": i.uom,
                "unit_price": float(i.unit_price) if i.unit_price is not None else None,
                "bulk_qty": float(i.bulk_qty) if i.bulk_qty is not None else None,
                "total_cost": float(i.total_cost) if i.total_cost is not None else None,
                "dcm_source": i.dcm_source,
                "dcm_confidence": float(i.dcm_confidence) if i.dcm_confidence is not None else None,
                "annotation": i.annotation,
            } for i in bom.items],
        }

    async def _audit(self, user, action: str, entity_id, *, before=None, after=None) -> None:
        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None), action=action,
            entity_type="bom", entity_id=entity_id, before=before, after=after,
            at=datetime.now(timezone.utc),
        ))
        await self.repo.commit()
