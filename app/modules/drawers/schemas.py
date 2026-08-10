"""modules/drawers/schemas.py — API contract for drawers & the merge gate."""
import uuid

from pydantic import BaseModel, Field


class StoreScanRequest(BaseModel):
    # Either code or id for each — barcode door sends codes, manual sends ids.
    drawer_barcode: str | None = None
    drawer_id: uuid.UUID | None = None
    piece_barcode: str | None = None
    piece_id: uuid.UUID | None = None
    part: str = Field(pattern="^(LEATHER|LINING)$")


class StoreScanResult(BaseModel):
    drawer_code: str
    piece_code: str
    state: str
    needs_lining: bool
    awaiting: list[str]
    ready_for_received: bool


class DrawerTransition(BaseModel):
    transition: str = Field(pattern="^(RECEIVED|SENDED)$")


class DrawerTransitionResult(BaseModel):
    drawer_code: str
    piece_code: str | None
    state: str


class DrawerLabel(BaseModel):
    """One printable drawer row: the drawer plus its DRAWER-type registry code.

    `barcode` is the string to encode on the label; `barcode_id` is the registry
    row that owns it. Both are null if the drawer has no registry row — that is a
    broken label the print sheet should SHOW, not silently drop."""
    drawer_id: uuid.UUID
    seq: int
    code: str
    state: str
    barcode_id: uuid.UUID | None = None
    barcode: str | None = None
    caption: str | None = None
    barcode_status: str | None = None


class DrawerLabelPage(BaseModel):
    total: int          # drawers matching the filters (ignores limit/offset)
    count: int          # rows in THIS page
    items: list[DrawerLabel]