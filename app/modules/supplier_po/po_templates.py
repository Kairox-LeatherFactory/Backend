"""
================================================================================
modules/supplier_po/po_templates.py — PO template registry (§2b)
================================================================================

"One PO template per supplier type" (§2): the differences between a leather, an
accessory/material, and a service/job-work PO are tiny — the line-grid UOM column
header + the default UOM — so they live as CONFIG (`config/po_templates.yaml`), read
here once and cached. Onboarding a new supplier type is a YAML row + (optionally) a
child HTML template in `templates/po/` — NO code branch, the same promote-by-config
posture as every prior stage.

This is the PURE, sync registry loader (no DB). Templates that are persisted into a
`po_template` table instead are an equivalent option (§10); the YAML is the shipped
default.

FUNCTION GUIDE
  _BUILTIN   the shipped fallback map (leather/accessory/service) so the renderer works
             before the YAML is seeded.
  _load() -> {supplier_type: cfg}   [private, cached] merge the YAML over _BUILTIN.
  template_cfg_for(supplier_type) -> dict | None
      The template cfg (uom_header/default_uom/...) for a type, or None to let the caller
      default. CALLED FROM: PoService._po_template_cfg (at send time).
  reset_cache()   test hook — drop the cached registry.
================================================================================
"""
from __future__ import annotations

import os

import yaml

_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "config", "po_templates.yaml",
)

# Shipped fallback so the renderer works even before the YAML is seeded.
_BUILTIN = {
    "leather": {"template_name": "leather", "uom_header": "DCM/SQF", "default_uom": "DCM"},
    "accessory": {"template_name": "accessory", "uom_header": "UOM", "default_uom": "NOS"},
    "service": {"template_name": "service", "uom_header": "UOM", "default_uom": "NOS"},
}

_cache: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache
    out = dict(_BUILTIN)
    try:
        with open(_CONFIG, encoding="utf-8") as fh:
            rows = yaml.safe_load(fh) or []
        for r in rows:
            st = (r.get("supplier_type") or "").lower()
            if st:
                out[st] = {
                    "template_name": r.get("template_name", st),
                    "uom_header": r.get("uom_header", "UOM"),
                    "default_uom": r.get("default_uom", "NOS"),
                    "gst_mode": r.get("gst_mode"),
                }
    except FileNotFoundError:
        pass
    _cache = out
    return out


def template_cfg_for(supplier_type: str | None) -> dict | None:
    """The template cfg for a supplier type, or None to let the caller default."""
    if not supplier_type:
        return None
    return _load().get(supplier_type.lower())


def reset_cache() -> None:
    global _cache
    _cache = None
