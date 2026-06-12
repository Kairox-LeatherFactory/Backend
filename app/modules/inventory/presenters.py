"""
================================================================================
modules/inventory/presenters.py — Stage-4 response shapes (§8)
================================================================================

PURE serialization: ORM rows + the computed line results → the exact JSON the FE
renders. Split out of the service so the service stays focused on orchestration.
Two shapes: the per-CHECK result (§8a) and the grouped DASHBOARD (§8b, client →
order → style with a status badge). No DB, no business rules.

FUNCTION GUIDE  (pure + sync; called by InventoryService to build API dicts)
  _f(v) / _iso(dt)   [private] None-safe float / ISO-timestamp formatting.
  preview_block(prev) -> dict   the importer summary (kept/dropped/warnings/rows). → preview & commit.
  item_block(i) -> dict         one inventory_item row. → list_items.
  line_view(item, line, matched_primary, result, available, reserve, on_hand, flags) -> dict
      a FRESHLY-computed check line (carries the live available/reserved). → run_check / _check_line.
  stored_line_view(item, ln, inv) -> dict
      re-render a PERSISTED line (reserved recovered as required−shortfall). → get_check/latest_for_bom.
  badge_for(lines) -> str   the BOM-level badge = the worst line status. → dashboard.
  check_view(check, line_views, excluded) -> dict   the full per-check envelope (summary +
      lines + excluded service lines). → run_check & _rebuild_view.
  dashboard_block(rows) -> dict   group flat per-BOM rows into client→order→style + totals. → dashboard.
================================================================================
"""
from __future__ import annotations

from decimal import Decimal

from app.modules.inventory.enums import InventoryLineStatus

_ZERO = Decimal("0")
# Worst-first ordering for the BOM-level badge (§8b).
_SEVERITY = {
    InventoryLineStatus.OUT_OF_STOCK.value: 2,
    InventoryLineStatus.PARTIAL.value: 1,
    InventoryLineStatus.SUFFICIENT.value: 0,
}


def _f(v):
    return float(v) if v is not None else None


def _iso(dt):
    return dt.isoformat() if dt else None


# ── inventory master ─────────────────────────────────────────────────────────
def preview_block(prev) -> dict:
    """The §2 preview/commit summary: what was kept / dropped / merged."""
    return {
        "raw_count": prev.raw_count,
        "kept": prev.kept,
        "dropped": prev.dropped,
        "warnings": prev.warnings,
        "rows": [{
            "normalized_key": r.normalized_key, "description": r.description,
            "uom": r.uom, "qty_on_hand": _f(r.qty_on_hand), "rate": _f(r.rate),
            "color": r.color, "lots": r.lots,
        } for r in prev.rows],
    }


def item_block(i) -> dict:
    return {
        "id": str(i.id), "description": i.description, "normalized_key": i.normalized_key,
        "uom": i.uom, "qty_on_hand": _f(i.qty_on_hand), "rate": _f(i.rate),
        "color": i.color, "is_active": i.is_active,
    }


# ── per-check lines (§8a) ─────────────────────────────────────────────────────
def line_view(item, line, matched_primary, result, available, reserve, on_hand, flags) -> dict:
    """A freshly-computed check line (run_check). Carries the live `available`/`reserved`
    the persisted row doesn't store."""
    return {
        "bom_item_id": str(item.id), "category": item.category,
        "name": item.name, "material_color": item.material_color,
        "required_qty": _f(line.required_qty), "uom": item.uom,
        "matched": ({
            "inventory_item_id": str(matched_primary.id),
            "description": matched_primary.description,
            "method": result.method, "uom": matched_primary.uom,
        } if matched_primary else None),
        "on_hand_qty": _f(on_hand), "available_qty": _f(available),
        "reserved_for_this_bom": _f(reserve), "shortfall_qty": _f(line.shortfall_qty),
        "status": line.status, "flags": flags,
        "suggestion": result.suggestion,
        "_rate": _f(matched_primary.rate) if matched_primary else None,
    }


