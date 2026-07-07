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

from decimal import Decimal
from decimal import Decimal
import uuid

from pydantic import BaseModel, Field

from pydantic.types import condecimal


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
    
class DxfYieldIn(BaseModel):
    factor: condecimal(gt=0)
    note: str | None = None

class FabricRoleIn(BaseModel):
    label: str
    role: str
    category: str
    is_leather: bool = False
    
class CostLineIn(BaseModel):
    category: str
    name: str
    uom: str | None = None
    unit_price: condecimal(ge=0) | None = None
    qty_per_garment: condecimal(gt=0) = Decimal("1")

class CostCatalogPut(BaseModel):
    lines: list[CostLineIn]
    
class CheckRuleIn(BaseModel):
    id: str
    kind: str
    severity: str = "warn"
    field: str | None = None
    range: list[float] | None = None
    params: dict | None = None

class ClientChecksPut(BaseModel):
    rules: list[CheckRuleIn]
    
class PomMappingIn(BaseModel):
    source_term: str
    pom_code: str
    language: str | None = None            # inferred from the term if omitted
    garment_type_code: str | None = None   # None = applies to any garment type
    weight: int = 1
