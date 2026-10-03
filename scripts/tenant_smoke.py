#!/usr/bin/env python3
"""Bounded multi-tenant API concurrency smoke harness.

Default mode creates a disposable local SQLite database and runs a tiny smoke
load. Supplying --base-url runs against an already provisioned API; use
--tokens-file to provide tenant/customer/token records without performing
registration or other destructive setup operations.

Example target invocation (not run by the default smoke):
  python scripts/tenant_smoke.py --tenants 1000 --users-per-tenant 10 \
      --requests-per-tenant 100 --base-url http://127.0.0.1:8000 \
      --tokens-file tenant_tokens.json
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterator

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class TenantRecord:
    tenant_id: str
    customer_id: str
    marker: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class Job:
    tenant_index: int
    request_index: int
    foreign_customer_id: str | None


@dataclass(frozen=True)
class Outcome:
    status: int | None
    latency_ms: float
    expected_status: int
    isolation_issue: str | None = None
    error: str | None = None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _configure_local_application(database_url: str):
    """Rebind module globals for a disposable in-process TestClient."""
    import app.config as cfg
    import app.db as dbmod
    import app.main as main
    import app.queue as queue_module
    import app.security as security

    cfg.settings = cfg.Settings(
        database_url=database_url,
        seed_demo=False,
        live_actions=False,
        auto_create_schema=True,
        app_env="development",
        api_rate_limit_per_minute=1_000_000,
    )
    engine = create_engine(database_url, connect_args={"check_same_thread": False}, future=True)
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
    queue_module.settings = cfg.settings
    main._login_attempts.clear()
    main._api_attempts.clear()
    main._redis_client = None
    return main, dbmod


def _seed_local_tenants(dbmod, security, tenant_count: int, users_per_tenant: int) -> list[TenantRecord]:
    from app.models import Customer, Location, Organization, User

    db = dbmod.SessionLocal()
    try:
        organizations = []
        locations = []
        users = []
        customers = []
        for tenant_index in range(tenant_count):
            tenant_id = f"load_org_{tenant_index:06d}"
            location_id = f"load_loc_{tenant_index:06d}"
            customer_id = f"load_customer_{tenant_index:06d}"
            marker = f"load-tenant-{tenant_index:06d}-sentinel"
            organizations.append(
                Organization(
                    id=tenant_id,
                    name=f"Load Tenant {tenant_index:06d}",
                    slug=f"load-tenant-{tenant_index:06d}",
                )
            )
            locations.append(Location(id=location_id, organization_id=tenant_id, name="Synthetic location"))
            customers.append(
                Customer(
                    id=customer_id,
                    organization_id=tenant_id,
                    location_id=location_id,
                    external_id=f"external-{tenant_index:06d}",
                    name=marker,
                    email=f"{marker}@example.invalid",
                    phone=f"+155500{tenant_index:06d}"[-15:],
                )
            )
            for user_index in range(users_per_tenant):
                users.append(
                    User(
                        id=f"load_user_{tenant_index:06d}_{user_index:03d}",
                        organization_id=tenant_id,
                        email=f"user-{user_index:03d}@tenant-{tenant_index:06d}.example.invalid",
                        password_hash="synthetic-load-only",
                        role="owner" if user_index == 0 else "member",
                        active=True,
                        token_version=1,
                    )
                )
        # Explicit flush ordering respects the composite customer/location FK.
        db.add_all(organizations)
        db.flush()
        db.add_all(locations)
        db.flush()
        db.add_all(users)
        db.add_all(customers)
        db.commit()

        by_tenant: dict[str, list[str]] = {org.id: [] for org in organizations}
        for user in users:
            by_tenant[user.organization_id].append(security.issue_token(user))
        return [
            TenantRecord(
                tenant_id=org.id,
                customer_id=f"load_customer_{index:06d}",
                marker=f"load-tenant-{index:06d}-sentinel",
                tokens=tuple(by_tenant[org.id]),
            )
            for index, org in enumerate(organizations)
        ]
    finally:
        db.close()


@contextmanager
def local_application(tenant_count: int, users_per_tenant: int) -> Iterator[tuple[TestClient, list[TenantRecord]]]:
    with tempfile.TemporaryDirectory(prefix="rros-tenant-smoke-") as temp_dir:
        main, dbmod = _configure_local_application(f"sqlite:///{Path(temp_dir) / 'smoke.db'}")
        import app.security as security

        with TestClient(main.app) as client:
            records = _seed_local_tenants(dbmod, security, tenant_count, users_per_tenant)
            yield client, records


def _load_token_file(path: str, expected_tenants: int, expected_users: int) -> list[TenantRecord]:
    """Load a non-secret test manifest; tokens are supplied by the caller."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("tenants") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("tokens file must contain a non-empty 'tenants' list")
    records = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each token manifest tenant must be an object")
        tokens = row.get("tokens")
        if not isinstance(tokens, list):
            token = row.get("token")
            tokens = [token] if token else []
        tokens = [str(token) for token in tokens if token]
        if not row.get("tenant_id") or not row.get("customer_id") or not row.get("marker") or not tokens:
            raise ValueError("each tenant needs tenant_id, customer_id, marker, and token(s)")
        if len(tokens) < expected_users:
            raise ValueError(f"tenant {row['tenant_id']} has fewer tokens than --users-per-tenant")
        records.append(
            TenantRecord(
                tenant_id=str(row["tenant_id"]),
                customer_id=str(row["customer_id"]),
                marker=str(row["marker"]),
                tokens=tuple(tokens),
            )
        )
    if len(records) != expected_tenants:
        raise ValueError(f"tokens file contains {len(records)} tenants, expected {expected_tenants}")
    return records


