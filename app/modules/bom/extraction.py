"""
================================================================================
modules/bom/extraction.py — Stage-2 extraction (LLM-ONLY, manual fallback)
================================================================================

There are NO HEURISTIC RULES in this module.

Every client's spec sheets and order sheets use a different template — different
language, different layout, different labels. A rule-based parser tuned to two
clients will silently return mostly-empty results for the third, fourth, fifth
client without warning that anything was missed. That's the "silent wrong data"
failure mode, which is worse than no data.

Instead: the LLM is the SOLE extractor, with each rung RETRIED (exponential
backoff) before falling through:

    Gemini (primary, retried)  ->  Groq (text fallback, retried)  ->  manual_entry_required

If both LLMs are unavailable, or both exhaust their retries, the public
functions return an empty result with `extracted_by="manual"` and the warning
`manual_entry_required`. The caller (service layer) sees this and either
prompts the cutting manager to enter data manually, or holds the BOM in a
"pending extraction" state for retry.

TYPED CONTRACT (the #3/#4 change)
    The public entry points now return the Pydantic models DIRECTLY
    (ExtractedSpec / ExtractedOrder) — the legacy intermediate-dict shim is gone
    (_spec_to_intermediate / _legacy_attributes / _order_to_intermediate /
    validate_intermediate all removed). The service layer consumes the typed
    objects, does its own native-term -> pom_code resolution and attribute
    flattening, and writes the staging rows from `model.model_dump(mode="json")`.
    Range/coherence warnings live in the contract (ExtractedSpec validators).

ARCHITECTURE (CLAUDE.md — unchanged posture)
  * PURE + SYNC. model.invoke() is blocking; the async service threadpools the
    whole call. No DB session in this module. (The retry time.sleep() blocks the
    threadpool worker, not the event loop — fine at our concurrency.)
  * No path EVER raises. Failures -> a valid ExtractedSpec/ExtractedOrder with
    `warnings=["...reason...", "manual_entry_required"]`.
  * POM-dictionary term resolution + garment_type mapping happen in the SERVICE
    (DB-touching), NOT here. Extraction emits NATIVE terms (no pom_code).
  * Reuses classifier._init_model / classifier._coerce and the existing settings.

FUNCTION GUIDE
  extract_spec(data, filename, mime) -> ExtractedSpec     PUBLIC entry. Never raises.
  extract_order(data, filename, mime, client_match_code=None) -> ExtractedOrder PUBLIC.
  llm_extract_spec(kind, payload) -> ExtractedSpec | None  Gemini->Groq, never raises.
  llm_extract_order(kind, payload) -> ExtractedOrder | None Gemini->Groq, never raises.
================================================================================
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

from app.core.config import settings
from app.modules.bom.excel_content import xlsx_to_markdown,xls_to_markdown
from app.modules.bom.extraction_schemas import (
    ExtractedOrder,
    ExtractedSpec,
    is_empty_order,
    is_empty_spec,
)
from app.modules.bom.pdf_content import pdf_extract_text, pdf_to_llm_input

logger = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"
PDF_MIME = "application/pdf"

# Per-rung retry policy. Blanket retry (we don't classify retryable vs fatal
# exceptions because the langchain/Gemini/Groq error taxonomy isn't stable) — kept
# cheap so a guaranteed failure (auth) costs ~1.5s, not minutes.
_LLM_RETRY_ATTEMPTS = 3
_LLM_RETRY_BASE_DELAY = 0.5  # seconds; exponential: 0.5, 1.0, 2.0


# ════════════════════════════════════════════════════════════════════════════
# LLM PROMPTS
# Single source of truth — change them here, never in callers.
# ════════════════════════════════════════════════════════════════════════════

_SPEC_PROMPT = """Extract structured data from a leather garment SPEC SHEET.

The document may be in any language (Japanese, Italian, English, Korean,
Spanish, German, French, etc.) and any layout (measurement grid, narrative
techpack, a mix). PRESERVE the original language for measurement names — do
not translate them.

Extract these fields (omit any field that's genuinely not in the document —
do NOT invent values):

- style_no, article, client_name, season
- sizes: list of size codes exactly as printed
- measurements: list of {source_term (native language), by_size {size: cm}, pitch}
- materials: dict with keys leather_quality, leather_substance_mm (list of
  allowed thicknesses), and any other material attributes named in the document
