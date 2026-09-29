"""
================================================================================
modules/materials/style_spec_schemas.py — API contract for the per-piece recipe
================================================================================
KEPT OUT OF materials/schemas.py DELIBERATELY. That file is the lot + supplier
contract — what the factory HAS. This one is what a garment NEEDS. They meet at
the six identity columns and nowhere else, and merging them would make one very
large module out of two small ones that change for different reasons.
================================================================================
"""
import uuid

from pydantic import BaseModel, Field


class SpecLineIn(BaseModel):
    """One line of the recipe, as posted by the authoring grid.

    `uom` IS ACCEPTED BUT IGNORED. The unit is a property of the material kind —
    buttons are pcs, thread is mtrs — so the server derives it from
    uom_for(category, subtype). It stays in the contract only so a client can
    round-trip a GET without stripping fields.
    """
    # WHICH GARMENTS THIS LINE IS FOR.
    #   ACCESSORY  — REQUIRED. A SKU is a colour and a size together, which is what
    #                decides whether an accessory goes in. Use apply_to / sku_ids /
    #                per_sku below to cover several in one call.
    #   LEATHER / LINING — optional; None means the style-wide default and
    #                garment_size is how such a line is scoped to a size.
    sku_id: uuid.UUID | None = None
    category: str
    subtype: str | None = None
    article: str | None = None
    colour: str | None = None
    thickness: str | None = None
    size: str | None = None
    # LEATHER AND LINING ONLY, and never inferred — send it or leave it NULL,
    # which means every size. It is REFUSED on an accessory line: an accessory says
    # which garments it is for by naming their SKU, and a second, independent size
    # scope could only contradict it.
    #
    # It used to be guessed from `size` when that read as a garment size. The guess
    # read any bare number from 30 to 70 as one (the EU jacket rungs), so a 60cm zip
    # became a size-60 garment's zip and was confined to 4XL — and a line that
    # reaches no garment is not a shorter recipe, it is no recipe.
    garment_size: str | None = None

    # ── covering several SKUs in one call ────────────────────────────────────
    # 85-90% of accessories are identical across a style's SKUs, so the common case
    # must not be eight requests. Exactly ONE of sku_id / apply_to / sku_ids /
    # per_sku may be sent; more than one is a 422, because which garments an
    # accessory is for is not something to guess at. The STORED shape is per-SKU
    # either way — the convenience is in the request, never in the data.
    apply_to: str | None = None           # "ALL_SKUS" — every ordered SKU
    sku_ids: list[uuid.UUID] | None = None
    # For the accessories whose size follows the garment — zip, rib knit trim.
    # Each entry may override size / qty_per_piece / colour / thickness /
    # material_lot_id / note for its own SKU.
    per_sku: list[dict] | None = None
    qty_per_piece: float
    uom: str | None = None              # derived; see the docstring
    material_lot_id: uuid.UUID | None = None
    note: str | None = None


class SpecReplace(BaseModel):
    """Save the whole grid. Idempotent on the line identity."""
    lines: list[SpecLineIn] = Field(default_factory=list)


class SpecLinePatch(BaseModel):
    """Partial edit of one line. Omitted fields keep their current value."""
    sku_id: uuid.UUID | None = None
    category: str | None = None
    subtype: str | None = None
    article: str | None = None
    colour: str | None = None
    thickness: str | None = None
    size: str | None = None
    # PATCHABLE, AND IT WAS NOT. Without this field the only way to correct which
    # garment sizes a leather/lining line is for was to rewrite the whole grid —
    # and worse, patching any other field silently cleared it. See patch_line.
    garment_size: str | None = None
    qty_per_piece: float | None = None
    material_lot_id: uuid.UUID | None = None
    note: str | None = None


class SpecConfirm(BaseModel):
    """The sign-off that unlocks release.

    `no_accessories: true` is the DM stating this garment takes none. It is the
    only thing that lets a style with an empty accessory list through the release
    gate — an empty list on its own is ambiguous (nobody entered them yet?) and
    releasing that silently is how a whole order reaches the store with no kit.
    Sending true while accessory lines exist is a 422, not a precedence rule.
    """
    no_accessories: bool = False


class SpecCopyFrom(BaseModel):
    source_style_id: uuid.UUID
    # SKU overrides only copy where both styles share a (colour, size); unmatched
    # ones are reported in `skipped_detail` rather than guessed onto the wrong
    # colourway, which would silently issue the wrong colour button.
    include_sku_overrides: bool = False


class ManualIssue(BaseModel):
    """Record a material issued OFF-SPEC to one garment.

    The escape hatch that makes freezing the recipe at release acceptable: a
    released style's spec cannot be edited, so a wrong article is corrected by
    recording what was ACTUALLY issued rather than by rewriting the recipe under
    live garments.
    """
    piece_barcode: str | None = None
    piece_id: uuid.UUID | None = None
    material_lot_id: uuid.UUID | None = None
    lot_barcode: str | None = None
    employee_barcode: str | None = None
    employee_id: uuid.UUID | None = None
    qty: float
    note: str | None = None
