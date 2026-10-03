-- Revenue Recovery OS v2.5 production hardening.
-- Prerequisite: migrations 001_v2_production.sql through 006_v2_4_sellability_features.sql.
-- Review/back up production data before applying.

-- 1) Align ORM text contracts with PostgreSQL columns introduced as JSONB in early migrations.
ALTER TABLE recovery_actions
  ALTER COLUMN payload_json TYPE TEXT USING payload_json::text;
ALTER TABLE api_keys
  ALTER COLUMN scopes_json TYPE TEXT USING scopes_json::text;

DROP FUNCTION IF EXISTS rros_api_key_lookup(TEXT);
CREATE OR REPLACE FUNCTION rros_api_key_lookup(p_key_hash TEXT)
RETURNS TABLE(id TEXT, organization_id TEXT, scopes_json TEXT, active BOOLEAN, expires_at TIMESTAMPTZ)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT k.id, k.organization_id, k.scopes_json::text, k.active, k.expires_at
  FROM api_keys k
  WHERE k.key_hash = p_key_hash
  LIMIT 1
$$;
GRANT EXECUTE ON FUNCTION rros_api_key_lookup(TEXT) TO PUBLIC;

-- 2) Complete database-side tenant isolation for integration event/state tables.
ALTER TABLE integration_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_oauth_states ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS rros_tenant_policy ON integration_events;
CREATE POLICY rros_tenant_policy ON integration_events
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());
DROP POLICY IF EXISTS rros_tenant_policy ON integration_oauth_states;
CREATE POLICY rros_tenant_policy ON integration_oauth_states
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());

-- 3) Enforce tenant-safe relationships that existed in the ORM but were not present in 002.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_oauth_state_org_id_ref') THEN
    ALTER TABLE integration_oauth_states ADD CONSTRAINT uq_oauth_state_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_int_event_org_id_ref') THEN
    ALTER TABLE integration_events ADD CONSTRAINT uq_int_event_org_id_ref UNIQUE (organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_oauth_state_org_user') THEN
    ALTER TABLE integration_oauth_states ADD CONSTRAINT fk_oauth_state_org_user
      FOREIGN KEY (organization_id, user_id) REFERENCES users(organization_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_int_event_org_integration') THEN
    ALTER TABLE integration_events ADD CONSTRAINT fk_int_event_org_integration
      FOREIGN KEY (organization_id, integration_id) REFERENCES integrations(organization_id, id);
  END IF;
END $$;

-- 4) Useful indexes for high-volume event ingestion and customer timelines.
CREATE INDEX IF NOT EXISTS ix_comm_org_lead_time ON communication_events(organization_id, lead_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_call_org_lead_time ON call_events(organization_id, lead_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_payment_org_customer_status ON payments(organization_id, customer_id, status);
CREATE INDEX IF NOT EXISTS ix_invoice_org_customer_due ON invoices(organization_id, customer_id, due_at DESC);
CREATE INDEX IF NOT EXISTS ix_attribution_org_created ON attribution_links(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_integration_events_org_created ON integration_events(organization_id, created_at DESC);

-- 5) Operational state constraints.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_sync_status') THEN
    ALTER TABLE integration_sync_states ADD CONSTRAINT ck_sync_status CHECK (status IN ('idle','running','succeeded','failed','retrying','dead_letter','paused'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_integration_status') THEN
    ALTER TABLE integrations ADD CONSTRAINT ck_integration_status CHECK (status IN ('not_configured','configured','connected','degraded','error','disconnected'));
  END IF;
END $$;
