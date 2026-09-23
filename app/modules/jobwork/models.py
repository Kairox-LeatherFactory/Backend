"""
================================================================================
modules/jobwork/models.py — work sent outside the factory, and what it cost
================================================================================
WHY THIS EXISTS

    When a deadline is short, leather, lining or tailoring goes to an outside
    factory. Nothing in the system modelled that: the pieces simply stopped
    moving, nobody could say where they were, and the money paid for the work was
    not recorded anywhere. Every stage in ProductionStage assumed in-house.

THREE TABLES, AND WHY EACH ONE

    vendor         the outside factory. Deliberately thin — name, contact,
                   active — because the thing that varies is the RATE, and a
                   rate belongs to a job rather than to a vendor: the same
                   tailor charges differently for a bomber and a vest.

    job_work       one dispatch: these pieces, this stage, this vendor, out on
                   this date, expected back on that one. `rate_per_piece` is
                   OPTIONAL — some work is quoted per piece and some is settled
                   another way, and demanding a number nobody has would mean the
                   dispatch does not get recorded at all.

    job_work_piece which garments went, and which have come back. One row per
                   piece, because a dispatch of forty rarely returns as forty:
                   some come back short, some rejected, and "the job is back" is
                   not the same statement as "every piece is back".

WHAT THE RATE BUYS YOU
    cost = rate_per_piece x pieces returned. That is the number the factory asked
    for — "even when we give it to the outside factory we are paying for each
    piece, so we have to track that". It is computed from the ledger rather than
    stored, so a piece that never came back is never paid for.
================================================================================
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Index, Numeric, String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import GUID, TimestampMixin, UUIDMixin


class Vendor(Base, UUIDMixin, TimestampMixin):
    """An outside factory that does work for us.

    THIN ON PURPOSE. The tempting extra column is a rate — but the same tailor
    charges differently for a bomber and a vest, so a rate on the vendor would be
    wrong the first time it was used. It lives on the job.
    """
    __tablename__ = "vendor"
    __table_args__ = (UniqueConstraint("name", name="uq_vendor_name"),)

    name: Mapped[str] = mapped_column(String(200), index=True)
    contact: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1")


class JobWork(Base, UUIDMixin, TimestampMixin):
    """One dispatch of garments to one vendor for one stage."""
    __tablename__ = "job_work"
    __table_args__ = (
        Index("ix_job_work_vendor_status", "vendor_id", "status"),
    )

    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("vendor.id", ondelete="SET NULL"),
        nullable=True, index=True)
    # The stage the vendor is performing. A ProductionStage value — the work is
    # the same work, it is simply being done somewhere else, and recording it as
    # a different kind of thing would take those garments out of every existing
    # progress count.
    stage: Mapped[str] = mapped_column(String(30), index=True)

    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_back: Mapped[date | None] = mapped_column(Date, index=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(20), index=True, default="OUT", server_default="OUT")

    # OPTIONAL, and that is the point. Some work is quoted per piece and some is
    # settled another way; demanding a number nobody has yet would mean the
    # dispatch simply does not get recorded, which is the state this replaces.
    rate_per_piece: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))

    # app_user ids — the LOGIN, never a scanned employee.
    dispatched_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    received_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500))


class JobWorkPiece(Base, UUIDMixin, TimestampMixin):
    """One garment on one dispatch.

    ONE ROW PER PIECE, because a dispatch of forty rarely returns as forty. Some
    come back short, some rejected — and "the job is back" is a different
    statement from "every piece is back". Only the pieces that actually returned
    are paid for.
    """
    __tablename__ = "job_work_piece"
    __table_args__ = (
        # A garment cannot be on the same dispatch twice.
        UniqueConstraint("job_work_id", "piece_id", name="uq_job_work_piece"),
        Index("ix_job_work_piece_piece_status", "piece_id", "status"),
    )

    # NOT NULL and CASCADE: a dispatch line has no meaning without its
    # dispatch, so it is owned rather than referenced. (A nullable FK would also
    # have to declare SET NULL, which would leave orphan lines claiming pieces
    # are out at a job that no longer exists.)
    job_work_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("job_work.id", ondelete="CASCADE"), index=True)
    piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id", ondelete="SET NULL"),
        nullable=True, index=True)
    # OUT | BACK | SHORT | REJECTED
    status: Mapped[str] = mapped_column(
        String(20), index=True, default="OUT", server_default="OUT")
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(300))
