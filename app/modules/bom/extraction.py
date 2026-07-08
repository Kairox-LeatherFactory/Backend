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

  TEXT payloads (xlsx/xls/csv markdown, digital-PDF text layer):
    Gemini text (retried) -> Groq text (retried) -> [PDF only: native-PDF rung]
    -> manual_entry_required

  SCANNED PDFs (no usable text layer):
    NATIVE PDF -> Gemini (retried) -> manual_entry_required

NATIVE-PDF TRANSPORT (the F8 fix — 2026-07)
    The old scanned-PDF path rasterised every page to PNG and inlined them all
    as base64 image blocks in ONE langchain message. Base64 inflates bytes
    ~33%, 200-DPI PNGs are large, and multi-page orders compounded it — the
    request routinely blew Gemini's server-side deadline (504 DeadlineExceeded)
    and there was no further rung, so dense scanned orders silently fell to
    manual. The proven fix (validated by the standalone extract_document.py):
    send the RAW PDF BYTES as a single native document part and let Gemini read
    text, tables, and scanned pages itself — no rasterisation, no inline
    images, tiny request. `pdf_render_pages` is no longer used by extraction
    (it remains in procurement/sniffing for document CLASSIFICATION only).

If every rung is unavailable or exhausted, the public functions return an
empty result with `extracted_by="manual"` and the warning
`manual_entry_required`. The caller (service layer) sees this and either
prompts the cutting manager to enter data manually, or holds the BOM in a
"pending extraction" state for retry.

