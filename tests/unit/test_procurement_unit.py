"""
UNIT · procurement Stage-1 intake — the pure, sync core. No database, no event loop.

Covers the four modules that are deliberately pure so the whole pipeline can run
inside run_in_threadpool without ever touching the event loop or a DB session:
  - sniffing.sniff_and_extract   content-sniffed MIME + feature extraction
  - registry.score_profile/best_match/detect_spec_type   the §3a heuristic scorer
  - validator.validate_document   the heuristic → LLM → vision escalation ladder
  - scanning.scan_bytes           the virus-scan policy gate
  - pipeline.process_upload       the sync orchestrator (scan → sniff → validate → store),
    driven end to end here with injected fake scanner/classifier/storage — no real
    clamd, no real LLM, no real disk, exactly as the module's own docstring intends
    ("classifier/scanner are INJECTABLE so tests drive the LLM/AV paths").
  - errors.STATUS_FOR_REASON      the reason_code -> HTTP status contract

THE CLAIM UNDER TEST throughout: a hard gate (virus/mime/size/corrupt) never gets
soft-pedaled into a guess, and a genuinely ambiguous document never gets a silent
accept or silent reject — it always surfaces as needs_manual_review.
"""
import io

import openpyxl
import pytest
from reportlab.pdfgen import canvas

from app.modules.procurement.enums import RejectReason, ScanStatus, ValidationStatus
from app.modules.procurement.errors import STATUS_FOR_REASON, UploadError
from app.modules.procurement.pipeline import process_upload, sha256_of
from app.modules.procurement.registry import (
    GENERIC_CODE,
    ProfileView,
    best_match,
    detect_spec_type,
    score_profile,
)
from app.modules.procurement.scanning import EICAR, ScannerUnavailable, scan_bytes
from app.modules.procurement.sniffing import (
    CSV_MIME,
    EmptyOrCorrupt,
    PDF_MIME,
    UnsupportedMime,
    XLSX_MIME,
    DocFeatures,
    sniff_and_extract,
)
from app.modules.procurement.validator import is_narrative_document, validate_document


# ── byte fixtures (real, parseable files — not hand-rolled magic-byte stubs) ──
def _pdf_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for line_no, line in enumerate(text.splitlines() or [text]):
        c.drawString(72, 720 - 14 * line_no, line[:110])
    c.save()
    return buf.getvalue()


def _blank_pdf_bytes() -> bytes:
    buf = io.BytesIO()
    canvas.Canvas(buf).save()          # a page with no text at all
    return buf.getvalue()


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv_bytes(rows: list[list]) -> bytes:
    return "\n".join(",".join(str(c) for c in row) for row in rows).encode("utf-8")


ORDER_SHEET_TEXT = (
    "BUYER ORDER SHEET\n"
    "Order Number: PO-88213\n"
    "Delivery Date: 2026-11-01\n"
    "Style: CLERMONT   SKU: CL-01\n"
    "Size S M L XL Total\n"
    "Qty  10 20 15 5 50\n"
)


# ══════════════════════════════════════════════════ sniffing.sniff_and_extract
def test_sniff_extract_rejects_zero_bytes():
    with pytest.raises(EmptyOrCorrupt):
        sniff_and_extract(b"", "order.pdf")


def test_sniff_extract_rejects_an_unsupported_type():
    png_magic = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    with pytest.raises(UnsupportedMime):
        sniff_and_extract(png_magic, "photo.png")


def test_sniff_extract_accepts_a_real_pdf_and_reads_its_text():
    data = _pdf_bytes(ORDER_SHEET_TEXT)
    feats = sniff_and_extract(data, "order.pdf")
    assert feats.mime == PDF_MIME
    assert feats.page_count == 1
    assert feats.has_text_layer is True
    assert "Order Number" in feats.text_blob


