"""
================================================================================
core/security.py — Self-issued JWT auth + role-based access control
================================================================================

PURPOSE
    The auth PRIMITIVES. We MINT and VERIFY our own JWTs now — the Supabase
    dependency is gone. This file provides:
      - password hashing/verification (bcrypt, called directly — see note below)
      - access-token creation and decoding (HS256, signed with settings.secret_key)
      - an in-memory login rate-limiter (5 attempts / 10 min per phone)

    NOTE: the FastAPI DEPENDENCIES that consume these primitives —
    `get_current_user()` (Bearer token → live User row) and `require_roles()` (the
    per-endpoint role gate, where DIRECT_MANAGER/MANAGING_DIRECTOR bypass) — live in
    `app/modules/users/deps.py`, because they need the users repository. This file
    stays dependency-free of app.modules so core never imports a module.

AUTH MODEL (what changed from Supabase)
    OLD: Supabase issued a token; we only verified it and read user_metadata.role.
    NEW: A user logs in at /auth/login with PHONE + PASSWORD. We verify the
         password against the bcrypt hash in our `users` table and issue our own
         JWT. The token carries:  sub=user_id, role, name.  Verification needs
         no network call — just the shared secret.

PASSWORD POLICY (v1, mocked data)
    Seeded users get password == their phone number (bcrypt-hashed). Each seeded
    user also has must_change_password=True so a "force reset on first login"
    flow can be added later without a migration. NOTE: phone-as-password is a
    known weak default acceptable only for this mocked v1 — rotate before real
    use.

TOKEN PAYLOAD CONTRACT (what the frontend can rely on)
    {
      "sub":  "<user uuid as string>",
      "role": "direct_manager" | "cutting_manager" | ... ,
      "name": "Display Name",
      "exp":  <unix timestamp>
    }

RATE LIMITER
    A process-local sliding counter. Good enough for a single-instance
    deployment. If you run multiple API replicas, move this to Redis (the shape
    of the helper stays identical).

FUNCTION GUIDE  (consumed by modules/users — auth/login + the deps that gate routes)
  verify_password(plain, hashed) -> bool
      bcrypt check; truncates to 72 BYTES first (bcrypt 5.x errors past that). Never raises.
      CALLED FROM: users authenticate (login).
  get_password_hash(password) -> str
      bcrypt hash for storage. CALLED FROM: user creation + scripts/seed.py (seeded pw = phone).
  create_access_token(*, user_id, role, name, expires_delta?) -> str
      Mint the signed HS256 JWT {sub, role, name, exp}. CALLED FROM: users authenticate.
  decode_access_token(token) -> dict | None
      Verify + decode (no network call); None on any JWT error. CALLED FROM: users.deps.get_current_user.
  login_attempts_check(phone) -> None     raise 429 if the phone exceeded the window budget.
  login_attempts_clear(phone) -> None      reset the counter after a successful login.
      Both CALLED FROM: the login endpoint (before verify / on success).
================================================================================
"""
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import HTTPException, status
from jose import JWTError, jwt

from app.core.config import settings
from app.core.enums import UserRole

# NOTE: we call the `bcrypt` library directly rather than going through passlib.
# passlib 1.7.4 (last released 2020) is incompatible with bcrypt >= 4.1 / 5.x:
# its backend self-test calls bcrypt.hashpw() with a >72-byte probe, and modern
# bcrypt raises ValueError instead of truncating, which crashed every verify().
# bcrypt produces/verifies standard $2b$ hashes, so existing DB hashes (created
# by passlib's bcrypt backend) still verify unchanged.


# ══════════════════════════════════════════════════════════════════════════
# Password helpers
# ══════════════════════════════════════════════════════════════════════════
def verify_password(plain_password: str, hashed_password: str) -> bool:
    # bcrypt only inspects the first 72 BYTES; truncate on the byte string so a
    # multibyte char near the boundary can't push us over (bcrypt 5.x errors).
    pw = plain_password.encode("utf-8")[:72]
    try:
        return bcrypt.checkpw(pw, hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def get_password_hash(password: str) -> str:
    pw = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")

print(get_password_hash("9000000001"))


# ══════════════════════════════════════════════════════════════════════════
# JWT helpers
# ══════════════════════════════════════════
def create_access_token(*, user_id: uuid.UUID, role: UserRole, name: str,
                        expires_delta: Optional[timedelta] = None) -> str:
    """Mint a signed access token. `role` is stored as its string value."""
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload = {
        "sub": str(user_id),
        "role": role.value if isinstance(role, UserRole) else str(role),
        "name": name,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None


# ══════════════════════════════════════════════════════════════════════════
# Login rate limiter (process-local; swap for Redis when running >1 replica)
# ══════════════════════════════════════════════════════════════════════════
_login_attempts: dict[str, list[float]] = {}


def login_attempts_check(phone: str) -> None:
    """Raise 429 if `phone` exceeded the attempt budget in the window."""
    now = time.time()
    window = settings.login_window_seconds
    hits = [t for t in _login_attempts.get(phone, []) if now - t < window]
    if len(hits) >= settings.login_max_attempts:
        retry = int(window - (now - hits[0]))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Retry in {retry}s.",
            headers={"Retry-After": str(retry)},
        )
    hits.append(now)
    _login_attempts[phone] = hits


def login_attempts_clear(phone: str) -> None:
    """Reset the counter after a successful login."""
    _login_attempts.pop(phone, None)
