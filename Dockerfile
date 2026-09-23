# ──────────────────────────────────────────────────────────────
# Leather Factory backend — Dockerfile  (the "recipe" for the box)
# ──────────────────────────────────────────────────────────────
# ONE image, every role. docker-compose passes the word (api/worker/beat);
# entrypoint.sh dispatches on it. The SAME image runs on dev, staging, and prod —
# that identity is what makes staging a trustworthy rehearsal.
#
# IMPORTANT: your requirements.txt MUST include gunicorn (and uvicorn). In World A
# gunicorn lived in your .venv; in Docker it must be pinned in requirements.txt,
# e.g.:  gunicorn==23.0.0   uvicorn[standard]==0.34.0
# ──────────────────────────────────────────────────────────────
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System tools the app needs:
#   gcc + libpq-dev : build psycopg2 (Postgres driver) if no wheel
#   curl            : used by the api HEALTHCHECK (defined per-service in compose)
#   dos2unix        : normalise entrypoint line endings (Windows-edited files)
#   tesseract-ocr   : OCR binary for scanned-PDF identity checks
#   procps          : pgrep — the celery BEAT healthcheck in compose (beat has no
#                     `inspect ping`, so liveness is "is the process still there")
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
        dos2unix \
        tesseract-ocr \
        procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps FIRST (cached layer; only rebuilds when requirements change).
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the rest of the source.
COPY . .

# Make the entrypoint runnable and CRLF-safe.
RUN dos2unix entrypoint.sh 2>/dev/null || true \
    && chmod +x entrypoint.sh

# Celery beat's schedule state ("when did each crontab entry last fire"). compose
# mounts a named volume here so a restart doesn't re-fire everything beat thinks
# it missed; this mkdir keeps the `beat` role working even without that volume.
RUN mkdir -p /app/var/celerybeat /app/var/procurement-documents

EXPOSE 8000
EXPOSE 5555

# UPDATED 2026-09-12 (Hamthan): the image-wide HEALTHCHECK that lived here
# (curl localhost:8000/health) is GONE. It is baked into every container built
# from this image, but only the `api` role serves HTTP — so the `worker` and
# `beat` containers, which are working perfectly, were reported `unhealthy`
# forever. That is not cosmetic: docker-compose.dev.yml gates the worker on
# `api: condition: service_healthy`, and anything that later gates on the worker
# would never start. Health is now declared PER SERVICE in docker-compose.yml:
#   api    -> curl /health
#   worker -> celery inspect ping
#   beat   -> pgrep the beat process

ENTRYPOINT ["./entrypoint.sh"]
CMD ["api"]
