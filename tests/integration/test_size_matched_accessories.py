"""
INTEGRATION · an accessory line reaches only the garments it is for.

THE BUG, AS REPORTED. "When DM assigned Thread S, M, and L, the Store showed all
three sizes for every piece instead of showing only the matching size."

THE CAUSE, ORIGINALLY. `StyleSpecService.merge_lines` keyed the effective recipe
on (category, subtype, article) and never read SKU.size, so a DM entering three
sizes of one accessory got one of two wrong answers and could not tell which:

    three distinct ARTICLES   -> three distinct keys -> all three on every
                                 garment of every size
    one article, three SIZES  -> one key             -> only the last survives,
                                 silently

ALL FOUR SURFACES went through that function — the kit checklist, the scan
payload, the production log's kit block and the actual spend — so the store
rendered three threads for one jacket AND the kit scan spent all three.

HOW IT IS FIXED NOW, AND WHY THE MECHANISM CHANGED

    The first fix added `garment_size`: a second, independent way to scope a line
    to a size, beside `size`, which already meant the MATERIAL's size. Two columns
    that both look like "size" is a trap, and it sprang: `garment_size` was inferred
    from `size` whenever `size` read as a garment size, and ANY bare number from 30
    to 70 read as one because those are the EU jacket rungs. So a 60cm zip entered
    as '60' was read as a size-60 garment, mapped to 4XL and scoped there — and a
    line that reaches no garment is not a shorter recipe, it is NO recipe:
    kit_required comes back False and the garment ships without its zip.

    AN ACCESSORY NAMES ITS SKU INSTEAD. A SKU is unique on
    (style_id, color_code, size) — colour and size together — so once a line names
    one there is nothing left to disambiguate and `garment_size` is not involved at
    all. The reported bug is still the thing under test; only the mechanism that
    prevents it has changed.

    `garment_size` REMAINS for leather and lining, which can still be style-wide.
    That is covered in test_style_spec_service_full.py, not here.
"""
import uuid

import pytest
import pytest_asyncio

from app.modules.materials.style_spec_service import StyleSpecService

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sized_style(db):
    """One style in three sizes — the shape the bug needs to show itself."""
    from app.modules.clients.models import Client, ClientOrder, SKU, Style
    cl = Client(id=uuid.uuid4(), name="BOGGI")
    db.add(cl)
    await db.flush()
    o = ClientOrder(id=uuid.uuid4(), client_id=cl.id, order_number="SS27")
    db.add(o)
    await db.flush()
    st = Style(id=uuid.uuid4(), client_order_id=o.id, name="CLERMONT",
               article="CL1", code="ST-SIZED")
    db.add(st)
    await db.flush()
    skus = {}
    for size in ("S", "M", "L"):
        k = SKU(id=uuid.uuid4(), style_id=st.id, color_code="NAVY",
                color_name="NAVY", size=size, qty_ordered=1, code=f"SK-{size}")
        db.add(k)
        skus[size] = k
    await db.commit()
    return st, skus


def _thread(sku, size):
    """Thread for ONE garment, named by its SKU.

    The SKU is the whole scope. `size` here is the material's own — the S spool, the
    M spool — and it is no longer inspected for whether it might secretly mean a
    garment size, because the SKU has already answered that.
    """
    return {"category": "ACCESSORY", "subtype": "THREAD", "article": "THREAD-40",
            "colour": "NAVY", "thickness": "40", "size": size,
            "sku_id": sku.id, "qty_per_piece": 2}


# ══════════════════════════════════════════════ the reported bug, pinned
async def test_a_sized_accessory_reaches_only_its_own_size(db, sized_style):
    """THE REPORTED BUG. Thread L belongs on an L jacket and nowhere else."""
    st, skus = sized_style
    svc = StyleSpecService(db)
    await svc.replace_spec(st.id, [_thread(skus[s], s) for s in ("S", "M", "L")],
                           actor_name="DM")
    for size in ("S", "M", "L"):
        lines = await svc.effective_lines(st.id, skus[size].id)
        acc = [l for l in lines if l.category == "ACCESSORY"]
        assert len(acc) == 1, f"a size-{size} garment got {len(acc)} thread lines"
        assert acc[0].size == size
        assert acc[0].sku_id == skus[size].id


async def test_three_sizes_are_three_lines_not_one_overwritten(db, sized_style):
    """The other failure mode, and the quieter one.

    With the article alone in the identity key, the second and third lines
    overwrote the first and the DM's entry silently became one line. `sku_id` is
    what keeps them apart now — the same guarantee with one fewer column involved.
    """
    st, skus = sized_style
    svc = StyleSpecService(db)
    await svc.replace_spec(st.id, [_thread(skus[s], s) for s in ("S", "M", "L")],
                           actor_name="DM")
    stored = await svc.repo.lines_for_style(st.id)
    threads = [l for l in stored if l.article == "THREAD-40"]
    assert len(threads) == 3
    assert {l.sku_id for l in threads} == {k.id for k in skus.values()}


