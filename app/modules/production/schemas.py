import uuid
from datetime import date
from pydantic import BaseModel, ConfigDict, Field


class OperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    label: str
    sequence: int


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
    bundle_ref: str | None


class StyleStageProgress(BaseModel):
    """The live status of one style across all its operations."""
    style_id: uuid.UUID
    style_name: str
    qty_ordered: int
    stages: dict[str, int]   # {"CUTTING": 152, "FUSING": 152, "PASTING": 155, ...}
