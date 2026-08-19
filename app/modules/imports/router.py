"""
================================================================================
modules/imports/router.py — Excel import endpoints (async-friendly)
================================================================================

TWO-STEP FLOW (the safe pattern for messy uploads)
  POST /imports/preview  — upload an .xlsx, get a parsed PREVIEW + warnings.
                           Nothing is written to the database.
  POST /imports/commit   — upload the same .xlsx, parse, validate, and WRITE it.

Splitting preview from commit lets a human see exactly what will happen — how
many clients, styles, pieces, and any warnings — before any data is stored.

ASYNC NOTE
    Parsing (openpyxl) and the bulk DB load are CPU/IO-blocking and use the SYNC
    SQLAlchemy session. Rather than rewrite the batch loader as async, we run it
    in a worker thread via Starlette's run_in_threadpool, so the event loop is
    never blocked. This keeps the proven idempotent loader intact while the API
    stays fully async.
================================================================================
"""
from __future__ import annotations

import os
import tempfile
import uuid
import zipfile

from decimal import Decimal

from fastapi import (APIRouter, Body, Depends, File, Form, HTTPException, Query,
                     UploadFile)
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.modules.clients.service import ClientService
from app.modules.imports.breakdown import BreakdownService
from app.modules.imports.load_to_db import load_preview_into_order
from app.modules.imports.import_engine import build_preview
from app.modules.users.deps import require_roles
from app.modules.users.models import User
from app.core.enums import UserRole
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/imports", tags=["Imports"])

# Uploading, correcting and releasing a breakdown are all DM work; MD passes via
# SUPERUSER_ROLES inside require_roles.
_DM = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)


def _save_upload(file: UploadFile) -> str:
    # F118: filename is optional in the multipart spec — guard before .lower().
    fname = (file.filename or "").lower()
    if not fname.endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload an .xlsx file")

    # F38: stream the body with a hard cap instead of an unbounded .read() that
    # pulls the whole upload into memory. The cap comes from settings.
    max_bytes = settings.max_upload_mb * 1024 * 1024
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    written = 0
    with os.fdopen(fd, "wb") as f:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                f.close()
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise HTTPException(
                    413, f"File exceeds the {settings.max_upload_mb} MB limit.")
            f.write(chunk)

    # F45: validate the CONTAINER, not just the extension. An .xlsx is a zip;
    # anything renamed to .xlsx that is not a valid zip is rejected before it
    # reaches openpyxl (guards against a decompression bomb / mislabelled file).
    if not zipfile.is_zipfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(400, "File is not a valid .xlsx workbook.")
    return path

async def _require_order(db: AsyncSession, order_number: str):
    """Reject early if the entered number doesn't match a client's order."""
    order = await ClientService(db).get_order_by_number(order_number)
    if not order:
        raise HTTPException(
            404, "Order number not found. Please verify with the client record.")
    return order

def _do_commit_into_order(path: str, order_number: str) -> dict:
    preview = build_preview(path)
    summary = preview.summary()
    db = SessionLocal()
    try:
        stats = load_preview_into_order(db, preview, order_number=order_number)
    finally:
        db.close()
    return {"summary": summary, "written": stats}

def _do_preview(path: str) -> dict:
    return build_preview(path).summary()


