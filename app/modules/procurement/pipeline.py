"""
================================================================================
modules/procurement/pipeline.py — The sync upload pipeline (runs in threadpool)
================================================================================

All the CPU/IO-blocking Stage-1 work in ONE synchronous function so the router
can push it into run_in_threadpool and never block the async event loop (the same
posture the imports handler uses for openpyxl). It touches NO database session —
the async service persists the result.

ORDER OF OPERATIONS (§6 quarantine→promote, §7 scan-then-validate)
    1. scan the raw bytes (virus). infected → UploadError(virus_detected); scanner
       unreachable while required → UploadError(scanner_unavailable). clean/skipped
       writes the bytes to quarantine/.
    2. sniff the TRUE mime + extract features. unsupported/empty → UploadError.
    3. validate identity (heuristic → LLM hybrid).
    4. accepted → PROMOTE quarantine/ → submissions/ and return its storage_url;
       rejected/needs-review → delete the quarantined object (never referenced by
       an accepted document) and return storage_url=None.

FUNCTION GUIDE  (pure-ish + sync; ProcurementService threadpools process_upload)
  PipelineResult   the bundle returned to the service: sha + feats + outcome + scan + storage.
  sha256_of(data) -> str   the dedupe/cache key (also called directly by the service).
  process_upload(data, filename, submission_id, expected_kind, profiles, *, classifier?,
                 scanner?, storage?) -> PipelineResult
      THE ORCHESTRATOR: scan (fail-closed) → sniff+extract → quarantine-store → validate →
      promote on accept / delete on reject. Raises UploadError on a hard gate (virus,
      scanner down, unsupported mime, empty). Touches NO DB — the service persists the result.
================================================================================
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.modules.procurement.classifier import Classifier, VisionClassifier
from app.modules.procurement.enums import RejectReason, ScanStatus
from app.modules.procurement.errors import UploadError
from app.modules.procurement.registry import ProfileView
from app.modules.procurement.scanning import (
    Scanner,
    ScannerUnavailable,
    scan_bytes,
)
from app.modules.procurement.sniffing import (
    DocFeatures,
    EmptyOrCorrupt,
    UnsupportedMime,
    sniff_and_extract,
)
from app.core.storage import (
    StorageBackend,
    get_storage,
    quarantine_key,
    submission_key,
)
from app.modules.procurement.validator import ValidationOutcome, validate_document

# slot → object-storage subdir under submissions/<id>/
_SLOT_DIR = {"order_sheet": "order-sheet", "spec_sheet": "spec-sheet"}


@dataclass
class PipelineResult:
    sha256: str
    feats: DocFeatures
    outcome: ValidationOutcome
    scan_status: ScanStatus
    scan_signature: str | None
    storage_url: str | None     # set ONLY when accepted+promoted
    storage_key: str | None


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def process_upload(
    data: bytes,
    filename: str,
    submission_id: str,
    expected_kind: str,
    profiles: list[ProfileView],
    *,
    classifier: Classifier | None = None,
    vision_classifier: VisionClassifier | None = None,
    scanner: Scanner | None = None,
    storage: StorageBackend | None = None,
) -> PipelineResult:
    """Run scan → sniff → validate → store. Raises UploadError on a hard gate."""
    storage = storage or get_storage()
    sha = sha256_of(data)

    # ── 1. virus scan (fail-closed when required) ────────────────────────────
    try:
        scan_status, signature = scan_bytes(data, scanner=scanner)
    except ScannerUnavailable as exc:
        raise UploadError(
            RejectReason.SCANNER_UNAVAILABLE,
            f"Virus scanner unavailable; refusing to store unscanned file. {exc}",
        ) from exc
    if scan_status == ScanStatus.INFECTED:
        # Nothing was stored; record the hit for audit upstream and reject.
        raise UploadError(
            RejectReason.VIRUS_DETECTED,
            f"Malware detected ({signature}); upload rejected.",
            payload={"scan_signature": signature, "sha256": sha},
        )

    # ── 2. sniff true MIME + features (needs the extension for the key) ──────
    try:
        feats = sniff_and_extract(data, filename)
    except UnsupportedMime as exc:
        raise UploadError(RejectReason.UNSUPPORTED_MIME, str(exc),
                          payload={"sha256": sha}) from exc
    except EmptyOrCorrupt as exc:
        raise UploadError(RejectReason.EMPTY_OR_CORRUPT, str(exc),
                          payload={"sha256": sha}) from exc

    # Write to quarantine first; promote only after a clean validation. for temp file
    qkey = quarantine_key(submission_id, _SLOT_DIR.get(expected_kind, expected_kind),
                          sha, feats.ext)
    storage.put(qkey, data)

    # ── 3. identity validation (heuristic → text LLM → vision LLM) ───────────
    outcome = validate_document(
        feats, expected_kind, profiles,
        classifier=classifier, vision_classifier=vision_classifier,
        data=data, filename=filename,
    )

    # ── 4. promote on accept, else delete the quarantined object ─────────────
    storage_url = storage_key = None
    if outcome.accepted:
        skey = submission_key(submission_id, _SLOT_DIR.get(expected_kind, expected_kind),
                              sha, feats.ext)
        storage_url = storage.move(qkey, skey)
        storage_key = skey
    else:
        storage.delete(qkey)

    return PipelineResult(
        sha256=sha, feats=feats, outcome=outcome,
        scan_status=scan_status, scan_signature=signature,
        storage_url=storage_url, storage_key=storage_key,
    )
