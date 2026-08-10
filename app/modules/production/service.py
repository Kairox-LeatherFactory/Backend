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
    ProductionStage, ScreenContext, UserRole,
)
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
 
        # resolve each piece's stage (unchanged)
        stage_by_piece: dict = {}
        for pid, piece in pieces.items():
            stage = screen_stage or await self._infer_stage_for_piece(piece, screen)
            if stage is None:
                not_found.append(f"{piece.code} (no next stage — already complete?)")
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
 
        # GATE 1 — ROLE (403, whole request) — runs in preview too, so the user
        # sees the same 403 they'd hit on commit.
        for stage, op in op_by_stage.items():
            await self._assert_role(user, stage, op)

        consumption_value = None
        if is_cut:
            if consumption_qty is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Consumption quantity is required for cut stages.")
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
                    "A cut stage requires a material lot.")
 
        screen_role_warning = None
        if screen in SCREEN_EXPECTED_ROLE and user.role not in _STAGE_BYPASS_ROLES:
            expected = SCREEN_EXPECTED_ROLE[screen]
            if user.role not in expected:
                screen_role_warning = (
                    f"{user.name} ({user.role.value}) is logging on the "
                    f"{screen.value} screen.")
 
        logged, rework, sequence_blocked, skill_blocked, merge_blocked =  [], [], [], [], []
        skill_warnings: list[dict] = []   # GATE 2 anomalies (non-blocking now)
        fresh_cut_count = 0
 
        # GATES 2-4 per piece + (write, IF NOT preview)
        for pid, piece in pieces.items():
            stage = stage_by_piece.get(pid)
            if stage is None:
                continue
            op = op_by_stage[stage]
 
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
            # GATE 3 — SEQUENCE (no-skip)
            ok_seq, _ = await self._sequence_ok(piece, stage)
            if not ok_seq:
                sequence_blocked.append(piece.code)
                continue
            # GATE 4 — MERGE (line-stitch entry)
            ok_merge, _ = await self._merge_ok(piece, stage)
            if not ok_merge:
                merge_blocked.append(piece.code)
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
        if is_cut and not preview and fresh_cut_count > 0:
            lot_id = leather_lot_id or lining_lot_id
            from app.modules.materials.service import MaterialService
            total_consumption = consumption_value * fresh_cut_count
            avail = await MaterialService(self.db).decrement_for_cut_nocommit(
                lot_id, float(total_consumption))
            consumption_recorded = {
                "lot_id": str(lot_id),
                "pieces_consuming": fresh_cut_count,
                "qty": float(total_consumption),
                "dcm": float(total_consumption),
                "available_after": avail,
            }
 
        rep_stage = screen_stage or (next(iter(op_by_stage)) if op_by_stage else None)
 
        if preview:
            # SHORT-CIRCUIT: nothing written, nothing committed.
            return {
                "stage": rep_stage.value if rep_stage else None,
                "count_logged": len(logged),
                "logged": logged, "rework": rework, "not_found": not_found,
                "sequence_blocked": sequence_blocked, "skill_blocked": skill_blocked,
                "merge_blocked": merge_blocked,
                "screen_role_warning": screen_role_warning,
                "consumption_recorded": None,
                "preview": True,
                "skill_warnings": skill_warnings,
            }
 
        await self.db.commit()
        return {
            "stage": rep_stage.value if rep_stage else None,
            "count_logged": len(logged),
            "logged": logged, "rework": rework, "not_found": not_found,
            "sequence_blocked": sequence_blocked, "skill_blocked": skill_blocked,
            "merge_blocked": merge_blocked,
            "screen_role_warning": screen_role_warning,
            "consumption_recorded": consumption_recorded,
            "preview": False,
            "skill_warnings": skill_warnings,
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
        order_id = rows[0][3] if rows else None
        # Live drawer state per piece → drives the STORE overlay.
        drawer_states = await self.repo.drawer_states_for_pieces(
            [p.id for p, _, _ in rows])

        from app.core.store_display import display_stage

        pieces = []
        for p, scode, slabel in rows:
            done = p.id in done_ids
            eligible, reason = True, None
            if op and prev_stage and not done and p.id not in prev_done_ids:
                eligible, reason = False, f"{prev_stage.value} not completed"

            # STORE overlay: if the piece has cleared the cut side and its drawer
            # is holding, SHOW store (+ sub-status). Never show LINE_STITCHING
            # until a real line-stitching event exists.
            disp = display_stage(
                current_event_stage=scode,
                drawer_state=drawer_states.get(p.id),
                needs_lining=bool(getattr(p, "needs_lining", True)),
            )
            pieces.append({
                "piece_id": p.id, "code": p.code, "seq": p.seq,
                "order_id": order_id, 
                # real event stage kept for callers that need the raw value:
                "event_stage": scode, "event_stage_label": slabel,
                # what the UI shows (may be STORE):
                "current_stage": disp["display_stage"],
                "current_stage_label": disp["label"],
                "in_store": disp["in_store"],
                "store_status": disp["store_status"],
                "done_at_op": done, "eligible": eligible, "blocked_reason": reason,
            })

    async def list_events(self, **filters) -> list[ProductionEvent]:
        return await self.repo.list_events(**filters)

    async def style_progress(self, style_id: uuid.UUID,
                             client_scope: uuid.UUID | None = None) -> dict[str, int]:
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