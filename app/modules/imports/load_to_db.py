"""
load_to_db.py — Take a validated ImportPreview and write it to the database.

Idempotent: re-running with the same data updates existing rows instead of
creating duplicates. Uses get-or-create keyed on natural identity:
  Client(name) -> Style(client, name) -> SKU(style, colour, size)
and for production: Operation(code), Rate(style, op, effective_from),
ProductionEvent rows tagged with an import_batch so a re-import can replace them.

This module is written against the real SQLAlchemy models in
app/modules/*, but is import-safe to test on SQLite.
"""
from __future__ import annotations
from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.modules.clients.utlis import make_style_code   # ← "utlis" typo
from app.modules.clients.models import Client, ClientOrder, Style, SKU,SkuOrderLine
from app.modules.production.models import Operation, ProductionEvent
from app.modules.wages.models import Rate
from app.modules.clients.utlis import make_style_code


def _order_lock_key(order_number: str) -> int:
    """A stable signed 64-bit advisory-lock key for one order number.

    Hashed in Python rather than with Postgres `hashtext` so the key is
    reproducible from the application side (and greppable in pg_locks when
    someone is debugging a stuck import).
    """
    import hashlib
    digest = hashlib.blake2b(order_number.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _acquire_order_import_lock(db: Session, order_number: str) -> None:
    """Take the transaction-scoped import lock for this order, or 409.

    No-op off Postgres: SQLite has no advisory locks and the test suite is
    single-threaded, so there is no concurrency to guard against there.
    """
    from fastapi import HTTPException
    from sqlalchemy import func

    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    got = db.scalar(select(func.pg_try_advisory_xact_lock(
        _order_lock_key(order_number))))
    if not got:
        raise HTTPException(
            409,
            f"Order {order_number} is already being imported by another request. "
            "A large breakdown sheet takes a few minutes — wait for it to finish "
            "before uploading again. Nothing has been written by this attempt.")


def _get_or_create_client(db: Session, name: str, country: str | None) -> Client:
    c = db.scalar(select(Client).where(Client.name == name))
    if not c:
        c = Client(name=name, country=country)
        db.add(c); db.flush()
    return c


def _get_or_create_order(db: Session, client: Client, order_number: str) -> ClientOrder:
    order = db.scalar(select(ClientOrder).where(
        ClientOrder.client_id == client.id, ClientOrder.order_number == order_number))
    if not order:
        order = ClientOrder(client_id=client.id, order_number=order_number)
        db.add(order); db.flush()
    return order


def _get_or_create_style(db: Session, order: ClientOrder, name: str,
                         article: str | None) -> Style:
    st = db.scalar(select(Style).where(
        Style.client_order_id == order.id, Style.name == name))
    if not st:
        st = Style(client_order_id=order.id, name=name, article=article,
                   # The article is part of the code now: the sticker, the
                   # traveler and the style code all name the same leather.
                   code=make_style_code(order.order_number, name, article))
        db.add(st); db.flush()
    return st


def _upsert_sku(db, order, style, color, size, qty):
    """Returns (SKU, status). Sums qty into the one-per-triple SKU and sets its
    deterministic code on first creation."""
    from sqlalchemy import select
    from app.modules.clients.models import SKU
    from app.modules.clients.service import make_sku_code
 
    color_code = (color or "—")
    existing = db.scalar(select(SKU).where(
        SKU.style_id == style.id, SKU.color_code == color_code, SKU.size == size))
    if existing:
        existing.qty_ordered = (existing.qty_ordered or 0) + qty
        return existing, "updated"
    sku = SKU(
        style_id=style.id, color_code=color_code, color_name=color,
        size=size, qty_ordered=qty,
        # style.article, not a local — a re-imported style keeps whatever
        # article it was created with, and its SKUs must agree with its code.
        code=make_sku_code(order.order_number, style.name, color or color_code,
                           size, style.article),
    )
    db.add(sku)
    db.flush()          # visible to the next lookup, and gives sku.id for lines
    return sku, "created"


def _apply_style_commercials(style, line, order, cp) -> None:
    """Fold a line's printed PRICE and DELIVERY DATE up onto its Style.

    THE SHEET PRINTS THESE ONCE PER STYLE ROW, but a style usually has SEVERAL
    rows — one per colourway. So the same fact arrives many times and the rows
    can disagree, which is where a silent import quietly picks whichever row
    happened to be last. Neither field is ever guessed:

      PRICE     first non-empty value wins, and any LATER row that disagrees
                raises a preview warning naming the style and BOTH numbers.
                First-wins rather than max or last because the top row of a
                style block is the one a human wrote deliberately; the rest are
                copies, and a copy that drifted is exactly what the warning is
                for.

      DELIVERY  the EARLIEST date wins, and a disagreement is warned about with
                every date listed. Earliest rather than first because delivery is
                a COMMITMENT: if two rows of one style claim different ship dates
                the factory is bound by the sooner one, and planning to the later
                one would miss it.

    CURRENCY falls back down a chain — the symbol in the cell, then the order,
    then the client — because a bare "83" is a real price whose currency is
    stated once at the top of the paperwork rather than in every cell. When
    nothing in the chain answers, the currency is left NULL and the preview says
    so; inventing one would put a number in the costing with no unit.
    """
    if line.unit_price is not None:
        if style.unit_price is None:
            style.unit_price = line.unit_price
        elif style.unit_price != line.unit_price:
            cp.warnings.append(
                f"{style.name}: row {line.source_row} prices this style at "
                f"{line.unit_price} but an earlier row said {style.unit_price}. "
                f"Kept {style.unit_price} — correct the sheet if that is wrong.")
        if not style.currency:
            style.currency = (line.currency or getattr(order, "currency", None)
                              or getattr(getattr(order, "client", None),
                                         "currency", None))
            if not style.currency:
                cp.warnings.append(
                    f"{style.name}: priced at {line.unit_price} but no currency "
                    f"is stated on the cell, the order or the client. Left NULL "
                    f"rather than assumed.")

    if line.delivery_date is not None:
        if style.delivery_date is None:
            style.delivery_date = line.delivery_date
        elif style.delivery_date != line.delivery_date:
            earliest = min(style.delivery_date, line.delivery_date)
            cp.warnings.append(
                f"{style.name}: rows disagree on the delivery date "
                f"({style.delivery_date} vs {line.delivery_date}). Kept the "
                f"earliest, {earliest} — a delivery date is a commitment, and "
                f"planning to the later one would miss it.")
            style.delivery_date = earliest


def _get_or_create_operation(db: Session, code: str, seq: int) -> Operation:
    op = db.scalar(select(Operation).where(Operation.code == code))
    if not op:
        op = Operation(code=code, label=code.title(), sequence=seq)
        db.add(op); db.flush()
    return op


# def load_preview(db: Session, preview, country_map: dict | None = None,
#                  replace: bool = True) -> dict:
#     """Write a validated preview to the DB. Returns a stats dict.

#     replace=True (default) gives true idempotency: before loading a client we
#     delete its existing styles/SKUs, so re-importing the same file yields the
#     same final numbers instead of doubling them. The within-import summing in
#     _upsert_sku still correctly combines split rows of the SAME import.
#     """
#     country_map = country_map or {}
#     stats = {"clients": 0, "styles": 0, "skus_created": 0, "skus_updated": 0,
#              "operations": 0, "rates": 0}

#     # Stable operation ordering for sequence numbers.
#     OP_SEQ = {"CUTTING":1,"FUSING":2,"PASTING":3,"SHELL":4,"L/A":5,
#               "LINING STICH":6,"FF":7,"FF-SAMPLE":8,"FF-SMS":9,"FF-SAMPLE ":8}

#     op_cache: dict[str, Operation] = {}
#     seen_rates: set = set()        # (style_id, op_id) already inserted this run

#     for key, cp in preview.clients.items():
#         client = _get_or_create_client(db, key, country_map.get(key))
#         stats["clients"] += 1
#         # One PO per client for now (the sheets don't carry PO numbers); use the key.
#         order = _get_or_create_order(db, client, f"{key}-PO")

#         if replace:
#             # Clear this client's prior order data so a re-import replaces, not adds.
#             old_styles = db.scalars(select(Style).where(
#                 Style.client_order_id == order.id)).all()
#             for st in old_styles:
#                 for sk in db.scalars(select(SKU).where(SKU.style_id == st.id)).all():
#                     for ol in db.scalars(                                                # <-- ADD
#                     select(SkuOrderLine).where(SkuOrderLine.sku_id == sk.id)).all():
#                         db.delete(ol)
#                     db.delete(sk)
#                 for rt in db.scalars(select(Rate).where(Rate.style_id == st.id)).all():
#                     db.delete(rt)
#                 db.delete(st)
#             db.flush()

#         # styles + skus from order lines
#         styles_seen = set()
#         for line in cp.order_lines:
#             style = _get_or_create_style(db, order, line.style, line.article)
#             if style.id not in styles_seen:
#                 styles_seen.add(style.id); stats["styles"] += 1
#             for size, qty in line.sizes.items():
#                 sku, res = _upsert_sku(db, order, style, line.color, size, qty)
#                 if res == "created": stats["skus_created"] += 1
#                 elif res == "updated": stats["skus_updated"] += 1
#                 db.add(SkuOrderLine(                                             
#                    sku_id=sku.id, order_date=line.order_date,
#                    qty=qty, source_row=line.source_row))

#         # operations + rates from production cards
#         for card in cp.production_cards:
#             for opcode in card.operations:
#                 if opcode not in op_cache:
#                     op = _get_or_create_operation(db, opcode, OP_SEQ.get(opcode, 99))
#                     op_cache[opcode] = op
#                     stats["operations"] += 1
#             # rates: attach to the matching style if we can find it by title prefix
#             ref_style = None
#             tprefix = card.title.split("-")[0].strip().upper()
#             for st in db.scalars(select(Style).where(Style.client_order_id == order.id)):
#                 if st.name.upper() in card.title.upper() or tprefix in st.name.upper():
#                     ref_style = st; break
#             if ref_style:
#                 for opcode, rate_val in card.rate.items():
#                     if not rate_val:
#                         continue
#                     op = op_cache[opcode]
#                     rate_key = (ref_style.id, op.id)
#                     if rate_key in seen_rates:
#                         continue          # already inserted for this style+op this run
#                     existing = db.scalar(select(Rate).where(
#                         Rate.style_id == ref_style.id, Rate.operation_id == op.id,
#                         Rate.effective_from == date(2026, 1, 1)))
#                     if not existing:
#                         db.add(Rate(style_id=ref_style.id, operation_id=op.id,
#                                     rate=rate_val, effective_from=date(2026, 1, 1)))
#                         db.flush()        # make it visible to the next get-or-create
#                         stats["rates"] += 1
#                     seen_rates.add(rate_key)

#     db.commit()
#     return stats

def _assert_no_production_history(db: Session, order: ClientOrder) -> None:
    """Guard for the replace-on-commit path: refuse to touch an order whose
    SKUs already have ProductionEvent rows, rather than silently deleting
    SKUs still referenced by production history."""
    sku_ids_subq = (
        select(SKU.id)
        .join(Style, SKU.style_id == Style.id)
        .where(Style.client_order_id == order.id)
    )
    has_production = db.scalar(
        select(ProductionEvent.id)
        .where(ProductionEvent.sku_id.in_(sku_ids_subq))
        .limit(1)
    )
    if has_production:
        from fastapi import HTTPException
        raise HTTPException(
            409,
            "This order already has production events logged. "
            "Re-importing would delete SKUs still referenced by production "
            "history. Void or amend the existing import instead of re-uploading.",
        )

def load_preview_into_order(db, preview, *, order_number: str,
                            replace: bool = True) -> dict:
    """Write a parsed breakdown sheet INTO the ClientOrder identified by
    order_number (created at client-creation). SKU codes use that order_number,
    so every SKU traces back to the client + order. Idempotent: replace=True
    clears this order's prior styles/SKUs first."""
    
    from app.modules.production.models import ProductionEvent
    from fastapi import HTTPException          # local import: keep loader fastapi-light
    order_number = (order_number or "").strip()
    order = db.scalar(select(ClientOrder).where(
        ClientOrder.order_number == order_number))
    if not order:                              # backstop; endpoint already checked
        raise HTTPException(
            404, "Order number not found. Please verify with the client record.")

    # ── CONCURRENCY GUARD ────────────────────────────────────────────────────
    # An import of a 1400-piece order is thousands of statements in ONE
    # transaction and takes minutes against a remote pooler. If a second commit
    # for the SAME order starts while the first is still running, it blocks on
    # the first uncommitted unique key (style.code, via ix_style_code) and simply
    # WAITS — until Supabase's statement_timeout (2 min) cancels it and the
    # caller gets an opaque 500:
    #     QueryCanceled: canceling statement due to statement timeout
    #     CONTEXT: while inserting index tuple in relation "ix_style_code"
    # which reads like a performance problem and is actually a lock queue. A
    # client that times out and retries produces this every time.
    #
    # The advisory lock turns that 2-minute hang into an immediate, honest 409.
    # It is TRANSACTION-scoped, so it releases on commit AND on rollback — there
    # is nothing to leak and nothing to clean up.
    _acquire_order_import_lock(db, order_number)

    OP_SEQ = {"CUTTING":1,"FUSING":2,"PASTING":3,"SHELL":4,"L/A":5,
              "LINING STICH":6,"FF":7,"FF-SAMPLE":8,"FF-SMS":9,"FF-SAMPLE ":8}
    stats = {"order_number": order_number, "styles": 0,
             "skus_created": 0, "skus_updated": 0, "operations": 0, "rates": 0}
    op_cache: dict[str, Operation] = {}
    seen_rates: set = set()

    if replace:
        # Guard: never destroy an order that already has production logged against it.
        sku_ids_subq = (
            select(SKU.id)
            .join(Style, SKU.style_id == Style.id)
            .where(Style.client_order_id == order.id)
        )
        has_production = db.scalar(
            select(ProductionEvent.id)
            .where(ProductionEvent.sku_id.in_(sku_ids_subq))
            .limit(1)
        )
        if has_production:
            raise HTTPException(
                409,
                "This order already has production events logged. "
                "Re-importing would delete SKUs still referenced by production history. "
                "Void or amend the existing import instead of re-uploading.",
            )
        for st in db.scalars(select(Style).where(
                Style.client_order_id == order.id)).all():
            for sk in db.scalars(select(SKU).where(SKU.style_id == st.id)).all():
                for ol in db.scalars(select(SkuOrderLine).where(
                        SkuOrderLine.sku_id == sk.id)).all():
                    db.delete(ol)
                db.delete(sk)
            for rt in db.scalars(select(Rate).where(Rate.style_id == st.id)).all():
                db.delete(rt)
            db.delete(st)
        db.flush()

    styles_seen = set()
    for cp in preview.clients.values():        # flatten all parsed lines → this order
        for line in cp.order_lines:
            style = _get_or_create_style(db, order, line.style, line.article)
            if style.id not in styles_seen:
                styles_seen.add(style.id); stats["styles"] += 1
            _apply_style_commercials(style, line, order, cp)
            for size, qty in line.sizes.items():
                sku, res = _upsert_sku(db, order, style, line.color, size, qty)
                if res == "created": stats["skus_created"] += 1
                elif res == "updated": stats["skus_updated"] += 1
                db.add(SkuOrderLine(sku_id=sku.id, order_date=line.order_date,
                                    qty=qty, source_row=line.source_row))

        for card in cp.production_cards:
            for opcode in card.operations:
                if opcode not in op_cache:
                    op_cache[opcode] = _get_or_create_operation(
                        db, opcode, OP_SEQ.get(opcode, 99))
                    stats["operations"] += 1
            tprefix = card.title.split("-")[0].strip().upper()
            ref_style = None
            for st in db.scalars(select(Style).where(
                    Style.client_order_id == order.id)):
                if st.name.upper() in card.title.upper() or tprefix in st.name.upper():
                    ref_style = st; break
            if ref_style:
                for opcode, rate_val in card.rate.items():
                    if not rate_val:
                        continue
                    op = op_cache[opcode]
                    if (ref_style.id, op.id) in seen_rates:
                        continue
                    existing = db.scalar(select(Rate).where(
                        Rate.style_id == ref_style.id, Rate.operation_id == op.id,
                        Rate.effective_from == date(2026, 1, 1)))
                    if not existing:
                        db.add(Rate(style_id=ref_style.id, operation_id=op.id,
                                    rate=rate_val, effective_from=date(2026, 1, 1)))
                        db.flush(); stats["rates"] += 1
                    seen_rates.add((ref_style.id, op.id))
                    
    # ── NO PRE-MINT HERE ANY MORE (change-list item 9) ───────────────────────
    # This used to call premint_order(db, order) and mint a barcode + a drawer
    # for every ordered unit of every SKU, on upload. It no longer does.
    #
    # WHY: uploading a breakdown sheet is not a decision to produce it. The DM
    # reviews and corrects the sheet first, then RELEASES the styles that are
    # actually going to the floor. Minting on upload burned per-piece barcodes
    # and consumed drawers from a 200-drawer pool for styles nobody had agreed to
    # cut yet, and there was no way to take it back — a piece barcode is a
    # permanent garment identity.
    #
    # The mint now happens in BreakdownService.release_styles, which calls
    # premint_order with the DM's chosen style_ids inside an audited transition.
    # Styles land here as DRAFT (Style.production_status defaults to it).
    stats["release_required"] = True
    stats["pieces_minted"] = 0

    db.commit()
    return stats

