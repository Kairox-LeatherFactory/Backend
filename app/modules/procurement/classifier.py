"""
================================================================================
modules/procurement/classifier.py — LLM identity classifier (§3b, escalation)
================================================================================

The SECOND, independent gate of the §3 hybrid. It runs ONLY on the ambiguous
minority the cheap heuristic can't settle: scanned/handwritten PDFs (Beau Geste,
Jackie — both real files have no text layer), unknown layouts, and mid-band
scores. The structured XLSX clear the heuristic for free and never reach here.

PROVIDER CHAIN (stage-0 §4, config.py): EXTRACTION_MODEL (Gemini) →
EXTRACTION_FALLBACK_MODEL (Groq) → "needs manual review". We NEVER silently guess:
a model answer below the accept threshold returns needs_manual_review, not a
fabricated classification.

STRUCTURED OUTPUT (validated before trust)
    { is_order_sheet, is_spec_sheet, doc_kind, spec_type, client_guess,
      confidence, evidence[], reject_reason }

GRACEFUL DEGRADATION + TESTABILITY
    `build_default_classifier()` returns None when no provider key is configured,
    so escalation degrades to needs_manual_review instead of crashing the front
    door (a model outage never stalls the easy cases — they were settled by the
    heuristic). A `Classifier` is just a callable, so tests inject a deterministic
    fake — exactly how intelligence/langgraph_agent.py injects a fake chat model —
    proving the wiring end-to-end with no API key.

FUNCTION GUIDE
  Classifier   the callable type: (features, expected_kind, profiles) → result dict | None.
  _build_prompt(feats, expected_kind, profiles) -> str   [private] the structured-output prompt.
  _coerce(raw) -> dict | None   [private] parse the model's JSON (tolerates ```json fences).
  _init_model(spec) -> chat model | None
      Build a LangChain model from a "provider:model" spec (gemini/groq), or None if the
      key is missing. Also reused by bom/extraction.build_default_extractor.
  build_default_classifier() -> Classifier | None
      Build the real Gemini→Groq classifier (None if neither key is set → escalation degrades
      to needs_manual_review). CALLED FROM: ProcurementService._get_classifier. The returned
      closure tries primary→fallback and coerces the JSON.
================================================================================
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Callable

from app.core.config import settings
from app.modules.procurement.sniffing import (
    PDF_MIME,
    DocFeatures,
    render_pdf_pages,
)
from app.modules.procurement.registry import ProfileView

logger = logging.getLogger(__name__)

# A classifier maps (features, expected_kind, candidate profiles) → a structured
# result dict (the schema above) or None if no model is available.
Classifier = Callable[[DocFeatures, str, "list[ProfileView]"], "dict | None"]

# A vision classifier additionally gets the RAW bytes + filename so it can send the
# page IMAGES (PDF) — or the full content (XLSX/CSV) — to a multimodal model. Same
# structured-result contract as the text Classifier above.
VisionClassifier = Callable[
    [bytes, str, DocFeatures, str, "list[ProfileView]"], "dict | None"
]

_SCHEMA_HINT = """Return ONLY a JSON object with this exact shape:
{
  "is_order_sheet": true|false,
  "is_spec_sheet":  true|false,
  "doc_kind": "order_sheet"|"spec_sheet"|"bom_quote"|"other",
  "spec_type": "measurement_grid"|"narrative_techpack"|null,
  "client_guess": "<client_code>"|null,
  "confidence": 0.0-1.0,
  "evidence": ["short phrase", "..."],
  "reject_reason": null|"why this is not the expected document"
}"""


def _build_prompt(feats: DocFeatures, expected_kind: str, profiles: list[ProfileView]) -> str:
    known = ", ".join(sorted({p.client_code for p in profiles if p.client_code != "_generic"}))
    layout = "scanned/handwritten PDF (no text layer)" if feats.is_scanned_pdf else feats.mime
    blob = (feats.text_blob or "")
    return (
        "You are a document-intake validator for a leather-garment factory's BOM "
        "procurement workflow. Decide whether the uploaded file is genuinely the "
        f"expected document type: '{expected_kind}'.\n\n"
        f"File type: {layout}. Filename hint and extracted content follow.\n"
        f"Known client codes: {known or 'none'}.\n\n"
        f"--- extracted content (may be empty for scanned files) ---\n{blob}\n"
        "--- end content ---\n\n"
        "An ORDER SHEET has per-size quantities, an order number, and SKU/style "
        "references. A SPEC SHEET is either a per-size MEASUREMENT GRID (with "
        "tolerances) or a NARRATIVE TECH PACK (free-text leather/pockets/stitching). "
        "If it is neither (e.g. a process narrative or a BOM quote), say so.\n\n"
        + _SCHEMA_HINT
    )


def _coerce(raw) -> dict | None:
    if not raw:
        return None

    if isinstance(raw, list):
        text_parts = []

        for item in raw:
            if isinstance(item, dict):
                text_parts.append(item.get("text", ""))
            else:
                text_parts.append(str(item))

        raw = "\n".join(text_parts)

    s = str(raw).strip()

    if "```" in s:
        s = s.split("```", 2)[1]
        s = s[4:] if s.lower().startswith("json") else s

    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        return None

    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def _init_model(spec: str):
    """spec like 'gemini:gemini-2.0-flash' / 'groq:llama-3.3-70b-versatile'.
    Returns a LangChain chat model or None if the provider/key is unavailable."""
    if not spec or ":" not in spec:
        return None
    provider, model = spec.split(":", 1)
    # A per-call timeout + no internal retries: a hung or unreachable provider must
    # RAISE within the budget so the caller's Gemini→Groq→deterministic fallback runs,
    # instead of the invoke() blocking the threadpool and stalling BOM generation.
    timeout = settings.llm_request_timeout
    retries = settings.llm_max_retries
    try:
        if provider == "gemini":
            if not settings.gemini_api_key:
                return None
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(model=model, google_api_key=settings.gemini_api_key,
                                          temperature=0, timeout=100, max_retries=retries)
        if provider == "groq":
            if not settings.groq_api_key:
                return None
            from langchain_groq import ChatGroq

            return ChatGroq(model=model, api_key=settings.groq_api_key, temperature=0,
                            timeout=timeout, max_retries=retries)
    except Exception:
        return None
    return None


def build_default_classifier() -> Classifier | None:
    """Build the real Gemini→Groq classifier, or None if neither key is set."""
    primary_spec = settings.extraction_model
    fallback_spec = settings.extraction_fallback_model
    primary = _init_model(primary_spec)
    fallback = _init_model(fallback_spec)
    if primary is None and fallback is None:
        return None

    def _classify(feats: DocFeatures, expected_kind: str,
                  profiles: list[ProfileView]) -> dict | None:
        prompt = _build_prompt(feats, expected_kind, profiles)
        for label, model, spec in (("gemini-primary", primary, primary_spec),
                            ("groq-fallback", fallback, fallback_spec)):

            if model is None:
                continue
            try:
                logger.info("classifying document via %s (expected_kind=%s)", label, expected_kind)
                resp = model.invoke(prompt)
                if label == "groq-fallback":
                    logger.info(
        "GROQ RESPONSE:\n%s",
        getattr(resp, "content", "")
    )
                parsed = _coerce(getattr(resp, "content", "") or "")
                if parsed is not None:
                    parsed["llm_label"] = label
                    parsed["llm_model"] = spec
                    return parsed
                logger.warning("%s returned unparseable output → trying next rung", label)
            except Exception as exc:
                logger.warning("%s failed (%s) → falling back", label, exc)
                continue        # provider error → try fallback → else None
        logger.warning("all classifier rungs exhausted → needs_manual_review")
        return None

    return _classify


# ── Vision classifier (§3c — the LAST rung: look at the actual pages) ─────────
def _vision_prompt(expected_kind: str, profiles: list[ProfileView], *, has_images: bool) -> str:
    """Instruction text for the multimodal call. Same structured-output schema as the
    text classifier so the validator interprets both results identically."""
    known = ", ".join(sorted({p.client_code for p in profiles if p.client_code != "_generic"}))
    source = (
        "The page IMAGES of the uploaded document are attached below."
        if has_images else
        "The full extracted CONTENT of the uploaded spreadsheet/CSV follows as text."
    )
    return (
        "You are a document-intake validator for a leather-garment factory's BOM "
        "procurement workflow, looking at the document directly (the cheap text "
        "heuristic and a first text-model pass were inconclusive). Decide whether "
        f"this is genuinely the expected document type: '{expected_kind}'.\n\n"
        f"{source}\n"
        f"Known client codes: {known or 'none'}.\n\n"
        "An ORDER SHEET has per-size quantities, an order number, and SKU/style "
        "references. A SPEC SHEET is either a per-size MEASUREMENT GRID (with "
        "tolerances) or a NARRATIVE TECH PACK (free-text leather/pockets/stitching). "
        "If it is neither (e.g. a process narrative or a BOM quote), say so.\n\n"
        + _SCHEMA_HINT
    )


def build_vision_classifier() -> VisionClassifier | None:
    """Build the Gemini multimodal classifier, or None if vision is disabled or no
    Gemini key is set (→ the vision rung is simply skipped and we defer to a human).

    PDF  → render the first pages to PNG (PyMuPDF) and send them as image parts.
    XLSX/CSV (no image) → send the full extracted content as a longer text retry.
    CALLED FROM: ProcurementService._get_vision_classifier."""
    if not settings.vision_classifier_enabled:
        return None
    spec = settings.vision_model
    model = _init_model(spec)
    if model is None:
        return None

    def _classify(data: bytes, filename: str, feats: DocFeatures,
                  expected_kind: str, profiles: list[ProfileView]) -> dict | None:
        from langchain_core.messages import HumanMessage

        content: list[dict] = []
        if feats.mime == PDF_MIME:
            images = render_pdf_pages(
                data, max_pages=settings.vision_max_pages, dpi=settings.ocr_dpi)
            if not images:
                return None     # can't rasterise → nothing to "see"; let it stay manual
            content.append({"type": "text",
                            "text": _vision_prompt(expected_kind, profiles, has_images=True)})
            for png in images:
                b64 = base64.b64encode(png).decode("ascii")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                })
        else:
            # Spreadsheet/CSV: no image to show — send the full content as a text retry.
            blob = (feats.text_blob or "")[:12000]
            if not blob.strip():
                return None
            content.append({
                "type": "text",
                "text": _vision_prompt(expected_kind, profiles, has_images=False)
                + f"\n\n--- content ---\n{blob}\n--- end content ---",
            })

        try:
            resp = model.invoke([HumanMessage(content=content)])
            parsed = _coerce(getattr(resp, "content", "") or "")
            if parsed is not None:
                parsed["llm_label"] = "vision"
                parsed["llm_model"] = spec
            return parsed
        except Exception:
            return None         # provider/transport error → defer to a human

    return _classify
