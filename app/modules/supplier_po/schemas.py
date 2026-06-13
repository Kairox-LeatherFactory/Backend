"""
modules/supplier_po/schemas.py — Stage-5 supplier-PO API contracts (§1–§9).
Only request bodies are typed; rich response shapes are assembled by the services.

SCHEMA GUIDE (Pydantic request bodies; FastAPI validates at the router boundary)
  SupplierCreate / SupplierUpdate   POST/PATCH /suppliers bodies → create/update_supplier.
  PoItemEdit / PoAddItem            building blocks of the PO bulk patch.
  PoBulkPatch                       PATCH /pos/{id}/items body → edit_po.
  PoRejectRequest                   POST /pos/{id}/reject body {reason} → reject_po.
  PoAcknowledgeRequest              POST /pos/{id}/acknowledge body → acknowledge_po.
  ProductionTransitionRequest       POST /production-tracking/{id}/transition {status} → transition.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


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
    """PATCH /suppliers/{id} — add a missing email/phone, fix GSTIN, set type (§9b)."""
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
    """PATCH /pos/{id}/items — the SAME bulk-edit + optimistic `revision` contract as
    the BOM (§4). 409 stale_revision on a stale base; 409 po_locked once sent."""
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