def test_sniff_extract_accepts_a_real_xlsx():
    data = _xlsx_bytes([["Style", "Qty"], ["CLERMONT", 50], ["TOWER", 30]])
    feats = sniff_and_extract(data, "order.xlsx")
    assert feats.mime == XLSX_MIME
    assert feats.n_rows == 3
    assert feats.numeric_ratio > 0


def test_sniff_extract_accepts_a_real_csv_and_scores_numeric_ratio():
    data = _csv_bytes([["Style", "Qty"], ["CLERMONT", 50], ["TOWER", 30]])
    feats = sniff_and_extract(data, "order.csv")
    assert feats.mime == CSV_MIME
    # 2 of 4 non-header-ish cells are numeric ("Style"/"CLERMONT"/"TOWER" text, 50/30 numeric)
    assert 0 < feats.numeric_ratio < 1


def test_sniff_extract_rejects_a_disguised_file_extension_mismatch():
    """Real PDF bytes, but the filename claims .csv — content sniff must win."""
    data = _pdf_bytes(ORDER_SHEET_TEXT)
    with pytest.raises(UnsupportedMime):
        sniff_and_extract(data, "order.csv")


def test_sniff_extract_ignores_a_missing_extension():
    """No extension to compare against -> the mismatch check is skipped entirely,
    classification rests purely on sniffed content."""
    data = _pdf_bytes(ORDER_SHEET_TEXT)
    feats = sniff_and_extract(data, "order")   # no dot at all
    assert feats.mime == PDF_MIME


def test_sniff_extract_extension_check_is_case_insensitive():
    data = _pdf_bytes(ORDER_SHEET_TEXT)
    feats = sniff_and_extract(data, "ORDER.PDF")
    assert feats.mime == PDF_MIME


def test_sniff_extract_a_textless_pdf_is_flagged_scanned():
    data = _blank_pdf_bytes()
    feats = sniff_and_extract(data, "scan.pdf")
    assert feats.has_text_layer is False
    assert feats.is_scanned_pdf is True


# ══════════════════════════════════════════════════ registry.score_profile / best_match
def _profile(**kw) -> ProfileView:
    base = dict(client_code="ACME", doc_kind="order_sheet", anchors=[], fingerprints=[],
                thresholds={"accept_high": 0.80, "reject_low": 0.30})
    base.update(kw)
    return ProfileView(**base)


def test_score_profile_anchor_hit_raises_confidence():
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, text_blob="Order Number PO-1")
    profile = _profile(anchors=[{"any": ["order number"], "weight": 1}])
    score = score_profile(feats, profile)
    assert score.confidence > 0
    assert score.anchors_hit == 1
    assert any(s.startswith("anchor:") for s in score.signals_matched)


def test_score_profile_size_token_does_not_match_inside_a_longer_word():
    """`_find_term` for kind='size_tokens' must treat 'S' as a standalone token —
    it must NOT match inside 'DELIVERS'."""
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, text_blob="Please see DELIVERS section")
    profile = _profile(anchors=[{"any": ["s"], "weight": 1, "kind": "size_tokens"}])
    score = score_profile(feats, profile)
    assert score.anchors_hit == 0


def test_score_profile_size_token_matches_as_a_standalone_word():
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, text_blob="Size S M L")
    profile = _profile(anchors=[{"any": ["s"], "weight": 1, "kind": "size_tokens"}])
    score = score_profile(feats, profile)
    assert score.anchors_hit == 1


def test_score_profile_fingerprint_regex_hit_is_recorded():
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, text_blob="PO-88213 issued")
    profile = _profile(fingerprints=[r"PO-\d{5}"])
    score = score_profile(feats, profile)
    assert score.fingerprint_hits == 1
    assert any(s.startswith("fingerprint:") for s in score.signals_matched)


def test_score_profile_bad_fingerprint_regex_is_skipped_not_fatal():
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, text_blob="anything")
    profile = _profile(fingerprints=[r"[unterminated("])   # invalid regex
    score = score_profile(feats, profile)     # must not raise
    assert score.fingerprint_hits == 0


