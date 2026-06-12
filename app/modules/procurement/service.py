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
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.modules.procurement.classifier import Classifier, build_default_classifier
from app.core.enums import DocumentKind
from app.modules.procurement.enums import (
    RejectReason,
    SubmissionStatus,
    ValidationStatus,
)
from app.modules.procurement import presenters
from app.modules.procurement.errors import UploadError
from app.core.models import AuditLog, Document
from app.modules.procurement.models import Submission
from app.modules.procurement.pipeline import PipelineResult, process_upload, sha256_of
from app.modules.procurement.registry import ProfileView
from app.modules.procurement.repository import ProcurementRepository
from app.modules.procurement.scanning import Scanner

_UNSET = object()

# Which Document column / submission slot each kind writes into.
_SLOT_ATTR = {
    DocumentKind.ORDER_SHEET.value: "order_document_id",
    DocumentKind.SPEC_SHEET.value: "spec_document_id",
}


class ProcurementService:
    def __init__(self, db: AsyncSession, *, classifier=_UNSET, scanner: Scanner | None = None):
        self.db = db
        self.repo = ProcurementRepository(db)
        # classifier/scanner are injectable so tests can drive the LLM/AV paths
        # deterministically without a key or a running clamd (see langgraph_agent's
        # fake-model pattern). Default classifier is built lazily from config.
        self._classifier = classifier
        self._scanner = scanner

    def _get_classifier(self) -> Classifier | None:
        if self._classifier is _UNSET:
            self._classifier = build_default_classifier()
        return self._classifier

    # ══════════════════════════════════════════════════════════════════════
    # Submissions
    # ══════════════════════════════════════════════════════════════════════
    async def open_submission(self, user, client_id: uuid.UUID | None) -> Submission:
        return await self.repo.create_submission(
            client_id=client_id, created_by=getattr(user, "id", None),
            status=SubmissionStatus.OPEN.value,
        )

    async def _load_submission(self, submission_id: uuid.UUID) -> Submission:
        sub = await self.repo.get_submission(submission_id)
        if sub is None:
            raise HTTPException(404, "Submission not found.")
        return sub

    # ══════════════════════════════════════════════════════════════════════
    # Upload + validate a slot
    # ══════════════════════════════════════════════════════════════════════
    async def upload_order_sheet(self, user, submission_id, data, filename) -> dict:
        return await self._upload_slot(user, submission_id, DocumentKind.ORDER_SHEET.value,
                                       data, filename)

    async def upload_spec_sheet(self, user, submission_id, data, filename) -> dict:
        return await self._upload_slot(user, submission_id, DocumentKind.SPEC_SHEET.value,
                                       data, filename)

    async def _upload_slot(self, user, submission_id, kind, data, filename) -> dict:
        sub = await self._load_submission(submission_id)
        # Lock-after-Stage-2: a consumed submission rejects further uploads (§5c).
        if sub.status == SubmissionStatus.CONSUMED.value:
            raise UploadError(
                RejectReason.SUBMISSION_LOCKED,
                "Submission already consumed by Stage 2; start a new submission.",
                payload=presenters.fingerprint(filename, data),
            )

        sha = sha256_of(data)

        # ── idempotency: byte-identical re-upload → cached result, no re-bill ─
        existing = await self.repo.get_document_by_sha(sha)
        if existing is not None:
            return await self._handle_existing(sub, kind, existing)

        # ── run the blocking pipeline off the event loop ─────────────────────
        templates = await self.repo.active_templates(kind)
        profiles = [_to_profile(t) for t in templates]
        classifier = self._get_classifier()
        try:
            result: PipelineResult = await run_in_threadpool(
                process_upload, data, filename, str(submission_id), kind, profiles,
                classifier=classifier, scanner=self._scanner,
            )
        except UploadError as exc:
            if exc.reason == RejectReason.VIRUS_DETECTED:
                await self._audit_virus(user, filename, data, sha, exc)
            raise presenters.enrich_gate_error(exc, sub, filename, data, kind, sha)

        # ── persist the document row (accepted OR rejected — caches by sha) ───
        doc = self._build_document(user, sub, kind, filename, sha, result)
        await self.repo.add_document(doc)

        if result.outcome.accepted:
            await self._accept_into_slot(sub, kind, doc)
            return presenters.success_envelope(sub, doc)

        # rejected / needs_manual_review → 4xx with diagnostics (§5b)
        raise UploadError(
            result.outcome.reason_code or RejectReason.NEEDS_MANUAL_REVIEW,
            f"Document failed validation: {result.outcome.status.value}",
            payload=presenters.rejection_envelope(sub, doc, result.outcome),
        )

    # ── idempotent re-hit on an already-seen sha256 ──────────────────────────
    async def _handle_existing(self, sub, kind, existing: Document) -> dict:
        if existing.validation_status == ValidationStatus.ACCEPTED.value:
            # Re-point the slot if this is the same submission (no new row, no LLM).
            if existing.submission_id == sub.id:
                await self._accept_into_slot(sub, kind, existing)
            return presenters.success_envelope(sub, existing)
        # A previously rejected / needs-review file → replay the cached diagnostics.
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
        if sub.status == SubmissionStatus.CONSUMED.value:
            return sub.status
        order_ok = self._slot_ok(sub.order_document_id, just_accepted,
                                 kind == DocumentKind.ORDER_SHEET.value)
        spec_ok = self._slot_ok(sub.spec_document_id, just_accepted,
                                kind == DocumentKind.SPEC_SHEET.value)
        return SubmissionStatus.COMPLETE.value if (order_ok and spec_ok) else SubmissionStatus.OPEN.value

    @staticmethod
    def _slot_ok(slot_id, just_accepted: Document, is_this_slot: bool) -> bool:
        # The just-accepted doc is guaranteed clean+accepted; the other slot must
        # already hold an accepted doc id. (Scan acceptability is enforced on accept.)
        if is_this_slot:
            return True
        return slot_id is not None

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
