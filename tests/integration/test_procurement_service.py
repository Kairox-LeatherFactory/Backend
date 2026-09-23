"""
================================================================================
tests/integration/test_procurement_service.py — ProcurementService on the DB
================================================================================
Layer: integration (service -> repo -> DB on in-memory SQLite/aiosqlite, per
tests/conftest.py). Exercises ProcurementService directly against real
Submission/Document/ClientTemplate rows, with the classifier/scanner FAKED
(injected per the module's own documented testability contract) so every case
is deterministic — no real clamd, no real LLM key needed.

Covers every ProcurementService method:
  open_submission · upload_order_sheet/upload_spec_sheet (-> _upload_slot) ·
  get_submission_status · get_document_report · link_submission_to_order ·
  claim_submission_for_breakdown / release_breakdown_claim / is_breakdown_claimed ·
  get_accepted_order_bytes · get_submission_client_id · spec_documents_for_client

KNOWN LIVE DISCREPANCY — service.py's `_recompute_status` implements "Fan-out
v2" (its own comment): a submission reaches `complete`/ready for the style
breakdown once the ORDER slot alone is accepted — a spec sheet is no longer
required, since one order can now cover many styles each with its own spec
attached later. But `presenters.submission_block` (the function that actually
fills `ready_for_stage_2`/`complete` in the GET /submissions/{id} response)
was not updated for that change — its own docstring still says "true iff BOTH
slots are accepted", and its code computes exactly that. So the SAME response
can show `status:"complete"` (DB field, order-only rule) alongside
`ready_for_stage_2:false` (presenter field, still both-slots rule) for the
identical submission — self-contradictory, and worse, `claim_submission_for_
breakdown`'s CAS reads `sub.status` (the order-only field), so the backend
will actually let the breakdown proceed at exactly the moment the status
endpoint is telling any caller it is NOT ready. Reproduced below in
test_upload_order_sheet_alone_makes_the_submission_complete, which asserts
the INTENDED behaviour and is expected to currently fail on
`ready_for_stage_2` — that failure is the bug report.
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


# ── real, parseable file fixtures (same helpers as the unit layer) ──────────
def _pdf_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for line_no, line in enumerate(text.split("\n")):
        c.drawString(72, 720 - 14 * line_no, line[:110])
    c.save()
    return buf.getvalue()


ORDER_TEXT = "BUYER ORDER SHEET\nOrder Number: PO-88213\nSize S M L Total\nQty 10 20 15 45"
SPEC_TEXT = "SPEC SHEET\nStyle: CLERMONT\nMeasurement chest waist hem\nTolerance +/-0.5cm"
UNRELATED_TEXT = "Completely unrelated content that matches nothing at all whatsoever."


# ── fakes: always-clean scanner, and classifiers that resolve every escalation ──
def _clean_scanner(data: bytes):
    return (False, None)


def _accepting_classifier(feats, expected_kind, candidates):
    """Confidently accepts whatever it's asked to classify as."""
    return {"confidence": 0.95, "doc_kind": expected_kind,
            "is_order_sheet": expected_kind == DocumentKind.ORDER_SHEET.value,
            "is_spec_sheet": expected_kind == DocumentKind.SPEC_SHEET.value,
            "evidence": ["forced accept"], "client_guess": None}


