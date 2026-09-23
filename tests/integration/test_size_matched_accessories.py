"""
INTEGRATION · an accessory line reaches only the garments it is for.

THE BUG, AS REPORTED. "When DM assigned Thread S, M, and L, the Store showed all
three sizes for every piece instead of showing only the matching size."

THE CAUSE. `StyleSpecService.merge_lines` keyed the effective recipe on
(category, subtype, article) and never read SKU.size, so a DM entering three
sizes of one accessory got one of two wrong answers and could not tell which:

    three distinct ARTICLES   -> three distinct keys -> all three on every
                                 garment of every size
    one article, three SIZES  -> one key             -> only the last survives,
                                 silently

ALL FOUR SURFACES went through that function — the kit checklist, the scan
payload, the production log's kit block and the actual spend — so the store
rendered three threads for one jacket AND the kit scan spent all three.

WHY A NEW COLUMN. `style_material_spec.size` already means the MATERIAL's size
("zip 60cm, button 18L") and feeds lot resolution. `garment_size` is which
garments the line is for. Overloading one onto the other would make a 60cm zip
look like a garment size and confine it to a size that does not exist.
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


def _thread(size):
    return {"category": "ACCESSORY", "subtype": "THREAD", "article": "THREAD-40",
            "colour": "NAVY", "thickness": "40", "size": size,
            "qty_per_piece": 2}


async def test_a_sized_accessory_reaches_only_its_own_size(db, sized_style):
    """The reported bug, pinned. Thread L belongs on an L jacket and nowhere else."""
    st, skus = sized_style
    svc = StyleSpecService(db)
    await svc.replace_spec(st.id, [_thread(s) for s in ("S", "M", "L")],
                           actor_name="DM")
    for size in ("S", "M", "L"):
        lines = await svc.effective_lines(st.id, skus[size].id)
        acc = [l for l in lines if l.category == "ACCESSORY"]
        assert len(acc) == 1, f"a size-{size} garment got {len(acc)} thread lines"
        assert acc[0].garment_size == size


async def test_three_sizes_are_three_lines_not_one_overwritten(db, sized_style):
    """The other failure mode, and the quieter one.

    With `size` in the identity key but not `garment_size`, the second and third
    lines overwrote the first and the DM's entry silently became one line.
    """
    st, skus = sized_style
    svc = StyleSpecService(db)
    await svc.replace_spec(st.id, [_thread(s) for s in ("S", "M", "L")],
                           actor_name="DM")
    stored = await svc.repo.lines_for_style(st.id)
    threads = [l for l in stored if l.article == "THREAD-40"]
    assert len(threads) == 3
    assert {l.garment_size for l in threads} == {"S", "M", "L"}


async def test_an_unsized_accessory_still_reaches_every_garment(db, sized_style):
    """NULL garment_size MEANS EVERY SIZE, and that is the back-compatibility.

    Every line that predates the column has NULL, so every already-released style
    resolves to exactly the recipe it resolved to before this change.
    """
    st, skus = sized_style
    svc = StyleSpecService(db)
    await svc.replace_spec(st.id, [
        {"category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-18L",
         "colour": "NAVY", "size": "18L", "qty_per_piece": 3}],
        actor_name="DM")
    for size in ("S", "M", "L"):
        lines = await svc.effective_lines(st.id, skus[size].id)
        acc = [l for l in lines if l.category == "ACCESSORY"]
        assert len(acc) == 1
        assert acc[0].garment_size is None, "18L is a BUTTON size, not a garment size"


async def test_a_material_size_is_not_mistaken_for_a_garment_size(db, sized_style):
    """A 60cm zip fits every jacket. Reading '60cm' as a garment size would
    confine it to a size that does not exist, and the kit would never issue."""
    st, skus = sized_style
    svc = StyleSpecService(db)
    await svc.replace_spec(st.id, [
        {"category": "ACCESSORY", "subtype": "ZIP", "article": "ZIP-N",
         "colour": "NAVY", "size": "60cm", "qty_per_piece": 1}],
        actor_name="DM")
    for size in ("S", "M", "L"):
        lines = await svc.effective_lines(st.id, skus[size].id)
        assert [l.article for l in lines if l.category == "ACCESSORY"] == ["ZIP-N"]


async def test_a_zip_labelled_with_a_garment_size_matches_that_garment(db, sized_style):
    """Your case: 'size of zip L is automatically merged to the size-L piece'.

    The DM enters what they always entered — a zip labelled L — and the matching
    starts working, because a material size that reads as a garment size defaults
    `garment_size` to it.
    """
    st, skus = sized_style
    svc = StyleSpecService(db)
    await svc.replace_spec(st.id, [
        {"category": "ACCESSORY", "subtype": "ZIP", "article": "ZIP-N",
         "colour": "NAVY", "size": s, "qty_per_piece": 1} for s in ("S", "M", "L")],
        actor_name="DM")
    for size in ("S", "M", "L"):
        lines = await svc.effective_lines(st.id, skus[size].id)
        acc = [l for l in lines if l.category == "ACCESSORY"]
        assert len(acc) == 1 and acc[0].garment_size == size


async def test_the_italian_ladder_matches_the_alpha_one(db, sized_style):
    """'52' and 'L' are the same garment. The order sheets use both."""
    st, skus = sized_style
    svc = StyleSpecService(db)
    assert svc.applies_to_size(type("L", (), {"garment_size": "52"})(), "L")
    assert svc.applies_to_size(type("L", (), {"garment_size": "L"})(), "52")
    assert not svc.applies_to_size(type("L", (), {"garment_size": "52"})(), "S")
