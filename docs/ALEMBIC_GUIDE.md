# Alembic, from scratch — how to run and maintain migrations in KairoX

Written for: whoever maintains this backend, assuming no prior Alembic experience.

Status: 46 revision files, one linear chain, single head `20260922_fresh_db_parity`.
No orphans, no branches. The chain is healthy — see §3 before deciding to squash it.

---

## 1. The mental model (read this once, everything else follows)

There are **three** things, and every Alembic problem is a disagreement between them.

| # | The thing | Where it lives | Who changes it |
|---|---|---|---|
| 1 | **The models** — what the schema *should* be | `app/modules/*/models.py` | you, when you write code |
| 2 | **The version files** — the recorded *steps* to get there | `alembic/versions/*.py` | `alembic revision` |
| 3 | **The `alembic_version` row** — which step a *particular database* has reached | one row, one column, inside each database | `alembic upgrade` / `alembic stamp` |

A database is not "up to date" because its tables look right. It is up to date because
**its `alembic_version` row names a revision, and that revision is the last file in the chain.**

The version files form a **linked list**, not a folder of loose scripts:

```
None <- 3e54e129d9aa <- ... <- 20260911_pom_status <- 20260917_... <- 20260922_fresh_db_parity (head)
        (baseline)                                                      ^
                                                                        "head" = the last one
```

Each file declares `revision = "<its own id>"` and `down_revision = "<the id before it>"`.
`alembic upgrade head` means: read my `alembic_version` row, walk forward through that list,
run each `upgrade()` I have not run, and bump the row after each one.

**This is why the number of files does not matter to Postgres.** A database that is already at
head runs *zero* of them on the next deploy. 46 files, 200 files — same speed, same result.

---

## 2. The only four commands you need day to day

