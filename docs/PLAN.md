# RROS production build checklist

This file tracks the implementation against the 73-point plan in `docs/RROS_PLAN.md`. A point is marked complete only when its code path and verification exist.

## Verified in this build
- [x] React + TypeScript + Vite frontend replaces the static shell in local builds and production Docker images.
- [x] Stripe Checkout subscription creation, tenant-scoped billing sessions, signed webhook reconciliation, and five-plan catalog.
- [x] PostgreSQL migration-from-zero through migration 020 in a disposable PostgreSQL 16 container.
- [x] Production image build and bundled-code Docker smoke.
- [x] Playwright browser journey: login, command center, Customer 360, global search, logout.
- [x] Chaos fault injection: provider timeout/429/500, uncertain outbound action, stale worker lease, malformed/replayed webhook, API restart boundary.
- [x] Parameterized tenant load harness with zero-isolation-violation smoke at 1,000 tenants × 10 users × 1 request (1,000 requests in 7.08s). The requested 1,000-tenant × 100-request run timed out in this sandbox and is not marked passed.
- [x] 65 backend tests pass; frontend typecheck/build pass; local release gate passes.

## Commands
```bash
python -m pytest -q
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run test:e2e
python scripts/postgres_migration_smoke.py
python scripts/docker_smoke.py
python scripts/release_gate.py
```

## Remaining honest risks
- The 1,000-tenant target-scale load run needs a performance-tuned/provisioned environment; the harness exists and smaller isolation runs pass.
- A fully distributed observability backend and a payment-provider live account still require deployment credentials/configuration; checkout is real but tested with provider transport mocks.
- The strict release report is green in this sandbox after the browser, Docker, and disposable PostgreSQL checks; live provider production certification remains an operational deployment step.
- [x] Product-correctness hardening: replay restores envelope external IDs, recovery execution requires an approved matching plan and stable idempotency key, attribution ledger enforces one classification per transaction/opportunity, and live queued actions do not receive false execution timestamps.
- [x] Integrity hardening: scanner now includes billing/lineage tables, full result counts with examples, attribution ceiling checks, and recovered-amount ledger reconciliation.
- [x] Frontend hardening: stale request protection, role-aware governed action controls, correct feature-flag envelope handling, agent trace ID handling, evidence values, and connector-save failure behavior.
