import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./revenue_recovery.db")
    jwt_secret: str = os.getenv("JWT_SECRET", "development-only-secret-change-before-production-2026")
    jwt_exp_minutes: int = int(os.getenv("JWT_EXP_MINUTES", "480"))
    live_actions: bool = os.getenv("LIVE_ACTIONS", "false").lower() == "true"
    verified_live_adapters: str = os.getenv("VERIFIED_LIVE_ADAPTERS", "")
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    safety_audit_all: bool = os.getenv("SAFETY_AUDIT_ALL", "true").lower() == "true"
    seed_demo: bool = os.getenv("SEED_DEMO", "true").lower() == "true"
    attribution_window_days: int = int(os.getenv("ATTRIBUTION_WINDOW_DAYS", "14"))
    webhook_secret_prefix: str = os.getenv("WEBHOOK_SECRET_PREFIX", "WEBHOOK_SECRET_")
    max_body_bytes: int = int(os.getenv("MAX_BODY_BYTES", "1048576"))
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    integration_encryption_key: str = os.getenv("INTEGRATION_ENCRYPTION_KEY", "")
    integration_client_prefix: str = os.getenv("INTEGRATION_CLIENT_PREFIX", "")
    auto_create_schema: bool = os.getenv("AUTO_CREATE_SCHEMA", "true").lower() == "true"
    login_rate_limit_per_minute: int = int(os.getenv("LOGIN_RATE_LIMIT_PER_MINUTE", "10"))
    register_rate_limit_per_minute: int = int(os.getenv("REGISTER_RATE_LIMIT_PER_MINUTE", "3"))
    api_rate_limit_per_minute: int = int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "600"))
    redis_url: str = os.getenv("REDIS_URL", "")
    allowed_hosts: str = os.getenv("ALLOWED_HOSTS", "*")
    app_version: str = os.getenv("APP_VERSION", "3.0.0")
    worker_poll_seconds: float = float(os.getenv("RROS_WORKER_POLL_SECONDS", "0.5"))
    worker_lease_seconds: int = int(os.getenv("RROS_WORKER_LEASE_SECONDS", "300"))
    worker_count: int = int(os.getenv("RROS_WORKERS", "4"))
    queue_max_attempts: int = int(os.getenv("RROS_QUEUE_MAX_ATTEMPTS", "8"))
    queue_tenant_concurrency: int = int(os.getenv("RROS_QUEUE_TENANT_CONCURRENCY", "8"))
    scheduler_interval_seconds: int = int(os.getenv("RROS_SCHEDULER_INTERVAL_SECONDS", "60"))
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "20"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "40"))
    db_pool_recycle: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    scheduler_batch_size: int = int(os.getenv("RROS_SCHEDULER_BATCH_SIZE", "500"))
    oauth_refresh_skew_seconds: int = int(os.getenv("RROS_OAUTH_REFRESH_SKEW_SECONDS", "180"))
    sync_interval_seconds: int = int(os.getenv("RROS_SYNC_INTERVAL_SECONDS", "300"))


settings = Settings()