def test_best_match_prefers_a_client_with_a_fingerprint_hit_over_higher_generic_confidence():
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, text_blob="PO-88213 order number")
    client = _profile(client_code="ACME", fingerprints=[r"PO-\d{5}"])
    generic = _profile(client_code=GENERIC_CODE, anchors=[{"any": ["order number"], "weight": 1}])
    best = best_match(feats, [client, generic])
    assert best.profile.client_code == "ACME"


def test_best_match_falls_back_to_generic_when_no_client_shows_a_real_signal():
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, text_blob="completely unrelated text")
    client = _profile(client_code="ACME", anchors=[{"any": ["something never present"], "weight": 1}])
    generic = _profile(client_code=GENERIC_CODE)
    best = best_match(feats, [client, generic])
    assert best.profile.client_code == GENERIC_CODE


def test_best_match_with_no_profiles_at_all_returns_a_zero_confidence_generic():
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, text_blob="")
    best = best_match(feats, [])
    assert best.confidence == 0.0
    assert best.profile.client_code == GENERIC_CODE


def test_detect_spec_type_wide_numeric_grid_is_measurement_grid():
    feats = DocFeatures(mime=XLSX_MIME, ext=".xlsx", size_bytes=10, n_cols=10, numeric_ratio=0.6)
    assert detect_spec_type(feats) == "measurement_grid"


def test_detect_spec_type_narrow_sheet_is_narrative_techpack():
    feats = DocFeatures(mime=XLSX_MIME, ext=".xlsx", size_bytes=10, n_cols=3, numeric_ratio=0.05)
    assert detect_spec_type(feats) == "narrative_techpack"


# ══════════════════════════════════════════════════ validator.is_narrative_document
def test_is_narrative_document_true_for_flowing_prose_with_headings():
    long_sentence = " ".join(["word"] * 20) + ". "
    blob = ("Stage 1 Executive Summary. " + long_sentence * 15
            + "Stage 2 Appendix A. " + long_sentence * 5)
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, has_text_layer=True,
                        text_blob=blob)
    from app.modules.procurement.sniffing import _compute_prose_signals
    _compute_prose_signals(blob, feats)
    assert is_narrative_document(feats) is True


def test_is_narrative_document_false_for_a_terse_tabular_sheet():
    feats = DocFeatures(mime=PDF_MIME, ext=".pdf", size_bytes=10, has_text_layer=True,
                        text_blob=ORDER_SHEET_TEXT, word_count=20, prose_ratio=0.0,
                        heading_hits=[])
    assert is_narrative_document(feats) is False


# ══════════════════════════════════════════════════ validator.validate_document
def test_validate_document_heuristic_accepts_above_the_bar():
    feats = sniff_and_extract(_pdf_bytes(ORDER_SHEET_TEXT), "order.pdf")
    profile = _profile(anchors=[{"any": ["order number"], "weight": 1}],
                       fingerprints=[r"PO-\d{5}"])
    outcome = validate_document(feats, "order_sheet", [profile])
    assert outcome.status == ValidationStatus.ACCEPTED
    assert outcome.method.value == "heuristic"


def test_validate_document_heuristic_rejects_below_the_bar():
    feats = sniff_and_extract(_pdf_bytes("Completely unrelated content with no signals."),
                              "order.pdf")
    profile = _profile(anchors=[{"any": ["order number"], "weight": 1}])
    outcome = validate_document(feats, "order_sheet", [profile])
    assert outcome.status == ValidationStatus.REJECTED
    assert outcome.reason_code == RejectReason.NOT_AN_ORDER_SHEET


def test_validate_document_narrative_guard_rejects_and_blocks_escalation():
    long_sentence = " ".join(["word"] * 20) + "."
    lines = ["Stage 1 Executive Summary."] + [long_sentence] * 15 + ["Stage 2 Appendix A."] + [long_sentence] * 5
    data = _pdf_bytes("\n".join(lines))
    feats = sniff_and_extract(data, "workflow.pdf")
    outcome = validate_document(feats, "order_sheet", [_profile()])
    assert outcome.status == ValidationStatus.REJECTED
    assert outcome.escalate_blocked is True


