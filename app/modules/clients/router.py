"""
================================================================================
modules/clients/router.py — HTTP API for clients & their orders (async)
================================================================================
Grouped by client. Listing is open to any authenticated user; creating a client
is restricted to the direct manager. A CLIENT-role user should only see their
own orders — that scoping is enforced here by comparing the resolved user's
client_id to the requested client_id.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.users.deps import get_current_user, require_roles
from app.modules.clients.service import ClientService
from app.modules.clients import schemas
from app.modules.users.models import User

router = APIRouter(prefix="/clients", tags=["Clients"])


@router.get("", response_model=list[schemas.ClientRead])
async def list_clients(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await ClientService(db).list_clients()


@router.post("", response_model=schemas.CreatedClientRead, status_code=201)
async def create_client(
    body: schemas.ClientCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    client, order = await ClientService(db).create_client(
        body.name, body.country, body.order_number)
    return schemas.CreatedClientRead(
        id=client.id, name=client.name, country=client.country,
        code=client.code, currency=client.currency,
        default_size_system=client.default_size_system,
        order_number=order.order_number, order_id=order.id)



@router.get("/{client_id}/orders", response_model=list[schemas.ClientOrderRead])
async def client_orders(
    client_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Client-role users may only view their own orders.
    if user.role == UserRole.CLIENT and user.client_id != client_id:
        raise HTTPException(403, "Clients may only view their own orders")
    return await ClientService(db).get_client_orders(client_id)

@router.post("/{client_id}/orders", response_model=schemas.ClientOrderRead,
             status_code=201)
async def add_order(
    client_id: uuid.UUID,
    body: schemas.ClientOrderCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Add another order (new unique order_number) to an existing client."""
    order = await ClientService(db).add_order(client_id=client_id, **body.model_dump())
    # Build the response explicitly — a fresh order has no styles, and touching
    # order.styles here would trigger an async-unsafe lazy load.
    return schemas.ClientOrderRead(
        id=order.id, order_number=order.order_number, order_date=order.order_date,
        delivery_deadline=order.delivery_deadline,
        sea_cutoff_date=order.sea_cutoff_date, ship_mode=order.ship_mode,
        currency=order.currency, agent=order.agent, line=order.line, styles=[])
    
@router.get("/styles", response_model=list[schemas.StyleOption])
async def list_styles(
    order_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await ClientService(db).list_style_options(order_id=order_id, client_id=client_id)