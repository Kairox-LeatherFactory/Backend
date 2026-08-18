"""
UNIT · every foreign key must declare the right delete rule, and the migration
       must agree with the models about which ones they are.

WHY THIS FILE EXISTS
    Production hit this, twice, from both ends of the same relationship:

        Unable to delete rows as one of them is currently referenced by a
        foreign key constraint from the table `piece`.
          DETAIL: Key (id)=(…) is still referenced from table piece.

        Unable to delete rows as one of them is currently referenced by a
        foreign key constraint from the table `drawer`.
          DETAIL: Key (id)=(…) is still referenced from table drawer.

    `piece.drawer_id → drawer.id` and `drawer.current_piece_id → piece.id` are a
    mutual cycle: with no delete rule the two rows are mutually undeletable and
    NO ordering of the DELETEs resolves it. That is what makes a cyclic FK
    different in kind from an ordinary one — an ordinary blocked delete is
    "delete the children first", a cyclic one has no first.

    The fix generalised: a NULLABLE foreign key is a column whose schema already
    says the link is optional, so it can always afford SET NULL, and pinning the
    parent row forever buys nothing.

THE THREE INVARIANTS ASSERTED HERE
    1. Every NULLABLE foreign key declares an `ondelete`.
    2. No NOT NULL foreign key declares SET NULL — Postgres rejects it, and a
       row that cannot exist without its parent must not be orphaned anyway.
    3. The migration's `_NULLABLE_FKS` list matches the models exactly, in both
       directions. A model that grows a nullable FK without a matching migration
       entry ships a database whose delete rules silently differ from the code's
       — the schema-drift trap, caught here instead of in the Supabase console.

WHAT IS DELIBERATELY NOT ASSERTED
    That the FK graph is acyclic. Two of the cycles are legitimate modelling: the
    piece↔drawer link is genuinely read from both ends (the drawer's pointer is
    the live claim, the piece's is its assignment) and the document/submission/
    client_order triangle records provenance in both directions. The rule is "a
    cycle must be deletable", not "no cycles".

NO DB. Pure metadata inspection, so it belongs in the unit layer.
"""
import importlib.util
import itertools
import pathlib
import sys

import pytest

from app.core.database import Base

# Import every model module so Base.metadata is complete. Same list as
# tests/conftest.py — an omission here would hide a foreign key rather than fail.
import app.core.models                      # noqa: F401
import app.modules.clients.models           # noqa: F401
import app.modules.employees.models         # noqa: F401
import app.modules.users.models             # noqa: F401
import app.modules.production.models        # noqa: F401
import app.modules.attendance.models        # noqa: F401
import app.modules.wages.models             # noqa: F401
import app.modules.barcode.models           # noqa: F401
import app.modules.bom.models               # noqa: F401
import app.modules.inventory.models         # noqa: F401
import app.modules.procurement.models       # noqa: F401
import app.modules.supplier_po.models       # noqa: F401

_MIGRATION = (pathlib.Path(__file__).resolve().parents[2]
              / "alembic" / "versions" / "20260818_fk_setnull_all.py")


def _all_fks():
    """(table, column, referred_table, nullable, ondelete) for every FK."""
    out = []
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            out.append((table.name, fk.parent.name, fk.column.table.name,
                        fk.parent.nullable, fk.ondelete))
    return sorted(out)


_NULLABLE = [f for f in _all_fks() if f[3]]
_NOT_NULL = [f for f in _all_fks() if not f[3]]


def _load_migration():
    spec = importlib.util.spec_from_file_location("_fk_setnull", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── invariant 1 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "table,column,referred,ondelete",
    [(t, c, r, o) for t, c, r, _n, o in _NULLABLE],
    ids=[f"{t}.{c}" for t, c, _r, _n, _o in _NULLABLE],
)
def test_every_nullable_fk_declares_an_ondelete_rule(table, column, referred, ondelete):
    assert ondelete, (
        f"{table}.{column} → {referred}.id is nullable but declares no ondelete, "
        f"so the parent row can never be deleted while this child exists. Add "
        f"ondelete='SET NULL' to the ForeignKey and add the constraint to "
        f"_NULLABLE_FKS in alembic/versions/20260818_fk_setnull_all.py."
    )


