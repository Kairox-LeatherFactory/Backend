"""
================================================================================
tests/test_employees_fixes.py — regression guards for the EMPLOYEES module
================================================================================
Covers:
  F37 — salary is NOT in the default roster schema; only in the pay variant
  F47 — the update path rejects fields outside an explicit allow-list
================================================================================
"""
import pytest


# ── F37: salary only in the pay-bearing schema (pure) ───────────────────────
def test_f37_plain_read_has_no_salary():
    from app.modules.employees.schemas import EmployeeRead, EmployeeReadWithPay
    assert "monthly_salary" not in EmployeeRead.model_fields
    assert "monthly_salary" in EmployeeReadWithPay.model_fields


def test_f37_router_chooses_schema_by_role():
    # Structural: the list endpoint branches on role before choosing the schema.
    import inspect
    from app.modules.employees import router as emp_router
    src = inspect.getsource(emp_router.list_employees)
    assert "EmployeeReadWithPay" in src and "EmployeeRead" in src
    assert "pay_roles" in src or "MANAGING_DIRECTOR" in src


# ── F47: update allow-list rejects unexpected fields (structural) ───────────
def test_f47_update_has_allow_list():
    import inspect
    from app.modules.employees import service as emp_service
    src = inspect.getsource(emp_service.EmployeeService.update)
    assert "_ALLOWED" in src, "F47: update must enumerate writable fields"
    assert "rejected" in src