@router.post("/preview")
async def preview_import(
    order_number: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Dry-run. Validates the order number first, then parses (no writes)."""
    await _require_order(db, order_number)
    path = _save_upload(file)
    try:
        return await run_in_threadpool(_do_preview, path)
    finally:
        os.remove(path)


@router.post("/commit")
async def commit_import(
    order_number: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Validate order number, parse, and write SKUs INTO that order (idempotent).

    NOTHING IS MINTED HERE ANY MORE (change-list item 9). The sheet lands as
    DRAFT styles you can correct; per-piece barcodes and drawer merges are
    created only when the DM releases a style. The response carries
    `release_required: true` and `pieces_minted: 0` to say so.

    Next call: GET /imports/breakdown/{order_number}.

    THE ORDER NUMBER IN THIS RESPONSE IS A RECEIPT, NOT THE ONLY COPY. The order
    is a permanent row (this endpoint 404s rather than create one), and every
    order is listed at GET /imports/orders with its breakdown status and a
    `breakdown_url`. Losing this response loses nothing — re-open the order from
    that index.
    """
    await _require_order(db, order_number)
    path = _save_upload(file)
    try:
        return await run_in_threadpool(_do_commit_into_order, path, order_number)
    finally:
        os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# THE BREAKDOWN TABLE + THE RELEASE GATE  (change-list item 9)
# ══════════════════════════════════════════════════════════════════════════════
class StylePatch(BaseModel):
    """Correct the STYLE behind a DRAFT breakdown line.

    Everything the breakdown sheet can get wrong about a style, not just the two
    fields the table happened to display. The sheets are hand-made from a client
    order and a spec sheet, so the article number, the thickness, the season and
    the unit price are all routinely wrong on the first pass — and re-uploading
    the whole workbook to fix a thickness is not a correction workflow.

    `needs_lining` is here as well as on release: a DM who realises mid-review
    that a style has no lining should be able to say so without waiting for the
    release dialog.
    """
    name: str | None = None
    article: str | None = None
    code: str | None = None
    gender: str | None = None
    label: str | None = None
    thickness: str | None = None
    season: str | None = None
    customer_ref: str | None = None
    internal_ref: str | None = None
    unit_price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=3)
    needs_lining: bool | None = None


class SkuPatch(BaseModel):
    """Correct one DRAFT breakdown line — and, optionally, its parent style.

    ONE CALL FOR ONE SCREEN. The breakdown table shows a style and its SKUs
    together in one editable row group, so editing them took two round trips to
    two endpoints with two failure modes: a 200 on the SKU and a 409 on the style
    left the operator's screen half-saved with no way to tell which half. Both
    now move, or neither does.
    """
    qty_ordered: int | None = Field(default=None, ge=0)
    color_name: str | None = None
    color_code: str | None = None
    size: str | None = None
    knit_color: str | None = None
    nylon_color: str | None = None
    # The parent style's fields, nested so there is no ambiguity about which
    # entity a bare `name` or `code` belongs to.
    style: StylePatch | None = None


class ReleaseStyle(BaseModel):
    """One style the DM is releasing, and their answer to the lining question."""
    style_id: uuid.UUID
    # null is NOT "no" — it means unanswered, and release will reject it. Three
    # states, because "we never asked" is a real and different state from "no".
    needs_lining: bool | None = None


