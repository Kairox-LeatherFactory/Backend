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

    async def get_sku(self, sku_id: uuid.UUID) -> SKU | None:
        return await self.db.get(SKU, sku_id)

    async def get_skus_for_style(self, style_id: uuid.UUID) -> list[SKU]:
        res = await self.db.execute(select(SKU).where(SKU.style_id == style_id))
        return list(res.scalars())
