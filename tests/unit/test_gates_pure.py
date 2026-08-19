"""
UNIT · the gate predicates that are genuinely pure.

Gate 2 (SKILL) is a `@staticmethod` taking plain values, so it is testable with
no session at all. Gates 1, 3 and 4 are not — they reach `self.repo`/`self.db`
and are covered in tests/integration. That asymmetry is itself an audit finding
(pass-08-service-layer.md): the four most safety-critical rules in the system
should all be as testable as this one.

The geofence used to be covered here too. LOCATION TRACKING IS REMOVED — its
implementation is commented out in `app/modules/attendance/geofence.py`, so its
tests are commented out with it at the bottom of this file.
"""
import pytest
from app.core.enums import UserRole, ScreenContext, screen_for_role
from app.core.enums_barcode import (
    MULTI_STAGE_DESIGNATIONS, STAGE_DESIGNATIONS, STAGE_ROLE_ACCESS, ProductionStage,
)
# LOCATION REMOVED — geofence.py no longer exports these.
# from app.modules.attendance.geofence import haversine_m, within_geofence
from app.modules.production.service import ProductionService

skill_ok = ProductionService._skill_ok


# ── GATE 2 · SKILL ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("designation,stage", [
    ("CUTTER", ProductionStage.LEATHER_CUTTING),
    ("CUTTER", ProductionStage.FUSING),          # CUTTER covers fusing too
    ("CUTTER", ProductionStage.LINING_CUTTING),
    ("LINING_CUTTER", ProductionStage.LINING_CUTTING),
    ("PASTER", ProductionStage.PASTING),
    ("TAILOR", ProductionStage.PASTING),
    ("TAILOR", ProductionStage.LINE_STITCHING),
    ("TAILOR", ProductionStage.SHELL_STITCHING),
    ("FINISHER", ProductionStage.FINAL_FINISH),
    ("INSPECTOR", ProductionStage.FINAL_INSPECTION),
    ("PACKER", ProductionStage.PACKAGE_EXPORT),
])
def test_catalogued_designation_may_work_its_own_stage(designation, stage):
    assert skill_ok(designation, stage) is True


@pytest.mark.parametrize("designation,stage", [
    ("CUTTER", ProductionStage.PASTING),
    ("CUTTER", ProductionStage.LINE_STITCHING),
    ("PASTER", ProductionStage.LEATHER_CUTTING),
    ("TAILOR", ProductionStage.LEATHER_CUTTING),
    ("PACKER", ProductionStage.FUSING),
    ("LINING_CUTTER", ProductionStage.SHELL_STITCHING),
])
def test_catalogued_designation_is_blocked_off_its_stage(designation, stage):
    assert skill_ok(designation, stage) is False


@pytest.mark.parametrize("designation", sorted(MULTI_STAGE_DESIGNATIONS))
@pytest.mark.parametrize("stage", list(ProductionStage))
def test_multi_stage_designations_work_every_stage(designation, stage):
    """HELPER / SUPERVISOR / OTHER are exempt by design (enums_barcode.py:255)."""
    assert skill_ok(designation, stage) is True


def test_designation_is_normalised_before_matching():
    """Designations are an UPPERCASE controlled vocabulary; the gate must not
    depend on how HR typed it."""
    for variant in ("cutter", "  Cutter  ", "CuTtEr"):
        assert skill_ok(variant, ProductionStage.LEATHER_CUTTING) is True
        assert skill_ok(variant, ProductionStage.PASTING) is False


@pytest.mark.parametrize("designation", [None, "", "   "])
def test_missing_designation_fails_open(designation):
    """Deliberate: HR backfills designations, so an unset one must not stop the
    floor (service.py:135)."""
    assert skill_ok(designation, ProductionStage.LEATHER_CUTTING) is True


