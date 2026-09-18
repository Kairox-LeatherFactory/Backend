"""
================================================================================
modules/cutting/service.py — the grid that replaces Kumar's Excel
================================================================================
WHAT THIS REPLACES

    Every garment's real cutting record lived in a spreadsheet: which hides went
    to which cutter, what each measured, the one he handed back, the extra one he
    asked for. The ERP saw none of it — only a single total dcm, typed once at the
    end — so it could not answer how much leather a jacket actually took.

    This service is that spreadsheet, with the three things a spreadsheet cannot
    do: the hides are real stock rows (so claiming one makes it unavailable to
    everyone else), the total is derived (so it cannot disagree with its parts),
    and approval is an audited transition (so the number that reaches the ledger
    has a name and a time against it).

THE SHAPE OF THE WORK, WHICH IS THE FACTORY'S OWN
    1. manager picks a style + colour            → generate()
    2. the system allocates ~10 hides per garment, sized to the garment
    3. the hides go to the cutter; he needs one more, or hands one back
                                                 → add_sheet() / remove_sheet()
    4. manager approves the row                  → approve()
    5. the cutter's scan logs it                 → the production logger reads
                                                   the approved row

STEP 2 IS AN ESTIMATE ON PURPOSE. Nobody supplies per-piece dcm for most styles,
so the allocator aims at core/leather_norms and expects to be edited. Getting it
roughly right turns ten manual picks into one correction; refusing to guess turns
it back into the Excel.

WHY ALLOCATION IS A STATE ON THE HIDE AND NOT A LIST ON THE ROW
    `material_sheet.cutting_row_id` points THIS way, and the hide's status moves
    to ALLOCATED. That makes "one hide on two garments" unrepresentable rather
    than merely forbidden — a list column on the row could hold the same hide
    twice, and two cutters would be sent for one skin.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    BarcodeAuditAction, CuttingRowStatus, MaterialCategory, SHEET_ALLOCATABLE,
    SheetStatus,
)
from app.core.leather_norms import expected_sheet_count, leather_target_dcm
from app.modules.cutting.repository import CuttingRepository


class CuttingService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = CuttingRepository(db)

    # ── lazy imports keep the module graph acyclic (CLAUDE.md §15) ───────────
    def _materials(self):
        from app.modules.materials.service import MaterialService
        return MaterialService(self.db)

    # ══════════════════════════════════════════════════════════ reading
    async def _sheet_cells(self, row) -> list:
        mats = self._materials()
        sheets = await mats.repo.sheets_for_row(row.id)
        return [{"sheet_id": s.id, "code": s.code, "dcm": float(s.dcm or 0),
                 "status": s.status} for s in sheets]

    async def _row_payload(self, row, *, piece_codes=None,
                           cutter_names=None) -> dict:
        cells = await self._sheet_cells(row)
        total = sum(Decimal(str(c["dcm"])) for c in cells) if cells else Decimal(0)

        warnings: list[str] = []
        if row.cutter_employee_id is None:
            warnings.append("No cutter assigned yet.")
        if not cells:
            warnings.append("No sheets on this row — nothing to cut from.")
        elif row.target_dcm is not None:
            gap = Decimal(str(row.target_dcm)) - total
            # 10% is the band inside which nobody should be asked to look. The
            # allocator is an estimate and hides vary; flagging every 3 dcm would
            # train the manager to ignore the column.
            if abs(gap) > Decimal(str(row.target_dcm)) * Decimal("0.10"):
                warnings.append(
                    f"Sheets total {float(total):g} dcm against a target of "
                    f"{float(row.target_dcm):g} dcm "
                    f"({'over' if gap < 0 else 'short'} by {abs(float(gap)):g}).")

        return {
            "row_id": row.id, "piece_id": row.piece_id,
            "piece_code": (piece_codes or {}).get(row.piece_id),
            "style_id": row.style_id, "sku_id": row.sku_id,
            "article": row.article, "colour": row.colour, "size": row.size,
            "rc_no": row.rc_no,
            "cutter_employee_id": row.cutter_employee_id,
            "cutter_name": (cutter_names or {}).get(row.cutter_employee_id),
            "work_date": row.work_date, "status": row.status,
            "target_dcm": float(row.target_dcm) if row.target_dcm is not None else None,
            "target_source": row.target_source,
            # DERIVED, ALWAYS. total_dcm is stored only so an approved row keeps
            # its number when its hides are later consumed; the live grid reads
            # the sheets, so the two can never drift while editing.
            "total_dcm": float(total) if cells else (
                float(row.total_dcm) if row.total_dcm is not None else None),
            "sheets": cells, "sheet_count": len(cells),
            "approved_at": row.approved_at, "logged_at": row.logged_at,
            "warnings": warnings,
        }

    async def grid(self, *, style_id: uuid.UUID, colour: str | None = None) -> dict:
        """The whole sheet for one style+colour, plus what the header needs."""
        style = await self.repo.get_style(style_id)
        if style is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Style not found.")

        rows = await self.repo.rows_for_style(style_id, colour=colour)
        piece_codes = await self._piece_codes([r.piece_id for r in rows])
        cutters = await self._present_cutters()
        names = {c["employee_id"]: c["name"] for c in cutters}

        payloads = [await self._row_payload(r, piece_codes=piece_codes,
                                            cutter_names=names) for r in rows]
        uncut = await self.repo.uncut_pieces(style_id, colour=colour)

        warnings: list[str] = []
        if not cutters:
            warnings.append(
                "Nobody is checked in today, so no cutter can be assigned. "
                "Mark attendance first — production refuses an absent worker.")
        return {
            "style_id": style.id, "style_name": style.name, "colour": colour,
            "colours": await self.repo.colours_for_style(style_id),
            "rows": payloads, "present_cutters": cutters,
            "uncut_pieces": len(uncut), "warnings": warnings,
        }

    async def _piece_codes(self, piece_ids: list) -> dict:
        ids = [p for p in piece_ids if p]
        if not ids:
            return {}
        from sqlalchemy import select
        from app.modules.production.models import Piece
        res = await self.db.execute(
            select(Piece.id, Piece.code).where(Piece.id.in_(tuple(ids))))
        return {pid: code for pid, code in res.all()}

    async def _present_cutters(self) -> list:
        """Who is in the factory today, so the grid cannot offer an absent name.

        Reuses the attendance roster rather than asking a second question:
        production's own `_assert_present` refuses to log a worker with no
        attendance row, so a grid that let one be assigned would collect the work
        and then fail at the scan — after the leather had already been handed out.
        """
        from app.modules.attendance.service import AttendanceService
        try:
            roster = await AttendanceService(self.db).today_roster()
        except Exception:
            # The roster is a convenience, not a gate. If attendance is
            # unavailable the grid still has to open.
            return []
        out = []
        for entry in roster or []:
            emp_id = (entry.get("employee_id") if isinstance(entry, dict)
                      else getattr(entry, "employee_id", None))
            name = (entry.get("name") if isinstance(entry, dict)
                    else getattr(entry, "name", None))
            desig = (entry.get("designation") if isinstance(entry, dict)
                     else getattr(entry, "designation", None))
            if emp_id is None:
                continue
            out.append({"employee_id": emp_id, "name": name,
                        "designation": desig})
        return out

    # ══════════════════════════════════════════════════════════ generating
    async def generate(self, body, *, entered_by: str | None = None) -> dict:
        """One DRAFT row per un-cut garment, with its hides already on it.

        IDEMPOTENT BY CONSTRUCTION. `uncut_pieces` selects pieces with NO cutting
        row, so running this twice creates nothing the second time — the same
        "re-tap is a no-op" rule attendance check-in and the accessory kit both
        follow. It is what makes the button safe to press again when a manager is
        not sure it worked.
        """
        style = await self.repo.get_style(body.style_id)
        if style is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Style not found.")

        candidates = await self.repo.uncut_pieces(
            body.style_id, colour=body.colour, limit=body.limit)
        if not candidates:
            return {"created": 0, "rows": [],
                    "warnings": ["Every piece of this style already has a "
                                 "cutting row."]}

        warnings: list[str] = []
        lot = None
        if body.allocate:
            lot, lot_warning = await self._resolve_lot(style, body)
            if lot_warning:
                warnings.append(lot_warning)

        spec_dcm = await self._spec_dcm_by_sku(body.style_id)
        mats = self._materials()
        # Read the free hides ONCE for the whole generate: re-querying per row
        # would re-offer a hide an earlier row in this same loop already claimed.
        free = list(await mats.repo.allocatable_sheets(lot.id)) if lot else []

        created = []
        for piece, sku in candidates:
            target, source = leather_target_dcm(
                size=sku.size, spec_dcm_per_piece=spec_dcm.get(sku.id))
            row = self.repo.add_row_nocommit(
                piece_id=piece.id, style_id=body.style_id, sku_id=sku.id,
                article=(lot.article if lot else style.article),
                colour=(lot.colour if lot else (sku.color_name or sku.color_code)),
                size=sku.size, rc_no=None,
                cutter_employee_id=body.cutter_employee_id,
                work_date=body.work_date or date.today(),
                status=CuttingRowStatus.DRAFT.value,
                target_dcm=Decimal(str(target)), target_source=source,
                note=None)
            await self.db.flush()          # need row.id to claim hides against

            if lot is not None:
                taken = self._take_sheets(free, Decimal(str(target)))
                for sheet in taken:
                    sheet.cutting_row_id = row.id
                    sheet.status = SheetStatus.ALLOCATED.value
                # RUNNING OUT MID-GENERATE IS THE COMMON CASE, NOT AN EDGE ONE.
                # The pool is whatever leather happens to be on the shelf, so a
                # 40-garment style routinely exhausts it partway down. Warning
                # only on an EMPTY row hid that: row 1 came out whole, row 2 came
                # out at a quarter of its target with nothing said, and the
                # manager approved it because the grid looked populated.
                got = sum((Decimal(str(s.dcm or 0)) for s in taken), Decimal(0))
                if not taken:
                    warnings.append(
                        f"{piece.code}: no hides left in {lot.article} — the row "
                        f"was created empty. Receive more leather, or add sheets "
                        f"by hand.")
                elif got < Decimal(str(target)):
                    warnings.append(
                        f"{piece.code}: only {float(got):g} dcm of hide was left "
                        f"against a target of {float(target):g} — the stock ran "
                        f"out partway through. Add sheets before approving.")
            created.append(row)

        await self.db.commit()

        piece_codes = await self._piece_codes([r.piece_id for r in created])
        return {
            "created": len(created),
            "rows": [await self._row_payload(r, piece_codes=piece_codes)
                     for r in created],
            "warnings": warnings,
        }

    @staticmethod
    def _take_sheets(free: list, target: Decimal) -> list:
        """Pick hides off `free` until they reach `target`. MUTATES `free`.

        SMALLEST FIRST, because the repository hands them over sorted that way:
        spending the offcuts before breaking into a big skin is what a cutting
        manager does, and it leaves the large hides available for the large sizes
        that actually need them.

        IT OVERSHOOTS RATHER THAN UNDERSHOOTS. The loop takes one more hide when
        it is still short, so the row lands at or just above the target. A sheet
        too many is handed back in thirty seconds; a sheet too few stops a cutter
        mid-garment and sends him to find the manager.
        """
        taken, running = [], Decimal(0)
        while free and running < target:
            sheet = free.pop(0)
            taken.append(sheet)
            running += Decimal(str(sheet.dcm or 0))
        return taken

    async def _resolve_lot(self, style, body):
        """Which leather these garments are cut from. Never a guess.

        An explicit lot wins. Otherwise the style's article + the requested
        colour must name exactly ONE active leather lot: zero is a 404 that says
        what to receive, several is a 409 that says to pick one. Guessing here
        would spend the wrong hides, and hides are the expensive thing.
        """
        mats = self._materials()
        if body.material_lot_id:
            lot = await mats.repo.get_lot(body.material_lot_id)
            if lot is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "Material lot not found.")
            if (lot.category or "").upper() != MaterialCategory.LEATHER.value:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Lot {lot.article} is {lot.category}, not LEATHER. A "
                    f"cutting row is cut from hides.")
            return lot, None

        lots = await mats.repo.find_lots(
            category=MaterialCategory.LEATHER.value,
            article=style.article, colour=body.colour)
        lots = [l for l in lots if l.is_active]
        if not lots:
            return None, (
                f"No active leather lot matches {style.article or 'this style'}"
                f"{' · ' + body.colour if body.colour else ''}. The rows were "
                f"created with no sheets — receive the leather, then add sheets "
                f"to each row.")
        if len(lots) > 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{len(lots)} leather lots match {style.article}"
                f"{' · ' + body.colour if body.colour else ''}. Send "
                f"material_lot_id to say which hides to spend — this is not "
                f"guessed, because the wrong choice spends the wrong leather.")
        return lots[0], None

    async def _spec_dcm_by_sku(self, style_id: uuid.UUID) -> dict:
        """{sku_id: dcm per piece} from the style's confirmed material spec.

        A measurement somebody signed off beats this system's size estimate, so
        it is looked up once per generate and handed to leather_norms. A style
        with no spec returns {} and every row falls back to the baseline.
        """
        try:
            from app.modules.materials.style_spec_service import StyleSpecService
            from sqlalchemy import select
            from app.modules.clients.models import SKU
            spec = StyleSpecService(self.db)
            skus = list((await self.db.execute(
                select(SKU).where(SKU.style_id == style_id))).scalars().all())
            out = {}
            for sku in skus:
                lines = await spec.effective_lines(style_id, sku.id)
                for line in lines:
                    if (line.category or "").upper() == MaterialCategory.LEATHER.value:
                        out[sku.id] = float(line.qty_per_piece or 0)
                        break
            return out
        except Exception:
            # The spec is an optimisation, not a dependency. A style without one
            # (every style released before the spec feature) must still generate.
            return {}

    # ══════════════════════════════════════════════════════════ editing
    async def _editable(self, row):
        """DRAFT is editable; anything else is not, and says why.

        APPROVED IS THE FREEZE LINE. Past it the numbers are what the production
        logger will spend and what the garment's cost is computed from, so an
        edit underneath them would rewrite a signed-off figure with no trace.
        Reopen instead — that is audited.
        """
        if row.status != CuttingRowStatus.DRAFT.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"This row is {row.status}, so its sheets and quantities are "
                f"frozen — they are what the cutting log will spend. Reopen it "
                f"first if it genuinely needs to change; that is recorded.")

    async def get_row_payload(self, row_id: uuid.UUID) -> dict:
        row = await self._require_row(row_id)
        codes = await self._piece_codes([row.piece_id])
        return await self._row_payload(row, piece_codes=codes)

    async def _require_row(self, row_id: uuid.UUID):
        row = await self.repo.get_row(row_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Cutting row not found.")
        return row

    async def patch_row(self, row_id: uuid.UUID, body) -> dict:
        """Edit any cell of the row. Every field is optional."""
        row = await self._require_row(row_id)
        await self._editable(row)
        for field in ("cutter_employee_id", "size", "rc_no", "article",
                      "colour", "work_date", "note"):
            value = getattr(body, field, None)
            if value is not None:
                setattr(row, field, value)
        await self.db.commit()
        return await self.get_row_payload(row_id)

    async def add_sheet(self, row_id: uuid.UUID, body) -> dict:
        """Put one more hide on the row — the cutter needed another skin.

        THREE DOORS, because the floor has three situations: he scanned the hide
        (sheet_code), the manager picked it off the lot list (sheet_id), or the
        delivery was never sheeted and the hide has to be created now (dcm).
        """
        row = await self._require_row(row_id)
        await self._editable(row)
        mats = self._materials()

        if body.dcm is not None:
            lot_id = body.material_lot_id or await self._row_lot_id(row)
            if lot_id is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "This row has no sheets yet, so there is no lot to create "
                    "one against. Send material_lot_id with the dcm.")
            lot = await mats.repo.get_lot(lot_id)
            if lot is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Lot not found.")
            minted = await mats.mint_sheets_nocommit(lot, [{"dcm": body.dcm}])
            sheet = minted[0]
            # It was created FOR this row, so it never passes through IN_STOCK —
            # a hide that is already in a cutter's hands must not be offerable to
            # another row for even one transaction.
            sheet.material_lot_id = lot.id
        else:
            sheet = await self._find_sheet(body)
            if sheet.status not in SHEET_ALLOCATABLE:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Sheet {sheet.code} is {sheet.status}"
                    + (f" on another cutting row." if sheet.cutting_row_id
                       and sheet.cutting_row_id != row.id else ".")
                    + " Only a hide on the shelf can be put on a row.")

        sheet.cutting_row_id = row.id
        sheet.status = SheetStatus.ALLOCATED.value
        await self.db.commit()
        return await self.get_row_payload(row_id)

    async def _row_lot_id(self, row):
        mats = self._materials()
        existing = await mats.repo.sheets_for_row(row.id)
        return existing[0].material_lot_id if existing else None

    async def _find_sheet(self, body):
        mats = self._materials()
        if body.sheet_code:
            sheet = await mats.repo.get_sheet_by_code(body.sheet_code)
            if sheet is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"No hide carries the code '{body.sheet_code}'.")
            return sheet
        if body.sheet_id:
            sheet = await mats.repo.get_sheet(body.sheet_id)
            if sheet is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Sheet not found.")
            return sheet
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Name the hide to add: sheet_code (scanned), sheet_id (picked), or "
            "dcm (create a new one against the lot).")

    async def remove_sheet(self, row_id: uuid.UUID, sheet_id: uuid.UUID) -> dict:
        """The cutter handed a hide back. It returns to the shelf, not to nowhere.

        RETURNED rather than IN_STOCK, and the distinction is the point: both are
        allocatable, but the status keeps the hide's history honest about having
        been out and come back.
        """
        row = await self._require_row(row_id)
        await self._editable(row)
        mats = self._materials()
        sheet = await mats.repo.get_sheet(sheet_id)
        if sheet is None or sheet.cutting_row_id != row.id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "That hide is not on this row.")
        sheet.cutting_row_id = None
        sheet.status = SheetStatus.RETURNED.value
        await self.db.commit()
        return await self.get_row_payload(row_id)

    async def patch_sheet(self, row_id: uuid.UUID, sheet_id: uuid.UUID,
                          body) -> dict:
        """Correct a hide's measurement — the tannery's number was wrong or misread.

        The dcm lives on the HIDE, not on the row, so the correction follows the
        skin wherever it goes and the row's total recomputes from it.
        """
        row = await self._require_row(row_id)
        await self._editable(row)
        mats = self._materials()
        sheet = await mats.repo.get_sheet(sheet_id)
        if sheet is None or sheet.cutting_row_id != row.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "That hide is not on this row.")
        sheet.dcm = Decimal(str(body.dcm))
        await self.db.commit()
        return await self.get_row_payload(row_id)

    # ══════════════════════════════════════════════════════════ approval
    async def approve(self, row_id: uuid.UUID, *, actor_user_id, actor_name=None):
        """Freeze the row. A hard, audited transition (CLAUDE.md §15).

        WHAT APPROVAL MEANS: the cutter has confirmed the hides, the manager has
        seen them, and from here these numbers are what the production log will
        spend and what this garment's leather cost is computed from.

        STOCK DOES NOT MOVE HERE. The hides go ISSUED — out of the shelf's reach
        — but `lot.on_hand` is untouched, because consumption still happens at the
        cutting log exactly as it does today. That keeps ONE decrement point for
        leather and leaves every existing consumption test, shortfall warning and
        analytics read true.
        """
        row = await self._require_row(row_id)
        if row.status == CuttingRowStatus.APPROVED.value:
            # Re-approving is a no-op, not an error: the manager could not tell
            # whether the first tap landed, and punishing him for checking is how
            # people learn to avoid the button.
            return {"row": await self.get_row_payload(row_id),
                    "message": f"Row was already approved."}
        if row.status != CuttingRowStatus.DRAFT.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Only a DRAFT row can be approved; this one is {row.status}.")

        mats = self._materials()
        sheets = await mats.repo.sheets_for_row(row.id)
        if not sheets:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This row has no sheets, so there is nothing to approve. Add the "
                "hides the cutter was given first.")
        if row.cutter_employee_id is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Assign the cutter before approving — the cutting log is what "
                "their wage is paid from, and it cannot be filled in later "
                "without rewriting a signed-off row.")

        total = sum((Decimal(str(s.dcm or 0)) for s in sheets), Decimal(0))
        now = datetime.now(timezone.utc)
        row.total_dcm = total
        row.status = CuttingRowStatus.APPROVED.value
        row.approved_by = actor_user_id
        row.approved_at = now
        for sheet in sheets:
            sheet.status = SheetStatus.ISSUED.value

        await self._audit(actor_user_id, BarcodeAuditAction.CUTTING_ROW_APPROVED,
                          row.id,
                          {"piece_id": str(row.piece_id) if row.piece_id else None,
                           "sheets": [s.code for s in sheets],
                           "total_dcm": float(total),
                           "target_dcm": float(row.target_dcm or 0),
                           "cutter_employee_id": (str(row.cutter_employee_id)
                                                  if row.cutter_employee_id else None),
                           "by": actor_name})
        await self.db.commit()
        return {
            "row": await self.get_row_payload(row_id),
            "message": (f"Approved: {len(sheets)} sheet(s), "
                        f"{float(total):g} dcm. Scan the piece on the cutting "
                        f"screen to log it."),
        }

    async def reopen(self, row_id: uuid.UUID, *, actor_user_id, reason=None,
                     actor_name=None) -> dict:
        """Unfreeze an approved row. Audited, because it un-signs a signature.

        A LOGGED row is NOT reopenable: its cutting event exists, its hides are
        consumed and its wage line may already be paid. Correcting that is a
        production-side correction, not an edit to the plan that preceded it.
        """
        row = await self._require_row(row_id)
        if row.status == CuttingRowStatus.LOGGED.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This row has already been logged as cut — its hides are spent. "
                "Correct the production event instead; reopening the plan would "
                "not un-cut the leather.")
        if row.status != CuttingRowStatus.APPROVED.value:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"Only an APPROVED row can be reopened; this one "
                                f"is {row.status}.")
        mats = self._materials()
        sheets = await mats.repo.sheets_for_row(row.id)
        row.status = CuttingRowStatus.DRAFT.value
        row.approved_by = None
        row.approved_at = None
        row.total_dcm = None
        for sheet in sheets:
            sheet.status = SheetStatus.ALLOCATED.value
        await self._audit(actor_user_id, BarcodeAuditAction.CUTTING_ROW_REOPENED,
                          row.id, {"reason": reason, "by": actor_name,
                                   "sheets": [s.code for s in sheets]})
        await self.db.commit()
        return await self.get_row_payload(row_id)

    # ══════════════════════════════════ the production logger's entry point
    async def approved_row_for_piece(self, piece_id: uuid.UUID):
        """The approved cutting record for this garment, or None.

        Read by the production logger so a cut scan does not re-ask for article,
        colour, lot and dcm that a human already entered and approved.
        """
        row = await self.repo.row_for_piece(piece_id)
        if row is None or row.status != CuttingRowStatus.APPROVED.value:
            return None
        return row

    async def mark_logged_nocommit(self, row, *, when=None) -> None:
        """The cut was logged: freeze the row and spend its hides. NO COMMIT.

        The caller (the production service) owns the transaction so the event,
        the stock decrement and these status moves land together — a hide marked
        CONSUMED against an event that rolled back would be leather the system
        believes is gone.
        """
        mats = self._materials()
        now = when or datetime.now(timezone.utc)
        row.status = CuttingRowStatus.LOGGED.value
        row.logged_at = now
        for sheet in await mats.repo.sheets_for_row(row.id):
            sheet.status = SheetStatus.CONSUMED.value
            sheet.consumed_at = now

    async def _audit(self, actor_user_id, action, entity_id, after: dict) -> None:
        """`actor_user_id` is an app_user.id — the LOGIN — never an employee.id.

        AuditLog.actor_user_id is a foreign key to app_user, and passing the
        scanned worker's id is what produced a 500 on the store scan. The worker
        travels in `after` as data.
        """
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_user_id,
            action=getattr(action, "value", str(action)),
            entity_type="cutting_row", entity_id=entity_id, after=after,
            at=datetime.now(timezone.utc)))
