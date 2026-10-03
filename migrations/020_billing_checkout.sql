-- 020 — tenant-scoped Stripe Checkout sessions and webhook reconciliation.
CREATE TABLE IF NOT EXISTS billing_sessions (
  id varchar(64) PRIMARY KEY,
  organization_id varchar(64) NOT NULL REFERENCES organizations(id),
  provider_session_id varchar(255) NOT NULL UNIQUE,
  plan_code varchar(40) NOT NULL,
  status varchar(30) NOT NULL DEFAULT 'created',
  checkout_url varchar(2000),
  provider_customer_id varchar(255),
  provider_subscription_id varchar(255),
  metadata_json text NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CONSTRAINT uq_billing_session_org_provider UNIQUE (organization_id, provider_session_id),
  CONSTRAINT ck_billing_session_status CHECK (status IN ('created','completed','expired','cancelled','failed'))
);
CREATE INDEX IF NOT EXISTS ix_billing_sessions_org_created ON billing_sessions(organization_id, created_at);
ALTER TABLE billing_sessions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS billing_sessions_org_isolation ON billing_sessions;
CREATE POLICY billing_sessions_org_isolation ON billing_sessions
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());
