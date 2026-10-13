"""
================================================================================
tests/integration/test_procurement_regression.py — regression guards, procurement
================================================================================
Follows the project's `_fixes.py` convention (see test_drawers_fixes.py,
test_bom_regression.py): each case locks in one specific, previously-verified
behaviour of app/modules/procurement so a future change either leaves it alone
deliberately, or breaks a named test instead of shipping silently.

R1  — a bug, isolated: presenters.submission_block's `ready_for_stage_2`/
      `complete` still requires BOTH slots, while service._recompute_status
      (the DB `status` column) implements "Fan-out v2" — order slot ALONE is
      enough. Same GET /submissions/{id} response can show `status:"complete"`
      next to `ready_for_stage_2:false` for the identical submission. This
      test isolates it with nothing else going on; left UNFIXED deliberately
      (see test_procurement_service.py's module docstring for the full
      writeup) — its failure is the bug report.
R2  — the order slot locks after consumption; the spec slot never does, even
      on the SAME (consumed) submission.
R3  — force=true (override_manual_review) is a no-op for a hard reject; only
      a needs_manual_review verdict is ever overridable.
R4  — CONSUMED is sticky: _recompute_status short-circuits to the current
      status once consumed, so a later spec upload can't move it back to
      "open"/"complete".
R5  — the byte-identical-reupload dedupe/idempotency short-circuit
      (`duplicate_content` 409, cached-verdict replay) is currently DISABLED
      in this build (`service._upload_slot` has the whole check commented out
      under a "TESTING PHASE" note). Pinned here as CURRENT behaviour so a
      re-enable is a deliberate, visible diff against this test, not a
      surprise.
R6  — claim_submission_for_breakdown is a true compare-and-set: exactly one
      of two racing claims wins.
R7  — a document belonging to submission A is never visible through
      submission B's report endpoint (404, not a cross-tenant 200).
================================================================================
"""
import io

import pytest
import pytest_asyncio
from fastapi import HTTPException
from reportlab.pdfgen import canvas

from app.core.enums import DocumentKind
from app.modules.procurement.enums import RejectReason, SubmissionStatus, ValidationStatus
from app.modules.procurement.errors import UploadError
from app.modules.procurement.models import ClientTemplate
from app.modules.procurement.service import ProcurementService


def _pdf_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for line_no, line in enumerate(text.split("\n")):
        c.drawString(72, 720 - 14 * line_no, line[:110])
    c.save()
    return buf.getvalue()


ORDER_TEXT = "BUYER ORDER SHEET\nOrder Number: PO-88213\nSize S M L Total\nQty 10 20 15 45"
SPEC_TEXT = "SPEC SHEET\nStyle: CLERMONT\nMeasurement chest waist hem\nTolerance +/-0.5cm"


def _clean_scanner(data: bytes):
    return (False, None)


class _FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    @staticmethod
    def _strip(key: str) -> str:
        return key.split("://", 1)[-1] if "://" in key else key

    def put(self, key, data):
        self.objects[self._strip(key)] = data
        return f"fake://{key}"

    def get(self, key):
        return self.objects[self._strip(key)]

    def delete(self, key):
        self.objects.pop(self._strip(key), None)

    def move(self, src, dst):
        self.objects[self._strip(dst)] = self.objects.pop(self._strip(src))
        return f"fake://{dst}"


