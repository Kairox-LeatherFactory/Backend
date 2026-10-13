"""
================================================================================
modules/users/router.py — Auth + user-management HTTP endpoints
================================================================================

ENDPOINTS
  Auth (mounted at /api/v1/auth):
    POST /login            phone+password -> JWT  (accepts JSON or OAuth2 form,
                           so Swagger's Authorize button works too)
    GET  /me               the current authenticated user
    POST /change-password  self-service password change

  User management (mounted at /api/v1/users, direct-manager only):
    GET  /                 list all logins
    POST /                 create a staff / manager / viewer login

  REMOVED 2026-09-19 (Hamthan): POST /clients. See the comment at the bottom of
  this file for what it did and why nothing needs it.

WHY two routers in one file
    They share the same service and schemas; keeping them together keeps the
    auth surface discoverable. main.py mounts them under different prefixes.
================================================================================
"""
from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.throttle import limit_login
from app.core.pagination import Page, PageParams
from app.core.enums import UserRole
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User
from app.modules.users.service import UserService
from app.modules.users import schemas

# ── Auth router ─────────────────────────────────────────────────────────────
auth_router = APIRouter(tags=["Authentication"])


@auth_router.post("/login", response_model=schemas.Token,
                  dependencies=[Depends(limit_login)])
async def login(
    body: schemas.LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Sign in. THROTTLED PER IP (core/throttle.py) — this is the only route an
    attacker can reach without credentials, and bcrypt at cost 12 makes a
    sustained guessing run a denial of service as well as a security problem."""
    return await UserService(db).login(body.username, body.password)


@auth_router.get("/me", response_model=schemas.UserRead)
async def me(user: User = Depends(get_current_user)):
    return user


@auth_router.post("/change-password", status_code=204)
async def change_password(
    body: schemas.PasswordChange,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await UserService(db).change_password(user, body.current_password, body.new_password)


# ── User-management router (direct manager only) ─────────────────────────────
users_router = APIRouter(tags=["Users"])


@users_router.get("", response_model=Page[schemas.UserRead])
async def list_users(
    active_only: bool = True,
    params: PageParams = Depends(),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER,UserRole.HR,UserRole.MANAGING_DIRECTOR)),
):
    """The login roster. PAGED — see core/pagination.py."""
    rows, total = await UserService(db).page_users(params, active_only)
    return Page[schemas.UserRead].of(
        [schemas.UserRead.model_validate(r) for r in rows],
        total=total, params=params)


@users_router.post("", response_model=schemas.UserRead, status_code=201)
async def create_user(
    body: schemas.UserCreate,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.HR, UserRole.MANAGING_DIRECTOR)),
):
    """B8: the role gate admits DM/HR/MD, but WHICH role may be granted is
    decided in the service against the caller's own authority."""
    return await UserService(db).create_user(body, actor=actor)


# ── REMOVED: POST /users/clients ─────────────────────────────────────────────
# WHAT IT DID: minted a CLIENT-role login bound to a client_id, so the buyer
# could log in and watch their own orders.
#
# WHY IT IS NOT NEEDED. Phase 1 is the factory floor's system of record — order
# sheet in, breakdown, barcode, cut, stitch, wages. The buyer is not a user of
# it: they send an order sheet and they receive a BOM, both by hand, and the
# one thing they approve (the costing) is approved off-system before production
# starts (CLAUDE.md §1, steps 3-4). So a client login has nothing to do inside
# the app that a person is not already doing for them outside it.
#
# It was also the ONLY way a CLIENT-role login could ever be created, which is
# what makes removing it worth doing rather than just leaving it unused: every
# `if user.role == UserRole.CLIENT` branch in clients/, analytics/, dashboard/,
# production/ and bom/ is a cross-tenant scoping check, and each one is a place
# a buyer could be shown another buyer's styles, prices or order book if the
# scoping were ever got wrong. With no door to mint the role, that surface
# cannot be reached at all. The branches stay where they are — they cost
# nothing, and Phase 2 may well want a client portal — but nothing can hold the
# role until this route (or an equivalent) is deliberately put back.
#
# UserService.create_client_user() and schemas.ClientUserCreate are left in
# place: they are the whole implementation, and re-enabling this is meant to be
# uncommenting a route, not rewriting a feature.
#
# @users_router.post("/clients", response_model=schemas.UserRead, status_code=201)
# async def create_client_user(
#     body: schemas.ClientUserCreate,
#     db: AsyncSession = Depends(get_db),
#     _: User = Depends(require_roles(
#         UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)),
# ):
#     """Direct manager provisions a client's login so they can track their orders.
#
#     B8: HR removed. Binding a login to a client_id grants cross-tenant read
#     access to that client's orders — that is a commercial decision, not an
#     employee-admin one."""
#     return await UserService(db).create_client_user(body)
