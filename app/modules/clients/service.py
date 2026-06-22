"""
================================================================================
modules/clients/service.py — Business logic for clients/orders (async)
================================================================================
The PUBLIC interface other modules call (production, analytics) — they go
through this service, never the repository directly. This keeps module
boundaries clean so a module can later be lifted into its own service.
================================================================================
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.clients.repository import ClientRepository


class ClientService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ClientRepository(db)

    async def list_clients(self) -> list[Client]:
        return await self.repo.list_clients()

    async def create_client(self, name: str, country: str | None) -> Client:
        return await self.repo.create_client(name, country)

    async def get_client_orders(self, client_id: uuid.UUID) -> list[ClientOrder]:
        return await self.repo.get_orders_for_client(client_id)

    async def get_style(self, style_id: uuid.UUID) -> Style | None:
        return await self.repo.get_style(style_id)

    async def get_order(self, order_id: uuid.UUID) -> ClientOrder | None:
        return await self.repo.get_order(order_id)

    # Public interface other modules rely on:
    async def get_sku(self, sku_id: uuid.UUID) -> SKU | None:
        return await self.repo.get_sku(sku_id)

    async def get_skus_for_style(self, style_id: uuid.UUID) -> list[SKU]:
        return await self.repo.get_skus_for_style(style_id)

    async def create_order_with_breakdown(
        self, *, client_id: uuid.UUID, order: dict, style: dict,
        lines: list[dict] | None = None, per_size: dict | None = None,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """Materialise a ClientOrder + Style + SKU breakdown in one shot — the public
        entry point bom.service calls at MD approval, once the order+spec sheets have been
        signed off (clients owns these tables, so the write lives here). `lines` is the
        parsed per-colour breakdown ([{color_code, color_name, sizes}]); `per_size` is the
        aggregate fallback when no colour dimension was parsed. Returns (order_id, style_id)."""
        return await self.repo.create_order_with_breakdown(
            client_id=client_id, order=order, style=style,
            lines=lines or [], per_size=per_size or {})
