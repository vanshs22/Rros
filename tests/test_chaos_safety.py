"""Deterministic, provider-free chaos checks for durable safety invariants.

These tests inject faults at the adapter and queue boundaries only. They never
contact a provider, kill a real process, or mutate a non-test database.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.connector_core import BaseAdapter, ConnectorError
from app.models import ConnectorAction, Integration
from app.queue import claim_next, complete_job, enqueue_job, heartbeat, recover_stale_jobs, utcnow


def isolated_client(tmp_path) -> TestClient:
    """Build a disposable SQLite API instance with live actions disabled."""
    import app.config as cfg
    import app.db as dbmod
    import app.main as main
    import app.security as security
    import app.queue as queue

    cfg.settings = cfg.Settings(
        database_url=f"sqlite:///{tmp_path / 'chaos.db'}",
        seed_demo=True,
        live_actions=False,
        auto_create_schema=True,
    )
    engine = create_engine(
        cfg.settings.database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    dbmod.engine = engine
    dbmod.SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    main.engine = engine
    main.settings = cfg.settings
    security.settings = cfg.settings
    queue.settings = cfg.settings
    main._login_attempts.clear()
    main._api_attempts.clear()
    main._redis_client = None
    return TestClient(main.app)


@pytest.mark.parametrize(
    ("method", "responses", "expected_calls", "ambiguous"),
    [
        (
            "GET",
            [
                httpx.ReadTimeout("synthetic delay"),
                httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://provider.invalid")),
            ],
            2,
            False,
        ),
        (
            "GET",
            [
                httpx.Response(429, headers={"Retry-After": "0"}, request=httpx.Request("GET", "https://provider.invalid")),
                httpx.Response(500, request=httpx.Request("GET", "https://provider.invalid")),
                httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://provider.invalid")),
            ],
            3,
            False,
        ),
        ("POST", [httpx.ConnectError("synthetic provider outage")], 1, True),
    ],
)
def test_provider_delay_errors_are_classified_without_network(
    monkeypatch, method, responses, expected_calls, ambiguous
):
    """Retries are allowed only for safe operations; ambiguous writes stop."""
    sleeps: list[float] = []
    calls: list[str] = []

    async def no_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async def run_case():
        adapter = BaseAdapter({"api_key": "synthetic"})
        try:
            async def fake_request(request_method, *args, **kwargs):
                calls.append(request_method)
                response = responses[min(len(calls) - 1, len(responses) - 1)]
                if isinstance(response, Exception):
                    raise response
                if isinstance(response, httpx.Response):
                    return response
                return response

            monkeypatch.setattr(adapter.client, "request", fake_request)
            try:
                result = await adapter.request(method, "https://provider.invalid/resource", retries=3)
                return result, None
            except ConnectorError as exc:
                return None, exc
        finally:
            await adapter.close()

    result, error = asyncio.run(run_case())
    assert len(calls) == expected_calls
    if ambiguous:
        assert result is None and error is not None and error.ambiguous and not error.retryable
    else:
        assert result == {"ok": True} and error is None
    if method == "GET":
        assert sleeps  # delay/error path exercised the bounded backoff hook


def test_worker_lease_loss_reclaims_job_and_rejects_old_owner(tmp_path):
    """A crashed worker can be replaced, but the old owner cannot complete work."""
    with isolated_client(tmp_path) as client:
        assert client.get("/api/ready").status_code == 200

    import app.db as dbmod

    first = dbmod.SessionLocal()
    try:
        job, created = enqueue_job(
            first,
            organization_id="org_demo",
            provider="rros",
            job_type="detect_org",
            payload={"fault": "lease-loss"},
            dedupe_key="chaos-lease-loss",
            max_attempts=3,
        )
        assert created is False
        first.commit()
        claimed = claim_next(first, worker_id="chaos-worker-a", lease_seconds=30)
        assert claimed is not None
        first.close()  # safe simulation of a process disappearing after claim
    finally:
        if first.is_active:
            first.close()

    second = dbmod.SessionLocal()
    try:
        row = second.get(type(claimed), claimed.id)
        row.lease_until = utcnow() - timedelta(seconds=1)
        second.commit()
        assert recover_stale_jobs(second, lease_seconds=1) == 1
        reclaimed = claim_next(second, worker_id="chaos-worker-b", lease_seconds=30)
        assert reclaimed is not None and reclaimed.id == claimed.id and reclaimed.attempts == 2
        assert heartbeat(second, reclaimed.id, "chaos-worker-a", lease_seconds=30) is False
        assert heartbeat(second, reclaimed.id, "chaos-worker-b", lease_seconds=30) is True
        assert complete_job(second, reclaimed.id, "chaos-worker-b", {"recovered": True}) is True
    finally:
        second.close()


def test_uncertain_connector_action_becomes_reconciliation_not_replay(tmp_path):
    """An expired outbound lease is quarantined instead of blindly replayed."""
    with isolated_client(tmp_path) as client:
        assert client.get("/api/ready").status_code == 200

    import app.db as dbmod

    db = dbmod.SessionLocal()
    try:
        integration = Integration(
            id="chaos-integration",
            organization_id="org_demo",
            provider="synthetic",
            status="configured",
            credentials_ciphertext="disabled",
        )
        action = ConnectorAction(
            id="chaos-action",
            organization_id="org_demo",
            integration_id=integration.id,
            provider="synthetic",
            operation="send_message",
            idempotency_key="chaos-action-key",
            request_hash="a" * 64,
            status="running",
        )
        db.add(integration)
        db.flush()
        db.add(action)
        db.flush()
        job, _ = enqueue_job(
            db,
            organization_id="org_demo",
            provider="synthetic",
            job_type="connector_action",
            connector_action_id=action.id,
            payload={"operation": "send_message"},
            dedupe_key="chaos-action-job",
            max_attempts=2,
        )
        db.commit()
        claimed = claim_next(db, worker_id="chaos-action-worker", lease_seconds=30)
        assert claimed is not None
        row = db.get(type(claimed), claimed.id)
        row.lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
        assert recover_stale_jobs(db, lease_seconds=1) == 1
        refreshed = db.get(ConnectorAction, action.id)
        assert refreshed.status == "needs_reconciliation"
        assert refreshed.result_uncertain is True
        assert "unknown" in (refreshed.error or "")
    finally:
        db.close()


def test_malformed_and_replayed_events_are_rejected_safely(tmp_path, monkeypatch):
    """Malformed webhook input cannot create data; replay remains idempotent."""
    monkeypatch.setenv("WEBHOOK_SECRET_org_demo", "chaos-secret")
    with isolated_client(tmp_path) as client:
        headers = {"X-Webhook-Secret": "chaos-secret"}
        assert client.post("/api/ingest/org_demo", headers=headers, content=b"{").status_code == 422
        assert client.post("/api/ingest/org_demo", headers=headers, json={"data": {}}).status_code == 422
        assert client.post(
            "/api/ingest/org_demo",
            headers=headers,
            json={"type": "customer.upserted", "external_id": "chaos-event", "data": {"name": "Synthetic"}},
        ).status_code == 200
        changed = client.post(
            "/api/ingest/org_demo",
            headers=headers,
            json={"type": "customer.upserted", "external_id": "chaos-event", "data": {"name": "Changed"}},
        )
        assert changed.status_code == 409
        replay = client.post(
            "/api/ingest/org_demo",
            headers=headers,
            json={"type": "customer.upserted", "external_id": "chaos-event", "data": {"name": "Synthetic"}},
        )
        assert replay.status_code == 200 and replay.json()["idempotent"] is True


def test_api_restart_retry_keeps_read_only_session_and_tenant_boundary(tmp_path):
    """Closing/reopening the API client models restart without replaying a write."""
    with isolated_client(tmp_path) as first:
        login = first.post("/api/auth/login", json={"email": "owner@example.com", "password": "demo-password"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        before = first.get("/api/customers", headers=headers)
        assert before.status_code == 200 and {row["id"] for row in before.json()} == {f"cus_{index}" for index in range(1, 5)}

    # Rebind the same disposable DB through the helper and retry the safe read.
    with isolated_client(tmp_path) as restarted:
        after = restarted.get("/api/customers", headers=headers)
        assert after.status_code == 200 and {row["id"] for row in after.json()} == {f"cus_{index}" for index in range(1, 5)}
        assert restarted.get("/api/customers/not-a-tenant-customer", headers=headers).status_code == 404
