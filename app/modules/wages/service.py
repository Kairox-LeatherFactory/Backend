"""
================================================================================
modules/wages/service.py — Wage-calc engine (async)
================================================================================
THE FORK — on Employee.wage_type, and nothing else:

  PIECE_RATE  sum(qty * effective_rate(style, op, work_date)) over the window.
              Each day's pieces are priced at the rate effective on THAT day, so
              a mid-period rate change splits correctly across earlier/later work.
              No floor, no daily guarantee, no attendance minimum. The pieces they
              cut ARE the wage.

  MONTHLY     monthly_salary prorated across the calendar months the window
              touches. Independent of production — a monthly tailor who logs 400
              pieces earns exactly his salary, not salary + piece money.

An employee gets EXACTLY ONE line. The old code populated the piece loop from
production events without checking wage_type, then added a monthly line for every
monthly employee — so a salaried tailor who logged output was paid twice. The
employees model docstring says it outright: TAILOR and CUTTER appear in BOTH the
monthly and piece-rate blocks of the source file. That is the common case here.

CODES IN, IDS NEVER OUT.
    Every rate entry point takes style_code / operation_code and resolves it here.
    _resolve_style and _resolve_operations are the only doors; a bad code is a 404
    with the code echoed back, not a silent empty sheet. Mirrors /production/scan,
    which takes sku_code for the same reason: a manager can read a code off a
    printed traveler.

PERIODS ARE HAND-TYPED. The manager enters period_start and period_end; nothing
is derived. compute_run therefore carries the guards that make freehand dates
safe — future dates rejected, inverted windows rejected, and any window that
intersects a CLOSED run rejected with 409, because the alternative is paying the
same fortnight twice and never finding out.

Results are FROZEN into wage_line rows so a closed run never recomputes.

LAYERING
    This file contains NO database access. No db.execute, no db.commit, no db.add.
    Every query and every transaction boundary lives in WageRepository; the service
    composes repository calls and applies business rules. Cross-module reads go
    through ClientService / EmployeeService / ProductionService, never their
    repositories — so wages can be lifted into its own process later without
    unpicking a web of direct table access.
================================================================================
"""
import uuid
from collections import defaultdict
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunStatus, WageType
from app.modules.clients.service import ClientService
from app.modules.employees.service import EmployeeService
from app.modules.production.service import ProductionService
from app.modules.wages.models import WageLine
from app.modules.wages.proration import prorate_monthly
from app.modules.wages.repository import WageRepository


def _as_wage_type(value) -> WageType | None:
    """Coerce whatever the ORM handed us into a WageType member.

    Employee.wage_type is an Enum() column so it normally arrives as a member, but
    the employees module compares it with .lower() in places, which means raw
    strings have leaked through before. An `is` check against a string fails
    silently and would send a monthly worker back into the piece loop — the exact
    bug this module exists to fix — so this normalises rather than trusting.
    """
    if isinstance(value, WageType):
        return value
    if value is None:
        return None
    try:
        return WageType(str(value).lower())
    except ValueError:
        return None


