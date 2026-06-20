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

Instead: the LLM is the SOLE extractor. The chain is availability- and
parse-success-based:

    Gemini (primary)  ->  Groq (text fallback)  ->  manual_entry_required

If both LLMs are unavailable, or both fail to produce a usable result, the
public functions return an empty result with `extracted_by="manual"` and the
warning `manual_entry_required`. The caller (service layer) sees this and
either prompts the cutting manager to enter data manually, or holds the BOM in
a "pending extraction" state for retry.

ARCHITECTURE (CLAUDE.md — unchanged posture)
  * PURE + SYNC. model.invoke() is blocking; the async service threadpools the
    whole call. No DB session in this module.
  * No path EVER raises. Failures -> a valid ExtractedSpec/ExtractedOrder with
    `warnings=["...reason...", "manual_entry_required"]`.
  * POM-dictionary term resolution + garment_type mapping happen in the SERVICE
    (DB-touching), NOT here. Extraction emits NATIVE terms (no pom_code).
  * Reuses classifier._init_model / classifier._coerce and the existing settings.

FUNCTION GUIDE
  extract_spec(data, filename, mime, *, spec_sheet_id=None) -> dict   PUBLIC entry.
  extract_order(data, filename, mime, client_match_code=None) -> dict PUBLIC entry.
  llm_extract_spec(kind, payload) -> ExtractedSpec | None   Gemini->Groq, never raises.
  llm_extract_order(kind, payload) -> ExtractedOrder | None Gemini->Groq, never raises.
  validate_intermediate(d) -> None  structural raise; range/coherence -> WARN (mutates).
  ExtractionError                   raised only on a structural contract violation.
================================================================================
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from app.core.config import settings
from app.modules.bom.excel_content import xlsx_to_markdown
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


class ExtractionError(ValueError):
    """Raised only by validate_intermediate on a structural contract violation
    (missing required keys, wrong row types). Range/coherence problems are
    surfaced as warnings, never as exceptions."""


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
- color_details: dict with primary, secondary, and any finish/colour notes
- lining: dict with lined (boolean), details (text), material
- accessories: list of {type, supplied_by (factory|client|null), placement,
  spec, finish}
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


def _llm_invoke(kind: str, content: list[dict]) -> tuple[str, str] | None:
    """Invoke the configured LLM. Tries the primary model first (Gemini), falls
    back to the text-only fallback (Groq) for text payloads — vision payloads
    have no text fallback because Groq doesn't currently support image input.

    Returns (engine_name, raw_response) on success, None when no model is
    available or both attempts failed. Never raises."""
    try:
        from langchain_core.messages import HumanMessage
    except ImportError:
        logger.warning("langchain_core not importable — extraction cannot use LLM")
        return None

    from app.modules.procurement.classifier import _init_model

    # Vision payloads use the vision-capable model; text uses extraction_model.
    primary_spec = (settings.vision_model if kind == "images"
                    else settings.extraction_model)
    primary = _init_model(primary_spec)
    if primary is not None:
        try:
            resp = primary.invoke([HumanMessage(content=content)])
            engine = "gemini" if "gemini" in primary_spec.lower() else "groq"
            return engine, str(getattr(resp, "content", resp))
        except Exception as exc:                                # noqa: BLE001
            logger.warning("primary LLM (%s) failed: %s", primary_spec, exc)

    # Groq fallback only meaningful for text payloads
    if kind == "images":
        return None

    fb_spec = settings.extraction_fallback_model
    fallback = _init_model(fb_spec)
    if fallback is not None:
        try:
            resp = fallback.invoke([HumanMessage(content=content)])
            engine = "groq" if "groq" in fb_spec.lower() else "gemini"
            return engine, str(getattr(resp, "content", resp))
        except Exception as exc:                                # noqa: BLE001
            logger.warning("fallback LLM (%s) failed: %s", fb_spec, exc)

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
    """Decide the file kind: 'pdf', 'xlsx', 'csv', 'unknown'. Prefers the mime,
    falls back to extension, then magic-byte sniff."""
    name = (filename or "").lower()
    sniffed = mime or _sniff_mime(data)
    if sniffed == PDF_MIME or name.endswith(".pdf"):
        return "pdf"
    if sniffed == XLSX_MIME or name.endswith((".xlsx", ".xlsm")):
        return "xlsx"
    if sniffed == CSV_MIME or name.endswith((".csv", ".tsv")):
        return "csv"
    return "unknown"


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS
# ════════════════════════════════════════════════════════════════════════════

