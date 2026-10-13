"""
INTEGRATION · the accessory catalogue, and how it fills itself in.

THE PROBLEM IT SOLVES. `MaterialSubtype` offered BUTTON / ZIP / THREAD / OTHER for
accessories, so eyelets, lace pins and rib knit trim all fell to `OTHER` — whose
spec requires `description` + `count` and whose filters are `article, colour`, with
NO SIZE AT ALL. Rib knit trim, an accessory whose size genuinely varies from one SKU
to the next, therefore had no size field to vary. Every new accessory was otherwise
an enum member, a MATERIAL_SPEC entry and a deploy.

HOW IT FILLS ITSELF IN. A kind becomes known because a packet of it ARRIVED. That is
the only moment anybody actually knows about a new accessory, and it is already a
form somebody is filling in — so `arrive` and `create_lot` register an unrecognised
`subtype` instead of rejecting it, and report it back so a kind that appears by
accident is visible rather than silent.

WHAT THESE TESTS ALSO PIN. The catalogue is an OVERLAY: with no rows at all —
which is the state `create_all` leaves the test harness in, because the seed lives
in the migration — every built-in kind must still behave exactly as it did. That
fallback is what makes the catalogue safe to deploy before it is seeded.
"""
import uuid

import pytest

from app.modules.materials import schemas
from app.modules.materials.accessory_catalog import (AccessoryCatalog, CODE_MAX,
                                                     normalise_code)
from app.modules.materials.schemas import LotCreate
from app.modules.materials.service import MaterialService

pytestmark = pytest.mark.integrity


def arrival(**kw):
    base = dict(article="EYE-4", colour="BLACK", total_qty=500,
                category="ACCESSORY")
    base.update(kw)
    return schemas.ArrivalCreate(**base)


