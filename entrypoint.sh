#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# KairoX backend — container entrypoint (api + celery build)
# ──────────────────────────────────────────────────────────────
# Reads ONE word:
#   api          → wait for DB → [migrate] → [seed] → GUNICORN(uvicorn workers)
#   worker       → wait for DB + broker → celery worker          (BOM/DXF tasks)
#   beat         → wait for broker → celery beat                 (periodic jobs)
#   worker-beat  → worker WITH embedded beat (-B)  — DEV ONLY, never >1 replica
#   flower       → celery monitoring UI on :5555   (profile: monitor)
#   migrate      → alembic upgrade head, then exit               (one-off)
#   seed         → run the THREE seed scripts, then exit         (MANUAL only)
#   shell        → bash                                          (debugging)
#   <anything>   → exec'd raw, e.g.
#                  docker compose run --rm worker celery -A app.core.celery:celery_app inspect active
#
# This container runs GUNICORN itself — it REPLACES the systemd gunicorn.service
# you use in World A. In Docker you do NOT write a .service file.
#
# UPDATED 2026-09-12 (Hamthan): docker-compose.yml has shipped `worker` and `beat`
# services since the Celery cut-over, but this script only ever handled
# api|migrate|seed|shell — both words fell through to the `*)` branch and were
# exec'd as literal binaries ("worker: not found"), so the two containers
# crash-looped and every enqueued BOM/DXF task sat in Redis forever. The whole
# bom module is Celery-driven (build_order_breakdown_for_submission,
# parse_pattern_dxf, plus the two beat jobs), so those cases are the difference
# between the BOM pipeline running and silently doing nothing.
#
# SEED POLICY: OFF by default. A deploy must never auto-load data. Seed is a
# deliberate one-off:  ... run --rm api seed
# ──────────────────────────────────────────────────────────────
set -euo pipefail
CMD="${1:-api}"

# Path to the leather-lots CSV INSIDE the image. Override with LEATHER_LOTS_CSV
# if your filename differs. Must exist in the repo (copied in by `COPY . .`).
LEATHER_LOTS_CSV="${LEATHER_LOTS_CSV:-data/clean_leather_lots_finalZZ.csv}"

# ── Celery wiring ────────────────────────────────────────────────────────────
# CELERY_APP      : the worker's app. app/core/celery.py imports EVERY models.py
#                   before defining celery_app — that import block is what stops
#                   the worker dying on "could not find table 'client'", so never
#                   point this at a module that skips it.
# CELERY_BEAT_APP : beat's app — DIFFERENT on purpose. conf.beat_schedule is set
#                   in app/core/beat_schedule.py, which app.core.celery does NOT
#                   import (that would be circular). Pointing beat at
#                   app.core.celery gives it an EMPTY schedule, so freight-risk-scan
#                   and notification-escalation would never fire. Targeting
#                   beat_schedule imports celery.py first (it does `from
#                   app.core.celery import celery_app`) and then registers the
#                   schedule onto that same object.
CELERY_APP="${CELERY_APP:-app.core.celery:celery_app}"
CELERY_BEAT_APP="${CELERY_BEAT_APP:-app.core.beat_schedule:celery_app}"
CELERY_LOGLEVEL="${CELERY_LOGLEVEL:-info}"
# Beat persists its "when did I last fire" state here. Kept on a VOLUME so a
# container restart doesn't re-fire every crontab entry it thinks it missed.
CELERY_BEAT_SCHEDULE_FILE="${CELERY_BEAT_SCHEDULE_FILE:-/app/var/celerybeat/celerybeat-schedule}"

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

# Celery fails LOUDLY but slowly against a broker that isn't up yet (it retries
# the connection forever and logs a wall of tracebacks). Waiting here keeps the
# worker's first log line meaningful. Works for the bundled dev redis AND for
# Upstash / Redis Cloud over TLS (rediss://) — same client, same ping.
wait_for_broker() {
    local url="${CELERY_BROKER_URL:-}"
    case "$url" in
        redis://*|rediss://*) ;;
        "") err "CELERY_BROKER_URL is empty — the worker has no broker to consume from."; exit 1 ;;
        *)  log "Broker is not Redis (${url%%://*}://) — skipping the ping check."; return 0 ;;
    esac
    log "Waiting for the Celery broker (Redis)..."
    local max="${BROKER_WAIT_MAX_ATTEMPTS:-60}" n=0
    until python -c "
import os,sys
try:
    import redis
    redis.from_url(os.environ['CELERY_BROKER_URL'],
                   socket_connect_timeout=2, socket_timeout=2).ping(); sys.exit(0)
except Exception: sys.exit(1)
" 2>/dev/null; do
        n=$((n+1)); [ "$n" -ge "$max" ] && { err "Broker unreachable after ${max}s: ${url%%@*}"; exit 1; }
        sleep 1
    done
    log "Broker ready (${n}s)."
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

