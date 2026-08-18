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

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, model_validator
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

    TWO SHAPES, ONE OF THEM DEPRECATED.

        styles: [{style_id, needs_lining}]     ← the contract. Every style
                                                 carries its lining answer.
        style_ids: [uuid, ...]                 ← DEPRECATED. Releases on the
                                                 name/colour INFERENCE instead of
                                                 a human answer.

    The legacy field is accepted for ONE RELEASE so a frontend mid-deploy is not
    broken by this change, and it is not a silent fallback: every style released
    that way comes back with `lining_declared: false`, and the response message
    names them. Remove it once the release screen sends `styles`.
    """
    styles: list[ReleaseStyle] | None = None
    style_ids: list[uuid.UUID] | None = Field(
        default=None, deprecated=True,
        description="DEPRECATED — use `styles` so each style carries its lining "
                    "answer. Releasing through this field falls back to inferring "
                    "the lining requirement from the style name.")
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
        if self.styles:
            unanswered = [str(s.style_id) for s in self.styles
                          if s.needs_lining is None]
            if unanswered:
                raise ValueError(
                    f"{len(unanswered)} style(s) have no lining answer: "
                    f"{', '.join(unanswered)}. Set `needs_lining` true or false on "
                    f"every style — it decides whether the garment needs a lining "
                    f"cut and whether its drawer must hold both parts before "
                    f"line-stitching, and it cannot be changed after release.")
        return self

    def resolved(self) -> tuple[list[uuid.UUID], dict[uuid.UUID, bool]]:
        """(style ids in order, {style_id: lining answer}). One reading of the
        body, so the router and the service cannot interpret it differently."""
        if self.styles:
            return ([s.style_id for s in self.styles],
                    {s.style_id: bool(s.needs_lining) for s in self.styles})
        return (list(self.style_ids or []), {})


class StyleIdsRequest(BaseModel):
    style_ids: list[uuid.UUID] = Field(min_length=1)


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