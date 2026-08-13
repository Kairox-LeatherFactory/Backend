"""
================================================================================
modules/production/service.py — Production logic, the two-door log, and 4 gates
================================================================================

ONE LOG, TWO DOORS, NO STAGE BUTTONS.
    log_batch() is the whole floor's logging surface. The caller sends an ACTOR
    (employee — by barcode or id) and TARGETS (pieces — by barcode or sku+seqs),
    plus consumption at cutting. The caller NEVER sends a stage:

      • a cut SCREEN (LEATHER_CUT / LINING_CUT) fixes the cut stage
      • otherwise the stage is INFERRED from each piece's own history (the next
        stage on the leather chain after the last one it completed)

    Barcode door and manual door POST the SAME shape and converge here — the
    router resolves barcodes to ids before calling, so this service sees ids only.

FOUR GATES, cheapest / most-likely-to-fail first:
    1. ROLE      — may this manager's role log this stage? (403, whole request)
    2. SKILL     — may this employee's designation work this stage? (per-piece
                   warning: skill_blocked; whole batch when the actor is wrong)
    3. SEQUENCE  — has this piece completed the previous leather-chain stage?
                   (per-piece: sequence_blocked)
    4. MERGE     — for LINE_STITCHING only: is the piece's drawer SENDED (leather
                   + lining merged)? (per-piece: merge_blocked)

    Gate 1 is a 403 because it is not per-piece — the role is wrong for the whole
    batch. Gates 2–4 are per-piece warnings so one bad piece never loses the 39
    good ones a manager scanned with it. Mirrors the existing not_found handling.

CUT NO LONGER MINTS.
    Pieces are minted at breakdown upload (imports/premint.py). cut() is gone;
    the two cut stages are logged through log_batch like every other stage, with
    the one difference that they capture consumption and decrement stock.

REWORK stays permissive and bypasses the sequence + merge gates: a piece
returning to a stage it already passed is legitimate, flagged, never blocked.
================================================================================
"""
import re
import uuid
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    MERGE_GATE_ENTRY, MULTI_STAGE_DESIGNATIONS, SCREEN_EXPECTED_ROLE,
    SCREEN_TO_STAGE, STAGE_DESIGNATIONS, STAGE_ROLE_ACCESS, Designation,
    DrawerState, ProductionStage, ScreenContext, UserRole, next_chain_stage,
)
from app.core.store_display import display_stage
from app.modules.clients.service import ClientService
from app.modules.employees.service import EmployeeService
from app.modules.production.models import Operation, Piece, ProductionEvent
from app.modules.production.repository import ProductionRepository
from app.modules.users.models import User

# Roles that log ANY stage. MD is the superuser; DM logs freely (spec).
_STAGE_BYPASS_ROLES = frozenset({UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR,})


def _norm(code: str) -> str:
    return (code or "").strip().upper()


def _slug(s: str | None, limit: int = 24) -> str:
    return (re.sub(r"[^A-Za-z0-9]", "", (s or "")).upper() or "NA")[:limit]


class ProductionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ProductionRepository(db)
        self.clients = ClientService(db)
        self.employees = EmployeeService(db)

    async def list_operations(self) -> list[Operation]:
        return await self.repo.list_operations()

    # ══════════════════════════════════════════════════════ stage resolution
    @staticmethod
    def _stage_of(op: Operation) -> ProductionStage | None:
        try:
            return ProductionStage(_norm(op.code))
        except ValueError:
            return None   # legacy/config code — order + skill gates skipped

    async def _infer_stage_for_piece(self, piece: Piece,
                                     screen: ScreenContext) -> ProductionStage | None:
        """The stage this scan logs for this piece.

        Cut screens fix the stage outright. In PIPELINE mode we take the next
        stage on the leather chain after the piece's furthest-completed stage.
        """
        if screen in SCREEN_TO_STAGE:
            return SCREEN_TO_STAGE[screen]
        # PIPELINE: the next chain stage after the furthest one this piece has an
        # event at. The loop itself lives in core.enums_barcode.next_chain_stage
        # because /barcode/resolve now answers the same question on the read side,
        # and two copies of it would eventually disagree.
        done = await self.repo.completed_stage_codes(piece.id)
        return next_chain_stage(done)

    # ══════════════════════════════════════════════════════════ GATE 1: role
    async def _assert_role(self, user: User, stage: ProductionStage | None,
                           op: Operation) -> None:
        if user.role in _STAGE_BYPASS_ROLES:
            return
        if stage is not None:
            allowed = STAGE_ROLE_ACCESS.get(stage, set())
            if user.role in allowed:
                return
            db_allowed = await self.repo.operations_for_role(user.role.value)
            if op.id in db_allowed:
                return
            names = ", ".join(sorted(getattr(r, "value", str(r)) for r in allowed)) \
                or "direct_manager, managing_director"
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"The {stage.value.replace('_', ' ').lower()} stage is logged by its "
                f"own manager ({names}). Your role ({user.role.value}) can't enter "
                f"this log — please ask the {names} to record it, or have DM/MD/HR "
                f"log it. (Employees of any skill may still be assigned here; it's "
                f"the login that's restricted, not the worker.)")
        if op.id not in await self.repo.operations_for_role(user.role.value):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"Your role may not log operation '{op.code}'.")

    # ═════════════════════════════════════════════════════════ GATE 2: skill
    @staticmethod
    def _skill_ok(designation: str | None, stage: ProductionStage | None) -> bool:
        if stage is None:
            return True
        d = Designation.normalise(designation)
        if not d or d in MULTI_STAGE_DESIGNATIONS:
            return True
        if d not in {v for s in STAGE_DESIGNATIONS.values() for v in s}:
            return True   # uncatalogued designation → fail-open (HR backfills)
        return d in STAGE_DESIGNATIONS.get(stage, set())

    @staticmethod
    def _skill_msg(emp_name: str, designation: str | None,
                   stage: ProductionStage) -> str:
        allowed = ", ".join(sorted(STAGE_DESIGNATIONS.get(stage, set()))) or "—"
        return (f"{emp_name} is a {Designation.normalise(designation)} and may not "
                f"work {stage.value}. Allowed: {allowed}.")

    # ══════════════════════════════════════════════════════ GATE 3: sequence
    @staticmethod
    def _cut_screen_hint(stage: ProductionStage) -> str:
        """Which screen logs a cut entry — the actionable half of a 'not cut yet'
        rejection. Without it the floor is told 'blocked' and nothing else."""
        if stage is ProductionStage.LINING_CUTTING:
            return "the LINING_CUT screen (lining manager)"
        return "the LEATHER_CUT screen (cutting manager)"

    async def _sequence_ok(self, piece: Piece,
                           stage: ProductionStage) -> tuple[bool, str | None]:
        prev = stage.predecessor()
        if prev is None:      # cut entries + LINE_STITCHING (merge-governed)
            return True, None
        prev_op = await self.repo.get_operation_by_code(prev.value)
        if prev_op is None:
            return True, None
        if await self.repo.has_event_at_op(piece.id, prev_op.id):
            return True, None
        return False, (f"{piece.code} has not completed {prev.value} — "
                       f"log {prev.value} before {stage.value}.")

    # ═════════════════════════════════════════════════════════ GATE 4: merge
    async def _merge_ok(self, piece: Piece,
                        stage: ProductionStage) -> tuple[bool, str | None]:
        """Only LINE_STITCHING is gated on completeness. The drawer must be SENDED
        (leather + lining both merged and the store released it).

        THE MESSAGE NAMES THE DRAWER. "its drawer must hold leather + lining" told
        an operator holding a garment nothing they could act on — the whole point
        of bug #12 is that the drawer is the thing they cannot see from the
        production screen. The drawer is already loaded here to answer the gate,
        so naming it costs nothing and turns the rejection into an instruction.
        """
        if stage is not MERGE_GATE_ENTRY:
            return True, None
        from app.modules.drawers.service import DrawerService
        drawers = DrawerService(self.db)
        drawer = await drawers.drawer_for_piece(piece.id)
        if drawer is not None and drawer.state == DrawerState.SENDED.value:
            return True, None
        where = f"drawer {drawer.code}" if drawer is not None else "its drawer"
        return False, (
            f"{piece.code} is not ready for {stage.value} — {where} must hold "
            f"leather + lining and be marked SENDED (send it from the Drawers "
            f"List).")

    # ═══════════════════════════════════════════════════════════════ presence
    async def _assert_present(self, employee_id: uuid.UUID, work_date: date) -> None:
        if work_date == date.today():
            from app.modules.attendance.service import AttendanceService
            if not await AttendanceService(self.db).is_present_today(employee_id):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Employee is not checked in today — mark attendance first.")

    # ══════════════════════════════════════════════════════════ THE TWO-DOOR LOG
    async def log_batch(self, *, user: User, employee_id: uuid.UUID,
                        piece_ids: list[uuid.UUID], work_date: date,
                        screen: ScreenContext,
                        leather_lot_id: uuid.UUID | None = None,
                        lining_lot_id: uuid.UUID | None = None,
                        consumption_qty: float | None = None,
                        preview: bool = False) -> dict:      # NEW param
        """Log one stage for a batch of pieces. Stage inferred, never sent.
 
        preview=True → run ALL gates, compute the result buckets, and return them
        WITHOUT writing any ProductionEvent, without decrementing any material lot,
        and without committing. The returned buckets are exactly what a real log
        would produce, so the frontend can show an accurate editable preview."""
        emp = await self.employees.get(employee_id)
        if not emp:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found.")
        await self._assert_present(employee_id, work_date)
 
        if not piece_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No pieces provided.")
 
        pieces: dict = {}
        not_found: list[str] = []
        for pid in piece_ids:
            if pid in pieces:
                continue
            p = await self.db.get(Piece, pid)
            if p is None:
                not_found.append(str(pid))
            else:
                pieces[pid] = p
 
        screen_stage = SCREEN_TO_STAGE.get(screen)

        # Every per-piece rejection is recorded here as {piece, stage, gate,
        # reason}. The flat buckets below stay for back-compat, but they carry a
        # bare code and no cause — which is how a caller ends up staring at
        # `{"stage": null, "sequence_blocked": ["…-001"]}` with nothing to act on.
        blocked: list[dict] = []

        def _block(piece_code: str, gate: str, reason: str,
                   stage: ProductionStage | None = None) -> None:
            blocked.append({
                "piece": piece_code, "gate": gate, "reason": reason,
                "stage": stage.value if stage is not None else None,
            })

        # resolve each piece's stage
        stage_by_piece: dict = {}
        uncut_on_pipeline: list[str] = []
        completed: list[str] = []
        for pid, piece in pieces.items():
            stage = screen_stage or await self._infer_stage_for_piece(piece, screen)
            if stage is None:
                # The piece is past the END of the chain. It is emphatically NOT
                # "not found" — its barcode resolved, its history is complete —
                # so reporting it in not_found was a lie the floor could not
                # interpret. Its own bucket, with the reason attached.
                completed.append(piece.code)
                _block(piece.code, "completed",
                       f"{piece.code} has already completed the final stage "
                       f"({ProductionStage.PACKAGE_EXPORT.value}) — nothing left "
                       f"to log.")
                continue
            # PIPELINE + a piece with no cut event yet: its "next stage" is a cut
            # entry, but cutting is logged on a CUT SCREEN (that is the whole
            # no-stage-buttons rule). Treated as a stage here it would drag a
            # consumption requirement into a pipeline batch and 422 the WHOLE
            # request — one un-cut piece losing every good piece scanned with it,
            # which is exactly what the per-piece gates exist to prevent. So it
            # is blocked PER PIECE, like any other out-of-sequence piece.
            if screen_stage is None and stage.is_cut_entry:
                uncut_on_pipeline.append(piece.code)
                _block(piece.code, "not_cut",
                       f"{piece.code} has not been cut yet, so it has no next "
                       f"pipeline stage. Log {stage.value} first, on "
                       f"{self._cut_screen_hint(stage)} — cutting is never logged "
                       f"from the pipeline screen.", stage)
                continue
            stage_by_piece[pid] = stage

        op_by_stage: dict = {}
        for stage in dict.fromkeys(stage_by_piece.values()):
            op = await self.repo.get_operation_by_code(stage.value)
            if op is None:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    f"Stage '{stage.value}' has no configured operation.")
            op_by_stage[stage] = op

        cut_stages = {s for s in op_by_stage if s.requires_consumption}
        is_cut = bool(cut_stages)
        if is_cut and len(op_by_stage) > 1:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A cutting scan may not be mixed with other stages in one batch.")
 
        # GATE 1 — ROLE. Runs in preview too, so the user sees the same verdict
        # they'd hit on commit.
        #
        # STILL A WHOLE-REQUEST 403 when the role is wrong for EVERY stage in the
        # batch. That is the documented contract (CLAUDE.md §8) and it is right:
        # what's wrong is the ROLE, not any particular piece, so there is nothing
        # to salvage and the caller needs one unambiguous error.
        #
        # BUT that rationale — "the role is wrong for the whole batch" — only
        # holds when the batch HAS one stage. A cut screen fixes one; a PIPELINE
        # screen infers a stage PER PIECE, so one straggler a stage behind gives
        # the batch a second stage. Raising on the first denial then 403'd a scan
        # of 40 legitimately-PASTING pieces because one piece was still at
        # FUSING: one bad piece losing the 39 good ones, precisely the outcome
        # gates 2-4 are per-piece to prevent. So when the batch is MIXED and at
        # least one stage IS permitted, the denied stages degrade to a per-piece
        # bucket — the good pieces log, the bad ones come back with the same 403
        # text as their reason.
        denied: dict = {}
        for stage, op in op_by_stage.items():
            try:
                await self._assert_role(user, stage, op)
            except HTTPException as exc:
                denied[stage] = exc
        if denied and len(denied) == len(op_by_stage):
            raise next(iter(denied.values()))

        # ── CONSUMPTION REQUIREMENTS: THE TWO CUTS DIFFER (bugs #9/#10) ──────
        # This used to be one rule for "any cut stage": dcm required, lot
        # required, else 422. That is right for LEATHER — leather is the
        # expensive, per-hide-measured material the whole costing rests on — and
        # wrong for LINING, where the client has now confirmed every field
        # (DCM, article, style, colour, thickness) is OPTIONAL.
        #
        # A lining cut with nothing supplied is a legitimate, complete record: the
        # work happened, the piece advances, and no stock moves because nobody
        # measured any. Forcing a number there only teaches the floor to invent
        # one, which is a worse ledger than an absent one.
        #
        # Supplying consumption on a lining cut still behaves exactly as before —
        # it is optional, not ignored.
        lining_only = cut_stages == {ProductionStage.LINING_CUTTING}
        consumption_value = None
        if is_cut and (consumption_qty is not None or not lining_only):
            if consumption_qty is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Consumption quantity (dcm) is required at leather cutting — "
                    "it is what the material ledger and the costing are built on. "
                    "Send `consumption.dcm` with `consumption.article` (+ colour, "
                    "and thickness if it narrows the lot), or a lot id directly.")
            try:
                consumption_value = Decimal(str(consumption_qty))
            except Exception as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Consumption quantity must be numeric.") from exc
            if consumption_value <= 0:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Consumption quantity must be > 0 at cutting.")
            if not (leather_lot_id or lining_lot_id):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "A measured cut needs to name its material: send "
                    "`consumption.article` (+ colour / thickness) or a lot id.")

        screen_role_warning = None
        if screen in SCREEN_EXPECTED_ROLE and user.role not in _STAGE_BYPASS_ROLES:
            expected = SCREEN_EXPECTED_ROLE[screen]
            if user.role not in expected:
                screen_role_warning = (
                    f"{user.name} ({user.role.value}) is logging on the "
                    f"{screen.value} screen.")
 
        logged, rework, sequence_blocked, skill_blocked, merge_blocked =  [], [], [], [], []
        role_blocked: list[str] = []
        sequence_blocked += uncut_on_pipeline   # never cut → can't be past cutting
        skill_warnings: list[dict] = []   # GATE 2 anomalies (non-blocking now)
        fresh_cut_count = 0
        # The stage each piece was ACTUALLY resolved to. The single top-level
        # `stage` cannot describe a mixed pipeline batch, and guessing one from
        # dict order reported a stage no piece was written at — see below.
        stage_by_code: dict[str, str] = {}

        # GATES 2-4 per piece + (write, IF NOT preview)
        for pid, piece in pieces.items():
            stage = stage_by_piece.get(pid)
            if stage is None:
                continue
            op = op_by_stage[stage]
            stage_by_code[piece.code] = stage.value

            # GATE 1 (per-piece remainder) — only reachable on a MIXED batch in
            # which some other stage was permitted; an all-denied batch already
            # raised the 403 above.
            if stage in denied:
                role_blocked.append(piece.code)
                _block(piece.code, "role", str(denied[stage].detail), stage)
                continue

            # GATE 2 — SKILL
            # GATE 2 — SKILL: DEMOTED to a recorded warning (drawer-redesign
            # build). Any employee may be recorded at any stage; the anomaly is
            # surfaced for audit but never blocks the log. The manager-role gate
            # (GATE 1) remains the hard authority on WHO may enter the log.
            if not self._skill_ok(emp.designation, stage):
                skill_warnings.append({
                    "piece": piece.code,
                    "employee": emp.name,
                    "designation": Designation.normalise(emp.designation),
                    "stage": stage.value,
                    "note": self._skill_msg(emp.name, emp.designation, stage),
                })
                # NB: no `continue` — the piece proceeds through the remaining
                # gates and is logged.
            # GATE 3 — SEQUENCE (no-skip). The reason string _sequence_ok has
            # always built was being discarded into `_`; it is the only thing
            # that tells the floor WHICH stage is missing.
            ok_seq, why_seq = await self._sequence_ok(piece, stage)
            if not ok_seq:
                sequence_blocked.append(piece.code)
                _block(piece.code, "sequence", why_seq or "", stage)
                continue
            # GATE 4 — MERGE (line-stitch entry)
            ok_merge, why_merge = await self._merge_ok(piece, stage)
            if not ok_merge:
                merge_blocked.append(piece.code)
                _block(piece.code, "merge", why_merge or "", stage)
                continue
            # already logged at this op? → rework
            if await self.repo.has_event_at_op(piece.id, op.id):
                rework.append(piece.code)
                continue
 
            logged.append(piece.code)
            if not preview:
                # REAL write only
                await self.repo.add_event_nocommit(
                    sku_id=piece.sku_id, operation_id=op.id, employee_id=employee_id,
                    work_date=work_date, qty=1, piece_id=piece.id,
                    entered_by=user.name,
                    leather_lot_id=leather_lot_id if is_cut and leather_lot_id is not None else None,
                    lining_lot_id=lining_lot_id if is_cut and lining_lot_id is not None else None,
                    consumption_qty=consumption_value if is_cut else None)
                # advance the piece's current operation pointer
                piece.current_operation_id = op.id
                if is_cut:
                    fresh_cut_count += 1
                # RECYCLE THE DRAWER at PACKAGE_EXPORT: the piece has shipped, so
                # its drawer returns to WAITING for the next merge. This is the
                # ONLY point a drawer frees (Hamthan #4: empty only after PACKAGE).
                # release_nocommit clears both sides of the piece<->drawer link.
                if stage is ProductionStage.PACKAGE_EXPORT:
                    from app.modules.drawers.service import DrawerService
                    await DrawerService(self.db).release_nocommit(piece.id)
 
        consumption_recorded = None
        stock_warning = None
        # NO MEASUREMENT → NO DECREMENT. An unmeasured lining cut (bug #10) writes
        # its events and stops there: there is no quantity to take off any lot, and
        # inventing one would corrupt the ledger this block exists to keep honest.
        if (is_cut and not preview and fresh_cut_count > 0
                and consumption_value is not None
                and (leather_lot_id or lining_lot_id)):
            lot_id = leather_lot_id or lining_lot_id
            from app.modules.materials.service import MaterialService
            total_consumption = consumption_value * fresh_cut_count
            # Hold the instance: the shortfall warning comes back on it, not in
            # the return value (which stays a float for existing callers).
            materials = MaterialService(self.db)
            avail = await materials.decrement_for_cut_nocommit(
                lot_id, float(total_consumption))
            stock_warning = materials.last_decrement_warning
            consumption_recorded = {
                "lot_id": str(lot_id),
                "pieces_consuming": fresh_cut_count,
                "qty": float(total_consumption),
                "dcm": float(total_consumption),
                "available_after": avail,
                # True when the ledger went short. The cut is still recorded —
                # see decrement_for_cut_nocommit for why this warns, not blocks.
                "stock_short": stock_warning is not None,
            }
 
        # ── the reported stage ────────────────────────────────────────────────
        # A cut SCREEN fixes one stage for the whole batch, so it answers for
        # itself. PIPELINE does not: it infers a stage PER PIECE, so a batch can
        # legitimately span several. `next(iter(op_by_stage))` picked whichever
        # stage happened to be inserted first — i.e. the first PIECE's stage —
        # and reported it for the whole response, so a batch that wrote FUSING
        # could answer `"stage": "LINE_STITCHING"` because an unrelated, merge-
        # blocked piece was scanned first. That is a wrong answer, not a vague
        # one. Now: one resolved stage → name it; several → "MIXED", with the
        # per-piece truth in stage_by_piece; none resolved → null, and `message`
        # says why (that null is what a caller sees when every piece was blocked
        # before a stage could be resolved at all).
        stages_seen = [s.value for s in dict.fromkeys(stage_by_piece.values())]
        if screen_stage is not None:
            rep_stage = screen_stage.value
        elif len(stages_seen) == 1:
            rep_stage = stages_seen[0]
        elif stages_seen:
            rep_stage = "MIXED"
        else:
            rep_stage = None

        # BUG #12 — the drawer for every piece in this batch, on the response the
        # scan screen already reads. One query for the whole batch. Without it the
        # operator has to leave the production screen to find out where the
        # garment they just logged actually lives.
        drawers_by_id = await self.repo.drawers_for_pieces(list(pieces))
        drawer_by_piece = {
            piece.code: drawers_by_id[pid]
            for pid, piece in pieces.items() if pid in drawers_by_id
        }

        result = {
            "stage": rep_stage,
            "stages": stages_seen,
            "stage_by_piece": stage_by_code,
            "count_logged": len(logged),
            "logged": logged, "rework": rework, "not_found": not_found,
            "completed": completed,
            "sequence_blocked": sequence_blocked, "skill_blocked": skill_blocked,
            "merge_blocked": merge_blocked, "role_blocked": role_blocked,
            "blocked": blocked,
            "message": self._log_message(
                rep_stage=rep_stage, logged=logged, rework=rework,
                blocked=blocked, not_found=not_found),
            "screen_role_warning": screen_role_warning,
            "consumption_recorded": None,
            "stock_warning": None,
            "preview": bool(preview),
            "skill_warnings": skill_warnings,
            # BUG #12 — where each scanned garment lives, on the response the scan
            # screen already reads. {piece_code: {code, state, holding, …}}.
            "drawer_by_piece": drawer_by_piece,
            # BUG #8 — filled in after the commit; see below.
            "sku_progress": None,
        }

        if preview:
            # SHORT-CIRCUIT: nothing written, nothing committed.
            return result

        await self.db.commit()
        result["consumption_recorded"] = consumption_recorded
        result["stock_warning"] = stock_warning

        # BUG #8 — how much of this SKU is left at the stage just logged, so the
        # screen can CLOSE a style for further scanning the moment it completes
        # rather than trusting a client-side tally (which a reload, a second gun
        # or a second operator all defeat).
        #
        # DELIBERATELY AFTER THE COMMIT. The session runs autoflush=False
        # (core/database.py:104), so a count issued while the events were still
        # pending would not see them and would report one piece too many as
        # remaining — closing the style one scan late, every time.
        if logged and rep_stage and rep_stage != "MIXED":
            resolved = screen_stage or next(
                iter(dict.fromkeys(stage_by_piece.values())), None)
            first = next(iter(pieces.values()), None)
            if resolved is not None and first is not None:
                result["sku_progress"] = await self._sku_progress_at_stage(
                    first.sku_id, resolved)
        return result

    @staticmethod
    def _log_message(*, rep_stage: str | None, logged: list, rework: list,
                     blocked: list, not_found: list) -> str:
        """One human sentence for the scan screen.

        Exists because the old response could come back with `stage: null`,
        `count_logged: 0` and a bare piece code in `sequence_blocked` — every
        field technically correct and the operator still unable to tell whether
        the system was broken or the piece was.
        """
        if logged:
            head = f"Logged {len(logged)} piece(s)"
            head += f" at {rep_stage}." if rep_stage and rep_stage != "MIXED" \
                else " across several stages (see stage_by_piece)."
        elif rework:
            head = f"Nothing new logged — {len(rework)} piece(s) already recorded " \
                   f"at this stage (rework)."
        elif not_found and not blocked:
            head = f"Nothing logged — {len(not_found)} code(s) did not resolve."
        elif blocked:
            gates = {b["gate"] for b in blocked}
            if gates == {"not_cut"}:
                head = (f"Nothing logged — none of these pieces has been cut yet, "
                        f"so there is no next pipeline stage to infer. Cut them on "
                        f"the {ScreenContext.LEATHER_CUT.value} / "
                        f"{ScreenContext.LINING_CUT.value} screen first.")
            elif gates == {"completed"}:
                head = "Nothing logged — these pieces have already finished the line."
            else:
                head = (f"Nothing logged — {len(blocked)} piece(s) were blocked "
                        f"({', '.join(sorted(gates))}). See `blocked` for the reason "
                        f"on each.")
        else:
            head = "Nothing logged."
        return head
    # ═══════════════════════════════════ THE SCAN-TIME STATE READ (bugs 4/6/8/12)
    async def piece_state(self, piece_id: uuid.UUID,
                          *, user: User | None = None,
                          employee_id: uuid.UUID | None = None) -> dict:
        """Everything the scan screen needs about ONE piece, before it logs.

        THIS IS THE SCAN AND THE VERIFY, FOR ONE PIECE. Pass `employee_id` (the
        scanned card) and it also answers whether that worker can log the piece's
        next stage right now — `ready_to_log` is the single boolean an automatic
        screen needs, and `blockers` says why when it is false. Without it the
        verify step has to fall back to a SKU-wide read, which describes a whole
        style and cannot answer anything about the garment in the operator's hand.

        WHY THIS ENDPOINT EXISTS
            Three of the reported bugs are the same missing read:

              #4  the operator has to pick the stage by hand, because the stage is
                  only inferred at WRITE time — the UI cannot see it in advance.
              #6  later stage cards can be scanned into before their predecessor
                  is done, because the UI has no way to know which are locked.
              #12 the drawer is invisible outside the Store hub.

            All three are answered by asking the server, at scan time, "what is
            true about this piece?" — which is what this returns.

        IT REUSES THE WRITE PATH'S OWN PREDICATES — _infer_stage_for_piece,
        _sequence_ok, _merge_ok — rather than restating them. That is the whole
        design constraint: a second, parallel copy of the gate logic would drift,
        and the failure mode is the worst kind — a card the UI shows as open that
        the server then rejects, or (worse) a card the UI locks that the server
        would have accepted, silently stalling the line. One source of truth, read
        and write.
        """
        piece = await self.db.get(Piece, piece_id)
        if piece is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Piece not found.")

        from app.modules.barcode.service import BarcodeService
        card = await BarcodeService(self.db)._piece_payload(piece_id)

        done = await self.repo.completed_stage_codes(piece.id)
        chain = ProductionStage.leather_chain()
        next_stage = await self._infer_stage_for_piece(piece, ScreenContext.PIPELINE)

        # ── the stage card map (bug #6) ──────────────────────────────────────
        # Every stage the UI draws a card for, with the reason it is not open.
        # LINING_CUTTING is included even though it is off the leather chain: it
        # is a real card on the floor (its own screen, its own manager) and a
        # lined piece is not finished with the cut side without it.
        stages: list[dict] = []
        for stage in [ProductionStage.LEATHER_CUTTING,
                      ProductionStage.LINING_CUTTING, *chain[1:]]:
            entry: dict = {"stage": stage.value,
                           "requires_consumption": stage.requires_consumption}
            if stage.value in done:
                entry["state"] = "completed"
                stages.append(entry)
                continue
            # A lining cut on a piece that needs no lining is not "locked" —
            # it is simply not part of this garment's route.
            if stage is ProductionStage.LINING_CUTTING and not bool(
                    getattr(piece, "needs_lining", True)):
                entry["state"] = "not_applicable"
                entry["reason"] = (f"{piece.code} needs no lining, so the lining "
                                   f"cut never applies to it.")
                stages.append(entry)
                continue
            ok_seq, why_seq = await self._sequence_ok(piece, stage)
            ok_merge, why_merge = await self._merge_ok(piece, stage)
            if not ok_seq:
                entry.update(state="locked", gate="sequence", reason=why_seq)
            elif not ok_merge:
                entry.update(state="locked", gate="merge", reason=why_merge)
            elif stage is next_stage or stage.is_cut_entry:
                # Cut entries have no predecessor, so they are open whenever they
                # are not already done — they are reached from their own screen,
                # not by pipeline inference, which is why `next_stage` (a PIPELINE
                # answer) does not name them.
                entry["state"] = "next"
            else:
                entry.update(
                    state="locked", gate="sequence",
                    reason=(f"{piece.code} is not at {stage.value} yet — "
                            f"{next_stage.value if next_stage else 'the line'} "
                            f"comes first."))
            stages.append(entry)

        # ── how much of this SKU is left at that stage (bug #8) ──────────────
        sku_block = await self._sku_progress_at_stage(piece.sku_id, next_stage)

        drawer = card.get("drawer")
        disp = display_stage(
            current_event_stage=card.get("current_stage"),
            drawer_state=(drawer or {}).get("state"),
            needs_lining=bool(getattr(piece, "needs_lining", True)),
        )

        # ── THE VERIFY ANSWER ────────────────────────────────────────────────
        # Everything above describes the PIECE. To decide whether this scan can
        # simply log itself, the screen also needs the verdict on the WORKER —
        # which is what `actor` / `blockers` / `ready_to_log` add when an employee
        # is supplied. Without them a "verify" button has to guess, or fall back
        # to a SKU-wide read that answers a different question entirely.
        actor, blockers = await self._verify_actor(
            employee_id=employee_id, stage=next_stage)

        role_ok = await self._can_log(user, next_stage)
        if role_ok is False and next_stage is not None:
            role_name = getattr(getattr(user, "role", None), "value", "this login")
            blockers.append({
                "gate": "role",
                "reason": (f"{role_name} may not log {next_stage.value} — ask the "
                           f"manager who owns that stage."),
            })

        if next_stage is None:
            blockers.append({
                "gate": "completed",
                "reason": f"{piece.code} has finished the line — nothing left to log.",
            })
        else:
            # The stage card already computed WHY the next stage is shut, if it
            # is. Reuse that verdict rather than forming a second opinion.
            nxt_card = next(
                (s for s in stages if s["stage"] == next_stage.value), None)
            if nxt_card and nxt_card["state"] == "locked":
                blockers.append({"gate": nxt_card.get("gate", "sequence"),
                                 "reason": nxt_card.get("reason", "")})
            if next_stage.requires_consumption:
                blockers.append({
                    "gate": "consumption",
                    "reason": (f"{next_stage.value} is a cut — it is logged from "
                               f"its own cut screen with the material, not by an "
                               f"automatic pipeline scan."),
                })

        return {
            "piece": card,
            "drawer": drawer,                                   # bug #12
            "completed_stages": sorted(done),
            # WHERE THE PIECE IS NOW, promoted to the top level. It was only
            # available nested inside `piece`, beside a `display_stage` that can
            # legitimately read STORE — so "what stage is this piece in" had two
            # plausible answers and neither was obvious. `current_stage` is the
            # real event-backed stage; `display_stage` is what the board shows.
            "current_stage": card.get("current_stage"),
            "current_stage_label": (
                card["current_stage"].replace("_", " ").title()
                if card.get("current_stage") else "Not started"),
            "next_stage": next_stage.value if next_stage else None,   # bug #4
            "next_stage_requires_consumption": bool(
                next_stage and next_stage.requires_consumption),
            "display_stage": disp["display_stage"],
            "display_label": disp["label"],
            "in_store": disp["in_store"],
            "stages": stages,                                   # bug #6
            "sku": sku_block,                                   # bug #8
            # Whether THIS login could log the next stage — so the screen can say
            # "ask the stitching manager" instead of letting the scan 403.
            "can_log_next": role_ok,
            "actor": actor,
            "blockers": blockers,
            # THE ONE BOOLEAN A SCAN SCREEN NEEDS. True = this scan can be logged
            # as it stands, with no stage picked by hand and nothing else to ask.
            # None (not False) when no employee was supplied: the question cannot
            # be answered without one, and False would tell the screen the piece
            # is blocked when it is merely unasked.
            "ready_to_log": None if employee_id is None else not blockers,
        }

    async def _verify_actor(
        self, *, employee_id: uuid.UUID | None,
        stage: "ProductionStage | None",
    ) -> tuple[dict | None, list[dict]]:
        """The worker half of the verify: who they are, and anything stopping them
        logging this stage right now.

        Kept beside piece_state rather than folded into it because it is the only
        part of that read which touches attendance, and because it returns the
        actor block the scan screen prints regardless of the verdict.
        """
        if employee_id is None:
            return None, []

        emp = await self.employees.get(employee_id)
        if emp is None:
            return None, [{"gate": "employee",
                           "reason": "No employee record for that card."}]

        blockers: list[dict] = []
        from app.modules.attendance.service import AttendanceService
        present = await AttendanceService(self.db).is_present_today(employee_id)
        if not present:
            blockers.append({
                "gate": "attendance",
                "reason": (f"{emp.name} is not checked in today — mark attendance "
                           f"before recording their work."),
            })

        # SKILL IS A WARNING, NOT A BLOCKER, and it stays one here. The log
        # records the piece and reports the anomaly; a verify that refused it
        # would stall the line over an advisory the server would have accepted.
        skill_ok = self._skill_ok(emp.designation, stage)
        return {
            "employee_id": str(emp.id),
            "name": emp.name,
            "designation": Designation.normalise(emp.designation),
            "present_today": present,
            "skill_ok": skill_ok,
            "skill_note": (None if skill_ok or stage is None
                           else self._skill_msg(emp.name, emp.designation, stage)),
        }, blockers

    async def _sku_progress_at_stage(self, sku_id: uuid.UUID,
                                     stage: "ProductionStage | None") -> dict:
        """How many pieces of this SKU still need `stage` (bug #8).

        The cutting screen used to load a whole style at once and submit it in one
        click; the fix is per-piece scanning, and per-piece scanning needs a
        server-side answer to "am I done with this style yet?" — a frontend tally
        of what the operator happened to scan is not that answer (a reload, a
        second gun or a second operator all break it).

        Reuses repo.piece_ids_done_at_op, the same batch read the checklist uses.
        """
        rows = await self.repo.list_pieces_for_sku(sku_id)
        piece_ids = [p.id for p, _, _, _ in rows]
        out = {"sku_id": str(sku_id), "total": len(piece_ids),
               "stage": stage.value if stage else None,
               "done": 0, "remaining": len(piece_ids), "closed": False}
        if stage is None or not piece_ids:
            return out
        op = await self.repo.get_operation_by_code(stage.value)
        if op is None:
            return out
        done_ids = await self.repo.piece_ids_done_at_op(piece_ids, op.id)
        out["done"] = len(done_ids)
        out["remaining"] = len(piece_ids) - len(done_ids)
        # CLOSED = every piece of this SKU is logged at this stage, so the style
        # must stop being offered for scanning here (the client's words).
        out["closed"] = out["remaining"] == 0
        return out

    async def _can_log(self, user: User | None,
                       stage: "ProductionStage | None") -> bool | None:
        """Would GATE 1 let this login log that stage? None when unknowable."""
        if user is None or stage is None:
            return None
        op = await self.repo.get_operation_by_code(stage.value)
        if op is None:
            return None
        try:
            await self._assert_role(user, stage, op)
        except HTTPException:
            return False
        return True

    # ══════════════════════════════════════════════════════════════ readers
    async def list_pieces_for_sku(self, *, sku_id: uuid.UUID | None = None,
                                  sku_code: str | None = None,
                                  operation_id: uuid.UUID | None = None,
                                  client_scope: uuid.UUID | None = None) -> dict:
        sku_id = await self._resolve_sku_id(sku_id, sku_code)
        if not sku_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide sku_id or sku_code.")
        sku = await self.clients.get_sku(sku_id)
        if not sku:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found")
        if client_scope is not None and not await self.clients.is_sku_visible_to_client(
            sku_id, client_scope
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found")

        op = None
        if operation_id:
            op = await self.repo.get_operation(operation_id)
            if not op:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Operation not found")

        # rows are 4-tuples: (piece, stage_code, stage_label, client_order_id).
        rows = await self.repo.list_pieces_for_sku(sku_id)
        piece_ids = [p.id for p, _, _, _ in rows]

        done_ids: set[uuid.UUID] = set()
        prev_done_ids: set[uuid.UUID] = set()
        prev_stage = None
        if op:
            done_ids = await self.repo.piece_ids_done_at_op(piece_ids, op.id)
            stage = self._stage_of(op)
            prev_stage = stage.predecessor() if stage else None
            if prev_stage:
                prev_op = await self.repo.get_operation_by_code(prev_stage.value)
                if prev_op:
                    prev_done_ids = await self.repo.piece_ids_done_at_op(piece_ids, prev_op.id)
                else:
                    prev_stage = None

        order_id = rows[0][3] if rows else None
        # Live drawer per piece → drives the STORE overlay AND (bug #12) puts the
        # drawer code on every checklist row, so a manager on any stage can see
        # where the garment is without opening the Store hub.
        drawers = await self.repo.drawers_for_pieces(piece_ids)

        pieces = []
        for p, scode, slabel, _ in rows:
            done = p.id in done_ids
            eligible, reason = True, None
            if op and prev_stage and not done and p.id not in prev_done_ids:
                eligible, reason = False, f"{prev_stage.value} not completed"

            # STORE overlay: if the piece has cleared the cut side and its drawer
            # is holding, SHOW store (+ sub-status). Never show LINE_STITCHING
            # until a real line-stitching event exists.
            drawer = drawers.get(p.id)
            disp = display_stage(
                current_event_stage=scode,
                drawer_state=(drawer or {}).get("state"),
                needs_lining=bool(getattr(p, "needs_lining", True)),
            )
            pieces.append({
                "piece_id": p.id, "code": p.code, "seq": p.seq,
                # bug #7: the zero-padded serial the cutting screen must show.
                "serial": f"{p.seq:03d}" if p.seq is not None else None,
                "order_id": order_id,
                # real event stage kept for callers that need the raw value:
                "event_stage": scode, "event_stage_label": slabel,
                # what the UI shows (may be STORE):
                "current_stage": disp["display_stage"],
                "current_stage_label": disp["label"],
                "in_store": disp["in_store"],
                "store_status": disp["store_status"],
                # bug #12: the assigned drawer, on every row.
                "drawer": drawer,
                "drawer_code": (drawer or {}).get("code"),
                "done_at_op": done, "eligible": eligible, "blocked_reason": reason,
            })

        # The ENVELOPE, not a bare list. Restored after the STORE-overlay edit
        # dropped it (the checklist screen reads total/done/pending/blocked from
        # here; without the return the endpoint answered `null`).
        done_count = sum(1 for x in pieces if x["done_at_op"])
        return {
            "sku_id": sku_id, "sku_code": sku.code,
            "colour": sku.color_name or sku.color_code, "size": sku.size,
            "order_id": order_id,
            "operation_id": op.id if op else None,
            "operation_code": op.code if op else None,
            "total": len(pieces), "done": done_count,
            "pending": len(pieces) - done_count,
            "blocked": sum(1 for x in pieces if not x["eligible"]),
            # BUG #8: every piece of this SKU is logged at the requested operation,
            # so the style must stop being offered for scanning here. False when no
            # operation was named — "closed" is meaningless without a stage.
            "closed": bool(op) and done_count == len(pieces) and bool(pieces),
            "pieces": pieces,
        }

    async def list_events(self, **filters) -> list[ProductionEvent]:
        return await self.repo.list_events(**filters)

    async def style_progress(self, style_id: uuid.UUID,
                             client_scope: uuid.UUID | None = None) -> dict[str, int]:
        """Per-stage completed counts for a style.

        TENANCY: the scoped WHERE alone is not enough. A client asking for
        another client's style used to get 200 + {} — indistinguishable from
        "your style, no work logged yet", and still a confirmation the id is
        real. Existence itself is information, so an invisible (or unknown)
        style is a 404, matching list_pieces_for_sku."""
        if not await self.clients.get_style(style_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Style not found")
        if client_scope is not None and not await self.clients.is_style_visible_to_client(
            style_id, client_scope
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Style not found")
        return await self.repo.stage_totals_for_style(style_id, client_scope=client_scope)

    async def list_sku_options(self, *, order_id=None, style_id=None,
                               client_scope: uuid.UUID | None = None) -> list[dict]:
        return await self.clients.list_sku_options(
            order_id=order_id, style_id=style_id, client_scope=client_scope)

    async def piece_counts(self, start: date, end: date):
        return await self.repo.piece_counts_by_employee_style_op(start, end)

    async def _resolve_sku_id(self, sku_id, sku_code):
        if sku_id:
            return sku_id
        if sku_code:
            sku = await self.clients.get_sku_by_code(sku_code)
            if not sku:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    f"Unknown SKU code '{sku_code}'")
            return sku.id
        return None