# ══════════════════════════════════════════ what the new rule refuses
async def test_a_style_wide_accessory_is_REFUSED(db, sized_style):
    """THE RULE THE WHOLE CHANGE RESTS ON.

    A style-wide accessory line is what forced `garment_size` into existence, and
    with it the size-coverage gate, the size-ambiguity gate, the "is this number a
    garment size" guess, and a PATCH that silently un-scoped a line. Refusing the
    shape is what lets all of that go.
    """
    from fastapi import HTTPException
    st, _skus = sized_style
    svc = StyleSpecService(db)
    with pytest.raises(HTTPException) as exc:
        await svc.add_line(st.id, {
            "category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-18L",
            "colour": "NAVY", "size": "18L", "qty_per_piece": 3}, actor_name="DM")
    assert exc.value.status_code == 422
    assert "must name the SKU" in str(exc.value.detail)
    # …and it says how to cover every colourway at once, not merely that it failed.
    assert "ALL_SKUS" in str(exc.value.detail)


async def test_garment_size_on_an_accessory_is_REFUSED(db, sized_style):
    """Refused, not ignored. A caller who sends it believes it is doing something,
    and its answer could only ever contradict the SKU's."""
    from fastapi import HTTPException
    st, skus = sized_style
    svc = StyleSpecService(db)
    with pytest.raises(HTTPException) as exc:
        await svc.add_line(st.id, dict(_thread(skus["M"], "M"),
                                       garment_size="M"), actor_name="DM")
    assert exc.value.status_code == 422
    assert "garment_size is not used on an accessory line" in str(exc.value.detail)


async def test_a_material_size_of_M_IS_ACCEPTED(db, sized_style):
    """THE 422 THE FLOOR KEPT HITTING, gone.

        POST .../material-spec/lines  {"size": "M", ...}
        422  This line's size is 'M', which is a garment size...

    It was refused because the line might have been style-wide, in which case 'M'
    could have meant "for M garments" and nothing said so. A line that names its SKU
    has already said which garment it is for, so 'M' is unambiguously the material's
    own size.
    """
    st, skus = sized_style
    svc = StyleSpecService(db)
    out = await svc.add_line(st.id, _thread(skus["M"], "M"), actor_name="DM")
    assert out["size"] == "M"
    assert out["garment_size"] is None


# ══════════════════════════════════════════════ the fan-out, which makes it usable
async def test_one_call_covers_every_sku(db, sized_style):
    """85-90% OF ACCESSORIES ARE THE SAME ON EVERY GARMENT, so the common case must
    not be one request per SKU. The STORED shape is still per-SKU — the convenience
    is in the request, never in the data, because a row that "applies to everything"
    is exactly what this change removed."""
    st, skus = sized_style
    svc = StyleSpecService(db)
    out = await svc.add_line(st.id, {
        "apply_to": "ALL_SKUS",
        "category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-18L",
        "colour": "NAVY", "size": "18L", "qty_per_piece": 3}, actor_name="DM")

    assert out["created"] == 3
    stored = [l for l in await svc.repo.lines_for_style(st.id)
              if l.article == "BTN-18L"]
    assert {l.sku_id for l in stored} == {k.id for k in skus.values()}
    assert all(l.garment_size is None for l in stored)


async def test_the_fan_out_is_idempotent_and_partially_so(db, sized_style):
    """Re-posting after a timeout is the normal way this endpoint gets called
    twice. A batch that finds some already present must add the rest and say so,
    not 409 the whole thing."""
    st, skus = sized_style
    svc = StyleSpecService(db)
    body = {"category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-18L",
            "colour": "NAVY", "size": "18L", "qty_per_piece": 3}

    await svc.add_line(st.id, dict(body, sku_id=skus["M"].id), actor_name="DM")
    out = await svc.add_line(st.id, dict(body, apply_to="ALL_SKUS"),
                             actor_name="DM")

    assert out["created"] == 2 and out["already_present"] == 1
    stored = [l for l in await svc.repo.lines_for_style(st.id)
              if l.article == "BTN-18L"]
    assert len(stored) == 3, "no duplicate for the SKU that already had one"


async def test_per_sku_sizes_land_on_the_right_garments(db, sized_style):
    """THE ZIP CASE. Most accessories are identical across SKUs; a zip is cut to the
    garment's length, so its size follows the SKU — which is what
    `size_varies_by_sku` on the accessory catalogue records."""
    st, skus = sized_style
    svc = StyleSpecService(db)
    out = await svc.add_line(st.id, {
        "category": "ACCESSORY", "subtype": "ZIP", "article": "ZIP-N",
        "colour": "NAVY", "qty_per_piece": 1,
        "per_sku": [{"sku_id": skus["S"].id, "size": "55"},
                    {"sku_id": skus["M"].id, "size": "60"},
                    {"sku_id": skus["L"].id, "size": "65"}],
    }, actor_name="DM")

    assert out["created"] == 3
    stored = {l.sku_id: l.size for l in await svc.repo.lines_for_style(st.id)
              if l.article == "ZIP-N"}
    assert stored == {skus["S"].id: "55", skus["M"].id: "60",
                      skus["L"].id: "65"}


