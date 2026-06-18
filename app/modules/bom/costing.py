"""
================================================================================
modules/bom/costing.py — BOM cost math (Stage 2 §5, grounded in BMO-1)
================================================================================

The worked reference is BMO-1 (CRIMIE / CRI 02F5 PL02, FOB US$107.25). Pure math —
no DB, no I/O — so it is trivially testable and reused by both BOM generation and
the bulk-PATCH recompute (§7). The house rule (CLAUDE.md §7) holds: compute at
write time, return the recomputed tree, NEVER recompute on read or trust the client.

PER LINE (one bom_item)
    total_cost  = qty_per_garment × unit_price        # per-garment ("Sample Mass")
    bulk_qty    = order_qty × qty_per_garment          # e.g. 60 × 34.5 = 2,070 dm²
    line_bulk   = order_qty × total_cost               # e.g. 60 × 62.1 = $3,726

PER BOM (header rollup)
    garment_fob_price = Σ line.total_cost              # BMO-1 TOTAL = 107.25
    bulk_total        = Σ line_bulk = order_qty × fob  # 60 × 107.25 = 6,435

A material line's qty_per_garment IS its DCM (§2). Non-material lines (manufacturing
30.00, packaging 3.00, FOB charge 5.00) have qty_per_garment 1 and the cost in
unit_price. Buyer-supplied accessories carry unit_price 0 (BMO-1 buttons).

FUNCTION GUIDE
  _d(v) / _money(v) / _qty(v)   [private]
      Coercion + rounding helpers. `_d` turns anything (None/str/float) into a
      Decimal; `_money` quantizes to 0.01 (paise/cents), `_qty` to 0.001. Keep all
      arithmetic in Decimal so money never drifts. Called only inside this file.
  compute_line(qty_per_garment, unit_price, order_qty) -> dict
      Recompute ONE line's derived numbers. qty_per_garment defaults to 1 for
      non-material lines. Returns {total_cost, bulk_qty, line_bulk}. Called by
      recompute_bom (below) once per line.
  recompute_bom(lines, order_qty) -> dict
      Roll the per-line math up to the header. `lines` = list of
      {qty_per_garment, unit_price}. Returns {order_qty, garment_fob_price,
      bulk_total, lines:[derived...]} — the full recomputed tree.
      CALLED FROM: BomService._recompute (generation + every bulk-PATCH edit);
      supplier_po/po_costing reuses the same shape for PO totals. Pure + sync —
      no DB, no I/O — so it is trivially unit-testable.
================================================================================
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_CENTS = Decimal("0.01")
_QTY = Decimal("0.001")


def _d(v) -> Decimal:
    if v is None:
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
        
    try:
        # 1. Strip whitespace to handle accidental spaces (e.g. "   ")
        cleaned_str = str(v).strip()
        
        # 2. If the field was left completely blank, safely return 0
        if not cleaned_str:
            return Decimal("0")
            
        # 3. Attempt the conversion
        return Decimal(cleaned_str)
        
    except InvalidOperation:
        # 4. Catch invalid text inputs (like "N/A" or "TBD") and fallback to 0
        return Decimal("0")   #might need exception handling in case person forgets to put into fields


def _money(v: Decimal) -> Decimal:
    return v.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _qty(v: Decimal) -> Decimal:
    return v.quantize(_QTY, rounding=ROUND_HALF_UP)


def compute_line(qty_per_garment, unit_price, order_qty) -> dict:
    """Recompute one line's derived numbers. qty_per_garment defaults to 1 for
    non-material lines (manufacturing/packaging/FOB). Returns the fields to write."""
    qpg = _d(qty_per_garment) if qty_per_garment is not None else Decimal("1")
    price = _d(unit_price)
    oq = _d(order_qty)
    total = _money(qpg * price)              # per-garment line cost
    bulk_qty = _qty(oq * qpg)
    line_bulk = _money(oq * total)           # this line's bulk amount
    return {"total_cost": total, "bulk_qty": bulk_qty, "line_bulk": line_bulk}


def recompute_bom(lines: list[dict], order_qty) -> dict:
    """Roll the per-line math up to the BOM header.

    `lines` is a list of {qty_per_garment, unit_price} dicts (or objects with those
    attrs). Returns {order_qty, garment_fob_price, bulk_total, lines:[{...derived}]}
    — the full recomputed tree the bulk-PATCH response echoes back (§7b)."""
    oq = _d(order_qty)
    out_lines: list[dict] = []
    fob = Decimal("0")
    bulk_total = Decimal("0")
    for ln in lines:
        qpg = ln.get("qty_per_garment") if isinstance(ln, dict) else getattr(ln, "qty_per_garment", None)
        price = ln.get("unit_price") if isinstance(ln, dict) else getattr(ln, "unit_price", None)
        d = compute_line(qpg, price, oq)
        fob += d["total_cost"]
        bulk_total += d["line_bulk"]
        out_lines.append(d)
    return {
        "order_qty": int(oq) if oq == oq.to_integral_value() else float(oq),
        "garment_fob_price": _money(fob),
        "bulk_total": _money(bulk_total),
        "lines": out_lines,
    }
