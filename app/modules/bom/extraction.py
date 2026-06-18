"""
================================================================================
modules/bom/extraction.py — POM/attribute/pattern extraction (Stage 2 §1/§4)
================================================================================

The config-driven extraction engine. One GENERIC pipeline; the per-client
behaviour is the adapter config in config/extraction_adapters.yaml selected off
(spec_type, client_match_code) — onboarding a client is a config entry, not a code
branch (§4, acceptance §11.6).

This module is PURE + SYNC (no DB session, no async) so the service can push it
through run_in_threadpool — the same posture as sniffing.py / pipeline.py. The
async service loads the POM-dictionary rows and persists the result.

POLICY (stage-0 §4 carried straight through): NO LLM unless required. The Beau
Geste measurement grid is parsed deterministically with openpyxl (machine-readable
→ no Gemini call). Only the Jackiee prose tech pack spends an LLM call — and even
that degrades to a deterministic best-effort prose parse when no provider key is
configured, so a model outage never stalls extraction.

The four adapter steps (§4a) — detect / extract_poms / extract_attributes /
resolve_pattern_ref — emit the §1a INTERMEDIATE JSON, validated before any row is
written (validate_intermediate).

FUNCTION GUIDE  (extract_spec is the entry point; BomService.generate_bom calls it
in a threadpool)
  load_adapters(path?) -> tuple        [lru_cached] read+cache config/extraction_adapters.yaml.
  select_adapter(spec_type, client_match_code, adapters?) -> dict
      Pick the adapter owning this sheet: exact (client, spec_type) → any for client →
      `_generic`. CALLED FROM: generate_bom (step 1).
  _norm_term(t)                        [private] strip+lowercase a POM label for matching.
  PomDict(rows)                        a session-free term→pom_code map built from the DB rows.
      .resolve(term, language) -> pom_code | None   tries language-specific then any-language.
  _col_idx(letter) / _num(v)           [private] spreadsheet helpers (A→1; cell→float|None).
  extract_poms_grid(wb, adapter, pom_dict) -> (poms, unresolved)
      Deterministic openpyxl parse of the numbered measurement block (NO LLM). Unmapped
      terms go to `unresolved` (never dropped). CALLED FROM: extract_spec when poms.strategy=deterministic.
  parse_techpack_fields(wb) -> {FIELD: prose}   3-column field→value sheet → a flat map.
  _prose_attributes(fields) -> dict    [private] regex the high-value facts (leather/colour/SMS) — the no-LLM fallback.
  extract_attributes_techpack(fields, garment_type, extractor) -> dict
      LLM attributes when an extractor is available, MERGED over the deterministic base.
  detect_pattern_ref(adapter, *, fields?, grid_text?) -> {pattern_code, base_size, source_term} | None
      Resolve "follow pattern X in size Y" via a per-adapter regex — NOT new POMs.
  extract_spec(data, filename, adapter, pom_dict, *, spec_sheet_id?, extractor?) -> dict
      THE ORCHESTRATOR. Loads the workbook, runs the four steps, builds the §1a JSON,
      validates it, returns it. This is what the service threadpools.
  _size_sort_key(s)                    [private] order sizes XS<S<M<...< numeric.
  ExtractionError                      raised when the intermediate JSON fails schema validation.
  validate_intermediate(d) -> None     enforce the fixed schema before any row is written.
  build_default_extractor() -> AttributeExtractor | None
      Build the real Gemini→Groq attribute extractor (or None if no key set → prose parse).
      The returned closure invokes primary→fallback and coerces JSON.
================================================================================
"""
from __future__ import annotations

import io
import logging
import os
import re
from functools import lru_cache
from typing import Callable

import yaml

from app.core.config import settings
from app.core.enums import SpecType
from app.modules.bom.enums import ExtractionSource

# An attribute extractor maps (techpack fields, garment_type) → the §1d attributes
# dict, or None when no model is available (then we fall back to a prose parse).
AttributeExtractor = Callable[[dict, "str | None"], "dict | None"]

logger = logging.getLogger(__name__)

GENERIC_CODE = "_generic"

_ADAPTERS_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "config", "extraction_adapters.yaml",
)


# ── adapter registry (cached config, no DB) ─────────────────────────────────
@lru_cache(maxsize=1)
def load_adapters(path: str | None = None) -> tuple:
    with open(path or _ADAPTERS_YAML, encoding="utf-8") as fh:
        return tuple(yaml.safe_load(fh) or [])


