-- Revenue Recovery OS v2.2 production hardening.
-- Prerequisite: 001_v2_production.sql + 002_integrations_v2.sql.
-- Review/back up production data before applying.

-- 1) Complete schema for runtime models.
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE customers ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE leads ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_contact_at TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS quote_sent_at TIMESTAMPTZ;

ALTER TABLE payments ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS failure_code TEXT;

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS severity TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS source_url TEXT;

ALTER TABLE revenue_opportunities ADD COLUMN IF NOT EXISTS assigned_user_id TEXT;
ALTER TABLE revenue_opportunities ADD COLUMN IF NOT EXISTS urgency NUMERIC(7,5) NOT NULL DEFAULT 0.5;
ALTER TABLE revenue_opportunities ADD COLUMN IF NOT EXISTS recoverability NUMERIC(7,5) NOT NULL DEFAULT 0.5;
ALTER TABLE revenue_opportunities ADD COLUMN IF NOT EXISTS customer_importance NUMERIC(7,5) NOT NULL DEFAULT 0.5;
ALTER TABLE revenue_opportunities ADD COLUMN IF NOT EXISTS priority_score NUMERIC(18,8) NOT NULL DEFAULT 0;
ALTER TABLE revenue_opportunities ADD COLUMN IF NOT EXISTS evidence_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE revenue_opportunities ADD COLUMN IF NOT EXISTS recovered_amount NUMERIC(18,2) NOT NULL DEFAULT 0;

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS source_opportunity_id TEXT;

ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS error TEXT;

ALTER TABLE integrations ADD COLUMN IF NOT EXISTS health_checked_at TIMESTAMPTZ;
ALTER TABLE integration_events ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE integration_events ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;

-- 2) Exact money representation: prevent float rounding in production.
ALTER TABLE leads ALTER COLUMN value TYPE NUMERIC(18,2) USING ROUND(value::numeric, 2);
ALTER TABLE appointments ALTER COLUMN value TYPE NUMERIC(18,2) USING ROUND(value::numeric, 2);
ALTER TABLE transactions ALTER COLUMN amount TYPE NUMERIC(18,2) USING ROUND(amount::numeric, 2);
ALTER TABLE payments ALTER COLUMN amount TYPE NUMERIC(18,2) USING ROUND(amount::numeric, 2);
ALTER TABLE revenue_opportunities ALTER COLUMN estimated_value TYPE NUMERIC(18,2) USING ROUND(estimated_value::numeric, 2);
ALTER TABLE revenue_opportunities ALTER COLUMN expected_value TYPE NUMERIC(18,2) USING ROUND(expected_value::numeric, 2);

-- Keep JSON payload columns aligned with the ORM's explicit JSON-string contract.
ALTER TABLE opportunity_events ALTER COLUMN metadata_json TYPE TEXT USING metadata_json::text;
ALTER TABLE recovery_plans ALTER COLUMN steps_json TYPE TEXT USING steps_json::text;
ALTER TABLE attribution_events ALTER COLUMN metadata_json TYPE TEXT USING metadata_json::text;
ALTER TABLE audit_logs ALTER COLUMN metadata_json TYPE TEXT USING metadata_json::text;

-- 3) Tenant-scoped case-insensitive user uniqueness.
DROP INDEX IF EXISTS users_email_idx;
DROP INDEX IF EXISTS ix_user_email_lower;
DROP INDEX IF EXISTS ux_user_org_email_ci;
DROP INDEX IF EXISTS users_org_email_ci_idx;
CREATE UNIQUE INDEX IF NOT EXISTS users_org_email_ci_idx ON users(organization_id, lower(email));

-- The original v2 action idempotency constraint was global. Replace it with a tenant-scoped index.
ALTER TABLE recovery_actions ADD COLUMN IF NOT EXISTS request_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE recovery_actions DROP CONSTRAINT IF EXISTS recovery_actions_idempotency_key_key;
DROP INDEX IF EXISTS recovery_actions_idempotency_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS recovery_actions_org_idempotency_idx ON recovery_actions(organization_id, idempotency_key);

