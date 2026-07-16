"""
================================================================================
modules/users/service.py — Authentication + user-management business logic
================================================================================

PURPOSE
    All the rules that sit above raw data access:
      authenticate()        verify phone+password, return the User or None
      login()               rate-limit, authenticate, mint a JWT, return a Token
      create_user()         direct-manager creates a staff/manager/viewer login
      create_client_user()  direct-manager provisions a CLIENT login for a client
      change_password()     self-service password change

PASSWORD DEFAULTING (v1)
    If no password is supplied when creating a user, it defaults to the user's
    phone number (bcrypt-hashed) and must_change_password is set True. This is
    the mocked-data convenience the spec asked for, made safe-ish by the flag.
================================================================================
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.core.security import (
    create_access_token,
    get_password_hash,
    login_attempts_check,
    login_attempts_clear,
    verify_password,
)
from app.modules.users.models import User
from app.modules.users.repository import UserRepository
from app.modules.users import schemas


class UserService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = UserRepository(db)

    # ── Authentication ──────────────────────────────────────────────────────
    async def authenticate(self, username: str, password: str) -> User | None:
        user = await self.repo.get_by_username(username)
        if not user or not user.is_active:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    async def login(self, username: str, password: str) -> schemas.Token:
        login_attempts_check(username)            # raises 429 if over budget
        user = await self.authenticate(username, password)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        login_attempts_clear(username)
        token = create_access_token(user_id=user.id, role=user.role, name=user.name)
        return schemas.Token(
            access_token=token, role=user.role, name=user.name,
            user_id=user.id, must_change_password=user.must_change_password,
        )

    # ── User management (direct manager) ─────────────────────────────────────
    async def create_user(self, body: schemas.UserCreate) -> User:
        if await self.repo.get_by_username(body.phone):
            raise HTTPException(status.HTTP_409_CONFLICT, "Phone already registered")
        if body.email and await self.repo.get_by_email(body.email):
            raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
        raw = body.password          # default password = phone
        return await self.repo.from_user_create(
            name=body.name, phone=body.phone, email=body.email, role=body.role,
            password_hash=get_password_hash(raw), employee_id=body.employee_id,
            must_change_password=body.password is None,
        )

    async def create_client_user(self, body: schemas.ClientUserCreate) -> User:
        if await self.repo.get_by_username(body.phone):
            raise HTTPException(status.HTTP_409_CONFLICT, "Phone already registered")
        raw = body.password
        return await self.repo.from_user_create(
            name=body.name, phone=body.phone, email=body.email,
            role=UserRole.CLIENT, password_hash=get_password_hash(raw),
            client_id=body.client_id, must_change_password=body.password is None,
        )

    async def change_password(self, user: User, current: str, new: str) -> None:
        if not verify_password(current, user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
        user.password_hash = get_password_hash(new)
        user.must_change_password = False
        await self.repo.save(user)

    async def list_users(self, active_only: bool = True) -> list[User]:
        return await self.repo.list_all(active_only)

    async def list_by_roles(self, roles, active_only: bool = True) -> list[User]:
        """Active users in any of `roles` — the public interface other modules use
        (e.g. procurement resolving the MD/DM recipients of a BOM-review notice)."""
        return await self.repo.list_by_roles(roles, active_only)

    async def get(self, user_id) -> User | None:
        """One user by id — used by the procurement escalation sweeper to resolve a
        notification recipient's email."""
        return await self.repo.get(user_id)
    
    async def provision_user(self, body: schemas.UserCreate) -> User:
        """Validate + stage. Does NOT commit — the caller owns the transaction."""
        if await self.repo.get_by_username(body.phone):
            raise HTTPException(409, "Phone already registered")
        if body.email and await self.repo.get_by_email(body.email):
            raise HTTPException(409, "Email already registered")
        return await self.repo.create(
            name=body.name, phone=body.phone, email=body.email, role=body.role,
            password_hash=get_password_hash(body.password),
            employee_id=body.employee_id,
            must_change_password=body.password is None,
        )
