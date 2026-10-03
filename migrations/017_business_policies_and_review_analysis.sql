-- 017 — governed AI policy storage and durable review analysis.
-- Forward-only and idempotent: safe to apply once through the production runner.
CREATE TABLE IF NOT EXISTS business_policies (
  id varchar(64) PRIMARY KEY,
  organization_id varchar(64) NOT NULL REFERENCES organizations(id),
  name varchar(200) NOT NULL,
  description text,
  policy_type varchar(80) NOT NULL,
  scope_type varchar(40) NOT NULL,
  scope_id varchar(64),
  governance_level integer NOT NULL DEFAULT 2,
  config_json text NOT NULL DEFAULT '{}',
  severity varchar(20) NOT NULL DEFAULT 'medium',
  enforcement_mode varchar(30) NOT NULL DEFAULT 'block',
  active boolean NOT NULL DEFAULT true,
  version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  effective_from timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  CONSTRAINT uq_business_policy_org_name UNIQUE (organization_id, name),
  CONSTRAINT uq_business_policy_scope UNIQUE (organization_id, scope_type, scope_id, policy_type),
  CONSTRAINT ck_business_policy_governance_level CHECK (governance_level BETWEEN 1 AND 5),
  CONSTRAINT ck_business_policy_severity CHECK (severity IN ('low','medium','high','critical')),
  CONSTRAINT ck_business_policy_enforcement CHECK (enforcement_mode IN ('block','warn','require_approval','log_only')),
  CONSTRAINT ck_business_policy_type CHECK (policy_type IN ('ai_governance','compliance','safety','communication','financial')),
  CONSTRAINT ck_business_policy_scope CHECK (scope_type IN ('organization','department','agent_type','channel','customer_segment'))
);
CREATE INDEX IF NOT EXISTS ix_business_policy_org_active
  ON business_policies(organization_id, active, effective_from, expires_at);
ALTER TABLE business_policies ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS business_policies_org_isolation ON business_policies;
CREATE POLICY business_policies_org_isolation ON business_policies
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS analysis_json text NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS ix_reviews_org_created_rating
  ON reviews(organization_id, created_at, rating);

-- The ORM accepts the five checklist governance levels while preserving old rows.
ALTER TABLE agent_definitions DROP CONSTRAINT IF EXISTS ck_agent_def_autonomy_mode;
ALTER TABLE agent_definitions ADD CONSTRAINT ck_agent_def_autonomy_mode
  CHECK (autonomy_mode IN ('observe','recommend','draft','approval_required','auto_execute'));

ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS payload_json text NOT NULL DEFAULT '{}';
