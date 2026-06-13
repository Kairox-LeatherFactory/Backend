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
    """'A'->1, 'B'->2, ... 'AA'->27 (openpyxl 1-based)."""
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
