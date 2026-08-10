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
                            DrawerState)
from app.modules.barcode.models import BarcodeRegistry, Drawer
from app.modules.production.models import Piece


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
                          limit: int = 500, offset: int = 0) -> dict:
        """Drawers + their DRAWER-type barcode, seq order — the print sheet.

        OUTER join on purpose. A drawer whose registry row is missing is a drawer
        nobody can scan; it must appear in the sheet with a null barcode so the
        gap is visible, not vanish from a list that claims to be every drawer.
        The join is pinned to type=DRAWER so a stale row of another type can
        never supply the code, and drawer codes are unique in the registry
        (uq_barcode_code) so one drawer yields at most one row.
        """
        def _filtered(stmt):
            if state:
                stmt = stmt.where(Drawer.state == state)
            if seq_from is not None:
                stmt = stmt.where(Drawer.seq >= seq_from)
            if seq_to is not None:
                stmt = stmt.where(Drawer.seq <= seq_to)
            return stmt

        total = int(await self.db.scalar(
            _filtered(select(func.count(Drawer.id)))) or 0)

        rows = (await self.db.execute(
            _filtered(
                select(Drawer, BarcodeRegistry.id, BarcodeRegistry.code,
                       BarcodeRegistry.caption, BarcodeRegistry.status)
                .outerjoin(
                    BarcodeRegistry,
                    and_(BarcodeRegistry.drawer_id == Drawer.id,
                         BarcodeRegistry.type == BarcodeType.DRAWER.value))
            )
            .order_by(Drawer.seq.asc())
            .limit(limit).offset(offset)
        )).all()

        items = [
            {
                "drawer_id": drawer.id,
                "seq": drawer.seq,
                "code": drawer.code,
                "state": drawer.state,
                "barcode_id": bc_id,
                "barcode": bc_code,
                "caption": caption,
                "barcode_status": bc_status,
            }
            for drawer, bc_id, bc_code, caption, bc_status in rows
        ]
        return {"total": total, "count": len(items), "items": items}

    # ── store-scan ───────────────────────────────────────────────────────────
    async def store_scan(self, *, drawer_id: uuid.UUID, piece_id: uuid.UUID,
                         part: DrawerPart) -> dict:
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

        if part is DrawerPart.LEATHER:
            drawer.leather_in = True
        else:
            drawer.lining_in = True

        needs_lining = bool(getattr(piece, "needs_lining", True))
        complete = drawer.leather_in and (drawer.lining_in or not needs_lining)
        # F07: name what is ACTUALLY in the drawer. A lining-first scan must not
        # report HOLDING_LEATHER.
        if complete:
            drawer.state = DrawerState.HOLDING_BOTH.value
        elif drawer.leather_in:
            drawer.state = DrawerState.HOLDING_LEATHER.value
        elif drawer.lining_in:
            drawer.state = DrawerState.HOLDING_LINING.value
        else:
            drawer.state = DrawerState.WAITING.value

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