- sub_materials: list of {name (native term, e.g. 別布), material} for any
  SECONDARY leather/fabric that is NOT the main shell and NOT the lining
  (e.g. a contrast panel). Omit if there are none.
- interlining: dict with present (boolean), material, placement — the
  fusible/non-fusible interlining if the document names one; omit if none.
- color_details: dict with primary, secondary, and any finish/colour notes use attributes details for other color details (e.g. "matte finish", "slightly darker than swatch") — omit if none.
- lining: dict with lined (boolean), details (text), material
- accessories: list of {type, supplied_by (factory|client|if nothing mentioned then factory by default), placement,
  spec, finish, qty_per_garment}
  - qty_per_garment is the INTEGER count of THIS accessory per single garment
    (e.g. 2 if there are two rear zippers, 1 for a single front zipper). If the
    document does not state a count, omit it (the system defaults to 1).
- brand_label: dict with type, text, placement
- size_label: text on the size label
- pattern_reference: {pattern_code, base_size} only if the document says
  "follow pattern X" or similar — otherwise omit
- technical_details: list of {category, instruction} where category is one of
  stitching|cutting|workmanship|finishing|general
- garment_type_guess: best guess at the garment type (jacket, pants, skirt, etc.)

Output ONLY valid JSON matching this structure. No prose, no markdown fences.
"""

_ORDER_PROMPT = """Extract structured data from a leather garment ORDER SHEET.

The document may be in any language and any layout. Order sheets may contain
multiple orders (one per visual block) and may have production-tracking rows
mixed in (Cutting started, Week Period, Overall Total) — IGNORE the
production rows.

Extract these fields:

- order_number, style_no, article, client_name, season, currency
- price_per_garment (numeric)
- delivery_date (ISO yyyy-mm-dd format if you can parse it)
- payment_term
- lines: list of {color, article, sizes {size: qty}}
  - One line per (style, color) combination
  - The same color under a different style is a SEPARATE line
  - sizes may use alpha codes (S, M, L, XL, XXL) or numeric (38, 40, 42, 44)
  - Continuation rows (blank style with data filled below) inherit from the
    previous style

Do NOT trust any printed GRAND TOTAL. It is recomputed downstream from your
line data; just give the lines accurately.