def select_adapter(spec_type: str, client_match_code: str | None,
                   adapters: list | tuple | None = None) -> dict:
    """Pick the adapter owning this sheet: an exact (client, spec_type) match wins;
    else the `_generic` adapter (LLM-only, stricter floor). Mirrors registry.best_match."""
    adapters = adapters if adapters is not None else load_adapters()
    code = client_match_code or GENERIC_CODE
    for a in adapters:
        if a.get("client_code") == code and a.get("spec_type") == spec_type:
            return a
    # fall back to any adapter for this client, then generic
    for a in adapters:
        if a.get("client_code") == code:
            return a
    for a in adapters:
        if a.get("client_code") == GENERIC_CODE:
            return a
    return {"client_code": GENERIC_CODE, "spec_type": spec_type,
            "poms": {"strategy": "none"}, "attributes": {"strategy": "llm"}}


# ── POM-dictionary view (session-free) ──────────────────────────────────────
def _norm_term(t: str) -> str:
    return re.sub(r"\s+", "", str(t)).strip().lower()


class PomDict:
    """A session-free term→code map built by the service from `pom_dictionary` rows.
    Lookup tries the adapter's language first, then any language (a JP-only sheet
    whose term also exists under `en` still resolves)."""

    def __init__(self, rows: list[tuple[str, str, str]]):
        # rows: (language, source_term, pom_code)
        self._by_lang_term: dict[tuple[str, str], str] = {}
        self._by_term: dict[str, str] = {}
        for lang, term, code in rows:
            key = _norm_term(term)
            self._by_lang_term[(lang, key)] = code
            self._by_term.setdefault(key, code)

    def resolve(self, term: str, language: str | None) -> str | None:
        key = _norm_term(term)
        if language and (language, key) in self._by_lang_term:
            return self._by_lang_term[(language, key)]
        return self._by_term.get(key)


# ── grid POM extraction (Beau Geste — deterministic openpyxl, NO LLM) ───────
def _col_idx(letter: str) -> int:
    """'A'->1, 'B'->2, ... 'AA'->27 (openpyxl 1-based). used to get the column number based on the cloumn value"""
    n = 0
    for ch in letter.strip().upper():
        n = n * 26 + (ord(ch) - 64)
    return n
 

