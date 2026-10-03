-- 013 — runtime integrity, connector action lineage, and safer queue state.
ALTER TABLE integration_events ADD COLUMN IF NOT EXISTS payload_hash varchar(64) NOT NULL DEFAULT '';
ALTER TABLE connector_actions ADD COLUMN IF NOT EXISTS opportunity_id varchar(64);
UPDATE connector_actions SET status = 'queued' WHERE status = 'pending';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_connector_action_org_opp') THEN
    ALTER TABLE connector_actions
      ADD CONSTRAINT fk_connector_action_org_opp
      FOREIGN KEY (organization_id, opportunity_id)
      REFERENCES revenue_opportunities (organization_id, id);
  END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_connector_action_status') THEN
    ALTER TABLE connector_actions DROP CONSTRAINT ck_connector_action_status;
  END IF;
  ALTER TABLE connector_actions
    ADD CONSTRAINT ck_connector_action_status
    CHECK (status IN ('queued','running','retrying','succeeded','failed','needs_reconciliation','cancelled'));
END $$;

CREATE INDEX IF NOT EXISTS ix_connector_action_org_opp ON connector_actions(organization_id, opportunity_id);
CREATE INDEX IF NOT EXISTS ix_integration_event_org_provider_processed ON integration_events(organization_id, provider, processed, created_at);

ALTER TABLE integration_jobs DROP CONSTRAINT IF EXISTS ck_job_type;
ALTER TABLE integration_jobs
  ADD CONSTRAINT ck_job_type
  CHECK (job_type IN ('connector_sync','connector_action','detect_org','agent_pipeline','integration_event'));

ALTER TABLE integration_jobs DROP CONSTRAINT IF EXISTS fk_job_org_integration;
ALTER TABLE integration_jobs
  ADD CONSTRAINT fk_job_org_integration
  FOREIGN KEY (organization_id, integration_id)
  REFERENCES integrations (organization_id, id);
ALTER TABLE integration_jobs DROP CONSTRAINT IF EXISTS fk_job_org_action;
ALTER TABLE integration_jobs
  ADD CONSTRAINT fk_job_org_action
  FOREIGN KEY (organization_id, connector_action_id)
  REFERENCES connector_actions (organization_id, id);

ALTER TABLE connector_receipts ADD COLUMN IF NOT EXISTS provider_status varchar(60);

DROP POLICY IF EXISTS integration_jobs_org_isolation ON integration_jobs;
CREATE POLICY integration_jobs_org_isolation ON integration_jobs
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());
DROP POLICY IF EXISTS connector_rate_states_org_isolation ON connector_rate_states;
CREATE POLICY connector_rate_states_org_isolation ON connector_rate_states
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());
DROP POLICY IF EXISTS connector_receipts_org_isolation ON connector_receipts;
CREATE POLICY connector_receipts_org_isolation ON connector_receipts
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());
DROP POLICY IF EXISTS connector_actions_org_isolation ON connector_actions;
CREATE POLICY connector_actions_org_isolation ON connector_actions
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());
