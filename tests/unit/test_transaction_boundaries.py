"""
================================================================================
tests/unit/test_transaction_boundaries.py — where a commit is allowed to live
================================================================================
CLAUDE.md s15: "Repository = all DB access. Service = business logic. Router =
HTTP only. Commits belong in the SERVICE (batch the whole scan into one
transaction), never scattered."

WHY THIS IS A TEST AND NOT A STYLE PREFERENCE

A commit inside a repository method is invisible at the call site. It reads like
a plain write, so the next person who composes two of those writes into one
business operation gets a partial commit and no warning: step one is durable,
step two raises, and the database keeps half the operation.

That is exactly what had happened in wages. `compute_run` ran create_run ->
add_lines -> persist_breakdown -> persist_unrated, and EVERY ONE of them
committed — five commits for one payroll run. A failure in the middle left a
real, committed, half-populated run behind, and the only way back was a
compensating delete that could itself fail. The repository now flushes and the
service commits once, so a failure just rolls back.

The two checks below are the invariant, not the style:

  1. No service operation may span more than one committing repository method.
     That is the partial-commit hazard, stated directly.
  2. The set of committing repository methods may not GROW. It is a ratchet, not
     a ban: the entries below are single-step operations where the commit is a
     layering smell rather than a live bug, and removing them is a wider refactor
     than the one that fixed the money path. Adding a NEW one is how the money-
     path bug would come back, so that is what this stops.

This is pure AST analysis — no database, no imports of the app.
================================================================================
"""
import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parents[2] / "app" / "modules"

# Repository methods that still commit, by module. Single-step operations: one
# service call, one write, nothing to be left half-done. SHRINK THIS LIST, never
# extend it — see the note above.
KNOWN_COMMITTING = {
    "attendance": {"add", "get_config", "save"},
    "clients": {"create_client", "create_client_with_order", "create_order",
                "create_order_with_breakdown", "delete_client", "update_client"},
    "employees": {"save"},
    "users": {"from_user_create", "save"},
    "production": {"add_event", "mint_pieces"},
}

# `commit()` itself is the SERVICE's handle on the transaction — that is the
# pattern we want, not a violation.
TRANSACTION_HANDLES = {"commit", "rollback", "flush"}

# Phase-2 modules (CLAUDE.md s12 and the "Out of scope" note in the audit). They
# were not reviewed and are not held to this invariant yet. Remove a name from
# here when that module gets the same pass wages did.
OUT_OF_SCOPE = {"bom", "procurement", "supplier_po", "inventory", "intelligence"}

# Routers that are RETIRED: not imported and not registered in main.py, so the
# code is unreachable. `/drawers` was replaced by `/store` when the store state
# moved onto the piece (migration 20260902_store_on_piece). Its commits are dead
# code rather than a live layering breach, and the file is kept only for
# reference. Deleting the file is the real fix; until then it is named here so
# the check stays honest instead of being weakened for everyone.
RETIRED_ROUTERS = {"drawers"}


def _committing_methods(path: pathlib.Path) -> set[str]:
    """Repository methods whose body calls .commit(), excluding the handle."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in TRANSACTION_HANDLES:
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "commit"):
                out.add(node.name)
    return out


def _modules():
    for repo in sorted(APP.glob("*/repository.py")):
        if repo.parent.name in OUT_OF_SCOPE:
            continue
        yield repo.parent.name, repo


@pytest.mark.integrity
@pytest.mark.parametrize("module,repo_path", list(_modules()),
                         ids=[m for m, _ in _modules()])
def test_no_new_committing_repository_methods(module, repo_path):
    """The ratchet: the committing set may shrink, never grow."""
    found = _committing_methods(repo_path)
    allowed = KNOWN_COMMITTING.get(module, set())
    new = found - allowed
    assert not new, (
        f"{module}/repository.py: {sorted(new)} commit inside the repository. "
        "Commits belong in the service (CLAUDE.md s15) — flush here and let the "
        "service own the transaction, so two of these writes can be composed "
        "into one operation without leaving half of it committed.")


@pytest.mark.integrity
@pytest.mark.parametrize("module,repo_path", list(_modules()),
                         ids=[m for m, _ in _modules()])
def test_no_service_operation_spans_two_commits(module, repo_path):
    """THE ACTUAL HAZARD: one business operation, two durable writes.

    If a service method calls two committing repository methods, the operation
    is not atomic and a failure between them is unrecoverable from inside the
    request.
    """
    service = repo_path.parent / "service.py"
    if not service.exists():
        pytest.skip(f"{module} has no service.py")
    committing = _committing_methods(repo_path)
    if not committing:
        pytest.skip(f"{module} repository commits nowhere")

    tree = ast.parse(service.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = [c.func.attr for c in ast.walk(node)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)]
        spanned = [c for c in called if c in committing]
        if len(spanned) > 1:
            offenders.append(f"{node.name} -> {spanned}")

    assert not offenders, (
        f"{module}/service.py: these operations span more than one committing "
        f"repository call, so a failure part-way through commits half the "
        f"operation: {offenders}. Make the repository methods flush and commit "
        f"once in the service.")


@pytest.mark.integrity
def test_routers_never_commit():
    """A router is HTTP only. It must not own a transaction."""
    offenders = []
    for router in sorted(APP.glob("*/*router*.py")):
        if router.parent.name in OUT_OF_SCOPE | RETIRED_ROUTERS:
            continue
        tree = ast.parse(router.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "commit"):
                offenders.append(f"{router.parent.name}/{router.name}:{node.lineno}")
    assert not offenders, (
        "commit() called from a router — routers are HTTP only (CLAUDE.md s15): "
        + ", ".join(offenders))
