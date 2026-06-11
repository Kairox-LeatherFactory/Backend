"""Pydantic schemas — the API contract for the clients module.

These shapes ARE the contract your frontend mocks against.

CONTRACT NOTE (Stage 0): the buyer order was renamed PurchaseOrder -> ClientOrder
and `po_number` -> `order_number`. New BOM-workflow fields are optional/nullable
so existing consumers keep working.
"""
import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class SKURead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    color_code: str
    color_name: str | None
    size: str
    qty_ordered: int
    nylon_color: str | None = None
    knit_color: str | None = None


class StyleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    gender: str | None
    label: str | None
    article: str | None
    thickness: str | None
    season: str | None = None
    customer_ref: str | None = None
    internal_ref: str | None = None
    unit_price: Decimal | None = None
    currency: str | None = None
    skus: list[SKURead] = []


class ClientOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    order_number: str
    order_date: date | None
    delivery_deadline: date | None
    sea_cutoff_date: date | None
    ship_mode: str
    currency: str | None = None
    agent: str | None = None
    line: str | None = None
    styles: list[StyleRead] = []


class ClientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    country: str | None
    code: str | None = None
    currency: str | None = None
    default_size_system: str | None = None


class ClientCreate(BaseModel):
    name: str
    country: str | None = None


class StyleCreate(BaseModel):
    client_order_id: uuid.UUID
    name: str
    gender: str | None = None
    label: str | None = None
    article: str | None = None
    thickness: str | None = None
