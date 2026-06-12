"""
================================================================================
modules/procurement/supplier_import.py — supplier workbook importer (§9a/§1d)
================================================================================

PURE parse of `SUPPLIERS_updated (2).xlsx` → a normalized preview (no DB, no I/O
beyond the uploaded bytes). The service runs it in a threadpool (openpyxl is
blocking) for `POST /suppliers/import/preview` (dry-run) and `.../commit` (the
upsert) — the same two-step idempotent design as the Stage-4 inventory importer.

The workbook has TWO sheets that tell two stories (§0):
  - `Supplier contact info` (1,433 × COMPANY NAME·PHONE·EMAIL·SERVICE) — the sparse
    contact directory feeding `supplier` (the SEND target). Only ~65 phones, ~45
    emails are real; the rest are "NULL"/blank/free-text.
  - `Suppliers provision` (1,524 txns) — the purchase LEDGER, the real
    article→supplier evidence, aggregated here into `supplier_supply_history`
    (§1d): per (supplier, normalized_description) the mode, txn_count, recency, and
    the rate band — the matcher's index.

Output is deterministic + idempotent: the same bytes always yield the same rows, so
committing twice produces identical `supplier` + `supplier_supply_history` rows.
================================================================================
"""
from __future__ import annotations

import io
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import openpyxl

from app.modules.inventory.inventory_normalize import canonical_uom, normalize_key
from app.modules.supplier_po.supplier_normalize import (
    clean,
    clean_email,
    clean_phone,
    normalize_supplier_name,
)

_CONTACT_SHEET = "Supplier contact info"
_PROVISION_SHEET = "Suppliers provision"


@dataclass
class SupplierRow:
    """One vendor ready to upsert into `supplier` (keyed on `normalized_name`)."""
    name: str                 # representative raw name (kept for display)
    normalized_name: str
    phone: str | None = None
    email: str | None = None
    service: str | None = None


@dataclass
class SupplyHistoryRow:
    """One aggregated (supplier, article) row for `supplier_supply_history` (§1d)."""
    supplier_normalized_name: str
    supplier_name: str
    normalized_description: str
    raw_description: str
    mode: str | None
    uom: str | None
    txn_count: int
    first_purchased_at: date | None
    last_purchased_at: date | None
    last_rate: Decimal | None
    min_rate: Decimal | None
    max_rate: Decimal | None


