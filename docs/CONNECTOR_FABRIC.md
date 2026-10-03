# RROS Connector Fabric 2.8

The connector fabric is the external data/action boundary for Revenue Recovery OS.
A provider adapter never writes directly to canonical domain tables. It performs a
provider API operation and returns a normalized `ConnectorResult`; the orchestration
layer persists an `IntegrationEvent`, applies a canonical event, and records the
operation/audit receipt.

## End-to-end contract

```text
Provider API / Webhook
        |
        v
Provider Adapter
        |
        | normalized event/action
        v
Integration Event Inbox
        |
        v
Canonical Event Service
        |
        v
Tenant-scoped PostgreSQL model
        |
        v
Revenue Graph / Opportunity
        |
        v
Agent Context -> Agent Handoffs -> Agent Decision
        |
        v
QA / Approval / Permission
        |
        v
Connector Action Ledger
        |
        v
Provider Adapter -> Provider API
        |
        v
External receipt / ID / status
        |
        v
Audit + Opportunity Event + Outcome / ROI / Learning
```

## Provider coverage

The current catalog has 34 executable adapter registrations:

- Google Business Profile
- Gmail
- Google Calendar
- HubSpot
- GoHighLevel
- Microsoft Graph / Outlook
- Salesforce
- Pipedrive
- Zoho CRM
- Stripe
- Twilio
- WhatsApp via Twilio
- QuickBooks Online
- Xero
- Square
- Shopify
- WooCommerce
- Calendly
- Slack
- Microsoft Teams
- ServiceTitan
- Housecall Pro
- Jobber
- JobNimbus
- FieldEdge
- AccuLynx
- GorillaDesk
- Dynamics 365
- Freshsales
- Close
- Keap
- Zendesk
- Intercom
- Yelp

A provider being executable does **not** mean every API capability is available on every account. Vendor scopes, plan level, partner approval, tenant IDs, location IDs, store domains, and account-specific configuration are enforced by the adapter.

## Data integrity

### Provider-scoped external IDs

Canonical entities namespace provider identifiers, e.g.:

```text
stripe:123
hubspot:123
```

This prevents two providers using the same external identifier from being merged accidentally. Raw provider IDs remain available in `integration_events` for reconciliation.

### Event idempotency

Inbound provider events are stored using tenant + provider + external ID. Replayed events do not create duplicate canonical records.

### Sync idempotency

Each integration/resource has a tenant-scoped `integration_sync_states` row containing its cursor, state, counters, timestamps and error state. Each provider event is persisted before canonical processing and can be replayed after a canonicalization failure.

### Action idempotency

Outbound actions use `connector_actions` with:

- tenant-scoped idempotency key
- request hash
- provider
- operation
- attempts
- status
- external ID
- response receipt
- error state
- timestamps

A repeated request with the same key and same request hash returns the existing successful receipt instead of executing the provider action again. A reused key with different request data is rejected.

Provider-specific idempotency headers are supplied where supported; the internal action ledger remains the primary RROS deduplication boundary.

## Security

- Credentials are stored encrypted, not in canonical records.
- Connector actions require authenticated tenant membership and appropriate role.
- Live execution remains disabled unless `LIVE_ACTIONS=true` is explicitly enabled.
- Agent actions cannot bypass QA/approval gates.
- Provider errors are sanitized before being returned to clients.
- Provider payloads are retained in the integration event boundary rather than blindly copied into logs.
- Canonical queries remain organization scoped.
- Provider external IDs are namespaced before entering canonical entities.
- Remote provider URLs are restricted to HTTPS and validated against private/local destinations.

## Important provider-specific setup

Some providers require additional account configuration beyond a bearer token:

- GoHighLevel: location ID and appropriate OAuth scopes.
- Salesforce: instance URL and OAuth authorization.
- QuickBooks: realm ID.
- Xero: tenant ID.
- Shopify: shop domain and Admin API access token.
- WooCommerce: store URL, consumer key and consumer secret.
- Microsoft Teams: team and channel IDs for channel operations.
- ServiceTitan: tenant ID, application key, OAuth access token and approved API scopes.
- FieldEdge: Marketplace/partner approval, tenant `target_id`, OAuth credentials and approved API scopes.
- Yelp review access: partner/plan permissions may be required.

## Verification

Release verification runs:

```bash
python -m compileall -q app tests
pytest -q
```

The 3.0 release suite covers the connector registry, adapter contracts, queue/lease behavior, canonical event processing, tenant isolation and agent execution; the exact count is reported by the release test run.


## 3.0 durable runtime

Connector synchronization and outbound actions are queued into PostgreSQL-backed jobs. Jobs are claimed with `FOR UPDATE SKIP LOCKED` on PostgreSQL, leased for a bounded period, heartbeated by the worker, and reclaimed when a lease expires. Provider calls occur outside the claim transaction. Rate budgets and circuit state are scoped by organization + provider.

Outbound action policy is intentionally conservative: 401 may trigger one credential refresh; 429 is retryable with provider backoff; safe/idempotent operations may retry 5xx/network failures; non-idempotent ambiguous failures become `needs_reconciliation` rather than being blindly repeated.
