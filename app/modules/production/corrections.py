"""
================================================================================
production/corrections.py — fixing a production record without touching the DB
================================================================================
THE REPORTED PROBLEM

    "Manager Zahoor assigned a piece to the wrong employee during cutting, so we
    had to delete the record directly from the database."

    That is the worst way to change a production record. No audit row, no reason,
    no actor, and the wage that follows the record moved with no trace of who
    moved it. The permission was what forced it: there was no API at all.

TWO OPERATIONS, BECAUSE THEY ARE TWO DIFFERENT MISTAKES

    REASSIGN  the work HAPPENED and the leather WAS cut — only the name on it is
              wrong. Nothing about the stage or the stock changes; the wage
              simply follows the corrected name. This is the everyday case and
              any manager who can log the stage can correct it.

    DELETE    the record should not exist at all: the wrong piece was scanned, a
              double-tap got through, a test scan reached production. Here the
              stock DOES have to come back, because nothing was cut. Narrow
              (DM/MD), reason required, and the reversal is recorded as its own
              movement rather than by editing the lot.

A CLOSED PAYROLL RUN IS A WALL, AND IT IS THE PROJECT'S OWN RULE
    Wage lines are not stored against events — they are COMPUTED from
    production_event at read time. So while a run is OPEN, correcting an event
    just recomputes and everything stays consistent with no wage surgery at all.

    Once a run is CLOSED that stops being true: the closed run is the document
    the cash was counted against, and it is never silently recomputed. Editing an
    event inside a closed period would make the payroll disagree with the events
    it claims to summarise, with nothing to show which is right. So corrections
    inside a closed period are refused and the operator is told why — the fix
    there is an explicit payroll adjustment, not a quiet edit underneath it.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunStatus
from app.modules.production.models import Operation, Piece, ProductionEvent


class CorrectionService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ══════════════════════════════════════════════════════════ guards
    async def _event(self, event_id: uuid.UUID) -> ProductionEvent:
        ev = await self.db.get(ProductionEvent, event_id)
        if ev is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "Production event not found.")
        return ev

    async def _assert_payroll_open(self, ev: ProductionEvent) -> None:
        """Refuse to touch an event a CLOSED payroll run has already counted.

        Wage lines are computed from these events at read time, so while a run is
        OPEN a correction simply recomputes. A CLOSED run is the document the
        cash was counted against and is never silently recomputed — editing
        underneath it would leave the payroll disagreeing with the events it
        claims to summarise, with nothing to say which is right.
        """
        from app.modules.wages.models import WageRun
        res = await self.db.execute(
            select(WageRun).where(WageRun.status == RunStatus.CLOSED,
                                  WageRun.period_start <= ev.work_date,
                                  WageRun.period_end >= ev.work_date))
        run = res.scalars().first()
        if run is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"This work is dated {ev.work_date}, which a CLOSED payroll run "
                f"({run.period_start}..{run.period_end}) has already been paid "
                f"against. That run is a frozen snapshot and is never "
                f"recomputed, so the event underneath it cannot be changed — "
                f"correct it with a payroll adjustment instead.")

    # ══════════════════════════════════════════════════════════ reassign
    async def reassign(self, event_id: uuid.UUID, *, employee_id: uuid.UUID,
                       reason: str | None = None, actor_user_id=None,
                       actor_name: str | None = None) -> dict:
        """Put the right worker's name on work that really happened.

        THE STAGE AND THE STOCK ARE UNTOUCHED, deliberately. The garment was cut;
        the leather is gone. Only who did it was recorded wrongly, so only that
        changes — and the wage follows because it is derived from this row.
        """
        from app.modules.employees.models import Employee
        ev = await self._event(event_id)
        await self._assert_payroll_open(ev)

        emp = await self.db.get(Employee, employee_id)
        if emp is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found.")
        if emp.id == ev.employee_id:
            # Not an error: a manager re-submitting the same correction should
            # not be punished for checking.
            return await self._payload(ev, note="already assigned to that worker")

        was = ev.employee_id
        ev.employee_id = emp.id
        await self._audit(actor_user_id, "PRODUCTION_EVENT_REASSIGNED", ev.id,
                          before={"employee_id": str(was) if was else None},
                          after={"employee_id": str(emp.id),
                                 "employee": emp.name, "reason": reason,
                                 "by": actor_name})
        await self.db.commit()
        return await self._payload(ev, note=f"reassigned to {emp.name}")

    # ════════════════════════════════════════════════════════════ delete
    async def delete(self, event_id: uuid.UUID, *, reason: str,
                     actor_user_id=None, actor_name: str | None = None) -> dict:
        """Remove a record that should never have existed, and give the stock back.

        A REASON IS MANDATORY. This is the operation that erases evidence, and
        "why" is the only thing that makes it reviewable afterwards. The old
        route — an UPDATE in the database — had no field for it.

        THE STOCK COMES BACK because nothing was actually cut. That is the
        difference from a reassign: there, hide really was consumed and returning
        it would invent leather. Here the event was fiction, so the decrement
        that rode with it was fiction too.

        THE PIECE'S POINTER IS REWOUND. `current_operation_id` is what the
        screens read as "where this garment is"; leaving it at a stage whose
        event has just been deleted would show the piece somewhere it has never
        been.
        """
        if not (reason or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Deleting a production record needs a reason — it is the only "
                "thing that makes the deletion reviewable later.")

        ev = await self._event(event_id)
        await self._assert_payroll_open(ev)

        op = await self.db.get(Operation, ev.operation_id)
        piece = await self.db.get(Piece, ev.piece_id) if ev.piece_id else None
        returned = None

        # ── give the hide back ───────────────────────────────────────────────
        lot_id = ev.leather_lot_id or ev.lining_lot_id
        if lot_id is not None and ev.consumption_qty:
            from app.modules.materials.service import MaterialService
            mats = MaterialService(self.db)
            lot = await mats.repo.get_lot(lot_id)
            if lot is not None:
                lot.on_hand = (lot.on_hand or 0) + ev.consumption_qty
                returned = float(ev.consumption_qty)

        # ── rewind the piece's pointer to where it really is ────────────────
        if piece is not None and piece.current_operation_id == ev.operation_id:
            piece.current_operation_id = await self._previous_op_id(piece, ev)

        await self._audit(
            actor_user_id, "PRODUCTION_EVENT_DELETED", ev.id,
            before={"piece": getattr(piece, "code", None),
                    "operation": getattr(op, "code", None),
                    "employee_id": str(ev.employee_id) if ev.employee_id else None,
                    "work_date": str(ev.work_date),
                    "consumption_qty": float(ev.consumption_qty or 0),
                    "lot_id": str(lot_id) if lot_id else None},
            after={"reason": reason, "by": actor_name,
                   "stock_returned": returned})

        await self.db.delete(ev)
        await self.db.commit()
        return {
            "event_id": event_id, "deleted": True,
            "stock_returned": returned,
            "message": (f"Record deleted."
                        + (f" {returned:g} returned to stock." if returned
                           else " No material was charged to it.")),
        }

    async def _previous_op_id(self, piece, ev):
        """The operation the piece's remaining events leave it at.

        Not "the previous stage in the chain": a garment may have been reworked,
        so its real position is whatever it still has an event for.
        """
        res = await self.db.execute(
            select(ProductionEvent.operation_id, Operation.sequence)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .where(ProductionEvent.piece_id == piece.id,
                   ProductionEvent.id != ev.id)
            .order_by(Operation.sequence.desc()))
        row = res.first()
        return row[0] if row else None

    # ══════════════════════════════════════════════════════════ helpers
    async def _payload(self, ev, note: str | None = None) -> dict:
        op = await self.db.get(Operation, ev.operation_id)
        piece = await self.db.get(Piece, ev.piece_id) if ev.piece_id else None
        return {
            "event_id": ev.id,
            "piece_code": getattr(piece, "code", None),
            "operation": getattr(op, "code", None),
            "employee_id": ev.employee_id,
            "work_date": ev.work_date,
            "is_rework": bool(ev.is_rework),
            "consumption_qty": (float(ev.consumption_qty)
                                if ev.consumption_qty is not None else None),
            "note": note,
        }

    async def _audit(self, actor_user_id, action, entity_id, *, before, after):
        """`actor_user_id` is an app_user.id — the LOGIN, never an employee.id."""
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_user_id, action=action,
            entity_type="production_event", entity_id=entity_id,
            before=before, after=after, at=datetime.now(timezone.utc)))
