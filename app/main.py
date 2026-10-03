import hashlib
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import engine, get_db, init_db
from .integrations import PROVIDERS, exchange_code, oauth_url, provider as get_provider, public_catalog, refresh_token, test_connection, validate_remote_url
from .integration_auth import _with_expiry
from .models import *
from .secret_store import decrypt_json, encrypt_json
from .agents import AGENT_DEFS, ensure_definitions, run_agent_pipeline, record_feedback, record_outcome
from .connectors import ADAPTERS, ConnectorError, adapter_for
from .queue import enqueue_job
from .runtime import SYNC_RESOURCES, adapter_contract
from .scheduler import enqueue_due_syncs
from .security import ACCESS_COOKIE, CSRF_COOKIE, clear_auth_cookies, current_user, hash_password, issue_token, require_role, set_auth_cookies, verify_password
from .services import TERMINAL, add_audit, add_event, attribute_transaction, canonical_event, detect_for_org, dashboard as build_dashboard, jdump, transition, money
from .safety import validate_recovery_action
from .compliance import validate_communication_compliance
from .platform import PLAN_CATALOG, PlatformError, anonymise_customer, customer_export, feature_enabled, global_search, metrics

BASE = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(BASE)
FRONTEND_DIST = os.path.join(PROJECT_ROOT, "frontend", "dist")
ROLES = {"owner", "admin", "manager", "agent", "analyst", "member", "viewer"}
CHANNELS = {"sms", "email", "whatsapp", "voice"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_login_attempts: dict[str, list[float]] = {}
_api_attempts: dict[str, list[float]] = {}
_redis_client = None
REQUIRED_TABLES = (
    "organizations", "users", "locations", "customers", "leads", "appointments",
    "transactions", "payments", "invoices", "reviews", "channel_consents",
    "revenue_opportunities", "recovery_plans", "recovery_actions", "recovery_approvals",
    "attribution_links", "audit_logs", "integrations", "integration_events",
    "integration_sync_states", "connector_actions", "integration_oauth_states", "webhook_events", "marketing_spend",
    "review_responses", "api_keys", "service_jobs", "agent_definitions", "agent_runs", "agent_handoffs", "agent_memory", "agent_outcomes", "agent_feedback",
    "integration_jobs", "connector_rate_states", "connector_receipts", "business_policies",
    "customer_identities", "communication_events", "call_events", "recovery_playbooks", "billing_sessions",
)

def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

def _issue_secret() -> str:
    return secrets.token_urlsafe(32)


def _issue_api_key() -> str:
    return "rros_" + secrets.token_urlsafe(40)


def _api_key_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _require_api_key(db: Session, raw: Optional[str], required_scope: str):
    if not raw or len(raw) < 20:
        raise HTTPException(401, "API key required")
    key_hash = _api_key_hash(raw)
    try:
        if settings.database_url.startswith("postgresql"):
            row = db.execute(
                text("select * from rros_api_key_lookup(:key_hash)"),
                {"key_hash": key_hash},
            ).mappings().first()
        else:
            row = db.execute(
                select(ApiKey.id, ApiKey.organization_id, ApiKey.scopes_json, ApiKey.active, ApiKey.expires_at)
                .where(ApiKey.key_hash == key_hash)
            ).mappings().first()
    except Exception as exc:
        raise HTTPException(503, "API key service unavailable") from exc
    if not row or not row["active"]:
        raise HTTPException(401, "Invalid API key")
    expires_at = row["expires_at"]
    if expires_at and expires_at <= datetime.now(timezone.utc):
        raise HTTPException(401, "API key has expired")
    if settings.database_url.startswith("postgresql"):
        db.execute(text("select set_config('app.current_org', :org, true)"), {"org": row["organization_id"]})
    key_row = db.scalar(select(ApiKey).where(ApiKey.id == row["id"], ApiKey.organization_id == row["organization_id"]))
    if key_row:
        last_used = key_row.last_used_at
        now = datetime.now(timezone.utc)
        last_used = last_used.replace(tzinfo=timezone.utc) if last_used and last_used.tzinfo is None else last_used
        if not last_used or (now - last_used).total_seconds() >= 300:
            key_row.last_used_at = now
            db.flush()
    scopes_value = row["scopes_json"]
    if isinstance(scopes_value, str):
        try:
            scopes = json.loads(scopes_value or "[]")
        except json.JSONDecodeError:
            scopes = []
    elif isinstance(scopes_value, list):
        scopes = scopes_value
    else:
        scopes = []
    if required_scope not in scopes:
        raise HTTPException(403, "API key does not have the required scope")
    return row


def _missing_schema_tables() -> list[str]:
    missing = []
    try:
        with engine.connect() as conn:
            for name in REQUIRED_TABLES:
                if settings.database_url.startswith("postgresql"):
                    exists = conn.execute(text("select to_regclass(:name) is not null"), {"name": f"public.{name}"}).scalar()
                else:
                    exists = conn.execute(text("select count(*) from sqlite_master where type='table' and name=:name"), {"name": name}).scalar()
                if not exists:
                    missing.append(name)
    except Exception:
        return list(REQUIRED_TABLES)
    return missing

def verify_schema_ready() -> None:
    missing = _missing_schema_tables()
    if missing:
        raise RuntimeError("Database schema is incomplete. Missing tables: " + ", ".join(missing) + ". Apply migrations 001_v2_production.sql through the latest numbered migration before starting production.")

def _rate_limit_key(key: str) -> str:
    return "rros:rl:" + hashlib.sha256(key.encode()).hexdigest()

def _redis():
    global _redis_client
    if not settings.redis_url:
        return None
    if _redis_client is None:
        try:
            import redis
            _redis_client = redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
                health_check_interval=30,
            )
        except Exception as exc:
            raise RuntimeError("Redis client is unavailable") from exc
    return _redis_client


def verify_database_role_security():
    if not settings.database_url.startswith("postgresql"):
        return
    with engine.connect() as conn:
        role = conn.execute(text("select current_user, rolbypassrls from pg_roles where rolname=current_user")).first()
        if not role:
            raise RuntimeError("Could not inspect the PostgreSQL runtime role")
        current_user, bypass_rls = role
        if bypass_rls:
            raise RuntimeError("Production runtime DB role must not have BYPASSRLS")
        tenant_table_count = conn.execute(text("""
            select count(*) from pg_class c
            join pg_namespace n on n.oid=c.relnamespace
            join pg_roles r on r.oid=c.relowner
            where n.nspname='public' and c.relkind='r'
              and c.relname = any(:tables) and r.rolname=current_user
        """), {"tables": list(REQUIRED_TABLES)}).scalar() or 0
        if tenant_table_count:
            raise RuntimeError("Production runtime DB role must not own application tables; use a separate migration/schema-owner role")
        rls_missing = conn.execute(text("""
            select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
            where n.nspname='public' and c.relname = any(:tables) and c.relrowsecurity=false
        """), {"tables": list(REQUIRED_TABLES)}).scalar() or 0
        if rls_missing:
            raise RuntimeError("One or more tenant tables are missing Row Level Security")
        for function_name in ("public.rros_auth_lookup(text,text)", "public.rros_api_key_lookup(text)"):
            granted = conn.execute(text("select has_function_privilege(current_user, :fn, 'EXECUTE')"), {"fn": function_name}).scalar()
            if not granted:
                raise RuntimeError(f"Production runtime role lacks EXECUTE on {function_name}")


def validate_production_config():
    if settings.app_env != "production":
        return
    if settings.database_url.startswith("sqlite"):
        raise RuntimeError("Production requires PostgreSQL; SQLite is development-only")
    if settings.auto_create_schema:
        raise RuntimeError("AUTO_CREATE_SCHEMA must be false in production; use reviewed migrations")
    if settings.seed_demo:
        raise RuntimeError("SEED_DEMO must be false in production")
    if settings.app_env == "production" and not settings.public_base_url.lower().startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL must use HTTPS in production")
    if len(settings.jwt_secret) < 32 or "development-only" in settings.jwt_secret:
        raise RuntimeError("JWT_SECRET must be replaced with a strong production secret")
    if len(settings.integration_encryption_key) < 32:
        raise RuntimeError("INTEGRATION_ENCRYPTION_KEY must be at least 32 characters in production")
    if not 15 <= settings.jwt_exp_minutes <= 1440:
        raise RuntimeError("JWT_EXP_MINUTES must be between 15 and 1440 in production")
    verify_database_role_security()
    if settings.live_actions:
        verified = {item.strip() for item in settings.verified_live_adapters.split(",") if item.strip()}
        unknown = verified - set(ADAPTERS)
        if not verified:
            raise RuntimeError("VERIFIED_LIVE_ADAPTERS must name certified provider adapters before LIVE_ACTIONS can be enabled")
        if unknown:
            raise RuntimeError("VERIFIED_LIVE_ADAPTERS contains unknown adapters: " + ", ".join(sorted(unknown)))
    if settings.allowed_hosts.strip() == "*":
        raise RuntimeError("ALLOWED_HOSTS must be explicitly configured in production")
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is required in production for distributed auth rate limiting")


@asynccontextmanager
async def lifespan(app):
    validate_production_config()
    init_db()
    if settings.app_env == "production":
        verify_schema_ready()
        verify_database_role_security()
    if settings.seed_demo:
        seed_demo()
    yield


app = FastAPI(
    title="Revenue Recovery OS",
    version=settings.app_version,
    description="Multi-tenant revenue leakage detection, recovery orchestration and attributable revenue intelligence.",
    lifespan=lifespan,
)
if settings.allowed_hosts.strip() != "*":
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in settings.allowed_hosts.split(",") if h.strip()])

STATIC_DIR = os.path.join(PROJECT_ROOT, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
ASSET_DIR = os.path.join(FRONTEND_DIST, "assets") if os.path.isdir(os.path.join(FRONTEND_DIST, "assets")) else os.path.join(STATIC_DIR, "assets")
if os.path.isdir(ASSET_DIR):
    app.mount("/assets", StaticFiles(directory=ASSET_DIR), name="frontend-assets")


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)
    organization_slug: Optional[str] = Field(default=None, max_length=120)


class Register(BaseModel):
    organization_name: str = Field(min_length=2, max_length=200)
    organization_slug: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9-]+$")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=200)


class CreateUser(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=200)
    role: str = Field(default="member", pattern="^(admin|manager|agent|analyst|member|viewer)$")


class PlanRequest(BaseModel):
    channel: str = Field(default="sms", pattern="^(sms|email|whatsapp|voice)$")
    message: Optional[str] = Field(default=None, max_length=5000)


class RecoveryRequest(PlanRequest):
    approval: bool = False
    idempotency_key: str = Field(min_length=8, max_length=255)


class IngestEvent(BaseModel):
    type: str = Field(min_length=1, max_length=120)
    external_id: Optional[str] = Field(default=None, max_length=255)
    occurred_at: datetime | None = None
    data: dict = Field(default_factory=dict)


class ConsentRequest(BaseModel):
    customer_id: str
    channel: str
    allowed: bool
    source: str = Field(default="client", max_length=100)


class ManualIntegrationRequest(BaseModel):
    credentials: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)


class IntegrationConfigRequest(BaseModel):
    config: dict = Field(default_factory=dict)


class MarketingSpendRequest(BaseModel):
    source: str = Field(min_length=1, max_length=120)
    amount: Decimal = Field(ge=0)
    period_start: date
    period_end: date
    location_id: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=2000)


class AssignRequest(BaseModel):
    user_id: Optional[str] = None


class BulkOpportunityRequest(BaseModel):
    opportunity_ids: list[str] = Field(min_length=1, max_length=100)
    action: str = Field(pattern="^(dismiss|assign)$")
    user_id: Optional[str] = None


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    expires_at: datetime | None = None


class ReviewDraftRequest(BaseModel):
    draft_text: Optional[str] = Field(default=None, max_length=5000)


class PlaybookRequest(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2000)
    trigger_type: str = Field(min_length=2, max_length=80)
    conditions: dict = Field(default_factory=dict)
    steps: list[dict] = Field(default_factory=list, max_length=20)


class PolicyRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    policy_type: str = Field(default="ai_governance", pattern="^(ai_governance|compliance|safety|communication|financial)$")
    scope_type: str = Field(default="organization", pattern="^(organization|department|agent_type|channel|customer_segment)$")
    scope_id: Optional[str] = Field(default=None, max_length=64)
    governance_level: int = Field(default=2, ge=1, le=5)
    config: dict = Field(default_factory=dict)
    severity: str = Field(default="medium", pattern="^(low|medium|high|critical)$")
    enforcement_mode: str = Field(default="block", pattern="^(block|warn|require_approval|log_only)$")


class BillingCheckoutRequest(BaseModel):
    plan_code: str = Field(pattern="^(starter|growth|agency|enterprise)$")
    success_url: Optional[str] = Field(default=None, max_length=2000)
    cancel_url: Optional[str] = Field(default=None, max_length=2000)


class PolicyUpdateRequest(BaseModel):
    active: Optional[bool] = None
    config: Optional[dict] = None
    governance_level: Optional[int] = Field(default=None, ge=1, le=5)
    enforcement_mode: Optional[str] = Field(default=None, pattern="^(block|warn|require_approval|log_only)$")


