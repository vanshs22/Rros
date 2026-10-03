# Revenue Recovery OS — Engineering Blueprint

## Launch loop
1. Connect CRM + Google Business Profile + calendar + payments.
2. Normalize events into the canonical event model.
3. Detect revenue leaks.
4. Create `revenue_opportunity` records with evidence, value, probability and confidence.
5. Generate a recovery plan.
6. Validate consent/policy/permissions before execution.
7. Execute through a connector.
8. Capture outcome and transaction evidence.
9. Classify attribution as estimated / influenced / assisted / directly_attributed / actual_recovered.
10. Learn which playbooks work.

## Pages
- Overview / Revenue Command Center
- Revenue Leaks / Opportunities
- Opportunity Detail / evidence + recovery plan + timeline
- Customers / customer graph + behavior baseline
- Reviews / classification + issue recovery
- Recovery / active plans and approvals
- Inbox / unified threads
- Analytics / leakage, attribution and experiments
- Automations / playbooks
- Integrations / connector health
- Settings / roles, policies, retention and audit

## Canonical API surface
- `GET /api/dashboard`
- `GET /api/opportunities`
- `GET /api/opportunities/{id}`
- `POST /api/opportunities/{id}/recovery-plan`
- `POST /api/opportunities/{id}/execute`
- `POST /api/detect`
- `GET /api/analytics`

Production API additions should follow `/v1/{resource}` and use organization context from authenticated JWT, never client-provided tenant IDs.

## AI agent contracts
Every agent must return structured JSON validated against a schema:
- Lead Investigator: evidence[], diagnosis, missing_data[], confidence
- Churn Analyst: baseline, deviations[], signals[], risk, confidence
- Review Analyst: topic, severity, sentiment, customer_match, recommended_case
- Revenue Analyst: estimated_value, probability, expected_value, assumptions[]
- Recovery Planner: objective, channel, steps[], escalation, approval_required
- Communication Agent: channel, message, consent_required, policy_flags[]
- Action Agent: tool, operation, arguments, idempotency_key
- QA Agent: allow|block, reasons[], policy_checks[]
- ROI Analyst: outcome_class, transaction_ids[], attribution_window, confidence

No agent directly mutates critical systems. The execution path is:
AI decision -> schema validation -> policy validation -> permission check -> approval if required -> connector -> result validation -> audit log.

## Recovery state machine
`open -> investigating -> planned -> awaiting_approval -> in_recovery -> engaged -> converted -> recovered`
Terminal alternatives: `dismissed`, `expired`, `lost`, `blocked`.
Every transition emits an immutable event.

## Connector architecture
`Provider Adapter -> Normalizer -> Canonical Event -> Event Bus -> Revenue Engine`.
Initial connectors: Google Business Profile, HubSpot, GoHighLevel, Gmail/Outlook, Google Calendar, Stripe, Twilio/WhatsApp, generic REST/webhook.

## Attribution rules
Never aggregate estimated opportunity into recovered revenue. A transaction becomes `directly_attributed` only when it falls inside a configured attribution window and the recovery action has a valid causal event chain. `assisted` and `influenced` remain separate.

## Production hardening checklist
- PostgreSQL + RLS
- JWT/RBAC
- encrypted OAuth secrets
- signed webhook verification
- idempotency keys
- retry + exponential backoff
- dead-letter queue
- connector sync checkpoints
- webhook replay
- audit logs
- consent/opt-out enforcement
- action cooldowns
- model/prompt version logging
- AI evaluation suite
- tenant data export/deletion
- observability and alerting
- no fabricated integration success

## Initial vertical
Home services: HVAC, plumbing, electrical, roofing, cleaning, landscaping, pest control, automotive service.

The repository intentionally keeps external sends in simulation mode until credentials, consent, channel policy and connector verification are configured.

## Agent OS runtime

The agent system is a governed pipeline, not an unbounded chatbot:

`Context -> Investigator/Churn/Review -> Revenue -> Planner -> Communication -> QA -> Action -> ROI -> Learning`

Each agent receives a versioned context snapshot and prior-agent handoff. Every run stores input, output, status, timestamps and pipeline version. Handoffs are persisted and tenant-scoped. Agent memory is separated into opportunity-scoped evidence summaries and organization-level outcome learning.

### Agent responsibilities
- Context Agent: canonical evidence pack and missing-data detection.
- Lead Investigator: contact/follow-up/appointment leakage diagnosis.
- Churn Analyst: behavioral purchase interval and dormancy analysis.
- Review Analyst: review severity/topic/recovery relevance.
- Revenue Analyst: value/probability/expected-value reasoning.
- Recovery Planner: channel, steps, escalation and approval requirement.
- Communication Agent: channel-specific compliant draft.
- QA/Guardrail Agent: consent/policy/action gate.
- Action Agent: idempotent connector action contract; it does not bypass approval.
- ROI Analyst: transaction/outcome attribution contract.
- Learning Agent: converts outcomes/feedback into auditable learning signals.

No agent directly mutates an external provider. The only allowed mutation path is `decision -> schema/policy -> permission -> approval -> connector -> result -> audit -> outcome`.

Learning is intentionally separated from autonomous policy mutation. Outcomes and human corrections become versioned signals; changing prompts, models, thresholds or action policy requires an explicit deployment/configuration change.
