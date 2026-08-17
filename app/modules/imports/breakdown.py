"""
================================================================================
modules/imports/breakdown.py — the uploaded breakdown as an editable table,
                               and the AUDITED release into production
================================================================================

THE TWO-PHASE COMMIT (change-list item 9)

    BEFORE
        POST /imports/commit parsed the sheet AND minted a per-piece barcode and
        a drawer for every ordered unit, in one irreversible step. A barcode is a
        permanent garment identity and the drawer pool is finite, so a sheet
        uploaded for review consumed both for styles nobody had agreed to cut.

    NOW
        1. POST /imports/commit          writes styles + SKUs. Nothing is minted.
                                         Every style lands DRAFT.
        2. GET/PATCH/DELETE /imports/breakdown/...   the DM corrects the table.
        3. POST /imports/breakdown/release           the DM names the styles that
                                         go to production. ONLY THEN are pieces,
                                         barcodes and drawer merges created.

    RELEASE IS A HARD, AUDITED TRANSITION — a `production_status` enum plus an
    audit_log row, never a boolean `is_released` (CLAUDE.md §15). It records who
    released what and when, because it is the moment the factory commits material
    and floor time to a style.

    EDITS ARE DRAFT-ONLY, AND THAT IS THE POINT. Once a style is RELEASED its
    SKUs have minted pieces whose barcodes are printed and scanned. Changing a
    released SKU's quantity or colour would rewrite the identity of garments
    already on the floor, so the API refuses it (409) and tells the caller to
    raise the quantity through a re-release instead, which TOPS UP (premint is
    idempotent) rather than rewriting.

PHASE-2 SEAM (the convergence risk flagged in the change list)
    `release_styles` is the ONE contract that turns a breakdown into production.
    When Phase 2 auto-generates a breakdown from a BOM, it writes the same DRAFT
    style/SKU rows and calls this same method. There must never be a second
    ingestion path that mints pieces — if you find yourself adding one, you are
    building the divergence this seam exists to prevent.

WHY SYNC INSIDE AN ASYNC SERVICE
    premint_order speaks the synchronous Session API (it shares the four-phase
    insert ordering with the importer, see premint.py). Rather than port that,
    `release_styles` runs it in a worker thread via run_in_threadpool — the same
    pattern imports/router.py already uses for the loader, so the event loop is
    never blocked and the proven insert ordering is untouched.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.enums import ProductionReleaseStatus
from app.core.lining_rules import lining_required
from app.modules.clients.models import SKU, ClientOrder, Style
from app.modules.production.models import Piece

# Only these transitions exist. CANCELLED is terminal for the style; RELEASED is
# terminal in the sense that it never goes back (pieces exist and are scanned).
_RELEASABLE = {ProductionReleaseStatus.DRAFT.value}


class BreakdownService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── the table ────────────────────────────────────────────────────────────
    async def get_table(self, order_number: str) -> dict:
        """The uploaded breakdown as a table: styles, their SKUs, and status.

        THIS IS THE SCREEN THE DM WORKS ON before anything is minted. Every row
        carries `production_status` so the UI can render DRAFT rows as editable
        and RELEASED rows as read-only, and `minted_pieces` so a released style
        shows what it actually produced rather than only what was ordered.
        """
        order = await self._order(order_number)

        style_rows = (await self.db.execute(
            select(Style).where(Style.client_order_id == order.id)
            .order_by(Style.name.asc())
        )).scalars().all()
        style_ids = [s.id for s in style_rows]

        sku_rows = (await self.db.execute(
            select(SKU).where(SKU.style_id.in_(style_ids)).order_by(
                SKU.color_name.asc(), SKU.size.asc())
        )).scalars().all() if style_ids else []

        minted = dict((await self.db.execute(
            select(SKU.style_id, func.count(Piece.id))
            .join(Piece, Piece.sku_id == SKU.id)
            .where(SKU.style_id.in_(style_ids))
            .group_by(SKU.style_id)
        )).all()) if style_ids else {}

        by_style: dict[uuid.UUID, list] = {}
        for sku in sku_rows:
            by_style.setdefault(sku.style_id, []).append(sku)

        styles = []
        for st in style_rows:
            skus = by_style.get(st.id, [])
            styles.append({
                "style_id": st.id,
                "style_code": st.code,
                "style_name": st.name,
                "article": st.article,
                "production_status": st.production_status
                                     or ProductionReleaseStatus.DRAFT.value,
                "released_at": st.released_at,
                "released_by": st.released_by,
                "editable": (st.production_status
                             or ProductionReleaseStatus.DRAFT.value) in _RELEASABLE,
                # The effective lining verdict for the whole style, so the DM sees
                # BEFORE releasing whether these garments will need a lining leg
                # in the store. Same rule the completeness gate applies.
                "needs_lining": lining_required(
                    style_name=st.name, style_article=st.article),
                "sku_count": len(skus),
                "qty_ordered": sum(int(s.qty_ordered or 0) for s in skus),
                "minted_pieces": int(minted.get(st.id, 0)),
                "skus": [{
                    "sku_id": s.id,
                    "sku_code": s.code,
                    "colour": s.color_name or s.color_code,
                    "color_code": s.color_code,
                    "size": s.size,
                    "qty_ordered": int(s.qty_ordered or 0),
                    "knit_color": s.knit_color,
                    "nylon_color": s.nylon_color,
                } for s in skus],
            })

        drafts = [s for s in styles
                  if s["production_status"] == ProductionReleaseStatus.DRAFT.value]
        return {
            "order_id": order.id,
            "order_number": order.order_number,
            "styles": styles,
            "totals": {
                "styles": len(styles),
                "styles_draft": len(drafts),
                "styles_released": sum(
                    1 for s in styles
                    if s["production_status"] == ProductionReleaseStatus.RELEASED.value),
                "qty_ordered": sum(s["qty_ordered"] for s in styles),
                "qty_draft": sum(s["qty_ordered"] for s in drafts),
                "minted_pieces": sum(s["minted_pieces"] for s in styles),
            },
        }

    # ── CRUD on the DRAFT sheet ──────────────────────────────────────────────
    async def update_sku(self, sku_id: uuid.UUID, patch: dict) -> dict:
        """Correct one line of a DRAFT breakdown. 409 once the style is released.

        Only the four fields a breakdown sheet actually gets wrong are editable:
        quantity, colour name/code, and size. Anything structural (which style a
        SKU belongs to) is a re-upload, not an edit.
        """
        sku, style = await self._sku_with_style(sku_id)
        self._assert_draft(style, "edit")

        allowed = {"qty_ordered", "color_name", "color_code", "size",
                   "knit_color", "nylon_color"}
        changed = {}
        for key, value in patch.items():
            if key not in allowed or value is None:
                continue
            if key == "qty_ordered":
                value = int(value)
                if value < 0:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "qty_ordered cannot be negative.")
            setattr(sku, key, value)
            changed[key] = value

        if not changed:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Nothing to update.")
        await self._audit("BREAKDOWN_SKU_UPDATED", sku.id,
                          {"style_id": str(style.id), "changed": changed})
        await self.db.commit()
        await self.db.refresh(sku)
        return {"sku_id": sku.id, "sku_code": sku.code,
                "qty_ordered": int(sku.qty_ordered or 0),
                "colour": sku.color_name or sku.color_code, "size": sku.size,
                "changed": changed}

    async def delete_sku(self, sku_id: uuid.UUID) -> dict:
        """Drop a line the sheet should not have had. DRAFT only.

        A hard delete is safe here and ONLY here: a DRAFT SKU has no pieces, no
        barcodes and no production events, because nothing is minted until
        release. The 409 below is what keeps that true.
        """
        sku, style = await self._sku_with_style(sku_id)
        self._assert_draft(style, "delete")

        has_pieces = await self.db.scalar(
            select(func.count(Piece.id)).where(Piece.sku_id == sku.id))
        if has_pieces:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{sku.code} already has {int(has_pieces)} minted piece(s) and "
                f"cannot be deleted — those barcodes are printed. Set its "
                f"quantity to 0 instead.")

        from app.modules.clients.models import SkuOrderLine
        for line in (await self.db.execute(
                select(SkuOrderLine).where(SkuOrderLine.sku_id == sku.id))).scalars():
            await self.db.delete(line)
        code = sku.code
        await self.db.delete(sku)
        await self._audit("BREAKDOWN_SKU_DELETED", sku_id,
                          {"style_id": str(style.id), "sku_code": code})
        await self.db.commit()
        return {"deleted": True, "sku_id": str(sku_id), "sku_code": code}

    async def cancel_styles(self, style_ids: list[uuid.UUID], *,
                            user_name: str) -> dict:
        """Withdraw DRAFT styles so they stop showing on the release screen."""
        out = {"cancelled": [], "rejected": []}
        for sid in dict.fromkeys(style_ids):
            style = await self.db.get(Style, sid)
            if style is None:
                out["rejected"].append({"style_id": str(sid),
                                        "reason": "Style not found."})
                continue
            current = style.production_status or ProductionReleaseStatus.DRAFT.value
            if current != ProductionReleaseStatus.DRAFT.value:
                out["rejected"].append({
                    "style_id": str(sid), "style_code": style.code,
                    "reason": f"{style.name} is {current} — only a DRAFT style "
                              f"can be cancelled."})
                continue
            style.production_status = ProductionReleaseStatus.CANCELLED.value
            await self._audit("BREAKDOWN_STYLE_CANCELLED", style.id,
                              {"style_code": style.code, "by": user_name})
            out["cancelled"].append({"style_id": str(sid),
                                     "style_code": style.code})
        await self.db.commit()
        return out

    # ── THE RELEASE ──────────────────────────────────────────────────────────
    async def release_styles(self, order_number: str, style_ids: list[uuid.UUID],
                             *, user_name: str,
                             allow_pool_growth: bool = False) -> dict:
        """Release named styles into production: mint pieces, barcodes, drawers.

        PARTIAL ACCEPT, like every other batch surface in this codebase. One
        already-released style must not lose the four the DM ticked with it, so
        each style lands in `released` or `rejected` with its reason.

        THE DRAWER POOL IS FINITE AND THIS IS WHERE THAT BITES. If the released
        pieces outrun the free drawers, the remainder are minted WITHOUT a drawer
        and reported as `pieces_waiting_for_drawer`. They have barcodes and
        identities; they simply cannot be stored, and therefore cannot pass the
        merge gate, until a drawer frees up or DM/MD grows the pool through
        POST /drawers/pool. That is a deliberate, visible stall rather than a
        silent unbounded pool.
        """
        order = await self._order(order_number)
        wanted = list(dict.fromkeys(style_ids))
        if not wanted:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Select at least one style to release.")

        releasable: list[uuid.UUID] = []
        rejected: list[dict] = []
        for sid in wanted:
            style = await self.db.get(Style, sid)
            if style is None or style.client_order_id != order.id:
                rejected.append({"style_id": str(sid),
                                 "reason": f"No style {sid} on order "
                                           f"{order.order_number}."})
                continue
            current = style.production_status or ProductionReleaseStatus.DRAFT.value
            if current != ProductionReleaseStatus.DRAFT.value:
                rejected.append({
                    "style_id": str(sid), "style_code": style.code,
                    "reason": f"{style.name} is already {current}. Releasing "
                              f"again would mint nothing new."})
                continue
            qty = int(await self.db.scalar(
                select(func.coalesce(func.sum(SKU.qty_ordered), 0))
                .where(SKU.style_id == sid)) or 0)
            if qty <= 0:
                rejected.append({
                    "style_id": str(sid), "style_code": style.code,
                    "reason": f"{style.name} has no ordered quantity — there is "
                              f"nothing to mint."})
                continue
            releasable.append(sid)

        if not releasable:
            return {"order_number": order.order_number, "released": [],
                    "rejected": rejected, "minted": {},
                    "message": "Nothing released — see `rejected` for each reason."}

        # THE MINT. Sync, in a worker thread, on its own Session — same pattern
        # as the loader (imports/router.py). It commits itself.
        stats = await run_in_threadpool(
            _release_sync, order.id, releasable, user_name, allow_pool_growth)

        released = []
        for sid in releasable:
            style = await self.db.get(Style, sid)
            await self.db.refresh(style)
            released.append({"style_id": str(sid), "style_code": style.code,
                             "style_name": style.name,
                             "production_status": style.production_status})
            await self._audit("BREAKDOWN_STYLE_RELEASED", sid, {
                "style_code": style.code, "order_number": order.order_number,
                "by": user_name, "pieces_minted": stats.get("pieces_minted", 0)})
        await self.db.commit()

        waiting = int(stats.get("pieces_waiting_for_drawer", 0))
        message = (f"Released {len(released)} style(s); "
                   f"{stats.get('pieces_minted', 0)} piece barcode(s) minted.")
        if waiting:
            message += (f" {waiting} piece(s) have NO DRAWER — the pool is full. "
                        f"They cannot be stored until a drawer frees up or DM/MD "
                        f"adds drawers (POST /drawers/pool).")
        return {"order_number": order.order_number, "released": released,
                "rejected": rejected, "minted": stats, "message": message}

    # ── helpers ──────────────────────────────────────────────────────────────
    async def _order(self, order_number: str) -> ClientOrder:
        order = await self.db.scalar(
            select(ClientOrder).where(
                ClientOrder.order_number == (order_number or "").strip()))
        if order is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"No order '{order_number}'.")
        return order

    async def _sku_with_style(self, sku_id: uuid.UUID) -> tuple[SKU, Style]:
        sku = await self.db.get(SKU, sku_id)
        if sku is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found.")
        style = await self.db.get(Style, sku.style_id)
        if style is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "SKU has no parent style.")
        return sku, style

    @staticmethod
    def _assert_draft(style: Style, verb: str) -> None:
        current = style.production_status or ProductionReleaseStatus.DRAFT.value
        if current in _RELEASABLE:
            return
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot {verb}: {style.name} is {current}. Its pieces already carry "
            f"printed barcodes, so the breakdown behind them is frozen. To make "
            f"more of this style, raise the SKU quantity on a NEW upload and "
            f"release again — release tops up, it never rewrites.")

    async def _audit(self, action: str, entity_id, after: dict) -> None:
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=None, action=action, entity_type="breakdown",
            entity_id=entity_id, after=after, at=datetime.now(timezone.utc)))


def _release_sync(order_id, style_ids, user_name: str,
                  allow_pool_growth: bool) -> dict:
    """The mint itself, on a synchronous Session, in one transaction.

    Stamping the style RELEASED and minting its pieces MUST be atomic: a crash
    between them leaves either barcodes nobody released or a released style with
    no garments. Both are worse than the upload failing.
    """
    from app.core.database import SessionLocal
    from app.modules.imports.premint import premint_order

    db = SessionLocal()
    try:
        order = db.get(ClientOrder, order_id)
        stats = premint_order(db, order, style_ids=style_ids,
                              allow_pool_growth=allow_pool_growth)
        now = datetime.now(timezone.utc)
        for sid in style_ids:
            style = db.get(Style, sid)
            style.production_status = ProductionReleaseStatus.RELEASED.value
            style.released_at = now
            style.released_by = user_name
        db.commit()
        return stats
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
