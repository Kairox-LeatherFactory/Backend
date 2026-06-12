"""
================================================================================
modules/procurement/errors.py — Upload rejection → stable HTTP status (§5b)
================================================================================

Every Stage-1 rejection carries a `reason_code` from the catalog and maps to a
STABLE HTTP status (415 unsupported MIME, 413 too large, 422 identity failures,
409 locked/slot conflicts, 503 scanner unavailable when fail-closed). The router
catches `UploadError` and renders the diagnostics envelope with the right status —
never a bare boolean.

FUNCTION GUIDE
  STATUS_FOR_REASON   the reason_code → HTTP status map (415/413/422/409/503).
  UploadError(reason, message, payload?)   the one exception every Stage-1 gate raises;
      `.http_status` derives the status from the reason. RAISED IN: pipeline / sniffing /
      scanning / validator / service; CAUGHT IN: procurement/router (_error_response).
================================================================================
"""
from __future__ import annotations

from app.modules.procurement.enums import RejectReason

# reason_code → HTTP status (the §5b mapping).
STATUS_FOR_REASON: dict[str, int] = {
    RejectReason.UNSUPPORTED_MIME.value: 415,
    RejectReason.FILE_TOO_LARGE.value: 413,
    RejectReason.EMPTY_OR_CORRUPT.value: 422,
    RejectReason.VIRUS_DETECTED.value: 422,
    RejectReason.SCANNER_UNAVAILABLE.value: 503,
    RejectReason.NOT_AN_ORDER_SHEET.value: 422,
    RejectReason.NOT_A_SPEC_SHEET.value: 422,
    RejectReason.WRONG_SLOT.value: 422,
    RejectReason.NEEDS_MANUAL_REVIEW.value: 422,
    RejectReason.SUBMISSION_LOCKED.value: 409,
}


class UploadError(Exception):
    """A Stage-1 upload was rejected. `payload` is the full diagnostics envelope
    (§5b) the router returns as JSON with `http_status`."""

    def __init__(self, reason: RejectReason, message: str, payload: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.payload = payload or {}

    @property
    def http_status(self) -> int:
        return STATUS_FOR_REASON.get(self.reason.value, 422)