def _num(v) -> float | None:
    """Coerce a cell to a number. Tolerates string-typed numerics ('5') and pulls the
    leading value out of a grading note ('0/5', '1/2/0〜' → None unless it starts numeric)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return None
    m = re.match(r'\s*(-?\d+(?:[.,]\d+)?)\s*$', str(v))
    return float(m.group(1).replace(",", ".")) if m else None


def extract_poms_grid(wb, adapter: dict, pom_dict: PomDict) -> tuple[list[dict], list[dict]]:
    """Deterministic parse of the numbered 規格寸法 block. Returns (poms, unresolved).
    Each pom: {pom_code, source_term, by_size, pitch, extracted_by, confidence}."""
    cfg = adapter.get("poms", {})
    language = adapter.get("language")
    sheet_name = adapter.get("sheet_name")
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb[wb.sheetnames[0]]

    term_c = _col_idx(cfg["term_col"])
    pitch_c = _col_idx(cfg["pitch_col"]) if cfg.get("pitch_col") else None
    size_cols = {sz: _col_idx(col) for sz, col in (cfg.get("size_cols") or {}).items()}
    first, last = int(cfg["first_row"]), int(cfg["last_row"])

    poms: list[dict] = []
    unresolved: list[dict] = []
    for r in range(first, last + 1):
        term = ws.cell(r, term_c).value
        if term is None or not str(term).strip():
            continue
        
        term = str(term).strip()
        by_size: dict[str, float] = {}
        for sz, c in size_cols.items():
            v = _num(ws.cell(r, c).value)
            if v is not None:
                by_size[sz] = v
        if not by_size:
            continue
        pitch = _num(ws.cell(r, pitch_c).value) if pitch_c is not None else None
        code = pom_dict.resolve(term, language)
        if code is None:
            unresolved.append({"source_term": term, "row": r})  # never dropped
            continue
        poms.append({
            "pom_code": code, "source_term": term, "by_size": by_size,
            "pitch": pitch, "extracted_by": ExtractionSource.DETERMINISTIC.value,
            "confidence": 0.99,
        })
    return poms, unresolved


# ── prose tech pack (Jackiee) → field map + attributes ──────────────────────
def parse_techpack_fields(wb) -> dict[str, str]:
    """A 3-column field→prose sheet → {FIELD_UPPER: joined prose}. The label sits in
    col A; col B/C carry the (often multi-line) value. Blank labels attach to the
    previous field (the real sheet wraps long instructions across rows)."""
    ws = wb[wb.sheetnames[0]]
    fields: dict[str, str] = {}
    last_key: str | None = None
    for row in ws.iter_rows(values_only=True):
        cells = [c for c in row]
        label = (str(cells[0]).strip() if cells and cells[0] is not None else "")
        rest = " ".join(str(c).strip() for c in cells[1:]
                        if c is not None and str(c).strip())
        if label:
            key = label.upper()
            fields[key] = (fields.get(key, "") + " " + rest).strip() if key in fields else rest
            last_key = key
        elif rest and last_key:
            fields[last_key] = (fields[last_key] + " " + rest).strip()
    return fields


_SUBSTANCE_RE = re.compile(r'(\d+[.,]\d+)\s*/\s*(\d+[.,]\d+)\s*mm', re.IGNORECASE)
_SMS_QTY_RE = re.compile(r'TOTAL\s+(\d+)\s+SMS', re.IGNORECASE)
_SMS_SIZE_RE = re.compile(r'SIZE\s+(\w+)', re.IGNORECASE)

# Common leather-garment trim keywords → canonical accessory type. The deterministic
# accessories floor emits {type, supplied_by?}; the LLM enriches each with
# placement/spec/finish (and replaces the whole list on merge when it has one).
_ACCESSORY_TYPES = {
    "zipper": "zipper", "zip": "zipper",
    "button": "button",
    "snap": "snap",
    "rivet": "rivet",
    "buckle": "buckle",
    "eyelet": "eyelet",
    "stud": "stud",
    "hook": "hook",
    "velcro": "velcro",
    "d-ring": "d-ring",
    "drawcord": "drawcord", "cord": "drawcord",
    "elastic": "elastic",
}
# Field labels that describe trims/hardware — searched in addition to the whole blob.
_ACCESSORY_FIELD_HINTS = ("ACCESSOR", "TRIM", "HARDWARE", "CLOSURE")
# "...supplied by factory/client/buyer" or "factory/client-supplied" → who supplies it.
_SUPPLIED_BY_RE = re.compile(
    r'(?:supplied\s+by\s+(factory|client|buyer)|(factory|client|buyer)[-\s]*supplied)',
    re.IGNORECASE,
)


def _supplied_by_near(text: str, start: int, end: int) -> str | None:
    """Who supplies the accessory matched at [start, end)? English puts the supplier
    clause AFTER the noun ("zipper supplied by factory"), so search forward first; fall
    back to a short backward window bounded at the previous comma so it can't bleed into
    the prior accessory's clause. Maps buyer→client (the buyer IS the client)."""
    fwd = text[end:end + 60]
    m = _SUPPLIED_BY_RE.search(fwd)
    if not m:
        back = text[max(0, start - 30):start].rsplit(",", 1)[-1]
        m = _SUPPLIED_BY_RE.search(back)
    if not m:
        return None
    who = (m.group(1) or m.group(2)).lower()
    return "factory" if who == "factory" else "client"


def _prose_accessories(fields: dict, blob: str) -> list[dict]:
    """Best-effort deterministic accessories list from the prose — {type, supplied_by?}
    per detected trim, deduped by type. Coarse on purpose (the LLM adds
    placement/spec/finish); returns [] when nothing matches."""
    haystack = blob
    for k, v in fields.items():
        if any(h in k for h in _ACCESSORY_FIELD_HINTS):
            haystack += " \n" + str(v)
    low = haystack.lower()
    accessories: list[dict] = []
    seen: set[str] = set()
    for kw, canon in _ACCESSORY_TYPES.items():
        if canon in seen:
            continue
        # word-boundary match (so "cord" ≠ "record"), tolerating a trailing plural "s".
        m = re.search(rf"(?<![a-z0-9]){re.escape(kw)}s?(?![a-z0-9])", low)
        if not m:
            continue
        entry: dict = {"type": canon}
        who = _supplied_by_near(haystack, m.start(), m.end())
        if who:
            entry["supplied_by"] = who
        accessories.append(entry)
        seen.add(canon)
    return accessories


def _prose_attributes(fields: dict) -> dict:
    """Deterministic best-effort attributes from the prose (the no-LLM fallback).
    Pulls the high-value, regex-able facts the §8 cross-check + costing need."""
    attrs: dict = {}
    blob = " \n".join(fields.values())
    if "LEATHER QUALITY" in fields:
        first = re.split(r"[,\n]", fields["LEATHER QUALITY"], maxsplit=1)[0]
        attrs["leather_quality"] = first.strip().lower()[:60]
    m = _SUBSTANCE_RE.search(blob)
    if m:
        attrs["leather_substance_mm"] = [float(m.group(1).replace(",", ".")),
                                         float(m.group(2).replace(",", "."))]
    if "COLOUR" in fields or "COLOR" in fields:
        raw = fields.get("COLOUR") or fields.get("COLOR") or ""
        attrs["primary_color"] = raw.split(",")[0].strip().upper()[:40]
    sms = fields.get("SMS ORDER", "")
    mq = _SMS_QTY_RE.search(sms)
    if mq:
        attrs["sms_qty"] = int(mq.group(1))
    ms = _SMS_SIZE_RE.search(sms)
    if ms:
        attrs["sms_size"] = ms.group(1)
    if "LINING" in " ".join(fields.keys()):
        for k in fields:
            if "LINING" in k:
                attrs["lining"] = fields[k][:120]
                break
    for k in fields:
        if "LABEL" in k or "BRANDING" in k:          # LABEL / LABELLING / LABELING / BRANDING
            attrs["labelling"] = str(fields[k]).strip()[:120]
            break
    accessories = _prose_accessories(fields, blob)
    if accessories:                                  # omit the key entirely when empty
        attrs["accessories"] = accessories
    return attrs


