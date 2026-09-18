"""HTTP shapes for stage-wise reject & rework."""
import uuid
from datetime import datetime

from pydantic import BaseModel, model_validator


class InspectionRequest(BaseModel):
    piece_id: uuid.UUID | None = None
    piece_barcode: str | None = None
    found_at_stage: str                       # where the defect was SEEN
    verdict: str                              # PASS | REJECT
    action: str | None = None                 # FIX | REDO   (a reject must say)
    return_to_stage: str | None = None        # on a REDO — the rejector chooses
    defect_type: str | None = None            # PRODUCT_DAMAGE | WORKMANSHIP
    # WHO IS ANSWERABLE FOR THIS PIECE. Required for WORKMANSHIP, refused for
    # PRODUCT_DAMAGE — naming somebody for a bad hide is worse than naming
    # nobody, because it puts a defect against a worker who did nothing wrong
    # and teaches the floor to stop reporting damage.
    responsible_employee_id: uuid.UUID | None = None
    responsible_stage: str | None = None      # which stage they were doing
    reason: str | None = None

    @model_validator(mode="after")
    def _need_a_piece(self):
        if not (self.piece_id or self.piece_barcode):
            raise ValueError("Scan or name the garment being inspected.")
        return self


class InspectionDecision(BaseModel):
    note: str | None = None


class InspectionRead(BaseModel):
    inspection_id: uuid.UUID
    piece_id: uuid.UUID | None = None
    piece_code: str | None = None
    found_at_stage: str
    verdict: str
    action: str | None = None
    return_to_stage: str | None = None
    defect_type: str | None = None
    responsible_employee_id: uuid.UUID | None = None
    responsible_stage: str | None = None
    reason: str | None = None
    status: str
    raised_at: datetime | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    resolved_at: datetime | None = None


class ResponsibilityRow(BaseModel):
    """Rejections attributed to one worker at one stage.

    The factory's own requirement: a worker whose stage was not done properly is
    answerable for that piece. A count is what makes that reviewable — a reason
    box cannot be counted across a month or produced in a wage conversation.
    """
    employee_id: uuid.UUID | None = None
    employee: str | None = None
    stage: str | None = None
    rejections: int
