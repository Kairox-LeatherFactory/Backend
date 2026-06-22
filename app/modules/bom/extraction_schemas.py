"""
================================================================================
modules/bom/extraction_schemas.py — the Stage-2 extraction CONTRACT (Pydantic v2)
================================================================================

PURPOSE
    The single typed contract the whole new extraction pipeline speaks. It mirrors
    the two Option-B staging tables 1:1 (SpecExtraction / OrderExtraction), so the
    LLM rungs, the heuristic rungs, the orchestrators, and the service-layer
    persistence all agree on shape without re-describing it.

    The LLM is the PRIMARY extractor (Gemini -> Groq); the heuristic is degraded
    mode only. Whichever engine wins, its result is coerced into THESE models and
    re-validated — so range/coherence checks run identically regardless of source.

WHY DERIVED TOTALS ARE NEVER LLM-TRUSTED
    OCR/Gemini routinely transpose or hallucinate a printed GRAND TOTAL. So
    ExtractedOrder.order_qty + per_size_qty are RECOMPUTED from the per-line sizes
    on construction (model_validator) and the model NEVER keeps a model-supplied
    total. The orchestrator separately compares the recomputed sum against any
    printed total and appends a `size_total_mismatch` warning — it never fails.

GRACEFUL POSTURE
    Field validators CLEAN rather than hard-reject wherever a messy-but-recoverable
    value appears (coerce "5" -> 5.0, drop non-positive measurements, dedupe+sort
    sizes). Truly malformed *types* still raise ValidationError — which the LLM path
    treats as "try the next rung", and which the heuristic path never triggers
    because it only ever feeds clean primitives in.

FUNCTION / MODEL GUIDE
  size_sort_key(s)        XS<S<M<L<XL<XXL<XXXL < numeric < everything-else; alias-aware.
  POMRow                  one measurement row: native term + per-size cm + pitch.
  Accessory               one trim item: type, who supplies it, placement, spec, finish.
  PatternRef              a "follow pattern X in size Y" reference (NOT new POMs).
  TechInstruction         one categorised workmanship/stitching/cutting/finishing note.
  ExtractedSpec           the full spec-sheet result (mirrors spec_extraction).
  OrderLine               one colour's per-size quantities on an order sheet.
  ExtractedOrder          the full order-sheet result (mirrors order_extraction).
  is_empty_spec(spec)     True when a spec carries no usable signal (-> try next rung).
  is_empty_order(order)   True when an order carries no quantities (-> try next rung).
================================================================================
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── size ordering (shared by spec sizes + order sizes) ───────────────────────
_SIZE_ORDER = ["XS", "S", "M", "L", "XL", "XXL", "XXXL", "XXXXL"]
_SIZE_ALIAS = {"2XL": "XXL", "3XL": "XXXL", "4XL": "XXXXL", "1X": "XL"}


def size_sort_key(s: Any):
    """Order sizes so an alpha run (XS..XXXXL, incl. 2XL/3XL aliases) sorts before a
    numeric run (38, 40, 42 ...) before anything unrecognised. Stable + total."""
    su = str(s).strip().upper()
    su = _SIZE_ALIAS.get(su, su)
    if su in _SIZE_ORDER:
        return (0, _SIZE_ORDER.index(su))
    try:
        return (1, float(str(s).strip()))
    except (TypeError, ValueError):
        return (2, su)


def _to_float(v: Any) -> float | None:
    """Coerce a possibly-string numeric ('5', '0,45', 4.2) to float, else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return None
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _to_str_or_none(v: Any) -> str | None:
    """Coerce a value to a clean trimmed string for fields the LLM might emit as a
    number (e.g. style_no=12345). None / blank / bool / complex types -> None. The LLM
    occasionally returns numeric order numbers or season years — accept them as strings
    instead of raising ValidationError on the whole record."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    if isinstance(v, (int, float)):
        return str(v)
    return None


# Synonyms the LLM tends to emit for who supplies a trim. Anything else -> None
# (no rejection — the field is informational; the heuristic still records placement
# and spec even when supplied_by is unknown).
_SUPPLIED_BY_CLIENT = {"client", "buyer", "customer", "you", "the buyer", "buyer-supplied",
                       "provided by you", "supplied by buyer", "supplied by client"}
_SUPPLIED_BY_FACTORY = {"factory", "us", "supplier", "vendor", "the factory",
                        "factory-supplied", "provided by us", "supplied by us",
                        "supplied by factory"}


def _norm_supplied_by(v: Any) -> str | None:
    """Coerce free-text supplied_by to 'factory' | 'client' | None. Replaces the strict
    Literal that silently rejected the whole ExtractedSpec on one bad accessory."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    if s in _SUPPLIED_BY_CLIENT:
        return "client"
    if s in _SUPPLIED_BY_FACTORY:
        return "factory"
    return None                                          # unknown -> null, never reject


