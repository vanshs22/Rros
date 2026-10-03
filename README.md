# Revenue Recovery OS 3.0.0

Revenue Recovery OS detects revenue leakage, creates recoverable opportunities, orchestrates approved recovery actions, and records attribution evidence.

## 3.0.0 production runtime

- Real organization/tenant model
- Multi-user RBAC: owner, admin, manager, agent, analyst, member, viewer
- React + TypeScript + Vite frontend with Playwright customer journey
- Stripe Checkout subscriptions with signed webhook reconciliation
- Tenant-scoped identity and external IDs
- Session revocation via token versioning
- Per-password random-salt PBKDF2 hashing
- Revenue opportunity state machine
- Detection idempotency
- Recovery action idempotency and concurrency conflict handling
- Consent + approval gates
- Auditable webhook ingestion with replay protection
- Tenant-safe attribution
- PostgreSQL schema with RLS defense-in-depth
- Safe simulation mode when no verified provider adapter exists

## Demo

```bash
python -m pip install -r requirements.txt
SEED_DEMO=true python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For the React frontend:

```bash
npm --prefix frontend ci
npm --prefix frontend run dev
```

Demo tenant: `acme-demo`

Demo user: `owner@example.com`

Demo password: `demo-password`

For production, set `APP_ENV=production`, `SEED_DEMO=false`, `AUTO_CREATE_SCHEMA=false`, use PostgreSQL, replace `JWT_SECRET`, set a strong `INTEGRATION_ENCRYPTION_KEY`, configure Redis, configure explicit `ALLOWED_HOSTS`, use a dedicated migration role and separate RLS-protected runtime role, and apply migrations 001 through the latest numbered migration with `scripts/migrate.py`.

## Multi-tenant behavior

Users belong to an organization. The same email address may exist in different organizations. When an email exists in multiple organizations, login requires the organization slug. Protected requests derive tenant identity from the JWT rather than accepting a client-supplied tenant ID.

## Safety model

`LIVE_ACTIONS=false` is the safe default. If enabled before a verified provider adapter is installed, the API rejects the action instead of pretending that a message was sent.

## Verification

```bash
python -m compileall -q app tests scripts
python -m pytest -q
ruff check app scripts tests --select E9,F63,F7,F82
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run test:e2e
python scripts/release_gate.py
```

Current local verification: **65 backend tests passing**, focused correctness lint passing, React typecheck/build passing, browser journey passing, disposable PostgreSQL migrations passing, and production Docker image smoke passing. The full 1,000-tenant × 100-request load target exceeded the sandbox time limit; use `scripts/tenant_scale_smoke.py` in a provisioned load environment for that target.

See `docs/PRODUCTION_READINESS.md` for the detailed readiness assessment and remaining deployment gates.


## Integration Hub v2.1

The integration layer now includes tenant-scoped encrypted credential storage, OAuth state protection, connection testing, token refresh, disconnect/reconnect, and a provider catalog covering CRM, reviews, communications, calendar, payments, accounting, field-service, commerce, support and collaboration systems. See `docs/INTEGRATIONS.md`.


## Release truth

The provider catalog is broader than the currently installed live provider adapters. A provider is only operationally live after its vendor-specific adapter, credentials, webhook verification, sync path and outbound action path have been installed and tested. The application intentionally refuses fake live execution.


## v3.0 Production Connector Runtime

The connector catalog now has **34 executable provider adapters** plus a durable multi-worker runtime. Provider credentials, scopes, account IDs and vendor approvals are still required before a tenant can use any specific provider. Each adapter has a real transport boundary, health check, synchronization contract and/or provider-specific action contract. Provider data is normalized into canonical RROS events rather than writing provider payloads directly into domain tables.

The connector layer now includes provider-scoped external IDs, tenant-scoped event idempotency, cursor-based sync state, per-record savepoints, a durable connector action ledger, request hashing, action receipts, provider idempotency headers where supported, and agent-to-connector execution through the same governed path.

See `docs/CONNECTOR_FABRIC.md` for the complete provider and data-flow contract.

Vendor documentation used to validate current API paths includes official Twilio, HubSpot, Google, HighLevel, Microsoft, Salesforce, Pipedrive, Zoho, Square, Calendly, ServiceTitan, FieldEdge, Intercom, Zendesk and Yelp developer documentation.


### Durable multi-user runtime

The runtime separates API requests from provider network calls. PostgreSQL-backed jobs are leased with row locking, heartbeated, and reclaimed after worker loss. Tenant/provider rate budgets and circuit state prevent a noisy integration from monopolizing a tenant. Connector actions persist request hashes, approvals, receipts and uncertainty state. A worker lease expiry during an outbound action moves the action to `needs_reconciliation` instead of blindly replaying a potentially successful non-idempotent provider call.
