"""
================================================================================
modules/production/inspection.py — stage-wise reject, rework, and who is to blame
================================================================================
THE GAP THIS CLOSES

    "A piece completed in Fusing was rejected during Pasting, but there is
    currently no proper way to send it back to Fusing for rework."

    There was not. The sequence gate treats a completed stage as complete for
    good; re-logging it lands in the `rework` bucket and writes nothing; and the
    only route back was an edit in the database. So defects were handled by
    telling somebody, and nothing was ever counted.

TWO STEPS, AND THE SECOND ONE IS THE DM
    Anyone standing at a stage can SEE a defect, so any manager or HR may raise
    a rejection. Moving the garment backwards is a different thing: it re-opens a
    completed stage, re-orders the line's work and can cost material. So the DM
    approves before the piece actually moves, and until then the rejection is a
    report rather than a movement.

WHO IS RESPONSIBLE IS RECORDED, BECAUSE THE FACTORY ASKS FOR IT
    If a stage was not done properly and the garment is damaged because of it,
    that worker is answerable for that piece. A free-text reason cannot be
    counted across a month, produced in a wage conversation, or told apart from a
    bad hide — which is the supplier's problem and nobody's fault on the floor.
    So the defect carries a TYPE, and WORKMANSHIP carries the employee and the
    stage they were doing.

    PRODUCT_DAMAGE NAMES NOBODY, deliberately. Attributing a flawed hide to the
    cutter who happened to use it would make the record worse than useless — it
    would make people stop reporting defects.

HOW A PIECE ACTUALLY GOES BACK
    Not by deleting events: the work happened and the wage was earned, and
    erasing it would take money off somebody for a defect that may not be theirs.
    Instead an APPROVED redo is an open permission the production log reads —
    `rework_target` below — which lets that one stage be logged again. The new
    event is flagged `is_rework`, so its cost is separable from the original.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    BarcodeAuditAction, DefectType, InspectionStatus, InspectionVerdict,
    ProductionStage, ReworkAction,
)
from app.modules.production.models import Piece, PieceInspection


class InspectionService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ══════════════════════════════════════════════════════════ raising
    async def raise_inspection(self, *, piece_id, found_at_stage, verdict,
                               action=None, return_to_stage=None,
                               defect_type=None, responsible_employee_id=None,
                               responsible_stage=None, reason=None,
                               actor_user_id=None, actor_name=None) -> dict:
        """Record a PASS, or raise a REJECT for the DM to decide on.

        A PASS is written and closed immediately — there is nothing to approve
        about a garment that is fine, and making somebody sign one off would mean
        nobody records passes at all.
        """
        piece = await self.db.get(Piece, piece_id)
        if piece is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Piece not found.")

        found = self._stage(found_at_stage, "found_at_stage")
        v = self._enum(InspectionVerdict, verdict, "verdict")

        if v is InspectionVerdict.PASS:
            row = PieceInspection(
                piece_id=piece.id, found_at_stage=found.value,
                verdict=v.value, status=InspectionStatus.RESOLVED.value,
                reason=reason, raised_by=actor_user_id,
                raised_at=datetime.now(timezone.utc),
                resolved_at=datetime.now(timezone.utc))
            self.db.add(row)
            await self.db.commit()
            return await self.payload(row)

        # ── a REJECT has to say what it wants done ───────────────────────────
        act = self._enum(ReworkAction, action, "action") if action else None
        if act is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A rejection must say what it needs: FIX (repair it where it is) "
                "or REDO (send it back so an earlier stage is done again).")

        target = None
        if act is ReworkAction.REDO:
            if not return_to_stage:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "A REDO must name the stage to go back to. The rejector "
                    "chooses it — a ruined panel goes back to cutting, not "
                    "merely one stage.")
            target = self._stage(return_to_stage, "return_to_stage")
            await self._assert_already_passed(piece, target)
            if target is found:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"{target.value} is where the defect was found. To repair it "
                    f"there, send action=FIX — a REDO goes back to an EARLIER "
                    f"stage.")

        dt = self._enum(DefectType, defect_type, "defect_type") if defect_type else None
        if dt is DefectType.WORKMANSHIP and responsible_employee_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A WORKMANSHIP defect has to name who is answerable for the "
                "piece — that is the whole reason the type exists. If nobody is "
                "at fault, record it as PRODUCT_DAMAGE instead.")
        if dt is DefectType.PRODUCT_DAMAGE and responsible_employee_id is not None:
            # NAMING SOMEBODY FOR A BAD HIDE IS WORSE THAN NAMING NOBODY: it puts
            # a defect against a worker who did nothing wrong, and it teaches the
            # floor to stop reporting damage at all.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "PRODUCT_DAMAGE is a fault in the material, so no employee is "
                "answerable for it. Drop responsible_employee_id, or record it "
                "as WORKMANSHIP if a stage was done badly.")

        open_row = await self.open_for_piece(piece.id)
        if open_row is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{piece.code} already has a rejection waiting on the DM "
                f"(raised at {open_row.found_at_stage}). Settle that one first — "
                f"two open rejections on one garment cannot both be acted on.")

        row = PieceInspection(
            piece_id=piece.id, found_at_stage=found.value, verdict=v.value,
            action=act.value, return_to_stage=target.value if target else None,
            defect_type=dt.value if dt else None,
            responsible_employee_id=responsible_employee_id,
            responsible_stage=(self._stage(responsible_stage,
                                           "responsible_stage").value
                               if responsible_stage else None),
            reason=reason, status=InspectionStatus.PENDING.value,
            raised_by=actor_user_id, raised_at=datetime.now(timezone.utc))
        self.db.add(row)
        await self._audit(actor_user_id, BarcodeAuditAction.PIECE_REJECTED,
                          piece.id,
                          {"piece": piece.code, "found_at": found.value,
                           "action": act.value,
                           "return_to": target.value if target else None,
                           "defect_type": dt.value if dt else None,
                           "by": actor_name})
        await self.db.commit()
        return await self.payload(row)

    # ══════════════════════════════════════════════════════════ deciding
    async def decide(self, inspection_id, *, approve: bool, actor_user_id=None,
                     actor_name=None, note=None) -> dict:
        """The DM's call. Only after this does the garment actually move."""
        row = await self.db.get(PieceInspection, inspection_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Inspection not found.")
        if row.status != InspectionStatus.PENDING.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"This rejection is already {row.status}; it cannot be decided "
                f"again.")

        row.status = (InspectionStatus.APPROVED.value if approve
                      else InspectionStatus.DECLINED.value)
        row.decided_by = actor_user_id
        row.decided_at = datetime.now(timezone.utc)
        row.decision_note = note
        # A FIX needs no stage re-opened — the piece never moves — so approving
        # one settles it outright. Only a REDO leaves an open permission behind.
        if approve and row.action == ReworkAction.FIX.value:
            row.resolved_at = row.decided_at

        await self._audit(
            actor_user_id,
            BarcodeAuditAction.PIECE_REWORK_APPROVED if approve
            else BarcodeAuditAction.PIECE_REWORK_DECLINED,
            row.piece_id,
            {"inspection_id": str(row.id), "action": row.action,
             "return_to": row.return_to_stage, "note": note, "by": actor_name})
        await self.db.commit()
        return await self.payload(row)

    # ═════════════════════════════════════ what the production log reads
    async def rework_target(self, piece_id) -> tuple:
        """(stage, inspection) this garment is allowed to redo, or (None, None).

        THE PERMISSION, NOT A HISTORY. An APPROVED redo is standing consent for
        ONE stage to be logged again; the log reads it, writes the event flagged
        `is_rework`, and marks the rejection RESOLVED so the permission is spent.
        Without that last step an approved redo would let the same stage be
        re-logged forever.
        """
        res = await self.db.execute(
            select(PieceInspection)
            .where(PieceInspection.piece_id == piece_id,
                   PieceInspection.status == InspectionStatus.APPROVED.value,
                   PieceInspection.action == ReworkAction.REDO.value)
            .order_by(PieceInspection.decided_at.asc()))
        row = res.scalars().first()
        if row is None or not row.return_to_stage:
            return None, None
        try:
            return ProductionStage(row.return_to_stage), row
        except ValueError:
            return None, None

    async def pending_for_pieces(self, piece_ids: list) -> dict:
        """{piece_id: inspection} for garments with a rejection awaiting the DM.

        A PIECE SOMEBODY HAS CALLED DEFECTIVE STOPS MOVING. Letting it walk on to
        the next stage while the paperwork catches up means the defect travels
        down the line and more work is spent on a garment that is going back
        anyway — and by the time the DM approves, the piece is three stages
        further on than where it was rejected.

        Batched: one query for a whole scan, because this runs on the hot path.
        """
        if not piece_ids:
            return {}
        res = await self.db.execute(
            select(PieceInspection)
            .where(PieceInspection.piece_id.in_(tuple(piece_ids)),
                   PieceInspection.status == InspectionStatus.PENDING.value))
        return {r.piece_id: r for r in res.scalars().all()}

    async def resolved_redo_spans(self, piece_id) -> list:
        """[(return_to_stage, found_at_stage)] for every redo actually performed.

        A REDO INVALIDATES A SPAN, NOT EVERYTHING AFTER IT. Redoing FUSING on a
        garment rejected at LINE_STITCHING invalidates the PASTING and the
        LINE_STITCHING done on that badly-fused panel — but it says nothing about
        SHELL_STITCHING, which the piece had never reached. Treating the whole
        tail as invalidated made every later stage demand a second event before
        it counted, so the garment logged SHELL_STITCHING, FINAL_FINISH and
        FINAL_INSPECTION twice each on its way out.

        `found_at_stage` is the upper bound because that is where the piece was
        when somebody rejected it: everything between the redo target and there
        was work done on the bad panel, and nothing beyond it had happened yet.
        """
        res = await self.db.execute(
            select(PieceInspection.return_to_stage,
                   PieceInspection.found_at_stage)
            .where(PieceInspection.piece_id == piece_id,
                   PieceInspection.action == ReworkAction.REDO.value,
                   PieceInspection.status == InspectionStatus.RESOLVED.value,
                   PieceInspection.return_to_stage.is_not(None)))
        return [(t, f) for t, f in res.all()]

    async def mark_resolved_nocommit(self, row) -> None:
        """The redo happened. NO COMMIT — the log owns the transaction, so the
        event, the stock movement and this all land together."""
        row.status = InspectionStatus.RESOLVED.value
        row.resolved_at = datetime.now(timezone.utc)

    async def open_for_piece(self, piece_id):
        res = await self.db.execute(
            select(PieceInspection)
            .where(PieceInspection.piece_id == piece_id,
                   PieceInspection.status.in_(
                       (InspectionStatus.PENDING.value,
                        InspectionStatus.APPROVED.value))))
        return res.scalars().first()

    # ══════════════════════════════════════════════════════════ reading
    async def list_open(self, *, status_filter=None, limit: int = 200) -> list:
        stmt = select(PieceInspection)
        if status_filter:
            stmt = stmt.where(PieceInspection.status == status_filter.upper())
        else:
            stmt = stmt.where(PieceInspection.status.in_(
                (InspectionStatus.PENDING.value, InspectionStatus.APPROVED.value)))
        stmt = stmt.order_by(PieceInspection.raised_at.desc()).limit(limit)
        rows = list((await self.db.execute(stmt)).scalars().all())
        return [await self.payload(r) for r in rows]

    async def for_piece(self, piece_id) -> list:
        res = await self.db.execute(
            select(PieceInspection)
            .where(PieceInspection.piece_id == piece_id)
            .order_by(PieceInspection.raised_at.asc()))
        return [await self.payload(r) for r in res.scalars().all()]

    async def responsibility_report(self, *, employee_id=None) -> list:
        """Defects by the worker answerable for them — the point of the column.

        WORKMANSHIP ONLY. Product damage names nobody, so counting it here would
        put a supplier's bad hide onto a person's record.
        """
        from sqlalchemy import func
        from app.modules.employees.models import Employee
        stmt = (select(PieceInspection.responsible_employee_id,
                       Employee.name,
                       PieceInspection.responsible_stage,
                       func.count())
                .join(Employee,
                      Employee.id == PieceInspection.responsible_employee_id)
                .where(PieceInspection.defect_type == DefectType.WORKMANSHIP.value,
                       PieceInspection.verdict == InspectionVerdict.REJECT.value)
                .group_by(PieceInspection.responsible_employee_id, Employee.name,
                          PieceInspection.responsible_stage))
        if employee_id:
            stmt = stmt.where(PieceInspection.responsible_employee_id == employee_id)
        return [{"employee_id": r[0], "employee": r[1], "stage": r[2],
                 "rejections": int(r[3])}
                for r in (await self.db.execute(stmt)).all()]

    async def payload(self, row) -> dict:
        piece = await self.db.get(Piece, row.piece_id) if row.piece_id else None
        return {
            "inspection_id": row.id,
            "piece_id": row.piece_id,
            "piece_code": getattr(piece, "code", None),
            "found_at_stage": row.found_at_stage,
            "verdict": row.verdict, "action": row.action,
            "return_to_stage": row.return_to_stage,
            "defect_type": row.defect_type,
            "responsible_employee_id": row.responsible_employee_id,
            "responsible_stage": row.responsible_stage,
            "reason": row.reason, "status": row.status,
            "raised_at": row.raised_at, "decided_at": row.decided_at,
            "decision_note": row.decision_note, "resolved_at": row.resolved_at,
        }

    # ══════════════════════════════════════════════════════════ helpers
    @staticmethod
    def _enum(cls, value, field):
        try:
            return cls(str(getattr(value, "value", value)).strip().upper())
        except ValueError:
            allowed = ", ".join(m.value for m in cls)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"{field} must be one of: {allowed}.")

    @staticmethod
    def _stage(value, field) -> ProductionStage:
        try:
            return ProductionStage(str(getattr(value, "value", value)).strip().upper())
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"{field} is not a production stage.")

    async def _assert_already_passed(self, piece, stage: ProductionStage) -> None:
        """A garment cannot be sent back to a stage it never reached.

        Without this a rejection could 'return' a piece to LINE_STITCHING it has
        never had, and the log would then happily write that stage as a rework —
        skipping the chain entirely through the one door built to bypass it.
        """
        from app.modules.production.repository import ProductionRepository
        repo = ProductionRepository(self.db)
        op = await repo.get_operation_by_code(stage.value)
        if op is None or not await repo.has_event_at_op(piece.id, op.id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{piece.code} has never completed {stage.value}, so it cannot "
                f"be sent BACK to it. Name a stage the garment has actually "
                f"passed.")

    async def _audit(self, actor_user_id, action, entity_id, after: dict) -> None:
        """`actor_user_id` is an app_user.id — the LOGIN, never an employee.id."""
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_user_id,
            action=getattr(action, "value", str(action)),
            entity_type="piece_inspection", entity_id=entity_id, after=after,
            at=datetime.now(timezone.utc)))
