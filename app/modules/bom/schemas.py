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

from typing import Annotated
from decimal import Decimal
import uuid

from pydantic import BaseModel, Field

from pydantic.types import condecimal
import uuid
from pydantic import BaseModel, Field, model_validator


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
    factor:  Annotated[Decimal, Field(gt=0)]
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
    unit_price: Annotated[Decimal, Field(ge=0)] | None = None
    qty_per_garment: Annotated[Decimal, Field(gt=0)] = Decimal("1")

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


class ColorOut(BaseModel):
    color_key: str
    color_label: str
    qty: int
    per_size_qty: dict[str, int]
    warnings: list[str] = Field(default_factory=list)


class OrderStyleOut(BaseModel):
    id: uuid.UUID
    style_signature: str
    style_name: str
    material: str | None
    qty: int
    per_size_qty: dict[str, int]
    colors: list[ColorOut]
    warnings: list[str]
    # matching state the UI renders as chips
    spec_document_id: uuid.UUID | None
    spec_match_status: str                    # none | suggested | confirmed
    pattern_reference_id: uuid.UUID | None
    dxf_match_status: str                     # none | suggested | confirmed
    bom_id: uuid.UUID | None                  # set once generated

    model_config = {"from_attributes": True}


class BreakdownOut(BaseModel):
    submission_id: uuid.UUID
    status: str                               # not_started | processing | ready
    styles: list[OrderStyleOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BreakdownAccepted(BaseModel):
    submission_id: uuid.UUID
    status: str                               # queued | already_ready | already_processing
    task_id: str | None = None


class AttachmentsIn(BaseModel):
    """Confirm/override the suggested spec/DXF for one style.
    Explicit id => confirm THAT document/pattern (override).
    Omitted id  => accept the current suggestion (if any).
    clear_*     => detach. clear_x and x_id together is a contradiction."""
    spec_document_id: uuid.UUID | None = None
    pattern_reference_id: uuid.UUID | None = None
    clear_spec: bool = False
    clear_dxf: bool = False

    @model_validator(mode="after")
    def _no_contradiction(self):
        if self.clear_spec and self.spec_document_id is not None:
            raise ValueError("clear_spec and spec_document_id are mutually exclusive")
        if self.clear_dxf and self.pattern_reference_id is not None:
            raise ValueError("clear_dxf and pattern_reference_id are mutually exclusive")
        return self


class StyleBomAccepted(BaseModel):
    order_style_id: uuid.UUID
    status: str                               # queued | already_generated
    bom_id: uuid.UUID | None = None
    task_id: str | None = None
