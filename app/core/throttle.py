"""
================================================================================
core/throttle.py — a rate limit on the LOGIN endpoint, and nowhere else
================================================================================

WHY ONLY LOGIN

Rate limiting the floor would be actively harmful. A barcode terminal doing two
hundred scans in a burst is a manager working through a trolley, not abuse, and
a throttle that fires mid-shift stops production to protect nothing. The
endpoints that matter on this system are behind a role gate and reached by
people the factory employs.

`/auth/login` is the exception, because it is the one route that is useful to an
attacker who has no credentials: it accepts an unauthenticated request and tells
you whether a guess was right. Without a limit, a password is only as strong as
how fast someone can iterate — and bcrypt at cost 12 is slow enough that a
sustained attempt is also a denial of service against the API.

SO THE RULE IS NARROW AND DELIBERATE (Hamthan, 2026-09-20: "auth only"):
a small number of attempts per minute, per client IP, on login alone.

FAIL OPEN, NOT CLOSED

If Redis is unavailable the request is ALLOWED. A throttle that locks everyone
out of the factory because a cache is down has caused a worse outage than the
one it was guarding against. The limiter is a speed bump, not a security
boundary — the security boundary is the password hash and the JWT.

WHY A FIXED WINDOW AND NOT A TOKEN BUCKET

A fixed window can let through up to 2x the limit across a boundary. For
"slow down credential guessing" that is irrelevant, and the implementation is
one INCR plus one EXPIRE, which cannot itself become a bottleneck.
================================================================================
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from app.core.config import settings

logger = logging.getLogger(__name__)

_KEY = "kairox:throttle:login"


def _client_ip(request: Request) -> str:
    """The caller's IP, honouring one proxy hop.

    Behind an ALB the socket address is the balancer, so every request would
    share one bucket and the whole factory would be throttled together.
    X-Forwarded-For's FIRST entry is the original client. This trusts that
    header, which is only safe because the app sits behind a load balancer that
    sets it — do not expose this service directly to the internet.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def limit_login(request: Request) -> None:
    """FastAPI dependency. Raises 429 when an IP is guessing too fast."""
    if not settings.login_rate_limit_enabled:
        return

    ip = _client_ip(request)
    key = f"{_KEY}:{ip}"
    window = settings.login_rate_limit_window_seconds
    ceiling = settings.login_rate_limit_attempts

    client = None
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.celery_broker_url,
                                   socket_connect_timeout=1, socket_timeout=1)
        count = await client.incr(key)
        if count == 1:
            # Only the first attempt in a window sets the expiry, so the window
            # is fixed from the first attempt rather than sliding forward with
            # every request — which would never let a blocked IP recover.
            await client.expire(key, window)
    except HTTPException:
        raise
    except Exception as exc:
        # FAIL OPEN. See the module docstring: a broken limiter must not lock
        # the factory out.
        logger.warning("login throttle unavailable (%s) — allowing the request", exc)
        return
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    if count > ceiling:
        logger.warning("login throttled ip=%s attempts=%s window=%ss",
                       ip, count, window)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(f"Too many sign-in attempts. Try again in {window} seconds."),
            headers={"Retry-After": str(window)},
        )
