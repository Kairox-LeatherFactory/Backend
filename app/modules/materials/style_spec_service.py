"""
================================================================================
modules/materials/style_spec_service.py — the per-piece recipe, and spending it
================================================================================
WHAT THIS MODULE IS FOR
    Before it, the only material the factory could spend automatically was
    leather, and its quantity was typed by the cutting manager on every scan.
    Accessories could be stocked, barcoded and received but never decremented,
    so accessory stock only ever went up and the floor built each kit from
    memory. This module holds the recipe (what one garment of a style takes),
    the gate that makes a style declare it before release, and the primitive
    that spends it when the store kits a drawer.

THE FOUR SURFACES IT SERVES, and why they all come through here
    imports/breakdown   release gate      — blockers_for_styles
    drawers/service     the kit scan      — issue_kit_nocommit
    barcode/service     piece+drawer scan — material_requirement_block
    production/service  the log response  — kit_by_pieces
    Each is a lazy import at its call site (CLAUDE.md §15), so the module graph
    stays acyclic and none of them pays for this import unless it asks.

WHAT IT DOES NOT OWN
    Leather and lining consumption. That lives on ProductionEvent because the
    lot link belongs to the act of cutting, not to the piece (CLAUDE.md §8).
    Two write paths, ONE read surface: material_requirement_block merges them so
    a screen never has to know there were two.

    Reservations. `requirement` REPORTS the shortfall; it does not reserve
    against it. Nothing in this codebase can release a MaterialReservation, and
    adjust_lot/retire_lot hard-block on outstanding ones — creating reservations
    here would wedge both operations permanently.
================================================================================
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import kit_rules
from app.core.enums import (
    BarcodeAuditAction, KitStatus, MaterialCategory, MaterialIssueSource,
    resolve_spec, uom_for,
)
from app.modules.barcode.models import StyleMaterialSpec
from app.modules.clients.models import SKU, Style, spec_editable
from app.modules.materials.repository import MaterialRepository
from app.modules.materials.style_spec_repository import StyleSpecRepository

# How a spec line resolved to a physical lot. The screen renders each
# differently: PINNED and MATCHED are ready to issue, NONE needs stock receiving,
# AMBIGUOUS needs a human to pick.
RESOLUTION_PINNED = "PINNED"        # the line names an exact lot id
RESOLUTION_MATCHED = "MATCHED"      # exactly one lot matches the six-column key
RESOLUTION_NONE = "NONE"            # no lot matches — receive stock first
RESOLUTION_AMBIGUOUS = "AMBIGUOUS"  # several match — the key is not specific enough


class StyleSpecService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = StyleSpecRepository(db)
        self.materials = MaterialRepository(db)

    # ══════════════════════════════════════════════════════════ small helpers
    async def _style(self, style_id: uuid.UUID) -> Style:
        style = await self.db.get(Style, style_id)
        if style is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Style not found.")
        return style

    @staticmethod
    def _assert_editable(style: Style) -> None:
        """The recipe freezes when the style is released. See spec_editable()."""
        if spec_editable(style):
            return
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{style.name} is {style.production_status} — its material spec is "
            f"frozen because its pieces carry printed barcodes and the spec is "
            f"already being spent against them. To correct what a garment was "
            f"actually issued, record it on the floor with POST "
            f"/materials/issues instead of editing the recipe underneath it.")

    async def _audit(self, actor_id, action: str, entity_id, after: dict) -> None:
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_id, action=action, entity_type="style",
            entity_id=entity_id, after=after, at=datetime.now(timezone.utc)))

    # ══════════════════════════════════════════════════════ line validation
    async def _clean_line(self, style: Style, body: dict) -> dict:
        """Validate one incoming recipe line and normalise it for storage.

        THE SAME STRICTNESS AS create_lot, ON PURPOSE. A recipe line and a stock
        lot are matched on the same six columns, so a line the lot form would
        have rejected can never resolve to anything. resolve_spec() is the one
        table that says which (category, subtype) pairs exist and what each one
        must carry, and both paths ask it.
        """
        category = (body.get("category") or "").strip().upper()
        subtype = (body.get("subtype") or "").strip().upper() or None
        spec = resolve_spec(category, subtype)
        if spec is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"'{category}"
                f"{'/' + subtype if subtype else ''}' is not a material kind this "
                f"factory stocks. Use LEATHER, LINING (PLAIN_LINING / RIBS / "
                f"KNIT) or ACCESSORY (BUTTON / ZIP / THREAD / OTHER) — the same "
                f"list GET /materials/spec returns.")

        article = (body.get("article") or "").strip()
        if not article:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Every recipe line must name an article — a line that does not "
                "say WHAT to issue can never resolve to a lot, so it could never "
                "be spent.")

        sku_id = body.get("sku_id")
        if sku_id is not None:
            sku = await self.db.get(SKU, sku_id)
            if sku is None or sku.style_id != style.id:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"SKU {sku_id} does not belong to {style.name}. A per-SKU "
                    f"override may only name a colour/size of its own style.")

        try:
            qty = Decimal(str(body.get("qty_per_piece")))
        except Exception as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "qty_per_piece must be numeric.") from exc
        # ZERO IS LEGAL ON AN OVERRIDE ONLY, and it is how a colourway says "this
        # one does not take that". On a style-wide line it would be a recipe
        # entry that consumes nothing, which is just a typo with a row in it.
        if qty < 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "qty_per_piece cannot be negative.")
        if qty == 0 and sku_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A style-wide line must consume something. Use qty_per_piece: 0 "
                "on a per-SKU override to say that one colourway does NOT take "
                "this material, or delete the line.")

        return {
            "style_id": style.id,
            "sku_id": sku_id,
            "category": category,
            "subtype": subtype,
            "article": article,
            "colour": (body.get("colour") or "").strip() or None,
            "thickness": (body.get("thickness") or "").strip() or None,
            "size": (body.get("size") or "").strip() or None,
            "qty_per_piece": qty,
            # DERIVED, AND A SENT VALUE IS DISCARDED. uom is a property of the
            # material kind, not a choice: buttons are pcs and thread is mtrs
            # whatever a client posts. Honouring a caller's unit would let one
            # style measure thread in yards and another in metres while both told
            # the ledger "mtrs" — and the ledger is what the decrement moves.
            # The field stays in the contract only so a GET can be round-tripped.
            "uom": uom_for(category, subtype),
            "material_lot_id": body.get("material_lot_id"),
            "note": (body.get("note") or "").strip() or None,
        }

    # ══════════════════════════════════════════════════ lot resolution
    async def _resolve_lot(self, line) -> tuple:
        """(lot | None, resolution, candidate_ids) for one recipe line.

        RESOLVE-BEFORE-SPEND IS THE WHOLE POINT OF RETURNING DATA HERE. The
        decrement 404s on a missing lot, and a 404 raised halfway through a kit
        would abort a scan that had already legitimately issued three other
        lines. So a line that cannot be resolved comes back as a VALUE the caller
        reports in `unresolved[]`, never as an exception.

        A PINNED lot that has been retired falls back to a key match rather than
        failing: the pin is an optimisation, not a promise, and a retired lot
        usually means the store re-received the same article under a new one.
        """
        if line.material_lot_id:
            lot = await self.materials.get_lot(line.material_lot_id)
            if lot is not None and lot.is_active:
                return lot, RESOLUTION_PINNED, []

        lots = await self.materials.find_lots(
            category=line.category, subtype=line.subtype, article=line.article,
            colour=line.colour, thickness=line.thickness, size=line.size)
        if not lots:
            return None, RESOLUTION_NONE, []
        if len(lots) > 1:
            # Same verdict the cut-lot picker reaches, and for the same reason —
            # but as data, so one ambiguous button does not lose the whole kit.
            return None, RESOLUTION_AMBIGUOUS, [str(l.id) for l in lots[:5]]
        return lots[0], RESOLUTION_MATCHED, []

    # ══════════════════════════════════════════ the style/SKU merge
    @staticmethod
    def merge_lines(lines: list, sku_id: uuid.UUID | None) -> list:
        """The effective recipe for ONE colourway: SKU overrides beat style defaults.

        THE OVERRIDE IS LINE-LEVEL, keyed on (category, subtype, article). The
        real case is "the TAN colourway takes TAN buttons of the same article", so
        a SKU line REPLACES the style line for that article and leaves the rest of
        the recipe alone. Set-level replacement was the alternative and it would
        force re-entering the whole recipe for every colour that differs by one
        button.

        A SKU line naming an article the style does not have is an ADDITION.
        A SKU line with qty_per_piece = 0 REMOVES that material for that
        colourway — which is why the zero is dropped here rather than earlier:
        it has to survive long enough to shadow the style line.

        PURE and static so the unit layer can exercise every case with plain
        objects and no database.
        """
        style_lines = [l for l in lines if l.sku_id is None]
        sku_lines = [l for l in lines if sku_id is not None and l.sku_id == sku_id]

        def key(l):
            return ((l.category or "").upper(), (l.subtype or "") or None,
                    (l.article or ""))

        merged = {key(l): l for l in style_lines}
        for l in sku_lines:
            merged[key(l)] = l          # override or addition
        return [l for l in merged.values() if (l.qty_per_piece or 0) > 0]

    async def effective_lines(self, style_id: uuid.UUID,
                              sku_id: uuid.UUID | None) -> list:
        """The recipe that actually applies to one SKU of one style."""
        return self.merge_lines(await self.repo.lines_for_style(style_id), sku_id)

    async def _piece_context(self, piece_id: uuid.UUID) -> tuple:
        """(piece, sku, style) for a piece id — the join every read here needs."""
        from app.modules.production.models import Piece
        piece = await self.db.get(Piece, piece_id)
        if piece is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Piece not found.")
        sku = await self.db.get(SKU, piece.sku_id)
        style = await self.db.get(Style, sku.style_id) if sku else None
        return piece, sku, style

    # ══════════════════════════════════════════════════════ authoring: read
    async def _line_payload(self, line, *, with_lot: bool = True) -> dict:
        out = {
            "line_id": str(line.id),
            "scope": "SKU" if line.sku_id else "STYLE",
            "sku_id": str(line.sku_id) if line.sku_id else None,
            "category": line.category, "subtype": line.subtype,
            "article": line.article, "colour": line.colour,
            "thickness": line.thickness, "size": line.size,
            "qty_per_piece": float(line.qty_per_piece or 0),
            "uom": line.uom,
            "material_lot_id": str(line.material_lot_id) if line.material_lot_id else None,
            "note": line.note, "is_active": bool(line.is_active),
            "resolution": None, "lot": None, "candidate_lot_ids": [],
        }
        if not with_lot:
            return out
        lot, resolution, candidates = await self._resolve_lot(line)
        out["resolution"] = resolution
        out["candidate_lot_ids"] = candidates
        if lot is not None:
            reserved = await self.materials.active_reserved(lot.id)
            on_hand = Decimal(str(lot.on_hand or 0))
            out["lot"] = {
                "lot_id": str(lot.id), "article": lot.article,
                "colour": lot.colour, "uom": lot.uom,
                "on_hand": float(on_hand),
                # RESERVED IS SHOWN, NOT SILENTLY SUBTRACTED. Nothing in this
                # codebase can release a reservation, so a stuck one would
                # otherwise present as a phantom shortfall with no visible cause.
                "reserved": float(reserved),
                "available": float(on_hand - reserved),
            }
        return out

    async def get_spec(self, style_id: uuid.UUID) -> dict:
        """The authoring grid: every line, how it resolves, and what blocks release."""
        style = await self._style(style_id)
        lines = await self.repo.lines_for_style(style_id)
        payload = [await self._line_payload(l) for l in lines]
        blockers = kit_rules.release_blockers(
            style_name=style.name,
            confirmed_at=style.material_spec_confirmed_at,
            no_accessories=style.material_spec_no_accessories,
            has_accessory_lines=any(l.category == MaterialCategory.ACCESSORY.value
                                    for l in lines),
            has_leather_line=any(l.category == MaterialCategory.LEATHER.value
                                 and (l.qty_per_piece or 0) > 0 for l in lines))
        return {
            "style_id": str(style.id), "style_code": style.code,
            "style_name": style.name,
            "production_status": style.production_status,
            "editable": spec_editable(style),
            "confirmed": style.material_spec_confirmed_at is not None,
            "confirmed_at": (style.material_spec_confirmed_at.isoformat()
                             if style.material_spec_confirmed_at else None),
            "confirmed_by": style.material_spec_confirmed_by,
            "no_accessories_declared": style.material_spec_no_accessories,
            "release_blockers": blockers,
            "sku_overrides_count": sum(1 for l in lines if l.sku_id),
            "lines": payload,
        }

    # ══════════════════════════════════════════════════════ authoring: write
    async def replace_spec(self, style_id: uuid.UUID, lines: list, *,
                           actor_name: str, actor_id=None) -> dict:
        """Save the whole grid in one call. IDEMPOTENT on the identity key.

        WHY A DIFF AND NOT delete-then-insert. A line that has already been ISSUED
        against is referenced by piece_material_issue rows; recreating it would
        give the same recipe entry a new id and orphan every issue that pointed at
        the old one — which is exactly the history the ledger exists to keep. So
        matching lines are UPDATED in place, absent ones are DEACTIVATED (never
        deleted, same reason), and only genuinely new identities are inserted.

        Saving the same body twice therefore returns the same line ids and writes
        nothing the second time.
        """
        style = await self._style(style_id)
        self._assert_editable(style)

        cleaned = [await self._clean_line(style, dict(row)) for row in lines]

        def identity(d):
            return (d["sku_id"], d["category"], d["subtype"], d["article"],
                    d["colour"], d["thickness"], d["size"])

        seen: dict = {}
        for d in cleaned:
            k = identity(d)
            if k in seen:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"The same material appears twice in this spec: "
                    f"{d['article']}"
                    f"{' · ' + d['colour'] if d['colour'] else ''}. Combine them "
                    f"into one line with the total per-piece quantity.")
            seen[k] = d

        existing = await self.repo.lines_for_style(style_id, active_only=False)
        by_identity = {
            (l.sku_id, l.category, l.subtype, l.article, l.colour, l.thickness,
             l.size): l for l in existing}

        kept, added = [], []
        for k, d in seen.items():
            row = by_identity.get(k)
            if row is None:
                row = self.repo.add_line_nocommit(**d)
                added.append(row)
            else:
                row.qty_per_piece = d["qty_per_piece"]
                row.uom = d["uom"]
                row.material_lot_id = d["material_lot_id"]
                row.note = d["note"]
                row.is_active = True
                kept.append(row)
        removed = 0
        for k, row in by_identity.items():
            if k not in seen and row.is_active:
                row.is_active = False       # soft: the ledger still points here
                removed += 1

        await self._audit(actor_id, BarcodeAuditAction.STYLE_MATERIAL_SPEC_AMENDED.value,
                          style.id, {"style_code": style.code, "by": actor_name,
                                     "lines": len(seen), "added": len(added),
                                     "updated": len(kept), "deactivated": removed})
        await self.db.commit()
        out = await self.get_spec(style_id)
        out["message"] = (f"Saved {len(seen)} line(s) for {style.name}"
                          f"{f'; {removed} removed' if removed else ''}.")
        return out

    async def add_line(self, style_id: uuid.UUID, body: dict, *,
                       actor_name: str, actor_id=None) -> dict:
        style = await self._style(style_id)
        self._assert_editable(style)
        d = await self._clean_line(style, body)
        dup = await self.repo.find_duplicate_line(
            style_id=style.id, sku_id=d["sku_id"], category=d["category"],
            subtype=d["subtype"], article=d["article"], colour=d["colour"],
            thickness=d["thickness"], size=d["size"])
        if dup is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{style.name} already has a line for {d['article']}"
                f"{' · ' + d['colour'] if d['colour'] else ''}. Edit that line "
                f"(PATCH .../lines/{dup.id}) rather than adding a second one — "
                f"two lines for one material would both be issued.")
        line = self.repo.add_line_nocommit(**d)
        await self.db.commit()
        await self.db.refresh(line)
        return await self._line_payload(line)

    async def patch_line(self, style_id: uuid.UUID, line_id: uuid.UUID,
                         patch: dict, *, actor_name: str, actor_id=None) -> dict:
        style = await self._style(style_id)
        self._assert_editable(style)
        line = await self.repo.get_line(line_id)
        if line is None or line.style_id != style.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "No such line on this style.")
        merged = {
            "sku_id": line.sku_id, "category": line.category,
            "subtype": line.subtype, "article": line.article,
            "colour": line.colour, "thickness": line.thickness,
            "size": line.size, "qty_per_piece": line.qty_per_piece,
            "uom": line.uom, "material_lot_id": line.material_lot_id,
            "note": line.note,
        }
        merged.update({k: v for k, v in patch.items() if v is not None})
        d = await self._clean_line(style, merged)
        for field, value in d.items():
            if field != "style_id":
                setattr(line, field, value)
        await self.db.commit()
        await self.db.refresh(line)
        return await self._line_payload(line)

    async def deactivate_line(self, style_id: uuid.UUID, line_id: uuid.UUID, *,
                              actor_name: str, actor_id=None) -> dict:
        """SOFT delete. The ledger points at this row and must keep resolving."""
        style = await self._style(style_id)
        self._assert_editable(style)
        line = await self.repo.get_line(line_id)
        if line is None or line.style_id != style.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "No such line on this style.")
        line.is_active = False
        await self.db.commit()
        return {"deactivated": True, "line_id": str(line_id),
                "article": line.article,
                "message": f"{line.article} removed from {style.name}'s recipe."}

    async def confirm(self, style_id: uuid.UUID, *, no_accessories: bool,
                      actor_name: str, actor_id=None) -> dict:
        """The human signing the recipe off. This is what unlocks release.

        SEPARATE FROM RELEASE ON PURPOSE. Confirming here rather than as a flag on
        the release call lets the DM finish the recipe days earlier, and lets the
        release screen render `release_blockers` BEFORE the button is pressed
        instead of explaining a rejection afterwards.

        `no_accessories: true` WHILE ACCESSORY LINES EXIST IS A 422, not a silent
        precedence rule. The two statements contradict each other, and guessing
        which one the DM meant is how a kit gets skipped for a whole order.
        """
        style = await self._style(style_id)
        self._assert_editable(style)
        lines = await self.repo.lines_for_style(style_id)
        accessory_lines = [l for l in lines
                           if l.category == MaterialCategory.ACCESSORY.value]
        if no_accessories and accessory_lines:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{style.name} has {len(accessory_lines)} accessory line(s) "
                f"({', '.join(l.article for l in accessory_lines[:3])}) but you "
                f"declared it takes none. Remove the lines, or confirm with "
                f"no_accessories: false.")

        now = datetime.now(timezone.utc)
        style.material_spec_confirmed_at = now
        style.material_spec_confirmed_by = actor_name
        style.material_spec_no_accessories = bool(no_accessories)

        has_leather = any(l.category == MaterialCategory.LEATHER.value
                          and (l.qty_per_piece or 0) > 0 for l in lines)
        blockers = kit_rules.release_blockers(
            style_name=style.name, confirmed_at=now,
            no_accessories=style.material_spec_no_accessories,
            has_accessory_lines=bool(accessory_lines),
            has_leather_line=has_leather)

        # WARNINGS, NOT BLOCKERS. Lining consumption has been optional on the cut
        # path since the client confirmed every lining field is optional, so
        # requiring a lining line here would contradict the ledger downstream.
        warnings = []
        if not any(l.category == MaterialCategory.LINING.value for l in lines):
            warnings.append(
                "No LINING line — the lining cut screen will not prefill a "
                "quantity, and no lining stock will move for this style.")

        await self._audit(
            actor_id, BarcodeAuditAction.STYLE_MATERIAL_SPEC_CONFIRMED.value,
            style.id,
            {"style_code": style.code, "by": actor_name,
             "line_count": len(lines), "accessory_line_count": len(accessory_lines),
             "no_accessories": bool(no_accessories),
             "has_leather_line": has_leather})
        await self.db.commit()

        return {
            "style_id": str(style.id), "style_code": style.code,
            "confirmed": True, "confirmed_at": now.isoformat(),
            "confirmed_by": actor_name,
            "no_accessories_declared": bool(no_accessories),
            "line_count": len(lines),
            "accessory_line_count": len(accessory_lines),
            "release_blockers": blockers, "warnings": warnings,
            "message": (f"{style.name}'s material spec is confirmed. It may now "
                        f"be released."
                        if not blockers else
                        f"{style.name} is confirmed but still cannot be "
                        f"released — see release_blockers."),
        }

    async def copy_from(self, style_id: uuid.UUID, source_style_id: uuid.UUID, *,
                        include_sku_overrides: bool = False,
                        actor_name: str, actor_id=None) -> dict:
        """Seed this style's recipe from another one.

        A leather factory repeats styles season after season, so re-typing five
        accessory lines per style is the difference between the gate being used
        and the gate being resented.

        SKU OVERRIDES ONLY COPY WHERE THE COLOURWAYS MATCH on (color_code, size).
        Two styles rarely share SKU ids, and copying an override onto the wrong
        colourway would silently issue the wrong colour button — so unmatched
        overrides are REPORTED in `skipped_detail` rather than guessed at.
        """
        style = await self._style(style_id)
        self._assert_editable(style)
        source = await self._style(source_style_id)

        src_lines = await self.repo.lines_for_style(source_style_id)
        sku_map: dict = {}
        if include_sku_overrides:
            mine = (await self.db.execute(
                select(SKU).where(SKU.style_id == style.id))).scalars().all()
            theirs = (await self.db.execute(
                select(SKU).where(SKU.style_id == source.id))).scalars().all()
            by_key = {(s.color_code, s.size): s.id for s in mine}
            for s in theirs:
                target = by_key.get((s.color_code, s.size))
                if target:
                    sku_map[s.id] = target

        copied, skipped_detail = 0, []
        for line in src_lines:
            if line.sku_id is not None:
                if not include_sku_overrides:
                    continue
                target_sku = sku_map.get(line.sku_id)
                if target_sku is None:
                    skipped_detail.append({
                        "article": line.article, "scope": "SKU",
                        "reason": f"{style.name} has no matching colour/size for "
                                  f"this override."})
                    continue
            else:
                target_sku = None
            dup = await self.repo.find_duplicate_line(
                style_id=style.id, sku_id=target_sku, category=line.category,
                subtype=line.subtype, article=line.article, colour=line.colour,
                thickness=line.thickness, size=line.size)
            if dup is not None:
                skipped_detail.append({"article": line.article,
                                       "reason": "Already on this style."})
                continue
            self.repo.add_line_nocommit(
                style_id=style.id, sku_id=target_sku, category=line.category,
                subtype=line.subtype, article=line.article, colour=line.colour,
                thickness=line.thickness, size=line.size,
                qty_per_piece=line.qty_per_piece, uom=line.uom,
                material_lot_id=line.material_lot_id, note=line.note)
            copied += 1
        await self.db.commit()
        return {
            "copied": copied, "skipped": len(skipped_detail),
            "skipped_detail": skipped_detail,
            "source_style": source.name,
            # THE COPY DOES NOT CONFIRM. Somebody still has to look at the numbers
            # for THIS style before it may be released.
            "message": (f"Copied {copied} line(s) from {source.name}. Review the "
                        f"quantities, then confirm the spec."),
        }

    # ══════════════════════════════════ the requirement projection (pre-release)
    async def requirement(self, style_id: uuid.UUID) -> dict:
        """qty_ordered x per-piece vs what is actually on the shelf.

        THIS IS THE SCREEN THAT SHOULD STOP AN ORDER, and the moment to look at it
        is BEFORE release: after release the garments exist and the shortfall is
        a stoppage instead of a purchase order. The shortfall feeds straight into
        the existing POST /suppliers/orders.

        `pieces` is scoped per line: a style-wide line is needed by every ordered
        unit of the style, an override only by its own SKU — and the override's
        SKU is subtracted from the style line so the same garment is never counted
        against two lines for the same material.
        """
        style = await self._style(style_id)
        lines = await self.repo.lines_for_style(style_id)
        rows = (await self.db.execute(
            select(SKU.id, func.coalesce(SKU.qty_ordered, 0))
            .where(SKU.style_id == style_id))).all()
        qty_by_sku = {sid: int(q or 0) for sid, q in rows}
        total_qty = sum(qty_by_sku.values())

        # Which SKUs have their own line for a given (category, subtype, article)?
        overridden: dict = {}
        for l in lines:
            if l.sku_id is not None:
                key = ((l.category or "").upper(), l.subtype, l.article)
                overridden.setdefault(key, set()).add(l.sku_id)

        out_lines, short_lines = [], 0
        for line in lines:
            payload = await self._line_payload(line)
            if line.sku_id is not None:
                pieces = qty_by_sku.get(line.sku_id, 0)
            else:
                key = ((line.category or "").upper(), line.subtype, line.article)
                pieces = total_qty - sum(qty_by_sku.get(s, 0)
                                         for s in overridden.get(key, ()))
            required = Decimal(str(line.qty_per_piece or 0)) * pieces
            payload["pieces"] = pieces
            payload["total_required"] = float(required)
            available = Decimal(str((payload["lot"] or {}).get("available", 0)))
            short = required - available if payload["lot"] else required
            payload["short_by"] = float(short) if short > 0 else 0.0
            if short > 0:
                short_lines += 1
                supplier = await self.materials.suggest_supplier(line.article)
                payload["suggested_supplier"] = (
                    {"id": str(supplier.id), "name": supplier.name}
                    if supplier else None)
            else:
                payload["suggested_supplier"] = None
            out_lines.append(payload)

        blockers = kit_rules.release_blockers(
            style_name=style.name,
            confirmed_at=style.material_spec_confirmed_at,
            no_accessories=style.material_spec_no_accessories,
            has_accessory_lines=any(l.category == MaterialCategory.ACCESSORY.value
                                    for l in lines),
            has_leather_line=any(l.category == MaterialCategory.LEATHER.value
                                 and (l.qty_per_piece or 0) > 0 for l in lines))
        return {
            "style_id": str(style.id), "style_code": style.code,
            "style_name": style.name, "qty_ordered": total_qty,
            "confirmed": style.material_spec_confirmed_at is not None,
            "release_blockers": blockers,
            "lines": out_lines, "short_lines": short_lines,
            "message": (
                f"{short_lines} of {len(out_lines)} material line(s) are short "
                f"for {total_qty} piece(s). Raise a supplier order before "
                f"releasing." if short_lines else
                f"Stock covers all {len(out_lines)} material line(s) for "
                f"{total_qty} piece(s)."),
        }

    # ══════════════════════════════════════ surfaces other modules call
    async def blockers_for_styles(self, style_ids: list[uuid.UUID]) -> dict:
        """{style_id: [blocker sentence, ...]} — the release gate, batched.

        Called from BreakdownService.release_styles' per-style validation loop,
        which already partial-accepts. Batched because a release can name a dozen
        styles and the loop must not become a dozen round trips.
        """
        if not style_ids:
            return {}
        rows = (await self.db.execute(
            select(Style).where(Style.id.in_(style_ids)))).scalars().all()
        by_style = await self.repo.lines_for_styles([s.id for s in rows])
        out: dict = {}
        for style in rows:
            lines = by_style.get(style.id, [])
            out[style.id] = kit_rules.release_blockers(
                style_name=style.name,
                confirmed_at=style.material_spec_confirmed_at,
                no_accessories=style.material_spec_no_accessories,
                has_accessory_lines=any(
                    l.category == MaterialCategory.ACCESSORY.value for l in lines),
                has_leather_line=any(
                    l.category == MaterialCategory.LEATHER.value
                    and (l.qty_per_piece or 0) > 0 for l in lines))
        return out

    async def kit_required_for_piece(self, piece_id: uuid.UUID) -> bool:
        """Does the garment behind this piece take any accessories at all?

        The Python half of the two-shape pattern (the SQL half is
        StyleSpecRepository.kit_required_sql). FALSE for every style that predates
        the spec feature, which is what makes the completeness predicate collapse
        to its pre-existing form for everything already on the floor.
        """
        _piece, sku, style = await self._piece_context(piece_id)
        if style is None:
            return False
        lines = await self.effective_lines(style.id, sku.id if sku else None)
        return any(l.category == MaterialCategory.ACCESSORY.value for l in lines)

    async def kit_by_pieces(self, piece_ids: list[uuid.UUID]) -> dict:
        """{piece_code: {kit_status, kit_required, outstanding}} for a whole batch.

        DELIBERATELY LEAN. /production/log can carry 40 pieces, and the full
        requirement block per piece would dwarf the response the scan screen
        actually reads. Three fields is enough for the screen to decide whether to
        show a "kit still owed" chip and link through.

        Four queries total regardless of batch size.
        """
        from app.modules.production.models import Piece
        if not piece_ids:
            return {}
        pieces = (await self.db.execute(
            select(Piece).where(Piece.id.in_(piece_ids)))).scalars().all()
        sku_ids = {p.sku_id for p in pieces if p.sku_id}
        skus = {s.id: s for s in (await self.db.execute(
            select(SKU).where(SKU.id.in_(sku_ids)))).scalars().all()} if sku_ids else {}
        style_ids = {s.style_id for s in skus.values()}
        lines_by_style = await self.repo.lines_for_styles(list(style_ids))
        issued_by_piece = await self.repo.issued_by_pieces([p.id for p in pieces])

        out: dict = {}
        for piece in pieces:
            sku = skus.get(piece.sku_id)
            all_lines = lines_by_style.get(sku.style_id, []) if sku else []
            eff = self.merge_lines(all_lines, sku.id if sku else None)
            acc = [l for l in eff if l.category == MaterialCategory.ACCESSORY.value]
            issued = issued_by_piece.get(piece.id, {})
            required_total = sum(float(l.qty_per_piece or 0) for l in acc)
            issued_total = sum(float(issued.get(l.id, 0)) for l in acc)
            out[piece.code] = {
                "kit_required": bool(acc),
                "kit_status": kit_rules.kit_status(
                    kit_required=bool(acc), required_total=required_total,
                    issued_total=issued_total),
                "outstanding": max(0.0, round(required_total - issued_total, 3)),
            }
        return out

    async def material_requirement_block(self, piece_id) -> dict:
        """What this garment needs, what it has been given, and what is still owed.

        THE ANSWER TO "how does the operator know which accessories to put in the
        drawer?". It hangs off BOTH scan payloads — the piece code and the drawer
        code — because those are the two things a person at the store actually
        has in their hand.

        ONE READ SURFACE OVER TWO WRITE PATHS. Leather and lining consumption live
        on ProductionEvent (the act of cutting); accessories live in
        piece_material_issue (the act of issuing). A screen must not have to know
        that, so both are merged here.

        A null piece_id returns the NOT_REQUIRED shape rather than None, so the
        drawer payload for an empty drawer still has a block to render.
        """
        empty = {"kit_status": KitStatus.NOT_REQUIRED.value, "kit_required": False,
                 "spec_confirmed": False, "summary_line": None,
                 "leather": None, "lining": None, "accessories": []}
        if piece_id is None:
            return empty
        try:
            piece, sku, style = await self._piece_context(piece_id)
        except HTTPException:
            return empty
        if style is None:
            return empty

        lines = await self.effective_lines(style.id, sku.id if sku else None)
        if not lines:
            return dict(empty,
                        spec_confirmed=style.material_spec_confirmed_at is not None)

        issued = await self.repo.issued_by_piece(piece.id)
        accessories, required_total, issued_total, unresolved = [], 0.0, 0.0, 0
        leather = lining = None

        for line in lines:
            lot, resolution, candidates = await self._resolve_lot(line)
            available = None
            if lot is not None:
                reserved = await self.materials.active_reserved(lot.id)
                available = float(Decimal(str(lot.on_hand or 0)) - reserved)
            base = {
                "spec_id": str(line.id),
                "scope": "SKU" if line.sku_id else "STYLE",
                "subtype": line.subtype, "article": line.article,
                "colour": line.colour, "thickness": line.thickness,
                "size": line.size,
                "qty_per_piece": float(line.qty_per_piece or 0),
                "uom": line.uom,
                "lot_id": str(lot.id) if lot else None,
                "available": available,
                "resolution": resolution,
                "candidate_lot_ids": candidates,
            }
            if line.category == MaterialCategory.ACCESSORY.value:
                got = float(issued.get(line.id, 0))
                need = float(line.qty_per_piece or 0)
                required_total += need
                issued_total += got
                if resolution in (RESOLUTION_NONE, RESOLUTION_AMBIGUOUS):
                    unresolved += 1
                accessories.append(dict(
                    base, issued_qty=got,
                    outstanding=max(0.0, round(need - got, 3)),
                    short=bool(available is not None and available < need)))
            elif line.category == MaterialCategory.LEATHER.value and leather is None:
                leather = base
            elif line.category == MaterialCategory.LINING.value and lining is None:
                lining = base

        # What the cut ACTUALLY consumed, from the event that recorded it — beside
        # what the recipe said it should. The two differing is normal and is the
        # first thing a costing question asks about.
        if leather is not None:
            leather["consumed"] = await self._consumed_at_cut(piece.id)

        return {
            "kit_status": kit_rules.kit_status(
                kit_required=bool(accessories), required_total=required_total,
                issued_total=issued_total, unresolved=unresolved),
            "kit_required": bool(accessories),
            "spec_confirmed": style.material_spec_confirmed_at is not None,
            "summary_line": self._summary_line(accessories),
            "leather": leather, "lining": lining, "accessories": accessories,
        }

    @staticmethod
    def _summary_line(accessories: list) -> str | None:
        """One printable sentence for a label or a scan toast."""
        if not accessories:
            return None
        parts = []
        for a in accessories:
            bits = [a["article"]]
            if a.get("colour"):
                bits.append(a["colour"])
            if a.get("size"):
                bits.append(a["size"])
            parts.append(f"{a['qty_per_piece']:g} {a['uom']} {' '.join(bits)}")
        return " · ".join(parts)

    async def _consumed_at_cut(self, piece_id) -> float | None:
        """The dcm actually recorded against this piece at LEATHER_CUTTING."""
        from app.modules.production.models import ProductionEvent
        val = await self.db.scalar(
            select(ProductionEvent.consumption_qty)
            .where(ProductionEvent.piece_id == piece_id,
                   ProductionEvent.consumption_qty.is_not(None))
            .order_by(ProductionEvent.work_date).limit(1))
        return float(val) if val is not None else None

    # ══════════════════════════════════════════════ THE KIT (the money path)
    async def issue_kit_nocommit(self, *, piece, drawer, requested_lines=None,
                                 employee_id=None, entered_by: str | None = None,
                                 materials_service=None) -> dict:
        """Issue a garment's accessories from stock into its drawer. NO COMMIT.

        THE CALLER OWNS THE TRANSACTION. DrawerService.store_scan commits once, at
        the end, so the stock movements, the ledger rows, the drawer flags and the
        audit row all land together or not at all. A half-issued kit is worse than
        an unissued one, because nothing downstream can tell them apart.

        IDEMPOTENCY IS A READ, NOT A CONSTRAINT (CLAUDE.md §13 forbids ON
        CONFLICT). outstanding = qty_per_piece minus what is already issued, so a
        second tap of the scan gun computes zero on every line, spends nothing and
        returns 200 with issued_now empty — the same "re-tap is a no-op" rule
        attendance check-in already follows. The unique constraint behind it is
        the CONCURRENCY backstop: on a true race the loser's IntegrityError rolls
        back its own decrement along with the rest of the scan, so stock cannot go
        out twice.

        RESOLVE EVERYTHING FIRST, THEN SPEND. The decrement 404s on a missing lot,
        and raising that halfway through would abort a scan that had already
        issued three good lines. So unresolvable lines come back in unresolved[]
        as data, the resolvable ones still go out, and the kit reports PARTIAL.

        SHORTFALL WARNS, NEVER BLOCKS — the rule the cut path has always had. The
        buttons are physically in the operator's hand; refusing to record them to
        protect a number would lose the record and teach the floor to work around
        the system.
        """
        from app.modules.materials.service import MaterialService
        _p, sku, style = await self._piece_context(piece.id)
        if style is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "This piece has no parent style.")

        lines = await self.effective_lines(style.id, sku.id if sku else None)
        accessories = [l for l in lines
                       if l.category == MaterialCategory.ACCESSORY.value]
        if not accessories:
            # LOUD, NOT SILENT. An operator who scans a kit and gets a cheerful
            # 200 back will believe the accessories were issued. They were not,
            # and nobody would find out until the garment reached finishing.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{style.name} has no accessory spec — there is nothing to kit "
                f"for {piece.code}. If this style does take accessories, add "
                f"them to its material spec first.")

        # An explicit line list means a partial or substituted issue: the operator
        # is short of one article and is issuing the rest, or swapping a lot.
        overrides = {}
        for row in (requested_lines or []):
            row = row if isinstance(row, dict) else dict(vars(row))
            if row.get("spec_id") is not None:
                overrides[str(row["spec_id"])] = row
        selective = bool(overrides)

        issued_already = await self.repo.issued_by_piece(piece.id)
        materials = materials_service or MaterialService(self.db)
        now = datetime.now(timezone.utc)

        issued_now, already_issued, outstanding, unresolved = [], [], [], []
        for line in accessories:
            want = Decimal(str(line.qty_per_piece or 0))
            have = Decimal(str(issued_already.get(line.id, 0)))
            owed = want - have

            if selective and str(line.id) not in overrides:
                # Not named in a selective issue: still report what it owes, so
                # the screen shows a COMPLETE checklist, but do not spend it.
                if owed > 0:
                    outstanding.append(self._kit_line(line, float(owed)))
                continue

            if owed <= 0:
                already_issued.append(
                    self._kit_line(line, 0.0, issued=float(have)))
                continue

            override = overrides.get(str(line.id), {})
            if override.get("qty") is not None:
                owed = min(owed, Decimal(str(override["qty"])))

            if override.get("material_lot_id"):
                lot = await self.materials.get_lot(override["material_lot_id"])
                resolution = RESOLUTION_PINNED if lot else RESOLUTION_NONE
                candidates = []
            else:
                lot, resolution, candidates = await self._resolve_lot(line)

            if lot is None:
                unresolved.append(dict(
                    self._kit_line(line, float(owed)),
                    reason=resolution, candidate_lot_ids=candidates,
                    note=(f"No active lot matches {line.article}"
                          f"{' · ' + line.colour if line.colour else ''} — "
                          f"receive stock for it first."
                          if resolution == RESOLUTION_NONE else
                          f"{len(candidates)} lots match {line.article} — pick "
                          f"one explicitly on this scan.")))
                continue

            available_after = await materials.decrement_for_issue_nocommit(
                lot.id, float(owed))
            row = await self.repo.find_issue(piece.id, line.id)
            if row is None:
                self.repo.add_issue_nocommit(
                    piece_id=piece.id,
                    drawer_id=drawer.id if drawer is not None else None,
                    spec_line_id=line.id, material_lot_id=lot.id,
                    category=line.category, subtype=line.subtype,
                    article=lot.article, colour=lot.colour,
                    qty=owed, uom=lot.uom,
                    source=MaterialIssueSource.STORE_KIT.value,
                    issued_by_employee_id=employee_id, entered_by=entered_by,
                    issued_at=now)
            else:
                # A TOP-UP UPDATES THE ROW rather than adding a second one: the
                # unique constraint permits exactly one row per (piece, line), and
                # that is what keeps the idempotency read a single lookup.
                row.qty = Decimal(str(row.qty or 0)) + owed
                row.material_lot_id = lot.id
                row.issued_at = now
            issued_now.append(dict(
                self._kit_line(line, float(owed)),
                lot_id=str(lot.id), available_after=available_after))
            if want - have - owed > 0:
                outstanding.append(
                    self._kit_line(line, float(want - have - owed)))

        required_total = sum(float(l.qty_per_piece or 0) for l in accessories)
        issued_total = (sum(float(issued_already.get(l.id, 0))
                            for l in accessories)
                        + sum(r["qty"] for r in issued_now))

        return {
            "status": kit_rules.kit_status(
                kit_required=True, required_total=required_total,
                issued_total=issued_total, unresolved=len(unresolved)),
            "summary_line": self._summary_line(
                [self._kit_line(l, float(l.qty_per_piece or 0))
                 for l in accessories]),
            "issued_now": issued_now, "already_issued": already_issued,
            "outstanding": outstanding, "unresolved": unresolved,
            "stock_warnings": list(materials.decrement_warnings),
            # THE FLAG THE DRAWER READS. True only when the kit is complete AND
            # nothing failed to resolve — a drawer holding an unresolved line
            # must not become sendable.
            "complete": not unresolved and not outstanding,
        }

    @staticmethod
    def _kit_line(line, qty: float, *, issued: float | None = None) -> dict:
        out = {
            "spec_id": str(line.id), "category": line.category,
            "subtype": line.subtype, "article": line.article,
            "colour": line.colour, "size": line.size,
            "qty_per_piece": float(line.qty_per_piece or 0),
            "qty": qty, "uom": line.uom,
        }
        if issued is not None:
            out["issued"] = issued
        return out

    async def kit_view(self, piece_id, drawer=None) -> dict:
        """The kit block WITHOUT issuing anything — the read-only checklist.

        Returned on every store-scan, not only the accessory one, because the
        operator holding the leather is the person who also has to put the buttons
        in. Answering "what else does this drawer need?" on the scan they were
        already doing is the whole visibility ask.
        """
        block = await self.material_requirement_block(piece_id)
        return {
            "status": block["kit_status"],
            "summary_line": block["summary_line"],
            "issued_now": [], "already_issued": [],
            "outstanding": [a for a in block["accessories"]
                            if a["outstanding"] > 0],
            "unresolved": [a for a in block["accessories"]
                           if a["resolution"] in (RESOLUTION_NONE,
                                                  RESOLUTION_AMBIGUOUS)],
            "stock_warnings": [],
            "complete": block["kit_required"] and all(
                a["outstanding"] <= 0 for a in block["accessories"]),
        }

    async def issue_manual(self, *, piece_id, material_lot_id, qty: float,
                           note: str | None = None, employee_id=None,
                           entered_by: str | None = None, actor_id=None) -> dict:
        """Record a material issued OFF-SPEC. Commits.

        THIS IS WHAT MAKES FREEZING THE RECIPE AT RELEASE ACCEPTABLE. A released
        style's spec cannot be edited — its pieces carry printed barcodes and the
        recipe is already being spent against them — so without this, a typo'd
        button article would be uncorrectable for a whole order and the floor
        would be issuing stock the system never saw.

        It writes source=MANUAL with a NULL spec id, which is deliberate on both
        counts: the unique constraint does not dedupe NULLs, so a manual issue is
        repeatable (three corrections on one garment are three real events), and
        the kit's idempotency read ignores these rows, so a correction never makes
        the checklist think a spec line was satisfied.
        """
        from app.modules.materials.service import MaterialService
        piece, _sku, _style = await self._piece_context(piece_id)
        lot = await self.materials.get_lot(material_lot_id)
        if lot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material lot not found.")
        if qty is None or float(qty) <= 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Issued quantity must be > 0.")

        materials = MaterialService(self.db)
        available_after = await materials.decrement_for_issue_nocommit(
            lot.id, float(qty))
        self.repo.add_issue_nocommit(
            piece_id=piece.id, drawer_id=piece.drawer_id,
            spec_line_id=None, material_lot_id=lot.id,
            category=lot.category, subtype=lot.subtype, article=lot.article,
            colour=lot.colour, qty=Decimal(str(qty)), uom=lot.uom,
            source=MaterialIssueSource.MANUAL.value,
            issued_by_employee_id=employee_id, entered_by=entered_by,
            issued_at=datetime.now(timezone.utc))
        await self._audit(
            actor_id, BarcodeAuditAction.MATERIAL_ISSUED_MANUAL.value, piece.id,
            {"piece": piece.code, "article": lot.article, "colour": lot.colour,
             "qty": float(qty), "uom": lot.uom, "by": entered_by, "note": note})
        await self.db.commit()
        return {
            "piece_code": piece.code, "article": lot.article,
            "colour": lot.colour, "qty": float(qty), "uom": lot.uom,
            "source": MaterialIssueSource.MANUAL.value,
            "available_after": available_after,
            "stock_warning": materials.last_decrement_warning,
            "message": (f"Recorded {qty:g} {lot.uom} of {lot.article} issued to "
                        f"{piece.code} outside the spec."),
        }
