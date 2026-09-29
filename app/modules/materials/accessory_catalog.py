"""
================================================================================
materials/accessory_catalog.py — the accessory kinds, as data
================================================================================
WHY THIS EXISTS

    `core.enums_barcode.MATERIAL_SPEC` is the single source of truth for what a
    material of a given (category, subtype) must carry. It is a hardcoded dict, and
    for LEATHER and LINING that is right: those three shapes are not going to grow.

    Accessories are different. Buttons, zips and thread were the only ones named, so
    everything else — eyelets, lace pins, rib knit trim — fell to `OTHER`, whose
    spec requires `description` + `count` and whose filters are `article, colour`
    with NO SIZE. An accessory whose size varies per SKU therefore had no size field
    to vary, which is the opposite of what the floor needs.

    So accessory kinds move into a table (barcode.models.AccessoryType) that the
    material-intake path fills in by itself, and this module is the seam between
    the two.

WHAT IT DOES NOT DO: REPLACE `resolve_spec`

    `resolve_spec` and `uom_for` are PURE FUNCTIONS in core/ and they stay pure.
    They are mirrored in the unit layer, they are imported on hot paths, and making
    them async would put a database round trip inside every lot read. So the
    built-ins remain the defaults and this OVERLAYS them:

        built-in (BUTTON/ZIP/THREAD/OTHER)  ->  returned unchanged when no row
        catalogue row                        ->  wins, field by field
        neither                              ->  None, and the caller 422s

    That also means every existing call site keeps working with no catalogue rows
    present at all, which is what makes this safe to deploy before it is seeded.

THE CACHE, AND WHY IT IS PER-PROCESS

    An accessory kind changes when somebody receives a new kind of accessory —
    measured in weeks, not seconds — while `spec_for` is asked on every lot create,
    every arrival and every recipe line. So the table is read once and held, and the
    cache is dropped on write through `invalidate()`. It is a plain module-level
    dict rather than Redis because it is tiny, derived, and safe to rebuild: a stale
    process at worst re-reads one row.
================================================================================
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MaterialCategory, resolve_spec, uom_for
from app.modules.barcode.models import AccessoryType

# `subtype` is String(20) on material_lot, style_material_spec,
# piece_material_issue and kit_substitution_request. Capping the source is cheaper
# and safer than widening four columns, one of which is a ledger.
CODE_MAX = 20
_CODE_OK = re.compile(r"^[A-Z0-9_]+$")

def normalise_code(raw: str | None) -> str | None:
    """'rib knit trim' -> 'RIB_KNIT_TRIM'. None when there is nothing usable.

    UPPERCASE AND UNDERSCORED, the same discipline `Designation.normalise` applies
    to job titles and for the same reason: this is a controlled vocabulary that a
    filter matches on, and 'Eyelet' / 'eyelets' / 'EYELETS' being three types is
    how a stock screen stops adding up.
    """
    token = re.sub(r"[\s\-/]+", "_", str(raw or "").strip()).upper()
    token = re.sub(r"[^A-Z0-9_]", "", token).strip("_")
    return token or None


class AccessoryCatalog:
    """Reads and writes the accessory kinds. One instance per request is fine."""

    def __init__(self, db: AsyncSession):
        self.db = db
        # {code: spec}. PER INSTANCE, and deliberately not a module global.
        #
        # A process-wide cache was the first thing written here and it was wrong in
        # two ways. In production, worker A registering a kind invalidates only A's
        # copy — worker B goes on answering "unknown" until something happens to
        # invalidate it there, so a kind just registered at the gate could be
        # refused by create_lot on the next request. In the tests it leaked across
        # cases: a kind registered against one test's database was still cached when
        # the next test looked at a clean one.
        #
        # An instance lives for one service call, which is exactly the right
        # lifetime: a fan-out of eight recipe lines shares one StyleSpecService and
        # so loads once, while nothing survives the request to go stale.
        self._cache: dict[str, dict] | None = None

    def invalidate(self) -> None:
        """Drop this instance's view. Called after any write to accessory_type."""
        self._cache = None

    # ── reading ──────────────────────────────────────────────────────────────
    async def _load(self) -> dict[str, dict]:
        if self._cache is not None:
            return self._cache
        rows = (await self.db.execute(
            select(AccessoryType).where(AccessoryType.is_active.is_(True)))
        ).scalars().all()
        self._cache = {r.code: self._as_spec(r) for r in rows}
        return self._cache

    @staticmethod
    def _as_spec(row: AccessoryType) -> dict:
        """A catalogue row in MATERIAL_SPEC's shape, so callers cannot tell them apart."""
        return {
            "required": set(row.requires or []),
            "qty_field": row.qty_field or "count",
            "qty_uom": row.qty_uom or "pcs",
            "filters": list(row.filters or ["article", "colour"]),
            # Extras the built-ins do not carry. Callers that do not know about
            # them ignore them; the recipe form and copy_from read them.
            "code": row.code,
            "label": row.label or row.code,
            "size_varies_by_sku": bool(row.size_varies_by_sku),
        }

    async def spec_for(self, category: str | None,
                       subtype: str | None) -> dict | None:
        """The spec for a (category, subtype) — catalogue first, built-in second.

        Non-accessory categories go straight to the built-ins: leather and lining
        are not catalogued and never will be.
        """
        cat = (category or "").upper()
        if cat != MaterialCategory.ACCESSORY.value:
            return resolve_spec(cat, subtype)
        code = normalise_code(subtype)
        if not code:
            return None
        row = (await self._load()).get(code)
        if row is not None:
            return row
        return resolve_spec(cat, code)

    async def uom_for(self, category: str | None, subtype: str | None) -> str:
        """`uom_for`, but catalogue-aware. The unit is never a caller's choice."""
        spec = await self.spec_for(category, subtype)
        if spec and spec.get("qty_uom"):
            return spec["qty_uom"]
        return uom_for(category or "", subtype)

    async def known_codes(self) -> list[str]:
        """Every accessory code, catalogue and built-in, for an error message."""
        builtin = {"BUTTON", "ZIP", "THREAD", "OTHER"}
        return sorted(builtin | set((await self._load()).keys()))

    async def size_varies_by_sku(self, subtype: str | None) -> bool:
        """Does this accessory's size change from one SKU to the next?

        False for a code nobody has catalogued: the safe default is "the same
        everywhere", because that is the 85-90% case and because guessing TRUE
        would ask the DM for a size per SKU on a button that has one size.
        """
        spec = await self.spec_for(MaterialCategory.ACCESSORY.value, subtype)
        return bool((spec or {}).get("size_varies_by_sku"))

    async def list_types(self) -> list[dict]:
        rows = (await self.db.execute(
            select(AccessoryType).order_by(AccessoryType.code))).scalars().all()
        return [{
            "code": r.code, "label": r.label or r.code,
            "qty_field": r.qty_field, "qty_uom": r.qty_uom,
            "requires": sorted(r.requires or []),
            "filters": list(r.filters or []),
            "size_varies_by_sku": bool(r.size_varies_by_sku),
            "is_active": bool(r.is_active),
            "first_seen_at": (r.first_seen_at.isoformat()
                              if r.first_seen_at else None),
            "created_by": r.created_by, "note": r.note,
        } for r in rows]

    # ── writing ──────────────────────────────────────────────────────────────
    async def register_nocommit(self, subtype: str | None, *,
                                created_by: str | None = None,
                                has_size: bool = False) -> dict | None:
        """Ensure an accessory kind exists. Returns its payload IF it was created.

        NO COMMIT — the caller owns the transaction, because this runs inside
        `create_lot` and `arrive`, which must land the lot, its barcode, its stock
        and this row together or not at all.

        RETURNS None WHEN NOTHING WAS CREATED, so the intake response can say "a
        new accessory kind appeared" and stay quiet otherwise. A type registered by
        accident at the gate has to be visible.

        THE DEFAULTS ARE THE BUTTON SHAPE — counted in pieces, wanting a size —
        because that is what most accessories are. `size` is required only when the
        arrival actually carried one: demanding it of a kind that has no size would
        make the next delivery of it fail the strict path for a field it cannot
        have.
        """
        code = normalise_code(subtype)
        if not code:
            return None
        if len(code) > CODE_MAX:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"'{code}' is {len(code)} characters and an accessory code may be "
                f"at most {CODE_MAX}, because that is the width of the `subtype` "
                f"column on the stock, recipe and issue-ledger tables. Use a "
                f"shorter code and put the full name in `label`.")
        if not _CODE_OK.match(code):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"'{code}' is not a usable accessory code — letters, digits and "
                f"underscores only.")

        # Already catalogued, or a built-in we are not going to shadow.
        if code in (await self._load()):
            return None
        if resolve_spec(MaterialCategory.ACCESSORY.value, code) is not None:
            return None

        required = ["count"] + (["size"] if has_size else [])
        filters = ["article", "colour"] + (["size"] if has_size else [])
        row = AccessoryType(
            code=code, label=code.replace("_", " ").title(),
            qty_field="count", qty_uom="pcs",
            requires=required, filters=filters,
            size_varies_by_sku=False, is_active=True,
            first_seen_at=datetime.now(timezone.utc), created_by=created_by,
            note="Registered automatically when this material first arrived. "
                 "Review its fields and whether its size varies by SKU.")
        self.db.add(row)
        # FLUSH, THEN PUBLISH. Sessions here are `autoflush=False`, so without this
        # the very next `spec_for()` — which `create_lot` calls immediately — would
        # reload from the database, MISS this pending row, fall back to the built-in
        # MATERIAL_SPEC, find nothing for a kind nobody hardcoded, and 422. The
        # failed request then rolled the registration back too, so receiving a
        # genuinely new accessory was broken outright: the kind never registered and
        # the lot was refused as "not an accessory kind".
        await self.db.flush()
        # SEEDED, NOT INVALIDATED. `_load()` ran a few lines above, so the cache is
        # populated and adding this one entry keeps it correct with no second query.
        # Invalidating would force that reload — which is what went wrong.
        spec = self._as_spec(row)
        if self._cache is None:
            self._cache = {}
        self._cache[code] = spec
        return {
            "code": code, "label": row.label, "qty_field": "count",
            "qty_uom": "pcs", "requires": required, "filters": filters,
            "size_varies_by_sku": False, "auto_registered": True,
            "message": (f"'{code}' is a new accessory kind and has been added to "
                        f"the catalogue. Check its fields, and set "
                        f"size_varies_by_sku if its size changes per SKU."),
        }

    async def patch_type(self, code: str, patch: dict, *,
                         actor_name: str | None = None) -> dict:
        """Refine a type. Commits.

        `code` AND `qty_field` ARE NOT PATCHABLE. The code is what `subtype` stores
        on every lot, recipe line and ledger row of that kind, so renaming it here
        would orphan all of them silently. `qty_field` is which attribute holds the
        quantity that was already added to `on_hand` — changing it would re-read
        historical stock through a different key.
        """
        norm = normalise_code(code)
        row = (await self.db.execute(
            select(AccessoryType).where(AccessoryType.code == norm))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No accessory kind '{norm}'. It is created by receiving one — see "
                f"POST /materials/arrivals — or listed at "
                f"GET /materials/accessory-types.")
        for field in ("code", "qty_field"):
            if patch.get(field) is not None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"`{field}` cannot be changed: every lot, recipe line and "
                    f"ledger row of this kind is keyed to it, and rewriting it "
                    f"here would orphan them without saying so.")
        if patch.get("label") is not None:
            row.label = str(patch["label"]).strip() or row.label
        if patch.get("qty_uom") is not None:
            row.qty_uom = str(patch["qty_uom"]).strip() or row.qty_uom
        if patch.get("requires") is not None:
            row.requires = sorted({str(x).strip().lower()
                                   for x in patch["requires"] if str(x).strip()})
        if patch.get("filters") is not None:
            row.filters = [str(x).strip().lower()
                           for x in patch["filters"] if str(x).strip()]
        if patch.get("size_varies_by_sku") is not None:
            row.size_varies_by_sku = bool(patch["size_varies_by_sku"])
        if patch.get("is_active") is not None:
            row.is_active = bool(patch["is_active"])
        if patch.get("note") is not None:
            row.note = str(patch["note"]).strip() or None
        row.created_by = row.created_by or actor_name
        await self.db.commit()
        self.invalidate()
        await self.db.refresh(row)
        return {
            "code": row.code, "label": row.label, "qty_field": row.qty_field,
            "qty_uom": row.qty_uom, "requires": sorted(row.requires or []),
            "filters": list(row.filters or []),
            "size_varies_by_sku": bool(row.size_varies_by_sku),
            "is_active": bool(row.is_active),
            "message": f"{row.code} updated.",
        }


