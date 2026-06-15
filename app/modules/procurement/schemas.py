"""
================================================================================
modules/procurement/schemas.py — Stage-1 intake API contracts (§5)
================================================================================

Request bodies are typed Pydantic models. The rich, deeply-nested success and
rejection ENVELOPES (§5a/§5b) are assembled as plain dicts by the service so the exact
spec shape is preserved; their structure is documented here for the FE. BOM and
supplier-PO request bodies moved to their own modules' schemas on the split.

SCHEMA GUIDE (Pydantic)
  OpenSubmissionRequest   POST /submissions body {client_id?} → open_submission.
  SlotStatus / SubmissionSummary   typed mirrors of the submission-status response shape
      (the service actually assembles the dict; these document it for the FE/OpenAPI).
================================================================================
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class OpenSubmissionRequest(BaseModel):
    """POST /submissions — open an empty upload batch. The client may be unknown at
    upload time (derived from the order sheet in Stage 2), so client_id is optional."""
    client_id: uuid.UUID | None = Field(default=None)


class GenerateBomRequest(BaseModel):
    """DEPRECATED body for POST /submissions/{id}/generate-bom. The trigger now takes NO
    body — the BOM is built from the order + spec sheets alone and the order/style
    hierarchy is created from the parsed order sheet at MD approval. Retained (all fields
    optional) only so an old client posting `{}` or stale ids does not 422."""
    client_order_id: uuid.UUID | None = None
    style_id: uuid.UUID | None = None


class SlotStatus(BaseModel):
    present: bool
    validation_status: str | None


class SubmissionSummary(BaseModel):
    order_sheet: SlotStatus
    spec_sheet: SlotStatus
    complete: bool
    ready_for_stage_2: bool
    blocking: list[str]
