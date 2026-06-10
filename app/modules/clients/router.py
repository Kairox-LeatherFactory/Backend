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


@router.post("", response_model=schemas.ClientRead, status_code=201)
async def create_client(
    body: schemas.ClientCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    return await ClientService(db).create_client(body.name, body.country)


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
