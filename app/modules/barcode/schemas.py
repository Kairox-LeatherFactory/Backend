"""modules/barcode/schemas.py — API contract for the barcode registry."""
import uuid

from pydantic import BaseModel, Field, model_validator


class BarcodeResolve(BaseModel):
    """Loose by design: resolve() returns a type + a type-specific sub-object, and
    the frontend branches on `type`. Kept as a passthrough dict rather than a
    rigid union so a new barcode type never breaks the read contract."""
    code: str
    type: str
    active: bool
    caption: str | None = None
    piece: dict | None = None
    employee: dict | None = None
    drawer: dict | None = None
    lot: dict | None = None


class PrintRequest(BaseModel):
    codes: list[str] | None = None
    sku_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_source(self):
        if not self.codes and not self.sku_id and not self.order_id:
            raise ValueError("Provide codes, sku_id, or order_id.")
        return self


class PrintLabel(BaseModel):
    code: str
    symbology: str
    caption: str
    known: bool


class PrintResponse(BaseModel):
    labels: list[PrintLabel]


class BarcodeAction(BaseModel):
    action: str = Field(pattern="^(deactivate|reissue)$")


class BarcodeActionResult(BaseModel):
    employee_id: uuid.UUID
    employee_barcode: str
    active: bool
    history_preserved: bool