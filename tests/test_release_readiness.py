"""Deterministic local release-readiness checks for checklist points 47-52 and 69-73."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.connector_core import BaseAdapter, ConnectorError
from app.connectors import ADAPTERS, adapter_for
from app.integrations import PROVIDERS
from app.models import ConnectorAction, Integration
from app.queue import (
    claim_next,
    complete_job,
    enqueue_job,
    fail_job,
    heartbeat,
    recover_stale_jobs,
    utcnow,
)
from app.runtime import SYNC_RESOURCES, adapter_contract


ROOT = Path(__file__).resolve().parents[1]


def isolated_client(tmp_path):
    """Create the existing FastAPI TestClient with every DB handle rebound."""
    import app.config as cfg
    import app.db as dbmod
    import app.main as main

    cfg.settings = cfg.Settings(
        database_url=f"sqlite:///{tmp_path / 'readiness.db'}",
        seed_demo=True,
        live_actions=False,
    )
    engine = create_engine(
        cfg.settings.database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    # Keep the TestClient fixture aligned with the repository's existing SQLite
    # harness. The production PostgreSQL schema owns composite-FK enforcement;
    # the local integrity scanner below checks the same tenant boundaries without
    # making SQLite reject the repository's legacy single-column FK shape.
    dbmod.engine = engine
    dbmod.SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    main.engine = engine
    main.settings = cfg.settings
    main._login_attempts.clear()
    main._redis_client = None
    return TestClient(main.app)


def register(c: TestClient):
    response = c.post(
        "/api/auth/register",
        json={
            "organization_name": "Acme Home Services",
            "organization_slug": "acme-home-services",
            "email": "owner@acme-home-services.example",
            "password": "AcmeHomeServicesPassword123!",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["organization_id"], body["webhook_secret"], {"Authorization": f"Bearer {body['access_token']}"}


def post_event(c: TestClient, organization_id: str, secret: str, payload: dict):
    response = c.post(
        f"/api/ingest/{organization_id}",
        headers={"X-Webhook-Secret": secret},
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_acme_home_services_customer_journey_is_replay_safe(tmp_path):
    """Exercise ingest -> detect -> approve -> simulate -> report with tenant isolation."""
    with isolated_client(tmp_path) as c:
        organization_id, secret, auth = register(c)
        customer_event = {
            "type": "customer.upserted",
            "external_id": "acme-customer-001",
            "data": {
                "external_id": "acme-customer-001",
                "name": "Jordan Rivera",
                "email": "jordan.rivera@example.com",
                "phone": "+15551230001",
            },
        }
        customer = post_event(c, organization_id, secret, customer_event)
        customer_id = customer["processed"]["id"]
        post_event(
            c,
            organization_id,
            secret,
            {
                "type": "lead.created",
                "external_id": "acme-lead-001",
                "data": {
                    "external_id": "acme-lead-001",
                    "customer_external_id": "acme-customer-001",
                    "customer_name": "Jordan Rivera",
                    "customer_email": "jordan.rivera@example.com",
                    "customer_phone": "+15551230001",
                    "source": "Google Local Services",
                    "status": "uncontacted",
                    "value": "1800.00",
                },
            },
        )
        post_event(
            c,
            organization_id,
            secret,
            {
                "type": "payment.failed",
                "external_id": "acme-payment-001",
                "data": {
                    "external_id": "acme-payment-001",
                    "customer_external_id": "acme-customer-001",
                    "amount": "450.00",
                    "failure_code": "card_declined",
                },
            },
        )

        # Replaying the same source event is a no-op and cannot create a second lead.
        replay = post_event(c, organization_id, secret, customer_event)
        assert replay["idempotent"] is True
        assert c.get(f"/api/customers/{customer_id}", headers=auth).json()["customer"]["name"] == "Jordan Rivera"

        first_detection = c.post("/api/detect", headers=auth)
        second_detection = c.post("/api/detect", headers=auth)
        assert first_detection.status_code == second_detection.status_code == 200
        opportunities = c.get("/api/opportunities", headers=auth)
        assert opportunities.status_code == 200
        customer_opportunities = [x for x in opportunities.json() if x["customer_id"] == customer_id]
        assert {x["type"] for x in customer_opportunities} >= {"lost_lead", "failed_payment"}

        target = next(x for x in customer_opportunities if x["type"] == "failed_payment")
        plan = c.post(
            f"/api/opportunities/{target['id']}/recovery-plan",
            headers=auth,
            json={"channel": "sms"},
        )
        assert plan.status_code == 200 and plan.json()["approval_required"] is True
        assert c.post(
            f"/api/consents",
            headers=auth,
            json={"customer_id": customer_id, "channel": "sms", "allowed": True, "source": "customer_opt_in"},
        ).status_code == 200
        executed = c.post(
            f"/api/opportunities/{target['id']}/execute",
            headers=auth,
            json={
                "channel": "sms",
                "approval": True,
                "message": "Acme Home Services can help with your service request.",
                "idempotency_key": "acme-journey-action-001",
            },
        )
        assert executed.status_code == 200 and executed.json()["execution"] == "simulation"
        assert executed.json()["status"] == "in_recovery"
        duplicate = c.post(
            f"/api/opportunities/{target['id']}/execute",
            headers=auth,
            json={
                "channel": "sms",
                "approval": True,
                "message": "Acme Home Services can help with your service request.",
                "idempotency_key": "acme-journey-action-001",
            },
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["idempotent"] is True

        recovered = post_event(
            c,
            organization_id,
            secret,
            {
                "type": "transaction.paid",
                "external_id": "acme-transaction-001",
                "data": {
                    "id": "tx-acme-recovered",
                    "external_id": "acme-transaction-001",
                    "customer_external_id": "acme-customer-001",
                    "amount": "450.00",
                },
            },
        )
        assert recovered["processed"]["entity"] == "transaction"
        attributed = c.post(f"/api/transactions/tx-acme-recovered/attribute/{target['id']}", headers=auth)
        assert attributed.status_code == 200 and attributed.json()["classification"] == "directly_attributed"

        detail = c.get(f"/api/opportunities/{target['id']}", headers=auth)
        assert detail.status_code == 200
        assert detail.json()["opportunity"]["status"] == "recovered"
        assert detail.json()["plans"] and detail.json()["actions"] and detail.json()["approvals"]
        report = c.get("/api/revenue/report", headers=auth)
        assert report.status_code == 200
        assert {"attribution", "dashboard", "sources"} <= report.json().keys()
        assert any(row["classification"] == "direct" and row["amount"] == 450.0 for row in report.json()["attribution"])

        other = c.post(
            "/api/auth/register",
            json={
                "organization_name": "Other Home Services",
                "organization_slug": "other-home-services",
                "email": "owner@other-home-services.example",
                "password": "OtherHomeServicesPassword123!",
            },
        )
        assert other.status_code == 200
        other_auth = {"Authorization": f"Bearer {other.json()['access_token']}"}
        assert c.get(f"/api/customers/{customer_id}", headers=other_auth).status_code == 404
        assert c.get(f"/api/opportunities/{target['id']}", headers=other_auth).status_code == 404


def test_failure_modes_are_classified_without_real_provider_calls(monkeypatch):
    """Cover outage, OAuth expiry, rate-limit, server-error, timeout, and ambiguity semantics."""
    sleeps = []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async def exercise(method, responses, **kwargs):
        adapter = BaseAdapter({"api_key": "test"})
        calls = []

        async def fake_request(*args, **request_kwargs):
            calls.append(args[0])
            response = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(adapter.client, "request", fake_request)
        try:
            result = await adapter.request(method, "https://provider.example/resource", retries=3, **kwargs)
            return result, calls, None
        except ConnectorError as exc:
            return None, calls, exc
        finally:
            await adapter.close()

    async def run_cases():
        ok = httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://provider.example"))
        throttled = httpx.Response(429, headers={"Retry-After": "0.25"}, request=httpx.Request("GET", "https://provider.example"))
        server_error = httpx.Response(500, request=httpx.Request("GET", "https://provider.example"))
        unauthorized = httpx.Response(401, request=httpx.Request("GET", "https://provider.example"))
        redirect = httpx.Response(302, headers={"Location": "https://elsewhere.example"}, request=httpx.Request("GET", "https://provider.example"))

        result, calls, error = await exercise("GET", [throttled, ok])
        assert result == {"ok": True} and len(calls) == 2 and error is None
        result, calls, error = await exercise("GET", [server_error, server_error, server_error])
        assert result is None and len(calls) == 3 and error.retryable and not error.ambiguous
        result, calls, error = await exercise("POST", [server_error])
        assert result is None and len(calls) == 1 and not error.retryable and error.ambiguous
        result, calls, error = await exercise("GET", [httpx.ReadTimeout("provider timeout")])
        assert result is None and len(calls) == 3 and error.retryable and not error.ambiguous
        result, calls, error = await exercise("POST", [httpx.ConnectError("provider unavailable")])
        assert result is None and len(calls) == 1 and not error.retryable and error.ambiguous
        result, calls, error = await exercise("GET", [unauthorized])
        assert result is None and len(calls) == 1 and error.status_code == 401 and not error.retryable
        result, calls, error = await exercise("GET", [redirect])
        assert result is None and len(calls) == 1 and error.status_code == 302 and not error.retryable

    asyncio.run(run_cases())
    assert sleeps and all(0.25 <= delay <= 8.0 for delay in sleeps)


def test_queue_restart_reclaims_leases_and_dead_letters_deterministically(tmp_path):
    """Simulate worker loss by closing its session, then reclaim and continue safely."""
    with isolated_client(tmp_path) as c:
        c.get("/api/ready")
    import app.db as dbmod

    first = dbmod.SessionLocal()
    try:
        job, created = enqueue_job(
            first,
            organization_id="org_demo",
            provider="rros",
            job_type="detect_org",
            payload={"reason": "restart-test"},
            dedupe_key="readiness-restart-job",
            max_attempts=3,
        )
        assert created is False
        first.commit()
        claimed = claim_next(first, worker_id="worker-before-restart", lease_seconds=30)
        assert claimed and claimed.status == "running"
        first.close()  # safe local simulation of a crashed worker process
    finally:
        if first.is_active:
            first.close()

    second = dbmod.SessionLocal()
    try:
        row = second.get(type(claimed), claimed.id)
        row.lease_until = utcnow() - timedelta(seconds=1)
        second.commit()
        assert recover_stale_jobs(second, lease_seconds=1) == 1
        reclaimed = claim_next(second, worker_id="worker-after-restart", lease_seconds=30)
        assert reclaimed and reclaimed.id == claimed.id and reclaimed.attempts == 2
        assert heartbeat(second, reclaimed.id, "worker-after-restart", lease_seconds=30) is True
        assert complete_job(second, reclaimed.id, "worker-after-restart", {"restarted": True}) is True

        dead, _ = enqueue_job(
            second,
            organization_id="org_demo",
            provider="rros",
            job_type="detect_org",
            payload={"reason": "dead-letter-test"},
            dedupe_key="readiness-dead-letter-job",
            max_attempts=1,
        )
        second.commit()
        dead = claim_next(second, worker_id="worker-failing", lease_seconds=30)
        assert dead
        assert fail_job(second, dead.id, "worker-failing", "worker failed", retryable=True) == "dead_letter"
    finally:
        second.close()


def test_bounded_concurrency_smoke_has_stable_tenant_scoped_reads(tmp_path):
    """Exercise a bounded 4-worker/32-request smoke load; this is not a production load claim."""
    with isolated_client(tmp_path) as c:
        login = c.post("/api/auth/login", json={"email": "owner@example.com", "password": "demo-password"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        def read_opportunities(_):
            response = c.get("/api/opportunities", headers=headers)
            return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(read_opportunities, range(32)))
        assert all(status == 200 for status, _ in results)
        snapshots = [json.dumps(body, sort_keys=True) for _, body in results]
        assert len(set(snapshots)) == 1


def test_integrity_scanner_emits_clean_and_corrupt_machine_reports(tmp_path):
    """Verify the executable integrity scanner catches a representative orphan."""
    with isolated_client(tmp_path) as c:
        assert c.get("/api/ready").status_code == 200
    database_url = f"sqlite:///{tmp_path / 'readiness.db'}"
    command = [sys.executable, "scripts/integrity_check.py", "--database-url", database_url, "--json"]
    clean = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    clean_report = json.loads(clean.stdout)
    assert clean_report["passed"] is True and clean_report["violation_count"] == 0

    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            text(
                "INSERT INTO opportunity_events "
                "(organization_id, opportunity_id, event_type, actor, metadata_json, created_at) "
                "VALUES ('missing-org', 'missing-opportunity', 'corrupt', 'test', '{}', CURRENT_TIMESTAMP)"
            )
        )
    corrupt = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert corrupt.returncode != 0
    corrupt_report = json.loads(corrupt.stdout)
    assert corrupt_report["passed"] is False
    assert any("opportunity_events" in item["check"] for item in corrupt_report["violations"])




def _contract_credentials(provider: str) -> dict:
    credentials = {"access_token": "contract-test", "api_key": "contract-test", "token": "contract-test", "auth_token": "contract-test"}
    credentials.update({
        "twilio": {"account_sid": "ACtest", "auth_token": "contract-test", "from_number": "+15550000000"},
        "whatsapp_twilio": {"account_sid": "ACtest", "auth_token": "contract-test", "whatsapp_from": "whatsapp:+15550000000"},
        "gohighlevel": {"location_id": "loc-test", "start_time_ms": 0, "end_time_ms": 9999999999999},
        "salesforce": {"instance_url": "https://example.my.salesforce.com"},
        "quickbooks": {"realm_id": "realm-test"},
        "xero": {"tenant_id": "xero-test"},
        "dynamics_365": {"instance_url": "https://example.crm.dynamics.com"},
        "freshsales": {"domain": "example.myfreshworks.com", "api_key": "contract-test"},
        "zendesk": {"subdomain": "example"},
        "yelp": {"business_id": "business-test"},
        "fieldedge": {"target_id": "target-test"},
        "shopify": {"shop_domain": "test-shop.myshopify.com"},
        "slack": {"channel_id": "C123"},
        "microsoft_teams": {"team_id": "T123", "channel_id": "C123"},
        "woocommerce": {"store_url": "https://shop.example.com", "consumer_key": "ck_test", "consumer_secret": "cs_test"},
        "servicetitan": {"tenant_id": "tenant-test", "app_key": "app-test"},
    }.get(provider, {}))
    return credentials


@pytest.mark.asyncio
async def test_connector_contracts_cover_catalog_resources_and_error_boundary(monkeypatch):
    """Validate every advertised provider boundary plus a realistic paginated result shape."""
    assert set(PROVIDERS) == set(ADAPTERS)
    for provider in sorted(PROVIDERS):
        contract = adapter_contract(ADAPTERS[provider])
        assert contract["has_sync"] is True
        assert set(SYNC_RESOURCES.get(provider, ())) <= set(contract["resources"])
        credentials = _contract_credentials(provider)
        config = dict(credentials)
        if provider == "gohighlevel":
            config.update({"start_time_ms": 1, "end_time_ms": 9999999999999, "calendar_id": "calendar-test"})
        adapter = adapter_for(provider, credentials, config)

        async def fake_request(method, url, **kwargs):
            return {"data": [], "items": [], "results": [], "next_cursor": "cursor-1"}

        monkeypatch.setattr(adapter, "request", fake_request)
        for resource in SYNC_RESOURCES.get(provider, ()):
            result = await adapter.sync(resource, cursor=None)
            assert isinstance(result.records, list), (provider, resource)
            assert result.next_cursor is None or isinstance(result.next_cursor, str)
        await adapter.close()
