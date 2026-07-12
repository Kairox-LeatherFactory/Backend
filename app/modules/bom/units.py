from decimal import Decimal

SF_TO_DM2 = Decimal("9.290304")                       # 1 sf = 9.290304 dm²
_AREA_TO_DM2 = {"dm2": Decimal("1"), "dm²": Decimal("1"),
                "sf": SF_TO_DM2, "sqft": SF_TO_DM2, "sq ft": SF_TO_DM2, "ft2": SF_TO_DM2}

def normalize_price(unit_price, price_uom, qty_uom):
    """Convert $/price_uom -> $/qty_uom so `qty(qty_uom) × price` is dimensionally
    correct. Area units reconcile via 1 sf = 9.290304 dm²; identical or non-area
    units pass through unchanged. This is the single guard against the 9.29× error."""
    if unit_price is None:
        return unit_price
    p, q = (price_uom or "").strip().lower(), (qty_uom or "").strip().lower()
    if p == q or not p or not q:
        return Decimal(str(unit_price))
    pf, qf = _AREA_TO_DM2.get(p), _AREA_TO_DM2.get(q)
    if pf is None or qf is None:
        return Decimal(str(unit_price))                # unknown/non-area: no conversion
    return Decimal(str(unit_price)) / pf * qf          # $/price_uom -> $/dm² -> $/qty_uom