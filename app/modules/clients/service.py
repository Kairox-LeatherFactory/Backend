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


# THE CODE MAKERS LIVE IN utlis.py — re-exported here, never redefined.
# This module used to carry its own `_slug` + `make_sku_code`, byte-identical to
# the pair in utlis.py, and imports/load_to_db.py imported THIS copy while
# clients/repository.py imported the other. A format change (adding the article
# segment) would have landed in one import path and not the other, so half the
# codes in one upload would carry the article and half would not.
from app.modules.clients.utlis import (        # noqa: F401  (re-export)
    _slug, make_sku_code, make_style_code, sku_label,
)

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

    async def list_clients(self, *, include_inactive: bool = False,
                           only_client_id=None) -> list[Client]:
        return await self.repo.list_clients(
            include_inactive=include_inactive, only_client_id=only_client_id)

    async def page_clients(self, params, *, include_inactive: bool = False,
                           only_client_id=None) -> tuple[list, int]:
        return await self.repo.page_clients(
            params, include_inactive=include_inactive,
            only_client_id=only_client_id)

    async def get_client(self, client_id: uuid.UUID) -> Client:
        client = await self.repo.get_client(client_id)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
        return client

    async def update_client(self, client_id: uuid.UUID, data: dict) -> Client:
        """Partial update of a client. Only the keys the caller SENT are applied
        (the router passes `exclude_unset=True`), so omitting a field leaves it
        alone and sending it as null clears it — the two are different requests.

        `code` is unique across clients, so a collision is a 409 and not a 500.
        The friendly pre-check races, exactly as it does on order_number, so the
        IntegrityError backstop is the real guarantee.
        """
        client = await self.get_client(client_id)
        if not data:
            return client
        new_code = (data.get("code") or "").strip() or None
        if "code" in data:
            data["code"] = new_code
        if new_code and new_code != client.code:
            clash = await self.repo.get_client_by_code(new_code)
            if clash and clash.id != client_id:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Client code '{new_code}' is already used by another client.")
        if "name" in data and data["name"] is not None:
            data["name"] = data["name"].strip()
            if not data["name"]:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "name cannot be blank")
        try:
            return await self.repo.update_client(client, data)
        except IntegrityError:
            await self.db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Client code '{new_code}' is already used by another client.")

    async def delete_client(self, client_id: uuid.UUID) -> None:
        """Hard-delete a client that has produced NOTHING.

        FIXED 2026-09-19 (Hamthan): the guard used to be "has no orders", and
        that made this endpoint impossible to pass. `POST /clients` does not
        create a client — it creates a client AND its first order in one call
        (order_number is a required field on the body, see create_client
        below). So every client born through the API owns exactly one order
        from the moment it exists, and DELETE answered "has 1 order(s)" for a
        row created seconds earlier that had never been near the factory. The
        mistyped-name client the endpoint was written for was the one case it
        could never actually serve.

        THE GUARD NOW COUNTS PIECES, which is what the old error message was
        really talking about ("would destroy their styles, pieces and
        production history"). A piece is the tracked unit (CLAUDE.md §4): its
        code is a printed barcode and every production event, inspection and
        piece-rate wage line hangs off it. No pieces means nothing was ever
        cut, so the cascade takes an empty order shell and nothing else. One
        piece means the client has traded, and then this is the same rule the
        barcode module applies to a worker who leaves (CLAUDE.md §6) — you
        delete the scannable code, never the record — so it stays a 409 and the
        answer stays `PATCH /clients/{id} {"is_active": false}`.

        THE PIECE COUNT IS NOT THE WHOLE STORY, which is why the IntegrityError
        arm exists. wage_style_rate, supplier_po_line and the cutting tables all
        reference `style.id` with plain FKs, so a client with no pieces but with
        (say) a style rate card still cannot be cascaded away. The database is
        the real authority; this turns its refusal into a 409 a user can act on
        instead of a 500.
        """
        client = await self.get_client(client_id)
        pieces = await self.repo.count_pieces_for_client(client_id)
        if pieces:
            orders = await self.repo.count_orders_for_client(client_id)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"'{client.name}' has {pieces} garment(s) in production across "
                f"{orders} order(s) and cannot be deleted — their barcodes are "
                f"printed and their production and wage history hangs off them. "
                f"Deactivate instead: "
                f'PATCH /clients/{client_id} {{"is_active": false}}.')
        try:
            await self.repo.delete_client(client)
        except IntegrityError:
            await self.db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"'{client.name}' has no garments in production, but other "
                f"records (a style rate, a supplier PO, a cutting entry) still "
                f"reference their styles, so the row cannot be removed. "
                f"Deactivate instead: "
                f'PATCH /clients/{client_id} {{"is_active": false}}.')

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

    async def page_client_orders(self, client_id: uuid.UUID, params):
        return await self.repo.page_orders_for_client(client_id, params)

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

    async def is_style_visible_to_client(self, style_id: uuid.UUID,
                                         client_id: uuid.UUID) -> bool:
        return await self.repo.is_style_visible_to_client(style_id, client_id)

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

    async def page_sku_options(self, params, *, order_id=None, style_id=None,
                               client_scope=None):
        """One page of the SKU picker.

        `label` is composed HERE, exactly as list_sku_options does — it is a
        required field on SkuOption and lives nowhere in the database. Paging
        must not become a second path that skips it.
        """
        rows, total = await self.repo.page_sku_options(
            params, order_id=order_id, style_id=style_id,
            client_scope=client_scope)
        for r in rows:
            r["label"] = sku_label(
                r["style_name"], r["color_name"], r["color_code"], r["size"]
            )
        return rows, total

    async def list_style_options(self, *, order_number=None, client_id=None) -> list[dict]:
        return await self.repo.list_style_options(
            order_number=order_number, client_id=client_id)

    async def page_style_options(self, params, *, order_number=None,
                                 client_id=None):
        return await self.repo.page_style_options(
            params, order_number=order_number, client_id=client_id)

    async def style_ids_for_order(self, order_id: uuid.UUID) -> list[uuid.UUID]:
        return await self.repo.style_ids_for_order(order_id)
