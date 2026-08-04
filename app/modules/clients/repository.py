"""
================================================================================
modules/clients/repository.py — Async data access for the order hierarchy
================================================================================
Only this module touches Client / ClientOrder / Style / SKU. All methods are
async and operate on an AsyncSession. selectinload is used for eager-loading
the styles->skus tree so the API can return a full order in one round trip
without triggering lazy loads (which are unsafe under async).
================================================================================
"""
import uuid

from sqlalchemy import select ,func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.clients.utlis import make_sku_code,make_style_code


class ClientRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_clients(self) -> list[Client]:
        res = await self.db.execute(select(Client).order_by(Client.name))
        return list(res.scalars())

    async def get_client(self, client_id: uuid.UUID) -> Client | None:
        return await self.db.get(Client, client_id)

    async def create_client(self, name: str, country: str | None) -> Client:
        c = Client(name=name, country=country)
        self.db.add(c)
        await self.db.commit()
        await self.db.refresh(c)
        return c

    async def get_orders_for_client(self, client_id: uuid.UUID) -> list[ClientOrder]:
        stmt = (
            select(ClientOrder)
            .where(ClientOrder.client_id == client_id)
            .options(selectinload(ClientOrder.styles).selectinload(Style.skus))
            .order_by(ClientOrder.order_number)
        )
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def get_style(self, style_id: uuid.UUID) -> Style | None:
        return await self.db.get(Style, style_id)

    async def get_order(self, order_id: uuid.UUID) -> ClientOrder | None:
        return await self.db.get(ClientOrder, order_id)

    async def get_sku(self, sku_id: uuid.UUID) -> SKU | None:
        return await self.db.get(SKU, sku_id)

    async def get_skus_for_style(self, style_id: uuid.UUID) -> list[SKU]:
        res = await self.db.execute(select(SKU).where(SKU.style_id == style_id))
        return list(res.scalars())

    async def create_order(self, *, client_id: uuid.UUID, order_number: str,
                           order_date=None, delivery_deadline=None,
                           sea_cutoff_date=None, ship_mode: str = "sea",
                           currency: str | None = None, agent: str | None = None,
                           line: str | None = None) -> ClientOrder:
        co = ClientOrder(
            client_id=client_id, order_number=order_number, order_date=order_date,
            delivery_deadline=delivery_deadline, sea_cutoff_date=sea_cutoff_date,
            ship_mode=ship_mode or "sea", currency=currency, agent=agent, line=line)
        self.db.add(co)
        await self.db.commit()
        await self.db.refresh(co)
        return co
    
    async def create_order_with_breakdown(
        self, *, client_id: uuid.UUID, order: dict, style: dict,
        lines: list[dict], per_size: dict,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """Insert ClientOrder → Style → SKU rows from a parsed order sheet. SKUs are keyed
        on (style_id, color_code, size) — duplicate (colour, size) cells are summed so the
        uq_sku_identity constraint never trips."""
        co = ClientOrder(
            client_id=client_id,
            order_number=str(order.get("order_number") or "UNKNOWN")[:50],
            currency=order.get("currency"),
            source_document_id=order.get("source_document_id"),
        )
        self.db.add(co)
        await self.db.flush()
        st = Style(
            client_order_id=co.id, name=str(style.get("name") or "UNSPECIFIED")[:120],
            customer_ref=style.get("customer_ref"), internal_ref=style.get("internal_ref"),
            season=style.get("season"), unit_price=style.get("unit_price"),
            currency=style.get("currency"), code=make_style_code(co.order_number, str(style.get("name") or "UNSPECIFIED")[:120]),
        )
        self.db.add(st)
        await self.db.flush()

        # Aggregate to (color_code, color_name, size) → qty so duplicate cells don't
        # violate uq_sku_identity. Fall back to a single "NA" colour from per_size.
        agg: dict[tuple[str, str | None, str], int] = {}
        for ln in (lines or []):
            color_name = ln.get("color_name") or ln.get("color")
            color_code = str(ln.get("color_code") or color_name or "NA")[:40]
            for size, qty in (ln.get("sizes") or {}).items():
                key = (color_code, color_name, str(size)[:10])
                agg[key] = agg.get(key, 0) + int(qty)
        if not agg:
            for size, qty in (per_size or {}).items():
                agg[("NA", None, str(size)[:10])] = int(qty)
        for (color_code, color_name, size), qty in agg.items():
            self.db.add(SKU(style_id=st.id, color_code=color_code,
                            color_name=color_name, size=size, qty_ordered=int(qty),
                            # F116: order is a dict — order.order_number raised
                            # AttributeError on every order that produced ≥1 SKU
                            # (i.e. every real order). Use the persisted ORM value,
                            # matching make_style_code above which already uses co.
                            code=make_sku_code(co.order_number, st.name,
                                color_name or color_code, size),))
        await self.db.commit()
        return co.id, st.id
    
    async def sku_label_context(self, sku_id) -> dict | None:
        row = (await self.db.execute(
            select(
                SKU.id, SKU.code, ClientOrder.order_number, Style.name,
                SKU.color_code, SKU.color_name, SKU.size, SKU.qty_ordered,
            )
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(SKU.id == sku_id)
        )).first()
        if not row:
            return None
        _id, code, order_number, style_name, color_code, color_name, size, qty = row
        return {
            "sku_id": _id, "code": code, "order_number": order_number,
            "style_name": style_name, "color_code": color_code,
            "color_name": color_name, "size": size, "qty_ordered": int(qty or 0),
        }
 
    async def list_sku_options(self, *, order_id=None, style_id=None,
                               client_scope: uuid.UUID | None = None) -> list[dict]:
        stmt = (
            select(
                SKU.id, SKU.code, ClientOrder.order_number, Style.name,
                SKU.color_code, SKU.color_name, SKU.size, SKU.qty_ordered,
            )
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
        )
        if client_scope is not None:
            stmt = stmt.where(ClientOrder.client_id == client_scope)
        if order_id:
            stmt = stmt.where(Style.client_order_id == order_id)
        if style_id:
            stmt = stmt.where(SKU.style_id == style_id)
        stmt = stmt.order_by(ClientOrder.order_number, Style.name, SKU.code)
        rows = (await self.db.execute(stmt)).all()
        return [
            {
                "sku_id": r[0], "code": r[1], "order_number": r[2], "style_name": r[3],
                "color_code": r[4], "color_name": r[5], "size": r[6],
                "qty_ordered": int(r[7] or 0),
            }
            for r in rows
        ]
        
    async def is_sku_visible_to_client(self, sku_id: uuid.UUID,
                                       client_id: uuid.UUID) -> bool:
        res = await self.db.execute(
            select(SKU.id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(SKU.id == sku_id, ClientOrder.client_id == client_id)
            .limit(1)
        )
        return res.scalar_one_or_none() is not None

    async def get_sku_by_code(self, code: str):
        from app.modules.clients.models import SKU
        res = await self.db.execute(
            select(SKU).where(SKU.code == (code or "").strip().upper())
        )
        return res.scalar_one_or_none()
 
    async def create_client_with_order(
        self, *, name: str, country: str | None, order_number: str,
    ) -> tuple[Client, ClientOrder]:
        """Client + its first ClientOrder in one transaction. The unique
        constraint on order_number is the real guard (see IntegrityError catch
        in the service)."""
        c = Client(name=name, country=country)
        self.db.add(c)
        await self.db.flush()                     # assign c.id
        co = ClientOrder(client_id=c.id, order_number=order_number)
        self.db.add(co)
        await self.db.commit()
        await self.db.refresh(c)
        await self.db.refresh(co)
        return c, co

    async def get_order_by_number(self, order_number: str) -> ClientOrder | None:
        res = await self.db.execute(
            select(ClientOrder).where(
                ClientOrder.order_number == (order_number or "").strip()))
        return res.scalar_one_or_none()
    
    async def get_style_by_code(self, code: str) -> Style | None:
        res = await self.db.execute(select(Style).where(Style.code == code))
        return res.scalar_one_or_none()

    async def get_style_summary_by_code(self, code: str) -> dict | None:
        """Resolve a style code -> ids + display fields, in one query.

        NOT get_style_by_code() + style.client_order.order_number: client_order is a
        lazy relationship and touching it on an AsyncSession raises MissingGreenlet.
        The join has to be explicit.
        """
        row = (await self.db.execute(
            select(Style.id, Style.code, Style.name, ClientOrder.order_number)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(Style.code == code)
        )).first()
        if not row:
            return None
        return {"style_id": row[0], "style_code": row[1],
                "style_name": row[2], "order_number": row[3]}

    async def get_style_codes(self, style_ids: list[uuid.UUID]) -> dict:
        """{style_id: {style_code, style_name}} for a batch of ids.

        Used to turn compute_run's unrated-operation warnings into codes the
        frontend can link on. Raw UUIDs there make the warning unactionable — the
        manager sees 340 pieces went unpaid with no route to the rate sheet.
        """
        if not style_ids:
            return {}
        rows = (await self.db.execute(
            select(Style.id, Style.code, Style.name).where(Style.id.in_(style_ids))
        )).all()
        return {r[0]: {"style_code": r[1], "style_name": r[2]} for r in rows}

    async def list_style_options(
        self, *, order_number: str | None = None, client_id: uuid.UUID | None = None
    ) -> list[dict]:
        """Styles as picker options, with SKU counts.

        Filters on order_number, not order_id — the caller is a UI that speaks
        codes. style_id is returned for the caller's internal joins (wages needs it
        to count rates) and is stripped before it reaches the response model.

        outerjoin, not join — a style whose SKUs have not been imported yet must
        still appear, or its rates can never be set.
        """
        stmt = (
            select(
                Style.id, Style.code, Style.name, Style.article,
                ClientOrder.order_number,
                func.count(SKU.id),
                func.coalesce(func.sum(SKU.qty_ordered), 0),
            )
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .outerjoin(SKU, SKU.style_id == Style.id)
            .where(Style.code.is_not(None))
            .group_by(Style.id, Style.code, Style.name, Style.article,
                      ClientOrder.order_number)
        )
        if order_number:
            stmt = stmt.where(ClientOrder.order_number == order_number)
        if client_id:
            stmt = stmt.where(ClientOrder.client_id == client_id)
        stmt = stmt.order_by(ClientOrder.order_number, Style.name)
        return [
            {"style_id": r[0], "style_code": r[1], "style_name": r[2],
             "article": r[3], "order_number": r[4],
             "sku_count": int(r[5]), "qty_ordered": int(r[6])}
            for r in (await self.db.execute(stmt)).all()
        ]