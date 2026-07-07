"""pattern.py — the DXF source (Source 1c) for the DcmResolver.
[Stages 4/5/7 in ARCHITECTURE_AND_DATAFLOW.md — dcm_for_category, graded_dxf, learn_yield.]

DXF and history COMPOSE: the DXF gives the precise part (net pattern area per size per
fabric, measured geometry); confirmed orders give the empirical part (the per-species
yield — the waste multiplier). DCM(size) = net_qty_sf(size, leather-fabrics-of-category)
× yield(species). Per order 1579: sheep 13.77 sf × ~2.5 = 34.4 (truth 34.5); goat 1.22
× ~2.1 = 2.56 (truth 2.6). PER MATERIAL — the old all-pieces 1.73 conflated leather +
pocketing + elastic, which is wrong; pocketing/elastic are is_leather:false and excluded.

Pure functions; they read a PatternData (or any object exposing .fabric_roles /
.fabric_matrix / .master_size — the persisted PatternExtraction row is compatible).
"""
from __future__ import annotations

# role (from the lexicon) -> the BomItemCategory-style key the resolver resolves on
_ROLE_TO_CATEGORY = {"main": "main_material", "sub_material": "sub_material",
                     "lining": "lining", "interlining": "interlining"}


def role_to_category(role: str) -> str:
    return _ROLE_TO_CATEGORY.get(role, role)


def _net_qty_for_category(pattern, category: str, size) -> tuple[str | None, float]:
    """Σ net_qty_sf of the LEATHER fabrics whose resolved role maps to `category`, at
    `size` (falling back to master_size). Returns (resolved_size_key, net_sf).

    (a) default: if NO fabric resolves to a leather role AND the pattern has ≤1 distinct
    fabric label (unlabelled/single-fabric export — e.g. Orfatti), the whole net area at
    the size is attributed to `category`. With multiple labelled-but-unmapped fabrics we
    refuse (return None) so the operator maps them (override b) rather than mis-attributing
    lining/textile area to leather."""
    fabric_roles = pattern.fabric_roles or {}
    leather = {f for f, m in fabric_roles.items()
               if m.get("is_leather") and m.get("category") == category}
    fm = pattern.fabric_matrix or {}
    key = str(size)
    if key not in fm:
        key = pattern.master_size if pattern.master_size in fm else None
    if key is None:
        return None, 0.0
    at = fm.get(key, {})
    if leather:
        net = sum(float(v) for f, v in at.items() if f in leather)
        return key, round(net, 2)
    distinct = {f for f in at if f}                       # blank/"" counts as "unlabelled"
    if len(distinct) <= 1:                                # single-fabric / unlabelled export
        net = sum(float(v) for v in at.values())
        return key, round(net, 2)
    return None, 0.0                                      # labelled but unmapped → refuse


def dcm_for_category(pattern, *, category: str, size, species: str, yields: dict):
    """Source-1c DCM for one leather material line: net_qty_sf(category, size) ×
    yield(species). Returns float or None (None -> resolver falls through to predictor)."""
    key, net = _net_qty_for_category(pattern, category, size)
    if key is None or net <= 0:
        return None
    yld = yields.get(species) or yields.get("_default") or 1.0
    return round(net * float(yld), 1)


def graded_dxf(pattern, *, category: str, sizes, species: str, yields: dict) -> dict:
    """Per-size DCM straight from the DXF (each size has its own measured net area), so a
    DXF line is graded by exact geometry rather than a POM-ratio proxy. {size: dcm_sf}."""
    out: dict = {}
    for s in sizes:
        d = dcm_for_category(pattern, category=category, size=s, species=species, yields=yields)
        if d is None:                       # size missing AND no master fallback
            d = dcm_for_category(pattern, category=category, size=pattern.master_size,
                                 species=species, yields=yields)
        out[s] = d
    return out


def learn_yield(pattern, *, category: str, size, species: str, confirmed_dcm_sf) -> dict | None:
    """Confirmed DCM ÷ pattern net area -> implied per-species yield. The calibration hook
    the confirm gate calls per confirmed leather line. Returns the observation fields, or
    None when this category has no leather geometry. ~1.5–3.0 is the sane band for hide."""
    key, net = _net_qty_for_category(pattern, category, size)
    if key is None or net <= 0:
        return None
    c = float(confirmed_dcm_sf)
    if c <= 0:
        return None
    style = getattr(pattern, "style", None) or getattr(pattern, "style_signature", None)
    return {"style": style, "species": species, "size": key,
            "net_qty_sf": round(net, 2), "confirmed_dcm_sf": round(c, 2),
            "implied_yield": round(c / net, 3)}


def effective_dxf_yields(seed_yields: dict,
                         observations_by_species: dict[str, list[float]] | None = None) -> dict:
    """Merge learned per-species mean implied-yield over the seeds bootstrap. A species
    with observations uses their mean; otherwise the seed. Keeps `_default`."""
    out = dict(seed_yields or {})
    for sp, ys in (observations_by_species or {}).items():
        vals = [float(y) for y in ys if y]
        if vals:
            out[sp] = round(sum(vals) / len(vals), 3)
    return out
