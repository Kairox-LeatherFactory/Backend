"""
================================================================================
modules/barcode/models.py — The barcode registry + the new physical entities
================================================================================

ONE REGISTRY, EVERY CODE.
    BarcodeRegistry is the single table every scan resolves through. Each printed
    code — a piece, a material lot, a drawer, an employee card — has exactly one
    row here, carrying its type, its status, and a nullable FK to the domain row
    it names. `resolve(code)` is therefore one indexed lookup, and there is one
    answer to "what is this code", never a guess.

WHY THE FK IS NULLABLE + POLYMORPHIC-BY-TYPE (not a hard relationship per type)
    A registry row points at exactly one of {piece, employee, drawer, material
    lot}. Modelling that as four nullable FKs keeps every link a real, indexed
    foreign key (so a deleted piece can't orphan a code) while letting one table
    serve all types. The `type` column says which FK is populated.

EMPLOYEE BARCODE IS EDITABLE; THE PIECE BARCODE IS NOT.
    A piece code is a permanent identity (it is Piece.code, minted at breakdown
    upload, printed on the garment). An employee code is a credential on a card:
    it can be reissued and it is RETIRED when the worker leaves. Retiring flips
    `status` to RETIRED — resolve() then returns 410 — and NEVER touches the
    employee row or any production/wage history. That is the whole point: you
    delete the scannable code, not the person.
================================================================================
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String,
    UniqueConstraint,Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models import GUID, JSON_VARIANT, TimestampMixin, UUIDMixin


class BarcodeRegistry(Base, UUIDMixin, TimestampMixin):
    """Every printed/scannable code. resolve() reads this and nothing else first."""
    __tablename__ = "barcode_registry"
    __table_args__ = (
        UniqueConstraint("code", name="uq_barcode_code"),
        Index("ix_barcode_order_type", "order_id", "type"),
    )
    code: Mapped[str] = mapped_column(String(120), index=True)   # normalised upper
    type: Mapped[str] = mapped_column(String(20), index=True)    # BarcodeType value
    status: Mapped[str] = mapped_column(String(15), index=True, default="active")

    # Exactly one of these is set, per `type`. All indexed, all real FKs.
    #
    # ondelete="SET NULL" — READ THIS BEFORE YOU DELETE A DOMAIN ROW.
    # Every nullable FK in this schema now carries SET NULL, so that a parent row
    # can actually be deleted instead of being pinned forever by its children.
    # On THIS table that rule is the least comfortable, because a registry row
    # exists in order to name a domain row: null its FK and you are left with an
    # ACTIVE code of type PIECE that names nothing.
    #
    # It is still the right rule here, for two reasons:
    #   • resolve() already guards every branch on the FK being present
    #     (`row.type == PIECE and row.piece_id`), so a widowed code degrades to
    #     "known code, no payload" — it does not crash the scan screen.
    #   • CASCADE is the alternative, and CASCADE would mean deleting a piece
    #     silently destroys its printed label's only record. That is the exact
    #     opposite of "you delete the scannable code, never the person or their
    #     record" (see the module docstring and CLAUDE.md §6).
    #
    # THE REAL POINT: deleting a piece / drawer / employee ROW is not a workflow
    # this system has. Workers are RETIRED (status → RETIRED, resolve → 410),
    # pieces are deactivated (`is_active`), drawers recycle to WAITING. These
    # delete rules exist for administrative cleanup — a mis-imported order, a
    # test batch — not for anything the floor does. If you do use them, sweep
    # `barcode_registry` for rows whose type-appropriate FK went NULL, because
    # they will otherwise keep inflating the per-order `minted` / `balance`
    # counts the factory reconciles against (see the is_alias note below).
    piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id", ondelete="SET NULL"), nullable=True, index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client_order.id", ondelete="SET NULL"), nullable=True, index=True)
    sku_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("sku.id", ondelete="SET NULL"), nullable=True, index=True)
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("style.id", ondelete="SET NULL"), nullable=True, index=True)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("employee.id", ondelete="SET NULL"), nullable=True, index=True)
    drawer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("drawer.id", ondelete="SET NULL"), nullable=True, index=True)
    material_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("material_lot.id", ondelete="SET NULL"), nullable=True, index=True)

    # Freeform caption for the label (STYLE · COLOUR · SIZE · #seq, etc.)
    caption: Mapped[str | None] = mapped_column(String(200))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(String(120))

    # ONE PIECE, TWO CODES — and only one of them is the label (bug #19).
    #
    # Since the compact code arrived, a piece has TWO active registry rows: the
    # small `PC-…` code that is printed and scanned (is_alias=False, the PRIMARY),
    # and its old long `KJ2451-CLERMONT-57-M-005` code, kept alive so labels
    # printed before the change still resolve (is_alias=True).
    #
    # WHY THIS IS A STORED FLAG AND NOT INFERRED FROM THE CODE PREFIX.
    # Every per-order count and the whole history list select `type=PIECE` rows.
    # With two rows per piece and no discriminator, `minted` doubles, `balance`
    # goes negative, the `duplicates` integrity proof reads as broken, and the
    # history table shows each garment twice. Those queries need to name the rule
    # they mean; "the code happens to start with PC-" is not that rule. An alias
    # also carries NO order/sku/style FK, so it stays out of those queries even
    # if one is ever written without the flag — belt and braces on a count the
    # factory reconciles against.
    is_alias: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", index=True)


class Drawer(Base, UUIDMixin, TimestampMixin):
    """A physical storage drawer. STATIC code, RECYCLING state.

    seq is the human number on the drawer; code is the scannable label. A drawer
    is merged to exactly one (sku_id, piece_seq) at a time, holds that piece's
    parts, and returns to WAITING after the piece ships — so `current_piece_id`
    moves over the drawer's life while `code` never changes.

    THE piece↔drawer CYCLE, AND WHY BOTH ENDS SURVIVE.
        `piece.drawer_id` and `drawer.current_piece_id` are the same 1:1 link
        read from opposite ends, and they are NOT interchangeable: the drawer's
        pointer is the LIVE CLAIM (a store scan moves state/leather_in/lining_in
        on the row that claims the piece; `release_nocommit` nulls it when the
        garment ships), while the piece's pointer is the piece's own assignment.
        Every card query in barcode/dashboard/production reads through the claim
        and every analytics store query reads through the assignment. So the
        cycle is deliberate — what was missing was a rule for what happens when
        one end is deleted. See the ondelete note below and on Piece.drawer_id.
    """
    __tablename__ = "drawer"
    __table_args__ = (
        UniqueConstraint("code", name="uq_drawer_code"),
    )
    code: Mapped[str] = mapped_column(String(60), index=True)     # "DRW-014"
    seq: Mapped[int] = mapped_column(Integer, index=True)
    state: Mapped[str] = mapped_column(String(20), default="waiting", index=True)
    # ondelete="SET NULL" — the other half of the cycle. Deleting a piece
    # releases the drawer instead of blocking the delete; a drawer with
    # `current_piece_id IS NULL` is the WAITING pool state the allocator already
    # hands out. Mutual SET NULL is legal in Postgres and is what makes either
    # row deletable in either order.
    #
    # use_alter=True — DDL ORDERING, NOT DELETE SEMANTICS, and it is on THIS side
    # on purpose. A cycle means create_all cannot topologically sort {piece,
    # drawer}; SQLAlchemy silently resolved it by moving EVERY foreign key on
    # BOTH tables out to ALTER TABLE ADD CONSTRAINT — and SQLite cannot execute
    # those, so on the test harness `piece` was created with no FKs at all
    # (sku_id and current_operation_id included). Marking this one back-pointer
    # as the alter-able edge lets the sort succeed: drawer is created first,
    # `piece` keeps all of its constraints inline, and the only FK SQLite loses
    # is this one. That is what keeps tests/integration/test_premint_insert_order
    # (which runs PRAGMA foreign_keys=ON) able to catch a bad insert order.
    current_piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("piece.id", ondelete="SET NULL", use_alter=True,
                   name="fk_drawer_current_piece_id_piece"),
        nullable=True, index=True)
    leather_in: Mapped[bool] = mapped_column(Boolean, default=False)
    lining_in: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NO `sent_to`. A drawer briefly carried one, on the assumption that a send
    # chose between a lining floor and a stitching floor. It does not: lining is
    # UPSTREAM of the store — the lining part is cut and then scanned INTO this
    # drawer — so a merged drawer has exactly one way forward, into
    # LINE_STITCHING. `state == SENDED` therefore already says everything a
    # destination column could, and a column whose only possible value is a
    # constant is a question the schema should not be asking.


class MaterialLot(Base, UUIDMixin, TimestampMixin):
    """A batch of stock. Creating a lot registers a child barcode AND adds stock.

    STOCK IS THREE NUMBERS, computed the same way inventory has always meant it:
        on_hand   — physically in the building
        reserved  — committed to a required quantity (can't be spent elsewhere)
        available — on_hand − reserved   (a derived read, not a stored column, so
                    the two can never drift; see MaterialService.stock)

    Category-specific attributes live in `attributes` (JSON) rather than 20 mostly
    -null columns: leather needs dcm+thickness, thread needs mtrs+thickness,
    buttons need size, 'other' needs a description. The typed ones the queries
    filter on (article, colour, thickness, size) are promoted to real columns so
    the stock-check filter is indexable.
    """
    __tablename__ = "material_lot"
    category: Mapped[str] = mapped_column(String(20), index=True)   # MaterialCategory
    subtype: Mapped[str | None] = mapped_column(String(20), index=True)
    article: Mapped[str] = mapped_column(String(120), index=True)
    colour: Mapped[str | None] = mapped_column(String(80), index=True)
    thickness: Mapped[str | None] = mapped_column(String(40), index=True)
    size: Mapped[str | None] = mapped_column(String(40), index=True)
    uom: Mapped[str] = mapped_column(String(20))
    on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("material_supplier.id", ondelete="SET NULL"), nullable=True, index=True)
    attributes: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MaterialReservation(Base, UUIDMixin, TimestampMixin):
    """A soft allocation against a lot. available = on_hand − Σ active reservations.

    Never mutates on_hand — mirrors the shipped inventory_reservation pattern so a
    reserved requirement can't be silently spent, and releasing frees it without
    disturbing physical stock.
    """
    __tablename__ = "material_reservation"
    material_lot_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("material_lot.id"), index=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    status: Mapped[str] = mapped_column(String(15), default="active", index=True)
    reason: Mapped[str | None] = mapped_column(String(120))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MaterialReceipt(Base, UUIDMixin, TimestampMixin):
    """One receiving record: approved qty added to a lot, rejected qty logged.

    Rejected quantity is kept for the supplier's quality history — it is the only
    place the factory learns which suppliers ship rejects.
    """
    __tablename__ = "material_receipt"
    material_lot_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("material_lot.id"), index=True)
    supplier_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("supplier_order.id", ondelete="SET NULL"), nullable=True, index=True)
    approved_qty: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    rejected_qty: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    received_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)


class MaterialSupplier(Base, UUIDMixin, TimestampMixin):
    """A material supplier. Minimal for v1 — the article→supplier suggestion reads
    `articles` (a comma list) as a simple lookup, NOT the AI procurement
    classifier (that is separate August-20 scope).

    F104: table is `material_supplier` (NOT `supplier`) to avoid colliding with
    the richer `supplier` table owned by the out-of-scope supplier_po module in
    the same MetaData registry. See F126 — whether these two supplier concepts
    should be unified is an Aug-20 procurement-scope decision, deliberately left
    distinct here.
    """
    __tablename__ = "material_supplier"
    name: Mapped[str] = mapped_column(String(200), index=True)
    articles: Mapped[str | None] = mapped_column(String(600))   # "SUEDE-A32,NAP-11"
    contact: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SupplierOrder(Base, UUIDMixin, TimestampMixin):
    """A manual supplier order. Two states only: ORDERED → ARRIVED."""
    __tablename__ = "supplier_order"
    category: Mapped[str] = mapped_column(String(20))
    article: Mapped[str] = mapped_column(String(120), index=True)
    colour: Mapped[str | None] = mapped_column(String(80))
    thickness: Mapped[str | None] = mapped_column(String(40))          # NEW
    dcm: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))        # NEW
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    uom: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(15), default="ordered", index=True)
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("material_supplier.id", ondelete="SET NULL"), nullable=True, index=True)
    ordered_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))