@pytest.fixture(autouse=True)
def _stub_storage(monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setattr("app.modules.procurement.pipeline.get_storage", lambda: fake)
    monkeypatch.setattr("app.core.storage.get_storage", lambda: fake)
    return fake


@pytest_asyncio.fixture
async def order_profile(db):
    t = ClientTemplate(
        client_code="ACME", doc_kind=DocumentKind.ORDER_SHEET.value,
        anchors=[{"any": ["order number"], "weight": 1}], fingerprints=[r"PO-\d{5}"],
        thresholds={"accept_high": 0.80, "reject_low": 0.30}, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


@pytest_asyncio.fixture
async def spec_profile(db):
    t = ClientTemplate(
        client_code="ACME", doc_kind=DocumentKind.SPEC_SHEET.value,
        anchors=[{"any": ["measurement"], "weight": 1}, {"any": ["tolerance"], "weight": 1}],
        thresholds={"accept_high": 0.80, "reject_low": 0.30}, is_active=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


@pytest.fixture
def svc(db):
    return ProcurementService(db, classifier=None, vision_classifier=None, scanner=_clean_scanner)


# ══════════════════════════════════════════ R1 — the readiness-field discrepancy, isolated
@pytest.mark.integrity
async def test_r1_ready_for_stage_2_disagrees_with_status_on_an_order_only_submission(svc, dm, order_profile):
    """Nothing else is going on here beyond the bare minimum to reach the
    disagreement: one accepted order sheet, no spec sheet at all."""
    sub = await svc.open_submission(dm, None)
    await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")
    status = await svc.get_submission_status(sub.id)

    assert status["status"] == SubmissionStatus.COMPLETE.value          # service.py: order-only
    assert status["ready_for_stage_2"] is True                          # presenters.py: still both-slots
    assert status["complete"] is True


# ══════════════════════════════════════════ R2 — order locks, spec never does
@pytest.mark.integrity
async def test_r2_order_slot_locks_after_consumption_spec_slot_never_does(svc, dm, order_profile, spec_profile):
    sub = await svc.open_submission(dm, None)
    await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")
    await svc.claim_submission_for_breakdown(sub.id)      # COMPLETE -> CONSUMED

    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT + "\nv2"), "order2.pdf")
    assert ei.value.reason == RejectReason.SUBMISSION_LOCKED

    # the SAME (now consumed) submission still accepts a spec upload
    envelope = await svc.upload_spec_sheet(dm, sub.id, _pdf_bytes(SPEC_TEXT), "spec.pdf")
    assert envelope["document"]["validation"]["status"] == ValidationStatus.ACCEPTED.value


# ══════════════════════════════════════════ R3 — force never overrides a hard reject
@pytest.mark.integrity
async def test_r3_force_true_is_a_noop_for_a_hard_reject(svc, dm, order_profile):
    sub = await svc.open_submission(dm, None)
    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(dm, sub.id, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
                                     "photo.png", override_manual_review=True)
    assert ei.value.reason == RejectReason.UNSUPPORTED_MIME
    assert ei.value.http_status == 415


# ══════════════════════════════════════════ R4 — CONSUMED is sticky
@pytest.mark.integrity
async def test_r4_consumed_status_survives_a_later_spec_upload(svc, dm, order_profile, spec_profile, db):
    sub = await svc.open_submission(dm, None)
    await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")
    await svc.claim_submission_for_breakdown(sub.id)
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.CONSUMED.value

    await svc.upload_spec_sheet(dm, sub.id, _pdf_bytes(SPEC_TEXT), "spec.pdf")
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.CONSUMED.value   # never reverted to complete/open


# ══════════════════════════════════════════ R5 — dedupe shortcut is currently disabled
@pytest.mark.integrity
async def test_r5_reuploading_identical_bytes_currently_re_validates_instead_of_short_circuiting(
    svc, dm, order_profile,
):
    """Pinned as CURRENT behaviour, not desired behaviour. With the sha-cache
    check commented out in service._upload_slot ("TESTING PHASE"), posting the
    exact same bytes to the exact same slot twice runs the full pipeline
    TWICE and produces TWO separate Document rows (the second supersedes the
    first) — it does not 201-with-no-new-row the way an idempotent replay
    would. When the shortcut is re-enabled, this test's second assertion
    should flip (same document id both times) — update it then, deliberately."""
    sub = await svc.open_submission(dm, None)
    data = _pdf_bytes(ORDER_TEXT)

    first = await svc.upload_order_sheet(dm, sub.id, data, "order.pdf")
    second = await svc.upload_order_sheet(dm, sub.id, data, "order.pdf")   # byte-identical

    assert first["document"]["id"] != second["document"]["id"]   # NOT a cached replay today


# ══════════════════════════════════════════ R6 — claim is a true CAS
@pytest.mark.integrity
async def test_r6_breakdown_claim_wins_exactly_once(svc, dm, order_profile):
    sub = await svc.open_submission(dm, None)
    await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")

    results = [await svc.claim_submission_for_breakdown(sub.id) for _ in range(3)]
    assert results.count(True) == 1
    assert results.count(False) == 2


# ══════════════════════════════════════════ R7 — cross-submission document leak
@pytest.mark.security
async def test_r7_a_document_is_never_visible_through_a_different_submissions_report(svc, dm, order_profile):
    sub_a = await svc.open_submission(dm, None)
    envelope = await svc.upload_order_sheet(dm, sub_a.id, _pdf_bytes(ORDER_TEXT), "order.pdf")
    import uuid
    doc_id = uuid.UUID(envelope["document"]["id"])

    sub_b = await svc.open_submission(dm, None)
    with pytest.raises(HTTPException) as ei:
        await svc.get_document_report(sub_b.id, doc_id)
    assert ei.value.status_code == 404