def test_uncatalogued_designation_fails_open():
    """A designation nobody has catalogued yet is allowed anywhere
    (service.py:137-138). This is intentional but load-bearing — if it ever
    becomes fail-CLOSED, every un-backfilled worker stops working."""
    assert skill_ok("WELDER", ProductionStage.LINE_STITCHING) is True
    assert skill_ok("ZZZ-NOT-A-JOB", ProductionStage.PACKAGE_EXPORT) is True


def test_no_stage_is_permitted():
    """`stage is None` means the piece has nothing left to do; the skill gate
    must not be what reports that."""
    assert skill_ok("PACKER", None) is True


def test_every_stage_has_at_least_one_designation():
    """A stage with an empty designation set would be unworkable by anyone whose
    designation IS catalogued — a silent floor stoppage."""
    for stage in ProductionStage:
        assert STAGE_DESIGNATIONS.get(stage), f"{stage.value} has no designations"


# ── GATE 1 data · the role map is a table, so assert its shape ───────────────
def test_every_stage_appears_in_the_role_map():
    for stage in ProductionStage:
        assert stage in STAGE_ROLE_ACCESS, f"{stage.value} missing from STAGE_ROLE_ACCESS"


def test_approval_stages_are_reserved_to_the_bypass_roles():
    """FINAL_INSPECTION / PACKAGE_EXPORT carry an EMPTY role set — they are
    APPROVALS, not floor work, so only the DM/MD/HR bypass reaches them.

    FINAL_FINISH is deliberately NOT in this list: it is floor work and belongs
    to the stitching manager, like every other post-cut stage. This test used to
    assert FINAL_FINISH was empty too, which never matched the table."""
    for stage in (ProductionStage.FINAL_INSPECTION, ProductionStage.PACKAGE_EXPORT):
        assert STAGE_ROLE_ACCESS[stage] == set()
    assert STAGE_ROLE_ACCESS[ProductionStage.FINAL_FINISH] == {UserRole.STITCHING_MANAGER}


def test_every_post_cut_floor_stage_belongs_to_the_stitching_manager():
    """The stitching manager is the ONLY role on the PIPELINE screen, so it is the
    only role whose scan can infer a post-cut stage. Any post-cut floor stage
    granted to someone else is unreachable by definition."""
    for stage in (ProductionStage.FUSING, ProductionStage.PASTING,
                  ProductionStage.LINE_STITCHING, ProductionStage.SHELL_STITCHING,
                  ProductionStage.FINAL_FINISH):
        assert STAGE_ROLE_ACCESS[stage] == {UserRole.STITCHING_MANAGER}, stage.value


def test_the_two_cut_paths_belong_to_different_roles():
    """The parallel cut paths are the reason LINING_MANAGER exists."""
    leather = STAGE_ROLE_ACCESS[ProductionStage.LEATHER_CUTTING]
    lining = STAGE_ROLE_ACCESS[ProductionStage.LINING_CUTTING]
    assert leather and lining and leather.isdisjoint(lining)


# ── the stage chain ──────────────────────────────────────────────────────────
def test_leather_chain_is_ordered_and_complete():
    chain = ProductionStage.leather_chain()
    assert chain[0] is ProductionStage.LEATHER_CUTTING
    assert chain[-1] is ProductionStage.PACKAGE_EXPORT
    assert len(chain) == len(set(chain))
    # LINING_CUTTING is deliberately off-chain: it is a parallel path
    assert ProductionStage.LINING_CUTTING not in chain


def test_predecessor_walks_the_chain_backwards():
    chain = ProductionStage.leather_chain()
    for earlier, later in zip(chain, chain[1:]):
        if later is not ProductionStage.LINE_STITCHING:
            assert later.predecessor() is earlier


def test_chain_entries_and_the_merge_gate_have_no_predecessor():
    """Both cut entries start a path; LINE_STITCHING is governed by the merge
    gate rather than by sequence (enums_barcode.py:137-154)."""
    assert ProductionStage.LEATHER_CUTTING.predecessor() is None
    assert ProductionStage.LINING_CUTTING.predecessor() is None
    assert ProductionStage.LINE_STITCHING.predecessor() is None