# ══════════════════════════════════════════════════ 1 · normalising a code
class TestNormaliseCode:
    """UPPERCASE AND UNDERSCORED, the discipline `Designation.normalise` applies to
    job titles and for the same reason: this is a controlled vocabulary a filter
    matches on, and 'Eyelet' / 'eyelets' / 'EYELETS' being three kinds is how a
    stock screen stops adding up."""

    @pytest.mark.parametrize("raw,expected", [
        ("rib knit trim", "RIB_KNIT_TRIM"),
        ("Lace-Pin", "LACE_PIN"),
        ("eyelet", "EYELET"),
        ("  zip  ", "ZIP"),
        ("snap/button", "SNAP_BUTTON"),
    ])
    def test_a_human_spelling_becomes_one_code(self, raw, expected):
        assert normalise_code(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", None, "!!!"])
    def test_nothing_usable_is_none_rather_than_an_empty_code(self, raw):
        assert normalise_code(raw) is None


# ══════════════════════════════════════ 2 · the gate registers a new kind
@pytest.mark.asyncio
async def test_receiving_an_unknown_accessory_registers_it(db):
    """"If there is a new accessory then it enters to material, and from there we
    get the data." The van is here and the packet is real, so the gate does not
    refuse it for being a kind nobody has catalogued."""
    out = await MaterialService(db).arrive(arrival(subtype="EYELET"))

    assert out["accessory_type_registered"] is not None
    reg = out["accessory_type_registered"]
    assert reg["code"] == "EYELET"
    assert reg["auto_registered"] is True
    assert "new accessory kind" in reg["message"]

    types = {t["code"]: t for t in await AccessoryCatalog(db).list_types()}
    assert "EYELET" in types
    # first_seen_at / created_by are how an auto-registered kind is told apart from
    # a curated one — the gate is the least supervised entry in the app.
    assert types["EYELET"]["first_seen_at"] is not None


@pytest.mark.asyncio
async def test_registering_is_reported_only_the_FIRST_time(db):
    """A kind appearing by accident must be visible. A kind arriving for the
    hundredth time must not be noise on every delivery."""
    svc = MaterialService(db)
    first = await svc.arrive(arrival(subtype="LACE_PIN"))
    second = await svc.arrive(arrival(subtype="LACE_PIN", total_qty=200))

    assert first["accessory_type_registered"] is not None
    assert second["accessory_type_registered"] is None


@pytest.mark.asyncio
async def test_a_kind_that_arrived_with_a_size_requires_one(db):
    """THE DEFAULTS ARE THE BUTTON SHAPE — counted in pieces, wanting a size —
    because that is what most accessories are. But `size` is required only when the
    arrival actually carried one: demanding it of a kind that has no size would make
    the next delivery of it fail the strict path for a field it cannot have."""
    svc = MaterialService(db)
    await svc.arrive(arrival(subtype="EYELET", size="4"))
    sized = await AccessoryCatalog(db).spec_for("ACCESSORY", "EYELET")
    assert "size" in sized["required"]
    assert "size" in sized["filters"]

    await svc.arrive(arrival(subtype="HANG_TAG", article="TAG-1"))
    unsized = await AccessoryCatalog(db).spec_for("ACCESSORY", "HANG_TAG")
    assert "size" not in unsized["required"]


@pytest.mark.asyncio
async def test_the_strict_lot_path_registers_too_then_validates(db):
    """`create_lot` is the considered path, so it still enforces the kind's fields —
    but it enforces them against a kind it has just learnt, rather than refusing the
    material for being unfamiliar."""
    out = await MaterialService(db).create_lot(LotCreate(
        category="ACCESSORY", subtype="RIB_KNIT_TRIM", article="RIB-1",
        colour="BLACK", attributes={"size": "M", "count": 40}))
    assert out["accessory_type_registered"]["code"] == "RIB_KNIT_TRIM"
    assert out["uom"] == "pcs"


@pytest.mark.asyncio
async def test_an_accessory_with_no_subtype_at_all_is_still_refused(db):
    """There is no generic accessory quantity, so "an accessory" is not a thing that
    can be received. The message names the kinds that exist and says how a new one
    comes into being."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await MaterialService(db).create_lot(LotCreate(
            category="ACCESSORY", article="MYSTERY", colour="BLACK",
            attributes={"count": 10}))
    assert exc.value.status_code == 422
    assert "needs a subtype" in str(exc.value.detail)
    assert "BUTTON" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_code_too_long_for_the_subtype_column_is_refused(db):
    """`subtype` is String(20) on material_lot, style_material_spec,
    piece_material_issue and kit_substitution_request — one of them a ledger. Capping
    the source is cheaper and safer than widening four columns, and a silent
    truncation would split one kind into two on the stock screen."""
    from fastapi import HTTPException
    long_code = "A" * (CODE_MAX + 5)
    with pytest.raises(HTTPException) as exc:
        await AccessoryCatalog(db).register_nocommit(long_code)
    assert exc.value.status_code == 422
    assert str(CODE_MAX) in str(exc.value.detail)
    assert "label" in str(exc.value.detail)


# ══════════════════════════════════ 3 · the overlay, and the built-in fallback
@pytest.mark.asyncio
async def test_the_built_ins_still_answer_with_an_empty_catalogue(db):
    """THE PROPERTY THAT MAKES THIS SAFE TO DEPLOY UNSEEDED. The test harness builds
    its schema with create_all and never runs the migration that seeds the kinds, so
    every assertion in this file's siblings runs against a catalogue with no rows —
    and buttons, zips and thread must behave exactly as they always did."""
    cat = AccessoryCatalog(db)
    button = await cat.spec_for("ACCESSORY", "BUTTON")
    assert button["qty_field"] == "count" and button["qty_uom"] == "pcs"
    assert button["filters"] == ["article", "colour", "size"]
    thread = await cat.spec_for("ACCESSORY", "THREAD")
    assert thread["qty_field"] == "mtrs"
    # …and leather/lining never consult the catalogue at all.
    assert (await cat.spec_for("LEATHER", None))["qty_field"] == "dcm"


@pytest.mark.asyncio
async def test_a_catalogued_kind_renders_its_own_form_not_the_OTHER_one(db):
    """THE BUG THE CATALOGUE EXISTS FOR, asserted end to end.

    Before this, an eyelet was `OTHER`: `filter_fields` gave the screen
    `article, colour` and no size box, so the DM could not enter the size of the very
    accessory whose size varies per SKU.
    """
    svc = MaterialService(db)
    before = await svc.filter_fields("ACCESSORY", "EYELET")
    assert "size" not in before["filters"], "unknown kinds fall back to the safe pair"

    await svc.arrive(arrival(subtype="EYELET", size="4"))
    after = await svc.filter_fields("ACCESSORY", "EYELET")
    assert "size" in after["filters"]
    assert "size" in after["required_to_add"]


# ══════════════════════════════════════════════════ 4 · refining a kind
@pytest.mark.asyncio
async def test_size_varies_by_sku_round_trips(db):
    """The 85-90% rule as data. False is the safe default for a kind nobody has
    reviewed — guessing TRUE would ask the DM for a size per SKU on a button that
    has one size."""
    svc = MaterialService(db)
    await svc.arrive(arrival(subtype="RIB_KNIT_TRIM", article="RIB-1", size="M"))
    cat = AccessoryCatalog(db)
    assert await cat.size_varies_by_sku("RIB_KNIT_TRIM") is False

    await cat.patch_type("RIB_KNIT_TRIM", {"size_varies_by_sku": True},
                         actor_name="DM")
    assert await AccessoryCatalog(db).size_varies_by_sku("RIB_KNIT_TRIM") is True


@pytest.mark.asyncio
async def test_the_code_and_the_quantity_field_cannot_be_patched(db):
    """The code is what every lot, recipe line and ledger row of this kind stores in
    `subtype`, so renaming it would orphan all of them silently. `qty_field` names
    the attribute whose value was already added to `on_hand`, so changing it would
    read historical stock through a different key."""
    from fastapi import HTTPException
    await MaterialService(db).arrive(arrival(subtype="EYELET"))
    cat = AccessoryCatalog(db)
    for field, value in (("code", "EYELET2"), ("qty_field", "mtrs")):
        with pytest.raises(HTTPException) as exc:
            await cat.patch_type("EYELET", {field: value}, actor_name="DM")
        assert exc.value.status_code == 422
        assert "orphan" in str(exc.value.detail) or "historical" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_patching_a_kind_nobody_has_received_says_how_one_is_made(db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await AccessoryCatalog(db).patch_type("NEVER_SEEN", {"label": "x"},
                                              actor_name="DM")
    assert exc.value.status_code == 404
    assert "receiving one" in str(exc.value.detail)


# ══════════════════════════════ 5 · a registered kind is usable in a recipe
@pytest.mark.asyncio
async def test_a_kind_learnt_at_the_gate_can_be_put_on_a_recipe(db, order_tree):
    """THE WHOLE POINT, END TO END. The packet arrives, the kind registers itself,
    and the DM can then specify it per SKU — without a deploy in between."""
    from app.modules.materials.style_spec_service import StyleSpecService
    await MaterialService(db).arrive(
        arrival(subtype="EYELET", article="EYE-4", size="4"))

    out = await StyleSpecService(db).add_line(order_tree["style"].id, {
        "category": "ACCESSORY", "subtype": "EYELET", "article": "EYE-4",
        "colour": "BLACK", "size": "4", "qty_per_piece": 8,
        "sku_id": order_tree["sku"].id}, actor_name="DM")

    assert out["subtype"] == "EYELET"
    assert out["uom"] == "pcs"
    assert out["scope"] == "SKU"


# ══════════════════════════════════════ 6 · the cache holds no process state
@pytest.mark.asyncio
async def test_the_catalogue_keeps_no_process_wide_cache(db):
    """THE BUG THIS FILE DID NOT CATCH THE FIRST TIME.

    The cache was written as a module-level global, and it was wrong in two ways at
    once. In production, worker A registering a kind invalidated only A's copy —
    worker B went on answering "unknown", so a kind just registered at the gate
    could be refused by `create_lot` on the very next request, which is precisely
    the failure the catalogue exists to prevent. In the tests it leaked across
    cases: a kind registered against one test's database was still cached when the
    next test looked at a clean one, and the only reason nothing went red was the
    order they happened to run in.

    An instance lives for one service call, which is the right lifetime. This
    asserts the global is gone, because "it works now" is not the property — "it
    cannot go stale between workers" is.
    """
    import app.modules.materials.accessory_catalog as mod
    assert not hasattr(mod, "_CACHE"), (
        "a module-level cache is shared by every request in the process and by "
        "every test in the run — see this test's docstring")
    assert not hasattr(mod, "invalidate"), (
        "invalidating is per-instance now; a module-level invalidate() could only "
        "clear one worker's view")

    cat = AccessoryCatalog(db)
    assert cat._cache is None, "nothing is loaded until something asks"
    await cat.spec_for("ACCESSORY", "BUTTON")
    assert cat._cache is not None, "…and then it is loaded once"


@pytest.mark.asyncio
async def test_a_fresh_instance_sees_a_kind_registered_a_moment_ago(db):
    """The read-your-own-writes case, which is what the per-instance lifetime has
    to preserve: the gate registers, and the very next service call must see it."""
    await MaterialService(db).arrive(arrival(subtype="EYELET"))
    assert await AccessoryCatalog(db).spec_for("ACCESSORY", "EYELET") is not None


@pytest.mark.asyncio
async def test_registering_a_built_in_kind_is_a_no_op(db):
    """BUTTON already has a built-in spec, so receiving one must not shadow it with
    an auto-registered row carrying the generic defaults."""
    out = await MaterialService(db).arrive(
        arrival(subtype="BUTTON", article="BTN-4H", size="18L"))
    assert out["accessory_type_registered"] is None
    spec = await AccessoryCatalog(db).spec_for("ACCESSORY", "BUTTON")
    assert spec["qty_field"] == "count" and "size" in spec["required"]


@pytest.mark.asyncio
async def test_a_new_kind_registers_and_validates_in_ONE_call(db):
    """THE SAME READ-BEFORE-FLUSH BUG, in the catalogue.

    `register_nocommit` adds the kind without committing, then `create_lot` asks
    `spec_for()` for it — which reloads from the database. With `autoflush=False`
    that reload MISSES the pending row, falls back to the built-in MATERIAL_SPEC,
    finds nothing for a kind nobody has hardcoded, and 422s. The failed request then
    rolls back the registration too, so receiving a genuinely new accessory was
    broken outright: the kind never registered and the lot was refused as "not an
    accessory kind".

    `no_autoflush` reproduces production. The existing
    test_the_strict_lot_path_registers_too_then_validates passes either way, which
    is exactly why it did not catch this.
    """
    with db.no_autoflush:
        out = await MaterialService(db).create_lot(LotCreate(
            category="ACCESSORY", subtype="GROMMET", article="GRM-8",
            colour="BRASS", attributes={"size": "8", "count": 250}))

    assert out["accessory_type_registered"]["code"] == "GROMMET"
    assert out["uom"] == "pcs"
    # …and the kind is really there for the next caller, not just in this response.
    assert await AccessoryCatalog(db).spec_for("ACCESSORY", "GROMMET") is not None


# `arrive` IS NOT AFFECTED, and a test here would pass for the wrong reason.
#
# It resolves the spec as `spec_for(...) or {}`, so a missed row degrades to the
# built-in fallback rather than a 422. And an auto-registered kind's unit is `pcs`,
# which is exactly what `uom_for` returns for any accessory — so the fallback and the
# registered value COINCIDE and no assertion can tell them apart. The row is still
# added and still lands at the scan's commit, so nothing is lost either.
#
# A test that cannot fail is noise, so there isn't one. The observable bug was
# create_lot's, above.
