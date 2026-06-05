#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# Leather Factory backend — container entrypoint
# ──────────────────────────────────────────────────────────────
# Dispatches based on the CMD passed by docker-compose / docker run:
#
#   api       → wait for DB → migrate → seed → uvicorn   (default)
#   migrate   → run Alembic migrations and exit          (useful in CI)
#   seed      → force-run the seed script and exit
#   shell     → drop into bash (for debugging)
#
# Environment knobs (set in docker-compose.yml):
#   RUN_MIGRATIONS=true   → run `alembic upgrade head` before starting (api only)
#   RUN_SEED=true         → run scripts.seed on boot (api only; seed is idempotent)
#   UVICORN_RELOAD=true   → start uvicorn with --reload (dev hot-reload)
#   DB_WAIT_MAX_ATTEMPTS  → how many seconds to wait for Postgres (default 60)
# ──────────────────────────────────────────────────────────────

set -euo pipefail

CMD="${1:-api}"

# ─── Helpers ──────────────────────────────────────────────────

log() { echo "[entrypoint] $*"; }
err() { echo "[entrypoint] ERROR: $*" >&2; }

# Wait for Postgres to accept real connections. We use psycopg2 (already on
# PATH via requirements) with the SAME DATABASE_URL the app/Alembic use, so a
# success here means migrations and the app can connect too. This is more
# reliable than pg_isready, which isn't installed in this image.
wait_for_postgres() {
    log "Waiting for Postgres to accept connections..."
    local max_attempts="${DB_WAIT_MAX_ATTEMPTS:-60}"
    local attempt=0
    until python -c "
import os, sys
try:
    import psycopg2
    url = os.environ.get('DATABASE_URL', '')
    # psycopg2 wants a raw DSN — strip the SQLAlchemy driver prefix.
    url = url.replace('postgresql+psycopg2://', 'postgresql://')
    psycopg2.connect(url, connect_timeout=2).close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge "$max_attempts" ]; then
            err "Postgres not reachable after ${max_attempts}s. Giving up."
            exit 1
        fi
        sleep 1
    done
    log "Postgres is ready (${attempt}s)."
}

# Run Alembic migrations. A failure here is fatal — a half-migrated schema
# should not start serving traffic.
run_migrations() {
    log "Running Alembic migrations..."
    if ! alembic upgrade head; then
        err "Migrations failed. Aborting."
        exit 1
    fi
    log "Migrations complete."
}

# Run the seed script. It is idempotent (existing rows are reused, not
# duplicated), so it's safe to run on every boot. A seed failure should NOT
# block the API — devs see the error and can reset the volume if needed.
run_seed() {
    log "Seeding database (idempotent)..."
    if ! python -m scripts.seed; then
        err "Seed failed. Continuing anyway — DB may be partially populated."
    fi
}

# ─── Dispatch ─────────────────────────────────────────────────

case "$CMD" in
    api)
        wait_for_postgres
        if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then run_migrations; fi
        if [ "${RUN_SEED:-true}" = "true" ]; then run_seed; fi
        log "Starting uvicorn..."
        exec uvicorn app.main:app \
            --host 0.0.0.0 \
            --port 8000 \
            --proxy-headers \
            ${UVICORN_RELOAD:+--reload}
        ;;

    migrate)
        wait_for_postgres
        run_migrations
        log "Migrations done. Exiting."
        ;;

    seed)
        wait_for_postgres
        run_seed
        log "Seed done. Exiting."
        ;;

    shell)
        log "Dropping into bash shell..."
        exec /bin/bash
        ;;

    *)
        # Unknown directive → treat the whole argv as a raw command.
        log "Unknown directive '$CMD' → running as raw command."
        exec "$@"
        ;;
esac