def extract_spec(data: bytes, filename: str, mime: str | None = None, *,
                 spec_sheet_id: str | None = None) -> dict:
    """Extract a leather spec sheet into the legacy intermediate dict the service
    layer reads. Never raises — failures surface as `manual_entry_required` in
    the returned warnings list.

    Routing:
      PDF (digital text)    -> text LLM
      PDF (scanned/image)   -> vision LLM (Gemini), no Groq fallback
      XLSX / XLSM           -> markdown -> text LLM
      CSV / TSV             -> text -> text LLM
      everything else       -> manual_entry_required"""
    spec: ExtractedSpec | None = None
    try:
        kind = _resolve_kind(data, filename, mime)

        if kind == "pdf":
            payload_kind, payload = pdf_to_llm_input(data)
            if not payload:
                spec = _empty_spec("pdf_unreadable")
            elif (payload_kind == "images"
                  and not getattr(settings, "vision_classifier_enabled", True)):
                spec = _empty_spec("vision_disabled_for_scanned_pdf")
            else:
                spec = llm_extract_spec(payload_kind, payload) \
                    or _empty_spec("llm_extraction_failed")

        elif kind == "xlsx":
            markdown = xlsx_to_markdown(data)
            if not markdown.strip():
                spec = _empty_spec("xlsx_unreadable")
            else:
                spec = llm_extract_spec("text", markdown) \
                    or _empty_spec("llm_extraction_failed")

        elif kind == "csv":
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:                                   # noqa: BLE001
                spec = _empty_spec("csv_unreadable")
            else:
                spec = llm_extract_spec("text", text) \
                    or _empty_spec("llm_extraction_failed")

        else:
            spec = _empty_spec(f"unsupported_file_type: mime={mime!r} name={filename!r}")

    except Exception as exc:                                    # noqa: BLE001
        logger.exception("extract_spec degraded for %s (%s): %s", filename, mime, exc)
        spec = _empty_spec(f"extract_spec_exception: {type(exc).__name__}")

    return _spec_to_intermediate(spec, spec_sheet_id)


def extract_order(data: bytes, filename: str, mime: str | None = None,
                  client_match_code: str | None = None) -> dict:
    """Extract a leather order sheet into the legacy intermediate dict the service
    layer reads. Never raises — failures surface as `manual_entry_required`."""
    logger.info("calling extract_order with filename=%r mime=%r client_match_code=%r "
                "data_length=%d", filename, mime, client_match_code, len(data))
    order: ExtractedOrder | None = None
    try:
        kind = _resolve_kind(data, filename, mime)

        if kind == "pdf":
            payload_kind, payload = pdf_to_llm_input(data)
            if not payload:
                order = _empty_order("pdf_unreadable")
            elif (payload_kind == "images"
                  and not getattr(settings, "vision_classifier_enabled", True)):
                order = _empty_order("vision_disabled_for_scanned_pdf")
            else:
                order = llm_extract_order(payload_kind, payload) \
                    or _empty_order("llm_extraction_failed")

        elif kind == "xlsx":
            markdown = xlsx_to_markdown(data)
            if not markdown.strip():
                order = _empty_order("xlsx_unreadable")
            else:
                order = llm_extract_order("text", markdown) \
                    or _empty_order("llm_extraction_failed")

        elif kind == "csv":
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:                                   # noqa: BLE001
                order = _empty_order("csv_unreadable")
            else:
                order = llm_extract_order("text", text) \
                    or _empty_order("llm_extraction_failed")

        else:
            order = _empty_order(f"unsupported_file_type: mime={mime!r} name={filename!r}")

    except Exception as exc:                                    # noqa: BLE001
        logger.exception("extract_order degraded for %s (%s): %s", filename, mime, exc)
        order = _empty_order(f"extract_order_exception: {type(exc).__name__}")

    return _order_to_intermediate(order, client_match_code)