# The kinds every deployment starts with: the four that were hardcoded, plus the
# ones the floor named. Seeded by 20260929_accessory_sku_scope and by
# scripts/seed.py, and kept here so both read one list.
SEED_TYPES: list[dict] = [
    {"code": "BUTTON", "label": "Button", "qty_field": "count", "qty_uom": "pcs",
     "requires": ["count", "size"], "filters": ["article", "colour", "size"],
     "size_varies_by_sku": False},
    {"code": "ZIP", "label": "Zip", "qty_field": "count", "qty_uom": "pcs",
     "requires": ["count", "size"], "filters": ["article", "colour", "size"],
     # THE ONE THE FLOOR SAID VARIES: a zip is cut to the garment's length.
     "size_varies_by_sku": True},
    {"code": "THREAD", "label": "Thread", "qty_field": "mtrs", "qty_uom": "mtrs",
     "requires": ["mtrs", "thickness"],
     "filters": ["article", "colour", "thickness"], "size_varies_by_sku": False},
    {"code": "OTHER", "label": "Other", "qty_field": "count", "qty_uom": "pcs",
     "requires": ["count", "description"], "filters": ["article", "colour"],
     "size_varies_by_sku": False},
    {"code": "EYELET", "label": "Eyelets", "qty_field": "count", "qty_uom": "pcs",
     "requires": ["count", "size"], "filters": ["article", "colour", "size"],
     "size_varies_by_sku": False},
    {"code": "LACE_PIN", "label": "Lace Pins", "qty_field": "count",
     "qty_uom": "pcs", "requires": ["count", "size"],
     "filters": ["article", "colour", "size"], "size_varies_by_sku": False},
    {"code": "RIB_KNIT_TRIM", "label": "Rib Knit Trim", "qty_field": "count",
     "qty_uom": "pcs", "requires": ["count", "size"],
     "filters": ["article", "colour", "size"],
     # The second one the floor named: its size follows the garment.
     "size_varies_by_sku": True},
]
