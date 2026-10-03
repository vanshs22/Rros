-- Revenue Recovery OS v2.5 production + sellability hardening.
-- Prerequisite: migrations 001 through 008.
-- Review/back up production data before applying.

-- 1) Prevent concurrent requests/workers from creating duplicate active recovery plans
-- for the same opportunity. Historical completed/cancelled plans remain allowed.
CREATE UNIQUE INDEX IF NOT EXISTS ux_plan_active_org_opp
  ON recovery_plans(organization_id, opportunity_id)
  WHERE status IN ('awaiting_approval','approved','active');

-- 2) Helpful operational indexes for customer/recovery timelines.
CREATE INDEX IF NOT EXISTS ix_recovery_plans_org_created
  ON recovery_plans(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_recovery_actions_org_created
  ON recovery_actions(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_webhook_events_org_received
  ON webhook_events(organization_id, received_at DESC);

-- 3) Make provider event identity explicitly source-scoped at the integration table
-- level. Existing integration_events already include provider in their uniqueness key;
-- retain that invariant and add a high-volume lookup index.
CREATE INDEX IF NOT EXISTS ix_integration_events_org_provider_external
  ON integration_events(organization_id, provider, external_id);
