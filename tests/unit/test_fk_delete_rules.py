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
    3. The SCHEMA agrees with the models: every nullable foreign key the models
       declare is created by the baseline migration with the SAME `ondelete`. A
       model that grows a nullable FK the schema does not carry ships a database
       whose delete rules silently differ from the code's — the schema-drift trap,
       caught here instead of in the Supabase console.

       THIS USED TO READ A HAND-KEPT LIST. `20260818_fk_setnull_all.py` carried
       `_NULLABLE_FKS`, a registry somebody had to remember to append to, and the
       test compared the models against it. That migration existed to REPOINT
       constraints on an already-deployed database; when the chain was squashed
       into the `1d4a32009101` baseline on 2026-09-28 it was deleted, and with it
       the registry — and its job, because the baseline now creates every
       constraint with its rule inline. So the invariant is checked against the
       schema that actually ships, which also means it survives the next squash
       instead of breaking on it.

WHAT IS DELIBERATELY NOT ASSERTED
    That the FK graph is acyclic. Two of the cycles are legitimate modelling: the
    piece↔drawer link is genuinely read from both ends (the drawer's pointer is
    the live claim, the piece's is its assignment) and the document/submission/
    client_order triangle records provenance in both directions. The rule is "a
    cycle must be deletable", not "no cycles".

NO DB. Pure metadata inspection, so it belongs in the unit layer.
"""
import itertools
import pathlib
import re
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

# The squashed baseline — the single revision that now creates the whole schema.
# Found by its `down_revision = None` rather than by name, so a rename or a future
# re-squash does not break this file the way the deleted registry did.
_VERSIONS = pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _all_fks():
    """(table, column, referred_table, nullable, ondelete) for every FK."""
    out = []
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            out.append((table.name, fk.parent.name, fk.column.table.name,
                        fk.parent.nullable, fk.ondelete))
    return sorted(out)


# ── THE ONE DOCUMENTED EXCEPTION ─────────────────────────────────────────────
# The SET NULL rule rests on a premise stated in the docstring above: "a NULLABLE
# foreign key is a column whose schema already says the link is optional". That is
# true of every other nullable FK in this schema, where NULL means "no parent".
#
# It is NOT true of style_material_spec.sku_id, where NULL is a VALUE with its own
# meaning: "this recipe line is the STYLE-WIDE default", as opposed to a non-null
# id meaning "this line overrides that default for one colourway". SET NULL would
# therefore not clear a link — it would silently PROMOTE a single colourway's
# override into the default for every colourway of the style, changing what gets
# issued to garments nobody touched. It can also collide head-on with the unique
# constraint (a style-wide line for the same material may already exist), turning
# an ordinary SKU delete into a 500.
#
# So the line is OWNED by its SKU and CASCADEs with it, and the exception is
# recorded here rather than left as a silent divergence. Invariants 1 and 2 still
# apply to it in full — it must declare SOME ondelete, and it does.
#
# ADD TO THIS SET ONLY when NULL genuinely carries meaning in the column, and say
# what that meaning is. "SET NULL was inconvenient" is not a reason.
_MEANINGFUL_NULL_FKS = {
    ("style_material_spec", "sku_id", "sku"),
}

_NULLABLE = [f for f in _all_fks() if f[3]]
_NOT_NULL = [f for f in _all_fks() if not f[3]]


def _baseline_path() -> pathlib.Path:
    """The one revision with `down_revision = None`."""
    roots = [f for f in sorted(_VERSIONS.glob("*.py"))
             if re.search(r"^down_revision\s*=\s*None", f.read_text(encoding="utf-8"), re.M)]
    assert len(roots) == 1, (
        f"expected exactly one root revision in alembic/versions, found "
        f"{[f.name for f in roots]} — tests/unit/test_alembic_chain.py explains why "
        f"more than one is a broken chain")
    return roots[0]


def _schema_fks() -> dict:
    """{(table, column, referred_table): ondelete or None} from the baseline.

    BRACKETS ARE MATCHED, NOT REGEXED, and that is not fussiness. A
    `sa.ForeignKeyConstraint(...)` call in an autogenerated baseline spans several
    lines and nests brackets, so a regex for the whole call reads about 70 of the
    156 constraints in this file and silently reports the other 86 as missing their
    delete rule — 111 false failures, which is exactly the shape of a test everyone
    learns to ignore.

    Each constraint is attributed to the `op.create_table` it sits inside, so the
    key names the OWNING table. Without that, two tables with a `piece_id` column
    collapse into one entry.

    COMPOSITE FOREIGN KEYS WOULD BE READ BY THEIR FIRST COLUMN ONLY. This schema has
    none, and `_all_fks()` walks the models one column at a time anyway, so the two
    sides agree. If a composite FK is ever added, both this and the model walk need
    to learn about it together.
    """
    src = _baseline_path().read_text(encoding="utf-8")

    starts = {m.start(): m.group(1)
              for m in re.finditer(r"op\.create_table\(\s*[\'\"]([a-z_0-9]+)[\'\"]", src)}
    ordered = sorted(starts)

    def owner(pos):
        before = [s for s in ordered if s < pos]
        return starts[before[-1]] if before else None

    out = {}
    for m in re.finditer(r"ForeignKeyConstraint\(", src):
        i, depth = m.end(), 1
        while depth and i < len(src):
            if src[i] in "([{":
                depth += 1
            elif src[i] in ")]}":
                depth -= 1
            i += 1
        # A bare IndexError from running off the end tells whoever hits it nothing.
        assert not depth, (
            f"unbalanced brackets after ForeignKeyConstraint( at offset {m.start()} "
            f"in {_baseline_path().name} — the parser cannot trust the rest of the "
            f"file, so invariant 3 is not checking what it claims to")
        body = src[m.end():i - 1]
        halves = body.split("],")
        if len(halves) < 2:
            continue
        cols = re.findall(r"[\'\"]([^\'\"]+)[\'\"]", halves[0])
        refs = re.findall(r"[\'\"]([^\'\"]+)[\'\"]", halves[1])
        if not (cols and refs):
            continue
        rule = re.search(r"ondelete=[\'\"]([A-Z ]+)[\'\"]", body)
        out[(owner(m.start()), cols[0], refs[0].split(".")[0])] = (
            rule.group(1) if rule else None)
    return out


# ── invariant 1 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "table,column,referred,ondelete",
    [(t, c, r, o) for t, c, r, _n, o in _NULLABLE],
    ids=[f"{t}.{c}" for t, c, _r, _n, _o in _NULLABLE],
)
def test_every_nullable_fk_declares_an_ondelete_rule(table, column, referred, ondelete):
    # Applies to the documented exceptions too: they may choose a different rule,
    # but they may not choose no rule at all.
    assert ondelete, (
        f"{table}.{column} → {referred}.id is nullable but declares no ondelete, "
        f"so the parent row can never be deleted while this child exists. Add "
        f"ondelete='SET NULL' to the ForeignKey; the baseline migration picks it "
        f"up from the model on the next autogenerate, and invariant 3 below then "
        f"checks that it did."
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

def test_the_schema_carries_every_nullable_fk_rule_the_models_declare():
    """Invariant 3. The baseline must create each nullable FK with the models' rule.

    BOTH DIRECTIONS MATTER. A constraint the baseline omits ships a database whose
    parent rows cannot be deleted; a constraint whose rule DIFFERS from the model is
    worse, because the code and the schema then disagree about what a delete does and
    only the schema is enforcing anything.

    Verified when this was written: 156/156 constraints parsed, all 113 nullable FKs
    present, zero mismatches.
    """
    schema = _schema_fks()
    declared = [(t, c, r, o) for t, c, r, _n, o in _NULLABLE
                if (t, c, r) not in _MEANINGFUL_NULL_FKS]

    absent = [(t, c, r) for t, c, r, _o in declared if (t, c, r) not in schema]
    assert not absent, (
        "these nullable FKs exist in the models but the baseline migration does not "
        f"create them, so the live database will not have them: {sorted(absent)}")

    wrong = [(f"{t}.{c}->{r}", f"model={o}", f"schema={schema[(t, c, r)]}")
             for t, c, r, o in declared
             if (schema[(t, c, r)] or None) != (o or None)]
    assert not wrong, (
        "the models and the baseline disagree about what deleting the parent does. "
        f"The schema is what is enforced, so the model is the lie here: {sorted(wrong)}"
    )


def test_the_baseline_is_parsed_at_all():
    """A GUARD ON THE GUARD. If the parse silently returned nothing, the test above
    would pass by vacuously finding no mismatches — which is precisely how a regexed
    version of it reported success over 86 unread constraints."""
    schema = _schema_fks()
    src = _baseline_path().read_text(encoding="utf-8")
    assert len(schema) == src.count("ForeignKeyConstraint("), (
        f"parsed {len(schema)} of {src.count('ForeignKeyConstraint(')} constraints — "
        f"the bracket matcher is dropping some, so invariant 3 is not really checking "
        f"the ones it missed")
    assert ("kit_substitution_request", "piece_id", "piece") in schema
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