def test_validate_document_mid_band_escalates_and_no_classifier_degrades_to_manual_review():
    """A profile tuned so the heuristic score lands strictly between reject_low and
    accept_high (a partial anchor hit, no fingerprint) -> escalate. With no classifier
    wired, escalation must degrade to needs_manual_review, never a guess."""
    feats = sniff_and_extract(_pdf_bytes("Order Number only, nothing else matches"), "order.pdf")
    profile = _profile(
        anchors=[{"any": ["order number"], "weight": 1},
                 {"any": ["never present anywhere"], "weight": 1}],
        thresholds={"accept_high": 0.90, "reject_low": 0.10})
    outcome = validate_document(feats, "order_sheet", [profile], classifier=None)
    assert outcome.status == ValidationStatus.NEEDS_MANUAL_REVIEW
    assert outcome.reason_code == RejectReason.NEEDS_MANUAL_REVIEW
    assert outcome.method.value == "heuristic"     # never fabricated as an llm verdict


def test_validate_document_mid_band_with_classifier_accepts():
    feats = sniff_and_extract(_pdf_bytes("Order Number only, nothing else matches"), "order.pdf")
    profile = _profile(
        anchors=[{"any": ["order number"], "weight": 1},
                 {"any": ["never present anywhere"], "weight": 1}],
        thresholds={"accept_high": 0.90, "reject_low": 0.10})

    def fake_classifier(feats, expected_kind, candidates):
        return {"confidence": 0.95, "doc_kind": "order_sheet", "is_order_sheet": True,
                "evidence": ["order number line"], "client_guess": "ACME"}

    outcome = validate_document(feats, "order_sheet", [profile], classifier=fake_classifier)
    assert outcome.status == ValidationStatus.ACCEPTED
    assert outcome.method.value == "llm"
    assert outcome.client_match == "ACME"


def test_validate_document_classifier_flags_wrong_slot():
    feats = sniff_and_extract(_pdf_bytes("Order Number only, nothing else matches"), "order.pdf")
    profile = _profile(
        anchors=[{"any": ["order number"], "weight": 1},
                 {"any": ["never present anywhere"], "weight": 1}],
        thresholds={"accept_high": 0.90, "reject_low": 0.10})

    def fake_classifier(feats, expected_kind, candidates):
        return {"confidence": 0.9, "doc_kind": "spec_sheet", "is_order_sheet": False}

    outcome = validate_document(feats, "order_sheet", [profile], classifier=fake_classifier)
    assert outcome.status == ValidationStatus.REJECTED
    assert outcome.reason_code == RejectReason.WRONG_SLOT


def test_validate_document_classifier_confident_reject():
    feats = sniff_and_extract(_pdf_bytes("Order Number only, nothing else matches"), "order.pdf")
    profile = _profile(
        anchors=[{"any": ["order number"], "weight": 1},
                 {"any": ["never present anywhere"], "weight": 1}],
        thresholds={"accept_high": 0.90, "reject_low": 0.10})

    def fake_classifier(feats, expected_kind, candidates):
        return {"confidence": 0.92, "doc_kind": None, "is_order_sheet": False}

    outcome = validate_document(feats, "order_sheet", [profile], classifier=fake_classifier)
    assert outcome.status == ValidationStatus.REJECTED
    assert outcome.reason_code == RejectReason.NOT_AN_ORDER_SHEET


def test_validate_document_classifier_low_confidence_is_manual_review_not_a_guess():
    feats = sniff_and_extract(_pdf_bytes("Order Number only, nothing else matches"), "order.pdf")
    profile = _profile(
        anchors=[{"any": ["order number"], "weight": 1},
                 {"any": ["never present anywhere"], "weight": 1}],
        thresholds={"accept_high": 0.90, "reject_low": 0.10})

    def fake_classifier(feats, expected_kind, candidates):
        return {"confidence": 0.5, "doc_kind": "order_sheet", "is_order_sheet": True}

    outcome = validate_document(feats, "order_sheet", [profile], classifier=fake_classifier)
    assert outcome.status == ValidationStatus.NEEDS_MANUAL_REVIEW


