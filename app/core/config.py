# @file    config.py

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Database ─────────────────────────────────────────────
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_NAME: str = "finflux"

    # ── App ──────────────────────────────────────────────────
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_ENV: str = "development"

    # ── CORS ─────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:5173"

    # ── Auth / JWT ───────────────────────────────────────────
    JWT_SECRET: str = "dev-only-change-me-in-production-please-use-secrets-token_urlsafe"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TTL_MIN: int = 15
    REFRESH_TTL_DAYS: int = 7
    INVITE_TTL_HOURS: int = 48
    RESET_TTL_HOURS: int = 4

    # ── Cookies ──────────────────────────────────────────────
    COOKIE_SECURE: bool = False
    COOKIE_DOMAIN: str = ""

    # ── Bootstrap admin ──────────────────────────────────────
    ADMIN_EMAIL: str = "admin@finflux.local"
    ADMIN_PASSWORD: str = "Admin@12345"
    ADMIN_NAME: str = "System Admin"

    # ── Crypto (ERP secrets) ─────────────────────────────────
    # FERNET_KEY: legacy key used by Fernet wrapper.
    # CREDENTIAL_ENCRYPTION_KEY: preferred env var for credential encryption
    # (Business Central + future connectors). When set, takes precedence.
    # Both must remain server-side only — never returned to clients.
    # The key MUST be stable across restarts — changing it makes existing
    # encrypted credentials unreadable (decrypt → INVALID_CONFIG).
    FERNET_KEY: str = ""
    CREDENTIAL_ENCRYPTION_KEY: str = ""

    # Opt-in escape hatch for local bootstrap only: when true AND APP_ENV is
    # development AND no key is configured, the backend will mint an ephemeral
    # Fernet key. This is unsafe — credentials encrypted under that key cannot
    # be decrypted after the next restart.
    ALLOW_EPHEMERAL_DEV_KEY: bool = False

    # ── Business Central HTTP defaults ───────────────────────
    BC_HTTP_TIMEOUT_SECONDS: float = 20.0

    # ── QuickBooks Online OAuth (Intuit app — server-side only) ──
    # Optional local-dev defaults only. Primary source of truth is per-entity
    # credentials saved from Admin → Integrations.
    QUICKBOOKS_CLIENT_ID: str = ""
    QUICKBOOKS_CLIENT_SECRET: str = ""
    QUICKBOOKS_REDIRECT_URI: str = ""
    QUICKBOOKS_ENVIRONMENT: str = "sandbox"
    QUICKBOOKS_SCOPES: str = ""
    QUICKBOOKS_AUTH_URL: str = ""
    QUICKBOOKS_TOKEN_URL: str = ""
    QUICKBOOKS_API_BASE_SANDBOX: str = ""
    QUICKBOOKS_API_BASE_PRODUCTION: str = ""
    QUICKBOOKS_HTTP_TIMEOUT_SECONDS: float = 30.0

    # ── Business Central automated sync ──────────────────────
    # Disabled by default — explicitly opt-in via env. The scheduler runs in
    # the same uvicorn process via APScheduler (no Celery / Redis); jobs are
    # ENTIRELY backend-driven and never expose tenant/client/secret/token
    # to logs or the frontend.
    BC_AUTO_SYNC_ENABLED: bool = False
    # Standard 5-field cron expression. Default = daily 02:00 in the
    # configured timezone.
    BC_AUTO_SYNC_CRON: str = "0 2 * * *"
    # Any IANA zone (Asia/Kolkata, Etc/UTC, …). Falls back to UTC if invalid.
    BC_AUTO_SYNC_TIMEZONE: str = "Asia/Kolkata"
    # Cap how many entities one cron tick may touch — prevents a runaway run
    # if a customer has many BC-mapped entities.
    BC_AUTO_SYNC_MAX_ENTITIES_PER_RUN: int = 20
    # Skip an entity if a sync is currently `running` and started less than
    # this many minutes ago. Acts as a soft duplicate-prevention lock.
    BC_AUTO_SYNC_LOCK_MINUTES: int = 60
    # Default look-back window (days) for incremental GL entry syncs when no
    # successful previous sync is on file.
    BC_AUTO_SYNC_INCREMENTAL_DAYS: int = 7

    # ── SLM / PHI-4 chat gateway (server-side only) ──────────
    # The Flux AI commentary generator calls a private Small-Language-Model
    # gateway (PHI-4 family today). Credentials and endpoint MUST come from
    # this env block — never from frontend code, never hardcoded. Empty
    # values mean "not configured": the API endpoint then returns a safe
    # SLM_CONFIG_MISSING error and the rest of the app continues to boot.
    SLM_CHAT_URL: str = ""
    SLM_CHAT_API_KEY: str = ""
    SLM_CHAT_PROJECT_ID: str = ""
    SLM_CHAT_TEMPLATE_ID: str = ""
    SLM_CHAT_TIMEOUT_SECONDS: float = 30.0
    SLM_CHAT_MAX_OUTPUT_TOKENS: int = 0  # 0 → omit max_tokens from request
    SLM_CHAT_CONCURRENCY: int = 4        # advisory cap for future batching

    # ── Email ────────────────────────────────────────────────
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "no-reply@finflux.local"
    SMTP_FROM_NAME: str = "FinFlux"
    SMTP_USE_TLS: bool = True
    APP_URL: str = "http://localhost:5173"

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            f"?charset=utf8mb4"
        )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV == "development"


settings = Settings()