class ReleaseRequest(BaseModel):
    """The DM names the styles that go to production, and declares their lining.

    THREE SHAPES, ONE OF THEM DEPRECATED.

        styles: [{style_id, needs_lining}]     ← the contract. Every style
                                                 carries its own lining answer.
        style_ids + needs_lining               ← BROADCAST. One answer covering
                                                 every id in the list. A real
                                                 answer from a human, so these
                                                 styles are DECLARED, not guessed.
        style_ids: [uuid, ...]                 ← DEPRECATED, and only without a
                                                 top-level `needs_lining`.
                                                 Releases on the name/colour
                                                 INFERENCE instead of an answer.

    The legacy field is accepted for ONE RELEASE so a frontend mid-deploy is not
    broken by this change, and it is not a silent fallback: every style released
    that way comes back with `lining_declared: false`, and the response message
    names them. Remove it once the release screen sends `styles`.

    WHY `extra="forbid"`. A top-level `needs_lining` used to be accepted by the
    HTTP layer and dropped on the floor: Pydantic ignores unknown keys by
    default, so a DM who sent `{style_ids: [...], needs_lining: true}` got a 200,
    a NULL declaration, and the "released with NO lining declaration" warning
    describing a request that had answered the question. Forbidding extras turns
    every misspelt or misplaced field into a 422 that names it, instead of a
    silent no-op on the one field this endpoint exists to capture.
    """
    model_config = ConfigDict(extra="forbid")

    styles: list[ReleaseStyle] | None = None
    style_ids: list[uuid.UUID] | None = Field(
        default=None, deprecated=True,
        description="DEPRECATED — use `styles` so each style carries its lining "
                    "answer, or pair this with a top-level `needs_lining` when "
                    "one answer covers the whole list. Alone, it falls back to "
                    "inferring the lining requirement from the style name.")
    # THE BROADCAST ANSWER. Same three states as the per-style field: null is
    # "not answered", not "no". Per-style answers WIN over it, so a body may
    # carry the common case here and override the exceptions in `styles`.
    needs_lining: bool | None = Field(
        default=None,
        description="One lining answer applied to every style in this request. "
                    "A per-style `needs_lining` in `styles` overrides it. Counts "
                    "as a DECLARATION (lining_declared: true), not an inference.")
    # Growing the drawer pool is a separate, explicit decision (POST /drawers/pool).
    # Setting this true releases AND mints the shortfall of drawers in one step —
    # offered because a DM who has just seen "230 pieces have no drawer" should not
    # have to make two calls, but it is opt-in so the pool never grows by accident.
    grow_drawer_pool: bool = False

    @model_validator(mode="after")
    def _one_shape(self):
        if not self.styles and not self.style_ids:
            raise ValueError(
                "Send `styles: [{style_id, needs_lining}]` — one entry per style "
                "you are releasing, each declaring whether that style takes a "
                "lining.")
        if self.styles and self.style_ids:
            raise ValueError(
                "Send either `styles` (preferred) or the deprecated `style_ids`, "
                "not both — two lists of styles in one request have no defined "
                "precedence.")
        # A top-level `needs_lining` answers every style that did not answer for
        # itself, so it settles this check too — the requirement is that each
        # style HAS an answer, not that it carried its own.
        if self.styles and self.needs_lining is None:
            unanswered = [str(s.style_id) for s in self.styles
                          if s.needs_lining is None]
            if unanswered:
                raise ValueError(
                    f"{len(unanswered)} style(s) have no lining answer: "
                    f"{', '.join(unanswered)}. Set `needs_lining` true or false on "
                    f"every style (or once at the top level to cover them all) — "
                    f"it decides whether the garment needs a lining "
                    f"cut and whether its drawer must hold both parts before "
                    f"line-stitching, and it cannot be changed after release.")
        return self

    def resolved(self) -> tuple[list[uuid.UUID], dict[uuid.UUID, bool]]:
        """(style ids in order, {style_id: lining answer}). One reading of the
        body, so the router and the service cannot interpret it differently.

        A style id lands in the map ONLY when a human actually answered for it,
        because the service reads presence-in-the-map as `lining_declared` — the
        flag that separates a declaration from a guess in the response, the audit
        row and core/lining_rules. Never default a missing answer to False here:
        that would record the DM as having declared "no lining" on a style nobody
        was asked about, which is the one direction the inference cannot recover
        from.
        """
        if self.styles:
            ids = [s.style_id for s in self.styles]
            answers = {s.style_id: bool(s.needs_lining) for s in self.styles
                       if s.needs_lining is not None}
            if self.needs_lining is not None:
                # Per-style wins; the broadcast fills only the gaps.
                answers = {sid: answers.get(sid, bool(self.needs_lining))
                           for sid in ids}
            return ids, answers

        ids = list(self.style_ids or [])
        if self.needs_lining is None:
            return ids, {}          # the deprecated inference path, unchanged
        return ids, {sid: bool(self.needs_lining) for sid in ids}


class StyleIdsRequest(BaseModel):
    style_ids: list[uuid.UUID] = Field(min_length=1)


