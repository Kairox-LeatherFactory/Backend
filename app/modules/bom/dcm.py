"""
================================================================================
modules/bom/dcm.py — DCM resolution math (Stage 2 §2, the spine)
================================================================================

DCM = dm² of each material consumed per garment (BMO-1: Sheep Glass 34.5, Goat
Suede 2.6). No spec sheet provides it, so the engine RESOLVES it through a
mandatory ordered fallback (§2):

    (1) style_consumption_template  → conf 0.95   (the DCM memory — cheapest, best)
    (2) similar-style retrieval     → conf ~0.70
    (3) AI heuristic: POM area × wastage → conf ≤0.50, FLAGGED (last resort)
    (4) cutting-manager manual confirm → conf 1.00 (source of truth; written back)

This module holds the PURE pieces: the cross-order style signature (so the memory
keys stably across orders, §3c) and the Source-3 area heuristic. The ordered DB
lookups (Sources 1/2/4) live in the service, which owns the session. A lower-
numbered source ALWAYS wins when available; the AI estimate is never the default.

FUNCTION GUIDE
  CONFIDENCE  [dict constant]
      Maps each DcmSource → its stamped confidence (template 0.95, similar 0.70,
      ai_estimate 0.50, manual 1.00). BomService reads it to set bom_item.dcm_confidence.
  slugify(name) -> str
      Lowercase + hyphenate a string. Helper for style_signature's last-resort key.
  style_signature(*, customer_ref, internal_ref, name) -> str
      The CROSS-ORDER-STABLE key for the DCM memory. Style rows are order-scoped, so
      we must NOT key on style_id — prefer customer_ref → internal_ref → slug(name).
      Returns an UPPERCASE signature string. CALLED FROM: BomService.generate_bom
      (to look up / write templates) and confirm_cutting (to back-fill them), so the
      second order of the same physical style hits Source 1.
  estimate_area_dcm(area_formula, wastage_pct, poms_for_size) -> Decimal | None
      Source-3 last-resort heuristic — the ONLY place finished measurements touch
      consumption. Computes a rough panel bounding-box area from POMs × the garment
      type's formula × (1 + wastage). Returns a positive Decimal estimate, or None
      when the needed POMs are absent (e.g. Jackiee has no POMs → can never use
      Source 3). CALLED FROM: BomService._resolve_dcm, after Sources 1/2 miss; the
      caller stamps dcm_source=ai_estimate + flags it for cutting review.
================================================================================
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.modules.bom.enums import DcmSource

# Per-source confidence stamps (§2). Every material bom_item carries one.
CONFIDENCE = {
    DcmSource.TEMPLATE: Decimal("0.95"),
    DcmSource.SIMILAR_STYLE: Decimal("0.70"),
    DcmSource.AI_ESTIMATE: Decimal("0.50"),
    DcmSource.MANUAL: Decimal("1.00"),
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def style_signature(*, customer_ref: str | None, internal_ref: str | None,
                    name: str | None) -> str:
    """The CROSS-ORDER-STABLE key for the DCM memory (§3c). Style rows are
    order-scoped, so we MUST NOT key on style_id — prefer the buyer's stable
    customer_ref (CR1-02F5-PL02) → internal_ref → a slug of the style name, so the
    second order of the same physical style hits the template (acceptance §11.5)."""
    if customer_ref and customer_ref.strip():
        return customer_ref.strip().upper()
    if internal_ref and internal_ref.strip():
        return internal_ref.strip().upper()
    return slugify(name or "unknown") or "unknown"


def estimate_area_dcm(area_formula: dict | None, wastage_pct, poms_for_size: dict) -> Decimal | None:
    """Source-3 heuristic (the ONLY place finished measurements touch consumption).
    A rough pattern bounding box per panel:

        length_cm = Σ(weight·POM) over length_poms
        width_cm  = Σ(weight·POM) over width_poms
        dcm_dm²   = panels · length_cm · width_cm · calibration / 100 · (1 + wastage)

    Returns a positive Decimal estimate, or None if the POMs the formula needs are
    absent (e.g. Jackiee has NO POMs — it can never use Source 3, by design §6).
    Explicitly an ESTIMATE: the caller stamps ai_estimate / conf ≤0.5 and flags it."""
    if not area_formula or not poms_for_size:
        return None

    def _weighted(spec: dict) -> float:
        total = 0.0
        if not spec:
            return 0.0
        for code, w in (spec or {}).items():
            v = poms_for_size.get(code)
            if v is None:
                return 0.0
            try:
                total += float(w) * float(v)
            except (ValueError, TypeError):
                return 0.0
        return total 
    
# The Scenario: A back panel's length formula requires two POMs: back_length (weight 1.0) and collar_height (weight 1.0). 
# In the uploaded spec, back_length = 80cm, but collar_height is missing (None).

# The Flaw: The code sees back_length, sets seen = True, and successfully computes the weighted length as 80.0. 
# It completely ignores the missing collar_height and returns 80.0 instead of rejecting the formula.

# The Impact: The system silently under-calculates the fabric surface area. 
# For a large production run (e.g., 5,000 units), this minor omission can translate to thousands of meters of missing fabric,
#    resulting in major financial losses.


    length = _weighted(area_formula.get("length_poms", {}))
    width = _weighted(area_formula.get("width_poms", {}))
    if length <= 0 or width <= 0:
        return None
    panels = float(area_formula.get("panels", 1) or 1)
    calibration = float(area_formula.get("calibration", 1.0) or 1.0)        #float might cause decimal error so maybe use Decimal instead of float
    wastage = float(wastage_pct or 0) / 100.0
    area_cm2 = panels * length * width * calibration
    dcm = area_cm2 / 100.0 * (1.0 + wastage)
    return Decimal(str(round(dcm, 3)))
