"""
INTEGRATION · StyleSpecService — the per-piece recipe, and spending it.

THE SECOND MONEY PATH IN THIS MODULE. Before the recipe existed, the only
material the factory could spend automatically was leather, and its quantity was
typed by hand on every scan; accessories could be stocked and barcoded but never
decremented, so accessory stock only ever went up. This service holds the
recipe, the gate that makes a style declare it before release, and the primitive
that spends it when the store kits a garment.

WHAT IS COVERED HERE, in the order the DM meets it:

    authoring     _clean_line's validation, add/patch/delete, replace_spec's
                  idempotent diff, confirm's sign-off, copy_from
    projection    requirement() — the screen that should stop an order
    resolution    _resolve_lot: PINNED / MATCHED / NONE / AMBIGUOUS
    the surfaces  blockers_for_styles, kit_by_pieces,
                  material_requirement_block, piece_materials
    THE SPEND     issue_kit_nocommit and issue_manual — idempotent, partial,
                  warn-never-block
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.enums import KitStatus, MaterialIssueSource
from app.modules.barcode.models import (
    BarcodeRegistry, MaterialLot, MaterialReservation, PieceMaterialIssue,
    StyleMaterialSpec,
)
from app.modules.clients.models import SKU
from app.modules.materials.style_spec_service import (
    RESOLUTION_AMBIGUOUS, RESOLUTION_MATCHED, RESOLUTION_NONE,
    RESOLUTION_PINNED, StyleSpecService,
)
from app.modules.production.models import ProductionEvent

pytestmark = pytest.mark.integrity

ACTOR = uuid.uuid4()
BY = "DM ISHTIYAQUE"


# ══════════════════════════════════════════════════════════════ helpers
def leather_line(**kw):
    base = dict(category="LEATHER", article="SUEDE-A32", colour="PINE",
                thickness="1.2mm", qty_per_piece=12.5)
    base.update(kw)
    return base


def button_line(**kw):
    base = dict(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                colour="BLACK", size="18L", qty_per_piece=4)
    base.update(kw)
    return base


async def make_lot(db, *, category="ACCESSORY", subtype="BUTTON",
                   article="BTN-4H", colour="BLACK", thickness=None,
                   size="18L", uom="pcs", on_hand=500, is_active=True):
    lot = MaterialLot(category=category, subtype=subtype, article=article,
                      colour=colour, thickness=thickness, size=size, uom=uom,
                      on_hand=Decimal(str(on_hand)), is_active=is_active)
    db.add(lot)
    await db.commit()
    await db.refresh(lot)
    return lot


async def orphan_piece(db, piece):
    """Point a piece at a SKU whose style no longer exists.

    `piece.sku_id` is NOT NULL, so "this piece has no parent style" cannot be
    modelled by nulling it — and that is the state the three `if style is None`
    branches in this service guard against. A SKU whose style_id names nothing
    is the reachable shape (a style deleted out from under its colourways).
    """
    from app.modules.clients.models import SKU as SKUModel
    dangling = SKUModel(style_id=uuid.uuid4(), color_code="X", size="X",
                        qty_ordered=1, code=f"ORPHAN-{uuid.uuid4().hex[:8]}")
    db.add(dangling)
    await db.flush()
    piece.sku_id = dangling.id
    await db.commit()
    await db.refresh(piece)
    return piece


@pytest.fixture
async def draft_style(db, order_tree):
    """The order tree's style, back in DRAFT so its recipe may be written."""
    style = order_tree["style"]
    style.production_status = "DRAFT"
    style.code = "JP-CLERMONT"
    await db.commit()
    return style


# ══════════════════════════════════════════════════════ authoring: one line
class TestAddLine:
    async def test_a_line_is_stored_with_its_unit_derived_from_the_material(
            self, db, draft_style):
        """`uom` IS ACCEPTED BUT IGNORED — buttons are pcs whatever a client
        posts. Honouring the caller's unit would let one style measure thread in
        yards while telling the ledger 'mtrs'."""
        out = await StyleSpecService(db).add_line(
            draft_style.id, button_line(uom="dozens"), actor_name=BY)
        assert out["uom"] == "pcs"
        assert out["scope"] == "STYLE"
        assert out["qty_per_piece"] == 4.0
        assert out["is_active"] is True

    async def test_an_unknown_material_kind_names_the_list_it_accepts(
            self, db, draft_style):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                draft_style.id, {"category": "TIMBER", "qty_per_piece": 1},
                actor_name=BY)
        assert e.value.status_code == 422
        assert "LEATHER" in e.value.detail and "/materials/spec" in e.value.detail

    async def test_an_accessory_line_must_name_an_article(self, db, draft_style):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                draft_style.id, button_line(article="  "), actor_name=BY)
        assert e.value.status_code == 422
        assert "must name an article" in e.value.detail

    @pytest.mark.parametrize("category,extra", [
        ("LEATHER", {"article": "SUEDE-A32"}),
        ("LINING", {"article": "POLY-1", "subtype": "PLAIN_LINING"}),
    ])
    async def test_leather_and_lining_lines_must_carry_a_thickness(
            self, db, draft_style, category, extra):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                draft_style.id,
                {"category": category, "qty_per_piece": 1, **extra},
                actor_name=BY)
        assert e.value.status_code == 422
        assert "must include thickness" in e.value.detail

    async def test_a_non_numeric_quantity_is_refused(self, db, draft_style):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                draft_style.id, button_line(qty_per_piece="four"), actor_name=BY)
        assert e.value.status_code == 422 and "must be numeric" in e.value.detail

    async def test_a_negative_quantity_is_refused(self, db, draft_style):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                draft_style.id, button_line(qty_per_piece=-1), actor_name=BY)
        assert e.value.status_code == 422 and "cannot be negative" in e.value.detail

    async def test_zero_is_illegal_on_a_style_wide_line(self, db, draft_style):
        """It would be a recipe entry that consumes nothing — a typo with a row
        in it. Zero is how a per-SKU override says 'not this one'."""
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                draft_style.id, button_line(qty_per_piece=0), actor_name=BY)
        assert e.value.status_code == 422
        assert "must consume something" in e.value.detail

    async def test_zero_is_legal_on_a_per_sku_override(self, db, draft_style,
                                                       order_tree):
        out = await StyleSpecService(db).add_line(
            draft_style.id,
            button_line(qty_per_piece=0, sku_id=order_tree["sku"].id),
            actor_name=BY)
        assert out["scope"] == "SKU" and out["qty_per_piece"] == 0.0

    async def test_an_override_may_only_name_a_colourway_of_its_own_style(
            self, db, draft_style):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                draft_style.id, button_line(sku_id=uuid.uuid4()), actor_name=BY)
        assert e.value.status_code == 422
        assert "does not belong to" in e.value.detail

    async def test_a_sized_material_infers_the_garment_size_it_belongs_on(
            self, db, draft_style):
        """A 'zip L' is the zip for an L jacket, and the DM enters exactly
        that — so the matching starts working without a new field to fill in."""
        out = await StyleSpecService(db).add_line(
            draft_style.id,
            button_line(subtype="ZIP", article="ZIP-1", size="L"),
            actor_name=BY)
        assert out["garment_size"] == "L"

    async def test_a_material_measurement_is_not_read_as_a_garment_size(
            self, db, draft_style):
        out = await StyleSpecService(db).add_line(
            draft_style.id,
            button_line(subtype="ZIP", article="ZIP-1", size="60CM"),
            actor_name=BY)
        assert out["garment_size"] is None

    async def test_an_explicit_garment_size_wins_over_the_inference(
            self, db, draft_style):
        out = await StyleSpecService(db).add_line(
            draft_style.id, button_line(size="18L", garment_size="S"),
            actor_name=BY)
        assert out["garment_size"] == "S"

    async def test_the_same_material_twice_is_a_409_pointing_at_the_first_line(
            self, db, draft_style):
        """Two lines for one material would both be issued."""
        first = await StyleSpecService(db).add_line(
            draft_style.id, button_line(), actor_name=BY)
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                draft_style.id, button_line(), actor_name=BY)
        assert e.value.status_code == 409
        assert first["line_id"] in e.value.detail

    async def test_an_unknown_style_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                uuid.uuid4(), button_line(), actor_name=BY)
        assert e.value.status_code == 404


