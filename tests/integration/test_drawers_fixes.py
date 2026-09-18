"""
================================================================================
tests/test_drawers_fixes.py — regression guards for the DRAWERS module
================================================================================
Covers:
  F07 — a lining-first scan reports HOLDING_LINING (not HOLDING_LEATHER)
  F08 — a RECEIVED/SENDED drawer refuses a new part scan (no reopen)
  F09 — SENDED cannot transition back to RECEIVED
  F11 — release clears BOTH sides of the piece<->drawer link
================================================================================
"""

import pytest

# ══════════════════════════════════════════════════════════════════════════════
# RETIRED WITH THE DRAWER — source-inspection of DrawerService.
#
# There were 200 physical drawers. A style releases 100+ garments, stalled
# mid-chain, and the surplus were minted onto a "waiting for a drawer" list that
# the merge gate then refused to line-stitch — so the DM had to re-allocate boxes
# by hand, which in practice did not happen. Since 20260902_store_piece the store
# is a STATE on the garment (piece.store_state), and a state has no capacity.
#
# THE RULES THIS FILE ASSERTED ARE NOT LOST. Every one of them — completeness,
# auto-receive, the store-entry gate, the lining verdict (including the stale
# needs_lining flag that let a KNIT jacket reach PACKAGE_EXPORT unlined), the
# merge gate that opens LINE_STITCHING, partial-accept send, the PACKAGE_EXPORT
# release and the piece lookup — is carried forward in
# tests/integration/test_store_merge.py, against the API the floor now uses.
#
# What is NOT carried forward, deliberately: "a piece scanned into the wrong
# drawer is a 409". There is no wrong drawer. That rejection policed an
# assignment the system invented at upload, and its absence is the feature.
#
# The file is kept rather than deleted so the drawer's behaviour stays readable
# while the tables are still in the database (they are retained, unwritten, for
# audit). It goes when they do.
# ══════════════════════════════════════════════════════════════════════════════


import pytest


# ── F07: the missing state member exists (pure) ─────────────────────────────
def test_f07_holding_lining_state_exists():
    from app.core.enums import DrawerState
    assert DrawerState.HOLDING_LINING.value == "holding_lining"


def test_f07_state_derivation_branches_on_part():
    import inspect
    from app.modules.drawers import service as drawer_service
    src = inspect.getsource(drawer_service.DrawerService.store_scan)
    # must branch to HOLDING_LINING for a lining-only drawer, not force LEATHER
    assert "HOLDING_LINING" in src


# ── F08: source-state guard against reopening a released drawer (structural) ─
def test_f08_store_scan_guards_terminal_states():
    import inspect
    from app.modules.drawers import service as drawer_service
    src = inspect.getsource(drawer_service.DrawerService.store_scan)
    assert "RECEIVED" in src and "SENDED" in src
    assert "cannot accept" in src.lower() or "already" in src.lower()


# ── F09: SENDED -> RECEIVED is blocked (structural) ─────────────────────────
def test_f09_received_guards_against_sended():
    import inspect
    from app.modules.drawers import service as drawer_service
    src = inspect.getsource(drawer_service.DrawerService.transition)
    assert "already SENDED" in src


# ── F11: release clears both sides of the link (structural) ─────────────────
def test_f11_release_clears_piece_side():
    import inspect
    from app.modules.drawers import service as drawer_service
    src = inspect.getsource(drawer_service.DrawerService.release_nocommit)
    assert "piece.drawer_id = None" in src
    assert "drawer.current_piece_id = None" in src


pytestmark = pytest.mark.skip(reason="Drawers are retired; see tests/integration/test_store_merge.py")
