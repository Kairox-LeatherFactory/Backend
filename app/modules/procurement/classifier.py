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
================================================================================
"""
from __future__ import annotations

import json
from typing import Callable

from app.core.config import settings
from app.modules.procurement.sniffing import DocFeatures
from app.modules.procurement.registry import ProfileView

# A classifier maps (features, expected_kind, candidate profiles) → a structured
# result dict (the schema above) or None if no model is available.
Classifier = Callable[[DocFeatures, str, "list[ProfileView]"], "dict | None"]

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
    blob = (feats.text_blob or "")[:6000]
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


def _coerce(raw: str) -> dict | None:
    """Parse the model's JSON, tolerating ```json fences and surrounding prose."""
    if not raw:
        return None
    s = raw.strip()
    if "```" in s:
        s = s.split("```", 2)[1]
        s = s[4:] if s.lower().startswith("json") else s
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None


def _init_model(spec: str):
    """spec like 'gemini:gemini-2.0-flash' / 'groq:llama-3.3-70b-versatile'.
    Returns a LangChain chat model or None if the provider/key is unavailable."""
    if not spec or ":" not in spec:
        return None
    provider, model = spec.split(":", 1)
    try:
        if provider == "gemini":
            if not settings.gemini_api_key:
                return None
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(model=model, google_api_key=settings.gemini_api_key,
                                          temperature=0)
        if provider == "groq":
            if not settings.groq_api_key:
                return None
            from langchain_groq import ChatGroq

            return ChatGroq(model=model, api_key=settings.groq_api_key, temperature=0)
    except Exception:
        return None
    return None


def build_default_classifier() -> Classifier | None:
    """Build the real Gemini→Groq classifier, or None if neither key is set."""
    primary = _init_model(settings.extraction_model)
    fallback = _init_model(settings.extraction_fallback_model)
    if primary is None and fallback is None:
        return None

    def _classify(feats: DocFeatures, expected_kind: str,
                  profiles: list[ProfileView]) -> dict | None:
        prompt = _build_prompt(feats, expected_kind, profiles)
        for model in (primary, fallback):
            if model is None:
                continue
            try:
                resp = model.invoke(prompt)
                parsed = _coerce(getattr(resp, "content", "") or "")
                if parsed is not None:
                    return parsed
            except Exception:
                continue        # provider error → try fallback → else None
        return None

    return _classify
