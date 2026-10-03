#!/usr/bin/env python3
"""Fast 1,000-tenant HTTP load/isolation smoke using a temporary local API."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TENANTS = int(os.getenv("RROS_SCALE_TENANTS", "1000"))
USERS = int(os.getenv("RROS_SCALE_USERS_PER_TENANT", "10"))
REQUESTS = int(os.getenv("RROS_SCALE_REQUESTS_PER_TENANT", "100"))
PORT = 8765


def seed_database(path: Path):
    os.environ.update({
        "APP_ENV": "development", "DATABASE_URL": f"sqlite:///{path}",
        "AUTO_CREATE_SCHEMA": "true", "SEED_DEMO": "false", "LIVE_ACTIONS": "false",
        "JWT_SECRET": "scale-smoke-secret-change-me-1234567890",
        "API_RATE_LIMIT_PER_MINUTE": "1000000",
    })
    from app.config import settings
    from app.db import Base, SessionLocal, engine
    from app.models import Customer, Location, Organization, User
    from app.security import issue_token
    Base.metadata.create_all(engine)
    db = SessionLocal()
    records = []
    try:
        organizations = [Organization(id=f"scale_org_{i:04d}", name=f"Scale {i}", slug=f"scale-{i}") for i in range(TENANTS)]
        locations = [Location(id=f"scale_loc_{i:04d}", organization_id=org.id, name="Synthetic") for i, org in enumerate(organizations)]
        users = []
        customers = []
        for i, org in enumerate(organizations):
            customer_id = f"scale_customer_{i:04d}"
            marker = f"scale-marker-{i:04d}"
            customers.append(Customer(id=customer_id, organization_id=org.id, location_id=locations[i].id, name=marker, email=f"{marker}@invalid.test"))
            for j in range(USERS):
                users.append(User(id=f"scale_user_{i:04d}_{j:02d}", organization_id=org.id, email=f"u{j}@t{i}.invalid", password_hash="synthetic", role="owner" if j == 0 else "member"))
        db.add_all(organizations); db.flush(); db.add_all(locations); db.flush(); db.add_all(users); db.add_all(customers); db.commit()
        by_org = {}
        for user in users:
            by_org.setdefault(user.organization_id, []).append(issue_token(user))
        for i, org in enumerate(organizations):
            records.append({"customer": f"scale_customer_{i:04d}", "marker": f"scale-marker-{i:04d}", "tokens": by_org[org.id]})
        return records
    finally:
        db.close()


async def main():
    import httpx
    with tempfile.TemporaryDirectory(prefix="rros-scale-") as tmp:
        db_path = Path(tmp) / "scale.db"
        records = seed_database(db_path)
        env = os.environ.copy()
        server = subprocess.Popen(["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT), "--workers", "4"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=15, limits=httpx.Limits(max_connections=250, max_keepalive_connections=250)) as client:
                for _ in range(60):
                    try:
                        if (await client.get("/api/health")).status_code == 200: break
                    except Exception: pass
                    await asyncio.sleep(0.25)
                else: raise RuntimeError("scale API did not start")
                latencies = []; errors = 0; violations = 0; completed = 0
                started = time.perf_counter()
                async def one(i, j):
                    nonlocal errors, violations, completed
                    other = (i + 1) % TENANTS if j % 10 == 1 else None
                    target = records[other or i]["customer"]
                    expected = 404 if other is not None else 200
                    headers = {"Authorization": f"Bearer {records[i]['tokens'][j % USERS]}"}
                    tick = time.perf_counter()
                    try:
                        response = await client.get(f"/api/customers/{target}", headers=headers)
                        body = response.text
                        latencies.append((time.perf_counter() - tick) * 1000)
                        completed += 1
                        if response.status_code != expected or (other is None and records[i]["marker"] not in body) or (other is not None and records[other]["marker"] in body):
                            violations += 1
                    except Exception:
                        errors += 1
                for batch_start in range(0, TENANTS, 10):
                    await asyncio.gather(*(one(i, j) for i in range(batch_start, min(batch_start + 10, TENANTS)) for j in range(REQUESTS)))
                elapsed = time.perf_counter() - started
                report = {"tenants": TENANTS, "users_per_tenant": USERS, "requests_per_tenant": REQUESTS, "expected_requests": TENANTS * REQUESTS, "completed_requests": completed, "errors": errors, "isolation_violations": violations, "elapsed_seconds": round(elapsed, 2), "requests_per_second": round(completed / elapsed, 2), "mean_latency_ms": round(mean(latencies), 2) if latencies else None}
                print(json.dumps(report, indent=2))
                return 0 if completed == TENANTS * REQUESTS and errors == 0 and violations == 0 else 1
        finally:
            server.terminate(); server.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
