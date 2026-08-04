"""
================================================================================
modules/barcode/repository.py — Async data access for the barcode registry
================================================================================
Owns every write to barcode_registry + the sequence counters that make employee
and drawer codes unique. resolve() is a single indexed read on `code`.

CODE GENERATION IS DETERMINISTIC AND COLLISION-SAFE.
    Piece codes come from the piece (STYLE-COLOUR-SIZE-seq, already unique).
    Employee/drawer/lot codes are minted here as PREFIX + zero-padded counter
    (EMP-000123). The counter is the current max for that prefix + 1; the unique
    index on `code` is the hard guard, so a concurrent mint fails loudly rather
    than colliding — retry the request if that ever fires (create rate is a few
    per day, so it effectively never does).
================================================================================
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import BarcodeStatus, BarcodeType
from app.modules.barcode.models import BarcodeRegistry

from datetime import datetime
from sqlalchemy import and_, func, select
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.production.models import Operation, Piece


def _norm(code: str | None) -> str:
    return (code or "").strip().upper()


class BarcodeRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── resolve ──────────────────────────────────────────────────────────────
    async def get_by_code(self, code: str) -> BarcodeRegistry | None:
        res = await self.db.execute(
            select(BarcodeRegistry).where(BarcodeRegistry.code == _norm(code))
        )
        return res.scalar_one_or_none()

    async def get_for_employee(self, employee_id: uuid.UUID,
                               active_only: bool = True) -> BarcodeRegistry | None:
        stmt = select(BarcodeRegistry).where(
            BarcodeRegistry.employee_id == employee_id,
            BarcodeRegistry.type == BarcodeType.EMPLOYEE.value,
        )
        if active_only:
            stmt = stmt.where(BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
        return await self.db.scalar(stmt.order_by(BarcodeRegistry.created_at.desc()))

    async def codes_for_employees(
        self, employee_ids: list[uuid.UUID], active_only: bool = True
    ) -> dict[uuid.UUID, str]:
        """employee_id → its card code, for a whole roster in ONE query.

        The per-row `get_for_employee` would be N queries behind a roster list;
        this is the batch form. Newest-first ordering means the dict keeps the
        most recent code per employee when a reissue left older retired rows
        behind (active_only=False)."""
        if not employee_ids:
            return {}
        stmt = select(
            BarcodeRegistry.employee_id, BarcodeRegistry.code
        ).where(
            BarcodeRegistry.employee_id.in_(employee_ids),
            BarcodeRegistry.type == BarcodeType.EMPLOYEE.value,
        )
        if active_only:
            stmt = stmt.where(BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
        res = await self.db.execute(stmt.order_by(BarcodeRegistry.created_at.asc()))
        # asc + overwrite == newest wins, without a window function.
        return {emp_id: code for emp_id, code in res.all() if emp_id is not None}

    # ── code minting ─────────────────────────────────────────────────────────
    async def _next_code(self, prefix: str, width: int = 6) -> str:
        """PREFIX + zero-padded (max existing counter for this prefix) + 1.

        F79/F99: previously this SELECTed every barcode with this prefix and took
        the max in Python — O(all barcodes) transferred per mint, quadratic across
        an import that mints hundreds of codes. Since the zero-padded numeric tail
        is fixed-width, lexicographic order == numeric order, so ORDER BY code DESC
        LIMIT 1 gives the highest existing code while transferring ONE row. This is
        dialect-agnostic (works identically on SQLite and Postgres); a dedicated
        integer sequence column would be the fully clean long-term form.
        """
        like = f"{prefix}-%"
        top = await self.db.scalar(
            select(BarcodeRegistry.code)
            .where(BarcodeRegistry.code.like(like))
            .order_by(BarcodeRegistry.code.desc())
            .limit(1)
        )
        mx = 0
        if top:
            tail = top[len(prefix) + 1:]
            if tail.isdigit():
                mx = int(tail)
        return f"{prefix}-{mx + 1:0{width}d}"

    # ── registration (all *_nocommit; the SERVICE owns the transaction) ──────
    def register_nocommit(self, *, code: str, type_: BarcodeType,
                          caption: str | None = None,
                          piece_id: uuid.UUID | None = None,
                          employee_id: uuid.UUID | None = None,
                          drawer_id: uuid.UUID | None = None,
                          material_lot_id: uuid.UUID | None = None) -> BarcodeRegistry:
        row = BarcodeRegistry(
            code=_norm(code), type=type_.value, status=BarcodeStatus.ACTIVE.value,
            caption=caption, piece_id=piece_id, employee_id=employee_id,
            drawer_id=drawer_id, material_lot_id=material_lot_id,
        )
        self.db.add(row)
        return row

    async def mint_employee_code_nocommit(self, employee_id: uuid.UUID,
                                          caption: str | None) -> BarcodeRegistry:
        code = await self._next_code("EMP")
        return self.register_nocommit(
            code=code, type_=BarcodeType.EMPLOYEE,
            employee_id=employee_id, caption=caption)

    async def mint_drawer_code_nocommit(self, drawer_id: uuid.UUID, seq: int,
                                        caption: str | None = None) -> BarcodeRegistry:
        code = f"DRW-{seq:04d}"
        return self.register_nocommit(
            code=code, type_=BarcodeType.DRAWER, drawer_id=drawer_id,
            caption=caption or f"Drawer {seq}")

    async def mint_lot_code_nocommit(self, material_lot_id: uuid.UUID,
                                     type_: BarcodeType,
                                     caption: str | None) -> BarcodeRegistry:
        prefix = {"LEATHER_LOT": "LOT-LEA", "LINING_LOT": "LOT-LIN",
                  "ACCESSORY_LOT": "LOT-ACC"}[type_.value]
        code = await self._next_code(prefix)
        return self.register_nocommit(
            code=code, type_=type_, material_lot_id=material_lot_id, caption=caption)

    # ── retire / reissue (employee) ──────────────────────────────────────────
    async def retire_nocommit(self, row: BarcodeRegistry, reason: str) -> None:
        row.status = BarcodeStatus.RETIRED.value
        row.retired_at = datetime.now(timezone.utc)
        row.retired_reason = reason

    # ── batch fetch for print ────────────────────────────────────────────────
    async def get_many(self, codes: list[str]) -> list[BarcodeRegistry]:
        if not codes:
            return []
        norm = [_norm(c) for c in codes]
        res = await self.db.execute(
            select(BarcodeRegistry).where(BarcodeRegistry.code.in_(norm))
        )
        return list(res.scalars())

    async def commit(self) -> None:
        await self.db.commit()

    # ── order picker ────────────────────────────────────────────────────────
    async def list_orders_with_barcodes(
        self, client_id: uuid.UUID | None = None
    ) -> list[dict]:
        """Every order that has at least one PIECE barcode, with its minted count
        and the generated-at date range. `client_id` (a CLIENT login) scopes to
        that client's orders only; None = staff, all orders."""
        from app.modules.barcode.models import BarcodeRegistry
        from app.modules.clients.models import Client, ClientOrder

        stmt = (
            select(
                ClientOrder.id.label("order_id"),
                ClientOrder.order_number,
                Client.name.label("client_name"),
                func.count(BarcodeRegistry.id).label("minted"),
                func.min(BarcodeRegistry.created_at).label("first_generated_at"),
                func.max(BarcodeRegistry.created_at).label("last_generated_at"),
            )
            .join(Client, Client.id == ClientOrder.client_id)
            .join(
                BarcodeRegistry,
                and_(BarcodeRegistry.order_id == ClientOrder.id,
                        BarcodeRegistry.type == "piece"),
            )
            .group_by(ClientOrder.id, ClientOrder.order_number, Client.name)
            .order_by(func.max(BarcodeRegistry.created_at).desc())
        )
        if client_id is not None:
            stmt = stmt.where(ClientOrder.client_id == client_id)

        rows = (await self.db.execute(stmt)).all()
        return [
            {
                "order_id": r.order_id,
                "order_number": r.order_number,
                "client_name": r.client_name,
                "minted": int(r.minted),
                "first_generated_at": r.first_generated_at,
                "last_generated_at": r.last_generated_at,
            }
            for r in rows
        ]

    # ── planned totals (SKU.qty_ordered) ────────────────────────────────────
    async def order_planned_total(self, order_id: uuid.UUID) -> int:
        from app.modules.clients.models import SKU, Style
        total = await self.db.scalar(
            select(func.coalesce(func.sum(SKU.qty_ordered), 0))
            .select_from(SKU)
            .join(Style, Style.id == SKU.style_id)
            .where(Style.client_order_id == order_id)
        )
        return int(total or 0)

    async def order_minted_total(self, order_id: uuid.UUID) -> int:
        from app.modules.barcode.models import BarcodeRegistry
        total = await self.db.scalar(
            select(func.count(BarcodeRegistry.id))
            .where(BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == "piece")
        )
        return int(total or 0)

    async def order_active_total(self, order_id: uuid.UUID) -> int:
        """Minted AND still active (a retired label is minted but not scannable)."""
        from app.modules.barcode.models import BarcodeRegistry
        total = await self.db.scalar(
            select(func.count(BarcodeRegistry.id))
            .where(BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == "piece",
                    BarcodeRegistry.status == "active")
        )
        return int(total or 0)

    async def order_distinct_code_total(self, order_id: uuid.UUID) -> int:
        """DISTINCT codes — if this ever differs from minted, a duplicate slipped
        past the unique index. Lets analytics PROVE uniqueness (duplicates=0)."""
        from app.modules.barcode.models import BarcodeRegistry
        total = await self.db.scalar(
            select(func.count(func.distinct(BarcodeRegistry.code)))
            .where(BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == "piece")
        )
        return int(total or 0)

    # ── per-style analytics (planned vs minted vs balance) ──────────────────
    async def style_breakdown(self, order_id: uuid.UUID) -> list[dict]:
        """Per-style planned (SUM qty_ordered) vs minted (count of piece barcodes)
        vs balance. Two independent aggregates joined on style_id in Python so a
        style with 0 minted still appears (LEFT side = planned)."""
        from app.modules.barcode.models import BarcodeRegistry
        from app.modules.clients.models import SKU, Style

        planned_rows = (await self.db.execute(
            select(Style.id, Style.name, Style.code,
                    func.coalesce(func.sum(SKU.qty_ordered), 0).label("planned"))
            .select_from(Style)
            .join(SKU, SKU.style_id == Style.id)
            .where(Style.client_order_id == order_id)
            .group_by(Style.id, Style.name, Style.code)
        )).all()

        minted_rows = (await self.db.execute(
            select(BarcodeRegistry.style_id,
                    func.count(BarcodeRegistry.id).label("minted"))
            .where(BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == "piece")
            .group_by(BarcodeRegistry.style_id)
        )).all()
        minted_by_style = {r.style_id: int(r.minted) for r in minted_rows}

        out = []
        for r in planned_rows:
            minted = minted_by_style.get(r.id, 0)
            planned = int(r.planned)
            out.append({
                "style_id": r.id,
                "style_name": r.name,
                "style_code": r.code,
                "planned": planned,
                "minted": minted,
                "balance": max(planned - minted, 0),
            })
        out.sort(key=lambda x: x["style_name"] or "")
        return out

    # ── filterable history list (paginated) ─────────────────────────────────
    async def list_barcodes(
        self,
        order_id: uuid.UUID,
        *,
        sku_id: uuid.UUID | None = None,
        style_id: uuid.UUID | None = None,
        size: str | None = None,
        status: str | None = None,
        date_from: "datetime | None" = None,
        date_to: "datetime | None" = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict], int]:
        """Return (rows, total_count). Filters: style, sku, style+size, status,
        generated-date range. size filters via SKU.size (join only when needed)."""
        from app.modules.barcode.models import BarcodeRegistry
        from app.modules.clients.models import SKU, Style
        from app.modules.production.models import Operation, Piece

        # Base filter on the indexed denormalised columns.
        conds = [BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == "piece"]
        if sku_id:
            conds.append(BarcodeRegistry.sku_id == sku_id)
        if style_id:
            conds.append(BarcodeRegistry.style_id == style_id)
        if status:
            conds.append(BarcodeRegistry.status == status.lower())
        if date_from:
            conds.append(BarcodeRegistry.created_at >= date_from)
        if date_to:
            conds.append(BarcodeRegistry.created_at <= date_to)

        need_sku_join = bool(size)  # size lives on SKU

        # total count (respecting filters)
        count_stmt = select(func.count(BarcodeRegistry.id))
        if need_sku_join:
            count_stmt = count_stmt.join(SKU, SKU.id == BarcodeRegistry.sku_id)
            conds.append(func.upper(SKU.size) == size.strip().upper())
        count_stmt = count_stmt.where(and_(*conds))
        total = int(await self.db.scalar(count_stmt) or 0)

        # page of rows, enriched with sku/style/size/colour/seq/stage/drawer
        stmt = (
            select(
                BarcodeRegistry.code,
                BarcodeRegistry.status,
                BarcodeRegistry.caption,
                BarcodeRegistry.created_at,
                SKU.code.label("sku_code"),
                SKU.color_name, SKU.color_code, SKU.size,
                Style.name.label("style_name"),
                Piece.seq,
                Operation.code.label("current_stage"),
            )
            .join(SKU, SKU.id == BarcodeRegistry.sku_id)
            .join(Style, Style.id == BarcodeRegistry.style_id)
            .outerjoin(Piece, Piece.id == BarcodeRegistry.piece_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .where(and_(*conds))
            .order_by(BarcodeRegistry.created_at.desc(), BarcodeRegistry.code)
            .limit(page_size)
            .offset((max(page, 1) - 1) * page_size)
        )
        rows = (await self.db.execute(stmt)).all()
        items = [
            {
                "code": r.code,
                "status": r.status,
                "sku_code": r.sku_code,
                "style_name": r.style_name,
                "colour": r.color_name or r.color_code,
                "size": r.size,
                "seq": r.seq,
                "current_stage": r.current_stage,
                "generated_at": r.created_at,
            }
            for r in rows
        ]
        return items, total

    # ── SKU picker for the filter dropdowns (scoped to one order) ────────────
    async def list_order_skus(self, order_id: uuid.UUID) -> list[dict]:
        from app.modules.clients.models import SKU, Style
        rows = (await self.db.execute(
            select(SKU.id, SKU.code, SKU.color_name, SKU.color_code, SKU.size,
                    Style.id.label("style_id"), Style.name.label("style_name"))
            .join(Style, Style.id == SKU.style_id)
            .where(Style.client_order_id == order_id)
            .order_by(Style.name, SKU.size)
        )).all()
        return [
            {"sku_id": r.id, "sku_code": r.code,
                "colour": r.color_name or r.color_code, "size": r.size,
                "style_id": r.style_id, "style_name": r.style_name}
            for r in rows
        ]