"""
================================================================================
modules/drawers/service.py — Drawers & the COMPLETENESS (merge) gate
================================================================================

THE NEW GATE CLASS.
    Earlier stages gate on SEQUENCE (no pasting before fusing). Storage gates on
    COMPLETENESS: no line-stitching until a piece's leather AND its lining are
    both confirmed in its drawer. The breakdown's material column set
    Piece.needs_lining at upload, so a leather-only piece (needs_lining=False) is
    complete on leather alone and passes the gate immediately.

STORE = SCAN THE DRAWER FIRST, THEN THE PIECE.
    The drawer scan confirms the piece is going where the upload's merge says it
    should. A piece scanned into the wrong drawer is a 409 — the merge map is the
    authority, not the worker's memory.

DRAWERS RECYCLE.
    waiting → merged (at upload) → holding_leather → holding_both → received →
    sended → (piece ships) → waiting. `code`/`seq` never change; `current_piece_id`
    and `state` move over the drawer's life. When PACKAGE_EXPORT logs, production
    calls release() and the drawer returns to WAITING for the next piece.
================================================================================
"""
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (BarcodeAuditAction, BarcodeType, DrawerPart,
                            DrawerState, ProductionStage)
from app.core.store_display import holding_label
from app.modules.barcode.models import BarcodeRegistry, Drawer
from app.modules.production.models import Piece

# Where a batch send routes a drawer. STITCHING is the one that opens the merge
# gate (production reads DrawerState.SENDED through is_sended); LINING records
# that the drawer went to the lining floor and deliberately does NOT.
SEND_DESTINATIONS = ("STITCHING", "LINING")


