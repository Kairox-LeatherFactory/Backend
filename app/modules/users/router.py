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
    POST /clients          provision a CLIENT login bound to a client_id

WHY two routers in one file
    They share the same service and schemas; keeping them together keeps the
    auth surface discoverable. main.py mounts them under different prefixes.
================================================================================
"""
from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User
from app.modules.users.service import UserService
from app.modules.users import schemas

# ── Auth router ─────────────────────────────────────────────────────────────
auth_router = APIRouter(tags=["Authentication"])


@auth_router.post("/login", response_model=schemas.Token)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """OAuth2-form login. The 'username' field carries the PHONE number.
    (Swagger's Authorize dialog posts this form; JSON clients can post the same
    fields.)"""
    return await UserService(db).login(form.username, form.password)


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


@users_router.get("", response_model=list[schemas.UserRead])
async def list_users(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    return await UserService(db).list_users(active_only)


@users_router.post("", response_model=schemas.UserRead, status_code=201)
async def create_user(
    body: schemas.UserCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    return await UserService(db).create_user(body)


@users_router.post("/clients", response_model=schemas.UserRead, status_code=201)
async def create_client_user(
    body: schemas.ClientUserCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Direct manager provisions a client's login so they can track their orders."""
    return await UserService(db).create_client_user(body)