def extract_attributes_techpack(fields: dict, garment_type: str | None,
                                extractor: AttributeExtractor | None) -> dict:
    """Attributes for a prose tech pack. Uses the LLM extractor when available (the
    §1d structured output), else the deterministic prose parse. LLM output is MERGED
    over the deterministic base so a partial model answer never loses a parsed fact."""
    base = _prose_attributes(fields)
    if extractor is not None:
        try:
            llm = extractor(fields, garment_type)
        except Exception:
            llm = None
        if llm:
            base = {**base, **{k: v for k, v in llm.items() if v not in (None, "", [])}}
    return base


# ── pattern-reference detection (both clients) ──────────────────────────────
def detect_pattern_ref(adapter: dict, *, fields: dict | None = None,
                       grid_text: str | None = None) -> dict | None:
    """Resolve a "follow existing pattern X [in size Y]" phrase to a pattern_reference
    (§1c). A per-adapter regex over a designated field (PATTERN, 特記事項) — NOT parsed
    as new POMs. Returns {pattern_code, base_size, source_term} or None."""
    cfg = adapter.get("pattern_ref") or {}
    pat = cfg.get("pattern")
    if not pat:
        return None
    # choose the haystack: a named field (Jackiee PATTERN) or the whole grid text
    haystack = ""
    if cfg.get("field") and fields:
        haystack = fields.get(cfg["field"].upper(), "")
    if not haystack:
        haystack = grid_text or (" \n".join(fields.values()) if fields else "")
    if not haystack:
        return None
    m = re.search(pat, haystack, re.IGNORECASE)
    if not m:
        return None
    code = m.group(1).strip()
    base_size = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else None
    # the source phrase, trimmed for audit
    src = haystack[max(0, m.start() - 10): m.end() + 10].strip()
    return {"pattern_code": code, "base_size": base_size, "source_term": src}


