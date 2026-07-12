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


def sku_label(style_name: str | None, color_name: str | None,
              color_code: str | None, size: str | None) -> str:
    """Human-friendly SKU name: 'CLERMONT · PINE GREEN · M'.
    Falls back to color_code when a colour name wasn't parsed.

    Module-level so other modules (e.g. analytics) can reuse it without
    constructing a ClientService; ClientService.sku_label delegates here."""
    colour = color_name or color_code or "NA"
    return " · ".join(p for p in (style_name or "NA", colour, size or "NA"))


class ClientService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ClientRepository(db)

    def sku_label(self, style_name: str | None, color_name: str | None,
                  color_code: str | None, size: str | None) -> str:
        return sku_label(style_name, color_name, color_code, size)

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
        
    async def sku_label_context(self, sku_id) -> dict | None:
        ctx = await self.repo.sku_label_context(sku_id)
        if ctx:
            ctx["label"] = self.sku_label(
                ctx["style_name"], ctx["color_name"], ctx["color_code"], ctx["size"]
            )
        return ctx
 
    async def list_sku_options(self, *, order_id=None, style_id=None) -> list[dict]:
        rows = await self.repo.list_sku_options(order_id=order_id, style_id=style_id)
        for r in rows:
            r["label"] = self.sku_label(
                r["style_name"], r["color_name"], r["color_code"], r["size"]
            )
        return rows