Run these from the backend root. They read `DATABASE_URL` out of `.env`
(via `alembic/env.py`, which is why `alembic.ini`'s own `sqlalchemy.url` line is ignored).

```bash
alembic current     # which revision is THIS database on?  (asks the DB)
alembic heads       # which revision is the LAST file?     (reads the folder)
alembic history     # the whole chain, newest first
alembic upgrade head   # bring this database up to the last file
```

If `current` and `heads` print the same id, the database is up to date. That one comparison
is the whole health check.

### Making a schema change — the loop

```bash
# 1. edit the model
#    app/modules/production/models.py  ->  add a column, a table, an index

# 2. ask Alembic to write the step for you
alembic revision --autogenerate -m "piece adds rework_reason"

# 3. READ THE GENERATED FILE.  Always.  See section 5 for what it gets wrong.
#    alembic/versions/20260923_1410_piece_adds_rework_reason.py

# 4. apply it
alembic upgrade head
```

**Autogenerate does not invent the change — it diffs your models against the database you are
pointed at.** Same models + already-migrated database = an empty migration. That is the correct
answer, not a failure.

### One rule that prevents most disasters

> **Never edit a migration file that has already run on any database you do not control.**

Once staging or production has executed a file, that file is history. Fix a mistake with a *new*
revision on top. Editing the old one means your DB and your files silently disagree forever.

---

## 3. "I can't maintain 46 files" — the honest answer

**You do not maintain them. You maintain the models.**

Version files are an append-only ledger, like git commits. Nobody maintains commit #7 from June.
Django projects routinely carry 300+ migration files; Rails ships `db/migrate` folders of similar
size. It is normal, and it costs nothing at runtime.

What actually hurts, and what to do about it:

| The real pain | The real fix |
|---|---|
| "I can't tell what the schema looks like" | Read `models.py`. That is the schema. Never read migrations to learn the schema. |
| "A fresh DB takes 46 steps to build" | It takes ~10 seconds. Not a problem until it is. |
| "The folder is cluttered with junk" | Delete the non-migration junk — see §7. |
| "I genuinely want one clean starting point" | **Squash.** That is §4. Do it once per major release, not monthly. |

So: squashing is legitimate housekeeping, not an emergency. Do it deliberately, with the
procedure below — not by deleting files and hoping.

---

## 4. Squashing 46 files into one baseline

### 4a. First answer your own question

> *"I want to remove all the versions and autogenerate — for that I have to delete my database
> as well, right? New one needed, then revision --autogenerate, upgrade head, right?"*

**Only if no database anywhere holds data you care about.** Your `.env` currently points at a
live Supabase project. Deleting it deletes real orders, pieces, wage lines and barcodes.

The piece you are missing is one command: **`alembic stamp`**.

```
alembic upgrade head   ->  RUN the migrations, then write the version row
alembic stamp <rev>    ->  write the version row ONLY.  Touches no table.
```

`stamp` is what makes a squash safe. An existing database already *has* the schema the new
baseline describes — it does not need to build it again, it only needs to be told
"you are now at the new baseline". That is a one-row UPDATE.

So there are two procedures. Pick by whether any live data matters.

---

### 4b. Way A — the nuke (throwaway / local dev only)

Use when: the only database is your local scratch DB and you do not care about its rows.

```bash
# 1. safety net: the old chain stays recoverable from git forever
git checkout -b alembic-squash
git tag alembic-pre-squash-2026-09-23

# 2. remove the old chain
git rm alembic/versions/*.py
rm -rf alembic/versions/__pycache__

# 3. start from an EMPTY database (no tables, and NO alembic_version table)
#    local Postgres:
dropdb factory && createdb factory
#    ...and make sure .env's DATABASE_URL points at it

# 4. one file that builds everything
alembic revision --autogenerate -m "squashed baseline 2026-09"

# 5. read it, then apply it
alembic upgrade head
alembic current     # should print the new baseline id
```

**Why the database must be empty in step 3:** autogenerate writes the *difference* between your
models and the database. Against a database that already has the tables, the difference is
nothing and you get an empty file. Against an empty one, the difference is the entire schema —
which is exactly the baseline you want.

**Why `alembic_version` must also be gone:** if the row still says `20260922_fresh_db_parity`
and that file no longer exists, Alembic refuses to do anything — "Can't locate revision".

---

### 4c. Way B — the safe squash (use this on staging / production)

Use when: a database somewhere has data. This keeps every row and every table exactly as it is.

```bash
# ── STEP 0 ── Get every real database to the CURRENT head first.
#    The squash point must be a state they all share.
DATABASE_URL=<staging>     alembic current   # must equal `alembic heads`
DATABASE_URL=<production>  alembic current   # must equal `alembic heads`
#    Any that lag:  alembic upgrade head

# ── STEP 1 ── Snapshot the old chain (git is the archive; no folder copying needed)
git checkout -b alembic-squash
git tag alembic-pre-squash-2026-09-23
git rm alembic/versions/*.py

# ── STEP 2 ── Generate the baseline against a SCRATCH EMPTY database.
#    Never point this at staging. Use a throwaway local DB or a Supabase branch.
createdb kairox_squash
DATABASE_URL="postgresql+psycopg2://postgres:pass@localhost:5432/kairox_squash" \
  alembic revision --autogenerate -m "squashed baseline 2026-09"
#    -> alembic/versions/20260923_XXXX_squashed_baseline_2026_09.py
#       with down_revision = None.  Open it and confirm that.

# ── STEP 3 ── Prove it builds a correct schema from nothing.
createdb kairox_verify
DATABASE_URL="...kairox_verify" alembic upgrade head
DATABASE_URL="...kairox_verify" python - <<'PY'
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from sqlalchemy import create_engine
from app.core.config import get_settings
from alembic import env  # imports every models module
from app.core.database import Base
e = create_engine(get_settings().database_url)
with e.connect() as c:
    diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
print("DRIFT:", len(diff))
for d in diff: print("  ", d)
PY
#    DRIFT must be 0. Anything listed is a real gap — fix the baseline, not the models.

# ── STEP 4 ── Move each LIVE database onto the new baseline WITHOUT running it.
DATABASE_URL=<staging>     alembic stamp head --purge
DATABASE_URL=<production>  alembic stamp head --purge
#    --purge empties alembic_version first. You need it because the row still names
#    a revision id whose file you just deleted; without --purge Alembic tries to
#    resolve that id, fails, and refuses.
#    No DDL runs. No data is touched. It is a pointer update.

DATABASE_URL=<staging> alembic current   # -> the new baseline id
```

After step 4 every database — live and fresh — agrees with a folder holding **one** file.
The next change you make adds file #2.

**The one thing a squash throws away:** the *data-fixing* steps in the old chain (for example
`20260810_role_values_case_fix`, which repaired existing `app_user.role` values). That is
harmless here, because the databases that needed that repair have already had it, and a brand-new
database has no rows to repair. But it is the reason you cannot squash a chain that is only
*partially* applied somewhere — hence STEP 0.

---

### 4d. "Can I do this in production when files exceed 20?"

Yes — §4c *is* that procedure, and production is the easy case: it runs `stamp`, which does
nothing to the data. But do not automate it or do it often.

Sensible cadence: **squash at a release boundary, at most once or twice a year**, and never
while a deploy is half-rolled-out. Between squashes, let the files accumulate — that is what
they are for.

---

## 5. What `--autogenerate` gets WRONG (read every generated file)

Autogenerate is a very good first draft and a terrible final answer. In this repo specifically:

1. **Column renames become drop + add — this deletes the data.**
   Autogenerate cannot tell a rename from "old column gone, new column appeared". It writes
   `op.drop_column('piece', 'old_name')` + `op.add_column(...)`. Replace both lines by hand with
   `op.alter_column('piece', 'old_name', new_column_name='new_name')`.

2. **New values on a native PG enum are invisible.** `app_user.role` is a real Postgres type
   (`user_role`). Adding a member to `UserRole` in Python produces **no** migration. You write it
   yourself (CLAUDE.md §13):
   ```python
   if op.get_bind().dialect.name == "postgresql":
       op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'LINING_MANAGER'")
   ```
   Add the **member NAME, uppercase** (`LINING_MANAGER`), never the value (`lining_manager`) —
   this project persists enum names, and a lowercase label is one the ORM will never emit.

3. **`CHECK` constraints and some `server_default`s are not diffed.** Write them by hand.

4. **Data is never migrated.** Backfilling a new NOT NULL column is three steps you write
   yourself: add it nullable → `op.execute("UPDATE ...")` → alter to NOT NULL.

5. **It only sees models that were imported.** `alembic/env.py` imports every module's `models`
   explicitly. Add a new module and forget to add its import there, and autogenerate will
   silently **DROP** its tables (it sees a table the metadata doesn't know about... and in the
   reverse direction, never creates it). Adding the import is part of adding a module.

6. **`down_revision` must point at the real head.** Run `alembic heads` first. If two people
   branch at once you get two heads — fix by editing one file's `down_revision` to chain after
   the other, or `alembic merge`.

---

## 6. The `DEBUG=true` interaction — the trap that produced two repair migrations

`app/main.py` startup: when `DEBUG=true` **and** the database has no `alembic_version` table,
the app runs `Base.metadata.create_all` — it builds today's tables directly from the models,
skipping migrations entirely.

That is how `20260910_bom_intake_tables` and `20260922_fresh_db_parity` came to exist: live
databases had tables no migration had ever created, so a fresh `alembic upgrade head` died.

**The rule for any new database, in one line:**

```bash
alembic upgrade head     # FIRST
uvicorn app.main:app     # only then start the app
```

Do it in that order and `create_all` never fires (the `alembic_version` table already exists,
and the startup code skips itself — it logs `Alembic-managed database: skipping debug create_all`).

In every deployed environment keep `DEBUG=false`, which removes the possibility entirely.

---

## 7. Housekeeping in `alembic/versions/`

That folder should contain **only** revision `.py` files. Right now it also holds:

- `versions.txt` — a dump of old migration source. Git already has this; delete it.
- `supplier_order_spec.zip` — does not belong in a migrations folder.
- `__pycache__/` — should be gitignored.

Also: file names are cosmetic. `alembic.ini`'s `file_template` gives new files a
`YYYYMMDD_HHMM_slug` name, but Alembic orders by `down_revision`, never by filename. Renaming a
file is safe; changing its `revision =` id is not.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Can't locate revision identified by 'xxxx'` | the DB's version row names a file that no longer exists | `alembic stamp head --purge` (after confirming the schema really matches) |
| `Target database is not up to date` | you tried to autogenerate while behind | `alembic upgrade head` first |
| Multiple heads | two branches added migrations in parallel | `alembic heads`, then repoint one `down_revision`, or `alembic merge heads` |
| `DuplicateColumn` / `DuplicateTable` on upgrade | the schema is AHEAD of `alembic_version` — usually `create_all` ran | verify the column exists, then `alembic stamp` past that revision |
| Empty autogenerate output | models and database already agree | nothing is wrong |
| Autogenerate wants to DROP a table you still use | its `models` module is missing from `alembic/env.py` | add the import, regenerate |

---

## 9. The short version

1. Change models → `alembic revision --autogenerate -m "..."` → **read the file** → `alembic upgrade head`.
2. Never edit a migration that has already run somewhere you do not control.
3. `alembic current` vs `alembic heads` is your health check.
4. File count is not a problem. Squash at a release boundary with §4c, using `stamp --purge` on
   live databases so nothing is rebuilt and no data moves.
5. New database: `alembic upgrade head` **before** the app's first start.
