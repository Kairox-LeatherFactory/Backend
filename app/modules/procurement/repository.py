"""
================================================================================
modules/procurement/repository.py — Stage-1 intake data access
================================================================================

The only place that talks to the DB for the Stage-1 surface: `submission`,
`client_template` (procurement-owned) and the cross-cutting `document` (core-owned,
imported from app.core.models). BOM / inventory / supplier-PO queries moved to their
own modules' repositories when the monolith was split.

FUNCTION GUIDE  (all async; called only by ProcurementService)
  commit() / save(obj)   session plumbing (save = commit + refresh).
  create_submission(*, client_id, created_by, status) -> Submission   mint the upload batch.
  get_submission(id) -> Submission | None.
  get_document(id) / get_document_by_sha(sha) -> Document | None   (sha = the dedupe/cache lookup).
  add_document(doc) -> Document   persist a validated upload (accepted OR rejected — caches by sha).
  active_templates(doc_kind?) -> [ClientTemplate]   the validation profiles for a slot
      (the validator reads these at request time → onboarding a client is a row, no code).
================================================================================
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Document
from app.modules.procurement.models import ClientTemplate, Submission


class ProcurementRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def commit(self) -> None:
        await self.db.commit()

    async def save(self, obj) -> None:
        await self.db.commit()
        await self.db.refresh(obj)

    # ── submission ───────────────────────────────────────────────────────────
    async def create_submission(
        self, *, client_id: uuid.UUID | None, created_by: uuid.UUID | None, status: str
    ) -> Submission:
        sub = Submission(client_id=client_id, created_by=created_by, status=status)
        self.db.add(sub)
        await self.db.commit()
        await self.db.refresh(sub)
        return sub

    async def get_submission(self, submission_id: uuid.UUID) -> Submission | None:
        res = await self.db.execute(select(Submission).where(Submission.id == submission_id))
        return res.scalar_one_or_none()

    # ── document (core-owned table; Stage-1 owns the write path) ─────────────
    async def get_document(self, document_id: uuid.UUID) -> Document | None:
        res = await self.db.execute(select(Document).where(Document.id == document_id))
        return res.scalar_one_or_none()

    async def get_document_by_sha(self, sha256: str) -> Document | None:
        res = await self.db.execute(select(Document).where(Document.sha256 == sha256))
        return res.scalar_one_or_none()

    async def add_document(self, doc: Document) -> Document:
        self.db.add(doc)
        await self.db.commit()
        await self.db.refresh(doc)
        return doc

    async def delete_document(self, doc: Document) -> None:
        """Drop a cached document row. Used to evict a stale NEEDS_MANUAL_REVIEW result so a
        byte-identical re-upload re-runs the pipeline (the sha cache must not pin an
        unresolved verdict forever — see service._handle_existing)."""
        await self.db.delete(doc)
        await self.db.commit()

    # ── client_template registry ─────────────────────────────────────────────
    async def active_templates(self, doc_kind: str | None = None) -> list[ClientTemplate]:
        stmt = select(ClientTemplate).where(ClientTemplate.is_active.is_(True))
        if doc_kind is not None:
            stmt = stmt.where(ClientTemplate.doc_kind == doc_kind)
        res = await self.db.execute(stmt)
        return list(res.scalars())