class TestTheFreeze:
    async def test_a_released_styles_leather_line_may_not_be_edited(
            self, db, order_tree):
        """Editing it would rewrite what a garment was costed at AFTER it was
        cut, and the cutting record and the costing would disagree."""
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).add_line(
                order_tree["style"].id, leather_line(), actor_name=BY)
        assert e.value.status_code == 409
        assert "frozen" in e.value.detail
        assert "/materials/issues" in e.value.detail

    async def test_an_accessory_line_stays_correctable_after_release(
            self, db, order_tree):
        """Backend fix #12. The wrong button is discovered precisely when
        somebody goes to fetch it, which is always after release."""
        out = await StyleSpecService(db).add_line(
            order_tree["style"].id, button_line(), actor_name=BY)
        assert out["article"] == "BTN-4H"

    async def test_the_freeze_is_judged_on_the_line_being_edited_not_the_patch(
            self, db, order_tree):
        line = await StyleSpecService(db).add_line(
            order_tree["style"].id, button_line(), actor_name=BY)
        out = await StyleSpecService(db).patch_line(
            order_tree["style"].id, uuid.UUID(line["line_id"]),
            {"qty_per_piece": 6}, actor_name=BY)
        assert out["qty_per_piece"] == 6.0

    async def test_a_released_styles_leather_line_may_not_be_deleted(
            self, db, draft_style, order_tree):
        line = await StyleSpecService(db).add_line(
            draft_style.id, leather_line(), actor_name=BY)
        draft_style.production_status = "RELEASED"
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).deactivate_line(
                draft_style.id, uuid.UUID(line["line_id"]), actor_name=BY)
        assert e.value.status_code == 409

    async def test_the_whole_grid_may_not_be_replaced_after_release(
            self, db, order_tree):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).replace_spec(
                order_tree["style"].id, [button_line()], actor_name=BY)
        assert e.value.status_code == 409


class TestPatchLine:
    async def test_omitted_fields_keep_their_current_value(self, db, draft_style):
        line = await StyleSpecService(db).add_line(
            draft_style.id, button_line(), actor_name=BY)
        out = await StyleSpecService(db).patch_line(
            draft_style.id, uuid.UUID(line["line_id"]), {"qty_per_piece": 6},
            actor_name=BY)
        assert out["qty_per_piece"] == 6.0
        assert out["article"] == "BTN-4H" and out["colour"] == "BLACK"

    async def test_a_line_that_belongs_to_another_style_is_a_404(
            self, db, draft_style, order_tree):
        line = await StyleSpecService(db).add_line(
            draft_style.id, button_line(), actor_name=BY)
        other = order_tree["style"]
        other.production_status = "DRAFT"
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).patch_line(
                uuid.uuid4(), uuid.UUID(line["line_id"]), {"qty_per_piece": 1},
                actor_name=BY)
        assert e.value.status_code == 404

    async def test_an_unknown_line_is_a_404(self, db, draft_style):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).patch_line(
                draft_style.id, uuid.uuid4(), {"qty_per_piece": 1},
                actor_name=BY)
        assert e.value.status_code == 404
        assert "No such line on this style" in e.value.detail


class TestDeactivateLine:
    async def test_removal_is_soft_so_the_ledger_still_resolves(
            self, db, draft_style):
        line = await StyleSpecService(db).add_line(
            draft_style.id, button_line(), actor_name=BY)
        out = await StyleSpecService(db).deactivate_line(
            draft_style.id, uuid.UUID(line["line_id"]), actor_name=BY)
        assert out["deactivated"] is True
        row = await db.get(StyleMaterialSpec, uuid.UUID(line["line_id"]))
        assert row is not None and row.is_active is False

    async def test_an_unknown_line_is_a_404(self, db, draft_style):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).deactivate_line(
                draft_style.id, uuid.uuid4(), actor_name=BY)
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════ authoring: the grid
class TestReplaceSpec:
    async def test_the_whole_grid_is_saved_in_one_call(self, db, draft_style):
        out = await StyleSpecService(db).replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        assert len(out["lines"]) == 2
        assert "Saved 2 line(s)" in out["message"]

    async def test_posting_the_same_body_twice_keeps_the_same_line_ids(
            self, db, draft_style):
        """A line already issued against is referenced by piece_material_issue;
        recreating it would orphan every issue that pointed at the old id."""
        svc = StyleSpecService(db)
        first = await svc.replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        second = await svc.replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        assert {l["line_id"] for l in first["lines"]} \
            == {l["line_id"] for l in second["lines"]}

    async def test_a_line_left_out_of_the_body_is_deactivated_not_deleted(
            self, db, draft_style):
        svc = StyleSpecService(db)
        first = await svc.replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        button = next(l for l in first["lines"] if l["article"] == "BTN-4H")
        out = await svc.replace_spec(
            draft_style.id, [leather_line()], actor_name=BY)
        assert "1 removed" in out["message"]
        row = await db.get(StyleMaterialSpec, uuid.UUID(button["line_id"]))
        assert row.is_active is False

    async def test_a_reinstated_line_reuses_its_original_row(self, db, draft_style):
        svc = StyleSpecService(db)
        first = await svc.replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        await svc.replace_spec(draft_style.id, [], actor_name=BY)
        again = await svc.replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        assert again["lines"][0]["line_id"] == first["lines"][0]["line_id"]

    async def test_a_changed_quantity_updates_in_place(self, db, draft_style):
        svc = StyleSpecService(db)
        first = await svc.replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        second = await svc.replace_spec(
            draft_style.id, [button_line(qty_per_piece=6)], actor_name=BY)
        assert second["lines"][0]["line_id"] == first["lines"][0]["line_id"]
        assert second["lines"][0]["qty_per_piece"] == 6.0

    async def test_the_same_material_twice_in_one_body_is_a_409(
            self, db, draft_style):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).replace_spec(
                draft_style.id, [button_line(), button_line()], actor_name=BY)
        assert e.value.status_code == 409
        assert "appears twice" in e.value.detail

    async def test_two_sizes_of_one_article_are_two_lines_not_one(
            self, db, draft_style):
        """garment_size is part of the identity. Leaving it out is what made the
        second line silently overwrite the first."""
        out = await StyleSpecService(db).replace_spec(
            draft_style.id,
            [button_line(subtype="THREAD", article="THR-40",
                         size=None, thickness="40", garment_size="S"),
             button_line(subtype="THREAD", article="THR-40",
                         size=None, thickness="40", garment_size="M")],
            actor_name=BY)
        assert len(out["lines"]) == 2

    async def test_the_amendment_is_audited(self, db, draft_style):
        from app.core.models import AuditLog
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY, actor_id=ACTOR)
        row = await db.scalar(select(AuditLog).where(
            AuditLog.action == "STYLE_MATERIAL_SPEC_AMENDED"))
        assert row.after["by"] == BY and row.after["added"] == 1


