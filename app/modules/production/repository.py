"""
================================================================================
modules/production/repository.py — Async data access for production
================================================================================
production_event is the finest grain: one manager records that one employee did
ONE PIECE of one operation on one day (qty=1). All aggregates below are unchanged
by per-piece because qty is always 1 (sum(qty) == piece count).

BARCODE-FEATURE CHANGES (this build)
- stage_piece_nocommit gains leather_lot_id / lining_lot_id / consumption_qty
  (defaulted None, so every existing caller is unaffected). Populated only at a
  cut stage by the two-door log.
- get_operation_by_code is now CASE-INSENSITIVE: the service matches uppercase
  ProductionStage values ("LEATHER_CUTTING") against operation codes that may be
  stored mixed-case in legacy rows. Exact-match would silently miss the gate.
- NEW completed_stage_codes(piece_id): the set of operation codes a piece has
  events at — used to infer the next pipeline stage.
- mint_pieces is RETAINED for back-compat but is NO LONGER CALLED at cutting;
  pieces mint at breakdown upload (imports/premint.py). Kept so any tool still
  referencing it does not break; safe to delete once nothing imports it.
================================================================================
"""
import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clients.models import ClientOrder, SKU, Style
from app.modules.production.models import (
    Operation,
    OperationAccess,
    Piece,
    ProductionEvent,
)


class ProductionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # --- operations / access ---
    async def list_operations(self) -> list[Operation]:
        res = await self.db.execute(
            select(Operation).where(Operation.is_active.is_(True)).order_by(Operation.sequence)
        )
        return list(res.scalars())

    async def get_operation(self, op_id: uuid.UUID) -> Operation | None:
        return await self.db.get(Operation, op_id)

    async def get_operation_by_code(self, code: str) -> Operation | None:
        # CASE-INSENSITIVE: ProductionStage values are UPPER; legacy op codes may
        # be mixed-case. Exact-match would miss the gate for those rows.
        norm = (code or "").strip().upper()
        res = await self.db.execute(
            select(Operation).where(func.upper(Operation.code) == norm)
        )
        return res.scalar_one_or_none()

    async def operations_for_role(self, role: str) -> set[uuid.UUID]:
        res = await self.db.execute(
            select(OperationAccess.operation_id).where(OperationAccess.role == role)
        )
        return set(res.scalars())

    # --- pieces ---
    async def max_seq_for_sku(self, sku_id: uuid.UUID) -> int:
        return int(await self.db.scalar(
            select(func.coalesce(func.max(Piece.seq), 0)).where(Piece.sku_id == sku_id)
        ) or 0)

    async def get_piece_by_code(self, code: str) -> Piece | None:
        res = await self.db.execute(
            select(Piece).where(func.upper(Piece.code) == (code or "").strip().upper())
        )
        return res.scalar_one_or_none()

    async def get_piece_by_sku_seq(self, sku_id: uuid.UUID, seq: int) -> Piece | None:
        res = await self.db.execute(
            select(Piece).where(Piece.sku_id == sku_id, Piece.seq == seq)
        )
        return res.scalar_one_or_none()

    async def has_event_at_op(self, piece_id: uuid.UUID, operation_id: uuid.UUID) -> bool:
        found = await self.db.scalar(
            select(ProductionEvent.id).where(
                ProductionEvent.piece_id == piece_id,
                ProductionEvent.operation_id == operation_id,
            ).limit(1)
        )
        return found is not None

    async def completed_stage_codes(self, piece_id: uuid.UUID) -> set[str]:
        """The set of operation CODES (upper) this piece has an event at.

        Used to INFER the next pipeline stage: the furthest leather-chain stage a
        piece has an event at, + 1. One query per piece (a piece has < 10 events).
        """
        rows = await self.db.execute(
            select(Operation.code)
            .join(ProductionEvent, ProductionEvent.operation_id == Operation.id)
            .where(ProductionEvent.piece_id == piece_id)
            .distinct()
        )
        return {(c or "").strip().upper() for (c,) in rows.all()}

    async def mint_pieces(
        self, *, sku_id: uuid.UUID, operation: Operation, employee_id: uuid.UUID,
        work_date: date, count: int, entered_by: str | None, prefix: str,
    ) -> list[Piece]:
        """DEPRECATED at cutting — pieces mint at breakdown upload now. Retained
        for back-compat; see module docstring. Creates `count` pieces + events.
        """
        base = await self.max_seq_for_sku(sku_id)
        pieces: list[Piece] = []
        for i in range(1, count + 1):
            seq = base + i
            pieces.append(Piece(
                code=f"{prefix}-{seq:03d}", seq=seq, sku_id=sku_id,
                current_operation_id=operation.id,
            ))
        self.db.add_all(pieces)
        await self.db.flush()
        for p in pieces:
            self.db.add(ProductionEvent(
                sku_id=sku_id, operation_id=operation.id, employee_id=employee_id,
                work_date=work_date, qty=1, entered_by=entered_by, piece_id=p.id,
            ))
        await self.db.commit()
        for p in pieces:
            await self.db.refresh(p)
        return pieces

    def stage_piece_nocommit(
        self, *, piece: Piece, operation_id: uuid.UUID, employee_id: uuid.UUID,
        work_date: date, entered_by: str | None,
        leather_lot_id: uuid.UUID | None = None,
        lining_lot_id: uuid.UUID | None = None,
        consumption_qty=None,
    ) -> None:
        """Add one qty=1 event for an existing piece and advance its current
        stage. Caller commits once for the whole batch.

        Consumption columns are populated ONLY at a cut stage; everywhere else
        they default to None and the event is exactly as before.
        """
        self.db.add(ProductionEvent(
            sku_id=piece.sku_id, operation_id=operation_id, employee_id=employee_id,
            work_date=work_date, qty=1, entered_by=entered_by, piece_id=piece.id,
            leather_lot_id=leather_lot_id, lining_lot_id=lining_lot_id,
            consumption_qty=consumption_qty,
        ))
        piece.current_operation_id = operation_id

    async def commit(self) -> None:
        await self.db.commit()

    # --- events ---
    async def add_event(self, **kw) -> ProductionEvent:
        ev = ProductionEvent(**kw)
        self.db.add(ev)
        await self.db.commit()
        await self.db.refresh(ev)
        return ev

    async def list_events(self, sku_id: uuid.UUID | None = None,
                        employee_id: uuid.UUID | None = None,
                        start: date | None = None,
                        end: date | None = None) -> list[ProductionEvent]:
        stmt = select(ProductionEvent)
        if sku_id:
            stmt = stmt.where(ProductionEvent.sku_id == sku_id)
        if employee_id:
            stmt = stmt.where(ProductionEvent.employee_id == employee_id)
        if start:
            stmt = stmt.where(ProductionEvent.work_date >= start)
        if end:
            stmt = stmt.where(ProductionEvent.work_date <= end)
        res = await self.db.execute(stmt.order_by(ProductionEvent.work_date.desc()))
        return list(res.scalars())

    async def stage_totals_for_style(self, style_id: uuid.UUID,
                                      client_scope: uuid.UUID | None = None) -> dict[str, int]:
        """SUM qty per operation across all SKUs of a style — the live card."""
        stmt = (
            select(Operation.code, func.coalesce(func.sum(ProductionEvent.qty), 0))
            .select_from(ProductionEvent)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(SKU.style_id == style_id)
        )
        if client_scope is not None:
            stmt = stmt.where(ClientOrder.client_id == client_scope)
        stmt = stmt.group_by(Operation.code)
        res = await self.db.execute(stmt)
        return {code: int(total) for code, total in res.all()}

    async def piece_counts_by_employee_style_op(self, start: date, end: date):
        """Rows of (employee_id, style_id, operation_id, work_date, total_qty) for
        a window — the raw material for piece-rate wage calculation. work_date is
        kept in the grouping so a mid-period rate change prices each day at the
        rate effective that day."""
        stmt = (
            select(
                ProductionEvent.employee_id,
                SKU.style_id,
                ProductionEvent.operation_id,
                ProductionEvent.work_date,
                func.sum(ProductionEvent.qty),
            )
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .where(ProductionEvent.work_date >= start, ProductionEvent.work_date <= end)
            .group_by(
                ProductionEvent.employee_id,
                SKU.style_id,
                ProductionEvent.operation_id,
                ProductionEvent.work_date,
            )
            # H11: deterministic ordering. Wage aggregation folds these rows in
            # arrival order, so an unordered result made the stored display rate
            # depend on the query plan.
            .order_by(
                ProductionEvent.employee_id,
                SKU.style_id,
                ProductionEvent.operation_id,
                ProductionEvent.work_date,
            )
        )
        res = await self.db.execute(stmt)
        return res.all()

    async def list_pieces_for_sku(
        self, sku_id: uuid.UUID
    ) -> list[tuple[Piece, str | None, str | None]]:
        """Every active piece of a SKU + its current stage (code, label)."""
        res = await self.db.execute(
            select(Piece, Operation.code, Operation.label)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .where(Piece.sku_id == sku_id, Piece.is_active.is_(True))
            .order_by(Piece.seq)
        )
        return [(p, c, l) for p, c, l in res.all()]

    async def piece_ids_done_at_op(
        self, piece_ids: list[uuid.UUID], operation_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """Batch form of has_event_at_op — ONE query, no N+1 on the scan screen."""
        if not piece_ids:
            return set()
        res = await self.db.execute(
            select(ProductionEvent.piece_id)
            .where(ProductionEvent.operation_id == operation_id,
                   ProductionEvent.piece_id.in_(piece_ids))
            .distinct()
        )
        return set(res.scalars())