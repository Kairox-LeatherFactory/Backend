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

from app.modules.clients.models import Client, ClientOrder, Style, SKU,SkuOrderLine
from app.modules.production.models import Operation, ProductionEvent
from app.modules.wages.models import Rate
from app.modules.clients.service import make_sku_code


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
        st = Style(client_order_id=order.id, name=name, article=article)
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
        code=make_sku_code(order.order_number, style.name, color or color_code, size),
    )
    db.add(sku)
    db.flush()          # visible to the next lookup, and gives sku.id for lines
    return sku, "created"


def _get_or_create_operation(db: Session, code: str, seq: int) -> Operation:
    op = db.scalar(select(Operation).where(Operation.code == code))
    if not op:
        op = Operation(code=code, label=code.title(), sequence=seq)
        db.add(op); db.flush()
    return op


def load_preview(db: Session, preview, country_map: dict | None = None,
                 replace: bool = True) -> dict:
    """Write a validated preview to the DB. Returns a stats dict.

    replace=True (default) gives true idempotency: before loading a client we
    delete its existing styles/SKUs, so re-importing the same file yields the
    same final numbers instead of doubling them. The within-import summing in
    _upsert_sku still correctly combines split rows of the SAME import.
    """
    country_map = country_map or {}
    stats = {"clients": 0, "styles": 0, "skus_created": 0, "skus_updated": 0,
             "operations": 0, "rates": 0}

    # Stable operation ordering for sequence numbers.
    OP_SEQ = {"CUTTING":1,"FUSING":2,"PASTING":3,"SHELL":4,"L/A":5,
              "LINING STICH":6,"FF":7,"FF-SAMPLE":8,"FF-SMS":9,"FF-SAMPLE ":8}

    op_cache: dict[str, Operation] = {}
    seen_rates: set = set()        # (style_id, op_id) already inserted this run

    for key, cp in preview.clients.items():
        client = _get_or_create_client(db, key, country_map.get(key))
        stats["clients"] += 1
        # One PO per client for now (the sheets don't carry PO numbers); use the key.
        order = _get_or_create_order(db, client, f"{key}-PO")

        if replace:
            # Clear this client's prior order data so a re-import replaces, not adds.
            old_styles = db.scalars(select(Style).where(
                Style.client_order_id == order.id)).all()
            for st in old_styles:
                for sk in db.scalars(select(SKU).where(SKU.style_id == st.id)).all():
                    for ol in db.scalars(                                                # <-- ADD
                    select(SkuOrderLine).where(SkuOrderLine.sku_id == sk.id)).all():
                        db.delete(ol)
                    db.delete(sk)
                for rt in db.scalars(select(Rate).where(Rate.style_id == st.id)).all():
                    db.delete(rt)
                db.delete(st)
            db.flush()

        # styles + skus from order lines
        styles_seen = set()
        for line in cp.order_lines:
            style = _get_or_create_style(db, order, line.style, line.article)
            if style.id not in styles_seen:
                styles_seen.add(style.id); stats["styles"] += 1
            for size, qty in line.sizes.items():
                sku, res = _upsert_sku(db, order, style, line.color, size, qty)
                if res == "created": stats["skus_created"] += 1
                elif res == "updated": stats["skus_updated"] += 1
                db.add(SkuOrderLine(                                             
                   sku_id=sku.id, order_date=line.order_date,
                   qty=qty, source_row=line.source_row))

        # operations + rates from production cards
        for card in cp.production_cards:
            for opcode in card.operations:
                if opcode not in op_cache:
                    op = _get_or_create_operation(db, opcode, OP_SEQ.get(opcode, 99))
                    op_cache[opcode] = op
                    stats["operations"] += 1
            # rates: attach to the matching style if we can find it by title prefix
            ref_style = None
            tprefix = card.title.split("-")[0].strip().upper()
            for st in db.scalars(select(Style).where(Style.client_order_id == order.id)):
                if st.name.upper() in card.title.upper() or tprefix in st.name.upper():
                    ref_style = st; break
            if ref_style:
                for opcode, rate_val in card.rate.items():
                    if not rate_val:
                        continue
                    op = op_cache[opcode]
                    rate_key = (ref_style.id, op.id)
                    if rate_key in seen_rates:
                        continue          # already inserted for this style+op this run
                    existing = db.scalar(select(Rate).where(
                        Rate.style_id == ref_style.id, Rate.operation_id == op.id,
                        Rate.effective_from == date(2026, 1, 1)))
                    if not existing:
                        db.add(Rate(style_id=ref_style.id, operation_id=op.id,
                                    rate=rate_val, effective_from=date(2026, 1, 1)))
                        db.flush()        # make it visible to the next get-or-create
                        stats["rates"] += 1
                    seen_rates.add(rate_key)

    db.commit()
    return stats
