"""
================================================================================
modules/clients/service.py — Business logic for clients/orders (async)
================================================================================
The PUBLIC interface other modules call (production, analytics) — they go
through this service, never the repository directly. This keeps module
boundaries clean so a module can later be lifted into its own service.
================================================================================
"""
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.clients.repository import ClientRepository
from fastapi import HTTPException, status          # add
from sqlalchemy.exc import IntegrityError


def _slug(s: str | None) -> str:
    """UPPER, runs of non-alnum -> single '_', trimmed. '' -> 'NA'."""
    out = re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_").upper()
    return out or "NA"
 
 
def make_sku_code(order_number: str | None, style_name: str | None,
                  colour: str | None, size: str | None) -> str:
    """Deterministic, globally-unique, readable SKU code.
    e.g. make_sku_code('JP','CLERMONT + VEST','DARK BROWN','46')
         -> 'JP-CLERMONT_VEST-DARK_BROWN-46'
    Deterministic => idempotent across re-imports (survives delete+recreate)."""
    return "-".join((
        _slug(order_number), _slug(style_name), _slug(colour), _slug(size),
    ))
 
 
# sku_label (display name) is UNCHANGED — keep it:
def sku_label(style_name, color_name, color_code, size) -> str:
    colour = color_name or color_code or "NA"
    return " · ".join(p for p in (style_name or "NA", colour, size or "NA"))

# app/modules/clients/service.py
from sqlalchemy import select
from app.modules.clients.models import ClientOrder

class ClientService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ClientRepository(db)
        
    def get_order_by_number(self, order_number: str) -> ClientOrder | None:
        return self.repo.get_order_by_number(order_number)

    def sku_label(self, style_name: str | None, color_name: str | None,
                  color_code: str | None, size: str | None) -> str:
        return sku_label(style_name, color_name, color_code, size)

    async def list_clients(self) -> list[Client]:
        return await self.repo.list_clients()

    async def add_order(self, *, client_id: uuid.UUID, order_number: str,
                        order_date=None, delivery_deadline=None,
                        sea_cutoff_date=None, ship_mode: str = "sea",
                        currency=None, agent=None, line=None) -> ClientOrder:
        order_number = (order_number or "").strip()
        if not order_number:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "order_number is required")
        if not await self.repo.get_client(client_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
        if await self.repo.get_order_by_number(order_number):     # friendly pre-check
            raise HTTPException(status.HTTP_409_CONFLICT,
                f"Order number '{order_number}' already exists. Choose a unique one.")
        try:
            return await self.repo.create_order(
                client_id=client_id, order_number=order_number, order_date=order_date,
                delivery_deadline=delivery_deadline, sea_cutoff_date=sea_cutoff_date,
                ship_mode=ship_mode, currency=currency, agent=agent, line=line)
        except IntegrityError:                                    # race-safe backstop
            await self.db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT,
                f"Order number '{order_number}' already exists. Choose a unique one.")
    
    async def create_client(self, name: str, country: str | None,
                            order_number: str) -> tuple[Client, ClientOrder]:
        order_number = (order_number or "").strip()
        if not order_number:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "order_number is required")
        # Friendly pre-check; DB constraint is the hard guarantee (race-safe).
        if await self.repo.get_order_by_number(order_number):
            raise HTTPException(status.HTTP_409_CONFLICT,
                f"Order number '{order_number}' already exists. Choose a unique one.")
        try:
            return await self.repo.create_client_with_order(
                name=name, country=country, order_number=order_number)
        except IntegrityError:
            await self.db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT,
                f"Order number '{order_number}' already exists. Choose a unique one.")

    async def get_client_orders(self, client_id: uuid.UUID) -> list[ClientOrder]:
        return await self.repo.get_orders_for_client(client_id)

    async def get_style(self, style_id: uuid.UUID) -> Style | None:
        return await self.repo.get_style(style_id)

    async def get_order(self, order_id: uuid.UUID) -> ClientOrder | None:
        return await self.repo.get_order(order_id)

    # Public interface other modules rely on:
    async def get_sku(self, sku_id: uuid.UUID) -> SKU | None:
        return await self.repo.get_sku(sku_id)
    async def get_sku_by_code(self, sku_code: str) -> SKU | None:
        return await self.repo.get_sku_by_code(sku_code)

    async def is_sku_visible_to_client(self, sku_id: uuid.UUID,
                                       client_id: uuid.UUID) -> bool:
        return await self.repo.is_sku_visible_to_client(sku_id, client_id)

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
            ctx["label"] = sku_label(
                ctx["style_name"], ctx["color_name"], ctx["color_code"], ctx["size"]
            )
        return ctx
 
    async def list_sku_options(self, *, order_id=None, style_id=None,
                               client_scope: uuid.UUID | None = None) -> list[dict]:
        rows = await self.repo.list_sku_options(
            order_id=order_id, style_id=style_id, client_scope=client_scope)
        for r in rows:
            r["label"] = sku_label(
                r["style_name"], r["color_name"], r["color_code"], r["size"]
            )
        return rows
    
    async def get_style_by_code(self, code: str) -> Style | None:
        return await self.repo.get_style_by_code(code)

    async def get_style_summary_by_code(self, code: str) -> dict | None:
        return await self.repo.get_style_summary_by_code(code)

    async def get_style_codes(self, style_ids: list[uuid.UUID]) -> dict:
        return await self.repo.get_style_codes(style_ids)

    async def list_style_options(self, *, order_number=None, client_id=None) -> list[dict]:
        return await self.repo.list_style_options(
            order_number=order_number, client_id=client_id)