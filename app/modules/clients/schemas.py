"""Pydantic schemas — the API contract for the clients module.

These shapes ARE the contract your frontend mocks against.
"""
import uuid
from datetime import date
from pydantic import BaseModel, ConfigDict


class SKURead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    color_code: str
    color_name: str | None
    size: str
    qty_ordered: int


class StyleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    gender: str | None
    label: str | None
    article: str | None
    thickness: str | None
    skus: list[SKURead] = []


class PurchaseOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    po_number: str
    order_date: date | None
    delivery_deadline: date | None
    sea_cutoff_date: date | None
    ship_mode: str
    styles: list[StyleRead] = []


class ClientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    country: str | None


class ClientCreate(BaseModel):
    name: str
    country: str | None = None


class StyleCreate(BaseModel):
    purchase_order_id: uuid.UUID
    name: str
    gender: str | None = None
    label: str | None = None
    article: str | None = None
    thickness: str | None = None