TYPED CONTRACT (the #3/#4 change)
    The public entry points return the Pydantic models DIRECTLY
    (ExtractedSpec / ExtractedOrder) — the legacy intermediate-dict shim is gone.
    The service layer consumes the typed objects, does its own native-term ->
    pom_code resolution and attribute flattening, and writes the staging rows
    from `model.model_dump(mode="json")`. Range/coherence warnings live in the
    contract (ExtractedSpec validators).

ARCHITECTURE (CLAUDE.md — unchanged posture)
  * PURE + SYNC. model.invoke() / generate_content() are blocking; the async
    service threadpools the whole call. No DB session in this module. (The
    retry time.sleep() blocks the threadpool worker, not the event loop —
    fine at our concurrency.)
  * No path EVER raises. Failures -> a valid ExtractedSpec/ExtractedOrder with
    `warnings=["...reason...", "manual_entry_required"]`.
  * POM-dictionary term resolution + garment_type mapping happen in the SERVICE
    (DB-touching), NOT here. Extraction emits NATIVE terms (no pom_code).
  * Reuses classifier._init_model / classifier._coerce and the existing settings.

DEPENDENCIES (requirements.txt)
    google-genai>=1.0        # NEW — native-PDF transport (separate package from
                             # langchain-google-genai; both coexist fine)

FUNCTION GUIDE
  extract_spec(data, filename, mime) -> ExtractedSpec     PUBLIC entry. Never raises.
  extract_order(data, filename, mime, client_match_code=None) -> ExtractedOrder PUBLIC.
  llm_extract_spec(kind, payload) -> ExtractedSpec | None  kind: "text"|"pdf". Never raises.
  llm_extract_order(kind, payload) -> ExtractedOrder | None Same contract.
  _gemini_native_pdf(data, prompt) -> str | None           The F8 transport, retried.
================================================================================
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.core.config import settings
from app.modules.bom.excel_content import xlsx_to_markdown, xls_to_markdown
from app.modules.bom.extraction_schemas import (
    ExtractedOrder,
    ExtractedOrderDoc,
    ExtractedSpec,
    StyleBreakdown,
    StyleColorBreakdown,
    is_empty_order,
    is_empty_spec,
)
from app.modules.bom.pdf_content import pdf_extract_text
import re as _re
import unicodedata as _ud
from collections import OrderedDict

logger = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"
PDF_MIME = "application/pdf"

# Per-rung retry policy. Blanket retry (we don't classify retryable vs fatal
# exceptions because the langchain/Gemini/Groq error taxonomy isn't stable) — kept
# cheap so a guaranteed failure (auth) costs ~1.5s, not minutes.
_LLM_RETRY_ATTEMPTS = 3
_LLM_RETRY_BASE_DELAY = 0.5  # seconds; exponential: 0.5, 1.0, 2.0

# Native-PDF transport: inline the PDF bytes as a document part up to this size;
# above it, use the Files API (upload once, reference by uri) — same behaviour
# as the proven standalone extract_document.py. Gemini's inline-part ceiling is
# 20 MB; we stop a little short of it.
_INLINE_PDF_MAX_BYTES = 19 * 1024 * 1024


_SIZE_RE = _re.compile(r"^(\d{1,3}|XXS|XS|S|M|L|XL|XXL|XXXL|[2-6]XL)$")
_DITTO = {'"', "''", "〃", "same", "SAME"}


def is_size_token(h: str) -> bool:
    """True iff a header is a real size label — never 'PREZZO', 'TOTALE CAPI',
    'COLORE'. Server-side belt-and-braces on whatever the LLM returns; without
    it, price/total columns inflate quantities (proven failure on real data)."""
    return bool(_SIZE_RE.match((h or "").strip().upper()))


def normalize_style(s: str | None) -> str:
    """Grouping key for a style: NFKC, trim, collapse whitespace, uppercase.
    Deterministic; never merges near-duplicates (those are flagged instead)."""
    if not s:
        return ""
    s = _ud.normalize("NFKC", str(s)).strip()
    return _re.sub(r"\s+", " ", s).upper()


def normalize_color(c: str | None) -> str:
    """Color key = the leading CODE token when present ('06 DARK BROWN' -> '06'),
    else the whole normalised label. John Peter names a color fully once and
    reuses the bare code in later blocks — keying on the code merges them."""
    if not c:
        return ""
    c = normalize_style(c)
    first = c.split(" ")[0]
    return first if _re.match(r"^\d+[A-Z]?$", first) else c


def _dup_key(s: str) -> str:
    """Aggressive key used ONLY to FLAG suspected duplicate styles, never to
    merge them: folds DET/DETACH/DETACHABLE/FOR DET, strips non-alnum."""
    s = normalize_style(s)
    s = _re.sub(r"\b(DETACHABLE|DETACH|FOR DET|DET)\b", "DET", s)
    return _re.sub(r"[^A-Z0-9]", "", s)


def split_styles_colors(raw_lines: list[dict], header: dict | None = None,
                        engine: str = "gemini") -> ExtractedOrderDoc:
    """Deterministic grouping of the LLM's per-line output into
    style -> color -> per-size quantities. The LLM parses the grid (it
    generalises across client templates); this function only groups — but it
    inherits dittos DEFENSIVELY in case any leak past the prompt, filters size
    tokens, aggregates the same style/color across order blocks, keeps the
    richest color label, recomputes every total, cross-checks each row against
    its printed_total, and flags (never merges) near-duplicate style names."""
    header = header or {}
    styles: "OrderedDict[str, dict]" = OrderedDict()
    doc_warnings: list[str] = []
    prev_model: str | None = None
    prev_material: str | None = None

    for ln in raw_lines or []:
        m = (ln.get("model") or "").strip()
        model = prev_model if (m in _DITTO or not m) else normalize_style(m)
        if not model:
            continue
        prev_model = model
        mat = (ln.get("material") or ln.get("article") or "").strip()
        material = prev_material if (mat in _DITTO or not mat) else mat
        prev_material = material

        sizes = {k.strip().upper(): int(v)
                 for k, v in (ln.get("sizes") or {}).items()
                 if is_size_token(k) and isinstance(v, (int, float)) and v > 0}
        if not sizes:
            continue
        row_qty = sum(sizes.values())

        st = styles.setdefault(model, {
            "material": material, "qty": 0,
            "per_size": {}, "colors": OrderedDict(), "warnings": []})
        if not st["material"] and material:
            st["material"] = material

        ckey = normalize_color(ln.get("color"))
        raw_label = (ln.get("color") or "").strip()
        col = st["colors"].setdefault(ckey, {
            "label": raw_label, "per_size": {}, "qty": 0, "warnings": []})
        if len(raw_label) > len(col["label"]):
            col["label"] = raw_label                       # '06' -> '06 DARK BROWN'

        for sz, q in sizes.items():
            col["per_size"][sz] = col["per_size"].get(sz, 0) + q
            st["per_size"][sz] = st["per_size"].get(sz, 0) + q
        col["qty"] += row_qty
        st["qty"] += row_qty

        # Printed-total cross-check: dense grids can drop a size cell during
        # extraction; the printed row total catches it. WARN, never overwrite.
        pt = ln.get("printed_total")
        if isinstance(pt, (int, float)) and int(pt) != row_qty:
            col["warnings"].append(
                f"row_total_mismatch: sizes_sum={row_qty} printed={int(pt)}")

    # Near-duplicate style names: flag for the operator, never auto-merge —
    # a wrong merge corrupts quantities invisibly; a wrong split is visible.
    dup: dict[str, list[str]] = {}
    for k in styles:
        dup.setdefault(_dup_key(k), []).append(k)
    for group in dup.values():
        if len(group) > 1:
            doc_warnings.append("possible_duplicate_style:" + "|".join(group))
            for k in group:
                styles[k]["warnings"].append(
                    "possible_duplicate_of:" + "|".join(x for x in group if x != k))

    return ExtractedOrderDoc(
        order_number=header.get("order_number"),
        client_name=header.get("client_name"),
        season=header.get("season"),
        currency=header.get("currency"),
        delivery_date=header.get("delivery_date"),
        payment_term=header.get("payment_term"),
        extracted_by=engine,
        warnings=doc_warnings,
        styles=[StyleBreakdown(
            style_key=k, material=s["material"], qty=s["qty"],
            per_size_qty=s["per_size"], warnings=s["warnings"],
            colors=[StyleColorBreakdown(
                color_key=ck, color_label=c["label"], per_size_qty=c["per_size"],
                qty=c["qty"], warnings=c["warnings"])
                for ck, c in s["colors"].items()],
        ) for k, s in styles.items()],
    )

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
  - lines: list of {model, color, article, sizes {size: qty}, printed_total}
  - `model` is the style name from the Modello/Model/Style column — REQUIRED on
    EVERY line. Ditto marks (", '', 〃) or a blank cell mean "same as the line
    above": RESOLVE them and write the actual style name, never the ditto mark.
  - Same rule for the material column: resolve dittos to the actual material.
  - One line per (style, color) ROW as printed. The same style repeated in a
    later order block on a later page is STILL that style — keep its name exact.
  - `printed_total` is the row's printed total-pieces number (TOTALE CAPI or
    similar) if the row has one; omit it otherwise. Do NOT use it to fill sizes.
  - sizes may use alpha codes (S, M, L, XL, XXL) or numeric (38, 40, 42...).
    ONLY size columns go in sizes — never price, total, or note columns.

Do NOT trust any printed GRAND TOTAL. It is recomputed downstream from your
line data; just give the lines accurately.

Output ONLY valid JSON. No prose, no markdown fences.
"""


# ════════════════════════════════════════════════════════════════════════════
# LLM PLUMBING
# ════════════════════════════════════════════════════════════════════════════

def _build_text_content(payload: Any, prompt: str) -> list[dict]:
    """Assemble the langchain message for a TEXT payload: prompt + text block.
    (The old image_url/base64 branch is gone — scanned PDFs now go through the
    native-PDF transport, never through inline page images.)"""
    return [
        {"type": "text", "text": prompt},
        {"type": "text", "text": str(payload)},
    ]


def _invoke_with_retry(model, message, label: str, *,
                       attempts: int = _LLM_RETRY_ATTEMPTS,
                       base_delay: float = _LLM_RETRY_BASE_DELAY):
    """Invoke a single langchain model with bounded exponential-backoff retry.
    Returns the raw response on success or None when every attempt failed.
    Never raises — a failure here just means the caller falls to the next rung."""
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


def _gemini_native_pdf(data: bytes, prompt: str, *,
                       attempts: int = _LLM_RETRY_ATTEMPTS,
                       base_delay: float = _LLM_RETRY_BASE_DELAY) -> str | None:
    """THE F8 TRANSPORT. Send the raw PDF to Gemini as a single native document
    part — no rasterisation, no inline base64 page images. Gemini reads text,
    tables, and scanned/image pages itself in one pass, so multi-page scanned
    orders no longer build the oversized payload that caused 504s.

    Proven by the standalone extract_document.py against real client PDFs; this
    keeps its exact generation config (temperature=0, top_p=0.1) and adds
    response_mime_type="application/json" because the pipeline parses typed
    JSON, not markdown.

    <= ~19 MB inlines via Part.from_bytes; larger PDFs upload once via the
    Files API and are referenced by uri. Returns the raw response text, or
    None when the SDK is missing / no key / all attempts failed. Never raises."""
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError:
        logger.warning("google-genai not importable — native-PDF rung unavailable "
                       "(pip install google-genai)")
        return None

    api_key = settings.gemini_api_key
    if not api_key:
        logger.warning("GEMINI_API_KEY not configured — native-PDF rung unavailable")
        return None

    # vision_model spec is "gemini:<model>"; the native SDK wants the bare model id.
    spec = settings.vision_model or settings.extraction_model
    model_id = spec.partition(":")[2] or spec

    try:
        client = genai.Client(api_key=api_key)
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("google-genai client init failed: %s", exc)
        return None

    uploaded = None
    for attempt in range(1, attempts + 1):
        try:
            if len(data) <= _INLINE_PDF_MAX_BYTES:
                doc_part = gtypes.Part.from_bytes(data=data, mime_type=PDF_MIME)
            else:
                if uploaded is None:                            # upload once, reuse on retry
                    import io
                    uploaded = client.files.upload(
                        file=io.BytesIO(data),
                        config={"mime_type": PDF_MIME},
                    )
                doc_part = uploaded

            resp = client.models.generate_content(
                model=model_id,
                contents=[doc_part, prompt],
                config=gtypes.GenerateContentConfig(
                    temperature=0,
                    top_p=0.1,
                    response_mime_type="application/json",
                ),
            )
            text = getattr(resp, "text", None)
            if text:
                return text
            logger.warning("native-PDF (%s) attempt %d/%d returned empty text",
                           model_id, attempt, attempts)
        except Exception as exc:                                # noqa: BLE001
            logger.warning("native-PDF (%s) attempt %d/%d failed: %s",
                           model_id, attempt, attempts, exc)
        if attempt < attempts:
            time.sleep(base_delay * (2 ** (attempt - 1)))

    logger.warning("native-PDF (%s) failed after %d attempts", model_id, attempts)
    return None


def _llm_invoke(kind: str, payload: Any, prompt: str) -> tuple[str, str] | None:
    """Invoke the configured LLM for the given payload kind.

      kind == "text": langchain ladder — Gemini text (retried) -> Groq (retried).
      kind == "pdf":  native-PDF transport — raw PDF bytes to Gemini (retried).
                      No Groq rung (Groq has no document/vision input).

    Returns (engine_name, raw_response) on success, None when no model is
    available or every rung exhausted its retries. Never raises."""
    if kind == "pdf":
        raw = _gemini_native_pdf(payload, prompt)
        return ("gemini", raw) if raw is not None else None

    try:
        from langchain_core.messages import HumanMessage
    except ImportError:
        logger.warning("langchain_core not importable — extraction cannot use LLM")
        return None

    from app.modules.procurement.classifier import _init_model

    message = HumanMessage(content=_build_text_content(payload, prompt))

    primary_spec = settings.extraction_model
    primary = _init_model(primary_spec)
    if primary is not None:
        resp = _invoke_with_retry(primary, message, primary_spec)
        if resp is not None:
            engine = "gemini" if "gemini" in primary_spec.lower() else "groq"
            return engine, str(getattr(resp, "content", resp))

    fb_spec = settings.extraction_fallback_model
    fallback = _init_model(fb_spec)
    if fallback is not None:
        resp = _invoke_with_retry(fallback, message, fb_spec)
        if resp is not None:
            engine = "groq" if "groq" in fb_spec.lower() else "gemini"
            return engine, str(getattr(resp, "content", resp))

    return None


def llm_extract_spec(kind: str, payload: Any) -> ExtractedSpec | None:
    """LLM spec extraction. kind: "text" (markdown/csv/text-layer) or "pdf"
    (raw PDF bytes -> native transport). Returns None when no LLM is available,
    the response is unparseable, schema validation fails, or the result is empty
    (no usable fields). The caller treats None as "fall through". Never raises."""
    from app.modules.procurement.classifier import _coerce
    result = _llm_invoke(kind, payload, _SPEC_PROMPT)
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
    result = _llm_invoke(kind, payload, _ORDER_PROMPT)
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


def _extract_pdf(data: bytes, llm_fn, empty_fn):
    """Shared PDF ladder for spec + order:

        digital text layer -> text rung (Gemini text -> Groq)   [cheap, has fallback]
                           -> native-PDF rescue on failure
        scanned (no layer) -> native-PDF rung directly

    llm_fn(kind, payload) -> typed model | None; empty_fn(reason) -> empty typed
    result. Never raises."""
    text = pdf_extract_text(data)
    if text:
        result = llm_fn("text", text)
        if result is not None:
            return result
        # Digital PDF but the text rung produced nothing usable (layout lost in
        # the text layer, or both text models down) — the native transport sees
        # the real page layout, so rescue through it before giving up.
        native = llm_fn("pdf", data)
        if native is not None:
            native.warnings = list(native.warnings) + ["text_rung_failed_native_pdf_used"]
            return native
        return empty_fn("llm_extraction_failed")

    # Scanned / image-only PDF: native transport is the primary (and only) rung.
    if not getattr(settings, "vision_classifier_enabled", True):
        return empty_fn("vision_disabled_for_scanned_pdf")
    native = llm_fn("pdf", data)
    if native is not None:
        return native
    return empty_fn("native_pdf_extraction_failed")


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS — return the typed contract directly.
# ════════════════════════════════════════════════════════════════════════════

def extract_spec(data: bytes, filename: str, mime: str | None = None) -> ExtractedSpec:
    """Extract a leather spec sheet into an ExtractedSpec. Never raises — failures
    surface as `manual_entry_required` in `warnings`.

    Routing:
      PDF (digital text)    -> text LLM (Gemini -> Groq), native-PDF rescue
      PDF (scanned/image)   -> NATIVE PDF -> Gemini (no rasterised images)
      XLSX / XLSM / XLS     -> markdown -> text LLM
      CSV / TSV             -> text -> text LLM
      everything else       -> manual_entry_required"""
    logger.info("calling extract_spec with filename=%r mime=%r data_length=%d",
                filename, mime, len(data))
    try:
        kind = _resolve_kind(data, filename, mime)

        if kind == "pdf":
            return _extract_pdf(data, llm_extract_spec, _empty_spec)

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
            return _extract_pdf(data, llm_extract_order, _empty_order)

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
    
def extract_order_doc(data: bytes, filename: str,
                      mime: str | None = None) -> ExtractedOrderDoc:
    """Multi-style order extraction: same routing/transport ladder as
    extract_order (xlsx/xls -> markdown -> text rung; PDF -> text rung or
    native-PDF rung), then deterministic style/color grouping. Never raises."""
    order = extract_order(data, filename, mime)            # existing ladder, reused
    if is_empty_order(order):
        return ExtractedOrderDoc(extracted_by="manual", warnings=list(order.warnings))
    header = {"order_number": order.order_number, "client_name": order.client_name,
              "season": order.season, "currency": order.currency,
              "delivery_date": order.delivery_date, "payment_term": order.payment_term}
    doc = split_styles_colors([l.model_dump() for l in order.lines],
                              header=header, engine=order.extracted_by)
    if not doc.styles:                                     # lines had no model column
        doc.warnings.append("no_styles_parsed_single_style_assumed")
        doc.styles = [StyleBreakdown(
            style_key=normalize_style(order.style_no) or "UNKNOWN",
            material=order.article, qty=order.order_qty or 0,
            per_size_qty=order.per_size_qty or {},
            colors=[], warnings=["style_from_header_not_lines"])]
    return doc