-- 4) Referenced composite keys for tenant-safe foreign keys.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_users_org_id_ref') THEN
    ALTER TABLE users ADD CONSTRAINT uq_users_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_locations_org_id_ref') THEN
    ALTER TABLE locations ADD CONSTRAINT uq_locations_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_customers_org_id_ref') THEN
    ALTER TABLE customers ADD CONSTRAINT uq_customers_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_leads_org_id_ref') THEN
    ALTER TABLE leads ADD CONSTRAINT uq_leads_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_appointments_org_id_ref') THEN
    ALTER TABLE appointments ADD CONSTRAINT uq_appointments_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_transactions_org_id_ref') THEN
    ALTER TABLE transactions ADD CONSTRAINT uq_transactions_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_payments_org_id_ref') THEN
    ALTER TABLE payments ADD CONSTRAINT uq_payments_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_reviews_org_id_ref') THEN
    ALTER TABLE reviews ADD CONSTRAINT uq_reviews_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_opportunities_org_id_ref') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT uq_opportunities_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_recovery_actions_org_id_ref') THEN
    ALTER TABLE recovery_actions ADD CONSTRAINT uq_recovery_actions_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_integrations_org_id_ref') THEN
    ALTER TABLE integrations ADD CONSTRAINT uq_integrations_org_id_ref UNIQUE (organization_id, id);
  END IF;
END $$;