@pytest_asyncio.fixture
async def order_profile(db):
    """An active client_template row so the heuristic has an anchor to score
    against — 'order number' is a strong, unambiguous anchor term."""
    t = ClientTemplate(
        client_code="ACME", doc_kind=DocumentKind.ORDER_SHEET.value,
        anchors=[{"any": ["order number"], "weight": 1}],
        fingerprints=[r"PO-\d{5}"],
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


class _FakeStorage:
    """In-memory stand-in for StorageBackend — no real disk I/O from this suite.
    ProcurementService has no storage injection point of its own (only
    classifier/vision_classifier/scanner are), so both real call sites — the
    pipeline's `storage or get_storage()` default and
    ProcurementService.get_accepted_order_bytes' own local import — are
    monkeypatched to this ONE shared instance by the autouse fixture below."""
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    @staticmethod
    def _strip(key: str) -> str:
        """Real backends accept either a bare key or a previously-returned
        storage_url (LocalStorageBackend._path does the same) — a document's
        `storage_url` column holds the URL form, not the bare key, so any
        caller reading it back must work either way."""
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


@pytest.fixture
def fake_storage(monkeypatch):
    fake = _FakeStorage()
    # pipeline.py did `from app.core.storage import get_storage` at ITS OWN
    # module load time, so patching app.core.storage.get_storage after the
    # fact would NOT reach it — the pipeline's own name has to be patched.
    monkeypatch.setattr("app.modules.procurement.pipeline.get_storage", lambda: fake)
    # get_accepted_order_bytes does a FRESH `from app.core.storage import
    # get_storage` inside its own function body on every call, so patching the
    # source module's attribute IS what that call site will pick up.
    monkeypatch.setattr("app.core.storage.get_storage", lambda: fake)
    return fake


@pytest.fixture
def svc(db, fake_storage):
    """A ProcurementService with a clean scanner and no LLM classifier wired —
    the heuristic (backed by order_profile/spec_profile) resolves everything."""
    return ProcurementService(db, classifier=None, vision_classifier=None,
                              scanner=_clean_scanner)


@pytest.fixture
def svc_llm(db):
    """Same, but with a classifier wired — for exercising the escalation path."""
    return ProcurementService(db, classifier=_accepting_classifier,
                              vision_classifier=None, scanner=_clean_scanner)


# ══════════════════════════════════════════════════ open_submission
async def test_open_submission_creates_an_open_row(svc, dm, db):
    sub = await svc.open_submission(dm, None)
    assert sub.status == SubmissionStatus.OPEN.value
    assert sub.client_id is None


async def test_open_submission_with_a_client(svc, dm, db):
    from app.modules.clients.models import Client
    client = Client(name="Acme Leathers", country="IT")
    db.add(client)
    await db.commit()
    await db.refresh(client)

    sub = await svc.open_submission(dm, client.id)
    assert sub.client_id == client.id


# ══════════════════════════════════════════════════ upload_order_sheet — happy path
async def test_upload_order_sheet_accepts_and_fills_the_slot(svc, dm, order_profile):
    sub = await svc.open_submission(dm, None)
    envelope = await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")
    assert envelope["document"]["validation"]["status"] == ValidationStatus.ACCEPTED.value
    assert envelope["submission"]["order_sheet"]["present"] is True


async def test_upload_order_sheet_alone_makes_the_submission_complete(svc, dm, order_profile):
    """Fan-out v2: the order slot ALONE gates readiness — no spec sheet required.

    THIS IS EXPECTED TO CURRENTLY FAIL on `ready_for_stage_2` — see the module
    docstring's "KNOWN LIVE DISCREPANCY" note. `status` (from
    service._recompute_status, order-only) correctly comes back "complete", but
    `ready_for_stage_2`/`complete` (from presenters.submission_block, which
    still literally requires BOTH slots present) comes back False in the SAME
    response — the two fields contradict each other. Left as the intended
    assertion, not weakened to match the bug, so the failure is the bug report."""
    sub = await svc.open_submission(dm, None)
    await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")
    status = await svc.get_submission_status(sub.id)
    assert status["status"] == SubmissionStatus.COMPLETE.value
    assert status["ready_for_stage_2"] is True
    assert status["spec_sheet"]["present"] is False


async def test_upload_spec_sheet_alone_does_not_gate_readiness(svc, dm, spec_profile):
    sub = await svc.open_submission(dm, None)
    await svc.upload_spec_sheet(dm, sub.id, _pdf_bytes(SPEC_TEXT), "spec.pdf")
    status = await svc.get_submission_status(sub.id)
    assert status["status"] == SubmissionStatus.OPEN.value
    assert status["ready_for_stage_2"] is False
    assert status["spec_sheet"]["present"] is True


async def test_upload_creates_a_submission_when_none_is_given(svc, dm, order_profile):
    """The direct-upload path (submission_id=None): a brand-new OPEN submission is
    minted before the pipeline runs."""
    envelope = await svc.upload_order_sheet(dm, None, _pdf_bytes(ORDER_TEXT), "order.pdf")
    assert envelope["submission_id"]
    status = await svc.get_submission_status(envelope["submission_id"])
    assert status["status"] == SubmissionStatus.COMPLETE.value


# ══════════════════════════════════════════════════ upload — rejection paths
async def test_upload_order_sheet_rejects_an_unrelated_document(svc, dm, order_profile):
    sub = await svc.open_submission(dm, None)
    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(UNRELATED_TEXT), "order.pdf")
    assert ei.value.reason == RejectReason.NOT_AN_ORDER_SHEET
    assert ei.value.http_status == 422


