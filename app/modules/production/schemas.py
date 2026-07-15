import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    label: str
    sequence: int


# ---- SKU picker (friendly name + stored code, no UUID on screen) -----------
class SkuOption(BaseModel):
    sku_id: uuid.UUID
    code: str | None          # "57-M" — unique per style, readable
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
    code: str                 # "KJ2451-CLERMONT-57-M-005"
    seq: int                  # 5
    sku_id: uuid.UUID
    current_operation_id: uuid.UUID | None


class CuttingCreate(BaseModel):
    """Mint N pieces for a SKU at the cutting table."""
    sku_id: uuid.UUID | None = None
    sku_code: str | None = None
    employee_id: uuid.UUID
    work_date: date
    count: int = Field(gt=0)
    
    @model_validator(mode="after")
    def _one_sku(self):
        if not self.sku_id and not self.sku_code:
            raise ValueError("Provide sku_id or sku_code.")
        return self


class CuttingResult(BaseModel):
    sku_id: uuid.UUID
    count: int
    pieces: list[PieceRead]   # print these codes on the traveler cards


class ScanBatchCreate(BaseModel):
    """Log a batch at one stage. Give EITHER sku_id+piece_seqs OR piece_codes."""
    operation_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    sku_id: uuid.UUID | None = None
    sku_code: str | None = None 
    piece_seqs: list[int] | None = None       # [1, 2, 5, 7] within sku_id
    piece_codes: list[str] | None = None      # full codes (typed / scanned)

    @model_validator(mode="after")
    def _one_input(self):
        has_sku = bool(self.sku_id or self.sku_code)         # <-- sku_code counts
        if not self.piece_codes and not (has_sku and self.piece_seqs):
            raise ValueError("Provide (sku_id + piece_seqs) or piece_codes.")
        return self


class ScanBatchResult(BaseModel):
    operation: str
    count_logged: int
    logged: list[str]         # piece codes logged
    rework: list[str]         # pieces already seen at this stage (looped back)
    not_found: list[str]      # unknown seqs/codes — skipped, never written


class PieceOption(BaseModel):
    piece_id: uuid.UUID
    code: str                        # "KJ2451-CLERMONT-57-M-005" — show this
    seq: int                         # 5 — what /scan takes in piece_seqs
    current_stage: str | None        # "CUTTING"; null if never logged
    current_stage_label: str | None
    done_at_op: bool = False         # already logged at the requested operation


class SkuPieceList(BaseModel):
    sku_id: uuid.UUID
    sku_code: str | None
    colour: str | None
    size: str | None
    operation_id: uuid.UUID | None
    operation_code: str | None
    total: int
    done: int
    pending: int
    pieces: list[PieceOption]

# ---- Legacy generic event --------------------------------------------------
class ProductionEventCreate(BaseModel):
    sku_id: uuid.UUID
    operation_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    qty: int = Field(gt=0)


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


class StyleStageProgress(BaseModel):
    """The live status of one style across all its operations."""
    style_id: uuid.UUID
    style_name: str
    qty_ordered: int
    stages: dict[str, int]    # {"CUTTING": 152, "FUSING": 152, "PASTING": 150, ...}