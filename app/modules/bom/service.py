# service.py
"""
modules/bom/service.py â€” Stage-2/3 BOM generation + approval engine (BomService)

The business brain of Stage 2. Consumes a submission's spec + order sheets and
produces the DRAFT `bom` + `bom_item` tree, then governs the editable contract,
the cutting-manager confirmation gate, and the Stage-3 approval/export gates.

THREE CHANGES IN THIS REVISION (the #3/#4/#5 + god-split work)
  #3/#4  TYPED, NO SHIM. extraction.extract_spec/extract_order now return
         ExtractedSpec / ExtractedOrder. This service consumes them directly â€”
         the old intermediate-dict round-trip (Pydantic -> dict -> Pydantic) is
         gone. Native-term -> pom_code resolution and attribute flattening
         (formerly extraction._legacy_attributes) live HERE now, where the DB is.
  #5     ONE TRANSACTION for generation. generate_for_order + generate_bom form a
         single unit-of-work: rows are staged with self.db.add()/flush() and
         committed EXACTLY ONCE at the end (mirrors the edit_bom_items pattern).
         A mid-generation failure now leaves NOTHING half-written.
            >>> REQUIRES: the BomRepository read/lookup helpers used in the
            >>> generate path (get_garment_type, pom_dictionary_rows,
            >>> find_consumption_template, find_similar_template,
            >>> get_consumption_template, get_bom_by_submission) must NOT commit.
            >>> Reads never should; if any do, the single-commit guarantee breaks.
            >>> The write helpers (repo.save/add/add_*) are intentionally NOT used
            >>> in the generate path anymore â€” session add/flush is used instead.
  god-split  generate_bom is now an orchestrator over private steps:
         _extract_spec, _resolve_and_persist_poms, _spec_attributes,
         _resolve_pattern, _build_items. Same class, same session, same
         transaction â€” deliberately NOT separate service classes (that would
         fragment the unit-of-work above).

The other gates (confirm_cutting, approve_bom, reject/reopen/export, edit) keep
their existing commit boundaries â€” out of scope for the generate unit-of-work.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import yaml

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.modules.bom import checks as checks_mod, pattern
from app.modules.bom import costing
from app.modules.bom import dcm
from app.modules.bom.dcm import (
    CONFIDENCE,
    dxf_yields,
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
from app.modules.bom.extraction import extract_order, extract_spec
from app.modules.bom.extraction_schemas import ExtractedOrder, ExtractedSpec
from app.modules.bom.export import render_bom_pdf
from app.core.models import AuditLog, Document
from app.modules.bom.models import (
    Bom,
    BomItem,
    OrderExtraction,
    PatternReference,
    PomMeasurement,
    SpecExtraction,
    SpecSheet,
)

from app.modules.bom.repository import BomRepository
from app.core.storage import get_storage
from app.modules.bom.pattern import learn_yield

# Which categories get DCM-resolved (leather AREA materials, in dmÂ²). Threads /
# accessories / manufacturing / packaging / FOB carry given qty + price, no DCM (Â§2).
MATERIAL_DCM_CATEGORIES = {
    BomItemCategory.MAIN_MATERIAL.value,
    BomItemCategory.SUB_MATERIAL.value,
    BomItemCategory.LINING.value,
    BomItemCategory.INTERLINING.value,
}

logger = logging.getLogger(__name__)


#  native-term â†’ pom_code resolution (the DB pom_dictionary is the truth)
_WS_RE = re.compile(r"\s+")


def _norm_term(t: str) -> str:
    """Match the dictionary seeding exactly: strip ALL whitespace, lowercase."""
    return _WS_RE.sub("", str(t)).strip().lower()


def _term_language(term: str) -> str | None:
    """Cheap language hint for lookup: any kana or CJK ideograph -> 'ja', else None."""
    for ch in term or "":
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x9FFF:
            return "ja"
    return None


class PomDict:
    """Session-free native-term â†’ pom_code map built from `pom_dictionary` rows.
    Tries the term's detected language first, then any language."""

    def __init__(self, rows: list[tuple[str, str, str]]):
        self._by_lang_term: dict[tuple[str, str], str] = {}
        self._by_term: dict[str, str] = {}
        for lang, term, code in rows:
            key = _norm_term(term)
            self._by_lang_term[(lang, key)] = code
            self._by_term.setdefault(key, code)

    def resolve(self, term: str, language: str | None = None) -> str | None:
        key = _norm_term(term)
        if language and (language, key) in self._by_lang_term:
            return self._by_lang_term[(language, key)]
        return self._by_term.get(key)


# content sniff so extract_* routes correctly even with a missing extension â”€â”€
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PDF_MIME = "application/pdf"


def _sniff_mime(data: bytes | None) -> str | None:
    if not data:
        return None
    if data[:4] == b"%PDF":
        return _PDF_MIME
    if data[:2] == b"PK":          # zip container â†’ xlsx/xlsm
        return _XLSX_MIME
    return None


# Default non-material cost lines seeded onto every generated DRAFT BOM so it is
# never born empty. Keyed by garment_type.code (UPPER), with a `_default` fallback.
def _resolve_cost_catalog_path() -> str:
    """Walk up from this module looking for config/cost_catalog.yaml.

    The previous fixed dirname chain was off by one for some layouts, so the path
    pointed at a non-existent file and BOMs were born WITHOUT their default cost
    lines (a silent failure). Walking up is robust to whether config/ sits at the
    app root or the project root; falls back to the four-up guess if nothing exists."""
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        candidate = os.path.join(here, "config", "cost_catalog.yaml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))),
        "config", "cost_catalog.yaml",
    )


_COST_CATALOG_YAML = _resolve_cost_catalog_path()


@lru_cache(maxsize=1)
def load_cost_catalog(path: str | None = None) -> dict:
    """[lru_cached] read+cache config/cost_catalog.yaml. Returns
    {garment_code|"_default": [line dict, ...]}."""
    with open(path or _COST_CATALOG_YAML, encoding="utf-8") as fh:
        return dict(yaml.safe_load(fh) or {})


