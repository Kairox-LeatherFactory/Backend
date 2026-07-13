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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.clients.service import make_sku_code


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
            currency=style.get("currency"),
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
                            code=make_sku_code(order.order_number, st.name, 
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
 
    async def list_sku_options(self, *, order_id=None, style_id=None) -> list[dict]:
        stmt = (
            select(
                SKU.id, SKU.code, ClientOrder.order_number, Style.name,
                SKU.color_code, SKU.color_name, SKU.size, SKU.qty_ordered,
            )
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
        )
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
        
    async def get_sku_by_code(self, code: str):
        from app.modules.clients.models import SKU
        res = await self.db.execute(
            select(SKU).where(SKU.code == (code or "").strip().upper())
        )
        return res.scalar_one_or_none()
 
