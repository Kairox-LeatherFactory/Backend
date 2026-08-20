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
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.enums import ProductionReleaseStatus
from app.core.lining_rules import lining_required
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.production.models import Piece

# Only these transitions exist. CANCELLED is terminal for the style; RELEASED is
# terminal in the sense that it never goes back (pieces exist and are scanned).
_RELEASABLE = {ProductionReleaseStatus.DRAFT.value}


# The rollup a manager reads off the order list. It answers "can I click this
# row yet, and what will I find" in one word, from the styles' release states.
_NOT_UPLOADED = "NOT_UPLOADED"       # the order exists; no breakdown sheet yet
_DRAFT = "DRAFT"                     # sheet uploaded, nothing released
_PARTIAL = "PARTIALLY_RELEASED"      # some styles in production, some still draft
_RELEASED = "RELEASED"               # every live style released
_CANCELLED = "CANCELLED"             # every style cancelled


class BreakdownService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── the ORDER LIST ───────────────────────────────────────────────────────
    async def list_orders(self, *, q: str | None = None,
                          client_id: uuid.UUID | None = None,
                          status: str | None = None,
                          has_breakdown: bool | None = None,
                          limit: int = 50, offset: int = 0) -> dict:
        """EVERY ORDER, PERMANENTLY — the index the breakdown screen is reached
        through.

        WHY THIS EXISTS. POST /imports/commit answers with the order number, and
        that answer was the only place the number appeared: close the tab and the
        breakdown table at GET /imports/breakdown/{order_number} was unreachable
        unless somebody had written the number down. Nothing was actually lost —
        `client_order` is a permanent table and commit REQUIRES the order to
        already exist, so it never creates one — but there was no way to LIST
        orders. `GET /clients/{client_id}/orders` needs a client id you may not
        have, and the analytics explorer returns a nested tree, not a clickable
        index. This is that index.

        Each row carries `breakdown_status`, rolled up from its styles, so the
        list says which orders still need work before a manager opens any of
        them:

            NOT_UPLOADED         order created, no breakdown sheet committed yet
            DRAFT                sheet uploaded; nothing released, nothing minted
            PARTIALLY_RELEASED   some styles in production, some still editable
            RELEASED             every live style released and minting pieces
            CANCELLED            every style on the order was cancelled

        ORDERED BY MOST RECENTLY WORKED ON — the newest of the order's own
        created_at and the last style written under it — so an order whose sheet
        was uploaded this morning sits above one raised months ago and untouched.
        Ordering by order_date would sort by a date the CLIENT chose, which has
        nothing to do with what the factory is currently handling.

        ONE aggregate query for the counts, not one per order: a factory with two
        years of orders would otherwise issue several hundred round trips to
        paint one list.
        """
        # ── the per-order rollup, computed in SQL ───────────────────────────
        # LEFT joins throughout: an order with no styles is exactly the
        # NOT_UPLOADED case this list has to be able to show, and an inner join
        # would silently drop it.
        released_v = ProductionReleaseStatus.RELEASED.value
        cancelled_v = ProductionReleaseStatus.CANCELLED.value

        agg = (
            select(
                Style.client_order_id.label("order_id"),
                func.count(func.distinct(Style.id)).label("style_count"),
                func.sum(
                    case((Style.production_status == released_v, 1), else_=0)
                ).label("released_count"),
                func.sum(
                    case((Style.production_status == cancelled_v, 1), else_=0)
                ).label("cancelled_count"),
                func.max(Style.created_at).label("last_style_at"),
            )
            .group_by(Style.client_order_id)
            .subquery()
        )
        # SKU totals ride a SEPARATE subquery rather than another join on the one
        # above. Joining style→sku and then counting styles in the same select
        # multiplies the style count by the number of SKUs per style — the classic
        # fan-out that makes a 3-style order report 47 styles.
        sku_agg = (
            select(
                Style.client_order_id.label("order_id"),
                func.count(SKU.id).label("sku_count"),
                func.coalesce(func.sum(SKU.qty_ordered), 0).label("qty_ordered"),
            )
            .join(SKU, SKU.style_id == Style.id)
            .group_by(Style.client_order_id)
            .subquery()
        )
        piece_agg = (
            select(
                Style.client_order_id.label("order_id"),
                func.count(Piece.id).label("pieces_minted"),
            )
            .join(SKU, SKU.style_id == Style.id)
            .join(Piece, Piece.sku_id == SKU.id)
            .group_by(Style.client_order_id)
            .subquery()
        )

        style_count = func.coalesce(agg.c.style_count, 0)
        released_count = func.coalesce(agg.c.released_count, 0)
        cancelled_count = func.coalesce(agg.c.cancelled_count, 0)
        # "Live" = not cancelled. A fully released order must not be dragged back
        # to PARTIALLY_RELEASED by styles somebody cancelled on purpose.
        live_count = style_count - cancelled_count

        status_expr = case(
            (style_count == 0, _NOT_UPLOADED),
            (live_count == 0, _CANCELLED),
            (released_count == 0, _DRAFT),
            (released_count >= live_count, _RELEASED),
            else_=_PARTIAL,
        )
        activity = func.coalesce(agg.c.last_style_at, ClientOrder.created_at)

        stmt = (
            select(
                ClientOrder.id, ClientOrder.order_number, ClientOrder.order_date,
                ClientOrder.delivery_deadline, ClientOrder.ship_mode,
                ClientOrder.currency, ClientOrder.created_at,
                Client.id.label("client_id"), Client.name.label("client_name"),
                Client.code.label("client_code"),
                style_count.label("style_count"),
                released_count.label("released_count"),
                cancelled_count.label("cancelled_count"),
                func.coalesce(sku_agg.c.sku_count, 0).label("sku_count"),
                func.coalesce(sku_agg.c.qty_ordered, 0).label("qty_ordered"),
                func.coalesce(piece_agg.c.pieces_minted, 0).label("pieces_minted"),
                agg.c.last_style_at,
                status_expr.label("breakdown_status"),
                activity.label("last_activity_at"),
            )
            .join(Client, Client.id == ClientOrder.client_id)
            .outerjoin(agg, agg.c.order_id == ClientOrder.id)
            .outerjoin(sku_agg, sku_agg.c.order_id == ClientOrder.id)
            .outerjoin(piece_agg, piece_agg.c.order_id == ClientOrder.id)
        )

        conds = []
        if q:
            # CONTAINS, case-insensitive — a manager types "1579" or part of the
            # client's name off a printed sheet, not the exact stored string.
            needle = f"%{q.strip().upper()}%"
            conds.append(or_(
                func.upper(ClientOrder.order_number).like(needle),
                func.upper(Client.name).like(needle),
            ))
        if client_id:
            conds.append(ClientOrder.client_id == client_id)
        if status:
            conds.append(status_expr == status.strip().upper())
        if has_breakdown is True:
            conds.append(style_count > 0)
        elif has_breakdown is False:
            conds.append(style_count == 0)
        if conds:
            stmt = stmt.where(and_(*conds))

        total = int(await self.db.scalar(
            select(func.count()).select_from(stmt.subquery())) or 0)

        rows = (await self.db.execute(
            stmt.order_by(activity.desc(), ClientOrder.order_number.asc())
                .limit(limit).offset(offset)
        )).all()

        items = [{
            "order_id": r.id,
            "order_number": r.order_number,
            "client_id": r.client_id,
            "client_name": r.client_name,
            "client_code": r.client_code,
            "order_date": r.order_date,
            "delivery_deadline": r.delivery_deadline,
            "ship_mode": r.ship_mode,
            "currency": r.currency,
            "breakdown_status": r.breakdown_status,
            "style_count": int(r.style_count or 0),
            "released_styles": int(r.released_count or 0),
            "cancelled_styles": int(r.cancelled_count or 0),
            "sku_count": int(r.sku_count or 0),
            "qty_ordered": int(r.qty_ordered or 0),
            "pieces_minted": int(r.pieces_minted or 0),
            # NULL until a breakdown sheet is committed — which is precisely what
            # distinguishes an order awaiting its sheet from one already worked on.
            "last_uploaded_at": r.last_style_at,
            "last_activity_at": r.last_activity_at,
            "created_at": r.created_at,
            # The call to make when this row is clicked. Spelled out so the
            # frontend does not have to reconstruct the URL from the order number
            # and get the encoding wrong on "Proposta N.2".
            "breakdown_url": f"/api/v1/imports/breakdown/{r.order_number}",
        } for r in rows]

        return {"total": total, "count": len(items), "items": items}

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
                # ── THE LINING QUESTION, IN THREE FIELDS ─────────────────────
                # These are three different things and collapsing them into one
                # is what let a guess be read as a fact:
                #
                #   needs_lining            the DM's ANSWER. null = not yet asked,
                #                           and the release screen must render
                #                           that as an unanswered question, not
                #                           as "no".
                #   needs_lining_suggested  what the system INFERS from the style
                #                           name / article. A default to
                #                           pre-select, never a verdict.
                #   lining_answered         whether anyone has actually answered.
                #
                # The DM's answer, once given, outranks the suggestion in both
                # directions — including saying NO to a style named KNIT, which
                # is the case inference cannot get right and the whole reason the
                # question is asked (core/lining_rules.py).
                "needs_lining": st.needs_lining,
                "needs_lining_suggested": lining_required(
                    style_name=st.name, style_article=st.article),
                "lining_answered": st.needs_lining is not None,
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
                # How many DRAFT styles still have no lining answer. The release
                # screen should not let the DM tick a style through without one —
                # this is the badge that says so before they try.
                "styles_awaiting_lining_answer": sum(
                    1 for s in drafts if not s["lining_answered"]),
            },
        }

    # ── CRUD on the DRAFT sheet ──────────────────────────────────────────────
    # Which STYLE fields a DRAFT correction may touch. Everything the sheet
    # actually gets wrong, and nothing structural: `client_order_id` (which order
    # a style belongs to) and `base_style_id` are deliberately absent — moving a
    # style between orders is a re-upload, not an edit, because the order is what
    # scopes its SKU codes, its rates and its client visibility.
    _STYLE_EDITABLE = {
        "name", "article", "code", "gender", "label", "thickness", "season",
        "customer_ref", "internal_ref", "unit_price", "currency", "needs_lining",
    }

    async def update_sku(self, sku_id: uuid.UUID, patch: dict,
                         *, style_patch: dict | None = None) -> dict:
        """Correct one line of a DRAFT breakdown, and optionally its parent style.

        409 once the style is released. The SKU fields are the ones a breakdown
        sheet gets wrong — quantity, colour name/code, size, lining colours.
        Anything structural (which style a SKU belongs to) is a re-upload.

        BOTH HALVES COMMIT TOGETHER. The style edit is applied on the same
        session and the same commit as the SKU edit, so a unique-code collision
        on the style rolls the colour change back with it rather than leaving the
        operator's row half-written.
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

        style_changed = await self._apply_style_patch(style, style_patch or {})

        if not changed and not style_changed:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Nothing to update.")
        if changed:
            await self._audit("BREAKDOWN_SKU_UPDATED", sku.id,
                              {"style_id": str(style.id), "changed": changed})
        if style_changed:
            await self._audit("BREAKDOWN_STYLE_UPDATED", style.id,
                              {"style_code": style.code, "changed": style_changed})
        await self._commit_unique_safe(style)
        await self.db.refresh(sku)
        await self.db.refresh(style)
        return {"sku_id": sku.id, "sku_code": sku.code,
                "qty_ordered": int(sku.qty_ordered or 0),
                "colour": sku.color_name or sku.color_code, "size": sku.size,
                "changed": changed,
                "style": {
                    "style_id": style.id, "style_code": style.code,
                    "style_name": style.name, "article": style.article,
                    "needs_lining": style.needs_lining,
                    "changed": style_changed,
                }}

    async def update_style(self, style_id: uuid.UUID, patch: dict) -> dict:
        """Correct a DRAFT style on its own. 409 once it is released."""
        style = await self.db.get(Style, style_id)
        if style is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Style not found.")
        self._assert_draft(style, "edit")

        changed = await self._apply_style_patch(style, patch)
        if not changed:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Nothing to update.")
        await self._audit("BREAKDOWN_STYLE_UPDATED", style.id,
                          {"style_code": style.code, "changed": changed})
        await self._commit_unique_safe(style)
        await self.db.refresh(style)
        return {
            "style_id": style.id, "style_code": style.code,
            "style_name": style.name, "article": style.article,
            "thickness": style.thickness, "season": style.season,
            "unit_price": (float(style.unit_price)
                           if style.unit_price is not None else None),
            "currency": style.currency,
            "needs_lining": style.needs_lining,
            "lining_answered": style.needs_lining is not None,
            "production_status": style.production_status
                                 or ProductionReleaseStatus.DRAFT.value,
            "changed": changed,
        }

    async def _apply_style_patch(self, style: Style, patch: dict) -> dict:
        """Write the allowed style fields onto `style`; return what changed.

        `needs_lining` is handled apart from the rest because None is MEANINGFUL
        for it — "not asked" is a real third state, distinct from true and false
        (see Style.needs_lining). Every other field treats None as "not sent",
        the normal PATCH convention. A single `if value is None: continue` loop
        would make it impossible to ever clear the lining answer back to unasked,
        and would silently drop `needs_lining: false` — the exact value that
        matters most here.
        """
        changed: dict = {}
        for key, value in (patch or {}).items():
            if key not in self._STYLE_EDITABLE:
                continue
            if key == "needs_lining":
                # `patch` is built with exclude_unset=True, so this key is
                # present only because the caller actually sent it — an explicit
                # null therefore means "put it back to unanswered", not "no
                # value supplied".
                style.needs_lining = None if value is None else bool(value)
                changed[key] = style.needs_lining
                continue
            if value is None:
                continue
            if key == "code":
                value = str(value).strip().upper()
                if not value:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "Style code cannot be blank — it is what barcode "
                        "captions and rate cards resolve through.")
                clash = await self.db.scalar(
                    select(Style.id).where(Style.code == value,
                                           Style.id != style.id))
                if clash is not None:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        f"Style code '{value}' is already used by another style. "
                        f"Codes are unique across the factory because a barcode "
                        f"caption and a wage rate both resolve through them.")
            if key == "unit_price" and float(value) < 0:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    "unit_price cannot be negative.")
            setattr(style, key, value)
            changed[key] = (float(value) if key == "unit_price" else value)
        return changed

    async def _commit_unique_safe(self, style: Style) -> None:
        """Commit, turning the style-code unique violation into a 409.

        The pre-check in _apply_style_patch loses to a concurrent edit: two DMs
        renaming two styles to the same code in the same second both pass the
        SELECT and one loses at COMMIT. Without this that surfaces as a 500 on a
        perfectly ordinary conflict.
        """
        from sqlalchemy.exc import IntegrityError
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Could not save style '{style.name}' — its code collides with "
                f"another style. Reload the breakdown and try again.") from exc

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
                             allow_pool_growth: bool = False,
                             lining_by_style: dict[uuid.UUID, bool] | None = None,
                             ) -> dict:
        """Release named styles into production: mint pieces, barcodes, drawers.

        THE LINING QUESTION IS ASKED HERE, AND THIS IS THE ONLY PLACE IT CAN BE
        ASKED. Release is the moment a style stops being a spreadsheet row and
        becomes barcoded garments in drawers, so it is the last moment anyone can
        answer "does this take a lining?" before the answer starts governing what
        may move. `lining_by_style` carries the DM's per-style answer; it is
        stamped on Style.needs_lining, copied down to every minted piece, and
        from then on outranks the name/colour inference in BOTH directions
        (core/lining_rules.py).

        A style with NO answer still releases and falls back to inference. That
        is a deliberate one-release grace period for the frontend, not the
        intended path — see the router's `styles` field.

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
        # as the loader (imports/router.py). It commits itself. The lining
        # declarations go WITH it because they have to be stamped on the style
        # BEFORE premint reads them to set each piece's flag; splitting the two
        # across sessions would mint a batch of pieces against the previous
        # answer.
        declared = {sid: bool(v) for sid, v in (lining_by_style or {}).items()
                    if v is not None}
        stats = await run_in_threadpool(
            _release_sync, order.id, releasable, user_name, allow_pool_growth,
            declared)

        released = []
        for sid in releasable:
            style = await self.db.get(Style, sid)
            await self.db.refresh(style)
            released.append({"style_id": str(sid), "style_code": style.code,
                             "style_name": style.name,
                             "production_status": style.production_status,
                             "needs_lining": style.needs_lining,
                             # True when the DM answered; False when this style
                             # released on inference and the store gate will be
                             # guessing. The screen should be able to show that
                             # difference rather than presenting both as facts.
                             "lining_declared": sid in declared})
            await self._audit("BREAKDOWN_STYLE_RELEASED", sid, {
                "style_code": style.code, "order_number": order.order_number,
                "by": user_name, "pieces_minted": stats.get("pieces_minted", 0),
                # THE DECLARATION IS AUDITED, and that audit is what replaces the
                # old one-directional safety rule: an explicit False is the only
                # way a lining requirement can now be switched OFF, so the record
                # has to name who switched it and when.
                "needs_lining": style.needs_lining,
                "lining_declared": sid in declared})
        await self.db.commit()

        waiting = int(stats.get("pieces_waiting_for_drawer", 0))
        message = (f"Released {len(released)} style(s); "
                   f"{stats.get('pieces_minted', 0)} piece barcode(s) minted.")
        lined = [r["style_code"] for r in released if r["needs_lining"]]
        if lined:
            message += (f" {len(lined)} style(s) take a lining — their drawers "
                        f"must hold BOTH parts before line-stitching.")
        # The deprecated style_ids path releases without a human answer. Say so
        # out loud: a garment whose lining requirement was guessed is exactly the
        # class that walked to PACKAGE_EXPORT unlined, and a silent fallback is
        # how it stayed invisible for an entire order.
        guessed = [r["style_code"] for r in released if not r["lining_declared"]]
        if guessed:
            message += (f" WARNING: {len(guessed)} style(s) were released with NO "
                        f"lining declaration ({', '.join(guessed[:5])}"
                        f"{'…' if len(guessed) > 5 else ''}) — their lining "
                        f"requirement was INFERRED from the style name. Release "
                        f"through `styles: [{{style_id, needs_lining}}]` instead.")
        if waiting:
            message += (f" {waiting} piece(s) have NO DRAWER — the pool is full. "
                        f"They cannot be stored until a drawer frees up or DM/MD "
                        f"adds drawers (POST /drawers/pool).")
        return {"order_number": order.order_number, "released": released,
                "rejected": rejected, "minted": stats,
                "styles_released_without_lining_answer": guessed,
                "message": message}

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
                  allow_pool_growth: bool,
                  lining_by_style: dict | None = None) -> dict:
    """The mint itself, on a synchronous Session, in one transaction.

    Stamping the style RELEASED and minting its pieces MUST be atomic: a crash
    between them leaves either barcodes nobody released or a released style with
    no garments. Both are worse than the upload failing.

    THE LINING DECLARATION IS STAMPED FIRST, BEFORE premint_order RUNS. premint
    reads `Style.needs_lining` to set each new piece's own flag
    (_sku_needs_lining), so writing it afterwards would mint the whole style
    against the PREVIOUS answer and leave the pieces disagreeing with the style
    they belong to — the exact stale-flag class of bug that started this work.
    """
    from app.core.database import SessionLocal
    from app.modules.imports.premint import premint_order

    db = SessionLocal()
    try:
        order = db.get(ClientOrder, order_id)
        now = datetime.now(timezone.utc)
        declared = lining_by_style or {}
        for sid in style_ids:
            style = db.get(Style, sid)
            if sid in declared:
                style.needs_lining = bool(declared[sid])
        # Flush the declarations so premint's db.get(Style, ...) sees them in
        # this same transaction rather than the pre-write values.
        db.flush()

        stats = premint_order(db, order, style_ids=style_ids,
                              allow_pool_growth=allow_pool_growth)
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