# ── the orchestrator: bytes → §1a intermediate JSON ─────────────────────────
def extract_spec(data: bytes, filename: str, adapter: dict, pom_dict: PomDict, *,
                 spec_sheet_id: str | None = None,
                 extractor: AttributeExtractor | None = None) -> dict:
    """Run the adapter's four steps and emit the §1a intermediate JSON. Validated by
    validate_intermediate before the caller persists pom_measurement / attribute rows."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    spec_type = adapter.get("spec_type")
    garment_type = adapter.get("garment_type")
    pom_strategy = (adapter.get("poms") or {}).get("strategy", "none")

    poms: list[dict] = []
    unresolved: list[dict] = []
    fields: dict = {}
    grid_text = ""

    if pom_strategy == "deterministic":
        poms, unresolved = extract_poms_grid(wb, adapter, pom_dict)
        # gather a text blob (incl. 特記事項) for pattern-ref detection
        ws = wb[adapter["sheet_name"]] if adapter.get("sheet_name") in wb.sheetnames \
            else wb[wb.sheetnames[0]]
        grid_text = "\n".join(
            str(c) for row in ws.iter_rows(values_only=True) for c in row
            if c is not None and str(c).strip()
        )
    else:
        # prose / generic → field map drives attributes + pattern ref
        fields = parse_techpack_fields(wb)

    attributes = {}
    if spec_type == SpecType.NARRATIVE_TECHPACK.value or pom_strategy in ("none", "llm"):
        attributes = extract_attributes_techpack(fields, garment_type, extractor)

    pattern_reference = detect_pattern_ref(adapter, fields=fields, grid_text=grid_text)

    sizes = sorted({s for p in poms for s in p["by_size"]}, key=_size_sort_key) if poms else []
    if not sizes and attributes.get("sms_size"):
        sizes = [attributes["sms_size"]]

    wb.close()
    logger.info("extract_spec: file=%s spec_type=%s pom_strategy=%s → %d POMs, %d sizes, "
                "%d attrs, %d unresolved, pattern_ref=%s", filename, spec_type, pom_strategy,
                len(poms), len(sizes), len(attributes), len(unresolved),
                bool(pattern_reference))
    result = {
        "spec_sheet_id": spec_sheet_id,
        "spec_type": spec_type,
        "garment_type_guess": garment_type,
        "unit": "cm",
        "sizes": sizes,
        "poms": poms,
        "attributes": attributes,
        "pattern_reference": pattern_reference,
        "unresolved": unresolved,
    }
    validate_intermediate(result)
    return result


_SIZE_ORDER = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]


def _size_sort_key(s: str):
    su = str(s).upper()
    if su in _SIZE_ORDER:
        return (0, _SIZE_ORDER.index(su))
    try:
        return (1, float(s))
    except (ValueError, TypeError):
        return (2, su)


# ── intermediate-JSON validation (§1a: validated before any row is written) ──
class ExtractionError(ValueError):
    """The intermediate extraction JSON failed its fixed-schema validation."""


def validate_intermediate(d: dict) -> None:
    if not isinstance(d, dict):
        raise ExtractionError("intermediate result is not an object")
    for key in ("spec_type", "poms", "sizes", "unresolved"):
        if key not in d:
            raise ExtractionError(f"intermediate result missing '{key}'")
    if not isinstance(d["poms"], list):
        raise ExtractionError("'poms' must be a list")
    for p in d["poms"]:
        for k in ("pom_code", "by_size", "extracted_by"):
            if k not in p:
                raise ExtractionError(f"pom row missing '{k}'")
        if not isinstance(p["by_size"], dict) or not p["by_size"]:
            raise ExtractionError(f"pom {p.get('pom_code')!r} has no by_size values")
    pr = d.get("pattern_reference")
    if pr is not None and not pr.get("pattern_code"):
        raise ExtractionError("pattern_reference present but has no pattern_code")


# ── default LLM extractor (Gemini → Groq), mirrors classifier.build_default ──
_ATTR_SCHEMA_HINT = """Return ONLY a JSON object with this shape (omit unknown keys):
{ "leather_quality": "...", "leather_substance_mm": [0.45, 0.50],
  "primary_color": "...", "lining": "...",
  "accessories": [ {"type":"zipper","placement":"...","spec":"...","finish":"...",
                    "supplied_by":"factory|client"} ],
  "labelling": "...", "sms_qty": 0, "sms_size": "..." }"""


def build_default_extractor() -> AttributeExtractor | None:
    """Build the real Gemini→Groq attribute extractor, or None if neither key is set
    (extraction then uses the deterministic prose parse)."""
    from app.modules.procurement.classifier import _init_model

    primary = _init_model(settings.extraction_model)
    fallback = _init_model(settings.extraction_fallback_model)
    if primary is None and fallback is None:
        logger.info("no LLM attribute extractor (no Gemini/Groq key) → deterministic prose parse")
        return None

    def _extract(fields: dict, garment_type: str | None) -> dict | None:
        from app.modules.procurement.classifier import _coerce

        body = "\n".join(f"{k}: {v}" for k, v in fields.items())[:8000]
        prompt = (
            "You are extracting BOM-relevant attributes from a leather-garment tech "
            f"pack (garment type: {garment_type or 'unknown'}). Pull the leather "
            "quality/substance, colour, lining, accessories (with who supplies each), "
            "labelling, and the SMS quantity/size.\n\n"
            f"--- tech sheet ---\n{body}\n--- end ---\n\n" + _ATTR_SCHEMA_HINT
        )
        for label, model in (("gemini-primary", primary), ("groq-fallback", fallback)):
            if model is None:
                continue
            try:
                logger.info("attribute extraction via %s (garment=%s)", label, garment_type)
                resp = model.invoke(prompt)
                parsed = _coerce(getattr(resp, "content", "") or "")
                if parsed is not None:
                    logger.info("attribute extraction succeeded via %s (%d keys)",
                                label, len(parsed))
                    return parsed
                logger.warning("%s returned unparseable output → trying next rung", label)
            except Exception as exc:
                logger.warning("%s failed (%s) → falling back", label, exc)
                continue
        logger.warning("all LLM extractors failed → deterministic prose parse will be used")
        return None

    return _extract


# ════════════════════════════════════════════════════════════════════════════
# Order-sheet content extraction (Stage 2: order qty + per-size breakdown)
# ════════════════════════════════════════════════════════════════════════════
# The order sheet drives a BOM's order_qty / per-size breakdown + customer refs (the
# spec sheet drives materials/POMs). XLSX/CSV are parsed deterministically with the same
# "detect the header wherever it is, read whatever size columns it declares" approach as
# the legacy imports.parse_orders. The real client order sheets are SCANNED PDFs (no text
# layer — they need OCR/vision at classification time), so the PDF path is best-effort on
# whatever text pypdf can pull; anything unparsed is a non-blocking WARNING, never a
# reject (surface, don't reject — same posture as bom.checks). One submission = one
# style, so the lines are aggregated into a single style's per-(colour,size) breakdown.
# MIME constants are duplicated (not imported from procurement.sniffing) so this pure
# extraction module keeps no cross-module dependency.
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"
PDF_MIME = "application/pdf"
_ALPHA_SIZES = {"XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL", "4XL"}
_NON_SIZE_HEADERS = {
    "S.NO", "SNO", "STYLE", "COLOUR", "COLOR", "SUEDE COLOUR", "SUEDE COLOR", "ARTICLE",
    "TOTAL", "TOTAL QTY", "DATE", "SEASON", "ORDER", "ORDER NO", "REF", "CUSTOMER REF",
}


def _is_order_size_token(text) -> bool:
    if text is None:
        return False
    t = str(text).strip().upper()
    if not t or t in _NON_SIZE_HEADERS:
        return False
    if t in _ALPHA_SIZES:
        return True
    return t.replace(".", "").isdigit()      # "38", "46" …


def _order_int(v) -> int:
    try:
        s = str(v).strip()
        return int(float(s)) if s else 0
    except (TypeError, ValueError):
        return 0


def _order_str(cells: list, idx) -> str | None:
    if idx is None or idx >= len(cells) or cells[idx] is None:
        return None
    s = str(cells[idx]).strip()
    return s or None


def _aggregate_order_lines(lines: list[dict], warnings: list[str]) -> dict:
    """Collapse parsed (style, colour, sizes) rows into one style's breakdown: a list of
    {colour → sizes} lines (deduped on colour+size) + the aggregate per-size totals."""
    by_color: dict[str, dict[str, int]] = {}
    color_names: dict[str, str | None] = {}
    per_size: dict[str, int] = {}
    style_name = None
    for ln in lines:
        style_name = style_name or ln.get("style")
        ckey = (ln.get("color") or "NA").strip().upper()
        color_names.setdefault(ckey, ln.get("color"))
        sizes = by_color.setdefault(ckey, {})
        for size, qty in (ln.get("sizes") or {}).items():
            s = str(size).strip().upper()
            sizes[s] = sizes.get(s, 0) + int(qty)
            per_size[s] = per_size.get(s, 0) + int(qty)
    out_lines = [
        {"color_code": ckey, "color_name": color_names.get(ckey), "sizes": sizes}
        for ckey, sizes in by_color.items()
    ]
    order_qty = sum(per_size.values())
    if order_qty == 0:
        warnings.append("order_sheet_no_quantities: no per-size quantities parsed.")
    return {
        "order_number": None, "style_name": style_name,
        "customer_ref": None, "internal_ref": None, "season": None,
        "unit_price": None, "currency": None,
        "order_qty": order_qty, "per_size_qty": per_size,
        "lines": out_lines, "warnings": warnings,
    }


def _extract_order_xlsx(data: bytes) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    lines: list[dict] = []
    warnings: list[str] = []
    for ws in wb.worksheets:
        col_map: dict = {}
        size_cols: dict = {}
        last_style = None
        for row in ws.iter_rows(values_only=True):
            cells = list(row)
            upper = [str(c).strip().upper() if c is not None else "" for c in cells]
            is_header = (
                any(u in ("S.NO", "SNO", "DATE") for u in upper[:2])
                or ("STYLE" in upper and any(_is_order_size_token(c) for c in cells))
            )
            if is_header:
                col_map, size_cols = {}, {}
                for idx, u in enumerate(upper):
                    if u == "STYLE":
                        col_map["style"] = idx
                    elif u in ("COLOUR", "COLOR", "SUEDE COLOUR", "SUEDE COLOR"):
                        col_map["color"] = idx
                    elif u == "ARTICLE":
                        col_map["article"] = idx
                    elif _is_order_size_token(cells[idx]):
                        size_cols[idx] = u
                continue
            if not size_cols:
                continue
            style = _order_str(cells, col_map.get("style"))
            color = _order_str(cells, col_map.get("color"))
            sizes = {label: _order_int(cells[idx])
                     for idx, label in size_cols.items()
                     if idx < len(cells) and _order_int(cells[idx]) > 0}
            row_total = sum(sizes.values())
            if not style and color and row_total > 0:
                style = last_style          # continuation row inherits the style
            if style and row_total > 0:
                last_style = style
                lines.append({"style": style, "color": color, "sizes": sizes})
    wb.close()
    return _aggregate_order_lines(lines, warnings)


def _extract_order_csv(data: bytes) -> dict:
    import csv as _csv

    text = data.decode("utf-8", errors="replace")
    rows = list(_csv.reader(io.StringIO(text)))
    lines: list[dict] = []
    warnings: list[str] = []
    col_map: dict = {}
    size_cols: dict = {}
    for cells in rows:
        upper = [str(c).strip().upper() for c in cells]
        if "STYLE" in upper and any(_is_order_size_token(c) for c in cells):
            col_map, size_cols = {}, {}
            for idx, u in enumerate(upper):
                if u == "STYLE":
                    col_map["style"] = idx
                elif u in ("COLOUR", "COLOR"):
                    col_map["color"] = idx
                elif _is_order_size_token(cells[idx]):
                    size_cols[idx] = u
            continue
        if not size_cols:
            continue
        style = _order_str(cells, col_map.get("style"))
        color = _order_str(cells, col_map.get("color"))
        sizes = {label: _order_int(cells[idx])
                 for idx, label in size_cols.items()
                 if idx < len(cells) and _order_int(cells[idx]) > 0}
        if style and sum(sizes.values()) > 0:
            lines.append({"style": style, "color": color, "sizes": sizes})
    return _aggregate_order_lines(lines, warnings)


_PDF_SIZE_QTY = re.compile(r"\b(XS|S|M|L|XL|XXL|XXXL)\b\s*[:\-]?\s*(\d{1,4})", re.IGNORECASE)


def _extract_order_pdf(data: bytes) -> dict:
    warnings: list[str] = []
    text = ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:                          # noqa: BLE001 — best-effort
        warnings.append(f"order_pdf_unreadable: {exc}")
    per_size: dict[str, int] = {}
    for m in _PDF_SIZE_QTY.finditer(text or ""):
        per_size[m.group(1).upper()] = per_size.get(m.group(1).upper(), 0) + int(m.group(2))
    if not per_size:
        warnings.append(
            "order_sheet_no_text_layer: per-size breakdown could not be parsed from the "
            "PDF (scanned/handwritten order sheets need OCR) — fill the quantities by hand.")
    lines = [{"style": None, "color": None, "sizes": per_size}] if per_size else []
    return _aggregate_order_lines(lines, warnings)


# ── Gemini-first extraction (vision for scanned PDFs, text for digital sheets) ──
# Mirrors procurement.build_vision_classifier: the SAME _init_model + _coerce + PDF→PNG
# rasterisation, but asking for a structured ORDER result instead of a classification.
# Used first for PDFs (the scanned/handwritten case the deterministic parser can't read)
# and as a last resort for machine-readable sheets that yielded nothing. Returns None
# whenever no Gemini key is configured / the model errors / the output is unusable, so the
# caller falls back to the deterministic parser (graceful degradation, same as the spec
# attribute extractor — a model outage never stalls BOM generation).
_ORDER_SCHEMA_HINT = """Return ONLY a JSON object with this exact shape (use null when \
unsure, omit unknown keys):
{
  "order_number": "...",
  "style_name": "...",
  "customer_ref": "...",
  "internal_ref": "...",
  "season": "...",
  "currency": "...",
  "unit_price": 0.0,
  "lines": [ { "color": "BLACK", "sizes": { "S": 4, "M": 17, "L": 23 } } ]
}
Each `lines` entry is ONE colour with its per-size quantities. Read EVERY size column and \
report the printed cell values; do not invent or total them."""


def _order_llm_prompt() -> str:
    return (
        "You are extracting a leather-garment buyer ORDER SHEET for a factory's BOM "
        "procurement workflow. Pull the order number, style name, customer/internal "
        "references, season, currency, per-garment price, and the PER-SIZE quantity "
        "breakdown for each colour exactly as printed (these sheets are often scanned or "
        "handwritten).\n\n" + _ORDER_SCHEMA_HINT
    )


def _order_text_blob(data: bytes, mime: str, name: str) -> str:
    """Flatten a digital order sheet (XLSX/CSV) to text for the non-vision LLM path."""
    try:
        if mime == CSV_MIME or name.endswith(".csv"):
            return data.decode("utf-8", errors="replace")
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        rows = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    rows.append("\t".join(cells))
        wb.close()
        return "\n".join(rows)
    except Exception:                                 # noqa: BLE001 — best-effort
        return ""


def _llm_order_to_result(parsed) -> dict | None:
    """Normalise the model's order JSON into the same shape _aggregate_order_lines emits."""
    if not isinstance(parsed, dict):
        return None
    norm: list[dict] = []
    for ln in (parsed.get("lines") or []):
        if not isinstance(ln, dict):
            continue
        sizes = {}
        for size, qty in (ln.get("sizes") or {}).items():
            q = _order_int(qty)
            if q > 0:
                sizes[str(size).strip().upper()] = q
        if sizes:
            norm.append({"style": parsed.get("style_name"),
                         "color": ln.get("color"), "sizes": sizes})
    if not norm:
        return None                                   # nothing usable → caller falls back
    result = _aggregate_order_lines(norm, ["order_extracted_by_llm"])
    for k in ("order_number", "customer_ref", "internal_ref", "season", "currency"):
        if parsed.get(k) is not None:
            result[k] = parsed[k]
    if parsed.get("style_name"):
        result["style_name"] = parsed["style_name"]
    if parsed.get("unit_price") is not None:
        try:
            result["unit_price"] = float(parsed["unit_price"])
        except (TypeError, ValueError):
            pass
    return result