Output ONLY valid JSON. No prose, no markdown fences.
"""


# ════════════════════════════════════════════════════════════════════════════
# LLM PLUMBING
# ════════════════════════════════════════════════════════════════════════════

def _build_content(kind: str, payload: Any, prompt: str) -> list[dict]:
    """Assemble the LLM message: prompt + payload (text or list of image bytes).
    Images are embedded as data: URLs (base64); text payloads as a plain text
    block. Vision-capable models can accept the image_url blocks alongside text."""
    blocks: list[dict] = [{"type": "text", "text": prompt}]
    if kind == "images":
        for img_bytes in payload or []:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
    else:
        blocks.append({"type": "text", "text": str(payload)})
    return blocks


def _invoke_with_retry(model, message, label: str, *,
                       attempts: int = _LLM_RETRY_ATTEMPTS,
                       base_delay: float = _LLM_RETRY_BASE_DELAY):
    """Invoke a single model with bounded exponential-backoff retry. Returns the
    raw response on success or None when every attempt failed. Never raises — a
    failure here just means the caller falls to the next rung."""
    for attempt in range(1, attempts + 1):
        try:
            return model.invoke([message])
        except Exception as exc:                                # noqa: BLE001
            if attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning("LLM (%s) attempt %d/%d failed: %s — retrying in %.1fs",
                               label, attempt, attempts, exc, delay)
                time.sleep(delay)
            else:
                logger.warning("LLM (%s) failed after %d attempts: %s",
                               label, attempts, exc)
    return None


def _llm_invoke(kind: str, content: list[dict]) -> tuple[str, str] | None:
    """Invoke the configured LLM. Primary (Gemini) with retry, then the text-only
    fallback (Groq) with retry for text payloads — vision payloads have no text
    fallback because Groq doesn't currently support image input.

    Returns (engine_name, raw_response) on success, None when no model is
    available or both rungs exhausted their retries. Never raises."""
    try:
        from langchain_core.messages import HumanMessage
    except ImportError:
        logger.warning("langchain_core not importable — extraction cannot use LLM")
        return None

    from app.modules.procurement.classifier import _init_model

    message = HumanMessage(content=content)

    # Vision payloads use the vision-capable model; text uses extraction_model.
    primary_spec = (settings.vision_model if kind == "images"
                    else settings.extraction_model)
    primary = _init_model(primary_spec)
    if primary is not None:
        resp = _invoke_with_retry(primary, message, primary_spec)
        if resp is not None:
            engine = "gemini" if "gemini" in primary_spec.lower() else "groq"
            return engine, str(getattr(resp, "content", resp))

    # Groq fallback only meaningful for text payloads
    if kind == "images":
        return None

    fb_spec = settings.extraction_fallback_model
    fallback = _init_model(fb_spec)
    if fallback is not None:
        resp = _invoke_with_retry(fallback, message, fb_spec)
        if resp is not None:
            engine = "groq" if "groq" in fb_spec.lower() else "gemini"
            return engine, str(getattr(resp, "content", resp))

    return None


def llm_extract_spec(kind: str, payload: Any) -> ExtractedSpec | None:
    """LLM spec extraction. Returns None when no LLM is available, the response
    is unparseable, schema validation fails, or the result is empty (no usable
    fields). The caller treats None as "fall through to manual." Never raises."""
    from app.modules.procurement.classifier import _coerce
    result = _llm_invoke(kind, _build_content(kind, payload, _SPEC_PROMPT))
    if result is None:
        return None
    engine, raw = result
    parsed = _coerce(raw)
    if not isinstance(parsed, dict):
        logger.info("LLM (%s) returned non-dict for spec: %r", engine, type(parsed))
        return None
    parsed["extracted_by"] = engine                             # we set this, not the LLM
    try:
        spec = ExtractedSpec.model_validate(parsed)
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("spec schema validation failed for %s: %s", engine, exc)
        return None
    if is_empty_spec(spec):
        logger.info("LLM (%s) returned empty spec — trying next rung", engine)
        return None
    return spec


def llm_extract_order(kind: str, payload: Any) -> ExtractedOrder | None:
    """LLM order extraction. Same contract as llm_extract_spec — returns None
    when nothing usable is produced. Never raises."""
    from app.modules.procurement.classifier import _coerce
    result = _llm_invoke(kind, _build_content(kind, payload, _ORDER_PROMPT))
    if result is None:
        return None
    engine, raw = result
    parsed = _coerce(raw)
    if not isinstance(parsed, dict): 
        logger.info("LLM (%s) returned non-dict for order: %r", engine, type(parsed))
        return None
    parsed["extracted_by"] = engine
    try:
        order = ExtractedOrder.model_validate(parsed)
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("order schema validation failed for %s: %s", engine, exc)
        return None
    if is_empty_order(order):
        logger.info("LLM (%s) returned empty order — trying next rung", engine)
        return None
    return order


# ════════════════════════════════════════════════════════════════════════════
# MANUAL FALLBACK
# When the LLM rungs all fail, we produce an empty but valid result with the
# warning "manual_entry_required". NO heuristic guessing — the system is
# honest about being unable to read this file, instead of silently returning
# partial data the user might trust.
# ════════════════════════════════════════════════════════════════════════════

def _empty_spec(reason: str) -> ExtractedSpec:
    """Build a valid-but-empty spec with the given reason + manual_entry_required."""
    return ExtractedSpec(
        extracted_by="manual",
        confidence_overall=0.0,
        warnings=[reason, "manual_entry_required"],
    )


def _empty_order(reason: str) -> ExtractedOrder:
    """Build a valid-but-empty order with the given reason + manual_entry_required."""
    return ExtractedOrder(
        extracted_by="manual",
        confidence_overall=0.0,
        warnings=[reason, "manual_entry_required"],
    )


# ════════════════════════════════════════════════════════════════════════════
# ROUTING HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _sniff_mime(data: bytes | None) -> str | None:
    """Identify file type from magic bytes when the caller didn't supply a mime
    or filename hint. Returns the canonical mime string or None."""
    if not data or len(data) < 4:
        return None
    if data[:4] == b"%PDF":
        return PDF_MIME
    if data[:2] == b"PK":                                       # zip container -> xlsx/xlsm
        return XLSX_MIME
    return None


def _resolve_kind(data: bytes, filename: str, mime: str | None) -> str:
    """Decide the file kind: 'pdf', 'xlsx', 'xls', 'csv', 'unknown'."""
    name = (filename or "").lower()
    sniffed = mime or _sniff_mime(data)
    if sniffed == PDF_MIME or name.endswith(".pdf"):
        return "pdf"
    if sniffed == XLSX_MIME or name.endswith((".xlsx", ".xlsm")):
        return "xlsx"
    if sniffed == "application/vnd.ms-excel" or name.endswith(".xls"):
        return "xls"
    if sniffed == CSV_MIME or name.endswith((".csv", ".tsv")):
        return "csv"
    return "unknown"


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS — return the typed contract directly.
# ════════════════════════════════════════════════════════════════════════════

def extract_spec(data: bytes, filename: str, mime: str | None = None) -> ExtractedSpec:
    """Extract a leather spec sheet into an ExtractedSpec. Never raises — failures
    surface as `manual_entry_required` in `warnings`.

    Routing:
      PDF (digital text)    -> text LLM
      PDF (scanned/image)   -> vision LLM (Gemini), no Groq fallback
      XLSX / XLSM           -> markdown -> text LLM
      CSV / TSV             -> text -> text LLM
      everything else       -> manual_entry_required"""
    logger.info("calling extract_spec with filename=%r mime=%r data_length=%d",
                filename, mime, len(data))
    try:
        kind = _resolve_kind(data, filename, mime)

        if kind == "pdf":
            payload_kind, payload = pdf_to_llm_input(data)
            if not payload:
                return _empty_spec("pdf_unreadable")
            if (payload_kind == "images"
                    and not getattr(settings, "vision_classifier_enabled", True)):
                return _empty_spec("vision_disabled_for_scanned_pdf")
            return llm_extract_spec(payload_kind, payload) or _empty_spec("llm_extraction_failed")

        if kind in ("xlsx", "xls"):
            markdown = (xlsx_to_markdown if kind == "xlsx" else xls_to_markdown)(data)
            if not markdown.strip():
                return _empty_spec("xls_unreadable" if kind == "xls" else "xlsx_unreadable")
            return llm_extract_spec("text", markdown) or _empty_spec("llm_extraction_failed")

        if kind == "csv":
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:                                   # noqa: BLE001
                return _empty_spec("csv_unreadable")
            return llm_extract_spec("text", text) or _empty_spec("llm_extraction_failed")

        return _empty_spec(f"unsupported_file_type: mime={mime!r} name={filename!r}")

    except Exception as exc:                                    # noqa: BLE001
        logger.exception("extract_spec degraded for %s (%s): %s", filename, mime, exc)
        return _empty_spec(f"extract_spec_exception: {type(exc).__name__}")


def extract_order(data: bytes, filename: str, mime: str | None = None,
                  client_match_code: str | None = None) -> ExtractedOrder:
    """Extract a leather order sheet into an ExtractedOrder. Never raises — failures
    surface as `manual_entry_required`. `client_match_code` is accepted for caller
    signature stability but is informational only (the service already holds it; the
    typed order model does not carry it)."""
    logger.info("calling extract_order with filename=%r mime=%r client_match_code=%r "
                "data_length=%d", filename, mime, client_match_code, len(data))
    try:
        kind = _resolve_kind(data, filename, mime)

        if kind == "pdf":
            payload_kind, payload = pdf_to_llm_input(data)
            if not payload:
                return _empty_order("pdf_unreadable")
            if (payload_kind == "images"
                    and not getattr(settings, "vision_classifier_enabled", True)):
                return _empty_order("vision_disabled_for_scanned_pdf")
            return llm_extract_order(payload_kind, payload) or _empty_order("llm_extraction_failed")

        if kind in ("xlsx", "xls"):
            markdown = (xlsx_to_markdown if kind == "xlsx" else xls_to_markdown)(data)
            if not markdown.strip():
                return _empty_order("xls_unreadable" if kind == "xls" else "xlsx_unreadable")
            return llm_extract_order("text", markdown) or _empty_order("llm_extraction_failed")
        
        if kind == "csv":
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:                                   # noqa: BLE001
                return _empty_order("csv_unreadable")
            return llm_extract_order("text", text) or _empty_order("llm_extraction_failed")

        return _empty_order(f"unsupported_file_type: mime={mime!r} name={filename!r}")

    except Exception as exc:                                    # noqa: BLE001
        logger.exception("extract_order degraded for %s (%s): %s", filename, mime, exc)
        return _empty_order(f"extract_order_exception: {type(exc).__name__}")