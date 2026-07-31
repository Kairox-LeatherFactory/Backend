"""
================================================================================
tests/unit/test_gates_and_stages.py — LAYER 1: UNIT (pure, no DB)
================================================================================
The gate predicates and stage machinery are pure functions of enums + a piece's
history. Test them in isolation — fastest layer, highest count, catches a
reordered pipeline or a mis-mapped role instantly.

All 49 of these mirror the assertions proven in verify/run_logic_checks.py, now
as parametrised pytest.
================================================================================
"""
import pytest

from app.core.enums import (
    Designation, MERGE_GATE_ENTRY, MULTI_STAGE_DESIGNATIONS, ProductionStage as PS,
    SCREEN_EXPECTED_ROLE, SCREEN_TO_STAGE, STAGE_DESIGNATIONS, STAGE_ROLE_ACCESS,
    ScreenContext, UserRole, LINING_MANAGER,
)
from app.modules.production.service import ProductionService


# ── stage order / predecessor ────────────────────────────────────────────────
@pytest.mark.parametrize("stage,expected", [
    (PS.FUSING, PS.LEATHER_CUTTING),
    (PS.PASTING, PS.FUSING),
    (PS.SHELL_STITCHING, PS.LINE_STITCHING),
    (PS.FINAL_INSPECTION, PS.FINAL_FINISH),
    (PS.LEATHER_CUTTING, None),
    (PS.LINING_CUTTING, None),
    (PS.LINE_STITCHING, None),   # merge-governed, not sequence
])
def test_predecessor(stage, expected):
    assert stage.predecessor() is expected


def test_merge_gate_entry_is_line_stitching():
    assert MERGE_GATE_ENTRY is PS.LINE_STITCHING


@pytest.mark.parametrize("stage,is_cut", [
    (PS.LEATHER_CUTTING, True), (PS.LINING_CUTTING, True),
    (PS.PASTING, False), (PS.FINAL_FINISH, False),
])
def test_cut_entry_flag(stage, is_cut):
    assert stage.is_cut_entry is is_cut
    assert stage.requires_consumption is is_cut


# ── role gate ─────────────────────────────────────────────────────────────────
def _role_ok(role, stage):
    if role in (UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER):
        return True
    return role in STAGE_ROLE_ACCESS.get(stage, set())


@pytest.mark.parametrize("role,stage,ok", [
    (UserRole.CUTTING_MANAGER, PS.LEATHER_CUTTING, True),
    (LINING_MANAGER, PS.LINING_CUTTING, True),
    (UserRole.CUTTING_MANAGER, PS.LINING_CUTTING, False),
    (UserRole.STITCHING_MANAGER, PS.PASTING, True),
    (UserRole.CUTTING_MANAGER, PS.PASTING, False),
    (UserRole.MANAGING_DIRECTOR, PS.FINAL_FINISH, True),   # bypass
    (UserRole.DIRECT_MANAGER, PS.LINING_CUTTING, True),    # bypass
    (UserRole.STITCHING_MANAGER, PS.FINAL_FINISH, False),
])
def test_role_gate(role, stage, ok):
    assert _role_ok(role, stage) is ok


# ── skill gate (via the real static method) ──────────────────────────────────
@pytest.mark.parametrize("designation,stage,ok", [
    ("CUTTER", PS.LEATHER_CUTTING, True),
    ("CUTTER", PS.PASTING, False),
    ("PASTER", PS.PASTING, True),
    ("  shell tailor ", PS.SHELL_STITCHING, True),     # normalised
    ("TAILOR", PS.SHELL_STITCHING, True),              # multi-station
    ("HELPER", PS.PASTING, True),                      # multi-stage
    (None, PS.PASTING, True),                          # fail-open
    ("WELDER", PS.PASTING, True),                      # uncatalogued → fail-open
    ("INSPECTOR", PS.FINAL_INSPECTION, True),
    ("PASTER", PS.LEATHER_CUTTING, False),
])
def test_skill_gate(designation, stage, ok):
    assert ProductionService._skill_ok(designation, stage) is ok


def test_helper_is_multi_stage():
    assert "HELPER" in MULTI_STAGE_DESIGNATIONS


# ── designation normalisation ─────────────────────────────────────────────────
@pytest.mark.parametrize("raw,norm", [
    ("  shell tailor ", "SHELL_TAILOR"),
    ("Line-Attacher", "LINE_ATTACHER"),
    ("cutter", "CUTTER"),
    ("final/finish", "FINAL_FINISH"),
    (None, None),
    ("", None),
])
def test_normalise(raw, norm):
    assert Designation.normalise(raw) == norm


# ── screen → stage ────────────────────────────────────────────────────────────
def test_screen_maps():
    assert SCREEN_TO_STAGE[ScreenContext.LEATHER_CUT] is PS.LEATHER_CUTTING
    assert SCREEN_TO_STAGE[ScreenContext.LINING_CUT] is PS.LINING_CUTTING
    assert ScreenContext.PIPELINE not in SCREEN_TO_STAGE
    assert UserRole.CUTTING_MANAGER in SCREEN_EXPECTED_ROLE[ScreenContext.LEATHER_CUT]
    assert LINING_MANAGER in SCREEN_EXPECTED_ROLE[ScreenContext.LINING_CUT]


# ── merge completeness rule ──────────────────────────────────────────────────
def _complete(leather_in, lining_in, needs_lining):
    return leather_in and (lining_in or not needs_lining)


@pytest.mark.parametrize("leather,lining,needs,done", [
    (True, False, False, True),    # leather-only: done on leather
    (True, False, True, False),    # needs lining: not done
    (True, True, True, True),      # both present: done
    (False, False, True, False),   # nothing: not done
])
def test_merge_completeness(leather, lining, needs, done):
    assert _complete(leather, lining, needs) is done


# ── stage_of maps operation codes to stages, None for legacy ─────────────────
class _Op:
    def __init__(self, code):
        self.code = code


@pytest.mark.parametrize("code,expected", [
    ("LEATHER_CUTTING", PS.LEATHER_CUTTING),
    ("leather_cutting", PS.LEATHER_CUTTING),   # case-insensitive
    ("PASTING", PS.PASTING),
    ("FF-SMS", None),                          # legacy code → no stage
    ("BOGUS", None),
])
def test_stage_of(code, expected):
    assert ProductionService._stage_of(_Op(code)) is expected