# ══════════════════════════════════════════════════════════════ the read
class TestGetSpec:
    async def test_the_grid_reports_status_confirmation_and_blockers(
            self, db, draft_style):
        await StyleSpecService(db).replace_spec(
            draft_style.id, [leather_line()], actor_name=BY)
        out = await StyleSpecService(db).get_spec(draft_style.id)
        assert out["style_name"] == "CLERMONT"
        assert out["editable"] is True
        assert out["confirmed"] is False
        assert out["sku_overrides_count"] == 0
        assert out["release_blockers"]          # not confirmed yet

    async def test_a_released_style_reports_itself_as_not_editable(
            self, db, order_tree):
        out = await StyleSpecService(db).get_spec(order_tree["style"].id)
        assert out["editable"] is False

    async def test_overrides_are_counted_separately(self, db, draft_style,
                                                    order_tree):
        await StyleSpecService(db).replace_spec(
            draft_style.id,
            [button_line(), button_line(sku_id=order_tree["sku"].id,
                                        qty_per_piece=6)],
            actor_name=BY)
        out = await StyleSpecService(db).get_spec(draft_style.id)
        assert out["sku_overrides_count"] == 1

    async def test_an_unknown_style_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).get_spec(uuid.uuid4())
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════ lot resolution
class TestLotResolution:
    async def test_exactly_one_matching_lot_resolves_matched(self, db, draft_style):
        await make_lot(db)
        out = await StyleSpecService(db).add_line(
            draft_style.id, button_line(), actor_name=BY)
        assert out["resolution"] == RESOLUTION_MATCHED
        assert out["lot"]["available"] == 500.0

    async def test_no_matching_lot_resolves_none_and_asks_for_stock(
            self, db, draft_style):
        out = await StyleSpecService(db).add_line(
            draft_style.id, button_line(), actor_name=BY)
        assert out["resolution"] == RESOLUTION_NONE
        assert out["lot"] is None

    async def test_several_matching_lots_resolve_ambiguous_with_candidates(
            self, db, draft_style):
        """A human has to pick. Same verdict the cut-lot picker reaches, but as
        DATA so one ambiguous button does not lose a whole kit."""
        await make_lot(db, thickness="A")
        await make_lot(db, thickness="B")
        out = await StyleSpecService(db).add_line(
            draft_style.id, button_line(thickness=None), actor_name=BY)
        assert out["resolution"] == RESOLUTION_AMBIGUOUS
        assert len(out["candidate_lot_ids"]) == 2

    async def test_a_named_lot_resolves_pinned(self, db, draft_style):
        lot = await make_lot(db, article="SOMETHING-ELSE")
        out = await StyleSpecService(db).add_line(
            draft_style.id, button_line(material_lot_id=lot.id), actor_name=BY)
        assert out["resolution"] == RESOLUTION_PINNED
        assert out["lot"]["lot_id"] == str(lot.id)

    async def test_a_pin_onto_a_retired_lot_falls_back_to_a_key_match(
            self, db, draft_style):
        """The pin is an optimisation, not a promise — a retired lot usually
        means the store re-received the same article under a new one."""
        dead = await make_lot(db, article="OLD", is_active=False)
        await make_lot(db)
        out = await StyleSpecService(db).add_line(
            draft_style.id, button_line(material_lot_id=dead.id), actor_name=BY)
        assert out["resolution"] == RESOLUTION_MATCHED

    async def test_a_reservation_is_shown_rather_than_silently_subtracted(
            self, db, draft_style):
        """Nothing in this codebase releases a reservation, so a stuck one would
        otherwise present as a phantom shortfall with no visible cause."""
        lot = await make_lot(db)
        db.add(MaterialReservation(material_lot_id=lot.id, qty=Decimal("100"),
                                   status="active"))
        await db.commit()
        out = await StyleSpecService(db).add_line(
            draft_style.id, button_line(), actor_name=BY)
        assert out["lot"]["reserved"] == 100.0
        assert out["lot"]["available"] == 400.0


# ══════════════════════════════════════════════════════════════ confirm
class TestConfirm:
    async def test_confirmation_stamps_the_style_and_clears_the_gate(
            self, db, draft_style):
        svc = StyleSpecService(db)
        await svc.replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        out = await svc.confirm(draft_style.id, no_accessories=False,
                                actor_name=BY, actor_id=ACTOR)
        assert out["confirmed"] is True
        assert out["confirmed_by"] == BY
        assert out["release_blockers"] == []
        assert "may now be released" in out["message"]

    async def test_a_style_with_no_leather_line_still_cannot_be_released(
            self, db, draft_style):
        """The dcm per piece is what the ledger and the costing are built on."""
        svc = StyleSpecService(db)
        await svc.replace_spec(draft_style.id, [button_line()], actor_name=BY)
        out = await svc.confirm(draft_style.id, no_accessories=False,
                                actor_name=BY)
        assert any("no LEATHER line" in b for b in out["release_blockers"])
        assert "still cannot be released" in out["message"]

    async def test_an_empty_accessory_list_needs_an_explicit_declaration(
            self, db, draft_style):
        """An empty list is ambiguous — a garment that takes none, or one whose
        buttons nobody has entered yet. Releasing the second silently is how a
        whole order reaches the store with no kit."""
        svc = StyleSpecService(db)
        await svc.replace_spec(draft_style.id, [leather_line()], actor_name=BY)
        without = await svc.confirm(draft_style.id, no_accessories=False,
                                    actor_name=BY)
        assert without["release_blockers"]
        with_declaration = await svc.confirm(draft_style.id, no_accessories=True,
                                             actor_name=BY)
        assert with_declaration["release_blockers"] == []

    async def test_declaring_no_accessories_while_they_exist_is_a_422(
            self, db, draft_style):
        """The two statements contradict each other; guessing which one the DM
        meant is how a kit gets skipped for a whole order."""
        svc = StyleSpecService(db)
        await svc.replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        with pytest.raises(HTTPException) as e:
            await svc.confirm(draft_style.id, no_accessories=True, actor_name=BY)
        assert e.value.status_code == 422
        assert "BTN-4H" in e.value.detail

    async def test_a_missing_lining_line_warns_but_does_not_block(
            self, db, draft_style):
        """Lining is optional on the cut path, so requiring it here would
        contradict the ledger downstream."""
        svc = StyleSpecService(db)
        await svc.replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        out = await svc.confirm(draft_style.id, no_accessories=False,
                                actor_name=BY)
        assert any("No LINING line" in w for w in out["warnings"])
        assert out["release_blockers"] == []

    async def test_a_style_with_a_lining_line_raises_no_warning(
            self, db, draft_style):
        svc = StyleSpecService(db)
        await svc.replace_spec(draft_style.id, [
            leather_line(),
            dict(category="LINING", subtype="PLAIN_LINING", article="POLY-1",
                 thickness="0.4mm", qty_per_piece=1.5),
        ], actor_name=BY)
        out = await svc.confirm(draft_style.id, no_accessories=True,
                                actor_name=BY)
        assert out["warnings"] == []

    async def test_confirmation_is_audited(self, db, draft_style):
        from app.core.models import AuditLog
        svc = StyleSpecService(db)
        await svc.replace_spec(draft_style.id, [leather_line()], actor_name=BY)
        await svc.confirm(draft_style.id, no_accessories=True, actor_name=BY,
                          actor_id=ACTOR)
        row = await db.scalar(select(AuditLog).where(
            AuditLog.action == "STYLE_MATERIAL_SPEC_CONFIRMED"))
        assert row.after["has_leather_line"] is True

    async def test_a_released_style_may_not_be_re_confirmed(self, db, order_tree):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).confirm(
                order_tree["style"].id, no_accessories=True, actor_name=BY)
        assert e.value.status_code == 409


