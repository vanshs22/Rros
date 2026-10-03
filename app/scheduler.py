"""Durable connector scheduler.

Only queues work; it never calls a provider API directly. This keeps scheduling
cheap and makes all actual network operations flow through the leased worker pool.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Integration, IntegrationJob, IntegrationSyncState
from .queue import enqueue_job
from .runtime import SYNC_RESOURCES

ACTIVE_JOB_STATUSES = ("queued", "running", "retrying")

def now():
    return datetime.now(timezone.utc)

def enqueue_due_syncs(db: Session, *, organization_id: Optional[str] = None, window_seconds: int = 60) -> dict:
    q = select(Integration).where(Integration.status.in_(("configured", "connected", "error")), Integration.credentials_ciphertext.is_not(None))
    if organization_id:
        q = q.where(Integration.organization_id == organization_id)
    integrations = db.scalars(q).all()
    queued = []
    skipped = 0
    bucket = int(now().timestamp() // max(1, window_seconds))
    for integ in integrations:
        for resource in SYNC_RESOURCES.get(integ.provider, ()):
            active = db.scalar(select(IntegrationJob.id).where(
                IntegrationJob.organization_id == integ.organization_id,
                IntegrationJob.integration_id == integ.id,
                IntegrationJob.provider == integ.provider,
                IntegrationJob.resource == resource,
                IntegrationJob.job_type == "connector_sync",
                IntegrationJob.status.in_(ACTIVE_JOB_STATUSES),
            ).limit(1))
            if active:
                skipped += 1
                continue
            sync_state = db.scalar(select(IntegrationSyncState).where(
                IntegrationSyncState.organization_id == integ.organization_id, IntegrationSyncState.integration_id == integ.id,
                IntegrationSyncState.resource == resource
            ))
            if sync_state and sync_state.next_run_at and sync_state.next_run_at > now():
                skipped += 1
                continue
            run_id = uuid.uuid4().hex
            job, deduped = enqueue_job(
                db, organization_id=integ.organization_id, integration_id=integ.id, provider=integ.provider,
                job_type="connector_sync", resource=resource,
                payload={"provider": integ.provider, "resource": resource, "cursor": (sync_state.cursor if sync_state else None), "run_id": run_id, "scheduled_bucket": bucket},
                dedupe_key=f"scheduled-sync:{integ.organization_id}:{integ.id}:{resource}:{bucket}", max_attempts=8,
            )
            queued.append({"provider": integ.provider, "resource": resource, "job_id": job.id, "deduped": deduped})
    db.commit()
    return {"queued": queued, "count": len(queued), "skipped": skipped, "bucket": bucket}

async def scheduler_loop(interval_seconds: int = 60) -> None:
    import asyncio
    from .db import SessionLocal
    interval = max(10, int(interval_seconds))
    while True:
        db = SessionLocal()
        try:
            enqueue_due_syncs(db, window_seconds=interval)
        except Exception:
            db.rollback()
        finally:
            db.close()
        await asyncio.sleep(interval)

if __name__ == "__main__":
    import asyncio, os
    asyncio.run(scheduler_loop(int(os.getenv("RROS_SCHEDULER_INTERVAL_SECONDS", "60"))))
