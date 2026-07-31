"""
================================================================================
tests/test_core_blockers.py — regression guards for the CORE deploy blockers
================================================================================
These are the tests that must stay green so the F0x/F32/F58 class of blockers
cannot silently return. They are pure-logic (no DB), so they run in the fast
`unit` layer.

Covers:
  F01  — one ProductionStage vocabulary, LINING_MANAGER is a real UserRole
  F02  — the seed operation codes equal the runtime enum vocabulary
  F32  — a non-local boot refuses the insecure default secret_key
  F58  — the lining manager is granted access to its own stage
================================================================================
"""
import pytest


# ── F01: the enum vocabulary is unified and load-bearing ────────────────────
def test_f01_single_production_stage_vocabulary():
    from app.core.enums import ProductionStage
    values = {s.value for s in ProductionStage}
    # The canonical, post-merge 9-stage set — NOT the old shadowed 8-stage set.
    assert values == {
        "LEATHER_CUTTING", "LINING_CUTTING", "FUSING", "PASTING",
        "LINE_STITCHING", "SHELL_STITCHING", "FINAL_FINISH",
        "FINAL_INSPECTION", "PACKAGE_EXPORT",
    }
    # The old shadowed vocabulary is gone.
    assert not hasattr(ProductionStage, "CUTTING")
    assert not hasattr(ProductionStage, "LINE_ATTACH")


def test_f01_lining_manager_is_a_real_userrole_member():
    from app.core.enums import UserRole, LINING_MANAGER
    # LINING_MANAGER must resolve to the real enum member, not the _role() shim.
    assert LINING_MANAGER is UserRole.LINING_MANAGER
    assert UserRole.LINING_MANAGER.value == "lining_manager"


def test_f01_stage_role_access_keys_on_real_members():
    from app.core.enums import ProductionStage, STAGE_ROLE_ACCESS, UserRole
    # Every stage has an access entry, and the lining cut is owned by the lining mgr.
    for stage in ProductionStage:
        assert stage in STAGE_ROLE_ACCESS, f"{stage} missing from STAGE_ROLE_ACCESS"
    assert UserRole.LINING_MANAGER in STAGE_ROLE_ACCESS[ProductionStage.LINING_CUTTING]


def test_f01_leather_chain_ordering_and_predecessors():
    from app.core.enums import ProductionStage as PS
    chain = PS.leather_chain()
    assert chain[0] is PS.LEATHER_CUTTING
    assert chain[-1] is PS.PACKAGE_EXPORT
    # cut entries have no predecessor
    assert PS.LEATHER_CUTTING.predecessor() is None
    assert PS.LINING_CUTTING.predecessor() is None
    # LINE_STITCHING is gated by the merge, not by sequence
    assert PS.LINE_STITCHING.predecessor() is None
    # a linear-chain stage points back one step
    assert PS.FUSING.predecessor() is PS.LEATHER_CUTTING


# ── F02 / F58: seed vocabulary == runtime vocabulary, lining has access ──────
def test_f02_seed_operation_codes_cover_the_runtime_vocabulary():
    from scripts.seed import OPS
    from app.core.enums import ProductionStage
    seeded = {code for code, _label, _seq in OPS}
    required = {s.value for s in ProductionStage.leather_chain()} | {"LINING_CUTTING"}
    missing = required - seeded
    assert not missing, f"seed is missing operation codes the runtime needs: {missing}"


def test_f58_seed_grants_lining_manager_access():
    from scripts.seed import ACCESS
    assert "lining_manager" in ACCESS
    assert "LINING_CUTTING" in ACCESS["lining_manager"]


# ── F32: the insecure default secret key cannot boot outside local ──────────
def test_f32_default_secret_key_refused_outside_local():
    from app.core.config import Settings
    # local + default is allowed (dev convenience)
    Settings(environment="local")
    # production/staging + the shipped default must refuse to construct
    with pytest.raises(Exception):
        Settings(environment="production")
    with pytest.raises(Exception):
        Settings(environment="staging")


def test_f32_real_secret_key_boots_in_production():
    from app.core.config import Settings
    s = Settings(environment="production",
                 secret_key="a-real-random-production-secret-value-123456")
    assert not s.secret_key.startswith("dev-only")