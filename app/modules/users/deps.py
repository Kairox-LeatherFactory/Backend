"""
================================================================================
modules/users/deps.py — Auth dependencies (get_current_user, require_roles)
================================================================================

PURPOSE
    The FastAPI dependencies that turn a Bearer token into a live User row and
    enforce role-based access. These live in the USERS module (not core) because
    they depend on the concrete User model — and core must not import modules
    (enforced by import-linter's core-independence contract). core/security.py
    keeps only the model-agnostic primitives (hashing, token mint/decode, rate
    limiting); the moment a dependency needs the User table, it belongs here.

USAGE (in any router)
    from app.modules.users.deps import get_current_user, require_roles
    async def route(user: User = Depends(get_current_user)): ...
    async def admin(_: User = Depends(require_roles(UserRole.DIRECT_MANAGER))): ...
================================================================================
"""
import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.core.security import decode_access_token
from app.modules.users.models import User

# Where Swagger's "Authorize" button posts to obtain a token.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the Bearer token to an active User row (one indexed lookup)."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception
    sub = payload.get("sub")
    if not sub:
        raise credentials_exception
    try:
        user_id = uuid.UUID(str(sub))
    except (ValueError, TypeError):
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise credentials_exception
    return user


def require_roles(*allowed: UserRole):
    """Dependency factory: restrict an endpoint to roles. DIRECT_MANAGER passes."""
    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role == UserRole.DIRECT_MANAGER:
            return user
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role.value}' is not permitted for this action",
            )
        return user
    return checker
