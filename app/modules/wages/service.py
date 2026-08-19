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
# Alias for the handful of scopes where a `status` PARAMETER shadows the
# module — list_runs filters by run status and needs both names at once.
from fastapi import status as _http
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunStatus, WageRunKind, WageType
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
    async def _scope_style_ids(self, *, order_number: str | None,
                               style_code: str | None) -> set | None:
        """The concrete set of styles a run pays. `None` means EVERY style.

        This is what makes the overlap guard correct. A run's scope is stored as
        a human code, but two runs collide on PIECES, not on strings:

            unscoped        -> None          (all styles, present and future)
            style_code A    -> {id(A)}
            order_number X  -> {every style id in X}

        A style belongs to exactly one order, so this mapping is total and
        unambiguous — which is precisely what the old string comparison could not
        express, and why it had to fall back to a conservative
        "any order-scoped run blocks any style-scoped run".
        """
        if style_code:
            return {(await self._resolve_style(style_code))["style_id"]}
        if order_number:
            order = await self.clients.get_order_by_number(order_number.strip())
            if not order:
                return set()          # unknown order pays nothing
            return set(await self.clients.style_ids_for_order(order.id))
        return None

    @staticmethod
    def _scopes_collide(a: set | None, b: set | None) -> bool:
        """Do two runs pay any style in common? `None` = the whole factory."""
        if a is None or b is None:
            return True               # an unscoped run pays everything
        return bool(a & b)

    @staticmethod
    def _kinds_collide(mine: str, theirs: str) -> bool:
        """Can two runs of these kinds pay the same money twice?

        THE FORK IS THE POPULATION, so two runs can only double-pay if their
        employee populations overlap:

            PIECE    vs PIECE     - yes, if their STYLES also intersect.
            MONTHLY  vs MONTHLY   - ALWAYS, on dates alone. A monthly run's
                                    order/style is a LABEL (scope_is_label), so
                                    two monthly runs over one fortnight pay the
                                    same salaried people twice no matter what
                                    each one is labelled with. Comparing their
                                    labels would let "August - CLERMONT" and
                                    "August - CARNABY" both pay every salary.
            PIECE    vs MONTHLY   - NEVER. Disjoint populations: the piece branch
                                    skips anyone not PIECE_RATE and the monthly
                                    branch skips anyone not MONTHLY. That is what
                                    lets the two runs coexist in one window,
                                    which is the whole point of the fork.
            COMBINED vs anything  - yes (subject to styles). A legacy combined run
                                    really did pay both populations.
        """
        C = WageRunKind.COMBINED.value
        if mine == C or theirs == C:
            return True
        return mine == theirs

    async def _validate_window(self, period_start: date, period_end: date,
                               *, replacing: uuid.UUID | None = None,
                               scope_order_number: str | None = None,
                               scope_style_code: str | None = None,
                               run_kind: str = WageRunKind.COMBINED.value) -> int:
        """Guards for a hand-typed window. Returns gap_days.

        `replacing` is the run being recomputed — its own window must not count
        as an overlap with itself, or recompute would always 409.

        THE OVERLAP GUARD IS ABOUT PIECES, NOT DATES.
            The rule a factory actually needs is "no garment is paid twice", and
            two runs can only do that if they share BOTH a date window AND a
            style. Dates alone are not a conflict: computing order KJ2451 and
            order KJ2452 for the same fortnight pays two disjoint sets of
            garments, and refusing the second is refusing correct work.

            This used to compare the runs' scope STRINGS, which could not tell
            whether a style belonged to an order and so blocked conservatively:
            ANY order-scoped run in the window killed ANY style-scoped run, even
            for a style in a different order. Now each scope is resolved to its
            real style-id set (`_scope_style_ids`) and the guard fires only on a
            genuine intersection.

            WHAT STILL BLOCKS, AND WHY IT MUST:
                unscoped vs anything  — an unscoped run already paid every style
                                        in the window, so a scoped re-run over
                                        those same days pays those pieces again.
                order X vs order X    — same styles.
                order X vs style A    — only when A actually belongs to X.
            WHAT NO LONGER BLOCKS:
                order X vs order Y, style A vs style B, order X vs style B.
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

        monthly = run_kind == WageRunKind.MONTHLY.value
        # A MONTHLY run's order/style is decoration. Resolving it to a style-id
        # set here would make the guard believe the run only pays that style's
        # people, and two monthly runs labelled with different styles would then
        # both pay every salary in the window.
        mine = None if monthly else await self._scope_style_ids(
            order_number=scope_order_number, style_code=scope_style_code)

        candidates = await self.repo.runs_in_window(
            period_start, period_end, exclude_run_id=replacing)
        for clash in candidates:
            clash_kind = (getattr(clash, "run_kind", None)
                          or WageRunKind.COMBINED.value)
            if not self._kinds_collide(run_kind, clash_kind):
                continue
            theirs = None if (getattr(clash, "scope_is_label", False)
                              or clash_kind == WageRunKind.MONTHLY.value) else (
                await self._scope_style_ids(
                    order_number=clash.scope_order_number,
                    style_code=clash.scope_style_code))
            if not self._scopes_collide(mine, theirs):
                continue
            state = ("closed" if clash.status == RunStatus.CLOSED
                     else "OPEN (a draft, in progress, or abandoned mid-compute)")
            clash_scope = (clash.scope_style_code or clash.scope_order_number
                           or "the whole factory")
            mine_scope = (scope_style_code or scope_order_number
                          or "the whole factory")
            if monthly and clash_kind == WageRunKind.MONTHLY.value:
                # Say plainly that the label did not narrow anything, or a
                # manager who just typed a different style into a monthly run
                # reads this 409 as a bug.
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"A MONTHLY run already covers {clash.period_start}.."
                    f"{clash.period_end} ({state} run {clash.id}), so this one "
                    f"would pay the same salaries twice. A monthly run's "
                    f"order/style is only a LABEL for the payroll screen — it "
                    f"does not narrow who is paid, so labelling this run with a "
                    f"different style does not make it a different payroll. "
                    f"Recompute run {clash.id} instead (POST /wages/runs/"
                    f"{clash.id}/recompute), move the window, or delete that run "
                    f"(DELETE /wages/runs/{clash.id}) if it is wreckage from a "
                    f"failed compute.")
            # Name the overlap in the terms the manager typed, and say what to do.
            shared = "every style" if (mine is None or theirs is None) else (
                f"{len(mine & theirs)} shared style(s)")
            kind_note = ("" if clash_kind != WageRunKind.COMBINED.value else
                         " That run pre-dates the piece/monthly split, so it paid "
                         "BOTH piece-rate and salaried staff for the whole "
                         "factory — which is why a narrower run still collides "
                         "with it.")
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"This {run_kind.upper()} run ({mine_scope}) would pay work "
                f"already covered by {state} {clash_kind.upper()} run {clash.id} "
                f"({clash.period_start}..{clash.period_end}, scope: {clash_scope}) "
                f"— {shared} in common over the same dates, so those garments "
                f"would be paid twice.{kind_note} Runs for DIFFERENT orders or "
                f"styles over the same dates are allowed, and a PIECE run never "
                f"collides with a MONTHLY one. Narrow this run, move the window, "
                f"recompute run {clash.id} (POST /wages/runs/{clash.id}/recompute), "
                f"or delete it (DELETE /wages/runs/{clash.id}) if it is wreckage "
                f"from a failed compute.",
            )

        last = await self.repo.last_closed_run(exclude_run_id=replacing)
        if last and period_start > last.period_end:
            return max(0, (period_start - last.period_end).days - 1)
        return 0

    # ══════════════════════════════════════════════════════════════════════
    # THE FREEZE CONTRACT — draft, freeze, reopen  (change-list item 3)
    # ══════════════════════════════════════════════════════════════════════
    # THE CONFLICT THIS RESOLVES
    #     The change list asks to "re-compute the frozen payroll and update it".
    #     The standing guardrail says a CLOSED run is a frozen snapshot and is
    #     never recomputed. Both are right, and the resolution is that they were
    #     talking about two different documents:
    #
    #       OPEN   = a DRAFT. Recompute it as often as you like — nothing has
    #                been paid against it, so there is nothing to protect.
    #       CLOSED = the document the cash was counted against. Rewriting it
    #                takes an explicit REOPEN, which is audited and stamped.
    #
    #     So: POST /wages/runs computes a DRAFT by default. Freeze it when the
    #     numbers are agreed. To change a frozen run, reopen → recompute →
    #     re-freeze, three deliberate steps, each on the record.
    #
    #     THE LEDGER ALWAYS READS THE FROZEN ROWS, never a live recompute.

    async def reopen_run(self, run_id: uuid.UUID, *, user_name: str,
                         reason: str) -> dict:
        """Unfreeze a CLOSED run so it can be recomputed. DM/MD only, audited.

        This is the ONLY door out of CLOSED, and it is deliberately a separate
        call from the recompute rather than a flag on it. A manager who has to
        press "reopen", type why, and then press "recompute" cannot rewrite a
        paid payslip by mistyping a run id.

        WHAT IT CANNOT PROTECT YOU FROM: cash already disbursed. Reopening on
        Monday changes the record, not the payment — which is exactly why the
        reason is stored and shown on every reprint of that payslip.
        """
        run = await self.repo.get_run(run_id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wage run not found")
        if run.status != RunStatus.CLOSED:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Run {run.id} is already {run.status.value} — only a CLOSED run "
                f"needs reopening.")
        reason = (reason or "").strip()
        if len(reason) < 5:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Give a reason for reopening a paid payroll run — it is printed "
                "on every reissued payslip for this period.")

        await self.repo.stamp_reopen(run, by=user_name, reason=reason)
        return {
            "id": run.id, "status": run.status,
            "period_start": run.period_start, "period_end": run.period_end,
            "reopen_count": run.reopen_count,
            "last_reopened_by": run.last_reopened_by,
            "last_reopen_reason": run.last_reopen_reason,
            "message": (f"Run {run.id} is OPEN again. Recompute it, then close it "
                        f"— it stays unfrozen until you do."),
        }

    async def close_run(self, run_id: uuid.UUID, *, user_name: str) -> dict:
        """FREEZE a draft run. From here it is the document of record."""
        run = await self.repo.get_run(run_id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wage run not found")
        if run.status == RunStatus.CLOSED:
            # Idempotent: closing a closed run is what a double-click does, and
            # it changed nothing, so it is not an error.
            return await self.get_run_detail(run_id)
        if not (run.lines or []):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Run {run.id} has no wage lines — freezing an empty run would "
                f"lock a window in which nobody is paid. Recompute it first.")
        await self.repo.close_run(run)
        return await self.get_run_detail(run_id)

    async def recompute_run(self, run_id: uuid.UUID, *, user_name: str,
                            confirm_closed: bool = False) -> dict:
        """Re-run payroll for an EXISTING run's window, discarding its old lines.

        FREE ON A DRAFT (OPEN). On a CLOSED run it refuses and points at
        `reopen_run` — see the freeze-contract note above. `confirm_closed=True`
        remains as the one-call escape hatch for a caller that genuinely means it
        (it still stamps the recompute), but the reopen door is the intended
        route because it captures a REASON and the escape hatch does not.

          • it targets one named run_id — never a side effect
          • the run's own window is excluded from the overlap check, so it
            cannot smuggle in a second payment for a different period
          • the run's stored SCOPE is reused, so a recompute cannot silently
            widen a one-style run into a whole-factory one
          • the old lines are SNAPSHOT, then deleted, then RESTORED if the
            repopulate fails (B6) — a crashed recompute can no longer leave a
            CLOSED run with zero lines for a fortnight already in envelopes
          • recompute_count / last_recomputed_at / last_recomputed_by are
            stamped, so a payslip reprinted after a recompute is visibly a
            different document from the one paid against
          • it is DM/MD only (see router)
        """
        run = await self.repo.get_run(run_id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wage run not found")

        # B6: a frozen run is frozen until someone explicitly unfreezes it.
        if run.status == RunStatus.CLOSED and not confirm_closed:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Run {run.id} is CLOSED ({run.period_start}..{run.period_end}) and "
                f"was already paid against. Reopen it first — "
                f"POST /wages/runs/{run.id}/reopen with a reason — then recompute "
                f"and close it again. (A caller that genuinely wants to skip that "
                f"may send confirm_closed=true, but no reason is recorded.)")

        # THE KIND IS READ OFF THE RUN, never re-supplied by the caller — for the
        # same reason the scope is. A recompute that could change a PIECE run into
        # a MONTHLY one would rewrite who was paid, under a run id somebody has
        # already printed payslips against.
        gap_days = await self._validate_window(
            run.period_start, run.period_end, replacing=run.id,
            run_kind=getattr(run, "run_kind", None) or WageRunKind.COMBINED.value,
            scope_order_number=run.scope_order_number,
            scope_style_code=run.scope_style_code,
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
                run, run.period_start, run.period_end, gap_days=gap_days,
                # A recompute re-freezes a run that was already CLOSED, and
                # leaves a draft a draft. Anything else would either silently
                # freeze a draft the manager was still editing, or quietly leave
                # a paid period unfrozen.
                freeze=(run.status == RunStatus.CLOSED),
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

    async def compute_run(self, period_start: date, period_end: date, *,
                          freeze: bool = True,
                          run_kind: str = WageRunKind.PIECE.value,
                          order_number: str | None = None,
                          style_code: str | None = None) -> dict:
        """Compute one payroll for a hand-entered window.

        `run_kind` IS NOW THE FIRST QUESTION, and it is not a filter — it decides
        which of two incompatible pricing rules applies (see WageRunKind):

          PIECE    pays PIECE_RATE workers and REQUIRES an order or a style. A
                   piece wage is earned on a specific garment, so naming that
                   garment's style is the definition of what is being paid, not a
                   convenience. Two dates alone used to be enough to compute here,
                   and that is exactly how a manager pays the whole factory's
                   piece work under a window they meant to narrow.

          MONTHLY  pays salaried staff and takes no paying scope. It MAY carry an
                   order_number / style_code, stored with `scope_is_label=True`
                   and shown on the payroll screen so a manager can see at a
                   glance which order the month was spent on — but it changes
                   NOTHING about who is paid or how much. A salary is a fact
                   about a person and a period; splitting it across styles has no
                   honest arithmetic behind it.

        A PIECE run and a MONTHLY run over the SAME window do not collide, by
        design: their employee populations are disjoint, so the pair of them is
        one complete payroll for that fortnight.

        `freeze=False` leaves the run OPEN — a DRAFT the manager can recompute
        freely and then close when the numbers are agreed.
        """
        kind = (run_kind or WageRunKind.PIECE.value).strip().lower()
        if kind not in {k.value for k in WageRunKind}:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"run_kind must be 'piece' or 'monthly', not {run_kind!r}.")
        if kind == WageRunKind.COMBINED.value:
            # COMBINED is a description of history, not a thing you can ask for.
            # Creating one would re-open the exact hole the fork closes: an
            # unscoped run that pays every piece of every style in the window.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "run_kind 'combined' is a legacy marker on runs computed before "
                "piece and monthly payroll were split. It cannot be created. "
                "Compute a PIECE run (naming an order or style) and a MONTHLY "
                "run for the same window instead — together they pay everybody, "
                "and neither can pay anyone twice.")

        is_monthly = kind == WageRunKind.MONTHLY.value

        # Both codes are validated on BOTH kinds. A label that names a style
        # nobody can find is worse than no label: it prints on the payroll screen
        # looking authoritative.
        style = await self._resolve_style(style_code) if style_code else None
        order_id = None
        if order_number:
            order = await self.clients.get_order_by_number(order_number.strip())
            if not order:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    f"No order '{order_number}'")
            order_id = order.id

        # THE RESTRICTION. Dates alone no longer compute a piece-rate payroll.
        if not is_monthly and not (style or order_id):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A PIECE run must name the work it is paying for: send "
                "`style_code` or `order_number`. Piece wages are earned on "
                "specific garments, so a window with no style behind it pays "
                "every piece of every order in those dates — which is how the "
                "same garment ends up on two payslips. If you meant to pay "
                "salaries, send run_kind='monthly' (no scope needed; an "
                "order/style there is just a label for the screen).")

        gap_days = await self._validate_window(
            period_start, period_end, run_kind=kind,
            scope_order_number=order_number, scope_style_code=style_code)
        run = await self.repo.create_run(
            period_start, period_end,
            run_kind=kind,
            scope_is_label=is_monthly,
            scope_order_number=(order_number or None),
            scope_style_code=(style["style_code"] if style else None))
        try:
            payload = await self._populate_run(
                run, period_start, period_end, gap_days=gap_days, freeze=freeze,
                # A MONTHLY run passes NO style/order filter downstream. Its codes
                # were stored for display and must not reach the piece query, or
                # the label would quietly become a filter after all.
                style_ids=(None if is_monthly
                           else ([style["style_id"]] if style else None)),
                order_id=(None if is_monthly else order_id),
            )
        except Exception:
            await self.repo.delete_run(run)
            raise
        payload["recomputed"] = False
        payload["recompute_count"] = 0
        return payload

    async def delete_run(self, run_id: uuid.UUID, *, user_name: str,
                         confirm_closed: bool = False) -> dict:
        """Delete a run outright. THE ESCAPE HATCH THE 409 HAS ALWAYS NAMED.

        The overlap guard tells a blocked manager to "delete that run if it is
        wreckage from a failed compute", and until now there was no route that
        could: `delete_run` existed on the repository but was only reachable from
        compute_run's own failure path. A manager whose compute died halfway
        therefore had a committed OPEN run permanently occupying the window, no
        way to remove it, and a 409 pointing at an action the API did not offer.

        REFUSES A CLOSED RUN by default. That run is the document the cash was
        counted against; deleting it destroys the only record of a payment that
        actually happened. `confirm_closed=True` is the deliberate override and
        it is DM/MD-gated at the router, for the rare genuine case (a run frozen
        against the wrong fortnight, before anyone was paid from it).

        The lines and the frozen breakdown rows go with it — WageRun.lines
        cascades delete-orphan, and the detail rows are cleared explicitly
        because they hang off the run id rather than off the relationship.
        """
        run = await self.repo.get_run(run_id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wage run not found")
        if run.status == RunStatus.CLOSED and not confirm_closed:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Run {run.id} is CLOSED ({run.period_start}..{run.period_end}) "
                f"and is the document its payments were counted against — "
                f"deleting it destroys the record of money that already left the "
                f"building. If you are certain nobody was paid from it, resend "
                f"with confirm_closed=true. To CHANGE a frozen run instead, "
                f"reopen it (POST /wages/runs/{run.id}/reopen) and recompute.")
        snapshot = {
            "id": run.id,
            "period_start": run.period_start,
            "period_end": run.period_end,
            "status": run.status,
            "run_kind": getattr(run, "run_kind", None),
            "scope_order_number": run.scope_order_number,
            "scope_style_code": run.scope_style_code,
            "lines_deleted": len(run.lines or []),
        }
        await self.repo.purge_run(run)
        snapshot["deleted"] = True
        snapshot["message"] = (
            f"Run {snapshot['id']} deleted. The window "
            f"{snapshot['period_start']}..{snapshot['period_end']} is free to "
            f"compute again.")
        return snapshot

    async def _populate_run(self, run, period_start: date, period_end: date,
                            *, gap_days: int, freeze: bool = True,
                            style_ids=None, order_id=None) -> dict:
        """Shared body of compute_run and recompute_run.

        Extracted so recompute cannot drift from compute — two copies of payroll
        arithmetic is how a factory ends up with two different answers for the
        same fortnight depending on which button was pressed.
        """
        kind = getattr(run, "run_kind", None) or WageRunKind.COMBINED.value
        is_monthly_run = kind == WageRunKind.MONTHLY.value
        is_piece_run = kind == WageRunKind.PIECE.value

        # A recompute passes no scope: it reads it back off the run, which is
        # what makes a recompute reproduce the SAME payment rather than silently
        # widening a one-style run into a whole-factory one.
        #
        # EXCEPT ON A MONTHLY RUN, where those codes are a LABEL. Reading them
        # back as a filter is the one way this fork could quietly break: the
        # compute path is careful to pass None, and a recompute that re-derived
        # them from the row would price the second run differently from the
        # first for the same stored scope.
        if not is_monthly_run:
            if style_ids is None and run.scope_style_code:
                style_ids = [
                    (await self._resolve_style(run.scope_style_code))["style_id"]]
            if order_id is None and run.scope_order_number:
                order = await self.clients.get_order_by_number(run.scope_order_number)
                order_id = order.id if order else None
        else:
            style_ids, order_id = None, None
        scoped = bool(style_ids or order_id)
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
        # SKIPPED ENTIRELY ON A MONTHLY RUN. Not filtered to zero rows - skipped,
        # so the query never runs. A monthly run pays salaries; if it also picked
        # up piece work it would pay a piece-rate cutter on the same window a
        # PIECE run pays them, which is the double payment the fork exists to
        # make impossible.
        rate_cache: dict[tuple[uuid.UUID, uuid.UUID, date], float | None] = {}
        rows = [] if is_monthly_run else await self.production.piece_counts(
            period_start, period_end, style_ids=style_ids, order_id=order_id)
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
        # WHO RUNS THIS BRANCH:
        #   MONTHLY   yes - this is its whole job.
        #   PIECE     no. A monthly salary is a fact about a PERSON, not a style:
        #             there is no honest way to say what share of a fitter's
        #             salary belongs to CLERMONT rather than CARNABY. A full
        #             monthly line on a one-style run would be paid again by every
        #             other style run in the same window.
        #   COMBINED  yes when unscoped - the legacy behaviour, preserved so a
        #             recompute of an old run reproduces what it originally paid.
        #
        # The old test was `not scoped`, which read the SCOPE to answer a question
        # about the POPULATION. That worked only because scope and population
        # happened to line up; it is why a monthly run could not carry a style
        # label at all, since the label would have switched the salaries off.
        pays_monthly = is_monthly_run or (kind == WageRunKind.COMBINED.value
                                          and not scoped)
        for emp in (employees if pays_monthly else []):
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

        # FREEZE THE WARNING WITH THE RUN. `unrated_operations` used to live only
        # in the HTTP response of the compute call: every later read hardcoded
        # [], so a manager who reopened the payslip screen saw an empty list and
        # no way to tell it apart from "everything was rated". It is a property
        # of the run, so it is stored on the run.
        unrated_out = await self._name_unrated(unrated)
        await self.repo.persist_unrated(run, unrated_out)

        if freeze:
            await self.repo.close_run(run)

        detail_lines = await self.repo.run_lines_detailed(run.id)

        return {
            "id": run.id,
            "period_start": period_start,
            "period_end": period_end,
            "status": RunStatus.CLOSED if freeze else RunStatus.OPEN,
            "run_kind": kind,
            "scope_order_number": run.scope_order_number,
            "scope_style_code": run.scope_style_code,
            # On a MONTHLY run the codes above are DECORATION. The screen must be
            # able to tell them apart from a real narrowing, or it will print
            # "CLERMONT payroll" over a sheet that paid every salaried person in
            # the building.
            "scope_is_label": bool(getattr(run, "scope_is_label", False)),
            # True when this run pays piece work only, so the screen says
            # "piece-rate only" rather than letting a manager read a one-style
            # run as a full payroll.
            "piece_rate_only": bool(is_piece_run or (scoped and not pays_monthly)),
            # The mirror image: a salaries-only sheet with no piece work on it.
            "monthly_only": bool(is_monthly_run),
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

    async def list_runs(self, *, limit: int = 50, offset: int = 0,
                        run_kind: str | None = None,
                        order_number: str | None = None,
                        style_code: str | None = None,
                        date_from: date | None = None,
                        date_to: date | None = None,
                        status: str | None = None) -> list[dict]:
        """THE RUN LIST — every run with the order/style it paid for, filterable.

        This is the route back to a run_id. A manager who wants to recompute
        last fortnight's CLERMONT piece payroll filters by style and window here,
        reads the id off the row, and posts it to /recompute. Before, the list
        returned neither the style nor the order on any row (the query never
        selected them), so the id was unreachable without opening runs one by
        one."""
        if status:
            valid = {r.value for r in RunStatus}
            if status.strip().lower() not in valid:
                raise HTTPException(
                    _http.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"status must be one of {sorted(valid)}.")
        if run_kind:
            valid_k = {k.value for k in WageRunKind}
            if run_kind.strip().lower() not in valid_k:
                raise HTTPException(
                    _http.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"run_kind must be one of {sorted(valid_k)}.")
        if date_from and date_to and date_to < date_from:
            raise HTTPException(
                _http.HTTP_422_UNPROCESSABLE_ENTITY,
                "date_to is before date_from.")
        return await self.repo.list_runs(
            limit=limit, offset=offset, run_kind=run_kind,
            order_number=order_number, style_code=style_code,
            date_from=date_from, date_to=date_to, status=status)

    # ══════════════════════════════════════════════════════════════════════
    # THE PAYROLL SCREENS (change-list item 3)
    # ══════════════════════════════════════════════════════════════════════
    async def list_orders(self, *, on: date | None = None,
                          unpriced_only: bool = False) -> list[dict]:
        """ORDER CARDS for the payroll landing screen.

        The screen is order → style → rate sheet. It was style-first, which put
        223 style cards from every order on one page with no way to tell which
        order a card belonged to. `styles_priced / styles` is the card's badge —
        the same "n of m priced" idea as the style card, one level up, so an
        order with unpriced styles is visible before a run silently pays zero for
        them.
        """
        on = on or date.today()
        styles = await self.list_styles(on=on)
        by_order: dict[str, dict] = {}
        for s in styles:
            card = by_order.setdefault(s["order_number"], {
                "order_number": s["order_number"], "styles": 0,
                "styles_priced": 0, "qty_ordered": 0, "sku_count": 0,
                "style_codes": [],
            })
            card["styles"] += 1
            card["styles_priced"] += 1 if s["fully_rated"] else 0
            card["qty_ordered"] += int(s["qty_ordered"] or 0)
            card["sku_count"] += int(s["sku_count"] or 0)
            card["style_codes"].append(s["style_code"])
        out = []
        for card in by_order.values():
            card["fully_priced"] = card["styles_priced"] >= card["styles"]
            if unpriced_only and card["fully_priced"]:
                continue
            out.append(card)
        return sorted(out, key=lambda c: c["order_number"])

    async def run_breakdown(self, run_id: uuid.UUID) -> dict:
        """A frozen run, folded three ways: per style, per stage, per employee.

        ALL THREE ARE FOLDS OF THE SAME ROW SET (wage_line_detail), computed
        here rather than by three separate queries, so the totals on the three
        tabs of the payroll screen cannot disagree with each other or with the
        run total. That disagreement is the classic payroll-screen bug and it is
        purely an artefact of asking the database the same question three ways.
        """
        run = await self.repo.get_run(run_id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wage run not found")
        rows = await self.repo.run_breakdown(run_id)

        by_style: dict[str, dict] = {}
        by_stage: dict[str, dict] = {}
        by_employee: dict[uuid.UUID, dict] = {}

        for r in rows:
            st = by_style.setdefault(r["style_code"] or str(r["style_id"]), {
                "style_code": r["style_code"], "style_name": r["style_name"],
                "pieces": 0, "amount": 0.0, "stages": {}, "employees": {},
            })
            st["pieces"] += r["pieces"]
            st["amount"] = round(st["amount"] + r["amount"], 2)

            # per stage WITHIN the style — the change list's "per style total
            # amount, per stage how much amount".
            stg = st["stages"].setdefault(r["operation_code"], {
                "operation_code": r["operation_code"],
                "operation_label": r["operation_label"],
                "sequence": r["sequence"], "pieces": 0, "amount": 0.0,
                "rate": r["rate"],
            })
            stg["pieces"] += r["pieces"]
            stg["amount"] = round(stg["amount"] + r["amount"], 2)

            emp_in_style = st["employees"].setdefault(str(r["employee_id"]), {
                "employee_id": r["employee_id"], "employee_name": r["employee_name"],
                "designation": r["designation"], "pieces": 0, "amount": 0.0,
            })
            emp_in_style["pieces"] += r["pieces"]
            emp_in_style["amount"] = round(emp_in_style["amount"] + r["amount"], 2)

            # factory-wide folds
            fs = by_stage.setdefault(r["operation_code"], {
                "operation_code": r["operation_code"],
                "operation_label": r["operation_label"],
                "sequence": r["sequence"], "pieces": 0, "amount": 0.0,
            })
            fs["pieces"] += r["pieces"]
            fs["amount"] = round(fs["amount"] + r["amount"], 2)

            fe = by_employee.setdefault(r["employee_id"], {
                "employee_id": r["employee_id"], "employee_name": r["employee_name"],
                "designation": r["designation"], "pieces": 0, "amount": 0.0,
                "styles": [],
            })
            fe["pieces"] += r["pieces"]
            fe["amount"] = round(fe["amount"] + r["amount"], 2)
            if r["style_code"] not in fe["styles"]:
                fe["styles"].append(r["style_code"])

        styles_out = []
        for st in by_style.values():
            st["stages"] = sorted(st["stages"].values(), key=lambda x: x["sequence"])
            st["employees"] = sorted(st["employees"].values(),
                                     key=lambda x: -x["amount"])
            styles_out.append(st)

        return {
            "run_id": run.id,
            "period_start": run.period_start, "period_end": run.period_end,
            "status": run.status,
            "scope_order_number": run.scope_order_number,
            "scope_style_code": run.scope_style_code,
            "computed_at": run.created_at,
            "recompute_count": run.recompute_count or 0,
            "reopen_count": run.reopen_count or 0,
            "total_amount": round(sum(s["amount"] for s in styles_out), 2),
            "total_pieces": sum(s["pieces"] for s in styles_out),
            "by_style": sorted(styles_out, key=lambda s: -s["amount"]),
            "by_stage": sorted(by_stage.values(), key=lambda s: s["sequence"]),
            "by_employee": sorted(by_employee.values(), key=lambda e: -e["amount"]),
        }

    async def run_pieces(self, run_id: uuid.UUID, *, style_code: str | None = None,
                         limit: int = 500, offset: int = 0) -> dict:
        """PER-PIECE payroll detail for a frozen run — garment, stage, worker,
        their barcode, and what that piece paid. Frozen rates, never re-priced."""
        run = await self.repo.get_run(run_id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wage run not found")
        out = await self.repo.run_piece_rows(
            run_id, run, style_code=style_code, limit=limit, offset=offset)
        out["run_id"] = run.id
        out["status"] = run.status
        return out

    async def ledger(self, **filters) -> dict:
        """Every computed run, latest first, searchable by order / style / date."""
        return await self.repo.ledger(**filters)

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
        kind = getattr(run, "run_kind", None) or WageRunKind.COMBINED.value
        scope_is_label = bool(getattr(run, "scope_is_label", False))
        return {
            "id": run.id,
            "period_start": run.period_start,
            "period_end": run.period_end,
            "status": run.status,
            "run_kind": kind,
            "scope_order_number": run.scope_order_number,
            "scope_style_code": run.scope_style_code,
            "scope_is_label": scope_is_label,
            # A MONTHLY run's codes are a label, so it is NOT "piece rate only"
            # just because it names a style — it is the opposite. Deriving this
            # from the presence of a scope, as it used to, now gets monthly runs
            # exactly backwards.
            "piece_rate_only": kind == WageRunKind.PIECE.value or (
                kind == WageRunKind.COMBINED.value
                and bool(run.scope_order_number or run.scope_style_code)),
            "monthly_only": kind == WageRunKind.MONTHLY.value,
            "total_amount": round(sum(ln["amount"] for ln in lines), 2),
            "total_pieces": sum(ln["pieces"] for ln in lines),
            "employee_count": len(lines),
            # THE FROZEN WARNING, not a hardcoded []. See _populate_run: work
            # that went unpaid because its style/operation had no rate is a
            # property of the run and must survive being re-read.
            "unrated_operations": list(getattr(run, "unrated_snapshot", None) or []),
            "gap_days": 0,
            "recomputed": (run.recompute_count or 0) > 0,
            "recompute_count": run.recompute_count or 0,
            "last_recomputed_at": run.last_recomputed_at,
            "last_recomputed_by": run.last_recomputed_by,
            # The unfreeze trail. A payslip reprinted from a run that has been
            # unfrozen after payment must carry that fact and its reason.
            "reopen_count": run.reopen_count or 0,
            "last_reopened_at": run.last_reopened_at,
            "last_reopened_by": run.last_reopened_by,
            "last_reopen_reason": run.last_reopen_reason,
            "lines": lines,
        }