async def test_naming_the_scope_twice_is_refused(db, sized_style):
    """Two scopes cannot be reconciled, and silently preferring one is how the
    wrong button ends up issued."""
    from fastapi import HTTPException
    st, skus = sized_style
    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).add_line(st.id, {
            "apply_to": "ALL_SKUS", "sku_id": skus["M"].id,
            "category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-18L",
            "colour": "NAVY", "size": "18L", "qty_per_piece": 3}, actor_name="DM")
    assert exc.value.status_code == 422
    assert "more than once" in str(exc.value.detail)


# ══════════════════════════════════════════════ leather and lining are unchanged
async def test_the_italian_ladder_still_matches_for_leather_and_lining(
        db, sized_style):
    """'52' and 'L' are the same garment. The order sheets use both, and a
    style-wide lining line still scopes itself by garment_size."""
    svc = StyleSpecService(db)
    assert svc.applies_to_size(type("L", (), {"garment_size": "52"})(), "L")
    assert svc.applies_to_size(type("L", (), {"garment_size": "L"})(), "52")
    assert not svc.applies_to_size(type("L", (), {"garment_size": "52"})(), "S")


async def test_the_fan_out_works_on_a_RELEASED_style(db, sized_style):
    """THE DOOR THAT MUST KEEP WORKING AFTER RELEASE, and the reason the fan-out
    lives on add_line rather than on the whole-grid PUT.

    `_assert_editable` freezes leather and lining at release — they are what the
    garments were cut and costed against — but deliberately leaves ACCESSORIES
    correctable (backend fix #12): "after release, if the DM finds an incorrect
    accessory assignment, there is currently no option to edit it". The wrong button
    is discovered precisely when somebody goes to fetch it, which is always after
    release.

    `replace_spec` asks `_assert_editable` with no category, so the PUT is frozen
    post-release. A fan-out that lived only there would be unusable at exactly the
    moment it is needed.
    """
    st, skus = sized_style
    st.production_status = "RELEASED"
    await db.commit()
    svc = StyleSpecService(db)

    out = await svc.add_line(st.id, {
        "apply_to": "ALL_SKUS",
        "category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-FIX",
        "colour": "NAVY", "size": "18L", "qty_per_piece": 2}, actor_name="DM")
    assert out["created"] == 3

    # …while leather is still frozen, which is the other half of the rule.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await svc.add_line(st.id, {
            "category": "LEATHER", "article": "SUEDE-A32", "colour": "NAVY",
            "thickness": "1.2mm", "qty_per_piece": 12.5}, actor_name="DM")
    assert exc.value.status_code == 409


async def test_per_sku_may_carry_its_own_quantity(db, sized_style):
    """A bigger garment can take more thread. The per-SKU door carries qty as well
    as size, so that is one call rather than three."""
    st, skus = sized_style
    svc = StyleSpecService(db)
    await svc.add_line(st.id, {
        "category": "ACCESSORY", "subtype": "THREAD", "article": "THREAD-40",
        "colour": "NAVY", "thickness": "40", "qty_per_piece": 2,
        "per_sku": [{"sku_id": skus["S"].id},
                    {"sku_id": skus["L"].id, "qty_per_piece": 3}],
    }, actor_name="DM")

    stored = {l.sku_id: float(l.qty_per_piece)
              for l in await svc.repo.lines_for_style(st.id)}
    assert stored[skus["S"].id] == 2.0, "the body's quantity is the default"
    assert stored[skus["L"].id] == 3.0, "…and the entry overrides it"


async def test_apply_to_all_skus_on_a_style_with_no_breakdown_says_so(db):
    """ALL_SKUS over nothing is not "applied to everything", it is a silent no-op —
    so it says what is missing instead."""
    from fastapi import HTTPException
    from app.modules.clients.models import Client, ClientOrder, Style
    cl = Client(id=uuid.uuid4(), name="X")
    db.add(cl)
    await db.flush()
    o = ClientOrder(id=uuid.uuid4(), client_id=cl.id, order_number="NO-SKUS")
    db.add(o)
    await db.flush()
    st = Style(id=uuid.uuid4(), client_order_id=o.id, name="BARE",
               article="B1", code="ST-BARE")
    db.add(st)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).add_line(st.id, {
            "apply_to": "ALL_SKUS", "category": "ACCESSORY", "subtype": "BUTTON",
            "article": "BTN-1", "colour": "NAVY", "size": "18L",
            "qty_per_piece": 4}, actor_name="DM")
    assert exc.value.status_code == 422
    assert "no ordered SKUs" in str(exc.value.detail)
    assert "breakdown" in str(exc.value.detail)
