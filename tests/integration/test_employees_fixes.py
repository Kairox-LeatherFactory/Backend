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
    
@pytest.mark.asyncio
async def test_patch_employee_reaches_update(client, dm_token):
    # create
    r = await client.post("/api/v1/employees",
        headers={"Authorization": f"Bearer {dm_token}"},
        json={"name": "TESTWORKER", "designation": "cutter",
              "wage_type": "piece_rate"})
    eid = r.json()["id"]
    # patch designation
    r2 = await client.patch(f"/api/v1/employees/{eid}",
        headers={"Authorization": f"Bearer {dm_token}"},
        json={"designation": "paster"})
    assert r2.status_code == 200
    assert r2.json()["designation"] == "PASTER"
 
@pytest.mark.asyncio
async def test_soft_delete_preserves_row(client, dm_token, db):
    r = await client.post("/api/v1/employees",
        headers={"Authorization": f"Bearer {dm_token}"},
        json={"name": "LEAVER", "designation": "cutter", "wage_type": "piece_rate"})
    eid = r.json()["id"]
    d = await client.delete(f"/api/v1/employees/{eid}",
        headers={"Authorization": f"Bearer {dm_token}"})
    assert d.status_code == 200 and d.json()["active"] is False
    # row still exists, just inactive
    from app.modules.employees.models import Employee
    import uuid as _u
    emp = await db.get(Employee, _u.UUID(eid))
    assert emp is not None and emp.is_active is False
 
@pytest.mark.asyncio
async def test_manager_created_via_employee_service_gets_login(client, md_token, db):
    r = await client.post("/api/v1/employees",
        headers={"Authorization": f"Bearer {md_token}"},
        json={"name": "CUTMGR", "designation": "cutter", "wage_type": "piece_rate",
              "role": "cutting_manager", "phone": "9990001111", "password": "temp1234"})
    assert r.status_code == 201
    assert r.json()["user_created"] is True
    # a login now exists for that phone with role cutting_manager
    from app.modules.users.models import User
    from sqlalchemy import select
    u = await db.scalar(select(User).where(User.phone == "9990001111"))
    assert u is not None and u.role.value == "cutting_manager"
    assert u.employee_id is not None   # linked to the employee row
 
@pytest.mark.asyncio
async def test_hr_cannot_create_direct_manager_here(client, hr_token):
    r = await client.post("/api/v1/employees",
        headers={"Authorization": f"Bearer {hr_token}"},
        json={"name": "SNEAKY", "wage_type": "piece_rate",
              "role": "direct_manager", "phone": "9", "password": "x"})
    assert r.status_code == 422   # validator blocks DM/MD before authority check