-- 5) Tenant-safe relationship constraints. Existing single-column FKs remain as a second guard.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_customer_org_location') THEN
    ALTER TABLE customers ADD CONSTRAINT fk_customer_org_location
      FOREIGN KEY (organization_id, location_id) REFERENCES locations(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_lead_org_customer') THEN
    ALTER TABLE leads ADD CONSTRAINT fk_lead_org_customer
      FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_appointment_org_customer') THEN
    ALTER TABLE appointments ADD CONSTRAINT fk_appointment_org_customer
      FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_appointment_org_lead') THEN
    ALTER TABLE appointments ADD CONSTRAINT fk_appointment_org_lead
      FOREIGN KEY (organization_id, lead_id) REFERENCES leads(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tx_org_customer') THEN
    ALTER TABLE transactions ADD CONSTRAINT fk_tx_org_customer
      FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tx_org_source_opportunity') THEN
    ALTER TABLE transactions ADD CONSTRAINT fk_tx_org_source_opportunity
      FOREIGN KEY (organization_id, source_opportunity_id) REFERENCES revenue_opportunities(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_payment_org_customer') THEN
    ALTER TABLE payments ADD CONSTRAINT fk_payment_org_customer
      FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_review_org_customer') THEN
    ALTER TABLE reviews ADD CONSTRAINT fk_review_org_customer
      FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_opp_org_location') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT fk_opp_org_location
      FOREIGN KEY (organization_id, location_id) REFERENCES locations(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_opp_org_customer') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT fk_opp_org_customer
      FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_opp_org_lead') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT fk_opp_org_lead
      FOREIGN KEY (organization_id, lead_id) REFERENCES leads(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_opp_org_assigned_user') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT fk_opp_org_assigned_user
      FOREIGN KEY (organization_id, assigned_user_id) REFERENCES users(organization_id, id);
  END IF;
END $$;

-- 6) New normalized communications/calls/invoices/recovery/attribution/sync entities.
CREATE TABLE IF NOT EXISTS invoices (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  customer_id TEXT,
  external_id TEXT,
  total NUMERIC(18,2) NOT NULL,
  amount_due NUMERIC(18,2) NOT NULL,
  status TEXT NOT NULL,
  due_at TIMESTAMPTZ NOT NULL,
  UNIQUE(organization_id, external_id),
  CONSTRAINT uq_invoice_org_id_ref UNIQUE (organization_id, id),
  CONSTRAINT fk_invoice_org_customer FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id),
  CONSTRAINT ck_invoice_amounts CHECK (total >= 0 AND amount_due >= 0 AND amount_due <= total)
);

CREATE TABLE IF NOT EXISTS communication_events (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  customer_id TEXT,
  lead_id TEXT,
  external_id TEXT,
  provider TEXT NOT NULL DEFAULT 'generic',
  channel TEXT NOT NULL,
  direction TEXT NOT NULL,
  status TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  subject TEXT,
  body_preview TEXT,
  thread_key TEXT,
  UNIQUE(organization_id, external_id),
  CONSTRAINT uq_comm_org_id_ref UNIQUE (organization_id, id),
  CONSTRAINT fk_comm_org_customer FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id),
  CONSTRAINT fk_comm_org_lead FOREIGN KEY (organization_id, lead_id) REFERENCES leads(organization_id, id),
  CONSTRAINT ck_comm_channel CHECK (channel IN ('sms','email','whatsapp','voice')),
  CONSTRAINT ck_comm_direction CHECK (direction IN ('inbound','outbound'))
);
CREATE INDEX IF NOT EXISTS ix_comm_org_customer_time ON communication_events(organization_id, customer_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_comm_org_thread_time ON communication_events(organization_id, thread_key, occurred_at DESC);

CREATE TABLE IF NOT EXISTS call_events (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  customer_id TEXT,
  lead_id TEXT,
  external_id TEXT,
  direction TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  duration_seconds INTEGER NOT NULL DEFAULT 0,
  UNIQUE(organization_id, external_id),
  CONSTRAINT uq_call_org_id_ref UNIQUE (organization_id, id),
  CONSTRAINT fk_call_org_customer FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id),
  CONSTRAINT fk_call_org_lead FOREIGN KEY (organization_id, lead_id) REFERENCES leads(organization_id, id),
  CONSTRAINT ck_call_duration_nonnegative CHECK (duration_seconds >= 0)
);

CREATE TABLE IF NOT EXISTS recovery_approvals (
  id BIGSERIAL PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  action_id BIGINT NOT NULL,
  approved_by_user_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  note TEXT,
  policy_version TEXT NOT NULL DEFAULT '1',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(organization_id, action_id),
  CONSTRAINT fk_approval_org_action FOREIGN KEY (organization_id, action_id) REFERENCES recovery_actions(organization_id, id),
  CONSTRAINT fk_approval_org_user FOREIGN KEY (organization_id, approved_by_user_id) REFERENCES users(organization_id, id),
  CONSTRAINT ck_approval_decision CHECK (decision IN ('approved','rejected'))
);

CREATE TABLE IF NOT EXISTS attribution_links (
  id BIGSERIAL PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  transaction_id TEXT NOT NULL,
  opportunity_id TEXT NOT NULL,
  classification TEXT NOT NULL,
  attributed_amount NUMERIC(18,2) NOT NULL,
  confidence NUMERIC(7,5) NOT NULL,
  primary_attribution BOOLEAN NOT NULL DEFAULT TRUE,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(organization_id, transaction_id, opportunity_id, classification),
  CONSTRAINT fk_attr_org_transaction FOREIGN KEY (organization_id, transaction_id) REFERENCES transactions(organization_id, id),
  CONSTRAINT fk_attr_org_opportunity FOREIGN KEY (organization_id, opportunity_id) REFERENCES revenue_opportunities(organization_id, id),
  CONSTRAINT ck_attr_classification CHECK (classification IN ('direct','assisted','influenced')),
  CONSTRAINT ck_attr_amount CHECK (attributed_amount >= 0),
  CONSTRAINT ck_attr_confidence CHECK (confidence >= 0 AND confidence <= 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_attr_primary_transaction ON attribution_links(organization_id, transaction_id) WHERE primary_attribution = TRUE;

CREATE TABLE IF NOT EXISTS integration_sync_states (
  id BIGSERIAL PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  integration_id TEXT NOT NULL,
  resource TEXT NOT NULL,
  cursor TEXT,
  status TEXT NOT NULL DEFAULT 'idle',
  records_synced INTEGER NOT NULL DEFAULT 0,
  last_started_at TIMESTAMPTZ,
  last_completed_at TIMESTAMPTZ,
  next_run_at TIMESTAMPTZ,
  last_error TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(organization_id, integration_id, resource),
  CONSTRAINT fk_sync_org_integration FOREIGN KEY (organization_id, integration_id) REFERENCES integrations(organization_id, id),
  CONSTRAINT ck_sync_records_nonnegative CHECK (records_synced >= 0)
);

-- 7) New useful constraints/indexes.
ALTER TABLE payments DROP CONSTRAINT IF EXISTS ck_payment_amount_nonnegative;
ALTER TABLE payments ADD CONSTRAINT ck_payment_amount_nonnegative CHECK (amount >= 0);
ALTER TABLE payments DROP CONSTRAINT IF EXISTS ck_payment_retry_count_nonnegative;
ALTER TABLE payments ADD CONSTRAINT ck_payment_retry_count_nonnegative CHECK (retry_count >= 0);

CREATE INDEX IF NOT EXISTS ix_opp_org_priority ON revenue_opportunities(organization_id, status, priority_score DESC, detected_at DESC);
CREATE INDEX IF NOT EXISTS ix_leads_org_quote ON leads(organization_id, quote_sent_at, status);
CREATE INDEX IF NOT EXISTS ix_payments_org_failed ON payments(organization_id, status, failed_at);
CREATE INDEX IF NOT EXISTS ix_invoices_org_due ON invoices(organization_id, status, due_at);
CREATE INDEX IF NOT EXISTS ix_webhooks_org_received ON webhook_events(organization_id, received_at DESC);

-- 8) RLS for all v2.2 tables.
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE communication_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE call_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE recovery_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE attribution_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_sync_states ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['invoices','communication_events','call_events','recovery_approvals','attribution_links','integration_sync_states'] LOOP
    EXECUTE format('DROP POLICY IF EXISTS rros_tenant_policy ON %I', t);
    EXECUTE format('CREATE POLICY rros_tenant_policy ON %I USING (organization_id = rros_current_org()) WITH CHECK (organization_id = rros_current_org())', t);
  END LOOP;
END $$;

-- 9) Narrow security-definer functions for unauthenticated flows that necessarily
-- precede tenant authentication (registration and OAuth callback state lookup).
CREATE OR REPLACE FUNCTION rros_register_tenant(p_name TEXT, p_slug TEXT, p_email TEXT, p_password_hash TEXT)
RETURNS TABLE(user_id TEXT, organization_id TEXT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_org TEXT; v_user TEXT;
BEGIN
  IF p_name IS NULL OR length(trim(p_name)) < 2 OR length(trim(p_name)) > 200 THEN
    RAISE EXCEPTION 'invalid organization name';
  END IF;
  IF p_slug IS NULL OR p_slug !~ '^[a-z0-9-]{2,120}$' THEN
    RAISE EXCEPTION 'invalid organization slug';
  END IF;
  IF p_email IS NULL OR length(trim(p_email)) < 3 OR length(trim(p_email)) > 320 THEN
    RAISE EXCEPTION 'invalid email';
  END IF;
  v_org := 'org_' || replace(gen_random_uuid()::text, '-', '');
  v_user := 'usr_' || replace(gen_random_uuid()::text, '-', '');
  INSERT INTO organizations(id,name,slug,active) VALUES(v_org,trim(p_name),lower(p_slug),TRUE);
  INSERT INTO users(id,organization_id,email,password_hash,role,active,token_version)
    VALUES(v_user,v_org,lower(trim(p_email)),p_password_hash,'owner',TRUE,1);
  RETURN QUERY SELECT v_user,v_org;
END $$;
GRANT EXECUTE ON FUNCTION rros_register_tenant(TEXT,TEXT,TEXT,TEXT) TO PUBLIC;

CREATE OR REPLACE FUNCTION rros_oauth_state_lookup(p_provider TEXT, p_state_hash TEXT)
RETURNS TABLE(organization_id TEXT, user_id TEXT, provider TEXT, expires_at TIMESTAMPTZ)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT s.organization_id, s.user_id, s.provider, s.expires_at
  FROM integration_oauth_states s
  WHERE s.provider = p_provider AND s.state_hash = p_state_hash AND s.used = FALSE AND s.expires_at >= NOW()
  LIMIT 1
$$;
GRANT EXECUTE ON FUNCTION rros_oauth_state_lookup(TEXT,TEXT) TO PUBLIC;

-- 10) Useful summary views for reporting/operations.
CREATE OR REPLACE VIEW rros_revenue_opportunity_summary AS
SELECT organization_id, type, status,
       COUNT(*) AS opportunity_count,
       COALESCE(SUM(estimated_value),0) AS estimated_value,
       COALESCE(SUM(expected_value),0) AS expected_value,
       COALESCE(SUM(recovered_amount),0) AS recovered_amount
FROM revenue_opportunities
GROUP BY organization_id, type, status;

-- 11) Finish composite tenant FKs on existing v2 entities.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_opp_event_org_opportunity') THEN
    ALTER TABLE opportunity_events ADD CONSTRAINT fk_opp_event_org_opportunity
      FOREIGN KEY (organization_id, opportunity_id) REFERENCES revenue_opportunities(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_plan_org_opportunity') THEN
    ALTER TABLE recovery_plans ADD CONSTRAINT fk_plan_org_opportunity
      FOREIGN KEY (organization_id, opportunity_id) REFERENCES revenue_opportunities(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_recovery_actions_org_id_ref') THEN
    ALTER TABLE recovery_actions ADD CONSTRAINT uq_recovery_actions_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_action_org_opportunity') THEN
    ALTER TABLE recovery_actions ADD CONSTRAINT fk_action_org_opportunity
      FOREIGN KEY (organization_id, opportunity_id) REFERENCES revenue_opportunities(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_attr_event_org_opportunity') THEN
    ALTER TABLE attribution_events ADD CONSTRAINT fk_attr_event_org_opportunity
      FOREIGN KEY (organization_id, opportunity_id) REFERENCES revenue_opportunities(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_int_event_org_integration') THEN
    ALTER TABLE integration_events ADD CONSTRAINT fk_int_event_org_integration
      FOREIGN KEY (organization_id, integration_id) REFERENCES integrations(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_oauth_state_org_user') THEN
    ALTER TABLE integration_oauth_states ADD CONSTRAINT fk_oauth_state_org_user
      FOREIGN KEY (organization_id, user_id) REFERENCES users(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_audit_org_actor_user') THEN
    ALTER TABLE audit_logs ADD CONSTRAINT fk_audit_org_actor_user
      FOREIGN KEY (organization_id, actor_user_id) REFERENCES users(organization_id, id);
  END IF;
END $$;

-- 12) Database-enforced domain checks that the application also validates.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_role') THEN
    ALTER TABLE users ADD CONSTRAINT ck_user_role CHECK (role IN ('owner','admin','manager','member','viewer'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_lead_value_nonnegative') THEN
    ALTER TABLE leads ADD CONSTRAINT ck_lead_value_nonnegative CHECK (value >= 0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_opp_amounts') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT ck_opp_amounts CHECK (estimated_value >= 0 AND expected_value >= 0 AND recovered_amount >= 0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_opp_probability') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT ck_opp_probability CHECK (probability >= 0 AND probability <= 1);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_opp_confidence') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT ck_opp_confidence CHECK (confidence >= 0 AND confidence <= 1);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_opp_priority_inputs') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT ck_opp_priority_inputs CHECK (urgency >= 0 AND urgency <= 1 AND recoverability >= 0 AND recoverability <= 1 AND customer_importance >= 0 AND customer_importance <= 1);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_opp_severity') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT ck_opp_severity CHECK (severity IN ('low','medium','high','critical'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_opp_status') THEN
    ALTER TABLE revenue_opportunities ADD CONSTRAINT ck_opp_status CHECK (status IN ('open','investigating','planned','awaiting_approval','in_recovery','engaged','converted','recovered','lost','dismissed','expired'));
  END IF;
END $$;
