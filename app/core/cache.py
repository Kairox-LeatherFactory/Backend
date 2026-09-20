"""
================================================================================
core/cache.py — read cache for the dashboard and analytics aggregates
================================================================================

THE PROBLEM THIS SOLVES, AND THE ONE IT MUST NOT CREATE

The dashboard and analytics surfaces are 32 routes that recompute large
aggregates on every request. Several managers keep those screens open, so the
same GROUP BY runs over and over against data that has not changed — and each
run holds one of the pool's small number of connections for its duration.

The obvious fix, a 30-60 second TTL, buys that back and introduces something
worse: a cutting manager logs a cut and does not see it on the dashboard for a
minute. On a factory floor that reads as "the system lost my scan", and the
operator scans again. A cache that makes people distrust the screen has cost
more than it saved.

SO THE CACHE IS BUSTED ON WRITE, NOT MERELY EXPIRED.

Every cached entry carries a VERSION number in its key. Writes that change what
the dashboards show — a production event, a store scan, a material move — bump
that version. Old keys are not deleted; they simply become unreachable and fall
out on their own TTL. The next read after any scan is live.

WHY A VERSION COUNTER RATHER THAN DELETING KEYS

Deleting by pattern means SCAN over the keyspace, which is O(number of keys) and
runs on every scan on the floor. Bumping one integer is O(1) and invalidates
everything at once. The cost is that superseded entries linger until their TTL —
memory, not correctness, and a few seconds of stale bytes nobody can reach.

IT MUST NEVER BE THE REASON A REQUEST FAILS.

Redis being down is a degraded cache, not a degraded API. Every operation here
swallows its errors and reports a miss, so the caller recomputes and answers
normally. A cache that can take the site down is not worth having.
================================================================================
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_PREFIX = "kairox:cache"
_VERSION_KEY = f"{_PREFIX}:version"

# Long enough to absorb a screen being refreshed repeatedly; short enough that a
# missed version bump cannot show stale numbers for long. The TTL is the
# BACKSTOP — the version bump is the real invalidation.
DEFAULT_TTL_SECONDS = 60


async def _client():
    """A Redis client, or None. Never raises."""
    try:
        import redis.asyncio as aioredis

        return aioredis.from_url(
            settings.celery_broker_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
    except Exception as exc:                      # pragma: no cover - env dependent
        logger.debug("cache: no redis client (%s)", exc)
        return None


def key_for(namespace: str, version: int, **parts: Any) -> str:
    """A stable key for one namespace, version and set of query parameters.

    The parameters are sorted and hashed so that two callers asking the same
    question hit the same entry regardless of argument order, and so a long
    filter set does not produce an unbounded key.
    """
    blob = json.dumps(parts, sort_keys=True, default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return f"{_PREFIX}:v{version}:{namespace}:{digest}"


async def current_version() -> int:
    """The version every key is built against. 0 when Redis is unreachable."""
    client = None
    try:
        # INSIDE the try. `_client()` catches its own import and URL errors, but
        # a caller can still substitute something that raises, and this module
        # promises never to raise — so the acquisition is guarded too, not just
        # the use.
        client = await _client()
        if client is None:
            return 0
        raw = await client.get(_VERSION_KEY)
        return int(raw) if raw else 0
    except Exception as exc:
        logger.debug("cache: version read failed (%s)", exc)
        return 0
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def invalidate() -> None:
    """Bump the version — every existing entry becomes unreachable at once.

    CALL THIS FROM THE WRITE PATH, in the same place the data actually changes:
    a production event, a store scan, a material decrement. It is one INCR, so
    calling it on every scan is cheap; calling it too rarely is what shows a
    manager yesterday's numbers.
    """
    client = None
    try:
        client = await _client()
        if client is None:
            return
        await client.incr(_VERSION_KEY)
    except Exception as exc:
        # A failed bump means the next read may serve one TTL of stale data.
        # Worth a warning, never worth failing the write that triggered it.
        logger.warning("cache: invalidate failed (%s) — entries will expire on TTL", exc)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def get_json(namespace: str, **parts: Any):
    """A cached value, or None for a miss. Never raises."""
    if not settings.cache_enabled:
        return None
    client = None
    try:
        client = await _client()
        if client is None:
            return None
        version = await current_version()
        raw = await client.get(key_for(namespace, version, **parts))
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.debug("cache: read miss for %s (%s)", namespace, exc)
        return None
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def set_json(namespace: str, value: Any, *,
                   ttl: int = DEFAULT_TTL_SECONDS, **parts: Any) -> None:
    """Store a value. Never raises, and never blocks the response on failure."""
    if not settings.cache_enabled:
        return
    client = None
    try:
        client = await _client()
        if client is None:
            return
        version = await current_version()
        await client.set(
            key_for(namespace, version, **parts),
            json.dumps(value, default=str),
            ex=ttl,
        )
    except Exception as exc:
        logger.debug("cache: write failed for %s (%s)", namespace, exc)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def cached(namespace: str, compute, *, ttl: int | None = None, **parts):
    """Return a cached value, or compute it and store it. Never raises.

        return await cached("dash:cutting",
                            lambda: DashboardService(db).overview(...),
                            scope=scope, order_id=order_id)

    `compute` is a callable returning an awaitable, so nothing is computed on a
    hit. The result must be JSON-serialisable, which is why this wraps the
    SERVICE call and not the response model — pydantic objects go through
    `model_dump` at the router, after this.

    ON A MISS, OR WITH REDIS DOWN, this is exactly the uncached call plus two
    failed socket attempts with a 1s timeout. That is the deliberate trade: the
    cache can be absent, it cannot be load-bearing.
    """
    hit = await get_json(namespace, **parts)
    if hit is not None:
        # A HIT COMES BACK AS A DICT, not as the model the service returned.
        # That is fine and deliberate: these are FastAPI routes with a
        # `response_model`, so FastAPI validates the dict into that model on the
        # way out and the client cannot tell the difference. It is also why the
        # model is dumped rather than pickled — a cache entry must survive a
        # deploy that changes the class.
        return hit

    value = await compute()

    # PYDANTIC MODELS MUST BE DUMPED, NOT str()'d. The dashboard services return
    # CuttingDashboard, StoreDashboard and friends. `json.dumps(model,
    # default=str)` would happily produce the model's repr — a STRING — and the
    # next reader would get that string back and fail response validation, or
    # worse, serve it. mode="json" also resolves UUIDs, dates and Decimals to
    # JSON primitives, which `default=str` would otherwise do inconsistently.
    payload = value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        payload = dump(mode="json")

    await set_json(namespace, payload,
                   ttl=ttl or settings.cache_ttl_seconds, **parts)
    return value
