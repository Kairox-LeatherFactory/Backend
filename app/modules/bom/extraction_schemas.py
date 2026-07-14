# extraction_schemas.py
"""
================================================================================
modules/bom/extraction_schemas.py — the Stage-2 extraction CONTRACT (Pydantic v2)
================================================================================

PURPOSE
    The single typed contract the whole extraction pipeline speaks. It mirrors the
    two Option-B staging tables 1:1 (SpecExtraction / OrderExtraction), so the LLM
    rungs, the orchestrators, and the service-layer persistence all agree on shape
    without re-describing it.

    The LLM is the PRIMARY extractor (Gemini -> Groq); manual is the degraded mode.
    Whichever engine wins, its result is coerced into THESE models and re-validated
    — so range/coherence checks run identically regardless of source. The extraction
    module now returns THESE models directly (no legacy dict shim); the service layer
    consumes them typed.

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
    treats as "try the next rung".

FUNCTION / MODEL GUIDE
  size_sort_key(s)        XS<S<M<L<XL<XXL<XXXL < numeric < everything-else; alias-aware.
  POMRow                  one measurement row: native term + per-size cm + pitch.
  Accessory               one trim item: type, supplier, placement, spec, finish, qty.
  SubMaterial             a secondary leather/fabric (contrast panel) → own DCM line.
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

# Measurements outside this band (in the spec's working unit) are almost certainly
# OCR noise or a transposed value. We WARN, never drop — the cutting manager decides.
_MEASURE_MIN = 1.0
_MEASURE_MAX = 300.0


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
    number (e.g. style_no=12345). None / blank / bool / complex types -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    if isinstance(v, (int, float)):
        return str(v)
    return None


# Synonyms the LLM tends to emit for who supplies a trim. Anything else -> None.
_SUPPLIED_BY_CLIENT = {"client", "buyer", "customer", "you", "the buyer", "buyer-supplied",
                       "provided by you", "supplied by buyer", "supplied by client"}
_SUPPLIED_BY_FACTORY = {"factory", "us", "supplier", "vendor", "the factory",
                        "factory-supplied", "provided by us", "supplied by us",
                        "supplied by factory"}


def _norm_supplied_by(v: Any) -> str | None:
    """Coerce free-text supplied_by to 'factory' | 'client' | None."""
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
    """Coerce free-text unit to 'cm' | 'inch'. Defaults to 'cm' on anything unknown."""
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
    qty_per_garment: int = 1              # count of THIS trim per garment (2 rear zips -> 2)

    @field_validator("supplied_by", mode="before")
    @classmethod
    def _norm_supplied(cls, v: Any) -> str | None:
        return _norm_supplied_by(v)

    @field_validator("type", "placement", "spec", "finish", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
        return _to_str_or_none(v)

    @field_validator("qty_per_garment", mode="before")
    @classmethod
    def _clean_qty(cls, v: Any) -> int:
        # Default to 1 when absent/zero/garbage — a trim line always uses at least one.
        f = _to_float(v)
        if f is None or f < 1:
            return 1
        return int(round(f))


class SubMaterial(BaseModel):
    """A SECONDARY leather/fabric that is neither the main shell nor the lining
    (e.g. a contrast panel, '別布'). Becomes its own DCM-resolved BOM line."""
    model_config = ConfigDict(extra="ignore")

    name: str | None = None               # native label as printed (e.g. '別布')
    material: str | None = None           # the actual material (e.g. 'GOAT')

    @field_validator("name", "material", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
        return _to_str_or_none(v)


class PatternRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pattern_code: str
    base_size: str | None = None
    source_term: str | None = None

    @field_validator("pattern_code", "base_size", "source_term", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
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
    season: str | None = None
    garment_type_guess: str | None = None
    sizes: list[str] = Field(default_factory=list)
    unit: str = "cm"                                      # coerced to 'cm' | 'inch'
    measurements: list[POMRow] = Field(default_factory=list)
    technical_details: list[TechInstruction] = Field(default_factory=list)
    materials: dict[str, Any] = Field(default_factory=dict)   # leather_quality, leather_substance_mm:[..], ...
    sub_materials: list[SubMaterial] = Field(default_factory=list)
    interlining: dict[str, Any] = Field(default_factory=dict)  # present:bool, material, placement
    lining: dict[str, Any] = Field(default_factory=dict)       # lined:bool|None, details, material
    brand_label: dict[str, Any] = Field(default_factory=dict)  # type, text, dimensions, placement
    size_label: str | None = None
    accessories: list[Accessory] = Field(default_factory=list)
    color_details: dict[str, Any] = Field(default_factory=dict)
    pattern_reference: PatternRef | None = None
    extracted_by: Literal["gemini", "groq", "heuristic", "manual"] = "heuristic"
    confidence_overall: float = 0.5
    warnings: list[str] = Field(default_factory=list)

    @field_validator("style_no", "article", "client_name", "season",
                     "garment_type_guess", "size_label", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
        return _to_str_or_none(v)

    @field_validator("unit", mode="before")
    @classmethod
    def _norm_unit_value(cls, v: Any) -> str:
        return _norm_unit(v)

    @field_validator("confidence_overall", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> float:
        f = _to_float(v)
        return min(1.0, max(0.0, f)) if f is not None else 0.5
    
    @field_validator("materials","interlining","lining","brand_label","color_details", mode="before")
    @classmethod
    def _none_to_dict(cls, v): return v or {}

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

    @model_validator(mode="after")
    def _warn_out_of_range(self) -> "ExtractedSpec":
        # Coherence WARNINGS (never failures) — this is the home the old
        # extraction.validate_intermediate range check used to live in, now that the
        # contract is typed. 'size_not_in_declared_set' is obsolete: _dedupe_sort_sizes
        # makes `sizes` the union of every measurement's sizes, so it can never fire.
        for pom in self.measurements:
            for size, val in pom.by_size.items():
                if val < _MEASURE_MIN or val > _MEASURE_MAX:
                    self.warnings.append(
                        f"measurement_out_of_range: {pom.source_term} {size}={val}")
        return self


# ── order-sheet result (mirrors order_extraction) ────────────────────────────
class OrderLine(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    model: str | None = None            # NEW: style/Modello this line belongs to
    color: str | None = None
    article: str | None = None
    sizes: dict[str, int] = Field(default_factory=dict)
    printed_total: int | None = None    # NEW: the row's printed TOTALE CAPI —
                                        # cross-check only; qty is ALWAYS recomputed
    
    @field_validator("model", "color", "article", mode="before")
    @classmethod
    def _coerce_str_model(cls, v: Any) -> str | None:
        return _to_str_or_none(v)

    @field_validator("color", "article", mode="before")
    @classmethod
    def _coerce_str_color(cls, v: Any) -> str | None:
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
    no materials/sub-materials/interlining/lining/colour/accessories, no labels, and
    no pattern reference."""
    return not (
        spec.measurements or spec.materials or spec.sub_materials or spec.interlining
        or spec.lining or spec.color_details or spec.accessories or spec.brand_label
        or spec.size_label or spec.technical_details or spec.pattern_reference
        or spec.style_no or spec.article
    )


