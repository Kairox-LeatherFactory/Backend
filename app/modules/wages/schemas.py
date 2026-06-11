import uuid
from datetime import date
from pydantic import BaseModel, ConfigDict


class RateSet(BaseModel):
    style_id: uuid.UUID
    operation_id: uuid.UUID
    rate: float
    effective_from: date


class WageLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    employee_id: uuid.UUID
    wage_type: str
    pieces: int
    amount: float


class WageRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True) #This tells Pydantic: Read attributes from ORM objects
    id: uuid.UUID
    period_start: date
    period_end: date
    status: str
    lines: list[WageLineRead] = []


class RunRequest(BaseModel):
    period_start: date
    period_end: date
