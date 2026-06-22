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
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import yaml

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
import re
from app.modules.bom.extraction import extract_order, extract_spec
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

# Which categories get DCM-resolved (leather AREA materials, in dm²). Threads /
# accessories / manufacturing / packaging / FOB carry given qty + price, no DCM (§2).
MATERIAL_DCM_CATEGORIES = {
    BomItemCategory.MAIN_MATERIAL.value,
    BomItemCategory.SUB_MATERIAL.value,
    BomItemCategory.LINING.value,
    BomItemCategory.INTERLINING.value,
}

logger = logging.getLogger(__name__)


# ── native-term → pom_code resolution (moved here from extraction) ──────────────
# The new extractor emits NATIVE source terms only (e.g. 'ウェスト上り'). The DB
# `pom_dictionary` table is now the single source of truth for term → code. Terms
# the dictionary doesn't know are surfaced as `unresolved` — they feed the future
# admin POST /pom-dictionary endpoint so non-technical staff can add new mappings
# without a developer edit + redeploy.
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
    """Session-free native-term → pom_code map built from `pom_dictionary` rows
    (language, source_term, pom_code). Tries the term's detected language first,
    then any language — a JP-only sheet whose term also exists under 'en' still
    resolves. Resolution moved out of extraction since dictionary access requires
    a DB session that the pure-functional extractor doesn't carry."""

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


# ── content sniff so extract_* routes correctly even with a missing extension ──
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PDF_MIME = "application/pdf"


def _sniff_mime(data: bytes | None) -> str | None:
    """Identify uploaded file type from magic bytes when the upload arrives without
    a usable extension or mime. Returns the canonical mime string or None."""
    if not data:
        return None
    if data[:4] == b"%PDF":
        return _PDF_MIME
    if data[:2] == b"PK":          # zip container → xlsx/xlsm
        return _XLSX_MIME
    return None


# Default non-material cost lines (manufacturing/packaging/FOB) seeded onto every
# generated DRAFT BOM so it is never born empty — the "configured" rows of the §5b
# provenance table. Keyed by garment_type.code (UPPER), with a `_default` fallback.
_COST_CATALOG_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "config", "cost_catalog.yaml",
)


