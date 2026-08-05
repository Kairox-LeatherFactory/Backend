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
from app.modules.attendance.service import AttendanceService
from app.modules.clients.service import ClientService
from app.modules.employees.service import EmployeeService
from app.modules.production.service import ProductionService
from app.modules.wages.models import WageLine
from app.modules.wages.proration import prorate_monthly
from app.modules.wages.repository import WageRepository
from app.core.config import settings


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


# H12: sentinel key prefix for the MONTHLY-salary-missing exclusion, so
# _name_unrated can tell it apart from a genuine (style_id, op_id) pair.
_MONTHLY_SALARY_MISSING = "MONTHLY_SALARY_MISSING"


class WageService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = WageRepository(db)
        self.production = ProductionService(db)
        self.employees = EmployeeService(db)
        self.clients = ClientService(db)
        # H12: needed by the monthly-attendance policy in _populate_run.
        self.attendance = AttendanceService(db)

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
        return ops, {o.code.strip().upper(): o for o in ops}

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
    async def _validate_window(self, period_start: date, period_end: date,
                               *, replacing: uuid.UUID | None = None) -> int:
        """Guards for a hand-typed window. Returns gap_days.

        `replacing` is the run being recomputed — its own window must not count
        as an overlap with itself, or recompute would always 409.
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

        # B7: overlapping_closed_run now returns any CLOSED **or OPEN** run in the
        # window, so a run that died mid-population can no longer hide from this.
        clash = await self.repo.overlapping_closed_run(
            period_start, period_end, exclude_run_id=replacing
        )
        if clash:
            state = ("closed" if clash.status == RunStatus.CLOSED
                     else "OPEN (in progress, or abandoned mid-compute)")
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Period overlaps {state} run {clash.id} "
                f"({clash.period_start}..{clash.period_end}). Payroll windows must "
                f"not intersect — the same pieces would be paid twice. If that run "
                f"is wreckage from a failed compute, delete it before retrying.",
            )

        last = await self.repo.last_closed_run(exclude_run_id=replacing)
        if last and period_start > last.period_end:
            return max(0, (period_start - last.period_end).days - 1)
        return 0

    async def recompute_run(self, run_id: uuid.UUID, *, user_name: str,
                            confirm_closed: bool = False) -> dict:
        """Re-run payroll for an EXISTING run's window, discarding its old lines.

        WHY THIS IS SAFE, AND WHERE IT IS NOT
            A closed run is a frozen snapshot: silently rewriting it means last
            month's payslip no longer matches the cash that left the building.
            That protection is preserved by making recompute EXPLICIT, GUARDED
            and AUDITED, not by making it impossible:
              • it targets one named run_id — never a side effect
              • recomputing a CLOSED run requires confirm_closed=True (B6): the
                caller must say, in the request, that they know they are
                rewriting a document that was already paid against
              • the run's own window is excluded from the overlap check, so it
                cannot smuggle in a second payment for a different period
              • the old lines are SNAPSHOT, then deleted, then RESTORED if the
                repopulate fails (B6) — a crashed recompute can no longer leave a
                CLOSED run with zero lines for a fortnight already in envelopes
              • recompute_count / last_recomputed_at / last_recomputed_by are
                stamped, so a payslip reprinted after a recompute is visibly a
                different document from the one paid against
              • it is DM/MD only (see router)

            WHAT IT STILL CANNOT PROTECT YOU FROM: cash already disbursed.
            Recomputing on Monday changes the record, not the payment.
        """
        run = await self.repo.get_run(run_id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wage run not found")

        # B6: a frozen run is frozen until someone explicitly unfreezes it.
        if run.status == RunStatus.CLOSED and not confirm_closed:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Run {run.id} is CLOSED ({run.period_start}..{run.period_end}) and "
                f"was already paid against. Re-send with confirm_closed=true to "
                f"rewrite it; the run will be stamped as recomputed.")

        gap_days = await self._validate_window(
            run.period_start, run.period_end, replacing=run.id
        )

        # B6: snapshot before destroying. clear_lines() commits, so without this
        # a failure inside _populate_run leaves the run CLOSED and empty.
        prior = [
            WageLine(wage_run_id=run.id, employee_id=ln.employee_id,
                     wage_type=ln.wage_type, pieces=ln.pieces, amount=ln.amount)
            for ln in (run.lines or [])
        ]

        await self.repo.clear_lines(run.id)
        try:
            payload = await self._populate_run(
                run, run.period_start, run.period_end, gap_days=gap_days
            )
        except Exception:
            # Put the paid-against lines back before surfacing the failure.
            if prior:
                await self.repo.add_lines(prior)
            raise

        await self.repo.stamp_recompute(run, by=user_name)
        payload["recomputed"] = True
        payload["recompute_count"] = run.recompute_count
        return payload

    async def compute_run(self, period_start: date, period_end: date) -> dict:
        """Compute and FREEZE payroll for a hand-entered window."""
        gap_days = await self._validate_window(period_start, period_end)
        run = await self.repo.create_run(period_start, period_end)
        try:
            payload = await self._populate_run(
                run, period_start, period_end, gap_days=gap_days
            )
        except Exception:
            await self.repo.delete_run(run)
            raise
        payload["recomputed"] = False
        payload["recompute_count"] = 0
        return payload

    async def _populate_run(self, run, period_start: date, period_end: date,
                            *, gap_days: int) -> dict:
        """Shared body of compute_run and recompute_run.

        Extracted so recompute cannot drift from compute — two copies of payroll
        arithmetic is how a factory ends up with two different answers for the
        same fortnight depending on which button was pressed.
        """
        # H13: include LEAVERS. active_only=True dropped anyone deactivated
        # between working and payday — their pieces then failed the wage_type
        # lookup and vanished from payroll with no warning. A leaver's final
        # fortnight is the single most disputed payslip in a factory.
        employees = await self.employees.list_all(active_only=False)
        wage_type_of = {e.id: _as_wage_type(e.wage_type) for e in employees}

        # H13: an unrecognised wage_type coerces to None (see _as_wage_type) and
        # then fails BOTH branch tests, so the worker is paid nothing and nobody
        # is told. Collect the ACTIVE ones so the run can report them rather than
        # swallow them (inactive + untyped is just a stale record, not a dispute).
        untyped = [e for e in employees
                   if wage_type_of.get(e.id) is None and e.is_active]

        lines: list[WageLine] = []
        per_emp_amount: dict[uuid.UUID, float] = defaultdict(float)
        per_emp_pieces: dict[uuid.UUID, int] = defaultdict(int)
        # Per-employee, per-(style, op) breakdown for the analytics surface.
        #   {(emp_id, style_id, op_id): {"pieces": n, "amount": x, "rate": r}}
        breakdown: dict[tuple, dict] = defaultdict(
            lambda: {"pieces": 0, "amount": 0.0, "rate": None}
        )
        unrated: dict[tuple, int] = defaultdict(int)

        # ── PIECE_RATE branch ────────────────────────────────────────────────
        rate_cache: dict[tuple[uuid.UUID, uuid.UUID, date], float | None] = {}
        rows = await self.production.piece_counts(period_start, period_end)
        for emp_id, style_id, op_id, work_date, qty in rows:
            if wage_type_of.get(emp_id) is not WageType.PIECE_RATE:
                continue
            key = (style_id, op_id, work_date)
            if key not in rate_cache:
                rate_cache[key] = await self.repo.effective_rate(style_id, op_id, work_date)
            rate = rate_cache[key]
            if rate is None:
                unrated[(style_id, op_id)] += int(qty)
                continue
            amount = float(qty) * rate
            per_emp_amount[emp_id] += amount
            per_emp_pieces[emp_id] += int(qty)

            b = breakdown[(emp_id, style_id, op_id)]
            b["pieces"] += int(qty)
            b["amount"] += amount
            # H11: store the BLENDED rate actually paid, not "last rate wins".
            # If a rate changed mid-period these pieces were priced at several
            # rates; effective_rate is the authority and `amount` already
            # reflects the split. Showing one of the several rates made the
            # printed line read pieces × rate != amount, and WHICH rate showed
            # was nondeterministic (piece_counts_by_employee_style_op has no
            # ORDER BY). amount / pieces always reconciles.
            b["rate"] = round(b["amount"] / b["pieces"], 4) if b["pieces"] else rate

        for emp_id, amount in per_emp_amount.items():
            lines.append(WageLine(
                wage_run_id=run.id, employee_id=emp_id,
                wage_type=WageType.PIECE_RATE,
                pieces=per_emp_pieces[emp_id], amount=round(amount, 2),
            ))

        # ── MONTHLY branch ───────────────────────────────────────────────────
        for emp in employees:
            if _as_wage_type(emp.wage_type) is not WageType.MONTHLY:
                continue
            # H13: a monthly line for a deactivated worker is wrong — proration is
            # calendar-only and would pay a leaver their full salary. The
            # piece-rate branch above legitimately pays a leaver for pieces they
            # actually cut; the monthly branch has no such evidence, so skip.
            if not emp.is_active:
                continue
            # H12: a NULL salary is a data error, not a ₹0.00 payslip. Surface it
            # the same way an unrated piece-rate operation is surfaced, rather
            # than emitting a zero line that still counts toward employee_count.
            if emp.monthly_salary is None:
                unrated[(_MONTHLY_SALARY_MISSING, emp.id)] += 1
                continue
            amount = prorate_monthly(
                float(emp.monthly_salary), period_start, period_end
            )
            # H12: proration is CALENDAR-only — it takes no attendance input, so
            # a monthly worker who never attended is paid in full. That may be
            # your policy, but it must be a decision, not an omission. When
            # settings.monthly_wage_requires_attendance is on, scale by the days
            # actually worked.
            if getattr(settings, "monthly_wage_requires_attendance", False):
                present = await self.attendance.days_present(
                    emp.id, period_start, period_end)
                span = (period_end - period_start).days + 1
                if span > 0:
                    amount = round(amount * (min(present, span) / span), 2)
            lines.append(WageLine(
                wage_run_id=run.id, employee_id=emp.id,
                wage_type=WageType.MONTHLY, pieces=0, amount=amount,
            ))

        await self.repo.add_lines(lines)
        await self.repo.persist_breakdown(run.id, breakdown)   # see repository
        await self.repo.close_run(run)

        unrated_out = await self._name_unrated(unrated)
        detail_lines = await self.repo.run_lines_detailed(run.id)

        return {
            "id": run.id,
            "period_start": period_start,
            "period_end": period_end,
            "status": RunStatus.CLOSED,
            "total_amount": round(sum(float(ln.amount) for ln in lines), 2),
            "total_pieces": sum(int(ln.pieces) for ln in lines),
            "employee_count": len(lines),
            "unrated_operations": unrated_out,
            # H13: every silent exclusion is now visible on the run.
            "excluded_untyped_employees": [
                {"employee_id": str(e.id), "name": e.name,
                 "wage_type": e.wage_type}
                for e in untyped
            ],
            "lines": detail_lines,
            "gap_days": gap_days,
        }

    async def _name_unrated(self, unrated: dict[tuple, int]) -> list[dict]:
        """Turn {(style_id, op_id): qty} into codes the frontend can link on.

        Raw UUIDs here would make the warning unactionable — the manager would see
        that 340 pieces went unpaid and have no way to reach the rate sheet that
        fixes it.

        H12: the dict may also carry (_MONTHLY_SALARY_MISSING, employee_id) keys
        from the monthly branch. Those are split out into a separate shape rather
        than resolved as a (style, op) pair, which would 404.
        """
        if not unrated:
            return []

        # H12: peel off the monthly-salary-missing sentinels first.
        salary_missing = {
            emp_id for (marker, emp_id), _ in unrated.items()
            if marker == _MONTHLY_SALARY_MISSING
        }
        piece_unrated = {
            k: v for k, v in unrated.items() if k[0] != _MONTHLY_SALARY_MISSING
        }

        out: list[dict] = []

        if piece_unrated:
            style_ids = list({s for s, _ in piece_unrated})
            styles = await self.clients.get_style_codes(style_ids)
            ops, _ = await self._resolve_operations()
            op_by_id = {o.id: o for o in ops}
            for (style_id, op_id), qty in piece_unrated.items():
                st = styles.get(style_id) or {}
                op = op_by_id.get(op_id)
                out.append(
                    {
                        "kind": "unrated_operation",
                        "style_code": st.get("style_code") or str(style_id),
                        "style_name": st.get("style_name") or "(unknown style)",
                        "operation_code": op.code if op else str(op_id),
                        "unpaid_pieces": qty,
                    }
                )
            out.sort(key=lambda r: -r["unpaid_pieces"])

        if salary_missing:
            names = await self.employees.names_for(list(salary_missing))
            for emp_id in salary_missing:
                out.append(
                    {
                        "kind": "monthly_salary_missing",
                        "employee_id": str(emp_id),
                        "employee_name": names.get(emp_id) or "(unknown employee)",
                        "unpaid_pieces": 0,
                    }
                )

        return out

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return await self.repo.list_runs(limit, offset)

    async def get_run_detail(self, run_id: uuid.UUID) -> dict:
        """Re-read a frozen run — payslip detail, no recomputation.

        Lines now carry the per-(style, operation) breakdown: style_code,
        style_name, operation_code, pieces, rate, amount. That is what makes a
        payslip auditable by the worker holding it: 'CUTTING on JP-CLERMONT_VEST,
        120 pieces at 12.50 = 1500' rather than a single unexplained total.
        """
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
            "recomputed": run.recompute_count > 0,
            "recompute_count": run.recompute_count,
            "last_recomputed_at": run.last_recomputed_at,
            "last_recomputed_by": run.last_recomputed_by,
            "lines": lines,
        }