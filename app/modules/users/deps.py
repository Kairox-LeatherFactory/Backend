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


# Roles that bypass every per-endpoint role gate (the superusers).
# Spec target: MANAGING_DIRECTOR is the superuser and DIRECT_MANAGER is
# operational-only. During the MD rollout we keep DIRECT_MANAGER bypassing too so
# the existing seeded god-account (phone 9000000001) does not lose access — drop
# DIRECT_MANAGER from this set once an MD owner is confirmed in every environment.
SUPERUSER_ROLES = (UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER)

# Roles with NO access to factory data.
#
# The EMPLOYEE role is no longer minted at all — shop-floor workers are not given
# system access (UserRole.login_roles()), and their attendance is entered for
# them by an operator (SECURITY / HR / MD / DM). This guard therefore now covers
# LEGACY rows only: `app_user.role` is a native PG enum whose values cannot be
# dropped, so any employee login issued before the change must still be shut out
# of the roster, other people's attendance, production, wages, clients and
# analytics.
#
# WHY A DENY-LIST DEPENDENCY AND NOT PER-ROUTE ALLOW-LISTS:
#   Most read routes in this codebase use bare get_current_user. Retro-fitting an
#   allow-list to every one of them is not realistic, and any route missed is a
#   leak. `block_employees` is one line per route and, more importantly, is the
#   DEFAULT applied at the router level (see main.py) so a NEW route is closed to
#   employees unless someone opens it deliberately.
RESTRICTED_SELF_SERVICE_ROLES = frozenset({UserRole.EMPLOYEE})


async def block_employees(user: "User" = Depends(get_current_user)) -> "User":
    """Reject the legacy EMPLOYEE role. Every login role passes."""
    if user.role == UserRole.EMPLOYEE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This area is not available to the employee role.",
        )
    return user

def require_roles(*allowed: UserRole):
    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role in SUPERUSER_ROLES:
            return user
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role.value}' is not permitted for this action",
            )
        return user
    return checker