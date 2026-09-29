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
critical path. `lines_for_styles` and `issued_by_pieces` both take a list and
return a dict for exactly that reason. (`has_accessory_lines` was a third; see the
note further down for why it and `kit_required_sql` were retired rather than fixed.)
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
                                  size: str | None,
                                  garment_size: str | None = None,
                                  active_only: bool = True):
        """The existing ACTIVE line with this exact identity, or None.

        THIS, NOT THE UNIQUE CONSTRAINT, IS THE DEDUPE. Four of the eight identity
        columns are nullable, and NULLs compare DISTINCT inside a unique index on
        both Postgres and SQLite — so two lines that both leave colour blank do
        not collide there even though they are plainly the same material. The `is`
        comparison below renders IS NULL, which is the semantics the recipe wants.

        Same reasoning, same shape as MaterialRepository.find_duplicate_lot; if
        one of them ever grows a rule, the other needs it too.

        `garment_size` IS PART OF THE IDENTITY, and leaving it out was a bug. The DB
        constraint uq_style_material_spec_line includes it and so does
        replace_spec's identity tuple — so without it "Thread for L" and "Thread for
        M" were ONE duplicate to add_line and TWO rows to the whole-grid save, and
        the two doors disagreed about whether the second line was allowed to exist.

        `active_only=False` FINDS A DEACTIVATED ROW TOO, which the fan-out needs: a
        line somebody removed still occupies the unique constraint, so re-adding it
        has to revive that row rather than collide with one nothing can see.
        """
        stmt = select(StyleMaterialSpec).where(
            StyleMaterialSpec.style_id == style_id,
            StyleMaterialSpec.category == (category or "").upper(),
            StyleMaterialSpec.article == article,
        )
        if active_only:
            stmt = stmt.where(StyleMaterialSpec.is_active.is_(True))
        for col, val in ((StyleMaterialSpec.sku_id, sku_id),
                         (StyleMaterialSpec.subtype, subtype),
                         (StyleMaterialSpec.colour, colour),
                         (StyleMaterialSpec.thickness, thickness),
                         (StyleMaterialSpec.size, size),
                         (StyleMaterialSpec.garment_size, garment_size)):
            stmt = stmt.where(col.is_(None) if val is None else col == val)
        # Prefer an ACTIVE row when both exist, so a revived duplicate never hides
        # the live one.
        stmt = stmt.order_by(StyleMaterialSpec.is_active.desc())
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none()

    def add_line_nocommit(self, **kw) -> StyleMaterialSpec:
        line = StyleMaterialSpec(**kw)
        self.db.add(line)
        return line

    # ── RETIRED: the "two-shape pattern" for kit_required ────────────────────
    # `has_accessory_lines(style_ids)` and `kit_required_sql(style_id_col)` used to
    # live here as the SQL half of the pattern core/lining_rules.py established: a
    # Python resolver for one object, a SQL expression for a page of them, side by
    # side so they could not answer differently.
    #
    # THEY ALREADY ANSWERED DIFFERENTLY. Both asked "does this STYLE declare any
    # accessory", while the Python half `kit_required_for_piece` asks "does this SKU
    # declare any" — and with accessories now scoped per SKU the two diverge on every
    # style whose colourways differ. A style-level EXISTS would report a kit owed on
    # a garment that needs none, and none on one that does.
    #
    # AND NOTHING CALLED THEM. The drawer list they were written for is retired; the
    # release gate computes `has_accessory_lines` inline from the lines it already
    # holds. Their only callers were their own tests. A wrong answer that nothing
    # asks for is worth deleting rather than fixing, and leaving them here is an
    # invitation to write a page-sized read against the wrong scope.
    #
    # If a batched form is ever needed again, it must key on SKU:
    #   EXISTS (SELECT 1 FROM style_material_spec
    #            WHERE sku_id = <the piece's sku> AND is_active AND category='ACCESSORY')

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

    async def issue_rows_for_piece(self, piece_id):
        """Every material actually issued to one garment, newest last.

        THE LEDGER ITSELF, not the {line_id: sum} the idempotency read needs.
        `issued_by_piece` answers "how much of this line is done"; this answers
        "what is physically in the bag and where it came from" — which is the
        question an operator asks when they open a garment's record, and the one
        nothing could answer before.

        MANUAL rows are INCLUDED here and excluded from `issued_by_piece`, and
        the difference is the point: an off-spec correction is not something the
        checklist reconciles against, but it absolutely is something that was
        given to this garment.
        """
        from app.modules.barcode.models import PieceMaterialIssue
        res = await self.db.execute(
            select(PieceMaterialIssue)
            .where(PieceMaterialIssue.piece_id == piece_id)
            .order_by(PieceMaterialIssue.issued_at.asc()))
        return list(res.scalars())
