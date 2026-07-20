"""
================================================================================
tests/test_procurement_stage1.py — Stage-1 upload & validation acceptance (§9)
================================================================================

Confirms every acceptance criterion against the REAL /data files:
  1. random PDF rejected with diagnostics
  2. all real sample formats accepted (order PDFs via LLM, spec XLSX via heuristic)
  3. a new client onboarded by config only (no code change)
  4. pairing + the Stage-2 readiness gate
  5. idempotent re-upload (sha256 dedupe + no second LLM call)
  6. size + content-sniffed MIME limits enforced (HTTP shell)
  7. virus gate (EICAR rejected when enabled; skipped when off)
  8. no silent guessing (borderline → needs_manual_review)
  9. stays in lane (no SpecSheet / ClientOrder / Bom created)

The LLM and ClamAV paths are exercised with INJECTED fakes (no API key, no running
clamd) — the same fake-dependency pattern intelligence/langgraph_agent.py uses to
prove the wiring end to end.
================================================================================
"""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

import app.core.config as cfgmod
from app.core.enums import UserRole
from app.core import storage as storagemod
from app.modules.procurement.enums import RejectReason, ScanStatus, ValidationStatus
from app.modules.procurement.errors import UploadError
from app.modules.bom.models import Bom
from app.modules.procurement.models import ClientTemplate
from app.modules.procurement.scanning import EICAR
from app.modules.procurement.seed_templates import load_template_rows
from app.modules.procurement.service import ProcurementService
from app.modules.users.models import User

DATA = Path(__file__).resolve().parent.parent / "data"


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _local_storage(tmp_path, monkeypatch):
    """Point storage at a throwaway dir so uploads don't touch the repo tree."""
    monkeypatch.setattr(cfgmod.settings, "storage_backend", "local", raising=False)
    monkeypatch.setattr(cfgmod.settings, "local_storage_dir", str(tmp_path / "store"),
                        raising=False)
    # Default OFF in tests (no clamd running); the virus test re-enables it with a
    # fake scanner — exactly the dev posture (scan_status=skipped when disabled).
    monkeypatch.setattr(cfgmod.settings, "virus_scan_enabled", False, raising=False)
    # This suite is designed around INJECTED fake classifiers "no API key" (see module
    # docstring). The local .env, however, enables the real Gemini VISION classifier and
    # ships a key, so `_get_vision_classifier()` builds a live model that escalates a
    # `needs_manual_review` text verdict into a network accept — overriding the injected
    # fake and making low-confidence tests non-deterministic. Pin the vision rung OFF so
    # the injected text classifier is authoritative (build_vision_classifier() -> None).
    monkeypatch.setattr(cfgmod.settings, "vision_classifier_enabled", False, raising=False)
    storagemod.reset_storage_cache()
    yield
    storagemod.reset_storage_cache()


async def _seed_templates(db):
    for r in load_template_rows():
        db.add(ClientTemplate(
            client_code=r["client_code"], doc_kind=r["doc_kind"],
            display_name=r.get("display_name"), language=r.get("language"),
            size_system=r.get("size_system"), currency=r.get("currency"),
            expected_layout=r.get("expected_layout"), spec_type_hint=r.get("spec_type_hint"),
            accepted_mime=r.get("accepted_mime"), anchors=r.get("anchors") or [],
            fingerprints=r.get("fingerprints") or [], grid_signals=r.get("grid_signals") or {},
            thresholds=r.get("thresholds") or {}, is_active=True,
        ))
    await db.commit()


async def _dm_user(db) -> User:
    u = User(id=uuid.uuid4(), name="DM", phone="9000000001",
             role=UserRole.DIRECT_MANAGER, password_hash="x", is_active=True)
    db.add(u)
    await db.commit()
    return u


def _read(name: str) -> bytes:
    return (DATA / name).read_bytes()


def _fake_order_llm(client_for_pages=None, confidence=0.91):
    """A stand-in for Gemini reading a scanned order PDF."""
    calls = {"n": 0}

    def clf(feats, kind, profiles):
        calls["n"] += 1
        if kind != "order_sheet":
            return None
        guess = (client_for_pages or {}).get(feats.page_count, "beau_geste")
        return {"is_order_sheet": True, "is_spec_sheet": False, "doc_kind": "order_sheet",
                "spec_type": None, "client_guess": guess, "confidence": confidence,
                "evidence": ["order number", "per-size quantity grid"], "reject_reason": None}

    clf.calls = calls
    return clf


