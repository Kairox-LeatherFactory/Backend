"""
================================================================================
modules/procurement/service.py — Stage-1 upload & validation orchestration
================================================================================

The business brain of Stage 1 (the workflow's front door). Owns:
  - pairing two uploads into one `submission` (the surrogate that becomes the
    client_order link in Stage 2);
  - the scan → sniff → validate pipeline (pushed to a threadpool) and persistence;
  - idempotency on re-upload (sha256 dedupe + cached classification → no second
    LLM bill), supersede-on-correction, and lock-after-Stage-2;
  - the Stage-2 readiness gate (both slots accepted + scan clean/skipped).

RBAC is enforced at the router via require_roles(DIRECT_MANAGER, MANAGING_DIRECTOR);
MD bypasses as superuser. The heavy, blocking work (openpyxl, pypdf, libmagic,
clamd, the optional LLM call) runs in run_in_threadpool — the event loop is never
blocked, mirroring the imports handler.

FUNCTION / METHOD GUIDE  (router → ProcurementService)
  ProcurementService(db, *, classifier?, scanner?)
      classifier/scanner are INJECTABLE so tests drive the LLM/AV paths without a key or
      a running clamd. _get_classifier() lazily builds the config default.
  open_submission(user, client_id?) -> Submission   mint an empty batch. → POST /submissions.
  _load_submission(id) [private] fetch or 404.
  upload_order_sheet / upload_spec_sheet(user, submission_id, data, filename) -> dict
      Thin wrappers over _upload_slot for each slot. → the two slot endpoints.
  _upload_slot(user, submission_id, kind, data, filename) -> dict   [private]
      THE CORE FLOW: reject if the submission is CONSUMED (locked); sha256; idempotent
      re-hit short-circuit; load the kind's client_template profiles; run the blocking
      process_upload pipeline in a threadpool (scan→sniff→validate→quarantine/promote);
      persist the Document (accepted OR rejected — caches by sha); on accept fill the slot
      + recompute the gate; on reject raise UploadError with diagnostics.
  _handle_existing(sub, kind, existing) [private] the sha256 cache hit — replay the stored
      verdict (re-point the slot if accepted; replay diagnostics if not) — NO second LLM bill.
  get_submission_status(id) -> dict   the slots + ready_for_stage_2 gate. → GET /submissions/{id}.
  get_document_report(submission_id, document_id) -> dict   the full per-doc validation report.
  _build_document(...) [private] map the PipelineResult → a Document row.
  _accept_into_slot(...) [private] fill the slot, supersede a prior doc, recompute status.
  _recompute_status / _slot_ok [private] the completeness gate logic.
  _audit_virus(...) [private] write the VIRUS_DETECTED audit row.
  _to_profile(t) [module fn] ORM client_template row → a session-free ProfileView for the validator.
================================================================================
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.modules.procurement.classifier import (
    Classifier,
    VisionClassifier,
    build_default_classifier,
    build_vision_classifier,
)
from app.core.enums import DocumentKind
from app.modules.procurement.enums import (
    RejectReason,
    SubmissionStatus,
    ValidationStatus,
)
from app.modules.bom.service import BomService
from app.modules.procurement import presenters
from app.modules.procurement.errors import UploadError
from app.core.models import AuditLog, Document
from app.modules.procurement.models import Submission
from app.modules.procurement.pipeline import PipelineResult, process_upload, sha256_of
from app.modules.procurement.registry import ProfileView
from app.modules.procurement.repository import ProcurementRepository
from app.modules.procurement.scanning import Scanner

_UNSET = object()

logger = logging.getLogger(__name__)

# Which Document column / submission slot each kind writes into.
_SLOT_ATTR = {
    DocumentKind.ORDER_SHEET.value: "order_document_id",
    DocumentKind.SPEC_SHEET.value: "spec_document_id",
}


class ProcurementService:
    def __init__(self, db: AsyncSession, *, classifier=_UNSET,
                 vision_classifier=_UNSET, scanner: Scanner | None = None):
        self.db = db
        self.repo = ProcurementRepository(db)
        # classifier/vision_classifier/scanner are injectable so tests can drive the
        # LLM/vision/AV paths deterministically without a key or a running clamd (see
        # langgraph_agent's fake-model pattern). Defaults are built lazily from config.
        self._classifier = classifier
        self._vision_classifier = vision_classifier
        self._scanner = scanner

    def _get_classifier(self) -> Classifier | None:
        if self._classifier is _UNSET:
            self._classifier = build_default_classifier()
        return self._classifier

    def _get_vision_classifier(self) -> VisionClassifier | None:
        if self._vision_classifier is _UNSET:
            self._vision_classifier = build_vision_classifier()
        return self._vision_classifier

    # ══════════════════════════════════════════════════════════════════════
    # Submissions
    # ══════════════════════════════════════════════════════════════════════
    async def open_submission(self, user, client_id: uuid.UUID | None) -> Submission:
        return await self.repo.create_submission(
            client_id=client_id, created_by=getattr(user, "id", None),
            status=SubmissionStatus.OPEN.value,
        )
    # consumed means lock 
    async def _load_submission(self, submission_id: uuid.UUID) -> Submission:
        sub = await self.repo.get_submission(submission_id)
        if sub is None:
            raise HTTPException(404, "Submission not found.")
        return sub

    # ══════════════════════════════════════════════════════════════════════
    # Upload + validate a slot
    # ══════════════════════════════════════════════════════════════════════
    async def upload_order_sheet(self, user, submission_id, data, filename,
                                 override_manual_review: bool = False) -> dict:
        sub_id = await self._resolve_or_create_submission(user, submission_id)
        return await self._upload_slot(user, sub_id, DocumentKind.ORDER_SHEET.value,
                                       data, filename, override_manual_review)

    async def upload_spec_sheet(self, user, submission_id, data, filename,
                                override_manual_review: bool = False) -> dict:
        sub_id = await self._resolve_or_create_submission(user, submission_id)
        return await self._upload_slot(user, sub_id, DocumentKind.SPEC_SHEET.value,
                                       data, filename, override_manual_review)

    async def _resolve_or_create_submission(
        self, user, submission_id: uuid.UUID | None
    ) -> uuid.UUID:
        """Return an existing submission_id as-is, or create a new OPEN submission
        when none is provided. The submission is created HERE (before the pipeline)
        so the document row has a valid submission_id FK. If the pipeline later rejects
        the file, the empty submission stays in OPEN state — it will never reach COMPLETE
        and can be ignored or GC'd. Only a successful accept fills a slot and makes the
        submission useful."""
        if submission_id is not None:
            return submission_id
        sub = await self.repo.create_submission(
            client_id=None,
            created_by=getattr(user, "id", None),
            status=SubmissionStatus.OPEN.value,
        )
        return sub.id
 
    async def _upload_slot(self, user, submission_id, kind, data, filename,
                           override_manual_review: bool = False) -> dict:
        sub = await self._load_submission(submission_id)
        # Lock-after-Stage-2, NARROWED to the ORDER slot: once the breakdown has
        # claimed the submission (CONSUMED), the order document is the source of
        # truth for the style/qty breakdown and must not change under it. Spec
        # sheets are the OPPOSITE case — in the guided flow they are EXPECTED to
        # arrive after the breakdown (one per style) and attach via
        # POST /boms/order-styles/{id}/attachments, so spec uploads stay open.
        if (sub.status == SubmissionStatus.CONSUMED.value
                and kind == DocumentKind.ORDER_SHEET.value):
            raise UploadError(
                RejectReason.SUBMISSION_LOCKED,
                "Order already consumed by the style breakdown; start a new "
                "submission to change the order document.",
                payload=presenters.fingerprint(filename, data),
            )

        sha = sha256_of(data)
        logger.info("upload received: submission=%s kind=%s filename=%s size=%d sha=%s force=%s",
                    submission_id, kind, filename, len(data), sha[:12], override_manual_review)

        # ── TESTING PHASE: dedupe/idempotency check disabled ──────────────────
        # Re-uploading byte-identical content now always re-runs the full pipeline as
        # if it were a brand-new file (no duplicate_content 409, no sha-cache replay).
        # To restore production dedupe behavior, uncomment the block below.
        # existing = await self.repo.get_document_by_sha(sha)
        # if existing is not None:
        #     replay = await self._handle_existing(sub, kind, existing)
        #     if replay is not None:
        #         return replay

        # ── run the blocking pipeline off the event loop ─────────────────────
        templates = await self.repo.active_templates(kind)
        profiles = [_to_profile(t) for t in templates]
        classifier = self._get_classifier()
        vision_classifier = self._get_vision_classifier()
        try:
            result: PipelineResult = await run_in_threadpool(
                process_upload, data, filename, str(submission_id), kind, profiles,
                classifier=classifier, vision_classifier=vision_classifier,
                scanner=self._scanner,
            )
        except UploadError as exc:
            logger.warning("upload hard-gated: submission=%s kind=%s reason=%s",
                           submission_id, kind, exc.reason.value)
            if exc.reason == RejectReason.VIRUS_DETECTED:
                await self._audit_virus(user, filename, data, sha, exc)
            raise presenters.enrich_gate_error(exc, sub, filename, data, kind, sha)

        o = result.outcome
        logger.info("validation verdict: submission=%s kind=%s status=%s method=%s conf=%.2f "
                    "client_match=%s reason=%s", submission_id, kind, o.status.value,
                    o.method.value, o.confidence, o.client_match,
                    o.reason_code.value if o.reason_code else None)

        # ── persist the document row (accepted OR rejected — caches by sha) ───
        doc = self._build_document(user, sub, kind, filename, sha, result)

        # ── manual-review OVERRIDE (force) ────────────────────────────────────
        # A foreign-language / low-confidence doc that the classifier could only call
        # `needs_manual_review` can be force-accepted by the DM/MD so Stage 2 still
        # generates a (fully editable) BOM the user corrects by hand. The override is
        # deliberately NARROW: only needs_manual_review is overridable — a hard reject
        # (virus, wrong-slot, not-the-kind, too-large) is NEVER bypassed. The doc is
        # accepted but flagged so the audit trail shows a human, not the gate, let it in.
        forced = (
            override_manual_review
            and not o.accepted
            and o.status == ValidationStatus.NEEDS_MANUAL_REVIEW
        )
        if forced:
            doc.validation_status = ValidationStatus.ACCEPTED.value
            sig = dict(doc.validation_signals or {})
            sig["manual_override"] = True
            sig["manual_override_by"] = str(getattr(user, "id", None))
            sig["original_status"] = o.status.value
            doc.validation_signals = sig
            # The pipeline only PROMOTES bytes (quarantine → submissions/) when the verdict
            # is `accepted`; for a needs_manual_review verdict it DELETES the quarantined
            # object and leaves storage_url=None. A force-accepted doc must still have its
            # bytes in submissions/ or Stage-2 generation can't load them (FileNotFoundError).
            # Promote the in-memory bytes here so the override→generate flow works end-to-end.
            slot_dir = {DocumentKind.ORDER_SHEET.value: "order-sheet",
                        DocumentKind.SPEC_SHEET.value: "spec-sheet"}.get(kind, kind)
            from app.core.storage import get_storage, submission_key
            from app.modules.procurement.sniffing import _EXT_FOR_MIME
            skey = submission_key(str(sub.id), slot_dir, sha,
                                  _EXT_FOR_MIME.get(result.feats.mime, ""))
            try:
                doc.storage_url = await run_in_threadpool(get_storage().put, skey, data)
            except Exception:
                logger.exception("failed to promote force-accepted %s bytes for submission=%s",
                                 kind, submission_id)
            logger.warning("MANUAL-REVIEW OVERRIDE accepted: submission=%s kind=%s sha=%s by=%s "
                           "(was %s, conf=%.2f)", submission_id, kind, sha[:12],
                           getattr(user, "id", None), o.status.value, o.confidence)

        await self.repo.add_document(doc)

        if o.accepted or forced:
            await self._accept_into_slot(sub, kind, doc)
            logger.info("slot filled: submission=%s kind=%s doc=%s forced=%s",
                        submission_id, kind, doc.id, forced)
            if forced:
                await self._audit_override(user, sub, kind, doc, o)
            return presenters.success_envelope(sub, doc)

        # rejected / needs_manual_review (and NOT force-overridden) → 4xx diagnostics (§5b).
        # The diagnostics now carry a `can_override` hint when the only blocker was low
        # confidence, so the FE can offer "accept anyway & edit the BOM" for this case.
        envelope = presenters.rejection_envelope(sub, doc, o)
        if o.status == ValidationStatus.NEEDS_MANUAL_REVIEW:
            envelope["validation"]["can_override"] = True
            envelope["validation"]["override_hint"] = (
                "Re-upload with force=true (override_manual_review) to accept this document "
                "and generate an editable BOM for manual correction."
            )
        logger.info("upload rejected to caller: submission=%s kind=%s status=%s",
                    submission_id, kind, o.status.value)
        raise UploadError(
            o.reason_code or RejectReason.NEEDS_MANUAL_REVIEW,
            f"Document failed validation: {o.status.value}",
            payload=envelope,
        )

    #── idempotent re-hit on an already-seen sha256 ──────────────────────────
    async def _handle_existing(self, sub, kind, existing: Document) -> dict | None:
        # The sha cache is a valid short-circuit ONLY for a TRUE idempotent re-upload:
        # the same bytes, into the SAME slot (kind), of the SAME submission. Two reasons
        # it must be scoped this tightly — both are silently-wrong otherwise:
        #   • cross-submission — `Document.sha256` is globally unique and
        #     `Document.submission_id` pins each row to ONE submission, so the bytes can
        #     neither be re-stored here nor borrow another submission's row. The old code
        #     returned success_envelope yet skipped _accept_into_slot → HTTP 201 "accepted"
        #     with an empty slot that can never reach COMPLETE (dead-end).
        #   • cross-kind (same submission, other slot) — repointing the spec slot at an
        #     accepted ORDER doc let one physical file masquerade as both sheets and the
        #     submission flipped COMPLETE with spec validation never run.
        # A byte-collision with a non-procurement Document (BOM/PO PDF: submission_id NULL,
        # kind bom_quote/supplier_po_pdf) also lands here and is correctly a clean 409.
        if existing.submission_id != sub.id or existing.kind != kind:
            raise UploadError(
                RejectReason.DUPLICATE_CONTENT,
                "A byte-identical file is already on record and cannot be reused for this slot.",
                payload=presenters.duplicate_envelope(sub, existing, kind),
            )

        if existing.validation_status == ValidationStatus.ACCEPTED.value:
            # Genuine same-slot re-upload → re-point the slot (no new row, no LLM bill).
            await self._accept_into_slot(sub, kind, existing)
            return presenters.success_envelope(sub, existing)

        # NEEDS_MANUAL_REVIEW is NOT terminal — it means "automated gates were inconclusive,
        # look again". Pinning it in the sha cache would make every re-upload replay the same
        # stalemate (and silently skip the OCR/vision rungs added later). Evict the stale row
        # and return None so the caller re-runs the full pipeline. Re-billing the LLM/vision is
        # the intended cost of resolving an unresolved doc; a hard reject below stays cached.
        if existing.validation_status == ValidationStatus.NEEDS_MANUAL_REVIEW.value:
            await self.repo.delete_document(existing)
            return None

        # A previously HARD-rejected file (not_an_order_sheet / wrong_slot / …) → replay
        # the cached diagnostics; that verdict is deterministic, no point re-billing.
        sig = existing.validation_signals or {}
        reason = sig.get("reason_code") or RejectReason.NEEDS_MANUAL_REVIEW.value
        raise UploadError(
            RejectReason(reason),
            "Document previously failed validation (cached result).",
            payload=presenters.rejection_envelope_from_row(sub, existing),
        )

    # ══════════════════════════════════════════════════════════════════════
    # Reads
    # ══════════════════════════════════════════════════════════════════════
    async def get_submission_status(self, submission_id: uuid.UUID) -> dict:
        sub = await self._load_submission(submission_id)
        order_doc = await self._slot_doc(sub.order_document_id)
        spec_doc = await self._slot_doc(sub.spec_document_id)
        return {
            "submission_id": str(sub.id),
            "status": sub.status,
            **presenters.submission_block(sub, order_doc, spec_doc),
        }

    async def get_document_report(self, submission_id: uuid.UUID, document_id: uuid.UUID) -> dict:
        sub = await self._load_submission(submission_id)
        doc = await self.repo.get_document(document_id)
        if doc is None or doc.submission_id != sub.id:
            raise HTTPException(404, "Document not found in this submission.")
        return {"submission_id": str(sub.id), "document": presenters.document_block(doc)}

    async def link_submission_to_order(self, submission_id: uuid.UUID,
                                       client_order_id: uuid.UUID) -> None:
        """Set the submission's client_order link once bom.service materialises the
        breakdown at approval. The submission table is procurement-owned, so bom routes
        this write through here (a permitted bom.service → procurement.service edge)."""
        sub = await self.repo.get_submission(submission_id)
        if sub is not None:
            sub.client_order_id = client_order_id
            await self.repo.save(sub)
            
    # ══════════════════════════════════════════════════════════════════════
    # Fan-out v2: the bom module's window into procurement-owned state.
    # Same permitted edge as link_submission_to_order — bom.service calls
    # THESE; it never touches submission/document rows directly.
    # ══════════════════════════════════════════════════════════════════════
    async def claim_submission_for_breakdown(self, submission_id: uuid.UUID) -> bool:
        """Atomic claim so two breakdown POSTs can't both enqueue: compare-and-set
        COMPLETE -> CONSUMED in one UPDATE. True exactly once. False when the
        submission is OPEN (order not accepted yet), already CONSUMED (someone
        else claimed), or unknown."""
        return await self.repo.cas_submission_status(
            submission_id,
            expect=SubmissionStatus.COMPLETE.value,
            to=SubmissionStatus.CONSUMED.value,
        )

    async def release_breakdown_claim(self, submission_id: uuid.UUID) -> None:
        """Worker failed before committing any breakdown rows — hand the claim
        back (CONSUMED -> COMPLETE) so the operator can re-trigger after a fix."""
        await self.repo.cas_submission_status(
            submission_id,
            expect=SubmissionStatus.CONSUMED.value,
            to=SubmissionStatus.COMPLETE.value,
        )

    async def is_breakdown_claimed(self, submission_id: uuid.UUID) -> bool:
        sub = await self.repo.get_submission(submission_id)
        return sub is not None and sub.status == SubmissionStatus.CONSUMED.value

    async def get_accepted_order_bytes(self, submission_id: uuid.UUID
                                       ) -> tuple[bytes, str, str | None] | None:
        """(bytes, filename, mime) of the submission's ACCEPTED order document,
        loaded from storage; None when the slot is empty or bytes are missing.
        This is what the breakdown worker extracts from."""
        sub = await self.repo.get_submission(submission_id)
        if sub is None or sub.order_document_id is None:
            return None
        doc = await self.repo.get_document(sub.order_document_id)
        if doc is None or not doc.storage_url:
            return None
        from app.core.storage import get_storage
        data = await run_in_threadpool(get_storage().get, doc.storage_url)
        return (data, doc.filename, doc.mime) if data else None

    async def get_submission_client_id(self, submission_id: uuid.UUID) -> uuid.UUID | None:
        """Return the submission's client_id, or None if the submission is missing."""
        sub = await self.repo.get_submission(submission_id)
        return sub.client_id if sub is not None else None

    async def spec_documents_for_client(self, client_id) -> list[Document]:
        """Accepted spec-sheet documents for this client — the suggestion pool
        the breakdown's name-matcher runs over."""
        return await self.repo.accepted_documents(
            kind=DocumentKind.SPEC_SHEET.value, client_id=client_id)

    async def _slot_doc(self, doc_id) -> Document | None:
        return await self.repo.get_document(doc_id) if doc_id else None

    # ══════════════════════════════════════════════════════════════════════
    # Persistence helpers
    # ══════════════════════════════════════════════════════════════════════
    def _build_document(self, user, sub, kind, filename, sha, result: PipelineResult) -> Document:
        o = result.outcome
        return Document(
            client_id=sub.client_id,
            kind=kind,                                   # the slot it was uploaded to
            filename=filename,
            mime=result.feats.mime,
            storage_url=result.storage_url,              # None unless promoted
            sha256=sha,
            page_count=result.feats.page_count,
            uploaded_by=getattr(user, "id", None),
            submission_id=sub.id,
            size_bytes=result.feats.size_bytes,
            validation_status=o.status.value,
            classified_kind=o.classified_kind,
            classified_spec_type=o.spec_type,
            classification_method=o.method.value,
            classification_confidence=o.confidence,
            client_match_code=o.client_match,
            scan_status=result.scan_status.value,
            validation_signals=presenters.signals_blob(o),
        )

    async def _accept_into_slot(self, sub: Submission, kind: str, doc: Document) -> None:
        attr = _SLOT_ATTR[kind]
        prior_id = getattr(sub, attr)
        if prior_id and prior_id != doc.id:
            prior = await self.repo.get_document(prior_id)
            if prior is not None and prior.validation_status == ValidationStatus.ACCEPTED.value:
                prior.validation_status = ValidationStatus.SUPERSEDED.value
        setattr(sub, attr, doc.id)
        sub.status = self._recompute_status(sub, doc, kind)
        await self.repo.save(sub)

    def _recompute_status(self, sub: Submission, just_accepted: Document, kind: str) -> str:
        # Fan-out v2: COMPLETE (= ready for the style breakdown) requires ONLY the
        # ORDER slot. The spec is per-STYLE now — one order document can carry 18
        # styles, each with its own spec sheet that arrives later and is attached
        # during the operator's confirm step. Requiring a spec here would deadlock
        # every multi-style submission. A spec uploaded up-front is still accepted
        # into its slot below — it simply joins the per-style suggestion pool
        # instead of gating readiness.
        if sub.status == SubmissionStatus.CONSUMED.value:
            return sub.status
        order_ok = self._slot_ok(sub.order_document_id, just_accepted,
                                 kind == DocumentKind.ORDER_SHEET.value)
        return SubmissionStatus.COMPLETE.value if order_ok else SubmissionStatus.OPEN.value

    @staticmethod
    def _slot_ok(slot_id, just_accepted: Document, is_this_slot: bool) -> bool:
        # The just-accepted doc is guaranteed clean+accepted; the other slot must
        # already hold an accepted doc id. (Scan acceptability is enforced on accept.)
        if is_this_slot:
            return True
        return slot_id is not None

    async def _audit_override(self, user, sub, kind, doc, outcome) -> None:
        """Record that a human (DM/MD) force-accepted a `needs_manual_review` document so
        Stage 2 could proceed — the audit trail must show the override was deliberate, by
        whom, and what the automated gate had actually said."""
        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None),
            action="DOCUMENT_MANUAL_OVERRIDE", entity_type="document", entity_id=doc.id,
            after={"submission_id": str(sub.id), "kind": kind, "sha256": doc.sha256,
                   "original_status": outcome.status.value,
                   "confidence": outcome.confidence,
                   "reason_code": outcome.reason_code.value if outcome.reason_code else None},
            at=datetime.now(timezone.utc),
        ))
        await self.repo.commit()

    async def _audit_virus(self, user, filename, data, sha, exc: UploadError) -> None:
        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None),
            action="VIRUS_DETECTED", entity_type="document", entity_id=None,
            after={"filename": filename, "sha256": sha,
                   "signature": exc.payload.get("scan_signature")},
            at=datetime.now(timezone.utc),
        ))
        await self.repo.commit()


# ── ORM row → session-free ProfileView ──────────────────────────────────────
def _to_profile(t) -> ProfileView:
    return ProfileView(
        client_code=t.client_code, doc_kind=t.doc_kind, display_name=t.display_name,
        expected_layout=t.expected_layout, spec_type_hint=t.spec_type_hint,
        anchors=t.anchors or [], fingerprints=t.fingerprints or [],
        grid_signals=t.grid_signals or {}, thresholds=t.thresholds or {},
    )