def _make_job(records: list[TenantRecord], tenant_index: int, request_index: int) -> Job:
    foreign = None
    # Negative controls are read-only and intentionally target a neighboring tenant.
    if len(records) > 1 and request_index % 10 == 1:
        foreign = records[(tenant_index + 1) % len(records)].customer_id
    return Job(tenant_index, request_index, foreign)


def _request_one(client: Any, records: list[TenantRecord], job: Job, timeout: float) -> Outcome:
    record = records[job.tenant_index]
    customer_id = job.foreign_customer_id or record.customer_id
    expected_status = 404 if job.foreign_customer_id else 200
    headers = {"Authorization": f"Bearer {record.tokens[job.request_index % len(record.tokens)]}"}
    started = time.perf_counter()
    try:
        response = client.get(f"/api/customers/{customer_id}", headers=headers, timeout=timeout)
        body = response.text
        isolation_issue = None
        if expected_status == 200:
            if record.customer_id not in body or record.marker not in body:
                isolation_issue = "own tenant marker missing from successful response"
        else:
            if response.status_code != 404:
                isolation_issue = f"foreign customer returned status {response.status_code}, expected 404"
        foreign_values = [
            value
            for index, other in enumerate(records)
            if index != job.tenant_index
            for value in (other.customer_id, other.marker)
            if value in body
        ]
        if foreign_values:
            isolation_issue = "foreign tenant value in response: " + ",".join(sorted(set(foreign_values))[:3])
        return Outcome(response.status_code, (time.perf_counter() - started) * 1000, expected_status, isolation_issue)
    except Exception as exc:  # report transport failures as load errors, never hide them
        return Outcome(None, (time.perf_counter() - started) * 1000, expected_status, error=f"{type(exc).__name__}: {exc}")


