"""
================================================================================
modules/cutting/repository.py — all DB access for the cutting grid
================================================================================
Repository = all DB access, service = business logic, router = HTTP only
(CLAUDE.md §15). The sheet side deliberately is NOT here: a hide belongs to
MaterialRepository, because it is a stock item that exists before any row claims
it and outlives the row being deleted. This module reaches for it through
MaterialService, the same way production reaches for consumption.
================================================================================
"""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import CuttingRowStatus
from app.modules.clients.models import SKU, Style
from app.modules.cutting.models import CuttingRow
from app.modules.production.models import Piece


class CuttingRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── rows ─────────────────────────────────────────────────────────────────
    def add_row_nocommit(self, **kw) -> CuttingRow:
        row = CuttingRow(**kw)
        self.db.add(row)
        return row

    async def get_row(self, row_id: uuid.UUID) -> CuttingRow | None:
        return await self.db.get(CuttingRow, row_id)

    async def row_for_piece(self, piece_id: uuid.UUID) -> CuttingRow | None:
        res = await self.db.execute(
            select(CuttingRow).where(CuttingRow.piece_id == piece_id))
        return res.scalar_one_or_none()

    async def rows_for_pieces(self, piece_ids: list) -> dict:
        """{piece_id: row} — the batch form the production logger needs.

        One query for a whole scan, because a 40-piece cut batch must not become
        40 round-trips on the hot path.
        """
        if not piece_ids:
            return {}
        res = await self.db.execute(
            select(CuttingRow).where(CuttingRow.piece_id.in_(tuple(piece_ids))))
        return {r.piece_id: r for r in res.scalars().all()}

    async def rows_for_style(self, style_id: uuid.UUID, *,
                             colour: str | None = None,
                             statuses: set | None = None) -> list:
        stmt = select(CuttingRow).where(CuttingRow.style_id == style_id)
        if colour:
            stmt = stmt.where(func.upper(CuttingRow.colour) == colour.strip().upper())
        if statuses:
            stmt = stmt.where(CuttingRow.status.in_(tuple(statuses)))
        # Size then serial: the grid reads as the Excel did, grouped by size.
        stmt = stmt.order_by(CuttingRow.size.asc(), CuttingRow.created_at.asc())
        return list((await self.db.execute(stmt)).scalars().all())

    # ── the pieces a grid is generated FROM ──────────────────────────────────
    async def uncut_pieces(self, style_id: uuid.UUID, *,
                           colour: str | None = None, limit: int | None = None):
        """Pieces of this style that have no cutting row yet, with their SKU.

        NOT "pieces with no LEATHER_CUTTING event": a row is generated BEFORE the
        cut, so the absence of a row is the right test. A piece whose row was
        cancelled becomes eligible again, which is what cancelling is for.

        Returns [(piece, sku)] so the caller has the size and colour without a
        second query per row.
        """
        stmt = (select(Piece, SKU)
                .join(SKU, SKU.id == Piece.sku_id)
                .outerjoin(CuttingRow, CuttingRow.piece_id == Piece.id)
                .where(SKU.style_id == style_id,
                       Piece.is_active.is_(True),
                       CuttingRow.id.is_(None)))
        if colour:
            c = colour.strip().upper()
            stmt = stmt.where(func.upper(func.coalesce(
                SKU.color_name, SKU.color_code)) == c)
        stmt = stmt.order_by(SKU.size.asc(), Piece.seq.asc())
        if limit:
            stmt = stmt.limit(limit)
        return list((await self.db.execute(stmt)).all())

    async def get_style(self, style_id: uuid.UUID) -> Style | None:
        return await self.db.get(Style, style_id)

    async def get_sku(self, sku_id: uuid.UUID) -> SKU | None:
        return await self.db.get(SKU, sku_id)

    async def colours_for_style(self, style_id: uuid.UUID) -> list:
        """The colourways this style was ordered in — fills the grid's picker."""
        res = await self.db.execute(
            select(func.coalesce(SKU.color_name, SKU.color_code))
            .where(SKU.style_id == style_id).distinct())
        return sorted({c for (c,) in res.all() if c})

    async def open_row_count(self, style_id: uuid.UUID) -> int:
        return int(await self.db.scalar(
            select(func.count()).select_from(CuttingRow)
            .where(CuttingRow.style_id == style_id,
                   CuttingRow.status == CuttingRowStatus.DRAFT.value)) or 0)