# ══════════════════════════════════════════════════════════════ copy_from
class TestCopyFrom:
    @pytest.fixture
    async def source_style(self, db, order_tree):
        from app.modules.clients.models import SKU as SKUModel, Style
        style = Style(client_order_id=order_tree["order"].id, name="SOURCE",
                      article="SRC", production_status="DRAFT", code="SRC-1")
        db.add(style)
        await db.flush()
        db.add(SKUModel(style_id=style.id, color_code="PINE",
                        color_name="PINE GREEN", size="M", qty_ordered=3,
                        code="SRC-PINE-M"))
        await db.commit()
        await db.refresh(style)
        await StyleSpecService(db).replace_spec(
            style.id, [leather_line(), button_line()], actor_name=BY)
        return style

    async def test_a_recipe_is_seeded_from_another_style(
            self, db, draft_style, source_style):
        """A leather factory repeats styles season after season."""
        out = await StyleSpecService(db).copy_from(
            draft_style.id, source_style.id, actor_name=BY)
        assert out["copied"] == 2
        assert out["source_style"] == "SOURCE"
        assert "Review the quantities" in out["message"]

    async def test_the_copy_does_not_confirm_the_recipe(
            self, db, draft_style, source_style):
        """Somebody still has to look at the numbers for THIS style."""
        await StyleSpecService(db).copy_from(
            draft_style.id, source_style.id, actor_name=BY)
        assert (await StyleSpecService(db).get_spec(
            draft_style.id))["confirmed"] is False

    async def test_overrides_are_skipped_unless_asked_for(
            self, db, draft_style, source_style):
        sku = await db.scalar(select(SKU).where(SKU.style_id == source_style.id))
        await StyleSpecService(db).add_line(
            source_style.id, button_line(article="KNIT-PINE", subtype="OTHER",
                                         size=None, sku_id=sku.id),
            actor_name=BY)
        out = await StyleSpecService(db).copy_from(
            draft_style.id, source_style.id, actor_name=BY)
        assert out["copied"] == 2 and out["skipped"] == 0

    async def test_an_override_copies_where_the_colourways_match(
            self, db, draft_style, source_style):
        sku = await db.scalar(select(SKU).where(SKU.style_id == source_style.id))
        await StyleSpecService(db).add_line(
            source_style.id, button_line(article="KNIT-PINE", subtype="OTHER",
                                         size=None, sku_id=sku.id),
            actor_name=BY)
        out = await StyleSpecService(db).copy_from(
            draft_style.id, source_style.id, include_sku_overrides=True,
            actor_name=BY)
        assert out["copied"] == 3

    async def test_an_unmatched_override_is_reported_rather_than_guessed_at(
            self, db, draft_style, source_style):
        """Copying it onto the wrong colourway would silently issue the wrong
        colour button."""
        from app.modules.clients.models import SKU as SKUModel
        odd = SKUModel(style_id=source_style.id, color_code="NAVY",
                       color_name="NAVY", size="XXL", qty_ordered=1,
                       code="SRC-NAVY-XXL")
        db.add(odd)
        await db.commit()
        await StyleSpecService(db).add_line(
            source_style.id, button_line(article="ZIP-NAVY", subtype="ZIP",
                                         size=None, sku_id=odd.id),
            actor_name=BY)
        out = await StyleSpecService(db).copy_from(
            draft_style.id, source_style.id, include_sku_overrides=True,
            actor_name=BY)
        assert out["skipped"] == 1
        assert out["skipped_detail"][0]["reason"].endswith("for this override.")

    async def test_a_line_this_style_already_has_is_skipped(
            self, db, draft_style, source_style):
        await StyleSpecService(db).add_line(
            draft_style.id, button_line(), actor_name=BY)
        out = await StyleSpecService(db).copy_from(
            draft_style.id, source_style.id, actor_name=BY)
        assert out["copied"] == 1 and out["skipped"] == 1
        assert out["skipped_detail"][0]["reason"] == "Already on this style."

    async def test_an_unknown_source_style_is_a_404(self, db, draft_style):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).copy_from(
                draft_style.id, uuid.uuid4(), actor_name=BY)
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════ the requirement screen
class TestRequirement:
    async def test_it_multiplies_the_recipe_by_what_was_ordered(
            self, db, draft_style):
        await make_lot(db)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        out = await StyleSpecService(db).requirement(draft_style.id)
        assert out["qty_ordered"] == 5
        line = out["lines"][0]
        assert line["pieces"] == 5
        assert line["total_required"] == 20.0
        assert line["short_by"] == 0.0
        assert out["short_lines"] == 0
        assert "Stock covers all" in out["message"]

    async def test_a_shortfall_is_measured_and_a_supplier_suggested(
            self, db, draft_style):
        from app.modules.barcode.models import MaterialSupplier
        db.add(MaterialSupplier(name="TRIMS LTD", articles="BTN-4H",
                                is_active=True))
        await make_lot(db, on_hand=10)
        await db.commit()
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        out = await StyleSpecService(db).requirement(draft_style.id)
        assert out["lines"][0]["short_by"] == 10.0
        assert out["lines"][0]["suggested_supplier"]["name"] == "TRIMS LTD"
        assert out["short_lines"] == 1
        assert "Raise a supplier order" in out["message"]

    async def test_a_line_with_no_lot_at_all_is_short_by_the_whole_amount(
            self, db, draft_style):
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        out = await StyleSpecService(db).requirement(draft_style.id)
        assert out["lines"][0]["short_by"] == 20.0
        assert out["lines"][0]["suggested_supplier"] is None

    async def test_an_override_is_counted_against_its_own_sku_only(
            self, db, draft_style, order_tree):
        """The override's SKU is subtracted from the style line, so the same
        garment is never counted against two lines for one material."""
        from app.modules.clients.models import SKU as SKUModel
        second = SKUModel(style_id=draft_style.id, color_code="NAVY",
                          color_name="NAVY", size="M", qty_ordered=3,
                          code="JP-CLERMONT-NAVY-M")
        db.add(second)
        await db.commit()
        await StyleSpecService(db).replace_spec(
            draft_style.id,
            [button_line(),
             button_line(sku_id=order_tree["sku"].id, qty_per_piece=6)],
            actor_name=BY)
        out = await StyleSpecService(db).requirement(draft_style.id)
        by_scope = {l["scope"]: l for l in out["lines"]}
        assert by_scope["SKU"]["pieces"] == 5
        assert by_scope["STYLE"]["pieces"] == 3       # 8 ordered − the 5 overridden

    async def test_a_sized_line_reaches_only_garments_of_that_size(
            self, db, draft_style):
        """A style with three sized zip lines ordered THREE zips per garment
        instead of one — and the requirement is what a purchase is raised
        from, so the error would have been bought."""
        from app.modules.clients.models import SKU as SKUModel
        db.add(SKUModel(style_id=draft_style.id, color_code="PINE",
                        color_name="PINE GREEN", size="L", qty_ordered=2,
                        code="JP-CLERMONT-PINE-L"))
        await db.commit()
        await StyleSpecService(db).replace_spec(draft_style.id, [
            button_line(subtype="ZIP", article="ZIP-1", size="M"),
            button_line(subtype="ZIP", article="ZIP-1", size="L"),
        ], actor_name=BY)
        out = await StyleSpecService(db).requirement(draft_style.id)
        by_size = {l["garment_size"]: l["pieces"] for l in out["lines"]}
        assert by_size == {"M": 5, "L": 2}

    async def test_a_style_with_no_recipe_reports_an_empty_grid(
            self, db, draft_style):
        out = await StyleSpecService(db).requirement(draft_style.id)
        assert out["lines"] == [] and out["short_lines"] == 0

    async def test_an_unknown_style_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).requirement(uuid.uuid4())
        assert e.value.status_code == 404


