"""
================================================================================
modules/procurement/presenters.py — Stage-1 response envelopes (§5a / §5b)
================================================================================

PURE serialization: ORM rows + a ValidationOutcome → the exact JSON envelopes the
API returns. Split out of service.py so the service stays focused on orchestration
(load → pipeline → persist → gate) and these shape-only helpers live in one place.

No DB session, no business rules, no I/O. Every function takes already-loaded
`Document` / `Submission` rows (duck-typed — no `models` import needed) and returns
a plain dict. The service calls these; nothing here calls back into the service.
================================================================================
"""
from __future__ import annotations

from app.core.enums import DocumentKind
from app.modules.procurement.enums import ScanStatus, ValidationStatus
from app.modules.procurement.errors import UploadError
from app.modules.procurement.pipeline import sha256_of

# A submission slot is "good for Stage 2" when its scan is clean or skipped (§2/§7).
ACCEPTABLE_SCAN = {ScanStatus.CLEAN.value, ScanStatus.SKIPPED.value}


def signals_blob(outcome) -> dict:
    """The matched/expected/found signal bundle persisted on Document.validation_signals
    (and replayed for a cached re-upload)."""
    return {
        "matched": outcome.signals_matched,
        "expected": outcome.signals_expected,
        "found": outcome.signals_found,
        "evidence": outcome.evidence,
        "reason_code": outcome.reason_code.value if outcome.reason_code else None,
        "suggested_fix": outcome.suggested_fix,
        "closest_profile": outcome.closest_profile,
        "spec_type": outcome.spec_type,
        "client_match": outcome.client_match,
        "method": outcome.method.value,
    }


def document_block(doc) -> dict:
    """The per-document envelope (§5a `document`), also returned by GET …/documents/{id}."""
    sig = doc.validation_signals or {}
    return {
        "id": str(doc.id),
        "kind": doc.classified_kind or doc.kind,
        "filename": doc.filename,
        "mime": doc.mime,
        "sha256": doc.sha256,
        "size_bytes": doc.size_bytes,
        "page_count": doc.page_count,
        "storage_url": doc.storage_url,
        "validation": {
            "status": doc.validation_status,
            "classified_as": doc.classified_kind,
            "spec_type": doc.classified_spec_type,
            "client_match": doc.client_match_code,
            "confidence": float(doc.classification_confidence) if doc.classification_confidence is not None else None,
            "method": doc.classification_method,
            "signals_matched": sig.get("matched", []),
            "signals_expected": sig.get("expected", []),
            "signals_found": sig.get("found", []),
            "reason_code": sig.get("reason_code"),
            "suggested_fix": sig.get("suggested_fix"),
        },
        "scan_status": doc.scan_status,
    }


def submission_block(sub, order_doc, spec_doc) -> dict:
    """The submission status + Stage-2 readiness gate (§2). `ready_for_stage_2` is
    true iff both slots are accepted AND neither has a blocking scan status."""
    def slot(doc):
        present = doc is not None and doc.validation_status == ValidationStatus.ACCEPTED.value
        return {"present": present,
                "validation_status": doc.validation_status if doc else None}

    order_slot = slot(order_doc)
    spec_slot = slot(spec_doc)
    blocking: list[str] = []
    if not order_slot["present"]:
        blocking.append("order_sheet missing" if order_doc is None
                        else f"order_sheet {order_doc.validation_status}")
    if not spec_slot["present"]:
        blocking.append("spec_sheet missing" if spec_doc is None
                        else f"spec_sheet {spec_doc.validation_status}")
    for doc, name in ((order_doc, "order_sheet"), (spec_doc, "spec_sheet")):
        if doc is not None and doc.scan_status not in ACCEPTABLE_SCAN:
            blocking.append(f"{name} scan_status={doc.scan_status}")
    complete = order_slot["present"] and spec_slot["present"]
    ready = complete and not any(b.startswith(("order_sheet scan", "spec_sheet scan")) for b in blocking)
    return {
        "order_sheet": order_slot,
        "spec_sheet": spec_slot,
        "complete": complete,
        "ready_for_stage_2": ready,
        "blocking": blocking,
    }