# ── THE ORDER INDEX ──────────────────────────────────────────────────────────
# Declared BEFORE /breakdown/{order_number}: it is a different path segment, so
# there is no capture conflict, but keeping the index next to the table it opens
# is how the two stay readable as one screen flow.
@router.get("/orders")
async def list_orders(
    q: str | None = Query(
        None, min_length=1, max_length=80,
        description="Search order number OR client name — case-insensitive "
                    "CONTAINS, so '1579' and 'jack' both work."),
    client_id: uuid.UUID | None = Query(None, description="One client's orders."),
    status: str | None = Query(
        None,
        description="NOT_UPLOADED | DRAFT | PARTIALLY_RELEASED | RELEASED | "
                    "CANCELLED — the breakdown rollup, not the order's own state."),
    has_breakdown: bool | None = Query(
        None, description="true = orders whose breakdown sheet is committed; "
                          "false = orders still waiting for one."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """QUERY. EVERY ORDER, PERMANENTLY — the index the breakdown screen opens from.

    POST /imports/commit answers with the order number, and that response used to
    be the only place it appeared: close the tab and
    GET /imports/breakdown/{order_number} was unreachable unless somebody had
    written the number down. Nothing was ever actually lost — `client_order` is a
    permanent table, and commit REQUIRES the order to already exist so it never
    creates one — but there was no way to LIST orders:
    GET /clients/{client_id}/orders needs a client id you may not have, and the
    analytics explorer returns a nested tree rather than a clickable index.

    Click a row, call the `breakdown_url` it carries, and you are on the
    breakdown table for that order.

    `breakdown_status` is rolled up from the order's styles, so the list says
    what still needs doing without opening anything:

      NOT_UPLOADED        the order exists; no breakdown sheet committed yet
      DRAFT               sheet uploaded, nothing released, nothing minted
      PARTIALLY_RELEASED  some styles in production, some still editable
      RELEASED            every live style released and minting pieces
      CANCELLED           every style on the order was cancelled

    ORDERED MOST-RECENTLY-WORKED-ON FIRST — the newest of the order's creation
    and its last-written style — so an order whose sheet landed this morning
    outranks one raised months ago and never touched. `order_date` is the
    client's date and says nothing about what the factory is handling now.

    `pieces_minted` is the count of real barcoded garments behind the order, so a
    RELEASED row can be told apart from one that released and produced nothing.
    """
    if status:
        valid = {"NOT_UPLOADED", "DRAFT", "PARTIALLY_RELEASED", "RELEASED",
                 "CANCELLED"}
        if status.strip().upper() not in valid:
            raise HTTPException(
                422, f"status must be one of {sorted(valid)}.")
    return await BreakdownService(db).list_orders(
        q=q, client_id=client_id, status=status, has_breakdown=has_breakdown,
        limit=limit, offset=offset)


@router.get("/breakdown/{order_number}")
async def breakdown_table(
    order_number: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """THE BREAKDOWN, AS A TABLE. Styles + their SKUs + release status.

    This is the screen between upload and production. Rows with
    `production_status: DRAFT` are editable and have minted nothing; rows with
    RELEASED have real barcoded garments behind them (`minted_pieces`) and are
    read-only.

    `needs_lining` per style is the same verdict the store completeness gate will
    apply, so the DM can see before releasing whether these garments will need a
    lining leg merged in the drawer.
    """
    return await BreakdownService(db).get_table(order_number)


@router.patch("/breakdown/skus/{sku_id}")
async def patch_breakdown_sku(
    sku_id: uuid.UUID,
    body: SkuPatch,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """Correct one DRAFT line — the SKU, its parent style, or both at once.

    Send `style: {...}` alongside the SKU fields to edit the style in the same
    call; the two are written in ONE transaction, so the screen can never end up
    with the colour saved and the article not.

    409 once the style is RELEASED: its pieces carry printed barcodes, and
    rewriting the breakdown behind a garment already on the floor is how a
    factory loses traceability. To make MORE of a released style, raise the
    quantity on a new upload and release again — release tops up.
    """
    patch = body.model_dump(exclude_unset=True)
    style_patch = patch.pop("style", None)
    return await BreakdownService(db).update_sku(
        sku_id, patch, style_patch=style_patch)


@router.patch("/breakdown/styles/{style_id}")
async def patch_breakdown_style(
    style_id: uuid.UUID,
    body: StylePatch,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """Correct a DRAFT style on its own — name, article, code, thickness, price,
    season, refs, or its lining declaration.

    The style-only door. `PATCH /breakdown/skus/{sku_id}` edits a style through
    its SKU, which is what the table row does; this is for the style header,
    which has no SKU to hang off. Same DRAFT-only rule, same audit row.

    A style `code` collision is a 409, not a 500: the column is unique because
    the code is what a barcode caption and a rate card resolve through.
    """
    return await BreakdownService(db).update_style(
        style_id, body.model_dump(exclude_unset=True))


@router.delete("/breakdown/skus/{sku_id}")
async def delete_breakdown_sku(
    sku_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """Drop a line the sheet should not have had. DRAFT + zero minted pieces only."""
    return await BreakdownService(db).delete_sku(sku_id)


@router.post("/breakdown/{order_number}/cancel")
async def cancel_breakdown_styles(
    order_number: str,
    body: StyleIdsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """Withdraw DRAFT styles so they stop appearing on the release screen."""
    return await BreakdownService(db).cancel_styles(
        body.style_ids, user_name=user.name)


@router.post("/breakdown/{order_number}/release", status_code=201)
async def release_breakdown_styles(
    order_number: str,
    body: ReleaseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """RELEASE STYLES INTO PRODUCTION. This is the mint.

    Creates, atomically and only now: the Piece rows, their per-piece barcodes
    (compact PC-XXXXXX + the long alias), and the drawer merge for each. Stamps
    each style RELEASED with the actor and the time, and writes an audit_log row.

    EVERY STYLE MUST DECLARE ITS LINING:

        {"styles": [{"style_id": "…", "needs_lining": true},
                    {"style_id": "…", "needs_lining": false}]}

    When ONE answer covers the whole batch, send it once at the top level:

        {"style_ids": ["…", "…"], "needs_lining": true}

    That is a declaration, not a guess — `lining_declared` comes back true. A
    per-style answer in `styles` overrides the top-level one, so the common case
    can be broadcast and the exceptions named. `style_ids` WITHOUT a top-level
    answer is the deprecated inference path and still warns.

    That answer is stamped on the style, copied onto every piece it mints, and
    from then on decides two things: whether the garment has a LINING_CUTTING
    stage at all, and whether its drawer must hold BOTH parts before the piece
    may enter line-stitching. A style declared `false` skips the lining cut and
    clears the store on its leather alone.

    IT OUTRANKS THE SYSTEM'S OWN GUESS, IN BOTH DIRECTIONS — including declaring
    a style named "…KNIT" as leather-only. That is the point of asking: inference
    off a style name cannot know that a particular wool shell has no lining, and
    the DM holding the spec sheet can. The declaration is audited with the actor
    and the time, which is what makes switching a lining requirement OFF a
    traceable decision rather than a silent one.

    IT CANNOT BE CHANGED AFTER RELEASE. Pieces are minted against it and their
    barcodes are printed. Fix it before releasing (PATCH the style), not after.

    PARTIAL ACCEPT: an already-released style comes back in `rejected` with its
    reason; the rest still release. Read `minted`, not the HTTP status.

    WATCH `minted.pieces_waiting_for_drawer`. The drawer pool is finite. Pieces
    beyond the free drawers are minted with no drawer — they have barcodes but
    nowhere to be stored, so they cannot pass the merge gate until a drawer frees
    up or the pool is grown. Send `grow_drawer_pool: true`, or call
    POST /drawers/pool, to clear it.
    """
    style_ids, lining_by_style = body.resolved()
    return await BreakdownService(db).release_styles(
        order_number, style_ids, user_name=user.name,
        allow_pool_growth=body.grow_drawer_pool,
        lining_by_style=lining_by_style)