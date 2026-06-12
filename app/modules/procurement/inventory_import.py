"""
================================================================================
modules/procurement/inventory_import.py — normalizing inventory importer (§2)
================================================================================

PURE parse of `INVENTORY (1).xlsx` → a normalized, DEDUPED preview (no DB, no I/O
beyond reading the uploaded bytes). The service runs this in a threadpool (openpyxl
is blocking) for both `POST /inventory/preview` (dry-run) and `POST /inventory/commit`
(the upsert), mirroring the imports module's two-step, idempotent design.

The raw sheet is `DESCRIPTION | NOM | PCS | RATE`, 1,525 rows, with duplicate lots,
blank quantities, and section/ledger NOISE rows. This module:
  1. drops the noise rows (recorded, never silently swallowed),
  2. coerces PCS→qty_on_hand / RATE→rate, NOM→canonical UOM,
  3. computes the normalized_key + colour,
  4. DEDUPS by normalized_key (the 410/446/673 lots of one article) → qty_on_hand
     summed, rate qty-weighted, modal UOM (a per-key UOM clash is a warning).
================================================================================
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import openpyxl

from app.modules.procurement.inventory_normalize import (
    canonical_uom,
    extract_color,
    is_noise_row,
    normalize_key,
)


def clean_str(value) -> str | None:
    """Trimmed string with collapsed internal whitespace, or None if effectively empty
    (the same defensive cell read the imports module uses — inlined to keep this pure
    parser free of a cross-module import)."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).strip())
    return s or None


@dataclass
class InventoryRow:
    """One DEDUPED article ready to upsert into `inventory_item`."""
    normalized_key: str
    description: str            # representative raw description (kept for audit)
    uom: str | None
    qty_on_hand: Decimal
    rate: Decimal | None
    color: str | None
    lots: int                  # how many sheet rows merged into this article


@dataclass
class InventoryPreview:
    rows: list[InventoryRow] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)     # {row, description, reason}
    warnings: list[str] = field(default_factory=list)
    raw_count: int = 0

    @property
    def kept(self) -> int:
        return len(self.rows)


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d


def _resolve_columns(header: tuple) -> dict[str, int]:
    """Map DESCRIPTION/NOM/PCS/RATE → column index by header name, falling back to the
    canonical 0–3 positions if the header is absent."""
    idx = {"description": 0, "uom": 1, "qty": 2, "rate": 3}
    names = {"DESCRIPTION": "description", "NOM": "uom", "PCS": "qty", "RATE": "rate"}
    matched = False
    for i, cell in enumerate(header or ()):
        key = (clean_str(cell) or "").upper()
        if key in names:
            idx[names[key]] = i
            matched = True
    return idx if matched else {"description": 0, "uom": 1, "qty": 2, "rate": 3}


def parse_inventory(data: bytes, *, sheet_name: str | None = None) -> InventoryPreview:
    """Parse + normalize + dedup the uploaded workbook bytes into an InventoryPreview.
    Deterministic + idempotent: the same bytes always yield the same rows (the house
    invariant), so committing twice produces identical `inventory_item` rows."""
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.worksheets[0]

    preview = InventoryPreview()
    # accumulator per normalized_key
    acc: dict[str, dict] = {}
    uom_seen: dict[str, set[str]] = {}

    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    cols = _resolve_columns(header)
    preview.raw_count = 0

    for n, row in enumerate(rows, start=1):
        preview.raw_count += 1
        desc = clean_str(row[cols["description"]] if len(row) > cols["description"] else None)
        raw_uom = row[cols["uom"]] if len(row) > cols["uom"] else None
        raw_qty = row[cols["qty"]] if len(row) > cols["qty"] else None
        raw_rate = row[cols["rate"]] if len(row) > cols["rate"] else None

        if is_noise_row(desc, raw_uom, raw_qty, raw_rate):
            preview.dropped.append({"row": n, "description": desc, "reason": "non_stock_row"})
            continue

        key = normalize_key(desc)
        if not key:
            preview.dropped.append({"row": n, "description": desc, "reason": "empty_key"})
            continue

        uom = canonical_uom(raw_uom)
        qty = _to_decimal(raw_qty) or Decimal("0")
        rate = _to_decimal(raw_rate)
        color = extract_color(desc)

        bucket = acc.get(key)
        if bucket is None:
            bucket = {"description": desc, "uom": uom, "qty": Decimal("0"),
                      "rate_num": Decimal("0"), "rate_den": Decimal("0"),
                      "rate_last": None, "color": color, "lots": 0}
            acc[key] = bucket
            uom_seen[key] = set()
        bucket["qty"] += qty
        bucket["lots"] += 1
        if uom:
            uom_seen[key].add(uom)
            bucket["uom"] = bucket["uom"] or uom
        if color and not bucket["color"]:
            bucket["color"] = color
        if rate is not None:
            # qty-weighted average where qty is known; else remember the last rate.
            if qty > 0:
                bucket["rate_num"] += rate * qty
                bucket["rate_den"] += qty
            bucket["rate_last"] = rate

    for key, b in acc.items():
        if len(uom_seen.get(key, set())) > 1:
            preview.warnings.append(
                f"UOM disagreement for '{key}': {sorted(uom_seen[key])} — kept '{b['uom']}'")
        if b["rate_den"] > 0:
            rate = (b["rate_num"] / b["rate_den"]).quantize(Decimal("0.01"))
        else:
            rate = b["rate_last"]
        preview.rows.append(InventoryRow(
            normalized_key=key, description=b["description"], uom=b["uom"],
            qty_on_hand=b["qty"].quantize(Decimal("0.001")), rate=rate,
            color=b["color"], lots=b["lots"],
        ))
    preview.rows.sort(key=lambda r: r.normalized_key)
    return preview