def _extract_order_llm(data: bytes, filename: str, mime: str | None) -> dict | None:
    """Gemini-first order extraction. PDF → vision (page images via PyMuPDF); digital →
    text. None when vision/keys are unavailable or the output is unusable (→ fall back)."""
    try:
        from app.modules.procurement.classifier import _coerce, _init_model
    except Exception:                                 # noqa: BLE001
        return None
    m = (mime or "").lower()
    name = (filename or "").lower()
    is_pdf = m == PDF_MIME or name.endswith(".pdf")

    content: list[dict] = []
    if is_pdf:
        if not settings.vision_classifier_enabled:
            return None
        model = _init_model(settings.vision_model)
        if model is None:
            return None
        try:
            import base64

            from app.modules.procurement.sniffing import render_pdf_pages

            images = render_pdf_pages(data, max_pages=settings.vision_max_pages,
                                      dpi=settings.ocr_dpi)
        except Exception:                             # noqa: BLE001
            return None
        if not images:
            return None
        content.append({"type": "text", "text": _order_llm_prompt()})
        for png in images:
            b64 = base64.b64encode(png).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
    else:
        model = (_init_model(settings.extraction_model)
                 or _init_model(settings.extraction_fallback_model))
        if model is None:
            return None
        blob = _order_text_blob(data, m, name)[:12000]
        if not blob.strip():
            return None
        content.append({"type": "text", "text": _order_llm_prompt()
                        + f"\n\n--- order sheet content ---\n{blob}\n--- end ---"})

    try:
        from langchain_core.messages import HumanMessage

        resp = model.invoke([HumanMessage(content=content)])
        parsed = _coerce(getattr(resp, "content", "") or "")
    except Exception as exc:                          # noqa: BLE001
        logger.warning("LLM order extraction failed (%s) → deterministic fallback", exc)
        return None
    return _llm_order_to_result(parsed)