# ════════════════════════════════════════════════════════════════════════════
# LEGACY INTERMEDIATE SHIMS
# The service layer reads specific keys (poms, attributes, sizes, unresolved,
# spec_type, customer_ref, etc.). These shims preserve that contract so the
# existing service code keeps working unchanged. When the staging-table
# migration lands, these can go away.
# ════════════════════════════════════════════════════════════════════════════

def _spec_to_intermediate(spec: ExtractedSpec, spec_sheet_id: str | None) -> dict:
    """Convert ExtractedSpec to the legacy intermediate dict the service reads."""
    d = spec.model_dump(mode="json")

    # POMs in the legacy shape (the service iterates these to write pom_measurement)
    d["poms"] = [
        {
            "source_term": m.source_term,
            "by_size": m.by_size,
            "pitch": m.pitch,
            "extracted_by": m.extracted_by,
            "confidence": m.confidence,
        }
        for m in spec.measurements
    ]

    # Flat attributes dict (legacy service reads leather_quality, primary_color,
    # lining, accessories from here when building line seeds)
    d["attributes"] = _legacy_attributes(spec)

    d["sizes"] = list(spec.sizes)
    d["unresolved"] = []                                        # filled by service after pom_dictionary lookup
    d["spec_type"] = "unknown"                                  # legacy field; classification moved out
    if spec_sheet_id is not None:
        d["spec_sheet_id"] = spec_sheet_id
    return d


def _legacy_attributes(spec: ExtractedSpec) -> dict:
    """Flatten typed spec fields into the unstructured attributes dict the service
    layer's _build_line_seeds reads. New code should read the typed fields directly."""
    attrs: dict[str, Any] = dict(spec.materials or {})

    if spec.color_details:
        primary = spec.color_details.get("primary")
        if primary:
            attrs["primary_color"] = primary

    lining = spec.lining or {}
    if lining.get("lined") is False:
        attrs["lining"] = "unlined"
    elif lining.get("details"):
        attrs["lining"] = lining["details"]
    elif lining.get("lined") is True:
        attrs["lining"] = "lined"

    if spec.accessories:
        attrs["accessories"] = [a.model_dump() for a in spec.accessories]

    return attrs


def _order_to_intermediate(order: ExtractedOrder, client_match_code: str | None) -> dict:
    """Convert ExtractedOrder to the legacy intermediate dict the service reads."""
    d = order.model_dump(mode="json")

    # Legacy aliases the service expects
    d["style_name"] = order.style_no
    d["unit_price"] = order.price_per_garment
    d["customer_ref"] = None                                    # not extracted from orders today
    d["internal_ref"] = None
    if client_match_code is not None:
        d["client_match_code"] = client_match_code

    return d


# ════════════════════════════════════════════════════════════════════════════
# VALIDATION (called by service.py after intermediate is built)
# ════════════════════════════════════════════════════════════════════════════

def validate_intermediate(d: dict) -> None:
    """Structural sanity + range warnings on the intermediate. Raises
    ExtractionError on structural defects (missing keys, malformed rows).
    Range / coherence issues are appended to d['warnings'], never raised."""
    for key in ("poms", "sizes"):
        if key not in d:
            raise ExtractionError(f"intermediate result missing '{key}'")
    if not isinstance(d.get("warnings"), list):
        d["warnings"] = []

    declared = set(d.get("sizes") or [])
    for pom in d.get("poms") or []:
        if not isinstance(pom, dict):
            raise ExtractionError(f"pom row not a dict: {pom!r}")
        term = pom.get("source_term") or "?"
        for size, val in (pom.get("by_size") or {}).items():
            if not isinstance(val, (int, float)) or val <= 0:
                raise ExtractionError(f"pom {term!r} size {size} not positive")
            if val < 1 or val > 300:
                d["warnings"].append(f"measurement_out_of_range: {term} {size}={val}")
            if declared and size not in declared:
                d["warnings"].append(f"size_not_in_declared_set: {size}")