def stored_line_view(item, ln, inv) -> dict:
    """Re-render a persisted check line (no recompute on read). `reserved_for_this_bom`
    is recovered as required − shortfall; the live `available` isn't persisted."""
    blob = ln.flags or {}
    reserved = (ln.required_qty or _ZERO) - (ln.shortfall_qty or _ZERO)
    return {
        "bom_item_id": str(ln.bom_item_id),
        "category": getattr(item, "category", None),
        "name": getattr(item, "name", None),
        "material_color": getattr(item, "material_color", None),
        "required_qty": _f(ln.required_qty), "uom": getattr(item, "uom", None),
        "matched": ({
            "inventory_item_id": str(inv.id), "description": inv.description,
            "method": ln.matched_method, "uom": inv.uom,
        } if inv else None),
        "on_hand_qty": _f(ln.on_hand_qty), "available_qty": _f(ln.on_hand_qty),
        "reserved_for_this_bom": _f(reserved), "shortfall_qty": _f(ln.shortfall_qty),
        "status": ln.status, "flags": blob.get("flags", []),
        "suggestion": blob.get("suggestion"),
        "_rate": _f(inv.rate) if inv else None,
    }


def badge_for(lines) -> str:
    """The BOM-level badge = the worst line status (§8b)."""
    worst = 0
    for ln in lines:
        worst = max(worst, _SEVERITY.get(ln.status, 0))
    for value, sev in _SEVERITY.items():
        if sev == worst:
            return value
    return InventoryLineStatus.SUFFICIENT.value


def check_view(check, line_views: list[dict], excluded) -> dict:
    counts = {"sufficient": 0, "partial": 0, "out_of_stock": 0}
    flag_counts = {"unmatched": 0, "uom_mismatch": 0}
    worst = 0
    shortfall_value = _ZERO
    for lv in line_views:
        counts[lv["status"]] = counts.get(lv["status"], 0) + 1
        worst = max(worst, _SEVERITY.get(lv["status"], 0))
        for fl in lv.get("flags", []):
            if fl in flag_counts:
                flag_counts[fl] += 1
        if lv.get("_rate") and lv.get("shortfall_qty"):
            shortfall_value += Decimal(str(lv["shortfall_qty"])) * Decimal(str(lv["_rate"]))
    badge = next((v for v, s in _SEVERITY.items() if s == worst),
                 InventoryLineStatus.SUFFICIENT.value)
    # strip the internal _rate before returning
    clean_lines = [{k: v for k, v in lv.items() if k != "_rate"} for lv in line_views]
    return {
        "inventory_check_id": str(check.id),
        "bom_id": str(check.bom_id),
        "status": check.status,
        "run_at": _iso(check.run_at),
        "summary": {
            "badge": badge,
            "lines_total": len(line_views),
            "sufficient": counts["sufficient"], "partial": counts["partial"],
            "out_of_stock": counts["out_of_stock"],
            "flags": flag_counts,
            "shortfall_value": float(shortfall_value.quantize(Decimal("0.01"))),
            "currency": "INR",
        },
        "lines": clean_lines,
        "excluded": [{"bom_item_id": str(i.id), "name": i.name, "category": i.category}
                     for i in excluded],
    }


# ── dashboard (§8b) ────────────────────────────────────────────────────────────
def dashboard_block(rows: list[dict]) -> dict:
    """Group the flat per-BOM rows into client → order → style with badges (§8b)."""
    clients: dict = {}
    boms_checked = 0
    sufficient = 0
    with_shortfall = 0
    for r in rows:
        boms_checked += 1
        if r["badge"] == InventoryLineStatus.SUFFICIENT.value:
            sufficient += 1
        else:
            with_shortfall += 1
        cid = str(r["client_id"]) if r["client_id"] else "unknown"
        client = clients.setdefault(cid, {
            "client_id": cid if r["client_id"] else None,
            "client_name": r["client_name"], "orders": {}})
        oid = str(r["client_order_id"])
        order = client["orders"].setdefault(oid, {
            "client_order_id": oid, "order_number": r["order_number"], "styles": []})
        order["styles"].append({
            "style_id": str(r["style_id"]), "style_name": r["style_name"],
            "bom_id": str(r["bom_id"]), "inventory_check_id": str(r["inventory_check_id"]),
            "badge": r["badge"], "shortfall_lines": r["shortfall_lines"],
            "checked_at": _iso(r["checked_at"]),
        })
    return {
        "clients": [{
            "client_id": c["client_id"], "client_name": c["client_name"],
            "orders": list(c["orders"].values()),
        } for c in clients.values()],
        "totals": {"boms_checked": boms_checked, "fully_sufficient": sufficient,
                   "with_shortfall": with_shortfall},
    }
