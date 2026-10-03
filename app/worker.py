"""Durable RROS worker.

Processes DB-backed jobs without holding database locks across network calls.
The database remains the source of truth; provider APIs are treated as external,
fallible systems and every boundary records enough state for replay/reconciliation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .agents import run_agent_pipeline
from .config import settings
from .integration_auth import fresh_credentials, refresh_integration
from .connectors import ADAPTERS, ConnectorError, adapter_for
from .db import SessionLocal
from .models import (
    AgentRun,
    ConnectorAction,
    ConnectorRateState,
    ConnectorReceipt,
    Integration,
    IntegrationEvent,
    IntegrationJob,
    IntegrationSyncState,
    RevenueOpportunity,
)
from .queue import acquire_provider_slot, claim_next, complete_job, enqueue_job, fail_job, worker_id
from .secret_store import decrypt_json
from .services import add_audit, add_event, canonical_event, detect_for_org, jdump, money

log = logging.getLogger("rros.worker")

RETRYABLE_HTTP_HINTS = ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "network error", "unavailable")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, ConnectorError):
        return bool(exc.retryable)
    return not isinstance(exc, (ValueError, KeyError))


def _integration(db: Session, org: str, provider: str) -> Integration:
    row = db.scalar(select(Integration).where(Integration.organization_id == org, Integration.provider == provider))
    if not row or not row.credentials_ciphertext:
        raise ConnectorError(f"{provider} is not configured")
    return row


def _record_provider_health(db: Session, org: str, provider: str, *, ok: bool, status_code: Optional[int] = None, error: Optional[str] = None) -> None:
    state = db.scalar(select(ConnectorRateState).where(
        ConnectorRateState.organization_id == org, ConnectorRateState.provider == provider
    ))
    if not state:
        return
    if ok:
        state.consecutive_failures = 0
        state.circuit_state = "closed"
        state.circuit_open_until = None
        state.last_status_code = status_code
        state.last_error = None
    else:
        state.consecutive_failures = int(state.consecutive_failures or 0) + 1
        state.last_status_code = status_code
        state.last_error = (error or "provider error")[:1000]
        if state.consecutive_failures >= 5:
            state.circuit_state = "open"
            state.circuit_open_until = _now().replace(microsecond=0)
            from datetime import timedelta
            state.circuit_open_until += timedelta(seconds=min(300, 2 ** min(state.consecutive_failures - 5, 8)))
    state.updated_at = _now()
    db.commit()


def _queue_detect(db: Session, org: str, provider: str, reason: str) -> None:
    # Collapse bursts of provider records into one detection job per 30 seconds.
    bucket = int(_now().timestamp() // 30)
    enqueue_job(
        db, organization_id=org, provider=provider, job_type="detect_org",
        payload={"reason": reason, "bucket": bucket},
        dedupe_key=f"detect:{org}:{bucket}", max_attempts=6,
    )


def _queue_agent_for_new_opportunities(db: Session, org: str) -> int:
    rows = db.scalars(select(RevenueOpportunity).where(
        RevenueOpportunity.organization_id == org,
        RevenueOpportunity.status.in_(["open", "investigating", "planned", "awaiting_approval", "in_recovery", "engaged"]),
    ).order_by(RevenueOpportunity.detected_at.desc()).limit(250)).all()
    queued = 0
    for opp in rows:
        has_success = db.scalar(select(AgentRun.id).where(
            AgentRun.organization_id == org, AgentRun.opportunity_id == opp.id, AgentRun.status == "succeeded"
        ).limit(1))
        if has_success:
            continue
        bucket = int(_now().timestamp() // 300)
        _, deduped = enqueue_job(
            db, organization_id=org, provider="rros", job_type="agent_pipeline",
            resource=opp.id, payload={"opportunity_id": opp.id},
            dedupe_key=f"agent:{opp.id}:{bucket}", max_attempts=4,
        )
        if not deduped:
            queued += 1
    if queued:
        db.commit()
    return queued


def _save_sync_event(db: Session, org: str, integration: Integration, provider: str, ev: dict[str, Any], *, resource: str, run_id: str) -> tuple[bool, Optional[str], Optional[int]]:
    """Persist the provider record and apply its canonical event atomically per record.

    The integration-event inbox row survives canonicalization failures so failed records
    can be replayed later without losing the original provider payload.
    """
    external_id = str(ev.get("external_id") or "") or None
    payload = ev.get("data") or {}
    payload_json = jdump(payload)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    event_key = f"sync:{resource}:{external_id or payload_hash[:32]}:{payload_hash[:16]}"
    existing = db.scalar(select(IntegrationEvent).where(
        IntegrationEvent.organization_id == org,
        IntegrationEvent.provider == provider,
        IntegrationEvent.external_id == event_key,
    ))
    if existing:
        if existing.payload_hash and existing.payload_hash != payload_hash:
            raise ValueError("Provider reused an external event ID with a different payload")
        if existing.processed:
            return False, existing.event_type, existing.id
    else:
        try:
            with db.begin_nested():
                existing = IntegrationEvent(
                    organization_id=org,
                    integration_id=integration.id,
                    provider=provider,
                    external_id=event_key,
                    event_type=str(ev.get("type") or "unknown"),
                    payload_json=payload_json,
                    payload_hash=payload_hash,
                )
                db.add(existing)
                db.flush()
        except IntegrityError:
            existing = db.scalar(select(IntegrationEvent).where(
                IntegrationEvent.organization_id == org,
                IntegrationEvent.provider == provider,
                IntegrationEvent.external_id == event_key,
            ))
            if not existing:
                raise
            if existing.payload_hash != payload_hash:
                raise ValueError("Provider reused an external event ID with a different payload")
            if existing.processed:
                return False, existing.event_type, existing.id

    try:
        # Isolate domain writes to a savepoint. The inbox row itself must remain durable
        # even when canonicalization fails, so the event can be replayed.
        with db.begin_nested():
            canonical_event(db, org, provider, existing.event_type, payload, payload.get("occurred_at"))
        existing.processed = True
        existing.processed_at = _now()
        existing.error = None
        db.flush()
        return True, existing.event_type, existing.id
    except Exception as exc:
        existing.processed = False
        existing.error = str(exc)[:1000]
        db.flush()
        return False, existing.event_type, existing.id


def _queue_integration_event_retry(db: Session, event: IntegrationEvent) -> None:
    enqueue_job(
        db, organization_id=event.organization_id, integration_id=event.integration_id,
        provider=event.provider, job_type="integration_event", resource=str(event.id),
        payload={"event_id": event.id}, dedupe_key=f"event:{event.organization_id}:{event.id}",
        max_attempts=8,
    )


async def _integration_event_job(db: Session, job: IntegrationJob) -> dict[str, Any]:
    event_id = int(job.resource or 0)
    event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.organization_id == job.organization_id, IntegrationEvent.id == event_id))
    if not event:
        raise ValueError("Integration event not found")
    if event.processed:
        return {"status": "succeeded", "idempotent": True, "event_id": event.id}
    payload = _safe_json(json.loads(event.payload_json or "{}"))
    try:
        result = canonical_event(db, job.organization_id, event.provider, event.event_type, payload, payload.get("occurred_at"))
        event.processed = True
        event.processed_at = _now()
        event.error = None
        db.commit()
        _queue_detect(db, job.organization_id, event.provider, f"integration_event:{event.id}")
        db.commit()
        return {"status": "succeeded", "event_id": event.id, "result": result}
    except Exception as exc:
        db.rollback()
        event = db.scalar(select(IntegrationEvent).where(IntegrationEvent.organization_id == job.organization_id, IntegrationEvent.id == event_id))
        event.error = str(exc)[:1000]
        event.processed = False
        db.commit()
        raise


async def _sync_job(db: Session, job: IntegrationJob) -> dict[str, Any]:
    integration = db.scalar(select(Integration).where(
        Integration.organization_id == job.organization_id,
        Integration.id == job.integration_id,
    ))
    if not integration or not integration.credentials_ciphertext:
        raise ConnectorError(f"{job.provider} is not configured")
    config = json.loads(integration.config_json or "{}")
    creds = await fresh_credentials(db, job.organization_id, integration)
    adapter = adapter_for(job.provider, creds, config)
    sync_state = db.scalar(select(IntegrationSyncState).where(
        IntegrationSyncState.organization_id == job.organization_id,
        IntegrationSyncState.integration_id == integration.id,
        IntegrationSyncState.resource == job.resource,
    ))
    if not sync_state:
        sync_state = IntegrationSyncState(
            organization_id=job.organization_id,
            integration_id=integration.id,
            resource=str(job.resource),
            status="running",
            last_started_at=_now(),
        )
        db.add(sync_state)
    else:
        sync_state.status = "running"
        sync_state.last_started_at = _now()
        sync_state.last_error = None
    db.commit()
    configured_limit = int(config.get("rate_limit_per_minute", 60)) if isinstance(config, dict) else 60
    allowed, retry_after = acquire_provider_slot(db, organization_id=job.organization_id, provider=job.provider, rate_limit_per_minute=configured_limit)
    if not allowed:
        raise ConnectorError(f"{job.provider} local rate budget exhausted; retry after {retry_after:.2f}s", retryable=True)

    received = applied = failed = 0
    payload_job = _safe_json(json.loads(job.payload_json or "{}"))
    run_id = str(payload_job.get("run_id") or job.id)
    candidate_cursor = payload_job.get("cursor")
    sync_cursor = candidate_cursor if candidate_cursor not in (None, "") else sync_state.cursor
    try:
        try:
            result = await adapter.sync(str(job.resource), cursor=sync_cursor)
        except ConnectorError as first_exc:
            if first_exc.status_code == 401:
                await adapter.close()
                db.rollback()
                creds = await refresh_integration(db, job.organization_id, integration.id, force=True)
                adapter = adapter_for(job.provider, creds, config)
                result = await adapter.sync(str(job.resource), cursor=sync_cursor)
            else:
                raise
        _record_provider_health(db, job.organization_id, job.provider, ok=True)
        received = len(result.records)
        for ev in result.records:
            changed, _, event_id = _save_sync_event(
                db, job.organization_id, integration, job.provider, ev,
                resource=str(job.resource), run_id=run_id,
            )
            if changed:
                applied += 1
            event = db.get(IntegrationEvent, event_id) if event_id else None
            if event and not event.processed:
                failed += 1
                _queue_integration_event_retry(db, event)
        sync_state = db.scalar(select(IntegrationSyncState).where(
            IntegrationSyncState.organization_id == job.organization_id,
            IntegrationSyncState.id == sync_state.id,
        ))
        sync_state.cursor = result.next_cursor
        sync_state.records_synced = int(sync_state.records_synced or 0) + applied
        sync_state.records_failed = int(sync_state.records_failed or 0) + failed
        sync_state.last_error_count = failed
        sync_state.status = "succeeded" if failed == 0 else "failed"
        sync_state.last_completed_at = _now()
        sync_interval = max(30, min(86400, int(config.get("sync_interval_seconds", settings.sync_interval_seconds) if isinstance(config, dict) else settings.sync_interval_seconds)))
        # Page continuations are queued immediately; only the completed resource gets
        # a next scheduled run. This prevents the scheduler from re-reading page 1
        # every tick while still keeping incremental syncs recurring.
        sync_state.next_run_at = (_now() + timedelta(seconds=sync_interval)) if not result.next_cursor else _now()
        sync_state.last_error = None if failed == 0 else f"{failed} record(s) failed canonical processing"
        integration.last_sync_at = _now()
        integration.status = "connected" if failed == 0 else "error"
        integration.last_error = None if failed == 0 else sync_state.last_error
        if result.next_cursor:
            cursor_hash = hashlib.sha256(str(result.next_cursor).encode()).hexdigest()[:24]
            enqueue_job(
                db, organization_id=job.organization_id, integration_id=integration.id, provider=job.provider,
                job_type="connector_sync", resource=str(job.resource),
                payload={"provider": job.provider, "resource": job.resource, "cursor": result.next_cursor, "run_id": run_id},
                dedupe_key=f"syncpage:{job.organization_id}:{integration.id}:{job.resource}:{run_id}:{cursor_hash}",
                max_attempts=8,
            )
        db.commit()
        _queue_detect(db, job.organization_id, job.provider, f"sync:{job.resource}")
        db.commit()
        return {
            "provider": job.provider,
            "resource": job.resource,
            "received": received,
            "applied": applied,
            "failed": failed,
            "next_cursor": result.next_cursor,
            "status": sync_state.status,
        }
    except ConnectorError as exc:
        db.rollback()
        state = db.scalar(select(IntegrationSyncState).where(
            IntegrationSyncState.organization_id == job.organization_id,
            IntegrationSyncState.integration_id == integration.id,
            IntegrationSyncState.resource == job.resource,
        ))
        if state:
            state.status = "retrying" if exc.retryable else "failed"
            state.last_error = str(exc)[:1000]
            state.last_error_count = max(1, int(state.last_error_count or 0))
            state.last_completed_at = _now()
        integration = db.scalar(select(Integration).where(
            Integration.organization_id == job.organization_id, Integration.id == integration.id,
        ))
        if integration:
            integration.status = "error"
            integration.last_error = str(exc)[:1000]
        _record_provider_health(db, job.organization_id, job.provider, ok=False, status_code=exc.status_code, error=str(exc))
        db.commit()
        raise
    except Exception as exc:
        db.rollback()
        state = db.scalar(select(IntegrationSyncState).where(
            IntegrationSyncState.organization_id == job.organization_id,
            IntegrationSyncState.integration_id == integration.id,
            IntegrationSyncState.resource == job.resource,
        ))
        if state:
            state.status = "failed"
            state.last_error = str(exc)[:1000]
            state.last_error_count = max(1, int(state.last_error_count or 0))
            state.last_completed_at = _now()
        integration = db.scalar(select(Integration).where(
            Integration.organization_id == job.organization_id, Integration.id == integration.id,
        ))
        if integration:
            integration.status = "error"
            integration.last_error = str(exc)[:1000]
        db.commit()
        raise
    finally:
        await adapter.close()


async def _action_job(db: Session, job: IntegrationJob) -> dict[str, Any]:
    action = db.scalar(select(ConnectorAction).where(
        ConnectorAction.organization_id == job.organization_id,
        ConnectorAction.id == job.connector_action_id,
    ))
    if not action:
        raise ValueError("Connector action not found")
    if action.status == "succeeded":
        return {"status": "succeeded", "external_id": action.external_id, "idempotent": True}
    if action.status == "needs_reconciliation":
        return {"status": "needs_reconciliation", "external_id": action.external_id, "idempotent": True, "reconciliation_required": True}
    integration = _integration(db, job.organization_id, action.provider)
    config = json.loads(integration.config_json or "{}")
    creds = await fresh_credentials(db, job.organization_id, integration)
    adapter = adapter_for(action.provider, creds, config)
    try:
        action.status = "running"
        action.started_at = _now()
        action.attempts = int(action.attempts or 0) + 1
        db.commit()
        configured_limit = int(config.get("rate_limit_per_minute", 60)) if isinstance(config, dict) else 60
        allowed, retry_after = acquire_provider_slot(db, organization_id=job.organization_id, provider=action.provider, rate_limit_per_minute=configured_limit)
        if not allowed:
            action = db.scalar(select(ConnectorAction).where(ConnectorAction.organization_id == job.organization_id, ConnectorAction.id == action.id))
            action.status = "retrying"
            action.error = f"local rate budget exhausted; retry after {retry_after:.2f}s"
            db.commit()
            raise ConnectorError(action.error, retryable=True)
        args = _safe_json(json.loads(action.request_json or "{}"))
        try:
            result = await adapter.execute(action.operation, args, idempotency_key=action.idempotency_key)
        except ConnectorError as first_exc:
            # A 401 is an explicit provider rejection, so no outbound side effect was
            # accepted by that request. Refresh once and replay the same idempotent
            # action; non-401 ambiguous failures still go to reconciliation.
            if first_exc.status_code == 401:
                await adapter.close()
                db.rollback()
                creds = await refresh_integration(db, job.organization_id, integration.id, force=True)
                adapter = adapter_for(action.provider, creds, config)
                result = await adapter.execute(action.operation, args, idempotency_key=action.idempotency_key)
            else:
                raise

        # CRITICAL OUTBOUND BOUNDARY: persist the provider success before any
        # canonicalization/audit work. A provider-side success must never be retried
        # merely because our post-processing failed.
        action = db.scalar(select(ConnectorAction).where(ConnectorAction.organization_id == job.organization_id, ConnectorAction.id == action.id))
        action.status = "succeeded"
        action.external_id = result.external_id
        action.response_json = jdump(result.raw or {"external_id": result.external_id})
        action.error = None
        action.result_uncertain = False
        action.completed_at = _now()
        integration.status = "connected"
        integration.last_error = None
        integration.health_checked_at = _now()
        _record_provider_health(db, job.organization_id, action.provider, ok=True)
        receipt_payload = result.raw or {"external_id": result.external_id}
        response_json = jdump(receipt_payload)
        response_hash = hashlib.sha256(response_json.encode()).hexdigest()
        receipt = db.scalar(select(ConnectorReceipt).where(
            ConnectorReceipt.organization_id == job.organization_id,
            ConnectorReceipt.action_id == action.id,
        ))
        if not receipt:
            receipt = ConnectorReceipt(
                id="rcpt_" + __import__("uuid").uuid4().hex,
                organization_id=job.organization_id,
                integration_id=integration.id,
                action_id=action.id,
                provider=action.provider,
                operation=action.operation,
                external_id=result.external_id,
                status="succeeded",
                provider_status=str((receipt_payload or {}).get("status") or (receipt_payload or {}).get("Status") or "succeeded")[:60],
                response_hash=response_hash,
                response_json=response_json,
            )
            db.add(receipt)
        db.commit()

        # Post-provider reconciliation is explicitly a separate transaction.
        canonicalization_pending = False
        if action.operation in {"send.sms", "send.whatsapp", "send.email"} and result.external_id:
            try:
                args_for_event = _safe_json(json.loads(action.request_json or "{}"))
                channel = "whatsapp" if action.operation == "send.whatsapp" else "sms" if action.operation == "send.sms" else "email"
                event_payload = {
                    "external_id": str(result.external_id),
                    "provider": action.provider,
                    "channel": channel,
                    "customer_email": args_for_event.get("to") if channel == "email" else None,
                    "customer_phone": args_for_event.get("to") if channel in {"sms", "whatsapp"} else None,
                    "body_preview": args_for_event.get("body"),
                    "subject": args_for_event.get("subject"),
                    "status": "sent",
                    "occurred_at": _now().isoformat(),
                }
                with db.begin_nested():
                    canonical_event(db, job.organization_id, action.provider, "communication.sent", event_payload, _now())
                if action.opportunity_id:
                    add_event(db, job.organization_id, action.opportunity_id, "connector_action_succeeded", "connector", {
                        "connector_action_id": action.id, "provider": action.provider, "operation": action.operation, "external_id": result.external_id,
                    })
                    add_audit(db, job.organization_id, "connector_action_succeeded", "connector_action", action.id, {
                        "opportunity_id": action.opportunity_id, "provider": action.provider, "operation": action.operation, "external_id": result.external_id,
                    })
                db.commit()
            except Exception as exc:
                canonicalization_pending = True
                db.rollback()
                event_key = f"action:{action.id}:{action.operation}:{result.external_id}"
                payload_hash = hashlib.sha256(jdump(event_payload).encode()).hexdigest()
                event = db.scalar(select(IntegrationEvent).where(
                    IntegrationEvent.organization_id == job.organization_id,
                    IntegrationEvent.provider == action.provider,
                    IntegrationEvent.external_id == event_key,
                ))
                if not event:
                    event = IntegrationEvent(
                        organization_id=job.organization_id,
                        integration_id=integration.id,
                        provider=action.provider,
                        external_id=event_key,
                        event_type="communication.sent",
                        payload_json=jdump(event_payload),
                        payload_hash=payload_hash,
                        processed=False,
                        error=str(exc)[:1000],
                    )
                    db.add(event); db.flush()
                else:
                    event.processed = False; event.error = str(exc)[:1000]
                _queue_integration_event_retry(db, event)
                db.commit()

        return {
            "status": "succeeded",
            "external_id": result.external_id,
            "idempotent": False,
            "receipt_id": receipt.id if receipt else None,
            "canonicalization_pending": canonicalization_pending,
        }
    except Exception as exc:
        db.rollback()
        if isinstance(exc, ConnectorError):
            _record_provider_health(db, job.organization_id, action.provider, ok=False, status_code=exc.status_code, error=str(exc))
        action = db.scalar(select(ConnectorAction).where(ConnectorAction.organization_id == job.organization_id, ConnectorAction.id == action.id))
        if action:
            if isinstance(exc, ConnectorError) and exc.ambiguous:
                action.status = "needs_reconciliation"
                action.result_uncertain = True
                action.error = str(exc)[:1000]
            elif isinstance(exc, ConnectorError) and exc.retryable:
                action.status = "retrying"
                action.error = str(exc)[:1000]
            else:
                action.status = "failed"
                action.error = str(exc)[:1000]
            db.commit()
        raise
    finally:
        await adapter.close()


async def _detect_job(db: Session, job: IntegrationJob) -> dict[str, Any]:
    result = detect_for_org(db, job.organization_id)
    queued_agents = _queue_agent_for_new_opportunities(db, job.organization_id)
    return {**(result or {"organization_id": job.organization_id}), "agent_jobs_queued": queued_agents}


async def _agent_job(db: Session, job: IntegrationJob) -> dict[str, Any]:
    opp = db.scalar(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == job.organization_id, RevenueOpportunity.id == job.resource))
    if not opp:
        raise ValueError("Opportunity not found for agent job")
    result = run_agent_pipeline(db, job.organization_id, opp, requested_by=None)
    db.commit()
    return result


async def process(job: IntegrationJob) -> dict[str, Any]:
    db = SessionLocal()
    try:
        if job.job_type == "connector_sync":
            result = await _sync_job(db, job)
        elif job.job_type == "connector_action":
            result = await _action_job(db, job)
        elif job.job_type == "detect_org":
            result = await _detect_job(db, job)
        elif job.job_type == "agent_pipeline":
            result = await _agent_job(db, job)
        elif job.job_type == "integration_event":
            result = await _integration_event_job(db, job)
        else:
            raise ValueError(f"Unsupported job type: {job.job_type}")
        return result
    finally:
        db.close()


async def _heartbeat_loop(job_id: str, worker_name: str, lease_seconds: int) -> None:
    interval = max(5.0, min(30.0, lease_seconds / 3))
    while True:
        await asyncio.sleep(interval)
        db = SessionLocal()
        try:
            from .queue import heartbeat
            heartbeat(db, job_id, worker_name, lease_seconds=lease_seconds)
        except Exception:
            log.exception("heartbeat failed job_id=%s", job_id)
        finally:
            db.close()


async def worker_loop(worker_name: Optional[str] = None, poll_seconds: Optional[float] = None, lease_seconds: Optional[int] = None) -> None:
    poll_seconds = float(poll_seconds if poll_seconds is not None else os.getenv("RROS_WORKER_POLL_SECONDS", "0.5"))
    lease_seconds = int(lease_seconds if lease_seconds is not None else os.getenv("RROS_WORKER_LEASE_SECONDS", "300"))
    wid = worker_name or worker_id()
    while True:
        db = SessionLocal()
        claimed = None
        try:
            claimed = claim_next(db, worker_id=wid, lease_seconds=lease_seconds)
        except Exception:
            log.exception("job claim failed")
        finally:
            db.close()
        if not claimed:
            await asyncio.sleep(poll_seconds)
            continue
        heartbeat_task = asyncio.create_task(_heartbeat_loop(claimed.id, wid, lease_seconds))
        try:
            result = await process(claimed)
            db2 = SessionLocal()
            try:
                complete_job(db2, claimed.id, wid, result)
            finally:
                db2.close()
        except Exception as exc:
            db2 = SessionLocal()
            try:
                retry = _is_retryable_error(exc)
                fail_job(db2, claimed.id, wid, str(exc), retryable=retry or not isinstance(exc, ValueError))
            finally:
                db2.close()
            log.exception("job failed job_id=%s type=%s", claimed.id, claimed.job_type)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass



async def run_workers(count: int = 1) -> None:
    count = max(1, min(64, int(count)))
    await asyncio.gather(*(worker_loop(worker_name=f"{worker_id('rros')}-{idx}") for idx in range(count)))


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    count = int(os.getenv("RROS_WORKERS", "4"))
    asyncio.run(run_workers(count))
