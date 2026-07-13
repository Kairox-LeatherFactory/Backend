# ──────────────────────────────────────────────────────────────
# Leather Factory backend — Dockerfile
# ──────────────────────────────────────────────────────────────
# Single-stage build. The Python slim base is already small, and the
# heavy deps (torch/faiss/sentence-transformers) ship as wheels, so a
# multi-stage build would add complexity for marginal size gain.
#
# ONE image, every role. docker-compose passes the command — entrypoint.sh
# dispatches on it:
#   api      → wait-for-db → migrate → seed → uvicorn   (default)
#   worker   → wait-for-db + redis → celery worker      (background BOM jobs)
#   beat     → wait-for-redis → celery beat             (scheduled jobs)
#   migrate / seed / shell
# Celery + Redis add NO system deps (redis-py is a pure-Python wheel); they enter
# via requirements.txt, which must pin:  celery[redis]==5.5.3 , redis==6.4.0
# ──────────────────────────────────────────────────────────────

# 3.13-slim: matches the Python version requirements.txt was resolved &
# verified against (see the header note in requirements.txt).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   - gcc + libpq-dev : build any C extensions that lack a wheel (incl. psycopg2)
#   - curl            : used by the api HEALTHCHECK below
#   - dos2unix        : entrypoint.sh is edited on Windows; normalise CRLF so
#                       the shebang resolves (otherwise: "no such file or directory")
#   - tesseract-ocr   : the OCR binary pytesseract shells out to, for the scanned-PDF
#                       rung of identity validation (sniffing._ocr_pdf). Absent → OCR
#                       silently no-ops and the upload escalates to the Gemini vision rung.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
        dos2unix \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer is cached and only rebuilt when
# requirements.txt changes — not on every source edit.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the rest of the source.
COPY . .

# Normalise line endings on the entrypoint (Windows checkouts otherwise fail
# with "no such file or directory" on /usr/bin/env bash) and make it executable.
RUN dos2unix entrypoint.sh 2>/dev/null || true \
    && chmod +x entrypoint.sh

EXPOSE 8000

# Image-level healthcheck targets the API. It is harmless for worker/beat (compose
# defines their lifecycle); compose's own api healthcheck is the one that gates
# worker/beat startup.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

# entrypoint.sh dispatches on the command; compose overrides "api" per service.
ENTRYPOINT ["./entrypoint.sh"]
CMD ["api"]