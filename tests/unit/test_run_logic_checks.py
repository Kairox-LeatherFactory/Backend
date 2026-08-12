"""
Standalone verification harness (no pytest needed) for the barcode feature's
PURE logic: stage ordering, the four gates' decision predicates, stage inference,
and the merge-gate rule. Runs in this sandbox to PROVE the logic before the full
pytest suite runs in the repo.

These mirror the exact predicates in ProductionService, lifted so they can run
without the DB. The repo test suite (tests/) exercises the same logic THROUGH the
service against a real SQLite session.
"""
from app.core.enums import (
    Designation, MERGE_GATE_ENTRY, MULTI_STAGE_DESIGNATIONS, ProductionStage as PS,
    SCREEN_EXPECTED_ROLE, SCREEN_TO_STAGE, STAGE_DESIGNATIONS, STAGE_ROLE_ACCESS,
    ScreenContext, UserRole, LINING_MANAGER,
)

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else " FAIL ") + name)


# ── mirror of ProductionService._skill_ok ────────────────────────────────────
def skill_ok(designation, stage):
    if stage is None:
        return True
    d = Designation.normalise(designation)
    if not d or d in MULTI_STAGE_DESIGNATIONS:
        return True
    if d not in {v for s in STAGE_DESIGNATIONS.values() for v in s}:
        return True  # uncatalogued → fail-open
    return d in STAGE_DESIGNATIONS.get(stage, set())


# ── mirror of ProductionService._infer_stage_for_piece (PIPELINE branch) ─────
def infer_stage(done_codes: set, screen: ScreenContext):
    if screen in SCREEN_TO_STAGE:
        return SCREEN_TO_STAGE[screen]
    chain = PS.leather_chain()
    furthest = -1
    for i, st in enumerate(chain):
        if st.value in done_codes:
            furthest = i
    nxt = furthest + 1
    return chain[nxt] if 0 <= nxt < len(chain) else None


# ── mirror of role gate ───────────────────────────────────────────────────────
def role_ok(role, stage):
    # Mirrors production.service._STAGE_BYPASS_ROLES — which includes HR. This
    # mirror listed only MD/DM, so every HR assertion here was checking the wrong
    # rule. Keep the two in step; the service is the source of truth.
    if role in (UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR):
        return True
    return role in STAGE_ROLE_ACCESS.get(stage, set())


print("\n=== STAGE ORDER & PREDECESSOR ===")
check("fusing predecessor = leather_cutting", PS.FUSING.predecessor() is PS.LEATHER_CUTTING)
check("pasting predecessor = fusing", PS.PASTING.predecessor() is PS.FUSING)
check("leather_cutting has no predecessor", PS.LEATHER_CUTTING.predecessor() is None)
check("lining_cutting has no predecessor", PS.LINING_CUTTING.predecessor() is None)
check("line_stitching predecessor is None (merge-governed)", PS.LINE_STITCHING.predecessor() is None)
check("shell_stitching predecessor = line_stitching", PS.SHELL_STITCHING.predecessor() is PS.LINE_STITCHING)
check("final_inspection predecessor = final_finish", PS.FINAL_INSPECTION.predecessor() is PS.FINAL_FINISH)
check("merge gate entry is line_stitching", MERGE_GATE_ENTRY is PS.LINE_STITCHING)

print("\n=== CUT ENTRY / CONSUMPTION FLAGS ===")
check("leather_cutting is cut entry", PS.LEATHER_CUTTING.is_cut_entry)
check("lining_cutting is cut entry", PS.LINING_CUTTING.is_cut_entry)
check("pasting is NOT cut entry", not PS.PASTING.is_cut_entry)
check("leather_cutting requires consumption", PS.LEATHER_CUTTING.requires_consumption)
check("fusing does NOT require consumption", not PS.FUSING.requires_consumption)

print("\n=== ROLE GATE ===")
check("cutting mgr may log leather cut", role_ok(UserRole.CUTTING_MANAGER, PS.LEATHER_CUTTING))
check("lining mgr may log lining cut", role_ok(LINING_MANAGER, PS.LINING_CUTTING))
check("cutting mgr may NOT log lining cut", not role_ok(UserRole.CUTTING_MANAGER, PS.LINING_CUTTING))
check("stitching mgr may log pasting", role_ok(UserRole.STITCHING_MANAGER, PS.PASTING))
check("cutting mgr may NOT log pasting", not role_ok(UserRole.CUTTING_MANAGER, PS.PASTING))
check("MD may log final finish (bypass)", role_ok(UserRole.MANAGING_DIRECTOR, PS.FINAL_FINISH))
check("DM may log lining cut (bypass)", role_ok(UserRole.DIRECT_MANAGER, PS.LINING_CUTTING))
check("stitching mgr may log fusing", role_ok(UserRole.STITCHING_MANAGER, PS.FUSING))
check("cutting mgr may NOT log fusing", not role_ok(UserRole.CUTTING_MANAGER, PS.FUSING))
check("stitching mgr may log final finish", role_ok(UserRole.STITCHING_MANAGER, PS.FINAL_FINISH))
# Inspection + export are APPROVALS: no floor manager owns them, only the bypass.
check("stitching mgr may NOT log final inspection", not role_ok(UserRole.STITCHING_MANAGER, PS.FINAL_INSPECTION))
check("stitching mgr may NOT log package export", not role_ok(UserRole.STITCHING_MANAGER, PS.PACKAGE_EXPORT))
check("HR may log package export (bypass)", role_ok(UserRole.HR, PS.PACKAGE_EXPORT))