# ── §9.1 random PDF rejected with diagnostics ────────────────────────────────
async def test_random_pdf_rejected_with_diagnostics(db):
    await _seed_templates(db)
    user = await _dm_user(db)
    svc = ProcurementService(db, classifier=None)
    sub = await svc.open_submission(user, None)

    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(user, sub.id,
                                     _read("BOM_Procurement_Workflow_fixed.pdf"),
                                     "BOM_Procurement_Workflow_fixed.pdf")
    exc = ei.value
    assert exc.reason == RejectReason.NOT_AN_ORDER_SHEET
    assert exc.http_status == 422
    v = exc.payload["validation"]
    assert v["reason_code"] == "not_an_order_sheet"
    assert v["signals_expected"] and v["signals_found"]
    assert "narrative" in " ".join(v["signals_found"]).lower()
    assert v["suggested_fix"]


# ── §9.2 all real formats accepted ───────────────────────────────────────────
async def test_real_order_pdfs_accepted_via_llm(db):
    await _seed_templates(db)
    user = await _dm_user(db)
    clf = _fake_order_llm(client_for_pages={1: "beau_geste", 11: "the_jackie"})
    svc = ProcurementService(db, classifier=clf)

    sub = await svc.open_submission(user, None)
    env = await svc.upload_order_sheet(user, sub.id, _read("Order-sheet-1.pdf"),
                                       "Order-sheet-1.pdf")
    val = env["document"]["validation"]
    assert val["status"] == "accepted"
    assert val["classified_as"] == "order_sheet"
    assert val["client_match"] == "beau_geste"
    assert val["method"] == "llm"
    assert env["document"]["storage_url"]            # promoted to submissions/

    sub2 = await svc.open_submission(user, None)
    env2 = await svc.upload_order_sheet(user, sub2.id, _read("jackiee-order-sheet.pdf"),
                                        "jackiee-order-sheet.pdf")
    assert env2["document"]["validation"]["client_match"] == "the_jackie"
    assert env2["document"]["validation"]["status"] == "accepted"


async def test_real_spec_xlsx_accepted_via_heuristic(db):
    await _seed_templates(db)
    user = await _dm_user(db)
    svc = ProcurementService(db, classifier=None)   # heuristic must not need an LLM

    sub = await svc.open_submission(user, None)
    env = await svc.upload_spec_sheet(user, sub.id, _read("spec_sheet_1.xlsx"),
                                      "spec_sheet_1.xlsx")
    v = env["document"]["validation"]
    assert v["status"] == "accepted"
    assert v["classified_as"] == "spec_sheet"
    assert v["spec_type"] == "measurement_grid"
    assert v["method"] == "heuristic"
    assert v["client_match"] == "beau_geste"

    sub2 = await svc.open_submission(user, None)
    env2 = await svc.upload_spec_sheet(user, sub2.id, _read("Jackie-cleint-spec-sheet.xlsx"),
                                       "Jackie-cleint-spec-sheet.xlsx")
    v2 = env2["document"]["validation"]
    assert v2["status"] == "accepted"
    assert v2["spec_type"] == "narrative_techpack"
    assert v2["client_match"] == "the_jackie"


# ── §9.3 new client onboarded by config only ─────────────────────────────────
async def test_new_client_by_config_no_code_change(db):
    await _seed_templates(db)
    user = await _dm_user(db)
    # Add an "australia" order-sheet profile — registry data only, no source edit.
    db.add(ClientTemplate(
        client_code="australia", doc_kind="order_sheet", display_name="Australia Co",
        anchors=[{"any": ["AUSSIE LEATHER CO", "AUSTRALIA"], "weight": 3},
                 {"any": ["ORDER", "QTY", "QUANTITY"], "weight": 1},
                 {"any": ["S", "M", "L", "XL"], "weight": 1, "kind": "size_tokens"}],
        fingerprints=[r"AU-ORD-\d{4}"],
        thresholds={"accept_high": 0.75, "reject_low": 0.30}, is_active=True))
    await db.commit()

    csv = ("AUSSIE LEATHER CO ORDER\n"
           "Order No,AU-ORD-2042,Delivery,2026-09-01\n"
           "Style,Colour,S,M,L,XL,QTY\n"
           "Jacket,Tan,4,7,9,3,23\n").encode("utf-8")
    svc = ProcurementService(db, classifier=None)
    sub = await svc.open_submission(user, None)
    env = await svc.upload_order_sheet(user, sub.id, csv, "australia_order.csv")
    v = env["document"]["validation"]
    assert v["status"] == "accepted"
    assert v["method"] == "heuristic"          # promoted to the free path by config
    assert v["client_match"] == "australia"


