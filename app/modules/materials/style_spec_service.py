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
RESOLUTION_MISMATCH = "MISMATCH"    # a lot was NAMED and it is not this line's

# What matching a scanned packet against the recipe can conclude. The store turns
# each into a different HTTP answer, which is the whole point of naming them:
# WRONG_SIZE waits for a DM, NO_LINE_FOR_SIZE is a recipe the DM must fix, and
# NOT_IN_RECIPE is simply the wrong packet in the operator's hand.
PACKET_OK = "OK"
PACKET_NO_KIT = "NO_KIT"            # the style declares no accessories at all
PACKET_WRONG_SIZE = "WRONG_SIZE"
PACKET_NO_LINE_FOR_SIZE = "NO_LINE_FOR_SIZE"
PACKET_NOT_IN_RECIPE = "NOT_IN_RECIPE"
PACKET_AMBIGUOUS = "AMBIGUOUS"


class StyleSpecService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = StyleSpecRepository(db)
        self.materials = MaterialRepository(db)
        # ACCESSORY KINDS ARE DATA. The catalogue overlays MATERIAL_SPEC for
        # accessories and falls back to it for leather/lining, so a recipe line is
        # validated against the same table a stock lot is — which is the property
        # that stops a line being written that no lot could ever match.
        from app.modules.materials.accessory_catalog import AccessoryCatalog
        self.catalog = AccessoryCatalog(db)

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
        have rejected can never resolve to anything. The accessory catalogue (plus
        the built-in MATERIAL_SPEC beneath it) is the one table that says which
        (category, subtype) pairs exist and what each carries, and both paths ask it.

        AN ACCESSORY LINE MUST NAME A SKU. See the sku_id check below — that is the
        change this whole module was reshaped around.
        """
        category = (body.get("category") or "").strip().upper()
        subtype = (body.get("subtype") or "").strip().upper() or None
        is_accessory = category == MaterialCategory.ACCESSORY.value
        spec = await self.catalog.spec_for(category, subtype)
        if spec is None:
            if is_accessory:
                known = ", ".join(await self.catalog.known_codes())
                detail = (f"'{subtype or '(none)'}' is not an accessory kind this "
                          f"factory stocks. Known kinds: {known}. A new kind is "
                          f"created by RECEIVING one — it appears here once a "
                          f"packet of it has arrived.")
            else:
                detail = (f"'{category}"
                          f"{'/' + subtype if subtype else ''}' is not a material "
                          f"kind this factory stocks. Use LEATHER or LINING "
                          f"(PLAIN_LINING / RIBS / KNIT) — the same list "
                          f"GET /materials/spec returns.")
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)

        article = (body.get("article") or "").strip() or None
        if category == "ACCESSORY" and not article:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Every accessory recipe line must name an article.")

        size = (body.get("size") or "").strip() or None
        garment_size = (body.get("garment_size") or "").strip().upper() or None

        if is_accessory:
            # AN ACCESSORY SCOPES THROUGH ITS SKU, SO garment_size IS MEANINGLESS
            # HERE — and refused rather than ignored, because a caller who sends it
            # believes it is doing something. A SKU is unique on
            # (style_id, color_code, size), so naming one has already said which
            # size; a second, independent size scope could only ever contradict it.
            if garment_size:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"garment_size is not used on an accessory line. A SKU already "
                    f"names a colour AND a size, so `sku_id` is what says which "
                    f"garments this accessory is for — send the SKU for size "
                    f"{garment_size}, or `apply_to: \"ALL_SKUS\"` to cover them all.")
            garment_size = None
            # AND `size` IS THE MATERIAL'S OWN SIZE AGAIN. It used to be inspected
            # here and refused when it read as a garment size ('M', 'L'), which is
            # the 422 the floor kept hitting on a perfectly good zip line. With the
            # SKU carrying the garment, '60' is centimetres and 'M' is the M-size
            # zip, and neither is ambiguous any more.
        else:
            # LEATHER AND LINING can still be style-wide, so for them garment_size
            # is the only way to scope a line to a size and the old ambiguity is
            # still real: a material size that is unmistakably a garment size, with
            # nothing saying which garments it is for, is refused rather than
            # guessed at. The guess used to read any number from 30 to 70 as a
            # garment size and confined a 60cm zip to 4XL.
            if garment_size is None and self._reads_as_garment_size(size):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"This {category} line's size is '{size}', which is a garment "
                    f"size, but it does not say which garments it is for. Set "
                    f"garment_size: '{str(size).strip().upper()}' if it is for "
                    f"{str(size).strip().upper()} garments only, or leave size "
                    f"blank and name the material's own size instead. A line with "
                    f"no garment_size goes on EVERY size.")

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
                    f"line may only name a colour/size of its own style.")
        elif is_accessory:
            # THE RULE THIS CHANGE EXISTS FOR. An accessory belongs to a garment,
            # and a garment is a SKU — colour and size together. A style-wide
            # accessory line is what forced the `garment_size` column into
            # existence, and with it the size-coverage gate, the size-ambiguity
            # gate, the "is this number a garment size" guess and a PATCH that
            # silently un-scoped a line. None of that is needed once the line names
            # the garment it is for.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"An accessory line must name the SKU it is for — a SKU is a "
                f"colour and a size together, which is what decides whether this "
                f"accessory goes in. Send `sku_id`, or `apply_to: \"ALL_SKUS\"` "
                f"(also `sku_ids` / `per_sku`) to cover every colourway of "
                f"{style.name} in one call.")

        try:
            qty = Decimal(str(body.get("qty_per_piece")))
        except Exception as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "qty_per_piece must be numeric.") from exc
        # ZERO IS LEGAL ON A SKU-SCOPED LINE ONLY, and it is how one colourway says
        # "this one does not take that". On a style-wide line it would be a recipe
        # entry that consumes nothing, which is just a typo with a row in it.
        # Accessories are always SKU-scoped now, so zero is always available to
        # them — deleting the line says the same thing and is usually clearer.
        if qty < 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "qty_per_piece cannot be negative.")
        if qty == 0 and sku_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A style-wide line must consume something. Use qty_per_piece: 0 "
                "on a SKU-scoped line to say that one colourway does NOT take "
                "this material, or delete the line.")

        return {
            "style_id": style.id,
            "sku_id": sku_id,
            "category": category,
            "subtype": subtype,
            "article": article,
            "colour": (body.get("colour") or "").strip() or None,
            "thickness": thickness,
            "size": size,
            # LEATHER AND LINING ONLY, and always NULL for an accessory — an
            # accessory says which garments it is for by naming their SKU. Explicit,
            # never inferred: the inference read any number from 30 to 70 as a
            # garment size and confined a 60cm zip to 4XL jackets, which does not
            # give the other sizes a shorter recipe, it gives them none.
            "garment_size": garment_size,
            "qty_per_piece": qty,
            # DERIVED, AND A SENT VALUE IS DISCARDED. uom is a property of the
            # material kind, not a choice: buttons are pcs and thread is mtrs
            # whatever a client posts. Honouring a caller's unit would let one
            # style measure thread in yards and another in metres while both told
            # the ledger "mtrs" — and the ledger is what the decrement moves.
            # The field stays in the contract only so a GET can be round-tripped.
            "uom": await self.catalog.uom_for(category, subtype),
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

    # ════════════════════════════════════════════════ the packet → line match
    @staticmethod
    def _same(a, b) -> bool:
        return (str(a or "").strip().upper()) == (str(b or "").strip().upper())

    @classmethod
    def lot_fits_line(cls, lot, line, *, approved: bool = False) -> tuple:
        """May this lot be spent against this recipe line? (ok, why not).

        THE CHECK THAT WAS MISSING. A caller-supplied `material_lot_id` used to be
        fetched and decremented with no comparison of any kind — not the article,
        not the colour, not the size, not even `is_active` — so an operator (or a
        mistyped integration) could spend the M-size button lot against an L
        garment's line and the ledger would record it as correct. The unpinned path
        has always matched on six columns; the pinned path trusted the caller.

        SUBTYPE AND CATEGORY ARE NEVER WAIVED, approval or not: a substitution is
        somebody saying "this size will do", never "a zip will do instead of a
        button". Article, colour and size ARE waivable, because those are what a
        DM-approved substitution actually substitutes.
        """
        if lot is None:
            return False, "the lot named on this scan does not exist"
        if not lot.is_active:
            return False, f"lot {lot.article} has been retired"
        if not cls._same(lot.category, line.category):
            return False, (f"{lot.article} is {lot.category}, and this line is "
                           f"{line.category}")
        if not cls._same(lot.subtype, line.subtype):
            return False, (f"{lot.article} is a {lot.subtype}, and this line "
                           f"needs a {line.subtype}")
        if approved:
            return True, ""
        for field, label in (("article", "article"), ("colour", "colour"),
                             ("size", "size")):
            want = getattr(line, field, None)
            if want and not cls._same(getattr(lot, field, None), want):
                return False, (f"this line's {label} is {want} and the lot's is "
                               f"{getattr(lot, field, None) or 'blank'}")
        return True, ""

    async def match_packet(self, *, piece, lot) -> dict:
        """Which recipe line is THIS packet, for THIS garment? The store's scan.

        THE PACKET'S OWN LABEL IS THE ANSWER TO "WHICH LINE", and that is the
        reason the store no longer issues a whole kit on one scan. A blanket kit
        scan spends every accessory line at once from the recipe alone, so the
        system never learns which physical packets were opened — and the one
        mistake that costs a shipment, an M packet in an L jacket, is invisible to
        it by construction. A packet scan puts the physical label and the garment's
        size in the same comparison.

        MATCHED IN TWO TIERS, and the split is what makes a wrong size a wrong size
        instead of an unknown packet. Tier one ignores size entirely (category,
        subtype, article, colour, thickness), so the M packet still finds the
        BUTTON lines of this style. Tier two then asks the two size questions
        separately: does this line belong on this garment, and is this the material
        the line asked for.
        """
        _p, sku, style = await self._piece_context(piece.id)
        if style is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "This piece has no parent style.")
        garment_size = (getattr(sku, "size", None) or "").strip().upper() or None

        # SKU precedence WITHOUT the size filter: passing garment_size=None makes
        # applies_to_size pass everything, so a style-wide line still loses to its
        # per-colourway override while every size stays visible to tier two.
        merged = self.merge_lines(await self.repo.lines_for_style(style.id),
                                  sku.id if sku else None, None)
        accessories = [l for l in merged
                       if l.category == MaterialCategory.ACCESSORY.value]

        tier1 = [l for l in accessories
                 if self._same(l.subtype, lot.subtype)
                 and self._same(l.article, lot.article)
                 and (not l.colour or self._same(l.colour, lot.colour))
                 and (not l.thickness or self._same(l.thickness, lot.thickness))]
        out = {"outcome": PACKET_NOT_IN_RECIPE, "line": None,
               "garment_size": garment_size, "lot": lot,
               "candidates": [], "expected_sizes": [], "no_kit_reason": None}
        if not accessories:
            # NO RECIPE AT ALL is a different answer from "not this packet", and
            # _no_kit_reason is the sentence that already distinguishes the three
            # ways it happens. Saying "wrong packet" here would send the operator
            # hunting for a packet that was never specified.
            out["outcome"] = PACKET_NO_KIT
            out["no_kit_reason"] = await self._no_kit_reason(style, sku, piece)
            return out
        if not tier1:
            return out

        # THE TARGET LINE MUST BE ONE THIS GARMENT ACTUALLY HAS. issue_kit_nocommit
        # iterates the size-filtered recipe, so an override naming the M line for
        # an L garment would be silently ignored — the approval would appear to
        # work and spend nothing.
        applicable = [l for l in tier1 if self.applies_to_size(l, garment_size)]
        if not applicable:
            out["outcome"] = PACKET_NO_LINE_FOR_SIZE
            out["expected_sizes"] = sorted(
                {str(l.garment_size) for l in tier1 if l.garment_size})
            return out

        exact = [l for l in applicable
                 if not l.size or self._same(l.size, lot.size)]
        if len(exact) > 1:
            out["outcome"] = PACKET_AMBIGUOUS
            out["candidates"] = [str(l.id) for l in exact[:5]]
            return out
        if exact:
            out["outcome"] = PACKET_OK
            out["line"] = exact[0]
            return out

        if len(applicable) > 1:
            out["outcome"] = PACKET_AMBIGUOUS
            out["candidates"] = [str(l.id) for l in applicable[:5]]
            return out
        out["outcome"] = PACKET_WRONG_SIZE
        out["line"] = applicable[0]
        out["expected_sizes"] = [str(applicable[0].size)] if applicable[0].size else []
        return out

    # ══════════════════════════════════════════ the style/SKU merge
    @staticmethod
    def _reads_as_garment_size(value) -> bool:
        """Is this material `size` UNMISTAKABLY a garment size? Alpha rungs only.

        NARROWER THAN IT WAS, AND THE NARROWING IS THE FIX. This used to accept
        any bare number from 30 to 70 as a garment size, on the strength of the EU
        jacket ladder — so a 60cm zip entered as size '60' was read as a size-60
        garment, mapped to 4XL, and silently scoped to 4XL jackets only. Every
        other size then had no zip line at all, which does not mean a short kit:
        it means `kit_required` is False and the garment ships without a zip.

        'L' and 'XXL' cannot be anything but a garment size, so they still count —
        not to infer from any more, but to REFUSE the line until the DM says what
        they meant. A number stays ambiguous by nature and is never read here; the
        It is LEATHER AND LINING'S check now: accessories name their SKU, so the
        question cannot arise for them. A number stays ambiguous by nature and is
        never read here.
        """
        token = (str(value or "")).strip().upper()
        if not token or token.isdigit():
            return False
        if not token.isalnum():
            return False                  # '63 · YKK-169', '60CM/BLACK'
        from app.core.leather_norms import normalise_size
        # An alpha rung is short and normalises. '18L' does not normalise.
        return len(token) <= 5 and normalise_size(token) is not None

    @staticmethod
    def applies_to_size(line, garment_size: str | None) -> bool:
        """Does this recipe line belong on a garment of this size?

        NULL garment_size MEANS EVERY SIZE, and that is what makes this change
        back-compatible: every line that predates the column has NULL, so every
        already-released style resolves to exactly the recipe it resolved to
        before.

        THE RULE ITSELF LIVES IN kit_rules.size_matches, because the release gate's
        coverage check and the store's packet scan ask the same question and a
        second spelling of "is an L line an L garment's line" is a second answer.
        """
        return kit_rules.size_matches(getattr(line, "garment_size", None),
                                      garment_size)

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

        ACCESSORIES ARRIVE PRE-SCOPED and need none of the above. Every accessory
        line names a SKU, so there are no style-wide accessory lines to override and
        `garment_size` is always NULL on them — the sku_id filter alone already
        answers "is this accessory this garment's". The size machinery below is now
        leather/lining's, which can still be style-wide.

        THEIR KEY IS THE FULL MATERIAL IDENTITY, and that is not symmetry for its
        own sake. The leather/lining key deliberately ignores colour, because a NAVY
        knit line SHOULD override the style-wide knit line — same material, one
        colourway's version of it. Accessories have no overriding to do, so ignoring
        colour buys nothing and costs a real case: a two-tone garment taking BTN-4H
        in BLACK and in NAVY is two lines, and a key without colour would silently
        keep one of them.
        """
        style_lines = [l for l in lines if l.sku_id is None]
        sku_lines = [l for l in lines if sku_id is not None and l.sku_id == sku_id]

        def key(l):
            base = ((l.category or "").upper(), (l.subtype or "") or None,
                    (l.article or ""))
            if (l.category or "").upper() == MaterialCategory.ACCESSORY.value:
                return base + ((l.colour or "") or None,
                               (l.thickness or "") or None,
                               (l.size or "") or None)
            return base + ((getattr(l, "garment_size", None) or "") or None,)

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
                                 and (l.qty_per_piece or 0) > 0 for l in lines),
            **await self.sku_checks(style_id, lines))
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

    # ══════════════════════════════════════════════════ adding lines (+ fan-out)
    async def _ordered_skus(self, style_id: uuid.UUID) -> list:
        """The style's SKUs that were actually ordered, oldest first.

        `qty_ordered > 0` ON PURPOSE. An importer can leave a zero-quantity SKU row
        behind for a colour/size the client did not buy, and fanning an accessory
        onto it would put a line on the release gate's coverage check that no
        garment will ever need.
        """
        rows = (await self.db.execute(
            select(SKU).where(SKU.style_id == style_id,
                              func.coalesce(SKU.qty_ordered, 0) > 0)
            .order_by(SKU.color_code, SKU.size))).scalars().all()
        return list(rows)

    async def add_line(self, style_id: uuid.UUID, body: dict, *,
                       actor_name: str, actor_id=None) -> dict:
        """Add one recipe line, or FAN ONE OUT across a style's SKUs.

        WHY THE FAN-OUT LIVES HERE AND NOT ON THE PUT. `_assert_editable` lets
        ACCESSORIES be corrected after release and freezes everything else, but
        `replace_spec` asks it without a category, so the whole-grid PUT is frozen
        post-release. The wrong button is discovered precisely when somebody goes to
        fetch it, which is always after release — so the one door that must keep
        working is this one.

        WHY A FAN-OUT AT ALL. An accessory is SKU-scoped now, so a NAVY+PINE order in
        S/M/L/XL is 8 rows for one button. The floor says 85-90% of accessories are
        identical across a style's SKUs and only zip and rib knit trim vary, so the
        common case must be ONE call: `apply_to: "ALL_SKUS"`. The stored shape stays
        purely per-SKU either way — the convenience is in the request, never in the
        data, because a "applies to everything" row is exactly what this change
        removed.
        """
        style = await self._style(style_id)
        body = dict(body or {})
        self._assert_editable(style, category=body.get("category"))

        apply_to = (body.pop("apply_to", None) or "").strip().upper() or None
        sku_ids = body.pop("sku_ids", None) or None
        per_sku = body.pop("per_sku", None) or None

        # EXACTLY ONE SCOPE. Two of them cannot be reconciled, and silently
        # preferring one is how the wrong button ends up issued.
        chosen = [n for n, v in (("sku_id", body.get("sku_id")),
                                 ("apply_to", apply_to),
                                 ("sku_ids", sku_ids),
                                 ("per_sku", per_sku)) if v]
        if len(chosen) > 1:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"This line names its scope more than once ({', '.join(chosen)}). "
                f"Send exactly one of sku_id, apply_to, sku_ids or per_sku — which "
                f"garments an accessory is for is not something to be guessed at.")
        if apply_to and apply_to != "ALL_SKUS":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"apply_to must be \"ALL_SKUS\". To name a subset, send sku_ids.")

        # ── build the per-SKU bodies ─────────────────────────────────────────
        rows: list[dict] = []
        if apply_to == "ALL_SKUS":
            skus = await self._ordered_skus(style.id)
            if not skus:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"{style.name} has no ordered SKUs to apply this to. Upload the "
                    f"breakdown first — the quantities are what say which "
                    f"colourways and sizes exist.")
            rows = [dict(body, sku_id=s.id) for s in skus]
        elif sku_ids:
            rows = [dict(body, sku_id=sid) for sid in sku_ids]
        elif per_sku:
            for entry in per_sku:
                entry = entry if isinstance(entry, dict) else dict(vars(entry))
                if not entry.get("sku_id"):
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "every per_sku entry must name a sku_id.")
                row = dict(body, sku_id=entry["sku_id"])
                # A PER-SKU SIZE IS THE WHOLE POINT of this door: the zip is the
                # accessory whose size follows the garment. qty may differ too — a
                # bigger garment can take more thread.
                for field in ("size", "qty_per_piece", "colour", "thickness",
                              "material_lot_id", "note"):
                    if entry.get(field) is not None:
                        row[field] = entry[field]
                rows.append(row)
        else:
            rows = [body]

        # ── write them ───────────────────────────────────────────────────────
        # IDEMPOTENT, AND PARTIALLY SO. A fan-out of 8 that finds 3 already present
        # must add the other 5 and say so, not 409 the batch: re-posting after a
        # timeout is the normal way this endpoint is used twice.
        created, already = [], []
        for row in rows:
            d = await self._clean_line(style, row)
            dup = await self.repo.find_duplicate_line(
                style_id=style.id, sku_id=d["sku_id"], category=d["category"],
                subtype=d["subtype"], article=d["article"], colour=d["colour"],
                thickness=d["thickness"], size=d["size"],
                garment_size=d.get("garment_size"), active_only=False)
            if dup is not None:
                if not dup.is_active:
                    # Re-adding a line somebody removed revives it, rather than
                    # colliding with a row nothing can see.
                    dup.is_active = True
                    dup.qty_per_piece = d["qty_per_piece"]
                    created.append(dup)
                else:
                    already.append(dup)
                continue
            created.append(self.repo.add_line_nocommit(**d))

        # A SINGLE line keeps its original 409, because there is no batch to
        # partially accept and a silent no-op would read as success.
        if not created and len(rows) == 1 and already:
            dup = already[0]
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{style.name} already has a line for {dup.article}"
                f"{' · ' + dup.colour if dup.colour else ''}"
                f"{' on ' + await self._sku_label(dup.sku_id) if dup.sku_id else ''}"
                f". Edit that line (PATCH .../lines/{dup.id}) rather than adding a "
                f"second one — two lines for one material would both be issued.")

        await self.db.commit()
        for line in created:
            await self.db.refresh(line)

        # A single un-fanned add keeps its original single-object response, so
        # every existing client is unaffected.
        if len(rows) == 1 and not (apply_to or sku_ids or per_sku):
            return await self._line_payload(created[0])
        return {
            "created": len(created), "already_present": len(already),
            "lines": [await self._line_payload(l) for l in created],
            "message": (
                f"{len(created)} line(s) added for {style.name}"
                f"{f'; {len(already)} were already there' if already else ''}."),
        }

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
            # `garment_size` IS IN THIS DICT for the same reason every other field
            # is: a PATCH keeps what it was not told to change. Leaving it out did
            # NOT leave the column alone — _clean_line returns garment_size=None
            # when the body has no key for it, and the loop below writes every key
            # it returns. So patching a line's note silently un-scoped it, widening
            # a size-specific material to every garment, which is the direction that
            # SPENDS stock. On a line whose size was an alpha rung it 422'd instead,
            # making the line unpatchable at all. Accessories no longer use this
            # column, but leather and lining still do.
            "garment_size": getattr(line, "garment_size", None),
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
            has_leather_line=has_leather,
            **await self.sku_checks(style_id, lines))

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
        overrides are REPORTED in `skipped_detail` rather than guessed at. That is
        LEATHER AND LINING's rule, and it is unchanged.

        ACCESSORIES ARE COPIED BY FAN-OUT, and they had to be. Every accessory line
        names a SKU now, so under the rule above they would be copied only where the
        two styles happened to share a (colour, size) — and `include_sku_overrides`
        defaults to False, so the DEFAULT copy would have carried ZERO accessory
        lines. That silently guts the feature this method exists for.

        So an accessory is copied as a RECIPE FOR AN ARTICLE and fanned onto THIS
        style's own SKUs:

          · the accessory kind's `size_varies_by_sku` says which way to match. A
            button is the same on every garment, so one source line covers all of
            this style's SKUs. A zip follows the garment, so each source line lands
            only on the SKUs whose size matches its own.
          · the SOURCE's colourways are irrelevant to the target's. A button article
            copied from last season's jacket belongs on all of this one's SKUs
            regardless of what colours either style came in.

        The quantity per piece is carried across; a source line whose size matches
        none of this style's sizes is reported, not guessed.
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

        # THIS style's ordered SKUs, which is what an accessory is fanned onto.
        my_skus = await self._ordered_skus(style.id)

        copied, skipped_detail = 0, []
        for line in src_lines:
            is_accessory = ((line.category or "").upper()
                            == MaterialCategory.ACCESSORY.value)

            if is_accessory:
                if not my_skus:
                    skipped_detail.append({
                        "article": line.article, "scope": "ACCESSORY",
                        "reason": f"{style.name} has no ordered SKUs yet — upload "
                                  f"its breakdown before copying accessories."})
                    continue
                if await self.catalog.size_varies_by_sku(line.subtype):
                    # A zip follows the garment: this source line is for ONE size.
                    want = (line.size or getattr(line, "garment_size", None))
                    targets = [k for k in my_skus
                               if kit_rules.size_matches(want, k.size)]
                    if not targets:
                        skipped_detail.append({
                            "article": line.article, "scope": "ACCESSORY",
                            "reason": f"{line.article} is for size "
                                      f"{want or '(unspecified)'} and {style.name} "
                                      f"has no garments that size."})
                        continue
                else:
                    # A button is the same on every garment.
                    targets = list(my_skus)
            else:
                if line.sku_id is not None:
                    if not include_sku_overrides:
                        continue
                    mapped = sku_map.get(line.sku_id)
                    if mapped is None:
                        skipped_detail.append({
                            "article": line.article, "scope": "SKU",
                            "reason": f"{style.name} has no matching colour/size "
                                      f"for this override."})
                        continue
                    targets = [mapped]
                else:
                    targets = [None]

            for target in targets:
                target_sku = getattr(target, "id", target)
                dup = await self.repo.find_duplicate_line(
                    style_id=style.id, sku_id=target_sku, category=line.category,
                    subtype=line.subtype, article=line.article, colour=line.colour,
                    thickness=line.thickness, size=line.size,
                    garment_size=(None if is_accessory
                                  else getattr(line, "garment_size", None)))
                if dup is not None:
                    skipped_detail.append({"article": line.article,
                                           "reason": "Already on this style."})
                    continue
                self.repo.add_line_nocommit(
                    style_id=style.id, sku_id=target_sku, category=line.category,
                    subtype=line.subtype, article=line.article, colour=line.colour,
                    thickness=line.thickness, size=line.size,
                    # An accessory carries no garment_size — its SKU says the size.
                    garment_size=(None if is_accessory
                                  else getattr(line, "garment_size", None)),
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

        ACCESSORIES ARE AGGREGATED, and that is not cosmetic. They are SKU-scoped
        now, so one button on a NAVY+PINE order in S/M/L/XL is EIGHT rows of 4. A
        purchase order is raised off this screen and the DM needs "buy 1,600
        buttons", not eight rows of 200 to add up by hand — and `short_by` and the
        suggested supplier are only meaningful on the total, because stock is not
        reserved per SKU. So accessory lines are grouped by their material identity
        (subtype, article, colour, size), the group carries the summed requirement
        and the shortfall, and the per-SKU rows ride underneath it in `per_sku`.
        Leather and lining keep their existing one-row-per-line shape.
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
        # {material identity: the group payload}, in first-seen order.
        acc_groups: dict[tuple, dict] = {}

        for line in lines:
            is_accessory = ((line.category or "").upper()
                            == MaterialCategory.ACCESSORY.value)
            gkey = None
            if is_accessory:
                gkey = ((line.subtype or "") or None, line.article,
                        (line.colour or "") or None, (line.size or "") or None)

            # THE LOT IS RESOLVED ONCE PER GROUP, NOT ONCE PER LINE, and on this
            # screen that is the difference between a page and a stall.
            # `_line_payload` runs two queries — the lot match and its reservations
            # — so with accessories fanned across eight SKUs a six-accessory style
            # would issue ~96 of them where it used to issue ~12. Every line in a
            # group shares one material identity and therefore one lot, so the
            # first line's answer IS the group's answer.
            payload = None
            if not is_accessory or gkey not in acc_groups:
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
            if payload is not None:
                payload["pieces"] = pieces
                payload["total_required"] = float(required)

            if is_accessory:
                # GROUPED, NOT LISTED. See the docstring: eight rows of one button
                # is not a purchase order.
                group = acc_groups.get(gkey)
                if group is None:
                    group = {
                        "category": line.category, "subtype": line.subtype,
                        "article": line.article, "colour": line.colour,
                        "thickness": line.thickness, "size": line.size,
                        "uom": line.uom, "scope": "ACCESSORY_GROUP",
                        # The lot is the same for every SKU of one material, so the
                        # first line's resolution describes the group.
                        "resolution": payload.get("resolution"),
                        "lot": payload.get("lot"),
                        "candidate_lot_ids": payload.get("candidate_lot_ids", []),
                        "qty_per_piece": float(line.qty_per_piece or 0),
                        "pieces": 0, "total_required": 0.0,
                        "sku_count": 0, "per_sku": [],
                    }
                    acc_groups[gkey] = group
                group["pieces"] += pieces
                group["total_required"] += float(required)
                group["sku_count"] += 1
                group["per_sku"].append({
                    "line_id": str(line.id),
                    "sku_id": str(line.sku_id) if line.sku_id else None,
                    "sku_label": (await self._sku_label(line.sku_id)
                                  if line.sku_id else None),
                    "qty_per_piece": float(line.qty_per_piece or 0),
                    "pieces": pieces, "total_required": float(required),
                })
                # A GROUP WITH DIFFERING PER-PIECE QUANTITIES cannot report one, and
                # saying "4" when one SKU takes 6 would be worse than saying nothing.
                if group["qty_per_piece"] != float(line.qty_per_piece or 0):
                    group["qty_per_piece"] = None
                continue

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

        # THE SHORTFALL IS COMPUTED ON THE GROUP, because stock is not reserved per
        # SKU: one lot of buttons serves every colourway, so comparing each SKU's
        # share against the whole lot would report every one of them as covered while
        # the total was short.
        for group in acc_groups.values():
            required = Decimal(str(group["total_required"]))
            available = Decimal(str((group["lot"] or {}).get("available", 0)))
            short = required - available if group["lot"] else required
            group["short_by"] = float(short) if short > 0 else 0.0
            if short > 0:
                short_lines += 1
                supplier = await self.materials.suggest_supplier(group["article"])
                group["suggested_supplier"] = (
                    {"id": str(supplier.id), "name": supplier.name}
                    if supplier else None)
            else:
                group["suggested_supplier"] = None
            out_lines.append(group)

        blockers = kit_rules.release_blockers(
            style_name=style.name,
            confirmed_at=style.material_spec_confirmed_at,
            no_accessories=style.material_spec_no_accessories,
            has_accessory_lines=any(l.category == MaterialCategory.ACCESSORY.value
                                    for l in lines),
            has_leather_line=any(l.category == MaterialCategory.LEATHER.value
                                 and (l.qty_per_piece or 0) > 0 for l in lines),
            **await self.sku_checks(style_id, lines))
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

    # ══════════════════════════════════════════════ the size-coverage checks
    async def _ordered_sku_labels(self, style_ids: list[uuid.UUID]) -> dict:
        """{style_id: [(sku_id, "NAVY · L"), ...]} for the SKUs actually ordered.

        WHAT THE COVERAGE GATE NEEDS, and all it needs. It replaced a pair of
        helpers that assembled ordered SIZES and a {sku_id: size} map, because the
        question changed: an accessory line names its SKU, so "is every ordered
        garment covered" is asked of SKUs directly and no size arithmetic is
        involved at all.

        `qty_ordered > 0` ON PURPOSE. An importer can leave a zero-quantity row for
        a colour/size nobody bought, and blocking a release over a garment that will
        never be made is a false blocker — which teaches people the gate is noise.

        The label is what the DM reads in the blocker sentence, so it is built the
        same way _sku_label builds it.
        """
        if not style_ids:
            return {}
        rows = (await self.db.execute(
            select(SKU.id, SKU.style_id, SKU.color_name, SKU.color_code, SKU.size)
            .where(SKU.style_id.in_(list(style_ids)),
                   func.coalesce(SKU.qty_ordered, 0) > 0))).all()
        out: dict = {}
        for sku_id, style_id, colour_name, colour_code, size in rows:
            colour = (colour_name or colour_code or "?").strip()
            label = f"{colour} · {size}" if size else colour
            out.setdefault(style_id, []).append((sku_id, label))
        return {k: sorted(v, key=lambda p: p[1]) for k, v in out.items()}

    @staticmethod
    def _coverage_dicts(lines) -> list[dict]:
        """Recipe lines as the plain dicts kit_rules' pure coverage check takes."""
        return [{
            "category": line.category, "subtype": line.subtype,
            "article": line.article, "sku_id": line.sku_id,
            "qty_per_piece": float(line.qty_per_piece or 0),
        } for line in lines]

    async def sku_checks(self, style_id: uuid.UUID, lines: list) -> dict:
        """The coverage kwarg release_blockers takes, for ONE style.

        Returned as a dict so every call site spreads it — `**await
        self.sku_checks(...)` — rather than each one remembering the argument name.
        There are four of them and the gate is only a gate if all four ask.
        """
        labels = (await self._ordered_sku_labels([style_id])).get(style_id, [])
        return {
            "skus_missing_accessories": kit_rules.skus_without_accessories(
                lines=self._coverage_dicts(lines), ordered_skus=labels),
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
        # ONE query for every style's ordered SKUs, for the same reason the lines are
        # batched: a release names a dozen styles and the gate must not become a
        # dozen more round trips.
        labels_by_style = await self._ordered_sku_labels([s.id for s in rows])
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
                    and (l.qty_per_piece or 0) > 0 for l in lines),
                skus_missing_accessories=kit_rules.skus_without_accessories(
                    lines=self._coverage_dicts(lines),
                    ordered_skus=labels_by_style.get(style.id, [])))
        return out

    async def kit_required_for_piece(self, piece_id: uuid.UUID) -> bool:
        """Does the garment behind this piece take any accessories at all?

        THERE IS NO SQL HALF ANY MORE. It asked the question per STYLE while this
        asks it per SKU, so the two diverged on every style whose colourways differ —
        and nothing called it. See the note where it used to live in
        StyleSpecRepository. If a batched form is needed, it must key on SKU; a
        style-level EXISTS would report a kit owed on a garment that needs none.

        FALSE for every style that predates the spec feature, which is what makes the
        completeness predicate collapse to its pre-existing form for everything
        already on the floor.
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
        # THE EMPTY SHAPE MUST CARRY EVERY KEY THE FULL ONE DOES. A screen renders
        # whichever it is handed, and `kit_view` reads these straight through — a
        # missing key here is a KeyError on the store scan of any garment whose style
        # predates the spec, which is most of what is already on the floor.
        empty = {"kit_status": KitStatus.NOT_REQUIRED.value, "kit_required": False,
                 "spec_confirmed": False, "summary_line": None,
                 "leather": None, "lining": None, "accessories": [],
                 "accessories_scanned": [], "accessories_pending": [],
                 "accessories_progress": {
                     "declared": 0, "scanned": 0, "pending": 0,
                     "required_total": 0.0, "issued_total": 0.0, "unresolved": 0},
                 "pending_line": None}
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
                # `state` AND `label` ON EVERY LINE, so no screen has to re-derive
                # either. accessories_in is a roll-up and cannot say WHICH packet is
                # missing; this is the per-line answer, in the same four words
                # wherever it is read (kit_rules.accessory_line_state).
                accessories.append(dict(
                    base, issued_qty=got,
                    outstanding=max(0.0, round(need - got, 3)),
                    state=kit_rules.accessory_line_state(
                        qty_per_piece=need, issued_qty=got,
                        resolvable=resolution not in (RESOLUTION_NONE,
                                                      RESOLUTION_AMBIGUOUS)),
                    label=kit_rules.accessory_label(base),
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

        # THE THREE-WAY SPLIT, COMPUTED ONCE HERE. Every surface that answers "what
        # does this garment need, what has been scanned, what is still pending"
        # reads it from this one place — the scan response, the garment lookup and
        # the store list — so the three can never disagree about which packet is
        # outstanding. Doing it in each of them is three implementations of one
        # subtraction, and the store screen had already grown its own.
        scanned = [a for a in accessories if a["outstanding"] <= 0]
        pending = [a for a in accessories if a["outstanding"] > 0]
        return {
            "kit_status": kit_rules.kit_status(
                kit_required=bool(accessories), required_total=required_total,
                issued_total=issued_total, unresolved=unresolved),
            "kit_required": bool(accessories),
            "spec_confirmed": style.material_spec_confirmed_at is not None,
            "summary_line": self._summary_line(accessories),
            "leather": leather, "lining": lining, "accessories": accessories,
            # DECLARED / SCANNED / PENDING, spelled out rather than implied by a
            # boolean. `accessories_in` says only "all of them or not".
            "accessories_scanned": scanned,
            "accessories_pending": pending,
            "accessories_progress": {
                "declared": len(accessories),
                "scanned": len(scanned),
                "pending": len(pending),
                "required_total": round(required_total, 3),
                "issued_total": round(issued_total, 3),
                "unresolved": unresolved,
            },
            "pending_line": self._pending_line(scanned, pending),
        }

    @staticmethod
    def _pending_line(scanned: list, pending: list) -> str | None:
        """"Waiting for ZIP · YKK-60 BLACK. Scanned: BUTTON · HORN-4H, THREAD · T40."

        THE SENTENCE THE OPERATOR IS OWED. They have just scanned two of three
        packets and the response has to tell them, in words, which one is still
        missing — not hand them a boolean and a list to diff. Null when the style
        declares no accessories, so the screen renders nothing rather than "waiting
        for nothing".
        """
        if not scanned and not pending:
            return None
        if not pending:
            return ("All accessories scanned: "
                    + ", ".join(a["label"] for a in scanned) + ".")
        out = "Waiting for " + ", ".join(a["label"] for a in pending) + "."
        if scanned:
            out += " Scanned: " + ", ".join(a["label"] for a in scanned) + "."
        return out

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
                f"would put the wrong colour in the bag. Add the lines for THIS "
                f"SKU — POST /styles/{style.id}/material-spec/lines with its "
                f"`sku_id`, or with apply_to: \"ALL_SKUS\" to cover every "
                f"colourway at once. (An accessory cannot be made style-wide any "
                f"more: a SKU is what says which colour and size it is for.) "
                f"GET /store/pieces/{piece.code}/materials shows exactly which "
                f"lines reach this garment and which do not.")
        if by_reason.get("other_size"):
            # LEGACY ROWS ONLY. An accessory line carries no garment_size any more —
            # its SKU says the size — so this can only be reached by a row written
            # before that rule, and the fix is to re-enter it against its SKU rather
            # than to go on scoping accessories two different ways.
            blocked = by_reason["other_size"]
            sizes = sorted({str(getattr(l, "garment_size", "")) for l in blocked})
            return (
                f"{style.name}'s {len(blocked)} accessory line(s) are scoped to "
                f"garment size(s) {', '.join(s for s in sizes if s)}, and "
                f"{piece.code} is a {size or 'garment of unknown size'}. Those are "
                f"lines from before accessories were scoped per SKU. Re-enter them "
                f"against their SKUs — POST /styles/{style.id}/material-spec/lines "
                f"with apply_to: \"ALL_SKUS\", or per_sku for one whose size "
                f"follows the garment.")
        if by_reason.get("zeroed"):
            return (
                f"{style.name} declares that {mine} takes none of its "
                f"{len(acc)} accessory line(s) — every one of them is set to "
                f"qty_per_piece 0 for this colourway, which is how a SKU-scoped "
                f"line says 'not this one'. There is nothing to kit for "
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
                # A NAMED LOT IS STILL CHECKED. See lot_fits_line: this path used
                # to spend whatever id it was handed, which is how the wrong size
                # became recordable as the right one.
                fits, why = self.lot_fits_line(
                    lot, line,
                    approved=bool(override.get("substitution_approved")))
                if not fits:
                    unresolved.append(dict(
                        self._kit_line(line, float(owed)),
                        reason=RESOLUTION_MISMATCH, candidate_lot_ids=[],
                        note=(f"The lot named for {line.article} was not issued "
                              f"because {why}. Scan the packet the line asks for, "
                              f"or have a DM approve the substitution.")))
                    continue
                resolution = RESOLUTION_PINNED
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
            # THE WHOLE CHECKLIST, THREE WAYS. `declared` is every line the SKU's
            # recipe names, `scanned` the ones fully issued, `pending` what is still
            # owed — so the operator at the terminal reads the answer instead of
            # diffing two lists. All three come from material_requirement_block, so
            # this view and the garment lookup cannot drift.
            "declared": block["accessories"],
            "scanned": block["accessories_scanned"],
            "pending": block["accessories_pending"],
            "progress": block["accessories_progress"],
            "pending_line": block["pending_line"],
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