# ══════════════════════════════════════════════ the cross-module surfaces
class TestBlockersForStyles:
    async def test_the_gate_is_answered_for_a_whole_batch(self, db, draft_style):
        svc = StyleSpecService(db)
        await svc.replace_spec(draft_style.id, [leather_line()], actor_name=BY)
        await svc.confirm(draft_style.id, no_accessories=True, actor_name=BY)
        out = await svc.blockers_for_styles([draft_style.id])
        assert out[draft_style.id] == []

    async def test_an_unconfirmed_style_reports_a_sentence_the_dm_can_read(
            self, db, draft_style):
        out = await StyleSpecService(db).blockers_for_styles([draft_style.id])
        assert any("has not been confirmed" in b for b in out[draft_style.id])

    async def test_an_empty_batch_costs_no_query(self, db):
        assert await StyleSpecService(db).blockers_for_styles([]) == {}


class TestKitSurfaces:
    @pytest.fixture
    async def kitted(self, db, draft_style, pieces):
        await make_lot(db)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        return pieces

    async def test_kit_required_is_true_only_when_accessories_reach_the_garment(
            self, db, kitted, draft_style, pieces):
        svc = StyleSpecService(db)
        assert await svc.kit_required_for_piece(pieces[0].id) is True
        await svc.replace_spec(draft_style.id, [leather_line()], actor_name=BY)
        assert await svc.kit_required_for_piece(pieces[0].id) is False

    async def test_a_piece_with_no_parent_style_needs_no_kit(self, db, pieces):
        piece = await orphan_piece(db, pieces[0])
        assert await StyleSpecService(db).kit_required_for_piece(piece.id) is False

    async def test_the_batched_kit_read_reports_three_fields_per_piece(
            self, db, kitted, pieces):
        """/production/log can carry 40 pieces; the full requirement block per
        piece would dwarf the response the scan screen actually reads."""
        out = await StyleSpecService(db).kit_by_pieces(
            [p.id for p in pieces])
        row = out[pieces[0].code]
        assert row["kit_required"] is True
        assert row["kit_status"] == KitStatus.PENDING.value
        assert row["outstanding"] == 4.0

    async def test_an_empty_batch_costs_no_query(self, db):
        assert await StyleSpecService(db).kit_by_pieces([]) == {}


class TestMaterialRequirementBlock:
    @pytest.fixture
    async def specced(self, db, draft_style):
        await make_lot(db)
        await make_lot(db, category="LEATHER", subtype=None, article="SUEDE-A32",
                       colour="PINE", thickness="1.2mm", size=None, uom="dcm",
                       on_hand=400)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        return draft_style

    async def test_a_garment_reports_what_it_needs_and_what_it_was_given(
            self, db, specced, pieces):
        out = await StyleSpecService(db).material_requirement_block(
            pieces[0].id)
        assert out["kit_required"] is True
        assert out["kit_status"] == KitStatus.PENDING.value
        assert out["leather"]["article"] == "SUEDE-A32"
        assert out["accessories"][0]["outstanding"] == 4.0
        assert out["accessories"][0]["issued_qty"] == 0.0
        assert out["summary_line"] == "4 pcs BTN-4H BLACK 18L"

    async def test_the_leather_block_carries_what_the_cut_actually_consumed(
            self, db, specced, pieces, order_tree, operations):
        """Beside what the recipe SAID it should. The two differing is normal
        and is the first thing a costing question asks about."""
        piece = pieces[0]
        db.add(ProductionEvent(
            sku_id=order_tree["sku"].id, piece_id=piece.id,
            operation_id=operations["LEATHER_CUTTING"].id,
            work_date=date.today(), consumption_qty=Decimal("13.75")))
        await db.commit()
        out = await StyleSpecService(db).material_requirement_block(piece.id)
        assert out["leather"]["qty_per_piece"] == 12.5
        assert out["leather"]["consumed"] == 13.75

    async def test_a_line_short_of_stock_is_flagged(self, db, draft_style, pieces):
        await make_lot(db, on_hand=1)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        out = await StyleSpecService(db).material_requirement_block(
            pieces[0].id)
        assert out["accessories"][0]["short"] is True

    async def test_an_unresolvable_line_holds_the_kit_at_partial(
            self, db, draft_style, pieces):
        """Reporting ISSUED there would be a lie that lets an unkitted garment
        onto the line."""
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        out = await StyleSpecService(db).material_requirement_block(
            pieces[0].id)
        assert out["accessories"][0]["resolution"] == RESOLUTION_NONE
        assert out["kit_status"] == KitStatus.PARTIAL.value

    async def test_a_null_piece_returns_the_not_required_shape(self, db):
        """So the drawer payload for an empty drawer still has a block to
        render, rather than a null."""
        out = await StyleSpecService(db).material_requirement_block(None)
        assert out["kit_status"] == KitStatus.NOT_REQUIRED.value
        assert out["accessories"] == []

    async def test_an_unknown_piece_returns_the_same_shape_not_a_404(self, db):
        out = await StyleSpecService(db).material_requirement_block(uuid.uuid4())
        assert out["kit_required"] is False

    async def test_a_piece_whose_style_is_gone_returns_the_same_shape(
            self, db, pieces):
        piece = await orphan_piece(db, pieces[0])
        out = await StyleSpecService(db).material_requirement_block(piece.id)
        assert out["kit_status"] == KitStatus.NOT_REQUIRED.value
        assert out["leather"] is None and out["lining"] is None

    async def test_the_lining_line_is_reported_beside_the_leather_one(
            self, db, draft_style, pieces):
        """ONE READ SURFACE OVER TWO WRITE PATHS: leather and lining both live
        on ProductionEvent, accessories in the issue ledger, and a screen must
        not have to know that."""
        await make_lot(db, category="LINING", subtype="PLAIN_LINING",
                       article="POLY-1", colour="BLACK", thickness="0.4mm",
                       size=None, uom="mtrs", on_hand=80)
        await StyleSpecService(db).replace_spec(draft_style.id, [
            leather_line(),
            dict(category="LINING", subtype="PLAIN_LINING", article="POLY-1",
                 colour="BLACK", thickness="0.4mm", qty_per_piece=1.5),
        ], actor_name=BY)
        out = await StyleSpecService(db).material_requirement_block(
            pieces[0].id)
        assert out["lining"]["article"] == "POLY-1"
        assert out["lining"]["qty_per_piece"] == 1.5
        assert out["lining"]["uom"] == "mtrs"
        assert out["lining"]["available"] == 80.0

    async def test_a_style_with_no_recipe_reports_whether_it_was_confirmed(
            self, db, draft_style, pieces):
        draft_style.material_spec_confirmed_at = None
        await db.commit()
        out = await StyleSpecService(db).material_requirement_block(
            pieces[0].id)
        assert out["kit_required"] is False and out["spec_confirmed"] is False