def test_only_the_cut_stages_require_consumption():
    for stage in ProductionStage:
        expected = stage in (ProductionStage.LEATHER_CUTTING,
                             ProductionStage.LINING_CUTTING)
        assert stage.requires_consumption is expected


# ── geofence · LOCATION REMOVED ─────────────────────────────────────────────
# The whole block below tested app/modules/attendance/geofence.py, whose
# implementation is now commented out. Uncomment both together to restore the
# 100-metre rule and its coverage.
# # ── geofence ─────────────────────────────────────────────────────────────────
# FACTORY = (12.9716, 77.5946)     # Bengaluru
#
#
# def test_haversine_zero_distance():
#     assert haversine_m(*FACTORY, *FACTORY) == pytest.approx(0.0, abs=0.01)
#
#
# def test_haversine_is_symmetric():
#     a, b = FACTORY, (12.9750, 77.5990)
#     assert haversine_m(*a, *b) == pytest.approx(haversine_m(*b, *a), abs=0.01)
#
#
# def test_haversine_known_separation():
#     """~1 degree of latitude is ~111 km anywhere on the globe."""
#     d = haversine_m(0.0, 0.0, 1.0, 0.0)
#     assert 110_000 < d < 112_000
#
#
# def test_inside_the_fence_is_allowed():
#     ok, dist = within_geofence(FACTORY[0], FACTORY[1], *FACTORY, 100)
#     assert ok is True and dist == pytest.approx(0.0, abs=0.01)
#
#
# def test_outside_the_fence_is_refused():
#     ok, dist = within_geofence(12.9900, 77.6200, *FACTORY, 100)
#     assert ok is False and dist > 100
#
#
# def test_the_boundary_itself_is_inside():
#     """Spec rule is `distance <= radius` (geofence.py:28) — a worker standing
#     exactly on the line is at work."""
#     ok, dist = within_geofence(*FACTORY, *FACTORY, 0)
#     assert ok is True and dist == 0.0
#
#
# def test_null_island_default_rejects_a_real_worker():
#     """AUDIT (pass-12, top-10 #10): ShiftConfig.factory_lat/lon default to 0.0
#     (attendance/models.py:63-64) and the row auto-creates on first read. Until a
#     DM sets real coordinates, every genuine check-in is measured against 0N 0E
#     and refused — which blocks production logging factory-wide on a fresh deploy.
#
#     This test documents the arithmetic behind that finding.
#     """
#     ok, dist = within_geofence(FACTORY[0], FACTORY[1], 0.0, 0.0, 100)
#     assert ok is False
#     assert dist > 1_000_000        # ~1,900 km away
#


def test_cutting_manager_pinned_to_leather():
    assert screen_for_role(UserRole.CUTTING_MANAGER) is ScreenContext.LEATHER_CUT
 
def test_lining_manager_pinned_to_lining():
    assert screen_for_role(UserRole.LINING_MANAGER) is ScreenContext.LINING_CUT
 
def test_stitching_manager_is_pipeline():
    assert screen_for_role(UserRole.STITCHING_MANAGER) is ScreenContext.PIPELINE
 
def test_cutting_manager_override_is_ignored():
    # a pinned role cannot override to another screen
    assert screen_for_role(UserRole.CUTTING_MANAGER,
                           override=ScreenContext.LINING_CUT) is ScreenContext.LEATHER_CUT
 
def test_md_may_override():
    assert screen_for_role(UserRole.MANAGING_DIRECTOR,
                           override=ScreenContext.LEATHER_CUT) is ScreenContext.LEATHER_CUT
 
def test_md_defaults_to_pipeline():
    assert screen_for_role(UserRole.MANAGING_DIRECTOR) is ScreenContext.PIPELINE
 
@pytest.mark.asyncio
async def test_log_without_screen_context_uses_role(client, cutting_mgr_token):
    # a cutting manager logs WITHOUT sending screen_context → leather cutting
    ...  # POST /production/log with body omitting screen_context; expect 201