class WageService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = WageRepository(db)
        self.production = ProductionService(db)
        self.employees = EmployeeService(db)
        self.clients = ClientService(db)

    # ── code resolution ─────────────────────────────────────────────────────
    async def _resolve_style(self, style_code: str) -> dict:
        """style_code -> {style_id, style_code, style_name, order_number}. 404s.

        The ONLY place a style code becomes an id. Codes are uppercase slugs
        (make_style_code), so input is normalised before lookup — a manager typing
        'jp-clermont_vest' must not get a 404 for a style that exists.
        """
        code = (style_code or "").strip().upper()
        if not code:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "style_code is required"
            )
        style = await self.clients.get_style_summary_by_code(code)
        if not style:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"No style with code '{code}'"
            )
        return style

    async def _resolve_operations(self) -> tuple[list, dict[str, object]]:
        """(operations in sequence order, {CODE: operation}).

        One call serves both the rate sheet and code resolution — operations are a
        short fixed list (CUTTING..FF), so fetching all of them beats a lookup per
        code and gives the sheet its rows for free.
        """
        ops = await self.production.list_operations()
        ops = sorted(ops, key=lambda o: o.sequence)
        return ops, {o.code.strip().upper(): o for o in ops} #used only

    @staticmethod
    def _resolve_operation(by_code: dict, operation_code: str):
        code = (operation_code or "").strip().upper()
        op = by_code.get(code)
        if not op:
            known = ", ".join(sorted(by_code)) or "(none configured)"
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No operation with code '{code}'. Known operations: {known}",
            )
        return op

    # ── style picker ────────────────────────────────────────────────────────
    async def list_styles(
        self,
        *,
        order_number: str | None = None,
        client_id: uuid.UUID | None = None,
        unpriced_only: bool = False,
        on: date | None = None,
    ) -> list[dict]:
        """The wages landing screen: readable style codes with pricing coverage.

        Style-level, not SKU-level. A Rate is per style x operation, so listing SKU
        codes would show JP-CLERMONT_VEST-DARK_BROWN-46, -48, -BLACK-46 ... as
        separate rows that all write to the same Rate row.

        unpriced_only filters to styles with at least one operation lacking a rate —
        those are the ones that will silently pay a worker zero.
        """
        on = on or date.today()
        rows = await self.clients.list_style_options(
            order_number=order_number, client_id=client_id
        )
        if not rows:
            return []

        ops, _ = await self._resolve_operations()
        total_ops = len(ops)
        counts = await self.repo.rated_operation_counts([r["style_id"] for r in rows], on)

        out = []
        for r in rows:
            rated = counts.get(r["style_id"], 0)
            fully = total_ops > 0 and rated >= total_ops
            if unpriced_only and fully:
                continue
            out.append(
                {
                    "style_code": r["style_code"],
                    "style_name": r["style_name"],
                    "article": r["article"],
                    "order_number": r["order_number"],
                    "sku_count": r["sku_count"],
                    "qty_ordered": r["qty_ordered"],
                    "rated_operations": rated,
                    "total_operations": total_ops,
                    "fully_rated": fully,
                }
            )
        return out

    # ── rates ───────────────────────────────────────────────────────────────
    async def rate_sheet(self, style_code: str, on: date) -> dict:
        """Every operation of a style with its rate in force on `on`.

        One screen: pick a style code, edit the column, POST /rates/bulk. Operations
        with rate=None are the ones compute_run cannot price, so they are counted
        out for the UI rather than buried in a list.
        """
        style = await self._resolve_style(style_code)
        ops, _ = await self._resolve_operations()
        rates = await self.repo.rates_for_style(style["style_id"], on)

        rows = [
            {
                "operation_code": op.code,
                "label": op.label,
                "sequence": op.sequence,
                "rate": rates[op.id][0] if op.id in rates else None,
                "effective_from": rates[op.id][1] if op.id in rates else None,
            }
            for op in ops
        ]
        return {
            "style_code": style["style_code"],
            "style_name": style["style_name"],
            "order_number": style["order_number"],
            "on": on,
            "operations": rows,
            "missing_rate_count": sum(1 for r in rows if r["rate"] is None),
        }

    async def rate_history(self, style_code: str, operation_code: str) -> dict:
        """Every rate ever set for one style x operation, newest first.

        The audit answer to "why was April priced at 12.50 when the sheet says
        14.00 today" — effective-dating means both are correct.
        """
        style = await self._resolve_style(style_code)
        _, by_code = await self._resolve_operations()
        op = self._resolve_operation(by_code, operation_code)
        history = await self.repo.rate_history(style["style_id"], op.id)
        return {
            "style_code": style["style_code"],
            "operation_code": op.code,
            "history": [
                {"rate": float(h.rate), "effective_from": h.effective_from}
                for h in history
            ],
        }

    async def set_rate(self, body) -> dict:
        """Single-cell save."""
        style = await self._resolve_style(body.style_code)
        _, by_code = await self._resolve_operations()
        op = self._resolve_operation(by_code, body.operation_code)
        await self.repo.upsert_rate(
            style["style_id"], op.id, body.rate, body.effective_from
        )
        return {
            "style_code": style["style_code"],
            "operation_code": op.code,
            "rate": body.rate,
            "effective_from": body.effective_from.isoformat(),
        }

    async def set_rates_bulk(self, body) -> dict:
        """One save of an edited rate sheet. The repository owns the transaction.

        Every operation_code is resolved BEFORE the first write: a typo in line 6
        must fail the whole save, not commit lines 1-5 and then 404.
        """
        style = await self._resolve_style(body.style_code)
        _, by_code = await self._resolve_operations()
        pairs = [
            (self._resolve_operation(by_code, ln.operation_code).id, ln.rate)
            for ln in body.lines
        ]
        saved = await self.repo.bulk_upsert_rates(
            style["style_id"], body.effective_from, pairs
        )
        return {
            "style_code": style["style_code"],
            "effective_from": body.effective_from.isoformat(),
            "saved": saved,
        }

    # ── runs ────────────────────────────────────────────────────────────────
    async def _validate_window(self, period_start: date, period_end: date) -> int:
        """Guards for a hand-typed window. Returns gap_days.

        Runs BEFORE create_run, because create_run commits — validating after it
        would strand an orphaned OPEN row behind every rejected request, which is
        what the previous version did.
        """
        if period_end < period_start:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "period_end is before period_start",
            )
        if period_end > date.today():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "cannot run payroll for future dates",
            )

        clash = await self.repo.overlapping_closed_run(period_start, period_end)
        if clash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Period overlaps closed run {clash.id} "
                f"({clash.period_start}..{clash.period_end}). Payroll windows must "
                f"not intersect — the same pieces would be paid twice.",
            )

        # Gap detection. Not an error: the manager may legitimately skip a stretch.
        # But hand-typed periods make an accidental gap invisible, so we surface it.
        last = await self.repo.last_closed_run()
        if last and period_start > last.period_end:
            return max(0, (period_start - last.period_end).days - 1)
        return 0

    async def compute_run(self, period_start: date, period_end: date) -> dict:
        """Compute and FREEZE payroll for a hand-entered window.

        Returns a SUMMARY (totals + warnings), not the lines — the lines are what
        GET /runs/{id} is for. Raises 409 on overlap, 422 on an invalid window.
        """
        gap_days = await self._validate_window(period_start, period_end)
        run = await self.repo.create_run(period_start, period_end)

        try:
            employees = await self.employees.list_all(active_only=True)
            wage_type_of = {e.id: _as_wage_type(e.wage_type) for e in employees}

            lines: list[WageLine] = []
            per_emp_amount: dict[uuid.UUID, float] = defaultdict(float)
            per_emp_pieces: dict[uuid.UUID, int] = defaultdict(int)
            unrated: dict[tuple, int] = defaultdict(int)

            # ── piece-rate population ───────────────────────────────────────
            # Rows are grouped per (emp, style, op, work_date) so each day is priced
            # at the rate effective on THAT day. rate_cache collapses the per-row
            # lookups: most employees share the same (style, op, work_date), so we
            # hit the DB once per key rather than once per row.
            rate_cache: dict[tuple[uuid.UUID, uuid.UUID, date], float | None] = {}
            rows = await self.production.piece_counts(period_start, period_end)
            for emp_id, style_id, op_id, work_date, qty in rows:
                # THE GUARD. A monthly tailor logs production too — his output must
                # not mint a piece-rate line on top of his salary.
                if wage_type_of.get(emp_id) is not WageType.PIECE_RATE:
                    continue
                key = (style_id, op_id, work_date)
                if key not in rate_cache:
                    rate_cache[key] = await self.repo.effective_rate(
                        style_id, op_id, work_date
                    )
                rate = rate_cache[key]
                if rate is None:
                    # Real work that priced to zero. Silently skipping it is how a
                    # worker opens an empty envelope, so it rides out in the summary.
                    unrated[(style_id, op_id)] += int(qty)
                    continue
                per_emp_amount[emp_id] += float(qty) * rate
                per_emp_pieces[emp_id] += int(qty)

            for emp_id, amount in per_emp_amount.items():
                lines.append(
                    WageLine(
                        wage_run_id=run.id,
                        employee_id=emp_id,
                        wage_type=WageType.PIECE_RATE,
                        pieces=per_emp_pieces[emp_id],
                        amount=round(amount, 2),
                    )
                )

            # ── monthly population ──────────────────────────────────────────
            # Independent of production. Prorated because a hand-typed window will
            # routinely straddle month boundaries, and a full salary for a 9-day
            # window is not a rounding error — it's a payroll incident.
            for emp in employees:
                if _as_wage_type(emp.wage_type) is not WageType.MONTHLY:
                    continue
                lines.append(
                    WageLine(
                        wage_run_id=run.id,
                        employee_id=emp.id,
                        wage_type=WageType.MONTHLY,
                        pieces=0,
                        amount=prorate_monthly(
                            float(emp.monthly_salary or 0), period_start, period_end
                        ),
                    )
                )

            await self.repo.add_lines(lines)
            await self.repo.close_run(run)
            unrated_out = await self._name_unrated(unrated)
        except Exception:
            # create_run already committed. Leaving an OPEN run behind would make the
            # next overlap check pass (it only looks at CLOSED) while the run list
            # shows a phantom.
            await self.repo.delete_run(run)
            raise

        return {
            "id": run.id,
            "period_start": period_start,
            "period_end": period_end,
            "status": RunStatus.CLOSED,
            "total_amount": round(sum(float(ln.amount) for ln in lines), 2),
            "total_pieces": sum(int(ln.pieces) for ln in lines),
            "employee_count": len(lines),
            "unrated_operations": unrated_out,
            "gap_days": gap_days,
        }

    async def _name_unrated(self, unrated: dict[tuple, int]) -> list[dict]:
        """Turn {(style_id, op_id): qty} into codes the frontend can link on.

        Raw UUIDs here would make the warning unactionable — the manager would see
        that 340 pieces went unpaid and have no way to reach the rate sheet that
        fixes it.
        """
        if not unrated:
            return []
        style_ids = list({s for s, _ in unrated})
        styles = await self.clients.get_style_codes(style_ids)
        ops, _ = await self._resolve_operations()
        op_by_id = {o.id: o for o in ops}
        out = []
        for (style_id, op_id), qty in unrated.items():
            st = styles.get(style_id) or {}
            op = op_by_id.get(op_id)
            out.append(
                {
                    "style_code": st.get("style_code") or str(style_id),
                    "style_name": st.get("style_name") or "(unknown style)",
                    "operation_code": op.code if op else str(op_id),
                    "unpaid_pieces": qty,
                }
            )
        out.sort(key=lambda r: -r["unpaid_pieces"])
        return out

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return await self.repo.list_runs(limit, offset)

    async def get_run_detail(self, run_id: uuid.UUID) -> dict:
        """Re-read a frozen run. This is how April payroll is reprinted in June
        without recomputing it — the whole point of freezing the lines."""
        run = await self.repo.get_run(run_id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wage run not found")
        lines = await self.repo.run_lines_detailed(run_id)
        return {
            "id": run.id,
            "period_start": run.period_start,
            "period_end": run.period_end,
            "status": run.status,
            "total_amount": round(sum(ln["amount"] for ln in lines), 2),
            "total_pieces": sum(ln["pieces"] for ln in lines),
            "employee_count": len(lines),
            "unrated_operations": [],
            "gap_days": 0,
            "lines": lines,
        }
