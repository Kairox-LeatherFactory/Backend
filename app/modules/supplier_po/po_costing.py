"""
================================================================================
modules/supplier_po/po_costing.py — PO line + GST math (§2c)
================================================================================

PURE, server-side recompute of a PO's money (no DB) — the house rule "all amounts
server-computed at write time", echoed by the editable-PO recompute (§4).

The real suppler-po-form PDFs show CGST 6% + SGST 6% (12% intra-state, both parties
in Tamil Nadu) — NOT the 2.5% the stage-0 prose guessed. An out-of-state supplier is
inter-state → IGST 12% instead of CGST+SGST. The rate + the intra/inter decision are
CONFIG + per-supplier (the supplier's GSTIN state code vs the buyer's 33…), never
hard-coded:

    subtotal  = Σ (qty × unit_price)
    INTRA → cgst = sgst = subtotal × rate/2
    INTER → igst = subtotal × rate
    round_off = round(subtotal + tax) − (subtotal + tax)
    total     = subtotal + tax + round_off

FUNCTION GUIDE  (pure + sync; the analogue of bom/costing.py for POs)
  _q(v)   [private] quantize to 0.01.
  line_amount(qty, unit_price) -> Decimal   one PO line's amount.
  gst_mode(supplier_state_code, buyer_state_code) -> "INTRA" | "INTER"
      same state → CGST+SGST; different → IGST; unknown → conservative INTRA (TN).
  compute_totals(items, *, supplier_state_code, buyer_state_code, gst_rate) -> dict
      Recompute subtotal/cgst/sgst/igst/round_off/total + each line `amount` (positional).
      CALLED FROM: PoService._recompute (generation + every PO bulk-PATCH edit).
================================================================================
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_ZERO = Decimal("0")
_CENTS = Decimal("0.01")

GST_INTRA = "INTRA"
GST_INTER = "INTER"


def _q(v: Decimal) -> Decimal:
    return v.quantize(_CENTS, rounding=ROUND_HALF_UP)


def line_amount(qty, unit_price) -> Decimal:
    q = Decimal(str(qty)) if qty is not None else _ZERO
    p = Decimal(str(unit_price)) if unit_price is not None else _ZERO
    return _q(q * p)


def gst_mode(supplier_state_code: str | None, buyer_state_code: str) -> str:
    """INTRA when supplier + buyer share a state (CGST+SGST); INTER otherwise (IGST).
    An unknown supplier state is treated conservatively as INTRA (the common TN case,
    matching the real forms)."""
    if supplier_state_code and supplier_state_code != buyer_state_code:
        return GST_INTER
    return GST_INTRA


def compute_totals(items: list[dict], *, supplier_state_code: str | None,
                   buyer_state_code: str, gst_rate: float) -> dict:
    """Recompute subtotal / cgst / sgst / igst / round_off / total for a PO.
    `items` = dicts with `qty` + `unit_price`. Returns the money dict + each line's
    recomputed `amount` (positional, aligned with `items`)."""
    rate = Decimal(str(gst_rate)) / Decimal("100")
    mode = gst_mode(supplier_state_code, buyer_state_code)

    amounts: list[Decimal] = []
    subtotal = _ZERO
    for it in items:
        amt = line_amount(it.get("qty"), it.get("unit_price"))
        amounts.append(amt)
        subtotal += amt
    subtotal = _q(subtotal)

    cgst = sgst = igst = _ZERO
    if mode == GST_INTER:
        igst = _q(subtotal * rate)
    else:
        half = _q(subtotal * rate / Decimal("2"))
        cgst = sgst = half
    tax = cgst + sgst + igst
    pre_round = subtotal + tax
    total = pre_round.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    round_off = _q(total - pre_round)

    return {
        "gst_mode": mode,
        "subtotal": subtotal,
        "cgst": cgst, "sgst": sgst, "igst": igst,
        "round_off": round_off,
        "total": _q(Decimal(total)),
        "amounts": amounts,
    }
