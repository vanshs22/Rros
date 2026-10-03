"""Database-backed durable job queue used for connector, detection, and agent work.

The queue is intentionally database-first so the application has a single source of
truth and can run with PostgreSQL in production without requiring Redis for correctness.
Redis can still be used as an optional wake-up/latency optimization later.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import ConnectorAction, ConnectorRateState, IntegrationJob
from .services import jdump

TERMINAL = {"succeeded", "failed", "dead_letter", "cancelled"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def enqueue_job(
    db: Session,
    *,
    organization_id: str,
    job_type: str,
    provider: str,
    payload: dict[str, Any] | None = None,
    dedupe_key: str,
    integration_id: Optional[str] = None,
    connector_action_id: Optional[str] = None,
    resource: Optional[str] = None,
    max_attempts: int = 8,
    available_at: datetime | None = None,
) -> tuple[IntegrationJob, bool]:
    payload = payload or {}
    req_hash = request_hash(payload)
    existing = db.scalar(select(IntegrationJob).where(IntegrationJob.organization_id == organization_id, IntegrationJob.dedupe_key == dedupe_key))
    if existing:
        if existing.request_hash != req_hash:
            raise ValueError("Job dedupe key is already used with different payload")
        if existing.status in TERMINAL:
            return existing, True
        return existing, True
    row = IntegrationJob(
        id="job_" + __import__("uuid").uuid4().hex,
        organization_id=organization_id,
        integration_id=integration_id,
        connector_action_id=connector_action_id,
        provider=provider,
        job_type=job_type,
        resource=resource,
        payload_json=jdump(payload),
        dedupe_key=dedupe_key,
        request_hash=req_hash,
        status="queued",
        attempts=0,
        max_attempts=max(1, min(100, int(max_attempts))),
        available_at=available_at or utcnow(),
        updated_at=utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row, False
    except IntegrityError:
        existing = db.scalar(select(IntegrationJob).where(IntegrationJob.organization_id == organization_id, IntegrationJob.dedupe_key == dedupe_key))
        if not existing:
            if job_type == "connector_sync":
                existing = db.scalar(select(IntegrationJob).where(
                    IntegrationJob.organization_id == organization_id,
                    IntegrationJob.integration_id == integration_id,
                    IntegrationJob.provider == provider,
                    IntegrationJob.resource == resource,
                    IntegrationJob.job_type == job_type,
                    IntegrationJob.status.in_(["queued", "running", "retrying"]),
                ).order_by(IntegrationJob.created_at).limit(1))
            elif job_type == "agent_pipeline":
                existing = db.scalar(select(IntegrationJob).where(
                    IntegrationJob.organization_id == organization_id,
                    IntegrationJob.resource == resource,
                    IntegrationJob.job_type == job_type,
                    IntegrationJob.status.in_(["queued", "running", "retrying"]),
                ).order_by(IntegrationJob.created_at).limit(1))
        if not existing:
            raise
        if existing.request_hash != req_hash and job_type not in {"connector_sync", "agent_pipeline"}:
            raise ValueError("Job dedupe key is already used with different payload")
        return existing, True


def recover_stale_jobs(db: Session, *, lease_seconds: int = 300, limit: int = 500) -> int:
    now = utcnow()
    cutoff = now - timedelta(seconds=lease_seconds)
    stmt = select(IntegrationJob).where(
        IntegrationJob.status == "running",
        ((IntegrationJob.lease_until < now) | (IntegrationJob.locked_at < cutoff)),
    ).limit(limit)
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    rows = db.scalars(stmt).all()
    for row in rows:
        if row.job_type == "connector_action" and row.connector_action_id:
            action = db.scalar(select(ConnectorAction).where(
                ConnectorAction.organization_id == row.organization_id,
                ConnectorAction.id == row.connector_action_id,
            ))
            if action and action.status == "running":
                # A worker can die after the provider accepted a non-idempotent request
                # but before our receipt committed. Never blindly replay that action.
                action.status = "needs_reconciliation"
                action.result_uncertain = True
                action.error = "Worker lease expired during outbound execution; provider result may be unknown"
        row.status = "retrying" if int(row.attempts or 0) < int(row.max_attempts or 8) else "dead_letter"
        row.available_at = now
        row.locked_by = None
        row.locked_at = None
        row.lease_until = None
        row.last_error = "worker lease expired; job reclaimed"
        row.updated_at = now
    if rows:
        db.commit()
    return len(rows)


def claim_next(db: Session, *, worker_id: str, lease_seconds: int = 300) -> IntegrationJob | None:
    now = utcnow()
    recover_stale_jobs(db, lease_seconds=lease_seconds, limit=50)
    # Fair scheduling: inspect a bounded FIFO window and skip tenants that have
    # already reached their concurrency budget. This prevents one large tenant
    # from monopolising all workers while preserving durable DB ownership.
    candidates = db.scalars(
        select(IntegrationJob)
        .where(
            IntegrationJob.status.in_(["queued", "retrying"]),
            IntegrationJob.available_at <= now,
        )
        .order_by(IntegrationJob.created_at, IntegrationJob.id)
        .limit(100)
    ).all()
    row = None
    tenant_limit = max(1, int(getattr(settings, "queue_tenant_concurrency", 8)))
    for candidate in candidates:
        running = db.scalar(select(func.count(IntegrationJob.id)).where(
            IntegrationJob.organization_id == candidate.organization_id,
            IntegrationJob.status == "running",
        )) or 0
        if int(running) >= tenant_limit:
            continue
        lock_stmt = select(IntegrationJob).where(
            IntegrationJob.id == candidate.id,
            IntegrationJob.status.in_(["queued", "retrying"]),
            IntegrationJob.available_at <= now,
        )
        if db.bind and db.bind.dialect.name == "postgresql":
            lock_stmt = lock_stmt.with_for_update(skip_locked=True)
        else:
            lock_stmt = lock_stmt.with_for_update()
        row = db.scalars(lock_stmt).first()
        if row:
            break
    if not row:
        return None
    row.status = "running"
    row.attempts = int(row.attempts or 0) + 1
    row.locked_by = worker_id
    row.locked_at = now
    row.lease_until = now + timedelta(seconds=lease_seconds)
    row.last_heartbeat_at = now
    row.updated_at = now
    db.commit()
    return row


def heartbeat(db: Session, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> bool:
    now = utcnow()
    row = db.scalar(select(IntegrationJob).where(IntegrationJob.id == job_id, IntegrationJob.locked_by == worker_id, IntegrationJob.status == "running"))
    if not row:
        return False
    row.last_heartbeat_at = now
    row.lease_until = now + timedelta(seconds=lease_seconds)
    row.updated_at = now
    db.commit()
    return True


def complete_job(db: Session, job_id: str, worker_id: str, result: dict[str, Any] | None = None) -> bool:
    row = db.scalar(select(IntegrationJob).where(IntegrationJob.id == job_id, IntegrationJob.locked_by == worker_id, IntegrationJob.status == "running"))
    if not row:
        return False
    row.status = "succeeded"
    row.result_json = jdump(result or {})
    row.last_error = None
    row.completed_at = utcnow()
    row.updated_at = row.completed_at
    row.locked_by = row.locked_at = row.lease_until = row.last_heartbeat_at = None
    db.commit()
    return True


def fail_job(db: Session, job_id: str, worker_id: str, error: str, *, retryable: bool = True) -> str:
    row = db.scalar(select(IntegrationJob).where(IntegrationJob.id == job_id, IntegrationJob.locked_by == worker_id, IntegrationJob.status == "running"))
    if not row:
        return "missing"
    attempts = int(row.attempts or 0)
    now = utcnow()
    if retryable and attempts < int(row.max_attempts or 8):
        delay = min(3600, 2 ** min(attempts, 10))
        row.status = "retrying"
        row.available_at = now + timedelta(seconds=delay)
        result = "retrying"
    else:
        row.status = "dead_letter"
        row.completed_at = now
        result = "dead_letter"
    row.last_error = str(error)[:1000]
    row.updated_at = now
    row.locked_by = row.locked_at = row.lease_until = row.last_heartbeat_at = None
    db.commit()
    return result



def acquire_provider_slot(db: Session, *, organization_id: str, provider: str, rate_limit_per_minute: int = 60) -> tuple[bool, float]:
    """Reserve one provider request token using a short DB transaction."""
    rate = max(1, min(10000, int(rate_limit_per_minute)))
    now = utcnow()
    stmt = select(ConnectorRateState).where(
        ConnectorRateState.organization_id == organization_id,
        ConnectorRateState.provider == provider,
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    row = db.scalar(stmt)
    if row and row.circuit_state == "open":
        until = row.circuit_open_until
        until = until.replace(tzinfo=timezone.utc) if until and until.tzinfo is None else until
        if until and until > now:
            db.rollback()
            return False, max(0.25, (until - now).total_seconds())
        row.circuit_state = "half_open"
        row.updated_at = now
        db.commit()
        # A single half-open request is allowed to probe provider health.
    if not row:
        row = ConnectorRateState(
            id="rl_" + __import__("uuid").uuid4().hex,
            organization_id=organization_id, provider=provider,
            rate_limit_per_minute=rate, tokens_available=Decimal(rate), last_refill_at=now,
        )
        try:
            with db.begin_nested():
                db.add(row); db.flush()
        except IntegrityError:
            row = db.scalar(select(ConnectorRateState).where(ConnectorRateState.organization_id == organization_id, ConnectorRateState.provider == provider))
            if not row:
                raise
    row.rate_limit_per_minute = rate
    last = row.last_refill_at.replace(tzinfo=timezone.utc) if row.last_refill_at and row.last_refill_at.tzinfo is None else (row.last_refill_at or now)
    elapsed = max(0.0, (now - last).total_seconds())
    row.tokens_available = min(Decimal(rate), Decimal(str(row.tokens_available or 0)) + Decimal(str(elapsed * rate / 60.0)))
    row.last_refill_at = now
    if row.tokens_available >= 1:
        row.tokens_available -= 1
        row.updated_at = now
        db.commit()
        return True, 0.0
    missing = float(1 - row.tokens_available)
    wait = max(0.25, missing * 60.0 / rate)
    row.updated_at = now
    db.commit()
    return False, wait

def worker_id(prefix: str = "rros-worker") -> str:
    return f"{prefix}-{socket.gethostname()}-{os.getpid()}"