@dataclass
class LineSeed:
    """One BOM line BEFORE DCM resolution + costing. Material lines have their
    qty_per_garment RESOLVED by the Â§2 policy; others keep the given value."""
    category: str
    name: str
    material_color: str | None = None
    uom: str | None = None
    unit_price: Decimal | float | None = None
    qty_per_garment: Decimal | float | None = None
    annotation: str | None = None
    source_ref: str | None = None
    supplied_by: str | None = None                    # client/buyer â†’ unit_price 0


@dataclass
class StyleIdentity:
    """The order/style identity for a BOM. Submission-anchored: built from the parsed
    ORDER SHEET (client_order_id/style_id None). Materialised/legacy: resolved off the
    clients module."""
    client_id: uuid.UUID | None
    client_order_id: uuid.UUID | None = None
    style_id: uuid.UUID | None = None
    customer_ref: str | None = None
    internal_ref: str | None = None
    name: str | None = None
    order_qty: int = 0
    per_size_qty: dict = field(default_factory=dict)
    order_number: str | None = None


class BomService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = BomRepository(db)

    async def get_bom_dto(self, bom_id):
        """A session-free BOM snapshot for cross-module consumers (inventory check,
        supplier-PO generation, production board). Returns None if absent."""
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

    async def get_bom_view_for_submission(self, submission_id) -> dict | None:
        """The _bom_view of the BOM anchored on a submission, or None."""
        bom = await self.repo.get_bom_by_submission(submission_id)
        return self._bom_view(bom) if bom is not None else None

    def _replay_response(self, bom: Bom) -> dict:
        """The generate_for_order response shape for an idempotent replay (a BOM
        already exists for this submission). Mirrors generate_bom's return."""
        oi = bom.order_identity or {}
        return {
            "bom": self._bom_view(bom),
            "flags": [],
            "order": {
                "order_qty": bom.order_qty,
                "per_size_qty": oi.get("per_size_qty") or {},
                "warnings": [],
            },
            "extraction": {"idempotent_replay": True},
            "idempotent_replay": True,
        }

    
    async def generate_for_order(
        self, user, *, spec_bytes: bytes, filename: str,
        spec_type: str | None, client_match_code: str | None,
        client_id: uuid.UUID | None, submission_id: uuid.UUID | None = None,
        order_bytes: bytes | None = None, order_filename: str | None = None,
        order_mime: str | None = None, order_match_code: str | None = None,
        source_document_id: uuid.UUID | None = None,
    ) -> dict:
        """Build a DRAFT BOM from the accepted ORDER + SPEC sheets ALONE. Parses the
        order sheet, creates the SpecSheet the engine extracts into, and runs
        generate_bom anchored on the submission â€” all in ONE transaction (committed
        once inside generate_bom). The Clientâ†’Orderâ†’Styleâ†’SKU tree is created later,
        at MD approval (materialize_breakdown)."""
        # A double-fire of the trigger (retry, double-click, at-least-once delivery,
        # Celery re-queue) must NOT mint a second BOM + spec_sheet + duplicate staging
        # rows + duplicate audit. If a BOM already exists for this submission, return it.
        if submission_id is not None:
            existing = await self.repo.get_bom_by_submission(submission_id)
            if existing is not None:
                logger.info("generate_for_order: BOM already exists for submission=%s "
                            "â†’ returning existing bom=%s (idempotent replay)",
                            submission_id, existing.id)
                return self._replay_response(existing)

        # Parse the order sheet off the event loop (openpyxl/pypdf are blocking).
        # Best-effort: anything unparsed is a warning in order.warnings, never a reject.
        order: ExtractedOrder | None = None
        if order_bytes:
            order = await run_in_threadpool(
                extract_order, order_bytes, order_filename or "order",
                order_mime or _sniff_mime(order_bytes),
                order_match_code or client_match_code,
            )
            
        logger.debug("generate_for_order parsed order: %s", order)

        # Stage the order-extraction row (flush-only â€” committed once in generate_bom).
        # We hold the ORM object and stamp its promotion inside generate_bom rather than
        # tucking an id into the payload (the typed model can't carry transient fields).
        order_extraction_row: OrderExtraction | None = None
        if order is not None:
            order_extraction_row = self._build_order_extraction_row(
                order, source_document_id=source_document_id)
            self.db.add(order_extraction_row)

        identity = self._identity_from_order(order, client_id)

        spec_sheet = SpecSheet(
            client_id=client_id, style_id=None,
            source_document_id=source_document_id,
            spec_type=spec_type or "unknown", attributes={},
        )
        self.db.add(spec_sheet)
        # Flush to populate spec_sheet.id (used as a FK for pom_measurement /
        # pattern_reference and in the replace-on-key delete). No commit yet.
        await self.db.flush()

        return await self.generate_bom(
            user, spec_sheet=spec_sheet, spec_bytes=spec_bytes,
            filename=filename or "spec", identity=identity,
            client_match_code=client_match_code, line_seeds=None,
            submission_id=submission_id, order=order,
            order_extraction_row=order_extraction_row,
        )

    @staticmethod
    def _identity_from_order(order: ExtractedOrder | None,
                             client_id: uuid.UUID | None) -> StyleIdentity:
        """Build StyleIdentity from the typed parsed order (no SKUs exist yet).
        client_order_id / style_id stay None until materialise_breakdown at approval."""
        if order is None:
            return StyleIdentity(client_id=client_id)
        return StyleIdentity(
            client_id=client_id, client_order_id=None, style_id=None,
            customer_ref=None, internal_ref=None,          # not extracted from orders
            name=order.style_no, order_qty=int(order.order_qty or 0),
            per_size_qty=dict(order.per_size_qty or {}),
            order_number=order.order_number,
        )

    @staticmethod
    def _order_identity_snapshot(identity: StyleIdentity,
                                 order: ExtractedOrder | None) -> dict:
        """The persisted order snapshot (a JSON column) read back by _resolve_identity +
        breakdown materialisation while style_id is NULL. Merges the typed order (colour
        lines, season, price) with the resolved identity fields; `warnings` dropped."""
        oi = order.model_dump(mode="json") if order is not None else {}
        oi.pop("warnings", None)
        oi["client_id"] = str(identity.client_id) if identity.client_id else None
        oi["order_qty"] = identity.order_qty
        oi["per_size_qty"] = identity.per_size_qty
        oi["order_number"] = identity.order_number
        oi["style_name"] = identity.name
        oi["customer_ref"] = identity.customer_ref
        oi["internal_ref"] = identity.internal_ref
        # materialize_breakdown reads oi["unit_price"] â€” alias the typed field so it
        # survives the dict round-trip (it's `price_per_garment` on the model).
        oi["unit_price"] = order.price_per_garment if order is not None else None
        return oi

    @staticmethod
    def _build_line_seeds(spec_attributes: dict | None,
                          garment_code: str | None) -> list[LineSeed]:
        """Seed builder (Â§5b): material/accessory lines from the spec's flattened
        attributes + the configured cost lines from cost_catalog.yaml. Unit prices on
        material lines are left blank for the cutting manager to fill."""
        attrs = spec_attributes or {}
        seeds: list[LineSeed] = []

        leather = attrs.get("leather_quality")
        if leather:
            seeds.append(LineSeed(
                category=BomItemCategory.MAIN_MATERIAL.value, name=str(leather),
                material_color=attrs.get("primary_color"), uom="dmÂ²",
                source_ref="spec.attributes.leather_quality"))

        # Secondary leathers/fabrics (e.g. a contrast panel 'åˆ¥å¸ƒ') â†’ own DCM line.
        for sub in attrs.get("sub_materials") or []:
            if not isinstance(sub, dict):
                continue
            name = sub.get("material") or sub.get("name")
            if not name:
                continue
            seeds.append(LineSeed(
                category=BomItemCategory.SUB_MATERIAL.value, name=str(name),
                uom="dmÂ²", annotation=sub.get("name"),
                source_ref="spec.attributes.sub_materials"))

        lining = attrs.get("lining")
        if lining and "unlined" not in str(lining).lower():
            seeds.append(LineSeed(
                category=BomItemCategory.LINING.value, name=str(lining), uom="dmÂ²",
                source_ref="spec.attributes.lining"))

        # Interlining (fusible/non-fusible) when the spec names one. DCM-resolved.
        interlining = attrs.get("interlining")
        if isinstance(interlining, dict) and interlining.get("present") is not False:
            il_name = interlining.get("material")
            if il_name:
                seeds.append(LineSeed(
                    category=BomItemCategory.INTERLINING.value, name=str(il_name),
                    uom="dmÂ²", annotation=interlining.get("placement"),
                    source_ref="spec.attributes.interlining"))

        for acc in attrs.get("accessories") or []:
            if not isinstance(acc, dict):
                continue
            name = acc.get("spec") or acc.get("type") or "accessory"
            # qty per garment from the spec (two rear zippers â†’ 2); default 1 only when
            # the document/extractor gave no usable count.
            try:
                qty = int(acc.get("qty_per_garment") or acc.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            if qty < 1:
                qty = 1
            seeds.append(LineSeed(
                category=BomItemCategory.ACCESSORY.value, name=str(name),
                material_color=acc.get("finish"), uom="pc", qty_per_garment=qty,
                supplied_by=acc.get("supplied_by"),
                source_ref="spec.attributes.accessories"))

        from app.modules.bom import config_store
        catalog = config_store.get_cost_catalog()
        
        cost_lines = catalog.get((garment_code or "").upper()) or catalog.get("_default") or []
        for ln in cost_lines:
            category, name = ln.get("category"), ln.get("name")
            if not category or not name:
                logger.warning("skipping malformed cost_catalog line (need category+name): %r", ln)
                continue
            seeds.append(LineSeed(
                category=category, name=name, uom=ln.get("uom"),
                unit_price=ln.get("unit_price"),
                qty_per_garment=ln.get("qty_per_garment", 1)))
        return seeds

   
    async def generate_bom(
        self, user, *, spec_sheet, spec_bytes: bytes, filename: str,
        identity: StyleIdentity, client_match_code: str | None,
        line_seeds: list[LineSeed] | None = None, currency: str | None = None,
        checks_cfg: tuple | None = None,
        submission_id: uuid.UUID | None = None,
        order: ExtractedOrder | None = None,
        order_extraction_row: OrderExtraction | None = None,
    ) -> dict:
        logger.info("generate_bom start: order=%s style=%s spec_type=%s client_match=%s seeds=%s",
                    identity.client_order_id, identity.style_id, spec_sheet.spec_type,
                    client_match_code, len(line_seeds) if line_seeds is not None else "auto")

        spec = await self._extract_spec(spec_bytes, filename)
        logger.debug("generate_bom extracted spec: %s", spec)

        spec_extraction_row = self._build_spec_extraction_row(
            spec, source_document_id=spec_sheet.source_document_id)
        self.db.add(spec_extraction_row)

        ext_warnings = list(spec.warnings)
        manual_entry_required = "manual_entry_required" in ext_warnings

        # 2. resolve native terms → pom_code, persist (replace-on-key)
        resolved_poms, unresolved, pom_rows = await self._resolve_and_persist_poms(spec, spec_sheet)

        # 3. flatten + merge extracted attributes into the spec sheet
        attributes = self._spec_attributes(spec)
        if attributes:
            spec_sheet.attributes = {**(spec_sheet.attributes or {}), **attributes}

        # 3b. seed BOM lines AFTER extraction (the ordering fix) 
        if line_seeds is None:
            line_seeds = self._build_line_seeds(
                spec_sheet.attributes, garment_code=spec.garment_type_guess)

        gt = await self.repo.get_garment_type(spec.garment_type_guess)
        gt_id = gt.id if gt else None

        # 4. pattern reference (resolve to template memory if possible) 
        pattern_template_id, pr_block = await self._resolve_pattern(
            spec, identity, spec_sheet, gt_id)

        sig = style_signature(customer_ref=identity.customer_ref,
                              internal_ref=identity.internal_ref, name=identity.name)
        sizes = list(spec.sizes)
        base_size = (spec.pattern_reference.base_size if spec.pattern_reference else None) \
            or (sizes[0] if sizes else None) 
        poms_for_size = self._poms_by_size(resolved_poms)

        bom = Bom(
            submission_id=submission_id, client_id=identity.client_id,
            client_order_id=identity.client_order_id, style_id=identity.style_id,
            status=BomStatus.DRAFT.value, currency=currency,
            order_qty=identity.order_qty, revision=1,
            garment_type_id=gt_id, dcm_base_size=base_size,
            order_identity=self._order_identity_snapshot(identity, order),
        )
        
        pattern = await self.repo.get_current_pattern(sig, client_id=identity.client_id)
        dxf_yields = dcm.effective_dxf_yields(
            dcm.dxf_yields(), await self.repo.yields_by_species()) if pattern else {}
        
        items = await self._build_items(
            line_seeds, identity=identity, sig=sig, gt=gt, gt_id=gt_id,
            base_size=base_size, poms_for_size=poms_for_size,
            pattern_template_id=pattern_template_id)
        self._recompute(bom, items, base_size)
        bom.items = items
        bom.source_document_id = spec_sheet.source_document_id
        self.db.add(bom)
        # Flush to populate bom.id + item ids without committing (the items collection
        # stays loaded in-memory, so we can build the view below pre-commit).
        await self.db.flush()

        ctx = {
            "attributes": spec_sheet.attributes or {},
            "poms": resolved_poms, "sizes": sizes,
            "per_size_qty": identity.per_size_qty, "order_qty": identity.order_qty,
            "pattern_reference": pr_block,
        }
        flags = checks_mod.run_checks(client_match_code, ctx, checks_cfg)

        if manual_entry_required:
            flags = [{
                "code": "extraction_failed", "severity": "error",
                "message": ("Spec sheet could not be read automatically. "
                            "Please enter material lines manually before submitting for review."),
                "details": ext_warnings,
            }] + flags

        order_warnings = list(order.warnings) if order is not None else []
        if "manual_entry_required" in order_warnings:
            flags = [{
                "code": "order_extraction_failed", "severity": "error",
                "message": ("Order sheet could not be read automatically. "
                            "Order quantities must be entered manually."),
                "details": order_warnings,
            }] + flags

        # 8. promotion stamps + audit (all in-memory; committed once below) 
        now = datetime.now(timezone.utc)
        spec_extraction_row.promoted_to_spec_sheet_id = spec_sheet.id
        spec_extraction_row.promoted_at = now
        if order_extraction_row is not None:
            order_extraction_row.promoted_to_bom_id = bom.id
            order_extraction_row.promoted_at = now

        # Build the views BEFORE the commit — expire_on_commit would otherwise turn the
        # post-commit attribute reads into lazy loads (MissingGreenlet under async).
        bom_view = self._bom_view(bom)
        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None), action="BOM_GENERATE",
            entity_type="bom", entity_id=bom.id, before=None,
            after=self._bom_snapshot(bom), at=now,
        ))

        await self.repo.commit()

        logger.info("generate_bom done: bom=%s items=%d fob=%s flags=%d unresolved=%d manual=%s",
                    bom_view["id"], len(bom_view["items"]), bom_view["garment_fob_price"],
                    len(flags), len(unresolved), manual_entry_required)

        return {
            "bom": bom_view,
            "flags": flags,
            "order": {
                "order_qty": identity.order_qty,
                "per_size_qty": identity.per_size_qty,
                "warnings": order_warnings,
            },
            "extraction": {
                "poms": len(pom_rows),
                "unresolved": unresolved,
                "pattern_reference": pr_block,
                "warnings": ext_warnings,
                "manual_entry_required": manual_entry_required,
            },
        }

    async def _extract_spec(self, spec_bytes: bytes, filename: str) -> ExtractedSpec:
        """Step 1: run the (blocking) LLM extraction off the event loop. Returns the
        typed contract directly — never raises (failures carry manual_entry_required)."""
        return await run_in_threadpool(
            extract_spec, spec_bytes, filename, _sniff_mime(spec_bytes))

    async def _resolve_and_persist_poms(self, spec: ExtractedSpec, spec_sheet):
        """Step 2: native term â†’ pom_code via the DB dictionary, then stage the
        pom_measurement rows (replace-on-key). Returns (resolved_poms, unresolved,
        pom_rows). Inlines the replace so it participates in the single transaction
        instead of depending on a repo method's commit boundary."""
        pom_dict = PomDict(await self.repo.pom_dictionary_rows())
        resolved_poms: list[dict] = []
        unresolved: list[dict] = []
        for p in spec.measurements:
            term = p.source_term or ""
            code = pom_dict.resolve(term, _term_language(term))
            if not code:
                unresolved.append({"source_term": term, "by_size": p.by_size})
                continue
            resolved_poms.append({
                "source_term": p.source_term, "by_size": p.by_size, "pitch": p.pitch,
                "extracted_by": p.extracted_by, "confidence": p.confidence,
                "pom_code": code,
            })

        pom_rows: list[PomMeasurement] = []
        seen: set[tuple[str, str]] = set()
        for p in resolved_poms:
            for size, value in p["by_size"].items():
                key = (str(size), p["pom_code"])
                if key in seen:           # guard the (spec_sheet_id, size, pom_code) unique constraint
                    logger.warning("duplicate (size=%s pom=%s) â€” keeping first", size, p["pom_code"])
                    continue
                seen.add(key)
                conf = p.get("confidence")
                pom_rows.append(PomMeasurement(
                    spec_sheet_id=spec_sheet.id, size=size, pom_code=p["pom_code"],
                    value=Decimal(str(value)),
                    # Store pitch if available.
                    pitch=Decimal(str(p["pitch"])) if p.get("pitch") is not None else None,
                    
                    # Store the original text found in Excel.
                    source_term=p.get("source_term"),
                    extracted_by=p.get("extracted_by") or spec.extracted_by
                    or ExtractionSource.MANUAL.value,
                    confidence=Decimal(str(conf)) if conf is not None else Decimal("0.00"),
                ))
 
        # replace-on-key, inline (single transaction): drop any existing rows for this
        # spec sheet, then stage the new ones. With the idempotency guard upstream the
        # spec sheet is always fresh, so the delete is a harmless no-op here.
        await self.repo.replace_pom_measurements_atomic(spec_sheet, pom_rows)
        return resolved_poms, unresolved, pom_rows

    @staticmethod
    def _spec_attributes(spec: ExtractedSpec) -> dict:
        """Step 3 helper: flatten the typed spec into the unstructured attributes dict
        the line-seed builder + checks read (formerly extraction._legacy_attributes â€”
        moved here, where the resolution lives)."""
        attrs: dict = dict(spec.materials or {})

        if spec.color_details:
            primary = spec.color_details.get("primary")
            if primary:
                attrs["primary_color"] = primary
            secondary = spec.color_details.get("secondary")
            if secondary:
                attrs["secondary_color"] = secondary
            if spec.color_details.get("details"):
                attrs["color_details"] = spec.color_details["details"]

        lining = spec.lining or {}
        if lining.get("lined") is False:
            attrs["lining"] = "unlined"
        elif lining.get("lined") is True:
            attrs["lining"] = "lined"
        if lining.get("details"):
            attrs["lining"] = lining["details"]
        if lining.get("material") is True:
            attrs["material"] = "material"    
        

        if spec.interlining:
            attrs["interlining"] = dict(spec.interlining)
        if spec.sub_materials:
            attrs["sub_materials"] = [s.model_dump() for s in spec.sub_materials]
        if spec.accessories:
            attrs["accessories"] = [a.model_dump() for a in spec.accessories]

        return attrs

    async def _resolve_pattern(self, spec: ExtractedSpec, identity: StyleIdentity,
                               spec_sheet, gt_id):
        """Step 4: stage a PatternReference for a "follow pattern X" spec and try to
        resolve it to a confirmed consumption template (Â§2 Source 1b). Returns
        (pattern_template_id, pr_block). Staged with self.db.add â€” committed once."""
        if spec.pattern_reference is None:
            return None, None
        pr = spec.pattern_reference
        ref = PatternReference(
            client_id=identity.client_id, spec_sheet_id=spec_sheet.id,
            pattern_code=pr.pattern_code, base_size=pr.base_size,
            notes=pr.source_term,
        )
        self.db.add(ref)

        pattern_template_id = None
        tmpl = await self.repo.find_consumption_template(
            client_id=identity.client_id,
            style_signature=pr.pattern_code.strip().upper(),
            garment_type_id=gt_id, material_category=BomItemCategory.MAIN_MATERIAL.value,
            size=pr.base_size or "",
        )
        if tmpl:
            ref.resolved_template_id = tmpl.id           # in-memory; flushed at commit
            pattern_template_id = tmpl.id
        pr_block = {"pattern_code": pr.pattern_code,
                    "resolved": ref.resolved_template_id is not None
                    or ref.resolved_style_id is not None}
        return pattern_template_id, pr_block

    async def _build_items(self, line_seeds, *, identity, sig, gt, gt_id, base_size,
                           poms_for_size, pattern_template_id, pattern=None, dxf_yields=None):
        """Step 6: turn seeds into BomItems, DCM-resolving leather AREA lines (Â§2) and
        carrying given qty/price on the rest."""
        items: list[BomItem] = []
        for seed in line_seeds: 
            price = Decimal("0") if (seed.supplied_by or "").lower() in ("client", "buyer") \
                else (Decimal(str(seed.unit_price)) if seed.unit_price is not None else Decimal("0"))
            dcm_source = None
            dcm_conf = None
            if seed.category in MATERIAL_DCM_CATEGORIES:
                dcm_val, src = await self._resolve_dcm(
                    client_id=identity.client_id, style_signature=sig, garment_type_id=gt_id,
                    garment_type_row=gt, material_category=seed.category, size=base_size,
                    poms_for_size=poms_for_size, pattern_template_id=pattern_template_id,
                    pattern=pattern, line_species=dcm.species_of(seed.name),
                    dxf_yields=dxf_yields)
                if dcm_val is not None:
                    qpg = dcm_val                              # F1: dcm_val, NOT the `dcm` module
                    dcm_source = src.value
                    dcm_conf = CONFIDENCE[src]
                else:
                    # #3: no DCM source resolved (first order: no DXF/template/similar, and
                    # the POM heuristic had nothing to work with). Emit a PROVISIONAL leather
                    # line — qty 0, confidence 0, flagged — never a silent 1-sf line, never a
                    # block. Binding DCM arrives via the cutting-manager confirm or DXF regen.
                    qpg = Decimal("0")
                    dcm_source = DcmSource.PROVISIONAL.value
                    dcm_conf = Decimal("0")
            else:
                qpg = Decimal(str(seed.qty_per_garment)) if seed.qty_per_garment is not None \
                    else Decimal("1")
                    
            items.append(BomItem(
                category=seed.category, name=seed.name, material_color=seed.material_color,
                qty_per_garment=qpg, uom=seed.uom, unit_price=price,
                annotation=seed.annotation, source_ref=seed.source_ref,
                dcm_source=dcm_source, dcm_confidence=dcm_conf,
            ))
            
        return items

    # the ordered DCM fallback (Â§2 Sources 1â†’3; Source 4 is the edit/confirm) â”€
    async def _resolve_dcm(self, *, client_id, style_signature, garment_type_id,
                           garment_type_row, material_category, size, poms_for_size,
                           pattern_template_id, pattern=None, line_species="_default",
                           dxf_yields=None):
        size_key = size if size is not None else ""
        # Source 1 â€” the DCM memory (exact, confirmed)
        tmpl = await self.repo.find_consumption_template(
            client_id=client_id, style_signature=style_signature,
            garment_type_id=garment_type_id, material_category=material_category, size=size_key,
        )
        if tmpl:
            return Decimal(str(tmpl.dcm_value)), DcmSource.TEMPLATE
        # Source 1b â€” via a resolved pattern reference's template
        if pattern_template_id is not None:
            tmpl = await self.repo.get_consumption_template(pattern_template_id)
            if tmpl and tmpl.material_category == material_category:
                return Decimal(str(tmpl.dcm_value)), DcmSource.TEMPLATE
        # Source 1c â€” DXF net pattern area Ã— per-species yield (measured geometry).
        if pattern is not None and material_category in MATERIAL_DCM_CATEGORIES:
            from app.modules.bom.pattern import dcm_for_category
            dxf_dcm = dcm_for_category(pattern, category=material_category, size=size,
                                       species=line_species, yields=dxf_yields or {})
            if dxf_dcm is not None:
                return Decimal(str(dxf_dcm)), DcmSource.DXF
        # Source 2 â€” similar style (rule-based nearest neighbour)
        sim = await self.repo.find_similar_template(
            client_id=client_id, garment_type_id=garment_type_id,
            material_category=material_category, size=size_key, exclude_signature=style_signature,
        )
        if sim:
            return Decimal(str(sim.dcm_value)), DcmSource.SIMILAR_STYLE
        # Source 3 â€” AI heuristic from POM area (last resort, FLAGGED)
        est = estimate_area_dcm(
            getattr(garment_type_row, "area_formula", None) if garment_type_row else None,
            getattr(garment_type_row, "default_wastage_pct", None) if garment_type_row else None,
            poms_for_size.get(size, {}) if size else {},
        )
        if est is not None and est > 0:
            return est, DcmSource.AI_ESTIMATE
        return None, None

    @staticmethod
    def _poms_by_size(poms: list[dict]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for p in poms:
            for size, val in (p.get("by_size") or {}).items():
                out.setdefault(size, {})[p["pom_code"]] = val
        return out
    
    async def ingest_pattern_dxf(self, user, *, data: bytes, style_signature=None, client_id=None):
        """Parse + persist a pattern DXF in ONE transaction. The repository owns the
        parse/attribute/supersede/write (persist_dxf); this method just stores the bytes
        and commits. Idempotent on (style_signature, sha256)."""
        import hashlib
        from starlette.concurrency import run_in_threadpool
        sha = hashlib.sha256(data).hexdigest()
        # store the raw bytes first so the pattern can be re-derived; pass the KEY to the repo
        key = f"patterns/{(style_signature or 'unknown').upper()}/{sha}.dxf"
        await run_in_threadpool(get_storage().put, key, data)
        row, unknown = await self.repo.persist_dxf(
            data, style_signature=style_signature, client_id=client_id, storage_key=key)
        await self.repo.commit()
        return {"pattern_id": str(row.id), "style_signature": row.style_signature,
                "n_pieces": row.n_pieces, "unknown_fabrics": unknown, "warnings": row.warnings}

    
    _EDIT_FIELDS = {"dcm", "qty_per_garment", "unit_price"}
    _EDITABLE_STATES = {BomStatus.DRAFT.value, BomStatus.READY_FOR_REVIEW.value}

    async def edit_bom_items(self, user, bom_id: uuid.UUID, base_revision: int,
                             edits: list[dict]) -> dict:
        bom = await self._load_bom(bom_id)
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
            if value < 0:
                raise HTTPException(422, detail={"error": "negative_value",
                                                 "bom_item_id": e.get("bom_item_id"),
                                                 "field": field_, "value": e.get("value")})
            if field_ in ("dcm", "qty_per_garment"):
                item.qty_per_garment = value
                if item.category in MATERIAL_DCM_CATEGORIES:
                    item.dcm_source = DcmSource.MANUAL.value
                    item.dcm_confidence = CONFIDENCE[DcmSource.MANUAL]
                    dcm_changed = True
            elif field_ == "unit_price":
                item.unit_price = value

        was_confirmed = bom.cutting_confirmed_at is not None
        reconfirm_required = dcm_changed and was_confirmed
        if reconfirm_required:
            bom.cutting_confirmed_at = None
            bom.cutting_confirmed_by = None
            bom.status = BomStatus.DRAFT.value

        self._recompute(bom, bom.items, self._base_size(bom))

        if await self.repo.claim_revision(bom_id, base_revision) == 0:
            await self.repo.rollback()
            fresh = await self.repo.get_bom(bom_id)
            raise HTTPException(409, detail={
                "error": "stale_revision",
                "current_revision": fresh.revision if fresh else None})
        await self.repo.commit()
        bom = await self.repo.get_bom(bom.id)
        await self._audit(user, "BOM_EDIT", bom.id, before=before, after=self._bom_snapshot(bom))
        return {"revision": bom.revision, "recomputed": self._bom_view(bom),
                "reconfirm_required": reconfirm_required}


    async def confirm_cutting(self, user, bom_id: uuid.UUID,
                              identity: StyleIdentity | None = None) -> dict:
        bom = await self._load_bom(bom_id)
        if bom.status not in (BomStatus.DRAFT.value, BomStatus.READY_FOR_REVIEW.value):
            raise HTTPException(409, detail={
                "error": "invalid_state_for_confirmation",
                "current_status": bom.status,
                "message": "Cutting confirmation is only allowed on a draft BOM."})
        now = datetime.now(timezone.utc)
        bom.cutting_confirmed_by = getattr(user, "id", None)
        bom.cutting_confirmed_at = now
        bom.status = BomStatus.READY_FOR_REVIEW.value
        await self.repo.save(bom)

        if identity is None:
            identity = await self._resolve_identity(bom)
        backfilled = 0
        if identity is not None:
            sig = style_signature(customer_ref=identity.customer_ref,
                                  internal_ref=identity.internal_ref, name=identity.name)
            pattern = await self.repo.get_current_pattern(sig, client_id=identity.client_id)
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
                    if pattern is not None and item.category in (
                            BomItemCategory.MAIN_MATERIAL.value, BomItemCategory.SUB_MATERIAL.value):
                        obs = learn_yield(pattern, category=item.category, size=bom.dcm_base_size,
                                        species=dcm.species_of(item.name),
                                        confirmed_dcm_sf=item.qty_per_garment)
                        if obs:
                            await self.repo.add_yield_observation(
                                obs, source_bom_id=bom.id, confirmed_by=getattr(user,"id",None), confirmed_at=now)
        await self._audit(user, "BOM_SUBMIT_FOR_REVIEW", bom.id,
                          after={"status": bom.status,
                                 "cutting_confirmed_at": now.isoformat(),
                                 "templates_backfilled": backfilled})

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
        """Stage-3 MD approve/lock. REFUSES a BOM whose cutting_confirmed_at is null."""
        bom = await self._load_bom(bom_id)
        if bom.cutting_confirmed_at is None:
            raise HTTPException(409, detail={
                "error": "cutting_confirmation_required",
                "message": "The cutting manager must confirm the BOM before approval."})
        before = self._bom_snapshot(bom)
        now = datetime.now(timezone.utc)
        await self._materialize_breakdown(user, bom)
        bom.status = BomStatus.LOCKED.value if lock else BomStatus.APPROVED.value
        bom.approved_by = getattr(user, "id", None)
        bom.approved_at = now
        bom.locked_at = now
        await self.repo.save(bom)
        await self._audit(user, "BOM_APPROVE", bom.id, before=before,
                          after={"status": bom.status, "approved_at": now.isoformat(),
                                 "locked_at": now.isoformat(), "revision": bom.revision})
        bom_id_str, bom_status = str(bom.id), bom.status
        try:
            from app.modules.supplier_po.production_tracking_service import (
                ProductionTrackingService,
            )
            await ProductionTrackingService(self.db).on_bom_approved(bom.id)
        except Exception:
            logger.exception("production board advance failed for bom=%s", bom_id_str)
            await self.repo.rollback()
        inventory_check_id = None
        try:
            from app.modules.inventory.service import InventoryService

            res = await InventoryService(self.db).run_check(user, uuid.UUID(bom_id_str))
            inventory_check_id = res.get("inventory_check_id")
        except Exception:
            logger.exception("inventory check failed for bom=%s", bom_id_str)
            await self.repo.rollback()
        return {"bom_id": bom_id_str, "status": bom_status,
                "inventory_check_id": inventory_check_id}

    async def _materialize_breakdown(self, user, bom: Bom) -> None:
        """Create the Clientâ†’Orderâ†’Styleâ†’SKU tree from the BOM's parsed order snapshot
        and back-link it. Idempotent: a BOM that already has a style is left alone."""
        if bom.style_id is not None:
            return
        oi = bom.order_identity or {}
        cid = oi.get("client_id")
        client_id = bom.client_id or (uuid.UUID(cid) if isinstance(cid, str) else cid)
        bom_id = bom.id
        submission_id = bom.submission_id
        source_document_id = bom.source_document_id
        if client_id is None:
            logger.warning("breakdown skipped: bom=%s has no client_id (order sheet did not "
                           "resolve a client) â€” order/style not created", bom_id)
            return
        from app.modules.clients.service import ClientService

        order_id, style_id = await ClientService(self.db).create_order_with_breakdown(
            client_id=client_id,
            order={"order_number": oi.get("order_number") or f"BOM-{str(bom_id)[:8]}",
                   "currency": oi.get("currency"),
                   "source_document_id": source_document_id},
            style={"name": oi.get("style_name") or "UNSPECIFIED",
                   "customer_ref": oi.get("customer_ref"),
                   "internal_ref": oi.get("internal_ref"),
                   "season": oi.get("season"), "unit_price": oi.get("unit_price"),
                   "currency": oi.get("currency")},
            lines=oi.get("lines") or [],
            per_size=oi.get("per_size_qty") or {},
        )
        bom.client_order_id = order_id
        bom.style_id = style_id
        await self.repo.save(bom)
        if submission_id is not None:
            try:
                from app.modules.procurement.service import ProcurementService

                await ProcurementService(self.db).link_submission_to_order(
                    submission_id, order_id)
            except Exception:
                logger.exception("failed to link submission %s â†’ order %s",
                                 submission_id, order_id)
        await self._audit(user, "BOM_BREAKDOWN", bom_id,
                          after={"client_order_id": str(order_id), "style_id": str(style_id)})

    async def reject_bom(self, user, bom_id: uuid.UUID, *, reason: str) -> dict:
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
        bom = await self._load_bom(bom_id)
        if bom.status not in (BomStatus.APPROVED.value, BomStatus.LOCKED.value,
                              BomStatus.EXPORTED.value):
            raise HTTPException(409, detail={
                "error": "not_approved",
                "message": "Export is only allowed from an approved/locked BOM."})

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

        pdf_bytes, mime, ext = await run_in_threadpool(render_bom_pdf, view, meta)
        sha = hashlib.sha256(pdf_bytes).hexdigest()

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

    
    async def _load_bom(self, bom_id: uuid.UUID) -> Bom:
        bom = await self.repo.get_bom(bom_id)
        if bom is None:
            raise HTTPException(404, "BOM not found.")
        return bom

    async def _resolve_identity(self, bom: Bom) -> "StyleIdentity | None":
        if bom.style_id is None:
            oi = bom.order_identity or {}
            cid = oi.get("client_id")
            client_id = bom.client_id or (uuid.UUID(cid) if isinstance(cid, str) else cid)
            return StyleIdentity(
                client_id=client_id, client_order_id=None, style_id=None,
                customer_ref=oi.get("customer_ref"), internal_ref=oi.get("internal_ref"),
                name=oi.get("style_name"),
                order_qty=bom.order_qty or int(oi.get("order_qty") or 0),
                per_size_qty=oi.get("per_size_qty") or {},
                order_number=oi.get("order_number"),
            )
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

    @staticmethod
    def _build_spec_extraction_row(spec: ExtractedSpec,
                                   source_document_id: uuid.UUID | None) -> SpecExtraction:
        """Turn an ExtractedSpec into a SpecExtraction staging row. The full model
        becomes raw_payload (audit truth); a handful of fields are denormalized as
        columns for dashboard queries. Staged BEFORE promotion; the caller stamps
        promoted_at + promoted_to_spec_sheet_id once the spec sheet is written."""
        warnings = list(spec.warnings)
        conf = spec.confidence_overall
        return SpecExtraction(
            source_document_id=source_document_id,
            extracted_by=spec.extracted_by or "manual",
            confidence_overall=Decimal(str(conf)) if conf is not None else None,
            raw_payload=spec.model_dump(mode="json"),
            style_no=spec.style_no,
            client_name=spec.client_name,
            garment_type_guess=spec.garment_type_guess,
            num_measurements=len(spec.measurements),
            num_accessories=len(spec.accessories),
            num_warnings=len(warnings),
            manual_entry_required="manual_entry_required" in warnings,
        )

    @staticmethod
    def _build_order_extraction_row(order: ExtractedOrder,
                                    source_document_id: uuid.UUID | None) -> OrderExtraction:
        """Turn an ExtractedOrder into an OrderExtraction staging row."""
        warnings = list(order.warnings)
        conf = order.confidence_overall
        return OrderExtraction(
            source_document_id=source_document_id,
            extracted_by=order.extracted_by or "manual",
            confidence_overall=Decimal(str(conf)) if conf is not None else None,
            raw_payload=order.model_dump(mode="json"),
            order_number=order.order_number,
            style_no=order.style_no,
            client_name=order.client_name,
            season=order.season,
            currency=order.currency,
            order_qty=int(order.order_qty or 0),
            num_lines=len(order.lines),
            num_warnings=len(warnings),
            manual_entry_required="manual_entry_required" in warnings,
        )

    async def _audit(self, user, action: str, entity_id, *, before=None, after=None) -> None:
        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None), action=action,
            entity_type="bom", entity_id=entity_id, before=before, after=after,
            at=datetime.now(timezone.utc),
        ))
        await self.repo.commit()
        
        
    async def set_dxf_yield(self, species: str, factor, note: str | None = None) -> dict:
        from app.modules.bom import config_store
        await self.repo.upsert_dxf_yield(species=species, factor=factor, note=note)
        snap = config_store._current()
        snap.dxf_yields = {**snap.dxf_yields, species: float(factor)}
        config_store.set_snapshot(snap)
        return {"species": species, "factor": float(factor)}

    async def upsert_fabric_role(self, data: dict) -> dict:
        from app.modules.bom import config_store
        from app.modules.bom.config_store import _FabricRoleView
        await self.repo.upsert_fabric_role(**data)
        snap = config_store._current()
        snap.fabric_lexicon = {**snap.fabric_lexicon,
            data["label"]: _FabricRoleView(data["role"], data["category"], bool(data["is_leather"]))}
        config_store.set_snapshot(snap)
        return data
    
    async def set_cost_catalog(self, garment_code: str, lines: list[dict]) -> dict:
            from app.modules.bom import config_store
            code = garment_code.upper()
            await self.repo.replace_cost_catalog(code, lines)
            rows = [(code, l["category"], l["name"], l.get("uom"),
                    l.get("unit_price"), l.get("qty_per_garment", 1), i)
                    for i, l in enumerate(lines)]
            grouped = config_store._group_cost_lines(rows)
            snap = config_store._current()
            snap.cost_catalog = {**snap.cost_catalog, **grouped}
            config_store.set_snapshot(snap)
            return {"garment_code": code, "lines": len(lines)}
        
    async def set_client_checks(self, client_code: str, rules: list[dict]) -> dict:
            from app.modules.bom import config_store
            await self.repo.replace_client_checks(client_code, rules)
            rows = [(client_code, r["id"], r["kind"], r.get("severity", "warn"), r.get("field"),
                    (r["range"][0] if r.get("range") else None),
                    (r["range"][1] if r.get("range") else None), r.get("params"), i)
                    for i, r in enumerate(rules)]
            new_entry = config_store._reconstruct_checks(rows)
            snap = config_store._current()
            snap.bom_checks = [e for e in snap.bom_checks if e["client_code"] != client_code] + new_entry
            config_store.set_snapshot(snap)
            return {"client_code": client_code, "rules": len(rules)}
        
    async def add_pom_mapping(self, data: dict) -> dict:
            language = data.get("language") or _term_language(data["source_term"])
            gt_id = None
            if data.get("garment_type_code"):
                gt = await self.repo.get_garment_type(data["garment_type_code"])
                gt_id = gt.id if gt else None
            await self.repo.upsert_pom_mapping(
                language=language, source_term=data["source_term"],
                pom_code=data["pom_code"], garment_type_id=gt_id, weight=data.get("weight", 1))
            return {"source_term": data["source_term"], "pom_code": data["pom_code"],
                    "language": language, "garment_type_id": str(gt_id) if gt_id else None}