class DrawerService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def repo_commit(self) -> None:
        """Single commit seam for this module (F71). The drawers module has no
        repository yet (F105 — D2), so this method localises the one place the
        session is committed. Callers that COMPOSE drawer mutations into a larger
        unit of work should use the *_nocommit variants instead and own the
        boundary (production already does this via release_nocommit)."""
        await self.db.commit()

    # ── lookups ──────────────────────────────────────────────────────────────
    async def get(self, drawer_id: uuid.UUID) -> Drawer | None:
        return await self.db.get(Drawer, drawer_id)

    async def get_by_code(self, code: str) -> Drawer | None:
        res = await self.db.execute(
            select(Drawer).where(Drawer.code == (code or "").strip().upper()))
        return res.scalar_one_or_none()

    async def drawer_for_piece(self, piece_id: uuid.UUID) -> Drawer | None:
        res = await self.db.execute(
            select(Drawer).where(Drawer.current_piece_id == piece_id))
        return res.scalar_one_or_none()

    # ── the label sheet (print) ──────────────────────────────────────────────
    async def list_labels(self, *, state: str | None = None,
                          seq_from: int | None = None, seq_to: int | None = None,
                          has_piece: bool | None = None,
                          sendable: bool | None = None,
                          limit: int = 500, offset: int = 0) -> dict:
        """Every drawer + its barcode + the garment inside it — the Drawers List.

        THIS STARTED LIFE AS A PRINT SHEET and is now also the working list bug
        #13 asks for, which needs the piece, not just the code: an operator
        choosing which drawers to send is choosing GARMENTS, and a screen of
        DRW-0001…DRW-0430 with no styles on it cannot support that choice. The
        piece columns are one extra LEFT JOIN on the same statement.

        OUTER join on the barcode, on purpose. A drawer whose registry row is
        missing is a drawer nobody can scan; it must appear in the sheet with a
        null barcode so the gap is visible, not vanish from a list that claims to
        be every drawer. The join is pinned to type=DRAWER so a stale row of
        another type can never supply the code, and drawer codes are unique in the
        registry (uq_barcode_code) so one drawer yields at most one row.
        """
        def _filtered(stmt):
            if state:
                stmt = stmt.where(Drawer.state == state)
            if seq_from is not None:
                stmt = stmt.where(Drawer.seq >= seq_from)
            if seq_to is not None:
                stmt = stmt.where(Drawer.seq <= seq_to)
            if has_piece is True:
                stmt = stmt.where(Drawer.current_piece_id.isnot(None))
            elif has_piece is False:
                stmt = stmt.where(Drawer.current_piece_id.is_(None))
            # "Show me what I can send right now" — RECEIVED is reached
            # automatically on completeness, so this is the send queue.
            if sendable is True:
                stmt = stmt.where(Drawer.state == DrawerState.RECEIVED.value)
            elif sendable is False:
                stmt = stmt.where(Drawer.state != DrawerState.RECEIVED.value)
            return stmt

        total = int(await self.db.scalar(
            _filtered(select(func.count(Drawer.id)))) or 0)

        rows = (await self.db.execute(
            _filtered(
                select(Drawer, BarcodeRegistry.id, BarcodeRegistry.code,
                       BarcodeRegistry.caption, BarcodeRegistry.status,
                       Piece.code.label("piece_code"), Piece.seq.label("piece_seq"),
                       Piece.needs_lining)
                .outerjoin(
                    BarcodeRegistry,
                    and_(BarcodeRegistry.drawer_id == Drawer.id,
                         BarcodeRegistry.type == BarcodeType.DRAWER.value))
                .outerjoin(Piece, Piece.id == Drawer.current_piece_id)
            )
            .order_by(Drawer.seq.asc())
            .limit(limit).offset(offset)
        )).all()

        items = []
        for r in rows:
            drawer = r[0]
            needs_lining = (True if r.needs_lining is None
                            else bool(r.needs_lining))
            complete = drawer.leather_in and (drawer.lining_in or not needs_lining)
            items.append({
                "drawer_id": drawer.id,
                "seq": drawer.seq,
                "code": drawer.code,
                "state": drawer.state,
                "holding": holding_label(leather_in=drawer.leather_in,
                                         lining_in=drawer.lining_in),
                "leather_in": bool(drawer.leather_in),
                "lining_in": bool(drawer.lining_in),
                "complete": bool(complete),
                # bug #13: the garment in the drawer, so the list is choosable.
                "piece_id": drawer.current_piece_id,
                "piece_code": r.piece_code,
                "piece_serial": (f"{r.piece_seq:03d}"
                                 if r.piece_seq is not None else None),
                "sent_to": drawer.sent_to,
                "can_send": drawer.state == DrawerState.RECEIVED.value,
                "barcode_id": r[1],
                "barcode": r[2],
                "caption": r[3],
                "barcode_status": r[4],
            })
        return {"total": total, "count": len(items), "items": items}

    # ── which bucket does this scan belong in? (bug #18) ─────────────────────
    async def infer_part(self, drawer: Drawer, piece: Piece) -> DrawerPart:
        """Work out whether this scan is the LEATHER or the LINING arriving.

        BUG #18 — the operator used to click "Hold Leather" or "Hold Lining" by
        hand after selecting the drawer. That button is a question the system can
        already answer: the piece's own production history says which side of the
        cut it has been through, and the drawer says which side it is still
        missing. A hand-picked bucket is only ever a chance to pick the wrong one.

        The order below is "what does the evidence say", then "what is missing":

          1. The piece has a LINING_CUTTING event and the drawer has no lining in
             → this is that lining arriving.
          2. The piece has a leather-side event and no leather in → the leather.
          3. Neither is decisive → fill whichever side is still empty.
          4. Both sides already in → 409; there is nothing left to put anywhere.

        Rule 3 matters more than it looks: a factory that has not yet started
        logging its cut events would otherwise have no inferable answer at all,
        and the store screen would be unusable. The drawer's own emptiness is
        always a valid signal.
        """
        if drawer.leather_in and drawer.lining_in:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Drawer {drawer.code} already holds both leather and lining for "
                f"{piece.code} — there is nothing further to scan in.")

        from app.modules.production.repository import ProductionRepository
        done = await ProductionRepository(self.db).completed_stage_codes(piece.id)

        if ProductionStage.LINING_CUTTING.value in done and not drawer.lining_in:
            return DrawerPart.LINING
        leather_side = {ProductionStage.LEATHER_CUTTING.value,
                        ProductionStage.FUSING.value,
                        ProductionStage.PASTING.value}
        if (done & leather_side) and not drawer.leather_in:
            return DrawerPart.LEATHER
        return DrawerPart.LEATHER if not drawer.leather_in else DrawerPart.LINING

    # ── store-scan ───────────────────────────────────────────────────────────
    async def store_scan(self, *, drawer_id: uuid.UUID, piece_id: uuid.UUID,
                         part: DrawerPart | None = None,
                         actor_id: uuid.UUID | None = None) -> dict:
        drawer = await self.get(drawer_id)
        if not drawer:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Drawer not found.")
        piece = await self.db.get(Piece, piece_id)
        if not piece:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Piece not found.")

        # The merge map is the authority: the piece must belong to THIS drawer.
        if drawer.current_piece_id != piece.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{piece.code} is not merged to drawer {drawer.code}. "
                "Scan the drawer the upload assigned to this piece.")

        # F08: a part scan is only valid while the drawer is still accumulating.
        # A drawer already RECEIVED or SENDED must not be dragged backwards by a
        # new scan — that would silently revoke a merge gate production may have
        # already passed.
        if drawer.state in (DrawerState.RECEIVED.value, DrawerState.SENDED.value):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Drawer {drawer.code} is already {drawer.state} and cannot accept "
                "another part scan. Its pieces have been released for the next stage.")

        # BUG #18: no manual Hold Leather / Hold Lining button. An explicit part
        # still wins (a screen that genuinely knows), otherwise the system reads
        # it off the piece's history and the drawer's contents.
        inferred = part is None
        if part is None:
            part = await self.infer_part(drawer, piece)

        if part is DrawerPart.LEATHER:
            drawer.leather_in = True
        else:
            drawer.lining_in = True

        needs_lining = bool(getattr(piece, "needs_lining", True))
        complete = drawer.leather_in and (drawer.lining_in or not needs_lining)
        # THE STATE NAMES WHAT IS PHYSICALLY IN THE DRAWER — nothing else.
        #   leather only            → HOLDING_LEATHER
        #   lining only             → HOLDING_LINING   (F07: a lining-first scan
        #                             must not report HOLDING_LEATHER)
        #   both                    → HOLDING_BOTH
        # COMPLETENESS is a separate question, answered by `complete` /
        # `ready_for_received`: a leather-only piece (needs_lining=False) is
        # ready on leather alone, but its drawer still holds LEATHER — calling
        # that HOLDING_BOTH (as it did) named a lining that is not in there and
        # was never coming.
        if drawer.leather_in and drawer.lining_in:
            drawer.state = DrawerState.HOLDING_BOTH.value
        elif drawer.leather_in:
            drawer.state = DrawerState.HOLDING_LEATHER.value
        elif drawer.lining_in:
            drawer.state = DrawerState.HOLDING_LINING.value
        else:
            drawer.state = DrawerState.WAITING.value

        # ── COMPLETENESS AUTO-ADVANCES TO RECEIVED (bug #13) ─────────────────
        # RECEIVED used to be a button the DM pressed on one drawer at a time. But
        # RECEIVED asserts exactly one thing — "this drawer holds everything this
        # piece needs" — and the scan that just landed is what makes that true or
        # not. There is no judgement left for a human to add, so the click was
        # pure latency: hundreds of drawers waiting on a manual confirmation of a
        # fact the server had already computed.
        #
        # SEND IS STILL A DECISION AND STAYS MANUAL. Receiving records what is in
        # the drawer; sending releases the piece into the next stage. Only the
        # first is mechanical — which is why this auto-advances one step and
        # stops. See send_batch.
        auto_received = False
        if complete:
            drawer.state = DrawerState.RECEIVED.value
            drawer.received_at = datetime.now(timezone.utc)
            auto_received = True
            await self._audit(
                actor_id, BarcodeAuditAction.DRAWER_RECEIVED.value, drawer.id,
                {"piece": piece.code, "state": drawer.state, "auto": True})

        await self.repo_commit()   # F71: commit via a single seam (see below)
        await self.db.refresh(drawer)

        awaiting = []
        if not drawer.leather_in:
            awaiting.append("LEATHER")
        if needs_lining and not drawer.lining_in:
            awaiting.append("LINING")
        return {
            "drawer_code": drawer.code, "piece_code": piece.code,
            "state": drawer.state, "needs_lining": needs_lining,
            "awaiting": awaiting, "ready_for_received": complete,
            "part": part.value,
            # True when the bucket was decided by the server, not the operator —
            # so the screen can show WHICH bucket it chose (bug #18).
            "part_inferred": inferred,
            "holding": holding_label(leather_in=drawer.leather_in,
                                     lining_in=drawer.lining_in),
            "auto_received": auto_received,
            # ── BUG #15: THIS SCAN IS NOT COMPLETION ─────────────────────────
            # The frontend was marking an item finished as soon as it was scanned
            # in. It is not: the piece is sitting in a drawer, and it does not
            # move on until someone selects that drawer and sends it. These two
            # fields say so in the response itself, so the UI has no excuse to
            # infer otherwise.
            "sent": drawer.state == DrawerState.SENDED.value,
            "sent_to": drawer.sent_to,
            "next_action": (
                f"{piece.code} is logged in drawer {drawer.code} and stays there. "
                f"Select the drawer in the Drawers List and Send to Lining / "
                f"Stitching to move it on."
                if complete else
                f"{piece.code} is logged in drawer {drawer.code}. Still awaiting "
                f"{' + '.join(awaiting)} before it can be sent."),
        }

    # ── received / sended ────────────────────────────────────────────────────
    async def transition(self, drawer_id: uuid.UUID, transition: str,
                         actor_id: uuid.UUID | None) -> dict:
        drawer = await self.get(drawer_id)
        if not drawer:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Drawer not found.")
        piece = await self.db.get(Piece, drawer.current_piece_id) if drawer.current_piece_id else None
        needs_lining = bool(getattr(piece, "needs_lining", True)) if piece else True
        t = transition.upper()

        if t == "RECEIVED":
            # F09: RECEIVED must validate the source state too — a drawer already
            # SENDED must not move back to RECEIVED (the machine is a forward
            # cycle; the SENDED branch already guards its direction).
            if drawer.state == DrawerState.SENDED.value:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Drawer {drawer.code} is already SENDED — cannot move back to "
                    "RECEIVED.")
            complete = drawer.leather_in and (drawer.lining_in or not needs_lining)
            if not complete:
                missing = "lining" if needs_lining and not drawer.lining_in else "leather"
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Cannot RECEIVE: still awaiting {missing} in drawer {drawer.code}.")
            drawer.state = DrawerState.RECEIVED.value
            drawer.received_at = datetime.now(timezone.utc)
            action = BarcodeAuditAction.DRAWER_RECEIVED.value

        elif t == "SENDED":
            if drawer.state != DrawerState.RECEIVED.value:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Cannot SEND before RECEIVED. Set RECEIVED first.")
            drawer.state = DrawerState.SENDED.value
            drawer.sended_at = datetime.now(timezone.utc)
            # The legacy single-drawer route names no destination; it always
            # meant "release to line-stitching", so record that rather than
            # leaving sent_to null and making the list show a blank column.
            drawer.sent_to = "STITCHING"
            action = BarcodeAuditAction.DRAWER_SENDED.value
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "transition must be RECEIVED or SENDED.")

        await self._audit(actor_id, action, drawer.id,
                          {"piece": piece.code if piece else None, "state": drawer.state})
        await self.repo_commit()
        await self.db.refresh(drawer)
        return {
            "drawer_code": drawer.code,
            "piece_code": piece.code if piece else None,
            "state": drawer.state,
        }

    # ── drawer detail (bug #13) ──────────────────────────────────────────────
    async def drawer_detail(self, drawer_id: uuid.UUID) -> dict:
        """One drawer, opened: what it holds, which garment, and what it is
        waiting for.

        The Drawers List (bug #13) needs a row you can click into. `list_labels`
        was built as a PRINT sheet — codes and states, no garment — so there was
        nothing behind the row. This is that detail: the piece card (article,
        serial, order), the hold-leather / hold-lining truth, and whether the
        drawer is eligible to be sent.
        """
        drawer = await self.get(drawer_id)
        if not drawer:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Drawer not found.")

        piece_card = None
        needs_lining = True
        if drawer.current_piece_id:
            from app.modules.barcode.service import BarcodeService
            piece_card = await BarcodeService(self.db)._piece_payload(
                drawer.current_piece_id)
            needs_lining = bool(piece_card.get("needs_lining", True))

        complete = drawer.leather_in and (drawer.lining_in or not needs_lining)
        awaiting = []
        if not drawer.leather_in:
            awaiting.append("LEATHER")
        if needs_lining and not drawer.lining_in:
            awaiting.append("LINING")

        return {
            "drawer_id": drawer.id, "code": drawer.code, "seq": drawer.seq,
            "state": drawer.state,
            "holding": holding_label(leather_in=drawer.leather_in,
                                     lining_in=drawer.lining_in),
            "leather_in": bool(drawer.leather_in),
            "lining_in": bool(drawer.lining_in),
            "needs_lining": needs_lining,
            "awaiting": awaiting,
            "complete": bool(complete),
            "received_at": drawer.received_at,
            "sended_at": drawer.sended_at,
            "sent_to": drawer.sent_to,
            "sent": drawer.state == DrawerState.SENDED.value,
            # What the list's Send button should do with this row.
            "can_send": drawer.state == DrawerState.RECEIVED.value,
            "piece": piece_card,
        }

    # ── the batch send (bugs #13, #14, #15) ──────────────────────────────────
    async def send_batch(self, *, drawer_ids: list[uuid.UUID], destination: str,
                         actor_id: uuid.UUID | None) -> dict:
        """Send MANY drawers — and the pieces in them — onward, in one action.

        WHY BATCH. The store does not release garments one at a time; it fills a
        bank of drawers and moves them together. The single-drawer transition
        endpoint made that N requests and N chances to lose track of which
        drawers had actually gone, which is what bug #14 is about.

        PARTIAL ACCEPT, LIKE THE PRODUCTION GATES. One drawer that is not ready
        must never lose the twenty that are — the same reasoning that makes gates
        2-4 of the production log per-piece rather than per-request. So every
        drawer lands in exactly one bucket, with the reason attached, and the
        good ones commit.

        DESTINATION IS NOT DECORATION. STITCHING sets SENDED, which is precisely
        what ProductionService._merge_ok reads (through is_sended) to release a
        piece into LINE_STITCHING — so this call is what unblocks that whole
        bunch of pieces. LINING records that the drawer went to the lining floor
        and leaves the stitching gate shut, because the garment is not ready for
        it yet.
        """
        dest = (destination or "").strip().upper()
        if dest not in SEND_DESTINATIONS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"destination must be one of {list(SEND_DESTINATIONS)}.")
        if not drawer_ids:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Select at least one drawer to send.")

        # De-duplicate but keep the caller's order, so the response reads back in
        # the order the operator ticked the boxes.
        wanted = list(dict.fromkeys(drawer_ids))
        rows = list((await self.db.execute(
            select(Drawer).where(Drawer.id.in_(wanted)))).scalars())
        by_id = {d.id: d for d in rows}

        sent: list[dict] = []
        not_ready: list[dict] = []
        not_found: list[str] = []
        now = datetime.now(timezone.utc)

        for did in wanted:
            drawer = by_id.get(did)
            if drawer is None:
                not_found.append(str(did))
                continue
            piece = (await self.db.get(Piece, drawer.current_piece_id)
                     if drawer.current_piece_id else None)

            if drawer.state == DrawerState.SENDED.value:
                # Already gone. Not an error worth failing a batch over — the
                # operator re-ticked a row — but it is not a fresh send either.
                not_ready.append({
                    "drawer_id": str(did), "drawer_code": drawer.code,
                    "state": drawer.state,
                    "reason": (f"Drawer {drawer.code} was already sent"
                               f"{' to ' + drawer.sent_to if drawer.sent_to else ''}.")})
                continue
            if drawer.state != DrawerState.RECEIVED.value:
                not_ready.append({
                    "drawer_id": str(did), "drawer_code": drawer.code,
                    "state": drawer.state,
                    "reason": (
                        f"Drawer {drawer.code} is {drawer.state} — it reaches "
                        f"RECEIVED automatically once its leather and lining are "
                        f"both scanned in. Scan the missing part first.")})
                continue

            drawer.state = DrawerState.SENDED.value
            drawer.sended_at = now
            drawer.sent_to = dest
            await self._audit(
                actor_id, BarcodeAuditAction.DRAWER_SENDED.value, drawer.id,
                {"piece": piece.code if piece else None, "state": drawer.state,
                 "destination": dest, "batch_size": len(wanted)})
            sent.append({
                "drawer_id": str(did), "drawer_code": drawer.code,
                "piece_code": piece.code if piece else None,
                "state": drawer.state, "sent_to": dest})

        await self.repo_commit()

        released = [s["piece_code"] for s in sent if s["piece_code"]]
        if sent and dest == "STITCHING":
            message = (f"Sent {len(sent)} drawer(s) to stitching — "
                       f"{len(released)} piece(s) released for line-stitching.")
        elif sent:
            message = (f"Sent {len(sent)} drawer(s) to lining. Their pieces stay "
                       f"blocked for line-stitching until they come back and are "
                       f"sent to stitching.")
        else:
            message = "Nothing sent — see `not_ready` for the reason on each."
        if not_ready:
            message += f" {len(not_ready)} drawer(s) were not ready."

        return {
            "destination": dest,
            "requested": len(wanted),
            "count_sent": len(sent),
            "sent": sent, "not_ready": not_ready, "not_found": not_found,
            "pieces_released": released,
            "message": message,
        }

    # ── the merge gate check (called by production before LINE_STITCHING) ─────
    async def is_sended(self, piece_id: uuid.UUID) -> bool:
        drawer = await self.drawer_for_piece(piece_id)
        return bool(drawer and drawer.state == DrawerState.SENDED.value)

    # ── recycle (called when PACKAGE_EXPORT logs) ────────────────────────────
    async def release_nocommit(self, piece_id: uuid.UUID) -> None:
        drawer = await self.drawer_for_piece(piece_id)
        if drawer:
            drawer.state = DrawerState.WAITING.value
            drawer.current_piece_id = None
            drawer.leather_in = False
            drawer.lining_in = False
            drawer.received_at = None
            drawer.sended_at = None
            # A recycled drawer has not been sent anywhere; a stale routing would
            # show the NEXT garment as already sent to stitching.
            drawer.sent_to = None
        # F11: clear BOTH sides of the piece↔drawer link. Previously only
        # drawer.current_piece_id was nulled, so after release the piece still
        # pointed at a drawer that no longer claimed it — the barcode payload and
        # the piece life-story disagreed permanently.
        piece = await self.db.get(Piece, piece_id)
        if piece is not None and getattr(piece, "drawer_id", None) is not None:
            piece.drawer_id = None

    async def _audit(self, actor_id, action, entity_id, after: dict) -> None:
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_id, action=action, entity_type="drawer",
            entity_id=entity_id, after=after, at=datetime.now(timezone.utc)))