@app.middleware("http")
async def request_hardening(request: Request, call_next):
    started = time.monotonic()
    if settings.app_env == "production" and request.url.path.startswith("/api/"):
        client_ip = request.client.host if request.client else "unknown"
        principal = request.headers.get("Authorization", "anonymous")[:32]
        if api_rate_limited(f"{client_ip}:{principal}:{request.url.path}"):
            return Response(content=json.dumps({"error": {"code": "RATE_LIMITED", "message": "Too many requests"}}), status_code=429, media_type="application/json")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_body_bytes:
                return Response(content="Request body too large", status_code=413)
        except ValueError:
            return Response(content="Invalid content length", status_code=400)

    if request.method not in SAFE_METHODS and request.url.path.startswith("/api/") and request.cookies.get(ACCESS_COOKIE) and not request.headers.get("Authorization"):
        expected = request.cookies.get(CSRF_COOKIE)
        supplied = request.headers.get("X-CSRF-Token")
        exempt = request.url.path in {"/api/auth/login", "/api/auth/register", "/api/billing/stripe/webhook"} or request.url.path.startswith("/api/ingest/")
        if not exempt and (not expected or not supplied or not hmac.compare_digest(expected, supplied)):
            return Response(content="CSRF validation failed", status_code=403)

    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    response = await call_next(request)
    metrics.observe("http_requests", (time.monotonic() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    if settings.app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def normalize_email(value: str) -> str:
    return value.strip().lower()


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def decimal_float(value) -> float:
    return round(float(value or 0), 2)


def login_rate_limited(key: str, limit: Optional[int] = None) -> bool:
    limit = settings.login_rate_limit_per_minute if limit is None else limit
    r = _redis()
    if r is not None:
        try:
            value = r.get(_rate_limit_key(key))
            return int(value or 0) >= limit
        except Exception as exc:
            if settings.app_env == "production":
                raise HTTPException(503, "Authentication rate-limit service unavailable") from exc
            return False
    now = time.time()
    hits = [t for t in _login_attempts.get(key, []) if now - t < 60.0]
    _login_attempts[key] = hits
    return len(hits) >= limit


def record_login_failure(key: str, limit: Optional[int] = None):
    limit = settings.login_rate_limit_per_minute if limit is None else limit
    r = _redis()
    if r is not None:
        try:
            k = _rate_limit_key(key)
            pipe = r.pipeline()
            pipe.incr(k)
            pipe.expire(k, 60)
            pipe.execute()
            return
        except Exception as exc:
            if settings.app_env == "production":
                raise HTTPException(503, "Authentication rate-limit service unavailable") from exc
            return
    now = time.time()
    hits = [t for t in _login_attempts.get(key, []) if now - t < 60.0]
    if len(hits) < max(limit, 1) * 2:
        hits.append(now)
    _login_attempts[key] = hits


def api_rate_limited(key: str, limit: Optional[int] = None) -> bool:
    limit = settings.api_rate_limit_per_minute if limit is None else limit
    now = time.time()
    hits = [t for t in _api_attempts.get(key, []) if now - t < 60.0]
    hits.append(now)
    _api_attempts[key] = hits[-max(limit * 2, 1):]
    return len(hits) > limit


def seed_demo():
    db = __import__("app.db", fromlist=["SessionLocal"]).SessionLocal()
    if db.scalar(select(func.count(Organization.id))) > 0:
        db.close()
        return
    t = datetime.now(timezone.utc)
    org = Organization(id="org_demo", name="Acme Home Services", slug="acme-demo")
    loc = Location(id="loc_demo", organization_id=org.id, name="Demo Location")
    usr = User(id="usr_demo", organization_id=org.id, email="owner@example.com", password_hash=hash_password("demo-password"), role="owner")
    customers = [
        Customer(id=f"cus_{i}", organization_id=org.id, location_id=loc.id, name=n, email=e, phone=f"+1555000{i}", created_at=t)
        for i, (n, e) in enumerate([("John Carter", "john@example.com"), ("Sarah Lee", "sarah@example.com"), ("Mike Ross", "mike@example.com"), ("Emma Stone", "emma@example.com")], 1)
    ]
    # Insert demo seed rows in explicit dependency order. The models use composite
    # tenant foreign keys, so relying on SQLAlchemy's flush ordering without ORM
    # relationships can violate SQLite/PostgreSQL FK enforcement.
    db.add(org); db.flush()
    db.add(loc); db.flush()
    db.add(usr); db.add_all(customers); db.flush()
    db.add_all([
        Lead(id="lead_1", organization_id=org.id, customer_id="cus_1", source="Google", status="stalled", value=Decimal("4200"), created_at=t - timedelta(days=6), last_contact_at=t - timedelta(days=5), quote_sent_at=t - timedelta(days=6)),
        Lead(id="lead_2", organization_id=org.id, customer_id="cus_2", source="Website", status="uncontacted", value=Decimal("1800"), created_at=t - timedelta(days=2)),
        Lead(id="lead_3", organization_id=org.id, customer_id="cus_3", source="Referral", status="lost", value=Decimal("3000"), created_at=t - timedelta(days=20)),
    ])
    db.flush()
    db.add_all([
        Appointment(id="apt_1", organization_id=org.id, customer_id="cus_2", lead_id="lead_2", status="cancelled", value=Decimal("900"), scheduled_at=t - timedelta(days=3), cancelled_at=t - timedelta(days=3)),
        Appointment(id="apt_2", organization_id=org.id, customer_id="cus_3", status="no_show", value=Decimal("700"), scheduled_at=t - timedelta(days=2)),
    ])
    db.add(Payment(id="pay_1", organization_id=org.id, customer_id="cus_4", amount=Decimal("650"), status="failed", failed_at=t - timedelta(days=1)))
    db.add_all([
        Transaction(id="tx_1", organization_id=org.id, customer_id="cus_1", amount=Decimal("1350"), status="paid", paid_at=t - timedelta(days=91)),
        Transaction(id="tx_0", organization_id=org.id, customer_id="cus_1", amount=Decimal("1200"), status="paid", paid_at=t - timedelta(days=182)),
    ])
    db.add(Review(id="rev_1", organization_id=org.id, customer_id="cus_2", source="google", rating=2, text="Waited 45 minutes even though I booked online.", topic="waiting_time", severity="medium", created_at=t - timedelta(days=7)))
    db.add(ChannelConsent(id="consent_1", organization_id=org.id, customer_id="cus_2", channel="sms", allowed=True, source="demo"))
    db.commit()
    detect_for_org(db, org.id)
    db.close()


def scoped(db, model, obj_id, org_id):
    if not obj_id:
        return None
    return db.scalar(select(model).where(model.id == obj_id, model.organization_id == org_id))


def serialize_opp(o):
    return {
        "id": o.id,
        "type": o.type,
        "status": o.status,
        "reason": o.reason,
        "severity": o.severity,
        "estimated_value": decimal_float(o.estimated_value),
        "probability": float(o.probability),
        "expected_value": decimal_float(o.expected_value),
        "confidence": float(o.confidence),
        "urgency": float(o.urgency),
        "recoverability": float(o.recoverability),
        "customer_importance": float(o.customer_importance),
        "priority_score": float(o.priority_score),
        "recovered_amount": decimal_float(o.recovered_amount),
        "customer_id": o.customer_id,
        "lead_id": o.lead_id,
        "assigned_user_id": o.assigned_user_id,
        "recommended_action": o.recommended_action,
        "evidence": json.loads(o.evidence_json or "[]"),
        "attribution_status": o.attribution_status,
        "detected_at": o.detected_at.isoformat(),
        "version": o.version,
    }


@app.get("/")
def root():
    react_index = os.path.join(FRONTEND_DIST, "index.html")
    return FileResponse(react_index if os.path.isfile(react_index) else os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "service": "revenue-recovery-os", "version": settings.app_version, "live_actions": settings.live_actions, "database": "reachable"}
    except Exception:
        raise HTTPException(503, "Database is unavailable")


@app.get("/api/v1/health")
def health_v1():
    return health()


@app.get("/api/metrics")
def metrics_endpoint(user: User = Depends(require_role("owner", "admin"))):
    return {"metrics": metrics.snapshot(), "version": settings.app_version}


@app.get("/api/features")
def feature_catalog(user: User = Depends(current_user)):
    names = {"global_search": True, "privacy_export": True, "privacy_anonymisation": True, "governed_actions": True, "review_analysis": True}
    return {"features": {name: feature_enabled(name, default=value) for name, value in names.items()}}


@app.get("/api/search")
def search(q: str = Query(..., min_length=2, max_length=120), limit: int = Query(25, ge=1, le=100), user: User = Depends(current_user), db: Session = Depends(get_db)):
    started = time.monotonic()
    result = global_search(db, user.organization_id, q, limit)
    metrics.observe("search", (time.monotonic() - started) * 1000)
    return {"query": q, "results": result, "count": len(result)}


@app.get("/api/v1/search")
def search_v1(q: str = Query(..., min_length=2, max_length=120), limit: int = Query(25, ge=1, le=100), user: User = Depends(current_user), db: Session = Depends(get_db)):
    return search(q=q, limit=limit, user=user, db=db)


@app.get("/api/policies")
def list_policies(user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    rows = db.scalars(select(BusinessPolicy).where(BusinessPolicy.organization_id == user.organization_id).order_by(BusinessPolicy.name)).all()
    return [{"id": x.id, "name": x.name, "description": x.description, "policy_type": x.policy_type, "scope_type": x.scope_type, "scope_id": x.scope_id, "governance_level": x.governance_level, "config": json.loads(x.config_json or "{}"), "severity": x.severity, "enforcement_mode": x.enforcement_mode, "active": x.active, "version": x.version} for x in rows]


@app.post("/api/policies")
def create_policy(req: PolicyRequest, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    if req.scope_type == "organization" and req.scope_id:
        raise HTTPException(422, "Organization policies cannot have a scope_id")
    row = BusinessPolicy(id=make_id("pol"), organization_id=user.organization_id, name=req.name.strip(), description=req.description, policy_type=req.policy_type, scope_type=req.scope_type, scope_id=req.scope_id, governance_level=req.governance_level, config_json=jdump(req.config), severity=req.severity, enforcement_mode=req.enforcement_mode)
    db.add(row)
    add_audit(db, user.organization_id, "business_policy_created", "business_policy", row.id, {"name": row.name, "governance_level": row.governance_level}, user.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A policy with this name or scope already exists")
    return {"id": row.id, "status": "created"}


@app.patch("/api/policies/{policy_id}")
def update_policy(policy_id: str, req: PolicyUpdateRequest, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    row = db.scalar(select(BusinessPolicy).where(BusinessPolicy.organization_id == user.organization_id, BusinessPolicy.id == policy_id))
    if not row:
        raise HTTPException(404, "Policy not found")
    changes = req.model_dump(exclude_unset=True)
    if "config" in changes:
        row.config_json = jdump(changes.pop("config"))
    for key, value in changes.items():
        setattr(row, key, value)
    row.version += 1
    row.updated_at = datetime.now(timezone.utc)
    add_audit(db, user.organization_id, "business_policy_updated", "business_policy", row.id, {"version": row.version}, user.id)
    db.commit()
    return {"id": row.id, "version": row.version, "active": row.active}


@app.get("/api/billing/plan")
def billing_plan(user: User = Depends(current_user), db: Session = Depends(get_db)):
    org = db.scalar(select(Organization).where(Organization.id == user.organization_id))
    plan = PLAN_CATALOG.get(org.plan_code, PLAN_CATALOG["free_trial"])
    stripe = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == "stripe"))
    stripe_config = json.loads(stripe.config_json or "{}") if stripe and stripe.config_json else {}
    configured_prices = isinstance(stripe_config.get("price_ids"), dict) and bool(stripe_config.get("price_ids"))
    env_prices = any(os.getenv(f"RROS_STRIPE_PRICE_{code.upper()}") for code in ("starter", "growth", "agency", "enterprise"))
    billing_available = bool(stripe and stripe.credentials_ciphertext and (configured_prices or env_prices))
    return {"plan_code": org.plan_code, "subscription_status": org.subscription_status, "monthly_opportunity_limit": org.monthly_opportunity_limit, "plan": plan, "catalog": PLAN_CATALOG, "billing_available": billing_available, "message": "Stripe Checkout is ready for configured plans." if billing_available else "Configure Stripe credentials and price IDs to enable checkout."}


@app.post("/api/billing/checkout")
async def billing_checkout(req: BillingCheckoutRequest, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    """Create a real Stripe Checkout subscription session using tenant Stripe credentials."""
    import httpx
    org = db.scalar(select(Organization).where(Organization.id == user.organization_id))
    integration = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == "stripe"))
    if not integration or not integration.credentials_ciphertext:
        raise HTTPException(503, "Configure the tenant Stripe integration before starting checkout")
    try:
        credentials = decrypt_json(integration.credentials_ciphertext)
        config = json.loads(integration.config_json or "{}")
    except Exception as exc:
        raise HTTPException(503, "Stripe credentials are unavailable") from exc
    price_ids = config.get("price_ids") if isinstance(config.get("price_ids"), dict) else {}
    price_id = price_ids.get(req.plan_code) or os.getenv(f"RROS_STRIPE_PRICE_{req.plan_code.upper()}")
    if not price_id:
        raise HTTPException(503, f"No Stripe price is configured for plan {req.plan_code}")
    public = urlparse(settings.public_base_url)
    def return_url(value: Optional[str], suffix: str) -> str:
        candidate = value or settings.public_base_url.rstrip("/") + suffix
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != public.netloc:
            raise HTTPException(422, "Checkout return URLs must use the configured public application origin")
        return candidate
    form = {
        "mode": "subscription",
        "line_items[0][price]": str(price_id),
        "line_items[0][quantity]": "1",
        "success_url": return_url(req.success_url, "/billing/success?session_id={CHECKOUT_SESSION_ID}"),
        "cancel_url": return_url(req.cancel_url, "/billing/cancelled"),
        "client_reference_id": org.id,
        "metadata[organization_id]": org.id,
        "metadata[plan_code]": req.plan_code,
        "subscription_data[metadata][organization_id]": org.id,
        "subscription_data[metadata][plan_code]": req.plan_code,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post("https://api.stripe.com/v1/checkout/sessions", data=form, auth=(str(credentials.get("api_key")), ""))
        payload = response.json()
        if response.status_code >= 400:
            raise HTTPException(502, {"code": "PROVIDER_ERROR", "message": "Stripe rejected checkout creation", "provider_status": response.status_code})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, {"code": "PROVIDER_ERROR", "message": "Stripe checkout is temporarily unavailable"}) from exc
    session_id = payload.get("id")
    checkout_url = payload.get("url")
    if not session_id or not checkout_url:
        raise HTTPException(502, "Stripe returned an incomplete checkout session")
    row = BillingSession(id=make_id("bill"), organization_id=org.id, provider_session_id=session_id, plan_code=req.plan_code, checkout_url=checkout_url, metadata_json=jdump({"price_id": price_id, "requested_by": user.id}))
    db.add(row)
    add_audit(db, org.id, "billing_checkout_created", "billing_session", row.id, {"plan_code": req.plan_code, "provider_session_id": session_id}, user.id)
    db.commit()
    return {"status": "created", "session_id": session_id, "checkout_url": checkout_url, "plan_code": req.plan_code}


@app.post("/api/billing/stripe/webhook")
async def stripe_billing_webhook(request: Request, db: Session = Depends(get_db)):
    """Verify and reconcile Stripe checkout/subscription events idempotently."""
    secret = settings.stripe_webhook_secret
    if not secret:
        raise HTTPException(503, "Stripe webhook secret is not configured")
    raw = await request.body()
    signature = request.headers.get("Stripe-Signature", "")
    parts = dict(item.split("=", 1) for item in signature.split(",") if "=" in item)
    timestamp = parts.get("t")
    received = parts.get("v1")
    if not timestamp or not received:
        raise HTTPException(401, "Invalid Stripe signature")
    try:
        timestamp_int = int(timestamp)
    except ValueError:
        raise HTTPException(401, "Invalid Stripe signature")
    if abs(int(time.time()) - timestamp_int) > 300:
        raise HTTPException(401, "Expired Stripe signature")
    expected = hmac.new(secret.encode(), f"{timestamp}.{raw.decode()}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise HTTPException(401, "Invalid Stripe signature")
    try:
        event = json.loads(raw.decode())
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid Stripe event JSON")
    event_type = str(event.get("type") or "")
    obj = ((event.get("data") or {}).get("object") or {})
    metadata = obj.get("metadata") or {}
    org_id = metadata.get("organization_id") or obj.get("client_reference_id")
    if event_type == "checkout.session.completed" and org_id:
        row = db.scalar(select(BillingSession).where(BillingSession.provider_session_id == obj.get("id")).with_for_update())
        org = db.scalar(select(Organization).where(Organization.id == org_id))
        if row and org and row.status != "completed":
            row.status = "completed"
            row.completed_at = datetime.now(timezone.utc)
            row.provider_customer_id = obj.get("customer")
            row.provider_subscription_id = obj.get("subscription")
            org.plan_code = row.plan_code
            org.monthly_opportunity_limit = int(PLAN_CATALOG.get(row.plan_code, {}).get("monthly_opportunity_limit", org.monthly_opportunity_limit))
            org.subscription_status = "active"
            add_audit(db, org.id, "billing_checkout_completed", "billing_session", row.id, {"plan_code": row.plan_code, "provider_subscription_id": row.provider_subscription_id})
            db.commit()
        return {"status": "accepted", "event_type": event_type, "idempotent": bool(row and row.status == "completed")}
    if event_type in {"customer.subscription.deleted", "customer.subscription.updated"} and org_id:
        org = db.scalar(select(Organization).where(Organization.id == org_id))
        if org:
            org.subscription_status = "cancelled" if event_type.endswith("deleted") else str(obj.get("status") or org.subscription_status)
            add_audit(db, org.id, "billing_subscription_reconciled", "organization", org.id, {"event_type": event_type, "subscription_id": obj.get("id")})
            db.commit()
    return {"status": "accepted", "event_type": event_type, "handled": bool(org_id)}


@app.get("/api/billing/sessions")
def billing_sessions(user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    rows = db.scalars(select(BillingSession).where(BillingSession.organization_id == user.organization_id).order_by(BillingSession.created_at.desc()).limit(50)).all()
    return [{"id": row.id, "plan_code": row.plan_code, "status": row.status, "provider_session_id": row.provider_session_id, "created_at": row.created_at.isoformat(), "completed_at": row.completed_at.isoformat() if row.completed_at else None} for row in rows]


@app.get("/api/privacy/customers/{customer_id}/export")
def export_customer(customer_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        return customer_export(db, user.organization_id, customer_id)
    except PlatformError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})


@app.post("/api/privacy/customers/{customer_id}/delete")
def delete_customer(customer_id: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    try:
        customer = anonymise_customer(db, user.organization_id, customer_id)
    except PlatformError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    add_audit(db, user.organization_id, "customer_anonymised", "customer", customer_id, {"retained_for_financial_compliance": True}, user.id)
    db.commit()
    return {"status": "anonymised", "customer_id": customer_id}


@app.get("/api/ready")
def ready():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        missing = _missing_schema_tables()
        if missing:
            raise HTTPException(503, "Database schema is incomplete")
        return {"ready": True, "schema": "ready"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Service not ready")


@app.post("/api/auth/register")
def register(req: Register, request: Request, response: Response, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    register_key = f"register:{client_ip}"
    if login_rate_limited(register_key, limit=settings.register_rate_limit_per_minute):
        raise HTTPException(429, "Too many registration attempts; try again later")
    record_login_failure(register_key, limit=settings.register_rate_limit_per_minute)
    email = normalize_email(req.email)
    password_hash = hash_password(req.password)
    webhook_secret = _issue_secret()
    webhook_secret_hash = _hash_secret(webhook_secret)
    try:
        if settings.database_url.startswith("postgresql"):
            row = db.execute(
                text("select * from rros_register_tenant(:name,:slug,:email,:password_hash)"),
                {"name": req.organization_name.strip(), "slug": req.organization_slug, "email": email, "password_hash": password_hash},
            ).mappings().one()
            db.execute(text("select set_config('app.current_org', :org, true)"), {"org": row["organization_id"]})
            user = db.scalar(select(User).where(User.id == row["user_id"], User.organization_id == row["organization_id"]))
            org = db.scalar(select(Organization).where(Organization.id == row["organization_id"]))
            org.webhook_secret_hash = webhook_secret_hash
            org.webhook_secret_version = 1
            db.commit()
        else:
            if db.scalar(select(Organization).where(Organization.slug == req.organization_slug)):
                raise HTTPException(409, "Organization slug already exists")
            org = Organization(id=make_id("org"), name=req.organization_name.strip(), slug=req.organization_slug, active=True, webhook_secret_hash=webhook_secret_hash, webhook_secret_version=1)
            user = User(id=make_id("usr"), organization_id=org.id, email=email, password_hash=password_hash, role="owner")
            db.add_all([org, user])
            db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Organization or user already exists")
    except Exception as exc:
        db.rollback()
        if settings.database_url.startswith("postgresql") and "rros_register_tenant" in str(exc):
            raise HTTPException(503, "Tenant registration is not available until the production database migration is installed")
        raise
    token = issue_token(user)
    set_auth_cookies(response, token)
    return {"access_token": token, "token_type": "bearer", "organization_id": org.id, "organization_slug": org.slug, "role": user.role, "webhook_secret": webhook_secret}


@app.post("/api/auth/login")
def login(req: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    email = normalize_email(req.email)
    client_ip = request.client.host if request.client else "unknown"
    account_key = f"account:{client_ip}:{email}"
    ip_key = f"ip:{client_ip}"
    if login_rate_limited(account_key) or login_rate_limited(ip_key):
        raise HTTPException(429, "Too many login attempts; try again later")
    if settings.database_url.startswith("postgresql"):
        rows = db.execute(text("select * from rros_auth_lookup(:email,:slug)"), {"email": email, "slug": req.organization_slug}).mappings().all()
        if len(rows) != 1 or not verify_password(req.password, rows[0]["password_hash"]):
            record_login_failure(account_key)
            record_login_failure(ip_key)
            raise HTTPException(401, "Invalid credentials or tenant")
        row = rows[0]
        db.execute(text("select set_config('app.current_org', :org, true)"), {"org": row["organization_id"]})
        u = db.scalar(select(User).where(User.id == row["user_id"], User.organization_id == row["organization_id"]))
    else:
        users = db.scalars(select(User).where(User.email == email, User.active == True)).all()
        if req.organization_slug:
            org = db.scalar(select(Organization).where(Organization.slug == req.organization_slug, Organization.active == True))
            users = [u for u in users if org and u.organization_id == org.id]
        if len(users) != 1 or not verify_password(req.password, users[0].password_hash):
            record_login_failure(account_key)
            record_login_failure(ip_key)
            raise HTTPException(401, "Invalid credentials or tenant")
        u = users[0]
    parts = u.password_hash.split("$", 3)
    needs_rehash = not (len(parts) >= 2 and parts[0] == "pbkdf2_sha256" and parts[1] == "310000")
    if needs_rehash:
        u.password_hash = hash_password(req.password)
        db.commit()
    token = issue_token(u)
    set_auth_cookies(response, token)
    org = db.scalar(select(Organization).where(Organization.id == u.organization_id))
    return {"access_token": token, "token_type": "bearer", "organization_id": u.organization_id, "organization_slug": org.slug if org else None, "role": u.role}


@app.post("/api/auth/logout")
def logout(response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.token_version += 1
    db.commit()
    clear_auth_cookies(response)
    return {"status": "logged_out"}


@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    return {"id": user.id, "email": user.email, "organization_id": user.organization_id, "organization_slug": org.slug if org else None, "role": user.role}


@app.post("/api/users")
def create_user(req: CreateUser, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    email = normalize_email(req.email)
    if db.scalar(select(User).where(User.organization_id == user.organization_id, User.email == email)):
        raise HTTPException(409, "User already exists in this organization")
    if user.role == "admin" and req.role == "admin":
        # Admins can manage members/managers/viewers, but cannot mint peer admins.
        raise HTTPException(403, "Only the organization owner can create another admin")
    u = User(id=make_id("usr"), organization_id=user.organization_id, email=email, password_hash=hash_password(req.password), role=req.role)
    db.add(u)
    add_audit(db, user.organization_id, "user_created", "user", u.id, {"role": u.role}, user.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "User already exists")
    return {"id": u.id, "email": u.email, "role": u.role, "active": u.active}


@app.get("/api/users")
def list_users(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000), user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.organization_id == user.organization_id).order_by(User.email).offset(offset).limit(limit)).all()
    return [{"id": u.id, "email": u.email, "role": u.role, "active": u.active} for u in rows]


@app.post("/api/users/{uid}/revoke")
def revoke_user(uid: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    target = scoped(db, User, uid, user.organization_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == user.id:
        raise HTTPException(400, "Cannot revoke your own current session")
    if target.role == "owner":
        raise HTTPException(400, "The organization owner cannot be revoked from this endpoint")
    target.active = False
    target.token_version += 1
    add_audit(db, user.organization_id, "user_revoked", "user", uid, {}, user.id)
    db.commit()
    return {"status": "revoked"}


@app.post("/api/tenant/api-keys")
def create_api_key(req: ApiKeyCreateRequest, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    name = req.name.strip()
    if db.scalar(select(ApiKey).where(ApiKey.organization_id == user.organization_id, ApiKey.name == name)):
        raise HTTPException(409, "API key name already exists")
    if req.expires_at and req.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(400, "expires_at must be in the future")
    raw = _issue_api_key()
    row = ApiKey(id=make_id("key"), organization_id=user.organization_id, name=name, key_prefix=raw[:16], key_hash=_api_key_hash(raw), scopes_json=jdump(["ingest:write"]), expires_at=req.expires_at)
    db.add(row)
    add_audit(db, user.organization_id, "api_key_created", "api_key", row.id, {"name": name, "scopes": ["ingest:write"]}, user.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "API key already exists; retry")
    return {"id": row.id, "name": row.name, "key_prefix": row.key_prefix, "api_key": raw, "scopes": ["ingest:write"], "expires_at": row.expires_at.isoformat() if row.expires_at else None, "warning": "Store this API key securely. It will not be shown again."}


@app.get("/api/tenant/api-keys")
def list_api_keys(user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    rows = db.scalars(select(ApiKey).where(ApiKey.organization_id == user.organization_id).order_by(ApiKey.created_at.desc())).all()
    return [{"id": x.id, "name": x.name, "key_prefix": x.key_prefix, "active": x.active, "scopes": json.loads(x.scopes_json or "[]"), "created_at": x.created_at.isoformat(), "last_used_at": x.last_used_at.isoformat() if x.last_used_at else None, "expires_at": x.expires_at.isoformat() if x.expires_at else None} for x in rows]


@app.post("/api/tenant/api-keys/{key_id}/revoke")
def revoke_api_key(key_id: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    row = scoped(db, ApiKey, key_id, user.organization_id)
    if not row:
        raise HTTPException(404, "API key not found")
    row.active = False
    add_audit(db, user.organization_id, "api_key_revoked", "api_key", key_id, {}, user.id)
    db.commit()
    return {"status": "revoked"}


@app.get("/api/tenant/usage")
def tenant_usage(user: User = Depends(current_user), db: Session = Depends(get_db)):
    org = db.scalar(select(Organization).where(Organization.id == user.organization_id))
    if not org:
        raise HTTPException(404, "Tenant not found")
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    opportunities_created = db.scalar(
        select(func.count(OpportunityEvent.id))
        .where(
            OpportunityEvent.organization_id == user.organization_id,
            OpportunityEvent.event_type == "opportunity_created",
            OpportunityEvent.created_at >= month_start,
        )
    ) or 0
    return {"plan": org.plan_code, "subscription_status": org.subscription_status, "monthly_opportunity_limit": org.monthly_opportunity_limit, "month_to_date_opportunities": int(opportunities_created), "remaining": max(0, int(org.monthly_opportunity_limit) - int(opportunities_created)), "utilization": round(float(opportunities_created) / max(1, org.monthly_opportunity_limit), 4)}


@app.get("/api/onboarding/status")
def onboarding_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    counts = {
        "customers": db.scalar(select(func.count(Customer.id)).where(Customer.organization_id == user.organization_id)) or 0,
        "leads": db.scalar(select(func.count(Lead.id)).where(Lead.organization_id == user.organization_id)) or 0,
        "appointments": db.scalar(select(func.count(Appointment.id)).where(Appointment.organization_id == user.organization_id)) or 0,
        "transactions": db.scalar(select(func.count(Transaction.id)).where(Transaction.organization_id == user.organization_id)) or 0,
        "reviews": db.scalar(select(func.count(Review.id)).where(Review.organization_id == user.organization_id)) or 0,
    }
    integrations = db.scalars(select(Integration).where(Integration.organization_id == user.organization_id)).all()
    connected = {x.provider for x in integrations if x.status in {"connected", "configured"}}
    steps = [
        {"key": "crm", "title": "Connect CRM or field-service system", "complete": any(p in connected for p in {"hubspot", "gohighlevel", "salesforce", "pipedrive", "zoho_crm", "servicetitan", "housecall_pro", "jobber"})},
        {"key": "reviews", "title": "Connect review source", "complete": any(p in connected for p in {"google_business_profile", "yelp"})},
        {"key": "payments", "title": "Connect payment/accounting source", "complete": any(p in connected for p in {"stripe", "square", "quickbooks", "xero"})},
        {"key": "communications", "title": "Connect a communication channel", "complete": any(p in connected for p in {"google_gmail", "microsoft_graph", "twilio", "whatsapp_twilio"})},
        {"key": "first_data", "title": "Import or ingest customer activity", "complete": any(counts.values())},
    ]
    complete = sum(x["complete"] for x in steps)
    return {"score": round(complete / len(steps) * 100), "steps": steps, "counts": counts, "connected_integrations": sorted(connected), "ready_for_detection": bool(any(counts.values()) or connected), "next": next((x["title"] for x in steps if not x["complete"]), "Run your first revenue-leak detection")}


@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return build_dashboard(db, user.organization_id)


@app.post("/api/detect")
def detect(user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    org = db.scalar(select(Organization).where(Organization.id == user.organization_id))
    if not org or not org.active:
        raise HTTPException(403, "Organization is inactive")
    if org.subscription_status in {"cancelled", "paused"}:
        raise HTTPException(402, "Revenue detection is unavailable for this subscription")
    if settings.database_url.startswith("postgresql"):
        db.execute(text("select pg_advisory_xact_lock(hashtext(:key))"), {"key": f"rros:detect:{user.organization_id}"})
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    created_this_month = db.scalar(
        select(func.count(OpportunityEvent.id))
        .where(
            OpportunityEvent.organization_id == user.organization_id,
            OpportunityEvent.event_type == "opportunity_created",
            OpportunityEvent.created_at >= month_start,
        )
    ) or 0
    remaining = max(0, int(org.monthly_opportunity_limit) - int(created_this_month))
    return detect_for_org(db, user.organization_id, max_new=remaining) | {"monthly_limit": org.monthly_opportunity_limit, "month_to_date_before_run": int(created_this_month), "remaining_before_run": remaining}


@app.get("/api/opportunities")
def opportunities(status: Optional[str] = None, type: Optional[str] = None, assigned_user_id: Optional[str] = None, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000), user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id)
    if status:
        q = q.where(RevenueOpportunity.status == status)
    if type:
        q = q.where(RevenueOpportunity.type == type)
    if assigned_user_id:
        q = q.where(RevenueOpportunity.assigned_user_id == assigned_user_id)
    rows = db.scalars(q.order_by(RevenueOpportunity.priority_score.desc(), RevenueOpportunity.detected_at.desc()).offset(offset).limit(limit)).all()
    return [serialize_opp(x) for x in rows]


@app.get("/api/opportunities/{oid}")
def opportunity(oid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = scoped(db, RevenueOpportunity, oid, user.organization_id)
    if not o:
        raise HTTPException(404, "Opportunity not found")
    events = db.scalars(select(OpportunityEvent).where(OpportunityEvent.opportunity_id == oid, OpportunityEvent.organization_id == user.organization_id).order_by(OpportunityEvent.created_at)).all()
    plans = db.scalars(select(RecoveryPlan).where(RecoveryPlan.opportunity_id == oid, RecoveryPlan.organization_id == user.organization_id).order_by(RecoveryPlan.created_at.desc())).all()
    approvals = db.scalars(select(RecoveryApproval).join(RecoveryAction, RecoveryAction.id == RecoveryApproval.action_id).where(RecoveryApproval.organization_id == user.organization_id, RecoveryAction.opportunity_id == oid)).all()
    actions = db.scalars(select(RecoveryAction).where(RecoveryAction.opportunity_id == oid, RecoveryAction.organization_id == user.organization_id).order_by(RecoveryAction.created_at.desc())).all()
    return {
        "opportunity": serialize_opp(o),
        "events": [{"type": e.event_type, "actor": e.actor, "metadata": json.loads(e.metadata_json), "created_at": e.created_at.isoformat()} for e in events],
        "plans": [{"id": p.id, "channel": p.channel, "status": p.status, "steps": json.loads(p.steps_json), "approval_required": p.approval_required} for p in plans],
        "actions": [{"id": a.id, "channel": a.channel, "status": a.status, "created_at": a.created_at.isoformat(), "executed_at": a.executed_at.isoformat() if a.executed_at else None} for a in actions],
        "approvals": [{"id": a.id, "decision": a.decision, "approved_by_user_id": a.approved_by_user_id, "policy_version": a.policy_version, "created_at": a.created_at.isoformat()} for a in approvals],
    }


@app.get("/api/opportunities/{oid}/evidence")
def opportunity_evidence(oid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = scoped(db, RevenueOpportunity, oid, user.organization_id)
    if not o:
        raise HTTPException(404, "Opportunity not found")
    return {"opportunity_id": oid, "evidence": json.loads(o.evidence_json or "[]"), "confidence": float(o.confidence), "priority": {"expected_value": decimal_float(o.expected_value), "urgency": float(o.urgency), "recoverability": float(o.recoverability), "customer_importance": float(o.customer_importance), "priority_score": float(o.priority_score)}}


@app.get("/api/opportunities/{oid}/recovery-brief")
def recovery_brief(oid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = scoped(db, RevenueOpportunity, oid, user.organization_id)
    if not o:
        raise HTTPException(404, "Opportunity not found")
    evidence = json.loads(o.evidence_json or "[]")
    safe_next = "Investigate context and confirm consent before outreach."
    if o.type in {"missed_call", "appointment_leak"}:
        safe_next = "Follow up promptly with the customer-preferred channel and offer a simple next step."
    elif o.type in {"failed_payment", "overdue_invoice"}:
        safe_next = "Use the authorized payment-recovery path; do not expose payment credentials in messages."
    elif o.type == "review_recovery":
        safe_next = "Resolve the underlying service issue first; public response should remain human-approved."
    return {"opportunity": serialize_opp(o), "brief": {"what_happened": o.reason, "why_now": f"Priority score {float(o.priority_score):.2f}", "recommended_next_step": safe_next, "evidence": evidence, "attribution_note": "Recovered revenue is recognized only after a valid attributable transaction path."}}


@app.post("/api/opportunities/{oid}/assign")
def assign(oid: str, req: AssignRequest, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    o = scoped(db, RevenueOpportunity, oid, user.organization_id)
    if not o:
        raise HTTPException(404, "Opportunity not found")
    target = None
    if req.user_id:
        target = scoped(db, User, req.user_id, user.organization_id)
        if not target or not target.active:
            raise HTTPException(400, "Assigned user is not active in this organization")
    o.assigned_user_id = target.id if target else None
    o.version += 1
    add_event(db, user.organization_id, oid, "opportunity_assigned", user.email, {"assigned_user_id": o.assigned_user_id})
    add_audit(db, user.organization_id, "opportunity_assigned", "opportunity", oid, {"assigned_user_id": o.assigned_user_id}, user.id)
    db.commit()
    return {"status": "ok", "assigned_user_id": o.assigned_user_id}


@app.post("/api/opportunities/bulk")
def bulk(req: BulkOpportunityRequest, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    rows = db.scalars(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id, RevenueOpportunity.id.in_(req.opportunity_ids))).all()
    if len(rows) != len(set(req.opportunity_ids)):
        raise HTTPException(404, "One or more opportunities were not found in this tenant")
    target = None
    if req.action == "assign":
        if not req.user_id:
            raise HTTPException(400, "user_id is required for bulk assignment")
        target = scoped(db, User, req.user_id, user.organization_id)
        if not target or not target.active:
            raise HTTPException(400, "Assigned user is not active in this organization")
    changed = 0
    for o in rows:
        if req.action == "dismiss":
            if o.status not in TERMINAL:
                transition(db, o, "dismissed", user.email)
                add_audit(db, user.organization_id, "opportunity_dismissed", "opportunity", o.id, {"bulk": True}, user.id)
                changed += 1
        else:
            o.assigned_user_id = target.id
            o.version += 1
            add_event(db, user.organization_id, o.id, "opportunity_assigned", user.email, {"assigned_user_id": target.id, "bulk": True})
            changed += 1
    db.commit()
    return {"changed": changed}


@app.post("/api/opportunities/{oid}/recovery-plan")
def plan(oid: str, req: PlanRequest, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    o = scoped(db, RevenueOpportunity, oid, user.organization_id)
    if not o:
        raise HTTPException(404, "Opportunity not found")
    if o.status in TERMINAL:
        raise HTTPException(409, "Opportunity is terminal")
    existing = db.scalar(select(RecoveryPlan).where(RecoveryPlan.organization_id == user.organization_id, RecoveryPlan.opportunity_id == oid, RecoveryPlan.status.in_(["awaiting_approval", "approved", "active"])).limit(1))
    if existing:
        return {"plan_id": existing.id, "steps": json.loads(existing.steps_json), "status": existing.status, "approval_required": existing.approval_required, "idempotent": True}
    steps = [
        {"step": 1, "action": "investigate_context"},
        {"step": 2, "action": f"prepare_{req.channel}"},
        {"step": 3, "action": "wait_and_detect_response"},
        {"step": 4, "action": "book_or_collect_payment"},
        {"step": 5, "action": "attribute_outcome"},
    ]
    p = RecoveryPlan(id=make_id("plan"), organization_id=user.organization_id, opportunity_id=oid, objective="Recover revenue", channel=req.channel, steps_json=jdump(steps), status="awaiting_approval", approval_required=True)
    db.add(p)
    if o.status == "open":
        transition(db, o, "planned", user.email)
    add_event(db, user.organization_id, oid, "recovery_plan_created", user.email, {"channel": req.channel, "approval_required": True})
    add_audit(db, user.organization_id, "recovery_plan_created", "opportunity", oid, {"channel": req.channel}, user.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(RecoveryPlan).where(
            RecoveryPlan.organization_id == user.organization_id,
            RecoveryPlan.opportunity_id == oid,
            RecoveryPlan.status.in_(["awaiting_approval", "approved", "active"]),
        ).limit(1))
        if existing:
            return {"plan_id": existing.id, "steps": json.loads(existing.steps_json), "status": existing.status, "approval_required": existing.approval_required, "idempotent": True}
        raise HTTPException(409, "Concurrent recovery-plan conflict; retry")
    return {"plan_id": p.id, "steps": steps, "status": p.status, "approval_required": True}



def _connector_action_key(org_id: str, provider: str, operation: str, args: dict) -> str:
    basis = {"org": org_id, "provider": provider, "operation": operation, "args": args}
    return "conn:" + hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()


async def _execute_connector(db: Session, user: User, provider_key: str, operation: str, args: dict, *, idempotency_key: Optional[str] = None):
    """Compatibility wrapper: all live connector execution goes through the durable queue."""
    action, integration, already = _prepare_connector_action(db, user, provider_key, operation, args, idempotency_key=idempotency_key)
    if action.status == "succeeded":
        return {"idempotent": True, "action": action, "result": json.loads(action.response_json or "{}") }
    job, deduped = _enqueue_connector_action(db, action, integration)
    db.commit()
    return {"status": "queued", "idempotent": bool(already or deduped), "action": action, "job_id": job.id, "result": None}



def _prepare_connector_action(db: Session, user: User, provider_key: str, operation: str, args: dict, *, idempotency_key: Optional[str] = None, opportunity_id: Optional[str] = None):
    row = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == provider_key))
    if not row or not row.credentials_ciphertext:
        raise HTTPException(503, f"{provider_key} is not configured")
    if provider_key not in ADAPTERS:
        raise HTTPException(501, f"No live adapter is installed for {provider_key}")
    contract = adapter_contract(ADAPTERS[provider_key])
    if operation not in set(contract.get("actions", ())) :
        raise HTTPException(422, f"Connector {provider_key} does not support action {operation}")
    key = idempotency_key or _connector_action_key(user.organization_id, provider_key, operation, args)
    request_hash = hashlib.sha256(json.dumps({"provider": provider_key, "operation": operation, "args": args}, sort_keys=True, default=str).encode()).hexdigest()
    existing = db.scalar(select(ConnectorAction).where(ConnectorAction.organization_id == user.organization_id, ConnectorAction.idempotency_key == key))
    if existing:
        if existing.request_hash != request_hash or existing.provider != provider_key or existing.operation != operation:
            raise HTTPException(409, "Connector idempotency key was already used for a different request")
        if opportunity_id and existing.opportunity_id and existing.opportunity_id != opportunity_id:
            raise HTTPException(409, "Connector idempotency key is linked to a different opportunity")
        return existing, row, True
    action = ConnectorAction(
        id=make_id("cact"), organization_id=user.organization_id, integration_id=row.id,
        opportunity_id=opportunity_id, provider=provider_key, operation=operation, idempotency_key=key, request_hash=request_hash,
        status="queued", attempts=0, request_json=jdump(args), response_json="{}",
        approved_by_user_id=user.id, approved_at=datetime.now(timezone.utc), approval_source="operator",
    )
    try:
        with db.begin_nested():
            db.add(action); db.flush()
    except IntegrityError:
        existing = db.scalar(select(ConnectorAction).where(ConnectorAction.organization_id == user.organization_id, ConnectorAction.idempotency_key == key))
        if not existing:
            raise HTTPException(409, "Concurrent connector action conflict; retry")
        if existing.request_hash != request_hash:
            raise HTTPException(409, "Connector idempotency key was already used for a different request")
        return existing, row, True
    return action, row, False


def _enqueue_connector_action(db: Session, action: ConnectorAction, row: Integration):
    try:
        job, deduped = enqueue_job(
            db, organization_id=action.organization_id, integration_id=row.id,
            connector_action_id=action.id, provider=action.provider, job_type="connector_action",
            payload={"action_id": action.id}, dedupe_key=f"connector-action:{action.id}", max_attempts=8,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return job, deduped


@app.post("/api/agent-runs/{run_id}/execute-action")
async def execute_agent_action(run_id: str, req: dict = {}, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    if req.get("approval") is not True:
        raise HTTPException(400, "Explicit approval is required to execute an Agent OS action")
    run = db.scalar(select(AgentRun).where(AgentRun.organization_id == user.organization_id, AgentRun.id == run_id))
    if not run:
        raise HTTPException(404, "Agent run not found")
    if run.status != "succeeded" or not run.opportunity_id:
        raise HTTPException(409, "Agent run is not executable")
    child = db.execute(select(AgentRun, AgentDefinition).join(AgentDefinition, (AgentDefinition.organization_id == AgentRun.organization_id) & (AgentDefinition.id == AgentRun.agent_definition_id)).where(AgentRun.organization_id == user.organization_id, AgentRun.parent_run_id == run.id, AgentDefinition.key == "action")).first()
    if not child:
        raise HTTPException(404, "Action Agent result not found")
    action_run, _definition = child
    action = json.loads(action_run.output_json or "{}")
    if not action.get("allowed"):
        raise HTTPException(403, "Action Agent did not authorize this action")
    provider_key = str(action.get("provider") or "")
    operation = str(action.get("operation") or "")
    args = dict(action.get("arguments") or {})
    if req:
        for k in ("to", "body", "subject", "channel", "summary", "start", "end", "attendee_email"):
            if k in req: args[k] = req[k]
    if not provider_key or not operation:
        raise HTTPException(400, "Agent action is missing provider or operation")
    if operation in {"send.sms", "send.whatsapp", "send.email"}:
        opp = db.scalar(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id, RevenueOpportunity.id == run.opportunity_id))
        if not opp:
            raise HTTPException(404, "Opportunity not found")
        cust = scoped(db, Customer, opp.customer_id, user.organization_id)
        if not cust:
            raise HTTPException(400, "Agent opportunity customer not found")
        args.setdefault("to", cust.email if operation == "send.email" else cust.phone)
        args.setdefault("body", args.get("message") or "")
        args.setdefault("subject", req.get("subject") or "Following up")
        if not args.get("to") or not args.get("body"):
            raise HTTPException(400, "Agent action is missing a destination or message body")
    if not settings.live_actions:
        raise HTTPException(503, "LIVE_ACTIONS is disabled; enable it only after provider credentials and scopes are verified")
    action_row, integration, already = _prepare_connector_action(db, user, provider_key, operation, args, idempotency_key=req.get("idempotency_key") or action.get("idempotency_key"), opportunity_id=run.opportunity_id)
    if already and action_row.approved_by_user_id is None:
        action_row.approved_by_user_id = user.id
        action_row.approved_at = datetime.now(timezone.utc)
        action_row.approval_source = "agent_operator"
    if action_row.status == "succeeded":
        return {"status": "succeeded", "agent_run_id": run.id, "connector_action_id": action_row.id, "external_id": action_row.external_id, "idempotent": True}
    job, deduped = _enqueue_connector_action(db, action_row, integration)
    add_audit(db, user.organization_id, "agent_connector_action_queued", "agent_run", run.id, {"provider": provider_key, "operation": operation, "connector_action_id": action_row.id, "job_id": job.id, "deduped": already or deduped}, user.id)
    return {"status": "queued", "agent_run_id": run.id, "connector_action_id": action_row.id, "job_id": job.id, "provider": provider_key, "operation": operation, "idempotent": bool(already or deduped)}


@app.post("/api/opportunities/{oid}/execute")
async def execute(oid: str, req: RecoveryRequest, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    o = db.scalar(select(RevenueOpportunity).where(RevenueOpportunity.id == oid, RevenueOpportunity.organization_id == user.organization_id).with_for_update())
    if not o:
        raise HTTPException(404, "Opportunity not found")
    if o.status in TERMINAL:
        raise HTTPException(409, "Opportunity is terminal")
    if not req.approval:
        raise HTTPException(400, "Explicit approval is required")
    cust = scoped(db, Customer, o.customer_id, user.organization_id)
    if not cust:
        raise HTTPException(400, "Opportunity has no customer for outbound recovery")
    active_plan = db.scalar(select(RecoveryPlan).where(
        RecoveryPlan.organization_id == user.organization_id,
        RecoveryPlan.opportunity_id == oid,
        RecoveryPlan.status.in_(["awaiting_approval", "approved", "active"]),
    ).order_by(RecoveryPlan.created_at.desc()).limit(1))
    if not active_plan:
        raise HTTPException(409, "Create and approve a recovery plan before executing an action")
    if active_plan.channel != req.channel:
        raise HTTPException(409, "Action channel does not match the active recovery plan")
    if req.channel in {"sms", "whatsapp"}:
        consent = db.scalar(select(ChannelConsent).where(ChannelConsent.organization_id == user.organization_id, ChannelConsent.customer_id == cust.id, ChannelConsent.channel == req.channel))
        if not consent or not consent.allowed:
            raise HTTPException(403, "No recorded channel consent")
    key = req.idempotency_key
    request_basis = {"opportunity_id": oid, "channel": req.channel, "message": req.message or "Recovery action prepared."}
    request_hash = hashlib.sha256(json.dumps(request_basis, sort_keys=True, default=str).encode()).hexdigest()
    existing = db.scalar(select(RecoveryAction).where(RecoveryAction.organization_id == user.organization_id, RecoveryAction.idempotency_key == key))
    if existing:
        if existing.opportunity_id != oid or existing.channel != req.channel or (existing.request_hash and existing.request_hash != request_hash):
            raise HTTPException(409, "Idempotency key was already used for a different recovery action")
        return {"status": existing.status, "idempotent": True, "action_id": existing.id}

    action_payload = {
        "customer_id": cust.id,
        "message": req.message or "Recovery action prepared.",
        "channel": req.channel,
        "opportunity_id": oid,
        "is_marketing": False,
    }
    safe, safety_report = validate_recovery_action(
        db=db,
        organization_id=user.organization_id,
        action_type="customer_outreach",
        channel=req.channel,
        customer_id=cust.id,
        opportunity_id=oid,
        payload=action_payload,
        autonomy_mode="approval_required",
    )
    if not safe:
        raise HTTPException(403, {"code": "SAFETY_VALIDATION_FAILED", "report": safety_report})
    compliant, compliance_report = validate_communication_compliance(
        db=db,
        organization_id=user.organization_id,
        channel=req.channel,
        direction="outbound",
        customer_id=cust.id,
        content=action_payload["message"],
        metadata={"opportunity_id": oid, "operator_id": user.id},
        is_marketing=False,
    )
    if not compliant:
        raise HTTPException(403, {"code": "COMMUNICATION_COMPLIANCE_FAILED", "report": compliance_report})

    if settings.live_actions:
        provider_key = "twilio" if req.channel == "sms" else "whatsapp_twilio" if req.channel == "whatsapp" else "google_gmail" if req.channel == "email" else None
        if not provider_key:
            raise HTTPException(503, "No live connector adapter is configured for this recovery channel")
        integ = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == provider_key))
        if not integ or not integ.credentials_ciphertext:
            raise HTTPException(503, f"{provider_key} is not configured")

    a = RecoveryAction(
        organization_id=user.organization_id,
        opportunity_id=oid,
        action_type="customer_outreach",
        channel=req.channel,
        status="approved",
        payload_json=jdump({"message": req.message or "Recovery action prepared.", "execution_mode": "simulation", "safety_report": safety_report, "compliance_report": compliance_report}),
        idempotency_key=key,
        request_hash=request_hash,
    )
    db.add(a)
    db.flush()
    connector_job_id = None
    approval = RecoveryApproval(organization_id=user.organization_id, action_id=a.id, approved_by_user_id=user.id, decision="approved", note="Explicit operator approval", policy_version="1")
    db.add(approval)
    if settings.live_actions:
        provider_key = "twilio" if req.channel == "sms" else "whatsapp_twilio" if req.channel == "whatsapp" else "google_gmail"
        op = "send.sms" if req.channel == "sms" else "send.whatsapp" if req.channel == "whatsapp" else "send.email"
        args = {"to": cust.email if req.channel == "email" else cust.phone, "body": req.message or "Just checking in.", "subject": "Following up"}
        if not args["to"]:
            raise HTTPException(400, "Customer has no destination for the selected channel")
        action_row, integration, already = _prepare_connector_action(db, user, provider_key, op, args, idempotency_key=f"recovery:{a.id}", opportunity_id=oid)
        a.status = "sent" if action_row.status == "succeeded" else "queued"
        a.payload_json = jdump({"message": req.message or "Recovery action prepared.", "execution_mode": "live", "provider": provider_key, "operation": op, "connector_action_id": action_row.id, "queued": action_row.status != "succeeded", "external_id": action_row.external_id, "safety_report": safety_report, "compliance_report": compliance_report})
        job, deduped = _enqueue_connector_action(db, action_row, integration)
        connector_job_id = job.id
    else:
        a.status = "simulated"
    a.executed_at = datetime.now(timezone.utc) if (not settings.live_actions or a.status == "sent") else None
    if o.status in {"open", "planned", "awaiting_approval"}:
        transition(db, o, "in_recovery", user.email, {"execution_mode": "live" if settings.live_actions else "simulation"})
    mode = "live" if settings.live_actions else "simulation"
    add_event(db, user.organization_id, oid, "recovery_action_approved", user.email, {"channel": req.channel, "mode": mode, "action_id": a.id})
    add_audit(db, user.organization_id, "recovery_action_approved", "recovery_action", str(a.id), {"opportunity_id": oid, "mode": mode}, user.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(RecoveryAction).where(RecoveryAction.organization_id == user.organization_id, RecoveryAction.idempotency_key == key))
        if existing:
            return {"status": existing.status, "idempotent": True, "action_id": existing.id}
        raise HTTPException(409, "Concurrent action conflict; retry with the same idempotency key")
    return {"status": o.status, "execution": mode, "action_id": a.id, "connector_job_id": connector_job_id, "note": "Live provider action queued durably; poll the job for the provider receipt." if mode == "live" else "Simulation recorded safely; set LIVE_ACTIONS=true after configuring a verified provider."}


@app.post("/api/opportunities/{oid}/dismiss")
def dismiss(oid: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    o = scoped(db, RevenueOpportunity, oid, user.organization_id)
    if not o:
        raise HTTPException(404, "Opportunity not found")
    if o.status in TERMINAL:
        raise HTTPException(409, "Opportunity is terminal")
    transition(db, o, "dismissed", user.email)
    add_audit(db, user.organization_id, "opportunity_dismissed", "opportunity", oid, {}, user.id)
    db.commit()
    return {"status": "dismissed"}


@app.post("/api/consents")
def consent(req: ConsentRequest, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    if req.channel not in CHANNELS:
        raise HTTPException(400, "Unsupported channel")
    c = scoped(db, Customer, req.customer_id, user.organization_id)
    if not c:
        raise HTTPException(404, "Customer not found")
    row = db.scalar(select(ChannelConsent).where(ChannelConsent.organization_id == user.organization_id, ChannelConsent.customer_id == req.customer_id, ChannelConsent.channel == req.channel))
    if not row:
        row = ChannelConsent(id=make_id("cons"), organization_id=user.organization_id, customer_id=req.customer_id, channel=req.channel, allowed=req.allowed, source=req.source)
        db.add(row)
    else:
        row.allowed = req.allowed
        row.source = req.source
        row.updated_at = datetime.now(timezone.utc)
    add_audit(db, user.organization_id, "consent_updated", "customer", c.id, {"channel": req.channel, "allowed": req.allowed, "source": req.source}, user.id)
    db.commit()
    return {"status": "ok", "allowed": row.allowed}


@app.post("/api/tenant/webhook-secret/rotate")
def rotate_webhook_secret(user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    org = db.scalar(select(Organization).where(Organization.id == user.organization_id))
    if not org:
        raise HTTPException(404, "Tenant not found")
    secret = _issue_secret()
    org.webhook_secret_hash = _hash_secret(secret)
    org.webhook_secret_version = int(org.webhook_secret_version or 0) + 1
    add_audit(db, user.organization_id, "webhook_secret_rotated", "organization", user.organization_id, {"version": org.webhook_secret_version}, user.id)
    db.commit()
    return {"organization_id": user.organization_id, "webhook_secret": secret, "version": org.webhook_secret_version}


@app.post("/api/ingest/key")
def ingest_with_api_key(evt: IngestEvent, x_rros_api_key: Optional[str] = Header(default=None), db: Session = Depends(get_db)):
    row = _require_api_key(db, x_rros_api_key, "ingest:write")
    org_id = row["organization_id"]
    occurred_at = evt.occurred_at or datetime.now(timezone.utc)
    identity_basis = {"type": evt.type, "external_id": evt.external_id, "data": evt.data, "occurred_at": evt.occurred_at}
    eid = evt.external_id or hashlib.sha256(json.dumps(identity_basis, sort_keys=True, default=str).encode()).hexdigest()
    raw = json.dumps({"type": evt.type, "external_id": eid, "occurred_at": evt.occurred_at, "data": evt.data}, sort_keys=True, default=str)
    phash = hashlib.sha256(raw.encode()).hexdigest()
    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.organization_id == org_id, WebhookEvent.external_id == eid).with_for_update())
    if existing and existing.payload_hash != phash:
        raise HTTPException(409, "Event ID was already used with a different payload")
    if existing and existing.status == "processed":
        db.commit()
        return {"status": "accepted", "event_id": eid, "idempotent": True}
    if not existing:
        try:
            with db.begin_nested():
                existing = WebhookEvent(organization_id=org_id, external_id=eid, event_type=evt.type, payload_hash=phash, payload_json=raw, status="received")
                db.add(existing); db.flush()
        except IntegrityError:
            existing = db.scalar(select(WebhookEvent).where(WebhookEvent.organization_id == org_id, WebhookEvent.external_id == eid).with_for_update())
            if not existing:
                raise HTTPException(409, "Concurrent event conflict; retry")
            if existing.payload_hash != phash:
                raise HTTPException(409, "Event ID was already used with a different payload")
            if existing.status == "processed":
                db.commit()
                return {"status": "accepted", "event_id": eid, "idempotent": True}
    try:
        canonical_data = dict(evt.data or {})
        if evt.external_id:
            canonical_data.setdefault("external_id", evt.external_id)
        result = canonical_event(db, org_id, "api_key", evt.type, canonical_data, occurred_at)
        existing.status = "processed"
        existing.processed_at = datetime.now(timezone.utc)
        existing.error = None
        add_audit(db, org_id, "api_event_processed", "webhook_event", eid, {"type": evt.type, "result": result})
        db.commit()
        return {"status": "accepted", "event_id": eid, "idempotent": False, "processed": result}
    except Exception as exc:
        db.rollback()
        failed = db.scalar(select(WebhookEvent).where(WebhookEvent.organization_id == org_id, WebhookEvent.external_id == eid))
        if not failed:
            failed = WebhookEvent(organization_id=org_id, external_id=eid, event_type=evt.type, payload_hash=phash, payload_json=raw, status="failed")
            db.add(failed)
        failed.status = "failed"; failed.error = str(exc)[:500]
        db.commit()
        raise HTTPException(422, "Canonical event could not be processed")


@app.post("/api/ingest/{org_id}")
def ingest(org_id: str, evt: IngestEvent, x_webhook_secret: Optional[str] = Header(default=None), db: Session = Depends(get_db)):
    if settings.database_url.startswith("postgresql"):
        row = db.execute(text("select * from rros_webhook_tenant_lookup(:org)"), {"org": org_id}).mappings().first()
        if not row or not row["active"]:
            raise HTTPException(404, "Tenant not found")
        db.execute(text("select set_config('app.current_org', :org, true)"), {"org": org_id})
        stored_hash = row.get("webhook_secret_hash")
    else:
        org = db.get(Organization, org_id)
        if not org or not org.active:
            raise HTTPException(404, "Tenant not found")
        stored_hash = org.webhook_secret_hash
    legacy_expected = os.getenv(f"{settings.webhook_secret_prefix}{org_id}")
    valid = bool(x_webhook_secret) and ((stored_hash and hmac.compare_digest(_hash_secret(x_webhook_secret), stored_hash)) or (legacy_expected and hmac.compare_digest(x_webhook_secret, legacy_expected)))
    if not valid:
        raise HTTPException(401, "Invalid webhook secret")
    occurred_at = evt.occurred_at or datetime.now(timezone.utc)
    identity_basis = {"type": evt.type, "external_id": evt.external_id, "data": evt.data, "occurred_at": evt.occurred_at}
    eid = evt.external_id or hashlib.sha256(json.dumps(identity_basis, sort_keys=True, default=str).encode()).hexdigest()
    raw = json.dumps({"type": evt.type, "external_id": eid, "occurred_at": evt.occurred_at, "data": evt.data}, sort_keys=True, default=str)
    phash = hashlib.sha256(raw.encode()).hexdigest()
    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.organization_id == org_id, WebhookEvent.external_id == eid).with_for_update())
    if existing and existing.payload_hash != phash:
        raise HTTPException(409, "Webhook event ID was already used with a different payload")
    if existing and existing.status == "processed":
        return {"status": "accepted", "event_id": eid, "idempotent": True}
    if not existing:
        try:
            with db.begin_nested():
                existing = WebhookEvent(organization_id=org_id, external_id=eid, event_type=evt.type, payload_hash=phash, payload_json=raw, status="received")
                db.add(existing)
                db.flush()
        except IntegrityError:
            # Another worker may have inserted the same tenant-scoped event concurrently.
            existing = db.scalar(select(WebhookEvent).where(WebhookEvent.organization_id == org_id, WebhookEvent.external_id == eid).with_for_update())
            if not existing:
                raise HTTPException(409, "Concurrent webhook conflict; retry")
            if existing.payload_hash != phash:
                raise HTTPException(409, "Webhook event ID was already used with a different payload")
            if existing.status == "processed":
                return {"status": "accepted", "event_id": eid, "idempotent": True}
    try:
        canonical_data = dict(evt.data or {})
        if evt.external_id:
            canonical_data.setdefault("external_id", evt.external_id)
        result = canonical_event(db, org_id, "generic_webhook", evt.type, canonical_data, occurred_at)
        existing.status = "processed"
        existing.processed_at = datetime.now(timezone.utc)
        existing.error = None
        add_audit(db, org_id, "canonical_event_processed", "webhook_event", eid, {"type": evt.type, "result": result})
        db.commit()
        return {"status": "accepted", "event_id": eid, "idempotent": False, "processed": result}
    except Exception as exc:
        db.rollback()
        failed = db.scalar(select(WebhookEvent).where(WebhookEvent.organization_id == org_id, WebhookEvent.external_id == eid))
        if not failed:
            failed = WebhookEvent(organization_id=org_id, external_id=eid, event_type=evt.type, payload_hash=phash, payload_json=raw, status="failed")
            db.add(failed)
        else:
            failed.status = "failed"
        failed.error = str(exc)[:500]
        db.commit()
        raise HTTPException(422, "Canonical event could not be processed")


@app.post("/api/webhook-events/{event_id}/replay")
def replay_webhook_event(event_id: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    """Replay a failed or received inbox event through the canonical pipeline."""
    event = db.scalar(select(WebhookEvent).where(
        WebhookEvent.organization_id == user.organization_id,
        WebhookEvent.external_id == event_id,
    ).with_for_update())
    if not event:
        raise HTTPException(404, "Webhook event not found")
    if event.status == "processed":
        return {"status": "processed", "event_id": event_id, "idempotent": True}
    if not event.payload_json:
        raise HTTPException(409, "Event payload is unavailable for replay")
    try:
        payload = json.loads(event.payload_json)
        occurred_at = payload.get("occurred_at") or datetime.now(timezone.utc)
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        canonical_data = dict(payload.get("data") or {})
        if payload.get("external_id"):
            canonical_data.setdefault("external_id", payload["external_id"])
        result = canonical_event(
            db,
            user.organization_id,
            "generic_webhook",
            event.event_type,
            canonical_data,
            occurred_at,
        )
        event.status = "processed"
        event.processed_at = datetime.now(timezone.utc)
        event.error = None
        add_audit(db, user.organization_id, "webhook_event_replayed", "webhook_event", event_id, {"result": result}, user.id)
        db.commit()
        return {"status": "accepted", "event_id": event_id, "idempotent": False, "processed": result}
    except Exception as exc:
        db.rollback()
        event = db.scalar(select(WebhookEvent).where(WebhookEvent.organization_id == user.organization_id, WebhookEvent.external_id == event_id))
        if event:
            event.status = "failed"
            event.error = str(exc)[:500]
            db.commit()
        raise HTTPException(422, "Webhook replay failed")


@app.get("/api/recovery/today")
def recovery_today(limit: int = Query(20, ge=1, le=100), user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id, RevenueOpportunity.status.in_(["open", "investigating", "planned", "awaiting_approval", "in_recovery", "engaged"])).order_by(RevenueOpportunity.priority_score.desc()).limit(limit)).all()
    return [serialize_opp(o) for o in rows]


@app.get("/api/revenue/report")
def revenue_report(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(AttributionLink.classification, func.coalesce(func.sum(AttributionLink.attributed_amount), 0), func.count(AttributionLink.id)).where(AttributionLink.organization_id == user.organization_id, AttributionLink.primary_attribution == True).group_by(AttributionLink.classification)).all()
    return {
        "attribution": [{"classification": r[0], "amount": decimal_float(r[1]), "transactions": r[2]} for r in rows],
        "dashboard": build_dashboard(db, user.organization_id),
        "sources": source_analytics(days=30, user=user, db=db),
    }


@app.get("/api/revenue/report.csv")
def revenue_report_csv(user: User = Depends(current_user), db: Session = Depends(get_db)):
    import csv
    from io import StringIO
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["classification", "amount", "transactions"])
    rows = db.execute(
        select(AttributionLink.classification, func.coalesce(func.sum(AttributionLink.attributed_amount), 0), func.count(AttributionLink.id))
        .where(AttributionLink.organization_id == user.organization_id, AttributionLink.primary_attribution == True)
        .group_by(AttributionLink.classification)
    ).all()
    for classification, amount_value, tx_count in rows:
        writer.writerow([classification, decimal_float(amount_value), tx_count])
    response = Response(content=out.getvalue(), media_type="text/csv")
    response.headers["Content-Disposition"] = 'attachment; filename="revenue-recovery-report.csv"'
    return response


@app.get("/api/inbox")
def inbox(limit: int = Query(200, ge=1, le=1000), user: User = Depends(current_user), db: Session = Depends(get_db)):
    messages = db.scalars(
        select(CommunicationEvent)
        .where(CommunicationEvent.organization_id == user.organization_id)
        .order_by(CommunicationEvent.occurred_at.desc())
        .limit(limit)
    ).all()
    grouped: dict[str, dict] = {}
    for m in messages:
        key = m.thread_key or m.customer_id or m.lead_id or m.id
        item = grouped.get(key)
        occurred = aware(m.occurred_at)
        if item is None:
            item = grouped[key] = {
                "thread_key": key, "customer_id": m.customer_id, "lead_id": m.lead_id,
                "channel": m.channel, "last_direction": m.direction, "last_status": m.status,
                "last_at": occurred, "subject": m.subject, "body_preview": m.body_preview,
                "latest_inbound": occurred if m.direction == "inbound" else None,
                "latest_outbound": occurred if m.direction == "outbound" else None,
            }
        else:
            if occurred and (item["latest_inbound"] is None or (m.direction == "inbound" and occurred > item["latest_inbound"])):
                if m.direction == "inbound": item["latest_inbound"] = occurred
            if occurred and (item["latest_outbound"] is None or (m.direction == "outbound" and occurred > item["latest_outbound"])):
                if m.direction == "outbound": item["latest_outbound"] = occurred
    now = datetime.now(timezone.utc)
    items = []
    for key, item in grouped.items():
        last_at = item["last_at"]
        inbound = item["latest_inbound"]
        outbound = item["latest_outbound"]
        items.append({
            "thread_key": key, "customer_id": item["customer_id"], "lead_id": item["lead_id"],
            "channel": item["channel"], "last_direction": item["last_direction"], "last_status": item["last_status"],
            "last_at": last_at.isoformat() if last_at else None,
            "age_hours": max(0, round((now - last_at).total_seconds() / 3600, 1)) if last_at else 0,
            "needs_response": bool(inbound and (not outbound or inbound > outbound)),
            "subject": item["subject"], "body_preview": item["body_preview"],
        })
    items.sort(key=lambda x: (not x["needs_response"], -x["age_hours"]))
    return items


@app.get("/api/insights/recommendations")
def recommendations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id, RevenueOpportunity.status.in_({"open", "investigating", "planned", "awaiting_approval", "in_recovery", "engaged"})).order_by(RevenueOpportunity.priority_score.desc()).limit(1000)).all()
    out = []
    unassigned = [r for r in rows if not r.assigned_user_id]
    urgent_unassigned = [r for r in unassigned if r.severity in {"high", "critical"}]
    if urgent_unassigned:
        ev = sum((Decimal(str(r.expected_value or 0)) for r in urgent_unassigned), Decimal("0"))
        out.append({"id": "assign_urgent", "title": f"Assign {len(urgent_unassigned)} urgent leaks", "reason": f"{money(ev)} expected value is waiting without an owner.", "action": "Assign the highest-priority opportunities to active team members.", "impact": decimal_float(ev), "kind": "ownership"})
    overdue = [r for r in rows if r.type == "overdue_invoice"]
    if overdue:
        ev = sum((Decimal(str(r.expected_value or 0)) for r in overdue), Decimal("0"))
        out.append({"id": "collect_overdue", "title": f"Review {len(overdue)} overdue invoices", "reason": f"{money(ev)} expected collectible value is currently overdue.", "action": "Use authorized payment-recovery paths and verify consent before communication.", "impact": decimal_float(ev), "kind": "collections"})
    response_gaps = [r for r in rows if r.type == "response_gap"]
    if response_gaps:
        ev = sum((Decimal(str(r.expected_value or 0)) for r in response_gaps), Decimal("0"))
        out.append({"id": "close_response_gaps", "title": f"Close {len(response_gaps)} response gaps", "reason": f"Inbound requests have no recorded outbound response.", "action": "Assign and respond through the customer-preferred compliant channel.", "impact": decimal_float(ev), "kind": "speed_to_lead"})
    failed_payments = [r for r in rows if r.type == "failed_payment"]
    if failed_payments:
        ev = sum((Decimal(str(r.expected_value or 0)) for r in failed_payments), Decimal("0"))
        out.append({"id": "recover_failed_payments", "title": f"Recover {len(failed_payments)} failed payments", "reason": f"{money(ev)} expected value is blocked by payment failures.", "action": "Use the authorized payment recovery flow; never request raw payment credentials.", "impact": decimal_float(ev), "kind": "payments"})
    reviews = [r for r in rows if r.type == "review_recovery"]
    if reviews:
        ev = sum((Decimal(str(r.expected_value or 0)) for r in reviews), Decimal("0"))
        out.append({"id": "resolve_reviews", "title": f"Resolve {len(reviews)} negative reviews", "reason": "Customer-experience issues are tied to recovery opportunities.", "action": "Resolve the underlying service issue and keep public responses human-approved.", "impact": decimal_float(ev), "kind": "reputation"})
    return out[:8]


@app.get("/api/audit-log")
def audit_log(limit: int = Query(100, ge=1, le=500), user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).where(AuditLog.organization_id == user.organization_id).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return [{"action": x.action, "resource_type": x.resource_type, "resource_id": x.resource_id, "actor_user_id": x.actor_user_id, "metadata": json.loads(x.metadata_json or "{}"), "created_at": x.created_at.isoformat()} for x in rows]


@app.get("/api/analytics/sources")
def source_analytics(days: int = Query(30, ge=1, le=3650), user: User = Depends(current_user), db: Session = Depends(get_db)):
    # Revenue is grouped by explicit lead-source lineage; this avoids silently assigning
    # revenue to a source merely because the customer has a historical marketing touch.
    end_day = date.today()
    start_day = end_day - timedelta(days=days - 1)
    start_dt = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    lead_rows = db.execute(
        select(func.lower(Lead.source), func.count(Lead.id), func.coalesce(func.sum(Lead.value), 0))
        .where(Lead.organization_id == user.organization_id, Lead.created_at >= start_dt, Lead.created_at < end_dt)
        .group_by(func.lower(Lead.source))
        .order_by(func.coalesce(func.sum(Lead.value), 0).desc())
    ).all()
    recovered_rows = db.execute(
        select(func.lower(Lead.source), func.count(func.distinct(Transaction.id)), func.coalesce(func.sum(AttributionLink.attributed_amount), 0))
        .join(RevenueOpportunity, (RevenueOpportunity.organization_id == Lead.organization_id) & (RevenueOpportunity.lead_id == Lead.id))
        .join(AttributionLink, (AttributionLink.organization_id == RevenueOpportunity.organization_id) & (AttributionLink.opportunity_id == RevenueOpportunity.id))
        .join(Transaction, (Transaction.organization_id == AttributionLink.organization_id) & (Transaction.id == AttributionLink.transaction_id))
        .where(
            Lead.organization_id == user.organization_id,
            RevenueOpportunity.lead_id == Lead.id,
            AttributionLink.organization_id == user.organization_id,
            AttributionLink.primary_attribution == True,
            Transaction.status == "paid",
            Transaction.paid_at >= start_dt,
            Transaction.paid_at < end_dt,
            Lead.created_at >= start_dt,
            Lead.created_at < end_dt,
        )
        .group_by(func.lower(Lead.source))
    ).all()
    spend_rows = db.execute(
        select(MarketingSpend.source, func.coalesce(func.sum(MarketingSpend.amount), 0))
        .where(
            MarketingSpend.organization_id == user.organization_id,
            MarketingSpend.period_end >= start_day,
            MarketingSpend.period_start <= end_day,
        )
        .group_by(MarketingSpend.source)
    ).all()
    source_map = {}
    for source, count, value in lead_rows:
        source_map[str(source or "unknown")] = {"source": str(source or "unknown"), "leads": count, "lead_value": decimal_float(value), "recovered": 0.0, "recovered_transactions": 0, "spend": 0.0}
    for source, count, value in recovered_rows:
        key = str(source or "unknown")
        source_map.setdefault(key, {"source": key, "leads": 0, "lead_value": 0.0, "recovered": 0.0, "recovered_transactions": 0, "spend": 0.0})
        source_map[key]["recovered_transactions"] = count
        source_map[key]["recovered"] = decimal_float(value)
    for source, amount in spend_rows:
        key = str(source or "unknown")
        source_map.setdefault(key, {"source": key, "leads": 0, "lead_value": 0.0, "recovered": 0.0, "recovered_transactions": 0, "spend": 0.0})
        source_map[key]["spend"] = decimal_float(amount)
    result = []
    for x in sorted(source_map.values(), key=lambda v: (v["recovered"], v["lead_value"]), reverse=True):
        spend = x["spend"]
        x["roi"] = round((x["recovered"] - spend) / spend, 4) if spend > 0 else None
        x["roas"] = round(x["recovered"] / spend, 4) if spend > 0 else None
        x["cost_per_lead"] = round(spend / x["leads"], 2) if x["leads"] else None
        x["recovery_rate"] = round(x["recovered_transactions"] / x["leads"], 4) if x["leads"] else None
        result.append(x)
    return {"window": {"days": days, "start": start_day.isoformat(), "end": end_day.isoformat()}, "sources": result}


@app.post("/api/marketing/spend")
def add_marketing_spend(req: MarketingSpendRequest, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    source = req.source.strip().lower()
    if req.period_end < req.period_start:
        raise HTTPException(400, "period_end must be on or after period_start")
    if req.location_id and not scoped(db, Location, req.location_id, user.organization_id):
        raise HTTPException(404, "Location not found")
    existing = db.scalar(select(MarketingSpend).where(
        MarketingSpend.organization_id == user.organization_id,
        MarketingSpend.location_id == req.location_id,
        MarketingSpend.source == source,
        MarketingSpend.period_start == req.period_start,
        MarketingSpend.period_end == req.period_end,
    ))
    if existing:
        existing.amount = req.amount
        existing.notes = req.notes
        row = existing
    else:
        row = MarketingSpend(
            id=make_id("spend"), organization_id=user.organization_id, location_id=req.location_id, source=source,
            amount=req.amount, period_start=req.period_start, period_end=req.period_end, notes=req.notes,
        )
        db.add(row)
    add_audit(db, user.organization_id, "marketing_spend_saved", "marketing_spend", row.id, {"source": source, "amount": str(req.amount)}, user.id)
    db.commit()
    return {"id": row.id, "source": row.source, "amount": decimal_float(row.amount), "period_start": row.period_start.isoformat(), "period_end": row.period_end.isoformat()}



async def _run_live_sync(db: Session, user: User, provider_key: str, resource: str, cursor: Optional[str] = None):
    """Compatibility wrapper: provider syncs are always durable jobs, never inline network work."""
    row = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == provider_key))
    if not row or not row.credentials_ciphertext:
        raise HTTPException(404, "Integration is not configured")
    if provider_key not in ADAPTERS:
        raise HTTPException(501, "A live adapter is not installed for this provider")
    run_id = __import__("uuid").uuid4().hex
    dedupe = f"manual-sync:{user.organization_id}:{row.id}:{resource}:{hashlib.sha256(str(cursor or '').encode()).hexdigest()[:24]}:{run_id}"
    job, deduped = enqueue_job(db, organization_id=user.organization_id, integration_id=row.id, provider=provider_key, job_type="connector_sync", resource=resource, payload={"provider":provider_key,"resource":resource,"cursor":cursor,"run_id":run_id}, dedupe_key=dedupe, max_attempts=8)
    db.commit()
    return {"status":"queued","job_id":job.id,"provider":provider_key,"resource":resource,"deduped":deduped}


@app.get("/api/integrations/live-adapters")
def live_adapters(user: User = Depends(current_user)):
    return {"providers": sorted(ADAPTERS), "count": len(ADAPTERS)}


@app.post("/api/integrations/{provider_key}/sync/{resource}")
def integration_sync(provider_key: str, resource: str, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    try:
        get_provider(provider_key)
    except KeyError:
        raise HTTPException(404, "Unknown integration provider")
    row = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == provider_key))
    if not row or not row.credentials_ciphertext:
        raise HTTPException(404, "Integration is not configured")
    sync = db.scalar(select(IntegrationSyncState).where(IntegrationSyncState.organization_id == user.organization_id, IntegrationSyncState.integration_id == row.id, IntegrationSyncState.resource == resource))
    cursor = sync.cursor if sync else None
    dedupe = f"sync:{provider_key}:{resource}:{hashlib.sha256(str(cursor or '').encode()).hexdigest()[:24]}"
    try:
        job, deduped = enqueue_job(db, organization_id=user.organization_id, integration_id=row.id, provider=provider_key, job_type="connector_sync", resource=resource, payload={"resource": resource, "cursor": cursor}, dedupe_key=dedupe, max_attempts=8)
    except ValueError as exc:
        db.rollback(); raise HTTPException(409, str(exc))
    db.commit()
    return {"status": "queued", "job_id": job.id, "provider": provider_key, "resource": resource, "deduped": deduped}


@app.post("/api/integrations/{provider_key}/action")
def integration_action(provider_key: str, req: dict, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    if req.get("approval") is not True:
        raise HTTPException(400, "Explicit approval is required")
    if not settings.live_actions:
        raise HTTPException(503, "LIVE_ACTIONS is disabled")
    op = str(req.get("operation") or "")
    args = dict(req.get("args") or {k: v for k, v in req.items() if k not in {"operation", "idempotency_key"}})
    action, integration, already = _prepare_connector_action(db, user, provider_key, op, args, idempotency_key=req.get("idempotency_key"))
    if action.status == "succeeded":
        return {"status": "succeeded", "provider": provider_key, "operation": op, "external_id": action.external_id, "connector_action_id": action.id, "idempotent": True}
    job, deduped = _enqueue_connector_action(db, action, integration)
    add_audit(db, user.organization_id, "connector_action_queued", "connector_action", action.id, {"provider": provider_key, "operation": op, "job_id": job.id}, user.id)
    db.commit()
    return {"status": "queued", "provider": provider_key, "operation": op, "connector_action_id": action.id, "job_id": job.id, "idempotent": bool(already or deduped)}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = db.scalar(select(IntegrationJob).where(IntegrationJob.organization_id == user.organization_id, IntegrationJob.id == job_id))
    if not job:
        raise HTTPException(404, "Job not found")
    return {"id": job.id, "type": job.job_type, "provider": job.provider, "resource": job.resource, "status": job.status, "attempts": job.attempts, "max_attempts": job.max_attempts, "last_error": job.last_error, "result": json.loads(job.result_json or "{}"), "created_at": job.created_at.isoformat(), "completed_at": job.completed_at.isoformat() if job.completed_at else None}


@app.get("/api/integrations/catalog")
def integration_catalog(user: User = Depends(current_user)):
    return {"providers": public_catalog()}


@app.get("/api/integrations")
def integrations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Integration).where(Integration.organization_id == user.organization_id).order_by(Integration.provider)).all()
    by = {x.provider: x for x in rows}
    return [
        {
            "provider": p.key,
            "name": p.name,
            "category": p.category,
            "auth": p.auth,
            "status": by[p.key].status if p.key in by else "not_configured",
            "last_sync_at": by[p.key].last_sync_at.isoformat() if p.key in by and by[p.key].last_sync_at else None,
            "last_error": by[p.key].last_error if p.key in by else None,
            "health_checked_at": by[p.key].health_checked_at.isoformat() if p.key in by and by[p.key].health_checked_at else None,
            "capabilities": list(p.capabilities),
            "implementation": p.implementation,
        }
        for p in PROVIDERS.values()
    ]


@app.post("/api/integrations/{provider_key}/configure")
def configure_integration(provider_key: str, req: ManualIntegrationRequest, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    try:
        p = get_provider(provider_key)
    except KeyError:
        raise HTTPException(404, "Unknown integration provider")
    if p.auth == "oauth":
        raise HTTPException(400, "This provider requires OAuth; use /oauth/start")
    if not req.credentials:
        raise HTTPException(400, "Credentials are required")
    if provider_key in {"twilio", "whatsapp_twilio"}:
        if not req.credentials.get("account_sid") or not req.credentials.get("auth_token"):
            raise HTTPException(400, "Twilio credentials require account_sid and auth_token")
    elif provider_key == "stripe":
        if not req.credentials.get("api_key"):
            raise HTTPException(400, "Stripe requires api_key")
    elif not any(req.credentials.get(k) for k in ("api_key", "access_token", "token", "auth_token")):
        raise HTTPException(400, "Credentials must include api_key, access_token, token or auth_token")
    if req.config.get("base_url"):
        if p.api_base:
            raise HTTPException(400, "This provider uses a fixed vendor endpoint; custom base URLs are not allowed")
        try:
            validate_remote_url(str(req.config["base_url"]).rstrip("/") + "/")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    row = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == provider_key))
    if not row:
        row = Integration(id=make_id("int"), organization_id=user.organization_id, provider=provider_key)
        db.add(row)
    row.credentials_ciphertext = encrypt_json(req.credentials)
    row.config_json = json.dumps(req.config, separators=(",", ":"))
    row.status = "configured"
    row.last_error = None
    add_audit(db, user.organization_id, "integration_configured", "integration", row.id, {"provider": provider_key, "auth": p.auth}, user.id)
    db.commit()
    return {"provider": provider_key, "status": row.status}


@app.post("/api/integrations/{provider_key}/oauth/start")
def integration_oauth_start(provider_key: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    try:
        p = get_provider(provider_key)
    except KeyError:
        raise HTTPException(404, "Unknown integration provider")
    if p.auth != "oauth":
        raise HTTPException(400, "Provider does not use OAuth")
    if not p.authorize_url or not p.token_url:
        raise HTTPException(400, "This provider requires account-specific OAuth configuration and cannot use the generic OAuth flow")
    client_id = os.getenv(f"RROS_{provider_key.upper()}_CLIENT_ID") or (os.getenv("RROS_GOOGLE_CLIENT_ID") if provider_key.startswith("google_") else None)
    if not client_id:
        raise HTTPException(503, "OAuth client ID is not configured for this provider")
    state = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    db.add(IntegrationOAuthState(id=make_id("ios"), organization_id=user.organization_id, user_id=user.id, provider=provider_key, state_hash=state_hash, expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)))
    db.commit()
    redirect = f"{settings.public_base_url.rstrip('/')}/api/integrations/{provider_key}/oauth/callback"
    extra = {}
    if provider_key.startswith("google_"):
        extra.update(access_type="offline", prompt="consent")
    return {"authorization_url": oauth_url(p, client_id, redirect, state, extra), "provider": provider_key}


@app.get("/api/integrations/{provider_key}/oauth/callback")
async def integration_oauth_callback(provider_key: str, code: str, state: str, db: Session = Depends(get_db)):
    try:
        p = get_provider(provider_key)
    except KeyError:
        raise HTTPException(404, "Unknown integration provider")
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    if settings.database_url.startswith("postgresql"):
        row = db.execute(text("select * from rros_oauth_state_lookup(:provider,:state_hash)"), {"provider": provider_key, "state_hash": state_hash}).mappings().first()
        if not row:
            raise HTTPException(400, "Invalid or expired OAuth state")
        db.execute(text("select set_config('app.current_org', :org, true)"), {"org": row["organization_id"]})
    st = db.scalar(select(IntegrationOAuthState).where(IntegrationOAuthState.provider == provider_key, IntegrationOAuthState.state_hash == state_hash, IntegrationOAuthState.used == False).with_for_update())
    if not st or st.expires_at < datetime.now(timezone.utc):
        raise HTTPException(400, "Invalid or expired OAuth state")
    client_id = os.getenv(f"RROS_{provider_key.upper()}_CLIENT_ID") or (os.getenv("RROS_GOOGLE_CLIENT_ID") if provider_key.startswith("google_") else None)
    client_secret = os.getenv(f"RROS_{provider_key.upper()}_CLIENT_SECRET") or (os.getenv("RROS_GOOGLE_CLIENT_SECRET") if provider_key.startswith("google_") else None)
    if not client_id or not client_secret:
        raise HTTPException(503, "OAuth client credentials are not configured")
    redirect = f"{settings.public_base_url.rstrip('/')}/api/integrations/{provider_key}/oauth/callback"
    try:
        tokens = await exchange_code(p, client_id, client_secret, code, redirect)
    except Exception:
        raise HTTPException(502, "OAuth token exchange failed")
    row = db.scalar(select(Integration).where(Integration.organization_id == st.organization_id, Integration.provider == provider_key).with_for_update())
    if not row:
        row = Integration(id=make_id("int"), organization_id=st.organization_id, provider=provider_key)
        db.add(row)
    tokens = _with_expiry({}, tokens)
    row.credentials_ciphertext = encrypt_json(tokens)
    row.status = "connected"
    row.last_error = None
    row.config_json = "{}"
    row.health_checked_at = datetime.now(timezone.utc)
    st.used = True
    add_audit(db, st.organization_id, "integration_connected", "integration", row.id, {"provider": provider_key, "auth": "oauth"}, st.user_id)
    db.commit()
    return {"status": "connected", "provider": provider_key, "message": "Integration connected. You can close this window."}


@app.post("/api/integrations/{provider_key}/test")
async def integration_test(provider_key: str, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    try:
        get_provider(provider_key)
    except KeyError:
        raise HTTPException(404, "Unknown integration provider")
    row = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == provider_key))
    if not row or not row.credentials_ciphertext:
        raise HTTPException(404, "Integration is not configured")
    if provider_key not in ADAPTERS:
        raise HTTPException(501, "No live adapter is installed for this provider")
    creds = decrypt_json(row.credentials_ciphertext)
    config = json.loads(row.config_json or "{}")
    adapter = adapter_for(provider_key, creds, config)
    try:
        result = await adapter.health()
        row.status = "connected"; row.last_error = None; row.health_checked_at = datetime.now(timezone.utc)
        db.commit()
        return {"status":"connected","provider":provider_key,"adapter":"live","result":{"ok":True}}
    except Exception as exc:
        row.status = "error"; row.last_error = str(exc)[:500]; row.health_checked_at = datetime.now(timezone.utc); db.commit()
        raise HTTPException(502, "Provider connection test failed")
    finally:
        await adapter.close()

@app.post("/api/integrations/{provider_key}/disconnect")
def integration_disconnect(provider_key: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    row = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == provider_key))
    if not row:
        raise HTTPException(404, "Integration not configured")
    row.credentials_ciphertext = None
    row.status = "disconnected"
    row.last_error = None
    row.health_checked_at = datetime.now(timezone.utc)
    add_audit(db, user.organization_id, "integration_disconnected", "integration", row.id, {"provider": provider_key}, user.id)
    db.commit()
    return {"provider": provider_key, "status": "disconnected"}


@app.post("/api/integrations/{provider_key}/config")
def integration_config(provider_key: str, req: IntegrationConfigRequest, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    try:
        get_provider(provider_key)
    except KeyError:
        raise HTTPException(404, "Unknown integration provider")
    p = get_provider(provider_key)
    if req.config.get("base_url"):
        if p.api_base:
            raise HTTPException(400, "This provider uses a fixed vendor endpoint; custom base URLs are not allowed")
        try:
            validate_remote_url(str(req.config["base_url"]).rstrip("/") + "/")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    row = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == provider_key))
    if not row:
        row = Integration(id=make_id("int"), organization_id=user.organization_id, provider=provider_key)
        db.add(row)
    row.config_json = json.dumps(req.config, separators=(",", ":"))
    add_audit(db, user.organization_id, "integration_config_updated", "integration", row.id, {"provider": provider_key}, user.id)
    db.commit()
    return {"provider": provider_key, "config_saved": True}


@app.post("/api/integrations/{provider_key}/refresh")
async def integration_refresh(provider_key: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    try:
        p = get_provider(provider_key)
    except KeyError:
        raise HTTPException(404, "Unknown integration provider")
    row = db.scalar(select(Integration).where(Integration.organization_id == user.organization_id, Integration.provider == provider_key).with_for_update())
    if not row or not row.credentials_ciphertext:
        raise HTTPException(404, "Integration not configured")
    if p.auth != "oauth":
        raise HTTPException(400, "Only OAuth integrations can refresh tokens")
    creds = decrypt_json(row.credentials_ciphertext)
    refresh = creds.get("refresh_token")
    if not refresh:
        raise HTTPException(400, "No refresh token stored")
    client_id = os.getenv(f"RROS_{provider_key.upper()}_CLIENT_ID") or (os.getenv("RROS_GOOGLE_CLIENT_ID") if provider_key.startswith("google_") else None)
    client_secret = os.getenv(f"RROS_{provider_key.upper()}_CLIENT_SECRET") or (os.getenv("RROS_GOOGLE_CLIENT_SECRET") if provider_key.startswith("google_") else None)
    if not client_id or not client_secret:
        raise HTTPException(503, "OAuth client credentials are not configured")
    try:
        tokens = await refresh_token(p, client_id, client_secret, refresh)
    except Exception:
        raise HTTPException(502, "Token refresh failed")
    creds = _with_expiry(creds, tokens)
    row.credentials_ciphertext = encrypt_json(creds)
    row.status = "connected"
    row.last_error = None
    row.health_checked_at = datetime.now(timezone.utc)
    db.commit()
    return {"provider": provider_key, "status": "connected"}


@app.get("/api/playbooks")
def playbooks(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(RecoveryPlaybook).where(RecoveryPlaybook.organization_id == user.organization_id).order_by(RecoveryPlaybook.updated_at.desc())).all()
    return [{"id": r.id, "name": r.name, "description": r.description, "trigger_type": r.trigger_type, "conditions": json.loads(r.conditions_json or "{}"), "steps": json.loads(r.steps_json or "[]"), "status": r.status, "version": r.version, "updated_at": r.updated_at.isoformat()} for r in rows]


@app.post("/api/playbooks")
def create_playbook(req: PlaybookRequest, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    name = req.name.strip()
    if db.scalar(select(RecoveryPlaybook).where(RecoveryPlaybook.organization_id == user.organization_id, RecoveryPlaybook.name == name)):
        raise HTTPException(409, "Playbook name already exists")
    for i, step in enumerate(req.steps, 1):
        channel = step.get("channel")
        if channel and channel not in CHANNELS:
            raise HTTPException(400, f"Unsupported channel in step {i}")
        if not isinstance(step, dict):
            raise HTTPException(400, f"Playbook step {i} must be an object")
    row = RecoveryPlaybook(id=make_id("pb"), organization_id=user.organization_id, name=name, description=req.description, trigger_type=req.trigger_type.strip(), conditions_json=jdump(req.conditions), steps_json=jdump(req.steps), status="draft", version=1)
    db.add(row)
    add_audit(db, user.organization_id, "playbook_created", "playbook", row.id, {"trigger_type": row.trigger_type}, user.id)
    db.commit()
    return {"id": row.id, "status": row.status, "version": row.version}


@app.post("/api/playbooks/{playbook_id}/publish")
def publish_playbook(playbook_id: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    row = scoped(db, RecoveryPlaybook, playbook_id, user.organization_id)
    if not row:
        raise HTTPException(404, "Playbook not found")
    steps = json.loads(row.steps_json or "[]")
    if not steps:
        raise HTTPException(400, "A playbook must contain at least one step before publishing")
    row.status = "published"
    row.version += 1
    row.updated_at = datetime.now(timezone.utc)
    add_audit(db, user.organization_id, "playbook_published", "playbook", row.id, {"version": row.version}, user.id)
    db.commit()
    return {"id": row.id, "status": row.status, "version": row.version}


@app.post("/api/playbooks/{playbook_id}/apply/{oid}")
def apply_playbook(playbook_id: str, oid: str, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    row = scoped(db, RecoveryPlaybook, playbook_id, user.organization_id)
    opp = scoped(db, RevenueOpportunity, oid, user.organization_id)
    if not row or not opp:
        raise HTTPException(404, "Playbook or opportunity not found")
    if row.status != "published":
        raise HTTPException(409, "Playbook must be published before it can be applied")
    conditions = json.loads(row.conditions_json or "{}")
    allowed_types = conditions.get("opportunity_types") or []
    if allowed_types and opp.type not in allowed_types:
        raise HTTPException(409, "Playbook conditions do not match this opportunity")
    allowed_statuses = conditions.get("statuses") or []
    if allowed_statuses and opp.status not in allowed_statuses:
        raise HTTPException(409, "Playbook conditions do not match this opportunity status")
    existing = db.scalar(select(RecoveryPlan).where(RecoveryPlan.organization_id == user.organization_id, RecoveryPlan.opportunity_id == oid, RecoveryPlan.status.in_(["awaiting_approval","approved","active"])).limit(1))
    if existing:
        return {"status": existing.status, "plan_id": existing.id, "idempotent": True}
    steps = json.loads(row.steps_json or "[]")
    channel = next((s.get("channel") for s in steps if isinstance(s, dict) and s.get("channel")), "sms")
    if channel not in CHANNELS:
        raise HTTPException(400, "Published playbook contains an unsupported channel")
    plan = RecoveryPlan(id=make_id("plan"), organization_id=user.organization_id, opportunity_id=oid, objective=row.name, channel=channel, steps_json=jdump(steps), status="awaiting_approval", approval_required=True)
    db.add(plan)
    if opp.status == "open":
        transition(db, opp, "planned", user.email, {"playbook_id": row.id})
    add_event(db, user.organization_id, oid, "playbook_applied", user.email, {"playbook_id": row.id, "playbook_version": row.version})
    add_audit(db, user.organization_id, "playbook_applied", "playbook", row.id, {"opportunity_id": oid, "version": row.version}, user.id)
    db.commit()
    return {"status": plan.status, "plan_id": plan.id, "playbook_id": row.id, "opportunity_id": oid, "approval_required": True, "execution": "approval_gated"}


def _review_draft(review: Review) -> str:
    topic = (review.topic or "your experience").replace("_", " ")
    if review.rating <= 2:
        return f"Thank you for taking the time to share your feedback. We’re sorry your experience around {topic} did not meet expectations. We’re reviewing what happened so we can address the underlying issue. Please contact our team directly so we can understand the details and make this right."
    if review.rating == 3:
        return f"Thank you for the thoughtful feedback. We appreciate you calling out {topic}. We’re using comments like yours to improve the experience, and we’d welcome the chance to learn more about what would have made your visit better."
    return f"Thank you for sharing your experience. We’re glad you had a positive experience, especially around {topic}. We appreciate your trust and look forward to serving you again."


@app.post("/api/reviews/{review_id}/response-draft")
def review_response_draft(review_id: str, req: ReviewDraftRequest, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    review = scoped(db, Review, review_id, user.organization_id)
    if not review:
        raise HTTPException(404, "Review not found")
    draft = (req.draft_text or _review_draft(review)).strip()
    existing = db.scalar(select(ReviewResponse).where(ReviewResponse.organization_id == user.organization_id, ReviewResponse.review_id == review_id))
    if existing:
        if existing.status == "sent":
            raise HTTPException(409, "A response has already been sent")
        existing.draft_text = draft
        existing.status = "draft"
        row = existing
    else:
        row = ReviewResponse(id=make_id("rr"), organization_id=user.organization_id, review_id=review_id, draft_text=draft, status="draft")
        db.add(row)
    add_audit(db, user.organization_id, "review_response_drafted", "review", review_id, {"method": "rules-v1", "rating": review.rating}, user.id)
    db.commit()
    return {"id": row.id, "review_id": review_id, "status": row.status, "draft_text": row.draft_text, "method": "rules-v1", "send_status": "not_sent"}


@app.post("/api/reviews/{review_id}/response-approve")
def review_response_approve(review_id: str, user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    row = db.scalar(select(ReviewResponse).where(ReviewResponse.organization_id == user.organization_id, ReviewResponse.review_id == review_id).with_for_update())
    if not row:
        raise HTTPException(404, "No response draft exists")
    if row.status == "sent":
        raise HTTPException(409, "Response already sent")
    row.status = "approved"
    row.approved_by_user_id = user.id
    add_audit(db, user.organization_id, "review_response_approved", "review_response", row.id, {"review_id": review_id}, user.id)
    db.commit()
    return {"status": row.status, "review_id": review_id, "send_status": "approval_only; live provider adapter required"}


@app.get("/api/reviews/{review_id}/response-draft")
def review_response_get(review_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    review = scoped(db, Review, review_id, user.organization_id)
    if not review:
        raise HTTPException(404, "Review not found")
    row = db.scalar(select(ReviewResponse).where(ReviewResponse.organization_id == user.organization_id, ReviewResponse.review_id == review_id))
    return {"review_id": review_id, "draft": {"id": row.id, "text": row.draft_text, "status": row.status} if row else None}


@app.get("/api/reviews/{review_id}/analysis")
def review_analysis(review_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    review = scoped(db, Review, review_id, user.organization_id)
    if not review:
        raise HTTPException(404, "Review not found")
    analysis = json.loads(review.analysis_json or "{}")
    if not analysis:
        from .review_system import ReviewAnalysis
        analysis = ReviewAnalysis(db, user.organization_id).analyze_review(review)
        db.commit()
    return {"review_id": review_id, "analysis": analysis}


@app.get("/api/customers")
def customers(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000), search: Optional[str] = Query(None, max_length=100), user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Customer).where(Customer.organization_id == user.organization_id)
    if search:
        term = f"%{search.strip()}%"
        q = q.where((Customer.name.ilike(term)) | (Customer.email.ilike(term)) | (Customer.phone.ilike(term)))
    rows = db.scalars(q.order_by(Customer.name).offset(offset).limit(limit)).all()
    return [{"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "lifecycle_status": c.lifecycle_status} for c in rows]


@app.get("/api/reviews")
def reviews(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000), rating: Optional[int] = Query(None, ge=1, le=5), user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Review).where(Review.organization_id == user.organization_id)
    if rating is not None:
        q = q.where(Review.rating == rating)
    rows = db.scalars(q.order_by(Review.created_at.desc()).offset(offset).limit(limit)).all()
    response_map = {x.review_id: x.status for x in db.scalars(select(ReviewResponse).where(ReviewResponse.organization_id == user.organization_id, ReviewResponse.review_id.in_([r.id for r in rows]))).all()} if rows else {}
    return [{"id": r.id, "customer_id": r.customer_id, "source": r.source, "rating": r.rating, "text": r.text, "topic": r.topic, "severity": r.severity, "source_url": r.source_url, "response_status": response_map.get(r.id), "created_at": r.created_at.isoformat()} for r in rows]


@app.get("/api/customers/{customer_id}")
def customer_360(customer_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    customer = scoped(db, Customer, customer_id, user.organization_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    leads = db.scalars(select(Lead).where(Lead.organization_id == user.organization_id, Lead.customer_id == customer.id).order_by(Lead.created_at.desc()).limit(100)).all()
    appointments = db.scalars(select(Appointment).where(Appointment.organization_id == user.organization_id, Appointment.customer_id == customer.id).order_by(Appointment.scheduled_at.desc()).limit(100)).all()
    transactions = db.scalars(select(Transaction).where(Transaction.organization_id == user.organization_id, Transaction.customer_id == customer.id).order_by(Transaction.paid_at.desc()).limit(100)).all()
    invoices = db.scalars(select(Invoice).where(Invoice.organization_id == user.organization_id, Invoice.customer_id == customer.id).order_by(Invoice.due_at.desc()).limit(100)).all()
    payments = db.scalars(select(Payment).where(Payment.organization_id == user.organization_id, Payment.customer_id == customer.id).order_by(Payment.failed_at.desc()).limit(100)).all()
    reviews = db.scalars(select(Review).where(Review.organization_id == user.organization_id, Review.customer_id == customer.id).order_by(Review.created_at.desc()).limit(100)).all()
    calls = db.scalars(select(CallEvent).where(CallEvent.organization_id == user.organization_id, CallEvent.customer_id == customer.id).order_by(CallEvent.started_at.desc()).limit(100)).all()
    messages = db.scalars(select(CommunicationEvent).where(CommunicationEvent.organization_id == user.organization_id, CommunicationEvent.customer_id == customer.id).order_by(CommunicationEvent.occurred_at.desc()).limit(100)).all()
    consents = db.scalars(select(ChannelConsent).where(ChannelConsent.organization_id == user.organization_id, ChannelConsent.customer_id == customer.id)).all()
    opportunities = db.scalars(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id, RevenueOpportunity.customer_id == customer.id).order_by(RevenueOpportunity.priority_score.desc()).limit(100)).all()
    lifetime_value = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(Transaction.organization_id == user.organization_id, Transaction.customer_id == customer.id, Transaction.status == "paid")) or 0
    paid = sorted([x for x in transactions if x.status == "paid" and x.paid_at], key=lambda x: aware(x.paid_at))
    gaps = [(aware(paid[i].paid_at) - aware(paid[i-1].paid_at)).days for i in range(1, len(paid)) if paid[i].paid_at and paid[i-1].paid_at]
    baseline_days = round(sum(gaps) / len(gaps), 1) if gaps else None
    days_since_purchase = max(0, (datetime.now(timezone.utc) - aware(paid[-1].paid_at)).days) if paid else None
    failed_count = sum(1 for x in payments if x.status == "failed")
    negative_reviews = sum(1 for x in reviews if x.rating <= 2)
    cancelled_count = sum(1 for x in appointments if x.status in {"cancelled", "no_show"})
    open_customer_opps = [x for x in opportunities if x.status in {"open", "investigating", "planned", "awaiting_approval", "in_recovery", "engaged"}]
    risk = 0.0
    factors = []
    if baseline_days and days_since_purchase is not None and days_since_purchase > max(60, baseline_days * 1.7):
        contribution = min(40.0, 20.0 + (days_since_purchase / max(baseline_days, 1) - 1.7) * 15.0)
        risk += contribution; factors.append({"factor": "purchase_recency", "impact": round(contribution,1), "detail": f"{days_since_purchase} days since last purchase vs {round(baseline_days)}-day baseline"})
    if failed_count:
        contribution = min(20.0, 8.0 + failed_count * 4.0)
        risk += contribution; factors.append({"factor": "failed_payments", "impact": round(contribution,1), "detail": f"{failed_count} failed payment(s)"})
    if negative_reviews:
        contribution = min(15.0, negative_reviews * 7.0)
        risk += contribution; factors.append({"factor": "negative_reviews", "impact": round(contribution,1), "detail": f"{negative_reviews} negative review(s)"})
    if cancelled_count:
        contribution = min(10.0, cancelled_count * 4.0)
        risk += contribution; factors.append({"factor": "appointment_friction", "impact": round(contribution,1), "detail": f"{cancelled_count} cancelled/no-show appointment(s)"})
    if open_customer_opps:
        contribution = min(15.0, sum(4.0 if x.severity == "critical" else 3.0 if x.severity == "high" else 1.5 for x in open_customer_opps))
        risk += contribution; factors.append({"factor": "open_recovery_opportunities", "impact": round(contribution,1), "detail": f"{len(open_customer_opps)} open recovery opportunity(ies)"})
    behavioral_risk = round(min(100.0, risk))
    risk_level = "critical" if behavioral_risk >= 75 else "high" if behavioral_risk >= 50 else "medium" if behavioral_risk >= 25 else "low"
    return {
        "customer": {"id": customer.id, "name": customer.name, "email": customer.email, "phone": customer.phone, "lifecycle_status": customer.lifecycle_status},
        "lifetime_value": decimal_float(lifetime_value),
        "health": {"risk_score": behavioral_risk, "risk_level": risk_level, "baseline_purchase_interval_days": baseline_days, "days_since_last_purchase": days_since_purchase, "factors": factors},
        "leads": [{"id": x.id, "status": x.status, "source": x.source, "value": decimal_float(x.value), "created_at": x.created_at.isoformat()} for x in leads],
        "appointments": [{"id": x.id, "status": x.status, "value": decimal_float(x.value), "scheduled_at": x.scheduled_at.isoformat()} for x in appointments],
        "transactions": [{"id": x.id, "amount": decimal_float(x.amount), "status": x.status, "paid_at": x.paid_at.isoformat()} for x in transactions],
        "invoices": [{"id": x.id, "total": decimal_float(x.total), "amount_due": decimal_float(x.amount_due), "status": x.status, "due_at": x.due_at.isoformat()} for x in invoices],
        "payments": [{"id": x.id, "amount": decimal_float(x.amount), "status": x.status, "failed_at": x.failed_at.isoformat() if x.failed_at else None, "failure_code": x.failure_code} for x in payments],
        "reviews": [{"id": x.id, "source": x.source, "rating": x.rating, "topic": x.topic, "severity": x.severity, "created_at": x.created_at.isoformat()} for x in reviews],
        "calls": [{"id": x.id, "direction": x.direction, "status": x.status, "started_at": x.started_at.isoformat(), "duration_seconds": x.duration_seconds} for x in calls],
        "messages": [{"id": x.id, "channel": x.channel, "direction": x.direction, "status": x.status, "occurred_at": x.occurred_at.isoformat(), "subject": x.subject, "body_preview": x.body_preview, "thread_key": x.thread_key} for x in messages],
        "consents": [{"channel": x.channel, "allowed": x.allowed, "source": x.source, "updated_at": x.updated_at.isoformat()} for x in consents],
        "opportunities": [serialize_opp(x) for x in opportunities],
    }


@app.get("/api/analytics/locations")
def location_analytics(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Location.id, Location.name, func.count(RevenueOpportunity.id), func.coalesce(func.sum(RevenueOpportunity.expected_value), 0), func.coalesce(func.sum(RevenueOpportunity.recovered_amount), 0))
        .join(RevenueOpportunity, (RevenueOpportunity.organization_id == Location.organization_id) & (RevenueOpportunity.location_id == Location.id), isouter=True)
        .where(Location.organization_id == user.organization_id)
        .group_by(Location.id, Location.name)
        .order_by(func.coalesce(func.sum(RevenueOpportunity.expected_value), 0).desc())
    ).all()
    return [{"location_id": r[0], "location": r[1], "opportunities": r[2], "expected_value": decimal_float(r[3]), "recovered_amount": decimal_float(r[4])} for r in rows]


@app.get("/api/analytics/recovery-funnel")
def recovery_funnel(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(RevenueOpportunity.status, func.count(RevenueOpportunity.id), func.coalesce(func.sum(RevenueOpportunity.expected_value), 0), func.coalesce(func.sum(RevenueOpportunity.recovered_amount), 0)).where(RevenueOpportunity.organization_id == user.organization_id).group_by(RevenueOpportunity.status)).all()
    order = ["open","investigating","planned","awaiting_approval","in_recovery","engaged","converted","recovered","lost","dismissed","expired"]
    by = {r[0]: {"status": r[0], "count": r[1], "expected_value": decimal_float(r[2]), "recovered_amount": decimal_float(r[3])} for r in rows}
    return [by[s] for s in order if s in by]


@app.get("/api/analytics/team")
def team_analytics(user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    rows = db.execute(select(User.id, User.email, User.role, func.count(RevenueOpportunity.id), func.coalesce(func.sum(RevenueOpportunity.expected_value), 0), func.coalesce(func.sum(RevenueOpportunity.recovered_amount), 0)).join(RevenueOpportunity, (RevenueOpportunity.organization_id == User.organization_id) & (RevenueOpportunity.assigned_user_id == User.id), isouter=True).where(User.organization_id == user.organization_id).group_by(User.id, User.email, User.role).order_by(func.coalesce(func.sum(RevenueOpportunity.recovered_amount), 0).desc(), User.email)).all()
    return [{"user_id": r[0], "email": r[1], "role": r[2], "opportunities": r[3], "expected_value": decimal_float(r[4]), "recovered_amount": decimal_float(r[5])} for r in rows]


@app.get("/api/analytics/recovery-health")
def recovery_health(user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = build_dashboard(db, user.organization_id)
    active_expected = Decimal(str(d.get("expected_value") or 0))
    recovered = Decimal(str(d.get("actual_recovered") or 0))
    open_rows = db.scalars(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id, RevenueOpportunity.status.in_(OPEN_STATUS := {"open","investigating","planned","awaiting_approval","in_recovery","engaged"}))).all()
    urgent = sum(1 for r in open_rows if r.severity in {"high","critical"})
    ownership = sum(1 for r in open_rows if r.assigned_user_id) / len(open_rows) if open_rows else 1.0
    recovery_conversion = float(recovered / (recovered + active_expected)) if (recovered + active_expected) > 0 else 0.0
    urgency_pressure = urgent / len(open_rows) if open_rows else 0.0
    index = round(max(0, min(100, recovery_conversion * 55 + ownership * 25 + (1 - urgency_pressure) * 20)))
    return {"index": index, "recovery_conversion": round(recovery_conversion,4), "ownership_rate": round(ownership,4), "urgent_pressure": round(urgency_pressure,4), "active_expected_value": decimal_float(active_expected), "actual_recovered": decimal_float(recovered), "open_opportunities": len(open_rows)}


@app.get("/api/analytics")
def analytics(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(RevenueOpportunity.type, func.count(RevenueOpportunity.id), func.sum(RevenueOpportunity.estimated_value), func.sum(RevenueOpportunity.expected_value), func.sum(RevenueOpportunity.recovered_amount)).where(RevenueOpportunity.organization_id == user.organization_id).group_by(RevenueOpportunity.type)).all()
    reviews = db.execute(select(Review.topic, func.count(Review.id)).where(Review.organization_id == user.organization_id).group_by(Review.topic).order_by(func.count(Review.id).desc())).all()
    return {
        "by_type": [{"type": r[0], "count": r[1], "estimated": decimal_float(r[2]), "expected": decimal_float(r[3]), "recovered": decimal_float(r[4])} for r in rows],
        "review_topics": [{"topic": r[0] or "other", "count": r[1]} for r in reviews],
    }


@app.get("/api/analytics/operations")
def operations_analytics(days: int = Query(7, ge=1, le=365), user: User = Depends(current_user), db: Session = Depends(get_db)):
    from .integration_example import get_safety_dashboard_data
    return get_safety_dashboard_data(db, user.organization_id, days)


@app.post("/api/transactions/{tx_id}/attribute/{oid}")
def attribute(tx_id: str, oid: str, classification: str = Query("direct", pattern="^(direct|assisted|influenced|estimated|unattributed)$"), user: User = Depends(require_role("owner", "admin")), db: Session = Depends(get_db)):
    try:
        result = attribute_transaction(db, user.organization_id, tx_id, oid, classification=classification)
        add_audit(db, user.organization_id, "transaction_attributed", "transaction", tx_id, {"opportunity_id": oid, "classification": result}, user.id)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Concurrent attribution conflict; transaction already has a primary attribution")
    return {"classification": result}


@app.get("/api/agents")
def agent_catalog(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ensure_definitions(db, user.organization_id)
    rows = db.scalars(select(AgentDefinition).where(AgentDefinition.organization_id == user.organization_id).order_by(AgentDefinition.key)).all()
    db.commit()
    return [{"id": r.id, "key": r.key, "name": r.name, "description": r.description, "autonomy_mode": r.autonomy_mode, "version": r.version, "status": r.status} for r in rows]


@app.get("/api/runtime/connectors")
def runtime_connectors(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from .connectors import ADAPTERS
    out=[]
    for key, provider_meta in PROVIDERS.items():
        cls=ADAPTERS.get(key)
        contract=adapter_contract(cls) if cls else {"resources":(),"actions":(),"has_sync":False,"has_execute":False}
        integ=db.scalar(select(Integration).where(Integration.organization_id==user.organization_id, Integration.provider==key))
        out.append({"provider":key,"name":provider_meta.name,"adapter":cls.__name__ if cls else None,"adapter_ready":bool(cls),"configured":bool(integ and integ.credentials_ciphertext),"status":integ.status if integ else "not_configured","sync_resources":list(contract["resources"] if isinstance(contract,dict) else []),"catalog_resources":list(SYNC_RESOURCES.get(key,()))})
    return {"count":len(out),"connectors":out}


@app.get("/api/runtime/queue")
def runtime_queue(user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    counts=db.execute(select(IntegrationJob.status, func.count(IntegrationJob.id)).where(IntegrationJob.organization_id==user.organization_id).group_by(IntegrationJob.status)).all()
    return {"counts":{str(k):int(v) for k,v in counts},"workers":int(os.getenv("RROS_WORKERS","4"))}


@app.post("/api/runtime/sync-all")
def runtime_sync_all(user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    return enqueue_due_syncs(db, organization_id=user.organization_id, window_seconds=300)


@app.get("/api/runtime/failures")
def runtime_failures(limit: int = Query(100, ge=1, le=500), user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    jobs = db.scalars(select(IntegrationJob).where(
        IntegrationJob.organization_id == user.organization_id,
        IntegrationJob.status.in_(("failed", "dead_letter")),
    ).order_by(IntegrationJob.updated_at.desc()).limit(limit)).all()
    actions = db.scalars(select(ConnectorAction).where(
        ConnectorAction.organization_id == user.organization_id,
        ConnectorAction.status.in_(("failed", "needs_reconciliation")),
    ).order_by(ConnectorAction.completed_at.desc(), ConnectorAction.created_at.desc()).limit(limit)).all()
    return {"jobs":[{"id":j.id,"provider":j.provider,"type":j.job_type,"resource":j.resource,"status":j.status,"attempts":j.attempts,"error":j.last_error} for j in jobs],"actions":[{"id":a.id,"provider":a.provider,"operation":a.operation,"status":a.status,"attempts":a.attempts,"error":a.error,"result_uncertain":a.result_uncertain,"opportunity_id":a.opportunity_id} for a in actions]}


@app.get("/api/runtime/reconciliation")
def runtime_reconciliation(limit: int = Query(100, ge=1, le=500), user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    actions = db.scalars(select(ConnectorAction).where(
        ConnectorAction.organization_id == user.organization_id, ConnectorAction.status == "needs_reconciliation"
    ).order_by(ConnectorAction.created_at.asc()).limit(limit)).all()
    return [{"id":a.id,"provider":a.provider,"operation":a.operation,"external_id":a.external_id,"request":json.loads(a.request_json or "{}"),"error":a.error,"created_at":a.created_at.isoformat()} for a in actions]


@app.post("/api/opportunities/{oid}/agents/run")
def run_agents(oid: str, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    opp = db.scalar(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id, RevenueOpportunity.id == oid))
    if not opp:
        raise HTTPException(404, "Opportunity not found")
    try:
        result = run_agent_pipeline(db, user.organization_id, opp, user.id)
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Agent pipeline failed: {exc}")


@app.post("/api/opportunities/{oid}/agents/enqueue")
def enqueue_agents(oid: str, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    opp = db.scalar(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id, RevenueOpportunity.id == oid))
    if not opp:
        raise HTTPException(404, "Opportunity not found")
    try:
        bucket = int(datetime.now(timezone.utc).timestamp() // 300)
        job, deduped = enqueue_job(db, organization_id=user.organization_id, provider="rros", job_type="agent_pipeline", resource=opp.id, payload={"opportunity_id": opp.id, "requested_by": user.id, "bucket": bucket}, dedupe_key=f"agent-pipeline:{opp.id}:{bucket}", max_attempts=5)
    except ValueError as exc:
        db.rollback(); raise HTTPException(409, str(exc))
    db.commit()
    return {"status": "queued", "job_id": job.id, "opportunity_id": opp.id, "deduped": deduped}


@app.get("/api/agent-runs/{run_id}")
def agent_run_detail(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    root = db.scalar(select(AgentRun).where(AgentRun.organization_id == user.organization_id, AgentRun.id == run_id))
    if not root:
        raise HTTPException(404, "Agent run not found")
    children = db.scalars(select(AgentRun).where(AgentRun.organization_id == user.organization_id, AgentRun.parent_run_id == run_id).order_by(AgentRun.created_at)).all()
    handoffs = db.scalars(select(AgentHandoff).where(AgentHandoff.organization_id == user.organization_id, AgentHandoff.pipeline_run_id == run_id).order_by(AgentHandoff.created_at)).all()
    memories = db.scalars(select(AgentMemory).where(AgentMemory.organization_id == user.organization_id, AgentMemory.source_run_id == run_id).order_by(AgentMemory.created_at)).all()
    return {"run": {"id": root.id, "opportunity_id": root.opportunity_id, "status": root.status, "pipeline_version": root.pipeline_version, "requested_by": root.requested_by, "created_at": root.created_at.isoformat() if root.created_at else None, "completed_at": root.completed_at.isoformat() if root.completed_at else None, "output": json.loads(root.output_json or "{}")}, "agents": [{"id": x.id, "status": x.status, "agent_definition_id": x.agent_definition_id, "output": json.loads(x.output_json or "{}"), "started_at": x.started_at.isoformat() if x.started_at else None, "completed_at": x.completed_at.isoformat() if x.completed_at else None} for x in children], "handoffs": [{"from": x.from_agent_key, "to": x.to_agent_key, "payload": json.loads(x.payload_json or "{}"), "created_at": x.created_at.isoformat()} for x in handoffs], "memories": [{"type": x.memory_type, "scope_type": x.scope_type, "scope_id": x.scope_id, "confidence": float(x.confidence), "content": json.loads(x.content_json or "{}") } for x in memories]}


class AgentFeedbackRequest(BaseModel):
    agent_key: str = Field(min_length=2, max_length=80)
    rating: int = Field(ge=1, le=5)
    correction: Optional[str] = Field(default=None, max_length=4000)


class LearningPromotionRequest(BaseModel):
    agent_key: str = Field(min_length=2, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    approved: bool = False


class AgentOutcomeRequest(BaseModel):
    outcome: str = Field(min_length=2, max_length=50)
    amount: Decimal = Field(default=Decimal("0"), ge=0)
    metadata: dict = Field(default_factory=dict)


@app.post("/api/agent-runs/{run_id}/feedback")
def agent_feedback(run_id: str, req: AgentFeedbackRequest, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    try:
        row = record_feedback(db, user.organization_id, run_id, req.agent_key, req.rating, req.correction, user.id)
        db.commit()
        return {"id": row.id, "status": "recorded"}
    except ValueError as exc:
        db.rollback(); raise HTTPException(400, str(exc))


@app.post("/api/opportunities/{oid}/agent-outcome")
def agent_outcome(oid: str, req: AgentOutcomeRequest, user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    opp = db.scalar(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == user.organization_id, RevenueOpportunity.id == oid))
    if not opp:
        raise HTTPException(404, "Opportunity not found")
    row = record_outcome(db, user.organization_id, opp, req.outcome, req.amount, req.metadata)
    db.commit()
    return {"id": row.id, "outcome": row.outcome, "amount": float(row.amount)}


@app.get("/api/agent-learning")
def agent_learning(user: User = Depends(require_role("owner", "admin", "manager")), db: Session = Depends(get_db)):
    outcomes = db.scalars(select(AgentOutcome).where(AgentOutcome.organization_id == user.organization_id).order_by(AgentOutcome.created_at.desc()).limit(500)).all()
    feedback = db.scalars(select(AgentFeedback).where(AgentFeedback.organization_id == user.organization_id).order_by(AgentFeedback.created_at.desc()).limit(500)).all()
    by_outcome = {}
    for x in outcomes:
        by_outcome[x.outcome] = by_outcome.get(x.outcome, 0) + 1
    by_agent = {}
    for x in feedback:
        by_agent.setdefault(x.agent_key, []).append(x.rating)
    return {"sample_size": len(outcomes), "outcomes": by_outcome, "agent_feedback": {k: {"count": len(v), "average": round(sum(v)/len(v), 2)} for k,v in by_agent.items()}, "learning_policy": "Signals are advisory; policy/model changes require explicit versioned deployment."}


@app.post("/api/agent-learning/promote")
def promote_agent_learning(req: LearningPromotionRequest, user: User = Depends(require_role("owner")), db: Session = Depends(get_db)):
    if not req.approved:
        raise HTTPException(400, "Explicit owner approval is required")
    feedback = db.scalars(select(AgentFeedback).where(
        AgentFeedback.organization_id == user.organization_id,
        AgentFeedback.agent_key == req.agent_key,
    ).order_by(AgentFeedback.created_at.desc()).limit(20)).all()
    average = sum(row.rating for row in feedback) / len(feedback) if feedback else 0
    if len(feedback) < 3 or average < 4.0:
        raise HTTPException(409, "Learning promotion requires at least three recent human ratings averaging 4.0 or higher")
    add_audit(db, user.organization_id, "agent_learning_promoted", "agent_definition", req.agent_key, {"version": req.version, "feedback_count": len(feedback), "feedback_average": round(average, 2)}, user.id)
    db.commit()
    return {"status": "promoted", "agent_key": req.agent_key, "version": req.version, "requires_deployment": True}
