"""
================================================================================
modules/barcode/repository.py — Async data access for the barcode registry
================================================================================
Owns every write to barcode_registry + the sequence counters that make employee
and drawer codes unique. resolve() is a single indexed read on `code`.

CODE GENERATION IS DETERMINISTIC AND COLLISION-SAFE.
    Piece codes come from the piece (STYLE-COLOUR-SIZE-seq, already unique).
    Employee/drawer/lot codes are minted here as PREFIX + zero-padded counter
    (EMP-000123). The counter is the current max for that prefix + 1; the unique
    index on `code` is the hard guard, so a concurrent mint fails loudly rather
    than colliding — retry the request if that ever fires (create rate is a few
    per day, so it effectively never does).
================================================================================
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, func, literal, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import BarcodeStatus, BarcodeType
from app.modules.barcode.models import (
    BarcodeRegistry, Drawer, MaterialLot, MaterialReservation,
)
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.employees.models import Employee
from app.modules.production.models import Operation, Piece, ProductionEvent


def _norm(code: str | None) -> str:
    return (code or "").strip().upper()


# ══════════════════════════════════════════════════════════════════════════
# THE COMPACT PIECE CODE  (bug #19)
# ══════════════════════════════════════════════════════════════════════════
# A piece's identity used to BE its printed code: "KJ2451-CLERMONT-57-M-005",
# ~24 characters. Encoded as Code128 that is a wide label and a slow, error-prone
# scan, and the client also wants ARTICLE on the sticker — which would make it
# longer still. So the two jobs are separated:
#
#     the BARCODE carries a small unique id      → PC-23456A
#     the STICKER prints the business identity   → order · style · article ·
#                                                   colour · size · serial
#
# The long code is NOT retired. Every label already printed keeps resolving,
# because the old code stays in the registry as an ALIAS row (is_alias=True)
# pointing at the same piece. See BarcodeRegistry.is_alias for why that flag is
# stored rather than inferred.
#
# BASE 30, NOT BASE 36. The alphabet omits 0/O and 1/I — the four characters a
# human re-keying a smudged label confuses — and is in ASCII-ascending order, so
# a fixed-width encoding sorts lexicographically exactly as it sorts numerically.
# That is what lets _next_code's "ORDER BY code DESC LIMIT 1" trick work here too
# (one row transferred per mint instead of the whole prefix).
SHORT_CODE_PREFIX = "PC"
SHORT_CODE_WIDTH = 6
_B30 = "23456789ABCDEFGHJKMNPQRSTVWXYZ"          # 30 chars, ascending ASCII
_B30_INDEX = {c: i for i, c in enumerate(_B30)}


def encode_short(counter: int) -> str:
    """1 → 'PC-222223'. Fixed width, left-padded with the alphabet's zero digit."""
    n = max(int(counter), 0)
    out = ""
    while n:
        n, rem = divmod(n, len(_B30))
        out = _B30[rem] + out
    return f"{SHORT_CODE_PREFIX}-{out.rjust(SHORT_CODE_WIDTH, _B30[0])}"


def decode_short(code: str | None) -> int:
    """'PC-222223' → 1. Returns 0 for anything that is not a short code, so a
    stray row can never poison the counter (it just doesn't raise the maximum)."""
    text = _norm(code)
    head, sep, tail = text.partition("-")
    if head != SHORT_CODE_PREFIX or not sep or not tail:
        return 0
    total = 0
    for ch in tail:
        idx = _B30_INDEX.get(ch)
        if idx is None:
            return 0
        total = total * len(_B30) + idx
    return total


class BarcodeRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── resolve ──────────────────────────────────────────────────────────────
    async def get_by_code(self, code: str) -> BarcodeRegistry | None:
        res = await self.db.execute(
            select(BarcodeRegistry).where(BarcodeRegistry.code == _norm(code))
        )
        return res.scalar_one_or_none()

    async def get_for_employee(self, employee_id: uuid.UUID,
                               active_only: bool = True) -> BarcodeRegistry | None:
        stmt = select(BarcodeRegistry).where(
            BarcodeRegistry.employee_id == employee_id,
            BarcodeRegistry.type == BarcodeType.EMPLOYEE.value,
        )
        if active_only:
            stmt = stmt.where(BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
        return await self.db.scalar(stmt.order_by(BarcodeRegistry.created_at.desc()))

    async def codes_for_employees(
        self, employee_ids: list[uuid.UUID], active_only: bool = True
    ) -> dict[uuid.UUID, str]:
        """employee_id → its card code, for a whole roster in ONE query.

        The per-row `get_for_employee` would be N queries behind a roster list;
        this is the batch form. Newest-first ordering means the dict keeps the
        most recent code per employee when a reissue left older retired rows
        behind (active_only=False)."""
        if not employee_ids:
            return {}
        stmt = select(
            BarcodeRegistry.employee_id, BarcodeRegistry.code
        ).where(
            BarcodeRegistry.employee_id.in_(employee_ids),
            BarcodeRegistry.type == BarcodeType.EMPLOYEE.value,
        )
        if active_only:
            stmt = stmt.where(BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
        res = await self.db.execute(stmt.order_by(BarcodeRegistry.created_at.asc()))
        # asc + overwrite == newest wins, without a window function.
        return {emp_id: code for emp_id, code in res.all() if emp_id is not None}

    # ── resolve payload reads ────────────────────────────────────────────────
    # One method per barcode type, each ONE query selecting ONLY the columns the
    # payload prints. No entity is hydrated: resolve() reads ~12 scalars off a
    # piece and never touches the other 6 columns, so loading Piece + SKU + Style
    # objects (and paying identity-map + attribute-instrumentation cost on every
    # scan) buys nothing. The service composes the response dict from these rows.

    async def piece_card(self, piece_id: uuid.UUID) -> Row | None:
        """Everything the PIECE payload shows, in one query.

        This was 3 round-trips in the service (piece+joins, then a drawer get, then
        a consumption select). The drawer is a LEFT JOIN (null before merge) and the
        consumption is a correlated scalar subquery (null before cutting), so the
        whole card is one statement — one scan, one query.

        THE DRAWER JOIN IS ON `Drawer.current_piece_id`, NOT `Piece.drawer_id`.
        Those two are the same link from opposite ends, but only one of them is
        live: `release_nocommit` nulls BOTH when a piece ships, while a store scan
        moves `Drawer.state`/`leather_in`/`lining_in` on the row that CLAIMS the
        piece. Reading the drawer through the claim is what makes the state and
        holding columns below (bug #12) the same answer the store screen gives.

        ARTICLE (bug #7/#19) comes off Style — it is the field the printed sticker
        must show and the one the barcode payload never carried.
        """
        consumption = (
            select(ProductionEvent.consumption_qty)
            .where(ProductionEvent.piece_id == Piece.id,
                   ProductionEvent.consumption_qty.isnot(None))
            .order_by(ProductionEvent.created_at)
            .limit(1)
            .correlate(Piece)
            .scalar_subquery()
        )
        return (await self.db.execute(
            select(
                Piece.id, Piece.code, Piece.seq, Piece.needs_lining,
                SKU.id.label("sku_id"),
                SKU.code.label("sku_code"),
                SKU.color_name, SKU.color_code, SKU.size,
                Style.id.label("style_id"),
                Style.name.label("style_name"),
                Style.article.label("article"),
                ClientOrder.id.label("order_id"),
                ClientOrder.order_number,
                Client.name.label("client_name"),
                Operation.code.label("current_stage"),
                Drawer.id.label("drawer_id"),
                Drawer.code.label("drawer_code"),
                Drawer.state.label("drawer_state"),
                Drawer.leather_in, Drawer.lining_in,
                consumption.label("consumption_qty"),
            )
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Client, Client.id == ClientOrder.client_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .outerjoin(Drawer, Drawer.current_piece_id == Piece.id)
            .where(Piece.id == piece_id)
        )).first()

    async def employee_card(self, employee_id: uuid.UUID) -> Row | None:
        return (await self.db.execute(
            select(Employee.id, Employee.name, Employee.designation,
                   Employee.wage_type, Employee.is_active)
            .where(Employee.id == employee_id)
        )).first()

    async def drawer_card(self, drawer_id: uuid.UUID) -> Row | None:
        return (await self.db.execute(
            select(Drawer.id, Drawer.code, Drawer.seq, Drawer.state,
                   Drawer.current_piece_id, Drawer.leather_in, Drawer.lining_in,
                   Drawer.sent_to)
            .where(Drawer.id == drawer_id)
        )).first()

    async def lot_card(self, lot_id: uuid.UUID) -> Row | None:
        """Lot columns + `available`, derived in SQL.

        available = on_hand − Σ active reservations — the same rule
        MaterialService.available_for_lot applies (that service stays the authority
        for the material module). Doing it as a correlated subquery here turns a
        3-query payload (lot get + lot get again inside the service + reservation
        sum) into one. `reserved` is never stored, so the two cannot drift."""
        reserved = (
            select(func.coalesce(func.sum(MaterialReservation.qty), 0))
            .where(MaterialReservation.material_lot_id == MaterialLot.id,
                   MaterialReservation.status == "active")
            .correlate(MaterialLot)
            .scalar_subquery()
        )
        return (await self.db.execute(
            select(
                MaterialLot.id, MaterialLot.category, MaterialLot.subtype,
                MaterialLot.article, MaterialLot.colour, MaterialLot.thickness,
                MaterialLot.size, MaterialLot.uom, MaterialLot.on_hand,
                (MaterialLot.on_hand - reserved).label("available"),
            )
            .where(MaterialLot.id == lot_id)
        )).first()

    # ── order existence / lookup ─────────────────────────────────────────────
    async def order_exists(self, order_id: uuid.UUID) -> bool:
        """EXISTS, not a row load — the callers only branch on it (404 or not)."""
        return bool(await self.db.scalar(
            select(literal(1)).where(ClientOrder.id == order_id).limit(1)
        ))

    async def order_id_by_number(self, order_number: str) -> uuid.UUID | None:
        return await self.db.scalar(
            select(ClientOrder.id)
            .where(ClientOrder.order_number == order_number.strip())
        )

    # ── code minting ─────────────────────────────────────────────────────────
    async def _next_code(self, prefix: str, width: int = 6) -> str:
        """PREFIX + zero-padded (max existing counter for this prefix) + 1.

        F79/F99: previously this SELECTed every barcode with this prefix and took
        the max in Python — O(all barcodes) transferred per mint, quadratic across
        an import that mints hundreds of codes. Since the zero-padded numeric tail
        is fixed-width, lexicographic order == numeric order, so ORDER BY code DESC
        LIMIT 1 gives the highest existing code while transferring ONE row. This is
        dialect-agnostic (works identically on SQLite and Postgres); a dedicated
        integer sequence column would be the fully clean long-term form.
        """
        like = f"{prefix}-%"
        top = await self.db.scalar(
            select(BarcodeRegistry.code)
            .where(BarcodeRegistry.code.like(like))
            .order_by(BarcodeRegistry.code.desc())
            .limit(1)
        )
        mx = 0
        if top:
            tail = top[len(prefix) + 1:]
            if tail.isdigit():
                mx = int(tail)
        return f"{prefix}-{mx + 1:0{width}d}"

    # ── registration (all *_nocommit; the SERVICE owns the transaction) ──────
    def register_nocommit(self, *, code: str, type_: BarcodeType,
                          caption: str | None = None,
                          piece_id: uuid.UUID | None = None,
                          employee_id: uuid.UUID | None = None,
                          drawer_id: uuid.UUID | None = None,
                          material_lot_id: uuid.UUID | None = None,
                          order_id: uuid.UUID | None = None,
                          sku_id: uuid.UUID | None = None,
                          style_id: uuid.UUID | None = None,
                          is_alias: bool = False) -> BarcodeRegistry:
        row = BarcodeRegistry(
            code=_norm(code), type=type_.value, status=BarcodeStatus.ACTIVE.value,
            caption=caption, piece_id=piece_id, employee_id=employee_id,
            drawer_id=drawer_id, material_lot_id=material_lot_id,
            order_id=order_id, sku_id=sku_id, style_id=style_id,
            is_alias=is_alias,
        )
        self.db.add(row)
        return row

    async def mint_employee_code_nocommit(self, employee_id: uuid.UUID,
                                          caption: str | None) -> BarcodeRegistry:
        code = await self._next_code("EMP")
        return self.register_nocommit(
            code=code, type_=BarcodeType.EMPLOYEE,
            employee_id=employee_id, caption=caption)

    async def mint_drawer_code_nocommit(self, drawer_id: uuid.UUID, seq: int,
                                        caption: str | None = None) -> BarcodeRegistry:
        code = f"DRW-{seq:04d}"
        return self.register_nocommit(
            code=code, type_=BarcodeType.DRAWER, drawer_id=drawer_id,
            caption=caption or f"Drawer {seq}")

    # ── the compact piece code (bug #19) ─────────────────────────────────────
    async def max_short_code_counter(self) -> int:
        """The highest short-code counter in use, as an int.

        ONE row transferred, for the same reason _next_code does it this way: the
        encoding is fixed-width over an ASCII-ascending alphabet, so the
        lexicographic maximum IS the numeric maximum. Callers that mint a whole
        import's worth of codes read this ONCE and then count up in Python — a
        1,400-piece upload must not run 1,400 MAX() queries (that is exactly the
        quadratic behaviour F79/F99 removed from _next_code).
        """
        top = await self.db.scalar(
            select(BarcodeRegistry.code)
            .where(BarcodeRegistry.code.like(f"{SHORT_CODE_PREFIX}-%"))
            .order_by(BarcodeRegistry.code.desc())
            .limit(1)
        )
        return decode_short(top)

    async def mint_piece_short_code_nocommit(
        self, piece_id: uuid.UUID, *, caption: str | None = None,
        order_id: uuid.UUID | None = None, sku_id: uuid.UUID | None = None,
        style_id: uuid.UUID | None = None,
    ) -> BarcodeRegistry:
        """Mint ONE compact code for a piece. For a single piece (a repair, a
        reprint); the importer uses max_short_code_counter + encode_short directly
        so it pays one read for the whole batch."""
        code = encode_short(await self.max_short_code_counter() + 1)
        return self.register_nocommit(
            code=code, type_=BarcodeType.PIECE, piece_id=piece_id,
            caption=caption, order_id=order_id, sku_id=sku_id, style_id=style_id)

    async def short_codes_for_pieces(
        self, piece_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """piece_id → its COMPACT scannable code, for a whole page in ONE query.

        Batch form, mirroring codes_for_employees. Three filters, each load-bearing:

          is_alias=False   the legacy long code is not the code to print
          status=ACTIVE    a retired label is not the code to print either
          code LIKE 'PC-%' the row must actually BE a compact code

        The prefix filter is not redundant with is_alias. A piece minted before
        the switch and not yet backfilled has exactly one row — its long code,
        primary and active — and without this clause that long code would come
        back as the piece's "short_code", which is precisely the thing it is not.
        A missing entry is the honest answer there, and it is what the payload
        documents: null means "not backfilled yet", so `scripts/backfill_short_
        codes.py` still has work to do.
        """
        if not piece_ids:
            return {}
        rows = await self.db.execute(
            select(BarcodeRegistry.piece_id, BarcodeRegistry.code)
            .where(BarcodeRegistry.piece_id.in_(piece_ids),
                   BarcodeRegistry.type == BarcodeType.PIECE.value,
                   BarcodeRegistry.is_alias.is_(False),
                   BarcodeRegistry.status == BarcodeStatus.ACTIVE.value,
                   BarcodeRegistry.code.like(f"{SHORT_CODE_PREFIX}-%"))
            .order_by(BarcodeRegistry.created_at.asc())
        )
        return {pid: code for pid, code in rows.all() if pid is not None}

    async def mint_lot_code_nocommit(self, material_lot_id: uuid.UUID,
                                     type_: BarcodeType,
                                     caption: str | None) -> BarcodeRegistry:
        prefix = {"LEATHER_LOT": "LOT-LEA", "LINING_LOT": "LOT-LIN",
                  "ACCESSORY_LOT": "LOT-ACC"}[type_.value]
        code = await self._next_code(prefix)
        return self.register_nocommit(
            code=code, type_=type_, material_lot_id=material_lot_id, caption=caption)

    # ── retire / reissue (employee) ──────────────────────────────────────────
    async def retire_nocommit(self, row: BarcodeRegistry, reason: str) -> None:
        row.status = BarcodeStatus.RETIRED.value
        row.retired_at = datetime.now(timezone.utc)
        row.retired_reason = reason

    # ── batch fetch for print ────────────────────────────────────────────────
    async def captions_for_codes(self, codes: list[str]) -> dict[str, str | None]:
        """{normalised code → caption} for the codes that exist.

        A label needs the code and the caption, nothing else — so this selects two
        columns instead of hydrating whole BarcodeRegistry entities (a print run is
        hundreds of codes). Membership in the dict IS the 'known' flag; a known code
        with no caption is present with a None value, so `in` and `.get()` differ
        meaningfully."""
        if not codes:
            return {}
        norm = {_norm(c) for c in codes}
        rows = await self.db.execute(
            select(BarcodeRegistry.code, BarcodeRegistry.caption)
            .where(BarcodeRegistry.code.in_(norm))
        )
        return {code: caption for code, caption in rows.all()}

    async def piece_codes_for(self, *, sku_id: uuid.UUID | None = None,
                              order_id: uuid.UUID | None = None) -> list[str]:
        """Every SCANNABLE piece code under a SKU and/or an order — the print run's
        expansion of 'print all labels for this SKU/order'.

        Reads the registry, not Piece.code: since bug #19 the code that goes on the
        label is the compact primary row, and Piece.code is the long identity that
        now only lives on the sticker text. Selecting Piece.code here would print a
        sheet of the very labels the change set out to shrink.
        """
        stmt = (
            select(BarcodeRegistry.code)
            .join(Piece, Piece.id == BarcodeRegistry.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .where(BarcodeRegistry.type == BarcodeType.PIECE.value,
                   BarcodeRegistry.is_alias.is_(False),
                   BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
            .order_by(Piece.code)
        )
        if sku_id:
            stmt = stmt.where(Piece.sku_id == sku_id)
        if order_id:
            stmt = stmt.join(Style, Style.id == SKU.style_id).where(
                Style.client_order_id == order_id)
        return list((await self.db.scalars(stmt)).all())

    async def label_details_for_codes(self, codes: list[str]) -> dict[str, dict]:
        """{code → the business identity printed UNDER the barcode} (bug #19).

        One query for a whole print run. The barcode itself is now a small opaque
        id, so this is what makes a label readable by a human: order, article,
        style, colour, size and the zero-padded serial (001/002/003 — bug #7).
        Keyed by the registry code, so it answers for the compact code AND for a
        legacy long alias, both of which name the same piece.
        """
        if not codes:
            return {}
        norm = {_norm(c) for c in codes}
        rows = (await self.db.execute(
            select(
                BarcodeRegistry.code,
                Piece.code.label("piece_code"), Piece.seq,
                SKU.color_name, SKU.color_code, SKU.size,
                Style.name.label("style_name"), Style.article,
                ClientOrder.order_number,
            )
            .join(Piece, Piece.id == BarcodeRegistry.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(BarcodeRegistry.code.in_(norm))
        )).all()
        return {
            r.code: {
                "order_number": r.order_number,
                "article": r.article,
                "style": r.style_name,
                "colour": r.color_name or r.color_code,
                "size": r.size,
                "serial": f"{r.seq:03d}" if r.seq is not None else None,
                "piece_code": r.piece_code,
            }
            for r in rows
        }

    def add_audit_nocommit(self, *, actor_id: uuid.UUID | None, action: str,
                           entity_id: uuid.UUID, after: dict) -> None:
        """Stage an audit row for an employee-barcode transition. Staged, not
        committed: it lands in the same transaction as the retire/mint."""
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_id, action=action, entity_type="employee_barcode",
            entity_id=entity_id, after=after, at=datetime.now(timezone.utc)))

    async def flush(self) -> None:
        await self.db.flush()

    async def commit(self) -> None:
        await self.db.commit()

    # ── order picker ────────────────────────────────────────────────────────
    async def list_orders_with_barcodes(self) -> list[dict]:
        """Every order that has at least one PIECE barcode, with its minted count
        and the generated-at date range.

        NOT client-scoped (Hamthan #6): the barcode screens are staff-wide, and
        the /barcode/orders* routes keep CLIENT/VIEWER out at the router's role
        gate instead. The old `client_id` parameter was never applied to the
        query — it promised a filter that did not exist, so it is gone rather
        than left as a scoping trap."""

        stmt = (
            select(
                ClientOrder.id.label("order_id"),
                ClientOrder.order_number,
                Client.name.label("client_name"),
                func.count(BarcodeRegistry.id).label("minted"),
                func.min(BarcodeRegistry.created_at).label("first_generated_at"),
                func.max(BarcodeRegistry.created_at).label("last_generated_at"),
            )
            .join(Client, Client.id == ClientOrder.client_id)
            .join(
                BarcodeRegistry,
                and_(BarcodeRegistry.order_id == ClientOrder.id,
                        BarcodeRegistry.type == BarcodeType.PIECE.value,
                        BarcodeRegistry.is_alias.is_(False)),
            )
            .group_by(ClientOrder.id, ClientOrder.order_number, Client.name)
            .order_by(func.max(BarcodeRegistry.created_at).desc())
        )

        rows = (await self.db.execute(stmt)).all()
        return [
            {
                "order_id": r.order_id,
                "order_number": r.order_number,
                "client_name": r.client_name,
                "minted": int(r.minted),
                "first_generated_at": r.first_generated_at,
                "last_generated_at": r.last_generated_at,
            }
            for r in rows
        ]

    # ── planned totals (SKU.qty_ordered) ────────────────────────────────────
    async def order_planned_total(self, order_id: uuid.UUID) -> int:
        total = await self.db.scalar(
            select(func.coalesce(func.sum(SKU.qty_ordered), 0))
            .select_from(SKU)
            .join(Style, Style.id == SKU.style_id)
            .where(Style.client_order_id == order_id)
        )
        return int(total or 0)

    # NOTE ON is_alias IN THE THREE COUNTS BELOW.
    # These answer "how many GARMENTS have a barcode", which the factory
    # reconciles against SKU.qty_ordered. Since bug #19 a piece can hold two
    # registry rows — the compact primary and its legacy long alias — so counting
    # rows without excluding aliases would report double the pieces, drive
    # `balance` negative and make the duplicates=0 integrity proof read as broken.
    # An alias also carries no order_id, so the filter is belt and braces.
    async def order_minted_total(self, order_id: uuid.UUID) -> int:
        total = await self.db.scalar(
            select(func.count(BarcodeRegistry.id))
            .where(BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == BarcodeType.PIECE.value,
                    BarcodeRegistry.is_alias.is_(False))
        )
        return int(total or 0)

    async def order_active_total(self, order_id: uuid.UUID) -> int:
        """Minted AND still active (a retired label is minted but not scannable)."""
        total = await self.db.scalar(
            select(func.count(BarcodeRegistry.id))
            .where(BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == BarcodeType.PIECE.value,
                    BarcodeRegistry.is_alias.is_(False),
                    BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
        )
        return int(total or 0)

    async def order_distinct_code_total(self, order_id: uuid.UUID) -> int:
        """DISTINCT codes — if this ever differs from minted, a duplicate slipped
        past the unique index. Lets analytics PROVE uniqueness (duplicates=0)."""
        total = await self.db.scalar(
            select(func.count(func.distinct(BarcodeRegistry.code)))
            .where(BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == BarcodeType.PIECE.value,
                    BarcodeRegistry.is_alias.is_(False))
        )
        return int(total or 0)

    # ── per-style analytics (planned vs minted vs balance) ──────────────────
    async def style_breakdown(self, order_id: uuid.UUID) -> list[dict]:
        """Per-style planned (SUM qty_ordered) vs minted (count of piece barcodes)
        vs balance. Two independent aggregates joined on style_id in Python so a
        style with 0 minted still appears (LEFT side = planned)."""

        planned_rows = (await self.db.execute(
            select(Style.id, Style.name, Style.code,
                    func.coalesce(func.sum(SKU.qty_ordered), 0).label("planned"))
            .select_from(Style)
            .join(SKU, SKU.style_id == Style.id)
            .where(Style.client_order_id == order_id)
            .group_by(Style.id, Style.name, Style.code)
        )).all()

        minted_rows = (await self.db.execute(
            select(BarcodeRegistry.style_id,
                    func.count(BarcodeRegistry.id).label("minted"))
            .where(BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == BarcodeType.PIECE.value,
                    BarcodeRegistry.is_alias.is_(False))
            .group_by(BarcodeRegistry.style_id)
        )).all()
        minted_by_style = {r.style_id: int(r.minted) for r in minted_rows}

        out = []
        for r in planned_rows:
            minted = minted_by_style.get(r.id, 0)
            planned = int(r.planned)
            out.append({
                "style_id": r.id,
                "style_name": r.name,
                "style_code": r.code,
                "planned": planned,
                "minted": minted,
                "balance": max(planned - minted, 0),
            })
        out.sort(key=lambda x: x["style_name"] or "")
        return out

    # ── filterable history list (paginated) ─────────────────────────────────
    async def list_barcodes(
        self,
        order_id: uuid.UUID,
        *,
        sku_id: uuid.UUID | None = None,
        style_id: uuid.UUID | None = None,
        size: str | None = None,
        status: str | None = None,
        date_from: "datetime | None" = None,
        date_to: "datetime | None" = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict], int]:
        """Return (rows, total_count). Filters: style, sku, style+size, status,
        generated-date range. size filters via SKU.size (join only when needed)."""

        # Base filter on the indexed denormalised columns. is_alias is excluded
        # explicitly: a piece has one PRIMARY code and possibly one legacy alias,
        # and the history table lists GARMENTS, not labels — without this each
        # piece would appear twice (see BarcodeRegistry.is_alias).
        conds = [BarcodeRegistry.order_id == order_id,
                    BarcodeRegistry.type == BarcodeType.PIECE.value,
                    BarcodeRegistry.is_alias.is_(False)]
        if sku_id:
            conds.append(BarcodeRegistry.sku_id == sku_id)
        if style_id:
            conds.append(BarcodeRegistry.style_id == style_id)
        if status:
            conds.append(BarcodeRegistry.status == status.lower())
        if date_from:
            conds.append(BarcodeRegistry.created_at >= date_from)
        if date_to:
            conds.append(BarcodeRegistry.created_at <= date_to)

        need_sku_join = bool(size)  # size lives on SKU

        # total count (respecting filters)
        count_stmt = select(func.count(BarcodeRegistry.id))
        if need_sku_join:
            count_stmt = count_stmt.join(SKU, SKU.id == BarcodeRegistry.sku_id)
            conds.append(func.upper(SKU.size) == size.strip().upper())
        count_stmt = count_stmt.where(and_(*conds))
        total = int(await self.db.scalar(count_stmt) or 0)

        # page of rows, enriched with sku/style/size/colour/seq/stage/drawer
        stmt = (
            select(
                BarcodeRegistry.code,
                BarcodeRegistry.status,
                BarcodeRegistry.caption,
                BarcodeRegistry.created_at,
                SKU.code.label("sku_code"),
                SKU.color_name, SKU.color_code, SKU.size,
                Style.name.label("style_name"),
                Style.article,
                Piece.code.label("piece_code"),
                Piece.seq,
                Operation.code.label("current_stage"),
            )
            .join(SKU, SKU.id == BarcodeRegistry.sku_id)
            .join(Style, Style.id == BarcodeRegistry.style_id)
            .outerjoin(Piece, Piece.id == BarcodeRegistry.piece_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .where(and_(*conds))
            .order_by(BarcodeRegistry.created_at.desc(), BarcodeRegistry.code)
            .limit(page_size)
            .offset((max(page, 1) - 1) * page_size)
        )
        rows = (await self.db.execute(stmt)).all()
        items = [
            {
                "code": r.code,
                "status": r.status,
                "sku_code": r.sku_code,
                "style_name": r.style_name,
                # bug #7/#19: the two columns the history table could not show.
                "article": r.article,
                "serial": f"{r.seq:03d}" if r.seq is not None else None,
                "piece_code": r.piece_code,
                "colour": r.color_name or r.color_code,
                "size": r.size,
                "seq": r.seq,
                "current_stage": r.current_stage,
                "generated_at": r.created_at,
            }
            for r in rows
        ]
        return items, total

    # ── SKU picker for the filter dropdowns (scoped to one order) ────────────
    async def list_order_skus(self, order_id: uuid.UUID) -> list[dict]:
        rows = (await self.db.execute(
            select(SKU.id, SKU.code, SKU.color_name, SKU.color_code, SKU.size,
                    Style.id.label("style_id"), Style.name.label("style_name"))
            .join(Style, Style.id == SKU.style_id)
            .where(Style.client_order_id == order_id)
            .order_by(Style.name, SKU.size)
        )).all()
        return [
            {"sku_id": r.id, "sku_code": r.code,
                "colour": r.color_name or r.color_code, "size": r.size,
                "style_id": r.style_id, "style_name": r.style_name}
            for r in rows
        ]