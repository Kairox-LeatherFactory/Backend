"""
modules/bom/schemas.py — Stage-2/3 BOM API contracts (the editable-BOM bulk-PATCH
+ approval lifecycle bodies). Rich response envelopes are assembled as dicts in the
service; only request bodies are typed here.

SCHEMA GUIDE (Pydantic request bodies; FastAPI validates them at the router boundary)
  BomItemEdit        one changed cell {bom_item_id, field, value}; used inside BomBulkPatch.
  BomBulkPatch       PATCH /boms/{id}/items body — {base_revision, edits[]}. → edit_bom_items.
  BomApproveRequest  POST /boms/{id}/approve body — {lock}. → approve_bom.
  BomRejectRequest   POST /boms/{id}/reject body — {reason} (mandatory). → reject_bom.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class BomItemEdit(BaseModel):
    """One changed cell. `field` ∈ {dcm, qty_per_garment, unit_price}; a `dcm` edit is
    the human source-of-truth (Source 4 → dcm_source=manual) and, if the BOM was already
    cutting-confirmed, re-opens the §10 gate."""
    bom_item_id: uuid.UUID
    field: str
    value: float


class BomBulkPatch(BaseModel):
    """PATCH /boms/{id}/items — bulk edit guarded by optimistic `revision` locking.
    A stale `base_revision` → 409 stale_revision; a fresh one → the recomputed tree."""
    base_revision: int = Field(..., ge=1)
    edits: list[BomItemEdit] = Field(..., min_length=1)


class BomApproveRequest(BaseModel):
    """Stage-3 approve/lock. Refused while cutting_confirmed_at is null (§10)."""
    lock: bool = Field(default=False)


class BomRejectRequest(BaseModel):
    """Stage-3 MD reject (§1b). The reason is mandatory."""
    reason: str = Field(..., min_length=1)