@dataclass
class SupplierPreview:
    suppliers: list[SupplierRow] = field(default_factory=list)
    history: list[SupplyHistoryRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    contact_rows: int = 0
    provision_rows: int = 0

    @property
    def suppliers_with_phone(self) -> int:
        return sum(1 for s in self.suppliers if s.phone)

    @property
    def suppliers_with_email(self) -> int:
        return sum(1 for s in self.suppliers if s.email)


def _to_decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _header_index(header: tuple, names: dict[str, str]) -> dict[str, int]:
    """Map canonical field → column index by (trimmed, upper) header label."""
    idx: dict[str, int] = {}
    for i, cell in enumerate(header or ()):
        key = (clean(cell) or "").upper()
        if key in names:
            idx[names[key]] = i
    return idx


def _parse_contacts(ws) -> tuple[dict[str, SupplierRow], int]:
    """`Supplier contact info` → {normalized_name: SupplierRow}. Last non-blank cell
    wins on a duplicate name (sheet-order-stable)."""
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    cols = _header_index(header, {
        "COMPANY NAME": "name", "PHONE": "phone", "EMAIL": "email", "SERVICE": "service",
    })
    name_i = cols.get("name", 0)
    out: dict[str, SupplierRow] = {}
    count = 0
    for row in rows:
        if not row:
            continue
        raw_name = clean(row[name_i] if len(row) > name_i else None)
        if not raw_name:
            continue
        count += 1
        norm = normalize_supplier_name(raw_name)
        if not norm:
            continue
        phone = clean_phone(row[cols["phone"]]) if "phone" in cols and len(row) > cols["phone"] else None
        email = clean_email(row[cols["email"]]) if "email" in cols and len(row) > cols["email"] else None
        service = clean(row[cols["service"]]) if "service" in cols and len(row) > cols["service"] else None
        existing = out.get(norm)
        if existing is None:
            out[norm] = SupplierRow(name=raw_name, normalized_name=norm,
                                    phone=phone, email=email, service=service)
        else:   # contact fill-if-present (never blank a known value with a later gap)
            existing.phone = existing.phone or phone
            existing.email = existing.email or email
            existing.service = existing.service or service
    return out, count


def _parse_provision(ws) -> tuple[dict[tuple, dict], int]:
    """`Suppliers provision` → accumulator keyed (normalized_name, normalized_desc)."""
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    cols = _header_index(header, {
        "DATE": "date", "MODE": "mode", "COMPANY NAME": "name",
        "DESCRIPTION": "desc", "NOM": "uom", "RATE": "rate",
    })
    name_i = cols.get("name")
    desc_i = cols.get("desc")
    if name_i is None or desc_i is None:
        return {}, 0
    acc: dict[tuple, dict] = {}
    count = 0
    for row in rows:
        if not row or len(row) <= max(name_i, desc_i):
            continue
        raw_name = clean(row[name_i])
        raw_desc = clean(row[desc_i])
        if not raw_name or not raw_desc:
            continue
        norm_name = normalize_supplier_name(raw_name)
        norm_desc = normalize_key(raw_desc)
        if not norm_name or not norm_desc:
            continue
        count += 1
        key = (norm_name, norm_desc)
        dt = _to_date(row[cols["date"]]) if "date" in cols and len(row) > cols["date"] else None
        mode = clean(row[cols["mode"]]).upper() if "mode" in cols and len(row) > cols["mode"] and clean(row[cols["mode"]]) else None
        uom = canonical_uom(row[cols["uom"]]) if "uom" in cols and len(row) > cols["uom"] else None
        rate = _to_decimal(row[cols["rate"]]) if "rate" in cols and len(row) > cols["rate"] else None

        b = acc.get(key)
        if b is None:
            b = {"supplier_name": raw_name, "raw_description": raw_desc,
                 "modes": Counter(), "uom": None, "txn_count": 0,
                 "first": None, "last": None, "last_rate": None,
                 "min_rate": None, "max_rate": None}
            acc[key] = b
        b["txn_count"] += 1
        if mode:
            b["modes"][mode] += 1
        if uom and not b["uom"]:
            b["uom"] = uom
        if dt is not None:
            b["first"] = dt if b["first"] is None else min(b["first"], dt)
            if b["last"] is None or dt >= b["last"]:
                b["last"] = dt
                if rate is not None:
                    b["last_rate"] = rate
        if rate is not None and rate > 0:
            b["min_rate"] = rate if b["min_rate"] is None else min(b["min_rate"], rate)
            b["max_rate"] = rate if b["max_rate"] is None else max(b["max_rate"], rate)
            if b["last_rate"] is None:
                b["last_rate"] = rate
    return acc, count


def parse_suppliers(data: bytes) -> SupplierPreview:
    """Parse + normalize + aggregate the supplier workbook into a SupplierPreview.
    Suppliers = the union of the contact directory and every vendor named in the
    provision ledger (so a ledger-only vendor is still a get-or-createable supplier)."""
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    preview = SupplierPreview()

    contact_ws = wb[_CONTACT_SHEET] if _CONTACT_SHEET in wb.sheetnames else None
    provision_ws = wb[_PROVISION_SHEET] if _PROVISION_SHEET in wb.sheetnames else None

    suppliers: dict[str, SupplierRow] = {}
    if contact_ws is not None:
        suppliers, preview.contact_rows = _parse_contacts(contact_ws)
    else:
        preview.warnings.append(f"sheet '{_CONTACT_SHEET}' not found — no contact data")

    acc: dict[tuple, dict] = {}
    if provision_ws is not None:
        acc, preview.provision_rows = _parse_provision(provision_ws)
    else:
        preview.warnings.append(f"sheet '{_PROVISION_SHEET}' not found — no supply history")

    # union: ensure every provision vendor has a supplier row (ledger-only vendors)
    for (norm_name, _desc), b in acc.items():
        if norm_name not in suppliers:
            suppliers[norm_name] = SupplierRow(name=b["supplier_name"],
                                               normalized_name=norm_name)

    preview.suppliers = sorted(suppliers.values(), key=lambda s: s.normalized_name)
    for (norm_name, norm_desc), b in acc.items():
        mode = b["modes"].most_common(1)[0][0] if b["modes"] else None
        preview.history.append(SupplyHistoryRow(
            supplier_normalized_name=norm_name,
            supplier_name=suppliers[norm_name].name,
            normalized_description=norm_desc, raw_description=b["raw_description"],
            mode=mode, uom=b["uom"], txn_count=b["txn_count"],
            first_purchased_at=b["first"], last_purchased_at=b["last"],
            last_rate=b["last_rate"], min_rate=b["min_rate"], max_rate=b["max_rate"],
        ))
    preview.history.sort(key=lambda h: (h.supplier_normalized_name, h.normalized_description))

    if preview.suppliers and preview.suppliers_with_email == 0:
        preview.warnings.append("no usable email on any supplier — sends will route to "
                                "WhatsApp/call or be held no_contact_channel (§5b)")
    return preview
