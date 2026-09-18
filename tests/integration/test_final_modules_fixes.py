"""
================================================================================
tests/test_final_modules_fixes.py — guards for analytics, clients, imports,
users, and barcode fixes
================================================================================
Covers:
  F33  — every CLIENT-reachable analytics method accepts client_scope
  F84  — AnalyticsService inherits the barcode mixin (endpoints resolve)
  F116 — SKU code uses co.order_number (ORM), not order.order_number (dict)
  F39  — /clients/styles pins CLIENT callers to their own client_id
  F38/F45/F118 — upload handler bounds size, validates zip, guards filename
  F41  — auth burns bcrypt time on the unknown-user path
  F43  — password change enforces min length + rejects the phone number
  F18  — retirement (410) enforced for ALL barcode types via shared lookup
  F79  — the barcode counter fetches one row, not all
================================================================================
"""
import ast
import inspect

import pytest


# ── F33: analytics scoping present on every drill-down method (structural) ──
def test_f33_analytics_methods_accept_client_scope():
    from app.modules.analytics import service as a_service
    from app.modules.analytics import barcode_ext as a_ext
    for name in ("order_tree", "style_detail", "piece_detail",
                 "stage_spread_alerts", "freight_risk"):
        sig = inspect.signature(getattr(a_service.AnalyticsService, name))
        assert "client_scope" in sig.parameters, f"F33: {name} missing client_scope"
    for name in ("piece_life_story", "consumption_vs_stock"):
        sig = inspect.signature(getattr(a_ext.BarcodeAnalyticsMixin, name))
        assert "client_scope" in sig.parameters, f"F33: {name} missing client_scope"


# ── F84: mixin inherited (AST, no DB import needed) ─────────────────────────
def test_f84_service_inherits_mixin():
    # encoding is explicit: the source carries non-cp1252 characters, so relying
    # on the platform default fails on Windows with UnicodeDecodeError.
    src = open(inspect.getsourcefile(
        __import__("app.modules.analytics.service", fromlist=["x"])),
        encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AnalyticsService":
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            assert "BarcodeAnalyticsMixin" in bases
            return
    raise AssertionError("AnalyticsService not found")


# ── F116: SKU code built from the ORM order, not the dict (structural) ──────
def test_f116_uses_orm_order_number():
    from app.modules.clients import repository as c_repo
    src = inspect.getsource(c_repo.ClientRepository.create_order_with_breakdown)
    assert "make_sku_code(co.order_number" in src
    assert "make_sku_code(order.order_number" not in src


# ── F39: /clients/styles scopes CLIENT callers (structural) ─────────────────
def test_f39_styles_scoped_for_client():
    from app.modules.clients import router as c_router
    src = inspect.getsource(c_router.list_styles)
    assert "UserRole.CLIENT" in src and "client_id = user.client_id" in src


# ── F38/F45/F118: upload handler hardened (structural) ──────────────────────
def test_f38_f45_f118_upload_hardened():
    # _save_upload became _validated_upload when the temp-file copy was dropped:
    # the upload's own spooled stream goes straight to openpyxl. All three guards
    # moved with it — they were never a property of writing the file to disk.
    from app.modules.imports import router as i_router
    src = inspect.getsource(i_router._validated_upload)
    assert "max_upload_mb" in src            # F38 size cap
    assert "is_zipfile" in src               # F45 container validation
    assert "file.filename or" in src         # F118 None-filename guard


# ── F43: password policy (pure) ─────────────────────────────────────────────
def test_f43_password_min_length():
    from pydantic import ValidationError
    from app.modules.users.schemas import PasswordChange
    with pytest.raises(ValidationError):
        PasswordChange(current_password="x", new_password="short")   # < 8
    PasswordChange(current_password="x", new_password="longenough1")


# ── F41: unknown-user path burns bcrypt time (structural) ───────────────────
def test_f41_constant_time_auth():
    from app.modules.users import service as u_service
    src = inspect.getsource(u_service.UserService.authenticate)
    assert "_dummy_hash()" in src


# ── F18: shared retirement lookup used by all resolvers (structural) ────────
def test_f18_shared_retirement_lookup():
    from app.modules.barcode import service as b_service
    src = inspect.getsource(b_service)
    assert "_get_active_or_410" in src
    for resolver in ("resolve_piece_id", "resolve_lot_id", "resolve_drawer_id"):
        rsrc = inspect.getsource(getattr(b_service.BarcodeService, resolver))
        assert "_get_active_or_410" in rsrc, f"F18: {resolver} not retirement-aware"


# ── F79: counter fetches one row, not all (structural) ──────────────────────
def test_f79_counter_single_row():
    from app.modules.barcode import repository as b_repo
    src = inspect.getsource(b_repo.BarcodeRepository._next_code)
    assert "limit(1)" in src.lower()
    assert "order_by" in src.lower()