"""
================================================================================
modules/procurement/enums.py — Stage-1 intake value-sets
================================================================================

The upload/validation front-door vocabulary only. Cross-cutting value-sets
(DocumentKind, SpecType, NotificationChannel/Type/Status) moved to app/core/enums.py
when the monolith was split, so a later-stage module never imports procurement just
to name a document kind. BOM / inventory / supplier-PO enums live in their own modules.

str-Enum + VARCHAR columns (the module convention) — never native PG ENUM.

ENUM GUIDE (each str-Enum; the model stores `.value` in a VARCHAR column)
  SubmissionStatus      open → complete → consumed/rejected (the upload-batch lifecycle).
  ValidationStatus      per-document verdict (pending/accepted/rejected/superseded/needs_manual_review).
  ScanStatus            clean/skipped/infected/error (clean+skipped pass the gate).
  ClassificationMethod  heuristic/llm/manual — which gate decided (the LLM-policy audit trail).
  ExpectedLayout        registry hint (scanned_pdf → skip text-layer, escalate straight to LLM).
  SizeSystem            letter/eu/mixed (client.default_size_system).
  RejectReason          the stable reason_code catalog → fixed HTTP statuses (see errors.py).
================================================================================
"""
import enum


class SubmissionStatus(str, enum.Enum):
    """Lifecycle of an upload-batch (the pairing key). OPEN until both slots are
    accepted+clean → COMPLETE → CONSUMED once Stage 2 picks it up. REJECTED is a
    terminal manual close."""
    OPEN = "open"
    COMPLETE = "complete"
    CONSUMED = "consumed"
    REJECTED = "rejected"


class ValidationStatus(str, enum.Enum):
    """Per-document identity-validation outcome (Document.validation_status)."""
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"


class ScanStatus(str, enum.Enum):
    """Virus-scan outcome (Document.scan_status). `skipped` = scanning disabled in dev;
    the completeness gate treats clean and skipped as acceptable."""
    CLEAN = "clean"
    SKIPPED = "skipped"
    INFECTED = "infected"
    ERROR = "error"


class ClassificationMethod(str, enum.Enum):
    """Which gate produced the classification — the audit trail for the LLM policy."""
    HEURISTIC = "heuristic"
    LLM = "llm"
    MANUAL = "manual"


class ExpectedLayout(str, enum.Enum):
    """A registry hint about a client doc's physical form. `scanned_pdf` tells the
    validator to skip the futile text-layer pass and escalate straight to the LLM."""
    SCANNED_PDF = "scanned_pdf"
    DIGITAL_PDF = "digital_pdf"
    SPREADSHEET = "spreadsheet"


class SizeSystem(str, enum.Enum):
    """Stored on `client.default_size_system`; orders mix systems per row regardless."""
    LETTER = "letter"
    EU = "eu"
    MIXED = "mixed"


class RejectReason(str, enum.Enum):
    """The stable reason_code catalog returned on rejection (§5b). Each maps to a fixed
    HTTP status in the router."""
    UNSUPPORTED_MIME = "unsupported_mime"          # 415
    FILE_TOO_LARGE = "file_too_large"              # 413
    EMPTY_OR_CORRUPT = "empty_or_corrupt"          # 422
    VIRUS_DETECTED = "virus_detected"              # 422
    SCANNER_UNAVAILABLE = "scanner_unavailable"    # 503
    NOT_AN_ORDER_SHEET = "not_an_order_sheet"      # 422
    NOT_A_SPEC_SHEET = "not_a_spec_sheet"          # 422
    WRONG_SLOT = "wrong_slot"                      # 422
    NEEDS_MANUAL_REVIEW = "needs_manual_review"    # 422
    SUBMISSION_LOCKED = "submission_locked"        # 409
    DUPLICATE_CONTENT = "duplicate_content"        # 409 — byte-identical file already
    #   on record elsewhere (other submission, or the other slot of this one). The sha
    #   cache is a true short-circuit ONLY for the same slot of the same submission;
    #   `Document.sha256` is globally unique so the bytes can't be re-stored here.