def test_validate_document_classifier_returning_nothing_degrades_to_manual_review():
    feats = sniff_and_extract(_pdf_bytes("Order Number only, nothing else matches"), "order.pdf")
    profile = _profile(
        anchors=[{"any": ["order number"], "weight": 1},
                 {"any": ["never present anywhere"], "weight": 1}],
        thresholds={"accept_high": 0.90, "reject_low": 0.10})
    outcome = validate_document(feats, "order_sheet", [profile], classifier=lambda *a: None)
    assert outcome.status == ValidationStatus.NEEDS_MANUAL_REVIEW
    assert outcome.method.value == "llm"


# ══════════════════════════════════════════════════ errors.STATUS_FOR_REASON
@pytest.mark.parametrize("reason,expected_status", [
    (RejectReason.UNSUPPORTED_MIME, 415),
    (RejectReason.FILE_TOO_LARGE, 413),
    (RejectReason.EMPTY_OR_CORRUPT, 422),
    (RejectReason.VIRUS_DETECTED, 422),
    (RejectReason.SCANNER_UNAVAILABLE, 503),
    (RejectReason.NOT_AN_ORDER_SHEET, 422),
    (RejectReason.NOT_A_SPEC_SHEET, 422),
    (RejectReason.WRONG_SLOT, 422),
    (RejectReason.NEEDS_MANUAL_REVIEW, 422),
    (RejectReason.SUBMISSION_LOCKED, 409),
    (RejectReason.DUPLICATE_CONTENT, 409),
])
def test_upload_error_http_status_matches_the_reason_catalog(reason, expected_status):
    assert STATUS_FOR_REASON[reason.value] == expected_status
    assert UploadError(reason, "msg").http_status == expected_status


def test_every_reject_reason_has_a_status_mapping():
    """No RejectReason may be silently unmapped — errors.py's own docstring promises
    a STABLE status for every reason in the catalog."""
    for reason in RejectReason:
        assert reason.value in STATUS_FOR_REASON


# ══════════════════════════════════════════════════ scanning.scan_bytes
def test_scan_bytes_disabled_skips_without_calling_a_scanner(monkeypatch):
    from app.modules.procurement import scanning as scanning_mod
    monkeypatch.setattr(scanning_mod.settings, "virus_scan_enabled", False)
    calls = []
    status, sig = scan_bytes(b"anything", scanner=lambda d: calls.append(d) or (False, None))
    assert status == ScanStatus.SKIPPED
    assert sig is None
    assert calls == []


def test_scan_bytes_enabled_clean(monkeypatch):
    from app.modules.procurement import scanning as scanning_mod
    monkeypatch.setattr(scanning_mod.settings, "virus_scan_enabled", True)
    status, sig = scan_bytes(b"clean data", scanner=lambda d: (False, None))
    assert status == ScanStatus.CLEAN
    assert sig is None


def test_scan_bytes_enabled_infected(monkeypatch):
    from app.modules.procurement import scanning as scanning_mod
    monkeypatch.setattr(scanning_mod.settings, "virus_scan_enabled", True)
    status, sig = scan_bytes(EICAR, scanner=lambda d: (True, "Eicar-Test-Signature"))
    assert status == ScanStatus.INFECTED
    assert sig == "Eicar-Test-Signature"


# ══════════════════════════════════════════════════ pipeline.process_upload (fakes injected)
class _FakeStorage:
    """In-memory stand-in for StorageBackend — no disk, no DB, deterministic."""
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        self.objects[key] = data
        return f"fake://{key}"

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def move(self, src_key: str, dst_key: str) -> str:
        self.objects[dst_key] = self.objects.pop(src_key)
        return f"fake://{dst_key}"


def _clean_scanner(data: bytes):
    return (False, None)


