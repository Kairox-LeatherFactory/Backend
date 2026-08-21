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
    sku_id: uuid.UUID | None = None     # None = the style-wide default
    category: str
    subtype: str | None = None
    article: str
    colour: str | None = None
    thickness: str | None = None
    size: str | None = None
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
