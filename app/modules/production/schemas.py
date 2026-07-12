import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class OperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    label: str
    sequence: int


# ---- SKU picker (friendly name, no UUID on screen) -------------------------
class SkuOption(BaseModel):
    sku_id: uuid.UUID
    label: str                # "CLERMONT · PINE GREEN · M"
    order_number: str | None
    style_name: str | None
    color_code: str | None
    color_name: str | None
    size: str | None
    qty_ordered: int


# ---- Pieces ----------------------------------------------------------------
class PieceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    sku_id: uuid.UUID
    current_operation_id: uuid.UUID | None


class CuttingCreate(BaseModel):
    """Mint N pieces for a SKU at the cutting table."""
    sku_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    count: int = Field(gt=0)


class CuttingResult(BaseModel):
    sku_id: uuid.UUID
    count: int
    pieces: list[PieceRead]   # print these codes on the traveler cards


class ScanBatchCreate(BaseModel):
    """A manager types the piece codes handled at one stage today."""
    operation_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    piece_codes: list[str] = Field(min_length=1)


class ScanBatchResult(BaseModel):
    operation: str
    count_logged: int
    logged: list[str]
    rework: list[str]         # pieces already seen at this stage (looped back)
    not_found: list[str]      # unknown codes — skipped, never written


# ---- Legacy generic event --------------------------------------------------
class ProductionEventCreate(BaseModel):
    sku_id: uuid.UUID
    operation_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    qty: int = Field(gt=0)
    bundle_ref: str | None = None


class ProductionEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sku_id: uuid.UUID
    operation_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    qty: int
    entered_by: str | None
    piece_id: uuid.UUID | None
    bundle_ref: str | None


class StyleStageProgress(BaseModel):
    """The live status of one style across all its operations."""
    style_id: uuid.UUID
    style_name: str
    qty_ordered: int
    stages: dict[str, int]   # {"CUTTING": 152, "FUSING": 152, "PASTING": 150, ...}