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
    # ── THE THIRD BUCKET: the accessory kit ──────────────────────────────────
    # Buttons, zips and thread are not cut — they are ISSUED from the store into
    # the drawer alongside the two cut parts, and issuing them SPENDS STOCK. This
    # boolean is the drawer's half of that: True once every accessory line on the
    # piece's style spec has been issued in full.
    #
    # NOT NULL + server_default "0" so every drawer already in the building reads
    # False, which — combined with `kit_required` being false for any style with
    # no accessory spec — makes the completeness predicate degenerate to exactly
    # what it computed before this column existed. See DrawerService._kit_required.
    #
    # NO `kit_issued_at`. leather_in/lining_in carry no timestamps either, and
    # piece_material_issue.issued_at plus last_activity_at below already answer
    # "when" — precisely, per line. A fourth clock could only drift from those.
    accessories_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ── THE STORE SCREEN'S "LATEST" CLOCK ────────────────────────────────────
    # Stamped by EVERY act on this drawer: merge at release, store-scan, receive,
    # send, and release-back-to-the-pool. Nothing else in the row can answer
    # "which drawers is the floor working on right now":
    #   created_at  — a bootstrapped pool shares one timestamp across 200 rows,
    #                 so it ranks them arbitrarily and forever.
    #   received_at / sended_at — both are NULLED by release_nocommit when the
    #                 garment ships, so the drawer that JUST shipped sinks to the
    #                 bottom of a "recent" list. They also say nothing about the
    #                 two events the store cares most about: the merge and the
    #                 part scan, neither of which writes a timestamp at all.
    # This column is monotonic within a drawer's cycle and is never cleared, so
    # `ORDER BY last_activity_at DESC` is the store screen's default ordering.
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True)
    # WHY the last act was recorded — "merged" | "scanned" | "received" | "sent"
    # | "released". The list shows it beside the timestamp so a drawer at the top
    # says why it is there rather than just when.
    last_activity_kind: Mapped[str | None] = mapped_column(String(20))
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

# ══════════════════════════════════════════════════════════════════════════════
# THE PER-PIECE MATERIAL SPEC (the recipe) AND ITS LEDGER (what was spent)
# ══════════════════════════════════════════════════════════════════════════════
# WHY THESE TWO TABLES LIVE HERE, beside MaterialLot rather than in a models file
# of their own: materials/repository.py already imports MaterialLot from this
# module, main.py already imports this module for Base.metadata, and CLAUDE.md
# §11 names a missed model import as the schema-drift trap that makes Alembic
# autogenerate DROP a table. A new models file would buy nothing and cost that.


class StyleMaterialSpec(Base, UUIDMixin, TimestampMixin):
    """One line of a style's per-piece recipe: this garment takes 4 of THIS.

    THE PROBLEM THIS SOLVES. Before this table the only consumption the system
    could spend was leather, and its quantity was typed by the cutting manager on
    every single scan. Accessories - buttons, zips, thread - could be stocked,
    barcoded and received, but nothing ever decremented them, so accessory
    on_hand only ever went up and the floor assembled each kit from memory. The
    recipe is now declared once, before the style is released, and spent
    automatically.

    THE SIX IDENTITY COLUMNS ARE MaterialRepository.LOT_KEY, DELIBERATELY:
        category, subtype, article, colour, thickness, size
    The same six, in the same order, so a spec line resolves to a stock lot
    through the existing find_lots(). The alternative was a second matching
    implementation that would drift from the first the moment either grew a rule.
    """
    __tablename__ = "style_material_spec"
    # THE UNIQUE CONSTRAINT IS A BACKSTOP, NOT THE DEDUPE. NULLs compare distinct
    # in a unique index on both Postgres and SQLite, so a line with colour IS NULL
    # inserts twice without tripping it. StyleSpecRepository.find_duplicate_line
    # is the real check, using the same explicit "is_(None) if val is None" idiom
    # MaterialRepository.find_duplicate_lot already uses for exactly this reason.
    __table_args__ = (
        UniqueConstraint(
            "style_id", "sku_id", "category", "subtype", "article",
            "colour", "thickness", "size", name="uq_style_material_spec_line"),
        Index("ix_style_material_spec_style_active", "style_id", "is_active"),
    )
    # BOTH FKs CASCADE - a deliberate departure from 20260818_fk_setnull_all.
    # That migration's rule is about nullable REFERENCE columns; these two are
    # OWNERSHIP. style_id follows SkuOrderLine.sku_id: a spec line has no meaning
    # without its style.
    style_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("style.id", ondelete="CASCADE"), index=True)
    # NULL = the style-wide default. Set = an override for one colour+size.
    #
    # A SKU line overrides a style line LINE-LEVEL, keyed on (category, subtype,
    # article) - the real case is "the TAN colourway takes TAN buttons of the same
    # article". A SKU line naming an article the style lacks is an ADDITION, and
    # qty_per_piece = 0 REMOVES that material for that colourway. Set-level
    # replacement was the alternative and it would force re-entering the whole
    # recipe for every colour that differs by one button.
    #
    # NOT SET NULL, despite being nullable: nulling it would silently PROMOTE a
    # colourway override into a style-wide default, and collide with the unique
    # constraint on the way.
    sku_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("sku.id", ondelete="CASCADE"), nullable=True, index=True)

    category: Mapped[str] = mapped_column(String(20), index=True)   # MaterialCategory
    subtype: Mapped[str | None] = mapped_column(String(20))         # MaterialSubtype
    # Accessories identify the item by article; leather and lining identify the
    # material kind by category/subtype/thickness and may leave this blank.
    article: Mapped[str | None] = mapped_column(String(120), index=True,
                                                  nullable=True)
    colour: Mapped[str | None] = mapped_column(String(80))
    thickness: Mapped[str | None] = mapped_column(String(40))
    size: Mapped[str | None] = mapped_column(String(40))            # zip 60cm, button 18L

    # (12,3) - the PER-PIECE scale, matching ProductionEvent.consumption_qty and
    # BomItem.qty_per_garment. Stock TOTALS are (14,3); keep the two distinct.
    qty_per_piece: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    uom: Mapped[str] = mapped_column(String(20))   # derived from uom_for(); never sent

    # PINNED AT AUTHOR TIME. The authoring screen resolves the article to a lot
    # anyway, so storing the id turns every later read into one indexed lookup
    # instead of a six-column search - and it keeps resolving after a lot is
    # renamed underneath the spec, which MaterialService.update_lot can do.
    material_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("material_lot.id", ondelete="SET NULL"),
        nullable=True, index=True)

    note: Mapped[str | None] = mapped_column(String(300))
    # Soft delete: a line already ISSUED against must stay readable through
    # piece_material_issue.spec_line_id, so lines deactivate rather
    # than disappear.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1")


