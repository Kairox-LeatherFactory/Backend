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

COMPLETENESS NO LONGER TRUSTS THE STORED FLAG (the lining-bypass bug)
    `complete = leather_in and (lining_in or not piece.needs_lining)` was reading
    a flag written once at breakdown upload and never recomputed — and on the live
    database that flag is wrong for most of an order. A KNIT jacket flagged False
    was therefore "complete" on its leather alone: it received, it sent, the merge
    gate opened, and the garment ran the whole chain to PACKAGE_EXPORT having
    never had a lining cut.

    Every completeness decision in this file now goes through
    `_needs_lining()` / `_needs_lining_map()`, which resolve the requirement from
    ALL available evidence (core/lining_rules.py) — the stored flag, the SKU's
    lining colour, the style name, and any lining-cut event already on the piece.
    Signals can only ADD a requirement, never remove one, so nothing that was
    genuinely leather-only stops being sendable.
================================================================================
"""
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (BarcodeAuditAction, BarcodeType, DrawerPart,
                            DrawerState, ProductionStage)
from app.core.lining_rules import (LINING_COLOUR_FIELDS, lining_required,
                                   lining_required_sql, why_lining_required)
from app.core.store_display import STORE_ENTRY_STAGE, holding_label
from app.modules.barcode.models import BarcodeRegistry, Drawer
from app.modules.clients.models import SKU, Style
from app.modules.production.models import Piece



# ── THE STORE SCREEN'S ACTIVITY CLOCK ────────────────────────────────────────
# Every act on a drawer stamps `last_activity_at` through this one function, so
# there is exactly one place that decides what "recently used" means. Inlining
# `drawer.last_activity_at = now` at the five mutation sites is how the previous
# ordering (`coalesce(sended_at, received_at, created_at)`) drifted: two of those
# three columns are CLEARED on release, and neither the merge nor the part scan
# wrote any timestamp at all.
#
# It is deliberately NOT reset by release_nocommit. A drawer that has just
# shipped its garment is the single most recently worked-on drawer in the
# building, and the old ordering buried it.
def _touch(drawer, kind: str, *, now=None) -> None:
    """Record that something just happened to this drawer, and what."""
    drawer.last_activity_at = now or datetime.now(timezone.utc)
    drawer.last_activity_kind = kind


class DrawerService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── THE LINING QUESTION — one answer, used by every gate in this file ─────
    async def _needs_lining(self, piece: Piece | None) -> tuple[bool, str | None]:
        """(does this garment take a lining?, why) for ONE piece.

        Returns (True, reason) so a rejection can say WHY a garment whose stored
        flag reads False is still waiting for a lining — otherwise the gate looks
        to the operator like a system fault rather than an instruction.

        A piece with no row at all (a drawer holding nothing) is treated as
        needing a lining: unknown must never be the permissive answer at a gate
        that releases garments into the line.
        """
        if piece is None:
            return True, "the drawer holds no identified garment"

        row = (await self.db.execute(
            select(SKU, Style)
            .join(Style, Style.id == SKU.style_id)
            .where(SKU.id == piece.sku_id)
        )).first()
        sku, style = (row[0], row[1]) if row else (None, None)

        has_cut = await self._has_lining_cut(piece.id)
        colours = tuple(getattr(sku, f, None) for f in LINING_COLOUR_FIELDS) \
            if sku is not None else ()
        kwargs = {
            "stored_flag": bool(getattr(piece, "needs_lining", False)),
            "style_name": getattr(style, "name", None),
            "style_article": getattr(style, "article", None),
            "colour_values": colours,
            "has_lining_cut_event": has_cut,
            # The DM's release-time declaration, when the style has one. It
            # decides in both directions and is the ONLY term that can answer
            # False over a positive signal — see core/lining_rules.py.
            "explicit": getattr(style, "needs_lining", None) if style is not None
                        else None,
        }
        return lining_required(**kwargs), why_lining_required(**kwargs)

    async def _has_lining_cut(self, piece_id: uuid.UUID) -> bool:
        """Has a LINING_CUTTING event ever been logged for this piece?

        The one signal that settles the question outright in the affirmative:
        somebody physically cut a lining for this garment, so the garment has one,
        whatever the breakdown sheet said.
        """
        from app.modules.production.repository import ProductionRepository
        done = await ProductionRepository(self.db).completed_stage_codes(piece_id)
        return ProductionStage.LINING_CUTTING.value in done

    async def _needs_lining_map(self, piece_ids: list[uuid.UUID]) -> dict:
        """Batch form of _needs_lining — {piece_id: bool}. Two queries, not N+1.

        Used by list_labels, which renders up to 2000 drawers and must apply
        exactly the predicate send_batch enforces.
        """
        ids = [p for p in dict.fromkeys(piece_ids) if p is not None]
        if not ids:
            return {}

        rows = (await self.db.execute(
            select(Piece.id, Piece.needs_lining, SKU.knit_color, SKU.nylon_color,
                   Style.name, Style.article, Style.needs_lining)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .where(Piece.id.in_(ids))
        )).all()

        from app.modules.production.models import Operation, ProductionEvent
        cut_ids = set((await self.db.execute(
            select(ProductionEvent.piece_id)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .where(ProductionEvent.piece_id.in_(ids),
                   func.upper(Operation.code) == ProductionStage.LINING_CUTTING.value)
            .distinct()
        )).scalars())

        out = {pid: True for pid in ids}    # unknown piece → assume lined (safe)
        for pid, flag, knit, nylon, sname, sarticle, declared in rows:
            out[pid] = lining_required(
                stored_flag=bool(flag), style_name=sname, style_article=sarticle,
                colour_values=(knit, nylon), has_lining_cut_event=pid in cut_ids,
                explicit=declared)
        return out

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
                          code: str | None = None,
                          sort: str = "recent",
                          pin_codes: list[str] | None = None,
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
        # The lining requirement as SQL — the SAME rule the Python pass below and
        # send_batch apply, so the send queue can never offer a row the server
        # then refuses. It omits the lining-cut-event term (that needs a
        # correlated EXISTS); the Python pass adds it, and it can only ever ADD a
        # requirement, so the SQL is safe in the one direction that matters.
        lining_sql = lining_required_sql(
            Piece.needs_lining, Style.name, Style.article,
            (SKU.knit_color, SKU.nylon_color),
            explicit_col=Style.needs_lining)

        def _joins(stmt):
            """Piece → SKU → Style, all OUTER: a drawer holding nothing must still
            appear in a list that claims to be every drawer."""
            return (stmt
                    .outerjoin(Piece, Piece.id == Drawer.current_piece_id)
                    .outerjoin(SKU, SKU.id == Piece.sku_id)
                    .outerjoin(Style, Style.id == SKU.style_id))

        def _filtered(stmt):
            if state:
                stmt = stmt.where(Drawer.state == state)
            if code:
                # Bug-list item 6: search by drawer code. Case-insensitive
                # CONTAINS, not equality — the operator types "42" or "drw-004"
                # off a label, not the exact zero-padded code.
                stmt = stmt.where(func.upper(Drawer.code).like(
                    f"%{code.strip().upper()}%"))
            if seq_from is not None:
                stmt = stmt.where(Drawer.seq >= seq_from)
            if seq_to is not None:
                stmt = stmt.where(Drawer.seq <= seq_to)
            if has_piece is True:
                stmt = stmt.where(Drawer.current_piece_id.isnot(None))
            elif has_piece is False:
                stmt = stmt.where(Drawer.current_piece_id.is_(None))
            # "Show me what I can send right now" — the SEND QUEUE, and it has to
            # be the same predicate send_batch enforces or the screen offers a
            # queue the server disagrees with.
            #
            # That predicate is COMPLETENESS, not state == RECEIVED: a drawer
            # holding both parts auto-receives, but a genuinely leather-only piece
            # never gets a second part and so never reaches RECEIVED, while still
            # being complete and perfectly sendable.
            complete_sql = and_(
                Drawer.leather_in.is_(True),
                or_(Drawer.lining_in.is_(True), ~lining_sql),
            )
            not_gone = Drawer.state != DrawerState.SENDED.value
            if sendable is True:
                stmt = stmt.where(and_(complete_sql, not_gone))
            elif sendable is False:
                stmt = stmt.where(~and_(complete_sql, not_gone))
            return stmt

        # The COUNT carries the same joins. `sendable` filters on style/SKU
        # columns, so without them the count would reference tables it never
        # selected from — a cartesian product on Postgres and a different total
        # from the page it is supposed to be counting.
        total = int(await self.db.scalar(
            _filtered(_joins(
                select(func.count(Drawer.id)).select_from(Drawer)))) or 0)

        # ── ORDERING: the print sheet and the store screen want opposite ends ──
        # `seq` is the PRINT order — DRW-0001…DRW-0430, which is how labels are
        # produced and how an operator finds a physical drawer in the rack.
        #
        # `recent` is the STORE SCREEN order, and it is now the DEFAULT: a store
        # operator opens this list many times a day to see what the floor is
        # working on, and prints labels rarely. The print sheet asks for
        # `sort=seq` explicitly.
        #
        # WHAT "LATEST" USED TO MEAN, AND WHY IT WAS WRONG.
        #     coalesce(sended_at, received_at, created_at)
        # got all three of the cases this screen exists for:
        #   • A MERGED drawer has neither sended_at nor received_at, so a garment
        #     merged into it seconds ago fell through to created_at.
        #   • A STORE SCAN writes neither column — it moves leather_in/lining_in
        #     and state — so the single most common act on a drawer was invisible.
        #   • release_nocommit NULLS both columns, so the drawer that had JUST
        #     shipped sorted to the very bottom.
        # And created_at cannot be the fallback that saves it: a 200-drawer pool
        # is bootstrapped in one transaction and shares one timestamp, so it
        # returns the same arbitrary ten rows forever.
        #
        # `last_activity_at` is stamped by every act (see _touch and premint) and
        # is never cleared. `updated_at` backfills it for rows written before this
        # column existed — TimestampMixin sets onupdate=func.now(), so any drawer
        # ever touched by the ORM already carries a true activity time.
        activity = func.coalesce(Drawer.last_activity_at, Drawer.updated_at,
                                 Drawer.created_at)

        # PINNED CODES — the "recently searched" band (change-list item: drawers).
        # A search is a fact about one operator's SESSION, not about the factory,
        # so it is not stored: the client keeps its own recent-search list and
        # replays it here as `pin_codes`. That keeps a read endpoint a read
        # endpoint (the alternative wrote a row to the database on every list
        # call) and it costs one CASE expression.
        #
        # Pinned rows sort ABOVE everything, in the order the client sent them —
        # the client's list is already most-recent-first, and re-sorting it here
        # would throw that away.
        pinned = [c.strip().upper() for c in (pin_codes or []) if c and c.strip()]
        pin_rank = None
        if pinned:
            pin_rank = case(
                {c: i for i, c in enumerate(pinned)},
                value=func.upper(Drawer.code),
                else_=len(pinned),
            )

        if (sort or "recent").strip().lower() == "seq":
            order_by = (Drawer.seq.asc(),)
        else:
            # seq is the tiebreaker, not decoration: drawers merged in one
            # transaction share a timestamp to the microsecond, and without a
            # stable second key they reshuffle between pages of the same list.
            order_by = (activity.desc(), Drawer.seq.asc())
        if pin_rank is not None:
            order_by = (pin_rank.asc(),) + order_by

        rows = (await self.db.execute(
            _filtered(_joins(
                select(Drawer, BarcodeRegistry.id, BarcodeRegistry.code,
                       BarcodeRegistry.caption, BarcodeRegistry.status,
                       Piece.code.label("piece_code"), Piece.seq.label("piece_seq"),
                       Piece.needs_lining,
                       activity.label("activity_at"))
                .outerjoin(
                    BarcodeRegistry,
                    and_(BarcodeRegistry.drawer_id == Drawer.id,
                         BarcodeRegistry.type == BarcodeType.DRAWER.value))
            ))
            .order_by(*order_by)
            .limit(limit).offset(offset)
        )).all()

        # ONE batch resolution of the lining question for the whole page, adding
        # the lining-cut-event term the SQL could not carry.
        lining_map = await self._needs_lining_map(
            [r[0].current_piece_id for r in rows])

        pin_set = set(pinned)
        items = []
        for r in rows:
            drawer = r[0]
            pid = drawer.current_piece_id
            needs_lining = lining_map.get(pid, True) if pid else True
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
                # EFFECTIVE, not the stored flag — see _needs_lining_map.
                "needs_lining": bool(needs_lining),
                "lining_reason": None,
                "complete": bool(complete),
                # bug #13: the garment in the drawer, so the list is choosable.
                "piece_id": drawer.current_piece_id,
                "piece_code": r.piece_code,
                "piece_serial": (f"{r.piece_seq:03d}"
                                 if r.piece_seq is not None else None),
                # SAME PREDICATE THE SEND USES. If this said "state == RECEIVED"
                # while send_batch accepts any complete drawer, the list would
                # grey out rows the server would happily take — and a leather-only
                # garment would look permanently stuck to the operator.
                "can_send": bool(complete)
                            and drawer.state != DrawerState.SENDED.value,
                "barcode_id": r[1],
                "barcode": r[2],
                "caption": r[3],
                "barcode_status": r[4],
                # WHEN this drawer was last worked on, and WHY it is where it is
                # in the list. A row at the top saying "sent · 2 min ago" is
                # self-explaining; a bare timestamp is not.
                "last_activity_at": r.activity_at,
                "last_activity": drawer.last_activity_kind,
                "pinned": bool(pinned) and (drawer.code or "").upper() in pin_set,
            })
        return {"total": total, "count": len(items), "items": items}

    # ── MAY THIS PART GO IN A DRAWER AT ALL? (the store-entry gate) ──────────
    async def _assert_ready_for_store(self, piece: Piece, part: DrawerPart,
                                      done: set[str]) -> None:
        """The piece's cut side must be FINISHED before that part may be stored.

            LEATHER  requires PASTING          (cut → fused → pasted)
            LINING   requires LINING_CUTTING

        WHY THIS EXISTS. The store is the merge point of the two cut paths, and
        core/store_display.py has always said so — `_CUT_SIDE_TERMINALS` is the
        set of stages after which a piece reads as "in store". But that was a
        READ overlay only: store_scan checked the merge map and the drawer's
        lifecycle state, and nothing at all about the piece's own progress. A
        piece minted an hour ago with zero production events could be scanned
        straight into its drawer, which then read HOLDING LEATHER over an empty
        slot. Nothing downstream could tell that apart from a real one: the
        drawer auto-RECEIVED on HOLDING_BOTH, the DM sent it, and the merge gate
        opened LINE_STITCHING for a garment that had never been pasted.

        So the rule is now enforced where it is WRITTEN, not only where it is
        displayed, and both ends read the same constant.

        IT APPLIES TO AN EXPLICIT `part` TOO, not just an inferred one. A gate
        that a caller can skip by naming the bucket itself is not a gate — and
        the explicit path is the one the barcode screen uses.
        """
        required = STORE_ENTRY_STAGE[part.value]
        if required in done:
            return
        side = "leather" if part is DrawerPart.LEATHER else "lining"
        # Name what the piece HAS done: "not pasted yet" is actionable, "rejected"
        # is not, and the operator standing at the drawer is the person who has to
        # work out where the garment actually is.
        progress = (f"it has completed {', '.join(sorted(done))}"
                    if done else "it has no logged production yet")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{piece.code} cannot be stored as {side} yet — {required} has not "
            f"been logged for it ({progress}). The store is where the two cut "
            f"paths meet: leather goes in after PASTING, lining after "
            f"LINING_CUTTING. Log the missing stage first.")

    # ── which bucket does this scan belong in? (bug #18) ─────────────────────
    async def infer_part(self, drawer: Drawer, piece: Piece,
                         *, done: set[str] | None = None) -> DrawerPart:
        """Work out whether this scan is the LEATHER or the LINING arriving.

        BUG #18 — the operator used to click "Hold Leather" or "Hold Lining" by
        hand after selecting the drawer. That button is a question the system can
        already answer: the piece's own production history says which side of the
        cut it has been through, and the drawer says which side it is still
        missing. A hand-picked bucket is only ever a chance to pick the wrong one.

        The order below is "what does the evidence say", then "what is missing":

          1. The piece has a LINING_CUTTING event and the drawer has no lining in
             → this is that lining arriving.
          2. The piece has PASTING and no leather in → the leather.
          3. Neither side is READY → fill whichever is still empty, and let
             _assert_ready_for_store reject it by name.
          4. Both sides already in → 409; there is nothing left to put anywhere.

        RULE 2 NOW ASKS FOR PASTING, NOT "any leather-side event". It used to
        accept LEATHER_CUTTING or FUSING as evidence that the leather had
        arrived, which is how a piece that was cut but not yet pasted could be
        bucketed as leather and stored. The store-entry gate is the authority on
        WHETHER a part may go in; this only picks WHICH side a legal scan fills,
        and the two must agree on what "the leather is ready" means or inference
        would keep proposing buckets the gate then refuses.

        Rule 3 no longer papers over a factory that logs no events: it picks the
        empty side so the rejection can name the stage that is actually missing,
        rather than choosing arbitrarily and reporting the wrong one.

        `done` is passed in by store_scan so the piece's history is read ONCE per
        scan and the bucket and the gate cannot see different histories.
        """
        if drawer.leather_in and drawer.lining_in:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Drawer {drawer.code} already holds both leather and lining for "
                f"{piece.code} — there is nothing further to scan in.")

        if done is None:
            from app.modules.production.repository import ProductionRepository
            done = await ProductionRepository(self.db).completed_stage_codes(piece.id)

        if ProductionStage.LINING_CUTTING.value in done and not drawer.lining_in:
            return DrawerPart.LINING
        if ProductionStage.PASTING.value in done and not drawer.leather_in:
            return DrawerPart.LEATHER
        return DrawerPart.LEATHER if not drawer.leather_in else DrawerPart.LINING

    # ── store-scan ───────────────────────────────────────────────────────────
    async def store_scan(self, *, drawer_id: uuid.UUID, piece_id: uuid.UUID,
                         part: DrawerPart | None = None,
                         actor_id: uuid.UUID | None = None,
                         employee_id: uuid.UUID | None = None) -> dict:
        """Record a part arriving in its drawer.

        TWO IDENTITIES, TWO PARAMETERS — and they are not interchangeable.

            actor_id     the LOGIN that performed this action  → app_user.id
            employee_id  the WORKER whose card was scanned     → employee.id

        They were briefly the same parameter, with the scanned worker passed as
        `actor_id`. That id then reached `_audit`, which writes
        `AuditLog.actor_user_id` — a foreign key to `app_user.id`. An employee is
        not a user, so Postgres rejected the row with "employee barcode ID is not
        found in the app_user table", and it only fired on the scan that COMPLETED
        a drawer, because the auto-RECEIVED branch is the only path here that
        audits. First scan fine, second one a 500.

        SQLite does not enforce foreign keys unless PRAGMA foreign_keys=ON, which
        is why the whole test suite went green on it. The regression test turns the
        pragma on for exactly this reason.

        The worker is not lost — they are DATA about the action, and belong in the
        audit payload and the response, which is where they now are.
        """
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
        #
        # ONE read of the piece's history, shared by the bucket choice and the
        # gate below: two reads could disagree if an event landed between them,
        # and the pair would then store a part the gate had just approved for the
        # other side.
        from app.modules.production.repository import ProductionRepository
        done = await ProductionRepository(self.db).completed_stage_codes(piece.id)

        inferred = part is None
        if part is None:
            part = await self.infer_part(drawer, piece, done=done)

        # THE STORE-ENTRY GATE. Leather may not enter a drawer before PASTING,
        # lining before LINING_CUTTING — checked for the explicit part as well as
        # the inferred one, or naming the bucket would skip the gate.
        await self._assert_ready_for_store(piece, part, done)

        if part is DrawerPart.LEATHER:
            drawer.leather_in = True
        else:
            drawer.lining_in = True
        # The part scan is the event the store screen most needs to see and the
        # only one that previously wrote NO timestamp anywhere.
        _touch(drawer, "scanned")

        needs_lining, lining_reason = await self._needs_lining(piece)
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

        # ── HOLDING BOTH AUTO-ADVANCES TO RECEIVED ───────────────────────────
        # RECEIVED used to be a button pressed on one drawer at a time, and it
        # asserts one thing the scan itself establishes, so it advances by itself.
        #
        # BUT ONLY ON HOLDING_BOTH — physically both parts in the drawer.
        #
        # It first triggered on `complete`, which is "leather in AND (lining in OR
        # the piece needs no lining)". That let the LEATHER-ONLY branch fire on a
        # single scan: a piece whose needs_lining flag was False went straight to
        # RECEIVED the moment its leather was stored, right after pasting, with an
        # empty lining side. On the floor that reads as the drawer receiving
        # itself before the lining has arrived.
        #
        # AND THAT FLAG CANNOT CARRY THIS DECISION. needs_lining is written once
        # at breakdown upload and never recomputed — on the live database 925 of
        # 1,425 pieces in one order are flagged wrongly (see
        # scripts/backfill_needs_lining.py). Auto-advancing a drawer on a value we
        # know to be unreliable means auto-advancing on a guess. Two booleans set
        # by two physical scans are not a guess.
        #
        # A GENUINELY LEATHER-ONLY PIECE IS NOT STRANDED. It stays HOLDING_LEATHER
        # with `ready_for_received` true, and a human confirms it through
        # POST /drawers/{id}/receive — which still validates completeness, so it
        # accepts exactly this case and nothing weaker. The judgement call ("this
        # garment really has no lining") stays with the person who can see the
        # garment, which is the right place for it.
        #
        # SEND REMAINS MANUAL EITHER WAY. Receiving records what is in the drawer;
        # sending releases the piece into the next stage. See send_batch.
        auto_received = False
        if drawer.leather_in and drawer.lining_in:
            drawer.state = DrawerState.RECEIVED.value
            drawer.received_at = datetime.now(timezone.utc)
            _touch(drawer, "received", now=drawer.received_at)
            auto_received = True
            await self._audit(
                actor_id, BarcodeAuditAction.DRAWER_RECEIVED.value, drawer.id,
                {"piece": piece.code, "state": drawer.state, "auto": True,
                 # The worker who physically put the part in. Recorded as data,
                 # NOT as actor_user_id — see the docstring above.
                 "employee_id": str(employee_id) if employee_id else None,
                 "part": part.value})

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
            # Why a lining is expected when the stored flag says otherwise. The
            # operator must be able to tell "the system is confused" apart from
            # "go and find the knit lining for this jacket".
            "lining_reason": lining_reason,
            "awaiting": awaiting, "ready_for_received": complete,
            "part": part.value,
            # True when the bucket was decided by the server, not the operator —
            # so the screen can show WHICH bucket it chose (bug #18).
            "part_inferred": inferred,
            # Echoed back so the screen can confirm WHO was credited with the scan.
            "employee_id": str(employee_id) if employee_id else None,
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
            # THREE OUTCOMES NOW, not two, because auto-receive needs both parts
            # while completeness does not. The middle one is the leather-only
            # piece: it is ready, but a person confirms it rather than the flag.
            "next_action": self._next_action(
                piece_code=piece.code, drawer_code=drawer.code,
                auto_received=auto_received, complete=complete,
                awaiting=awaiting),
        }

    @staticmethod
    def _next_action(*, piece_code: str, drawer_code: str, auto_received: bool,
                     complete: bool, awaiting: list[str]) -> str:
        """One sentence telling the operator what actually happens next."""
        if auto_received:
            return (f"{piece_code} is in drawer {drawer_code}, which now holds "
                    f"both parts and has been received. Select it in the Drawers "
                    f"List and Send to Lining / Stitching to move it on.")
        if complete:
            # Leather-only: nothing more is coming, but the drawer does not
            # receive itself on one part. Name the confirmation that is needed
            # instead of leaving it looking stuck.
            return (f"{piece_code} needs no lining, so drawer {drawer_code} holds "
                    f"everything it will get. Confirm receipt on the drawer, then "
                    f"send it.")
        return (f"{piece_code} is logged in drawer {drawer_code}. Still awaiting "
                f"{' + '.join(awaiting)} before it can be received.")

    # ── received / sended ────────────────────────────────────────────────────
    async def transition(self, drawer_id: uuid.UUID, transition: str,
                         actor_id: uuid.UUID | None) -> dict:
        drawer = await self.get(drawer_id)
        if not drawer:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Drawer not found.")
        piece = await self.db.get(Piece, drawer.current_piece_id) if drawer.current_piece_id else None
        needs_lining, lining_reason = await self._needs_lining(piece)
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
                because = f" ({lining_reason})" if missing == "lining" and lining_reason else ""
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Cannot RECEIVE: still awaiting {missing} in drawer "
                    f"{drawer.code}{because}.")
            drawer.state = DrawerState.RECEIVED.value
            drawer.received_at = datetime.now(timezone.utc)
            _touch(drawer, "received", now=drawer.received_at)
            action = BarcodeAuditAction.DRAWER_RECEIVED.value

        elif t == "SENDED":
            if drawer.state != DrawerState.RECEIVED.value:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Cannot SEND before RECEIVED. Set RECEIVED first.")
            drawer.state = DrawerState.SENDED.value
            drawer.sended_at = datetime.now(timezone.utc)
            _touch(drawer, "sent", now=drawer.sended_at)
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
        needs_lining, lining_reason = True, None
        if drawer.current_piece_id:
            from app.modules.barcode.service import BarcodeService
            piece_card = await BarcodeService(self.db)._piece_payload(
                drawer.current_piece_id)
            # NOT piece_card["needs_lining"] — that is the stored flag, which is
            # exactly what let a lined garment through the gate.
            piece = await self.db.get(Piece, drawer.current_piece_id)
            needs_lining, lining_reason = await self._needs_lining(piece)

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
            "lining_reason": lining_reason,
            "awaiting": awaiting,
            "complete": bool(complete),
            "received_at": drawer.received_at,
            "sended_at": drawer.sended_at,
            "sent": drawer.state == DrawerState.SENDED.value,
            # What the list's Send button should do with this row.
            # Same predicate as send_batch — see the note in list_labels.
            "can_send": bool(complete) and drawer.state != DrawerState.SENDED.value,
            "piece": piece_card,
        }

    # ── the batch send (bugs #13, #14, #15) ──────────────────────────────────
    async def send_batch(self, *, drawer_ids: list[uuid.UUID],
                         actor_id: uuid.UUID | None) -> dict:
        """Send MANY drawers — and the pieces in them — onward, in one action.

        WHY BATCH. The store does not release garments one at a time; it fills a
        bank of drawers and moves them together. The single-drawer transition
        endpoint made that N requests and N chances to lose track of which
        drawers had actually gone.

        THERE IS NO DESTINATION TO CHOOSE, and asking for one was a modelling
        mistake. The store sits at ONE point in the pipeline:

            leather cut ─┐
                         ├─► drawer (merge) ─► LINE_STITCHING ─► SHELL_STITCHING
            lining cut ──┘                     ─► FINAL_FINISH ─► ...

        Lining is UPSTREAM: the lining is cut and then scanned INTO the drawer.
        A drawer that holds both parts has exactly one way forward, so "send to
        lining" would mean sending a garment backwards to a stage it has already
        cleared. Sending simply releases the piece into line-stitching — which is
        precisely what ProductionService._merge_ok reads, through is_sended.

        PARTIAL ACCEPT, LIKE THE PRODUCTION GATES. One drawer that is not ready
        must never lose the twenty that are — the same reasoning that makes gates
        2-4 of the production log per-piece rather than per-request. So every
        drawer lands in exactly one bucket, with the reason attached, and the
        good ones commit.
        """
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
                    "reason": f"Drawer {drawer.code} was already sent."})
                continue

            # THE GATE IS COMPLETENESS, NOT THE LITERAL 'RECEIVED' STATE.
            #
            # This required state == RECEIVED, and that quietly stranded a whole
            # class of garment. Auto-receive fires only on HOLDING_BOTH — two
            # physical scans — so a piece that needs no lining never gets a second
            # part, never auto-receives, and could never be sent. Its drawer sat
            # complete and immovable, and nothing in the UI could free it, so the
            # garment never reached line-stitching at all. On the live database
            # that flag covers most pieces.
            #
            # Completeness is the real question a send asks: does this drawer hold
            # everything its garment needs? It SUBSUMES the old check — a drawer
            # only ever reached RECEIVED by being complete — so nothing that used
            # to be sendable stops being sendable, and the stranded case is freed.
            #
            # THIS IS THE GATE THE LINING BYPASS WALKED THROUGH. `needs_lining`
            # here used to be the stored flag; a KNIT jacket flagged False was
            # "complete" on its leather alone, so SEND set the drawer to SENDED,
            # the production merge gate read SENDED and opened, and the garment
            # ran to PACKAGE_EXPORT with no lining ever cut. _needs_lining()
            # resolves the requirement from every signal instead.
            needs_lining, lining_reason = await self._needs_lining(piece)
            complete = drawer.leather_in and (drawer.lining_in or not needs_lining)
            if not complete:
                missing = "leather" if not drawer.leather_in else "lining"
                because = (f" This garment takes a lining because {lining_reason}."
                           if missing == "lining" and lining_reason else "")
                not_ready.append({
                    "drawer_id": str(did), "drawer_code": drawer.code,
                    "state": drawer.state,
                    "needs_lining": bool(needs_lining),
                    "reason": (
                        f"Drawer {drawer.code} is still awaiting its {missing}. "
                        f"Scan the missing part into it before sending.{because}")})
                continue

            drawer.state = DrawerState.SENDED.value
            drawer.sended_at = now
            _touch(drawer, "sent", now=now)
            # A complete drawer that never passed through RECEIVED (the
            # leather-only case) is received at the moment it is sent — otherwise
            # the audit trail would show a garment released with no record of it
            # ever having been confirmed complete.
            if drawer.received_at is None:
                drawer.received_at = now
            await self._audit(
                actor_id, BarcodeAuditAction.DRAWER_SENDED.value, drawer.id,
                {"piece": piece.code if piece else None, "state": drawer.state,
                 "batch_size": len(wanted)})
            sent.append({
                "drawer_id": str(did), "drawer_code": drawer.code,
                "piece_code": piece.code if piece else None,
                "state": drawer.state})

        await self.repo_commit()

        released = [s["piece_code"] for s in sent if s["piece_code"]]
        if sent:
            message = (f"Sent {len(sent)} drawer(s) — {len(released)} piece(s) "
                       f"released for line-stitching.")
        else:
            message = "Nothing sent — see `not_ready` for the reason on each."
        if not_ready:
            message += f" {len(not_ready)} drawer(s) were not ready."

        return {
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
            # STAMPED, NOT CLEARED. received_at/sended_at are wiped because the
            # drawer is being handed back to the pool empty, but the fact that
            # something happened to it a second ago is exactly what the store
            # screen's "latest" list is asking about — and clearing those two
            # columns is precisely what used to bury a just-shipped drawer at the
            # bottom of it.
            _touch(drawer, "released")
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