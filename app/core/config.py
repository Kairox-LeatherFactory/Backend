"""
================================================================================
core/config.py — Central application configuration (single source of truth)
================================================================================

PURPOSE
    Every tunable value the app needs (database URLs, JWT secret, token lifetime,
    feature flags) is declared here ONCE and read from environment variables.
    No module reads os.environ directly — they all import `settings` from here.

WHY THIS DESIGN
    - Twelve-factor config: code is identical across local/staging/prod; only the
      environment differs. Secrets never live in source.
    - pydantic-settings validates and type-coerces env vars at startup, so a
      misconfigured deployment fails loudly and immediately instead of at the
      first request.
    - `@lru_cache` makes get_settings() a singleton — the .env file is parsed
      exactly once per process.

KEY FIELDS A DEVELOPER NEEDS TO KNOW
    database_url          Sync Postgres URL  (psycopg2)  — used by Alembic + seed.
    async_database_url    Async Postgres URL (asyncpg)   — used by the live API.
                          Auto-derived from database_url if not set explicitly.
    secret_key            HMAC key used to SIGN our own JWTs (we now MINT tokens,
                          we no longer merely verify Supabase's).
    access_token_expire_minutes   JWT lifetime.

MIGRATION NOTE (Supabase -> self-issued JWT)
    The old `supabase_jwt_secret` field is intentionally removed. We now issue
    and verify our own tokens with `secret_key`. See core/security.py.
================================================================================
"""
from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Export the .env file into the real process environment as well. pydantic-settings
# reads .env only for the Settings fields below; SDKs that read os.environ directly
# (e.g. langchain-groq's ChatGroq -> GROQ_API_KEY) need the vars actually exported.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "Leather Factory Intelligence Platform"
    environment: str = "local"          # local | staging | production
    debug: bool = True                  # auto-create tables on startup when True
    # Root log level for the app's loggers. DEBUG surfaces every pipeline step
    # (upload → scan → sniff → classify → validate → BOM extract); INFO is the
    # production default. Configured once in app.main._configure_logging().
    log_level: str = "INFO"             # DEBUG | INFO | WARNING | ERROR

    # ── Database ─────────────────────────────────────────────────────────────
    # Sync URL drives Alembic migrations and the seed script (simpler, blocking).
    database_url: str = "postgresql+psycopg2://factory:factory@localhost:5432/factory"
    # Async URL drives the live API (non-blocking). If left blank we derive it
    # from database_url by swapping the driver to asyncpg.
    async_database_url: str = ""
    # Optional least-privilege, READ-ONLY URL for the LLM / agent path (SELECT on
    # whitelisted tables only). Blank => unused (the agent shares the app session).
    # This is NOT Supabase Auth — just a separate Postgres role connection string.
    ai_reader_database_url: str = ""

    # ── Auth (self-issued JWT) ───────────────────────────────────────────────
    # Chat model for the LangGraph agent. Blank => deterministic router (no model).
    # Examples: "ollama:qwen2.5:3b-instruct", "anthropic:claude-3-5-haiku", "openai:gpt-4o-mini"
    chat_model: str = ""
    
    # PDF "is this scanned?" threshold. A page with fewer than this many extracted
    # text characters routes to the vision LLM instead of the text LLM. 40 chars per
    # page reliably distinguishes a real text-layer PDF from one with noise leak.
    pdf_text_density_min: int = 40

    # ── Document extraction (BOM Procurement Workflow) ───────────────────────
    # Policy: NO LLM unless extraction/classification genuinely requires it (scanned
    # / handwritten PDFs, narrative spec sheets, ambiguous classification). The
    # service runs a provider chain Gemini -> Groq -> "needs manual entry"; it never
    # silently guesses. Embeddings remain local HF (intelligence/models_catalog.py).
    extraction_model: str = "gemini:gemini-2.0-flash"          # primary (multimodal/long-context)
    extraction_fallback_model: str = "groq:llama-3.3-70b-versatile"  # fallback (text/structured)
    gemini_api_key: str = ""        # langchain-google-genai
    groq_api_key: str = ""          # langchain-groq (also read from env by ChatGroq)
    # Hard wall-clock budget (seconds) for a SINGLE LLM call. A hung/slow Gemini must
    # raise — not block BOM generation forever — so the Gemini→Groq→deterministic
    # fallback actually fires. max_retries=0 keeps a dead provider from burning the
    # whole budget on internal backoff before we move to the next rung.
    llm_request_timeout: float = 20.0
    llm_max_retries: int = 0

    # ── Scanned-document OCR + Gemini vision fallback (§3a identity validation) ─
    # The escalation ladder for an upload the cheap heuristic can't settle:
    #   1. OCR  — Tesseract over rasterised pages fills text_blob for a scanned PDF
    #             so the text classifier (Gemini→Groq) gets something to read.
    #   2. text classifier — runs on that OCR text (or the native text/cells).
    #   3. vision classifier — if the text path stays below vision_conf_threshold
    #             (genuinely ambiguous / needs_manual_review), send the actual page
    #             images to Gemini vision for a second opinion. PDFs go as rendered
    #             PNGs; XLSX/CSV (no image) fall back to a full-content Gemini retry.
    # Each rung degrades gracefully: no tesseract binary / no Gemini key → the rung
    # is skipped and we defer to a human rather than crash or fabricate a verdict.
    ocr_enabled: bool = True
    ocr_language: str = "eng+jap"                       # tesseract language pack(s), e.g. "eng+fra"
    ocr_max_pages: int = 5                           # cap pages OCR'd (cost/latency bound)
    ocr_dpi: int = 200                               # rasterisation DPI for OCR + vision
    vision_classifier_enabled: bool = True
    vision_model: str = "gemini:gemini-2.0-flash"    # multimodal model for the vision rung
    vision_conf_threshold: float = 0.5               # below this from the text path → escalate
    vision_max_pages: int = 4                        # cap page-images sent to the vision model

    secret_key: str = "dev-only-insecure-change-me-in-prod"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24      # 24 hours

    # Login rate-limit (defends the login endpoint from brute force).
    login_max_attempts: int = 10
    login_window_seconds: int = 60                 # 10 minutes

    # ── Stage 1: upload, storage & virus scan (BOM Procurement Workflow) ─────
    # Pluggable storage backend so the repo keeps NO hard Supabase dependency and
    # the SQLite/local test path is preserved. `local` writes under a directory;
    # `supabase`/`s3` are the staging/prod drivers (blank-defaulted credentials).
    storage_backend: str = "local"                 # local | s3 | minio | supabase
    local_storage_dir: str = "./var/procurement-documents"
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_bucket: str = "procurement-documents"
    s3_endpoint_url: str = ""                       # MinIO / S3-compatible endpoint
    s3_region: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    # Upload limits. MAX_UPLOAD_MB closes the unbounded-read gap in the imports
    # handler (CLAUDE.md §13.4) — the body is streamed and aborted past the cap.
    max_upload_mb: int = 25
    # Virus scan (real ClamAV) gated by a flag: ON in staging/prod (reject on hit,
    # fail closed if clamd is unreachable); OFF in dev records scan_status=skipped.
    virus_scan_enabled: bool = False
    clamd_host: str = "127.0.0.1"
    clamd_port: int = 3310

    # ── Stage 3: BOM approval — notifications + escalation (BOM Procurement) ──
    # When a BOM enters `ready_for_review` the MD (and, when enabled, the DM) get an
    # in-app `notification` row delivered over SSE. If a recipient has NOT *seen* it
    # within `bom_review_escalation_hours`, a DB-driven in-process sweeper sends an
    # auto-email (the product owner's 2-hour rule; supersedes the doc's "5 hour").
    bom_review_escalation_hours: int = 2
    notify_dm_on_review: bool = True             # notify MD + DM (vs MD only)
    # The in-process escalation sweeper (started in main.py lifespan). Single-replica
    # only — at >1 replica move to SELECT ... FOR UPDATE SKIP LOCKED / an external
    # worker (mirrors the in-process login rate-limiter caveat). Off in tests.
    notification_sweeper_enabled: bool = True
    notification_sweep_seconds: int = 60
    # Email transport for the escalation. `log` (default) writes to stdout — exercises
    # the path with no SMTP server; `smtp` sends for real; `noop` discards.
    email_backend: str = "log"                   # log | smtp | noop
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@leatherfactory.local"
    smtp_use_tls: bool = True
    # Base URL the in-app/email deep link points at (the approval screen).
    frontend_base_url: str = "http://localhost:3000"

    # ── Stage 5: supplier PO — buyer block, GST, send, tracking, escalation ───
    # The fixed buyer block printed on every PO form (PAKKAR TANVEER EXPORTS), from the
    # real suppler-po-form PDFs. Config, not hard-coded, so a different factory entity is
    # one settings change.
    po_buyer_name: str = "PAKKAR TANVEER EXPORTS"
    po_buyer_address: str = "NO.1 Hyder Garden, 3rd Street, Periamet, Chennai-600012"
    po_buyer_gstin: str = "33AAGPA0428C1ZO"
    po_buyer_state_code: str = "33"                 # Tamil Nadu — drives intra/inter GST (§2c)
    po_buyer_email: str = "tanveer@ptexports.com"
    po_buyer_phone: str = ""
    po_gst_rate: float = 12.0                        # CGST 6 + SGST 6 intra, IGST 12 inter (§2c)
    po_default_delivery_days: int = 10
    po_default_payment_terms_days: int = 60
    # The external supplier escalation window stays 5h (§7a) — distinct from the 2h
    # internal BOM-review nudge. The PO escalation sweeper shares the lifespan loop.
    po_escalation_hours: int = 5
    po_escalation_sweeper_enabled: bool = True
    # Base URL the open-tracking pixel + wrapped links resolve against (the API, not the
    # FE) — must be reachable by the supplier's mail client (§6b).
    po_tracking_base_url: str = "http://localhost:8000/api/v1/procurement"
    # WhatsApp + Voice escalation transport (§7b). `log` (default) writes to stdout so the
    # ladder is testable offline; `twilio` sends for real (blank-defaulted credentials).
    escalation_transport: str = "log"               # log | noop | twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""                  # "whatsapp:+1..."
    twilio_voice_from: str = ""                     # "+1..."
    # Amazon SES is the recommended real email driver (§5a) — it slots behind the same
    # email_backend switch as smtp/log (email_backend=ses; AWS_SES_* below).
    aws_ses_region: str = ""
    aws_ses_access_key_id: str = ""
    aws_ses_secret_access_key: str = ""

    # ── Domain knobs ─────────────────────────────────────────────────────────
    # How many days before a PO's sea-freight cutoff we start raising warnings.
    sea_cutoff_warning_days: int = 7

    # ── Derived helpers ──────────────────────────────────────────────────────
    @property
    def effective_async_url(self) -> str:
        """Async URL to use, deriving one from the sync URL if not provided."""
        if self.async_database_url:
            return self.async_database_url
        url = self.database_url
        # Normalise common sync prefixes to the asyncpg driver.
        if url.startswith("postgresql+psycopg2://"):
            return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("sqlite") and "+aiosqlite" not in url:
            # Tests/local: any sync sqlite variant -> async aiosqlite.
            if url.startswith("sqlite+pysqlite://"):
                return url.replace("sqlite+pysqlite://", "sqlite+aiosqlite://", 1)
            return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return url

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Convenient module-level singleton for the common `from app.core.config import settings`.
settings = get_settings()
