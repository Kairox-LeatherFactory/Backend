"""Production: operations, pieces, the event log, and the manager->operation map.

PER-PIECE MODEL
1. A Piece is one physical garment. Its identity is (sku_id, seq): piece `seq`
   of that SKU — e.g. size-M piece 5 of 21. seq runs 1..N within the SKU, so
   `UniqueConstraint(sku_id, seq)` is the authoritative integrity rule.
2. `code` is the fully-qualified, globally-unique string printed on the traveler
   card. It BECOMES the parent barcode: STYLE-COLOUR-SIZE-seq.
3. Pieces are minted at BREAKDOWN UPLOAD now (imports/premint.py), not at
   cutting. Cutting is a scan-and-log stage like every other stage.
4. REWORK is permissive: a piece may be logged at the same operation more than
   once. It keeps its seq and code; repeats are surfaced as a metric.
5. There is NO bundle_ref. The piece IS the tracked unit.

BARCODE-FEATURE ADDITIONS (this build)
- Piece.needs_lining      — from the breakdown material column; drives the merge
                            (completeness) gate. Leather-only pieces skip it.
- Piece.drawer_id         — the drawer this piece is merged to at upload.
- ProductionEvent.{leather_lot_id, lining_lot_id, consumption_qty}
                          — captured ONLY at a cut stage; null everywhere else.
                            The lot link lives on the EVENT (the act of cutting),
                            never on the Piece — the frozen contract decision.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, Date, ForeignKey, Index, Integer, Numeric, String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models import GUID, TimestampMixin, UUIDMixin


class Operation(Base, UUIDMixin, TimestampMixin):
    """An ordered, named production step. CUTTING, FUSING, ... configurable."""
    __tablename__ = "operation"
    code: Mapped[str] = mapped_column(String(30), unique=True)
    label: Mapped[str] = mapped_column(String(60))
    sequence: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class OperationAccess(Base, UUIDMixin):
    """Which manager role may log which operation. Config table — edit freely.

    NOTE: this DB table remains the runtime override the service consults after
    the STAGE_ROLE_ACCESS enum default (an MD can grant an exception without a
    deploy). The enum is the default; this table is the escape hatch.
    """
    __tablename__ = "operation_access"
    __table_args__ = (
        UniqueConstraint("role", "operation_id", name="uq_role_operation"),
    )
    role: Mapped[str] = mapped_column(String(40), index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("operation.id"))


class StyleOperation(Base, UUIDMixin):
    """Which operations a given style actually uses (a vest skips some)."""
    __tablename__ = "style_operation"
    __table_args__ = (
        UniqueConstraint("style_id", "operation_id", name="uq_style_operation"),
    )
    style_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("style.id"), index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("operation.id"))


class Piece(Base, UUIDMixin, TimestampMixin):
    """One physical garment. Identity = (sku_id, seq); code is the printed id."""
    __tablename__ = "piece"
    __table_args__ = (
        UniqueConstraint("sku_id", "seq", name="uq_piece_sku_seq"),
    )
    code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    sku_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("sku.id"), index=True)
    current_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("operation.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── barcode-feature columns ──────────────────────────────────────────────
    # Does this piece need a lining at all? Read from the breakdown material
    # column at upload. Leather-only pieces skip the completeness (merge) gate.
    needs_lining: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1")
    # The drawer this piece is merged to (assigned at upload). One piece = one
    # drawer; the FK on the piece makes "which drawer holds this piece" a single
    # indexed read. FK target created by the barcode migration (drawer table).
    #
    # ondelete="SET NULL" — THIS IS ONE HALF OF THE piece↔drawer CYCLE.
    # `drawer.current_piece_id` points back here, so without a delete rule the
    # two rows are mutually undeletable: Postgres refuses to remove the drawer
    # (a piece still references it) and refuses to remove the piece (a drawer
    # still references it), with no order that resolves. SET NULL is not a
    # concession — it is already a legal, handled state: a piece with
    # `drawer_id IS NULL` is the "waiting for a drawer" case the pool allocator
    # and analytics both speak about (see imports/premint.allocate_waiting_pieces
    # and the `no_drawer` store bucket). Deleting a drawer returns its piece to
    # that waiting list rather than deleting the garment's record.
    drawer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("drawer.id", ondelete="SET NULL"),
        nullable=True, index=True)

    # ── THE STORE, ON THE GARMENT ────────────────────────────────────────────
    # These five columns are the old Drawer's state, moved to the thing it was
    # always describing. A drawer never had a property of its own worth keeping:
    # "holding leather", "kit issued", "received", "sent" are all facts about the
    # GARMENT, and routing them through a numbered box only added a 200-slot
    # bottleneck that the DM had to re-allocate by hand.
    #
    # `drawer_id` above is retained and no longer written, so the historical link
    # stays readable for audit while nothing new depends on it.
    store_state: Mapped[str] = mapped_column(
        String(20), default="waiting", server_default="waiting", index=True)
    # The three buckets. They survive RECEIVED and SENDED — a released garment
    # still HOLDS its parts, and `store_state` has stopped saying so — which is
    # why contents are three booleans and not derived from the state.
    leather_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0")
    lining_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0")
    accessories_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0")
    store_entered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    store_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    store_sended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))


class ProductionEvent(Base, UUIDMixin, TimestampMixin):
    """THE central table. One worker, one operation, ONE PIECE, one day, qty=1."""
    __tablename__ = "production_event"
    sku_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("sku.id"), index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("operation.id"), index=True)
    # NULLABLE SINCE JOB WORK. A stage performed by an outside factory has no
    # employee — the vendor did it — and forcing a name here would credit one of
    # our workers with somebody else's work AND generate a wage line for it. The
    # wage query inner-joins Employee, so a NULL simply drops out of payroll,
    # which is exactly right: nobody on our books earned it.
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("employee.id", ondelete="SET NULL"),
        nullable=True, index=True)
    # Set instead of employee_id when an outside factory did the work.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("vendor.id", ondelete="SET NULL"),
        nullable=True, index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    qty: Mapped[int] = mapped_column(Integer, default=1)     # always 1 for a piece event
    entered_by: Mapped[str | None] = mapped_column(String(120))
    piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id", ondelete="SET NULL"), index=True
    )

    # ── barcode-feature columns (cut stages only; null elsewhere) ────────────
    leather_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("material_lot.id", ondelete="SET NULL"), nullable=True, index=True)
    lining_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("material_lot.id", ondelete="SET NULL"), nullable=True, index=True)
    # dcm for leather, mtrs for lining — the lot's uom disambiguates.
    consumption_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    # WAS THIS EVENT A REDO? Set when the piece was sent back by an approved
    # rejection and the stage is being performed again.
    #
    # IT EXISTS TO SPLIT THE COST. "This order cost X, of which Y was rework" is
    # not answerable from a single consumption column: the second cut of a
    # re-made panel is real leather spent, but it is not what the garment was
    # supposed to cost, and averaging the two hides how much the floor is
    # actually losing to defects. Summing consumption_qty grouped by this flag
    # gives both numbers from one table.
    is_rework: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", index=True)

    operation: Mapped["Operation"] = relationship()
    piece: Mapped["Piece | None"] = relationship()


class PieceInspection(Base, UUIDMixin, TimestampMixin):
    """A garment rejected at a stage, and who is answerable for it.

    THE GAP THIS CLOSES. A piece completed at FUSING was rejected during PASTING
    and there was no way to send it back: the sequence gate considers FUSING done
    for good, re-logging it is refused as "rework" and writes nothing, and the
    only route was an edit in the database. So defects were handled by telling
    somebody, and nothing was ever counted.

    TWO STEPS, BECAUSE SENDING A GARMENT BACKWARDS IS NOT A FLOOR DECISION.
    Anyone who stands at a stage can SEE a defect, so any manager or HR may raise
    one. But moving a piece back re-opens a completed stage, re-orders work and
    can cost material, so the DM approves before it actually moves. Until then
    the rejection is a report.

    WHO IS RESPONSIBLE IS A COLUMN, NOT A SENTENCE — and that is the factory's
    own requirement: "if any employee not properly cut, or any stage not properly
    done their work, because of that the product is damaged, then that employee is
    responsible for that piece". A free-text reason cannot be counted across a
    month, cannot be produced in a wage conversation and cannot separate a bad
    hide (the supplier's problem) from bad work (a training or pay problem). So
    the defect carries a TYPE and, when it is workmanship, the employee and the
    stage they did.
    """
    __tablename__ = "piece_inspection"
    __table_args__ = (
        Index("ix_piece_inspection_piece_status", "piece_id", "status"),
    )

    # SET NULL like production_event.piece_id: an inspection that happened must
    # survive the administrative deletion of a mis-imported piece.
    piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id", ondelete="SET NULL"),
        nullable=True, index=True)

    # WHERE THE DEFECT WAS FOUND — not where it was caused. A bad fuse is found
    # at pasting; keeping the two apart is what makes "which stage causes most
    # rework" answerable.
    found_at_stage: Mapped[str] = mapped_column(String(30), index=True)
    verdict: Mapped[str] = mapped_column(String(10))          # PASS | REJECT
    action: Mapped[str | None] = mapped_column(String(10))    # FIX | REDO
    # Where it goes back to, on a REDO. The rejector chooses it from the stages
    # the piece has already passed — a ruined panel goes to cutting, not merely
    # one stage back.
    return_to_stage: Mapped[str | None] = mapped_column(String(30))

    defect_type: Mapped[str | None] = mapped_column(String(20), index=True)
    # Answerable for this piece. NULL for PRODUCT_DAMAGE, which is nobody's
    # fault on the floor.
    responsible_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("employee.id", ondelete="SET NULL"),
        nullable=True, index=True)
    # Which stage they were doing when it went wrong — the stage that CAUSED it.
    responsible_stage: Mapped[str | None] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(String(500))

    status: Mapped[str] = mapped_column(
        String(20), index=True, default="PENDING", server_default="PENDING")

    # app_user ids — the LOGIN, never the scanned employee.id.
    raised_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    raised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(500))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── CROSS-MODULE FK RESOLUTION ───────────────────────────────────────────────
# `production_event.vendor_id` names a table defined in app.modules.jobwork.
# SQLAlchemy resolves FK target strings against the SHARED MetaData when mappers
# are configured, so `vendor` has to be REGISTERED by the time anything maps
# these classes — not merely importable. Without this line, any module that
# imports production.models WITHOUT also importing jobwork.models dies with
#
#     NoReferencedTableError: Foreign key associated with column
#     'production_event.vendor_id' could not find table 'vendor'
#
# which is what tests/unit/test_fk_delete_rules.py hits: it imports the model
# modules directly and never touches main.py's import block.
#
# The import is at the BOTTOM and one-directional — jobwork.models imports
# nothing from here — so the module graph stays acyclic. Same reason
# barcode/models.py imports cutting.models: a table SQLAlchemy cannot see is a
# table Alembic will try to DROP (CLAUDE.md §11).
from app.modules.jobwork import models as _jobwork_models  # noqa: E402,F401
