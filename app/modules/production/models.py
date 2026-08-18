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
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, ForeignKey, Integer, Numeric, String, UniqueConstraint,
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


class ProductionEvent(Base, UUIDMixin, TimestampMixin):
    """THE central table. One worker, one operation, ONE PIECE, one day, qty=1."""
    __tablename__ = "production_event"
    sku_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("sku.id"), index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("operation.id"), index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("employee.id"), index=True)
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

    operation: Mapped["Operation"] = relationship()
    piece: Mapped["Piece | None"] = relationship()