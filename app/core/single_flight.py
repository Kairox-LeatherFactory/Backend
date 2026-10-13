"""
================================================================================
core/single_flight.py — run a periodic job ONCE across every replica
================================================================================

THE PROBLEM THIS SOLVES

`app.main` starts its sweepers as asyncio tasks inside the API process. That is
fine on one box with one worker and wrong everywhere else: the sweeper is per
PROCESS, so the real firing rate is

    gunicorn workers (or ECS tasks) x replicas

Escalation is not idempotent from the recipient's point of view — it sends email.
At WEB_CONCURRENCY=4 that is four escalation emails per overdue notification, per
cycle, from a single box; behind an ALB with N tasks it is 4N. It is also the
reason the API cannot simply be scaled out, which is the whole point of moving to
ECS.

THE FIX

Before each cycle a process tries to take a short-lived Redis lock named for the
job. Exactly one wins and does the work; the rest skip that cycle. The lock
carries a TTL a little longer than the work is expected to take, so a process
that dies mid-sweep does not wedge the job forever — the next cycle simply finds
the lock expired.

The lock is held by VALUE as well as by key (a uuid this process generated), and
released with a compare-and-delete Lua script, so a process whose lock already
expired cannot delete the lock a DIFFERENT process has since taken.

WHY NOT JUST USE CELERY BEAT

It is the better home for these jobs and the repo already runs a `beat` service
(pinned to one replica, with a comment saying never to scale it). But the beat
entry for notification escalation calls
`NotificationService.escalate_overdue()`, which does not exist on that class —
the only real method is `run_escalations()` — so that task has been raising
AttributeError on every fire. Those files belong to the bom module, which is out
of scope for this change set, so the sweepers stay where they are and are made
safe to replicate instead. Moving them to beat afterwards is still the right
end state.

IF REDIS IS UNREACHABLE the cycle is SKIPPED, not run. A missed escalation is
recoverable and shows up in the logs; N duplicate emails to a client are not.
================================================================================
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from app.core.config import settings

logger = logging.getLogger(__name__)

# Compare-and-delete: only drop the lock if we still hold THIS value.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_KEY_PREFIX = "kairox:single-flight:"


@asynccontextmanager
async def single_flight(job_name: str, ttl_seconds: int):
    """Yield True to exactly one caller per `ttl_seconds` window, False to the rest.

        async with single_flight("notification-sweep", ttl_seconds=55) as mine:
            if not mine:
                continue        # another replica is doing this cycle
            ...

    `ttl_seconds` should comfortably exceed how long the job takes but stay below
    the interval between cycles, so a crashed holder frees the job by the next
    tick rather than blocking it.
    """
    token = str(uuid.uuid4())
    key = f"{_KEY_PREFIX}{job_name}"
    client = None
    acquired = False
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.celery_broker_url,
                                   socket_connect_timeout=2, socket_timeout=2)
        # SET key token NX PX ttl — atomic "take it only if free".
        acquired = bool(await client.set(key, token, nx=True,
                                         px=ttl_seconds * 1000))
        yield acquired
    except Exception as exc:
        # Deliberately conservative: skip rather than risk every replica running.
        logger.warning("single_flight(%s): no lock (%s) — skipping this cycle",
                       job_name, exc)
        yield False
    finally:
        if client is not None:
            try:
                if acquired:
                    await client.eval(_RELEASE, 1, key, token)
            except Exception:
                # The TTL is the backstop; an un-released lock frees itself.
                logger.debug("single_flight(%s): release failed", job_name,
                             exc_info=True)
            try:
                await client.aclose()
            except Exception:
                pass