class TestKitView:
    async def test_the_checklist_reads_without_issuing_anything(
            self, db, draft_style, pieces):
        await make_lot(db)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        out = await StyleSpecService(db).kit_view(pieces[0].id)
        assert out["issued_now"] == [] and out["stock_warnings"] == []
        assert len(out["outstanding"]) == 1
        assert out["complete"] is False

    async def test_a_style_that_declares_no_accessories_is_complete(
            self, db, pieces):
        """This used to require kit_required, so every garment of every style
        released before the material spec came back complete: false on a kit it
        could never be given."""
        out = await StyleSpecService(db).kit_view(pieces[0].id)
        assert out["status"] == KitStatus.NOT_REQUIRED.value
        assert out["complete"] is True

    async def test_an_unresolvable_line_is_listed_separately(
            self, db, draft_style, pieces):
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        out = await StyleSpecService(db).kit_view(pieces[0].id)
        assert len(out["unresolved"]) == 1


# ══════════════════════════════════════════════════════ THE SPEND: the kit
class TestIssueKit:
    @pytest.fixture
    async def ready(self, db, draft_style, pieces):
        lot = await make_lot(db)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        return draft_style, pieces[0], lot

    async def test_the_kit_spends_stock_and_writes_the_ledger_row(
            self, db, ready, cutter):
        style, piece, lot = ready
        out = await StyleSpecService(db).issue_kit_nocommit(
            piece=piece, drawer=None, employee_id=cutter[0].id,
            entered_by="STORE")
        await db.commit()

        assert out["status"] == KitStatus.ISSUED.value
        assert out["complete"] is True
        assert out["issued_now"][0]["qty"] == 4.0
        assert out["issued_now"][0]["available_after"] == 496.0

        await db.refresh(lot)
        assert lot.on_hand == Decimal("496.000")
        row = await db.scalar(select(PieceMaterialIssue).where(
            PieceMaterialIssue.piece_id == piece.id))
        assert row.source == MaterialIssueSource.STORE_KIT.value
        assert row.issued_by_employee_id == cutter[0].id
        assert row.entered_by == "STORE"

    async def test_a_second_tap_of_the_gun_spends_nothing(self, db, ready):
        """IDEMPOTENCY IS A READ, not a constraint (CLAUDE.md §13 forbids ON
        CONFLICT): outstanding computes zero on every line."""
        style, piece, lot = ready
        svc = StyleSpecService(db)
        await svc.issue_kit_nocommit(piece=piece, drawer=None)
        await db.commit()
        out = await svc.issue_kit_nocommit(piece=piece, drawer=None)
        await db.commit()

        assert out["issued_now"] == []
        assert out["already_issued"][0]["issued"] == 4.0
        assert out["complete"] is True
        await db.refresh(lot)
        assert lot.on_hand == Decimal("496.000")

    async def test_a_partial_issue_tops_the_same_ledger_row_up(self, db, ready):
        """The unique constraint permits exactly one row per (piece, line), and
        that is what keeps the idempotency read a single lookup."""
        style, piece, lot = ready
        svc = StyleSpecService(db)
        lines = await svc.repo.lines_for_style(style.id)
        spec = next(l for l in lines if l.category == "ACCESSORY")

        first = await svc.issue_kit_nocommit(
            piece=piece, drawer=None,
            requested_lines=[{"spec_id": str(spec.id), "qty": 1}])
        await db.commit()
        assert first["issued_now"][0]["qty"] == 1.0
        assert first["outstanding"][0]["qty"] == 3.0
        assert first["complete"] is False

        second = await svc.issue_kit_nocommit(piece=piece, drawer=None)
        await db.commit()
        assert second["issued_now"][0]["qty"] == 3.0
        assert second["complete"] is True

        rows = (await db.execute(select(PieceMaterialIssue).where(
            PieceMaterialIssue.piece_id == piece.id))).scalars().all()
        assert len(rows) == 1 and rows[0].qty == Decimal("4.000")

    async def test_a_selective_issue_still_reports_the_lines_it_did_not_spend(
            self, db, draft_style, pieces):
        """So the screen shows a COMPLETE checklist, even when the operator is
        short of one article and is issuing the rest."""
        await make_lot(db)
        await make_lot(db, subtype="ZIP", article="ZIP-1", size="M")
        await StyleSpecService(db).replace_spec(draft_style.id, [
            button_line(),
            button_line(subtype="ZIP", article="ZIP-1", size="M",
                        qty_per_piece=1),
        ], actor_name=BY)
        svc = StyleSpecService(db)
        lines = await svc.repo.lines_for_style(draft_style.id)
        button = next(l for l in lines if l.article == "BTN-4H")

        out = await svc.issue_kit_nocommit(
            piece=pieces[0], drawer=None,
            requested_lines=[{"spec_id": str(button.id)}])
        await db.commit()
        assert [r["article"] for r in out["issued_now"]] == ["BTN-4H"]
        assert [r["article"] for r in out["outstanding"]] == ["ZIP-1"]
        assert out["complete"] is False

    async def test_a_substituted_lot_may_be_named_on_the_scan(self, db, ready):
        style, piece, _ = ready
        other = await make_lot(db, article="BTN-SUB", size="20L")
        svc = StyleSpecService(db)
        lines = await svc.repo.lines_for_style(style.id)
        spec = next(l for l in lines if l.category == "ACCESSORY")

        out = await svc.issue_kit_nocommit(
            piece=piece, drawer=None,
            requested_lines=[{"spec_id": str(spec.id),
                              "material_lot_id": other.id}])
        await db.commit()
        assert out["issued_now"][0]["lot_id"] == str(other.id)
        await db.refresh(other)
        assert other.on_hand == Decimal("496.000")

    async def test_a_named_lot_that_does_not_exist_comes_back_as_data(
            self, db, ready):
        style, piece, _ = ready
        svc = StyleSpecService(db)
        lines = await svc.repo.lines_for_style(style.id)
        spec = next(l for l in lines if l.category == "ACCESSORY")
        out = await svc.issue_kit_nocommit(
            piece=piece, drawer=None,
            requested_lines=[{"spec_id": str(spec.id),
                              "material_lot_id": uuid.uuid4()}])
        assert out["unresolved"][0]["reason"] == RESOLUTION_NONE

    async def test_an_unresolvable_line_never_aborts_the_lines_that_resolved(
            self, db, draft_style, pieces):
        """The decrement 404s on a missing lot, and raising that halfway through
        would abort a scan that had already issued three good lines."""
        await make_lot(db)
        await StyleSpecService(db).replace_spec(draft_style.id, [
            button_line(),
            button_line(subtype="ZIP", article="NOT-IN-STOCK", size=None,
                        qty_per_piece=1),
        ], actor_name=BY)
        out = await StyleSpecService(db).issue_kit_nocommit(
            piece=pieces[0], drawer=None)
        await db.commit()
        assert [r["article"] for r in out["issued_now"]] == ["BTN-4H"]
        assert out["unresolved"][0]["article"] == "NOT-IN-STOCK"
        assert "receive stock for it first" in out["unresolved"][0]["note"]
        assert out["status"] == KitStatus.PARTIAL.value
        assert out["complete"] is False

    async def test_an_ambiguous_line_asks_the_operator_to_pick(
            self, db, draft_style, pieces):
        await make_lot(db, thickness="A")
        await make_lot(db, thickness="B")
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line(thickness=None)], actor_name=BY)
        out = await StyleSpecService(db).issue_kit_nocommit(
            piece=pieces[0], drawer=None)
        assert out["unresolved"][0]["reason"] == RESOLUTION_AMBIGUOUS
        assert "pick" in out["unresolved"][0]["note"]

    async def test_issuing_more_than_is_on_hand_warns_but_records_the_issue(
            self, db, draft_style, pieces):
        """The buttons are physically in the operator's hand; refusing to record
        them to protect a number would lose the record."""
        lot = await make_lot(db, on_hand=1)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        out = await StyleSpecService(db).issue_kit_nocommit(
            piece=pieces[0], drawer=None)
        await db.commit()
        assert out["stock_warnings"][0]["short_by"] == 3.0
        assert out["status"] == KitStatus.ISSUED.value
        await db.refresh(lot)
        assert lot.on_hand == Decimal("-3.000")

    async def test_a_style_with_no_accessory_spec_refuses_loudly(
            self, db, draft_style, pieces):
        """An operator who scans a kit and gets a cheerful 200 will believe the
        accessories were issued. They were not."""
        await StyleSpecService(db).replace_spec(
            draft_style.id, [leather_line()], actor_name=BY)
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).issue_kit_nocommit(
                piece=pieces[0], drawer=None)
        assert e.value.status_code == 409
        assert "no accessory spec" in e.value.detail
        assert "material-spec" in e.value.detail

    async def test_lines_scoped_to_another_colourway_say_so_by_name(
            self, db, draft_style, pieces):
        """'No accessory spec' was ONE message covering two different
        situations, and it sent the DM to add lines that were already there."""
        from app.modules.clients.models import SKU as SKUModel
        other = SKUModel(style_id=draft_style.id, color_code="NAVY",
                         color_name="NAVY", size="M", qty_ordered=1,
                         code="JP-CLERMONT-NAVY-M")
        db.add(other)
        await db.commit()
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line(sku_id=other.id)], actor_name=BY)
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).issue_kit_nocommit(
                piece=pieces[0], drawer=None)
        assert "scoped to other colourways" in e.value.detail
        assert "NAVY" in e.value.detail

    async def test_lines_for_another_garment_size_say_so_by_name(
            self, db, draft_style, pieces):
        await StyleSpecService(db).replace_spec(
            draft_style.id,
            [button_line(subtype="ZIP", article="ZIP-1", size="L")],
            actor_name=BY)
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).issue_kit_nocommit(
                piece=pieces[0], drawer=None)
        assert "garment size(s) L" in e.value.detail
        assert "an L zip is not an S zip" in e.value.detail

    async def test_a_colourway_that_declares_it_takes_none_says_so(
            self, db, draft_style, pieces, order_tree):
        await StyleSpecService(db).replace_spec(
            draft_style.id,
            [button_line(sku_id=order_tree["sku"].id, qty_per_piece=0)],
            actor_name=BY)
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).issue_kit_nocommit(
                piece=pieces[0], drawer=None)
        assert "takes none of its" in e.value.detail
        assert "the spec working as written" in e.value.detail

    async def test_a_piece_with_no_parent_style_is_a_404(self, db, pieces):
        piece = await orphan_piece(db, pieces[0])
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).issue_kit_nocommit(
                piece=piece, drawer=None)
        assert e.value.status_code == 404
        assert "no parent style" in e.value.detail


