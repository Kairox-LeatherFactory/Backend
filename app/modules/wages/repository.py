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
from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import RunStatus
from app.modules.wages.models import Rate, WageLine, WageRun


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

    # ── runs ────────────────────────────────────────────────────────────────
    async def overlapping_closed_run(self, start: date, end: date) -> WageRun | None:
        """Any CLOSED run whose window intersects [start, end].

        Two inclusive ranges overlap iff a.start <= b.end AND a.end >= b.start.
        This is the guard that makes hand-typed periods safe: without it, running
        Apr 1-30 and then Apr 15-May 15 pays the same fortnight twice, closes both
        runs, and nothing in the system ever notices.
        """
        return await self.db.scalar(
            select(WageRun)
            .where(
                WageRun.status == RunStatus.CLOSED,
                WageRun.period_start <= end,
                WageRun.period_end >= start,
            )
            .limit(1)
        )

    async def last_closed_run(self) -> WageRun | None:
        """Most recently ENDING closed run — the reference for gap detection."""
        return await self.db.scalar(
            select(WageRun)
            .where(WageRun.status == RunStatus.CLOSED)
            .order_by(WageRun.period_end.desc())
            .limit(1)
        )

    async def create_run(self, period_start: date, period_end: date) -> WageRun:
        """Opens a run. Callers MUST validate the window before calling this —
        it commits, so a rejection afterwards strands an OPEN row."""
        run = WageRun(period_start=period_start, period_end=period_end)
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
        """Lines joined to the employee. WageLine has no Employee relationship, so
        the name is joined explicitly — lazy-loading it would raise MissingGreenlet
        on the async session."""
        from app.modules.employees.models import Employee

        stmt = (
            select(
                WageLine.id,
                WageLine.employee_id,
                Employee.name,
                Employee.designation,
                WageLine.wage_type,
                WageLine.pieces,
                WageLine.amount,
            )
            .join(Employee, Employee.id == WageLine.employee_id)
            .where(WageLine.wage_run_id == run_id)
            .order_by(WageLine.wage_type, Employee.name)
        )
        return [
            {
                "id": r[0],
                "employee_id": r[1],
                "employee_name": r[2],
                "designation": r[3],
                "wage_type": r[4],
                "pieces": int(r[5]),
                "amount": float(r[6]),
            }
            for r in (await self.db.execute(stmt)).all()
        ]
