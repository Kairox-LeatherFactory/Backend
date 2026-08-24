"""
================================================================================
modules/materials/style_spec_repository.py — data access for the per-piece recipe
================================================================================
Two tables, one repository (CLAUDE.md §15: repository = ALL db access):

    style_material_spec   the recipe   — what a garment of this style takes
    piece_material_issue  the ledger   — what was actually spent on this garment

EVERY MULTI-PIECE READ IS BATCHED. These rows are read on the two hottest
surfaces in the app — /barcode/resolve (every scan) and /production/log (up to
40 pieces per scan) — so a per-piece query here is an N+1 on the floor's
critical path. `lines_for_styles`, `issued_by_pieces` and `has_accessory_lines`
all take a list and return a dict for exactly that reason.
================================================================================
"""
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MaterialIssueSource
from app.modules.barcode.models import PieceMaterialIssue, StyleMaterialSpec

# The recipe line's identity, and deliberately the same six columns as
# MaterialRepository.LOT_KEY — plus the two that say WHOSE recipe it is. A line
# resolves to a lot by matching the six across the two tables, so the tuples
# must stay in step.
SPEC_KEY = ("category", "subtype", "article", "colour", "thickness", "size")


class StyleSpecRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── the recipe ───────────────────────────────────────────────────────────
    async def lines_for_style(self, style_id: uuid.UUID, *,
                              active_only: bool = True) -> list[StyleMaterialSpec]:
        stmt = select(StyleMaterialSpec).where(
            StyleMaterialSpec.style_id == style_id)
        if active_only:
            stmt = stmt.where(StyleMaterialSpec.is_active.is_(True))
        res = await self.db.execute(
            stmt.order_by(StyleMaterialSpec.category,
                          StyleMaterialSpec.subtype,
                          StyleMaterialSpec.article))
        return list(res.scalars())

    async def lines_for_styles(self, style_ids: list[uuid.UUID]) -> dict:
        """{style_id: [active line, ...]} for many styles in ONE query."""
        if not style_ids:
            return {}
        res = await self.db.execute(
            select(StyleMaterialSpec).where(
                StyleMaterialSpec.style_id.in_(style_ids),
                StyleMaterialSpec.is_active.is_(True)))
        out: dict = {}
        for line in res.scalars():
            out.setdefault(line.style_id, []).append(line)
        return out

    async def get_line(self, line_id: uuid.UUID) -> StyleMaterialSpec | None:
        return await self.db.get(StyleMaterialSpec, line_id)

    async def find_duplicate_line(self, *, style_id: uuid.UUID,
                                  sku_id: uuid.UUID | None, category: str,
                                  subtype: str | None, article: str,
                                  colour: str | None, thickness: str | None,
                                  size: str | None):
        """The existing ACTIVE line with this exact identity, or None.

        THIS, NOT THE UNIQUE CONSTRAINT, IS THE DEDUPE. Four of the eight identity
        columns are nullable, and NULLs compare DISTINCT inside a unique index on
        both Postgres and SQLite — so two lines that both leave colour blank do
        not collide there even though they are plainly the same material. The `is`
        comparison below renders IS NULL, which is the semantics the recipe wants.

        Same reasoning, same shape as MaterialRepository.find_duplicate_lot; if
        one of them ever grows a rule, the other needs it too.
        """
        stmt = select(StyleMaterialSpec).where(
            StyleMaterialSpec.is_active.is_(True),
            StyleMaterialSpec.style_id == style_id,
            StyleMaterialSpec.category == (category or "").upper(),
            StyleMaterialSpec.article == article,
        )
        for col, val in ((StyleMaterialSpec.sku_id, sku_id),
                         (StyleMaterialSpec.subtype, subtype),
                         (StyleMaterialSpec.colour, colour),
                         (StyleMaterialSpec.thickness, thickness),
                         (StyleMaterialSpec.size, size)):
            stmt = stmt.where(col.is_(None) if val is None else col == val)
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none()

    def add_line_nocommit(self, **kw) -> StyleMaterialSpec:
        line = StyleMaterialSpec(**kw)
        self.db.add(line)
        return line

    async def has_accessory_lines(self, style_ids: list[uuid.UUID]) -> dict:
        """{style_id: bool} — does this style declare any ACCESSORY at all?

        The release gate asks it for a whole batch of styles, and the drawer
        completeness predicate asks it for a whole page of drawers, so it is a
        single grouped count rather than a per-style EXISTS.
        """
        if not style_ids:
            return {}
        rows = await self.db.execute(
            select(StyleMaterialSpec.style_id, func.count())
            .where(StyleMaterialSpec.style_id.in_(style_ids),
                   StyleMaterialSpec.is_active.is_(True),
                   StyleMaterialSpec.category == "ACCESSORY")
            .group_by(StyleMaterialSpec.style_id))
        found = {sid: int(n or 0) > 0 for sid, n in rows.all()}
        return {sid: found.get(sid, False) for sid in style_ids}

    @staticmethod
    def kit_required_sql(style_id_col):
        """Correlated EXISTS: does the style in `style_id_col` declare accessories?

        The SQL half of the two-shape pattern core/lining_rules.py established —
        a Python resolver for one object, a SQL expression for a page of them, side
        by side so they cannot answer differently. Used by the drawer list, which
        renders up to 2000 rows and must not run a Python resolver per row.
        """
        return (select(StyleMaterialSpec.id)
                .where(StyleMaterialSpec.style_id == style_id_col,
                       StyleMaterialSpec.is_active.is_(True),
                       StyleMaterialSpec.category == "ACCESSORY")
                .exists())

    # ── the ledger ───────────────────────────────────────────────────────────
    def add_issue_nocommit(self, **kw) -> PieceMaterialIssue:
        row = PieceMaterialIssue(**kw)
        self.db.add(row)
        return row

    async def issued_by_piece(self, piece_id: uuid.UUID, *,
                              source: str | None = None) -> dict:
        """{spec_line_id: Σ qty already issued} for one piece.

        THE IDEMPOTENCY READ. `outstanding = qty_per_piece − issued[line.id]`, so
        a re-scan of a fully-issued drawer computes zero everywhere and spends
        nothing. It sums rather than checking existence because a line can be
        topped up (a partial issue, then the rest), and "issued 3 of 4" and
        "issued 4 of 4" must not look the same.

        Rows with a NULL spec id (MANUAL corrections) are excluded — they are
        deliberately repeatable and are not what the kit is reconciling against.
        """
        stmt = (select(PieceMaterialIssue.spec_line_id,
                       func.coalesce(func.sum(PieceMaterialIssue.qty), 0))
                .where(PieceMaterialIssue.piece_id == piece_id,
                       PieceMaterialIssue.spec_line_id.is_not(None))
                .group_by(PieceMaterialIssue.spec_line_id))
        if source:
            stmt = stmt.where(PieceMaterialIssue.source == source)
        rows = await self.db.execute(stmt)
        return {sid: Decimal(str(total or 0)) for sid, total in rows.all()}

    async def issued_by_pieces(self, piece_ids: list[uuid.UUID]) -> dict:
        """{piece_id: {spec_id: Σ qty}} for many pieces in ONE query.

        /production/log resolves a kit status for every piece in the batch; one
        query per piece would be 40 round trips on a single scan.
        """
        if not piece_ids:
            return {}
        rows = await self.db.execute(
            select(PieceMaterialIssue.piece_id,
                   PieceMaterialIssue.spec_line_id,
                   func.coalesce(func.sum(PieceMaterialIssue.qty), 0))
            .where(PieceMaterialIssue.piece_id.in_(piece_ids),
                   PieceMaterialIssue.spec_line_id.is_not(None))
            .group_by(PieceMaterialIssue.piece_id,
                      PieceMaterialIssue.spec_line_id))
        out: dict = {}
        for pid, sid, total in rows.all():
            out.setdefault(pid, {})[sid] = Decimal(str(total or 0))
        return out

    async def issues_for_piece(self, piece_id: uuid.UUID) -> list[PieceMaterialIssue]:
        """Every issue row for a piece, newest last — the piece's material story."""
        res = await self.db.execute(
            select(PieceMaterialIssue)
            .where(PieceMaterialIssue.piece_id == piece_id)
            .order_by(PieceMaterialIssue.issued_at))
        return list(res.scalars())

    async def find_issue(self, piece_id: uuid.UUID,
                         spec_id: uuid.UUID) -> PieceMaterialIssue | None:
        """The one row the unique constraint permits, so a top-up UPDATES it."""
        return (await self.db.execute(
            select(PieceMaterialIssue).where(
                PieceMaterialIssue.piece_id == piece_id,
                PieceMaterialIssue.spec_line_id == spec_id).limit(1)
        )).scalar_one_or_none()
