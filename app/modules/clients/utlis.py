import re
def _slug(s: str | None) -> str:
    """UPPER, runs of non-alnum -> single '_', trimmed. '' -> 'NA'."""
    out = re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_").upper()
    return out or "NA"
 
 
def make_sku_code(order_number: str | None, style_name: str | None,
                  colour: str | None, size: str | None) -> str:
    """Deterministic, globally-unique, readable SKU code.
    e.g. make_sku_code('JP','CLERMONT + VEST','DARK BROWN','46')
         -> 'JP-CLERMONT_VEST-DARK_BROWN-46'
    Deterministic => idempotent across re-imports (survives delete+recreate)."""
    return "-".join((
        _slug(order_number), _slug(style_name), _slug(colour), _slug(size),
    ))
 
 
# sku_label (display name) is UNCHANGED — keep it:
def sku_label(style_name, color_name, color_code, size) -> str:
    colour = color_name or color_code or "NA"
    return " · ".join(p for p in (style_name or "NA", colour, size or "NA"))