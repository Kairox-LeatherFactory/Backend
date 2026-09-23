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
    that spends it when the store kits a garment.

THE FOUR SURFACES IT SERVES, and why they all come through here
    imports/breakdown   release gate      — blockers_for_styles
    store/service       the kit scan      — issue_kit_nocommit
    barcode/service     the piece scan    — material_requirement_block
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
from app.modules.materials.service import stock_numbers
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
    def _assert_editable(style: Style, *, category: str | None = None) -> None:
        """The recipe freezes when the style is released — ACCESSORIES EXCEPTED.

        WHY IT FREEZES AT ALL. Once released, the style's pieces carry printed
        barcodes and the recipe is already being spent against them. Editing the
        LEATHER line underneath that would rewrite what a garment was costed at
        after it was cut, and the cutting record and the costing would disagree
        with nothing to say which is right.

        WHY ACCESSORIES ARE DIFFERENT (backend fix #12). Reported: "After
        release, if the DM finds an incorrect accessory assignment, there is
        currently no option to edit it." An accessory line is not a measurement
        of work already done — it is a list of what still has to be put in the
        bag, and the wrong button is discovered precisely when somebody goes to
        fetch it, which is always after release.

        WHAT AN EDIT DOES NOT DO is rewrite history. `piece_material_issue` is
        the record of what was ACTUALLY issued and is untouched: garments already
        kitted keep exactly what they were given. The edit changes what the
        checklist asks for from here on.
        """
        if spec_editable(style):
            return
        if (category or "").upper() == MaterialCategory.ACCESSORY.value:
            return
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{style.name} is {style.production_status} — its {category or 'material'} "
            f"spec is frozen because its pieces carry printed barcodes and the "
            f"spec is already being spent against them. Accessory lines may "
            f"still be corrected after release; leather and lining may not, "
            f"because they are what the garments were cut and costed against. "
            f"To correct what a garment was actually issued, record it on the "
            f"floor with POST /materials/issues.")

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

        article = (body.get("article") or "").strip() or None
        if category == "ACCESSORY" and not article:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Every accessory recipe line must name an article.")

        thickness = (body.get("thickness") or "").strip() or None
        if category in {"LEATHER", "LINING"} and not thickness:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{category} recipe lines must include thickness.")

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
            "thickness": thickness,
            "size": (body.get("size") or "").strip() or None,
            # WHICH GARMENT SIZES THIS LINE IS FOR. Explicit wins; otherwise it
            # is inferred from the material's own size, because in this factory
            # a size-specific accessory is labelled with the GARMENT's size —
            # a "zip L" is the zip for an L jacket. That default is what lets the
            # DM keep entering exactly what they entered before and have the
            # matching start working. A 60cm zip or an 18L button does not read
            # as a garment size and stays NULL = every size.
            "garment_size": (
                (body.get("garment_size") or "").strip() or None
                or ((body.get("size") or "").strip().upper()
                    if self._looks_like_a_garment_size(body.get("size"))
                    else None)),
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
    def _looks_like_a_garment_size(value) -> bool:
        """Is this material `size` actually a GARMENT size in disguise?

        In this factory an accessory that varies by garment size is labelled with
        the garment's size — a "zip L" is the zip for an L jacket, and the DM
        enters exactly that. So a line whose material size reads as a garment
        size is almost certainly size-specific, and defaulting garment_size to it
        makes the matching work without asking the DM to fill in a new field they
        have never had to fill in before.

        A 60cm zip or an 18L button is NOT a garment size, and must not be read
        as one: doing so would confine a perfectly general line to a size that
        does not exist.
        """
        from app.core.leather_norms import normalise_size
        token = (str(value or "")).strip().upper()
        if not token:
            return False
        # A real garment size normalises to a rung. '60CM' and '18L' do not.
        if any(ch.isalpha() for ch in token) and not token.isalnum():
            return False
        if token.isdigit():
            return 30 <= int(token) <= 70        # the EU jacket ladder
        return normalise_size(token) is not None and len(token) <= 5

    @staticmethod
    def applies_to_size(line, garment_size: str | None) -> bool:
        """Does this recipe line belong on a garment of this size?

        NULL garment_size MEANS EVERY SIZE, and that is what makes this change
        back-compatible: every line that predates the column has NULL, so every
        already-released style resolves to exactly the recipe it resolved to
        before.
        """
        want = getattr(line, "garment_size", None)
        if not want:
            return True
        if not garment_size:
            return True          # the piece's size is unknown — do not drop it
        from app.core.leather_norms import normalise_size
        a, b = str(want).strip().upper(), str(garment_size).strip().upper()
        if a == b:
            return True
        # '52' and 'L' are the same garment on the Italian ladder.
        na, nb = normalise_size(a), normalise_size(b)
        return na is not None and na == nb

    @staticmethod
    def merge_lines(lines: list, sku_id: uuid.UUID | None,
                    garment_size: str | None = None) -> list:
        """The effective recipe for ONE colourway AND ONE SIZE.

        THE OVERRIDE IS LINE-LEVEL, keyed on (category, subtype, article,
        garment_size). A SKU-scoped line replaces the style-wide line with the
        same key and is added alongside one with a different key.

        `garment_size` IS WHY THIS SIGNATURE CHANGED. It used to key on three
        fields and never look at the piece's size at all, which is how a DM
        entering Thread S / M / L got either all three lines on every garment
        (distinct articles → distinct keys) or silently only the last one (same
        article, distinct sizes → one key). Both are wrong, and neither is
        visible from outside: the store rendered three sizes of thread for one
        jacket and the kit scan spent all three.

        ALL FOUR CONSUMING SURFACES GO THROUGH HERE — kit_required_for_piece,
        kit_by_pieces, material_requirement_block and issue_kit_nocommit — so
        this one filter fixes the checklist, the scan payload, the production
        log's kit block and the actual spend together. That is the reason the fix
        belongs here and not in each caller.
        """
        style_lines = [l for l in lines if l.sku_id is None]
        sku_lines = [l for l in lines if sku_id is not None and l.sku_id == sku_id]

        def key(l):
            return ((l.category or "").upper(), (l.subtype or "") or None,
                    (l.article or ""),
                    (getattr(l, "garment_size", None) or "") or None)

        merged = {key(l): l for l in style_lines}
        for l in sku_lines:
            merged[key(l)] = l          # override or addition

        return [l for l in merged.values()
                if (l.qty_per_piece or 0) > 0
                and StyleSpecService.applies_to_size(l, garment_size)]

    @staticmethod
    def line_not_applicable_reason(line, sku_id, garment_size: str | None):
        """Why does this style's line NOT reach this garment? None = it does.

        WHY A NAMED REASON AND NOT A SILENT FILTER. `merge_lines` drops a line by
        returning a shorter list, and every caller downstream then sees a recipe
        that simply does not mention the buttons. That is correct behaviour and
        an awful diagnosis: the DM opens `/material-spec/requirement`, sees three
        accessory lines, scans the garment, and is told the style "has no
        accessory spec". Both statements are true — the requirement view is
        STYLE-WIDE and the scan is PER GARMENT — and nothing on either screen
        says so. This turns the drop into a sentence.

        THE TWO REASONS A LINE IS DROPPED, and neither is a bug:
          · it is scoped to a DIFFERENT colourway (`sku_id`), so a NAVY jacket
            does not get the PINE GREEN knit. Issuing it anyway would put the
            wrong colour in the bag.
          · it is for a different GARMENT SIZE — the L zip is not the S zip.
        A zeroed override is the third, and it is a deliberate statement that
        this colourway takes none of that material.
        """
        if line.sku_id is not None and line.sku_id != sku_id:
            return "other_sku"
        if (line.qty_per_piece or 0) <= 0:
            return "zeroed"
        if not StyleSpecService.applies_to_size(line, garment_size):
            return "other_size"
        return None

    async def effective_lines(self, style_id: uuid.UUID,
                              sku_id: uuid.UUID | None,
                              garment_size: str | None = None) -> list:
        """The recipe that actually applies to one SKU of one style, AT ITS SIZE.

        The size is looked up from the SKU when the caller does not supply it, so
        every existing call site becomes size-aware without being edited — which
        matters, because there are four of them and missing one would leave a
        surface silently spending the wrong accessories.
        """
        if garment_size is None and sku_id is not None:
            sku = await self.db.get(SKU, sku_id)
            garment_size = getattr(sku, "size", None)
        return self.merge_lines(await self.repo.lines_for_style(style_id),
                                sku_id, garment_size)

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
            # WHICH GARMENTS THIS LINE IS FOR. Returned because the recipe grid
            # has to SHOW it beside `size`: the two look alike and mean different
            # things — `size` is the material's (a 60cm zip), this is which
            # jackets it belongs on. Stored and matched but never returned is how
            # a DM edits a line and cannot see what they changed.
            "garment_size": getattr(line, "garment_size", None),
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
            out["lot"] = {
                "lot_id": str(lot.id), "article": lot.article,
                "colour": lot.colour, "uom": lot.uom,
                # arrived / used / balance / reserved / available / on_hand, all
                # reconciling with each other. RESERVED IS SHOWN, NOT SILENTLY
                # SUBTRACTED from what is on the shelf: a stuck reservation would
                # otherwise present as a phantom shortfall with no visible cause.
                **stock_numbers(lot.on_hand, lot.used, reserved),
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
            # garment_size is part of the identity: "Thread for L" and "Thread
            # for M" are two lines, not one line entered twice. Leaving it out is
            # what made the second silently overwrite the first.
            return (d["sku_id"], d["category"], d["subtype"], d["article"],
                    d["colour"], d["thickness"], d["size"],
                    d.get("garment_size"))

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
             l.size, getattr(l, 'garment_size', None)): l for l in existing}

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
        self._assert_editable(style, category=(body or {}).get("category"))
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
        line = await self.repo.get_line(line_id)
        if line is None or line.style_id != style.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "No such line on this style.")
        # The line is fetched FIRST so the freeze can be judged on what is
        # actually being edited: accessories stay correctable after release,
        # leather and lining do not.
        self._assert_editable(style, category=line.category)
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
            # Don't change line.style_id.
            if field != "style_id":
                setattr(line, field, value)
        await self.db.commit()
        await self.db.refresh(line)
        return await self._line_payload(line)

    async def deactivate_line(self, style_id: uuid.UUID, line_id: uuid.UUID, *,
                              actor_name: str, actor_id=None) -> dict:
        """SOFT delete. The ledger points at this row and must keep resolving."""
        style = await self._style(style_id)
        line = await self.repo.get_line(line_id)
        if line is None or line.style_id != style.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "No such line on this style.")
        # The line is fetched FIRST so the freeze can be judged on what is
        # actually being edited: accessories stay correctable after release,
        # leather and lining do not.
        self._assert_editable(style, category=line.category)
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

        # HOW MANY GARMENTS OF EACH SIZE, so a size-specific line is multiplied
        # by the garments it actually reaches. Without this a style with three
        # sized zip lines ordered three zips per garment instead of one — the
        # requirement is what a purchase is raised from, so the error would have
        # been bought.
        size_rows = (await self.db.execute(
            select(SKU.size, func.coalesce(func.sum(SKU.qty_ordered), 0))
            .where(SKU.style_id == style_id).group_by(SKU.size))).all()
        qty_by_size = {(sz or "").strip().upper(): int(q or 0)
                       for sz, q in size_rows}

        # Which SKUs have their own line for a given (category, subtype, article)?
        overridden: dict = {}
        for l in lines:
            if l.sku_id is not None:
                key = ((l.category or "").upper(), l.subtype, l.article)
                overridden.setdefault(key, set()).add(l.sku_id)

        out_lines, short_lines = [], 0
        for line in lines:
            payload = await self._line_payload(line)
            gsize = (getattr(line, "garment_size", None) or "").strip().upper()
            if line.sku_id is not None:
                pieces = qty_by_sku.get(line.sku_id, 0)
            elif gsize:
                # A sized line reaches only garments of that size.
                pieces = qty_by_size.get(gsize, 0)
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
        by_style = await self.repo.lines_for_styles([s.id for s in rows]) # LINE STYLE ONLY
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
            # SIZE-AWARE, like every other consumer of the recipe. Without the
            # third argument this batched read answered a different recipe from
            # the one the store would actually issue — the scan screen showed
            # three sizes of thread owed and the kit then issued one.
            eff = self.merge_lines(all_lines, sku.id if sku else None,
                                   getattr(sku, "size", None))
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
        garment?". It hangs off the PIECE payload, because the garment is the one
        thing the person at the store physically has in their hand — the store
        scan is the worker and the garment, and nothing else.

        ONE READ SURFACE OVER TWO WRITE PATHS. Leather and lining consumption live
        on ProductionEvent (the act of cutting); accessories live in
        piece_material_issue (the act of issuing). A screen must not have to know
        that, so both are merged here.

        A null piece_id returns the NOT_REQUIRED shape rather than None, so a
        screen always has something to render.
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
    async def piece_materials(self, piece_id) -> dict:
        """EVERYTHING that is or should be merged into ONE garment.

        THE QUESTION NOTHING COULD ANSWER. `material_requirement_block` shows
        what this garment's recipe asks for, and `/material-spec/requirement`
        shows the style's whole line set — but between them sat the case that
        actually bites: a line that EXISTS on the style and does not reach this
        garment. The DM saw three accessory lines on one screen, scanned the
        piece, and was told the style had no accessory spec. Both screens were
        telling the truth about different questions.

        So this returns BOTH halves, side by side and each with its reason:

          `applies`        — the recipe for THIS colourway at THIS size: the
                             leather, the lining and every accessory line, with
                             what has been issued against it and what is owed.
          `not_applicable` — the style's other lines, each saying why it is not
                             this garment's: scoped to another colourway, for
                             another garment size, or zeroed for this one.
          `issued`         — the LEDGER. What physically went into the bag, from
                             which lot, when, and through whose card. Includes
                             MANUAL off-spec corrections, which the checklist
                             deliberately ignores but the garment really got.
          `consumed`       — leather/lining, which live on the cutting EVENT and
                             not in the issue ledger (CLAUDE.md §8). Merged here
                             so a screen never has to know there were two writes.

        ONE READ, BOTH DOORS. The store screen and the barcode scan both call
        this, so "what is in this garment" cannot mean two different things
        depending on which gun you picked up.
        """
        piece, sku, style = await self._piece_context(piece_id)
        empty = {
            "piece_id": str(piece.id), "piece_code": piece.code,
            "sku_id": str(sku.id) if sku else None,
            "sku_label": await self._sku_label(sku.id) if sku else None,
            "style_id": str(style.id) if style else None,
            "style_name": getattr(style, "name", None),
            "garment_size": getattr(sku, "size", None),
            "colour": (getattr(sku, "color_name", None)
                       or getattr(sku, "color_code", None)),
            "spec_confirmed": False, "no_accessories_declared": None,
            "kit_required": False, "kit_status": KitStatus.NOT_REQUIRED.value,
            "summary_line": None,
            "applies": {"leather": None, "lining": None, "accessories": []},
            "not_applicable": [], "issued": [], "consumed": None,
            "store": None,
        }
        if style is None:
            return empty

        size = getattr(sku, "size", None)
        all_lines = await self.repo.lines_for_style(style.id)
        block = await self.material_requirement_block(piece.id)

        # THE OTHER HALF — the lines this garment is NOT on, each with its reason.
        not_applicable = []
        for line in all_lines:
            reason = self.line_not_applicable_reason(
                line, sku.id if sku else None, size)
            if reason is None:
                continue
            payload = await self._line_payload(line, with_lot=False)
            payload["reason"] = reason
            payload["reason_note"] = {
                "other_sku": (
                    f"Scoped to {await self._sku_label(line.sku_id)}; this "
                    f"garment is {await self._sku_label(sku.id) if sku else 'unassigned'}."),
                "other_size": (
                    f"For garment size {getattr(line, 'garment_size', None)}; "
                    f"this garment is {size or 'of unknown size'}."),
                "zeroed": (
                    "Set to qty_per_piece 0 for this colourway — the spec says "
                    "this one does not take it."),
            }.get(reason, reason)
            not_applicable.append(payload)

        issued = []
        for row in await self.repo.issue_rows_for_piece(piece.id):
            issued.append({
                "issue_id": str(row.id),
                "spec_line_id": str(row.spec_line_id) if row.spec_line_id else None,
                "category": row.category, "subtype": row.subtype,
                "article": row.article, "colour": row.colour,
                "qty": float(row.qty), "uom": row.uom,
                "material_lot_id": (str(row.material_lot_id)
                                    if row.material_lot_id else None),
                # MANUAL means an off-spec correction: really issued, and
                # deliberately not counted against the checklist.
                "source": row.source,
                "issued_by_employee_id": (str(row.issued_by_employee_id)
                                          if row.issued_by_employee_id else None),
                "entered_by": row.entered_by,
                "issued_at": row.issued_at,
            })

        return dict(
            empty,
            spec_confirmed=bool(block.get("spec_confirmed")),
            no_accessories_declared=style.material_spec_no_accessories,
            kit_required=bool(block.get("kit_required")),
            kit_status=block.get("kit_status"),
            summary_line=block.get("summary_line"),
            applies={
                "leather": block.get("leather"),
                "lining": block.get("lining"),
                "accessories": block.get("accessories") or [],
            },
            not_applicable=not_applicable,
            issued=issued,
            consumed=((block.get("leather") or {}).get("consumed")
                      if block.get("leather") else None),
            store={
                "state": piece.store_state,
                "leather_in": bool(piece.leather_in),
                "lining_in": bool(piece.lining_in),
                "accessories_in": bool(piece.accessories_in),
            },
        )

    async def _sku_label(self, sku_id) -> str:
        """'NAVY · S' for a SKU id — what a person calls that colourway."""
        if sku_id is None:
            return "style-wide"
        sku = await self.db.get(SKU, sku_id)
        if sku is None:
            return str(sku_id)
        colour = getattr(sku, "color_name", None) or getattr(sku, "color_code", None)
        return " · ".join(p for p in (colour, getattr(sku, "size", None)) if p) \
            or str(sku_id)

    async def _no_kit_reason(self, style, sku, piece) -> str:
        """The sentence behind a kit scan that found nothing to issue.

        It reads the style's WHOLE line set and says which of the three things
        happened, naming the rows — so the DM can act on it instead of being
        told to add a spec that is already there. See the call site in
        issue_kit_nocommit for why one message could not cover all three.
        """
        all_lines = await self.repo.lines_for_style(style.id)
        acc = [l for l in all_lines
               if l.category == MaterialCategory.ACCESSORY.value]
        if not acc:
            return (
                f"{style.name} has no accessory spec — there is nothing to kit "
                f"for {piece.code}. If this style does take accessories, add "
                f"them to its material spec first "
                f"(PUT /styles/{style.id}/material-spec).")

        size = getattr(sku, "size", None)
        by_reason: dict = {}
        for line in acc:
            reason = self.line_not_applicable_reason(
                line, sku.id if sku else None, size)
            by_reason.setdefault(reason, []).append(line)

        mine = await self._sku_label(sku.id) if sku else "no colourway"
        if by_reason.get("other_sku"):
            blocked = by_reason["other_sku"]
            owners = sorted({await self._sku_label(l.sku_id) for l in blocked})
            return (
                f"{style.name} has {len(acc)} accessory line(s) "
                f"({', '.join(sorted({l.article for l in acc}))}), but none of "
                f"them is for {piece.code}. {len(blocked)} of them are scoped to "
                f"other colourways ({', '.join(owners)}) and this garment is "
                f"{mine}. A per-SKU "
                f"line is deliberately not issued to another colourway — that "
                f"would put the wrong colour in the bag. Either add the lines "
                f"for this SKU, or make them style-wide by clearing `sku_id` "
                f"(PUT /styles/{style.id}/material-spec). "
                f"GET /store/pieces/{piece.code}/materials shows exactly which "
                f"lines reach this garment and which do not.")
        if by_reason.get("other_size"):
            blocked = by_reason["other_size"]
            sizes = sorted({str(getattr(l, "garment_size", "")) for l in blocked})
            return (
                f"{style.name}'s {len(blocked)} accessory line(s) are for "
                f"garment size(s) {', '.join(s for s in sizes if s)}, and "
                f"{piece.code} is a {size or 'garment of unknown size'}. A sized "
                f"line is not issued to another size — an L zip is not an S zip. "
                f"Add the line for this size, or clear `garment_size` to make it "
                f"apply to every size.")
        if by_reason.get("zeroed"):
            return (
                f"{style.name} declares that {mine} takes none of its "
                f"{len(acc)} accessory line(s) — every one of them is set to "
                f"qty_per_piece 0 for this colourway, which is how a per-SKU "
                f"override says 'not this one'. There is nothing to kit for "
                f"{piece.code}, and that is the spec working as written.")
        return (
            f"{style.name} has {len(acc)} accessory line(s) but none resolves "
            f"for {piece.code}. See "
            f"GET /store/pieces/{piece.code}/materials.")

    async def issue_kit_nocommit(self, *, piece, requested_lines=None,
                                 employee_id=None, entered_by: str | None = None,
                                 materials_service=None, **_legacy) -> dict:
        """Issue a garment's accessories from stock into it. NO COMMIT.

        THE CALLER OWNS THE TRANSACTION. StoreService.store_scan commits once, at
        the end, so the stock movements, the ledger rows, the piece's flags and
        the audit row all land together or not at all. A half-issued kit is worse
        than an unissued one, because nothing downstream can tell them apart.

        `drawer=` is swallowed by `**_legacy`. The kit went INTO a numbered box;
        it goes into the garment, and `piece_material_issue.drawer_id` is left
        NULL — the column is retained for the historical rows, not written.

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
            #
            # AND IT MUST SAY WHICH "NO". "No accessory spec" was one message
            # covering two completely different situations, and it sent the DM
            # to add lines that were already there:
            #   · the style genuinely declares none          → add them
            #   · the style declares several, but every one is scoped to another
            #     colourway or another garment size          → the lines exist;
            #     this GARMENT is not on any of them
            # The second is what `/material-spec/requirement` shows as a healthy
            # three-line recipe, because that view is STYLE-WIDE while a kit is
            # issued PER GARMENT.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                await self._no_kit_reason(style, sku, piece))

        # An explicit line list means a partial or substituted issue: the operator
        # is short of one article and is issuing the rest, or swapping a lot.
        overrides = {}
        # If row is already a dictionary, keep it as it is. Otherwise, convert the object's attributes into a dictionary.
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
            want = Decimal(str(line.qty_per_piece or 0)) # 4 from material spec
            have = Decimal(str(issued_already.get(line.id, 0))) # 4 we have a fixed one
            owed = want - have # 0 if we already issued the full amount

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

    async def kit_view(self, piece_id, **_legacy) -> dict:
        """The kit block WITHOUT issuing anything — the read-only checklist.

        Returned on every store-scan, not only the accessory one, because the
        operator holding the leather is the person who also has to put the buttons
        in. Answering "what else does this garment need?" on the scan they were
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
            # NOTHING IS OWED — which is TRUE for a style that declares no
            # accessories at all. This used to require kit_required, so every
            # garment of every style released before the material spec came back
            # `complete: false` on a kit it could never be given, and the store
            # screen showed a permanent outstanding chip on it. The write path
            # has always used kit_rules.kit_satisfied (`accessories_in or not
            # kit_required`); this is the same sentence, said on the read path.
            "complete": (not block["kit_required"]
                         or all(a["outstanding"] <= 0
                                for a in block["accessories"])),
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
            piece_id=piece.id,
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