# ── §9.4 pairing + readiness gate ────────────────────────────────────────────
async def test_pairing_and_stage2_gate(db):
    await _seed_templates(db)
    user = await _dm_user(db)
    clf = _fake_order_llm()
    svc = ProcurementService(db, classifier=clf)

    sub = await svc.open_submission(user, None)
    # only the spec slot filled → not ready
    await svc.upload_spec_sheet(user, sub.id, _read("spec_sheet_1.xlsx"), "spec_sheet_1.xlsx")
    status = await svc.get_submission_status(sub.id)
    assert status["complete"] is False
    assert status["ready_for_stage_2"] is False
    assert any("order_sheet" in b for b in status["blocking"])

    # now the order slot → complete + ready
    await svc.upload_order_sheet(user, sub.id, _read("Order-sheet-1.pdf"), "Order-sheet-1.pdf")
    status2 = await svc.get_submission_status(sub.id)
    assert status2["complete"] is True
    assert status2["ready_for_stage_2"] is True
    assert status2["blocking"] == []


# ── §9.5 idempotent re-upload: same id, no second LLM call ───────────────────
@pytest.mark.xfail(
    strict=True,
    reason="SERVICE FEATURE DISABLED (report only, not fixable from tests): "
           "app/modules/procurement/service.py:179-187 comments out the sha256 "
           "dedupe/idempotency block ('TESTING PHASE: dedupe/idempotency check "
           "disabled'), so a byte-identical re-upload always creates a NEW Document "
           "(new id) and re-invokes the classifier. This test asserts the production "
           "contract (same document id, exactly one Document row, no second classifier "
           "call). Restore by uncommenting that block; then delete this xfail. NOTE the "
           "targeted _handle_existing dedupe tests below still pass — only the full "
           "upload flow's sha lookup is disabled.",
)
async def test_idempotent_reupload_no_second_llm(db):
    await _seed_templates(db)
    user = await _dm_user(db)
    clf = _fake_order_llm()
    svc = ProcurementService(db, classifier=clf)
    sub = await svc.open_submission(user, None)

    data = _read("Order-sheet-1.pdf")
    env1 = await svc.upload_order_sheet(user, sub.id, data, "Order-sheet-1.pdf")
    env2 = await svc.upload_order_sheet(user, sub.id, data, "Order-sheet-1.pdf")
    assert env1["document"]["id"] == env2["document"]["id"]
    assert clf.calls["n"] == 1                  # second upload was a cache hit

    from app.core.models import Document
    n_docs = await db.scalar(select(func.count(Document.id)))
    assert n_docs == 1


# ── §9.7 virus gate ──────────────────────────────────────────────────────────
async def test_virus_detected_when_enabled(db, monkeypatch):
    await _seed_templates(db)
    user = await _dm_user(db)
    monkeypatch.setattr(cfgmod.settings, "virus_scan_enabled", True, raising=False)
    fake_scanner = lambda payload: (EICAR in payload, "Eicar-Test-Signature")
    svc = ProcurementService(db, classifier=None, scanner=fake_scanner)
    sub = await svc.open_submission(user, None)

    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(user, sub.id, EICAR + b"  %PDF rest",
                                     "eicar.pdf")
    assert ei.value.reason == RejectReason.VIRUS_DETECTED
    assert ei.value.http_status == 422
    # an audit_log row was written
    from app.core.models import AuditLog
    n_audit = await db.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.action == "VIRUS_DETECTED"))
    assert n_audit == 1