async def test_upload_order_sheet_rejects_an_unsupported_file_type(svc, dm, order_profile):
    sub = await svc.open_submission(dm, None)
    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(dm, sub.id, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "photo.png")
    assert ei.value.reason == RejectReason.UNSUPPORTED_MIME
    assert ei.value.http_status == 415


async def test_upload_order_sheet_rejects_an_empty_file(svc, dm, order_profile):
    sub = await svc.open_submission(dm, None)
    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(dm, sub.id, b"", "order.pdf")
    assert ei.value.reason == RejectReason.EMPTY_OR_CORRUPT


async def test_upload_wrong_document_to_the_wrong_slot(svc, dm, order_profile, spec_profile):
    """A real spec sheet posted to the ORDER slot — the heuristic rejects it as
    not-an-order-sheet (wrong_slot needs the LLM rung to name the OTHER kind
    explicitly; the pure-heuristic path still correctly refuses it either way)."""
    sub = await svc.open_submission(dm, None)
    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(SPEC_TEXT), "spec_as_order.pdf")
    assert ei.value.reason in (RejectReason.NOT_AN_ORDER_SHEET, RejectReason.WRONG_SLOT)


async def test_upload_needs_manual_review_carries_the_override_hint(svc, dm):
    """No client_template at all -> only the generic (strict) profile scores it;
    a document with SOME plausible signal but nothing decisive should reach
    needs_manual_review with can_override surfaced, not a hard reject."""
    sub = await svc.open_submission(dm, None)
    # A borderline doc: has *a* real text layer but nothing a strict generic
    # profile confidently recognises -> mid/low heuristic band, classifier=None
    # on `svc` -> needs_manual_review (never a silent guess).
    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(UNRELATED_TEXT), "ambiguous.pdf")
    assert ei.value.reason in (RejectReason.NOT_AN_ORDER_SHEET, RejectReason.NEEDS_MANUAL_REVIEW)


# ══════════════════════════════════════════════════ force / manual-review override
async def test_force_accept_overrides_a_needs_manual_review_verdict(dm, db, fake_storage):
    """Force a genuinely ambiguous doc through with override_manual_review=True.

    Deliberately does NOT request the `order_profile` fixture: its fingerprint
    (`PO-\\d{5}`) matches ORDER_TEXT and would win best_match outright (an
    instant accept), masking the escalate-band setup this case needs. The
    lone ACME2 template below is the only order_sheet profile active here."""
    # A profile with a very high accept bar and a low reject bar so a partial
    # anchor hit lands in the escalate band; classifier=None -> needs_manual_review.
    t = ClientTemplate(
        client_code="ACME2", doc_kind=DocumentKind.ORDER_SHEET.value,
        anchors=[{"any": ["order number"], "weight": 1},
                 {"any": ["something that will never appear"], "weight": 1}],
        thresholds={"accept_high": 0.95, "reject_low": 0.05}, is_active=True,
    )
    db.add(t)
    await db.commit()

    svc_no_llm = ProcurementService(db, classifier=None, vision_classifier=None,
                                    scanner=_clean_scanner)
    sub = await svc_no_llm.open_submission(dm, None)
    data = _pdf_bytes(ORDER_TEXT)

    with pytest.raises(UploadError) as ei:
        await svc_no_llm.upload_order_sheet(dm, sub.id, data, "order.pdf")
    assert ei.value.reason == RejectReason.NEEDS_MANUAL_REVIEW

    envelope = await svc_no_llm.upload_order_sheet(
        dm, sub.id, data, "order.pdf", override_manual_review=True)
    assert envelope["document"]["validation"]["status"] == ValidationStatus.ACCEPTED.value


