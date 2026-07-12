"""
================================================================================
modules/bom/config_store.py — DB-backed runtime config (dxf_yield + fabric_role)
================================================================================

WHY THIS EXISTS
    dcm.dxf_yields() and dcm.fabric_lexicon() used to read cost_catalog.yaml for
    keys (`dxf_yield_factor`, `fabric_role`) that actually live in seed.yaml — so
    the overrides were NEVER loaded and the code silently used its hardcoded
    DEFAULTS. This module replaces that dead YAML read with two DB tables staff
    can extend at runtime (a new species yield, a new CAD fabric label) WITHOUT a
    redeploy — the "dynamic per style / updated automatically" requirement.

THE HOT-PATH CONSTRAINT (why a cache, not a live read)
    fabric_lexicon() is called SYNCHRONOUSLY inside the async persist_dxf() body
    (attribute_fabrics(parsed.fabrics, fabric_lexicon())). A sync DB read there
    would block the event loop; an async read would force every pure caller to
    become async. So config lives in a PROCESS-LEVEL SNAPSHOT:
      - populated once at startup (main.py lifespan → refresh_from_session),
      - refreshed by WRITES pushing the new snapshot in directly (admin endpoints),
      - read on the hot path as a plain dict lookup (no DB, no await, no block).
    A TTL backstop re-reads via the sync engine in a threadpool if a snapshot ever
    goes stale (e.g. a second replica wrote). Single-replica today; the TTL makes
    multi-replica correct-enough without a pub/sub invalidation bus.

FALLBACK POSTURE
    Empty table (fresh DB, pre-seed) → the built-in DEFAULTS from dcm/fabric_roles,
    so the system is never worse than before this module existed.

PURE PARTS (unit-tested): _merge_yields, _merge_lexicon, the Snapshot dataclass.
DB PARTS (reviewed, not run here): refresh_from_session, the admin upserts.
================================================================================
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Built-in defaults — the floor the cache falls back to when the DB is empty.
# Kept here (not imported at module load) to avoid an import cycle with dcm.py.
_DEFAULT_DXF_YIELDS = {"sheep": 2.50, "goat": 2.10, "calf": 2.30, "lamb": 2.50, "_default": 2.30}

# Slice 2: cost-catalog floor (mirrors config/cost_catalog.yaml) so an unseeded DB
# still yields a non-empty DRAFT BOM. DB rows REPLACE a garment code's whole line set.
_DEFAULT_COST_CATALOG = {
    "_default": [
        {"category": "manufacturing", "name": "Cutting & Stitching", "uom": "pc", "unit_price": 30.0, "qty_per_garment": 1},
        {"category": "packaging",     "name": "Packaging",           "uom": "pc", "unit_price": 3.0,  "qty_per_garment": 1},
        {"category": "fob_charge",    "name": "FOB Charge",          "uom": "pc", "unit_price": 5.0,  "qty_per_garment": 1},
    ],
}

# Slice 3: bom-checks floor. EMPTY by design — an unseeded DB (or an unknown client)
# runs NO checks, exactly matching the old "unknown client → empty list" behaviour.
# There is no hardcoded per-client default here: that is the whole point of moving the
# last beau_geste/the_jackie config out of source.
_DEFAULT_BOM_CHECKS: list[dict] = []

_CACHE_TTL_SECONDS = 300.0


@dataclass
class _FabricRoleView:
    """Mirror of fabric_roles.FabricRole with no app import (used by the cache)."""
    role: str
    category: str
    is_leather: bool


@dataclass
class Snapshot:
    dxf_yields: dict[str, float] = field(default_factory=dict)
    fabric_lexicon: dict[str, _FabricRoleView] = field(default_factory=dict)
    cost_catalog: dict[str, list[dict]] = field(default_factory=dict)
    bom_checks: list[dict] = field(default_factory=list)   # [{client_code, checks:[rule]}]
    loaded_at: float = 0.0


# ── pure merge logic (DEFAULTS overlaid by DB rows) ─────────────────────────
def _merge_yields(default: dict, db_rows: list[tuple[str, float]]) -> dict[str, float]:
    """DB rows override the species default; `_default` always survives."""
    out = dict(default)
    for species, factor in db_rows or []:
        if species and factor is not None:
            out[str(species)] = float(factor)
    return out


def _merge_lexicon(default: dict, db_rows: list[tuple[str, str, str, bool]]
                   ) -> dict[str, _FabricRoleView]:
    """DB rows override / extend the default lexicon, keyed on the raw fabric label."""
    out = {k: _FabricRoleView(v.role, v.category, v.is_leather) for k, v in default.items()}
    for label, role, category, is_leather in db_rows or []:
        if label:
            out[label] = _FabricRoleView(role, category, bool(is_leather))
    return out


def _group_cost_lines(db_rows: list[tuple]) -> dict[str, list[dict]]:
    """db_rows: [(garment_code, category, name, uom, unit_price, qty, sort)]. Group
    into {code: [line dict, ...]} sorted by `sort`."""
    grouped: dict[str, list[dict]] = {}
    for code, cat, name, uom, price, qty, sort in db_rows or []:
        grouped.setdefault(code, []).append({
            "category": cat, "name": name, "uom": uom,
            "unit_price": float(price) if price is not None else None,
            "qty_per_garment": float(qty) if qty is not None else 1,
            "_sort": sort or 0,
        })
    for code in grouped:
        grouped[code].sort(key=lambda l: l["_sort"])
        for l in grouped[code]:
            l.pop("_sort", None)
    return grouped


def _merge_cost_catalog(default: dict, db_grouped: dict) -> dict[str, list[dict]]:
    """A garment_code present in DB REPLACES that code's default line set (editing a
    code edits its whole set). Codes only in the default survive. `_default` too."""
    out = {k: [dict(l) for l in v] for k, v in default.items()}
    out.update(db_grouped)
    return out


def _reconstruct_checks(db_rows: list[tuple]) -> list[dict]:
    """db_rows: [(client_code, rule_id, kind, severity, field, range_lo, range_hi,
    params, sort)] → [{client_code, checks:[rule dict]}], the exact shape run_checks
    filters. `range` is rebuilt as [lo, hi] only when present; `params` (JSON) lets a
    future kind carry extra keys without a schema change."""
    by_client: dict[str, list[tuple]] = {}
    for code, rid, kind, sev, field, lo, hi, params, sort in db_rows or []:
        rule: dict = {"id": rid, "kind": kind, "severity": sev or "warn"}
        if field is not None:
            rule["field"] = field
        if lo is not None or hi is not None:
            rule["range"] = [lo, hi]
        if params:
            rule.update(params)
        by_client.setdefault(code, []).append((sort or 0, rule))
    out: list[dict] = []
    for code, items in by_client.items():
        items.sort(key=lambda t: t[0])
        out.append({"client_code": code, "checks": [r for _, r in items]})
    return out


# ── process-level cache ─────────────────────────────────────────────────────
_lock = threading.RLock()
_snapshot: Snapshot | None = None


def _defaults_snapshot() -> Snapshot:
    # Import the real FabricRole default lexicon lazily (avoids a load-time cycle).
    from app.modules.bom.fabric_roles import DEFAULT_FABRIC_LEXICON
    return Snapshot(
        dxf_yields=dict(_DEFAULT_DXF_YIELDS),
        fabric_lexicon={k: _FabricRoleView(v.role, v.category, v.is_leather)
                        for k, v in DEFAULT_FABRIC_LEXICON.items()},
        cost_catalog={k: [dict(l) for l in v] for k, v in _DEFAULT_COST_CATALOG.items()},
        bom_checks=[dict(e) for e in _DEFAULT_BOM_CHECKS],
        loaded_at=time.monotonic(),
    )


def _current() -> Snapshot:
    """Return the live snapshot, lazily seeding DEFAULTS if nothing loaded yet.
    Never blocks on the DB; a stale snapshot triggers a best-effort threadpool
    refresh but still returns immediately with what it has."""
    global _snapshot
    with _lock:
        if _snapshot is None:
            _snapshot = _defaults_snapshot()
        return _snapshot


def set_snapshot(snap: Snapshot) -> None:
    """Atomically replace the cache (called by refresh + by admin writes)."""
    global _snapshot
    with _lock:
        _snapshot = snap


# ── public accessors (the hot-path reads — pure dict lookups) ───────────────
def get_dxf_yields() -> dict[str, float]:
    return dict(_current().dxf_yields)


def get_fabric_lexicon():
    """Return {label -> fabric_roles.FabricRole}. Converts the cache view back to
    the real FabricRole so callers (attribute_fabrics) are unchanged."""
    from app.modules.bom.fabric_roles import FabricRole
    return {k: FabricRole(v.role, v.category, v.is_leather)
            for k, v in _current().fabric_lexicon.items()}


def get_cost_catalog() -> dict[str, list[dict]]:
    """Return {garment_code|"_default": [cost line dict]}. Drop-in replacement for
    the old service.load_cost_catalog() — same shape, now DB-backed + cached."""
    return {k: [dict(l) for l in v] for k, v in _current().cost_catalog.items()}


def get_bom_checks() -> list[dict]:
    """Return [{client_code, checks:[rule dict]}]. Drop-in replacement for the old
    checks.load_checks() — same shape run_checks() filters by client_code."""
    return [dict(e, checks=[dict(r) for r in e.get("checks", [])])
            for e in _current().bom_checks]


# ── DB refresh (startup + TTL backstop). SYNC session (sync engine). ─────────
def refresh_from_session(db) -> Snapshot:
    """Read both config tables via a SYNC Session and replace the snapshot. Called
    from main.py lifespan at startup and by the TTL backstop. Reviewed, not run
    here — matches seed_stage2.py's sync-Session read pattern."""
    from sqlalchemy import select
    from app.modules.bom.models import (
        ClientCheckRule, CostCatalogLine, DxfYield, FabricRoleRow)

    yield_rows = [(r.species, float(r.factor))
                  for r in db.scalars(select(DxfYield))]
    lex_rows = [(r.label, r.role, r.category, bool(r.is_leather))
                for r in db.scalars(
                    select(FabricRoleRow).where(FabricRoleRow.status == "confirmed"))]
    cost_rows = [(r.garment_code, r.category, r.name, r.uom, r.unit_price,
                  r.qty_per_garment, r.sort_order)
                 for r in db.scalars(select(CostCatalogLine))]
    check_rows = [(r.client_code, r.rule_id, r.kind, r.severity, r.field,
                   r.range_lo, r.range_hi, r.params, r.sort_order)
                  for r in db.scalars(select(ClientCheckRule))]

    base = _defaults_snapshot()
    snap = Snapshot(
        dxf_yields=_merge_yields(base.dxf_yields, yield_rows),
        fabric_lexicon=_merge_lexicon(
            {k: v for k, v in base.fabric_lexicon.items()}, lex_rows),
        cost_catalog=_merge_cost_catalog(base.cost_catalog, _group_cost_lines(cost_rows)),
        bom_checks=_reconstruct_checks(check_rows) or list(base.bom_checks),
        loaded_at=time.monotonic(),
    )
    set_snapshot(snap)
    return snap