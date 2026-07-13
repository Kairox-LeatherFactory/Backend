"""Production: operations, pieces, the event log, and the manager->operation map.

PER-PIECE MODEL
1. A Piece is one physical garment. Its identity is (sku_id, seq): piece `seq`
   of that SKU — e.g. size-M piece 5 of 21. seq runs 1..N within the SKU, so
   `UniqueConstraint(sku_id, seq)` is the authoritative integrity rule.
2. `code` is the fully-qualified, globally-unique string printed on the traveler
   card: {ORDER}-{STYLE}-{sku.code}-{seq}  e.g. KJ2451-CLERMONT-57-M-005.
   It carries the SKU identity AND the piece number through every stage.
3. Pieces are minted at CUTTING (N Piece rows + N CUTTING events, qty=1). Every
   later stage logs a qty=1 event against an EXISTING piece, so a downstream
   stage can never exceed the count cut. sum(qty) == piece count, so wages /
   progress / freight-risk are unchanged.
4. REWORK is permissive: a piece may be logged at the same operation more than
   once (fails QC, goes back, returns). It keeps its seq and code; repeats are
   surfaced as a metric, never rejected. Hence NO unique(piece_id, operation_id).
5. There is NO bundle_ref. The piece IS the tracked unit.
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
    code: Mapped[str] = mapped_column(String(30), unique=True)
    label: Mapped[str] = mapped_column(String(60))
    sequence: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class OperationAccess(Base, UUIDMixin):
    """Which manager role may log which operation. Config table — edit freely."""
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
    # Globally-unique printed/scannable string, stored normalised (upper/trimmed).
    code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # 1..N within the SKU — the "5th piece of size M". Typed at scan time.
    seq: Mapped[int] = mapped_column(Integer)
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
    qty: Mapped[int] = mapped_column(Integer, default=1)     # always 1 for a piece event
    entered_by: Mapped[str | None] = mapped_column(String(120))  # the manager who keyed it
    piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id"), index=True
    )

    operation: Mapped["Operation"] = relationship()
    piece: Mapped["Piece | None"] = relationship()