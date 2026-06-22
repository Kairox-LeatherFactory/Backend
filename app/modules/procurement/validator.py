"""
================================================================================
modules/procurement/validator.py — The §3 hybrid identity gate (pure, sync)
================================================================================

Run a CHEAP heuristic first; escalate to the LLM ONLY on the genuinely ambiguous
minority; never silently guess.

    file ─► [already MIME/size/AV-gated] ─►
        ├─ scanned PDF / no text layer ─────────────────► escalate (text LLM on OCR text)
        ├─ score ≥ accept_high ─────────────────────────► ACCEPT  (heuristic)
        ├─ score ≤ reject_low ──────────────────────────► REJECT  (heuristic)
        └─ reject_low < score < accept_high ────────────► escalate (text LLM)
                                                              │
        text verdict below VISION_CONF_THRESHOLD (and not ───┘
        a trusted structural reject) ───────────────────► escalate again (Gemini VISION
                                                            on the page images / full content)

    escalate: classifier present → ACCEPT / REJECT / needs_manual_review (method=llm)
              classifier absent  → needs_manual_review (graceful; easy cases already
              settled by the heuristic, so a model outage never stalls the door).
    OCR (sniffing._ocr_pdf) populates the text for a scanned PDF BEFORE this module runs,
    so the text classifier gets something to read; the vision rung is the last resort.

This module is PURE and SYNC (no DB, no I/O beyond the injected classifiers) so the
whole thing runs inside run_in_threadpool. The service supplies the registry rows as
ProfileViews, an optional text Classifier, and an optional VisionClassifier.

FUNCTION GUIDE  (pure + sync; called by pipeline.process_upload)
  is_narrative_document(feats) -> bool
      True for a flowing-prose PDF with section headings — a document ABOUT a process,
      not an order/spec sheet. This rejects the BOM-workflow PDF even though it quotes
      real client refs (fingerprints identify the CLIENT, not the KIND).
  ValidationOutcome   the verdict dataclass: status/method/confidence + classification +
      the matched/expected/found signals + a suggested fix. `.accepted` convenience prop.
      `escalate_blocked` (internal) marks a trusted reject the vision rung must not re-open.
  _structural_found(feats, extra) [private] human-readable "what we saw" list for diagnostics.
  validate_document(feats, expected_kind, profiles, *, classifier?, vision_classifier?,
      data?, filename?) -> ValidationOutcome
      THE GATE. Runs _validate_text; if that stays below VISION_CONF_THRESHOLD and a vision
      classifier is wired, shows the actual pages to Gemini and trusts that second opinion.
  _should_escalate_to_vision(outcome, threshold) [private] gate for the vision rung: only a
      non-accept that needs review / is low-confidence, never a trusted structural reject.
  _validate_text(...) [private] the heuristic→text-LLM pass (the original gate body).
  _escalate(...) -> ValidationOutcome   [private] the text-LLM path: no classifier →
      needs_manual_review; else run it then _interpret_result.
  _interpret_result(result, ...) [private] map a structured model result (text OR vision —
      same schema) → ACCEPT / REJECT / WRONG_SLOT / needs_manual_review (never a silent guess).
================================================================================
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.enums import DocumentKind
from app.modules.procurement.classifier import Classifier, VisionClassifier
from app.modules.procurement.enums import (
    ClassificationMethod,
    RejectReason,
    ValidationStatus,
)
from app.modules.procurement.registry import (
    GENERIC_CODE,
    ProfileView,
    best_match,
    detect_spec_type,
)
from app.modules.procurement.sniffing import PDF_MIME, DocFeatures

logger = logging.getLogger(__name__)

# Default accept bar for the LLM path when no scored profile is available
# (e.g. a scanned PDF with no text to score).
_LLM_ACCEPT = 0.70


def is_narrative_document(feats: DocFeatures) -> bool:
    """A flowing-prose PDF with section structure is a *document about* a process,
    not an order/spec sheet (both of which are terse & tabular). This is what lets
    the heuristic reject the BOM workflow PDF even though it QUOTES Beau Geste's
    real refs as worked examples — fingerprints identify the client, not the kind."""
    return (
        feats.mime == PDF_MIME
        and feats.has_text_layer
        and feats.word_count >= 250
        and feats.prose_ratio >= 0.30
        and len(feats.heading_hits) >= 2
    )

# GENERIC English fallback ONLY — used when no client template is onboarded for this
# slot. The real expected-signals are derived per request from the matched client
# profile's OWN anchor terms (see `_expected_signals_for`), so a foreign-language buyer
# (Italian "PROPOSTA D'ORDINE" / "TAGLIE", Japanese 規格寸法 …) is described in its own
# vocabulary instead of this static English list.
_GENERIC_EXPECTED_SIGNALS = {
    DocumentKind.ORDER_SHEET.value: [
        "order number", "quantity/size grid", "SKU or style refs", "delivery date",
    ],
    DocumentKind.SPEC_SHEET.value: [
        "per-size measurements + tolerances (measurement grid)",
        "or narrative leather/pocket/stitching spec (tech pack)",
        "customer/label", "style name",
    ],
}


def _expected_signals_for(expected_kind: str, candidates: list[ProfileView]) -> list[str]:
    """The signals a document of `expected_kind` SHOULD carry, in the onboarded
    clients' OWN language. Built from the union of the candidate client profiles'
    configured anchor terms (the per-client YAML, seeded into `client_template`), so the
    diagnostic speaks the buyer's vocabulary rather than a fixed English list. Falls back
    to `_GENERIC_EXPECTED_SIGNALS` when no client profile (only `_generic`) is onboarded."""
    terms: list[str] = []
    seen: set[str] = set()
    for p in candidates:
        if p.client_code == GENERIC_CODE:
            continue
        for group in p.anchors or []:
            for t in (group.get("any") or [])[:3]:   # the few most telling terms per group
                key = str(t).strip()
                low = key.lower()
                if key and low not in seen:
                    seen.add(low)
                    terms.append(key)
    if terms:
        return terms[:10]
    return _GENERIC_EXPECTED_SIGNALS.get(expected_kind, [])
_NOT_KIND = {
    DocumentKind.ORDER_SHEET.value: RejectReason.NOT_AN_ORDER_SHEET,
    DocumentKind.SPEC_SHEET.value: RejectReason.NOT_A_SPEC_SHEET,
}
_SUGGESTED_FIX = {
    DocumentKind.ORDER_SHEET.value: (
        "This does not look like a buyer order sheet. Upload the client's order "
        "sheet (PDF/XLSX/CSV) containing per-size quantities and an order number."
    ),
    DocumentKind.SPEC_SHEET.value: (
        "This does not look like a spec sheet. Upload the client's specification "
        "(a per-size measurement grid, or a narrative tech pack)."
    ),
}


@dataclass
class ValidationOutcome:
    status: ValidationStatus
    method: ClassificationMethod
    confidence: float
    classified_kind: str | None = None
    spec_type: str | None = None
    client_match: str | None = None
    closest_profile: str | None = None
    reason_code: RejectReason | None = None
    signals_matched: list[str] = field(default_factory=list)
    signals_expected: list[str] = field(default_factory=list)
    signals_found: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    suggested_fix: str | None = None
    # Internal only (never serialised by presenters): a deliberate structural reject we
    # TRUST (the narrative-document guard) sets this so the vision rung can't re-open it.
    escalate_blocked: bool = False
    llm_label: str | None = None
    llm_model: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == ValidationStatus.ACCEPTED


def _structural_found(feats: DocFeatures, extra: list[str]) -> list[str]:
    found = list(extra)
    if feats.page_count:
        found.append(f"{feats.page_count}-page {('scanned ' if feats.is_scanned_pdf else '')}PDF")
    if feats.has_text_layer:
        found.append("prose text layer")
    if feats.n_cols:
        found.append(f"spreadsheet grid {feats.n_rows}x{feats.n_cols}")
    return found


def validate_document(
    feats: DocFeatures,
    expected_kind: str,
    profiles: list[ProfileView],
    *,
    classifier: Classifier | None = None,
    vision_classifier: VisionClassifier | None = None,
    data: bytes = b"",
    filename: str = "",
) -> ValidationOutcome:
    """The §3 identity gate with the full escalation ladder:
        heuristic  →  text classifier (on native or OCR text)  →  vision classifier.
    The text pass is `_validate_text`; when it stays below `vision_conf_threshold`
    (genuinely ambiguous / needs_manual_review) and a vision classifier is wired, we
    show the actual page images to Gemini for a second opinion. A vision result of any
    kind is trusted over the inconclusive text verdict; if vision is unavailable or also
    inconclusive, the text verdict stands (still never a fabricated accept)."""
    outcome = _validate_text(feats, expected_kind, profiles, classifier=classifier)

    if vision_classifier is None or not _should_escalate_to_vision(
        outcome, settings.vision_conf_threshold
    ):
        return outcome

    candidates = [p for p in profiles if p.doc_kind == expected_kind]
    generic = next((p for p in candidates if p.client_code == GENERIC_CODE), None)
    accept_bar = generic.accept_high if generic else _LLM_ACCEPT
    expected_signals = _expected_signals_for(expected_kind, candidates)
    logger.info("escalating to VISION classifier: kind=%s text_verdict=%s conf=%.2f",
                expected_kind, outcome.status.value, outcome.confidence)
    try:
        result = vision_classifier(data, filename, feats, expected_kind, candidates)
    except Exception:
        logger.exception("vision classifier raised; keeping text verdict")
        result = None
    if not result:
        logger.info("vision classifier inconclusive/unavailable; keeping text verdict %s",
                    outcome.status.value)
        return outcome      # vision unavailable/errored → keep the text verdict
    return _interpret_result(
        result, feats, expected_kind,
        closest=outcome.closest_profile, expected_signals=expected_signals,
        accept_bar=accept_bar, signals_matched=[],
    )


def _should_escalate_to_vision(outcome: ValidationOutcome, threshold: float) -> bool:
    """Send the actual pages to the vision model only when the text path was NOT a
    confident accept — i.e. it needs manual review, or it rejected with low confidence.
    A trusted structural reject (the narrative-document guard) is never re-opened."""
    if outcome.accepted or outcome.escalate_blocked:
        return False
    if outcome.status == ValidationStatus.NEEDS_MANUAL_REVIEW:
        return True
    return outcome.confidence < threshold


def _validate_text(
    feats: DocFeatures,
    expected_kind: str,
    profiles: list[ProfileView],
    *,
    classifier: Classifier | None = None,
) -> ValidationOutcome:
    candidates = [p for p in profiles if p.doc_kind == expected_kind]
    generic = next((p for p in candidates if p.client_code == GENERIC_CODE), None)
    expected_signals = _expected_signals_for(expected_kind, candidates)

    # ── force escalation for scanned/handwritten PDFs (no text to score) ─────
    if feats.is_scanned_pdf or not (feats.text_blob or "").strip():
        logger.info("no text layer (scanned=%s) → escalating to LLM for kind=%s",
                    feats.is_scanned_pdf, expected_kind)
        return _escalate(
            feats, expected_kind, candidates, classifier,
            heuristic_conf=0.0, signals_matched=[],
            closest=None, expected_signals=expected_signals,
        )

    # ── narrative-document guard (process doc that quotes real client refs) ──
    if is_narrative_document(feats):
        found = ["prose paragraphs / flowing narrative text"]
        if feats.heading_hits:
            found.append("section headings: " + ", ".join(feats.heading_hits[:6]))
        return ValidationOutcome(
            status=ValidationStatus.REJECTED,
            method=ClassificationMethod.HEURISTIC,
            confidence=round(min(feats.prose_ratio, 0.25), 4),
            classified_kind=None, client_match=None, closest_profile=GENERIC_CODE,
            reason_code=_NOT_KIND.get(expected_kind),
            signals_expected=expected_signals, signals_found=found,
            suggested_fix=(
                "This looks like a process/specification narrative, not a buyer "
                + expected_kind.replace("_", " ")
                + ". Upload the client's "
                + expected_kind.replace("_", " ")
                + " (PDF/XLSX/CSV) with the actual order/spec content."
            ),
            escalate_blocked=True,   # a TRUSTED structural reject — don't let vision re-open it
        )

    # ── heuristic scoring ────────────────────────────────────────────────────
    score = best_match(feats, candidates)
    profile = score.profile
    closest = profile.client_code
    client_match = None if profile.client_code == GENERIC_CODE else profile.client_code
    logger.info("heuristic score: kind=%s profile=%s conf=%.2f fp_hits=%d "
                "(accept_high=%.2f reject_low=%.2f)", expected_kind, closest,
                score.confidence, score.fingerprint_hits,
                profile.accept_high, profile.reject_low)

    if score.confidence >= profile.accept_high:
        spec_type = detect_spec_type(feats) if expected_kind == DocumentKind.SPEC_SHEET.value else None
        return ValidationOutcome(
            status=ValidationStatus.ACCEPTED,
            method=ClassificationMethod.HEURISTIC,
            confidence=round(score.confidence, 4),
            classified_kind=expected_kind,
            spec_type=spec_type,
            client_match=client_match,
            closest_profile=closest,
            signals_matched=score.signals_matched,
            signals_expected=expected_signals,
            evidence=score.signals_matched,
        )

    if score.confidence <= profile.reject_low:
        return ValidationOutcome(
            status=ValidationStatus.REJECTED,
            method=ClassificationMethod.HEURISTIC,
            confidence=round(score.confidence, 4),
            classified_kind=None,
            client_match=None,
            closest_profile=closest,
            reason_code=_NOT_KIND.get(expected_kind),
            signals_matched=score.signals_matched,
            signals_expected=expected_signals,
            signals_found=_structural_found(feats, score.signals_matched),
            suggested_fix=_SUGGESTED_FIX.get(expected_kind),
        )

    # mid-band → escalate
    return _escalate(
        feats, expected_kind, candidates, classifier,
        heuristic_conf=score.confidence, signals_matched=score.signals_matched,
        closest=closest, expected_signals=expected_signals,
    )


def _escalate(
    feats: DocFeatures,
    expected_kind: str,
    candidates: list[ProfileView],
    classifier: Classifier | None,
    *,
    heuristic_conf: float,
    signals_matched: list[str],
    closest: str | None,
    expected_signals: list[str],
) -> ValidationOutcome:
    generic = next((p for p in candidates if p.client_code == GENERIC_CODE), None)
    accept_bar = generic.accept_high if generic else _LLM_ACCEPT

    if classifier is None:
        # Graceful: no model available → defer to a human, don't fabricate.
        return ValidationOutcome(
            status=ValidationStatus.NEEDS_MANUAL_REVIEW,
            method=ClassificationMethod.HEURISTIC,
            confidence=round(heuristic_conf, 4),
            classified_kind=None,
            closest_profile=closest,
            reason_code=RejectReason.NEEDS_MANUAL_REVIEW,
            signals_matched=signals_matched,
            signals_expected=expected_signals,
            signals_found=_structural_found(feats, signals_matched),
            suggested_fix=(
                "Could not classify automatically and no review model is configured. "
                "A human must confirm this document's type."
            ),
        )

    logger.info("invoking TEXT classifier (LLM) for kind=%s (heuristic_conf=%.2f)",
                expected_kind, heuristic_conf)
    result = classifier(feats, expected_kind, candidates)
    if not result:
        logger.warning("TEXT classifier returned nothing for kind=%s → needs_manual_review",
                       expected_kind)
        return ValidationOutcome(
            status=ValidationStatus.NEEDS_MANUAL_REVIEW,
            method=ClassificationMethod.LLM,
            confidence=round(heuristic_conf, 4),
            closest_profile=closest,
            reason_code=RejectReason.NEEDS_MANUAL_REVIEW,
            signals_expected=expected_signals,
            signals_found=_structural_found(feats, signals_matched),
            suggested_fix="Automated review was inconclusive; a human must confirm the document type.",
        )

    return _interpret_result(
        result, feats, expected_kind,
        closest=closest, expected_signals=expected_signals,
        accept_bar=accept_bar, signals_matched=signals_matched,
    )


def _interpret_result(
    result: dict,
    feats: DocFeatures,
    expected_kind: str,
    *,
    closest: str | None,
    expected_signals: list[str],
    accept_bar: float,
    signals_matched: list[str],
) -> ValidationOutcome:
    """Turn a structured model result (TEXT classifier OR VISION classifier — same
    schema) into a verdict. Both rungs are `method=llm`: wrong-slot reject, accept above
    the bar, confident not-the-kind reject, else needs_manual_review (never a silent guess)."""
    conf = float(result.get("confidence") or 0.0)
    doc_kind = result.get("doc_kind")
    evidence = [str(e) for e in (result.get("evidence") or [])][:8]
    client_guess = result.get("client_guess") or None
    llm_label = result.get("llm_label")
    llm_model = result.get("llm_model")
    logger.info("LLM verdict: expected=%s doc_kind=%s conf=%.2f accept_bar=%.2f client_guess=%s",
                expected_kind, doc_kind, conf, accept_bar, client_guess)

    # The model thinks it's the OTHER slot's document → wrong slot (a clear 422).
    other = (
        DocumentKind.SPEC_SHEET.value
        if expected_kind == DocumentKind.ORDER_SHEET.value
        else DocumentKind.ORDER_SHEET.value
    )
    matches_expected = (
        result.get("is_order_sheet") if expected_kind == DocumentKind.ORDER_SHEET.value
        else result.get("is_spec_sheet")
    )

    if doc_kind == other or (matches_expected is False and result.get(f"is_{other}")):
        return ValidationOutcome(
            status=ValidationStatus.REJECTED,
            method=ClassificationMethod.LLM, confidence=round(conf, 4),
            closest_profile=closest, reason_code=RejectReason.WRONG_SLOT,
            signals_expected=expected_signals, signals_found=evidence,
            suggested_fix=f"This looks like a {other.replace('_', ' ')}, not a "
                          f"{expected_kind.replace('_', ' ')}. Upload it to the correct slot.",
        )

    if matches_expected and doc_kind == expected_kind and conf >= accept_bar:
        spec_type = None
        if expected_kind == DocumentKind.SPEC_SHEET.value:
            spec_type = result.get("spec_type") or detect_spec_type(feats)
        return ValidationOutcome(
            status=ValidationStatus.ACCEPTED,
            method=ClassificationMethod.LLM, confidence=round(conf, 4),
            classified_kind=expected_kind, spec_type=spec_type,
            client_match=client_guess, closest_profile=closest,
            signals_matched=evidence, signals_expected=expected_signals,
            evidence=evidence,
            llm_label=llm_label, llm_model=llm_model,
        )

    # The model is confident it's NOT the expected kind → reject with its reason.
    if matches_expected is False and conf >= accept_bar:
        return ValidationOutcome(
            status=ValidationStatus.REJECTED,
            method=ClassificationMethod.LLM, confidence=round(conf, 4),
            closest_profile=closest, reason_code=_NOT_KIND.get(expected_kind),
            signals_expected=expected_signals,
            signals_found=evidence or _structural_found(feats, signals_matched),
            suggested_fix=result.get("reject_reason") or _SUGGESTED_FIX.get(expected_kind),
        )

    # Order/spec-shaped but below the accept bar → human keys it (no guess).
    return ValidationOutcome(
        status=ValidationStatus.NEEDS_MANUAL_REVIEW,
        method=ClassificationMethod.LLM, confidence=round(conf, 4),
        classified_kind=None, closest_profile=closest,
        reason_code=RejectReason.NEEDS_MANUAL_REVIEW,
        signals_matched=evidence, signals_expected=expected_signals,
        signals_found=evidence or _structural_found(feats, signals_matched),
        suggested_fix=(
            "This is order/spec-shaped but the classifier was not confident enough "
            "to accept it. A human must confirm the document type."
        ),
    )
