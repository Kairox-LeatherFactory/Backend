"""
================================================================================
modules/jobwork/service.py — garments that leave the building, and come back
================================================================================
THE GAP

    When a deadline is short, tailoring (or cutting, or lining) goes to an
    outside factory. Nothing modelled that: the pieces stopped moving, nobody
    could say where they were, and the money paid for the work was recorded
    nowhere. Every stage assumed in-house.

THE TWO RULES THE FACTORY CHOSE

    1. A garment that is OUT cannot be scanned in-house. Otherwise the system
       happily records line-stitching done here on a jacket sitting at another
       factory twenty miles away — and nothing would ever contradict it.

    2. The vendor's work is LOGGED AS THE STAGE, against the vendor rather than
       an employee. The work really happened, so the garment must advance and
       every progress count must include it; but nobody on our payroll did it,
       so no wage may be generated. `production_event.employee_id` is nullable
       for exactly this, and the wage query inner-joins Employee, so a
       vendor-logged event drops out of payroll on its own.

AND THE MONEY. "Even when we give it to the outside factory we are paying for
each piece, so we have to track that." `rate_per_piece` is optional — some work
is quoted per piece, some is settled another way, and demanding a number nobody
has yet means the dispatch does not get recorded at all. Where it is given, the
cost is rate x PIECES THAT CAME BACK, computed from the ledger rather than
stored, so a garment that never returned is never paid for.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    BarcodeAuditAction, JobWorkPieceStatus, JobWorkStatus, ProductionStage,
)
from app.modules.jobwork.models import JobWork, JobWorkPiece, Vendor
from app.modules.production.models import Piece


class JobWorkService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ══════════════════════════════════════════════════════════ vendors
    async def create_vendor(self, *, name: str, contact=None, note=None) -> dict:
        if not (name or "").strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "A vendor needs a name.")
        existing = await self.db.execute(
            select(Vendor).where(func.upper(Vendor.name) == name.strip().upper()))
        found = existing.scalars().first()
        if found is not None:
            # Not an error: the same outside factory being registered twice is a
            # duplicate, and returning the original is more useful than a 409.
            return self._vendor_payload(found)
        v = Vendor(name=name.strip(), contact=contact, note=note)
        self.db.add(v)
        await self.db.commit()
        return self._vendor_payload(v)

    async def list_vendors(self, *, active_only: bool = True) -> list:
        stmt = select(Vendor)
        if active_only:
            stmt = stmt.where(Vendor.is_active.is_(True))
        rows = (await self.db.execute(stmt.order_by(Vendor.name))).scalars().all()
        return [self._vendor_payload(v) for v in rows]

    @staticmethod
    def _vendor_payload(v) -> dict:
        return {"vendor_id": v.id, "name": v.name, "contact": v.contact,
                "note": v.note, "is_active": bool(v.is_active)}

    # ══════════════════════════════════════════════════════════ dispatch
    async def dispatch(self, *, vendor_id, stage, piece_ids: list,
                       expected_back: date | None = None,
                       rate_per_piece=None, currency=None, note=None,
                       actor_user_id=None, actor_name=None) -> dict:
        """Send garments out. They stop being scannable here until they return."""
        vendor = await self.db.get(Vendor, vendor_id)
        if vendor is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found.")
        stg = self._stage(stage)
        if not piece_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Name the garments being sent out.")

        now = datetime.now(timezone.utc)
        job = JobWork(
            vendor_id=vendor.id, stage=stg.value, dispatched_at=now,
            expected_back=expected_back, status=JobWorkStatus.OUT.value,
            rate_per_piece=(Decimal(str(rate_per_piece))
                            if rate_per_piece is not None else None),
            currency=currency, dispatched_by=actor_user_id, note=note)
        self.db.add(job)
        await self.db.flush()

        sent, skipped = [], []
        for pid in dict.fromkeys(piece_ids):
            piece = await self.db.get(Piece, pid)
            if piece is None:
                skipped.append({"piece_id": str(pid), "reason": "not found"})
                continue
            # A GARMENT CANNOT BE IN TWO PLACES. Dispatching one that is already
            # out would leave two open records claiming it, and the return of
            # either would look like it had come home.
            away = await self.current_job_for_piece(pid)
            if away is not None:
                skipped.append({
                    "piece_id": str(pid), "piece": piece.code,
                    "reason": f"already out at {await self._vendor_name(away)}"})
                continue
            self.db.add(JobWorkPiece(job_work_id=job.id, piece_id=pid,
                                     status=JobWorkPieceStatus.OUT.value))
            sent.append(piece.code)

        if not sent:
            # Nothing left the building, so there is no dispatch to record.
            await self.db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "None of those garments could be sent: "
                + "; ".join(s["reason"] for s in skipped))

        await self._audit(actor_user_id, BarcodeAuditAction.JOB_WORK_DISPATCHED,
                          job.id, {"vendor": vendor.name, "stage": stg.value,
                                   "pieces": sent, "rate_per_piece": rate_per_piece,
                                   "by": actor_name})
        await self.db.commit()
        return await self.payload(job, extra={"skipped": skipped})

    # ══════════════════════════════════════════════════════════ return
    async def receive(self, job_id, *, piece_ids=None, rejected_ids=None,
                      short_ids=None, work_date=None, actor_user_id=None,
                      actor_name=None) -> dict:
        """Book garments back in, and LOG THE STAGE the vendor performed.

        THE STAGE IS LOGGED AGAINST THE VENDOR. The work really happened, so the
        garment has to advance and every progress count has to include it — but
        nobody on our payroll did it, so `employee_id` stays NULL and the event
        drops out of the wage query on its own.

        REJECTED AND SHORT ARE NOT PAID FOR and do not advance. A piece that came
        back badly done is a quality conversation; one that never came back is a
        loss to chase. Neither is work delivered.
        """
        job = await self.db.get(JobWork, job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Job work not found.")

        rows = await self._rows(job.id)
        by_piece = {r.piece_id: r for r in rows}
        rejected = set(rejected_ids or [])
        short = set(short_ids or [])
        # Default: everything still out is coming back.
        coming = (set(piece_ids) if piece_ids
                  else {r.piece_id for r in rows
                        if r.status == JobWorkPieceStatus.OUT.value})
        coming -= rejected | short

        now = datetime.now(timezone.utc)
        logged, marked = [], []
        for pid in coming:
            row = by_piece.get(pid)
            if row is None or row.status != JobWorkPieceStatus.OUT.value:
                continue
            row.status = JobWorkPieceStatus.BACK.value
            row.returned_at = now
            piece = await self.db.get(Piece, pid)
            if piece is not None:
                await self._log_vendor_stage(job, piece, work_date or date.today())
                logged.append(piece.code)

        for pid, state in [(p, JobWorkPieceStatus.REJECTED.value) for p in rejected] \
                        + [(p, JobWorkPieceStatus.SHORT.value) for p in short]:
            row = by_piece.get(pid)
            if row is not None:
                row.status = state
                row.returned_at = now if state == JobWorkPieceStatus.REJECTED.value else None
                marked.append(state)

        still_out = sum(1 for r in rows if r.status == JobWorkPieceStatus.OUT.value)
        job.status = (JobWorkStatus.OUT.value if still_out == len(rows)
                      else JobWorkStatus.RETURNED.value if still_out == 0
                      else JobWorkStatus.PARTIAL.value)
        if job.status == JobWorkStatus.RETURNED.value:
            job.returned_at = now
        job.received_by = actor_user_id

        await self._audit(actor_user_id, BarcodeAuditAction.JOB_WORK_RETURNED,
                          job.id, {"logged": logged, "rejected": len(rejected),
                                   "short": len(short), "by": actor_name})
        await self.db.commit()
        return await self.payload(job)

    async def _log_vendor_stage(self, job, piece, work_date) -> None:
        """Write the production event the vendor earned. NO COMMIT.

        Same transaction as the return, so a garment marked BACK without its
        stage — or a stage without the return — cannot exist.
        """
        from app.modules.production.repository import ProductionRepository
        repo = ProductionRepository(self.db)
        op = await repo.get_operation_by_code(job.stage)
        if op is None:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"Stage '{job.stage}' has no configured operation.")
        if await repo.has_event_at_op(piece.id, op.id):
            return          # already logged — a re-tap of the return is a no-op
        await repo.add_event_nocommit(
            sku_id=piece.sku_id, operation_id=op.id,
            employee_id=None,            # nobody on our payroll did this
            vendor_id=job.vendor_id,
            work_date=work_date, qty=1, piece_id=piece.id,
            entered_by=f"job work")
        piece.current_operation_id = op.id

    # ═════════════════════════════════════ what the production log reads
    async def current_job_for_piece(self, piece_id):
        """The open dispatch this garment is on, or None.

        Read by the production log on every scan: a garment that is physically at
        another factory must not be logged as worked here.
        """
        res = await self.db.execute(
            select(JobWork)
            .join(JobWorkPiece, JobWorkPiece.job_work_id == JobWork.id)
            .where(JobWorkPiece.piece_id == piece_id,
                   JobWorkPiece.status == JobWorkPieceStatus.OUT.value))
        return res.scalars().first()

    async def out_pieces(self, piece_ids: list) -> dict:
        """{piece_id: (job, vendor_name)} for garments currently away. Batched."""
        if not piece_ids:
            return {}
        res = await self.db.execute(
            select(JobWorkPiece.piece_id, JobWork, Vendor.name)
            .join(JobWork, JobWork.id == JobWorkPiece.job_work_id)
            .outerjoin(Vendor, Vendor.id == JobWork.vendor_id)
            .where(JobWorkPiece.piece_id.in_(tuple(piece_ids)),
                   JobWorkPiece.status == JobWorkPieceStatus.OUT.value))
        return {pid: (job, name) for pid, job, name in res.all()}

    # ══════════════════════════════════════════════════════════ reading
    async def payload(self, job, extra: dict | None = None) -> dict:
        rows = await self._rows(job.id)
        back = [r for r in rows if r.status == JobWorkPieceStatus.BACK.value]
        rate = float(job.rate_per_piece) if job.rate_per_piece is not None else None
        return {
            "job_id": job.id,
            "vendor_id": job.vendor_id,
            "vendor": await self._vendor_name(job),
            "stage": job.stage,
            "status": job.status,
            "dispatched_at": job.dispatched_at,
            "expected_back": job.expected_back,
            "returned_at": job.returned_at,
            "rate_per_piece": rate,
            "currency": job.currency,
            "pieces_out": sum(1 for r in rows
                              if r.status == JobWorkPieceStatus.OUT.value),
            "pieces_back": len(back),
            "pieces_rejected": sum(1 for r in rows
                                   if r.status == JobWorkPieceStatus.REJECTED.value),
            "pieces_short": sum(1 for r in rows
                                if r.status == JobWorkPieceStatus.SHORT.value),
            # COST IS DERIVED, and only on what came back. A garment that never
            # returned was never work delivered, so it is never paid for.
            "cost": (rate * len(back)) if rate is not None else None,
            "overdue": self._is_overdue(job),
            "note": job.note,
            **(extra or {}),
        }

    @staticmethod
    def _is_overdue(job) -> bool:
        return bool(job.expected_back
                    and job.status in (JobWorkStatus.OUT.value,
                                       JobWorkStatus.PARTIAL.value)
                    and job.expected_back < date.today())

    async def list_jobs(self, *, status_filter=None, vendor_id=None,
                        overdue_only: bool = False, limit: int = 200) -> list:
        stmt = select(JobWork)
        if status_filter:
            stmt = stmt.where(JobWork.status == status_filter.upper())
        if vendor_id:
            stmt = stmt.where(JobWork.vendor_id == vendor_id)
        stmt = stmt.order_by(JobWork.dispatched_at.desc()).limit(limit)
        jobs = (await self.db.execute(stmt)).scalars().all()
        out = [await self.payload(j) for j in jobs]
        return [j for j in out if j["overdue"]] if overdue_only else out

    async def _rows(self, job_id) -> list:
        res = await self.db.execute(
            select(JobWorkPiece).where(JobWorkPiece.job_work_id == job_id))
        return list(res.scalars().all())

    async def _vendor_name(self, job) -> str | None:
        if job.vendor_id is None:
            return None
        v = await self.db.get(Vendor, job.vendor_id)
        return getattr(v, "name", None)

    @staticmethod
    def _stage(value) -> ProductionStage:
        try:
            return ProductionStage(str(getattr(value, "value", value)).strip().upper())
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "stage is not a production stage.")

    async def _audit(self, actor_user_id, action, entity_id, after: dict) -> None:
        """`actor_user_id` is an app_user.id — the LOGIN, never an employee.id."""
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_user_id,
            action=getattr(action, "value", str(action)),
            entity_type="job_work", entity_id=entity_id, after=after,
            at=datetime.now(timezone.utc)))
