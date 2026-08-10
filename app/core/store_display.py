"""
================================================================================
core/store_display.py — The STORE overlay: a DERIVED display stage, not an enum
================================================================================

WHY THIS EXISTS
--------------------------------------------------------------------------------
The user-facing pipeline is:

    LEATHER_CUTTING → FUSING → PASTING ─┐
                                         ├─► STORE ─► LINE_STITCHING → SHELL_
    LINING_CUTTING ──────────────────────┘          STITCHING → FINAL_FINISH →
                                                     FINAL_INSPECTION → PACKAGE

STORE sits between the cut side and LINE_STITCHING. But STORE is NOT a
ProductionEvent — a piece never "does work" at STORE. STORE is the state of the
piece's DRAWER: leather arriving, lining arriving, the DM confirming (RECEIVED)
and releasing (SEND). So STORE cannot be a ProductionStage enum member — putting
it in leather_chain() would break the event-driven sequence inference
(completed_stage_codes → furthest event + 1), because there is no STORE event to
find.

THE RULE (Hamthan, this build)
    "After PASTING or LINING_CUTTING the piece is in STORE, and every manager must
     SEE it in STORE in the production endpoint. The stage MUST NOT read
     LINE_STITCHING until the line-stitching work is actually done and its event
     is submitted."

So STORE is computed at READ time from two facts we already store:
    - the piece's furthest completed production EVENT (its real current_stage), and
    - its DRAWER's state (holding_leather / holding_lining / holding_both /
      received / sended).

display_stage() returns what the UI shows. It NEVER invents a LINE_STITCHING that
hasn't been logged: a piece only shows LINE_STITCHING once a LINE_STITCHING event
exists. Until then, once it has cleared the cut side, it shows STORE + a
sub-status so managers know exactly what the store is waiting for.

This module has NO imports from production/drawers/barcode services — it takes
plain values, so it can be called from any read path without a cycle.
================================================================================
"""
from __future__ import annotations

# Real production stages (event-backed). Kept as plain strings so this module
# imports nothing heavy; they mirror ProductionStage values exactly.
_LEATHER_CUT = "LEATHER_CUTTING"
_LINING_CUT = "LINING_CUTTING"
_FUSING = "FUSING"
_PASTING = "PASTING"
_LINE_STITCHING = "LINE_STITCHING"

# The virtual display stage. Not a ProductionStage member.
STORE = "STORE"

# Drawer states (mirror DrawerState values).
_D_WAITING = "waiting"
_D_MERGED = "merged"
_D_HOLDING_LEATHER = "holding_leather"
_D_HOLDING_LINING = "holding_lining"
_D_HOLDING_BOTH = "holding_both"
_D_RECEIVED = "received"
_D_SENDED = "sended"

# The last real event-stage on the CUT side that hands off to STORE.
# After either of these (and before LINE_STITCHING), the piece is "in store".
_CUT_SIDE_TERMINALS = {_PASTING, _LINING_CUT}


# Human sub-labels for the store sub-status. Drives the caption managers read.
_STORE_SUBLABEL = {
    _D_MERGED:          "In store · awaiting parts",
    _D_HOLDING_LEATHER: "In store · holding leather · awaiting lining",
    _D_HOLDING_LINING:  "In store · holding lining · awaiting leather",
    _D_HOLDING_BOTH:    "In store · holding both · awaiting DM receive",
    _D_RECEIVED:        "In store · received · awaiting send",
    _D_SENDED:          "In store · sent · moved to line-stitching (not yet worked)",
    _D_WAITING:         "In store",
}


def display_stage(
    *,
    current_event_stage: str | None,
    drawer_state: str | None,
    needs_lining: bool = True,
) -> dict:
    """Resolve what the production/barcode UI should SHOW for a piece.

    Parameters
    ----------
    current_event_stage : the operation code of the piece's furthest real
        production EVENT (Piece.current_operation_id's op code), or None if the
        piece has never been logged.
    drawer_state : the piece's drawer state (DrawerState value), or None if the
        piece has no drawer (already shipped / released).
    needs_lining : whether this piece needs a lining at all (leather-only pieces
        are "complete" on leather alone).

    Returns
    -------
    dict with:
        display_stage  : the stage string the UI shows (may be STORE)
        in_store       : bool — True while the piece is parked in the store
        store_status   : the drawer sub-state driving the caption (or None)
        label          : a ready-to-render human caption
    """
    ev = (current_event_stage or "").strip().upper() or None
    ds = (drawer_state or "").strip().lower() or None

    # 1) The piece has an actual LINE_STITCHING (or later) event → it has left
    #    the store for real. Show the real event stage. (Once line-stitching is
    #    logged, current_event_stage is LINE_STITCHING or beyond, so this branch
    #    is simply "a real post-store stage exists".)
    if ev is not None and ev not in _CUT_SIDE_TERMINALS and ev not in (
        _LEATHER_CUT, _FUSING, _LINING_CUT
    ):
        # ev is LINE_STITCHING / SHELL_STITCHING / FINAL_* / PACKAGE — a real,
        # event-backed stage past the store. Show it as-is.
        return {
            "display_stage": ev,
            "in_store": False,
            "store_status": None,
            "label": ev.replace("_", " ").title(),
        }

    # 2) The piece is on the cut side or just handed off. If its drawer is in a
    #    HOLDING state (a part has actually arrived in the store) or beyond, it is
    #    IN STORE — regardless of whether the last event was the leather-side
    #    terminal (PASTING) or the lining cut. The drawer, not the last event,
    #    decides the store sub-status.
    #
    #    Bare MERGED is EXCLUDED here: a drawer is MERGED at upload before any
    #    part is scanned in, so a piece that is only part-way through the leather
    #    chain (e.g. leather cut, fusing pending) but whose drawer has had nothing
    #    scanned in should still show its REAL cut-side stage, not STORE. It
    #    enters STORE the moment the first part is scanned (holding_*), or once
    #    it has cleared the cut-side terminal (handled just below).
    holding_states = {
        _D_HOLDING_LEATHER, _D_HOLDING_LINING,
        _D_HOLDING_BOTH, _D_RECEIVED, _D_SENDED,
    }
    # A piece that has cleared the cut side (its last event is PASTING or
    # LINING_CUTTING) is in the store even if its drawer still reads MERGED —
    # it's waiting for its other part to be scanned in.
    cleared_cut_side = ev in _CUT_SIDE_TERMINALS
    if ds in holding_states or (ds == _D_MERGED and cleared_cut_side):
        if ds == _D_MERGED:
            ds = _D_MERGED  # keep sub-label "awaiting parts"
        return {
            "display_stage": STORE,
            "in_store": True,
            "store_status": ds,
            "label": _STORE_SUBLABEL.get(ds, "In store"),
        }

    # 3) Still mid cut-side work (e.g. leather cut done, fusing/pasting pending)
    #    and not yet in a holding drawer → show the real cut-side event stage.
    if ev is not None:
        return {
            "display_stage": ev,
            "in_store": False,
            "store_status": None,
            "label": ev.replace("_", " ").title(),
        }

    # 4) Never logged, no drawer signal → not started.
    return {
        "display_stage": None,
        "in_store": False,
        "store_status": None,
        "label": "Not started",
    }