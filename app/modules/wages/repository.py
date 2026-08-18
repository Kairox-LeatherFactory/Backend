"""
================================================================================
modules/wages/repository.py — Async data access for rates & wage runs
================================================================================
effective_rate picks the latest rate <= work_date (effective-dated rates).
A CLOSED run is a frozen snapshot of wage_line rows — never recomputed.

TRANSACTION OWNERSHIP
    Every commit in the wages module happens HERE. The service layer never calls
    db.execute / db.commit / db.add — it composes repository calls and applies
    business rules. upsert_rate() commits one cell; bulk_upsert_rates() commits a
    whole sheet exactly once, so line 4 of 7 blowing up can't leave a half-saved
    rate sheet behind.
================================================================================
"""
import uuid
from datetime import date, timezone, datetime

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import RunStatus
from app.modules.wages.models import Rate, WageLine, WageLineDetailRow, WageRun


class WageRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── rates ───────────────────────────────────────────────────────────────
    async def effective_rate(
        self, style_id: uuid.UUID, operation_id: uuid.UUID, on: date
    ) -> float | None:
        """The rate in force for one operation on one style on `on`.

        Latest row with effective_from <= on. None means no rate was ever
        configured for that pair — the caller must NOT treat that as zero
        silently; compute_run reports it as unpaid work.
        """
        stmt = (
            select(Rate.rate)
            .where(
                Rate.style_id == style_id,
                Rate.operation_id == operation_id,
                Rate.effective_from <= on,
            )
            .order_by(Rate.effective_from.desc())
            .limit(1)
        )
        val = await self.db.scalar(stmt)
        return float(val) if val is not None else None

    async def rates_for_style(
        self, style_id: uuid.UUID, on: date
    ) -> dict[uuid.UUID, tuple[float, date]]:
        """Effective rate + its effective_from for EVERY operation of a style.

        Same semantics as effective_rate(), but window-ranked per operation so a
        7-operation sheet is one round trip instead of seven. row_number() over a
        partition is portable across Postgres and SQLite 3.25+; DISTINCT ON would
        be marginally faster but pins us to Postgres, and the test suite isn't.
        """
        ranked = (
            select(
                Rate.operation_id,
                Rate.rate,
                Rate.effective_from,
                func.row_number()
                .over(
                    partition_by=Rate.operation_id,
                    order_by=Rate.effective_from.desc(),
                )
                .label("rn"),
            )
            .where(Rate.style_id == style_id, Rate.effective_from <= on)
            .subquery()
        )
        rows = (
            await self.db.execute(
                select(ranked.c.operation_id, ranked.c.rate, ranked.c.effective_from)
                .where(ranked.c.rn == 1)
            )
        ).all()
        return {r[0]: (float(r[1]), r[2]) for r in rows}

    async def rated_operation_counts(
        self, style_ids: list[uuid.UUID], on: date
    ) -> dict[uuid.UUID, int]:
        """How many DISTINCT operations have a rate in force on `on`, per style.

        Feeds the "3 of 7 operations priced" badge on the styles landing screen —
        the manager needs to see which styles are unpriced BEFORE payroll runs and
        silently pays zero for them, not after.

        distinct(operation_id) matters: a style repriced three times over the year
        has three Rate rows for CUTTING and must still count as one priced
        operation.
        """
        if not style_ids:
            return {}
        stmt = (
            select(Rate.style_id, func.count(func.distinct(Rate.operation_id)))
            .where(Rate.style_id.in_(style_ids), Rate.effective_from <= on)
            .group_by(Rate.style_id)
        )
        return {r[0]: int(r[1]) for r in (await self.db.execute(stmt)).all()}

    async def rate_history(
        self, style_id: uuid.UUID, operation_id: uuid.UUID
    ) -> list[Rate]:
        """Every rate ever set for a pair, newest first. Audit view — 'why was
        April priced at 12.50 when the sheet says 14.00 today'."""
        return list(
            (
                await self.db.execute(
                    select(Rate)
                    .where(Rate.style_id == style_id, Rate.operation_id == operation_id)
                    .order_by(Rate.effective_from.desc())
                )
            ).scalars()
        )

    async def upsert_rate(
        self, style_id, operation_id, rate, effective_from
    ) -> Rate:
        """Single-cell save. Commits."""
        r = await self._upsert_rate_nocommit(style_id, operation_id, rate, effective_from)
        await self.db.commit()
        await self.db.refresh(r)
        return r

    async def bulk_upsert_rates(
        self,
        style_id: uuid.UUID,
        effective_from: date,
        lines: list[tuple[uuid.UUID, float]],
    ) -> int:
        """Save a whole edited rate sheet in ONE transaction. Returns rows written.

        `lines` is [(operation_id, rate)] — plain tuples, not the pydantic schema,
        so the repository stays ignorant of the API contract.

        Per-line commits would leave a half-saved sheet if line 4 of 7 blew up:
        three operations repriced, four still on yesterday's rate, and no error the
        manager can act on. One transaction or none.
        """
        for operation_id, rate in lines:
            await self._upsert_rate_nocommit(style_id, operation_id, rate, effective_from)
        await self.db.commit()
        return len(lines)

    async def _upsert_rate_nocommit(
        self, style_id, operation_id, rate, effective_from
    ) -> Rate:
        """upsert_rate without the commit. Internal — the public callers are
        upsert_rate (single cell, commits) and bulk_upsert_rates (batch, commits
        once)."""
        existing = await self.db.scalar(
            select(Rate).where(
                and_(
                    Rate.style_id == style_id,
                    Rate.operation_id == operation_id,
                    Rate.effective_from == effective_from,
                )
            )
        )
        if existing:
            existing.rate = rate
            return existing
        r = Rate(
            style_id=style_id,
            operation_id=operation_id,
            rate=rate,
            effective_from=effective_from,
        )
        self.db.add(r)
        await self.db.flush()
        return r

    async def runs_in_window(self, start: date, end: date,
                             *, exclude_run_id: uuid.UUID | None = None):
        """Every run — CLOSED **or OPEN** — whose date window intersects [start, end].

        DATES ONLY. This deliberately does NOT decide whether the runs conflict;
        it returns the candidates and the service compares the actual sets of
        styles each one pays. See WageService._validate_window for why that
        separation matters — the guard is about PIECES, and a repository method
        that also resolved order numbers to style ids would be answering a
        business question with a name that says it answers a date question.

        Replaces `overlapping_closed_run`, which compared SCOPE KEYS as strings
        and therefore got two cases wrong:
          • an order-scoped run blocked EVERY style-scoped run in the window,
            including styles belonging to a completely different order, because
            the rule was a conservative `scope_style_code IS NOT NULL`;
          • it could not see that a style-scoped run and an order-scoped run for
            THAT style's order really do collide, except by that same blunt rule.
        Both are answered exactly by comparing style-id sets instead.

        B7 (kept): OPEN runs are included. create_run() commits the run as OPEN
        before a single line is written and add_lines() commits separately, so a
        run that died mid-population leaves committed money in an OPEN run. An
        OPEN run in the window is either in progress or wreckage; either way a
        second run over the same pieces must not start silently.
        """
        stmt = select(WageRun).where(
            WageRun.period_start <= end,
            WageRun.period_end >= start,
        )
        if exclude_run_id:
            stmt = stmt.where(WageRun.id != exclude_run_id)
        # CLOSED first: when several runs clash, name the one that actually paid.
        stmt = stmt.order_by(WageRun.status.desc(), WageRun.period_start)
        return list((await self.db.scalars(stmt)).all())

    async def last_closed_run(self, *, exclude_run_id: uuid.UUID | None = None):
        """Latest CLOSED run — genuinely CLOSED-only: this feeds the gap_days
        calculation, which must measure from the last run that actually paid."""
        stmt = select(WageRun).where(WageRun.status == RunStatus.CLOSED)
        if exclude_run_id:
            stmt = stmt.where(WageRun.id != exclude_run_id)
        return await self.db.scalar(stmt.order_by(WageRun.period_end.desc()).limit(1))

    async def clear_lines(self, run_id: uuid.UUID) -> int:
        """Delete every frozen line + breakdown row of a run. ONE transaction.

        Bulk DELETE, not ORM cascade: loading 300 WageLine objects to delete them
        is three round trips and a lot of identity-map churn for an operation
        whose entire semantic is 'make these rows not exist'.
        """
        d1 = await self.db.execute(
            delete(WageLineDetailRow).where(WageLineDetailRow.wage_run_id == run_id))
        d2 = await self.db.execute(
            delete(WageLine).where(WageLine.wage_run_id == run_id))
        await self.db.commit()
        return int(d2.rowcount or 0) + int(d1.rowcount or 0)
    
    async def persist_breakdown(self, run_id: uuid.UUID, breakdown: dict) -> None:
        """Freeze the per-(employee, style, operation) rows. Commits once."""
        rows = [
            WageLineDetailRow(
                wage_run_id=run_id, employee_id=emp_id, style_id=style_id,
                operation_id=op_id, pieces=v["pieces"],
                rate=round(v["rate"] or 0, 2), amount=round(v["amount"], 2),
            )
            for (emp_id, style_id, op_id), v in breakdown.items()
        ]
        if not rows:
            return
        self.db.add_all(rows)
        await self.db.commit()
        
    async def stamp_recompute(self, run: WageRun, *, by: str) -> None:
        run.recompute_count = (run.recompute_count or 0) + 1
        run.last_recomputed_at = datetime.now(timezone.utc)
        run.last_recomputed_by = by
        await self.db.commit()

    async def stamp_reopen(self, run: WageRun, *, by: str, reason: str) -> None:
        """Unfreeze a CLOSED run, on the record.

        Counted separately from recompute because they are different facts: a
        recompute changes the arithmetic, a reopen removes the protection. A
        payslip that has been unfrozen after payment must be identifiable as
        such, and the reason has to be legible a year later.
        """
        run.status = RunStatus.OPEN
        run.reopen_count = (run.reopen_count or 0) + 1
        run.last_reopened_at = datetime.now(timezone.utc)
        run.last_reopened_by = by
        run.last_reopen_reason = reason
        await self.db.commit()

    async def create_run(self, period_start: date, period_end: date, *,
                         scope_order_number: str | None = None,
                         scope_style_code: str | None = None) -> WageRun:
        """Opens a run. Callers MUST validate the window before calling this —
        it commits, so a rejection afterwards strands an OPEN row."""
        run = WageRun(period_start=period_start, period_end=period_end,
                      scope_order_number=scope_order_number,
                      scope_style_code=scope_style_code)
        self.db.add(run)
        await self.db.commit()
        await self.db.refresh(run)
        return run

    async def get_run(self, run_id: uuid.UUID) -> WageRun | None:
        stmt = (
            select(WageRun)
            .where(WageRun.id == run_id)
            .options(selectinload(WageRun.lines))
        )
        return await self.db.scalar(stmt)

    async def add_lines(self, lines: list[WageLine]) -> None:
        self.db.add_all(lines)
        await self.db.commit()

    async def close_run(self, run: WageRun) -> WageRun | None:
        run.status = RunStatus.CLOSED
        await self.db.commit()
        return await self.get_run(run.id)

    async def delete_run(self, run: WageRun) -> None:
        """Only ever used to clean up an OPEN run that failed mid-compute."""
        await self.db.delete(run)
        await self.db.commit()

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Run summaries, newest first. Totals aggregated in SQL — loading every
        line of every run to sum them in Python does not survive two years of
        payroll."""
        stmt = (
            select(
                WageRun.id,
                WageRun.period_start,
                WageRun.period_end,
                WageRun.status,
                func.coalesce(func.sum(WageLine.amount), 0),
                func.coalesce(func.sum(WageLine.pieces), 0),
                func.count(WageLine.id),
            )
            .outerjoin(WageLine, WageLine.wage_run_id == WageRun.id)
            .group_by(
                WageRun.id, WageRun.period_start, WageRun.period_end, WageRun.status
            )
            .order_by(WageRun.period_end.desc())
            .limit(limit)
            .offset(offset)
        )
        return [
            {
                "id": r[0],
                "period_start": r[1],
                "period_end": r[2],
                "status": r[3],
                "total_amount": float(r[4]),
                "total_pieces": int(r[5]),
                "employee_count": int(r[6]),
                "unrated_operations": [],
                "gap_days": 0,
            }
            for r in (await self.db.execute(stmt)).all()
        ]

    async def run_lines_detailed(self, run_id: uuid.UUID) -> list[dict]:
        """Lines joined to the employee, each carrying its style/operation
        breakdown.

        TWO queries, not N+1: one for the lines, one for every breakdown row of
        the run, grouped in Python. A 300-employee run would otherwise issue 301
        queries to render one payslip screen.
        """
        from app.modules.clients.models import Style
        from app.modules.employees.models import Employee
        from app.modules.production.models import Operation

        line_stmt = (
            select(WageLine.id, WageLine.employee_id, Employee.name,
                   Employee.designation, WageLine.wage_type,
                   WageLine.pieces, WageLine.amount)
            .join(Employee, Employee.id == WageLine.employee_id)
            .where(WageLine.wage_run_id == run_id)
            .order_by(WageLine.wage_type, Employee.name)
        )
        detail_stmt = (
            select(WageLineDetailRow.employee_id, Style.code, Style.name,
                   Operation.code, Operation.label,
                   WageLineDetailRow.pieces, WageLineDetailRow.rate,
                   WageLineDetailRow.amount)
            .join(Style, Style.id == WageLineDetailRow.style_id)
            .join(Operation, Operation.id == WageLineDetailRow.operation_id)
            .where(WageLineDetailRow.wage_run_id == run_id)
            .order_by(Style.code, Operation.code)
        )

        details: dict[uuid.UUID, list[dict]] = {}
        for emp_id, scode, sname, ocode, olabel, pieces, rate, amount in (
            await self.db.execute(detail_stmt)
        ).all():
            details.setdefault(emp_id, []).append({
                "style_code": scode,
                "style_name": sname,
                "operation_code": ocode,
                "operation_label": olabel,
                "pieces": int(pieces),
                "rate": float(rate),
                "amount": float(amount),
            })

        out = []
        for r in (await self.db.execute(line_stmt)).all():
            emp_id = r[1]
            rows = details.get(emp_id, [])
            out.append({
                "id": r[0],
                "employee_id": emp_id,
                "employee_name": r[2],
                "designation": r[3],
                "wage_type": r[4],
                "pieces": int(r[5]),
                "amount": float(r[6]),
                # Per-piece rate for the line as a whole. Only meaningful when the
                # employee worked a single style x operation; null otherwise, so
                # the UI shows the breakdown rows instead of an average that is
                # true of nothing.
                "rate": rows[0]["rate"] if len(rows) == 1 else None,
                "style_codes": sorted({x["style_code"] for x in rows}),
                "breakdown": rows,
            })
        return out
    
    # ══════════════════════════════════════════════════════════════════════
    # THE PAYROLL REPORTING SURFACE (change-list item 3)
    # ══════════════════════════════════════════════════════════════════════
    # Everything below reads the FROZEN rows (wage_line / wage_line_detail) and
    # recomputes nothing. That is the whole contract: a ledger that re-derived
    # amounts at read time would show a different number every time somebody
    # corrected a historical production event, which is exactly what freezing a
    # run exists to prevent.

    async def run_breakdown(self, run_id: uuid.UUID) -> list[dict]:
        """Every frozen (employee, style, operation) row of a run, named.

        ONE query. The three groupings the payroll screen needs — per style, per
        stage, per employee — are all folds of this same row set, done in the
        service, so the three views can never disagree about a total.
        """
        from app.modules.clients.models import Style
        from app.modules.employees.models import Employee
        from app.modules.production.models import Operation

        stmt = (
            select(WageLineDetailRow.employee_id, Employee.name,
                   Employee.designation,
                   WageLineDetailRow.style_id, Style.code, Style.name,
                   WageLineDetailRow.operation_id, Operation.code, Operation.label,
                   Operation.sequence,
                   WageLineDetailRow.pieces, WageLineDetailRow.rate,
                   WageLineDetailRow.amount)
            .join(Employee, Employee.id == WageLineDetailRow.employee_id)
            .join(Style, Style.id == WageLineDetailRow.style_id)
            .join(Operation, Operation.id == WageLineDetailRow.operation_id)
            .where(WageLineDetailRow.wage_run_id == run_id)
            .order_by(Style.code, Operation.sequence, Employee.name)
        )
        return [{
            "employee_id": r[0], "employee_name": r[1], "designation": r[2],
            "style_id": r[3], "style_code": r[4], "style_name": r[5],
            "operation_id": r[6], "operation_code": r[7],
            "operation_label": r[8], "sequence": int(r[9] or 0),
            "pieces": int(r[10]), "rate": float(r[11]), "amount": float(r[12]),
        } for r in (await self.db.execute(stmt)).all()]

    async def run_piece_rows(self, run_id: uuid.UUID, run, *,
                             style_code: str | None = None,
                             limit: int = 2000, offset: int = 0) -> dict:
        """PER-PIECE payroll detail: which garment, which stage, which worker,
        their card, and what that one piece paid.

        WHY THE EVENTS AND NOT A STORED PIECE-LEVEL TABLE
            wage_line_detail freezes the money at (employee, style, operation)
            granularity, which is the level a rate is actually set at. Storing a
            row per piece as well would triple the payroll tables to hold no new
            money — the amount for one piece IS the frozen rate for its
            (employee, style, operation) cell.

            So the pieces come from production_event (which garment, which day,
            which worker — all immutable history) and the MONEY comes from the
            FROZEN cell. Nothing is re-priced: if the rate table changed after the
            run closed, this still shows what the run paid.

        Pieces whose cell is not in the frozen breakdown (a monthly worker, or an
        unrated operation) come back with `amount: null` and a `note`, rather than
        a zero that reads like the work was worth nothing.
        """
        from app.modules.clients.models import SKU, Style
        from app.modules.employees.models import Employee
        from app.modules.barcode.models import BarcodeRegistry
        from app.core.enums import BarcodeStatus, BarcodeType
        from app.modules.production.models import (Operation, Piece,
                                                   ProductionEvent)

        frozen = {(r["employee_id"], r["style_id"], r["operation_id"]):
                  (r["rate"], r["pieces"]) for r in await self.run_breakdown(run_id)}

        stmt = (
            select(Piece.code, Piece.seq, SKU.color_name, SKU.size,
                   Style.id, Style.code, Style.name,
                   Operation.id, Operation.code, Operation.label, Operation.sequence,
                   Employee.id, Employee.name, Employee.designation,
                   ProductionEvent.work_date, ProductionEvent.qty,
                   BarcodeRegistry.code.label("employee_barcode"))
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .outerjoin(
                BarcodeRegistry,
                and_(BarcodeRegistry.employee_id == Employee.id,
                     BarcodeRegistry.type == BarcodeType.EMPLOYEE.value,
                     BarcodeRegistry.status == BarcodeStatus.ACTIVE.value))
            .where(ProductionEvent.work_date >= run.period_start,
                   ProductionEvent.work_date <= run.period_end)
            .order_by(Style.code, Operation.sequence, Piece.code)
        )
        if style_code:
            stmt = stmt.where(func.upper(Style.code) == style_code.strip().upper())
        elif run.scope_style_code:
            stmt = stmt.where(func.upper(Style.code) == run.scope_style_code.upper())
        if run.scope_order_number:
            from app.modules.clients.models import ClientOrder
            stmt = stmt.join(
                ClientOrder, ClientOrder.id == Style.client_order_id).where(
                    ClientOrder.order_number == run.scope_order_number)

        total = int(await self.db.scalar(
            select(func.count()).select_from(stmt.subquery())) or 0)
        rows = (await self.db.execute(stmt.limit(limit).offset(offset))).all()

        items = []
        for r in rows:
            cell = frozen.get((r[11], r[4], r[7]))
            rate = cell[0] if cell else None
            items.append({
                "piece_code": r[0],
                "serial": f"{r[1]:03d}" if r[1] is not None else None,
                "colour": r[2], "size": r[3],
                "style_code": r[5], "style_name": r[6],
                "operation_code": r[8], "operation_label": r[9],
                "employee_id": r[11], "employee_name": r[12],
                "designation": r[13],
                # What the scanner gun reads — the change list asks for the card
                # number beside the name on every payroll line.
                "employee_barcode": r[16],
                "work_date": r[14],
                "qty": int(r[15] or 1),
                "rate": rate,
                "amount": (round(rate * int(r[15] or 1), 2)
                           if rate is not None else None),
                "note": None if rate is not None else (
                    "Not priced in this run — the worker is on a monthly wage, or "
                    "this style/stage had no rate when the run was computed."),
            })
        return {"total": total, "count": len(items), "items": items}

    async def ledger(self, *, order_number: str | None = None,
                     style_code: str | None = None,
                     date_from: date | None = None, date_to: date | None = None,
                     status: str | None = None,
                     limit: int = 50, offset: int = 0) -> dict:
        """THE LEDGER: every computed run, LATEST FIRST, searchable.

        Ordered by `created_at DESC` and not by period, because the change list
        asks for "the latest computed first" — the question a manager asks the
        ledger is "what did we just run", not "which fortnight is chronologically
        last". A recompute does not create a new row, so the recompute stamps on
        the summary are what tell you a run has been rebuilt since.
        """
        stmt = (
            select(WageRun.id, WageRun.period_start, WageRun.period_end,
                   WageRun.status, WageRun.scope_order_number,
                   WageRun.scope_style_code, WageRun.created_at,
                   WageRun.recompute_count, WageRun.last_recomputed_at,
                   WageRun.reopen_count,
                   func.coalesce(func.sum(WageLine.amount), 0),
                   func.coalesce(func.sum(WageLine.pieces), 0),
                   func.count(WageLine.id))
            .outerjoin(WageLine, WageLine.wage_run_id == WageRun.id)
            .group_by(WageRun.id)
            .order_by(WageRun.created_at.desc())
        )
        if order_number:
            stmt = stmt.where(WageRun.scope_order_number == order_number.strip())
        if style_code:
            stmt = stmt.where(
                func.upper(WageRun.scope_style_code) == style_code.strip().upper())
        if date_from:
            stmt = stmt.where(WageRun.period_end >= date_from)
        if date_to:
            stmt = stmt.where(WageRun.period_start <= date_to)
        if status:
            stmt = stmt.where(WageRun.status == RunStatus(status.lower()))

        rows = (await self.db.execute(stmt.limit(limit).offset(offset))).all()
        return {"count": len(rows), "items": [{
            "run_id": r[0], "period_start": r[1], "period_end": r[2],
            "status": r[3], "scope_order_number": r[4], "scope_style_code": r[5],
            "computed_at": r[6], "recompute_count": int(r[7] or 0),
            "last_recomputed_at": r[8], "reopen_count": int(r[9] or 0),
            "total_amount": float(r[10]), "total_pieces": int(r[11]),
            "employee_count": int(r[12]),
        } for r in rows]}

    # ── H8: one run, one transaction ─────────────────────────────────────────
    # The committing variants above stay for now so existing callers keep
    # working. New payroll paths use these and let the SERVICE commit once, so a
    # run is never durable in a half-built state (which is what makes B6 and B7
    # possible in the first place).

    async def create_run_nocommit(self, period_start: date,
                                  period_end: date) -> WageRun:
        run = WageRun(period_start=period_start, period_end=period_end)
        self.db.add(run)
        await self.db.flush()          # assigns run.id, stays in-transaction
        return run
    
    async def add_lines_nocommit(self, lines: list[WageLine]) -> None:
        self.db.add_all(lines)
        await self.db.flush()

    async def persist_breakdown_nocommit(self, run_id: uuid.UUID,
                                         breakdown: dict) -> None:
        rows = [
            WageLineDetailRow(
                wage_run_id=run_id, employee_id=emp_id, style_id=style_id,
                operation_id=op_id, pieces=v["pieces"],
                rate=round(v["rate"] or 0, 2), amount=round(v["amount"], 2),
            )
            for (emp_id, style_id, op_id), v in breakdown.items()
        ]
        if not rows:
            return
        self.db.add_all(rows)
        await self.db.flush()

    async def clear_lines_nocommit(self, run_id: uuid.UUID) -> int:
        d1 = await self.db.execute(
            delete(WageLineDetailRow).where(WageLineDetailRow.wage_run_id == run_id))
        d2 = await self.db.execute(
            delete(WageLine).where(WageLine.wage_run_id == run_id))
        await self.db.flush()
        return int(d2.rowcount or 0) + int(d1.rowcount or 0)

    def close_run_nocommit(self, run: WageRun) -> None:
        run.status = RunStatus.CLOSED

    async def commit(self) -> None:
        """The single commit for a whole payroll run."""
        await self.db.commit()

    async def rollback(self) -> None:
        await self.db.rollback()