def run_load(
    client: Any,
    records: list[TenantRecord],
    requests_per_tenant: int,
    global_concurrency: int,
    per_tenant_concurrency: int,
    tenant_batch_size: int,
    timeout: float,
    seed: int,
) -> dict[str, Any]:
    """Run bounded work without materializing all requests or sockets at once."""
    del seed  # Scheduling is deterministic by construction; retained for CLI compatibility.
    jobs: queue.Queue[Job | None] = queue.Queue(maxsize=max(1, global_concurrency * 2))
    tenant_slots = [threading.BoundedSemaphore(per_tenant_concurrency) for _ in records]
    outcomes: list[Outcome] = []
    outcomes_lock = threading.Lock()

    def producer() -> None:
        try:
            for batch_start in range(0, len(records), tenant_batch_size):
                batch = range(batch_start, min(len(records), batch_start + tenant_batch_size))
                for request_index in range(requests_per_tenant):
                    for tenant_index in batch:
                        jobs.put(_make_job(records, tenant_index, request_index))
        finally:
            for _ in range(global_concurrency):
                jobs.put(None)

    def worker() -> None:
        while True:
            job = jobs.get()
            try:
                if job is None:
                    return
                with tenant_slots[job.tenant_index]:
                    outcome = _request_one(client, records, job, timeout)
                with outcomes_lock:
                    outcomes.append(outcome)
            finally:
                jobs.task_done()

    producer_thread = threading.Thread(target=producer, name="tenant-smoke-producer", daemon=True)
    workers = [threading.Thread(target=worker, name=f"tenant-smoke-worker-{i}", daemon=True) for i in range(global_concurrency)]
    started = time.perf_counter()
    producer_thread.start()
    for worker_thread in workers:
        worker_thread.start()
    producer_thread.join()
    jobs.join()
    for worker_thread in workers:
        worker_thread.join()
    elapsed = time.perf_counter() - started

    latencies = sorted(outcome.latency_ms for outcome in outcomes)
    expected_total = len(records) * requests_per_tenant
    status_counts: dict[str, int] = {}
    errors = 0
    isolation_violations = 0
    for outcome in outcomes:
        key = str(outcome.status) if outcome.status is not None else "exception"
        status_counts[key] = status_counts.get(key, 0) + 1
        if outcome.error or outcome.status != outcome.expected_status:
            errors += 1
        if outcome.isolation_issue:
            isolation_violations += 1

    def percentile(percent: float) -> float:
        if not latencies:
            return 0.0
        index = min(len(latencies) - 1, max(0, int(round((percent / 100) * (len(latencies) - 1)))))
        return round(latencies[index], 3)

    return {
        "requests": {"expected": expected_total, "completed": len(outcomes), "status_counts": status_counts},
        "latency_ms": {
            "mean": round(mean(latencies), 3) if latencies else 0.0,
            "p50": percentile(50),
            "p95": percentile(95),
            "p99": percentile(99),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "throughput_requests_per_second": round(len(outcomes) / elapsed, 3) if elapsed else 0.0,
        "errors": errors,
        "isolation": {
            "checks": len(outcomes),
            "violations": isolation_violations,
            "foreign_negative_controls": sum(1 for i in range(requests_per_tenant) if len(records) > 1 and i % 10 == 1) * len(records),
        },
        "elapsed_seconds": round(elapsed, 3),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants", type=_positive_int, default=2)
    parser.add_argument("--users-per-tenant", type=_positive_int, default=2)
    parser.add_argument("--requests-per-tenant", type=_nonnegative_int, default=4)
    parser.add_argument("--global-concurrency", type=_positive_int, default=8)
    parser.add_argument("--per-tenant-concurrency", type=_positive_int, default=4)
    parser.add_argument("--tenant-batch-size", type=_positive_int, default=50)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--base-url", default=None, help="Existing API URL; omit for disposable local SQLite smoke")
    parser.add_argument("--tokens-file", help="JSON manifest for --base-url mode; no registration is performed")
    parser.add_argument("--seed", type=int, default=7, help="Reserved deterministic scheduling seed")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.request_timeout <= 0:
        raise SystemExit("--request-timeout must be greater than zero")
    if args.per_tenant_concurrency > args.global_concurrency:
        args.per_tenant_concurrency = args.global_concurrency
    if args.base_url and not args.tokens_file:
        raise SystemExit("--tokens-file is required with --base-url; the harness never registers load tenants")

    if args.base_url:
        try:
            records = _load_token_file(args.tokens_file, args.tenants, args.users_per_tenant)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"invalid --tokens-file: {exc}") from exc
        limits = httpx.Limits(max_connections=args.global_concurrency, max_keepalive_connections=args.global_concurrency)
        with httpx.Client(base_url=args.base_url.rstrip("/"), limits=limits, timeout=args.request_timeout) as client:
            report = run_load(client, records, args.requests_per_tenant, args.global_concurrency, args.per_tenant_concurrency, args.tenant_batch_size, args.request_timeout, args.seed)
    else:
        with local_application(args.tenants, args.users_per_tenant) as (client, records):
            report = run_load(client, records, args.requests_per_tenant, args.global_concurrency, args.per_tenant_concurrency, args.tenant_batch_size, args.request_timeout, args.seed)

    report["configuration"] = {
        "tenants": args.tenants,
        "users_per_tenant": args.users_per_tenant,
        "requests_per_tenant": args.requests_per_tenant,
        "global_concurrency": args.global_concurrency,
        "per_tenant_concurrency": args.per_tenant_concurrency,
        "mode": "remote" if args.base_url else "local-disposable",
        "seed": args.seed,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["errors"] or report["isolation"]["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
