-- Revenue Recovery OS v2.4 sellability + operating controls.
-- Prerequisite: migrations 001_v2_production.sql through 005_v2_3_tenant_webhook_and_readiness.sql.
-- Review/back up production data before applying.

-- 1) Commercial / workspace metadata.
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS plan_code TEXT NOT NULL DEFAULT 'starter';
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'trialing';
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS monthly_opportunity_limit INTEGER NOT NULL DEFAULT 1000;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS brand_name TEXT;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS brand_logo_url TEXT;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_org_subscription_status') THEN
    ALTER TABLE organizations ADD CONSTRAINT ck_org_subscription_status CHECK (subscription_status IN ('trialing','active','past_due','cancelled','paused'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_org_opportunity_limit') THEN
    ALTER TABLE organizations ADD CONSTRAINT ck_org_opportunity_limit CHECK (monthly_opportunity_limit > 0);
  END IF;
END $$;

-- 2) Machine-to-machine API keys for CRM/webhook/event ingestion.
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  name TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  scopes_json JSONB NOT NULL DEFAULT '["ingest:write"]',
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_used_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  CONSTRAINT uq_api_key_org_name UNIQUE (organization_id,name),
  CONSTRAINT uq_api_key_org_id_ref UNIQUE (organization_id,id),
  CONSTRAINT ck_api_key_hash_length CHECK (length(key_hash) = 64)
);
CREATE INDEX IF NOT EXISTS ix_api_keys_org_active ON api_keys(organization_id, active);

ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS rros_tenant_policy ON api_keys;
CREATE POLICY rros_tenant_policy ON api_keys
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());

CREATE OR REPLACE FUNCTION rros_api_key_lookup(p_key_hash TEXT)
RETURNS TABLE(id TEXT, organization_id TEXT, scopes_json JSONB, active BOOLEAN, expires_at TIMESTAMPTZ)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT k.id, k.organization_id, k.scopes_json, k.active, k.expires_at
  FROM api_keys k
  WHERE k.key_hash = p_key_hash
  LIMIT 1
$$;
GRANT EXECUTE ON FUNCTION rros_api_key_lookup(TEXT) TO PUBLIC;

-- 3) Review-response workflow. Drafts are distinct from actual public replies.
CREATE TABLE IF NOT EXISTS review_responses (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  review_id TEXT NOT NULL,
  draft_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  approved_by_user_id TEXT,
  external_response_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  sent_at TIMESTAMPTZ,
  CONSTRAINT uq_review_response_org_review UNIQUE (organization_id,review_id),
  CONSTRAINT fk_review_response_org_review FOREIGN KEY (organization_id,review_id) REFERENCES reviews(organization_id,id),
  CONSTRAINT fk_review_response_org_approver FOREIGN KEY (organization_id,approved_by_user_id) REFERENCES users(organization_id,id),
  CONSTRAINT ck_review_response_status CHECK (status IN ('draft','approved','sent','failed','cancelled')),
  CONSTRAINT uq_review_response_org_id_ref UNIQUE (organization_id,id)
);
CREATE INDEX IF NOT EXISTS ix_review_responses_org_status ON review_responses(organization_id,status);
ALTER TABLE review_responses ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS rros_tenant_policy ON review_responses;
CREATE POLICY rros_tenant_policy ON review_responses
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());

-- 4) Reporting indexes.
CREATE INDEX IF NOT EXISTS ix_review_responses_org_review ON review_responses(organization_id, review_id);
CREATE INDEX IF NOT EXISTS ix_api_keys_org_created ON api_keys(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_opp_org_detected ON revenue_opportunities(organization_id, detected_at DESC);

-- 5) Keep all tenant relationships database-enforced.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_recovery_approval_org_action') THEN
    ALTER TABLE recovery_approvals ADD CONSTRAINT fk_recovery_approval_org_action
      FOREIGN KEY (organization_id, action_id) REFERENCES recovery_actions(organization_id,id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_recovery_approval_org_user') THEN
    ALTER TABLE recovery_approvals ADD CONSTRAINT fk_recovery_approval_org_user
      FOREIGN KEY (organization_id, approved_by_user_id) REFERENCES users(organization_id,id);
  END IF;
END $$;