# ── invariant 2 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "table,column,referred,ondelete",
    [(t, c, r, o) for t, c, r, _n, o in _NOT_NULL],
    ids=[f"{t}.{c}" for t, c, _r, _n, _o in _NOT_NULL],
)
def test_no_not_null_fk_declares_set_null(table, column, referred, ondelete):
    assert ondelete != "SET NULL", (
        f"{table}.{column} → {referred}.id is NOT NULL, so ON DELETE SET NULL is "
        f"illegal — Postgres rejects the constraint. Use CASCADE if the child is "
        f"genuinely owned by the parent, or leave it at the default RESTRICT so "
        f"the delete is refused until the children are dealt with deliberately."
    )


# ── invariant 3 ──────────────────────────────────────────────────────────────

def test_the_migration_lists_exactly_the_nullable_fks_the_models_declare():
    listed = {(t, c, r) for t, c, r, _name in _load_migration()._NULLABLE_FKS}
    declared = {(t, c, r) for t, c, r, _n, _o in _NULLABLE}

    missing = declared - listed
    extra = listed - declared
    assert not missing, (
        "these nullable FKs exist in the models but are not in the migration's "
        f"_NULLABLE_FKS, so the live database will not get their delete rule: {sorted(missing)}"
    )
    assert not extra, (
        "these FKs are in the migration's _NULLABLE_FKS but no longer exist as "
        f"nullable FKs in the models: {sorted(extra)}"
    )


# ── the cycles specifically: the bug that started all of this ────────────────

def _sccs(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan. Recursive is fine at this table count; the limit is raised so a
    deeper schema does not fail spuriously."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    counter = itertools.count()
    out: list[list[str]] = []

    def strong(v: str) -> None:
        index[v] = low[v] = next(counter)
        stack.append(v)
        on_stack[v] = True
        for w in graph.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif on_stack.get(w):
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                comp.append(w)
                if w == v:
                    break
            out.append(comp)

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 10_000))
    try:
        for v in list(graph):
            if v not in index:
                strong(v)
    finally:
        sys.setrecursionlimit(old)
    return out


def _cyclic_edges() -> set[tuple[str, str, str]]:
    """FKs whose two endpoints are in the SAME strongly connected component.

    Membership of *a* cycle is not enough. `style` is cyclic via its own
    `base_style_id` self-loop and `client_order` is cyclic via the document
    triangle, but `style.client_order_id → client_order` joins two DIFFERENT
    components and is an ordinary, resolvable FK — delete the styles, then the
    order. Only same-component edges are the undeletable kind.
    """
    graph: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            graph.setdefault(table.name, set()).add(fk.column.table.name)

    component_of: dict[str, int] = {}
    for i, comp in enumerate(_sccs(graph)):
        if len(comp) > 1 or comp[0] in graph.get(comp[0], ()):
            for name in comp:
                component_of[name] = i

    return {
        (t, c, r) for t, c, r, _n, _o in _all_fks()
        if component_of.get(t) is not None and component_of.get(t) == component_of.get(r)
    }


def test_the_schema_still_has_the_cycles_this_file_was_written_for():
    """A guard on the guard: if this drops to nothing, the test below is vacuous."""
    found = {(t, c) for t, c, _r in _cyclic_edges()}
    assert ("piece", "drawer_id") in found
    assert ("drawer", "current_piece_id") in found
    assert ("document", "submission_id") in found
    assert ("style", "base_style_id") in found


def test_every_cyclic_fk_is_nullable_so_set_null_is_available_to_it():
    """The reason the SET NULL rule is sufficient for the cycles.

    If a cyclic FK were ever NOT NULL, SET NULL would be illegal on it and the
    cycle would become genuinely undeletable — the only remaining exits being a
    DEFERRABLE constraint or a manual UPDATE. Nothing in the schema is in that
    position today, and this test is what says so out loud.
    """
    cyclic = _cyclic_edges()
    offenders = [(t, c, r) for t, c, r, n, _o in _all_fks()
                 if (t, c, r) in cyclic and not n]
    assert not offenders, (
        f"NOT NULL foreign keys inside a cycle, which SET NULL cannot rescue: {offenders}"
    )
