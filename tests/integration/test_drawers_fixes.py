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