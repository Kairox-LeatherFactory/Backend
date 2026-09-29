"""
================================================================================
modules/store/service.py — leather + lining + kit, merged on the garment
================================================================================
WHY THE DRAWER WENT AWAY

    There were 200 physical drawers. A style releases 100+ garments, stalls
    partway down the chain, and the next 50 have nowhere to go. Re-allocating by
    hand was complicated enough that in practice it did not happen, so pieces sat
    on a "waiting for a drawer" list instead of moving — and the merge gate then
    refused to line-stitch them, because a piece with no drawer could not be
    proven complete.

    The drawer earned none of that. Every fact it held was a fact about the
    GARMENT: its leather arrived, its lining arrived, its kit was issued, it was
    confirmed, it was released. Those now live on the piece, and the store stops
    being a place with a fixed number of slots.

WHAT THE OPERATOR DOES NOW
    Scan the worker. Scan the garment. That is the whole flow.

    The old one was employee → DRAWER → piece, with a 409 when the garment was
    not the one that drawer had been assigned at upload. That third scan existed
    only to find a numbered box, and the "wrong drawer" rejection existed only to
    police an assignment the system had invented. Both are gone.

WHAT IS DELIBERATELY UNCHANGED
    · the completeness rule          core/kit_rules.piece_complete
    · the auto-receive rule          core/kit_rules.auto_receive_ready
    · the store-entry gate           core/store_display.STORE_ENTRY_STAGE
    · the accessory kit and ledger   StyleSpecService.issue_kit_nocommit
    · the lining verdict             core/lining_rules

    All four were already pure functions over garment facts, which is the reason
    this refactor is a relocation and not a rewrite. Rewriting them would have
    put the store's hardest-won rules — the stale needs_lining flag, the
    auto-receive that fired on an empty lining side — back at risk.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import kit_rules, lining_rules
from app.core.enums import (
    BarcodeAuditAction, ProductionStage, StorePart, StoreState,
)
from app.core.store_display import STORE_ENTRY_STAGE, holding_label
from app.modules.production.models import Piece


class StoreService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ══════════════════════════════════════════════════════════ lookups
    async def get_piece(self, piece_id: uuid.UUID) -> Piece | None:
        """Read a piece. For a READ. If you are about to change its store state,
        use get_piece_for_update instead."""
        return await self.db.get(Piece, piece_id)

    async def get_piece_for_update(self, piece_id: uuid.UUID) -> Piece | None:
        """Read a piece with the row LOCKED for the rest of the transaction.

        EVERY STORE TRANSITION IS A READ-MODIFY-WRITE: read store_state, decide
        what the next state is, write it. Two store operators scanning the same
        garment at the same instant — the leather into one terminal, the lining
        into another — both read HOLDING_NONE, both compute their own single-part
        state, and the second write wins. The garment ends up recorded as holding
        one part when it physically holds both, and the merge gate then blocks
        line-stitching on a piece that is actually complete.

        SELECT ... FOR UPDATE serialises the two scans on the row, so the second
        one reads the first one's result and correctly lands on HOLDING_BOTH.

        SQLite ignores row locks, which is fine — the tests are single-writer.
        This protects Postgres, where two terminals on a factory floor really are
        concurrent.
        """
        return await self.db.get(Piece, piece_id, with_for_update=True)

    async def _context(self, piece):
        from app.modules.clients.models import SKU, Style
        sku = await self.db.get(SKU, piece.sku_id) if piece.sku_id else None
        style = await self.db.get(Style, sku.style_id) if sku else None
        return sku, style

    async def needs_lining(self, piece) -> tuple:
        """Does this garment take a lining, and why? The stored flag is EVIDENCE.

        `piece.needs_lining` is written once at upload and never recomputed; on
        the live database two orders holding the SAME 17 styles came out 0-flagged
        and 925-flagged. A garment whose flag said False was "complete" on its
        leather alone, received, sent, and walked to PACKAGE_EXPORT having never
        had a lining cut. So the flag is one signal among five and only ever ADDS
        a requirement — the DM's explicit declaration at release is the one thing
        that can remove one.
        """
        if piece is None:
            return True, "the store holds no identified garment"
        sku, style = await self._context(piece)
        has_cut = await self._has_lining_cut(piece.id)
        kwargs = dict(
            stored_flag=getattr(piece, "needs_lining", None),
            style_name=getattr(style, "name", None),
            style_article=getattr(style, "article", None),
            colour_values=(getattr(sku, "knit_color", None),
                           getattr(sku, "nylon_color", None)),
            has_lining_cut_event=has_cut,
            explicit=getattr(style, "needs_lining", None))
        return (lining_rules.lining_required(**kwargs),
                lining_rules.why_lining_required(**kwargs))

    async def needs_lining_map(self, piece_ids: list) -> dict:
        """The batch form — production reads it for a whole scan at once."""
        out = {}
        for pid in piece_ids or []:
            piece = await self.db.get(Piece, pid)
            if piece is not None:
                out[pid] = (await self.needs_lining(piece))[0]
        return out

    async def _has_lining_cut(self, piece_id) -> bool:
        from app.modules.production.repository import ProductionRepository
        repo = ProductionRepository(self.db)
        op = await repo.get_operation_by_code(ProductionStage.LINING_CUTTING.value)
        if op is None:
            return False
        return await repo.has_event_at_op(piece_id, op.id)

    async def _kit_required(self, piece) -> bool:
        from app.modules.materials.style_spec_service import StyleSpecService
        try:
            return await StyleSpecService(self.db).kit_required_for_piece(piece.id)
        except Exception:
            # A style with no spec — every style released before the spec feature
            # — requires no kit. Failing open here is what keeps those garments
            # sendable exactly as they were.
            return False

    # ══════════════════════════════════════════════════════════ the scan
    async def _completed_stages(self, piece_id) -> set:
        from app.modules.production.repository import ProductionRepository
        return set(await ProductionRepository(self.db).completed_stage_codes(piece_id))

    async def infer_part(self, piece, done: set) -> StorePart:
        """LEATHER or LINING — never ACCESSORY. See StorePart for why.

        The order is "what does the evidence say", then "what is missing":
          1. a LINING_CUTTING event and no lining in → that lining arriving
          2. PASTING and no leather in               → the leather
          3. neither ready                            → fill the empty side, so
             the entry gate can reject it by NAME rather than arbitrarily
          4. both already in                          → 409, nothing left to put

        Rule 2 asks for PASTING, not "any leather-side event". Accepting
        LEATHER_CUTTING or FUSING is how a piece that was cut but not pasted got
        bucketed as leather and stored, and the merge gate then opened on it.
        """
        lining_done = ProductionStage.LINING_CUTTING.value in done
        pasted = ProductionStage.PASTING.value in done

        if lining_done and not piece.lining_in:
            return StorePart.LINING
        if pasted and not piece.leather_in:
            return StorePart.LEATHER
        if not piece.leather_in:
            return StorePart.LEATHER
        if not piece.lining_in:
            return StorePart.LINING
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{piece.code} already has both its leather and its lining in the "
            f"store. There is nothing left to scan in.")

    def _assert_ready_for_store(self, piece, part: StorePart, done: set) -> None:
        """A part may only enter the store once its own side is finished.

        THE WRITE GATE, not just the read overlay. display_stage() would not SHOW
        a piece as in-store until PASTING or LINING_CUTTING, while the old store
        scan accepted any merged piece at any time — so leather could be scanned
        in straight off the breakdown upload, before it was cut. The store
        believed it, the merge gate opened on it, and the garment reached
        LINE_STITCHING having never been pasted.
        """
        if part is StorePart.ACCESSORY:
            return                    # a kit is issued, not cut; nothing to wait for
        required = STORE_ENTRY_STAGE.get(part.value)
        if required and required not in done:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{piece.code} cannot be stored as {part.value.lower()} yet — "
                f"{required} has not been logged. Log it first; the store is "
                f"where a finished part is parked, not where an unfinished one "
                f"waits.")

    async def store_scan(self, *, piece_id, employee_id, part=None,
                         lot_id=None, lot_ids=None, qty=None,
                         substitution_reason=None, actor_user_id=None,
                         entered_by: str | None = None) -> dict:
        """Put one part of one garment into the store. ONE transaction.

        The stock movements, the ledger rows, the piece's flags and the audit row
        all land together or not at all — a half-issued kit is worse than an
        unissued one, because nothing downstream can tell them apart.

        AN ACCESSORY NEEDS ITS PACKET'S OWN LABEL — `lot_id` — and there is no
        longer any way to issue a whole kit from the recipe in one tap. See
        _issue_packet for why that blanket scan had to go.
        """
        piece = await self.get_piece_for_update(piece_id)
        if piece is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Piece not found.")
        if piece.store_state == StoreState.SENDED.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{piece.code} has already been sent to line-stitching. It is "
                f"out of the store; nothing more can be scanned into it.")

        done = await self._completed_stages(piece.id)
        # ONE LIST FROM HERE DOWN. A single `lot_barcode` is a one-element batch, so
        # the scan-gun door and the batch door run the same code and cannot drift on
        # what a packet means or which problems are fatal.
        packets = list(lot_ids or ([lot_id] if lot_id is not None else []))
        # A PACKET LABEL CAN ONLY MEAN ONE THING, so scanning one is the same as
        # saying part=ACCESSORY. `inferred` stays False: the operator chose this by
        # scanning a third barcode, which is not the system guessing.
        inferred = part is None and not packets
        if packets:
            chosen = StorePart.ACCESSORY
            if part is not None:
                asked = str(getattr(part, "value", part)).strip().upper()
                if asked != StorePart.ACCESSORY.value:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        f"An accessory packet was scanned but this scan says "
                        f"part={asked}. Leather and lining are not issued from a "
                        f"lot label; drop `part` or send ACCESSORY.")
        elif part is None:
            chosen = await self.infer_part(piece, done)
        else:
            try:
                # `getattr(part, "value", part)` FIRST, and it is not decoration.
                # StorePart subclasses str, but since Python 3.11 str() on an
                # enum member returns "StorePart.LEATHER", not "LEATHER" — so a
                # caller passing the enum (which every internal caller naturally
                # does) got 'STOREPART.LEATHER' and a 422 telling them to send
                # one of the three values they had just sent.
                chosen = StorePart(str(getattr(part, "value", part)).strip().upper())
            except ValueError:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "part must be LEATHER, LINING or ACCESSORY.")
            if chosen is StorePart.ACCESSORY:
                # THE BLANKET KIT SCAN IS GONE. It spent every accessory line the
                # recipe named from one tap, so the system never learned which
                # physical packets were opened — and an M packet in an L jacket was
                # invisible to it by construction.
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Scan the accessory packet's own label. Accessories are "
                    f"issued packet by packet — the buttons, then the zip, then "
                    f"the thread — because the label is what proves the right size "
                    f"went into {piece.code}. Send `lot_barcode` for one, or "
                    f"`lot_barcodes` for several in a single call.")
        self._assert_ready_for_store(piece, chosen, done)

        now = datetime.now(timezone.utc)
        kit = None
        substitution = None
        batch = None
        warnings: list[str] = []

        if chosen is StorePart.ACCESSORY:
            kit, substitution, batch = await self._issue_accessories(
                piece=piece, lot_ids=packets, qty=qty, employee_id=employee_id,
                entered_by=entered_by, actor_user_id=actor_user_id,
                reason=substitution_reason)
            piece.accessories_in = bool(kit["complete"])
        elif chosen is StorePart.LEATHER:
            piece.leather_in = True
        else:
            piece.lining_in = True

        if piece.store_entered_at is None:
            piece.store_entered_at = now

        needs_lining, lining_reason = await self.needs_lining(piece)
        kit_required = await self._kit_required(piece)

        # ── the state, derived from what is physically in ────────────────────
        if piece.leather_in and piece.lining_in:
            piece.store_state = StoreState.HOLDING_BOTH.value
        elif piece.leather_in:
            piece.store_state = StoreState.HOLDING_LEATHER.value
        elif piece.lining_in:
            piece.store_state = StoreState.HOLDING_LINING.value
        else:
            # An accessory scan before either part arrived. The kit is recorded
            # (and its stock spent) but the garment is not holding anything yet.
            piece.store_state = StoreState.MERGED.value

        # ── auto-receive: STRICTER than completeness, and that is scar tissue ─
        # It first fired on `piece_complete`, which let a garment flagged
        # needs_lining=False jump to RECEIVED the moment its leather was stored —
        # over an empty lining side, on a flag known to be wrong for 925 of 1,425
        # pieces in one live order. Two booleans set by two physical scans are not
        # a guess, so it asks for both parts literally present.
        if kit_rules.auto_receive_ready(
                leather_in=piece.leather_in, lining_in=piece.lining_in,
                accessories_in=piece.accessories_in, kit_required=kit_required):
            piece.store_state = StoreState.RECEIVED.value
            piece.store_received_at = now
            await self._audit(actor_user_id, BarcodeAuditAction.DRAWER_RECEIVED,
                              piece.id, {"piece": piece.code,
                                         "state": piece.store_state,
                                         "by_employee": str(employee_id)})

        complete = kit_rules.piece_complete(
            leather_in=piece.leather_in, lining_in=piece.lining_in,
            accessories_in=piece.accessories_in, needs_lining=needs_lining,
            kit_required=kit_required)

        # THE KIT RIDES ON EVERY SCAN, not only the accessory one. The operator
        # standing there with the lining in his hand is the person best placed to
        # fetch the buttons too, and he will not go and look up a second screen
        # to find out they are owed. An ACCESSORY scan returns what it just
        # issued; any other scan returns the current checklist.
        if kit is None:
            try:
                from app.modules.materials.style_spec_service import StyleSpecService
                kit = await StyleSpecService(self.db).kit_view(piece.id)
            except Exception:
                # A style with no spec has no kit to show. It must not cost the
                # operator the scan he just made.
                kit = None

        if chosen is StorePart.ACCESSORY and kit and kit.get("unresolved"):
            warnings.append(
                f"{len(kit['unresolved'])} accessory line(s) matched no lot and "
                f"were not issued — the garment cannot be sent until they are.")

        await self.db.commit()
        # THE DASHBOARDS ARE NOW STALE — say so immediately.
        #
        # Bust the read cache in the same breath as the commit, not on a timer.
        # A cutting manager who logs a cut expects to see it on the dashboard at
        # once; a minute of TTL reads on the floor as "the system lost my scan",
        # and the operator scans again. One INCR, and every cached aggregate
        # becomes unreachable. No-op when caching is off or Redis is away.
        from app.core.cache import invalidate as _invalidate_read_cache
        await _invalidate_read_cache()
        # WHAT IS STILL OWED, as a list the screen can render without recomputing
        # the completeness rule for itself. A second implementation of "what is
        # this garment waiting for" is a second implementation that can disagree.
        awaiting = []
        if not piece.leather_in:
            awaiting.append("LEATHER")
        if needs_lining and not piece.lining_in:
            awaiting.append("LINING")
        if kit_required and not piece.accessories_in:
            # "ACCESSORIES", plural, because this is the human list of what is
            # still owed and it is what the store screen already renders.
            awaiting.append("ACCESSORIES")

        return {
            "piece_code": piece.code,
            "store_state": piece.store_state,
            "awaiting": awaiting,
            # True when THIS scan completed the garment and it received itself.
            "auto_received": (piece.store_state == StoreState.RECEIVED.value
                              and piece.store_received_at == now),
            # The garment holds everything it will ever get. Named for what it
            # means rather than for the button it used to enable.
            "ready_for_received": complete,
            # Has it left the store yet? Distinct from `complete`: a garment can
            # hold everything it will ever get and still be sitting on the shelf.
            "sent": piece.store_state == StoreState.SENDED.value,
            "holding": holding_label(leather_in=piece.leather_in,
                                     lining_in=piece.lining_in),
            "leather_in": piece.leather_in, "lining_in": piece.lining_in,
            "accessories_in": piece.accessories_in,
            "part_stored": chosen.value, "part_inferred": inferred,
            "complete": complete, "needs_lining": needs_lining,
            "lining_reason": lining_reason, "kit": kit,
            # Set only when this scan SPENT a DM-approved wrong-size packet, so
            # the screen can say so out loud rather than looking like a clean
            # issue. On a SINGLE-packet scan a refused substitution never reaches
            # here — it is a 409; in a batch it appears in accessory_batch.refused.
            "substitution": substitution,
            # PER-PACKET OUTCOMES, null for a leather/lining scan so the existing
            # response shape is untouched. `issued` vs `scanned` is what a screen
            # shows when a tray was half good, and `still_owed` is what the garment
            # is waiting for before it can leave the store.
            "accessory_batch": batch,
            "next_action": self._next_action(piece, complete, needs_lining,
                                             kit_required),
            "warnings": warnings,
        }

    # ════════════════════════════════════════════════ the accessory packet
    async def _issue_packet(self, *, piece, lot_id, qty, employee_id,
                            entered_by, actor_user_id, reason,
                            collect: bool = False) -> tuple:
        """Issue ONE accessory packet into one garment.

        Returns `(substitution, record)` — `record` describing what happened to this
        packet, so a batch can report per-packet outcomes.

        `collect` DECIDES WHETHER A PROBLEM IS AN EXCEPTION OR A VALUE, and both
        callers need it to be one or the other:

          · a SINGLE-packet scan (`collect=False`) raises the 422/409 exactly as it
            always has — that is the documented contract and what the floor's
            one-beep-one-request screen reads.
          · a BATCH (`collect=True`) cannot raise, because an exception would abort
            the packets that were fine. One bad packet must never lose the good ones
            scanned with it — the rule `send()` and the production log's per-piece
            gates already follow.

        WHY ONE PACKET AND NOT THE WHOLE KIT. The blanket scan read the recipe and
        decremented every accessory line it named — four buttons, one zip, ten
        metres of thread — from a single tap on the garment. Nothing in that
        exchange involved the physical packets, so the system could not tell an L
        zip from an M one and never asked. The floor's own control is the label on
        the packet the operator is holding; scanning it is what lets the two sizes
        be compared at all.

        THE WRONG SIZE IS REFUSED, NOT FLAGGED. Everything else in this app that
        finds a problem mid-scan records the work and warns — the skill gate, the
        stock shortfall — because the work physically happened and losing the
        record is worse. This is the exception, and the asymmetry is deliberate: an
        M button in an L jacket is discovered by the client in Dubai, and the bill
        is return freight plus a remade garment. There is nothing to preserve by
        recording it, so the scan fails and a DM/MD decides.
        """
        from app.core.enums import KitSubstitutionStatus
        from app.modules.materials.repository import MaterialRepository
        from app.modules.materials import style_spec_service as spec_mod
        from app.modules.materials.style_spec_service import StyleSpecService

        # ONE PLACE THAT DECIDES raise-or-report, so the two doors cannot drift on
        # which problems they consider fatal.
        def refuse(code: int, detail: str, reason_code: str,
                   headers: dict | None = None, request_id: str | None = None):
            if not collect:
                raise HTTPException(code, detail, headers=headers)
            return {"ok": False, "reason": reason_code, "detail": detail,
                    "status_code": code, "lot_id": str(lot_id),
                    "packet": _label[0], "substitution_request_id": request_id}

        # A provisional label, so a refusal about the LOT ITSELF can still name it.
        _label = [str(lot_id)]

        lot = await MaterialRepository(self.db).get_lot(lot_id)
        if lot is None:
            return None, refuse(
                status.HTTP_404_NOT_FOUND,
                "That accessory packet is not a known lot.", "UNKNOWN_LOT")
        _label[0] = self._lot_label(lot)
        if str(lot.category or "").upper() != "ACCESSORY":
            return None, refuse(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{lot.article} is {lot.category}, not an accessory. Leather and "
                f"lining are consumed at the cut, not issued in the store.",
                "NOT_AN_ACCESSORY")
        if not lot.is_active:
            return None, refuse(
                status.HTTP_409_CONFLICT,
                f"Lot {lot.article} has been retired — its label should not be on "
                f"a packet in use. Scan the current packet.", "RETIRED_LOT")

        specs = StyleSpecService(self.db)
        match = await specs.match_packet(piece=piece, lot=lot)
        outcome, line = match["outcome"], match["line"]
        label = self._lot_label(lot)

        if outcome == spec_mod.PACKET_NO_KIT:
            # LOUD, NOT SILENT, and it must say WHICH "no" — the style declares
            # none, or its lines are all scoped to another colourway or size. A
            # cheerful 201 here would have the operator believe they issued a kit
            # that does not exist.
            return None, refuse(status.HTTP_409_CONFLICT,
                                match["no_kit_reason"], "NO_KIT")
        if outcome == spec_mod.PACKET_NOT_IN_RECIPE:
            return None, refuse(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{piece.code}'s recipe does not name {label}. Check the packet, "
                f"or record it as an off-spec issue with POST /materials/issues if "
                f"it genuinely went into this garment.", "NOT_IN_RECIPE")
        if outcome == spec_mod.PACKET_NO_LINE_FOR_SIZE:
            # The release-gate coverage hole, showing up on the floor. A DM has to
            # fix the RECIPE; approving one packet would leave every other garment
            # of this size in the same state.
            expected = ", ".join(match["expected_sizes"]) or "other sizes"
            return None, refuse(
                status.HTTP_409_CONFLICT,
                f"{piece.code} is a {match['garment_size'] or 'no-size'} garment "
                f"and this style's {label} lines are only for {expected} — there "
                f"is no line for this size at all. The recipe needs a line for "
                f"{match['garment_size'] or 'this size'} before any of these "
                f"garments can be kitted.", "NO_LINE_FOR_SIZE")
        if outcome == spec_mod.PACKET_AMBIGUOUS:
            return None, refuse(
                status.HTTP_409_CONFLICT,
                f"{label} matches {len(match['candidates'])} lines of "
                f"{piece.code}'s recipe, so this scan cannot tell which one it is "
                f"issuing. Have the recipe's duplicate lines combined or scoped.",
                "AMBIGUOUS")

        substitution = None
        approved_row = None
        if outcome == spec_mod.PACKET_WRONG_SIZE:
            row = await self._substitution_row(piece.id, line.id, lot.id)
            if row is not None and row.status == KitSubstitutionStatus.APPROVED.value:
                approved_row = row
            else:
                held = await self._hold_for_substitution(
                    row=row, piece=piece, line=line, lot=lot, label=label,
                    garment_size=match["garment_size"], qty=qty,
                    employee_id=employee_id, entered_by=entered_by,
                    actor_user_id=actor_user_id, reason=reason,
                    commit=not collect)
                return None, refuse(
                    status.HTTP_409_CONFLICT, held["detail"], held["reason"],
                    headers={"X-Kit-Substitution-Request": held["request_id"]},
                    request_id=held["request_id"])

        request = {"spec_id": str(line.id), "material_lot_id": lot.id,
                   "substitution_approved": approved_row is not None}
        if qty is not None:
            request["qty"] = qty
        issued = await specs.issue_kit_nocommit(
            piece=piece, requested_lines=[request],
            employee_id=employee_id, entered_by=entered_by)

        # FLUSH, THEN READ. `issue_kit_nocommit` adds the ledger row WITHOUT
        # committing — the scan owns its single commit — and sessions here are
        # `autoflush=False` (core/database.py). So the SELECT inside `kit_view`
        # could not see the row just written: it read 0 issued, `complete` came back
        # False, `accessories_in` was persisted False, and a garment needing three
        # accessories could never be completed however many times it was scanned.
        # LINE_STITCHING is gated on SENDED, which needs that flag, so the floor hit
        # a wall that looked like "it only lets me scan one accessory".
        #
        # IT IS ALSO WHAT MAKES A BATCH IDEMPOTENT. `issue_kit_nocommit` opens with
        # `issued_by_piece`, its idempotency read; without a flush per packet, the
        # same packet twice in one request would miss its own first row and spend
        # the quantity twice.
        #
        # Nothing is committed here. The unique constraint on
        # (piece_id, spec_line_id) now bites at flush rather than at commit, which
        # is better: a true concurrent double-tap raises where this code can still
        # reason about it, and still rolls the whole scan back.
        await self.db.flush()

        if approved_row is not None:
            # CONSUMED ONLY WHEN THE LINE IS ACTUALLY SATISFIED. Marking it spent up
            # front broke the short issue: two of four buttons went in, the approval
            # was used up, and the remaining two could not be issued from the same
            # packet — while the CONSUMED branch told the operator nothing was owed,
            # which was flatly untrue. The approval covers "this packet into this
            # garment", and a part-issue has not finished that.
            still_owed = any(r.get("spec_id") == str(line.id)
                             for r in issued["outstanding"])
            if not still_owed:
                approved_row.status = KitSubstitutionStatus.CONSUMED.value
            substitution = {
                "request_id": str(approved_row.id),
                "approved_by": approved_row.decided_by,
                "garment_size": approved_row.garment_size,
                "lot_size": approved_row.lot_size,
                "note": approved_row.decision_note,
                "status": approved_row.status,
                "message": (f"{label} was issued into a "
                            f"{approved_row.garment_size} garment on "
                            f"{approved_row.decided_by or 'a manager'}'s approval."
                            + ("" if not still_owed else
                               " Part of the line is still owed, so the approval "
                               "stays open for the rest of it.")),
            }
            await self._audit(
                actor_user_id,
                BarcodeAuditAction.MATERIAL_KIT_SUBSTITUTION_APPROVED,
                piece.id,
                {"piece": piece.code, "request": str(approved_row.id),
                 "lot": str(lot.id), "spent": True,
                 "status": approved_row.status,
                 "garment_size": approved_row.garment_size,
                 "lot_size": approved_row.lot_size})

        await self._audit(actor_user_id, BarcodeAuditAction.MATERIAL_KIT_ISSUED,
                          piece.id,
                          {"piece": piece.code, "lot": str(lot.id),
                           "packet": label, "spec_line": str(line.id),
                           "by_employee": str(employee_id),
                           "substituted": approved_row is not None})
        return substitution, {
            "ok": True, "packet": label, "lot_id": str(lot.id),
            "spec_line_id": str(line.id),
            "qty": sum(r["qty"] for r in issued["issued_now"]) or 0.0,
            "already_issued": bool(issued["already_issued"]),
            "substituted": approved_row is not None,
            # Carried so the batch can fold them into the one kit block it builds.
            "_issued": issued,
        }

    async def _kit_block(self, piece, records: list) -> dict:
        """The WHOLE checklist after a scan, with what this scan did folded in.

        NOT PER PACKET. A selective issue reports only the line it was asked about,
        which was the right shape when one scan did the whole kit and is the wrong
        shape now: the operator who has just scanned the zip needs to be told the
        buttons are still owed, on this response, while they are still standing at
        the terminal. So the packets' own results ride on top of one read-only view
        taken after all of them — which is also one query instead of N.
        """
        from app.modules.materials.style_spec_service import StyleSpecService
        kit = await StyleSpecService(self.db).kit_view(piece.id)

        issued_now, already, warnings, unresolved = [], [], [], []
        for rec in records:
            issued = rec.get("_issued")
            if not issued:
                continue
            issued_now.extend(issued["issued_now"])
            already.extend(issued["already_issued"])
            warnings.extend(issued["stock_warnings"])
            unresolved.extend(issued["unresolved"])
        kit["issued_now"] = issued_now
        kit["already_issued"] = already
        kit["stock_warnings"] = warnings

        # ONE SHAPE FOR `unresolved`, WHICHEVER HALF IT CAME FROM. The read view
        # calls the field `resolution` and the write path calls it `reason`, and
        # merging the two lists raw would hand the screen rows that answer the same
        # question under two different keys — so both keys are present on every row.
        merged = [self._normalise_unresolved(r) for r in kit["unresolved"]]
        seen = {r.get("spec_id") for r in merged}
        for row in unresolved:
            row = self._normalise_unresolved(row)
            if row.get("spec_id") not in seen:
                merged.append(row)
        kit["unresolved"] = merged

        packets = [r["packet"] for r in records if r.get("ok")]
        kit["packet"] = packets[0] if len(packets) == 1 else None
        kit["packets"] = packets
        return kit

    async def _issue_accessories(self, *, piece, lot_ids, qty, employee_id,
                                 entered_by, actor_user_id, reason) -> tuple:
        """Issue one or more accessory packets. Returns (kit, substitution, batch).

        ONE LIST, BOTH DOORS. A single `lot_barcode` arrives here as a one-element
        list, so the scan-gun path and the batch path run the same code and cannot
        drift on what a packet means or which problems are fatal.

        PARTIAL ACCEPT IN A BATCH, AND ONLY IN A BATCH. `collect` is True only when
        more than one packet was sent: a single scan keeps raising its 422/409, which
        is the contract the one-beep-one-request screen reads. In a batch a refusal
        becomes a row in `refused[]` and the good packets still issue, because one
        bad packet must never lose the others scanned with it.
        """
        collect = len(lot_ids) > 1
        records, substitution = [], None
        for lot_id in lot_ids:
            sub, record = await self._issue_packet(
                piece=piece, lot_id=lot_id, qty=qty, employee_id=employee_id,
                entered_by=entered_by, actor_user_id=actor_user_id,
                reason=reason, collect=collect)
            records.append(record)
            substitution = sub or substitution

        kit = await self._kit_block(piece, records)
        issued = [r for r in records if r.get("ok")]
        refused = [r for r in records if not r.get("ok")]
        batch = {
            "scanned": len(records), "issued": len(issued),
            "per_packet": [{k: v for k, v in r.items() if k != "_issued"}
                           for r in records],
            "refused": [{k: v for k, v in r.items() if k != "_issued"}
                        for r in refused],
            "still_owed": sorted({r["article"] for r in kit["outstanding"]}),
        }
        return kit, substitution, batch

    @staticmethod
    def _normalise_unresolved(row: dict) -> dict:
        """`reason` and `resolution` say the same thing, so carry both.

        NONE / AMBIGUOUS / MISMATCH is the vocabulary either way. The read view
        named the field `resolution` and the issue path named it `reason`, and the
        screen should not have to know which half of the response a row came from.
        """
        out = dict(row)
        out.setdefault("reason", out.get("resolution"))
        out.setdefault("resolution", out.get("reason"))
        out.setdefault("candidate_lot_ids", [])
        return out

    @staticmethod
    def _lot_label(lot) -> str:
        bits = [str(lot.article or "?")]
        if lot.colour:
            bits.append(str(lot.colour))
        if lot.size:
            bits.append(f"size {lot.size}")
        return " · ".join(bits)

    async def _substitution_row(self, piece_id, spec_line_id, lot_id):
        from app.modules.barcode.models import KitSubstitutionRequest
        return (await self.db.execute(
            select(KitSubstitutionRequest).where(
                KitSubstitutionRequest.piece_id == piece_id,
                KitSubstitutionRequest.spec_line_id == spec_line_id,
                KitSubstitutionRequest.material_lot_id == lot_id)
            .limit(1))).scalar_one_or_none()

    async def _hold_for_substitution(self, *, row, piece, line, lot, label,
                                     garment_size, qty, employee_id,
                                     entered_by, actor_user_id, reason,
                                     commit: bool) -> dict:
        """Record the ask a DM can answer, and DESCRIBE the refusal. Never raises.

        It returns `{reason, detail, request_id}` and the caller turns that into a
        409 or a row in `refused[]`. It used to raise directly; returning lets a
        batch refuse one packet without losing the others.

        `commit` IS THE WHOLE SUBTLETY, and it differs by door:

          · SINGLE packet (`commit=True`) — the caller is about to raise, and a raise
            discards the transaction. So the ask is committed here or it is lost, and
            the operator would be told "wait for approval" with nothing anywhere for
            anyone to approve. Nothing else has been written at this point in the
            scan, so the commit cannot leak a half-done merge.
          · BATCH (`commit=False`) — nothing is raised, so the ask rides the scan's
            single commit alongside the packets that succeeded. Committing here
            instead would publish those packets' issues and the garment's
            half-updated flags early.
        """
        from app.core.enums import KitSubstitutionStatus
        from app.modules.barcode.models import KitSubstitutionRequest

        if row is not None and row.status == KitSubstitutionStatus.REJECTED.value:
            return {
                "reason": "SUBSTITUTION_REFUSED", "request_id": str(row.id),
                "detail": (
                    f"{label} has already been REFUSED for {piece.code} by "
                    f"{row.decided_by or 'a manager'}"
                    f"{': ' + row.decision_note if row.decision_note else ''}. "
                    f"Fetch the {line.size or garment_size} packet."),
            }
        if row is not None and row.status == KitSubstitutionStatus.CONSUMED.value:
            # The approval was one garment's, and it has been spent IN FULL — the
            # status is only set once the line owes nothing, so this really is a
            # re-tap and not a half-finished issue. A re-tap is a no-op anyway, so
            # say which of the two this is rather than silently doing nothing.
            return {
                "reason": "SUBSTITUTION_SPENT", "request_id": str(row.id),
                "detail": (
                    f"{label} was already issued into {piece.code} in full, on an "
                    f"approved substitution. That approval covered this one "
                    f"garment; a further garment needs its own decision."),
            }

        if row is None:
            row = KitSubstitutionRequest(
                piece_id=piece.id, spec_line_id=line.id, material_lot_id=lot.id,
                garment_size=garment_size, lot_size=lot.size,
                article=lot.article, colour=lot.colour, subtype=lot.subtype,
                qty=(Decimal(str(qty)) if qty is not None
                     else Decimal(str(line.qty_per_piece or 0))),
                status=KitSubstitutionStatus.PENDING.value,
                requested_by_employee_id=employee_id, requested_by=entered_by,
                reason=reason)
            self.db.add(row)
            await self._audit(
                actor_user_id,
                BarcodeAuditAction.MATERIAL_KIT_SUBSTITUTION_REQUESTED,
                piece.id,
                {"piece": piece.code, "lot": str(lot.id), "packet": label,
                 "spec_line": str(line.id), "garment_size": garment_size,
                 "lot_size": lot.size, "spent": False,
                 "by_employee": str(employee_id), "reason": reason})
            if commit:
                await self.db.commit()
                await self.db.refresh(row)
            else:
                # FLUSHED, NOT COMMITTED, so `row.id` is real for the response and
                # the ask lands with the scan's single commit.
                await self.db.flush()

        return {
            "reason": "WRONG_SIZE", "request_id": str(row.id),
            "detail": (
                f"WRONG SIZE — NOT ISSUED. {piece.code} is a "
                f"{garment_size or 'no-size'} garment and this packet is "
                f"{lot.size or 'unsized'}"
                f"{'; its recipe asks for ' + str(line.size) if line.size else ''}. "
                f"Nothing has been taken from stock. Fetch the "
                f"{line.size or garment_size} packet, or ask a DM/MD to approve "
                f"this substitution (request {row.id}) and scan it again."),
        }

    # ══════════════════════════════════════════ the substitution decisions
    async def list_substitutions(self, *, status_filter: str | None = None,
                                 limit: int = 200, offset: int = 0) -> dict:
        """The DM's queue. PENDING first and oldest first, like the arrivals queue.

        An unfinished approval nobody can find is a garment stuck in the store with
        no visible reason — the operator was told to wait and the person who can
        end the wait never learned there was one.
        """
        from sqlalchemy import func as _func
        from app.core.enums import KitSubstitutionStatus
        from app.modules.barcode.models import KitSubstitutionRequest

        stmt = select(KitSubstitutionRequest)
        if status_filter:
            stmt = stmt.where(
                KitSubstitutionRequest.status == status_filter.strip().upper())
        total = await self.db.scalar(
            select(_func.count()).select_from(stmt.subquery())) or 0
        # `id` IS A TIEBREAK, NOT A SORT. Two requests raised in the same instant
        # tie on created_at, and an unstable ORDER BY under limit/offset can serve
        # one row on two pages and skip another entirely — so the pager needs a
        # total order even where the second column carries no meaning.
        rows = (await self.db.execute(
            stmt.order_by(KitSubstitutionRequest.created_at.asc(),
                          KitSubstitutionRequest.id.asc())
            .limit(limit).offset(offset))).scalars().all()

        out = []
        for row in rows:
            piece = await self.db.get(Piece, row.piece_id)
            out.append({
                "request_id": str(row.id),
                "piece_id": str(row.piece_id),
                "piece_code": getattr(piece, "code", None),
                "spec_line_id": str(row.spec_line_id),
                "material_lot_id": (str(row.material_lot_id)
                                    if row.material_lot_id else None),
                "article": row.article, "colour": row.colour,
                "subtype": row.subtype,
                "garment_size": row.garment_size, "lot_size": row.lot_size,
                "qty": float(row.qty or 0), "status": row.status,
                "requested_by": row.requested_by, "reason": row.reason,
                "decided_by": row.decided_by,
                "decided_at": (row.decided_at.isoformat()
                               if row.decided_at else None),
                "decision_note": row.decision_note,
                "requested_at": (row.created_at.isoformat()
                                 if row.created_at else None),
                "summary": (f"{row.article} size {row.lot_size or '?'} into a "
                            f"{row.garment_size or '?'} garment"),
            })
        pending = await self.db.scalar(
            select(_func.count()).select_from(KitSubstitutionRequest)
            .where(KitSubstitutionRequest.status
                   == KitSubstitutionStatus.PENDING.value)) or 0
        return {"count": len(out), "total": int(total), "pending": int(pending),
                "limit": limit, "offset": offset, "requests": out}

    async def decide_substitution(self, request_id, *, approve: bool,
                                  note: str | None = None,
                                  actor_user_id=None,
                                  actor_name: str | None = None) -> dict:
        """A DM/MD answers one wrong-size ask. It does NOT issue anything.

        APPROVING IS PERMISSION, NOT THE ISSUE ITSELF, and separating the two is
        what keeps the ledger honest about who did what. The operator is holding
        the packet; the DM is not. So the DM grants permission and the operator
        re-scans, which is what writes the stock movement against the worker's own
        card. An approval that spent stock by itself would record a manager as
        having issued a packet they never touched.

        A REJECTED ask may be approved later and an approved one withdrawn while it
        is unspent — "no" on Tuesday and "yes" on Wednesday is a real sequence.
        CONSUMED is terminal: that approval has been spent.
        """
        from app.core.enums import KitSubstitutionStatus
        from app.modules.barcode.models import KitSubstitutionRequest

        row = await self.db.get(KitSubstitutionRequest, request_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "No such substitution request.")
        if row.status == KitSubstitutionStatus.CONSUMED.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"That approval has already been spent — {row.article} went into "
                f"the garment. Deciding it again would change nothing.")

        row.status = (KitSubstitutionStatus.APPROVED.value if approve
                      else KitSubstitutionStatus.REJECTED.value)
        row.decided_by_user_id = actor_user_id
        row.decided_by = actor_name
        row.decided_at = datetime.now(timezone.utc)
        row.decision_note = (note or "").strip() or None

        piece = await self.db.get(Piece, row.piece_id)
        await self._audit(
            actor_user_id,
            (BarcodeAuditAction.MATERIAL_KIT_SUBSTITUTION_APPROVED if approve
             else BarcodeAuditAction.MATERIAL_KIT_SUBSTITUTION_REJECTED),
            row.piece_id,
            {"piece": getattr(piece, "code", None), "request": str(row.id),
             "article": row.article, "garment_size": row.garment_size,
             "lot_size": row.lot_size, "by": actor_name, "note": row.decision_note,
             # The decision alone spends nothing; the operator's re-scan does.
             "spent": False})
        await self.db.commit()
        return {
            "request_id": str(row.id), "status": row.status,
            "piece_code": getattr(piece, "code", None),
            "article": row.article, "garment_size": row.garment_size,
            "lot_size": row.lot_size,
            "decided_by": actor_name, "decision_note": row.decision_note,
            "message": (
                f"Approved. The operator can now scan {row.article} size "
                f"{row.lot_size or '?'} into {getattr(piece, 'code', 'the garment')} "
                f"— it is not issued until they do."
                if approve else
                f"Refused. {row.article} size {row.lot_size or '?'} will not be "
                f"issued into {getattr(piece, 'code', 'the garment')}; the floor "
                f"must fetch the right packet."),
        }

    @staticmethod
    def _next_action(piece, complete: bool, needs_lining: bool,
                     kit_required: bool) -> str:
        """One sentence telling the operator what this garment is waiting on."""
        if piece.store_state == StoreState.SENDED.value:
            return "Sent to line-stitching."
        if not piece.leather_in:
            return "Waiting for its leather — scan that in when it is pasted."
        if needs_lining and not piece.lining_in:
            return "Waiting for its lining — scan that in when it is cut."
        if kit_required and not piece.accessories_in:
            return ("Waiting for its accessories — scan each packet's own "
                    "label to issue it.")
        if complete:
            return "Complete. Send it to open line-stitching."
        return "In store."

    # ══════════════════════════════════════════════════════════ the release
    async def send(self, *, piece_ids: list, actor_user_id=None,
                   actor_name: str | None = None) -> dict:
        """Release garments to line-stitching. PARTIAL ACCEPT, by design.

        One incomplete garment must never lose the forty complete ones selected
        with it — the same rule the production log's per-piece gates follow. The
        rejected ones come back named, with what each is still missing.

        THE GATE IS COMPLETENESS, NOT A PRIOR RECEIVED. A garment that is complete
        has everything it will ever get, and requiring a separate RECEIVED tap
        first only adds a click that the auto-receive already performs.
        """
        sent, not_ready, not_found = [], [], []
        now = datetime.now(timezone.utc)

        for pid in piece_ids or []:
            piece = await self.get_piece_for_update(pid)
            if piece is None:
                not_found.append(str(pid))
                continue
            if piece.store_state == StoreState.SENDED.value:
                sent.append(piece.code)       # already out; re-tap is a no-op
                continue

            needs_lining, reason = await self.needs_lining(piece)
            kit_required = await self._kit_required(piece)
            if not kit_rules.piece_complete(
                    leather_in=piece.leather_in, lining_in=piece.lining_in,
                    accessories_in=piece.accessories_in,
                    needs_lining=needs_lining, kit_required=kit_required):
                missing = ("leather" if not piece.leather_in
                           else "lining" if needs_lining and not piece.lining_in
                           else "accessory kit")
                not_ready.append({
                    "piece": piece.code, "missing": missing,
                    "state": piece.store_state,
                    "reason": (f"{piece.code} is still awaiting its {missing}."
                               + (f" It takes a lining because {reason}."
                                  if missing == "lining" and reason else ""))})
                continue

            piece.store_state = StoreState.SENDED.value
            piece.store_sended_at = now
            if piece.store_received_at is None:
                piece.store_received_at = now   # backfill: it was complete
            sent.append(piece.code)
            await self._audit(actor_user_id, BarcodeAuditAction.DRAWER_SENDED,
                              piece.id, {"piece": piece.code, "by": actor_name})

        await self.db.commit()
        # THE DASHBOARDS ARE NOW STALE — say so immediately.
        #
        # Bust the read cache in the same breath as the commit, not on a timer.
        # A cutting manager who logs a cut expects to see it on the dashboard at
        # once; a minute of TTL reads on the floor as "the system lost my scan",
        # and the operator scans again. One INCR, and every cached aggregate
        # becomes unreachable. No-op when caching is off or Redis is away.
        from app.core.cache import invalidate as _invalidate_read_cache
        await _invalidate_read_cache()
        parts = []
        if sent:
            parts.append(f"{len(sent)} garment(s) sent to line-stitching")
        if not_ready:
            parts.append(f"{len(not_ready)} not ready")
        if not_found:
            parts.append(f"{len(not_found)} not found")
        return {"count_sent": len(sent), "sent": sent, "not_ready": not_ready,
                "not_found": not_found,
                "message": "; ".join(parts) or "Nothing to send."}

    async def release_nocommit(self, piece_id) -> None:
        """The garment has shipped — it leaves the store. NO COMMIT.

        Called at PACKAGE_EXPORT. There is no pool to return to any more: a
        drawer recycled because the BOX was reused, but a garment ships once.
        """
        piece = await self.get_piece_for_update(piece_id)
        if piece is None:
            return
        piece.store_state = StoreState.WAITING.value
        piece.leather_in = False
        piece.lining_in = False
        piece.accessories_in = False
        piece.store_received_at = None
        piece.store_sended_at = None

    # ══════════════════════════════════════════════════════════ reading
    async def _kit_summaries(self, piece_ids: list) -> dict:
        """{piece_code: {kit_required, kit_status, outstanding}} for a batch.

        THE ACCESSORY ANSWER IS NEVER A BARE BOOLEAN, and this is where the read
        paths stop pretending it is. `piece.accessories_in` is a ROLL-UP —
        "every accessory line this style declares has been issued in full" — and
        on its own it cannot tell an operator whether the zip is missing or the
        buttons are. `kit_status` names the three states the floor actually has
        (NOT_REQUIRED / PENDING / PARTIAL / ISSUED) and `outstanding` says how
        much is still owed across the lines.

        Batched on purpose: the store list renders 200 garments, and asking the
        spec per piece was 200 round-trips for a column.
        """
        if not piece_ids:
            return {}
        try:
            from app.modules.materials.style_spec_service import StyleSpecService
            return await StyleSpecService(self.db).kit_by_pieces(list(piece_ids))
        except Exception:
            # A style with no spec — everything released before the spec feature
            # — requires no kit. Failing open keeps those garments readable.
            return {}

    async def piece_row(self, piece, *, kit_summary: dict | None = None) -> dict:
        from app.modules.clients.models import SKU, Style
        sku = await self.db.get(SKU, piece.sku_id) if piece.sku_id else None
        style = await self.db.get(Style, sku.style_id) if sku else None
        needs_lining, _ = await self.needs_lining(piece)
        if kit_summary is None:
            kit_summary = (await self._kit_summaries([piece.id])).get(piece.code)
        if kit_summary:
            kit_required = bool(kit_summary.get("kit_required"))
        else:
            # The batch read found nothing for this code (no spec, or it threw).
            # Ask the single-piece way rather than reporting "no kit" for a
            # garment that may well owe one.
            kit_summary = {}
            kit_required = await self._kit_required(piece)

        # WHAT IS STILL OWED, spelled the same way the scan response spells it, so
        # the lookup screen and the scan screen cannot disagree about a garment.
        awaiting = []
        if not piece.leather_in:
            awaiting.append("LEATHER")
        if needs_lining and not piece.lining_in:
            awaiting.append("LINING")
        if kit_required and not piece.accessories_in:
            awaiting.append("ACCESSORIES")

        return {
            "piece_id": piece.id, "piece_code": piece.code,
            "store_state": piece.store_state,
            "holding": holding_label(leather_in=piece.leather_in,
                                     lining_in=piece.lining_in),
            "leather_in": piece.leather_in, "lining_in": piece.lining_in,
            "accessories_in": piece.accessories_in,
            # The accessory side, as three fields instead of one boolean. See
            # _kit_summaries for why the boolean alone was never enough.
            "kit_required": kit_required,
            "kit_status": kit_summary.get(
                "kit_status", kit_rules.kit_status(
                    kit_required=kit_required, required_total=0.0,
                    issued_total=0.0)),
            "accessories_outstanding": float(kit_summary.get("outstanding") or 0),
            "needs_lining": needs_lining,
            "awaiting": awaiting,
            "complete": kit_rules.piece_complete(
                leather_in=piece.leather_in, lining_in=piece.lining_in,
                accessories_in=piece.accessories_in,
                needs_lining=needs_lining, kit_required=kit_required),
            "style_name": getattr(style, "name", None),
            "colour": (getattr(sku, "color_name", None)
                       or getattr(sku, "color_code", None)),
            "size": getattr(sku, "size", None),
        }

    async def piece_detail(self, piece) -> dict:
        """One garment, WITH its accessory checklist line by line.

        THE ANSWER TO "how do I verify they took the accessories?". The list view
        answers it as a status; here every declared line comes back on its own —
        4 BUTTON BLACK 20L, 1 ZIP GUNMETAL 60cm, 200 mtrs THREAD — with what has
        been issued against it, what is still owed, and whether the lot it would
        come from is even resolvable. A garment is not "accessories: true"; it is
        five lines, each of which is or is not satisfied.
        """
        row = await self.piece_row(piece)
        try:
            from app.modules.materials.style_spec_service import StyleSpecService
            block = await StyleSpecService(self.db).material_requirement_block(
                piece.id)
        except Exception:
            block = {}
        row["accessories"] = block.get("accessories") or []
        row["summary_line"] = block.get("summary_line")
        row["spec_confirmed"] = bool(block.get("spec_confirmed"))
        return row

    async def piece_materials(self, piece) -> dict:
        """Everything merged into this garment, and everything that is not.

        Delegates to StyleSpecService.piece_materials — the SAME call the
        barcode scan makes, so the store screen and the scan gun cannot answer
        "what is in this garment" differently.
        """
        from app.modules.materials.style_spec_service import StyleSpecService
        out = await StyleSpecService(self.db).piece_materials(piece.id)
        needs_lining, reason = await self.needs_lining(piece)
        out["needs_lining"] = needs_lining
        out["lining_reason"] = reason
        return out

    async def list_pieces(self, *, state: str | None = None,
                          style_id=None, limit: int = 200,
                          offset: int = 0) -> dict:
        """What is in the store right now. PAGED.

        `limit` ALONE WAS NOT A PAGER, it was a cap: a caller could ask for the
        first 200 garments and had no way to ask for the next 200 — and a busy
        store holds far more than 200. `offset` and `total` are what make it one.

        The shape is ADDITIVE on purpose: `count` and `pieces` are what the store
        screen already reads, so they keep their names and meanings and the new
        pager fields sit beside them. See core/pagination.py on why the published
        shapes are not retrofitted.
        """
        from sqlalchemy import func
        stmt = select(Piece).where(Piece.is_active.is_(True))
        if state:
            stmt = stmt.where(Piece.store_state == state.strip().lower())
        else:
            stmt = stmt.where(Piece.store_state != StoreState.WAITING.value)
        if style_id:
            from app.modules.clients.models import SKU
            stmt = stmt.join(SKU, SKU.id == Piece.sku_id).where(
                SKU.style_id == style_id)
        # Counted from the SAME statement so the two cannot disagree about which
        # rows they are talking about (core/pagination.paginate, same rule).
        total = int(await self.db.scalar(
            select(func.count()).select_from(stmt.subquery())) or 0)
        stmt = stmt.order_by(Piece.code.asc()).limit(limit).offset(offset)
        pieces = list((await self.db.execute(stmt)).scalars().all())
        # ONE kit read for the whole page, not one per row.
        kits = await self._kit_summaries([p.id for p in pieces])
        return {"count": len(pieces), "total": total,
                "limit": limit, "offset": offset,
                "has_more": (offset + len(pieces)) < total,
                "pieces": [await self.piece_row(p, kit_summary=kits.get(p.code))
                           for p in pieces]}

    async def _audit(self, actor_user_id, action, entity_id, after: dict) -> None:
        """`actor_user_id` is an app_user.id — the LOGIN — never an employee.id.

        AuditLog.actor_user_id is a foreign key to app_user; passing the scanned
        worker's id is what produced a 500 on the old store scan. The worker
        travels in `after` as data.
        """
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_user_id,
            action=getattr(action, "value", str(action)),
            entity_type="store", entity_id=entity_id, after=after,
            at=datetime.now(timezone.utc)))