def is_empty_order(order: ExtractedOrder) -> bool:
    """An order is 'empty' when no per-size quantities survived derivation."""
    return order.order_qty <= 0 and not order.lines


class StyleColorBreakdown(BaseModel):
    """One leather color within a style: aggregated across ALL order blocks."""
    color_key: str                       # normalised code ('06', '651', '724')
    color_label: str                     # richest label seen ('06 DARK BROWN')
    per_size_qty: dict[str, int]
    qty: int
    warnings: list[str] = Field(default_factory=list)   # e.g. printed-total mismatch


class StyleBreakdown(BaseModel):
    """One style within the order document — the unit a BOM is minted for."""
    style_key: str                       # normalised ('SHINOBI KNIT DETACH')
    material: str | None = None          # from the Materiale column
    qty: int
    per_size_qty: dict[str, int]
    colors: list[StyleColorBreakdown]
    warnings: list[str] = Field(default_factory=list)


class ExtractedOrderDoc(BaseModel):
    """A parsed order DOCUMENT: header + one StyleBreakdown per distinct style.
    Totals are recomputed from size cells; printed totals are checks only."""
    model_config = ConfigDict(extra="ignore")
    order_number: str | None = None
    client_name: str | None = None
    season: str | None = None
    currency: str | None = None
    delivery_date: str | None = None
    payment_term: str | None = None
    styles: list[StyleBreakdown] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    extracted_by: str = "manual"