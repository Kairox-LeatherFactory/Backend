"""
INTEGRATION · client edit + delete — the two operations the module was missing.

Clients could only be CREATED and LISTED. A mistyped name or a duplicated row
had no way out of the system, and a client who stopped trading stayed on every
dropdown forever.

THE ONE IDEA WORTH PINNING
    DELETE and DEACTIVATE are not two flavours of the same thing here, and which
    one you get is decided by the data, not by a flag:

      no orders   → a hard delete is safe, because nothing hangs off the row.
      any orders  → 409. `Client.client_orders` cascades `all, delete-orphan`,
                    so deleting would take the client's orders, styles and SKUs,
                    and every Piece / production event / wage line hangs off
                    those SKUs. That history is exactly what CLAUDE.md §6
                    refuses to destroy for a departing worker; a client is no
                    different. `PATCH {"is_active": false}` is the answer, and
                    the 409 says so.
"""
import uuid

import pytest
from fastapi import HTTPException

from app.modules.clients.models import Client, ClientOrder
from app.modules.clients.service import ClientService

pytestmark = pytest.mark.integrity


async def _bare_client(db, name="ACME", **kw):
    """A client with no orders — the only kind that may be hard-deleted."""
    c = Client(name=name, **kw)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


# ══════════════════════════════════════════════════════════════════════ edit
@pytest.mark.asyncio
async def test_edit_writes_only_the_fields_that_were_sent(db):
    """PARTIAL update. A form that posts one field must not blank the others —
    this is the whole reason the router passes `exclude_unset=True`."""
    c = await _bare_client(db, name="OLD NAME", country="IT", currency="EUR")

    updated = await ClientService(db).update_client(c.id, {"name": "NEW NAME"})

    assert updated.name == "NEW NAME"
    assert updated.country == "IT", "an unsent field must be left alone"
    assert updated.currency == "EUR"


@pytest.mark.asyncio
async def test_sending_null_clears_a_field(db):
    """Omitting a key and sending it as null are DIFFERENT requests: the first
    leaves the value, the second clears it."""
    c = await _bare_client(db, name="ACME", country="IT")

    updated = await ClientService(db).update_client(c.id, {"country": None})

    assert updated.country is None


@pytest.mark.asyncio
async def test_a_duplicate_code_is_a_conflict_not_a_crash(db):
    """`Client.code` is unique. A collision must be a 409 the frontend can show,
    not the 500 an unhandled IntegrityError would produce."""
    await _bare_client(db, name="FIRST", code="KJ")
    second = await _bare_client(db, name="SECOND", code="GGZ")

    with pytest.raises(HTTPException) as exc:
        await ClientService(db).update_client(second.id, {"code": "KJ"})
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_a_client_may_keep_its_own_code(db):
    """The collision check must compare against OTHER clients only — resaving a
    form without touching `code` is not a conflict with yourself."""
    c = await _bare_client(db, name="ACME", code="KJ")

    updated = await ClientService(db).update_client(
        c.id, {"code": "KJ", "country": "FR"})

    assert updated.code == "KJ" and updated.country == "FR"


@pytest.mark.asyncio
async def test_a_blank_name_is_refused(db):
    """The name is the client's identity on every screen; whitespace is not a
    name, and the row would be unnameable afterwards."""
    c = await _bare_client(db, name="ACME")

    with pytest.raises(HTTPException) as exc:
        await ClientService(db).update_client(c.id, {"name": "   "})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_editing_an_unknown_client_is_404(db):
    with pytest.raises(HTTPException) as exc:
        await ClientService(db).update_client(uuid.uuid4(), {"name": "X"})
    assert exc.value.status_code == 404


# ════════════════════════════════════════════════════════ deactivate + list
@pytest.mark.asyncio
async def test_deactivating_hides_a_client_from_the_default_list(db):
    """Deactivation is the retire path for a client who HAS traded — they leave
    the dropdowns, and nothing they own is touched."""
    keep = await _bare_client(db, name="ACTIVE ONE")
    gone = await _bare_client(db, name="RETIRED ONE")
    svc = ClientService(db)

    await svc.update_client(gone.id, {"is_active": False})

    names = {c.name for c in await svc.list_clients()}
    assert keep.name in names
    assert gone.name not in names

    all_names = {c.name for c in await svc.list_clients(include_inactive=True)}
    assert {keep.name, gone.name} <= all_names, "the row is hidden, not deleted"


@pytest.mark.asyncio
async def test_reactivating_puts_a_client_back_on_the_list(db):
    """Deactivation is reversible — that is the difference between it and the
    delete below, and the reason it is the right answer for a client who has
    traded."""
    c = await _bare_client(db, name="BACK AGAIN")
    svc = ClientService(db)

    await svc.update_client(c.id, {"is_active": False})
    assert "BACK AGAIN" not in {x.name for x in await svc.list_clients()}

    await svc.update_client(c.id, {"is_active": True})
    assert "BACK AGAIN" in {x.name for x in await svc.list_clients()}


@pytest.mark.asyncio
async def test_a_new_client_is_active_by_default(db):
    """Nothing has to set the flag for a client to be visible — otherwise every
    creation path would need updating and one of them would be missed."""
    c = await _bare_client(db, name="FRESH")
    assert c.is_active is True
    assert "FRESH" in {x.name for x in await ClientService(db).list_clients()}


# ════════════════════════════════════════════════════════════════════ delete
@pytest.mark.asyncio
async def test_a_client_with_no_orders_is_deleted(db):
    """The case delete exists for: a mistyped or duplicated row with nothing
    hanging off it."""
    c = await _bare_client(db, name="TYPO LTD")
    svc = ClientService(db)

    await svc.delete_client(c.id)

    assert await svc.repo.get_client(c.id) is None
    assert "TYPO LTD" not in {x.name for x in await svc.list_clients()}


@pytest.mark.asyncio
async def test_a_client_with_orders_is_refused_and_told_what_to_do(db, order_tree):
    """THE GUARD. Deleting here would cascade into the order, its styles, its
    SKUs and every piece and wage line built on them."""
    client = order_tree["client"]

    with pytest.raises(HTTPException) as exc:
        await ClientService(db).delete_client(client.id)

    assert exc.value.status_code == 409
    detail = str(exc.value.detail)
    assert "is_active" in detail, "the error must name the deactivate call"

    # and nothing was destroyed on the way to the refusal
    assert await ClientService(db).repo.get_client(client.id) is not None
    assert await db.get(ClientOrder, order_tree["order"].id) is not None


@pytest.mark.asyncio
async def test_deleting_an_unknown_client_is_404(db):
    with pytest.raises(HTTPException) as exc:
        await ClientService(db).delete_client(uuid.uuid4())
    assert exc.value.status_code == 404
