"""
================================================================================
tests/unit/test_cache_busts_on_write.py — the half of caching that is dangerous
================================================================================

A read cache is easy. INVALIDATING it is the part that goes wrong, and when it
goes wrong on this system a cutting manager logs a cut, looks at the dashboard,
does not see it, and scans again. The cache would then have caused the double
entry it was never involved in.

So these tests are about the version counter, which is the whole invalidation
mechanism: every cached key embeds the current version, and a write bumps it,
which makes every existing entry unreachable at once.

FAKE REDIS, ON PURPOSE. A real Redis would make this an integration test that
skips on any machine without one — and a cache test that silently skips is
exactly how the invalidation breaks unnoticed. The fake implements the four
calls the module uses (get / set / incr / aclose) so the LOGIC is tested
everywhere, every run.
================================================================================
"""
import json

import pytest

from app.core import cache as cache_mod


class FakeRedis:
    """The four operations core/cache.py actually uses."""

    def __init__(self, store: dict):
        self.store = store

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, 0)) + 1)
        return int(self.store[key])

    async def aclose(self):
        pass


@pytest.fixture
def fake_redis(monkeypatch):
    store: dict = {}

    async def _client():
        return FakeRedis(store)

    monkeypatch.setattr(cache_mod, "_client", _client)
    monkeypatch.setattr(cache_mod.settings, "cache_enabled", True)
    return store


@pytest.mark.asyncio
async def test_a_value_round_trips(fake_redis):
    await cache_mod.set_json("dash", {"pieces": 12}, scope=None)
    assert await cache_mod.get_json("dash", scope=None) == {"pieces": 12}


@pytest.mark.asyncio
async def test_different_parameters_do_not_collide(fake_redis):
    """Two managers scoped to different clients must not see each other's
    numbers. The parameters are part of the key, not decoration."""
    await cache_mod.set_json("dash", {"who": "acme"}, scope="acme")
    await cache_mod.set_json("dash", {"who": "rival"}, scope="rival")

    assert await cache_mod.get_json("dash", scope="acme") == {"who": "acme"}
    assert await cache_mod.get_json("dash", scope="rival") == {"who": "rival"}


@pytest.mark.asyncio
async def test_parameter_order_does_not_change_the_key(fake_redis):
    """Two callers asking the same question must hit the same entry, however
    they happened to order their keyword arguments."""
    await cache_mod.set_json("dash", {"v": 1}, a=1, b=2)
    assert await cache_mod.get_json("dash", b=2, a=1) == {"v": 1}


@pytest.mark.asyncio
async def test_a_write_makes_every_cached_entry_unreachable(fake_redis):
    """THE POINT OF THE WHOLE DESIGN.

    A cut is logged, so the dashboards are stale. After `invalidate()` no
    previously cached entry may be served — not this one, not one for a
    different scope, not one for a different namespace.
    """
    await cache_mod.set_json("dash:cutting", {"cut": 10}, scope=None)
    await cache_mod.set_json("dash:store", {"held": 4}, scope=None)
    await cache_mod.set_json("dash:cutting", {"cut": 99}, scope="acme")

    await cache_mod.invalidate()

    assert await cache_mod.get_json("dash:cutting", scope=None) is None
    assert await cache_mod.get_json("dash:store", scope=None) is None
    assert await cache_mod.get_json("dash:cutting", scope="acme") is None, (
        "a scoped entry survived the bump — a manager would still be looking at "
        "pre-scan numbers")


@pytest.mark.asyncio
async def test_reads_after_a_write_see_the_new_value(fake_redis):
    """The full cycle: cache, write, recompute, cache again."""
    calls = {"n": 0}

    async def compute():
        calls["n"] += 1
        return {"pieces": calls["n"]}

    first = await cache_mod.cached("dash", compute, scope=None)
    second = await cache_mod.cached("dash", compute, scope=None)
    assert first == second == {"pieces": 1}
    assert calls["n"] == 1, "the second read should have been a hit"

    await cache_mod.invalidate()

    third = await cache_mod.cached("dash", compute, scope=None)
    assert third == {"pieces": 2}, "the read after a write must recompute"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_a_pydantic_model_is_dumped_not_stringified(fake_redis):
    """A model cached as its repr would come back as a STRING and fail response
    validation downstream — or worse, be served. The dashboards return models,
    so this is the realistic case, not a hypothetical one."""
    from pydantic import BaseModel

    class Dash(BaseModel):
        pieces: int

    async def compute():
        return Dash(pieces=5)

    returned = await cache_mod.cached("dash", compute, scope=None)
    assert isinstance(returned, Dash), "a miss returns the model itself"

    stored = [v for k, v in fake_redis.items() if ":dash:" in k]
    assert stored, "nothing was cached"
    assert json.loads(stored[0]) == {"pieces": 5}, (
        f"the model was not dumped to JSON: {stored[0]!r}")


@pytest.mark.asyncio
async def test_the_cache_is_off_unless_enabled(monkeypatch):
    """A cache whose invalidation is not wired up would show stale numbers, so
    it stays off until someone turns it on deliberately."""
    store: dict = {}

    async def _client():
        return FakeRedis(store)

    monkeypatch.setattr(cache_mod, "_client", _client)
    monkeypatch.setattr(cache_mod.settings, "cache_enabled", False)

    await cache_mod.set_json("dash", {"v": 1}, scope=None)
    assert store == {}, "nothing may be written while caching is disabled"
    assert await cache_mod.get_json("dash", scope=None) is None


@pytest.mark.asyncio
async def test_redis_being_down_is_a_miss_not_an_error(monkeypatch):
    """The cache must never be the reason a request fails."""
    async def _broken():
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(cache_mod, "_client", _broken)
    monkeypatch.setattr(cache_mod.settings, "cache_enabled", True)

    calls = {"n": 0}

    async def compute():
        calls["n"] += 1
        return {"v": calls["n"]}

    assert await cache_mod.get_json("dash", scope=None) is None
    await cache_mod.set_json("dash", {"v": 1}, scope=None)   # must not raise
    await cache_mod.invalidate()                             # must not raise
    assert await cache_mod.cached("dash", compute, scope=None) == {"v": 1}