# ══════════════════════════════════ THE SPEND: the off-spec correction
class TestIssueManual:
    async def test_a_correction_spends_stock_and_is_recorded_as_manual(
            self, db, pieces, cutter):
        """The escape hatch that makes freezing the recipe at release
        acceptable."""
        lot = await make_lot(db)
        out = await StyleSpecService(db).issue_manual(
            piece_id=pieces[0].id, material_lot_id=lot.id, qty=2,
            note="wrong button on the sheet", employee_id=cutter[0].id,
            entered_by="STORE MGR", actor_id=ACTOR)

        assert out["source"] == MaterialIssueSource.MANUAL.value
        assert out["qty"] == 2.0 and out["available_after"] == 498.0
        assert out["stock_warning"] is None
        assert "outside the spec" in out["message"]

        row = await db.scalar(select(PieceMaterialIssue).where(
            PieceMaterialIssue.piece_id == pieces[0].id))
        assert row.spec_line_id is None
        assert row.source == MaterialIssueSource.MANUAL.value

    async def test_it_is_repeatable_because_the_constraint_does_not_dedupe_nulls(
            self, db, pieces):
        """Three corrections on one garment are three real events."""
        lot = await make_lot(db)
        svc = StyleSpecService(db)
        for _ in range(3):
            await svc.issue_manual(piece_id=pieces[0].id,
                                   material_lot_id=lot.id, qty=1)
        rows = (await db.execute(select(PieceMaterialIssue).where(
            PieceMaterialIssue.piece_id == pieces[0].id))).scalars().all()
        assert len(rows) == 3

    async def test_a_correction_never_satisfies_a_spec_line(
            self, db, draft_style, pieces):
        """The kit's idempotency read ignores these rows."""
        lot = await make_lot(db)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        await StyleSpecService(db).issue_manual(
            piece_id=pieces[0].id, material_lot_id=lot.id, qty=4)
        out = await StyleSpecService(db).material_requirement_block(
            pieces[0].id)
        assert out["accessories"][0]["outstanding"] == 4.0
        assert out["kit_status"] == KitStatus.PENDING.value

    async def test_an_over_issue_warns_without_blocking(self, db, pieces):
        lot = await make_lot(db, on_hand=1)
        out = await StyleSpecService(db).issue_manual(
            piece_id=pieces[0].id, material_lot_id=lot.id, qty=5)
        assert out["stock_warning"]["short_by"] == 4.0

    async def test_the_correction_is_audited(self, db, pieces):
        from app.core.models import AuditLog
        lot = await make_lot(db)
        await StyleSpecService(db).issue_manual(
            piece_id=pieces[0].id, material_lot_id=lot.id, qty=1,
            note="swapped", actor_id=ACTOR)
        row = await db.scalar(select(AuditLog).where(
            AuditLog.action == "MATERIAL_ISSUED_MANUAL"))
        assert row.after["note"] == "swapped"

    @pytest.mark.parametrize("qty", [0, -1, None])
    async def test_a_non_positive_quantity_is_refused(self, db, pieces, qty):
        lot = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).issue_manual(
                piece_id=pieces[0].id, material_lot_id=lot.id, qty=qty)
        assert e.value.status_code == 422

    async def test_an_unknown_lot_is_a_404(self, db, pieces):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).issue_manual(
                piece_id=pieces[0].id, material_lot_id=uuid.uuid4(), qty=1)
        assert e.value.status_code == 404

    async def test_an_unknown_piece_is_a_404(self, db):
        lot = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).issue_manual(
                piece_id=uuid.uuid4(), material_lot_id=lot.id, qty=1)
        assert e.value.status_code == 404