def test_process_upload_computes_the_same_sha256_as_the_helper():
    data = _pdf_bytes(ORDER_SHEET_TEXT)
    storage = _FakeStorage()
    profile = _profile(anchors=[{"any": ["order number"], "weight": 1}],
                       fingerprints=[r"PO-\d{5}"])
    result = process_upload(data, "order.pdf", "sub-1", "order_sheet", [profile],
                            scanner=_clean_scanner, storage=storage)
    assert result.sha256 == sha256_of(data)


def test_process_upload_accepted_promotes_quarantine_to_submission_key():
    data = _pdf_bytes(ORDER_SHEET_TEXT)
    storage = _FakeStorage()
    profile = _profile(anchors=[{"any": ["order number"], "weight": 1}],
                       fingerprints=[r"PO-\d{5}"])
    result = process_upload(data, "order.pdf", "sub-1", "order_sheet", [profile],
                            scanner=_clean_scanner, storage=storage)
    assert result.outcome.accepted is True
    assert result.storage_url is not None
    # the object now lives under the FINAL submission key, not the quarantine key
    assert result.storage_key in storage.objects
    assert all(not k.startswith("quarantine/") for k in storage.objects)


def test_process_upload_rejected_deletes_the_quarantined_object():
    data = _pdf_bytes("Nothing here matches any client signal at all.")
    storage = _FakeStorage()
    profile = _profile(anchors=[{"any": ["order number"], "weight": 1}],
                       fingerprints=[r"PO-\d{5}"])
    result = process_upload(data, "order.pdf", "sub-1", "order_sheet", [profile],
                            scanner=_clean_scanner, storage=storage)
    assert result.outcome.accepted is False
    assert result.storage_url is None
    assert storage.objects == {}          # nothing left behind for a rejected file


def test_process_upload_unsupported_mime_raises_upload_error_before_any_storage_write():
    storage = _FakeStorage()
    with pytest.raises(UploadError) as ei:
        process_upload(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "photo.png", "sub-1",
                       "order_sheet", [], scanner=_clean_scanner, storage=storage)
    assert ei.value.reason == RejectReason.UNSUPPORTED_MIME
    assert storage.objects == {}


def test_process_upload_empty_file_raises_upload_error():
    storage = _FakeStorage()
    with pytest.raises(UploadError) as ei:
        process_upload(b"", "order.pdf", "sub-1", "order_sheet", [],
                       scanner=_clean_scanner, storage=storage)
    assert ei.value.reason == RejectReason.EMPTY_OR_CORRUPT


def test_process_upload_infected_file_raises_upload_error_with_signature_and_stores_nothing(monkeypatch):
    from app.modules.procurement import scanning as scanning_mod
    monkeypatch.setattr(scanning_mod.settings, "virus_scan_enabled", True)
    storage = _FakeStorage()
    with pytest.raises(UploadError) as ei:
        process_upload(EICAR, "eicar.pdf", "sub-1", "order_sheet", [],
                       scanner=lambda d: (True, "Eicar-Test-Signature"), storage=storage)
    assert ei.value.reason == RejectReason.VIRUS_DETECTED
    assert ei.value.payload["scan_signature"] == "Eicar-Test-Signature"
    assert storage.objects == {}


def test_process_upload_scanner_unreachable_fails_closed(monkeypatch):
    from app.modules.procurement import scanning as scanning_mod
    monkeypatch.setattr(scanning_mod.settings, "virus_scan_enabled", True)

    def flaky_scanner(data: bytes):
        raise ScannerUnavailable("clamd unreachable at test:0")

    storage = _FakeStorage()
    with pytest.raises(UploadError) as ei:
        process_upload(_pdf_bytes(ORDER_SHEET_TEXT), "order.pdf", "sub-1", "order_sheet", [],
                       scanner=flaky_scanner, storage=storage)
    assert ei.value.reason == RejectReason.SCANNER_UNAVAILABLE
    assert storage.objects == {}    # never stored unscanned, under any circumstance