async def test_force_accept_never_overrides_a_hard_reject(svc, dm, order_profile):
    """override_manual_review=True must be a no-op for a HARD reject (wrong file
    type) — only needs_manual_review is ever overridable."""
    sub = await svc.open_submission(dm, None)
    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(dm, sub.id, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
                                     "photo.png", override_manual_review=True)
    assert ei.value.reason == RejectReason.UNSUPPORTED_MIME


# ══════════════════════════════════════════════════ supersede on re-upload
async def test_reupload_supersedes_the_prior_accepted_document(svc, dm, order_profile, db):
    sub = await svc.open_submission(dm, None)
    first = await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order_v1.pdf")
    first_doc_id = first["document"]["id"]

    corrected = ORDER_TEXT + "\nRevision: 2"
    second = await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(corrected), "order_v2.pdf")
    assert second["document"]["id"] != first_doc_id

    import uuid
    report = await svc.get_document_report(sub.id, uuid.UUID(first_doc_id))
    assert report["document"]["validation"]["status"] == ValidationStatus.SUPERSEDED.value


# ══════════════════════════════════════════════════ submission_locked (order slot only)
async def test_order_slot_locks_once_consumed(svc, dm, order_profile, db):
    sub = await svc.open_submission(dm, None)
    await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")

    # simulate the breakdown claiming the submission (CONSUMED)
    claimed = await svc.claim_submission_for_breakdown(sub.id)
    assert claimed is True

    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT + "\nv2"), "order2.pdf")
    assert ei.value.reason == RejectReason.SUBMISSION_LOCKED
    assert ei.value.http_status == 409


async def test_spec_slot_never_locks_even_after_consumption(svc, dm, order_profile, spec_profile):
    sub = await svc.open_submission(dm, None)
    await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")
    await svc.claim_submission_for_breakdown(sub.id)

    envelope = await svc.upload_spec_sheet(dm, sub.id, _pdf_bytes(SPEC_TEXT), "spec.pdf")
    assert envelope["document"]["validation"]["status"] == ValidationStatus.ACCEPTED.value


# ══════════════════════════════════════════════════ get_submission_status
async def test_status_of_a_fresh_submission_lists_both_slots_missing(svc, dm):
    sub = await svc.open_submission(dm, None)
    status = await svc.get_submission_status(sub.id)
    assert status["order_sheet"]["present"] is False
    assert status["spec_sheet"]["present"] is False
    assert "order_sheet missing" in status["blocking"]
    assert "spec_sheet missing" in status["blocking"]


async def test_status_of_an_unknown_submission_404s(svc):
    import uuid
    with pytest.raises(HTTPException) as ei:
        await svc.get_submission_status(uuid.uuid4())
    assert ei.value.status_code == 404


# ══════════════════════════════════════════════════ get_document_report
async def test_document_report_shows_rejection_diagnostics(svc, dm, order_profile):
    sub = await svc.open_submission(dm, None)
    with pytest.raises(UploadError):
        await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(UNRELATED_TEXT), "bad.pdf")

    # A rejected upload still persists its Document row (caches by sha) — fetch
    # it via the repo, since the raised UploadError doesn't carry the row's id.
    from sqlalchemy import select
    from app.core.models import Document
    res = await svc.db.execute(select(Document).where(Document.submission_id == sub.id))
    doc = res.scalars().one()

    report = await svc.get_document_report(sub.id, doc.id)
    assert report["document"]["validation"]["status"] == ValidationStatus.REJECTED.value
    assert report["document"]["validation"]["reason_code"] == RejectReason.NOT_AN_ORDER_SHEET.value
    assert report["document"]["validation"]["suggested_fix"]


async def test_document_report_404s_across_submissions(svc, dm, order_profile):
    """A document that genuinely exists, requested through a DIFFERENT submission's
    report endpoint, must 404 — never leak cross-submission."""
    sub_a = await svc.open_submission(dm, None)
    envelope = await svc.upload_order_sheet(dm, sub_a.id, _pdf_bytes(ORDER_TEXT), "order.pdf")
    import uuid
    doc_id = uuid.UUID(envelope["document"]["id"])

    sub_b = await svc.open_submission(dm, None)
    with pytest.raises(HTTPException) as ei:
        await svc.get_document_report(sub_b.id, doc_id)
    assert ei.value.status_code == 404


