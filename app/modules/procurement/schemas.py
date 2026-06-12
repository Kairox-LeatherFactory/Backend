"""
================================================================================
modules/procurement/schemas.py — Stage-1 intake API contracts (§5)
================================================================================

Request bodies are typed Pydantic models. The rich, deeply-nested success and
rejection ENVELOPES (§5a/§5b) are assembled as plain dicts by the service so the exact
spec shape is preserved; their structure is documented here for the FE. BOM and
supplier-PO request bodies moved to their own modules' schemas on the split.
================================================================================
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class OpenSubmissionRequest(BaseModel):
    """POST /submissions — open an empty upload batch. The client may be unknown at
    upload time (derived from the order sheet in Stage 2), so client_id is optional."""
    client_id: uuid.UUID | None = Field(default=None)


class SlotStatus(BaseModel):
    present: bool
    validation_status: str | None


class SubmissionSummary(BaseModel):
    order_sheet: SlotStatus
    spec_sheet: SlotStatus
    complete: bool
    ready_for_stage_2: bool
    blocking: list[str]