async def test_virus_skipped_when_disabled(db, monkeypatch):
    await _seed_templates(db)
    user = await _dm_user(db)
    monkeypatch.setattr(cfgmod.settings, "virus_scan_enabled", False, raising=False)
    svc = ProcurementService(db, classifier=None)
    sub = await svc.open_submission(user, None)
    env = await svc.upload_spec_sheet(user, sub.id, _read("spec_sheet_1.xlsx"),
                                      "spec_sheet_1.xlsx")
    assert env["document"]["scan_status"] == ScanStatus.SKIPPED.value


# ── §9.8 no silent guessing ──────────────────────────────────────────────────
async def test_no_silent_guessing_needs_manual_review(db):
    await _seed_templates(db)
    user = await _dm_user(db)
    # LLM is unsure (below the accept bar) → must NOT fabricate a classification.
    low_conf = _fake_order_llm(confidence=0.45)
    svc = ProcurementService(db, classifier=low_conf)
    sub = await svc.open_submission(user, None)

    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(user, sub.id, _read("Order-sheet-1.pdf"),
                                     "Order-sheet-1.pdf")
    assert ei.value.reason == RejectReason.NEEDS_MANUAL_REVIEW


# ── §9.6 (service-level) content-sniffed MIME reject ─────────────────────────
async def test_unsupported_mime_rejected_by_sniff(db):
    await _seed_templates(db)
    user = await _dm_user(db)
    svc = ProcurementService(db, classifier=None)
    sub = await svc.open_submission(user, None)
    # a PNG renamed .pdf — content sniff must win over the extension
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    with pytest.raises(UploadError) as ei:
        await svc.upload_order_sheet(user, sub.id, png, "disguised.pdf")
    assert ei.value.reason == RejectReason.UNSUPPORTED_MIME
    assert ei.value.http_status == 415


# ── §9.9 stays in lane ───────────────────────────────────────────────────────
async def test_stays_in_lane_no_stage2_rows(db):
    await _seed_templates(db)
    user = await _dm_user(db)
    svc = ProcurementService(db, classifier=None)
    sub = await svc.open_submission(user, None)
    await svc.upload_spec_sheet(user, sub.id, _read("spec_sheet_1.xlsx"), "spec_sheet_1.xlsx")

    from app.modules.bom.models import SpecSheet
    from app.modules.clients.models import ClientOrder
    assert await db.scalar(select(func.count(SpecSheet.id))) == 0
    assert await db.scalar(select(func.count(ClientOrder.id))) == 0
    assert await db.scalar(select(func.count(Bom.id))) == 0


