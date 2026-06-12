"""
================================================================================
modules/procurement/dcm.py — DCM resolution math (Stage 2 §2, the spine)
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