_UNIT_INCH = {"inch", "inches", "in", "in.", '"', "''", "imperial"}


def _norm_unit(v: Any) -> str:
    """Coerce free-text unit to 'cm' | 'inch'. Defaults to 'cm' (the factory's working
    unit) on anything unknown. Replaces the strict Literal that rejected the whole spec
    when an LLM returned 'inches' / 'centimeter' / 'CM ' (case + trailing space)."""
    if v is None:
        return "cm"
    s = str(v).strip().lower().rstrip(".")
    return "inch" if s in _UNIT_INCH else "cm"


# ── building blocks ──────────────────────────────────────────────────────────
class POMRow(BaseModel):
    """One point-of-measure as the source document prints it: the NATIVE term (any
    language), per-size values, and an optional grading pitch. Term resolution to a
    standard pom_code is the SERVICE's job, not extraction's — so no pom_code here."""
    model_config = ConfigDict(extra="ignore")

    source_term: str
    by_size: dict[str, float] = Field(default_factory=dict)
    pitch: float | None = None
    extracted_by: str = "heuristic"
    confidence: float = 0.5

    @field_validator("by_size", mode="before")
    @classmethod
    def _clean_by_size(cls, v: Any) -> dict[str, float]:
        # Coerce values to float and drop anything non-positive or unparseable, so a
        # POMRow only ever carries real measurements (the "values positive" contract).
        if not isinstance(v, dict):
            return {}
        out: dict[str, float] = {}
        for size, val in v.items():
            f = _to_float(val)
            if f is not None and f > 0:
                out[str(size).strip().upper()] = f
        return out

    @field_validator("pitch", mode="before")
    @classmethod
    def _clean_pitch(cls, v: Any) -> float | None:
        f = _to_float(v)
        return f if (f is not None and f >= 0) else None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_conf(cls, v: Any) -> float:
        f = _to_float(v)
        return min(1.0, max(0.0, f)) if f is not None else 0.5