# ══════════════════════════════ what is in this garment, and what is not
class TestPieceMaterials:
    async def test_it_returns_the_recipe_the_ledger_and_the_store_state(
            self, db, draft_style, pieces, cutter):
        await make_lot(db)
        await make_lot(db, category="LEATHER", subtype=None, article="SUEDE-A32",
                       colour="PINE", thickness="1.2mm", size=None, uom="dcm",
                       on_hand=400)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [leather_line(), button_line()], actor_name=BY)
        await StyleSpecService(db).issue_kit_nocommit(
            piece=pieces[0], drawer=None, employee_id=cutter[0].id)
        await db.commit()

        out = await StyleSpecService(db).piece_materials(pieces[0].id)
        assert out["piece_code"] == pieces[0].code
        assert out["sku_label"] == "PINE GREEN · M"
        assert out["garment_size"] == "M"
        assert out["applies"]["leather"]["article"] == "SUEDE-A32"
        assert out["applies"]["accessories"][0]["issued_qty"] == 4.0
        assert out["issued"][0]["article"] == "BTN-4H"
        assert out["issued"][0]["source"] == MaterialIssueSource.STORE_KIT.value
        assert out["store"]["state"] is not None

    async def test_the_style_lines_this_garment_is_not_on_carry_their_reason(
            self, db, draft_style, pieces, order_tree):
        """The DM saw three accessory lines on one screen, scanned the piece,
        and was told the style had no accessory spec. Both screens were telling
        the truth about different questions."""
        from app.modules.clients.models import SKU as SKUModel
        other = SKUModel(style_id=draft_style.id, color_code="NAVY",
                         color_name="NAVY", size="M", qty_ordered=1,
                         code="JP-CLERMONT-NAVY-M")
        db.add(other)
        await db.commit()
        await StyleSpecService(db).replace_spec(draft_style.id, [
            button_line(article="BTN-NAVY", sku_id=other.id),
            button_line(subtype="ZIP", article="ZIP-1", size="L"),
            button_line(article="BTN-ZERO", sku_id=order_tree["sku"].id,
                        qty_per_piece=0),
        ], actor_name=BY)

        out = await StyleSpecService(db).piece_materials(pieces[0].id)
        reasons = {r["article"]: r["reason"] for r in out["not_applicable"]}
        assert reasons == {"BTN-NAVY": "other_sku", "ZIP-1": "other_size",
                           "BTN-ZERO": "zeroed"}
        notes = {r["article"]: r["reason_note"] for r in out["not_applicable"]}
        assert "NAVY" in notes["BTN-NAVY"]
        assert "garment size L" in notes["ZIP-1"]
        assert "does not take it" in notes["BTN-ZERO"]

    async def test_a_manual_correction_appears_in_the_ledger_half(
            self, db, pieces):
        """Deliberately ignored by the checklist, but the garment really got
        it."""
        lot = await make_lot(db)
        await StyleSpecService(db).issue_manual(
            piece_id=pieces[0].id, material_lot_id=lot.id, qty=2)
        out = await StyleSpecService(db).piece_materials(pieces[0].id)
        assert out["issued"][0]["source"] == MaterialIssueSource.MANUAL.value
        assert out["issued"][0]["spec_line_id"] is None

    async def test_a_piece_with_no_style_returns_the_empty_shape(self, db, pieces):
        piece = await orphan_piece(db, pieces[0])
        out = await StyleSpecService(db).piece_materials(piece.id)
        assert out["kit_required"] is False
        assert out["applies"]["accessories"] == []
        assert out["style_id"] is None

    async def test_an_unknown_piece_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await StyleSpecService(db).piece_materials(uuid.uuid4())
        assert e.value.status_code == 404

    async def test_the_sku_label_falls_back_when_there_is_nothing_to_name(self, db):
        svc = StyleSpecService(db)
        assert await svc._sku_label(None) == "style-wide"
        missing = uuid.uuid4()
        assert await svc._sku_label(missing) == str(missing)


# ══════════════════════════════════════════════════════════════ repository
class TestSpecRepositoryEdges:
    async def test_the_batched_reads_short_circuit_on_an_empty_list(self, db):
        repo = StyleSpecService(db).repo
        assert await repo.lines_for_styles([]) == {}
        assert await repo.has_accessory_lines([]) == {}
        assert await repo.issued_by_pieces([]) == {}

    async def test_has_accessory_lines_answers_false_for_a_style_with_none(
            self, db, draft_style):
        repo = StyleSpecService(db).repo
        await StyleSpecService(db).replace_spec(
            draft_style.id, [leather_line()], actor_name=BY)
        assert await repo.has_accessory_lines([draft_style.id]) \
            == {draft_style.id: False}

    async def test_has_accessory_lines_answers_true_once_one_exists(
            self, db, draft_style):
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        assert (await StyleSpecService(db).repo.has_accessory_lines(
            [draft_style.id]))[draft_style.id] is True

    async def test_deactivated_lines_are_hidden_unless_asked_for(
            self, db, draft_style):
        svc = StyleSpecService(db)
        await svc.replace_spec(draft_style.id, [button_line()], actor_name=BY)
        await svc.replace_spec(draft_style.id, [], actor_name=BY)
        assert await svc.repo.lines_for_style(draft_style.id) == []
        assert len(await svc.repo.lines_for_style(
            draft_style.id, active_only=False)) == 1

    async def test_the_idempotency_read_excludes_manual_corrections(
            self, db, draft_style, pieces):
        lot = await make_lot(db)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        await StyleSpecService(db).issue_manual(
            piece_id=pieces[0].id, material_lot_id=lot.id, qty=4)
        issued = await StyleSpecService(db).repo.issued_by_piece(pieces[0].id)
        assert issued == {}

    async def test_the_idempotency_read_can_be_narrowed_to_one_source(
            self, db, draft_style, pieces):
        """`source` exists so a caller can ask "how much of this came from the
        STORE KIT", separately from the whole ledger."""
        await make_lot(db)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        await StyleSpecService(db).issue_kit_nocommit(
            piece=pieces[0], drawer=None)
        await db.commit()
        repo = StyleSpecService(db).repo
        kit = await repo.issued_by_piece(
            pieces[0].id, source=MaterialIssueSource.STORE_KIT.value)
        assert sum(kit.values()) == Decimal("4.000")
        assert await repo.issued_by_piece(
            pieces[0].id, source=MaterialIssueSource.MANUAL.value) == {}

    async def test_the_batched_issue_read_answers_for_many_pieces_at_once(
            self, db, draft_style, pieces):
        """/production/log resolves a kit status for every piece in the batch;
        one query per piece would be 40 round trips on a single scan."""
        await make_lot(db)
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        svc = StyleSpecService(db)
        for piece in pieces[:2]:
            await svc.issue_kit_nocommit(piece=piece, drawer=None)
        await db.commit()

        out = await svc.repo.issued_by_pieces([p.id for p in pieces])
        assert len(out) == 2
        assert all(sum(v.values()) == Decimal("4.000") for v in out.values())

    async def test_the_ledger_read_includes_them(self, db, pieces):
        lot = await make_lot(db)
        await StyleSpecService(db).issue_manual(
            piece_id=pieces[0].id, material_lot_id=lot.id, qty=4)
        rows = await StyleSpecService(db).repo.issue_rows_for_piece(
            pieces[0].id)
        assert len(rows) == 1

    async def test_issues_for_piece_returns_the_rows_oldest_first(
            self, db, pieces):
        lot = await make_lot(db)
        svc = StyleSpecService(db)
        for _ in range(2):
            await svc.issue_manual(piece_id=pieces[0].id,
                                   material_lot_id=lot.id, qty=1)
        rows = await svc.repo.issues_for_piece(pieces[0].id)
        assert len(rows) == 2
        assert rows[0].issued_at <= rows[1].issued_at

    async def test_the_correlated_exists_matches_the_python_resolver(
            self, db, draft_style):
        """The two-shape pattern: a Python resolver for one object, a SQL
        expression for a page of them, side by side so they cannot disagree."""
        from app.modules.clients.models import Style
        from app.modules.materials.style_spec_repository import StyleSpecRepository
        await StyleSpecService(db).replace_spec(
            draft_style.id, [button_line()], actor_name=BY)
        found = await db.scalar(
            select(Style.id).where(
                Style.id == draft_style.id,
                StyleSpecRepository.kit_required_sql(Style.id)))
        assert found == draft_style.id
