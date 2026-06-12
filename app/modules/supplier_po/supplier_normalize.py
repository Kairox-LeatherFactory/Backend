"""
================================================================================
modules/supplier_po/supplier_normalize.py — supplier text normalization (§1/§9)
================================================================================

PURE helpers, no DB, no I/O — the single place the messy
`SUPPLIERS_updated (2).xlsx` cells are cleaned, so the IMPORTER (§9a) and the
MATCHER (§1) treat a supplier name, a contact field, and a provision article
IDENTICALLY.

The two sheets are dirty in specific ways the importer must survive:
  - the contact sheet's PHONE column carries the literal string "NULL", free-text
    like "Audit maintenance", and blanks — only ~65 of 1,433 rows are a real phone;
  - EMAIL is blank for ~97% of rows;
  - SERVICE is noisy 49-distinct free text;
  - the provision DESCRIPTION is the trustworthy article evidence (normalized with
    the SAME Stage-4 `normalize_key` so a BOM term and a historical buy collapse to
    one comparable key).

FUNCTION GUIDE  (all pure + sync; shared by supplier_import + supplier_service/matcher)
  normalize_key (re-exported from inventory) — the article key, identical to Stage 4.
  clean(value) -> str | None            trimmed, null-ish ("NULL"/"-"/blank) → None.
  normalize_supplier_name(name) -> str  the dedupe key (M.V.P.N.K ≡ M V P N K).
  clean_phone(value) -> str | None      pull a real 7+ digit phone out of the messy cell.
  clean_email(value) -> str | None      a syntactically valid email or None.
  state_code_from_gstin(gstin) -> str | None   first 2 GSTIN digits (33=TN) → the GST decision.
  mode_for_bom_category(category) -> str | None  BOM category → provision MODE bucket (matcher fallback).
  supplier_type_for_mode(mode) -> str | None     dominant history mode → curated SupplierType.
  CALLED FROM: supplier_import (cleaning every cell) + supplier_service (matching + type/state defaults).
================================================================================
"""
from __future__ import annotations

import re

# Re-use the Stage-4 normalizer verbatim so the supply-history key equals the key the
# Stage-4 matcher computes on BOM lines + inventory rows (§1a "the same normalized_key").
from app.modules.inventory.inventory_normalize import normalize_key  # noqa: F401

# Cells that mean "no value" in the real workbook (case-insensitive).
_NULLISH = {"", "null", "none", "n/a", "na", "-", "nil"}

# The provision MODE → the §1b category bucket the BOM line.category maps onto.
#   MAIN/SUB material  → LEATHER
#   lining/thread/accessory/packaging → MATERIALS
#   manufacturing/service → SERVICE/JOB WORK
_BOM_CATEGORY_TO_MODE = {
    "main_material": "LEATHER",
    "sub_material": "LEATHER",
    "lining": "MATERIALS",
    "interlining": "MATERIALS",
    "thread": "MATERIALS",
    "accessory": "MATERIALS",
    "packaging": "MATERIALS",
    "manufacturing": "SERVICE",
    "fob_charge": "SERVICE",
}

# A supplier MODE → the curated SupplierType (the §2 template + §3 approver routing).
_MODE_TO_TYPE = {
    "LEATHER": "leather",
    "MATERIALS": "accessory",
    "SERVICE": "service",
    "JOB WORK": "service",
}


def clean(value) -> str | None:
    """Trimmed, whitespace-collapsed string, or None for a blank / null-ish cell."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).strip())
    if s.lower() in _NULLISH:
        return None
    return s or None


def normalize_supplier_name(name) -> str:
    """The dedupe key for a supplier (§9c): uppercase, punctuation → space, collapsed.
    Get-or-create + the importer upsert both key on this so "M.V.P.N.K" and "M V P N K"
    are one vendor."""
    s = clean(name) or ""
    s = re.sub(r"[^A-Za-z0-9]+", " ", s.upper())
    return re.sub(r"\s+", " ", s).strip()


_PHONE_RE = re.compile(r"[+()\d][\d\s\-()]{6,}")


def clean_phone(value) -> str | None:
    """Extract a usable phone from the messy PHONE cell, else None. The column carries
    "NULL", free-text job notes ("Audit maintenance"), and real numbers; keep only a
    cell that actually contains a 7+ digit run."""
    s = clean(value)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return None
    m = _PHONE_RE.search(s)
    return (m.group(0).strip() if m else s)[:50]


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def clean_email(value) -> str | None:
    """Return a syntactically valid email or None (most rows are blank)."""
    s = clean(value)
    if not s:
        return None
    m = _EMAIL_RE.search(s)
    return m.group(0).lower() if m else None


def state_code_from_gstin(gstin) -> str | None:
    """The first two digits of a GSTIN are the state code (33 = Tamil Nadu). Drives the
    §2c intra/inter-state GST decision."""
    s = clean(gstin)
    if not s or len(s) < 2 or not s[:2].isdigit():
        return None
    return s[:2]


def mode_for_bom_category(category: str | None) -> str | None:
    """Map a BOM line category to the provision MODE bucket (§1b category fallback)."""
    if not category:
        return None
    return _BOM_CATEGORY_TO_MODE.get(category.lower())


def supplier_type_for_mode(mode: str | None) -> str | None:
    """The curated SupplierType defaulting from a supplier's dominant history mode."""
    if not mode:
        return None
    return _MODE_TO_TYPE.get(mode.strip().upper())