def success_envelope(sub, doc) -> dict:
    """The 201/200 success body (§5a): the document + the current submission summary."""
    order_doc = doc if doc.kind == DocumentKind.ORDER_SHEET.value else None
    spec_doc = doc if doc.kind == DocumentKind.SPEC_SHEET.value else None
    order_id = sub.order_document_id
    spec_id = sub.spec_document_id
    order_present = order_id is not None
    spec_present = spec_id is not None
    order_status = doc.validation_status if (order_doc and order_id == doc.id) else (
        ValidationStatus.ACCEPTED.value if order_present else None)
    spec_status = doc.validation_status if (spec_doc and spec_id == doc.id) else (
        ValidationStatus.ACCEPTED.value if spec_present else None)
    blocking = []
    if not order_present:
        blocking.append("order_sheet missing")
    if not spec_present:
        blocking.append("spec_sheet missing")
    complete = order_present and spec_present
    return {
        "submission_id": str(sub.id),
        "document": document_block(doc),
        "submission": {
            "order_sheet": {"present": order_present, "validation_status": order_status},
            "spec_sheet": {"present": spec_present, "validation_status": spec_status},
            "complete": complete,
            "ready_for_stage_2": complete,
            "blocking": blocking,
        },
    }


def fingerprint(filename: str, data: bytes) -> dict:
    """The minimal document fingerprint used in pre-validation gate errors."""
    return {"document_fingerprint": {
        "filename": filename, "sha256": sha256_of(data), "size_bytes": len(data)}}


def rejection_envelope(sub, doc, outcome) -> dict:
    """The 422 diagnostics envelope (§5b) built from a fresh validation outcome."""
    return {
        "error": "document_validation_failed",
        "submission_id": str(sub.id),
        "document_fingerprint": {
            "filename": doc.filename, "mime": doc.mime,
            "sha256": doc.sha256, "size_bytes": doc.size_bytes,
        },
        "validation": {
            "status": outcome.status.value,
            "reason_code": outcome.reason_code.value if outcome.reason_code else None,
            "expected_kind": doc.kind,
            "confidence": outcome.confidence,
            "signals_expected": outcome.signals_expected,
            "signals_found": outcome.signals_found,
            "closest_client_profile": outcome.closest_profile,
            "method": outcome.method.value,
            "suggested_fix": outcome.suggested_fix,
        },
    }


def rejection_envelope_from_row(sub, doc) -> dict:
    """The §5b envelope replayed from a cached Document row (idempotent re-upload of
    a previously rejected file — no re-validation, no second LLM bill)."""
    sig = doc.validation_signals or {}
    return {
        "error": "document_validation_failed",
        "submission_id": str(sub.id),
        "document_fingerprint": {
            "filename": doc.filename, "mime": doc.mime,
            "sha256": doc.sha256, "size_bytes": doc.size_bytes,
        },
        "validation": {
            "status": doc.validation_status,
            "reason_code": sig.get("reason_code"),
            "expected_kind": doc.kind,
            "confidence": float(doc.classification_confidence) if doc.classification_confidence is not None else None,
            "signals_expected": sig.get("expected", []),
            "signals_found": sig.get("found", []),
            "closest_client_profile": sig.get("closest_profile"),
            "method": doc.classification_method,
            "suggested_fix": sig.get("suggested_fix"),
        },
    }


def enrich_gate_error(exc: UploadError, sub, filename, data, kind, sha) -> UploadError:
    """Attach the §5b fingerprint envelope to a hard-gate UploadError (MIME / size /
    virus / scanner) so even pre-validation rejections are diagnosable."""
    payload = {
        "error": "document_validation_failed",
        "submission_id": str(sub.id),
        "document_fingerprint": {
            "filename": filename, "sha256": sha, "size_bytes": len(data),
        },
        "validation": {
            "status": "rejected",
            "reason_code": exc.reason.value,
            "expected_kind": kind,
            "suggested_fix": exc.message,
        },
    }
    payload["validation"].update(
        {k: v for k, v in exc.payload.items() if k not in payload})
    exc.payload = payload
    return exc
