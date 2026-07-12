"""Production: operations, pieces, the event log, and the manager->operation map.

KEY DESIGN POINTS (updated for PER-PIECE tracking):
1. A Piece is a single physical garment, minted at CUTTING, carrying a unique,
   human-typeable `code` — the "bundle id" printed on its traveler card. A
   manager types this code at each subsequent stage.
2. production_event is still the finest grain, but qty is now always 1 and each
   tracked event references the Piece it acted on. Because sum(qty) == piece
   count, EVERY existing aggregate (wages, style progress, freight risk) keeps
   working with no SQL change.
3. Per-piece REPLACES the old "pieces don't conserve" tolerance. A scan must
   reference a piece that already exists, so a downstream stage can never exceed
   the count minted at cutting; the stage-spread is now true WIP-in-flight, not
   miscount noise. REWORK IS PERMISSIVE: a piece may be scanned at the same
   operation more than once (fails QC, goes back a stage). We surface repeats as
   a metric, we do NOT reject them — hence NO unique(piece_id, operation_id).
4. operation_access maps which manager ROLE may enter which operation — config,
   not hardcoded, so you re-assign without a code change.
"""
import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models import GUID, TimestampMixin, UUIDMixin


class Operation(Base, UUIDMixin, TimestampMixin):
    """An ordered, named production step. CUTTING, FUSING, ... configurable."""
    __tablename__ = "operation"
    code: Mapped[str] = mapped_column(String(30), unique=True)   # CUTTING, FUSING, SHELL, LA, LS, FF
    label: Mapped[str] = mapped_column(String(60))
    sequence: Mapped[int] = mapped_column(Integer)              # ordering in the chain
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class OperationAccess(Base, UUIDMixin):
    """Which manager role may log which operation. Config table — edit freely."""
    __tablename__ = "operation_access"
    __table_args__ = (
        UniqueConstraint("role", "operation_id", name="uq_role_operation"),
    )
    role: Mapped[str] = mapped_column(String(40), index=True)   # matches Role enum value
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
    """A single physical garment tracked through every stage.

    Minted at CUTTING (each cut piece = one row + one CUTTING event). `code` is
    the unique, human-typeable id on the traveler card — what your spec calls the
    "bundle id". `current_operation_id` is a denormalised convenience for the live
    'where is this piece' / feed views; the event log remains the source of truth.
    """
    __tablename__ = "piece"
    # Stored normalised (upper/trimmed) so a manually typed lookup always matches.
    code: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("sku.id"), index=True)
    current_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("operation.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductionEvent(Base, UUIDMixin, TimestampMixin):
    """THE central table. One worker, one operation, ONE PIECE, one day, qty=1."""
    __tablename__ = "production_event"
    sku_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("sku.id"), index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("operation.id"), index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("employee.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    qty: Mapped[int] = mapped_column(Integer, default=1)   # always 1 for a piece event
    # Who keyed it in (the manager). Audit trail.
    entered_by: Mapped[str | None] = mapped_column(String(120))
    # Per-piece linkage. Nullable so legacy / aggregate events still validate.
    piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id"), index=True
    )
    # DEPRECATED free-text bundle string. Kept only to backfill piece_id, then drop.
    bundle_ref: Mapped[str | None] = mapped_column(String(60), index=True)

    operation: Mapped["Operation"] = relationship()
    piece: Mapped["Piece | None"] = relationship()