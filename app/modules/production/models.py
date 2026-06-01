"""Production: operations, the event log, and the manager->operation mapping.

KEY DESIGN POINTS:
1. production_event is the finest grain: one manager records that one employee
   did N pieces of one operation on one SKU on one day.
2. Events DO NOT reference the previous stage. Pieces are not assumed to conserve
   across operations (your Carnaby card: Cutting 152, Pasting 155). The spread
   between stages is a *metric we surface*, never an error we reject.
3. operation_access maps which manager ROLE may enter which operation — config,
   not hardcoded, so you re-assign without a code change.
"""
import uuid
from datetime import date
from sqlalchemy import String, Integer, ForeignKey, Date, UniqueConstraint, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.core.models import UUIDMixin, TimestampMixin, GUID


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


class ProductionEvent(Base, UUIDMixin, TimestampMixin):
    """THE central table. One worker, one operation, one SKU, one day, a quantity."""
    __tablename__ = "production_event"
    sku_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("sku.id"), index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("operation.id"), index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("employee.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    qty: Mapped[int] = mapped_column(Integer)
    # Who keyed it in (the manager). Audit trail.
    entered_by: Mapped[str | None] = mapped_column(String(120))
    # Optional bundle linkage — present only if you use bundle tickets.
    bundle_ref: Mapped[str | None] = mapped_column(String(60), index=True)
    operation: Mapped["Operation"] = relationship()
