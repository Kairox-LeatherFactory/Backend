"""Pydantic schemas — the API contract for the clients module.

These shapes ARE the contract your frontend mocks against.

CONTRACT NOTE (Stage 0): the buyer order was renamed PurchaseOrder -> ClientOrder
and `po_number` -> `order_number`. New BOM-workflow fields are optional/nullable
so existing consumers keep working.
"""
import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SKURead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    color_code: str
    color_name: str | None
    size: str
    qty_ordered: int
    nylon_color: str | None = None
    knit_color: str | None = None

# add near ClientOrderRead
class ClientOrderCreate(BaseModel):
    """Add another order to an existing client. Only order_number is required;
    the rest are optional order-level fields (dates drive freight-risk)."""
    order_number: str = Field(min_length=1)   # globally unique across ALL clients
    order_date: date | None = None
    delivery_deadline: date | None = None
    sea_cutoff_date: date | None = None
    ship_mode: str = "sea"                     # "sea" | "air"
    currency: str | None = None
    agent: str | None = None
    line: str | None = None

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
    
# add after ClientRead:
class CreatedClientRead(ClientRead):
    """POST /clients response — the client plus the order minted for it."""
    order_number: str
    order_id: uuid.UUID


class ClientCreate(BaseModel):
    name: str
    country: str | None = None
    order_number: str = Field(min_length=1)   # manager-entered; globally unique


class StyleCreate(BaseModel):
    client_order_id: uuid.UUID
    name: str
    gender: str | None = None
    label: str | None = None
    article: str | None = None
    thickness: str | None = None
    
class StyleOption(BaseModel):
    style_id: uuid.UUID
    style_name: str
    article: str | None
    order_number: str
    sku_count: int
    qty_ordered: int
