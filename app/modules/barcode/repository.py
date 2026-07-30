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

    # ── code minting ─────────────────────────────────────────────────────────
    async def _next_code(self, prefix: str, width: int = 6) -> str:
        """PREFIX + zero-padded (max existing counter for this prefix) + 1."""
        like = f"{prefix}-%"
        rows = await self.db.execute(
            select(BarcodeRegistry.code).where(BarcodeRegistry.code.like(like))
        )
        mx = 0
        plen = len(prefix) + 1
        for (c,) in rows.all():
            tail = c[plen:]
            if tail.isdigit():
                mx = max(mx, int(tail))
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