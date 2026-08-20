#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# KairoX backend — container entrypoint (api-only build)
# ──────────────────────────────────────────────────────────────
# Reads ONE word:
#   api      → wait for DB → [migrate] → [seed] → GUNICORN(uvicorn workers)
#   migrate  → alembic upgrade head, then exit          (one-off)
#   seed     → run the THREE seed scripts, then exit     (MANUAL only)
#   shell    → bash                                      (debugging)
#
# This container runs GUNICORN itself — it REPLACES the systemd gunicorn.service
# you use in World A. In Docker you do NOT write a .service file.
#
# SEED POLICY: OFF by default. A deploy must never auto-load data. Seed is a
# deliberate one-off:  ... run --rm api seed
# ──────────────────────────────────────────────────────────────
set -euo pipefail
CMD="${1:-api}"

# Path to the leather-lots CSV INSIDE the image. Override with LEATHER_LOTS_CSV
# if your filename differs. Must exist in the repo (copied in by `COPY . .`).
LEATHER_LOTS_CSV="${LEATHER_LOTS_CSV:-data/clean_leather_lots_finalZZ.csv}"

log() { echo "[entrypoint] $*"; }
err() { echo "[entrypoint] ERROR: $*" >&2; }

wait_for_postgres() {
    log "Waiting for Postgres..."
    local max="${DB_WAIT_MAX_ATTEMPTS:-60}" n=0
    until python -c "
import os,sys
try:
    import psycopg2
    u=os.environ.get('DATABASE_URL','').replace('postgresql+psycopg2://','postgresql://')
    psycopg2.connect(u, connect_timeout=2).close(); sys.exit(0)
except Exception: sys.exit(1)
" 2>/dev/null; do
        n=$((n+1)); [ "$n" -ge "$max" ] && { err "Postgres unreachable after ${max}s"; exit 1; }
        sleep 1
    done
    log "Postgres ready (${n}s)."
}

run_migrations() {
    log "alembic upgrade head"
    alembic upgrade head || { err "Migrations failed."; exit 1; }
    log "Migrations complete."
}

# ── The THREE seed scripts, in the REQUIRED order ────────────────────────────
# 1) employees  — needs migrations done first (enum values must exist)
# 2) leather lots — needs an explicit --csv (Windows default path won't resolve here)
# 3) drawer barcodes
# All three are idempotent, so a re-run is safe, but we still only run this
# MANUALLY via the `seed` command — never automatically on api boot in prod.
run_seed() {
    log "── Seed 1/3: employees + cards + logins ──"
    python -m scripts.seed_employees || { err "seed_employees failed."; exit 1; }

    log "── Seed 2/3: leather lots (csv=${LEATHER_LOTS_CSV}) ──"
    if [ ! -f "${LEATHER_LOTS_CSV}" ]; then
        err "Leather-lots CSV not found at ${LEATHER_LOTS_CSV}."
        err "Set LEATHER_LOTS_CSV to the correct path, or ensure the file is in the image."
        exit 1
    fi
    python -m scripts.load_leather_lots --csv "${LEATHER_LOTS_CSV}" \
        || { err "load_leather_lots failed."; exit 1; }

    log "── Seed 3/3: 200 drawer barcodes ──"
    python -m scripts.gen_drawer_barcodes || { err "gen_drawer_barcodes failed."; exit 1; }

    log "All three seed scripts complete."
}

case "$CMD" in
    api)
        wait_for_postgres
        [ "${RUN_MIGRATIONS:-true}" = "true" ] && run_migrations
        # RUN_SEED stays OFF in staging/prod. Only turn it on in dev if you want
        # auto-seed on boot. Otherwise seed manually with the `seed` command.
        [ "${RUN_SEED:-false}" = "true" ] && run_seed
        log "Starting Gunicorn (uvicorn workers)..."
        exec gunicorn app.main:app \
            -k uvicorn.workers.UvicornWorker \
            -w "${WEB_CONCURRENCY:-2}" \
            -b 0.0.0.0:8000 \
            --timeout "${GUNICORN_TIMEOUT:-120}" \
            --graceful-timeout 30 \
            --access-logfile - \
            --error-logfile -
        ;;
    migrate) wait_for_postgres; run_migrations; log "Done."; ;;
    seed)    wait_for_postgres; run_seed;       log "Done."; ;;
    shell)   exec /bin/bash ;;
    *)       log "Unknown '$CMD' → raw"; exec "$@" ;;
esac