"""
modules/bom/dcm.py â€” DCM resolution math (Stage 2 Â§2, the spine)

DCM = dmÂ² of each material consumed per garment (BMO-1: Sheep Glass 34.5, Goat
Suede 2.6). No spec sheet provides it, so the engine RESOLVES it through a
mandatory ordered fallback (Â§2):

    (1) style_consumption_template  â†’ conf 0.95   (the DCM memory â€” cheapest, best)
    (2) similar-style retrieval     â†’ conf ~0.70
    (3) AI heuristic: POM area Ã— wastage â†’ conf â‰¤0.50, FLAGGED (last resort)
    (4) cutting-manager manual confirm â†’ conf 1.00 (source of truth; written back)

This module holds the PURE pieces: the cross-order style signature (so the memory
keys stably across orders, Â§3c) and the Source-3 area heuristic. The ordered DB
lookups (Sources 1/2/4) live in the service, which owns the session. A lower-
numbered source ALWAYS wins when available; the AI estimate is never the default.

FUNCTION GUIDE
  CONFIDENCE  [dict constant]
      Maps each DcmSource â†’ its stamped confidence (template 0.95, similar 0.70,
      ai_estimate 0.50, manual 1.00). BomService reads it to set bom_item.dcm_confidence.
  slugify(name) -> str
      Lowercase + hyphenate a string. Helper for style_signature's last-resort key.
  style_signature(*, customer_ref, internal_ref, name) -> str
      The CROSS-ORDER-STABLE key for the DCM memory. Style rows are order-scoped, so
      we must NOT key on style_id â€” prefer customer_ref â†’ internal_ref â†’ slug(name).
      Returns an UPPERCASE signature string. CALLED FROM: BomService.generate_bom
      (to look up / write templates) and confirm_cutting (to back-fill them), so the
      second order of the same physical style hits Source 1.
  estimate_area_dcm(area_formula, wastage_pct, poms_for_size) -> Decimal | None
      Source-3 last-resort heuristic â€” the ONLY place finished measurements touch
      consumption. Computes a rough panel bounding-box area from POMs Ã— the garment
      type's formula Ã— (1 + wastage). Returns a positive Decimal estimate, or None
      when the needed POMs are absent (e.g. Jackiee has no POMs â†’ can never use
      Source 3). CALLED FROM: BomService._resolve_dcm, after Sources 1/2 miss; the
      caller stamps dcm_source=ai_estimate + flags it for cutting review.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.modules.bom.enums import DcmSource

# Per-source confidence stamps (Â§2). Every material bom_item carries one.
CONFIDENCE = {
    DcmSource.TEMPLATE: Decimal("0.95"),
    DcmSource.DXF: Decimal("0.88"),          # NEW â€” below template, above similar
    DcmSource.SIMILAR_STYLE: Decimal("0.70"),
    DcmSource.AI_ESTIMATE: Decimal("0.50"),
    DcmSource.MANUAL: Decimal("1.00"),
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def style_signature(*, customer_ref: str | None, internal_ref: str | None,name: str | None) -> str:
    """The CROSS-ORDER-STABLE key for the DCM memory (Â§3c). Style rows are
    order-scoped, so we MUST NOT key on style_id â€” prefer the buyer's stable
    customer_ref (CR1-02F5-PL02) â†’ internal_ref â†’ a slug of the style name, so the
    second order of the same physical style hits the template (acceptance Â§11.5)."""
    if customer_ref and customer_ref.strip():
        return customer_ref.strip().upper()
    if internal_ref and internal_ref.strip():
        return internal_ref.strip().upper()
    slug = slugify(name or "")
    return slug if slug else None


def estimate_area_dcm(area_formula: dict | None, wastage_pct, poms_for_size: dict) -> Decimal | None:
    """Source-3 heuristic (the ONLY place finished measurements touch consumption).
    A rough pattern bounding box per panel:
        length_cm = Î£(weightÂ·POM) over length_poms
        width_cm  = Î£(weightÂ·POM) over width_poms
        dcm_dmÂ²   = panels Â· length_cm Â· width_cm Â· calibration / 100 Â· (1 + wastage)

    Returns a positive Decimal estimate, or None if the POMs the formula needs are
    absent (e.g. Jackiee has NO POMs â€” it can never use Source 3, by design Â§6).
    Explicitly an ESTIMATE: the caller stamps ai_estimate / conf â‰¤0.5 and flags it."""
    if not area_formula or not poms_for_size:
        return None

    def _weighted(spec: dict) -> float:
        total = 0.0
        seen = False
        for code, w in (spec or {}).items():
            v = poms_for_size.get(code)
            if v is not None:
                total += float(w) * float(v)
                seen = True
        return total if seen else 0.0

    length = _weighted(area_formula.get("length_poms", {}))
    width = _weighted(area_formula.get("width_poms", {}))
    if length <= 0 or width <= 0:
        return None
    panels = float(area_formula.get("panels", 1) or 1)
    calibration = float(area_formula.get("calibration", 1.0) or 1.0)
    wastage = float(wastage_pct or 0) / 100.0
    area_cm2 = panels * length * width * calibration
    dcm = area_cm2 / 100.0 * (1.0 + wastage)
    return Decimal(str(round(dcm, 3)))

# DXF-path yields (per species). Distinct from any proxy calibration: this multiplies the
# TRUE net pattern area. Bootstrap from order 1579 (sheep 34.5/13.77=2.51, goat 2.6/1.22=2.13);
# the learning loop replaces these with measured means. Sits in cost_catalog.yaml under a
# `dxf_yield_factor:` key, or falls back to these defaults.
_DEFAULT_DXF_YIELDS = {"sheep": 2.50, "goat": 2.10, "calf": 2.30, "lamb": 2.50, "_default": 2.30}

def dxf_yields() -> dict:
    from app.modules.bom import config_store
    return config_store.get_dxf_yields()   # or move it to config

def species_of(name: str) -> str:
    m = (name or "").lower()
    if "goat" in m: return "goat"
    if "sheep" in m or "lamb" in m: return "sheep"
    if "calf" in m: return "calf"
    return "_default"

def fabric_lexicon() -> dict:
    from app.modules.bom import config_store
    return config_store.get_fabric_lexicon()

def effective_dxf_yields(seed: dict, observations_by_species: dict) -> dict:
    out = dict(seed or {})
    for sp, ys in (observations_by_species or {}).items():
        vals = [float(y) for y in ys if y]
        if vals: out[sp] = round(sum(vals)/len(vals), 3)
    return out