class PieceMaterialIssue(Base, UUIDMixin, TimestampMixin):
    """One material actually spent on one garment. The ledger, and the idempotency key.

    WHY THIS IS A TABLE AND NOT A DERIVED NUMBER. It is the money path. Scan guns
    double-tap, gateways retry, and two operators can reach the same drawer. The
    unique constraint on (piece_id, spec_line_id) means one row per
    (piece, recipe line), so the second tap finds the row, computes an outstanding
    of zero and spends nothing. Deriving "has this been issued" from stock
    movements would make a double-tap indistinguishable from a real second issue,
    which is how a ledger goes quietly wrong.

    THE FOUR SNAPSHOT COLUMNS ARE THE RECORD, NOT REDUNDANCY. Lots are retired
    rather than deleted, but a lot's article and colour are PATCHABLE through
    MaterialService.update_lot. Reading them back through material_lot_id would
    let an edit today silently rewrite what a garment was issued last month.

    LEATHER AND LINING ARE NOT HERE. Their consumption lives on ProductionEvent,
    because the lot link belongs to the ACT OF CUTTING and not to the piece
    (CLAUDE.md section 8). Two write paths, one read surface - see
    StyleSpecService.material_requirement_block, which merges them.
    """
    __tablename__ = "piece_material_issue"
    __table_args__ = (
        UniqueConstraint("piece_id", "spec_line_id",
                         name="uq_piece_material_issue_line"),
        Index("ix_piece_material_issue_piece_source", "piece_id", "source"),
    )
    # NULLABLE / SET NULL, matching ProductionEvent.piece_id: a stock movement
    # that physically happened must survive the administrative deletion of a
    # mis-imported piece, and the snapshot below keeps the orphaned row readable.
    # The idempotency read is WHERE piece_id = :id, which never matches NULL, so
    # this costs the hot path nothing.
    piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id", ondelete="SET NULL"),
        nullable=True, index=True)
    drawer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("drawer.id", ondelete="SET NULL"),
        nullable=True, index=True)
    spec_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("style_material_spec.id", ondelete="SET NULL"),
        nullable=True, index=True)
    material_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("material_lot.id", ondelete="SET NULL"),
        nullable=True, index=True)

    # ── the snapshot (see the docstring) ─────────────────────────────────────
    category: Mapped[str] = mapped_column(String(20))
    subtype: Mapped[str | None] = mapped_column(String(20))
    article: Mapped[str] = mapped_column(String(120))
    colour: Mapped[str | None] = mapped_column(String(80))

    # (14,3) - this is a STOCK quantity (it came off MaterialLot.on_hand), so it
    # carries the stock scale, not the per-piece one.
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    uom: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20), index=True)   # MaterialIssueSource

    # WHO, in the two-identities sense DrawerService.store_scan documents: the
    # WORKER whose card was scanned is DATA about the act; the LOGIN that
    # performed it is entered_by (a name, like ProductionEvent.entered_by) plus
    # the audit row. An employee is not an app_user and must never be written as
    # one - that mistake 500'd the store screen once already.
    issued_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("employee.id", ondelete="SET NULL"), nullable=True)
    entered_by: Mapped[str | None] = mapped_column(String(120))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
