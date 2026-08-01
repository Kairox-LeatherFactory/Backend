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

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    MERGE_GATE_ENTRY, MULTI_STAGE_DESIGNATIONS, SCREEN_EXPECTED_ROLE,
    SCREEN_TO_STAGE, STAGE_DESIGNATIONS, STAGE_ROLE_ACCESS, Designation,
    ProductionStage, ScreenContext, UserRole,
)
from app.modules.clients.service import ClientService
from app.modules.employees.service import EmployeeService
from app.modules.production.models import Operation, Piece, ProductionEvent
from app.modules.production.repository import ProductionRepository
from app.modules.users.models import User

# Roles that log ANY stage. MD is the superuser; DM logs freely (spec).
_STAGE_BYPASS_ROLES = frozenset({UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER})


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
        # PIPELINE: find the furthest chain stage the piece has an event at.
        chain = ProductionStage.leather_chain()
        done = await self.repo.completed_stage_codes(piece.id)
        furthest = -1
        for i, st in enumerate(chain):
            if st.value in done:
                furthest = i
        nxt = furthest + 1
        return chain[nxt] if 0 <= nxt < len(chain) else None

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
                f"Role '{user.role.value}' may not log '{stage.value}'. Permitted: {names}.")
        # unknown op → config table only
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
        (leather + lining both merged and the DM released it)."""
        if stage is not MERGE_GATE_ENTRY:
            return True, None
        from app.modules.drawers.service import DrawerService
        if await DrawerService(self.db).is_sended(piece.id):
            return True, None
        return False, (f"{piece.code} is not ready for {stage.value} — its drawer "
                       "must hold leather + lining and be marked SENDED by the DM.")

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
                        consumption_qty: float | None = None) -> dict:
        """Log one stage for a batch of pieces. Stage is inferred, never sent."""
        emp = await self.employees.get(employee_id)
        if not emp:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found.")
        await self._assert_present(employee_id, work_date)

        if not piece_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No pieces provided.")

        # Load pieces once, de-duplicated, caller order preserved.
        pieces: dict[uuid.UUID, Piece] = {}
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

        # ─────────────────────────────────────────────────────────────────────
        # B3: resolve EVERY piece's stage BEFORE any gate runs.
        # The old code took one "representative" stage from the first piece and
        # ran the role gate against that alone, while each piece's real stage was
        # recomputed later in the loop. In PIPELINE mode a batch spans several
        # stages, so every piece whose stage differed from the first piece's was
        # logged with NO role check. Gate 1 stays whole-request (403) per the
        # module contract — it is now simply checked against every stage present.
        # ─────────────────────────────────────────────────────────────────────
        stage_by_piece: dict[uuid.UUID, ProductionStage] = {}
        for pid, piece in pieces.items():
            stage = screen_stage or await self._infer_stage_for_piece(piece, screen)
            if stage is None:
                not_found.append(f"{piece.code} (no next stage — already complete?)")
                continue
            stage_by_piece[pid] = stage

        # One Operation lookup per distinct stage (dict.fromkeys keeps order).
        op_by_stage: dict[ProductionStage, Operation] = {}
        for stage in dict.fromkeys(stage_by_piece.values()):
            op = await self.repo.get_operation_by_code(stage.value)
            if op is None:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    f"Stage '{stage.value}' has no configured operation.")
            op_by_stage[stage] = op

        # GATE 1 — ROLE, on every stage in the batch, before anything is logged.
        for stage, op in op_by_stage.items():
            await self._assert_role(user, stage, op)

        rep_stage = screen_stage or (next(iter(op_by_stage)) if op_by_stage else None)
        cut_stages = {s for s in op_by_stage if s.requires_consumption}
        is_cut = bool(cut_stages)
        if is_cut and len(op_by_stage) > 1:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A cutting scan may not be mixed with other stages in one batch — "
                "material consumption cannot be attributed across stages.")

        # Screen↔role cross-check (warning, not a block).
        screen_role_warning = None
        if screen in SCREEN_EXPECTED_ROLE and user.role not in _STAGE_BYPASS_ROLES:
            expected = SCREEN_EXPECTED_ROLE[screen]
            if user.role not in expected:
                screen_role_warning = (
                    f"{user.name} ({user.role.value}) is logging on the "
                    f"{screen.value} screen; expected "
                    f"{', '.join(sorted(getattr(r,'value',str(r)) for r in expected))}.")

        # GATE 2 — SKILL. H10: per-piece against that piece's own stage, so one
        # mismatch no longer discards the whole tray, and skill_blocked is real.
        skill_blocked: list[str] = []
        for pid in list(stage_by_piece):
            stage = stage_by_piece[pid]
            if not self._skill_ok(emp.designation, stage):
                skill_blocked.append(
                    f"{pieces[pid].code}: "
                    f"{self._skill_msg(emp.name, emp.designation, stage)}")
                del stage_by_piece[pid]

        # Consumption required at a cut stage.
        consumed_out = None
        cut_stage = next(iter(cut_stages)) if is_cut else None
        cut_lot_id = None
        if is_cut:
            if consumption_qty is None or consumption_qty <= 0:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"{cut_stage.value} requires material consumption (qty > 0).")
            cut_lot_id = (leather_lot_id
                          if cut_stage is ProductionStage.LEATHER_CUTTING
                          else lining_lot_id)
            if cut_lot_id is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "A material lot is required at cutting.")

        logged, rework, seq_blocked, merge_blocked = [], [], [], []
        fresh_cut = 0          # B4: pieces consuming material for the FIRST time
        seen: set[uuid.UUID] = set()

        for pid in piece_ids:
            if pid in seen:
                continue
            seen.add(pid)
            stage = stage_by_piece.get(pid)
            if stage is None:
                continue        # not found, no next stage, or skill-blocked
            piece = pieces[pid]
            op = op_by_stage[stage]

            is_rework = await self.repo.has_event_at_op(piece.id, op.id)

            if not is_rework:
                ok, why = await self._sequence_ok(piece, stage)
                if not ok:
                    seq_blocked.append(why)
                    continue
                ok, why = await self._merge_ok(piece, stage)
                if not ok:
                    merge_blocked.append(why)
                    continue
                if stage.requires_consumption:
                    fresh_cut += 1
            else:
                rework.append(piece.code)

            # Consumption rides the EVENT (the act of cutting), never the piece.
            # Attributed from THIS piece's stage, not a batch-wide guess.
            ev_leather = ev_lining = ev_qty = None
            if stage.requires_consumption:
                if stage is ProductionStage.LEATHER_CUTTING:
                    ev_leather = leather_lot_id
                else:
                    ev_lining = lining_lot_id
                ev_qty = consumption_qty

            self.repo.stage_piece_nocommit(
                piece=piece, operation_id=op.id, employee_id=employee_id,
                work_date=work_date, entered_by=user.name,
                leather_lot_id=ev_leather, lining_lot_id=ev_lining,
                consumption_qty=ev_qty)
            logged.append(piece.code)

            # recycle the drawer when the piece is packaged/exported
            if stage is ProductionStage.PACKAGE_EXPORT:
                from app.modules.drawers.service import DrawerService
                await DrawerService(self.db).release_nocommit(piece.id)

        # B4: decrement ONCE per batch, for FIRST-TIME cuts only. A rework pass
        # re-logs the event but consumes no new hide, so it must not move stock.
        if is_cut and fresh_cut:
            from app.modules.materials.service import MaterialService
            total = float(consumption_qty) * fresh_cut
            avail = await MaterialService(self.db).decrement_for_cut_nocommit(
                cut_lot_id, total)
            consumed_out = {"lot_id": str(cut_lot_id), "dcm": total,
                            "pieces_consuming": fresh_cut,
                            "available_after": avail}

        await self.repo.commit()
        return {
            "stage": rep_stage.value if rep_stage else None,
            "count_logged": len(logged),
            "logged": logged, "rework": rework, "not_found": not_found,
            "sequence_blocked": seq_blocked, "skill_blocked": skill_blocked,
            "merge_blocked": merge_blocked,
            "screen_role_warning": screen_role_warning,
            "consumption_recorded": consumed_out,
        }

    @staticmethod
    def _empty_result(stage, *, skill, screen_warning):
        return {
            "stage": stage.value if stage else None, "count_logged": 0,
            "logged": [], "rework": [], "not_found": [],
            "sequence_blocked": [], "skill_blocked": skill, "merge_blocked": [],
            "screen_role_warning": screen_warning, "consumption_recorded": None,
        }

    # ══════════════════════════════════════════════════════════════ readers
    async def list_pieces_for_sku(self, *, sku_id: uuid.UUID | None = None,
                                  sku_code: str | None = None,
                                  operation_id: uuid.UUID | None = None,client_scope: uuid.UUID | None = None) -> dict:
        sku_id = await self._resolve_sku_id(sku_id, sku_code)
        if not sku_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide sku_id or sku_code.")
        sku = await self.clients.get_sku(sku_id)
        if not sku:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found")

        op = None
        if operation_id:
            op = await self.repo.get_operation(operation_id)
            if not op:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Operation not found")

        rows = await self.repo.list_pieces_for_sku(sku_id)
        piece_ids = [p.id for p, _, _ in rows]

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

        pieces = []
        for p, scode, slabel in rows:
            done = p.id in done_ids
            eligible, reason = True, None
            if op and prev_stage and not done and p.id not in prev_done_ids:
                eligible, reason = False, f"{prev_stage.value} not completed"
            pieces.append({
                "piece_id": p.id, "code": p.code, "seq": p.seq,
                "current_stage": scode, "current_stage_label": slabel,
                "done_at_op": done, "eligible": eligible, "blocked_reason": reason,
            })
        done_count = sum(1 for x in pieces if x["done_at_op"])
        return {
            "sku_id": sku_id, "sku_code": sku.code,
            "colour": sku.color_name or sku.color_code, "size": sku.size,
            "operation_id": op.id if op else None,
            "operation_code": op.code if op else None,
            "total": len(pieces), "done": done_count,
            "pending": len(pieces) - done_count,
            "blocked": sum(1 for x in pieces if not x["eligible"]),
            "pieces": pieces,
        }

    async def list_events(self, **filters) -> list[ProductionEvent]:
        return await self.repo.list_events(**filters)

    async def style_progress(self, style_id: uuid.UUID,client_scope: uuid.UUID | None = None) -> dict[str, int]:
        return await self.repo.stage_totals_for_style(style_id)

    async def list_sku_options(self, *, order_id=None, style_id=None, client_scope: uuid.UUID | None = None) -> list[dict]:
        return await self.clients.list_sku_options(order_id=order_id, style_id=style_id)

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