@lru_cache(maxsize=1)
def load_cost_catalog(path: str | None = None) -> dict:
    """[lru_cached] read+cache config/cost_catalog.yaml — same posture as
    extraction.load_adapters. Returns {garment_code|"_default": [line dict, ...]}."""
    with open(path or _COST_CATALOG_YAML, encoding="utf-8") as fh:
        return dict(yaml.safe_load(fh) or {})


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
    """The order/style identity for a BOM. For a submission-anchored BOM it is built
    from the PARSED ORDER SHEET (client_order_id/style_id are None — no breakdown exists
    yet); for a persisted/legacy BOM it is resolved off the clients module."""
    client_id: uuid.UUID | None
    client_order_id: uuid.UUID | None = None
    style_id: uuid.UUID | None = None
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

    async def get_bom_view_for_submission(self, submission_id) -> dict | None:
        """The _bom_view of the BOM anchored on a submission, or None. Lets procurement's
        Stage-1→2 trigger replay an already-generated BOM idempotently (it owns the
        submission lifecycle but must not touch the bom repository — CLAUDE.md §3.2)."""
        bom = await self.repo.get_bom_by_submission(submission_id)
        return self._bom_view(bom) if bom is not None else None

    # ══════════════════════════════════════════════════════════════════════
    # Stage-2 BOM build — the bom-owned half of the Stage-1 → Stage-2 trigger.
    # The procurement service orchestrates the submission lifecycle and calls this.
    # ══════════════════════════════════════════════════════════════════════
    async def generate_for_order(
        self, user, *, spec_bytes: bytes, filename: str,
        spec_type: str | None, client_match_code: str | None,
        client_id: uuid.UUID | None, submission_id: uuid.UUID | None = None,
        order_bytes: bytes | None = None, order_filename: str | None = None,
        order_mime: str | None = None, order_match_code: str | None = None,
        source_document_id: uuid.UUID | None = None,
    ) -> dict:
        """Build a DRAFT BOM from the accepted ORDER + SPEC sheets ALONE — no SKU breakdown
        exists yet. Parses the order sheet for the order qty + per-size + refs, creates the
        SpecSheet the engine extracts into, and runs generate_bom anchored on the submission.
        The Client→Order→Style→SKU tree is created later, at MD approval (materialize_breakdown).
        Knows NOTHING about submissions beyond the opaque id — procurement loads the bytes and
        marks the submission consumed around this call (CLAUDE.md §3.2: bom owns spec_sheet/bom)."""
        # Parse the order sheet off the event loop (openpyxl/pypdf are blocking). Best-effort:
        # anything unparsed is a warning carried in `parsed_order["warnings"]`, never a reject.
        parsed_order: dict = {}
        if order_bytes:
            parsed_order = await run_in_threadpool(
                extract_order, order_bytes, order_filename or "order",
                order_mime or _sniff_mime(order_bytes),
                order_match_code or client_match_code,
            )
        print("extract_order result:", parsed_order)
        logger.info(f"return full detail from generate_for_order: {parsed_order}")
            
        order_extraction_row: OrderExtraction | None = None
        if order_bytes:
            order_extraction_row = self._build_order_extraction_row(
                parsed_order, source_document_id=source_document_id,
            )
            await self.repo.add_order_extraction(order_extraction_row)
            # Promotion stamp happens later, after the Bom row is created (in
            # generate_bom). We pass the row's id in via order_identity so the
            # downstream code can find it. (Alternative: pass the row itself
            # through the call chain — pick whichever is cleaner for your code.)
            parsed_order["_order_extraction_id"] = str(order_extraction_row.id)
        
        identity = self._identity_from_order(parsed_order, client_id)

        # The SpecSheet row the engine extracts into. style_id is None (no style yet);
        # spec_type drives adapter selection; attributes start empty and are filled by
        # extraction inside generate_bom.
        spec_sheet = SpecSheet(
            client_id=client_id, style_id=None,
            source_document_id=source_document_id,
            spec_type=spec_type or "unknown", attributes={},
        )
        await self.repo.add(spec_sheet)

        # line_seeds=None → generate_bom builds them AFTER extraction from the spec's REAL
        # empty dict the SpecSheet was born with, so only cost lines ever seeded).
        # attributes (the empty-attributes ordering fix; previously they were built off the
        # build_default_extractor() wires the Gemini→Groq attribute extractor for narrative
        # tech packs (None when no key is set → deterministic prose parse; measurement grids
        # never call it). The order sheet's own Gemini-vision path lives in extract_order.
        return await self.generate_bom(
            user, spec_sheet=spec_sheet, spec_bytes=spec_bytes,
            filename=filename or "spec", identity=identity,
            client_match_code=client_match_code, line_seeds=None,
            submission_id=submission_id, order_identity=parsed_order,
        )

    def _identity_from_order(self, parsed_order: dict,
                             client_id: uuid.UUID | None) -> StyleIdentity:
        """Build StyleIdentity from the parsed order sheet (no SKUs exist yet). order_qty /
        per_size come straight off the order sheet; client_id is passed in by procurement
        from the submission. client_order_id / style_id stay None until the breakdown is
        materialised at approval (materialize_breakdown)."""
        po = parsed_order or {}
        return StyleIdentity(
            client_id=client_id, client_order_id=None, style_id=None,
            customer_ref=po.get("customer_ref"), internal_ref=po.get("internal_ref"),
            name=po.get("style_name"), order_qty=int(po.get("order_qty") or 0),
            per_size_qty=po.get("per_size_qty") or {},
            order_number=po.get("order_number"),
        )

    @staticmethod
    def _order_identity_snapshot(identity: StyleIdentity,
                                 parsed_order: dict | None) -> dict:
        """The persisted order snapshot read back by _resolve_identity + breakdown
        materialisation while style_id is NULL. Merges the parsed order sheet (colour
        lines, season, price) with the resolved identity fields; `warnings` are dropped
        (they surface in the generate response, not the stored record)."""
        oi = dict(parsed_order or {})
        oi.pop("warnings", None)
        oi.pop("_order_extraction_id", None)
        oi["client_id"] = str(identity.client_id) if identity.client_id else None
        oi["order_qty"] = identity.order_qty
        oi["per_size_qty"] = identity.per_size_qty
        oi["order_number"] = identity.order_number
        oi["style_name"] = identity.name
        oi["customer_ref"] = identity.customer_ref
        oi["internal_ref"] = identity.internal_ref
        return oi

    @staticmethod
    def _build_line_seeds(spec_attributes: dict | None,
                          garment_code: str | None) -> list[LineSeed]:
        """Minimal seed builder (§5b): material/accessory lines from the spec's extracted
        attributes (when present) + the configured cost lines from cost_catalog.yaml.
        Unit prices on material lines are left blank for the cutting manager to fill."""
        attrs = spec_attributes or {}
        seeds: list[LineSeed] = []

        leather = attrs.get("leather_quality")
        if leather:
            seeds.append(LineSeed(
                category=BomItemCategory.MAIN_MATERIAL.value, name=str(leather),
                material_color=attrs.get("primary_color"), uom="dm²",
                source_ref="spec.attributes.leather_quality"))
        lining = attrs.get("lining")
        if lining and "unlined" not in str(lining).lower():
            seeds.append(LineSeed(
                category=BomItemCategory.LINING.value, name=str(lining), uom="dm²",
                source_ref="spec.attributes.lining"))
        for acc in attrs.get("accessories") or []:
            if not isinstance(acc, dict):
                continue
            name = acc.get("spec") or acc.get("type") or "accessory"
            seeds.append(LineSeed(
                category=BomItemCategory.ACCESSORY.value, name=str(name),
                material_color=acc.get("finish"), uom="pc", qty_per_garment=1,
                supplied_by=acc.get("supplied_by"),
                source_ref="spec.attributes.accessories"))

        catalog = load_cost_catalog()
        cost_lines = catalog.get((garment_code or "").upper()) or catalog.get("_default") or []
        for ln in cost_lines:
            # A malformed cost_catalog.yaml row (missing category/name) is a config error,
            # not a reason to 500 the whole generation — skip it with a warning.
            category, name = ln.get("category"), ln.get("name")
            if not category or not name:
                logger.warning("skipping malformed cost_catalog line (need category+name): %r", ln)
                continue
            seeds.append(LineSeed(
                category=category, name=name, uom=ln.get("uom"),
                unit_price=ln.get("unit_price"),
                qty_per_garment=ln.get("qty_per_garment", 1)))
        return seeds

    # ══════════════════════════════════════════════════════════════════════
    # Generation (§1, §2, §5, §6)
    # ══════════════════════════════════════════════════════════════════════
    async def generate_bom(
        self, user, *, spec_sheet, spec_bytes: bytes, filename: str,
        identity: StyleIdentity, client_match_code: str | None,
        line_seeds: list[LineSeed] | None = None, currency: str | None = None,
        checks_cfg: tuple | None = None,
        submission_id: uuid.UUID | None = None, order_identity: dict | None = None,
    ) -> dict:
        # `adapters`/`checks_cfg` default to None → the shipped YAML registries.
        # Passing them in lets a brand-new client be onboarded by CONFIG ALONE (the
        # prod path being a YAML entry the loaders read) — proves §11.6 end-to-end.
        # `line_seeds=None` → build them below from the EXTRACTED attributes (the trigger
        # path); a non-None list (legacy/direct callers) is honoured as-is.
        # ── 1. extract POMs / attributes / pattern ref (threadpool) ───────────
        logger.info("generate_bom start: order=%s style=%s spec_type=%s client_match=%s seeds=%s",
                    identity.client_order_id, identity.style_id, spec_sheet.spec_type,
                    client_match_code, len(line_seeds) if line_seeds is not None else "auto")
        # AFTER (steps 1 + 2):
        # ── 1. extract POMs (NATIVE terms) / attributes / pattern ref ─────────
        # LLM-primary (Gemini → Groq → manual fallback). Emits native source terms
        # only — no pom_code. Term → code resolution is THIS service's job (the DB
        # `pom_dictionary` is the single source of truth). Never raises: failures
        # surface as `manual_entry_required` in `intermediate["warnings"]`.
        intermediate = await run_in_threadpool(
            extract_spec, spec_bytes, filename, _sniff_mime(spec_bytes),
            spec_sheet_id=str(spec_sheet.id),
        )
        
        logger.info("return full detail from generate_for_spec",intermediate)
        
        spec_extraction_row = self._build_spec_extraction_row(
            intermediate, source_document_id=spec_sheet.source_document_id,
        )
        await self.repo.add_spec_extraction(spec_extraction_row)

        # ── 1b. detect total extraction failure (LLM unavailable / both failed) ─
        # When extraction fails entirely, we still build a DRAFT BOM (with the
        # configured cost lines from cost_catalog.yaml) so the cutting manager has
        # a place to enter materials manually. The caller (router → frontend) sees
        # `manual_entry_required: True` in the response and surfaces the prompt.
        ext_warnings = intermediate.get("warnings") or []
        manual_entry_required = "manual_entry_required" in ext_warnings

        # ── 2. resolve native terms → pom_code, then persist (replace-on-key) ─
        pom_dict = PomDict(await self.repo.pom_dictionary_rows())
        resolved_poms: list[dict] = []
        unresolved: list[dict] = []
        for p in intermediate["poms"]:
            term = p.get("source_term") or ""
            code = pom_dict.resolve(term, _term_language(term))
            if not code:
                unresolved.append({"source_term": term, "by_size": p.get("by_size")})
                continue
            resolved_poms.append({**p, "pom_code": code})

        pom_rows: list[PomMeasurement] = []
        seen: set[tuple[str, str]] = set()
        for p in resolved_poms:
            for size, value in p["by_size"].items():
                key = (str(size), p["pom_code"])
                if key in seen:           # guard the (spec_sheet_id, size, pom_code) unique constraint
                    logger.warning("duplicate (size=%s pom=%s) — keeping first", size, p["pom_code"])
                    continue
                seen.add(key)
                conf = p.get("confidence")
                pom_rows.append(PomMeasurement(
                    spec_sheet_id=spec_sheet.id, size=size, pom_code=p["pom_code"],
                    value=Decimal(str(value)),
                    pitch=Decimal(str(p["pitch"])) if p.get("pitch") is not None else None,
                    source_term=p.get("source_term"),
                    extracted_by=p.get("extracted_by") or intermediate.get("extracted_by")
                    or ExtractionSource.MANUAL.value,
                    confidence=Decimal(str(conf)) if conf is not None else Decimal("0.00"),
                ))
        await self.repo.replace_pom_measurements(spec_sheet.id, pom_rows)

        # ── 3. merge extracted attributes into the spec sheet ─────────────────
        if intermediate.get("attributes"):
            spec_sheet.attributes = {**(spec_sheet.attributes or {}),**intermediate["attributes"]}
            await self.repo.save(spec_sheet)

        # ── 3b. seed the BOM lines AFTER extraction (the ordering fix) ────────
        # Material/lining/accessory seeds now come from the spec's REAL attributes (not
        # the empty dict the SpecSheet was born with), and the cost lines are keyed by the
        # detected garment type. An explicit line_seeds (legacy/direct callers) is kept.
        if line_seeds is None:
            line_seeds = self._build_line_seeds(
                spec_sheet.attributes, garment_code=intermediate.get("garment_type_guess"))

        # Resolve the garment type up-front: the pattern-template lookup below and the
        # DCM memory back-fill both key templates on garment_type_id, so the lookup MUST
        # use the same gt_id the back-fill writes (was hard-coded None → never matched).
        gt = await self.repo.get_garment_type(intermediate.get("garment_type_guess"))
        gt_id = gt.id if gt else None

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
            # The base style was back-filled under THIS garment type, so key on gt_id.
            tmpl = await self.repo.find_consumption_template(
                client_id=identity.client_id,
                style_signature=pr["pattern_code"].strip().upper(),
                garment_type_id=gt_id, material_category=BomItemCategory.MAIN_MATERIAL.value,
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
        sig = style_signature(customer_ref=identity.customer_ref,
                              internal_ref=identity.internal_ref, name=identity.name)
        sizes = intermediate.get("sizes") or []
        base_size = (intermediate.get("pattern_reference") or {}).get("base_size") \
            or (sizes[0] if sizes else None) \
            or (intermediate.get("attributes") or {}).get("sms_size")
        poms_for_size = self._poms_by_size(resolved_poms)

        # ── 6. build bom_items with DCM resolution + costing ──────────────────
        bom = Bom(
            submission_id=submission_id, client_id=identity.client_id,
            client_order_id=identity.client_order_id, style_id=identity.style_id,
            status=BomStatus.DRAFT.value, currency=currency,
            order_qty=identity.order_qty, revision=1,
            garment_type_id=gt_id, dcm_base_size=base_size,
            order_identity=self._order_identity_snapshot(identity, order_identity),
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
            "poms": resolved_poms, "sizes": sizes,
            "per_size_qty": identity.per_size_qty, "order_qty": identity.order_qty,
            "pattern_reference": pr_block,
        }
        flags = checks_mod.run_checks(client_match_code, ctx, checks_cfg)

        # Prepend a clear flag when the extractor couldn't read the spec — this
        # supersedes the cascade of "missing X" check warnings the user would
        # otherwise see, and tells the cutting manager up-front: enter manually.
        if manual_entry_required:
            flags = [{
                "code": "extraction_failed",
                "severity": "error",
                "message": ("Spec sheet could not be read automatically. "
                            "Please enter material lines manually before submitting for review."),
                "details": ext_warnings,
            }] + flags
            
        # Also surface order-sheet extraction warnings (manual_entry_required on the
        # order side, dropped negative qtys, etc.) as a flag — these affect the
        # quantities the cutting manager will plan against.
        order_warnings = (order_identity or {}).get("warnings", [])
        if "manual_entry_required" in order_warnings:
            flags = [{
                "code": "order_extraction_failed",
                "severity": "error",
                "message": ("Order sheet could not be read automatically. "
                            "Order quantities must be entered manually."),
                "details": order_warnings,
            }] + flags

        await self._audit(user, "BOM_GENERATE", bom.id, after=self._bom_snapshot(bom))
        logger.info("generate_bom done: bom=%s items=%d fob=%s flags=%d unresolved=%d manual=%s",
                    bom.id, len(bom.items), bom.garment_fob_price, len(flags),
                    len(unresolved), manual_entry_required)
        
        now = datetime.now(timezone.utc)
        spec_extraction_row.promoted_to_spec_sheet_id = spec_sheet.id
        spec_extraction_row.promoted_at = now
        await self.repo.save(spec_extraction_row)
 
        # Order side — find the row we wrote earlier via the id we tucked into
        # parsed_order (above in generate_for_order). Only present when there
        # was an order to extract.
        order_extraction_id = (order_identity or {}).get("_order_extraction_id")
        if order_extraction_id:
            order_extraction_row = await self.repo.get_order_extraction(
                uuid.UUID(order_extraction_id),
            )
            if order_extraction_row is not None:
                order_extraction_row.promoted_to_bom_id = bom.id
                order_extraction_row.promoted_at = now
                await self.repo.save(order_extraction_row)
            
        return {
            "bom": self._bom_view(bom),
            "flags": flags,
            "order": {
                "order_qty": identity.order_qty,
                "per_size_qty": identity.per_size_qty,
                "warnings": (order_identity or {}).get("warnings", []),
            },
            "extraction": {
                "poms": len(pom_rows),
                "unresolved": unresolved,
                "pattern_reference": pr_block,
                "warnings": ext_warnings,
                "manual_entry_required": manual_entry_required,
            },
        }
    # ── the ordered DCM fallback (§2 Sources 1→3; Source 4 is the edit/confirm) ─
    async def _resolve_dcm(self, *, client_id, style_signature, garment_type_id,
                           garment_type_row, material_category, size, poms_for_size,
                           pattern_template_id):
        # Normalise the lookup key: confirm_cutting back-fills templates under
        # `base_size or ""`, so a missing base size is stored under "". Read under the
        # SAME key (was guarded on `size is not None` + raw size → a no-base-size value
        # written under "" was never reachable). Source 3 still uses the raw `size`.
        size_key = size if size is not None else ""
        # Source 1 — the DCM memory (exact, confirmed)
        tmpl = await self.repo.find_consumption_template(
            client_id=client_id, style_signature=style_signature,
            garment_type_id=garment_type_id, material_category=material_category, size=size_key,
        )
        if tmpl:
            return Decimal(str(tmpl.dcm_value)), DcmSource.TEMPLATE
        # Source 1b — via a resolved pattern reference's template
        if pattern_template_id is not None:
            tmpl = await self.repo.get_consumption_template(pattern_template_id)
            if tmpl and tmpl.material_category == material_category:
                return Decimal(str(tmpl.dcm_value)), DcmSource.TEMPLATE
        # Source 2 — similar style (rule-based nearest neighbour)
        sim = await self.repo.find_similar_template(
            client_id=client_id, garment_type_id=garment_type_id,
            material_category=material_category, size=size_key, exclude_signature=style_signature,
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
            # DCM, qty and price are physical magnitudes — never negative.
            if value < 0:
                raise HTTPException(422, detail={"error": "negative_value",
                                                 "bom_item_id": e.get("bom_item_id"),
                                                 "field": field_, "value": e.get("value")})
            if field_ in ("dcm", "qty_per_garment"):
                item.qty_per_garment = value
                # The DCM stamp + gate re-open apply ONLY to leather AREA material lines
                # (the lines DCM resolution governs). A quantity edit on a non-material
                # line (accessory/manufacturing/packaging/FOB) is just a qty change — it
                # must not write a bogus dcm_source nor bounce a confirmed BOM out of review.
                if item.category in MATERIAL_DCM_CATEGORIES:
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
        # State guard: cutting confirmation is only valid from DRAFT (the pre-confirm
        # state) or, idempotently, from READY_FOR_REVIEW (a re-confirm). Confirming an
        # APPROVED/LOCKED/EXPORTED BOM would silently un-approve it (reset to
        # ready_for_review, re-fire MD notices, re-run the template back-fill); a
        # REJECTED BOM must go through reopen_bom (which bumps the revision) first.
        if bom.status not in (BomStatus.DRAFT.value, BomStatus.READY_FOR_REVIEW.value):
            raise HTTPException(409, detail={
                "error": "invalid_state_for_confirmation",
                "current_status": bom.status,
                "message": "Cutting confirmation is only allowed on a draft BOM."})
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
        # The client has signed off (the internal MD/cutting approval IS that sign-off):
        # materialise the Client→Order→Style→SKU breakdown from the parsed order sheet
        # and back-link it onto the BOM FIRST. A breakdown failure must abort the whole
        # approval (downstream depends on the order/style), so it runs BEFORE the approval
        # is stamped/committed — otherwise the approval commits and a later breakdown
        # failure leaves a locked BOM with no order/style. Idempotent on re-approval.
        await self._materialize_breakdown(user, bom)
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
        # Capture before the best-effort side-effects: a failure there triggers a session
        # rollback which would expire `bom`, and re-reading an expired attribute under
        # async raises MissingGreenlet.
        bom_id_str, bom_status = str(bom.id), bom.status
        # Stage 5 (§8c): advance the production board BOM_APPROVED edge as a service→service
        # side-effect. Best-effort — the board never blocks a valid approval. On failure we
        # roll back (so a half-written board edge doesn't poison the session for the next
        # call) and log; the approval itself is already committed above and stands.
        try:
            from app.modules.supplier_po.production_tracking_service import (
                ProductionTrackingService,
            )
            await ProductionTrackingService(self.db).on_bom_approved(bom.id)
        except Exception:
            logger.exception("production board advance failed for bom=%s", bom_id_str)
            await self.repo.rollback()
        # Stage 4 (§8c): approval auto-fires the inventory check so the freshly approved
        # BOM lands on the procurement dashboard with its stock badge without a manual
        # step. Best-effort — a check failure must not roll back a valid approval; the
        # explicit POST /boms/{id}/inventory-check re-run is always available.
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
        """Create the Client→Order→Style→SKU tree from the BOM's parsed order snapshot and
        back-link it onto the BOM (and the submission). Idempotent: a BOM that already has a
        style is left alone (re-approval / export). The client tables are written via
        clients.service — bom never writes them directly (CLAUDE.md §3.2 table ownership)."""
        if bom.style_id is not None:
            return                                       # already materialised
        # Capture everything off the BOM BEFORE create_order_with_breakdown commits — that
        # commit expires this instance, and reading an expired attribute afterwards would
        # trigger a lazy load (MissingGreenlet under async). We only WRITE bom after.
        oi = bom.order_identity or {}
        cid = oi.get("client_id")
        client_id = bom.client_id or (uuid.UUID(cid) if isinstance(cid, str) else cid)
        bom_id = bom.id
        submission_id = bom.submission_id
        source_document_id = bom.source_document_id
        if client_id is None:
            logger.warning("breakdown skipped: bom=%s has no client_id (order sheet did not "
                           "resolve a client) — order/style not created", bom_id)
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
        # Denormalised convenience link on the submission (table owned by procurement, so
        # written through its service — a lazy import avoids a module-load cycle, mirroring
        # how procurement lazily imports bom.service). Best-effort: the order is always
        # resolvable via bom.submission_id, so a link failure must not fail approval.
        if submission_id is not None:
            try:
                from app.modules.procurement.service import ProcurementService

                await ProcurementService(self.db).link_submission_to_order(
                    submission_id, order_id)
            except Exception:
                logger.exception("failed to link submission %s → order %s",
                                 submission_id, order_id)
        await self._audit(user, "BOM_BREAKDOWN", bom_id,
                          after={"client_order_id": str(order_id), "style_id": str(style_id)})

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
        """Resolve order/style identity for the DCM back-fill / export when the caller
        didn't pass it in. A submission-anchored BOM (style_id still NULL) reads it off the
        persisted order snapshot; a materialised/legacy BOM reads it via clients.service
        (a permitted service→service call)."""
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
    def _build_spec_extraction_row(
        intermediate: dict,
        source_document_id: uuid.UUID | None,
    ) -> SpecExtraction:
        """Turn an extract_spec intermediate dict into a SpecExtraction row. The
        full intermediate becomes raw_payload (audit truth); a handful of fields
        are denormalized as columns for dashboard queries.
    
        Called BEFORE the operational tables (spec_sheet, pom_measurement) are
        written, so the staging row exists even when promotion fails. The caller
        stamps promoted_at + promoted_to_spec_sheet_id once promotion completes."""
        warnings = intermediate.get("warnings") or []
        measurements = intermediate.get("measurements") or intermediate.get("poms") or []
        accessories = (intermediate.get("attributes") or {}).get("accessories") or []
        conf = intermediate.get("confidence_overall")
        return SpecExtraction(
            source_document_id=source_document_id,
            extracted_by=intermediate.get("extracted_by") or "manual",
            confidence_overall=Decimal(str(conf)) if conf is not None else None,
            raw_payload=intermediate,
            style_no=intermediate.get("style_no"),
            client_name=intermediate.get("client_name"),
            garment_type_guess=intermediate.get("garment_type_guess"),
            num_measurements=len(measurements),
            num_accessories=len(accessories),
            num_warnings=len(warnings),
            manual_entry_required="manual_entry_required" in warnings,
        )
        
        
    @staticmethod
    def _build_order_extraction_row(
        parsed_order: dict,
        source_document_id: uuid.UUID | None,
    ) -> OrderExtraction:
        """Turn an extract_order intermediate dict into an OrderExtraction row.
        Same shape as the spec version above."""
        warnings = parsed_order.get("warnings") or []
        lines = parsed_order.get("lines") or []
        conf = parsed_order.get("confidence_overall")
        return OrderExtraction(
            source_document_id=source_document_id,
            extracted_by=parsed_order.get("extracted_by") or "manual",
            confidence_overall=Decimal(str(conf)) if conf is not None else None,
            raw_payload=parsed_order,
            order_number=parsed_order.get("order_number"),
            style_no=parsed_order.get("style_no") or parsed_order.get("style_name"),
            client_name=parsed_order.get("client_name"),
            season=parsed_order.get("season"),
            currency=parsed_order.get("currency"),
            order_qty=int(parsed_order.get("order_qty") or 0),
            num_lines=len(lines),
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