def extract_order(data: bytes, filename: str, mime: str | None = None,
                  client_match_code: str | None = None) -> dict:
    """Parse an order sheet into {order_qty, per_size_qty, lines[colour→sizes], style_name,
    refs, warnings}. PDFs (usually scanned) go to Gemini VISION first, falling back to a
    pypdf-text best-effort; machine-readable XLSX/CSV are parsed deterministically (exact,
    no LLM needed — CLAUDE.md §4 'NO LLM unless required'), escalating to the LLM only if
    they yield nothing. NEVER raises — an unparseable sheet returns zero quantities + a
    warning so Stage 2 still produces a (hand-correctable) BOM. `client_match_code` is
    accepted for future per-client adapters."""
    m = (mime or "").lower()
    name = (filename or "").lower()
    is_pdf = m == PDF_MIME or name.endswith(".pdf")
    try:
        if is_pdf:
            llm = _extract_order_llm(data, filename, mime)
            return llm if llm is not None else _extract_order_pdf(data)
        if m == XLSX_MIME or name.endswith((".xlsx", ".xlsm")):
            result = _extract_order_xlsx(data)
        elif m == CSV_MIME or name.endswith(".csv"):
            result = _extract_order_csv(data)
        else:
            llm = _extract_order_llm(data, filename, mime)
            if llm is not None:
                return llm
            logger.warning("extract_order: unsupported order-sheet type %s / %s", mime, filename)
            return _aggregate_order_lines([], [f"order_sheet_unsupported_type: {mime or name}"])
        # A machine-readable sheet that parsed to nothing → last-resort LLM escalation.
        if result.get("order_qty", 0) == 0:
            llm = _extract_order_llm(data, filename, mime)
            if llm is not None:
                return llm
        return result
    except Exception as exc:                          # noqa: BLE001 — best-effort
        logger.warning("extract_order failed for %s (%s): %s", filename, mime, exc)
        return _aggregate_order_lines([], [f"order_parse_error: {exc}"])