# Build the `celery ... worker` argv. `embed_beat=1` adds -B (dev single-container).
build_worker_args() {
    local embed_beat="${1:-0}"
    WORKER_ARGS=(
        -A "${CELERY_APP}" worker
        --loglevel "${CELERY_LOGLEVEL}"
        # Concurrency is PREFORK processes. BOM extraction is IO-bound on the
        # Gemini call but the DXF parse (ezdxf/shapely) is CPU-bound, so keep
        # this near the core count rather than cranking it.
        --concurrency "${CELERY_CONCURRENCY:-4}"
        # ezdxf/shapely hold onto memory across a big pattern parse; recycling
        # the child caps the leak instead of letting the box drift into swap.
        --max-tasks-per-child "${CELERY_MAX_TASKS_PER_CHILD:-100}"
        --hostname "${CELERY_HOSTNAME:-worker@%h}"
    )
    # Only pass -Q when queues are explicitly configured. app/core/celery.py sets
    # no task_default_queue and no task_routes, so every task is published to the
    # default queue "celery" — passing -Q with anything else here gives you a
    # worker that consumes NOTHING while tasks pile up looking successful.
    [ -n "${CELERY_QUEUES:-}" ] && WORKER_ARGS+=( --queues "${CELERY_QUEUES}" )
    [ "$embed_beat" = "1" ] && WORKER_ARGS+=( --beat --schedule "${CELERY_BEAT_SCHEDULE_FILE}" )
    return 0
}

# Dev-only: restart the worker when a .py changes. Celery has no built-in
# --autoreload (removed in 4.x), so this shells out to watchdog's watchmedo.
# WHY it matters here: the worker holds your task + pydantic schema code in
# memory, so an edit to e.g. extraction_schemas.py does NOTHING until the
# process restarts — easy to mistake for "my fix didn't work".
exec_worker() {
    build_worker_args "${1:-0}"
    if [ "${CELERY_AUTORELOAD:-false}" = "true" ]; then
        if command -v watchmedo >/dev/null 2>&1; then
            log "Starting Celery worker WITH autoreload (dev): celery ${WORKER_ARGS[*]}"
            exec watchmedo auto-restart --directory=/app/app --pattern='*.py' --recursive \
                --signal SIGTERM -- celery "${WORKER_ARGS[@]}"
        fi
        err "CELERY_AUTORELOAD=true but watchmedo is not installed (pip install watchdog) — starting without it."
    fi
    log "Starting Celery worker: celery ${WORKER_ARGS[*]}"
    exec celery "${WORKER_ARGS[@]}"
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

    worker)
        # Tasks open their OWN AsyncSession (a worker is a different PROCESS from
        # the API), so the DB must be reachable here too — not just in the api box.
        wait_for_postgres
        wait_for_broker
        exec_worker 0
        ;;

    worker-beat)
        # DEV convenience: one container, worker + embedded scheduler. NEVER run
        # this with more than one replica — each embedded beat is its own
        # scheduler, so every periodic task fires once PER container.
        wait_for_postgres
        wait_for_broker
        exec_worker 1
        ;;

    beat)
        # Beat only PUBLISHES to the broker — it never touches Postgres, so no
        # wait_for_postgres here (the worker that picks the job up does that).
        wait_for_broker
        mkdir -p "$(dirname "${CELERY_BEAT_SCHEDULE_FILE}")"
        log "Starting Celery beat (schedule=${CELERY_BEAT_SCHEDULE_FILE})..."
        # --pidfile= (empty) disables the pidfile: a container killed with
        # SIGKILL leaves a stale pid behind, and beat then refuses to boot.
        exec celery -A "${CELERY_BEAT_APP}" beat \
            --loglevel "${CELERY_LOGLEVEL}" \
            --schedule "${CELERY_BEAT_SCHEDULE_FILE}" \
            --pidfile=
        ;;

    flower)
        wait_for_broker
        command -v celery >/dev/null 2>&1 || { err "celery not installed."; exit 1; }
        python -c "import flower" 2>/dev/null || {
            err "flower is not installed — add 'flower' to requirements.txt and rebuild."; exit 1; }
        log "Starting Flower on :5555..."
        exec celery -A "${CELERY_APP}" flower \
            --address=0.0.0.0 --port=5555 \
            --url_prefix="${FLOWER_URL_PREFIX:-}" \
            ${FLOWER_BASIC_AUTH:+--basic_auth="${FLOWER_BASIC_AUTH}"}
        ;;

    migrate) wait_for_postgres; run_migrations; log "Done."; ;;
    seed)    wait_for_postgres; run_seed;       log "Done."; ;;
    shell)   exec /bin/bash ;;
    *)       log "Unknown '$CMD' → raw"; exec "$@" ;;
esac