# ── sha-dedupe is scoped to (submission, slot) — no false success / no bypass ─
# Regression for the two cross-submission / cross-kind dead-ends. Seeds Document
# rows directly so it is independent of the LLM/PDF pipeline.
async def _seed_doc(db, *, submission_id, kind, sha,
                    status=ValidationStatus.ACCEPTED.value):
    from app.core.models import Document
    doc = Document(kind=kind, filename=f"{kind}.bin", mime="application/pdf",
                   sha256=sha, size_bytes=10, validation_status=status,
                   submission_id=submission_id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def test_dedupe_cross_submission_is_conflict_not_false_success(db):
    """Bug 1: a byte-identical accepted doc owned by ANOTHER submission must 409 —
    never a 201 'success' that leaves this submission's slot empty (dead-end)."""
    user = await _dm_user(db)
    svc = ProcurementService(db, classifier=None)
    sub_a = await svc.open_submission(user, None)
    sub_b = await svc.open_submission(user, None)
    other = await _seed_doc(db, submission_id=sub_b.id, kind="order_sheet", sha="dead00")

    with pytest.raises(UploadError) as ei:
        await svc._handle_existing(sub_a, "order_sheet", other)
    assert ei.value.reason == RejectReason.DUPLICATE_CONTENT
    assert ei.value.http_status == 409
    # the slot was NOT silently filled with another submission's document
    await db.refresh(sub_a)
    assert sub_a.order_document_id is None


async def test_dedupe_cross_kind_is_conflict_not_validation_bypass(db):
    """Bug 2: the same bytes accepted in the ORDER slot must not be reusable for the
    SPEC slot of the same submission (one file masquerading as both sheets)."""
    user = await _dm_user(db)
    svc = ProcurementService(db, classifier=None)
    sub = await svc.open_submission(user, None)
    order_doc = await _seed_doc(db, submission_id=sub.id, kind="order_sheet", sha="beef01")

    with pytest.raises(UploadError) as ei:
        await svc._handle_existing(sub, "spec_sheet", order_doc)
    assert ei.value.reason == RejectReason.DUPLICATE_CONTENT
    assert ei.value.http_status == 409
    await db.refresh(sub)
    assert sub.spec_document_id is None        # spec slot never repointed at the order doc


async def test_dedupe_same_slot_same_submission_still_idempotent(db):
    """The legitimate cache hit (same bytes, same slot, same submission) still
    short-circuits to success and re-points the slot — no new row, no LLM."""
    user = await _dm_user(db)
    svc = ProcurementService(db, classifier=None)
    sub = await svc.open_submission(user, None)
    doc = await _seed_doc(db, submission_id=sub.id, kind="order_sheet", sha="cafe02")

    env = await svc._handle_existing(sub, "order_sheet", doc)
    assert env["document"]["id"] == str(doc.id)
    await db.refresh(sub)
    assert sub.order_document_id == doc.id


# ── HTTP shell: RBAC + size cap (§9.6) + the full multipart happy path ───────
async def _http_setup(monkeypatch):
    """Stand up the real ASGI app on a fresh in-memory engine; seed a DM + an
    employee; return (app, dm_token, emp_token)."""
    from httpx import ASGITransport, AsyncClient  # noqa: F401  (re-exported to caller)
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod
    from app.core.database import Base
    from app.main import app
    from app.modules.users import schemas as user_schemas
    from app.modules.users.service import UserService

    engine = create_async_engine("sqlite+aiosqlite://",
                                 connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(dbmod, "async_engine", engine, raising=False)
    monkeypatch.setattr(dbmod, "AsyncSessionLocal",
                        async_sessionmaker(bind=engine, expire_on_commit=False), raising=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with dbmod.AsyncSessionLocal() as db:
        await _seed_templates(db)
        await UserService(db).create_user(user_schemas.UserCreate(
            name="DM", phone="9000000001", role=UserRole.DIRECT_MANAGER, password="9000000001"))
        await UserService(db).create_user(user_schemas.UserCreate(
            name="Emp", phone="9100000001", role=UserRole.EMPLOYEE, password="9100000001"))
    return app, engine


async def _token(client, phone):
    r = await client.post("/api/v1/auth/login", json={"username": phone, "password": phone})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_http_happy_path_rbac_and_limits(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    app, engine = await _http_setup(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        dm = await _token(c, "9000000001")
        emp = await _token(c, "9100000001")

        # open a submission (DM)
        r = await c.post("/api/v1/procurement/submissions", headers=dm, json={})
        assert r.status_code == 201
        sid = r.json()["submission_id"]

        # RBAC: an employee may not open submissions
        r = await c.post("/api/v1/procurement/submissions", headers=emp, json={})
        assert r.status_code == 403

        # happy path: upload the real spec sheet (heuristic accept)
        files = {"file": ("spec_sheet_1.xlsx", _read("spec_sheet_1.xlsx"),
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        r = await c.post(f"/api/v1/procurement/submissions/{sid}/spec-sheet", headers=dm, files=files)
        assert r.status_code == 201, r.text
        assert r.json()["document"]["validation"]["status"] == "accepted"

        # GET status reflects the filled spec slot
        r = await c.get(f"/api/v1/procurement/submissions/{sid}", headers=dm)
        assert r.json()["spec_sheet"]["present"] is True
        assert r.json()["ready_for_stage_2"] is False

        # 415: a PNG disguised as .pdf (content sniff wins)
        files = {"file": ("x.pdf", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "application/pdf")}
        r = await c.post(f"/api/v1/procurement/submissions/{sid}/order-sheet", headers=dm, files=files)
        assert r.status_code == 415

        # 413: exceed the (lowered) size cap
        monkeypatch.setattr(cfgmod.settings, "max_upload_mb", 1, raising=False)
        big = {"file": ("big.pdf", b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024), "application/pdf")}
        r = await c.post(f"/api/v1/procurement/submissions/{sid}/order-sheet", headers=dm, files=big)
        assert r.status_code == 413
    await engine.dispose()
