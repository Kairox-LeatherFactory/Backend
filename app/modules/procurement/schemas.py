"""
================================================================================
modules/procurement/schemas.py — Stage-1 API contracts (§5)
================================================================================

Request bodies are typed Pydantic models. The rich, deeply-nested success and
rejection ENVELOPES (§5a/§5b) are assembled as plain dicts by the service (the
same style the imports router uses) so the exact spec shape is preserved without
fighting nested-model coercion; their structure is documented here for the FE.
================================================================================
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class OpenSubmissionRequest(BaseModel):
    """POST /submissions — open an empty upload batch. The client may be unknown at
    upload time (it is derived from the order sheet in Stage 2), so client_id is
    optional and usually left null."""
    client_id: uuid.UUID | None = Field(default=None)


# ── Envelope shapes (for documentation / FE typing; built as dicts at runtime) ──
class SlotStatus(BaseModel):
    present: bool
    validation_status: str | None


class SubmissionSummary(BaseModel):
    order_sheet: SlotStatus
    spec_sheet: SlotStatus
    complete: bool
    ready_for_stage_2: bool
    blocking: list[str]


# ── Stage 2 — editable BOM contract (§7) ─────────────────────────────────────
class BomItemEdit(BaseModel):
    """One changed cell. `field` ∈ {dcm, qty_per_garment, unit_price}; a `dcm` edit
    is the human source-of-truth (Source 4 → dcm_source=manual, conf 1.00) and, if
    the BOM was already cutting-confirmed, re-opens the §10 gate (§9)."""
    bom_item_id: uuid.UUID
    field: str
    value: float


class BomBulkPatch(BaseModel):
    """PATCH /boms/{id}/items — bulk edit guarded by optimistic `revision` locking
    (§7b). `base_revision` is the revision the client last read; a stale one → 409
    stale_revision, a fresh one → the recomputed tree + revision+1."""
    base_revision: int = Field(..., ge=1)
    edits: list[BomItemEdit] = Field(..., min_length=1)


class BomApproveRequest(BaseModel):
    """Stage-3 approve/lock. Refused while cutting_confirmed_at is null (§10).
    Approval always locks the BOM; `lock` only selects the surfaced status label."""
    lock: bool = Field(default=False)


class BomRejectRequest(BaseModel):
    """Stage-3 MD reject (§1b). The reason is mandatory and is recorded in the
    BOM_REJECT audit `after` + on `bom.rejection_reason`."""
    reason: str = Field(..., min_length=1)


# ══════════════════════════════════════════════════════════════════════════
# Stage 5 — supplier PO (§1–§9)
# ══════════════════════════════════════════════════════════════════════════
class SupplierCreate(BaseModel):
    """POST /suppliers — the §1c 'no match → add new vendor' path (§9b)."""
    name: str = Field(..., min_length=1)
    phone: str | None = None
    email: str | None = None
    service: str | None = None
    gstin: str | None = None
    address: str | None = None
    currency: str | None = None
    payment_terms_days: int | None = None
    lead_time_days: int | None = None
    supplier_type: str | None = None
    whatsapp_phone: str | None = None


class SupplierUpdate(BaseModel):
    """PATCH /suppliers/{id} — add a missing email/phone, fix GSTIN, set type (§9b).
    Editing a contact onto a contactless matched vendor unblocks §5 email send."""
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    service: str | None = None
    gstin: str | None = None
    address: str | None = None
    currency: str | None = None
    payment_terms_days: int | None = None
    lead_time_days: int | None = None
    supplier_type: str | None = None
    whatsapp_phone: str | None = None


class PoItemEdit(BaseModel):
    """One changed PO-line cell. `field` ∈ {description, color, uom, qty, unit_price}."""
    po_item_id: uuid.UUID
    field: str
    value: object


class PoAddItem(BaseModel):
    description: str
    color: str | None = None
    uom: str | None = None
    qty: float | None = None
    unit_price: float | None = None
    bom_item_id: uuid.UUID | None = None
    inventory_item_id: uuid.UUID | None = None


class PoBulkPatch(BaseModel):
    """PATCH /pos/{id}/items — the SAME bulk-edit + optimistic `revision` contract as the
    BOM (§4). 409 stale_revision on a stale base; 409 po_locked once sent."""
    base_revision: int = Field(..., ge=1)
    item_edits: list[PoItemEdit] = Field(default_factory=list)
    po_edits: dict | None = None
    add_items: list[PoAddItem] = Field(default_factory=list)
    remove_item_ids: list[uuid.UUID] = Field(default_factory=list)


class PoRejectRequest(BaseModel):
    reason: str = Field(..., min_length=1)


class PoAcknowledgeRequest(BaseModel):
    """Manual acknowledgement (§7d) — DM/MD marks a PO confirmed. `channel` records how."""
    channel: str = Field(default="manual")
    confirmed_qty: float | None = None
    notes: str | None = None


class ProductionTransitionRequest(BaseModel):
    """Manual board transition (§8c) — e.g. RELEASED_TO_PRODUCTION (the human go/no-go)."""
    status: str = Field(..., min_length=1)