async def test_document_report_unknown_document_404s(svc, dm):
    import uuid
    sub = await svc.open_submission(dm, None)
    with pytest.raises(HTTPException) as ei:
        await svc.get_document_report(sub.id, uuid.uuid4())
    assert ei.value.status_code == 404


# ══════════════════════════════════════════════════ breakdown claim CAS
async def test_claim_submission_for_breakdown_requires_complete_status(svc, dm):
    sub = await svc.open_submission(dm, None)   # still OPEN, order never uploaded
    claimed = await svc.claim_submission_for_breakdown(sub.id)
    assert claimed is False


async def test_claim_submission_for_breakdown_succeeds_exactly_once(svc, dm, order_profile, db):
    sub = await svc.open_submission(dm, None)
    await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")

    first = await svc.claim_submission_for_breakdown(sub.id)
    second = await svc.claim_submission_for_breakdown(sub.id)   # simulates a racing second POST
    assert first is True
    assert second is False
    assert await svc.is_breakdown_claimed(sub.id) is True


async def test_release_breakdown_claim_hands_it_back(svc, dm, order_profile):
    sub = await svc.open_submission(dm, None)
    await svc.upload_order_sheet(dm, sub.id, _pdf_bytes(ORDER_TEXT), "order.pdf")
    await svc.claim_submission_for_breakdown(sub.id)

    await svc.release_breakdown_claim(sub.id)
    assert await svc.is_breakdown_claimed(sub.id) is False
    # and it can be re-claimed after release
    assert await svc.claim_submission_for_breakdown(sub.id) is True


# ══════════════════════════════════════════════════ the bom.service <-> procurement.service edge
async def test_link_submission_to_order_sets_the_client_order_id(svc, dm):
    import uuid
    sub = await svc.open_submission(dm, None)
    order_id = uuid.uuid4()
    await svc.link_submission_to_order(sub.id, order_id)

    refreshed = await svc.repo.get_submission(sub.id)
    assert refreshed.client_order_id == order_id


async def test_link_submission_to_order_on_a_missing_submission_is_a_silent_noop(svc):
    import uuid
    await svc.link_submission_to_order(uuid.uuid4(), uuid.uuid4())   # must not raise


async def test_get_accepted_order_bytes_round_trips_through_storage(svc, dm, order_profile, fake_storage):
    sub = await svc.open_submission(dm, None)
    data = _pdf_bytes(ORDER_TEXT)
    await svc.upload_order_sheet(dm, sub.id, data, "order.pdf")

    got = await svc.get_accepted_order_bytes(sub.id)
    assert got is not None
    got_bytes, filename, _mime = got
    assert got_bytes == data
    assert filename == "order.pdf"


async def test_get_accepted_order_bytes_none_when_slot_empty(svc, dm):
    sub = await svc.open_submission(dm, None)
    assert await svc.get_accepted_order_bytes(sub.id) is None


async def test_get_submission_client_id(svc, dm, db):
    from app.modules.clients.models import Client
    client = Client(name="Acme", country="IT")
    db.add(client)
    await db.commit()
    await db.refresh(client)

    sub = await svc.open_submission(dm, client.id)
    assert await svc.get_submission_client_id(sub.id) == client.id
    import uuid
    assert await svc.get_submission_client_id(uuid.uuid4()) is None


async def test_spec_documents_for_client_returns_only_accepted_spec_sheets(svc, dm, spec_profile, db):
    from app.modules.clients.models import Client
    client = Client(name="Acme", country="IT")
    db.add(client)
    await db.commit()
    await db.refresh(client)

    sub = await svc.open_submission(dm, client.id)
    await svc.upload_spec_sheet(dm, sub.id, _pdf_bytes(SPEC_TEXT), "spec.pdf")

    docs = await svc.spec_documents_for_client(client.id)
    assert len(docs) == 1
    assert docs[0].kind == DocumentKind.SPEC_SHEET.value
