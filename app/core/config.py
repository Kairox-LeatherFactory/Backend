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

    # ── Database ─────────────────────────────────────────────────────────────
    # Sync URL drives Alembic migrations and the seed script (simpler, blocking).
    database_url: str = "postgresql+psycopg2://factory:factory@localhost:5432/factory"
    # Async URL drives the live API (non-blocking). If left blank we derive it
    # from database_url by swapping the driver to asyncpg.
    async_database_url: str = ""

    # ── Auth (self-issued JWT) ───────────────────────────────────────────────
    # Chat model for the LangGraph agent. Blank => deterministic router (no model).
    # Examples: "ollama:qwen2.5:3b-instruct", "anthropic:claude-3-5-haiku", "openai:gpt-4o-mini"
    chat_model: str = ""

    secret_key: str = "dev-only-insecure-change-me-in-prod"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24      # 24 hours

    # Login rate-limit (defends the login endpoint from brute force).
    login_max_attempts: int = 5
    login_window_seconds: int = 600                  # 10 minutes

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
