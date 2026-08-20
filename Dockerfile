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
#   curl            : used by the HEALTHCHECK
#   dos2unix        : normalise entrypoint line endings (Windows-edited files)
#   tesseract-ocr   : OCR binary for scanned-PDF identity checks
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
        dos2unix \
        tesseract-ocr \
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

EXPOSE 8000

# The box checks its own health every 30s by hitting /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["api"]
