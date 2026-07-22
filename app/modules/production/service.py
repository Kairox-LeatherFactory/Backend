"""
================================================================================
modules/production/service.py — Production logic + role->operation access (async)
================================================================================

CORE FLOW (per-piece)
    cut()   mints N pieces for a SKU at CUTTING (seq continues from the SKU's
            current max), builds each piece's qualified code
            {ORDER}-{STYLE}-{sku.code}-{seq}, one transaction, returns the codes
            to print on traveler cards.
    scan()  logs a batch at ONE operation. Two input styles, both supported:
              • SKU-scoped  : sku_id + piece_seqs=[1,2,5,...]  (manager picks the
                              SKU, types just the numbers)  ← primary path
              • Full-code   : piece_codes=["KJ2451-CLERMONT-57-M-005", ...]
                              (typed in full / future scan-gun)
            Existing pieces get one qty=1 event each and advance their current
            stage; unknown seqs/codes are reported, never written (a typo can't
            corrupt the ledger); a piece already seen at this operation is still
            logged and flagged REWORK (permissive). One commit for the batch.

Cross-module access goes through ClientService / EmployeeService.
================================================================================
"""
import re
import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.modules.clients.service import ClientService
from app.modules.employees.service import EmployeeService
from app.modules.production.models import Operation, Piece, ProductionEvent
from app.modules.production.repository import ProductionRepository
from app.modules.users.models import User

CUTTING_CODE = "CUTTING"


def _norm(code: str) -> str:
    """Normalise a manually-typed code so lookups match what we stored."""
    return (code or "").strip().upper()


def _slug(s: str | None, limit: int = 24) -> str:
    """Compact, typeable token for a code segment (alnum, upper, no spaces)."""
    return (re.sub(r"[^A-Za-z0-9]", "", (s or "")).upper() or "NA")[:limit]


class ProductionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ProductionRepository(db)
        self.clients = ClientService(db)
        self.employees = EmployeeService(db)

    async def list_operations(self) -> list[Operation]:
        return await self.repo.list_operations()

    # ------------------------------------------------------------------ helpers
    async def _assert_can_log(self, user: User, op: Operation) -> None:
        if user.role != UserRole.DIRECT_MANAGER and user.role != UserRole.MANAGER_DIRECT:
            allowed = await self.repo.operations_for_role(user.role.value)
            if op.id not in allowed:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"Your role may not log operation '{op.code}'",
                )

    async def _assert_present(self, employee_id: uuid.UUID, work_date: date) -> None:
        """A worker not checked in TODAY cannot have produced TODAY. History is
        left alone."""
        if work_date == date.today():
            from app.modules.attendance.service import AttendanceService
            if not await AttendanceService(self.db).is_present_today(employee_id):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Employee is not checked in today — mark attendance first.",
                )

    # -------------------------------------------------------------------- cut
    async def cut(self, *, user: User, sku_id: uuid.UUID, employee_id: uuid.UUID,
                  work_date: date, count: int) -> list[Piece]:
        """Mint `count` pieces for a SKU at the cutting table."""
        if count < 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "count must be >= 1")

        ctx = await self.clients.sku_label_context(sku_id)
        if not ctx:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found")
        if not await self.employees.get(employee_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")

        op = await self.repo.get_operation_by_code(CUTTING_CODE)
        if not op:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"No '{CUTTING_CODE}' operation configured",
            )
        await self._assert_can_log(user, op)
        await self._assert_present(employee_id, work_date)

        # {ORDER}-{STYLE}-{sku.code}   e.g. KJ2451-CLERMONT-57-M  -> -{seq} appended
        # prefix = ctx["label"]
        prefix = _norm(ctx["code"])
        return await self.repo.mint_pieces(
            sku_id=sku_id, operation=op, employee_id=employee_id,
            work_date=work_date, count=count, entered_by=user.name, prefix=prefix,
        )

    # ------------------------------------------------------------------- scan
    async def scan(self, *, user: User, operation_id: uuid.UUID,
                   employee_id: uuid.UUID, work_date: date,
                   sku_id: uuid.UUID | None = None,
                   piece_seqs: list[int] | None = None,
                   piece_codes: list[str] | None = None) -> dict:
        """Log a batch at one operation. Partial-accept + per-item report."""
        if not piece_codes and not (sku_id and piece_seqs):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Provide either (sku_id + piece_seqs) or piece_codes.",
            )
        op = await self.repo.get_operation(operation_id)
        if not op:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Operation not found")
        if not await self.employees.get(employee_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
        await self._assert_can_log(user, op)
        await self._assert_present(employee_id, work_date)

        # Resolve every target to (display_token, piece_or_None).
        targets: list[tuple[str, Piece | None]] = []
        if sku_id and piece_seqs:
            for s in piece_seqs:
                targets.append((f"#{s}", await self.repo.get_piece_by_sku_seq(sku_id, s)))
        if piece_codes:
            for c in piece_codes:
                targets.append((c, await self.repo.get_piece_by_code(_norm(c))))

        logged: list[str] = []
        rework: list[str] = []
        not_found: list[str] = []
        seen: set[uuid.UUID] = set()

        for token, piece in targets:
            if piece is None:
                not_found.append(token)
                continue
            if piece.id in seen:
                continue
            seen.add(piece.id)
            if await self.repo.has_event_at_op(piece.id, op.id):
                rework.append(piece.code)
            self.repo.stage_piece_nocommit(
                piece=piece, operation_id=op.id, employee_id=employee_id,
                work_date=work_date, entered_by=user.name,
            )
            logged.append(piece.code)

        await self.repo.commit()
        return {
            "operation": op.code,
            "count_logged": len(logged),
            "logged": logged,
            "rework": rework,
            "not_found": not_found,
        }
        
        
    async def list_pieces_for_sku(self, *, sku_id: uuid.UUID | None = None,
                                  sku_code: str | None = None,
                                  operation_id: uuid.UUID | None = None) -> dict:
        """Every minted piece of a SKU — the scan screen's checklist.
        With operation_id each piece is flagged done_at_op so the UI can badge
        the ones already logged there. Deliberately NOT gated on the previous
        stage: surface state, let the manager decide (same as scan())."""
        sku_id = await self._resolve_sku_id(sku_id, sku_code)
        if not sku_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Provide sku_id or sku_code.")
        sku = await self.clients.get_sku(sku_id)
        if not sku:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found")

        op = None
        if operation_id:
            op = await self.repo.get_operation(operation_id)
            if not op:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Operation not found")

        rows = await self.repo.list_pieces_for_sku(sku_id)
        done_ids: set[uuid.UUID] = set()
        if op:
            done_ids = await self.repo.piece_ids_done_at_op(
                [p.id for p, _, _ in rows], op.id)

        pieces = [
            {"piece_id": p.id, "code": p.code, "seq": p.seq,
             "current_stage": scode, "current_stage_label": slabel,
             "done_at_op": p.id in done_ids}
            for p, scode, slabel in rows
        ]
        done = sum(1 for x in pieces if x["done_at_op"])
        return {
            "sku_id": sku_id, "sku_code": sku.code,
            "colour": sku.color_name or sku.color_code, "size": sku.size,
            "operation_id": op.id if op else None,
            "operation_code": op.code if op else None,
            "total": len(pieces), "done": done, "pending": len(pieces) - done,
            "pieces": pieces,
        }

    # # --------------------------------------------------- legacy generic event
    
    # async def log_event(self, user: User, sku_id: uuid.UUID, operation_id: uuid.UUID,
    #                     employee_id: uuid.UUID, work_date: date, qty: int) -> ProductionEvent:
    #     """Back-compat qty-based entry (no piece linkage). Prefer cut()/scan()."""
    #     if not await self.clients.get_sku(sku_id):
    #         raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found")
    #     if not await self.employees.get(employee_id):
    #         raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    #     op = await self.repo.get_operation(operation_id)
    #     if not op:
    #         raise HTTPException(status.HTTP_404_NOT_FOUND, "Operation not found")
    #     await self._assert_can_log(user, op)
    #     await self._assert_present(employee_id, work_date)
    #     return await self.repo.add_event(
    #         sku_id=sku_id, operation_id=operation_id, employee_id=employee_id,
    #         work_date=work_date, qty=qty, entered_by=user.name,
    #     )

    async def list_events(self, **filters) -> list[ProductionEvent]:
        return await self.repo.list_events(**filters)

    async def style_progress(self, style_id: uuid.UUID) -> dict[str, int]:
        return await self.repo.stage_totals_for_style(style_id)

    async def list_sku_options(self, *, order_id: uuid.UUID | None = None,
                               style_id: uuid.UUID | None = None) -> list[dict]:
        """Friendly SKU picker (code + 'style · colour · size' label, no UUID)."""
        return await self.clients.list_sku_options(order_id=order_id, style_id=style_id)

    # Public interface for the wages module:
    async def piece_counts(self, start: date, end: date):
        return await self.repo.piece_counts_by_employee_style_op(start, end)
    
    
    async def _resolve_sku_id(self, sku_id, sku_code):
        if sku_id:
            return sku_id
        if sku_code:
            sku = await self.clients.get_sku_by_code(sku_code)
            if not sku:
                from fastapi import HTTPException, status
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    f"Unknown SKU code '{sku_code}'")
            return sku.id
        return None