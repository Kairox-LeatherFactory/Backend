# ──────────────────────────────────────────────────────────────
# Leather Factory backend — Dockerfile
# ──────────────────────────────────────────────────────────────
# Single-stage build. The Python slim base is already small, and the
# heavy deps (torch/faiss/sentence-transformers) ship as wheels, so a
# multi-stage build would add complexity for marginal size gain.
#
# Same image is used for every role; docker-compose passes the command
# ("api", "migrate", "seed", "shell") which entrypoint.sh dispatches on.
# ──────────────────────────────────────────────────────────────

# 3.13-slim: matches the Python version requirements.txt was resolved &
# verified against (see the header note in requirements.txt).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   - gcc + libpq-dev : build any C extensions that lack a wheel
#   - curl            : used by the HEALTHCHECK below
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

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

# entrypoint.sh dispatches on the command; compose overrides "api" as needed.
ENTRYPOINT ["./entrypoint.sh"]
CMD ["api"]
