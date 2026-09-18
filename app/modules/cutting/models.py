"""
================================================================================
modules/cutting/models.py — the pre-cutting record, sheet by sheet
================================================================================
THE PROBLEM THIS TABLE EXISTS FOR

    Production V1 starts recording at the END of cutting: the bundle is already
    cut, and the cutting manager types one total dcm into the log. Everything
    before that — which hides arrived, what each measured, who they went to, the
    sheet the cutter handed back, the extra one he asked for — lived in Kumar's
    Excel and nowhere else. So the ERP could not answer the one question the whole
    material ledger is for: how much leather did this garment actually take?

    `cutting_row` is that Excel sheet, one row per garment, inside the ERP.

ONE ROW IS ONE GARMENT, and that is the factory's own unit.
    Not a bundle, not a batch, not a size-run: one jacket, cut by one named cutter
    from 7-12 named hides. That is why `piece_id` is the spine of the row — the
    row IS the piece's cutting record, and the Production Logger finds it by
    scanning the piece barcode that is already printed on the garment. No new
    "bundle barcode" is minted, because the piece code already identifies exactly
    the thing the row describes.

WHY THE SHEETS ARE NOT A COLUMN HERE
    They hang off `material_sheet.cutting_row_id`, pointing this way. Two reasons,
    and both are load-bearing:
      · a hide is a STOCK item first and a row member second — it exists before
        any row claims it and survives the row being deleted (SET NULL), so the
        stock side has to own it;
      · the FK makes "one hide on two rows" unrepresentable rather than merely
        forbidden. A sheets-list column on the row could hold the same hide twice.

    So `total_dcm` is DERIVED — the sum of the row's sheets — and never typed.
    The Excel had a hand-maintained total column; a typed total is a number that
    can disagree with its own parts, which is the class of bug the breakdown
    importer's reconciliation exists to catch.

WHY ITS OWN MODULE AND NOT `production`
    A ProductionEvent is a fact about work that HAS HAPPENED — immutable, one per
    piece per operation, the thing wages are paid from. A cutting row is a PLAN
    being edited: cells change, sheets come and go, and it is wrong until the
    cutter says it is right. Putting a mutable draft in the events table would put
    an editable row in the ledger everything else trusts.
================================================================================
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import GUID, TimestampMixin, UUIDMixin


class CuttingRow(Base, UUIDMixin, TimestampMixin):
    """One garment's cutting record: who cuts it, from which hides, and how much.

    THE STATUS IS A HARD, AUDITED TRANSITION, not a boolean (CLAUDE.md §15).
    DRAFT is freely editable and means nothing has been promised. APPROVED is the
    freeze line: the manager has seen the hides, the cutter has confirmed the
    count, and from here the numbers are what the Production Logger will spend.
    LOGGED means the cutting event exists and the hides are gone.
    """
    __tablename__ = "cutting_row"
    __table_args__ = (
        # ONE OPEN ROW PER GARMENT. A piece cut twice is rework and re-opens the
        # same row; it never gets a second one, or two rows would each claim
        # hides for the same jacket and the ledger would double-count it.
        UniqueConstraint("piece_id", name="uq_cutting_row_piece"),
        # The grid's own query: this style+colour, newest first.
        Index("ix_cutting_row_style_status", "style_id", "status"),
    )

    # SET NULL, like production_event.piece_id: a cutting record that physically
    # happened must survive the administrative deletion of a mis-imported piece.
    # The snapshot columns below keep the orphaned row readable.
    piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id", ondelete="SET NULL"),
        nullable=True, index=True)
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("style.id", ondelete="SET NULL"),
        nullable=True, index=True)
    sku_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("sku.id", ondelete="SET NULL"),
        nullable=True, index=True)

    # THE SNAPSHOT — the Excel's own columns, and the reason an orphaned row is
    # still readable. article/colour are also PATCHABLE on the lot they came from
    # (MaterialService.update_lot), so reading them back through a FK would let an
    # edit today rewrite what was cut last month. Same rule as
    # piece_material_issue's snapshot.
    article: Mapped[str | None] = mapped_column(String(120))
    colour: Mapped[str | None] = mapped_column(String(80))
    size: Mapped[str | None] = mapped_column(String(40))
    rc_no: Mapped[str | None] = mapped_column(String(40))   # rate-card number

    cutter_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("employee.id", ondelete="SET NULL"),
        nullable=True, index=True)
    work_date: Mapped[date | None] = mapped_column(Date, index=True)

    status: Mapped[str] = mapped_column(
        String(20), index=True, default="DRAFT", server_default="DRAFT")

    # WHAT THE ALLOCATOR AIMED AT, kept beside what it actually got. Without it
    # nobody can tell a row that came out at 9 sheets because the garment needed
    # 9 from one that came out at 9 because the allocator was told 400 dcm and
    # the hides happened to be large. `target_source` says whose number it was —
    # "style_spec" (a measurement someone signed off) or "size_baseline" (this
    # system's estimate). See core/leather_norms.py.
    target_dcm: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    target_source: Mapped[str | None] = mapped_column(String(20))

    # DERIVED from the row's sheets and frozen at approval. Nullable while DRAFT
    # precisely so it cannot be mistaken for a promise before one is made.
    total_dcm: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    # actor_user_id semantics: this is an app_user.id (the LOGIN), never the
    # scanned employee.id — AuditLog.actor_user_id FKs to app_user and passing a
    # worker's id here is what 500'd the store scan. The WORKER is
    # cutter_employee_id above; the two are different people and different tables.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    logged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(300))
