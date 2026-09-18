"""HTTP shapes for the store. One scan fewer than the drawer flow had."""
import uuid

from pydantic import BaseModel, Field, model_validator


class KitLineRequest(BaseModel):
    spec_id: uuid.UUID | None = None
    qty: float | None = None
    material_lot_id: uuid.UUID | None = None


class StoreScanRequest(BaseModel):
    """Scan the worker, scan the garment. THAT IS THE WHOLE FLOW.

    THE DRAWER SCAN IS GONE, and removing it is the feature. The old flow was
    employee → DRAWER → piece, with a 409 when the piece was not the one that
    drawer had been assigned at upload. That third scan existed only to find a
    numbered box; the box is gone, so what is left is the worker and the garment.

    `part` stays OPTIONAL and is inferred from the garment's own history — the
    operator should not be asked a question the system can answer. ACCESSORY is
    the exception and must always be explicit: an inferred accessory scan would
    SPEND STOCK against the style's recipe, and a mis-inference would move money
    nothing on the floor asked to move.
    """
    employee_barcode: str | None = None
    employee_id: uuid.UUID | None = None
    piece_barcode: str | None = None
    piece_id: uuid.UUID | None = None
    part: str | None = None                    # LEATHER | LINING | ACCESSORY
    lines: list[KitLineRequest] | None = None  # a partial/substituted kit issue

    @model_validator(mode="after")
    def _need_both(self):
        if not (self.employee_barcode or self.employee_id):
            raise ValueError("Scan the worker's card first.")
        if not (self.piece_barcode or self.piece_id):
            raise ValueError("Scan the garment.")
        return self


class StoreScanResult(BaseModel):
    piece_code: str
    store_state: str
    holding: str                 # HOLDING LEATHER / LINING / BOTH / EMPTY
    leather_in: bool
    lining_in: bool
    accessories_in: bool
    part_stored: str | None
    part_inferred: bool
    complete: bool
    # What this garment is still owed — LEATHER / LINING / ACCESSORY. Returned so
    # the screen never has to re-implement the completeness rule; a second
    # implementation is a second implementation that can disagree.
    awaiting: list[str] = Field(default_factory=list)
    # True when THIS scan completed the garment and it received itself.
    auto_received: bool = False
    # Alias of `complete`, kept because that is what the store screen calls it.
    ready_for_received: bool = False
    # Has it left the store? A garment can be complete and still on the shelf.
    sent: bool = False
    needs_lining: bool
    lining_reason: str | None = None
    kit: dict | None = None
    next_action: str
    warnings: list[str] = Field(default_factory=list)


class StoreSendRequest(BaseModel):
    """Release garments to line-stitching. Piece ids, not drawer ids."""
    piece_ids: list[uuid.UUID] = Field(default_factory=list)
    piece_barcodes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.piece_ids and not self.piece_barcodes:
            raise ValueError("Select at least one garment to send.")
        return self


class StoreSendResult(BaseModel):
    count_sent: int = 0
    sent: list[str] = Field(default_factory=list)
    not_ready: list[dict] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    message: str


class StorePieceRow(BaseModel):
    piece_id: uuid.UUID
    piece_code: str
    store_state: str
    holding: str
    leather_in: bool
    lining_in: bool
    accessories_in: bool
    needs_lining: bool
    complete: bool
    style_name: str | None = None
    colour: str | None = None
    size: str | None = None


class StoreList(BaseModel):
    count: int
    pieces: list[StorePieceRow] = Field(default_factory=list)