print("\n=== SKILL GATE ===")
check("CUTTER may cut leather", skill_ok("CUTTER", PS.LEATHER_CUTTING))
check("CUTTER may NOT paste", not skill_ok("CUTTER", PS.PASTING))
check("PASTER may paste", skill_ok("PASTER", PS.PASTING))
check("shell tailor (messy case) may shell-stitch", skill_ok("  shell tailor ", PS.SHELL_STITCHING))
check("TAILOR (multi) may shell-stitch", skill_ok("TAILOR", PS.SHELL_STITCHING))
check("HELPER (multi-stage) may do anything", skill_ok("HELPER", PS.PASTING))
check("None designation passes (fail-open)", skill_ok(None, PS.PASTING))
check("uncatalogued designation passes (fail-open)", skill_ok("WELDER", PS.PASTING))
check("INSPECTOR may final-inspect", skill_ok("INSPECTOR", PS.FINAL_INSPECTION))
check("PASTER may NOT cut", not skill_ok("PASTER", PS.LEATHER_CUTTING))

print("\n=== STAGE INFERENCE (PIPELINE) ===")
check("no history → leather_cutting", infer_stage(set(), ScreenContext.PIPELINE) is PS.LEATHER_CUTTING)
check("after leather cut → fusing", infer_stage({"LEATHER_CUTTING"}, ScreenContext.PIPELINE) is PS.FUSING)
check("after fusing → pasting", infer_stage({"LEATHER_CUTTING", "FUSING"}, ScreenContext.PIPELINE) is PS.PASTING)
check("after pasting → line_stitching",
      infer_stage({"LEATHER_CUTTING", "FUSING", "PASTING"}, ScreenContext.PIPELINE) is PS.LINE_STITCHING)
check("after final_inspection → package_export",
      infer_stage({s.value for s in PS.leather_chain()[:-1]}, ScreenContext.PIPELINE) is PS.PACKAGE_EXPORT)
check("fully done → None (nothing next)",
      infer_stage({s.value for s in PS.leather_chain()}, ScreenContext.PIPELINE) is None)

print("\n=== SCREEN CONTEXT FIXES CUT STAGE ===")
check("LEATHER_CUT screen → leather_cutting", infer_stage(set(), ScreenContext.LEATHER_CUT) is PS.LEATHER_CUTTING)
check("LINING_CUT screen → lining_cutting", infer_stage(set(), ScreenContext.LINING_CUT) is PS.LINING_CUTTING)
check("cutting mgr expected on LEATHER_CUT screen",
      UserRole.CUTTING_MANAGER in SCREEN_EXPECTED_ROLE[ScreenContext.LEATHER_CUT])
check("lining mgr expected on LINING_CUT screen",
      LINING_MANAGER in SCREEN_EXPECTED_ROLE[ScreenContext.LINING_CUT])

print("\n=== SEQUENCE GATE (piece history) ===")
# sequence_ok = piece has predecessor's event
def sequence_ok(done_codes, stage):
    prev = stage.predecessor()
    if prev is None:
        return True
    return prev.value in done_codes
check("pasting blocked without fusing", not sequence_ok({"LEATHER_CUTTING"}, PS.PASTING))
check("pasting allowed with fusing", sequence_ok({"LEATHER_CUTTING", "FUSING"}, PS.PASTING))
check("leather cut always allowed (no predecessor)", sequence_ok(set(), PS.LEATHER_CUTTING))
check("line_stitching sequence-gate is a pass (merge governs it)", sequence_ok(set(), PS.LINE_STITCHING))

print("\n=== MERGE GATE (completeness) ===")
# merge rule: leather_in AND (lining_in OR not needs_lining) → complete
def merge_complete(leather_in, lining_in, needs_lining):
    return leather_in and (lining_in or not needs_lining)
check("leather-only complete on leather alone", merge_complete(True, False, False))
check("lining-needed NOT complete on leather alone", not merge_complete(True, False, True))
check("lining-needed complete with both", merge_complete(True, True, True))
check("not complete with neither", not merge_complete(False, False, True))

print("\n" + "=" * 60)
print(f"PASSED {len(PASS)}   FAILED {len(FAIL)}")
if FAIL:
    print("FAILURES:", FAIL)
    raise SystemExit(1)
print("ALL PURE-LOGIC CHECKS PASSED")