class Accessory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    supplied_by: str | None = None        # coerced to 'factory' | 'client' | None
    placement: str | None = None
    spec: str | None = None
    finish: str | None = None

    @field_validator("supplied_by", mode="before")
    @classmethod
    def _norm_supplied(cls, v: Any) -> str | None:
        # The LLM frequently emits synonyms ('buyer', 'us', 'vendor'). A strict Literal
        # raised on ANY of these and silently dropped the entire ExtractedSpec to the
        # heuristic — replaced with coercion so unknown values become None, not failures.
        return _norm_supplied_by(v)

    @field_validator("type", "placement", "spec", "finish", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
        # Coerce LLM-emitted numbers/booleans to strings so a quirky type:1 or finish:0
        # doesn't reject the whole record. type is required str — if coercion returns
        # None Pydantic raises on the empty value (a typeless accessory is meaningless).
        return _to_str_or_none(v)


class PatternRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pattern_code: str
    base_size: str | None = None
    source_term: str | None = None

    @field_validator("pattern_code", "base_size", "source_term", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
        # Pattern codes and base sizes are sometimes emitted as numbers (base_size:42).
        return _to_str_or_none(v)


class TechInstruction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: Literal["stitching", "cutting", "workmanship", "finishing", "general"] = "general"
    instruction: str

    @field_validator("category", mode="before")
    @classmethod
    def _norm_category(cls, v: Any) -> str:
        s = str(v or "").strip().lower()
        return s if s in {"stitching", "cutting", "workmanship", "finishing", "general"} else "general"


# ── spec-sheet result (mirrors spec_extraction) ──────────────────────────────
class ExtractedSpec(BaseModel):
    """The full result of parsing a spec sheet (measurement grid, narrative tech pack,
    or anything in between). Emits NATIVE terms; the service resolves them later."""
    model_config = ConfigDict(extra="ignore")

    style_no: str | None = None
    article: str | None = None
    client_name: str | None = None
    garment_type_guess: str | None = None
    sizes: list[str] = Field(default_factory=list)
    unit: str = "cm"                                      # coerced to 'cm' | 'inch'
    measurements: list[POMRow] = Field(default_factory=list)
    technical_details: list[TechInstruction] = Field(default_factory=list)
    materials: dict[str, Any] = Field(default_factory=dict)   # leather_quality, leather_substance_mm:[..], ...
    lining: dict[str, Any] = Field(default_factory=dict)      # lined:bool|None, details, material
    brand_label: dict[str, Any] = Field(default_factory=dict)  # type, text, dimensions, placement, instructions
    size_label: str | None = None
    accessories: list[Accessory] = Field(default_factory=list)
    color_details: dict[str, Any] = Field(default_factory=dict)
    pattern_reference: PatternRef | None = None
    extracted_by: Literal["gemini", "groq", "heuristic", "manual"] = "heuristic"
    confidence_overall: float = 0.5
    warnings: list[str] = Field(default_factory=list)

    @field_validator("style_no", "article", "client_name", "garment_type_guess",
                     "size_label", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
        # LLMs sometimes emit style_no:12345 (integer) or season:2026. Coerce instead
        # of raising — a numeric identifier is still a usable identifier.
        return _to_str_or_none(v)

    @field_validator("unit", mode="before")
    @classmethod
    def _norm_unit_value(cls, v: Any) -> str:
        # 'inches' / 'CM ' / 'centimeter' all used to reject the whole spec; coerce.
        return _norm_unit(v)

    @field_validator("confidence_overall", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> float:
        f = _to_float(v)
        return min(1.0, max(0.0, f)) if f is not None else 0.5

    @model_validator(mode="after")
    def _dedupe_sort_sizes(self) -> "ExtractedSpec":
        # The authoritative size set is the union of every measurement's sizes plus any
        # sizes the model/heuristic declared. Deduped + sorted so downstream is stable.
        seen: set[str] = set()
        for s in self.sizes:
            seen.add(str(s).strip().upper())
        for pom in self.measurements:
            seen.update(pom.by_size.keys())
        self.sizes = sorted({s for s in seen if s}, key=size_sort_key)
        return self


# ── order-sheet result (mirrors order_extraction) ────────────────────────────
class OrderLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    color: str | None = None
    article: str | None = None
    sizes: dict[str, int] = Field(default_factory=dict)

    @field_validator("color", "article", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
        # Colour codes occasionally arrive as numbers (color:840 for a Pantone-like code).
        return _to_str_or_none(v)

    @field_validator("sizes", mode="before")
    @classmethod
    def _clean_sizes(cls, v: Any) -> dict[str, int]:
        if not isinstance(v, dict):
            return {}
        out: dict[str, int] = {}
        for size, qty in v.items():
            f = _to_float(qty)
            if f is not None and f > 0:
                out[str(size).strip().upper()] = int(round(f))
        return out


class ExtractedOrder(BaseModel):
    """The full result of parsing an order sheet. order_qty + per_size_qty are DERIVED
    from `lines` on construction and are never trusted from the model directly."""
    model_config = ConfigDict(extra="ignore")

    order_number: str | None = None
    style_no: str | None = None
    article: str | None = None
    client_name: str | None = None
    season: str | None = None
    currency: str | None = None
    price_per_garment: float | None = None
    delivery_date: str | None = None          # ISO (yyyy-mm-dd) when parseable
    payment_term: str | None = None
    color_details: list[Any] = Field(default_factory=list)
    leather_material: dict[str, Any] = Field(default_factory=dict)
    lines: list[OrderLine] = Field(default_factory=list)
    per_size_qty: dict[str, int] = Field(default_factory=dict)
    order_qty: int = 0
    extracted_by: Literal["gemini", "groq", "heuristic", "manual"] = "heuristic"
    confidence_overall: float = 0.5
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _surface_dropped_qtys(cls, data: Any) -> Any:
        # Before field-level cleaning silently drops negative / unparseable line qtys,
        # walk the raw LLM payload and append a warning per drop so the user knows
        # what was lost. Heuristic path passes OrderLine instances (not raw dicts) and
        # is skipped naturally — it never produces negatives anyway.
        if not isinstance(data, dict):
            return data
        raw_lines = data.get("lines")
        if not isinstance(raw_lines, list):
            return data
        dropped: list[str] = []
        for idx, line in enumerate(raw_lines):
            if not isinstance(line, dict):
                continue
            color = (line.get("color") or f"line{idx}").strip() if isinstance(
                line.get("color"), str) else f"line{idx}"
            sizes = line.get("sizes")
            if not isinstance(sizes, dict):
                continue
            for size, qty in sizes.items():
                f = _to_float(qty)
                if f is not None and f < 0:
                    dropped.append(f"negative_qty_dropped: {color} {size}={qty}")
                elif f is None and qty not in (None, ""):
                    dropped.append(f"unparseable_qty_dropped: {color} {size}={qty!r}")
        if dropped:
            existing = list(data.get("warnings") or [])
            data["warnings"] = existing + dropped
        return data

    @field_validator("order_number", "style_no", "article", "client_name", "season",
                     "currency", "delivery_date", "payment_term", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
        # season:2026 / order_number:12345 / currency:840 are all real LLM emissions.
        return _to_str_or_none(v)

    @field_validator("price_per_garment", mode="before")
    @classmethod
    def _clean_price(cls, v: Any) -> float | None:
        f = _to_float(v)
        return f if (f is not None and f > 0) else None

    @field_validator("confidence_overall", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> float:
        f = _to_float(v)
        return min(1.0, max(0.0, f)) if f is not None else 0.5

    @model_validator(mode="after")
    def _derive_totals(self) -> "ExtractedOrder":
        # Recompute per-size aggregate + order_qty from the lines (the only trustworthy
        # source). If there are no lines but a per_size_qty was supplied (e.g. a PDF
        # best-effort that found sizes without colour lines), derive the total from that.
        per_size: dict[str, int] = {}
        for ln in self.lines:
            for size, qty in ln.sizes.items():
                per_size[size] = per_size.get(size, 0) + int(qty)
        if per_size:
            self.per_size_qty = {s: per_size[s] for s in sorted(per_size, key=size_sort_key)}
        else:
            cleaned = {}
            for s, q in (self.per_size_qty or {}).items():
                f = _to_float(q)
                if f is not None and f > 0:
                    cleaned[str(s).strip().upper()] = int(round(f))
            self.per_size_qty = {s: cleaned[s] for s in sorted(cleaned, key=size_sort_key)}
        self.order_qty = sum(self.per_size_qty.values())
        return self


# ── emptiness probes (the LLM path: "ValidationError OR empty -> next rung") ──
def is_empty_spec(spec: ExtractedSpec) -> bool:
    """A spec is 'empty' (model produced nothing usable) when it has no measurements,
    no materials/lining/colour/accessories, no labels, and no pattern reference."""
    return not (
        spec.measurements or spec.materials or spec.lining or spec.color_details
        or spec.accessories or spec.brand_label or spec.size_label
        or spec.technical_details or spec.pattern_reference
        or spec.style_no or spec.article
    )


def is_empty_order(order: ExtractedOrder) -> bool:
    """An order is 'empty' when no per-size quantities survived derivation."""
    return order.order_qty <= 0 and not order.lines