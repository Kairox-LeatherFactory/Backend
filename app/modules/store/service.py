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
                         lines=None, actor_user_id=None,
                         entered_by: str | None = None) -> dict:
        """Put one part of one garment into the store. ONE transaction.

        The stock movements, the ledger rows, the piece's flags and the audit row
        all land together or not at all — a half-issued kit is worse than an
        unissued one, because nothing downstream can tell them apart.
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
        inferred = part is None
        if part is None:
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
        self._assert_ready_for_store(piece, chosen, done)

        now = datetime.now(timezone.utc)
        kit = None
        warnings: list[str] = []

        if chosen is StorePart.ACCESSORY:
            from app.modules.materials.style_spec_service import StyleSpecService
            kit = await StyleSpecService(self.db).issue_kit_nocommit(
                piece=piece, drawer=None, requested_lines=lines,
                employee_id=employee_id, entered_by=entered_by)
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
            "next_action": self._next_action(piece, complete, needs_lining,
                                             kit_required),
            "warnings": warnings,
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
            return "Waiting for its accessories — scan the kit to issue them."
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
    async def piece_row(self, piece) -> dict:
        from app.modules.clients.models import SKU, Style
        sku = await self.db.get(SKU, piece.sku_id) if piece.sku_id else None
        style = await self.db.get(Style, sku.style_id) if sku else None
        needs_lining, _ = await self.needs_lining(piece)
        kit_required = await self._kit_required(piece)
        return {
            "piece_id": piece.id, "piece_code": piece.code,
            "store_state": piece.store_state,
            "holding": holding_label(leather_in=piece.leather_in,
                                     lining_in=piece.lining_in),
            "leather_in": piece.leather_in, "lining_in": piece.lining_in,
            "accessories_in": piece.accessories_in,
            "needs_lining": needs_lining,
            "complete": kit_rules.piece_complete(
                leather_in=piece.leather_in, lining_in=piece.lining_in,
                accessories_in=piece.accessories_in,
                needs_lining=needs_lining, kit_required=kit_required),
            "style_name": getattr(style, "name", None),
            "colour": (getattr(sku, "color_name", None)
                       or getattr(sku, "color_code", None)),
            "size": getattr(sku, "size", None),
        }

    async def list_pieces(self, *, state: str | None = None,
                          style_id=None, limit: int = 200) -> dict:
        stmt = select(Piece).where(Piece.is_active.is_(True))
        if state:
            stmt = stmt.where(Piece.store_state == state.strip().lower())
        else:
            stmt = stmt.where(Piece.store_state != StoreState.WAITING.value)
        if style_id:
            from app.modules.clients.models import SKU
            stmt = stmt.join(SKU, SKU.id == Piece.sku_id).where(
                SKU.style_id == style_id)
        stmt = stmt.order_by(Piece.code.asc()).limit(limit)
        pieces = list((await self.db.execute(stmt)).scalars().all())
        return {"count": len(pieces),
                "pieces": [await self.piece_row(p) for p in pieces]}

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
