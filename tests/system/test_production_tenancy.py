"""
SYSTEM · cross-tenant data leakage on the production read endpoints.

THE SHAPE OF THE BUG THIS FILE LOOKS FOR
    `client_scope` (production/router.py:37-41) is the tenancy filter: it returns
    the caller's own client_id for a CLIENT login and None for staff. The router
    threads it into three service calls (:71, :98, :110). Analytics does exactly
    the same thing (analytics/router.py:23-32) and its service APPLIES the
    predicate — `order_tree` adds `.where(ClientOrder.client_id == client_scope)`
    (analytics/service.py:169-170) so a substituted competitor id resolves to 404.

    Production's service accepts the kwarg and never reads it. So the filter is
    present at every layer a reviewer would look at, and enforced at none.

    That is why these tests are written against TWO clients with real rows: a
    signature check would pass, and only data can show the leak.

Layer note: F140 fixed the TypeError by ADDING the parameter. These tests are
what F140 should have been — they fail until the predicate is applied.
"""
import uuid

import pytest

from app.core.enums import UserRole
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.production.models import Piece

API = "/api/v1"


@pytest.fixture
async def two_clients(db):
    """Two competitors in one factory, each with an order, style, SKU and piece.

    This is the only shape in which a tenancy bug is visible: with one client,
    an unfiltered query and a correctly filtered one return the same rows.
    """
    out = {}
    for tag in ("ACME", "RIVAL"):
        client = Client(name=f"{tag} LEATHER", country="IT")
        db.add(client)
        await db.flush()
        order = ClientOrder(client_id=client.id, order_number=f"{tag}-PO")
        db.add(order)
        await db.flush()
        style = Style(client_order_id=order.id, name=f"{tag}STYLE",
                      article=f"{tag}1", code=f"{tag}STYLE")
        db.add(style)
        await db.flush()
        sku = SKU(style_id=style.id, color_code="BLK", color_name="BLACK",
                  size="M", qty_ordered=3, code=f"{tag}-PO-{tag}STYLE-BLK-M")
        db.add(sku)
        await db.flush()
        piece = Piece(code=f"{tag}-PIECE-001", seq=1, sku_id=sku.id,
                      current_operation_id=None)
        db.add(piece)
        await db.flush()
        out[tag] = {"client": client, "order": order, "style": style,
                    "sku": sku, "piece": piece}
    await db.commit()
    return out


# ═══════════════════════════════════════════════ the SKU picker leaks everything
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_client_login_sees_only_its_own_skus(api_client, as_role,
                                                     two_clients):
    """FINDING F147. `list_sku_options` (production/service.py:438-439) accepts
    `client_scope` and then calls `self.clients.list_sku_options(order_id=...,
    style_id=...)` — the scope is never passed on and never applied.

    So ACME's login enumerates RIVAL's SKU codes. A SKU code is
    order·style·colour·size, i.e. it names a competitor's customer, garment,
    colourway and size run in one string.
    """
    as_role(UserRole.CLIENT, client_id=two_clients["ACME"]["client"].id)
    r = await api_client.get(f"{API}/production/skus")
    assert r.status_code == 200

    codes = [row.get("sku_code") or row.get("code") for row in r.json()]
    assert any("ACME" in (c or "") for c in codes), "the caller lost their own data"
    assert not any("RIVAL" in (c or "") for c in codes), (
        f"cross-tenant leak: ACME's token returned RIVAL's SKUs — {codes}")


@pytest.mark.security
@pytest.mark.asyncio
async def test_a_client_cannot_read_another_clients_style_progress(
        api_client, as_role, two_clients):
    """FINDING F147. `style_progress` (production/service.py:435-436) ignores the
    scope and returns `stage_totals_for_style(style_id)` for ANY style id.

    Production volume per stage is a competitor's throughput. The analytics
    equivalent 404s a cross-tenant id on purpose (analytics/service.py:167-173),
    because existence itself is information.
    """
    as_role(UserRole.CLIENT, client_id=two_clients["ACME"]["client"].id)
    rival_style = two_clients["RIVAL"]["style"].id

    r = await api_client.get(f"{API}/production/styles/{rival_style}/progress")
    assert r.status_code == 404, (
        f"cross-tenant leak: ACME read RIVAL's stage progress "
        f"(HTTP {r.status_code}, body={r.text[:200]})")


@pytest.mark.security
@pytest.mark.asyncio
async def test_a_client_cannot_enumerate_another_clients_pieces(
        api_client, as_role, two_clients):
    """FINDING F147. `list_pieces_for_sku` (production/service.py:376-378) takes
    `client_scope` and its body never mentions it again — it resolves the SKU and
    returns every piece with code, seq and current stage.

    This is the per-garment traceability surface. Handed to the wrong client it
    is a live feed of a competitor's work in progress.
    """
    as_role(UserRole.CLIENT, client_id=two_clients["ACME"]["client"].id)
    rival_sku = two_clients["RIVAL"]["sku"].id

    r = await api_client.get(f"{API}/production/skus/{rival_sku}/pieces")
    assert r.status_code == 404, (
        f"cross-tenant leak: ACME enumerated RIVAL's pieces "
        f"(HTTP {r.status_code}, body={r.text[:200]})")


# ═══════════════════════════════════ the same endpoints, for staff (control)
@pytest.mark.asyncio
async def test_staff_still_read_across_clients(api_client, as_role, two_clients):
    """The control that stops the fix from over-correcting. `client_scope`
    returns None for every non-CLIENT role, and the factory's own managers
    legitimately work across all clients — a fix that scoped staff to nothing
    would break the floor."""
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/production/skus")
    assert r.status_code == 200

    codes = [row.get("sku_code") or row.get("code") for row in r.json()]
    assert any("ACME" in (c or "") for c in codes)
    assert any("RIVAL" in (c or "") for c in codes)


@pytest.mark.asyncio
async def test_staff_may_read_any_styles_progress(api_client, as_role, two_clients):
    as_role(UserRole.DIRECT_MANAGER)
    for tag in ("ACME", "RIVAL"):
        r = await api_client.get(
            f"{API}/production/styles/{two_clients[tag]['style'].id}/progress")
        assert r.status_code == 200


# ═════════════════════════════════════════════ IDOR on a non-existent id
@pytest.mark.security
@pytest.mark.asyncio
async def test_an_unknown_sku_id_is_404_for_everyone(api_client, as_role):
    """A random id must not 500. This is the baseline the tenancy 404 has to be
    indistinguishable from — if 'not yours' and 'not there' answered differently,
    the difference would itself enumerate the other tenant's ids."""
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/production/skus/{uuid.uuid4()}/pieces")
    assert r.status_code == 404


@pytest.mark.security
@pytest.mark.asyncio
async def test_the_tenancy_404_is_indistinguishable_from_a_missing_row(
        api_client, as_role, two_clients):
    """Both must be a bare 404. If the cross-tenant case answered 403, the
    caller would learn that the id EXISTS and belongs to someone else — which is
    the fact the scoping was meant to hide."""
    as_role(UserRole.CLIENT, client_id=two_clients["ACME"]["client"].id)

    foreign = await api_client.get(
        f"{API}/production/skus/{two_clients['RIVAL']['sku'].id}/pieces")
    missing = await api_client.get(
        f"{API}/production/skus/{uuid.uuid4()}/pieces")

    assert foreign.status_code == missing.status_code == 404
