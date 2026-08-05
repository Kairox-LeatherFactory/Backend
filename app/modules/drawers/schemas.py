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